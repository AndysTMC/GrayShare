from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, Response, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

try:
    import smbclient  # type: ignore
except Exception:  # pragma: no cover
    smbclient = None


BASE_DIR = Path(__file__).parent
RESOURCE_DIR = (
    Path(getattr(sys, "_MEIPASS"))
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
    else BASE_DIR
)


def _default_app_data_dir() -> Path:
    """Per-OS default data dir.

    - Windows: %USERPROFILE%\\.grayshare (unchanged from v1)
    - macOS:   ~/Library/Application Support/GrayShare
    - Linux:   $XDG_DATA_HOME/grayshare (default ~/.local/share/grayshare)
    """
    if os.name == "nt":
        return Path(os.getenv("USERPROFILE", str(Path.home()))) / ".grayshare"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "GrayShare"
    xdg = os.getenv("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "grayshare"


APP_DATA_DIR = (
    Path(os.getenv("APP_DATA_DIR"))
    if os.getenv("APP_DATA_DIR")
    else _default_app_data_dir()
)
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
INBOX_DIR = APP_DATA_DIR / "inbox"
INBOX_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = APP_DATA_DIR / "settings.json"
APP_CONFIG_FILE = APP_DATA_DIR / "app_config.json"
WEBVIEW_DATA_DIR = APP_DATA_DIR / "webview"
BACKEND_LOG_FILE = APP_DATA_DIR / "backend.log"

# Large file I/O: avoid tiny default buffer; stream instead of loading whole file into RAM.
COPY_BUFFER_BYTES = int(os.getenv("FILE_IO_BUFFER_BYTES", str(8 * 1024 * 1024)))  # 8 MiB
STREAM_CHUNK_BYTES = int(os.getenv("FILE_STREAM_CHUNK_BYTES", str(1024 * 1024)))  # 1 MiB

# Chunked transfer (parallel upload / parallel download)
CHUNK_MIN_BYTES = int(os.getenv("CHUNK_MIN_BYTES", str(256 * 1024)))  # 256 KiB
CHUNK_MAX_BYTES = int(os.getenv("CHUNK_MAX_BYTES", str(256 * 1024 * 1024)))  # 256 MiB
SHARE_SESSION_STALE_SECONDS = int(os.getenv("SHARE_SESSION_STALE_SECONDS", "45"))
PENDING_UPLOAD_STALE_SECONDS = int(os.getenv("PENDING_UPLOAD_STALE_SECONDS", "1800"))

ACTIVITY_MAX = 200
activity_log: deque = deque(maxlen=ACTIVITY_MAX)
# Persistent transfer history: JSONL append per event, survives restarts.
HISTORY_FILE = APP_DATA_DIR / "history.jsonl"
HISTORY_MAX_LINES = 500
history_lock = threading.Lock()
DEFAULT_CLIENT_SETTINGS = {
    "display_name": "",
    "chunk_mb": 0,
    "threads": 0,
    "theme": "light",
}
DEFAULT_APP_CONFIG = {
    "port": 4567,
}
runtime_current_port = int(os.getenv("APP_PORT", "0") or "0")
runtime_host_ip = str(os.getenv("APP_HOST_IP", "") or "").strip()
runtime_close_callback: Optional[Callable[[], None]] = None
backend_log_lock = threading.Lock()
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR"}


def append_backend_log(message: str, *, level: str = "INFO") -> None:
    try:
        BACKEND_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        normalized_level = str(level or "INFO").upper().strip()
        if normalized_level not in VALID_LOG_LEVELS:
            normalized_level = "INFO"
        with backend_log_lock:
            with BACKEND_LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(f"[{timestamp}] [{normalized_level}] {message}\n")
    except Exception:
        pass


def _append_history_sync(entry: Dict[str, Any]) -> None:
    try:
        with history_lock:
            with HISTORY_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load_history_sync() -> List[Dict[str, Any]]:
    """Load persisted history (oldest last), trimmed to ACTIVITY_MAX."""
    if not HISTORY_FILE.is_file():
        return []
    entries: List[Dict[str, Any]] = []
    try:
        with HISTORY_FILE.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return entries[-ACTIVITY_MAX:]


def log_activity(kind: str, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
    entry = {
        "id": secrets.token_hex(8),
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "message": message,
        "meta": meta or {},
    }
    activity_log.appendleft(entry)
    publish_event("activity", entry)
    _append_history_sync(entry)
    meta_text = ""
    if meta:
        try:
            meta_text = f" | {json.dumps(meta, sort_keys=True, ensure_ascii=False)}"
        except Exception:
            meta_text = f" | {meta!r}"
    append_backend_log(f"[activity:{kind}] {message}{meta_text}")


# --- Server-Sent Events bus -------------------------------------------------
# Clients subscribe to /api/events with the access key and receive instant
# notifications for share starts/stops, replacing the fixed 5s polling loop.
# Fallback polling stays in place for clients/proxies where SSE is unavailable.
event_queues: Set[asyncio.Queue] = set()
event_loop: Optional[asyncio.AbstractEventLoop] = None


def publish_event(event_type: str, payload: Dict[str, Any]) -> None:
    """Fan out an event to all SSE subscribers (safe to call from threads)."""
    if not event_queues:
        return
    message = {"type": event_type, "payload": payload}
    for queue in list(event_queues):
        try:
            loop = event_loop or asyncio.get_event_loop()
            loop.call_soon_threadsafe(queue.put_nowait, message)
        except RuntimeError:
            pass


async def event_stream(request: Request):
    """SSE generator; yields share/activity events until client disconnects."""
    global event_loop
    event_loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    event_queues.add(queue)
    try:
        # Initial hello so the client knows the stream is live.
        yield f"data: {json.dumps({'type': 'hello', 'payload': {'ts': datetime.now(timezone.utc).isoformat()}})}\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                message = await asyncio.wait_for(queue.get(), timeout=15.0)
                yield f"data: {json.dumps(message)}\n\n"
            except asyncio.TimeoutError:
                # Comment keepalive keeps proxies from closing idle streams.
                yield ": keepalive\n\n"
    finally:
        event_queues.discard(queue)


def log_backend_event(
    level: str,
    scope: str,
    message: str,
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    meta_text = ""
    if meta:
        try:
            meta_text = f" | {json.dumps(meta, sort_keys=True, ensure_ascii=False)}"
        except Exception:
            meta_text = f" | {meta!r}"
    append_backend_log(f"[{scope}] {message}{meta_text}", level=level)


def _normalize_client_settings(raw: Dict[str, Any] | None = None) -> ClientSettings:
    data = dict(DEFAULT_CLIENT_SETTINGS)
    if raw:
        data.update(raw)
    data["display_name"] = str(data.get("display_name", "")).strip()
    theme = str(data.get("theme", "light")).lower().strip()
    data["theme"] = "dark" if theme == "dark" else "light"
    return ClientSettings.model_validate(data)


def _normalize_app_config(raw: Dict[str, Any] | None = None) -> AppConfig:
    data = dict(DEFAULT_APP_CONFIG)
    if raw:
        data.update(raw)
    try:
        data["port"] = int(data.get("port", 4567))
    except Exception:
        data["port"] = 4567
    if data["port"] < 1 or data["port"] > 65535:
        data["port"] = 4567
    return AppConfig.model_validate(data)


def _load_client_settings_sync() -> ClientSettings:
    if SETTINGS_FILE.is_file():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            append_backend_log(
                "Failed to read settings.json. Recreating defaults.\n"
                f"{traceback.format_exc().rstrip()}"
            )
            raw = DEFAULT_CLIENT_SETTINGS
    else:
        raw = DEFAULT_CLIENT_SETTINGS
    settings = _normalize_client_settings(raw)
    serialized = json.dumps(settings.model_dump(), indent=2)
    # Write back only when normalization actually changed the stored value,
    # so read paths don't dirty the disk on every request.
    if not SETTINGS_FILE.is_file() or SETTINGS_FILE.read_text(encoding="utf-8") != serialized:
        SETTINGS_FILE.write_text(serialized, encoding="utf-8")
        append_backend_log(f"Client settings loaded from {SETTINGS_FILE}.")
    return settings


def _save_client_settings_sync(settings: ClientSettings) -> ClientSettings:
    normalized = _normalize_client_settings(settings.model_dump())
    SETTINGS_FILE.write_text(
        json.dumps(normalized.model_dump(), indent=2),
        encoding="utf-8",
    )
    append_backend_log(
        f"Client settings saved to {SETTINGS_FILE}."
        f" display_name={normalized.display_name!r} theme={normalized.theme!r}"
        f" chunk_mb={normalized.chunk_mb} threads={normalized.threads}"
    )
    return normalized


def _load_app_config_sync() -> AppConfig:
    if APP_CONFIG_FILE.is_file():
        try:
            raw = json.loads(APP_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            append_backend_log(
                "Failed to read app_config.json. Recreating defaults.\n"
                f"{traceback.format_exc().rstrip()}"
            )
            raw = DEFAULT_APP_CONFIG
    else:
        raw = DEFAULT_APP_CONFIG
    config = _normalize_app_config(raw)
    serialized = json.dumps(config.model_dump(), indent=2)
    # Write back only when normalization changed the stored value (avoids disk
    # thrash on every read).
    if not APP_CONFIG_FILE.is_file() or APP_CONFIG_FILE.read_text(encoding="utf-8") != serialized:
        APP_CONFIG_FILE.write_text(serialized, encoding="utf-8")
        append_backend_log(f"App config loaded from {APP_CONFIG_FILE}. port={config.port}")
    return config


def _save_app_config_sync(config: AppConfig) -> AppConfig:
    normalized = _normalize_app_config(config.model_dump())
    APP_CONFIG_FILE.write_text(
        json.dumps(normalized.model_dump(), indent=2),
        encoding="utf-8",
    )
    append_backend_log(f"App config saved to {APP_CONFIG_FILE}. port={normalized.port}")
    return normalized


def _check_port_availability_sync(port: int, current_port: int) -> tuple[bool, str]:
    if port < 1 or port > 65535:
        return False, "Enter a port between 1 and 65535."
    if current_port > 0 and port == current_port:
        return True, f"Port {port} is the current GrayShare port."

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("0.0.0.0", port))
        except OSError:
            return False, f"Port {port} is not available."
    return True, f"Port {port} is available. The saved change will apply next time you open GrayShare."


def configure_runtime_control(
    *,
    current_port: int,
    host_ip: str | None = None,
    close_callback: Optional[Callable[[], None]] = None,
) -> None:
    global runtime_current_port
    global runtime_host_ip
    global runtime_close_callback
    runtime_current_port = max(0, int(current_port or 0))
    runtime_host_ip = str(host_ip or runtime_host_ip or "").strip()
    runtime_close_callback = close_callback
    append_backend_log(
        f"Runtime control configured. current_port={runtime_current_port} host_ip={runtime_host_ip or 'unset'}"
    )


def _clear_app_data_sync() -> tuple[int, int, List[str]]:
    deleted_items = 0
    preserved_items = 0
    skipped: List[str] = []
    preserve = set()
    if SETTINGS_FILE.exists():
        preserve.add(SETTINGS_FILE.resolve())
    if APP_CONFIG_FILE.exists():
        preserve.add(APP_CONFIG_FILE.resolve())
    if WEBVIEW_DATA_DIR.exists():
        preserve.add(WEBVIEW_DATA_DIR.resolve())

    for path in list(APP_DATA_DIR.iterdir()):
        if path.resolve() in preserve:
            preserved_items += 1
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted_items += 1
        except Exception as exc:
            skipped.append(f"{path.name}: {exc}")

    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    append_backend_log(
        f"Clear data completed. deleted={deleted_items} preserved={preserved_items} skipped={len(skipped)}"
    )
    for skipped_item in skipped:
        append_backend_log(f"Clear data skipped item: {skipped_item}")
    return deleted_items, preserved_items, skipped


class SharingUser(BaseModel):
    sharer_id: str
    display_name: str
    filename: str
    has_passcode: bool
    size_bytes: int


# Capability key: LAN clients must present this (via ?k= on the app URL) to see
# active shares. Distributed through the QR code / network URL; regenerated on
# every backend boot.
ACCESS_KEY = secrets.token_urlsafe(12)


class InboxFile(BaseModel):
    name: str
    size_bytes: int
    modified_iso: str


class ActivityEntry(BaseModel):
    id: str
    ts: str
    kind: str
    message: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class ServerSettings(BaseModel):
    storage_mode: str
    copy_buffer_bytes: int
    stream_chunk_bytes: int
    inbox_path: str
    app_data_path: str
    smb_active: bool


class AppConfig(BaseModel):
    port: int = Field(default=0, ge=0, le=65535)


class DesktopConfigState(BaseModel):
    configured_port: int = Field(default=0, ge=0, le=65535)
    current_port: int = Field(default=0, ge=0, le=65535)
    close_supported: bool


class PortAvailability(BaseModel):
    port: int = Field(ge=1, le=65535)
    available: bool
    message: str
    current_port: int = Field(default=0, ge=0, le=65535)


class SaveAndCloseRequest(BaseModel):
    port: int = Field(ge=1, le=65535)


class SaveAndCloseResult(BaseModel):
    status: str
    port: int = Field(ge=1, le=65535)
    message: str


class AppConfigSaveResult(BaseModel):
    configured_port: int = Field(default=0, ge=0, le=65535)
    current_port: int = Field(default=0, ge=0, le=65535)
    message: str


class ShareInitBody(BaseModel):
    display_name: str
    filename: str
    content_type: str = "application/octet-stream"
    total_size: int
    chunk_size: int
    passcode: Optional[str] = None


class ReceiveInfo(BaseModel):
    filename: str
    size_bytes: int
    chunk_size: int
    chunk_count: int
    content_type: str
    has_passcode: bool


class NetworkInfo(BaseModel):
    ip: str
    port: int
    endpoint: str
    url: str
    access_key: str = ""


class ClientSettings(BaseModel):
    display_name: str = Field(default="", max_length=40)
    chunk_mb: int = Field(default=0, ge=0, le=256)
    threads: int = Field(default=0, ge=0, le=16)
    theme: str = Field(default="light")


class DataClearResult(BaseModel):
    deleted_items: int
    preserved_items: int
    skipped: List[str] = Field(default_factory=list)


class LocalSaveRequest(BaseModel):
    target_path: str
    passcode: Optional[str] = None


class LocalSaveResult(BaseModel):
    saved_path: str
    size_bytes: int


class FrontendLogPayload(BaseModel):
    level: str = Field(default="ERROR", max_length=10)
    message: str = Field(default="", max_length=1000)
    source: str = Field(default="frontend", max_length=80)
    page: str = Field(default="", max_length=500)
    user_agent: str = Field(default="", max_length=500)
    details: Dict[str, Any] = Field(default_factory=dict)


@dataclass
class ShareSession:
    sharer_id: str
    display_name: str
    file_token: str
    filename: str
    size_bytes: int
    content_type: str
    passcode: Optional[str]
    storage_uri: str
    active: bool = True
    # If > 0, clients may use GET /api/receive/{id}/chunk/{i} with this chunk size.
    transfer_chunk_size: int = 0
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class PendingChunkedUpload:
    sharer_id: str
    display_name: str
    filename: str
    content_type: str
    passcode: Optional[str]
    total_size: int
    chunk_size: int
    total_chunks: int
    file_token: str
    # Final file is preallocated at init; chunks are written in place at their
    # offset. No part files, no merge phase.
    target_path: Path
    received: Set[int] = field(default_factory=set)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class StorageBackend:
    async def save_upload(self, upload: UploadFile, file_token: str) -> Tuple[str, int]:
        raise NotImplementedError

    async def open_reader(self, uri: str):
        raise NotImplementedError

    async def delete_file(self, uri: str) -> bool:
        raise NotImplementedError


def _copy_upload_to_path_sync(upload_file, dest_path: Path, buffer_size: int) -> int:
    """Blocking stream copy; run via asyncio.to_thread so the event loop stays responsive."""
    with dest_path.open("wb") as out:
        shutil.copyfileobj(upload_file, out, length=buffer_size)
    return dest_path.stat().st_size


class LocalStorage(StorageBackend):
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def save_upload(self, upload: UploadFile, file_token: str) -> Tuple[str, int]:
        filename = f"{file_token}_{upload.filename}"
        path = self.base_dir / filename
        try:
            size = await asyncio.to_thread(
                _copy_upload_to_path_sync, upload.file, path, COPY_BUFFER_BYTES
            )
        finally:
            await upload.close()
        return str(path), size

    async def open_reader(self, uri: str):
        return open(uri, "rb")

    async def delete_file(self, uri: str) -> bool:
        def _delete_local() -> bool:
            p = Path(uri).resolve()
            base = self.base_dir.resolve()
            try:
                p.relative_to(base)
            except ValueError:
                return False
            if p.is_file():
                p.unlink()
                return True
            return False

        return await asyncio.to_thread(_delete_local)


class SMBStorage(StorageBackend):
    def __init__(self, share_path: str):
        if smbclient is None:
            raise RuntimeError(
                "smbprotocol/smbclient is unavailable. Install dependencies first."
            )
        self.share_path = share_path.rstrip("\\/")
        user = os.getenv("SMB_USERNAME")
        password = os.getenv("SMB_PASSWORD")
        server = os.getenv("SMB_SERVER")
        if user and password:
            smbclient.ClientConfig(username=user, password=password)
        if not server:
            raise RuntimeError("SMB_SERVER is required when SMB mode is enabled.")

    async def save_upload(self, upload: UploadFile, file_token: str) -> Tuple[str, int]:
        filename = f"{file_token}_{upload.filename}"
        uri = f"{self.share_path}\\{filename}"

        def _copy_smb() -> int:
            total = 0
            with smbclient.open_file(uri, mode="wb") as out:
                while True:
                    chunk = upload.file.read(COPY_BUFFER_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    total += len(chunk)
            return total

        try:
            size = await asyncio.to_thread(_copy_smb)
        finally:
            await upload.close()
        return uri, size

    async def open_reader(self, uri: str):
        return smbclient.open_file(uri, mode="rb")

    async def delete_file(self, uri: str) -> bool:
        def _delete_smb() -> bool:
            try:
                smbclient.remove(uri)
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_delete_smb)


def build_storage() -> StorageBackend:
    mode = os.getenv("FILES_STORAGE_MODE", "local").lower().strip()
    if mode == "smb":
        share_path = os.getenv("SMB_SHARE_PATH")
        if not share_path:
            raise RuntimeError(
                "SMB_SHARE_PATH is required when FILES_STORAGE_MODE=smb."
            )
        return SMBStorage(share_path)
    return LocalStorage(INBOX_DIR)


app = FastAPI(title="GrayShare API")
templates = Jinja2Templates(directory=str(RESOURCE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(RESOURCE_DIR / "static")), name="static")

storage = build_storage()
share_sessions: Dict[str, ShareSession] = {}
pending_chunked: Dict[str, PendingChunkedUpload] = {}
# Restore persisted transfer history so the History view survives restarts.
for _entry in reversed(_load_history_sync()):
    activity_log.appendleft(_entry)
append_backend_log(
    "Backend initialized. "
    f"pid={os.getpid()} "
    f"storage_backend={type(storage).__name__} "
    f"storage_mode={os.getenv('FILES_STORAGE_MODE', 'local').lower().strip()} "
    f"app_data={APP_DATA_DIR}"
)


@app.middleware("http")
async def log_unhandled_request_errors(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception:
        append_backend_log(
            f"Unhandled request error for {request.method} {request.url.path}\n"
            f"{traceback.format_exc().rstrip()}",
            level="ERROR",
        )
        raise


def _chunk_spec(session: ShareSession) -> Tuple[int, int]:
    """Logical chunk size and count for parallel downloads."""
    if session.transfer_chunk_size and session.transfer_chunk_size > 0:
        cs = session.transfer_chunk_size
        if session.size_bytes == 0:
            return cs, 1
        cc = max(1, (session.size_bytes + cs - 1) // cs)
        return cs, cc
    if session.size_bytes <= 0:
        return 1, 1
    return session.size_bytes, 1


def _read_range_local(path: Path, offset: int, length: int) -> Iterator[bytes]:
    with path.open("rb") as f:
        f.seek(offset)
        remaining = length
        while remaining > 0:
            n = min(STREAM_CHUNK_BYTES, remaining)
            chunk = f.read(n)
            if not chunk:
                break
            remaining -= len(chunk)
            if chunk:
                yield chunk


def _read_range_smb(uri: str, offset: int, length: int) -> Iterator[bytes]:
    f = smbclient.open_file(uri, mode="rb")
    try:
        f.seek(offset)
        remaining = length
        while remaining > 0:
            n = min(STREAM_CHUNK_BYTES, remaining)
            chunk = f.read(n)
            if not chunk:
                break
            remaining -= len(chunk)
            if chunk:
                yield chunk
    finally:
        f.close()


def _range_iterator_for_session(session: ShareSession, offset: int, length: int) -> Iterator[bytes]:
    if isinstance(storage, SMBStorage):
        yield from _read_range_smb(session.storage_uri, offset, length)
    else:
        path = _resolved_path_under_inbox(session.storage_uri)
        yield from _read_range_local(path, offset, length)


def _write_chunk_at_offset_sync(target: Path, offset: int, upload_file, expected_size: int) -> int:
    """Write one chunk directly into the preallocated file at its offset.

    Each request opens its own handle and writes to a disjoint region, so
    concurrent chunk uploads need no shared lock.
    """
    written = 0
    with target.open("r+b") as out:
        out.seek(offset)
        while True:
            chunk = upload_file.read(COPY_BUFFER_BYTES)
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
    return written


def _preallocate_file_sync(path: Path, size: int) -> None:
    with path.open("wb") as f:
        if size > 0:
            f.truncate(size)


async def _finalize_chunked_upload(pending: PendingChunkedUpload) -> ShareSession:
    safe_name = Path(pending.filename).name or "file.bin"
    final_path = pending.target_path
    size = await asyncio.to_thread(lambda: final_path.stat().st_size)
    if size != pending.total_size or len(pending.received) != pending.total_chunks:
        await asyncio.to_thread(final_path.unlink, missing_ok=True)
        log_backend_event(
            "WARN",
            "transfer",
            "Chunked upload finalize rejected.",
            meta={
                "sharer_id": pending.sharer_id,
                "filename": safe_name,
                "expected_size": pending.total_size,
                "actual_size": size,
                "received_chunks": len(pending.received),
                "total_chunks": pending.total_chunks,
            },
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Upload incomplete or size mismatch: expected {pending.total_size} bytes "
                f"and {pending.total_chunks} chunks, got {size} bytes and "
                f"{len(pending.received)} chunks."
            ),
        )
    session = ShareSession(
        sharer_id=pending.sharer_id,
        display_name=pending.display_name.strip(),
        file_token=pending.file_token,
        filename=safe_name,
        size_bytes=size,
        content_type=pending.content_type or "application/octet-stream",
        passcode=pending.passcode,
        storage_uri=str(final_path.resolve()),
        active=True,
        transfer_chunk_size=pending.chunk_size,
    )
    share_sessions[pending.sharer_id] = session
    pending_chunked.pop(pending.sharer_id, None)
    log_activity(
        "share_start",
        f'{session.display_name} is sharing "{session.filename}" (chunked)',
        {
            "sharer_id": pending.sharer_id,
            "filename": session.filename,
            "size_bytes": session.size_bytes,
            "chunk_size": pending.chunk_size,
        },
    )
    return session


def _resolved_path_under_inbox(storage_uri: str) -> Path:
    """Ensure receive only serves files from our inbox (local mode)."""
    try:
        p = Path(storage_uri).resolve()
        p.relative_to(INBOX_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="File not found.") from exc
    if not p.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return p


def _copy_reader_to_path_sync(reader, dest_path: Path) -> int:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("wb") as out:
        shutil.copyfileobj(reader, out, length=COPY_BUFFER_BYTES)
    return dest_path.stat().st_size


def _copy_session_to_local_path_sync(session: ShareSession, target_path: Path) -> int:
    if isinstance(storage, SMBStorage):
        with smbclient.open_file(session.storage_uri, mode="rb") as inp:
            return _copy_reader_to_path_sync(inp, target_path)
    src_path = _resolved_path_under_inbox(session.storage_uri)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    # The share is removed from the inbox once every receiver is done, so the
    # host copy is the last consumer: a same-volume rename is instant and
    # correct here. Fall back to a full copy across volumes.
    try:
        os.replace(src_path, target_path)
    except OSError:
        shutil.copyfile(src_path, target_path)
    return target_path.stat().st_size


def _is_loopback_request(request: Request) -> bool:
    host = request.client.host if request.client else ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_authorized_request(request: Request, k: str = "") -> bool:
    """Loopback clients are trusted; LAN clients must present the access key.

    Used by endpoints that expose host metadata or admin operations.
    """
    if _is_loopback_request(request):
        return True
    return secrets.compare_digest(str(k or ""), ACCESS_KEY)


def _redact_network_info(info: NetworkInfo) -> NetworkInfo:
    """LAN-safe variant without the capability key or tokenized URL."""
    return NetworkInfo(
        ip=info.ip,
        port=info.port,
        endpoint=info.endpoint,
        url=f'{os.getenv("APP_SCHEME", "http").lower().strip() or "http"}://{info.endpoint}/',
        access_key="",
    )


def _extract_passcode(
    request: Request | None = None,
    query_value: Optional[str] = None,
    form_value: Optional[str] = None,
) -> Optional[str]:
    """Prefer the X-GrayShare-Passcode header; fall back to query/form.

    Header transport keeps passcodes out of URLs (browser history, server logs).
    """
    if request is not None:
        header_value = request.headers.get("x-grayshare-passcode")
        if header_value:
            return header_value
    if form_value:
        return form_value
    return query_value


def _passcode_matches(provided: Optional[str], expected: Optional[str]) -> bool:
    if not expected:
        return True
    if not provided:
        return False
    return secrets.compare_digest(provided.strip(), expected)


def _get_active_session(
    sharer_id: str,
    passcode: Optional[str],
    request: Request | None = None,
) -> ShareSession:
    session = share_sessions.get(sharer_id)
    if not session or not session.active:
        raise HTTPException(status_code=404, detail="Share session not found.")
    if not _passcode_matches(passcode, session.passcode):
        raise HTTPException(status_code=403, detail="Invalid passcode.")
    return session


def _is_share_session_stale(
    session: ShareSession,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(timezone.utc)
    return (current - session.last_seen_at).total_seconds() > SHARE_SESSION_STALE_SECONDS


def _is_pending_upload_stale(
    pending: PendingChunkedUpload,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(timezone.utc)
    return (current - pending.updated_at).total_seconds() > PENDING_UPLOAD_STALE_SECONDS


async def _remove_share_session(
    sharer_id: str,
    *,
    reason: str,
    missing_ok: bool = False,
) -> tuple[ShareSession | None, bool]:
    session = share_sessions.pop(sharer_id, None)
    if not session:
        if missing_ok:
            return None, False
        raise HTTPException(status_code=404, detail="Share session not found.")
    session.active = False
    deleted_file = await storage.delete_file(session.storage_uri)
    if reason == "manual":
        message = f'{session.display_name} stopped sharing "{session.filename}"'
    elif reason == "saved-locally":
        message = f'Saved "{session.filename}" — share ended'
    else:
        message = f'{session.display_name} is no longer sharing "{session.filename}"'
    log_activity(
        "share_stop",
        message,
        {
            "sharer_id": sharer_id,
            "filename": session.filename,
            "reason": reason,
        },
    )
    log_backend_event(
        "WARN" if not deleted_file else "INFO",
        "transfer",
        "Share session removed.",
        meta={
            "sharer_id": sharer_id,
            "filename": session.filename,
            "reason": reason,
            "deleted_file": deleted_file,
        },
    )
    return session, deleted_file


async def _prune_stale_share_sessions() -> None:
    now = datetime.now(timezone.utc)
    stale_ids = [
        sharer_id
        for sharer_id, session in list(share_sessions.items())
        if session.active and _is_share_session_stale(session, now)
    ]
    for sharer_id in stale_ids:
        await _remove_share_session(sharer_id, reason="stale", missing_ok=True)


async def _prune_stale_pending_uploads() -> None:
    now = datetime.now(timezone.utc)
    stale_items = [
        (sharer_id, pending)
        for sharer_id, pending in list(pending_chunked.items())
        if _is_pending_upload_stale(pending, now)
    ]
    for sharer_id, pending in stale_items:
        pending_chunked.pop(sharer_id, None)
        await asyncio.to_thread(
            lambda p=pending.target_path: p.unlink(missing_ok=True)
        )
        log_backend_event(
            "WARN",
            "transfer",
            "Discarded stale pending chunk upload.",
            meta={
                "sharer_id": sharer_id,
                "filename": pending.filename,
                "received_chunks": len(pending.received),
                "total_chunks": pending.total_chunks,
            },
        )


def _parse_byte_range(range_header: str | None, size: int) -> tuple[int, int] | None:
    if not range_header:
        return None
    value = str(range_header).strip()
    if not value:
        return None
    if not value.lower().startswith("bytes="):
        raise HTTPException(
            status_code=416,
            detail="Invalid Range header.",
            headers={"Content-Range": f"bytes */{size}"},
        )
    spec = value[6:].strip()
    if "," in spec:
        raise HTTPException(
            status_code=416,
            detail="Multiple ranges are not supported.",
            headers={"Content-Range": f"bytes */{size}"},
        )
    if "-" not in spec:
        raise HTTPException(
            status_code=416,
            detail="Invalid Range header.",
            headers={"Content-Range": f"bytes */{size}"},
        )

    start_text, end_text = spec.split("-", 1)
    start_text = start_text.strip()
    end_text = end_text.strip()

    if size < 0:
        size = 0
    if size == 0:
        raise HTTPException(
            status_code=416,
            detail="Requested range is not satisfiable.",
            headers={"Content-Range": "bytes */0"},
        )

    if not start_text:
        try:
            suffix_length = int(end_text)
        except Exception as exc:
            raise HTTPException(
                status_code=416,
                detail="Invalid Range header.",
                headers={"Content-Range": f"bytes */{size}"},
            ) from exc
        if suffix_length <= 0:
            raise HTTPException(
                status_code=416,
                detail="Requested range is not satisfiable.",
                headers={"Content-Range": f"bytes */{size}"},
            )
        if suffix_length >= size:
            return 0, size - 1
        return size - suffix_length, size - 1

    try:
        start = int(start_text)
    except Exception as exc:
        raise HTTPException(
            status_code=416,
            detail="Invalid Range header.",
            headers={"Content-Range": f"bytes */{size}"},
        ) from exc

    if end_text:
        try:
            end = int(end_text)
        except Exception as exc:
            raise HTTPException(
                status_code=416,
                detail="Invalid Range header.",
                headers={"Content-Range": f"bytes */{size}"},
            ) from exc
    else:
        end = size - 1

    if start < 0 or end < start or start >= size:
        raise HTTPException(
            status_code=416,
            detail="Requested range is not satisfiable.",
            headers={"Content-Range": f"bytes */{size}"},
        )

    return start, min(end, size - 1)


def _content_disposition(filename: str, *, attachment: bool = True) -> str:
    """RFC 6266/5987 Content-Disposition value, safe for quotes/non-ASCII."""
    safe = (filename or "file.bin").replace("\\", "_").replace('"', "_")
    safe = safe.replace("\r", " ").replace("\n", " ")
    try:
        safe.encode("ascii")
        return f'{"attachment" if attachment else "inline"}; filename="{safe}"'
    except UnicodeEncodeError:
        pass
    from urllib.parse import quote

    quoted = quote(safe, safe="")
    return (
        f'{"attachment" if attachment else "inline"}; filename="{safe.encode("ascii", "ignore").decode() or "file"}"; '
        f"filename*=UTF-8''{quoted}"
    )


def _build_receive_response(session: ShareSession, request: Request):
    total_size = max(0, int(session.size_bytes or 0))
    media_type = session.content_type or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(session.filename),
    }
    range_spec = _parse_byte_range(request.headers.get("range"), total_size)

    if range_spec is None:
        if total_size == 0:
            headers["Content-Length"] = "0"
            return Response(content=b"", media_type=media_type, headers=headers)
        headers["Content-Length"] = str(total_size)
        return StreamingResponse(
            _range_iterator_for_session(session, 0, total_size),
            media_type=media_type,
            headers=headers,
        )

    start, end = range_spec
    length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(
        _range_iterator_for_session(session, start, length),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


def _detect_local_ip() -> str:
    env_ip = os.getenv("APP_HOST_IP")
    if env_ip:
        return env_ip
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        host = socket.gethostname()
        ip = socket.gethostbyname(host)
        if ip:
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def _current_network_info() -> NetworkInfo:
    port = runtime_current_port or int(os.getenv("APP_PORT", "8000"))
    ip = runtime_host_ip or _detect_local_ip()
    scheme = os.getenv("APP_SCHEME", "http").lower().strip() or "http"
    endpoint = f"{ip}:{port}"
    return NetworkInfo(
        ip=ip,
        port=port,
        endpoint=endpoint,
        url=f"{scheme}://{endpoint}/?k={ACCESS_KEY}",
        access_key=ACCESS_KEY,
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # Newer Starlette requires request as a keyword argument.
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/manifest.webmanifest")
async def web_manifest():
    return FileResponse(
        path=RESOURCE_DIR / "static" / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        path=RESOURCE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/"},
    )


@app.get("/api/events")
async def sse_events(request: Request, k: str = Query(default="")):
    """Server-Sent Events stream: instant share/activity notifications.

    Gated by the same access key as /api/shares — event payloads carry
    filenames and sharer names.
    """
    if not secrets.compare_digest(str(k or ""), ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Invalid or missing access key.")
    return StreamingResponse(
        event_stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/shares", response_model=List[SharingUser])
async def list_shares(k: str = Query(default="")):
    await _prune_stale_share_sessions()
    await _prune_stale_pending_uploads()
    # Share metadata (names, filenames, sizes) is only revealed to clients that
    # present the access key distributed via the QR code / network URL.
    if not secrets.compare_digest(str(k or ""), ACCESS_KEY):
        raise HTTPException(status_code=403, detail="Invalid or missing access key.")
    return [
        SharingUser(
            sharer_id=session.sharer_id,
            display_name=session.display_name,
            filename=session.filename,
            has_passcode=bool(session.passcode),
            size_bytes=session.size_bytes,
        )
        for session in share_sessions.values()
        if session.active
    ]


def _list_inbox_files_sync() -> List[InboxFile]:
    items: List[InboxFile] = []
    if not INBOX_DIR.is_dir():
        return items
    paths = [p for p in INBOX_DIR.iterdir() if p.is_file()]
    for p in sorted(paths, key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        items.append(
            InboxFile(
                name=p.name,
                size_bytes=st.st_size,
                modified_iso=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            )
        )
    return items


@app.get("/api/inbox", response_model=List[InboxFile])
async def list_inbox(request: Request, k: str = Query(default="")):
    """Files stored under the server `inbox/` folder (local storage mode uploads)."""
    if not _is_authorized_request(request, k):
        raise HTTPException(status_code=403, detail="Invalid or missing access key.")
    return await asyncio.to_thread(_list_inbox_files_sync)


@app.get("/api/activity", response_model=List[ActivityEntry])
async def list_activity(request: Request, k: str = Query(default="")):
    """Recent transfer events (persisted to history.jsonl across restarts)."""
    if not _is_authorized_request(request, k):
        raise HTTPException(status_code=403, detail="Invalid or missing access key.")
    return list(activity_log)


@app.get("/api/settings", response_model=ServerSettings)
async def get_server_settings(request: Request, k: str = Query(default="")):
    """Non-secret server configuration for the settings UI.

    Host filesystem paths are redacted for LAN clients; loopback gets full
    detail (the desktop Settings page shows the data folder).
    """
    authorized = _is_authorized_request(request, k)
    return ServerSettings(
        storage_mode=os.getenv("FILES_STORAGE_MODE", "local").lower().strip(),
        copy_buffer_bytes=COPY_BUFFER_BYTES,
        stream_chunk_bytes=STREAM_CHUNK_BYTES,
        inbox_path=str(INBOX_DIR.resolve()) if authorized else "",
        app_data_path=str(APP_DATA_DIR.resolve()) if authorized else "",
        smb_active=isinstance(storage, SMBStorage),
    )


@app.get("/api/settings/client", response_model=ClientSettings)
async def get_client_settings(request: Request, k: str = Query(default="")):
    if not _is_authorized_request(request, k):
        raise HTTPException(status_code=403, detail="Invalid or missing access key.")
    return await asyncio.to_thread(_load_client_settings_sync)


@app.put("/api/settings/client", response_model=ClientSettings)
async def update_client_settings(settings: ClientSettings, request: Request):
    # The host's settings.json is a local resource; LAN clients keep their
    # own copy in localStorage and must not mutate the host's file.
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Host settings are only available locally.")
    return await asyncio.to_thread(_save_client_settings_sync, settings)


@app.get("/api/app/config", response_model=DesktopConfigState)
async def get_desktop_config(request: Request):
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Desktop settings are only available locally.")
    config = await asyncio.to_thread(_load_app_config_sync)
    return DesktopConfigState(
        configured_port=config.port,
        current_port=runtime_current_port,
        close_supported=runtime_close_callback is not None,
    )


@app.put("/api/app/config", response_model=AppConfigSaveResult)
async def update_desktop_config(config: AppConfig, request: Request):
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Desktop settings are only available locally.")
    if config.port < 1 or config.port > 65535:
        raise HTTPException(status_code=400, detail="Enter a port between 1 and 65535.")
    available, message = await asyncio.to_thread(
        _check_port_availability_sync,
        config.port,
        runtime_current_port,
    )
    if not available:
        raise HTTPException(status_code=409, detail=message)
    saved = await asyncio.to_thread(_save_app_config_sync, config)
    return AppConfigSaveResult(
        configured_port=saved.port,
        current_port=runtime_current_port,
        message=f"Port {saved.port} saved. The change will take effect the next time you open GrayShare.",
    )


@app.get("/api/app/port-check", response_model=PortAvailability)
async def check_desktop_port(request: Request, port: int = Query(..., ge=1, le=65535)):
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Desktop settings are only available locally.")
    available, message = await asyncio.to_thread(
        _check_port_availability_sync,
        port,
        runtime_current_port,
    )
    return PortAvailability(
        port=port,
        available=available,
        message=message,
        current_port=runtime_current_port,
    )


@app.post("/api/app/save-and-close", response_model=SaveAndCloseResult)
async def save_and_close_desktop_app(
    payload: SaveAndCloseRequest,
    request: Request,
    background_tasks: BackgroundTasks,
):
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Desktop close is only available locally.")
    if runtime_close_callback is None:
        raise HTTPException(status_code=501, detail="Save and close is only available in the desktop app.")
    available, message = await asyncio.to_thread(
        _check_port_availability_sync,
        payload.port,
        runtime_current_port,
    )
    if not available:
        raise HTTPException(status_code=409, detail=message)
    await asyncio.to_thread(_save_app_config_sync, AppConfig(port=payload.port))
    background_tasks.add_task(runtime_close_callback)
    return SaveAndCloseResult(
        status="closing",
        port=payload.port,
        message=f"Saved port {payload.port}. GrayShare is closing.",
    )


@app.post("/api/data/clear", response_model=DataClearResult)
async def clear_app_data(request: Request):
    # Destructive host operation — loopback only. A LAN device (or a malicious
    # webpage firing cross-origin POSTs) must never be able to wipe the host's
    # inbox and history.
    if not _is_loopback_request(request):
        raise HTTPException(status_code=403, detail="Clear data is only available locally.")
    for session in list(share_sessions.values()):
        session.active = False
        try:
            await storage.delete_file(session.storage_uri)
        except Exception:
            pass
    share_sessions.clear()
    pending_chunked.clear()
    activity_log.clear()
    deleted_items, preserved_items, skipped = await asyncio.to_thread(_clear_app_data_sync)
    # history.jsonl lives under APP_DATA_DIR so _clear_app_data_sync already
    # removed it; make sure a fresh empty file exists for future appends.
    await asyncio.to_thread(lambda: HISTORY_FILE.touch(exist_ok=True))
    if SETTINGS_FILE.exists():
        skipped.append("settings.json preserved")
    if APP_CONFIG_FILE.exists():
        skipped.append("app_config.json preserved")
    if WEBVIEW_DATA_DIR.exists():
        skipped.append("webview profile preserved so local settings remain available")
    return DataClearResult(
        deleted_items=deleted_items,
        preserved_items=preserved_items,
        skipped=skipped,
    )


@app.get("/api/health")
async def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


@app.get("/api/network/info", response_model=NetworkInfo)
async def network_info(request: Request, k: str = Query(default="")):
    """Connection info for the UI.

    The access key / tokenized URL is only revealed to loopback clients or
    callers already presenting a valid key — otherwise this endpoint would
    defeat the capability gating on /api/shares and /api/events.
    """
    info = await asyncio.to_thread(_current_network_info)
    if _is_authorized_request(request, k):
        return info
    return _redact_network_info(info)


@app.post("/api/telemetry/upload-probe")
async def upload_probe(request: Request):
    """Discard body; used by the client to estimate upload throughput."""
    await request.body()
    return Response(status_code=204)


@app.post("/api/log/client")
async def client_log(payload: FrontendLogPayload):
    level = str(payload.level or "ERROR").upper().strip()
    if level not in VALID_LOG_LEVELS:
        level = "INFO"
    log_backend_event(
        level,
        "frontend",
        payload.message or "Client-side log event",
        meta={
            "source": payload.source,
            "page": payload.page,
            "user_agent": payload.user_agent,
            "details": payload.details,
        },
    )
    return {"ok": True}


@app.post("/api/share/init")
async def share_init(body: ShareInitBody):
    """Start a parallel chunked upload (local storage only)."""
    await _prune_stale_pending_uploads()
    if isinstance(storage, SMBStorage):
        raise HTTPException(
            status_code=501,
            detail="Chunked upload requires FILES_STORAGE_MODE=local (not SMB).",
        )
    if body.total_size < 0:
        raise HTTPException(status_code=400, detail="Invalid total_size.")
    if body.chunk_size < CHUNK_MIN_BYTES or body.chunk_size > CHUNK_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"chunk_size must be between {CHUNK_MIN_BYTES} and {CHUNK_MAX_BYTES} bytes.",
        )
    display_name = body.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name is required.")
    safe_name = Path(body.filename).name or "file.bin"
    if body.total_size == 0:
        total_chunks = 1
    else:
        total_chunks = (body.total_size + body.chunk_size - 1) // body.chunk_size
    sharer_id = secrets.token_urlsafe(10)
    file_token = secrets.token_urlsafe(12)
    target_path = INBOX_DIR / f"{file_token}_{safe_name}"
    await asyncio.to_thread(_preallocate_file_sync, target_path, body.total_size)
    pending = PendingChunkedUpload(
        sharer_id=sharer_id,
        display_name=display_name,
        filename=safe_name,
        content_type=body.content_type or "application/octet-stream",
        passcode=body.passcode.strip() if body.passcode else None,
        total_size=body.total_size,
        chunk_size=body.chunk_size,
        total_chunks=total_chunks,
        file_token=file_token,
        target_path=target_path,
    )
    pending_chunked[sharer_id] = pending
    log_backend_event(
        "INFO",
        "transfer",
        "Chunked share initialized.",
        meta={
            "sharer_id": sharer_id,
            "filename": safe_name,
            "display_name": display_name,
            "total_size": body.total_size,
            "chunk_size": body.chunk_size,
            "total_chunks": total_chunks,
        },
    )
    network = await asyncio.to_thread(_current_network_info)
    return {
        "sharer_id": sharer_id,
        "total_chunks": total_chunks,
        "url": network.url,
        "endpoint": network.endpoint,
    }


@app.post("/api/share/{sharer_id}/finalize")
async def share_finalize(sharer_id: str):
    """Client calls this after all chunks are uploaded to trigger the merge.
    Returns the completed ShareSession."""
    await _prune_stale_pending_uploads()
    pending = pending_chunked.get(sharer_id)
    if not pending:
        raise HTTPException(status_code=404, detail="Pending upload not found.")
    needed = set(range(pending.total_chunks))
    if pending.received != needed:
        missing = needed - pending.received
        log_backend_event(
            "WARN",
            "transfer",
            "Chunked share finalize rejected because chunks are missing.",
            meta={"sharer_id": sharer_id, "missing_chunks": sorted(missing)},
        )
        raise HTTPException(
            status_code=400,
            detail=f"Missing chunks: {sorted(missing)}.",
        )
    session = await _finalize_chunked_upload(pending)
    log_backend_event(
        "INFO",
        "transfer",
        "Chunked share finalized.",
        meta={
            "sharer_id": session.sharer_id,
            "filename": session.filename,
            "size_bytes": session.size_bytes,
            "chunk_size": session.transfer_chunk_size,
        },
    )
    network = await asyncio.to_thread(_current_network_info)
    return {
        "sharer_id": session.sharer_id,
        "filename": session.filename,
        "url": network.url,
        "endpoint": network.endpoint,
    }


@app.post("/api/share/{sharer_id}/chunk")
async def share_upload_chunk(
    sharer_id: str,
    chunk_index: int = Form(...),
    file: UploadFile = File(...),
):
    pending = pending_chunked.get(sharer_id)
    if not pending:
        if sharer_id in share_sessions:
            return {"ok": True, "chunk_index": chunk_index, "complete": True}
        raise HTTPException(status_code=404, detail="Upload session not found.")
    if chunk_index < 0 or chunk_index >= pending.total_chunks:
        raise HTTPException(status_code=400, detail="Invalid chunk_index.")

    if pending.total_size == 0:
        expected = 0
    else:
        expected = (
            pending.chunk_size
            if chunk_index < pending.total_chunks - 1
            else pending.total_size - chunk_index * pending.chunk_size
        )

    complete = False
    # Each chunk writes to a disjoint region of the preallocated file via its
    # own file handle, so concurrent uploads need no shared lock.
    offset = chunk_index * pending.chunk_size
    try:
        written = await asyncio.to_thread(
            _write_chunk_at_offset_sync, pending.target_path, offset, file.file, expected
        )
    finally:
        await file.close()
    if written != expected:
        pending.received.discard(chunk_index)
        log_backend_event(
            "WARN",
            "transfer",
            "Chunk upload rejected because its size did not match expectation.",
            meta={
                "sharer_id": sharer_id,
                "chunk_index": chunk_index,
                "expected_size": expected,
                "actual_size": written,
            },
        )
        raise HTTPException(
            status_code=400,
            detail=f"Chunk size mismatch: expected {expected} bytes, got {written}.",
        )
    pending.received.add(chunk_index)
    pending.updated_at = datetime.now(timezone.utc)

    return {"ok": True, "chunk_index": chunk_index, "complete": False}


@app.get("/api/receive/{sharer_id}/info", response_model=ReceiveInfo)
async def receive_info(
    sharer_id: str,
    request: Request,
    passcode: Optional[str] = Query(default=None),
):
    await _prune_stale_share_sessions()
    session = _get_active_session(
        sharer_id, _extract_passcode(request, query_value=passcode), request=request
    )
    cs, cc = _chunk_spec(session)
    log_backend_event(
        "INFO",
        "transfer",
        "Receiver requested share metadata.",
        meta={
            "sharer_id": sharer_id,
            "filename": session.filename,
            "chunk_count": cc,
            "has_passcode": bool(session.passcode),
        },
    )
    return ReceiveInfo(
        filename=session.filename,
        size_bytes=session.size_bytes,
        chunk_size=cs,
        chunk_count=cc,
        content_type=session.content_type or "application/octet-stream",
        has_passcode=bool(session.passcode),
    )


@app.get("/api/receive/{sharer_id}/chunk/{chunk_index}")
async def receive_chunk_get(
    sharer_id: str,
    chunk_index: int,
    request: Request,
    passcode: Optional[str] = Query(default=None),
):
    await _prune_stale_share_sessions()
    session = _get_active_session(
        sharer_id, _extract_passcode(request, query_value=passcode), request=request
    )

    cs, cc = _chunk_spec(session)
    if chunk_index < 0 or chunk_index >= cc:
        raise HTTPException(status_code=400, detail="Invalid chunk index.")

    # Avoid duplicate logs with POST /receive when there is only one chunk.
    if chunk_index == 0 and cc > 1:
        log_activity(
            "receive",
            f'Download started: "{session.filename}" from {session.display_name} (parallel chunks)',
            {
                "sharer_id": sharer_id,
                "filename": session.filename,
                "from": session.display_name,
            },
        )

    if session.transfer_chunk_size and session.transfer_chunk_size > 0:
        offset = chunk_index * session.transfer_chunk_size
        length = min(session.transfer_chunk_size, session.size_bytes - offset)
    else:
        if chunk_index != 0:
            raise HTTPException(status_code=400, detail="Invalid chunk index.")
        offset = 0
        length = session.size_bytes

    if length == 0:
        return Response(
            content=b"",
            media_type=session.content_type or "application/octet-stream",
            headers={"Content-Length": "0"},
        )

    headers = {
        "Content-Disposition": _content_disposition(f"{session.filename}.part{chunk_index}"),
        "Content-Length": str(length),
    }
    return StreamingResponse(
        _range_iterator_for_session(session, offset, length),
        media_type=session.content_type or "application/octet-stream",
        headers=headers,
    )


@app.post("/api/share")
async def start_share(
    display_name: str = Form(...),
    passcode: Optional[str] = Form(default=None),
    file: UploadFile = File(...),
):
    display_name = display_name.strip()
    if not display_name:
        raise HTTPException(status_code=400, detail="display_name is required.")
    sharer_id = secrets.token_urlsafe(10)
    file_token = secrets.token_urlsafe(12)
    storage_uri, saved_size = await storage.save_upload(file, file_token)
    session = ShareSession(
        sharer_id=sharer_id,
        display_name=display_name,
        file_token=file_token,
        filename=Path(file.filename or "file.bin").name or "file.bin",
        size_bytes=saved_size if saved_size else (file.size or 0),
        content_type=file.content_type or "application/octet-stream",
        passcode=passcode.strip() if passcode else None,
        storage_uri=storage_uri,
        active=True,
        transfer_chunk_size=0,
    )
    share_sessions[sharer_id] = session
    log_backend_event(
        "INFO",
        "transfer",
        "Single-request share created.",
        meta={
            "sharer_id": sharer_id,
            "filename": session.filename,
            "size_bytes": session.size_bytes,
            "display_name": session.display_name,
        },
    )
    log_activity(
        "share_start",
        f'{session.display_name} is sharing "{session.filename}"',
        {
            "sharer_id": sharer_id,
            "filename": session.filename,
            "size_bytes": session.size_bytes,
        },
    )
    network = await asyncio.to_thread(_current_network_info)
    return {
        "sharer_id": sharer_id,
        "status": "sharing",
        "url": network.url,
        "endpoint": network.endpoint,
    }


@app.post("/api/share/{sharer_id}/stop")
async def stop_share(sharer_id: str):
    log_backend_event("INFO", "transfer", "Manual stop requested for share.", meta={"sharer_id": sharer_id})
    _, deleted_file = await _remove_share_session(sharer_id, reason="manual")
    return {"status": "stopped", "deleted_file": deleted_file}


@app.post("/api/share/{sharer_id}/heartbeat")
async def share_heartbeat(sharer_id: str):
    session = share_sessions.get(sharer_id)
    if not session or not session.active:
        raise HTTPException(status_code=404, detail="Share session not found.")
    session.last_seen_at = datetime.now(timezone.utc)
    return {"ok": True}


@app.post("/api/receive/{sharer_id}")
async def receive_file(
    sharer_id: str,
    request: Request,
    passcode: Optional[str] = Form(default=None),
):
    await _prune_stale_share_sessions()
    session = _get_active_session(
        sharer_id, _extract_passcode(request, form_value=passcode), request=request
    )
    log_backend_event(
        "INFO",
        "transfer",
        "Standard receive requested.",
        meta={"sharer_id": sharer_id, "filename": session.filename, "mode": "post-form"},
    )

    log_activity(
        "receive",
        f'Download started: "{session.filename}" from {session.display_name}',
        {
            "sharer_id": sharer_id,
            "filename": session.filename,
            "from": session.display_name,
        },
    )

    return _build_receive_response(session, request)


@app.get("/api/receive/{sharer_id}/download")
async def receive_file_download(
    sharer_id: str,
    request: Request,
    passcode: Optional[str] = Query(default=None),
):
    await _prune_stale_share_sessions()
    session = _get_active_session(
        sharer_id, _extract_passcode(request, query_value=passcode), request=request
    )
    log_backend_event(
        "INFO",
        "transfer",
        "Browser-native download requested.",
        meta={"sharer_id": sharer_id, "filename": session.filename, "mode": "browser-native"},
    )
    log_activity(
        "receive",
        f'Browser download started: "{session.filename}" from {session.display_name}',
        {
            "sharer_id": sharer_id,
            "filename": session.filename,
            "from": session.display_name,
            "mode": "browser-native",
        },
    )
    return _build_receive_response(session, request)


@app.post("/api/receive/{sharer_id}/save-local", response_model=LocalSaveResult)
async def receive_file_save_local(
    sharer_id: str,
    payload: LocalSaveRequest,
    request: Request,
):
    await _prune_stale_share_sessions()
    if not _is_loopback_request(request):
        raise HTTPException(
            status_code=403,
            detail="Local save is only available from this device.",
        )

    session = share_sessions.get(sharer_id)
    if not session or not session.active:
        raise HTTPException(status_code=404, detail="Share session not found.")
    if not _passcode_matches(_extract_passcode(request, form_value=payload.passcode), session.passcode):
        raise HTTPException(status_code=403, detail="Invalid passcode.")

    target_path = Path(payload.target_path).expanduser()
    if not target_path.is_absolute():
        raise HTTPException(status_code=400, detail="Choose an absolute file path.")
    if target_path.exists() and target_path.is_dir():
        raise HTTPException(status_code=400, detail="Choose a file path, not a folder.")
    if not target_path.parent.exists():
        raise HTTPException(status_code=400, detail="Target folder does not exist.")

    log_backend_event(
        "INFO",
        "transfer",
        "Desktop local save requested.",
        meta={
            "sharer_id": sharer_id,
            "filename": session.filename,
            "target_path": str(target_path),
        },
    )
    size = await asyncio.to_thread(_copy_session_to_local_path_sync, session, target_path)
    log_activity(
        "receive",
        f'Saved "{session.filename}" from {session.display_name} to "{target_path.name}"',
        {
            "sharer_id": sharer_id,
            "filename": session.filename,
            "from": session.display_name,
            "saved_path": str(target_path),
            "size_bytes": size,
        },
    )
    # The desktop save consumed the file (moved out of the inbox on the same
    # volume), so end the share session cleanly. missing_ok guards the
    # cross-volume fallback where the inbox copy still exists — the sender can
    # keep sharing in that case.
    await _remove_share_session(sharer_id, reason="saved-locally", missing_ok=True)
    return LocalSaveResult(saved_path=str(target_path), size_bytes=size)

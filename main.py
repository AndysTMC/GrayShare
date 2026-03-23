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
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
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
APP_DATA_DIR = (
    Path(os.getenv("APP_DATA_DIR"))
    if os.getenv("APP_DATA_DIR")
    else Path(os.getenv("USERPROFILE", str(Path.home()))) / ".grayshare"
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
SHARE_SESSION_STALE_SECONDS = int(os.getenv("SHARE_SESSION_STALE_SECONDS", "15"))

ACTIVITY_MAX = 100
activity_log: deque = deque(maxlen=ACTIVITY_MAX)
DEFAULT_CLIENT_SETTINGS = {
    "display_name": "",
    "chunk_mb": 0,
    "threads": 0,
    "refresh_sec": 5,
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


def log_activity(kind: str, message: str, meta: Optional[Dict[str, Any]] = None) -> None:
    activity_log.appendleft(
        {
            "id": secrets.token_hex(8),
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "message": message,
            "meta": meta or {},
        }
    )
    meta_text = ""
    if meta:
        try:
            meta_text = f" | {json.dumps(meta, sort_keys=True, ensure_ascii=False)}"
        except Exception:
            meta_text = f" | {meta!r}"
    append_backend_log(f"[activity:{kind}] {message}{meta_text}")


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
    SETTINGS_FILE.write_text(
        json.dumps(settings.model_dump(), indent=2),
        encoding="utf-8",
    )
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
    APP_CONFIG_FILE.write_text(
        json.dumps(config.model_dump(), indent=2),
        encoding="utf-8",
    )
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


class ClientSettings(BaseModel):
    display_name: str = Field(default="", max_length=40)
    chunk_mb: int = Field(default=0, ge=0, le=256)
    threads: int = Field(default=0, ge=0, le=16)
    refresh_sec: int = Field(default=5, ge=2, le=60)
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
    parts_dir: Path
    received: Set[int] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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


def _write_chunk_part_sync(dest: Path, upload_file) -> int:
    with dest.open("wb") as out:
        shutil.copyfileobj(upload_file, out, length=COPY_BUFFER_BYTES)
    return dest.stat().st_size


def _merge_pending_parts_sync(parts_dir: Path, total_chunks: int, out_path: Path) -> None:
    with out_path.open("wb") as out:
        for i in range(total_chunks):
            part = parts_dir / f"{i:06d}"
            with part.open("rb") as inp:
                shutil.copyfileobj(inp, out, length=COPY_BUFFER_BYTES)
            part.unlink()
    parts_dir.rmdir()


async def _finalize_chunked_upload(pending: PendingChunkedUpload) -> ShareSession:
    safe_name = Path(pending.filename).name or "file.bin"
    final_path = INBOX_DIR / f"{pending.file_token}_{safe_name}"
    await asyncio.to_thread(
        _merge_pending_parts_sync, pending.parts_dir, pending.total_chunks, final_path
    )
    size = await asyncio.to_thread(lambda: final_path.stat().st_size)
    if size != pending.total_size:
        await asyncio.to_thread(final_path.unlink, missing_ok=True)
        log_backend_event(
            "WARN",
            "transfer",
            "Merged chunked upload size mismatch.",
            meta={
                "sharer_id": pending.sharer_id,
                "filename": safe_name,
                "expected_size": pending.total_size,
                "actual_size": size,
            },
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Merged file size mismatch: expected {pending.total_size} bytes, "
                f"got {size}."
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


def _iter_local_file_chunks(path: str, chunk_size: int) -> Iterator[bytes]:
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            # Never yield b"" — some asyncio/uvicorn paths assert on empty writes (Py3.13+).
            if chunk:
                yield chunk


def _iter_smb_file_chunks(uri: str, chunk_size: int) -> Iterator[bytes]:
    f = smbclient.open_file(uri, mode="rb")
    try:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            if chunk:
                yield chunk
    finally:
        f.close()


def file_byte_iterator(storage_uri: str, chunk_size: int) -> Iterator[bytes]:
    """Stream file in chunks (never loads whole file into RAM)."""
    if isinstance(storage, SMBStorage):
        yield from _iter_smb_file_chunks(storage_uri, chunk_size)
    else:
        yield from _iter_local_file_chunks(storage_uri, chunk_size)


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


def _get_active_session(sharer_id: str, passcode: Optional[str]) -> ShareSession:
    session = share_sessions.get(sharer_id)
    if not session or not session.active:
        raise HTTPException(status_code=404, detail="Share session not found.")
    if session.passcode and passcode != session.passcode:
        raise HTTPException(status_code=403, detail="Invalid passcode.")
    return session


def _is_share_session_stale(
    session: ShareSession,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now(timezone.utc)
    return (current - session.last_seen_at).total_seconds() > SHARE_SESSION_STALE_SECONDS


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
    message = (
        f'{session.display_name} stopped sharing "{session.filename}"'
        if reason == "manual"
        else f'{session.display_name} is no longer sharing "{session.filename}"'
    )
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


def _build_receive_response(session: ShareSession):
    if not isinstance(storage, SMBStorage):
        path = _resolved_path_under_inbox(session.storage_uri)
        return FileResponse(
            path=path,
            filename=session.filename,
            media_type=session.content_type or "application/octet-stream",
        )

    headers = {
        "Content-Disposition": f'attachment; filename="{session.filename}"',
    }
    if session.size_bytes == 0:
        headers["Content-Length"] = "0"
        return Response(
            content=b"",
            media_type=session.content_type or "application/octet-stream",
            headers=headers,
        )
    if session.size_bytes > 0:
        headers["Content-Length"] = str(session.size_bytes)

    return StreamingResponse(
        file_byte_iterator(session.storage_uri, STREAM_CHUNK_BYTES),
        media_type=session.content_type or "application/octet-stream",
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


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


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


@app.get("/api/shares", response_model=List[SharingUser])
async def list_shares():
    await _prune_stale_share_sessions()
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
async def list_inbox():
    """Files stored under the server `inbox/` folder (local storage mode uploads)."""
    return await asyncio.to_thread(_list_inbox_files_sync)


@app.get("/api/activity", response_model=List[ActivityEntry])
async def list_activity():
    """Recent server-side transfer events (in-memory; resets on restart)."""
    return list(activity_log)


@app.get("/api/settings", response_model=ServerSettings)
async def get_server_settings():
    """Non-secret server configuration for the settings UI."""
    return ServerSettings(
        storage_mode=os.getenv("FILES_STORAGE_MODE", "local").lower().strip(),
        copy_buffer_bytes=COPY_BUFFER_BYTES,
        stream_chunk_bytes=STREAM_CHUNK_BYTES,
        inbox_path=str(INBOX_DIR.resolve()),
        app_data_path=str(APP_DATA_DIR.resolve()),
        smb_active=isinstance(storage, SMBStorage),
    )


@app.get("/api/settings/client", response_model=ClientSettings)
async def get_client_settings():
    return await asyncio.to_thread(_load_client_settings_sync)


@app.put("/api/settings/client", response_model=ClientSettings)
async def update_client_settings(settings: ClientSettings):
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
async def clear_app_data():
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
async def network_info():
    port = runtime_current_port or int(os.getenv("APP_PORT", "8000"))
    ip = runtime_host_ip or await asyncio.to_thread(_detect_local_ip)
    scheme = os.getenv("APP_SCHEME", "http").lower().strip() or "http"
    return NetworkInfo(
        ip=ip,
        port=port,
        endpoint=f"{ip}:{port}",
        url=f"{scheme}://{ip}:{port}/",
    )


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
    parts_dir = INBOX_DIR / f".pending_{file_token}"
    parts_dir.mkdir(parents=True, exist_ok=True)
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
        parts_dir=parts_dir,
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
    return {"sharer_id": sharer_id, "total_chunks": total_chunks}


@app.post("/api/share/{sharer_id}/finalize")
async def share_finalize(sharer_id: str):
    """Client calls this after all chunks are uploaded to trigger the merge.
    Returns the completed ShareSession."""
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
    return {"sharer_id": session.sharer_id, "filename": session.filename}


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
    async with pending.lock:
        part_path = pending.parts_dir / f"{chunk_index:06d}"
        try:
            size = await asyncio.to_thread(_write_chunk_part_sync, part_path, file.file)
        finally:
            await file.close()
        if size != expected:
            if part_path.exists():
                part_path.unlink(missing_ok=True)
            pending.received.discard(chunk_index)
            log_backend_event(
                "WARN",
                "transfer",
                "Chunk upload rejected because its size did not match expectation.",
                meta={
                    "sharer_id": sharer_id,
                    "chunk_index": chunk_index,
                    "expected_size": expected,
                    "actual_size": size,
                },
            )
            raise HTTPException(
                status_code=400,
                detail=f"Chunk size mismatch: expected {expected} bytes, got {size}.",
            )
        pending.received.add(chunk_index)

    return {"ok": True, "chunk_index": chunk_index, "complete": False}


@app.get("/api/receive/{sharer_id}/info", response_model=ReceiveInfo)
async def receive_info(
    sharer_id: str,
    passcode: Optional[str] = Query(default=None),
):
    await _prune_stale_share_sessions()
    session = _get_active_session(sharer_id, passcode)
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
    passcode: Optional[str] = Query(default=None),
):
    await _prune_stale_share_sessions()
    session = _get_active_session(sharer_id, passcode)

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
        "Content-Disposition": f'attachment; filename="{session.filename}.part{chunk_index}"',
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
    return {"sharer_id": sharer_id, "status": "sharing"}


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
async def receive_file(sharer_id: str, passcode: Optional[str] = Form(default=None)):
    await _prune_stale_share_sessions()
    session = _get_active_session(sharer_id, passcode)
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

    return _build_receive_response(session)


@app.get("/api/receive/{sharer_id}/download")
async def receive_file_download(
    sharer_id: str,
    passcode: Optional[str] = Query(default=None),
):
    await _prune_stale_share_sessions()
    session = _get_active_session(sharer_id, passcode)
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
    return _build_receive_response(session)


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
    if session.passcode and payload.passcode != session.passcode:
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
    return LocalSaveResult(saved_path=str(target_path), size_bytes=size)

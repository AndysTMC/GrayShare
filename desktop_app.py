import argparse
import atexit
import ctypes
import ipaddress
import json
import os
import signal
import shutil
import socket
import ssl
import subprocess
import sys
import traceback
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

import uvicorn

try:
    import webview
except Exception:
    webview = None

try:
    from zeroconf import ServiceInfo, Zeroconf
except Exception:
    ServiceInfo = None
    Zeroconf = None

try:
    import miniupnpc
except Exception:
    miniupnpc = None

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except Exception:
    x509 = None
    rsa = None
    hashes = None
    serialization = None
    NameOID = None


TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


def detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip:
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def is_port_available(port: int) -> bool:
    if port < 1 or port > 65535:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
    return True


def _iter_grayshare_processes() -> list[tuple[int, str]]:
    if os.name != "nt":
        return []
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    entries: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            exe_name = str(entry.szExeFile or "")
            if exe_name.lower() == "grayshare.exe":
                entries.append((int(entry.th32ProcessID), exe_name))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return entries


def terminate_stale_grayshare_processes(log_file: Path | None = None) -> int:
    if os.name != "nt":
        return 0
    current_pid = os.getpid()
    kernel32 = ctypes.windll.kernel32
    terminated = 0
    for pid, _exe_name in _iter_grayshare_processes():
        if pid <= 0 or pid == current_pid:
            continue
        handle = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            if kernel32.TerminateProcess(handle, 0):
                terminated += 1
                if log_file:
                    append_startup_log(log_file, f"Terminated stale GrayShare.exe process {pid} before startup.")
        finally:
            kernel32.CloseHandle(handle)
    return terminated


def build_tls_cert(tmp_dir: Path, host_ip: str):
    if not all([x509, rsa, hashes, serialization, NameOID]):
        return None, None
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "GrayShare"),
            x509.NameAttribute(NameOID.COMMON_NAME, host_ip),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=2))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address(host_ip))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_file = tmp_dir / "tls_key.pem"
    cert_file = tmp_dir / "tls_cert.pem"
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_file, key_file


class PortForwarder:
    def __init__(self, port: int):
        self.port = port
        self.upnp = None
        self.forwarded = False

    def open(self):
        if miniupnpc is None:
            return
        try:
            upnp = miniupnpc.UPnP()
            upnp.discoverdelay = 500
            found = upnp.discover()
            if found <= 0:
                return
            upnp.selectigd()
            local_ip = upnp.lanaddr
            self.forwarded = bool(
                upnp.addportmapping(self.port, "TCP", local_ip, self.port, "GrayShare", "")
            )
            if self.forwarded:
                self.upnp = upnp
        except Exception:
            self.forwarded = False

    def close(self):
        if not self.forwarded or self.upnp is None:
            return
        try:
            self.upnp.deleteportmapping(self.port, "TCP")
        except Exception:
            pass


class MdnsAnnouncer:
    def __init__(self, host_ip: str, port: int):
        self.host_ip = host_ip
        self.port = port
        self.zeroconf = None
        self.service_info = None

    def start(self):
        if Zeroconf is None or ServiceInfo is None:
            return
        try:
            self.zeroconf = Zeroconf()
            instance = f"GrayShare-{self.port}._grayshare._tcp.local."
            self.service_info = ServiceInfo(
                "_grayshare._tcp.local.",
                instance,
                addresses=[socket.inet_aton(self.host_ip)],
                port=self.port,
                properties={"name": "GrayShare"},
                server="grayshare.local.",
            )
            self.zeroconf.register_service(self.service_info)
        except Exception:
            self.stop()

    def stop(self):
        if self.zeroconf and self.service_info:
            try:
                self.zeroconf.unregister_service(self.service_info)
            except Exception:
                pass
        if self.zeroconf:
            try:
                self.zeroconf.close()
            except Exception:
                pass
        self.zeroconf = None
        self.service_info = None


class DesktopBridge:
    def __init__(self):
        self.window = None
        self._launch_lock = threading.Lock()
        self._launch_state = {
            "status": "loading",
            "url": "",
            "message": "",
        }

    def bind_window(self, window) -> None:
        self.window = window

    def set_launch_ready(self, url: str) -> None:
        with self._launch_lock:
            self._launch_state = {
                "status": "ready",
                "url": str(url or ""),
                "message": "",
            }

    def set_launch_error(self, message: str) -> None:
        with self._launch_lock:
            self._launch_state = {
                "status": "error",
                "url": "",
                "message": str(message or "GrayShare could not start."),
            }

    def get_launch_state(self) -> dict:
        with self._launch_lock:
            return dict(self._launch_state)

    def choose_save_path(self, suggested_name: str = "") -> str:
        if self.window is None:
            return ""
        dialog_enum = getattr(getattr(webview, "FileDialog", None), "SAVE", None)
        if dialog_enum is None:
            dialog_enum = getattr(webview, "SAVE_DIALOG", None)
        if dialog_enum is None:
            return ""
        result = self.window.create_file_dialog(
            dialog_type=dialog_enum,
            save_filename=suggested_name or "download.bin",
        )
        if not result:
            return ""
        if isinstance(result, (list, tuple)):
            return str(result[0] or "")
        return str(result or "")


class EmbeddedServer:
    def __init__(
        self,
        host: str,
        port: int,
        cert_file: Path | None,
        key_file: Path | None,
        log_file: Path | None = None,
    ):
        self.host = host
        self.port = port
        self.cert_file = cert_file
        self.key_file = key_file
        self.log_file = log_file
        self.process = None
        self.startup_exception = None
        self.startup_traceback = ""

    def start(self):
        env = os.environ.copy()
        env["APP_PORT"] = str(self.port)
        env["APP_SCHEME"] = "https" if self.cert_file and self.key_file else "http"
        if self.log_file:
            env["GRAYSHARE_STARTUP_LOG"] = str(self.log_file)
        cmd = [
            sys.executable,
            "--server-only",
            "--port",
            str(self.port),
            "--server-host",
            str(self.host),
        ]
        if self.cert_file:
            cmd.extend(["--tls-cert", str(self.cert_file)])
        if self.key_file:
            cmd.extend(["--tls-key", str(self.key_file)])
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            cmd,
            env=env,
            creationflags=creationflags,
        )
        if self.log_file and self.process is not None:
            append_startup_log(
                self.log_file,
                f"Embedded server subprocess started with PID {self.process.pid} on port {self.port}.",
            )

    def stop(self):
        if self.process is None:
            return
        if self.process.poll() is not None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=6)
        except Exception:
            try:
                self.process.kill()
                self.process.wait(timeout=3)
            except Exception:
                pass


def append_startup_log(log_file: Path, message: str, *, level: str = "INFO") -> None:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        normalized_level = str(level or "INFO").upper().strip()
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] [{normalized_level}] {message}\n")
    except Exception:
        pass


def reset_startup_log(log_file: Path) -> None:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with log_file.open("w", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] GrayShare launch log reset.\n")
    except Exception:
        pass


def reset_runtime_log(log_file: Path, label: str) -> None:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with log_file.open("w", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] {label} log reset.\n")
    except Exception:
        pass


def build_splash_html(title: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0f0f10;
      --fg: #f3f3f1;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      overflow: hidden;
      background: var(--bg);
      color: var(--fg);
      font-family: "Georgia", "Times New Roman", serif;
    }}
    h1 {{
      margin: 0;
      padding: 0 24px;
      text-align: center;
      font-size: clamp(28px, 4.3vw, 38px);
      font-weight: 500;
      line-height: 1.05;
      letter-spacing: 0.02em;
      animation: pulse 2.8s ease-in-out infinite;
      text-rendering: optimizeLegibility;
    }}
    .loader {{
      width: 92px;
      height: 3px;
      margin: 18px auto 0;
      overflow: hidden;
      border-radius: 999px;
      background: rgba(255,255,255,0.14);
      position: relative;
    }}
    .loader::before {{
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 38%;
      border-radius: inherit;
      background: linear-gradient(90deg, rgba(255,255,255,0.18), rgba(255,255,255,0.95), rgba(255,255,255,0.18));
      animation: glide 1.8s ease-in-out infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{
        opacity: 0.62;
      }}
      50% {{
        opacity: 1;
      }}
    }}
    @keyframes glide {{
      0% {{
        transform: translateX(-120%);
      }}
      100% {{
        transform: translateX(310%);
      }}
    }}
    .error {{
      display: none;
      margin-top: 16px;
      padding: 0 28px;
      text-align: center;
      font: 500 14px/1.55 "Segoe UI", system-ui, sans-serif;
      color: rgba(255,255,255,0.72);
    }}
    body.error .loader {{
      display: none;
    }}
    body.error .error {{
      display: block;
    }}
  </style>
</head>
<body>
  <div>
    <h1 id="splash-title">Starting GrayShare</h1>
    <div class="loader" aria-hidden="true"></div>
    <div id="splash-error" class="error"></div>
  </div>
  <script>
    const titleEl = document.getElementById("splash-title");
    const errorEl = document.getElementById("splash-error");

    async function pollLaunchState() {{
      try {{
        const api = window.pywebview?.api;
        if (!api?.get_launch_state) {{
          setTimeout(pollLaunchState, 250);
          return;
        }}
        const state = await api.get_launch_state();
        if (state?.status === "ready" && state.url) {{
          window.location.replace(state.url);
          return;
        }}
        if (state?.status === "error") {{
          document.body.classList.add("error");
          titleEl.textContent = "GrayShare failed to start";
          errorEl.textContent = state.message || "See startup.log for details.";
          return;
        }}
      }} catch {{
      }}
      setTimeout(pollLaunchState, 350);
    }}

    window.addEventListener("pywebviewready", () => {{
      setTimeout(pollLaunchState, 120);
    }});
    setTimeout(pollLaunchState, 350);
  </script>
</body>
</html>"""


def build_error_html(title: str, message: str, details: str) -> str:
    safe_message = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_details = details.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #f5f2f2;
      color: #221b1b;
      font-family: "Segoe UI", system-ui, sans-serif;
    }}
    .shell {{
      width: min(640px, calc(100vw - 40px));
      padding: 28px 26px;
      border-radius: 18px;
      border: 1px solid #e2c7c7;
      background: #fffdfd;
      box-shadow: 0 18px 36px rgba(0,0,0,0.08);
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 28px;
    }}
    p {{
      margin: 0 0 14px;
      line-height: 1.55;
      color: #5f4d4d;
    }}
    pre {{
      margin: 0;
      max-height: 260px;
      overflow: auto;
      padding: 14px;
      border-radius: 12px;
      background: #f7eeee;
      border: 1px solid #ecd6d6;
      white-space: pre-wrap;
      word-break: break-word;
      font: 13px/1.45 Consolas, monospace;
    }}
  </style>
</head>
<body>
  <div class="shell">
    <h1>{title}</h1>
    <p>{safe_message}</p>
    <pre>{safe_details}</pre>
  </div>
</body>
</html>"""


def wait_until_healthy(
    base_url: str,
    server: EmbeddedServer | None = None,
    timeout_sec: float = 12.0,
) -> bool:
    deadline = time.time() + timeout_sec
    context = None
    if base_url.startswith("https://"):
        context = ssl._create_unverified_context()
    while time.time() < deadline:
        if server and server.startup_exception:
            return False
        if server and server.process and server.process.poll() is not None:
            return False
        try:
            with urlopen(f"{base_url}/api/health", timeout=1.5, context=context) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def describe_server_failure(server: EmbeddedServer | None) -> str:
    if not server:
        return "Embedded server did not initialize."
    if server.startup_exception:
        return str(server.startup_exception) or server.startup_exception.__class__.__name__
    if server.process and server.process.poll() is not None:
        return f"Embedded server process exited with code {server.process.returncode}."
    return "Embedded server did not become healthy before timeout."


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--width", type=int, default=700)
    p.add_argument("--height", type=int, default=700)
    p.add_argument("--title", type=str, default="GrayShare")
    p.add_argument("--tls", action="store_true")
    p.add_argument("--no-tls", action="store_true")
    p.add_argument("--server-only", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--server-host", type=str, default="0.0.0.0", help=argparse.SUPPRESS)
    p.add_argument("--tls-cert", type=str, default="", help=argparse.SUPPRESS)
    p.add_argument("--tls-key", type=str, default="", help=argparse.SUPPRESS)
    return p.parse_args()


def run_server_only(args) -> int:
    log_path = os.getenv("GRAYSHARE_STARTUP_LOG", "").strip()
    log_file = Path(log_path) if log_path else None
    data_dir = Path(os.getenv("APP_DATA_DIR", str(Path.home() / ".grayshare")))
    backend_log_file = data_dir / "backend.log"
    cert_file = args.tls_cert or None
    key_file = args.tls_key or None
    try:
        append_startup_log(
            backend_log_file,
            f"Backend process booting on {args.server_host or '0.0.0.0'}:{int(args.port or 0)}.",
        )
        from main import app as fastapi_app

        config = uvicorn.Config(
            fastapi_app,
            host=args.server_host or "0.0.0.0",
            port=int(args.port or 0),
            reload=False,
            log_level="warning",
            access_log=False,
            lifespan="off",
            ssl_certfile=cert_file,
            ssl_keyfile=key_file,
        )
        server = uvicorn.Server(config)
        append_startup_log(backend_log_file, "Backend process entering uvicorn event loop.")
        server.run()
        append_startup_log(backend_log_file, "Backend process exited uvicorn event loop.")
        return 0
    except Exception:
        append_startup_log(
            backend_log_file,
            "Backend process failed before startup completed:\n"
            f"{traceback.format_exc().rstrip()}",
            level="ERROR",
        )
        if log_file:
            append_startup_log(
                log_file,
                "Embedded server subprocess failed:\n"
                f"{traceback.format_exc().rstrip()}",
                level="ERROR",
            )
        raise


def app_data_dir() -> Path:
    base = Path(os.getenv("USERPROFILE", str(Path.home())))
    path = base / ".grayshare"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_app_config(raw: dict | None = None) -> dict:
    port = 4567
    if raw:
        try:
            port = int(raw.get("port", 4567))
        except Exception:
            port = 4567
    if port < 1 or port > 65535:
        port = 4567
    return {"port": port}


def load_app_config(data_dir: Path) -> dict:
    config_path = data_dir / "app_config.json"
    if not config_path.is_file():
        return _normalize_app_config()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    config = _normalize_app_config(raw)
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


class DesktopController:
    def __init__(self, args, log_file: Path):
        self.args = args
        self.log_file = log_file
        self.window = None
        self._cleanup_callback = None
        self._closing = False
        self._cleanup_complete = threading.Event()
        self._lock = threading.Lock()

    def bind_window(self, window) -> None:
        self.window = window

    def bind_cleanup(self, callback) -> None:
        self._cleanup_callback = callback

    def request_close(self, reason: str = "close requested") -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
        append_startup_log(self.log_file, f"Graceful shutdown requested: {reason}.")
        threading.Thread(
            target=self._close_current_app,
            args=(reason,),
            daemon=True,
            name="GrayShareClose",
        ).start()

    def on_window_closing(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
        append_startup_log(self.log_file, "Window close detected. Starting graceful shutdown.")
        self._run_cleanup()

    def _run_cleanup(self) -> None:
        if self._cleanup_complete.is_set():
            return
        if self._cleanup_callback is not None:
            try:
                self._cleanup_callback()
            except Exception:
                append_startup_log(
                    self.log_file,
                    "Graceful shutdown cleanup failed:\n"
                    f"{traceback.format_exc().rstrip()}",
                    level="ERROR",
                )
        self._cleanup_complete.set()

    def _close_current_app(self, reason: str) -> None:
        def _force_exit_later() -> None:
            time.sleep(8)
            os._exit(0)

        threading.Thread(
            target=_force_exit_later,
            daemon=True,
            name="GrayShareCloseWatchdog",
        ).start()
        self._run_cleanup()
        if self.window is not None:
            try:
                self.window.destroy()
                return
            except Exception:
                append_startup_log(
                    self.log_file,
                    f"Window destroy during {reason} failed:\n"
                    f"{traceback.format_exc().rstrip()}",
                    level="ERROR",
                )
        os._exit(0)


def purge_deferred_data(data_dir: Path) -> None:
    marker = data_dir / ".clear_webview"
    if not marker.exists():
        return
    marker.unlink(missing_ok=True)


def purge_webview_service_workers(data_dir: Path, log_file: Path | None = None) -> None:
    webview_root = data_dir / "webview" / "EBWebView" / "Default"
    cleanup_targets = [
        webview_root / "Service Worker",
        webview_root / "Code Cache",
    ]
    for target in cleanup_targets:
        if not target.exists():
            continue
        try:
            shutil.rmtree(target, ignore_errors=False)
            if log_file:
                append_startup_log(log_file, f"Cleared stale webview cache: {target.name}")
        except Exception:
            if log_file:
                append_startup_log(
                    log_file,
                    "Unable to clear webview cache:\n"
                    f"{traceback.format_exc().rstrip()}",
                    level="WARN",
                )


def main():
    args = parse_args()
    if args.server_only:
        raise SystemExit(run_server_only(args))
    if webview is None:
        raise RuntimeError("pywebview is required. Install dependencies and run again.")

    host_ip = detect_local_ip()
    data_dir = app_data_dir()
    app_config = load_app_config(data_dir)
    configured_port = app_config.get("port", 0)
    purge_deferred_data(data_dir)
    os.environ["APP_DATA_DIR"] = str(data_dir)
    os.environ["APP_HOST_IP"] = host_ip
    log_file = data_dir / "startup.log"
    backend_log_file = data_dir / "backend.log"
    reset_runtime_log(log_file, "GrayShare desktop")
    reset_runtime_log(backend_log_file, "GrayShare backend")
    append_startup_log(log_file, "Launching GrayShare desktop app.")
    append_startup_log(log_file, f"Desktop log file: {log_file}")
    append_startup_log(log_file, f"Backend log file: {backend_log_file}")
    terminated_count = terminate_stale_grayshare_processes(log_file=log_file)
    if terminated_count:
        time.sleep(0.6)
    purge_webview_service_workers(data_dir, log_file=log_file)
    controller = DesktopController(args, log_file)
    temp_dir = Path(tempfile.mkdtemp(prefix="grayshare_"))
    bind_host = "0.0.0.0"
    cert_file = None
    key_file = None
    server = None
    ui_url = ""
    explicit_port = int(args.port or 0)
    port = explicit_port if explicit_port > 0 else configured_port
    mdns = None
    upnp = None
    cleanup_lock = threading.Lock()
    cleanup_done = False

    def cleanup_server_artifacts(
        current_server: EmbeddedServer | None,
        current_cert: Path | None,
        current_key: Path | None,
    ) -> None:
        if current_server:
            current_server.stop()
        if current_cert and current_cert.exists():
            current_cert.unlink(missing_ok=True)
        if current_key and current_key.exists():
            current_key.unlink(missing_ok=True)

    def start_embedded(
        use_port: int,
        prefer_tls: bool,
    ) -> tuple[EmbeddedServer, str, Path | None, Path | None]:
        local_cert = None
        local_key = None
        if prefer_tls:
            try:
                local_cert, local_key = build_tls_cert(temp_dir, host_ip)
            except Exception:
                local_cert, local_key = None, None
        protocol = "https" if local_cert and local_key else "http"
        local_url = f"{protocol}://127.0.0.1:{use_port}"
        srv = EmbeddedServer(bind_host, use_port, local_cert, local_key, log_file=log_file)
        srv.start()
        return srv, local_url, local_cert, local_key

    def cleanup():
        nonlocal cleanup_done
        with cleanup_lock:
            if cleanup_done:
                return
            cleanup_done = True
        if mdns:
            mdns.stop()
        if upnp:
            upnp.close()
        if server:
            server.stop()
        if cert_file and cert_file.exists():
            cert_file.unlink(missing_ok=True)
        if key_file and key_file.exists():
            key_file.unlink(missing_ok=True)
        if temp_dir.exists():
            for f in temp_dir.glob("*"):
                f.unlink(missing_ok=True)
            temp_dir.rmdir()

    try:
        bridge = DesktopBridge()
        splash_html = build_splash_html(args.title)
        window = webview.create_window(
            args.title,
            html=splash_html,
            width=args.width,
            height=args.height,
            js_api=bridge,
        )
        bridge.bind_window(window)
        controller.bind_window(window)
        controller.bind_cleanup(cleanup)
        window.events.closing += controller.on_window_closing

        def handle_signal(signum, _frame) -> None:
            controller.request_close(f"signal {signum}")

        for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handle_signal)
            except Exception:
                pass

        def start_network_services(active_port: int) -> None:
            nonlocal mdns
            nonlocal upnp
            local_mdns = MdnsAnnouncer(host_ip, active_port)
            local_upnp = PortForwarder(active_port)
            try:
                local_mdns.start()
                local_upnp.open()
                mdns = local_mdns
                upnp = local_upnp
            except Exception:
                append_startup_log(
                    log_file,
                    "LAN advertisement startup failed:\n"
                    f"{traceback.format_exc().rstrip()}",
                    level="WARN",
                )
                try:
                    local_mdns.stop()
                except Exception:
                    pass
                try:
                    local_upnp.close()
                except Exception:
                    pass

        def bootstrap_window(target_window) -> None:
            nonlocal server
            nonlocal ui_url
            nonlocal cert_file
            nonlocal key_file
            nonlocal port
            nonlocal mdns
            nonlocal upnp

            max_attempts = 4
            last_error = "Unknown startup failure."
            ok = False
            prefer_tls = args.tls and not args.no_tls

            for attempt in range(1, max_attempts + 1):
                if port <= 0:
                    port = find_free_port()
                elif not is_port_available(port):
                    requested_port = port
                    port = find_free_port()
                    if requested_port == configured_port and configured_port > 0:
                        append_startup_log(
                            log_file,
                            f"Configured port {requested_port} is unavailable. Using temporary port {port} for this launch.",
                            level="WARN",
                        )
                    elif requested_port == explicit_port and explicit_port > 0:
                        append_startup_log(
                            log_file,
                            f"Requested port {requested_port} is unavailable. Using temporary port {port} for this launch.",
                            level="WARN",
                        )
                    else:
                        append_startup_log(
                            log_file,
                            f"Port {requested_port} is unavailable. Using temporary port {port} for this launch.",
                            level="WARN",
                        )
                append_startup_log(
                    log_file,
                    f"Starting embedded server on port {port} (attempt {attempt}/{max_attempts}).",
                )
                server, ui_url, cert_file, key_file = start_embedded(port, prefer_tls=prefer_tls)
                ok = wait_until_healthy(ui_url, server)
                if not ok and ui_url.startswith("https://"):
                    append_startup_log(log_file, "HTTPS startup failed, retrying without TLS.", level="WARN")
                    cleanup_server_artifacts(server, cert_file, key_file)
                    server = None
                    cert_file = None
                    key_file = None
                    server, ui_url, cert_file, key_file = start_embedded(port, prefer_tls=False)
                    ok = wait_until_healthy(ui_url, server)
                if ok:
                    os.environ["APP_SCHEME"] = "https" if ui_url.startswith("https://") else "http"
                    break
                last_error = describe_server_failure(server)
                append_startup_log(log_file, f"Embedded server failed on port {port}: {last_error}", level="ERROR")
                cleanup_server_artifacts(server, cert_file, key_file)
                server = None
                cert_file = None
                key_file = None
                port = 0

            if not ok:
                append_startup_log(
                    log_file,
                    "Embedded server never became healthy. Showing startup failure screen.",
                    level="ERROR",
                )
                bridge.set_launch_error(
                    f"{last_error} See {log_file} for details."
                )
                return

            append_startup_log(log_file, f"Embedded server is healthy at {ui_url}.")
            append_startup_log(log_file, f"Splash screen is handing off to {ui_url}.")
            bridge.set_launch_ready(ui_url)
            threading.Thread(
                target=start_network_services,
                args=(port,),
                daemon=True,
                name="GrayShareNetworkServices",
            ).start()

        def start_bootstrap(target_window) -> None:
            threading.Thread(
                target=bootstrap_window,
                args=(target_window,),
                daemon=True,
                name="GrayShareBootstrap",
            ).start()

        atexit.register(cleanup)
        append_startup_log(log_file, "Opening splash window while GrayShare boots.")
        webview.start(
            start_bootstrap,
            (window,),
            debug=False,
            private_mode=False,
            storage_path=str(data_dir / "webview"),
        )
    except Exception as exc:
        append_startup_log(
            log_file,
            "Desktop window failed to open:\n"
            f"{traceback.format_exc().rstrip()}",
            level="ERROR",
        )
        raise RuntimeError(
            "Desktop window failed to open. "
            f"See {log_file} for details."
        ) from exc
    finally:
        cleanup()


if __name__ == "__main__":
    main()

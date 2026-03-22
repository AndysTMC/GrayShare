import argparse
import atexit
import ipaddress
import os
import shutil
import socket
import ssl
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

    def bind_window(self, window) -> None:
        self.window = window

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
        self.server = None
        self.thread = None
        self.startup_exception = None
        self.startup_traceback = ""

    def start(self):
        os.environ["APP_PORT"] = str(self.port)
        self.thread = threading.Thread(target=self._run, daemon=True, name="EmbeddedServer")
        self.thread.start()

    def _run(self):
        try:
            from main import app as fastapi_app

            config = uvicorn.Config(
                fastapi_app,
                host=self.host,
                port=self.port,
                reload=False,
                log_level="warning",
                access_log=False,
                lifespan="off",
                ssl_certfile=str(self.cert_file) if self.cert_file else None,
                ssl_keyfile=str(self.key_file) if self.key_file else None,
            )
            self.server = uvicorn.Server(config)
            self.server.run()
        except Exception as exc:
            self.startup_exception = exc
            self.startup_traceback = traceback.format_exc()
            if self.log_file:
                append_startup_log(
                    self.log_file,
                    "Embedded server startup failed:\n"
                    f"{self.startup_traceback.rstrip()}",
                )

    def stop(self):
        if self.server:
            self.server.should_exit = True
            self.server.force_exit = True
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)


def append_startup_log(log_file: Path, message: str) -> None:
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


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
        if server and server.thread and not server.thread.is_alive() and server.server is None:
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
    if server.thread and not server.thread.is_alive():
        return "Embedded server thread exited before reporting healthy."
    return "Embedded server did not become healthy before timeout."


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--width", type=int, default=1120)
    p.add_argument("--height", type=int, default=760)
    p.add_argument("--title", type=str, default="GrayShare")
    p.add_argument("--tls", action="store_true")
    p.add_argument("--no-tls", action="store_true")
    return p.parse_args()


def app_data_dir() -> Path:
    base = Path(os.getenv("USERPROFILE", str(Path.home())))
    path = base / ".grayshare"
    path.mkdir(parents=True, exist_ok=True)
    return path


def purge_deferred_data(data_dir: Path) -> None:
    marker = data_dir / ".clear_webview"
    if not marker.exists():
        return
    marker.unlink(missing_ok=True)


def main():
    args = parse_args()
    if webview is None:
        raise RuntimeError("pywebview is required. Install dependencies and run again.")

    host_ip = detect_local_ip()
    data_dir = app_data_dir()
    purge_deferred_data(data_dir)
    os.environ["APP_DATA_DIR"] = str(data_dir)
    os.environ["APP_HOST_IP"] = host_ip
    log_file = data_dir / "startup.log"
    append_startup_log(log_file, "Launching GrayShare desktop app.")
    temp_dir = Path(tempfile.mkdtemp(prefix="grayshare_"))
    bind_host = "0.0.0.0"
    cert_file = None
    key_file = None
    server = None
    ui_url = ""
    port = args.port if args.port > 0 else 0
    mdns = None
    upnp = None

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

    atexit.register(cleanup)
    max_attempts = 1 if args.port > 0 else 4
    last_error = "Unknown startup failure."
    ok = False
    prefer_tls = args.tls and not args.no_tls
    for attempt in range(1, max_attempts + 1):
        if port <= 0:
            port = find_free_port()
        append_startup_log(
            log_file,
            f"Starting embedded server on port {port} (attempt {attempt}/{max_attempts}).",
        )
        server, ui_url, cert_file, key_file = start_embedded(port, prefer_tls=prefer_tls)
        ok = wait_until_healthy(ui_url, server)
        if not ok and ui_url.startswith("https://"):
            append_startup_log(log_file, "HTTPS startup failed, retrying without TLS.")
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
        append_startup_log(log_file, f"Embedded server failed on port {port}: {last_error}")
        cleanup_server_artifacts(server, cert_file, key_file)
        server = None
        cert_file = None
        key_file = None
        port = 0
    if not ok:
        cleanup()
        raise RuntimeError(
            "Local server failed to start. "
            f"{last_error} See {log_file} for details."
        )

    append_startup_log(log_file, f"Embedded server is healthy at {ui_url}.")
    mdns = MdnsAnnouncer(host_ip, port)
    upnp = PortForwarder(port)
    mdns.start()
    upnp.open()

    try:
        append_startup_log(log_file, f"Opening desktop window at {ui_url}.")
        bridge = DesktopBridge()
        window = webview.create_window(
            args.title,
            ui_url,
            width=args.width,
            height=args.height,
            js_api=bridge,
        )
        bridge.bind_window(window)
        webview.start(
            debug=False,
            private_mode=True,
            storage_path=str(data_dir / "webview"),
        )
    except Exception as exc:
        append_startup_log(
            log_file,
            "Desktop window failed to open:\n"
            f"{traceback.format_exc().rstrip()}",
        )
        raise RuntimeError(
            "Desktop window failed to open. "
            f"See {log_file} for details."
        ) from exc
    finally:
        cleanup()


if __name__ == "__main__":
    main()

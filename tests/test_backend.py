"""GrayShare backend unit tests: Range parser, chunk lifecycle, disposition, passcodes.

Run with: .venv/bin/python -m pytest tests/test_backend.py -v
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Isolate app data before importing main.
_tmp = None


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    global _tmp
    _tmp = tmp_path_factory.mktemp("grayshare_ut")
    os.environ["APP_DATA_DIR"] = str(_tmp)
    import main
    return main


# ---------------------------------------------------------------------------
# Range header parsing
# ---------------------------------------------------------------------------

class FakeSession:
    """Duck-typed stand-in so _parse_byte_range tests need no real session."""

    def __init__(self):
        pass


def test_range_full_file(app_module):
    assert app_module._parse_byte_range(None, 100) is None
    assert app_module._parse_byte_range("", 100) is None


def test_range_simple(app_module):
    assert app_module._parse_byte_range("bytes=0-49", 100) == (0, 49)
    assert app_module._parse_byte_range("bytes=10-19", 100) == (10, 19)


def test_range_open_ended(app_module):
    assert app_module._parse_byte_range("bytes=50-", 100) == (50, 99)


def test_range_suffix(app_module):
    assert app_module._parse_byte_range("bytes=-10", 100) == (90, 99)
    # Suffix longer than file -> whole file.
    assert app_module._parse_byte_range("bytes=-500", 100) == (0, 99)


def test_range_end_clamped(app_module):
    assert app_module._parse_byte_range("bytes=90-200", 100) == (90, 99)


def test_range_invalid_raises_416(app_module):
    for bad in ("bytes=abc", "bytes=5-2", "bytes=100-", "bytes=0-1,5-9", "chunks=0-1", "bytes=-0"):
        with pytest.raises(HTTPException) as exc:
            app_module._parse_byte_range(bad, 100)
        assert exc.value.status_code == 416, bad


def test_range_empty_file_raises_416(app_module):
    with pytest.raises(HTTPException) as exc:
        app_module._parse_byte_range("bytes=0-", 0)
    assert exc.value.status_code == 416


# ---------------------------------------------------------------------------
# Content-Disposition encoding
# ---------------------------------------------------------------------------

def test_disposition_ascii(app_module):
    value = app_module._content_disposition("report.pdf")
    assert value == 'attachment; filename="report.pdf"'


def test_disposition_quotes_sanitized(app_module):
    value = app_module._content_disposition('weird"file"name.txt')
    assert '"' not in value.replace('attachment; filename="', "", 1).rstrip('"')
    assert "weird" in value and "name.txt" in value


def test_disposition_newlines_stripped(app_module):
    value = app_module._content_disposition("evil\r\nX-Injected: 1.txt")
    assert "\r" not in value and "\n" not in value


def test_disposition_unicode_rfc5987(app_module):
    value = app_module._content_disposition("résumé.txt")
    assert "filename*=UTF-8''" in value
    assert "r%C3%A9sum%C3%A9.txt" in value


def test_disposition_empty_falls_back(app_module):
    value = app_module._content_disposition("")
    assert "file.bin" in value or "file" in value


# ---------------------------------------------------------------------------
# Passcode matching
# ---------------------------------------------------------------------------

def test_passcode_no_expected(app_module):
    assert app_module._passcode_matches(None, None) is True
    assert app_module._passcode_matches("anything", None) is True


def test_passcode_required(app_module):
    assert app_module._passcode_matches(None, "secret") is False
    assert app_module._passcode_matches("", "secret") is False
    assert app_module._passcode_matches("wrong", "secret") is False
    assert app_module._passcode_matches("secret", "secret") is True
    assert app_module._passcode_matches("  secret  ", "secret") is True  # trimmed


# ---------------------------------------------------------------------------
# Chunk spec + upload lifecycle (uses real temp inbox)
# ---------------------------------------------------------------------------

def _make_pending(main, total_size, chunk_size, name="f.bin"):
    token = main.secrets.token_urlsafe(6)
    target = main.INBOX_DIR / f"ut_{token}_{name}"
    main._preallocate_file_sync(target, total_size)
    pending = main.PendingChunkedUpload(
        sharer_id=main.secrets.token_urlsafe(6),
        display_name="UT",
        filename=name,
        content_type="application/octet-stream",
        passcode=None,
        total_size=total_size,
        chunk_size=chunk_size,
        total_chunks=max(1, (total_size + chunk_size - 1) // chunk_size),
        file_token=token,
        target_path=target,
    )
    main.pending_chunked[pending.sharer_id] = pending
    return pending


def _upload_chunk_sync(main, pending, index, data):
    """Simulate one chunk request's write path synchronously."""
    import io

    offset = index * pending.chunk_size
    written = main._write_chunk_at_offset_sync(
        pending.target_path, offset, io.BytesIO(data), len(data)
    )
    if written != len(data):
        raise AssertionError("written mismatch")
    pending.received.add(index)
    pending.updated_at = main.datetime.now(main.timezone.utc)


def test_chunk_lifecycle_out_of_order(app_module, tmp_path):
    main = app_module
    chunk = 1024
    payload = bytes(range(256)) * 8  # 2048 bytes -> 2 chunks
    pending = _make_pending(main, len(payload), chunk)

    # Upload out of order.
    _upload_chunk_sync(main, pending, 1, payload[chunk:])
    _upload_chunk_sync(main, pending, 0, payload[:chunk])

    session = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        main._finalize_chunked_upload(pending)
    )
    stored = Path(session.storage_uri).read_bytes()
    assert stored == payload
    assert session.size_bytes == len(payload)
    assert session.transfer_chunk_size == chunk
    # Cleanup
    main.share_sessions.pop(session.sharer_id, None)
    Path(session.storage_uri).unlink(missing_ok=True)


def test_concurrent_chunk_writes_lock_free(app_module):
    """True parallel writes into the preallocated file must not corrupt each
    other or require any lock — this is the invariant the offset-write design
    depends on (see AGENTS.md 'Upload Pipeline')."""
    main = app_module
    import io

    chunk = 64 * 1024
    n = 8
    payload = [bytes((i * 7 + j) % 256 for j in range(chunk)) for i in range(n)]

    async def run():
        pending = _make_pending(main, chunk * n, chunk)

        async def upload(index):
            data = payload[index]
            await asyncio.to_thread(
                main._write_chunk_at_offset_sync,
                pending.target_path,
                index * chunk,
                io.BytesIO(data),
                len(data),
            )
            pending.received.add(index)

        # All 8 chunks racing simultaneously.
        await asyncio.gather(*(upload(i) for i in range(n)))
        return pending

    loop = asyncio.new_event_loop()
    pending = None
    try:
        pending = loop.run_until_complete(run())
        stored = pending.target_path.read_bytes()
        assert pending.received == set(range(n))
        assert stored == b"".join(payload)
    finally:
        loop.close()
        if pending is not None:
            pending.target_path.unlink(missing_ok=True)


def test_finalize_rejects_missing_chunks(app_module):
    main = app_module
    pending = _make_pending(main, 4096, 1024)
    _upload_chunk_sync(main, pending, 0, b"x" * 1024)  # 1 of 4

    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(HTTPException) as exc:
            loop.run_until_complete(main._finalize_chunked_upload(pending))
        assert exc.value.status_code == 400
    finally:
        loop.close()
        pending.target_path.unlink(missing_ok=True)


def test_short_chunk_detected_at_upload_time(app_module):
    """With preallocation, file size is always total_size — so integrity is
    enforced per-chunk at upload time, not by finalize. A short chunk must be
    detectable by the caller (the endpoint rejects it and discards the index).
    """
    main = app_module
    import io

    pending = _make_pending(main, 2048, 1024)
    written = main._write_chunk_at_offset_sync(
        pending.target_path, 1024, io.BytesIO(b"b" * 512), 1024
    )
    assert written == 512  # caller sees mismatch vs expected=1024 -> reject
    # Simulate the endpoint's rejection path.
    pending.received.discard(1)
    assert 1 not in pending.received
    pending.target_path.unlink(missing_ok=True)


def test_preallocate_exact_size(app_module, tmp_path):
    main = app_module
    target = tmp_path / "pre.bin"
    main._preallocate_file_sync(target, 12345)
    assert target.stat().st_size == 12345
    main._preallocate_file_sync(target, 0)
    assert target.stat().st_size == 0


def test_history_persists_and_loads(app_module):
    main = app_module
    main.log_activity("share_start", "history persistence test", {"k": 1})
    entries = main._load_history_sync()
    assert any(e["message"] == "history persistence test" for e in entries)


def test_access_key_present(app_module):
    assert isinstance(app_module.ACCESS_KEY, str) and len(app_module.ACCESS_KEY) >= 16


def test_wrong_length_access_key_is_403(app_module):
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    res = client.get("/api/shares", params={"k": "short"})
    assert res.status_code == 403
    res = client.get("/api/shares")
    assert res.status_code == 403
    res = client.get("/api/shares", params={"k": app_module.ACCESS_KEY})
    assert res.status_code == 200


def test_stop_discards_pending_upload(app_module):
    from fastapi.testclient import TestClient

    main = app_module
    pending = _make_pending(main, 4096, 1024)
    assert pending.target_path.is_file()
    client = TestClient(main.app)
    res = client.post(
        f"/api/share/{pending.sharer_id}/stop",
        params={"k": main.ACCESS_KEY},
    )
    assert res.status_code == 200
    assert res.json().get("pending") is True
    assert not pending.target_path.exists()
    assert pending.sharer_id not in main.pending_chunked


def test_clear_data_does_not_delete_unrelated_dirs(app_module):
    main = app_module
    decoy = main.APP_DATA_DIR / "app"
    decoy.mkdir()
    (decoy / "keep.txt").write_text("installer checkout", encoding="utf-8")
    inbox_file = main.INBOX_DIR / "stale.bin"
    inbox_file.write_bytes(b"x")
    deleted, _preserved, skipped = main._clear_app_data_sync()
    assert (decoy / "keep.txt").is_file()
    assert any("app:" in item for item in skipped)
    assert deleted >= 1
    assert not inbox_file.exists()


def test_share_init_lan_requires_access_key(app_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(app_module, "_is_loopback_request", lambda _request: False)
    client = TestClient(app_module.app)
    payload = {
        "display_name": "UT",
        "filename": "a.bin",
        "content_type": "application/octet-stream",
        "total_size": 1024,
        "chunk_size": 256 * 1024,
    }
    res = client.post("/api/share/init", json=payload)
    assert res.status_code == 403
    res = client.post(
        "/api/share/init",
        json=payload,
        params={"k": app_module.ACCESS_KEY},
    )
    assert res.status_code == 200


def test_backend_launch_cmd_includes_script_when_unfrozen():
    import desktop_app

    cmd = desktop_app.build_backend_command(
        executable="/usr/bin/python3",
        port=4567,
        host="0.0.0.0",
        frozen=False,
        script_path="/opt/grayshare/desktop_app.py",
    )
    assert cmd[:4] == [
        "/usr/bin/python3",
        "/opt/grayshare/desktop_app.py",
        "--server-only",
        "--port",
    ]
    frozen_cmd = desktop_app.build_backend_command(
        executable="/opt/GrayShare.exe",
        port=4567,
        host="0.0.0.0",
        frozen=True,
        script_path="/opt/grayshare/desktop_app.py",
    )
    assert frozen_cmd[0] == "/opt/GrayShare.exe"
    assert "--server-only" in frozen_cmd
    assert "/opt/grayshare/desktop_app.py" not in frozen_cmd


def test_resolve_listen_port_defaults_to_4567(tmp_path, monkeypatch):
    import desktop_app

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    assert desktop_app.resolve_listen_port(None, tmp_path) == 4567
    assert desktop_app.resolve_listen_port(0, tmp_path) == 4567
    assert desktop_app.resolve_listen_port(9001, tmp_path) == 9001
    (tmp_path / "app_config.json").write_text('{"port": 7777}', encoding="utf-8")
    assert desktop_app.resolve_listen_port(None, tmp_path) == 7777


def test_enable_venv_system_site_packages(tmp_path, monkeypatch):
    import desktop_app
    import sys

    cfg = tmp_path / "pyvenv.cfg"
    cfg.write_text("home = /usr\ninclude-system-site-packages = false\n", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(tmp_path))
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    assert desktop_app.enable_venv_system_site_packages() is True
    text = cfg.read_text(encoding="utf-8")
    assert "include-system-site-packages = true" in text
    assert desktop_app.enable_venv_system_site_packages() is False


def test_lan_ip_prefers_home_wifi_over_docker_and_vm():
    import desktop_app

    assert desktop_app.skip_netif("docker0")
    assert desktop_app.skip_netif("veth0abc")
    assert desktop_app.skip_netif("br-1234")
    assert not desktop_app.skip_netif("wlp2s0")
    wifi = desktop_app.lan_ip_score("192.168.1.13", "wlp2s0")
    vm = desktop_app.lan_ip_score("172.16.0.2", "br-internal")
    docker = desktop_app.lan_ip_score("172.17.0.1", "docker0")
    assert docker < 0
    assert vm < 0 or wifi > vm
    assert wifi > desktop_app.lan_ip_score("10.0.0.5", "eth0")
    assert desktop_app.lan_ip_score("172.16.0.2", "eth0") < wifi


def test_strip_snap_library_paths():
    import desktop_app

    raw = "/usr/lib:/snap/core20/current/lib/x86_64-linux-gnu:/usr/local/lib"
    cleaned = desktop_app.strip_snap_library_paths(raw)
    assert "/snap/" not in cleaned
    assert "/usr/lib" in cleaned
    assert desktop_app.strip_snap_library_paths("/snap/core20/current/lib") == ""


def test_linux_gi_message_mentions_running_interpreter():
    import desktop_app
    import sys

    msg = desktop_app.linux_gi_unavailable_message()
    assert "python3-gi" in msg or "GTK" in msg
    assert sys.executable in msg
    assert "grayshare --headless" in msg

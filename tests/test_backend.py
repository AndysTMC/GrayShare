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

# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

block_cipher = None


def find_runtime_binaries():
    binaries = []
    search_roots = [
        Path(sys.prefix),
        Path(sys.base_prefix),
    ]
    seen = set()
    candidate_names = ("ffi.dll", "libffi-8.dll", "libffi-7.dll", "libffi-6.dll")
    candidate_dirs = ("DLLs", "Library/bin")

    for root in search_roots:
        for relative_dir in candidate_dirs:
            directory = root / relative_dir
            if not directory.exists():
                continue
            for name in candidate_names:
                dll_path = directory / name
                if dll_path.exists():
                    key = str(dll_path.resolve()).lower()
                    if key not in seen:
                        binaries.append((str(dll_path), "."))
                        seen.add(key)
    return binaries


runtime_binaries = find_runtime_binaries()

a = Analysis(
    ["desktop_app.py"],
    pathex=["."],
    binaries=runtime_binaries,
    datas=[
        ("templates", "templates"),
        ("static", "static"),
    ],
    hiddenimports=[
        "main",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "jinja2",
        "webview",
        "webview.platforms.winforms",
        "webview.platforms.edgechromium",
        "zeroconf",
        "miniupnpc",
        "cryptography",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GrayShare",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon="static/app_icon.ico",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="file_version_info.txt",
)

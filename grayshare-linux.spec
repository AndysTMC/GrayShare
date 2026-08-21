# -*- mode: python ; coding: utf-8 -*-
# Linux build definition: pyinstaller grayshare-linux.spec
# Produces dist/grayshare (one-file executable).

import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ["desktop_app.py"],
    pathex=["."],
    binaries=[],
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
        # FastAPI resolves python-multipart lazily at request time.
        "multipart",
        # Optional SMB storage backend (try/except import in main.py).
        "smbclient",
        "webview",
        "webview.platforms.gtk",
        "webview.platforms.qt",
        "zeroconf",
        "miniupnpc",
        "cryptography",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["webview.platforms.winforms", "webview.platforms.edgechromium", "webview.platforms.cocoa"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="grayshare",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

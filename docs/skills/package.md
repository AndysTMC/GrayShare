# Package and verify

Recurring job: freeze GrayShare and check that the binary still boots.

## Windows

```powershell
.\build_portable.ps1 -SkipInstall
```

Full installer (needs NSIS `makensis`):

```powershell
.\build_portable.ps1
```

Outputs: `dist\GrayShare.exe`, `dist\GrayShare-Setup.exe`. If `dist` is locked (running `GrayShare.exe`, or Explorer in `dist` / `dist_build`), the script falls back to `dist_build`.

If packaging regresses, check `_ctypes` / `ffi.dll` in `grayshare.spec`, hidden imports (`multipart`, `smbclient`, uvicorn protocol extras), version metadata, and icons.

## Linux / macOS

```bash
./build.sh --skip-install                  # dist/grayshare
./build.sh --skip-install --server-only    # dist/grayshare-server (no GUI deps)
```

## After a runtime or packaging change

1. Syntax-check changed Python and JS.
2. Rebuild (`build_portable.ps1 -SkipInstall` or `./build.sh --skip-install`).
3. Launch the packaged binary.
4. `GET /api/health`.
5. Read `startup.log` and `backend.log` in the per-OS data dir.
6. If transfer code changed, exercise the receive path you touched (desktop save-local, File System Access, or native download).

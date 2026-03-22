# AGENTS.md

This file is for coding agents and maintainers working in this repository.

## Project Shape

GrayShare is a Windows desktop wrapper around a FastAPI app.

Core files:

- `desktop_app.py`
  Desktop launcher, embedded server startup, LAN/service advertisement, native save dialog bridge.
- `main.py`
  FastAPI server, in-memory session state, storage backends, transfer endpoints.
- `templates/index.html`
  HTML shell for the single-page UI.
- `static/app.js`
  Frontend logic for send/receive, QR, activity, settings, clear-data, and save behavior.
- `static/styles.css`
  UI styling.
- `grayshare.spec`
  PyInstaller build definition.
- `installer.nsi`
  NSIS installer script.
- `build_portable.ps1`
  Main build entry point.

## Important Current Behavior

### Data Location

The desktop/runtime data root is:

- `%USERPROFILE%\.grayshare`

Key subpaths:

- `inbox`
- `webview`
- `startup.log`
- `settings.json`
- `app_config.json`

### Client Settings

Client settings are split by access path:

- loopback / desktop (`localhost`, `127.0.0.1`, `::1`)
  Uses `%USERPROFILE%\.grayshare\settings.json`
- LAN / browser clients
  Use browser `localStorage`

Do not collapse this back to a single storage path unless that is an explicit product change.

### Desktop Port Config

The preferred desktop port is stored in:

- `%USERPROFILE%\.grayshare\app_config.json`

Loopback Settings can:

- validate a candidate port with `/api/app/port-check`
- save the chosen port and trigger a desktop restart with `/api/app/restart`

If the configured port is unavailable at startup, `desktop_app.py` falls back to an automatic port and logs the failure in `startup.log`.

### Clear Data

`POST /api/data/clear` is supposed to remove runtime data while preserving:

- `%USERPROFILE%\.grayshare\settings.json`
- `%USERPROFILE%\.grayshare\app_config.json`
- `%USERPROFILE%\.grayshare\webview`

If you change clear-data behavior, check the interaction with:

- `%USERPROFILE%\.grayshare\webview`
- `%USERPROFILE%\.grayshare\settings.json`
- `%USERPROFILE%\.grayshare\app_config.json`
- `desktop_app.py` startup cleanup
- the Settings page copy in `templates/index.html`
- the success message in `static/app.js`

### Receive Paths

There are currently three receive flows:

- desktop app on loopback
  Uses pywebview native save dialog and `POST /api/receive/{id}/save-local`
- browsers with File System Access API
  Uses JS blob + file handle write
- mobile / plain browsers
  Uses `GET /api/receive/{id}/download` so the browser handles the download natively

If receive behavior changes, verify all three.

### Storage Modes

`FILES_STORAGE_MODE` controls the storage backend:

- `local`
- `smb`

SMB mode uses `smbprotocol` / `smbclient` and is only the file storage backend. Device-to-device sharing is still over the FastAPI HTTP server.

## Build Workflow

Primary build command:

```powershell
.\build_portable.ps1 -SkipInstall
```

Full build:

```powershell
.\build_portable.ps1
```

Outputs:

- `dist\GrayShare.exe`
- `dist\GrayShare-Setup.exe`

If `dist` is locked, the script falls back to `dist_build`.

Common lock sources:

- running `GrayShare.exe`
- Explorer open in `dist` or `dist_build`

## Packaging Notes

- `grayshare.spec` explicitly bundles the native FFI runtime needed by `_ctypes` in the packaged exe.
- `file_version_info.txt` carries Windows file metadata for the portable exe.
- `installer.nsi` contains publisher/version/icon metadata for the NSIS installer.

If packaging regresses, check:

- `_ctypes` / `ffi.dll` bundling
- hidden imports in `grayshare.spec`
- icon resources
- `dist` lock fallback behavior in `build_portable.ps1`

## Verification Checklist

After meaningful runtime or packaging changes:

1. Parse or syntax-check modified source files.
2. Rebuild with `.\build_portable.ps1 -SkipInstall`.
3. Start the packaged exe and verify `GET /api/health`.
4. If receive/save logic changed, verify the correct path:
   desktop loopback, browser save-handle path, or native browser download path.

Useful local smoke test pattern:

- launch `dist\GrayShare.exe`
- watch `%USERPROFILE%\.grayshare\startup.log`
- call `/api/health`

## Editing Guidance

- Prefer narrow fixes over broad rewrites; the app has accumulated behavior-specific workarounds for pywebview, packaging, and mobile browser downloads.
- Be careful around user-visible strings in `static/app.js` and `templates/index.html`; some existing text contains mojibake from earlier edits. Do not mass-normalize text unless you intend to clean the UI copy everywhere.
- Keep desktop-only capabilities behind the pywebview bridge or loopback-only endpoints. Do not expose arbitrary local file writes to LAN clients.
- If you change receive endpoints, keep passcode validation consistent across:
  `info`, `chunk`, `download`, `POST /receive`, and `save-local`.

## Recommended Next Places To Look

For desktop startup issues:

- `desktop_app.py`
- `%USERPROFILE%\.grayshare\startup.log`

For transfer bugs:

- `main.py`
- `static/app.js`

For packaging issues:

- `grayshare.spec`
- `build_portable.ps1`
- `installer.nsi`

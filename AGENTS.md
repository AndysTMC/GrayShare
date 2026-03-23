# AGENTS.md

Notes for coding agents and maintainers working in this repository.

## Repo Shape

GrayShare is a Windows desktop shell around a FastAPI app.

Main files:

- `desktop_app.py`
  Desktop launcher, splash screen, child backend process startup, LAN advertisement, native save bridge, graceful shutdown.
- `main.py`
  FastAPI app, storage backends, share/session state, transfer endpoints, settings/config APIs, runtime logging.
- `templates/index.html`
  Single-page shell.
- `static/app.js`
  Frontend logic for send/receive, QR, PWA, settings, desktop/browser behavior split, and client-side error reporting.
- `static/styles.css`
  Styling.
- `grayshare.spec`
  PyInstaller build definition.
- `installer.nsi`
  NSIS installer script.
- `build_portable.ps1`
  Main build script.

## Runtime Data

Desktop/runtime data lives in:

- `%USERPROFILE%\.grayshare`

Important paths:

- `inbox`
- `webview`
- `settings.json`
- `app_config.json`
- `startup.log`
- `backend.log`

## Logging

There are two per-launch logs:

- `startup.log`
  Desktop launcher log from `desktop_app.py`
- `backend.log`
  FastAPI/backend log from `main.py`

Both logs are reset at each fresh desktop launch.

Current logging coverage includes:

- desktop startup and shutdown
- stale-process cleanup
- backend boot
- settings/config reads and writes
- important transfer actions
- backend unhandled request exceptions
- forwarded frontend JS errors and rejected promises via `/api/log/client`

Do not add logs to high-frequency polling routes unless the user explicitly wants noisy trace logging.

## Desktop Startup Model

Current desktop flow:

1. `GrayShare.exe` starts.
2. `desktop_app.py` resets logs and opens a splash window.
3. It launches the backend as a separate child process using `--server-only`.
4. It waits for `/api/health`.
5. The splash hands off to the real app URL.

Important consequence:

- do not import `main.py` from the desktop process just to mutate runtime state; that creates a second backend module instance in the UI process and leads to confusing side effects

## Settings Behavior

Client settings are intentionally split:

- loopback / desktop access (`localhost`, `127.0.0.1`, `::1`)
  `%USERPROFILE%\.grayshare\settings.json`
- LAN browser access
  browser `localStorage`

Do not collapse these without an explicit product change.

Desktop port preference is stored in:

- `%USERPROFILE%\.grayshare\app_config.json`

Current behavior:

- the Settings page checks port availability with `/api/app/port-check`
- saving the port writes `app_config.json`
- the change applies on the next launch
- if the configured port is busy, startup falls back to a free temporary port and logs the fallback

## Clear Data

`POST /api/data/clear` should remove runtime data while preserving:

- `%USERPROFILE%\.grayshare\settings.json`
- `%USERPROFILE%\.grayshare\app_config.json`
- `%USERPROFILE%\.grayshare\webview`

If you change clear-data behavior, verify the desktop Settings copy and the success message in `static/app.js`.

## Receive Flows

There are three receive paths:

- desktop loopback
  pywebview save dialog + `POST /api/receive/{id}/save-local`
- browsers with File System Access API
  in-browser save-handle path
- mobile / plain browsers
  native browser download via `GET /api/receive/{id}/download`

If receive behavior changes, verify all three.

## Storage Modes

`FILES_STORAGE_MODE`:

- `local`
- `smb`

SMB is only the backing storage mode. Sharing between devices is still over GrayShare's HTTP server.

## PWA Notes

Install-related assets:

- `/manifest.webmanifest`
- `/sw.js`
- `static/pwa-192.png`
- `static/pwa-512.png`

Keep manifest and service-worker routes rooted at `/`, not under `/static`.

Desktop pywebview should not depend on the PWA path to function.

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

If `dist` is locked, the build falls back to `dist_build`.

Common lock sources:

- a running `GrayShare.exe`
- Explorer open in `dist` or `dist_build`

## Packaging Notes

If packaging regresses, check:

- `_ctypes` / `ffi.dll` bundling in `grayshare.spec`
- hidden imports
- version metadata
- icon resources
- `dist` lock fallback in `build_portable.ps1`

## Verification

After meaningful runtime or packaging changes:

1. Syntax-check changed Python and JS files.
2. Rebuild with `.\build_portable.ps1 -SkipInstall`.
3. Launch the packaged exe.
4. Verify `GET /api/health`.
5. Check `%USERPROFILE%\.grayshare\startup.log` and `%USERPROFILE%\.grayshare\backend.log`.
6. If transfer code changed, verify the relevant receive path.

## Editing Guidance

- Prefer narrow fixes; this app has several platform-specific workarounds already.
- Keep desktop-only capability behind loopback checks or the pywebview bridge.
- Do not expose arbitrary file-write behavior to LAN clients.
- Be careful with user-visible copy in `static/app.js` and `templates/index.html`; the file has some existing mojibake and should not be mass-normalized casually.

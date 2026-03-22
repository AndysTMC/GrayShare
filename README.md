# GrayShare

GrayShare is a Windows-first LAN file sharing app built with FastAPI, pywebview, and a single-page frontend.

It has two main usage modes:

- desktop app: a packaged Windows executable that starts the local API server and opens the UI in a pywebview window
- browser client: any device on the same network can open the advertised URL and use the same UI

## What It Does

- shares one file at a time from a sender
- lists active sharers for receivers
- supports optional passcodes per share
- shows a QR code for the current LAN URL
- stores app data under `%USERPROFILE%\.grayshare`
- supports local-disk storage by default, with optional SMB-backed storage

## Current Architecture

- [desktop_app.py](./desktop_app.py)
  Starts the embedded Uvicorn server, opens the pywebview window, advertises the service on LAN, and exposes a small desktop JS bridge for native save dialogs.
- [main.py](./main.py)
  FastAPI application, storage backends, share/session state, upload/download endpoints, local save endpoint, activity log, and server settings.
- [templates/index.html](./templates/index.html)
  Single-page UI shell.
- [static/app.js](./static/app.js)
  Client-side app logic, upload/download behavior, QR rendering, local settings, and view switching.
- [static/styles.css](./static/styles.css)
  App styling.
- [grayshare.spec](./grayshare.spec)
  PyInstaller spec for the portable Windows executable.
- [installer.nsi](./installer.nsi)
  NSIS installer script for `GrayShare-Setup.exe`.
- [build_portable.ps1](./build_portable.ps1)
  Main build script for both the portable exe and the NSIS installer.

## Runtime Behavior

### Desktop Launch

The packaged app:

- chooses a free local port unless one is provided
- starts the API server on `0.0.0.0`
- opens the UI against `127.0.0.1`
- advertises the LAN URL via `/api/network/info`
- writes startup diagnostics to `%USERPROFILE%\.grayshare\startup.log`

By default the app prefers plain `http` for LAN compatibility. TLS is optional from the command line.

### Storage Modes

GrayShare supports two storage backends:

- `local` (default)
  Files are stored in `%USERPROFILE%\.grayshare\inbox`
- `smb`
  Files are stored on an SMB share using `smbprotocol` / `smbclient`

Environment variables for SMB mode:

```powershell
$env:FILES_STORAGE_MODE = "smb"
$env:SMB_SERVER = "YOUR_SERVER"
$env:SMB_SHARE_PATH = "\\YOUR_SERVER\YOUR_SHARE\GrayShare"
$env:SMB_USERNAME = "YOUR_USERNAME"
$env:SMB_PASSWORD = "YOUR_PASSWORD"
```

If `FILES_STORAGE_MODE` is not set, GrayShare uses local storage.

### Client Settings

Client-side settings are stored in browser/webview `localStorage`, not in a backend config file.

Current local settings:

- display name
- theme
- upload chunk size override
- upload worker/thread override
- receive list refresh interval

### Clear Data

The Settings page includes a `Clear Data` action.

It clears:

- inbox files
- logs
- runtime transfer state

It preserves:

- the webview/browser profile so `localStorage` settings remain available

## Download Strategy

GrayShare uses different receive paths depending on the client:

- desktop app on loopback
  Uses the pywebview bridge plus `/api/receive/{id}/save-local` to save directly to a native file path.
- browsers with `showSaveFilePicker`
  Downloads into memory and writes to a user-selected file handle.
- mobile / normal browsers without desktop save APIs
  Uses the native browser download endpoint `/api/receive/{id}/download`.

This split exists to avoid browser-specific save quirks, especially on Android.

## Development

### Requirements

- Python 3.13 environment
- Windows for the packaged desktop app flow
- optional NSIS for installer generation

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pyinstaller
```

Run the API only:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

Open:

```text
http://127.0.0.1:8000
```

Run the desktop app in source form:

```powershell
.\.venv\Scripts\python.exe .\desktop_app.py
```

Optional flags:

```powershell
.\.venv\Scripts\python.exe .\desktop_app.py --port 50123 --width 1200 --height 800
.\.venv\Scripts\python.exe .\desktop_app.py --tls
.\.venv\Scripts\python.exe .\desktop_app.py --no-tls
```

## Build

Build both the portable exe and installer:

```powershell
.\build_portable.ps1
```

Skip dependency installation:

```powershell
.\build_portable.ps1 -SkipInstall
```

Skip NSIS installer creation:

```powershell
.\build_portable.ps1 -SkipInstall -SkipNsis
```

Build outputs:

- `dist\GrayShare.exe`
- `dist\GrayShare-Setup.exe`

If `dist` is locked by Explorer or a running process, the build script falls back to `dist_build`.

## App Data

Main runtime data lives under:

```text
%USERPROFILE%\.grayshare
```

Notable paths:

- `%USERPROFILE%\.grayshare\inbox`
- `%USERPROFILE%\.grayshare\webview`
- `%USERPROFILE%\.grayshare\startup.log`

## API Overview

Main endpoints:

- `GET /`
- `GET /api/health`
- `GET /api/network/info`
- `GET /api/shares`
- `POST /api/share`
- `POST /api/share/init`
- `POST /api/share/{sharer_id}/chunk`
- `POST /api/share/{sharer_id}/finalize`
- `POST /api/share/{sharer_id}/stop`
- `GET /api/receive/{sharer_id}/info`
- `GET /api/receive/{sharer_id}/chunk/{chunk_index}`
- `POST /api/receive/{sharer_id}`
- `GET /api/receive/{sharer_id}/download`
- `POST /api/receive/{sharer_id}/save-local`
- `GET /api/activity`
- `GET /api/inbox`
- `GET /api/settings`
- `POST /api/data/clear`

## Troubleshooting

### App does not start

Check:

- `%USERPROFILE%\.grayshare\startup.log`

### Build fails because `dist` or `dist_build` is locked

Close:

- running `GrayShare.exe` processes
- Explorer windows open on the output folder

Then rebuild.

### Phone cannot open the LAN URL

Check:

- the phone and PC are on the same LAN
- Windows Firewall allows GrayShare on private networks
- guest Wi-Fi or AP isolation is not enabled

### Installer uninstall

The NSIS uninstaller removes:

- installed files
- shortcuts
- `%USERPROFILE%\.grayshare` for the current user

Close GrayShare before uninstalling so Windows does not keep runtime files locked.

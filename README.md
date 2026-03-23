# GrayShare

GrayShare is a Windows LAN file-sharing app with a packaged desktop executable and a browser client for other devices on the same network.

## What It Does

- share files from one device to others on the LAN
- show a QR code for quick mobile access
- support optional passcodes
- save desktop data under `%USERPROFILE%\.grayshare`
- build both a portable exe and an NSIS installer

## How It Works

- `GrayShare.exe` opens a pywebview desktop window
- the desktop app starts the FastAPI backend as a separate child process
- the desktop UI uses loopback (`127.0.0.1`)
- phones and other devices use the LAN URL shown in the app

Desktop and backend logs are written per launch to:

- `%USERPROFILE%\.grayshare\startup.log`
- `%USERPROFILE%\.grayshare\backend.log`

## Data Location

GrayShare stores runtime data in:

```text
%USERPROFILE%\.grayshare
```

Important files and folders:

- `inbox`
- `webview`
- `settings.json`
- `app_config.json`
- `startup.log`
- `backend.log`

## Settings

Settings behave differently depending on how the app is opened:

- desktop / loopback access
  Settings are stored in `%USERPROFILE%\.grayshare\settings.json`
- LAN browser access
  Settings are stored in that browser's `localStorage`

The preferred desktop port is stored in `%USERPROFILE%\.grayshare\app_config.json`.
If that port is busy on launch, GrayShare falls back to a free temporary port for that run.

## Storage Modes

GrayShare supports:

- `local` (default)
- `smb`

`smb` mode is only the storage backend. Device-to-device transfer still happens over GrayShare's HTTP server.

Example SMB environment:

```powershell
$env:FILES_STORAGE_MODE = "smb"
$env:SMB_SERVER = "YOUR_SERVER"
$env:SMB_SHARE_PATH = "\\YOUR_SERVER\YOUR_SHARE\GrayShare"
$env:SMB_USERNAME = "YOUR_USERNAME"
$env:SMB_PASSWORD = "YOUR_PASSWORD"
```

## Receive Paths

GrayShare currently uses three receive flows:

- desktop loopback
  Native save dialog via pywebview and `POST /api/receive/{id}/save-local`
- browsers with File System Access API
  browser-selected save handle
- mobile / plain browsers
  native browser download via `GET /api/receive/{id}/download`

## PWA

Browser clients include a manifest and service worker for install support.

Important limitation:

- `localhost` is installable in supported browsers
- plain `http://LAN-IP:port` may not show an install prompt on some browsers because PWA installability depends on secure-context rules

## Development

Create an environment and install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pyinstaller
```

Run the desktop app from source:

```powershell
.\.venv\Scripts\python.exe .\desktop_app.py
```

Run the API only:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload
```

## Build

Build both the portable exe and installer:

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

## Troubleshooting

If the desktop app fails to start, check:

- `%USERPROFILE%\.grayshare\startup.log`
- `%USERPROFILE%\.grayshare\backend.log`

If LAN clients cannot connect, check:

- both devices are on the same network
- Windows Firewall allows GrayShare on private networks
- the network is not using client isolation / guest isolation

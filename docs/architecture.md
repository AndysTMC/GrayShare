---
type: belief
---

# Current shape

GrayShare is one process tree: a desktop (or headless) launcher plus a FastAPI child that serves a single-page client. Other devices on the LAN open the advertised URL. There is no cloud, no account, and no separate receiver app.

## Boundaries

| Piece | Lives in | Job |
|---|---|---|
| Desktop launcher | `desktop_app.py` | Splash, port, child backend, pywebview, mDNS, optional UPnP, native save dialog, shutdown |
| HTTP API + storage | `main.py` | Shares, chunked transfer, receive, settings, history, SSE |
| Browser UI | `templates/index.html`, `static/app.js`, `static/styles.css` | Send, receive, QR, history, settings |
| Packaging | `grayshare.spec`, `grayshare-linux.spec`, `build_portable.ps1`, `build.sh`, `installer.nsi` | Frozen exe / Linux binary / NSIS |

The UI process must not import `main.py` to mutate runtime state. That would load a second backend module in the parent. The child is always `sys.executable --server-only`. See [0001](decisions/0001-child-backend-process.md).

Windows-only code (stale-process kill, WebView2 default, `CREATE_NO_WINDOW`) is gated by `os.name == "nt"`. Linux GUI needs distro WebKitGTK (`python3-gi`, `gir1.2-webkit2-4.1`); `--server-only` skips that.

## Runtime data

Per-OS directory (`app_data_dir()` in `desktop_app.py` and `_default_app_data_dir()` in `main.py` — keep them in sync). `APP_DATA_DIR` overrides all of them.

- Windows: `%USERPROFILE%\.grayshare`
- macOS: `~/Library/Application Support/GrayShare`
- Linux: `$XDG_DATA_HOME/grayshare` (default `~/.local/share/grayshare`)

| Path | Role |
|---|---|
| `inbox/` | Uploaded files waiting to be received |
| `webview/` | pywebview profile (preserved on clear-data) |
| `settings.json` | Loopback client settings (display name, theme, chunk/threads) |
| `app_config.json` | Preferred desktop port (applies next launch) |
| `history.jsonl` | Append-only transfer events; last 200 replayed on boot |
| `startup.log` / `backend.log` | Reset each desktop launch |

LAN browsers do **not** write `settings.json`. They keep `grayshare.clientSettings` in `localStorage`. Do not collapse the two stores.

## Data model (intent)

There is no generated schema. Product state is in-memory plus those JSON/JSONL files.

- **PendingChunkedUpload** — preallocated inbox file; chunk indexes in `received`; discarded if stale (~30 min).
- **ShareSession** — active share after finalize; heartbeat `last_seen_at`; pruned after ~45 s without a beat.
- **settings.json** — host UI prefs, loopback only.
- **app_config.json** — port preference, not the bound port of this run.
- **history.jsonl** — durable activity tape; trimmed on read, not rewritten.

`FILES_STORAGE_MODE=smb` is a backing store for the inbox, not a second sharing protocol. Chunked `POST /api/share/init` returns 501 in SMB mode; the client falls back to a single `POST /api/share`.

## Desktop startup

1. Reset logs, optional stale-exe kill (full image path only).
2. Splash window (pywebview) with a JS bridge.
3. Child `--server-only` on the configured port, or a free port if that one is busy.
4. Wait for `GET /api/health`.
5. Splash navigates to `http://127.0.0.1:<port>`.
6. mDNS `_grayshare._tcp.local.` in the background. UPnP only if both env flags in [0005](decisions/0005-upnp-requires-passcode.md) are set.

Headless: `python desktop_app.py --server-only --port N` prints the LAN URL and access key.

## Transfer

**Send (local storage).** Client measures upload speed, then `POST /api/share/init` (truncate to `total_size`), parallel `POST /api/share/{id}/chunk` at `offset = index * chunk_size`, then `finalize`. No part files, no merge, no session lock. See [0003](decisions/0003-offset-chunk-writes.md). Multi-file send is a store-method zip in the browser (`buildZipBlob`); classic headers, cap 4 GiB.

**Live presence.** `GET /api/events?k=` is SSE gated like `/api/shares`. Keepalive comment every 15 s. On SSE connect the 5 s poll becomes a 30 s safety net; on error it returns to 5 s. Do not remove the poll.

**Receive (three paths — change one, check all three):**

1. Desktop loopback — pywebview save dialog, then `POST /api/receive/{id}/save-local`. Same-volume `os.replace`; cross-volume copy. Success ends the share (`saved-locally`) because the inbox copy is consumed.
2. File System Access — one `createWritable` stream; parallel workers queue offset writes (`createFileHandleSink` in `static/app.js`).
3. Native browser download — `GET /api/receive/{id}/download` with HTTP Range (`206` / `416`). Files above ~1.5 GiB without FS Access take this path so the client does not buffer a Blob.

## Auth and privilege

- **Access key** — per-boot token. `/api/shares` and `/api/events` are 403 without `?k=`. QR/network URL carries it; the client stores `grayshare.accessKey` and strips the query. See [0002](decisions/0002-per-boot-access-key.md).
- **Passcodes** — optional per share. Compare with `_passcode_matches` (constant-time). Prefer `X-GrayShare-Passcode`; query/form remain for native downloads.
- **Loopback-only** — save-local, clear-data, host `settings.json` / port APIs. LAN clients must not write the host disk. See [0004](decisions/0004-loopback-host-writes.md).
- **Network info** — access key in the URL is revealed only to loopback or a caller who already has a valid key.

`POST /api/data/clear` deletes inbox, logs, and `history.jsonl`. It keeps `settings.json`, `app_config.json`, and `webview/`.

## PWA

`/manifest.webmanifest` and `/sw.js` stay at `/`, not under `/static`. Desktop pywebview unregisters service workers and does not depend on installability. There is no in-app install button. Plain `http://LAN-IP` is often not a secure context, so LAN install prompts may not appear.

## Logging

`startup.log` (launcher) and `backend.log` (API). Frontend errors go to `POST /api/log/client`. Do not log high-frequency poll routes.

# Agent protocol

## Commands

- Install (Linux/macOS): `curl -fsSL https://raw.githubusercontent.com/AndysTMC/GrayShare/main/install.sh | bash`; Windows: `irm https://raw.githubusercontent.com/AndysTMC/GrayShare/main/install.ps1 | iex`. Both install a global `grayshare` command.
- Run (Linux/macOS): `./grayshare.sh` (desktop) or `./grayshare.sh --headless --port 4567`; Windows: `.\grayshare.ps1` / `-Headless`. Launchers self-bootstrap the venv on first run.
- Install: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt pyinstaller` (Windows: `.\.venv\Scripts\Activate.ps1`)
- Linux GUI libs: `sudo apt install python3-gi gir1.2-webkit2-4.1 libgtk-3-0`
- Dev (desktop): `python desktop_app.py`
- Dev (API only): `python -m uvicorn main:app --reload`
- Headless: `python desktop_app.py --server-only --port 4567`
- Test (one file / full): `pip install -r requirements-dev.txt` then `.venv/bin/python -m pytest tests/test_backend.py -v`
- Lint / format: none in-repo
- Build Windows: `.\build_portable.ps1 -SkipInstall` (full installer: `.\build_portable.ps1`)
- Build Linux/macOS: `./build.sh --skip-install` or `./build.sh --skip-install --server-only`

Keep `desktop_app.py` `app_data_dir()` in sync with `main.py` `_default_app_data_dir()`.

## Hard rules

- Do not add a dependency, edit generated/frozen artifacts, or change packaging hidden imports without an explicit ask.
- Do not commit secrets, credentials, or `.env` values into the repo or these docs.
- Minimal diffs. For work that will edit more than two files, write `PLAN.md` first (session file; do not commit an empty one).
- Run the targeted test before calling the task done.
- Do not import `main.py` from the desktop process to mutate runtime state; the API is a `--server-only` child.
- Do not collapse loopback `settings.json` and LAN `localStorage` settings.
- Do not reintroduce part-file merging or a session-wide asyncio lock on chunked upload.
- Do not remove the per-boot access-key gate on `/api/shares` and `/api/events`.
- Do not restore unconditional UPnP; mapping requires `GRAYSHARE_ENABLE_UPNP=1` and `GRAYSHARE_REQUIRE_PASSCODE=1`.
- Stale-process kill matches the full image path (`QueryFullProcessImageNameW`), never the exe name alone.
- Keep desktop-only capability behind loopback checks or the pywebview bridge. Do not expose arbitrary file-write to LAN clients.
- `POST /api/data/clear` must preserve `settings.json`, `app_config.json`, and `webview/`.
- Do not remove the share-list polling fallback when SSE is connected.
- Keep `/manifest.webmanifest` and `/sw.js` at `/`. Do not reintroduce a visible install-app control.
- Do not add logs to high-frequency poll routes.
- Prefer narrow fixes. Do not mass-normalize copy in `static/app.js` / `templates/index.html` (existing mojibake).
- If receive behavior changes, verify desktop save-local, File System Access, and native download.

## Authority

- Level 0 (not facts): `PLAN.md`, chat
- Level 2 (constraints): accepted files in `docs/decisions/`
- Level 3 (prefer over prose): `main.py` / `static/app.js` over docs if they disagree; specs and build scripts over packaging essays
- Level 4 (do not edit unless asked): this file, `README.md`, `LICENSE` if present

## Where to read

| Need | File |
|---|---|
| What this is | [README.md](README.md) |
| How the system fits together | [docs/architecture.md](docs/architecture.md) |
| Why a choice was made | [docs/decisions/](docs/decisions/_index.md) |
| Package / verify | [docs/skills/package.md](docs/skills/package.md) |

## After you finish

Propose, do not silently apply: a decision draft if you chose something, a one-line note if git will not explain it. Do not silently edit this file or `README.md`.

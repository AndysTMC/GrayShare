#!/usr/bin/env bash
# Linux/macOS build script for GrayShare.
# Usage: ./build.sh [--skip-install] [--server-only]
#
#   default        builds dist/grayshare (desktop + server, one file)
#   --server-only  builds dist/grayshare-server (headless, no GUI deps)
set -euo pipefail
cd "$(dirname "$0")"

SKIP_INSTALL=0
SERVER_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --skip-install) SKIP_INSTALL=1 ;;
    --server-only) SERVER_ONLY=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PY="${PYTHON:-python3}"

if [[ $SKIP_INSTALL -eq 0 ]]; then
  if [[ ! -d .venv ]]; then
    if [[ "$(uname -s)" == "Linux" ]]; then
      "$PY" -m venv --system-site-packages .venv
    else
      "$PY" -m venv .venv
    fi
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt pyinstaller
else
  if [[ -d .venv ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
  fi
fi

if [[ $SERVER_ONLY -eq 1 ]]; then
  echo "==> Building headless server (no GUI dependencies bundled)"
  pyinstaller --clean --noconfirm \
    --workpath build --distpath dist \
    --name grayshare-server \
    --hidden-import main \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.websockets.auto \
    --hidden-import jinja2 \
    --hidden-import multipart \
    --hidden-import smbclient \
    --add-data "templates:templates" \
    --add-data "static:static" \
    --exclude-module webview \
    --exclude-module zeroconf \
    --exclude-module miniupnpc \
    desktop_app.py
  echo "==> Built dist/grayshare-server"
  echo "    Run: ./dist/grayshare-server --server-only --port 4567"
else
  echo "==> Building desktop app"
  pyinstaller --clean --noconfirm \
    --workpath build --distpath dist \
    grayshare-linux.spec
  echo "==> Built dist/grayshare"
  echo "    Run: ./dist/grayshare"
fi

echo "Done."

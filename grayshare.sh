#!/usr/bin/env bash
# GrayShare one-command launcher (Linux/macOS).
#
#   ./grayshare.sh              desktop app
#   ./grayshare.sh --headless   headless server on the LAN
#   ./grayshare.sh --port 4567  pick a port
#
# First run creates .venv and installs dependencies automatically.
set -euo pipefail
cd "$(dirname "$0")"

PY=".venv/bin/python"

# First-run bootstrap: create venv and install deps if missing.
if [[ ! -x "$PY" ]]; then
    echo "==> First run: setting up GrayShare (one time)..."
    python3 -m venv .venv
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r requirements.txt
    echo "==> Setup complete."
fi

ARGS=()
HEADLESS=0
for arg in "$@"; do
    case "$arg" in
        --headless|-H) HEADLESS=1 ;;
        *) ARGS+=("$arg") ;;
    esac
done

if [[ $HEADLESS -eq 1 ]]; then
    exec "$PY" desktop_app.py --server-only "${ARGS[@]}"
else
    exec "$PY" desktop_app.py "${ARGS[@]}"
fi

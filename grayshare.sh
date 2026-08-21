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

if [[ -n "${PYTHON:-}" ]]; then
    BOOT_PY="$PYTHON"
elif [[ "$(uname -s)" == "Linux" && -x /usr/bin/python3 ]]; then
    BOOT_PY=/usr/bin/python3
else
    BOOT_PY=python3
fi
PY=".venv/bin/python"

# First-run bootstrap: create venv and install deps if missing.
VENV_ARGS=()
if [[ "$(uname -s)" == "Linux" ]]; then
    VENV_ARGS=(--system-site-packages)
fi
recreate=0
if [[ ! -x "$PY" ]]; then
    recreate=1
elif [[ "$(uname -s)" == "Linux" ]]; then
    have=$("$PY" -c "import sys; print(sys.base_prefix)" 2>/dev/null || true)
    want=$("$BOOT_PY" -c "import sys; print(sys.prefix)" 2>/dev/null || true)
    if [[ -n "$want" && "$have" != "$want" ]]; then
        echo "==> Recreating venv with $BOOT_PY so GTK (python3-gi) works..."
        recreate=1
    fi
fi
if [[ $recreate -eq 1 ]]; then
    echo "==> First run: setting up GrayShare (one time)..."
    rm -rf .venv
    "$BOOT_PY" -m venv "${VENV_ARGS[@]}" .venv
    PIP_DISABLE_PIP_VERSION_CHECK=1 "$PY" -m pip install --quiet --no-cache-dir --upgrade pip
    PIP_DISABLE_PIP_VERSION_CHECK=1 "$PY" -m pip install --quiet --no-cache-dir -r requirements.txt
    echo "==> Setup complete."
elif [[ "$(uname -s)" == "Linux" && -f .venv/pyvenv.cfg ]]; then
    sed -i 's/^include-system-site-packages = .*/include-system-site-packages = true/' .venv/pyvenv.cfg || true
fi

# Snap (VS Code/Chrome) leaks /snap/core20 into LD_LIBRARY_PATH; WebKitGTK then
# loads snap libpthread and crashes. Strip before exec.
if [[ -n "${LD_LIBRARY_PATH:-}" ]]; then
    _gs_ld=""
    IFS=:
    for _gs_p in $LD_LIBRARY_PATH; do
        case "$_gs_p" in
            *"/snap/"*|"/snap"*) ;;
            "") ;;
            *) _gs_ld="${_gs_ld:+$_gs_ld:}$_gs_p" ;;
        esac
    done
    unset IFS
    if [[ -n "$_gs_ld" ]]; then
        export LD_LIBRARY_PATH="$_gs_ld"
    else
        unset LD_LIBRARY_PATH
    fi
    unset _gs_ld _gs_p
fi
unset SNAP_LIBRARY_PATH || true

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

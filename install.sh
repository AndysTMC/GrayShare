#!/usr/bin/env bash
# GrayShare installer.
#
#   curl -fsSL https://raw.githubusercontent.com/AndysTMC/GrayShare/main/install.sh -o install-grayshare.sh
#   bash install-grayshare.sh          # review first, then run
#
# Or the one-liner (runs immediately):
#   curl -fsSL https://raw.githubusercontent.com/AndysTMC/GrayShare/main/install.sh | bash
#
# What it does:
#   - clones/updates the app to ~/.local/lib/grayshare/app
#   - creates a private venv there and installs dependencies
#   - puts a `grayshare` command in ~/.local/bin
#
# After installing:
#   grayshare                desktop app
#   grayshare --headless     headless LAN server (prints URL + key)
#   grayshare --port 4567    pick a port
#   grayshare update         pull latest + refresh dependencies
set -euo pipefail

REPO="https://github.com/AndysTMC/GrayShare.git"
# Program files must NOT live in the runtime data dir (~/.local/share/grayshare on
# Linux) — Clear Data would delete the checkout. Prefer ~/.local/lib/grayshare.
LEGACY_ROOT="$HOME/.local/share/grayshare"
if [[ -n "${GRAYSHARE_HOME:-}" ]]; then
    INSTALL_ROOT="$GRAYSHARE_HOME"
elif [[ -d "$LEGACY_ROOT/app/.git" ]]; then
    INSTALL_ROOT="$LEGACY_ROOT"
else
    INSTALL_ROOT="$HOME/.local/lib/grayshare"
fi
APP_DIR="$INSTALL_ROOT/app"
BIN_DIR="$HOME/.local/bin"
BRANCH="main"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- prerequisites ----------------------------------------------------------
command -v git >/dev/null 2>&1 || die "git is required. Install it first (e.g. sudo apt install git)."

# apt python3-gi is compiled for Debian's /usr/bin/python3. Homebrew/uv
# python3.14 cannot import it even with --system-site-packages.
if [[ -n "${PYTHON:-}" ]]; then
    PY="$PYTHON"
elif [[ "$(uname -s)" == "Linux" && -x /usr/bin/python3 ]]; then
    PY=/usr/bin/python3
else
    command -v python3 >/dev/null 2>&1 || die "python3 is required. Install Python 3.10+ first."
    PY=$(command -v python3)
fi
if ! "$PY" -c "import venv" >/dev/null 2>&1; then
    die "python3 venv module missing. On Ubuntu/Debian: sudo apt install python3-venv python3-pip"
fi
say "Using $PY"

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"

# --- get the code -----------------------------------------------------------
if [[ -d "$APP_DIR/.git" ]]; then
    say "Updating existing checkout in $APP_DIR"
    git -C "$APP_DIR" fetch origin "$BRANCH" >/dev/null 2>&1 || true
    git -C "$APP_DIR" reset --hard "origin/$BRANCH" >/dev/null
else
    say "Cloning GrayShare into $APP_DIR"
    rm -rf "$APP_DIR.tmp"
    git clone --depth 1 --branch "$BRANCH" "$REPO" "$APP_DIR.tmp" >/dev/null 2>&1 \
        || die "clone failed — check your internet connection."
    rm -rf "$APP_DIR"
    mv "$APP_DIR.tmp" "$APP_DIR"
fi

# --- python environment -----------------------------------------------------
say "Setting up Python environment (first install takes a minute)"
cd "$APP_DIR"
# Linux GUI needs apt python3-gi on Debian Python, not Homebrew/uv Python.
VENV_ARGS=()
if [[ "$(uname -s)" == "Linux" ]]; then
    VENV_ARGS=(--system-site-packages)
fi
recreate=0
if [[ ! -x ".venv/bin/python" ]]; then
    recreate=1
elif [[ "$(uname -s)" == "Linux" ]]; then
    have=$(.venv/bin/python -c "import sys; print(sys.base_prefix)" 2>/dev/null || true)
    want=$("$PY" -c "import sys; print(sys.prefix)" 2>/dev/null || true)
    if [[ -n "$want" && "$have" != "$want" ]]; then
        say "Recreating venv with $PY (was $have)"
        recreate=1
    fi
fi
if [[ $recreate -eq 1 ]]; then
    rm -rf .venv
    "$PY" -m venv "${VENV_ARGS[@]}" .venv
elif [[ "$(uname -s)" == "Linux" && -f .venv/pyvenv.cfg ]]; then
    sed -i 's/^include-system-site-packages = .*/include-system-site-packages = true/' .venv/pyvenv.cfg || true
fi
# --no-cache-dir avoids "Cache entry deserialization failed". Resolver
# noise about unrelated system packages (e.g. orange3) is harmless.
PIP_DISABLE_PIP_VERSION_CHECK=1 ./.venv/bin/python -m pip install --quiet --no-cache-dir --upgrade pip
if ! PIP_DISABLE_PIP_VERSION_CHECK=1 ./.venv/bin/python -m pip install --quiet --no-cache-dir -r requirements.txt; then
    die "dependency install failed."
fi
if ! ./.venv/bin/python -c "import fastapi, uvicorn, webview" >/dev/null 2>&1; then
    die "Python packages did not import after install. Try: sudo apt install python3-venv python3-pip"
fi

# --- the grayshare command --------------------------------------------------
WRAPPER="$BIN_DIR/grayshare"
cat > "$WRAPPER" <<WRAP
#!/usr/bin/env bash
# GrayShare launcher (installed by install.sh)
APP="$APP_DIR"
PY="\$APP/.venv/bin/python"

if [[ ! -x "\$PY" ]]; then
    echo "GrayShare installation is broken. Reinstall:"
    echo "  curl -fsSL https://raw.githubusercontent.com/AndysTMC/GrayShare/main/install.sh | bash"
    exit 1
fi

case "\${1:-}" in
    update)
        git -C "\$APP" fetch origin "$BRANCH" >/dev/null 2>&1
        git -C "\$APP" reset --hard "origin/$BRANCH" >/dev/null
        SYS_PY=/usr/bin/python3
        if [[ "\$(uname -s)" == "Linux" && -x "\$SYS_PY" ]]; then
            HAVE=\$("\$PY" -c "import sys; print(sys.base_prefix)" 2>/dev/null || true)
            WANT=\$("\$SYS_PY" -c "import sys; print(sys.prefix)" 2>/dev/null || true)
            if [[ -n "\$WANT" && "\$HAVE" != "\$WANT" ]]; then
                echo "Recreating venv with \$SYS_PY so GTK (python3-gi) works..."
                rm -rf "\$APP/.venv"
                "\$SYS_PY" -m venv --system-site-packages "\$APP/.venv"
                PY="\$APP/.venv/bin/python"
            elif [[ -f "\$APP/.venv/pyvenv.cfg" ]]; then
                sed -i 's/^include-system-site-packages = .*/include-system-site-packages = true/' "\$APP/.venv/pyvenv.cfg" 2>/dev/null || true
            fi
        fi
        PIP_DISABLE_PIP_VERSION_CHECK=1 "\$PY" -m pip install --quiet --no-cache-dir --upgrade pip
        PIP_DISABLE_PIP_VERSION_CHECK=1 "\$PY" -m pip install --quiet --no-cache-dir -r "\$APP/requirements.txt"
        echo "GrayShare updated."
        exit 0
        ;;
    uninstall)
        echo "This removes the GrayShare program files and the 'grayshare' command."
        echo "Transfer data is kept. To delete it too:"
        echo "  Linux:  ~/.local/share/grayshare"
        echo "  macOS:  ~/Library/Application Support/GrayShare"
        printf "Continue? [y/N] "
        read -r answer
        case "\$answer" in y|Y|yes|Yes)
            rm -f "$BIN_DIR/grayshare"
            rm -rf "$APP_DIR"
            rmdir "$INSTALL_ROOT" 2>/dev/null || true
            echo "GrayShare uninstalled."
            echo "If the next 'grayshare' command fails, run: hash -r"
            ;;
        *) echo "Cancelled." ;;
        esac
        exit 0
        ;;
    version|--version|-v)
        cd "\$APP"
        TAG=\$(git describe --tags --always 2>/dev/null || echo "dev")
        echo "GrayShare \$TAG"
        exit 0
        ;;
esac

ARGS=()
HEADLESS=0
for a in "\$@"; do
    case "\$a" in
        --headless|-H) HEADLESS=1 ;;
        *) ARGS+=("\$a") ;;
    esac
done

cd "\$APP"
# Snap (VS Code/Chrome) leaks /snap/core20 into LD_LIBRARY_PATH; WebKitGTK then
# loads snap libpthread and crashes (GLIBC_PRIVATE).
if [[ -n "\${LD_LIBRARY_PATH:-}" ]]; then
    _gs_ld=""
    IFS=:
    for _gs_p in \$LD_LIBRARY_PATH; do
        case "\$_gs_p" in
            *"/snap/"*|"/snap"*) ;;
            "") ;;
            *) _gs_ld="\${_gs_ld:+\$_gs_ld:}\$_gs_p" ;;
        esac
    done
    unset IFS
    if [[ -n "\$_gs_ld" ]]; then
        export LD_LIBRARY_PATH="\$_gs_ld"
    else
        unset LD_LIBRARY_PATH
    fi
    unset _gs_ld _gs_p
fi
unset SNAP_LIBRARY_PATH || true
if [[ \$HEADLESS -eq 1 ]]; then
    exec "\$PY" desktop_app.py --server-only "\${ARGS[@]}"
else
    exec "\$PY" desktop_app.py "\${ARGS[@]}"
fi
WRAP
chmod +x "$WRAPPER"

# --- PATH check -------------------------------------------------------------
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        say "NOTE: $BIN_DIR is not in your PATH."
        echo
        echo "    Add this to your ~/.bashrc or ~/.zshrc:"
        echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
        echo
        echo "    Then: source ~/.bashrc   (or open a new terminal)"
        ;;
esac

say "Installed!"
echo
echo "  Start using it:"
echo "    grayshare --headless        # headless LAN server (prints URL + key)"
echo "    grayshare                   # desktop window"
echo "    grayshare --port 4567       # pick a port"
echo "    grayshare update            # update to the latest version"

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
#   - clones/updates the app to ~/.local/share/grayshare/app
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
INSTALL_ROOT="${GRAYSHARE_HOME:-$HOME/.local/share/grayshare}"
APP_DIR="$INSTALL_ROOT/app"
BIN_DIR="$HOME/.local/bin"
BRANCH="main"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- prerequisites ----------------------------------------------------------
command -v git >/dev/null 2>&1 || die "git is required. Install it first (e.g. sudo apt install git)."
command -v python3 >/dev/null 2>&1 || die "python3 is required. Install Python 3.10+ first."

if ! python3 -c "import venv" >/dev/null 2>&1; then
    die "python3 venv module missing. On Ubuntu/Debian: sudo apt install python3-venv python3-pip"
fi

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
if [[ ! -x ".venv/bin/python" ]]; then
    python3 -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt

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
        "\$PY" -m pip install --quiet --upgrade pip
        "\$PY" -m pip install --quiet -r requirements.txt
        echo "GrayShare updated."
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

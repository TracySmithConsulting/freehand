#!/usr/bin/env bash
# FreeHand Installer for macOS / Linux / WSL2
# Usage: curl -fsSL https://freehand.tracysmith.co.za/install.sh | bash

set -e

SCRIPT_NAME="$(basename "$0")"
INSTALL_DIR="${FREEHAND_HOME:-$HOME/.freehand}"
PYTHON_BIN=""
NODE_BIN=""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  FreeHand Installer v0.1.0"
echo "  Local AI agent with OAuth integrations"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── OS detection ────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
    Linux*)   PLATFORM="linux" ;;
    Darwin*)  PLATFORM="macos" ;;
    *)        echo "Unsupported OS: $OS"; exit 1 ;;
esac
echo "  Platform: $PLATFORM"

# ── Check Python 3.11+ ─────────────────────────────────────
check_python() {
    local py="$1"
    if command -v "$py" &>/dev/null; then
        local ver
        ver=$("$py" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
        local major minor
        major="${ver%%.*}"
        minor="${ver##*.}"
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PYTHON_BIN="$py"
            echo "  Python $ver found at $($py -m site --user-base 2>/dev/null || echo /usr)"
            return 0
        fi
    fi
    return 1
}

if ! check_python "python3.12" && \
   ! check_python "python3.11" && \
   ! check_python "python3"; then
    echo ""
    echo "  Python 3.11+ not found."
    echo "  Installing Python..."
    if [ "$PLATFORM" = "macos" ]; then
        if ! command -v brew &>/dev/null; then
            echo "  Homebrew not found. Installing via brew install python@3.12"
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        fi
        brew install python@3.12
        PYTHON_BIN="python3.12"
    else
        echo "  Please install Python 3.11+ manually:"
        echo "    Ubuntu/Debian: sudo apt install python3.12 python3.12-venv python3.12-dev"
        echo "    Fedora:        sudo dnf install python3.12 python3.12-venv"
        echo "    Then re-run this installer."
        exit 1
    fi
else
    echo "  Python $PYTHON_BIN is available."
fi

# ── Check Node.js 18+ ──────────────────────────────────────
check_node() {
    if command -v node &>/dev/null; then
        local ver
        ver=$(node -v | sed 's/v//')
        local major="${ver%%.*}"
        if [[ "$major" -ge 18 ]]; then
            NODE_BIN="node"
            echo "  Node.js $ver found"
            return 0
        fi
    fi
    return 1
}

if ! check_node; then
    echo ""
    echo "  Node.js 18+ not found."
    echo "  Installing Node.js..."
    if [ "$PLATFORM" = "macos" ]; then
        brew install node@18
        NODE_BIN="node"
    else
        curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
        apt-get install -y nodejs
        NODE_BIN="node"
    fi
else
    echo "  Node.js $NODE_BIN is available."
fi

# ── Create install directory ───────────────────────────────
echo ""
echo "  Installing to $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# ── Create Python virtual environment ──────────────────────
echo "  Creating Python virtual environment..."
"$PYTHON_BIN" -m venv venv
source venv/bin/activate

# ── Install FreeHand ───────────────────────────────────────
echo "  Installing freehand-agent..."
pip install --upgrade pip
pip install "freehand-agent[all]"

# ── Install WhatsApp bridge ────────────────────────────────
echo "  Installing WhatsApp bridge..."
if [ -d "$INSTALL_DIR/bridge" ]; then
    cd "$INSTALL_DIR/bridge"
    "$NODE_BIN" install --no-audit --no-fund
    cd "$INSTALL_DIR"
else
    # Download bridge from GitHub if not bundled
    echo "  Bridge directory not found — downloading..."
    curl -fsSL "https://github.com/TracySmithConsulting/freehand/releases/download/v0.1.0/freehand-agent-0.1.0.tar.gz" \
        | tar xzf - --strip-components=2 bridge/
fi

# ── Initialize FreeHand ────────────────────────────────────
echo ""
echo "  Initializing FreeHand..."
freehand init

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  FreeHand installed successfully!"
echo ""
echo "  Quick start:"
echo "    cd $INSTALL_DIR"
echo "    source venv/bin/activate"
echo "    freehand serve"
echo ""
echo "  Then open: http://localhost:8000"
echo ""
echo "  For OAuth setup, see: docs/OAUTH_SETUP.md"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

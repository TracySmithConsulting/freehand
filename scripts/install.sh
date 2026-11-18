#!/usr/bin/env bash
# FreeHand for Linux, in one line:
#
#   curl -fsSL https://github.com/TracySmithConsulting/freehand/releases/latest/download/install.sh | bash
#
# It sets up the same services as the FreeHand app does on Windows
# (install/compose.yml: FreeHand, its connections, its memory and the
# WhatsApp bridge), all from ready-made images, with the free Docker
# engine. Then FreeHand opens in the browser at its welcome setup.
#
# Running it again updates FreeHand. Your data stays.
#
# Settings (all optional):
#   FREEHAND_HOME         where FreeHand lives (default ~/freehand)
#   FREEHAND_YES=1        don't ask before installing the Docker engine
#   FREEHAND_COMPOSE_URL  where compose.yml comes from (default: the
#                         newest FreeHand release)
#
# docs/design/granny-installer.md, step 4.

set -euo pipefail

HOME_DIR="${FREEHAND_HOME:-$HOME/freehand}"
COMPOSE_URL="${FREEHAND_COMPOSE_URL:-https://github.com/TracySmithConsulting/freehand/releases/latest/download/compose.yml}"
ADDRESS="http://localhost:8000"

say() { printf '%s\n' "$*"; }
stop() { say ""; say "FreeHand wasn't installed: $*"; exit 1; }

say ""
say "Setting up FreeHand."
say ""

# ── This computer ───────────────────────────────────────────────────

case "$(uname -s)" in
    Linux*) ;;
    Darwin*) stop "this is the Linux install. FreeHand for Mac comes later. Until then, a Mac with Docker Desktop can run install/compose.yml from FreeHand's code." ;;
    *) stop "this install is for Linux." ;;
esac

if [ "$(id -u)" -eq 0 ]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    SUDO=""
fi

# Questions go to the keyboard, even when this script comes through a pipe.
ask() {
    local question="$1" reply=""
    if [ "${FREEHAND_YES:-}" = "1" ]; then return 0; fi
    if [ ! -r /dev/tty ]; then
        stop "it needs to ask you something, and can't here. Run it in a terminal, or set FREEHAND_YES=1 to say yes."
    fi
    printf '%s [y/N] ' "$question" > /dev/tty
    read -r reply < /dev/tty || true
    case "$reply" in [Yy]|[Yy][Ee][Ss]) return 0 ;; *) return 1 ;; esac
}

need_root() {
    if [ "$(id -u)" -ne 0 ] && [ -z "$SUDO" ]; then
        stop "installing the Docker engine needs an administrator. Run this as root, or install sudo."
    fi
}

# ── The Docker engine ───────────────────────────────────────────────

have_engine() {
    command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

install_engine() {
    # The engine from this Linux's own packages: the free Docker engine,
    # nothing downloaded from elsewhere and run.
    local install=""
    if command -v apt-get >/dev/null 2>&1; then
        install="apt-get update && apt-get install -y docker.io docker-compose-v2"
    elif command -v dnf >/dev/null 2>&1; then
        install="dnf install -y moby-engine docker-compose"
    elif command -v pacman >/dev/null 2>&1; then
        install="pacman -S --noconfirm --needed docker docker-compose"
    elif command -v zypper >/dev/null 2>&1; then
        install="zypper --non-interactive install docker docker-compose"
    elif command -v apk >/dev/null 2>&1; then
        install="apk add docker docker-cli-compose"
    else
        stop "FreeHand needs the Docker engine, and this Linux's package manager isn't one this install knows. Install Docker (https://docs.docker.com/engine/install/), then run this again."
    fi
    say "FreeHand needs the free Docker engine, which isn't on this computer yet."
    say "It comes from this Linux's own packages:"
    say "  $install"
    ask "Install it now?" || stop "the Docker engine is needed. Install it yourself, then run this again."
    need_root
    $SUDO sh -c "$install"
    # Start it now, and whenever the computer starts.
    if command -v systemctl >/dev/null 2>&1; then
        $SUDO systemctl enable --now docker
    elif command -v rc-update >/dev/null 2>&1; then
        $SUDO rc-update add docker default && $SUDO service docker start
    elif command -v service >/dev/null 2>&1; then
        $SUDO service docker start
    fi
    have_engine || stop "the Docker engine was installed, but 'docker compose' isn't working. Install Docker's compose plugin (https://docs.docker.com/compose/install/linux/), then run this again."
}

have_engine || install_engine

# Use the engine directly if this person may; otherwise through sudo.
# (Joining the docker group would avoid sudo, but it's the same as being
# an administrator, so that's left to the person to choose.)
if docker info >/dev/null 2>&1; then
    DOCKER="docker"
elif [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then
    DOCKER="$SUDO docker"
else
    if command -v systemctl >/dev/null 2>&1; then $SUDO systemctl start docker >/dev/null 2>&1 || true; fi
    if docker info >/dev/null 2>&1; then DOCKER="docker"
    elif [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then DOCKER="$SUDO docker"
    else stop "the Docker engine isn't running. Start it (for example: sudo systemctl start docker), then run this again."
    fi
fi
compose() { (cd "$HOME_DIR" && $DOCKER compose "$@"); }

# ── Is the address free? ────────────────────────────────────────────

ours_running() {
    $DOCKER ps --format '{{.Names}}' 2>/dev/null | grep -qx 'freehand-app'
}

if ! ours_running && command -v ss >/dev/null 2>&1; then
    for port in 8000 3001; do
        if ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${port}\$"; then
            stop "something else on this computer already uses port $port, which FreeHand needs. Stop it, then run this again."
        fi
    done
fi

# ── FreeHand's folder, settings and secrets ─────────────────────────

mkdir -p "$HOME_DIR/vault"
say "FreeHand lives in $HOME_DIR."

tmp="$HOME_DIR/compose.yml.download"
curl -fsSL "$COMPOSE_URL" -o "$tmp" || { rm -f "$tmp"; stop "FreeHand's settings file couldn't be downloaded. Check the internet connection, then run this again."; }
grep -q 'ghcr.io/tracysmithconsulting/freehand' "$tmp" || { rm -f "$tmp"; stop "the settings file that was downloaded isn't FreeHand's, so it wasn't used."; }
mv "$tmp" "$HOME_DIR/compose.yml"

hex() { od -An -tx1 -N32 /dev/urandom | tr -d ' \n'; }
b64() { head -c 32 /dev/urandom | base64 | tr -d '\n'; }

# Made once and kept: changing them later would lock FreeHand out of its
# own connections.
if [ ! -s "$HOME_DIR/.env" ]; then
    (
        umask 077
        {
            printf 'OC_ADMIN_TOKEN=%s\n' "$(hex)"
            printf 'OC_RUNTIME_TOKEN=%s\n' "$(hex)"
            printf 'OC_ENCRYPTION_KEY=%s\n' "$(b64)"
            printf 'FREEHAND_LLM_PROXY_TOKEN=%s\n' "$(hex)"
            printf 'BRIDGE_SECRET=%s\n' "$(hex)"
        } > "$HOME_DIR/.env"
    )
fi
chmod 600 "$HOME_DIR/.env"

# ── Download and start ──────────────────────────────────────────────

# Says it's still going every minute while a long step works.
beat() {
    local message="$1"
    ( while sleep 60; do say "  $message"; done ) &
    BEAT=$!
}
unbeat() { kill "$BEAT" 2>/dev/null || true; wait "$BEAT" 2>/dev/null || true; }

say ""
say "Downloading FreeHand: about 1.8 GB the first time (much less for an update), taking about 6 GB of space. On a slow connection this can take an hour or more."
beat "Still downloading..."
if ! compose pull --quiet; then unbeat; stop "FreeHand couldn't be downloaded. Check the internet connection, then run this again."; fi
unbeat

say "Starting FreeHand. The first time can take a few minutes."
beat "Still starting..."
if ! compose up -d; then unbeat; stop "FreeHand didn't start. 'docker compose logs' in $HOME_DIR says why."; fi
unbeat

answering=""
for _ in $(seq 1 100); do
    if curl -fsS -o /dev/null "$ADDRESS/api/page-version" 2>/dev/null; then answering=yes; break; fi
    sleep 3
done

say ""
if [ -n "$answering" ]; then
    say "FreeHand is ready: $ADDRESS"
else
    say "FreeHand started, but isn't answering yet. Give it a minute, then open $ADDRESS"
fi
say "Open that address in your browser. The first time, FreeHand helps you choose how it thinks."
say ""
say "  To update FreeHand: run the same install line again. Your data stays."
say "  To stop it:  cd $HOME_DIR && $DOCKER compose down"
say "  To start it: cd $HOME_DIR && $DOCKER compose up -d"
say "  It starts by itself when the computer starts."

# Open it, when there's a desktop and this isn't the administrator.
if [ -n "$answering" ] && [ "$(id -u)" -ne 0 ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$ADDRESS" >/dev/null 2>&1 || true
fi

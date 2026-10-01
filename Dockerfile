# FreeHand — multi-stage Python 3.11 build
#
# Stage 1 (builder): install Python deps into a venv we can copy.
# Stage 2 (runtime): copy the venv + source, run as a non-root user
#                    (UID/GID configurable via build-args), expose :8000.
#
# Two persistent host paths the container needs at runtime:
#   1. vault/   — encryption key, connections DB, broker config.
#                 Mount as a volume; permissions are managed by the
#                 container's `chown -R freehand:freehand /app/vault`.
#   2. agent.db — the SQLite FTS5 database for memories. FreeHand
#                 reads DB_PATH = <project_root>/agent.db (resolved
#                 relative to the source file). On the host, this is
#                 the project root; in the container, it's /app/agent.db.
#                 Mount the host's agent.db into the container at
#                 /app/agent.db, OR consolidate it into the vault in a
#                 future round.
#
# The agent.db bind-mount is the one place host-vs-container ownership
# matters. If the host file is owned by a different UID, the
# `freehand` user inside the container can't open it for write.
# Workarounds (any one of these):
#   - Pass --build-arg UID=$(id -u) --build-arg GID=$(id -g) so the
#     in-container freehand user matches your host user
#   - Run with `docker run --user $(id -u):$(id -g)` to override
#   - chmod 666 the host agent.db before mounting (last resort)
# The compose file (compose.yml) handles this automatically via
# user: "${UID:-1000}:${GID:-1000}".
#
# Standalone usage (single container, no OpenConnector):
#   docker build --build-arg UID=$(id -u) --build-arg GID=$(id -g) \
#     -t freehand:dev .
#   docker run --rm -p 8765:8000 \
#     -v "$(pwd)/vault:/app/vault" \
#     -v "$(pwd)/agent.db:/app/agent.db" \
#     freehand:dev
#
# Joint FreeHand + OpenConnector:
#   docker compose up -d

# ── Stage 1: builder ────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build deps. fastapi/uvicorn/typer/aiohttp/apscheduler don't
# need compilation, but `cryptography` (Fernet) does on some platforms.
# Add `gcc` + `libffi-dev` defensively so the build is portable.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy ONLY the project metadata first — this layer caches when only
# source files change.
COPY pyproject.toml ./

# Install runtime deps + the package itself. `--no-cache-dir` keeps
# the layer small. The package isn't installed editable — the image
# IS the deploy artifact.
RUN pip install --no-cache-dir .

# ── Stage 2: runtime ───────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Build-args let the host's UID/GID match the in-container freehand
# user, so bind-mounted agent.db is writable. Default 1000 for a
# typical Linux desktop user; override on the build command with
# --build-arg UID=$(id -u) --build-arg GID=$(id -g).
ARG UID=1000
ARG GID=1000

WORKDIR /app

# Create the freehand user with the host's UID/GID. `--system` keeps
# the user out of login tables. uvicorn doesn't need root, and a
# FastAPI app running as root is a security smell.
RUN groupadd --system --gid ${GID} freehand \
    && useradd --system --uid ${UID} --gid freehand --home /app freehand

# Copy the installed Python environment from the builder stage.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy the FreeHand source. The vault/ directory is intentionally NOT
# copied — it's mounted as a volume at runtime so encryption.key,
# connections.db, and broker_config.json persist across container
# restarts.
COPY --chown=freehand:freehand . /app/

# Ensure the vault dir exists with the right ownership. The host mount
# overrides this at runtime, but it makes `docker run` without `-v`
# produce a sensible (if empty) vault state instead of a crash.
RUN mkdir -p /app/vault && chown -R freehand:freehand /app/vault

USER freehand

EXPOSE 8000

# uvicorn config — bind to all interfaces inside the container so
# `docker run -p 8000:8000` exposes it. --no-access-log keeps the
# container logs quieter (compose logs only show app prints + uvicorn
# errors); turn it off if you want request-level visibility.
#
# `--workers 1` because FreeHand uses SQLite (no shared-state across
# processes) and writes `vault/api_key` on first boot (single-writer).
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--workers", "1"]

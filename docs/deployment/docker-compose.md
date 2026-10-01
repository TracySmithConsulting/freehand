# FreeHand deployment — Docker Compose

> **Status:** Round 9 (Oct 2026). `docker compose up` brings up FreeHand + OpenConnector together with healthchecks on a fresh host.

This document covers the local-dev / single-host deployment. Production deployment on a Tailscale box or VPS is structurally similar but needs additional hardening (real tokens, external Postgres for OC, TLS termination, vault encryption key management) — out of scope here.

## What you get

A single `docker compose up` brings up two services:

| Service | Image | Port | Purpose |
|---|---|---|---|
| `open-connector` | `ghcr.io/oomol-lab/open-connector:latest` | 3001 | Node runtime that hosts 1500+ provider OAuth configs (Round 7 tier-5 fallback) |
| `freehand` | Local Dockerfile build | 8000 | Python FastAPI app |

Both bind to `127.0.0.1` only — no LAN exposure. To expose on a tailnet or VPS, change the `ports:` lines in `compose.yml` from `127.0.0.1:PORT:PORT` to `PORT:PORT`.

## Quick start

```bash
# 1. Clone FreeHand
git clone https://github.com/TracySmithConsulting/freehand.git
cd freehand

# 2. Create the env file with your tokens (defaults are placeholders)
cp .env.example .env
# Edit .env if you need real OC tokens (see "Environment variables" below)

# 3. Make sure the host's agent.db is writable (Windows hosts only)
chmod 666 agent.db

# 4. Bring up the stack
docker compose up -d --build

# 5. Verify both services are healthy
docker compose ps

# 6. Tail logs from both
docker compose logs -f

# 7. Stop (vault volume preserved)
docker compose down

# 8. Stop AND wipe the vault (data loss)
docker compose down -v
```

After step 5 you should see:

```
NAME                     STATUS                    PORTS
freehand-app              Up (healthy)              127.0.0.1:8000->8000/tcp
freehand-open-connector   Up (healthy)              127.0.0.1:3001->3001/tcp
```

## Service map

```
┌──────────────────────────────────────────────────────────────────┐
│  Docker host                                                     │
│                                                                  │
│  ┌──────────────────────────────┐    ┌──────────────────────┐   │
│  │ open-connector (Node)        │    │ freehand (Python)     │   │
│  │ :3001 → :3001                │◀──▶│ :8000 → :8000         │   │
│  │                              │    │                       │   │
│  │ Healthcheck: wget /health    │    │ Healthcheck: GET /    │   │
│  └────────────┬─────────────────┘    └──────────┬────────────┘   │
│               │                                  │               │
│               ▼                                  ▼               │
│  ┌────────────────────────┐     ┌────────────────────────────┐  │
│  │ oc-data (anon volume)  │     │ ./vault  (bind mount)     │  │
│  │ OC's SQLite store      │     │ encryption.key             │  │
│  │                        │     │ connections.db             │  │
│  │                        │     │ broker_config.json         │  │
│  └────────────────────────┘     └────────────────────────────┘  │
│                                                                  │
│  FreeHand ALSO bind-mounts: ./agent.db → /app/agent.db            │
└──────────────────────────────────────────────────────────────────┘
```

## Volume mounts

| Host path | Container path | Owner | Purpose |
|---|---|---|---|
| `./vault` | `/app/vault` | `freehand:freehand` | FreeHand's encrypted vault — Fernet key, connection tokens, broker config |
| `./agent.db` | `/app/agent.db` | `freehand:freehand` (target) | FreeHand's SQLite FTS5 DB for memories |
| (anonymous) | `/data` (in OC) | OC's runtime user | OC's own SQLite store |

**The `./vault` and `./agent.db` mounts are bind-mounted from the host — your existing data survives `docker compose down/up`.** OC's `/data` is an anonymous Docker volume (named `freehand-oc-data`) that survives `down` but is removed by `down -v`.

## Environment variables

`compose.yml` reads from `.env` (gitignored). `.env.example` shows the full template:

```bash
OC_ADMIN_TOKEN=admintest123
OC_RUNTIME_TOKEN=runttest123
OC_ENCRYPTION_KEY=this-is-a-thirty-two-byte-test-key
UID=1000
GID=1000
FREEHAND_USER=0:0   # Windows compatibility default
```

**Production hardening** (recommended before exposing to a network):

```bash
# Real OC tokens — generate via:
openssl rand -hex 32   # → OC_ADMIN_TOKEN, OC_RUNTIME_TOKEN
openssl rand -base64 32  # → OC_ENCRYPTION_KEY (must be exactly 32 bytes)

# Real FreeHand vault key — generate via:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Seed vault/encryption.key with the output BEFORE first compose-up.

# Match your host's UID/GID (Linux only):
UID=$(id -u) GID=$(id -g) docker compose up
FREEHAND_USER=${UID}:${GID}   # add to .env so compose runs FreeHand non-root
```

## Healthchecks

| Service | Check | Expected |
|---|---|---|
| `open-connector` | `wget --spider http://127.0.0.1:3001/health` | 200 OK |
| `freehand` | `python -c "urllib.request.urlopen('http://127.0.0.1:8000/').read()"` | HTML index page |

If `freehand` is stuck in `Restarting`, check the logs:
```bash
docker compose logs freehand
```
The most common cause is the bind-mount `agent.db` being read-only — see "Known issue: Windows bind-mount UID mapping" below.

## Smoke test (verifies the stack after every change)

```bash
# Confirm both services healthy
docker compose ps

# Confirm OC catalog probe works through FreeHand
docker compose exec freehand curl -s http://open-connector:3001/v1/providers \
  -H "Authorization: Bearer runttest123" | python -c "import json,sys; print(len(json.load(sys.stdin).get('data',[])))"

# Confirm the 5-service matrix against the running compose stack:
API_KEY=$(python -c "import json; print(json.load(open('vault/settings.json'))['api_key'])")

for svc in google slack hackernews linear myspace; do
  printf "%-12s → " "$svc"
  curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 \
      "http://localhost:8000/api/integrations/${svc}/authorize?label=tracy" \
      -H "X-API-Key: $API_KEY"
done

# Expected:
# google     → 200 (tier-1 broker legacy JSON path)
# slack      → 200 (Round 9 tier-1b + SlackConnector)
# hackernews → 302 (Round 7 tier-5 OC redirect)
# linear     → 302 (Round 7 tier-5 OC redirect)
# myspace    → 503 (Round 8 honest-option-A fallback with pre-filled GitHub URL)
```

## Known issue: Windows bind-mount UID mapping

Docker Desktop on Windows uses **gRPC-FUSE** for bind mounts and doesn't propagate host UID mappings to the container cleanly. The result: even when the host's `agent.db` is `chmod 666`, the in-container `freehand` user can't write to it.

**Symptom:** `freehand-app` is stuck in `Restarting (N)` status. Logs show:

```
File "/app/core/database.py", line 12, in init_db
    conn.execute("PRAGMA journal_mode=WAL")
sqlite3.OperationalError: attempt to write a readonly database
```

**Workaround (default):** `compose.yml` sets `FREEHAND_USER=0:0` by default, which makes FreeHand run as root inside the container. Root can write to anything, so the bind-mount writable issue is bypassed. The trade-off: FreeHand is no longer running as a non-root user inside the container.

**Proper fix (Round 10 candidate):** move `agent.db` into `./vault/` and bind-mount `./vault` only. Single mount, ownership set by the container's `chown -R freehand:freehand /app/vault` line. No UID-mapping issue, non-root works.

**Linux hosts and macOS:** not affected. UID mapping propagates cleanly. Override `FREEHAND_USER=${UID}:${GID}` in `.env` for non-root.

## Known issue: OC's `requestedScopes` is global

When you `PUT /api/oauth/configs/<service>` with `requestedScopes: [...]`, OC stores that override for ALL users of this OC instance. If you ever need to wire a service with a different scope set for a different purpose (e.g. test Slack with `chat:write` only vs. production Slack with all 5 scopes), you'd need a separate OC instance per scope profile. For now, one OC = one scope set per service.

## Backup and restore

**Backup** (vault + agent.db are the things to preserve):

```bash
# Stop the stack first (clean snapshot)
docker compose down

# Copy the vault and agent.db to a backup location
tar -czf freehand-vault-$(date +%Y%m%d).tar.gz vault/ agent.db
```

**Restore**:

```bash
# Stop the stack
docker compose down

# Wipe the current state (CAREFUL — this is destructive)
rm -rf vault/* agent.db

# Extract the backup
tar -xzf freehand-vault-YYYYMMDD.tar.gz

# Bring up the stack — FreeHand picks up the restored state
docker compose up -d
```

**OC's own data** (`/data` inside the OC container, anonymous Docker volume `freehand-oc-data`): contains Slack token records, GitHub OAuth configs, etc. Not currently backed up by this process — pull it via `docker compose exec open-connector ...` if you need it.

## When NOT to use

- **Production with multiple users**: compose brings up a single FreeHand instance with shared state. If you need per-user isolation, the per-user tier-1 broker overrides already cover that — but the host vault is still shared. Real multi-user wants FreeHand per user behind a reverse proxy.
- **High availability**: single-instance compose has zero HA. For real HA, Kubernetes or Nomad.
- **Internet exposure**: this compose binds to `127.0.0.1` only. For Tailscale / reverse-proxy / HTTPS, additional reverse-proxy config (Caddy / nginx / Traefik) is needed.

## Related

- `docs/integrations/shared-apps.md` — Round 8 tier-1b workflow (`freehand shared-app add ...`)
- `docs/integrations/open-connector.md` — Round 7 OC bring-up (alternative to direct broker)
- `docs/integrations/slack-oauth-setup.md` — Slack-portal walkthrough (per-service OAuth app creation)
- `references/open-connector-runtime.md` (in the `freehand-agent-development` skill) — Detailed OC runtime quirks, auth-scope table, MCP canonical Accept header
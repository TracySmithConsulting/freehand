# FreeHand — Round 9: Slack connector + Docker Compose

> **For Hermes:** Plan only. Do not execute until Tracy signs off.
> Reload `freehand-agent-development` skill before any implementation turn — every OAuth/broker change deserves the 4-round review discipline.

**Goal:** Two deliverables in one round.
- **(a) Slack connector** — add `core/oauth/providers/slack.py` and register in `core/oauth/providers/__init__.py`. This closes the gap Round 8 flagged: tier-1b broker works end-to-end only for services with a FreeHand connector. With this in place, `freehand shared-app add slack --client-id ... --client-secret ... --scopes ...` actually completes the dance end-to-end through FreeHand (no OC needed at runtime for Slack).
- **(c) Docker Compose** — ship a `compose.yml` that runs FreeHand + OpenConnector together with healthchecks on one command. Closes the operational hygiene punted from Round 8.

**User requirement (carried from Round 8):** Tracy's stated goal was *"I want it to be as easy as possible for people to connect their apps."* Round 8 made that promise for any service OC knows. Round 9 (a) makes it real for Slack without needing OC, and (c) makes the whole stack launchable on a fresh machine in one command.

**Architecture:**

- **(a)** Slack connector follows the same shape as `google.py` / `microsoft.py` / `zoom.py`: a class with `authorize_url()`, `handle_callback()`, and `test()` methods. Credentials fetched via `broker.get_client_credentials("slack")` so tier-1b just works. The connector only handles the OAuth dance; the user-facing tier-1b / tier-5 routing logic stays in the broker + router (no changes there).
- **(c)** `compose.yml` brings up three services: `freehand` (Python uvicorn on :8000), `open-connector` (Node on :3001, using the official `ghcr.io/oomol-lab/open-connector` image per Round 7's skill reference — actually checked in the OC repo), and a shared volume for `vault/`. Healthchecks on each so Docker Compose can wait for readiness. `bootstrap.sh` (or compose-managed init) handles the encryption.key migration — first-run users get one generated.

**Tech Stack:** Python 3.11+, FastAPI (already in tree), Slack Web API via `aiohttp` (already in tree), OpenConnector via Docker image (no new runtime deps), Docker Compose v2.

---

## Why this shape (and not the alternatives)

| Alternative | Why we reject it |
|---|---|
| **Make SlackConnector call OC at runtime** | Defeats the point of tier-1b. If FreeHand has the credentials, FreeHand should call Slack directly. |
| **Generic "external OAuth" connector** that any service plugs into | Looks DRY but breaks down on per-provider quirks (Slack's `v2_user.access` vs Google's token endpoint, Slack's `team` param, etc.). Per-service connectors is the existing pattern; stay consistent. |
| **Auto-generate SlackConnector from OC's runtime introspection** | OC's runtime holds tokens, not metadata about how to call OAuth providers. Would still need per-service code. Punt. |
| **Skip (a), ship only (c)** | Leaves the headline Round 8 gap unaddressed. The "Slack doesn't have a FreeHand connector" gap is the most visible piece of unfinished business. |
| **Skip (c), ship only (a)** | Operational hygiene is small but real — fresh-machine install is currently "clone two repos, install Python deps, install npm deps, set env vars, start both." One command is the right shape. |
| **Ship Dockerfiles for FreeHand + OC, no compose** | Two commands is barely better than today. Compose gives healthchecks + dependency ordering + single shutdown. |

**What we keep:** the 5-tier broker Round 8 shipped. **What we add:** SlackConnector + compose.yml + smoke tests for both.

---

## Phase 0 — Foundational decisions (must be locked before Phase 1)

**0.1 Slack connector scope**

The connector handles ONLY the OAuth dance for Slack:
- `authorize_url(state, redirect_uri)` — builds `https://slack.com/oauth/v2_user/authorize?...` with client_id, redirect_uri, scope, state
- `handle_callback(code, state, redirect_uri)` — POST to `https://slack.com/api/oauth.v2.user.access` to exchange code for token, parse the response (Slack's response shape is `{"ok": true/false, "access_token": "...", ...}` — need to handle `ok: false` explicitly)
- `test(token_data)` — call `auth.test` to verify the token still works

**The connector does NOT** handle:
- Sending messages (`chat.postMessage`) — that's an agent action, out of scope. Per the Round 8 footer, auto-registering OC actions as FreeHand tools is a separate Round 10+ candidate.
- Refreshing tokens — Slack user tokens don't expire, so no refresh needed.

**0.2 OAuth user-token vs bot-token clarification**

Slack has TWO token flavors after the OAuth dance:
- **Bot User OAuth Token** (`xoxb-...`) — for actions as the app's bot user. Scoped by Bot Token Scopes.
- **User OAuth Token** (`xoxp-...`) — for actions as the installing user. Scoped by User Token Scopes.

Our `--scopes` flag in `freehand shared-app add` maps to **Bot Token Scopes** because that's what the developer portal exposes on the OAuth & Permissions page. The dance returns a Bot token (`xoxb-...`) — the agent acts AS THE BOT, not as Tracy. This is the right default for an agent (doesn't need Tracy's personal permissions), and matches what the Round 8 Slack live smoke produced (`accountId: U0C56R6LTD4` is the bot user ID, grantedScopes matches the requested 4 scopes).

**0.3 Connector registry update**

`core/oauth/providers/__init__.py` adds `"slack": SlackConnector` to the `CONNECTORS` dict. This is one of the new places the Slack connector becomes live — without this registration, `get_connector("slack")` still returns None.

**0.4 Compose service topology**

Three services, one shared network:
- `open-connector` (Node, ghcr.io/oomol-lab/open-connector image, port 3001, healthcheck on `/health`)
- `freehand` (Python, local Dockerfile build, port 8000, depends_on open-connector healthcheck, env wired)
- (No `db` service — FreeHand uses SQLite at `vault/connections.db`, mounted as a volume)

The `freehand` service has `depends_on: open-connector: condition: service_healthy` so it doesn't start until OC's `/health` returns 200.

**0.5 Environment variables shipped by compose**

`compose.yml` passes these env vars to FreeHand (matching Round 7's recipe):
- `OOMOL_CONNECT_BASE_URL=http://open-connector:3001` (the compose service name, not `localhost`)
- `OOMOL_CONNECT_RUNTIME_TOKEN=<from .env file>`
- `OOMOL_CONNECT_ADMIN_TOKEN` is admin-only and stays out of FreeHand's env (FreeHand uses runtime token for `/v1/providers`, admin token is for OC's admin endpoints)

`OOMOL_CONNECT_ENCRYPTION_KEY` — OC needs this to start. Ship a `.env.example` with a placeholder and instructions to generate a real one for production.

**0.6 What we deliberately do NOT do in this round**

- No chat.postMessage / agent actions (Round 10+)
- No Notion / Linear connectors (separate PRs when needed)
- No docker image push to a registry (just local compose)
- No production deployment guides (compose is dev/local)
- No migration of existing FreeHand installs to compose

---

## Files likely to change

| File | Change |
|---|---|
| `core/oauth/providers/slack.py` (NEW) | `SlackConnector` class with `authorize_url`, `handle_callback`, `test`. ~120 lines + docstring + comments. |
| `core/oauth/providers/__init__.py` (MODIFY) | Add `SlackConnector` to `CONNECTORS` dict. One line. |
| `tests/test_slack_connector.py` (NEW) | TDD suite: authorize URL shape, handle_callback success/error paths, test() success, broker integration (tier-1b picks up Slack credentials). ~10 tests. |
| `Dockerfile` (NEW) | Multi-stage Python 3.11-slim build for FreeHand. Installs deps, copies code, runs uvicorn. |
| `compose.yml` (NEW) | Three services: open-connector (image), freehand (build), with healthchecks and dependency ordering. |
| `.env.example` (NEW) | Template for OOMOL_CONNECT_* env vars. Generator note for production. |
| `.dockerignore` (NEW) | Exclude `.git`, `__pycache__`, `vault/`, `.pytest_cache`, `node_modules`, `*.db`. |
| `docs/deployment/docker-compose.md` (NEW) | How to bring up the stack: `docker compose up`, what services come up, port mapping, health checks, how to verify. |
| `CHANGELOG.md` (MODIFY) | New entry under "Unreleased": Round 9 — Slack connector + Docker Compose. |

---

## Tasks

### Task 1: Write failing test for SlackConnector

**Objective:** Lock in the contract before writing any Slack code.

**Files:**
- Create: `tests/test_slack_connector.py`

**Step 1:** Write failing tests covering:
- `authorize_url` produces a valid Slack OAuth URL with client_id, redirect_uri, scope, state
- `authorize_url` URL-encodes special characters in scope (commas → %2C) per Slack's spec
- `handle_callback` POSTs to `https://slack.com/api/oauth.v2.user.access` with form-encoded body (Pitfall 16 — pass dict, not string)
- `handle_callback` parses `{"ok": true, "access_token": "xoxb-..."}` correctly
- `handle_callback` raises on `{"ok": false, "error": "invalid_code"}` (Pitfall 19-style: surface Slack's error, don't swallow)
- `test(token_data)` calls `auth.test` and returns True/False based on `ok` field
- Broker integration: `get_connector("slack")` returns a `SlackConnector` instance (after Task 2)
- Tier-1b integration: with a shared Slack app in broker_config, the connector's `_get_credentials()` returns the shared credentials

**Step 2:** Run, see FAIL with "No module named core.oauth.providers.slack" / "Provider not implemented: slack".

### Task 2: Implement SlackConnector

**Objective:** Per-service OAuth connector following the existing pattern (Google is the reference).

**Files:**
- Create: `core/oauth/providers/slack.py`

**Step 1:** Module docstring naming the self.methods called.

**Step 2:** Implement:
```python
class SlackConnector:
    name = "Slack"
    icon = "..."
    supports_multi_account = True
    uses_password = False
    
    scopes_read = ["channels:read", "users:read", "im:history"]
    scopes_write = ["chat:write", "im:write"]
    
    def _get_credentials(self) -> tuple[str, str]:
        # Same pattern as google.py — call broker, get tier-1b
        from core.oauth.broker import get_client_credentials
        client_id, client_secret, _source = get_client_credentials("slack")
        return client_id or "", client_secret or ""
    
    def authorize_url(self, state: str, redirect_uri: str) -> str:
        client_id, _ = self._get_credentials()
        scopes = " ".join(self.scopes_read + self.scopes_write)
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
        })
        return f"https://slack.com/oauth/v2_user/authorize?{params}"
    
    async def handle_callback(self, code: str, state: str, redirect_uri: str) -> dict:
        client_id, client_secret = self._get_credentials()
        async with aiohttp.ClientSession() as session:
            # Pitfall 16: pass dict, not pre-encoded string
            async with session.post(
                "https://slack.com/api/oauth.v2.user.access",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
            ) as resp:
                payload = await resp.json()
                # Slack returns {"ok": false, "error": "..."} on failure.
                # Surface that explicitly rather than letting "access_token"
                # missing silently fail downstream.
                if not payload.get("ok"):
                    raise RuntimeError(
                        f"Slack token exchange failed: {payload.get('error', 'unknown')}"
                    )
                return {
                    "access_token": payload["access_token"],
                    "scope": payload.get("scope", ""),
                    "team_id": payload.get("team", {}).get("id"),
                    "team_name": payload.get("team", {}).get("name"),
                    "bot_user_id": payload.get("bot_user_id"),
                    "expires_in": None,  # Slack user tokens don't expire
                }
    
    async def test(self, token_data: dict) -> bool:
        access_token = token_data.get("access_token")
        if not access_token:
            return False
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as resp:
                payload = await resp.json()
                return bool(payload.get("ok"))
```

**Step 3:** Register in `core/oauth/providers/__init__.py`:
```python
from .slack import SlackConnector
# ...
CONNECTORS = {
    "google": GoogleConnector,
    "microsoft": MicrosoftConnector,
    "zoom": ZoomConnector,
    "facebook": FacebookConnector,
    "instagram": InstagramConnector,
    "github": GitHubPATConnector,
    "email": EmailConnector,
    "slack": SlackConnector,  # NEW — Round 9
}
```

**Step 4:** Run Task 1's tests. Expect PASS.

### Task 3: Wire Slack connector to broker tier-1b (live verification)

**Objective:** End-to-end verification: register a Slack shared app via `freehand shared-app add`, then `GET /api/integrations/slack/authorize` returns a real Slack authorize URL (not 501).

**Files:** None (live test only).

**Step 1:** With OC stopped, FreeHand running with a Slack shared app registered, hit `/api/integrations/slack/authorize?label=test`. Expect 200 JSON with `authorize_url` containing `slack.com/oauth/v2_user/authorize?...` (SlackConnector built the URL using tier-1b creds).

**Step 2:** Complete the dance in a browser. Expect Slack consent screen, click Allow, expect FreeHand's callback to store the token in `vault/connections.db`.

**Step 3:** Verify with `freehand connections` — slack connection appears with the granted scopes.

**Step 4:** Commit. `git add core/oauth/providers/slack.py core/oauth/providers/__init__.py tests/test_slack_connector.py`

```bash
git commit -m "feat(oauth): SlackConnector for tier-1b end-to-end support"
```

### Task 4: Write Dockerfile for FreeHand

**Objective:** Containerize FreeHand so compose can run it.

**Files:**
- Create: `Dockerfile`

**Step 1:** Multi-stage Python 3.11-slim build. Stages:
- `builder` — install deps into a venv, copy source
- `runtime` — copy the venv + source, set non-root user, expose :8000, run uvicorn

**Step 2:** Pin Python version (`python:3.11-slim`), use `--no-cache-dir` for pip. Copy `pyproject.toml` and install via `pip install .` (not editable — the image is the deploy artifact).

**Step 3:** `.dockerignore` excludes `.git`, `__pycache__`, `.pytest_cache`, `vault/` (mounted as volume), `*.db`, `node_modules`, `dist`, `build`, `freehand-live.log`, `three-sound-draft-1.md`, `.hermes/`.

**Step 4:** Smoke test the image locally: `docker build -t freehand:dev . && docker run --rm -p 8765:8000 -v "$(pwd)/vault:/app/vault" freehand:dev` and curl `localhost:8765/` for 200. This is operational, no commit gate beyond "image builds + starts".

### Task 5: Write compose.yml + .env.example

**Objective:** One-command bring-up of FreeHand + OC + shared vault.

**Files:**
- Create: `compose.yml`
- Create: `.env.example`

**Step 1:** `compose.yml` shape:

```yaml
services:
  open-connector:
    image: ghcr.io/oomol-lab/open-connector:latest
    container_name: freehand-open-connector
    ports:
      - "127.0.0.1:3001:3001"
    environment:
      - PORT=3001
      - OOMOL_CONNECT_ADMIN_TOKEN=${OC_ADMIN_TOKEN:-admintest123}
      - OOMOL_CONNECT_RUNTIME_TOKEN=${OC_RUNTIME_TOKEN:-runttest123}
      - OOMOL_CONNECT_ENCRYPTION_KEY=${OC_ENCRYPTION_KEY:-this-is-a-thirty-two-byte-test-key}
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:3001/health"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 20s

  freehand:
    build: .
    container_name: freehand-app
    ports:
      - "127.0.0.1:8000:8000"
    depends_on:
      open-connector:
        condition: service_healthy
    environment:
      - OOMOL_CONNECT_BASE_URL=http://open-connector:3001
      - OOMOL_CONNECT_RUNTIME_TOKEN=${OC_RUNTIME_TOKEN:-runttest123}
    volumes:
      - ./vault:/app/vault
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/').read()"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 15s
```

Notes:
- `127.0.0.1:3001:3001` (not `0.0.0.0`) — loopback only (Round 8's decision: "if FreeHand is later reachable over Tailscale, OpenConnector stays on loopback unless explicitly opted in")
- `volumes: ./vault:/app/vault` — host vault mounted into container so `vault/connections.db`, `vault/encryption.key`, `vault/broker_config.json` survive container restarts
- Healthchecks on both — compose waits for OC before starting FreeHand (the recipe in Round 7)

**Step 2:** `.env.example`:
```
# OpenConnector admin token (used for OC's admin endpoints only)
OC_ADMIN_TOKEN=admintest123

# OpenConnector runtime token (FreeHand uses this for /v1/providers catalog probe)
OC_RUNTIME_TOKEN=runttest123

# OpenConnector encryption key (must be 32 bytes)
# For PRODUCTION: generate a real key, e.g. `openssl rand -base64 32`
OC_ENCRYPTION_KEY=this-is-a-thirty-two-byte-test-key
```

**Step 3:** Add `compose.yml` and `.env.example` to the repo (NOT `.env` — that's gitignored).

**Step 4:** Commit.

### Task 6: Joint-boot smoke test (compose)

**Objective:** Verify `docker compose up` brings up both services healthy, and the existing 5-service matrix passes.

**Files:** None (live verification).

**Step 1:** `docker compose up -d` from the repo root.

**Step 2:** Wait for both healthchecks (the FreeHand container depends on OC healthy, so it auto-waits).

**Step 3:** Verify:
- `docker compose ps` — both services "Up (healthy)"
- `curl http://127.0.0.1:3001/health` → 200
- `curl http://127.0.0.1:8000/` → 200
- `curl /api/integrations/slack/authorize` → 200 JSON with real Slack OAuth URL (Task 3's deliverable, now live via compose)
- `curl /api/integrations/hackernews/authorize` → 302 to OC console (Round 7 path, still works)

**Step 4:** `docker compose down -v` (the `-v` clears the named volumes — important for clean smoke).

**Step 5:** Document the recipe in `docs/deployment/docker-compose.md`.

### Task 7: Document + CHANGELOG

**Objective:** End-user and contributor docs + CHANGELOG entry.

**Files:**
- Create: `docs/deployment/docker-compose.md`
- Modify: `CHANGELOG.md`

**Step 1:** `docs/deployment/docker-compose.md` covers:
- Prerequisites: Docker Desktop with Compose v2
- Quick start: `cp .env.example .env`, `docker compose up -d`, `docker compose logs -f`
- Service topology diagram (text)
- Health checks: what they probe, expected timing
- How to add a shared Slack app via `docker compose exec freehand python cli.py shared-app add slack ...`
- Backup and restore: vault is mounted as volume, `docker compose down` doesn't lose data
- Production hardening notes (rotate `OC_ENCRYPTION_KEY`, use real `OC_*_TOKEN` values, consider external Postgres for OC)

**Step 2:** CHANGELOG entry:
```
## [Unreleased] — 2026-10-01
### Added — Round 9: Slack connector + Docker Compose

- `core/oauth/providers/slack.py` — SlackConnector class. Round 9 closes
  the tier-1b-for-new-services gap: Slack can now be registered via
  `freehand shared-app add slack` and the OAuth dance completes through
  FreeHand (no OC needed at runtime for Slack). Token exchange against
  `oauth.v2.user.access` uses dict body (Pitfall 16 — aiohttp
  Content-Type trap). Surfaces Slack's `{"ok": false, "error": "..."}`
  response explicitly rather than swallowing it.
- `core/oauth/providers/__init__.py` — register `SlackConnector` in
  `CONNECTORS` dict.
- `Dockerfile` — multi-stage Python 3.11-slim build.
- `compose.yml` — FreeHand + OpenConnector + shared vault volume,
  healthchecks, dependency ordering. `docker compose up` brings up the
  whole stack.
- `.env.example` — template for `OOMOL_CONNECT_*` env vars with prod
  hardening notes.
- `.dockerignore` — exclude dev artifacts from the image.
- `docs/deployment/docker-compose.md` — bring-up recipe.

### Notes
- Compose binds to loopback only (`127.0.0.1:3001:3001`,
  `127.0.0.1:8000:8000`). For Tailscale / reverse-proxy / HTTPS
  deployment, opt in via `HOST=0.0.0.0` for OC and FreeHand's
  `--host 0.0.0.0`.
- No chat.postMessage / agent actions (Round 10+).
- Notion / Linear connectors still needed for those services.
- Docker image is local-only; no registry push in this round.
```

**Step 3:** Commit.

```bash
git add Dockerfile compose.yml .env.example .dockerignore docs/deployment/docker-compose.md CHANGELOG.md
git commit -m "feat(deploy): Docker Compose for FreeHand + OpenConnector"
```

---

## Tests / validation

Full suite after each task:

```bash
cd "/c/Users/trace/Documents/Default Project" && \
  python -m pytest tests/ --tb=short -q
```

Expected: 251+ existing tests still pass; new tests in `test_slack_connector.py` add ~10 cases. Tier-1b for Slack works end-to-end through FreeHand.

Live verification before merge:
- Task 3 smoke: Slack OAuth dance completes through FreeHand without OC (with shared app registered, no OC env vars needed)
- Task 6 smoke: `docker compose up` brings up both services healthy; the 5-service matrix from Round 8 still passes against the compose stack
- `.env.example` doesn't ship real secrets; `OC_*_TOKEN` defaults are placeholders

---

## Risks, tradeoffs, open questions

| Risk | Mitigation |
|---|---|
| Slack's `v2_user.access` endpoint shape changes | Pitfall 30 — capture live response, pin fixture. Tests assert on the live shape, not a hand-rolled one. |
| Slack rejects tokens from apps registered with broad scopes | Round 8 ships `requestedScopes` for OC path. The new connector uses the per-service scopes_read + scopes_write class attributes (narrow set). User can override by changing class attributes or by registering different scopes at api.slack.com. |
| Docker Compose image pulls OC from ghcr.io — what if Docker Hub / ghcr is rate-limited? | The official image is `ghcr.io/oomol-lab/open-connector:latest`. Round 7's skill reference documents this. Fallback: use the native Node recipe (clone + npm install). Documented as a "if Docker is not available" path in `docker-compose.md`. |
| Vault volume mounted from host — permissions issues on Linux | Documented in `docker-compose.md` — FreeHand runs as a non-root user; the mounted vault needs to be world-readable OR FreeHand needs to run as a user matching the host UID. Default: `chmod -R a+rX vault/` after first init. |
| `OC_ENCRYPTION_KEY` default in `.env.example` is the test key | Documented clearly: "for production, generate a real key." The test key is fine for local development. |
| Slack's bot-only token (`xoxb-...`) means the agent acts AS THE BOT, not as the user. Some Slack actions require user tokens. | Documented in the connector docstring. Users who need user-context actions (e.g. sending a DM "from" Tracy) need to add `chat:write.users` or similar User Token Scopes. Round 9 ships bot tokens; user tokens are a follow-up if needed. |
| Compose doesn't include OC's Postgres option (uses SQLite by default) | OC defaults to SQLite. Postgres is opt-in. Documented as Round 10+ if needed for production. |

---

## Slicing discipline

Per Round 7 lessons in the `freehand-agent-development` skill: vertical slices, one failure-per-implementation step, commit per slice.

| Task | Slice type | Files touched | Commit |
|---|---|---|---|
| 1 | RED (test only) | `tests/test_slack_connector.py` | none yet |
| 2 | GREEN (connector + registry) | `core/oauth/providers/slack.py`, `core/oauth/providers/__init__.py` | `feat(oauth): SlackConnector for tier-1b end-to-end support` |
| 3 | Live smoke (verification) | none | none |
| 4 | Dockerfile + .dockerignore | `Dockerfile`, `.dockerignore` | `chore(deploy): Dockerfile for FreeHand` |
| 5 | Compose + env template | `compose.yml`, `.env.example` | `feat(deploy): Docker Compose for FreeHand + OpenConnector` |
| 6 | Joint-boot smoke (verification) | none | none |
| 7 | Docs + CHANGELOG | `docs/deployment/docker-compose.md`, `CHANGELOG.md` | same commit as Task 5 |

---

## Open question for Tracy before Task 2

Slack connector's `scopes_read` and `scopes_write` defaults. The agent will request these from Slack's consent screen:

- **`scopes_read`** (read-only actions): `channels:read`, `users:read`, `im:history` — agent can list channels, look up users, read DM history
- **`scopes_write`** (write actions): `chat:write`, `im:write` — agent can post messages to channels and DMs

**Tracy's decision (01 Oct 2026):** include `im:write` (send DMs) in the default scope set. Document the privacy implication in the connector docstring and in `docs/integrations/slack-oauth-setup.md`. Provide a one-line opt-out recipe for users who want bot-messaging but not DM-send: register the Slack app without `im:write` in Bot Token Scopes, then `freehand shared-app add slack --scopes "chat:write,channels:read,users:read,im:history"` (no `im:write`).

The connector's `scopes_read` and `scopes_write` class attributes stay at:
```python
scopes_read = ["channels:read", "users:read", "im:history"]
scopes_write = ["chat:write", "im:write"]
```

The connector's `authorize_url` joins `scopes_read + scopes_write` with a single space (Slack's scope separator) and URL-encodes the result. Slack rejects duplicate scopes, so the union set must be deduplicated — the test pins this.

---

## Provenance / context for future maintainers

Tracy raised this on 01 Oct 2026 after Round 8 closed. Two parallel asks bundled into one round:

1. **Slack connector** — closes the gap Round 8's CHANGELOG explicitly called out as "Round 9 candidate: add per-service Python connectors for tier-1b services." With Slack being the most-requested provider and the only one with an actual live OAuth app registered at api.slack.com, it's the natural first pick. Notion / Linear come later if/when needed.

2. **Docker Compose** — the Round 8 footer punted this ("Docker Compose punted to Round 8c — out of scope here"). Bundled with (a) because both are operationally simple and the operational story matters as much as the feature code.

The shape keeps FreeHand's tier discipline intact: tier-1b works for Slack (Round 9), tier-5 OC works for everything else (Round 7), tier-1 user override still wins (Pitfall-5 invariant). The user requirement "easy way for people to connect their apps" gets one more layer of fulfillment without disturbing the broker contract.

Round 10 candidates (out of scope here):
- Auto-register OC actions as FreeHand tools (Round 7's footer option B)
- Notion / Linear / GitHub connectors (mirror of Round 9 (a))
- Postgres-backed OC for production (replace SQLite)
- OC client library upgrade if the API surface changes
- Rekey flow for `vault/encryption.key` rotation

**Status:** Draft. Awaiting Tracy's sign-off on the Slack connector's default scope set before Task 2.

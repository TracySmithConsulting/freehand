# Changelog

All notable changes to FreeHand will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased] — 2026-10-02

### Added — Round 10 PR 1: Vault consolidation

agent.db moved from project root to vault/agent.db so a single Docker
bind-mount covers ALL persistent state. Round 9 had two mounts
(`./vault` and `./agent.db`) which forced the Windows-only
`FREEHAND_USER=0:0` workaround because Docker Desktop on Windows uses
gRPC-FUSE and doesn't propagate host UID mappings cleanly across
two mounts. Round 10 PR 1 puts agent.db inside `./vault/` — one
mount, container owns the whole thing.

- **`core/database.py`** — `DB_PATH = VAULT_DIR / "agent.db"` (was
  `Path(__file__).parent.parent / "agent.db"`). New
  `migrate_legacy_root_db()` runs once on first app boot (wired into
  `server.py:startup_event`, NOT into `init_db()` — see the comment
  in core/database.py for why). Moves the main file + WAL sidecars
  (`agent.db-wal`, `agent.db-shm`). Idempotent. Safe on empty vault.
  Doesn't overwrite a vault copy the user already populated.
- **`core/memory.py`** — local `DB_PATH` deleted; canonical path
  imported from `core.database`.
- **`server.py`** — local `DB_PATH` binding deleted; canonical path
  imported from `core.database`. All handlers (`get_tasks`,
  `get_approvals`, etc.) now connect to the single canonical
  `vault/agent.db`.
- **`Dockerfile`** — comment updated. Standalone example uses one
  mount: `-v "$(pwd)/vault:/app/vault"`. Build-arg defaults
  flipped to `UID=0 GID=0` (Windows-host compatible) — Round 9's
  default was `1000:1000`; that worked on Linux hosts but
  conflicted with the two-mount design. Round 10 PR 1 + Linux
  hosts can now safely override back to non-root via
  `--build-arg UID=$(id -u)`.
- **`compose.yml`** — `agent.db` bind-mount line removed. Only
  `./vault:/app/vault` remains. Build-arg defaults flip to
  `UID=1000 GID=1000` (Linux non-root default). Windows hosts
  keep `FREEHAND_USER=0:0` as the runtime override.
- **`docs/deployment/docker-compose.md`** — Volume mounts table,
  backup/restore, "Known issue" sections updated to reflect
  the single-mount design.

### Verified live

- **7 new tests in `tests/test_database.py`** — DB_PATH constant
  shape (2 tests), migration moves main + WAL sidecars, idempotent,
  creates vault/ when missing, no-op when nothing to migrate,
  preserves user data when vault already has a copy.
- **275 tests passing, 0 regressions** (was 268 pre-PR).
- **No breaking changes**: every existing handler still works;
  the only test fixture that broke (test_integration.py) was
  relying on `server.py`'s local `DB_PATH` constant being the
  same path as `core.database`'s — that's now consistent.

### Migration story for existing installs

On first boot of a Round-10-Pull-1 image, `migrate_legacy_root_db()`
detects any pre-Round-10 `agent.db` at the project root and moves
it (plus WAL sidecars) into `vault/agent.db`. Idempotent — a
sentinel-style check ensures it never runs twice. **No data loss.**
If the vault copy already exists (user manually moved), the legacy
file is removed to avoid two-divergent-copies risk; the unique user's
vault copy is preserved untouched.

### Added — Round 10 PR 2: Credential store + tool registry

**What changed**: FreeHand now has a first-class, Fernet-encrypted
multi-credential store and an LLM-facing tool registry. Tracy can
register multiple accounts per service (e.g. two GitHub accounts
under labels "work" and "personal") and the LLM sees each as a
distinct tool namespace. OC-discovered actions are auto-classified
by risk and gated behind explicit opt-in for sensitive /
destructive actions.

**Why**: Round 5-9 had two parallel storage paths — vault/connections.db
for OAuth dance results, plus a static set of GitHub-PAT tools in
`core/agent_config.py`. They were per-flow (not per-(service,label))
and the LLM could only see one GitHub account. Round 10 PR 2 unifies
this around a single credential store + a dynamic tool registry that
reads OpenConnector's action catalog.

**New modules**:
- **`core/oauth/credential_store.py`** (304 lines) — Fernet-encrypted
  store at `vault/credential_store.json`. Per-(service, label)
  namespace. Two `auth_type`s: `oauth` (full token_data dict) and
  `api_key` (raw token string). Same Fernet key as `shared_apps.py`
  and `manager.py`. Lock-protected atomic writes.
- **`core/tools/registry.py`** (505 lines) — the bridge between
  OpenConnector's action catalog and FreeHand's LLM-facing tool
  list. Probes OC for a service, classifies actions by risk,
  registers them as `oc_<service>_<label>_<action>` tools.
- **`tests/test_credential_store.py`** (13 tests),
  **`tests/test_credential_cli.py`** (12),
  **`tests/test_migrate_legacy.py`** (9),
  **`tests/test_tool_registry.py`** (14),
  **`tests/test_tools_cli.py`** (9),
  **`tests/test_registry_bootstrap.py`** (5) — 62 new tests total.

**New CLI subcommands**:
- `freehand credential add <service> --token <key> [--label <name>]`
  — register an API key / PAT. Secret Fernet-encrypted on write.
- `freehand credential list` — show service, label, auth_type,
  registered_by for all credentials. **Never the decrypted secret.**
- `freehand credential remove <service> [--label <name>]`
- `freehand credential rename <service> --from <old> --to <new>`
- `freehand tools refresh` — re-probe OC for every credentialed
  service. Adds new actions, drops actions OC no longer exposes.
- `freehand tools list [service]` — show currently-active tools.
- `freehand tools enable-writes <service>` — opt in to sensitive
  / destructive actions (chat:write, chat:delete, channels:history).
- `freehand tools disable <service>` — remove all tools for service.

**Classification rule** (Tracy 02 Oct 2026, refined from live OC probe):
  `risk == "standard"` → auto-register as read tool.
  `risk in {"sensitive", "destructive"}` → behind `enable-writes`.
  Slack's `defaultSelected` is OC's consent-screen UX signal, NOT a
  safety signal. Slack uses `risk=sensitive` for read actions like
  `channels:history` — we don't auto-register those.

**Tool naming**: `oc_<service>_<label>_<action>` always, even for
single-credential services (label="default" is implicit). Uniform
shape for the LLM.

**Schema format**: OpenAI function-calling shape — matches the
existing `TOOL_SCHEMAS` in `core/agent_config.py`. No translation
layer for the dispatch loop.

**Server wiring**:
- `server.py:startup_event()` now calls
  `core.oauth.manager.migrate_legacy_connections_db()` so the
  Round 5-9 OAuth connections in `vault/connections.db` get
  migrated to the new credential store on first boot of a
  Round-10-Pull-2 image.
- `server.py:startup_event()` now calls
  `core.tools.registry.bootstrap()` so OC-discovered tools persist
  across FreeHand restarts. After bootstrap, `list_available_tools()`
  includes them and `intercept_action()` gates them on read/write
  permissions.

**Migration story for existing installs**:
On first boot of a Round-10-Pull-2 image:
1. `migrate_legacy_root_db()` runs (PR 1's job — moves agent.db).
2. `migrate_legacy_connections_db()` reads `vault/connections.db`
   and writes its rows to `vault/credential_store.json` as
   `auth_type="oauth"` entries. Idempotent — sentinel `{"_migrated": true}`
   in credential_store.json gates the run. Skips rows that already
   exist in the new store (preserves newer registrations). The
   legacy `connections.db` file is **NOT deleted** — the legacy
   connections table keeps working until Round 11+ removes it.
3. `registry.bootstrap()` reads `vault/tool_registry.json` and
   injects stored tools into `TOOL_SCHEMAS` / `TOOL_REGISTRY`.
   First boot with no `tool_registry.json` is a no-op (return 0).

**Test status**: **337 tests passing, 2 skipped, 0 regressions**
(was 275 after PR 1; +62 from PR 2).

**No breaking changes**: every existing handler still works. The
new `oc_*` tools coexist with the static `list_github_repos` etc.
tools. Round 11 will rename / re-namespace the dispatch loop to use
the registry exclusively; PR 2 ships the new infrastructure
alongside the old.

### Added — Round 11: Dynamic OC tool dispatch (plumbing only, NOT verified live)

**What changed**: The LLM dispatch loop now has an `oc_` branch that
routes any tool name starting with `oc_` to a new dispatch module
which looks up the credential in `credential_store` and calls
OpenConnector's MCP endpoint. Static tools keep their explicit
branches — Round 11 is additive only.

**What did NOT change**: The actual end-to-end round-trip does
**not** work yet. The plumbing is in place and the unit tests pass,
but the live Slack smoke (intended as the final Task 4 verification)
failed because the wire format FreeHand ships doesn't match OC's
actual MCP interface. See `.hermes/plans/2026-10-05_round-11-live-smoke-discovery.md`
for the full writeup.

**The 3 things Round 11 ships**:

1. **`core/oauth/open_connector.py:get_provider_actions(service, label)`**
   — real catalog probe. GET against `OC /v1/providers/<service>`,
   returns the `authorizationOptions` list. Empty list on 404 / OC
   down / parse error. Closes the stub that Round 10's registry
   depended on (Round 10 tests passed only because they monkeypatched
   the stub).

2. **`core/tools/dispatch.py:dispatch_oc_tool(name, args)`** — parses
   the `oc_<service>_<label>_<action>` triple, looks up the credential
   from `credential_store`, calls OC's MCP endpoint. Uniform
   `{ok, content|error}` envelope. Error codes: `malformed_name`,
   `no_credential`, `oc_unreachable`, `oc_error`, `exception`.

3. **`core/agent.py:execute_tool()`** — one new branch at the end
   of the dispatch chain. Any tool name starting with `oc_` routes
   to `dispatch_oc_tool`. Static tools (`read_docx`, `navigate`,
   `list_github_repos`, etc.) keep their explicit branches.

**The 1 thing Round 11 does NOT do**: the actual Slack round-trip.
The dispatch layer's wire format (Round 11's `call_mcp_action`)
sends `params.name = "<authorizationOptions_id>"`, e.g.
`"channels:read"`. OC's actual MCP interface expects
`params.name = "execute_action"` with the action id nested inside
`arguments.actionId` in `service.action_name` format (e.g.
`"slack.list_channels"`). The live smoke confirmed this with a
real `curl` probe.

**Plus an operational surprise**: OC's catalog now has **1568
providers and 18345 actions** (up from the ~30 I estimated when
writing the Round 10 plan). The Round 12 work needs to switch
from `authorizationOptions[].risk` to the per-action `operationType`
from `search_actions` / `get_action_guide`, and add an action-id
translation table for the wire format mismatch.

**Tests**: 16 new tests across 3 files (4 + 9 + 3).
**353 passed, 2 skipped, 0 regressions** (was 337 after Round 10 PR 2).

**Verified live**: **No.** Discovery writeup in
`.hermes/plans/2026-10-05_round-11-live-smoke-discovery.md` documents
what the live probe of OC revealed. Round 12 will fix the wire
format, register a Slack connection in OC's runtime, and re-run
the smoke.

**No breaking changes**: A user with the new `oc_` tools sees them
in `list_available_tools()` and the LLM can attempt to call them.
If the call fails (which it currently does, due to the wire format
mismatch), the dispatch returns a clean `oc_error` envelope, not a
Python traceback. Round 11 ships additive plumbing; Round 12 fixes
the wire.

---

## [Unreleased] — 2026-10-01

### Added — Round 9: Slack connector + Docker Compose

Tracy's stated goal for this round (closing the gap Round 8's CHANGELOG called out as "Round 9 candidate: add per-service Python connectors for tier-1b services"): Slack gets a FreeHand-native connector that closes the loop end-to-end for tier-1b. With OC, the user clicks Connect Slack → OC's "configure your client first" page. With tier-1b + SlackConnector, the user clicks Connect Slack → FreeHand serves Slack's consent screen directly using its own credentials.

Plus the operational hygiene that Round 8 punted: Docker Compose for FreeHand + OpenConnector, brought up with one command.

- **`core/oauth/providers/slack.py`** — new `SlackConnector` class. Closes the gap Round 8's CHANGELOG flagged as the "Round 9 candidate" tier-1b-for-new-services work. Mirrors the existing `google.py` / `microsoft.py` shape:
  - `_get_credentials()` reads via `broker.get_client_credentials()` so tier-1b shared apps work transparently
  - `authorize_url()` builds `slack.com/oauth/v2_user/authorize` with deduplicated scopes (Slack rejects duplicate scopes with `invalid_scope`)
  - `handle_callback()` POSTs to `oauth.v2.user.access` with a **dict** body (Pitfall 16 — aiohttp Content-Type trap). Surfaces Slack's `{"ok": false, "error": "..."}` response explicitly rather than swallowing.
  - `test()` calls `auth.test`, returns True iff Slack responds `ok=true`
  - `parse_error()` maps common Slack errors to human-readable strings with Pitfall 33 hints
  - Default scope set per Tracy's 01 Oct 2026 decision: `im:write` included for DM-send capability. Opt-out recipe documented in `docs/integrations/slack-oauth-setup.md`.

- **`core/oauth/providers/__init__.py`** — register `SlackConnector` in the `CONNECTORS` dict (8th entry, joining google/microsoft/zoom/facebook/instagram/github/email).

- **`core/oauth/manager.py`** — `save_connection()` now handles `token_data` missing an `expires_in` key (or having it set to `None`). Providers like Slack whose tokens don't expire now use a 10-year far-future `expires_at` instead of crashing the dance. Three regression tests pin the contract.

- **`core/oauth/router.py`** — `ALLOWED_SERVICES` is now derived from `CONNECTORS.keys()` at import time. Adding a new connector automatically extends the allowlist; the Round 8 `slack` oversight is now structurally prevented. Pitfall 41-style existing-test updates: `test_list_integrations` (was 7, now 8), `test_unknown_service_with_oc_known_returns_302` (slack → hackernews), `test_unknown_service_with_oc_down_returns_503` (slack → hackernews).

- **`Dockerfile`** — multi-stage Python 3.11-slim build:
  - `builder` stage installs pip deps + package
  - `runtime` stage copies installed env + source, runs as `freehand:freehand` (UID/GID via build-args, default 0:0 for Windows compatibility), exposes :8000
  - Documents the Windows bind-mount UID-mapping caveat with three workarounds (build-arg, `--user`, chmod)
  - Smoke verified live on Docker Desktop 29.8.1 (Windows)

- **`.dockerignore`** — excludes `vault/`, `agent.db`, `__pycache__`, `node_modules`, build artifacts, IDE noise so the build context is lean and secrets never leak into the image.

- **`compose.yml`** — joint FreeHand + OpenConnector bring-up:
  - Two services: `open-connector` (image pull, no build), `freehand` (local build)
  - Healthchecks on both so FreeHand waits for OC's `/health` before starting
  - Shared vault via `./vault:/app/vault` bind mount; agent.db via `./agent.db:/app/agent.db`
  - OC's own `/data` is an anonymous Docker volume (managed separately from FreeHand's vault)
  - `FREEHAND_USER` env var defaults to `0:0` so compose-up works on Windows hosts; Linux hosts override via `.env` for non-root
  - Syntactic check: `docker compose config --quiet` exits 0

- **`.env.example`** — template for `OOMOL_CONNECT_*` env vars + `UID`/`GID`/`FREEHAND_USER`. Production hardening notes (openssl rand, 32-byte Fernet key constraint).

- **`docs/deployment/docker-compose.md`** (new) — full bring-up guide: quick start, service map, volume mounts, env vars, healthchecks, smoke test, backup/restore, known issues (Windows UID mapping, OC `requestedScopes` globality), when NOT to use.

### Fixed

- **`save_connection` crashes when token has no `expires_in`**: live dance caught `int(None)` raising TypeError, callback returning 500. Now handles `None` (10-year far-future) and missing-key (default 3600s) correctly. Regression-tested with 3 new tests in `tests/test_oauth_broker.py::TestSaveConnectionExpiresInNone`.

- **`ALLOWED_SERVICES` drift from `CONNECTORS`**: was a hand-maintained 7-entry list that missed the new Slack entry. Now derived at import time, structurally can't drift.

### Verified live

**Task 3 — Slack OAuth dance through FreeHand** (no OC involved):

```
freehand shared-app add slack --client-id ... --client-secret ... \
    --scopes "chat:write,channels:read,users:read,im:history,im:write"
GET /api/integrations/slack/authorize?label=tracy
  → 200 JSON with slack.com/oauth/v2_user/authorize URL
  → redirect_uri: http://127.0.0.1:8000/api/integrations/slack/callback
Browser consent → Allow → callback → token exchange → store
  → vault/connections.db shows slack/tracy with accountId U0C56R6LTD4
  → expires_at 2036-09-28 (10-year far-future per the fix)
```

**Task 6 — 5-service matrix against docker compose stack**:

| Service | Path | Result |
|---|---|---|
| `google` | tier-1 broker | 200 JSON |
| `slack` | tier-1b + SlackConnector | 200 JSON |
| `hackernews` | tier-5 OC redirect | 302 to `http://open-connector:3001/...` |
| `linear` | tier-5 OC redirect | 302 to OC |
| `myspace` | none → 503 | `error=no_shared_app`, pre-filled GitHub URL, contact_email |

### Notes

- **Microsoft 365 still routes via first-party broker** (Azure subscription requirement unchanged for that one provider).
- **Slack's hostname-strict redirect_uri**: `localhost` and `127.0.0.1` are different URIs to Slack. Register whichever host:port you actually hit (per Pitfall 11: redirect_uri is built from request.url at runtime).
- **Docker Compose Windows caveat documented**: gRPC-FUSE doesn't propagate host UID mappings cleanly. `FREEHAND_USER=0:0` default + chmod 666 on agent.db are the workarounds; Linux hosts work correctly with the default non-root posture.
- **`agent.db` consolidation into `vault/`** is a Round 10 candidate. Currently mounted as a separate bind volume — the only place where Docker UID mapping bites.
- **No `chat.postMessage` / agent actions**: tier-1b still does NOT auto-register provider actions as FreeHand tools. Round 10+ candidate.
- **Six commits on main**: `091ec78` (SlackConnector + tests), `6ac7f9b` (manager fix), `054a5d8` (Dockerfile + .dockerignore), `a9eb1a3` (ALLOWED_SERVICES refactor + compose.yml + .env.example), `d1ee4a3` (compose Windows compatibility), plus this docs commit.
- **Test count**: 268 passing + 2 skipped = 270 collected, 0 regressions. Was 251 + 2 before Round 9.

## [Unreleased] — 2026-10-01

### Added — Round 8: Shared OAuth broker apps

Tracy's stated goal for this round (30 Sep 2026): *"I want it to be as easy as possible for people to connect their apps. Where I can't do it, we go with option A, we say so honestly, and then they would have to do their own dance, but as far as possible, I'd go with B."* Round 8 is the broker-brokered path: FreeHand holds the shared OAuth credentials in its Fernet-encrypted vault so users get a one-click consent screen instead of the "register your own app" loop.

- **Tier-1b in the broker** (`core/oauth/broker.py`) — new resolution tier between tier-1 (per-user override) and tier-2 (env vars). Returns `(client_id, client_secret, "shared_app")` when `vault/broker_config.json -> shared_apps.<service>` has an entry. **Pitfall-5 invariant preserved**: tier-1 (per-user override) still wins. Real credentials surface to the caller (unlike tier-5 OC's pointer). Inserted between tier-1 and tier-2 so the resolution order is `user > shared_app > env > broker > oc > none`.

- **`core/oauth/shared_apps.py`** — new module: `load_shared_apps()` (broker-facing, decrypts secrets via `vault/encryption.key`), `add_shared_app()` / `remove_shared_app()` / `list_shared_apps()` (CLI-facing, `list` deliberately never returns the secret). `client_secret` is Fernet-encrypted on disk immediately on write — same key path as `api_key` and connection tokens. Lock-protected writes via `threading.Lock`.

- **`GET /api/integrations/<service>/authorize` 503 fallback** (`core/oauth/router.py`) — when the service has neither a per-user override, a tier-1b shared app, env/broker credentials, nor an OC catalog entry, FreeHand returns **HTTP 503 with a structured body** instead of the old 404 dead-end:
  ```json
  {
    "error": "no_shared_app",
    "service": "<service>",
    "message": "FreeHand doesn't have a shared OAuth app for <service> yet...",
    "request_url": "https://github.com/TracySmithConsulting/freehand/issues/new?title=...&body=...",
    "contact_email": "tracy@tracysmith.co.za",
    "scopes_help": "https://docs.tracysmith.co.za/integrations/shared-apps.html#contributing-a-shared-app"
  }
  ```
  `request_url` is a pre-filled GitHub issue URL — user clicks, reviews, submits. **FreeHand does NOT create the issue on the user's behalf** (zero auth surface). Tracy's email is the non-GitHub fallback. Built with `urllib.parse.urlencode` (stdlib, no new runtime dep — Pitfall 32).

- **Tier-1b preempts tier-5 redirect** — for services NOT in `ALLOWED_SERVICES`: if broker returns `shared_app` source, FreeHand falls through to the existing connector path. If broker returns `open_connector`, FreeHand 302s to OC's Web Console. Otherwise, the 503 fallback fires. **Ordering invariant**: services in `ALLOWED_SERVICES` (Google, Microsoft, etc.) always fall through to the existing connector path — tier-1b/tier-5/503 are ONLY for services outside the allowlist. Otherwise an allowlisted service like Google that OC also knows would get hijacked by OC's redirect.

- **`freehand shared-app` CLI subcommands** (`cli.py`) — `add <service> --client-id ... --client-secret ... [--scopes ...] [--registered-by ...]`, `list`, `remove <service>`. Lazy imports keep the CLI boot path fast. The CLI is the right home for these operations — Tracy is the only person who runs them, they're one-time-per-service, and the broker does the runtime lookup via `/authorize`.

- **`docs/integrations/shared-apps.md`** (new) — end-user and Tracy-facing: what shared apps are, when to use them vs. per-user tier-1 vs. OC tier-5, the CLI workflow, the 503 fallback UX, the security model (`encryption.key` shared with `api_key`), "Requesting a service" subsection (GitHub pre-fill + email paths), "Contributing a shared app" path for developer PRs.

- **`docs/integrations/slack-oauth-setup.md`** (new) — Slack-portal walkthrough for the shared-app registration: the four-token-type taxonomy (`xoxp`/`xoxb`/`xapp`/Client-ID), what `client_id` vs `client_secret` vs Bot User OAuth Token are for, the Reinstall trap (Pitfall 33), the Add-then-Save-URLs trap (Pitfall 33a), the missing_scope trap (Pitfall 34), and the `--scopes` alignment with Bot Token Scopes registered at api.slack.com.

### Fixed

- **Round 7 unknown-service 404 replaced with structured 503** — `tests/test_integration.py::TestIntegrations::test_unknown_service` and `tests/test_tier5_router_redirect.py::TestAuthoriseTier5Redirect::test_unknown_service_with_oc_*_returns_404` updated to assert 503 + structured body. The old 404 was a user dead-end with no actionable path; the new 503 gives the user a way to request the service.

### Verified live

Task 6 live smoke + Task 7 matrix (2026-10-01):

| Service | Path | Result |
|---|---|---|
| `google` | tier-1 (per-user broker) | 200 JSON with Google authorize URL |
| `slack` | tier-5 (OC) — full OAuth dance via OC | 302 to OC → Slack consent screen → token stored (`accountId: U0C56R6LTD4`, `grantedScopes: [im:history, channels:read, users:read, chat:write]`) |
| `hackernews` | tier-5 (OC, no-auth) | 302 to OC console |
| `linear` | tier-5 (OC) | 302 to OC console |
| `myspace` | none → **503** | `error=no_shared_app`, `service=myspace`, pre-filled GitHub URL, `contact_email=tracy@tracysmith.co.za` |

### Notes

- **Microsoft 365 still routes via first-party broker** (Azure subscription requirement unchanged for that one provider).
- **Honest scope limitation**: tier-1b works end-to-end for services that have a FreeHand connector (Google, Microsoft, Zoom, etc. — the existing `_get_credentials()` calls `broker.get_client_credentials()` and picks up tier-1b transparently). For services WITHOUT a FreeHand connector (Slack, Notion, Linear, etc.), tier-1b's broker side works but FreeHand has nothing to render the authorize URL — `get_connector(service)` returns `None` and the user gets `501 Connector not implemented`. Round 9 candidate: add per-service Python connectors for tier-1b services.
- **Test count**: net +24 tests added in Round 8 (7 broker + 8 router + 8 CLI + 1 from Round 7's `test_tier5_router_redirect.py` shape updates). Pre-Round-8 baseline was 228 passing + 2 skipped = 230 collected. Post-Round-8: 251 passing + 2 skipped = 253 collected, 0 regressions.
- **No FreeHand tool registry growth**: tier-1b does NOT auto-register provider actions as FreeHand tools (that would let `run_agent()` call `slack.post_message` directly via FreeHand's tool layer). That's a separate Round 9 candidate — out of scope here.
- **Four commits**: `36a88e0` (broker wiring + helper), `41cad51` (router tier-1b preemption + 503), `6b165a0` (CLI subcommands), plus this docs commit.

## [Unreleased] — 2026-09-28

### Added — Round 7: OpenConnector fallback broker

- **`core/oauth/open_connector.py`** — Tier-5 client. Stdlib-only (`urllib`, no runtime dep added). Methods: `is_available()`, `service_is_known(service_id)`, plus a forward-looking `call_mcp_action(action, arguments)` helper. 30-second TTL cache. Never raises into the broker.
- **`core/oauth/broker.py`** — Tier-5 wired into `get_client_credentials()`. Returns `(None, None, "open_connector")` when tiers 1-4 are empty AND `_oc_available()` AND `_oc_service_known()`. Pitfall 5 invariant preserved — user override still wins. New helper `broker.is_known_to_open_connector(service)` exposed for callers that want to ask "is this service tier-5?" without going through full credential resolution.
- **`core/oauth/router.py`** — `GET /api/integrations/<service>/authorize` tier-5 redirect. Above the `ALLOWED_SERVICES` guard. If the broker reports the service as tier-5, FreeHand 302s the browser to OpenConnector's Web Console (Pitfall 24 — never a JSON dump with a URL to copy). The legacy allowlist path is preserved: `google` / `microsoft` / `zoom` / `facebook` / `instagram` / `github` / `email` continue to return the existing `{"authorize_url": ..., "state": ...}` JSON.
- **18 new tests** — `tests/test_open_connector_broker.py` (5), `tests/test_open_connector_client.py` (9), `tests/test_tier5_router_redirect.py` (4).
- **`docs/integrations/open-connector.md`** — Bring-up, env vars, troubleshooting guide.

### Fixed
- **`service_is_known` reads `service` key, not `id`**. Live smoke test against `oomol-lab/open-connector@main` (`#93cd3e5`) exposed the wire-format mismatch: `/v1/providers` returns `{service: "hackernews", displayName: ..., ...}` not `{id: ...}`. Fix honours both keys so the client survives any future wire-format shuffle. Regression-tested via `test_true_for_oc_real_wire_format`.

### Notes
- **Microsoft 365 / Outlook routing is unchanged**: still goes through FreeHand's first-party broker (Azure subscription is still required for that one provider). OpenConnector's hosted Slack/Notion/etc. don't change the Azure requirement for M365.
- **No FreeHand tool registry growth**: this round does NOT auto-register OpenConnector actions as FreeHand tools (that would let `run_agent()` call `slack.post_message` directly). Round 8 candidate — out of scope here.
- **Test count**: net +18 tests added in Round 7 (5 + 9 + 4 across three new files). Pre-Round-7 baseline was 210 unit + 37 browser-v2 = 247 collected. Post-Round-7: 230 collected, 228 passing, 2 skipped, 0 regressions.

## [Unreleased] — 2026-08-28

### Added — Browser v2 (CDP auto-connect, validation, network capture, checkpoints)

- **Feature 1 — CDP auto-connect**: `get_axtree`, `navigate`, `click`, `fill` now attempt three connection strategies in order: (1) connect to existing Chrome via `chrome://inspect/#remote-debugging` JSON API on localhost:9222 (preserves cookies/logins), (2) connect via Chrome Extension Bridge on localhost:9223 (no consent prompt needed), (3) launch fresh sandboxed Chromium (fallback). `CDP_STRATEGY` environment variable forces a specific strategy for testing.
- **`get_validation_summary`**: Returns a structured pass/fail summary of the current page: `valid` (bool|null), `field_errors` (list of {field, message}), `aria_live_text`, `http_status`, and `recommendation`. Scans ARIA live regions, error summary divs, field-level `aria-describedby` errors, `aria-invalid`, and pass/fail keyword text. (`core/tools/browser.py`)
- **`start_request_capture` / `get_captured_requests` / `stop_request_capture`**: Network interception tools using Playwright's `route` API. Requests matching `url_pattern` (regex or substring) are stored in memory with method, URL, request headers/body, response status, response headers/body, and timestamp. (`core/tools/browser.py`)
- **`save_checkpoint` / `restore_checkpoint` / `list_checkpoints` / `delete_checkpoint`**: Save and restore page state without re-entering data. Captures form values, checkbox states, radio states, select values, cookies, and localStorage as JSON. Checkpoints stored in `vault/checkpoints/`. (`core/tools/browser.py`)
- **`freehand/chrome_extension/`**: Full Chrome Extension scaffold (Manifest V3) bridging FreeHand's HTTP/JSON-RPC interface to Chrome's built-in `chrome.debugger` API. Components: `manifest.json`, `background.js` (service worker), `content_script.js`, `popup/popup.html` + `popup.js`, `freehand-nph.py` (native messaging host), and `README.md`.
- **11 new tools registered**: `get_validation_summary`, `start_request_capture`, `get_captured_requests`, `stop_request_capture`, `save_checkpoint`, `restore_checkpoint`, `list_checkpoints`, `delete_checkpoint`, `navigate`, `click`, `fill`. Total tools: **35** (was 24).
- **`tests/test_browser_v2.py`**: 37 tests covering CDP endpoint detection, validation regex patterns, request capture state machine, checkpoint serialization, Chrome Extension manifest structure, and tool registration.

### Added — How Claude and ChatGPT Control Browsers (investigation)

- **CDP (Chrome DevTools Protocol)** is the mechanism both vendors use — HTTP/WebSocket interface that Chrome exposes when started with `--remote-debugging-port=9222`.
- **Claude Code**: launches a separate Chrome instance with the debug port flag; connects via WebSocket. Does NOT connect to user's existing browser.
- **ChatGPT Computer Use**: uses a lightweight desktop helper app (Electron-based on Windows) for OS-level actions; uses CDP for browser. The Chrome Extension path enables "your existing browser with your logins" by bypassing the consent prompt.
- **`chrome://inspect/#remote-debugging`** is the key URL for manual testing of CDP connections.
- **Windows foreground constraint**: ChatGPT's Computer Use on Windows must run on the active desktop — it takes over mouse/keyboard. Background co-work is only possible on macOS/Linux.

## [Unreleased] — 2026-08-11

### Fixed
- **C1 (agent loop safety)**: `run_agent()` now detects stuck loops where the LLM calls the same tool with identical arguments 3+ times consecutively. Aborts early with an explanatory error instead of burning `max_turns` tokens. (`core/agent.py`)
- **C2 (tool permission registry)**: replaced the hardcoded `write_tools` set (8 entries, with 2 dead `send_email`/`create_meeting`) with a centralised `TOOL_REGISTRY` mapping 24 tools to `read` | `write`. Unknown tools fail closed (require approval) rather than silently bypassing the check. (`core/agent_config.py`, `core/agent.py`)
- **C3 (OAuth pending-state growth)**: `_sweep_stale_pending_states()` drops `oauth.pending_states` entries older than 1 hour and enforces a hard cap of 50, oldest-first. Called before both authorize (write path) and callback (lookup path). Callback now also rejects states older than the TTL even if the sweep missed them (clock-skew safety). Prevents unbounded growth of `settings.json` when callbacks never complete. `_parse_iso()` now normalises naive timestamps (legacy `datetime.utcnow()` writes) to UTC-aware so existing settings.json files don't crash the sweep. New writes use `datetime.now(timezone.utc)` (aware). (`core/oauth/router.py`)
- **C5 (Slack approval URL base)**: Slack approval buttons now derive the base URL from `settings.public_base_url`, with a printed warning + `http://localhost:8000` fallback when unset. Previously hardcoded localhost broke any Tailscale / reverse-proxy / HTTPS deployment. (`core/security.py`)

### Added
- `get_tool_permission(name)` returns `'read' | 'write' | 'unknown'` for tool name. Centralised permission source for `run_agent()` and any future tool-aware code. (`core/agent_config.py`)
- `_parse_iso(s)` and `_sweep_stale_pending_states(settings)` exported helpers in `core/oauth/router.py` for future tests.
- **`tests/test_security_fixes.py`** — 23 unit tests covering C1, C2, C3, C5. Uses isolated vault fixtures (no touch on real `settings.json`). Verifies: registry completeness, loop detection, ISO parsing tolerance, sweep behaviour, public_base_url fallback + warning.

## [Unreleased] — 2026-08-11

### Security — round 2 (critical, missed in first review)

- **N1 (API key auth)**: every `/api/*` and `/api/tools/*` endpoint now requires `X-API-Key` header. Key auto-generated on first run and persisted to `vault/settings.json → api_key`. Gateway webhooks (`/api/gateway/*`) exempt because they have their own auth (bot tokens, HMAC, bridge secret). Health and root paths exempt. Constant-time compare. (`server.py`)
- **N2 (settings deep-merge)**: `POST /api/settings` now deep-merges updates into existing settings instead of overwriting whole file. Unknown keys rejected via allowlist. Recursive merge preserves nested data (e.g. updating `oauth.providers.google` does not clobber `oauth.redirect_uris`). (`server.py`)
- **N3 (command truncation)**: `/api/agent/command` now stores only the first 200 chars of the command in the `memories` table, prefixed with a SHA256 hash for traceability. Sensitive content in commands is not persisted indefinitely. (`server.py`)
- **N4 (slug sanitisation)**: `process_scribble` rejects topic slugs containing `..`, `/`, or `\\`, and verifies the resolved path is inside `THREADS_DIR` before writing. Prevents path traversal from user-controlled scribble content. (`core/scheduler.py`)
- **N6 (source-aware tier)**: write tool calls from remote sources (telegram, slack, whatsapp) at GOD_MODE tier are now demoted to SEMI_AUTONOMOUS for that single call, requiring approval. Local (web, CLI) source at GOD_MODE retains legacy behaviour. Source is now passed through `intercept_action` payload for auditability. (`core/agent.py`)
- **N7 (dead code cleanup)**: removed `conn = DB_PATH.__class__(DB_PATH)` line in `core/gateway.py` `/task` handler — never made a DB connection, just confusing. (`core/gateway.py`)
- **N8 (datetime cleanup)**: all `datetime.utcnow()` calls in `core/scheduler.py` (4) and `core/gateway.py` (2) switched to `datetime.now(timezone.utc)`. Avoids `TypeError` on aware/naive comparisons; future-proofs for Python 3.12+ where `utcnow()` is deprecated. (`core/scheduler.py`, `core/gateway.py`)
- **N12a (Telegram allow_from)**: telegram gateway now requires caller chat_id to be in `settings["telegram_allow_from"]`. Empty/missing list rejects ALL messages (closed-by-default) with one-shot warning. Backwards-compat with legacy `telegram_chat_whitelist` key. (`core/gateway.py`)
- **N12b (Slack allow_from)**: same pattern — Slack user_id must be in `settings["slack_allow_from"]`. Empty list rejects all. (`core/gateway.py`)
- **N12c (WhatsApp allow_from + bridge secret)**: WhatsApp webhook now requires `X-Bridge-Secret` header matching `settings["bridge_secret"]`. Bridge must be configured to send this header. Caller JID must be in `settings["whatsapp_allow_from"]`. If bridge_secret is unset, endpoint returns 503 (closed by default). (`server.py`, `core/gateway.py`)

### Reliability

- **H1 (LLM retry/backoff)**: `call_llm` now retries up to 3 times with exponential backoff (2s, 4s) on transient errors (HTTP 408/425/429/500/502/503/504, timeouts, connection errors). Auth errors (401/403) and client errors (400) are not retried. Programmer errors are not retried. (`core/agent.py`)
- **H3 (tool result cap)**: tool results sent to the LLM are capped at 8000 chars with a `[... truncated]` marker. Full result is preserved in the API response (`tools_used` list) for the user. Prevents `read_sheets` 5000-row responses from blowing context on the next turn. (`core/agent.py`)

### Other

- **N11 (telegram whitelist warning)**: deferred — covered by N12a allow_from default-deny.
- **N5 (browser URL allowlist)**: deferred — N1 reduces attack surface but does not eliminate. Needs design discussion before implementation.
- **N9 (request body size limit)**: deferred — needs FastAPI middleware, design discussion needed.
- **C7 (install.sh brew prompt)**: `install.sh` now prompts before auto-installing Homebrew on macOS (skips and exits cleanly when non-interactive). Default behaviour is to refuse and abort, forcing the user to read the warning. (`scripts/install.sh`)
- **M4 (tool count)**: README + CHANGELOG updated from "17 agent tools" to "24 agent tools (6 write + 18 read)" to match actual `TOOL_REGISTRY`. (`README.md`, `CHANGELOG.md`)

### Tests added

- **`tests/test_integration.py`** — updated to use new `api_key` + `client` fixtures. Added `TestAuthEnforcement` class (6 tests) verifying 401 on missing/wrong API key and that `/health` + gateway endpoints remain accessible without key.
- **`tests/test_round2_fixes.py`** — 24 new tests across N2/N3/N4/N6/N8/N12/H1/H3. Uses isolated vault fixtures (no touch on real settings.json). Tests include LLM retry on 429/5xx/timeout, NO retry on 400, source-aware tier demotion, allow_from denial + acceptance + legacy compat, slug path traversal rejection, settings deep-merge at multiple levels.

Full suite: **81 tests, 81 passing**.

## [Unreleased] — 2026-08-11 — round 3

### Security — round 3 (browser / memory / body-size / approvals)

- **R1 (screenshot filename collision)**: screenshot files were named `screenshot_{len(url)}.png`. Two URLs of equal length silently overwrote each other. Now derived from `sha256(url)[:16]` — collision-free in practice, no PII in filename. (`core/tools/browser.py`)
- **R3 (ref-marker data corruption)**: `extract_text` stripped `[ref=e...]` markers via naive string replace (`line.replace("[ref=e", "").replace("]", "")`). The `]` replace also stripped legitimate `]` characters from page content (e.g. "see [ref=e10] in our docs" → "see e10 in our docs"). Now uses precise regex `\[ref=e\d+\]`. (`core/tools/browser.py`)
- **R5 (FTS5 query injection)**: `search_memory` passed user input directly to FTS5 `MATCH` operator. Queries containing `*`, `:`, `OR`, `NOT`, `^`, or unmatched quotes could return every row, raise parse errors, or crash the agent loop. Now wraps in double-quote phrase query after escaping internal quotes. Defensive `try/except` returns empty on any FTS5 error rather than crashing. (`core/memory.py`)
- **R6 (aria snapshot nesting)**: `_parse_aria_snapshot` claimed to build nested trees via stack-based parsing, but the implementation always produced flat siblings with empty `children` arrays. Now uses leading-dash count to determine depth — produces real nested structure matching Playwright's aria snapshot output. (`core/tools/browser.py`)
- **R9 (screenshot permission + size cap)**: `screenshot()` was marked "read-only, allowed in all tiers" but actually wrote files to disk and could capture logged-in pages with visible credentials. Now requires `intercept_action` approval (same as click/fill/navigate). Adds operator-configurable `settings["max_screenshot_size_mb"]` cap (default 20MB); oversized screenshots are deleted after capture with a clear error. (`core/tools/browser.py`)
- **N9 (request body size limit)**: FastAPI middleware rejects requests with `Content-Length` > `settings["max_request_body_bytes"]` (default 1 MiB). Exempts gateway webhooks (Slack/Telegram payloads), `/health`, and static routes. Returns 413 with actionable error. (`server.py`)
- **C4 (clear_pending_approvals footgun)**: the old function silently cancelled all in-flight approvals with no warning, no reason, no audit trail. Now requires a `reason` parameter (with stderr warning when empty), writes a `bulk_clear` audit row to the `approvals` table, and the `/api/approvals/clear` endpoint accepts `{"reason": "..."}` in the body. (`core/security.py`, `server.py`)

### Tests added

- **`tests/test_round3_fixes.py`** — 22 new tests across R1/R3/R5/R6/R9/N9/C4. Uses isolated DB/vault fixtures. Tests include: FTS5 injection queries not crashing the agent loop, screenshot filename collisions avoided, screenshot permission check, 413 on oversized body, clear_pending_approvals audit log + warning.

Full suite: **103 tests, 103 passing**.

## [Unreleased] — 2026-08-12 — round 4

### Functionality

- **B2 (tool registry → function-calling schemas)**: `list_available_tools()` was a hardcoded list of 14 tools while TOOL_REGISTRY had 24 — 10 write tools were dead code from the LLM's perspective. Replaced with a schema-driven build: every entry in `TOOL_REGISTRY` now gets a corresponding schema in the new `TOOL_SCHEMAS` dict, and the LLM sees all 24 tools. Adding a new tool = register in TOOL_REGISTRY + add schema in TOOL_SCHEMAS. (`core/agent_config.py`)
- **R10 (skip scratchpad in vault sync)**: `sync_vault_to_sqlite()` no longer indexes `00_Scribble.md` or `.sweep_state.json` into the FTS5 memories table. They were always transient/operational files. (`core/memory.py`)
- **R11 (parse_skills boundary check)**: `parse_skills()` now verifies each skill_dir resolves inside the skills root, and each SKILL.md resolves inside its own skill_dir. Prevents symlink-based escapes from loading malicious skill content. (`core/memory.py`)

### Tests added

- **`tests/test_round4_fixes.py`** — 14 new tests across B2/R10/R11/R13/R15. Includes registry-schema consistency checks, end-to-end vault sync tests, workspace boundary tests for the office write tools.

Full suite: **117 tests, 117 passing, 2 skipped (Windows symlink privilege)**.

## [Unreleased] — 2026-08-16 — round 6 (Microsoft 365 wired)

### Fixed
- **Microsoft OAuth token exchange content-type**: `MicrosoftConnector.handle_callback()` was pre-URL-encoding the token-exchange payload and passing it as a string, which made aiohttp send `Content-Type: text/plain`. Microsoft rejects this with `AADSTS900144` ("request body must contain grant_type"). Now passes a dict (same fix as Google on round 5 / commit `63b32fa`). End-to-end verified: real Microsoft 365 account (`trace-space@outlook.com`) connected via Azure broker app, 3 real emails read from Graph API. (`core/oauth/providers/microsoft.py`)

### Tests
- **Regression tests for provider token-exchange content-type**: `TestProviderTokenExchangeContentType` (2 tests) asserts every provider's `handle_callback` posts a dict (not a pre-urlencoded string). Catches the bug pattern if a new provider copies the old Microsoft code. (`tests/test_oauth_broker.py`)



Implements the [[../Tracy-Personal-Wiki/concepts/freehand-mcp-broker|freehand-mcp-broker design]]: Deliverable 1 (MCP server surface) + Deliverable 2 (OAuth broker Option B).

### Deliverable 1 — FreeHand as MCP server

- **New `/mcp` endpoint** at `POST /mcp` (JSON-RPC 2.0 over HTTP). Mounted with the same `X-API-Key` auth as `/api/*`. Methods: `initialize`, `ping`, `tools/list`, `tools/call`, `resources/list`, `resources/read`. (`core/mcp_server.py` + `server.py` mount)
- All 24 tools (Round 4 schema-driven) are now reachable from any MCP client — Claude Desktop, Hermes, custom agents. Same permission checks as the in-process agent loop (write tools at GOD_MODE execute, lower tiers require approval).
- Resources serve vault files as `freehand://vault/<rel>` URIs. Skips `00_Scribble.md` and `.sweep_state.json` (operational noise). Path-traversal rejected.
- 25 new tests in `tests/test_mcp_server.py`: JSON-RPC envelope shape, batch requests, write-tool permission gate, resource path traversal, malformed JSON handling, HTTP-level integration via TestClient.

### Deliverable 2 — OAuth broker Option B

- **New `core/oauth/broker.py`** with `get_client_credentials(service)` returning `(client_id, client_secret, source)`. Resolution order: user override in `settings.json` → `FREEHAND_BROKER_<SERVICE>_*` env vars → `vault/broker_config.json` → `(None, None, "none")`.
- **All 5 OAuth connectors** (Google, Microsoft, Zoom, Facebook, Instagram) now route through broker. Microsoft tenant stays in user settings (not a secret). Facebook+Instagram share a single Meta app_id/secret pair.
- **New HTTP endpoints** under `/api/broker/`: `GET /status`, `POST /config`, `DELETE /config`. All require `X-API-Key`. Atomic write via tmp+rename.
- 22 new tests in `tests/test_oauth_broker.py`: resolution order, atomic write, connector integration, HTTP endpoints, user-overrides-broker.

### Operator notes

After upgrading, the broker is **inactive by default** — no built-in client_ids. To enable the broker:

```bash
# Option A: env vars (preferred for production)
export FREEHAND_BROKER_GOOGLE_CLIENT_ID="..."
export FREEHAND_BROKER_GOOGLE_CLIENT_SECRET="..."
# ...repeat for each service

# Option B: broker_config.json (preferred for dev)
curl -X POST -H "X-API-Key: $FREEHAND_API_KEY" \
     -d '{"providers": {"google": {"client_id": "...", "client_secret": "..."}}}' \
     http://localhost:8000/api/broker/config
```

User-supplied credentials in `settings["oauth"]["providers"][service]` always win over the broker. The broker is a fallback, not a replacement.

To connect an MCP client (e.g. Claude Desktop):

```json
{
  "mcpServers": {
    "freehand": {
      "url": "http://localhost:8000/mcp",
      "transport": "http",
      "headers": {"X-API-Key": "<your-api-key>"}
    }
  }
}
```

Full suite: **164 tests, 164 passing, 2 skipped (Windows symlink privilege)**.

- **Round 6.1 (commit pending, 2026-08-16)** — added `rename_connection_label()` to fix the wrong-label UX bug. `freehand rename google --from dbsa --to shazacin` renames a connection in place. 3 regression tests (rename existing, missing label, doesn't affect others). Now 173 tests passing.

## [Unreleased] — 2026-08-12 — round 5.1 (MCP end-to-end smoke + Bearer auth)

End-to-end smoke test with Codex CLI caught two real bugs and added Bearer auth compatibility.

### Fixed

- **Event-loop collision in MCP tool dispatch**: `_mcp_tool_call()` was calling `execute_tool()` (async) via `loop.run_until_complete()` from inside FastAPI's already-running event loop, raising "Cannot run the event loop while another loop is running." Fixed by dispatching the coroutine to a fresh worker thread via `_run_async_in_thread()`, which gets its own clean event loop. (`core/mcp_server.py`)
- **`/api/memory/search` is GET, not POST**: discovered during smoke test — `/api/memory/search` only accepts GET. Not changed (correct behaviour), but worth noting for future API tests. (`server.py`)

### Added

- **Bearer-token auth compatibility**: `require_api_key()` now accepts `Authorization: Bearer <key>` in addition to `X-API-Key: <key>`. This is what MCP-standard clients (Codex, Claude Desktop, generic MCP SDKs) send by default. Constant-time comparison, case-insensitive prefix per RFC 7235. 4 new tests. (`server.py`)

### Verified

End-to-end smoke test against Codex CLI 0.145.0 (using `Authorization: Bearer <key>`):

  $ codex mcp add freehand --url http://localhost:8000/mcp \
                            --bearer-token-env-var FREEHAND_API_KEY
  $ export FREEHAND_API_KEY=<api-key>
  $ codex exec "Use freehand search_memory to search for 'MCP smoke test'"

  → Codex connected, dispatched `search_memory`, got a real FTS5 match
  → Returned the title, vault path, and timestamp of the matched note

Before this commit, the MCP server would have returned
"Cannot run the event loop while another loop is running" on every
real tool call. After, it works end-to-end.

OpenCode is NOT installed (only Codex CLI, Pi Agent, and Antigravity IDE are present). Hermes itself supports MCP natively — adding a `mcp_servers:` entry to `~/.hermes/config.yaml` would let Hermes drive FreeHand's tools too. Not done in this round — Tracy can decide if she wants that wired up.

Full suite: **168 tests, 168 passing, 2 skipped (Windows symlink privilege)**.

## [0.1.0] — 2026-08-10

### Added
- **FreeHand agent system** — local AI agent with LLM tool calling
- **OAuth integration system** — 7 connectors (Google, Microsoft, Zoom, Facebook, Instagram, GitHub PAT, IMAP/SMTP email)
- **Cross-agent memory import** — detect and import from Hermes Agent and OpenClaw
- **Hermes Agent state.db extraction** — selective memory extraction from large SQLite databases
- **Token auto-refresh** — automatic refresh_token exchange for Google, Microsoft, Zoom
- **Permission tiers** — Autonomous, Semi-Autonomous, God Mode with approval workflows
- **Remote gateways** — Telegram, Slack, WhatsApp (via bridge)
- **Scribble sweep scheduler** — cron-based task extraction from notes
- **FTS5 memory search** — full-text search across vault notes
- **Encrypted token storage** — Fernet-encrypted OAuth tokens at rest
- **CLI** — `freehand` command with subcommands for all operations
- **Web UI** — single-page dashboard with Connections, Agents, Settings panels
- **35 agent tools** — 14 write + 21 read (email, calendar, sheets, GitHub, Zoom, browser v2, documents, memory search)
- **Install scripts** — one-command install for Windows (PowerShell) and macOS/Linux (bash)
- **GitHub Actions CI** — test, release, and PyPI publish workflows

### Changed
- Renamed project from `myagent` to `freehand-agent`
- Removed Nango stubs and references

### Security
- Added `oauth_access` to external actions requiring approval
- Auto-generated Fernet encryption key on first run (mode 0o600)
- Skips secret files (auth.json, openclaw.json) during agent import

[0.1.0]: https://github.com/TracySmithConsulting/freehand/releases/tag/v0.1.0

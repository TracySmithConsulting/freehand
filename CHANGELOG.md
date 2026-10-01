# Changelog

All notable changes to FreeHand will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

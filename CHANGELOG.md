# Changelog

All notable changes to FreeHand will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- **24 agent tools** — 6 write + 18 read (email, calendar, sheets, GitHub, Zoom, browser, documents, memory search)
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

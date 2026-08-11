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
- **17 agent tools** — email, calendar, sheets, GitHub, Zoom, browser, documents
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

# Changelog

All notable changes to FreeHand will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

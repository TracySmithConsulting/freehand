# FreeHand

**Your local AI agent, on your devices, in your channels.**

FreeHand is a local-first AI agent system with OAuth integrations, cross-agent memory import, and Tailscale network access. Built for the South African market — free, open source, no vendor lock-in.

```
pip install freehand-agent
freehand serve
# Open http://localhost:8000
```

## Features

- **🔌 7 OAuth connectors** — Gmail, Outlook, Zoom, Facebook, Instagram, GitHub, custom email (IMAP/SMTP)
- **🔗 Cross-agent memory import** — automatically detects and imports memories from Hermes Agent and OpenClaw
- **🛠 24 agent tools** — 6 write (file writes, OAuth-protected posts/PRs/issues/meetings) + 18 read (email, calendar, sheets, GitHub, Zoom, browser automation, document processing, memory search)
- **🔒 Permission tiers** — Autonomous, Semi-Autonomous, God Mode with approval workflows
- **💬 Remote gateways** — Telegram, Slack, WhatsApp
- **🧠 FTS5 memory search** — full-text search across all vault notes
- **📅 Task scheduler** — cron-based task execution via scribble sweep
- **🔐 Encrypted token storage** — Fernet encryption for all OAuth tokens
- **🌐 Tailscale support** — accessible from anywhere on your tailnet

## Install

### One-command install (recommended)

**Windows (PowerShell):**
```powershell
iwr https://freehand.tracysmith.co.za/install.ps1 -useb | iex
```

**macOS / Linux:**
```bash
curl -fsSL https://freehand.tracysmith.co.za/install.sh | bash
```

### From PyPI

```bash
pip install freehand-agent
freehand init
freehand serve
```

### From source

```bash
git clone https://github.com/TracySmithConsulting/freehand.git
cd freehand
pip install -e ".[all]"
freehand init
freehand serve
```

## Quick Start

```bash
# 1. Install
pip install freehand-agent
freehand init

# 2. Start the server
freehand serve

# 3. Open in browser
# http://localhost:8000

# 4. Connect your accounts (Settings → OAuth Credentials)
# See docs/OAUTH_SETUP.md for step-by-step
```

## CLI Commands

```bash
freehand serve                    # Start the web server
freehand init                     # Initialize database and vault
freehand sweep                    # Process pending scribble entries

# Connections
freehand connect <service>        # Start OAuth flow (google, microsoft, zoom, etc.)
freehand connections              # List all connections
freehand disconnect <service>     # Remove a connection
freehand test <service>           # Test if connection is valid

# Agents (cross-agent import)
freehand agents list              # Detect installed AI agents
freehand agents import hermes     # Import from Hermes Agent
freehand agents import openclaw   # Import from OpenClaw
freehand agents status            # Show import history
freehand agents remove <agent>    # Remove imported data

# Tools
freehand office read-docx <path>
freehand browser axtree <url>
freehand browser screenshot <url>

# Gateway
freehand gateway status
freehand gateway pair <phone>
```

## OAuth Setup

See [docs/OAUTH_SETUP.md](docs/OAUTH_SETUP.md) for detailed instructions on registering apps with:
- Google Cloud Console (Gmail, Calendar, Sheets, Meet)
- Microsoft Entra ID (Outlook, Teams, Calendar)
- Zoom Marketplace (Meetings, recordings)
- Meta for Developers (Facebook, Instagram)
- GitHub (PAT-based, no app registration needed)

## Cross-Agent Memory Import

FreeHand can detect and import memories from other AI agents installed on your system:

| Agent | What gets imported |
|-------|-------------------|
| **Hermes Agent** | SOUL.md, AGENTS.md, MEMORY.md, USER.md, config, 20+ skills, state.db (selective) |
| **OpenClaw** | SOUL.md, IDENTITY.md, MEMORY.md, USER.md, AGENTS.md, journal entries |

Run `freehand agents list` to detect, then `freehand agents import hermes --state` to import.

## Architecture

```
┌─────────────────────────────────────────────┐
│  Web UI  (static/index.html)                │
│  http://localhost:8000                      │
└──────────────────┬──────────────────────────┘
                   │ FastAPI
┌──────────────────▼──────────────────────────┐
│  server.py     core/                        │
│  ┌─────────────┴─────────────────────────┐  │
│  │  /api/agent/command    ← LLM agent    │  │
│  │  /api/integrations/*   ← OAuth flows  │  │
│  │  /api/agents/*         ← import mgmt  │  │
│  │  /api/tools/*          ← tool API     │  │
│  └───────────────────────────────────────┘  │
│                                             │
│  core/agents/     ← detection & import      │
│  core/oauth/      ← token management        │
│  core/tools/      ← office, browser, integrations│
│  core/memory/     ← FTS5 search           │
│  core/security/   ← permission tiers        │
│  core/gateway/    ← Telegram/Slack/WhatsApp │
│  core/scheduler/  ← cron-based sweep      │
└──────────────────┬──────────────────────────┘
                   │
┌──────────────────▼──────────────────────────┐
│  vault/              ← user data            │
│  agent.db            ← SQLite (FTS5)       │
│  connections.json    ← encrypted tokens    │
│  imports/hermes/     ← imported memories   │
│  imports/openclaw/   ← imported memories   │
└─────────────────────────────────────────────┘
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/status` | Service status |
| `POST` | `/api/agent/command` | Run agent command |
| `GET` | `/api/integrations` | List integrations |
| `GET` | `/api/integrations/{service}/authorize` | Get OAuth URL |
| `GET` | `/api/integrations/callback/{service}` | OAuth callback |
| `POST` | `/api/integrations/{service}/disconnect` | Remove connection |
| `GET` | `/api/agents` | Detect installed agents |
| `POST` | `/api/agents/import` | Import from agent |
| `GET` | `/api/agents/imported` | Import history |

Full API reference: [API Documentation](https://github.com/TracySmithConsulting/freehand/wiki/API)

## Security

- All OAuth tokens are **encrypted at rest** using Fernet (AES-128-CBC + HMAC-SHA256)
- Encryption key is auto-generated on first run at `vault/encryption.key` (mode 0o600)
- Permission tiers enforce approval workflows for write operations
- Nango removed — all OAuth is handled in-process
- No telemetry or data collection

## License

MIT — see [LICENSE](LICENSE)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)

## Changelog

See [CHANGELOG.md](CHANGELOG.md)

## Links

- **Source:** https://github.com/TracySmithConsulting/freehand
- **PyPI:** https://pypi.org/project/freehand-agent/
- **Issues:** https://github.com/TracySmithConsulting/freehand/issues
- **Setup Guide:** docs/OAUTH_SETUP.md

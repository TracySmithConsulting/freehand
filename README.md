# FreeHand

**Your local AI agent, on your devices, in your channels.**

FreeHand is a local-first AI agent system with OAuth integrations, cross-agent memory import, and Tailscale network access. Built for the South African market — free, open source, no vendor lock-in.

```
pip install freehand-agent
freehand start
# Open http://localhost:8000
```

## Features

- **🔌 7 OAuth connectors** — Gmail, Outlook, Zoom, Facebook, Instagram, GitHub, custom email (IMAP/SMTP)
- **🔗 Cross-agent memory import** — automatically detects and imports memories from Hermes Agent and OpenClaw
- **🛠 24 agent tools** — 6 write (file writes, OAuth-protected posts/PRs/issues/meetings) + 18 read (email, calendar, sheets, GitHub, Zoom, browser automation, document processing, memory search)
- **🔒 Permission tiers** — Autonomous, Semi-Autonomous, Full Access with approval workflows
- **💬 Remote gateways** — Telegram, Slack, WhatsApp
- **🧠 FTS5 memory search** — full-text search across all vault notes
- **📅 Task scheduler** — cron-based task execution via scribble sweep
- **🔐 Encrypted token storage** — Fernet encryption for all OAuth tokens
- **🌐 Tailscale support** — accessible from anywhere on your tailnet
- **🔀 Pluggable connector backends** — OpenConnector for the broad catalog, with an optional Composio adapter for Meta OAuth
- **🖥 Help on your own computer** — the FreeHand Helper for Windows (below)

## FreeHand Helper for Windows

The FreeHand Helper is a small Windows program that lets FreeHand help on
your own computer, whether FreeHand runs on the same PC or on a server. It
uses Windows accessibility (what screen readers use), so it's especially
good with apps and websites that are hard to use with a screen reader.

- **Look:** read a window's buttons, fields and menus, say which have no
  name, take a picture of a window, and check the computer's details.
- **Act:** click, type, press keys, open apps and websites, and switch,
  close or minimise windows.
- **Change:** install, update and remove programs with winget (Windows' own
  installer), and run four known fixes: sound, printer, network and taskbar.

**It asks you first,** in a dialog a screen reader reads, and changes are
asked every time. **Ctrl+Alt+Escape stops it** at once. It never types
passwords, never acts in Windows' security screens or FreeHand's own
console, and can't run commands of its own.

**Download:** [FreeHand.exe](https://github.com/TracySmithConsulting/freehand/releases/download/helper-latest/FreeHand.exe)
(Windows 10 and 11, nothing else to install). When it asks where FreeHand
runs, choose **Somewhere else**. Then, in FreeHand, open Help on my
computer and press Add a computer. More in the
[Helper guide](helper/windows/README.md).

## Install

### Windows: the FreeHand app (easiest)

**Download [FreeHand.exe](https://github.com/TracySmithConsulting/freehand/releases/download/helper-latest/FreeHand.exe)** and open it.
It checks the computer, switches on what Windows needs (one "allow
changes?" and a restart), downloads FreeHand (about 1.8 GB), and opens it
in the browser. No Docker Desktop, terminal or licence needed, and every
screen works with a screen reader. Windows 10 (version 2004 or newer) or
11, with about 12 GB of free space.

Until it's signed, Windows may say "Windows protected your PC": choose
**More info**, then **Run anyway**. More in the
[FreeHand app guide](helper/windows/README.md).

### Linux: one line

```bash
curl -fsSL https://github.com/TracySmithConsulting/freehand/releases/latest/download/install.sh | bash
```

It sets up the same services as the Windows app, from ready-made images,
with the free Docker engine (from your Linux's own packages, after asking).
Then open http://localhost:8000 for the welcome setup. Running the line
again updates FreeHand. Mac comes later.

### Windows: one line (PowerShell)

```powershell
irm https://github.com/TracySmithConsulting/freehand/releases/latest/download/install.ps1 | iex
```

It downloads the FreeHand app above and opens it.

### From PyPI

```bash
pip install freehand-agent
freehand start
```

### From source

```bash
git clone https://github.com/TracySmithConsulting/freehand.git
cd freehand
pip install -e ".[all]"
freehand start
```

## Quick Start

```bash
# 1. Install and start
pip install freehand-agent
freehand start

# 2. Open in browser (the command normally does this for you)
# http://localhost:8000

# 3. Connect your accounts (Settings → OAuth Credentials)
# See docs/OAUTH_SETUP.md for step-by-step
```

### Optional Meta authentication through Composio

FreeHand can use Composio's hosted Connect Links for Facebook and Instagram.
This keeps Meta OAuth credentials and user access tokens outside the local
FreeHand installation. Set these variables before running `freehand start`:

```text
COMPOSIO_API_KEY=your-project-key
COMPOSIO_AUTH_CONFIG_FACEBOOK=your-facebook-auth-config-id
COMPOSIO_AUTH_CONFIG_INSTAGRAM=your-instagram-auth-config-id
```

When these are present, the Facebook and Instagram Connect buttons use
Composio. Other services continue to use FreeHand or OpenConnector as before.
If they are absent, the existing first-party Meta connector remains available.
See [docs/integrations/composio.md](docs/integrations/composio.md) for the
complete setup and hosted-deployment notes.

## CLI Commands

```bash
freehand start                    # Initialize, start, and open the local UI
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

## Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io),
certificate by [SignPath Foundation](https://signpath.org).

- **What's signed:** the FreeHand Helper for Windows, built by GitHub Actions
  from this repository's public source
  ([workflow](.github/workflows/helper.yml)). Nothing built on a personal
  computer is signed.
- **Authors and reviewers:** the members of
  [TracySmithConsulting](https://github.com/TracySmithConsulting).
  Every change goes through a pull request.
- **Approver:** Tracy Smith. Every release is approved by hand.
- **Privacy:** the Helper connects only to the FreeHand address the person
  pairs it with, and sends only what they allow. It sends nothing to anyone
  else.

The full policy: [Code signing policy](docs/code-signing-policy.md).

## License

FreeHand is free and open source under the MIT licence. See [LICENSE](LICENSE)
for the legal text and [Using, sharing and changing FreeHand](docs/LICENSING.md)
for a plain-language guide.

You're welcome to fork FreeHand. If you share your own version, please give it
its own name. See [The FreeHand name](TRADEMARKS.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)

## Changelog

See [CHANGELOG.md](CHANGELOG.md)

## Links

- **Source:** https://github.com/TracySmithConsulting/freehand
- **PyPI:** https://pypi.org/project/freehand-agent/
- **Issues:** https://github.com/TracySmithConsulting/freehand/issues
- **Setup Guide:** docs/OAUTH_SETUP.md

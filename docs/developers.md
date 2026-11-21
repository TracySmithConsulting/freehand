# FreeHand for developers

For people who want to run FreeHand from the command line, set up connections by hand, or work on FreeHand itself. Everyone else: see the [README](../README.md).

To contribute, see [Contributing](../CONTRIBUTING.md).

## Quick start

```bash
# 1. Install and start
pip install freehand-agent
freehand start

# 2. Open in browser (the command normally does this for you)
# http://localhost:8000

# 3. Connect your accounts on the Connections page
# See docs/OAUTH_SETUP.md for setting up your own apps
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
See [docs/integrations/composio.md](integrations/composio.md) for the
complete setup and hosted-deployment notes.

## CLI commands

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

## OAuth setup

See [OAUTH_SETUP.md](OAUTH_SETUP.md) for detailed instructions on registering apps with:
- Google Cloud Console (Gmail, Calendar, Sheets, Meet)
- Microsoft Entra ID (Outlook, Teams, Calendar)
- Zoom Marketplace (Meetings, recordings)
- Meta for Developers (Facebook, Instagram)
- GitHub (PAT-based, no app registration needed)

## Cross-agent memory import

FreeHand can detect and import memories from other AI agents installed on your system:

| Agent | What gets imported |
|-------|-------------------|
| **Hermes Agent** | SOUL.md, AGENTS.md, MEMORY.md, USER.md, config, 20+ skills, state.db (selective) |
| **OpenClaw** | SOUL.md, IDENTITY.md, MEMORY.md, USER.md, AGENTS.md, journal entries |

Run `freehand agents list` to detect, then `freehand agents import hermes --state` to import.

## How it fits together

- **The web page** (`freehand/static/index.html`) is FreeHand's window, at
  http://localhost:8000.
- **The server** (`server.py`, FastAPI) answers it. The main parts:
  - `/api/agent/command`: the AI agent;
  - `/api/integrations/*`: connecting accounts;
  - `/api/agents/*`: importing from other agents;
  - `/api/tools/*`: the tools directly.
- **The parts behind it,** in `core/`:
  - `core/agents/`: finding and importing other agents;
  - `core/oauth/`: connections and their keys;
  - `core/tools/`: documents, the browser, and connected services;
  - `core/memory.py`: searching your notes;
  - `core/security.py`: the permission levels and approvals;
  - `core/gateway.py`: Telegram, Slack and WhatsApp;
  - `core/scheduler.py`: scheduled jobs.
- **Your data,** all in `vault/`:
  - `agent.db`: FreeHand's database (SQLite, with full-text search);
  - the encrypted connection keys;
  - `imports/hermes/` and `imports/openclaw/`: imported memories.

## API endpoints

A few of the main ones. Every route needs FreeHand's key (`X-API-Key: <key>`, or
`Authorization: Bearer <key>`), except signing in, pairing a Helper, the chat
apps' webhooks and the sign-in callback below, which each check something else.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/status` | Service status |
| `POST` | `/api/agent/command` | Run agent command |
| `GET` | `/api/integrations` | List integrations |
| `GET` | `/api/integrations/{service}/authorize` | Get OAuth URL |
| `GET` | `/api/integrations/{service}/callback` | OAuth callback (checks the one-time state instead of the key) |
| `POST` | `/api/integrations/{service}/disconnect` | Remove connection |
| `GET` | `/api/agents` | Detect installed agents |
| `POST` | `/api/agents/import` | Import from agent |
| `GET` | `/api/agents/imported` | Import history |

# Contributing to FreeHand

Thanks for your interest in FreeHand! This document covers how to get started.

## Development Setup

```bash
# Clone the repo
git clone https://github.com/TracySmithConsulting/freehand.git
cd freehand

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or: venv\Scripts\activate on Windows

# Install dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/

# Start the server
freehand serve
```

## Project Structure

```
freehand/
├── core/
│   ├── agents/       # Cross-agent detection and import
│   ├── oauth/        # OAuth connectors and token management
│   ├── tools/        # Agent tool wrappers
│   ├── agent.py      # Main agent loop
│   ├── agent_config.py  # LLM config and system prompt
│   ├── database.py   # SQLite schema
│   ├── gateway.py    # Telegram/Slack/WhatsApp
│   ├── memory.py     # FTS5 memory search
│   ├── scheduler.py  # Task scheduling
│   └── security.py   # Permission tiers
├── bridge/           # WhatsApp Node.js bridge
├── static/           # Web UI
├── docs/             # Documentation
├── scripts/          # Install scripts
└── tests/            # Test suite
```

## Adding a New OAuth Connector

1. Create `core/oauth/providers/<service>.py` extending `BaseConnector`
2. Implement: `authorize_url()`, `handle_callback()`, `refresh()`, `test()`, `get_user_info()`, `parse_error()`
3. Register in `core/oauth/providers/__init__.py`
4. Add agent tool wrappers in `core/tools/integrations.py`
5. Add to `list_available_tools()` in `core/agent_config.py`
6. Update tests in `tests/test_oauth.py`

## Adding a New Agent Importer

1. Create `core/agents/<agent>.py`
2. Implement `preview_<agent>()` and `import_<agent>()` functions
3. Register paths in `core/agents/detector.py`
4. Add CLI command in `cli.py`
5. Add API endpoint in `server.py`

## Code Style

- Python 3.11+ type hints
- Black formatting (if available)
- Docstrings on all public functions
- No inline comments unless explaining *why*

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):
- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation
- `test:` — tests
- `chore:` — maintenance

## Pull Requests

1. Fork the repo
2. Create a feature branch
3. Add tests for new code
4. Run `pytest tests/` — all tests must pass
5. Update CHANGELOG.md
6. Submit PR with description

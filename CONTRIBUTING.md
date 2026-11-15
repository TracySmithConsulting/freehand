# Contributing to FreeHand

Thanks for your interest in FreeHand! This document covers how to get started.

## What FreeHand cares about

Every change is judged against FreeHand's principles:

- **Accessible to everyone,** with a screen reader and a keyboard.
- **The granny test:** never assume someone has used an AI agent before.
  Tinkerers can tinker, and non-technical people can still use it.
- **Safe:** FreeHand asks before acting, keeps secrets private, and fails
  safe.
- **Economical:** careful with what's sent to the AI model, and how often.

## Before you start: open an issue

Please open an issue describing the change, and agree the idea with the
maintainer, **before** writing code. A pull request that arrives without
that may be closed, however good the code is, simply because it isn't
something FreeHand wants or has time to review.

Bug fixes with a clear explanation are always welcome.

## AI-assisted contributions

You're welcome to use AI to help write your contribution. FreeHand itself
is built with AI help. But:

- **Say so** in the pull request (the template asks), and which tool.
- **You're responsible for every line.** Read it, understand it, and test
  it yourself. "The AI wrote it" isn't an explanation for a bug.
- **Small and focused:** one change per pull request. Large, sprawling or
  machine-generated changes across many files will be closed.
- **No weakening tests to make a change pass.** See the test guard below.

## What gets merged, and what doesn't

The maintainer may close any pull request without a detailed review,
including ones that are too large, don't follow these guidelines, add
complexity without a clear need, or arrive in bulk. That isn't personal:
reviewing takes time, and FreeHand is maintained by one person.

## The checks every pull request must pass

- **The tests** (`pytest tests/`) on Windows, macOS and Linux, with two
  Python versions. GitHub runs them on every pull request, and nothing can
  be merged until they all pass.
- **The test guard** (`scripts/guard_tests.py`): a pull request may not
  have fewer tests than before, or switch tests off with `skip` or `xfail`.
  When removing tests is genuinely right (for example a feature is removed),
  the maintainer adds the label `tests-removed-on-purpose`.

Pull requests from first-time contributors need the maintainer's approval
before GitHub runs them.

## The name

If you share your own version of FreeHand, please give it its own name. See
[The FreeHand name](TRADEMARKS.md).

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

1. Open an issue and agree the idea first (see above)
2. Fork the repo
3. Create a feature branch
4. Add tests for new code
5. Run `pytest tests/` — all tests must pass
6. Submit the pull request, and fill in the template: what it changes, why,
   the checklist, and whether you used AI

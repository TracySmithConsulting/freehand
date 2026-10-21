"""Round 18: test→real-vault isolation, enforced for the whole suite.

Root cause this closes: several core modules used to bind DB_PATH /
SETTINGS_PATH at import time (`from core.database import DB_PATH`), so a test
fixture that patched core.database.DB_PATH still left security/gateway/
oauth-manager/memory writing approvals and OAuth state into the REAL
vault/agent.db and vault/settings.json. The modules now read those paths
dynamically through `core.database` / `core.agent_config`, and this autouse
fixture points every persistent-state constant at a per-test tmp directory.

Ordering matters: this fixture runs BEFORE any test-level monkeypatch, so a
test that deliberately re-points a path still wins.

If you add a new module that derives paths from vault/, make it read
core.database / core.agent_config dynamically instead of from-importing,
and add its constant here.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolate_persistent_state(tmp_path, monkeypatch):
    import core.agent_config as agent_config
    import core.database as database
    import core.gateway as gateway
    import core.memory as memory
    import core.oauth.manager as oauth_manager
    import server as server_module

    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(database, "VAULT_DIR", vault)
    monkeypatch.setattr(database, "DB_PATH", vault / "agent.db")

    monkeypatch.setattr(agent_config, "VAULT_DIR", vault)
    monkeypatch.setattr(agent_config, "SETTINGS_PATH", vault / "settings.json")

    monkeypatch.setattr(memory, "VAULT_DIR", vault)

    monkeypatch.setattr(gateway, "VAULT_DIR", vault)
    monkeypatch.setattr(gateway, "SCRIBBLE_PATH", vault / "00_Scribble.md")

    # Never generate or consume the real encryption.key.
    monkeypatch.setattr(oauth_manager, "VAULT_DIR", vault)
    monkeypatch.setattr(oauth_manager, "ENCRYPTION_KEY_PATH", vault / "encryption.key")

    # server.py from-imports DB_PATH from core.database and derives its own
    # VAULT_DIR/SCRIBBLE_PATH/VAULT_SETTINGS_PATH at import time — a fresh
    # checkout therefore resolves them to THAT checkout's (empty) vault,
    # which broke server-driven tests and polluted the checkout. Same
    # isolation net as the modules above: repoint server's constants at the
    # tmp vault per test (test-level patches can still override).
    monkeypatch.setattr(server_module, "DB_PATH", vault / "agent.db")
    monkeypatch.setattr(server_module, "VAULT_DIR", vault)
    monkeypatch.setattr(server_module, "SCRIBBLE_PATH", vault / "00_Scribble.md")
    monkeypatch.setattr(server_module, "VAULT_SETTINGS_PATH", vault / "settings.json")

    # The tmp DB always exists and always has the full schema, so any code
    # path (security, gateway, OAuth manager, MCP, server endpoints) that
    # opens core.database.DB_PATH sees a valid, EMPTY database — never the
    # real one, and never a schema-less file.
    database.init_db().close()

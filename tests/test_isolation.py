"""Round 18 regression guard: the suite must never touch the real vault.

Before the fixture-isolation fix, tests that patched core.database.DB_PATH
still leaked approval rows and OAuth pending-states into the real
vault/agent.db and vault/settings.json whenever the flow crossed a module
that had from-imported those constants at import time (security, gateway,
oauth manager, memory).

This test plants a sentinel row in the REAL database and snapshots the REAL
settings.json, runs a previously-leaky test file in a subprocess
(the subprocess inherits conftest.py's autouse isolation, which is the very
behavior under test), then asserts nothing in the real vault changed.
"""
import json
import os
import sqlite3
import sys
import unittest.mock
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_DB = PROJECT_ROOT / "vault" / "agent.db"
REAL_SETTINGS = PROJECT_ROOT / "vault" / "settings.json"
SENTINEL_DESC = "ISOLATION-SENTINEL-do-not-edit"


@pytest.fixture()
def real_vault_snapshot():
    """Plant a sentinel row; return (count, settings-bytes) to compare after."""
    if not REAL_DB.exists():
        pytest.skip("no real vault/agent.db in this checkout — nothing to protect")

    conn = sqlite3.connect(REAL_DB)
    conn.row_factory = sqlite3.Row
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    has_approvals = "approvals" in tables
    if has_approvals:
        conn.execute(
            "INSERT INTO approvals (action_type, description, payload, status) "
            "VALUES ('sentinel', ?, '{}', 'pending')",
            (SENTINEL_DESC,),
        )
        conn.commit()
        before = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    else:
        # Schema drift, not isolation: the sentinel needs the table to exist.
        conn.close()
        pytest.skip("real DB has no approvals table — unrelated environment issue")
    yield before
    conn.execute("DELETE FROM approvals WHERE description = ?", (SENTINEL_DESC,))
    conn.commit()
    conn.close()


def _run_leaky_file_subprocess():
    """Run a previously-leaky test file; returns (returncode, stdout)."""
    import subprocess
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_round17_polish.py", "-q", "-x"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode, proc.stdout


def test_leaky_file_run_leaves_real_vault_untouched(real_vault_snapshot):
    before = real_vault_snapshot
    settings_before = REAL_SETTINGS.read_bytes() if REAL_SETTINGS.exists() else None

    returncode, stdout = _run_leaky_file_subprocess()
    assert returncode == 0, f"subprocess suite failed:\n{stdout}"

    conn = sqlite3.connect(REAL_DB)
    after = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    leaked = conn.execute(
        "SELECT id, description FROM approvals WHERE description != ?",
        (SENTINEL_DESC,),
    ).fetchall()
    conn.close()

    assert after == before, (
        f"Real vault gained {after - before} approval row(s) during the test run: {leaked}"
    )
    if settings_before is not None:
        assert REAL_SETTINGS.read_bytes() == settings_before, (
            "vault/settings.json was modified during the test run "
            "(e.g. OAuth pending_states leaked)"
        )


def test_conftest_fixture_points_every_constant_at_tmp(tmp_path):
    """Direct check: the autouse fixture leaves no persistent-state constant
    pointed at the real vault while any test runs."""
    import core.agent_config as agent_config
    import core.database as database
    import core.gateway as gateway
    import core.memory as memory
    import core.oauth.manager as oauth_manager

    real_vault = str(PROJECT_ROOT / "vault")

    assert str(database.DB_PATH).startswith(str(tmp_path / "vault"))
    assert str(agent_config.SETTINGS_PATH).startswith(str(tmp_path / "vault"))
    assert str(memory.VAULT_DIR).startswith(str(tmp_path))
    assert str(gateway.VAULT_DIR).startswith(str(tmp_path))
    assert str(oauth_manager.ENCRYPTION_KEY_PATH).startswith(str(tmp_path / "vault"))
    # Belt and braces: none of them resolves to the real vault path.
    for value in (database.DB_PATH, agent_config.SETTINGS_PATH,
                  memory.VAULT_DIR, gateway.VAULT_DIR,
                  oauth_manager.VAULT_DIR, oauth_manager.ENCRYPTION_KEY_PATH):
        assert str(value) != real_vault and real_vault not in str(value)

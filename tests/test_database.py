"""Round 10 — PR 1 (vault consolidation).

Tests for the agent.db → vault/ move and the one-time migration of any
existing on-disk DB from the project root into the vault.

Round 9 left agent.db at the project root. Round 10 (PR 1) moves it to
vault/agent.db so a single Docker bind-mount covers all persistent
state. Migration runs once on first boot, idempotent, safe on empty
vault directories.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core import database  # noqa: E402


# ── DB_PATH constant ──────────────────────────────────────────────────


class TestDBPathConstant:
    """DB_PATH must point into vault/, not the project root."""

    def test_db_path_points_into_vault(self):
        """Round 9 left DB_PATH at <project_root>/agent.db. Round 10
        moves it to vault/agent.db so a single bind-mount covers all
        persistent state (vault/encryption.key + agent.db)."""
        # DB_PATH is computed relative to the source file location:
        # core/database.py → parent.parent → <project_root>, then
        # /vault/agent.db. On Windows str(Path) uses backslashes; on
        # POSIX forward slashes. Use the Path.parts check (cross-platform)
        # plus the suffix Path.name to lock the contract.
        assert database.DB_PATH.name == "agent.db"
        assert "vault" in database.DB_PATH.parts, (
            f"DB_PATH must include 'vault' in its parts: {database.DB_PATH.parts}"
        )

    def test_db_path_does_not_point_at_project_root(self):
        """Round 9 regression guard. If a future refactor moves DB_PATH
        back to the project root, this test fails."""
        # Path.parents[-1] is the immediate parent. Must be 'vault'.
        assert database.DB_PATH.parent.name == "vault", (
            f"DB_PATH parent must be 'vault', got {database.DB_PATH.parent.name!r}. "
            f"Full path: {database.DB_PATH}"
        )


# ── Migration of legacy on-disk DB ───────────────────────────────────


class TestMigrateLegacyRootDB:
    """migrate_legacy_root_db() moves a project-root agent.db into vault/.

    Idempotent: running twice is a no-op. Safe on empty vault.
    Handles WAL sidecars (agent.db-wal, agent.db-shm).
    """

    def _setup_legacy_db(self, tmp_path: Path) -> Path:
        """Create a project-root agent.db (legacy location). Returns the
        project root Path. Vault lives at tmp_path/vault, legacy DB at
        tmp_path/agent.db."""
        # Create the legacy DB at the project root
        legacy_db = tmp_path / "agent.db"
        legacy_db.write_bytes(b"FAKE_LEGACY_DB_CONTENT")
        # WAL sidecar files (SQLite creates these alongside the main DB
        # when WAL mode is on). Must move with the main DB or SQLite
        # will refuse to open the migrated copy.
        (tmp_path / "agent.db-wal").write_bytes(b"legacy_wal")
        (tmp_path / "agent.db-shm").write_bytes(b"legacy_shm")
        # Vault exists but has nothing inside
        vault = tmp_path / "vault"
        vault.mkdir()
        return tmp_path

    def test_migrate_legacy_root_db_moves_main_and_sidecars(self, tmp_path, monkeypatch):
        """The happy case: legacy DB at project root, vault exists but
        empty. Migration moves main + wal + shm into vault/."""
        self._setup_legacy_db(tmp_path)
        # Patch DATABASE.PROJECT_ROOT and VAULT so the migration reads
        # the test temp paths. This is the only way to test the
        # migration without faking the source file location.
        from core import agent_config
        monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(database, "VAULT_DIR", tmp_path / "vault")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "vault" / "agent.db")
        # Patch MAULT_PATH constant in database module so migrate
        # writes to our test vault, not the real one.
        # Run migration
        result = database.migrate_legacy_root_db()
        assert result is True, "Migration should report True when it moved files"
        # Main file moved
        assert (tmp_path / "vault" / "agent.db").exists(), (
            "Migration must move the main agent.db into vault/"
        )
        assert (tmp_path / "vault" / "agent.db").read_bytes() == b"FAKE_LEGACY_DB_CONTENT", (
            "Main file contents must be preserved"
        )
        # WAL sidecars moved with it
        assert (tmp_path / "vault" / "agent.db-wal").read_bytes() == b"legacy_wal"
        assert (tmp_path / "vault" / "agent.db-shm").read_bytes() == b"legacy_shm"
        # Source removed
        assert not (tmp_path / "agent.db").exists(), (
            "Migration must remove the legacy file (not copy)"
        )
        assert not (tmp_path / "agent.db-wal").exists()
        assert not (tmp_path / "agent.db-shm").exists()

    def test_migrate_legacy_root_db_is_idempotent(self, tmp_path, monkeypatch):
        """Running migration twice is a no-op the second time."""
        self._setup_legacy_db(tmp_path)
        monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(database, "VAULT_DIR", tmp_path / "vault")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "vault" / "agent.db")
        # First call: moves files
        first = database.migrate_legacy_root_db()
        assert first is True
        # Second call: nothing to move (no legacy file exists anymore)
        second = database.migrate_legacy_root_db()
        assert second is False, (
            "Second call should be a no-op (no legacy file to move)"
        )

    def test_migrate_legacy_root_db_creates_vault_if_missing(self, tmp_path, monkeypatch):
        """If the vault directory doesn't exist yet (fresh install),
        it gets created and the legacy DB moves into it."""
        # Legacy DB exists at project root, vault/ does NOT exist
        legacy_db = tmp_path / "agent.db"
        legacy_db.write_bytes(b"FRESH_INSTALL")
        # Don't create tmp_path/"vault" — test the "missing vault" case
        monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(database, "VAULT_DIR", tmp_path / "vault")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "vault" / "agent.db")
        result = database.migrate_legacy_root_db()
        assert result is True
        assert (tmp_path / "vault").is_dir(), "Migration must create vault/ if missing"
        assert (tmp_path / "vault" / "agent.db").exists()

    def test_migrate_legacy_root_db_noop_when_nothing_to_migrate(self, tmp_path, monkeypatch):
        """Both project root and vault are clean — nothing to migrate.
        Returns False, no error."""
        # No legacy DB, vault exists
        (tmp_path / "vault").mkdir()
        monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(database, "VAULT_DIR", tmp_path / "vault")
        monkeypatch.setattr(database, "DB_PATH", tmp_path / "vault" / "agent.db")
        result = database.migrate_legacy_root_db()
        assert result is False, (
            "Nothing-to-migrate case should return False, not raise"
        )

    def test_migrate_legacy_root_db_noop_when_vault_already_has_db(self, tmp_path, monkeypatch):
        """Vault already has agent.db (user did a manual copy). Migration
        should NOT overwrite the vault copy — preserve user data. The
        legacy copy at the project root gets deleted (no two copies),
        the vault copy is untouched."""
        # Both legacy and vault DBs exist
        legacy_db = tmp_path / "agent.db"
        legacy_db.write_bytes(b"LEGACY_CONTENT")
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "agent.db").write_bytes(b"USER_PRESERVED")
        monkeypatch.setattr(database, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(database, "VAULT_DIR", vault)
        monkeypatch.setattr(database, "DB_PATH", vault / "agent.db")
        result = database.migrate_legacy_root_db()
        assert result is False, (
            "When vault already has agent.db, skip migration to preserve"
            " user data. Return False (no-op)."
        )
        # Vault DB unchanged
        assert (vault / "agent.db").read_bytes() == b"USER_PRESERVED"
        # Legacy DB removed (not preserved — avoids two diverging copies)
        assert not legacy_db.exists(), (
            "Legacy DB must be removed when vault already has its copy,"
            " otherwise two copies can diverge. User can recover from"
            " a backup if they need the legacy content."
        )
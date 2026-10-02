"""Tests for the legacy connections.db → credential_store migration.

Round 10 PR 2 task 2.3: pre-Round-10 OAuth connections (stored in
vault/connections.db with Fernet-encrypted token_data) need to be
migrated to vault/credential_store.json on first boot so the broker
+ tool layer can find them under the new (service, label) shape.

Migration semantics:
- Reads vault/connections.db (the legacy OAuth store from Rounds 5-9).
- For each row, writes a credential_store entry with
  auth_type='oauth' and the original (service, label) pair.
- Idempotent: running twice is a no-op. The migration marks itself
  done via a sentinel in vault/credential_store.json itself
  (the '_migrated' key) so the migration never re-runs.

Security:
- token_data stays Fernet-encrypted during the move. We decrypt to
  the original plaintext, then re-encrypt into the new store. (The
  legacy ciphertext and the new ciphertext are independent Fernet
  outputs against the same key.)
- The original vault/connections.db file is NOT deleted. The
  connections table will keep working until Round 11+ removes it.
  This is belt-and-suspenders for the migration story.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.oauth import credential_store, manager  # noqa: E402
from core.oauth.manager import encrypt  # noqa: E402


def _setup_legacy_connections_db(vault: Path, rows: list[dict]) -> Path:
    """Create a legacy vault/connections.db with the given rows.

    Each row is a dict with keys: service, label, token_data, scopes.
    The token_data is stored Fernet-encrypted (matching the legacy
    format from core.oauth.manager.save_connection).
    """
    db_path = vault / "connections.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL DEFAULT '',
            token_data TEXT NOT NULL,
            scopes TEXT NOT NULL,
            connected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_refreshed TEXT,
            expires_at TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            UNIQUE(service, label)
        );
    """)
    for row in rows:
        encrypted = encrypt(json.dumps(row["token_data"]))
        conn.execute(
            """INSERT INTO connections
               (service, label, token_data, scopes, status)
               VALUES (?, ?, ?, ?, 'active')""",
            (row["service"], row["label"], encrypted, row["scopes"]),
        )
    conn.commit()
    conn.close()
    return db_path


def _isolated_credential_store(tmp_path, monkeypatch):
    """Redirect credential_store to a tmp vault/credential_store.json."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(credential_store, "VAULT_DIR", vault)
    monkeypatch.setattr(credential_store, "STORAGE_PATH", vault / "credential_store.json")
    monkeypatch.setattr(manager, "VAULT_DIR", vault)
    monkeypatch.setattr(manager, "DB_PATH", vault / "connections.db")
    return vault


# ── Happy path: row migrates ──────────────────────────────────────────


class TestMigrateLegacyConnectionsDB:
    def test_migrates_one_row(self, tmp_path, monkeypatch):
        """One row in legacy connections.db → one entry in credential_store.json.
        auth_type is 'oauth' (the legacy store was OAuth-only)."""
        vault = _isolated_credential_store(tmp_path, monkeypatch)
        _setup_legacy_connections_db(vault, [{
            "service": "slack",
            "label": "tracy",
            "token_data": {
                "access_token": "xoxb-FAKE-LEGACY",
                "scope": "chat:write,channels:read",
                "team": {"id": "T1", "name": "Test"},
            },
            "scopes": "chat:write,channels:read",
        }])

        migrated = manager.migrate_legacy_connections_db()
        assert migrated == 1

        # The credential is now in the new store
        entry = credential_store.get("slack", "tracy")
        assert entry is not None
        assert entry["auth_type"] == "oauth"
        # The decrypted secret is the original token_data JSON-serialized
        assert json.loads(entry["secret"]) == {
            "access_token": "xoxb-FAKE-LEGACY",
            "scope": "chat:write,channels:read",
            "team": {"id": "T1", "name": "Test"},
        }

    def test_migrates_multiple_rows(self, tmp_path, monkeypatch):
        """Multiple rows in legacy db all migrate."""
        vault = _isolated_credential_store(tmp_path, monkeypatch)
        _setup_legacy_connections_db(vault, [
            {"service": "slack", "label": "tracy",
             "token_data": {"access_token": "xoxb-A"}, "scopes": "chat:write"},
            {"service": "google", "label": "work",
             "token_data": {"access_token": "ya-B"}, "scopes": "email"},
            {"service": "microsoft", "label": "default",
             "token_data": {"access_token": "ya-C"}, "scopes": "User.Read"},
        ])

        migrated = manager.migrate_legacy_connections_db()
        assert migrated == 3
        assert credential_store.get("slack", "tracy") is not None
        assert credential_store.get("google", "work") is not None
        assert credential_store.get("microsoft", "default") is not None


# ── Idempotency ────────────────────────────────────────────────────────


class TestMigrationIdempotency:
    def test_second_run_is_noop(self, tmp_path, monkeypatch):
        """Run once: migrates. Run again: zero migrations.
        The _migrated sentinel in credential_store.json gates the run.
        """
        vault = _isolated_credential_store(tmp_path, monkeypatch)
        _setup_legacy_connections_db(vault, [{
            "service": "slack", "label": "tracy",
            "token_data": {"access_token": "xoxb-FIRST"}, "scopes": "chat:write",
        }])

        first = manager.migrate_legacy_connections_db()
        assert first == 1
        second = manager.migrate_legacy_connections_db()
        assert second == 0, "Second call must be a no-op (sentinel set)"

    def test_sentinel_persists_across_calls(self, tmp_path, monkeypatch):
        """The _migrated marker is read from disk each time, not cached
        in memory. A fresh Python interpreter (or after process restart)
        still sees the migration as done.
        """
        vault = _isolated_credential_store(tmp_path, monkeypatch)
        _setup_legacy_connections_db(vault, [{
            "service": "slack", "label": "tracy",
            "token_data": {"access_token": "xoxb-X"}, "scopes": "chat:write",
        }])

        manager.migrate_legacy_connections_db()
        # Now clear in-memory state and re-run — sentinel should still
        # be on disk, so the second call returns 0.
        credential_store._read_storage.cache_clear() if hasattr(
            credential_store._read_storage, "cache_clear"
        ) else None
        second = manager.migrate_legacy_connections_db()
        assert second == 0


# ── Edge cases ────────────────────────────────────────────────────────


class TestMigrationEdgeCases:
    def test_no_legacy_db_means_nothing_to_migrate(self, tmp_path, monkeypatch):
        """Fresh install — no connections.db, no migration."""
        vault = _isolated_credential_store(tmp_path, monkeypatch)
        # Don't create connections.db
        migrated = manager.migrate_legacy_connections_db()
        assert migrated == 0

    def test_empty_legacy_db_means_nothing_to_migrate(self, tmp_path, monkeypatch):
        """connections.db exists but has zero rows."""
        vault = _isolated_credential_store(tmp_path, monkeypatch)
        # Create the schema but no rows
        db_path = vault / "connections.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT 'default',
                user_id TEXT NOT NULL DEFAULT '',
                token_data TEXT NOT NULL,
                scopes TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );
        """)
        conn.commit()
        conn.close()
        migrated = manager.migrate_legacy_connections_db()
        assert migrated == 0

    def test_skip_migration_flag(self, tmp_path, monkeypatch):
        """A CLI flag exists to skip the migration entirely (for users
        who want to start fresh). With skip=True, no rows migrate.
        """
        vault = _isolated_credential_store(tmp_path, monkeypatch)
        _setup_legacy_connections_db(vault, [{
            "service": "slack", "label": "tracy",
            "token_data": {"access_token": "xoxb-X"}, "scopes": "chat:write",
        }])
        migrated = manager.migrate_legacy_connections_db(skip=True)
        assert migrated == 0
        # Credential not in store
        assert credential_store.get("slack", "tracy") is None


# ── Coexistence with existing credential_store entries ────────────────


class TestCoexistence:
    def test_existing_entries_are_preserved(self, tmp_path, monkeypatch):
        """If credential_store already has a (slack, tracy) entry
        (added by a NEW registration), the migration must NOT overwrite.
        """
        vault = _isolated_credential_store(tmp_path, monkeypatch)
        # Pre-populate credential_store with a NEW slack entry
        credential_store.add("slack", "tracy", "xoxb-NEW-FROM-API", "oauth")
        # Set up legacy db with the same (slack, tracy) key
        _setup_legacy_connections_db(vault, [{
            "service": "slack", "label": "tracy",
            "token_data": {"access_token": "xoxb-LEGACY"}, "scopes": "chat:write",
        }])

        migrated = manager.migrate_legacy_connections_db()
        # Migration SKIPPED the existing entry — the new value is intact
        assert migrated == 0, "Should skip rows that already exist"
        entry = credential_store.get("slack", "tracy")
        assert entry["secret"] == "xoxb-NEW-FROM-API"

    def test_migration_adds_only_missing_rows(self, tmp_path, monkeypatch):
        """Some rows pre-exist in credential_store, some are new from
        the legacy db. Migration adds only the new ones.
        """
        vault = _isolated_credential_store(tmp_path, monkeypatch)
        # Pre-existing row uses auth_type=api_key with a plain string
        # secret (the typical CLI 'add' use case). NOT a JSON dict.
        credential_store.add("slack", "tracy", "xoxb-NEW", "api_key")
        _setup_legacy_connections_db(vault, [
            {"service": "slack", "label": "tracy",
             "token_data": {"access_token": "xoxb-LEGACY-1"}, "scopes": "chat:write"},
            {"service": "google", "label": "work",
             "token_data": {"access_token": "ya-LEGACY-2"}, "scopes": "email"},
        ])
        migrated = manager.migrate_legacy_connections_db()
        # 1 new (google/work), 1 skipped (slack/tracy already exists)
        assert migrated == 1
        # Both present, no overwrites
        # slack/tracy keeps the original api_key secret (plain string)
        assert credential_store.get("slack", "tracy")["secret"] == "xoxb-NEW"
        # google/work is the migrated row — OAuth token_data, JSON-encoded
        assert json.loads(credential_store.get("google", "work")["secret"]) == {
            "access_token": "ya-LEGACY-2"
        }

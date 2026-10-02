import sqlite3
from pathlib import Path
from datetime import datetime


# Round 10 (PR 1): agent.db moved from project root into vault/ so a
# single Docker bind-mount covers ALL persistent state (encryption.key,
# connections.db, broker_config.json, credential_store.json, agent.db).
PROJECT_ROOT = Path(__file__).parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
DB_PATH = VAULT_DIR / "agent.db"


def migrate_legacy_root_db() -> bool:
    """One-time migration: move <project_root>/agent.db into vault/.

    Round 9 and earlier stored agent.db at the project root. Round 10
    consolidated persistent state into vault/. This function detects a
    legacy on-disk DB at the project root and moves it (main file +
    WAL sidecars) into vault/. Idempotent — running twice is a no-op
    once the file has been moved.

    Returns True if a file was moved, False otherwise.

    Safety rules:
    - If vault/agent.db already exists (user manually moved), DON'T
      overwrite. Leave the user's copy alone; remove the legacy file
      so we don't end up with two copies.
    - Move both the main file and the WAL sidecars (agent.db-wal,
      agent.db-shm). Missing them would make SQLite reject the
      migrated DB.
    - WAL mode means the main file might not contain all committed
      data on disk; the -wal file has the rest. Both must move.
    """
    legacy = PROJECT_ROOT / "agent.db"
    target = VAULT_DIR / "agent.db"

    # No legacy file: nothing to do.
    if not legacy.exists():
        return False

    # Vault already has agent.db — don't overwrite user data.
    # Remove the legacy to avoid two copies diverging.
    if target.exists():
        legacy.unlink()
        wal = PROJECT_ROOT / "agent.db-wal"
        shm = PROJECT_ROOT / "agent.db-shm"
        if wal.exists():
            wal.unlink()
        if shm.exists():
            shm.unlink()
        return False

    # Create vault/ if it doesn't exist (fresh install case).
    VAULT_DIR.mkdir(parents=True, exist_ok=True)

    # Move main file + WAL sidecars.
    legacy.rename(target)
    for suffix in ("-wal", "-shm"):
        legacy_sidecar = PROJECT_ROOT / f"agent.db{suffix}"
        if legacy_sidecar.exists():
            legacy_sidecar.rename(VAULT_DIR / f"agent.db{suffix}")

    return True


def init_db() -> sqlite3.Connection:
    """Initialize SQLite database with FTS5 enabled and create required tables.

    NOTE: One-time migration of legacy agent.db (Round 9 and earlier
    stored it at the project root) into vault/agent.db. This used to
    be called here, but that broke test fixtures that monkeypatch
    DB_PATH — the migration would touch the REAL project root, not
    the test's tmp_path. The migration is now wired into
    server.py:startup_event so it runs ONCE per app boot, not on every
    init_db() call. Tests that need to trigger it explicitly can call
    migrate_legacy_root_db() themselves.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            title,
            content,
            content='memories',
            content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, title, content)
            VALUES (new.id, new.title, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, title, content)
            VALUES ('delete', old.id, old.title, old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, title, content)
            VALUES ('delete', old.id, old.title, old.content);
            INSERT INTO memories_fts(rowid, title, content)
            VALUES (new.id, new.title, new.content);
        END;

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            cron_schedule TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            last_run TEXT,
            next_run TEXT
        );

        CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_type TEXT NOT NULL,
            description TEXT NOT NULL,
            payload TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS remote_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            user_id TEXT NOT NULL,
            last_active TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

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

        CREATE TABLE IF NOT EXISTS imported_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            source_path TEXT NOT NULL,
            file_type TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            vault_path TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            file_size INTEGER
        );

        CREATE TABLE IF NOT EXISTS import_manifest (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            files_imported INTEGER NOT NULL DEFAULT 0,
            skills_imported INTEGER NOT NULL DEFAULT 0,
            state_extracted INTEGER NOT NULL DEFAULT 0,
            vault_root TEXT NOT NULL
        );
    """)

    conn.commit()
    return conn


if __name__ == "__main__":
    conn = init_db()
    query = 'SELECT sql FROM sqlite_master WHERE type="table"'
    tables = conn.execute(query).fetchall()
    print(f"Database initialized at {DB_PATH}")
    print(f"Tables: {[t[0].split()[2] for t in tables]}")
    conn.close()

import sqlite3
from pathlib import Path
from datetime import datetime


DB_PATH = Path(__file__).parent.parent / "agent.db"


def init_db() -> sqlite3.Connection:
    """Initialize SQLite database with FTS5 enabled and create required tables."""
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

"""Merger: persist import results into FreeHand database."""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from core.database import DB_PATH
from core.agents.models import ImportResult


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def record_import(result: ImportResult) -> int:
    """Record an import in the database. Returns manifest_id."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys=ON")

    for entry in result.files_imported:
        vault_path = Path(entry["vault"])
        conn.execute(
            """
            INSERT INTO imported_sources
                (agent, source_path, file_type, content_hash, vault_path, imported_at, file_size)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.agent,
                entry["source"],
                entry["type"],
                _file_hash(vault_path),
                entry["vault"],
                now,
                entry["size"],
            ),
        )

    cursor = conn.execute(
        """
        INSERT INTO import_manifest
            (agent, imported_at, files_imported, skills_imported, state_extracted, vault_root)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            result.agent,
            now,
            len(result.files_imported),
            len(result.skills_imported),
            1 if result.state_extracted else 0,
            str(result.vault_root),
        ),
    )
    manifest_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return manifest_id


def list_imports() -> List[dict]:
    """List all import manifests."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, agent, imported_at, files_imported, skills_imported,
               state_extracted, vault_root
        FROM import_manifest
        ORDER BY imported_at DESC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_imported_sources(agent: str = None) -> List[dict]:
    """List imported source files, optionally filtered by agent."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    if agent:
        rows = conn.execute(
            "SELECT * FROM imported_sources WHERE agent = ? ORDER BY imported_at DESC",
            (agent,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM imported_sources ORDER BY imported_at DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_import(agent: str) -> bool:
    """Remove all imported sources for an agent. Returns True if anything was deleted."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.execute(
        "DELETE FROM imported_sources WHERE agent = ?", (agent,)
    )
    deleted = cursor.rowcount > 0
    conn.execute("DELETE FROM import_manifest WHERE agent = ?", (agent,))
    conn.commit()
    conn.close()
    return deleted


def get_import_status() -> dict:
    """Get a summary of all imports."""
    imports = list_imports()
    sources = list_imported_sources()
    return {
        "total_imports": len(imports),
        "total_files": sum(i.get("files_imported", 0) for i in imports),
        "total_skills": sum(i.get("skills_imported", 0) for i in imports),
        "agents": list({i["agent"] for i in imports}),
        "imports": imports,
        "sources": sources,
    }
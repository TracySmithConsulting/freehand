"""Selective memory extraction from Hermes state.db.

Hermes state.db is large (~400MB). Instead of copying the whole file, we:
1. Open it read-only
2. Query only user/project-relevant tables
3. Export to readable markdown

Tables to skip: logs, events, cache, telemetry, crashes, runtime state.
Tables to keep: user_profile, memories, session_summaries, facts, learned_skills, project_context.
"""

import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

KEEP_TABLE_KEYWORDS = (
    "user", "profile", "memory", "memories", "fact",
    "learned", "skill", "session_summary", "project",
    "context", "preference", "personality", "trait",
    "identity", "preference", "note", "journal",
    "conversation", "message_meta",
)

SKIP_TABLE_KEYWORDS = (
    "log", "event", "metric", "cache", "telemetry",
    "crash", "runtime", "tool_call", "raw_message",
    "embedding", "raw_event", "audit", "trace",
)

MAX_ROWS_PER_TABLE = 100


def _classify_table(name: str) -> str:
    """Return 'keep', 'skip', or 'unknown'."""
    lower = name.lower()
    for kw in SKIP_TABLE_KEYWORDS:
        if kw in lower:
            return "skip"
    for kw in KEEP_TABLE_KEYWORDS:
        if kw in lower:
            return "keep"
    return "unknown"


def _table_has_meaningful_data(conn, table: str) -> bool:
    """Check if table has rows and at least one text column."""
    try:
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
        count = cursor.fetchone()[0]
        if count == 0:
            return False
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        for col in cols:
            col_type = (col[2] or "").upper()
            if "TEXT" in col_type or "BLOB" in col_type or "VARCHAR" in col_type:
                return True
        return False
    except Exception:
        return False


def _format_row_as_text(conn, table: str) -> List[Dict]:
    """Read up to MAX_ROWS_PER_TABLE rows from a table, skip embeddings/BLOBs."""
    rows = conn.execute(
        f"SELECT * FROM {table} LIMIT {MAX_ROWS_PER_TABLE}"
    ).fetchall()
    col_info = conn.execute("PRAGMA table_info(" + table + ")").fetchall()
    cols = [d[1] for d in col_info]  # PRAGMA: cid=0, name=1, type=2, ...
    skip_cols = {i for i, c in enumerate(col_info) if any(
        kw in (c[1] or "").lower() for kw in ("embedding", "blob", "raw_", "vector")
    )}
    results = []
    for row in rows:
        record = {}
        for i, col in enumerate(cols):
            if i in skip_cols:
                record[col] = "<skipped>"
            else:
                val = row[i]
                if isinstance(val, bytes):
                    val = f"<binary {len(val)} bytes>"
                record[col] = val
        results.append(record)
    return results


def _format_md_table(records: List[Dict]) -> str:
    if not records:
        return "_no rows_\n"
    cols = list(records[0].keys())
    md = "| " + " | ".join(cols) + " |\n"
    md += "| " + " | ".join(["---"] * len(cols)) + " |\n"
    for rec in records:
        row_vals = []
        for col in cols:
            val = rec.get(col, "")
            s = str(val) if val is not None else ""
            s = s.replace("|", "\\|").replace("\n", " ").strip()
            if len(s) > 100:
                s = s[:97] + "..."
            row_vals.append(s)
        md += "| " + " | ".join(row_vals) + " |\n"
    return md


def extract_hermes_state(db_path: Path, output_path: Path) -> Tuple[int, List[str]]:
    """Extract user/project memory from Hermes state.db to a markdown file.

    Returns (tables_extracted, errors).
    """
    errors = []
    extracted = []

    if not db_path.exists():
        return 0, [f"state.db not found: {db_path}"]

    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except Exception as e:
        return 0, [f"Failed to open state.db: {e}"]

    try:
        all_tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
    except Exception as e:
        conn.close()
        return 0, [f"Failed to read tables: {e}"]

    sections = [f"# Hermes State Memory (extracted from state.db)\n"]
    sections.append(f"Extracted: {sqlite3.datetime.datetime.now().isoformat()}\n")
    sections.append(f"Source: `{db_path}`\n")
    sections.append(f"Total tables in DB: {len(all_tables)}\n\n")

    for table in all_tables:
        classification = _classify_table(table)
        if classification == "skip":
            continue
        if not _table_has_meaningful_data(conn, table):
            continue

        try:
            records = _format_row_as_text(conn, table)
            if not records:
                continue
            sections.append(f"## {table}\n\n")
            sections.append(_format_md_table(records))
            sections.append("\n")
            extracted.append(table)
        except Exception as e:
            errors.append(f"Failed to extract {table}: {e}")

    conn.close()
    output_path.write_text("".join(sections), encoding="utf-8")
    return len(extracted), errors


def get_state_db_tables(db_path: Path) -> List[Dict]:
    """List all tables in the Hermes state.db with their classifications."""
    if not db_path.exists():
        return []
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        all_tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        result = []
        for table in all_tables:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                count = -1
            result.append({
                "name": table,
                "classification": _classify_table(table),
                "row_count": count,
            })
        conn.close()
        return result
    except Exception:
        return []
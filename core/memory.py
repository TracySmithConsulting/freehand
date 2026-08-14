import sqlite3
import os
import re
from pathlib import Path
from typing import List, Dict, Optional


PROJECT_ROOT = Path(__file__).parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
DB_PATH = PROJECT_ROOT / "agent.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def sync_vault_to_sqlite() -> int:
    """Scan all .md files in /vault (including /vault/Threads) and sync
    their content into the SQLite memories FTS table.

    Returns the number of files synced.
    """
    conn = _get_conn()
    cursor = conn.cursor()

    md_files: List[Path] = []
    # R10 fix: skip the scratchpad and other known-noise files. These
    # are either actively transient (00_Scribble.md is constantly edited
    # by the agent loop) or not knowledge to surface in memory search.
    SKIP_FILENAMES = {"00_Scribble.md", ".sweep_state.json"}
    for root, _dirs, files in os.walk(VAULT_DIR):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            if fname in SKIP_FILENAMES:
                continue
            md_files.append(Path(root) / fname)

    synced = 0
    for md_path in md_files:
        rel_path = str(md_path.relative_to(PROJECT_ROOT))
        try:
            content = md_path.read_text(encoding="utf-8")
        except Exception:
            continue

        title = md_path.stem
        title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()

        updated_at = md_path.stat().st_mtime
        import datetime
        updated_at_str = datetime.datetime.fromtimestamp(
            updated_at
        ).isoformat()

        existing = cursor.execute(
            "SELECT id FROM memories WHERE path = ?", (rel_path,)
        ).fetchone()

        if existing:
            cursor.execute(
                "UPDATE memories SET title = ?, content = ?, updated_at = ? WHERE path = ?",
                (title, content, updated_at_str, rel_path),
            )
        else:
            cursor.execute(
                "INSERT INTO memories (path, title, content, updated_at) VALUES (?, ?, ?, ?)",
                (rel_path, title, content, updated_at_str),
            )
        synced += 1

    conn.commit()
    conn.close()
    return synced


def search_memory(query: str, limit: int = 3) -> List[Dict]:
    """Search the FTS5 memories index for `query` and return the top
    `limit` most relevant markdown file records as dicts.

    R5 fix: FTS5 MATCH operator is injection-sensitive. Untrusted input
    containing tokens like `*`, `"`, `OR`, `NOT`, `:`, `^` is interpreted
    as FTS5 syntax and can return every row, fail with a parse error, or
    crash the query. We sanitise by quoting the entire input as a phrase
    after escaping internal double-quotes.
    """
    conn = _get_conn()
    cursor = conn.cursor()

    if not query.strip():
        conn.close()
        return []

    # R5: wrap query in double quotes (phrase query) and escape any
    # internal double-quotes by doubling them. FTS5 phrase query is
    # interpreted literally — no operators, no wildcards.
    safe_query = '"' + query.replace('"', '""') + '"'

    try:
        cursor.execute(
            """
            SELECT m.id, m.path, m.title, m.updated_at,
                   memories_fts.rank AS relevance
            FROM memories_fts
            JOIN memories m ON m.id = memories_fts.rowid
            WHERE memories_fts MATCH ?
            ORDER BY memories_fts.rank
            LIMIT ?
            """,
            (safe_query, limit),
        )
        results = [dict(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError:
        # FTS5 parse error (shouldn't happen with phrase query, but
        # defensive). Return empty rather than crash the agent loop.
        results = []
    finally:
        conn.close()
    return results


def parse_skills(skills_dir: Optional[Path] = None) -> List[Dict]:
    """Parse all SKILL.md files under `skills_dir` and return a list of
    skill dictionaries with frontmatter fields.

    Each dict contains:
        - name        (str)   : frontmatter 'name'
        - description (str)   : frontmatter 'description'
        - body        (str)   : markdown after the frontmatter block
        - path        (str)   : file path relative to PROJECT_ROOT
    """
    if skills_dir is None:
        skills_dir = PROJECT_ROOT / "skills"

    skills: List[Dict] = []

    if not skills_dir.exists():
        return skills

    # R11 fix: compute the resolved skills_dir root once and reject
    # any skill subdir that resolves outside it. Defends against future
    # changes (or symlinks) that could let a malicious SKILL.md escape
    # the skills/ boundary.
    skills_root_resolved = skills_dir.resolve()

    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        try:
            # Reject any skill_dir that escapes the skills_root.
            if not skill_dir.resolve().is_relative_to(skills_root_resolved):
                continue
        except (OSError, ValueError):
            continue

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        # Also verify SKILL.md itself is inside the skill_dir after resolve.
        try:
            if not skill_md.resolve().is_relative_to(skill_dir.resolve()):
                continue
        except (OSError, ValueError):
            continue

        try:
            text = skill_md.read_text(encoding="utf-8")
        except Exception:
            continue

        name = ""
        description = ""
        body = text

        frontmatter_match = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
        if frontmatter_match:
            fm_text = frontmatter_match.group(1)
            body = text[frontmatter_match.end():]

            name_match = re.search(r"^name:\s*(.+)$", fm_text, re.MULTILINE)
            if name_match:
                name = name_match.group(1).strip().strip('"').strip("'")

            desc_match = re.search(
                r"^description:\s*(.+)$", fm_text, re.MULTILINE
            )
            if desc_match:
                description = desc_match.group(1).strip().strip('"').strip("'")

        rel_path = str(skill_md.relative_to(PROJECT_ROOT))
        skills.append({
            "name": name,
            "description": description,
            "body": body.strip(),
            "path": rel_path,
        })

    return skills


if __name__ == "__main__":
    print("Syncing vault to SQLite...")
    count = sync_vault_to_sqlite()
    print(f"  Synced {count} file(s)")

    print("\nParsed skills:")
    for skill in parse_skills():
        print(f"  - {skill['name']}: {skill['description'][:60]}...")

    print("\nSearch test 'agent':")
    results = search_memory("agent")
    for r in results:
        print(f"  [{r['path']}] {r['title']} (rank={r['relevance']:.2f})")

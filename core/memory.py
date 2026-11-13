import sqlite3
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from core import database  # canonical vault/agent.db path (Round 10 PR 1)


PROJECT_ROOT = Path(__file__).parent.parent
from core import agent_config
VAULT_DIR = agent_config.VAULT_DIR


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(database.DB_PATH)
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
    # Nov 2026: the scribble pad (00_Scribble.md) IS indexed now - it used
    # to be skipped, so "what did I jot down about ...?" missed anything
    # not yet swept into Threads/Journal. Skills are instructions, not
    # notes, so they stay out (they have their own page).
    SKIP_FILENAMES = {".sweep_state.json"}
    skills_dir = (Path(VAULT_DIR) / "skills").resolve()
    for root, _dirs, files in os.walk(VAULT_DIR):
        if Path(root).resolve().is_relative_to(skills_dir):
            continue
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
        if md_path.name == "00_Scribble.md":
            title = "Scribble pad"

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

    # Forget files that are gone (a deleted note shouldn't keep turning up).
    # Agent command records (path '/agent/command') aren't files; keep them.
    present = {str(p.relative_to(PROJECT_ROOT)) for p in md_files}
    for (row_path,) in cursor.execute("SELECT path FROM memories WHERE path != '/agent/command'").fetchall():
        if row_path not in present:
            cursor.execute("DELETE FROM memories WHERE path = ?", (row_path,))

    conn.commit()
    conn.close()
    return synced


_STOPWORDS = frozenset(
    "a an and are about at be but by did do does for from had has have i if in into is it its "
    "jot jotted me my of on or our so that the their them then there these they this to "
    "up was we were what when where which who why will with wrote write written you your "
    "note notes noted scribble scribbled scribbles down".split()
)


def _fts_terms(query: str) -> List[str]:
    """Words worth searching for, each as a safe FTS5 prefix term.

    Every word is quoted (so FTS5 operators in the input are just text)
    and gets a trailing * so 'plumber' also finds 'plumbers'."""
    words = re.findall(r"[^\W_]+", query.lower())
    keep = [w for w in words if w not in _STOPWORDS and len(w) > 1] or [w for w in words if len(w) > 1]
    seen, terms = set(), []
    for w in keep:
        if w not in seen:
            seen.add(w)
            terms.append('"' + w.replace('"', '""') + '"*')
    return terms[:12]


def search_memory(query: str, limit: int = 3, sync: bool = False) -> List[Dict]:
    """Search the user's scribbles and notes (scribble pad, Journal, topic
    Threads, imported notes) and return the best `limit` matches.

    Nov 2026: by words, not exact phrase. "What did I jot down about the
    plumber?" used to need a note containing that exact phrase; now every
    meaningful word is matched in any order, word starts included. If no
    note has all the words, notes with any of them are returned. Each
    result has a short ``snippet`` around the match.

    R5 (still holds): user text is never FTS5 syntax - each word is quoted
    (see _fts_terms), so `*`, `"`, `OR`, `NOT`, `:`, `^` are just text.

    sync=True re-reads the vault first, so a scribble written a moment ago
    is found.
    """
    if not query.strip():
        return []
    if sync:
        try:
            sync_vault_to_sqlite()
        except (OSError, sqlite3.Error):
            pass
    terms = _fts_terms(query)
    if not terms:
        return []

    conn = _get_conn()
    cursor = conn.cursor()
    results: List[Dict] = []
    try:
        for joiner in (" AND ", " OR "):
            cursor.execute(
                """
                SELECT m.id, m.path, m.title, m.updated_at,
                       snippet(memories_fts, 1, '', '', ' ... ', 24) AS snippet,
                       memories_fts.rank AS relevance
                FROM memories_fts
                JOIN memories m ON m.id = memories_fts.rowid
                WHERE memories_fts MATCH ? AND m.path != '/agent/command'
                ORDER BY memories_fts.rank
                LIMIT ?
                """,
                (joiner.join(terms), limit),
            )
            results = [dict(row) for row in cursor.fetchall()]
            if results or len(terms) == 1:
                break
    except sqlite3.OperationalError:
        # Defensive: a malformed MATCH must never crash the agent loop.
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

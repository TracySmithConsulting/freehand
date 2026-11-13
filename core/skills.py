"""Skills that earn their place (Nov 2026).

A skill is a SKILL.md file (Hermes / Agent Skills format: front matter with
``name`` and ``description``, then instructions) under ``vault/skills/``.
Files stay as they are, so they keep working in Hermes; FreeHand keeps its
own record of each one in agent.db (table ``skills_state``).

Why: agents that write skills freely leave a folder of junk - skills that
never worked, or were used once. Here every skill has to earn its place:

- New skills start as **drafts**: ones FreeHand writes (``save``), ones
  imported from other agents, ones copied in by hand.
- FreeHand sees drafts too, marked "untested", and reports after using a
  skill whether it worked (``record_outcome``).
- A draft becomes **active** after ``PROMOTE_AFTER`` successes, or when
  the person approves it on the Skills page.
- A skill is **archived** after repeated failures in a row, or, for
  drafts, when unused for ``STALE_DRAFT_DAYS`` days. Archived skills are
  hidden from FreeHand but kept, and can be restored.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

PROMOTE_AFTER = 3
ARCHIVE_DRAFT_AFTER_FAILURES = 2
ARCHIVE_ACTIVE_AFTER_FAILURES = 3
STALE_DRAFT_DAYS = 30
MAX_SKILL_CHARS = 20000
PROMPT_LIST_LIMIT = 40

STATUSES = ("draft", "active", "archived")


def _root() -> Path:
    from core import database
    return Path(database.VAULT_DIR) / "skills"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conn() -> sqlite3.Connection:
    from core import database
    conn = sqlite3.connect(str(database.DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS skills_state ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " path TEXT UNIQUE NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'draft',"
        " source TEXT NOT NULL DEFAULT 'user',"
        " uses INTEGER NOT NULL DEFAULT 0,"
        " successes INTEGER NOT NULL DEFAULT 0,"
        " failures INTEGER NOT NULL DEFAULT 0,"
        " failures_in_a_row INTEGER NOT NULL DEFAULT 0,"
        " created_at TEXT NOT NULL,"
        " last_used_at TEXT,"
        " status_reason TEXT NOT NULL DEFAULT '',"
        " last_note TEXT NOT NULL DEFAULT '')"
    )
    return conn


def _parse(text: str, fallback_name: str) -> tuple[str, str, str]:
    """(name, description, body) from a SKILL.md."""
    text = text.replace("\r\n", "\n")
    name, description, body = "", "", text
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.DOTALL)
    if m:
        fm, body = m.group(1), text[m.end():]
        nm = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
        dm = re.search(r"^description:\s*(.+)$", fm, re.MULTILINE)
        name = nm.group(1).strip().strip("\"'") if nm else ""
        description = dm.group(1).strip().strip("\"'") if dm else ""
    return name or fallback_name, description, body.strip()


def _source_for(rel: Path) -> str:
    first = rel.parts[0] if rel.parts else ""
    if first == "learned":
        return "learned"
    if first.endswith("-imports"):
        return "imported"
    return "user"


def _files() -> dict[str, Path]:
    """SKILL.md files under the skills folder, keyed by path relative to it.
    Anything that resolves outside the folder (symlinks) is skipped."""
    root = _root()
    if not root.is_dir():
        return {}
    root_resolved = root.resolve()
    found = {}
    for md in root.rglob("SKILL.md"):
        try:
            if not md.resolve().is_relative_to(root_resolved):
                continue
        except (OSError, ValueError):
            continue
        found[md.relative_to(root).as_posix()] = md
    return found


def scan() -> list[dict]:
    """Register new skill files, forget deleted ones, archive stale drafts,
    and return every skill with its record, newest activity first."""
    files = _files()
    now = _now()
    stale_before = (now - timedelta(days=STALE_DRAFT_DAYS)).isoformat()
    conn = _conn()
    try:
        known = {r["path"] for r in conn.execute("SELECT path FROM skills_state")}
        for rel in files.keys() - known:
            conn.execute(
                "INSERT INTO skills_state (path, status, source, created_at) VALUES (?, 'draft', ?, ?)",
                (rel, _source_for(Path(rel)), now.isoformat()),
            )
        for rel in known - files.keys():
            conn.execute("DELETE FROM skills_state WHERE path = ?", (rel,))
        conn.execute(
            "UPDATE skills_state SET status = 'archived', status_reason = ? "
            "WHERE status = 'draft' AND COALESCE(last_used_at, created_at) < ?",
            (f"Not used in {STALE_DRAFT_DAYS} days", stale_before),
        )
        conn.commit()
        rows = [dict(r) for r in conn.execute("SELECT * FROM skills_state")]
    finally:
        conn.close()
    out = []
    for row in rows:
        md = files.get(row["path"])
        if not md:
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        name, description, _ = _parse(text, md.parent.name)
        row.update({"name": name, "description": description})
        out.append(row)
    # Recently used first, the rest alphabetical (easier to find by ear).
    out.sort(key=lambda r: r["name"].lower())
    out.sort(key=lambda r: r.get("last_used_at") or "", reverse=True)
    return out


def _find(name_or_id) -> Optional[dict]:
    key = str(name_or_id).strip().lower()
    for s in scan():
        if str(s["id"]) == key or s["name"].lower() == key or Path(s["path"]).parent.name.lower() == key:
            return s
    return None


def read(name_or_id) -> Optional[dict]:
    s = _find(name_or_id)
    if not s:
        return None
    md = _root() / s["path"]
    _, _, body = _parse(md.read_text(encoding="utf-8", errors="replace"), md.parent.name)
    return {**s, "body": body[:MAX_SKILL_CHARS]}


def use(name: str) -> dict:
    """FreeHand opens a skill to follow it. Counts as a use."""
    s = read(name)
    if not s or s["status"] == "archived":
        return {"error": f"No skill called '{name}'. Use one from the Skills list."}
    conn = _conn()
    try:
        conn.execute("UPDATE skills_state SET uses = uses + 1, last_used_at = ? WHERE id = ?",
                     (_now().isoformat(), s["id"]))
        conn.commit()
    finally:
        conn.close()
    note = ("This skill is a DRAFT: it hasn't proven itself yet, so check each step. "
            if s["status"] == "draft" else "")
    return {
        "name": s["name"],
        "status": s["status"],
        "instructions": s["body"],
        "after": note + "When you've finished, call skill_outcome with whether it worked.",
    }


def record_outcome(name: str, worked: bool, note: str = "") -> dict:
    s = _find(name)
    if not s:
        return {"error": f"No skill called '{name}'."}
    successes = s["successes"] + (1 if worked else 0)
    failures = s["failures"] + (0 if worked else 1)
    in_a_row = 0 if worked else s["failures_in_a_row"] + 1
    status, reason, message = s["status"], s["status_reason"], "Noted."
    if worked and status == "draft" and successes >= PROMOTE_AFTER:
        status, reason = "active", f"Worked {successes} times"
        message = f"'{s['name']}' has worked {successes} times, so it's now an active skill."
    elif not worked:
        limit = ARCHIVE_DRAFT_AFTER_FAILURES if status == "draft" else ARCHIVE_ACTIVE_AFTER_FAILURES
        if status != "archived" and in_a_row >= limit:
            status, reason = "archived", f"Didn't work {in_a_row} times in a row"
            message = f"'{s['name']}' didn't work {in_a_row} times in a row, so it has been archived."
    conn = _conn()
    try:
        conn.execute(
            "UPDATE skills_state SET successes = ?, failures = ?, failures_in_a_row = ?, "
            "status = ?, status_reason = ?, last_note = ? WHERE id = ?",
            (successes, failures, in_a_row, status, reason, (note or "")[:500], s["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return {"recorded": True, "status": status, "message": message}


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:60] or "skill"


def save(name: str, description: str, instructions: str) -> dict:
    """FreeHand writes a new skill. It always starts as a draft."""
    from core import long_memory
    name, description, instructions = (name or "").strip(), (description or "").strip(), (instructions or "").strip()
    if not name or not description or not instructions:
        return {"error": "A skill needs a name, a one-line description, and the steps."}
    if len(instructions) > MAX_SKILL_CHARS:
        return {"error": "That skill is too long. Keep it to the steps that matter."}
    if long_memory.looks_secret(instructions) or long_memory.looks_secret(description):
        return {"error": "That skill looks like it contains a password or key. Leave secrets out of skills."}
    existing = _find(name)
    if existing and not (existing["source"] == "learned" and existing["status"] == "draft"):
        return {"error": f"There is already a skill called '{existing['name']}'. Choose another name."}
    folder = _root() / "learned" / _slug(name)
    folder.mkdir(parents=True, exist_ok=True)
    one_line = " ".join(description.split())
    (folder / "SKILL.md").write_text(
        f"---\nname: {_slug(name)}\ndescription: {one_line}\n---\n\n{instructions}\n", encoding="utf-8")
    if existing:  # rewriting its own draft starts the count again
        set_status(existing["id"], "draft", "Rewritten by FreeHand", reset=True)
    scan()
    return {"saved": True, "name": _slug(name), "status": "draft",
            "message": "Saved as a draft skill. It becomes permanent once it has worked a few times, or when you approve it on the Skills page."}


def set_status(skill_id, status: str, reason: str = "", reset: bool = False) -> dict:
    if status not in STATUSES:
        return {"error": "Unknown status."}
    conn = _conn()
    try:
        sql = "UPDATE skills_state SET status = ?, status_reason = ?, failures_in_a_row = 0"
        if reset:
            sql += ", uses = 0, successes = 0, failures = 0"
        if status != "archived":
            # Restoring or approving counts as fresh activity, so the
            # unused-draft clean-up doesn't archive it again straight away.
            sql += f", last_used_at = '{_now().isoformat()}'"
        cur = conn.execute(sql + " WHERE id = ?", (status, reason, int(skill_id)))
        conn.commit()
        changed = cur.rowcount
    finally:
        conn.close()
    return {"updated": True, "status": status} if changed else {"error": "No such skill."}


def delete(skill_id) -> dict:
    """Remove a skill for good: its file and its record."""
    s = _find(skill_id)
    if not s:
        return {"error": "No such skill."}
    md = (_root() / s["path"]).resolve()
    root = _root().resolve()
    if not md.is_relative_to(root):
        return {"error": "That skill is outside the skills folder."}
    folder = md.parent
    if folder != root and folder.resolve().is_relative_to(root):
        shutil.rmtree(folder, ignore_errors=True)
    else:
        md.unlink(missing_ok=True)
    scan()
    return {"deleted": True}


def prompt_index() -> str:
    """The skills list for FreeHand's instructions: active first, then
    drafts. Archived skills are left out."""
    skills = [s for s in scan() if s["status"] != "archived"]
    if not skills:
        return ""
    skills.sort(key=lambda s: (s["status"] != "active", s["name"]))
    lines = []
    for s in skills[:PROMPT_LIST_LIMIT]:
        tag = "" if s["status"] == "active" else " (draft, untested)"
        desc = s["description"][:140] + ("..." if len(s["description"]) > 140 else "")
        lines.append(f"- {s['name']}{tag}: {desc}")
    more = len(skills) - PROMPT_LIST_LIMIT
    if more > 0:
        lines.append(f"- ...and {more} more on the Skills page")
    return "\n".join(lines)

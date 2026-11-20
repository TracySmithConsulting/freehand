"""Brush strokes: ways to make FreeHand your own, shared (Nov 2026).

docs/design/brush-strokes.md. Step 1: team members and skills, added from
a GitHub link or a downloaded file, and "Share as a brush stroke".

The rule that keeps it safe: **words and settings, never code.** A brush
stroke's words are a stranger's, so FreeHand:

- checks it without AI before the person sees it: its shape and sizes,
  that its tools are FreeHand's own tool choices, nothing that looks like
  a secret, and warning words (quoted to the person, never trusted as the
  protection);
- shows one short card, and adds it only on the person's yes, from the
  exact copy it checked (``check`` hands back a ticket, ``add`` takes it);
- adds a team member set to "Ask me first" for everything, and a skill as
  a draft that has to earn its place (core/skills.py);
- keeps a record, so **Remove** undoes it in one step.

A brush stroke is a folder (on GitHub) with ``brush-stroke.json`` and its
words, or the same as one downloaded file:

    {"brush_stroke": {"kind": "team_member", "name": "Marketing helper",
                      "about": "...", "made_by": "...", "version": "1.0",
                      "tools": ["facebook", "web"]},
     "files": {"instructions.md": "..."}}
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

KINDS = {"team_member": "a team member", "skill": "a skill"}
WORDS_FILE = {"team_member": "instructions.md", "skill": "SKILL.md"}
MANIFEST = "brush-stroke.json"
GALLERY = "https://github.com/TracySmithConsulting/freehand-brush-strokes/"
MAX_FILE_BYTES = 64 * 1024
MAX_WORDS = {"team_member": 3000, "skill": 20000}
TICKET_MINUTES = 15
SETTING = "brush_strokes_by_members"  # the owner's switch; on unless switched off


class BrushStrokeError(ValueError):
    """Why a brush stroke can't be added. Plain words."""


# ── Fetching from GitHub ─────────────────────────────────────────────

_GITHUB = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/(?:tree|blob)/([\w.\-/]+?)/?$")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # only ever the address FreeHand worked out


def _raw_base(url: str) -> str:
    """https://github.com/o/r/tree/main/marketing -> the raw folder address.
    Only GitHub; a link to the brush-stroke.json file itself works too."""
    url = str(url or "").strip()
    m = _GITHUB.match(url)
    if not m:
        raise BrushStrokeError("Paste a link to a brush stroke on GitHub. It starts with https://github.com/ "
                               "and goes to the brush stroke's folder.")
    owner, repo, rest = m.groups()
    if rest.endswith("/" + MANIFEST):
        rest = rest[: -len(MANIFEST) - 1]
    if ".." in rest.split("/"):
        raise BrushStrokeError("That link doesn't look right.")
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}/"


def _get(url: str) -> str:
    opener = urllib.request.build_opener(_NoRedirects)
    req = urllib.request.Request(url, headers={"User-Agent": "FreeHand"})
    try:
        with opener.open(req, timeout=15) as resp:
            data = resp.read(MAX_FILE_BYTES + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise BrushStrokeError("There's no brush stroke at that link. Check it goes to the brush stroke's folder.")
        raise BrushStrokeError("GitHub didn't give the brush stroke. Try again in a minute.")
    except (urllib.error.URLError, OSError):
        raise BrushStrokeError("FreeHand couldn't reach GitHub. Check the internet connection and try again.")
    if len(data) > MAX_FILE_BYTES:
        raise BrushStrokeError("That brush stroke is too big. A brush stroke is only words and settings.")
    return data.decode("utf-8", errors="replace")


def fetch(url: str) -> dict:
    """The brush stroke at a GitHub link, as a bundle. Fetches only its
    two files, from raw.githubusercontent.com; nothing is run."""
    base = _raw_base(url)
    try:
        manifest = json.loads(_get(base + MANIFEST))
    except ValueError:
        raise BrushStrokeError("That brush stroke's brush-stroke.json isn't readable.")
    kind = manifest.get("kind") if isinstance(manifest, dict) else None
    if kind not in KINDS:
        raise BrushStrokeError("FreeHand can add team members and skills for now. This is something else.")
    return {"brush_stroke": manifest, "files": {WORDS_FILE[kind]: _get(base + WORDS_FILE[kind])}}


def from_upload(text: str) -> dict:
    """A brush stroke someone downloaded (one file)."""
    if len(str(text or "").encode("utf-8")) > 2 * MAX_FILE_BYTES:
        raise BrushStrokeError("That file is too big to be a brush stroke.")
    try:
        bundle = json.loads(text)
    except (TypeError, ValueError):
        raise BrushStrokeError("That file isn't a brush stroke. A brush stroke file ends in .brushstroke.json.")
    if not isinstance(bundle, dict) or not isinstance(bundle.get("brush_stroke"), dict):
        raise BrushStrokeError("That file isn't a brush stroke. A brush stroke file ends in .brushstroke.json.")
    return bundle


# ── Checking ─────────────────────────────────────────────────────────

# Warning words: quoted to the person, never trusted as the protection.
_WARNINGS = [
    (r"\b(ignore|forget|disregard|override)\b[^.\n]{0,40}\b(rules?|instructions?|guidelines?|safety)\b",
     "tries to change FreeHand's rules"),
    (r"\b(don'?t|do not|never|without)\b[^.\n]{0,20}\b(ask|asking|check|checking|confirm|approval|telling)\b",
     "says not to ask first"),
    (r"\bjust do it\b", "says not to ask first"),
    (r"\b(passwords?|api[ _-]?keys?|tokens?|secrets?|bank|card numbers?|pin)\b", "mentions passwords, keys or banking"),
    (r"\b(send|forward|email|post|upload|share)\b[^.\n]{0,40}\b(to|with)\b[^.\n]{0,40}(@|https?://)",
     "sends something to an address in it"),
    (r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "has an email address in it"),
    (r"https?://[^\s]*[^\s.,;:!?)]", "has a web link in it"),
    (r"\b(turn(ing)? off|switch(ing)? off|disabl\w*|skip\w*|bypass\w*|get(ting)? round)\b[^.\n]{0,30}"
     r"\b(approv\w*|checks?|checking|safety|warnings?|asking|limits?)\b", "talks about getting round FreeHand's checks"),
    (r"\bapprov\w*\b[^.\n]{0,20}\b(yourself|automatically|on (their|my|the person's) behalf)\b",
     "talks about approving things itself"),
]
_HIDDEN = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")


_ENDS = re.compile(r"[.!?](?=\s|$)|\n")  # a full stop inside an address isn't an end


def _sentence(text: str, start: int, end: int) -> str:
    """The whole sentence a warning word is in, to quote it."""
    lefts = [m.end() for m in _ENDS.finditer(text, 0, start)]
    left = lefts[-1] if lefts else 0
    m = _ENDS.search(text, end)
    right = m.end() if m else len(text)
    words = " ".join(text[left:right].split())
    return words if len(words) <= 200 else words[:197] + "..."


def warnings(text: str) -> list:
    """[{"why": ..., "words": ...}] for the CAREFUL warning, without AI."""
    found, seen = [], set()
    if _HIDDEN.search(text or ""):
        found.append({"why": "has hidden characters in it", "words": ""})
    for pattern, why in _WARNINGS:
        for m in re.finditer(pattern, text or "", flags=re.IGNORECASE):
            words = _sentence(text, m.start(), m.end())
            if (why, words) not in seen:
                seen.add((why, words))
                found.append({"why": why, "words": words})
            if len(found) >= 10:
                return found
    return found


def _text(value, limit: int) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split())[:limit]


def _body(skill_md: str) -> str:
    """A SKILL.md's steps, without its front matter (FreeHand writes its own)."""
    text = str(skill_md or "").replace("\r\n", "\n").strip()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def is_gallery(source: str) -> bool:
    return str(source or "").startswith(GALLERY)


def check(bundle: dict, source: str = "") -> dict:
    """What it is, in plain words, and its warnings; or BrushStrokeError."""
    from core import long_memory, team
    manifest = bundle.get("brush_stroke") if isinstance(bundle, dict) else None
    files = bundle.get("files") if isinstance(bundle, dict) else None
    if not isinstance(manifest, dict) or not isinstance(files, dict):
        raise BrushStrokeError("That isn't a brush stroke.")
    kind = manifest.get("kind")
    if kind not in KINDS:
        raise BrushStrokeError("FreeHand can add team members and skills for now. This is something else.")
    extra = set(files) - {WORDS_FILE[kind]}
    if extra:
        raise BrushStrokeError("A brush stroke is only words and settings, and this one has other files: "
                               + ", ".join(sorted(str(x) for x in extra)[:5]) + ".")
    name = _text(manifest.get("name"), 60)
    about = _text(manifest.get("about"), 300)
    if not name or not about:
        raise BrushStrokeError("That brush stroke doesn't say its name and what it does.")
    if kind == "team_member" and not re.match(r"^[\w][\w '\-]*$", name[:40]):
        raise BrushStrokeError("A team member's name can only have letters, numbers, spaces and dashes.")
    raw = files.get(WORDS_FILE[kind])
    if not isinstance(raw, str):
        raise BrushStrokeError(f"That brush stroke is missing its {WORDS_FILE[kind]}.")
    words = _body(raw) if kind == "skill" else raw.replace("\r\n", "\n").strip()
    if not words:
        raise BrushStrokeError("That brush stroke has no instructions.")
    if len(words) > MAX_WORDS[kind]:
        raise BrushStrokeError(f"Its instructions are too long ({len(words)} characters; the most is "
                               f"{MAX_WORDS[kind]}). A long one can hide words nobody reads.")
    tools = manifest.get("tools") or []
    if kind == "team_member":
        if not isinstance(tools, list):
            raise BrushStrokeError("That brush stroke's tools aren't readable.")
        unknown = [str(t) for t in tools if t not in team.TOOL_CHOICES]
        if unknown:
            raise BrushStrokeError("It asks for things FreeHand doesn't offer team members: "
                                   + ", ".join(unknown[:5]) + ".")
    elif tools:
        raise BrushStrokeError("A skill can't ask for tools: it's only words.")
    if long_memory.looks_secret(words) or long_memory.looks_secret(about):
        raise BrushStrokeError("It looks like it contains a password or key, so it isn't added.")
    uses = [team.TOOL_CHOICES[t]["label"] for t in tools] if kind == "team_member" else []
    return {
        "kind": kind,
        "kind_words": KINDS[kind],
        "name": name,
        "about": about,
        "made_by": _text(manifest.get("made_by"), 80),
        "version": _text(manifest.get("version"), 20),
        "tools": list(dict.fromkeys(tools)) if kind == "team_member" else [],
        "uses": uses,
        "instructions": words,
        "source": str(source or "")[:500],
        "gallery": is_gallery(source),
        "warnings": warnings(about + "\n" + words),
    }


def card(checked: dict) -> str:
    """The card the person reads before saying yes, as one passage."""
    who = f", made by {checked['made_by']}" if checked["made_by"] else ""
    where = ("From the FreeHand gallery." if checked["gallery"]
             else "Nobody has checked this one. Read its instructions before you add it.")
    parts = [f"{checked['name']}, {checked['kind_words']}{who}. {where}", checked["about"]]
    if checked["kind"] == "team_member":
        uses = checked["uses"]
        listed = (uses[0] if len(uses) == 1 else ", ".join(uses[:-1]) + " and " + uses[-1]) if uses else "nothing outside FreeHand"
        parts.append(f"It can use: {listed}. It asks you first before it sends, posts or changes anything.")
    else:
        parts.append("It starts as a draft skill, and stays only if it works.")
    return "\n\n".join(parts)


# ── Who may add them ─────────────────────────────────────────────────

def members_may_add(settings: Optional[dict] = None) -> bool:
    from core import people
    s = settings if settings is not None else people._settings()
    return s.get(SETTING) is not False


def refusal(kind: str, person_id: Optional[str] = None) -> Optional[str]:
    from core import children, people
    person_id = person_id or people.current()
    owner = people.list_people()[0]["name"]
    if person_id == people.OWNER_ID:
        return None
    if children.is_child(person_id):
        return "Children can't add brush strokes. Ask a parent."
    if kind == "skill":
        return f"Skills change how FreeHand works for everyone, so only {owner} adds them."
    if not members_may_add():
        return f"{owner} has switched off adding brush strokes. Ask {owner} if you'd like this one."
    return None


# ── Tickets: what was checked is what's added ────────────────────────

_tickets: dict = {}
_lock = threading.Lock()


def _ticket(checked: dict, person_id: str) -> str:
    token = secrets.token_urlsafe(16)
    now = time.time()
    with _lock:
        for k in [k for k, v in _tickets.items() if v["expires"] < now]:
            _tickets.pop(k, None)
        _tickets[token] = {"checked": checked, "person": person_id, "expires": now + TICKET_MINUTES * 60}
    return token


def look(bundle: dict, source: str = "") -> dict:
    """Check a brush stroke for the current person: the card, and a ticket
    to add exactly this copy."""
    from core import people
    checked = check(bundle, source)
    out = {**checked, "card": card(checked), "may_add": True}
    why_not = refusal(checked["kind"])
    if why_not:
        out.update(may_add=False, why_not=why_not)
    else:
        out["ticket"] = _ticket(checked, people.current())
    return out


# ── Adding, listing and removing ─────────────────────────────────────

def _conn() -> sqlite3.Connection:
    from core import database
    conn = sqlite3.connect(str(database.DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS brush_strokes ("
                 " id INTEGER PRIMARY KEY AUTOINCREMENT, person TEXT NOT NULL, kind TEXT NOT NULL,"
                 " name TEXT NOT NULL, target TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',"
                 " version TEXT NOT NULL DEFAULT '', gallery INTEGER NOT NULL DEFAULT 0, added_at TEXT NOT NULL)")
    return conn


def _skill_folder(name: str):
    from core import skills
    return skills._root() / "brush-strokes-imports" / skills._slug(name)


def add(ticket: str) -> dict:
    """Add the brush stroke the person was shown, on their yes."""
    from core import people, skills, team
    person = people.current()
    with _lock:
        held = _tickets.get(str(ticket or ""))
        if held and held["person"] == person:  # someone else's try doesn't use it up
            _tickets.pop(str(ticket), None)
        else:
            held = None
    if not held or held["expires"] < time.time():
        raise BrushStrokeError("That took too long, so nothing was added. Look at the brush stroke again.")
    c = held["checked"]
    why_not = refusal(c["kind"], person)
    if why_not:
        raise BrushStrokeError(why_not)
    if c["kind"] == "team_member":
        try:
            member = team.save({"name": c["name"][:40], "job": c["about"], "instructions": c["instructions"],
                                "tools": c["tools"], "auto_tools": [], "template": "brush-stroke"})
        except team.TeamError as e:
            raise BrushStrokeError(str(e))
        target, done = member["id"], f"{member['name']} is now one of your team members. It asks you first for everything."
    else:
        folder = _skill_folder(c["name"])
        if (folder / "SKILL.md").exists() or skills._find(c["name"]):
            raise BrushStrokeError(f"There's already a skill called {c['name']}.")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {skills._slug(c['name'])}\ndescription: {c['about']}\n---\n\n{c['instructions']}\n",
            encoding="utf-8")
        skills.scan()
        target, done = folder.relative_to(skills._root()).as_posix(), (
            f"{c['name']} is now a draft skill. It stays once it has worked a few times, or when you approve it.")
    conn = _conn()
    try:
        cur = conn.execute("INSERT INTO brush_strokes (person, kind, name, target, source, version, gallery, added_at)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (person, c["kind"], c["name"], target, c["source"], c["version"], int(c["gallery"]),
                            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        conn.commit()
        added_id = cur.lastrowid
    finally:
        conn.close()
    return {"added": True, "id": added_id, "message": done}


def list_added(person_id: Optional[str] = None) -> list:
    """What this person added (the owner: everyone's)."""
    from core import people
    person_id = person_id or people.current()
    conn = _conn()
    try:
        if person_id == people.OWNER_ID:
            rows = conn.execute("SELECT * FROM brush_strokes ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM brush_strokes WHERE person = ? ORDER BY id DESC", (person_id,)).fetchall()
    finally:
        conn.close()
    return [{**dict(r), "kind_words": KINDS.get(r["kind"], r["kind"]), "gallery": bool(r["gallery"]),
             "added_by": (people.get(r["person"]) or {}).get("name", r["person"])} for r in rows]


def remove(added_id) -> dict:
    """Undo it in one step: the team member or skill goes."""
    from core import people, skills, team
    person = people.current()
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM brush_strokes WHERE id = ?", (int(added_id),)).fetchone()
    except (TypeError, ValueError):
        row = None
    finally:
        conn.close()
    if row is None or (person != people.OWNER_ID and row["person"] != person):
        raise BrushStrokeError("That brush stroke isn't one you added.")
    if row["kind"] == "team_member":
        token = people.set_current(row["person"])  # team members are their adder's
        try:
            team.delete(row["target"])
        finally:
            people.reset(token)
    else:
        found = next((s for s in skills.scan() if s["path"] == row["target"] + "/SKILL.md"), None)
        if found:
            skills.delete(found["id"])
    conn = _conn()
    try:
        conn.execute("DELETE FROM brush_strokes WHERE id = ?", (row["id"],))
        conn.commit()
    finally:
        conn.close()
    return {"removed": True, "message": f"{row['name']} is gone."}


def forget(person_id: str) -> None:
    """Someone left: their records go (what they added was theirs)."""
    try:
        conn = _conn()
        try:
            conn.execute("DELETE FROM brush_strokes WHERE person = ?", (person_id,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


# ── Sharing your own ─────────────────────────────────────────────────

def export_team_member(member_id: str, made_by: str = "") -> dict:
    """One of the person's own team members as a brush stroke file. Only
    its name, job, tools and instructions: never its keys, accounts,
    memories, model, who it's shared with, or its log."""
    from core import team
    member = team.get(member_id)
    if not member or not team.mine(member):
        raise BrushStrokeError("You can share your own team members.")
    manifest = {"kind": "team_member", "name": member["name"], "about": member.get("job", ""),
                "made_by": _text(made_by, 80), "version": "1.0", "tools": list(member.get("tools") or [])}
    bundle = {"brush_stroke": manifest, "files": {"instructions.md": member.get("instructions") or member.get("job", "")}}
    check(bundle)  # what's shared must pass the same checks
    return bundle


def file_name(bundle: dict) -> str:
    name = re.sub(r"[^a-z0-9]+", "-", str(bundle["brush_stroke"].get("name", "")).lower()).strip("-") or "brush-stroke"
    return f"{name}.brushstroke.json"

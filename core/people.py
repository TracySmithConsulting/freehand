"""Who is talking to FreeHand (Nov 2026): the foundation for separate users.

FreeHand is used by one person today. The plan (docs/design/people.md) is
an office on a server where several people use one FreeHand, with shared
connections and shared information plus a private workspace each. This
module is the first, safe step: every message is linked to a *person*, and
what FreeHand stores (chats, tasks, approvals, memories) is tagged with
whose it is. Nothing is split up yet; on a one-person FreeHand everything
is simply tagged as the owner's, and nothing behaves differently.

Settings (vault/settings.json), all optional:

    "owner_name": "Tracy",
    "people": [
        {"id": "thandi", "name": "Thandi", "role": "member",
         "telegram": ["123456789"], "whatsapp": ["+27 82 123 4567"],
         "slack": ["U0123ABC"]}
    ]

The owner always exists (id "owner"). The console (its API key), the CLI,
and the existing ``{channel}_allow_from`` lists all belong to the owner, so
older settings keep working unchanged. A chat ID or number listed under a
person is allowed in, and its messages are that person's.
"""
from __future__ import annotations

import contextvars
import re
import sqlite3
from typing import Optional

OWNER_ID = "owner"
CHANNELS = ("telegram", "whatsapp", "slack")
PERSON_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# The person the current request is for. Set once per agent run; read by
# whatever stores something (a task, an approval, a memory) so the person
# doesn't have to be passed through every function. asyncio.to_thread
# copies it into worker threads.
_current: contextvars.ContextVar[str] = contextvars.ContextVar("freehand_person", default=OWNER_ID)


def _settings() -> dict:
    from core import agent_config
    try:
        return agent_config._load_settings()
    except Exception:
        return {}


def identity(channel: str, value) -> str:
    """A chat ID / user ID / phone number in the form used for matching."""
    s = str(value or "").strip()
    if channel == "whatsapp":
        user, _, domain = s.partition("@")
        if domain in ("", "s.whatsapp.net", "c.us"):
            return re.sub(r"\D", "", user.split(":")[0])
        return s.lower()
    return s


def list_people(settings: Optional[dict] = None) -> list[dict]:
    """Everyone this FreeHand knows, the owner first."""
    s = settings if settings is not None else _settings()
    owner = {"id": OWNER_ID, "name": (s.get("owner_name") or "").strip() or "Owner", "role": "owner"}
    out = [owner]
    for p in s.get("people") or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip().lower()
        if not PERSON_ID_RE.match(pid) or pid == OWNER_ID or any(q["id"] == pid for q in out):
            continue
        out.append({
            "id": pid,
            "name": str(p.get("name") or pid).strip(),
            "role": "member" if p.get("role") != "owner" else "owner",
            **{ch: [str(x) for x in (p.get(ch) or [])] for ch in CHANNELS},
        })
    return out


def get(person_id: str, settings: Optional[dict] = None) -> Optional[dict]:
    return next((p for p in list_people(settings) if p["id"] == person_id), None)


def _legacy_allow(channel: str, s: dict) -> list[str]:
    allow = s.get(f"{channel}_allow_from") or []
    if not allow and channel == "telegram":
        allow = s.get("telegram_chat_whitelist") or []
    return [identity(channel, x) for x in allow]


def person_for(source: str, caller_id: str = "", settings: Optional[dict] = None) -> Optional[str]:
    """Whose message is this? None = nobody FreeHand knows (not allowed in).

    The console, CLI and tests are the owner's. For a chat channel, a person
    whose links include the caller wins; then the old allow_from lists,
    which are the owner's.
    """
    channel = (source or "").strip().lower()
    if channel not in CHANNELS:
        return OWNER_ID
    s = settings if settings is not None else _settings()
    who = identity(channel, caller_id)
    if not who:
        return None
    for p in list_people(s)[1:]:
        if who in (identity(channel, x) for x in p.get(channel, [])):
            return p["id"]
    if who in _legacy_allow(channel, s):
        return OWNER_ID
    return None


def allowed_ids(channel: str, settings: Optional[dict] = None) -> list[str]:
    """Every caller ID allowed on a channel (owner's list plus each person's)."""
    s = settings if settings is not None else _settings()
    ids = list(_legacy_allow(channel, s))
    for p in list_people(s)[1:]:
        ids += [identity(channel, x) for x in p.get(channel, [])]
    return ids


# ── The current person ───────────────────────────────────────────────

def current() -> str:
    return _current.get()


def set_current(person_id: Optional[str]):
    """Set the person for this request. Returns a token for reset()."""
    return _current.set(person_id or OWNER_ID)


def reset(token) -> None:
    try:
        _current.reset(token)
    except (ValueError, LookupError):
        pass


def memory_tags(person_id: Optional[str] = None) -> list[str]:
    """Hindsight tags for something this person said or asked to remember."""
    return [f"person:{person_id or current()}"]


# ── Tagging stored rows ──────────────────────────────────────────────

_TAGGED_TABLES = ("tasks", "approvals", "conversation_turns")
_checked: set[tuple[str, str]] = set()


def ensure_person_column(conn: sqlite3.Connection, table: str) -> None:
    """Add a ``person`` column (default 'owner') to a table once.

    Existing rows become the owner's, which is right: until now FreeHand
    only ever had one person.
    """
    if table not in _TAGGED_TABLES:
        raise ValueError(f"not a person-tagged table: {table}")
    try:
        db = conn.execute("PRAGMA database_list").fetchone()[2] or ":memory:"
    except sqlite3.Error:
        db = ":memory:"
    key = (db, table)
    if key in _checked and db != ":memory:":
        return
    try:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if cols and "person" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN person TEXT NOT NULL DEFAULT '{OWNER_ID}'")
            conn.commit()
        if cols:
            _checked.add(key)
    except sqlite3.Error:
        pass

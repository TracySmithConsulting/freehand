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
# The roles a person in settings may have. There is one owner, and the
# settings file can't make another. An unknown role (a typo, or a role
# from a newer FreeHand) doesn't get in at all, rather than getting a
# member's rights: when the child role comes, "chlid" must not be a member.
ROLES = ("member",)
CHANNELS = ("telegram", "whatsapp", "slack")
PERSON_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# The person the current request is for. Set once per agent run; read by
# whatever stores something (a task, an approval, a memory) so the person
# doesn't have to be passed through every function. asyncio.to_thread
# copies it into worker threads; threading.Thread and thread pools don't,
# so work sent there goes through carry() (or it would run as the owner).
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
        role = str(p.get("role") or "member").strip().lower()
        if role not in ROLES:
            _warn_role(pid, role)
            continue
        out.append({
            "id": pid,
            "name": str(p.get("name") or pid).strip(),
            "role": role,
            **{ch: [str(x) for x in (p.get(ch) or [])] for ch in CHANNELS},
        })
    return out


_warned_roles: set = set()


def _warn_role(pid: str, role: str) -> None:
    if (pid, role) not in _warned_roles:
        _warned_roles.add((pid, role))
        import logging
        logging.getLogger(__name__).warning(
            "Person %r in settings has the role %r, which FreeHand doesn't know, so they "
            "aren't let in. The roles are: %s.", pid, role, ", ".join(ROLES))


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


def carry(fn):
    """fn, made to run in another thread as the current person.

    A new thread (threading.Thread, a thread pool, run_in_executor) starts
    without the current person, so without this, work a person asked for
    would run there as the owner. asyncio.to_thread already carries it."""
    ctx = contextvars.copy_context()
    return lambda *args, **kwargs: ctx.run(fn, *args, **kwargs)


def reset(token) -> None:
    try:
        _current.reset(token)
    except (ValueError, LookupError):
        pass


def approvals_scope(person_id: Optional[str] = None) -> Optional[str]:
    """Whose approvals this person sees and answers in the console: their
    own (the person id), or everyone's (None) for the owner.

    The owner sees everyone's for now because the console is theirs: a
    member can't sign in to it yet, and a risky request can only be
    approved there, not from chat. Once each person signs in, this narrows
    to "administrators see shared connections" (docs/design/people.md)."""
    person_id = person_id or current()
    return None if person_id == OWNER_ID else person_id


# Each person's own files (scribble pad, Threads, Journal) live in the
# vault under people/<id>/. The owner's stay where they always were, in
# the vault itself, so a one-person FreeHand doesn't change at all.
WORKSPACES_DIR = "people"


def workspace_parts(person_id: Optional[str] = None) -> tuple:
    """The person's own folder, as parts under the vault: () for the owner."""
    person_id = person_id or current()
    if person_id == OWNER_ID:
        return ()
    if not PERSON_ID_RE.match(person_id):
        raise ValueError(f"not a person id: {person_id!r}")
    return (WORKSPACES_DIR, person_id)


def may_read_vault_file(rel_path: str, person_id: Optional[str] = None) -> bool:
    """Whether this person may see a vault file (a path relative to the
    vault, such as "Threads/plumber.md"): a member only their own folder,
    the owner everything except the other people's folders."""
    person_id = person_id or current()
    parts = [p for p in str(rel_path).replace("\\", "/").split("/") if p]
    in_someones = bool(parts) and parts[0] == WORKSPACES_DIR
    if person_id == OWNER_ID:
        return not in_someones
    return in_someones and len(parts) > 2 and parts[1] == person_id


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


# ── Adding, changing and removing people (the People page) ───────────

class PeopleError(ValueError):
    """Something the owner can fix, in words they can read."""


def link_owner(channel: str, value, settings: Optional[dict] = None) -> Optional[str]:
    """Who this chat link already belongs to, if anyone."""
    s = settings if settings is not None else _settings()
    who = identity(channel, value)
    if not who:
        return None
    if who in _legacy_allow(channel, s):
        return OWNER_ID
    for p in list_people(s)[1:]:
        if who in (identity(channel, x) for x in p.get(channel, [])):
            return p["id"]
    return None


def _clean_links(channel: str, values, person_id: Optional[str], s: dict) -> list[str]:
    out = []
    for raw in values or []:
        value = str(raw or "").strip()
        if not value:
            continue
        who = identity(channel, value)
        if channel == "telegram" and not who.isdigit():
            raise PeopleError("A Telegram chat ID is a number, like 123456789. "
                              "A group's starts with a minus, and a group can't be someone's own chat.")
        if channel == "whatsapp" and not (len(who) >= 7 and who.isdigit()) and not who.endswith("@lid"):
            raise PeopleError("A WhatsApp number needs the country code, like +27 82 123 4567.")
        holder = link_owner(channel, value, s)
        if holder and holder != person_id:
            name = (get(holder, s) or {}).get("name", holder)
            raise PeopleError(f"That {channel.capitalize()} is already {name}'s. A chat can be only one person's.")
        if who not in (identity(channel, v) for v in out):
            out.append(value)
    return out


def _new_id(name: str, s: dict) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "person"
    if not re.match(r"^[a-z0-9]", base):
        base = "person"
    taken = {p["id"] for p in list_people(s)} | {str(p.get("id", "")) for p in s.get("people") or [] if isinstance(p, dict)}
    pid, n = base, 2
    while pid in taken or pid == OWNER_ID:
        pid, n = f"{base}-{n}", n + 1
    return pid


def _clean_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise PeopleError("Give the person a name.")
    return name[:60]


def add_person(name, telegram=None, whatsapp=None) -> dict:
    """Add a member. Returns them as list_people shows them."""
    from core import agent_config
    s = agent_config._load_settings()
    name = _clean_name(name)
    pid = _new_id(name, s)
    entry = {"id": pid, "name": name, "role": "member",
             "telegram": _clean_links("telegram", telegram, pid, s),
             "whatsapp": _clean_links("whatsapp", whatsapp, pid, s)}
    s["people"] = list(s.get("people") or []) + [entry]
    agent_config._save_settings(s)
    return get(pid, s)


def update_person(person_id: str, name=None, telegram=None, whatsapp=None) -> dict:
    """Change a member's name or chat links. Other fields are kept."""
    from core import agent_config
    s = agent_config._load_settings()
    entries = list(s.get("people") or [])
    for i, p in enumerate(entries):
        if isinstance(p, dict) and str(p.get("id", "")).strip().lower() == person_id and person_id != OWNER_ID:
            p = dict(p)
            if name is not None:
                p["name"] = _clean_name(name)
            if telegram is not None:
                p["telegram"] = _clean_links("telegram", telegram, person_id, s)
            if whatsapp is not None:
                p["whatsapp"] = _clean_links("whatsapp", whatsapp, person_id, s)
            entries[i] = p
            s["people"] = entries
            agent_config._save_settings(s)
            return get(person_id, s)
    raise PeopleError("There's nobody with that id on this FreeHand.")


def remove_person(person_id: str) -> None:
    """Take someone off this FreeHand. Their chat links and sign-in stop
    working at once, and they're signed out everywhere. What they made
    (chats, jobs, memories, notes) stays for now; their jobs don't run
    (docs/design/people.md, "When someone leaves")."""
    from core import agent_config, sessions
    if person_id == OWNER_ID:
        raise PeopleError("The owner can't be removed.")
    s = agent_config._load_settings()
    entries = list(s.get("people") or [])
    kept = [p for p in entries if not (isinstance(p, dict) and str(p.get("id", "")).strip().lower() == person_id)]
    if len(kept) == len(entries):
        raise PeopleError("There's nobody with that id on this FreeHand.")
    s["people"] = kept
    agent_config._save_settings(s)
    sessions.end_all(person_id)

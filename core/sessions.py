"""Signing in to the console as a person (docs/design/people.md, step 1).

The owner's console on their own computer signs itself in with FreeHand's
API key, as before, and that key is always the owner. Anyone else (and the
owner on a hosted FreeHand) signs in as themselves:

- **A code to their chat.** They pick their name, FreeHand sends a 6-digit
  code to their linked Telegram or WhatsApp, and they type it in. Nothing
  to remember (the granny test), and it reuses the chat links in settings.
- **A one-time link** the owner makes, for someone with no chat linked.

Either way the browser gets a session: a long random token, kept only as
a hash here, that says who's signed in on that device. A session lasts
30 days from its last use. It stops at once when the person is no longer
on this FreeHand, so removing someone from settings signs them out.

Codes and links are kept only as hashes too, and each works once. A code
lasts 10 minutes and allows 5 tries; a new code can be asked for once a
minute. A link lasts 24 hours.
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from core import people

SESSION_DAYS = 30
CODE_MINUTES = 10
CODE_TRIES = 5
CODE_EVERY_SECONDS = 60
LINK_HOURS = 24

_lock = threading.Lock()
# Codes waiting to be typed in: person -> {"hash", "expires", "tries", "sent"}.
# In memory on purpose: a restart just means asking for a new code.
_codes: dict[str, dict] = {}


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _conn() -> sqlite3.Connection:
    from core import database
    conn = sqlite3.connect(str(database.DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS person_sessions ("
        " token_hash TEXT PRIMARY KEY,"
        " person TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " expires_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sign_in_links ("
        " token_hash TEXT PRIMARY KEY,"
        " person TEXT NOT NULL,"
        " expires_at TEXT NOT NULL)"
    )
    return conn


# ── Sessions ─────────────────────────────────────────────────────────

def start(person_id: str) -> str:
    """A new session for this person. Returns the token for the browser."""
    token = secrets.token_urlsafe(32)
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO person_sessions (token_hash, person, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (_hash(token), person_id, _iso(_now()), _iso(_now() + timedelta(days=SESSION_DAYS))),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def person_for(token: str) -> Optional[str]:
    """Who this session is signed in as, or None (unknown, ended, expired,
    or the person is no longer on this FreeHand). Each use extends it."""
    token = (token or "").strip()
    if not token or len(token) > 200:
        return None
    conn = _conn()
    try:
        row = conn.execute("SELECT person, expires_at FROM person_sessions WHERE token_hash = ?",
                           (_hash(token),)).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= _iso(_now()) or people.get(row["person"]) is None:
            conn.execute("DELETE FROM person_sessions WHERE token_hash = ?", (_hash(token),))
            conn.commit()
            return None
        conn.execute("UPDATE person_sessions SET expires_at = ? WHERE token_hash = ?",
                     (_iso(_now() + timedelta(days=SESSION_DAYS)), _hash(token)))
        conn.commit()
        return row["person"]
    finally:
        conn.close()


def end(token: str) -> None:
    """Sign out this one device."""
    conn = _conn()
    try:
        conn.execute("DELETE FROM person_sessions WHERE token_hash = ?", (_hash(token or ""),))
        conn.commit()
    finally:
        conn.close()


def end_all(person_id: str) -> int:
    """Sign this person out on every device. Returns how many."""
    with _lock:
        _codes.pop(person_id, None)
    conn = _conn()
    try:
        cur = conn.execute("DELETE FROM person_sessions WHERE person = ?", (person_id,))
        conn.execute("DELETE FROM sign_in_links WHERE person = ?", (person_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── Where a code can be sent ─────────────────────────────────────────

def code_destination(person_id: str) -> Optional[tuple[str, str]]:
    """(channel, address) to send this person's code to, or None.

    Their own Telegram chat first, then WhatsApp. The owner's are the old
    allow-from lists. Never a group: a code sent there is everyone's."""
    from core import agent_config
    s = agent_config._load_settings()
    person = people.get(person_id, s)
    if person is None:
        return None
    if person_id == people.OWNER_ID:
        telegram = list(s.get("telegram_allow_from") or s.get("telegram_chat_whitelist") or [])
        whatsapp = list(s.get("whatsapp_allow_from") or [])
    else:
        telegram, whatsapp = person.get("telegram", []), person.get("whatsapp", [])
    for chat in telegram:
        chat = str(chat).strip()
        if chat.isdigit():  # a private chat; group ids start with "-"
            return "telegram", chat
    for number in whatsapp:
        digits = people.identity("whatsapp", number)
        if digits.isdigit():
            return "whatsapp", f"{digits}@s.whatsapp.net"
    return None


CHANNEL_NAMES = {"telegram": "Telegram", "whatsapp": "WhatsApp"}


def who_can_sign_in() -> list[dict]:
    """Everyone, for the sign-in screen: name, and where a code would go."""
    out = []
    for p in people.list_people():
        where = code_destination(p["id"])
        out.append({"id": p["id"], "name": p["name"],
                    "code_by": CHANNEL_NAMES[where[0]] if where else None})
    return out


# ── Codes ────────────────────────────────────────────────────────────

async def send_code(person_id: str) -> dict:
    """Make a code and send it to the person's chat."""
    where = code_destination(person_id)
    if where is None:
        return {"error": "There's no Telegram or WhatsApp linked for you. Ask the owner for a sign-in link."}
    with _lock:
        waiting = _codes.get(person_id)
        if waiting and time.time() - waiting["sent"] < CODE_EVERY_SECONDS:
            return {"error": "A code was just sent. Wait a minute before asking for another."}
        code = f"{secrets.randbelow(1_000_000):06d}"
        _codes[person_id] = {"hash": _hash(code), "expires": time.time() + CODE_MINUTES * 60,
                             "tries": 0, "sent": time.time()}
    from core import notices
    text = (f"Your FreeHand sign-in code is {code}. It works for {CODE_MINUTES} minutes. "
            "If you didn't ask for it, ignore this message.")
    try:
        sent = await notices._send(where[0], where[1], text)
    except Exception:
        sent = False
    if not sent:
        with _lock:
            _codes.pop(person_id, None)
        return {"error": f"FreeHand couldn't send the code to your {CHANNEL_NAMES[where[0]]}. Try again later."}
    return {"sent_to": CHANNEL_NAMES[where[0]]}


def check_code(person_id: str, code: str) -> dict:
    """Swap a right code for a session. Each code works once."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    with _lock:
        waiting = _codes.get(person_id)
        if not waiting or time.time() > waiting["expires"]:
            _codes.pop(person_id, None)
            return {"error": "That code has run out. Ask for a new one."}
        waiting["tries"] += 1
        if not secrets.compare_digest(_hash(code), waiting["hash"]):
            if waiting["tries"] >= CODE_TRIES:
                _codes.pop(person_id, None)
                return {"error": "That's not the code, and that was the last try. Ask for a new one."}
            return {"error": "That's not the code. Check it and try again."}
        _codes.pop(person_id, None)
    if people.get(person_id) is None:
        return {"error": "You're not on this FreeHand any more."}
    return {"session": start(person_id), "person": person_id}


# ── One-time links the owner makes ───────────────────────────────────

def make_link(person_id: str) -> dict:
    """A one-time sign-in link for this person (the owner gives it to them)."""
    if people.get(person_id) is None:
        return {"error": "There's nobody with that id on this FreeHand."}
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(hours=LINK_HOURS)
    conn = _conn()
    try:
        conn.execute("INSERT INTO sign_in_links (token_hash, person, expires_at) VALUES (?, ?, ?)",
                     (_hash(token), person_id, _iso(expires)))
        conn.commit()
    finally:
        conn.close()
    return {"link": f"/#sign-in={token}", "expires": _iso(expires), "hours": LINK_HOURS}


def use_link(token: str) -> dict:
    """Swap a one-time link for a session. The link then stops working."""
    conn = _conn()
    try:
        row = conn.execute("SELECT person, expires_at FROM sign_in_links WHERE token_hash = ?",
                           (_hash(token or ""),)).fetchone()
        conn.execute("DELETE FROM sign_in_links WHERE token_hash = ?", (_hash(token or ""),))
        conn.commit()
    finally:
        conn.close()
    if row is None or row["expires_at"] <= _iso(_now()):
        return {"error": "That sign-in link has been used or has run out. Ask the owner for a new one."}
    if people.get(row["person"]) is None:
        return {"error": "You're not on this FreeHand any more."}
    return {"session": start(row["person"]), "person": row["person"]}

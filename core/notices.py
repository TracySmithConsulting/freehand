"""Messages from FreeHand that the person didn't just ask for: a reminder,
or the result of a scheduled job.

Every notice is kept until the web page has shown it, so it's never lost
when the page is closed. When the job was asked for on Telegram, Slack or
WhatsApp, it's sent there too (docs/design/scheduled-jobs-and-team-members.md,
"Where the result goes").
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import core.database as database

log = logging.getLogger(__name__)

CHANNELS = ("telegram", "slack", "whatsapp")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person TEXT NOT NULL DEFAULT 'owner',
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0
)
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(database.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def _person(person: Optional[str]) -> str:
    if person:
        return person
    from core import people
    return people.current()


def add(text: str, title: str = "", person: Optional[str] = None) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO notices (person, title, text, created_at) VALUES (?, ?, ?, ?)",
            (_person(person), title or "", text,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def unseen(person: Optional[str] = None, limit: int = 20) -> list[dict]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, title, text, created_at FROM notices "
            "WHERE person = ? AND seen = 0 ORDER BY id LIMIT ?",
            (_person(person), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_seen(ids: list, person: Optional[str] = None) -> int:
    ids = [int(i) for i in ids if str(i).isdigit()]
    if not ids:
        return 0
    conn = _conn()
    try:
        marks = ",".join("?" * len(ids))
        cur = conn.execute(
            f"UPDATE notices SET seen = 1 WHERE person = ? AND id IN ({marks})",
            (_person(person), *ids),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def parse_target(value) -> dict:
    """A task's deliver_to column: JSON like {"channel": "telegram", "to": "123"}."""
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


async def _send(channel: str, to: str, text: str) -> bool:
    from core import gateway
    sender = {
        "telegram": gateway._telegram_send_message,
        "slack": gateway._slack_send_message,
        "whatsapp": gateway._whatsapp_send_message,
    }[channel]
    return await sender(to, text)


async def deliver(text: str, title: str = "", target=None, person: Optional[str] = None) -> dict:
    """Keep the notice for the web page, and send it to the chat app the job
    came from, if any. Never raises: a failed send is logged and reported."""
    result = {"web": None, "channel": None, "sent": None}
    try:
        result["web"] = add(text, title, person)
    except sqlite3.Error:
        log.exception("Could not keep a notice for the web page")
    target = parse_target(target)
    channel, to = target.get("channel"), str(target.get("to") or "").strip()
    if channel in CHANNELS and to:
        result["channel"] = channel
        try:
            result["sent"] = bool(await _send(channel, to, text))
        except Exception:
            log.exception("Could not send a notice to %s", channel)
            result["sent"] = False
    return result

"""Short-term conversation memory: the last few exchanges in each chat.

Each message used to start from nothing, so follow-ups such as "and
tomorrow?" had no context. FreeHand now keeps the last ``MAX_TURNS``
exchanges per conversation and sends them with the next message.

A conversation is one chat surface and person: the web console, one
WhatsApp number, one Telegram chat, and so on (``source`` + ``caller_id``),
so two people messaging FreeHand never see each other's exchanges.
History older than ``IDLE_HOURS`` since the last message is dropped: a
new day starts a fresh conversation, and anything worth keeping is in
long-term memory (core/long_memory.py).

Stored in agent.db (table ``conversation_turns``), created on first use.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

MAX_TURNS = 6
IDLE_HOURS = 3
MAX_CHARS = 4000  # per stored message, so one long reply can't crowd out the rest


def _conn() -> sqlite3.Connection:
    from core import database
    conn = sqlite3.connect(str(database.DB_PATH), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_turns ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " conversation TEXT NOT NULL,"
        " user_text TEXT NOT NULL,"
        " reply_text TEXT NOT NULL,"
        " created_at TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_turns ON conversation_turns(conversation, id)")
    from core import people
    people.ensure_person_column(conn, "conversation_turns")
    return conn


def conversation_key(source: str = "", caller_id: str = "") -> str:
    return f"{source or 'web'}:{caller_id or ''}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def history(key: str) -> list[dict]:
    """Recent exchanges as chat messages, oldest first. [] after a long gap."""
    try:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT user_text, reply_text, created_at FROM conversation_turns "
                "WHERE conversation = ? ORDER BY id DESC LIMIT ?",
                (key, MAX_TURNS),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    if not rows:
        return []
    try:
        last = datetime.fromisoformat(rows[0][2])
    except ValueError:
        return []
    if _now() - last > timedelta(hours=IDLE_HOURS):
        return []
    messages: list[dict] = []
    for user_text, reply_text, _ in reversed(rows):
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": reply_text})
    return messages


def add_turn(key: str, user_text: str, reply_text: str, person: str = "owner") -> None:
    if not (user_text or "").strip() or not (reply_text or "").strip():
        return
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO conversation_turns (conversation, user_text, reply_text, created_at, person) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, user_text[:MAX_CHARS], reply_text[:MAX_CHARS], _now().isoformat(), person or "owner"),
            )
            # Keep the table small: only the newest turns per conversation matter.
            conn.execute(
                "DELETE FROM conversation_turns WHERE conversation = ? AND id NOT IN ("
                " SELECT id FROM conversation_turns WHERE conversation = ? ORDER BY id DESC LIMIT ?)",
                (key, key, MAX_TURNS),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def clear(key: str) -> None:
    """Start a fresh conversation (for a future "New conversation" button)."""
    try:
        conn = _conn()
        try:
            conn.execute("DELETE FROM conversation_turns WHERE conversation = ?", (key,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass

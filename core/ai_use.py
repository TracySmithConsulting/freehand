"""How much AI each person uses in a month, and the limit the owner set
for them (docs/design/people.md, "Roles and permissions").

One use is one message FreeHand answered with AI, in a chat, a scheduled
job, or through a team member (however many steps it took). Reminders and
lists that need no AI don't count.

The owner has no limit. A member has none until the owner sets one on the
People page ("ai_per_month" on the person in settings). Over it, FreeHand
answers without calling the AI at all, and says when it starts again.
Counts are kept per month in agent.db (table ``person_ai_use``).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

SETTING = "ai_per_month"
MAX_LIMIT = 100_000


def _conn() -> sqlite3.Connection:
    from core import database
    conn = sqlite3.connect(str(database.DB_PATH), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS person_ai_use ("
        " person TEXT NOT NULL, month TEXT NOT NULL,"
        " runs INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (person, month))"
    )
    return conn


def _now(now: Optional[datetime]) -> datetime:
    return now or datetime.now(timezone.utc)


def month(now: Optional[datetime] = None) -> str:
    from core import scheduler
    return scheduler._month(_now(now))


def next_month_name(now: Optional[datetime] = None) -> str:
    from core import scheduler
    local = _now(now).astimezone(scheduler.local_tz())
    first = (local.replace(day=28) + timedelta(days=4)).replace(day=1)
    return f"1 {first:%B}"


def _person(person_id: Optional[str]) -> str:
    from core import people
    return person_id or people.current()


def used(person_id: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """{"runs": n, "tokens": n} this month."""
    try:
        conn = _conn()
        try:
            row = conn.execute("SELECT runs, tokens FROM person_ai_use WHERE person = ? AND month = ?",
                               (_person(person_id), month(now))).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        row = None
    return {"runs": row[0], "tokens": row[1]} if row else {"runs": 0, "tokens": 0}


def limit(person_id: Optional[str] = None, settings: Optional[dict] = None) -> Optional[int]:
    """Their monthly limit, or None for no limit (always for the owner)."""
    from core import people
    person_id = _person(person_id)
    if person_id == people.OWNER_ID:
        return None
    s = settings if settings is not None else people._settings()
    for p in s.get("people") or []:
        if isinstance(p, dict) and str(p.get("id", "")).strip().lower() == person_id:
            value = p.get(SETTING)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_LIMIT:
                return value
            return None
    return None


def refusal(person_id: Optional[str] = None, now: Optional[datetime] = None) -> Optional[str]:
    """Why this person can't use AI now, or None."""
    from core import people
    cap = limit(person_id)
    if cap is None:
        return None
    runs = used(person_id, now)["runs"]
    if runs < cap:
        return None
    owner = people.list_people()[0]["name"]
    return (f"You've used FreeHand's AI {runs} times this month, which is the limit {owner} set, "
            f"so it starts again on {next_month_name(now)}. Ask {owner} if you need more.")


def count(usage: Optional[dict], person_id: Optional[str] = None, now: Optional[datetime] = None) -> None:
    """Count one answer that used AI (usage["calls"] > 0)."""
    if not usage or not usage.get("calls"):
        return
    tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO person_ai_use (person, month, runs, tokens) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(person, month) DO UPDATE SET runs = runs + 1, tokens = tokens + excluded.tokens",
                (_person(person_id), month(now), tokens))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # counting must never break a reply


def parse_limit(value) -> Optional[int]:
    """What the form sent: a whole number, or empty for no limit."""
    from core import people
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        cap = int(value)
    except (TypeError, ValueError):
        raise people.PeopleError("Use a whole number for how often they may use AI, or leave it empty.")
    if isinstance(value, float) or not 0 <= cap <= MAX_LIMIT:
        raise people.PeopleError(f"Choose a whole number from 0 to {MAX_LIMIT:,}, or leave it empty for no limit.")
    return cap


def set_limit(person_id: str, value) -> Optional[int]:
    """The owner sets a member's monthly limit; None or "" for no limit."""
    from core import agent_config, people
    cap = parse_limit(value)
    s = agent_config._load_settings()
    entries = list(s.get("people") or [])
    for i, p in enumerate(entries):
        if isinstance(p, dict) and str(p.get("id", "")).strip().lower() == person_id and person_id != people.OWNER_ID:
            p = dict(p)
            if cap is None:
                p.pop(SETTING, None)
            else:
                p[SETTING] = cap
            entries[i] = p
            s["people"] = entries
            agent_config._save_settings(s)
            return cap
    raise people.PeopleError("There's nobody with that id on this FreeHand.")


def forget(person_id: str) -> None:
    """Someone left (core/leaving.py): their counts go too."""
    try:
        conn = _conn()
        try:
            conn.execute("DELETE FROM person_ai_use WHERE person = ?", (person_id,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass

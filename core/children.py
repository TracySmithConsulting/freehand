"""Children on a family FreeHand (docs/design/people.md, "Families and
children"): the hard limits, enforced by FreeHand's code.

A child is a person with the role "child" and one or more parents (the
owner, or members). On top of everything a member can't do
(core/member_limits.py):

- **Anything that reaches outside waits for a parent.** Sending, posting,
  booking, browsing to a site, using an account: every step that isn't
  reading, or writing a document in the child's own folder, needs a
  parent's yes, at any permission level. The child can cancel it but
  never approve it, and an unanswered one is never done.
- **Quiet hours:** no answers between, say, 21:00 and 06:00 (their time),
  scheduled jobs included.
- **A daily message limit.**
- **Guidance for the AI,** for the child's age. It helps, but AI models
  can be talked round, which is why the limits above come first.

What parents can see is said honestly to the child (``what_parents_see``).
For now that's what they approve; reading chats, summaries and alerts are
the next step and the parents' choice.

Settings, on the child in "people":

    {"id": "lerato", "name": "Lerato", "role": "child", "parents": ["owner"],
     "quiet_hours": {"from": "21:00", "to": "06:00"}, "daily_messages": 40,
     "age_band": "under_13"}
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Optional

ROLE = "child"
AGE_BANDS = {
    "under_13": "under 13",
    "13_to_15": "13 to 15",
    "16_to_17": "16 or 17",
}
DEFAULT_DAILY_MESSAGES = 50
MAX_DAILY_MESSAGES = 1000
# Writing a document in the child's own folder isn't reaching outside.
LOCAL_WRITES = {"write_docx", "write_xlsx", "write_pptx"}
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class ChildError(ValueError):
    """A child's settings don't make sense. Plain words."""


def _entry(person_id: str, settings: Optional[dict] = None) -> Optional[dict]:
    from core import people
    s = settings if settings is not None else people._settings()
    for p in s.get("people") or []:
        if isinstance(p, dict) and str(p.get("id", "")).strip().lower() == person_id:
            return p
    return None


def is_child(person_id: Optional[str] = None, settings: Optional[dict] = None) -> bool:
    from core import people
    person = people.get(person_id or people.current(), settings)
    return bool(person) and person["role"] == ROLE


def parents_of(child_id: str, settings: Optional[dict] = None) -> list:
    """The child's parents who are on this FreeHand; the owner if none."""
    from core import people
    entry = _entry(child_id, settings) or {}
    raw = entry.get("parents") if isinstance(entry.get("parents"), list) else []
    found = []
    for pid in raw:
        p = people.get(str(pid).strip().lower(), settings)
        if p and p["role"] != ROLE and p["id"] not in found:
            found.append(p["id"])
    return found or [people.OWNER_ID]


def children_of(parent_id: str, settings: Optional[dict] = None) -> list:
    from core import people
    return [p["id"] for p in people.list_people(settings)
            if p["role"] == ROLE and parent_id in parents_of(p["id"], settings)]


def _names(ids: list) -> str:
    from core import people
    names = [(people.get(i) or {}).get("name", i) for i in ids]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def rules(child_id: str, settings: Optional[dict] = None) -> dict:
    entry = _entry(child_id, settings) or {}
    quiet = entry.get("quiet_hours") if isinstance(entry.get("quiet_hours"), dict) else {}
    daily = entry.get("daily_messages")
    return {
        "parents": parents_of(child_id, settings),
        "quiet_hours": {"from": quiet.get("from", ""), "to": quiet.get("to", "")} if quiet else None,
        "daily_messages": daily if isinstance(daily, int) and not isinstance(daily, bool) else DEFAULT_DAILY_MESSAGES,
        "age_band": entry.get("age_band") if entry.get("age_band") in AGE_BANDS else "under_13",
    }


def clean_rules(body: dict, child_id: str = "") -> dict:
    """Check what the People page sent for a child."""
    from core import people
    body = body if isinstance(body, dict) else {}
    parents = []
    for pid in body.get("parents") or []:
        pid = str(pid).strip().lower()
        p = people.get(pid)
        if p is None or p["role"] == ROLE or pid == child_id:
            raise ChildError("A parent must be you or someone on this FreeHand who isn't a child.")
        if pid not in parents:
            parents.append(pid)
    quiet = None
    q_from, q_to = str(body.get("quiet_from") or "").strip(), str(body.get("quiet_to") or "").strip()
    if q_from or q_to:
        if not (_TIME.match(q_from) and _TIME.match(q_to)) or q_from == q_to:
            raise ChildError("Give quiet hours as two different times, for example 21:00 and 06:00.")
        quiet = {"from": "%02d:%s" % (int(q_from.split(":")[0]), q_from.split(":")[1]),
                 "to": "%02d:%s" % (int(q_to.split(":")[0]), q_to.split(":")[1])}
    daily = body.get("daily_messages", DEFAULT_DAILY_MESSAGES)
    try:
        daily = int(daily)
    except (TypeError, ValueError):
        raise ChildError("Use a whole number for messages a day.")
    if not 1 <= daily <= MAX_DAILY_MESSAGES:
        raise ChildError(f"Choose from 1 to {MAX_DAILY_MESSAGES} messages a day.")
    band = str(body.get("age_band") or "under_13")
    if band not in AGE_BANDS:
        raise ChildError("Choose the child's age.")
    return {"parents": parents or [people.OWNER_ID], "quiet_hours": quiet,
            "daily_messages": daily, "age_band": band}


def save_rules(child_id: str, cleaned: dict) -> None:
    from core import agent_config
    s = agent_config._load_settings()
    entries = list(s.get("people") or [])
    for i, p in enumerate(entries):
        if isinstance(p, dict) and str(p.get("id", "")).strip().lower() == child_id:
            p = {**p, "role": ROLE, "parents": cleaned["parents"],
                 "daily_messages": cleaned["daily_messages"], "age_band": cleaned["age_band"]}
            if cleaned["quiet_hours"]:
                p["quiet_hours"] = cleaned["quiet_hours"]
            else:
                p.pop("quiet_hours", None)
            entries[i] = p
            s["people"] = entries
            agent_config._save_settings(s)
            return
    raise ChildError("There's nobody with that id on this FreeHand.")


def make_member(person_id: str) -> None:
    """No longer a child: the child's rules go."""
    from core import agent_config
    s = agent_config._load_settings()
    entries = list(s.get("people") or [])
    for i, p in enumerate(entries):
        if isinstance(p, dict) and str(p.get("id", "")).strip().lower() == person_id:
            entries[i] = {k: v for k, v in p.items()
                          if k not in ("parents", "quiet_hours", "daily_messages", "age_band")}
            entries[i]["role"] = "member"
            s["people"] = entries
            agent_config._save_settings(s)
            return


# ── Quiet hours and the daily limit ──────────────────────────────────

def _local_now(now: Optional[datetime]) -> datetime:
    from core import scheduler
    return (now or datetime.now(timezone.utc)).astimezone(scheduler.local_tz())


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet_hours(child_id: str, now: Optional[datetime] = None) -> bool:
    quiet = rules(child_id)["quiet_hours"]
    if not quiet or not quiet.get("from") or not quiet.get("to"):
        return False
    t = _local_now(now)
    minute, start, end = t.hour * 60 + t.minute, _minutes(quiet["from"]), _minutes(quiet["to"])
    return start <= minute < end if start < end else (minute >= start or minute < end)


def _conn() -> sqlite3.Connection:
    from core import database
    conn = sqlite3.connect(str(database.DB_PATH), timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS child_messages ("
                 " person TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0,"
                 " PRIMARY KEY (person, day))")
    return conn


def messages_today(child_id: str, now: Optional[datetime] = None) -> int:
    try:
        conn = _conn()
        try:
            row = conn.execute("SELECT n FROM child_messages WHERE person = ? AND day = ?",
                               (child_id, _local_now(now).strftime("%Y-%m-%d"))).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    return row[0] if row else 0


def refusal(child_id: Optional[str] = None, now: Optional[datetime] = None) -> Optional[str]:
    """Why FreeHand won't answer this child now, or None. A message that's
    answered is counted for today (count_message)."""
    from core import people
    child_id = child_id or people.current()
    if not is_child(child_id):
        return None
    r = rules(child_id)
    if in_quiet_hours(child_id, now):
        return (f"It's quiet time now, so FreeHand is resting until {r['quiet_hours']['to']}. "
                "Ask again then!")
    if messages_today(child_id, now) >= r["daily_messages"]:
        return (f"That's all {r['daily_messages']} of today's messages, so FreeHand is resting until "
                f"tomorrow. If you need more, ask {_names(r['parents'])}.")
    return None


def count_message(child_id: Optional[str] = None, now: Optional[datetime] = None) -> None:
    from core import people
    child_id = child_id or people.current()
    if not is_child(child_id):
        return
    try:
        conn = _conn()
        try:
            conn.execute("INSERT INTO child_messages (person, day, n) VALUES (?, ?, 1) "
                         "ON CONFLICT(person, day) DO UPDATE SET n = n + 1",
                         (child_id, _local_now(now).strftime("%Y-%m-%d")))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def forget(child_id: str) -> None:
    try:
        conn = _conn()
        try:
            conn.execute("DELETE FROM child_messages WHERE person = ?", (child_id,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


# ── Steps that need a parent ─────────────────────────────────────────

def needs_parent(tool_name: str) -> bool:
    """A child's step that reaches outside: a parent approves it."""
    from core import people
    if not is_child(people.current()):
        return False
    from core.agent_config import get_tool_permission
    from core.tools import composio_registry, registry
    if registry.is_destructive_tool(tool_name) or composio_registry.is_destructive_tool(tool_name):
        return True
    return get_tool_permission(tool_name) != "read" and tool_name not in LOCAL_WRITES


def tell_parents(approval_id: int, what: str, child_id: Optional[str] = None) -> None:
    """A note on each parent's console that their child is waiting."""
    from core import notices, people
    child_id = child_id or people.current()
    child = (people.get(child_id) or {}).get("name", "Your child")
    for parent in parents_of(child_id):
        try:
            notices.add(f"{child} asked FreeHand to: {what}. Approve or deny it under Approvals "
                        f"(#{approval_id}).", title=f"{child} is waiting for your yes", person=parent)
        except Exception:
            pass  # the approval itself is what matters


def waiting_message(child_id: Optional[str] = None) -> str:
    from core import people
    names = _names(parents_of(child_id or people.current()))
    return f"This needs a yes from {names} first. FreeHand will do it once they say yes."


def may_approve_for(row_person: str, approver: Optional[str] = None) -> bool:
    """Whether this person may approve a step asked for by row_person."""
    from core import people
    approver = approver or people.current()
    return approver == people.OWNER_ID or (is_child(row_person) and approver in parents_of(row_person))


# ── What the child is told, and the AI's guidance ────────────────────

def what_parents_see(child_id: Optional[str] = None) -> str:
    from core import people
    names = _names(parents_of(child_id or people.current()))
    return (f"{names} can see and answer anything FreeHand would send, post or do outside for you. "
            "They can't read your chats.")


_GUIDANCE = {
    "under_13": ("You're talking with a child under 13. Use short, simple, kind words. Keep to topics "
                 "suitable for a young child, and gently steer away from anything that isn't."),
    "13_to_15": ("You're talking with a young teenager (13 to 15). Be friendly and clear, and keep to "
                 "topics suitable for their age."),
    "16_to_17": ("You're talking with a teenager (16 or 17). Treat them with respect, and keep to topics "
                 "suitable for someone under 18."),
}


def guidance(child_id: Optional[str] = None) -> str:
    """Added to the AI's instructions for this child only."""
    from core import people
    child_id = child_id or people.current()
    if not is_child(child_id):
        return ""
    r = rules(child_id)
    return (
        "\n## Talking with a child\n"
        f"{_GUIDANCE[r['age_band']]}\n"
        "- With homework, help them think it through: explain, give hints and check their work. "
        "Don't just hand over the answers.\n"
        "- Never ask for personal details, such as their address, school, phone number or photos.\n"
        "- If they say they're being hurt, are in danger, or are thinking of hurting themselves, "
        "answer kindly, tell them it isn't their fault, encourage them to talk to a parent or a "
        "trusted adult now, and give Childline South Africa: 116 (free, any time).\n"
        f"- If they ask what their parents can see: {what_parents_see(child_id)}\n"
    )

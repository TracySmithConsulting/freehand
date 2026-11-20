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

- **A daily AI allowance,** set by the parent.
- **Parents' requests on their phone:** a child's waiting step also goes
  to each parent's own Telegram or WhatsApp, to answer with /approve.
- **What parents see,** their choice: the chats, a weekly summary, or
  only alerts. And when FreeHand is worried about the child, a parent is
  told, unless the child says a parent is the one hurting them.

What parents can see is said honestly to the child (``what_parents_see``).

Settings, on the child in "people":

    {"id": "lerato", "name": "Lerato", "role": "child", "parents": ["owner"],
     "quiet_hours": {"from": "21:00", "to": "06:00"}, "daily_messages": 40,
     "age_band": "under_13", "oversight": "full", "daily_ai": 300000}
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
    band = entry.get("age_band") if entry.get("age_band") in AGE_BANDS else "under_13"
    ai = entry.get("daily_ai")
    return {
        "parents": parents_of(child_id, settings),
        "quiet_hours": {"from": quiet.get("from", ""), "to": quiet.get("to", "")} if quiet else None,
        "daily_messages": daily if isinstance(daily, int) and not isinstance(daily, bool) else DEFAULT_DAILY_MESSAGES,
        "age_band": band,
        "oversight": entry.get("oversight") if entry.get("oversight") in OVERSIGHT else suggested_oversight(band),
        "daily_ai": ai if isinstance(ai, int) and not isinstance(ai, bool) and ai > 0 else None,
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
    oversight = str(body.get("oversight") or suggested_oversight(band))
    if oversight not in OVERSIGHT:
        raise ChildError("Choose what their parents see.")
    allowance = str(body.get("daily_ai") or "none")
    if allowance != "none" and allowance not in ALLOWANCES:
        raise ChildError("Choose their AI allowance a day.")
    return {"parents": parents or [people.OWNER_ID], "quiet_hours": quiet,
            "daily_messages": daily, "age_band": band, "oversight": oversight,
            "daily_ai": ALLOWANCES.get(allowance)}


def save_rules(child_id: str, cleaned: dict) -> None:
    from core import agent_config
    s = agent_config._load_settings()
    entries = list(s.get("people") or [])
    for i, p in enumerate(entries):
        if isinstance(p, dict) and str(p.get("id", "")).strip().lower() == child_id:
            was_full = rules(child_id, s)["oversight"] == "full" and p.get("role") == ROLE
            p = {**p, "role": ROLE, "parents": cleaned["parents"],
                 "daily_messages": cleaned["daily_messages"], "age_band": cleaned["age_band"],
                 "oversight": cleaned.get("oversight") or suggested_oversight(cleaned["age_band"])}
            for key in ("quiet_hours", "daily_ai"):
                if cleaned.get(key):
                    p[key] = cleaned[key]
                else:
                    p.pop(key, None)
            entries[i] = p
            s["people"] = entries
            agent_config._save_settings(s)
            if was_full and p["oversight"] != "full":
                forget_chats(child_id)  # out of full view, the chats kept go
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
                          if k not in ("parents", "quiet_hours", "daily_messages", "age_band",
                                       "oversight", "daily_ai")}
            entries[i]["role"] = "member"
            s["people"] = entries
            agent_config._save_settings(s)
            forget_activity(person_id)
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
    if r["daily_ai"] and ai_today(child_id, now) >= r["daily_ai"]:
        return ("That's all of today's AI allowance, so FreeHand is resting until tomorrow. "
                f"If you need more, ask {_names(r['parents'])}.")
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
    forget_activity(child_id)


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
    child_id = child_id or people.current()
    names = _names(parents_of(child_id))
    mode = rules(child_id)["oversight"]
    seen = {
        "full": f"{names} can read your chats with FreeHand.",
        "summary": (f"{names} get a weekly summary: how much you used FreeHand and what kinds of things it "
                    "helped with, not your words."),
        "alerts": f"{names} can't read your chats.",
    }[mode]
    return (f"{seen} They see and answer anything FreeHand would send, post or do outside for you, and "
            "FreeHand tells them if you seem to be in danger.")


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
        "trusted adult now, and give Childline South Africa: 116 (free, any time). Also call "
        f"{ALERT_TOOL}, and tell them you're letting their parent know. If they say a parent or "
        "someone at home is hurting them, call it with about_a_parent true (no parent is told), "
        "don't say a parent will know, and encourage them to talk to another trusted adult, such as "
        "a teacher.\n"
        f"- If they ask what their parents can see: {what_parents_see(child_id)}\n"
    )


# ── A daily AI allowance (Tracy's choice, 20 Nov 2026) ───────────────
# FreeHand doesn't know every model's price, so the cap is on how much AI
# a child's messages use in a day (tokens), not on money. A long answer,
# a document or a web search uses more of it than a short question.

ALLOWANCES = {"small": 100_000, "medium": 300_000, "large": 1_000_000}


def _ai_conn() -> sqlite3.Connection:
    conn = _conn()
    conn.execute("CREATE TABLE IF NOT EXISTS child_ai_day ("
                 " person TEXT NOT NULL, day TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0,"
                 " PRIMARY KEY (person, day))")
    return conn


def ai_today(child_id: str, now: Optional[datetime] = None) -> int:
    try:
        conn = _ai_conn()
        try:
            row = conn.execute("SELECT tokens FROM child_ai_day WHERE person = ? AND day = ?",
                               (child_id, _local_now(now).strftime("%Y-%m-%d"))).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    return row[0] if row else 0


def count_ai(usage: Optional[dict], child_id: Optional[str] = None, now: Optional[datetime] = None) -> None:
    from core import people
    child_id = child_id or people.current()
    if not usage or not is_child(child_id):
        return
    tokens = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    if tokens <= 0:
        return
    try:
        conn = _ai_conn()
        try:
            conn.execute("INSERT INTO child_ai_day (person, day, tokens) VALUES (?, ?, ?) "
                         "ON CONFLICT(person, day) DO UPDATE SET tokens = tokens + excluded.tokens",
                         (child_id, _local_now(now).strftime("%Y-%m-%d"), tokens))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # counting must never break a reply


def allowance_used(child_id: str, now: Optional[datetime] = None) -> Optional[int]:
    """How much of today's allowance is used, as a percentage; None if no cap."""
    cap = rules(child_id)["daily_ai"]
    if not cap:
        return None
    return min(100, round(100 * ai_today(child_id, now) / cap))


# ── What parents see (Tracy's choice, 20 Nov 2026) ───────────────────
# Each child's parents choose: "full" (read the chats), "summary" (a
# weekly summary: how much and what kinds of help, never their words) or
# "alerts" (only when FreeHand is worried). FreeHand suggests full view
# under 13 and a summary for teenagers. Chats are kept only in full view,
# and for KEEP_DAYS.

OVERSIGHT = {  # as the parents' page says it
    "full": "You can read their chats",
    "summary": "You get a weekly summary, not their chats",
    "alerts": "You get alerts only, when FreeHand is worried",
}
KEEP_DAYS = 30
MAX_KEPT_CHARS = 4000
SUMMARY_DAYS = 7
MAX_ALERTS_A_DAY = 3


def suggested_oversight(age_band: str) -> str:
    return "full" if age_band == "under_13" else "summary"


def _activity_conn() -> sqlite3.Connection:
    conn = _conn()
    conn.execute("CREATE TABLE IF NOT EXISTS child_activity ("
                 " id INTEGER PRIMARY KEY AUTOINCREMENT, person TEXT NOT NULL, at TEXT NOT NULL,"
                 " kind TEXT NOT NULL, asked TEXT NOT NULL DEFAULT '', answered TEXT NOT NULL DEFAULT '',"
                 " helped TEXT NOT NULL DEFAULT '')")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_child_activity ON child_activity(person, at)")
    conn.execute("CREATE TABLE IF NOT EXISTS child_summaries (person TEXT PRIMARY KEY, sent_at TEXT NOT NULL)")
    return conn


def _iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _since(days: int, now: Optional[datetime] = None) -> str:
    from datetime import timedelta
    return _iso((now or datetime.now(timezone.utc)) - timedelta(days=days))


# The kinds of help, in plain words, from the tools FreeHand used.
_KINDS = (
    (("search", "browse", "fetch", "web"), "looking things up"),
    (("write_docx", "write_xlsx", "write_pptx"), "making documents"),
    (("read_docx", "read_xlsx", "read_pptx", "read_text_file", "list_folder"), "reading files"),
    (("email",), "email"),
    (("calendar",), "the calendar"),
    (("scribble", "note"), "notes"),
    (("remind", "schedule", "task"), "reminders"),
)


def _kinds(tools_used) -> list:
    found = []
    for t in tools_used or []:
        name = str((t or {}).get("name", "") if isinstance(t, dict) else t).lower()
        for words, kind in _KINDS:
            if any(w in name for w in words) and kind not in found:
                found.append(kind)
    return found


def _purge(conn: sqlite3.Connection, now: Optional[datetime] = None) -> None:
    conn.execute("DELETE FROM child_activity WHERE at < ?", (_since(KEEP_DAYS, now),))


def record(command: str, result: Optional[dict], child_id: Optional[str] = None,
           now: Optional[datetime] = None) -> None:
    """Keep what a child's parents may see of this message: the words only
    in full view; how much and what kind of help for a summary; nothing
    for alerts only."""
    from core import people
    child_id = child_id or people.current()
    if not is_child(child_id):
        return
    mode = rules(child_id)["oversight"]
    if mode == "alerts":
        return
    result = result or {}
    full = mode == "full"
    try:
        conn = _activity_conn()
        try:
            _purge(conn, now)
            conn.execute("INSERT INTO child_activity (person, at, kind, asked, answered, helped) VALUES (?, ?, 'chat', ?, ?, ?)",
                         (child_id, _iso(now), str(command or "")[:MAX_KEPT_CHARS] if full else "",
                          str(result.get("text") or "")[:MAX_KEPT_CHARS] if full else "",
                          ", ".join(_kinds(result.get("tools_used")))))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass  # keeping a copy must never break a reply


def forget_chats(child_id: str) -> None:
    """Out of full view: the chats kept so far go."""
    try:
        conn = _activity_conn()
        try:
            conn.execute("UPDATE child_activity SET asked = '', answered = '' WHERE person = ? AND kind = 'chat'",
                         (child_id,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _rows(child_id: str, kind: str, days: int, now: Optional[datetime] = None, limit: int = 200) -> list:
    try:
        conn = _activity_conn()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT at, asked, answered, helped FROM child_activity WHERE person = ? AND kind = ?"
                                " AND at >= ? ORDER BY id DESC LIMIT ?",
                                (child_id, kind, _since(days, now), limit)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def summary(child_id: str, now: Optional[datetime] = None) -> dict:
    """The last week, without their words: how many messages, on how many
    days, and what kinds of help."""
    from core import scheduler
    rows = _rows(child_id, "chat", SUMMARY_DAYS, now, limit=10_000)
    days, kinds = set(), []
    for r in rows:
        try:
            days.add(datetime.strptime(r["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                     .astimezone(scheduler.local_tz()).strftime("%Y-%m-%d"))
        except ValueError:
            pass
        for k in [k.strip() for k in r["helped"].split(",") if k.strip()]:
            if k not in kinds:
                kinds.append(k)
    alerts = len(_rows(child_id, "alert", SUMMARY_DAYS, now))
    return {"messages": len(rows), "days": len(days), "kinds": kinds, "alerts": alerts}


def summary_text(child_id: str, now: Optional[datetime] = None) -> str:
    from core import people
    name = (people.get(child_id) or {}).get("name", "Your child")
    s = summary(child_id, now)
    if not s["messages"]:
        text = f"{name} didn't use FreeHand this week."
    else:
        text = (f"{name} sent FreeHand {s['messages']} message{'s' if s['messages'] != 1 else ''} this week, "
                f"on {s['days']} day{'s' if s['days'] != 1 else ''}.")
        if s["kinds"]:
            kinds = s["kinds"]
            text += " Besides talking, FreeHand helped with " + (
                kinds[0] if len(kinds) == 1 else ", ".join(kinds[:-1]) + " and " + kinds[-1]) + "."
    if s["alerts"]:
        times = {1: "once", 2: "twice"}.get(s["alerts"], f"{s['alerts']} times")
        text += f" FreeHand was worried about them {times} this week."
    return text


def activity(child_id: str, now: Optional[datetime] = None) -> dict:
    """What this child's parents may see, by their choice."""
    from core import people
    r = rules(child_id)
    out = {"id": child_id, "name": (people.get(child_id) or {}).get("name", child_id),
           "oversight": r["oversight"], "oversight_words": OVERSIGHT[r["oversight"]],
           "alerts": [{"at": a["at"], "worry": a["asked"]} for a in _rows(child_id, "alert", KEEP_DAYS, now)],
           "messages_today": messages_today(child_id, now), "daily_messages": r["daily_messages"],
           "allowance_used": allowance_used(child_id, now)}
    if r["oversight"] == "full":
        out["chats"] = [{"at": c["at"], "asked": c["asked"], "answered": c["answered"]}
                        for c in _rows(child_id, "chat", KEEP_DAYS, now, limit=100)]
    if r["oversight"] in ("full", "summary"):
        out["summary"] = summary_text(child_id, now)
    return out


def parent_may_see(child_id: str, person_id: Optional[str] = None) -> bool:
    from core import people
    person_id = person_id or people.current()
    return is_child(child_id) and (person_id == people.OWNER_ID or person_id in parents_of(child_id))


def children_for(person_id: Optional[str] = None) -> list:
    """The children this person is a parent of (the owner: every child)."""
    from core import people
    person_id = person_id or people.current()
    if person_id == people.OWNER_ID:
        return [p["id"] for p in people.list_people() if p["role"] == ROLE]
    return children_of(person_id)


# ── Telling parents, on their console and their phone ────────────────

async def _phone(parent_id: str, text: str) -> bool:
    """To the parent's own Telegram or WhatsApp chat, if they have one."""
    from core import notices, sessions
    where = sessions.code_destination(parent_id)
    if where is None:
        return False
    try:
        return bool(await notices._send(where[0], where[1], text))
    except Exception:
        return False


async def ask_parents(approval_id: int, what: str, child_id: Optional[str] = None) -> None:
    """A child is waiting: a note on each parent's console, and a message
    on their phone they can answer with /approve or /deny (Tracy's choice,
    20 Nov 2026). An unanswered one is still never done."""
    from core import people
    child_id = child_id or people.current()
    tell_parents(approval_id, what, child_id)
    child = (people.get(child_id) or {}).get("name", "Your child")
    text = (f"{child} asked FreeHand to: {what}\n\n"
            f"Reply /approve {approval_id} to let FreeHand do it, or /deny {approval_id}. "
            "If you don't answer, it isn't done.")
    for parent in parents_of(child_id):
        await _phone(parent, text)


def parent_chat_matches(parent_id: str, source: str, caller_id: str) -> bool:
    """Whether this chat is the parent's own (where their requests go)."""
    from core import people, sessions
    where = sessions.code_destination(parent_id)
    if where is None or where[0] != source:
        return False
    if source == "whatsapp":
        return people.identity("whatsapp", caller_id) == people.identity("whatsapp", where[1])
    return str(caller_id).strip() == where[1]


# ── When FreeHand is worried (Tracy's choice, 20 Nov 2026) ───────────
# A child who says they're being hurt or are in danger gets a kind answer
# and Childline, and a parent is told. If a parent is the one hurting
# them, no parent is told: only Childline. This depends on the AI
# noticing, so it isn't watertight, and the People page says so.

ALERT_TOOL = "tell_parents_im_worried"


def alert_schema() -> dict:
    return {"type": "function", "function": {
        "name": ALERT_TOOL,
        "description": ("Tell the child's parents that you're worried about the child: they said they're being "
                        "hurt, are in danger, or are thinking of hurting themselves. Tell the child you're letting "
                        "their parent know. Set about_a_parent to true if the child says a parent or someone at "
                        "home is hurting them: then no parent is told."),
        "parameters": {"type": "object", "properties": {
            "worry": {"type": "string", "description": "In a sentence, what the child said that worries you."},
            "about_a_parent": {"type": "boolean",
                               "description": "True if a parent or someone at home is the one hurting them."},
        }, "required": ["worry", "about_a_parent"]},
    }}


async def raise_alert(worry: str, about_a_parent, child_id: Optional[str] = None,
                      now: Optional[datetime] = None) -> dict:
    from core import notices, people
    child_id = child_id or people.current()
    if not is_child(child_id):
        return {"error": "This is only for children."}
    if about_a_parent is not False:  # anything but a clear "no" protects the child
        return {"not_told": True, "tell_the_person": (
            "No parent was told. Give them Childline South Africa: 116 (free, any time), and encourage them "
            "to talk to another trusted adult, such as a teacher.")}
    worry = " ".join(str(worry or "").split())[:300] or "something they said"
    if len(_rows(child_id, "alert", 1, now)) >= MAX_ALERTS_A_DAY:
        return {"told": True, "tell_the_person": "Their parents already know today. Keep being kind, and give Childline: 116."}
    try:
        conn = _activity_conn()
        try:
            _purge(conn, now)
            conn.execute("INSERT INTO child_activity (person, at, kind, asked) VALUES (?, ?, 'alert', ?)",
                         (child_id, _iso(now), worry))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    name = (people.get(child_id) or {}).get("name", "Your child")
    text = (f"FreeHand is worried about {name}. They said: {worry}\n\n"
            "FreeHand answered kindly and gave them Childline (116). Please talk with them soon.")
    for parent in parents_of(child_id):
        try:
            notices.add(text, title=f"FreeHand is worried about {name}", person=parent)
        except Exception:
            pass
        await _phone(parent, text)
    return {"told": True, "tell_the_person": (
        "Their parent has been told. Tell the child you've let their parent know, be kind, and give "
        "Childline: 116.")}


# ── The weekly summary ───────────────────────────────────────────────

def _summary_due(child_id: str, now: Optional[datetime] = None) -> bool:
    """Once a week. The first week starts when the child is first seen."""
    try:
        conn = _activity_conn()
        try:
            row = conn.execute("SELECT sent_at FROM child_summaries WHERE person = ?", (child_id,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO child_summaries (person, sent_at) VALUES (?, ?)", (child_id, _iso(now)))
                conn.commit()
                return False
            if row[0] > _since(SUMMARY_DAYS, now):
                return False
            conn.execute("UPDATE child_summaries SET sent_at = ? WHERE person = ?", (_iso(now), child_id))
            conn.commit()
            return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False


async def send_weekly_summaries(now: Optional[datetime] = None) -> int:
    """For each child whose parents chose a summary: once a week, to each
    parent's console and phone. Made without AI. Returns how many sent."""
    from core import notices, people
    sent = 0
    for child in [p["id"] for p in people.list_people() if p["role"] == ROLE]:
        if rules(child)["oversight"] != "summary" or not _summary_due(child, now):
            continue
        text = summary_text(child, now) + " More under Your children, in FreeHand."
        name = (people.get(child) or {}).get("name", "Your child")
        for parent in parents_of(child):
            try:
                notices.add(text, title=f"{name}'s week with FreeHand", person=parent)
            except Exception:
                pass
            await _phone(parent, text)
        sent += 1
    return sent


def forget_activity(child_id: str) -> None:
    try:
        conn = _activity_conn()
        try:
            conn.execute("DELETE FROM child_activity WHERE person = ?", (child_id,))
            conn.execute("DELETE FROM child_summaries WHERE person = ?", (child_id,))
            conn.execute("DELETE FROM child_ai_day WHERE person = ?", (child_id,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass

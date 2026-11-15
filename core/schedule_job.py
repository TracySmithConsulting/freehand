"""Setting up a scheduled job in plain words (build step 2 of
docs/design/scheduled-jobs-and-team-members.md).

Used by the model's ``schedule_job`` tool and by the web page's form, so
both understand the same things:

- **What:** "remind" (a message, word for word) and "todos" (the person's
  to-do list) run in code with no model, so they're free however often
  they run. "think" asks the model each time, for jobs that need work.
- **Checks:** "check_email" (new emails from a sender, or with some words)
  and "check_page" (a web page changed, or now mentions some words). Code
  checks, which is free; the person is told, or the model woken, only when
  something is found (core/checks.py).
- **When:** once (today, tomorrow, a weekday, a date, or in N minutes), or
  every day, every weekday, every week on a day, every month on a date, at
  a time in the person's own time zone. Code does the date maths, which
  small models get wrong.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

TOOL = "schedule_job"
TYPES = ("remind", "todos", "check_email", "check_page", "think")
CHECK_TYPES = ("check_email", "check_page")
REPEATS = ("once", "hourly", "daily", "weekdays", "weekly", "monthly")
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
CHANNELS = ("telegram", "slack", "whatsapp")

KEYWORDS = {
    "remind", "reminder", "reminders", "schedule", "scheduled", "every", "daily", "weekly",
    "monthly", "tomorrow", "tonight", "later", "minutes", "hour", "hours", "alarm",
    "weekday", "weekdays", "morning", "evening", "o'clock", "oclock", *DAYS,
    "hourly", "watch", "monitor", "notify", "alert", "changes", "changed",
}


def wanted(texts) -> bool:
    words = set()
    for t in texts:
        words.update(re.findall(r"[a-z']+", (t or "").lower()))
    return bool(words & KEYWORDS)


def schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": TOOL,
            "description": (
                "Set up something for FreeHand to do later, once or on a repeat. "
                "type 'remind' sends a reminder message and 'todos' sends the person's to-do list: "
                "both are free (no AI runs), so use them whenever the job is only a message. "
                "'check_email' (new emails from a sender or with some words) and 'check_page' (a web page "
                "changes, or starts to mention some words) are free to check: code checks, and the person is "
                "told only when something is found. With then='think', you're asked about what was found, "
                "following 'instructions'. Checks repeat, every hour by default. "
                "Use 'think' only when the job needs work at the time, like a summary or a draft. "
                "When the person names one of their team members to do it (\"have Dev ...\"), use 'think' "
                "with team_member, so that team member does it with their own tools. "
                "Give times in the person's own time; code works out the dates. "
                "Afterwards, tell the person the summary this returns, and describe the job as it was saved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(TYPES)},
                    "what": {"type": "string", "description": "The job in a few words, e.g. 'Phone the dentist'."},
                    "message": {"type": "string", "description": "For 'remind': the reminder to send, e.g. 'Reminder: phone the dentist.'"},
                    "repeat": {"type": "string", "enum": list(REPEATS), "description": "Default 'once'."},
                    "date": {"type": "string", "description": "For 'once': today, tomorrow, a weekday name, or YYYY-MM-DD."},
                    "time": {"type": "string", "description": "HH:MM, 24-hour, e.g. 09:00 or 17:30."},
                    "in_minutes": {"type": "integer", "description": "For 'once': instead of date and time, this many minutes from now."},
                    "weekday": {"type": "string", "enum": list(DAYS), "description": "For 'weekly'."},
                    "day_of_month": {"type": "integer", "description": "For 'monthly': 1 to 28."},
                    "sender": {"type": "string", "description": "For 'check_email': part of the sender's name or address, e.g. 'fnb'."},
                    "words": {"type": "string", "description": "For checks: words to look for, separated by commas. Any one counts."},
                    "url": {"type": "string", "description": "For 'check_page': the page's full address."},
                    "then": {"type": "string", "enum": ["tell", "think"], "description": "For checks: 'tell' (default, free) or 'think'."},
                    "instructions": {"type": "string", "description": "For checks with then='think': what to do with what's found."},
                    "team_member": {"type": "string", "description": "For 'think': the name of the person's team member who should do it, if they asked for one."},
                },
                "required": ["type", "what"],
            },
        },
    }


def _time(value) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2})(?:[:h.](\d{2}))?\s*(am|pm)?\s*", str(value or "").lower())
    if not m:
        raise ValueError("Say the time as hours and minutes, for example 09:00 or 17:30.")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and hour < 12:
        hour += 12
    if m.group(3) == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError("That time doesn't exist. Use 00:00 to 23:59.")
    return hour, minute


def _date(value, now: datetime):
    text = str(value or "today").strip().lower()
    if text in ("", "today"):
        return now.date()
    if text == "tomorrow":
        return (now + timedelta(days=1)).date()
    if text in DAYS:
        ahead = (DAYS.index(text) - now.weekday()) % 7
        return (now + timedelta(days=ahead)).date()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Say the day as today, tomorrow, a weekday, or a date like 2026-12-24.")


def build(spec: dict, tz=None, now: Optional[datetime] = None) -> dict:
    """Plain choices -> what create_task needs. Raises ValueError with a
    plain-words message when something doesn't make sense."""
    spec = spec if isinstance(spec, dict) else {}
    tz = tz or timezone.utc
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    kind = str(spec.get("type") or "remind").strip().lower()
    if kind not in TYPES:
        raise ValueError("Choose a reminder, your to-do list, or something for FreeHand to do.")
    what = str(spec.get("what") or spec.get("title") or "").strip()[:200]
    if not what and kind == "todos":
        what = "Send me my to-do list"
    if not what:
        raise ValueError("Say what the job is.")
    action = None
    if kind in CHECK_TYPES:
        then = str(spec.get("then") or "tell").strip().lower()
        if then not in ("tell", "think"):
            then = "tell"
        action = {"type": "check", "check": "email" if kind == "check_email" else "page",
                  "words": str(spec.get("words") or "").strip()[:200], "then": then,
                  "instructions": str(spec.get("instructions") or "").strip()[:500]}
        if kind == "check_email":
            action["sender"] = str(spec.get("sender") or "").strip()[:100]
            action["account"] = str(spec.get("account") or "").strip()[:100]
            if not action["sender"] and not action["words"]:
                raise ValueError("Say who the email is from, or which words to look for.")
        else:
            url = str(spec.get("url") or "").strip()
            if url and "://" not in url:
                url = "https://" + url
            if not re.match(r"https?://[^\s/]+\.[^\s]+", url):
                raise ValueError("Give the web page's full address, for example https://example.com/tickets")
            action["url"] = url[:500]
        # A check that runs once only notes what's there now: it needs a repeat.
        if str(spec.get("repeat") or "once").strip().lower() == "once":
            spec = {**spec, "repeat": "hourly"}
    elif kind == "remind":
        action = {"type": "remind", "text": str(spec.get("message") or what).strip()[:500]}
    elif kind == "todos":
        action = {"type": "todos"}

    repeat = str(spec.get("repeat") or "once").strip().lower()
    if repeat not in REPEATS:
        raise ValueError("Choose once, every day, every weekday, every week or every month.")
    cron, run_at = "", None
    if repeat == "once":
        minutes = spec.get("in_minutes")
        if minutes not in (None, ""):
            try:
                minutes = int(minutes)
            except (TypeError, ValueError):
                raise ValueError("Say how many minutes from now, as a number.")
            if not 1 <= minutes <= 60 * 24 * 366:
                raise ValueError("Choose between 1 minute and a year from now.")
            run_at = now + timedelta(minutes=minutes)
        else:
            day = _date(spec.get("date"), now)
            hour, minute = _time(spec.get("time") or "09:00")
            run_at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            if run_at <= now:
                if str(spec.get("date") or "").strip().lower() in DAYS:
                    run_at += timedelta(days=7)  # "on Monday" when it's Monday afternoon
                else:
                    raise ValueError("That time has already passed. Choose a time in the future.")
    else:
        hour, minute = _time(spec.get("time") or "09:00")
        if repeat == "hourly":
            cron = f"{minute} * * * *"
        elif repeat == "daily":
            cron = f"{minute} {hour} * * *"
        elif repeat == "weekdays":
            cron = f"{minute} {hour} * * 1-5"
        elif repeat == "weekly":
            day = str(spec.get("weekday") or "").strip().lower()
            if day not in DAYS:
                raise ValueError("Choose which day of the week.")
            cron = f"{minute} {hour} * * {(DAYS.index(day) + 1) % 7}"
        else:
            try:
                dom = int(spec.get("day_of_month"))
            except (TypeError, ValueError):
                raise ValueError("Choose which day of the month, 1 to 28.")
            if not 1 <= dom <= 28:
                raise ValueError("Choose a day from 1 to 28, so it happens every month.")
            cron = f"{minute} {hour} {dom} * *"
    member = str(spec.get("team_member") or "").strip()[:40] if kind == "think" else ""
    return {"title": what, "cron": cron, "run_at": run_at,
            "kind": "think" if kind == "think" else "check" if kind in CHECK_TYPES else "do",
            "action": action, "team_member": member or None}


def resolve_member(job: dict) -> None:
    """Turn the team member's name into their id, or say plainly they don't exist."""
    if not job.get("team_member"):
        return
    from core import team
    member = team.get(job["team_member"])
    if not member:
        raise ValueError(f"You don't have a team member called {job['team_member']}.")
    job["team_member"] = member["id"]


def cost(kind: str, action=None) -> str:
    """What a job costs, in plain words."""
    if kind == "do":
        return "Free."
    if kind == "check":
        from core import checks
        if isinstance(action, str):
            try:
                action = json.loads(action)
            except ValueError:
                action = {}
        return checks.cost(action or {})
    return "Uses AI each time."


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _when(dt: datetime) -> str:
    return f"{DAYS[dt.weekday()].capitalize()} {dt.day} {dt.strftime('%B')} at {dt:%H:%M}"


def describe(cron: str, next_run: Optional[str], status: str = "pending", tz=None) -> str:
    """When a job happens, in plain words."""
    tz = tz or timezone.utc
    nxt = None
    if next_run:
        try:
            nxt = datetime.fromisoformat(str(next_run).replace("Z", "+00:00")).astimezone(tz)
        except ValueError:
            nxt = None
    if status == "todo":
        return "On your to-do list (no time set)"
    cron = (cron or "").strip()
    if not cron:
        if status == "done":
            return "Done"
        return "Once, " + _when(nxt) if nxt else "Once, as soon as possible"
    parts = cron.split()
    if len(parts) == 5 and parts[0].isdigit() and parts[1:] == ["*", "*", "*", "*"]:
        return "Every hour" if parts[0] == "0" else f"Every hour, at {int(parts[0])} minutes past"
    simple = len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[3] == "*"
    if simple:
        at = f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        dom, dow = parts[2], parts[4]
        if dom == "*" and dow == "*":
            return f"Every day at {at}"
        if dom == "*" and dow == "1-5":
            return f"Every weekday at {at}"
        if dom == "*" and dow.isdigit() and int(dow) <= 7:
            return f"Every {DAYS[(int(dow) - 1) % 7].capitalize()} at {at}"
        if dow == "*" and dom.isdigit():
            return f"Every month on the {_ordinal(int(dom))} at {at}"
    return f"On a custom schedule ({cron})"


def usage_note(task: dict, tz=None, now: Optional[datetime] = None, per_job: Optional[int] = None) -> str:
    """What a job used with AI this month, in plain words, or "" for none."""
    month = (now or datetime.now(timezone.utc)).astimezone(tz or timezone.utc).strftime("%Y-%m")
    runs = int(task.get("month_runs") or 0)
    if task.get("usage_month") != month or not runs:
        return ""
    tokens = int(task.get("month_tokens") or 0)
    limit = f" of {per_job}" if per_job is not None else ""
    return (f"This month: used AI {runs}{limit} time{'s' if runs != 1 or limit else ''}, "
            f"about {tokens:,} tokens.")


def target(source: str, caller_id: str) -> dict:
    """Where results go: back to the chat app it was asked on, if any (the
    web page always gets them too)."""
    if source in CHANNELS and caller_id:
        return {"channel": source, "to": caller_id}
    return {"channel": "web"}


def run_tool(args: dict, source: str = "", caller_id: str = "") -> str:
    """The model's schedule_job tool. Returns JSON for the model."""
    from core import scheduler
    tz = scheduler.local_tz()
    try:
        job = build(args, tz=tz)
        if job["kind"] == "check":
            from core import checks
            checks.validate(job["action"])
        resolve_member(job)
    except ValueError as e:
        return json.dumps({"saved": False, "problem": str(e)})
    except Exception as e:  # checks.CheckError: plain words already
        return json.dumps({"saved": False, "problem": str(e)})
    created = scheduler.create_task(job["title"], job["cron"], deliver_to=target(source, caller_id),
                                    kind=job["kind"], action=job["action"], run_at=job["run_at"],
                                    team_member=job["team_member"])
    summary = describe(created["cron_schedule"], created["next_run"], tz=tz)
    free = job["kind"] != "think"
    note = {"do": "Free, no AI needed."}.get(job["kind"]) or cost(job["kind"], job["action"])
    if job["kind"] == "check":
        note += " The first check only notes what's there now; after that, only new things count."
    # So the reply doesn't promise what FreeHand can't do.
    facts = ["To stop or change it, the person uses the Scheduled jobs screen (Pause or Delete); "
             "it can't be stopped from the chat yet."]
    if job["action"] and job["action"].get("check") == "email":
        facts.append("The check sees each email's sender, subject and first few lines, not the whole email.")
    return json.dumps({
        "saved": True,
        "summary": f"{job['title']}: {summary}. {note}",
        "facts_for_your_reply": " ".join(facts),
        "free": free,
        "time_zone_known": tz is not timezone.utc,
    })

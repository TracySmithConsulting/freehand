"""Managing scheduled jobs from the chat: list, pause, resume, change and
delete. People on Telegram, Slack or WhatsApp can't reach the Scheduled
jobs screen, so they can do it all by asking.

- Only the person's own jobs, never someone else's in a shared FreeHand.
- Pausing and resuming are instant: they can be undone.
- Deleting can't be undone, so it takes two steps: FreeHand names the job
  and asks, and only a "yes" in a later message deletes it. The model can't
  confirm on the person's behalf in the same message.
- A change keeps what isn't mentioned: "move it to 10" keeps the day and
  the repeat (core/schedule_job.build does the rest, with the same checks
  as setting a job up).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

TOOL = "manage_jobs"
ACTIONS = ("list", "pause", "resume", "change", "delete")
WHEN_FIELDS = ("repeat", "date", "time", "in_minutes", "weekday", "day_of_month")
CHECK_FIELDS = ("words", "sender", "url", "account")

# Words that bring the tool in, when the person has jobs.
_NOUNS = {"job", "jobs", "reminder", "reminders", "scheduled", "schedule", "alarm", "alarms", "watch",
          "watching", "check", "checks", "checking", "report", "summary", "summaries", "list"}
_VERBS = {"stop", "cancel", "pause", "resume", "unpause", "restart", "delete", "remove", "change", "move",
          "reschedule", "edit", "postpone", "earlier", "later", "snooze"}

# Deletes waiting for a yes: (person, source, caller) -> {"id", "asked"}.
_PENDING: dict = {}


def wanted(texts, person: Optional[str] = None) -> bool:
    words = set()
    for t in texts:
        words.update(re.findall(r"[a-z']+", (t or "").lower()))
    if not words & (_NOUNS | _VERBS):
        return False
    return bool(_rows(person, live_only=True))


def schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": TOOL,
            "description": (
                "See and manage the person's scheduled jobs (reminders, checks, reports). Use 'list' first to "
                "find the job's id. 'pause' and 'resume' can be undone. 'change' keeps whatever you don't "
                "mention: give only what changes, for example just a new time. 'delete' can't be undone: "
                "the first call returns what to ask the person; delete only after they say yes in their next "
                "message, by calling 'delete' again. If they only want it to stop for now, pause it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(ACTIONS)},
                    "job_id": {"type": "integer"},
                    "what": {"type": "string", "description": "For 'change': a new name for the job."},
                    "message": {"type": "string", "description": "For 'change' of a reminder: its new words."},
                    "repeat": {"type": "string", "enum": ["once", "hourly", "daily", "weekdays", "weekly", "monthly"]},
                    "date": {"type": "string", "description": "today, tomorrow, a weekday name, or YYYY-MM-DD."},
                    "time": {"type": "string", "description": "HH:MM, 24-hour, in the person's own time."},
                    "in_minutes": {"type": "integer"},
                    "weekday": {"type": "string"},
                    "day_of_month": {"type": "integer"},
                    "words": {"type": "string", "description": "For checks: new words to look for."},
                    "sender": {"type": "string", "description": "For email checks: a new sender."},
                    "url": {"type": "string", "description": "For page checks: a new address."},
                },
                "required": ["action"],
            },
        },
    }


# ── The jobs ──────────────────────────────────────────────────────────

def _rows(person: Optional[str] = None, live_only: bool = False) -> list:
    from core import people, scheduler
    conn = scheduler._tasks_conn()
    try:
        people.ensure_person_column(conn, "tasks")
        scheduler._ensure_deliver_column(conn)
        rows = conn.execute(
            "SELECT * FROM tasks WHERE person = ? AND status IN ('pending', 'paused') ORDER BY id",
            (person or people.current(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _job(job_id, person: Optional[str] = None) -> Optional[dict]:
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return None
    return next((r for r in _rows(person) if r["id"] == job_id), None)


def _say(row: dict) -> dict:
    from core import schedule_job, scheduler, team
    tz = scheduler.local_tz()
    out = {
        "id": row["id"],
        "what": row["title"],
        "when": schedule_job.describe(row["cron_schedule"], row["next_run"], row["status"], tz=tz),
        "cost": schedule_job.cost(row.get("kind") or "think", row.get("action")),
        "paused": row["status"] == "paused",
    }
    if row.get("team_member"):
        member = team.get(row["team_member"])
        out["done_by"] = member["name"] if member else "(a team member who has been deleted)"
    return out


def _spec_from(row: dict, tz) -> dict:
    """The plain choices that would make this job again (the reverse of
    schedule_job.build), so a change can keep what isn't mentioned."""
    from core import schedule_job
    try:
        action = json.loads(row.get("action") or "{}") or {}
    except ValueError:
        action = {}
    kind = row.get("kind") or "think"
    if kind == "do":
        spec = {"type": "todos"} if action.get("type") == "todos" else \
               {"type": "remind", "message": action.get("text") or row["title"]}
    elif kind == "check":
        spec = {"type": "check_email" if action.get("check") == "email" else "check_page",
                **{k: action.get(k) for k in CHECK_FIELDS + ("then", "instructions") if action.get(k)}}
    else:
        spec = {"type": "think"}
        if row.get("team_member"):
            spec["team_member"] = row["team_member"]
    spec["what"] = row["title"]
    cron = (row.get("cron_schedule") or "").split()
    if not cron and row.get("next_run"):
        at = datetime.fromisoformat(row["next_run"].replace("Z", "+00:00")).astimezone(tz)
        spec.update(repeat="once", date=at.strftime("%Y-%m-%d"), time=at.strftime("%H:%M"))
    elif len(cron) == 5 and cron[0].isdigit() and cron[3] == "*":
        minute, hour, dom, dow = cron[0], cron[1], cron[2], cron[4]
        spec["time"] = f"{int(hour):02d}:{int(minute):02d}" if hour.isdigit() else f"00:{int(minute):02d}"
        if hour == "*" and dom == "*" and dow == "*":
            spec["repeat"] = "hourly"
        elif dom == "*" and dow == "*":
            spec["repeat"] = "daily"
        elif dom == "*" and dow == "1-5":
            spec["repeat"] = "weekdays"
        elif dom == "*" and dow.isdigit():
            spec.update(repeat="weekly", weekday=schedule_job.DAYS[(int(dow) - 1) % 7])
        elif dow == "*" and dom.isdigit():
            spec.update(repeat="monthly", day_of_month=int(dom))
        else:
            spec["custom_cron"] = row["cron_schedule"]
    elif cron:
        spec["custom_cron"] = row["cron_schedule"]
    return spec


def change(row: dict, changes: dict, tz=None, now: Optional[datetime] = None) -> dict:
    """Apply a change to a job. Returns the updated row's plain description.
    Raises ValueError (plain words) when the change doesn't make sense."""
    from core import checks, schedule_job, scheduler
    tz = tz or scheduler.local_tz()
    changes = {k: v for k, v in (changes or {}).items() if v not in (None, "")}
    spec = _spec_from(row, tz)
    custom = spec.pop("custom_cron", None)
    timing = {k: changes[k] for k in WHEN_FIELDS if k in changes}
    if timing:
        if "in_minutes" in timing:
            for k in ("date", "time"):
                spec.pop(k, None)
            spec["repeat"] = "once"
        if timing.get("repeat") and timing["repeat"] != spec.get("repeat"):
            for k in ("date", "weekday", "day_of_month"):
                spec.pop(k, None)
        spec.update(timing)
        custom = None
    for k in ("what", "message") + CHECK_FIELDS + ("then", "instructions"):
        if k in changes:
            spec[k] = changes[k]
    if row.get("kind") == "do" and spec.get("type") == "remind" and "what" in changes and "message" not in changes:
        spec["message"] = "Reminder: " + str(changes["what"])
    if not timing and (custom or spec.get("repeat") in (None, "once")):
        # Nothing about when, for a one-off or a custom schedule: keep it exactly as it is.
        keep = {"cron": row["cron_schedule"], "next_run": row["next_run"]}
        spec = {**spec, "repeat": "daily", "time": "09:00"}  # just so build can check the rest
    else:
        keep = None
    job = schedule_job.build(spec, tz=tz, now=now)
    if job["kind"] == "check" and any(k in changes for k in CHECK_FIELDS):
        checks.validate(job["action"])
    old_action = json.loads(row.get("action") or "{}") if row.get("action") else {}
    if job["kind"] == "check" and not any(k in changes for k in CHECK_FIELDS) and old_action.get("state"):
        job["action"]["state"] = old_action["state"]  # same check: keep what it has seen
    if keep:
        cron, next_run = keep["cron"], keep["next_run"]
    elif job["run_at"] is not None:
        cron, next_run = "", scheduler._iso_utc(job["run_at"].astimezone(timezone.utc))
    else:
        cron = job["cron"]
        found = scheduler._cron_next(cron, after=now, tz=tz)
        next_run = scheduler._iso_utc(found) if found else None
    conn = scheduler._tasks_conn()
    try:
        conn.execute(
            "UPDATE tasks SET title = ?, cron_schedule = ?, next_run = ?, action = ?, fail_count = 0, "
            "status = CASE WHEN status = 'paused' THEN 'paused' ELSE 'pending' END WHERE id = ?",
            (job["title"], cron, next_run, json.dumps(job["action"]) if job["action"] else row.get("action"),
             row["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return _say(_job(row["id"], row["person"]))


def set_paused(job_id: int, paused: bool) -> Optional[str]:
    """Pause or resume. Resuming works out its next time again. Returns the
    new status, or None if there's no such job."""
    from core import people, scheduler
    conn = scheduler._tasks_conn()
    try:
        scheduler._ensure_deliver_column(conn)
        people.ensure_person_column(conn, "tasks")
        # Only the current person's own job; anyone else's is "no such job".
        row = conn.execute("SELECT cron_schedule, next_run FROM tasks WHERE id = ? AND person = ?",
                           (job_id, people.current())).fetchone()
        if row is None:
            return None
        if paused:
            conn.execute("UPDATE tasks SET status = 'paused' WHERE id = ?", (job_id,))
        else:
            nxt = row["next_run"]
            if row["cron_schedule"]:
                found = scheduler._cron_next(row["cron_schedule"], tz=scheduler.local_tz())
                nxt = scheduler._iso_utc(found) if found else None
            conn.execute("UPDATE tasks SET status = 'pending', fail_count = 0, next_run = ? WHERE id = ?",
                         (nxt, job_id))
        conn.commit()
    finally:
        conn.close()
    return "paused" if paused else "pending"


def _delete(job_id: int) -> None:
    from core import people, scheduler
    conn = scheduler._tasks_conn()
    try:
        conn.execute("DELETE FROM tasks WHERE id = ? AND person = ?", (job_id, people.current()))
        conn.commit()
    finally:
        conn.close()


# ── The tool ──────────────────────────────────────────────────────────

def run_tool(args: dict, source: str = "", caller_id: str = "", command: str = "") -> str:
    """The model's manage_jobs tool. Returns JSON for the model."""
    from core import people
    args = args if isinstance(args, dict) else {}
    action = str(args.get("action") or "list").strip().lower()
    person = people.current()
    if action == "list":
        jobs = [_say(r) for r in _rows(person)]
        return json.dumps({"jobs": jobs} if jobs else {"jobs": [], "note": "The person has no scheduled jobs."})
    if action not in ACTIONS:
        return json.dumps({"problem": "Choose list, pause, resume, change or delete."})
    row = _job(args.get("job_id"), person)
    if row is None:
        # The list comes with it, saving a call (earlier lists aren't kept between messages).
        return json.dumps({"problem": "There's no job with that id among the person's jobs. Here they are:",
                           "jobs": [_say(r) for r in _rows(person)]})
    if action in ("pause", "resume"):
        set_paused(row["id"], action == "pause")
        return json.dumps({"done": True, "job": _say(_job(row["id"], person))})
    if action == "change":
        try:
            return json.dumps({"done": True, "job": change(row, args)})
        except Exception as e:  # ValueError or checks.CheckError: plain words
            if not isinstance(e, ValueError) and type(e).__name__ != "CheckError":
                raise
            return json.dumps({"done": False, "problem": str(e)})
    # delete: only with a yes from the person in a later message
    key = (person, source or "", caller_id or "")
    waiting = _PENDING.get(key)
    if waiting and waiting["id"] == row["id"] and waiting["asked"] != command:
        _PENDING.pop(key, None)
        _delete(row["id"])
        return json.dumps({"done": True, "deleted": row["title"]})
    _PENDING[key] = {"id": row["id"], "asked": command}
    job = _say(row)
    return json.dumps({
        "done": False,
        "ask_the_person": (f"Delete \"{job['what']}\" ({job['when']})? It can't be undone. "
                           "If you only want it to stop for now, I can pause it instead."),
        "then": "If they say yes in their next message, call delete again with the same job_id.",
    })

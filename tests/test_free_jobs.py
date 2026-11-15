"""Free scheduled jobs, one-off times and plain choices (build step 2 of
docs/design/scheduled-jobs-and-team-members.md).

Reminders and the to-do list run in code, with no model, so they cost
nothing however often they run. Times are the person's own.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from core import schedule_job

SAST = ZoneInfo("Africa/Johannesburg")
# Sunday 15 November 2026, 13:00 in South Africa.
NOW = datetime(2026, 11, 15, 11, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    import core.database as database
    from core import scheduler
    path = tmp_path / "agent.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    monkeypatch.setattr(scheduler, "VAULT_DIR", tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"timezone": "Africa/Johannesburg"}))
    database.init_db()
    return path


@pytest.fixture
def no_model(monkeypatch):
    """No AI model set up, and a loud failure if anything asks one."""
    async def must_not_run(*a, **k):
        raise AssertionError("a free job asked the AI model")
    monkeypatch.setattr("core.agent.run_agent", must_not_run)
    monkeypatch.setattr("core.scheduler._llm_ready", lambda: False)


def _rows(db, sql="SELECT * FROM tasks"):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


# ── Plain choices to a schedule ───────────────────────────────────────

def test_tomorrow_at_nine_is_nine_where_the_person_is():
    job = schedule_job.build({"type": "remind", "what": "Phone the dentist", "date": "tomorrow", "time": "09:00"},
                             tz=SAST, now=NOW)
    assert job["run_at"] == datetime(2026, 11, 16, 9, 0, tzinfo=SAST)
    assert job["kind"] == "do" and job["cron"] == ""
    assert job["action"] == {"type": "remind", "text": "Phone the dentist"}


def test_in_minutes_and_weekday_names():
    job = schedule_job.build({"type": "remind", "what": "Tea", "in_minutes": 20}, tz=SAST, now=NOW)
    assert job["run_at"] == NOW.astimezone(SAST).replace(minute=20)
    # It's Sunday 13:00: "Sunday at 9" means next Sunday, "Monday" means tomorrow.
    sunday = schedule_job.build({"what": "x", "date": "sunday", "time": "9:00"}, tz=SAST, now=NOW)
    assert sunday["run_at"].day == 22
    monday = schedule_job.build({"what": "x", "date": "Monday", "time": "5pm"}, tz=SAST, now=NOW)
    assert (monday["run_at"].day, monday["run_at"].hour) == (16, 17)


def test_repeats_become_cron():
    def cron(**spec):
        return schedule_job.build({"what": "x", "time": "07:30", **spec}, tz=SAST, now=NOW)["cron"]
    assert cron(repeat="daily") == "30 7 * * *"
    assert cron(repeat="weekdays") == "30 7 * * 1-5"
    assert cron(repeat="weekly", weekday="sunday") == "30 7 * * 0"
    assert cron(repeat="weekly", weekday="monday") == "30 7 * * 1"
    assert cron(repeat="monthly", day_of_month=1) == "30 7 1 * *"


@pytest.mark.parametrize("spec, words", [
    ({"date": "today", "time": "08:00"}, "already passed"),
    ({"time": "25:00"}, "doesn't exist"),
    ({"time": "soon"}, "hours and minutes"),
    ({"date": "next blursday"}, "today, tomorrow"),
    ({"repeat": "monthly", "day_of_month": 31}, "1 to 28"),
    ({"repeat": "weekly"}, "day of the week"),
    ({"repeat": "fortnightly"}, "Choose once"),
])
def test_mistakes_get_plain_answers(spec, words):
    with pytest.raises(ValueError, match=words):
        schedule_job.build({"what": "x", **spec}, tz=SAST, now=NOW)


def test_descriptions_are_plain_words():
    d = schedule_job.describe
    assert d("0 9 * * *", None) == "Every day at 09:00"
    assert d("30 7 * * 1-5", None) == "Every weekday at 07:30"
    assert d("0 8 * * 1", None) == "Every Monday at 08:00"
    assert d("0 8 * * 0", None) == "Every Sunday at 08:00"
    assert d("0 8 2 * *", None) == "Every month on the 2nd at 08:00"
    assert d("*/5 * * * *", None) == "On a custom schedule (*/5 * * * *)"
    assert d("", "2026-11-16T07:00:00+00:00", tz=SAST) == "Once, Monday 16 November at 09:00"
    assert d("", None, "todo") == "On your to-do list (no time set)"


def test_cron_is_read_in_the_persons_time_zone():
    from core.scheduler import _cron_next
    nxt = _cron_next("0 9 * * *", after=NOW, tz=SAST)
    assert nxt == datetime(2026, 11, 16, 7, 0, tzinfo=timezone.utc)
    assert _cron_next("0 9 * * *", after=NOW) == datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


# ── Free jobs run in code ─────────────────────────────────────────────

def test_a_reminder_runs_with_no_model_at_all(db, no_model):
    from core import notices
    from core.scheduler import create_task, run_due_tasks
    create_task("Phone the dentist", "", kind="do", action={"type": "remind", "text": "phone the dentist"})
    out = run_due_tasks()
    assert out["executed"] == 1
    assert [n["text"] for n in notices.unseen()] == ["Reminder: phone the dentist"]
    assert _rows(db)[0]["status"] == "done"


def test_the_to_do_list_is_sent_in_code(db, no_model):
    from core import notices
    from core.scheduler import create_task, run_due_tasks
    create_task("Buy milk", "", status="todo")
    create_task("Fix the gate", "", status="todo")
    create_task("Send me my to-do list", "", kind="do", action={"type": "todos"})
    run_due_tasks()
    assert notices.unseen()[0]["text"] == "Your to-do list:\n- Buy milk\n- Fix the gate"


def test_think_jobs_wait_for_a_model_while_free_ones_run(db, no_model):
    from core.scheduler import create_task, run_due_tasks
    think = create_task("Summarise the news", "")
    run_due_tasks()
    assert [r["status"] for r in _rows(db, f"SELECT status FROM tasks WHERE id = {think['id']}")] == ["pending"]


def test_a_one_off_waits_for_its_time(db, no_model):
    from core.scheduler import create_task, run_due_tasks
    created = create_task("Tea", "", kind="do", action={"type": "remind", "text": "tea"},
                          run_at=datetime(2026, 11, 15, 15, 0, tzinfo=SAST))
    assert created["recurring"] is False and created["next_run"] == "2026-11-15T13:00:00+00:00"
    assert run_due_tasks(now=datetime(2026, 11, 15, 12, 59, tzinfo=timezone.utc))["executed"] == 0
    assert run_due_tasks(now=datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc))["executed"] == 1
    assert _rows(db)[0]["status"] == "done"


def test_a_repeating_free_job_comes_round_again_in_local_time(db, no_model):
    from core.scheduler import create_task, run_due_tasks
    create_task("Stretch", "0 9 * * *", kind="do", action={"type": "remind", "text": "stretch"})
    run_due_tasks(now=datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc))
    row = _rows(db)[0]
    assert row["status"] == "pending"
    assert row["next_run"] == "2030-01-02T07:00:00+00:00"  # 09:00 in South Africa


# ── The model's tool ──────────────────────────────────────────────────

def test_the_tool_is_offered_only_for_times():
    assert schedule_job.wanted(["Remind me to phone the dentist tomorrow at 9"])
    assert schedule_job.wanted(["Every Monday send me my to-do list"])
    assert not schedule_job.wanted(["What's the capital of France?"])


def test_the_tool_saves_a_free_reminder_that_answers_on_telegram(db):
    out = json.loads(schedule_job.run_tool(
        {"type": "remind", "what": "Phone the dentist", "message": "Reminder: phone the dentist.",
         "repeat": "daily", "time": "09:00"}, source="telegram", caller_id="123"))
    assert out["saved"] and out["free"]
    assert out["summary"] == "Phone the dentist: Every day at 09:00. Free, no AI needed."
    row = _rows(db)[0]
    assert (row["kind"], json.loads(row["deliver_to"])) == ("do", {"channel": "telegram", "to": "123"})


def test_a_bad_request_goes_back_to_the_model_in_plain_words(db):
    out = json.loads(schedule_job.run_tool({"type": "remind", "what": "x", "time": "half past"}))
    assert out == {"saved": False, "problem": "Say the time as hours and minutes, for example 09:00 or 17:30."}
    assert _rows(db) == []


def _call(name, args):
    return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_the_agent_saves_a_job_and_tells_the_person(db, monkeypatch):
    from core import agent
    seen = []
    replies = iter([_call("schedule_job", {"type": "remind", "what": "Phone the dentist", "in_minutes": 30}),
                    {"content": "Done. I'll remind you in 30 minutes. It's free."}])

    async def fake_llm(messages, tools=None, config=None):
        seen.append([t["function"]["name"] for t in tools or []])
        return next(replies)
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    out = asyncio.run(agent.run_agent("Remind me to phone the dentist in 30 minutes", source="selftest"))
    assert "schedule_job" in seen[0]
    assert out["text"].startswith("Done.")
    assert _rows(db)[0]["kind"] == "do"


def test_a_running_job_cannot_make_more_jobs(db, monkeypatch):
    from core import agent
    from core.scheduler import _due_prompt
    seen = []

    async def fake_llm(messages, tools=None, config=None):
        seen.append([t["function"]["name"] for t in tools or []])
        return {"content": "Here's your summary."}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    asyncio.run(agent.run_agent(_due_prompt("Every Monday summarise the news"), source="selftest"))
    assert "schedule_job" not in seen[0]


# ── The web page's endpoints ──────────────────────────────────────────

@pytest.fixture
def client(db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr("server.get_or_create_api_key", lambda: "k")
    monkeypatch.setattr(server, "VAULT_SETTINGS_PATH", tmp_path / "settings.json")
    c = TestClient(server.app)
    c.headers.update({"X-API-Key": "k"})
    return c


def test_the_form_makes_a_free_job_and_lists_it_in_plain_words(client):
    r = client.post("/api/tasks", json={"type": "remind", "what": "Stretch", "repeat": "weekdays", "time": "10:00"})
    assert r.status_code == 200 and r.json()["description"] == "Every weekday at 10:00"
    listed = client.get("/api/tasks").json()
    assert listed[0]["description"] == "Every weekday at 10:00" and listed[0]["free"] is True


def test_the_form_explains_mistakes(client):
    r = client.post("/api/tasks", json={"type": "remind", "what": "x", "repeat": "monthly", "day_of_month": 30})
    assert r.status_code == 400 and "1 to 28" in r.json()["detail"]
    r = client.post("/api/tasks", json={"title": "x", "cron_schedule": "not-a-cron"})
    assert r.status_code == 400


def test_pause_and_resume(client, db):
    task = client.post("/api/tasks", json={"type": "remind", "what": "Stretch", "repeat": "daily"}).json()
    assert client.post(f"/api/tasks/{task['id']}/pause", json={"paused": True}).json()["status"] == "paused"
    assert _rows(db)[0]["status"] == "paused"
    assert client.post(f"/api/tasks/{task['id']}/pause", json={"paused": False}).json()["status"] == "pending"
    assert _rows(db)[0]["next_run"]
    assert client.post("/api/tasks/999/pause", json={}).status_code == 404


def test_the_browser_sets_the_time_zone(client, tmp_path):
    assert client.post("/api/timezone", json={"timezone": "Europe/London"}).json() == {
        "timezone": "Europe/London", "changed": True}
    assert json.loads((tmp_path / "settings.json").read_text())["timezone"] == "Europe/London"
    assert client.post("/api/timezone", json={"timezone": "Not/AZone"}).status_code == 400


def test_old_reminders_are_offered_once_and_change_only_with_a_yes(client, db):
    from core.scheduler import create_task
    a = create_task("Remind me to water the plants", "0 18 * * *")
    b = create_task("Remind me to take my tablets", "0 8 * * *")
    create_task("Summarise the news", "0 7 * * *")
    offer = client.get("/api/tasks/tidy").json()
    assert [s["id"] for s in offer] == [a["id"], b["id"]]
    assert client.post("/api/tasks/tidy", json={"ids": [a["id"]]}).json() == {"changed": 1}
    kinds = {r["id"]: (r["kind"], r["action"]) for r in _rows(db)}
    assert kinds[a["id"]] == ("do", json.dumps({"type": "remind", "text": "Reminder: water the plants."}))
    assert kinds[b["id"]][0] == "think"  # not ticked, so not changed
    assert client.get("/api/tasks/tidy").json() == []  # asked once

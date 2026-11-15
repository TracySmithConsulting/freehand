"""Stopping and changing scheduled jobs from the chat (core/manage_jobs.py).

People on Telegram, Slack or WhatsApp can't reach the Scheduled jobs
screen, so they can list, pause, resume, change and delete jobs by asking.
Deleting takes a yes in a later message.
"""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core import agent, manage_jobs, people, scheduler

SAST = ZoneInfo("Africa/Johannesburg")


@pytest.fixture(autouse=True)
def sast(monkeypatch):
    monkeypatch.setattr(scheduler, "local_tz", lambda: SAST)
    manage_jobs._PENDING.clear()


def tool(args, command="the message", source="telegram", caller="123"):
    return json.loads(manage_jobs.run_tool(args, source, caller, command))


# Years ahead, so the tests keep working as time passes.
def _reminder(when=datetime(2030, 11, 18, 9, 0, tzinfo=SAST)):
    return scheduler.create_task("Phone the dentist", "", kind="do",
                                 action={"type": "remind", "text": "Reminder: phone the dentist."}, run_at=when)


def test_list_shows_the_persons_jobs_in_plain_words():
    _reminder()
    scheduler.create_task("Stretch", "30 10 * * 1-5", kind="do", action={"type": "remind", "text": "Stretch"})
    jobs = tool({"action": "list"})["jobs"]
    assert [(j["what"], j["when"], j["cost"]) for j in jobs] == [
        ("Phone the dentist", "Once, Monday 18 November at 09:00", "Free."),
        ("Stretch", "Every weekday at 10:30", "Free."),
    ]


def test_only_the_persons_own_jobs():
    token = people.set_current("sam")
    try:
        sams = _reminder()
    finally:
        people.reset(token)
    assert tool({"action": "list"})["jobs"] == []
    wrong = tool({"action": "pause", "job_id": sams["id"]})
    assert "no job with that id" in wrong["problem"] and wrong["jobs"] == []  # not Sam's


def test_a_wrong_id_comes_back_with_the_list():
    job = _reminder()
    wrong = tool({"action": "pause", "job_id": 999})
    assert [j["id"] for j in wrong["jobs"]] == [job["id"]]


def test_pause_and_resume():
    job = _reminder()
    assert tool({"action": "pause", "job_id": job["id"]})["job"]["paused"] is True
    assert tool({"action": "resume", "job_id": job["id"]})["job"]["paused"] is False


def test_moving_a_one_off_keeps_its_day():
    job = _reminder()
    out = tool({"action": "change", "job_id": job["id"], "time": "10:30"})
    assert out["job"]["when"] == "Once, Monday 18 November at 10:30"


def test_moving_a_repeat_keeps_its_days():
    job = scheduler.create_task("Stretch", "30 10 * * 1-5", kind="do", action={"type": "remind", "text": "Stretch"})
    assert tool({"action": "change", "job_id": job["id"], "time": "15:00"})["job"]["when"] == "Every weekday at 15:00"
    assert tool({"action": "change", "job_id": job["id"], "repeat": "weekly", "weekday": "friday"})["job"]["when"] \
        == "Every Friday at 15:00"


def test_changing_the_words_keeps_the_time():
    job = _reminder()
    out = tool({"action": "change", "job_id": job["id"], "message": "Reminder: phone Dr Naidoo."})
    assert out["job"]["when"] == "Once, Monday 18 November at 09:00"
    row = manage_jobs._job(job["id"])
    assert json.loads(row["action"])["text"] == "Reminder: phone Dr Naidoo."


def test_a_check_keeps_what_it_has_seen_unless_what_it_looks_for_changes(monkeypatch):
    monkeypatch.setattr("core.checks.email_accounts", lambda: [("google", "Personal")])
    job = scheduler.create_task("Bank", "0 * * * *", kind="check", action={
        "type": "check", "check": "email", "sender": "fnb", "then": "tell", "state": {"seen": ["1", "2"]}})
    tool({"action": "change", "job_id": job["id"], "repeat": "daily", "time": "08:00"})
    assert json.loads(manage_jobs._job(job["id"])["action"])["state"] == {"seen": ["1", "2"]}
    tool({"action": "change", "job_id": job["id"], "sender": "absa"})
    action = json.loads(manage_jobs._job(job["id"])["action"])
    assert action["sender"] == "absa" and "state" not in action  # a new check starts fresh


def test_a_change_that_makes_no_sense_says_so():
    job = _reminder()
    out = tool({"action": "change", "job_id": job["id"], "repeat": "monthly", "day_of_month": 31})
    assert out == {"done": False, "problem": "Choose a day from 1 to 28, so it happens every month."}


def test_delete_needs_a_yes_in_a_later_message():
    job = _reminder()
    first = tool({"action": "delete", "job_id": job["id"]}, command="Cancel my dentist reminder")
    assert first["done"] is False and "can't be undone" in first["ask_the_person"]
    # The model can't confirm by itself in the same message...
    again = tool({"action": "delete", "job_id": job["id"]}, command="Cancel my dentist reminder")
    assert again["done"] is False and manage_jobs._job(job["id"])
    # ...only the person's next message can.
    done = tool({"action": "delete", "job_id": job["id"]}, command="Yes")
    assert done == {"done": True, "deleted": "Phone the dentist"}
    assert manage_jobs._job(job["id"]) is None


def test_a_yes_only_counts_for_the_job_it_was_asked_about():
    a, b = _reminder(), _reminder()
    tool({"action": "delete", "job_id": a["id"]}, command="cancel it")
    assert tool({"action": "delete", "job_id": b["id"]}, command="yes")["done"] is False
    assert manage_jobs._job(a["id"]) and manage_jobs._job(b["id"])


def test_the_tool_is_offered_only_when_it_can_help():
    assert not manage_jobs.wanted(["Cancel my dentist reminder"])  # no jobs yet
    _reminder()
    assert manage_jobs.wanted(["Cancel my dentist reminder"])
    assert manage_jobs.wanted(["What jobs do I have?"])
    assert not manage_jobs.wanted(["What's the capital of France?"])


def _call(name, args):
    return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_on_telegram_a_reminder_is_moved_by_asking(monkeypatch):
    job = _reminder()
    replies = iter([_call("manage_jobs", {"action": "list"}),
                    _call("manage_jobs", {"action": "change", "job_id": job["id"], "time": "11:00"}),
                    {"content": "Done: I'll remind you on Monday at 11:00 instead."}])
    seen = []

    async def fake_llm(messages, tools=None, config=None):
        seen.append([t["function"]["name"] for t in tools or []])
        return next(replies)
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    out = asyncio.run(agent.run_agent("Move my dentist reminder to 11", source="selftest"))
    assert "manage_jobs" in seen[0]
    assert out["text"].startswith("Done")
    assert manage_jobs._say(manage_jobs._job(job["id"]))["when"] == "Once, Monday 18 November at 11:00"


def test_the_screens_pause_button_still_works():
    from fastapi.testclient import TestClient
    import server
    job = _reminder()
    c = TestClient(server.app)
    key = {"X-API-Key": server.get_or_create_api_key()}
    assert c.post(f"/api/tasks/{job['id']}/pause", json={"paused": True}, headers=key).json()["status"] == "paused"
    assert c.post("/api/tasks/999/pause", json={}, headers=key).status_code == 404

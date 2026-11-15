"""Scheduled tasks: results reach the person, and a task with no time runs
once (docs/design/scheduled-jobs-and-team-members.md, build step 1).

Before Nov 2026 a task's result went into a conversation nobody saw, and a
task with no time became "0 2 * * *": every night at 2am, forever.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    import core.database as database
    path = tmp_path / "agent.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    return path


@pytest.fixture
def fake_agent(monkeypatch):
    calls = []
    reply = {"text": "Reminder: phone the dentist.", "tools_used": [], "error": None}

    async def run_agent(command, max_turns=5, source="", caller_id=""):
        calls.append(command)
        return dict(reply)

    monkeypatch.setattr("core.agent.run_agent", run_agent)
    monkeypatch.setattr("core.scheduler._llm_ready", lambda: True)
    return calls, reply


def _row(db, task_id):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    finally:
        conn.close()


def test_a_task_with_no_time_runs_once_not_every_night_at_2am(db, fake_agent):
    from core.scheduler import create_task, run_due_tasks
    created = create_task("Phone the dentist", "")
    assert created["recurring"] is False
    assert created["cron_schedule"] != "0 2 * * *"
    assert run_due_tasks()["executed"] == 1
    assert _row(db, created["id"])["status"] == "done"
    assert run_due_tasks()["executed"] == 0


def test_the_result_is_kept_for_the_web_page(db, fake_agent):
    from core import notices
    from core.scheduler import create_task, run_due_tasks
    create_task("Phone the dentist", "", deliver_to={"channel": "web"})
    run_due_tasks()
    waiting = notices.unseen()
    assert [n["text"] for n in waiting] == ["Reminder: phone the dentist."]
    assert waiting[0]["title"] == "Phone the dentist"
    assert notices.mark_seen([waiting[0]["id"]]) == 1
    assert notices.unseen() == []


def test_the_agent_is_told_the_task_is_due_now(db, fake_agent):
    from core.scheduler import create_task, run_due_tasks
    calls, _ = fake_agent
    create_task("Phone the dentist", "")
    run_due_tasks()
    assert "Phone the dentist" in calls[0] and "Do it now" in calls[0]


def test_a_task_asked_for_on_telegram_is_answered_on_telegram_too(db, fake_agent, monkeypatch):
    from core import gateway, notices
    from core.scheduler import create_task, run_due_tasks
    sent = []

    async def send(chat_id, text):
        sent.append((chat_id, text))
        return True

    monkeypatch.setattr(gateway, "_telegram_send_message", send)
    create_task("Phone the dentist", "", deliver_to={"channel": "telegram", "to": "123"})
    run_due_tasks()
    assert sent == [("123", "Reminder: phone the dentist.")]
    assert len(notices.unseen()) == 1  # and in FreeHand's chat


def test_a_failed_send_doesnt_fail_the_task(db, fake_agent, monkeypatch):
    from core import gateway
    from core.scheduler import create_task, run_due_tasks

    async def broken(chat_id, text):
        raise RuntimeError("no network")

    monkeypatch.setattr(gateway, "_telegram_send_message", broken)
    created = create_task("Phone the dentist", "", deliver_to={"channel": "telegram", "to": "123"})
    assert run_due_tasks()["executed"] == 1
    assert _row(db, created["id"])["status"] == "done"


def test_a_failed_task_says_so_in_plain_words(db, fake_agent):
    from core import notices
    from core.scheduler import create_task, run_due_tasks
    _, reply = fake_agent
    reply.update(text="", error="model service timed out")
    created = create_task("Summarise the news", "")
    # A failed one-off task is due again at once: no message each time...
    assert run_due_tasks()["failed"] == 1
    assert run_due_tasks()["failed"] == 1
    assert notices.unseen() == []
    # ...but after three failures in a row it stops, and says so once.
    assert run_due_tasks()["failed"] == 1
    assert _row(db, created["id"])["status"] == "paused"
    text = notices.unseen()[0]["text"]
    assert "couldn't do your scheduled task" in text and "timed out" not in text
    assert run_due_tasks()["failed"] == 0
    assert len(notices.unseen()) == 1


def test_a_success_resets_the_failure_count(db, fake_agent):
    from core.scheduler import create_task, run_due_tasks
    _, reply = fake_agent
    created = create_task("Daily digest", "* * * * *")
    reply.update(error="timeout")
    run_due_tasks(now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert _row(db, created["id"])["fail_count"] == 1
    reply.update(error=None)
    run_due_tasks(now=datetime(2030, 1, 2, tzinfo=timezone.utc))
    assert _row(db, created["id"])["fail_count"] == 0


def test_notices_belong_to_the_person_whose_task_it_was(db, fake_agent):
    from core import notices
    from core.scheduler import create_task, run_due_tasks
    from core import people
    token = people.set_current("sam")
    try:
        create_task("Sam's reminder", "")
    finally:
        people.reset(token)
    run_due_tasks()
    assert notices.unseen() == []  # not the owner's
    assert len(notices.unseen(person="sam")) == 1


def test_a_scribbled_task_with_no_time_is_a_to_do_that_never_runs(db, fake_agent, tmp_path, monkeypatch):
    from core import scheduler
    monkeypatch.setattr(scheduler, "SCRIBBLE_PATH", tmp_path / "00_Scribble.md")
    monkeypatch.setattr(scheduler, "SWEEP_STATE_PATH", tmp_path / ".sweep_state.json")
    monkeypatch.setattr(scheduler, "VAULT_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "THREADS_DIR", tmp_path / "Threads")
    monkeypatch.setattr(scheduler, "_classify_entry",
                        lambda text: {"classification": "task", "topic": "", "cron": ""})
    scheduler.append_scribble("todo buy milk")
    assert scheduler.process_scribble()["tasks_created"] == 1
    calls, _ = fake_agent
    assert scheduler.run_due_tasks()["executed"] == 0
    assert calls == []
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT status, cron_schedule FROM tasks").fetchone() == ("todo", "")
    conn.close()


def test_gateway_task_with_no_time_replies_where_it_was_asked(db):
    from core.gateway import _route_to_agent
    reply = asyncio.run(_route_to_agent("/task phone the dentist", "telegram", "123"))
    assert "once" in reply and "here" in reply
    conn = sqlite3.connect(db)
    cron, target = conn.execute("SELECT cron_schedule, deliver_to FROM tasks").fetchone()
    conn.close()
    assert cron == ""
    assert json.loads(target) == {"channel": "telegram", "to": "123"}


def test_notices_endpoints(db, monkeypatch):
    from fastapi.testclient import TestClient
    import server
    from core import notices
    monkeypatch.setattr("server.get_or_create_api_key", lambda: "k")
    notices.add("Reminder: phone the dentist.", "Phone the dentist")
    client = TestClient(server.app)
    got = client.get("/api/notices", headers={"X-API-Key": "k"}).json()
    assert [n["text"] for n in got] == ["Reminder: phone the dentist."]
    r = client.post("/api/notices/seen", headers={"X-API-Key": "k"}, json={"ids": [got[0]["id"]]})
    assert r.json() == {"marked": 1}
    assert client.get("/api/notices", headers={"X-API-Key": "k"}).json() == []
    assert client.post("/api/notices/seen", headers={"X-API-Key": "k"}, json={}).status_code == 400

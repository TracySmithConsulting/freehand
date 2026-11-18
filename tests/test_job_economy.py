"""Scheduled jobs that use AI are economical (build step 4 of
docs/design/scheduled-jobs-and-team-members.md): short instructions, no
chat history, only the tools their words call for, a step limit, and what
they used counted per month.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from core import agent, schedule_job
from core.agent_config import build_job_prompt, build_system_prompt


def test_a_job_gets_a_fraction_of_freehands_instructions():
    job, full = build_job_prompt(), build_system_prompt()
    assert len(job) * 4 < len(full)  # in real use with accounts and skills, more like a tenth
    assert "nobody is there to answer" in job and "never instructions" in job
    assert "Now: " in job  # so "today's news" means today


@pytest.fixture
def seen(monkeypatch):
    calls = []

    async def fake_llm(messages, tools=None, config=None):
        calls.append({"messages": messages, "tools": [t["function"]["name"] for t in tools or []]})
        return {"content": "Here's your summary.", "_usage": {"prompt_tokens": 300, "completion_tokens": 50}}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    remembered = []
    monkeypatch.setattr(agent, "_remember_turn", lambda *a: remembered.append(a))
    monkeypatch.setattr(agent.conversation, "history",
                        lambda key: [{"role": "user", "content": "an old chat"}, {"role": "assistant", "content": "ok"}])
    return calls, remembered


def test_a_job_run_is_small_and_not_kept_in_the_chat(seen):
    calls, remembered = seen
    out = asyncio.run(agent.run_agent("Summarise this week's news about load shedding", source="cli", job=True))
    assert out["text"] == "Here's your summary."
    messages = calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]  # no chat history
    assert messages[0]["content"].startswith("You are FreeHand, doing a scheduled job")
    assert "ask_user" not in calls[0]["tools"] and "schedule_job" not in calls[0]["tools"]
    assert remembered == []


def test_a_normal_chat_still_gets_everything(seen):
    calls, remembered = seen
    asyncio.run(agent.run_agent("Remind me every day at 9 to stretch", source="selftest"))
    messages = calls[0]["messages"]
    assert len(messages) == 4 and "ask_user" in calls[0]["tools"] and "schedule_job" in calls[0]["tools"]
    assert len(remembered) == 1


def test_a_job_only_gets_the_tools_its_words_call_for(seen):
    calls, _ = seen
    asyncio.run(agent.run_agent("Summarise my calendar for tomorrow", source="cli", job=True))
    tools = calls[0]["tools"]
    assert "list_calendar_events" in tools
    assert "post_to_facebook" not in tools and "read_docx" not in tools


# ── In the scheduler ──────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    import core.database as database
    path = tmp_path / "agent.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    monkeypatch.setattr("core.scheduler._llm_ready", lambda: True)
    return path


def _row(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM tasks").fetchone())
    finally:
        conn.close()


def test_the_scheduler_runs_think_jobs_small_with_a_step_limit(db, monkeypatch):
    from core import scheduler
    asked = {}

    async def fake_run_agent(command, max_turns=5, source="", caller_id="", job=False, person=None):
        asked.update(max_turns=max_turns, job=job)
        return {"text": "Summary.", "error": None, "usage": {"prompt_tokens": 1000, "completion_tokens": 200}}
    monkeypatch.setattr("core.agent.run_agent", fake_run_agent)
    scheduler.create_task("Summarise the news", "0 7 * * *")
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc))
    assert asked == {"max_turns": scheduler.JOB_STEPS, "job": True}
    from core import notices
    assert [n["text"] for n in notices.unseen()] == ["Summary."]  # counting the use didn't lose the message
    row = _row(db)
    assert (row["usage_month"], row["month_runs"], row["month_tokens"]) == ("2030-01", 1, 1200)
    scheduler.run_due_tasks(now=datetime(2030, 1, 2, 8, 0, tzinfo=timezone.utc))
    assert (_row(db)["month_runs"], _row(db)["month_tokens"]) == (2, 2400)
    scheduler.run_due_tasks(now=datetime(2030, 2, 1, 8, 0, tzinfo=timezone.utc))
    row = _row(db)
    assert (row["usage_month"], row["month_runs"], row["month_tokens"]) == ("2030-02", 1, 1200)  # a new month


def test_a_check_counts_only_when_it_wakes_the_model(db, monkeypatch):
    from core import checks, scheduler
    holder = {"text": "Coming soon"}

    async def fake_fetch(url):
        return holder["text"]

    async def fake_llm(messages, tools=None, config=None):
        return {"content": "They're on sale.", "_usage": {"prompt_tokens": 120, "completion_tokens": 30}}
    monkeypatch.setattr(checks, "fetch_page", fake_fetch)
    monkeypatch.setattr("core.agent.call_llm", fake_llm)
    scheduler.create_task("Tickets", "* * * * *", kind="check",
                          action={"type": "check", "check": "page", "url": "https://example.com", "then": "think"})
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc))
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 0, 1, tzinfo=timezone.utc))
    assert _row(db)["month_runs"] == 0  # nothing new: no AI
    holder["text"] = "On sale now"
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 0, 2, tzinfo=timezone.utc))
    row = _row(db)
    assert (row["month_runs"], row["month_tokens"]) == (1, 150)
    assert "last_usage" not in json.loads(row["action"])


def test_the_use_is_shown_in_plain_words():
    now = datetime(2030, 1, 15, tzinfo=timezone.utc)
    note = schedule_job.usage_note({"usage_month": "2030-01", "month_runs": 3, "month_tokens": 12500}, now=now)
    assert note == "This month: used AI 3 times, about 12,500 tokens."
    assert schedule_job.usage_note({"usage_month": "2029-12", "month_runs": 3}, now=now) == ""
    assert schedule_job.usage_note({}, now=now) == ""


# ── Limits on AI uses a month ─────────────────────────────────────────

@pytest.fixture
def counted(db, monkeypatch):
    runs = []

    async def fake_run_agent(command, max_turns=5, source="", caller_id="", job=False, person=None):
        runs.append(command)
        return {"text": "Summary.", "error": None, "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    monkeypatch.setattr("core.agent.run_agent", fake_run_agent)
    return runs


def test_a_job_stops_at_its_limit_says_so_once_and_starts_again_next_month(counted, db, monkeypatch):
    from core import notices, scheduler
    monkeypatch.setattr(scheduler, "limits", lambda: {"per_job": 2, "total": 300})
    scheduler.create_task("Summarise the news", "0 7 * * *")
    for day in (1, 2, 3, 4):
        scheduler.run_due_tasks(now=datetime(2030, 1, day, 8, 0, tzinfo=timezone.utc))
    assert len(counted) == 2
    texts = [n["text"] for n in notices.unseen()]
    assert texts[:2] == ["Summary.", "Summary."]
    assert len(texts) == 3 and "used AI 2 times this month" in texts[2] and "until 1 February" in texts[2]
    assert _row(db)["status"] == "pending"  # held, not paused or done
    scheduler.run_due_tasks(now=datetime(2030, 2, 1, 8, 0, tzinfo=timezone.utc))
    assert len(counted) == 3


def test_all_jobs_together_have_a_limit_too(counted, monkeypatch):
    from core import notices, scheduler
    monkeypatch.setattr(scheduler, "limits", lambda: {"per_job": 100, "total": 1})
    scheduler.create_task("Job one", "0 7 * * *")
    scheduler.create_task("Job two", "0 7 * * *")
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc))
    assert counted == [scheduler._due_prompt("Job one")]
    assert "all your scheduled jobs together" in notices.unseen()[-1]["text"]


def test_a_one_off_over_the_limit_waits(counted, db, monkeypatch):
    from core import scheduler
    monkeypatch.setattr(scheduler, "limits", lambda: {"per_job": 0, "total": 300})
    scheduler.create_task("Once", "")
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert counted == []
    assert _row(db)["status"] == "pending"  # it runs when the limit allows


def test_a_check_over_its_limit_still_tells_free(db, monkeypatch):
    from core import checks, scheduler
    holder = {"text": "Coming soon"}

    async def fake_fetch(url):
        return holder["text"]

    async def no_llm(*a, **k):
        raise AssertionError("over the limit, the model must not be asked")
    monkeypatch.setattr(checks, "fetch_page", fake_fetch)
    monkeypatch.setattr("core.agent.call_llm", no_llm)
    monkeypatch.setattr(scheduler, "limits", lambda: {"per_job": 0, "total": 300})
    scheduler.create_task("Tickets", "* * * * *", kind="check",
                          action={"type": "check", "check": "page", "url": "https://example.com", "then": "think"})
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc))
    holder["text"] = "On sale now"
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 0, 1, tzinfo=timezone.utc))
    from core import notices
    assert "On sale now" in notices.unseen()[0]["text"]


def test_limits_can_be_seen_and_changed(db, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import server
    from core import scheduler
    monkeypatch.setattr("server.get_or_create_api_key", lambda: "k")
    monkeypatch.setattr(server, "VAULT_SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(scheduler, "VAULT_DIR", tmp_path)
    c = TestClient(server.app)
    c.headers.update({"X-API-Key": "k"})
    assert c.get("/api/tasks/limits").json() == {"per_job": 100, "total": 300, "used": 0}
    assert c.post("/api/tasks/limits", json={"per_job": 20, "total": 50}).json() == {"per_job": 20, "total": 50}
    assert scheduler.limits() == {"per_job": 20, "total": 50}
    assert c.post("/api/tasks/limits", json={"per_job": "lots", "total": 50}).status_code == 400


def test_the_screen_shows_use_against_the_limit():
    now = datetime(2030, 1, 15, tzinfo=timezone.utc)
    note = schedule_job.usage_note({"usage_month": "2030-01", "month_runs": 3, "month_tokens": 900}, now=now, per_job=100)
    assert note == "This month: used AI 3 of 100 times, about 900 tokens."

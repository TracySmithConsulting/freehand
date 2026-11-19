"""A monthly limit on AI for each person (core/ai_use.py): the owner sets
it on the People page; over it, FreeHand answers without calling the AI."""
import asyncio
import contextlib
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from core import agent, agent_config, ai_use, people, scheduler, sessions
from server import app, get_or_create_api_key, SESSION_HEADER

NOV = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 2, 12, tzinfo=timezone.utc)


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


@pytest.fixture
def owner():
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", people=[{"id": "thandi", "name": "Thandi", "role": "member"}])
    agent_config._save_settings(s)
    return TestClient(app, headers={"X-API-Key": get_or_create_api_key()})


@pytest.fixture
def llm(monkeypatch):
    """A pretend AI: counts the calls, and each answer used it once."""
    calls = []

    async def fake_run_agent(command, max_turns, source, caller_id, job=False, member=None, delegated=False):
        calls.append((people.current(), command))
        return {"text": "ok", "tools_used": [], "error": None,
                "usage": {"calls": 1, "prompt_tokens": 10, "completion_tokens": 5}}
    monkeypatch.setattr(agent, "_run_agent", fake_run_agent)
    return calls


def ask(text, person):
    return asyncio.run(agent.run_agent(text, person=person))


def test_nobody_has_a_limit_until_the_owner_sets_one(owner, llm):
    assert ai_use.limit("owner") is None and ai_use.limit("thandi") is None
    for _ in range(3):
        assert ask("hi", "thandi")["text"] == "ok"
    assert ai_use.used("thandi")["runs"] == 3 and ai_use.used("thandi")["tokens"] == 45


def test_over_the_limit_freehand_says_so_without_calling_the_ai(owner, llm):
    owner.put("/api/people/thandi", json={"ai_per_month": 2})
    assert ask("one", "thandi")["text"] == "ok"
    assert ask("two", "thandi")["text"] == "ok"
    third = ask("three", "thandi")
    assert "used FreeHand's AI 2 times this month, which is the limit Tracy set" in third["text"]
    assert "Ask Tracy if you need more" in third["text"]
    assert [c for c in llm if c[0] == "thandi"] == [("thandi", "one"), ("thandi", "two")]
    assert ai_use.used("thandi")["runs"] == 2  # the refusal isn't counted


def test_the_owner_never_has_a_limit(owner, llm):
    s = agent_config._load_settings()
    s["owner_ai_per_month"] = 0
    agent_config._save_settings(s)
    assert ask("hi", "owner")["text"] == "ok"
    with pytest.raises(people.PeopleError):
        ai_use.set_limit("owner", 5)


def test_an_answer_without_ai_doesnt_count(owner, monkeypatch):
    async def no_ai(*a, **k):
        return {"text": "Your to-do list is empty.", "tools_used": [], "error": None}
    monkeypatch.setattr(agent, "_run_agent", no_ai)
    ask("what's on my list?", "thandi")
    assert ai_use.used("thandi")["runs"] == 0


def test_a_new_month_starts_again(owner):
    ai_use.set_limit("thandi", 1)
    ai_use.count({"calls": 1}, "thandi", now=NOV)
    assert ai_use.refusal("thandi", now=NOV)
    assert "starts again on 1 December" in ai_use.refusal("thandi", now=NOV)
    assert ai_use.refusal("thandi", now=DEC) is None


def test_the_people_page_shows_and_sets_it(owner):
    ai_use.count({"calls": 1}, "thandi")
    r = owner.put("/api/people/thandi", json={"ai_per_month": "200"})
    assert r.status_code == 200, r.text
    thandi = r.json()["people"][0]
    assert (thandi["ai_per_month"], thandi["ai_this_month"]) == (200, 1)
    # Empty: no limit again. Leaving it out of the request keeps it.
    owner.put("/api/people/thandi", json={"name": "Thandi M"})
    assert ai_use.limit("thandi") == 200
    owner.put("/api/people/thandi", json={"ai_per_month": ""})
    assert ai_use.limit("thandi") is None


@pytest.mark.parametrize("value", ["lots", -1, 100_001, 2.5])
def test_a_wrong_limit_is_explained_and_adds_nobody(owner, value):
    r = owner.post("/api/people", json={"name": "Sam", "ai_per_month": value})
    assert r.status_code == 400 and "whole number" in r.json()["detail"]
    assert [p["name"] for p in owner.get("/api/people").json()["people"]] == ["Thandi"]


def test_a_limit_can_be_set_when_adding_someone(owner):
    r = owner.post("/api/people", json={"name": "Sam", "ai_per_month": 50})
    assert r.status_code == 200 and r.json()["person"]["ai_per_month"] == 50


def test_a_member_sees_their_own_use_and_cant_change_it(owner):
    ai_use.set_limit("thandi", 10)
    ai_use.count({"calls": 1}, "thandi")
    thandi = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})
    me = thandi.get("/api/me").json()
    assert (me["ai_this_month"], me["ai_per_month"]) == (1, 10)
    assert thandi.put("/api/people/thandi", json={"ai_per_month": ""}).status_code == 403


def _job_row(person, runs=0):
    conn = scheduler._tasks_conn()
    conn.row_factory = sqlite3.Row
    scheduler._ensure_deliver_column(conn)
    people.ensure_person_column(conn, "tasks")
    conn.execute("INSERT INTO tasks (title, cron_schedule, status, person, usage_month, month_runs) "
                 "VALUES ('Summary', '0 8 * * 1', 'pending', ?, ?, ?)", (person, scheduler._month(NOV), runs))
    conn.commit()
    return conn, conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT 1").fetchone()


def test_a_members_scheduled_jobs_stop_at_their_limit(owner):
    ai_use.set_limit("thandi", 1)
    ai_use.count({"calls": 1}, "thandi", now=NOV)
    conn, row = _job_row("thandi")
    try:
        why = scheduler.ai_allowed(conn, row, NOV)
    finally:
        conn.close()
    assert why and "the limit Tracy set" in why


def test_one_persons_jobs_dont_use_up_everyone_elses(owner):
    s = agent_config._load_settings()
    s["job_limits"] = {"per_job": 100, "total": 5}
    agent_config._save_settings(s)
    conn, _ = _job_row("thandi", runs=5)
    try:
        _, owners_job = _job_row("owner")
        assert scheduler.ai_allowed(conn, owners_job, NOV) is None
        _, hers = _job_row("thandi")
        assert "all your scheduled jobs together" in scheduler.ai_allowed(conn, hers, NOV)
    finally:
        conn.close()


def test_when_someone_leaves_their_counts_go(owner, monkeypatch):
    from core import long_memory
    monkeypatch.setattr(long_memory, "enabled", lambda: False)
    ai_use.count({"calls": 1}, "thandi")
    owner.request("DELETE", "/api/people/thandi", json={})
    assert ai_use.used("thandi")["runs"] == 0

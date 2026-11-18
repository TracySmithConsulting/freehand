"""Separate users, before the separating (Nov 2026): the five things a
review found that would quietly undo it, each of which made a person's
work run as the owner. Everything here is done as Thandi, and must stay
Thandi's (docs/design/people.md, "Before separating")."""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

from core import agent_config, gateway, people, scheduler, security


THANDI = {"id": "thandi", "name": "Thandi", "telegram": ["555"], "slack": ["U555"]}


def save_settings(**values):
    s = agent_config._load_settings()
    s.update(values)
    agent_config._save_settings(s)


async def _who():
    return people.current()


# ── 1. Work moved to another thread stays the person's ───────────────

def test_work_sent_to_another_thread_is_still_the_persons():
    from core import mcp_server
    from core.tools import composio_registry
    hops = {
        "composio": composio_registry._run_async,
        "mcp": mcp_server._run_async_in_thread,
        "scheduler": scheduler._run_coro,
    }

    async def as_thandi():
        token = people.set_current("thandi")
        try:
            # Inside a running loop, so each one really does use a new thread.
            return {name: hop(_who()) for name, hop in hops.items()}
        finally:
            people.reset(token)

    assert asyncio.run(as_thandi()) == {"composio": "thandi", "mcp": "thandi", "scheduler": "thandi"}


def test_carry_runs_a_plain_thread_as_the_person():
    import threading
    seen = []
    token = people.set_current("thandi")
    try:
        t = threading.Thread(target=people.carry(lambda: seen.append(people.current())))
        t.start(); t.join()
    finally:
        people.reset(token)
    assert seen == ["thandi"]
    assert people.current() == "owner"


# ── 2. A scheduled job runs as the person who made it ────────────────

def _job_as(person, title, monkeypatch):
    seen = []

    async def fake_run_agent(command, max_turns=5, source="", caller_id="", job=False, person=None, **kw):
        seen.append({"person": person, "current": people.current()})
        return {"text": "Done.", "error": None}

    monkeypatch.setattr("core.agent.run_agent", fake_run_agent)
    monkeypatch.setattr("core.scheduler._llm_ready", lambda: True)
    token = people.set_current(person)
    try:
        task = scheduler.create_task(title, "")
    finally:
        people.reset(token)
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc))
    return seen, task


def test_a_scheduled_job_runs_as_the_person_who_made_it(monkeypatch):
    save_settings(people=[THANDI])
    seen, _ = _job_as("thandi", "Summarise my week", monkeypatch)
    assert seen == [{"person": "thandi", "current": "thandi"}]
    assert people.current() == "owner"  # and the scheduler puts things back


def test_a_job_of_someone_no_longer_here_doesnt_run_as_the_owner(monkeypatch):
    save_settings(people=[])  # Thandi has left
    seen, task = _job_as("thandi", "Read my email", monkeypatch)
    assert seen == []
    from core import database
    conn = sqlite3.connect(database.DB_PATH)
    last_run = conn.execute("SELECT last_run FROM tasks WHERE id = ?", (task["id"],)).fetchone()[0]
    conn.close()
    assert "person gone" in last_run


# ── 3. Roles: one owner, and an unknown role doesn't get in ──────────

def test_the_settings_file_cant_make_a_second_owner_or_a_typo_a_member():
    s = {"people": [
        {"id": "boss", "role": "owner"},
        {"id": "kid", "role": "chlid"},
        {"id": "thandi", "role": "Member"},
        {"id": "sam"},  # no role: a member, as before
    ]}
    assert [(p["id"], p["role"]) for p in people.list_people(s)] == [
        ("owner", "owner"), ("thandi", "member"), ("sam", "member")]
    assert people.person_for("telegram", "1", {"people": [{"id": "kid", "role": "chlid", "telegram": ["1"]}]}) is None


# ── 4. Telegram and Slack: the person is who wrote, not the chat ─────

def _telegram(monkeypatch):
    routed = []

    async def fake_route(text, source, caller_id):
        routed.append((caller_id, people.current()))
        return "ok"

    async def fake_send(chat_id, text):
        return True

    monkeypatch.setattr(gateway, "_route_to_agent", fake_route)
    monkeypatch.setattr(gateway, "_telegram_send_message", fake_send)
    # Updates already seen are dropped; these tests' numbers are their own.
    monkeypatch.setattr(gateway, "_seen_deliveries", {})
    return routed


def _update(n, chat, sender, kind="group"):
    async def go():
        await gateway.handle_telegram_update(
            {"update_id": 7000 + n, "message": {"chat": {"id": chat, "type": kind},
                                         "from": {"id": sender}, "text": "hello"}})
        await asyncio.gather(*list(gateway._background_tasks))
    asyncio.run(go())


def test_in_a_telegram_group_each_person_is_themselves(monkeypatch):
    routed = _telegram(monkeypatch)
    save_settings(telegram_allow_from=["42", "-100"], people=[THANDI])
    _update(1, -100, 555)  # Thandi in the group the owner allowed
    _update(2, -100, 42)   # the owner in it
    _update(3, -100, 777)  # someone FreeHand doesn't know
    assert routed == [("-100", "thandi"), ("-100", "owner")]


def test_a_group_cant_be_someones_personal_link(monkeypatch):
    routed = _telegram(monkeypatch)
    # Listed under Thandi, the group would make everyone in it Thandi.
    save_settings(telegram_allow_from=["42"], people=[{**THANDI, "telegram": ["555", "-200"]}])
    _update(4, -200, 555)
    _update(5, -200, 42)
    assert routed == []


def test_a_private_telegram_chat_works_as_before(monkeypatch):
    routed = _telegram(monkeypatch)
    save_settings(telegram_allow_from=["42"], people=[THANDI])
    _update(6, 555, 555, kind="private")
    _update(7, 42, 42, kind="private")
    assert routed == [("555", "thandi"), ("42", "owner")]


def test_a_slack_message_is_the_users_not_the_channels(monkeypatch):
    seen = []

    async def fake_route(text, source, caller_id):
        seen.append((caller_id, people.current()))
        return "ok"

    async def fake_send(channel, text):
        return True

    monkeypatch.setattr(gateway, "_route_to_agent", fake_route)
    save_settings(slack_allow_from=["U42"], people=[THANDI])
    asyncio.run(gateway._reply_in_background("slack", "C0CHANNEL", "hi", fake_send, sender="U555"))
    assert seen == [("C0CHANNEL", "thandi")]
    assert people.current() == "owner"


def test_a_chat_approval_is_only_the_requesters_own():
    row = {"status": "pending", "person": "thandi",
           "payload": json.dumps({"source": "telegram", "caller_id": "-100"})}
    assert security._remote_ownership_refusal(row, "telegram", "-100", "thandi") is None
    assert "your own requests" in security._remote_ownership_refusal(row, "telegram", "-100", "owner")
    # Without a person (older callers), the chat rule alone, as before.
    assert security._remote_ownership_refusal(row, "telegram", "-100") is None

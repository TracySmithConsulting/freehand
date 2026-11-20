"""Children on a family FreeHand (core/children.py): the hard limits.

Anything that reaches outside waits for a parent, and only a parent
approves it; quiet hours and a daily message limit; guidance for their
age; and the child is told honestly what their parents can see."""
import asyncio
import contextlib
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from core import agent, agent_config, children, notices, people, security, sessions
from core.security import PermissionTier, set_tier
from server import app, get_or_create_api_key, SESSION_HEADER

CHILD = {"name": "Lerato", "role": "child", "parents": ["owner", "sipho"], "age_band": "under_13",
         "quiet_from": "21:00", "quiet_to": "06:00", "daily_messages": 3, "consent": True}


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


@pytest.fixture
def family():
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", timezone="Africa/Johannesburg",
             people=[{"id": "sipho", "name": "Sipho", "role": "member"},
                     {"id": "aunt", "name": "Aunt Zodwa", "role": "member"}])
    agent_config._save_settings(s)
    owner = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
    r = owner.post("/api/people", json=CHILD)
    assert r.status_code == 200, r.text
    return owner


NOON = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)      # 12:00 in Johannesburg
LATE = datetime(2026, 11, 19, 20, 30, tzinfo=timezone.utc)     # 22:30


# ── Adding a child ───────────────────────────────────────────────────

def test_adding_a_child_needs_a_parents_consent(family):
    r = family.post("/api/people", json={**CHILD, "name": "Ayanda", "consent": False})
    assert r.status_code == 400 and "parent or guardian" in r.json()["detail"]
    assert people.get("ayanda") is None


@pytest.mark.parametrize("change, words", [
    ({"parents": ["lerato"]}, "A parent must be"),
    ({"parents": ["nobody"]}, "A parent must be"),
    ({"quiet_from": "21:00", "quiet_to": ""}, "two different times"),
    ({"quiet_from": "25:00", "quiet_to": "06:00"}, "two different times"),
    ({"daily_messages": 0}, "from 1 to 1000"),
    ({"age_band": "baby"}, "Choose the child's age"),
])
def test_mistakes_in_a_childs_limits_are_explained(family, change, words):
    r = family.post("/api/people", json={**CHILD, "name": "Ayanda", **change})
    assert r.status_code == 400 and words in r.json()["detail"]
    assert people.get("ayanda") is None


def test_the_people_page_shows_a_childs_limits(family):
    lerato = next(p for p in family.get("/api/people").json()["people"] if p["id"] == "lerato")
    assert lerato["role"] == "child"
    assert lerato["child"]["parents"] == ["owner", "sipho"]
    assert lerato["child"]["quiet_hours"] == {"from": "21:00", "to": "06:00"}
    assert lerato["child"]["daily_messages"] == 3


def test_a_child_can_become_a_member_again(family):
    r = family.put("/api/people/lerato", json={"role": "member"})
    assert r.status_code == 200, r.text
    assert people.get("lerato")["role"] == "member" and not children.is_child("lerato")


def test_changing_a_childs_limits_doesnt_ask_for_consent_again(family):
    r = family.put("/api/people/lerato", json={**CHILD, "consent": False, "daily_messages": 10})
    assert r.status_code == 200, r.text
    assert children.rules("lerato")["daily_messages"] == 10


# ── Quiet hours and the daily limit ──────────────────────────────────

def test_quiet_hours_cross_midnight(family):
    assert not children.in_quiet_hours("lerato", NOON)
    assert children.in_quiet_hours("lerato", LATE)
    assert children.in_quiet_hours("lerato", datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc))  # 05:00
    assert not children.in_quiet_hours("lerato", datetime(2026, 11, 20, 4, 30, tzinfo=timezone.utc))  # 06:30


def test_no_answers_in_quiet_hours_or_over_the_daily_limit(family, monkeypatch):
    calls = []

    async def fake(command, *a, **k):
        calls.append(command)
        return {"text": "ok", "tools_used": [], "error": None}
    monkeypatch.setattr(agent, "_run_agent", fake)
    monkeypatch.setattr(children, "in_quiet_hours", lambda child_id, now=None: False)
    for i in range(3):
        assert asyncio.run(agent.run_agent(f"q{i}", person="lerato"))["text"] == "ok"
    over = asyncio.run(agent.run_agent("one more", person="lerato"))["text"]
    assert "all 3 of today's messages" in over and "Tracy or Sipho" in over
    monkeypatch.setattr(children, "in_quiet_hours", lambda child_id, now=None: True)
    assert "quiet time" in asyncio.run(agent.run_agent("hi", person="lerato"))["text"]
    assert calls == ["q0", "q1", "q2"]
    # A grown-up has neither.
    assert asyncio.run(agent.run_agent("hi", person="sipho"))["text"] == "ok"


# ── Steps that reach outside wait for a parent ───────────────────────

@pytest.mark.parametrize("tool, needs", [
    ("send_email", True), ("post_to_facebook", True), ("navigate", True), ("oc_gmail_send", True),
    ("web_search", False), ("search_memory", False), ("write_docx", False),
])
def test_what_needs_a_parent(family, tool, needs):
    with as_person("lerato"):
        assert children.needs_parent(tool) is needs
    with as_person("sipho"):
        assert children.needs_parent(tool) is False


def _pending():
    conn = security._get_conn()
    rows = conn.execute("SELECT id, person, payload FROM approvals WHERE status = 'pending'").fetchall()
    conn.close()
    return rows


def test_a_childs_send_waits_for_a_parent_even_at_full_access(family, monkeypatch):
    with as_person("lerato"):
        approval = security.intercept_action(
            "oauth_access", "send an email", {"tool": "send_email", "needs_owner": True, "parents": ["owner", "sipho"]},
            force_confirm=True)
    assert approval["approval_id"]
    row = security._get_approval(approval["approval_id"])
    # Lerato can't approve it, but can cancel it.
    with as_person("lerato"):
        with pytest.raises(ValueError, match="Only a parent can approve this"):
            security.handle_approval(approval["approval_id"], "approve")
    # Her aunt isn't her parent: she doesn't even see it.
    with as_person("aunt"):
        assert security.get_pending_approvals() == []
        with pytest.raises(ValueError):
            security.handle_approval(approval["approval_id"], "approve")
    # Sipho, her parent, sees it and approves it.
    with as_person("sipho"):
        assert [a["id"] for a in security.get_pending_approvals()] == [approval["approval_id"]]
        assert security.handle_approval(approval["approval_id"], "approve")["status"] == "approved"
    assert row["person"] == "lerato"


def test_the_agent_loop_asks_a_parent_and_tells_them(family, monkeypatch):
    """A real run: Lerato's FreeHand tries to open a website, at Full access.
    (Sending from the family's mailbox is refused outright: it isn't hers.)"""
    set_tier("full_access")
    replies = [{"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "navigate", "arguments": json.dumps({"url": "https://example.com"})}}]}]
    ran = []

    async def fake_llm(messages, tools=None, config=None):
        reply = replies.pop(0) if replies else {"content": "I've asked a parent."}
        return {**reply, "_usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    async def fake_run_tool(name, args):
        ran.append(name)
        return {"content": "{}"}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    monkeypatch.setattr(agent, "_run_tool", fake_run_tool)
    monkeypatch.setattr(children, "in_quiet_hours", lambda child_id, now=None: False)
    asyncio.run(agent.run_agent("open example.com", person="lerato"))
    assert ran == []  # nothing was opened
    (row,) = _pending()
    payload = json.loads(row[2])
    assert row[1] == "lerato" and payload["parents"] == ["owner", "sipho"] and payload["needs_owner"] is True
    with as_person("sipho"):
        told = notices.unseen()
    assert told and "Lerato is waiting for your yes" in told[0]["title"]


# ── Guidance, and what the child is told ─────────────────────────────

def test_the_ai_gets_guidance_for_the_childs_age_only(family):
    with as_person("lerato"):
        g = children.guidance()
    assert "under 13" in g and "Don't just hand over the answers" in g and "116" in g
    with as_person("sipho"):
        assert children.guidance() == ""


def test_the_child_is_told_honestly_what_parents_see(family):
    lerato = TestClient(app, headers={SESSION_HEADER: sessions.start("lerato")})
    me = lerato.get("/api/me").json()
    assert me["child"] is True
    # Under 13, full view is suggested (tests/test_children_oversight.py has the others).
    assert me["what_parents_see"] == ("Tracy or Sipho can read your chats with FreeHand. They see and answer "
                                      "anything FreeHand would send, post or do outside for you, and FreeHand "
                                      "tells them if you seem to be in danger.")


def test_a_child_cant_change_their_own_limits(family):
    lerato = TestClient(app, headers={SESSION_HEADER: sessions.start("lerato")})
    assert lerato.put("/api/people/lerato", json={"role": "member"}).status_code == 403
    assert children.is_child("lerato")


def test_when_a_child_leaves_their_counts_go(family, monkeypatch):
    from core import long_memory
    monkeypatch.setattr(long_memory, "enabled", lambda: False)
    children.count_message("lerato")
    family.request("DELETE", "/api/people/lerato", json={})
    assert children.messages_today("lerato") == 0


def test_an_unknown_role_still_isnt_let_in(family):
    s = agent_config._load_settings()
    s["people"].append({"id": "kid", "name": "Kid", "role": "chlid"})
    agent_config._save_settings(s)
    assert people.get("kid") is None

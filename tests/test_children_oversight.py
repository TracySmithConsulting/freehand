"""Children, the parents' choices (Tracy, 20 November 2026; core/children.py):

- a daily AI allowance;
- a child's waiting request also goes to each parent's phone, and only
  that parent's own chat can answer it there;
- what parents see: the chats, a weekly summary (no words), or alerts only;
- when FreeHand is worried, a parent is told, unless a parent is the danger."""
import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from core import agent, agent_config, children, notices, people, security, sessions
from server import app, get_or_create_api_key, SESSION_HEADER

CHILD = {"name": "Lerato", "role": "child", "parents": ["owner", "sipho"], "age_band": "under_13",
         "daily_messages": 50, "consent": True}
NOW = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


@pytest.fixture
def phoned(monkeypatch):
    sent = []

    async def fake_send(channel, to, text):
        sent.append((channel, to, text))
        return True
    monkeypatch.setattr(notices, "_send", fake_send)
    return sent


@pytest.fixture
def family(phoned):
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", timezone="Africa/Johannesburg",
             people=[{"id": "sipho", "name": "Sipho", "role": "member", "telegram": ["555"]},
                     {"id": "aunt", "name": "Aunt Zodwa", "role": "member", "telegram": ["777"]}])
    agent_config._save_settings(s)
    owner = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
    r = owner.post("/api/people", json=CHILD)
    assert r.status_code == 200, r.text
    return owner


def _set(family, **change):
    r = family.put("/api/people/lerato", json={**CHILD, **change})
    assert r.status_code == 200, r.text


# ── The daily AI allowance ───────────────────────────────────────────

def test_the_ai_allowance_stops_a_child_for_the_day(family, monkeypatch):
    _set(family, daily_ai="small")
    assert children.rules("lerato")["daily_ai"] == children.ALLOWANCES["small"]
    monkeypatch.setattr(children, "in_quiet_hours", lambda child_id, now=None: False)
    calls = []

    async def fake(command, *a, **k):
        calls.append(command)
        return {"text": "ok", "tools_used": [], "error": None,
                "usage": {"prompt_tokens": 90_000, "completion_tokens": 20_000, "calls": 1}}
    monkeypatch.setattr(agent, "_run_agent", fake)
    assert asyncio.run(agent.run_agent("a long question", person="lerato"))["text"] == "ok"
    assert children.allowance_used("lerato") == 100
    over = asyncio.run(agent.run_agent("another", person="lerato"))
    assert "AI allowance" in over["text"] and "Tracy or Sipho" in over["text"]
    assert calls == ["a long question"]
    # Sipho has no allowance, and his use isn't counted as a child's.
    asyncio.run(agent.run_agent("hi", person="sipho"))
    assert children.ai_today("sipho") == 0


def test_no_allowance_is_the_default_and_a_wrong_one_is_explained(family):
    assert children.rules("lerato")["daily_ai"] is None
    assert children.allowance_used("lerato") is None
    r = family.put("/api/people/lerato", json={**CHILD, "daily_ai": "huge"})
    assert r.status_code == 400 and "AI allowance" in r.json()["detail"]
    _set(family, daily_ai="medium")
    _set(family, daily_ai="none")
    assert children.rules("lerato")["daily_ai"] is None


# ── What parents see ─────────────────────────────────────────────────

def test_the_suggestion_follows_age_until_the_parent_chooses(family):
    assert children.rules("lerato")["oversight"] == "full"
    _set(family, age_band="13_to_15")
    assert children.rules("lerato")["oversight"] == "summary"
    _set(family, age_band="13_to_15", oversight="alerts")
    assert children.rules("lerato")["oversight"] == "alerts"
    r = family.put("/api/people/lerato", json={**CHILD, "oversight": "everything"})
    assert r.status_code == 400 and "what their parents see" in r.json()["detail"]


def test_words_are_kept_only_in_full_view(family):
    done = {"text": "Photosynthesis is how plants make food.", "tools_used": [{"name": "web_search"}]}
    children.record("What is photosynthesis?", done, "lerato", NOW)
    (chat,) = children.activity("lerato", NOW)["chats"]
    assert chat["asked"] == "What is photosynthesis?" and "plants" in chat["answered"]

    _set(family, oversight="summary")  # the words kept so far go
    children.record("And respiration?", done, "lerato", NOW)
    seen = children.activity("lerato", NOW)
    assert "chats" not in seen
    assert "sent FreeHand 2 messages this week, on 1 day" in seen["summary"]
    assert "looking things up" in seen["summary"]
    conn = children._activity_conn()
    assert conn.execute("SELECT asked, answered FROM child_activity WHERE person='lerato'").fetchall() == [("", "")] * 2
    conn.close()

    _set(family, oversight="alerts")
    children.record("Anything", done, "lerato", NOW)
    seen = children.activity("lerato", NOW)
    assert "chats" not in seen and "summary" not in seen


def test_old_chats_are_forgotten_after_30_days(family):
    children.record("old", {"text": "x"}, "lerato", NOW - timedelta(days=31))
    children.record("new", {"text": "y"}, "lerato", NOW)
    assert [c["asked"] for c in children.activity("lerato", NOW)["chats"]] == ["new"]


def test_only_a_childs_parents_see_them(family):
    children.record("hello", {"text": "hi!"}, "lerato")
    assert [c["id"] for c in family.get("/api/children").json()["children"]] == ["lerato"]
    sipho = TestClient(app, headers={SESSION_HEADER: sessions.start("sipho")})
    (seen,) = sipho.get("/api/children").json()["children"]
    assert seen["chats"][0]["asked"] == "hello"
    for who in ("aunt", "lerato"):
        client = TestClient(app, headers={SESSION_HEADER: sessions.start(who)})
        assert client.get("/api/children").json()["children"] == []


def test_the_child_is_told_what_their_parents_chose(family):
    with as_person("lerato"):
        assert children.what_parents_see().startswith("Tracy or Sipho can read your chats with FreeHand.")
        _set(family, oversight="summary")
        assert "weekly summary" in children.what_parents_see() and "not your words" in children.what_parents_see()
        _set(family, oversight="alerts")
        assert "can't read your chats" in children.what_parents_see()
        assert "if you seem to be in danger" in children.what_parents_see()


def test_the_weekly_summary_goes_to_each_parent_once_a_week(family, phoned):
    _set(family, oversight="summary")
    children.record("q", {"text": "a"}, "lerato", NOW)
    assert asyncio.run(children.send_weekly_summaries(NOW)) == 0  # the first week starts now
    assert asyncio.run(children.send_weekly_summaries(NOW + timedelta(days=3))) == 0
    assert asyncio.run(children.send_weekly_summaries(NOW + timedelta(days=7, minutes=1))) == 1
    assert asyncio.run(children.send_weekly_summaries(NOW + timedelta(days=8))) == 0
    with as_person("sipho"):
        (note,) = notices.unseen()
    assert note["title"] == "Lerato's week with FreeHand"
    assert [p[:2] for p in phoned] == [("telegram", "555")]
    # In full view, no summary is sent (the parents read the chats).
    _set(family, oversight="full")
    assert asyncio.run(children.send_weekly_summaries(NOW + timedelta(days=30))) == 0


# ── Approving from a parent's phone ──────────────────────────────────

def _childs_request(tool="navigate"):
    with as_person("lerato"):
        held = security.intercept_action(
            "oauth_access", "open example.com",
            {"tool": tool, "args": {"url": "https://example.com"}, "source": "telegram", "caller_id": "999",
             "needs_owner": True, "parents": ["owner", "sipho"]}, "telegram", "999", force_confirm=True)
    return held["approval_id"]


def test_a_waiting_request_goes_to_the_parents_phone(family, phoned):
    approval = _childs_request()
    with as_person("lerato"):
        asyncio.run(children.ask_parents(approval, "open example.com"))
    (to_sipho,) = phoned  # Tracy has no chat linked here
    assert to_sipho[:2] == ("telegram", "555")
    assert f"/approve {approval}" in to_sipho[2] and "Lerato asked FreeHand to: open example.com" in to_sipho[2]
    with as_person("sipho"):
        assert "Lerato is waiting for your yes" in notices.unseen()[0]["title"]


def test_a_parent_approves_from_their_own_chat_only(family, monkeypatch):
    ran = []

    async def fake_execute(name, args):
        ran.append((name, people.current()))
        return {"content": "opened"}
    monkeypatch.setattr(agent, "execute_tool", fake_execute)
    approval = _childs_request()

    def approve(source, chat, who):
        with as_person(who):
            return asyncio.run(security.approve_from_remote(approval, source, chat, who))
    # Not her aunt, not Sipho from someone else's chat, and not Lerato.
    assert "own requests" in approve("telegram", "777", "aunt")
    assert "own requests" in approve("telegram", "123", "sipho")
    assert "Only a parent" in approve("telegram", "999", "lerato")
    assert ran == []
    # Sipho, from his own chat: done, as Lerato's step.
    assert approve("telegram", "555", "sipho").startswith(f"Approved #{approval}")
    assert ran == [("navigate", "lerato")]


def test_a_parent_denies_from_their_phone(family):
    approval = _childs_request()
    with as_person("sipho"):
        assert security.deny_from_remote(approval, "telegram", "555", "sipho") == f"Denied #{approval}."


# ── When FreeHand is worried ─────────────────────────────────────────

def test_a_worry_is_told_to_each_parent(family, phoned):
    with as_person("lerato"):
        out = asyncio.run(children.raise_alert("She said someone at school hits her.", False, now=NOW))
    assert out["told"] is True and "116" in out["tell_the_person"]
    for parent in ("owner", "sipho"):
        with as_person(parent):
            assert notices.unseen()[0]["title"] == "FreeHand is worried about Lerato"
    assert phoned and "someone at school hits her" in phoned[0][2]
    # Parents see it in every view, alerts only too.
    _set(family, oversight="alerts")
    assert children.activity("lerato")["alerts"][0]["worry"] == "She said someone at school hits her."


@pytest.mark.parametrize("about_a_parent", [True, None, "yes"])
def test_no_parent_is_told_when_a_parent_may_be_the_danger(family, phoned, about_a_parent):
    with as_person("lerato"):
        out = asyncio.run(children.raise_alert("Dad hurts me.", about_a_parent))
    assert out.get("not_told") is True and "116" in out["tell_the_person"]
    for parent in ("owner", "sipho"):
        with as_person(parent):
            assert notices.unseen() == []
    assert phoned == [] and children.activity("lerato")["alerts"] == []


def test_the_agent_loop_tells_a_parent(family, monkeypatch):
    replies = [{"content": "", "tool_calls": [{"id": "w1", "type": "function", "function": {
        "name": children.ALERT_TOOL,
        "arguments": json.dumps({"worry": "Says a boy at school threatens her.", "about_a_parent": False})}}]}]

    async def fake_llm(messages, tools=None, config=None):
        reply = replies.pop(0) if replies else {"content": "I've let your parent know. Childline is 116."}
        return {**reply, "_usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    monkeypatch.setattr(children, "in_quiet_hours", lambda child_id, now=None: False)
    out = asyncio.run(agent.run_agent("a boy at school threatens me", person="lerato"))
    assert "116" in out["text"]
    with as_person("sipho"):
        assert notices.unseen()[0]["title"] == "FreeHand is worried about Lerato"


def test_alerts_are_limited_to_a_few_a_day(family, phoned):
    with as_person("lerato"):
        for _ in range(5):
            asyncio.run(children.raise_alert("worried", False))
    assert len(children.activity("lerato")["alerts"]) == children.MAX_ALERTS_A_DAY


def test_only_a_child_has_the_worry_tool(family, monkeypatch):
    offered = {}

    async def fake_llm(messages, tools=None, config=None):
        offered[people.current()] = [t["function"]["name"] for t in tools or []]
        return {"content": "ok", "_usage": {"prompt_tokens": 1, "completion_tokens": 1}}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    monkeypatch.setattr(children, "in_quiet_hours", lambda child_id, now=None: False)
    asyncio.run(agent.run_agent("hello", person="lerato"))
    asyncio.run(agent.run_agent("hello", person="sipho"))
    assert children.ALERT_TOOL in offered["lerato"] and children.ALERT_TOOL not in offered["sipho"]
    with as_person("sipho"):
        out = asyncio.run(children.raise_alert("x", False))
    assert "only for children" in out["error"]
    with as_person("lerato"):
        assert children.ALERT_TOOL in children.guidance() and "about_a_parent true" in children.guidance()


def test_when_a_child_leaves_what_parents_saw_goes(family, monkeypatch):
    from core import long_memory
    monkeypatch.setattr(long_memory, "enabled", lambda: False)
    children.record("hello", {"text": "hi"}, "lerato")
    children.count_ai({"prompt_tokens": 5, "completion_tokens": 5}, "lerato")
    family.request("DELETE", "/api/people/lerato", json={})
    conn = children._activity_conn()
    assert conn.execute("SELECT COUNT(*) FROM child_activity").fetchone()[0] == 0
    conn.close()
    assert children.ai_today("lerato") == 0

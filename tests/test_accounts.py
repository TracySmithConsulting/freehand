"""Personal and shared accounts (core/accounts.py): a connected account is
one person's, or shared with everyone, and FreeHand only uses it for
someone who may. Everything is the owner's until the owner says so."""
import asyncio
import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from core import accounts, agent, agent_config, people, security, sessions


THANDI = {"id": "thandi", "name": "Thandi", "role": "member", "telegram": ["555"]}
GMAIL = "oc:gmail:default"


@contextlib.contextmanager
def as_person(pid):
    token = people.set_current(pid)
    try:
        yield
    finally:
        people.reset(token)


@pytest.fixture(autouse=True)
def office():
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", people=[THANDI])
    agent_config._save_settings(s)
    security.set_tier("semi_autonomous")


# ── Whose an account is ──────────────────────────────────────────────

def test_every_account_is_the_owners_until_the_owner_says_otherwise():
    assert accounts.owner_of(GMAIL) == "owner"
    assert accounts.may_use(GMAIL, "owner")
    assert not accounts.may_use(GMAIL, "thandi")
    with as_person("thandi"):
        assert "is Tracy's" in accounts.refusal(GMAIL)


def test_a_shared_account_is_everyones():
    accounts.set_owner("oc:gmail:info", "shared")
    assert accounts.may_use("oc:gmail:info", "thandi") and accounts.may_use("oc:gmail:info", "owner")


def test_thandis_account_isnt_even_the_owners():
    accounts.set_owner("oc:gmail:thandi", "thandi")
    assert accounts.may_use("oc:gmail:thandi", "thandi")
    assert not accounts.may_use("oc:gmail:thandi", "owner")


def test_giving_it_back_to_the_owner_clears_the_setting():
    accounts.set_owner(GMAIL, "shared")
    accounts.set_owner(GMAIL, "owner")
    assert accounts.owners() == {}


@pytest.mark.parametrize("account, whose", [("oc:gmail:default", "nobody"), ("gmail", "shared"),
                                            ("other:gmail:default", "shared")])
def test_only_real_accounts_and_people(account, whose):
    with pytest.raises(ValueError):
        accounts.set_owner(account, whose)


@pytest.mark.parametrize("tool, args, account", [
    ("read_email", {"service": "google"}, "oc:gmail:default"),
    ("send_email", {"service": "microsoft", "label": "Work"}, "oc:outlook:work"),
    ("read_email", {"service": "email", "label": "shop"}, "app:email:shop"),
    ("list_calendar_events", {}, "oc:googlecalendar:default"),
    ("create_calendar_event", {"service": "microsoft"}, "oc:outlook_calendar:default"),
    ("read_sheet_range", {"service": "google"}, "oc:googlesheets:default"),
    ("schedule_zoom_meeting", {}, "oc:zoom:default"),
    ("post_to_instagram", {}, "composio:instagram:default"),
    ("post_to_facebook", {"label": "shop"}, "app:facebook:shop"),
    ("web_search", {"query": "x"}, None),
])
def test_which_account_a_tool_uses(tool, args, account):
    assert accounts.for_tool(tool, args) == account


def test_oc_and_composio_tools_name_their_account(monkeypatch):
    from core.tools import composio_registry, registry
    monkeypatch.setattr(registry, "list_tools", lambda *a, **k: [
        {"tool_name": "oc_slack_default_list_channels", "service": "slack", "label": "default"}])
    monkeypatch.setattr(composio_registry, "list_tools", lambda *a, **k: [
        {"tool_name": "co_instagram_shop_get_user", "service": "instagram", "label": "shop"}])
    assert accounts.for_tool("oc_slack_default_list_channels") == "oc:slack:default"
    assert accounts.for_tool("co_instagram_shop_get_user") == "composio:instagram:shop"
    tools = [{"function": {"name": n}} for n in
             ("oc_slack_default_list_channels", "co_instagram_shop_get_user", "web_search")]
    with as_person("thandi"):
        assert [t["function"]["name"] for t in accounts.usable_tools(tools)] == ["web_search"]
    accounts.set_owner("oc:slack:default", "shared")
    with as_person("thandi"):
        assert len(accounts.usable_tools(tools)) == 2


# ── In a real agent run ──────────────────────────────────────────────

@pytest.fixture
def llm(monkeypatch):
    replies = []

    async def fake_llm(messages, tools=None, config=None):
        reply = replies.pop(0) if replies else {"content": "Done."}
        return {**reply, "_usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(agent, "call_llm", fake_llm)
    ran = []

    async def fake_run_tool(name, args):
        ran.append(name)
        return {"content": "{}"}

    monkeypatch.setattr(agent, "_run_tool", fake_run_tool)
    return replies, ran


def _call(name, args):
    return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def _pending():
    with as_person("owner"):
        return security.get_pending_approvals()


def test_thandi_asking_to_send_from_the_owners_gmail_is_refused_before_any_approval(llm):
    replies, ran = llm
    replies.append(_call("send_email", {"service": "google", "to": "x@example.com", "subject": "s", "body": "b"}))
    asyncio.run(agent.run_agent("email x", source="telegram", caller_id="555"))
    assert ran == [] and _pending() == []


def test_from_a_shared_mailbox_it_goes_to_approval_as_usual(llm):
    accounts.set_owner(GMAIL, "shared")
    replies, ran = llm
    replies.append(_call("send_email", {"service": "google", "to": "x@example.com", "subject": "s", "body": "b"}))
    asyncio.run(agent.run_agent("email x", source="telegram", caller_id="555"))
    assert len(_pending()) == 1


def test_the_owner_still_uses_their_own(llm):
    replies, ran = llm
    replies.append(_call("read_email", {"service": "google"}))
    asyncio.run(agent.run_agent("any new email?"))
    assert ran == ["read_email"]


def test_an_approved_step_runs_as_the_person_who_asked(monkeypatch):
    with as_person("thandi"):
        held = security.intercept_action("oauth_access", "Send", {"tool": "send_email", "args": {"service": "google"}})
    seen = []

    async def fake_execute(name, args):
        seen.append(people.current())
        return {"content": "{}"}

    monkeypatch.setattr(agent, "execute_tool", fake_execute)
    with as_person("owner"):
        security.handle_approval(held["approval_id"], "approve")
        asyncio.run(security.re_execute_approval(held["approval_id"]))
    assert seen == ["thandi"]


# ── Where each account is actually used ──────────────────────────────

def test_openconnector_refuses_someone_elses_account():
    from core.oauth import open_connector
    with as_person("thandi"):
        out = open_connector.execute_action("gmail.fetch_emails", {}, "default")
    assert out["ok"] is False and out["error"]["code"] == "not_your_account"


def test_own_domain_email_refuses_someone_elses_mailbox():
    from core.tools import own_email
    with as_person("thandi"):
        with pytest.raises(own_email.EmailError, match="Tracy's"):
            own_email.read("default")
        with pytest.raises(own_email.EmailError):
            own_email.send("default", ["x@example.com"], "s", "b")


def test_composio_refuses_someone_elses_account(monkeypatch):
    from core.tools import composio_dispatch, composio_registry, integrations
    monkeypatch.setattr(composio_registry, "_read_storage", lambda: {"tools": [
        {"tool_name": "co_instagram_default_x", "enabled": True, "service": "instagram", "label": "default",
         "composio_tool_slug": "X"}]})
    with as_person("thandi"):
        out = asyncio.run(composio_dispatch.dispatch_composio_tool("co_instagram_default_x", {}))
        data, err = asyncio.run(integrations._composio_run("instagram", "X", {}, "default"))
    assert out["error"]["code"] == "not_your_account"
    assert data is None and "Tracy's" in err["error"]


def test_freehands_own_sign_ins_refuse_someone_elses(monkeypatch):
    from core.tools import integrations
    with as_person("thandi"):
        conn, err = asyncio.run(integrations._ensure_token("facebook", "default"))
    assert conn is None and "Tracy's" in err


def test_thandi_isnt_told_the_names_of_the_owners_accounts(monkeypatch):
    from core.tools import integrations
    monkeypatch.setattr(integrations, "list_connections", lambda svc: [
        {"label": "private", "status": "active", "scopes": "", "connected_at": ""}] if svc == "email" else [])
    with as_person("thandi"):
        assert not any(r.get("label") == "private" for r in integrations.get_connections_summary())
    with as_person("owner"):
        assert any(r.get("label") == "private" for r in integrations.get_connections_summary())


# ── The Connections screen ───────────────────────────────────────────

def test_the_connections_list_says_whose_each_account_is(monkeypatch):
    from core.oauth import open_connector
    from server import app, get_or_create_api_key
    monkeypatch.setattr(open_connector, "list_accounts", lambda: [
        {"service": "gmail", "label": "default", "display": "tracy@example.com", "scopes": []},
        {"service": "gmail", "label": "info", "display": "info@example.com", "scopes": []}])
    accounts.set_owner("oc:gmail:info", "shared")
    owner = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
    gmail = next(g for g in owner.get("/api/integrations").json()["connections"] if g["service"] == "gmail")
    assert {c["label"]: (c["account"], c["whose"]) for c in gmail["connections"]} == {
        "default": ("oc:gmail:default", "owner"), "info": ("oc:gmail:info", "shared")}


def test_only_the_owner_says_whose_an_account_is():
    from server import app, get_or_create_api_key, SESSION_HEADER
    owner = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
    r = owner.post("/api/accounts/whose", json={"account": "oc:gmail:info", "whose": "shared"})
    assert r.status_code == 200 and accounts.owner_of("oc:gmail:info") == "shared"
    assert owner.post("/api/accounts/whose", json={"account": GMAIL, "whose": "nobody"}).status_code == 400
    thandi = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})
    assert thandi.post("/api/accounts/whose", json={"account": GMAIL, "whose": "thandi"}).status_code == 403
    assert accounts.owner_of(GMAIL) == "owner"

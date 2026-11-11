"""Item 7 (Nov 2026): approvals that actually work from chat.

Rules pinned here:
- Chat messages run the agent at every tier; there is no blanket
  "approve this message" gate (it created approvals nothing could release).
- The web console releases any held call, whatever channel asked for it.
- A chat gateway can approve/deny only requests made from that same chat,
  and can only release registered, non-destructive tools.
- Approval prompts go to the requesting Telegram chat; Slack prompts no
  longer carry link buttons that could never work.
"""
import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import server
from core import agent, database, gateway, security


def _pending(payload: dict) -> int:
    conn = sqlite3.connect(str(database.DB_PATH))
    cur = conn.execute(
        "INSERT INTO approvals (action_type, description, payload, status) "
        "VALUES ('oauth_access', 'Tool call', ?, 'pending')",
        (json.dumps(payload),),
    )
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid


def _status(aid: int) -> str:
    conn = sqlite3.connect(str(database.DB_PATH))
    row = conn.execute("SELECT status FROM approvals WHERE id = ?", (aid,)).fetchone()
    conn.close()
    return row[0]


def _say(text, source="telegram", caller_id="42"):
    return asyncio.run(gateway._route_to_agent(text, source=source, caller_id=caller_id))


@pytest.fixture
def executed(monkeypatch):
    calls = []

    async def fake_execute_tool(name, args):
        calls.append((name, args))
        return {"content": json.dumps({"ok": True, "tool": name})}

    monkeypatch.setattr(agent, "execute_tool", fake_execute_tool)
    return calls


@pytest.fixture
def agent_calls(monkeypatch):
    calls = []

    async def fake_run_agent(command, source="", caller_id=""):
        calls.append(command)
        return {"text": f"agent: {command}", "tools_used": [], "error": None}

    monkeypatch.setattr(agent, "run_agent", fake_run_agent)
    return calls


# ── Chat commands run at every tier ─────────────────────────────────────

@pytest.mark.parametrize("tier", ["autonomous", "semi_autonomous", "full_access"])
def test_chat_message_reaches_agent_at_every_tier(tier, agent_calls):
    security.set_tier(tier)
    assert _say("what's on my calendar?") == "agent: what's on my calendar?"
    assert agent_calls == ["what's on my calendar?"]


def test_chat_message_creates_no_blanket_approval(agent_calls):
    security.set_tier("semi_autonomous")
    _say("hello")
    assert security.get_pending_approvals() == []


def test_bare_deny_word_without_id_goes_to_agent(agent_calls):
    assert _say("deny it") == "agent: deny it"


def test_slash_approve_without_id_shows_usage(agent_calls):
    assert _say("/approve") == "Use: /approve <id>"
    assert agent_calls == []


# ── Approving from chat ────────────────────────────────────────────────

def test_owner_chat_approves_and_tool_runs(executed):
    aid = _pending({"tool": "write_docx", "args": {"path": "a.docx"},
                    "source": "telegram", "caller_id": "42"})
    reply = _say(f"/approve {aid}")
    assert reply.startswith(f"Approved #{aid}. Ran write_docx")
    assert executed == [("write_docx", {"path": "a.docx"})]
    assert _status(aid) == "approved"


def test_slack_bare_approve_form_works(executed):
    aid = _pending({"tool": "write_docx", "args": {}, "source": "slack", "caller_id": "C1"})
    reply = _say(f"approve {aid}", source="slack", caller_id="C1")
    assert reply.startswith(f"Approved #{aid}")
    assert len(executed) == 1


def test_other_chat_cannot_approve(executed):
    aid = _pending({"tool": "write_docx", "args": {}, "source": "telegram", "caller_id": "42"})
    reply = _say(f"/approve {aid}", caller_id="99")
    assert "only approve or deny requests made from this chat" in reply
    assert executed == []
    assert _status(aid) == "pending"


def test_same_id_on_another_channel_cannot_approve(executed):
    aid = _pending({"tool": "write_docx", "args": {}, "source": "telegram", "caller_id": "42"})
    _say(f"approve {aid}", source="whatsapp", caller_id="42")
    assert executed == []
    assert _status(aid) == "pending"


def test_web_console_requests_cannot_be_approved_from_chat(executed):
    aid = _pending({"tool": "write_docx", "args": {}, "source": "", "caller_id": None})
    _say(f"/approve {aid}")
    assert executed == []
    assert _status(aid) == "pending"


def test_destructive_tool_refused_from_chat_but_released_by_console(executed, monkeypatch):
    from core.tools import registry
    monkeypatch.setattr(registry, "is_destructive_tool", lambda name: name == "write_docx")
    aid = _pending({"tool": "write_docx", "args": {"path": "x"},
                    "source": "telegram", "caller_id": "42"})

    reply = _say(f"/approve {aid}")
    assert "destructive" in reply and "web console" in reply
    assert executed == []
    assert _status(aid) == "pending"  # still approvable locally

    client = TestClient(server.app, headers={"X-API-Key": server.get_or_create_api_key()})
    r = client.post(f"/api/approvals/{aid}/approve")
    assert r.status_code == 200
    assert r.json()["re_execute"]["re_executed"] is True
    assert executed == [("write_docx", {"path": "x"})]


def test_unregistered_tool_refused_from_chat(executed):
    aid = _pending({"tool": "not_a_real_tool", "args": {},
                    "source": "telegram", "caller_id": "42"})
    reply = _say(f"/approve {aid}")
    assert "not a registered tool" in reply
    assert executed == []


def test_already_handled_approval(executed):
    aid = _pending({"tool": "write_docx", "args": {}, "source": "telegram", "caller_id": "42"})
    _say(f"/approve {aid}")
    assert "already been handled" in _say(f"/approve {aid}")
    assert len(executed) == 1


# ── Denying from chat ──────────────────────────────────────────────────

def test_owner_chat_denies(executed):
    aid = _pending({"tool": "write_docx", "args": {}, "source": "telegram", "caller_id": "42"})
    assert _say(f"/deny {aid}") == f"Denied #{aid}."
    assert _status(aid) == "rejected"
    assert executed == []


def test_other_chat_cannot_deny():
    aid = _pending({"tool": "write_docx", "args": {}, "source": "telegram", "caller_id": "42"})
    _say(f"/deny {aid}", caller_id="99")
    assert _status(aid) == "pending"


# ── The agent records which chat asked ─────────────────────────────────

def test_held_write_records_source_and_caller(monkeypatch):
    security.set_tier("semi_autonomous")
    replies = iter([
        {"tool_calls": [{"id": "t1", "function": {
            "name": "write_docx", "arguments": json.dumps({"path": "a.docx"})}}]},
        {"content": "Waiting for approval."},
    ])

    async def fake_llm(messages, tools=None, config=None):
        return next(replies)

    monkeypatch.setattr(agent, "call_llm", fake_llm)
    monkeypatch.setattr(security, "_send_external_approval", lambda *a, **k: None)
    asyncio.run(agent.run_agent("write it", source="telegram", caller_id="42"))

    (row,) = security.get_pending_approvals()
    payload = json.loads(row["payload"])
    assert payload["source"] == "telegram"
    assert payload["caller_id"] == "42"
    assert payload["tool"] == "write_docx"


# ── Prompts sent to chat ───────────────────────────────────────────────

class _Sent:
    def __init__(self):
        self.bodies = []

    def __call__(self, req, timeout=None):
        self.bodies.append(json.loads(req.data.decode()))

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return b"{}"

        return _Resp()


def test_telegram_prompt_goes_to_requesting_chat_with_details(monkeypatch):
    security._save_settings({"telegram_bot_token": "T", "telegram_chat_id": "owner"})
    sent = _Sent()
    monkeypatch.setattr(security.urllib.request, "urlopen", sent)
    security._send_external_approval(
        "oauth_access", "Tool call: post_to_facebook", 7, "telegram",
        caller_id="42", payload={"args": {"content": "*hi* _there_"}},
    )
    (body,) = sent.bodies
    assert body["chat_id"] == "42"
    assert "parse_mode" not in body  # args may contain Markdown characters
    assert "*hi* _there_" in body["text"]
    assert "/approve 7" in body["text"]


def test_slack_prompt_has_no_dead_link_buttons(monkeypatch):
    security._save_settings({"slack_webhook_url": "https://hooks.example/x"})
    sent = _Sent()
    monkeypatch.setattr(security.urllib.request, "urlopen", sent)
    security._send_external_approval("oauth_access", "Tool call: x", 9, "slack", payload={})
    (body,) = sent.bodies
    assert not any(b.get("type") == "actions" for b in body["blocks"])
    assert "approve 9" in json.dumps(body)


# ── Chat approvals expire ──────────────────────────────────────────────

def _age(aid: int, hours: float) -> None:
    conn = sqlite3.connect(str(database.DB_PATH))
    conn.execute(
        "UPDATE approvals SET created_at = datetime('now', ?) WHERE id = ?",
        (f"-{hours * 3600:.0f} seconds", aid),
    )
    conn.commit()
    conn.close()


def test_fresh_chat_approval_still_works(executed):
    aid = _pending({"tool": "write_docx", "args": {}, "source": "telegram", "caller_id": "42"})
    _age(aid, 23)
    assert _say(f"/approve {aid}").startswith(f"Approved #{aid}")
    assert len(executed) == 1


def test_stale_chat_approval_refused_but_console_can_still_approve(executed):
    aid = _pending({"tool": "write_docx", "args": {"path": "a.docx"},
                    "source": "telegram", "caller_id": "42"})
    _age(aid, 25)
    reply = _say(f"/approve {aid}")
    assert "more than 24 hours old" in reply
    assert executed == []
    assert _status(aid) == "pending"

    client = TestClient(server.app, headers={"X-API-Key": server.get_or_create_api_key()})
    assert client.post(f"/api/approvals/{aid}/approve").json()["re_execute"]["re_executed"] is True
    assert executed == [("write_docx", {"path": "a.docx"})]


def test_stale_request_can_still_be_denied_from_chat():
    aid = _pending({"tool": "write_docx", "args": {}, "source": "telegram", "caller_id": "42"})
    _age(aid, 72)
    assert _say(f"/deny {aid}") == f"Denied #{aid}."
    assert _status(aid) == "rejected"


def test_unreadable_timestamp_counts_as_stale():
    assert security._is_stale({"created_at": "not a date"}) is True
    assert security._is_stale({"created_at": None}) is True

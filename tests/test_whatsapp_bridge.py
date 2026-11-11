"""WhatsApp bridge, FreeHand side (Nov 2026).

- FreeHand authenticates to the bridge with settings.bridge_secret.
- allow_from matches "+27 82 ..." against "27...@s.whatsapp.net".
- Incoming messages are answered in the background, once per message id,
  through the bridge's /reply (no more double replies).
- Approval prompts reach the WhatsApp chat that asked.
- /pair is not available from chat.
- The bridge's own Node tests run too, when Node is installed.
"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from core import gateway, security

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BRIDGE_DIR = PROJECT_ROOT / "freehand" / "bridge"
JID = "27821234567@s.whatsapp.net"


@pytest.fixture(autouse=True)
def _settings():
    gateway._seen_deliveries.clear()
    gateway._background_tasks.clear()
    gateway._save_settings({
        "bridge_secret": "a-long-test-bridge-secret",
        "whatsapp_allow_from": ["+27 82 123 4567"],
    })


async def _drain():
    while gateway._background_tasks:
        await asyncio.gather(*list(gateway._background_tasks), return_exceptions=True)


# ── allow_from normalisation ───────────────────────────────────────────

@pytest.mark.parametrize("caller,allowed", [
    (JID, True),
    ("27821234567:7@s.whatsapp.net", True),   # device suffix
    ("+27821234567", True),
    ("27820000000@s.whatsapp.net", False),
    ("123-456@g.us", False),
    ("", False),
])
def test_whatsapp_allow_from_matches_numbers(caller, allowed):
    assert gateway._check_allow_from("whatsapp", caller) is allowed


def test_lid_ids_must_be_listed_exactly():
    gateway._save_settings({"whatsapp_allow_from": ["98765@lid"]})
    assert gateway._check_allow_from("whatsapp", "98765@lid") is True
    assert gateway._check_allow_from("whatsapp", "98765@s.whatsapp.net") is False


# ── Incoming messages ──────────────────────────────────────────────────

@pytest.fixture
def chat(monkeypatch):
    record = {"ran": [], "sent": []}

    async def fake_route(text, source="", caller_id=""):
        record["ran"].append((source, caller_id, text))
        return "x" * 5000

    async def fake_send(jid, text):
        record["sent"].append((jid, text))
        return True

    monkeypatch.setattr(gateway, "_route_to_agent", fake_route)
    monkeypatch.setattr(gateway, "_whatsapp_send_message", fake_send)
    return record


def test_message_answered_once_per_message_id(chat):
    async def scenario():
        a = await gateway.handle_whatsapp_message(JID, "hi", "MSG1")
        b = await gateway.handle_whatsapp_message(JID, "hi", "MSG1")
        await _drain()
        return a, b

    a, b = asyncio.run(scenario())
    assert a == {"ok": True, "queued": True}
    assert b == {"ok": True, "duplicate": True}
    assert chat["ran"] == [("whatsapp", JID, "hi")]
    assert len(chat["sent"]) == 1


def test_unlisted_number_is_skipped(chat):
    async def scenario():
        r = await gateway.handle_whatsapp_message("27820000000@s.whatsapp.net", "hi", "MSG2")
        await _drain()
        return r

    assert asyncio.run(scenario())["skipped"] == "allow_from"
    assert chat["ran"] == []


def test_endpoint_requires_secret_and_passes_message_id(monkeypatch):
    seen = {}

    async def fake_handle(remote_jid, text, message_id=None):
        seen.update(jid=remote_jid, text=text, id=message_id)
        return {"ok": True, "queued": True}

    monkeypatch.setattr(server, "handle_whatsapp_message", fake_handle)
    client = TestClient(server.app)
    body = {"remote_jid": JID, "text": "hi", "message_id": "MSG3"}
    assert client.post("/api/gateway/whatsapp", json=body).status_code == 401
    assert client.post("/api/gateway/whatsapp", json=body,
                       headers={"X-Bridge-Secret": "wrong"}).status_code == 401
    r = client.post("/api/gateway/whatsapp", json=body,
                    headers={"X-Bridge-Secret": "a-long-test-bridge-secret"})
    assert r.json() == {"ok": True, "queued": True}
    assert seen == {"jid": JID, "text": "hi", "id": "MSG3"}


# ── Talking to the bridge ──────────────────────────────────────────────

class _Resp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload


class _Session:
    def __init__(self, calls, status=200, payload=None):
        self.calls, self.status, self.payload = calls, status, payload or {"sent": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        return _Resp(self.status, self.payload)


def test_bridge_requests_carry_the_secret(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway.aiohttp, "ClientSession", lambda: _Session(calls))
    assert asyncio.run(gateway._whatsapp_send_message(JID, "hello")) is True
    (call,) = calls
    assert call["url"] == f"http://127.0.0.1:{gateway.BRIDGE_PORT}/reply"
    assert call["headers"]["X-Bridge-Secret"] == "a-long-test-bridge-secret"
    assert call["json"] == {"jid": JID, "text": "hello"}


def test_no_bridge_request_without_a_secret(monkeypatch):
    gateway._save_settings({})
    calls = []
    monkeypatch.setattr(gateway.aiohttp, "ClientSession", lambda: _Session(calls))
    resp = asyncio.run(gateway._send_to_bridge("/reply", {"jid": JID, "text": "x"}))
    assert "bridge_secret not configured" in resp["error"]
    assert calls == []


def test_bridge_http_error_is_reported(monkeypatch):
    calls = []
    monkeypatch.setattr(gateway.aiohttp, "ClientSession",
                        lambda: _Session(calls, status=503, payload={"sent": False}))
    assert asyncio.run(gateway._whatsapp_send_message(JID, "x")) is False


def test_default_port_matches_the_bridge():
    js = (BRIDGE_DIR / "bridge-server.js").read_text(encoding="utf-8")
    assert f"const DEFAULT_PORT = {gateway.BRIDGE_PORT};" in js


def test_long_replies_are_not_cut_to_a_tweet():
    assert gateway._truncate("x" * 3000) == "x" * 3000
    assert len(gateway._truncate("x" * 5000)) == gateway.CHAT_REPLY_MAX_CHARS


# ── Approval prompts and pairing ───────────────────────────────────────

def test_approval_prompt_goes_to_the_whatsapp_chat(monkeypatch):
    sent = []

    class _R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        sent.append({"url": req.full_url, "body": json.loads(req.data.decode()),
                     "secret": req.get_header("X-bridge-secret")})
        return _R()

    monkeypatch.setattr(security.urllib.request, "urlopen", fake_urlopen)
    security._send_external_approval("oauth_access", "Tool call: write_docx", 12, "whatsapp",
                                     caller_id=JID, payload={"args": {"path": "a.docx"}})
    (msg,) = sent
    assert msg["url"].endswith("/reply")
    assert msg["secret"] == "a-long-test-bridge-secret"
    assert msg["body"]["jid"] == JID
    assert "/approve 12" in msg["body"]["text"] and "a.docx" in msg["body"]["text"]


def test_pairing_cannot_be_started_from_chat(monkeypatch):
    called = []

    async def fake_bridge(path, data=None):
        called.append(path)
        return {"code": "X"}

    monkeypatch.setattr(gateway, "_send_to_bridge", fake_bridge)
    reply = asyncio.run(gateway._route_to_agent("/pair 27821234567", source="telegram", caller_id="42"))
    assert "web console" in reply
    assert called == []


def test_status_reports_link_and_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_AUTH_DIR", str(tmp_path))
    assert gateway.get_gateway_status()["whatsapp"]["auth_exists"] is False
    (tmp_path / "creds.json").write_text("{}")
    status = gateway.get_gateway_status()["whatsapp"]
    assert status["auth_exists"] is True
    assert status["bridge_secret_set"] is True


# ── The bridge's own tests ─────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
@pytest.mark.skipif(not (BRIDGE_DIR / "node_modules").exists(), reason="bridge dependencies not installed")
def test_bridge_node_suite_passes():
    result = subprocess.run(
        ["node", "--test"], cwd=BRIDGE_DIR, capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]

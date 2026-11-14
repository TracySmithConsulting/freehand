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

    async def fake_handle(remote_jid, text, message_id=None, from_self=False, audio=None):
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


def test_bridge_url_can_point_at_the_docker_host(monkeypatch):
    monkeypatch.setenv("FREEHAND_BRIDGE_URL", "http://host.docker.internal:8765/")
    assert gateway._get_bridge_url() == "http://host.docker.internal:8765"
    monkeypatch.delenv("FREEHAND_BRIDGE_URL")
    assert gateway._get_bridge_url() == f"http://127.0.0.1:{gateway.BRIDGE_PORT}"


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
    monkeypatch.setattr(gateway, "_bridge_health", lambda: {"running": False, "connected": False})
    monkeypatch.setenv("WHATSAPP_AUTH_DIR", str(tmp_path))
    assert gateway.get_gateway_status()["whatsapp"]["auth_exists"] is False
    (tmp_path / "creds.json").write_text("{}")
    status = gateway.get_gateway_status()["whatsapp"]
    assert status["auth_exists"] is True
    assert status["bridge_secret_set"] is True


def test_status_asks_the_bridge_when_the_session_folder_is_out_of_sight(tmp_path, monkeypatch):
    """In Docker, FreeHand can't see the bridge's session folder on the host."""
    monkeypatch.setenv("WHATSAPP_AUTH_DIR", str(tmp_path))  # empty: no creds.json
    monkeypatch.setattr(gateway, "_bridge_health", lambda: {"running": True, "connected": True})
    status = gateway.get_gateway_status()["whatsapp"]
    assert status["auth_exists"] is True
    assert status["connected"] is True and status["bridge_running"] is True
    assert status["allow_from"] == ["+27 82 123 4567"]
    assert status["allow_self"] is True


def test_bridge_health_reports_a_stopped_bridge(monkeypatch):
    monkeypatch.setenv("FREEHAND_BRIDGE_URL", "http://127.0.0.1:9")  # nothing listens
    assert gateway._bridge_health() == {"running": False, "connected": False}


# ── Self chat ("Message yourself") ─────────────────────────────────────

SELF_JID = "98765432@lid"  # not on the allow list


def test_owner_self_chat_is_answered_without_an_allow_list_entry(chat):
    async def scenario():
        r = await gateway.handle_whatsapp_message(SELF_JID, "hi", "SELF1", from_self=True)
        await _drain()
        return r

    assert asyncio.run(scenario()) == {"ok": True, "queued": True}
    assert chat["ran"] == [("whatsapp", SELF_JID, "hi")]
    assert chat["sent"][0][0] == SELF_JID


def test_self_chat_can_be_switched_off(chat):
    settings = gateway._load_settings()
    settings["whatsapp_allow_self"] = False
    gateway._save_settings(settings)

    async def scenario():
        r = await gateway.handle_whatsapp_message(SELF_JID, "hi", "SELF2", from_self=True)
        await _drain()
        return r

    assert asyncio.run(scenario())["skipped"] == "allow_from"
    assert chat["ran"] == []


def test_from_self_must_be_exactly_true(chat):
    async def scenario():
        r = await gateway.handle_whatsapp_message(SELF_JID, "hi", "SELF3", from_self="yes")
        await _drain()
        return r

    assert asyncio.run(scenario())["skipped"] == "allow_from"


def test_endpoint_passes_from_self(monkeypatch):
    seen = {}

    async def fake_handle(remote_jid, text, message_id=None, from_self=False, audio=None):
        seen.update(jid=remote_jid, from_self=from_self)
        return {"ok": True, "queued": True}

    monkeypatch.setattr(server, "handle_whatsapp_message", fake_handle)
    client = TestClient(server.app)
    headers = {"X-Bridge-Secret": "a-long-test-bridge-secret"}
    client.post("/api/gateway/whatsapp", headers=headers,
                json={"remote_jid": SELF_JID, "text": "hi", "message_id": "S", "from_self": True})
    assert seen == {"jid": SELF_JID, "from_self": True}
    client.post("/api/gateway/whatsapp", headers=headers,
                json={"remote_jid": SELF_JID, "text": "hi", "message_id": "T", "from_self": "true"})
    assert seen["from_self"] is False


# ── Gateway settings form ──────────────────────────────────────────────

def test_gateway_settings_save_the_allow_list_and_self_switch(monkeypatch):
    monkeypatch.setattr(gateway, "_bridge_health", lambda: {"running": True, "connected": True})
    status = gateway.update_gateway_settings({
        "whatsapp_allow_from": "+27 82 123 4567\n, +27 83 000 0000 ,\n",
        "whatsapp_allow_self": False,
    })
    assert status["whatsapp"]["allow_from"] == ["+27 82 123 4567", "+27 83 000 0000"]
    assert status["whatsapp"]["allow_self"] is False


def test_gateway_settings_refuse_non_gateway_keys(monkeypatch):
    """The gateway form used to accept any key, including the tier."""
    monkeypatch.setattr(gateway, "_bridge_health", lambda: {"running": False, "connected": False})
    before = gateway._load_settings()
    with pytest.raises(ValueError, match="tier"):
        gateway.update_gateway_settings({"tier": "full_access", "slack_bot_token": "x"})
    assert gateway._load_settings() == before
    key = server.get_or_create_api_key()
    r = TestClient(server.app).post("/api/gateway/settings", json={"tier": "full_access"},
                                    headers={"X-API-Key": key})
    assert r.status_code == 400


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/gateway/status", None),
    ("post", "/api/gateway/settings", {"whatsapp_allow_from": ["+1 555 0100"]}),
    ("post", "/api/gateway/whatsapp/pair", {"phone": "15550100"}),
])
def test_gateway_admin_endpoints_need_the_api_key(method, path, body, monkeypatch):
    """require_api_key used to skip every /api/gateway/* path, so anyone who
    could reach FreeHand could read the status, change the allow lists or
    start WhatsApp pairing."""
    async def fake_pair(phone):
        raise AssertionError("pairing must not start without the key")

    monkeypatch.setattr(server, "handle_whatsapp_pair", fake_pair)
    client = TestClient(server.app)
    call = getattr(client, method)
    r = call(path) if body is None else call(path, json=body)
    assert r.status_code == 401
    assert "+1 555 0100" not in gateway._load_settings().get("whatsapp_allow_from", [])


# ── The bridge's own tests ─────────────────────────────────────────────

@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
@pytest.mark.skipif(not (BRIDGE_DIR / "node_modules").exists(), reason="bridge dependencies not installed")
def test_bridge_node_suite_passes():
    result = subprocess.run(
        ["node", "--test"], cwd=BRIDGE_DIR, capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]

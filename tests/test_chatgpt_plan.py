"""Using your ChatGPT plan (core/chatgpt_plan.py): each person signs in
with their own ChatGPT, OpenAI's sign-in reply is checked before anything
is kept, and FreeHand's chats are turned into Responses requests that
follow the preview's rules. OpenAI itself is stood in for."""
import asyncio
import base64
import contextlib
import html
import json
import time
import urllib.parse

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient

from core import agent, agent_config, chatgpt_plan, people, sessions
from server import app, get_or_create_api_key, SESSION_HEADER

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwk() -> dict:
    n = KEY.public_key().public_numbers()
    return {"kty": "RSA", "kid": "k1", "alg": "RS256",
            "n": b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")), "e": b64(n.e.to_bytes(3, "big"))}


def id_token(**claims) -> str:
    body = {"iss": chatgpt_plan.AUTH, "aud": "oaiapp_thandi", "sub": "user-1", "email": "thandi@example.com",
            "exp": time.time() + 3600, "nonce": "n", **claims}
    head = b64(json.dumps({"alg": "RS256", "kid": "k1"}).encode())
    payload = b64(json.dumps(body).encode())
    sig = KEY.sign(f"{head}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{payload}.{b64(sig)}"


class FakeOpenAI:
    """auth.openai.com and api.openai.com, as far as FreeHand uses them."""

    def __init__(self):
        self.forms = []
        self.scope = chatgpt_plan.SCOPES
        self.refresh = (200, None)
        self.nonce = None

    async def get_json(self, url, headers=None):
        if url == chatgpt_plan.DISCOVERY_URL:
            return {"jwks_uri": "https://auth.openai.com/jwks", "revocation_endpoint": "https://auth.openai.com/revoke"}
        if url.endswith("/jwks"):
            return {"keys": [jwk()]}
        if url.endswith("/models"):
            return {"models": [{"slug": "gpt-6.2-sol", "display_name": "GPT-6.2", "visibility": "list"},
                               {"slug": "hidden", "display_name": "Hidden", "visibility": "hide"}]}
        return None

    async def post_form(self, url, form):
        self.forms.append((url, dict(form)))
        if url == chatgpt_plan.TOKEN_URL and form["grant_type"] == "authorization_code":
            return 200, {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600,
                         "scope": self.scope, "id_token": id_token(aud=form["client_id"], nonce=self.nonce)}
        if url == chatgpt_plan.TOKEN_URL and form["grant_type"] == "refresh_token":
            status, data = self.refresh
            return status, data or {"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600,
                                    "scope": self.scope}
        if url.endswith("/revoke"):
            return 200, None
        return 404, None


@pytest.fixture
def openai(monkeypatch):
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", people=[{"id": "thandi", "name": "Thandi", "role": "member"}])
    agent_config._save_settings(s)
    fake = FakeOpenAI()
    monkeypatch.setattr(chatgpt_plan, "_get_json", fake.get_json)
    monkeypatch.setattr(chatgpt_plan, "_post_form_status", fake.post_form)
    chatgpt_plan._keys_cache.update(at=0.0, discovery=None, jwks=None)
    chatgpt_plan._pending.clear()
    return fake


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


def sign_in(client, fake, client_id="oaiapp_thandi"):
    """Start from the page, approve at "ChatGPT", come back to the callback."""
    r = client.post("/api/chatgpt/start", headers={"Host": "127.0.0.1:8000"})
    assert r.status_code == 200, r.text
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["url"]).query)
    fake.nonce = q["nonce"][0]
    back = client.get("/auth/callback", params={"code": "c1", "state": q["state"][0], "client_id": client_id})
    return q, back


def thandi():
    return TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})


def owner():
    return TestClient(app, headers={"X-API-Key": get_or_create_api_key()})


def test_signing_in_registers_freehand_and_keeps_it_safely(openai):
    client = thandi()
    q, back = sign_in(client, openai)
    assert q["client_id"] == ["dynamic_agent_client"] and q["agent_name_hint"] == ["FreeHand"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8000/auth/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["resource"] == ["https://api.openai.com/v1"]
    assert set(q["scope"][0].split()) >= {"openid", "offline_access", "chatgpt.tokens.use.direct"}
    assert q["ext_agent_host_id"][0].startswith("urn:uuid:")
    assert back.status_code == 200 and "Signed in with ChatGPT" in back.text
    status = client.get("/api/chatgpt").json()
    assert status["signed_in"] and status["email"] == "thandi@example.com"
    assert status["models"] == [{"slug": "gpt-6.2-sol", "name": "GPT-6.2"}]  # hidden ones left out
    assert "access-1" not in json.dumps(status)  # tokens never reach the page
    raw = (agent_config.VAULT_DIR / chatgpt_plan.FILE_NAME).read_text()
    assert "access-1" not in raw and "refresh-1" not in raw and "thandi@example.com" not in raw  # encrypted
    exchange = [f for u, f in openai.forms if f.get("grant_type") == "authorization_code"][0]
    assert exchange["client_id"] == "oaiapp_thandi" and exchange["code_verifier"]


def test_its_hers_alone(openai):
    sign_in(thandi(), openai)
    assert not owner().get("/api/chatgpt").json()["signed_in"]
    assert chatgpt_plan.signed_in("thandi") and not chatgpt_plan.signed_in("owner")


def test_signing_in_again_reuses_the_registration(openai):
    client = thandi()
    sign_in(client, openai)
    host = chatgpt_plan.host_id()
    q, back = sign_in(client, openai)
    assert q["client_id"] == ["oaiapp_thandi"] and "agent_name_hint" not in q
    assert q["ext_agent_host_id"] == [host] and q["id_token_hint"] and q["login_hint"] == ["thandi@example.com"]
    assert back.status_code == 200


def test_declining_the_plan_is_explained(openai):
    openai.scope = "openid profile email offline_access"
    client = thandi()
    sign_in(client, openai)
    status = client.get("/api/chatgpt").json()
    assert not status["signed_in"] and "didn't allow FreeHand" in status["problem"]
    q, _ = sign_in(client, openai)
    assert q["prompt"] == ["consent"]  # asked again, properly


@pytest.mark.parametrize("change, words", [
    ({"aud": "someone-else"}, "wasn't for FreeHand"),
    ({"iss": "https://evil.example"}, "wasn't for FreeHand"),
    ({"nonce": "other"}, "didn't match"),
    ({"exp": time.time() - 600}, "expired"),
])
def test_a_wrong_sign_in_reply_is_refused(change, words):
    token = id_token(**{"aud": "oaiapp_thandi", **change})
    with pytest.raises(chatgpt_plan.SignInError, match=words):
        chatgpt_plan.check_jwt(token, [jwk()], "oaiapp_thandi", "n")


def test_a_forged_signature_is_refused():
    head, body, sig = id_token().split(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=="))
    forged["sub"] = "someone-else"
    with pytest.raises(chatgpt_plan.SignInError, match="didn't check out"):
        chatgpt_plan.check_jwt(f"{head}.{b64(json.dumps(forged).encode())}.{sig}", [jwk()], "oaiapp_thandi", "n")


def test_a_callback_freehand_didnt_start_is_refused(openai):
    r = TestClient(app).get("/auth/callback", params={"code": "c", "state": "made-up", "client_id": "x"})
    assert r.status_code == 400 and "wasn't started from FreeHand" in html.unescape(r.text)
    assert not chatgpt_plan.signed_in("owner")


def test_saying_no_at_chatgpt_changes_nothing(openai):
    client = thandi()
    r = client.post("/api/chatgpt/start", headers={"Host": "127.0.0.1:8000"})
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["url"]).query)["state"][0]
    back = TestClient(app).get("/auth/callback", params={"state": state, "error": "access_denied"})
    assert back.status_code == 400 and "didn't allow FreeHand" in html.unescape(back.text)
    assert not chatgpt_plan.signed_in("thandi")


def test_on_another_device_the_address_is_pasted(openai):
    client = thandi()
    r = client.post("/api/chatgpt/start", headers={"Host": "freehand.example.com"})
    assert r.json()["comes_back"] is False
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["url"]).query)
    assert q["redirect_uri"] == ["http://127.0.0.1:1455/auth/callback"]
    openai.nonce = q["nonce"][0]
    bad = client.post("/api/chatgpt/finish", json={"address": "https://example.com/somewhere"})
    assert bad.status_code == 400 and "isn't the address" in bad.json()["detail"]
    address = f"http://127.0.0.1:1455/auth/callback?code=c1&state={q['state'][0]}&client_id=oaiapp_thandi"
    assert owner().post("/api/chatgpt/finish", json={"address": address}).status_code == 400  # not the owner's
    done = client.post("/api/chatgpt/finish", json={"address": address})
    assert done.status_code == 200 and done.json()["signed_in"]


def test_the_token_is_renewed_and_the_new_one_kept(openai):
    sign_in(thandi(), openai)
    acc = chatgpt_plan._account("thandi")
    acc["expires_at"] = time.time() + 10
    chatgpt_plan._save_account("thandi", acc)
    assert asyncio.run(chatgpt_plan.access_token("thandi")) == "access-2"
    assert chatgpt_plan._account("thandi")["refresh_token"] == "refresh-2"  # rotated
    refresh = [f for u, f in openai.forms if f.get("grant_type") == "refresh_token"][0]
    assert refresh["client_id"] == "oaiapp_thandi" and "scope" not in refresh


def test_an_ended_sign_in_asks_to_sign_in_again(openai):
    sign_in(thandi(), openai)
    acc = chatgpt_plan._account("thandi")
    acc["expires_at"] = 0
    chatgpt_plan._save_account("thandi", acc)
    openai.refresh = (400, {"error": "invalid_grant"})
    assert asyncio.run(chatgpt_plan.access_token("thandi")) is None
    status = chatgpt_plan.status("thandi")
    assert not status["signed_in"] and "has ended" in status["problem"]


def test_a_network_hiccup_keeps_the_sign_in(openai):
    sign_in(thandi(), openai)
    acc = chatgpt_plan._account("thandi")
    acc["expires_at"] = 0
    chatgpt_plan._save_account("thandi", acc)
    openai.refresh = (None, None)
    assert asyncio.run(chatgpt_plan.access_token("thandi")) is None
    assert chatgpt_plan.signed_in("thandi")  # not erased for a passing failure


def test_signing_out_ends_the_session_at_openai(openai):
    client = thandi()
    sign_in(client, openai)
    r = client.post("/api/chatgpt/sign-out")
    assert r.status_code == 200 and r.json()["revoked"] and not r.json()["status"]["signed_in"]
    revoke = [f for u, f in openai.forms if u.endswith("/revoke")][0]
    assert revoke == {"token": "refresh-1", "token_type_hint": "refresh_token", "client_id": "oaiapp_thandi"}
    assert chatgpt_plan._account("thandi")["client_id"] == "oaiapp_thandi"  # kept for next time


def test_choosing_a_model(openai):
    client = thandi()
    sign_in(client, openai)
    assert client.post("/api/chatgpt/model", json={"model": "nope"}).status_code == 400
    assert client.post("/api/chatgpt/model", json={"model": "gpt-6.2-sol"}).json()["model"] == "gpt-6.2-sol"


def test_the_welcome_is_shown_once(openai):
    client = thandi()
    sign_in(client, openai)
    assert client.get("/api/chatgpt").json()["welcomed"] is False
    client.post("/api/chatgpt/welcomed")
    sign_in(client, openai)
    assert client.get("/api/chatgpt").json()["welcomed"] is True


def test_hosted_freehands_dont_offer_it(openai, monkeypatch):
    monkeypatch.setenv("FREEHAND_CHATGPT_PLAN", "0")
    client = thandi()
    assert client.get("/api/chatgpt").json()["available"] is False
    assert client.post("/api/chatgpt/start").status_code == 400


def test_a_chat_becomes_a_responses_request():
    tools = [{"type": "function", "function": {"name": "send_email", "description": "Send an email.",
                                               "parameters": {"type": "object", "properties": {"to": {"type": "string"}}}}}]
    messages = [
        {"role": "system", "content": "You are FreeHand."},
        {"role": "user", "content": "Email Sipho"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "send_email", "arguments": "{\"to\":\"s@x\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Sent."},
        {"role": "user", "content": [{"type": "text", "text": "Thanks"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]},
    ]
    body = chatgpt_plan.to_request(messages, tools, "gpt-6.2-sol")
    assert body["store"] is False and body["stream"] is True
    assert body["instructions"] == "You are FreeHand."
    assert not any(i.get("role") == "system" for i in body["input"])
    for banned in ("temperature", "max_output_tokens", "max_tokens", "previous_response_id", "user", "metadata"):
        assert banned not in body
    assert body["input"][1] == {"type": "function_call", "call_id": "call_1", "namespace": "freehand",
                                "name": "send_email", "arguments": "{\"to\":\"s@x\"}"}
    assert body["input"][2] == {"type": "function_call_output", "call_id": "call_1", "output": "Sent."}
    assert body["input"][3]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,AA"}
    (ns,) = body["tools"]
    assert ns["type"] == "namespace" and ns["tools"][0]["name"] == "send_email" and ns["tools"][0]["strict"] is False


class FakeStream:
    def __init__(self, events):
        text = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        self.chunks = [text[i:i + 37].encode() for i in range(0, len(text), 37)]  # split mid-event

    @property
    def content(self):
        return self

    async def iter_any(self):
        for c in self.chunks:
            yield c


def test_the_answer_is_read_through_to_the_end():
    events = [
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 12, "output_tokens": 3}, "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "content": [{"type": "output_text", "text": "Hello"}]},
            {"type": "function_call", "call_id": "call_9", "namespace": "freehand", "name": "web_search",
             "arguments": "{\"q\":\"x\"}"}]}},
    ]
    msg, status, _ = asyncio.run(chatgpt_plan._read_stream(FakeStream(events)))
    assert status == 200 and msg["content"] == "Hello"
    assert msg["tool_calls"] == [{"id": "call_9", "type": "function",
                                  "function": {"name": "web_search", "arguments": "{\"q\":\"x\"}"}}]
    assert msg["_usage"] == {"prompt_tokens": 12, "completion_tokens": 3}
    assert agent.parse_tool_call(msg)["name"] == "web_search"


def test_a_stream_that_stops_early_isnt_an_answer():
    msg, status, _ = asyncio.run(chatgpt_plan._read_stream(FakeStream([
        {"type": "response.output_text.delta", "delta": "Hel"}])))
    assert "error" in msg and status is None


def test_a_used_up_limit_says_where_to_look():
    msg, status, _ = asyncio.run(chatgpt_plan._read_stream(FakeStream([
        {"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}])))
    assert status == 429 and chatgpt_plan.USAGE_URL in msg["error"]


def test_her_chats_use_her_plan_and_yours_dont(openai, monkeypatch):
    sign_in(thandi(), openai)
    sent = []

    async def fake_send(body, token, timeout):
        sent.append((token, body["model"]))
        return {"role": "assistant", "content": "From ChatGPT"}, 200, False

    async def usual(messages, tools, config, last):
        return {"role": "assistant", "content": "From the usual model"}, 200, False

    monkeypatch.setattr(chatgpt_plan, "_send", fake_send)
    real_one = agent._call_one

    async def one(messages, tools, config, last):
        if config.get("provider") == "chatgpt":
            return await real_one(messages, tools, config, last)
        return await usual(messages, tools, config, last)

    monkeypatch.setattr(agent, "_call_one", one)
    agent_config.set_llm_config({"provider": "openrouter", "api_key": "sk-or-x", "model": "m"})
    with as_person("thandi"):
        assert asyncio.run(agent.call_llm([{"role": "user", "content": "hi"}]))["content"] == "From ChatGPT"
    with as_person("owner"):
        assert asyncio.run(agent.call_llm([{"role": "user", "content": "hi"}]))["content"] == "From the usual model"
    assert sent == [("access-1", "gpt-6.2-sol")]


def test_when_her_limit_is_used_up_the_usual_model_takes_over(openai, monkeypatch):
    sign_in(thandi(), openai)

    async def limited(body, token, timeout):
        return {"error": chatgpt_plan.explain(429, "subscription_sharing_usage_limit_exceeded")}, 429, False

    real_one = agent._call_one

    async def one(messages, tools, config, last):
        if config.get("provider") == "chatgpt":
            return await real_one(messages, tools, config, last)
        return {"role": "assistant", "content": "From the usual model"}, 200, False

    monkeypatch.setattr(chatgpt_plan, "_send", limited)
    monkeypatch.setattr(agent, "_call_one", one)
    agent.LAST_SWITCH.clear()
    agent._SWITCH_NOTICED.clear()
    agent_config.set_llm_config({"provider": "openrouter", "api_key": "sk-or-x", "model": "m"})
    with as_person("thandi"):
        msg = asyncio.run(agent.call_llm([{"role": "user", "content": "hi"}]))
    assert msg["content"] == "From the usual model"
    assert msg["_switched"]["from"] == "your ChatGPT plan" and chatgpt_plan.USAGE_URL in msg["_switched"]["reason"]


def test_only_chatgpt_is_enough_to_start(openai):
    client = owner()
    sign_in(client, openai, client_id="oaiapp_tracy")
    from core import first_run, scheduler
    assert first_run.status()["needed"] is False
    assert first_run.status()["model"]["service"] == "your ChatGPT plan"
    assert scheduler._llm_ready()
    assert client.get("/api/runner/status").json()["llm_configured"] is True


def test_when_she_leaves_her_sign_in_ends(openai):
    sign_in(thandi(), openai)
    from core import leaving
    result = asyncio.run(leaving.remove("thandi"))
    assert any("ChatGPT plan" in line for line in result["done"])
    assert chatgpt_plan._account("thandi") is None
    assert any(u.endswith("/revoke") for u, _ in openai.forms)

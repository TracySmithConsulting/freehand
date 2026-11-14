"""Easier AI model setup (Nov 2026): presets, key detection, model lists,
a test before saving, and OpenRouter sign-in. No real service is called:
llm_providers' HTTP is replaced with a fake."""
import io
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

import server
from core import agent_config, llm_providers


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def fake_http(monkeypatch):
    """routes: {url substring: (status, json)}; records requests."""
    routes, seen = {}, []

    def urlopen(req, timeout=None):
        seen.append(req)
        for part, (status, payload) in routes.items():
            if part in req.full_url:
                if status != 200:
                    raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(b"{}"))
                return FakeResponse(json.dumps(payload).encode())
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(llm_providers.urllib.request, "urlopen", urlopen)
    return routes, seen


MODELS = {"data": [{"id": "MiniMax-M2.5"}, {"id": "minimax-m3"}]}


def test_a_key_prefix_names_the_service(fake_http):
    routes, seen = fake_http
    routes["openrouter.ai/api/v1/models"] = (200, {"data": [{"id": "anthropic/claude-sonnet"}]})
    out = llm_providers.detect("sk-or-v1-abc")
    assert out["provider"] == "openrouter" and out["models"] == ["anthropic/claude-sonnet"]
    assert len(seen) == 1  # no guessing needed


def test_a_key_without_a_prefix_goes_only_to_the_chosen_service(fake_http):
    routes, seen = fake_http
    routes["api.minimax.io/v1/models"] = (200, MODELS)
    out = llm_providers.detect("eyJhbGciOi.minimax-style-key", selected="minimax")
    assert out["provider"] == "minimax" and "minimax-m3" in out["models"]
    assert [r.full_url for r in seen] == ["https://api.minimax.io/v1/models"]


def test_a_key_is_never_tried_at_several_services(fake_http):
    """A key is a password: sending it around to find its owner would hand
    it to companies it doesn't belong to."""
    _, seen = fake_http
    out = llm_providers.detect("nobody-knows-this-key")
    assert "Choose the service" in out["error"]
    assert seen == []


def test_a_known_prefix_beats_the_chosen_service(fake_http):
    routes, seen = fake_http
    routes["api.anthropic.com/v1/models"] = (200, {"data": [{"id": "claude-sonnet-4-6"}]})
    out = llm_providers.detect("sk-ant-api03-x", selected="minimax")
    assert out["provider"] == "anthropic" and out["by_prefix"]
    assert all("anthropic.com" in r.full_url for r in seen)


def test_google_model_names_lose_their_prefix(fake_http):
    routes, _ = fake_http
    routes["generativelanguage"] = (200, {"data": [{"id": "models/gemini-2.5-flash"}]})
    assert llm_providers.list_models("google", "AIzaKey")[0] == ["gemini-2.5-flash"]


@pytest.mark.parametrize("status,words", [
    (401, "key wasn't accepted"), (402, "needs credit"), (404, "didn't recognise that model"),
    (429, "hit a limit"), (503, "problem on its side")])
def test_errors_are_in_plain_words(fake_http, status, words):
    routes, _ = fake_http
    routes["chat/completions"] = (status, {})
    out = llm_providers.test("minimax", "minimax-m3", "key")
    assert not out["ok"] and words in out["message"]


def test_a_working_test(fake_http):
    routes, seen = fake_http
    routes["chat/completions"] = (200, {"choices": [{"message": {"content": "OK"}}]})
    out = llm_providers.test("minimax", "minimax-m3", "key")
    assert out["ok"]
    assert json.loads(seen[0].data)["temperature"] == 0


def test_claude_requests_leave_out_temperature(fake_http):
    routes, seen = fake_http
    routes["chat/completions"] = (200, {"choices": [{"message": {"content": "OK"}}]})
    llm_providers.test("anthropic", "claude-sonnet-4-6", "sk-ant-x")
    assert "temperature" not in json.loads(seen[0].data)
    assert llm_providers.omit_temperature("anthropic")


def test_saved_settings_from_older_versions_are_recognised():
    assert llm_providers.identify("openai_compatible", "https://api.minimax.io/v1") == "minimax"
    assert llm_providers.identify("custom", "https://my.own/v1") == "custom"
    assert llm_providers.identify("google") == "google"


def test_the_preset_address_is_used_when_none_is_saved():
    base, headers = agent_config.resolve_llm_endpoint({"provider": "groq", "api_key": "gsk_x", "base_url": ""})
    assert base == "https://api.groq.com/openai/v1" and headers["Authorization"] == "Bearer gsk_x"


def test_google_keys_are_sent(monkeypatch):
    """The old code dropped the Gemini key unless the address had an
    {API_KEY} placeholder, so Gemini requests went unauthenticated."""
    _, headers = agent_config.resolve_llm_endpoint({"provider": "google", "api_key": "AIzaX", "base_url": ""})
    assert headers["Authorization"] == "Bearer AIzaX"


# ── OpenRouter sign-in ───────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(server.app, headers={"X-API-Key": server.get_or_create_api_key()})


def test_openrouter_sign_in_starts_with_a_pkce_link(client):
    url = client.post("/api/llm/openrouter/start").json()["url"]
    assert url.startswith("https://openrouter.ai/auth?")
    assert "code_challenge_method=S256" in url and "state=" in url
    assert "callback_url=http%3A%2F%2Flocalhost" in url


def test_a_callback_without_a_state_from_freehand_is_refused(client):
    r = client.get("/api/llm/openrouter/callback?code=abc&state=made-up")
    assert r.status_code == 400 and "Sign-in expired" in r.text
    assert agent_config.get_llm_config().get("api_key", "") == ""


def test_starting_sign_in_needs_the_api_key():
    r = TestClient(server.app).post("/api/llm/openrouter/start")
    assert r.status_code == 401


# ── Pasted endpoints and Agnes AI (Nov 2026) ──────────────────────────

def test_a_pasted_full_endpoint_becomes_the_base_address():
    from core import llm_providers as lp
    for pasted in ("https://apihub.agnes-ai.com/v1/chat/completions",
                   "https://apihub.agnes-ai.com/v1/chat/completions/",
                   "https://apihub.agnes-ai.com/v1/models",
                   " https://apihub.agnes-ai.com/v1/ "):
        assert lp.base_url_for("custom", pasted) == "https://apihub.agnes-ai.com/v1"


def test_a_pasted_agnes_endpoint_is_recognised_as_agnes():
    from core import llm_providers as lp
    assert lp.identify("custom", "https://apihub.agnes-ai.com/v1/chat/completions") == "agnes"
    assert lp.get("agnes")["base_url"] == "https://apihub.agnes-ai.com/v1"
    assert any(p["id"] == "agnes" for p in lp.public_list())


def test_a_missing_model_list_points_at_the_address(monkeypatch):
    import urllib.error
    from core import llm_providers as lp

    def not_found(*a, **k):
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
    monkeypatch.setattr(lp.urllib.request, "urlopen", not_found)
    models, problem = lp.list_models("custom", "k", "https://example.test/api")
    assert models is None and "address" in problem and "/v1" in problem

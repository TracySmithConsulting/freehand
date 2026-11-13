"""Hindsight uses FreeHand's AI model through /internal/llm/v1 (Nov 2026),
so people set a model up once. The real model is never called here."""
import json

import pytest
from fastapi.testclient import TestClient

import server
from core import agent_config


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FREEHAND_LLM_PROXY_TOKEN", "proxy-secret")
    monkeypatch.setattr(agent_config, "get_llm_config", lambda: {
        "provider": "custom", "model": "minimax-m3", "api_key": "sk-real-key",
        "base_url": "https://api.minimax.io/v1/"})
    return TestClient(server.app)


AUTH = {"Authorization": "Bearer proxy-secret"}


class FakeUpstream:
    def __init__(self, seen):
        self.seen = seen
        self.status = 200
        self.headers = {"Content-Type": "application/json"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()


class FakeSession:
    seen = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        FakeSession.seen.update(url=url, body=json, headers=headers)
        return FakeUpstream(FakeSession.seen)


def test_requests_go_to_freehands_model_with_its_key(client, monkeypatch):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    r = client.post("/internal/llm/v1/chat/completions", headers=AUTH,
                    json={"model": "freehand", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "ok"
    seen = FakeSession.seen
    assert seen["url"] == "https://api.minimax.io/v1/chat/completions"
    assert seen["body"]["model"] == "minimax-m3"  # FreeHand's model, not the placeholder
    assert seen["headers"]["Authorization"] == "Bearer sk-real-key"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"X-API-Key": "proxy-secret"}])
def test_without_the_token_it_is_refused(client, headers):
    r = client.post("/internal/llm/v1/chat/completions", headers=headers, json={"messages": []})
    assert r.status_code == 401


def test_it_is_off_when_no_token_is_configured(client, monkeypatch):
    monkeypatch.delenv("FREEHAND_LLM_PROXY_TOKEN")
    r = client.post("/internal/llm/v1/chat/completions", headers=AUTH, json={"messages": []})
    assert r.status_code == 401


def test_no_model_set_up_says_so(client, monkeypatch):
    monkeypatch.setattr(agent_config, "get_llm_config", lambda: {"provider": "openai", "model": "x", "api_key": "", "base_url": ""})
    r = client.post("/internal/llm/v1/chat/completions", headers=AUTH, json={"messages": []})
    assert r.status_code == 503 and "No AI model" in r.json()["detail"]


def test_models_lists_freehands_model(client):
    r = client.get("/internal/llm/v1/models", headers=AUTH)
    assert r.json()["data"][0]["id"] == "minimax-m3"

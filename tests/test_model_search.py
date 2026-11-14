"""Search all models (Nov 2026): one search across the services FreeHand
has keys for, plus OpenRouter's public list, grouped by service. Keys
stay on the server and only ever go to their own service."""
import pytest
from fastapi.testclient import TestClient

import server
from core import agent_config, llm_catalog, llm_providers


@pytest.fixture(autouse=True)
def fresh_cache():
    llm_catalog._cache.clear()


@pytest.fixture
def services(monkeypatch):
    """Fake model lists; records which key went to which service."""
    calls = []
    lists = {
        "minimax": ["MiniMax-M2.5", "MiniMax-M3"],
        "ollama_cloud": ["gpt-oss:120b", "qwen3-coder:480b", "qwen3:235b"],
        "openrouter": ["anthropic/claude-sonnet-4.5", "qwen/qwen3-max", "openai/gpt-5"],
    }

    def fake_list(provider, api_key, base_url="", timeout=8.0, limit=300):
        calls.append((provider, api_key))
        return lists.get(provider, []), ""
    monkeypatch.setattr(llm_providers, "list_models", fake_list)
    agent_config.set_llm_config({"provider": "minimax", "model": "MiniMax-M2.5", "api_key": "mm-key", "base_url": ""})
    agent_config._save_llm_backups([{"provider": "ollama_cloud", "model": "gpt-oss:120b", "api_key": "ol-key", "base_url": ""}])
    return calls


def test_results_are_grouped_by_service(services):
    out = llm_catalog.search("qwen")
    by = {g["name"]: g for g in out["groups"]}
    assert by["Ollama Cloud"]["models"] == ["qwen3-coder:480b", "qwen3:235b"]
    assert by["OpenRouter"]["models"] == ["qwen/qwen3-max"]
    assert by["MiniMax"]["matches"] == 0
    assert by["OpenRouter"]["ready"] is False  # browsing only: no OpenRouter key yet


def test_every_word_must_match(services):
    out = llm_catalog.search("qwen coder")
    assert [m for g in out["groups"] for m in g["models"]] == ["qwen3-coder:480b"]


def test_each_key_only_goes_to_its_own_service(services):
    llm_catalog.search("q")
    assert ("minimax", "mm-key") in services
    assert ("ollama_cloud", "ol-key") in services
    assert ("openrouter", "") in services  # the public list, no key at all
    assert len(services) == 3


def test_lists_are_cached(services):
    llm_catalog.search("qwen")
    llm_catalog.search("claude")
    assert len(services) == 3


def test_choosing_a_backup_services_model_as_main_promotes_it(services):
    out = llm_catalog.pick("ollama_cloud", "qwen3:235b", "", "main")
    assert out["ok"] and "previous main model is now the first backup" in out["message"]
    main = agent_config.get_llm_config()
    assert (main["provider"], main["model"], main["api_key"]) == ("ollama_cloud", "qwen3:235b", "ol-key")
    assert agent_config.get_llm_backups()[0]["provider"] == "minimax"


def test_adding_a_backup_reuses_the_saved_key(services):
    assert llm_catalog.pick("minimax", "MiniMax-M3", "", "backup")["ok"]
    added = agent_config.get_llm_backups()[-1]
    assert (added["provider"], added["model"], added["api_key"]) == ("minimax", "MiniMax-M3", "mm-key")


def test_openrouter_models_need_a_sign_in_first(services):
    out = llm_catalog.pick("openrouter", "openai/gpt-5", "", "main")
    assert not out["ok"] and "Sign in with OpenRouter" in out["message"]
    assert agent_config.get_llm_config()["provider"] == "minimax"


def test_the_endpoints_need_the_api_key_and_work(services):
    assert TestClient(server.app).get("/api/llm/catalog?q=qwen").status_code == 401
    c = TestClient(server.app, headers={"X-API-Key": server.get_or_create_api_key()})
    groups = c.get("/api/llm/catalog?q=qwen").json()["groups"]
    assert any(g["matches"] for g in groups)
    assert c.post("/api/llm/pick", json={"provider": "x", "model": "y", "action": "delete"}).status_code == 400


def test_the_model_list_never_sends_the_main_key_to_another_service(monkeypatch):
    """It used to: with MiniMax as main and Ollama Cloud chosen, the MiniMax
    key went to Ollama when the key box was empty."""
    sent = []
    monkeypatch.setattr(llm_providers, "list_models",
                        lambda provider, key, base_url="", timeout=8.0: sent.append((provider, key)) or ([], ""))
    agent_config.set_llm_config({"provider": "minimax", "model": "m", "api_key": "mm-key", "base_url": ""})
    c = TestClient(server.app, headers={"X-API-Key": server.get_or_create_api_key()})
    c.post("/api/llm/models", json={"provider": "ollama_cloud", "api_key": ""})
    c.post("/api/llm/models", json={"provider": "minimax", "api_key": ""})
    assert sent == [("ollama_cloud", ""), ("minimax", "mm-key")]

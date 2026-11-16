"""The first-run setup (core/first_run.py, step 2 of
docs/design/granny-installer.md): a new FreeHand with no AI model walks the
person through getting one, testing every key for real before keeping it."""
import pytest

from core import first_run, llm_providers
from core.agent_config import get_llm_backups, get_llm_config


@pytest.fixture
def services(monkeypatch):
    """Pretend AI services: detect says which service, test says it works."""
    seen = {"tests": []}

    def fake_detect(key, selected=""):
        if key.startswith("nvapi-"):
            return {"provider": "nvidia", "name": "NVIDIA (free models)", "models": ["meta/llama-3.3-70b-instruct", "x/other"]}
        if key.startswith("sk-or-"):
            return {"provider": "openrouter", "name": "OpenRouter", "models": ["openrouter/auto"]}
        if key.startswith("gsk_"):
            return {"provider": "groq", "name": "Groq", "models": ["openai/gpt-oss-20b", "qwen/qwen3-32b"]}
        return {"error": "FreeHand can't tell which service this key is for from the key alone."}

    def fake_test(provider, model, key, base_url=""):
        seen["tests"].append((provider, model))
        if key.endswith("bad"):
            return {"ok": False, "message": "The service said the key isn't right."}
        return {"ok": True, "message": "Working."}
    monkeypatch.setattr(llm_providers, "detect", fake_detect)
    monkeypatch.setattr(llm_providers, "test", fake_test)
    return seen


def test_a_new_freehand_needs_setting_up():
    s = first_run.status()
    assert s["needed"] is True and s["done"] is False and s["backups"] == 0 and "model" not in s


def test_a_key_is_tested_then_kept_as_the_main_model(services):
    out = first_run.use_key("gsk_abc123")
    assert out["ok"] and out["model"] == "openai/gpt-oss-20b"  # from the account's own list
    assert services["tests"] == [("groq", "openai/gpt-oss-20b")]
    config = get_llm_config()
    assert (config["provider"], config["model"], config["api_key"]) == ("groq", "openai/gpt-oss-20b", "gsk_abc123")
    s = first_run.status()
    assert s["needed"] is False and s["model"] == {"service": "Groq", "model": "openai/gpt-oss-20b"}


def test_a_key_that_doesnt_work_isnt_kept(services):
    out = first_run.use_key("gsk_bad")
    assert not out["ok"] and "didn't answer with that key" in out["message"]
    assert first_run.status()["needed"] is True


def test_a_key_from_an_unknown_service_says_so(services):
    out = first_run.use_key("mystery-key")
    assert not out["ok"] and "can't tell which service" in out["message"]


def test_a_free_openrouter_start_uses_the_free_models(services):
    assert first_run.use_key("sk-or-abc", free=True)["model"] == "openrouter/free"


def test_a_backup_is_added_not_swapped_in(services):
    first_run.use_key("gsk_main")
    out = first_run.use_key("nvapi-abc", provider="nvidia", role="backup")
    assert out["ok"] and "as a backup" in out["message"]
    assert get_llm_config()["provider"] == "groq"
    assert [(b["provider"], b["model"]) for b in get_llm_backups()] == [("nvidia", "meta/llama-3.3-70b-instruct")]
    assert first_run.use_key("nvapi-abc", provider="nvidia", role="backup")["message"].endswith("already a backup.")
    assert len(get_llm_backups()) == 1


def test_models_are_chosen_from_what_the_account_offers():
    assert first_run.choose_model("groq", ["qwen/qwen3-32b", "openai/gpt-oss-120b"]) == "openai/gpt-oss-120b"
    assert first_run.choose_model("nvidia", ["z/thing-instruct", "a/base"]) == "z/thing-instruct"
    assert first_run.choose_model("groq", []) == llm_providers.get("groq")["default_model"]


def test_ollama_must_be_running_with_a_model(monkeypatch):
    monkeypatch.setattr(llm_providers, "list_models", lambda *a, **k: (None, "unreachable"))
    assert "can't find Ollama" in first_run.use_ollama()["message"]
    monkeypatch.setattr(llm_providers, "list_models", lambda *a, **k: ([], ""))
    assert "no models yet" in first_run.use_ollama()["message"]
    monkeypatch.setattr(llm_providers, "list_models", lambda *a, **k: (["llama3.2"], ""))
    monkeypatch.setattr(llm_providers, "test", lambda *a, **k: {"ok": True})
    out = first_run.use_ollama()
    assert out["ok"] and get_llm_config()["provider"] == "ollama"


def test_the_name_is_kept_and_freehand_uses_it():
    from core.agent_config import build_system_prompt
    assert first_run.set_name("  Tracy   Smith ") == "Tracy Smith"
    assert first_run.status()["name"] == "Tracy Smith"
    assert "The person's name is Tracy Smith." in build_system_prompt().splitlines()[:4]


def test_nvidia_is_a_known_service():
    p = llm_providers.get("nvidia")
    assert p["base_url"] == "https://integrate.api.nvidia.com/v1" and p["key_prefixes"] == ["nvapi-"]
    assert any("phone" in step for step in p["steps"])


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr("server.get_or_create_api_key", lambda: "k")
    c = TestClient(server.app)
    c.headers.update({"X-API-Key": "k"})
    return c


def test_the_setup_endpoints(client, services):
    s = client.get("/api/setup").json()
    assert s["needed"] is True and set(s["services"]) == {"openrouter", "nvidia", "groq", "google"}
    assert client.post("/api/setup/name", json={"name": "Thandi"}).json() == {"name": "Thandi"}
    assert client.post("/api/setup/key", json={}).status_code == 400
    r = client.post("/api/setup/key", json={"key": "gsk_secret123", "role": "main"})
    assert r.json()["ok"] is True and "gsk_secret123" not in r.text  # the key is never sent back
    assert client.post("/api/setup/done").json() == {"done": True}
    assert client.get("/api/setup").json()["done"] is True


def test_the_free_choice_is_remembered_through_the_openrouter_sign_in(client):
    import server
    server._OPENROUTER_PENDING.clear()
    assert client.post("/api/llm/openrouter/start", json={"free": True}).json()["url"].startswith("https://openrouter.ai/auth?")
    assert [v["free"] for v in server._OPENROUTER_PENDING.values()] == [True]
    server._OPENROUTER_PENDING.clear()
    client.post("/api/llm/openrouter/start")  # Settings' own button: no body
    assert [v["free"] for v in server._OPENROUTER_PENDING.values()] == [False]


def test_the_page_shows_the_setup_only_when_needed(client):
    page = client.get("/").text
    welcome = page[page.index('id="welcome"') - 60:page.index('id="welcome"') + 120]
    assert "hidden" in welcome
    script = page[page.index("// ---- The first-run setup"):page.index("// ---- Was FreeHand updated")]
    assert "if (!s || !s.needed) return;" in script

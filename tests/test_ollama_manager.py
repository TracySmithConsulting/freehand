"""Models on this computer (Nov 2026): find Ollama, recommend a model that
fits this computer and can use tools, download it, and use it."""
import json

import pytest
from fastapi.testclient import TestClient

import server
from core import agent_config, ollama_manager as om


@pytest.fixture(autouse=True)
def offline_sizes(monkeypatch):
    # Use the built-in sizes; never reach Ollama's library from a test.
    monkeypatch.setattr(om, "download_size", lambda model, fallback: fallback)


LAPTOP = {"ram_gb": 15.7, "gpus": [{"name": "NVIDIA GeForce RTX 4050 Laptop GPU", "vram_gb": 6, "vendor": "nvidia"},
                                   {"name": "Intel(R) UHD Graphics", "vram_gb": 2, "vendor": "intel"}]}


def test_a_6gb_graphics_card_gets_a_4b_model_first():
    rec = om.recommend(LAPTOP)
    assert rec["models"][0]["model"] == "qwen3:4b" and rec["models"][0]["best"]
    assert rec["models"][0]["runs_on"] == "graphics card"
    assert "qwen3:8b" not in [m["model"] for m in rec["models"]]  # 5.2 GB + room won't fit in 6 GB
    assert "RTX 4050" in rec["summary"]


def test_built_in_intel_graphics_dont_count():
    rec = om.recommend({"ram_gb": 16, "gpus": [{"name": "Intel UHD", "vram_gb": 2, "vendor": "intel"}]})
    assert all(m["runs_on"] == "processor" for m in rec["models"])
    assert all(m["download_gb"] <= 3.0 for m in rec["models"])
    assert "processor" in rec["summary"]


def test_a_big_card_gets_big_models():
    rec = om.recommend({"ram_gb": 64, "gpus": [{"name": "RTX 4090", "vram_gb": 24, "vendor": "nvidia"}]})
    assert rec["models"][0]["model"] == "qwen3:32b"


def test_an_unknown_computer_asks_for_the_check():
    assert om.recommend(None) == {"known": False, "models": []}


def test_computer_info_is_read_from_the_vault():
    (agent_config.VAULT_DIR / "computer.json").write_text(json.dumps(LAPTOP), encoding="utf-8")
    assert om.computer()["ram_gb"] == 15.7


def test_status_when_ollama_is_not_running(monkeypatch):
    monkeypatch.setattr(om, "_call", lambda *a, **k: (None, None))
    s = om.status()
    assert s["running"] is False and s["models"] == []


def test_status_lists_models_and_which_can_use_tools(monkeypatch):
    def fake(method, path, body=None, timeout=0):
        if path == "/api/version":
            return 200, {"version": "0.12.0"}
        if path == "/api/tags":
            return 200, {"models": [{"name": "qwen3:4b", "size": 2_500_000_000}, {"name": "gemma3:1b", "size": 800_000_000}]}
        if path == "/api/show":
            return 200, {"capabilities": ["completion", "tools"] if body["model"] == "qwen3:4b" else ["completion"]}
        return None, None
    monkeypatch.setattr(om, "_call", fake)
    (agent_config.VAULT_DIR / "computer.json").write_text(json.dumps(LAPTOP), encoding="utf-8")
    s = om.status()
    assert s["running"] and s["version"] == "0.12.0"
    assert s["models"] == [{"model": "qwen3:4b", "size_gb": 2.5, "tools": True},
                           {"model": "gemma3:1b", "size_gb": 0.8, "tools": False}]
    assert s["recommended"]["models"][0]["downloaded"] is True


def test_only_one_download_at_a_time(monkeypatch):
    monkeypatch.setattr(om, "_pull_worker", lambda model: None)
    om._pull.update(model="", done=True)
    assert om.start_pull("qwen3:4b")["ok"]
    second = om.start_pull("llama3.2:3b")
    assert not second["ok"] and "still downloading" in second["message"]
    om._pull.update(done=True)
    assert not om.start_pull("bad name")["ok"]


def test_using_a_local_model_keeps_the_old_main_as_a_backup():
    agent_config.set_llm_config({"provider": "minimax", "model": "MiniMax-M3", "api_key": "k", "base_url": ""})
    out = om.use("qwen3:4b", "main")
    assert out["ok"] and "previous main model is now the first backup" in out["message"]
    assert agent_config.get_llm_config()["provider"] == "ollama"
    assert agent_config.get_llm_backups()[0]["model"] == "MiniMax-M3"
    assert om.use("llama3.2:3b", "backup")["ok"]
    assert agent_config.get_llm_backups()[-1]["model"] == "llama3.2:3b"


def test_the_endpoints_need_the_api_key():
    c = TestClient(server.app)
    assert c.get("/api/ollama/status").status_code == 401
    assert c.post("/api/ollama/pull", json={"model": "qwen3:4b"}).status_code == 401
    keyed = TestClient(server.app, headers={"X-API-Key": server.get_or_create_api_key()})
    assert keyed.post("/api/ollama/use", json={"model": "x", "as": "boss"}).status_code == 400


def test_ollama_on_another_port_is_found(monkeypatch):
    """This computer runs Ollama on 11435 (OLLAMA_HOST), not 11434."""
    (agent_config.VAULT_DIR / "computer.json").write_text(json.dumps({**LAPTOP, "ollama_port": 11435}), encoding="utf-8")
    assert om.base_url() == "http://host.docker.internal:11435"
    assert om.chat_base_url() == "http://host.docker.internal:11435/v1"
    assert om.use("qwen3.5:4b", "main")["ok"]
    assert agent_config.get_llm_config()["base_url"] == "http://host.docker.internal:11435/v1"


def test_the_usual_port_needs_no_address():
    (agent_config.VAULT_DIR / "computer.json").write_text(json.dumps({**LAPTOP, "ollama_port": 11434}), encoding="utf-8")
    assert om.chat_base_url() == ""


def test_ollamas_own_suggestions_come_first_and_cloud_ones_are_left_out(monkeypatch):
    monkeypatch.setattr(om, "_call", lambda m, path, body=None, timeout=0: (200, {"recommendations": [
        {"model": "glm-5.3-flash:cloud", "description": "Cloud", "required_plan": "pro"},
        {"model": "qwen3.5:4b", "description": "Compact multimodal reasoning and tool use", "vram_bytes": 6000000000},
        {"model": "gemma4:12b", "description": "Agentic workflows", "vram_bytes": 11000000000},
    ]}))
    recs = om.ollama_recommendations()
    assert [r["model"] for r in recs] == ["qwen3.5:4b", "gemma4:12b"]
    out = om.recommend(LAPTOP, recs)
    assert [m["model"] for m in out["models"]] == ["qwen3.5:4b"]  # 12b needs 11 GB, the card has 6
    assert out["models"][0]["best"] and out["models"][0]["runs_on"] == "graphics card"

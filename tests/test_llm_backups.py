"""Backup models (Nov 2026): when the main model fails, FreeHand tries the
backups in order and says so. No real model is called."""
import asyncio

import pytest
from fastapi.testclient import TestClient

import server
from core import agent, agent_config, long_memory

MAIN = {"provider": "minimax", "model": "minimax-m3", "api_key": "main-key", "base_url": ""}
GEMINI = {"provider": "google", "model": "gemini-2.5-flash", "api_key": "AIzaBackupKey", "base_url": ""}
OLLAMA = {"provider": "ollama_cloud", "model": "gpt-oss:120b", "api_key": "ollama-key", "base_url": ""}


@pytest.fixture
def models():
    agent_config.set_llm_config(MAIN)
    agent_config.add_llm_backup(GEMINI)
    agent_config.add_llm_backup(OLLAMA)
    agent._SWITCH_NOTICED.clear()
    agent.LAST_SWITCH.clear()


def fake_models(monkeypatch, outcomes):
    """outcomes: {model: (message | None, status, timed_out)}; records order."""
    tried = []

    async def fake_one(messages, tools, config, last):
        tried.append((config["model"], last))
        msg, status, timed_out = outcomes[config["model"]]
        return (msg if msg is not None else {"error": f"LLM API error {status}"}), status, timed_out

    monkeypatch.setattr(agent, "_call_one", fake_one)
    return tried


def test_the_chain_is_main_then_backups_in_order(models):
    assert [c["model"] for c in agent_config.get_llm_chain()] == ["minimax-m3", "gemini-2.5-flash", "gpt-oss:120b"]


def test_no_credit_switches_to_the_first_backup(models, monkeypatch):
    tried = fake_models(monkeypatch, {
        "minimax-m3": (None, 402, False),
        "gemini-2.5-flash": ({"role": "assistant", "content": "hi"}, 200, False),
        "gpt-oss:120b": ({"role": "assistant", "content": "unused"}, 200, False),
    })
    msg = asyncio.run(agent.call_llm([{"role": "user", "content": "hi"}]))
    assert msg["content"] == "hi"
    assert msg["_switched"]["reason"] == "it has no credit left"
    assert "Gemini" in msg["_switched"]["to"]
    assert tried == [("minimax-m3", False), ("gemini-2.5-flash", False)]


def test_an_unresponsive_main_model_is_skipped_quickly(models, monkeypatch):
    fake_models(monkeypatch, {
        "minimax-m3": (None, None, True),
        "gemini-2.5-flash": (None, 429, False),
        "gpt-oss:120b": ({"role": "assistant", "content": "from ollama"}, 200, False),
    })
    msg = asyncio.run(agent.call_llm([]))
    assert msg["content"] == "from ollama"
    assert msg["_switched"]["reason"] == "it didn't respond in time"
    assert agent.LAST_SWITCH["to"].startswith("Ollama Cloud")


def test_when_everything_fails_the_main_error_is_reported(models, monkeypatch):
    fake_models(monkeypatch, {m: (None, 503, False) for m in ("minimax-m3", "gemini-2.5-flash", "gpt-oss:120b")})
    msg = asyncio.run(agent.call_llm([]))
    assert "error" in msg and "backup models didn't answer either" in msg["error"]


def test_people_are_told_once_an_hour_and_the_note_is_not_remembered(models, monkeypatch):
    fake_models(monkeypatch, {
        "minimax-m3": (None, 402, False),
        "gemini-2.5-flash": ({"role": "assistant", "content": "Answer."}, 200, False),
        "gpt-oss:120b": (None, 500, False),
    })
    monkeypatch.setattr(long_memory, "recall", lambda q: [])
    saved = []
    monkeypatch.setattr(agent, "_remember_turn", lambda key, cmd, reply, src: saved.append(reply))
    first = asyncio.run(agent.run_agent("hello", source="web"))
    second = asyncio.run(agent.run_agent("again", source="web"))
    assert "didn't answer because it has no credit left" in first["text"]
    assert "Check your main model in Settings" in first["text"]
    assert second["text"] == "Answer."  # not repeated within the hour
    assert saved == ["Answer.", "Answer."]  # the note is shown, not saved


def test_the_switch_marker_never_goes_back_to_the_model(models, monkeypatch):
    seen = []

    async def fake_one(messages, tools, config, last):
        seen.append([dict(m) for m in messages])
        if config["model"] == "minimax-m3":
            return {"error": "x"}, 402, False
        if len(seen) <= 2:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "search_memory", "arguments": '{"query": "a"}'}}]}, 200, False
        return {"role": "assistant", "content": "done"}, 200, False

    monkeypatch.setattr(agent, "_call_one", fake_one)
    monkeypatch.setattr(long_memory, "recall", lambda q: [])
    asyncio.run(agent.run_agent("search", source="selftest"))
    assert not any("_switched" in m for msgs in seen for m in msgs)


# ── Managing backups ──────────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(server.app, headers={"X-API-Key": server.get_or_create_api_key()})


def test_the_page_never_sees_backup_keys(models, client):
    data = client.get("/api/llm/backups").json()
    assert [b["model"] for b in data["backups"]] == ["gemini-2.5-flash", "gpt-oss:120b"]
    assert data["backups"][0]["key_hint"] == "ending pKey"
    assert "AIzaBackupKey" not in str(data)


def test_reorder_promote_and_remove(models, client):
    client.post("/api/llm/backups/1/move", json={"direction": "up"})
    assert [c["model"] for c in agent_config.get_llm_chain()][1] == "gpt-oss:120b"
    client.post("/api/llm/backups/0/promote")
    assert agent_config.get_llm_config()["model"] == "gpt-oss:120b"
    assert [b["model"] for b in agent_config.get_llm_backups()] == ["minimax-m3", "gemini-2.5-flash"]
    client.delete("/api/llm/backups/1")
    assert [b["model"] for b in agent_config.get_llm_backups()] == ["minimax-m3"]


def test_a_backup_needs_a_model(client):
    assert client.post("/api/llm/backups", json={"provider": "google"}).status_code == 400


def test_backups_alone_keep_the_task_runner_going():
    agent_config.add_llm_backup(GEMINI)
    assert agent_config.get_llm_chain()[0]["model"] == "gemini-2.5-flash"
    from core import scheduler
    assert scheduler._llm_ready()

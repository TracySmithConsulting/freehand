"""Nov 2026 memory: long-term memory through Hindsight, plus short-term
conversation history. Hindsight is never contacted here: its HTTP layer
(long_memory._call) is replaced with a fake."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core import conversation, long_memory


@pytest.fixture
def hs(monkeypatch):
    calls = []
    replies = {}

    def fake_call(method, path, body=None, timeout=10.0):
        # Replies are keyed by the last path segment: "recall", "list", ...
        calls.append((method, path, body))
        last = path.split("?")[0].rsplit("/", 1)[-1]
        return replies.get(last, (200, {}))

    monkeypatch.setattr(long_memory, "_call", fake_call)
    long_memory._bank_ready.clear()
    return calls, replies


def test_recall_returns_clean_unique_lines(hs, monkeypatch):
    calls, replies = hs
    replies["recall"] = (200, {"results": [
        {"text": "Anna lives in Durban. | Involving: Anna"},
        {"text": "anna lives in durban."},
        {"text": "The user prefers short answers."},
    ]})
    assert long_memory.recall("where does my sister live") == [
        "Anna lives in Durban.", "The user prefers short answers."]
    assert calls[0][0] == "PUT"  # the bank is created on first use


def test_recall_is_quiet_when_hindsight_is_down(monkeypatch):
    monkeypatch.setattr(long_memory, "_call", lambda *a, **k: (None, None))
    long_memory._bank_ready.clear()
    assert long_memory.recall("anything") == []
    assert "isn't reachable" in long_memory.retain("I like tea")["error"]


@pytest.mark.parametrize("text", [
    "my password is hunter2",
    "use key sk-abcdefghijklmnopqrstuvwx",
    "token: ghp_abcdefghijklmnopqrstuvwxyz123",
])
def test_secrets_are_never_saved(hs, text):
    calls, _ = hs
    assert "password or key" in long_memory.retain(text)["error"]
    assert not any(c[0] == "POST" for c in calls)


def test_retain_runs_in_the_background_by_default(hs):
    calls, _ = hs
    assert long_memory.retain("I like tea", "chat via web") == {"saved": True}
    post = [c for c in calls if c[0] == "POST"][0]
    assert post[2]["async"] is True
    assert post[2]["items"][0]["context"] == "chat via web"


def test_forget_is_a_soft_retire(hs):
    calls, _ = hs
    assert long_memory.forget_fact("m1") == {"forgotten": True}
    method, path, body = calls[-1]
    assert method == "PATCH" and path.endswith("/memories/m1")
    assert body["state"] == "invalidated"


def test_list_only_shows_editable_facts(hs):
    calls, replies = hs
    replies["list"] = (200, {"total": 1, "items": [
        {"id": "a", "text": "Anna lives in Durban. | When: x", "mentioned_at": "2026-11-13T10:00:00"}]})
    out = long_memory.list_facts()
    assert out["items"][0]["text"] == "Anna lives in Durban."
    listed = [c[1] for c in calls if "/memories/list" in c[1]]
    assert any("type=world" in p for p in listed) and any("type=experience" in p for p in listed)
    assert not any("observation" in p for p in listed)


# ── Short-term conversation history ────────────────────────────────────

def test_history_is_per_conversation():
    a = conversation.conversation_key("whatsapp", "+27000000001")
    b = conversation.conversation_key("whatsapp", "+27000000002")
    conversation.add_turn(a, "What's on today?", "A dentist visit at 10.")
    assert [m["content"] for m in conversation.history(a)] == ["What's on today?", "A dentist visit at 10."]
    assert conversation.history(b) == []


def test_history_keeps_only_recent_turns():
    key = conversation.conversation_key("web", "")
    for i in range(conversation.MAX_TURNS + 3):
        conversation.add_turn(key, f"q{i}", f"a{i}")
    msgs = conversation.history(key)
    assert len(msgs) == conversation.MAX_TURNS * 2
    assert msgs[-1]["content"] == f"a{conversation.MAX_TURNS + 2}"


def test_history_resets_after_a_long_gap(monkeypatch):
    key = conversation.conversation_key("telegram", "42")
    conversation.add_turn(key, "hi", "hello")
    later = datetime.now(timezone.utc) + timedelta(hours=conversation.IDLE_HOURS + 1)
    monkeypatch.setattr(conversation, "_now", lambda: later)
    assert conversation.history(key) == []


# ── The agent loop uses both ────────────────────────────────────────────

def test_agent_sends_memories_and_history_then_saves_the_turn(monkeypatch):
    from core import agent

    seen = {}

    async def fake_llm(messages, tools=None, config=None):
        seen["messages"] = messages
        return {"role": "assistant", "content": "It's on 4 March."}

    saved = []
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    monkeypatch.setattr(long_memory, "recall", lambda q: ["Anna's birthday is 4 March."])
    monkeypatch.setattr(long_memory, "retain", lambda text, context="", wait=False: saved.append((text, context)))
    key = conversation.conversation_key("web", "")
    conversation.add_turn(key, "Who is Anna?", "Your sister.")

    async def run():
        out = await agent.run_agent("When is her birthday?", source="web")
        await asyncio.gather(*list(agent._background_tasks))
        return out

    out = asyncio.run(run())
    assert out["text"] == "It's on 4 March."
    system, *rest = seen["messages"]
    assert "Anna's birthday is 4 March." in system["content"]
    assert [m["content"] for m in rest] == ["Who is Anna?", "Your sister.", "When is her birthday?"]
    assert saved and "When is her birthday?" in saved[0][0]
    assert conversation.history(key)[-1]["content"] == "It's on 4 March."


def test_agent_does_not_save_turns_with_secrets(monkeypatch):
    from core import agent

    async def fake_llm(messages, tools=None, config=None):
        return {"role": "assistant", "content": "Saved."}

    saved = []
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    monkeypatch.setattr(long_memory, "recall", lambda q: [])
    monkeypatch.setattr(long_memory, "retain", lambda *a, **k: saved.append(a))
    asyncio.run(agent.run_agent("my password is hunter2", source="web"))
    assert saved == []


def test_model_reasoning_is_not_shown():
    from core.agent import strip_reasoning
    assert strip_reasoning("<think>The memory says pelican.</think>  pelican") == "pelican"
    assert strip_reasoning("<think>unfinished thought") == ""
    assert strip_reasoning("Plain answer") == "Plain answer"


def test_the_same_fact_saved_twice_is_listed_once(hs):
    _, replies = hs
    replies["list"] = (200, {"items": [
        {"id": "a", "text": "The user has 3 guide dogs.", "mentioned_at": "2026-11-13T11:00"},
        {"id": "b", "text": "The user has 3 guide dogs. | Involving: user", "mentioned_at": "2026-11-13T11:05"},
    ]})
    out = long_memory.list_facts()
    dogs = [i for i in out["items"] if "guide dogs" in i["text"]]
    assert len(dogs) == 1 and sorted(dogs[0]["ids"]) == ["a", "b"]


def test_forget_in_chat_retires_every_copy(hs):
    calls, replies = hs
    replies["recall"] = (200, {"results": [
        {"id": "a", "text": "The user has 3 guide dogs."},
        {"id": "b", "text": "The user has 3 guide dogs. | Involving: user"},
        {"id": "c", "text": "The user lives in Pretoria."},
    ]})
    out = long_memory.forget_matching("my guide dogs")
    assert out["forgotten"] and out["text"] == "The user has 3 guide dogs."
    retired = [c[1].rsplit("/", 1)[-1] for c in calls if c[0] == "PATCH"]
    assert retired == ["a", "b"]


def test_an_answer_comes_even_when_every_turn_used_a_tool(monkeypatch):
    """FreeHand searched 5 times and never replied (empty message). It now
    asks the model once more, without tools, for an answer."""
    from core import agent
    calls = []

    async def fake_llm(messages, tools=None, config=None):
        calls.append(tools)
        if tools:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"t{len(calls)}", "type": "function",
                 "function": {"name": "search_memory", "arguments": f'{{"query": "q{len(calls)}"}}'}}]}
        return {"role": "assistant", "content": "Your notes say the build passed."}

    monkeypatch.setattr(agent, "call_llm", fake_llm)
    monkeypatch.setattr(long_memory, "recall", lambda q: [])
    monkeypatch.setattr(long_memory, "retain", lambda *a, **k: None)
    out = asyncio.run(agent.run_agent("What did I write about the build?", max_turns=3, source="selftest"))
    assert out["text"] == "Your notes say the build passed."
    assert calls[-1] is None  # the last call offered no tools


def test_only_the_tool_call_that_ran_is_kept_in_the_history(monkeypatch):
    """MiniMax asked for two searches at once; FreeHand ran one, and the
    unanswered second made MiniMax reject the next request."""
    from core import agent
    seen = []

    async def fake_llm(messages, tools=None, config=None):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "search_memory", "arguments": '{"query": "x"}'}},
                {"id": "b", "type": "function", "function": {"name": "search_memory", "arguments": '{"query": "y"}'}}]}
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(agent, "call_llm", fake_llm)
    monkeypatch.setattr(long_memory, "recall", lambda q: [])
    asyncio.run(agent.run_agent("search", source="selftest"))
    assistant = [m for m in seen[1] if m.get("role") == "assistant" and m.get("tool_calls")][0]
    assert [c["id"] for c in assistant["tool_calls"]] == ["a"]
    assert [m["tool_call_id"] for m in seen[1] if m.get("role") == "tool"] == ["a"]


# ── Choosing the memory bank (Nov 2026) ──────────────────────────────

def test_banks_are_listed_with_counts_and_the_current_one_first(monkeypatch):
    from core import long_memory
    monkeypatch.setattr(long_memory, "bank_id", lambda: "freehand")
    monkeypatch.setattr(long_memory, "_call", lambda m, path, body=None, timeout=0: (200, {"banks": [
        {"bank_id": "tracy-hermes", "fact_count": 133, "last_write_at": "2026-11-14"}]}))
    out = long_memory.list_banks()
    assert out["current"] == "freehand"
    assert out["banks"][0] == {"bank_id": "freehand", "facts": 0, "last_write": "", "new": True}
    assert out["banks"][1]["bank_id"] == "tracy-hermes" and out["banks"][1]["facts"] == 133


def test_choosing_a_bank_saves_it_and_checks_the_name():
    from core import agent_config, long_memory
    assert not long_memory.choose_bank("../etc")["ok"]
    assert not long_memory.choose_bank("")["ok"]
    out = long_memory.choose_bank("freehand-tracy")
    assert out["ok"] and "still there" in out["message"]
    assert agent_config._load_settings()["memory"]["bank_id"] == "freehand-tracy"
    assert long_memory.bank_id() == "freehand-tracy"


def test_setup_offers_a_new_bank_by_default():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("setup_env", pathlib.Path(__file__).parent.parent / "scripts" / "setup_env.py")
    setup_env = importlib.util.module_from_spec(spec); spec.loader.exec_module(setup_env)
    banks = [{"bank_id": "tracy-hermes", "facts": 133}, {"bank_id": "freehand", "facts": 2}]
    answers = iter(["", ""])  # Enter, Enter: the recommended choices
    out = setup_env.memory_choice(banks, ask=lambda prompt: next(answers), say=lambda *a: None)
    assert out == {"FREEHAND_OWN_MEMORY": "0", "FREEHAND_HINDSIGHT_URL": "http://host.docker.internal:8888",
                   "FREEHAND_MEMORY_BANK": "freehand-2"}
    answers = iter(["2", "1"])
    assert setup_env.memory_choice(banks, ask=lambda p: next(answers), say=lambda *a: None)["FREEHAND_MEMORY_BANK"] == "tracy-hermes"
    answers = iter(["3"])
    own = setup_env.memory_choice(banks, ask=lambda p: next(answers), say=lambda *a: None)
    assert own == {"FREEHAND_OWN_MEMORY": "1", "HINDSIGHT_PORT": "8890", "HINDSIGHT_CONSOLE_PORT": "9990"}

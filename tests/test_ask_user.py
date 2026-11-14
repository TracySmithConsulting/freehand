"""Questions with choices (Nov 2026): the agent's ask_user tool ends the
turn with questions, shown as choices in the console and as a numbered
list in chat apps."""
import asyncio
import json

from core import agent, ask_user


def call(args, id="q1"):
    return {"content": "", "tool_calls": [{"id": id, "type": "function",
                                            "function": {"name": "ask_user", "arguments": json.dumps(args)}}]}


TWO = {"intro": "A couple of things first.", "questions": [
    {"question": "Who should it go to?", "options": ["Just me", "The whole team"]},
    {"question": "Which days?", "options": ["Monday", "Wednesday", "Friday"], "multiple": True},
]}


def test_questions_end_the_turn_and_come_back_as_choices(monkeypatch):
    seen = []

    async def fake_llm(messages, tools=None, config=None):
        seen.append([t["function"]["name"] for t in tools or []])
        return call(TWO)
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    out = asyncio.run(agent.run_agent("set up a weekly reminder", source="selftest"))
    assert len(seen) == 1 and "ask_user" in seen[0]
    assert out["questions"]["questions"][1]["multiple"] is True
    assert out["text"].startswith("A couple of things first.")
    assert "1. Who should it go to? (choose one)" in out["text"]
    assert "   c) Friday" in out["text"]


def test_the_plain_text_reads_well_in_a_chat_app():
    text = ask_user.as_text(ask_user.clean({"questions": [{"question": "Tea or coffee?", "options": ["Tea", "Coffee"]}]}))
    assert text == "Tea or coffee? (choose one)\n   a) Tea\n   b) Coffee\nReply with your choices, for example a, or in your own words."


def test_small_model_mistakes_are_forgiven():
    # One question instead of a list, options as one string, duplicates.
    out = ask_user.clean({"question": "Which?", "options": "Red, Blue; red"})
    assert out["questions"] == [{"question": "Which?", "options": ["Red", "Blue"], "multiple": False}]
    assert ask_user.clean({"questions": [{"question": "Only one?", "options": ["Yes"]}]}) is None
    assert len(ask_user.clean({"questions": [{"question": f"Q{i}?", "options": ["a", "b"]} for i in range(9)]})["questions"]) == 4


def test_a_bad_ask_goes_back_to_the_model(monkeypatch):
    replies = iter([call({"questions": []}), {"content": "Which colour would you like?"}])

    async def fake_llm(messages, tools=None, config=None):
        return dict(next(replies))
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    out = asyncio.run(agent.run_agent("paint it", source="selftest"))
    assert out["text"] == "Which colour would you like?" and "questions" not in out


def test_the_questions_go_into_the_chat_history(monkeypatch):
    async def fake_llm(messages, tools=None, config=None):
        return call(TWO)
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    remembered = []
    monkeypatch.setattr(agent, "_remember_turn", lambda key, cmd, reply, src: remembered.append(reply))
    asyncio.run(agent.run_agent("set up a weekly reminder", source="selftest"))
    assert "Which days?" in remembered[0]


def test_the_console_shows_choices():
    html = open("freehand/static/index.html", encoding="utf-8").read()
    assert "function buildAskForm" in html and "Send my answers" in html
    assert "box.type = q.multiple ? 'checkbox' : 'radio'" in html

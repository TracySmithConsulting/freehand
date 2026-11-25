"""The command box while FreeHand works (Nov 2026): the box empties when
a message is sent, and the steps FreeHand takes are shown in plain words,
with only a few read out, so it never looks stuck (core/progress.py)."""
import asyncio
import json
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from core import agent, progress
from server import app, get_or_create_api_key

PAGE = (pathlib.Path(__file__).resolve().parent.parent / "freehand" / "static" / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("name, args, said", [
    ("document_outline", {"file": "DBSA Manual/CHP 5 AK.docx"}, "Reading CHP 5 AK.docx"),
    ("document_edit", {"file": "c.docx", "changes": []}, "Editing c.docx"),
    ("document_create", {"output": "Notes"}, "Writing Notes"),
    ("document_picture", {"prompt": "a tree", "file": "ch2.docx"}, "Making a picture for ch2.docx"),
    ("work_files", {}, "Looking in your work folder"),
    ("web_search", {"query": "DeafBlind South Africa"}, "Searching the web for “DeafBlind South Africa”"),
    ("read_emails", {}, "Checking your email"),
    ("list_calendar_events", {}, "Checking your calendar"),
    ("load_tools", {"group": "work"}, ""),
    ("mystery_tool", {}, "Using mystery tool"),
])
def test_steps_in_plain_words(name, args, said):
    assert progress.describe_tool(name, args) == said


def test_steps_are_kept_for_the_person_who_asked():
    token = progress.start("run-abc-123", "owner")
    progress.say("Thinking about your message")
    progress.say("Thinking about your message")  # said once, not twice in a row
    progress.say("Reading c.docx")
    assert progress.get("run-abc-123", "owner") == {
        "steps": ["Thinking about your message", "Reading c.docx"], "done": False, "known": True}
    assert progress.get("run-abc-123", "someone-else")["steps"] == []
    progress.finish(token)
    assert progress.get("run-abc-123", "owner")["done"] is True
    progress.say("After the end")  # nothing is following any more
    assert progress.get("run-abc-123", "owner")["steps"][-1] == "Reading c.docx"
    assert not progress.valid("x") and not progress.valid("../../etc") and progress.valid("run-abc-123")


def test_the_agent_says_each_step(monkeypatch):
    calls = []

    async def model(messages, tools=None, config=None):
        calls.append(1)
        if len(calls) == 1:
            return {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "web_search", "arguments": json.dumps({"query": "tactile cues"})}}], "_usage": {}}
        return {"content": "Here you go.", "_usage": {}}
    monkeypatch.setattr(agent, "call_llm", model)

    async def search(name, args):
        return {"content": json.dumps({"results": []})}
    monkeypatch.setattr(agent, "_run_tool", search)
    token = progress.start("agent-steps-1", "owner")
    try:
        asyncio.run(agent.run_agent("look up tactile cues"))
    finally:
        progress.finish(token)
    assert progress.get("agent-steps-1", "owner")["steps"] == [
        "Thinking about your message", "Searching the web for “tactile cues”"]


def test_the_console_can_follow_a_message(monkeypatch):
    async def model(messages, tools=None, config=None):
        return {"content": "Hello.", "_usage": {}}
    monkeypatch.setattr(agent, "call_llm", model)
    client = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
    r = client.post("/api/agent/command", json={"command": "hi", "progress_id": "console-run-0001"})
    assert r.status_code == 200 and r.json()["text"] == "Hello."
    got = client.get("/api/agent/progress/console-run-0001").json()
    assert got["done"] is True and got["steps"] == ["Thinking about your message"]
    assert client.get("/api/agent/progress/bad id!").status_code in (400, 404)
    assert TestClient(app).get("/api/agent/progress/console-run-0001").status_code in (401, 403)


def test_the_command_box_empties_and_shows_progress():
    submit = PAGE[PAGE.index("var commandForm = document.getElementById('command-form');"):
                  PAGE.index("// ---- The camera coach")]
    assert "'Sending: ' + value" not in submit
    assert submit.index("input.value = '';") < submit.index("apiFetch('/api/agent/command'")
    assert "progress_id: work.id" in submit and "if (!input.value) input.value = value;" in submit
    # One quiet status line is read out; the list of steps is shown, not read out.
    assert re.search(r'<p id="work-status" class="sr-only" role="status" aria-live="polite">', PAGE)
    assert '<div id="work-progress" hidden>' in PAGE and 'id="work-steps"' in PAGE
    assert "SAY_EVERY = 10000, STILL_AFTER = 25000" in submit
    assert "if (voiceReadsReplies()) return;" in submit  # FreeHand's own voice isn't talked over
    assert "FreeHand is still working on your last message" in submit

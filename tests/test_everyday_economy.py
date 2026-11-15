"""Everyday chat costs less (Nov 2026).

An everyday message was about 6,800 tokens before the model read a word of
it: every tool (about 4,000), every skill described (about 1,000), and 15
memories. Now tools go by topic for every model, only the skills that
match are described, and 8 memories are sent. With the person's real setup
that came to about 3,400 to 4,600 tokens.
"""
import asyncio
import json

import pytest

from core import agent, skills, tool_selection
from core.agent_config import list_available_tools


def names(tools):
    return [t["function"]["name"] for t in tools]


def test_tools_go_by_topic_for_every_model():
    sent, state = tool_selection.select(list_available_tools(), ["What's on my calendar tomorrow?"], by_topic=True)
    got = names(sent)
    assert "list_calendar_events" in got
    assert "read_email" not in got and "read_docx" not in got and "post_to_facebook" not in got
    assert got[-1] == "load_tools"  # the rest can still be loaded mid-answer
    assert {"email", "documents", "facebook"} <= set(state["by_group"]) - state["loaded"]


def test_all_tools_can_be_switched_back_on():
    assert tool_selection.tools_by_topic({})
    assert not tool_selection.tools_by_topic({"all_tools": True})
    assert not tool_selection.tools_by_topic({"all_tools": "on"})


def test_a_search_doesnt_bring_the_form_filling_tools():
    sent, _ = tool_selection.select(list_available_tools(), ["Search for the latest news"], by_topic=True)
    got = names(sent)
    assert "web_search" in got and "extract_text" in got
    assert "click" not in got and "fill" not in got


def test_rarely_needed_tools_come_when_the_words_call_for_them():
    plain, _ = tool_selection.select(list_available_tools(), ["Thanks, that's great"], by_topic=True)
    assert "forget_memory" not in names(plain) and "save_skill" not in names(plain)
    forget, _ = tool_selection.select(list_available_tools(), ["That's wrong, forget it"], by_topic=True)
    assert "forget_memory" in names(forget)


def test_always_sent_tools_stay_small():
    sent, _ = tool_selection.select(list_available_tools(), ["Thanks, that's great"], by_topic=True)
    assert len(json.dumps(sent)) // 4 < 1300, "the tools sent with every message have grown again"


@pytest.fixture
def seen(monkeypatch):
    calls = []
    replies = []

    async def fake_llm(messages, tools=None, config=None):
        calls.append({"messages": messages, "tools": names(tools or [])})
        return replies.pop(0) if replies else {"content": "ok"}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    return calls, replies


def test_the_agent_sends_tools_by_topic(seen, monkeypatch):
    calls, _ = seen
    asyncio.run(agent.run_agent("Thanks, that's great", source="selftest"))
    assert "read_docx" not in calls[0]["tools"]
    monkeypatch.setattr("core.agent_config.get_llm_config", lambda: {"all_tools": True})
    asyncio.run(agent.run_agent("Thanks, that's great", source="selftest"))
    assert "read_docx" in calls[1]["tools"]


def test_using_a_skill_brings_the_tool_to_say_how_it_went(seen):
    calls, replies = seen
    replies += [{"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
        "name": "use_skill", "arguments": json.dumps({"name": "anything"})}}]}, {"content": "Done."}]
    asyncio.run(agent.run_agent("Do the weekly report the usual way", source="selftest"))
    assert "skill_outcome" not in calls[0]["tools"]
    assert "skill_outcome" in calls[1]["tools"]


def test_eight_memories_not_fifteen(seen, monkeypatch):
    calls, _ = seen
    monkeypatch.setattr(agent.long_memory, "recall", lambda q: [f"memory {i}" for i in range(20)])
    asyncio.run(agent.run_agent("hello", source="selftest"))
    system = calls[0]["messages"][0]["content"]
    assert "- memory 7" in system and "- memory 8" not in system


def test_only_matching_skills_are_described(monkeypatch):
    found = [
        {"name": "accessible-document-handling", "status": "draft",
         "description": "Make a Word document accessible for screen readers such as NVDA."},
        {"name": "gradio-migration", "status": "draft", "description": "Move Gradio 4 chatbots to Gradio 6."},
        {"name": "weekly-report", "status": "active", "description": "Write the weekly report."},
    ]
    monkeypatch.setattr(skills, "scan", lambda: found)
    text = skills.prompt_index("Help me make this Word document accessible for NVDA")
    assert "accessible-document-handling (draft, untested): Make a Word document" in text
    assert "Gradio" not in text and "gradio-migration (draft)" in text  # by name only
    assert "weekly-report" in text
    # With no message to go by, all of them are described, as before.
    assert "Move Gradio 4" in skills.prompt_index()


def test_email_results_are_trimmed_to_what_matters():
    from core.tools.integrations import slim_emails
    outlook = {"messages": [{"id": "a", "subject": "Invoice", "bodyPreview": "Please pay " + "x" * 900,
                             "receivedDateTime": "2026-11-15T08:00:00Z", "isRead": False, "hasAttachments": True,
                             "from": {"emailAddress": {"name": "Sam", "address": "sam@x.com"}},
                             "body": {"content": "<html>" + "y" * 50000 + "</html>"}, "webLink": "https://..."}]}
    slim = slim_emails("microsoft", outlook)
    assert slim == {"count": 1, "emails": [{"id": "a", "from": "Sam <sam@x.com>", "subject": "Invoice",
                                            "date": "2026-11-15T08:00:00Z", "unread": True,
                                            "preview": ("Please pay " + "x" * 900)[:300], "attachments": True}]}
    gmail = {"messages": [{"messageId": "1", "sender": "FNB", "subject": "Statement", "snippet": "Ready",
                           "messageTimestamp": "1", "labelIds": ["INBOX", "UNREAD"]}]}
    assert slim_emails("google", gmail)["emails"][0]["unread"] is True
    assert slim_emails("google", {"error": "Not connected"}) == {"error": "Not connected"}

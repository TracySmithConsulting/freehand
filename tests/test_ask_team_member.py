"""FreeHand hands work to team members (build step 6 of
docs/design/scheduled-jobs-and-team-members.md).

Only the main FreeHand hands out work, and only to team members who aren't
paused. A team member does it with its own setup and reports back; it can't
hand work on, so it only ever goes one level down.
"""
import asyncio
import json

import pytest

from core import agent, team

THANDI = {"name": "Thandi", "job": "Drafts our social media posts.", "tools": ["facebook"], "model": "cheap"}


def _call(name, args, id="c1"):
    return {"content": "", "tool_calls": [{"id": id, "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(args)}}]}


@pytest.fixture
def llm(monkeypatch):
    """A pretend model. FreeHand's replies and the team member's are told
    apart by their instructions."""
    calls = []
    script = {"freehand": [], "member": []}

    async def fake_llm(messages, tools=None, config=None):
        who = "member" if messages[0]["content"].startswith("You are Thandi") else "freehand"
        calls.append({"who": who, "messages": messages, "config": config,
                      "tools": [t["function"]["name"] for t in tools or []]})
        reply = script[who].pop(0) if script[who] else {"content": f"({who} reply)"}
        return {**reply, "_usage": {"prompt_tokens": 100, "completion_tokens": 10}}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    monkeypatch.setattr("core.agent_config.get_llm_backups", lambda: [{"provider": "ollama", "model": "qwen"}])
    return calls, script


def test_no_team_no_tool(llm):
    calls, _ = llm
    asyncio.run(agent.run_agent("Draft a post about our specials", source="selftest"))
    assert "ask_team_member" not in calls[0]["tools"]


def test_freehand_is_told_who_does_what():
    team.save(dict(THANDI))
    schema = team.ask_schema(team.all_members())
    assert schema["function"]["parameters"]["properties"]["name"]["enum"] == ["Thandi"]
    assert "Thandi: Drafts our social media posts." in schema["function"]["description"]
    team.save({**THANDI, "paused": True}, "thandi")
    assert team.ask_schema(team.all_members()) is None  # paused: not offered


def test_freehand_hands_the_work_on_and_tells_the_person(llm):
    calls, script = llm
    thandi = team.save(dict(THANDI))
    script["freehand"] += [_call("ask_team_member", {"name": "Thandi", "request": "Draft a post about mince pies"}),
                           {"content": "Thandi drafted this for you: Fresh mince pies are here!"}]
    script["member"].append({"content": "Fresh mince pies are here!"})
    out = asyncio.run(agent.run_agent("Can someone draft a post about our mince pies?", source="selftest"))
    assert out["text"] == "Thandi drafted this for you: Fresh mince pies are here!"
    assert [c["who"] for c in calls] == ["freehand", "member", "freehand"]
    member_call = calls[1]
    # Just the request, with its own setup and model, and nothing to hand on or ask.
    assert [m["role"] for m in member_call["messages"]] == ["system", "user"]
    assert member_call["messages"][1]["content"] == "Draft a post about mince pies"
    assert "FreeHand handed you this request" in member_call["messages"][0]["content"]
    assert member_call["config"] == {"provider": "ollama", "model": "qwen"}
    assert "ask_team_member" not in member_call["tools"] and "ask_user" not in member_call["tools"]
    # What came back to FreeHand, and what it all cost.
    back = json.loads(calls[2]["messages"][-1]["content"])
    assert back == {"team_member": "Thandi", "reply": "Fresh mince pies are here!", "tools_they_used": []}
    assert out["usage"]["calls"] == 3 and out["usage"]["prompt_tokens"] == 300
    # Counted and logged under the team member's name.
    assert team.usage(thandi)["runs"] == 1
    assert team.recent(thandi)[0]["asked"] == "Draft a post about mince pies"


def test_a_team_member_cant_hand_work_on(llm):
    calls, _ = llm
    team.save(dict(THANDI))
    team.save({"name": "Dev", "job": "Watches GitHub.", "tools": ["github"]})
    asyncio.run(agent.run_agent("Thandi, ask Dev what's new", source="selftest"))
    assert calls[0]["who"] == "member" and "ask_team_member" not in calls[0]["tools"]


def test_an_unknown_or_paused_team_member_is_refused(llm):
    calls, script = llm
    team.save(dict(THANDI))
    team.save({"name": "Dev", "job": "Watches GitHub.", "tools": ["github"], "paused": True})
    script["freehand"] += [_call("ask_team_member", {"name": "Dev", "request": "What's new?"}),
                           {"content": "Dev is paused."}]
    asyncio.run(agent.run_agent("What's new on GitHub?", source="selftest"))
    assert [c["who"] for c in calls] == ["freehand", "freehand"]
    assert "isn't one of the person's team members" in calls[1]["messages"][-1]["content"]


def test_scheduled_jobs_dont_hand_work_out(llm):
    calls, _ = llm
    team.save(dict(THANDI))
    asyncio.run(agent.run_agent("Summarise the news", source="cli", job=True))
    assert "ask_team_member" not in calls[0]["tools"]

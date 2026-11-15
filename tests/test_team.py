"""Team members (build step 5 of docs/design/scheduled-jobs-and-team-members.md).

A team member has its own name, job, instructions, tools, model and limits.
It works inside FreeHand's safety rules, asks before posting unless the
person said it may just do it, and is logged and counted under its name.
"""
import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from core import agent, schedule_job, team
from core.security import PermissionTier, set_tier

THANDI = {"name": "Thandi", "job": "Drafts our social media posts.", "instructions": "Friendly tone, no hashtags.",
          "tools": ["facebook", "web"]}


@pytest.fixture
def thandi():
    return team.save(dict(THANDI))


# ── Saving one ────────────────────────────────────────────────────────

def test_a_team_member_is_saved_with_sensible_defaults(thandi):
    assert thandi["id"] == "thandi" and thandi["steps"] == 8 and thandi["monthly_limit"] == 100
    assert thandi["auto_tools"] == [] and thandi["memories"] is False
    assert team.get("THANDI")["job"] == "Drafts our social media posts."
    assert (team.TEAM_DIR / "thandi.json").exists()


@pytest.mark.parametrize("data, words", [
    ({"job": "x"}, "Give the team member a name"),
    ({"name": "<script>", "job": "x"}, "Give the team member a name"),
    ({"name": "Sam"}, "what this team member's job is"),
    ({"name": "Sam", "job": "x", "steps": 99}, "from 1 to 20"),
    ({"name": "Sam", "job": "x", "monthly_limit": "lots"}, "whole numbers"),
])
def test_mistakes_get_plain_answers(data, words):
    with pytest.raises(team.TeamError, match=words):
        team.save(data)


def test_two_team_members_cant_share_a_name(thandi):
    with pytest.raises(team.TeamError, match="already have a team member called Thandi"):
        team.save({"name": "thandi", "job": "Something else"})


def test_just_do_it_only_covers_tools_that_act_and_were_chosen():
    m = team.save({**THANDI, "auto_tools": ["post_to_facebook", "post_to_instagram", "extract_text"]})
    assert m["auto_tools"] == ["post_to_facebook"]
    assert team.may_just_do(m, "post_to_facebook") and not team.may_just_do(m, "post_to_instagram")


def test_unknown_tool_choices_are_dropped():
    assert team.save({**THANDI, "tools": ["facebook", "everything"]})["tools"] == ["facebook"]


def test_templates_use_only_real_tools():
    from core.agent_config import TOOL_REGISTRY
    for key, tpl in team.TEMPLATES.items():
        for choice in tpl["tools"]:
            assert choice in team.TOOL_CHOICES, (key, choice)
    for choice in team.TOOL_CHOICES.values():
        for tool in choice["tools"]:
            assert tool in TOOL_REGISTRY, tool


# ── Talking to one ────────────────────────────────────────────────────

def test_a_message_starting_with_the_name_goes_to_them(thandi):
    assert team.addressed("Thandi, draft a post")[1] == "draft a post"
    assert team.addressed("thandi: draft a post")[0]["id"] == "thandi"
    assert team.addressed("Ask Thandi to draft a post")[1] == "draft a post"
    assert team.addressed("Hi Thandi, how are you?")[1] == "how are you?"
    assert team.addressed("Thandi said hello")[0] is None
    assert team.addressed("What did Thandi do today?")[0] is None


def test_the_prompt_keeps_freehands_rules_around_the_instructions(thandi):
    text = team.prompt(thandi)
    assert text.startswith("You are Thandi, a member of the person's FreeHand team.")
    assert "Friendly tone, no hashtags." in text
    rules_at = text.index("These rules come from FreeHand")
    assert rules_at > text.index("Friendly tone")  # the rules come after, so they have the last word
    assert "isn't watching" in team.prompt(thandi, scheduled=True)


def test_cheaper_model_is_the_first_backup(monkeypatch):
    backups = [{"provider": "openrouter", "model": "cheap-one"}, {"provider": "ollama", "model": "qwen"}]
    monkeypatch.setattr("core.agent_config.get_llm_backups", lambda: backups)
    assert team.model_config({"model": "cheap"}) == backups[0]
    assert team.model_config({"model": "ollama:qwen"}) == backups[1]
    assert team.model_config({"model": ""}) is None
    assert team.model_config({"model": "gone"}) is None  # falls back to the main model


@pytest.fixture
def llm(monkeypatch):
    calls = []
    replies = []

    async def fake_llm(messages, tools=None, config=None):
        calls.append({"messages": messages, "tools": [t["function"]["name"] for t in tools or []], "config": config})
        reply = replies.pop(0) if replies else {"content": "Here's a draft."}
        return {**reply, "_usage": {"prompt_tokens": 100, "completion_tokens": 20}}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    return calls, replies


def _call(name, args):
    return {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                            "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_talking_to_a_team_member_uses_their_setup(thandi, llm):
    calls, _ = llm
    out = asyncio.run(agent.run_agent("Thandi, draft a post about our December specials", source="selftest"))
    assert out["text"] == "Thandi: Here's a draft." and out["team_member"] == "Thandi"
    first = calls[0]
    assert first["messages"][0]["content"].startswith("You are Thandi")
    assert first["messages"][-1]["content"] == "draft a post about our December specials"
    assert set(first["tools"]) == {"list_facebook_pages", "post_to_facebook", "web_search", "extract_text",
                                   "get_axtree", "take_screenshot", "ask_user"}
    assert team.usage(thandi)["runs"] == 1 and team.usage(thandi)["tokens"] == 120
    assert team.recent(thandi)[0]["asked"] == "draft a post about our December specials"


def test_a_team_member_cant_use_a_tool_it_wasnt_given(thandi, llm, monkeypatch):
    _, replies = llm
    replies.append(_call("read_email", {}))
    ran = []

    async def fake_run_tool(name, args):
        ran.append(name)
        return {"content": "{}"}
    monkeypatch.setattr(agent, "_run_tool", fake_run_tool)
    asyncio.run(agent.run_agent("Thandi, read my email", source="selftest"))
    assert ran == []


def test_ask_me_first_asks_even_at_full_access(thandi, llm, monkeypatch):
    set_tier(PermissionTier.FULL_ACCESS.value)
    _, replies = llm
    replies.append(_call("post_to_facebook", {"message": "Hello"}))
    ran = []

    async def fake_run_tool(name, args):
        ran.append(name)
        return {"content": "{}"}
    monkeypatch.setattr(agent, "_run_tool", fake_run_tool)
    asyncio.run(agent.run_agent("Thandi, post hello", source="selftest"))
    assert ran == []
    from core.security import get_pending_approvals
    pending = get_pending_approvals()
    assert len(pending) == 1 and pending[0]["description"].startswith("Thandi wants to: Post to Facebook")


def test_just_do_it_posts_without_asking(llm, monkeypatch):
    set_tier(PermissionTier.SEMI_AUTONOMOUS.value)
    team.save({**THANDI, "auto_tools": ["post_to_facebook"]})
    _, replies = llm
    replies.append(_call("post_to_facebook", {"message": "Hello"}))
    ran = []

    async def fake_run_tool(name, args):
        ran.append(name)
        return {"content": "{}"}
    monkeypatch.setattr(agent, "_run_tool", fake_run_tool)
    asyncio.run(agent.run_agent("Thandi, post hello", source="selftest"))
    assert ran == ["post_to_facebook"]


def test_a_paused_or_tired_team_member_doesnt_use_ai(llm):
    calls, _ = llm
    team.save({**THANDI, "paused": True})
    assert "Thandi is paused" in asyncio.run(agent.run_agent("Thandi, hi", source="selftest"))["text"]
    team.save({**THANDI, "paused": False, "monthly_limit": 0}, "thandi")
    assert "limit you set" in asyncio.run(agent.run_agent("Thandi, hi", source="selftest"))["text"]
    assert calls == []


def test_the_chosen_model_is_used(llm, monkeypatch):
    calls, _ = llm
    backup = {"provider": "ollama", "model": "qwen"}
    monkeypatch.setattr("core.agent_config.get_llm_backups", lambda: [backup])
    team.save({**THANDI, "model": "cheap"})
    asyncio.run(agent.run_agent("Thandi, hi", source="selftest"))
    assert calls[0]["config"] == backup


def test_ordinary_messages_still_go_to_freehand(thandi, llm):
    calls, _ = llm
    out = asyncio.run(agent.run_agent("What's the weather like?", source="selftest"))
    assert "team_member" not in out
    assert not calls[0]["messages"][0]["content"].startswith("You are Thandi")


# ── Scheduled jobs done by a team member ──────────────────────────────

@pytest.fixture
def db(tmp_path, monkeypatch):
    import core.database as database
    path = tmp_path / "agent.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    database.init_db()
    monkeypatch.setattr("core.scheduler._llm_ready", lambda: True)
    return path


def test_a_scheduled_job_can_be_done_by_a_team_member(thandi, db, llm):
    from core import notices, scheduler
    calls, _ = llm
    out = json.loads(schedule_job.run_tool({"type": "think", "what": "Draft Monday's post", "repeat": "weekly",
                                            "weekday": "monday", "time": "08:00", "team_member": "thandi"}))
    assert out["saved"]
    scheduler.run_due_tasks(now=datetime(2030, 1, 7, 9, 0, tzinfo=timezone.utc))
    assert calls[0]["messages"][0]["content"].startswith("You are Thandi")
    assert "isn't watching" in calls[0]["messages"][0]["content"]
    assert "ask_user" not in calls[0]["tools"]
    assert notices.unseen()[0]["text"] == "Thandi: Here's a draft."


def test_a_job_for_someone_who_isnt_on_the_team_is_refused(db):
    out = json.loads(schedule_job.run_tool({"type": "think", "what": "x", "in_minutes": 5, "team_member": "Bongani"}))
    assert out == {"saved": False, "problem": "You don't have a team member called Bongani."}


def test_a_paused_team_members_jobs_wait_and_a_deleted_ones_stop(thandi, db, llm):
    from core import notices, scheduler
    calls, _ = llm
    scheduler.create_task("Draft", "0 8 * * *", team_member="thandi")
    team.save({**thandi, "paused": True}, "thandi")
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc))
    assert calls == [] and notices.unseen() == []
    team.delete("thandi")
    for day in (2, 3, 4):
        scheduler.run_due_tasks(now=datetime(2030, 1, day, 9, 0, tzinfo=timezone.utc))
    assert "has been deleted" in notices.unseen()[0]["text"]


# ── The screen ────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr("server.get_or_create_api_key", lambda: "k")
    c = TestClient(server.app)
    c.headers.update({"X-API-Key": "k"})
    return c


def test_add_change_pause_and_delete(client):
    options = client.get("/api/team").json()
    assert {t["value"] for t in options["templates"]} == set(team.TEMPLATES)
    facebook = [t for t in options["tools"] if t["value"] == "facebook"][0]
    assert facebook["can_act"] == ["post_to_facebook"]
    made = client.post("/api/team", json=THANDI)
    assert made.status_code == 200 and made.json()["used_this_month"] == 0
    assert client.post("/api/team", json=THANDI).status_code == 400
    changed = client.put("/api/team/thandi", json={**THANDI, "job": "Posts for the shop"}).json()
    assert changed["job"] == "Posts for the shop"
    assert client.post("/api/team/thandi/pause", json={"paused": True}).json()["paused"] is True
    assert client.delete("/api/team/thandi").json() == {"status": "deleted"}
    assert client.delete("/api/team/thandi").status_code == 404
    assert client.put("/api/team/nobody", json=THANDI).status_code == 404


def test_github_projects_are_named_plainly():
    from core.tools.github_read import _repo
    assert _repo("https://github.com/TracySmithConsulting/freehand") == "TracySmithConsulting/freehand"
    assert _repo("TracySmithConsulting/freehand") == "TracySmithConsulting/freehand"
    with pytest.raises(ValueError, match="owner/name"):
        _repo("freehand")

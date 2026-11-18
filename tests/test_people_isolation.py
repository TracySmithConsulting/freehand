"""Separate users: the isolation tests (docs/design/people.md).

Written before the separation is built, as the plan says: "Write the
isolation tests first and let them fail. Then build until they pass."

Each test fills FreeHand with some of the owner's things (a chat, a job,
an approval, a memory, a note), then works as Thandi, a member, and checks
that none of the owner's things reach her: not directly, not through the
console's lists, not through work moved to another thread, and not
through her scheduled jobs.

Two kinds of test:
- "Thandi still has her own": these pass now, and must keep passing, so
  the separation can't be "built" by hiding everything from everyone.
- NOT_BUILT: these fail now. Each one is a piece of the separation still
  to build. They're strict, so when a piece is built its test passes, the
  run says so, and the marker must come off: a built piece can't quietly
  slip back. They only count as "expected" when an assertion fails, so a
  broken test setup still shows up as an error.
"""
import asyncio
import contextlib
import urllib.parse
from datetime import datetime, timezone

import pytest

from core import agent_config, conversation, long_memory, manage_jobs, people, scheduler, security


NOT_BUILT = pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="Separation not built yet (docs/design/people.md, 'Still to build')",
)

THANDI = {"id": "thandi", "name": "Thandi", "role": "member", "telegram": ["555"]}
SECRET = "OWNER-SECRET"  # in every one of the owner's things


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


def _texts(value) -> str:
    return repr(value)


@pytest.fixture
def office(tmp_path, monkeypatch):
    """A FreeHand with an owner and Thandi, and some of the owner's things."""
    s = agent_config._load_settings()
    s["people"] = [THANDI]
    agent_config._save_settings(s)
    monkeypatch.setattr(scheduler, "SCRIBBLE_PATH", tmp_path / "vault" / "00_Scribble.md")
    security.set_tier("semi_autonomous")

    with as_person("owner"):
        conversation.add_turn("web:", f"{SECRET}: my salary is 90k", "Noted.", person="owner")
        conversation.add_turn("telegram:111", f"{SECRET}: call the bank", "Will do.", person="owner")
        scheduler.create_task(f"{SECRET}: read my email every morning", "0 8 * * *")
        approval = security.intercept_action("send", f"{SECRET}: email the accountant", {"to": "acc@example.com"})
        scheduler.append_scribble(f"{SECRET}: private note")
    return {"approval_id": approval["approval_id"]}


# ── Chats ────────────────────────────────────────────────────────────

def test_thandi_still_has_her_own_chat(office):
    with as_person("thandi"):
        conversation.add_turn("telegram:555", "Remind me about Friday", "Sure.", person="thandi")
        assert "Remind me about Friday" in _texts(conversation.history("telegram:555"))


def test_thandi_never_gets_the_owners_chat(office):
    with as_person("thandi"):
        seen = conversation.history("web:") + conversation.history("telegram:111")
    assert SECRET not in _texts(seen)


def test_the_owner_doesnt_get_thandis_chat_either(office):
    with as_person("thandi"):
        conversation.add_turn("web:", "Thandi's question", "Thandi's answer", person="thandi")
    with as_person("owner"):
        seen = conversation.history("web:")
    assert "Thandi's" not in _texts(seen)
    assert SECRET in _texts(seen)  # the owner's own are still there


def test_starting_a_fresh_chat_clears_only_your_own_turns(office):
    with as_person("thandi"):
        conversation.add_turn("web:", "Thandi's question", "Thandi's answer", person="thandi")
        conversation.clear("web:")
        assert conversation.history("web:") == []
    with as_person("owner"):
        assert SECRET in _texts(conversation.history("web:"))


# ── Scheduled jobs ───────────────────────────────────────────────────

def test_thandi_still_has_her_own_jobs_and_only_hers_in_chat(office):
    with as_person("thandi"):
        scheduler.create_task("Water the plants", "0 9 * * *")
        listed = manage_jobs._rows()
    assert "Water the plants" in _texts(listed)
    assert SECRET not in _texts(listed)


def test_the_scheduled_jobs_screen_shows_thandi_only_hers(office):
    import server
    with as_person("thandi"):
        listed = asyncio.run(server.get_tasks())
    assert SECRET not in _texts(listed)


def test_the_owners_jobs_screen_shows_only_the_owners(office):
    import server
    with as_person("thandi"):
        scheduler.create_task("Water the plants", "0 9 * * *")
    with as_person("owner"):
        listed = asyncio.run(server.get_tasks())
    assert SECRET in _texts(listed)
    assert "Water the plants" not in _texts(listed)


def test_thandi_cant_pause_or_delete_the_owners_job(office):
    import server
    from fastapi import HTTPException
    with as_person("owner"):
        owners = asyncio.run(server.get_tasks())[0]["id"]
    with as_person("thandi"):
        for attempt in (lambda: server.pause_task(owners, {"paused": True}), lambda: server.delete_task(owners)):
            with pytest.raises(HTTPException) as refused:
                asyncio.run(attempt())
            assert refused.value.status_code == 404
    with as_person("owner"):
        job = [t for t in asyncio.run(server.get_tasks()) if t["id"] == owners]
    assert job and job[0]["status"] == "pending"


# ── Approvals ────────────────────────────────────────────────────────

def test_thandi_still_has_her_own_approvals(office):
    with as_person("thandi"):
        mine = security.intercept_action("send", "Email my client", {"to": "c@example.com"})
        pending = security.get_pending_approvals()
    assert mine["approval_id"] in [a["id"] for a in pending]


def test_thandi_doesnt_see_the_owners_approvals(office):
    with as_person("thandi"):
        pending = security.get_pending_approvals()
    assert SECRET not in _texts(pending)


def test_thandi_cant_answer_the_owners_approval(office):
    with as_person("thandi"):
        try:
            security.handle_approval(office["approval_id"], "approve")
        except ValueError:
            pass
    with as_person("owner"):
        still = [a["id"] for a in security.get_pending_approvals()]
    assert office["approval_id"] in still


def test_thandi_clearing_her_approvals_leaves_the_owners(office):
    with as_person("thandi"):
        security.clear_pending_approvals("tidying up")
    with as_person("owner"):
        still = [a["id"] for a in security.get_pending_approvals()]
    assert office["approval_id"] in still


def test_for_now_the_owners_console_still_answers_thandis_approvals(office):
    """Until each person signs in to the console, it's the owner's, and a
    risky request can only be approved there (people.approvals_scope)."""
    with as_person("thandi"):
        hers = security.intercept_action("send", "Email my client", {"to": "c@example.com"})["approval_id"]
    with as_person("owner"):
        assert hers in [a["id"] for a in security.get_pending_approvals()]
        assert security.handle_approval(hers, "deny")["status"] == "rejected"


# ── Memories ─────────────────────────────────────────────────────────

class FakeHindsight:
    """Enough of Hindsight's memory bank: saving with tags, recall and the
    list, filtered by tags the way Hindsight does it. Its default match,
    "any", also returns memories with no tags; the strict ones don't."""

    def __init__(self):
        self.memories = []

    def call(self, method, path, body=None, timeout=0):
        if path.endswith("/memories") and method == "POST":
            for item in body["items"]:
                self.memories.append({"id": str(len(self.memories)), "text": item["content"],
                                      "tags": list(item.get("tags") or [])})
            return 200, {}
        if path.endswith("/memories/recall"):
            found = self._matching(body.get("tags"), body.get("tags_match", "any"))
            return 200, {"results": [{"text": m["text"]} for m in found]}
        if "/memories/list" in path:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            found = self._matching(query.get("tags"), (query.get("tags_match") or ["any"])[0])
            return 200, {"items": [{"id": m["id"], "text": m["text"], "tags": m["tags"]} for m in found]}
        return 200, {}

    def _matching(self, tags, mode):
        if not tags:
            return list(self.memories)
        strict = mode in ("any_strict", "all_strict", "exact")
        return [m for m in self.memories
                if set(m["tags"]) & set(tags) or (not m["tags"] and not strict)]


@pytest.fixture
def bank(monkeypatch):
    fake = FakeHindsight()
    monkeypatch.setattr(long_memory, "enabled", lambda: True)
    monkeypatch.setattr(long_memory, "_ensure_bank", lambda: True)
    monkeypatch.setattr(long_memory, "_call", fake.call)
    with as_person("owner"):
        long_memory.retain(f"{SECRET}: the owner is planning to sell the business")
    # A memory from before people existed: no tags at all.
    fake.memories.append({"id": "old", "text": f"{SECRET}: an old untagged memory", "tags": []})
    fake.memories.append({"id": "pool", "text": "The office closes at 5", "tags": ["shared"]})
    with as_person("thandi"):
        long_memory.retain("Thandi takes rooibos, no sugar")
    return fake


@NOT_BUILT
def test_thandi_recalls_her_own_and_the_shared_pool_only(office, bank):
    with as_person("thandi"):
        recalled = long_memory.recall("anything to know?")
    assert "rooibos" in _texts(recalled)
    assert "closes at 5" in _texts(recalled)
    assert SECRET not in _texts(recalled)  # neither the owner's nor the untagged one


@NOT_BUILT
def test_the_memory_page_shows_thandi_only_hers_and_the_pool(office, bank):
    with as_person("thandi"):
        listed = long_memory.list_facts()
    assert SECRET not in _texts(listed)


# ── Files: the scribble pad ──────────────────────────────────────────

@NOT_BUILT
def test_thandi_doesnt_see_the_owners_scribble_pad(office):
    import server
    with as_person("thandi"):
        seen = scheduler._read_scribble() + _texts(asyncio.run(server.get_scribble()))
    assert SECRET not in seen


@NOT_BUILT
def test_thandis_notes_dont_land_on_the_owners_scribble_pad(office):
    with as_person("thandi"):
        scheduler.append_scribble("Thandi: buy printer paper")
    with as_person("owner"):
        assert "printer paper" not in scheduler._read_scribble()


# ── Through thread hops and scheduled jobs ───────────────────────────

def _what_thandi_can_reach() -> str:
    return _texts([conversation.history("web:"), security.get_pending_approvals(),
                   long_memory.recall("anything to know?"), scheduler._read_scribble()])


@NOT_BUILT
def test_work_moved_to_another_thread_reaches_nothing_of_the_owners(office, bank):
    from core.tools import composio_registry

    async def reach():
        return _what_thandi_can_reach()

    async def as_thandi():
        with as_person("thandi"):
            return composio_registry._run_async(reach())

    assert SECRET not in asyncio.run(as_thandi())


@NOT_BUILT
def test_thandis_scheduled_job_reaches_nothing_of_the_owners(office, bank, monkeypatch):
    seen = []

    async def fake_run_agent(command, max_turns=5, source="", caller_id="", job=False, person=None, **kw):
        seen.append(_what_thandi_can_reach())
        return {"text": "Done.", "error": None}

    monkeypatch.setattr("core.agent.run_agent", fake_run_agent)
    monkeypatch.setattr("core.scheduler._llm_ready", lambda: True)
    with as_person("thandi"):
        scheduler.create_task("Summarise my week", "")
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc))
    assert len(seen) >= 1
    assert all(SECRET not in s for s in seen)

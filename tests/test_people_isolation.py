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
SECRET = "OWNER-ONLY"  # in every one of the owner's things (not "secret": memory refuses those)


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
    vault = tmp_path / "vault"
    monkeypatch.setattr(scheduler, "SCRIBBLE_PATH", vault / "00_Scribble.md")
    monkeypatch.setattr(scheduler, "THREADS_DIR", vault / "Threads")
    monkeypatch.setattr(scheduler, "SWEEP_STATE_PATH", vault / ".sweep_state.json")
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
        memory = next((m for m in self.memories if path.split("/memories/")[-1].split("/")[0] == m["id"]), None)
        if "/memories/" in path and memory is None:
            return 404, {"detail": "Memory not found"}
        if method == "GET":
            return 200, dict(memory)
        if method == "PATCH":
            if body.get("state") == "invalidated":
                self.memories.remove(memory)
            elif "text" in body:
                memory["text"] = body["text"]
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
        assert long_memory.retain(f"{SECRET}: the owner is planning to sell the business").get("saved")
    # A memory from before people existed: no tags at all.
    fake.memories.append({"id": "old", "text": f"{SECRET}: an old untagged memory", "tags": []})
    fake.memories.append({"id": "pool", "text": "The office closes at 5", "tags": ["shared"]})
    with as_person("thandi"):
        assert long_memory.retain("Thandi takes rooibos, no sugar").get("saved")
    return fake


def test_thandi_recalls_her_own_and_the_shared_pool_only(office, bank):
    with as_person("thandi"):
        recalled = long_memory.recall("anything to know?")
    assert "rooibos" in _texts(recalled)
    assert "closes at 5" in _texts(recalled)
    assert SECRET not in _texts(recalled)  # neither the owner's nor the untagged one


def test_the_owner_still_recalls_their_old_untagged_memories_but_not_thandis(office, bank):
    with as_person("owner"):
        recalled = _texts(long_memory.recall("anything to know?"))
    assert "an old untagged memory" in recalled and "sell the business" in recalled
    assert "closes at 5" in recalled
    assert "rooibos" not in recalled


def test_thandi_cant_correct_or_forget_the_owners_memories(office, bank):
    owners = [m["id"] for m in bank.memories if SECRET in m["text"]]
    with as_person("thandi"):
        for mid in owners:
            assert long_memory.update_fact(mid, "changed by Thandi").get("error")
            assert long_memory.forget_fact(mid).get("error")
    assert [m["id"] for m in bank.memories if SECRET in m["text"]] == owners


def test_forget_that_in_thandis_chat_never_reaches_the_owners(office, bank):
    with as_person("thandi"):
        long_memory.forget_matching("the business")
    assert sum(SECRET in m["text"] for m in bank.memories) == 2


def test_thandi_can_still_correct_and_forget_her_own(office, bank):
    hers = next(m["id"] for m in bank.memories if "rooibos" in m["text"])
    with as_person("thandi"):
        assert long_memory.update_fact(hers, "Thandi takes rooibos with honey").get("updated")
        assert long_memory.forget_fact(hers).get("forgotten")
    assert not any("rooibos" in m["text"] for m in bank.memories)


def test_the_memory_page_shows_thandi_only_hers_and_the_pool(office, bank):
    with as_person("thandi"):
        listed = long_memory.list_facts()
    assert SECRET not in _texts(listed)


# ── Files: the scribble pad ──────────────────────────────────────────

def test_thandi_doesnt_see_the_owners_scribble_pad(office):
    import server
    with as_person("thandi"):
        seen = scheduler._read_scribble() + _texts(asyncio.run(server.get_scribble()))
    assert SECRET not in seen


def test_thandis_notes_dont_land_on_the_owners_scribble_pad(office):
    with as_person("thandi"):
        scheduler.append_scribble("Thandi: buy printer paper")
    with as_person("owner"):
        assert "printer paper" not in scheduler._read_scribble()


# ── Through thread hops and scheduled jobs ───────────────────────────

def _what_thandi_can_reach() -> str:
    return _texts([conversation.history("web:"), security.get_pending_approvals(),
                   long_memory.recall("anything to know?"), scheduler._read_scribble()])


def test_work_moved_to_another_thread_reaches_nothing_of_the_owners(office, bank):
    from core.tools import composio_registry

    async def reach():
        return _what_thandi_can_reach()

    async def as_thandi():
        with as_person("thandi"):
            return composio_registry._run_async(reach())

    assert SECRET not in asyncio.run(as_thandi())


def test_thandis_scheduled_job_reaches_nothing_of_the_owners(office, bank, monkeypatch):
    seen = []

    async def fake_run_agent(command, max_turns=5, source="", caller_id="", job=False, person=None, **kw):
        seen.append((command, people.current(), _what_thandi_can_reach()))
        return {"text": "Done.", "error": None}

    monkeypatch.setattr("core.agent.run_agent", fake_run_agent)
    monkeypatch.setattr("core.scheduler._llm_ready", lambda: True)
    with as_person("thandi"):
        scheduler.create_task("Summarise my week", "")
    scheduler.run_due_tasks(now=datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc))
    # The owner's own job is due too, and rightly sees the owner's things.
    hers = [(who, reached) for command, who, reached in seen if "Summarise my week" in command]
    assert [who for who, _ in hers] == ["thandi"]
    assert SECRET not in hers[0][1]


# ── Each person's own notes folder ───────────────────────────────────

def _classify_as(monkeypatch, classification):
    monkeypatch.setattr(scheduler, "_classify_entry",
                        lambda text: {"classification": classification, "topic": "garden"})


def test_thandis_pad_is_filed_into_her_own_folder(office, monkeypatch):
    _classify_as(monkeypatch, "thread")
    with as_person("thandi"):
        scheduler.append_scribble("the roses need pruning")
    scheduler.sweep_everyones_pads()
    vault = scheduler.VAULT_DIR
    hers = vault / "people" / "thandi" / "Threads" / "garden.md"
    assert "roses need pruning" in hers.read_text(encoding="utf-8")
    assert SECRET not in hers.read_text(encoding="utf-8")
    owners = (vault / "Threads" / "garden.md").read_text(encoding="utf-8")
    assert SECRET in owners and "roses" not in owners  # each swept into their own Threads
    with as_person("thandi"):
        assert "roses" not in scheduler._read_scribble()  # swept off her pad


def test_a_task_on_thandis_pad_becomes_her_task(office, monkeypatch):
    _classify_as(monkeypatch, "task")
    with as_person("thandi"):
        scheduler.append_scribble("phone the plumber")
    scheduler.sweep_everyones_pads()
    with as_person("thandi"):
        assert "phone the plumber" in scheduler.run_action({"type": "todos"}, "thandi")
    with as_person("owner"):
        assert "phone the plumber" not in scheduler.run_action({"type": "todos"}, "owner")


@pytest.fixture
def notes(monkeypatch):
    """The owner's and Thandi's notes, indexed for search."""
    from core import memory
    monkeypatch.setattr(memory, "PROJECT_ROOT", memory.VAULT_DIR.parent)
    for rel, text in [("Threads/money.md", f"# Money\n\n{SECRET}: the geyser quote is R4500\n"),
                      ("people/thandi/Threads/home.md", "# Home\n\nThandi's geyser is fixed\n")]:
        path = memory.VAULT_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    memory.sync_vault_to_sqlite()
    return memory


def test_searching_notes_finds_only_your_own(office, notes):
    with as_person("thandi"):
        hers = _texts(notes.search_memory("geyser", limit=10))
    with as_person("owner"):
        owners = _texts(notes.search_memory("geyser", limit=10))
    assert "Thandi's geyser" in hers and SECRET not in hers
    assert SECRET in owners and "Thandi's geyser" not in owners


def test_the_memory_pages_files_are_only_your_own(office, notes):
    import server
    from fastapi import HTTPException
    def names(person):
        with as_person(person):
            return sorted(row["path"].replace("\\", "/").split("/")[-1] for row in asyncio.run(server.memory_files()))

    assert "money.md" in names("owner") and "home.md" not in names("owner")
    assert names("thandi") == ["home.md"]
    with as_person("owner"):
        with pytest.raises(HTTPException) as refused:
            asyncio.run(server.memory_file(f"{notes.VAULT_DIR.name}/people/thandi/Threads/home.md"))
    assert refused.value.status_code == 404


def test_the_owner_keeps_their_files_where_they_were(office):
    assert people.workspace_parts("owner") == ()
    assert people.workspace_parts("thandi") == ("people", "thandi")
    assert people.may_read_vault_file("Threads/x.md", "owner")
    assert not people.may_read_vault_file("people/thandi/Threads/x.md", "owner")
    assert people.may_read_vault_file("people/thandi/Threads/x.md", "thandi")
    assert not people.may_read_vault_file("people/thandiwe/x.md", "thandi")
    assert not people.may_read_vault_file("Threads/x.md", "thandi")

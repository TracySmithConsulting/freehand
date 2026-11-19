"""Sharing on a FreeHand with several people (docs/design/people.md):

- "share this": a memory saved into the shared pool, which everyone
  recalls; only the person who shared it, or the owner, changes it;
- the shared folder (vault/people/shared/): everyone reads and adds
  documents there, and its notes turn up in everyone's search."""
import contextlib
import json
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from core import agent_config, long_memory, people, scheduler, sessions
from core.tools import office
from server import app, get_or_create_api_key, SESSION_HEADER


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


class FakeBank:
    def __init__(self):
        self.memories = []

    def _matching(self, tags, mode):
        strict = mode in ("any_strict", "all_strict", "exact")
        return [m for m in self.memories if set(m["tags"]) & set(tags or []) or (not m["tags"] and not strict)]

    def call(self, method, path, body=None, timeout=0):
        if path.endswith("/memories") and method == "POST":
            for item in body["items"]:
                self.memories.append({"id": str(len(self.memories) + 1), "text": item["content"],
                                      "tags": list(item["tags"])})
            return 200, {}
        if path.endswith("/memories/recall"):
            return 200, {"results": [{"id": m["id"], "text": m["text"]}
                                     for m in self._matching(body.get("tags"), body.get("tags_match"))]}
        if "/memories/list" in path:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            if q.get("type") != ["world"]:
                return 200, {"items": []}
            found = self._matching(q.get("tags"), q.get("tags_match", ["any"])[0])
            return 200, {"items": [dict(m) for m in found]}
        if path.endswith("/observations"):
            return 200, {}
        mid = path.split("/memories/")[-1].split("/")[0]
        memory = next((m for m in self.memories if m["id"] == mid), None)
        if memory is None:
            return 404, {}
        if method == "GET":
            return 200, dict(memory)
        if method == "PATCH":
            if body.get("state") == "invalidated":
                self.memories.remove(memory)
            elif "text" in body:
                memory["text"] = body["text"]
        return 200, {}


@pytest.fixture
def office_people(monkeypatch):
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", people=[{"id": "thandi", "name": "Thandi", "role": "member"},
                                         {"id": "sam", "name": "Sam", "role": "member"}])
    agent_config._save_settings(s)
    bank = FakeBank()
    monkeypatch.setattr(long_memory, "enabled", lambda: True)
    monkeypatch.setattr(long_memory, "_ensure_bank", lambda: True)
    monkeypatch.setattr(long_memory, "_call", bank.call)
    clients = {
        "owner": TestClient(app, headers={"X-API-Key": get_or_create_api_key()}),
        "thandi": TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")}),
        "sam": TestClient(app, headers={SESSION_HEADER: sessions.start("sam")}),
    }
    return bank, clients


def facts(client):
    r = client.get("/api/memory/facts")
    assert r.status_code == 200, r.text
    return {f["text"]: f for f in r.json()["items"]}


# ── "Share this" ─────────────────────────────────────────────────────

def test_a_shared_memory_is_recalled_by_everyone(office_people):
    bank, c = office_people
    assert c["thandi"].post("/api/memory/facts", json={"text": "The office closes at 5 on Fridays",
                                                       "shared": True}).status_code == 200
    assert c["thandi"].post("/api/memory/facts", json={"text": "Thandi takes rooibos"}).status_code == 200
    for who in ("owner", "sam"):
        with as_person(who):
            recalled = " ".join(long_memory.recall("when does the office close?"))
        assert "closes at 5" in recalled and "rooibos" not in recalled
    assert bank.memories[0]["tags"] == ["person:thandi", "shared"]


def test_the_memory_page_lists_the_shared_pool_apart(office_people):
    _, c = office_people
    c["thandi"].post("/api/memory/facts", json={"text": "The office closes at 5 on Fridays", "shared": True})
    c["sam"].post("/api/memory/facts", json={"text": "Sam's own note"})
    mine = facts(c["thandi"])["The office closes at 5 on Fridays"]
    assert (mine["shared"], mine["mine"], mine["can_change"]) == (True, True, True)
    theirs = facts(c["sam"])["The office closes at 5 on Fridays"]
    assert (theirs["shared"], theirs["mine"], theirs["can_change"]) == (True, False, False)
    assert facts(c["sam"])["Sam's own note"]["shared"] is False
    assert c["sam"].get("/api/memory/facts").json()["can_share"] is True


def test_only_the_sharer_or_the_owner_changes_a_shared_memory(office_people):
    bank, c = office_people
    c["thandi"].post("/api/memory/facts", json={"text": "The office closes at 5 on Fridays", "shared": True})
    mid = bank.memories[0]["id"]
    for r in (c["sam"].put(f"/api/memory/facts/{mid}", json={"text": "The office never closes"}),
              c["sam"].delete(f"/api/memory/facts/{mid}")):
        assert r.status_code == 400 and "only they or Tracy can change it" in r.json()["detail"]
    assert c["thandi"].put(f"/api/memory/facts/{mid}", json={"text": "The office closes at 4 on Fridays"}).status_code == 200
    assert c["owner"].delete(f"/api/memory/facts/{mid}").status_code == 200
    assert bank.memories == []


def test_forgetting_in_chat_cant_reach_someone_elses_shared_memory(office_people):
    bank, c = office_people
    c["thandi"].post("/api/memory/facts", json={"text": "The office closes at 5 on Fridays", "shared": True})
    with as_person("sam"):
        out = long_memory.forget_matching("when the office closes")
    assert "only they or Tracy" in out["error"]
    assert len(bank.memories) == 1


def test_share_this_in_chat_saves_into_the_pool(office_people, monkeypatch):
    bank, _ = office_people
    import asyncio
    from core import agent
    with as_person("thandi"):
        asyncio.run(agent.execute_tool("remember", {"fact": "Parking is behind the building", "shared": True}))
        asyncio.run(agent.execute_tool("remember", {"fact": "Thandi parks on the street"}))
    tags = {m["text"]: m["tags"] for m in bank.memories}
    assert tags["Parking is behind the building"] == ["person:thandi", "shared"]
    assert tags["Thandi parks on the street"] == ["person:thandi"]


def test_on_your_own_there_is_nothing_to_share_with(office_people):
    _, c = office_people
    s = agent_config._load_settings()
    s["people"] = []
    agent_config._save_settings(s)
    assert c["owner"].get("/api/memory/facts").json()["can_share"] is False


def test_when_someone_leaves_what_they_shared_stays_shared(office_people, monkeypatch):
    bank, c = office_people
    c["thandi"].post("/api/memory/facts", json={"text": "The office closes at 5 on Fridays", "shared": True})
    c["thandi"].post("/api/memory/facts", json={"text": "Thandi takes rooibos"})
    docs = {}

    def documents_call(method, path, body=None, timeout=0):
        # Hindsight documents (core/leaving.py): one per memory here.
        if "/documents?" in path:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            return 200, {"items": [{"id": m["id"], "tags": m["tags"]} for m in bank.memories
                                   if set(q["tags"]) & set(m["tags"])]}
        if "/documents/" in path:
            m = next(x for x in bank.memories if x["id"] == path.rsplit("/", 1)[1])
            if method == "DELETE":
                bank.memories.remove(m)
            else:
                m["tags"] = body["tags"]
            return 200, {}
        return bank.call(method, path, body, timeout)
    monkeypatch.setattr(long_memory, "_call", documents_call)
    c["owner"].request("DELETE", "/api/people/thandi", json={})
    assert [(m["text"], m["tags"]) for m in bank.memories] == [("The office closes at 5 on Fridays", ["shared"])]


# ── The shared folder ────────────────────────────────────────────────

def test_everyone_may_read_the_shared_folder_but_not_each_others(office_people):
    for who in ("owner", "thandi", "sam"):
        assert people.may_read_vault_file("people/shared/Price list.md", who)
    assert not people.may_read_vault_file("people/thandi/Threads/x.md", "sam")
    assert not people.may_read_vault_file("people/shared", "sam")  # the folder itself isn't a file


def test_a_member_writes_documents_in_their_folder_and_the_shared_one(office_people):
    vault = scheduler.VAULT_DIR
    with as_person("thandi"):
        assert office._check_write_permission(str(vault / "people" / "shared" / "Prices.xlsx"), ".xlsx")["allowed"]
        assert office._check_write_permission(str(vault / "people" / "thandi" / "Mine.xlsx"), ".xlsx")["allowed"]
        refused = office._check_write_permission(str(vault / "people" / "sam" / "His.xlsx"), ".xlsx")
        assert not refused["allowed"]
        assert "shared" in refused["error"]  # it says where she can write
        refused = office._check_write_permission(str(vault / "Owner.xlsx"), ".xlsx")
        assert not refused["allowed"]
    with as_person("owner"):
        assert office._check_write_permission(str(vault / "people" / "shared" / "Prices.xlsx"), ".xlsx")["allowed"]


def test_shared_notes_turn_up_in_everyones_search(office_people, monkeypatch):
    from core import memory
    monkeypatch.setattr(memory, "PROJECT_ROOT", scheduler.VAULT_DIR.parent)
    folder = scheduler.VAULT_DIR / "people" / "shared"
    folder.mkdir(parents=True)
    (folder / "Office hours.md").write_text("# Office hours\nWe close at 5 on Fridays", encoding="utf-8")
    for who in ("owner", "thandi"):
        with as_person(who):
            found = memory.search_memory("close Fridays", limit=5, sync=True)
        assert any("Office hours" in f["path"] for f in found), who


def test_nobody_can_be_called_shared(office_people):
    _, c = office_people
    pid = c["owner"].post("/api/people", json={"name": "Shared"}).json()["person"]["id"]
    assert pid == "shared-2"
    s = agent_config._load_settings()
    s["people"].append({"id": "shared", "name": "Sneaky", "role": "member"})
    agent_config._save_settings(s)
    assert people.get("shared") is None

"""When someone leaves (core/leaving.py): the owner chooses what of theirs
is kept, and everything else is deleted, so nothing is left under their
id and a newcomer can't inherit it."""
import contextlib
import json
import sqlite3
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from core import (accounts, agent_config, conversation, database, leaving, long_memory, people,
                  scheduler, security, sessions)
from server import app, get_or_create_api_key, SESSION_HEADER


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


class FakeBank:
    """Hindsight's documents: one per thing remembered, with its tags.
    Changing a document's tags or deleting it reaches its facts."""

    def __init__(self):
        self.docs = {}
        self.reachable = True

    def call(self, method, path, body=None, timeout=0):
        if not self.reachable:
            return None, None
        if path.endswith("/memories") and method == "POST":
            for item in body["items"]:
                self.docs[str(len(self.docs) + 1)] = {"text": item["content"], "tags": list(item["tags"])}
            return 200, {}
        if path.endswith("/memories/recall"):
            return 200, {"results": [{"text": d["text"]} for d in self.docs.values()
                                     if set(d["tags"]) & set(body["tags"])]}
        if "/documents?" in path:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            wanted = set(query["tags"])
            assert query["tags_match"] == ["any_strict"]
            found = [{"id": i, "tags": d["tags"]} for i, d in self.docs.items() if wanted & set(d["tags"])]
            return 200, {"items": found[:int(query["limit"][0])]}
        doc_id = urllib.parse.unquote(path.rsplit("/documents/", 1)[1])
        if method == "DELETE":
            del self.docs[doc_id]
        elif method == "PATCH":
            self.docs[doc_id]["tags"] = body["tags"]
        return 200, {}

    def whose(self, text):
        return next(d["tags"] for d in self.docs.values() if d["text"] == text)


@pytest.fixture
def bank(monkeypatch):
    fake = FakeBank()
    monkeypatch.setattr(long_memory, "enabled", lambda: True)
    monkeypatch.setattr(long_memory, "_ensure_bank", lambda: True)
    monkeypatch.setattr(long_memory, "_call", fake.call)
    return fake


class Client(TestClient):
    disconnected: list = []


@pytest.fixture
def office(bank, monkeypatch):
    """Thandi, with something of every kind, and the owner's own things."""
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", people=[{"id": "thandi", "name": "Thandi", "role": "member", "telegram": ["555"]}],
             account_owners={"oc:gmail:thandi": "thandi", "oc:gmail:info": "shared"})
    agent_config._save_settings(s)
    folder = scheduler.VAULT_DIR / "people" / "thandi"
    (folder / "Threads").mkdir(parents=True)
    (folder / "00_Scribble.md").write_text("call the plumber", encoding="utf-8")
    (folder / "Threads" / "plumber.md").write_text("# Plumber\nBooked for Tuesday", encoding="utf-8")
    (folder / ".sweep_state.json").write_text("{}", encoding="utf-8")
    with as_person("thandi"):
        scheduler.create_task("Thandi's Monday summary", "0 8 * * 1",
                              deliver_to={"channel": "telegram", "to": "555"})
        conversation.add_turn("telegram:555", "hi", "hello", person="thandi")
        assert long_memory.retain("Thandi's daughter plays netball").get("saved")
    conn = security._get_conn()
    people.ensure_person_column(conn, "approvals")
    conn.execute("INSERT INTO approvals (action_type, description, payload, status, person) "
                 "VALUES ('send_email', 'Send the invoice', '{}', 'pending', 'thandi')")
    conn.commit()
    conn.close()
    bank.docs["shared"] = {"text": "The office closes at 5", "tags": ["person:thandi", "shared"]}
    with as_person("owner"):
        scheduler.create_task("Tracy's own job", "0 9 * * *")
        assert long_memory.retain("Tracy likes long walks").get("saved")
    sessions.start("thandi")
    disconnected = []

    async def disconnect(service, label="default"):
        disconnected.append((service, label))
        return {"status": "disconnected"}
    import importlib
    monkeypatch.setattr(importlib.import_module("core.oauth.router"), "disconnect", disconnect)
    client = Client(app, headers={"X-API-Key": get_or_create_api_key()})
    client.disconnected = disconnected
    return client


def rows(sql, *args):
    conn = sqlite3.connect(str(database.DB_PATH))
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def nothing_left_under(person_id, bank):
    assert rows("SELECT COUNT(*) FROM tasks WHERE person = ?", person_id)[0][0] == 0
    assert rows("SELECT COUNT(*) FROM approvals WHERE person = ?", person_id)[0][0] == 0
    assert rows("SELECT COUNT(*) FROM conversation_turns WHERE person = ?", person_id)[0][0] == 0
    assert not (scheduler.VAULT_DIR / "people" / person_id).exists()
    assert not any(f"person:{person_id}" in d["tags"] for d in bank.docs.values())
    assert person_id not in accounts.owners().values()


def test_the_owner_sees_what_they_have_before_deciding(office):
    r = office.get("/api/people/thandi/leaving")
    assert r.status_code == 200, r.text
    h = r.json()
    assert (h["name"], h["files"], h["jobs"], h["approvals_waiting"], h["memories"]) == ("Thandi", 2, 1, 1, 2)
    assert h["accounts"] == [{"account": "oc:gmail:thandi", "name": accounts.describe("oc:gmail:thandi")}]


def test_removing_with_the_usual_choices(office, bank):
    r = office.request("DELETE", "/api/people/thandi", json={})
    assert r.status_code == 200, r.text
    done = " ".join(r.json()["done"])
    assert r.json()["all_done"] is True
    # Access stopped.
    assert people.person_for("telegram", "555") is None
    assert rows("SELECT COUNT(*) FROM person_sessions WHERE person = 'thandi'")[0][0] == 0
    # Notes kept, in the owner's "From Thandi".
    kept = scheduler.VAULT_DIR / "From Thandi"
    assert (kept / "Threads" / "plumber.md").read_text(encoding="utf-8").endswith("Booked for Tuesday")
    assert not (kept / ".sweep_state.json").exists()
    assert "From Thandi" in done
    # Jobs kept: the owner's now, paused, results to the console only.
    job = rows("SELECT title, status, deliver_to, person FROM tasks WHERE title LIKE 'Thandi%'")
    assert job == [("Thandi's Monday summary (from Thandi)", "paused", None, "owner")]
    # Memories deleted; what she shared stays shared; the owner's untouched.
    assert "Thandi's daughter plays netball" not in [d["text"] for d in bank.docs.values()]
    assert bank.whose("The office closes at 5") == ["shared"]
    assert bank.whose("Tracy likes long walks") == ["person:owner"]
    # Her account disconnected; the shared one untouched.
    assert office.disconnected == [("gmail", "thandi")]
    assert accounts.owners() == {"oc:gmail:info": "shared"}
    nothing_left_under("thandi", bank)
    assert office.get("/api/people").json()["unfinished"] == []


def test_deleting_everything_about_someone(office, bank):
    r = office.request("DELETE", "/api/people/thandi", json={
        "keep_files": False, "keep_jobs": False, "keep_memories": False, "disconnect_accounts": True})
    assert r.status_code == 200, r.text
    assert not list(scheduler.VAULT_DIR.glob("From *"))
    assert [t for (t,) in rows("SELECT title FROM tasks")] == ["Tracy's own job"]
    nothing_left_under("thandi", bank)


def test_keeping_their_memories_and_accounts_for_the_owner(office, bank):
    r = office.request("DELETE", "/api/people/thandi", json={"keep_memories": True, "disconnect_accounts": False})
    assert r.status_code == 200, r.text
    assert bank.whose("Thandi's daughter plays netball") == ["person:owner"]
    assert bank.whose("The office closes at 5") == ["shared", "person:owner"]
    assert office.disconnected == []
    assert accounts.owner_of("oc:gmail:thandi") == people.OWNER_ID
    with as_person("owner"):
        assert "netball" in " ".join(long_memory.recall("Thandi's daughter?"))
    nothing_left_under("thandi", bank)


def test_a_kept_job_never_goes_to_their_old_chat(office, bank):
    office.request("DELETE", "/api/people/thandi", json={})
    (deliver,) = rows("SELECT deliver_to FROM tasks WHERE title LIKE 'Thandi%'")[0]
    assert deliver is None


def test_their_waiting_approvals_and_chats_are_gone(office, bank):
    office.request("DELETE", "/api/people/thandi", json={})
    with as_person("owner"):
        assert security.get_pending_approvals() == []
    assert rows("SELECT COUNT(*) FROM conversation_turns")[0][0] == 0


def test_when_memory_cant_be_reached_the_rest_is_done_and_finish_tries_again(office, bank):
    bank.reachable = False
    r = office.request("DELETE", "/api/people/thandi", json={})
    assert r.status_code == 200, r.text
    assert r.json()["all_done"] is False and "Finish" in " ".join(r.json()["done"])
    assert people.person_for("telegram", "555") is None  # access still stopped at once
    assert (scheduler.VAULT_DIR / "From Thandi").is_dir()
    assert office.get("/api/people").json()["unfinished"] == [{"id": "thandi", "name": "Thandi", "memories": "delete"}]
    assert "still can't be reached" in office.post("/api/people/thandi/finish").json()["done"][0]
    bank.reachable = True
    r = office.post("/api/people/thandi/finish")
    assert r.json()["all_done"] is True and "2 memories were deleted" in r.json()["done"][0]
    assert r.json()["unfinished"] == []
    assert office.get("/api/people").json()["unfinished"] == []
    nothing_left_under("thandi", bank)


def test_nothing_to_finish_is_a_404(office):
    assert office.post("/api/people/thandi/finish").status_code == 404


def test_a_newcomer_never_gets_the_id_of_someone_who_left(office, bank):
    office.request("DELETE", "/api/people/thandi", json={})
    pid = office.post("/api/people", json={"name": "Thandi"}).json()["person"]["id"]
    assert pid == "thandi-2"


def test_a_folder_left_by_an_older_freehand_isnt_inherited_either(office):
    (scheduler.VAULT_DIR / "people" / "sam").mkdir(parents=True)
    assert office.post("/api/people", json={"name": "Sam"}).json()["person"]["id"] == "sam-2"


def test_a_second_leaver_with_the_same_name_gets_their_own_folder(office, bank):
    (scheduler.VAULT_DIR / "From Thandi").mkdir()
    office.request("DELETE", "/api/people/thandi", json={})
    assert (scheduler.VAULT_DIR / "From Thandi (2)" / "00_Scribble.md").exists()


def test_the_owner_cant_be_removed_and_nobody_is_a_404(office):
    assert office.request("DELETE", "/api/people/owner", json={}).status_code == 404
    assert office.request("DELETE", "/api/people/nobody", json={}).status_code == 404
    assert office.get("/api/people/nobody/leaving").status_code == 404


def test_a_member_cant_remove_anyone(office):
    s = agent_config._load_settings()
    s["people"].append({"id": "sam", "name": "Sam", "role": "member"})
    agent_config._save_settings(s)
    member = TestClient(app, headers={SESSION_HEADER: sessions.start("sam")})
    assert member.request("DELETE", "/api/people/thandi", json={}).status_code == 403
    assert member.get("/api/people/thandi/leaving").status_code == 403
    assert member.post("/api/people/thandi/finish").status_code == 403
    assert people.get("thandi") is not None


def test_removing_without_choices_still_works(office, bank):
    """An older console sends DELETE with no body: the usual choices."""
    assert office.delete("/api/people/thandi").status_code == 200
    assert (scheduler.VAULT_DIR / "From Thandi").is_dir()
    nothing_left_under("thandi", bank)


def test_kept_notes_are_found_by_the_owners_search(office, bank, monkeypatch):
    from core import memory
    monkeypatch.setattr(memory, "PROJECT_ROOT", scheduler.VAULT_DIR.parent)
    office.request("DELETE", "/api/people/thandi", json={})
    with as_person("owner"):
        found = memory.search_memory("plumber Tuesday", limit=5, sync=True)
    assert any("From Thandi" in f["path"] for f in found)

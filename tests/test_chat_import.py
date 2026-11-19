"""Bringing in ChatGPT and Claude exports (core/chat_import.py):
conversations become the person's notes, the memory list goes into
memory (marked so it can be taken out), and nothing is unpacked to disk."""
import contextlib
import io
import json
import urllib.parse
import zipfile

import pytest
from fastapi.testclient import TestClient

from core import agent_config, chat_import, long_memory, people, scheduler, sessions
from server import app, get_or_create_api_key, SESSION_HEADER

CHATGPT = [{
    "title": "Planning the Durban trip", "create_time": 1767225600, "id": "c1", "current_node": "a2",
    "mapping": {
        "root": {"message": None, "parent": None},
        "u1": {"parent": "root", "message": {"author": {"role": "user"},
                                             "content": {"content_type": "text", "parts": ["Where should we stay in Durban?"]}}},
        "a1": {"parent": "u1", "message": {"author": {"role": "assistant"},
                                           "content": {"content_type": "text", "parts": ["An abandoned answer"]}}},
        "a2": {"parent": "u1", "message": {"author": {"role": "assistant"},
                                           "content": {"content_type": "text", "parts": ["Umhlanga is lovely in summer."]}}},
    },
}]
CLAUDE = [{
    "uuid": "k1", "name": "Rooibos cake recipe", "created_at": "2026-03-04T10:00:00Z",
    "chat_messages": [
        {"sender": "human", "text": "How do I bake a rooibos cake?"},
        {"sender": "assistant", "text": "", "content": [{"type": "text", "text": "Steep the tea first, then..."}]},
    ],
}]


def zipped(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, value in files.items():
            zf.writestr(name, value if isinstance(value, (str, bytes)) else json.dumps(value))
    return buf.getvalue()


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


class FakeBank:
    def __init__(self):
        self.docs = {}

    def call(self, method, path, body=None, timeout=0):
        if path.endswith("/memories") and method == "POST":
            doc = str(len(self.docs) + 1)
            self.docs[doc] = {"tags": body["items"][0]["tags"], "lines": [i["content"] for i in body["items"]]}
            return 200, {}
        if "/documents?" in path:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            assert q["tags_match"] == ["all_strict"]
            return 200, {"items": [{"id": i, "tags": d["tags"]} for i, d in self.docs.items()
                                   if set(q["tags"]) <= set(d["tags"])]}
        if "/documents/" in path and method == "DELETE":
            self.docs.pop(path.rsplit("/", 1)[1], None)
            return 200, {}
        return 200, {}

    def lines(self, person):
        return [l for d in self.docs.values() if f"person:{person}" in d["tags"] for l in d["lines"]]


@pytest.fixture
def setup(monkeypatch):
    s = agent_config._load_settings()
    s.update(owner_name="Tracy", people=[{"id": "thandi", "name": "Thandi", "role": "member"}])
    agent_config._save_settings(s)
    bank = FakeBank()
    monkeypatch.setattr(long_memory, "enabled", lambda: True)
    monkeypatch.setattr(long_memory, "_ensure_bank", lambda: True)
    monkeypatch.setattr(long_memory, "_call", bank.call)
    from core import memory
    monkeypatch.setattr(memory, "PROJECT_ROOT", scheduler.VAULT_DIR.parent)
    owner = TestClient(app, headers={"X-API-Key": get_or_create_api_key()})
    thandi = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})
    return bank, owner, thandi


def upload(client, data: bytes, name="export.zip"):
    return client.post("/api/imports/chat-export", content=data,
                       headers={"Content-Type": "application/octet-stream"})


def test_a_chatgpt_export_becomes_notes(setup):
    _, owner, _ = setup
    r = upload(owner, zipped({"conversations.json": CHATGPT, "user.json": {"email": "x@y.z"}}))
    assert r.status_code == 200, r.text
    assert (r.json()["source"], r.json()["conversations"], r.json()["memories"]) == ("chatgpt", 1, 0)
    (note,) = (scheduler.VAULT_DIR / "imports" / "chatgpt").glob("*.md")
    text = note.read_text(encoding="utf-8")
    assert note.name == "2026-01-01 Planning the Durban trip.md"
    assert "Where should we stay in Durban?" in text and "Umhlanga is lovely" in text
    assert "abandoned answer" not in text  # only the thread as it ended
    assert "x@y.z" not in text


def test_a_claude_export_with_its_memories(setup):
    bank, owner, _ = setup
    memories = {"conversations_memory": "## About Tracy\n- Lives in Durban\n- Prefers short answers\n- API key sk-abcdefghijklmnopqrstuvwxyz123456"}
    r = upload(owner, zipped({"data/conversations.json": CLAUDE, "data/memories.json": memories}))
    assert r.status_code == 200, r.text
    assert (r.json()["source"], r.json()["memories"], r.json()["left_out"]) == ("claude", 2, 1)
    assert bank.lines("owner") == ["Lives in Durban", "Prefers short answers"]
    assert all("import:claude" in d["tags"] for d in bank.docs.values())
    (note,) = (scheduler.VAULT_DIR / "imports" / "claude").glob("*.md")
    assert "Steep the tea first" in note.read_text(encoding="utf-8")


def test_a_bare_conversations_file_works_too(setup):
    _, owner, _ = setup
    r = upload(owner, json.dumps(CLAUDE).encode(), "conversations.json")
    assert r.status_code == 200 and r.json()["source"] == "claude"


@pytest.mark.parametrize("data, words", [
    (b"not a zip or json", "couldn't read that file"),
    (zipped({"readme.txt": "hi"}), "no conversations.json"),
    (json.dumps({"hello": 1}).encode(), "isn't a ChatGPT or Claude"),
    (json.dumps([{"something": "else"}]).encode(), "isn't a ChatGPT or Claude"),
])
def test_the_wrong_file_is_explained(setup, data, words):
    _, owner, _ = setup
    r = upload(owner, data)
    assert r.status_code == 400 and words in r.json()["detail"]


def test_a_huge_file_inside_the_zip_is_refused(setup, monkeypatch):
    _, owner, _ = setup
    monkeypatch.setattr(chat_import, "MAX_JSON", 50)
    r = upload(owner, zipped({"conversations.json": CHATGPT}))
    assert r.status_code == 400 and "too big" in r.json()["detail"]
    assert not (scheduler.VAULT_DIR / "imports").exists()


def test_bringing_it_in_again_replaces_it(setup):
    bank, owner, _ = setup
    memories = {"m": "- Lives in Durban"}
    upload(owner, zipped({"conversations.json": CLAUDE, "memories.json": memories}))
    upload(owner, zipped({"conversations.json": CLAUDE, "memories.json": memories}))
    assert len(list((scheduler.VAULT_DIR / "imports" / "claude").glob("*.md"))) == 1
    assert bank.lines("owner") == ["Lives in Durban"]


def test_pasting_what_they_remember(setup):
    bank, owner, _ = setup
    r = owner.post("/api/imports/memories-text", json={"source": "chatgpt", "text":
                   "• Has a dog called Biscuit\n\n2. Works as a bookkeeper\npassword: hunter2hunter2"})
    assert r.status_code == 200, r.text
    assert (r.json()["memories"], r.json()["left_out"]) == (2, 1)
    assert bank.lines("owner") == ["Has a dog called Biscuit", "Works as a bookkeeper"]
    assert owner.post("/api/imports/memories-text", json={"source": "chatgpt", "text": "  "}).status_code == 400
    assert owner.post("/api/imports/memories-text", json={"source": "bard", "text": "x y z"}).status_code == 400


def test_a_members_import_is_hers_alone(setup):
    bank, owner, thandi = setup
    r = upload(thandi, zipped({"conversations.json": CHATGPT, "memories.json": {"m": "- Likes netball"}}))
    assert r.status_code == 200, r.text
    assert list((scheduler.VAULT_DIR / "people" / "thandi" / "imports" / "chatgpt").glob("*.md"))
    assert not (scheduler.VAULT_DIR / "imports").exists()
    assert bank.lines("thandi") == ["Likes netball"] and bank.lines("owner") == []
    assert thandi.get("/api/imports/chat-exports").json()["imported"][0]["source"] == "chatgpt"
    assert owner.get("/api/imports/chat-exports").json()["imported"] == []


def test_taking_it_out_removes_the_notes_and_the_memories(setup):
    bank, owner, thandi = setup
    upload(owner, zipped({"conversations.json": CLAUDE, "memories.json": {"m": "- Lives in Durban"}}))
    upload(thandi, zipped({"conversations.json": CLAUDE, "memories.json": {"m": "- Likes netball"}}))
    r = owner.delete("/api/imports/chat-exports/claude")
    assert r.status_code == 200 and r.json()["conversations"] == 1 and r.json()["memories"] == 1
    assert not (scheduler.VAULT_DIR / "imports" / "claude").exists()
    assert bank.lines("owner") == [] and bank.lines("thandi") == ["Likes netball"]  # hers stays
    assert owner.delete("/api/imports/chat-exports/bard").status_code == 400


def test_the_notes_are_found_by_search(setup):
    _, owner, _ = setup
    upload(owner, zipped({"conversations.json": CHATGPT}))
    from core import memory
    with as_person("owner"):
        found = memory.search_memory("Umhlanga Durban", limit=5, sync=True)
    assert any("imports" in f["path"] for f in found)


def test_freehand_knows_the_owners_import_is_there(setup):
    _, owner, _ = setup
    upload(owner, zipped({"conversations.json": CHATGPT}))
    from core.agents import list_imported_sources
    assert [s["agent"] for s in list_imported_sources()] == ["chatgpt"]
    owner.delete("/api/imports/chat-exports/chatgpt")
    assert list_imported_sources() == []


def test_a_big_upload_is_let_through_to_this_route_only(setup):
    from server import _get_max_body_bytes
    _, owner, _ = setup
    big = str(_get_max_body_bytes() * 2)
    r = owner.post("/api/imports/memories-text", headers={"Content-Length": big}, content=b"{}")
    assert r.status_code == 413


def test_an_empty_upload_is_explained(setup):
    _, owner, _ = setup
    r = owner.post("/api/imports/chat-export", content=b"")
    assert r.status_code == 400 and "Choose the export file" in r.json()["detail"]


def test_an_upload_over_the_limit_is_stopped_while_it_comes_in(setup, monkeypatch):
    _, owner, _ = setup
    monkeypatch.setattr(chat_import, "MAX_UPLOAD", 100)

    def chunks():  # no Content-Length: the size is only known by counting
        for _ in range(10):
            yield b"x" * 50
    r = owner.post("/api/imports/chat-export", content=chunks())
    assert r.status_code == 413
    assert not (scheduler.VAULT_DIR / "imports").exists()

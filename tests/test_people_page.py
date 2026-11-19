"""The People page: the owner adds, changes and removes people, and adds
someone who wrote to FreeHand in one click (core/people.py,
core/newcomers.py, server.py)."""
import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from core import agent_config, gateway, newcomers, people, sessions
from server import app, get_or_create_api_key, SESSION_HEADER


def save_settings(**values):
    s = agent_config._load_settings()
    s.update(values)
    agent_config._save_settings(s)


@pytest.fixture
def owner(monkeypatch):
    newcomers.clear()
    sessions._codes.clear()
    save_settings(owner_name="Tracy", telegram_allow_from=["111"], people=[])
    return TestClient(app, headers={"X-API-Key": get_or_create_api_key()})


def test_the_owner_adds_a_person_and_sees_them(owner):
    r = owner.post("/api/people", json={"name": "Thandi Mokoena", "telegram": ["555"]})
    assert r.status_code == 200, r.text
    assert r.json()["person"]["id"] == "thandi-mokoena"
    listed = owner.get("/api/people").json()
    assert listed["owner"]["name"] == "Tracy" and listed["owner"]["code_by"] == "Telegram"
    assert [(p["name"], p["telegram"]) for p in listed["people"]] == [("Thandi Mokoena", ["555"])]
    assert people.person_for("telegram", "555") == "thandi-mokoena"  # and FreeHand now answers her


def test_two_people_with_the_same_name_get_their_own_ids(owner):
    a = owner.post("/api/people", json={"name": "Sam"}).json()["person"]["id"]
    b = owner.post("/api/people", json={"name": "Sam"}).json()["person"]["id"]
    assert (a, b) == ("sam", "sam-2")


def test_a_name_with_no_latin_letters_still_gets_an_id(owner):
    pid = owner.post("/api/people", json={"name": "Ζωή"}).json()["person"]["id"]
    assert people.PERSON_ID_RE.match(pid)


@pytest.mark.parametrize("body, words", [
    ({"name": ""}, "Give the person a name"),
    ({"name": "Thandi", "telegram": ["-100123"]}, "a group can't be someone's own chat"),
    ({"name": "Thandi", "telegram": ["thandi"]}, "is a number"),
    ({"name": "Thandi", "whatsapp": ["082"]}, "country code"),
    ({"name": "Thandi", "telegram": ["111"]}, "already Tracy's"),
])
def test_mistakes_are_explained(owner, body, words):
    r = owner.post("/api/people", json=body)
    assert r.status_code == 400 and words in r.json()["detail"]
    assert owner.get("/api/people").json()["people"] == []


def test_a_chat_can_be_only_one_persons(owner):
    owner.post("/api/people", json={"name": "Thandi", "whatsapp": ["+27 82 123 4567"]})
    r = owner.post("/api/people", json={"name": "Sam", "whatsapp": ["27821234567"]})
    assert r.status_code == 400 and "already Thandi's" in r.json()["detail"]


def test_changing_someone_keeps_what_the_page_doesnt_show(owner):
    save_settings(people=[{"id": "thandi", "name": "Thandi", "role": "member", "telegram": ["555"], "slack": ["U1"]}])
    r = owner.put("/api/people/thandi", json={"name": "Thandi M", "telegram": ["556"], "whatsapp": []})
    assert r.status_code == 200, r.text
    saved = agent_config._load_settings()["people"][0]
    assert saved == {"id": "thandi", "name": "Thandi M", "role": "member", "telegram": ["556"],
                     "slack": ["U1"], "whatsapp": []}


def test_changing_someone_can_keep_their_own_link(owner):
    owner.post("/api/people", json={"name": "Thandi", "telegram": ["555"]})
    assert owner.put("/api/people/thandi", json={"name": "Thandi", "telegram": ["555"]}).status_code == 200


def test_removing_someone_signs_them_out_and_stops_their_chats(owner, monkeypatch):
    owner.post("/api/people", json={"name": "Thandi", "telegram": ["555"]})
    token = sessions.start("thandi")
    assert owner.delete("/api/people/thandi").status_code == 200
    assert sessions.person_for(token) is None
    assert people.person_for("telegram", "555") is None
    assert owner.delete("/api/people/owner").status_code == 404


def test_a_member_cant_use_the_people_page(owner):
    owner.post("/api/people", json={"name": "Thandi"})
    thandi = TestClient(app, headers={SESSION_HEADER: sessions.start("thandi")})
    for method, path in [("GET", "/api/people"), ("POST", "/api/people"), ("PUT", "/api/people/thandi"),
                         ("DELETE", "/api/people/thandi")]:
        assert thandi.request(method, path, json={"name": "x"}).status_code == 403, (method, path)


# ── Someone who wrote to FreeHand ────────────────────────────────────

def _telegram(chat, kind="private", first="Thandi", n=0):
    return {"update_id": 9000 + n, "message": {"chat": {"id": chat, "type": kind}, "text": "hello",
                                               "from": {"id": chat, "first_name": first, "last_name": "M"}}}


def test_someone_new_on_telegram_is_offered_but_not_answered(owner, monkeypatch):
    sent = []

    async def fake_send(chat_id, text):
        sent.append(chat_id)
        return True

    monkeypatch.setattr(gateway, "_telegram_send_message", fake_send)
    monkeypatch.setattr(gateway, "_seen_deliveries", {})
    asyncio.run(gateway.handle_telegram_update(_telegram(777)))
    assert sent == []
    assert [(n["channel"], n["id"], n["name"]) for n in owner.get("/api/people").json()["newcomers"]] == \
        [("telegram", "777", "Thandi M")]


def test_a_group_isnt_offered_as_a_person(owner, monkeypatch):
    monkeypatch.setattr(gateway, "_seen_deliveries", {})
    asyncio.run(gateway.handle_telegram_update(_telegram(-100777, kind="group", n=1)))
    assert owner.get("/api/people").json()["newcomers"] == []


def test_someone_new_on_whatsapp_is_offered(owner, monkeypatch):
    monkeypatch.setattr(gateway, "_seen_deliveries", {})
    asyncio.run(gateway.handle_whatsapp_message("27829998888@s.whatsapp.net", "hello", message_id="w1"))
    assert [(n["channel"], n["id"]) for n in owner.get("/api/people").json()["newcomers"]] == \
        [("whatsapp", "27829998888")]


def test_adding_someone_who_wrote_lets_them_in_and_takes_them_off_the_list(owner, monkeypatch):
    monkeypatch.setattr(gateway, "_seen_deliveries", {})
    asyncio.run(gateway.handle_telegram_update(_telegram(777, n=2)))
    r = owner.post("/api/people", json={"name": "Thandi M", "telegram": ["777"]})
    assert r.json()["newcomers"] == []
    assert people.person_for("telegram", "777") == "thandi-m"


def test_only_the_last_few_are_kept():
    newcomers.clear()
    for i in range(newcomers.KEEP + 5):
        newcomers.note("telegram", str(1000 + i), f"P{i}")
    assert len(newcomers.recent()) == newcomers.KEEP


def test_someone_added_can_sign_in_with_a_code(owner, monkeypatch):
    out = []

    async def fake_send(channel, to, text):
        out.append(text)
        return True

    from core import notices
    monkeypatch.setattr(notices, "_send", fake_send)
    owner.post("/api/people", json={"name": "Thandi", "telegram": ["555"]})
    raw = TestClient(app)
    raw.post("/api/sign-in/code", json={"person": "thandi"})
    code = re.search(r"\b(\d{6})\b", out[-1]).group(1)
    assert raw.post("/api/sign-in/check", json={"person": "thandi", "code": code}).status_code == 200

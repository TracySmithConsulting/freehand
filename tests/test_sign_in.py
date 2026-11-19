"""Signing in to the console as a person (core/sessions.py, server.py).

Through the real web server, as a browser would: Thandi signs in with a
code sent to her Telegram, and from then on the console is hers alone.
"""
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from core import agent_config, people, scheduler, sessions
from server import app, get_or_create_api_key, SESSION_HEADER


THANDI = {"id": "thandi", "name": "Thandi", "role": "member", "telegram": ["555"]}
SAM = {"id": "sam", "name": "Sam", "role": "member"}  # no chat linked


def save_settings(**values):
    s = agent_config._load_settings()
    s.update(values)
    agent_config._save_settings(s)


@pytest.fixture
def sent(monkeypatch):
    """Messages FreeHand sends to a chat: (channel, to, text)."""
    out = []

    async def fake_send(channel, to, text):
        out.append((channel, to, text))
        return True

    from core import notices
    monkeypatch.setattr(notices, "_send", fake_send)
    sessions._codes.clear()
    save_settings(people=[THANDI, SAM], telegram_allow_from=["111"], owner_name="Tracy")
    return out


@pytest.fixture
def owner():
    return TestClient(app, headers={"X-API-Key": get_or_create_api_key()})


def _code(sent):
    return re.search(r"\b(\d{6})\b", sent[-1][2]).group(1)


def sign_in(person, sent):
    raw = TestClient(app)
    assert raw.post("/api/sign-in/code", json={"person": person}).status_code == 200
    r = raw.post("/api/sign-in/check", json={"person": person, "code": _code(sent)})
    assert r.status_code == 200, r.text
    return TestClient(app, headers={SESSION_HEADER: r.json()["session"]}), r.json()["session"]


# ── Signing in ───────────────────────────────────────────────────────

def test_the_sign_in_screen_lists_names_and_where_a_code_goes_but_no_numbers(sent):
    listed = TestClient(app).get("/api/sign-in/people").json()["people"]
    assert {p["id"]: p["code_by"] for p in listed} == {"owner": "Telegram", "thandi": "Telegram", "sam": None}
    assert "555" not in str(listed) and "111" not in str(listed)


def test_a_code_to_her_telegram_signs_thandi_in(sent):
    thandi, _ = sign_in("thandi", sent)
    assert sent[0][:2] == ("telegram", "555")
    me = thandi.get("/api/me").json()
    assert me["id"] == "thandi" and me["name"] == "Thandi" and not me["owner"]


def test_a_wrong_code_doesnt_and_five_wrong_tries_void_it(sent):
    raw = TestClient(app)
    raw.post("/api/sign-in/code", json={"person": "thandi"})
    right = _code(sent)
    wrong = "000000" if right != "000000" else "111111"
    for _ in range(sessions.CODE_TRIES):
        assert raw.post("/api/sign-in/check", json={"person": "thandi", "code": wrong}).status_code == 400
    # Even the right code doesn't work now.
    assert raw.post("/api/sign-in/check", json={"person": "thandi", "code": right}).status_code == 400


def test_a_code_works_once(sent):
    raw = TestClient(app)
    raw.post("/api/sign-in/code", json={"person": "thandi"})
    code = _code(sent)
    assert raw.post("/api/sign-in/check", json={"person": "thandi", "code": code}).status_code == 200
    assert raw.post("/api/sign-in/check", json={"person": "thandi", "code": code}).status_code == 400


def test_a_new_code_only_once_a_minute(sent):
    raw = TestClient(app)
    assert raw.post("/api/sign-in/code", json={"person": "thandi"}).status_code == 200
    assert raw.post("/api/sign-in/code", json={"person": "thandi"}).status_code == 400
    assert len(sent) == 1


def test_a_code_is_never_sent_to_a_group(sent):
    save_settings(people=[{**THANDI, "telegram": ["-100123"]}])
    assert sessions.code_destination("thandi") is None
    assert TestClient(app).post("/api/sign-in/code", json={"person": "thandi"}).status_code == 400
    assert sent == []


def test_someone_with_no_chat_is_told_to_ask_for_a_link(sent):
    r = TestClient(app).post("/api/sign-in/code", json={"person": "sam"})
    assert r.status_code == 400 and "sign-in link" in r.json()["detail"]


def test_whatsapp_when_theres_no_telegram(sent):
    save_settings(people=[{"id": "thandi", "name": "Thandi", "whatsapp": ["+27 82 123 4567"]}])
    assert sessions.code_destination("thandi") == ("whatsapp", "27821234567@s.whatsapp.net")


# ── One-time links the owner makes ───────────────────────────────────

def test_the_owner_makes_a_one_time_link_that_works_once(sent, owner):
    link = owner.post("/api/people/sam/sign-in-link").json()["link"]
    token = link.split("sign-in=", 1)[1]
    r = TestClient(app).post("/api/sign-in/link", json={"link": token})
    assert r.status_code == 200 and r.json()["person"] == "sam"
    assert TestClient(app).post("/api/sign-in/link", json={"link": token}).status_code == 400


def test_a_member_cant_make_links_or_sign_others_out(sent):
    thandi, _ = sign_in("thandi", sent)
    assert thandi.post("/api/people/sam/sign-in-link").status_code == 403
    assert thandi.post("/api/people/owner/sign-out").status_code == 403


# ── Signed in, the console is hers ───────────────────────────────────

def test_signed_in_thandi_sees_only_her_jobs_and_can_pause_them(sent, owner):
    owner.post("/api/tasks", json={"title": "Tracy's job", "cron_schedule": "0 8 * * *"})
    thandi, _ = sign_in("thandi", sent)
    thandi.post("/api/tasks", json={"title": "Thandi's job", "cron_schedule": "0 9 * * *"})
    hers = thandi.get("/api/tasks").json()
    assert [t["title"] for t in hers] == ["Thandi's job"]
    assert thandi.post(f"/api/tasks/{hers[0]['id']}/pause", json={"paused": True}).status_code == 200
    assert "Thandi's job" not in str(owner.get("/api/tasks").json())  # and the owner's are the owner's


def test_a_member_cant_reach_the_owners_screens(sent):
    thandi, _ = sign_in("thandi", sent)
    for method, path in [("GET", "/api/settings"), ("POST", "/api/security/tier"), ("GET", "/api/llm/backups"),
                         ("POST", "/api/people/team-sharing"), ("GET", "/api/helpers"), ("POST", "/api/broker/config")]:
        r = thandi.request(method, path, json={})
        assert r.status_code == 403, (method, path, r.status_code)
        assert r.json()["detail"] == "Only the owner can do this."


def test_her_chat_message_is_hers_even_if_it_says_its_from_the_owners_telegram(sent, monkeypatch):
    seen = []

    async def fake_run_command(command, source="", caller_id="", person=None):
        seen.append((source, caller_id, person, people.current()))
        return {"text": "ok"}

    monkeypatch.setattr("core.agent.run_command", fake_run_command)
    thandi, _ = sign_in("thandi", sent)
    r = thandi.post("/api/agent/command", json={"command": "hi", "source": "telegram", "caller_id": "111"})
    assert r.status_code == 200
    assert seen == [("", "", "thandi", "thandi")]


def test_the_owners_key_still_works_as_before(sent, owner, monkeypatch):
    seen = []

    async def fake_run_command(command, source="", caller_id="", person=None):
        seen.append((source, caller_id, person))
        return {"text": "ok"}

    monkeypatch.setattr("core.agent.run_command", fake_run_command)
    assert owner.get("/api/settings").status_code == 200
    assert owner.get("/api/me").json()["owner"] is True
    owner.post("/api/agent/command", json={"command": "hi", "source": "telegram", "caller_id": "111"})
    assert seen == [("telegram", "111", None)]


def test_one_persons_request_never_leaks_into_the_next(sent, owner):
    owner.post("/api/tasks", json={"title": "Tracy's job", "cron_schedule": "0 8 * * *"})
    thandi, _ = sign_in("thandi", sent)
    thandi.get("/api/tasks")
    assert "Tracy's job" in str(owner.get("/api/tasks").json())
    assert people.current() == "owner"


def test_a_members_browser_doesnt_change_freehands_time_zone(sent, owner):
    owner.post("/api/timezone", json={"timezone": "Africa/Johannesburg"})
    thandi, _ = sign_in("thandi", sent)
    r = thandi.post("/api/timezone", json={"timezone": "Europe/London"})
    assert r.status_code == 200  # kept as hers (tests/test_person_timezones.py) ...
    assert agent_config._load_settings()["timezone"] == "Africa/Johannesburg"  # ... not the owner's


def test_every_route_a_member_may_use_really_exists():
    from server import MEMBER_ROUTES
    real = {(m, r.path) for r in app.routes for m in (getattr(r, "methods", None) or ())}
    assert MEMBER_ROUTES <= real


# ── Signing out ──────────────────────────────────────────────────────

def test_signing_out_ends_the_session(sent):
    thandi, _ = sign_in("thandi", sent)
    assert thandi.post("/api/sign-out").status_code == 200
    assert thandi.get("/api/me").status_code == 401


def test_removing_someone_from_settings_signs_them_out(sent):
    thandi, _ = sign_in("thandi", sent)
    save_settings(people=[SAM])
    assert thandi.get("/api/me").status_code == 401


def test_the_owner_signs_someone_out_everywhere(sent, owner):
    thandi, _ = sign_in("thandi", sent)
    assert owner.post("/api/people/thandi/sign-out").json()["signed_out"] == 1
    assert thandi.get("/api/me").status_code == 401


def test_an_old_session_runs_out(sent):
    thandi, token = sign_in("thandi", sent)
    from core import database
    conn = sqlite3.connect(str(database.DB_PATH))
    conn.execute("UPDATE person_sessions SET expires_at = '2000-01-01T00:00:00Z'")
    conn.commit()
    conn.close()
    assert sessions.person_for(token) is None
    assert thandi.get("/api/me").status_code == 401


def test_a_bad_session_is_refused_even_with_the_key(sent):
    both = TestClient(app, headers={"X-API-Key": get_or_create_api_key(), SESSION_HEADER: "not-a-session"})
    assert both.get("/api/me").status_code == 401


def test_sessions_and_links_are_kept_only_as_hashes(sent, owner):
    _, token = sign_in("thandi", sent)
    link = owner.post("/api/people/sam/sign-in-link").json()["link"].split("sign-in=", 1)[1]
    from core import database
    conn = sqlite3.connect(str(database.DB_PATH))
    stored = str(conn.execute("SELECT * FROM person_sessions").fetchall()) + \
        str(conn.execute("SELECT * FROM sign_in_links").fetchall())
    conn.close()
    assert token not in stored and link not in stored

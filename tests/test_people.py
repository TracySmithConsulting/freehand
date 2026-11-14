"""Separate users, foundations (Nov 2026): every message is linked to a
person, and what FreeHand stores is tagged with whose it is. On a
one-person FreeHand everything is the owner's and nothing changes."""
import asyncio
import sqlite3

from core import agent_config, conversation, gateway, long_memory, people, scheduler


def save_settings(**values):
    s = agent_config._load_settings()
    s.update(values)
    agent_config._save_settings(s)


THANDI = {"id": "thandi", "name": "Thandi", "telegram": ["555"], "whatsapp": ["+27 82 123 4567"]}


def test_the_owner_always_exists_and_owns_the_console():
    assert [p["id"] for p in people.list_people({})] == ["owner"]
    assert people.person_for("", "") == "owner"
    assert people.person_for("cli", "") == "owner"
    assert people.list_people({"owner_name": "Tracy"})[0]["name"] == "Tracy"


def test_old_allow_lists_still_belong_to_the_owner():
    s = {"telegram_allow_from": ["42"], "telegram_chat_whitelist": []}
    assert people.person_for("telegram", "42", s) == "owner"
    assert people.person_for("telegram", "99", s) is None


def test_a_persons_own_links_make_the_message_theirs():
    s = {"telegram_allow_from": ["42"], "people": [THANDI]}
    assert people.person_for("telegram", "555", s) == "thandi"
    # WhatsApp numbers match however they're written.
    assert people.person_for("whatsapp", "27821234567@s.whatsapp.net", s) == "thandi"
    assert people.person_for("telegram", "42", s) == "owner"


def test_bad_or_duplicate_people_are_ignored():
    s = {"people": [{"id": "Owner"}, {"id": "../x"}, THANDI, {"id": "thandi", "name": "Again"}]}
    assert [p["id"] for p in people.list_people(s)] == ["owner", "thandi"]


def test_a_person_listed_is_let_in_by_the_gateway():
    save_settings(telegram_allow_from=["42"], people=[THANDI])
    assert gateway._check_allow_from("telegram", "555")
    assert gateway._check_allow_from("telegram", "42")
    assert not gateway._check_allow_from("telegram", "7")
    assert gateway._check_allow_from("whatsapp", "27821234567@s.whatsapp.net")


def test_chats_tasks_and_approvals_are_tagged_with_the_person(tmp_path):
    from core import database
    token = people.set_current("thandi")
    try:
        conversation.add_turn("telegram:555", "hi", "hello", person=people.current())
        scheduler.create_task("Water the plants", "")
        from core import security
        security.set_tier("semi_autonomous")
        out = security.intercept_action("send", "Send a note", {"to": "x"})
        assert out["approval_id"]
    finally:
        people.reset(token)
    conn = sqlite3.connect(str(database.DB_PATH))
    try:
        assert conn.execute("select person from conversation_turns").fetchall() == [("thandi",)]
        assert conn.execute("select person from tasks").fetchall() == [("thandi",)]
        assert conn.execute("select person from approvals").fetchall() == [("thandi",)]
    finally:
        conn.close()


def test_rows_from_before_people_become_the_owners(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("create table tasks (id integer primary key, title text)")
    conn.execute("insert into tasks (title) values ('old task')")
    conn.commit()
    people.ensure_person_column(conn, "tasks")
    assert conn.execute("select title, person from tasks").fetchall() == [("old task", "owner")]
    conn.close()


def test_memories_are_tagged_with_whose_they_are(monkeypatch):
    sent = []
    monkeypatch.setattr(long_memory, "enabled", lambda: True)
    monkeypatch.setattr(long_memory, "_ensure_bank", lambda: True)
    monkeypatch.setattr(long_memory, "_call", lambda method, path, body=None, timeout=0: sent.append(body) or (200, {}))
    token = people.set_current("thandi")
    try:
        assert long_memory.retain("Thandi likes rooibos", "chat via telegram")["saved"]
    finally:
        people.reset(token)
    item = sent[0]["items"][0]
    assert item["tags"] == ["person:thandi"] and item["metadata"] == {"person": "thandi"}


def test_the_agent_run_knows_whose_message_it_is(monkeypatch):
    save_settings(people=[THANDI])
    seen = []

    async def fake_inner(command, max_turns, source, caller_id):
        seen.append(people.current())
        return {"text": "ok"}
    from core import agent
    monkeypatch.setattr(agent, "_run_agent", fake_inner)
    asyncio.run(agent.run_agent("hi", source="telegram", caller_id="555"))
    asyncio.run(agent.run_agent("hi", source="", caller_id=""))
    assert seen == ["thandi", "owner"]
    assert people.current() == "owner"  # reset after the run

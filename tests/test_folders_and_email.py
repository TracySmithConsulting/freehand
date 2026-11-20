"""Reaching the person's folders (core/folders.py) and better email
(core/tools/integrations.py): bringing in only what changed from Hermes,
OpenClaw and a notes folder, reading Markdown and text, opening a whole
email, replying in its conversation, and saving a draft. Hermes, the
notes folder and OpenConnector are all stood in for."""
import asyncio
import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from core import agent, agent_config, folders, people
from core.agents import detector, hermes, merger, state_extractor
from core.agents.models import DetectedAgent, ImportOptions
from core.tools import integrations


@contextlib.contextmanager
def as_person(person_id):
    token = people.set_current(person_id)
    try:
        yield
    finally:
        people.reset(token)


def run(name, args):
    return json.loads(asyncio.run(agent.execute_tool(name, args))["content"])


# ── Notes ────────────────────────────────────────────────────────────

@pytest.fixture
def notes(tmp_path):
    root = tmp_path / "Wikis"
    (root / "DBSA" / "Meetings").mkdir(parents=True)
    (root / "DBSA" / "Meetings" / "AGM.md").write_text("# AGM\nVote on the constitution.", encoding="utf-8")
    (root / "Shazacin.txt").write_text("Client list", encoding="utf-8")
    (root / ".obsidian").mkdir()
    (root / ".obsidian" / "workspace.md").write_text("app settings", encoding="utf-8")
    (root / "secret.env").write_text("KEY=1", encoding="utf-8")
    s = agent_config._load_settings()
    s["notes_dir"] = str(root)
    agent_config._save_settings(s)
    return root


def test_notes_come_in_keeping_their_folders(notes):
    result = folders.import_notes(agent_config.VAULT_DIR)
    vault_notes = Path(agent_config.VAULT_DIR) / "imports" / "notes"
    assert (vault_notes / "DBSA" / "Meetings" / "AGM.md").read_text(encoding="utf-8").startswith("# AGM")
    assert (vault_notes / "Shazacin.txt").exists()
    assert not (vault_notes / ".obsidian").exists() and not (vault_notes / "secret.env").exists()
    assert len(result.files_imported) == 2


def test_only_whats_changed_comes_in_again(notes, monkeypatch):
    import core.agents as agents_pkg
    monkeypatch.setattr(agents_pkg, "detect_agent", lambda name: None)
    first = folders.bring_in_new("notes")
    assert first["brought_in"] == 2
    again = folders.bring_in_new("notes")
    assert again["brought_in"] == 0 and "nothing new" in again["done"][0]
    (notes / "Shazacin.txt").write_text("Client list, updated", encoding="utf-8")
    assert folders.bring_in_new("notes")["brought_in"] == 1
    from core.agents import list_imported_sources
    assert len([s for s in list_imported_sources() if s["agent"] == "notes"]) == 2  # one record per file


def test_a_folder_freehand_cant_see_is_explained(monkeypatch):
    import core.agents as agents_pkg
    monkeypatch.setattr(agents_pkg, "detect_agent", lambda name: None)
    monkeypatch.setattr(folders, "in_docker", lambda: True)
    result = folders.bring_in_new("all")
    assert result["unreachable"] == ["hermes", "openclaw", "notes"]
    assert result["help"].startswith("FreeHand can't see Hermes, OpenClaw and your notes")
    assert "FREEHAND_HERMES_DIR" in result["help"] and "never changes them" in result["help"]


# ── Hermes ───────────────────────────────────────────────────────────

def hermes_files(root: Path):
    return [{"path": str(root / rel), "relative": rel, "type": kind, "size": "1"}
            for rel, kind in (("memories/MEMORY.md", "memory"), ("skills/cake/SKILL.md", "skill"))]


@pytest.fixture
def fake_hermes(tmp_path):
    root = tmp_path / "hermes"
    (root / "memories").mkdir(parents=True)
    (root / "skills" / "cake").mkdir(parents=True)
    (root / "memories" / "MEMORY.md").write_text("Tracy chairs DeafBlind SA.", encoding="utf-8")
    (root / "skills" / "cake" / "SKILL.md").write_text("Bake it.", encoding="utf-8")
    return root


def test_a_changed_hermes_memory_comes_in_but_improved_skills_stay(fake_hermes):
    agent_ = DetectedAgent(name="hermes", display_name="Hermes", path=fake_hermes, files=hermes_files(fake_hermes))
    vault = Path(agent_config.VAULT_DIR)
    hermes.import_hermes(agent_, ImportOptions(agent="hermes"), vault)
    skill = vault / "skills" / hermes.SKILLS_VAULT_SUBDIR / "cake" / "SKILL.md"
    skill.write_text("Bake it. Lesson: use rooibos.", encoding="utf-8")  # FreeHand improved it

    (fake_hermes / "memories" / "MEMORY.md").write_text("Tracy chairs DeafBlind SA. New: Blind SA project.", encoding="utf-8")
    again = hermes.import_hermes(agent_, ImportOptions(agent="hermes"), vault)
    assert [f["type"] for f in again.files_imported] == ["memory"]
    assert "Blind SA project" in (vault / "imports" / hermes.VAULT_SUBDIR / "memories_MEMORY.md").read_text(encoding="utf-8")
    assert "rooibos" in skill.read_text(encoding="utf-8")

    third = hermes.import_hermes(agent_, ImportOptions(agent="hermes"), vault)
    assert third.files_imported == [] and any(s.startswith("unchanged") for s in third.skipped)


def test_importing_again_keeps_one_record_per_file(fake_hermes):
    agent_ = DetectedAgent(name="hermes", display_name="Hermes", path=fake_hermes, files=hermes_files(fake_hermes)[:1])
    vault = Path(agent_config.VAULT_DIR)
    for change in ("a", "b", "c"):
        (fake_hermes / "memories" / "MEMORY.md").write_text(change, encoding="utf-8")
        merger.record_import(hermes.import_hermes(agent_, ImportOptions(agent="hermes"), vault))
    assert len(merger.list_imported_sources("hermes")) == 1


def test_old_duplicate_records_are_counted_once():
    from core import database
    conn = sqlite3.connect(str(database.DB_PATH))
    for _ in range(3):
        conn.execute("INSERT INTO imported_sources (agent, source_path, file_type, content_hash, vault_path, "
                     "imported_at, file_size) VALUES ('hermes', 's', 'memory', 'h', '/v/MEMORY.md', 't', 1)")
    conn.commit()
    conn.close()
    assert len(merger.list_imported_sources()) == 1


def test_a_database_in_a_read_only_folder_is_read_from_a_copy(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE user_memory (fact TEXT)")
    conn.execute("INSERT INTO user_memory VALUES ('Likes rooibos')")
    conn.commit()
    conn.close()
    real = sqlite3.connect
    calls = []

    def read_only_folder(target, *a, **kw):
        calls.append(str(target))
        if str(target).startswith("file:"):
            raise sqlite3.OperationalError("unable to open database file")
        return real(target, *a, **kw)

    monkeypatch.setattr(state_extractor.sqlite3, "connect", read_only_folder)
    conn, copy_dir = state_extractor._open_readonly(db)
    assert conn.execute("SELECT fact FROM user_memory").fetchone() == ("Likes rooibos",)
    conn.close()
    assert copy_dir is not None and copy_dir != tmp_path
    state_extractor._drop(copy_dir)
    assert not copy_dir.exists()


def test_docker_looks_where_compose_shows_the_folders():
    assert detector._unix_hermes_paths()[0] == Path("/host/hermes")
    assert detector._unix_openclaw_paths()[0] == Path("/host/openclaw")


# ── Reading notes and folders ────────────────────────────────────────

def test_markdown_and_text_can_be_read(notes):
    found = run("read_text_file", {"path": str(notes / "DBSA" / "Meetings" / "AGM.md")})
    assert "constitution" in found["text"]
    listing = run("list_folder", {"path": str(notes)})
    assert listing["folders"] == ["DBSA"] and listing["files"] == ["Shazacin.txt"]
    assert str(notes) in run("list_folder", {})["folders_you_may_read"]


def test_secrets_and_other_folders_stay_out_of_reach(notes, tmp_path):
    assert "reads .md" in run("read_text_file", {"path": str(notes / "secret.env")})["error"]
    elsewhere = tmp_path / "private.md"
    elsewhere.write_text("x", encoding="utf-8")
    assert "not allowed" in run("read_text_file", {"path": str(elsewhere)})["error"]


def test_a_member_cant_read_the_owners_notes_or_bring_in_memories(notes):
    s = agent_config._load_settings()
    s["people"] = [{"id": "thandi", "name": "Thandi", "role": "member"}]
    agent_config._save_settings(s)
    with as_person("thandi"):
        assert "not allowed" in run("read_text_file", {"path": str(notes / "Shazacin.txt")})["error"]
        assert "Only the owner" in run("bring_in_new_memories", {"source": "all"})["error"]


# ── Email ────────────────────────────────────────────────────────────

class FakeOC:
    def __init__(self):
        self.calls = []

    async def __call__(self, service, kind, action, inputs, label):
        self.calls.append((service, action, inputs))
        if action == "get_message" and service == "google":
            return {"messageId": "m1", "threadId": "t1", "from": "Lebo Mashile <ict.admin@blindsa.org.za>",
                    "to": "tracy@example.com", "subject": "RE: Assistance Blind SA", "date": "17 Nov",
                    "body": "Thank you. Could you look at the leave form first?"}, None
        if action == "get_message":
            return {"id": "o1", "subject": "RE: Assistance Blind SA",
                    "from": {"emailAddress": {"name": "Lebo Mashile", "address": "ict.admin@blindsa.org.za"}},
                    "toRecipients": [{"emailAddress": {"name": "Tracy", "address": "tracy@example.com"}}],
                    "body": {"contentType": "html", "content": "<p>Thank you.</p><p>Could you look at the leave form?</p>"}}, None
        return {"ok": True}, None


@pytest.fixture
def oc(monkeypatch):
    fake = FakeOC()
    monkeypatch.setattr(integrations, "_oc_run", fake)
    return fake


def test_a_whole_email_can_be_read(oc):
    gmail = run("open_email", {"service": "google", "id": "m1"})
    assert gmail["text"] == "Thank you. Could you look at the leave form first?" and gmail["thread"] == "t1"
    outlook = run("open_email", {"service": "microsoft", "id": "o1"})
    assert outlook["from"] == "Lebo Mashile <ict.admin@blindsa.org.za>"
    assert "<p>" not in outlook["text"] and "leave form" in outlook["text"]
    assert oc.calls[-1][2]["bodyContentType"] == "text"


def test_the_list_says_its_only_previews():
    data = {"messages": [{"messageId": "m1", "threadId": "t1", "subject": "Hi", "snippet": "Short"}]}
    slim = integrations.slim_emails("google", data)
    assert slim["emails"][0]["thread"] == "t1" and "open_email" in slim["note"]


def test_a_reply_stays_in_the_conversation(oc):
    asyncio.run(integrations.reply_to_email("google", "default", "m1", "Happy to help."))
    assert oc.calls[-1] == ("google", "reply_email", {"threadId": "t1", "messageId": "m1", "body": "Happy to help."})
    asyncio.run(integrations.reply_to_email("microsoft", "work", "o1", "Happy to help."))
    assert oc.calls[-1] == ("microsoft", "reply_email", {"messageId": "o1", "comment": "Happy to help."})


def test_a_reply_asks_first_and_shows_what_it_says():
    assert agent_config.get_tool_permission("reply_to_email") == "write"
    shown = agent.describe_action("reply_to_email", {"about": "Lebo Mashile, RE: Assistance Blind SA",
                                                      "body": "Dear Lebo, happy to help."})
    assert "Lebo Mashile, RE: Assistance Blind SA" in shown and "happy to help" in shown


def test_a_draft_is_saved_not_sent(oc):
    assert agent_config.get_tool_permission("save_email_draft") == "read"
    out = asyncio.run(integrations.save_email_draft("google", "default", body="Dear Lebo", reply_to_id="m1"))
    action, inputs = oc.calls[-1][1], oc.calls[-1][2]
    assert action == "create_email_draft" and out["saved_to_drafts"] and out["in_the_conversation"]
    assert (inputs["to"], inputs["subject"], inputs["threadId"]) == ("ict.admin@blindsa.org.za", "RE: Assistance Blind SA", "t1")
    out = asyncio.run(integrations.save_email_draft("microsoft", "work", body="Dear Lebo", reply_to_id="o1"))
    assert oc.calls[-1][2]["toRecipients"] == ["ict.admin@blindsa.org.za"] and "not inside" in out["note"]


def test_the_right_tools_come_with_the_words():
    from core import tool_selection
    assert "email" in tool_selection.wanted_groups(["Draft a reply to Lebo's email"])
    assert "files" in tool_selection.wanted_groups(["Check Hermes for anything new"])
    assert {"open_email", "reply_to_email", "save_email_draft"} <= set(tool_selection.BUILTIN_GROUPS["email"])


def test_freehand_is_told_what_it_can_and_cant_do():
    prompt = agent_config.build_system_prompt()
    assert "[phone number]" in prompt and "can't read files or folders" in prompt
    assert "open_email" in prompt and "bring_in_new_memories" in prompt


# ── Setting up ───────────────────────────────────────────────────────

def test_setup_finds_hermes_and_openclaw(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("setup_env", Path(__file__).parent.parent / "scripts" / "setup_env.py")
    setup_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup_env)
    local = tmp_path / "AppData" / "Local"
    (local / "hermes").mkdir(parents=True)
    (local / "hermes" / "SOUL.md").write_text("x")
    (tmp_path / ".openclaw" / "workspace").mkdir(parents=True)
    (tmp_path / ".openclaw" / "workspace" / "SOUL.md").write_text("x")
    found = setup_env.found_folders({"LOCALAPPDATA": str(local)}, home=tmp_path)
    assert found == {"FREEHAND_HERMES_DIR": str(local / "hermes"), "FREEHAND_OPENCLAW_DIR": str(tmp_path / ".openclaw")}


def test_compose_shows_freehand_the_folders_read_only():
    import yaml
    compose = yaml.safe_load((Path(__file__).parent.parent / "compose.yml").read_text(encoding="utf-8"))
    mounts = compose["services"]["freehand"]["volumes"]
    for target in ("/host/hermes", "/host/openclaw", "/host/notes"):
        assert any(m.endswith(f":{target}:ro") for m in mounts), target

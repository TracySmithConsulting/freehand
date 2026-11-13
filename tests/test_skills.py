"""Skills that earn their place (core/skills.py). conftest points the vault
at a temporary folder, so these never touch real skills."""
from datetime import datetime, timedelta, timezone

import pytest

from core import database, skills


def write_skill(rel, name, description, body="Do the thing."):
    md = database.VAULT_DIR / "skills" / rel / "SKILL.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    return md


def by_name(name):
    return {s["name"]: s for s in skills.scan()}[name]


def test_every_new_skill_starts_as_a_draft():
    write_skill("hermes-imports/md-to-pdf", "md-to-pdf", "Turn Markdown into PDF")
    write_skill("my-own", "my-own", "Something I wrote")
    s = {x["name"]: x for x in skills.scan()}
    assert s["md-to-pdf"]["status"] == "draft" and s["md-to-pdf"]["source"] == "imported"
    assert s["my-own"]["status"] == "draft" and s["my-own"]["source"] == "user"


def test_a_draft_becomes_active_after_working_three_times():
    write_skill("learned/weekly-summary", "weekly-summary", "Summarise the week")
    for _ in range(skills.PROMOTE_AFTER - 1):
        assert skills.use("weekly-summary")["status"] == "draft"
        skills.record_outcome("weekly-summary", True)
    out = skills.record_outcome("weekly-summary", True)
    assert out["status"] == "active" and "now an active skill" in out["message"]


def test_a_draft_that_keeps_failing_is_archived():
    write_skill("learned/flaky", "flaky", "Often wrong")
    skills.record_outcome("flaky", False)
    assert by_name("flaky")["status"] == "draft"
    out = skills.record_outcome("flaky", False)
    assert out["status"] == "archived"
    assert "error" in skills.use("flaky")  # archived skills aren't offered


def test_a_success_resets_the_failure_count():
    write_skill("learned/ok", "ok", "Mostly fine")
    skills.record_outcome("ok", False)
    skills.record_outcome("ok", True)
    skills.record_outcome("ok", False)
    assert by_name("ok")["status"] == "draft"


def test_unused_drafts_are_archived_after_30_days(monkeypatch):
    write_skill("hermes-imports/old-junk", "old-junk", "Never used")
    skills.scan()
    later = datetime.now(timezone.utc) + timedelta(days=skills.STALE_DRAFT_DAYS + 1)
    monkeypatch.setattr(skills, "_now", lambda: later)
    s = by_name("old-junk")
    assert s["status"] == "archived" and "Not used" in s["status_reason"]


def test_approved_and_active_skills_are_not_archived_for_age(monkeypatch):
    write_skill("mine", "mine", "Approved by the user")
    skills.set_status(by_name("mine")["id"], "active", "Approved by you")
    later = datetime.now(timezone.utc) + timedelta(days=365)
    monkeypatch.setattr(skills, "_now", lambda: later)
    assert by_name("mine")["status"] == "active"


def test_freehand_saves_new_skills_as_drafts():
    out = skills.save("Weekly Invoice Summary", "Summarise invoices each Friday", "1. Open the sheet\n2. Total it")
    assert out["saved"] and out["status"] == "draft"
    s = by_name("weekly-invoice-summary")
    assert s["source"] == "learned" and s["status"] == "draft"
    assert "Total it" in skills.read("weekly-invoice-summary")["body"]


def test_save_refuses_secrets_and_name_clashes():
    assert "password or key" in skills.save("x", "y", "my password is hunter2")["error"]
    write_skill("mine", "mine", "Hand-written")
    assert "already a skill" in skills.save("mine", "y", "steps")["error"]


def test_the_prompt_lists_active_first_and_hides_archived():
    write_skill("a-draft", "a-draft", "Untested")
    write_skill("a-good-one", "a-good-one", "Proven")
    write_skill("a-bad-one", "a-bad-one", "Retired")
    skills.set_status(by_name("a-good-one")["id"], "active")
    skills.set_status(by_name("a-bad-one")["id"], "archived")
    lines = skills.prompt_index().splitlines()
    assert lines[0].startswith("- a-good-one:")
    assert any("a-draft (draft, untested)" in line for line in lines)
    assert not any("a-bad-one" in line for line in lines)


def test_delete_removes_the_file():
    md = write_skill("hermes-imports/junk", "junk", "Delete me")
    assert skills.delete(by_name("junk")["id"])["deleted"]
    assert not md.exists() and "junk" not in {s["name"] for s in skills.scan()}


@pytest.mark.skipif(not hasattr(__import__("os"), "symlink"), reason="no symlinks")
def test_skills_outside_the_folder_are_ignored(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: sneaky\ndescription: x\n---\nx", encoding="utf-8")
    root = database.VAULT_DIR / "skills"
    root.mkdir(parents=True, exist_ok=True)
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted here")
    assert "sneaky" not in {s["name"] for s in skills.scan()}

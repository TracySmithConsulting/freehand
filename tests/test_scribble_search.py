"""Searchable scribbles (Nov 2026). conftest points the vault at a temp
folder, so these never read real notes."""
import pytest

from core import memory


@pytest.fixture(autouse=True)
def _project_root_is_the_temp_vaults_parent(monkeypatch):
    # sync_vault_to_sqlite stores paths relative to PROJECT_ROOT.
    monkeypatch.setattr(memory, "PROJECT_ROOT", memory.VAULT_DIR.parent)


def write(rel, text):
    p = memory.VAULT_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_the_scribble_pad_is_searchable():
    write("00_Scribble.md", "# 00_Scribble\n\n- [ ] plumber said the geyser quote is R4500\n")
    results = memory.search_memory("what did I jot down about the plumber?", limit=5, sync=True)
    assert results and results[0]["title"] == "Scribble pad"
    assert "R4500" in results[0]["snippet"]


def test_words_match_in_any_order_and_by_word_start():
    write("Threads/home.md", "# Home\n\nThe plumbers fixed the geyser on Tuesday.\n")
    assert memory.search_memory("geyser plumber", sync=True)


def test_falls_back_to_any_word_when_none_has_all():
    write("Threads/garden.md", "# Garden\n\nBuy compost for the roses.\n")
    titles = [r["title"] for r in memory.search_memory("roses tractor", sync=True)]
    assert "Garden" in titles


def test_search_syntax_in_the_question_is_just_text():
    write("Journal/2026-11.md", "# Journal 2026-11\n\nNOT a problem, said Anna.\n")
    for q in ['NOT OR * "', "anna*", "anna:", "^anna"]:
        memory.search_memory(q, sync=True)  # must not raise
    assert memory.search_memory("anna", sync=True)


def test_skills_are_not_notes():
    write("skills/hermes-imports/x/SKILL.md", "---\nname: x\n---\nplumber skill text")
    write("Threads/home.md", "# Home\n\nplumber came\n")
    titles = [r["title"] for r in memory.search_memory("plumber", limit=10, sync=True)]
    assert titles == ["Home"]


def test_deleted_notes_stop_turning_up():
    p = write("Threads/old.md", "# Old\n\nquince jam recipe\n")
    assert memory.search_memory("quince", sync=True)
    p.unlink()
    assert memory.search_memory("quince", sync=True) == []

"""Putting the DBSA manual together (Nov 2026): real Word footnotes, a
contents page, combining chapters without losing their bullets or
footnotes, and three things that went wrong along the way: work notes
shown to the person, "{'text': ...}" scraps, and characters Word can't
store (core/doc_notes.py)."""
import asyncio

import pytest

pytest.importorskip("docx")

from docx import Document  # noqa: E402

from core import agent, doc_notes, documents as d, work_notes  # noqa: E402

W = doc_notes.W


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "folder", lambda: tmp_path)
    return tmp_path


def chapter(path, title, bullets=("One", "Two"), numbers=()):
    doc = Document()
    doc.add_heading(title, level=1)
    doc.add_heading(title + " section", level=2)
    doc.add_paragraph("Global estimates suggest it affects 0.2% of people.")
    for b in bullets:
        doc.add_paragraph(b)
    for n in numbers:
        doc.add_paragraph(n)
    doc.save(str(path))
    out = d.edit(path.name, [{"paragraphs": list(range(3, 3 + len(bullets))), "list": "bullet"}]
                 + ([{"paragraphs": list(range(3 + len(bullets), 3 + len(bullets) + len(numbers))), "list": "number"}]
                    if numbers else []), output=path.stem)
    return out["saved_as"]


# ── Footnotes ──────────────────────────────────────────────────────────

def test_a_footnote_goes_after_the_words_and_is_listed(work):
    name = chapter(work / "c3.docx", "Chapter 3")
    out = d.edit(name, [{"paragraph": 2, "footnote": "WHO (n.d.).", "after": "estimate"},
                        {"paragraph": 3, "footnote": {"text": "Second note."}},
                        {"paragraph": 4, "footnote": "Never added.", "after": "not there"}])
    assert out["changes"] == ['paragraph 2: a footnote added after "estimate"', "paragraph 3: a footnote added"]
    assert out["not_done"] == ['paragraph 4: "not there" isn\'t in that paragraph']
    doc = Document(str(work / out["saved_as"]))
    runs = doc.paragraphs[2]._p.findall(f"{{{W}}}r")
    texts = ["".join(t.text for t in r.iter(f"{{{W}}}t")) or ("[note]" if r.find(f".//{{{W}}}footnoteReference") is not None else "")
             for r in runs]
    assert "".join(texts) == "Global estimates[note] suggest it affects 0.2% of people."  # the whole word
    assert doc_notes.list_footnotes(doc) == [{"footnote": 1, "text": "WHO (n.d.).", "p": 2},
                                             {"footnote": 2, "text": "Second note.", "p": 3}]
    o = d.outline(out["saved_as"])
    assert o["footnotes"] == 2 and o["paragraphs"][2]["footnotes"] == 1
    assert d.outline(out["saved_as"], show="footnotes")["footnotes"][1]["text"] == "Second note."
    styles = {s.name for s in doc.styles}
    assert {"footnote text", "footnote reference"} <= styles
    # Rewriting the paragraph keeps its footnote mark.
    again = d.edit(out["saved_as"], [{"paragraph": 2, "text": "Estimates suggest it affects 0.2% of people."}])
    assert len(doc_notes.list_footnotes(Document(str(work / again["saved_as"])))) == 2


def test_footnotes_survive_saving_and_reopening(work):
    name = chapter(work / "c.docx", "Chapter 1")
    out = d.edit(name, [{"paragraph": 2, "footnote": "A source."}])
    first = d.edit(out["saved_as"], [{"paragraph": 1, "footnote": "Another source."}])
    doc = Document(str(work / first["saved_as"]))
    assert [n["text"] for n in doc_notes.list_footnotes(doc)] == ["Another source.", "A source."]  # reading order


# ── Contents ───────────────────────────────────────────────────────────

def test_a_contents_page_lists_the_headings_and_updates_in_word(work):
    doc = Document()
    doc.add_paragraph("DBSA Manual")
    doc.add_heading("Foreword", level=1)
    doc.add_heading("Chapter 1", level=1)
    doc.add_heading("1.1 Introduction", level=2)
    doc.add_heading("1.1.1 Too deep", level=3)
    doc.save(str(work / "m.docx"))
    out = d.edit("m.docx", [{"paragraph": 0, "contents": True, "levels": 2}])
    assert out["changes"] == ["a contents page added after paragraph 0 (3 headings)"]
    doc = Document(str(work / out["saved_as"]))
    assert [p.text for p in doc.paragraphs[1:5]] == ["Contents", "Foreword", "Chapter 1", "1.1 Introduction"]
    assert doc.paragraphs[1].style.name == "TOC Heading" and d._heading_level("TOC Heading") is None
    assert 'TOC \\o "1-2"' in "".join(t.text for t in doc.element.body.iter(f"{{{W}}}instrText"))
    assert doc.settings.element.find(f"{{{W}}}updateFields") is not None
    assert "no headings" in d.edit("m.docx", [{"paragraph": 4, "contents": True}])["error"]


# ── Combining keeps bullets, numbers and footnotes ─────────────────────

def test_combining_keeps_each_chapters_lists_and_footnotes(work):
    one = chapter(work / "c1.docx", "Chapter 1", bullets=("Apples", "Pears"), numbers=("First step", "Second step"))
    two = chapter(work / "c2.docx", "Chapter 2", bullets=("Red", "Blue", "Green"))
    one = d.edit(one, [{"paragraph": 2, "footnote": "Note in chapter 1."}], output="c1 notes")["saved_as"]
    two = d.edit(two, [{"paragraph": 2, "footnote": "Note in chapter 2."}], output="c2 notes")["saved_as"]
    out = d.combine([one, two], output="Book")
    doc = Document(str(work / out["saved_as"]))
    kinds = [(p.text, d._list_kind(doc, p)) for p in doc.paragraphs if d._list_kind(doc, p)]
    assert kinds == [("Apples", "bullet"), ("Pears", "bullet"), ("First step", "number"), ("Second step", "number"),
                     ("Red", "bullet"), ("Blue", "bullet"), ("Green", "bullet")]
    assert [n["text"] for n in doc_notes.list_footnotes(doc)] == ["Note in chapter 1.", "Note in chapter 2."]
    ids = [r.get(f"{{{W}}}id") for r in doc.element.body.iter(f"{{{W}}}footnoteReference")]
    assert len(set(ids)) == 2  # each its own note


# ── The bumps along the way ────────────────────────────────────────────

def test_text_in_any_shape_becomes_plain_words(work):
    chapter(work / "c.docx", "Chapter 3")
    out = d.edit("c.docx", [{"paragraph": 2, "text": {"text": "Clean words."}},
                            {"paragraph": 3, "text": [{"text": "Part one, "}, "part two\x0b."]},
                            {"paragraph": 1, "insert_after": {"text": "Inserted\x00 paragraph."}}])
    texts = [p.text for p in Document(str(work / out["saved_as"])).paragraphs]
    assert "Clean words." in texts and "Part one, part two." in texts and "Inserted paragraph." in texts
    assert not any("{'text'" in t for t in texts)


def test_work_notes_never_reach_the_person(monkeypatch):
    reply = "Chapter 8 is done.\n\n[Work notes (for you; the person didn't see these):\n- read ch8]"
    assert work_notes.strip(reply) == "Chapter 8 is done."
    assert work_notes.strip("No notes here.") == "No notes here."

    async def model(messages, tools=None, config=None):
        return {"content": reply, "_usage": {}}
    monkeypatch.setattr(agent, "call_llm", model)
    assert asyncio.run(agent.run_agent("Please check chapter 8."))["text"] == "Chapter 8 is done."
    assert "never write these in a reply" in work_notes.as_history("- read ch8")


def test_the_tools_describe_footnotes_and_contents():
    from core import agent_config
    source = open(agent_config.__file__, encoding="utf-8").read()
    assert "ONLY once the person has approved" in source and '"footnotes"]' in source
    assert "contents: true" in source

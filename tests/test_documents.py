"""Documents in the work folder (core/documents.py): read, check, fix,
edit, clean, combine and create, always as new files beside the
originals, which are never changed."""
import asyncio
import hashlib
import io
import json
from pathlib import Path

import pytest

docx = pytest.importorskip("docx")
pptx = pytest.importorskip("pptx")

from docx import Document  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Pt  # noqa: E402
from PIL import Image  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Inches  # noqa: E402

from core import agent, agent_config, documents as d, people  # noqa: E402


@pytest.fixture
def work(tmp_path, monkeypatch):
    folder = tmp_path / "FreeHand work"
    folder.mkdir()
    monkeypatch.setattr(d, "folder", lambda: folder)
    return folder


def png(colour=(200, 30, 30)):
    b = io.BytesIO()
    Image.new("RGB", (80, 60), colour).save(b, "PNG")
    b.seek(0)
    return b


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sample_docx(path):
    doc = Document()
    doc.add_heading("Annual report", level=1)
    p = doc.add_paragraph()
    p.add_run("The ")
    p.add_run("quick brown").bold = True
    p.add_run(" fox [Editor: check this figure] jumps.")
    doc.add_paragraph("A second paragraph [TK].")
    bold = doc.add_paragraph()
    bold.add_run("Looks like a heading").bold = True
    doc.add_heading("Details", level=3)  # skips level 2
    doc.add_picture(png())
    t = doc.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Name", "Total"
    t.rows[1].cells[0].text, t.rows[1].cells[1].text = "Ann", "5"
    for _ in range(3):
        doc.add_paragraph("")
    doc.add_paragraph("The end.")
    doc.save(str(path))
    return path


def tracked_docx(path):
    doc = Document()
    p = doc.add_paragraph("Keep this. ")
    ins = p._p.makeelement(qn("w:ins"), {qn("w:id"): "1", qn("w:author"): "Ed"})
    r = p._p.makeelement(qn("w:r"), {})
    t = r.makeelement(qn("w:t"), {})
    t.text = "Added text."
    r.append(t)
    ins.append(r)
    p._p.append(ins)
    dele = p._p.makeelement(qn("w:del"), {qn("w:id"): "2", qn("w:author"): "Ed"})
    r2 = p._p.makeelement(qn("w:r"), {})
    dt = r2.makeelement(qn("w:delText"), {})
    dt.text = "Removed text."
    r2.append(dt)
    dele.append(r2)
    p._p.append(dele)
    commented = doc.add_paragraph("A sentence with a comment.")
    doc.add_comment(commented.runs, text="Is this right?", author="Ed")
    doc.save(str(path))
    return path


def sample_pptx(path):
    prs = Presentation()
    s1 = prs.slides.add_slide(prs.slide_layouts[1])
    s1.shapes.title.text = "Welcome"
    s1.placeholders[1].text = "Hello [TODO: fix greeting] everyone"
    s2 = prs.slides.add_slide(prs.slide_layouts[6])  # blank: no title
    s2.shapes.add_picture(png(), Inches(1), Inches(1))
    box = s2.shapes.add_textbox(Inches(1), Inches(4), Inches(4), Inches(1))
    box.text_frame.text = "Tiny print"
    box.text_frame.paragraphs[0].runs[0].font.size = Pt(10)
    prs.save(str(path))
    return path


def tiny_pdf(path, text="Hello from a PDF", tagged=False):
    stream = f"BT /F1 24 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R" + (b" /MarkInfo << /Marked true >> /Lang (en-GB)" if tagged else b"") + b" >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    Path(path).write_bytes(out)
    return path


# ── The folder ──────────────────────────────────────────────────────────

def test_only_files_in_the_work_folder(work, tmp_path):
    outside = tmp_path / "secret.docx"
    Document().save(str(outside))
    for bad in (str(outside), "../secret.docx"):
        with pytest.raises(d.Refused, match="only works on files in your work folder"):
            d.resolve(bad)


def test_a_file_by_part_of_its_name(work):
    sample_docx(work / "Annual report 2026.docx")
    sample_docx(work / "Budget.docx")
    assert d.resolve("annual").name == "Annual report 2026.docx"
    with pytest.raises(d.Refused, match="More than one file matches"):
        d.resolve(".docx")
    with pytest.raises(d.Refused, match="There's no"):
        d.resolve("missing")


def test_no_folder_and_members_are_explained(work, monkeypatch):
    token = people.set_current("thandi")
    try:
        with pytest.raises(d.Refused, match="only for FreeHand's owner"):
            d.list_files()
    finally:
        people.reset(token)
    monkeypatch.setattr(d, "folder", lambda: None)
    with pytest.raises(d.Refused, match="FREEHAND_WORK_DIR"):
        d.list_files()


def test_listing(work):
    sample_docx(work / "a.docx")
    (work / "notes.md").write_text("# Hi")
    (work / "~$a.docx").write_text("lock file")
    (work / "photo.jpg").write_bytes(b"x")
    listed = [(f["name"], f["kind"]) for f in d.list_files()["files"]]
    assert listed == [("a.docx", "Word"), ("notes.md", "Markdown"), ("photo.jpg", "picture")]


# ── Reading and checking ────────────────────────────────────────────────

def test_word_outline(work):
    sample_docx(work / "r.docx")
    out = d.outline("r.docx")
    assert out["paragraphs"][0] == {"p": 0, "text": "Annual report", "heading": 1}
    assert out["pictures"] == [{"picture": 1, "alt_text": "", "decorative": False}]
    assert out["tables"][0]["first_row"] == ["Name", "Total"] and not out["tables"][0]["header_row"]


def test_word_accessibility_check(work):
    sample_docx(work / "r.docx")
    found = " | ".join(i["problem"] for i in d.check("r.docx")["problems"])
    for expected in ("no alt text", "Heading levels skip", "look like headings", "no header row",
                     "no title", "Empty paragraphs"):
        assert expected in found, expected


def test_powerpoint_outline_and_check(work):
    sample_pptx(work / "deck.pptx")
    out = d.outline("deck.pptx")
    assert [s["title"] for s in out["slides"]] == ["Welcome", ""]
    found = " | ".join(i["problem"] for i in d.check("deck.pptx")["problems"])
    for expected in ("have no title (slides [2])", "no alt text", "smaller than 18 point", "no title in its properties"):
        assert expected in found, expected


def test_pdf_reading_and_check(work):
    tiny_pdf(work / "plain.pdf")
    tiny_pdf(work / "good.pdf", tagged=True)
    out = d.outline("plain.pdf")
    assert "Hello from a PDF" in out["pages"][0]["text"] and out["page_count"] == 1
    plain = " | ".join(i["problem"] for i in d.check("plain.pdf")["problems"])
    assert "isn't tagged" in plain and "language isn't set" in plain
    good = " | ".join(i["problem"] for i in d.check("good.pdf")["problems"])
    assert "tagged" not in good and "language" not in good


# ── Editing, always into a new file ─────────────────────────────────────

def test_find_and_replace_keeps_formatting_and_the_original(work):
    src = sample_docx(work / "r.docx")
    before = digest(src)
    out = d.edit("r.docx", [{"find": "quick brown fox", "replace": "slow red fox"},
                            {"find": "fox", "replace": "foxes"}])  # contains its own search: mustn't loop
    assert out["saved_as"] == "r (edited).docx" and digest(src) == before
    p = Document(str(work / out["saved_as"])).paragraphs[1]
    assert p.text == "The slow red foxes [Editor: check this figure] jumps."
    assert p.runs[1].bold  # the replacement took the formatting of where it started


def test_paragraph_changes(work):
    sample_docx(work / "r.docx")
    out = d.edit("r.docx", [{"paragraph": 3, "style": "Heading 2"},
                            {"paragraph": 2, "text": "Rewritten."},
                            {"paragraph": 0, "insert_after": "Introduction", "style": "Heading 2"},
                            {"paragraph": 99, "text": "nope"}])
    doc = Document(str(work / out["saved_as"]))
    texts = [(p.style.name, p.text) for p in doc.paragraphs[:5]]
    assert texts[1] == ("Heading 2", "Introduction")
    assert ("Normal", "Rewritten.") in texts and ("Heading 2", "Looks like a heading") in texts
    assert out["not_done"] == ["paragraph 99 doesn't exist"]


def test_results_never_overwrite(work):
    sample_docx(work / "r.docx")
    first = d.edit("r.docx", [{"find": "end", "replace": "finish"}])["saved_as"]
    second = d.edit("r.docx", [{"find": "end", "replace": "close"}])["saved_as"]
    assert (first, second) == ("r (edited).docx", "r (edited) (2).docx")
    named = d.edit("r.docx", [{"find": "end", "replace": "x"}], output="../../escape")["saved_as"]
    assert named == "escape.docx" and (work / "escape.docx").exists()


def test_clean_accepts_changes_removes_comments_and_notes(work):
    tracked_docx(work / "t.docx")
    sample_docx(work / "r.docx")
    out = d.clean("t.docx")
    doc = Document(str(work / out["saved_as"]))
    assert doc.paragraphs[0].text == "Keep this. Added text."
    body = doc.element.body
    assert not list(body.iter(qn("w:ins"))) and not list(body.iter(qn("w:del")))
    assert not list(body.iter(qn("w:commentReference"))) and out["comments_removed"] == 1
    notes = d.clean("r.docx")
    assert notes["notes_removed"] == ["[Editor: check this figure]", "[TK]"]
    cleaned = Document(str(work / notes["saved_as"]))
    assert cleaned.paragraphs[1].text == "The quick brown fox jumps."
    assert cleaned.paragraphs[2].text == "A second paragraph."


def test_nothing_to_clean_makes_no_copy(work):
    doc = Document()
    doc.add_paragraph("Clean already. [See figure 2] is a real reference.")
    doc.save(str(work / "c.docx"))
    assert d.clean("c.docx")["nothing_to_clean"]
    assert [p.name for p in work.iterdir()] == ["c.docx"]


def test_powerpoint_edit_and_clean(work):
    sample_pptx(work / "deck.pptx")
    out = d.edit("deck.pptx", [{"slide": 1, "notes": "Smile."}, {"slide": 2, "title": "Our photo"},
                               {"slide": 2, "picture": 1, "alt_text": "A red square."},
                               {"find": "everyone", "replace": "all"}])
    prs = Presentation(str(work / out["saved_as"]))
    s1, s2 = prs.slides
    assert s1.notes_slide.notes_text_frame.text == "Smile."
    assert "all" in s1.placeholders[1].text
    assert any(sh.has_text_frame and sh.text_frame.text == "Our photo" for sh in s2.shapes)
    assert d._shape_alt(next(sh for sh in s2.shapes if sh.shape_type == 13)) == "A red square."
    cleaned = d.clean("deck.pptx")
    assert cleaned["notes_removed"] == ["[TODO: fix greeting]"]


# ── Combining and creating ──────────────────────────────────────────────

def test_combine_keeps_pictures_and_brings_in_other_kinds(work):
    from openpyxl import Workbook
    sample_docx(work / "one.docx")
    (work / "two.md").write_text("# Part two\n\n- a point\n\n| A | B |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
    (work / "three.txt").write_text("Plain text paragraph.\n\nAnother one.", encoding="utf-8")
    wb = Workbook()
    wb.active.append(["Item", "Cost"])
    wb.active.append(["Tea", 12])
    wb.save(str(work / "four.xlsx"))
    tiny_pdf(work / "five.pdf")
    out = d.combine(["one.docx", "two.md", "three.txt", "four.xlsx", "five.pdf"], "Everything", title="All of it")
    assert out["saved_as"] == "Everything.docx"
    doc = Document(str(work / "Everything.docx"))
    texts = [p.text for p in doc.paragraphs]
    assert "Part two" in texts and "Plain text paragraph." in texts and "Hello from a PDF" in texts
    assert len(doc.inline_shapes) == 1  # the picture came across
    assert all(d._has_header_row(t) for t in doc.tables[1:])
    assert doc.core_properties.title == "All of it"


def test_create_word_from_markdown(work):
    out = d.create("Plan", "# The plan\n\nIntro with **bold**.\n\n## Steps\n\n1. First\n2. Second\n")
    doc = Document(str(work / out["saved_as"]))
    styles = [(p.style.name, p.text) for p in doc.paragraphs]
    assert ("Heading 1", "The plan") in styles and ("List Number", "First") in styles
    assert doc.core_properties.title == "The plan" and d._docx_default_lang(doc) == "en-ZA"


def test_create_powerpoint_from_markdown(work):
    md = "# Welcome\n- One\n- Two\nNotes: say hello\n\n# Next steps\n- Three\n"
    out = d.create("Talk", md, kind="powerpoint", title="My talk")
    prs = Presentation(str(work / out["saved_as"]))
    assert [d._slide_title(s) for s in prs.slides] == ["My talk", "Welcome", "Next steps"]
    assert prs.slides[1].notes_slide.notes_text_frame.text == "say hello"
    assert round(prs.slide_width / prs.slide_height, 2) == 1.78  # 16:9
    assert d.check("Talk.pptx")["count"] == 0


# ── Fixing ──────────────────────────────────────────────────────────────

def test_fix_word(work, monkeypatch):
    sample_docx(work / "r.docx")
    seen = []

    async def fake_describe(image, context=""):
        seen.append(context)
        return "A red rectangle."
    monkeypatch.setattr(d, "describe", fake_describe)
    out = asyncio.run(d.fix("r.docx"))
    fixed = Document(str(work / out["saved_as"]))
    assert fixed.core_properties.title == "Annual report"
    assert d._docx_default_lang(fixed) == "en-US"  # Word's own template had set it: left as it was
    assert d._has_header_row(fixed.tables[0])
    assert d._docx_pictures(fixed)[0]["alt_text"] == "A red rectangle."
    assert seen == ["Details"]  # the text just before the picture, for context
    left = " | ".join(i["problem"] for i in d.check(out["saved_as"])["problems"])
    assert "alt text" not in left and "header row" not in left and "title" not in left


def test_fix_powerpoint_says_whats_left(work, monkeypatch):
    sample_pptx(work / "deck.pptx")

    async def decorative(image, context=""):
        return ""
    monkeypatch.setattr(d, "describe", decorative)
    out = asyncio.run(d.fix("deck.pptx"))
    assert any("slides [2] have no title" in s for s in out["still_to_do"])
    prs = Presentation(str(work / out["saved_as"]))
    assert prs.core_properties.title == "Welcome"


def test_alt_text_uses_the_ai_model_once_per_picture(monkeypatch):
    calls = []

    async def fake_llm(messages, tools=None, config=None):
        calls.append(messages)
        return {"content": "DECORATIVE", "_usage": {}}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    assert asyncio.run(d.describe(png().getvalue())) == ""
    content = calls[0][0]["content"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert asyncio.run(d.describe(b"not a picture")) is None and len(calls) == 1


# ── Wiring ──────────────────────────────────────────────────────────────

def test_tools_are_offered_for_document_work_and_explain_problems(work):
    from core import tool_selection
    assert "work" in tool_selection.wanted_groups(["combine these documents into one"])
    for name in d.__dict__.get("WORK_TOOLS", agent.WORK_TOOLS):
        assert agent_config.TOOL_REGISTRY[name] == "read" and name in agent_config.TOOL_SCHEMAS
    said = json.loads(asyncio.run(agent.execute_tool("document_outline", {"file": "missing.docx"}))["content"])
    assert said == {"error": "There's no \"missing.docx\" in your work folder."}
    sample_docx(work / "r.docx")
    listed = json.loads(asyncio.run(agent.execute_tool("work_files", {}))["content"])
    assert listed["files"][0]["name"] == "r.docx"


def test_compose_shares_the_work_folder():
    root = Path(__file__).resolve().parent.parent
    for name in ("compose.yml", "install/compose.yml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "${FREEHAND_WORK_DIR:-./host-folders/no-work}:/host/work\n" in text
        assert "FREEHAND_WORK_FROM=${FREEHAND_WORK_DIR:-}" in text
    assert '"pypdf>=4.0"' in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_language_is_set_when_missing(work):
    doc = Document()
    defaults = doc.styles.element.find(qn("w:docDefaults"))
    lang = defaults.find(qn("w:rPrDefault") + "/" + qn("w:rPr") + "/" + qn("w:lang"))
    lang.getparent().remove(lang)
    doc.add_heading("Hi", level=1)
    doc.save(str(work / "nolang.docx"))
    assert any("language isn't set" in i["problem"] for i in d.check("nolang.docx")["problems"])
    out = asyncio.run(d.fix("nolang.docx", alt_text=False))
    assert d._docx_default_lang(Document(str(work / out["saved_as"]))) == "en-ZA"


def test_a_file_name_isnt_alt_text():
    for fake in ("image.png", "IMG_2041.JPG", "Picture 3", "photo", ""):
        assert d._no_alt(fake), fake
    assert not d._no_alt("A red blanket on a sofa.")


def test_document_tools_only_go_when_the_words_call_for_them():
    # Economy: about 1,500 tokens, so not with every message (Nov 2026).
    from core import tool_selection
    all_tools = agent_config.list_available_tools()
    names = lambda tools: {t.get("function", t).get("name") for t in tools}
    quiet, _ = tool_selection.select(all_tools, ["what's the weather tomorrow?"])
    assert not names(quiet) & (set(agent.WORK_TOOLS) | {"walkthrough_report"})
    assert "load_tools" in names(quiet)  # the model can still ask for them
    asked, _ = tool_selection.select(all_tools, ["please clean up this Word document"])
    assert set(agent.WORK_TOOLS) <= names(asked)


# ── A book's worth: comments, tracked changes, parts, more steps ───────

def test_editors_comments_can_be_read(work):
    tracked_docx(work / "t.docx")
    out = d.outline("t.docx", show="comments")
    assert out["count"] == 1
    assert out["comments"][0] == {"comment": "0", "author": "Ed", "text": "Is this right?",
                                  "on": "A sentence with a comment.", "paragraph": 1}
    assert "see_also" in d.outline("t.docx")


def test_tracked_changes_are_listed_then_accepted_or_rejected_one_by_one(work):
    tracked_docx(work / "t.docx")
    listed = d.outline("t.docx", show="changes")["changes"]
    assert [(c["change"], c["type"], c["text"], c["author"], c["paragraph"]) for c in listed] == [
        ("1", "inserted", "Added text.", "Ed", 0), ("2", "deleted", "Removed text.", "Ed", 0)]
    out = d.edit("t.docx", [{"accept": ["1"]}, {"reject": [2]}])
    doc = Document(str(work / out["saved_as"]))
    assert doc.paragraphs[0].text == "Keep this. Added text.Removed text."  # the deletion was turned down
    body = doc.element.body
    assert not list(body.iter(qn("w:ins"))) and not list(body.iter(qn("w:del")))
    out2 = d.edit("t.docx", [{"reject": "all"}])
    assert Document(str(work / out2["saved_as"])).paragraphs[0].text == "Keep this. Removed text."


def test_one_comment_can_be_removed_when_dealt_with(work):
    tracked_docx(work / "t.docx")
    out = d.edit("t.docx", [{"remove_comment": "0"}])
    doc = Document(str(work / out["saved_as"]))
    assert not list(doc.element.body.iter(qn("w:commentReference")))
    assert list(doc.comments) == []
    assert doc.paragraphs[1].text == "A sentence with a comment."
    assert d.edit("t.docx", [{"remove_comment": "9"}])["error"].startswith("Nothing was changed")


def test_a_long_document_comes_in_parts(work, monkeypatch):
    monkeypatch.setattr(d, "PART_CHARS", 1000)
    doc = Document()
    for i in range(30):
        doc.add_paragraph(f"Paragraph {i}. " + "words " * 30)
    doc.save(str(work / "book.docx"))
    seen, start, reads = [], 0, 0
    while True:
        part = d.outline("book.docx", from_paragraph=start)
        seen += [p["p"] for p in part["paragraphs"]]
        reads += 1
        if "next_from" not in part:
            break
        assert "Read on with from_paragraph" in part["more"]
        start = part["next_from"]
    assert seen == list(range(30)) and reads > 3


def test_a_long_pdf_comes_in_parts(work, monkeypatch):
    from pypdf import PdfReader, PdfWriter
    one = tiny_pdf(work.parent / "one.pdf")
    writer = PdfWriter()
    for _ in range(3):
        writer.append(PdfReader(str(one)))
    with open(work / "long.pdf", "wb") as f:
        writer.write(f)
    monkeypatch.setattr(d, "PART_CHARS", 20)
    first = d.outline("long.pdf")
    assert [p["page"] for p in first["pages"]] == [1] and first["next_from"] == 2
    assert [p["page"] for p in d.outline("long.pdf", from_paragraph=3)["pages"]] == [3]


def test_document_work_gets_more_steps(work, monkeypatch):
    calls = []

    async def busy_model(messages, tools=None, config=None):
        calls.append(1)
        n = len(calls)
        return {"content": "", "tool_calls": [{"id": f"c{n}", "type": "function", "function": {
            "name": "document_outline", "arguments": json.dumps({"file": f"chapter {n}.docx"})}}],
            "_usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    monkeypatch.setattr(agent, "call_llm", busy_model)
    asyncio.run(agent.run_agent("read the chapters in my work folder"))
    # 15 steps (not 5: a long document takes many), then one call to report back.
    assert len(calls) == 16


# ── After the first chapter (Nov 2026) ─────────────────────────────────

def test_the_whole_font_changes_without_rebuilding(work):
    doc = Document()  # Word's template: body text in a theme font
    doc.add_heading("Chapter", level=1)
    p = doc.add_paragraph()
    p.add_run("Body text.").font.name = "Times New Roman"
    tick = p.add_run("\u2713")
    tick.font.name = "Wingdings"
    doc.save(str(work / "c.docx"))
    out = d.edit("c.docx", [{"font": "Verdana", "size": 12}])
    assert out["changes"] == ["font set to Verdana and body text set to 12 point"]
    fixed = Document(str(work / out["saved_as"]))
    normal = fixed.styles["Normal"].element.rPr.rFonts
    assert normal.get(qn("w:ascii")) == "Verdana" and normal.get(qn("w:asciiTheme")) is None
    for rfonts in fixed.styles.element.iter(qn("w:rFonts")):  # headings too: no theme font left to win
        assert rfonts.get(qn("w:asciiTheme")) is None and rfonts.get(qn("w:ascii")) == "Verdana"
    runs = fixed.paragraphs[1].runs
    assert runs[0].font.name is None and runs[1].font.name == "Wingdings"  # the tick keeps its font
    assert fixed.styles["Normal"].font.size.pt == 12
    assert [p.text for p in fixed.paragraphs] == ["Chapter", "Body text.\u2713"]  # nothing else changed
    assert "isn't a font size" in d.edit("c.docx", [{"size": "huge"}])["error"]


def test_new_documents_go_in_the_folder_named(work):
    (work / "DBSA Manual").mkdir()
    out = d.create("DBSA Manual/Captions", "# Captions\n\n- one\n")
    assert out["saved_as"].replace("\\", "/") == "DBSA Manual/Captions.docx"
    assert d.create("../../outside", "# x")["saved_as"] == "outside.docx"  # never outside the folder
    assert d.create("Nowhere/x", "# x")["saved_as"] == "x.docx"  # a folder that isn't there: the top


def test_picture_tools_stay_within_reach_a_few_messages_later():
    from core import tool_selection
    all_tools = agent_config.list_available_tools()
    names = lambda tools: {t.get("function", t).get("name") for t in tools}
    # Tracy's three messages about Chapter 1: the last two have no document words.
    said = ["Go ahead.", "Please implement all the changes.",
            "There are prompts for generating an image. Please generate it and put it in the right place."]
    sent, _ = tool_selection.select(all_tools, said)
    assert "document_picture" in names(sent)
    src = Path(agent.__file__).read_text(encoding="utf-8")
    assert 'if m.get("role") == "user"][-3:]' in src
    assert "MAKE PICTURES" in tool_selection.GROUP_SUMMARIES["work"]
    assert "Never use it" in agent_config.TOOL_SCHEMAS["document_create"]["description"]


def test_a_word_at_the_end_of_a_sentence_still_counts():
    # "an image." used to be read as "image." and miss the picture tools; the
    # same for "Check my email." and every other group.
    from core import tool_selection
    assert "work" in tool_selection.wanted_groups(["Please generate an image."])
    assert "email" in tool_selection.wanted_groups(["Check my email."])
    assert "calendar" in tool_selection.wanted_groups(["What's on tomorrow?", "Look at my calendar."])
    assert "files" in tool_selection.wanted_groups(["open notes.md"])  # a file name's parts count
    assert "work" in tool_selection.wanted_groups(["Fix Report.docx"])

"""Documents, stage 2: the look check and PDFs (core/doc_render.py),
pictures (core/doc_images.py) and combining PowerPoints."""
import asyncio
import base64
import io
import json
import shutil

import pytest

pytest.importorskip("pptx")
from docx import Document  # noqa: E402
from PIL import Image  # noqa: E402
from pptx import Presentation  # noqa: E402
from pptx.util import Inches  # noqa: E402

from core import agent, doc_images, doc_render, documents as d  # noqa: E402
import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

# The sample files and the work-folder fixture from stage 1's tests.
_spec = importlib.util.spec_from_file_location("_stage1", Path(__file__).with_name("test_documents.py"))
_stage1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_stage1)
png, sample_docx, sample_pptx, tiny_pdf = _stage1.png, _stage1.sample_docx, _stage1.sample_pptx, _stage1.tiny_pdf
work = _stage1.work


def png_bytes(colour=(20, 120, 200), size=(64, 36)):
    b = io.BytesIO()
    Image.new("RGB", size, colour).save(b, "PNG")
    return b.getvalue()


# ── The look check ──────────────────────────────────────────────────────

def test_which_pages():
    assert doc_render._which(None, 20) == [1, 2, 3, 4, 5, 6]
    assert doc_render._which("2-4, 9, 30", 10) == [2, 3, 4, 9]
    assert doc_render._which([1, 1, 3], 5) == [1, 3]


def test_look_sends_the_pages_in_one_call(work, monkeypatch):
    sample_docx(work / "r.docx")
    pdf = tiny_pdf(work.parent / "r.pdf")
    monkeypatch.setattr(doc_render, "available", lambda: True)
    monkeypatch.setattr(doc_render, "_to_pdf", lambda src, out: pdf)
    monkeypatch.setattr(doc_render, "_pages", lambda p, wanted, dpi=70: [(n, png_bytes()) for n in wanted])
    calls = []

    async def fake_llm(messages, tools=None, config=None):
        calls.append(messages)
        return {"content": '```json\n{"pages": [{"page": 1, "looks": "A plain page.", "problems": ["The '
                           'picture covers the heading."]}]}\n```', "_usage": {"prompt_tokens": 900}}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    out = asyncio.run(doc_render.look("r.docx", question="Is the logo too big?"))
    assert out["pages"][0]["problems"] == ["The picture covers the heading."]
    assert out["looked_at"] == [1] and out["pages_in_all"] == 1
    (messages,) = calls
    parts = messages[0]["content"]
    assert "Is the logo too big?" in parts[0]["text"]
    assert parts[2]["image_url"]["url"].startswith("data:image/png;base64,")


def test_look_explains_a_model_that_cant_see(work, monkeypatch):
    tiny_pdf(work / "p.pdf")
    monkeypatch.setattr(doc_render, "_pages", lambda p, wanted, dpi=70: [(1, png_bytes())])

    async def blind(messages, tools=None, config=None):
        return {"content": "NO PICTURE", "_usage": {}}
    monkeypatch.setattr(agent, "call_llm", blind)
    assert "couldn't see the pages" in asyncio.run(doc_render.look("p.pdf"))["error"]


def test_without_libreoffice_it_says_so(work, monkeypatch):
    sample_docx(work / "r.docx")
    monkeypatch.setattr(doc_render, "available", lambda: False)
    monkeypatch.setattr(doc_render, "soffice", lambda: None)
    with pytest.raises(d.Refused, match="needs LibreOffice"):
        asyncio.run(doc_render.look("r.docx"))
    with pytest.raises(d.Refused, match="needs LibreOffice"):
        doc_render.save_pdf("r.docx")


@pytest.mark.skipif(not doc_render.available(), reason="LibreOffice isn't on this computer (it is in FreeHand's image)")
def test_real_pdf_and_pages(work):
    doc = Document()
    doc.core_properties.title = "Real"
    doc.add_heading("Real heading", level=1)
    doc.add_paragraph("Some text.")
    doc.save(str(work / "real.docx"))
    out = doc_render.save_pdf("real.docx")
    assert out["saved_as"] == "real.pdf"
    assert out["accessibility"] == ["Tagged, with a title and language."]
    assert d._pdf_read(work / "real.pdf")["tagged"]
    shots = doc_render._pages(work / "real.pdf", [1])
    assert shots and shots[0][1][:4] == b"\x89PNG"


# ── Pictures ────────────────────────────────────────────────────────────

def test_image_services_come_from_the_ai_model_settings(monkeypatch):
    from core import agent_config
    chain = [{"provider": "ollama_cloud", "base_url": "", "api_key": "k1"},
             {"provider": "openai_compatible", "base_url": "https://api.minimax.io/v1", "api_key": "k2"},
             {"provider": "openrouter", "base_url": "", "api_key": "k3"},
             {"provider": "agnes", "base_url": "https://apihub.agnes-ai.com/v1", "api_key": "k4"},
             {"provider": "ollama", "base_url": "http://x:1/v1", "api_key": ""}]
    monkeypatch.setattr(agent_config, "get_llm_chain", lambda: chain)
    assert [s for s, _, _ in doc_images.services()] == ["agnes", "minimax", "openrouter"]
    monkeypatch.setattr(doc_images, "_settings", lambda: {"image_service": "openrouter"})
    assert [s for s, _, _ in doc_images.services()] == ["openrouter", "agnes", "minimax"]


class FakeResponse:
    def __init__(self, status, data):
        self.status, self._data = status, data

    async def json(self, content_type=None):
        return self._data

    async def read(self):
        return png_bytes()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, answers):
        self.answers, self.sent = answers, []

    def post(self, url, json=None, headers=None):
        self.sent.append((url, json))
        return FakeResponse(*self.answers.pop(0))

    def get(self, url):
        self.sent.append((url, None))
        return FakeResponse(200, None)


def test_each_service_is_asked_the_right_way():
    b64 = base64.b64encode(png_bytes()).decode()
    s = FakeSession([(400, {"error": {"message": "bad size"}}), (200, {"data": [{"b64_json": b64}]})])
    assert asyncio.run(doc_images._agnes(s, "https://a/v1", "k", "a cat", "16:9")) == png_bytes()
    assert s.sent[0][0] == "https://a/v1/images/generations" and s.sent[0][1]["size"] == "1792x1024"
    assert "size" not in s.sent[1][1]  # tried again without the size it didn't like

    s = FakeSession([(200, {"data": {"image_base64": [b64]}, "base_resp": {"status_code": 0}})])
    asyncio.run(doc_images._minimax(s, "https://api.minimax.io/v1", "k", "a cat", "1:1"))
    url, body = s.sent[0]
    assert url == "https://api.minimax.io/v1/image_generation"
    assert body["model"] == "image-01" and body["response_format"] == "base64" and body["aspect_ratio"] == "1:1"

    s = FakeSession([(200, {"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64," + b64}}]}}]})])
    asyncio.run(doc_images._openrouter(s, "https://openrouter.ai/api/v1", "k", "a cat", "16:9"))
    url, body = s.sent[0]
    assert url.endswith("/chat/completions") and body["modalities"] == ["image", "text"]


def test_the_next_service_is_tried_and_reasons_are_given(monkeypatch):
    monkeypatch.setattr(doc_images, "services", lambda: [("agnes", "a", "k"), ("minimax", "m", "k")])

    async def broken(session, base, key, prompt, aspect):
        raise RuntimeError("402: no credit")

    async def works(session, base, key, prompt, aspect):
        return png_bytes()
    monkeypatch.setattr(doc_images, "MAKERS", {"agnes": broken, "minimax": works})
    data, by = asyncio.run(doc_images.make("a blanket"))
    assert by == "MiniMax" and data[:4] == b"\x89PNG"
    monkeypatch.setattr(doc_images, "MAKERS", {"agnes": broken, "minimax": broken})
    with pytest.raises(d.Refused, match="Agnes \\(402: no credit\\); MiniMax"):
        asyncio.run(doc_images.make("a blanket"))
    monkeypatch.setattr(doc_images, "services", lambda: [])
    with pytest.raises(d.Refused, match="None of your AI services can make pictures"):
        asyncio.run(doc_images.make("a blanket"))


def test_a_made_picture_goes_into_a_word_copy_with_alt_text(work, monkeypatch):
    sample_docx(work / "r.docx")

    async def fake_make(prompt, aspect="16:9"):
        return png_bytes(), "Agnes"

    async def fake_describe(image, context=""):
        return "A blue rectangle."
    monkeypatch.setattr(doc_images, "make", fake_make)
    monkeypatch.setattr(d, "describe", fake_describe)
    out = asyncio.run(doc_images.add_picture(prompt="a calm blue banner", file="r.docx", after_paragraph=0))
    assert out["picture"] == "a calm blue banner.png" and out["made_by"] == "Agnes"
    assert out["alt_text"] == "A blue rectangle." and out["saved_as"] == "r (with picture).docx"
    doc = Document(str(work / out["saved_as"]))
    assert d._docx_pictures(doc)[0]["alt_text"] == "A blue rectangle."  # the new one comes first
    assert doc.paragraphs[1].text == "" and list(doc.paragraphs[1]._p.iter(f"{{{d.WP}}}docPr"))


def test_their_own_photo_goes_on_a_new_slide(work):
    sample_pptx(work / "deck.pptx")
    (work / "blanket.jpg").write_bytes(png_bytes(size=(400, 300)))  # a PNG named .jpg still opens
    out = asyncio.run(doc_images.add_picture(picture="blanket", file="deck.pptx",
                                             alt_text="My crochet blanket in blues and greens."))
    prs = Presentation(str(work / out["saved_as"]))
    last = prs.slides[-1]
    assert d._slide_title(last) == "My crochet blanket in blues and greens."
    pics = [s for s in last.shapes if s.shape_type == 13]
    assert d._shape_alt(pics[0]) == "My crochet blanket in blues and greens."
    assert any(f["name"] == "blanket.jpg" and f["kind"] == "picture" for f in d.list_files()["files"])


# ── Combining PowerPoints ──────────────────────────────────────────────

def test_combine_powerpoints_keeps_pictures_and_notes(work):
    sample_pptx(work / "one.pptx")
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "From two"
    s.shapes.add_picture(png(), Inches(5), Inches(2))
    s.notes_slide.notes_text_frame.text = "Remember this."
    prs.save(str(work / "two.pptx"))
    (work / "three.md").write_text("# From notes\n- a point\n", encoding="utf-8")
    out = d.combine(["one.pptx", "two.pptx", "three.md"], "All.pptx", title="All slides")
    combined = Presentation(str(work / out["saved_as"]))
    titles = [d._slide_title(s) for s in combined.slides]
    assert out["slides"] == 4 and titles[:1] == ["Welcome"]
    copied = combined.slides[2]
    assert any(sh.has_text_frame and sh.text_frame.text == "From two" for sh in copied.shapes)
    assert [sh for sh in copied.shapes if sh.shape_type == 13]  # the picture came across
    assert copied.notes_slide.notes_text_frame.text == "Remember this."
    assert any(sh.has_text_frame and sh.text_frame.text == "From notes" for sh in combined.slides[3].shapes)
    assert combined.core_properties.title == "All slides"
    combined.save(io.BytesIO())  # and it saves cleanly


def test_combine_powerpoint_refuses_other_kinds(work):
    sample_pptx(work / "one.pptx")
    tiny_pdf(work / "x.pdf")
    with pytest.raises(d.Refused, match="PowerPoints can be combined with"):
        d.combine(["one.pptx", "x.pdf"], "Mixed.pptx")


def test_new_tools_are_wired():
    from core import agent_config, tool_selection
    for name in ("document_look", "document_save_pdf", "document_picture"):
        assert name in agent.WORK_TOOLS and agent_config.TOOL_REGISTRY[name] == "read"
    assert "work" in tool_selection.wanted_groups(["make a picture for slide 3"])
    said = json.loads(asyncio.run(agent.execute_tool("document_picture", {}))["content"])
    assert "error" in said

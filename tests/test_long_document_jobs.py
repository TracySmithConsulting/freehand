"""Long document jobs (Nov 2026): a 188-paragraph chapter stalled because
each "go ahead" started again from scratch and the steps ran out before
any editing. FreeHand now keeps work notes between messages, keeps the
document tools on while a job is under way, and can change a whole
chapter's headings, case and full stops in one edit."""
import asyncio
import json

import pytest

pytest.importorskip("docx")

from docx import Document  # noqa: E402

from core import agent, conversation, documents as d, tool_selection, work_notes  # noqa: E402


@pytest.fixture
def work(tmp_path, monkeypatch):
    folder = tmp_path / "FreeHand work"
    folder.mkdir()
    monkeypatch.setattr(d, "folder", lambda: folder)
    return folder


def chapter(path):
    doc = Document()
    doc.add_paragraph("CHAPTER 5")                                       # 0
    doc.add_paragraph("COMMUNICATION DEVELOPMENT IN DEAFBLIND INDIVIDUALS")  # 1
    doc.add_paragraph("5.1 EARLY COMMUNICATION AND BONDING")             # 2
    p = doc.add_paragraph()                                              # 3: two runs, one bold
    p.add_run("5.2 ESTABLISHING ")
    p.add_run("COMMUNICATION").bold = True
    doc.add_paragraph("Touch cues.")                                     # 4
    doc.add_paragraph("Object cues.")                                    # 5
    doc.add_paragraph("And so on...")                                    # 6
    doc.save(str(path))


# ── Case and full stops, many paragraphs at once ───────────────────────

@pytest.mark.parametrize("text, how, keep, expected", [
    ("COMMUNICATION DEVELOPMENT IN DEAFBLIND INDIVIDUALS", "sentence", ["DeafBlind"],
     "Communication development in DeafBlind individuals"),
    ("5.3.1 CO-ACTIVE MOVEMENT AND HAND-UNDER-HAND INTERACTION", "sentence", [],
     "5.3.1 Co-active movement and hand-under-hand interaction"),
    ("Communication Development in DeafBlind Individuals", "sentence", [],
     "Communication development in DeafBlind individuals"),  # inner capitals kept when not all caps
    ("CHAPTER 4 – THE SOCIO-ECONOMIC IMPACT OF DEAFBLINDNESS", "title", ["DeafBlindness"],
     "Chapter 4 – The Socio-economic Impact of DeafBlindness"),
    ("WORKING WITH DBSA IN SOUTH AFRICA", "sentence", ["DBSA", "South Africa"],
     "Working with DBSA in South Africa"),
    ("a quiet word", "upper", [], "A QUIET WORD"),
])
def test_recase(text, how, keep, expected):
    assert d._recase(text, how, keep) == expected


def test_a_chapters_headings_in_one_edit(work):
    chapter(work / "c5.docx")
    out = d.edit("c5.docx", [
        {"paragraph": 0, "case": "sentence", "style": "Heading 1"},
        {"paragraphs": "1-3", "style": "Heading 2", "case": "sentence", "keep": ["DeafBlind"]},
        {"paragraphs": [4, 5, 6], "drop_full_stop": True},
    ])
    assert out["changes"] == [
        "paragraph 0 is now Heading 1", "paragraphs 0, 1, 2, 3 in sentence case",
        "paragraphs 1, 2, 3 are now Heading 2", "paragraphs 4, 5: full stop removed"]
    assert out["not_done"] == ["paragraph 6 doesn't end with a full stop"]  # "..." is left alone
    doc = Document(str(work / out["saved_as"]))
    assert [p.text for p in doc.paragraphs] == [
        "Chapter 5", "Communication development in DeafBlind individuals", "5.1 Early communication and bonding",
        "5.2 Establishing communication", "Touch cues", "Object cues", "And so on..."]
    assert [p.style.name for p in doc.paragraphs[:4]] == ["Heading 1", "Heading 2", "Heading 2", "Heading 2"]
    runs = doc.paragraphs[3].runs
    assert runs[1].text == "communication" and runs[1].bold  # formatting stays with its words


def test_many_paragraphs_only_for_changes_that_suit_them(work):
    chapter(work / "c5.docx")
    out = d.edit("c5.docx", [{"paragraphs": [1, 2], "text": "Same words twice"},
                             {"paragraphs": "one to three", "delete": True}])
    assert "take one paragraph" in out["error"] and "isn't a list of paragraph numbers" in out["error"]
    out = d.edit("c5.docx", [{"paragraphs": [4, 5], "delete": True}, {"paragraph": 2, "case": "loud"}])
    assert out["changes"] == ["paragraphs 4, 5 removed"] and "case is" in out["not_done"][0]


# ── Work notes ─────────────────────────────────────────────────────────

def test_work_note_lines():
    read = work_notes.line("document_outline", {"file": "CHP 5 AK.docx"},
                           json.dumps({"file": "DBSA Manual/CHP 5 AK.docx", "paragraph_count": 188}))
    assert read == "read \"DBSA Manual/CHP 5 AK.docx\", 188 paragraphs"
    saved = work_notes.line("document_edit", {"file": "CHP 5 AK.docx"},
                            json.dumps({"saved_as": "CHP 5 AK - edited.docx", "changes": ["a", "b"],
                                        "not_done": ["paragraph 99 doesn't exist"]}))
    assert saved == ("edit: saved \"CHP 5 AK - edited.docx\" from \"CHP 5 AK.docx\" (2 change(s)); "
                     "not done: paragraph 99 doesn't exist")
    failed = work_notes.line("document_edit", {"file": "x.docx"}, json.dumps({"error": "Nothing was changed"}))
    assert failed == "document_edit x.docx: didn't work: Nothing was changed"
    note = work_notes.note([read, read, saved], out_of_steps=True)
    assert note.count("188 paragraphs") == 1 and "The steps ran out" in note and "Don't read again" in note
    assert work_notes.note([], False) == ""


def test_a_note_comes_back_with_its_reply_but_not_to_the_person():
    key = conversation.conversation_key("web", "notes-test")
    conversation.add_turn(key, "Let's do chapter 5", "Here's the plan.", work_note="- read \"CHP 5 AK.docx\"")
    conversation.add_turn(key, "Thanks", "You're welcome.")
    history = conversation.history(key)
    assert history[1]["content"].startswith("Here's the plan.\n\n[Work notes")
    assert "CHP 5 AK.docx" in history[1]["content"] and history[3]["content"] == "You're welcome."
    assert not work_notes.under_way(history)  # the last reply did no document work
    assert work_notes.under_way(history[:2])


def test_a_job_under_way_keeps_the_document_tools():
    tools = [{"type": "function", "function": {"name": n, "parameters": {}}}
             for n in ("document_edit", "work_files", "get_time")]
    sent, _ = tool_selection.select(tools, ["Make the changes you suggested."], by_topic=True)
    assert "document_edit" not in {t["function"]["name"] for t in sent}
    sent, _ = tool_selection.select(tools, ["Make the changes you suggested."], by_topic=True, also=("work",))
    assert "document_edit" in {t["function"]["name"] for t in sent}


def test_go_ahead_carries_on_from_the_notes(work, monkeypatch):
    chapter(work / "c5.docx")
    seen = []

    async def reader(messages, tools=None, config=None):
        # Reads until the steps run out, then reports back.
        seen.append((messages, tools))
        if tools is None:
            return {"content": "I've read the chapter. The plan: headings 1 to 3.", "_usage": {}}
        n = len(seen)
        return {"content": "", "tool_calls": [{"id": f"c{n}", "type": "function", "function": {
            "name": "document_outline", "arguments": json.dumps({"file": "c5.docx", "from_paragraph": n})}}],
            "_usage": {}}
    monkeypatch.setattr(agent, "call_llm", reader)
    asyncio.run(agent.run_agent("Let's move on to chapter 5", source="web", caller_id="go-ahead-test"))
    asked_to_stop = seen[-1][0][-1]["content"]
    assert "exact changes still to make" in asked_to_stop  # so next time can act straight away

    later = []

    async def doer(messages, tools=None, config=None):
        later.append((messages, tools))
        return {"content": "Done.", "_usage": {}}
    monkeypatch.setattr(agent, "call_llm", doer)
    asyncio.run(agent.run_agent("Make the changes you suggested.", source="web", caller_id="go-ahead-test"))
    messages, tools = later[0]
    reply = [m for m in messages if m["role"] == "assistant"][-1]["content"]
    assert reply.startswith("I've read the chapter.") and "The steps ran out" in reply
    assert "read \"c5.docx\" from paragraph 1, 7 paragraphs" in reply
    assert "document_edit" in {t["function"]["name"] for t in tools if t.get("function")}

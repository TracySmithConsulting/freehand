"""Screen reader walkthroughs (core/walkthroughs.py): notes while recording,
by plain code and quietly; then a draft report, transcript and captions,
written beside the recording without changing anything already there."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import agent, agent_config, gateway, people, walkthroughs as wt

THANDI = {"id": "thandi", "name": "Thandi", "telegram": ["555"]}
T0 = datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def place(tmp_path, monkeypatch):
    folder = tmp_path / "Walkthroughs"
    folder.mkdir()
    monkeypatch.setattr(wt, "folder", lambda: folder)
    return folder


@pytest.fixture
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(wt, "_now", lambda: now["t"])

    def at(seconds):
        now["t"] = T0 + timedelta(seconds=seconds)
    return at


def save_settings(**values):
    s = agent_config._load_settings()
    s.update(values)
    agent_config._save_settings(s)


# ── While recording ─────────────────────────────────────────────────────

def test_start_note_stop(place, clock):
    assert "Walkthrough for Acme's app started" in wt.quick_command("start walkthrough for Acme's app")
    clock(252)
    assert wt.quick_command("note: the Stop button has no name") == "Noted at 04:12."
    clock(3725)
    assert wt.quick_command("Note the reply isn't read out") == "Noted at 1:02:05."
    said = wt.quick_command("stop walkthrough")
    assert "with 2 notes" in said and "write the walkthrough report" in said
    (notes,) = place.glob("*notes.md")
    text = notes.read_text(encoding="utf-8")
    assert "- 04:12: the Stop button has no name" in text
    assert "- 1:02:05: the reply isn't read out" in text


def test_a_note_without_a_walkthrough_goes_to_the_agent_as_usual(place, clock):
    assert wt.quick_command("note: buy milk") is None
    assert wt.quick_command("what's the weather?") is None


def test_only_the_owner_uses_walkthroughs(place, clock):
    token = people.set_current("thandi")
    try:
        assert wt.quick_command("start walkthrough") is None
        assert "only for FreeHand's owner" in wt.start_report()["error"]
    finally:
        people.reset(token)


def test_stopping_twice_or_starting_again_is_explained(place, clock):
    assert wt.quick_command("stop walkthrough") == "There's no walkthrough going."
    wt.quick_command("start walkthrough")
    assert "the last one hadn't been stopped" in wt.quick_command("start walkthrough")


def test_saved_notes_never_overwrite_a_file(place, clock):
    for _ in range(2):
        wt.quick_command("start walkthrough for Acme")
        wt.quick_command("note: one")
        wt.quick_command("stop walkthrough")
    assert len(list(place.glob("*notes*.md"))) == 2


def test_quick_commands_use_no_ai(place, clock, monkeypatch):
    async def no_ai(*a, **k):
        raise AssertionError("the AI was called")
    monkeypatch.setattr(agent, "call_llm", no_ai)
    monkeypatch.setattr(agent, "_run_agent", no_ai)
    said = asyncio.run(agent.run_agent("start walkthrough"))
    assert said["text"].startswith("Walkthrough started")


def test_telegram_replies_are_silent_and_skip_the_agent(place, clock, monkeypatch):
    sent, routed = [], []

    async def fake_send(chat_id, text, quiet=False):
        sent.append((chat_id, text, quiet))
        return True

    async def fake_route(text, source, caller_id):
        routed.append(text)
        return "ok"
    monkeypatch.setattr(gateway, "_telegram_send_message", fake_send)
    monkeypatch.setattr(gateway, "_route_to_agent", fake_route)
    monkeypatch.setattr(gateway, "_seen_deliveries", {})
    save_settings(telegram_allow_from=["42"], people=[THANDI])

    def message(n, sender, text):
        async def go():
            await gateway.handle_telegram_update(
                {"update_id": 9100 + n, "message": {"chat": {"id": sender, "type": "private"},
                                                  "from": {"id": sender}, "text": text}})
            await asyncio.gather(*list(gateway._background_tasks))
        asyncio.run(go())

    message(1, 42, "start walkthrough")
    message(2, 42, "note: focus jumps to the top")
    message(3, 555, "note: hello")  # Thandi isn't the owner: an ordinary message
    # The two walkthrough replies are silent; Thandi's ordinary reply isn't.
    assert [q for _, _, q in sent] == [True, True, False]
    assert sent[1][1] == "Noted at 00:00."
    assert routed == ["note: hello"]


# ── Afterwards ──────────────────────────────────────────────────────────

SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "This is a walkthrough of the Acme assistant."},
    {"start": 61.5, "end": 66.0, "text": "Problem: the reply isn't read out at all."},
    {"start": 130.0, "end": 133.0, "text": "Problem."},
    {"start": 133.0, "end": 138.0, "text": "The Stop button just says button."},
    {"start": 200.0, "end": 204.0, "text": "That's the end."},
]


def test_problem_moments_are_found_with_their_times():
    found = wt.markers(SEGMENTS)
    assert [(m["seconds"], m["text"]) for m in found] == [
        (61.5, "the reply isn't read out at all."),
        (130.0, "The Stop button just says button."),
    ]


def test_captions():
    out = wt.srt(SEGMENTS[:2])
    assert out.startswith("1\n00:00:00,000 --> 00:00:04,000\nThis is a walkthrough")
    assert "2\n00:01:01,500 --> 00:01:06,000\nProblem:" in out


def test_notes_are_moved_onto_the_recordings_times(place, clock):
    # "start walkthrough" at 08:00:00; recording started 10 seconds later
    # and ran 300 seconds, so its file was last written at 08:05:10.
    wt.quick_command("start walkthrough")
    clock(130)
    wt.quick_command("note: no name on the Stop button")
    wt.quick_command("stop walkthrough")
    rec = place / "acme.mkv"
    rec.write_bytes(b"x")
    end = (T0 + timedelta(seconds=310)).timestamp()
    os.utime(rec, (end, end))
    notes, close = wt.notes_for(rec, duration=300)
    assert close and notes == [{"seconds": 120.0, "text": "no name on the Stop button"}]


def test_notes_from_another_day_are_left_out(place, clock):
    wt.quick_command("start walkthrough")
    wt.quick_command("note: old")
    wt.quick_command("stop walkthrough")
    rec = place / "later.mp4"
    rec.write_bytes(b"x")
    later = (T0 + timedelta(days=2)).timestamp()
    os.utime(rec, (later, later))
    assert wt.notes_for(rec, duration=300) == ([], True)


def test_the_newest_recording_or_one_by_name(place):
    old, new = place / "first acme.mp4", place / "Recordings" / "second.mkv"
    new.parent.mkdir()
    for i, p in enumerate((old, new)):
        p.write_bytes(b"x")
        os.utime(p, (1_000_000 + i, 1_000_000 + i))
    (place / "notes.md").write_text("not a recording")
    assert wt.find_recording() == new
    assert wt.find_recording("acme") == old


DRAFT = {
    "product": "Acme assistant",
    "tasks": [{"task": "Ask a question", "result": "No"}],
    "summary": "A blind customer can't use the assistant, because replies aren't read out.",
    "works_well": ["Signing up works well with the keyboard."],
    "problems": [{"title": f"Problem {i}", "severity": "Blocker", "where": "Chat", "time": "01:01",
                  "what_happens": "Nothing is read.", "why_it_matters": "They give up.",
                  "how_to_fix": "Use a live region.", "wcag": "4.1.3 Status Messages"} for i in range(1, 8)],
    "other": ["The logo has no description (02:10)."],
}


def test_the_report_is_an_accessible_word_document(tmp_path):
    from docx import Document
    from docx.oxml.ns import qn
    out = wt.write_report(tmp_path / "acme report (draft).docx", DRAFT, "acme.mkv", "Acme", [])
    doc = Document(str(out))
    assert doc.core_properties.title == "Screen reader walkthrough: Acme assistant"
    assert doc.core_properties.language == "en-ZA"
    headings = [(p.style.name, p.text) for p in doc.paragraphs if p.style.name.startswith(("Heading", "Title"))]
    assert headings[0] == ("Title", "Screen reader walkthrough: Acme assistant")
    assert ("Heading 1", "The five biggest problems") in headings
    assert [t for s, t in headings if s == "Heading 2"] == [f"{i}. Problem {i}" for i in range(1, 6)]  # five at most
    for table in doc.tables:  # each table's first row is a header row
        assert table.rows[0]._tr.trPr.find(qn("w:tblHeader")) is not None
    assert "Check every problem against the recording" in doc.paragraphs[1].text


def test_the_whole_job_writes_new_files_beside_the_recording(place, monkeypatch):
    rec = place / "acme.mkv"
    rec.write_bytes(b"x")
    (place / "acme transcript.txt").write_text("already here")
    monkeypatch.setattr(wt, "transcribe_recording", lambda p: {"segments": SEGMENTS, "duration": 205.0})
    sent = []

    async def fake_llm(messages, tools=None, config=None):
        sent.append(messages)
        return {"content": "```json\n" + json.dumps(DRAFT) + "\n```", "_usage": {"prompt_tokens": 2000}}
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    done = asyncio.run(wt.make_report(rec, "Acme assistant", "Acme", ["Ask a question"]))
    assert done["report"] == "acme report (draft).docx" and done["problems"] == 5 and done["marks"] == 2
    assert (place / "acme transcript.txt").read_text() == "already here"  # untouched
    assert (place / "acme transcript (2).txt").read_text(encoding="utf-8").startswith("[00:00] This is")
    assert (place / "acme captions.srt").exists()
    asked = sent[0][1]["content"]
    assert "01:01: the reply isn't read out at all." in asked and "- Ask a question" in asked


def test_report_problems_are_explained(place, monkeypatch):
    monkeypatch.setattr(wt, "folder", lambda: None)
    assert "FREEHAND_WALKTHROUGHS_DIR" in wt.start_report()["error"]
    monkeypatch.setattr(wt, "folder", lambda: place)
    assert "no recording in your Walkthroughs folder" in wt.start_report()["error"]


def test_the_report_runs_in_the_background_and_says_when_its_ready(place, monkeypatch):
    from core import notices
    (place / "acme.mkv").write_bytes(b"x")
    told = []

    async def fake_make(path, product, client, tasks, tell=None):
        return {"report": "acme report (draft).docx", "transcript": "t", "captions": "c",
                "problems": 3, "marks": 2, "notes": 1, "minutes": 25}

    async def fake_deliver(text, title="", target=None, person=None):
        told.append((text, target))
    monkeypatch.setattr(wt, "make_report", fake_make)
    monkeypatch.setattr(notices, "deliver", fake_deliver)

    async def go():
        started = wt.start_report(target={"channel": "telegram", "to": "42"})
        assert started["started"] and "few minutes" in started["tell_the_person"]
        await asyncio.gather(*list(wt._jobs))
    asyncio.run(go())
    (text, target), = told
    assert "acme report (draft).docx" in text and "3 problems" in text
    assert target == {"channel": "telegram", "to": "42"}


def test_the_tool_is_offered_when_walkthroughs_come_up():
    from core import tool_selection
    assert "walkthroughs" in tool_selection.wanted_groups(["write the walkthrough report"])
    assert agent_config.TOOL_REGISTRY["walkthrough_report"] == "read"
    assert "walkthrough_report" in agent_config.TOOL_SCHEMAS


def test_compose_shares_only_the_walkthroughs_folder_for_writing():
    root = Path(__file__).resolve().parent.parent
    for name in ("compose.yml", "install/compose.yml"):
        text = (root / name).read_text(encoding="utf-8")
        assert "${FREEHAND_WALKTHROUGHS_DIR:-./host-folders/no-walkthroughs}:/host/walkthroughs\n" in text
        assert "FREEHAND_WALKTHROUGHS_FROM=${FREEHAND_WALKTHROUGHS_DIR:-}" in text
        assert ":/host/notes:ro" in text  # the other shared folders stay read-only

"""Screen reader walkthroughs: notes while recording, then a draft report.

A walkthrough is a recording of someone using an app or website with a
screen reader, talking through what happens, for a client. FreeHand helps
in two places:

While recording, with plain code (no AI, no waiting):
    "start walkthrough" (or "start walkthrough for Acme's app")
    "note: the Stop button has no name"   -> kept with the time
    "stop walkthrough"
From Telegram the replies are silent, so the phone doesn't make a sound
on the recording. Saying "Problem:" out loud while recording works too:
FreeHand finds it in the transcript, with the exact time.

Afterwards, "write the walkthrough report" (the walkthrough_report tool)
takes the newest recording in the person's Walkthroughs folder and, in
the background:
    1. turns its speech into text on this computer (faster-whisper),
       saved beside it as a transcript and as captions (.srt);
    2. finds the "Problem:" moments and the notes, at their times;
    3. asks the AI model once for a draft of the five biggest problems;
    4. writes an accessible Word document beside the recording;
then says it's ready. Nothing in the folder is changed or deleted: every
file FreeHand writes is new.

The folder: in Docker, /host/walkthroughs (FREEHAND_WALKTHROUGHS_DIR in
.env, the only folder FreeHand may write to outside its vault); outside
Docker, settings "walkthroughs_dir" or Documents/Walkthroughs. Only the
owner uses walkthroughs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

log = logging.getLogger("freehand.walkthroughs")

IN_DOCKER = Path("/host/walkthroughs")
RECORDING_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".m4a", ".mp3", ".wav", ".ogg", ".flv", ".ts"}
MAX_NOTE = 500
MAX_TRANSCRIPT_CHARS = 60_000
NEARBY_SECONDS = 90
MARKER = re.compile(r"\bproblem\b\s*[:,.\-]?\s*(.*)", re.I)

_jobs: set = set()  # report jobs running now (kept so they aren't garbage collected)

# Where this request came from, so a finished report is also sent there.
import contextvars  # noqa: E402
_request: contextvars.ContextVar = contextvars.ContextVar("freehand_walkthrough_request", default=None)


def set_request(source: str, caller_id: str):
    return _request.set({"channel": source, "to": caller_id} if source and caller_id else None)


def request_target() -> Optional[dict]:
    return _request.get()


# ── Where things live ───────────────────────────────────────────────────

def folder() -> Optional[Path]:
    """The person's Walkthroughs folder, or None when FreeHand wasn't given one."""
    if IN_DOCKER.exists():
        shared = os.environ.get("FREEHAND_WALKTHROUGHS_FROM", "").strip()
        return IN_DOCKER if shared and IN_DOCKER.is_dir() else None
    try:
        from core import agent_config
        named = str(agent_config._load_settings().get("walkthroughs_dir") or "")
    except Exception:
        named = ""
    path = Path(named).expanduser() if named else Path.home() / "Documents" / "Walkthroughs"
    return path if path.is_dir() else None


def _state_file() -> Path:
    from core import agent_config
    return Path(agent_config.VAULT_DIR) / "walkthroughs" / "session.json"


def _load() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(state: dict) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def clock(seconds: float) -> str:
    seconds = max(0, int(seconds))  # rounded down: jumping there lands at the start of the moment
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _new_path(base: Path) -> Path:
    """base, or base (2), (3)... so nothing already there is overwritten."""
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = base.with_name(f"{base.stem} ({n}){base.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


# ── While recording: plain code, no AI ─────────────────────────────────

_START = re.compile(r"^\s*start(?:\s+a|\s+the|\s+my)?\s+walk-?through\b[\s:,-]*(?:for\s+)?(.*)$", re.I)
_STOP = re.compile(r"^\s*(?:stop|end|finish)(?:\s+the|\s+my)?\s+walk-?through\b", re.I)
_NOTE = re.compile(r"^\s*note\b\s*[:,.\-]?\s*(.+)$", re.I | re.S)


def _owner() -> bool:
    from core import people
    return people.current() == people.OWNER_ID


def quick_command(text: str) -> Optional[str]:
    """Start, note or stop: the reply, or None when it's not one of these
    (then the message goes to the agent as usual). A note only counts
    while a walkthrough is going, so "note ..." works as before otherwise."""
    if not text or not _owner():
        return None
    start = _START.match(text)
    if start:
        return start_walkthrough(start.group(1).strip(" .") or "")
    if _STOP.match(text):
        return stop_walkthrough()
    note = _NOTE.match(text)
    if note and _load().get("active"):
        return add_note(note.group(1))
    return None


def start_walkthrough(label: str = "") -> str:
    state = _load()
    replaced = state.get("active")
    state["active"] = {"label": label[:120], "started_at": _now().isoformat(), "notes": []}
    _save(state)
    where = f" for {label}" if label else ""
    lead = "I've started a new walkthrough (the last one hadn't been stopped). " if replaced else ""
    return (f"{lead}Walkthrough{where} started. Send \"note:\" and what you noticed at any time, "
            "and \"stop walkthrough\" when you've finished.")


def add_note(text: str) -> str:
    state = _load()
    active = state.get("active")
    if not active:
        return "There's no walkthrough going. Send \"start walkthrough\" first."
    started = datetime.fromisoformat(active["started_at"])
    seconds = (_now() - started).total_seconds()
    active["notes"].append({"seconds": round(seconds, 1), "text": " ".join(text.split())[:MAX_NOTE]})
    _save(state)
    return f"Noted at {clock(seconds)}."


def stop_walkthrough() -> str:
    state = _load()
    active = state.pop("active", None)
    if not active:
        return "There's no walkthrough going."
    active["stopped_at"] = _now().isoformat()
    state["last"] = active
    _save(state)
    notes = active["notes"]
    saved = ""
    place = folder()
    if place and notes:
        stamp = datetime.fromisoformat(active["started_at"]).astimezone().strftime("%Y-%m-%d %H%M")
        name = f"{stamp} {active['label']} notes.md" if active["label"] else f"{stamp} walkthrough notes.md"
        path = _new_path(place / _safe(name))
        lines = [f"# Walkthrough notes: {active['label'] or stamp}", ""]
        lines += [f"- {clock(n['seconds'])}: {n['text']}" for n in notes]
        try:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            saved = f" They're saved in your Walkthroughs folder as \"{path.name}\"."
        except OSError:
            log.warning("couldn't save walkthrough notes to %s", path)
    count = f"{len(notes)} note" + ("" if len(notes) == 1 else "s")
    return (f"Walkthrough stopped, with {count}.{saved} When the recording is saved in your "
            "Walkthroughs folder, ask me to write the walkthrough report.")


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip(" .") or "walkthrough"


# ── Afterwards: the report ──────────────────────────────────────────────

def find_recording(name: str = "") -> Optional[Path]:
    """The recording asked for by name (or part of it), else the newest."""
    place = folder()
    if not place:
        return None
    files = [p for p in place.rglob("*") if p.is_file() and p.suffix.lower() in RECORDING_SUFFIXES]
    if name:
        want = name.lower()
        files = [p for p in files if want in p.name.lower()]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def transcribe_recording(path: Path) -> dict:
    """{"segments": [{start, end, text}], "duration"} or {"error"}. Blocking."""
    from core import voice
    if not voice.available():
        return {"error": "Speech recognition isn't installed in this FreeHand."}
    language = str(voice._settings().get("language") or "").strip() or None
    try:
        parts, info = voice._model().transcribe(str(path), language=language, vad_filter=True, beam_size=1)
        segments = [{"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
                    for s in parts if s.text.strip()]
    except Exception as e:  # a file the decoder can't read
        log.warning("walkthrough transcription failed for %s: %s", path.name, e)
        return {"error": f"FreeHand couldn't read the sound in \"{path.name}\"."}
    if not segments:
        return {"error": f"FreeHand didn't hear any words in \"{path.name}\"."}
    return {"segments": segments, "duration": float(getattr(info, "duration", 0) or segments[-1]["end"])}


def markers(segments: List[dict]) -> List[dict]:
    """Where the person said "Problem: ..." out loud."""
    found = []
    for i, s in enumerate(segments):
        m = MARKER.search(s["text"])
        if not m:
            continue
        said = m.group(1).strip()
        if len(said) < 15 and i + 1 < len(segments):  # it ran on into the next part
            said = (said + " " + segments[i + 1]["text"]).strip()
        found.append({"seconds": s["start"], "text": said or s["text"]})
    return found


def notes_for(path: Path, duration: float) -> tuple:
    """The notes from the walkthrough this recording belongs to, timed on
    the recording, and whether the times are close (True) or rough.

    A recording's file is last written when recording stops, so it began
    about `duration` seconds before that. Notes are timed from "start
    walkthrough", so they're moved by the difference."""
    state = _load()
    session = state.get("active") or state.get("last")
    if not session or not session.get("notes"):
        return [], True
    began = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).timestamp() - duration
    started = datetime.fromisoformat(session["started_at"]).timestamp()
    shift = started - began
    if not (-120 <= shift <= duration + 60):
        return [], True  # a different session from this recording
    timed = [{"seconds": max(0.0, n["seconds"] + shift), "text": n["text"]} for n in session["notes"]]
    return timed, abs(shift) < 600


def transcript_text(segments: List[dict], keep_near: Optional[List[float]] = None) -> str:
    lines = [f"[{clock(s['start'])}] {s['text']}" for s in segments]
    text = "\n".join(lines)
    if len(text) <= MAX_TRANSCRIPT_CHARS or not keep_near:
        return text[:MAX_TRANSCRIPT_CHARS]
    # Too long to send whole: the parts near each problem and note.
    near = [l for s, l in zip(segments, lines) if any(abs(s["start"] - t) <= NEARBY_SECONDS for t in keep_near)]
    return ("(Only the parts near each problem and note.)\n" + "\n".join(near))[:MAX_TRANSCRIPT_CHARS]


def srt(segments: List[dict]) -> str:
    def stamp(t):
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return "\n".join(f"{i}\n{stamp(s['start'])} --> {stamp(s['end'])}\n{s['text']}\n"
                     for i, s in enumerate(segments, 1))


REPORT_PROMPT = """You draft accessibility reports for a screen reader walkthrough: a recording of a blind
tester using a client's app or website with the NVDA screen reader and a braille display, talking through
what happens. You get the transcript (with times), the moments the tester said "Problem:", and notes they
took while recording. Draft the report from that evidence only: never invent a problem, a fix or a detail.

Reply with JSON only, in this shape:
{"product": "...", "tasks": [{"task": "...", "result": "Yes" | "Yes, with difficulty" | "No" | "Not tried"}],
 "summary": "two or three plain sentences",
 "works_well": ["..."],
 "problems": [{"title": "short name", "severity": "Blocker" | "Serious" | "Annoying", "where": "...",
   "time": "mm:ss", "what_happens": "...", "why_it_matters": "...", "how_to_fix": "...",
   "wcag": "number and name, e.g. 4.1.3 Status Messages"}],
 "other": ["smaller problems, one line each, with a time"]}

Rules: at most five problems, the most serious first (Blocker: a blind customer can't finish the task;
Serious: only with a lot of effort or guesswork; Annoying: slow, noisy or confusing). Use the time from the
transcript where the problem happens. Write plainly and kindly, from a blind customer's point of view, in
British English. If you aren't sure of something, put [check] after it, so the tester checks it. Leave a
field as "" rather than guess."""


async def draft(product: str, tasks: List[str], segments: List[dict], marks: List[dict],
                notes: List[dict], rough_times: bool) -> dict:
    """One AI call: the report's content, as a dict, or {"error"}."""
    from core import agent, ai_use
    over = ai_use.refusal()
    if over:
        return {"error": over}
    keep = [m["seconds"] for m in marks] + [n["seconds"] for n in notes]
    parts = [f"Product: {product or '(not given: work it out from the transcript)'}"]
    if tasks:
        parts.append("Tasks agreed with the client:\n" + "\n".join(f"- {t}" for t in tasks))
    if marks:
        parts.append("Where the tester said \"Problem:\":\n" + "\n".join(f"- {clock(m['seconds'])}: {m['text']}" for m in marks))
    if notes:
        how = "about" if rough_times else "at"
        parts.append(f"Notes taken while recording ({how} these times):\n" +
                     "\n".join(f"- {clock(n['seconds'])}: {n['text']}" for n in notes))
    parts.append("Transcript:\n" + transcript_text(segments, keep))
    reply = await agent.call_llm([{"role": "system", "content": REPORT_PROMPT},
                                  {"role": "user", "content": "\n\n".join(parts)}])
    if "error" in reply:
        return {"error": "Your AI service didn't answer, so the report couldn't be drafted. "
                         "The transcript is saved, so you can ask again."}
    u = reply.get("_usage") or {}
    ai_use.count({"calls": 1, "prompt_tokens": int(u.get("prompt_tokens") or 0),
                  "completion_tokens": int(u.get("completion_tokens") or 0)})
    text = agent.strip_reasoning(str(reply.get("content") or ""))
    found = re.search(r"\{.*\}", text, re.S)
    try:
        content = json.loads(found.group(0)) if found else None
    except ValueError:
        content = None
    if not isinstance(content, dict):
        return {"error": "The AI's draft couldn't be read. The transcript is saved, so you can ask again."}
    return content


def write_report(path: Path, content: dict, recording: str, client: str, tasks: List[str]) -> Path:
    """An accessible Word document: real headings, a title, its language,
    tables with header rows. Returns where it was saved."""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def text(v) -> str:
        return " ".join(str(v or "").split())

    product = text(content.get("product")) or "[Product name]"
    doc = Document()
    doc.core_properties.title = f"Screen reader walkthrough: {product}"
    doc.core_properties.author = "Tracy Smith Consulting"
    doc.core_properties.language = "en-ZA"
    rpr = doc.styles.element.find(qn("w:docDefaults"))
    if rpr is not None:
        run_props = rpr.find(qn("w:rPrDefault") + "/" + qn("w:rPr"))
        if run_props is not None:
            lang = run_props.find(qn("w:lang"))
            if lang is None:
                lang = OxmlElement("w:lang")
                run_props.append(lang)
            lang.set(qn("w:val"), "en-ZA")

    def table(rows, header):
        t = doc.add_table(rows=0, cols=len(header))
        t.style = "Table Grid"
        cells = t.add_row().cells
        for c, h in zip(cells, header):
            c.text = h
            for r in c.paragraphs[0].runs:
                r.bold = True
        tr_pr = t.rows[0]._tr.get_or_add_trPr()
        head = OxmlElement("w:tblHeader")
        head.set(qn("w:val"), "true")
        tr_pr.append(head)  # a header row screen readers announce, repeated on each page
        for row in rows:
            for c, v in zip(t.add_row().cells, row):
                c.text = v
        return t

    def field(label, value):
        p = doc.add_paragraph(style="List Bullet")
        p.add_run(f"{label}: ").bold = True
        p.add_run(text(value) or "[fill in]")

    doc.add_heading(f"Screen reader walkthrough: {product}", level=0)
    note = doc.add_paragraph()
    note.add_run("Draft by FreeHand. Check every problem against the recording, then delete this line "
                 "before sending.").italic = True
    for label, value in (("Prepared for", client or "[Client name]"),
                         ("Prepared by", "Tracy Smith, Tracy Smith Consulting"),
                         ("Date", datetime.now().strftime("%d %B %Y").lstrip("0")),
                         ("Recording", recording)):
        p = doc.add_paragraph()
        p.add_run(f"{label}: ").bold = True
        p.add_run(value)

    doc.add_heading("What I tested", level=1)
    table([["Product", product], ["Screen reader", "NVDA [version]"], ["Braille display", "[make and model]"],
           ["Browser", "[browser and version]"], ["Computer", "Windows [version]"]], ["Item", "Details"])
    task_rows = content.get("tasks") if isinstance(content.get("tasks"), list) else []
    if not task_rows and tasks:
        task_rows = [{"task": t, "result": ""} for t in tasks]
    if task_rows:
        doc.add_paragraph("The tasks we agreed:")
        for t in task_rows:
            doc.add_paragraph(text(t.get("task") if isinstance(t, dict) else t), style="List Number")

    doc.add_heading("Summary", level=1)
    doc.add_paragraph(text(content.get("summary")) or "[Two or three sentences in plain words.]")
    if task_rows:
        table([[text(t.get("task")), text(t.get("result")) or "[Yes / Yes, with difficulty / No]"]
               for t in task_rows if isinstance(t, dict)], ["Task", "Could a blind customer do it?"])

    doc.add_heading("What works well", level=1)
    for item in (content.get("works_well") or ["[Something that worked well, so the team keeps doing it.]"]):
        doc.add_paragraph(text(item), style="List Bullet")

    doc.add_heading("The five biggest problems", level=1)
    problems = [p for p in (content.get("problems") or []) if isinstance(p, dict)][:5]
    doc.add_paragraph("These are in order, starting with the one that matters most." if problems else
                      "I didn't find any problems that would stop a blind customer on these tasks.")
    for i, p in enumerate(problems, 1):
        doc.add_heading(f"{i}. {text(p.get('title')) or '[Short name for the problem]'}", level=2)
        field("How serious", p.get("severity"))
        field("Where", p.get("where"))
        field("In the recording", p.get("time"))
        for label, key in (("What happens", "what_happens"), ("Why it matters", "why_it_matters"),
                           ("How to fix it", "how_to_fix"), ("WCAG reference", "wcag")):
            para = doc.add_paragraph()
            para.add_run(f"{label}: ").bold = True
            para.add_run(text(p.get(key)) or "[fill in]")

    other = [text(o) for o in (content.get("other") or []) if text(o)]
    if other:
        doc.add_heading("Other things I noticed", level=1)
        for o in other:
            doc.add_paragraph(o, style="List Bullet")

    doc.add_heading("What this walkthrough covers", level=1)
    doc.add_paragraph(
        f"This walkthrough shows how {product} works for a blind person using a screen reader and a braille "
        "display, on the tasks above. It isn't a full accessibility audit. It doesn't cover every page, "
        "other screen readers, or the needs of people with other disabilities, and it doesn't confirm that "
        "the product meets WCAG.")
    doc.add_heading("Next steps", level=1)
    for line in ("The call: [times offered], to go through this together.",
                 "After fixing: I can check your fixes for [price].",
                 "A full audit: if you'd like every part of the product checked against WCAG 2.2 AA, I can quote for it."):
        doc.add_paragraph(line, style="List Bullet")
    doc.add_paragraph("Thank you for caring about blind customers. Get in touch at tracy@tracysmith.co.za.")

    out = _new_path(path)
    doc.save(str(out))
    return out


async def make_report(recording: Path, product: str = "", client: str = "", tasks: Optional[List[str]] = None,
                      tell: Optional[Callable] = None) -> dict:
    """The whole job: transcript, captions, draft, Word document."""
    tasks = [t for t in (tasks or []) if str(t).strip()]
    heard = await asyncio.to_thread(transcribe_recording, recording)
    if heard.get("error"):
        return heard
    segments, duration = heard["segments"], heard["duration"]
    base = recording.with_suffix("")
    transcript = _new_path(Path(f"{base} transcript.txt"))
    transcript.write_text("\n".join(f"[{clock(s['start'])}] {s['text']}" for s in segments) + "\n", encoding="utf-8")
    captions = _new_path(Path(f"{base} captions.srt"))
    captions.write_text(srt(segments), encoding="utf-8")
    marks = markers(segments)
    notes, close = notes_for(recording, duration)
    content = await draft(product, tasks, segments, marks, notes, rough_times=not close)
    if content.get("error"):
        return {"error": content["error"], "transcript": transcript.name}
    report = await asyncio.to_thread(write_report, Path(f"{base} report (draft).docx"), content,
                                     recording.name, client, tasks)
    return {"report": report.name, "transcript": transcript.name, "captions": captions.name,
            "problems": len([p for p in content.get("problems") or [] if isinstance(p, dict)][:5]),
            "marks": len(marks), "notes": len(notes), "minutes": round(duration / 60)}


def start_report(recording: str = "", product: str = "", client: str = "", tasks=None,
                 target: Optional[dict] = None) -> dict:
    """The walkthrough_report tool: check, then run the job in the background
    and say when it's done (on the web page, and the chat it came from)."""
    if not _owner():
        return {"error": "Walkthrough reports are only for FreeHand's owner."}
    if not folder():
        return {"error": "FreeHand can't see a Walkthroughs folder yet. In FreeHand's .env, set "
                         "FREEHAND_WALKTHROUGHS_DIR to your Walkthroughs folder, then restart FreeHand."}
    path = find_recording(recording)
    if not path:
        return {"error": "There's no recording in your Walkthroughs folder" +
                         (f" with \"{recording}\" in its name." if recording else ".") +
                         " Save it there from OBS or your phone, then ask again."}
    if any(not j.done() for j in _jobs):
        return {"error": "A walkthrough report is already being made. I'll tell you when it's ready."}
    if isinstance(tasks, str):
        tasks = [t.strip() for t in re.split(r"[;\n]", tasks) if t.strip()]

    async def job():
        from core import notices
        try:
            done = await make_report(path, product, client, tasks)
        except Exception:
            log.exception("walkthrough report failed")
            done = {"error": "Something went wrong making the walkthrough report."}
        if done.get("error"):
            text = f"The walkthrough report for \"{path.name}\" couldn't be finished: {done['error']}"
        else:
            found = f"{done['problems']} problem" + ("" if done["problems"] == 1 else "s")
            text = (f"Your walkthrough report draft is ready: \"{done['report']}\", in your Walkthroughs folder. "
                    f"It has {found}, from {done['marks']} \"Problem:\" moments and {done['notes']} notes in "
                    f"{done['minutes']} minutes of recording. The transcript and captions are beside it. "
                    "Check each problem against the recording before you send it.")
        await notices.deliver(text, "Walkthrough report", target=target)

    task = asyncio.get_running_loop().create_task(job())
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)
    size = path.stat().st_size / (1024 * 1024)
    return {"started": True, "recording": path.name, "size_mb": round(size),
            "tell_the_person": f"I'm making the report from \"{path.name}\". Turning the speech into text takes "
                               "a few minutes for a half-hour recording. I'll tell you when it's ready."}

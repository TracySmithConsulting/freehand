"""What FreeHand is doing right now, for the command box (Nov 2026).

A long reply used to leave the console saying "Sending: ..." with the
typed text still in the box until the answer came, so it looked stuck.
Now the console gives each message an id, and while FreeHand works it
notes each step in plain words ("Reading CHP 5 AK.docx", "Searching the
web"). The console asks for them every second or two, shows them, and
reads out only some, so a screen reader isn't flooded.

Kept in memory only, for the person who asked, and dropped after an hour.
"""
from __future__ import annotations

import contextvars
import re
import time
from typing import Optional

_current: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("freehand_progress", default=None)
_runs: dict = {}
KEEP_SECONDS = 3600
MAX_RUNS = 100
MAX_STEPS = 60
ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def valid(run_id) -> bool:
    return isinstance(run_id, str) and bool(ID.match(run_id))


def start(run_id: str, person: str = "owner"):
    """Begin noting steps for this message. Returns a token for finish()."""
    now = time.time()
    for key in [k for k, v in _runs.items() if now - v["updated"] > KEEP_SECONDS]:
        _runs.pop(key, None)
    while len(_runs) >= MAX_RUNS:
        _runs.pop(next(iter(_runs)))
    _runs[run_id] = {"person": person, "steps": [], "done": False, "updated": now}
    return _current.set(run_id)


def finish(token) -> None:
    run_id = _current.get()
    if run_id in _runs:
        _runs[run_id]["done"] = True
        _runs[run_id]["updated"] = time.time()
    _current.reset(token)


def say(text: str) -> None:
    """Note a step, if the console is following this message."""
    run = _runs.get(_current.get() or "")
    if not run or not text:
        return
    if run["steps"] and run["steps"][-1] == text:
        return
    if len(run["steps"]) < MAX_STEPS:
        run["steps"].append(text)
    run["updated"] = time.time()


def get(run_id: str, person: str = "owner") -> dict:
    run = _runs.get(run_id)
    if not run or run["person"] != person:
        return {"steps": [], "done": False, "known": False}
    return {"steps": list(run["steps"]), "done": run["done"], "known": True}


# ── Plain words for each step ──────────────────────────────────────────

def _name(args: dict, *keys: str) -> str:
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            value = value.strip().replace("\\", "/").split("/")[-1]
            return value if len(value) <= 60 else value[:57] + "..."
    return ""


DOCUMENT_STEPS = {
    "work_files": "Looking in your work folder",
    "document_outline": "Reading {file}",
    "document_check": "Checking {file} for accessibility",
    "document_edit": "Editing {file}",
    "document_clean": "Cleaning up {file}",
    "document_combine": "Combining documents",
    "document_create": "Writing {file}",
    "document_fix_accessibility": "Making an accessible copy of {file}",
    "document_look": "Looking at how {file} looks on the page",
    "document_save_pdf": "Saving {file} as a PDF",
}

KEYWORD_STEPS = (
    ("web_search", "Searching the web"),
    ("search_memory", "Checking what FreeHand remembers"),
    ("remember", "Remembering that"),
    ("forget", "Updating what FreeHand remembers"),
    ("send_email", "Sending the email"),
    ("draft", "Writing a draft"),
    ("email", "Checking your email"),
    ("calendar", "Checking your calendar"),
    ("facebook", "Working on Facebook"),
    ("instagram", "Working on Instagram"),
    ("zoom", "Checking Zoom"),
    ("sheet", "Reading the spreadsheet"),
    ("github", "Checking GitHub"),
    ("schedule", "Setting up the scheduled task"),
    ("job", "Checking your scheduled tasks"),
    ("walkthrough", "Working on the walkthrough"),
    ("skill", "Using a skill"),
    ("navigate", "Opening a web page"),
    ("screenshot", "Looking at the screen"),
    ("click", "Working on the web page"),
    ("fill", "Filling in the form"),
)


def describe_tool(name: str, args) -> str:
    """A short, friendly line for a step, or "" for steps not worth saying."""
    args = args if isinstance(args, dict) else {}
    if name in ("load_tools", "ask_user"):
        return ""
    if name == "document_picture":
        into = _name(args, "file")
        return f"Making a picture for {into}" if into else "Making a picture"
    if name in DOCUMENT_STEPS:
        file = _name(args, "file", "output") or "the document"
        if name == "document_create":
            file = _name(args, "output") or "a new document"
        return DOCUMENT_STEPS[name].format(file=file)
    lowered = name.lower()
    for key, said in KEYWORD_STEPS:
        if key in lowered:
            query = str(args.get("query") or "").strip()
            if key == "web_search" and query:
                return f"Searching the web for “{query[:60]}”"
            return said
    words = lowered.replace("_", " ").strip()
    return f"Using {words}" if words else ""

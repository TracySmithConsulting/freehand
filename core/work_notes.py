"""Work notes: what FreeHand did on documents, kept with its reply.

Each message used to start a document job afresh: FreeHand kept only its
replies, not what it had read and saved, so "Go ahead" meant reading the
chapter, the notes and the earlier chapters all over again, and on a long
chapter the steps ran out before any editing. (Chapter 5 of a book, Nov
2026: six "go ahead"s, six reviews, no edits.)

Now each reply that used the work-folder tools keeps a short note: what it
read, what it saved, what didn't work, and whether the steps ran out. The
note goes back to the model with that reply next time (not to the person),
with a reminder to carry on from there rather than start again. And while
a job is under way, the document tools stay on, whatever words the next
message uses ("Make the changes you suggested").
"""
from __future__ import annotations

import json
from typing import Iterable, List

MARK = "[Work notes"
MAX_LINES = 25


def line(name: str, args, content) -> str:
    """One line saying what a work-folder tool did."""
    args = args if isinstance(args, dict) else {}
    try:
        result = json.loads(content) if isinstance(content, str) else {}
    except ValueError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    file = str(args.get("file") or "")
    if result.get("error"):
        return f"{name} {file}: didn't work: {str(result['error'])[:150]}".replace("  ", " ")
    if name == "work_files":
        return "listed the work folder"
    if name == "document_outline":
        said = f"read \"{result.get('file') or file}\""
        show = str(args.get("show") or "text")
        if show != "text":
            said += f" ({show})"
        if args.get("from_paragraph"):
            said += f" from paragraph {args['from_paragraph']}"
        if result.get("paragraph_count"):
            said += f", {result['paragraph_count']} paragraphs"
        if result.get("next_from"):
            said += f"; not all of it: more from paragraph {result['next_from']}"
        return said
    if name == "document_check":
        return f"checked \"{result.get('file') or file}\": {len(result.get('problems') or [])} problem(s)"
    if name == "document_look":
        return f"looked at the pages of \"{file}\""
    saved = result.get("saved_as")
    if saved:
        names = args.get("files") if isinstance(args.get("files"), list) else []
        source = file or ", ".join(str(n) for n in names)
        said = f"{name.replace('document_', '')}: saved \"{saved}\"" + (f" from \"{source}\"" if source else "")
        if isinstance(result.get("changes"), list):
            said += f" ({len(result['changes'])} change(s))"
        if result.get("not_done"):
            said += "; not done: " + "; ".join(str(x) for x in result["not_done"])[:200]
        return said
    if name == "document_picture" and result.get("picture"):
        return f"made the picture \"{result['picture']}\""
    return f"{name} {file}".strip()


def note(log: Iterable[str], out_of_steps: bool) -> str:
    """The note kept with a reply, or "" if no document work was done."""
    lines: List[str] = []
    for entry in log:
        if entry and entry not in lines:
            lines.append(entry)
    if not lines:
        return ""
    if len(lines) > MAX_LINES:
        lines = lines[:5] + [f"... {len(lines) - MAX_LINES + 5} more ..."] + lines[-(MAX_LINES - 6):]
    out = "- " + "\n- ".join(lines)
    if out_of_steps:
        out += "\nThe steps ran out before this was finished."
    out += ("\nNext time: if the person agrees or says go ahead, carry on from here. Make the agreed changes "
            "first, on the newest saved copy, in as few document_edit calls as you can (\"paragraphs\" changes "
            "many at once). Don't read again what's listed above; read only the document you're changing, "
            "and only if you need its paragraph numbers.")
    return out


def as_history(work_note: str) -> str:
    """The note as it's added to the reply in the history sent to the model."""
    if not (work_note or "").strip():
        return ""
    return f"\n\n{MARK} (for you; the person didn't see these):\n{work_note}]"


def under_way(history: list) -> bool:
    """True when FreeHand's last reply did document work."""
    for message in reversed(history or []):
        if message.get("role") == "assistant":
            return MARK in str(message.get("content") or "")
    return False

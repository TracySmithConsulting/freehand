"""Bringing in what ChatGPT or Claude knows about you, from their data
export (the zip file each service emails you), or from their memory list
pasted in.

- **Conversations** become notes in the person's own folder
  (``imports/chatgpt/`` or ``imports/claude/``), one per conversation,
  which FreeHand searches like any note. Free: no AI is used.
- **What the service remembers about you** (its memory list, if the
  export has it, or pasted from its settings) goes into FreeHand's
  memory, marked so it can all be taken out again. FreeHand doesn't learn
  from every conversation: that would cost a lot of AI use for little.
- **Safely:** only the conversations and memory files are read from the
  zip, never unpacked to disk, with size limits so a broken or hostile
  zip can't fill the computer. Lines that look like a password or key
  aren't kept in memory.
- **Each person's own:** a member's import goes in their folder and their
  memories, nobody else's.

Formats (as exported in 2026):

- ChatGPT: ``conversations.json``, a list of conversations, each with a
  ``mapping`` of messages linked by ``parent``, ending at
  ``current_node``.
- Claude: ``conversations.json``, a list of conversations, each with
  ``chat_messages`` (``sender`` "human" or "assistant", ``text``), and,
  where present, a memories file.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SOURCES = {"chatgpt": "ChatGPT", "claude": "Claude"}
MAX_UPLOAD = 1024 * 1024 * 1024          # 1 GiB: exports with pictures are big
MAX_JSON = 400 * 1024 * 1024             # the conversations file itself, unpacked
MAX_MEMORIES = 300
MAX_MESSAGE = 20_000                     # characters kept per message


class ImportError_(ValueError):
    """Something about the file isn't right. Plain words."""


def memory_tag(source: str) -> str:
    return f"import:{source}"


# ── Reading the export ───────────────────────────────────────────────

def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.file_size > MAX_JSON:
        raise ImportError_(f"{Path(info.filename).name} is too big to bring in "
                           f"({info.file_size // (1024 * 1024)} MB unpacked).")
    with zf.open(info) as f:
        data = f.read(MAX_JSON + 1)  # never more than the limit, whatever the zip claims
    if len(data) > MAX_JSON:
        raise ImportError_(f"{Path(info.filename).name} is too big to bring in.")
    return data


def _load_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise ImportError_("FreeHand couldn't read that file. Choose the zip file ChatGPT or Claude sent you, "
                           "or the conversations.json inside it.")


def read_export(f) -> dict:
    """{"source", "conversations": [...], "memories": [...]} from a zip
    export or a conversations.json, read from a file (or bytes) without
    loading a whole big zip into memory."""
    if isinstance(f, (bytes, bytearray)):
        f = io.BytesIO(f)
    conversations, memories = None, []
    f.seek(0)
    if zipfile.is_zipfile(f):
        f.seek(0)
        try:
            zf = zipfile.ZipFile(f)
        except zipfile.BadZipFile:
            raise ImportError_("That zip file is damaged. Download the export again.")
        with zf:
            for info in zf.infolist():
                name = Path(info.filename).name.lower()
                if info.is_dir() or not name.endswith(".json"):
                    continue
                if name == "conversations.json":
                    conversations = _load_json(_read_member(zf, info))
                elif "memor" in name:
                    memories += memory_lines(_load_json(_read_member(zf, info)))
        if conversations is None:
            raise ImportError_("That zip has no conversations.json in it. Choose the export ChatGPT or Claude "
                               "emailed you.")
    else:
        f.seek(0)
        raw = f.read(MAX_JSON + 1)
        if len(raw) > MAX_JSON:
            raise ImportError_("That file is too big to bring in.")
        conversations = _load_json(raw)
    source, parsed = parse_conversations(conversations)
    return {"source": source, "conversations": parsed, "memories": memories[:MAX_MEMORIES]}


def _when(value) -> str:
    """A date, from a Unix time (ChatGPT) or an ISO string (Claude)."""
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
        if isinstance(value, str) and value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        pass
    return ""


def _chatgpt_text(message: dict) -> str:
    content = message.get("content") or {}
    parts = content.get("parts") if isinstance(content, dict) else None
    if isinstance(parts, list):
        return "\n".join(p for p in parts if isinstance(p, str)).strip()
    text = content.get("text") if isinstance(content, dict) else None
    return text.strip() if isinstance(text, str) else ""


def _parse_chatgpt(conv: dict) -> dict:
    mapping = conv.get("mapping") or {}
    node = conv.get("current_node")
    chain = []
    seen = set()
    while node and node in mapping and node not in seen:  # the thread as it ended, not abandoned edits
        seen.add(node)
        chain.append(mapping[node])
        node = mapping[node].get("parent")
    if not chain:  # no current_node: take them in time order
        chain = sorted(mapping.values(), key=lambda n: ((n.get("message") or {}).get("create_time") or 0),
                       reverse=True)
    messages = []
    for n in reversed(chain):
        m = n.get("message") or {}
        role = (m.get("author") or {}).get("role")
        text = _chatgpt_text(m)
        if role in ("user", "assistant") and text:
            messages.append(("you" if role == "user" else "them", text[:MAX_MESSAGE]))
    return {"title": str(conv.get("title") or "").strip() or "Untitled",
            "when": _when(conv.get("create_time")), "id": str(conv.get("id") or conv.get("conversation_id") or ""),
            "messages": messages}


def _parse_claude(conv: dict) -> dict:
    messages = []
    for m in conv.get("chat_messages") or []:
        if not isinstance(m, dict):
            continue
        text = m.get("text") if isinstance(m.get("text"), str) else ""
        if not text.strip():
            text = "\n".join(c.get("text", "") for c in m.get("content") or []
                             if isinstance(c, dict) and c.get("type") == "text")
        sender = m.get("sender")
        if sender in ("human", "assistant") and text.strip():
            messages.append(("you" if sender == "human" else "them", text.strip()[:MAX_MESSAGE]))
    return {"title": str(conv.get("name") or "").strip() or "Untitled",
            "when": _when(conv.get("created_at")), "id": str(conv.get("uuid") or ""), "messages": messages}


def parse_conversations(data) -> tuple:
    if not isinstance(data, list):
        raise ImportError_("That isn't a ChatGPT or Claude conversations file.")
    convs = [c for c in data if isinstance(c, dict)]
    if any("mapping" in c for c in convs):
        return "chatgpt", [p for p in (_parse_chatgpt(c) for c in convs if "mapping" in c) if p["messages"]]
    if any("chat_messages" in c for c in convs):
        return "claude", [p for p in (_parse_claude(c) for c in convs if "chat_messages" in c) if p["messages"]]
    if not convs and isinstance(data, list):
        raise ImportError_("There are no conversations in that file.")
    raise ImportError_("That isn't a ChatGPT or Claude conversations file.")


_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def memory_lines(value) -> list:
    """Short facts from a memory list: pasted text, or a memories file's
    strings, one per line, without bullets and headings."""
    texts = []

    def walk(v):
        if isinstance(v, str):
            texts.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(value)
    out, seen = [], set()
    for text in texts:
        for line in text.splitlines():
            line = _BULLET.sub("", line).strip().strip("*").strip()
            if line.startswith("#") or len(line) < 4 or len(line) > 1000:
                continue
            if line.lower() not in seen:
                seen.add(line.lower())
                out.append(line)
    return out[:MAX_MEMORIES]


# ── Saving it ────────────────────────────────────────────────────────

def _folder(source: str) -> Path:
    from core import scheduler
    return scheduler._workspace() / "imports" / source


def _file_name(conv: dict, taken: set) -> str:
    title = re.sub(r"[^\w .,'()-]", "", conv["title"], flags=re.UNICODE).strip(" .")[:70] or "Untitled"
    base = f"{conv['when']} {title}".strip()
    name, n = f"{base}.md", 2
    while name.lower() in taken:
        name, n = f"{base} ({n}).md", n + 1
    taken.add(name.lower())
    return name


def _note(conv: dict, source: str) -> str:
    who = SOURCES[source]
    lines = [f"# {conv['title']}", "", f"_A conversation with {who}"
             + (f", started {conv['when']}" if conv["when"] else "") + ". Brought in from its export._", ""]
    for role, text in conv["messages"]:
        lines += [f"**{'You' if role == 'you' else who}:**", "", text, ""]
    return "\n".join(lines)


def save(export: dict) -> dict:
    """Write the conversations as notes, and the memory list to memory, for
    the current person. Bringing the same export in again replaces it."""
    from core import long_memory
    source = export["source"]
    folder = _folder(source)
    if folder.exists():
        shutil.rmtree(folder)  # the same export again: replace, don't double up
    folder.mkdir(parents=True)
    taken: set = set()
    for conv in export["conversations"]:
        (folder / _file_name(conv, taken)).write_text(_note(conv, source), encoding="utf-8")
    remembered = {"saved": 0, "left_out": 0}
    if export["memories"]:
        long_memory.forget_tagged(memory_tag(source))
        remembered = long_memory.retain_many(export["memories"], f"Brought in from {SOURCES[source]}",
                                             memory_tag(source))
    _record(source, len(export["conversations"]), folder)
    _sync_notes()
    return {"source": source, "name": SOURCES[source], "conversations": len(export["conversations"]),
            "memories": remembered.get("saved", 0), "left_out": remembered.get("left_out", 0),
            "memory_error": remembered.get("error")}


def save_pasted(source: str, text: str) -> dict:
    from core import long_memory
    if source not in SOURCES:
        raise ImportError_("Choose ChatGPT or Claude.")
    lines = memory_lines(text or "")
    if not lines:
        raise ImportError_("Paste the list of things it remembers, one on each line.")
    result = long_memory.retain_many(lines, f"Brought in from {SOURCES[source]}", memory_tag(source))
    if result.get("error"):
        raise ImportError_(result["error"])
    return {"source": source, "name": SOURCES[source], "memories": result["saved"],
            "left_out": result.get("left_out", 0)}


def imported() -> list:
    """What the current person has brought in."""
    out = []
    for source, name in SOURCES.items():
        folder = _folder(source)
        notes = len(list(folder.glob("*.md"))) if folder.is_dir() else 0
        if notes:
            out.append({"source": source, "name": name, "conversations": notes})
    return out


def remove(source: str) -> dict:
    """Take out everything brought in from this service, for the current
    person: the notes, and the memories."""
    from core import long_memory
    if source not in SOURCES:
        raise ImportError_("Choose ChatGPT or Claude.")
    folder = _folder(source)
    notes = len(list(folder.glob("*.md"))) if folder.is_dir() else 0
    if folder.exists():
        shutil.rmtree(folder)
    forgotten = long_memory.forget_tagged(memory_tag(source))
    _forget_record(source)
    _sync_notes()
    return {"source": source, "conversations": notes, "memories": forgotten,
            "memory_error": None if forgotten is not None else
            f"The notes from {SOURCES[source]} are deleted, but FreeHand's memory couldn't be reached, so what "
            "it brought in is still remembered. Take it out again later."}


def _record(source: str, count: int, folder: Path) -> None:
    """The owner's imports are listed with Hermes and OpenClaw, so FreeHand
    knows they're there (core/agents)."""
    from core import people
    if people.current() != people.OWNER_ID:
        return
    try:
        from core.agents.merger import record_import, remove_import
        from core.agents.models import ImportResult
        remove_import(source)
        result = ImportResult(agent=source, vault_root=folder)
        for f in folder.glob("*.md"):
            result.add_file(str(f), str(f), f.stat().st_size, "conversation")
        record_import(result)
    except Exception:
        pass  # a convenience: the notes are what matter


def _forget_record(source: str) -> None:
    from core import people
    if people.current() != people.OWNER_ID:
        return
    try:
        from core.agents.merger import remove_import
        remove_import(source)
    except Exception:
        pass


def _sync_notes() -> None:
    try:
        from core import memory
        memory.sync_vault_to_sqlite()
    except Exception:
        pass

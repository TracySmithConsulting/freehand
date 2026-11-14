"""Which tools to send the model with each message (Nov 2026: economy).

FreeHand's principle: be economical with the model. Every tool sent with a
message is text the model must read, and that the user pays for. Sending
all of them every time cost about 30,000 tokens per message, most of it
71 Facebook and Instagram tools from Composio that most messages never use.

So tools come in groups:

- FreeHand's own tools are small (about 3,400 tokens in all) and are always
  sent, except in **lean mode** (small and local models), where only the
  core group goes every time and the rest by topic.
- Add-on tools (Composio, OpenConnector discoveries) are sent only when the
  message, or the one before it, mentions their service.
- ``load_tools`` is always there, so the model can ask for a group the
  keywords missed. The agent loop adds that group for the rest of the turn.
"""
from __future__ import annotations

import re
from typing import Iterable

LOAD_TOOLS = "load_tools"

# FreeHand's own tools, by group. Anything not listed is "other" and goes
# with the core group (so a new tool is never silently unavailable).
BUILTIN_GROUPS: dict[str, tuple[str, ...]] = {
    "core": ("remember", "forget_memory", "search_memory", "use_skill", "skill_outcome",
             "save_skill", "list_connections"),
    "email": ("read_email", "send_email"),
    "calendar": ("list_calendar_events", "create_calendar_event"),
    "zoom": ("list_zoom_meetings", "schedule_zoom_meeting"),
    "facebook": ("list_facebook_pages", "post_to_facebook"),
    "instagram": ("list_instagram_accounts", "post_to_instagram"),
    "documents": ("read_docx", "write_docx", "read_xlsx", "read_pptx"),
    "sheets": ("read_sheets", "read_sheet_range"),
    "browser": ("get_axtree", "extract_text", "take_screenshot", "navigate", "click", "fill",
                "get_validation_summary", "save_checkpoint", "restore_checkpoint",
                "delete_checkpoint", "list_checkpoints", "start_request_capture",
                "get_captured_requests", "stop_request_capture"),
}

GROUP_SUMMARIES = {
    "email": "read and send email (Gmail, Outlook)",
    "calendar": "calendar events",
    "zoom": "Zoom meetings",
    "facebook": "Facebook pages and posts",
    "instagram": "Instagram account, posts, comments and messages",
    "documents": "Word, Excel and PowerPoint files",
    "sheets": "Google Sheets",
    "browser": "open and read web pages, fill in forms",
}

# Words that bring a group in. Whole words, case-insensitive.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "email": ("email", "e-mail", "mail", "inbox", "gmail", "outlook", "reply", "send"),
    "calendar": ("calendar", "diary", "meeting", "meetings", "appointment", "appointments",
                 "event", "events", "schedule", "today", "tomorrow", "week", "busy", "free"),
    "zoom": ("zoom",),
    "facebook": ("facebook", "fb", "page", "pages", "meta"),
    "instagram": ("instagram", "insta", "ig", "reel", "reels", "story", "stories"),
    "documents": ("doc", "docx", "word", "document", "documents", "excel", "xlsx", "spreadsheet",
                  "powerpoint", "pptx", "slides", "presentation", "file", "files"),
    "sheets": ("sheet", "sheets", "spreadsheet"),
    "browser": ("website", "web", "site", "page", "url", "link", "http", "https", "www",
                "browse", "browser", "form", "online", "search", "look", "google"),
}

_WORD = re.compile(r"[a-z0-9][a-z0-9.\-']*")


def _words(texts: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for text in texts:
        low = (text or "").lower()
        out.update(_WORD.findall(low))
        if "://" in low or "www." in low:
            out.add("url")
    return out


def wanted_groups(texts: Iterable[str]) -> set[str]:
    words = _words(texts)
    return {g for g, keys in KEYWORDS.items() if words & set(keys)}


def _name(tool: dict) -> str:
    return (tool.get("function") or {}).get("name", "")


def _dynamic_groups() -> dict[str, str]:
    """Add-on tool name -> its service (Composio and OpenConnector tools)."""
    out: dict[str, str] = {}
    try:
        from core.tools import composio_registry
        for t in composio_registry.list_tools():
            out[t["tool_name"]] = str(t.get("service") or "other").lower()
    except Exception:
        pass
    try:
        from core.tools import registry
        for t in registry.list_tools():
            out[t["tool_name"]] = str(t.get("service") or "other").lower()
    except Exception:
        pass
    return out


def group_of(name: str, dynamic: dict[str, str]) -> tuple[str, bool]:
    """(group, is_add_on) for a tool name."""
    if name in dynamic:
        return dynamic[name], True
    for group, names in BUILTIN_GROUPS.items():
        if name in names:
            return group, False
    return "core", False


def load_tools_schema(groups: list[str]) -> dict:
    listed = "; ".join(f"{g}: {GROUP_SUMMARIES.get(g, g + ' tools')}" for g in groups)
    return {
        "type": "function",
        "function": {
            "name": LOAD_TOOLS,
            "description": ("Load more tools when the ones you have can't do what the user asked. "
                            f"Groups: {listed}."),
            "parameters": {
                "type": "object",
                "properties": {"group": {"type": "string", "enum": groups,
                                         "description": "Which group of tools to load"}},
                "required": ["group"],
            },
        },
    }


def select(all_tools: list[dict], texts: Iterable[str], lean: bool = False) -> tuple[list[dict], dict]:
    """The tools to send now, and a plan for load_tools.

    Returns (tools, state) where state holds every group's tools so
    ``add_group`` can load one later in the same turn.
    """
    texts = list(texts)
    wanted = wanted_groups(texts)
    dynamic = _dynamic_groups()
    by_group: dict[str, list[dict]] = {}
    add_on_groups: set[str] = set()
    for tool in all_tools:
        group, add_on = group_of(_name(tool), dynamic)
        by_group.setdefault(group, []).append(tool)
        if add_on:
            add_on_groups.add(group)

    send: list[dict] = []
    loaded: set[str] = set()
    for group, tools in by_group.items():
        always = group == "core" or (not lean and group not in add_on_groups and group in BUILTIN_GROUPS)
        if always or group in wanted:
            send.extend(tools)
            loaded.add(group)
    waiting = sorted(g for g in by_group if g not in loaded)
    if waiting:
        send.append(load_tools_schema(waiting))
    return send, {"by_group": by_group, "loaded": loaded}


def add_group(tools: list[dict], state: dict, group: str) -> tuple[list[dict], str]:
    """Handle a load_tools call: the new tool list and a note for the model."""
    group = (group or "").strip().lower()
    by_group = state.get("by_group", {})
    if group not in by_group:
        return tools, f"There's no tool group called '{group}'. Groups: {', '.join(sorted(by_group))}."
    if group in state["loaded"]:
        return tools, f"The {group} tools are already loaded."
    state["loaded"].add(group)
    added = by_group[group]
    rest = [t for t in tools if _name(t) != LOAD_TOOLS]
    waiting = sorted(g for g in by_group if g not in state["loaded"])
    new = rest + added + ([load_tools_schema(waiting)] if waiting else [])
    names = ", ".join(_name(t) for t in added[:12]) + (" ..." if len(added) > 12 else "")
    return new, f"Loaded {len(added)} {group} tools: {names}. Use them now."


def lean_mode(config: dict) -> bool:
    """Small and local models get lean mode: fewer tools and a shorter
    prompt. Setting llm.lean: "on", "off" or "auto" (the default: on for
    models running on this computer through Ollama)."""
    choice = str(config.get("lean", "auto")).lower()
    if choice in ("on", "true", "1"):
        return True
    if choice in ("off", "false", "0"):
        return False
    from core import llm_providers
    return llm_providers.identify(config.get("provider", ""), config.get("base_url", "")) == "ollama"

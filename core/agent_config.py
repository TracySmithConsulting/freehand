import json
import os
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone


VAULT_DIR = Path(__file__).parent.parent / "vault"
SETTINGS_PATH = VAULT_DIR / "settings.json"


def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text())
        except Exception:
            pass
    return {}


def resolve_public_base_url(settings: dict) -> str:
    """The external https URL people and providers use to reach FreeHand.

    One answer for every caller (OAuth/Composio callbacks, approval links):
      1. env FREEHAND_PUBLIC_BASE_URL   (set by the tenant compose file)
      2. settings["public_base_url"]
      3. settings["oauth"]["public_base_url"]  (older key, still honoured)
    Returns "" when none is set; never has a trailing slash.
    """
    oauth = settings.get("oauth") if isinstance(settings.get("oauth"), dict) else {}
    for value in (
        os.environ.get("FREEHAND_PUBLIC_BASE_URL", ""),
        settings.get("public_base_url", ""),
        oauth.get("public_base_url", ""),
    ):
        value = str(value or "").strip().rstrip("/")
        if value:
            return value
    return ""


def get_llm_config() -> dict:
    settings = _load_settings()
    llm = settings.get("llm", {})
    return {
        "provider": llm.get("provider", "openai"),
        "model": llm.get("model", "gpt-4o-mini"),
        "api_key": llm.get("api_key", ""),
        "base_url": llm.get("base_url", ""),
        "temperature": llm.get("temperature", 0.7),
        "max_tokens": llm.get("max_tokens", 4096),
    }


def resolve_llm_endpoint(config: Optional[dict] = None) -> tuple:
    """(base_url, headers) for the configured model's OpenAI-compatible API.

    Shared by the agent loop and the memory service's pass-through
    (/internal/llm/v1), so both reach the model the same way."""
    if config is None:
        config = get_llm_config()
    from core import llm_providers
    provider = config.get("provider", "openai")
    api_key = config.get("api_key", "")
    # The preset's address when none is saved (core/llm_providers.py).
    base_url = llm_providers.base_url_for(provider, config.get("base_url", ""))
    if provider == "google" and api_key and "{API_KEY}" in base_url:
        base_url = base_url.replace("{API_KEY}", api_key)
        api_key = ""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return base_url.rstrip("/"), headers


def set_llm_config(updates: dict) -> dict:
    settings = _load_settings()
    if "llm" not in settings:
        settings["llm"] = {}
    settings["llm"].update(updates)
    _save_settings(settings)
    return get_llm_config()


# ── Backup models (Nov 2026) ───────────────────────────────────────────
# People can save several models. FreeHand uses the main one (settings
# "llm") and, if it fails - no credit, a usage limit, not responding, a
# rejected key - tries the backups in order (settings "llm_backups").

_MODEL_FIELDS = ("provider", "model", "api_key", "base_url")


def llm_is_configured(config: Optional[dict] = None) -> bool:
    """Has this model been set up enough to try? A key, an address, or a
    service that needs neither (Ollama on this computer)."""
    if config is None:
        config = get_llm_config()
    if str(config.get("api_key", "")).strip() or str(config.get("base_url", "")).strip():
        return True
    from core import llm_providers
    p = llm_providers.get(config.get("provider", ""))
    return bool(p and p.get("no_key"))


def get_llm_backups() -> List[Dict]:
    backups = _load_settings().get("llm_backups") or []
    out = []
    for b in backups if isinstance(backups, list) else []:
        if isinstance(b, dict) and b.get("model"):
            out.append({k: str(b.get(k, "")) for k in _MODEL_FIELDS})
    return out


def _save_llm_backups(backups: List[Dict]) -> None:
    settings = _load_settings()
    settings["llm_backups"] = [{k: b.get(k, "") for k in _MODEL_FIELDS} for b in backups]
    _save_settings(settings)


def get_llm_chain() -> List[Dict]:
    """The main model, then each backup, in the order to try them. Backups
    share the main model's temperature and length settings."""
    main = get_llm_config()
    chain = [main] if llm_is_configured(main) else []
    for b in get_llm_backups():
        cfg = {**main, **b}
        if llm_is_configured(cfg):
            chain.append(cfg)
    return chain


def add_llm_backup(config: Dict) -> List[Dict]:
    backups = get_llm_backups()
    backups.append({k: str(config.get(k, "")).strip() for k in _MODEL_FIELDS})
    _save_llm_backups(backups)
    return backups


def remove_llm_backup(index: int) -> List[Dict]:
    backups = get_llm_backups()
    if 0 <= index < len(backups):
        backups.pop(index)
        _save_llm_backups(backups)
    return backups


def move_llm_backup(index: int, delta: int) -> List[Dict]:
    backups = get_llm_backups()
    new = index + delta
    if 0 <= index < len(backups) and 0 <= new < len(backups):
        backups[index], backups[new] = backups[new], backups[index]
        _save_llm_backups(backups)
    return backups


def promote_llm_backup(index: int) -> List[Dict]:
    """Make a backup the main model; the old main becomes the first backup."""
    backups = get_llm_backups()
    if not 0 <= index < len(backups):
        return backups
    chosen = backups.pop(index)
    old_main = {k: get_llm_config().get(k, "") for k in _MODEL_FIELDS}
    if llm_is_configured(old_main):
        backups.insert(0, old_main)
    set_llm_config(chosen)
    _save_llm_backups(backups)
    return backups


def _save_settings(data: dict) -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data))


def list_available_tools() -> List[Dict]:
    """Build OpenAI-style function-calling schemas from TOOL_REGISTRY.

    B2 fix: previously this was a hardcoded list of 14 tools while
    TOOL_REGISTRY had 24. The LLM could only see 14 — 10 were dead
    code. Now every tool registered in TOOL_REGISTRY with a matching
    entry in TOOL_SCHEMAS automatically becomes visible.

    Adding a new tool: register in TOOL_REGISTRY + add schema in
    TOOL_SCHEMAS. No need to edit this function.
    """
    tools = []
    for name in TOOL_REGISTRY.keys():
        schema = TOOL_SCHEMAS.get(name)
        if schema is None:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": schema["description"],
                "parameters": schema["parameters"],
            },
        })
    return tools


# ── Tool permission registry ─────────────────────────────────────────
# Centralised source of truth for which tools are read-only vs write.
# Permission classes:
#   "read"  — does not change external state; allowed in any tier.
#   "write" — mutates external state (filesystem, OAuth-protected APIs).
#             Requires permission check (intercept_action) unless tier == FULL_ACCESS.
# Keep this in sync with `TOOL_SCHEMAS` below — every entry here MUST
# have a matching schema, or the tool won't appear in the LLM's
# available tools.
TOOL_REGISTRY: Dict[str, str] = {
    # Office
    "read_docx":          "read",
    "write_docx":         "write",
    "read_xlsx":          "read",
    "read_pptx":          "read",
    # Browser v2 (CDP auto-connect, validation, network capture, checkpoints)
    "get_axtree":             "read",
    "extract_text":           "read",
    "take_screenshot":        "read",
    "navigate":              "write",
    "click":                 "write",
    "fill":                  "write",
    "get_validation_summary": "read",
    "start_request_capture":  "read",
    "get_captured_requests": "read",
    "stop_request_capture":  "read",
    "save_checkpoint":       "write",
    "restore_checkpoint":     "write",
    "list_checkpoints":      "read",
    "delete_checkpoint":     "write",
    # Email / calendar / sheets (read-only surface exposed here)
    "read_email":         "read",
    "list_calendar_events": "read",
    "read_sheets":        "read",
    "read_sheet_range":   "read",
    # Zoom
    "list_zoom_meetings":     "read",
    "schedule_zoom_meeting":  "write",
    # Meta
    "list_facebook_pages":    "read",
    "post_to_facebook":       "write",
    "list_instagram_accounts":"read",
    "post_to_instagram":      "write",
    # Meta-utility
    "list_connections":       "read",
    # Memory
    "search_memory":          "read",
    # Long-term memory (Hindsight). Local to this FreeHand and reversible
    # (forgetting is a soft retire), so no approval step.
    "remember":               "read",
    "forget_memory":          "read",
    # Skills (core/skills.py). Local files; new ones are always drafts.
    "use_skill":              "read",
    "skill_outcome":          "read",
    "save_skill":             "read",
}


# ── Tool schemas (LLM-visible parameter definitions) ────────────────────
# Each entry maps tool name -> {"description": str, "parameters": JSON
# Schema dict}. Only tools listed here are visible to the LLM.
# Adding a new tool: add to TOOL_REGISTRY above AND a schema here.
TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    # ── Office ─────────────────────────────────────────────────────
    "read_docx": {
        "description": "Read a .docx file and return its content",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the .docx file"}
            },
            "required": ["path"],
        },
    },
    "write_docx": {
        "description": "Write a .docx file with the given content",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "title": {"type": "string"},
                "content": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["path", "content"],
        },
    },
    "read_xlsx": {
        "description": "Read an .xlsx spreadsheet and return its content",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "read_pptx": {
        "description": "Read a .pptx presentation and return its content",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    # ── Browser v2 ────────────────────────────────────────────────
    "get_axtree": {
        "description": "Get the accessibility tree (AXTree) from a URL. Uses CDP auto-connect to use your existing Chrome session if available (preserves cookies and logins). Falls back to a sandboxed Chromium.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "mode": {"type": "string", "enum": ["headless", "headed"]},
            },
            "required": ["url"],
        },
    },
    "extract_text": {
        "description": "Extract readable text content from a URL. Same browser session as get_axtree.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "mode": {"type": "string", "enum": ["headless", "headed"]},
            },
            "required": ["url"],
        },
    },
    "take_screenshot": {
        "description": "Take a screenshot of a URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "mode": {"type": "string", "enum": ["headless", "headed"]},
                "full_page": {"type": "boolean"},
            },
            "required": ["url"],
        },
    },
    "navigate": {
        "description": "Navigate to a URL and return the AXTree plus a validation summary. Write action — requires approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "mode": {"type": "string", "enum": ["headless", "headed"]},
            },
            "required": ["url"],
        },
    },
    "click": {
        "description": "Click an element on a page and return the result plus a validation summary. Write action — requires approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "selector": {"type": "string"},
                "mode": {"type": "string", "enum": ["headless", "headed"]},
            },
            "required": ["url", "selector"],
        },
    },
    "fill": {
        "description": "Fill a form field and return the result plus a validation summary. Write action — requires approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "selector": {"type": "string"},
                "value": {"type": "string"},
                "mode": {"type": "string", "enum": ["headless", "headed"]},
            },
            "required": ["url", "selector", "value"],
        },
    },
    "get_validation_summary": {
        "description": "Get a structured pass/fail summary of the current page state. Scans for ARIA live regions, error summaries, field-level errors, and pass/fail keyword text. Returns valid (bool|null), field_errors, aria_live_text, http_status, and a recommendation for next action.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "mode": {"type": "string", "enum": ["headless", "headed"]},
            },
            "required": ["url"],
        },
    },
    "start_request_capture": {
        "description": "Start capturing network requests and responses. All requests matching url_pattern (regex or substring) are stored. Call get_captured_requests to retrieve them.",
        "parameters": {
            "type": "object",
            "properties": {
                "url_pattern": {"type": "string", "description": "Regex or substring to filter requests. None captures everything."},
            },
            "required": [],
        },
    },
    "get_captured_requests": {
        "description": "Retrieve the most recent captured network requests since the last start_request_capture call. Each entry includes method, URL, headers, body, response status, headers, and body.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max entries to return (default 50, max 200)."},
            },
            "required": [],
        },
    },
    "stop_request_capture": {
        "description": "Stop capturing network requests.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "list_checkpoints": {
        "description": "List all saved checkpoints with name, URL, timestamp, and size.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "save_checkpoint": {
        "description": "Save the current page state as a named checkpoint. Captures form values, checkbox states, cookies, and localStorage. Essential for iterative form testing — restore to re-enter all data without a page reload.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "url": {"type": "string"},
                "mode": {"type": "string", "enum": ["headless", "headed"]},
            },
            "required": ["name", "url"],
        },
    },
    "restore_checkpoint": {
        "description": "Restore a saved checkpoint by name, re-applying form values, cookies, and localStorage without a page reload.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "url": {"type": "string", "description": "Verify the checkpoint matches this URL. Omit to restore regardless of current URL."},
                "mode": {"type": "string", "enum": ["headless", "headed"]},
            },
            "required": ["name"],
        },
    },
    "delete_checkpoint": {
        "description": "Delete a saved checkpoint by name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    # ── Email / Calendar / Sheets ──────────────────────────────────
    "read_email": {
        "description": "Read emails from a connected email service (google, microsoft, or email)",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": ["google", "microsoft", "email"]},
                "label": {"type": "string", "description": "Account: the OpenConnector connection name for Google/Microsoft (default 'default')"},
                "filter": {"type": "string", "description": "Optional filter query"},
            },
            "required": ["service"],
        },
    },
    "list_calendar_events": {
        "description": "List calendar events from Google or Microsoft",
        "parameters": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "enum": ["google", "microsoft"]},
                "label": {"type": "string", "description": "Account: the OpenConnector connection name for Google/Microsoft (default 'default')"},
                "from": {"type": "string", "description": "Start datetime ISO format"},
                "to": {"type": "string", "description": "End datetime ISO format"},
            },
            "required": ["service"],
        },
    },
    "read_sheets": {
        "description": "Read a Google Sheets spreadsheet metadata",
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Account: the OpenConnector connection name for Google/Microsoft (default 'default')"},
                "spreadsheet_id": {"type": "string"},
            },
            "required": ["spreadsheet_id"],
        },
    },
    "read_sheet_range": {
        "description": "Read a range of cells from a Google Sheets spreadsheet",
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Account: the OpenConnector connection name for Google/Microsoft (default 'default')"},
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string", "description": "A1 notation range, e.g. 'Sheet1!A1:D10'"},
            },
            "required": ["spreadsheet_id", "range"],
        },
    },
    # ── Zoom ───────────────────────────────────────────────────────
    "list_zoom_meetings": {
        "description": "List Zoom meetings for the connected account",
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Account name chosen when connecting (default 'default')"},
                "page_size": {"type": "integer"},
            },
            "required": [],
        },
    },
    "schedule_zoom_meeting": {
        "description": "Schedule a new Zoom meeting (write action — requires approval)",
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Account name chosen when connecting (default 'default')"},
                "title": {"type": "string"},
                "start_time": {"type": "string", "description": "ISO 8601 start datetime"},
                "duration": {"type": "integer", "description": "Duration in minutes (default 60)"},
            },
            "required": ["title", "start_time"],
        },
    },
    # ── Meta (Facebook / Instagram) ───────────────────────────────
    "list_facebook_pages": {
        "description": "List Facebook pages available to the connected account",
        "parameters": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": [],
        },
    },
    "post_to_facebook": {
        "description": "Post content to a Facebook page (write action — requires approval)",
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "page_id": {"type": "string", "description": "Facebook page ID to post to"},
                "content": {"type": "string", "description": "Post text content"},
            },
            "required": ["page_id", "content"],
        },
    },
    "list_instagram_accounts": {
        "description": "Show the connected Instagram professional account",
        "parameters": {
            "type": "object",
            "properties": {"label": {"type": "string", "description": "Account name chosen when connecting (default 'default')"}},
            "required": [],
        },
    },
    "post_to_instagram": {
        "description": "Post a picture to the connected Instagram professional account (write action — requires approval)",
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Account name chosen when connecting (default 'default')"},
                "content": {"type": "string", "description": "Caption text"},
                "media_url": {"type": "string", "description": "Public https address of the image to post"},
            },
            "required": ["media_url"],
        },
    },
    # ── Meta-utility ──────────────────────────────────────────────
    "list_connections": {
        "description": "List all connected services (Google, Microsoft, Zoom, Facebook, Instagram, GitHub) and their status",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    # ── Memory ─────────────────────────────────────────────────────
    "search_memory": {
        "description": (
            "Search the user's scribbles and notes: the scribble pad, Journal, topic threads and "
            "imported notes. Use it when they ask what they jotted, wrote or noted down. "
            "Matches words in any order and returns a snippet of each match."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    "remember": {
        "description": (
            "Save a fact to long-term memory when the user asks you to remember something, "
            "or tells you something lasting about themselves (a preference, a name, a date). "
            "Conversations are also remembered automatically; use this when it matters."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "One clear sentence, e.g. 'The user's sister Anna lives in Durban.'"},
            },
            "required": ["fact"],
        },
    },
    "forget_memory": {
        "description": "Forget a remembered fact when the user asks you to forget or says it is wrong. Tell the user what was forgotten.",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What to forget, e.g. 'where my sister lives'"},
            },
            "required": ["description"],
        },
    },
    "use_skill": {
        "description": "Open a skill from the Skills list to follow its instructions, when a task matches its description.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The skill's name as listed"}},
            "required": ["name"],
        },
    },
    "skill_outcome": {
        "description": "After following a skill, report whether it worked. Be honest: this decides whether the skill is kept.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "worked": {"type": "boolean", "description": "True if the task got done by following the skill"},
                "note": {"type": "string", "description": "Short note on what went wrong or what to improve"},
            },
            "required": ["name", "worked"],
        },
    },
    "save_skill": {
        "description": (
            "Write a new skill: reusable step-by-step instructions for a task. Only when the user asks, "
            "or right after you completed a multi-step task they are likely to repeat. It starts as a draft."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short name, e.g. 'weekly-invoice-summary'"},
                "description": {"type": "string", "description": "One line: when to use this skill"},
                "instructions": {"type": "string", "description": "The steps, in plain Markdown. No passwords or keys."},
            },
            "required": ["name", "description", "instructions"],
        },
    },
}


def get_tool_permission(name: str) -> str:
    """Return the permission class for a tool: 'read' | 'write' | 'unknown'.

    Tools not in the registry are treated as 'unknown' so the agent loop
    fails closed (request approval) rather than allowing unsanctioned writes.
    """
    return TOOL_REGISTRY.get(name, "unknown")


def build_system_prompt(lean: bool = False) -> str:
    """FreeHand's instructions to the model.

    ``lean`` (small and local models, core/tool_selection.py) leaves out
    what the tool descriptions already say and the lists a small model
    doesn't need, so more of its limited context is left for the chat.
    """
    prompt = _build_system_prompt()
    if not lean:
        return prompt
    import re as _re
    # Sections a small model can do without: the tool catalogue (each tool
    # describes itself), imported-file counts, and the tier explanations.
    for heading in ("### Office Tools", "### Browser Tools", "### Email & Calendar",
                    "## Imported Memories (from other agents):"):
        prompt = _re.sub(_re.escape(heading) + r".*?(?=\n#{2,3} |\Z)", "", prompt, flags=_re.S)
    prompt = _re.sub(r"(## Current Tier: [^\n]*\n)(?:- [^\n]*\n)+", r"\1", prompt)
    return _re.sub(r"\n{3,}", "\n\n", prompt).strip() + "\n"


def _build_system_prompt() -> str:
    settings = _load_settings()
    tier = settings.get("tier", "semi_autonomous")
    connected = []
    try:
        from core.oauth.manager import list_connections
        conns = list_connections()
        for c in conns:
            connected.append(f"- {c['service']} ({c['label']})")
    except Exception:
        pass

    try:
        # Accounts connected in OpenConnector (Gmail, Outlook, Zoom, ...).
        from core.oauth import open_connector
        for a in open_connector.list_accounts():
            connected.append(f"- {a['service']} ({a['label']})")
    except Exception:
        pass

    connected_str = "\n".join(connected) if connected else "  (none)"

    try:
        from core import skills
        skills_str = skills.prompt_index() or "  (no skills yet)"
    except Exception:
        skills_str = "  (skills unavailable)"

    # Imported files from other agents: a one-line count per agent, not a
    # list. Listing every file path (5,000+ after a Hermes/OpenClaw import)
    # made this prompt ~140,000 tokens on EVERY message, which is slow and
    # expensive and tells the model nothing it can use. What matters from
    # those files is already in long-term memory, the skills list, and
    # search_memory.
    imported_lines = []
    try:
        from collections import Counter
        from core.agents import list_imported_sources
        counts = Counter(src["agent"] for src in list_imported_sources())
        for agent_name, n in sorted(counts.items()):
            imported_lines.append(f"- {agent_name.capitalize()}: {n} files imported. What was useful in them is in your memory, skills and notes.")
    except Exception:
        pass
    imported_str = "\n".join(imported_lines) if imported_lines else "  (none)"

    return f"""You are FreeHand, a local AI agent that helps users with their tasks.

## Your Capabilities

You have access to the following tool categories:

### Office Tools
- read_docx(path) — Read Word documents
- write_docx(path, title, content) — Write Word documents
- read_xlsx(path) — Read Excel spreadsheets
- read_pptx(path) — Read PowerPoint presentations

### Browser Tools
- get_axtree(url, mode) — Get accessibility tree from a webpage
- extract_text(url, mode) — Extract readable text from a webpage
- take_screenshot(url, mode, full_page) — Screenshot a webpage

### Email & Calendar
- read_email(service, label, filter) — Read emails (google/microsoft/email)
- list_calendar_events(service, label, from, to) — List calendar events
- read_sheets(label, spreadsheet_id) — Read Google Sheets info
- list_zoom_meetings(label, page_size) — List Zoom meetings

### Memory
- Long-term memory is automatic: you remember past conversations, and the
  facts relevant to each message are listed under "What you remember about
  the user" when there are any. Never say your memory is full.
- remember(fact) — Save something the user wants you to remember
- forget_memory(description) — Forget something the user asks you to forget
- search_memory(query, limit) — Search the user's scribbles and notes
  (scribble pad, Journal, topic threads). Use it for "what did I jot down
  about ...?"

### Skills
Skills are saved instructions for tasks. When a task matches one below,
open it with use_skill and follow it, then report with skill_outcome.
Drafts are untested: check their steps as you go. Write a new skill with
save_skill only when asked, or after finishing a multi-step task the user
will likely repeat.
{skills_str}

### Voice
The user can talk to you: the console's Speak button and Telegram voice
notes turn speech into text on this computer, and the console can read
your replies aloud. So you can speak and listen through FreeHand; never
say you have no voice. Speech recognition can mishear, and a screen reader
or other sound in the room can end up in the text. If a message is garbled
or doesn't fit the conversation, don't act on it or take offence: say
briefly what you heard and ask what they meant.

### Connections
- list_connections() — See which services are connected

## Current Tier: {tier}
- autonomous: read-only access
- semi_autonomous: may request approval for writes
- full_access: full access (legacy value "god_mode" still accepted)

## Connected Services:
{connected_str}

## Imported Memories (from other agents):
{imported_str}

## Instructions
- Use tools when the user's request requires external data
- Be concise in your responses
- If a tool call fails, explain what went wrong and suggest next steps
- For write operations, respect the permission tier
- If you need to ask the user for clarification, do so directly
"""

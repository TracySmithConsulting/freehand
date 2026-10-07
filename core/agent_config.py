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


def set_llm_config(updates: dict) -> dict:
    settings = _load_settings()
    if "llm" not in settings:
        settings["llm"] = {}
    settings["llm"].update(updates)
    _save_settings(settings)
    return get_llm_config()


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
#             Requires permission check (intercept_action) unless tier == GOD_MODE.
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
    "list_zoom_recordings":   "read",
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
                "label": {"type": "string", "description": "Connection label, default 'default'"},
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
                "label": {"type": "string"},
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
                "label": {"type": "string"},
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
                "label": {"type": "string"},
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
                "label": {"type": "string"},
                "page_size": {"type": "integer"},
            },
            "required": [],
        },
    },
    "list_zoom_recordings": {
        "description": "List Zoom meeting recordings for the connected account",
        "parameters": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": [],
        },
    },
    "schedule_zoom_meeting": {
        "description": "Schedule a new Zoom meeting (write action — requires approval)",
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
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
        "description": "List Instagram business accounts available to the connected account",
        "parameters": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": [],
        },
    },
    "post_to_instagram": {
        "description": "Post content to an Instagram business account (write action — requires approval)",
        "parameters": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "content": {"type": "string", "description": "Caption text"},
                "media_url": {"type": "string", "description": "Public URL to the image/video to post"},
            },
            "required": [],
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
        "description": "Search the agent's memory (vault notes and scribbles)",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
}


def get_tool_permission(name: str) -> str:
    """Return the permission class for a tool: 'read' | 'write' | 'unknown'.

    Tools not in the registry are treated as 'unknown' so the agent loop
    fails closed (request approval) rather than allowing unsanctioned writes.
    """
    return TOOL_REGISTRY.get(name, "unknown")


def build_system_prompt() -> str:
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

    connected_str = "\n".join(connected) if connected else "  (none)"

    # Gather imported memories
    imported_lines = []
    try:
        from core.agents import list_imported_sources
        sources = list_imported_sources()
        for src in sources:
            imported_lines.append(f"[{src['agent'].upper()}] {src['file_type']}: {src['vault_path']}")
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
- search_memory(query, limit) — Search your notes and scribbles

### Connections
- list_connections() — See which services are connected

## Current Tier: {tier}
- autonomous: read-only access
- semi_autonomous: may request approval for writes
- god_mode: full access

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

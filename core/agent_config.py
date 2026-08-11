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
    tools = []

    # Office tools
    tools.append({
        "type": "function",
        "function": {
            "name": "read_docx",
            "description": "Read a .docx file and return its content",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the .docx file"}
                },
                "required": ["path"],
            },
        },
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "write_docx",
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
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "read_xlsx",
            "description": "Read an .xlsx spreadsheet and return its content",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"],
            },
        },
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "read_pptx",
            "description": "Read a .pptx presentation and return its content",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"],
            },
        },
    })

    # Browser tools
    tools.append({
        "type": "function",
        "function": {
            "name": "get_axtree",
            "description": "Get the accessibility tree (AXTree) from a URL",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "mode": {"type": "string", "enum": ["headless", "headed"]},
                },
                "required": ["url"],
            },
        },
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "extract_text",
            "description": "Extract readable text content from a URL",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "mode": {"type": "string", "enum": ["headless", "headed"]},
                },
                "required": ["url"],
            },
        },
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Take a screenshot of a URL",
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
    })

    # Integration tools
    tools.append({
        "type": "function",
        "function": {
            "name": "read_email",
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
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "list_calendar_events",
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
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "read_sheets",
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
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "list_github_repos",
            "description": "List GitHub repositories for the connected account",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "private": {"type": "boolean"},
                },
                "required": [],
            },
        },
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "list_github_issues",
            "description": "List issues in a GitHub repository",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "repo": {"type": "string", "description": "Format: owner/repo"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"]},
                },
                "required": ["repo"],
            },
        },
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "list_zoom_meetings",
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
    })
    tools.append({
        "type": "function",
        "function": {
            "name": "list_connections",
            "description": "List all connected services and their status",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    })

    # Memory/search
    tools.append({
        "type": "function",
        "function": {
            "name": "search_memory",
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
    })

    return tools


# ── Tool permission registry ─────────────────────────────────────────
# Centralised source of truth for which tools are read-only vs write.
# Permission classes:
#   "read"  — does not change external state; allowed in any tier.
#   "write" — mutates external state (filesystem, OAuth-protected APIs).
#             Requires permission check (intercept_action) unless tier == GOD_MODE.
# Keep this in sync with `list_available_tools()` AND `execute_tool()`.
TOOL_REGISTRY: Dict[str, str] = {
    # Office
    "read_docx":          "read",
    "write_docx":         "write",
    "read_xlsx":          "read",
    "read_pptx":          "read",
    # Browser
    "get_axtree":         "read",
    "extract_text":       "read",
    "take_screenshot":    "read",
    # Email / calendar / sheets (read-only surface exposed here)
    "read_email":         "read",
    "list_calendar_events": "read",
    "read_sheets":        "read",
    "read_sheet_range":   "read",
    # GitHub (read)
    "list_github_repos":  "read",
    "list_github_issues": "read",
    # GitHub (write)
    "create_github_issue":     "write",
    "create_github_pull_request": "write",
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

### GitHub
- list_github_repos(label, private) — List repositories
- list_github_issues(label, repo, state) — List issues in a repo

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

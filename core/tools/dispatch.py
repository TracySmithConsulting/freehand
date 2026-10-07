"""Dynamic OC tool dispatch (Round 11/12 of FreeHand maintenance).

The single entry point the LLM dispatch loop calls when the tool
name starts with ``oc_``. Parses the (service, label, action)
triple from the name, looks up the credential from
credential_store, and routes the call through OpenConnector's
``execute_action`` MCP tool.

Round 12 fix: Round 11's dispatch used call_mcp_action with the
wrong wire format. Round 12 uses execute_action with the label
as connectionName. The OC action id is looked up from the
registry's persisted tool entry (where the registry stored it at
discover_tools time, see core/tools/registry.py).

Static tools (read_docx, navigate, etc.)
do NOT flow through this module - they keep their if/elif
branches in core/agent.execute_tool(). This module is ONLY
for the oc_<service>_<label>_<action> tool names that Round 10
PR 2 ships via the tool registry. The static GitHub tools were
dropped in Round 14 - GitHub now routes through the oc_ path.

Why a dedicated module: keeps the parsing / credential / call
logic in one place, and isolates the OC dependency. The
agent dispatch loop is one elif branch; the heavy lifting
lives here where it's easy to test.

Why last-underscore parsing: the tool name shape is
``oc_<service>_<label>_<action>``. The first underscore is the
separator between ``oc_`` and service. The last underscore is
the separator between label and action. Everything in between
is the label. This handles labels with underscores ("work_v2",
"my_personal") and action IDs with underscores ("get_user_by_id").
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import sys
from typing import Optional

log = logging.getLogger(__name__)

# Public re-export for tests that patch it
from core.oauth.open_connector import execute_action  # noqa: F401


def _ensure_sys_path():
    """Late-bind sys.path so this module works when imported via tests/*."""
    if str(Path(__file__).parent.parent.parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def parse_oc_tool_name(tool_name: str) -> tuple:
    """Parse ``oc_<service>_<label>_<action>`` into (service, label, action).

    The FIRST underscore (after ``oc_``) separates service from label.
    The LAST underscore separates label from action. Everything in
    between is the label. This means:
    - Labels with underscores work ("oc_github_work_v2_repo" → label="work_v2")
    - Action IDs with underscores work ("get_user_by_id" → action="get_user_by_id")
    - Action IDs with colons work too ("reactions:add:name" → action="reactions:add:name")

    Round 13 fix: when the label has NO underscores and the action
    name has underscores (e.g. ``oc_slack_tracy_list_channels``),
    the "last underscore" rule breaks. To disambiguate, look up
    the tool's known (service, label) pair from the registry and
    use it to anchor the split.

    Raises ValueError if the name doesn't start with "oc_" or has
    fewer than 3 parts.
    """
    if not tool_name.startswith("oc_"):
        raise ValueError(f"not an oc_ tool: {tool_name!r}")
    rest = tool_name[3:]  # strip "oc_"
    # Split off service (first underscore) and action (last underscore).
    # The label is everything in between.
    first_us = rest.find("_")
    if first_us < 0:
        raise ValueError(f"malformed oc_ tool name: {tool_name!r}")
    service = rest[:first_us]
    after_service = rest[first_us + 1:]
    last_us = after_service.rfind("_")
    if last_us < 0:
        raise ValueError(f"malformed oc_ tool name: {tool_name!r}")
    candidate_label = after_service[:last_us]
    candidate_action = after_service[last_us + 1:]
    if not service or not candidate_label or not candidate_action:
        raise ValueError(f"malformed oc_ tool name: {tool_name!r}")
    # Try the registry-anchored split: if the label matches a known
    # label for this service, use it. Otherwise fall back to the
    # "last underscore" heuristic.
    _ensure_sys_path()
    from core.tools import registry as _registry
    known_labels = _registry._known_labels_for(service)
    if known_labels and candidate_label not in known_labels:
        # Try to find a label that, when stripped, leaves a valid
        # action. Sort labels longest-first to prefer multi-word labels
        # like "work_v2" over single-word matches.
        for label in sorted(known_labels, key=len, reverse=True):
            prefix = f"{service}_{label}_"
            if rest.startswith(prefix):
                return service, label, rest[len(prefix):]
    return service, candidate_label, candidate_action


def get_oc_action_id(tool_name: str) -> Optional[str]:
    """Read the OC action id for a tool_name from the registry's persisted store.

    The registry (core/tools/registry.py) writes one tool_registry.json
    entry per (service, label) pair. Each entry has:
      - tool_name: the FreeHand-side name (oc_<svc>_<label>_<action>)
      - oc_action_id: the OC-side id (e.g. 'slack.list_channels')
      - schema, risk, etc.

    The translation (authorizationOptions[].id -> service.action_name)
    happens at discover_tools() time via OC's search_actions endpoint.
    This function is the dispatch-side reader.

    Returns None if the tool isn't in the registry (the LLM typed a
    name that wasn't registered, or the registry hasn't been bootstrapped).
    """
    _ensure_sys_path()
    from core.tools import registry as _registry
    storage = _registry._read_storage()
    for t in storage.get("tools", []):
        if t.get("tool_name") == tool_name:
            return t.get("oc_action_id")
    return None


def _get_credential_for_dispatch(service: str, label: str) -> Optional[dict]:
    """Read the credential for (service, label) from credential_store.

    Returns the full entry dict (with 'secret', 'auth_type', etc.)
    or None. The secret is NOT passed to OC - OC has its own
    connections table. The credential_store entry is checked for
    existence so the dispatch can return a clean no_credential
    error, and for the audit trail (which account is being used).
    """
    from core.oauth import credential_store
    return credential_store.get(service, label)


def dispatch_oc_tool(tool_name: str, args: dict) -> dict:
    """Dispatch one oc_<service>_<label>_<action> tool call to OC.

    Returns a dict shaped for the LLM dispatch loop:
      On success: {"ok": True, "content": <json string from OC>}
      On error:   {"ok": False, "error": {"code": <str>, "message": <str>}}

    Error codes (Round 12):
      "malformed_name"  - tool name didn't match oc_<svc>_<label>_<action>
      "unknown_tool"    - tool_name not in registry (no oc_action_id)
      "no_credential"   - no entry in credential_store for (service, label)
      "oc_unreachable"  - OC's MCP endpoint timed out / refused / no token
      "oc_error"        - OC returned an error envelope (passthrough)
    """
    try:
        service, label, _authopt_id = parse_oc_tool_name(tool_name)
    except ValueError as e:
        return {"ok": False, "error": {"code": "malformed_name", "message": str(e)}}

    oc_action_id = get_oc_action_id(tool_name)
    if oc_action_id is None:
        return {
            "ok": False,
            "error": {
                "code": "unknown_tool",
                "message": (
                    f"Tool '{tool_name}' is not registered. "
                    f"Run 'freehand tools refresh' to discover tools from OpenConnector."
                ),
            },
        }

    cred = _get_credential_for_dispatch(service, label)
    if cred is None:
        return {
            "ok": False,
            "error": {
                "code": "no_credential",
                "message": (
                    f"No credential registered for {service}/{label}. "
                    f"Use 'freehand credential add {service} --token <key>' "
                    f"or run 'freehand shared-app add {service}' to start the OAuth dance."
                ),
            },
        }

    try:
        result = execute_action(oc_action_id, args, connection_name=label)
    except Exception as e:
        log.warning("dispatch_oc_tool %s: execute_action raised: %s", tool_name, e)
        return {
            "ok": False,
            "error": {"code": "oc_unreachable", "message": str(e)[:200]},
        }

    if result is None:
        return {
            "ok": False,
            "error": {
                "code": "oc_unreachable",
                "message": "OpenConnector did not respond. Is it running?",
            },
        }

    if not result.get("ok", False):
        # OC returned an error envelope - pass it through.
        return {"ok": False, "error": result.get("error", {
            "code": "oc_error", "message": "OC call failed",
        })}

    # Successful call - result.data is the OC payload.
    data = result.get("data", {})
    content = json.dumps(data, default=str) if not isinstance(data, str) else data
    return {"ok": True, "content": content}

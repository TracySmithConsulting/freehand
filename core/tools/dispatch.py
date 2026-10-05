"""Dynamic OC tool dispatch (Round 11 of FreeHand maintenance).

The single entry point the LLM dispatch loop calls when the tool
name starts with ``oc_``. Parses the (service, label, action)
triple from the name, looks up the credential from
credential_store, and routes the call through OpenConnector's
MCP endpoint.

Static tools (read_docx, navigate, list_github_repos, etc.)
do NOT flow through this module — they keep their if/elif
branches in core/agent.execute_tool(). This module is ONLY
for the oc_<service>_<label>_<action> tool names that Round 10
PR 2 ships via the tool registry.

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
from typing import Optional

log = logging.getLogger(__name__)

# Public re-export for tests that patch it
from core.oauth.open_connector import call_mcp_action  # noqa: F401


def parse_oc_tool_name(tool_name: str) -> tuple:
    """Parse ``oc_<service>_<label>_<action>`` into (service, label, action).

    The FIRST underscore (after ``oc_``) separates service from label.
    The LAST underscore separates label from action. Everything in
    between is the label. This means:
    - Labels with underscores work ("oc_github_work_v2_repo" → label="work_v2")
    - Action IDs with underscores work ("get_user_by_id" → action="get_user_by_id")
    - Action IDs with colons work too ("reactions:add:name" → action="reactions:add:name")

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
    label = after_service[:last_us]
    action = after_service[last_us + 1:]
    if not service or not label or not action:
        raise ValueError(f"malformed oc_ tool name: {tool_name!r}")
    return service, label, action


def _get_credential_for_dispatch(service: str, label: str) -> Optional[dict]:
    """Read the credential for (service, label) from credential_store.

    Returns the full entry dict (with 'secret', 'auth_type', etc.)
    or None if no credential is registered. The dispatch module
    doesn't care about the secret content — OC's MCP endpoint
    knows how to handle its own auth — but we check the credential
    exists so we can return a useful no_credential error envelope.

    Imported lazily so this module can be loaded without dragging
    in credential_store's Fernet dependency.
    """
    from core.oauth import credential_store  # noqa: E402
    return credential_store.get(service, label)


def dispatch_oc_tool(tool_name: str, args: dict) -> dict:
    """Dispatch one oc_<service>_<label>_<action> tool call to OC.

    Returns a dict shaped for the LLM dispatch loop:
      On success: {"ok": True, "content": <json string from OC>}
      On error:   {"ok": False, "error": {"code": <str>, "message": <str>}}

    Error codes:
      "malformed_name" — tool name didn't match the oc_<svc>_<label>_<action> shape
      "no_credential"  — no entry in credential_store for (service, label)
      "oc_error"       — OC returned an error envelope (passthrough)
      "oc_unreachable" — OC's MCP endpoint timed out / refused
      "exception"      — anything else (caught broadly so the LLM
                         doesn't see Python tracebacks)
    """
    try:
        service, label, action = parse_oc_tool_name(tool_name)
    except ValueError as e:
        return {"ok": False, "error": {"code": "malformed_name", "message": str(e)}}

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

    # Inject the secret as an extra argument so OC's MCP endpoint
    # can authenticate the call. The endpoint is responsible for
    # using it (or ignoring it if the connection's already
    # authenticated upstream).
    call_args = dict(args)
    call_args["__credential__"] = cred.get("secret")

    try:
        result = call_mcp_action(action, call_args)
    except Exception as e:
        log.warning("dispatch_oc_tool %s: OC call raised: %s", tool_name, e)
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

    # OC's MCP endpoint returns either {"result": ...} on success or
    # {"error": {...}} on failure. The Round 7 call_mcp_action helper
    # parses both into the envelope we use here.
    if "error" in result:
        return {"ok": False, "error": result["error"]}

    # Successful call — result is the OC payload. Wrap it as a JSON
    # string for the LLM dispatch loop's contract.
    content = result.get("result", result)
    if not isinstance(content, str):
        content = json.dumps(content, default=str)
    return {"ok": True, "content": content}

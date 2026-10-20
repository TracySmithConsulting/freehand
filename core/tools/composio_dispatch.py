"""Execution bridge from FreeHand dynamic tools to Composio."""
from __future__ import annotations

import json


async def dispatch_composio_tool(tool_name: str, args: dict) -> dict:
    from core.oauth import composio
    from core.tools import composio_registry

    record = next((item for item in composio_registry._read_storage().get("tools", []) if item.get("tool_name") == tool_name and item.get("enabled")), None)
    if not record:
        return {"ok": False, "error": {"code": "unknown_tool", "message": f"Composio tool not registered: {tool_name}"}}

    account = composio.get_account(record["service"], record.get("label", "default"))
    if not account or not account.get("connected_account_id"):
        return {"ok": False, "error": {"code": "not_connected", "message": f"No active Composio account for {record['service']}"}}

    try:
        response = await composio.execute_tool(
            record["composio_tool_slug"],
            account["connected_account_id"],
            args,
            user_id=account.get("user_id", ""),
        )
    except composio.ComposioError as exc:
        return {"ok": False, "error": {"code": "composio_error", "message": str(exc)}}

    if response.get("successful") is False or response.get("error"):
        return {"ok": False, "error": {"code": "composio_execution_failed", "message": str(response.get("error") or response)}}
    data = response.get("data", response)
    return {"ok": True, "content": json.dumps(data, default=str)}

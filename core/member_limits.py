"""What a member may ask FreeHand to do (docs/design/people.md, step 6).

A member (anyone but the owner) works with their own things and the
shared ones (core/people.py, core/accounts.py). On top of that:

- **The browser's saved states and request recordings are the owner's.**
  A saved state (a checkpoint) holds a site's sign-in, and a recording
  can hold the sign-in headers of whatever the browser fetched, so a
  member can't save, restore, list or delete checkpoints, or record
  requests.
- **A member's browser is a clean one.** It never drives the owner's own
  Chrome, with the owner's sign-ins (core/tools/browser.py).
- **A member's documents stay in their own folder** (core/tools/office.py).
- **A step that changes something on a shared account** (sending from
  info@, posting on the office page) waits for the owner's approval,
  even at Full access, and the member can't approve it themselves. They
  can still cancel it.

Things a member can't do in the console (settings, the AI model, the
security level, connections, people) are refused there (server.py,
MEMBER_ROUTES); there's no chat tool that changes them.
"""
from __future__ import annotations

from typing import Optional

OWNER_ONLY_TOOLS = {
    "save_checkpoint": "save the browser's state",
    "restore_checkpoint": "bring back a saved browser state",
    "list_checkpoints": "list the saved browser states",
    "delete_checkpoint": "delete a saved browser state",
    "start_request_capture": "record what the browser fetches",
    "get_captured_requests": "read what the browser recorded",
    "stop_request_capture": "stop recording what the browser fetches",
}

NEEDS_OWNER = "needs_owner"


def is_member(person_id: Optional[str] = None) -> bool:
    from core import people
    return (person_id or people.current()) != people.OWNER_ID


def _owner_name() -> str:
    from core import people
    return people.list_people()[0]["name"]


def refusal(tool_name: str, args: Optional[dict] = None) -> Optional[str]:
    """Why a member may not use this tool at all, or None."""
    if not is_member() or tool_name not in OWNER_ONLY_TOOLS:
        return None
    return (f"Only {_owner_name()} can ask FreeHand to {OWNER_ONLY_TOOLS[tool_name]}: "
            "saved browser states can hold someone's sign-ins.")


def needs_owner_approval(tool_name: str, args: Optional[dict] = None) -> bool:
    """A member's change on a shared account: the owner approves it."""
    if not is_member():
        return False
    from core import accounts
    from core.agent_config import get_tool_permission
    account = accounts.for_tool(tool_name, args)
    if not account or accounts.owner_of(account) != accounts.SHARED:
        return False
    from core.tools import composio_registry, registry
    return (get_tool_permission(tool_name) != "read" or registry.is_destructive_tool(tool_name)
            or composio_registry.is_destructive_tool(tool_name))


def waiting_message() -> str:
    return (f"This uses a shared account, so {_owner_name()} approves it. "
            f"FreeHand will do it once {_owner_name()} says yes.")


def approval_refusal(row: Optional[dict], action: str) -> Optional[str]:
    """A member may cancel their own shared-account step, not approve it.
    A child's step that needs a parent: only a parent (or the owner)
    approves it (core/children.py)."""
    if action != "approve" or not is_member() or not row:
        return None
    import json
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        payload = {}
    if payload.get(NEEDS_OWNER):
        from core import children
        if payload.get("parents") is not None and children.may_approve_for(str(row.get("person") or "")):
            return None  # a parent approving their child's step
        if payload.get("parents") is not None:
            return "Only a parent can approve this."
        return f"Only {_owner_name()} can approve this: it uses a shared account."
    return None

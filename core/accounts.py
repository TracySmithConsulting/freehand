"""Whose a connected account is (docs/design/people.md, step 5).

A connected account (a mailbox, a calendar, a Slack, an Instagram) is one
person's, or shared with everyone on this FreeHand:

- **One person's**: only FreeHand working for that person may use it.
  Thandi can't ask FreeHand to read the owner's email, and the owner can't
  read Thandi's.
- **Shared**: anyone may use it, for example the office mailbox (info@)
  or the office calendar.

Every account is the owner's until the owner says otherwise on the
Connections screen, so nothing becomes shared by accident: accounts
connected before FreeHand had people were all the owner's.

An account is named by where it lives and its label (the name it was
given when it was connected):

- ``oc:<app>:<label>``: connected in OpenConnector (Gmail, Outlook, Google
  Calendar, Zoom, Slack, GitHub...). FreeHand's own email and calendar
  tools use these too.
- ``composio:<service>:<label>``: connected through Composio (Instagram).
- ``app:<service>:<label>``: FreeHand's own sign-ins (Facebook, GitHub)
  and email on your own domain (``app:email:<label>``).

The check is made twice: before FreeHand asks anyone to approve a step
(so nobody approves something that can't run), and where each account
is actually used (so no route gets past it).
"""
from __future__ import annotations

from typing import Optional

SHARED = "shared"
SETTING = "account_owners"

# FreeHand's own tools that take a service and a label, and the kind of
# OpenConnector app each uses (core/tools/integrations.py, OC_APPS).
_TOOL_KINDS = {
    "read_email": "email", "send_email": "email",
    "list_calendar_events": "calendar", "create_calendar_event": "calendar",
    "read_sheets": "sheets", "read_sheet_range": "sheets",
    "list_zoom_meetings": "meetings", "schedule_zoom_meeting": "meetings",
}
_TOOL_DEFAULT_SERVICE = {"list_zoom_meetings": "zoom", "schedule_zoom_meeting": "zoom"}
_COMPOSIO_TOOLS = {"list_instagram_accounts": "instagram", "post_to_instagram": "instagram"}
_APP_TOOLS = {"list_facebook_pages": "facebook", "post_to_facebook": "facebook"}


def key(kind: str, service: str, label: Optional[str] = None) -> str:
    return f"{kind}:{str(service or '').strip().lower()}:{str(label or 'default').strip().lower()}"


def _settings() -> dict:
    from core import people
    return people._settings()


def owners(settings: Optional[dict] = None) -> dict:
    s = settings if settings is not None else _settings()
    raw = s.get(SETTING)
    return {str(k).lower(): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def owner_of(account: str, settings: Optional[dict] = None) -> str:
    """The person id it belongs to, or SHARED. The owner's unless set."""
    from core import people
    return owners(settings).get(account.lower(), people.OWNER_ID)


def may_use(account: Optional[str], person_id: Optional[str] = None, settings: Optional[dict] = None) -> bool:
    if not account:
        return True
    from core import people
    whose = owner_of(account, settings)
    return whose == SHARED or whose == (person_id or people.current())


def describe(account: str) -> str:
    """'Gmail (default)', for messages."""
    try:
        kind, service, label = account.split(":", 2)
    except ValueError:
        return account
    from core.tools.integrations import OC_APP_NAMES
    name = OC_APP_NAMES.get(service) or service.replace("_", " ").title()
    if kind == "app" and service == "email":
        name = "Email"
    return f"{name} ({label})"


def refusal(account: Optional[str], person_id: Optional[str] = None) -> Optional[str]:
    """Why FreeHand may not use this account for this person, or None."""
    if may_use(account, person_id):
        return None
    from core import people
    s = _settings()
    whose = owner_of(account, s)
    holder = people.get(whose, s)
    name = holder["name"] if holder else "someone else"
    return (f"The {describe(account)} account is {name}'s, so FreeHand can't use it for you. "
            f"Ask the owner to share it, or to connect one of your own.")


def for_tool(tool_name: str, args: Optional[dict] = None) -> Optional[str]:
    """The account a tool call would use, or None if it uses none."""
    args = args or {}
    label = args.get("label") or "default"
    if tool_name in _TOOL_KINDS:
        service = args.get("service") or _TOOL_DEFAULT_SERVICE.get(tool_name, "google")
        if service == "email":
            return key("app", "email", label)
        from core.tools.integrations import OC_APPS
        app = OC_APPS.get((service, _TOOL_KINDS[tool_name]))
        return key("oc", app, label) if app else None
    if tool_name in _COMPOSIO_TOOLS:
        return key("composio", _COMPOSIO_TOOLS[tool_name], label)
    if tool_name in _APP_TOOLS:
        return key("app", _APP_TOOLS[tool_name], label)
    if tool_name.startswith("oc_"):
        from core.tools import registry
        record = next((t for t in registry.list_tools() if t.get("tool_name") == tool_name), None)
        return key("oc", record["service"], record.get("label")) if record else None
    if tool_name.startswith("co_"):
        from core.tools import composio_registry
        record = next((t for t in composio_registry.list_tools() if t.get("tool_name") == tool_name), None)
        return key("composio", record["service"], record.get("label")) if record else None
    return None


def tool_refusal(tool_name: str, args: Optional[dict] = None) -> Optional[str]:
    return refusal(for_tool(tool_name, args))


def usable_tools(schemas: list) -> list:
    """The tool list without account tools this person may not use, so
    FreeHand doesn't even offer them (oc_ and co_ tools name one account)."""
    out = []
    for schema in schemas:
        name = schema.get("function", {}).get("name", "")
        if (name.startswith("oc_") or name.startswith("co_")) and tool_refusal(name):
            continue
        out.append(schema)
    return out


def set_owner(account: str, whose: str) -> str:
    """Make an account one person's, or SHARED. Only the owner does this."""
    from core import agent_config, people
    account = str(account or "").strip().lower()
    if account.count(":") != 2 or account.split(":", 1)[0] not in ("oc", "composio", "app"):
        raise ValueError("That isn't a connected account.")
    s = agent_config._load_settings()
    whose = str(whose or "").strip().lower()
    if whose != SHARED and people.get(whose, s) is None:
        raise ValueError("Choose a person on this FreeHand, or everyone.")
    current = owners(s)
    if whose == people.OWNER_ID:
        current.pop(account, None)  # the default
    else:
        current[account] = whose
    s[SETTING] = current
    agent_config._save_settings(s)
    return whose

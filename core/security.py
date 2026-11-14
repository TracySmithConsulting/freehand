import json
import sqlite3
import threading
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any

try:
    import urllib.request
    _HAS_HTTP = True
except ImportError:
    _HAS_HTTP = False

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from core import database


class PermissionTier(Enum):
    AUTONOMOUS = "autonomous"
    SEMI_AUTONOMOUS = "semi_autonomous"
    FULL_ACCESS = "full_access"


# Serialized values from settings/vaults written before Round 18.
# "god_mode" (the old name of FULL_ACCESS) keeps loading as an alias.
LEGACY_TIER_ALIASES = {
    "god_mode": "full_access",
}


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


EXTERNAL_ACTIONS = frozenset({
    "delete", "send", "execute", "deploy", "withdraw",
    "transfer", "post", "publish", "modify_system",
    "create_user", "change_config", "run_script",
    "write_file",      # File writes outside workspace
    "browser_action",  # Browser automation actions
    "oauth_access",    # External API writes via connected services
})


PROJECT_ROOT = Path(__file__).parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
SETTINGS_PATH = VAULT_DIR / "settings.json"

_lock = threading.Lock()
_subscribers: Dict[int, List[callable]] = {}


def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_settings(data: dict) -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data))


def get_current_tier() -> PermissionTier:
    settings = _load_settings()
    tier_str = settings.get("tier", "semi_autonomous")
    tier_str = LEGACY_TIER_ALIASES.get(tier_str, tier_str)
    try:
        return PermissionTier(tier_str)
    except ValueError:
        return PermissionTier.SEMI_AUTONOMOUS


def set_tier(tier: str) -> str:
    tier = LEGACY_TIER_ALIASES.get(tier, tier)
    try:
        PermissionTier(tier)
    except ValueError:
        raise ValueError(
            f"Invalid tier: {tier}. Must be one of: "
            + ", ".join(t.value for t in PermissionTier)
        )
    settings = _load_settings()
    settings["tier"] = tier
    _save_settings(settings)
    return tier


def _is_external_action(action_type: str) -> bool:
    return action_type.lower().strip() in EXTERNAL_ACTIONS


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(database.DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def intercept_action(
    action_type: str,
    description: str,
    payload: Optional[dict] = None,
    source: Optional[str] = None,
    caller_id: Optional[str] = None,
    force_confirm: bool = False,
) -> dict:
    """Check if an action is allowed under the current permission tier.

    - FULL_ACCESS: always allowed
    - AUTONOMOUS: blocked for all external actions
    - SEMI_AUTONOMOUS: pauses execution, creates pending approval

    ``force_confirm`` (Round 15 slice 2): when True, an external action
    must pause for an out-of-band approval EVEN in FULL_ACCESS, where every
    external action otherwise auto-approves. This is how a destructive OC
    tool (e.g. github.delete_repo) is gated in the highest-trust tier.
    Non-external actions are unaffected, and a force_confirmed call at
    AUTONOMOUS is still denied outright (no approval row) — the tier
    floor is preserved.

    Returns dict with keys:
        allowed (bool) — whether the action can proceed immediately
        approval_id (int|None) — ID if approval was required/pending
        tier (str) — current tier name
    """
    tier = get_current_tier()
    is_external = _is_external_action(action_type)

    # FULL_ACCESS normally auto-approves. A force-confirmed external action
    # is the exception: it must still pause for an out-of-band approval.
    if tier == PermissionTier.FULL_ACCESS and not (force_confirm and is_external):
        return {"allowed": True, "approval_id": None, "tier": tier.value}

    if not is_external:
        return {"allowed": True, "approval_id": None, "tier": tier.value}

    if tier == PermissionTier.AUTONOMOUS:
        return {"allowed": False, "approval_id": None, "tier": tier.value}

    # SEMI_AUTONOMOUS — or FULL_ACCESS with force_confirm — create pending approval
    payload_json = json.dumps(payload or {})
    from core import people
    conn = _get_conn()
    people.ensure_person_column(conn, "approvals")
    cursor = conn.execute(
        "INSERT INTO approvals (action_type, description, payload, status, person) VALUES (?, ?, ?, ?, ?)",
        (action_type, description, payload_json, "pending", people.current()),
    )
    approval_id = cursor.lastrowid
    conn.commit()
    conn.close()

    # Notify SSE subscribers
    _notify_subscribers({
        "id": approval_id,
        "action_type": action_type,
        "description": description,
        "payload": payload or {},
        "source": source,
        "caller_id": caller_id,
    })

    # Send external notification if source is telegram/slack
    if source in ("telegram", "slack", "whatsapp"):
        _send_external_approval(
            action_type, description, approval_id, source,
            caller_id=caller_id, payload=payload,
        )

    return {
        "allowed": False,
        "approval_id": approval_id,
        "tier": tier.value,
        "status": "pending",
    }


def _notify_subscribers(event_data: dict) -> None:
    with _lock:
        for callback in list(_subscribers.values()):
            try:
                callback(event_data)
            except Exception:
                pass


def subscribe(callback: callable) -> int:
    with _lock:
        sub_id = id(callback)
        _subscribers[sub_id] = callback
        return sub_id


def unsubscribe(sub_id: int) -> None:
    with _lock:
        _subscribers.pop(sub_id, None)


def _send_external_approval(
    action_type: str,
    description: str,
    approval_id: int,
    source: str,
    caller_id: Optional[str] = None,
    payload: Optional[dict] = None,
) -> None:
    details = _approval_details(payload)
    if source == "telegram":
        _send_telegram_approval(action_type, description, approval_id, caller_id, details)
    elif source == "slack":
        _send_slack_approval(action_type, description, approval_id, details)
    elif source == "whatsapp":
        _send_whatsapp_approval(action_type, description, approval_id, caller_id, details)


def _approval_details(payload: Optional[dict], cap: int = 400) -> str:
    """Short preview of a held tool call's arguments, so a person approving
    from a phone can see WHAT they are approving, not just the tool name."""
    args = (payload or {}).get("args")
    if not args:
        return ""
    text = json.dumps(args, ensure_ascii=False, default=str)
    return text if len(text) <= cap else text[:cap] + "..."


def _get_approval_base_url() -> str:
    """Resolve the base URL used in approval-button links.

    Reads `settings["public_base_url"]`. Falls back to
    `http://localhost:8000` if unset, but logs a warning so the user
    notices (Tailscale / reverse-proxy / HTTPS deployments MUST set this
    explicitly, or Slack button clicks will fail).

    Returns the URL with no trailing slash.
    """
    from core.agent_config import resolve_public_base_url
    base = resolve_public_base_url(_load_settings())
    if base:
        return base
    # Fallback with warning. Single warning per process is enough.
    if not _get_approval_base_url._warned:
        print(
            "[WARN] public_base_url not set in settings.json — Slack approval "
            "buttons will link to http://localhost:8000. If you are running "
            "FreeHand on Tailscale, behind a reverse proxy, or over HTTPS, "
            "set settings.public_base_url to your real external URL "
            "(e.g. 'https://freehand.tail123.ts.net')."
        )
        _get_approval_base_url._warned = True
    return "http://localhost:8000"

_get_approval_base_url._warned = False


def _send_telegram_approval(
    action_type: str,
    description: str,
    approval_id: int,
    caller_id: Optional[str] = None,
    details: str = "",
) -> None:
    settings = _load_settings()
    bot_token = settings.get("telegram_bot_token", "")
    # Send the prompt to the chat that made the request (only that chat can
    # approve it remotely); fall back to the configured owner chat.
    chat_id = str(caller_id or "").strip() or settings.get("telegram_chat_id", "")
    if not bot_token or not chat_id:
        return
    # Plain text, not Markdown: tool args can contain * _ ` which would make
    # Telegram reject the whole message.
    text = (
        f"Approval request #{approval_id}\n"
        f"Action: {action_type}\n"
        f"Description: {description}\n"
        + (f"Details: {details}\n" if details else "")
        + f"\nReply /approve {approval_id} or /deny {approval_id}"
    )
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    if not _HAS_HTTP:
        return
    try:
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:
        pass


def _send_whatsapp_approval(
    action_type: str,
    description: str,
    approval_id: int,
    caller_id: Optional[str] = None,
    details: str = "",
) -> None:
    """Send the approval prompt to the WhatsApp chat that asked, via the
    bridge's authenticated /reply endpoint."""
    jid = str(caller_id or "").strip()
    secret = str(_load_settings().get("bridge_secret", "")).strip()
    if not jid or not secret or not _HAS_HTTP:
        return
    from core.gateway import _get_bridge_url  # lazy: gateway imports security
    text = (
        f"Approval request #{approval_id}\n"
        f"Action: {action_type}\n"
        f"Description: {description}\n"
        + (f"Details: {details}\n" if details else "")
        + f"\nReply /approve {approval_id} or /deny {approval_id}"
    )
    try:
        payload = json.dumps({"jid": jid, "text": text}).encode()
        req = urllib.request.Request(
            _get_bridge_url() + "/reply",
            data=payload,
            headers={"Content-Type": "application/json", "X-Bridge-Secret": secret},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:
        pass


def _send_slack_approval(
    action_type: str,
    description: str,
    approval_id: int,
    details: str = "",
) -> None:
    """Post an approval prompt to the configured Slack webhook.

    This used to post Approve/Deny link buttons pointing at
    /api/approvals/<id>/approve. A link click is an unauthenticated GET
    against a key-gated POST endpoint, so the buttons could never work.
    Now it tells the person to reply in chat (or use the web console).
    "approve 5" works without the slash, because Slack intercepts messages
    that start with "/" as slash commands.
    """
    settings = _load_settings()
    webhook = settings.get("slack_webhook_url", "")
    if not webhook:
        return
    if not _HAS_HTTP:
        return

    base = _get_approval_base_url()
    text = (
        f"*Action:* {action_type}\n*Description:* {description}"
        + (f"\n*Details:* `{details}`" if details else "")
        + f"\n\nReply `approve {approval_id}` or `deny {approval_id}` to the "
        f"FreeHand bot, or open the console: {base}"
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Approval Request #{approval_id}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
    ]
    try:
        payload = json.dumps({"blocks": blocks, "text": f"Approval request #{approval_id}"}).encode()
        req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:
        pass


def get_pending_approvals() -> List[Dict]:
    conn = _get_conn()
    cursor = conn.execute(
        "SELECT id, action_type, description, payload, status, created_at "
        "FROM approvals WHERE status = 'pending' ORDER BY created_at DESC"
    )
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results


def handle_approval(approval_id: int, action: str) -> dict:
    """Approve or reject a pending approval.

    Returns dict with status and id.
    """
    if action not in ("approve", "deny"):
        raise ValueError(f"Invalid action: {action}")

    new_status = "approved" if action == "approve" else "rejected"
    conn = _get_conn()
    cursor = conn.execute(
        "UPDATE approvals SET status = ? WHERE id = ? AND status = 'pending'",
        (new_status, approval_id),
    )
    conn.commit()
    affected = cursor.rowcount
    conn.close()

    if affected == 0:
        raise ValueError(f"Approval #{approval_id} not found or already processed")

    return {"status": new_status, "id": approval_id}


def clear_pending_approvals(reason: str = "") -> int:
    """Reject all pending approvals. Returns count cleared.

    C4 fix: this is a powerful operation that silently cancels every
    in-flight approval request — including ones the user may have
    already responded to via Telegram/Slack. Old signature had no
    parameters, no warning, no audit trail.

    New behaviour:
    - `reason` is required (positional callers from internal code use the
      default empty string, but external callers should provide context).
    - Emits a stderr warning when called.
    - Writes an audit row to the `approvals` table with status='rejected'
      so there's a record of who cleared what.

    Returns the number of pending approvals that were cleared.
    """
    if not reason:
        import sys
        print(
            "[WARN] clear_pending_approvals() called without a reason. "
            "Pass a string explaining why you're cancelling pending approvals.",
            file=sys.stderr,
        )

    conn = _get_conn()
    cursor = conn.execute(
        "UPDATE approvals SET status = 'rejected' WHERE status = 'pending'"
    )
    count = cursor.rowcount
    conn.commit()

    if count > 0:
        # Audit log: insert a marker row indicating bulk rejection.
        from datetime import datetime, timezone
        conn.execute(
            "INSERT INTO approvals (action_type, description, payload, status) VALUES (?, ?, ?, ?)",
            (
                "bulk_clear",
                f"Bulk-cleared {count} pending approvals" + (f": {reason}" if reason else ""),
                json.dumps({"count": count, "reason": reason}),
                "rejected",
            ),
        )
        conn.commit()

    conn.close()
    return count


# ── Approvals: who may release a held action ─────────────────────────
# REMOTE_SOURCES is the single source of truth for "which channels are
# remote chat gateways vs the local web console / CLI". agent.py's N6
# write tightening and the approval rules below both gate on it.
REMOTE_SOURCES = {"telegram", "slack", "whatsapp"}

# The rule is decided by WHERE THE APPROVAL IS GIVEN, not where the request
# came from:
#   - Web console / CLI (approver "local"): approving re-runs the held tool,
#     whatever channel asked for it. That is the trusted surface.
#   - Chat gateway: only the same chat that made the request can approve
#     it, and only for registered, non-destructive tools. Destructive tools
#     (e.g. github.delete_repo) can only be released from the web console,
#     so a hijacked chat account can never fire one.


def _get_approval(approval_id: int) -> Optional[dict]:
    """Fetch one approvals row by id as a dict (or None if absent)."""
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT id, action_type, description, payload, status, created_at "
        "FROM approvals WHERE id = ?",
        (approval_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _payload_of(row: dict) -> dict:
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _remote_release_refusal(tool: str) -> Optional[str]:
    """Why a chat gateway may NOT release this tool (None if it may).

    Fails closed: a tool the permission registry doesn't know is refused,
    as is anything flagged destructive.
    """
    from core.agent_config import get_tool_permission
    from core.tools import composio_registry as _composio_registry
    from core.tools import registry as _registry
    if get_tool_permission(tool) == "unknown":
        return f"'{tool}' is not a registered tool, so it can only be approved in the FreeHand web console."
    if _registry.is_destructive_tool(tool) or _composio_registry.is_destructive_tool(tool):
        return f"'{tool}' is a destructive action, so it can only be approved in the FreeHand web console."
    return None


async def re_execute_approval(approval_id: int, approver: str = "local") -> dict:
    """Re-drive the exact tool + args an APPROVED action was held with.

    ``approver`` is the channel the approval was given from: "local" (web
    console / CLI) or a REMOTE_SOURCES gateway. Remote approvers cannot
    release destructive or unregistered tools (see _remote_release_refusal).

    Returns:
      {"re_executed": True, "tool": <name>, "result": <execute_tool result>}
      {"re_executed": False, "reason": <why>}  for any of:
        missing row, status not 'approved', no tool in payload, remote
        approver releasing a destructive/unregistered tool.

    core.agent is imported lazily (it imports security at top level), and
    so that a monkeypatched core.agent.execute_tool is what actually runs.
    """
    row = _get_approval(approval_id)
    if row is None:
        return {"re_executed": False, "reason": "no such approval"}

    if row.get("status") != "approved":
        return {
            "re_executed": False,
            "reason": f"approval status is '{row.get('status')}', not approved",
        }

    payload = _payload_of(row)
    tool = payload.get("tool")
    if not tool:
        return {"re_executed": False, "reason": "approval payload has no tool"}

    if approver in REMOTE_SOURCES:
        refusal = _remote_release_refusal(tool)
        if refusal:
            return {"re_executed": False, "reason": refusal}

    args = payload.get("args") or {}
    import core.agent as _agent  # lazy: avoid import cycle + respect tests
    result = await _agent.execute_tool(tool, args)
    return {"re_executed": True, "tool": tool, "result": result}


def _remote_ownership_refusal(row: Optional[dict], source: str, caller_id: str) -> Optional[str]:
    """Check a chat user may act on this approval: it must exist, be pending,
    and have been requested from this same channel and chat."""
    if row is None or row.get("status") != "pending":
        return "That approval was not found or has already been handled."
    payload = _payload_of(row)
    if (
        str(payload.get("source", "")) != source
        or str(payload.get("caller_id", "")) != str(caller_id)
    ):
        return (
            "You can only approve or deny requests made from this chat. "
            "Use the FreeHand web console for others."
        )
    return None


# A chat approval must be given while the request is fresh. An old pending
# request (the person may have forgotten it, or the situation changed) can
# still be approved in the web console, where it is shown in full.
REMOTE_APPROVAL_MAX_AGE_SECONDS = 24 * 3600


def _is_stale(row: dict) -> bool:
    """True if the approval is older than REMOTE_APPROVAL_MAX_AGE_SECONDS.

    created_at is SQLite CURRENT_TIMESTAMP (UTC, "YYYY-MM-DD HH:MM:SS").
    An unreadable timestamp counts as stale (fail closed).
    """
    from datetime import datetime, timezone
    try:
        created = datetime.strptime(str(row.get("created_at")), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return True
    age = datetime.now(timezone.utc).replace(tzinfo=None) - created
    return age.total_seconds() > REMOTE_APPROVAL_MAX_AGE_SECONDS


def _summarise_result(result: Optional[dict], cap: int = 500) -> str:
    content = (result or {}).get("content", "")
    if not isinstance(content, str):
        content = json.dumps(content, default=str)
    return content if len(content) <= cap else content[:cap] + "..."


async def approve_from_remote(approval_id: int, source: str, caller_id: str) -> str:
    """/approve from a chat gateway. Returns the reply text for the chat."""
    row = _get_approval(approval_id)
    refusal = _remote_ownership_refusal(row, source, caller_id)
    if refusal:
        return refusal

    if _is_stale(row):
        hours = REMOTE_APPROVAL_MAX_AGE_SECONDS // 3600
        return (
            f"Approval #{approval_id} is more than {hours} hours old, so it can't be "
            "approved from chat. Approve it in the FreeHand web console if it's still "
            f"wanted, or reply /deny {approval_id}."
        )

    tool = _payload_of(row).get("tool")
    if tool:
        refusal = _remote_release_refusal(tool)
        if refusal:
            # Left pending on purpose, so it can still be approved locally.
            return refusal

    try:
        handle_approval(approval_id, "approve")
    except ValueError:
        return "That approval was not found or has already been handled."

    if not tool:
        return f"Approved #{approval_id}."
    outcome = await re_execute_approval(approval_id, approver=source)
    if not outcome.get("re_executed"):
        return f"Approved #{approval_id}, but it did not run: {outcome.get('reason', 'unknown reason')}"
    return f"Approved #{approval_id}. Ran {tool}:\n{_summarise_result(outcome.get('result'))}"


def deny_from_remote(approval_id: int, source: str, caller_id: str) -> str:
    """/deny from a chat gateway. Same ownership rule as approve."""
    refusal = _remote_ownership_refusal(_get_approval(approval_id), source, caller_id)
    if refusal:
        return refusal
    try:
        handle_approval(approval_id, "deny")
    except ValueError:
        return "That approval was not found or has already been handled."
    return f"Denied #{approval_id}."

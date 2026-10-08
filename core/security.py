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
from core.database import DB_PATH


class PermissionTier(Enum):
    AUTONOMOUS = "autonomous"
    SEMI_AUTONOMOUS = "semi_autonomous"
    GOD_MODE = "god_mode"


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
    try:
        return PermissionTier(tier_str)
    except ValueError:
        return PermissionTier.SEMI_AUTONOMOUS


def set_tier(tier: str) -> str:
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
    conn = sqlite3.connect(str(DB_PATH))
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

    - GOD_MODE: always allowed
    - AUTONOMOUS: blocked for all external actions
    - SEMI_AUTONOMOUS: pauses execution, creates pending approval

    ``force_confirm`` (Round 15 slice 2): when True, an external action
    must pause for an out-of-band approval EVEN in GOD_MODE, where every
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

    # GOD_MODE normally auto-approves. A force-confirmed external action
    # is the exception: it must still pause for an out-of-band approval.
    if tier == PermissionTier.GOD_MODE and not (force_confirm and is_external):
        return {"allowed": True, "approval_id": None, "tier": tier.value}

    if not is_external:
        return {"allowed": True, "approval_id": None, "tier": tier.value}

    if tier == PermissionTier.AUTONOMOUS:
        return {"allowed": False, "approval_id": None, "tier": tier.value}

    # SEMI_AUTONOMOUS — or GOD_MODE with force_confirm — create pending approval
    payload_json = json.dumps(payload or {})
    conn = _get_conn()
    cursor = conn.execute(
        "INSERT INTO approvals (action_type, description, payload, status) VALUES (?, ?, ?, ?)",
        (action_type, description, payload_json, "pending"),
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
    if source in ("telegram", "slack"):
        _send_external_approval(action_type, description, approval_id, source)

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
) -> None:
    if source == "telegram":
        _send_telegram_approval(action_type, description, approval_id)
    elif source == "slack":
        _send_slack_approval(action_type, description, approval_id)


def _get_approval_base_url() -> str:
    """Resolve the base URL used in approval-button links.

    Reads `settings["public_base_url"]`. Falls back to
    `http://localhost:8000` if unset, but logs a warning so the user
    notices (Tailscale / reverse-proxy / HTTPS deployments MUST set this
    explicitly, or Slack button clicks will fail).

    Returns the URL with no trailing slash.
    """
    settings = _load_settings()
    base = settings.get("public_base_url", "").strip().rstrip("/")
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


def _send_telegram_approval(action_type: str, description: str, approval_id: int) -> None:
    settings = _load_settings()
    bot_token = settings.get("telegram_bot_token", "")
    chat_id = settings.get("telegram_chat_id", "")
    if not bot_token or not chat_id:
        return
    text = (
        f"🔐 *Approval Request #{approval_id}*\n\n"
        f"*Action:* {action_type}\n"
        f"*Description:* {description}\n\n"
        f"Reply with `/approve {approval_id}` or `/deny {approval_id}`"
    )
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    if not _HAS_HTTP:
        return
    try:
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception:
        pass


def _send_slack_approval(action_type: str, description: str, approval_id: int) -> None:
    settings = _load_settings()
    webhook = settings.get("slack_webhook_url", "")
    if not webhook:
        return
    if not _HAS_HTTP:
        return

    # C5 fix: derive base URL from settings.public_base_url (with fallback).
    # Slack buttons must be clickable from the user's browser; hardcoding
    # localhost breaks any non-local deployment.
    base = _get_approval_base_url()

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"Approval Request #{approval_id}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Action:* {action_type}\n*Description:* {description}"}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "url": f"{base}/api/approvals/{approval_id}/approve",
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "url": f"{base}/api/approvals/{approval_id}/deny",
                    "style": "danger",
                },
            ],
        },
    ]
    try:
        payload = json.dumps({"blocks": blocks}).encode()
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


# ── Round 15 slice 3: close the loop on held destructive actions ──────
# REMOTE_SOURCES is the single source of truth for "which sources are
# remote gateways vs a local terminal/web session". agent.py's N6 write
# tightening and re_execute_approval() both gate on it, so it lives here.
REMOTE_SOURCES = {"telegram", "slack", "whatsapp"}


def _get_approval(approval_id: int) -> Optional[dict]:
    """Fetch one approvals row by id as a dict (or None if absent)."""
    conn = _get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT id, action_type, description, payload, status "
        "FROM approvals WHERE id = ?",
        (approval_id,),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


async def re_execute_approval(approval_id: int) -> dict:
    """Round 15 slice 3 — when an APPROVED destructive action is approved,
    re-drive the exact tool + args it was held with.

    This is what makes the out-of-band /approve buttons real instead of
    decorative: before this, handle_approval only flipped the DB status,
    so approving a held github.delete_repo did nothing.

    Source-gated (the security decision): only LOCAL-originating calls
    re-execute. A call held at a remote gateway (telegram/slack/whatsapp)
    stays veto/record-only — a remote /approve button must NOT fire a
    destructive action.

    Returns:
      {"re_executed": True, "tool": <name>, "result": <execute_tool result>}
      {"re_executed": False, "reason": <why>}  for any of:
        missing row, status not 'approved', no tool in payload, remote source.

    The re-execute target is looked up lazily on the core.agent module
    (agent.py imports security at top level, so a top-level import here
    would be a cycle) — and lazily so a monkeypatched
    core.agent.execute_tool is what actually runs.
    """
    row = _get_approval(approval_id)
    if row is None:
        return {"re_executed": False, "reason": "no such approval"}

    if row.get("status") != "approved":
        return {
            "re_executed": False,
            "reason": f"approval status is '{row.get('status')}', not approved",
        }

    try:
        payload = json.loads(row.get("payload") or "{}")
    except (json.JSONDecodeError, TypeError):
        payload = {}

    tool = payload.get("tool")
    if not tool:
        return {"re_executed": False, "reason": "approval payload has no tool"}

    source = str(payload.get("source", ""))
    if source in REMOTE_SOURCES:
        return {
            "re_executed": False,
            "reason": (
                f"remote source '{source}' is veto-only: a remote /approve "
                "must not fire a destructive action"
            ),
        }

    args = payload.get("args") or {}
    import core.agent as _agent  # lazy: avoid import cycle + respect tests
    result = await _agent.execute_tool(tool, args)
    return {"re_executed": True, "tool": tool, "result": result}

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
) -> dict:
    """Check if an action is allowed under the current permission tier.

    - GOD_MODE: always allowed
    - AUTONOMOUS: blocked for all external actions
    - SEMI_AUTONOMOUS: pauses execution, creates pending approval

    Returns dict with keys:
        allowed (bool) — whether the action can proceed immediately
        approval_id (int|None) — ID if approval was required/pending
        tier (str) — current tier name
    """
    tier = get_current_tier()

    if tier == PermissionTier.GOD_MODE:
        return {"allowed": True, "approval_id": None, "tier": tier.value}

    if not _is_external_action(action_type):
        return {"allowed": True, "approval_id": None, "tier": tier.value}

    if tier == PermissionTier.AUTONOMOUS:
        return {"allowed": False, "approval_id": None, "tier": tier.value}

    # SEMI_AUTONOMOUS — create pending approval
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


def clear_pending_approvals() -> int:
    """Reject all pending approvals. Returns count cleared."""
    conn = _get_conn()
    cursor = conn.execute(
        "UPDATE approvals SET status = 'rejected' WHERE status = 'pending'"
    )
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count

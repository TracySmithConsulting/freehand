import json
import re
import hmac
import hashlib
import asyncio
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime, timezone

import aiohttp

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from core.database import DB_PATH
from core.security import get_current_tier, intercept_action, handle_approval
from core.memory import sync_vault_to_sqlite
from core.scheduler import process_scribble


PROJECT_ROOT = Path(__file__).parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
SCRIBBLE_PATH = VAULT_DIR / "00_Scribble.md"
BRIDGE_PORT = 8765


def _load_settings() -> dict:
    settings_path = VAULT_DIR / "settings.json"
    if settings_path.exists():
        try:
            return json.loads(settings_path.read_text())
        except Exception:
            pass
    return {}


def _save_settings(data: dict) -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    settings_path = VAULT_DIR / "settings.json"
    settings_path.write_text(json.dumps(data))


def _get_bridge_url() -> str:
    return f"http://127.0.0.1:{BRIDGE_PORT}"


def _truncate(text: str, max_len: int = 280) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


async def _send_to_bridge(path: str, json_data: dict = None) -> dict:
    """Send a request to the WhatsApp bridge server."""
    url = _get_bridge_url() + path
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=json_data or {},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                return await resp.json()
    except Exception:
        return {"error": "Bridge unavailable"}


async def _telegram_send_message(chat_id: str, text: str) -> bool:
    """Send a message via Telegram Bot API."""
    settings = _load_settings()
    bot_token = settings.get("telegram_bot_token", "").strip()
    if not bot_token:
        return False
    text = _truncate(text)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                return resp.status == 200
    except Exception:
        return False


async def _slack_send_message(channel: str, text: str) -> bool:
    """Send a message via Slack Web API."""
    settings = _load_settings()
    bot_token = settings.get("slack_bot_token", "").strip()
    if not bot_token:
        return False
    text = _truncate(text)
    url = "https://slack.com/api/chat.postMessage"
    headers = {"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"channel": channel, "text": text},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
                return data.get("ok", False)
    except Exception:
        return False


def _verify_slack_signature(body: str, signature: str, timestamp: str) -> bool:
    """Verify Slack webhook signature."""
    settings = _load_settings()
    signing_secret = settings.get("slack_signing_secret", "").strip()
    if not signing_secret:
        return True
    ts = timestamp
    sig_base_string = f"v0:{ts}:{body}"
    my_signature = "v0=" + hmac.new(
        signing_secret.encode(), sig_base_string.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(my_signature, signature)


# ── N12: per-channel allow_from check ────────────────────────────────────
# Each gateway has its own transport-level auth (Telegram bot token, Slack
# HMAC, WhatsApp bridge secret), but that only proves the *sender* is
# legitimate, not that the *user* is allowed to use FreeHand. allow_from
# is the second layer: a list of acceptable user identifiers per channel.
#
# Behaviour:
#   - If allow_from is unset OR empty → emit warning on first use,
#     deny the request. ("Closed by default" — Tracy's instinct.)
#   - If allow_from is a non-empty list → caller_id must be in the list.
#
# Settings keys:
#   settings["telegram_allow_from"]   = ["123456789", ...]   (chat_ids)
#   settings["slack_allow_from"]       = ["U0123ABC", ...]    (user_ids)
#   settings["whatsapp_allow_from"]    = ["+27821234567", ...] (phone JIDs)
#
# Settings keys (legacy, kept for backwards-compat):
#   settings["telegram_chat_whitelist"] = same role; if both set, the
#   union is allowed. New deployments should use allow_from.

def _check_allow_from(channel: str, caller_id: str) -> bool:
    """Return True if caller_id is allowed for the given channel.

    Reads `settings["{channel}_allow_from"]`. Also accepts the legacy
    `telegram_chat_whitelist` for backward compatibility.

    Logs a one-shot warning per (channel, process) if the list is empty.
    """
    settings = _load_settings()
    # Primary key
    allow = settings.get(f"{channel}_allow_from", [])
    # Backwards-compat for telegram
    if not allow and channel == "telegram":
        allow = settings.get("telegram_chat_whitelist", [])
    allow = [str(x) for x in allow] if allow else []

    if not allow:
        # Closed-by-default + warn
        key = f"_warned_allow_from_{channel}"
        if not getattr(_check_allow_from, key, False):
            print(
                f"[WARN] {channel}_allow_from not set in settings.json — "
                f"all incoming {channel} messages will be REJECTED. Add your "
                f"{channel} user ID (chat_id / user_id / phone_jid) to "
                f"settings['{channel}_allow_from'] to allow access."
            )
            setattr(_check_allow_from, key, True)
        return False

    return str(caller_id) in allow


async def _route_to_agent(text: str, source: str = "", caller_id: str = "") -> str:
    """Core routing logic: parse commands and route to appropriate handler."""
    text = text.strip()
    settings = _load_settings()
    tier = get_current_tier()

    # Command parsing
    if text.startswith("/scribble"):
        content = text[9:].strip()
        if content:
            existing = SCRIBBLE_PATH.read_text(encoding="utf-8") if SCRIBBLE_PATH.exists() else ""
            marker = "\n\n---\n"
            if existing and not existing.endswith("\n\n---\n"):
                marker = "\n" + marker
            # N8 fix: aware UTC datetime
            new_entry = f"{marker}> [{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}]{marker}{content}\n"
            SCRIBBLE_PATH.write_text(existing + new_entry, encoding="utf-8")
            return f"Added to Scribble: {content[:80]}"
        return "Use: /scribble <your note>"

    if text.startswith("/task"):
        match = re.match(r"/task\s+(.+?)(?:\s+\[([^\]]+)\])?$", text)
        if match:
            title = match.group(1).strip()
            cron = match.group(2) or "0 2 * * *"
            # N7 fix: removed dead `DB_PATH.__class__(DB_PATH)` line that did nothing
            # useful (Path() doesn't make a DB connection). Just open the DB.
            import sqlite3
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute(
                "INSERT INTO tasks (title, cron_schedule, status) VALUES (?, ?, ?)",
                (title, cron, "pending")
            )
            conn.commit()
            conn.close()
            return f"Task created: {title} (cron: {cron})"
        return "Use: /task <title> [cron_schedule]"

    if text.startswith("/memory"):
        query = text[7:].strip()
        if query:
            from core.memory import search_memory
            results = search_memory(query, limit=3)
            if results:
                lines = []
                for r in results:
                    lines.append(f"- [{r['path']}] {r['title']}")
                return "Memory results:\n" + "\n".join(lines)
            return "No memories found for: " + query
        return "Use: /memory <search query>"

    if text.startswith("/approve"):
        parts = text.split()
        if len(parts) >= 2:
            try:
                aid = int(parts[1])
                result = handle_approval(aid, "approve")
                return f"Approved action #{aid}"
            except ValueError:
                return "Invalid approval ID."
        return "Use: /approve <id>"

    if text.startswith("/deny"):
        parts = text.split()
        if len(parts) >= 2:
            try:
                aid = int(parts[1])
                result = handle_approval(aid, "deny")
                return f"Denied action #{aid}"
            except ValueError:
                return "Invalid approval ID."
        return "Use: /deny <id>"

    if text.startswith("/tier"):
        new_tier = text[5:].strip()
        try:
            from core.security import set_tier
            set_tier(new_tier)
            return f"Tier set to: {new_tier}"
        except ValueError as e:
            return str(e)
        return f"Current tier: {tier.value}"

    if text.startswith("/status"):
        pending = 0
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        pending = conn.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
        conn.close()
        whatsapp_ok = settings.get("whatsapp_enabled", False)
        telegram_ok = bool(settings.get("telegram_bot_token", "").strip())
        slack_ok = bool(settings.get("slack_bot_token", "").strip())
        lines = [
            f"Tier: {tier.value}",
            f"Pending approvals: {pending}",
            f"Telegram: {'connected' if telegram_ok else 'not configured'}",
            f"Slack: {'connected' if slack_ok else 'not configured'}",
            f"WhatsApp: {'linked' if whatsapp_ok else 'not linked'}",
        ]
        return "\n".join(lines)

    if text.startswith("/scribble"):
        return await _handle_scribble_command(text, source, caller_id)

    if text.startswith("/sweep"):
        result = process_scribble()
        return f"Sweep done: {result['processed']} entries, {result['tasks_created']} tasks, {result['threads_updated']} threads"

    if text.startswith("/sync"):
        count = sync_vault_to_sqlite()
        return f"Synced {count} file(s) to memory index"

    if text.startswith("/pair"):
        phone = text[5:].strip().replace("+", "")
        if phone:
            resp = await _send_to_bridge("/pair", {"phone": phone})
            if "code" in resp:
                return f"Your pairing code is: {resp['code']}\nEnter this in WhatsApp to link your device."
            return f"Pairing error: {resp.get('error', 'unknown')}"
        return "Use: /pair <phone_number>"

    # Default: route to agent command
    return await _handle_agent_command(text, source, caller_id, tier)


async def _handle_agent_command(text: str, source: str, caller_id: str, tier) -> str:
    """Route text to the agent command endpoint with security check."""
    result = intercept_action("execute", f"Agent command: {text[:50]}", {"text": text, "source": source}, source, caller_id)
    if not result["allowed"]:
        if result["approval_id"]:
            approval_msg = f"Action requires approval (ID: {result['approval_id']}). Reply with /approve {result['approval_id']} or /deny {result['approval_id']}."
            if source == "telegram":
                await _telegram_send_message(caller_id, approval_msg)
            elif source == "slack":
                await _slack_send_message(caller_id, approval_msg)
            return approval_msg
        return "Action blocked by permission tier. Use /tier to change."

    # Execute via agent command endpoint
    import aiohttp
    base_url = "http://127.0.0.1:8000"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                base_url + "/api/agent/command",
                json={"command": text, "source": source, "caller_id": caller_id},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                data = await resp.json()
                return data.get("result", "Command processed.")
    except Exception as e:
        return f"Error: {str(e)[:100]}"


async def _handle_scribble_command(text: str, source: str, caller_id: str) -> str:
    """Handle /scribble command — append to 00_Scribble.md."""
    content = text[9:].strip()
    if not content:
        return "Use: /scribble <your note>"
    existing = SCRIBBLE_PATH.read_text(encoding="utf-8") if SCRIBBLE_PATH.exists() else ""
    # N8 fix: aware UTC datetime
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    new_line = f"- [ ] [{timestamp}] {content}\n"
    if existing.strip():
        # Insert before the --- separator if present
        separator = "\n---\n"
        if separator in existing:
            parts = existing.split(separator, 1)
            new_content = parts[0] + separator + new_line + separator + parts[1]
        else:
            new_content = existing.rstrip() + "\n" + new_line
    else:
        new_content = existing + new_line
    SCRIBBLE_PATH.write_text(new_content, encoding="utf-8")
    return f"Added to Scribble: {content[:80]}"


async def handle_telegram_webhook(update: dict) -> dict:
    """Process an incoming Telegram webhook update."""
    message = update.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if not text:
        return {"ok": True}

    # N12a: allow_from check (second layer after bot-token auth)
    if not _check_allow_from("telegram", chat_id):
        return {"ok": True}  # Silently drop — don't leak bot existence

    response = await _route_to_agent(text, source="telegram", caller_id=chat_id)
    await _telegram_send_message(chat_id, response)
    return {"ok": True}


async def handle_slack_webhook(body: str, headers: dict) -> dict:
    """Process an incoming Slack Events API webhook."""
    # Verify signature
    signature = headers.get("x-slack-signature", "")
    timestamp = headers.get("x-slack-request-timestamp", "")
    if not _verify_slack_signature(body, signature, timestamp):
        return {"status": 401, "detail": "Invalid signature"}

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {"status": 400, "detail": "Invalid JSON"}

    # Handle URL verification challenge
    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge", "")}

    # Handle events
    event = data.get("event", {})
    if event.get("type") != "message":
        return {"ok": True}

    text = (event.get("text") or "").strip()
    if not text:
        return {"ok": True}

    # N12b: allow_from check on Slack user_id
    user_id = event.get("user", "")
    if not _check_allow_from("slack", user_id):
        return {"ok": True}  # Silently drop

    channel = event.get("channel", "")
    response = await _route_to_agent(text, source="slack", caller_id=channel)
    await _slack_send_message(channel, response)
    return {"ok": True}


async def handle_whatsapp_message(remote_jid: str, text: str) -> dict:
    """Handle an incoming WhatsApp message, route it, and send response back."""
    # N12c: allow_from check on WhatsApp JID
    if not _check_allow_from("whatsapp", remote_jid):
        return {"response": "Not authorized", "bridge": {"skipped": "allow_from"}}
    response = await _route_to_agent(text, source="whatsapp", caller_id=remote_jid)
    # Send response back via bridge
    bridge_resp = await _send_to_bridge("/reply", {
        "jid": remote_jid,
        "text": response
    })
    return {"response": response, "bridge": bridge_resp}


def get_gateway_status() -> dict:
    """Return current gateway connection status."""
    settings = _load_settings()
    tier = get_current_tier()
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    pending = conn.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    conn.close()

    return {
        "tier": tier.value,
        "pending_approvals": pending,
        "telegram": {
            "configured": bool(settings.get("telegram_bot_token", "").strip()),
            "whitelist_count": len(settings.get("telegram_chat_whitelist", [])),
        },
        "slack": {
            "configured": bool(settings.get("slack_bot_token", "").strip()),
            "signing_secret_set": bool(settings.get("slack_signing_secret", "").strip()),
        },
        "whatsapp": {
            "enabled": settings.get("whatsapp_enabled", False),
            "pairing_phone": settings.get("whatsapp_pairing_phone", ""),
            "auth_exists": (PROJECT_ROOT / "bridge" / "auth_info_baileys").exists(),
        },
        "bridge_port": BRIDGE_PORT,
    }


def update_gateway_settings(updates: dict) -> dict:
    """Update gateway settings and return new status."""
    settings = _load_settings()
    settings.update(updates)
    _save_settings(settings)
    return get_gateway_status()


async def handle_whatsapp_pair(phone: str) -> dict:
    """Request a WhatsApp pairing code via the bridge."""
    resp = await _send_to_bridge("/pair", {"phone": phone})
    return resp

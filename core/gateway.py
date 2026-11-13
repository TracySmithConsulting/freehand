import json
import os
import re
import hmac
import hashlib
import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Dict, Optional, List
from datetime import datetime, timezone

import aiohttp

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from core import agent_config
from core import database
from core.security import get_current_tier, approve_from_remote, deny_from_remote
from core.memory import sync_vault_to_sqlite
from core.scheduler import process_scribble, create_task, append_scribble


log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
VAULT_DIR = PROJECT_ROOT / "vault"
SCRIBBLE_PATH = VAULT_DIR / "00_Scribble.md"
# Must match the bridge's BRIDGE_PORT (freehand/bridge/.env, default 8765).
BRIDGE_PORT = int(os.environ.get("FREEHAND_BRIDGE_PORT", "8765"))
BRIDGE_DIR = PROJECT_ROOT / "freehand" / "bridge"


def _whatsapp_auth_dir() -> Path:
    """Where the bridge keeps the linked-device session (WHATSAPP_AUTH_DIR,
    else freehand/bridge/auth_info_baileys). The status check used to look
    in a non-existent top-level bridge/ folder and always said "not linked"."""
    custom = os.environ.get("WHATSAPP_AUTH_DIR", "").strip()
    return Path(custom) if custom else BRIDGE_DIR / "auth_info_baileys"


def _load_settings() -> dict:
    """Settings I/O delegates to agent_config so there is ONE patchable
    path (agent_config.SETTINGS_PATH) and tests can never miss a second
    re-derived copy of it."""
    return agent_config._load_settings()


def _save_settings(data: dict) -> None:
    agent_config._save_settings(data)


def _get_bridge_url() -> str:
    """Base URL of the WhatsApp bridge.

    FREEHAND_BRIDGE_URL overrides the default. It is needed when FreeHand
    runs in Docker and the bridge on the host: inside the container
    127.0.0.1 is the container itself (compose.yml sets
    http://host.docker.internal:8765).
    """
    custom = os.environ.get("FREEHAND_BRIDGE_URL", "").strip().rstrip("/")
    return custom or f"http://127.0.0.1:{BRIDGE_PORT}"


# Chat replies are capped just under Telegram's 4096-character message limit
# (Slack and WhatsApp allow more). This was 280, which cut off most agent
# answers mid-sentence.
CHAT_REPLY_MAX_CHARS = 4000


def _truncate(text: str, max_len: int = CHAT_REPLY_MAX_CHARS) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


async def _send_to_bridge(path: str, json_data: dict = None) -> dict:
    """POST to the WhatsApp bridge, authenticated with settings.bridge_secret.

    The bridge rejects every request without the shared secret (it used to
    accept anything from anyone who could reach it).
    """
    secret = str(_load_settings().get("bridge_secret", "")).strip()
    if not secret:
        return {"error": "bridge_secret not configured - run `freehand gateway secret`"}
    url = _get_bridge_url() + path
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=json_data or {},
                headers={"X-Bridge-Secret": secret},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                if resp.status >= 400 and "error" not in data:
                    data["error"] = f"Bridge returned HTTP {resp.status}"
                return data
    except Exception:
        return {"error": "Bridge unavailable"}


async def _whatsapp_send_message(jid: str, text: str) -> bool:
    """Send a WhatsApp message through the bridge."""
    resp = await _send_to_bridge("/reply", {"jid": jid, "text": _truncate(text)})
    if not resp.get("sent"):
        log.warning("WhatsApp reply not sent: %s", resp.get("error", "unknown"))
        return False
    return True


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
            # Agent replies often contain an unbalanced * or _ (file names,
            # snake_case), which makes Telegram reject Markdown with a 400 —
            # the reply used to vanish silently. Retry as plain text.
            for extra in ({"parse_mode": "Markdown"}, {}):
                async with session.post(
                    url,
                    json={"chat_id": chat_id, "text": text, **extra},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return True
                    if resp.status != 400:
                        return False
            return False
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


SLACK_MAX_CLOCK_SKEW_SECONDS = 300


def _verify_slack_signature(body: str, signature: str, timestamp: str) -> bool:
    """Verify a Slack request signature (Slack's v0 signing scheme).

    Fails closed: with no slack_signing_secret configured, every request is
    rejected (this used to return True, i.e. accept anything). The request
    timestamp must be within 5 minutes of now, as Slack recommends, so a
    captured request cannot be replayed later.
    """
    settings = _load_settings()
    signing_secret = settings.get("slack_signing_secret", "").strip()
    if not signing_secret or not signature or not timestamp:
        if not signing_secret:
            log.warning("slack_signing_secret not set — rejecting Slack webhook")
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > SLACK_MAX_CLOCK_SKEW_SECONDS:
        return False
    sig_base_string = f"v0:{timestamp}:{body}"
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

def _whatsapp_identity(value) -> str:
    """Normalise a WhatsApp number or id for allow_from comparison.

    Phone-number ids ("27821234567@s.whatsapp.net", "27821234567:3@...",
    "+27 82 123 4567") become their digits. Other ids (e.g. "...@lid",
    which WhatsApp uses when it hides the number) are compared exactly.
    """
    s = str(value or "").strip()
    user, _, domain = s.partition("@")
    if domain in ("", "s.whatsapp.net", "c.us"):
        return re.sub(r"\D", "", user.split(":")[0])
    return s.lower()


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
    if channel == "whatsapp":
        # Numbers may be listed as "+27 82 123 4567" while the bridge sends
        # the WhatsApp id "27821234567@s.whatsapp.net"; compare digits.
        allow = [_whatsapp_identity(x) for x in allow]
        caller_id = _whatsapp_identity(caller_id)

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
    # /scribble goes through core.scheduler's single-writer appender so the
    # entry lands in the checklist format the sweeper actually parses (the
    # old inline quote-block writer here bypassed the sweep entirely).
    if text.startswith("/scribble"):
        return await _handle_scribble_command(text, source, caller_id)

    if text.startswith("/task"):
        match = re.match(r"/task\s+(.+?)(?:\s+\[([^\]]+)\])?$", text)
        if match:
            title = match.group(1).strip()
            cron = match.group(2) or "0 2 * * *"
            created = create_task(title, cron)
            if created["recurring"]:
                return f"Task created: {title} (cron: " + created["cron_schedule"] + ", next: " + created["next_run"] + ")"
            return "Task created (one-shot - cron '" + cron + "' is not a valid 5-field schedule): " + title
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

    # "/approve 5" or "approve 5" — Slack swallows messages that start with
    # "/" as slash commands, so the bare form must work there too.
    # A bare word needs a numeric id ("approve 5"); otherwise a message like
    # "deny it" is ordinary chat for the agent. "/approve" alone gets usage.
    decision = (
        re.match(r"^/(approve|deny)\b\s*(\S*)\s*$", text, re.IGNORECASE)
        or re.match(r"^(approve|deny)\s+(\d+)\s*$", text, re.IGNORECASE)
    )
    if decision:
        verb, arg = decision.group(1).lower(), decision.group(2)
        if not arg.isdigit():
            return f"Use: /{verb} <id>"
        if verb == "approve":
            return await approve_from_remote(int(arg), source, str(caller_id))
        return deny_from_remote(int(arg), source, str(caller_id))

    if text.startswith("/tier"):
        # Changing the permission tier is a local-only operation. A remote
        # gateway (or anyone who forged a message from one) must never be
        # able to lift itself to full_access.
        return (
            f"Tier is {tier.value}. The tier can only be changed from the "
            "FreeHand web console, not from chat."
        )

    if text.startswith("/status"):
        pending = 0
        import sqlite3
        conn = sqlite3.connect(str(database.DB_PATH))
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


    if text.startswith("/sweep"):
        result = process_scribble()
        return f"Sweep done: {result['processed']} entries, {result['tasks_created']} tasks, {result['threads_updated']} threads"

    if text.startswith("/sync"):
        count = sync_vault_to_sqlite()
        return f"Synced {count} file(s) to memory index"

    if text.startswith("/pair"):
        # Linking a WhatsApp account to FreeHand is an owner action, like
        # changing the tier: never from a remote chat.
        return (
            "WhatsApp pairing can only be started from the FreeHand web console "
            "or with `freehand gateway pair <phone>`."
        )

    # Default: route to agent command
    return await _handle_agent_command(text, source, caller_id, tier)


async def _handle_agent_command(text: str, source: str, caller_id: str, tier) -> str:
    """Run a chat message through the agent.

    There is no blanket "approve this message" gate any more: the sender is
    already authenticated (webhook secret / Slack signature / bridge secret)
    and on the allow_from list, and the agent loop gates every write tool
    by tier, demoting remote writes to an approval even at full_access.
    The old gate created an approval for EVERY chat message that nothing
    could ever release, so chat only worked at full_access.
    """
    # Run the agent in-process. This used to POST to
    # http://127.0.0.1:8000/api/agent/command without an X-API-Key (always
    # 401), read a non-existent "result" field, and gave up after 30s — so
    # every chat command silently answered "Command processed."
    from core.agent import run_command
    try:
        data = await run_command(text, source=source, caller_id=caller_id)
    except Exception as e:
        return f"Error: {str(e)[:200]}"
    if data.get("error"):
        return f"Error: {str(data['error'])[:300]}"
    return data.get("text") or "Done (the agent returned no text)."


async def _handle_scribble_command(text: str, source: str, caller_id: str) -> str:
    """Handle /scribble command - append to 00_Scribble.md in the sweepable
    checklist format via the scheduler's single-writer appender."""
    return append_scribble(text[9:].strip())

# ── Fast acknowledgement + duplicate suppression ───────────────────────
# Slack requires a 2xx within 3 seconds and otherwise re-delivers the same
# event up to 3 more times; Telegram re-delivers an update when the webhook
# is slow or errors. An agent run takes far longer than that, so the
# webhooks used to run the agent once per delivery: one Slack message could
# run it four times (duplicate replies, duplicate approvals, and at
# full_access duplicate posts).
#
# Now each webhook authenticates, checks allow_from and the delivery id,
# returns immediately, and runs the agent in a background task. Delivery
# ids (Telegram update_id, Slack event_id) are remembered for an hour, so
# any re-delivery is dropped.

DELIVERY_TTL_SECONDS = 3600
_seen_deliveries: Dict[str, float] = {}
_seen_lock = threading.Lock()
_background_tasks: set = set()


def _first_delivery(key: str) -> bool:
    """True the first time a delivery id is seen (within the TTL)."""
    now = time.monotonic()
    with _seen_lock:
        for k, ts in list(_seen_deliveries.items()):
            if now - ts > DELIVERY_TTL_SECONDS:
                del _seen_deliveries[k]
        if key in _seen_deliveries:
            return False
        _seen_deliveries[key] = now
        return True


def _spawn(coro) -> asyncio.Task:
    """Run ``coro`` in the background after the webhook has returned.

    The event loop only keeps weak references to tasks, so we hold a strong
    one until it finishes. Exceptions are logged rather than lost.
    """
    task = asyncio.get_running_loop().create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            log.error("gateway background task failed", exc_info=t.exception())

    task.add_done_callback(_done)
    return task


async def _reply_in_background(source: str, caller_id: str, text: str, send) -> None:
    """Run one chat message through the agent and send the reply."""
    try:
        response = await _route_to_agent(text, source=source, caller_id=caller_id)
    except Exception as e:  # never leave the person without an answer
        log.exception("%s message failed", source)
        response = f"Error: {str(e)[:200]}"
    await send(caller_id, response)


async def handle_telegram_webhook(update: dict) -> dict:
    """Accept an incoming Telegram update and answer it in the background.

    The caller (server.py) has already checked the webhook secret token.
    """
    message = update.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if not text:
        return {"ok": True}

    # N12a: allow_from check (second layer after the secret token)
    if not _check_allow_from("telegram", chat_id):
        return {"ok": True}  # Silently drop — don't leak bot existence

    update_id = update.get("update_id")
    if update_id is not None and not _first_delivery(f"telegram:{update_id}"):
        return {"ok": True, "duplicate": True}

    _spawn(_reply_in_background("telegram", chat_id, text, _telegram_send_message))
    return {"ok": True, "queued": True}


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
    # Ignore bot posts (including FreeHand's own replies, which would
    # otherwise loop) and edits/deletes/joins, which carry a subtype.
    if event.get("bot_id") or event.get("subtype"):
        return {"ok": True}

    text = (event.get("text") or "").strip()
    if not text:
        return {"ok": True}

    # N12b: allow_from check on Slack user_id
    user_id = event.get("user", "")
    if not _check_allow_from("slack", user_id):
        return {"ok": True}  # Silently drop

    # Slack re-delivers when it doesn't get a 2xx within 3 seconds; the
    # event_id is identical on every retry.
    event_id = data.get("event_id") or event.get("client_msg_id") or event.get("ts")
    if event_id and not _first_delivery(f"slack:{event_id}"):
        return {"ok": True, "duplicate": True}

    channel = event.get("channel", "")
    _spawn(_reply_in_background("slack", channel, text, _slack_send_message))
    return {"ok": True, "queued": True}


def _whatsapp_self_allowed() -> bool:
    """Whether the linked number may talk to FreeHand in its own
    "Message yourself" chat (setting whatsapp_allow_self, default on).

    People with only one WhatsApp number link their own number, so the
    self chat is the only place they can reach FreeHand.
    """
    return _load_settings().get("whatsapp_allow_self", True) is not False


async def handle_whatsapp_message(
    remote_jid: str,
    text: str,
    message_id: Optional[str] = None,
    from_self: bool = False,
) -> dict:
    """Accept a WhatsApp message from the bridge and answer it in the background.

    The caller (server.py) has already checked X-Bridge-Secret. The reply
    goes out through the bridge's /reply; the bridge ignores this response
    body (it used to queue the whole JSON object as message text).

    from_self means the bridge saw the linked number write in its own
    "Message yourself" chat. That person is the account owner, so it is
    allowed without an allow_from entry (unless whatsapp_allow_self is off).
    The bridge sets it only for that chat, and only FreeHand's bridge, which
    holds the shared secret, can reach this code.
    """
    owner = from_self is True and _whatsapp_self_allowed()
    # N12c: allow_from check on the WhatsApp id
    if not owner and not _check_allow_from("whatsapp", remote_jid):
        # Logged so the owner can find the exact id to allow (WhatsApp may
        # send a hidden-number "...@lid" id instead of the phone number).
        log.warning("WhatsApp message from %s ignored: not on whatsapp_allow_from", remote_jid)
        return {"ok": True, "skipped": "allow_from"}

    if message_id and not _first_delivery(f"whatsapp:{message_id}"):
        return {"ok": True, "duplicate": True}

    _spawn(_reply_in_background("whatsapp", remote_jid, text, _whatsapp_send_message))
    return {"ok": True, "queued": True}


def _bridge_health() -> dict:
    """Ask the WhatsApp bridge whether it is running and connected.

    Its /health needs no secret and reveals nothing sensitive. This is the
    reliable check: in Docker, FreeHand can't see the bridge's session
    folder on the host, so the file check alone said "not linked".
    """
    import urllib.request
    try:
        with urllib.request.urlopen(_get_bridge_url() + "/health", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        return {"running": True, "connected": bool(data.get("whatsapp_connected"))}
    except (OSError, ValueError, AttributeError):  # URLError/timeouts are OSError
        return {"running": False, "connected": False}


def get_gateway_status() -> dict:
    """Return current gateway connection status."""
    settings = _load_settings()
    tier = get_current_tier()
    import sqlite3
    conn = sqlite3.connect(str(database.DB_PATH))
    pending = conn.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    conn.close()
    bridge = _bridge_health()
    session_saved = (_whatsapp_auth_dir() / "creds.json").exists()

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
            # Linked if the bridge says so, or (bridge stopped) its saved
            # session is visible from here.
            "auth_exists": bridge["connected"] or session_saved,
            "bridge_running": bridge["running"],
            "connected": bridge["connected"],
            "bridge_secret_set": bool(str(settings.get("bridge_secret", "")).strip()),
            "allow_from": list(settings.get("whatsapp_allow_from", []) or []),
            "allow_self": settings.get("whatsapp_allow_self", True) is not False,
        },
        "bridge_port": BRIDGE_PORT,
    }


# What the console's gateway form may change. Anything else (tier, API
# key, LLM settings ...) has its own, narrower endpoint; this one used to
# accept any key at all.
GATEWAY_SETTING_KEYS = frozenset({
    "telegram_bot_token", "telegram_chat_whitelist", "telegram_allow_from",
    "telegram_webhook_secret",
    "slack_bot_token", "slack_signing_secret", "slack_allow_from",
    "whatsapp_allow_from", "whatsapp_allow_self", "whatsapp_enabled",
    "whatsapp_pairing_phone",
})


def _clean_list(value) -> list:
    """Accept a list or a comma/newline-separated string; drop blanks."""
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    if not isinstance(value, list):
        return []
    return [str(x).strip() for x in value if str(x).strip()]


def update_gateway_settings(updates: dict) -> dict:
    """Update gateway settings and return new status.

    Raises ValueError naming any key the gateway form may not set.
    """
    rejected = sorted(k for k in updates if k not in GATEWAY_SETTING_KEYS)
    if rejected:
        raise ValueError("Not a gateway setting: " + ", ".join(rejected))
    settings = _load_settings()
    for key, value in updates.items():
        if key.endswith("_allow_from") or key == "telegram_chat_whitelist":
            value = _clean_list(value)
        elif key == "whatsapp_allow_self":
            value = bool(value)
        settings[key] = value
    _save_settings(settings)
    return get_gateway_status()


async def handle_whatsapp_pair(phone: str) -> dict:
    """Request a WhatsApp pairing code via the bridge."""
    resp = await _send_to_bridge("/pair", {"phone": phone})
    return resp

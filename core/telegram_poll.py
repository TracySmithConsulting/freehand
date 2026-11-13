"""Telegram without a public address: FreeHand asks Telegram for new
messages itself ("long polling", Bot API getUpdates).

A Telegram webhook needs FreeHand reachable from the internet over https,
which a FreeHand on someone's own computer isn't. So by default FreeHand
polls: it works anywhere, with only the bot token. A hosted FreeHand with
a public address can set ``telegram_mode`` to "webhook" in settings to
use the webhook (POST /api/gateway/telegram) instead.

One loop, started with the server. It checks the settings every few
seconds, so saving a bot token in Settings starts it, and clearing the
token stops it. Messages go through the same handler as the webhook
(core/gateway.py handle_telegram_update), with the same allow list.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger("freehand.telegram_poll")

POLL_SECONDS = 25  # how long Telegram may hold each request open
IDLE_SECONDS = 5   # how often to re-check settings when there's no token


def _settings() -> dict:
    from core.agent_config import _load_settings
    return _load_settings()


def polling_wanted(settings: dict) -> bool:
    token = str(settings.get("telegram_bot_token", "")).strip()
    return bool(token) and str(settings.get("telegram_mode", "polling")).strip().lower() != "webhook"


async def run_forever() -> None:
    from core.gateway import handle_telegram_update

    offset = 0
    cleared_webhook_for = ""
    backoff = IDLE_SECONDS
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=POLL_SECONDS + 15)) as session:
        while True:
            try:
                settings = _settings()
                if not polling_wanted(settings):
                    await asyncio.sleep(IDLE_SECONDS)
                    continue
                token = str(settings["telegram_bot_token"]).strip()
                api = f"https://api.telegram.org/bot{token}"
                if cleared_webhook_for != token:
                    # Telegram refuses getUpdates while a webhook is set.
                    async with session.post(f"{api}/deleteWebhook") as resp:
                        await resp.read()
                    cleared_webhook_for, offset = token, 0
                async with session.get(f"{api}/getUpdates", params={
                    "offset": offset, "timeout": POLL_SECONDS, "allowed_updates": '["message"]',
                }) as resp:
                    data = await resp.json(content_type=None)
                if not isinstance(data, dict) or not data.get("ok"):
                    # A wrong token answers 401/404; don't hammer Telegram.
                    log.warning("Telegram polling refused: %s", (data or {}).get("description") if isinstance(data, dict) else data)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 300)
                    continue
                backoff = IDLE_SECONDS
                for update in data.get("result") or []:
                    offset = max(offset, int(update.get("update_id", 0)) + 1)
                    try:
                        await handle_telegram_update(update, from_polling=True)
                    except Exception:  # one bad update must not stop the loop
                        log.exception("Telegram update failed")
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                log.debug("Telegram polling error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)
            except Exception:  # never let the loop die
                log.exception("Telegram polling loop error")
                await asyncio.sleep(backoff)

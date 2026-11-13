"""Chat webhook delivery (Nov 2026).

- Webhooks answer immediately and run the agent in the background (Slack
  re-delivers after 3s; Telegram re-delivers slow/failed updates).
- Re-deliveries are dropped by delivery id (Telegram update_id, Slack
  event_id), so one message runs the agent once.
- Slack signatures fail closed (no secret = reject) and reject stale
  timestamps (replay protection).
- Slack ignores bot posts/subtypes (no reply loops); Telegram falls back to
  plain text when Markdown is rejected.
"""
import asyncio
import hashlib
import hmac
import json
import time

import pytest

from core import gateway

SECRET = "slack-signing-secret"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    gateway._seen_deliveries.clear()
    gateway._background_tasks.clear()
    gateway._save_settings({
        "telegram_allow_from": ["42"],
        "slack_allow_from": ["U1"],
        "slack_signing_secret": SECRET,
    })
    yield
    gateway._seen_deliveries.clear()


@pytest.fixture
def chat(monkeypatch):
    """Fake agent + fake senders; records what ran and what was sent."""
    record = {"ran": [], "sent": []}

    async def fake_route(text, source="", caller_id=""):
        record["ran"].append((source, caller_id, text))
        return f"reply to {text}"

    async def fake_send(target, text):
        record["sent"].append((target, text))
        return True

    monkeypatch.setattr(gateway, "_route_to_agent", fake_route)
    monkeypatch.setattr(gateway, "_telegram_send_message", fake_send)
    monkeypatch.setattr(gateway, "_slack_send_message", fake_send)
    return record


async def _drain():
    while gateway._background_tasks:
        await asyncio.gather(*list(gateway._background_tasks), return_exceptions=True)


def _tg(update_id, text="hi", chat_id=42):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _slack(event_id, text="hi", ts=None, secret=SECRET, **event_extra):
    body = json.dumps({
        "type": "event_callback",
        "event_id": event_id,
        "event": {"type": "message", "user": "U1", "channel": "C1", "text": text, **event_extra},
    })
    ts = str(int(time.time()) if ts is None else ts)
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return body, {"x-slack-signature": sig, "x-slack-request-timestamp": ts}


# ── Telegram ───────────────────────────────────────────────────────────

def test_telegram_returns_before_the_agent_finishes(monkeypatch):
    sent = []

    async def scenario():
        release = asyncio.Event()

        async def slow_route(text, source="", caller_id=""):
            await release.wait()
            return "done"

        async def send(target, text):
            sent.append(text)

        monkeypatch.setattr(gateway, "_route_to_agent", slow_route)
        monkeypatch.setattr(gateway, "_telegram_send_message", send)

        result = await asyncio.wait_for(gateway.handle_telegram_webhook(_tg(1)), timeout=1)
        assert result == {"ok": True, "queued": True}
        assert sent == []  # agent still running
        release.set()
        await _drain()

    asyncio.run(scenario())
    assert sent == ["done"]


def test_telegram_redelivery_runs_agent_once(chat):
    async def scenario():
        first = await gateway.handle_telegram_webhook(_tg(7))
        again = await gateway.handle_telegram_webhook(_tg(7))
        await _drain()
        return first, again

    first, again = asyncio.run(scenario())
    assert first["queued"] is True
    assert again == {"ok": True, "duplicate": True}
    assert chat["ran"] == [("telegram", "42", "hi")]
    assert chat["sent"] == [("42", "reply to hi")]


def test_telegram_distinct_updates_both_run(chat):
    async def scenario():
        await gateway.handle_telegram_webhook(_tg(1, "a"))
        await gateway.handle_telegram_webhook(_tg(2, "b"))
        await _drain()

    asyncio.run(scenario())
    assert [r[2] for r in chat["ran"]] == ["a", "b"]


def test_delivery_ids_expire_after_ttl(chat, monkeypatch):
    monkeypatch.setattr(gateway, "DELIVERY_TTL_SECONDS", -1)

    async def scenario():
        await gateway.handle_telegram_webhook(_tg(9))
        await gateway.handle_telegram_webhook(_tg(9))
        await _drain()

    asyncio.run(scenario())
    assert len(chat["ran"]) == 2


def test_disallowed_telegram_chat_is_not_queued(chat):
    async def scenario():
        r = await gateway.handle_telegram_webhook(_tg(3, chat_id=99))
        await _drain()
        return r

    assert asyncio.run(scenario()) == {"ok": True}
    assert chat["ran"] == []


def test_agent_crash_still_answers_the_chat(monkeypatch):
    sent = []

    async def boom(text, source="", caller_id=""):
        raise RuntimeError("model exploded")

    async def send(target, text):
        sent.append(text)

    monkeypatch.setattr(gateway, "_route_to_agent", boom)
    monkeypatch.setattr(gateway, "_telegram_send_message", send)

    async def scenario():
        await gateway.handle_telegram_webhook(_tg(4))
        await _drain()

    asyncio.run(scenario())
    assert sent == ["Error: model exploded"]


# ── Slack ──────────────────────────────────────────────────────────────

def test_slack_retry_runs_agent_once(chat):
    async def scenario():
        body, headers = _slack("Ev1")
        first = await gateway.handle_slack_webhook(body, headers)
        retry = await gateway.handle_slack_webhook(body, headers)
        await _drain()
        return first, retry

    first, retry = asyncio.run(scenario())
    assert first["queued"] is True
    assert retry == {"ok": True, "duplicate": True}
    assert chat["ran"] == [("slack", "C1", "hi")]


def test_slack_rejected_when_signing_secret_unset(chat):
    gateway._save_settings({"slack_allow_from": ["U1"]})
    body, headers = _slack("Ev2")
    assert asyncio.run(gateway.handle_slack_webhook(body, headers))["status"] == 401
    assert chat["ran"] == []


def test_slack_rejects_wrong_signature(chat):
    body, headers = _slack("Ev3", secret="not-the-secret")
    assert asyncio.run(gateway.handle_slack_webhook(body, headers))["status"] == 401


def test_slack_rejects_stale_timestamp_replay(chat):
    body, headers = _slack("Ev4", ts=int(time.time()) - 600)
    assert asyncio.run(gateway.handle_slack_webhook(body, headers))["status"] == 401
    assert chat["ran"] == []


def test_slack_url_verification_still_works():
    body = json.dumps({"type": "url_verification", "challenge": "abc"})
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    r = asyncio.run(gateway.handle_slack_webhook(
        body, {"x-slack-signature": sig, "x-slack-request-timestamp": ts}))
    assert r == {"challenge": "abc"}


@pytest.mark.parametrize("extra", [{"bot_id": "B1"}, {"subtype": "message_changed"}])
def test_slack_ignores_bot_posts_and_subtypes(chat, extra):
    async def scenario():
        body, headers = _slack("Ev5", **extra)
        r = await gateway.handle_slack_webhook(body, headers)
        await _drain()
        return r

    assert asyncio.run(scenario()) == {"ok": True}
    assert chat["ran"] == []


# ── Telegram send: Markdown fallback ───────────────────────────────────

class _FakeResp:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, statuses, calls):
        self.statuses, self.calls = list(statuses), calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return _FakeResp(self.statuses.pop(0))


@pytest.mark.parametrize("statuses,expected_ok,expected_calls", [
    ([200], True, 1),        # Markdown accepted
    ([400, 200], True, 2),   # Markdown rejected -> plain text accepted
    ([403], False, 1),       # bot blocked: don't retry
])
def test_telegram_send_falls_back_to_plain_text(monkeypatch, statuses, expected_ok, expected_calls):
    gateway._save_settings({"telegram_bot_token": "T"})
    calls = []
    monkeypatch.setattr(gateway.aiohttp, "ClientSession", lambda: _FakeSession(statuses, calls))
    ok = asyncio.run(gateway._telegram_send_message("42", "snake_case *oops"))
    assert ok is expected_ok
    assert len(calls) == expected_calls
    assert calls[0].get("parse_mode") == "Markdown"
    if expected_calls == 2:
        assert "parse_mode" not in calls[1]

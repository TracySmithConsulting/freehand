"""Voice (Nov 2026): Telegram voice notes, Telegram without a public
address, and the console's Speak button. No real Telegram, model or
microphone is used."""
import asyncio

import pytest
from fastapi.testclient import TestClient

import server
from core import agent_config, gateway, telegram_poll, voice


def save_settings(**values):
    s = agent_config._load_settings()
    s.update(values)
    agent_config._save_settings(s)


@pytest.fixture
def telegram(monkeypatch):
    sent, routed = [], []

    async def fake_send(chat_id, text):
        sent.append((chat_id, text))
        return True

    async def fake_route(text, source, caller_id):
        routed.append((text, source, caller_id))
        return "It's sunny."

    async def fake_download(file_id):
        return b"OggS-fake-audio"

    monkeypatch.setattr(gateway, "_telegram_send_message", fake_send)
    monkeypatch.setattr(gateway, "_route_to_agent", fake_route)
    monkeypatch.setattr(gateway, "_telegram_download", fake_download)
    gateway._told_unknown_chats.clear()
    return sent, routed


def run_update(update, **kw):
    async def go():
        out = await gateway.handle_telegram_update(update, **kw)
        await asyncio.gather(*list(gateway._background_tasks))
        return out
    return asyncio.run(go())


VOICE = {"update_id": 1, "message": {"chat": {"id": 42}, "voice": {"file_id": "abc", "duration": 3}}}


def test_a_voice_note_is_heard_answered_and_echoed(telegram, monkeypatch):
    sent, routed = telegram
    save_settings(telegram_allow_from=["42"])
    monkeypatch.setattr(voice, "transcribe", lambda data, suffix: {"text": "What's the weather?"})
    out = run_update(VOICE)
    assert out["queued"]
    assert routed == [("What's the weather?", "telegram", "42")]
    assert sent[0][1].startswith("You said: “What's the weather?”")
    assert "It's sunny." in sent[0][1]


def test_an_unclear_voice_note_gets_a_kind_reply(telegram, monkeypatch):
    sent, routed = telegram
    save_settings(telegram_allow_from=["42"])
    monkeypatch.setattr(voice, "transcribe", lambda d, s: {"error": "FreeHand didn't hear any words in that recording."})
    run_update({**VOICE, "update_id": 2})
    assert routed == []
    assert "couldn't understand that voice note" in sent[0][1] and "type your message" in sent[0][1]


def test_strangers_voice_notes_are_ignored(telegram, monkeypatch):
    sent, routed = telegram
    save_settings(telegram_allow_from=["7"])
    called = []
    monkeypatch.setattr(voice, "transcribe", lambda d, s: called.append(1) or {"text": "x"})
    run_update({**VOICE, "update_id": 3}, from_polling=True)
    assert sent == [] and routed == [] and called == []


def test_first_contact_while_nobody_is_allowed_gets_the_chat_id_once(telegram):
    sent, routed = telegram
    text = {"update_id": 5, "message": {"chat": {"id": 99}, "text": "hi"}}
    run_update(text, from_polling=True)
    run_update({**text, "update_id": 6}, from_polling=True)
    assert len(sent) == 1 and "99" in sent[0][1] and "Allowed Chat IDs" in sent[0][1]
    assert routed == []


def test_the_public_webhook_never_reveals_the_bot(telegram):
    sent, _ = telegram
    run_update({"update_id": 9, "message": {"chat": {"id": 99}, "text": "hi"}})
    assert sent == []


def test_polling_is_the_default_and_webhook_mode_turns_it_off():
    assert telegram_poll.polling_wanted({"telegram_bot_token": "123:abc"})
    assert not telegram_poll.polling_wanted({"telegram_bot_token": "123:abc", "telegram_mode": "webhook"})
    assert not telegram_poll.polling_wanted({})


# ── The console's Speak button ────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(server.app, headers={"X-API-Key": server.get_or_create_api_key()})


def test_speak_returns_the_words(client, monkeypatch):
    seen = []
    monkeypatch.setattr(voice, "transcribe", lambda data, suffix: seen.append((data, suffix)) or {"text": "hello there", "language": "en"})
    r = client.post("/api/voice/transcribe", content=b"webm-bytes", headers={"Content-Type": "audio/webm;codecs=opus"})
    assert r.json()["text"] == "hello there"
    assert seen == [(b"webm-bytes", ".webm")]


def test_speak_explains_problems(client, monkeypatch):
    monkeypatch.setattr(voice, "transcribe", lambda d, s: {"error": "FreeHand couldn't make out that recording."})
    r = client.post("/api/voice/transcribe", content=b"x", headers={"Content-Type": "audio/webm"})
    assert r.status_code == 400 and "couldn't make out" in r.json()["detail"]


def test_recordings_may_be_bigger_than_other_requests(client, monkeypatch):
    monkeypatch.setattr(voice, "transcribe", lambda d, s: {"text": "long"})
    big = b"0" * (2 * 1024 * 1024)
    assert client.post("/api/voice/transcribe", content=big, headers={"Content-Type": "audio/webm"}).status_code == 200


def test_speak_needs_the_api_key():
    assert TestClient(server.app).post("/api/voice/transcribe", content=b"x").status_code == 401


def test_empty_and_oversized_recordings_are_refused_before_the_model():
    assert "empty" in voice.transcribe(b"")["error"]
    assert "too long" in voice.transcribe(b"0" * (voice.MAX_AUDIO_BYTES + 1))["error"]


def test_the_console_page_is_never_served_stale(client):
    # Browsers kept the pre-update console, so voice fixes seemed broken.
    assert client.get("/").headers.get("cache-control") == "no-cache"


# ── WhatsApp voice notes (Nov 2026) ───────────────────────────────────

import base64


@pytest.fixture
def whatsapp(monkeypatch):
    sent, routed = [], []

    async def fake_send(jid, text):
        sent.append((jid, text))
        return True

    async def fake_route(text, source, caller_id):
        routed.append((text, source, caller_id))
        return "Rain this afternoon."

    monkeypatch.setattr(gateway, "_whatsapp_send_message", fake_send)
    monkeypatch.setattr(gateway, "_route_to_agent", fake_route)
    return sent, routed


def run_wa(**kw):
    async def go():
        out = await gateway.handle_whatsapp_message(**kw)
        await asyncio.gather(*list(gateway._background_tasks))
        return out
    return asyncio.run(go())


JID = "27821234567@s.whatsapp.net"


def test_a_whatsapp_voice_note_is_heard_answered_and_echoed(whatsapp, monkeypatch):
    sent, routed = whatsapp
    save_settings(whatsapp_allow_from=["+27 82 123 4567"])
    seen = []
    monkeypatch.setattr(voice, "transcribe", lambda data, suffix: seen.append((data, suffix)) or {"text": "Will it rain?"})
    audio = {"data_b64": base64.b64encode(b"OggS-voice").decode(), "mimetype": "audio/ogg; codecs=opus", "seconds": 3}
    out = run_wa(remote_jid=JID, text="", message_id="wa-1", audio=audio)
    assert out["voice"]
    assert seen == [(b"OggS-voice", ".ogg")]
    assert routed == [("Will it rain?", "whatsapp", JID)]
    assert sent[0][1].startswith("You said: “Will it rain?”") and "Rain this afternoon." in sent[0][1]


def test_whatsapp_voice_notes_from_strangers_are_ignored(whatsapp, monkeypatch):
    sent, routed = whatsapp
    save_settings(whatsapp_allow_from=["+27 11 000 0000"], whatsapp_allow_self=True)
    called = []
    monkeypatch.setattr(voice, "transcribe", lambda d, s: called.append(1) or {"text": "x"})
    audio = {"data_b64": base64.b64encode(b"x").decode()}
    assert run_wa(remote_jid=JID, text="", message_id="wa-2", audio=audio)["skipped"] == "allow_from"
    assert sent == [] and called == []


def test_whatsapp_voice_note_problems_get_a_kind_reply(whatsapp, monkeypatch):
    sent, _ = whatsapp
    save_settings(whatsapp_allow_from=["+27 82 123 4567"])
    monkeypatch.setattr(voice, "transcribe", lambda d, s: {"text": "never"})
    assert run_wa(remote_jid=JID, text="", message_id="wa-3", audio={"too_long": True})["skipped"] == "too_long"
    assert run_wa(remote_jid=JID, text="", message_id="wa-4", audio={"failed": True})["skipped"] == "no_audio"
    assert run_wa(remote_jid=JID, text="", message_id="wa-5", audio={"data_b64": "not base64!!"})["skipped"] == "bad_audio"
    assert "under 10 minutes" in sent[0][1] and "couldn't fetch" in sent[1][1] and "couldn't fetch" in sent[2][1]


def test_the_message_yourself_chat_can_send_voice_notes(whatsapp, monkeypatch):
    sent, routed = whatsapp
    save_settings(whatsapp_allow_from=[], whatsapp_allow_self=True)
    monkeypatch.setattr(voice, "transcribe", lambda d, s: {"text": "Remind me to call Mum"})
    run_wa(remote_jid=JID, text="", message_id="wa-6", from_self=True,
           audio={"data_b64": base64.b64encode(b"v").decode(), "mimetype": "audio/ogg"})
    assert routed and routed[0][0] == "Remind me to call Mum"


def test_the_whatsapp_endpoint_accepts_a_voice_note_without_text(monkeypatch):
    save_settings(bridge_secret="s" * 32)
    got = []

    async def fake_handle(remote_jid, text, message_id=None, from_self=False, audio=None):
        got.append((remote_jid, text, audio))
        return {"ok": True, "queued": True}
    monkeypatch.setattr(server, "handle_whatsapp_message", fake_handle)
    r = TestClient(server.app).post("/api/gateway/whatsapp", headers={"X-Bridge-Secret": "s" * 32},
                                    json={"remote_jid": JID, "text": "", "audio": {"data_b64": "AAAA"}})
    assert r.json()["queued"] and got[0][2] == {"data_b64": "AAAA"}

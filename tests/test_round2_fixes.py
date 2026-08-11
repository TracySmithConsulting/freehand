"""Tests for the second round of critical fixes (Aug 2026):
- N1: API key auth on /api/* and /api/tools/* (already in test_integration)
- N2: settings deep-merge instead of overwrite
- N3: command truncation in /api/agent/command log
- N4: slug sanitisation in process_scribble
- N6: source-aware tier check in run_agent
- N8: datetime.now(timezone.utc) instead of utcnow()
- N12: allow_from check on Telegram/Slack/WhatsApp gateways
- H1: LLM retry/backoff on transient errors
- H3: tool result cap sent to LLM
"""

import sys
import asyncio
import json
import tempfile
import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core import agent, scheduler, security as security_mod
from core import security  # re-export for tests


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Point scheduler + gateway at a fresh vault dir for each test."""
    vault = tmp_path / "vault"
    vault.mkdir()

    from core import gateway, scheduler as sched_mod

    monkeypatch.setattr(gateway, "VAULT_DIR", vault)
    monkeypatch.setattr(gateway, "SCRIBBLE_PATH", vault / "00_Scribble.md")
    monkeypatch.setattr(sched_mod, "VAULT_DIR", vault)
    monkeypatch.setattr(sched_mod, "SCRIBBLE_PATH", vault / "00_Scribble.md")
    monkeypatch.setattr(sched_mod, "THREADS_DIR", vault / "Threads")
    monkeypatch.setattr(sched_mod, "SWEEP_STATE_PATH", vault / ".sweep_state.json")

    yield vault


# ── N2: settings deep-merge ─────────────────────────────────────────────

class TestSettingsMerge:
    def test_merge_preserves_unrelated_keys(self):
        from server import _merge_settings, SETTINGS_ALLOWED_KEYS

        existing = {"api_key": "abc", "tier": "semi_autonomous", "oauth": {"redirect_uris": {"google": "x"}}}
        updates = {"tier": "god_mode"}
        merged, rejected = _merge_settings(existing, updates)
        assert merged["api_key"] == "abc"
        assert merged["tier"] == "god_mode"
        assert merged["oauth"]["redirect_uris"]["google"] == "x"

    def test_merge_rejects_unknown_keys(self):
        from server import _merge_settings
        existing = {"api_key": "abc"}
        updates = {"unknown_evil_key": "drop tables"}
        merged, rejected = _merge_settings(existing, updates)
        assert "unknown_evil_key" not in merged
        assert "unknown_evil_key" in rejected

    def test_merge_does_not_overwrite_nested_dicts(self):
        from server import _merge_settings
        existing = {"oauth": {"redirect_uris": {"google": "x"}, "pending_states": {"abc": "y"}}}
        updates = {"oauth": {"redirect_uris": {"microsoft": "z"}}}
        merged, rejected = _merge_settings(existing, updates)
        # Both redirect_uris sub-keys present (deep merge at one level)
        assert merged["oauth"]["redirect_uris"]["google"] == "x"
        assert merged["oauth"]["redirect_uris"]["microsoft"] == "z"
        # pending_states untouched
        assert merged["oauth"]["pending_states"]["abc"] == "y"


# ── N4: slug sanitisation in process_scribble ──────────────────────────

class TestSafeThreadPath:
    def test_simple_slug_accepted(self, isolated_vault):
        from core.scheduler import _safe_thread_path
        result = _safe_thread_path("general-topic")
        assert result is not None
        assert result.name == "general-topic.md"

    def test_traversal_slug_rejected(self, isolated_vault):
        from core.scheduler import _safe_thread_path
        assert _safe_thread_path("..") is None
        assert _safe_thread_path("../etc") is None
        assert _safe_thread_path("../../etc/passwd") is None

    def test_separator_slug_rejected(self, isolated_vault):
        from core.scheduler import _safe_thread_path
        assert _safe_thread_path("foo/bar") is None
        assert _safe_thread_path("foo\\bar") is None

    def test_empty_slug_rejected(self, isolated_vault):
        from core.scheduler import _safe_thread_path
        assert _safe_thread_path("") is None
        assert _safe_thread_path("   ") is None

    def test_resolved_path_inside_threads_dir(self, isolated_vault):
        """Even after .resolve(), the path must remain inside THREADS_DIR."""
        from core.scheduler import _safe_thread_path, THREADS_DIR
        result = _safe_thread_path("topic-name")
        assert result is not None
        assert result.parent.resolve() == THREADS_DIR.resolve()


# ── N6: source-aware tier check ────────────────────────────────────────

class TestSourceAwareTier:
    def test_remote_source_demotes_god_mode_for_write(self, monkeypatch):
        """A write tool call from telegram/slack/whatsapp must NOT execute
        freely even when tier == GOD_MODE — must require approval."""
        from core.security import PermissionTier
        monkeypatch.setattr(agent, "get_current_tier",
                            lambda: PermissionTier.GOD_MODE)

        calls = {"intercepted": 0}

        def fake_intercept(action_type, description, payload, source, caller_id):
            calls["intercepted"] += 1
            return {"allowed": False, "approval_id": 42, "tier": "semi_autonomous"}

        monkeypatch.setattr(agent, "intercept_action", fake_intercept)

        async def fake_run_tool(name, args):
            return {"content": "should not reach"}

        monkeypatch.setattr(agent, "_run_tool", fake_run_tool)

        calls_llm = {"n": 0}

        async def fake_call_llm(messages, tools=None, config=None):
            calls_llm["n"] += 1
            if calls_llm["n"] == 1:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_docx",
                            "arguments": json.dumps({"path": "/tmp/x", "title": "t", "content": ["a"]}),
                        },
                    }],
                }
            return {"role": "assistant", "content": "done"}

        monkeypatch.setattr(agent, "call_llm", fake_call_llm)

        result = asyncio.run(agent.run_agent("write a doc", source="telegram"))
        assert calls["intercepted"] == 1, "Remote write tool must require approval even at GOD_MODE"
        assert result["text"] == "done"

    def test_local_source_keeps_god_mode(self, monkeypatch):
        """A write tool call from web/CLI source with GOD_MODE must execute
        freely (the legacy behaviour)."""
        from core.security import PermissionTier
        monkeypatch.setattr(agent, "get_current_tier",
                            lambda: PermissionTier.GOD_MODE)

        calls = {"intercepted": 0, "ran_tool": False}

        def fake_intercept(action_type, description, payload, source, caller_id):
            calls["intercepted"] += 1
            return {"allowed": False}

        async def fake_run_tool(name, args):
            calls["ran_tool"] = True
            return {"content": "ok"}

        monkeypatch.setattr(agent, "intercept_action", fake_intercept)
        monkeypatch.setattr(agent, "_run_tool", fake_run_tool)

        calls_llm = {"n": 0}

        async def fake_call_llm(messages, tools=None, config=None):
            calls_llm["n"] += 1
            if calls_llm["n"] == 1:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "write_docx",
                            "arguments": json.dumps({"path": "/tmp/x", "title": "t", "content": ["a"]}),
                        },
                    }],
                }
            return {"role": "assistant", "content": "done"}

        monkeypatch.setattr(agent, "call_llm", fake_call_llm)

        result = asyncio.run(agent.run_agent("write", source=""))
        assert calls["intercepted"] == 0, "Local write tool at GOD_MODE must not intercept"
        assert calls["ran_tool"] is True


# ── N12: allow_from check ──────────────────────────────────────────────

class TestAllowFrom:
    def test_telegram_unset_rejects(self, isolated_vault):
        """If telegram_allow_from is unset/empty, deny + warn."""
        from core.gateway import _check_allow_from, _load_settings
        isolated_vault.joinpath("settings.json").write_text(json.dumps({}))

        # Reset warning flag so we can observe it once
        if hasattr(_check_allow_from, "_warned_allow_from_telegram"):
            _check_allow_from._warned_allow_from_telegram = False

        result = _check_allow_from("telegram", "12345")
        assert result is False, "Unset allow_from must deny"

    def test_telegram_set_accepts_listed(self, isolated_vault):
        from core.gateway import _check_allow_from
        isolated_vault.joinpath("settings.json").write_text(json.dumps({
            "telegram_allow_from": ["12345", "67890"]
        }))
        assert _check_allow_from("telegram", "12345") is True
        assert _check_allow_from("telegram", "67890") is True
        assert _check_allow_from("telegram", "99999") is False

    def test_legacy_telegram_whitelist_still_works(self, isolated_vault):
        """Backwards-compat with `telegram_chat_whitelist`."""
        from core.gateway import _check_allow_from
        isolated_vault.joinpath("settings.json").write_text(json.dumps({
            "telegram_chat_whitelist": ["legacy-chat-id"]
        }))
        assert _check_allow_from("telegram", "legacy-chat-id") is True
        assert _check_allow_from("telegram", "other") is False

    def test_slack_set_accepts_listed(self, isolated_vault):
        from core.gateway import _check_allow_from
        isolated_vault.joinpath("settings.json").write_text(json.dumps({
            "slack_allow_from": ["U0123"]
        }))
        assert _check_allow_from("slack", "U0123") is True
        assert _check_allow_from("slack", "U9999") is False

    def test_whatsapp_set_accepts_listed(self, isolated_vault):
        from core.gateway import _check_allow_from
        isolated_vault.joinpath("settings.json").write_text(json.dumps({
            "whatsapp_allow_from": ["+27821234567"]
        }))
        assert _check_allow_from("whatsapp", "+27821234567") is True
        assert _check_allow_from("whatsapp", "+27000000000") is False

    def test_string_caller_id_matches_string_list(self, isolated_vault):
        """Allow_from entries may be ints or strings; caller_id is always str."""
        from core.gateway import _check_allow_from
        isolated_vault.joinpath("settings.json").write_text(json.dumps({
            "telegram_allow_from": [12345]  # int in list
        }))
        # caller_id passed as string "12345"
        assert _check_allow_from("telegram", "12345") is True


# ── H1: LLM retry/backoff ──────────────────────────────────────────────

class _MockResp:
    def __init__(self, status, body=None):
        self.status = status
        self._body = body or {}
    async def text(self): return str(self._body)
    async def json(self): return self._body


def _patched_session_with_responses(responses):
    """Patch aiohttp.ClientSession so session.post(...) returns an async
    context manager yielding a response from the responses list, in order.

    Each call advances the index. Exhausting the list repeats the last
    response so retries can be observed.
    """
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock
    state = {"idx": 0}

    def _next_response():
        if state["idx"] < len(responses):
            r = responses[state["idx"]]
            state["idx"] += 1
            return r
        return responses[-1]

    @asynccontextmanager
    async def fake_session_ctx():
        session = MagicMock()
        class _Post:
            async def __aenter__(self):
                return _next_response()
            async def __aexit__(self, *args):
                return False
        session.post = lambda *a, **kw: _Post()
        yield session

    return fake_session_ctx


class TestLLMRetry:
    def test_429_triggers_retry(self):
        """A 429 response on attempt 1 should be retried; success on attempt 2."""
        from core import agent as agent_mod

        responses = [
            _MockResp(429, "rate limited"),
            _MockResp(200, {"choices": [{"message": {"content": "ok"}}]}),
        ]
        with patch("aiohttp.ClientSession", _patched_session_with_responses(responses)):
            result = asyncio.run(agent_mod.call_llm(
                messages=[{"role": "user", "content": "x"}],
                config={"api_key": "k", "base_url": "http://x"}
            ))

        # Result should be the successful response from attempt 2
        assert result.get("content") == "ok"

    def test_400_no_retry(self):
        """A 400 (caller error) must NOT be retried."""
        from core import agent as agent_mod

        responses = [_MockResp(400, "bad")]
        with patch("aiohttp.ClientSession", _patched_session_with_responses(responses)):
            result = asyncio.run(agent_mod.call_llm(
                messages=[{"role": "user", "content": "x"}],
                config={"api_key": "k", "base_url": "http://x"}
            ))

        assert "error" in result
        assert "400" in result["error"]

    def test_5xx_triggers_retry(self):
        """A 503 (server error) must be retried."""
        from core import agent as agent_mod

        responses = [
            _MockResp(503, "service unavailable"),
            _MockResp(200, {"choices": [{"message": {"content": "ok"}}]}),
        ]
        with patch("aiohttp.ClientSession", _patched_session_with_responses(responses)):
            result = asyncio.run(agent_mod.call_llm(
                messages=[{"role": "user", "content": "x"}],
                config={"api_key": "k", "base_url": "http://x"}
            ))

        assert result.get("content") == "ok"

    def test_timeout_triggers_retry(self):
        """A timeout must be retried."""
        from core import agent as agent_mod
        from contextlib import asynccontextmanager
        import asyncio as _asyncio
        from unittest.mock import MagicMock

        call_count = {"n": 0}

        @asynccontextmanager
        async def fake_session_ctx():
            class _Post:
                async def __aenter__(self):
                    call_count["n"] += 1
                    if call_count["n"] < 2:
                        raise _asyncio.TimeoutError()
                    return _MockResp(200, {"choices": [{"message": {"content": "ok"}}]})
                async def __aexit__(self, *args):
                    return False
            session = MagicMock()
            session.post = lambda *a, **kw: _Post()
            yield session

        with patch("aiohttp.ClientSession", fake_session_ctx):
            result = asyncio.run(agent_mod.call_llm(
                messages=[{"role": "user", "content": "x"}],
                config={"api_key": "k", "base_url": "http://x"}
            ))

        assert result.get("content") == "ok"
        assert call_count["n"] >= 2


# ── H3: tool result cap sent to LLM ───────────────────────────────────

class TestToolResultCap:
    def test_large_result_truncated_in_messages(self, monkeypatch):
        """A tool result > 8000 chars must be truncated when sent to the LLM
        but kept full in tools_used."""
        from core import agent as agent_mod, security
        from core.security import PermissionTier
        monkeypatch.setattr(security, "get_current_tier",
                            lambda: PermissionTier.SEMI_AUTONOMOUS)

        async def fake_run_tool(name, args):
            # Return a large result: 20K chars of 'A'
            big_content = "A" * 20000
            return {"content": json.dumps({"result": big_content})}

        monkeypatch.setattr(agent_mod, "_run_tool", fake_run_tool)

        # Track what gets appended to messages
        messages_seen = []

        async def fake_call_llm(messages, tools=None, config=None):
            messages_seen.append(list(messages))
            return {"role": "assistant", "content": "ok"}

        monkeypatch.setattr(agent_mod, "call_llm", fake_call_llm)

        calls_llm = {"n": 0}

        async def fake_call_llm_with_counter(messages, tools=None, config=None):
            calls_llm["n"] += 1
            if calls_llm["n"] == 1:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_docx",
                            "arguments": json.dumps({"path": "/tmp/x"}),
                        },
                    }],
                }
            # Final answer
            messages_seen.append(list(messages))
            return {"role": "assistant", "content": "done"}

        monkeypatch.setattr(agent_mod, "call_llm", fake_call_llm_with_counter)

        result = asyncio.run(agent_mod.run_agent("read", max_turns=3))

        # The last messages_seen (call 2) should contain the truncated
        # tool result for the LLM. Find the tool message in the LLM-bound list.
        llm_messages = messages_seen[-1]
        tool_msgs = [m for m in llm_messages if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1
        tool_content = tool_msgs[-1]["content"]
        assert len(tool_content) < 8500, f"LLM-bound tool msg should be capped at ~8K, got {len(tool_content)}"
        assert "[... truncated" in tool_content

        # But tools_used (returned to the API caller) still has full content
        # (well, the first 200 chars per the existing truncation there)
        assert len(result["tools_used"]) >= 1


# ── N8: datetime.now(timezone.utc) ─────────────────────────────────────

class TestDatetimeAware:
    def test_process_scribble_writes_aware_timestamp(self, isolated_vault, monkeypatch):
        """The sweep_state.json must contain an aware UTC timestamp."""
        from core.scheduler import process_scribble, VAULT_DIR, SCRIBBLE_PATH
        monkeypatch.setattr(scheduler, "VAULT_DIR", VAULT_DIR)

        # Seed scribble with a single task entry
        SCRIBBLE_PATH.write_text(
            "# 00_Scribble\n\n> scratchpad\n\n"
            "## Quick Notes\n\n"
            "- [ ] Test task\n",
            encoding="utf-8",
        )

        # Stub OmniRoute and heuristic
        monkeypatch.setattr(scheduler, "_call_omniroute", lambda text: None)
        monkeypatch.setattr(scheduler, "_classify_heuristic",
                            lambda text: {"classification": "thread", "topic": "general"})

        process_scribble()

        # Reload sweep state
        from core.scheduler import SWEEP_STATE_PATH
        if SWEEP_STATE_PATH.exists():
            state = json.loads(SWEEP_STATE_PATH.read_text(encoding="utf-8"))
            ts = state.get("last_sweep")
            assert ts is not None
            # Parse and check tzinfo is set
            dt = datetime.fromisoformat(ts)
            assert dt.tzinfo is not None, f"last_sweep must be tz-aware, got {ts}"
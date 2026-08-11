"""Tests for the four critical security/correctness fixes (Aug 2026):
- C1: agent loop stuck-detection (run_agent)
- C2: tool permission registry (TOOL_REGISTRY + get_tool_permission)
- C3: OAuth pending-state TTL sweep (_sweep_stale_pending_states + _parse_iso)
- C5: Slack approval URL base (_get_approval_base_url)

These tests isolate the vault dir via fixtures so they don't touch real settings.json.
"""

import sys
import asyncio
import json
import tempfile
import shutil
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core import agent, agent_config, security
from core.oauth import router as oauth_router


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_vault(tmp_path, monkeypatch):
    """Point security + oauth modules at a fresh vault dir for each test."""
    vault = tmp_path / "vault"
    vault.mkdir()

    monkeypatch.setattr(security, "VAULT_DIR", vault)
    monkeypatch.setattr(security, "SETTINGS_PATH", vault / "settings.json")
    monkeypatch.setattr(agent_config, "VAULT_DIR", vault)
    monkeypatch.setattr(agent_config, "SETTINGS_PATH", vault / "settings.json")

    yield vault


# ── C2: tool permission registry ───────────────────────────────────────

class TestToolPermissionRegistry:
    def test_registry_has_all_listed_tools(self):
        """Every tool in list_available_tools() must have a registry entry."""
        from core.agent_config import list_available_tools, TOOL_REGISTRY

        declared = {t["function"]["name"] for t in list_available_tools()}
        registered = set(TOOL_REGISTRY.keys())

        missing = declared - registered
        assert not missing, f"Tools declared but not in TOOL_REGISTRY: {missing}"

    def test_registry_only_has_known_classes(self):
        from core.agent_config import TOOL_REGISTRY
        valid = {"read", "write"}
        for name, klass in TOOL_REGISTRY.items():
            assert klass in valid, f"{name} has invalid permission class {klass!r}"

    def test_get_tool_permission_known_read(self):
        assert agent_config.get_tool_permission("read_docx") == "read"
        assert agent_config.get_tool_permission("search_memory") == "read"

    def test_get_tool_permission_known_write(self):
        assert agent_config.get_tool_permission("write_docx") == "write"
        assert agent_config.get_tool_permission("post_to_facebook") == "write"

    def test_get_tool_permission_unknown_fails_closed(self):
        """Unknown tools must be 'unknown' so the agent loop blocks them."""
        assert agent_config.get_tool_permission("does_not_exist") == "unknown"
        assert agent_config.get_tool_permission("") == "unknown"

    def test_no_dead_write_tools_in_registry(self):
        """Regression: send_email and create_meeting were in the old hardcoded set
        but never existed in execute_tool(). They must NOT be in the registry."""
        from core.agent_config import TOOL_REGISTRY
        assert "send_email" not in TOOL_REGISTRY
        assert "create_meeting" not in TOOL_REGISTRY


# ── C1: agent loop stuck-detection ─────────────────────────────────────

class TestAgentLoopSafety:
    def test_stuck_loop_breaks_after_3_identical_calls(self, monkeypatch):
        """If LLM emits the same tool call 3+ times in a row, the agent must abort."""
        # Force a known tier so we don't read the real settings.json
        monkeypatch.setattr(security, "get_current_tier",
                            lambda: security.PermissionTier.SEMI_AUTONOMOUS)

        # Count LLM calls so we can return a final answer after the loop trips
        calls = {"n": 0}

        async def fake_call_llm(messages, tools=None, config=None):
            calls["n"] += 1
            # Emit the same tool call repeatedly
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{calls['n']}",
                    "type": "function",
                    "function": {
                        "name": "read_docx",
                        "arguments": json.dumps({"path": "/tmp/test.docx"}),
                    },
                }],
            }

        async def fake_execute_tool(name, args):
            return {"content": "mock result"}

        monkeypatch.setattr(agent, "call_llm", fake_call_llm)
        monkeypatch.setattr(agent, "execute_tool", fake_execute_tool)

        result = asyncio.run(agent.run_agent("read the doc", max_turns=20))

        # Must have aborted with the stuck-loop error, not used all 20 turns
        assert "Agent loop detected" in (result.get("error") or "")
        # 3 identical calls trigger the break → 2 tool results in tools_used
        assert len(result["tools_used"]) < 20
        assert len(result["tools_used"]) >= 2

    def test_different_args_do_not_trip_loop_detector(self, monkeypatch):
        """Calls with different args to the same tool should NOT trip the detector."""
        monkeypatch.setattr(security, "get_current_tier",
                            lambda: security.PermissionTier.SEMI_AUTONOMOUS)

        calls = {"n": 0}
        MAX_CALLS = 5  # Number of distinct tool calls before we emit the final answer

        async def fake_call_llm(messages, tools=None, config=None):
            calls["n"] += 1
            if calls["n"] > MAX_CALLS:
                # Final answer after 5 distinct calls
                return {"role": "assistant", "content": "done"}
            # Vary the path so the loop detector sees different args
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{calls['n']}",
                    "type": "function",
                    "function": {
                        "name": "read_docx",
                        "arguments": json.dumps({"path": f"/tmp/file{calls['n']}.docx"}),
                    },
                }],
            }

        async def fake_execute_tool(name, args):
            return {"content": "ok"}

        monkeypatch.setattr(agent, "call_llm", fake_call_llm)
        monkeypatch.setattr(agent, "execute_tool", fake_execute_tool)

        result = asyncio.run(agent.run_agent("read 5 files", max_turns=10))

        # No stuck-loop error
        assert "Agent loop detected" not in (result.get("error") or "")
        # All 5 distinct calls should be in tools_used
        assert len(result["tools_used"]) == 5

    def test_unknown_tool_refused_in_semi_autonomous(self, monkeypatch):
        """Unknown tool must NOT execute — agent loop should reject it.

        After rejection, the LLM stub gives up (returns a final answer),
        so we verify:
        - execute_tool was never called (fail-closed)
        - the rejection message is recorded in tools_used
        """
        monkeypatch.setattr(security, "get_current_tier",
                            lambda: security.PermissionTier.SEMI_AUTONOMOUS)

        calls = {"n": 0}

        async def fake_call_llm(messages, tools=None, config=None):
            calls["n"] += 1
            # First call: try the unknown tool. Second call: give up.
            if calls["n"] == 1:
                return {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "this_tool_does_not_exist",
                            "arguments": "{}",
                        },
                    }],
                }
            return {"role": "assistant", "content": "i give up"}

        executed = {"called": False}

        async def fake_execute_tool(name, args):
            executed["called"] = True
            return {"content": "should not happen"}

        monkeypatch.setattr(agent, "call_llm", fake_call_llm)
        monkeypatch.setattr(agent, "execute_tool", fake_execute_tool)

        result = asyncio.run(agent.run_agent("do bad thing", max_turns=5))

        # CRITICAL: the unknown tool must never reach execute_tool
        assert executed["called"] is False, "Unknown tool was executed — fail-open bug"

        # The refusal must be recorded in tools_used (so the LLM can see why it failed)
        assert len(result["tools_used"]) == 1
        assert "not registered" in result["tools_used"][0]["result"]

        # And the LLM's final text comes through
        assert result["text"] == "i give up"


# ── C3: OAuth pending-state TTL sweep ───────────────────────────────────

class TestPendingStateSweep:
    def test_parse_iso_z_suffix(self):
        from core.oauth.router import _parse_iso
        result = _parse_iso("2026-08-11T12:00:00Z")
        assert result is not None
        assert result.tzinfo is not None

    def test_parse_iso_offset(self):
        from core.oauth.router import _parse_iso
        result = _parse_iso("2026-08-11T12:00:00+00:00")
        assert result is not None

    def test_parse_iso_empty_returns_none(self):
        from core.oauth.router import _parse_iso
        assert _parse_iso("") is None

    def test_parse_iso_garbage_returns_none(self):
        from core.oauth.router import _parse_iso
        assert _parse_iso("not-a-date") is None

    def test_sweep_drops_old_entries(self):
        from core.oauth.router import _sweep_stale_pending_states
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        settings = {
            "oauth": {"pending_states": {
                "old": {"service": "google", "label": "w", "created_at": old},
                "fresh": {"service": "google", "label": "w", "created_at": fresh},
            }}
        }
        mutated = _sweep_stale_pending_states(settings)
        assert mutated is True
        assert "old" not in settings["oauth"]["pending_states"]
        assert "fresh" in settings["oauth"]["pending_states"]

    def test_sweep_treats_missing_timestamp_as_expired(self):
        """Entries with no created_at must be dropped (safe default)."""
        from core.oauth.router import _sweep_stale_pending_states
        settings = {
            "oauth": {"pending_states": {
                "no_ts": {"service": "google", "label": "w"},
            }}
        }
        mutated = _sweep_stale_pending_states(settings)
        assert mutated is True
        assert settings["oauth"]["pending_states"] == {}

    def test_sweep_enforces_hard_cap_oldest_first(self):
        from core.oauth.router import _sweep_stale_pending_states, PENDING_STATE_HARD_CAP
        # Build 60 fresh entries — none are expired, but cap is 50.
        now = datetime.now(timezone.utc)
        settings = {
            "oauth": {"pending_states": {
                f"state_{i}": {"service": "google", "label": "w",
                               "created_at": (now - timedelta(seconds=i)).isoformat()}
                for i in range(60)
            }}
        }
        mutated = _sweep_stale_pending_states(settings)
        assert mutated is True
        assert len(settings["oauth"]["pending_states"]) == PENDING_STATE_HARD_CAP
        # The 10 oldest (i=50..59, largest i = oldest) must be gone
        remaining = set(settings["oauth"]["pending_states"].keys())
        assert "state_50" not in remaining
        assert "state_59" not in remaining
        # The 10 newest (i=0..9) must remain
        assert "state_0" in remaining
        assert "state_9" in remaining

    def test_sweep_returns_false_when_nothing_to_do(self):
        from core.oauth.router import _sweep_stale_pending_states
        settings = {"oauth": {"pending_states": {}}}
        assert _sweep_stale_pending_states(settings) is False

    def test_sweep_handles_missing_oauth_key(self):
        """Defensive: settings without 'oauth' key should not crash."""
        from core.oauth.router import _sweep_stale_pending_states
        assert _sweep_stale_pending_states({}) is False
        assert _sweep_stale_pending_states({"oauth": {}}) is False


# ── C5: Slack approval URL base ─────────────────────────────────────────

class TestPublicBaseUrl:
    def test_unset_falls_back_to_localhost(self, isolated_vault, capsys):
        # Clear the warned flag so we get the warning output
        security._get_approval_base_url._warned = False
        result = security._get_approval_base_url()
        assert result == "http://localhost:8000"
        # Warning was emitted to stdout
        captured = capsys.readouterr()
        assert "public_base_url not set" in captured.out

    def test_set_value_is_used(self, isolated_vault):
        security._save_settings({"public_base_url": "https://freehand.tail.ts.net"})
        result = security._get_approval_base_url()
        assert result == "https://freehand.tail.ts.net"

    def test_trailing_slash_stripped(self, isolated_vault):
        security._save_settings({"public_base_url": "https://freehand.tail.ts.net/"})
        result = security._get_approval_base_url()
        assert result == "https://freehand.tail.ts.net"

    def test_whitespace_only_falls_back(self, isolated_vault, capsys):
        security._get_approval_base_url._warned = False
        security._save_settings({"public_base_url": "   "})
        result = security._get_approval_base_url()
        assert result == "http://localhost:8000"
        # Whitespace-only should still emit the warning
        captured = capsys.readouterr()
        assert "public_base_url not set" in captured.out

    def test_warning_only_emitted_once_per_process(self, isolated_vault, capsys):
        security._get_approval_base_url._warned = False
        security._get_approval_base_url()  # 1st call — warning
        security._get_approval_base_url()  # 2nd call — silent
        security._get_approval_base_url()  # 3rd call — silent
        captured = capsys.readouterr()
        assert captured.out.count("public_base_url not set") == 1
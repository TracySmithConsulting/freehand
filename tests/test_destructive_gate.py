"""Round 15 Slice 2 — the invocation-time destructive gate.

The enable-time gate (commit 4803f68) confirmed *enabling* writes. This
slice covers *invoking* them: a DESTRUCTIVE OC action (persisted risk ==
"destructive") must ALWAYS pause for an out-of-band approval, even at
GOD_MODE — closing the foot-gun where github.delete_repo ran unattended
in the highest-trust tier. Non-destructive writes keep today's tier
behaviour (GOD_MODE still auto-runs them).

Three slices:
  A. registry exposes a per-tool risk accessor (is_destructive_tool)
  B. security.intercept_action(force_confirm=True) pauses even at GOD_MODE
  C. the agent loop force-confirms destructive OC tools at GOD_MODE
     (and does NOT confirm plain writes)

These are RED tests: they fail until the implementation lands.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

# Project root, derived portably from this file's location (no hardcoded
# Windows sys.path line — R14 CI lesson applied up front).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import security, agent, agent_config  # noqa: E402
from core.tools import registry  # noqa: E402
from core.database import init_db  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Point the approvals DB at a throwaway file and create the schema.

    mirrors test_round3_fixes.py's idiom: patch both core.database.DB_PATH
    and core.security.DB_PATH to the same tmp file, then init_db()."""
    db = tmp_path / "agent.db"
    monkeypatch.setattr("core.database.DB_PATH", db)
    monkeypatch.setattr("core.security.DB_PATH", db)
    init_db()
    return db


def _set_tier(monkeypatch, tier):
    """Force the tier in BOTH modules that read it: security (used by
    intercept_action) and agent (used by run_agent's loop-level tier)."""
    monkeypatch.setattr(security, "get_current_tier", lambda: tier)
    monkeypatch.setattr(agent, "get_current_tier", lambda: tier)


@pytest.fixture(autouse=True)
def _fresh_tool_state():
    """Snapshot TOOL_REGISTRY/TOOL_SCHEMAS so registry merges + the
    Slice-C registry overrides don't leak between tests."""
    saved_reg = dict(agent_config.TOOL_REGISTRY)
    saved_sch = dict(agent_config.TOOL_SCHEMAS)
    yield
    for k in [k for k in agent_config.TOOL_REGISTRY if k not in saved_reg]:
        agent_config.TOOL_REGISTRY.pop(k, None)
    for k in [k for k in agent_config.TOOL_SCHEMAS if k not in saved_sch]:
        agent_config.TOOL_SCHEMAS.pop(k, None)


# ── Slice A: registry per-tool risk accessor ─────────────────────────


class TestRegistryRiskAccessor:
    def test_is_destructive_tool_reads_persisted_risk(self, tmp_path, monkeypatch):
        """After enable_writes, a destructive OC tool is flagged; a plain
        write and a read are not. Reads the persisted risk field, not the
        read/write-only TOOL_REGISTRY."""
        from tests.test_enable_writes_gate import GITHUB_ACTIONS

        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(registry, "STORAGE_PATH", vault / "tool_registry.json")

        def fake_actions(svc, label="default"):
            return GITHUB_ACTIONS if svc == "github" else []
        monkeypatch.setattr(registry, "_get_provider_actions", fake_actions)
        monkeypatch.setattr(registry, "_has_credential", lambda s, l: True)

        registry.discover_tools("github", "default")   # reads only
        registry.enable_writes("github", "default")     # promotes write+destructive

        assert registry.is_destructive_tool("oc_github_default_delete_repo") is True
        # create_gist is write (not destructive); get_user is read:
        assert registry.is_destructive_tool("oc_github_default_create_gist") is False
        assert registry.is_destructive_tool("oc_github_default_get_user") is False
        # unknown tool -> not destructive (fail-safe):
        assert registry.is_destructive_tool("oc_github_default__nope") is False


# ── Slice B: security.intercept_action(force_confirm=...) ────────────


class TestForceConfirmGate:
    def test_force_confirm_pauses_at_god_mode(self, tmp_db, monkeypatch):
        """The foot-gun fix: a destructive action must create a pending
        approval EVEN in GOD_MODE (where every external action
        auto-approves today)."""
        _set_tier(monkeypatch, security.PermissionTier.GOD_MODE)
        result = security.intercept_action(
            "oauth_access", "delete repo",
            {"tool": "oc_github_default_delete_repo"},
            source="cli", caller_id="tracy", force_confirm=True,
        )
        assert result["allowed"] is False
        assert result.get("approval_id") is not None

    def test_god_mode_still_auto_approves_without_force(self, tmp_db, monkeypatch):
        """Regression guard: a plain external action at GOD_MODE with NO
        force_confirm auto-approves immediately (unchanged behaviour)."""
        _set_tier(monkeypatch, security.PermissionTier.GOD_MODE)
        result = security.intercept_action(
            "oauth_access", "post message",
            {"tool": "oc_slack_default_post_message"},
            source="cli",
        )
        assert result["allowed"] is True
        assert result.get("approval_id") is None

    def test_force_confirm_denied_at_autonomous(self, tmp_db, monkeypatch):
        """AUTONOMOUS denies a destructive call outright (no approval
        row) — same posture as any external action under that tier."""
        _set_tier(monkeypatch, security.PermissionTier.AUTONOMOUS)
        result = security.intercept_action(
            "oauth_access", "delete repo",
            force_confirm=True, source="cli",
        )
        assert result["allowed"] is False
        assert result.get("approval_id") is None


# ── Slice C: agent-loop hook ─────────────────────────────────────────
# Drives run_agent with a fake LLM that emits ONE tool call then a final
# answer, at GOD_MODE. The loop must force-confirm destructive OC tools
# (execute_tool NOT called; an approval row is created) but still run
# plain writes unattended (execute_tool called) — that's the behaviour
# we're preserving.

class TestAgentLoopForceConfirm:
    def _drive(self, tmp_db, monkeypatch, tool_name, destructive, tier):
        _set_tier(monkeypatch, tier)
        # TOOL_REGISTRY: put the tool in as a 'write' so the loop's
        # write-branch is entered.
        agent_config.TOOL_REGISTRY[tool_name] = "write"
        # Control the destructive flag deterministically (decouples the
        # loop test from persisted registry storage).
        monkeypatch.setattr(
            registry, "is_destructive_tool",
            lambda name: name == tool_name and destructive,
        )

        calls = {"executed": [], "llm_n": 0}

        async def fake_call_llm(messages, tools=None, config=None):
            calls["llm_n"] += 1
            if calls["llm_n"] > 1:
                return {"role": "assistant", "content": "done"}
            return {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": tool_name,
                                 "arguments": json.dumps({"repo": "x/y"})},
                }],
            }

        async def fake_execute_tool(name, args):
            calls["executed"].append(name)
            return {"content": "ok"}

        monkeypatch.setattr(agent, "call_llm", fake_call_llm)
        monkeypatch.setattr(agent, "execute_tool", fake_execute_tool)

        result = asyncio.run(agent.run_agent("act", max_turns=3))
        return calls, result

    def test_destructive_tool_held_for_approval_at_god_mode(
            self, tmp_db, monkeypatch):
        """The foot-gun: at GOD_MODE a destructive OC tool must NOT fire
        unattended — it pauses for an out-of-band approval."""
        calls, _ = self._drive(
            tmp_db, monkeypatch,
            "oc_github_default_delete_repo", destructive=True,
            tier=security.PermissionTier.GOD_MODE,
        )
        assert "oc_github_default_delete_repo" not in calls["executed"], (
            "destructive tool ran unattended at GOD_MODE — force-confirm "
            "gate is missing"
        )
        # An approval was created for it:
        pending = security.get_pending_approvals()
        matched = [a for a in pending
                   if "delete_repo" in a["description"]
                   or "delete_repo" in (a["payload"] or "")]
        assert matched, "no pending approval was created for the destructive call"

    def test_plain_write_still_runs_at_god_mode(self, tmp_db, monkeypatch):
        """Regression guard: a non-destructive write keeps today's
        GOD_MODE behaviour — it executes without pausing."""
        calls, _ = self._drive(
            tmp_db, monkeypatch,
            "oc_slack_default_post_message", destructive=False,
            tier=security.PermissionTier.GOD_MODE,
        )
        assert "oc_slack_default_post_message" in calls["executed"], (
            "plain write was unexpectedly gated at GOD_MODE — it should run"
        )

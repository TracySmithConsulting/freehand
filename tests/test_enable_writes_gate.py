"""Round 15 — the `enable-writes` confirmation gate.

The OC catalog's write + destructive actions were already promotable
(Round 10's `enable_writes`), but three things were missing:

1. Enabled writes did not survive a routine `tools refresh` —
   `discover_tools()` wipes the (service, label) entries and
   re-persists reads only, so a refresh silently re-locked writes.
2. No confirmation at enable-time — `enable-writes` promoted 66
   GitHub actions with no "yep, including 30 destructive" step.
3. No destructive-vs-write distinction surfaced anywhere.

These tests pin the intended behaviour (RED before the fix):

  - enable-writes SETS A PERSISTENT MARKER; refresh() preserves it.
  - the CLI prompts for confirmation (destructive count shown),
    `--yes` bypasses it, declining aborts without enabling.
  - `pending_writes_summary` reports (writes, destructive) counts
    so the confirmation prompt can be informative.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

# Project root, derived portably from this file's location so the test
# imports work on any OS / CI checkout (no hardcoded Windows path —
# that string is a *relative* component on POSIX and poisons sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools import registry  # noqa: E402
from core.oauth import credential_store  # noqa: E402
from cli import app  # noqa: E402


# ── OC catalog fixtures (Round 13 action-centric shape) ──────────────
# Copied from tests/test_tool_registry.py (which has a hardcoded
# Windows sys.path line we don't want to drag into this file).

SLACK_ACTIONS = [
    {"id": "slack.list_channels", "service": "slack", "name": "list_channels",
     "description": "List Slack public channels.", "operationType": "read",
     "requiredScopes": ["channels:read"],
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"id": "slack.post_message", "service": "slack", "name": "post_message",
     "description": "Post a message.", "operationType": "write",
     "requiredScopes": ["chat:write"],
     "inputSchema": {"type": "object",
                     "properties": {"channel_id": {"type": "string"},
                                    "text": {"type": "string"}},
                     "required": ["channel_id", "text"]}},
    {"id": "slack.delete_message", "service": "slack", "name": "delete_message",
     "description": "Delete a message.", "operationType": "destructive",
     "requiredScopes": ["chat:write"],
     "inputSchema": {"type": "object",
                     "properties": {"channel_id": {"type": "string"},
                                    "ts": {"type": "string"}}}},
    {"id": "slack.conversations_history", "service": "slack",
     "name": "conversations_history",
     "description": "Read channel message history.", "operationType": "sensitive",
     "requiredScopes": ["channels:history"],
     "inputSchema": {"type": "object",
                     "properties": {"channel_id": {"type": "string"}}}},
]

GITHUB_ACTIONS = [
    {"id": "github.get_user", "service": "github", "name": "get_user",
     "description": "Read the authenticated user's profile.",
     "operationType": "read", "requiredScopes": ["read:user"],
     "inputSchema": {"type": "object", "properties": {}}},
    {"id": "github.create_gist", "service": "github", "name": "create_gist",
     "description": "Create a gist.", "operationType": "write",
     "requiredScopes": ["gist"],
     "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}},
                     "required": ["content"]}},
    {"id": "github.delete_repo", "service": "github", "name": "delete_repo",
     "description": "Delete a repository.", "operationType": "destructive",
     "requiredScopes": ["delete_repo"],
     "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}},
                     "required": ["repo"]}},
]


def _stub_oc_catalog(monkeypatch, by_service):
    def fake_get_actions(service_id, label="default"):
        return by_service.get(service_id, [])
    monkeypatch.setattr("core.tools.registry._get_provider_actions",
                       fake_get_actions)
    monkeypatch.setattr("core.tools.registry._search_actions",
                        lambda service_id, label="default": [])


def _isolated_registry(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(registry, "VAULT_DIR", vault)
    monkeypatch.setattr(registry, "STORAGE_PATH", vault / "tool_registry.json")
    monkeypatch.setattr(credential_store, "VAULT_DIR", vault)
    monkeypatch.setattr(credential_store, "STORAGE_PATH",
                        vault / "credential_store.json")
    return vault


@pytest.fixture(autouse=True)
def _isolate_global_tool_state():
    """Snapshot TOOL_SCHEMAS / TOOL_REGISTRY per test (same fixture as
    test_tool_registry.py — the registry mutates both dicts)."""
    from core import agent_config
    saved_schemas = dict(agent_config.TOOL_SCHEMAS)
    saved_registry = dict(agent_config.TOOL_REGISTRY)
    yield
    for k in [k for k in agent_config.TOOL_SCHEMAS if k not in saved_schemas]:
        agent_config.TOOL_SCHEMAS.pop(k, None)
    for k in [k for k in agent_config.TOOL_REGISTRY if k not in saved_registry]:
        agent_config.TOOL_REGISTRY.pop(k, None)


def _tool_names() -> list:
    return sorted(t["schema"]["function"]["name"] for t in registry.list_tools())


# ── Slice 1: enabled writes survive refresh() ─────────────────────────


class TestEnableWritesPersistence:
    def test_enabled_writes_survive_refresh(self, tmp_path, monkeypatch):
        """The core R15 bug: a routine `tools refresh` used to re-lock
        enabled writes (discover_tools wipes + re-persists reads only)."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)

        registry.discover_tools("slack", "default")
        registry.enable_writes("slack", "default")
        assert "oc_slack_default_post_message" in _tool_names()

        # A routine re-probe (CLI `tools refresh` / server startup)
        # must KEEP the enabled writes, not silently drop them.
        registry.refresh()
        names = _tool_names()
        assert "oc_slack_default_post_message" in names, (
            "refresh() re-locked enabled writes — the persistence marker "
            "is missing or not honoured by discover_tools"
        )
        assert "oc_slack_default_delete_message" in names
        assert "oc_slack_default_conversations_history" in names

    def test_enabled_writes_survive_repeated_refreshes(self, tmp_path, monkeypatch):
        """Two refreshes in a row (the CLI case: user runs refresh often)
        both keep the writes."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)

        registry.discover_tools("slack", "default")
        registry.enable_writes("slack", "default")
        registry.refresh()
        registry.refresh()
        assert "oc_slack_default_post_message" in _tool_names()

    def test_marker_lives_in_storage(self, tmp_path, monkeypatch):
        """The enabled-writes state must be persisted on disk
        (surviving a CLI→server process boundary), not just in memory."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)

        registry.discover_tools("slack", "default")
        registry.enable_writes("slack", "default")

        storage = json.loads((tmp_path / "vault" / "tool_registry.json")
                             .read_text())
        marker = storage.get("writes_enabled") or storage.get("enabled_writes")
        assert marker, (
            "no writes_enabled marker in tool_registry.json — "
            "enabled writes cannot survive a process restart or refresh()"
        )

    def test_disable_clears_marker(self, tmp_path, monkeypatch):
        """After `tools disable <svc>`, a refresh must NOT resurrect
        the writes. (Reads re-register on refresh — that's expected and
        correct; the point is the WRITE state stays off.)"""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)

        registry.discover_tools("slack", "default")
        registry.enable_writes("slack", "default")
        registry.disable("slack", "default")
        registry.refresh()
        names = _tool_names()
        # Writes stay locked (the marker was cleared by disable)
        assert "oc_slack_default_post_message" not in names
        assert "oc_slack_default_delete_message" not in names
        # Reads legitimately re-register on the refresh (credential still
        # present) — that's the correct behaviour, not a leak.
        assert "oc_slack_default_list_channels" in names


# ── Slice 2: confirmation at enable-time (CLI) ────────────────────────


class TestEnableWritesConfirmation:
    def _enabled(self) -> bool:
        return "oc_slack_default_post_message" in _tool_names()

    def test_prompt_shows_destructive_count_and_aborts_on_no(
            self, tmp_path, monkeypatch):
        """Declining the confirmation leaves writes DISABLED and exits
        non-zero (nothing partially enabled)."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)
        registry.discover_tools("slack", "default")

        result = CliRunner().invoke(
            app, ["tools", "enable-writes", "slack"], input="n\n")
        assert result.exit_code != 0, (
            f"declining the confirmation must exit non-zero; "
            f"got {result.exit_code}: {result.stdout}"
        )
        assert not self._enabled(), "declined confirmation must not enable"

    def test_confirmation_yes_enables(self, tmp_path, monkeypatch):
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)
        registry.discover_tools("slack", "default")

        result = CliRunner().invoke(
            app, ["tools", "enable-writes", "slack"], input="y\n")
        assert result.exit_code == 0, result.stdout
        assert self._enabled()

    def test_confirmation_message_mentions_destructive(
            self, tmp_path, monkeypatch):
        """SLACK_ACTIONS has 1 destructive action (delete_message). The
        prompt/summary must surface that — '3 write actions' alone would
        be misleading."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)
        registry.discover_tools("slack", "default")

        result = CliRunner().invoke(
            app, ["tools", "enable-writes", "slack"], input="y\n")
        assert "destructive" in result.stdout.lower(), (
            f"prompt should surface the destructive count, got: "
            f"{result.stdout!r}"
        )

    def test_yes_flag_bypasses_prompt(self, tmp_path, monkeypatch):
        """`--yes` (no interactive input at all) enables without prompt —
        for scripted / automation use."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)
        registry.discover_tools("slack", "default")

        result = CliRunner().invoke(
            app, ["tools", "enable-writes", "slack", "--yes"])
        assert result.exit_code == 0, result.stdout
        assert self._enabled()

    def test_no_confirmation_needed_when_nothing_pending(
            self, tmp_path, monkeypatch):
        """A service with no write/destructive actions exits non-zero
        (as before) — but without any prompt (nothing to confirm)."""
        _isolated_registry(tmp_path, monkeypatch)
        # github catalog here has only read actions
        _stub_oc_catalog(monkeypatch,
                         {"github": [a for a in GITHUB_ACTIONS
                                     if a["operationType"] == "read"]})
        credential_store.add("github", "default", "ghp-X", "api_key")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)
        registry.discover_tools("github", "default")

        result = CliRunner().invoke(
            app, ["tools", "enable-writes", "github"])  # no input fed
        assert result.exit_code != 0
        # Nothing was enabled (no writes/destructive in the catalog), so
        # no "Enabled N ... tools" success line should appear.
        assert "Enabled" not in result.stdout


# ── Slice 3: destructive-vs-write distinction (registry API) ─────────


class TestPendingWritesSummary:
    def test_counts_writes_and_destructive_separately(self, tmp_path, monkeypatch):
        """pending_writes_summary reports (write, destructive) so the
        confirmation prompt can say '3 write + 30 destructive'."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"github": GITHUB_ACTIONS})
        credential_store.add("github", "default", "ghp-X", "api_key")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)

        writes, destructive = registry.pending_writes_summary(
            "github", "default")
        assert writes == 1, "create_gist is write"
        assert destructive == 1, "delete_repo is destructive"

    def test_sensitive_counts_as_write_not_destructive(self, tmp_path, monkeypatch):
        """operationType=sensitive (e.g. channels:history) is gated with
        the writes but is NOT destructive."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(registry, "_has_credential",
                            lambda s, l: True)

        writes, destructive = registry.pending_writes_summary(
            "slack", "default")
        # slack fixture: post_message(write) + conversations_history
        # (sensitive→write bucket) = 2 writes; delete_message = 1 destructive
        assert writes == 2
        assert destructive == 1

    def test_unknown_service_returns_zeroes(self, tmp_path, monkeypatch):
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {})
        assert registry.pending_writes_summary("nope", "default") == (0, 0)

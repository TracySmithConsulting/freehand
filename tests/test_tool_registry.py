"""Tests for the tool discovery + registration layer (Round 10 PR 2).

Mirrors the OC catalog shape captured live from oomol-lab/open-connector
(Pitfall 30 — fixture matches the wire format the handler shape will
see in production, not a hand-rolled guess):

  Each authorizationOption:
    {
      "id": "channels:read",
      "label": "Public channels",
      "description": "List public Slack channels...",
      "required": true,
      "defaultSelected": true,
      "risk": "standard" | "sensitive" | "destructive",
      "requires": ["channels:read"]  // optional list of dependent scopes
    }

Classification rule (Tracy 02 Oct 2026, refined from live probe):
  risk == 'standard'           → auto-register as read tool
  risk in {'sensitive','destructive'} → behind 'freehand tools
                                     enable-writes <service>'
  'defaultSelected' is OC's consent-screen UX signal, NOT a safety
  signal. Slack uses risk=sensitive for read actions like
  channels:history. We don't auto-register those.

Tool naming (Tracy 02 Oct 2026):
  oc_<service>_<label>_<action_id>  — always labeled, even for
  single-credential services (label='default' is implicit). The LLM
  learns one naming pattern.

Schema format (Tracy 02 Oct 2026):
  OpenAI function-calling shape — matches core/agent_config.py's
  existing TOOL_SCHEMAS format. LLM dispatch loop doesn't need a
  translation layer.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.tools import registry  # noqa: E402


# ── Live OC catalog fixture ─────────────────────────────────────────────
# Captured from `GET /api/oauth/configs` against oomol-lab/open-connector
# on 02 Oct 2026 (Round 10 Task 2.5 fixture). Pinning per Pitfall 30.

SLACK_CATALOG_OPTIONS = [
    {"id": "channels:read", "label": "Public channels",
     "description": "List public Slack channels and read their metadata.",
     "required": True, "defaultSelected": True, "risk": "standard"},
    {"id": "channels:history", "label": "Public channel messages",
     "description": "Read message history in public Slack channels.",
     "required": False, "defaultSelected": True, "risk": "sensitive",
     "requires": ["channels:read"]},
    {"id": "chat:write", "label": "Send messages",
     "description": "Send messages as the connected Slack user.",
     "required": False, "defaultSelected": True, "risk": "sensitive"},
    {"id": "chat:delete", "label": "Delete messages",
     "description": "Delete messages sent by the app.",
     "required": False, "defaultSelected": False, "risk": "destructive"},
    {"id": "users:read", "label": "User directory",
     "description": "Read Slack user profiles and directory information.",
     "required": False, "defaultSelected": True, "risk": "standard"},
    {"id": "im:history", "label": "Direct message history",
     "description": "Read direct-message history.",
     "required": False, "defaultSelected": True, "risk": "sensitive",
     "requires": ["im:read"]},
]

GITHUB_CATALOG_OPTIONS = [
    {"id": "repo", "label": "Repository access",
     "description": "Read and write access to repositories.",
     "required": True, "defaultSelected": True, "risk": "sensitive"},
    {"id": "read:user", "label": "User profile",
     "description": "Read the authenticated user's profile.",
     "required": True, "defaultSelected": True, "risk": "standard"},
    {"id": "gist", "label": "Gists",
     "description": "Create and modify gists.",
     "required": False, "defaultSelected": False, "risk": "sensitive"},
    {"id": "delete_repo", "label": "Delete repositories",
     "description": "Delete repositories.",
     "required": False, "defaultSelected": False, "risk": "destructive"},
]


def _stub_oc_catalog(monkeypatch, by_service):
    """Stub core.oauth.open_connector.get_provider_actions to return canned data."""
    def fake_get_actions(service_id, label="default"):
        return by_service.get(service_id, [])
    monkeypatch.setattr(
        "core.tools.registry._get_provider_actions",
        fake_get_actions,
    )


def _isolated_registry(tmp_path, monkeypatch):
    """Redirect registry storage to a tmp dir. The registry persists its
    enabled-tools list in vault/tool_registry.json."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(registry, "VAULT_DIR", vault)
    monkeypatch.setattr(registry, "STORAGE_PATH", vault / "tool_registry.json")
    return vault


# ── Action classification ───────────────────────────────────────────────


class TestClassifyAction:
    def test_standard_is_read(self):
        """risk=standard → register as read tool immediately."""
        action = {"id": "channels:read", "risk": "standard"}
        assert registry.classify_action(action) == "read"

    def test_sensitive_is_write(self):
        """risk=sensitive → behind enable-writes (read AND write actions
        like channels:history are both gated together)."""
        action = {"id": "channels:history", "risk": "sensitive"}
        assert registry.classify_action(action) == "write"

    def test_destructive_is_write(self):
        """risk=destructive → behind enable-writes (more dangerous than
        sensitive — labels show the user what they're unlocking)."""
        action = {"id": "chat:delete", "risk": "destructive"}
        assert registry.classify_action(action) == "write"

    def test_missing_risk_defaults_to_write(self):
        """If OC adds a new risk value without us updating, default to
        write (the safe-by-default posture)."""
        action = {"id": "experimental", "risk": "experimental"}
        assert registry.classify_action(action) == "write"


# ── Tool naming ────────────────────────────────────────────────────────


class TestBuildToolName:
    def test_single_credential_uses_label(self):
        """Even single-credential services get the label in the name.
        Tracy's decision 02 Oct 2026: uniform shape."""
        name = registry.build_tool_name("slack", "default", "channels:read")
        assert name == "oc_slack_default_channels:read"

    def test_multi_credential_uses_label(self):
        name = registry.build_tool_name("github", "work", "repo")
        assert name == "oc_github_work_repo"

    def test_label_normalization(self):
        """Labels are lowercased; action IDs preserve their colon."""
        name = registry.build_tool_name("GitHub", "WORK", "repo:read")
        assert name == "oc_github_work_repo:read"


# ── Schema generation ──────────────────────────────────────────────────


class TestBuildToolSchema:
    def test_schema_shape_is_openai_function_calling(self):
        """Tool schema matches core/agent_config.TOOL_SCHEMAS shape
        (Pitfall 26 + Tracy 02 Oct 2026 decision: OpenAI function-calling)."""
        action = {
            "id": "channels:read",
            "label": "Public channels",
            "description": "List public Slack channels and read their metadata.",
        }
        schema = registry.build_tool_schema("slack", "default", action)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "oc_slack_default_channels:read"
        assert "Public channels" in schema["function"]["description"]
        assert "parameters" in schema["function"]
        assert "required" in schema["function"]["parameters"]
        # Pitfall 26: required is mandatory even for empty
        assert schema["function"]["parameters"]["required"] == []

    def test_schema_includes_input_schema_when_provided(self):
        """If OC provides input_fields (parameter definitions), the
        schema's parameters include them with proper types."""
        action = {
            "id": "chat.postMessage",
            "label": "Send messages",
            "description": "Send a message to a channel.",
            "inputFields": [
                {"name": "channel", "type": "string", "required": True,
                 "description": "Channel ID"},
                {"name": "text", "type": "string", "required": True,
                 "description": "Message text"},
            ],
        }
        schema = registry.build_tool_schema("slack", "default", action)
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        assert "channel" in params["properties"]
        assert "text" in params["properties"]
        assert params["properties"]["channel"]["type"] == "string"
        assert params["required"] == ["channel", "text"]


# ── Discovery from OC catalog ──────────────────────────────────────────


class TestDiscoverTools:
    def test_discovers_only_standard_actions(self, tmp_path, monkeypatch):
        """Only risk=standard actions become read tools. sensitive and
        destructive stay on the 'to enable' list."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_CATALOG_OPTIONS})
        # Add a credential so the service is eligible for tool discovery
        from core.oauth import credential_store
        credential_store.add("slack", "default", "xoxb-FAKE", "oauth")
        # Stub has() to return True so the registry sees slack/credential
        monkeypatch.setattr(
            "core.tools.registry._has_credential",
            lambda service, label: True,
        )

        result = registry.discover_tools("slack", "default")
        # result is a dict: registered (read tools), pending_writes
        registered = result["registered"]
        pending = result["pending_writes"]

        # 2 risk=standard actions registered: channels:read, users:read
        names = sorted(t["function"]["name"] for t in registered)
        assert names == [
            "oc_slack_default_channels:read",
            "oc_slack_default_users:read",
        ]
        # 4 risk=sensitive/destructive actions pending writes enable:
        # channels:history, chat:write, chat:delete, im:history
        pending_names = sorted(p["id"] for p in pending)
        assert pending_names == [
            "channels:history", "chat:delete", "chat:write", "im:history",
        ]


# ── Registration round-trip ───────────────────────────────────────────


class TestRegistryPersistence:
    def test_registered_tools_persist_across_calls(self, tmp_path, monkeypatch):
        """Tools registered by discover_tools() are persisted in
        vault/tool_registry.json. A fresh registry read sees them."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_CATALOG_OPTIONS})
        from core.oauth import credential_store
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(
            "core.tools.registry._has_credential",
            lambda service, label: True,
        )

        registry.discover_tools("slack", "default")
        # Fresh registry load — see the registered tools
        tools = registry.list_tools()
        names = sorted(t["schema"]["function"]["name"] for t in tools)
        assert "oc_slack_default_channels:read" in names
        assert "oc_slack_default_users:read" in names
        # Pending writes are NOT in the active list
        assert "oc_slack_default_channels:history" not in names

    def test_enable_writes_promotes_pending_to_active(self, tmp_path, monkeypatch):
        """enable_writes() moves pending actions into the active list."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_CATALOG_OPTIONS})
        from core.oauth import credential_store
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(
            "core.tools.registry._has_credential",
            lambda service, label: True,
        )

        registry.discover_tools("slack", "default")
        # Before enable: chat:write is in pending, not in list_tools()
        names_before = sorted(t["schema"]["function"]["name"] for t in registry.list_tools())
        assert "oc_slack_default_chat:write" not in names_before

        # Enable writes for slack
        promoted = registry.enable_writes("slack")
        # 4 sensitive/destructive actions promoted (including
        # destructive chat:delete)
        assert promoted == 4

        # After enable: chat:write IS in list_tools()
        names_after = sorted(t["schema"]["function"]["name"] for t in registry.list_tools())
        assert "oc_slack_default_chat:write" in names_after
        assert "oc_slack_default_channels:history" in names_after

    def test_disable_removes_service_tools(self, tmp_path, monkeypatch):
        """disable() removes ALL tools (read + write) for the service/label."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_CATALOG_OPTIONS})
        from core.oauth import credential_store
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(
            "core.tools.registry._has_credential",
            lambda service, label: True,
        )

        registry.discover_tools("slack", "default")
        registry.enable_writes("slack")
        before = len(registry.list_tools())
        assert before > 0

        removed = registry.disable("slack")
        # 2 standard + 4 sensitive/destructive = 6 tools removed
        assert removed == 6
        assert registry.list_tools() == []

    def test_refresh_re_runs_discovery(self, tmp_path, monkeypatch):
        """refresh() probes OC again. New actions appear; removed actions
        from OC's catalog are dropped from the registry."""
        _isolated_registry(tmp_path, monkeypatch)
        # Initial catalog: 6 slack options, 4 github options
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_CATALOG_OPTIONS,
                                       "github": GITHUB_CATALOG_OPTIONS})
        from core.oauth import credential_store
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        credential_store.add("github", "default", "ghp-X", "api_key")
        monkeypatch.setattr(
            "core.tools.registry._has_credential",
            lambda service, label: True,
        )

        # First refresh — registers both
        first = registry.refresh()
        # 3 standard actions: slack channels:read + slack users:read
        # + github read:user
        assert first["added"] == 3

        # Now OC's catalog loses one action (e.g. slack channels:read
        # disappears in a new OC version)
        new_slack = [a for a in SLACK_CATALOG_OPTIONS
                     if a["id"] != "channels:read"]
        _stub_oc_catalog(monkeypatch, {"slack": new_slack,
                                       "github": GITHUB_CATALOG_OPTIONS})

        second = registry.refresh()
        # channels:read was registered, now OC doesn't have it → removed
        assert second["removed"] >= 1
        # No new actions for slack (same set minus one)
        names = sorted(t["schema"]["function"]["name"] for t in registry.list_tools())
        assert "oc_slack_default_channels:read" not in names
        assert "oc_slack_default_users:read" in names
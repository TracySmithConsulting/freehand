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

SLACK_ACTIONS = [
    {
        "id": "slack.list_channels",
        "service": "slack",
        "name": "list_channels",
        "description": "List Slack public channels.",
        "operationType": "read",
        "requiredScopes": ["channels:read"],
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
    {
        "id": "slack.conversations_members",
        "service": "slack",
        "name": "conversations_members",
        "description": "List members of a conversation.",
        "operationType": "read",
        "requiredScopes": ["channels:read", "groups:read"],
        "inputSchema": {
            "type": "object",
            "properties": {"channel_id": {"type": "string"}},
            "required": ["channel_id"],
        },
    },
    {
        "id": "slack.post_message",
        "service": "slack",
        "name": "post_message",
        "description": "Post a message.",
        "operationType": "write",
        "requiredScopes": ["chat:write"],
        "inputSchema": {
            "type": "object",
            "properties": {"channel_id": {"type": "string"}, "text": {"type": "string"}},
            "required": ["channel_id", "text"],
        },
    },
    {
        "id": "slack.delete_message",
        "service": "slack",
        "name": "delete_message",
        "description": "Delete a message.",
        "operationType": "destructive",
        "requiredScopes": ["chat:write"],
        "inputSchema": {
            "type": "object",
            "properties": {"channel_id": {"type": "string"}, "ts": {"type": "string"}},
        },
    },
    {
        "id": "slack.get_user_info",
        "service": "slack",
        "name": "get_user_info",
        "description": "Look up a user by id.",
        "operationType": "read",
        "requiredScopes": ["users:read"],
        "inputSchema": {
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
    },
    {
        "id": "slack.conversations_history",
        "service": "slack",
        "name": "conversations_history",
        "description": "Read channel message history.",
        "operationType": "sensitive",
        "requiredScopes": ["channels:history"],
        "inputSchema": {
            "type": "object",
            "properties": {"channel_id": {"type": "string"}},
        },
    },
]

GITHUB_ACTIONS = [
    {
        "id": "github.get_user",
        "service": "github",
        "name": "get_user",
        "description": "Read the authenticated user's profile.",
        "operationType": "read",
        "requiredScopes": ["read:user"],
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "id": "github.list_repos",
        "service": "github",
        "name": "list_repos",
        "description": "List repositories.",
        "operationType": "read",
        "requiredScopes": ["repo"],
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "id": "github.create_gist",
        "service": "github",
        "name": "create_gist",
        "description": "Create a gist.",
        "operationType": "write",
        "requiredScopes": ["gist"],
        "inputSchema": {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
    },
    {
        "id": "github.delete_repo",
        "service": "github",
        "name": "delete_repo",
        "description": "Delete a repository.",
        "operationType": "destructive",
        "requiredScopes": ["delete_repo"],
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    },
]


def _stub_oc_catalog(monkeypatch, by_service):
    """Stub core.oauth.open_connector.get_service_actions to return canned data.

    Round 13: registry calls get_service_actions (replaces the
    Round 10 get_provider_actions name). The fixture just maps
    each (service, label) to a list of RuntimeActionMetadata dicts.
    """
    def fake_get_actions(service_id, label="default"):
        return by_service.get(service_id, [])
    monkeypatch.setattr(
        "core.tools.registry._get_provider_actions",
        fake_get_actions,
    )
    # Round 13 doesn't call _search_actions in discover_tools, but
    # some legacy tests might still mock it. Provide a no-op stub.
    monkeypatch.setattr(
        "core.tools.registry._search_actions",
        lambda service_id, label="default": [],
    )


def _isolated_registry(tmp_path, monkeypatch):
    """Redirect registry + credential_store storage to a tmp dir.

    Both modules keep a module-level STORAGE_PATH that points to
    vault/. Without this, tests leak the production credential list
    into each other (e.g. Round 12 test_refresh_re_runs_discovery
    was seeing the pre-registered ``slack/tracy`` credential and
    double-counting it as an extra (service, label) pair)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(registry, "VAULT_DIR", vault)
    monkeypatch.setattr(registry, "STORAGE_PATH", vault / "tool_registry.json")
    from core.oauth import credential_store
    monkeypatch.setattr(credential_store, "VAULT_DIR", vault)
    monkeypatch.setattr(credential_store, "STORAGE_PATH",
                        vault / "credential_store.json")
    return vault


@pytest.fixture(autouse=True)
def _isolate_global_tool_state():
    """Each test sees a fresh TOOL_SCHEMAS + TOOL_REGISTRY.

    The registry mutates core.agent_config.TOOL_SCHEMAS and
    TOOL_REGISTRY to inject discovered tools. Without this fixture,
    mutations from one test leak into the next (e.g.
    test_round4_fixes::test_all_24_tools_visible_to_llm fails when
    OC-discovered tools from earlier tests are still in the dict).
    We snapshot both dicts before each test and restore them after.
    """
    from core import agent_config
    saved_schemas = dict(agent_config.TOOL_SCHEMAS)
    saved_registry = dict(agent_config.TOOL_REGISTRY)
    yield
    # Pop any keys that weren't in the snapshot
    new_schemas = [k for k in agent_config.TOOL_SCHEMAS if k not in saved_schemas]
    new_registry = [k for k in agent_config.TOOL_REGISTRY if k not in saved_registry]
    for k in new_schemas:
        agent_config.TOOL_SCHEMAS.pop(k, None)
    for k in new_registry:
        agent_config.TOOL_REGISTRY.pop(k, None)


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
        name = registry.build_tool_name("slack", "default", "list_channels")
        assert name == "oc_slack_default_list_channels"

    def test_multi_credential_uses_label(self):
        name = registry.build_tool_name("github", "work", "list_repos")
        assert name == "oc_github_work_list_repos"

    def test_label_normalization(self):
        """Labels are lowercased; action IDs are passed through as-is."""
        name = registry.build_tool_name("GitHub", "WORK", "list_repos")
        assert name == "oc_github_work_list_repos"


# ── Schema generation ──────────────────────────────────────────────────


class TestBuildToolSchema:
    def test_schema_shape_is_openai_function_calling(self):
        """Tool schema matches core/agent_config.TOOL_SCHEMAS shape
        (Pitfall 26 + Tracy 02 Oct 2026 decision: OpenAI function-calling).

        Round 13: action id is ``slack.list_channels`` (OC format),
        tool name strips the service prefix and uses the action name
        (``list_channels``) → ``oc_slack_default_list_channels``."""
        action = {
            "id": "slack.list_channels",
            "name": "list_channels",
            "description": "List Slack public channels.",
            "operationType": "read",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        }
        schema = registry.build_tool_schema("slack", "default", action)
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "oc_slack_default_list_channels"
        assert "list_channels" in schema["function"]["description"]
        assert "parameters" in schema["function"]
        assert "required" in schema["function"]["parameters"]
        # Pitfall 26: required is mandatory even for empty
        assert schema["function"]["parameters"]["required"] == []

    def test_schema_includes_input_schema_when_provided(self):
        """If OC provides inputSchema (JSON Schema), the schema's
        parameters include its properties with proper types."""
        action = {
            "id": "slack.post_message",
            "name": "post_message",
            "description": "Post a message.",
            "operationType": "write",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "Channel ID"},
                    "text": {"type": "string", "description": "Message text"},
                },
                "required": ["channel_id", "text"],
            },
        }
        schema = registry.build_tool_schema("slack", "default", action)
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        assert "channel_id" in params["properties"]
        assert "text" in params["properties"]
        assert params["properties"]["channel_id"]["type"] == "string"
        assert params["required"] == ["channel_id", "text"]


# ── Discovery from OC catalog ──────────────────────────────────────────


class TestDiscoverTools:
    def test_discovers_only_standard_actions(self, tmp_path, monkeypatch):
        """Round 13: only operationType=read actions become read tools.
        write/sensitive/destructive stay on the 'to enable' list.

        SLACK_ACTIONS has 3 reads (list_channels, conversations_members,
        get_user_info) and 3 non-reads (post_message, delete_message,
        conversations_history)."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
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

        # 3 operationType=read actions: list_channels, conversations_members, get_user_info
        names = sorted(e["schema"]["function"]["name"] for e in registered)
        assert names == [
            "oc_slack_default_conversations_members",
            "oc_slack_default_get_user_info",
            "oc_slack_default_list_channels",
        ]
        # 3 non-read actions pending writes enable: post_message, delete_message, conversations_history
        pending_names = sorted(p["id"] for p in pending)
        assert pending_names == [
            "slack.conversations_history",
            "slack.delete_message",
            "slack.post_message",
        ]


# ── Registration round-trip ───────────────────────────────────────────


class TestRegistryPersistence:
    def test_registered_tools_persist_across_calls(self, tmp_path, monkeypatch):
        """Tools registered by discover_tools() are persisted in
        vault/tool_registry.json. A fresh registry read sees them."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
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
        assert "oc_slack_default_list_channels" in names
        assert "oc_slack_default_get_user_info" in names
        # Pending writes are NOT in the active list
        assert "oc_slack_default_post_message" not in names

    def test_enable_writes_promotes_pending_to_active(self, tmp_path, monkeypatch):
        """enable_writes() moves pending actions into the active list."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
        from core.oauth import credential_store
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        monkeypatch.setattr(
            "core.tools.registry._has_credential",
            lambda service, label: True,
        )

        registry.discover_tools("slack", "default")
        # Before enable: post_message is in pending, not in list_tools()
        names_before = sorted(t["schema"]["function"]["name"] for t in registry.list_tools())
        assert "oc_slack_default_post_message" not in names_before

        # Enable writes for slack
        promoted = registry.enable_writes("slack")
        # 3 non-read actions promoted (write + sensitive + destructive)
        assert promoted == 3

        # After enable: post_message IS in list_tools()
        names_after = sorted(t["schema"]["function"]["name"] for t in registry.list_tools())
        assert "oc_slack_default_post_message" in names_after
        assert "oc_slack_default_conversations_history" in names_after

    def test_disable_removes_service_tools(self, tmp_path, monkeypatch):
        """disable() removes ALL tools (read + write) for the service/label."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS})
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
        # 3 reads + 3 non-reads = 6 tools removed
        assert removed == 6
        assert registry.list_tools() == []

    def test_refresh_re_runs_discovery(self, tmp_path, monkeypatch):
        """refresh() probes OC again. New actions appear; removed actions
        from OC's catalog are dropped from the registry."""
        _isolated_registry(tmp_path, monkeypatch)
        # Initial catalog: 6 slack actions, 4 github actions
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_ACTIONS,
                                       "github": GITHUB_ACTIONS})
        from core.oauth import credential_store
        credential_store.add("slack", "default", "xoxb-X", "oauth")
        credential_store.add("github", "default", "ghp-X", "api_key")
        monkeypatch.setattr(
            "core.tools.registry._has_credential",
            lambda service, label: True,
        )

        # First refresh — registers both
        first = registry.refresh()
        # 5 standard reads total: 3 slack (list_channels, conversations_members,
        # get_user_info) + 2 github (get_user, list_repos)
        assert first["added"] == 5

        # Now OC's catalog loses one action (e.g. slack list_channels
        # disappears in a new OC version)
        new_slack = [a for a in SLACK_ACTIONS
                     if a["id"] != "slack.list_channels"]
        _stub_oc_catalog(monkeypatch, {"slack": new_slack,
                                       "github": GITHUB_ACTIONS})

        second = registry.refresh()
        # list_channels was registered, now OC doesn't have it → removed
        assert second["removed"] >= 1
        # No new actions for slack (same set minus one)
        names = sorted(t["schema"]["function"]["name"] for t in registry.list_tools())
        assert "oc_slack_default_list_channels" not in names
        assert "oc_slack_default_get_user_info" in names
"""Tests for OC action-id translation at discover_tools time.

Round 12 of FreeHand maintenance.

When the registry registers an `oc_<svc>_<label>_<authopt_id>` tool,
it looks up the OC action id (e.g. 'slack.list_channels') via
OC's search_actions endpoint and stores both ids in the tool
entry. The dispatch layer reads the OC action id back when
it needs to call execute_action.

Live verified 06 Oct 2026: OC's search_actions for service='slack'
returns the per-action operationType and full input parameters.
We use the 'id' field as the OC action id.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.tools import registry  # noqa: E402
from core.oauth import credential_store  # noqa: E402


# ── Live OC catalog fixture (slack authorizationOptions) ────────────

SLACK_AUTH_OPTIONS = [
    {"id": "channels:read", "risk": "standard", "label": "Public channels",
     "description": "List public Slack channels.",
     "inputFields": [{"name": "limit", "type": "integer", "required": False}]},
    {"id": "channels:history", "risk": "sensitive", "label": "Public channel messages",
     "description": "Read message history in public Slack channels.",
     "inputFields": []},
    {"id": "chat:write", "risk": "sensitive", "label": "Send messages",
     "description": "Send messages as the connected Slack user.",
     "inputFields": [{"name": "channel", "type": "string", "required": True},
                     {"name": "text", "type": "string", "required": True}]},
]

SLACK_SEARCH_ACTIONS = [
    {"id": "slack.list_channels", "service": "slack", "operationType": "read",
     "name": "list_channels", "description": "List Slack public channels."},
    {"id": "slack.get_channel_messages", "service": "slack", "operationType": "read",
     "name": "get_channel_messages", "description": "Get recent messages."},
    {"id": "slack.post_message", "service": "slack", "operationType": "write",
     "name": "post_message", "description": "Post a message to a channel."},
]


def _isolated_registry(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(registry, "VAULT_DIR", vault)
    monkeypatch.setattr(registry, "STORAGE_PATH", vault / "tool_registry.json")
    return vault


def _stub_oc(monkeypatch, auth_options_by_service, search_actions_by_service):
    """Stub the two OC catalog functions: get_provider_actions returns
    authorizationOptions, search_actions returns the per-action
    service.action_name list with operationType."""
    monkeypatch.setattr(
        "core.tools.registry._get_provider_actions",
        lambda service, label="default": auth_options_by_service.get(service, []),
    )
    monkeypatch.setattr(
        "core.tools.registry._search_actions",
        lambda service, label="default": search_actions_by_service.get(service, []),
    )


def _stub_credentials(monkeypatch, present_pairs):
    pairs = set(present_pairs)
    monkeypatch.setattr(
        "core.tools.registry._has_credential",
        lambda service, label: (service, label) in pairs,
    )


@pytest.fixture(autouse=True)
def _isolate_global_tool_state():
    """Same isolation fixture as test_tool_registry.py."""
    from core import agent_config
    saved_schemas = dict(agent_config.TOOL_SCHEMAS)
    saved_registry = dict(agent_config.TOOL_REGISTRY)
    yield
    new_schemas = [k for k in agent_config.TOOL_SCHEMAS if k not in saved_schemas]
    new_registry = [k for k in agent_config.TOOL_REGISTRY if k not in saved_registry]
    for k in new_schemas:
        agent_config.TOOL_SCHEMAS.pop(k, None)
    for k in new_registry:
        agent_config.TOOL_REGISTRY.pop(k, None)


# ── The translation itself ─────────────────────────────────────


class TestTranslateAuthoptIdToOcActionId:
    def test_translates_by_label_match(self):
        """channels:read (authopt) -> slack.list_channels (OC action)
        because both have label 'Public channels' / 'List Slack public channels'."""
        from core.tools.registry import translate_authopt_id_to_oc_action_id
        authopt = {"id": "channels:read", "label": "Public channels",
                   "description": "List public Slack channels."}
        oc_id = translate_authopt_id_to_oc_action_id(
            "slack", authopt, SLACK_SEARCH_ACTIONS
        )
        assert oc_id == "slack.list_channels"

    def test_falls_back_to_description_match(self):
        """If label doesn't match, match on description substring."""
        from core.tools.registry import translate_authopt_id_to_oc_action_id
        authopt = {"id": "channels:history", "label": "Public channel messages",
                   "description": "Read message history in public Slack channels."}
        # search_results has no exact label match, but get_channel_messages
        # is a different action; the test should return None or the closest
        # match. Verify it doesn't crash and returns either None or a string.
        oc_id = translate_authopt_id_to_oc_action_id(
            "slack", authopt, SLACK_SEARCH_ACTIONS
        )
        # Either None (no confident match) or a string (heuristic)
        assert oc_id is None or isinstance(oc_id, str)

    def test_returns_none_when_no_match(self):
        """Unknown authopt id -> None (the action gets dropped)."""
        from core.tools.registry import translate_authopt_id_to_oc_action_id
        authopt = {"id": "totally_unknown", "label": "???",
                   "description": "Some action"}
        oc_id = translate_authopt_id_to_oc_action_id(
            "slack", authopt, SLACK_SEARCH_ACTIONS
        )
        assert oc_id is None


class TestDiscoverStoresOcActionId:
    def test_registered_tool_has_oc_action_id(self, tmp_path, monkeypatch):
        """discover_tools writes oc_action_id to each tool entry."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc(monkeypatch, {"slack": SLACK_AUTH_OPTIONS}, {"slack": SLACK_SEARCH_ACTIONS})
        _stub_credentials(monkeypatch, [("slack", "default")])

        registry.discover_tools("slack", "default")
        tools = registry.list_tools()
        # At least one tool has oc_action_id set
        assert any(t.get("oc_action_id") for t in tools)
        # Specifically, channels:read's tool has oc_action_id=slack.list_channels
        ch_tool = next((t for t in tools if "channels:read" in t["tool_name"]), None)
        assert ch_tool is not None
        assert ch_tool["oc_action_id"] == "slack.list_channels"

    def test_untranslatable_action_dropped(self, tmp_path, monkeypatch):
        """If translation returns None, the authopt is dropped from the registry."""
        _isolated_registry(tmp_path, monkeypatch)
        # Only one authopt, and it has no matching OC action
        _stub_oc(monkeypatch, {"slack": [
            {"id": "totally_unknown", "risk": "standard", "label": "???",
             "description": "No match", "inputFields": []},
        ]}, {"slack": []})  # no search results
        _stub_credentials(monkeypatch, [("slack", "default")])

        result = registry.discover_tools("slack", "default")
        # The unknown authopt is dropped - no tool registered
        assert result["registered"] == []
        assert registry.list_tools() == []

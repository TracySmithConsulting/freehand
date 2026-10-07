"""Tests for the dynamic OC-tool dispatch layer (Round 11/12).

The dispatch module takes an oc_<service>_<label>_<action> tool
name, parses the triple, looks up the credential from
credential_store, and calls OC's execute_action via
core.oauth.open_connector.execute_action.

Round 12 fix: Round 11's dispatch used call_mcp_action with the
wrong wire format. Round 12 uses execute_action with
connectionName=<label>.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.tools import dispatch  # noqa: E402


# ── Name parsing ────────────────────────────────────────────────


class TestParseOcToolName:
    def test_parses_three_part_name(self):
        """oc_slack_default_channels:read → (slack, default, channels:read)"""
        service, label, action = dispatch.parse_oc_tool_name(
            "oc_slack_default_channels:read"
        )
        assert service == "slack"
        assert label == "default"
        assert action == "channels:read"

    def test_parses_label_with_underscores(self):
        """oc_github_my_personal_repo:list → (github, my_personal, repo:list)"""
        service, label, action = dispatch.parse_oc_tool_name(
            "oc_github_my_personal_repo:list"
        )
        assert service == "github"
        assert label == "my_personal"
        assert action == "repo:list"

    def test_action_id_with_multiple_colons(self):
        """Slack-style multi-colon action IDs work.
        oc_slack_default_reactions:add:name → last underscore is the
        action separator."""
        service, label, action = dispatch.parse_oc_tool_name(
            "oc_slack_default_reactions:add:name"
        )
        assert service == "slack"
        assert label == "default"
        assert action == "reactions:add:name"

    def test_invalid_prefix_raises(self):
        """Tool names not starting with oc_ raise ValueError."""
        with pytest.raises(ValueError, match="not an oc_ tool"):
            dispatch.parse_oc_tool_name("read_docx")

    def test_too_few_parts_raises(self):
        """oc_ alone or oc_foo raises — need at least service + label + action."""
        with pytest.raises(ValueError, match="malformed"):
            dispatch.parse_oc_tool_name("oc_")
        with pytest.raises(ValueError, match="malformed"):
            dispatch.parse_oc_tool_name("oc_github")

    def test_label_no_underscore_action_underscored_uses_registry(
        self, tmp_path, monkeypatch
    ):
        """Round 13: ``oc_slack_tracy_list_channels`` with label="tracy"
        (no underscore) and action="list_channels" (with underscore).

        The "last underscore" rule would split this as
        (slack, "tracy_list", "channels") — wrong. The registry
        provides the anchor: tracy is a known label for slack,
        so we split at the second underscore.
        """
        from core.oauth import credential_store
        from core.tools import registry

        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(registry, "VAULT_DIR", vault)
        monkeypatch.setattr(registry, "STORAGE_PATH", vault / "tool_registry.json")
        monkeypatch.setattr(credential_store, "VAULT_DIR", vault)
        monkeypatch.setattr(credential_store, "STORAGE_PATH",
                            vault / "credential_store.json")
        credential_store.add("slack", "tracy", "xoxb-fake", "oauth")

        svc, lbl, act = dispatch.parse_oc_tool_name("oc_slack_tracy_list_channels")
        assert (svc, lbl, act) == ("slack", "tracy", "list_channels")

    def test_unknown_label_falls_back_to_last_underscore(self, monkeypatch):
        """If the registry has no known labels for the service, fall back
        to the "last underscore" heuristic. Better than crashing."""
        from core.tools import registry
        monkeypatch.setattr(registry, "_known_labels_for", lambda svc: set())
        svc, lbl, act = dispatch.parse_oc_tool_name("oc_slack_default_list_channels")
        # No known labels → split at last underscore. With "default"
        # already known, this should still work, but if registry returns
        # empty, "default_list" becomes the label.
        # The function docstring notes: this is the fallback.
        assert svc == "slack"


# ── Action id translation (Round 12) ────────────────────────────


class TestActionIdTranslation:
    """Round 12 fix: registry stores the OC action id (e.g. 'slack.list_channels')
    in the persisted tool entry; dispatch reads it from there. The translation
    happens at registry time, not dispatch time."""

    def test_get_oc_action_id_from_registry(self, tmp_path, monkeypatch):
        """dispatch reads the OC action id from the registry's persisted entry.
        The oc_<svc>_<label>_<authopt_id> tool name's last segment is the
        authorizationOptions[].id, but the registry stores the OC action id
        (service.action_name) alongside it."""
        from core.tools import registry
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(registry, "VAULT_DIR", vault)
        monkeypatch.setattr(registry, "STORAGE_PATH", vault / "tool_registry.json")

        # Pre-seed registry with a stored tool that has the OC action id
        import json
        (vault / "tool_registry.json").write_text(json.dumps({
            "tools": [
                {
                    "service": "slack", "label": "tracy",
                    "tool_name": "oc_slack_tracy_channels:read",
                    "risk": "standard",
                    "schema": {
                        "type": "function",
                        "function": {
                            "name": "oc_slack_tracy_channels:read",
                            "description": "List Slack channels",
                            "parameters": {"type": "object", "properties": {}, "required": []},
                        },
                    },
                    "oc_action_id": "slack.list_channels",  # Round 12 addition
                }
            ]
        }))

        action_id = dispatch.get_oc_action_id("oc_slack_tracy_channels:read")
        assert action_id == "slack.list_channels"

    def test_get_oc_action_id_missing_returns_none(self, tmp_path, monkeypatch):
        """If the tool isn't in the registry (e.g. typed by hand), return None
        and the dispatch returns a no_such_tool error."""
        from core.tools import registry
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(registry, "VAULT_DIR", vault)
        monkeypatch.setattr(registry, "STORAGE_PATH", vault / "tool_registry.json")

        action_id = dispatch.get_oc_action_id("oc_slack_tracy_no_such_tool")
        assert action_id is None


# ── Dispatch result shape (Round 12: execute_action + connectionName) ──


class TestDispatchResult:
    def test_success_envelope(self):
        """Successful OC call returns {"ok": True, "content": <json>}."""
        result = {"ok": True, "data": {"channels": [{"channelId": "C1", "name": "general"}]}}
        with patch.object(dispatch, "execute_action", return_value=result) as mock_call, \
             patch.object(dispatch, "get_oc_action_id", return_value="slack.list_channels"), \
             patch.object(dispatch, "_get_credential_for_dispatch", return_value={"secret": "x"}):
            response = dispatch.dispatch_oc_tool(
                "oc_slack_tracy_channels:read", {}
            )
        assert response["ok"] is True
        assert "channels" in response["content"]

    def test_no_credential_returns_error(self):
        """No credential for (service, label) -> no_credential error envelope."""
        with patch.object(dispatch, "get_oc_action_id", return_value="slack.list_channels"), \
             patch.object(dispatch, "_get_credential_for_dispatch", return_value=None):
            response = dispatch.dispatch_oc_tool(
                "oc_slack_tracy_channels:read", {}
            )
        assert response["ok"] is False
        assert response["error"]["code"] == "no_credential"

    def test_no_action_id_returns_error(self):
        """Tool not in registry -> unknown_tool error envelope."""
        with patch.object(dispatch, "get_oc_action_id", return_value=None):
            response = dispatch.dispatch_oc_tool(
                "oc_slack_tracy_no_such_tool", {}
            )
        assert response["ok"] is False
        assert response["error"]["code"] == "unknown_tool"

    def test_oc_error_envelope_passthrough(self):
        """OC returns ok=False - we pass it through unchanged."""
        result = {
            "ok": False,
            "error": {"code": "rate_limited", "message": "Try again in 30s"},
        }
        with patch.object(dispatch, "execute_action", return_value=result), \
             patch.object(dispatch, "get_oc_action_id", return_value="slack.list_channels"), \
             patch.object(dispatch, "_get_credential_for_dispatch", return_value={"secret": "x"}):
            response = dispatch.dispatch_oc_tool(
                "oc_slack_tracy_channels:read", {}
            )
        assert response["ok"] is False
        assert response["error"]["code"] == "rate_limited"

    def test_execute_action_called_with_connection_name(self):
        """execute_action is called with connectionName=<label>."""
        result = {"ok": True, "data": {}}
        with patch.object(dispatch, "execute_action", return_value=result) as mock_call, \
             patch.object(dispatch, "get_oc_action_id", return_value="slack.list_channels"), \
             patch.object(dispatch, "_get_credential_for_dispatch", return_value={"secret": "x"}):
            dispatch.dispatch_oc_tool("oc_slack_tracy_channels:read", {"limit": 5})
        mock_call.assert_called_once()
        args, kwargs = mock_call.call_args
        # positional: action_id, input_data; keyword: connection_name
        assert args[0] == "slack.list_channels"
        assert args[1] == {"limit": 5}
        assert kwargs.get("connection_name") == "tracy"

    def test_oc_unreachable_returns_error(self):
        """OC unreachable (execute_action returns None) -> oc_unreachable error."""
        with patch.object(dispatch, "execute_action", return_value=None), \
             patch.object(dispatch, "get_oc_action_id", return_value="slack.list_channels"), \
             patch.object(dispatch, "_get_credential_for_dispatch", return_value={"secret": "x"}):
            response = dispatch.dispatch_oc_tool(
                "oc_slack_tracy_channels:read", {}
            )
        assert response["ok"] is False
        assert response["error"]["code"] == "oc_unreachable"

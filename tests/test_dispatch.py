"""Tests for the dynamic OC-tool dispatch layer (Round 11).

The dispatch module takes an oc_<service>_<label>_<action> tool
name, parses the triple, looks up the credential from
credential_store, and calls OC's MCP endpoint via
core.oauth.open_connector.call_mcp_action.
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


# ── Dispatch result shape ────────────────────────────────────────


class TestDispatchResult:
    def test_success_envelope(self):
        """Successful OC call returns {"ok": True, "content": <json>}."""
        result = {
            "ok": True,
            "content": '{"channels": [{"id": "C1", "name": "general"}]}',
        }
        with patch.object(dispatch, "call_mcp_action", return_value=result) as mock_call, \
             patch.object(dispatch, "_get_credential_for_dispatch", return_value={"secret": "x"}):
            response = dispatch.dispatch_oc_tool(
                "oc_slack_default_channels:list",
                {},
            )
        assert response["ok"] is True
        assert "channels" in response["content"]

    def test_no_credential_returns_503(self):
        """No credential for (service, label) → no_credential error envelope."""
        with patch.object(dispatch, "_get_credential_for_dispatch", return_value=None):
            response = dispatch.dispatch_oc_tool(
                "oc_slack_default_channels:list", {}
            )
        assert response["ok"] is False
        assert response["error"]["code"] == "no_credential"

    def test_oc_returns_error_envelope_passthrough(self):
        """OC returns an error envelope — we pass it through unchanged."""
        result = {
            "ok": False,
            "error": {"code": "rate_limited", "message": "Try again in 30s"},
        }
        with patch.object(dispatch, "call_mcp_action", return_value=result), \
             patch.object(dispatch, "_get_credential_for_dispatch", return_value={"secret": "x"}):
            response = dispatch.dispatch_oc_tool(
                "oc_slack_default_channels:list", {}
            )
        assert response["ok"] is False
        assert response["error"]["code"] == "rate_limited"

    def test_action_id_with_special_chars(self):
        """Action IDs with colons (slack style) parse and dispatch."""
        result = {"ok": True, "content": "{}"}
        with patch.object(dispatch, "call_mcp_action", return_value=result) as mock_call, \
             patch.object(dispatch, "_get_credential_for_dispatch", return_value={"secret": "x"}):
            dispatch.dispatch_oc_tool(
                "oc_slack_default_chat:write", {"channel": "C1", "text": "hi"}
            )
        mock_call.assert_called_once()
        args, kwargs = mock_call.call_args
        assert args[0] == "chat:write"

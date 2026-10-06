"""Tests for the execute_action helper in open_connector.py.

Round 12 of FreeHand maintenance.

execute_action is OC's MCP tool for actually running a provider
action (the action's real API call). Round 11's dispatch used
call_mcp_action with the wrong wire format; Round 12 adds
execute_action as the right helper.

Live verified 06 Oct 2026: execute_action(actionId='slack.list_channels',
input={'limit': 5}, connectionName='tracy') returned 3 real
Slack channels from Tracy's workspace.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.oauth import open_connector  # noqa: E402


def _stub_runtime_token(monkeypatch, token="runttest123"):
    """Stub the runtime token that open_connector.py loads at import time."""
    monkeypatch.setenv("OOMOL_CONNECT_RUNTIME_TOKEN", token)
    open_connector._reset_cache_for_tests()


def _stub_admin_token(monkeypatch, token="admintest123"):
    monkeypatch.setenv("OOMOL_CONNECT_ADMIN_TOKEN", token)


def _make_sse_response(data_dict):
    """Wrap a dict in OC's SSE response shape."""
    return f"event: message\ndata: {json.dumps(data_dict)}\n".encode("utf-8")


def test_execute_action_happy_path(monkeypatch):
    """execute_action returns the parsed result envelope on success."""
    _stub_runtime_token(monkeypatch)
    _stub_admin_token(monkeypatch)
    inner = {"ok": True, "data": {"channels": [{"channelId": "C1", "name": "general"}]}}
    response = {"result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
                "jsonrpc": "2.0", "id": 1}
    fake_body = _make_sse_response(response)

    with patch.object(open_connector.urllib.request, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = fake_body
        mock_urlopen.return_value.__enter__.return_value.status = 200
        result = open_connector.execute_action(
            "slack.list_channels", {"limit": 5}, connection_name="tracy"
        )
    assert result["ok"] is True
    assert "channels" in result["data"]
    assert result["data"]["channels"][0]["name"] == "general"


def test_execute_action_error_envelope(monkeypatch):
    """OC returns ok=False with error code on missing connection."""
    _stub_runtime_token(monkeypatch)
    _stub_admin_token(monkeypatch)
    inner = {"ok": False, "error": {"code": "no_connection",
                                     "message": "no slack connection named 'tracy'"}}
    response = {"result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
                "jsonrpc": "2.0", "id": 1}
    fake_body = _make_sse_response(response)

    with patch.object(open_connector.urllib.request, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = fake_body
        mock_urlopen.return_value.__enter__.return_value.status = 200
        result = open_connector.execute_action("slack.list_channels", {})
    assert result["ok"] is False
    assert result["error"]["code"] == "no_connection"


def test_execute_action_no_token(monkeypatch):
    """No runtime token -> return None (matching call_mcp_action's contract)."""
    monkeypatch.delenv("OOMOL_CONNECT_RUNTIME_TOKEN", raising=False)
    open_connector._reset_cache_for_tests()
    result = open_connector.execute_action("slack.list_channels", {})
    assert result is None


def test_execute_action_oc_unreachable(monkeypatch):
    """OC down -> return None (not exception)."""
    _stub_runtime_token(monkeypatch)
    _stub_admin_token(monkeypatch)
    with patch.object(open_connector.urllib.request, "urlopen",
                      side_effect=ConnectionError("OC down")):
        result = open_connector.execute_action("slack.list_channels", {})
    assert result is None


def test_execute_action_sends_correct_wire(monkeypatch):
    """Verify the JSON-RPC payload matches OC's expected shape.

    Wire format (confirmed live 06 Oct 2026):
      params.name = 'execute_action'
      params.arguments = {actionId, input, connectionName (optional)}
    """
    _stub_runtime_token(monkeypatch)
    _stub_admin_token(monkeypatch)
    captured = {}

    def fake_urlopen(req, **kwargs):
        captured["body"] = json.loads(req.data.decode("utf-8"))

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            status = 200

            def read(self):
                return _make_sse_response({"result": {"content": [
                    {"type": "text", "text": '{"ok":true,"data":{}}'}
                ]}})

        return FakeResp()

    monkeypatch.setattr(open_connector.urllib.request, "urlopen", fake_urlopen)
    open_connector.execute_action(
        "slack.list_channels", {"limit": 5}, connection_name="tracy"
    )
    body = captured["body"]
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "execute_action"
    args = body["params"]["arguments"]
    assert args["actionId"] == "slack.list_channels"
    assert args["input"] == {"limit": 5}
    assert args["connectionName"] == "tracy"


def test_execute_action_omits_connection_name_when_none(monkeypatch):
    """If connectionName is None, the field is omitted (OC uses default)."""
    _stub_runtime_token(monkeypatch)
    _stub_admin_token(monkeypatch)
    captured = {}

    def fake_urlopen(req, **kwargs):
        captured["body"] = json.loads(req.data.decode("utf-8"))

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            status = 200

            def read(self):
                return _make_sse_response({"result": {"content": [
                    {"type": "text", "text": '{"ok":true,"data":{}}'}
                ]}})

        return FakeResp()

    monkeypatch.setattr(open_connector.urllib.request, "urlopen", fake_urlopen)
    open_connector.execute_action("hackernews.get_item", {"id": 1})
    args = captured["body"]["params"]["arguments"]
    assert "connectionName" not in args
    assert args["actionId"] == "hackernews.get_item"
    assert args["input"] == {"id": 1}

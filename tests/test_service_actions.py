"""Tests for get_service_actions — Round 13 Task 1.

OC's runtime action catalog endpoint is /v1/actions?service=<svc>.
It returns RuntimeActionMetadata rows (one per action) with id,
name, operationType (read/write/destructive), requiredScopes,
and inputSchema. This module owns the live HTTP call + caching.
"""
from __future__ import annotations

import json
import urllib.error
from typing import Any
from unittest import mock

import pytest


# Use a fake httplib response so we don't need a running server.
class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts with cold cache + cold runtime token."""
    from core.oauth import open_connector
    open_connector._CACHE.clear()
    open_connector._runtime_token = None
    yield
    open_connector._CACHE.clear()
    open_connector._runtime_token = None


def _patch_urlopen(payload: Any, status: int = 200):
    return mock.patch(
        "urllib.request.urlopen",
        return_value=_FakeResponse(payload, status=status),
    )


# ── Sample action catalog (matches live OC's shape from /v1/actions?service=slack)

SLACK_ACTIONS_PAYLOAD = {
    "success": True,
    "data": [
        {
            "id": "slack.list_channels",
            "service": "slack",
            "name": "list_channels",
            "description": "List Slack public channels visible to the connected Slack identity.",
            "operationType": "read",
            "requiredScopes": ["channels:read"],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100}
                },
            },
        },
        {
            "id": "slack.post_message",
            "service": "slack",
            "name": "post_message",
            "description": "Post a message to a Slack channel.",
            "operationType": "write",
            "requiredScopes": ["chat:write"],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["channel_id", "text"],
            },
        },
        {
            "id": "slack.delete_message",
            "service": "slack",
            "name": "delete_message",
            "description": "Delete a message posted by the app.",
            "operationType": "destructive",
            "requiredScopes": ["chat:write"],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string"},
                    "ts": {"type": "string"},
                },
            },
        },
    ],
}


# ── Tests ───────────────────────────────────────────────────────────────

def test_returns_action_list_when_endpoint_responds(monkeypatch):
    """get_service_actions returns RuntimeActionMetadata dicts."""
    from core.oauth import open_connector

    monkeypatch.setenv("OOMOL_CONNECT_RUNTIME_TOKEN", "test-rt-token")
    with _patch_urlopen(SLACK_ACTIONS_PAYLOAD):
        result = open_connector.get_service_actions("slack")

    assert len(result) == 3
    assert result[0]["id"] == "slack.list_channels"
    assert result[0]["operationType"] == "read"
    assert result[1]["requiredScopes"] == ["chat:write"]


def test_returns_empty_when_service_unknown(monkeypatch):
    """404 (service not in catalog) returns []. Don't raise."""
    from core.oauth import open_connector

    monkeypatch.setenv("OOMOL_CONNECT_RUNTIME_TOKEN", "test-rt-token")
    error = urllib.error.HTTPError(
        "http://127.0.0.1:3001/v1/actions?service=nosuch",
        404,
        "Not Found",
        {},
        b"not found",
    )
    with mock.patch("urllib.request.urlopen", side_effect=error):
        result = open_connector.get_service_actions("nosuch")

    assert result == []


def test_returns_empty_when_runtime_token_missing(monkeypatch):
    """Without a runtime token, /v1/* returns 401. We treat as empty."""
    from core.oauth import open_connector

    monkeypatch.delenv("OOMOL_CONNECT_RUNTIME_TOKEN", raising=False)
    # Force _load_runtime_token to return None, simulating a config
    # with no token (broker_config.json doesn't have one either
    # since the autouse fixture isolates state).
    monkeypatch.setattr(open_connector, "_load_runtime_token", lambda: None)
    result = open_connector.get_service_actions("slack")
    assert result == []


def test_returns_empty_on_connection_error(monkeypatch):
    """Network down → []. Don't crash the registry."""
    from core.oauth import open_connector

    monkeypatch.setenv("OOMOL_CONNECT_RUNTIME_TOKEN", "test-rt-token")
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("refused"),
    ):
        result = open_connector.get_service_actions("slack")

    assert result == []


def test_results_are_cached(monkeypatch):
    """Second call for the same service uses the cache (1 HTTP call)."""
    from core.oauth import open_connector

    monkeypatch.setenv("OOMOL_CONNECT_RUNTIME_TOKEN", "test-rt-token")
    with _patch_urlopen(SLACK_ACTIONS_PAYLOAD) as m:
        first = open_connector.get_service_actions("slack")
        second = open_connector.get_service_actions("slack")
        assert first is second  # cached
        assert m.call_count == 1


def test_includes_runtime_token_in_authorization_header(monkeypatch):
    """The runtime token (per-user, tier-1b) gates /v1/* — must be sent."""
    from core.oauth import open_connector

    monkeypatch.setenv("OOMOL_CONNECT_RUNTIME_TOKEN", "secret-rt-token")
    captured_headers = {}

    def fake_urlopen(req, **kwargs):
        captured_headers.update(req.headers)
        return _FakeResponse(SLACK_ACTIONS_PAYLOAD)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        open_connector.get_service_actions("slack")

    assert captured_headers.get("Authorization") == "Bearer secret-rt-token"


def test_includes_query_string_with_service(monkeypatch):
    """The endpoint expects the service id as ?service=<name>."""
    from core.oauth import open_connector

    monkeypatch.setenv("OOMOL_CONNECT_RUNTIME_TOKEN", "x")
    captured_url = []

    def fake_urlopen(req, **kwargs):
        captured_url.append(req.full_url)
        return _FakeResponse(SLACK_ACTIONS_PAYLOAD)

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        open_connector.get_service_actions("google_drive")

    assert any("service=google_drive" in u for u in captured_url)
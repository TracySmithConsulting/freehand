"""Tests for the get_provider_actions catalog probe in open_connector.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.oauth import open_connector  # noqa: E402


def _stub_admin_token(monkeypatch, token="admintest123"):
    """FreeHand's open_connector module loads the admin token from
    vault/broker_config.json or env. Stub the env path so tests don't
    need a real broker config."""
    monkeypatch.setenv("OOMOL_CONNECT_ADMIN_TOKEN", token)


def test_get_provider_actions_returns_options(monkeypatch):
    """get_provider_actions(service, label) returns the
    authorizationOptions list from OC's /v1/providers/<service>."""
    _stub_admin_token(monkeypatch)
    fake_response = json.dumps({
        "service": "slack",
        "auth": {
            "authorizationOptions": [
                {"id": "channels:read", "risk": "standard",
                 "defaultSelected": True, "required": True,
                 "label": "Public channels",
                 "description": "List public Slack channels."},
                {"id": "chat:write", "risk": "sensitive",
                 "defaultSelected": True, "required": False,
                 "label": "Send messages",
                 "description": "Send messages as the connected Slack user."},
            ]
        }
    }).encode("utf-8")

    with patch.object(open_connector.urllib.request, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = fake_response
        mock_urlopen.return_value.__enter__.return_value.status = 200
        actions = open_connector.get_provider_actions("slack", "default")
    assert len(actions) == 2
    assert actions[0]["id"] == "channels:read"
    assert actions[1]["risk"] == "sensitive"


def test_get_provider_actions_returns_empty_on_404(monkeypatch):
    """OC returns 404 for an unknown service — get_provider_actions
    must return an empty list, not raise."""
    _stub_admin_token(monkeypatch)
    import urllib.error
    with patch.object(open_connector.urllib.request, "urlopen",
                      side_effect=urllib.error.HTTPError(
                          "http://127.0.0.1:3001/v1/providers/nonexistent",
                          404, "Not Found", {}, None)):
        actions = open_connector.get_provider_actions("nonexistent")
    assert actions == []


def test_get_provider_actions_handles_no_auth(monkeypatch):
    """A provider with no auth.authorizationOptions returns an empty list."""
    _stub_admin_token(monkeypatch)
    fake_response = json.dumps({"service": "hackernews"}).encode("utf-8")
    with patch.object(open_connector.urllib.request, "urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = fake_response
        mock_urlopen.return_value.__enter__.return_value.status = 200
        actions = open_connector.get_provider_actions("hackernews")
    assert actions == []


def test_get_provider_actions_returns_empty_when_oc_down(monkeypatch):
    """OC unreachable — return empty list (not raise). The registry
    treats this as 'no new tools to register'."""
    _stub_admin_token(monkeypatch)
    with patch.object(open_connector.urllib.request, "urlopen",
                      side_effect=ConnectionError("OC down")):
        actions = open_connector.get_provider_actions("slack")
    assert actions == []

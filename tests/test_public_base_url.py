"""Item 9 (Nov 2026): one public base URL for callbacks and approval links.

Before: OAuth read env or settings.oauth.public_base_url; approval links
read only settings.public_base_url; tenant boxes set neither, and the
fallback built "https://host:None/..." on a default port.
"""
import sys
from pathlib import Path

import pytest
from starlette.requests import Request

from core import agent_config, security
from core.agent_config import resolve_public_base_url
import importlib

router = importlib.import_module("core.oauth.router")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import provision_tenant as pt  # noqa: E402


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("FREEHAND_PUBLIC_BASE_URL", raising=False)


def _request(scheme="https", host="acme.example.com", port=None):
    server = (host, port if port is not None else (443 if scheme == "https" else 80))
    headers = [(b"host", (host if port is None else f"{host}:{port}").encode())]
    return Request({
        "type": "http", "method": "GET", "scheme": scheme, "path": "/",
        "query_string": b"", "headers": headers, "server": server,
    })


class TestResolveOrder:
    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("FREEHAND_PUBLIC_BASE_URL", "https://env.example.com/")
        s = {"public_base_url": "https://top.example.com",
             "oauth": {"public_base_url": "https://oauth.example.com"}}
        assert resolve_public_base_url(s) == "https://env.example.com"

    def test_top_level_then_legacy_oauth_key(self):
        assert resolve_public_base_url({
            "public_base_url": "https://top.example.com",
            "oauth": {"public_base_url": "https://oauth.example.com"},
        }) == "https://top.example.com"
        assert resolve_public_base_url(
            {"oauth": {"public_base_url": "https://oauth.example.com/"}}
        ) == "https://oauth.example.com"

    def test_blank_is_unset(self):
        assert resolve_public_base_url({"public_base_url": "   "}) == ""
        assert resolve_public_base_url({}) == ""


class TestCallers:
    def test_oauth_callback_uses_env(self, monkeypatch):
        monkeypatch.setenv("FREEHAND_PUBLIC_BASE_URL", "https://acme.example.com")
        uri = router._get_redirect_uri("google", _request("http", "freehand-app", 8000))
        assert uri == "https://acme.example.com/api/integrations/google/callback"

    def test_oauth_callback_uses_top_level_setting(self):
        agent_config.SETTINGS_PATH.write_text('{"public_base_url": "https://acme.example.com"}')
        uri = router._get_redirect_uri("microsoft", _request("http", "freehand-app", 8000))
        assert uri == "https://acme.example.com/api/integrations/microsoft/callback"

    def test_fallback_on_default_port_has_no_none(self):
        uri = router._get_redirect_uri("google", _request("https", "acme.example.com"))
        assert ":None" not in uri
        assert uri == "https://acme.example.com/api/integrations/google/callback"

    def test_fallback_keeps_explicit_port(self):
        uri = router._get_redirect_uri("google", _request("http", "localhost", 8000))
        assert uri == "http://localhost:8000/api/integrations/google/callback"

    def test_composio_callback_uses_shared_resolver(self):
        agent_config.SETTINGS_PATH.write_text('{"public_base_url": "https://acme.example.com"}')
        uri = router._get_composio_callback_uri("facebook", "default", _request("http", "x", 8000))
        assert uri.startswith("https://acme.example.com/api/integrations/facebook/composio/callback")

    def test_approval_links_honour_env(self, monkeypatch):
        monkeypatch.setenv("FREEHAND_PUBLIC_BASE_URL", "https://acme.example.com")
        assert security._get_approval_base_url() == "https://acme.example.com"


class TestTenantProvisioning:
    def test_env_file_sets_public_url(self):
        env = pt.build_env("acme-corp", 8101, "https://acme.example.com", "https://acme.example.com")
        assert "FREEHAND_PUBLIC_BASE_URL=https://acme.example.com\n" in env

    def test_compose_passes_public_url_to_freehand(self, tmp_path):
        compose = pt.build_compose("acme-corp", 8100, 8101, tmp_path)
        assert "- FREEHAND_PUBLIC_BASE_URL=${FREEHAND_PUBLIC_BASE_URL:-}" in compose

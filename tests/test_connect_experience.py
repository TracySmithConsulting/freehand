"""Round 15.6 — connect experience: browser gets consent screen, API keeps JSON.

The Connect button does window.open('/api/integrations/<svc>/authorize').
For OC-known services that endpoint already 302s into OC's consent screen
(Pitfall 24), but for static connectors it returns 200 application/json —
so the popup landed on the browser's JSON "Pretty-print" view instead of
the provider's consent page. Same button, two UXes.

This suite pins the unified rule:
- Accept: text/html (browsers) -> 302 to the consent screen, ALWAYS
  (static services, OC services, even the 503 fallback which becomes a
  small HTML page carrying the 'request this service' link)
- no/other Accept (API clients: curl, MCP, docs examples) -> the JSON
  contract is UNCHANGED: 200 {"authorize_url", ...} / 503 structured body.

It also pins the new GET /api/integrations field:
- 'oc_services': OpenConnector catalogue services NOT already in the
  static allowlist, deduped + lowercase. Empty list when OC is
  unreachable; the endpoint never crashes on an OC outage.

Tests load the router module the proven way (register in sys.modules so
the `from .router import router` shadow doesn't bite — see test_tier5)
and swap the OAuth-settings file for a tmp dict so /authorize state
writes never touch the real vault. No real OC runtime or OOMOL token
is required: the OC catalogue is patched at a router-level helper.
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from core.oauth import broker as broker_mod  # noqa: E402
from core.oauth import open_connector as oc_mod  # noqa: E402

# Load the router module by file path and register it as "core.oauth.router"
# so the package's `from .router import router` shadow is bypassed and
# patching router-level names takes effect on the very module FastAPI serves.
import importlib.util as _ilu  # noqa: E402
import pathlib as _pl  # noqa: E402

_R = str(_pl.Path(__file__).resolve().parent.parent / "core" / "oauth" / "router.py")
_spec = _ilu.spec_from_file_location("core.oauth.router", _R)
router_mod = _ilu.module_from_spec(_spec)
sys.modules["core.oauth.router"] = router_mod
_spec.loader.exec_module(router_mod)

router = router_mod.router
ALLOWED_SERVICES = router_mod.ALLOWED_SERVICES


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    _state = {"oauth": {}}

    def _load():
        return _state

    def _save(data):
        _state.clear()
        _state.update(data)

    with (
        patch.object(router_mod, "_load_oauth_settings", _load),
        patch.object(router_mod, "_save_oauth_settings", _save),
    ):
        yield TestClient(app)


class TestAuthorizeContentNegotiation:
    def test_browser_html_gets_302_to_consent(self, client):
        """The Connect-button case: a popup sends Accept: text/html for a
        STATIC service (facebook) and must land on the provider consent page,
        NOT a JSON body."""
        r = client.get(
            "/api/integrations/facebook/authorize?label=work",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        assert r.status_code == 302, f"got {r.status_code}: {r.text[:200]}"
        loc = r.headers.get("location", "")
        assert loc.startswith("http"), f"consent redirect not a URL: {loc}"

    def test_api_client_still_gets_json(self, client):
        """Regression guard: no Accept-html (curl, MCP, docs examples)
        keeps the existing JSON contract byte-for-byte."""
        r = client.get("/api/integrations/facebook/authorize?label=work")
        assert r.status_code == 200
        body = r.json()
        assert "authorize_url" in body
        assert body["label"] == "work"

    def test_oc_service_browser_goes_to_oc(self, client, monkeypatch):
        """When OC knows the service, a browser Accept 302s into OC's *web
        console provider page* (/providers/<svc>) — the clickable console,
        NOT the API root (which serves a JSON banner without a built console)."""
        monkeypatch.setenv("OOMOL_CONNECT_BASE_URL", "http://127.0.0.1:3001")
        monkeypatch.delenv("OOMOL_CONNECT_CONSOLE_URL", raising=False)
        with (
            patch.object(broker_mod, "is_known_to_open_connector", lambda svc: True),
            patch.object(broker_mod, "get_client_credentials", lambda svc: (None, None, "none")),
        ):
            r = client.get(
                "/api/integrations/linear/authorize",
                headers={"Accept": "text/html"},
                follow_redirects=False,
            )
        assert r.status_code == 302, r.text[:200]
        loc = r.headers.get("location", "")
        assert loc == "http://127.0.0.1:3001/providers/linear?connectionName=default", loc
        assert "authorize=linear" not in loc, loc

    def test_oc_service_console_url_override(self, client, monkeypatch):
        """In dev the console is a separate app: OOMOL_CONNECT_CONSOLE_URL
        redirects into it instead of the API base (which has no built console)."""
        monkeypatch.setenv("OOMOL_CONNECT_BASE_URL", "http://127.0.0.1:3001")
        monkeypatch.setenv("OOMOL_CONNECT_CONSOLE_URL", "http://127.0.0.1:5173")
        with (
            patch.object(broker_mod, "is_known_to_open_connector", lambda svc: True),
            patch.object(broker_mod, "get_client_credentials", lambda svc: (None, None, "none")),
        ):
            r = client.get(
                "/api/integrations/github/authorize",
                headers={"Accept": "text/html"},
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert r.headers.get("location", "") == "http://127.0.0.1:5173/providers/github?connectionName=default"

    def test_oc_service_api_client_unchanged_302(self, client):
        """OC services were ALWAYS 302 (even for API clients) — keep that;
        only the static path gained content negotiation."""
        with (
            patch.object(broker_mod, "is_known_to_open_connector", lambda svc: True),
            patch.object(broker_mod, "get_client_credentials", lambda svc: (None, None, "none")),
            patch.object(oc_mod, "start_oauth_connection", lambda svc, name: (None, "no_admin_token")),
        ):
            r = client.get("/api/integrations/linear/authorize", follow_redirects=False)
        assert r.status_code == 302


class TestOpenConnectorDirectSignIn:
    """Nov 2026: Connect starts OC sign-ins itself, so people go straight to
    the provider instead of OC's admin-token page."""

    def _patches(self, start):
        return (
            patch.object(broker_mod, "is_known_to_open_connector", lambda svc: True),
            patch.object(broker_mod, "get_client_credentials", lambda svc: (None, None, "none")),
            patch.object(oc_mod, "start_oauth_connection", start),
        )

    def test_browser_goes_straight_to_the_provider(self, client):
        calls = []

        def start(svc, name):
            calls.append((svc, name))
            return "https://accounts.google.com/o/oauth2/v2/auth?state=s", None

        a, b, c = self._patches(start)
        with a, b, c:
            r = client.get("/api/integrations/gmail/authorize?label=Personal",
                           headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"].startswith("https://accounts.google.com/")
        assert calls == [("gmail", "Personal")]

    def test_account_name_with_spaces_is_refused_kindly(self, client):
        a, b, c = self._patches(lambda svc, name: ("https://x", None))
        with a, b, c:
            r = client.get("/api/integrations/gmail/authorize?label=My%20work",
                           headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 400
        assert "no spaces" in r.text

    def test_api_key_apps_still_use_the_open_connector_page(self, client, monkeypatch):
        monkeypatch.setenv("OOMOL_CONNECT_CONSOLE_URL", "http://localhost:3001")
        a, b, c = self._patches(lambda svc, name: (None, "unsupported_auth_type"))
        with a, b, c:
            r = client.get("/api/integrations/github/authorize",
                           headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "http://localhost:3001/providers/github?connectionName=default"

    def test_open_connector_down_shows_a_page_not_json(self, client):
        a, b, c = self._patches(lambda svc, name: (None, "unreachable"))
        with a, b, c:
            r = client.get("/api/integrations/gmail/authorize",
                           headers={"Accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 502
        assert r.text.startswith("<!DOCTYPE html>")
        assert "Check that OpenConnector is running" in r.text

    def test_fallback_503_browser_gets_html(self, client):
        """Even the 'request this service' fallback must not dump JSON in a
        popup: a browser Accept gets an HTML page carrying the same
        request_url; an API client keeps the structured JSON body."""
        with (
            patch.object(broker_mod, "_load_shared_apps", lambda: {}),
            patch.object(broker_mod, "is_known_to_open_connector", lambda svc: False),
        ):
            rb = client.get(
                "/api/integrations/linear/authorize",
                headers={"Accept": "text/html"},
            )
            assert rb.status_code == 503, rb.text[:200]
            assert "text/html" in rb.headers.get("content-type", "")

            ra = client.get("/api/integrations/linear/authorize")
            assert ra.status_code == 503
            body = ra.json()
            assert body["error"] == "no_shared_app"


class TestListIntegrationsOcServices:
    def test_oc_services_lists_only_oc_catalogue(self, client):
        """oc_services = (OC catalogue) − (static allowlist): the static
        ones already appear in available_services. Deduped, lowercase."""
        with patch.object(router_mod, "_oc_catalogue", lambda: ["github", "hackernews", "slack"]):
            body = client.get("/api/integrations").json()
        assert body["oc_services"] == ["github", "hackernews"], body.get("oc_services")

    def test_oc_services_empty_when_oc_down(self, client):
        """No OC runtime -> empty list; the endpoint must NOT fail."""
        with patch.object(router_mod, "_oc_catalogue", lambda: []):
            body = client.get("/api/integrations").json()
        assert body.get("oc_services") == []

    def test_available_services_unchanged(self, client):
        """available_services is the static allowlist plus the featured
        OpenConnector apps (Gmail, Calendar, Sheets, Outlook) — never the
        rest of the OC catalogue, and no featured app twice."""
        with patch.object(router_mod, "_oc_catalogue", lambda: ["github", "gmail"]):
            body = client.get("/api/integrations").json()
        assert set(body["available_services"]) == set(ALLOWED_SERVICES) | set(router_mod.OC_FEATURED_SERVICES)
        assert body["oc_services"] == ["github"]


class TestOpenConnectorAccountsInPanel:
    """Nov 2026: accounts connected in OpenConnector show as connected in
    Connections, and Test/Disconnect work on them."""

    ACCOUNTS = [{"service": "gmail", "label": "Personal", "display": "me@gmail.com",
                 "scopes": ["gmail.modify"], "id": "abc"}]

    def test_list_includes_open_connector_accounts(self, client):
        with (
            patch.object(oc_mod, "list_accounts", lambda: self.ACCOUNTS),
            patch.object(router_mod, "list_connections", lambda: []),
        ):
            body = client.get("/api/integrations").json()
        gmail = [g for g in body["connections"] if g["service"] == "gmail"]
        assert gmail, body["connections"]
        acc = gmail[0]["connections"][0]
        assert acc["label"] == "Personal" and acc["email"] == "me@gmail.com"
        assert acc["status"] == "active" and acc["backend"] == "open_connector"

    def test_test_and_disconnect_use_the_named_account(self, client):
        removed = []
        with (
            patch.object(oc_mod, "list_accounts", lambda: self.ACCOUNTS),
            patch.object(oc_mod, "disconnect_account", lambda s, n: removed.append((s, n)) or True),
        ):
            assert client.post("/api/integrations/gmail/test?label=Personal").json()["valid"] is True
            assert client.post("/api/integrations/gmail/test?label=Work").status_code == 404
            r = client.post("/api/integrations/gmail/disconnect?label=Personal")
        assert r.status_code == 200
        assert removed == [("gmail", "Personal")]

    def test_open_connector_down_keeps_the_panel_working(self, client):
        with (
            patch.object(oc_mod, "list_accounts", lambda: []),
            patch.object(router_mod, "list_connections", lambda: []),
        ):
            r = client.get("/api/integrations")
        assert r.status_code == 200

class TestSignInAsksWhichAccount:
    """Adding a second Outlook/Gmail account used to reconnect whoever was
    already signed in to the browser."""

    def test_microsoft_shows_its_account_chooser(self):
        from urllib.parse import parse_qs, urlsplit
        from core.oauth.open_connector import ask_which_account
        url = ask_which_account("https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=x&state=s")
        q = parse_qs(urlsplit(url).query)
        assert q["prompt"] == ["select_account"] and q["state"] == ["s"] and q["client_id"] == ["x"]

    def test_google_keeps_consent_and_adds_the_chooser(self):
        from urllib.parse import parse_qs, urlsplit
        from core.oauth.open_connector import ask_which_account
        url = ask_which_account("https://accounts.google.com/o/oauth2/v2/auth?prompt=consent&state=s")
        assert parse_qs(urlsplit(url).query)["prompt"] == ["consent select_account"]

    def test_an_existing_microsoft_prompt_and_other_sites_are_left_alone(self):
        from core.oauth.open_connector import ask_which_account
        ms = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?prompt=consent"
        zoom = "https://zoom.us/oauth/authorize?state=s"
        assert ask_which_account(ms) == ms and ask_which_account(zoom) == zoom

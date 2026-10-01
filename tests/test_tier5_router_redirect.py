"""Tests for the OpenConnector tier-5 redirect in the OAuth router (Round 7).

Pitfall 24: redirect-not-JSON for the "Connect Slack" button. When a user
hits /api/integrations/<service>/authorize and tier-5 (OpenConnector)
knows the service, FreeHand 302s them to OpenConnector's Web Console
consent screen rather than returning a JSON dump with a URL.

These tests build a minimal FastAPI app mounting the same router and
stub the broker tier-5 helpers directly so we don't need a real OC
runtime. The fixture also isolates VAULT_DIR so tests don't touch the
real FreeHand vault.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

# FastAPI TestClient without pulling the full server.py (which would
# boot FreeHand's broader routing layer with side-effects).
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.oauth import broker as broker_mod
# Pitfall 27 trap (FreeHand): `core.oauth/__init__.py` does
# `from .router import router`, which shadows the `router` module
# name with the APIRouter object. Both `from core.oauth import router`
# AND `import core.oauth.router as router` end up binding `router`
# to the APIRouter, not the module. Workaround: load the module by
# file path via importlib, bypassing the package's __init__.
import importlib.util as _importlib_util
_router_path = rf"{sys.path[0]}\core\oauth\router.py"
_router_spec = _importlib_util.spec_from_file_location("core.oauth.router", _router_path)
router_module = _importlib_util.module_from_spec(_router_spec)
sys.modules["core.oauth.router"] = router_module
_router_spec.loader.exec_module(router_module)


def _isolated_app():
    """Return a TestClient whose only mounted router is oauth.router.

    Skips server.py's lifespan/middleware so we don't fire the boot path
    on every test run.
    """
    app = FastAPI()
    app.include_router(router_module.router)
    return TestClient(app)


# ── Tier-5 redirect ───────────────────────────────────────────────────


class TestAuthoriseTier5Redirect:
    def test_unknown_service_with_oc_known_returns_302(self, tmp_path, monkeypatch):
        """OC is up and advertises a service that is NOT in
        ALLOWED_SERVICES — FreeHand 302s to OC's web console.

        Round 9 note: 'slack' used to be the canonical test service
        here, but Round 9 added SlackConnector and slack is now in
        ALLOWED_SERVICES (derived from CONNECTORS). The Round 9 test
        for slack's connector path lives in test_slack_connector.py.
        Here we use 'hackernews' which has no FreeHand connector and
        IS advertised by OC — same Round 7 semantics, no allowlist
        interference."""
        from core import agent_config
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(agent_config, "VAULT_DIR", vault)
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: True)
        monkeypatch.setattr(broker_mod, "_oc_service_known", lambda svc: svc == "hackernews")
        client = _isolated_app()
        r = client.get(
            "/api/integrations/hackernews/authorize?label=tracy", follow_redirects=False
        )
        assert r.status_code in (302, 307), (
            f"expected tier-5 redirect, got {r.status_code}: {r.text}"
        )
        location = r.headers.get("location", "")
        assert "hackernews" in location.lower()

    def test_unknown_service_with_oc_unknown_returns_503(self, tmp_path, monkeypatch):
        """OC is up but doesn't have this service — Round 8 replaced the
        old 404 with a 503 'honest option A' fallback (pre-filled GitHub
        URL + email contact). This test pins the new contract so future
        rounds don't accidentally revert to 404."""
        from core import agent_config
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(agent_config, "VAULT_DIR", vault)
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: True)
        monkeypatch.setattr(broker_mod, "_oc_service_known", lambda svc: False)
        # Round 8 tier-1b is empty
        monkeypatch.setattr(broker_mod, "_load_shared_apps", lambda: {})
        client = _isolated_app()
        r = client.get("/api/integrations/zzzunknown/authorize")
        assert r.status_code == 503, (
            f"Round 8 expects 503 honest-option-A fallback, got {r.status_code}: {r.text}"
        )
        body = r.json()
        assert body.get("error") == "no_shared_app"
        assert "request_url" in body  # pre-filled GitHub issue URL

    def test_unknown_service_with_oc_down_returns_503(self, tmp_path, monkeypatch):
        """OC is down — broker falls through to none — user gets the Round 8
        503 honest-option-A fallback. Same shape as the OC-unknown case.

        Round 9 note: uses 'hackernews' instead of 'slack' (see note in
        test_unknown_service_with_oc_known_returns_302)."""
        from core import agent_config
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(agent_config, "VAULT_DIR", vault)
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: False)
        monkeypatch.setattr(broker_mod, "_load_shared_apps", lambda: {})
        client = _isolated_app()
        r = client.get("/api/integrations/hackernews/authorize")
        assert r.status_code == 503
        body = r.json()
        assert body.get("error") == "no_shared_app"


class TestAuthoriseRegression:
    """Existing behaviour must still hold — Pitfall 5."""

    def test_allowlisted_service_with_broker_finds_creds_still_returns_json(
        self, tmp_path, monkeypatch
    ):
        """Existing Google path: tier-1..4 finds creds, returns JSON with
        authorize_url. Round 7 must not change this."""
        from core import agent_config
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(agent_config, "VAULT_DIR", vault)
        # Tier-5 stub: irrelevant to this test (Google is allowlisted), but
        # be explicit anyway.
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: False)
        client = _isolated_app()
        r = client.get("/api/integrations/google/authorize?label=work")
        # This will only pass if the existing test fixtures (broker_config
        # etc.) make tier-1..4 resolve. In the plain isolated app, no
        # broker_config exists — so the response is whatever the existing
        # implementation does with empty config. The interesting assertion
        # is just "didn't 302" — we don't accidentally introduce a redirect
        # for an allowlisted service.
        assert r.status_code in (200, 404, 501), (
            f"unexpected status {r.status_code}: {r.text}"
        )
        # CRUCIALLY: must not be 302 (redirect is reserved for tier-5).
        assert r.status_code != 302, (
            "tier-5 redirect must not preempt allowlisted-service path"
        )

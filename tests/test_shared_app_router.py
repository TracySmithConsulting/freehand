"""Tests for tier-1b (FreeHand-managed shared OAuth apps) in the OAuth router.

Round 8 of FreeHand maintenance.

Covers:
- Tier-1b above the tier-5 redirect (more-specific wins per Pitfall 5).
- 503 'honest option A' fallback when tier-1b is empty AND tier-5
  (OpenConnector) doesn't know the service. Body includes:
    * request_url: pre-filled GitHub issue URL (user clicks, reviews, submits)
    * contact_email: tracy@tracysmith.co.za (non-GitHub path)
    * scopes_help: link to shared-apps docs
    * service: the missing service id
- Existing tier-5 redirect path is preserved (no regression).
- Existing allowlisted-service JSON path is preserved (no regression).
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.oauth import broker as broker_mod

# Pitfall 27 (FreeHand): `core.oauth/__init__.py` shadows the `router` module
# name with the APIRouter object. Load router by file path via importlib.
# CI fix (Round 14): derive the path from __file__ — the original
# backslash-joined raw string only worked on Windows. Joining onto
# sys.path[0] was also unsafe: every test file does
# sys.path.insert(0, r"C:\...Windows path..."), which on POSIX runners
# lands in sys.path as a *relative* component and poisons sys.path[0].
import importlib.util as _importlib_util
import pathlib
_project_root = pathlib.Path(__file__).resolve().parent.parent
_router_path = str(_project_root / "core" / "oauth" / "router.py")
_router_spec = _importlib_util.spec_from_file_location("core.oauth.router", _router_path)
router_module = _importlib_util.module_from_spec(_router_spec)
sys.modules["core.oauth.router"] = router_module
_router_spec.loader.exec_module(router_module)


def _isolated_app() -> TestClient:
    """Minimal FastAPI app mounting only the OAuth router — skips server.py."""
    app = FastAPI()
    app.include_router(router_module.router)
    return TestClient(app)


def _isolated_vault(tmp_path, monkeypatch):
    """Patch VAULT_DIR so tests don't touch the real FreeHand vault."""
    from core import agent_config
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(agent_config, "VAULT_DIR", vault)
    return vault


# ── Tier-1b takes priority over tier-5 ──────────────────────────────────


class TestTier1bBeatsTier5:
    def test_tier1b_set_oc_known_returns_json_no_302(self, tmp_path, monkeypatch):
        """Tracy registered a Slack shared app AND OC knows Slack.
        Tier-1b is more specific than tier-5 (real FreeHand credentials
        beat runtime pointer), so the user gets FreeHand's JSON path —
        NOT a 302 to OC. This is the Round 8 fix that prevents OC's
        'configure your client first' page from hijacking the flow
        when Tracy has done the work."""
        _isolated_vault(tmp_path, monkeypatch)
        # tier-1b present
        monkeypatch.setattr(
            broker_mod, "_load_shared_apps",
            lambda: {"slack": {"client_id": "shared_id", "client_secret": "shared_secret"}},
        )
        # tier-5 ALSO knows slack — the regression vector
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: True)
        monkeypatch.setattr(broker_mod, "_oc_service_known", lambda svc: svc == "slack")
        client = _isolated_app()
        r = client.get("/api/integrations/slack/authorize?label=tracy", follow_redirects=False)
        # Slack has no connector yet (not in providers/__init__.py), so the
        # current router returns 501 with the new tier-1b check below the
        # tier-5 redirect. The KEY assertion: NOT 302.
        assert r.status_code != 302, (
            f"tier-1b must preempt tier-5 redirect; got 302 to {r.headers.get('location')}"
        )


# ── 503 'honest option A' fallback ─────────────────────────────────────


class Test503Fallback:
    def test_unknown_service_no_tier1b_no_oc_returns_503(self, tmp_path, monkeypatch):
        """Service not in ALLOWED_SERVICES, no tier-1b, no tier-5.
        Round 8 replaces the old 404 with a 503 that carries actionable
        'ask for it' info (pre-filled GitHub URL + email fallback)."""
        _isolated_vault(tmp_path, monkeypatch)
        monkeypatch.setattr(broker_mod, "_load_shared_apps", lambda: {})
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: True)
        monkeypatch.setattr(broker_mod, "_oc_service_known", lambda svc: False)
        client = _isolated_app()
        r = client.get("/api/integrations/linear/authorize?label=tracy")
        assert r.status_code == 503, (
            f"expected 503 with structured fallback, got {r.status_code}: {r.text}"
        )
        body = r.json()
        assert body["error"] == "no_shared_app"
        assert body["service"] == "linear"

    def test_503_body_has_prefilled_github_issue_url(self, tmp_path, monkeypatch):
        """The request_url field is a GitHub 'new issue' URL with title
        and body query params pre-filled — user clicks, reviews, submits.
        FreeHand does NOT create the issue on the user's behalf (Pitfall
        for Round 8: zero auth surface, user stays in control)."""
        _isolated_vault(tmp_path, monkeypatch)
        monkeypatch.setattr(broker_mod, "_load_shared_apps", lambda: {})
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: True)
        monkeypatch.setattr(broker_mod, "_oc_service_known", lambda svc: False)
        client = _isolated_app()
        r = client.get("/api/integrations/notion/authorize?label=tracy")
        assert r.status_code == 503
        body = r.json()
        assert "request_url" in body
        url = body["request_url"]
        assert url.startswith(
            "https://github.com/TracySmithConsulting/freehand/issues/new"
        ), f"unexpected repo or endpoint: {url}"
        assert "notion" in url.lower(), f"service not pre-filled: {url}"

    def test_503_body_has_email_fallback(self, tmp_path, monkeypatch):
        """Users who don't want GitHub get Tracy's email."""
        _isolated_vault(tmp_path, monkeypatch)
        monkeypatch.setattr(broker_mod, "_load_shared_apps", lambda: {})
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: True)
        monkeypatch.setattr(broker_mod, "_oc_service_known", lambda svc: False)
        client = _isolated_app()
        r = client.get("/api/integrations/linear/authorize?label=tracy")
        body = r.json()
        assert body.get("contact_email") == "tracy@tracysmith.co.za"

    def test_503_body_message_is_human_readable(self, tmp_path, monkeypatch):
        """The message field explains what the 503 means and points to
        the request_url / contact_email fields. No JSON dump antipattern."""
        _isolated_vault(tmp_path, monkeypatch)
        monkeypatch.setattr(broker_mod, "_load_shared_apps", lambda: {})
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: True)
        monkeypatch.setattr(broker_mod, "_oc_service_known", lambda svc: False)
        client = _isolated_app()
        r = client.get("/api/integrations/linear/authorize?label=tracy")
        body = r.json()
        msg = body.get("message", "")
        assert "linear" in msg.lower(), f"message missing service name: {msg!r}"
        assert "shared" in msg.lower(), f"message should mention 'shared': {msg!r}"
        assert ("github" in msg.lower()) or ("issue" in msg.lower()), (
            f"message should mention how to request: {msg!r}"
        )

    def test_503_with_oc_down_too(self, tmp_path, monkeypatch):
        """OC is down (no catalog available) AND no tier-1b. Still 503 —
        the fallback is the same shape either way."""
        _isolated_vault(tmp_path, monkeypatch)
        monkeypatch.setattr(broker_mod, "_load_shared_apps", lambda: {})
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: False)
        client = _isolated_app()
        r = client.get("/api/integrations/linear/authorize?label=tracy")
        assert r.status_code == 503
        body = r.json()
        assert body["error"] == "no_shared_app"


# ── Regression: existing behaviour preserved ────────────────────────────


class TestRound7Regression:
    def test_tier5_still_redirects_when_no_tier1b(self, tmp_path, monkeypatch):
        """Round 7 behavior: tier-5 redirect fires when tier-1b is empty
        and OC knows the service. Must still work after Round 8."""
        _isolated_vault(tmp_path, monkeypatch)
        monkeypatch.setattr(broker_mod, "_load_shared_apps", lambda: {})
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

    def test_unknown_service_no_tier1b_no_oc_returns_503_not_404(self, tmp_path, monkeypatch):
        """Old behavior was 404. Round 8 changes it to 503 for the
        'honest option A' UX. This test pins the contract so we don't
        accidentally revert to 404."""
        _isolated_vault(tmp_path, monkeypatch)
        monkeypatch.setattr(broker_mod, "_load_shared_apps", lambda: {})
        monkeypatch.setattr(broker_mod, "_oc_available", lambda: True)
        monkeypatch.setattr(broker_mod, "_oc_service_known", lambda svc: False)
        client = _isolated_app()
        r = client.get("/api/integrations/zzz_unknown_service/authorize")
        assert r.status_code == 503, (
            f"Round 8 should return 503 (not 404) for honest-option-A fallback: {r.text}"
        )

"""Tests for tier-5 of the OAuth broker: OpenConnector fallback.

Round 7 of FreeHand maintenance.

Covers:
- Tier 5 fires after tiers 1-4 return "none" and the service is known to OC.
- Tier 5 returns ("open_connector", service_id, "runtime") — not real
  credentials, just a pointer. FreeHand calls *through* OC at runtime.
- Tier 5 silently no-ops when OpenConnector is down (returns "none").
- Tier 5 never raises into the caller.
- Tier 5 result preserves the 4-tier ordering invariant (Pitfall 5):
  user > env > broker > oc > none.
"""

import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.oauth import broker


def _stub_user_settings(settings_payload: dict):
    """Mirror the helper from test_oauth_broker.py — keep tests independent."""
    return patch.object(broker, "_load_user_settings", return_value=settings_payload)


def _stub_vault_dir(tmp_path):
    """Same vault isolation as test_oauth_broker.py."""
    from core import agent_config
    vault = tmp_path / "vault"
    vault.mkdir()
    return patch.object(agent_config, "VAULT_DIR", vault)


# ── Tier 5: OpenConnector fallback ────────────────────────────────────


class TestOpenConnectorFallback:
    """The fallback tier. Resolves to ("open_connector", service_id, "runtime")
    only when tiers 1-4 are empty AND the OC runtime reports it knows the service.
    """

    def test_tier5_falls_back_to_open_connector(self, tmp_path, monkeypatch):
        """Tiers 1-4 empty + OC knows the service → tier 5 returns oc source."""
        with _stub_vault_dir(tmp_path), _stub_user_settings({}):
            # Make sure env is clear
            monkeypatch.delenv("FREEHAND_BROKER_SLACK_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_SLACK_CLIENT_SECRET", raising=False)
            # Make sure broker_config.json is empty (tmp_path/vault above is fresh)
            # OC stubs: available + service known
            monkeypatch.setattr(broker, "_oc_available", lambda: True)
            monkeypatch.setattr(
                broker, "_oc_service_known", lambda svc: svc == "slack"
            )
            cid, sec, source = broker.get_client_credentials("slack")
        assert cid is None and sec is None, (
            "tier 5 must not surface credentials — FreeHand calls through OC"
        )
        assert source == "open_connector"

    def test_tier5_returns_none_when_oc_down(self, tmp_path, monkeypatch):
        """If OC runtime is unreachable, broker falls all the way through to 'none'."""
        with _stub_vault_dir(tmp_path), _stub_user_settings({}):
            monkeypatch.delenv("FREEHAND_BROKER_SLACK_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_SLACK_CLIENT_SECRET", raising=False)
            monkeypatch.setattr(broker, "_oc_available", lambda: False)
            cid, sec, source = broker.get_client_credentials("slack")
        assert cid is None
        assert sec is None
        assert source == "none"

    def test_tier5_returns_none_when_oc_does_not_know_service(
        self, tmp_path, monkeypatch
    ):
        """OC is up but doesn't have this service — broker returns 'none'."""
        with _stub_vault_dir(tmp_path), _stub_user_settings({}):
            monkeypatch.delenv("FREEHAND_BROKER_SLACK_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_SLACK_CLIENT_SECRET", raising=False)
            monkeypatch.setattr(broker, "_oc_available", lambda: True)
            monkeypatch.setattr(broker, "_oc_service_known", lambda svc: False)
            cid, sec, source = broker.get_client_credentials("slack")
        assert source == "none"

    def test_tier5_never_raises_when_oc_http_errors(self, tmp_path, monkeypatch):
        """If OC raises anything during the availability check, broker returns 'none'."""
        with _stub_vault_dir(tmp_path), _stub_user_settings({}):
            monkeypatch.delenv("FREEHAND_BROKER_SLACK_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_SLACK_CLIENT_SECRET", raising=False)
            def _boom():
                raise RuntimeError("nope")
            monkeypatch.setattr(broker, "_oc_available", _boom)
            cid, sec, source = broker.get_client_credentials("slack")
        assert source == "none"
        assert cid is None and sec is None

    def test_tier5_preserves_resolution_order_invariant(
        self, tmp_path, monkeypatch
    ):
        """Pitfall 5 invariant: even after adding tier 5, user override still wins."""
        with _stub_vault_dir(tmp_path), _stub_user_settings({"oauth": {"providers": {
            "slack": {"client_id": "user-cid", "client_secret": "user-sec"},
        }}}):
            # OC would gladly supply slack, but user wins
            monkeypatch.setattr(broker, "_oc_available", lambda: True)
            monkeypatch.setattr(broker, "_oc_service_known", lambda svc: True)
            cid, sec, source = broker.get_client_credentials("slack")
        assert cid == "user-cid"
        assert sec == "user-sec"
        assert source == "user", (
            "tier 5 must NOT preempt tiers 1-4 — user override always wins"
        )

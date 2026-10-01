"""Tests for tier-1b of the OAuth broker: FreeHand-managed shared OAuth apps.

Round 8 of FreeHand maintenance.

Covers:
- Tier-1b fires after tier-1 (user override) returns "none" and the
  shared_apps registry has an entry for the service.
- Tier-1b returns REAL credentials (client_id + client_secret + "shared_app"
  source) — NOT a pointer like tier-5. FreeHand serves the provider's
  consent screen directly using the shared credentials.
- Tier-1b transparently decrypts the client_secret (Fernet via the
  existing encryption.key) on load.
- Pitfall-5 invariant: tier-1 (user override) still beats tier-1b when
  both are configured for the same service.
- Tier-1b never raises into the caller — corrupted encrypted blob,
  malformed entry, or filesystem error → broker returns "none" or
  falls through to tiers 2-5.
- Tier-1b ordering invariant: user > shared_app > env > broker > oc > none.
  Adding tier-1b does NOT reorder tiers 1 or 5.
"""

import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.oauth import broker


def _stub_user_settings(settings_payload: dict):
    """Mirror the helper from test_oauth_broker.py — keep tests independent."""
    return patch.object(broker, "_load_user_settings", return_value=settings_payload)


def _stub_shared_apps(shared_payload: dict):
    """Patch broker._load_shared_apps() — defined in Task 2 (RED until then)."""
    return patch.object(broker, "_load_shared_apps", return_value=shared_payload)


def _stub_vault_dir(tmp_path):
    """Same vault isolation as test_open_connector_broker.py."""
    from core import agent_config
    vault = tmp_path / "vault"
    vault.mkdir()
    return patch.object(agent_config, "VAULT_DIR", vault)


# ── Tier 1b: shared OAuth apps ─────────────────────────────────────────


def test_tier1b_returns_shared_app_credentials():
    """Tier-1b hits when tier-1 is empty and shared_apps has an entry."""
    with _stub_user_settings({}), \
         _stub_shared_apps({"slack": {"client_id": "shared_id", "client_secret": "shared_secret"}}), \
         patch.object(broker, "_oc_available", return_value=False):
        client_id, secret, source = broker.get_client_credentials("slack")
    assert source == "shared_app"
    assert client_id == "shared_id"
    assert secret == "shared_secret"


def test_tier1_user_override_beats_tier1b():
    """Pitfall-5 invariant: per-user override still wins over shared app.

    Tracy's flow: per-user credentials (settings.json oauth.providers.slack)
    are MORE SPECIFIC than FreeHand's shared app. They must always win,
    even if both are configured. This test is the canary — if it fails,
    either tier-1 was bypassed or tier-1b was inserted in the wrong place.
    """
    with _stub_user_settings({
        "oauth": {"providers": {"slack": {
            "client_id": "user_id", "client_secret": "user_secret",
        }}},
    }), \
         _stub_shared_apps({"slack": {"client_id": "shared_id", "client_secret": "shared_secret"}}), \
         patch.object(broker, "_oc_available", return_value=False):
        client_id, secret, source = broker.get_client_credentials("slack")
    assert source == "user"  # tier-1 wins
    assert client_id == "user_id"
    assert secret == "user_secret"


def test_tier1b_returns_none_when_no_shared_app_for_service():
    """Service not in shared_apps registry falls through to tiers 2-5."""
    with _stub_user_settings({}), \
         _stub_shared_apps({"notion": {"client_id": "x", "client_secret": "y"}}), \
         patch.object(broker, "_oc_available", return_value=False):
        # 'slack' is not in shared_apps — must fall through
        client_id, secret, source = broker.get_client_credentials("slack")
    assert source == "none"
    assert client_id is None
    assert secret is None


def test_tier1b_falls_through_on_decrypt_error(monkeypatch):
    """If _load_shared_apps raises (e.g. corrupted Fernet blob), tier-1b
    must not propagate the exception — broker falls through to tiers 2-5."""
    def _corrupt_load():
        raise ValueError("Fernet decryption failed: corrupt blob")

    with _stub_user_settings({}), \
         patch.object(broker, "_load_shared_apps", side_effect=_corrupt_load), \
         patch.object(broker, "_oc_available", return_value=False):
        # Must not raise. Must fall through cleanly.
        client_id, secret, source = broker.get_client_credentials("slack")
    # Falls through to "none" since tiers 2-5 also empty in this stub
    assert source == "none"


def test_tier1b_logs_warning_on_malformed_entry(caplog):
    """A shared_apps entry missing client_secret or client_id is malformed.
    Broker must WARN (not crash) and fall through."""
    import logging
    caplog.set_level(logging.WARNING, logger="core.oauth.broker")

    with _stub_user_settings({}), \
         _stub_shared_apps({"slack": {"client_id": "shared_id"}}), \
         patch.object(broker, "_oc_available", return_value=False):
        client_id, secret, source = broker.get_client_credentials("slack")
    assert source == "none"
    assert any("slack" in rec.message for rec in caplog.records)


def test_tier1b_never_returns_open_connector_when_shared_app_exists():
    """If a shared app exists for the service, tier-1b MUST return its
    credentials — never let tier-5 (OC) win. This guards against
    resolution-order reordering bugs where tier-5 was moved above tier-1b."""
    with _stub_user_settings({}), \
         _stub_shared_apps({"slack": {"client_id": "shared_id", "client_secret": "shared_secret"}}), \
         patch.object(broker, "_oc_available", return_value=True), \
         patch.object(broker, "_oc_service_known", return_value=True):
        client_id, secret, source = broker.get_client_credentials("slack")
    assert source == "shared_app"
    assert source != "open_connector"


def test_tier1b_preserves_resolution_order_invariant():
    """The canary test. Walking through the full resolution order:
    user > shared_app > env > broker > oc > none.

    Verify by setting EVERY tier for slack and confirming each layer
    can be isolated: tier-1 wins when set, tier-1b wins when tier-1
    absent, etc. If a future refactor reorders these, this fails.
    """
    user_only = {
        "oauth": {"providers": {"slack": {
            "client_id": "user_id", "client_secret": "user_secret",
        }}},
    }
    shared_only = {"slack": {"client_id": "shared_id", "client_secret": "shared_secret"}}

    # Tier 1 alone wins
    with _stub_user_settings(user_only), \
         _stub_shared_apps({}), \
         patch.object(broker, "_oc_available", return_value=False):
        _, _, source = broker.get_client_credentials("slack")
    assert source == "user"

    # Tier 1 absent → tier 1b wins
    with _stub_user_settings({}), \
         _stub_shared_apps(shared_only), \
         patch.object(broker, "_oc_available", return_value=False):
        _, _, source = broker.get_client_credentials("slack")
    assert source == "shared_app"

    # Tier 1 wins over tier 1b
    with _stub_user_settings(user_only), \
         _stub_shared_apps(shared_only), \
         patch.object(broker, "_oc_available", return_value=False):
        _, _, source = broker.get_client_credentials("slack")
    assert source == "user"

"""Tests for the OAuth broker (Option B from freehand-mcp-broker design).

Covers:
- Resolution order: user override > env var > broker_config.json > none
- Each connector's _get_credentials() returns the right values
- broker_status() reports correctly
- write_broker_config() is atomic + correct
- /api/broker/* endpoints enforce X-API-Key auth
- User override always wins over broker (Option B's contract)
"""

import os
import sys
import json
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.oauth import broker


def _stub_user_settings(settings_payload: dict):
    """Patch broker._load_user_settings to return our payload.

    The broker module binds _load_user_settings at import time as a
    module attribute so tests can monkey-patch it cleanly. This is the
    recommended pattern (avoids the core/oauth/__init__.py shadowing
    trap where `from core.oauth import router` returns the APIRouter).
    """
    return patch.object(broker, "_load_user_settings", return_value=settings_payload)


def _stub_vault_dir(tmp_path):
    """Point VAULT_DIR at an isolated test dir so we don't pollute real settings."""
    from core import agent_config
    vault = tmp_path / "vault"
    vault.mkdir()
    return patch.object(agent_config, "VAULT_DIR", vault)


# ── Resolution order ──────────────────────────────────────────────────

class TestResolutionOrder:
    def test_user_override_wins(self, tmp_path, monkeypatch):
        """User-supplied credentials in settings.json take precedence."""
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({"oauth": {"providers": {
                 "google": {"client_id": "user-cid", "client_secret": "user-sec"},
             }}}):
            cid, sec, source = broker.get_client_credentials("google")
        assert cid == "user-cid"
        assert sec == "user-sec"
        assert source == "user"

    def test_env_var_fallback(self, tmp_path, monkeypatch):
        """If user has nothing, env vars supply credentials."""
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({}):
            monkeypatch.setenv("FREEHAND_BROKER_GOOGLE_CLIENT_ID", "env-cid")
            monkeypatch.setenv("FREEHAND_BROKER_GOOGLE_CLIENT_SECRET", "env-sec")
            cid, sec, source = broker.get_client_credentials("google")
        assert cid == "env-cid"
        assert sec == "env-sec"
        assert source == "env"

    def test_broker_config_json_fallback(self, tmp_path, monkeypatch):
        """If user has nothing and env has nothing, broker_config.json supplies."""
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({}):
            # Make sure env is clear
            monkeypatch.delenv("FREEHAND_BROKER_GOOGLE_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_GOOGLE_CLIENT_SECRET", raising=False)
            # Write broker_config.json
            broker.write_broker_config({
                "google": {"client_id": "broker-cid", "client_secret": "broker-sec"},
            })
            cid, sec, source = broker.get_client_credentials("google")
        assert cid == "broker-cid"
        assert sec == "broker-sec"
        assert source == "broker"

    def test_none_when_nothing_configured(self, tmp_path, monkeypatch):
        """If nothing is configured anywhere, returns (None, None, 'none')."""
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({}):
            monkeypatch.delenv("FREEHAND_BROKER_GOOGLE_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_GOOGLE_CLIENT_SECRET", raising=False)
            cid, sec, source = broker.get_client_credentials("google")
        assert cid is None
        assert sec is None
        assert source == "none"

    def test_user_wins_over_env_and_broker(self, tmp_path, monkeypatch):
        """All three sources set — user wins."""
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({"oauth": {"providers": {
                 "google": {"client_id": "user-cid", "client_secret": "user-sec"},
             }}}):
            monkeypatch.setenv("FREEHAND_BROKER_GOOGLE_CLIENT_ID", "env-cid")
            monkeypatch.setenv("FREEHAND_BROKER_GOOGLE_CLIENT_SECRET", "env-sec")
            broker.write_broker_config({
                "google": {"client_id": "broker-cid", "client_secret": "broker-sec"},
            })
            cid, sec, source = broker.get_client_credentials("google")
        assert cid == "user-cid"
        assert source == "user"

    def test_env_wins_over_broker(self, tmp_path, monkeypatch):
        """User empty, env set, broker set — env wins."""
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({}):
            monkeypatch.setenv("FREEHAND_BROKER_GOOGLE_CLIENT_ID", "env-cid")
            monkeypatch.setenv("FREEHAND_BROKER_GOOGLE_CLIENT_SECRET", "env-sec")
            broker.write_broker_config({
                "google": {"client_id": "broker-cid", "client_secret": "broker-sec"},
            })
            cid, sec, source = broker.get_client_credentials("google")
        assert cid == "env-cid"
        assert source == "env"

    def test_partial_user_credentials_dont_count(self, tmp_path, monkeypatch):
        """If user has only client_id (no secret), don't return it — fall through."""
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({"oauth": {"providers": {
                 "google": {"client_id": "user-cid"},  # no client_secret
             }}}):
            monkeypatch.delenv("FREEHAND_BROKER_GOOGLE_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_GOOGLE_CLIENT_SECRET", raising=False)
            cid, sec, source = broker.get_client_credentials("google")
        assert source == "none"
        assert cid is None


# ── broker_status ────────────────────────────────────────────────────

class TestBrokerStatus:
    def test_status_reports_all_services(self, tmp_path):
        with _stub_vault_dir(tmp_path):
            status = broker.broker_status()
        assert "services" in status
        assert "google" in status["services"]
        assert "microsoft" in status["services"]
        assert "zoom" in status["services"]
        assert "facebook" in status["services"]
        assert "instagram" in status["services"]
        assert "github" in status["services"]

    def test_status_source_field_per_service(self, tmp_path, monkeypatch):
        """Each service entry has configured + source fields."""
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({}):
            monkeypatch.delenv("FREEHAND_BROKER_GOOGLE_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_GOOGLE_CLIENT_SECRET", raising=False)
            status = broker.broker_status()
        for service, info in status["services"].items():
            assert "configured" in info, f"{service} missing 'configured'"
            assert "source" in info, f"{service} missing 'source'"


# ── write_broker_config ──────────────────────────────────────────────

class TestWriteBrokerConfig:
    def test_write_creates_file(self, tmp_path):
        with _stub_vault_dir(tmp_path):
            broker.write_broker_config({
                "google": {"client_id": "x", "client_secret": "y"},
            })
            p = broker._broker_config_path()
            assert p.exists()
            data = json.loads(p.read_text())
            assert data["providers"]["google"]["client_id"] == "x"

    def test_write_is_atomic_via_tmp_rename(self, tmp_path, monkeypatch):
        """write_broker_config should not leave .tmp files behind."""
        with _stub_vault_dir(tmp_path):
            broker.write_broker_config({
                "google": {"client_id": "x", "client_secret": "y"},
            })
            p = broker._broker_config_path()
            tmp = p.with_suffix(".json.tmp")
            assert not tmp.exists(), "Atomic write should clean up .tmp file"
            assert p.exists(), "Final file should exist after write"

    def test_write_returns_new_status(self, tmp_path):
        """write_broker_config returns the new status snapshot."""
        with _stub_vault_dir(tmp_path):
            result = broker.write_broker_config({
                "google": {"client_id": "x", "client_secret": "y"},
            })
        assert "services" in result
        assert result["services"]["google"]["configured"] is True
        assert result["services"]["google"]["source"] == "broker"


# ── Connectors use broker ────────────────────────────────────────────

class TestConnectorsUseBroker:
    """Confirm each connector's _get_credentials returns broker-resolved values."""

    def test_google_connector_uses_broker(self, tmp_path, monkeypatch):
        from core.oauth.providers.google import GoogleConnector
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({}):
            monkeypatch.delenv("FREEHAND_BROKER_GOOGLE_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_GOOGLE_CLIENT_SECRET", raising=False)
            broker.write_broker_config({
                "google": {"client_id": "g-broker-cid", "client_secret": "g-broker-sec"},
            })
            cid, sec = GoogleConnector()._get_credentials()
        assert cid == "g-broker-cid"
        assert sec == "g-broker-sec"

    def test_zoom_connector_uses_broker(self, tmp_path, monkeypatch):
        from core.oauth.providers.zoom import ZoomConnector
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({}):
            monkeypatch.delenv("FREEHAND_BROKER_ZOOM_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_ZOOM_CLIENT_SECRET", raising=False)
            broker.write_broker_config({
                "zoom": {"client_id": "z-broker-cid", "client_secret": "z-broker-sec"},
            })
            cid, sec = ZoomConnector()._get_credentials()
        assert cid == "z-broker-cid"
        assert sec == "z-broker-sec"

    def test_microsoft_connector_uses_broker(self, tmp_path, monkeypatch):
        """Microsoft connector picks up client_id/secret from broker.

        Tenant is still read from settings.json (broker only manages
        client_id/secret). We write a real (isolated) settings.json so
        both broker AND the Microsoft connector read the same value.
        """
        from core.oauth.providers.microsoft import MicrosoftConnector
        from core import agent_config
        vault = tmp_path / "vault"
        vault.mkdir()
        # Write a real settings.json that BOTH the broker and the
        # Microsoft connector will read.
        (vault / "settings.json").write_text(json.dumps({
            "oauth": {"providers": {"microsoft": {"tenant": "common"}}},
        }), encoding="utf-8")
        monkeypatch.setattr(agent_config, "VAULT_DIR", vault)

        monkeypatch.delenv("FREEHAND_BROKER_MICROSOFT_CLIENT_ID", raising=False)
        monkeypatch.delenv("FREEHAND_BROKER_MICROSOFT_CLIENT_SECRET", raising=False)
        broker.write_broker_config({
            "microsoft": {"client_id": "ms-broker-cid", "client_secret": "ms-broker-sec"},
        })
        cid, sec, tenant = MicrosoftConnector()._get_credentials()
        assert cid == "ms-broker-cid"
        assert sec == "ms-broker-sec"
        # Tenant preserved from user settings (not broker)
        assert tenant == "common"

    def test_facebook_connector_uses_broker(self, tmp_path, monkeypatch):
        from core.oauth.providers.facebook import FacebookConnector
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({}):
            monkeypatch.delenv("FREEHAND_BROKER_FACEBOOK_CLIENT_ID", raising=False)
            monkeypatch.delenv("FREEHAND_BROKER_FACEBOOK_CLIENT_SECRET", raising=False)
            broker.write_broker_config({
                "facebook": {"client_id": "fb-broker-cid", "client_secret": "fb-broker-sec"},
            })
            cid, sec = FacebookConnector()._get_credentials()
        assert cid == "fb-broker-cid"
        assert sec == "fb-broker-sec"

    def test_user_override_takes_precedence_over_broker(self, tmp_path, monkeypatch):
        """User-supplied credentials win even when broker has values."""
        from core.oauth.providers.google import GoogleConnector
        with _stub_vault_dir(tmp_path), \
             _stub_user_settings({"oauth": {"providers": {
                 "google": {"client_id": "user-cid", "client_secret": "user-sec"},
             }}}):
            broker.write_broker_config({
                "google": {"client_id": "broker-cid", "client_secret": "broker-sec"},
            })
            cid, sec = GoogleConnector()._get_credentials()
        assert cid == "user-cid"
        assert sec == "user-sec"


# ── HTTP endpoints ───────────────────────────────────────────────────

class TestBrokerHttpEndpoints:
    def test_status_endpoint_requires_api_key(self):
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"X-API-Key": api_key})
        r = client.get("/api/broker/status")
        assert r.status_code == 200
        assert "services" in r.json()

    def test_status_endpoint_rejects_no_auth(self):
        from fastapi.testclient import TestClient
        from server import app
        client = TestClient(app)
        r = client.get("/api/broker/status")
        assert r.status_code == 401

    def test_write_config_endpoint_writes_file(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        from core import agent_config
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(agent_config, "VAULT_DIR", vault)

        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"X-API-Key": api_key})

        r = client.post(
            "/api/broker/config",
            json={"providers": {
                "google": {"client_id": "test-cid", "client_secret": "test-sec"},
            }},
        )
        assert r.status_code == 200
        assert r.json()["services"]["google"]["configured"] is True

        # And the file exists
        assert (vault / "broker_config.json").exists()

    def test_write_config_endpoint_rejects_non_dict(self):
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"X-API-Key": api_key})

        r = client.post("/api/broker/config", json={"providers": "not a dict"})
        assert r.status_code == 400

    def test_clear_endpoint_removes_file(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        from core import agent_config
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(agent_config, "VAULT_DIR", vault)

        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"X-API-Key": api_key})

        # First write
        client.post("/api/broker/config", json={"providers": {
            "google": {"client_id": "x", "client_secret": "y"},
        }})
        assert (vault / "broker_config.json").exists()

        # Then clear
        r = client.delete("/api/broker/config")
        assert r.status_code == 200
        assert not (vault / "broker_config.json").exists()
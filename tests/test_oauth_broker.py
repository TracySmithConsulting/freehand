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

# --- Round 6 regression: Microsoft (and any other) providers must send
# application/x-www-form-urlencoded on the token exchange, not text/plain.
# Passing a pre-urlencoded string to aiohttp's data= kwarg makes it default
# to text/plain, which Microsoft rejects with AADSTS900144 ("request body
# must contain grant_type"). Same bug pattern as Google on round 5.

class TestProviderTokenExchangeContentType:
    """Every provider's handle_callback must post a dict (not a string) so
    aiohttp sends the correct Content-Type header to the token endpoint."""

    def test_microsoft_uses_dict_payload(self):
        import inspect
        from core.oauth.providers.microsoft import MicrosoftConnector
        src = inspect.getsource(MicrosoftConnector.handle_callback)
        # Must NOT use the old urlencode + data=string pattern
        assert "urlencode({" not in src, (
            "Microsoft handle_callback still uses urlencode() — would send "
            "text/plain Content-Type and Microsoft rejects with AADSTS900144"
        )
        # Must use a dict-based `data=` kwarg
        assert "data=payload_dict" in src or "data={" in src, (
            "Microsoft handle_callback should post a dict via data=payload_dict"
        )

    def test_google_uses_dict_payload(self):
        """Cross-check: Google's handler was fixed in round 5 commit 63b32fa."""
        import inspect
        from core.oauth.providers.google import GoogleConnector
        src = inspect.getsource(GoogleConnector.handle_callback)
        assert "urlencode({" not in src
        assert "data=payload_dict" in src or "data={" in src


class TestRenameConnectionLabel:
    """Round 6.1: rename_connection_label() — used when user picks the wrong
    label at connect time (e.g. adds second Gmail as 'dbsa' meaning 'shazacin').
    The (service, label) pair is the primary key."""

    def _isolated_manager(self, tmp_path, monkeypatch):
        from core.oauth import manager
        from core import database
        db = tmp_path / "test.db"
        monkeypatch.setattr(manager, "DB_PATH", db)
        monkeypatch.setattr(database, "DB_PATH", db)
        # Initialize schema (connections table etc.) on the isolated DB
        database.init_db()
        return manager

    def test_rename_existing_label(self, tmp_path, monkeypatch):
        m = self._isolated_manager(tmp_path, monkeypatch)
        m.save_connection("google", "dbsa", {"access_token": "x", "expires_in": 3600}, ["email"])
        assert [c["label"] for c in m.list_connections()] == ["dbsa"]

        ok = m.rename_connection_label("google", "dbsa", "shazacin")
        assert ok is True
        labels = [c["label"] for c in m.list_connections()]
        assert labels == ["shazacin"]

    def test_rename_missing_label_returns_false(self, tmp_path, monkeypatch):
        m = self._isolated_manager(tmp_path, monkeypatch)
        ok = m.rename_connection_label("google", "nope", "shazacin")
        assert ok is False

    def test_rename_does_not_affect_other_labels(self, tmp_path, monkeypatch):
        m = self._isolated_manager(tmp_path, monkeypatch)
        m.save_connection("google", "tracy", {"access_token": "a", "expires_in": 3600}, ["email"])
        m.save_connection("google", "dbsa", {"access_token": "b", "expires_in": 3600}, ["email"])
        m.save_connection("google", "work",  {"access_token": "c", "expires_in": 3600}, ["email"])

        m.rename_connection_label("google", "dbsa", "shazacin")
        labels = sorted(c["label"] for c in m.list_connections())
        assert labels == ["shazacin", "tracy", "work"]


class TestSaveConnectionExpiresInNone:
    """Round 9: save_connection must handle token_data without an
    expires_in field (providers like Slack whose tokens don't expire).

    Regression for the live dance that crashed with:
        TypeError: int() argument must be a string, a bytes-like object
                   or a real number, not 'NoneType'
    at core/oauth/manager.py save_connection() line 70.

    Slack bot/user tokens are valid until explicitly revoked via
    auth.revoke — there's no expiry. The manager must treat
    expires_in=None as "no expiry" and set a far-future expires_at.
    """

    def _isolated_manager(self, tmp_path, monkeypatch):
        from core.oauth import manager
        from core import database
        db = tmp_path / "test.db"
        monkeypatch.setattr(manager, "DB_PATH", db)
        monkeypatch.setattr(database, "DB_PATH", db)
        database.init_db()
        return manager

    def test_save_connection_with_expires_in_none_does_not_raise(self, tmp_path, monkeypatch):
        """The bug that surfaced during the Round 9 live dance: int(None)
        raised TypeError, the callback handler returned 500, and the
        connection was never stored. This test pins the fix."""
        m = self._isolated_manager(tmp_path, monkeypatch)
        # Slack connector returns expires_in=None for non-expiring tokens
        token_data = {
            "access_token": "xoxb-fake",
            "scope": "chat:write,channels:read",
            "team_id": "T12345",
            "team_name": "Test",
            "bot_user_id": "U12345",
            "expires_in": None,
        }
        # Must not raise
        conn_id = m.save_connection("slack", "tracy", token_data, ["chat:write"])
        assert conn_id is not None

    def test_save_connection_with_no_expires_in_key_does_not_raise(self, tmp_path, monkeypatch):
        """Even safer: token_data with NO expires_in key at all (not just
        None) must work — covers providers that omit the key entirely."""
        m = self._isolated_manager(tmp_path, monkeypatch)
        token_data = {
            "access_token": "xoxb-fake",
            "scope": "chat:write",
            "team_id": "T1",
        }
        # No expires_in key at all — save_connection should still work
        conn_id = m.save_connection("slack", "tracy", token_data, ["chat:write"])
        assert conn_id is not None

    def test_save_connection_with_none_expires_in_sets_far_future(self, tmp_path, monkeypatch):
        """A connection with expires_in=None must get a far-future
        expires_at — not None, not the default 3600s-from-now."""
        m = self._isolated_manager(tmp_path, monkeypatch)
        token_data = {
            "access_token": "xoxb-fake",
            "scope": "chat:write",
            "team_id": "T1",
            "expires_in": None,
        }
        m.save_connection("slack", "tracy", token_data, ["chat:write"])
        conn = m.get_connection("slack", "tracy")
        assert conn is not None
        expires_at = conn["expires_at"]
        # expires_at must be a string parseable as datetime, and must
        # be at least 5 years in the future (proves we didn't fall back
        # to the 3600s default).
        from datetime import datetime, timezone
        if expires_at.endswith("Z"):
            expires_at = expires_at[:-1] + "+00:00"
        parsed = datetime.fromisoformat(expires_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        years_ahead = (parsed - now).days / 365.25
        assert years_ahead >= 5, (
            f"Non-expiring token should have far-future expires_at, got "
            f"{expires_at!r} which is only {years_ahead:.2f} years ahead"
        )

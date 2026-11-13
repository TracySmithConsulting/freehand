"""Pre-release review fixes (Nov 2026).

1. Telegram webhook requires the secret token header (was unauthenticated).
2. /tier cannot be changed from a remote chat gateway.
3. Agent/API document reads are confined to the vault + allowed_read_dirs.
4. Legacy broker provider secrets are encrypted at rest; writing them no
   longer deletes the shared_apps block.
6. Chat gateways run the agent in-process and relay its real reply.
"""
import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import server
from core import agent, agent_config, gateway, security
from core.oauth import broker
from core.tools import office


def _write_settings(data: dict) -> None:
    agent_config.SETTINGS_PATH.write_text(json.dumps(data))


# ── 1. Telegram webhook secret ──────────────────────────────────────────

class TestTelegramWebhookSecret:
    def _post(self, headers=None):
        client = TestClient(server.app)
        return client.post(
            "/api/gateway/telegram",
            json={"message": {"chat": {"id": 42}, "text": "/status"}},
            headers=headers or {},
        )

    def test_closed_when_secret_not_configured(self):
        _write_settings({"telegram_allow_from": ["42"]})
        assert self._post().status_code == 503

    def test_missing_header_rejected(self):
        _write_settings({"telegram_webhook_secret": "s3cret", "telegram_allow_from": ["42"]})
        assert self._post().status_code == 401

    def test_wrong_secret_rejected_and_handler_not_called(self):
        _write_settings({"telegram_webhook_secret": "s3cret", "telegram_allow_from": ["42"]})
        with patch.object(server, "handle_telegram_webhook") as h:
            r = self._post({"X-Telegram-Bot-Api-Secret-Token": "guess"})
        assert r.status_code == 401
        h.assert_not_called()

    def test_correct_secret_reaches_handler(self):
        _write_settings({"telegram_webhook_secret": "s3cret", "telegram_allow_from": ["42"]})

        async def fake(update):
            return {"ok": True, "seen": update["message"]["text"]}

        with patch.object(server, "handle_telegram_webhook", side_effect=fake):
            r = self._post({"X-Telegram-Bot-Api-Secret-Token": "s3cret"})
        assert r.status_code == 200
        assert r.json()["seen"] == "/status"


# ── 2. No tier changes from chat ────────────────────────────────────────

class TestRemoteTierChangeRefused:
    @pytest.mark.parametrize("source", ["telegram", "slack", "whatsapp"])
    def test_tier_command_does_not_change_tier(self, source):
        security.set_tier("semi_autonomous")
        reply = asyncio.run(gateway._route_to_agent("/tier full_access", source=source, caller_id="1"))
        assert security.get_current_tier() == security.PermissionTier.SEMI_AUTONOMOUS
        assert "web console" in reply


# ── 3. Read confinement ─────────────────────────────────────────────────

def _make_docx(path):
    from docx import Document
    path.parent.mkdir(parents=True, exist_ok=True)
    d = Document()
    d.add_paragraph("hello from the vault")
    d.save(str(path))
    return path


class TestReadConfinement:
    def test_vault_file_allowed(self):
        p = _make_docx(agent_config.VAULT_DIR / "docs" / "a.docx")
        assert office.check_read_allowed(str(p)) is None

    def test_outside_vault_denied(self, tmp_path):
        p = _make_docx(tmp_path / "elsewhere" / "b.docx")
        assert office.check_read_allowed(str(p)) is not None

    def test_traversal_out_of_vault_denied(self):
        evil = str(agent_config.VAULT_DIR / ".." / "outside.docx")
        assert office.check_read_allowed(evil) is not None

    def test_sibling_with_same_prefix_denied(self, tmp_path):
        # <tmp>/vault2 must not count as inside <tmp>/vault
        p = _make_docx(tmp_path / "vault2" / "c.docx")
        assert office.check_read_allowed(str(p)) is not None
        assert office._is_within(str(p), agent_config.VAULT_DIR) is False

    def test_allowed_read_dirs_setting_extends_roots(self, tmp_path):
        extra = tmp_path / "Documents"
        p = _make_docx(extra / "d.docx")
        _write_settings({"allowed_read_dirs": [str(extra)]})
        assert office.check_read_allowed(str(p)) is None

    def test_agent_tool_refuses_outside_path_without_reading(self, tmp_path):
        p = _make_docx(tmp_path / "secret" / "e.docx")
        with patch.object(agent, "read_docx") as reader:
            out = asyncio.run(agent.execute_tool("read_docx", {"path": str(p)}))
        reader.assert_not_called()
        assert "not allowed" in json.loads(out["content"])["error"]

    def test_agent_tool_reads_vault_file(self):
        p = _make_docx(agent_config.VAULT_DIR / "f.docx")
        out = asyncio.run(agent.execute_tool("read_docx", {"path": str(p)}))
        assert "hello from the vault" in out["content"]

    def test_api_endpoint_returns_403_outside_roots(self, tmp_path):
        key = server.get_or_create_api_key()
        p = _make_docx(tmp_path / "secret" / "g.docx")
        client = TestClient(server.app, headers={"X-API-Key": key})
        r = client.get("/api/tools/office/read/docx", params={"path": str(p)})
        assert r.status_code == 403


# ── 4. Broker secrets encrypted at rest ─────────────────────────────────

class TestBrokerSecretEncryption:
    @pytest.fixture(autouse=True)
    def _no_env_or_user_creds(self, monkeypatch):
        for svc in ("GOOGLE", "MICROSOFT"):
            monkeypatch.delenv(f"FREEHAND_BROKER_{svc}_CLIENT_ID", raising=False)
            monkeypatch.delenv(f"FREEHAND_BROKER_{svc}_CLIENT_SECRET", raising=False)
        with patch.object(broker, "_load_user_settings", return_value={}):
            yield

    def test_written_secret_is_not_plaintext_on_disk(self):
        broker.write_broker_config({"google": {"client_id": "cid", "client_secret": "plain-secret"}})
        raw = broker._broker_config_path().read_text()
        assert "plain-secret" not in raw
        entry = json.loads(raw)["providers"]["google"]
        assert "client_secret" not in entry
        assert entry["client_secret_enc"].startswith("gAAAA")

    def test_round_trip_through_get_client_credentials(self):
        broker.write_broker_config({"google": {"client_id": "cid", "client_secret": "plain-secret"}})
        assert broker.get_client_credentials("google") == ("cid", "plain-secret", "broker")

    def test_write_preserves_shared_apps(self):
        p = broker._broker_config_path()
        p.write_text(json.dumps({"shared_apps": {"slack": {"client_id": "x"}}}))
        broker.write_broker_config({"google": {"client_id": "cid", "client_secret": "s"}})
        assert json.loads(p.read_text())["shared_apps"] == {"slack": {"client_id": "x"}}

    def test_migration_encrypts_legacy_plaintext_and_is_idempotent(self):
        p = broker._broker_config_path()
        p.write_text(json.dumps({
            "providers": {"microsoft": {"client_id": "m", "client_secret": "legacy", "tenant": "common"}},
            "shared_apps": {"slack": {"client_id": "x"}},
        }))
        # Legacy plaintext still resolves before migration...
        assert broker.get_client_credentials("microsoft")[1] == "legacy"
        assert broker.migrate_plaintext_secrets() == 1
        data = json.loads(p.read_text())
        assert "legacy" not in p.read_text()
        assert data["providers"]["microsoft"]["tenant"] == "common"
        assert data["shared_apps"] == {"slack": {"client_id": "x"}}
        # ...and after.
        assert broker.get_client_credentials("microsoft") == ("m", "legacy", "broker")
        assert broker.migrate_plaintext_secrets() == 0


# ── 6. Gateway runs the agent and relays its reply ──────────────────────

class TestGatewayRunsAgent:
    def test_reply_is_agent_text(self, monkeypatch):
        security.set_tier("full_access")
        seen = {}

        async def fake_run_agent(command, source="", caller_id=""):
            seen.update(command=command, source=source, caller_id=caller_id)
            return {"text": "Here are your meetings.", "tools_used": [], "error": None}

        monkeypatch.setattr(agent, "run_agent", fake_run_agent)
        reply = asyncio.run(gateway._route_to_agent("what's on today?", source="telegram", caller_id="42"))
        assert reply == "Here are your meetings."
        assert seen == {"command": "what's on today?", "source": "telegram", "caller_id": "42"}

    def test_agent_error_is_reported(self, monkeypatch):
        security.set_tier("full_access")

        async def fake_run_agent(command, source="", caller_id=""):
            return {"text": "", "tools_used": [], "error": "LLM API error 401: bad key"}

        monkeypatch.setattr(agent, "run_agent", fake_run_agent)
        reply = asyncio.run(gateway._route_to_agent("hi", source="slack", caller_id="U1"))
        assert reply.startswith("Error:") and "401" in reply

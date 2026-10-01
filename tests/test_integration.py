"""Core integration tests for FreeHand."""

import sys
sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

import pytest
from fastapi.testclient import TestClient
from server import app, get_or_create_api_key


@pytest.fixture(scope="module")
def api_key():
    """Return the API key that get_or_create_api_key() minted (or pre-existing).

    Tests that hit protected endpoints use the `client` fixture (which
    injects this key as X-API-Key on every request).
    """
    return get_or_create_api_key()


@pytest.fixture(scope="module")
def client(api_key):
    """TestClient with X-API-Key pre-set on every request.

    Tests that need to bypass auth (e.g. test that /health works without
    auth) should use `raw_client` instead.
    """
    return TestClient(app, headers={"X-API-Key": api_key})


@pytest.fixture(scope="module")
def raw_client():
    """TestClient without any pre-set headers (for negative-path auth tests)."""
    return TestClient(app)


# Keep module-level `c` for backwards compatibility with older tests in
# this file (it doesn't have the API key, so those tests will now fail
# against protected endpoints — they need migration to the `client` fixture).
c = TestClient(app)


class TestCoreEndpoints:
    def test_root(self, client):
        assert client.get("/").status_code == 200

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_status(self, client):
        r = client.get("/api/v1/status")
        assert r.status_code == 200
        assert r.json()["service"] == "freehand"

    def test_scribble(self, client):
        r = client.get("/api/scribble")
        assert r.status_code == 200
        assert "content" in r.json()

    def test_tasks(self, client):
        r = client.get("/api/tasks")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_approvals(self, client):
        r = client.get("/api/approvals")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_tier(self, client):
        r = client.get("/api/security/tier")
        assert r.status_code == 200
        assert "tier" in r.json()

    def test_settings(self, client):
        r = client.get("/api/settings")
        assert r.status_code == 200

    def test_skills(self, client):
        r = client.get("/api/skills")
        assert r.status_code == 200


class TestAgentEndpoint:
    def test_agent_command(self, client):
        r = client.post("/api/agent/command", json={"command": "test"})
        assert r.status_code == 200
        d = r.json()
        assert "text" in d or "error" in d


class TestIntegrations:
    def test_list_integrations(self, client):
        r = client.get("/api/integrations")
        assert r.status_code == 200
        d = r.json()
        assert "connections" in d
        assert "available_services" in d
        # Round 9: slack was added to CONNECTORS, so ALLOWED_SERVICES
        # (derived from CONNECTORS at import time) now has 8 entries.
        # Asserting a hardcoded count here is a maintenance hazard —
        # better to assert the seven PRE-EXISTING services are present
        # (Round 8 contract) AND slack is also present (Round 9 contract).
        expected_pre_round9 = {
            "google", "microsoft", "zoom", "facebook",
            "instagram", "github", "email",
        }
        actual = set(d["available_services"])
        assert expected_pre_round9.issubset(actual), (
            f"Pre-Round-9 services missing: {expected_pre_round9 - actual}"
        )
        assert "slack" in actual, "Round 9: slack must be in available_services"
        assert len(actual) == len(expected_pre_round9) + 1, (
            f"Expected exactly 8 services (7 pre-Round-9 + slack), got {len(actual)}: {sorted(actual)}"
        )

    def test_authorize_url(self, client):
        r = client.get("/api/integrations/google/authorize?label=work")
        assert r.status_code == 200
        d = r.json()
        assert "authorize_url" in d
        assert d["label"] == "work"

    def test_unknown_service(self, client):
        # Round 8 changed the unknown-service response from 404 to 503
        # with a structured 'honest option A' body (pre-filled GitHub
        # issue URL + email fallback). The old 404 was the user dead-end;
        # 503 gives the user a way to request the service.
        r = client.get("/api/integrations/unknown/authorize")
        assert r.status_code == 503
        body = r.json()
        assert body["error"] == "no_shared_app"
        assert body["service"] == "unknown"
        assert "request_url" in body
        assert "contact_email" in body

    def test_test_no_connection(self, client):
        r = client.post("/api/integrations/google/test")
        assert r.status_code == 404


class TestAgentAPI:
    def test_list_agents(self, client):
        r = client.get("/api/agents")
        assert r.status_code == 200
        agents = r.json()
        assert isinstance(agents, list)
        assert len(agents) >= 0  # May or may not detect agents in CI

    def test_detect_agents(self, client):
        r = client.post("/api/agents/detect")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_imported_status(self, client):
        r = client.get("/api/agents/imported")
        assert r.status_code == 200
        d = r.json()
        assert "total_imports" in d
        assert "total_files" in d

    def test_preview_hermes(self, client):
        r = client.post("/api/agents/import/preview", json={"agent": "hermes"})
        assert r.status_code == 200
        d = r.json()
        assert "files" in d

    def test_preview_openclaw(self, client):
        r = client.post("/api/agents/import/preview", json={"agent": "openclaw"})
        assert r.status_code == 200
        d = r.json()
        assert "files" in d

    def test_unknown_agent(self, client):
        r = client.post("/api/agents/import", json={"agent": "nonexistent"})
        assert r.status_code == 404


class TestGateway:
    def test_gateway_status(self, client):
        r = client.get("/api/gateway/status")
        assert r.status_code == 200
        d = r.json()
        assert "telegram" in d
        assert "slack" in d
        assert "whatsapp" in d
        assert "nango" not in d  # Should be removed


# ── Negative-path tests (auth enforcement) ─────────────────────────────

class TestAuthEnforcement:
    """Verify that protected endpoints reject missing/wrong X-API-Key."""

    def test_status_requires_api_key(self, raw_client):
        r = raw_client.get("/api/v1/status")
        assert r.status_code == 401

    def test_agent_command_requires_api_key(self, raw_client):
        r = raw_client.post("/api/agent/command", json={"command": "x"})
        assert r.status_code == 401

    def test_settings_requires_api_key(self, raw_client):
        r = raw_client.get("/api/settings")
        assert r.status_code == 401

    def test_health_does_not_require_api_key(self, raw_client):
        # /health is explicitly exempted from auth
        r = raw_client.get("/health")
        assert r.status_code == 200

    def test_gateway_telegram_does_not_require_api_key(self, raw_client):
        # Gateway endpoints have their own auth mechanism
        r = raw_client.post("/api/gateway/telegram", json={"message": {}})
        # Telegram handler returns {"ok": True} or {"ok": False, "error": "Invalid JSON"}
        assert r.status_code == 200

    def test_wrong_api_key_rejected(self, raw_client):
        r = raw_client.get(
            "/api/v1/status",
            headers={"X-API-Key": "wrong-key-here"}
        )
        assert r.status_code == 401

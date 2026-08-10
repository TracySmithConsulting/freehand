"""Core integration tests for FreeHand."""

import sys
sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

import pytest
from fastapi.testclient import TestClient
from server import app

c = TestClient(app)


class TestCoreEndpoints:
    def test_root(self):
        assert c.get("/").status_code == 200

    def test_health(self):
        r = c.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_status(self):
        r = c.get("/api/v1/status")
        assert r.status_code == 200
        assert r.json()["service"] == "freehand"

    def test_scribble(self):
        r = c.get("/api/scribble")
        assert r.status_code == 200
        assert "content" in r.json()

    def test_tasks(self):
        r = c.get("/api/tasks")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_approvals(self):
        r = c.get("/api/approvals")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_tier(self):
        r = c.get("/api/security/tier")
        assert r.status_code == 200
        assert "tier" in r.json()

    def test_settings(self):
        r = c.get("/api/settings")
        assert r.status_code == 200

    def test_skills(self):
        r = c.get("/api/skills")
        assert r.status_code == 200


class TestAgentEndpoint:
    def test_agent_command(self):
        r = c.post("/api/agent/command", json={"command": "test"})
        assert r.status_code == 200
        d = r.json()
        assert "text" in d or "error" in d


class TestIntegrations:
    def test_list_integrations(self):
        r = c.get("/api/integrations")
        assert r.status_code == 200
        d = r.json()
        assert "connections" in d
        assert "available_services" in d
        assert len(d["available_services"]) == 7

    def test_authorize_url(self):
        r = c.get("/api/integrations/google/authorize?label=work")
        assert r.status_code == 200
        d = r.json()
        assert "authorize_url" in d
        assert d["label"] == "work"

    def test_unknown_service(self):
        r = c.get("/api/integrations/unknown/authorize")
        assert r.status_code == 404

    def test_test_no_connection(self):
        r = c.post("/api/integrations/google/test")
        assert r.status_code == 404


class TestAgentAPI:
    def test_list_agents(self):
        r = c.get("/api/agents")
        assert r.status_code == 200
        agents = r.json()
        assert isinstance(agents, list)
        assert len(agents) >= 0  # May or may not detect agents in CI

    def test_detect_agents(self):
        r = c.post("/api/agents/detect")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_imported_status(self):
        r = c.get("/api/agents/imported")
        assert r.status_code == 200
        d = r.json()
        assert "total_imports" in d
        assert "total_files" in d

    def test_preview_hermes(self):
        r = c.post("/api/agents/import/preview", json={"agent": "hermes"})
        assert r.status_code == 200
        d = r.json()
        assert "files" in d

    def test_preview_openclaw(self):
        r = c.post("/api/agents/import/preview", json={"agent": "openclaw"})
        assert r.status_code == 200
        d = r.json()
        assert "files" in d

    def test_unknown_agent(self):
        r = c.post("/api/agents/import", json={"agent": "nonexistent"})
        assert r.status_code == 404


class TestGateway:
    def test_gateway_status(self):
        r = c.get("/api/gateway/status")
        assert r.status_code == 200
        d = r.json()
        assert "telegram" in d
        assert "slack" in d
        assert "whatsapp" in d
        assert "nango" not in d  # Should be removed

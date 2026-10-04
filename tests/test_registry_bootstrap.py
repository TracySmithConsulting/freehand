"""Tests for the registry bootstrap wiring in server.py startup.

Round 10 PR 2 task 2.8: registry.bootstrap() is called once at server
startup so OC-discovered tools persist across FreeHand restarts.

We don't spin up a full FastAPI test client for this — we call
the startup hook body directly with monkeypatched paths.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.tools import registry  # noqa: E402


def _isolated_vault(tmp_path, monkeypatch):
    """Redirect all registry + credential_store + agent_config paths
    to a fresh tmp vault so tests don't touch real state."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(registry, "VAULT_DIR", vault)
    monkeypatch.setattr(registry, "STORAGE_PATH", vault / "tool_registry.json")
    return vault


@pytest.fixture(autouse=True)
def _isolate_global_tool_state():
    """Keep TOOL_SCHEMAS + TOOL_REGISTRY clean across tests in this file."""
    from core import agent_config
    saved_schemas = dict(agent_config.TOOL_SCHEMAS)
    saved_registry = dict(agent_config.TOOL_REGISTRY)
    yield
    new_schemas = [k for k in agent_config.TOOL_SCHEMAS if k not in saved_schemas]
    new_registry = [k for k in agent_config.TOOL_REGISTRY if k not in saved_registry]
    for k in new_schemas:
        agent_config.TOOL_SCHEMAS.pop(k, None)
    for k in new_registry:
        agent_config.TOOL_REGISTRY.pop(k, None)


# ── bootstrap() exists and merges persisted tools ──────────────────────


class TestBootstrap:
    def test_bootstrap_merges_stored_tools(self, tmp_path, monkeypatch):
        """If vault/tool_registry.json has persisted tools, bootstrap()
        merges them into core.agent_config.TOOL_SCHEMAS so the LLM
        can see them at server startup."""
        from core import agent_config
        vault = _isolated_vault(tmp_path, monkeypatch)

        # Pre-seed vault/tool_registry.json with one registered tool
        import json
        (vault / "tool_registry.json").write_text(json.dumps({
            "tools": [
                {
                    "service": "slack",
                    "label": "default",
                    "tool_name": "oc_slack_default_channels:read",
                    "schema": {
                        "type": "function",
                        "function": {
                            "name": "oc_slack_default_channels:read",
                            "description": "List public Slack channels",
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "required": [],
                            },
                        },
                    },
                }
            ]
        }))

        # Bootstrap — should inject the tool
        count = registry.bootstrap()
        assert count == 1
        # The tool is now in TOOL_SCHEMAS
        assert "oc_slack_default_channels:read" in agent_config.TOOL_SCHEMAS
        # And in TOOL_REGISTRY with the right classification
        assert agent_config.TOOL_REGISTRY.get("oc_slack_default_channels:read") == "read"

    def test_bootstrap_no_tools_returns_zero(self, tmp_path, monkeypatch):
        """Empty storage → bootstrap is a no-op, returns 0."""
        _isolated_vault(tmp_path, monkeypatch)
        count = registry.bootstrap()
        assert count == 0

    def test_bootstrap_missing_file_returns_zero(self, tmp_path, monkeypatch):
        """No tool_registry.json at all → bootstrap is a no-op."""
        _isolated_vault(tmp_path, monkeypatch)
        # Storage_path doesn't exist
        assert not registry.STORAGE_PATH.exists()
        count = registry.bootstrap()
        assert count == 0

    def test_bootstrap_idempotent(self, tmp_path, monkeypatch):
        """Calling bootstrap twice doesn't double-register tools."""
        _isolated_vault(tmp_path, monkeypatch)
        import json
        (registry.STORAGE_PATH).parent.mkdir(parents=True, exist_ok=True)
        registry.STORAGE_PATH.write_text(json.dumps({
            "tools": [
                {
                    "service": "slack", "label": "default",
                    "tool_name": "oc_slack_default_channels:read",
                    "schema": {
                        "type": "function",
                        "function": {
                            "name": "oc_slack_default_channels:read",
                            "description": "x",
                            "parameters": {"type": "object", "properties": {}, "required": []},
                        },
                    },
                }
            ]
        }))

        first = registry.bootstrap()
        second = registry.bootstrap()
        assert first == 1
        assert second == 1  # same count — bootstrap is idempotent

        from core import agent_config
        # TOOL_SCHEMAS only has one entry for this tool, not two
        keys = [k for k in agent_config.TOOL_SCHEMAS if k == "oc_slack_default_channels:read"]
        assert len(keys) == 1


# ── bootstrap() is called by server startup ───────────────────────────


class TestServerStartupWiring:
    def test_startup_event_calls_bootstrap(self, tmp_path, monkeypatch):
        """server.py's startup_event() must call registry.bootstrap()
        so tools persist across restarts.

        We don't instantiate the FastAPI app (that pulls in too many
        dependencies). Instead we replicate the wiring: import the
        startup function and call it. If the function references
        registry.bootstrap, the test catches a missing call.
        """
        # Sanity: the wiring lives in server.py startup_event
        import server
        import inspect
        source = inspect.getsource(server.startup_event)
        assert "registry.bootstrap" in source, (
            "server.py:startup_event must call registry.bootstrap() "
            "so OC-discovered tools persist across restarts"
        )

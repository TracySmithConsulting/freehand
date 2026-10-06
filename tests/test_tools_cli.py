"""Tests for the `freehand tools` CLI subcommands (Round 10 PR 2).

Mirrors the shape of tests/test_credential_cli.py (CliRunner +
isolated registry via monkeypatch on VAULT_DIR / STORAGE_PATH).
"""
from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from cli import app  # noqa: E402
from core.tools import registry  # noqa: E402


def _runner():
    return CliRunner()


def _isolated_registry(tmp_path, monkeypatch):
    """Redirect registry storage to a tmp vault."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(registry, "VAULT_DIR", vault)
    monkeypatch.setattr(registry, "STORAGE_PATH", vault / "tool_registry.json")
    return vault


# Live OC catalog fixture — same as tests/test_tool_registry.py
SLACK_CATALOG_OPTIONS = [
    {"id": "channels:read", "label": "Public channels",
     "description": "List public Slack channels and read their metadata.",
     "required": True, "defaultSelected": True, "risk": "standard"},
    {"id": "channels:history", "label": "Public channel messages",
     "description": "Read message history in public Slack channels.",
     "required": False, "defaultSelected": True, "risk": "sensitive",
     "requires": ["channels:read"]},
    {"id": "chat:write", "label": "Send messages",
     "description": "Send messages as the connected Slack user.",
     "required": False, "defaultSelected": True, "risk": "sensitive"},
    {"id": "chat:delete", "label": "Delete messages",
     "description": "Delete messages sent by the app.",
     "required": False, "defaultSelected": False, "risk": "destructive"},
    {"id": "users:read", "label": "User directory",
     "description": "Read Slack user profiles and directory information.",
     "required": False, "defaultSelected": True, "risk": "standard"},
]

GITHUB_CATALOG_OPTIONS = [
    {"id": "read:user", "label": "User profile",
     "description": "Read the authenticated user's profile.",
     "required": True, "defaultSelected": True, "risk": "standard"},
    {"id": "repo", "label": "Repository access",
     "description": "Read and write access to repositories.",
     "required": True, "defaultSelected": True, "risk": "sensitive"},
]


def _stub_oc_catalog(monkeypatch, by_service):
    def fake_get_actions(service_id, label="default"):
        return by_service.get(service_id, [])
    monkeypatch.setattr(
        "core.tools.registry._get_provider_actions",
        fake_get_actions,
    )
    # Round 12: also stub _search_actions so the translation step has input.
    def fake_search_actions(service_id, label="default"):
        authopts = by_service.get(service_id, [])
        out = []
        for a in authopts:
            authopt_id = a.get("id", "")
            label = a.get("label", authopt_id)
            out.append({
                "id": f"{service_id}.{authopt_id.replace(':', '_')}",
                "service": service_id,
                "operationType": a.get("risk", "standard"),
                "name": label,
                "description": f"OC action: {label} for {service_id}.",
            })
        return out
    monkeypatch.setattr(
        "core.tools.registry._search_actions",
        fake_search_actions,
    )


def _stub_credentials(monkeypatch, present_pairs):
    """Mark (service, label) as having credentials (bypass real credential_store)."""
    pairs = set(present_pairs)
    monkeypatch.setattr(
        "core.tools.registry._has_credential",
        lambda service, label: (service, label) in pairs,
    )


@pytest.fixture(autouse=True)
def _isolate_global_tool_state():
    """Mirror the fixture in test_tool_registry.py — keep TOOL_SCHEMAS
    and TOOL_REGISTRY clean across tests in this file too."""
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


# ── tools list ────────────────────────────────────────────────────────


class TestToolsList:
    def test_list_empty(self, tmp_path, monkeypatch):
        _isolated_registry(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["tools", "list"])
        assert result.exit_code == 0

    def test_list_shows_registered_tools(self, tmp_path, monkeypatch):
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_CATALOG_OPTIONS})
        _stub_credentials(monkeypatch, [("slack", "default")])

        # Register tools
        registry.discover_tools("slack", "default")

        result = _runner().invoke(app, ["tools", "list"])
        assert result.exit_code == 0
        # Tool names appear
        assert "oc_slack_default_channels:read" in result.stdout
        assert "oc_slack_default_users:read" in result.stdout
        # Pending writes do NOT appear in list
        assert "oc_slack_default_channels:history" not in result.stdout
        assert "oc_slack_default_chat:write" not in result.stdout

    def test_list_filter_by_service(self, tmp_path, monkeypatch):
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {
            "slack": SLACK_CATALOG_OPTIONS,
            "github": GITHUB_CATALOG_OPTIONS,
        })
        _stub_credentials(monkeypatch, [("slack", "default"),
                                        ("github", "default")])

        registry.discover_tools("slack", "default")
        registry.discover_tools("github", "default")

        result = _runner().invoke(app, ["tools", "list", "slack"])
        assert result.exit_code == 0
        # Only slack tools appear
        assert "oc_slack_default_channels:read" in result.stdout
        assert "oc_github_default_read:user" not in result.stdout


# ── tools enable-writes ──────────────────────────────────────────────


class TestToolsEnableWrites:
    def test_enable_writes_promotes_pending(self, tmp_path, monkeypatch):
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_CATALOG_OPTIONS})
        _stub_credentials(monkeypatch, [("slack", "default")])
        registry.discover_tools("slack", "default")

        # chat:write is in pending, not list
        before = _runner().invoke(app, ["tools", "list"])
        assert "oc_slack_default_chat:write" not in before.stdout

        result = _runner().invoke(app, ["tools", "enable-writes", "slack"])
        assert result.exit_code == 0

        # After enable, chat:write IS in list
        after = _runner().invoke(app, ["tools", "list"])
        assert "oc_slack_default_chat:write" in after.stdout

    def test_enable_writes_unknown_service_exits_nonzero(self, tmp_path, monkeypatch):
        _isolated_registry(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["tools", "enable-writes", "nonexistent"])
        assert result.exit_code != 0


# ── tools disable ────────────────────────────────────────────────────


class TestToolsDisable:
    def test_disable_removes_all(self, tmp_path, monkeypatch):
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {"slack": SLACK_CATALOG_OPTIONS})
        _stub_credentials(monkeypatch, [("slack", "default")])
        registry.discover_tools("slack", "default")
        _runner().invoke(app, ["tools", "enable-writes", "slack"])

        result = _runner().invoke(app, ["tools", "disable", "slack"])
        assert result.exit_code == 0

        # All slack tools gone
        after = _runner().invoke(app, ["tools", "list"])
        assert "oc_slack_default_channels:read" not in after.stdout
        assert "oc_slack_default_chat:write" not in after.stdout

    def test_disable_unknown_service_exits_nonzero(self, tmp_path, monkeypatch):
        _isolated_registry(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["tools", "disable", "nonexistent"])
        assert result.exit_code != 0


# ── tools refresh ────────────────────────────────────────────────────


class TestToolsRefresh:
    def test_refresh_probes_all_credentialed_services(self, tmp_path, monkeypatch):
        _isolated_registry(tmp_path, monkeypatch)
        _stub_oc_catalog(monkeypatch, {
            "slack": SLACK_CATALOG_OPTIONS,
            "github": GITHUB_CATALOG_OPTIONS,
        })
        _stub_credentials(monkeypatch, [("slack", "default"),
                                        ("github", "default")])

        result = _runner().invoke(app, ["tools", "refresh"])
        assert result.exit_code == 0
        # Both services have tools registered
        listed = _runner().invoke(app, ["tools", "list"])
        assert "oc_slack_default_channels:read" in listed.stdout
        assert "oc_github_default_read:user" in listed.stdout

    def test_refresh_handles_no_credentials(self, tmp_path, monkeypatch):
        """No credentials registered → refresh is a no-op, exits 0."""
        _isolated_registry(tmp_path, monkeypatch)
        _stub_credentials(monkeypatch, [])
        result = _runner().invoke(app, ["tools", "refresh"])
        assert result.exit_code == 0

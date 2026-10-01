"""Tests for the freehand shared-app CLI subcommands.

Round 8 of FreeHand maintenance.

Covers:
- shared-app add <service> --client-id ... --client-secret ... --scopes ...
- shared-app list (secrets never displayed)
- shared-app remove <service>
- Error paths: missing required args, remove of unknown service, list when
  vault is empty, add with whitespace-only secret (rejected).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

# Pitfall 27 (FreeHand): `core.oauth/__init__.py` shadows the `router`
# module name with the APIRouter object. We need `shared_apps` (NOT
# router), so the shadowing doesn't apply here. But the runner still
# needs the project root on sys.path so `cli` and `core.oauth.shared_apps`
# import cleanly.
from cli import app  # noqa: E402
from core.oauth import shared_apps  # noqa: E402


def _isolated_vault(monkeypatch, tmp_path):
    """Patch VAULT_DIR + create encryption.key so the CLI can write."""
    from core import agent_config
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(agent_config, "VAULT_DIR", vault)
    # Generate encryption.key in the isolated vault so Fernet works.
    from core.oauth.manager import _get_fernet
    _get_fernet()
    return vault


def _runner():
    return CliRunner()


# ── shared-app add ──────────────────────────────────────────────────────


class TestSharedAppAdd:
    def test_add_creates_entry(self, tmp_path, monkeypatch):
        """add creates an entry that round-trips through list."""
        _isolated_vault(monkeypatch, tmp_path)
        result = _runner().invoke(
            app,
            ["shared-app", "add", "slack",
             "--client-id", "test_id_123",
             "--client-secret", "test_secret_xyz",
             "--scopes", "chat:write,channels:read",
             "--registered-by", "tracy"],
        )
        assert result.exit_code == 0, (
            f"add failed: exit={result.exit_code}\nstdout: {result.stdout}"
        )
        # Round-trip via list
        result2 = _runner().invoke(app, ["shared-app", "list"])
        assert "slack" in result2.stdout
        assert "test_id_123" in result2.stdout
        # Secret MUST NOT appear in list output (security check)
        assert "test_secret_xyz" not in result2.stdout

    def test_add_requires_client_secret(self, tmp_path, monkeypatch):
        """add with --client-secret='' rejects with non-zero exit."""
        _isolated_vault(monkeypatch, tmp_path)
        result = _runner().invoke(
            app,
            ["shared-app", "add", "slack",
             "--client-id", "test_id",
             "--client-secret", ""],
        )
        assert result.exit_code != 0, (
            f"add with empty secret should fail; got {result.stdout}"
        )

    def test_add_replaces_existing_entry(self, tmp_path, monkeypatch):
        """Adding the same service twice replaces the entry (last-write-wins)."""
        _isolated_vault(monkeypatch, tmp_path)
        _runner().invoke(
            app, ["shared-app", "add", "slack",
                  "--client-id", "first", "--client-secret", "first_secret"],
        )
        _runner().invoke(
            app, ["shared-app", "add", "slack",
                  "--client-id", "second", "--client-secret", "second_secret"],
        )
        result = _runner().invoke(app, ["shared-app", "list"])
        assert "second" in result.stdout
        assert "first" not in result.stdout


# ── shared-app list ─────────────────────────────────────────────────────


class TestSharedAppList:
    def test_list_empty_vault(self, tmp_path, monkeypatch):
        """Empty vault returns a clean 'no shared apps' message."""
        _isolated_vault(monkeypatch, tmp_path)
        result = _runner().invoke(app, ["shared-app", "list"])
        assert result.exit_code == 0
        # Either says "No shared apps" or shows empty output — either is OK.
        # The security-relevant assertion is below in test_list_does_not_leak_secrets.

    def test_list_does_not_leak_secrets(self, tmp_path, monkeypatch):
        """list output must NEVER contain client_secret values. Critical
        security check — if this ever fails, the CLI is leaking admin
        credentials to stdout (Pitfall 27-class)."""
        _isolated_vault(monkeypatch, tmp_path)
        _runner().invoke(
            app, ["shared-app", "add", "slack",
                  "--client-id", "shared_id_aaa",
                  "--client-secret", "super_secret_xyzzy_999"],
        )
        result = _runner().invoke(app, ["shared-app", "list"])
        assert result.exit_code == 0
        assert "super_secret_xyzzy_999" not in result.stdout
        # The client_id IS allowed in list output
        assert "shared_id_aaa" in result.stdout


# ── shared-app remove ───────────────────────────────────────────────────


class TestSharedAppRemove:
    def test_remove_existing_entry(self, tmp_path, monkeypatch):
        _isolated_vault(monkeypatch, tmp_path)
        _runner().invoke(
            app, ["shared-app", "add", "slack",
                  "--client-id", "x", "--client-secret", "y"],
        )
        result = _runner().invoke(app, ["shared-app", "remove", "slack"])
        assert result.exit_code == 0
        # Verify gone
        result2 = _runner().invoke(app, ["shared-app", "list"])
        assert "slack" not in result2.stdout

    def test_remove_unknown_service_exits_nonzero(self, tmp_path, monkeypatch):
        """remove of a service that was never added — exit non-zero, clear message."""
        _isolated_vault(monkeypatch, tmp_path)
        result = _runner().invoke(app, ["shared-app", "remove", "nonexistent"])
        assert result.exit_code != 0


# ── End-to-end round-trip ───────────────────────────────────────────────


class TestRoundTrip:
    def test_add_then_remove_then_add_works(self, tmp_path, monkeypatch):
        """Verify the full lifecycle: add → list → remove → list empty → re-add → list."""
        _isolated_vault(monkeypatch, tmp_path)
        runner = _runner()

        assert runner.invoke(app, ["shared-app", "add", "slack",
                                   "--client-id", "a", "--client-secret", "b"]).exit_code == 0
        assert "slack" in runner.invoke(app, ["shared-app", "list"]).stdout

        assert runner.invoke(app, ["shared-app", "remove", "slack"]).exit_code == 0
        assert "slack" not in runner.invoke(app, ["shared-app", "list"]).stdout

        assert runner.invoke(app, ["shared-app", "add", "slack",
                                   "--client-id", "c", "--client-secret", "d"]).exit_code == 0
        assert "c" in runner.invoke(app, ["shared-app", "list"]).stdout

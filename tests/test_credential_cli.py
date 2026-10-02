"""Tests for the `freehand credential` CLI subcommands.

Round 10 PR 2 of FreeHand maintenance.

Mirrors tests/test_shared_app_cli.py shape (CliRunner + isolated
credential_store via monkeypatch on VAULT_DIR / STORAGE_PATH).
"""
from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

# Pitfall 43: CliRunner() with no args (mix_stderr was removed in
# current Typer). Don't pass kwargs that don't exist.
from cli import app  # noqa: E402
from core.oauth import credential_store  # noqa: E402


def _runner():
    return CliRunner()


def _isolated_store(tmp_path, monkeypatch):
    """Redirect credential_store to a tmp vault."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(credential_store, "VAULT_DIR", vault)
    monkeypatch.setattr(credential_store, "STORAGE_PATH", vault / "credential_store.json")
    return vault


# ── credential add ─────────────────────────────────────────────────────


class TestCredentialAdd:
    def test_add_creates_entry(self, tmp_path, monkeypatch):
        """Add an api_key credential. List shows it. Secret never displayed."""
        _isolated_store(tmp_path, monkeypatch)
        result = _runner().invoke(app, [
            "credential", "add", "github",
            "--label", "work",
            "--token", "ghp_FAKE_TOKEN_123",
        ])
        assert result.exit_code == 0, f"add failed: {result.stdout}"
        # Round-trip via list
        result2 = _runner().invoke(app, ["credential", "list"])
        assert "github" in result2.stdout
        assert "work" in result2.stdout
        # Secret MUST NOT appear in list output (security check)
        assert "ghp_FAKE_TOKEN_123" not in result2.stdout

    def test_add_default_label(self, tmp_path, monkeypatch):
        """If --label is not passed, default to 'default'."""
        _isolated_store(tmp_path, monkeypatch)
        result = _runner().invoke(app, [
            "credential", "add", "github",
            "--token", "ghp_X",
        ])
        assert result.exit_code == 0
        # list shows the default label
        result2 = _runner().invoke(app, ["credential", "list"])
        assert "default" in result2.stdout

    def test_add_rejects_empty_token(self, tmp_path, monkeypatch):
        """Empty --token rejected with non-zero exit."""
        _isolated_store(tmp_path, monkeypatch)
        result = _runner().invoke(app, [
            "credential", "add", "github",
            "--token", "",
        ])
        assert result.exit_code != 0, f"add with empty token should fail; got: {result.stdout}"

    def test_add_replaces_existing_label(self, tmp_path, monkeypatch):
        """Adding the same (service, label) twice replaces (last-write-wins)."""
        _isolated_store(tmp_path, monkeypatch)
        _runner().invoke(app, [
            "credential", "add", "github",
            "--label", "work",
            "--token", "first_token",
        ])
        _runner().invoke(app, [
            "credential", "add", "github",
            "--label", "work",
            "--token", "second_token",
        ])
        # get() returns second_token
        entry = credential_store.get("github", "work")
        assert entry["secret"] == "second_token"

    def test_add_supports_multi_credential(self, tmp_path, monkeypatch):
        """Two different labels for the same service coexist."""
        _isolated_store(tmp_path, monkeypatch)
        _runner().invoke(app, [
            "credential", "add", "github",
            "--label", "work", "--token", "ghp_WORK",
        ])
        _runner().invoke(app, [
            "credential", "add", "github",
            "--label", "personal", "--token", "ghp_PERSONAL",
        ])
        assert credential_store.get("github", "work")["secret"] == "ghp_WORK"
        assert credential_store.get("github", "personal")["secret"] == "ghp_PERSONAL"


# ── credential list ────────────────────────────────────────────────────


class TestCredentialList:
    def test_list_empty(self, tmp_path, monkeypatch):
        """Empty store returns a clean 'no credentials' message."""
        _isolated_store(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["credential", "list"])
        assert result.exit_code == 0

    def test_list_never_displays_secrets(self, tmp_path, monkeypatch):
        """list output must NEVER contain the secret value."""
        _isolated_store(tmp_path, monkeypatch)
        _runner().invoke(app, [
            "credential", "add", "github",
            "--label", "work", "--token", "ghp_LEAKED",
        ])
        result = _runner().invoke(app, ["credential", "list"])
        assert "ghp_LEAKED" not in result.stdout, (
            f"list output leaked secret: {result.stdout}"
        )


# ── credential remove ─────────────────────────────────────────────────


class TestCredentialRemove:
    def test_remove_existing(self, tmp_path, monkeypatch):
        _isolated_store(tmp_path, monkeypatch)
        _runner().invoke(app, [
            "credential", "add", "github",
            "--label", "work", "--token", "x",
        ])
        result = _runner().invoke(app, [
            "credential", "remove", "github", "--label", "work",
        ])
        assert result.exit_code == 0
        # Verify gone
        assert credential_store.get("github", "work") is None

    def test_remove_missing_label_exits_nonzero(self, tmp_path, monkeypatch):
        """Remove of a credential that was never added — exit non-zero."""
        _isolated_store(tmp_path, monkeypatch)
        result = _runner().invoke(app, [
            "credential", "remove", "nonexistent",
        ])
        assert result.exit_code != 0


# ── credential rename ──────────────────────────────────────────────────


class TestCredentialRename:
    def test_rename_moves_entry(self, tmp_path, monkeypatch):
        _isolated_store(tmp_path, monkeypatch)
        _runner().invoke(app, [
            "credential", "add", "github",
            "--label", "old_label", "--token", "ghp_X",
        ])
        result = _runner().invoke(app, [
            "credential", "rename", "github",
            "--from", "old_label", "--to", "new_label",
        ])
        assert result.exit_code == 0
        # Old gone, new has the secret
        assert credential_store.get("github", "old_label") is None
        assert credential_store.get("github", "new_label")["secret"] == "ghp_X"

    def test_rename_missing_label_exits_nonzero(self, tmp_path, monkeypatch):
        _isolated_store(tmp_path, monkeypatch)
        result = _runner().invoke(app, [
            "credential", "rename", "github",
            "--from", "nonexistent", "--to", "x",
        ])
        assert result.exit_code != 0


# ── End-to-end round-trip ────────────────────────────────────────────


class TestRoundTrip:
    def test_add_then_list_then_remove_then_add_works(self, tmp_path, monkeypatch):
        """Verify the full lifecycle: add → list → remove → list empty → re-add → list."""
        _isolated_store(tmp_path, monkeypatch)
        runner = _runner()

        assert runner.invoke(app, [
            "credential", "add", "github",
            "--label", "work", "--token", "ghp_FOO",
        ]).exit_code == 0
        assert "github" in runner.invoke(app, ["credential", "list"]).stdout

        assert runner.invoke(app, [
            "credential", "remove", "github", "--label", "work",
        ]).exit_code == 0
        assert "github" not in runner.invoke(app, ["credential", "list"]).stdout

        assert runner.invoke(app, [
            "credential", "add", "github",
            "--label", "work", "--token", "ghp_BAR",
        ]).exit_code == 0
        assert "ghp_BAR" not in runner.invoke(app, ["credential", "list"]).stdout
        # The credential is in the store with the new secret
        assert credential_store.get("github", "work")["secret"] == "ghp_BAR"

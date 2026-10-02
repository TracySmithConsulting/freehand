"""Round 10 — PR 2 (tool discovery + credential store).

Tests for the credential store at vault/credential_store.json. The
store holds per-service, per-label credentials (OAuth tokens, API
keys, PATs) Fernet-encrypted at rest. The (service, label) pair is
the primary key — multi-credential Tracy can wire two GitHub
accounts (work, personal) and have both available to the LLM as
distinct tool namespaces.

Mirrors the shape of tests/test_oauth_broker.py's TestRenameConnectionLabel
class (per-row fixtures with isolated manager) and tests/test_database.py
(monkeypatch constants to redirect paths).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core.oauth import credential_store  # noqa: E402


# ── Fixtures ───────────────────────────────────────────────────────────


def _isolated_store(tmp_path, monkeypatch):
    """Redirect credential_store to a tmp vault/credential_store.json.

    Mirrors the pattern in tests/test_oauth_broker.py's _isolated_manager
    and tests/test_database.py's monkeypatch pattern.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    storage = vault / "credential_store.json"
    monkeypatch.setattr(credential_store, "VAULT_DIR", vault)
    monkeypatch.setattr(credential_store, "STORAGE_PATH", storage)
    # No existing file — fresh start
    return storage


# ── CRUD round-trip ─────────────────────────────────────────────────────


class TestCredentialStoreCRUD:
    def test_add_creates_entry_with_encrypted_secret(self, tmp_path, monkeypatch):
        """add() stores the credential with Fernet-encrypted secret.
        On-disk JSON MUST NOT contain the plaintext secret.
        """
        storage = _isolated_store(tmp_path, monkeypatch)
        credential_store.add(
            service="github",
            label="work",
            secret="ghp_FAKE_TOKEN_12345",
            auth_type="api_key",
        )
        # Storage file exists
        assert storage.exists()
        # On-disk JSON does NOT contain plaintext
        raw = json.loads(storage.read_text(encoding="utf-8"))
        assert "github" in raw["credentials"]
        assert "work" in raw["credentials"]["github"]
        # The secret_enc field is the Fernet ciphertext, NOT the plaintext
        on_disk_secret = raw["credentials"]["github"]["work"].get("secret_enc", "")
        assert on_disk_secret != "ghp_FAKE_TOKEN_12345", (
            "Plaintext secret leaked to disk — Fernet encryption failed"
        )
        # Fernet ciphertexts are gAAAAA-prefixed base64
        assert on_disk_secret.startswith("gAAAAA"), (
            f"Expected Fernet ciphertext format, got: {on_disk_secret[:20]}"
        )

    def test_get_returns_decrypted_secret(self, tmp_path, monkeypatch):
        """get() decrypts the secret before returning."""
        _isolated_store(tmp_path, monkeypatch)
        credential_store.add("slack", "tracy", "xoxb-FAKE-TOKEN", "oauth")
        entry = credential_store.get("slack", "tracy")
        assert entry is not None
        assert entry["service"] == "slack"
        assert entry["label"] == "tracy"
        assert entry["auth_type"] == "oauth"
        assert entry["secret"] == "xoxb-FAKE-TOKEN", (
            "get() must decrypt the Fernet ciphertext back to plaintext"
        )

    def test_list_all_returns_metadata_no_secrets(self, tmp_path, monkeypatch):
        """list_all() returns all credentials' metadata but NEVER the
        decrypted secret. (Same security model as shared_apps.list_shared_apps.)
        """
        _isolated_store(tmp_path, monkeypatch)
        credential_store.add("github", "work", "ghp_SECRET_AAA", "api_key")
        credential_store.add("github", "personal", "ghp_SECRET_BBB", "api_key")
        credential_store.add("slack", "tracy", "xoxb-SECRET_CCC", "oauth")
        all_entries = credential_store.list_all()
        # Three entries
        assert len(all_entries) == 3
        # Each entry has service, label, auth_type, registered_at — NOT secret
        for entry in all_entries:
            assert "secret" not in entry, (
                f"list_all() leaked secret for {entry['service']}/{entry['label']}"
            )
            for required_key in ("service", "label", "auth_type", "registered_at"):
                assert required_key in entry

    def test_get_returns_none_for_missing(self, tmp_path, monkeypatch):
        """get() of a non-existent (service, label) returns None, not raises."""
        _isolated_store(tmp_path, monkeypatch)
        assert credential_store.get("nope", "default") is None
        credential_store.add("github", "work", "x", "api_key")
        assert credential_store.get("github", "nope") is None
        assert credential_store.get("nope", "work") is None

    def test_add_with_existing_label_replaces(self, tmp_path, monkeypatch):
        """Adding the same (service, label) twice replaces the entry
        (last-write-wins). This matches the shared_apps behavior.
        """
        _isolated_store(tmp_path, monkeypatch)
        credential_store.add("github", "work", "first_token", "api_key")
        credential_store.add("github", "work", "second_token", "api_key")
        entry = credential_store.get("github", "work")
        assert entry["secret"] == "second_token", (
            "Second add() with same (service, label) must replace, not append"
        )

    def test_remove_deletes_entry(self, tmp_path, monkeypatch):
        """remove() returns True when the entry existed, False otherwise.
        Subsequent get() returns None.
        """
        _isolated_store(tmp_path, monkeypatch)
        credential_store.add("github", "work", "x", "api_key")
        assert credential_store.remove("github", "work") is True
        assert credential_store.get("github", "work") is None
        # Second remove returns False
        assert credential_store.remove("github", "work") is False


# ── Multi-credential support ───────────────────────────────────────────


class TestMultiCredential:
    def test_two_labels_for_same_service_coexist(self, tmp_path, monkeypatch):
        """The whole point of labels: two GitHub accounts, both stored,
        both readable independently."""
        _isolated_store(tmp_path, monkeypatch)
        credential_store.add("github", "work", "ghp_WORK", "api_key")
        credential_store.add("github", "personal", "ghp_PERSONAL", "api_key")
        work = credential_store.get("github", "work")
        personal = credential_store.get("github", "personal")
        assert work["secret"] == "ghp_WORK"
        assert personal["secret"] == "ghp_PERSONAL"
        # list_all shows both
        all_entries = credential_store.list_all()
        labels = sorted(e["label"] for e in all_entries if e["service"] == "github")
        assert labels == ["personal", "work"]

    def test_labels_for_different_services_dont_collide(self, tmp_path, monkeypatch):
        """Same label across different services is fine (one Slack label=tracy,
        one GitHub label=tracy — different (service, label) keys).
        """
        _isolated_store(tmp_path, monkeypatch)
        credential_store.add("slack", "tracy", "xoxb-SLACK", "oauth")
        credential_store.add("github", "tracy", "ghp-GITHUB", "api_key")
        assert credential_store.get("slack", "tracy")["secret"] == "xoxb-SLACK"
        assert credential_store.get("github", "tracy")["secret"] == "ghp-GITHUB"


# ── Rename ──────────────────────────────────────────────────────────────


class TestRename:
    def test_rename_moves_entry(self, tmp_path, monkeypatch):
        """rename() moves the credential from (service, old) to (service, new).
        Subsequent get(service, old) returns None; get(service, new) returns
        the same secret. Mirrors Round 6.1's rename_connection_label.
        """
        _isolated_store(tmp_path, monkeypatch)
        credential_store.add("github", "old_label", "ghp_TOKEN", "api_key")
        # Rename
        assert credential_store.rename("github", "old_label", "new_label") is True
        # Old gone
        assert credential_store.get("github", "old_label") is None
        # New has the secret
        new_entry = credential_store.get("github", "new_label")
        assert new_entry is not None
        assert new_entry["secret"] == "ghp_TOKEN"
        assert new_entry["label"] == "new_label"

    def test_rename_missing_label_returns_false(self, tmp_path, monkeypatch):
        _isolated_store(tmp_path, monkeypatch)
        assert credential_store.rename("github", "nonexistent", "x") is False


# ── Storage layout ──────────────────────────────────────────────────────


class TestStorageLayout:
    def test_storage_file_is_valid_json(self, tmp_path, monkeypatch):
        """After multiple adds, the on-disk file is parseable JSON."""
        storage = _isolated_store(tmp_path, monkeypatch)
        credential_store.add("github", "work", "x", "api_key")
        credential_store.add("slack", "tracy", "y", "oauth")
        # Round-trip parse
        raw = json.loads(storage.read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert "credentials" in raw
        assert set(raw["credentials"].keys()) == {"github", "slack"}

    def test_storage_file_atomic_write(self, tmp_path, monkeypatch):
        """Writes go through tmp file + rename so a crash mid-write
        doesn't leave a half-written JSON file.
        """
        _isolated_store(tmp_path, monkeypatch)
        credential_store.add("github", "work", "x", "api_key")
        # No .tmp file should be left around
        tmp_files = list((tmp_path / "vault").glob("*.tmp"))
        assert not tmp_files, f"Stale .tmp files: {tmp_files}"


# ── Encryption invariants ──────────────────────────────────────────────


class TestEncryptionInvariants:
    def test_secret_is_always_encrypted_on_disk(self, tmp_path, monkeypatch):
        """Regardless of how many adds, the on-disk JSON never contains
        plaintext secrets. This is the security boundary.
        """
        storage = _isolated_store(tmp_path, monkeypatch)
        secrets_added = [
            ("github", "work", "ghp_FOO_aaa111"),
            ("github", "personal", "ghp_BBB_ccc333"),
            ("slack", "tracy", "xoxb-DDD-eee999"),
            ("notion", "default", "secret_DDD-fff555"),
        ]
        for svc, lbl, sec in secrets_added:
            credential_store.add(svc, lbl, sec, "api_key")
        # Read raw bytes and grep for each plaintext
        raw_bytes = storage.read_bytes()
        for _, _, sec in secrets_added:
            assert sec.encode() not in raw_bytes, (
                f"Plaintext '{sec}' found in on-disk storage — "
                f"Fernet encryption bypassed"
            )

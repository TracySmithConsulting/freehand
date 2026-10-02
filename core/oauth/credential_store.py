"""FreeHand credential store (Round 10 PR 2).

Per-service, per-label credential storage. The (service, label) pair
is the primary key — multi-credential Tracy can wire two GitHub
accounts under different labels, two Slack workspaces, etc.

Auth types:
- ``oauth`` — token obtained via the OAuth dance (Round 8). The
  ``secret`` here is the post-dance access/refresh token pair (the
  full token_data dict from the connector's handle_callback).
- ``api_key`` — single string token (GitHub PAT, Notion integration
  key, Linear API key, Anthropic API key, etc.). The ``secret`` is
  the token string.

Storage:
- ``vault/credential_store.json`` — Fernet-encrypted at rest via
  the same ``vault/encryption.key`` used for connection tokens.
- On-disk shape (read by humans for debugging): the ``secret_enc``
  field is a Fernet ciphertext (gAAAAA-prefixed), never plaintext.
- All write paths are atomic (tmp file + rename) and lock-protected
  via ``threading.Lock`` so two concurrent CLI invocations don't
  clobber each other.

Methods FreeHand's broker + tool layer call:
- ``add(service, label, secret, auth_type)`` — register a credential.
- ``get(service, label) -> Optional[dict]`` — fetch one credential,
  decrypted. ``None`` if absent.
- ``list_all() -> List[dict]`` — list all credentials' metadata.
  Returns ``{service, label, auth_type, registered_at}`` — never the
  secret. CLI uses this for ``freehand credential list``.
- ``remove(service, label) -> bool`` — delete a credential.
- ``rename(service, old_label, new_label) -> bool`` — Round 6.1 parity.

Multi-credential design:
- Same service can have multiple labels (e.g. github:work,
  github:personal). Each (service, label) is a separate row.
- Add with the same (service, label) replaces the existing entry
  (last-write-wins).
- Default label is "default" — users don't have to type it
  explicitly, but the storage always has a label.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

sys_path_inserted = False


def _ensure_sys_path():
    """Late-bind sys.path so this module works when imported via
    tests/* without the caller having to do sys.path manipulation.
    Idempotent.
    """
    global sys_path_inserted
    if sys_path_inserted:
        return
    import sys
    project_root = Path(__file__).parent.parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    sys_path_inserted = True


log = logging.getLogger(__name__)

_lock = threading.Lock()

# Vault layout — same shape as shared_apps.py. Tests monkeypatch
# VAULT_DIR and STORAGE_PATH to redirect to tmp dirs.
VAULT_DIR = Path(__file__).parent.parent.parent / "vault"
STORAGE_PATH = VAULT_DIR / "credential_store.json"


def _read_storage() -> dict:
    """Read credential_store.json. Returns the on-disk dict, or
    ``{"version": 1, "credentials": {}}`` if absent or unreadable.

    Never raises — callers (broker tier-1b / tool layer) treat read
    errors as "no credentials" and fall through.
    """
    if not STORAGE_PATH.exists():
        return {"version": 1, "credentials": {}}
    try:
        return json.loads(STORAGE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("credential_store: failed to read %s: %s", STORAGE_PATH, e)
        return {"version": 1, "credentials": {}}


def _write_storage(storage: dict) -> None:
    """Atomic write of credential_store.json. Caller holds the lock.

    Round 9's pattern: tmp file + rename. Crash mid-write leaves
    the previous file intact, never a half-written JSON.
    """
    STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORAGE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(storage, indent=2), encoding="utf-8")
    tmp.replace(STORAGE_PATH)


def _encrypt_secret(plaintext: str) -> str:
    """Fernet-encrypt via the same vault key used for connection tokens.

    For ``auth_type='oauth'`` the plaintext is a JSON-serialized
    dict of token data (access_token, refresh_token, scope, etc.).
    For ``auth_type='api_key'`` it's the token string. Either way the
    on-disk shape is the same Fernet ciphertext.
    """
    _ensure_sys_path()
    from core.oauth.manager import encrypt
    return encrypt(plaintext)


def _decrypt_secret(ciphertext: str) -> Optional[str]:
    """Fernet-decrypt. Returns None on any failure — caller treats as
    malformed entry, logs warning, falls through.
    """
    _ensure_sys_path()
    try:
        from core.oauth.manager import decrypt
        return decrypt(ciphertext)
    except Exception as e:
        log.warning("credential_store: Fernet decrypt failed: %s", e)
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def add(
    service: str,
    label: str,
    secret: str,
    auth_type: str,
    registered_by: str = "",
) -> dict:
    """Add or replace a credential at (service, label).

    Args:
        service: lowercased service id (e.g. "github", "slack", "notion").
        label: lowercased label (e.g. "work", "personal", "default").
            The CLI defaults to "default" if the user doesn't pass --label.
        secret: the credential. String for api_key; JSON-serialized
            dict (the connector's handle_callback return value) for oauth.
        auth_type: "api_key" or "oauth". Determines the storage field
            but not the encryption — both Fernet-encrypt the secret.
        registered_by: optional audit field.

    Returns:
        Metadata dict (service, label, auth_type, registered_at).
        Does NOT include the decrypted secret — that's get()'s job.
    """
    if not service or not label or not secret or not auth_type:
        raise ValueError("service, label, secret, and auth_type are required")
    service = service.lower()
    label = label.lower()
    if auth_type not in ("api_key", "oauth"):
        raise ValueError(f"auth_type must be 'api_key' or 'oauth', got: {auth_type!r}")

    encrypted = _encrypt_secret(secret)

    with _lock:
        storage = _read_storage()
        creds = storage.setdefault("credentials", {})
        svc = creds.setdefault(service, {})
        svc[label] = {
            "auth_type": auth_type,
            "secret_enc": encrypted,
            "registered_by": registered_by,
            "registered_at": _now_iso(),
        }
        _write_storage(storage)

    log.info("credential_store: added %s/%s (auth_type=%s)", service, label, auth_type)
    return {
        "service": service,
        "label": label,
        "auth_type": auth_type,
        "registered_by": registered_by,
        "registered_at": svc[label]["registered_at"],
    }


def get(service: str, label: str) -> Optional[dict]:
    """Return decrypted credential at (service, label) or None.

    Returned dict has keys: service, label, auth_type, secret,
    registered_at. The secret is the original plaintext — for
    auth_type='oauth' it's a JSON-serialized token-data dict; for
    auth_type='api_key' it's the token string.

    Callers that need to know "does this credential exist?" without
    paying the decrypt cost should use ``has(service, label)`` instead.
    """
    service = service.lower()
    label = label.lower()
    storage = _read_storage()
    entry = storage.get("credentials", {}).get(service, {}).get(label)
    if not entry:
        return None
    secret_enc = entry.get("secret_enc")
    if not secret_enc:
        log.warning("credential_store: missing secret_enc for %s/%s", service, label)
        return None
    plain = _decrypt_secret(secret_enc)
    if plain is None:
        return None
    return {
        "service": service,
        "label": label,
        "auth_type": entry.get("auth_type", ""),
        "secret": plain,
        "registered_at": entry.get("registered_at", ""),
        "registered_by": entry.get("registered_by", ""),
    }


def has(service: str, label: str) -> bool:
    """Cheap existence check — no decrypt, no vault read.

    Useful for the tool layer's "should I register oc_<svc>_<label>_*
    tools?" question.
    """
    service = service.lower()
    label = label.lower()
    storage = _read_storage()
    return label in storage.get("credentials", {}).get(service, {})


def list_all() -> List[dict]:
    """List all credentials' metadata. NEVER includes the decrypted secret.

    Used by ``freehand credential list``. Shape: ordered (service,
    label) for stable display.
    """
    storage = _read_storage()
    out: List[dict] = []
    for service, labels in sorted(storage.get("credentials", {}).items()):
        if not isinstance(labels, dict):
            continue
        for label, entry in sorted(labels.items()):
            if not isinstance(entry, dict):
                continue
            out.append({
                "service": service,
                "label": label,
                "auth_type": entry.get("auth_type", ""),
                "registered_at": entry.get("registered_at", ""),
                "registered_by": entry.get("registered_by", ""),
            })
    return out


def remove(service: str, label: str) -> bool:
    """Remove a credential at (service, label).

    Returns True if the entry existed, False if not.
    """
    service = service.lower()
    label = label.lower()
    with _lock:
        storage = _read_storage()
        svc = storage.get("credentials", {}).get(service)
        if not svc or label not in svc:
            return False
        del svc[label]
        # Clean up empty service dict
        if not svc:
            del storage["credentials"][service]
        _write_storage(storage)
    log.info("credential_store: removed %s/%s", service, label)
    return True


def rename(service: str, old_label: str, new_label: str) -> bool:
    """Move a credential from (service, old_label) to (service, new_label).

    Mirrors Round 6.1's rename_connection_label: atomic move, returns
    True on success, False if (service, old_label) didn't exist.
    Collides with (service, new_label) is a no-op-safe overwrite
    (the new_label entry wins, same as add()).
    """
    service = service.lower()
    old_label = old_label.lower()
    new_label = new_label.lower()
    with _lock:
        storage = _read_storage()
        svc = storage.get("credentials", {}).get(service)
        if not svc or old_label not in svc:
            return False
        # Move: if new_label already exists, the rename overwrites it
        # (last-write-wins, same as add()).
        svc[new_label] = svc.pop(old_label)
        _write_storage(storage)
    log.info("credential_store: renamed %s/%s -> %s", service, old_label, new_label)
    return True

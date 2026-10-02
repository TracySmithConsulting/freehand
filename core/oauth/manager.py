import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timezone, timedelta
import logging

from cryptography.fernet import Fernet

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from core.database import DB_PATH

log = logging.getLogger(__name__)


VAULT_DIR = Path(__file__).parent.parent.parent / "vault"
ENCRYPTION_KEY_PATH = VAULT_DIR / "encryption.key"

_lock = threading.Lock()


def _get_fernet() -> Fernet:
    key_path = ENCRYPTION_KEY_PATH
    if not key_path.exists():
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
    key = key_path.read_bytes()
    return Fernet(key)


def encrypt(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _get_fernet().decrypt(value.encode()).decode()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(s: str) -> datetime:
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=timezone.utc)


def save_connection(service, label, token_data, scopes, user_id=""):
    encrypted = encrypt(json.dumps(token_data))
    scopes_str = ",".join(scopes)
    now = _now_iso()
    expires_in = token_data.get("expires_in", 3600)
    expires_at = token_data.get("expires_at")
    if not expires_at:
        if expires_in is None:
            # Provider doesn't expire tokens (e.g. Slack bot/user tokens
            # are valid until explicitly revoked via auth.revoke).
            # Use a 10-year far-future so the connection stays "active"
            # in the DB without forcing the user to refresh.
            expires_at = (
                datetime.now(timezone.utc).timestamp() + (10 * 365 * 24 * 3600)
            )
        else:
            expires_at = datetime.now(timezone.utc).timestamp() + int(expires_in)
        expires_at = datetime.utcfromtimestamp(expires_at).isoformat()
    conn = _get_conn()
    cursor = conn.execute(
        """
        INSERT INTO connections (service, label, user_id, token_data, scopes, connected_at, expires_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(service, label) DO UPDATE SET
            token_data = excluded.token_data,
            scopes = excluded.scopes,
            user_id = excluded.user_id,
            expires_at = excluded.expires_at,
            last_refreshed = excluded.last_refreshed,
            status = excluded.status
        """,
        (service, label, user_id, encrypted, scopes_str, now, expires_at, "active"),
    )
    conn.commit()
    conn_id = cursor.lastrowid
    conn.close()
    return conn_id


def get_connection(service, label="default"):
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM connections WHERE service = ? AND label = ? AND status = 'active'",
        (service, label),
    ).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["token_data"] = json.loads(decrypt(result["token_data"]))
    return result


def rename_connection_label(service: str, old_label: str, new_label: str) -> bool:
    """Rename a connection's label. Returns True if a row was updated.

    Used when the user picks the wrong label at connect time (e.g.
    "dbsa" when they meant "shazacin"). The (service, label) pair is
    the primary key, so this is a single UPDATE.
    """
    conn = _get_conn()
    cursor = conn.execute(
        "UPDATE connections SET label = ? WHERE service = ? AND label = ? AND status = 'active'",
        (new_label, service, old_label),
    )
    conn.commit()
    updated = cursor.rowcount > 0
    conn.close()
    return updated


def list_connections(service=None):
    conn = _get_conn()
    if service:
        rows = conn.execute(
            "SELECT * FROM connections WHERE service = ? AND status = 'active' ORDER BY label",
            (service,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM connections WHERE status = 'active' ORDER BY service, label"
        ).fetchall()
    conn.close()
    results = []
    for row in rows:
        d = dict(row)
        d["token_data"] = json.loads(decrypt(d["token_data"]))
        results.append(d)
    return results


def delete_connection(service, label="default"):
    conn = _get_conn()
    cursor = conn.execute(
        "DELETE FROM connections WHERE service = ? AND label = ?",
        (service, label),
    )
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


async def refresh_token(service, label="default"):
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM connections WHERE service = ? AND label = ? AND status = 'active'",
        (service, label),
    ).fetchone()
    conn.close()
    if not row:
        return None
    token_data = json.loads(decrypt(row["token_data"]))
    refresh_tok = token_data.get("refresh_token")
    if not refresh_tok:
        return None
    if token_data.get("refresh_expires_at"):
        ref_exp = _parse_iso(token_data["refresh_expires_at"])
        if datetime.now(timezone.utc) >= ref_exp:
            conn = _get_conn()
            conn.execute(
                "UPDATE connections SET status = 'expired' WHERE service = ? AND label = ?",
                (service, label),
            )
            conn.commit()
            conn.close()
            return None
    from core.oauth.providers import get_connector
    connector = get_connector(service)
    if not connector:
        return None
    try:
        new_token = await connector.refresh(token_data)
        if new_token:
            scopes = row["scopes"].split(",") if row["scopes"] else []
            save_connection(service, label, new_token, scopes, token_data.get("user_id", ""))
            return new_token
    except Exception:
        pass
    return None


def is_token_expired(service, label="default"):
    conn = get_connection(service, label)
    if not conn:
        return True
    expires_at = conn.get("expires_at", "")
    if not expires_at:
        return False
    exp_dt = _parse_iso(expires_at)
    now = datetime.now(timezone.utc)
    # Strip tzinfo from both sides for comparison
    exp_naive = exp_dt.replace(tzinfo=None)
    now_naive = now.replace(tzinfo=None)
    return now_naive >= exp_naive - timedelta(minutes=5)


def get_connection_status(service, label="default"):
    conn = get_connection(service, label)
    if not conn:
        return {"connected": False, "status": "disconnected"}
    expired = is_token_expired(service, label)
    expires_at = conn.get("expires_at", "")
    connected_at = conn.get("connected_at", "")
    scopes = conn.get("scopes", "").split(",")
    user_info = conn.get("token_data", {}).get("user_info", {})
    email = user_info.get("email", "") or user_info.get("upn", "") or user_info.get("login", "")
    return {
        "connected": True,
        "status": "expired" if expired else "active",
        "label": conn["label"],
        "service": conn["service"],
        "scopes": scopes,
        "connected_at": connected_at,
        "expires_at": expires_at,
        "email": email,
        "name": user_info.get("name", "") or user_info.get("displayName", "") or user_info.get("login", ""),
        "expired": expired,
    }


# ── Legacy connections.db migration (Round 10 PR 2) ──────────────────


def migrate_legacy_connections_db(skip: bool = False) -> int:
    """One-time migration: read vault/connections.db rows, write them
    to vault/credential_store.json as OAuth credentials.

    Round 5-9 stored OAuth connection tokens in vault/connections.db
    (SQLite, with Fernet-encrypted token_data column). Round 10
    introduces vault/credential_store.json as the new first-class
    registry. This function moves existing rows to the new store
    so the broker + tool layer can find them under the (service,
    label) shape.

    Returns the number of rows migrated (0 if nothing to do, or if
    the migration was previously run, or if skip=True).

    Idempotency:
    - A sentinel ``"_migrated": true`` flag in
      ``vault/credential_store.json`` gates the migration. Once
      the migration has run, the sentinel is set and subsequent
      calls return 0 without touching anything.
    - For an interrupted run (some rows migrated, sentinel not
      written), the next call's "skip if already in store" check
      prevents double-migration.

    Security:
    - The legacy token_data is Fernet-encrypted under the SAME key
      as the new store. We decrypt to plaintext, then re-encrypt
      into the new store. Both ciphertexts are independent Fernet
      outputs against the same key.
    - The legacy vault/connections.db file is NOT deleted. The
      legacy table will keep working until Round 11+ removes it.
    - The flag ``skip=True`` is for users who want a fresh start
      (e.g. after rotating the encryption key). It marks the
      migration as done without reading the legacy file.

    Coexistence with existing credential_store entries:
    - If (service, label) already exists in credential_store.json,
      the migration SKIPS that row (preserves the newer entry).
      This handles the case where a user re-ran the OAuth dance
      against a Round-10 build and the new token already landed
      in the new store.
    """
    if skip:
        _mark_migration_done()
        return 0

    # Already migrated? Read the sentinel and bail early.
    storage = _read_credential_store_safe()
    if storage.get("_migrated") is True:
        return 0

    legacy_db = DB_PATH
    if not legacy_db.exists():
        # No legacy file — nothing to migrate. Mark done so we
        # don't re-check on every boot.
        _mark_migration_done()
        return 0

    # Open the legacy DB read-only.
    try:
        conn = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        log.warning("migrate_legacy_connections_db: cannot open %s: %s", legacy_db, e)
        return 0

    try:
        rows = conn.execute(
            "SELECT service, label, token_data, scopes FROM connections"
        ).fetchall()
    except sqlite3.OperationalError:
        # Table doesn't exist — empty legacy db
        rows = []
    finally:
        conn.close()

    # Lazy import to avoid circular dependency (manager -> credential_store
    # is fine, but doing it at module level would force credential_store
    # to load before manager finishes initializing).
    from core.oauth import credential_store as _cs

    migrated = 0
    for service, label, token_data_enc, scopes_csv in rows:
        # Skip rows that already exist in the new store (preserves
        # any newer registrations).
        if _cs.has(service, label):
            continue
        # Decrypt the legacy token_data (it's a JSON-serialized dict
        # of {access_token, scope, refresh_token, ...}).
        try:
            token_data = json.loads(decrypt(token_data_enc))
        except Exception as e:
            log.warning(
                "migrate_legacy_connections_db: skipping %s/%s (decrypt failed): %s",
                service, label, e,
            )
            continue
        # Re-encrypt into the new store as an OAuth credential.
        _cs.add(
            service=service,
            label=label,
            secret=json.dumps(token_data),
            auth_type="oauth",
        )
        migrated += 1

    _mark_migration_done()
    log.info("migrate_legacy_connections_db: migrated %d rows", migrated)
    return migrated


def _read_credential_store_safe() -> dict:
    """Read credential_store.json without forcing the credential_store
    module to be imported. Used by the migration's sentinel check.
    """
    from core.oauth import credential_store as _cs
    return _cs._read_storage()


def _mark_migration_done() -> None:
    """Set the _migrated sentinel in credential_store.json."""
    from core.oauth import credential_store as _cs
    storage = _cs._read_storage()
    storage["_migrated"] = True
    _cs._write_storage(storage)

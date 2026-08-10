import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timezone, timedelta

from cryptography.fernet import Fernet

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from core.database import DB_PATH


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

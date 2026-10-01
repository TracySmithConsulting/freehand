"""FreeHand-managed shared OAuth app registry (Round 8 tier-1b).

Methods FreeHand's broker calls: ``load_shared_apps()``.
Methods the CLI calls: ``add_shared_app(service, ...)``,
``remove_shared_app(service)``, ``list_shared_apps()``.

Storage: ``vault/broker_config.json -> shared_apps`` block — a sibling
top-level key to the existing ``providers`` block. The two blocks are
independent so the admin ``POST /api/broker/config`` endpoint (which
writes the ``providers`` block) does NOT clobber shared apps.

Security: ``client_secret`` is Fernet-encrypted via the same
``vault/encryption.key`` that protects ``api_key`` and per-connection
tokens. Encryption happens transparently on every write; the broker's
``load_shared_apps()`` decrypts on read so callers see plaintext.

Tier-extension note (Pitfall 5): tier-1b is more specific than tier-2/3/4
(env / broker / OC) but less specific than tier-1 (per-user override).
The broker resolution order remains user > shared_app > env > broker > oc > none.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

log = logging.getLogger(__name__)

_lock = threading.Lock()


def _broker_config_path() -> Path:
    """Path to broker_config.json — same as core/oauth/broker.py uses."""
    from core.agent_config import VAULT_DIR
    return VAULT_DIR / "broker_config.json"


def _read_config() -> dict:
    """Read broker_config.json. Returns empty dict if absent or unreadable.

    Never raises — callers (broker tier-1b path) treat read errors as
    "no shared apps configured" and fall through.
    """
    p = _broker_config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("shared_apps: failed to read broker_config.json: %s", e)
        return {}


def _write_config(config: dict) -> None:
    """Atomic write of broker_config.json. Caller holds the lock."""
    p = _broker_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2), encoding="utf-8")
    tmp.replace(p)


def _encrypt_secret(plaintext: str) -> str:
    """Fernet-encrypt via the same vault key used for connection tokens."""
    from core.oauth.manager import encrypt
    return encrypt(plaintext)


def _decrypt_secret(ciphertext: str) -> Optional[str]:
    """Fernet-decrypt. Returns None on any failure — caller treats as
    malformed entry, logs warning, falls through."""
    try:
        from core.oauth.manager import decrypt
        return decrypt(ciphertext)
    except Exception as e:
        log.warning("shared_apps: Fernet decrypt failed: %s", e)
        return None


def load_shared_apps() -> Dict[str, dict]:
    """Return ``{service: {client_id, client_secret, scopes, ...}}`` with
    ``client_secret`` DECRYPTED. Missing config or read errors -> empty dict.

    Called by ``broker.get_client_credentials()`` on every tier-1b lookup.
    """
    config = _read_config()
    raw = config.get("shared_apps", {})
    if not isinstance(raw, dict):
        log.warning("shared_apps: malformed block in broker_config.json (not a dict)")
        return {}

    out: Dict[str, dict] = {}
    for service, entry in raw.items():
        if not isinstance(entry, dict):
            log.warning("shared_apps: malformed entry for %s (not a dict)", service)
            continue
        encrypted = entry.get("client_secret_enc")
        if not encrypted:
            log.warning("shared_apps: missing client_secret_enc for %s", service)
            continue
        plain = _decrypt_secret(encrypted)
        if plain is None:
            continue
        out[service] = {
            "client_id": entry.get("client_id", ""),
            "client_secret": plain,
            "scopes": entry.get("scopes", []),
            "registered_by": entry.get("registered_by", ""),
            "registered_at": entry.get("registered_at", ""),
        }
    return out


def add_shared_app(
    service: str,
    client_id: str,
    client_secret: str,
    scopes: Optional[List[str]] = None,
    registered_by: str = "",
) -> dict:
    """Add or replace a shared app entry. Returns the new entry (encrypted on disk).

    Args:
        service: lowercased service id (e.g. "slack").
        client_id: OAuth client id from the developer portal.
        client_secret: OAuth client secret — Fernet-encrypted on disk.
        scopes: list of OAuth scopes requested by this app.
        registered_by: who registered the shared app (for audit trail).

    Raises:
        ValueError: if required fields are missing.
    """
    if not service or not client_id or not client_secret:
        raise ValueError("service, client_id, and client_secret are required")

    service = service.lower()
    scopes = scopes or []
    encrypted = _encrypt_secret(client_secret)

    with _lock:
        config = _read_config()
        shared = config.get("shared_apps", {})
        if not isinstance(shared, dict):
            shared = {}
        shared[service] = {
            "client_id": client_id,
            "client_secret_enc": encrypted,
            "scopes": scopes,
            "registered_by": registered_by,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
        config["shared_apps"] = shared
        _write_config(config)

    log.info("shared_apps: added shared app for %s (registered_by=%s)", service, registered_by)
    return {
        "service": service,
        "client_id": client_id,
        "scopes": scopes,
        "registered_by": registered_by,
        "registered_at": shared[service]["registered_at"],
    }


def remove_shared_app(service: str) -> bool:
    """Remove a shared app entry. Returns True if removed, False if not present."""
    service = service.lower()
    with _lock:
        config = _read_config()
        shared = config.get("shared_apps", {})
        if not isinstance(shared, dict) or service not in shared:
            return False
        del shared[service]
        config["shared_apps"] = shared
        _write_config(config)

    log.info("shared_apps: removed shared app for %s", service)
    return True


def list_shared_apps() -> List[dict]:
    """List all shared apps. SECRETS NEVER RETURNED — only client_id, scopes, and metadata.

    Used by the ``freehand shared-app list`` CLI subcommand. The encryption
    means there's no reason to surface the secret to a user; if they need
    to re-add, they paste a fresh secret from the provider portal.
    """
    out: List[dict] = []
    for service, entry in load_shared_apps().items():
        out.append({
            "service": service,
            "client_id": entry["client_id"],
            "scopes": entry.get("scopes", []),
            "registered_by": entry.get("registered_by", ""),
            "registered_at": entry.get("registered_at", ""),
        })
    out.sort(key=lambda r: r["service"])
    return out

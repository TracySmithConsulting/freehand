"""FreeHand OAuth broker (Option B from freehand-mcp-broker design).

Solves the "I just want to say Connect Google" friction.

Background:
  Each FreeHand user who wants to connect to Google / Microsoft / Zoom /
  Meta / GitHub currently needs to register their own OAuth application
  with each service, then paste their client_id + client_secret into
  settings.json. This is the chicken-and-egg blocker for non-developer
  users.

Option B (this module):
  FreeHand ships a built-in broker config that holds one canonical
  OAuth client_id/secret per service. If the user has not provided their
  own, the broker returns the FreeHand-shared values. If the user has
  provided their own, those win (user override).

  This is a "soft" broker — the FreeHand-shared credentials still come
  from somewhere. By default they're empty (no built-in broker); the
  administrator can populate them via either:
    1. Environment variables (FREEHAND_BROKER_<SERVICE>_CLIENT_ID, ...)
    2. A broker_config.json file next to settings.json

  We deliberately don't ship hardcoded client_ids. The administrator
  of each FreeHand installation is responsible for:
    - Registering the OAuth app with Google/Microsoft/etc.
    - Setting the client_id/secret in broker_config.json or env vars
    - Configuring the OAuth app's allowed redirect URIs to point at
      their FreeHand instance

This module is purely a lookup function. The connector code in
core/oauth/providers/* still reads credentials, but now calls
`broker.get_client_credentials(service)` instead of digging through
settings.json directly.

Resolution order (first wins):
  1. settings["oauth"]["providers"][service] — user-supplied override
  2. env vars FREEHAND_BROKER_<SERVICE>_CLIENT_ID/_CLIENT_SECRET
  3. broker_config.json next to settings.json
  4. None, None (caller treats as "no credentials configured")
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("freehand.broker")

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Where the broker config file lives. Default: next to settings.json
BROKER_CONFIG_FILENAME = "broker_config.json"

# Environment variable prefixes for shared broker credentials.
# Format: FREEHAND_BROKER_<SERVICE>_CLIENT_ID, _CLIENT_SECRET
ENV_PREFIX = "FREEHAND_BROKER_"

# Bound at import time so tests can patch `broker._load_user_settings`.
# We import the function and store it as a module attribute to make
# monkeypatching simple. The function lives in core.oauth.router.
from core.oauth.router import _load_oauth_settings as _load_user_settings


def _broker_config_path() -> Path:
    """Path to broker_config.json — sits next to settings.json."""
    from core.agent_config import VAULT_DIR
    return VAULT_DIR / BROKER_CONFIG_FILENAME


def _normalize_service(service: str) -> str:
    """Service names are lowercase in settings. Env vars use UPPERCASE."""
    return service.strip().lower()


def _upper_service(service: str) -> str:
    return _normalize_service(service).upper()


def _load_broker_config() -> dict:
    """Load broker_config.json if it exists, else return empty dict."""
    p = _broker_config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"Could not read {p}: {e}")
        return {}


def get_client_credentials(service: str) -> Tuple[Optional[str], Optional[str], str]:
    """Return (client_id, client_secret, source) for the given OAuth service.

    source is one of:
      - "user"     — user-supplied override in settings.json
      - "env"      — environment variable
      - "broker"   — broker_config.json
      - "none"     — not configured anywhere

    Always returns the user override first if both are set.

    Returns (None, None, "none") if no credentials are configured.
    Caller should treat that as "user must register their own OAuth app."
    """
    service = _normalize_service(service)

    # 1. User override (settings.json) — wins
    try:
        settings = _load_user_settings()
        providers = settings.get("oauth", {}).get("providers", {})
        p = providers.get(service, {})
        cid = p.get("client_id", "")
        sec = p.get("client_secret", "")
        if cid and sec:
            return cid, sec, "user"
    except Exception:
        # If settings can't be loaded (e.g. vault dir missing), fall through
        pass

    # 2. Environment variables
    env_cid = os.environ.get(f"{ENV_PREFIX}{_upper_service(service)}_CLIENT_ID", "")
    env_sec = os.environ.get(f"{ENV_PREFIX}{_upper_service(service)}_CLIENT_SECRET", "")
    if env_cid and env_sec:
        return env_cid, env_sec, "env"

    # 3. broker_config.json
    config = _load_broker_config()
    p = config.get("providers", {}).get(service, {})
    cid = p.get("client_id", "")
    sec = p.get("client_secret", "")
    if cid and sec:
        return cid, sec, "broker"

    return None, None, "none"


def broker_status() -> dict:
    """Diagnostic: which services have credentials, and where from.

    Returns a dict like:
      {
        "broker_config_path": "/path/to/broker_config.json",
        "broker_config_exists": true,
        "services": {
          "google": {"configured": true, "source": "broker"},
          "microsoft": {"configured": false, "source": "none"},
          ...
        },
      }
    """
    p = _broker_config_path()
    out = {
        "broker_config_path": str(p),
        "broker_config_exists": p.exists(),
        "services": {},
    }
    for service in ("google", "microsoft", "zoom", "facebook", "instagram", "github", "email"):
        cid, sec, source = get_client_credentials(service)
        out["services"][service] = {
            "configured": cid is not None and sec is not None,
            "source": source,
        }
    return out


def write_broker_config(providers: dict) -> dict:
    """Write broker_config.json with the given providers dict.

    Atomic write via tmp file + rename. Used by the admin endpoint
    POST /api/broker/config.

    Args:
        providers: dict like {"google": {"client_id": "...", "client_secret": "..."},
                              "microsoft": {...}}

    Returns the new broker_status() snapshot.
    """
    p = _broker_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"providers": providers}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(p)
    return broker_status()

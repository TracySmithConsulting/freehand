import json
import secrets
import sys
from pathlib import Path
from datetime import datetime, timezone

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import HTMLResponse

from core.oauth.manager import (
    save_connection,
    get_connection,
    list_connections,
    delete_connection,
)
from core.oauth.providers import get_connector

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

ALLOWED_SERVICES = [
    "google",
    "microsoft",
    "zoom",
    "facebook",
    "instagram",
    "github",
    "email",
]


def _load_oauth_settings() -> dict:
    settings_path = Path(__file__).parent.parent.parent / "vault" / "settings.json"
    if settings_path.exists():
        try:
            return json.loads(settings_path.read_text())
        except Exception:
            pass
    return {}


def _save_oauth_settings(data: dict) -> None:
    settings_path = Path(__file__).parent.parent.parent / "vault" / "settings.json"
    settings_path.write_text(json.dumps(data))


# ── Pending-state TTL sweep ────────────────────────────────────────────
# OAuth `state` parameters live in `settings.json` so they survive restarts.
# If the callback never completes (user closes tab, OAuth provider errors,
# network drops), the entry stays forever and the file grows unbounded.
# This sweep drops entries that are too old OR if the cap is exceeded.

PENDING_STATE_TTL_SECONDS = 3600      # 1 hour
PENDING_STATE_HARD_CAP = 50           # if more than this, drop oldest first


def _parse_iso(s: str):
    """Best-effort ISO-8601 parser. Returns timezone-aware UTC datetime or None.

    Tolerates:
    - Trailing Z (UTC)
    - Explicit offsets (+00:00, -05:00, etc.)
    - Naive timestamps (no tzinfo) — assumed to be UTC. This is needed
      for backwards-compat with entries written before C3 was deployed,
      which used `datetime.utcnow()` (naive).
    - Empty strings and garbage (returns None).
    """
    if not s:
        return None
    try:
        # Handle trailing Z
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Naive datetime — assume UTC (legacy data from utcnow() calls)
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _sweep_stale_pending_states(settings: dict) -> bool:
    """In-place sweep of `settings["oauth"]["pending_states"]`.

    Drops entries older than PENDING_STATE_TTL_SECONDS, and if more than
    PENDING_STATE_HARD_CAP remain, drops the oldest first.

    Returns True if any entries were removed (settings dict mutated).
    Returns False if nothing changed.

    NOTE: Does not persist. Caller decides whether to save.
    """
    pending = settings.get("oauth", {}).get("pending_states")
    if not pending:
        return False

    now = datetime.now(timezone.utc)
    mutated = False

    # 1) Drop expired entries
    expired = []
    for state_key, meta in pending.items():
        created = _parse_iso(meta.get("created_at", "")) if isinstance(meta, dict) else None
        if created is None:
            # No timestamp ⇒ treat as expired (safe default)
            expired.append(state_key)
            continue
        age = (now - created).total_seconds()
        if age > PENDING_STATE_TTL_SECONDS:
            expired.append(state_key)

    for k in expired:
        pending.pop(k, None)
        mutated = True

    # 2) If still over the cap, drop oldest first
    if len(pending) > PENDING_STATE_HARD_CAP:
        # Sort by created_at ascending; drop the oldest until under cap.
        sortable = []
        fallback_ts = datetime.min.replace(tzinfo=timezone.utc)
        for state_key, meta in pending.items():
            created = _parse_iso(meta.get("created_at", "")) if isinstance(meta, dict) else None
            sortable.append((created or fallback_ts, state_key))
        sortable.sort()
        # Drop the oldest len - cap entries
        to_drop = len(pending) - PENDING_STATE_HARD_CAP
        for _, state_key in sortable[:to_drop]:
            pending.pop(state_key, None)
            mutated = True

    return mutated


def _get_redirect_uri(service: str, request: Request) -> str:
    settings = _load_oauth_settings()
    redirect_uris = settings.get("oauth", {}).get("redirect_uris", {})
    stored = redirect_uris.get(service, "")
    current = f"{request.url.scheme}://{request.url.hostname}:{request.url.port}/api/integrations/callback/{service}"
    if stored and stored != current:
        if "oauth" not in settings:
            settings["oauth"] = {}
        if "redirect_uris" not in settings["oauth"]:
            settings["oauth"]["redirect_uris"] = {}
        settings["oauth"]["redirect_uris"][service] = current
        _save_oauth_settings(settings)
    return current or current


@router.get("")
async def list_integrations(request: Request):
    connections = list_connections()
    services = {}
    for conn in connections:
        svc = conn["service"]
        if svc not in services:
            services[svc] = {"service": svc, "connections": []}
        services[svc]["connections"].append({
            "label": conn["label"],
            "user_id": conn.get("user_id", ""),
            "connected_at": conn["connected_at"],
            "expires_at": conn.get("expires_at", ""),
            "scopes": conn["scopes"].split(",") if conn["scopes"] else [],
            "status": conn["status"],
        })
    return {
        "connections": list(services.values()),
        "available_services": ALLOWED_SERVICES,
    }


@router.get("/{service}/authorize")
async def get_authorize_url(service: str, label: str = "default", request: Request = None):
    if service not in ALLOWED_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")
    if label not in ("default", "work", "personal"):
        if len(label) > 50:
            raise HTTPException(status_code=400, detail="Label too long (max 50 chars)")

    connector = get_connector(service)
    if not connector:
        raise HTTPException(status_code=501, detail=f"Connector not implemented: {service}")

    redirect_uri = _get_redirect_uri(service, request)
    state = secrets.token_urlsafe(32)
    stored_state = json.dumps({"state": state, "service": service, "label": label})

    auth_url = connector.authorize_url(state=stored_state, redirect_uri=redirect_uri)

    settings = _load_oauth_settings()
    if "oauth" not in settings:
        settings["oauth"] = {}
    if "pending_states" not in settings["oauth"]:
        settings["oauth"]["pending_states"] = {}

    # C3 fix: sweep stale entries before adding a new one. Prevents
    # unbounded growth of settings.json when callbacks never complete.
    if _sweep_stale_pending_states(settings):
        # Sweep removed some entries — persist the cleanup.
        _save_oauth_settings(settings)

    settings["oauth"]["pending_states"][state] = {
        "service": service,
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_oauth_settings(settings)

    return {"authorize_url": auth_url, "state": state, "label": label}


@router.get("/{service}/callback")
async def handle_callback(
    service: str,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    error_description: str = Query(default=""),
    request: Request = None,
):
    if error:
        return HTMLResponse(
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Auth Failed</title>
            <style>body{{font-family:sans-serif;padding:2rem;background:#f5f5f5;}}
            .box{{background:#fff;border-radius:8px;padding:2rem;max-width:500px;margin:0 auto;
            box-shadow:0 1px 3px rgba(0,0,0,0.1);}}h2{{color:#c0392b;}}</style>
            </head><body><div class="box"><h2>Authorization Failed</h2>
            <p>{error_description or error}</p>
            <p><a href="/#connections" style="color:#0f3460;">Back to FreeHand</a></p>
            </div></body></html>""",
            status_code=400,
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state parameter")

    settings = _load_oauth_settings()
    pending = settings.get("oauth", {}).get("pending_states", {})

    # C3 fix: sweep stale entries before lookup. If sweep mutated settings,
    # persist and reload pending so the lookup below sees the cleaned dict.
    if _sweep_stale_pending_states(settings):
        _save_oauth_settings(settings)
        pending = settings.get("oauth", {}).get("pending_states", {})

    pending_state = pending.get(state)
    if not pending_state:
        raise HTTPException(status_code=400, detail="Invalid or expired state parameter")

    # C3 fix: enforce TTL on the looked-up state even if sweep missed it
    # (e.g. clock skew between sweep and lookup). Reject states older than
    # the TTL — they shouldn't be honoured.
    created = _parse_iso(pending_state.get("created_at", ""))
    if created is not None:
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age > PENDING_STATE_TTL_SECONDS:
            # Drop and reject
            pending.pop(state, None)
            _save_oauth_settings(settings)
            raise HTTPException(status_code=400, detail="State parameter expired")

    service_from_state = pending_state.get("service")
    label = pending_state.get("label", "default")

    if service_from_state != service:
        raise HTTPException(status_code=400, detail="Service mismatch in state parameter")

    redirect_uri = _get_redirect_uri(service, request)

    connector = get_connector(service)
    if not connector:
        raise HTTPException(status_code=501, detail=f"Connector not implemented: {service}")

    try:
        token_data = await connector.handle_callback(code, state, redirect_uri)
    except Exception as e:
        msg = connector.parse_error(400, {"error": str(e)}) if hasattr(connector, "parse_error") else str(e)
        return HTMLResponse(
            f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Auth Error</title>
            <style>body{{font-family:sans-serif;padding:2rem;background:#f5f5f5;}}
            .box{{background:#fff;border-radius:8px;padding:2rem;max-width:500px;margin:0 auto;
            box-shadow:0 1px 3px rgba(0,0,0,0.1);}}h2{{color:#c0392b;}}</style>
            </head><body><div class="box"><h2>Connection Failed</h2>
            <p>{msg}</p>
            <p><a href="/#connections" style="color:#0f3460;">Back to FreeHand</a></p>
            </div></body></html>""",
            status_code=400,
        )

    scopes = connector.scopes_read + connector.scopes_write
    conn_id = save_connection(service, label, token_data, scopes)

    settings = _load_oauth_settings()
    if "oauth" in settings and "pending_states" in settings["oauth"]:
        settings["oauth"]["pending_states"].pop(state, None)
        _save_oauth_settings(settings)

    return HTMLResponse(
        f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Connected!</title>
        <style>body{{font-family:sans-serif;padding:2rem;background:#f5f5f5;}}
        .box{{background:#fff;border-radius:8px;padding:2rem;max-width:500px;margin:0 auto;
        box-shadow:0 1px 3px rgba(0,0,0,0.1);}}h2{{color:#1a8c4a;}}</style>
        </head><body><div class="box"><h2>Connected!</h2>
        <p>You have successfully connected your {connector.name} account.</p>
        <p>Label: <strong>{label}</strong></p>
        <p><a href="/#connections" style="color:#0f3460;">Back to FreeHand</a></p>
        </div><script>setTimeout(function(){{window.close()}}, 2000);</script>
        </body></html>""",
        status_code=200,
    )


@router.post("/{service}/disconnect")
async def disconnect(service: str, label: str = "default"):
    if service not in ALLOWED_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")
    deleted = delete_connection(service, label)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No connection found: {service} ({label})")
    return {"status": "disconnected", "service": service, "label": label}


@router.post("/{service}/test")
async def test_connection(service: str, label: str = "default"):
    if service not in ALLOWED_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")
    conn = get_connection(service, label)
    if not conn:
        raise HTTPException(status_code=404, detail=f"Not connected: {service} ({label})")

    connector = get_connector(service)
    if not connector:
        raise HTTPException(status_code=501, detail=f"Connector not implemented: {service}")

    try:
        valid = connector.test(conn["token_data"])
        return {"service": service, "label": label, "valid": valid}
    except Exception as e:
        return {"service": service, "label": label, "valid": False, "error": str(e)}


@router.get("/{service}/status")
async def get_service_status(service: str, label: str = "default"):
    """Return detailed connection status for display in the UI."""
    if service not in ALLOWED_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")
    from core.oauth.manager import get_connection_status
    return get_connection_status(service, label)


@router.get("/{service}/settings")
async def get_service_settings(service: str):
    if service not in ALLOWED_SERVICES:
        raise HTTPException(status_code=404, detail=f"Unknown service: {service}")
    settings = _load_oauth_settings()
    oauth = settings.get("oauth", {})
    providers = oauth.get("providers", {})
    if service == "meta":
        result = {
            "facebook": providers.get("facebook", {}),
            "instagram": providers.get("instagram", {}),
        }
    else:
        result = providers.get(service, {})
    masked = {}
    for k, v in result.items():
        if isinstance(v, str) and len(v) > 8 and any(s in k.lower() for s in ("secret", "key", "token")):
            masked[k] = v[:4] + "..." + v[-4:]
        else:
            masked[k] = v
    return {"service": service, "settings": masked}

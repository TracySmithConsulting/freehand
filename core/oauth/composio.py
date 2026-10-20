"""Small Composio Connect Link adapter.

Composio owns the provider OAuth credentials and the user's provider tokens.
FreeHand only keeps the Composio connected-account id and a local label, so
the normal local installation never handles a Facebook or Instagram token.

Configuration:

* ``COMPOSIO_API_KEY``
* ``COMPOSIO_AUTH_CONFIG_FACEBOOK``
* ``COMPOSIO_AUTH_CONFIG_INSTAGRAM``
* ``COMPOSIO_BASE_URL`` (optional, defaults to Composio's API)

The auth-config ids are deliberately explicit. They are project-specific and
must not be guessed from a toolkit name. A future hosted FreeHand service can
replace ``local_user_id()`` with its authenticated application user id.
"""
from __future__ import annotations

import json
import os
import uuid
from urllib.parse import quote, urlencode
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp


def _load_project_env() -> None:
    """Load the project's simple .env file without adding a dependency.

    Explicit process environment variables win, which keeps Docker, CI, and
    hosted deployments in control of their own configuration.
    """
    env_path = Path(__file__).parent.parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


_load_project_env()

BASE_URL = os.environ.get("COMPOSIO_BASE_URL", "https://backend.composio.dev").rstrip("/")
VAULT_DIR = Path(__file__).parent.parent.parent / "vault"
IDENTITY_PATH = VAULT_DIR / "composio_identity.json"
ACCOUNTS_PATH = VAULT_DIR / "composio_accounts.json"

SERVICE_AUTH_CONFIG_ENV = {
    "facebook": "COMPOSIO_AUTH_CONFIG_FACEBOOK",
    "instagram": "COMPOSIO_AUTH_CONFIG_INSTAGRAM",
}


class ComposioError(RuntimeError):
    """A safe, user-facing Composio adapter error."""


def _api_key() -> str:
    return os.environ.get("COMPOSIO_API_KEY", "").strip()


def auth_config_id(service: str) -> str:
    env_name = SERVICE_AUTH_CONFIG_ENV.get(service.lower())
    return os.environ.get(env_name, "").strip() if env_name else ""


def is_configured(service: str) -> bool:
    """Return whether Composio can start auth for this service."""
    return bool(_api_key() and auth_config_id(service))


def configured_services() -> list[str]:
    return [service for service in SERVICE_AUTH_CONFIG_ENV if is_configured(service)]


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def local_user_id() -> str:
    """Return a stable anonymous user id for one local FreeHand install."""
    identity = _read_json(IDENTITY_PATH, {})
    existing = identity.get("user_id") if isinstance(identity, dict) else ""
    if isinstance(existing, str) and existing:
        return existing

    user_id = f"freehand-local-{uuid.uuid4().hex}"
    _write_json(IDENTITY_PATH, {"user_id": user_id})
    return user_id


def _account_records() -> list[dict]:
    data = _read_json(ACCOUNTS_PATH, {"accounts": []})
    records = data.get("accounts", []) if isinstance(data, dict) else []
    return records if isinstance(records, list) else []


def _save_account_records(records: list[dict]) -> None:
    _write_json(ACCOUNTS_PATH, {"version": 1, "accounts": records})


def remember_account(
    service: str,
    label: str,
    user_id: str,
    connected_account_id: str,
    status: str = "active",
) -> None:
    """Persist non-secret Composio account metadata for the local UI."""
    records = _account_records()
    record = {
        "service": service.lower(),
        "label": label,
        "user_id": user_id,
        "connected_account_id": connected_account_id,
        "status": status,
        "connected_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": "",
        "scopes": [],
    }
    records = [
        item for item in records
        if not (
            item.get("service") == record["service"]
            and item.get("label") == label
        )
    ]
    records.append(record)
    _save_account_records(records)


def list_accounts(service: Optional[str] = None) -> list[dict]:
    records = _account_records()
    if service:
        records = [item for item in records if item.get("service") == service.lower()]
    return records


def get_account(service: str, label: str = "default") -> Optional[dict]:
    for item in list_accounts(service):
        if item.get("label") == label:
            return item
    return None


def remove_account(service: str, label: str = "default") -> Optional[dict]:
    records = _account_records()
    found = get_account(service, label)
    if found is None:
        return None
    _save_account_records([
        item for item in records
        if not (
            item.get("service") == service.lower()
            and item.get("label") == label
        )
    ])
    return found


async def _request_json(method: str, path: str, payload: Optional[dict] = None) -> dict:
    api_key = _api_key()
    if not api_key:
        raise ComposioError("Composio is not configured: COMPOSIO_API_KEY is missing")

    headers = {"x-api-key": api_key, "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method,
                f"{BASE_URL}{path}",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as response:
                raw = await response.text()
                try:
                    data = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    data = {}
                if response.status >= 400:
                    message = "Composio request failed"
                    if isinstance(data, dict):
                        error = data.get("error")
                        if isinstance(error, dict):
                            message = error.get("message") or message
                        elif isinstance(error, str):
                            message = error
                    raise ComposioError(f"{message} (HTTP {response.status})")
                return data if isinstance(data, dict) else {}
    except ComposioError:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        raise ComposioError(f"Could not reach Composio: {exc}") from exc


async def create_connect_link(
    service: str,
    label: str,
    callback_url: str,
) -> dict:
    """Create a Composio hosted auth link for one local account."""
    service = service.lower()
    config_id = auth_config_id(service)
    if not _api_key() or not config_id:
        raise ComposioError(
            f"Composio is not configured for {service}. Set COMPOSIO_API_KEY "
            f"and {SERVICE_AUTH_CONFIG_ENV.get(service, 'the service auth config')} first."
        )

    user_id = local_user_id()
    # Composio aliases are unique within an entity. A browser that is closed
    # midway leaves an INITIATED connected account behind, so retrying with
    # the same visible FreeHand label would otherwise return "alias already
    # in use". Keep the FreeHand label stable while giving each retry a fresh
    # Composio-side alias. Completed accounts are still tracked by their
    # connected-account id and local label.
    existing = get_account(service, label)
    alias = label
    if existing and existing.get("status") in ("initiated", "active"):
        alias = f"{label}-{uuid.uuid4().hex[:10]}"
    data = await _request_json(
        "POST",
        "/api/v3.1/connected_accounts/link",
        {
            "auth_config_id": config_id,
            "user_id": user_id,
            "alias": alias,
            "callback_url": callback_url,
        },
    )
    redirect_url = data.get("redirect_url")
    if not isinstance(redirect_url, str) or not redirect_url:
        raise ComposioError("Composio returned no redirect URL")

    connected_account_id = data.get("connected_account_id")
    if isinstance(connected_account_id, str) and connected_account_id:
        remember_account(service, label, user_id, connected_account_id, "initiated")
    return data


async def get_connected_account(connected_account_id: str) -> dict:
    return await _request_json(
        "GET",
        f"/api/v3.1/connected_accounts/{connected_account_id}",
    )


async def delete_connected_account(connected_account_id: str) -> dict:
    return await _request_json(
        "DELETE",
        f"/api/v3.1/connected_accounts/{connected_account_id}",
    )


async def list_tools(service: str, limit: int = 1000) -> list[dict]:
    """Return the current Composio action catalogue for a toolkit.

    Composio calls providers ``toolkits`` in its tools API.  The service
    values used by FreeHand (``facebook`` and ``instagram``) are already the
    toolkit slugs, so no provider-specific translation is needed here.
    """
    query = urlencode({
        "toolkit_slug": service.lower(),
        "toolkit_versions": "latest",
        "limit": max(1, min(int(limit), 1000)),
    })
    data = await _request_json("GET", f"/api/v3/tools?{query}")
    items = data.get("items", data.get("tools", data.get("data", [])))
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


async def execute_tool(
    tool_slug: str,
    connected_account_id: str,
    arguments: Optional[dict] = None,
    user_id: str = "",
) -> dict:
    """Execute one Composio action using a stored connected account."""
    payload = {
        "connected_account_id": connected_account_id,
        "version": "latest",
        "arguments": arguments or {},
    }
    if user_id:
        payload["user_id"] = user_id
    return await _request_json(
        "POST",
        f"/api/v3.1/tools/execute/{quote(tool_slug, safe='')}",
        payload,
    )

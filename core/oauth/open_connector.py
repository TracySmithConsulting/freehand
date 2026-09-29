"""OpenConnector fallback broker client (Round 7).

Methods the FreeHand broker calls: ``is_available()``,
``service_is_known(service_id)``, ``call_mcp_action(action, arguments)``.
Results are cached in-process with a 30-second TTL.

Never raises into the broker: returns False or None on any transport
error, which broker treats as ``tier-5 silent-no-op``.

Configuration (all optional; default behaviour matches the OpenConnector
dev defaults):

- ``OOMOL_CONNECT_BASE_URL`` — defaults to ``http://127.0.0.1:3000``.
- ``OOMOL_CONNECT_RUNTIME_TOKEN`` — defaults to empty; if unset, tier-5
  advertises as down (cannot call the runtime without a token).
- ``OOMOL_CONNECT_HEALTH_TIMEOUT_SECONDS`` — defaults to 1.5.
- ``OOMOL_CONNECT_CACHE_TTL_SECONDS`` — defaults to 30.

Auth scope (verified empirically against open-connector@main, 28 Sep 2026):
``/v1/*`` and ``/mcp`` need the **runtime** token, NOT the admin token.
FreeHand persists the runtime token in ``vault/open_connector_runtime_token``
(envelope-encrypted alongside ``vault/api_key``); this module reads it via
``_load_runtime_token()`` at import time.

Accept header for MCP: must include ``application/json, text/event-stream`` —
otherwise the runtime returns 406 Not Acceptable (MCP-canonical negotiation).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional

import urllib.error
import urllib.request

log = logging.getLogger("freehand.open_connector")

_BASE_URL = os.environ.get("OOMOL_CONNECT_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
_HEALTH_TIMEOUT = float(os.environ.get("OOMOL_CONNECT_HEALTH_TIMEOUT_SECONDS", "1.5"))
_CACHE_TTL = float(os.environ.get("OOMOL_CONNECT_CACHE_TTL_SECONDS", "30"))

_MCP_ACCEPT = "application/json, text/event-stream"

# In-process cache. Keyed by string. Value: (timestamp, payload).
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}

_runtime_token: Optional[str] = None


def _load_runtime_token() -> Optional[str]:
    """Read the bootstrap runtime token if one has been persisted.

    Round 7 stub: env-var only. Round 7 Task 5 will replace this with a
    Fernet-encrypted read from ``vault/open_connector_runtime_token``.
    Returning ``None`` here short-circuits tier-5 to ``none`` until the
    token persistence lands.
    """
    global _runtime_token
    if _runtime_token is None:
        env = os.environ.get("OOMOL_CONNECT_RUNTIME_TOKEN", "")
        _runtime_token = env if env else None
    return _runtime_token


def _cached(key: str, loader) -> Any:
    """Return loader()'s result, cached for ``_CACHE_TTL`` seconds."""
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and now - entry[0] < _CACHE_TTL:
            return entry[1]
    # Drop the lock while loading so concurrent callers don't serialise.
    try:
        value = loader()
    except Exception as e:  # last-line defence — caller treats False/None.
        log.debug("open-connector loader %s failed: %s", key, e)
        value = None
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _reset_cache_for_tests() -> None:
    """Clear the cache so tests don't leak state between calls."""
    with _cache_lock:
        _cache.clear()


def is_available() -> bool:
    """Return True iff the OpenConnector runtime responds on /v1/health.

    Cached for 30s. Returns False on any transport error or 401 (token
    issue is a deployment bug, but tier-5 should never raise into the
    broker — silent no-op is the right default).
    """
    token = _load_runtime_token()
    if not token:
        return False

    def _check() -> bool:
        req = urllib.request.Request(
            f"{_BASE_URL}/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=_HEALTH_TIMEOUT) as resp:
                if resp.status != 200:
                    return False
                body = resp.read().decode("utf-8", "replace")
                data = json.loads(body)
                return bool(data.get("data", {}).get("ok"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
            log.debug("open-connector health check failed: %s", e)
            return False

    return bool(_cached("availability", _check))


def _list_services_raw() -> Optional[list]:
    """GET /v1/providers — full provider catalog. Used by service_is_known."""
    token = _load_runtime_token()
    if not token:
        return None

    def _loader() -> Optional[list]:
        req = urllib.request.Request(
            f"{_BASE_URL}/v1/providers",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status != 200:
                    return None
                body = resp.read().decode("utf-8", "replace")
                data = json.loads(body)
                svc_list = data.get("data")
                return svc_list if isinstance(svc_list, list) else None
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
            log.debug("open-connector list_services failed: %s", e)
            return None

    return _cached("providers", _loader)


def service_is_known(service: str) -> bool:
    """Return True iff ``service`` is in OpenConnector's provider catalog.

    Service ids are lowercase (e.g. ``slack``, ``notion``, ``github``).
    The HTTP call fetches the full provider catalog and caches it for
    30s, so the broker can resolve thousands of services cheaply.
    """
    if not service:
        return False
    services = _list_services_raw()
    if not services:
        return False
    needle = service.strip().lower()
    return any(
        isinstance(s, dict) and isinstance(s.get("id"), str) and s["id"] == needle
        for s in services
    )


def call_mcp_action(action: str, arguments: dict) -> Optional[dict]:
    """Helper for future Task 5 — FreeHand executing an MCP tool over the wire.

    Not used by the broker tier-5 (which only asks "does OC know this
    service?"). Kept here as the canonical example of how FreeHand talks
    to OpenConnector. Returns the parsed JSON envelope on success, None
    on any error.
    """
    token = _load_runtime_token()
    if not token:
        return None
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": action, "arguments": arguments},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{_BASE_URL}/mcp",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": _MCP_ACCEPT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if resp.status != 200:
                return None
            # Server returns text/event-stream: "event: message\ndata: {...}"
            for line in raw.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[len("data:"):].strip())
            return None
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, OSError) as e:
        log.debug("open-connector mcp call %s failed: %s", action, e)
        return None

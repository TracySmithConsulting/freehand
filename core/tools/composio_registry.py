"""Dynamic Composio action discovery for FreeHand.

This intentionally lives beside, rather than inside, the OpenConnector
registry.  The two backends have different action metadata and credentials,
but expose the same OpenAI function schema to the agent.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

VAULT_DIR = Path(__file__).parent.parent.parent / "vault"
STORAGE_PATH = VAULT_DIR / "composio_tool_registry.json"
_lock = threading.Lock()


def _read_storage() -> dict:
    if not STORAGE_PATH.exists():
        return {"version": 1, "tools": []}
    try:
        data = json.loads(STORAGE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"version": 1, "tools": []}
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "tools": []}


def _write_storage(data: dict) -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORAGE_PATH.with_suffix(STORAGE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(STORAGE_PATH)


def _slug_part(value: str) -> str:
    part = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_")
    return part or "tool"


def build_tool_name(service: str, label: str, tool_slug: str) -> str:
    """Create a stable, OpenAI-compatible FreeHand tool name."""
    base = f"co_{_slug_part(service.lower())}_{_slug_part(label.lower())}_{_slug_part(tool_slug)}"
    if len(base) <= 64:
        return base
    digest = hashlib.sha1(tool_slug.encode("utf-8")).hexdigest()[:8]
    return f"{base[:55]}_{digest}"


def _json_type(value: Any) -> str:
    return value if value in {"string", "number", "integer", "boolean", "array", "object"} else "string"


def _parameter_schema(item: Any) -> dict:
    if not isinstance(item, dict):
        return {"type": "string"}
    prop = {"type": _json_type(item.get("type", "string"))}
    for key in ("description", "default", "enum"):
        if key in item and item[key] is not None:
            prop[key] = item[key]
    if isinstance(item.get("items"), dict):
        prop["items"] = item["items"]
    return prop


def _input_schema(tool: dict) -> tuple[dict, list[str]]:
    raw = tool.get("input_parameters", tool.get("inputParameters", tool.get("parameters", {})))
    if isinstance(raw, dict) and raw.get("type") == "object" and isinstance(raw.get("properties"), dict):
        return dict(raw.get("properties", {})), list(raw.get("required", []) or [])
    if isinstance(raw, dict):
        properties = {}
        required = []
        for name, spec in raw.items():
            if not isinstance(name, str):
                continue
            properties[name] = _parameter_schema(spec)
            if isinstance(spec, dict) and spec.get("required") is True:
                required.append(name)
        return properties, required
    return {}, []


def classify_tool(tool: dict) -> str:
    """Classify Composio actions conservatively from their stable slug."""
    slug = str(tool.get("slug", tool.get("name", ""))).upper()
    if any(word in slug for word in ("DELETE", "REMOVE", "REVOKE", "DESTROY", "PURGE", "CLEAR")):
        return "destructive"
    if any(slug.startswith(prefix) or f"_{prefix}" in slug for prefix in (
        "GET", "LIST", "SEARCH", "FETCH", "FIND", "CHECK", "VERIFY", "LOOKUP", "READ", "DESCRIBE", "VIEW"
    )):
        return "standard"
    return "write"


def build_tool_schema(service: str, label: str, tool: dict) -> dict:
    slug = str(tool.get("slug", tool.get("name", ""))).strip()
    name = str(tool.get("name", slug)).strip() or slug
    description = tool.get("description") or tool.get("human_description") or "Composio action"
    properties, required = _input_schema(tool)
    tool_name = build_tool_name(service, label, slug)
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": f"{name}: {description}",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def _merge(items: list[dict]) -> None:
    from core import agent_config
    for item in items:
        schema = item.get("schema", item) if isinstance(item, dict) else {}
        fn = schema["function"]
        name = fn["name"]
        agent_config.TOOL_SCHEMAS[name] = {
            "description": fn["description"],
            "parameters": fn["parameters"],
        }
        agent_config.TOOL_REGISTRY[name] = "read" if item.get("risk", "standard") == "standard" else "write"


def _unmerge(schemas: list[dict]) -> None:
    from core import agent_config
    for schema in schemas:
        name = schema.get("function", {}).get("name")
        if name:
            agent_config.TOOL_SCHEMAS.pop(name, None)
            agent_config.TOOL_REGISTRY.pop(name, None)


async def discover_tools_async(service: str, label: str = "default") -> dict:
    from core.oauth import composio
    account = composio.get_account(service, label)
    if not account or account.get("status") not in ("active", "connected", "enabled"):
        return {"registered": [], "pending_writes": []}

    tools = await composio.list_tools(service)
    previous = {
        t.get("composio_tool_slug"): t
        for t in _read_storage().get("tools", [])
        if t.get("service") == service.lower() and t.get("label") == label
    }
    records = []
    registered = []
    pending = []
    for tool in tools:
        slug = str(tool.get("slug", tool.get("name", ""))).strip()
        if not slug:
            continue
        risk = classify_tool(tool)
        schema = build_tool_schema(service, label, tool)
        old = previous.get(slug, {})
        enabled = bool(old.get("enabled")) if old else risk == "standard"
        record = {
            "service": service.lower(),
            "label": label,
            "tool_name": schema["function"]["name"],
            "schema": schema,
            "risk": risk,
            "composio_tool_slug": slug,
            "enabled": enabled,
        }
        records.append(record)
        if enabled:
            registered.append(record)
        elif risk != "standard":
            pending.append(record)

    storage = _read_storage()
    storage["tools"] = [
        t for t in storage.get("tools", [])
        if not (t.get("service") == service.lower() and t.get("label") == label)
    ] + records
    with _lock:
        _write_storage(storage)
    _unmerge([t.get("schema", {}) for t in previous.values() if isinstance(t.get("schema"), dict)])
    _merge(registered)
    return {"registered": registered, "pending_writes": pending}


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result = []
    error = []
    def runner():
        try:
            result.append(asyncio.run(coro))
        except Exception as exc:
            error.append(exc)
    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0]


def discover_tools(service: str, label: str = "default") -> dict:
    try:
        return _run_async(discover_tools_async(service, label))
    except Exception as exc:
        log.warning("composio: discovery failed for %s/%s: %s", service, label, exc)
        return {"registered": [], "pending_writes": []}


def list_tools(service: Optional[str] = None, label: Optional[str] = None) -> list[dict]:
    out = []
    for record in _read_storage().get("tools", []):
        if not record.get("enabled"):
            continue
        if service and record.get("service") != service.lower():
            continue
        if label and record.get("label") != label:
            continue
        out.append(record)
    return out


def has_account(service: str, label: str = "default") -> bool:
    from core.oauth import composio
    return composio.get_account(service, label) is not None


def pending_writes_summary(service: str, label: str = "default") -> tuple[int, int]:
    writes = destructive = 0
    for record in _read_storage().get("tools", []):
        if record.get("service") != service.lower() or record.get("label") != label or record.get("enabled"):
            continue
        if record.get("risk") == "destructive":
            destructive += 1
        else:
            writes += 1
    return writes, destructive


def enable_writes(service: str, label: str = "default") -> int:
    storage = _read_storage()
    promoted = []
    count = 0
    for record in storage.get("tools", []):
        if record.get("service") == service.lower() and record.get("label") == label and not record.get("enabled"):
            record["enabled"] = True
            promoted.append(record["schema"])
            count += 1
    if count:
        _write_storage(storage)
        _merge([record for record in storage.get("tools", []) if record.get("service") == service.lower() and record.get("label") == label and record.get("enabled")])
    return count


def disable(service: str, label: str = "default") -> int:
    storage = _read_storage()
    removed = [t for t in storage.get("tools", []) if t.get("service") == service.lower() and t.get("label") == label]
    if not removed:
        return 0
    storage["tools"] = [t for t in storage.get("tools", []) if t not in removed]
    _write_storage(storage)
    _unmerge([t.get("schema", {}) for t in removed])
    return len(removed)


def is_destructive_tool(tool_name: str) -> bool:
    return any(
        t.get("tool_name") == tool_name and t.get("risk") == "destructive"
        for t in _read_storage().get("tools", [])
    )


def bootstrap() -> int:
    active = [t for t in _read_storage().get("tools", []) if t.get("enabled")]
    _merge([t for t in active if isinstance(t.get("schema"), dict)])
    return len(active)


def refresh() -> dict:
    from core.oauth import composio
    added = removed = updated = 0
    accounts = composio.list_accounts()
    known = set()
    for account in accounts:
        service, label = account.get("service"), account.get("label", "default")
        result = discover_tools(service, label)
        known.add((service, label))
        active = len(result["registered"])
        if active:
            updated += 1
    storage = _read_storage()
    stale = [t for t in storage.get("tools", []) if (t.get("service"), t.get("label")) not in known]
    if stale:
        storage["tools"] = [t for t in storage.get("tools", []) if t not in stale]
        _write_storage(storage)
        _unmerge([t.get("schema", {}) for t in stale])
        removed = len(stale)
    return {"added": added, "removed": removed, "updated": updated}

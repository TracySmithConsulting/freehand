"""Tool discovery + registration layer (Round 10 PR 2).

This module is the bridge between OpenConnector's action catalog and
FreeHand's LLM-facing tool list. Probing OC for a service, classifying
actions by risk, and registering them under the ``oc_<service>_<label>_<action>``
naming convention.

Design constraints (Tracy 02 Oct 2026, refined from live OC probe):

1. **Classification rule**: only ``risk == "standard"`` actions are
   auto-registered as read tools. ``risk in {"sensitive","destructive"}``
   stays behind ``freehand tools enable-writes <service>``.
   - Slack's ``defaultSelected`` is OC's UX signal for the consent
     screen, NOT a safety signal. Slack uses ``risk=sensitive`` for
     read actions like ``channels:history``. We don't auto-register
     those.

2. **Tool naming**: ``oc_<service>_<label>_<action>`` always, even for
   single-credential services (label='default' is implicit). One
   uniform shape for the LLM.

3. **Schema format**: OpenAI function-calling shape — matches
   ``core/agent_config.TOOL_SCHEMAS``. No translation layer needed
   for the dispatch loop.

Persistence:
- ``vault/tool_registry.json`` holds the currently-enabled tools.
  Each tool is keyed by ``(service, label, action_id)`` and stores
  the schema dict. Lock-protected writes.
- On server boot, the registry reads this file and merges its tools
  into ``core.agent_config.TOOL_SCHEMAS`` so ``list_available_tools()``
  picks them up automatically.

Multi-credential support:
- Per-(service, label) registration. Two GitHub accounts under
  labels "work" and "personal" produce ``oc_github_work_*`` and
  ``oc_github_personal_*`` tools, all co-existing.

Methods FreeHand's CLI + server call:
- ``discover_tools(service, label) -> dict`` — probe OC for one
  service/label pair. Auto-registers standard-risk tools; pending
  list holds sensitive/destructive.
- ``enable_writes(service, label=None) -> int`` — promote pending
  to active. Returns count promoted.
- ``disable(service, label=None) -> int`` — remove ALL tools for
  the service/label. Returns count removed.
- ``refresh() -> dict`` — re-probe OC for every credentialed
  service/label. Returns ``{added, removed, updated}``.
- ``list_tools(service=None, label=None) -> List[dict]`` — query
  currently-active tools. Used by ``freehand tools list``.
- ``bootstrap() -> int`` — read vault/tool_registry.json and merge
  into the global TOOL_SCHEMAS / TOOL_REGISTRY dicts. Called once
  at server startup. Returns count of tools merged.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# Vault layout — same shape as shared_apps.py / credential_store.py.
# Tests monkeypatch VAULT_DIR + STORAGE_PATH to redirect to tmp dirs.
VAULT_DIR = Path(__file__).parent.parent.parent / "vault"
STORAGE_PATH = VAULT_DIR / "tool_registry.json"

_lock = threading.Lock()


def _ensure_sys_path():
    """Late-bind sys.path so this module works when imported via tests/*."""
    if str(Path(__file__).parent.parent.parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ── Action classification ───────────────────────────────────────────────


def classify_action(action: dict) -> str:
    """Classify an OC authorizationOption as 'read' or 'write'.

    Returns 'read' for risk=standard. Returns 'write' for risk in
    {sensitive, destructive}, AND for any unrecognized risk value
    (safe-by-default — unknown risks stay behind enable-writes).
    """
    risk = action.get("risk", "")
    if risk == "standard":
        return "read"
    # sensitive, destructive, or unknown → write (gated)
    return "write"


# ── Tool naming ────────────────────────────────────────────────────────


def build_tool_name(service: str, label: str, action_id: str) -> str:
    """Build the canonical tool name.

    Format: oc_<service>_<label>_<action_id>

    Examples:
        oc_slack_default_channels:read
        oc_github_work_repo

    Service and label are lowercased; action_id preserves its case
    (which usually includes colons like Slack's ``channels:history``).
    """
    return f"oc_{service.lower()}_{label.lower()}_{action_id}"


# ── Schema generation ──────────────────────────────────────────────────


def _json_schema_type(oc_type: str) -> str:
    """Map OC's input field type strings to JSON Schema types.

    OC uses shorthand like 'string', 'number', 'integer', 'boolean',
    'array', 'object'. JSON Schema is the same set — pass through.
    Unknown types default to 'string' (safe fallback).
    """
    if oc_type in ("string", "number", "integer", "boolean", "array", "object"):
        return oc_type
    return "string"


def build_tool_schema(service: str, label: str, action: dict) -> dict:
    """Build an OpenAI function-calling schema for one OC action.

    Schema shape (matches existing core/agent_config.TOOL_SCHEMAS):

        {
          "type": "function",
          "function": {
            "name": "oc_<service>_<label>_<action_id>",
            "description": "<action.name>: <action.description>",
            "parameters": {
              "type": "object",
              "properties": {...},
              "required": [...]  # Pitfall 26: always present, even []
            }
          }
        }

    Round 13: action id (e.g. ``slack.list_channels``) is used as-is,
    with ``.`` replaced by ``_`` for tool-naming compatibility
    (``oc_slack_tracy_list_channels``).

    If OC provides ``inputSchema`` (JSON Schema object), it populates
    ``parameters.properties`` and ``parameters.required``. Otherwise
    parameters is empty (just ``type`` and ``required``).
    """
    # Round 13 uses service.action_name format. Replace "." with "_"
    # for tool-naming (OpenAI function names don't allow dots).
    raw_action_id = action["id"]
    # Strip the "<service>." prefix since we already include service+label.
    if "." in raw_action_id:
        action_id_only = raw_action_id.split(".", 1)[1]
    else:
        action_id_only = raw_action_id

    name = build_tool_name(service, label, action_id_only)
    description = f"{action.get('name', raw_action_id)}: {action.get('description', '')}"

    # Round 13: OC returns inputSchema (full JSON Schema). We pull
    # out properties + required. Older OC versions used inputFields
    # (Round 10 style); fall back to that if inputSchema is missing.
    properties: Dict[str, dict] = {}
    required: List[str] = []
    input_schema = action.get("inputSchema")
    if isinstance(input_schema, dict):
        properties = dict(input_schema.get("properties", {}) or {})
        req = input_schema.get("required", []) or []
        required = list(req) if isinstance(req, list) else []
    else:
        input_fields = action.get("inputFields", []) or []
        for field in input_fields:
            fname = field.get("name", "")
            if not fname:
                continue
            prop: Dict[str, object] = {
                "type": _json_schema_type(field.get("type", "string")),
            }
            if "description" in field:
                prop["description"] = field["description"]
            if "enum" in field:
                prop["enum"] = field["enum"]
            if "default" in field:
                prop["default"] = field["default"]
            properties[fname] = prop
            if field.get("required"):
                required.append(fname)

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,  # Pitfall 26: always present
            },
        },
    }


# ── OC catalog probe ──────────────────────────────────────────────────


def _get_provider_actions(service_id: str, label: str = "default"):
    """Probe OC for one service's runtime action catalog.

    Round 13: renamed from "authorizationOptions" fetch to the actual
    RuntimeActionMetadata fetch. The old name persists for test
    fixture compatibility but the implementation now returns OC
    action dicts (id, operationType, requiredScopes, inputSchema),
    not authorizationOptions.

    Lazy-imports core.oauth.open_connector so this module's
    test-time imports don't pay the cost.
    """
    _ensure_sys_path()
    from core.oauth.open_connector import get_service_actions  # type: ignore
    return get_service_actions(service_id)


def _search_actions(service_id: str, label: str = "default"):
    """Legacy helper — preserved for any test fixture that mocked it.

    Round 13: discover_tools doesn't need translation anymore
    because each action comes back with its OC id already
    (e.g. ``slack.list_channels``). This helper still calls
    search_actions in case anyone is using it directly.
    """
    _ensure_sys_path()
    from core.oauth.open_connector import search_actions  # type: ignore
    return search_actions(service_id, label)


def translate_authopt_id_to_oc_action_id(
    service: str, authopt: dict, search_results: list
):
    """Round 13: NO-OP stub. Kept so test fixtures that mock this
    name don't break.

    The old translation (authopt.id -> service.action_name) is
    obsolete: Round 13's action-centric model uses OC's action id
    directly. The function returns ``authopt.get('id')`` (which is
    expected to already be an OC action id in the new model).
    """
    # Round 13: action-centric model — the input's "id" IS the OC
    # action id. No translation needed.
    return authopt.get("id")


def _has_credential(service: str, label: str) -> bool:
    """Cheap existence check: is there a credential for (service, label)?"""
    _ensure_sys_path()
    from core.oauth import credential_store  # type: ignore
    return credential_store.has(service, label)


# ── Storage ────────────────────────────────────────────────────────────


def _read_storage() -> dict:
    """Read tool_registry.json. Returns the on-disk dict, or
    ``{"tools": []}`` if absent. Never raises.
    """
    if not STORAGE_PATH.exists():
        return {"tools": []}
    try:
        return json.loads(STORAGE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("registry: failed to read %s: %s", STORAGE_PATH, e)
        return {"tools": []}


def _write_storage(storage: dict) -> None:
    """Atomic write. Caller holds the lock."""
    STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORAGE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(storage, indent=2), encoding="utf-8")
    tmp.replace(STORAGE_PATH)


# ── Public API ─────────────────────────────────────────────────────────


def discover_tools(service: str, label: str = "default") -> dict:
    """Probe OC for ``service``'s runtime action catalog. Register
    read-type actions as tools. Hold write/destructive actions in a
    'pending writes' list (the user enables them via
    ``freehand tools enable-writes <service>``).

    Returns ``{"registered": List[schema], "pending_writes": List[action]}``.

    Round 13 (action-centric): each OC action has its own id
    (e.g. ``slack.list_channels``). We register one tool per action
    — no scope-to-action translation needed. Read tools auto-register;
    write/destructive tools are queued until explicitly enabled.

    Pre-condition: a credential for (service, label) must exist
    (we don't probe OC for services without credentials — those are
    the user's "request via 503" path, not auto-discovery).

    Persistence: registered schemas are written to
    ``vault/tool_registry.json``. Merged into the in-memory
    ``core.agent_config.TOOL_SCHEMAS`` so ``list_available_tools()``
    picks them up immediately.
    """
    if not _has_credential(service, label):
        log.warning(
            "registry: skip discovery for %s/%s (no credential)",
            service, label,
        )
        return {"registered": [], "pending_writes": []}

    try:
        actions = _get_provider_actions(service, label)
    except Exception as e:
        log.warning("registry: OC probe failed for %s: %s", service, e)
        return {"registered": [], "pending_writes": []}

    registered: List[dict] = []
    pending_writes: List[dict] = []
    for action in actions:
        # Round 13: classify by operationType, not by authorizationOption risk.
        # OC's RuntimeActionMetadata.operationType is the source of truth.
        op_type = action.get("operationType", "read")
        kind = "read" if op_type == "read" else "write"

        schema = build_tool_schema(service, label, action)
        # The action's own id IS the OC action id (e.g. "slack.list_channels").
        oc_action_id = action.get("id")

        if kind == "read":
            registered.append({
                "schema": schema,
                "risk": "standard" if op_type == "read" else op_type,
                "oc_action_id": oc_action_id,
            })
        else:
            pending_writes.append(action)

    if not registered and not pending_writes:
        return {"registered": [], "pending_writes": []}

    # Persist the registered tools
    _persist_registered(service, label, registered)
    _merge_into_global_schemas(service, label, registered)

    log.info(
        "registry: %s/%s — registered %d reads, %d pending writes",
        service, label, len(registered), len(pending_writes),
    )
    return {"registered": registered, "pending_writes": pending_writes}


def _persist_registered(service: str, label: str, registered: List[dict]) -> None:
    """Write the registered schemas to vault/tool_registry.json, scoped
    to (service, label). Removes any prior (service, label) entry
    before adding the new one — discover_tools is idempotent on
    re-run for the same (service, label).

    Each persisted entry also stores the OC action's ``risk`` so
    bootstrap() can re-classify tools on next startup (the schema
    itself doesn't carry the risk signal — function-calling format
    only has description + parameters).
    """
    with _lock:
        storage = _read_storage()
        tools = [
            t for t in storage.get("tools", [])
            if not (t.get("service") == service and t.get("label") == label)
        ]
        for entry in registered:
            schema = entry["schema"]
            tools.append({
                "service": service,
                "label": label,
                "tool_name": schema["function"]["name"],
                "schema": schema,
                "risk": entry.get("risk", "standard"),
                "oc_action_id": entry.get("oc_action_id"),  # Round 12
            })
        storage["tools"] = tools
        _write_storage(storage)


def _merge_into_global_schemas(service: str, label: str, registered: List[dict]) -> None:
    """Inject the discovered tools into core.agent_config.TOOL_SCHEMAS
    and TOOL_REGISTRY. This is what makes ``list_available_tools()``
    pick them up — it iterates TOOL_SCHEMAS and merges into the
    function-calling shape.

    Each entry in ``registered`` is a ``{"schema": ..., "risk": ...}``
    dict. The schema has function-calling shape; the risk is the
    OC action's risk field (``standard`` | ``sensitive`` |
    ``destructive``) which determines TOOL_REGISTRY's read/write
    classification (Pitfall: a sensitive OC action might be a
    "read" tool to the LLM but still needs the write permission
    gate in intercept_action).

    Companion function ``_unmerge_from_global_schemas`` strips the
    same entries (used by disable()).
    """
    _ensure_sys_path()
    from core import agent_config  # type: ignore
    for entry in registered:
        schema = entry["schema"]
        risk = entry.get("risk", "standard")
        name = schema["function"]["name"]
        agent_config.TOOL_SCHEMAS[name] = {
            "description": schema["function"]["description"],
            "parameters": schema["function"]["parameters"],
        }
        # Classification: standard = read, anything else = write.
        # Even a sensitive READ (e.g. channels:history) needs the
        # write permission gate, because the underlying action is
        # not in the read-only tier.
        kind = "read" if risk == "standard" else "write"
        agent_config.TOOL_REGISTRY[name] = kind


def enable_writes(service: str, label: str = "default") -> int:
    """Re-probe OC and promote ALL pending writes (sensitive +
    destructive) for (service, label) into the active tool list.
    Returns count promoted.
    """
    if not _has_credential(service, label):
        return 0

    try:
        actions = _get_provider_actions(service, label)
    except Exception as e:
        log.warning("registry: OC probe failed for %s: %s", service, e)
        return 0

    promoted_schemas: List[dict] = []
    for action in actions:
        # Round 13: classify by operationType, not by authorizationOption risk.
        op_type = action.get("operationType", "read")
        kind = "read" if op_type == "read" else "write"
        if kind == "write":
            schema = build_tool_schema(service, label, action)
            promoted_schemas.append({
                "schema": schema,
                "risk": op_type,
                "oc_action_id": action.get("id"),
            })

    if not promoted_schemas:
        return 0

    # Read existing registered entries for (service, label) and merge
    existing = _read_active_registered_for(service, label)
    combined = existing + promoted_schemas
    _persist_registered(service, label, combined)
    _merge_into_global_schemas(service, label, combined)
    log.info(
        "registry: %s/%s — promoted %d writes",
        service, label, len(promoted_schemas),
    )
    return len(promoted_schemas)


def _read_active_registered_for(service: str, label: str) -> List[dict]:
    """Read the currently-active (service, label) registered entries
    from storage. Each entry is a dict ``{"schema": ..., "risk": ...}``
    — same shape passed to _persist_registered."""
    storage = _read_storage()
    out: List[dict] = []
    for t in storage.get("tools", []):
        if t.get("service") == service and t.get("label") == label:
            out.append({"schema": t["schema"], "risk": t.get("risk", "standard")})
    return out


def disable(service: str, label: str = "default") -> int:
    """Remove ALL tools (read + write) for (service, label). Returns
    count removed. Clears the in-memory TOOL_SCHEMAS too."""
    storage = _read_storage()
    removed_schemas: List[dict] = []
    kept: List[dict] = []
    for t in storage.get("tools", []):
        if t.get("service") == service and t.get("label") == label:
            removed_schemas.append(t["schema"])
        else:
            kept.append(t)

    with _lock:
        storage["tools"] = kept
        _write_storage(storage)

    _unmerge_from_global_schemas(removed_schemas)
    log.info(
        "registry: %s/%s — disabled %d tools",
        service, label, len(removed_schemas),
    )
    return len(removed_schemas)


def _unmerge_from_global_schemas(schemas: List[dict]) -> None:
    """Inverse of _merge_into_global_schemas — remove from TOOL_SCHEMAS
    and TOOL_REGISTRY. Takes raw schema dicts (not the registered
    wrapper) since disable() already has the schemas from storage."""
    _ensure_sys_path()
    from core import agent_config  # type: ignore
    for schema in schemas:
        name = schema["function"]["name"]
        agent_config.TOOL_SCHEMAS.pop(name, None)
        agent_config.TOOL_REGISTRY.pop(name, None)


def refresh() -> dict:
    """Re-probe OC for every credentialed service/label. Adds new tools,
    drops tools whose underlying actions disappeared from OC.

    Returns ``{"added": int, "removed": int, "updated": int}``.
    """
    _ensure_sys_path()
    from core.oauth import credential_store  # type: ignore
    added = removed = updated = 0

    # Find every (service, label) pair that has a credential
    all_credentials = credential_store.list_all()
    pairs = [(c["service"], c["label"]) for c in all_credentials]

    # Track which (service, label) the catalog currently knows about
    catalog_keys = set()
    for service, label in pairs:
        before_count = len(_read_active_registered_for(service, label))
        result = discover_tools(service, label)
        after_count = len(result["registered"])
        if after_count > before_count:
            added += after_count - before_count
        elif after_count < before_count:
            removed += before_count - after_count
        else:
            updated += 1
        for entry in result["registered"]:
            catalog_keys.add(entry["schema"]["function"]["name"])

    # Drop tools whose (service, label) no longer has a credential
    storage = _read_storage()
    kept: List[dict] = []
    dropped_schemas: List[dict] = []
    for t in storage.get("tools", []):
        key = t["tool_name"]
        if key in catalog_keys:
            kept.append(t)
        else:
            dropped_schemas.append(t["schema"])
            removed += 1

    if dropped_schemas:
        with _lock:
            storage["tools"] = kept
            _write_storage(storage)
        _unmerge_from_global_schemas(dropped_schemas)

    return {"added": added, "removed": removed, "updated": updated}


def list_tools(service: str = None, label: str = None) -> List[dict]:
    """List currently-active tools, optionally filtered by service/label."""
    storage = _read_storage()
    out: List[dict] = []
    for t in storage.get("tools", []):
        if service and t.get("service") != service:
            continue
        if label and t.get("label") != label:
            continue
        schema = t.get("schema", {})
        out.append({
            "service": t.get("service"),
            "label": t.get("label"),
            "tool_name": t.get("tool_name"),
            "schema": schema,
            "oc_action_id": t.get("oc_action_id"),  # Round 12
        })
    return out


def bootstrap() -> int:
    """Read vault/tool_registry.json and merge ALL stored tools into
    the in-memory TOOL_SCHEMAS / TOOL_REGISTRY. Called once at server
    startup so tools registered across CLI invocations persist across
    FreeHand restarts. Returns count merged.

    Each stored entry has ``{"schema": ..., "risk": ...}`` — the
    risk is the OC action's risk field, used to re-classify the
    tool in TOOL_REGISTRY.
    """
    storage = _read_storage()
    tools = storage.get("tools", [])
    if not tools:
        return 0

    # Group by (service, label) for the merge helper
    grouped: Dict[tuple, List[dict]] = {}
    for t in tools:
        key = (t.get("service"), t.get("label"))
        grouped.setdefault(key, []).append({
            "schema": t["schema"],
            "risk": t.get("risk", "standard"),
        })

    count = 0
    for (service, label), registered in grouped.items():
        _merge_into_global_schemas(service, label, registered)
        count += len(registered)

    log.info("registry: bootstrap merged %d tools", count)
    return count
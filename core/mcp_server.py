"""FreeHand MCP server surface.

Implements the Model Context Protocol (MCP) JSON-RPC 2.0 binding over
HTTP so any MCP client (Claude Desktop, Hermes, custom agents) can use
FreeHand's 24 tools over the network.

Wire format: JSON-RPC 2.0 over HTTP POST.

Endpoint: POST /mcp (mounted by server.py)

Methods implemented:
  - tools/list    → returns TOOL_REGISTRY tools in MCP format
  - tools/call     → dispatches to core.agent.execute_tool()
                    (uses existing tier-based permission checks)
  - resources/list → returns vault files as MCP resources
  - resources/read → returns file content of a vault file
  - initialize     → MCP protocol handshake
  - ping           → liveness check

Auth: X-API-Key header (existing api_key from settings.json). Same auth
as /api/* and /api/tools/* endpoints. The api_key is the user's MCP
credential.

Reference: https://modelcontextprotocol.io/specification/2025-06-18
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.agent_config import list_available_tools, TOOL_REGISTRY
from core.agent import execute_tool
from core.security import (
    PermissionTier,
    get_current_tier,
    intercept_action,
)
from core.memory import search_memory

log = logging.getLogger("freehand.mcp")

router = APIRouter(prefix="/mcp", tags=["mcp"])
# Auth is mounted at the app level in server.py via
# app.include_router(mcp_router_mod.router, dependencies=[Depends(require_api_key)])
# — we don't add it here to avoid the server.py ↔ core.mcp_server circular import.
#
# Auth header compatibility: MCP clients (Codex, Claude Desktop, generic
# MCP SDKs) typically use `Authorization: Bearer <key>` rather than
# `X-API-Key`. The require_api_key dependency accepts both — see its
# header-compat block.

# MCP protocol version this server implements.
MCP_PROTOCOL_VERSION = "2025-06-18"

# Cap on resources returned by resources/list — keeps response bounded.
MAX_RESOURCES_PER_LIST = 200

# Cap on resource content size for resources/read — large files truncated.
MAX_RESOURCE_CONTENT_BYTES = 256 * 1024  # 256 KB


# ── JSON-RPC plumbing ──────────────────────────────────────────────────

def _jsonrpc_response(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id, code: int, message: str, data: Any = None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# Standard JSON-RPC error codes we use
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ── Tool list ───────────────────────────────────────────────────────────

def _mcp_tools() -> List[Dict]:
    """Return TOOL_REGISTRY tools in MCP-compatible format.

    MCP tool schema follows JSON Schema (like OpenAI's function-calling
    format). We build it from list_available_tools() — the same source
    the in-process agent loop uses — so MCP and the agent loop can't drift.
    """
    mcp_tools = []
    for tool in list_available_tools():
        func = tool["function"]
        mcp_tools.append({
            "name": func["name"],
            "description": func["description"],
            "inputSchema": func["parameters"],
        })
    return mcp_tools


# ── Tool dispatch ───────────────────────────────────────────────────────

def _mcp_tool_call(name: str, arguments: Dict) -> Dict:
    """Dispatch a tool call to the existing execute_tool().

    Reuses the same permission/tier machinery as the in-process agent loop.
    A write tool at GOD_MODE from an MCP call still executes (same as if
    FreeHand itself had decided to call it). Write tools at lower tiers
    are rejected — the MCP client should surface that to the user.

    Note: execute_tool() is async (returns a coroutine). To avoid
    "Cannot run the event loop while another loop is running" when
    called from inside FastAPI's existing loop, we dispatch the
    coroutine via run_in_executor on a fresh thread with its own loop.
    """
    tier = get_current_tier()
    permission = TOOL_REGISTRY.get(name, "unknown")

    if permission == "unknown":
        return {
            "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
            "isError": True,
        }
    if permission == "write" and tier != PermissionTier.GOD_MODE:
        result = intercept_action(
            "oauth_access",
            f"MCP tool call: {name}",
            {"tool": name, "arguments": arguments, "source": "mcp"},
            source="mcp",
        )
        if not result.get("allowed"):
            return {
                "content": [{
                    "type": "text",
                    "text": (
                        f"Permission denied for {name}: "
                        f"write tools require explicit approval at the current "
                        f"permission tier. Use /approve in the FreeHand UI "
                        f"first, or upgrade tier to GOD_MODE in settings."
                    ),
                }],
                "isError": True,
            }

    try:
        tool_result = _run_async_in_thread(execute_tool(name, arguments))
        return {
            "content": [{"type": "text", "text": str(tool_result.get("content", ""))}],
            "isError": "error" in tool_result,
        }
    except Exception as e:
        log.exception(f"MCP tool call {name} failed")
        return {
            "content": [{"type": "text", "text": f"Tool execution error: {str(e)[:500]}"}],
            "isError": True,
        }


def _run_async_in_thread(coro):
    """Run an async coroutine to completion in a fresh thread with its own loop.

    Required because FastAPI's request handler is itself async — running
    a new event loop inside it raises "Cannot run the event loop while
    another loop is running." By offloading to a worker thread we get
    a clean loop each time.

    Returns the result of the coroutine. Re-raises any exception.
    """
    import asyncio
    import threading
    result_box = {"value": None, "error": None}

    def _runner():
        try:
            result_box["value"] = asyncio.run(coro)
        except Exception as e:
            result_box["error"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if result_box["error"] is not None:
        raise result_box["error"]
    return result_box["value"]


# ── Resource list ──────────────────────────────────────────────────────

def _mcp_resources(vault_dir: Path) -> List[Dict]:
    """Return vault .md files as MCP resources.

    Each resource has a `uri` (freehand://vault/<relative-path>) and
    metadata. We deliberately don't include .sweep_state.json or
    00_Scribble.md here — those are operational/transient and not
    useful as MCP resources.
    """
    from core.memory import VAULT_DIR as _VD  # noqa: F401 (sanity)
    skip = {"00_scribble.md", ".sweep_state.json"}
    out: List[Dict] = []
    if not vault_dir.exists():
        return out
    for path in sorted(vault_dir.rglob("*.md")):
        if path.name.lower() in skip:
            continue
        rel = str(path.relative_to(vault_dir))
        out.append({
            "uri": f"freehand://vault/{rel}",
            "name": path.stem,
            "description": f"Vault file: {rel}",
            "mimeType": "text/markdown",
        })
        if len(out) >= MAX_RESOURCES_PER_LIST:
            break
    return out


def _mcp_resource_read(uri: str, vault_dir: Path) -> Dict:
    """Read content of a vault resource by URI."""
    if not uri.startswith("freehand://vault/"):
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "text/plain",
                "text": f"Unsupported URI scheme: {uri}",
            }],
            "isError": True,
        }
    rel = uri[len("freehand://vault/"):]
    # Path traversal defence — reject .., /, \\, absolute paths
    if ".." in rel or rel.startswith("/") or "\\" in rel:
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "text/plain",
                "text": "Invalid resource path (traversal rejected)",
            }],
            "isError": True,
        }
    target = (vault_dir / rel).resolve()
    try:
        target.relative_to(vault_dir.resolve())
    except ValueError:
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "text/plain",
                "text": "Resource escapes vault directory",
            }],
            "isError": True,
        }
    if not target.exists():
        return {
            "contents": [{
                "uri": uri,
                "mimeType": "text/plain",
                "text": "Resource not found",
            }],
            "isError": True,
        }
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = target.read_text(encoding="latin-1", errors="replace")
    if len(text) > MAX_RESOURCE_CONTENT_BYTES:
        text = text[:MAX_RESOURCE_CONTENT_BYTES] + "\n\n[... truncated ...]"
    return {
        "contents": [{
            "uri": uri,
            "mimeType": "text/markdown",
            "text": text,
        }],
    }


# ── Main JSON-RPC dispatch ─────────────────────────────────────────────

@router.post("")
@router.post("/")
async def mcp_endpoint(request: Request):
    """JSON-RPC 2.0 endpoint for MCP clients.

    Accepts a single JSON-RPC request or a batch. Always returns a JSON-RPC
    response (single or batch). Auth via X-API-Key header is enforced at
    the router level via the require_api_key dependency.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _jsonrpc_error(None, PARSE_ERROR, "Invalid JSON"),
            status_code=400,
        )

    # JSON-RPC supports both single requests and batches.
    is_batch = isinstance(body, list)
    requests = body if is_batch else [body]

    responses = []
    for req in requests:
        if not isinstance(req, dict):
            responses.append(_jsonrpc_error(None, INVALID_REQUEST, "Request must be an object"))
            continue
        if req.get("jsonrpc") != "2.0":
            responses.append(_jsonrpc_error(req.get("id"), INVALID_REQUEST, "jsonrpc must be '2.0'"))
            continue
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}

        try:
            response = _dispatch(method, params, req_id)
            if response is not None:
                responses.append(response)
        except Exception as e:
            log.exception(f"MCP dispatch error on method {method}")
            responses.append(_jsonrpc_error(req_id, INTERNAL_ERROR, f"Internal error: {str(e)[:200]}"))

    if not responses:
        # Notification (no id) — return 204 No Content per JSON-RPC spec.
        from fastapi import status as _status
        return JSONResponse({}, status_code=_status.HTTP_204_NO_CONTENT)
    return JSONResponse(responses if is_batch else responses[0])


def _dispatch(method: str, params: Dict, req_id) -> Dict:
    """Dispatch a single JSON-RPC method to its handler."""
    if method == "initialize":
        return _jsonrpc_response(req_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {
                "name": "freehand",
                "version": "0.1.8",
            },
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
        })

    if method == "ping":
        return _jsonrpc_response(req_id, {})

    if method == "tools/list":
        return _jsonrpc_response(req_id, {"tools": _mcp_tools()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name or not isinstance(name, str):
            return _jsonrpc_error(req_id, INVALID_PARAMS, "params.name must be a string")
        if not isinstance(arguments, dict):
            return _jsonrpc_error(req_id, INVALID_PARAMS, "params.arguments must be an object")
        return _jsonrpc_response(req_id, _mcp_tool_call(name, arguments))

    if method == "resources/list":
        from core.memory import VAULT_DIR
        return _jsonrpc_response(req_id, {"resources": _mcp_resources(VAULT_DIR)})

    if method == "resources/read":
        uri = params.get("uri")
        if not uri or not isinstance(uri, str):
            return _jsonrpc_error(req_id, INVALID_PARAMS, "params.uri must be a string")
        from core.memory import VAULT_DIR
        return _jsonrpc_response(req_id, _mcp_resource_read(uri, VAULT_DIR))

    # Method not implemented — JSON-RPC standard error
    return _jsonrpc_error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")

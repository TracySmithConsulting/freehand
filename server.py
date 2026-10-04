import json
import secrets as _secrets
import sqlite3
import asyncio
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import sys
from importlib import resources as importlib_resources

sys.path.insert(0, str(Path(__file__).parent))
from core.database import init_db, DB_PATH  # DB_PATH is the canonical path from core.database (vault/agent.db)
from core.memory import sync_vault_to_sqlite, search_memory, parse_skills
from core.scheduler import get_scheduler, process_scribble
from core.security import (
    get_current_tier,
    set_tier,
    intercept_action,
    get_pending_approvals,
    handle_approval,
    clear_pending_approvals,
    subscribe as _sse_subscribe,
    unsubscribe as _sse_unsubscribe,
)
from core.gateway import (
    handle_telegram_webhook,
    handle_slack_webhook,
    handle_whatsapp_message,
    handle_whatsapp_pair,
    get_gateway_status,
    update_gateway_settings,
)
from core.agents import (
    detect_agents,
    detect_agent,
    summarize_agents,
    import_hermes,
    import_openclaw,
    import_generic,
    record_import,
    list_imports,
    list_imported_sources,
    remove_import,
    get_import_status,
)
from core.tools.office import (
    read_docx,
    write_docx,
    read_xlsx,
    write_xlsx,
    read_pptx,
    write_pptx,
)
from core.tools.browser import (
    get_axtree,
    extract_text,
    click,
    fill,
    navigate,
    screenshot,
)
from core.oauth import router as oauth_router
from core.oauth import broker as oauth_broker
from core import mcp_server as mcp_router_mod

app = FastAPI(
    title="FreeHand API",
    description="Local AI agent system with Tailscale network access",
    version="0.1.0"
)

# DB_PATH is imported from core.database (canonical vault/agent.db path)
VAULT_DIR = Path(__file__).parent / "vault"
SCRIBBLE_PATH = VAULT_DIR / "00_Scribble.md"

# N9 fix: request body size limit middleware.
# Without a cap, anyone with X-API-Key can POST a 100MB body to
# /api/tools/browser/fill and force the server to spend time parsing
# huge inputs (or OOM). 1MB is plenty for any current FreeHand endpoint;
# tune via settings["max_request_body_bytes"] if needed.
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB


def _get_max_body_bytes() -> int:
    """Read the operator-configurable body size cap from settings.json."""
    try:
        with open(Path(__file__).parent / "vault" / "settings.json") as f:
            settings = json.load(f)
        return int(settings.get("max_request_body_bytes", DEFAULT_MAX_BODY_BYTES))
    except Exception:
        return DEFAULT_MAX_BODY_BYTES


@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    """Reject requests whose Content-Length exceeds the cap."""
    # Allow gateway webhooks (they may carry Slack/Telegram payloads —
    # typically small but we want to be permissive here so callbacks work).
    if request.url.path.startswith("/api/gateway/"):
        return await call_next(request)

    # Health/static: no body expected.
    if request.url.path in ("/", "/health"):
        return await call_next(request)

    cap = _get_max_body_bytes()
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > cap:
                return JSONResponse(
                    {"error": f"Request body too large (>{cap} bytes). Increase settings.max_request_body_bytes or shrink payload."},
                    status_code=413,
                )
        except ValueError:
            pass

    return await call_next(request)


# ── API-key auth (N1 fix) ─────────────────────────────────────────────
# Every /api/* and /api/tools/* endpoint requires an X-API-Key header
# matching settings["api_key"]. On first run, an api_key is generated
# and persisted to vault/settings.json; the user is told via stderr.
#
# Exceptions (no auth required):
#   GET  /                          - serves the static web UI
#   GET  /health                    - liveness probe
#   POST /api/gateway/telegram      - bot-token-based auth + allow_from
#   POST /api/gateway/slack         - HMAC signature + allow_from
#   POST /api/gateway/whatsapp      - bridge-secret header + allow_from
#
# This is "single shared secret" auth — sufficient for a local-first
# single-user agent. NOT suitable for multi-user or public exposure.

def _load_settings_for_auth() -> dict:
    settings_path = VAULT_DIR / "settings.json"
    if settings_path.exists():
        try:
            return json.loads(settings_path.read_text())
        except Exception:
            pass
    return {}


def _save_settings_for_auth(data: dict) -> None:
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    (VAULT_DIR / "settings.json").write_text(json.dumps(data))


def get_or_create_api_key() -> str:
    """Return the current API key, generating one if missing."""
    settings = _load_settings_for_auth()
    key = settings.get("api_key", "").strip()
    if not key:
        # 32-byte URL-safe token; ~43 chars. Compared in constant time below.
        key = _secrets.token_urlsafe(32)
        settings["api_key"] = key
        _save_settings_for_auth(settings)
        print(
            "[FreeHand] Generated new api_key (settings.json → api_key).\n"
            "           Pass it as `X-API-Key: <key>` header on all /api/* requests.\n"
            "           Read it: cat vault/settings.json | jq -r .api_key"
        )
    return key


async def require_api_key(request: Request) -> None:
    """FastAPI dependency: enforce X-API-Key on protected endpoints.

    Accepts the API key via two header styles:
      - X-API-Key: <key>                      (FreeHand-native)
      - Authorization: Bearer <key>           (MCP-standard; what Codex,
                                              Claude Desktop, generic MCP
                                              SDKs send by default)

    Comparison is constant-time to avoid timing leaks.
    """
    # Allow health, root, and gateway webhooks (each has its own auth)
    path = request.url.path
    if path in ("/", "/health"):
        return
    if path.startswith("/api/gateway/"):
        return  # Telegram/Slack/WhatsApp each have their own auth mechanism

    expected = get_or_create_api_key()

    # Try X-API-Key first, then Authorization: Bearer
    provided = request.headers.get("X-API-Key", "").strip()
    if not provided:
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()

    if not provided or not _secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

try:
    static_path = importlib_resources.files("freehand").joinpath("static").as_posix()
    app.mount("/static", StaticFiles(directory=static_path), name="static")
except Exception:
    pass
app.include_router(oauth_router)


# ── OAuth Broker Admin Endpoints ──────────────────────────────────────
# Diagnostic + write endpoints for the broker config. Broker holds the
# FreeHand-shared OAuth client_id/secret per service (see core/oauth/broker.py).
# User-supplied credentials in settings.json always win; broker is fallback.

@app.get("/api/broker/status", dependencies=[Depends(require_api_key)])
async def broker_status_endpoint():
    """Report which services have credentials configured and where from."""
    return oauth_broker.broker_status()


@app.post("/api/broker/config", dependencies=[Depends(require_api_key)])
async def broker_write_config_endpoint(body: dict):
    """Write broker_config.json with the FreeHand-shared OAuth credentials.

    Body shape:
        {"providers": {"google": {"client_id": "...", "client_secret": "..."},
                        "microsoft": {...}, ...}}

    Returns the new broker_status() snapshot.
    """
    providers = body.get("providers", {})
    if not isinstance(providers, dict):
        raise HTTPException(status_code=400, detail="'providers' must be an object")
    return oauth_broker.write_broker_config(providers)


@app.delete("/api/broker/config", dependencies=[Depends(require_api_key)])
async def broker_clear_endpoint():
    """Remove broker_config.json (reset to user-only credentials)."""
    p = oauth_broker._broker_config_path()
    if p.exists():
        p.unlink()
    return oauth_broker.broker_status()
# MCP server surface (JSON-RPC 2.0 over HTTP POST at /mcp). Same X-API-Key
# auth as /api/* — the user's settings["api_key"] doubles as their MCP
# credential. Auth dependency is mounted here (not on the router itself)
# to avoid the server.py ↔ core.mcp_server circular import. See core/mcp_server.py
# for method coverage.
app.include_router(
    mcp_router_mod.router,
    dependencies=[Depends(require_api_key)],
)


@app.on_event("startup")
async def startup_event():
    # Round 10 (PR 1): one-time migration of legacy agent.db from the
    # project root into vault/agent.db. Runs ONCE per app boot, not on
    # every init_db() call (test fixtures monkeypatch DB_PATH and would
    # otherwise touch real files). Idempotent — no-op after first run.
    from core.database import migrate_legacy_root_db, init_db
    migrate_legacy_root_db()
    init_db()
    VAULT_DIR.mkdir(exist_ok=True)
    if not SCRIBBLE_PATH.exists():
        SCRIBBLE_PATH.write_text("# 00_Scribble\n\n> Agent scratchpad\n\n## Quick Notes\n\n- [ ] Task to remember\n")
    get_scheduler()
    # Round 10 (PR 2): one-time migration of legacy connections.db rows
    # into vault/credential_store.json. Idempotent — no-op after the
    # first run (sentinel "_migrated": true is set in storage).
    from core.oauth.manager import migrate_legacy_connections_db
    migrate_legacy_connections_db()
    # Round 10 (PR 2): bootstrap the tool registry so OC-discovered
    # tools persist across FreeHand restarts. bootstrap() reads
    # vault/tool_registry.json (written by `freehand tools refresh` /
    # `enable-writes` / `disable`) and injects the stored tools into
    # core.agent_config.TOOL_SCHEMAS + TOOL_REGISTRY. After this runs,
    # list_available_tools() includes the OC-discovered tools and
    # intercept_action() can gate them on read/write permissions.
    from core.tools import registry as _tool_registry
    _tool_registry.bootstrap()
    # Log any expired connections on startup
    from core.oauth.manager import list_connections, is_token_expired
    all_conns = list_connections()
    for conn in all_conns:
        if is_token_expired(conn["service"], conn["label"]):
            print(f"[FreeHand] Token expired for {conn['service']} ({conn['label']}) — user should reconnect")


@app.get("/")
async def root():
    try:
        static_path = importlib_resources.files("freehand").joinpath("static", "index.html")
        return FileResponse(str(static_path))
    except Exception:
        raise HTTPException(status_code=404, detail="Static files not found")


@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/v1/status", dependencies=[Depends(require_api_key)])
async def api_status():
    return {
        "service": "freehand",
        "version": "0.1.0",
        "access": {
            "local": "http://localhost:8000",
            "tailscale": "http://100.x.y.z:8000"
        }
    }


@app.get("/api/scribble", dependencies=[Depends(require_api_key)])
async def get_scribble():
    if not SCRIBBLE_PATH.exists():
        return {"content": ""}
    return {"content": SCRIBBLE_PATH.read_text()}


@app.post("/api/scribble", dependencies=[Depends(require_api_key)])
async def save_scribble(body: dict):
    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    SCRIBBLE_PATH.write_text(content)
    return {"status": "saved", "path": str(SCRIBBLE_PATH)}


@app.get("/api/tasks", dependencies=[Depends(require_api_key)])
async def get_tasks():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks ORDER BY id DESC")
    tasks = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return tasks


@app.post("/api/tasks", dependencies=[Depends(require_api_key)])
async def create_task(body: dict):
    title = body.get("title", "").strip()
    cron_schedule = body.get("cron_schedule", "").strip()
    if not title or not cron_schedule:
        raise HTTPException(status_code=400, detail="Title and cron_schedule are required")
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO tasks (title, cron_schedule, status) VALUES (?, ?, ?)",
        (title, cron_schedule, "pending")
    )
    conn.commit()
    task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    return {"id": task_id, "title": title, "cron_schedule": cron_schedule, "status": "pending"}


@app.delete("/api/tasks/{task_id}", dependencies=[Depends(require_api_key)])
async def delete_task(task_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "deleted"}


@app.get("/api/approvals", dependencies=[Depends(require_api_key)])
async def api_get_approvals():
    return get_pending_approvals()


@app.post("/api/approvals/{approval_id}/approve", dependencies=[Depends(require_api_key)])
async def api_approve(approval_id: int):
    try:
        result = handle_approval(approval_id, "approve")
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/approvals/{approval_id}/deny", dependencies=[Depends(require_api_key)])
async def api_deny(approval_id: int):
    try:
        result = handle_approval(approval_id, "deny")
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/approvals/clear", dependencies=[Depends(require_api_key)])
async def api_clear_approvals(body: dict = None):
    """Reject all pending approvals. C4 fix: accepts optional `reason` field."""
    reason = ""
    if body and isinstance(body, dict):
        reason = body.get("reason", "") or ""
    count = clear_pending_approvals(reason=reason)
    return {"cleared": count, "reason": reason}


@app.get("/api/approvals/stream", dependencies=[Depends(require_api_key)])
async def approval_stream(request: Request):
    queue: list = []

    def push(event_data: dict):
        import json as _json
        queue.append(f"event: approval_pending\ndata: {_json.dumps(event_data)}\n\n")

    sub_id = _sse_subscribe(push)

    async def event_generator():
        try:
            while not await request.is_disconnected():
                while queue:
                    yield queue.pop(0)
                await asyncio.sleep(0.5)
        finally:
            _sse_unsubscribe(sub_id)

    import asyncio
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/security/tier", dependencies=[Depends(require_api_key)])
async def get_tier():
    return {"tier": get_current_tier().value}


@app.post("/api/security/tier", dependencies=[Depends(require_api_key)])
async def set_tier_endpoint(body: dict):
    tier = body.get("tier", "").strip()
    try:
        result = set_tier(tier)
        return {"tier": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/security/check", dependencies=[Depends(require_api_key)])
async def security_check(body: dict):
    action_type = body.get("action_type", "").strip()
    description = body.get("description", "").strip()
    payload = body.get("payload")
    source = body.get("source", "")
    caller_id = body.get("caller_id")

    if not action_type or not description:
        raise HTTPException(status_code=400, detail="action_type and description are required")

    result = intercept_action(action_type, description, payload, source, caller_id)
    return result


# ── N2 fix: deep-merge settings instead of overwriting whole file ──
# The old endpoint let any caller replace the entire settings.json,
# which would silently drop keys that other parts of the system rely on.
# Now: known keys are merged at the top level; unknown keys are rejected.

SETTINGS_ALLOWED_KEYS = frozenset({
    "api_key", "omniroute", "llm", "tier", "oauth", "telegram",
    "telegram_bot_token", "telegram_chat_id", "telegram_chat_whitelist",
    "slack", "slack_bot_token", "slack_webhook_url", "slack_signing_secret",
    "slack_allow_from", "whatsapp", "whatsapp_enabled", "whatsapp_pairing_phone",
    "bridge_secret", "whatsapp_allow_from", "public_base_url",
    "sweep_interval", "allowed_browser_domains", "user",
    "max_request_body_bytes", "max_screenshot_size_mb",
})


def _merge_settings(existing: dict, updates: dict) -> tuple:
    """Recursive deep-merge of settings, bounded to known keys.

    For each key in `updates`:
    - If not in SETTINGS_ALLOWED_KEYS → reject.
    - If both existing and updates values are dicts → recursive merge.
    - Otherwise → replace.

    Returns (merged, rejected_keys).

    Three-level deep merge (oauth.providers.google) is supported because
    the recursion handles arbitrary nesting.
    """
    merged = dict(existing)
    rejected = []
    for k, v in updates.items():
        if k not in SETTINGS_ALLOWED_KEYS:
            rejected.append(k)
            continue
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge_dicts(merged[k], v)
        else:
            merged[k] = v
    return merged, rejected


def _deep_merge_dicts(existing: dict, updates: dict) -> dict:
    """Recursive deep-merge of two dicts. Lists are replaced, not merged."""
    out = dict(existing)
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


@app.get("/api/settings", dependencies=[Depends(require_api_key)])
async def get_settings():
    settings_path = Path(__file__).parent / "vault" / "settings.json"
    if settings_path.exists():
        return json.loads(settings_path.read_text())
    return {"api_key": "", "omniroute": ""}


@app.post("/api/settings", dependencies=[Depends(require_api_key)])
async def save_settings(body: dict):
    settings_path = Path(__file__).parent / "vault" / "settings.json"
    existing = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except Exception:
            pass
    merged, rejected = _merge_settings(existing, body)
    settings_path.write_text(json.dumps(merged))
    return {"status": "saved", "rejected": rejected}


@app.post("/api/agent/command", dependencies=[Depends(require_api_key)])
async def agent_command(body: dict):
    command = body.get("command", "").strip()
    source = body.get("source", "")
    caller_id = body.get("caller_id")
    if not command:
        raise HTTPException(status_code=400, detail="Command is required")

    # N3 fix: truncate command before storing. Full text goes to the agent
    # loop but only the first 200 chars land in the memories table. Sensitive
    # secrets in commands are not persisted indefinitely.
    import hashlib
    cmd_truncated = command[:200]
    cmd_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO memories (path, title, content) VALUES (?, ?, ?)",
        ("/agent/command", f"Command ({cmd_hash}): " + cmd_truncated[:50], cmd_truncated)
    )
    conn.commit()
    conn.close()

    # Run the agent
    from core.agent import run_agent
    result = await run_agent(command, source=source, caller_id=caller_id)
    return result


@app.post("/api/memory/sync", dependencies=[Depends(require_api_key)])
async def memory_sync():
    count = sync_vault_to_sqlite()
    return {"status": "synced", "files": count}


@app.get("/api/memory/search", dependencies=[Depends(require_api_key)])
async def memory_search(q: str = "", limit: int = 3):
    if not q.strip():
        return []
    results = search_memory(q, limit=limit)
    return results


@app.get("/api/skills", dependencies=[Depends(require_api_key)])
async def list_skills():
    skills = parse_skills()
    return skills


@app.post("/api/sweep", dependencies=[Depends(require_api_key)])
async def api_sweep():
    result = process_scribble()
    return result


# ── Gateway / Remote Chat Endpoints ────────────────────────────────────────────
#
# Gateway webhooks use their own auth mechanism (bot tokens, HMAC signatures,
# bridge secrets). They DO NOT require X-API-Key — see require_api_key()
# exception list above. Inside each handler we enforce allow_from as the
# second layer (see core/gateway.py).

@app.post("/api/gateway/telegram")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return {"ok": False, "error": "Invalid JSON"}
    result = await handle_telegram_webhook(update)
    return result


@app.post("/api/gateway/slack")
async def slack_webhook(request: Request):
    signature = request.headers.get("x-slack-signature", "")
    timestamp = request.headers.get("x-slack-request-timestamp", "")
    body = await request.body()
    result = await handle_slack_webhook(body.decode("utf-8"), {
        "x-slack-signature": signature,
        "x-slack-request-timestamp": timestamp,
    })
    status = result.get("status", 200)
    return JSONResponse(content=result, status_code=status)


# N12c: WhatsApp webhook requires X-Bridge-Secret matching settings.bridge_secret.
# The bridge (separate Node.js process) must send this header. If unset, the
# endpoint is closed entirely (returns 503).
@app.post("/api/gateway/whatsapp")
async def whatsapp_webhook(request: Request):
    settings = _load_settings_for_auth()
    expected = settings.get("bridge_secret", "").strip()
    if not expected:
        return JSONResponse(
            {"error": "bridge_secret not configured in settings.json"},
            status_code=503,
        )
    provided = request.headers.get("X-Bridge-Secret", "").strip()
    if not provided or not _secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Bridge-Secret")

    try:
        data = await request.json()
    except Exception:
        return {"error": "Invalid JSON"}
    remote_jid = data.get("remote_jid", "")
    text = data.get("text", "")
    if not remote_jid or not text:
        return {"error": "remote_jid and text required"}
    result = await handle_whatsapp_message(remote_jid, text)
    return result


@app.get("/api/gateway/status", dependencies=[Depends(require_api_key)])
async def gateway_status():
    return get_gateway_status()


@app.post("/api/gateway/settings", dependencies=[Depends(require_api_key)])
async def gateway_settings(body: dict):
    result = update_gateway_settings(body)
    return result


@app.post("/api/gateway/whatsapp/pair", dependencies=[Depends(require_api_key)])
async def whatsapp_pair(body: dict):
    phone = body.get("phone", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone number required")
    result = await handle_whatsapp_pair(phone)
    return result


# ── Tool Endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/tools/office/read/docx", dependencies=[Depends(require_api_key)])
async def tool_read_docx(path: str):
    result = read_docx(path)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/tools/office/write/docx", dependencies=[Depends(require_api_key)])
async def tool_write_docx(body: dict):
    path = body.get("path", "").strip()
    title = body.get("title", "Untitled").strip()
    content = body.get("content", [])
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    result = write_docx(path, title, content)
    if "error" in result:
        return result
    return result


@app.get("/api/tools/office/read/xlsx", dependencies=[Depends(require_api_key)])
async def tool_read_xlsx(path: str):
    result = read_xlsx(path)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/tools/office/write/xlsx", dependencies=[Depends(require_api_key)])
async def tool_write_xlsx(body: dict):
    path = body.get("path", "").strip()
    data = body.get("data", {})
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    result = write_xlsx(path, data)
    if "error" in result:
        return result
    return result


@app.get("/api/tools/office/read/pptx", dependencies=[Depends(require_api_key)])
async def tool_read_pptx(path: str):
    result = read_pptx(path)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/tools/office/write/pptx", dependencies=[Depends(require_api_key)])
async def tool_write_pptx(body: dict):
    path = body.get("path", "").strip()
    slides = body.get("slides", [])
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    result = write_pptx(path, slides)
    if "error" in result:
        return result
    return result


@app.post("/api/tools/browser/axtree", dependencies=[Depends(require_api_key)])
async def tool_browser_axtree(body: dict):
    url = body.get("url", "").strip()
    mode = body.get("mode", "headless")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: asyncio.run(get_axtree(url, mode=mode)))
    return result


@app.post("/api/tools/browser/text", dependencies=[Depends(require_api_key)])
async def tool_browser_text(body: dict):
    url = body.get("url", "").strip()
    mode = body.get("mode", "headless")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: asyncio.run(extract_text(url, mode=mode)))
    return result


@app.post("/api/tools/browser/click", dependencies=[Depends(require_api_key)])
async def tool_browser_click(body: dict):
    url = body.get("url", "").strip()
    selector = body.get("selector", "").strip()
    mode = body.get("mode", "headless")
    if not url or not selector:
        raise HTTPException(status_code=400, detail="url and selector are required")
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: asyncio.run(click(url, selector, mode=mode)))
    return result


@app.post("/api/tools/browser/fill", dependencies=[Depends(require_api_key)])
async def tool_browser_fill(body: dict):
    url = body.get("url", "").strip()
    selector = body.get("selector", "").strip()
    value = body.get("value", "").strip()
    mode = body.get("mode", "headless")
    if not url or not selector or not value:
        raise HTTPException(status_code=400, detail="url, selector, and value are required")
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: asyncio.run(fill(url, selector, value, mode=mode)))
    return result


@app.post("/api/tools/browser/navigate", dependencies=[Depends(require_api_key)])
async def tool_browser_navigate(body: dict):
    url = body.get("url", "").strip()
    mode = body.get("mode", "headless")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: asyncio.run(navigate(url, mode=mode)))
    return result


@app.post("/api/tools/browser/screenshot", dependencies=[Depends(require_api_key)])
async def tool_browser_screenshot(body: dict):
    url = body.get("url", "").strip()
    mode = body.get("mode", "headless")
    full_page = body.get("full_page", False)
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: asyncio.run(screenshot(url, mode=mode, full_page=full_page)))
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )


# ── Agent Import Endpoints ─────────────────────────────────────────────────────

@app.get("/api/agents", dependencies=[Depends(require_api_key)])
async def agents_list():
    """List all detected AI agents."""
    return summarize_agents()


@app.post("/api/agents/detect", dependencies=[Depends(require_api_key)])
async def agents_detect():
    """Force re-scan for installed agents."""
    return summarize_agents()


@app.get("/api/agents/imported", dependencies=[Depends(require_api_key)])
async def agents_imported():
    """List all import records."""
    return get_import_status()


@app.post("/api/agents/import", dependencies=[Depends(require_api_key)])
async def agents_import(body: dict):
    """Import from a detected agent."""
    agent_name = body.get("agent", "").strip().lower()
    include_state = body.get("include_state", False)
    include_skills = body.get("include_skills", True)
    overwrite = body.get("overwrite", False)

    detected = detect_agent(agent_name)
    if not detected:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_name}")

    vault_dir = Path(__file__).parent / "vault"
    from core.agents.models import ImportOptions
    opts = ImportOptions(
        agent=agent_name,
        include_state=include_state,
        include_skills=include_skills,
        overwrite=overwrite,
    )

    importers = {"hermes": import_hermes, "openclaw": import_openclaw}
    importer = importers.get(agent_name)
    if not importer:
        raise HTTPException(status_code=501, detail=f"No importer for: {agent_name}")

    result = importer(detected, opts, vault_dir)
    manifest_id = record_import(result)

    return {
        "manifest_id": manifest_id,
        "agent": result.agent,
        "files_imported": len(result.files_imported),
        "skills_imported": len(result.skills_imported),
        "state_extracted": result.state_extracted,
        "errors": result.errors,
        "skipped": result.skipped,
    }


@app.post("/api/agents/import/preview", dependencies=[Depends(require_api_key)])
async def agents_import_preview(body: dict):
    """Preview an import without writing."""
    agent_name = body.get("agent", "").strip().lower()
    include_state = body.get("include_state", False)

    detected = detect_agent(agent_name)
    if not detected:
        raise HTTPException(status_code=404, detail=f"Agent not found: {agent_name}")

    from core.agents.models import ImportOptions
    from core.agents import preview_hermes, preview_openclaw
    opts = ImportOptions(agent=agent_name, include_state=include_state)

    if agent_name == "hermes":
        return preview_hermes(detected, opts)
    elif agent_name == "openclaw":
        return preview_openclaw(detected, opts)
    else:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}")


@app.delete("/api/agents/import/{agent_name}", dependencies=[Depends(require_api_key)])
async def agents_remove(agent_name: str):
    """Remove all imports for an agent."""
    vault_dir = Path(__file__).parent / "vault"
    from core.agents import remove_import
    import shutil

    deleted_db = remove_import(agent_name)

    for share in [vault_dir / "imports" / agent_name,
                   vault_dir / "skills" / f"{agent_name}-imports"]:
        if share.exists():
            shutil.rmtree(share)

    return {"deleted": deleted_db or True, "agent": agent_name}

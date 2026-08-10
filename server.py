import json
import sqlite3
import asyncio
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import sys
from importlib import resources as importlib_resources

sys.path.insert(0, str(Path(__file__).parent))
from core.database import init_db
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

app = FastAPI(
    title="FreeHand API",
    description="Local AI agent system with Tailscale network access",
    version="0.1.0"
)

DB_PATH = Path(__file__).parent / "agent.db"
VAULT_DIR = Path(__file__).parent / "vault"
SCRIBBLE_PATH = VAULT_DIR / "00_Scribble.md"

try:
    static_path = importlib_resources.files("freehand").joinpath("static").as_posix()
    app.mount("/static", StaticFiles(directory=static_path), name="static")
except Exception:
    pass
app.include_router(oauth_router)


@app.on_event("startup")
async def startup_event():
    init_db()
    VAULT_DIR.mkdir(exist_ok=True)
    if not SCRIBBLE_PATH.exists():
        SCRIBBLE_PATH.write_text("# 00_Scribble\n\n> Agent scratchpad\n\n## Quick Notes\n\n- [ ] Task to remember\n")
    get_scheduler()
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


@app.get("/api/v1/status")
async def api_status():
    return {
        "service": "freehand",
        "version": "0.1.0",
        "access": {
            "local": "http://localhost:8000",
            "tailscale": "http://100.x.y.z:8000"
        }
    }


@app.get("/api/scribble")
async def get_scribble():
    if not SCRIBBLE_PATH.exists():
        return {"content": ""}
    return {"content": SCRIBBLE_PATH.read_text()}


@app.post("/api/scribble")
async def save_scribble(body: dict):
    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Content cannot be empty")
    SCRIBBLE_PATH.write_text(content)
    return {"status": "saved", "path": str(SCRIBBLE_PATH)}


@app.get("/api/tasks")
async def get_tasks():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks ORDER BY id DESC")
    tasks = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return tasks


@app.post("/api/tasks")
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


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "deleted"}


@app.get("/api/approvals")
async def api_get_approvals():
    return get_pending_approvals()


@app.post("/api/approvals/{approval_id}/approve")
async def api_approve(approval_id: int):
    try:
        result = handle_approval(approval_id, "approve")
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/approvals/{approval_id}/deny")
async def api_deny(approval_id: int):
    try:
        result = handle_approval(approval_id, "deny")
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/approvals/clear")
async def api_clear_approvals():
    count = clear_pending_approvals()
    return {"cleared": count}


@app.get("/api/approvals/stream")
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


@app.get("/api/security/tier")
async def get_tier():
    return {"tier": get_current_tier().value}


@app.post("/api/security/tier")
async def set_tier_endpoint(body: dict):
    tier = body.get("tier", "").strip()
    try:
        result = set_tier(tier)
        return {"tier": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/security/check")
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


@app.get("/api/settings")
async def get_settings():
    settings_path = Path(__file__).parent / "vault" / "settings.json"
    if settings_path.exists():
        return json.loads(settings_path.read_text())
    return {"api_key": "", "omniroute": ""}


@app.post("/api/settings")
async def save_settings(body: dict):
    settings_path = Path(__file__).parent / "vault" / "settings.json"
    settings_path.write_text(json.dumps(body))
    return {"status": "saved"}


@app.post("/api/agent/command")
async def agent_command(body: dict):
    command = body.get("command", "").strip()
    source = body.get("source", "")
    caller_id = body.get("caller_id")
    if not command:
        raise HTTPException(status_code=400, detail="Command is required")

    # Log the command
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO memories (path, title, content) VALUES (?, ?, ?)",
        ("/agent/command", "Command: " + command[:50], command)
    )
    conn.commit()
    conn.close()

    # Run the agent
    from core.agent import run_agent
    result = await run_agent(command, source=source, caller_id=caller_id)
    return result


@app.post("/api/memory/sync")
async def memory_sync():
    count = sync_vault_to_sqlite()
    return {"status": "synced", "files": count}


@app.get("/api/memory/search")
async def memory_search(q: str = "", limit: int = 3):
    if not q.strip():
        return []
    results = search_memory(q, limit=limit)
    return results


@app.get("/api/skills")
async def list_skills():
    skills = parse_skills()
    return skills


@app.post("/api/sweep")
async def api_sweep():
    result = process_scribble()
    return result


# ── Gateway / Remote Chat Endpoints ────────────────────────────────────────────

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


@app.post("/api/gateway/whatsapp")
async def whatsapp_webhook(request: Request):
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


@app.get("/api/gateway/status")
async def gateway_status():
    return get_gateway_status()


@app.post("/api/gateway/settings")
async def gateway_settings(body: dict):
    result = update_gateway_settings(body)
    return result


@app.post("/api/gateway/whatsapp/pair")
async def whatsapp_pair(body: dict):
    phone = body.get("phone", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone number required")
    result = await handle_whatsapp_pair(phone)
    return result


# ── Tool Endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/tools/office/read/docx")
async def tool_read_docx(path: str):
    result = read_docx(path)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/tools/office/write/docx")
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


@app.get("/api/tools/office/read/xlsx")
async def tool_read_xlsx(path: str):
    result = read_xlsx(path)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/tools/office/write/xlsx")
async def tool_write_xlsx(body: dict):
    path = body.get("path", "").strip()
    data = body.get("data", {})
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    result = write_xlsx(path, data)
    if "error" in result:
        return result
    return result


@app.get("/api/tools/office/read/pptx")
async def tool_read_pptx(path: str):
    result = read_pptx(path)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.post("/api/tools/office/write/pptx")
async def tool_write_pptx(body: dict):
    path = body.get("path", "").strip()
    slides = body.get("slides", [])
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    result = write_pptx(path, slides)
    if "error" in result:
        return result
    return result


@app.post("/api/tools/browser/axtree")
async def tool_browser_axtree(body: dict):
    url = body.get("url", "").strip()
    mode = body.get("mode", "headless")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: asyncio.run(get_axtree(url, mode=mode)))
    return result


@app.post("/api/tools/browser/text")
async def tool_browser_text(body: dict):
    url = body.get("url", "").strip()
    mode = body.get("mode", "headless")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: asyncio.run(extract_text(url, mode=mode)))
    return result


@app.post("/api/tools/browser/click")
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


@app.post("/api/tools/browser/fill")
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


@app.post("/api/tools/browser/navigate")
async def tool_browser_navigate(body: dict):
    url = body.get("url", "").strip()
    mode = body.get("mode", "headless")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    import asyncio
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: asyncio.run(navigate(url, mode=mode)))
    return result


@app.post("/api/tools/browser/screenshot")
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

@app.get("/api/agents")
async def agents_list():
    """List all detected AI agents."""
    return summarize_agents()


@app.post("/api/agents/detect")
async def agents_detect():
    """Force re-scan for installed agents."""
    return summarize_agents()


@app.get("/api/agents/imported")
async def agents_imported():
    """List all import records."""
    return get_import_status()


@app.post("/api/agents/import")
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


@app.post("/api/agents/import/preview")
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


@app.delete("/api/agents/import/{agent_name}")
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

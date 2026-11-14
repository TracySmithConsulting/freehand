import json
import secrets as _secrets
import sqlite3
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import sys
from importlib import resources as importlib_resources

sys.path.insert(0, str(Path(__file__).parent))
from core.database import init_db, DB_PATH  # DB_PATH is the canonical path from core.database (vault/agent.db)
from core.memory import sync_vault_to_sqlite, search_memory
from core.scheduler import get_scheduler, process_scribble, create_task as create_scheduled_task
from core.security import (
    get_current_tier,
    set_tier,
    intercept_action,
    get_pending_approvals,
    handle_approval,
    clear_pending_approvals,
    re_execute_approval,
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

# R17-P1: read the version from the installed package metadata instead of
# hardcoding it — the hardcoded "0.1.x" here drifted from pyproject.toml
# (which said 0.1.8 while this file said 0.1.0). When the package isn't
# installed (bare test runs), fall back rather than crash.
from importlib.metadata import PackageNotFoundError, version as _pkg_version
try:
    APP_VERSION = _pkg_version("freehand-agent")
except PackageNotFoundError:
    APP_VERSION = "0.1.0"

app = FastAPI(
    title="FreeHand API",
    description="Local AI agent system with Tailscale network access",
    version=APP_VERSION,
)

# DB_PATH is imported from core.database (canonical vault/agent.db path)
VAULT_DIR = Path(__file__).parent / "vault"
SCRIBBLE_PATH = VAULT_DIR / "00_Scribble.md"
# Module-level so tests can monkeypatch it; the /api/settings handlers use it.
VAULT_SETTINGS_PATH = VAULT_DIR / "settings.json"

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


# Chat webhooks: authenticated by their own secret or signature (see each
# handler), never by X-API-Key. Nothing else under /api/gateway/ is exempt.
CHAT_WEBHOOK_PATHS = frozenset({
    "/api/gateway/telegram",
    "/api/gateway/slack",
    "/api/gateway/whatsapp",
})


@app.middleware("http")
async def limit_request_body(request: Request, call_next):
    """Reject requests whose Content-Length exceeds the cap."""
    # Allow gateway webhooks (they may carry Slack/Telegram payloads —
    # typically small but we want to be permissive here so callbacks work).
    if request.url.path in CHAT_WEBHOOK_PATHS:
        return await call_next(request)

    # Health/static: no body expected.
    if request.url.path in ("/", "/health"):
        return await call_next(request)

    cap = _get_max_body_bytes()
    if request.url.path == "/api/voice/transcribe":
        # Recordings from the console's Speak button (key-protected).
        cap = max(cap, 25 * 1024 * 1024)
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
#   POST /api/gateway/telegram      - webhook secret header + allow_from
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
    if path in CHAT_WEBHOOK_PATHS:
        # Telegram/Slack/WhatsApp each have their own auth mechanism. Only
        # these exact paths: this used to skip every /api/gateway/* path,
        # so the gateway status, settings form and WhatsApp pairing needed
        # no key at all.
        return

    expected = get_or_create_api_key()

    # Try X-API-Key first, then Authorization: Bearer
    provided = request.headers.get("X-API-Key", "").strip()
    if not provided:
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()

    if not provided or not _secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")

# ── The memory service's AI model, through FreeHand ───────────────────
# Hindsight needs an AI model to pick facts out of conversations. Rather
# than asking people to set a model up twice, Hindsight (in compose) calls
# this OpenAI-compatible endpoint, and FreeHand passes the request on to
# whatever model is set up in FreeHand - so changing the model in Settings
# changes it for memory too. Guarded by FREEHAND_LLM_PROXY_TOKEN (shared
# with Hindsight through .env); without that token the endpoint is off.

def _llm_proxy_allowed(request: Request) -> bool:
    import os as _os
    expected = _os.environ.get("FREEHAND_LLM_PROXY_TOKEN", "").strip()
    auth = request.headers.get("authorization", "").strip()
    provided = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return bool(expected) and bool(provided) and _secrets.compare_digest(provided, expected)


@app.get("/internal/llm/v1/models")
async def llm_proxy_models(request: Request):
    if not _llm_proxy_allowed(request):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    from core.agent_config import get_llm_config
    model = get_llm_config().get("model", "")
    return {"object": "list", "data": [{"id": model, "object": "model", "owned_by": "freehand"}]}


@app.post("/internal/llm/v1/chat/completions")
async def llm_proxy_chat(request: Request):
    import aiohttp
    from fastapi.responses import Response, StreamingResponse
    from core import llm_providers
    from core.agent_config import get_llm_chain, resolve_llm_endpoint

    if not _llm_proxy_allowed(request):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    chain = get_llm_chain()
    if not chain:
        raise HTTPException(status_code=503, detail="No AI model is set up in FreeHand yet (Settings, AI model).")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    config = chain[0]
    body["model"] = config.get("model") or body.get("model", "")
    base_url, headers = resolve_llm_endpoint(config)
    url = f"{base_url}/chat/completions"
    timeout = aiohttp.ClientTimeout(total=300)

    if not body.get("stream"):
        # The main model, then the backups, like FreeHand's own replies.
        last_status, last_content, last_type = 502, b'{"error": "No AI model answered"}', "application/json"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            for cfg in chain:
                attempt = {**body, "model": cfg.get("model") or body.get("model", "")}
                if llm_providers.omit_temperature(cfg.get("provider", ""), cfg.get("base_url", "")):
                    attempt.pop("temperature", None)
                cfg_base, cfg_headers = resolve_llm_endpoint(cfg)
                try:
                    async with session.post(f"{cfg_base}/chat/completions", json=attempt, headers=cfg_headers) as upstream:
                        last_status = upstream.status
                        last_content = await upstream.read()
                        last_type = upstream.headers.get("Content-Type", "application/json")
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    last_status, last_content = 502, b'{"error": "Couldn\'t reach the AI model"}'
                    continue
                if last_status == 200:
                    break
                if 400 <= last_status < 500 and last_status not in (401, 402, 403, 404, 408, 429):
                    break  # the request itself was refused; another model won't help
        return Response(content=last_content, status_code=last_status, media_type=last_type)

    if body.get("stream"):
        session = aiohttp.ClientSession(timeout=timeout)
        upstream = await session.post(url, json=body, headers=headers)

        async def relay():
            try:
                async for chunk in upstream.content.iter_any():
                    yield chunk
            finally:
                upstream.release()
                await session.close()

        # Streams can't switch models halfway, so they use the main model.
        return StreamingResponse(relay(), status_code=upstream.status,
                                 media_type=upstream.headers.get("Content-Type", "text/event-stream"))


# ── Voice (core/voice.py): speech to text on this computer ────────────

@app.get("/api/voice/status", dependencies=[Depends(require_api_key)])
async def voice_status():
    from core import voice
    return {"available": voice.available()}


@app.post("/api/voice/transcribe", dependencies=[Depends(require_api_key)])
async def voice_transcribe(request: Request):
    """The console's Speak button sends its recording here (raw audio body,
    e.g. audio/webm from the browser) and gets the words back."""
    from core import voice
    data = await request.body()
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    suffix = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".mp4",
              "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav"}.get(ctype, ".webm")
    result = await asyncio.to_thread(voice.transcribe, data, suffix)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ── Easier AI model setup (core/llm_providers.py) ─────────────────────

@app.get("/api/llm/providers", dependencies=[Depends(require_api_key)])
async def llm_providers_list():
    from core import llm_providers
    from core.agent_config import get_llm_config
    cfg = get_llm_config()
    return {
        "providers": llm_providers.public_list(),
        "current": llm_providers.identify(cfg.get("provider", ""), cfg.get("base_url", "")),
    }


@app.post("/api/llm/detect", dependencies=[Depends(require_api_key)])
async def llm_detect(request: Request):
    """Which service is this key for, and which models does it offer?"""
    from core import llm_providers
    body = await request.json() or {}
    return await asyncio.to_thread(
        llm_providers.detect, str(body.get("api_key", "")), str(body.get("provider", "")))


@app.post("/api/llm/models", dependencies=[Depends(require_api_key)])
async def llm_models(request: Request):
    from core import llm_providers
    from core.agent_config import get_llm_config
    body = await request.json() or {}
    provider, base_url = str(body.get("provider", "")), str(body.get("base_url", ""))
    key = str(body.get("api_key", ""))
    if not key:
        # The page doesn't have to resend a saved key - but only the key
        # saved for THIS service. (It used to send the main model's key to
        # whichever service was chosen.)
        main = get_llm_config()
        same = (llm_providers.identify(main.get("provider", ""), main.get("base_url", ""))
                == llm_providers.identify(provider, base_url)
                and llm_providers.base_url_for(main.get("provider", ""), main.get("base_url", ""))
                == llm_providers.base_url_for(provider, base_url))
        key = main.get("api_key", "") if same else ""
    models, problem = await asyncio.to_thread(llm_providers.list_models, provider, key, base_url)
    return {"models": models or [], "error": problem}


@app.get("/api/llm/catalog", dependencies=[Depends(require_api_key)])
async def llm_catalog_search(q: str = ""):
    """Search models across every service with a saved key, plus
    OpenRouter's public list, grouped by service (core/llm_catalog.py)."""
    from core import llm_catalog
    return await asyncio.to_thread(llm_catalog.search, q[:100])


@app.post("/api/llm/pick", dependencies=[Depends(require_api_key)])
async def llm_catalog_pick(request: Request):
    """Test a model chosen in the search, or make it the main model or a
    backup, reusing the key saved for its service."""
    from core import llm_catalog
    body = await request.json() or {}
    action = str(body.get("action", "test"))
    if action not in ("test", "main", "backup"):
        raise HTTPException(status_code=400, detail="action must be test, main or backup")
    result = await asyncio.to_thread(
        llm_catalog.pick, str(body.get("provider", "")), str(body.get("model", "")),
        str(body.get("base_url", "")), action)
    if action in ("main", "backup") and result.get("ok"):
        # Like saving a key in Settings: wake the task runner now.
        from core.scheduler import run_due_tasks
        asyncio.get_running_loop().create_task(asyncio.to_thread(run_due_tasks))
    return result


@app.post("/api/llm/test", dependencies=[Depends(require_api_key)])
async def llm_test(request: Request):
    """Try these settings with one tiny message, before saving them."""
    from core import llm_providers
    body = await request.json() or {}
    return await asyncio.to_thread(
        llm_providers.test, str(body.get("provider", "")), str(body.get("model", "")).strip(),
        str(body.get("api_key", "")).strip(), str(body.get("base_url", "")).strip())


# Backup models: tried in order when the main model fails (core/agent.py
# call_llm). Keys are never sent back to the page; it sees the last four
# characters only.

def _backup_view(backups: list) -> list:
    from core import llm_providers
    out = []
    for i, b in enumerate(backups):
        p = llm_providers.get(llm_providers.identify(b.get("provider", ""), b.get("base_url", "")))
        key = b.get("api_key", "")
        out.append({
            "index": i,
            "service": p["name"] if p else b.get("provider", ""),
            "provider": b.get("provider", ""),
            "model": b.get("model", ""),
            "base_url": b.get("base_url", ""),
            "key_hint": ("ending " + key[-4:]) if len(key) >= 8 else ("set" if key else "none"),
        })
    return out


@app.get("/api/llm/backups", dependencies=[Depends(require_api_key)])
async def llm_backups_list():
    from core import agent
    from core.agent_config import get_llm_backups
    return {"backups": _backup_view(get_llm_backups()), "last_switch": dict(agent.LAST_SWITCH)}


@app.post("/api/llm/backups", dependencies=[Depends(require_api_key)])
async def llm_backups_add(request: Request):
    from core.agent_config import add_llm_backup
    body = await request.json() or {}
    if not str(body.get("model", "")).strip():
        raise HTTPException(status_code=400, detail="Choose a model for the backup first.")
    return {"backups": _backup_view(add_llm_backup(body))}


@app.delete("/api/llm/backups/{index}", dependencies=[Depends(require_api_key)])
async def llm_backups_remove(index: int):
    from core.agent_config import remove_llm_backup
    return {"backups": _backup_view(remove_llm_backup(index))}


@app.post("/api/llm/backups/{index}/move", dependencies=[Depends(require_api_key)])
async def llm_backups_move(index: int, request: Request):
    from core.agent_config import move_llm_backup
    body = await request.json() or {}
    delta = -1 if str(body.get("direction", "")) == "up" else 1
    return {"backups": _backup_view(move_llm_backup(index, delta))}


@app.post("/api/llm/backups/{index}/promote", dependencies=[Depends(require_api_key)])
async def llm_backups_promote(index: int):
    from core.agent_config import promote_llm_backup
    return {"backups": _backup_view(promote_llm_backup(index))}


@app.post("/api/llm/backups/{index}/test", dependencies=[Depends(require_api_key)])
async def llm_backups_test(index: int):
    from core import llm_providers
    from core.agent_config import get_llm_backups
    backups = get_llm_backups()
    if not 0 <= index < len(backups):
        raise HTTPException(status_code=404, detail="No such backup.")
    b = backups[index]
    return await asyncio.to_thread(llm_providers.test, b["provider"], b["model"], b["api_key"], b["base_url"])


# OpenRouter sign-in (OAuth with PKCE, https://openrouter.ai/docs/use-cases/oauth-pkce):
# the console asks for a sign-in link (key-protected, so only the owner can
# start one), opens it, OpenRouter returns to the callback with a one-time
# code, and FreeHand swaps it for a key and saves it. The callback only
# accepts a state FreeHand handed out in the last 10 minutes, so another
# site can't plant its own key here.
_OPENROUTER_PENDING: dict = {}


@app.post("/api/llm/openrouter/start", dependencies=[Depends(require_api_key)])
async def openrouter_start(request: Request):
    import base64
    import hashlib
    import time
    import urllib.parse
    now = time.time()
    for s, v in list(_OPENROUTER_PENDING.items()):
        if now - v["at"] > 600:
            _OPENROUTER_PENDING.pop(s, None)
    verifier = _secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = _secrets.token_urlsafe(24)
    _OPENROUTER_PENDING[state] = {"verifier": verifier, "at": now}
    # OpenRouter allows localhost callbacks on any port.
    port = request.url.port or 8000
    callback = f"http://localhost:{port}/api/llm/openrouter/callback"
    url = "https://openrouter.ai/auth?" + urllib.parse.urlencode({
        "callback_url": callback, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state, "key_label": "FreeHand",
    })
    return {"url": url}


def _signin_page(title: str, message: str, ok: bool) -> "HTMLResponse":
    import html as _html
    from fastapi.responses import HTMLResponse
    close = "<script>setTimeout(function(){window.close();},4000);</script>" if ok else ""
    return HTMLResponse(
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{_html.escape(title)}</title></head>"
        "<body style='font-family:system-ui;padding:2rem;max-width:40rem'>"
        f"<h1>{_html.escape(title)}</h1><p role='status'>{_html.escape(message)}</p>"
        "<p><button type='button' onclick='window.close()'>Close this window</button></p>"
        f"{close}</body></html>",
        status_code=200 if ok else 400,
    )


@app.get("/api/llm/openrouter/callback")
async def openrouter_callback(code: str = "", state: str = "", error: str = ""):
    import aiohttp
    from core import llm_providers
    from core.agent_config import get_llm_config, set_llm_config
    pending = _OPENROUTER_PENDING.pop(state, None) if state else None
    if error or not code:
        return _signin_page("Sign-in cancelled", "OpenRouter didn't sign you in. Close this window and try again.", False)
    if not pending:
        return _signin_page("Sign-in expired", "This sign-in link is old or wasn't started from FreeHand. Close this window and start again from Settings.", False)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post("https://openrouter.ai/api/v1/auth/keys", json={
                "code": code, "code_verifier": pending["verifier"], "code_challenge_method": "S256",
            }) as resp:
                data = await resp.json(content_type=None) if resp.status == 200 else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        data = None
    key = (data or {}).get("key") if isinstance(data, dict) else None
    if not key:
        return _signin_page("Sign-in didn't finish", "FreeHand couldn't get a key from OpenRouter. Close this window and try again.", False)
    from core.agent_config import get_llm_backups, llm_is_configured, _save_llm_backups
    current = get_llm_config()
    was_openrouter = llm_providers.identify(current.get("provider", ""), current.get("base_url", "")) == "openrouter"
    keep_model = current.get("model") if was_openrouter else ""
    if not was_openrouter and llm_is_configured(current):
        # Don't throw away the model people had: it becomes the first backup.
        _save_llm_backups([{k: current.get(k, "") for k in ("provider", "model", "api_key", "base_url")},
                           *get_llm_backups()])
    set_llm_config({"provider": "openrouter", "api_key": key, "base_url": "",
                    "model": keep_model or llm_providers.get("openrouter")["default_model"]})
    # Like saving a key in Settings: wake the task runner now.
    from core.scheduler import run_due_tasks
    asyncio.get_running_loop().create_task(asyncio.to_thread(run_due_tasks))
    return _signin_page("Signed in with OpenRouter",
                        "FreeHand can now use OpenRouter. Close this window and choose a model in Settings.", True)


_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _console_auto_key_allowed(request: Request) -> bool:
    """Should this request get the API key without typing it?

    Yes only for FreeHand's own console opened on this computer:
    - FREEHAND_CONSOLE_AUTO_KEY isn't "0" (hosted tenants set it to 0);
    - the address the browser used is this computer (Host 127.0.0.1,
      localhost or [::1]). A web page on another site can't pretend
      otherwise: a DNS-rebinding page keeps its own host name;
    - the browser says the request comes from the same page origin
      (Sec-Fetch-Site / Origin), so another site can't ask for it.
      Browsers also stop other sites reading the answer (no CORS).
    FreeHand's port is bound to 127.0.0.1 in compose, so other machines
    can't reach it at all. Expose it further and set
    FREEHAND_CONSOLE_AUTO_KEY=0.
    """
    import os as _os
    if _os.environ.get("FREEHAND_CONSOLE_AUTO_KEY", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    host = request.headers.get("host", "").strip().lower()
    if host.startswith("["):
        hostname = host[1:].split("]", 1)[0]
    else:
        hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if hostname not in _LOCAL_HOSTS:
        return False
    site = request.headers.get("sec-fetch-site", "").strip().lower()
    if site and site != "same-origin":
        return False
    origin = request.headers.get("origin", "").strip().lower()
    if origin and origin not in (f"http://{host}", f"https://{host}"):
        return False
    return True


@app.get("/api/console-key")
async def console_key(request: Request):
    """The console on this computer gets the key automatically, so nobody
    has to find and paste it (granny test). See _console_auto_key_allowed."""
    from fastapi.responses import JSONResponse
    if not _console_auto_key_allowed(request):
        return JSONResponse(
            {"detail": "Enter FreeHand's API key under Settings to use this console."},
            status_code=403, headers={"Cache-Control": "no-store"},
        )
    return JSONResponse({"key": get_or_create_api_key()}, headers={"Cache-Control": "no-store"})


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


_telegram_poll_task = None


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
    # R17-P2: repair legacy task rows (NULL next_run + duplicates) once
    # per boot, after the DB is initialized. Idempotent by design.
    from core.scheduler import backfill_next_run
    backfill_next_run()
    get_scheduler()
    # Telegram without a public address: poll for messages (it idles until
    # a bot token is saved). FREEHAND_TELEGRAM_POLLING=0 turns it off.
    import os as _os
    if _os.environ.get("FREEHAND_TELEGRAM_POLLING", "1") != "0":
        from core import telegram_poll
        global _telegram_poll_task
        _telegram_poll_task = asyncio.get_running_loop().create_task(telegram_poll.run_forever())
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
    from core.tools import composio_registry as _composio_tool_registry
    _composio_tool_registry.bootstrap()
    # Encrypt any legacy plaintext client_secret in broker_config.json
    # (providers block). Idempotent — no-op once migrated.
    oauth_broker.migrate_plaintext_secrets()
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
        # "no-cache" = check with FreeHand each time (cheap: unchanged pages
        # come back as 304). Without it, browsers kept showing the console
        # from before an update, so fixes seemed not to work.
        return FileResponse(str(static_path), headers={"Cache-Control": "no-cache"})
    except Exception:
        raise HTTPException(status_code=404, detail="Static files not found")


@app.get("/health")
async def health_check():
    # datetime.utcnow() is deprecated in Python 3.12+; utcnow() returns a
    # naive datetime, so keep the same naive-UTC shape via timezone.utc.
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }


@app.get("/api/v1/status", dependencies=[Depends(require_api_key)])
async def api_status():
    return {
        "service": "freehand",
        "version": APP_VERSION,
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
    created = create_scheduled_task(title, cron_schedule)
    if not created["recurring"]:
        raise HTTPException(
            status_code=400,
            detail=f"'{cron_schedule}' is not a valid 5-field cron schedule",
        )
    return created


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
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # Approving in the web console re-runs the exact held tool call,
    # whatever channel requested it — this is the trusted surface. Chat
    # gateways approve via core.security.approve_from_remote, which only
    # releases the caller's own, registered, non-destructive requests.
    result["re_execute"] = await re_execute_approval(approval_id)
    return result


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
    "telegram_webhook_secret", "telegram_allow_from", "allowed_read_dirs",
    "allowed_write_dirs",
    "slack", "slack_bot_token", "slack_webhook_url", "slack_signing_secret",
    "slack_allow_from", "whatsapp", "whatsapp_enabled", "whatsapp_pairing_phone",
    "bridge_secret", "whatsapp_allow_from", "whatsapp_allow_self", "public_base_url",
    "sweep_interval", "allowed_browser_domains", "user",
    "max_request_body_bytes", "max_screenshot_size_mb",
    "memory",  # long-term memory: hindsight_url, bank_id, enabled
    "voice",  # speech to text: model, language
    "telegram_mode",  # "polling" (default) or "webhook"
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
    if VAULT_SETTINGS_PATH.exists():
        return json.loads(VAULT_SETTINGS_PATH.read_text())
    return {"api_key": "", "omniroute": ""}


@app.post("/api/settings", dependencies=[Depends(require_api_key)])
async def save_settings(body: dict):
    existing = {}
    if VAULT_SETTINGS_PATH.exists():
        try:
            existing = json.loads(VAULT_SETTINGS_PATH.read_text())
        except Exception:
            pass
    merged, rejected = _merge_settings(existing, body)
    VAULT_SETTINGS_PATH.write_text(json.dumps(merged))
    # R17-P5: a newly saved LLM key previously meant the task runner sat idle
    # until the next scheduler tick. Kick a due-tasks pass off in the
    # background (run_due_tasks is thread-safe and idles cleanly when the
    # config still isn't usable) so the first saved key gives immediate
    # feedback. Non-fatal if there's no running loop (bare test calls).
    llm = body.get("llm") if isinstance(body.get("llm"), dict) else {}
    runner_triggered = bool(
        str(llm.get("api_key", "")).strip() or str(llm.get("base_url", "")).strip()
    )
    if runner_triggered:
        from core.scheduler import run_due_tasks
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            runner_triggered = False
        else:
            loop.create_task(asyncio.to_thread(run_due_tasks))
    return {"status": "saved", "rejected": rejected, "runner_triggered": runner_triggered}


@app.get("/api/runner/status", dependencies=[Depends(require_api_key)])
async def runner_status():
    """One-shot task-runner health for the Tasks tab: is an LLM even
    configured? Mirrors exactly the check run_due_tasks() uses, so the UI
    notices 'runner idle — no LLM configured' the moment it matters.
    """
    from core import agent_config as _ac
    cfg = _ac.get_llm_config()
    if not _ac.llm_is_configured(cfg):
        # No main model, but a backup still keeps the runner going.
        backups = _ac.get_llm_backups()
        cfg = backups[0] if backups else None
    configured = cfg is not None
    return {
        "llm_configured": configured,
        "provider": cfg.get("provider") if configured else None,
        "model": cfg.get("model") if configured else None,
    }


@app.post("/api/agent/command", dependencies=[Depends(require_api_key)])
async def agent_command(body: dict):
    command = body.get("command", "").strip()
    source = body.get("source", "")
    caller_id = body.get("caller_id")
    if not command:
        raise HTTPException(status_code=400, detail="Command is required")

    # Records a truncated copy (N3) and runs the agent loop.
    from core.agent import run_command
    result = await run_command(command, source=source, caller_id=caller_id)
    return result


# ── Long-term memory (Hindsight): what FreeHand remembers ──────────────
# The Memory page lists the facts FreeHand has learned, in plain sentences,
# and lets people correct, forget or add them (core/long_memory.py).

@app.get("/api/memory/status", dependencies=[Depends(require_api_key)])
async def memory_status():
    from core import long_memory
    return await asyncio.to_thread(long_memory.status)


@app.get("/api/memory/facts", dependencies=[Depends(require_api_key)])
async def memory_facts(q: str = "", offset: int = 0, limit: int = 50):
    from core import long_memory
    return await asyncio.to_thread(
        long_memory.list_facts, max(1, min(limit, 200)), max(0, offset), q.strip()[:200])


@app.post("/api/memory/facts", dependencies=[Depends(require_api_key)])
async def memory_add_fact(request: Request):
    from core import long_memory
    body = await request.json()
    text = str((body or {}).get("text", "")).strip()[:2000]
    # Wait for Hindsight, so the new memory is listed when the page reloads.
    result = await asyncio.to_thread(long_memory.retain, text, "Added on the Memory page", True)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.put("/api/memory/facts/{memory_id}", dependencies=[Depends(require_api_key)])
async def memory_update_fact(memory_id: str, request: Request):
    from core import long_memory
    body = await request.json()
    result = await asyncio.to_thread(long_memory.update_fact, memory_id, str((body or {}).get("text", "")))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.delete("/api/memory/facts/{memory_id}", dependencies=[Depends(require_api_key)])
async def memory_forget_fact(memory_id: str):
    from core import long_memory
    result = await asyncio.to_thread(long_memory.forget_fact, memory_id)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
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


@app.get("/api/memory/files", dependencies=[Depends(require_api_key)])
async def memory_files():
    """List vault .md files known to the FTS index (Journal, Threads,
    notes) — the Memory panel's browsing surface. Callers should hit
    /api/memory/sync first (the UI does) so fresh files appear.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    vault_resolved = VAULT_DIR.resolve()
    try:
        rows = conn.execute(
            "SELECT path, title, updated_at FROM memories "
            "ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
        # Only offer rows whose file still exists in the vault — stale
        # index entries (e.g. legacy test writes to fake paths like
        # "/agent/command") can never be Opened and 404 noisily.
        out = []
        vault_parent = vault_resolved.parent
        for row in rows:
            candidate = (vault_parent / row["path"]).resolve()
            if candidate.is_file() and candidate.suffix == ".md":
                try:
                    candidate.relative_to(vault_resolved)
                except ValueError:
                    continue
                out.append(dict(row))
        return out
    finally:
        conn.close()


@app.get("/api/memory/file", dependencies=[Depends(require_api_key)])
async def memory_file(path: str = ""):
    """Read one indexed vault .md file's content for the Memory panel.

    The path must be exactly a relative path that is already in the
    index — resolve()ed locations outside vault/ are rejected, so this
    is no arbitrary-file reader.
    """
    rel = (path or "").strip()
    if not rel:
        raise HTTPException(status_code=400, detail="Missing path parameter")
    vault_resolved = VAULT_DIR.resolve()
    # memories.path entries are stored relative to the project root
    # ("vault/Journal/2026-10.md"), so anchor the candidate at the vault's
    # parent and then prove it lands back inside vault/. Works under the
    # tmp_path monkeypatch too, where vault/ lives somewhere else entirely.
    candidate = (vault_resolved.parent / rel).resolve()
    try:
        candidate.relative_to(vault_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path escapes the vault")
    if candidate.suffix != ".md":
        raise HTTPException(status_code=400, detail="Only .md vault files are readable")
    if not candidate.exists():
        # Index and disk drifted; tell the caller to re-sync.
        raise HTTPException(status_code=404, detail="File not found — try Sync, then search again")
    return {"path": rel, "title": candidate.stem, "content": candidate.read_text(encoding="utf-8", errors="replace")}

# ── Skills (core/skills.py): drafts earn their place ──────────────────

@app.get("/api/skills", dependencies=[Depends(require_api_key)])
async def list_skills():
    """Every skill with its status: draft, active or archived."""
    from core import skills
    return await asyncio.to_thread(skills.scan)


@app.get("/api/skills/{skill_id}", dependencies=[Depends(require_api_key)])
async def read_skill(skill_id: int):
    from core import skills
    s = await asyncio.to_thread(skills.read, skill_id)
    if not s:
        raise HTTPException(status_code=404, detail="No such skill.")
    return s


@app.post("/api/skills/{skill_id}/status", dependencies=[Depends(require_api_key)])
async def set_skill_status(skill_id: int, request: Request):
    from core import skills
    body = await request.json()
    status = str((body or {}).get("status", ""))
    reasons = {"active": "Approved by you", "archived": "Archived by you", "draft": "Restored by you"}
    result = await asyncio.to_thread(skills.set_status, skill_id, status, reasons.get(status, ""))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.delete("/api/skills/{skill_id}", dependencies=[Depends(require_api_key)])
async def delete_skill(skill_id: int):
    from core import skills
    result = await asyncio.to_thread(skills.delete, skill_id)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


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

# Telegram signs nothing, so the webhook must be registered with a
# secret_token (setWebhook ... secret_token=<telegram_webhook_secret>);
# Telegram then sends it back in X-Telegram-Bot-Api-Secret-Token on every
# update. Without this check, anyone who can reach this URL could forge an
# update from an allowed chat_id (chat ids are not secrets). Closed when
# the secret is unset, same as the WhatsApp bridge.
@app.post("/api/gateway/telegram")
async def telegram_webhook(request: Request):
    settings = _load_settings_for_auth()
    expected = str(settings.get("telegram_webhook_secret", "")).strip()
    if not expected:
        return JSONResponse(
            {"error": "telegram_webhook_secret not configured in settings.json"},
            status_code=503,
        )
    provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "").strip()
    if not provided or not _secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing Telegram secret token")
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
    audio = data.get("audio") if isinstance(data.get("audio"), dict) else None
    if not remote_jid or not (text or audio):
        return {"error": "remote_jid and text (or audio) required"}
    result = await handle_whatsapp_message(
        remote_jid, text, data.get("message_id"), from_self=data.get("from_self") is True,
        audio=audio,
    )
    return result


@app.get("/api/gateway/status", dependencies=[Depends(require_api_key)])
async def gateway_status():
    return get_gateway_status()


@app.post("/api/gateway/whatsapp/power", dependencies=[Depends(require_api_key)])
async def whatsapp_power(request: Request):
    """The console's WhatsApp on/off switch (pauses or resumes the bridge)."""
    from core.gateway import set_whatsapp_power
    body = await request.json() or {}
    return await set_whatsapp_power(body.get("on") is True)


@app.post("/api/gateway/settings", dependencies=[Depends(require_api_key)])
async def gateway_settings(body: dict):
    try:
        return update_gateway_settings(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/gateway/whatsapp/pair", dependencies=[Depends(require_api_key)])
async def whatsapp_pair(body: dict):
    phone = body.get("phone", "").strip()
    if not phone:
        raise HTTPException(status_code=400, detail="phone number required")
    result = await handle_whatsapp_pair(phone)
    return result


# ── Tool Endpoints ─────────────────────────────────────────────────────────────

def _require_readable(path: str) -> None:
    """403 unless the path is inside the vault or settings.allowed_read_dirs."""
    from core.tools.office import check_read_allowed
    denied = check_read_allowed(path)
    if denied:
        raise HTTPException(status_code=403, detail=denied)


@app.get("/api/tools/office/read/docx", dependencies=[Depends(require_api_key)])
async def tool_read_docx(path: str):
    _require_readable(path)
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
    _require_readable(path)
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
    _require_readable(path)
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

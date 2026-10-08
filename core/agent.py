import json
import asyncio
from typing import Dict, List, Optional, Any
from datetime import datetime

import aiohttp

from core.agent_config import get_llm_config, build_system_prompt, list_available_tools
from core.security import (
    get_current_tier,
    intercept_action,
    PermissionTier,
    REMOTE_SOURCES,
)
from core.tools.office import read_docx, write_docx, read_xlsx, read_pptx
from core.tools.browser import (
    get_axtree, extract_text, screenshot, navigate, click, fill,
    get_validation_summary,
    start_request_capture, get_captured_requests, stop_request_capture,
    save_checkpoint, restore_checkpoint, list_checkpoints, delete_checkpoint,
)
from core.tools.integrations import (
    read_email, list_calendar_events, read_sheets, read_sheet_range,
    list_zoom_meetings, schedule_zoom_meeting,
    list_facebook_pages, post_to_facebook, list_instagram_accounts,
    post_to_instagram, list_zoom_recordings, get_connections_summary,
)
from core.memory import search_memory


async def call_llm(messages: List[Dict], tools: List[Dict] = None, config: dict = None) -> dict:
    """Call an OpenAI-compatible API and return the response.

    H1 fix: 3-attempt retry with exponential backoff (2s, 4s, 8s) on
    transient errors (HTTP 429/5xx, timeouts, connection errors). Auth
    errors (401/403) and client errors (400) are NOT retried — they
    indicate the request itself is wrong.
    """
    if config is None:
        config = get_llm_config()

    provider = config.get("provider", "openai")
    model = config.get("model", "gpt-4o-mini")
    api_key = config.get("api_key", "")
    base_url = config.get("base_url", "")
    temperature = config.get("temperature", 0.7)
    max_tokens = config.get("max_tokens", 4096)

    if provider == "openai" and not base_url:
        base_url = "https://api.openai.com/v1"
    elif provider == "google":
        if not base_url:
            base_url = "https://generativelanguage.googleapis.com/v1beta/openai"
        if api_key:
            base_url = base_url.replace("{API_KEY}", api_key)
            api_key = ""

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = aiohttp.ClientTimeout(total=120)

    # H1: retry configuration
    RETRY_BACKOFFS = [0, 2, 4]  # seconds; first attempt has 0s delay
    RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

    last_error = None
    for attempt, delay in enumerate(RETRY_BACKOFFS):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("choices", [{}])[0].get("message", {})
                    # Read error body for context
                    try:
                        body_text = (await resp.text())[:200]
                    except Exception:
                        body_text = ""
                    last_error = f"LLM API error {resp.status}: {body_text}"
                    # Only retry on retryable status codes
                    if resp.status not in RETRYABLE_STATUS:
                        return {"error": last_error}
                    # Otherwise, fall through to next retry
        except asyncio.TimeoutError:
            last_error = "LLM request timed out (120s)"
        except aiohttp.ClientError as e:
            last_error = f"LLM connection error: {str(e)[:200]}"
        except Exception as e:
            # Non-retryable exception (programmer error, not network)
            return {"error": f"LLM request failed: {str(e)[:200]}"}

    # All retries exhausted
    return {"error": last_error or "LLM request failed after retries"}


def parse_tool_call(message: dict) -> Optional[dict]:
    """Extract tool call from an LLM response message."""
    tool_calls = message.get("tool_calls", [])
    if not tool_calls:
        return None
    tc = tool_calls[0]
    name = tc.get("function", {}).get("name", "")
    args_str = tc.get("function", {}).get("arguments", "{}")
    try:
        args = json.loads(args_str)
    except json.JSONDecodeError:
        args = {}
    return {"id": tc.get("id", ""), "name": name, "args": args}


async def execute_tool(name: str, args: dict) -> dict:
    """Execute a tool by name with the given arguments."""
    try:
        if name == "read_docx":
            result = read_docx(args.get("path", ""))
            return {"content": json.dumps(result, default=str)}
        elif name == "write_docx":
            result = write_docx(args.get("path", ""), args.get("title", ""), args.get("content", []))
            return {"content": json.dumps(result, default=str)}
        elif name == "read_xlsx":
            result = read_xlsx(args.get("path", ""))
            return {"content": json.dumps(result, default=str)}
        elif name == "read_pptx":
            result = read_pptx(args.get("path", ""))
            return {"content": json.dumps(result, default=str)}
        elif name == "get_axtree":
            import asyncio as _asyncio
            result = await get_axtree(args.get("url", ""), mode=args.get("mode", "headless"))
            return {"content": json.dumps(result, default=str)}
        elif name == "extract_text":
            import asyncio as _asyncio
            result = await extract_text(args.get("url", ""), mode=args.get("mode", "headless"))
            return {"content": json.dumps(result, default=str)}
        elif name == "take_screenshot":
            import asyncio as _asyncio
            result = await screenshot(args.get("url", ""), mode=args.get("mode", "headless"), full_page=args.get("full_page", False))
            return {"content": json.dumps(result, default=str)}
        elif name == "read_email":
            result = await read_email(
                service=args.get("service", "google"),
                label=args.get("label", "default"),
                filter_query=args.get("filter", ""),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "list_calendar_events":
            result = await list_calendar_events(
                service=args.get("service", "google"),
                label=args.get("label", "default"),
                from_dt=args.get("from", ""),
                to_dt=args.get("to", ""),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "read_sheets":
            result = await read_sheets(
                label=args.get("label", "default"),
                spreadsheet_id=args.get("spreadsheet_id", ""),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "read_sheet_range":
            result = await read_sheet_range(
                label=args.get("label", "default"),
                spreadsheet_id=args.get("spreadsheet_id", ""),
                range_str=args.get("range", ""),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "list_zoom_meetings":
            result = await list_zoom_meetings(
                label=args.get("label", "default"),
                page_size=args.get("page_size", 30),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "schedule_zoom_meeting":
            result = await schedule_zoom_meeting(
                label=args.get("label", "default"),
                title=args.get("title", ""),
                start_time=args.get("start_time", ""),
                duration=args.get("duration", 60),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "list_facebook_pages":
            result = await list_facebook_pages(label=args.get("label", "default"))
            return {"content": json.dumps(result, default=str)}
        elif name == "post_to_facebook":
            result = await post_to_facebook(
                label=args.get("label", "default"),
                page_id=args.get("page_id", ""),
                content=args.get("content", ""),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "list_instagram_accounts":
            result = await list_instagram_accounts(label=args.get("label", "default"))
            return {"content": json.dumps(result, default=str)}
        elif name == "post_to_instagram":
            result = await post_to_instagram(
                label=args.get("label", "default"),
                content=args.get("content", ""),
                media_url=args.get("media_url", ""),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "list_zoom_recordings":
            result = await list_zoom_recordings(label=args.get("label", "default"))
            return {"content": json.dumps(result, default=str)}
        elif name == "list_connections":
            result = get_connections_summary()
            return {"content": json.dumps(result, default=str)}
        elif name == "search_memory":
            results = search_memory(args.get("query", ""), limit=args.get("limit", 5))
            return {"content": json.dumps(results, default=str)}

        # ── Browser v2 tools ───────────────────────────────────────────
        elif name == "navigate":
            result = await navigate(args.get("url", ""), mode=args.get("mode", "headless"))
            return {"content": json.dumps(result, default=str)}
        elif name == "click":
            result = await click(
                selector=args.get("selector", ""),
                url=args.get("url", ""),
                mode=args.get("mode", "headless"),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "fill":
            result = await fill(
                url=args.get("url", ""),
                selector=args.get("selector", ""),
                value=args.get("value", ""),
                mode=args.get("mode", "headless"),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "get_validation_summary":
            result = await get_validation_summary(
                args.get("url", ""), mode=args.get("mode", "headless")
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "start_request_capture":
            result = start_request_capture(url_pattern=args.get("url_pattern"))
            return {"content": json.dumps(result, default=str)}
        elif name == "get_captured_requests":
            result = get_captured_requests(limit=args.get("limit", 50))
            return {"content": json.dumps(result, default=str)}
        elif name == "stop_request_capture":
            result = stop_request_capture()
            return {"content": json.dumps(result, default=str)}
        elif name == "save_checkpoint":
            result = await save_checkpoint(
                name=args.get("name", ""),
                url=args.get("url", ""),
                mode=args.get("mode", "headless"),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "restore_checkpoint":
            result = await restore_checkpoint(
                name=args.get("name", ""),
                url=args.get("url"),
                mode=args.get("mode", "headless"),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "list_checkpoints":
            result = list_checkpoints()
            return {"content": json.dumps(result, default=str)}
        elif name == "delete_checkpoint":
            result = delete_checkpoint(name=args.get("name", ""))
            return {"content": json.dumps(result, default=str)}
        # ── Round 11: dynamic OC tool dispatch ────────────────
        # Routes any tool name starting with 'oc_' to the registry's
        # dynamic dispatch. Static tools (read_docx, navigate, etc.)
        # keep their explicit branches above. Round 14 dropped the
        # static GitHub tools — GitHub now goes through the OC path
        # (oc_github_default_* tools).
        if name.startswith("oc_"):
            from core.tools import dispatch as _oc_dispatch
            oc_result = _oc_dispatch.dispatch_oc_tool(name, args)
            if oc_result.get("ok"):
                return {"content": oc_result["content"]}
            # Error envelope — surface as the dispatch contract expects.
            err = oc_result.get("error", {})
            return {
                "content": json.dumps({
                    "error": err.get("code", "oc_error"),
                    "message": err.get("message", "OC call failed"),
                }, default=str)
            }
        else:
            return {"content": json.dumps({"error": f"Unknown tool: {name}"})}
    except Exception as e:
        return {"content": json.dumps({"error": str(e)})}


async def run_agent(command: str, max_turns: int = 5, source: str = "", caller_id: str = "") -> dict:
    """Run the agent loop: call LLM, execute tools, return final response."""
    tier = get_current_tier()
    system_prompt = build_system_prompt()
    tools = list_available_tools()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": command},
    ]

    final_response = {"text": "", "tools_used": [], "error": None}

    # ── Loop-safety: track recent tool calls to detect infinite retries ──
    # If the LLM keeps calling the same tool with the same args, break early.
    # Cap: 2 consecutive identical calls ⇒ likely stuck; 3 ⇒ break with explanation.
    recent_call_keys: List[str] = []
    DUPLICATE_BREAK_THRESHOLD = 3
    last_tool_call_key: Optional[str] = None

    # H3 fix: cap tool result size sent to the LLM. A single `read_sheets`
    # returning 5000 rows would blow context on the next turn. 8K chars is
    # generous enough for most tool results while keeping context manageable.
    TOOL_RESULT_LLM_CAP = 8000

    # N6 fix: source-aware write authorisation. Tools like `write_docx` are
    # dangerous when triggered from a remote gateway. We restrict 'web' and
    # 'cli' to the full tier system, but require explicit confirmation for
    # write tools triggered from remote gateways when tier is GOD_MODE.
    # This doesn't replace the tier system — it layers on top.
    # REMOTE_SOURCES is imported from core.security (single source of truth,
    # also used by re_execute_approval for its source-gating).
    is_remote_source = source in REMOTE_SOURCES

    for turn in range(max_turns):
        llm_resp = await call_llm(messages, tools=tools)

        if "error" in llm_resp:
            final_response["error"] = llm_resp["error"]
            break

        tool_call = parse_tool_call(llm_resp)
        if not tool_call:
            # No tool call — this is the final response
            final_response["text"] = llm_resp.get("content", "")
            break

        # Execute the tool
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]

        # ── Detect stuck loops: same tool + same args called repeatedly ──
        call_key = f"{tool_name}|{json.dumps(tool_args, sort_keys=True)}"
        if call_key == last_tool_call_key:
            recent_call_keys.append(call_key)
        else:
            recent_call_keys = [call_key]
            last_tool_call_key = call_key

        if len(recent_call_keys) >= DUPLICATE_BREAK_THRESHOLD:
            final_response["error"] = (
                f"Agent loop detected: '{tool_name}' called {DUPLICATE_BREAK_THRESHOLD} times "
                f"with identical arguments. Aborting to avoid wasted tokens."
            )
            break

        # ── Permission check via central registry (C2 fix) ──────────────
        # Unknown tools are treated as 'unknown' permission — fail closed,
        # requiring approval rather than silently bypassing the check.
        from core.agent_config import get_tool_permission
        permission = get_tool_permission(tool_name)

        if permission == "write":
            # N6: tighten permission for remote sources.
            # Even at GOD_MODE, write tools from telegram/slack/whatsapp go
            # through the approval workflow unless explicitly exempted.
            effective_tier = tier
            if is_remote_source and tier == PermissionTier.GOD_MODE:
                # Demote write tools from remote sources to SEMI_AUTONOMOUS
                # behaviour for this single call. The tier setting in
                # settings.json is unchanged.
                effective_tier = PermissionTier.SEMI_AUTONOMOUS
            if effective_tier != PermissionTier.GOD_MODE:
                result = intercept_action(
                    "oauth_access",
                    f"Tool call: {tool_name}",
                    {"tool": tool_name, "args": tool_args, "source": source},
                    source,
                    caller_id,
                )
                if not result.get("allowed"):
                    tool_result = {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps({
                            "error": f"Approval required for {tool_name}",
                            "approval_id": result.get("approval_id"),
                        }),
                    }
                else:
                    tool_result = await _run_tool(tool_name, tool_args)
                    tool_result["role"] = "tool"
                    tool_result["tool_call_id"] = tool_call["id"]
            else:
                # 'effective_tier' is GOD_MODE (either real, or remote source
                # didn't apply because tier wasn't GOD_MODE).
                #
                # Round 15 slice 2 (foot-gun fix): destructive OC tools
                # (persisted risk == "destructive", e.g. github.delete_repo)
                # must STILL pause for an out-of-band approval even at
                # GOD_MODE, where every other external write auto-runs.
                # Non-destructive writes keep today's "just runs" behaviour.
                from core.tools import registry as _registry
                if _registry.is_destructive_tool(tool_name):
                    result = intercept_action(
                        "oauth_access",
                        f"Destructive tool call: {tool_name}",
                        {"tool": tool_name, "args": tool_args, "source": source},
                        source,
                        caller_id,
                        force_confirm=True,
                    )
                    tool_result = {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "content": json.dumps({
                            "error": f"Destructive approval required for {tool_name}",
                            "approval_id": result.get("approval_id"),
                        }),
                    }
                else:
                    tool_result = await _run_tool(tool_name, tool_args)
                    tool_result["role"] = "tool"
                    tool_result["tool_call_id"] = tool_call["id"]
        elif permission == "unknown":
            # Unknown tool — don't execute; tell the LLM the tool is not registered.
            tool_result = {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": json.dumps({
                    "error": (
                        f"Tool '{tool_name}' is not registered in the permission "
                        f"registry. Refusing to execute. Available tools are listed "
                        f"in the system prompt."
                    ),
                }),
            }
        else:
            # 'read' permission — execute freely (no tier check needed)
            tool_result = await _run_tool(tool_name, tool_args)
            tool_result["role"] = "tool"
            tool_result["tool_call_id"] = tool_call["id"]

        # H3 fix: cap the result sent back to the LLM. The full result is
        # kept in tools_used for the API response; only the LLM-bound copy
        # is truncated.
        llm_content = tool_result.get("content", "")
        if isinstance(llm_content, str) and len(llm_content) > TOOL_RESULT_LLM_CAP:
            truncated = (
                llm_content[:TOOL_RESULT_LLM_CAP]
                + f"\n\n[... truncated at {TOOL_RESULT_LLM_CAP} chars. "
                f"Full result available to the user via the tools_used list. ...]"
            )
            messages.append(llm_resp)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": truncated,
            })
        else:
            messages.append(llm_resp)
            messages.append(tool_result)

        final_response["tools_used"].append({
            "name": tool_name,
            "args": tool_args,
            "result": tool_result.get("content", "")[:200],
        })

    return final_response


async def _run_tool(name: str, args: dict) -> dict:
    """Run a single tool and return the result as a dict with 'content'."""
    result = await execute_tool(name, args)
    return result

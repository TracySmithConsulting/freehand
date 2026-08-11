import json
import asyncio
from typing import Dict, List, Optional, Any
from datetime import datetime

import aiohttp

from core.agent_config import get_llm_config, build_system_prompt, list_available_tools
from core.security import get_current_tier, intercept_action, PermissionTier
from core.tools.office import read_docx, write_docx, read_xlsx, read_pptx
from core.tools.browser import get_axtree, extract_text, screenshot
from core.tools.integrations import (
    read_email, list_calendar_events, read_sheets, read_sheet_range,
    get_github_repos, list_github_issues, create_github_issue,
    create_github_pull_request, list_zoom_meetings, schedule_zoom_meeting,
    list_facebook_pages, post_to_facebook, list_instagram_accounts,
    post_to_instagram, list_zoom_recordings, get_connections_summary,
)
from core.memory import search_memory


async def call_llm(messages: List[Dict], tools: List[Dict] = None, config: dict = None) -> dict:
    """Call an OpenAI-compatible API and return the response."""
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
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return {"error": f"LLM API error {resp.status}: {text[:200]}"}
                data = await resp.json()
                return data.get("choices", [{}])[0].get("message", {})
    except asyncio.TimeoutError:
        return {"error": "LLM request timed out (120s)"}
    except Exception as e:
        return {"error": f"LLM request failed: {str(e)[:200]}"}


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
        elif name == "list_github_repos":
            result = await get_github_repos(
                label=args.get("label", "default"),
                private=args.get("private", False),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "list_github_issues":
            result = await list_github_issues(
                label=args.get("label", "default"),
                repo=args.get("repo", ""),
                state=args.get("state", "open"),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "create_github_issue":
            result = await create_github_issue(
                label=args.get("label", "default"),
                repo=args.get("repo", ""),
                title=args.get("title", ""),
                body=args.get("body", ""),
            )
            return {"content": json.dumps(result, default=str)}
        elif name == "create_github_pull_request":
            result = await create_github_pull_request(
                label=args.get("label", "default"),
                repo=args.get("repo", ""),
                title=args.get("title", ""),
                head=args.get("head", ""),
                base=args.get("base", ""),
                body=args.get("body", ""),
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

        if permission == "write" and tier != PermissionTier.GOD_MODE:
            result = intercept_action(
                "oauth_access",
                f"Tool call: {tool_name}",
                {"tool": tool_name, "args": tool_args},
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
        elif permission == "unknown" and tier != PermissionTier.GOD_MODE:
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
            # 'read' permission or GOD_MODE — execute freely
            tool_result = await _run_tool(tool_name, tool_args)
            tool_result["role"] = "tool"
            tool_result["tool_call_id"] = tool_call["id"]

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

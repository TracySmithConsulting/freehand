import json
import asyncio
import re
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
    post_to_instagram, get_connections_summary,
)
from core.memory import search_memory
from core import conversation, long_memory, skills

# Background memory saves; kept referenced so they aren't garbage-collected.
_background_tasks: set = set()

_THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Remove a model's private reasoning from its reply.

    Some models (MiniMax, DeepSeek, Qwen) put their thinking in the answer
    as <think>...</think>; people on WhatsApp or the console saw it."""
    return _THINK_RE.sub("", text).strip()


# The last time FreeHand had to use a backup model, for the Settings page.
LAST_SWITCH: Dict[str, Any] = {}
# When each failing main model was last mentioned to the user.
_SWITCH_NOTICED: Dict[str, float] = {}
SWITCH_NOTICE_EVERY_SECONDS = 3600


def _switch_note(switched: dict) -> str:
    """One plain sentence about the switch, at most once an hour per model,
    so people learn their main account needs attention without every
    reply repeating it."""
    import time
    key = switched.get("from", "")
    now = time.time()
    if now - _SWITCH_NOTICED.get(key, 0) < SWITCH_NOTICE_EVERY_SECONDS:
        return ""
    _SWITCH_NOTICED[key] = now
    return (f"(Note: your main AI model, {switched['from']}, didn't answer because {switched['reason']}, "
            f"so I used your backup, {switched['to']}. Check your main model in Settings.)")


def _model_name(config: dict) -> str:
    from core import llm_providers
    p = llm_providers.get(llm_providers.identify(config.get("provider", ""), config.get("base_url", "")))
    service = p["name"] if p and p["id"] != "custom" else (config.get("base_url") or "your custom service")
    return f"{service} ({config.get('model', '')})"


def _failure_reason(status: Optional[int], timed_out: bool = False) -> str:
    if timed_out:
        return "it didn't respond in time"
    if status is None:
        return "FreeHand couldn't reach it"
    if status in (401, 403):
        return "its key wasn't accepted"
    if status == 402:
        return "it has no credit left"
    if status == 429:
        return "it hit a usage limit or ran out of credit"
    if status == 404:
        return "it didn't recognise the model name"
    if status >= 500:
        return "it had a problem on its side"
    return f"it refused the request (error {status})"


async def call_llm(messages: List[Dict], tools: List[Dict] = None, config: dict = None) -> dict:
    """Call the AI model, switching to backup models if it fails.

    With an explicit ``config`` only that model is tried. Otherwise the
    main model is tried first, then each backup in order (Settings, AI
    model), until one answers. A reply from a backup carries ``_switched``
    ({"from", "to", "reason"}) so the user can be told.
    """
    if config is not None:
        msg, _status, _timed_out = await _call_one(messages, tools, config, last=True)
        return msg

    from core.agent_config import get_llm_chain
    chain = get_llm_chain() or [get_llm_config()]
    first_failure = None
    for i, cfg in enumerate(chain):
        last = i == len(chain) - 1
        msg, status, timed_out = await _call_one(messages, tools, cfg, last=last)
        if "error" not in msg:
            if i > 0 and first_failure:
                switched = {"from": _model_name(chain[0]), "to": _model_name(cfg), "reason": first_failure}
                msg = {**msg, "_switched": switched}
                LAST_SWITCH.clear()
                LAST_SWITCH.update({**switched, "at": datetime.now().isoformat(timespec="minutes")})
            return msg
        if first_failure is None:
            first_failure = _failure_reason(status, timed_out)
            first_error = msg["error"]
    if len(chain) > 1:
        return {"error": f"{first_error} (your backup models didn't answer either)"}
    return {"error": first_error}


async def _call_one(messages: List[Dict], tools: Optional[List[Dict]], config: dict, last: bool) -> tuple:
    """One model. Returns (message or {"error"}, HTTP status or None, timed out?).

    H1 fix: retries with backoff on transient errors (HTTP 429/5xx,
    timeouts, connection errors). Auth errors (401/403) and client errors
    (400) are NOT retried — they indicate the request itself is wrong.
    When a backup model is waiting (``last`` is False), FreeHand gives up
    sooner - one retry, a 60-second limit, no retry after a timeout - so
    a stuck service doesn't keep people waiting minutes.
    """
    from core.agent_config import resolve_llm_endpoint
    model = config.get("model", "gpt-4o-mini")
    temperature = config.get("temperature", 0.7)
    max_tokens = config.get("max_tokens", 4096)

    base_url, headers = resolve_llm_endpoint(config)
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    from core import llm_providers
    if llm_providers.omit_temperature(config.get("provider", ""), config.get("base_url", "")):
        # Newer Claude models reject temperatures below 1 here.
        payload.pop("temperature")
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    seconds = 120 if last else 60
    timeout = aiohttp.ClientTimeout(total=seconds)

    # H1: retry configuration
    RETRY_BACKOFFS = [0, 2, 4] if last else [0, 2]  # seconds; first attempt has 0s delay
    RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

    last_error = None
    status = None
    timed_out = False
    for attempt, delay in enumerate(RETRY_BACKOFFS):
        if delay:
            await asyncio.sleep(delay)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
                    status = resp.status
                    if resp.status == 200:
                        data = await resp.json()
                        msg = dict(data.get("choices", [{}])[0].get("message", {}) or {})
                        # Token counts, for the "this reply used about ..." note.
                        # Removed before the message goes back to the model.
                        usage = data.get("usage")
                        msg["_usage"] = usage if isinstance(usage, dict) else {
                            "estimated": True,
                            "prompt_tokens": len(json.dumps(payload)) // 4,
                            "completion_tokens": len(str(msg.get("content") or "")) // 4,
                        }
                        return msg, 200, False
                    # Read error body for context
                    try:
                        body_text = (await resp.text())[:200]
                    except Exception:
                        body_text = ""
                    last_error = f"LLM API error {resp.status}: {body_text}"
                    # Only retry on retryable status codes
                    if resp.status not in RETRYABLE_STATUS:
                        return {"error": last_error}, status, False
                    # Otherwise, fall through to next retry
        except asyncio.TimeoutError:
            last_error = f"LLM request timed out ({seconds}s)"
            timed_out, status = True, None
            if not last:
                break  # a backup is waiting: don't sit through another timeout
        except aiohttp.ClientError as e:
            last_error = f"LLM connection error: {str(e)[:200]}"
            status = None
        except Exception as e:
            # Non-retryable exception (programmer error, not network)
            return {"error": f"LLM request failed: {str(e)[:200]}"}, None, False

    # All retries exhausted
    return {"error": last_error or "LLM request failed after retries"}, status, timed_out


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
        if name in ("read_docx", "read_xlsx", "read_pptx"):
            from core.tools.office import check_read_allowed
            denied = check_read_allowed(args.get("path", ""))
            if denied:
                return {"content": json.dumps({"error": denied})}
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
        elif name == "github_activity":
            from core.tools.github_read import github_activity
            result = await github_activity(args.get("repo", ""))
            return {"content": json.dumps(result, default=str)}
        elif name == "list_connections":
            result = get_connections_summary()
            return {"content": json.dumps(result, default=str)}
        elif name == "search_memory":
            results = await asyncio.to_thread(
                search_memory, args.get("query", ""), args.get("limit", 5), True)
            return {"content": json.dumps(results, default=str)}
        elif name == "remember":
            result = await asyncio.to_thread(long_memory.retain, args.get("fact", ""), "Told to remember", True)
            return {"content": json.dumps(result, default=str)}
        elif name == "forget_memory":
            result = await asyncio.to_thread(long_memory.forget_matching, args.get("description", ""))
            return {"content": json.dumps(result, default=str)}
        elif name == "use_skill":
            result = await asyncio.to_thread(skills.use, args.get("name", ""))
            return {"content": json.dumps(result, default=str)}
        elif name == "skill_outcome":
            result = await asyncio.to_thread(
                skills.record_outcome, args.get("name", ""), bool(args.get("worked")), args.get("note", ""))
            return {"content": json.dumps(result, default=str)}
        elif name == "save_skill":
            result = await asyncio.to_thread(
                skills.save, args.get("name", ""), args.get("description", ""), args.get("instructions", ""))
            return {"content": json.dumps(result, default=str)}

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
        if name.startswith("co_"):
            from core.tools.composio_dispatch import dispatch_composio_tool
            composio_result = await dispatch_composio_tool(name, args)
            if composio_result.get("ok"):
                return {"content": composio_result["content"]}
            err = composio_result.get("error", {})
            return {
                "content": json.dumps({
                    "error": err.get("code", "composio_error"),
                    "message": err.get("message", "Composio call failed"),
                }, default=str)
            }
        else:
            return {"content": json.dumps({"error": f"Unknown tool: {name}"})}
    except Exception as e:
        return {"content": json.dumps({"error": str(e)})}


async def run_agent(command: str, max_turns: int = 5, source: str = "", caller_id: str = "",
                    job: bool = False, member: Optional[dict] = None) -> dict:
    """Run the agent loop: call LLM, execute tools, return final response.

    ``job=True`` is a scheduled job (core/scheduler.py): short instructions
    (build_job_prompt), no chat history, a few memories, only the tools the
    job's words call for, and nothing that needs someone there (questions,
    the computer Helper, more jobs). The run isn't kept in the chat history.

    ``member`` is a team member (core/team.py): its own instructions inside
    FreeHand's rules, only its tools, its model and limits. A message that
    starts with a team member's name ("Thandi, draft a post") goes to them.

    Everything this run stores (chat history, tasks, approvals, memories)
    is tagged with whose message it is (core/people.py). The gateways only
    let known people in, so an unknown caller here is the owner (console,
    CLI, tests)."""
    from core import people
    token = people.set_current(people.person_for(source, caller_id) or people.OWNER_ID)
    try:
        if member is None and not job:
            from core import team
            member, command = team.addressed(command)
        if member is not None:
            return await _run_member(command, source, caller_id, job, member)
        return await _run_agent(command, max_turns, source, caller_id, job)
    finally:
        people.reset(token)


async def _run_member(command: str, source: str, caller_id: str, job: bool, member: dict,
                      delegated: bool = False) -> dict:
    """A team member's turn: checks first (paused, over its limit), then the
    loop with its setup, then the run counted and logged under its name."""
    from core import team
    name = member["name"]
    if member.get("paused"):
        return {"text": f"{name} is paused. You can resume them under My team members.",
                "tools_used": [], "error": None, "team_member": name}
    over = team.over_limit(member)
    if over:
        return {"text": over, "tools_used": [], "error": None, "team_member": name}
    result = await _run_agent(command, int(member.get("steps") or team.DEFAULT_STEPS), source, caller_id,
                              job, member=member, delegated=delegated)
    try:
        team.record(member, command, result)
    except OSError:
        pass  # the log is a convenience; the reply matters more
    result["team_member"] = name
    if result.get("text"):
        result["text"] = f"{name}: {result['text']}"
    return result


async def _run_agent(command: str, max_turns: int, source: str, caller_id: str, job: bool = False,
                     member: Optional[dict] = None, delegated: bool = False) -> dict:
    tier = get_current_tier()
    from core import tool_selection
    from core.agent_config import get_llm_config, build_job_prompt
    # A scheduled job is always lean: only the tools its words call for.
    lean = job or tool_selection.lean_mode(get_llm_config())
    if member:
        from core import team
        system_prompt = team.prompt(member, scheduled=job, delegated=delegated)
        model_cfg = team.model_config(member)
    else:
        system_prompt = build_job_prompt() if job else build_system_prompt(lean=lean)
        model_cfg = None

    # Memory (Nov 2026). Long-term: the few facts that matter for this
    # message, from Hindsight. Short-term: the last few exchanges in this
    # conversation, so follow-ups like "and tomorrow?" make sense. Either
    # one being unavailable just means less context, never a failed chat.
    conv_key = conversation.conversation_key(source, caller_id)
    if member:
        conv_key += "|team:" + member["id"]  # its own short chat with the person
    # A team member sees memories only if the person allowed it.
    remembered = ([] if member and not member.get("memories")
                  else await asyncio.to_thread(long_memory.recall, command))
    if remembered:
        system_prompt += (
            "\n## What you remember about the user\n"
            "Use these when they help; don't recite them unprompted.\n"
            + "\n".join(f"- {line}" for line in remembered[:5 if lean or member else 15])
            + "\n"
        )

    # Handed work by FreeHand, a team member gets just the request.
    history = [] if job or delegated else conversation.history(conv_key)
    if lean or member:
        history = history[-4:]  # the last two exchanges are enough for a small model
    messages = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": command},
    ]

    # Economy (Nov 2026): only the tools this message needs, plus
    # load_tools for the rest (core/tool_selection.py). The previous user
    # message counts too, so "and post it there" still finds Facebook.
    earlier = [m["content"] for m in history if m.get("role") == "user"][-1:]
    from core import ask_user, helper_hub, schedule_job
    from core.scheduler import DUE_PREFIX
    if member:
        # Only its own tools: no load_tools, scheduling, Helper or other team members.
        from core import team
        allowed = set(team.tool_names(member))
        tools, tool_state = tool_selection.select(
            [s for s in list_available_tools() if s["function"]["name"] in allowed], [command], lean=False)
    else:
        allowed = None
        tools, tool_state = tool_selection.select(list_available_tools(), [command, *earlier], lean=lean)
    if not job and not delegated:
        tools = [*tools, ask_user.schema()]
    # The main FreeHand can hand work to a team member (core/team.py). Team
    # members can't, so it only ever goes one level down.
    from core import team as _team
    ask_team = None if (member or job) else _team.ask_schema(_team.all_members())
    if ask_team:
        tools.append(ask_team)
    # Reminders and scheduled jobs, when the message is about a time. Not
    # while a scheduled job is itself running, so a job can't make more jobs.
    if schedule_job.wanted([command, *earlier]) and not job and not member and not command.startswith(DUE_PREFIX):
        tools.append(schedule_job.schema())
    # Stopping and changing jobs from the chat (core/manage_jobs.py), when
    # the person has jobs and the message is about them.
    from core import manage_jobs
    if not job and not member and manage_jobs.wanted([command, *earlier]):
        tools.append(manage_jobs.schema())
    # The user's own computer, through the FreeHand Helper: only while one
    # is connected, and only when the message is about the computer (and
    # someone is there to approve it, so not in a scheduled job).
    if helper_hub.device_now() and not job and not member:
        if helper_hub.wanted([command, *earlier]):
            tools.extend(helper_hub.schemas())
        if helper_hub.wanted_change([command, *earlier]):
            tools.extend(helper_hub.change_schemas())
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "estimated": False}

    def count(resp: dict) -> None:
        u = resp.pop("_usage", None)
        if isinstance(u, dict):
            usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
            usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
            usage["calls"] += 1
            usage["estimated"] = usage["estimated"] or bool(u.get("estimated"))

    final_response = {"text": "", "tools_used": [], "error": None}
    switched = None  # set when a backup model answered instead of the main one

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
    # write tools triggered from remote gateways when tier is FULL_ACCESS.
    # This doesn't replace the tier system — it layers on top.
    # REMOTE_SOURCES is imported from core.security (single source of truth,
    # also used by re_execute_approval for its source-gating).
    is_remote_source = source in REMOTE_SOURCES

    work_turns = 0
    loads = 0
    while work_turns < max_turns:
        llm_resp = await call_llm(messages, tools=tools, config=model_cfg)
        count(llm_resp)
        if "_switched" in llm_resp:
            # Keep FreeHand's marker out of what goes back to the model.
            switched = llm_resp.pop("_switched")

        if "error" in llm_resp:
            final_response["error"] = llm_resp["error"]
            break

        tool_call = parse_tool_call(llm_resp)
        if not tool_call:
            # No tool call — this is the final response
            final_response["text"] = strip_reasoning(llm_resp.get("content", "") or "")
            break

        # The model may ask for several tools at once, but FreeHand runs one
        # per turn. Keep only that one in the history: a request without a
        # matching result is rejected by stricter providers (MiniMax: "tool
        # call and result not match"). The model can ask again next turn.
        calls = llm_resp.get("tool_calls") or []
        if len(calls) > 1:
            llm_resp = {**llm_resp, "tool_calls": [c for c in calls if c.get("id") == tool_call["id"]][:1] or calls[:1]}

        # Execute the tool
        tool_name = tool_call["name"]
        tool_args = tool_call["args"]

        if tool_name == ask_user.ASK_USER:
            # Not a real tool either: the questions are the reply, and the
            # turn ends here. The user's answer arrives as the next message.
            asked = ask_user.clean(tool_args)
            if asked:
                final_response["text"] = ask_user.as_text(asked)
                final_response["questions"] = asked
                final_response["tools_used"].append({"name": tool_name, "args": tool_args, "result": "asked"})
                break
            work_turns += 1
            messages.append(llm_resp)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": ask_user.refusal()})
            continue

        if ask_team and tool_name == _team.ASK:
            who = _team.get(str((tool_args or {}).get("name") or ""))
            request = str((tool_args or {}).get("request") or "").strip()
            if not who or who.get("paused") or not request:
                out = json.dumps({"error": "That isn't one of the person's team members who is working now, "
                                           "or there was no request."})
            else:
                done = await _run_member(request, source, caller_id, False, who, delegated=True)
                # What the team member used counts in this reply's total too.
                du = done.get("usage") or {}
                usage["prompt_tokens"] += int(du.get("prompt_tokens") or 0)
                usage["completion_tokens"] += int(du.get("completion_tokens") or 0)
                usage["calls"] += int(du.get("calls") or 0)
                reply = (done.get("text") or done.get("error") or "")
                if reply.startswith(who["name"] + ": "):
                    reply = reply[len(who["name"]) + 2:]
                out = json.dumps({
                    "team_member": who["name"],
                    "reply": reply[:_team.MAX_REPLY],
                    "tools_they_used": [x.get("name") for x in done.get("tools_used") or []],
                })
            messages.append(llm_resp)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": out})
            final_response["tools_used"].append({"name": tool_name, "args": tool_args, "result": out[:200]})
            continue

        if tool_name == manage_jobs.TOOL and not job and not member:
            try:
                out = await asyncio.to_thread(manage_jobs.run_tool, tool_args, source, caller_id, command)
            except Exception as e:
                out = json.dumps({"problem": f"That couldn't be done: {str(e)[:150]}"})
            messages.append(llm_resp)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": out})
            final_response["tools_used"].append({"name": tool_name, "args": tool_args, "result": out[:200]})
            continue

        if tool_name == schedule_job.TOOL:
            # Saving a job the person asked for: no approval needed. What it
            # does when it runs goes through the usual permission rules.
            try:
                out = await asyncio.to_thread(schedule_job.run_tool, tool_args, source, caller_id)
            except Exception as e:
                out = json.dumps({"saved": False, "problem": f"The job couldn't be saved: {str(e)[:150]}"})
            messages.append(llm_resp)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": out})
            final_response["tools_used"].append({"name": tool_name, "args": tool_args, "result": out[:200]})
            continue

        if tool_name == tool_selection.LOAD_TOOLS and loads < 3:
            # Not a real tool: add a group of tools, and don't count the turn.
            loads += 1
            tools, note = tool_selection.add_group(tools, tool_state, str((tool_args or {}).get("group", "")))
            messages.append(llm_resp)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": note})
            continue
        work_turns += 1

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

        if tool_name in helper_hub.TOOLS:
            # Work on the computer takes more steps (open, look, type,
            # check), each approved on the PC, so allow up to 10.
            max_turns = max(max_turns, 10)
            # The Helper asks the person on their PC itself, so no
            # approval here. A screenshot comes back as an image for the
            # model to look at.
            try:
                out = await helper_hub.run_tool(tool_name, tool_args)
            except Exception as e:  # a bad answer must not crash the whole reply
                out = {"content": json.dumps({"error": f"The Helper's answer couldn't be read: {str(e)[:150]}"})}
            messages.append(llm_resp)
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": out["content"]})
            if out.get("image"):
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": "Here is the screenshot from my computer."},
                    {"type": "image_url", "image_url": {"url": out["image"]}},
                ]})
            final_response["tools_used"].append({"name": tool_name, "args": tool_args,
                                                 "result": out["content"][:200]})
            continue

        # ── Permission check via central registry (C2 fix) ──────────────
        # Unknown tools are treated as 'unknown' permission — fail closed,
        # requiring approval rather than silently bypassing the check.
        from core.agent_config import get_tool_permission
        permission = get_tool_permission(tool_name)

        if allowed is not None and tool_name not in allowed:
            # A team member asked for a tool it wasn't given.
            tool_result = {"role": "tool", "tool_call_id": tool_call["id"], "content": json.dumps(
                {"error": f"{tool_name} isn't one of {member['name']}'s tools."})}
        elif member and permission == "write":
            # A team member: "Ask me first" unless the person switched on
            # "Just do it" for this tool. Deleting always asks, and the
            # permission level's floor (Autonomous: no outside actions) holds.
            from core import team
            from core.tools import registry as _registry
            from core.tools import composio_registry as _composio_registry
            destructive = _registry.is_destructive_tool(tool_name) or _composio_registry.is_destructive_tool(tool_name)
            if team.may_just_do(member, tool_name) and not destructive and tier != PermissionTier.AUTONOMOUS:
                tool_result = await _run_tool(tool_name, tool_args)
                tool_result["role"] = "tool"
                tool_result["tool_call_id"] = tool_call["id"]
            else:
                result = intercept_action(
                    "oauth_access",
                    f"{member['name']} wants to use {tool_name}",
                    {"tool": tool_name, "args": tool_args, "source": source, "caller_id": caller_id,
                     "team_member": member["id"]},
                    source, caller_id, force_confirm=True,
                )
                tool_result = {"role": "tool", "tool_call_id": tool_call["id"], "content": json.dumps({
                    "error": f"Waiting for the person's approval for {tool_name}. Tell them it's waiting.",
                    "approval_id": result.get("approval_id"),
                })}
        elif permission == "write":
            # N6: tighten permission for remote sources.
            # Even at FULL_ACCESS, write tools from telegram/slack/whatsapp go
            # through the approval workflow unless explicitly exempted.
            effective_tier = tier
            if is_remote_source and tier == PermissionTier.FULL_ACCESS:
                # Demote write tools from remote sources to SEMI_AUTONOMOUS
                # behaviour for this single call. The tier setting in
                # settings.json is unchanged.
                effective_tier = PermissionTier.SEMI_AUTONOMOUS
            if effective_tier != PermissionTier.FULL_ACCESS:
                result = intercept_action(
                    "oauth_access",
                    f"Tool call: {tool_name}",
                    {"tool": tool_name, "args": tool_args, "source": source, "caller_id": caller_id},
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
                # 'effective_tier' is FULL_ACCESS (either real, or remote source
                # didn't apply because tier wasn't FULL_ACCESS).
                #
                # Round 15 slice 2 (foot-gun fix): destructive OC tools
                # (persisted risk == "destructive", e.g. github.delete_repo)
                # must STILL pause for an out-of-band approval even at
                # FULL_ACCESS, where every other external write auto-runs.
                # Non-destructive writes keep today's "just runs" behaviour.
                from core.tools import registry as _registry
                from core.tools import composio_registry as _composio_registry
                if _registry.is_destructive_tool(tool_name) or _composio_registry.is_destructive_tool(tool_name):
                    result = intercept_action(
                        "oauth_access",
                        f"Destructive tool call: {tool_name}",
                        {"tool": tool_name, "args": tool_args, "source": source, "caller_id": caller_id},
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

    if not final_response["text"] and not final_response["error"] and final_response["tools_used"]:
        # Every turn went on tool calls (for example several searches), so
        # there was no reply - people got an empty message. Ask once more,
        # without tools, for an answer from what was found.
        messages.append({
            "role": "user",
            "content": ("Please answer me now from what you've found and done so far, without using more "
                        "tools. Don't mention tools or this message; if something isn't finished, say what's left."),
        })
        last = await call_llm(messages, config=model_cfg)
        count(last)
        if "_switched" in last:
            switched = last.pop("_switched")
        if "error" in last:
            final_response["error"] = last["error"]
        else:
            final_response["text"] = strip_reasoning(last.get("content", "") or "")

    if final_response["text"] and not final_response["error"] and not job and not delegated:
        if member:
            # Its own short chat only, not long-term memory.
            from core import people
            conversation.add_turn(conv_key, command, final_response["text"], person=people.current())
        else:
            _remember_turn(conv_key, command, final_response["text"], source)
    if usage["calls"]:
        final_response["usage"] = usage

    # Shown, not remembered: the note is added after the turn is saved.
    if switched and final_response["text"]:
        final_response["model_switch"] = switched
        note = _switch_note(switched)
        if note:
            final_response["text"] += "\n\n" + note
    return final_response


def _remember_turn(conv_key: str, command: str, reply: str, source: str) -> None:
    """Keep this exchange: in the conversation history now, and in long-term
    memory in the background (Hindsight extracts the facts worth keeping)."""
    from core import people
    conversation.add_turn(conv_key, command, reply, person=people.current())
    if long_memory.looks_secret(command) or long_memory.looks_secret(reply):
        return
    if source == "selftest":
        # Live checks of FreeHand itself must not end up in someone's memory.
        return
    text = f"User: {command}\nFreeHand: {reply}"
    try:
        task = asyncio.get_running_loop().create_task(
            asyncio.to_thread(long_memory.retain, text, f"chat via {source or 'web'}")
        )
    except RuntimeError:
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _record_command(command: str) -> None:
    """Store a truncated, hashed record of a command in the memories table.

    N3: only the first 200 chars are persisted so secrets pasted into a
    command don't live in the DB indefinitely.
    """
    import hashlib
    import sqlite3
    from core import database
    cmd_truncated = command[:200]
    cmd_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()[:16]
    conn = sqlite3.connect(str(database.DB_PATH))
    try:
        conn.execute(
            "INSERT INTO memories (path, title, content) VALUES (?, ?, ?)",
            ("/agent/command", f"Command ({cmd_hash}): " + cmd_truncated[:50], cmd_truncated),
        )
        conn.commit()
    finally:
        conn.close()


async def run_command(command: str, source: str = "", caller_id: str = "") -> dict:
    """Record a command and run the agent on it.

    Single entry point shared by POST /api/agent/command and the chat
    gateways, so both behave identically.
    """
    _record_command(command)
    return await run_agent(command, source=source, caller_id=caller_id)


async def _run_tool(name: str, args: dict) -> dict:
    """Run a single tool and return the result as a dict with 'content'."""
    result = await execute_tool(name, args)
    return result

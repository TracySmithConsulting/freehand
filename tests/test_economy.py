"""Economy (Nov 2026): send the model only what each message needs.

Every tool and every line of instructions is text the model reads and the
user pays for, on every message."""
import asyncio

from core import agent, tool_selection


def tool(name):
    return {"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object"}}}


BUILTIN = [tool(n) for n in ("remember", "search_memory", "read_email", "list_calendar_events",
                             "read_docx", "get_axtree", "post_to_facebook", "brand_new_tool")]
FB_ADDON = [tool(f"co_facebook_default_FB_{i}") for i in range(48)]
IG_ADDON = [tool(f"co_instagram_default_IG_{i}") for i in range(23)]


def names(tools):
    return [t["function"]["name"] for t in tools]


def fake_dynamic(monkeypatch):
    dyn = {t["function"]["name"]: "facebook" for t in FB_ADDON}
    dyn.update({t["function"]["name"]: "instagram" for t in IG_ADDON})
    monkeypatch.setattr(tool_selection, "_dynamic_groups", lambda: dyn)


def test_add_on_tools_stay_out_unless_the_message_is_about_them(monkeypatch):
    fake_dynamic(monkeypatch)
    sent, _ = tool_selection.select(BUILTIN + FB_ADDON + IG_ADDON, ["What's on my calendar tomorrow?"])
    got = names(sent)
    assert not any(n.startswith("co_") for n in got)
    assert "list_calendar_events" in got and "read_email" in got and "brand_new_tool" in got
    assert got[-1] == "load_tools"


def test_mentioning_a_service_brings_its_tools(monkeypatch):
    fake_dynamic(monkeypatch)
    sent, _ = tool_selection.select(BUILTIN + FB_ADDON + IG_ADDON, ["Post this to Instagram please"])
    got = names(sent)
    assert sum(n.startswith("co_instagram") for n in got) == 23
    assert not any(n.startswith("co_facebook") for n in got)


def test_the_previous_message_counts_too(monkeypatch):
    fake_dynamic(monkeypatch)
    sent, _ = tool_selection.select(BUILTIN + FB_ADDON, ["and post it there", "Draft a Facebook update"])
    assert any(n.startswith("co_facebook") for n in names(sent))


def test_lean_mode_sends_only_core_tools_and_topic_ones(monkeypatch):
    fake_dynamic(monkeypatch)
    sent, _ = tool_selection.select(BUILTIN + FB_ADDON, ["Check my email"], lean=True)
    got = names(sent)
    assert set(got) == {"remember", "search_memory", "brand_new_tool", "read_email", "load_tools"}


def test_load_tools_adds_a_group_once(monkeypatch):
    fake_dynamic(monkeypatch)
    sent, state = tool_selection.select(BUILTIN + IG_ADDON, ["hello"])
    sent, note = tool_selection.add_group(sent, state, "instagram")
    assert "Loaded 23 instagram tools" in note
    assert sum(n.startswith("co_instagram") for n in names(sent)) == 23
    assert "load_tools" not in names(sent)  # nothing left to load
    _, again = tool_selection.add_group(sent, state, "instagram")
    assert "already loaded" in again
    _, unknown = tool_selection.add_group(sent, state, "fax")
    assert "no tool group called 'fax'" in unknown


def test_lean_mode_is_automatic_for_local_ollama_only():
    assert tool_selection.lean_mode({"provider": "ollama"})
    assert not tool_selection.lean_mode({"provider": "ollama_cloud"})
    assert not tool_selection.lean_mode({"provider": "openrouter"})
    assert tool_selection.lean_mode({"provider": "openrouter", "lean": "on"})
    assert not tool_selection.lean_mode({"provider": "ollama", "lean": "off"})


def test_lean_instructions_drop_the_tool_catalogue():
    from core import agent_config
    full, lean = agent_config.build_system_prompt(), agent_config.build_system_prompt(lean=True)
    assert "### Office Tools" in full and "### Office Tools" not in lean
    assert "### Memory" in lean and "### Voice" in lean
    assert len(lean) < len(full)


def test_the_loop_loads_tools_mid_answer_and_reports_usage(monkeypatch):
    """The model asks for Instagram tools, gets them, then answers. The
    load doesn't use up one of the five turns."""
    fake_dynamic(monkeypatch)
    monkeypatch.setattr(agent, "list_available_tools", lambda: BUILTIN + IG_ADDON)
    seen_tools = []
    replies = iter([
        {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                         "function": {"name": "load_tools", "arguments": '{"group": "instagram"}'}}],
         "_usage": {"prompt_tokens": 900, "completion_tokens": 10}},
        {"content": "Here are your Instagram posts.", "_usage": {"prompt_tokens": 1500, "completion_tokens": 20}},
    ])

    async def fake_llm(messages, tools=None, config=None):
        seen_tools.append(names(tools or []))
        return dict(next(replies))
    monkeypatch.setattr(agent, "call_llm", fake_llm)
    out = asyncio.run(agent.run_agent("show my latest posts", source="selftest"))
    assert out["text"] == "Here are your Instagram posts."
    assert not any(n.startswith("co_instagram") for n in seen_tools[0])
    assert sum(n.startswith("co_instagram") for n in seen_tools[1]) == 23
    assert out["usage"] == {"prompt_tokens": 2400, "completion_tokens": 30, "calls": 2, "estimated": False}

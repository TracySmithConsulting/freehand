"""Where an everyday message's tokens go: FreeHand's instructions, the
memories, the chat history and the tools, measured with the person's real
setup. Nothing is sent to the AI model: the first call is caught and
measured instead.

    python scripts/measure_prompt.py "What's the weather like in Cape Town?"

Tokens are estimated as characters / 4, which is close for English.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def tokens(text: str) -> int:
    return len(text) // 4


def measure(command: str) -> dict:
    from core import agent
    caught = {}

    async def catch(messages, tools=None, config=None):
        caught.update(messages=messages, tools=tools or [])
        return {"content": "(measured)"}

    real = agent.call_llm
    agent.call_llm = catch
    try:
        asyncio.run(agent.run_agent(command, source="selftest"))
    finally:
        agent.call_llm = real
    messages, tools = caught["messages"], caught["tools"]
    system = messages[0]["content"]
    sections = {}
    for part in re.split(r"\n(?=#{2,3} )", system):
        head = part.strip().splitlines()[0][:60] if part.strip() else "(blank)"
        sections[head] = tokens(part)
    out = {
        "system_prompt": tokens(system),
        "system_sections": dict(sorted(sections.items(), key=lambda kv: -kv[1])),
        "history_messages": len(messages) - 2,
        "history": sum(tokens(m["content"] if isinstance(m["content"], str) else json.dumps(m["content"]))
                       for m in messages[1:-1]),
        "message": tokens(messages[-1]["content"]),
        "tools_count": len(tools),
        "tools": tokens(json.dumps(tools)),
        "tools_biggest": sorted(((t["function"]["name"], tokens(json.dumps(t))) for t in tools),
                                key=lambda x: -x[1])[:12],
    }
    out["total"] = out["system_prompt"] + out["history"] + out["message"] + out["tools"]
    return out


if __name__ == "__main__":
    print(json.dumps(measure(" ".join(sys.argv[1:]) or "What's the weather like in Cape Town?"), indent=2))

"""Long-term memory through Hindsight (https://github.com/vectorize-io/hindsight).

FreeHand never runs out of memory space: Hindsight stores any number of
facts in a database, and FreeHand recalls only the few that matter for each
message. Nothing has to be deleted to make room, so there is no "memory is
full" step.

- ``recall(query)``: the facts most relevant to a message, as short lines.
- ``retain(text)``: save a conversation turn or a fact. Hindsight's own LLM
  extracts the facts; FreeHand sends it in the background.
- ``list_facts`` / ``update_fact`` / ``forget_fact``: the Memory page, where
  people can see, correct and remove what FreeHand remembers. Forgetting is
  a soft retire ("invalidated"), so it can be undone in Hindsight.

Only Hindsight's raw facts (types ``world`` and ``experience``) can be
edited. Its ``observation`` summaries are rebuilt from the facts, so they
are recalled but not listed for editing.

Settings (``settings.json`` key ``memory``, then environment, then default):

- ``hindsight_url`` / ``FREEHAND_HINDSIGHT_URL``: default
  ``http://127.0.0.1:8888`` (compose sets ``http://host.docker.internal:8888``).
- ``bank_id`` / ``FREEHAND_MEMORY_BANK``: default ``freehand``. One bank per
  person keeps memories from mixing.
- ``enabled``: default True. When Hindsight is unreachable every function
  returns quietly (empty results or an error dict), so chat still works.

Never raises into the agent loop.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

log = logging.getLogger("freehand.long_memory")

EDITABLE_TYPES = ("world", "experience")

# Turns that look like they contain a secret are never sent to memory.
_SECRET_PATTERNS = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{10,}|AIza[0-9A-Za-z_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(password|passwd|passcode|api[_ ]?key|secret|token|pin)\b\s*(is|:|=)\s*\S+)",
    re.IGNORECASE,
)


def _settings() -> dict:
    try:
        from core.agent_config import _load_settings
        mem = _load_settings().get("memory") or {}
        return mem if isinstance(mem, dict) else {}
    except Exception:  # settings must never break memory, nor memory chat
        return {}


def enabled() -> bool:
    return bool(_settings().get("enabled", True))


def base_url() -> str:
    url = _settings().get("hindsight_url") or os.environ.get("FREEHAND_HINDSIGHT_URL") or "http://127.0.0.1:8888"
    return str(url).rstrip("/")


def bank_id() -> str:
    bank = _settings().get("bank_id") or os.environ.get("FREEHAND_MEMORY_BANK") or "freehand"
    return str(bank).strip() or "freehand"


def looks_secret(text: str) -> bool:
    return bool(_SECRET_PATTERNS.search(text or ""))


def _bank_path(suffix: str = "") -> str:
    return f"/v1/default/banks/{urllib.parse.quote(bank_id(), safe='')}{suffix}"


def _call(method: str, path: str, body: Optional[dict] = None, timeout: float = 10.0) -> tuple[Optional[int], Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace"))
        except (json.JSONDecodeError, OSError):
            detail = None
        return e.code, detail
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        log.debug("hindsight %s %s failed: %s", method, path, e)
        return None, None


_bank_ready: set = set()


def _ensure_bank() -> bool:
    """Create the bank on first use (PUT is create-or-update)."""
    key = (base_url(), bank_id())
    if key in _bank_ready:
        return True
    status, _ = _call("PUT", _bank_path(), {})
    if status is not None and 200 <= status < 300:
        _bank_ready.add(key)
        return True
    return False


def clean_text(text: str) -> str:
    """Hindsight appends ' | When: ... | Involving: ...' to facts; people
    only need the sentence."""
    return (text or "").split(" | ")[0].strip()


def status() -> dict:
    """For the Memory page: is long-term memory working, and where."""
    if not enabled():
        return {"enabled": False, "reachable": False, "bank_id": bank_id()}
    code, _ = _call("GET", "/health", timeout=3.0)
    return {"enabled": True, "reachable": code == 200, "bank_id": bank_id(), "url": base_url()}


def recall(query: str, max_tokens: int = 600, timeout: float = 6.0) -> list[str]:
    """Short lines FreeHand should know for this message. [] on any problem."""
    if not enabled() or not (query or "").strip():
        return []
    if not _ensure_bank():
        return []
    code, data = _call(
        "POST", _bank_path("/memories/recall"),
        {"query": query[:2000], "max_tokens": max_tokens, "prefer_observations": True},
        timeout=timeout,
    )
    if code != 200 or not isinstance(data, dict):
        return []
    lines, seen = [], set()
    for r in data.get("results") or []:
        text = clean_text(r.get("text", "")) if isinstance(r, dict) else ""
        if text and text.lower() not in seen:
            seen.add(text.lower())
            lines.append(text)
    return lines


def retain(text: str, context: str = "", wait: bool = False) -> dict:
    """Save text (a conversation turn or a fact). Background by default."""
    if not enabled():
        return {"error": "Long-term memory is turned off."}
    text = (text or "").strip()
    if not text:
        return {"error": "Nothing to remember."}
    if looks_secret(text):
        return {"error": "That looks like a password or key, so FreeHand won't keep it in memory."}
    if not _ensure_bank():
        return {"error": "FreeHand's memory (Hindsight) isn't reachable right now."}
    # Whose memory this is (core/people.py). Recall doesn't filter on it
    # yet; it's there so a shared pool and private memories can be told
    # apart once FreeHand has several people (docs/design/people.md).
    from core import people
    item = {"content": text[:8000], "tags": people.memory_tags(),
            "metadata": {"person": people.current()}}
    if context:
        item["context"] = context[:200]
    code, data = _call(
        "POST", _bank_path("/memories"), {"items": [item], "async": not wait},
        timeout=120.0 if wait else 15.0,
    )
    if code is None or not (200 <= code < 300):
        return {"error": "FreeHand couldn't save that to memory. Try again later."}
    return {"saved": True}


def list_facts(limit: int = 50, offset: int = 0, query: str = "") -> dict:
    """Editable facts, newest first, for the Memory page."""
    if not _ensure_bank():
        return {"error": "FreeHand's memory (Hindsight) isn't reachable right now.", "items": []}
    items = []
    for fact_type in EDITABLE_TYPES:
        # Fetch generously: duplicates are merged below.
        params = {"type": fact_type, "state": "valid", "limit": min(1000, (limit + offset) * 2)}
        if query:
            params["q"] = query
        code, data = _call("GET", _bank_path("/memories/list?" + urllib.parse.urlencode(params)))
        if code != 200 or not isinstance(data, dict):
            return {"error": "FreeHand couldn't read its memory right now.", "items": []}
        for m in data.get("items") or []:
            items.append({
                "id": m.get("id"),
                "text": clean_text(m.get("text", "")),
                "when": m.get("mentioned_at") or m.get("date") or "",
                "type": fact_type,
            })
    items.sort(key=lambda m: m["when"] or "", reverse=True)
    # The same fact is often saved more than once (said on the Memory page,
    # then again in a chat). Show it once; "ids" holds every copy so
    # Forget and Correct handle them all.
    merged: dict = {}
    for m in items:
        key = m["text"].lower()
        if key in merged:
            if m["id"] not in merged[key]["ids"]:
                merged[key]["ids"].append(m["id"])
        else:
            merged[key] = {**m, "ids": [m["id"]]}
    unique = list(merged.values())
    return {"items": unique[offset:offset + limit], "total": len(unique)}


def update_fact(memory_id: str, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"error": "The memory can't be empty. Use Forget to remove it."}
    if looks_secret(text):
        return {"error": "That looks like a password or key, so FreeHand won't keep it in memory."}
    code, data = _call("PATCH", _bank_path(f"/memories/{urllib.parse.quote(memory_id, safe='')}"),
                       {"text": text[:2000]}, timeout=60.0)
    if code == 200:
        return {"updated": True}
    return {"error": _detail(data) or "FreeHand couldn't change that memory."}


def forget_fact(memory_id: str, reason: str = "Forgotten at the user's request") -> dict:
    mid = urllib.parse.quote(memory_id, safe="")
    # Hindsight also builds summary "observations" from facts. Retiring a
    # fact left those in place, so a forgotten fact could still be recalled
    # through its summary. Clear them first.
    _call("DELETE", _bank_path(f"/memories/{mid}/observations"), timeout=30.0)
    code, data = _call("PATCH", _bank_path(f"/memories/{mid}"),
                       {"state": "invalidated", "reason": reason[:200]}, timeout=30.0)
    if code == 200:
        return {"forgotten": True}
    return {"error": _detail(data) or "FreeHand couldn't forget that memory."}


def forget_matching(description: str) -> dict:
    """For "forget that ..." in chat: retire the closest matching fact."""
    if not _ensure_bank():
        return {"error": "FreeHand's memory (Hindsight) isn't reachable right now."}
    code, data = _call(
        "POST", _bank_path("/memories/recall"),
        {"query": description[:1000], "types": list(EDITABLE_TYPES), "max_tokens": 300},
    )
    results = (data or {}).get("results") if code == 200 and isinstance(data, dict) else None
    if not results:
        return {"error": "FreeHand couldn't find a memory like that."}
    best = results[0]
    text = clean_text(best.get("text", ""))
    # Retire every saved copy of the same fact, not just the closest one.
    ids = [r.get("id", "") for r in results if clean_text(r.get("text", "")).lower() == text.lower()]
    out = {}
    for mid in ids or [best.get("id", "")]:
        out = forget_fact(mid)
        if out.get("error"):
            return out
    out["text"] = text
    return out


def _detail(data: Any) -> str:
    if isinstance(data, dict):
        d = data.get("detail")
        if isinstance(d, str):
            return d[:200]
    return ""

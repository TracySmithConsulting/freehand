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


# ── Choosing the memory bank (Nov 2026) ─────────────────────────────
# One Hindsight can hold several banks, for example one for FreeHand and
# one for Hermes. People choose which FreeHand uses: its own (the safe
# default) or one they share on purpose.

BANK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def list_banks() -> dict:
    """The banks on this Hindsight, with how many memories each holds."""
    code, data = _call("GET", "/v1/default/banks?limit=100", timeout=8.0)
    if code != 200 or not isinstance(data, dict):
        return {"error": "FreeHand's memory (Hindsight) isn't reachable right now.", "banks": [], "current": bank_id()}
    banks = [{
        "bank_id": str(b.get("bank_id") or ""),
        "facts": int(b.get("fact_count") or 0),
        "last_write": b.get("last_write_at") or "",
    } for b in data.get("banks") or [] if isinstance(b, dict) and b.get("bank_id")]
    current = bank_id()
    if not any(b["bank_id"] == current for b in banks):
        # Not created yet (FreeHand creates its bank on first use).
        banks.append({"bank_id": current, "facts": 0, "last_write": "", "new": True})
    banks.sort(key=lambda b: (b["bank_id"] != current, b["bank_id"].lower()))
    return {"banks": banks, "current": current}


def choose_bank(new_bank: str) -> dict:
    """Make FreeHand use another bank (created on first use if new)."""
    new_bank = (new_bank or "").strip()
    if not BANK_ID_RE.match(new_bank):
        return {"ok": False, "message": "Use letters, numbers, dashes, dots or underscores for the bank's name, up to 64."}
    from core.agent_config import _load_settings, _save_settings
    settings = _load_settings()
    mem = settings.get("memory") if isinstance(settings.get("memory"), dict) else {}
    mem["bank_id"] = new_bank
    settings["memory"] = mem
    _save_settings(settings)
    return {"ok": True, "bank_id": new_bank,
            "message": f"FreeHand now uses the memory bank \"{new_bank}\". Memories in the old bank are still there."}


def status() -> dict:
    """For the Memory page: is long-term memory working, and where."""
    if not enabled():
        return {"enabled": False, "reachable": False, "bank_id": bank_id()}
    code, _ = _call("GET", "/health", timeout=3.0)
    return {"enabled": True, "reachable": code == 200, "bank_id": bank_id(), "url": base_url()}


def _scope(person_id: Optional[str] = None) -> tuple[list, str]:
    """Whose memories this person may recall, list or change: their own
    (tag ``person:<id>``) and the shared pool (tag ``shared``).

    A member's match is strict: Hindsight's default, "any", also returns
    memories with no tags at all. Those are from before FreeHand had
    people, so they're the owner's, and only the owner's match includes
    them. Hindsight builds its summaries (observations) per tag set, so a
    summary never mixes one person's memories into another's (checked
    against a real Hindsight, November 2026)."""
    from core import people
    person_id = person_id or people.current()
    return [f"person:{person_id}", "shared"], ("any" if person_id == people.OWNER_ID else "any_strict")


def _visible(memory_id: str) -> bool:
    """Whether the current person may change or forget this memory."""
    code, data = _call("GET", _bank_path(f"/memories/{urllib.parse.quote(memory_id, safe='')}"))
    if code != 200 or not isinstance(data, dict):
        return False
    tags, match = _scope()
    have = set(data.get("tags") or [])
    return bool(have & set(tags)) or (not have and match == "any")


SHARED_TAG = "shared"


def _change_refusal(memory_id: str) -> Optional[str]:
    """Why the current person may not correct or forget this memory, or
    None. Anyone recalls the shared pool; only the person who shared a
    memory, or the owner, changes it."""
    from core import people
    code, data = _call("GET", _bank_path(f"/memories/{urllib.parse.quote(memory_id, safe='')}"))
    if code != 200 or not isinstance(data, dict) or not _visible(memory_id):
        return "FreeHand couldn't find that memory."  # someone else's is "not found", like a wrong id
    tags = set(data.get("tags") or [])
    me = people.current()
    if SHARED_TAG in tags and me != people.OWNER_ID and f"person:{me}" not in tags:
        owner = people.list_people()[0]["name"]
        return f"Someone else shared that memory, so only they or {owner} can change it."
    return None


def recall(query: str, max_tokens: int = 600, timeout: float = 6.0) -> list[str]:
    """Short lines FreeHand should know for this message. [] on any problem."""
    if not enabled() or not (query or "").strip():
        return []
    if not _ensure_bank():
        return []
    tags, match = _scope()
    code, data = _call(
        "POST", _bank_path("/memories/recall"),
        {"query": query[:2000], "max_tokens": max_tokens, "prefer_observations": True,
         "tags": tags, "tags_match": match},
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


def retain(text: str, context: str = "", wait: bool = False, shared: bool = False) -> dict:
    """Save text (a conversation turn or a fact). Background by default.
    ``shared``: into the shared pool everyone on this FreeHand recalls
    ("share this with the office"), still marked as whose it was."""
    if not enabled():
        return {"error": "Long-term memory is turned off."}
    text = (text or "").strip()
    if not text:
        return {"error": "Nothing to remember."}
    if looks_secret(text):
        return {"error": "That looks like a password or key, so FreeHand won't keep it in memory."}
    if not _ensure_bank():
        return {"error": "FreeHand's memory (Hindsight) isn't reachable right now."}
    # Whose memory this is (core/people.py). Recall, the Memory page and
    # forgetting only reach the person's own and the shared pool (_scope).
    from core import people
    item = {"content": text[:8000], "tags": people.memory_tags() + ([SHARED_TAG] if shared else []),
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


def retain_many(lines: list, context: str, tag: str) -> dict:
    """Save several facts at once, marked with ``tag`` so they can all be
    found again (core/chat_import.py: what came from ChatGPT or Claude).
    Lines that look like a password or key are left out. In the
    background: Hindsight works through them in its own time."""
    if not enabled():
        return {"error": "Long-term memory is turned off."}
    clean = [str(x).strip()[:2000] for x in lines if str(x or "").strip()]
    kept = [x for x in clean if not looks_secret(x)]
    if not kept:
        return {"saved": 0, "left_out": len(clean)}
    if not _ensure_bank():
        return {"error": "FreeHand's memory (Hindsight) isn't reachable right now."}
    from core import people
    tags = people.memory_tags() + [tag]
    items = [{"content": x, "tags": tags, "context": context[:200], "metadata": {"person": people.current()}}
             for x in kept]
    for start in range(0, len(items), 50):  # a few requests, not hundreds
        code, _ = _call("POST", _bank_path("/memories"), {"items": items[start:start + 50], "async": True},
                        timeout=30.0)
        if code is None or not (200 <= code < 300):
            return {"error": "FreeHand couldn't save those to memory. Try again later.", "saved": start}
    return {"saved": len(kept), "left_out": len(clean) - len(kept)}


def forget_tagged(tag: str) -> Optional[int]:
    """Delete the current person's memories marked with ``tag`` (their
    documents, so the facts and summaries made from them go too). How
    many, or None if memory can't be reached."""
    from core import people
    if not enabled():
        return 0
    if not _ensure_bank():
        return None
    me, done = f"person:{people.current()}", 0
    for _ in range(1000):
        params = {"tags": [me, tag], "tags_match": "all_strict", "limit": 100}  # theirs, from this import
        code, data = _call("GET", _bank_path("/documents?" + urllib.parse.urlencode(params, doseq=True)))
        if code != 200 or not isinstance(data, dict):
            return None
        mine = [d for d in data.get("items") or [] if isinstance(d, dict) and d.get("id")
                and me in (d.get("tags") or []) and tag in (d.get("tags") or [])]
        if not mine:
            return done
        for d in mine:
            code, _ = _call("DELETE", _bank_path(f"/documents/{urllib.parse.quote(d['id'], safe='')}"), timeout=60.0)
            if code is None or not 200 <= code < 300:
                return None
            done += 1
    return done


def list_facts(limit: int = 50, offset: int = 0, query: str = "") -> dict:
    """Editable facts, newest first, for the Memory page."""
    if not _ensure_bank():
        return {"error": "FreeHand's memory (Hindsight) isn't reachable right now.", "items": []}
    items = []
    for fact_type in EDITABLE_TYPES:
        # Fetch generously: duplicates are merged below.
        tags, match = _scope()
        params = {"type": fact_type, "state": "valid", "limit": min(1000, (limit + offset) * 2),
                  "tags": tags, "tags_match": match}
        if query:
            params["q"] = query
        code, data = _call("GET", _bank_path("/memories/list?" + urllib.parse.urlencode(params, doseq=True)))
        if code != 200 or not isinstance(data, dict):
            return {"error": "FreeHand couldn't read its memory right now.", "items": []}
        from core import people
        me = people.current()
        for m in data.get("items") or []:
            mtags = set(m.get("tags") or [])
            mine = f"person:{me}" in mtags or (
                me == people.OWNER_ID and not any(t.startswith("person:") for t in mtags))
            shared = SHARED_TAG in mtags
            items.append({
                "id": m.get("id"),
                "text": clean_text(m.get("text", "")),
                "when": m.get("mentioned_at") or m.get("date") or "",
                "type": fact_type,
                "shared": shared,
                "mine": mine,
                "can_change": mine or not shared or me == people.OWNER_ID,
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
    refused = _change_refusal(memory_id)
    if refused:
        return {"error": refused}
    code, data = _call("PATCH", _bank_path(f"/memories/{urllib.parse.quote(memory_id, safe='')}"),
                       {"text": text[:2000]}, timeout=60.0)
    if code == 200:
        return {"updated": True}
    return {"error": _detail(data) or "FreeHand couldn't change that memory."}


def forget_fact(memory_id: str, reason: str = "Forgotten at the user's request") -> dict:
    refused = _change_refusal(memory_id)
    if refused:
        return {"error": refused}
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
        {"query": description[:1000], "types": list(EDITABLE_TYPES), "max_tokens": 300,
         "tags": _scope()[0], "tags_match": _scope()[1]},
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

"""People who wrote to FreeHand on Telegram or WhatsApp but aren't on it.

FreeHand doesn't answer them, as before. It only remembers the last few,
for a day and in memory, so the owner can add someone on the People page
in one click, instead of hunting for a Telegram chat ID. Only private
chats: a group isn't a person.
"""
from __future__ import annotations

import threading
import time

KEEP = 10
KEEP_SECONDS = 24 * 3600

_lock = threading.Lock()
_seen: dict[tuple[str, str], dict] = {}


def note(channel: str, ident: str, name: str = "") -> None:
    ident = str(ident or "").strip()
    if not ident:
        return
    with _lock:
        _seen[(channel, ident)] = {"channel": channel, "id": ident,
                                   "name": " ".join(str(name or "").split())[:60], "when": time.time()}
        if len(_seen) > KEEP:
            oldest = min(_seen, key=lambda k: _seen[k]["when"])
            _seen.pop(oldest, None)


def recent() -> list[dict]:
    """Newest first, without anyone added since."""
    from core import people
    s = people._settings()
    now = time.time()
    with _lock:
        for key in [k for k, v in _seen.items() if now - v["when"] > KEEP_SECONDS]:
            _seen.pop(key, None)
        items = sorted(_seen.values(), key=lambda v: v["when"], reverse=True)
    return [dict(v) for v in items if not people.link_owner(v["channel"], v["id"], s)]


def forget(channel: str, ident: str) -> None:
    with _lock:
        _seen.pop((channel, str(ident)), None)


def clear() -> None:
    with _lock:
        _seen.clear()

"""Search every model FreeHand can reach, grouped by service (Nov 2026).

For tinkerers: one search box across the services this FreeHand has keys
for (the main model and each backup), plus OpenRouter's public list,
which needs no key to browse. Results are grouped by service, so people
can see which service each model comes through.

Keys stay on the server: a key is only ever sent to its own service
(llm_providers.list_models), and choosing a model reuses the key already
saved for that service, so the browser never handles it.
"""
from __future__ import annotations

import threading
import time
from typing import Optional
from urllib.parse import urlsplit

from core import agent_config, llm_providers

CACHE_SECONDS = 600
PER_GROUP = 40
_cache: dict = {}
_lock = threading.Lock()


def _accounts() -> list[dict]:
    """The services this FreeHand can use: the main model, the backups,
    then OpenRouter's public list (to browse, even without a key)."""
    out: list[dict] = []
    seen = set()
    configs = [("main", agent_config.get_llm_config())]
    configs += [(f"backup:{i}", b) for i, b in enumerate(agent_config.get_llm_backups())]
    for source, cfg in configs:
        base_in = cfg.get("base_url", "")
        pid = llm_providers.identify(cfg.get("provider", ""), base_in)
        base = llm_providers.base_url_for(pid, base_in)
        preset = llm_providers.get(pid) or {}
        key = (cfg.get("api_key") or "").strip()
        if not base or not (key or preset.get("no_key")):
            continue
        ident = (pid, base.lower())
        if ident in seen:
            continue
        seen.add(ident)
        out.append({"provider": pid, "base_url": "" if base == preset.get("base_url", "").rstrip("/") else base,
                    "api_key": key, "source": source, "name": _name(pid, base)})
    if not any(a["provider"] == "openrouter" for a in out):
        out.append({"provider": "openrouter", "base_url": "", "api_key": "", "source": "public",
                    "name": _name("openrouter", "")})
    return out


def _name(pid: str, base: str) -> str:
    preset = llm_providers.get(pid) or {}
    if pid == "custom" or not preset:
        return urlsplit(base).hostname or "Another service"
    return preset.get("name") or pid


def _models(acct: dict) -> tuple[list, str]:
    ident = (acct["provider"], acct["base_url"], hash(acct["api_key"]))
    with _lock:
        hit = _cache.get(ident)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2]
    # The whole list: alphabetical lists cut at 300 lost most of OpenRouter's.
    models, problem = llm_providers.list_models(acct["provider"], acct["api_key"], acct["base_url"],
                                                timeout=10.0, limit=5000)
    models = models or []
    with _lock:
        _cache[ident] = (time.time(), models, problem)
    return models, problem


def search(query: str) -> dict:
    """Models whose names contain every word typed, grouped by service."""
    words = [w for w in (query or "").lower().split() if w]
    groups = []
    for acct in _accounts():
        models, problem = _models(acct)
        matches = [m for m in models if all(w in m.lower() for w in words)] if words else []
        groups.append({
            "provider": acct["provider"],
            "base_url": acct["base_url"],
            "name": acct["name"],
            "ready": acct["source"] != "public",
            "total": len(models),
            "matches": len(matches),
            "models": matches[:PER_GROUP],
            "problem": problem,
        })
    return {"query": query or "", "groups": groups}


def _find(provider: str, base_url: str) -> Optional[dict]:
    base = (base_url or "").rstrip("/").lower()
    for acct in _accounts():
        if acct["provider"] == provider and acct["base_url"].rstrip("/").lower() == base:
            return acct
    return None


def pick(provider: str, model: str, base_url: str = "", action: str = "test") -> dict:
    """Test a model from the search, or make it the main model or a backup,
    using the key already saved for its service."""
    model = (model or "").strip()
    acct = _find(provider, base_url)
    if not model or not acct:
        return {"ok": False, "message": "Choose a model from the search results first."}
    preset = llm_providers.get(acct["provider"]) or {}
    if not acct["api_key"] and not preset.get("no_key"):
        how = "Sign in with OpenRouter" if acct["provider"] == "openrouter" else f"Add a key for {acct['name']}"
        return {"ok": False, "message": f"{how} first, then choose this model again."}
    settings = {"provider": acct["provider"], "model": model, "api_key": acct["api_key"], "base_url": acct["base_url"]}

    if action == "test":
        return llm_providers.test(acct["provider"], model, acct["api_key"], acct["base_url"])
    if action == "backup":
        agent_config.add_llm_backup(settings)
        return {"ok": True, "message": f"Added {model} from {acct['name']} as a backup."}
    if action == "main":
        if acct["source"].startswith("backup:"):
            index = int(acct["source"].split(":", 1)[1])
            backups = agent_config.get_llm_backups()
            backups[index]["model"] = model
            agent_config._save_llm_backups(backups)
            agent_config.promote_llm_backup(index)
            note = " Your previous main model is now the first backup."
        else:
            agent_config.set_llm_config(settings)
            note = ""
        return {"ok": True, "message": f"FreeHand now uses {model} from {acct['name']}.{note}"}
    return {"ok": False, "message": "Unknown action."}

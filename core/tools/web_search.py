"""Searching the web.

Three ways, chosen in Settings (settings "web_search"):

- **DuckDuckGo** (the default): free, no account or key. It reads
  DuckDuckGo's plain results page, which DuckDuckGo may limit if FreeHand
  searches a lot.
- **Brave Search**: sturdier, with a free key from brave.com/search/api
  (about 2,000 searches a month free). The key is sent only to Brave.
- **SearXNG**: the person's own private search server, by its address.

Results are short (title, address and a line or two), five by default, so
they cost little to send to the model. The same search within ten minutes
comes from memory instead of searching again.
"""
from __future__ import annotations

import html
import re
import time
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

PROVIDERS = ("duckduckgo", "brave", "searxng")
MAX_RESULTS = 10
CACHE_SECONDS = 600
_cache: dict = {}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")


def settings() -> dict:
    from core.agent_config import _load_settings
    s = (_load_settings() or {}).get("web_search") or {}
    provider = s.get("provider") if s.get("provider") in PROVIDERS else "duckduckgo"
    return {"provider": provider, "brave_key": str(s.get("brave_key") or "").strip(),
            "searxng_url": str(s.get("searxng_url") or "").strip().rstrip("/")}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text or ""))).strip()


def _ddg_url(href: str) -> str:
    """DuckDuckGo sometimes wraps the address in its own redirect."""
    href = html.unescape(href or "")
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) or href
    return href


def parse_duckduckgo(body: str, limit: int) -> list[dict]:
    out = []
    for block in re.split(r'<div class="result ', body)[1:]:
        if block.startswith("result--ad") or "result--ad" in block[:80]:
            continue  # adverts
        link = re.search(r'class="result__a"[^>]*?\shref="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>', block, re.S)
        out.append({"title": _clean(link.group(2))[:150], "url": _ddg_url(link.group(1)),
                    "snippet": _clean(snippet.group(1))[:240] if snippet else ""})
        if len(out) >= limit:
            break
    return out


async def _duckduckgo(session, query: str, limit: int) -> list[dict]:
    async with session.post("https://html.duckduckgo.com/html/", data={"q": query},
                            headers={"User-Agent": _UA}) as r:
        body = await r.text()
        if r.status != 200 or "anomaly" in body.lower():
            raise LookupError("The free search (DuckDuckGo) didn't answer: it may be limiting searches. "
                              "Try again later, or add a free Brave Search key in Settings, Web search.")
    return parse_duckduckgo(body, limit)


async def _brave(session, query: str, limit: int, key: str) -> list[dict]:
    async with session.get("https://api.search.brave.com/res/v1/web/search",
                           params={"q": query, "count": str(limit)},
                           headers={"X-Subscription-Token": key, "Accept": "application/json"}) as r:
        if r.status in (401, 403):
            raise LookupError("Brave Search didn't accept the key. Check it in Settings, Web search.")
        if r.status == 429:
            raise LookupError("Brave Search says this month's free searches are used up, or there were "
                              "too many at once. Try again later.")
        if r.status != 200:
            raise LookupError(f"Brave Search answered with an error ({r.status}).")
        data = await r.json()
    return [{"title": _clean(x.get("title"))[:150], "url": x.get("url", ""),
             "snippet": _clean(x.get("description"))[:240]}
            for x in ((data.get("web") or {}).get("results") or [])[:limit]]


async def _searxng(session, query: str, limit: int, base: str) -> list[dict]:
    if not re.match(r"https?://", base):
        raise LookupError("Give your SearXNG server's full address in Settings, Web search.")
    async with session.get(f"{base}/search", params={"q": query, "format": "json"},
                           headers={"User-Agent": "FreeHand"}) as r:
        if r.status != 200:
            raise LookupError(f"Your SearXNG server answered with an error ({r.status}). "
                              "Check that JSON results are switched on in its settings.")
        data = await r.json()
    return [{"title": _clean(x.get("title"))[:150], "url": x.get("url", ""),
             "snippet": _clean(x.get("content"))[:240]}
            for x in (data.get("results") or [])[:limit]]


async def web_search(query: str, max_results: int = 5, provider: Optional[str] = None) -> dict:
    """Search the web. Returns {"query", "source", "results": [{title, url, snippet}]}
    or {"error": plain words}."""
    import aiohttp
    query = re.sub(r"\s+", " ", str(query or "")).strip()[:300]
    if not query:
        return {"error": "Say what to search for."}
    try:
        limit = max(1, min(MAX_RESULTS, int(max_results or 5)))
    except (TypeError, ValueError):
        limit = 5
    s = settings()
    provider = provider if provider in PROVIDERS else s["provider"]
    if provider == "brave" and not s["brave_key"]:
        provider = "duckduckgo"  # no key yet: the free search still works
    if provider == "searxng" and not s["searxng_url"]:
        provider = "duckduckgo"
    key = (provider, query.lower(), limit)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if provider == "brave":
                results = await _brave(session, query, limit, s["brave_key"])
            elif provider == "searxng":
                results = await _searxng(session, query, limit, s["searxng_url"])
            else:
                results = await _duckduckgo(session, query, limit)
    except LookupError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"The search couldn't be done: {str(e)[:120]}"}
    out = {"query": query, "source": {"duckduckgo": "DuckDuckGo", "brave": "Brave Search",
                                      "searxng": "your SearXNG server"}[provider],
           "results": results}
    if not results:
        out["note"] = "Nothing was found. Try different words."
    _cache[key] = (time.time(), out)
    if len(_cache) > 200:
        _cache.pop(next(iter(_cache)))
    return out

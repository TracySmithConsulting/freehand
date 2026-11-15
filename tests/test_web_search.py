"""Searching the web (core/tools/web_search.py): free by default with
DuckDuckGo, or Brave Search with a key, or the person's own SearXNG."""
import asyncio
import json

import pytest

from core.tools import web_search as ws

DDG_PAGE = """
<div class="result results_links results_links_deep result--ad "><a rel="nofollow" class="result__a" href="https://ads.example/x">Buy now</a>
<a class="result__snippet" href="#">An advert</a></div>
<div class="result results_links results_links_deep web-result "><h2><a rel="nofollow" class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.capetown.gov.za%2Fload-shedding&amp;rut=abc">Load-shedding - City of <b>Cape Town</b></a></h2>
<a class="result__snippet" href="#">Refer to the schedule for the stage announced &amp; your area.</a></div>
<div class="result results_links results_links_deep web-result "><h2><a rel="nofollow" class="result__a"
 href="https://eskom.co.za/">Eskom</a></h2><a class="result__snippet" href="#">Eskom news.</a></div>
"""


@pytest.fixture(autouse=True)
def fresh():
    ws._cache.clear()


def test_duckduckgo_results_are_read_without_adverts():
    results = ws.parse_duckduckgo(DDG_PAGE, 5)
    assert results == [
        {"title": "Load-shedding - City of Cape Town", "url": "https://www.capetown.gov.za/load-shedding",
         "snippet": "Refer to the schedule for the stage announced & your area."},
        {"title": "Eskom", "url": "https://eskom.co.za/", "snippet": "Eskom news."},
    ]
    assert len(ws.parse_duckduckgo(DDG_PAGE, 1)) == 1


@pytest.fixture
def searches(monkeypatch):
    calls = []

    async def fake_ddg(session, query, limit):
        calls.append(("duckduckgo", query, limit))
        return [{"title": "A", "url": "https://a.example", "snippet": "a"}]

    async def fake_brave(session, query, limit, key):
        calls.append(("brave", query, key))
        return [{"title": "B", "url": "https://b.example", "snippet": "b"}]
    monkeypatch.setattr(ws, "_duckduckgo", fake_ddg)
    monkeypatch.setattr(ws, "_brave", fake_brave)
    return calls


def _settings(monkeypatch, **web):
    monkeypatch.setattr("core.agent_config._load_settings", lambda: {"web_search": web})


def test_free_search_by_default(searches):
    out = asyncio.run(ws.web_search("load shedding Cape Town"))
    assert out["source"] == "DuckDuckGo" and out["results"][0]["title"] == "A"
    assert searches == [("duckduckgo", "load shedding Cape Town", 5)]


def test_the_same_search_soon_after_comes_from_memory(searches):
    asyncio.run(ws.web_search("Load shedding  Cape Town"))
    asyncio.run(ws.web_search("load shedding cape town"))
    assert len(searches) == 1


def test_brave_is_used_with_a_key(searches, monkeypatch):
    _settings(monkeypatch, provider="brave", brave_key="secret-key")
    out = asyncio.run(ws.web_search("news"))
    assert out["source"] == "Brave Search" and searches == [("brave", "news", "secret-key")]


def test_brave_without_a_key_still_searches_free(searches, monkeypatch):
    _settings(monkeypatch, provider="brave")
    assert asyncio.run(ws.web_search("news"))["source"] == "DuckDuckGo"


def test_problems_are_said_plainly(monkeypatch):
    async def limited(session, query, limit):
        raise LookupError("The free search (DuckDuckGo) didn't answer: it may be limiting searches.")
    monkeypatch.setattr(ws, "_duckduckgo", limited)
    assert "may be limiting" in asyncio.run(ws.web_search("news"))["error"]
    assert asyncio.run(ws.web_search("   "))["error"] == "Say what to search for."


def test_the_tool_is_registered_and_offered_for_searches():
    from core import tool_selection
    from core.agent_config import TOOL_REGISTRY, list_available_tools
    assert TOOL_REGISTRY["web_search"] == "read"
    tools, _ = tool_selection.select(list_available_tools(), ["What's the latest news on load shedding?"], lean=True)
    assert "web_search" in [t["function"]["name"] for t in tools]


def test_team_members_who_use_websites_can_search():
    from core import team
    assert "web_search" in team.TOOL_CHOICES["web"]["tools"]
    assert "duckduckgo" not in json.dumps(team.TEMPLATES)  # no more workaround in the templates


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr("server.get_or_create_api_key", lambda: "k")
    c = TestClient(server.app)
    c.headers.update({"X-API-Key": "k"})
    return c


def test_the_key_is_saved_but_never_sent_back(client):
    assert client.get("/api/web-search").json() == {"provider": "duckduckgo", "has_brave_key": False, "searxng_url": ""}
    assert client.post("/api/web-search", json={"provider": "brave"}).status_code == 400  # no key yet
    saved = client.post("/api/web-search", json={"provider": "brave", "brave_key": "secret-key"}).json()
    assert saved == {"provider": "brave", "has_brave_key": True, "searxng_url": ""}
    assert "secret-key" not in client.get("/api/web-search").text
    # Saving again with an empty box keeps the key.
    assert client.post("/api/web-search", json={"provider": "brave"}).json()["has_brave_key"] is True
    assert client.post("/api/web-search", json={"provider": "searxng", "searxng_url": "not a url"}).status_code == 400
    assert client.post("/api/web-search", json={"provider": "everything"}).status_code == 400

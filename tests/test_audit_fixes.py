"""Fixes from the 21 November 2026 audit.

- S1: the page tools (get_axtree, extract_text and the rest) open only web
  pages on the internet: never files on this computer (file://), this
  computer's own services, or the local network. They run without an
  approval, so they mustn't get round the read-folder limit.
- S2: the console's escapeHtml escapes quote marks, and the Connections
  buttons don't build onclick code out of account labels.
- A1: choosing a page in the menu moves the focus to that page's heading.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import agent
from core.tools import browser

PAGE = (PROJECT_ROOT / "freehand" / "static" / "index.html").read_text(encoding="utf-8")


def _resolves_to(monkeypatch, table: dict):
    """Pretend DNS: each name answers with the address in table."""
    def fake(host, port, *args, **kwargs):
        if host in table:
            ip = table[host]
        else:
            try:
                socket.inet_pton(socket.AF_INET6 if ":" in host else socket.AF_INET, host)
                ip = host
            except OSError:
                raise socket.gaierror("not found")
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(browser.socket, "getaddrinfo", fake)


# ── S1: which pages the browser may open ─────────────────────────────────

@pytest.mark.parametrize("url", [
    "file:///app/vault/encryption.key",
    "file:///C:/Users/someone/.ssh/id_rsa",
    "javascript:alert(1)",
    "chrome://settings",
    "data:text/html,hello",
    "",
    "not an address",
])
def test_only_web_addresses(url):
    assert browser.page_refusal(url) == browser.NOT_A_WEB_PAGE


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/api/settings",
    "http://localhost:8888/",
    "http://[::1]/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://172.18.0.3:3001/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.101.102.103/",  # a Tailscale address
    "http://0.0.0.0/",
    "http://[::ffff:127.0.0.1]/",
    "http://hindsight:8888/",
    "http://open-connector:3001/",
])
def test_not_this_computer_or_the_local_network(monkeypatch, url):
    _resolves_to(monkeypatch, {"localhost": "127.0.0.1", "hindsight": "172.18.0.4",
                               "open-connector": "172.18.0.5"})
    assert browser.page_refusal(url) == browser.NOT_ON_THE_INTERNET


def test_a_name_that_points_inside_is_refused(monkeypatch):
    _resolves_to(monkeypatch, {"sneaky.example": "127.0.0.1"})
    assert browser.page_refusal("https://sneaky.example/") == browser.NOT_ON_THE_INTERNET


def test_a_name_that_cant_be_found_is_refused(monkeypatch):
    _resolves_to(monkeypatch, {})
    assert "couldn't find" in browser.page_refusal("https://nowhere.example/")


def test_pages_on_the_internet_are_fine(monkeypatch):
    _resolves_to(monkeypatch, {"example.com": "93.184.216.34", "news.example": "2606:2800:220:1::1"})
    assert browser.page_refusal("https://example.com/news?x=1") is None
    assert browser.page_refusal("http://news.example:8080/") is None


def test_no_browser_is_started_for_a_file(monkeypatch):
    async def must_not_launch(*a, **k):
        raise AssertionError("the browser was started for a file:// address")
    monkeypatch.setattr(browser, "_launch_browser", must_not_launch)
    with pytest.raises(browser.PageNotAllowed):
        asyncio.run(browser._get_page("file:///app/vault/encryption.key"))
    result = asyncio.run(browser.get_axtree("file:///app/vault/encryption.key"))
    assert "error" in result and "FreeHand only opens web pages" in result["error"]


class _FakeRequest:
    def __init__(self, url):
        self.url = url


class _FakeRoute:
    def __init__(self, url):
        self.request = _FakeRequest(url)
        self.outcome = None

    async def continue_(self):
        self.outcome = "continued"

    async def abort(self):
        self.outcome = "aborted"


class _FakeContext:
    def __init__(self, page):
        self.page = page
        self.guard = None

    async def route(self, pattern, handler):
        self.guard = handler

    def on(self, *a):
        pass

    async def new_page(self):
        return self.page


class _FakePage:
    def __init__(self, lands_on):
        self.url = "about:blank"
        self.lands_on = lands_on

    async def goto(self, url, **kwargs):
        self.url = self.lands_on

    async def wait_for_timeout(self, ms):
        pass


class _FakeBrowser:
    def __init__(self, page):
        self.context = _FakeContext(page)
        self.closed = False

    async def new_context(self):
        return self.context

    async def close(self):
        self.closed = True


def _fake_browser(monkeypatch, lands_on):
    fake = _FakeBrowser(_FakePage(lands_on))

    async def launch(*a, **k):
        return fake, "sandboxed"
    monkeypatch.setattr(browser, "_launch_browser", launch)
    return fake


def test_a_redirect_to_this_computer_is_refused(monkeypatch):
    _resolves_to(monkeypatch, {"example.com": "93.184.216.34"})
    fake = _fake_browser(monkeypatch, "http://127.0.0.1:8000/api/settings")
    with pytest.raises(browser.PageNotAllowed):
        asyncio.run(browser._get_page("https://example.com/"))
    assert fake.closed, "the browser should be closed, with nothing read from the page"


def test_a_page_cant_load_this_computers_services(monkeypatch):
    _resolves_to(monkeypatch, {"example.com": "93.184.216.34", "hindsight": "172.18.0.4"})
    fake = _fake_browser(monkeypatch, "https://example.com/")
    asyncio.run(browser._get_page("https://example.com/"))
    guard = fake.context.guard
    assert guard is not None, "every request the page makes should be checked"

    def outcome(url):
        route = _FakeRoute(url)
        asyncio.run(guard(route))
        return route.outcome

    assert outcome("https://example.com/style.css") == "continued"
    assert outcome("data:image/png;base64,AAAA") == "continued"
    assert outcome("http://hindsight:8888/v1/default/banks") == "aborted"
    assert outcome("http://127.0.0.1:8000/api/settings") == "aborted"
    assert outcome("file:///app/vault/settings.json") == "aborted"


@pytest.mark.parametrize("tool", ["extract_text", "get_axtree", "take_screenshot", "navigate"])
def test_the_ai_gets_a_plain_refusal(tool):
    out = json.loads(asyncio.run(agent.execute_tool(tool, {"url": "file:///app/vault/encryption.key"}))["content"])
    assert out["error"] == "not_allowed"
    assert out["tell_the_person"] == browser.NOT_A_WEB_PAGE


def test_every_browser_tool_that_takes_an_address_is_checked():
    """A new browser tool with a url must join agent.PAGE_TOOLS."""
    from core.agent_config import TOOL_SCHEMAS
    from core.tool_selection import BUILTIN_GROUPS
    browser_group = set(BUILTIN_GROUPS["browser"]) | set(BUILTIN_GROUPS["search"])
    takes_url = {name for name in browser_group
                 if "url" in TOOL_SCHEMAS.get(name, {}).get("parameters", {}).get("properties", {})}
    assert takes_url, "no browser tools found"
    assert takes_url <= agent.PAGE_TOOLS, f"not checked: {sorted(takes_url - agent.PAGE_TOOLS)}"


# ── S2: quote marks in names ─────────────────────────────────────────────

def _node():
    node = shutil.which("node")
    assert node, "Node.js is needed to check the page's script (it's installed in CI)"
    return node


def test_escapehtml_escapes_quote_marks(tmp_path):
    source = re.search(r"function escapeHtml\(str\) \{.*?\n      \}", PAGE, re.S)
    assert source, "escapeHtml not found in index.html"
    script = tmp_path / "escape.js"
    script.write_text(source.group(0) + "\nprocess.stdout.write(JSON.stringify(["
                      "escapeHtml('x\"onfocus=\"alert(1)'), escapeHtml(\"it's <b>&\"), escapeHtml(''), escapeHtml(null)]));",
                      encoding="utf-8")
    out = json.loads(subprocess.run([_node(), str(script)], capture_output=True, text=True, timeout=60).stdout)
    assert out == ["x&quot;onfocus=&quot;alert(1)", "it&#39;s &lt;b&gt;&amp;", "", ""]


def test_connections_buttons_dont_build_code_from_labels():
    for name in ("reconnectConnection", "testConnection", "disconnectService"):
        assert f'onclick="{name}(' not in PAGE, f"{name} is still written into onclick"
    assert 'data-act="test"' in PAGE and 'data-act="disconnect"' in PAGE


# ── Both in the real page script (the replay driver, tests/js_replay) ────

@pytest.fixture(scope="module")
def replay():
    node = _node()
    driver_dir = PROJECT_ROOT / "tests" / "js_replay"
    if not (driver_dir / "node_modules" / "linkedom").exists():
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        assert npm, "npm is needed to install linkedom for the page replay"
        subprocess.run([npm, "install", "--prefix", str(driver_dir), "--no-save", "linkedom"],
                       capture_output=True, timeout=300, check=True)
    from fastapi.testclient import TestClient
    import server
    html = TestClient(server.app).get("/").text

    def run(tmp_path, steps, verdict, responses=None):
        page = tmp_path / "index.html"
        page.write_text(html, encoding="utf-8")
        result = tmp_path / "result.json"
        answers = {"resultPath": str(result), "steps": steps, "verdict": verdict,
                   "responses": {"/api/approvals": [], "/api/settings": {}, "/api/tasks": [],
                                 "/api/security/tier": {"tier": "semi_autonomous"},
                                 "/api/gateway/status": {}, "/api/agents": [],
                                 "/api/agents/imported": [], "/api/runner/status": {"llm_configured": False},
                                 "/api/scribble": {"content": ""}, "/api/memory/files": [],
                                 **(responses or {})}}
        answers_path = tmp_path / "answers.json"
        answers_path.write_text(json.dumps(answers), encoding="utf-8")
        proc = subprocess.run([node, str(driver_dir / "replay_driver.js"), str(page), str(answers_path)],
                              capture_output=True, text=True, timeout=120, cwd=str(driver_dir))
        if result.exists():
            return json.loads(result.read_text(encoding="utf-8"))
        return {"threw": (proc.stdout + proc.stderr).strip()}
    return run


def test_a_label_with_quotes_stays_a_label(replay, tmp_path):
    label = "thandi-');window.HIT=1;//\" onfocus=\"window.HIT=2"
    verdict = replay(tmp_path, [
        {"click": "button[data-act='test']"},
        {"waitMs": 200},
    ], "({hit: window.HIT || 0, label: (document.querySelector('.connection-label') || {}).textContent || '',"
       " extra: [...document.querySelectorAll('.connection-card button')].some(b => b.hasAttribute('onfocus')),"
       " tested: window.__fetchLog.filter(k => k.indexOf('/test') !== -1)})",
        responses={"/api/integrations GET": {
            "connections": [{"service": "google", "connections": [
                {"label": label, "user_id": "", "connected_at": "2026-11-01T10:00:00",
                 "expires_at": "2036-01-01T00:00:00", "scopes": ["email"], "status": "active"}]}],
            "available_services": []},
            "/api/integrations/test": {"ok": True}})
    assert verdict.get("threw") is None, verdict.get("threw")
    assert verdict["stepError"] is None, verdict["stepError"]
    assert verdict["hit"] == 0, "a label ran as code"
    assert not verdict["extra"], "a label added an attribute to a button"
    assert 'onfocus="window.HIT=2' in verdict["label"], "the label should show as plain text"
    assert verdict["tested"], "the Test button no longer tests the connection"


# ── A1: the menu moves the focus ─────────────────────────────────────────

def test_choosing_a_page_in_the_menu_moves_the_focus_there():
    """The menu's click handler moves the focus to the page itself, and stops
    the browser following the link: jumping to a section that can't take
    the focus clears it, and the focus was left on the page body. The
    address goes in the history instead, so Back still works.

    The replay page (tests/js_replay) is a small stand-in without
    FreeHand's pages, so this was also checked by hand in a real browser:
    Enter or a click on a menu link lands on that page's heading, and Back
    returns to the last page with the focus on its heading."""
    handler = re.search(r"document\.addEventListener\('click', function\(e\) \{\s*"
                        r"var a = e\.target\.closest && e\.target\.closest\('a\[href\^=\"#\"\]'\);(.*?)\}, true\);",
                        PAGE, re.S)
    assert handler, "the page's link click handler wasn't found"
    body = handler.group(1)
    assert "e.preventDefault();" in body
    assert "history.pushState(null, '', '#' + id)" in body
    assert "revealSection(id, true);" in body


# ── The rest of the audit (Nov 2026) ─────────────────────────────────────

def test_spoken_names_start_with_the_visible_words():
    """WCAG 2.5.3: voice control users say what they see."""
    assert "look.setAttribute('aria-label', 'Look at it: ' + g.name);" in PAGE
    assert "['Sign out everywhere', 'Sign out everywhere: ' + p.name," in PAGE


def test_tick_boxes_are_24_pixels():
    """WCAG 2.5.8: at least 24 by 24 pixels."""
    rule = re.search(r'input\[type="checkbox"\], input\[type="radio"\] \{(.*?)\}', PAGE, re.S).group(1)
    assert "width: 1.5rem;" in rule and "height: 1.5rem;" in rule
    assert 'input[type="file"] { min-height: 1.5rem; }' in PAGE


def test_no_command_line_or_jargon_in_the_everyday_words():
    for old in ("jq -r .api_key", "Re-sync Vault Files", "No files indexed yet", "works standalone"):
        assert old not in PAGE, old


def test_a_name_that_cant_be_printed_doesnt_stop_freehand():
    """Windows output to a file or log is often cp1252: printing an arrow or
    a name like "Zoë" used to fail the request doing it."""
    import io
    import server  # noqa: F401  (sets up its output streams on import)
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    stream.reconfigure(errors="backslashreplace")
    print("Zoë → done", file=stream)
    stream.flush()
    assert b"Zo\xeb \u2192 done" in stream.buffer.getvalue()
    source = (PROJECT_ROOT / "server.py").read_text(encoding="utf-8")
    assert '_stream.reconfigure(errors="backslashreplace")' in source


def test_the_stray_npm_files_and_the_chrome_extension_are_gone():
    for gone in ("package.json", "package-lock.json", "freehand/chrome_extension"):
        assert not (PROJECT_ROOT / gone).exists(), gone

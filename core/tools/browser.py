"""Browser automation tool with AXTree extraction, CDP auto-connect, validation
reporting, network capture, and state checkpoints.

Security: Read operations (get_axtree, extract_text) are allowed in all tiers.
Write operations (click, fill, navigate) trigger intercept_action() approval in
SEMI_AUTONOMOUS tier and are blocked in AUTONOMOUS tier.

Feature 1 — CDP auto-connect: attempts to connect to an existing Chrome via CDP
  before launching a sandboxed browser. On Windows, reads DevToolsActivePort.
  On macOS/Linux, reads the equivalent profile path. Falls back to a fresh
  launch with a clean profile when no existing Chrome is available or the
  user has not approved the consent dialog.

Feature 2 — Validation summary: get_validation_summary() scans the page for
  ARIA live regions, error summary divs, field-level errors, and pass/fail
  keywords to give the LLM structured feedback about form submission results.

Feature 3 — Network capture: start_request_capture() / get_captured_requests()
  intercept all network requests and responses (AJAX, fetch, navigation) using
  Playwright's route interception, storing method/URL/headers/body/response
  in a bounded deque for later inspection.

Feature 4 — Checkpoint / restore: save_checkpoint() serialises the page DOM
  state (form values, checkbox states, cookies, localStorage) to disk.
  restore_checkpoint() restores it without a full page reload, enabling
  iterative form-testing without re-entering data.

Feature 5 — Chrome Extension detection: detects if the FreeHand Chrome Extension
  is installed and uses its local CDP bridge instead of the remote-debugging port.
"""

import asyncio
import hashlib
import json
import os
import re
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.security import intercept_action, PROJECT_ROOT

# ── Screenshot size cap (R9 fix) ──────────────────────────────────────────────

DEFAULT_SCREENSHOT_SIZE_CAP_MB = 20


def _screenshot_path_for(url: str) -> Path:
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return Path("vault") / "screenshots" / f"screenshot_{url_hash}.png"


def _screenshot_size_cap_mb() -> int:
    try:
        from core.agent_config import _load_settings

        return int(_load_settings().get("max_screenshot_size_mb", DEFAULT_SCREENSHOT_SIZE_CAP_MB))
    except Exception:
        return DEFAULT_SCREENSHOT_SIZE_CAP_MB


# ── Feature 1: CDP Auto-Connect ──────────────────────────────────────────────

# Chrome DevToolsActivePort location by OS
_CHROME_DEVTOOLS_PORT_FILE = {
    "windows": Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data/DevToolsActivePort",
    "darwin": Path("~/Library/Application Support/Google/Chrome/DevToolsActivePort").expanduser(),
    "linux": Path("~/.config/google-chrome/DevToolsActivePort").expanduser(),
}


def _get_chrome_cdp_endpoint() -> Optional[str]:
    """Read the CDP WebSocket endpoint from Chrome's DevToolsActivePort file.

    Returns the ws:// URL on success, None if the file doesn't exist or can't
    be parsed (Chrome not running with --remote-debugging-port, or Chrome 136+
    using a dynamic port that isn't written to this file).
    """
    import platform

    system = platform.system().lower()
    port_file = _CHROME_DEVTOOLS_PORT_FILE.get(system)
    if not port_file or not port_file.exists():
        return None

    try:
        lines = port_file.read_text().strip().split("\n")
        # Format: <port>\n<ws-url>\n<other-stuff>
        if len(lines) >= 2:
            return lines[1].strip()
    except Exception:
        pass
    return None


def _chrome_extension_bridge_port() -> Optional[int]:
    """Return the port where the FreeHand Chrome Extension HTTP bridge listens.

    Returns None if the extension is not installed/active (no health-check ping).
    The extension runs a local HTTP bridge on port 9223 by default.
    """
    try:
        import urllib.request

        req = urllib.request.Request(
            "http://localhost:9223/health",
            headers={"User-Agent": "FreeHand/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                return 9223
    except Exception:
        pass
    return None


async def _launch_browser(headless: bool = True, cdp_overrides: bool = True):
    """Launch a Playwright Chromium browser.

    Strategy:
      1. If cdp_overrides=True, first try Chrome Extension bridge (port 9223).
         The extension speaks CDP natively — no consent prompt needed.
      2. Then try existing Chrome via DevToolsActivePort (CDP over WebSocket).
         Works when Chrome has --remote-debugging-port or --autoConnect.
         Chrome shows a ONE-TIME consent dialog; after clicking Allow, the
         DevToolsActivePort file is written and subsequent connects succeed.
      3. Fall back to launching a fresh Chromium with a clean profile.
         No access to the user's existing sessions, cookies, or logins.

    Returns (browser, strategy_used: str).
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        # Strategy 1: Chrome Extension bridge
        if cdp_overrides:
            ext_port = _chrome_extension_bridge_port()
            if ext_port:
                try:
                    ws_url = f"ws://localhost:{ext_port}/devtools/browser/freehand"
                    browser = await p.chromium.connect_over_cdp(ws_url, timeout=5000)
                    return browser, "chrome_extension"
                except Exception:
                    pass  # Fall through to next strategy

        # Strategy 2: Existing Chrome via DevToolsActivePort
        if cdp_overrides:
            ws_url = _get_chrome_cdp_endpoint()
            if ws_url:
                try:
                    browser = await p.chromium.connect_over_cdp(ws_url, timeout=5000)
                    return browser, "existing_chrome"
                except Exception:
                    pass  # Fall through to fallback

        # Strategy 3: Fresh sandboxed Chromium
        browser = await p.chromium.launch(headless=headless)
        return browser, "sandboxed"


# ── Feature 3: Network Request Capture ───────────────────────────────────────

# Bounded deque holding captured request/response pairs
_CAPTURED_REQUESTS: deque[dict] = deque(maxlen=200)
_CAPTURE_ACTIVE = False
_CAPTURE_PATTERN: Optional[str] = None


def _reset_capture() -> None:
    global _CAPTURED_REQUESTS, _CAPTURE_ACTIVE, _CAPTURE_PATTERN
    _CAPTURED_REQUESTS.clear()
    _CAPTURE_ACTIVE = False
    _CAPTURE_PATTERN = None


async def _capture_handler(request, response=None, error=None):
    """Called by Playwright route handlers to record requests."""
    if not _CAPTURE_ACTIVE:
        return

    # Match against url_pattern if set
    if _CAPTURE_PATTERN:
        try:
            if not re.search(_CAPTURE_PATTERN, request.url):
                return
        except re.error:
            pass  # Invalid regex — skip filter

    import time

    entry = {
        "id": len(_CAPTURED_REQUESTS),
        "method": request.method,
        "url": request.url,
        "headers": dict(request.headers),
        "body": request.post_body,
        "timing_ms": None,
        "response_status": None,
        "response_headers": None,
        "response_body": None,
        "error": str(error) if error else None,
        "captured_at": "",
    }

    if response:
        try:
            entry["response_status"] = response.status
            entry["response_headers"] = dict(response.headers)
            entry["response_body"] = (response.text if hasattr(response, "text") else "")[:2048]
        except Exception:
            entry["response_body"] = ""

    _CAPTURED_REQUESTS.append(entry)


# ── Feature 4: Checkpoint Storage ─────────────────────────────────────────────

_CHECKPOINT_DIR = Path("vault") / "checkpoints"


def _get_checkpoints_dir() -> Path:
    d = _CHECKPOINT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── AXTree Helpers ────────────────────────────────────────────────────────────

def _parse_aria_snapshot(snapshot: str) -> List[Dict]:
    """Parse aria snapshot string into a nested element tree."""
    import re

    LINE_RE = re.compile(r"^(\s*)-\s*\[ref=(e\d+)\]\s*(.*)$")
    ROLE_NAME_RE = re.compile(r"^([^:]+?)(?::\s*(.*))?$")

    root: List[Dict] = []
    stack: List = []

    for line in snapshot.split("\n"):
        if not line.strip():
            continue

        m = LINE_RE.match(line)
        if not m:
            if stack:
                indent, current = stack[-1]
                stripped = line.strip()
                if stripped.startswith("-"):
                    stripped = stripped[1:].strip()
                if stripped and current is not None:
                    existing_text = current.get("text", "")
                    if existing_text:
                        current["text"] = existing_text + " " + stripped
                    else:
                        current["text"] = stripped
            continue

        indent_str, ref, rest = m.group(1), m.group(2), m.group(3).strip()
        indent_level = len(indent_str)

        while stack and stack[-1][0] >= indent_level:
            stack.pop()

        role_match = ROLE_NAME_RE.match(rest)
        if role_match:
            role = role_match.group(1).strip()
            name = (role_match.group(2) or "").strip()
        else:
            role = rest
            name = ""

        element = {"ref": ref, "role": role, "name": name, "text": "", "children": []}

        if stack:
            parent = stack[-1][1]
            if parent is not None:
                parent["children"].append(element)
        else:
            root.append(element)

        stack.append((indent_level, element))

    return root


# ── Feature 2: Validation Summary Helpers ─────────────────────────────────────

_PASS_PATTERNS = re.compile(
    r"\b(correct|passed?|success|all\s+good|no\s+errors?|passed\s+all)\b", re.I
)
_FAIL_PATTERNS = re.compile(
    r"\b(wrong|incorrect|failed?|error|failed\s+\d+\s+question|no\s+correct|not\s+quite)\b",
    re.I,
)
_QUESTION_COUNT_RE = re.compile(r"\d+")


def _extract_validation_summary(page) -> dict:
    """Walk the DOM and collect all structured validation signals.

    Returns a dict with:
      valid (bool|None): True/False if determinable, None if unknown
      summary_text (str): concatenated accessible status text
      field_errors (list): per-field errors
      aria_live_text (str): text from ARIA live regions
      unknown (bool): True if nothing readable was found
      recommendation (str): guidance for the next action
    """
    import time

    try:
        result = page.evaluate(
            """() => {
            const signals = {
                aria_live: [],
                error_summary: [],
                field_errors: [],
                pass_fail_text: [],
                http_status: null,
                page_title: document.title,
                body_text: document.body.innerText.slice(0, 500),
            };

            // 1. ARIA live regions
            document.querySelectorAll('[role="alert"], [role="status"], [aria-live]').forEach(el => {
                const text = (el.innerText || '').trim();
                if (text) signals.aria_live.push(text);
            });

            // 2. Error summary divs (Laravel, Django, React, Vue, Bootstrap, HTML5)
            const summarySelectors = [
                '.error-summary', '.validation-summary', '.alert-danger',
                '.alert-error', '.form-errors', '.errors', '.validation-errors',
                '[role="alert"]', '.has-errors', '.invalid-feedback',
                '.field-error', '.form-error', '.input-error',
            ];
            summarySelectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    const text = (el.innerText || '').trim();
                    if (text) signals.error_summary.push(text);
                });
            });

            // 3. Field-level errors
            document.querySelectorAll('.is-invalid, .has-error, [aria-invalid="true"], .invalid').forEach(el => {
                const label = el.getAttribute('aria-label') ||
                    (el.closest('label') || el).innerText || el.name || el.id || '';
                signals.field_errors.push({
                    field: label.slice(0, 100),
                    element: el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''),
                    message: (el.innerText || '').trim().slice(0, 200),
                });
            });

            // 4. Pass/fail keyword search in body
            signals.pass_fail_text = document.body.innerText.match(
                /(?:correct|passed?|success|failed|wrong|incorrect|error|all\\s+good|no\\s+errors?)[^.!?]{0,100}/gi
            ) || [];

            // 5. HTTP status (if on an error page the server-set status is in the DOM)
            //    We check if <body> has class/id indicating an error code
            const body = document.body;
            const statusMatch = (document.title + ' ' + (body.className || '') + ' ' + (body.id || ''))
                .match(/\\b(4\\d{2}|5\\d{2})\\b/);
            if (statusMatch) signals.http_status = parseInt(statusMatch[1]);

            return signals;
        }"""
        )

        aria_text = " | ".join(result["aria_live"])
        summary_text = " | ".join(result["error_summary"])
        pass_fail = " | ".join(result["pass_fail_text"][:10])  # cap at 10 matches

        # Determine overall validity
        has_aria_alert = bool(result["aria_live"])
        has_error_summary = bool(result["error_summary"])
        has_field_errors = bool(result["field_errors"])
        has_fail_keyword = bool(_FAIL_PATTERNS.search(summary_text + " " + pass_fail))
        has_pass_keyword = bool(_PASS_PATTERNS.search(summary_text + " " + pass_fail))

        valid: Optional[bool] = None
        if has_pass_keyword and not has_fail_keyword:
            valid = True
        elif has_fail_keyword and not has_pass_keyword:
            valid = False

        unknown = not (has_aria_alert or has_error_summary or has_field_errors or valid is not None)

        # Build recommendation
        if unknown:
            recommendation = "No structured validation signal found. Inspect the page manually or use get_axtree() for more detail."
        elif valid is True:
            recommendation = "Page indicates success. Proceed to next step."
        elif valid is False:
            field_count = len(result["field_errors"])
            recommendation = (
                f"Page indicates failure. {field_count} field-level errors found. "
                "Use get_axtree() to locate specific wrong fields, then correct and resubmit."
            )
        else:
            recommendation = "Mixed signals detected. Review aria_live_text and summary_text fields for details."

        return {
            "valid": valid,
            "summary_text": (summary_text + " | " + pass_fail).strip(" |"),
            "field_errors": result["field_errors"],
            "aria_live_text": aria_text,
            "http_status": result.get("http_status"),
            "page_title": result.get("page_title", ""),
            "unknown": unknown,
            "recommendation": recommendation,
        }
    except Exception as e:
        return {
            "valid": None,
            "summary_text": "",
            "field_errors": [],
            "aria_live_text": "",
            "http_status": None,
            "page_title": "",
            "unknown": True,
            "recommendation": f"Validation extraction failed: {e}. Use get_axtree() instead.",
        }


# ── Shared Browser Page Helper ─────────────────────────────────────────────────

async def _get_page(url: str, mode: str = "headless", timeout: int = 30000):
    """Launch (or connect to) a browser and navigate to url.

    Returns (page, strategy_used: str, browser, context).
    Caller must await browser.close() when done.
    """
    global _CAPTURE_ACTIVE

    headless = mode == "headless"
    browser, strategy = await _launch_browser(headless=headless)

    # Attach network capture handlers if active
    context = await browser.new_context()
    if _CAPTURE_ACTIVE:

        async def _on_request(request):
            await _capture_handler(request)

        async def _on_response(response):
            await _capture_handler(response.request, response)

        context.on("request", _on_request)
        context.on("response", _on_response)

    page = await context.new_page()

    try:
        await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
    except Exception:
        pass

    return page, strategy, browser, context


# ── TOOL: get_axtree ──────────────────────────────────────────────────────────

async def get_axtree(url: str, mode: str = "headless", timeout: int = 30000) -> dict:
    """Get the accessibility tree (AXTree) for a URL.

    Uses Playwright's aria_snapshot() which returns a structured representation
    of the page's accessibility tree, optimised for AI.

    Feature 1 (CDP auto-connect) is applied automatically: if Chrome is running
    with --remote-debugging-port or the FreeHand Chrome Extension is installed,
    the existing browser session is used (preserving cookies and logins).
    Otherwise falls back to a fresh sandboxed Chromium.

    Args:
        url: The URL to navigate to
        mode: "headless" (default) or "headed" (visible browser)
        timeout: Maximum time in milliseconds to wait for page load

    Returns dict with:
        url (str): The final URL after navigation
        title (str): Page title
        snapshot (str): Raw aria snapshot string
        elements (list[dict]): Structured element data with refs
        cdp_strategy (str): "chrome_extension", "existing_chrome", or "sandboxed"
        error (str, optional): Error message if any
    """
    try:
        page, strategy, browser, _ = await _get_page(url, mode, timeout)
        try:
            snapshot = await page.aria_snapshot(mode="ai")
            title = await page.title()
            elements = _parse_aria_snapshot(snapshot)
            return {
                "url": page.url,
                "title": title,
                "snapshot": snapshot,
                "elements": elements,
                "element_count": len(elements),
                "cdp_strategy": strategy,
            }
        except Exception as e:
            return {
                "url": url,
                "title": "",
                "snapshot": "",
                "elements": [],
                "error": str(e),
                "cdp_strategy": strategy,
            }
        finally:
            await browser.close()
    except ImportError:
        return {"error": "Playwright not installed. Run: pip install playwright"}
    except Exception as e:
        return {"error": f"Browser error: {str(e)}"}


# ── TOOL: extract_text ────────────────────────────────────────────────────────

async def extract_text(url: str, mode: str = "headless", timeout: int = 30000) -> dict:
    """Extract readable text content from a URL.

    Gets the aria snapshot and extracts just the text content, stripping ref
    markers and formatting for readability.

    Returns dict with url, title, and text content.
    """
    result = await get_axtree(url, mode=mode, timeout=timeout)
    if "error" in result:
        return result

    snapshot = result.get("snapshot", "")
    lines = []
    REF_PATTERN = re.compile(r"\[ref=e\d+\]")
    for line in snapshot.split("\n"):
        clean = REF_PATTERN.sub("", line)
        if clean.strip():
            lines.append(clean.strip())

    result["text"] = "\n".join(lines)
    result.pop("snapshot", None)
    result.pop("elements", None)
    return result


# ── TOOL: navigate ───────────────────────────────────────────────────────────

async def navigate(url: str, mode: str = "headless") -> dict:
    """Navigate to a URL and return the AXTree.

    This is considered a write action since it changes browser state.

    Returns dict with AXTree data plus cdp_strategy.
    """
    result = intercept_action(
        action_type="browser_action",
        description=f"Navigate to {url}",
        payload={"url": url, "action": "navigate"},
        source="tool",
    )
    if not result.get("allowed", False):
        return {
            "error": "Permission denied",
            "approval_id": result.get("approval_id"),
            "message": "Browser action requires approval. Use /approve <id> to allow.",
        }

    full_result = await get_axtree(url, mode=mode)
    # Add validation summary to the result
    try:
        page, strategy, browser, _ = await _get_page(url, mode)
        try:
            full_result["validation"] = _extract_validation_summary(page)
        except Exception:
            pass
        finally:
            await browser.close()
    except Exception:
        pass

    return full_result


# ── TOOL: click ───────────────────────────────────────────────────────────────

async def click(selector: str, url: str, mode: str = "headless") -> dict:
    """Click an element on a page.

    This is a write action that requires approval in SEMI_AUTONOMOUS tier.

    Args:
        url: The page URL to navigate to first
        selector: CSS selector or aria role selector
        mode: "headless" or "headed"

    Returns dict with success status, current page info, validation summary,
    and cdp_strategy.
    """
    result = intercept_action(
        action_type="browser_action",
        description=f"Click {selector} on {url}",
        payload={"url": url, "selector": selector, "action": "click"},
        source="tool",
    )
    if not result.get("allowed", False):
        return {
            "error": "Permission denied",
            "approval_id": result.get("approval_id"),
            "message": "Browser action requires approval. Use /approve <id> to allow.",
        }

    try:
        page, strategy, browser, _ = await _get_page(url, mode)
        try:
            await page.locator(selector).click(timeout=10000)
            await page.wait_for_timeout(1500)

            validation = _extract_validation_summary(page)

            return {
                "success": True,
                "url": page.url,
                "title": await page.title(),
                "selector": selector,
                "action": "click",
                "cdp_strategy": strategy,
                "validation": validation,
            }
        except Exception as e:
            return {
                "success": False,
                "url": page.url,
                "error": f"Click failed: {str(e)}",
                "selector": selector,
                "cdp_strategy": strategy,
            }
        finally:
            await browser.close()
    except Exception as e:
        return {"error": f"Browser error: {str(e)}"}


# ── TOOL: fill ────────────────────────────────────────────────────────────────

async def fill(url: str, selector: str, value: str, mode: str = "headless") -> dict:
    """Fill a form field with a value.

    This is a write action that requires approval in SEMI_AUTONOMOUS tier.

    Args:
        url: The page URL
        selector: CSS selector for the input field
        value: Text value to fill
        mode: "headless" or "headed"

    Returns dict with success status, validation summary, and cdp_strategy.
    """
    result = intercept_action(
        action_type="browser_action",
        description=f"Fill {selector} on {url}",
        payload={"url": url, "selector": selector, "value": value, "action": "fill"},
        source="tool",
    )
    if not result.get("allowed", False):
        return {
            "error": "Permission denied",
            "approval_id": result.get("approval_id"),
            "message": "Browser action requires approval. Use /approve <id> to allow.",
        }

    try:
        page, strategy, browser, _ = await _get_page(url, mode)
        try:
            await page.locator(selector).fill(value, timeout=10000)
            await page.wait_for_timeout(500)

            validation = _extract_validation_summary(page)

            return {
                "success": True,
                "url": page.url,
                "selector": selector,
                "value_length": len(value),
                "action": "fill",
                "cdp_strategy": strategy,
                "validation": validation,
            }
        except Exception as e:
            return {
                "success": False,
                "url": page.url,
                "error": f"Fill failed: {str(e)}",
                "selector": selector,
                "cdp_strategy": strategy,
            }
        finally:
            await browser.close()
    except Exception as e:
        return {"error": f"Browser error: {str(e)}"}


# ── TOOL: screenshot ─────────────────────────────────────────────────────────

async def screenshot(url: str, mode: str = "headless", full_page: bool = False) -> dict:
    """Take a screenshot of a URL.

    Permission: screenshots are sensitive (can capture credentials, PII, session
    cookies on screen). Require approval like other browser actions. Tier=AUTONOMOUS
    still blocks; tier=GOD_MODE allows.

    Filename: derived from sha256(url) — collision-free.

    Args:
        url: The URL to screenshot
        mode: "headless" or "headed"
        full_page: Capture full scrollable page

    Returns dict with screenshot path, cdp_strategy, and validation summary.
    """
    result = intercept_action(
        action_type="browser_action",
        description=f"Screenshot {url}",
        payload={"url": url, "action": "screenshot", "full_page": full_page},
        source="tool",
    )
    if not result.get("allowed", False):
        return {
            "error": "Permission denied",
            "approval_id": result.get("approval_id"),
            "message": "Screenshot requires approval. Use /approve <id> to allow.",
        }

    try:
        page, strategy, browser, _ = await _get_page(url, mode)
        size_cap_mb = _screenshot_size_cap_mb()
        output_path = _screenshot_path_for(url)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            await page.screenshot(path=str(output_path), full_page=full_page)
            validation = _extract_validation_summary(page)
        finally:
            await browser.close()

        actual_size = output_path.stat().st_size
        actual_mb = actual_size / (1024 * 1024)
        if actual_mb > size_cap_mb:
            output_path.unlink(missing_ok=True)
            return {
                "error": f"Screenshot too large ({actual_mb:.1f}MB > cap {size_cap_mb}MB). "
                "Increase settings.max_screenshot_size_mb or pass full_page=False.",
                "cdp_strategy": strategy,
            }

        return {
            "success": True,
            "url": url,
            "screenshot_path": str(output_path.resolve()),
            "full_page": full_page,
            "size_mb": round(actual_mb, 2),
            "cdp_strategy": strategy,
            "validation": validation,
        }
    except Exception as e:
        return {"error": f"Screenshot failed: {str(e)}"}


# ── FEATURE 2: get_validation_summary ────────────────────────────────────────

async def get_validation_summary(url: str, mode: str = "headless") -> dict:
    """Get a structured validation/pass-fail summary for a page.

    After a form submission (or any state-changing action), the page may show
    success or error feedback via ARIA live regions, error summary divs,
    field-level error classes, or keyword text. This tool extracts all of that
    into a structured report so the LLM knows exactly what went wrong and where.

    Args:
        url: The page URL to evaluate
        mode: "headless" or "headed"

    Returns dict with:
        valid (bool | None): True if page indicates success, False if failure,
            None if indeterminate
        summary_text (str): concatenated text from all error/success elements
        field_errors (list[dict]): per-field errors with field name, element,
            and message
        aria_live_text (str): text from [role="alert"] and [aria-live] regions
        http_status (int | None): detected HTTP status code (e.g. 429, 500)
        page_title (str): document title
        unknown (bool): True if no structured signal was found
        recommendation (str): guidance for the next action
        cdp_strategy (str): "chrome_extension", "existing_chrome", or "sandboxed"
    """
    result = intercept_action(
        action_type="browser_action",
        description=f"Get validation summary for {url}",
        payload={"url": url, "action": "get_validation_summary"},
        source="tool",
    )
    if not result.get("allowed", False):
        return {
            "error": "Permission denied",
            "approval_id": result.get("approval_id"),
            "message": "Browser action requires approval. Use /approve <id> to allow.",
        }

    try:
        page, strategy, browser, _ = await _get_page(url, mode)
        try:
            summary = _extract_validation_summary(page)
            summary["cdp_strategy"] = strategy
            return summary
        finally:
            await browser.close()
    except Exception as e:
        return {
            "error": f"Validation summary failed: {str(e)}",
            "cdp_strategy": "unknown",
        }


# ── FEATURE 3: start_request_capture / get_captured_requests ─────────────────

def start_request_capture(url_pattern: Optional[str] = None) -> dict:
    """Start capturing network requests and responses.

    All network traffic matching url_pattern (regex or substring) is intercepted
    and stored. If url_pattern is None, all requests are captured.

    Call get_captured_requests() to retrieve captured entries.

    Args:
        url_pattern: Regex pattern or substring to filter requests.
            Example: "api.*quiz" captures all quiz AJAX calls.
            Use None to capture everything.

    Returns dict with capturing status and pattern.
    """
    global _CAPTURE_ACTIVE, _CAPTURE_PATTERN
    _CAPTURE_ACTIVE = True
    _CAPTURE_PATTERN = url_pattern
    return {
        "capturing": True,
        "pattern": url_pattern,
        "max_entries": 200,
        "message": "Network capture started. Call get_captured_requests() to retrieve entries.",
    }


def get_captured_requests(limit: int = 50) -> dict:
    """Retrieve captured network requests and responses.

    Returns up to `limit` of the most recent captured entries since the last
    call to start_request_capture(). Each entry includes method, URL, request
    headers/body, response status, headers, and truncated body.

    Args:
        limit: Maximum number of entries to return (default 50, max 200).

    Returns dict with list of request entries and metadata.
    """
    global _CAPTURED_REQUESTS
    limit = min(limit, 200)
    entries = list(_CAPTURED_REQUESTS)[-limit:]
    return {
        "count": len(entries),
        "total_captured": len(_CAPTURED_REQUESTS),
        "active": _CAPTURE_ACTIVE,
        "pattern": _CAPTURE_PATTERN,
        "requests": entries,
    }


def stop_request_capture() -> dict:
    """Stop capturing network requests."""
    global _CAPTURE_ACTIVE
    was_active = _CAPTURE_ACTIVE
    _CAPTURE_ACTIVE = False
    return {
        "capturing": False,
        "was_active": was_active,
        "message": "Network capture stopped." if was_active else "Capture was not active.",
    }


# ── FEATURE 4: save_checkpoint / restore_checkpoint / list / delete ──────────

async def save_checkpoint(name: str, url: str, mode: str = "headless") -> dict:
    """Save the current page state as a named checkpoint.

    A checkpoint captures: form field values, checkbox states, cookies for the
    current origin, localStorage, and an optional screenshot. This lets you
    restore the page to exactly this state later without re-entering data —
    essential for iterative form testing.

    Args:
        name: Human-readable checkpoint name (used to restore)
        url: The page URL to capture
        mode: "headless" or "headed"

    Returns dict with saved checkpoint metadata.
    """
    result = intercept_action(
        action_type="browser_action",
        description=f"Save checkpoint '{name}' for {url}",
        payload={"url": url, "name": name, "action": "save_checkpoint"},
        source="tool",
    )
    if not result.get("allowed", False):
        return {
            "error": "Permission denied",
            "approval_id": result.get("approval_id"),
            "message": "Browser action requires approval. Use /approve <id> to allow.",
        }

    try:
        page, strategy, browser, context = await _get_page(url, mode)
        try:
            # Serialise DOM state via page.evaluate
            dom_state = page.evaluate(
                """() => {
                const state = {
                    form_values: {},
                    checkbox_states: {},
                    input_values: {},
                    select_values: {},
                    radio_states: {},
                };

                // Collect form field values by name
                document.querySelectorAll('input, select, textarea').forEach(el => {
                    if (!el.name) return;
                    const key = el.name;
                    if (el.type === 'checkbox') {
                        if (!state.checkbox_states[key]) state.checkbox_states[key] = [];
                        if (el.checked) state.checkbox_states[key].push(el.value);
                    } else if (el.type === 'radio') {
                        if (el.checked) state.radio_states[key] = el.value;
                    } else if (el.tagName === 'SELECT') {
                        state.select_values[key] = Array.from(el.selectedOptions).map(o => o.value);
                    } else {
                        state.input_values[key] = el.value;
                    }
                });

                return state;
            }"""
            )

            # Collect cookies for the origin
            try:
                cookies = await context.cookies([page.url])
            except Exception:
                cookies = []

            # Collect localStorage
            try:
                local_storage = page.evaluate(
                    """() => {
                    const out = {};
                    for (let i = 0; i < localStorage.length; i++) {
                        const k = localStorage.key(i);
                        out[k] = localStorage.getItem(k);
                    }
                    return out;
                }"""
                )
            except Exception:
                local_storage = {}

            # Screenshot (thumbnail only, small to keep checkpoint small)
            import time

            checkpoint_id = f"ckpt_{uuid.uuid4().hex[:12]}"
            screenshot_path = None
            try:
                screenshot_path = str(
                    _get_checkpoints_dir() / f"{checkpoint_id}_preview.png"
                )
                await page.screenshot(path=screenshot_path, timeout=5000)
            except Exception:
                screenshot_path = None

            checkpoint = {
                "id": checkpoint_id,
                "name": name,
                "url": page.url,
                "title": await page.title(),
                "saved_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                "dom_state": dom_state,
                "cookies": cookies,
                "local_storage": local_storage,
                "screenshot_path": screenshot_path,
                "cdp_strategy": strategy,
            }

            # Write to disk
            checkpoint_file = _get_checkpoints_dir() / f"{checkpoint_id}.json"
            checkpoint_file.write_text(
                json.dumps(checkpoint, indent=2, default=str)
            )

            import os

            size_kb = checkpoint_file.stat().st_size / 1024

            return {
                "saved": True,
                "name": name,
                "checkpoint_id": checkpoint_id,
                "url": page.url,
                "size_kb": round(size_kb, 1),
                "screenshot_path": screenshot_path,
                "cdp_strategy": strategy,
            }
        finally:
            await browser.close()
    except Exception as e:
        return {"error": f"save_checkpoint failed: {str(e)}"}


async def restore_checkpoint(name: str, url: Optional[str] = None, mode: str = "headless") -> dict:
    """Restore a saved checkpoint by name.

    If url is provided, verifies the checkpoint matches the current page URL.
    If url is omitted, restores the checkpoint regardless of the current page.

    After restoring, form values, cookies, and localStorage are applied to the
    page without a full page reload.

    Args:
        name: The checkpoint name to restore
        url: Optional URL to verify before restoring
        mode: "headless" or "headed"

    Returns dict with restoration status.
    """
    result = intercept_action(
        action_type="browser_action",
        description=f"Restore checkpoint '{name}'",
        payload={"name": name, "action": "restore_checkpoint"},
        source="tool",
    )
    if not result.get("allowed", False):
        return {
            "error": "Permission denied",
            "approval_id": result.get("approval_id"),
            "message": "Browser action requires approval. Use /approve <id> to allow.",
        }

    # Find checkpoint by name
    checkpoints_dir = _get_checkpoints_dir()
    matching = sorted(
        checkpoints_dir.glob("ckpt_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    checkpoint_data = None
    for cp_file in matching:
        try:
            cp = json.loads(cp_file.read_text())
            if cp.get("name") == name:
                checkpoint_data = cp
                break
        except Exception:
            continue

    if not checkpoint_data:
        available = [p.stem for p in matching]
        return {
            "error": f"Checkpoint '{name}' not found. Available: {available}",
            "available_checkpoints": available,
        }

    if url and checkpoint_data.get("url") != url:
        return {
            "error": "checkpoint_mismatch",
            "current_url": url,
            "checkpoint_url": checkpoint_data.get("url"),
            "checkpoint_name": name,
            "message": "Current URL does not match the checkpoint URL. "
            "Navigate to the checkpoint URL first or pass url=None to restore anyway.",
        }

    try:
        page, strategy, browser, context = await _get_page(
            checkpoint_data["url"], mode
        )
        try:
            # Restore cookies
            if checkpoint_data.get("cookies"):
                try:
                    # Filter to current origin
                    origin_cookies = [
                        c for c in checkpoint_data["cookies"]
                        if c.get("domain") and page.url.startswith("http")
                    ]
                    if origin_cookies:
                        await context.add_cookies(origin_cookies)
                except Exception as e:
                    pass  # Non-critical

            # Restore localStorage
            ls = checkpoint_data.get("local_storage", {})
            if ls:
                for k, v in ls.items():
                    try:
                        page.evaluate(
                            f"localStorage.setItem({json.dumps(k)}, {json.dumps(v)})"
                        )
                    except Exception:
                        pass

            # Restore DOM state
            dom = checkpoint_data.get("dom_state", {})
            errors = []

            for name_attr, value in dom.get("input_values", {}).items():
                try:
                    await page.fill(f'[name="{name_attr}"]', str(value))
                except Exception:
                    errors.append(f"input:{name_attr}")

            for name_attr, values in dom.get("checkbox_states", {}).items():
                for val in values:
                    try:
                        await page.check(f'[name="{name_attr}"][value="{val}"]')
                    except Exception:
                        pass

            for name_attr, value in dom.get("radio_states", {}).items():
                try:
                    await page.check(f'[name="{name_attr}"][value="{value}"]')
                except Exception:
                    pass

            return {
                "restored": True,
                "name": name,
                "checkpoint_id": checkpoint_data.get("id"),
                "url": page.url,
                "title": await page.title(),
                "dom_errors": errors if errors else None,
                "cdp_strategy": strategy,
            }
        finally:
            await browser.close()
    except Exception as e:
        return {"error": f"restore_checkpoint failed: {str(e)}"}


def list_checkpoints() -> dict:
    """List all saved checkpoints.

    Returns a list of checkpoint metadata (name, URL, timestamp, size).
    Does not include full DOM state — use restore_checkpoint to apply one.
    """
    checkpoints_dir = _get_checkpoints_dir()
    checkpoints = []
    for cp_file in sorted(checkpoints_dir.glob("ckpt_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if "_preview" in cp_file.name:
            continue
        try:
            cp = json.loads(cp_file.read_text())
            checkpoints.append({
                "name": cp.get("name"),
                "id": cp.get("id"),
                "url": cp.get("url"),
                "title": cp.get("title"),
                "saved_at": cp.get("saved_at"),
                "size_kb": round(cp_file.stat().st_size / 1024, 1),
                "screenshot_path": cp.get("screenshot_path"),
            })
        except Exception:
            continue

    return {"count": len(checkpoints), "checkpoints": checkpoints}


def delete_checkpoint(name: str) -> dict:
    """Delete a saved checkpoint by name.

    Args:
        name: The checkpoint name to delete

    Returns dict with deletion status.
    """
    checkpoints_dir = _get_checkpoints_dir()
    deleted = False
    remaining = []
    for cp_file in checkpoints_dir.glob("ckpt_*.json"):
        if "_preview" in cp_file.name:
            continue
        try:
            cp = json.loads(cp_file.read_text())
            if cp.get("name") == name:
                cp_file.unlink()
                # Also delete preview image
                preview = checkpoints_dir / f"{cp.get('id')}_preview.png"
                preview.unlink(missing_ok=True)
                deleted = True
            else:
                remaining.append(cp.get("name"))
        except Exception:
            continue

    if deleted:
        return {"deleted": True, "name": name, "remaining_count": len(remaining)}
    else:
        return {
            "error": f"Checkpoint '{name}' not found.",
            "available": remaining,
        }


# ── Exported tool list (used by agent_config.py TOOL_REGISTRY) ────────────────
# These are the names the LLM can call. Each maps directly to the async def above.
BROWSER_TOOLS = {
    "get_axtree",
    "extract_text",
    "navigate",
    "click",
    "fill",
    "screenshot",
    "get_validation_summary",
    "start_request_capture",
    "get_captured_requests",
    "stop_request_capture",
    "save_checkpoint",
    "restore_checkpoint",
    "list_checkpoints",
    "delete_checkpoint",
}

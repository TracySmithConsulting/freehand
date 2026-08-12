"""Browser automation tool with AXTree (accessibility tree) extraction.

Uses Playwright to interact with web pages. Supports both headless mode
(for automated scraping) and headed mode (for interactive use where the
user can see and control the browser).

Security: Read operations (get_axtree, extract_text) are allowed in all
tiers. Write operations (click, fill, navigate) trigger intercept_action()
approval in SEMI_AUTONOMOUS tier and are blocked in AUTONOMOUS tier.
"""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.security import intercept_action, PROJECT_ROOT

# R9 fix: cap screenshot file size to prevent DoS via huge pages.
# Default 20MB covers typical full-page screenshots; configurable via
# settings["max_screenshot_size_mb"] (read at call time, not at import,
# so the operator can tune without restarting).
DEFAULT_SCREENSHOT_SIZE_CAP_MB = 20

# R1 fix: hash the URL for screenshot filenames instead of using len(url).
# Two URLs of equal length used to collide and silently overwrite each
# other's screenshots. SHA256 prefix is 16 hex chars — collision-free
# in practice, no PII in the filename.
def _screenshot_path_for(url: str) -> Path:
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return Path("vault") / "screenshots" / f"screenshot_{url_hash}.png"


def _screenshot_size_cap_mb() -> int:
    """Read the operator-configurable screenshot size cap from settings.json."""
    try:
        from core.agent_config import _load_settings
        return int(_load_settings().get("max_screenshot_size_mb", DEFAULT_SCREENSHOT_SIZE_CAP_MB))
    except Exception:
        return DEFAULT_SCREENSHOT_SIZE_CAP_MB


async def get_axtree(url: str, mode: str = "headless", timeout: int = 30000) -> dict:
    """Get the accessibility tree (AXTree) for a URL.

    Uses Playwright's aria_snapshot() which returns a structured
    representation of the page's accessibility tree, optimized for AI.

    Args:
        url: The URL to navigate to
        mode: "headless" (default) or "headed" (visible browser)
        timeout: Maximum time in milliseconds to wait for page load

    Returns dict with:
        url (str): The final URL after navigation
        title (str): Page title
        snapshot (str): Raw aria snapshot string
        elements (list[dict]): Structured element data with refs
        error (str, optional): Error message if any
    """
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser_opts = {"headless": mode == "headless"}
            browser = await p.chromium.launch(**browser_opts)
            context = await browser.new_context()
            page = await context.new_page()

            try:
                await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                # Wait a bit for dynamic content
                await page.wait_for_timeout(1000)

                # Get aria snapshot (AXTree)
                snapshot = await page.aria_snapshot(mode="ai")

                # Extract title
                title = await page.title()

                # Parse snapshot into structured format
                elements = _parse_aria_snapshot(snapshot)

                result = {
                    "url": page.url,
                    "title": title,
                    "snapshot": snapshot,
                    "elements": elements,
                    "element_count": len(elements),
                }
            except Exception as e:
                result = {
                    "url": url,
                    "title": "",
                    "snapshot": "",
                    "elements": [],
                    "error": str(e),
                }
            finally:
                await browser.close()

            return result
    except ImportError:
        return {"error": "Playwright not installed. Run: pip install playwright"}
    except Exception as e:
        return {"error": f"Browser error: {str(e)}"}


async def extract_text(url: str, mode: str = "headless", timeout: int = 30000) -> dict:
    """Extract readable text content from a URL.

    Gets the aria snapshot and extracts just the text content,
    stripping ref markers and formatting for readability.

    Returns dict with url, title, and text content.
    """
    result = await get_axtree(url, mode=mode, timeout=timeout)
    if "error" in result:
        return result

    # Extract plain text from snapshot
    snapshot = result.get("snapshot", "")
    lines = []
    # R3 fix: precise ref-marker stripping. Old code did
    #     line.replace("[ref=e", "").replace("]", "")
    # which corrupted text containing "]" anywhere (e.g.
    # "see [ref=e10] in our docs" became "see e10 in our docs").
    # Regex matches ONLY the actual ref marker pattern.
    import re as _re
    REF_PATTERN = _re.compile(r"\[ref=e\d+\]")
    for line in snapshot.split("\n"):
        clean = REF_PATTERN.sub("", line)
        if clean.strip():
            lines.append(clean.strip())

    result["text"] = "\n".join(lines)
    result.pop("snapshot", None)
    result.pop("elements", None)
    return result


async def click(url: str, selector: str, mode: str = "headless") -> dict:
    """Click an element on a page.

    This is a write action that requires approval in SEMI_AUTONOMOUS tier.

    Args:
        url: The page URL
        selector: CSS selector or aria role selector
        mode: "headless" or "headed"

    Returns dict with success status and current page info.
    """
    # Check permissions
    result = intercept_action(
        action_type="browser_action",
        description=f"Click element on {url}: {selector}",
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
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=mode == "headless")
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(url, timeout=30000)
            await page.wait_for_timeout(500)

            # Try to click
            try:
                await page.locator(selector).click()
                await page.wait_for_timeout(1000)

                # Get updated state
                new_url = page.url
                title = await page.title()

                result = {
                    "success": True,
                    "url": new_url,
                    "title": title,
                    "selector": selector,
                    "action": "click",
                }
            except Exception as e:
                result = {
                    "success": False,
                    "url": page.url,
                    "error": f"Click failed: {str(e)}",
                    "selector": selector,
                }
            finally:
                await browser.close()

            return result
    except Exception as e:
        return {"error": f"Browser error: {str(e)}"}


async def fill(url: str, selector: str, value: str, mode: str = "headless") -> dict:
    """Fill a form field with a value.

    This is a write action that requires approval in SEMI_AUTONOMOUS tier.

    Args:
        url: The page URL
        selector: CSS selector for the input field
        value: Text value to fill
        mode: "headless" or "headed"

    Returns dict with success status.
    """
    result = intercept_action(
        action_type="browser_action",
        description=f"Fill field on {url}: {selector} = {value[:50]}",
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
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=mode == "headless")
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(url, timeout=30000)
            await page.wait_for_timeout(500)

            try:
                await page.locator(selector).fill(value)
                await page.wait_for_timeout(500)

                return {
                    "success": True,
                    "url": page.url,
                    "selector": selector,
                    "value_length": len(value),
                    "action": "fill",
                }
            except Exception as e:
                return {
                    "success": False,
                    "url": page.url,
                    "error": f"Fill failed: {str(e)}",
                    "selector": selector,
                }
            finally:
                await browser.close()
    except Exception as e:
        return {"error": f"Browser error: {str(e)}"}


async def navigate(url: str, mode: str = "headless") -> dict:
    """Navigate to a URL and return the AXTree.

    This is considered a write action since it changes browser state.

    Args:
        url: The URL to navigate to
        mode: "headless" or "headed"

    Returns dict with AXTree data.
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

    return await get_axtree(url, mode=mode)


async def screenshot(url: str, mode: str = "headless", full_page: bool = False) -> dict:
    """Take a screenshot of a page.

    Permission: R9 fix — screenshots require approval in SEMI_AUTONOMOUS
    tier. Capturing a logged-in banking page can leak credentials visible
    on screen; the tier system catches this. AUTONOMOUS blocks, GOD_MODE
    allows.

    Filename: R1 fix — derived from sha256(url) instead of len(url) so
    collisions can't overwrite earlier screenshots.

    Args:
        url: The URL to screenshot
        mode: "headless" or "headed"
        full_page: Capture full scrollable page

    Returns dict with screenshot path.
    """
    # R9 fix: screenshots are sensitive (can capture credentials, PII,
    # session cookies visible on screen). Require approval like other
    # browser actions. Tier=AUTONOMOUS still blocks; tier=GOD_MODE allows.
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
        from playwright.async_api import async_playwright

        size_cap_mb = _screenshot_size_cap_mb()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=mode == "headless")
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(url, timeout=30000)
            await page.wait_for_timeout(1000)

            # R1 fix: hash-based filename avoids length-collision overwrites.
            output_path = _screenshot_path_for(url)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            await page.screenshot(path=str(output_path), full_page=full_page)
            await browser.close()

            # R9 fix: enforce size cap on the saved file. Operators can
            # configure via settings["max_screenshot_size_mb"].
            actual_size = output_path.stat().st_size
            actual_mb = actual_size / (1024 * 1024)
            if actual_mb > size_cap_mb:
                output_path.unlink(missing_ok=True)
                return {
                    "error": f"Screenshot too large ({actual_mb:.1f}MB > cap {size_cap_mb}MB). Increase settings.max_screenshot_size_mb or pass full_page=False.",
                }

            return {
                "success": True,
                "url": url,
                "screenshot_path": str(output_path.resolve()),
                "full_page": full_page,
                "size_mb": round(actual_mb, 2),
            }
    except Exception as e:
        return {"error": f"Screenshot failed: {str(e)}"}


def _parse_aria_snapshot(snapshot: str) -> List[Dict]:
    """Parse aria snapshot string into a nested element tree.

    R6 fix: the previous implementation claimed to nest elements via a
    stack but always produced flat siblings — children were always [].
    This version uses leading-dash count to determine depth, matching
    Playwright's aria snapshot output (where `-` indent = hierarchy).

    Args:
        snapshot: Raw aria snapshot string from page.aria_snapshot()

    Returns list of dicts (top-level elements) with:
        ref (str), role (str), name (str), text (str),
        children (list[dict])

    Each child has the same shape — recursive structure.
    """
    import re
    LINE_RE = re.compile(r'^(\s*)-\s*\[ref=(e\d+)\]\s*(.*)$')
    # Pattern: "- [ref=e10] role: name - additional text"
    # or: "- [ref=e10] role: name" (no trailing text)
    ROLE_NAME_RE = re.compile(r'^([^:]+?)(?::\s*(.*))?$')

    root: List[Dict] = []
    # Stack: list of (indent_level, element_dict) representing the current path
    stack: List = []  # of (indent_level, parent_element_or_None)

    for line in snapshot.split("\n"):
        if not line.strip():
            continue

        m = LINE_RE.match(line)
        if not m:
            # Text-only indented child of the most recent element
            if stack:
                indent, current = stack[-1]
                stripped = line.strip()
                if stripped.startswith("-"):
                    stripped = stripped[1:].strip()
                if stripped and current is not None:
                    # Append to current element's text accumulator
                    existing_text = current.get("text", "")
                    if existing_text:
                        current["text"] = existing_text + " " + stripped
                    else:
                        current["text"] = stripped
            continue

        indent_str, ref, rest = m.group(1), m.group(2), m.group(3).strip()
        indent_level = len(indent_str)

        # Pop stack until we find the parent (any element with
        # strictly less indentation than this line).
        while stack and stack[-1][0] >= indent_level:
            stack.pop()

        # Parse role and name from rest.
        # Common shapes:
        #   "button: Submit form"
        #   "navigation"
        #   "text: Hello world"
        role_match = ROLE_NAME_RE.match(rest)
        if role_match:
            role = role_match.group(1).strip()
            name = (role_match.group(2) or "").strip()
        else:
            role = rest
            name = ""

        element = {
            "ref": ref,
            "role": role,
            "name": name,
            "text": "",
            "children": [],
        }

        # Attach to parent or root.
        if stack:
            parent = stack[-1][1]
            if parent is not None:
                parent["children"].append(element)
        else:
            root.append(element)

        # Push this element as the current parent at this indent.
        stack.append((indent_level, element))

    return root

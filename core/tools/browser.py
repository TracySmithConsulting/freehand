"""Browser automation tool with AXTree (accessibility tree) extraction.

Uses Playwright to interact with web pages. Supports both headless mode
(for automated scraping) and headed mode (for interactive use where the
user can see and control the browser).

Security: Read operations (get_axtree, extract_text) are allowed in all
tiers. Write operations (click, fill, navigate) trigger intercept_action()
approval in SEMI_AUTONOMOUS tier and are blocked in AUTONOMOUS tier.
"""

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.security import intercept_action


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
    for line in snapshot.split("\n"):
        # Remove ref markers like [ref=e2]
        clean = line.replace("[ref=e", "").replace("]", "")
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

    Read-only operation, allowed in all tiers.

    Args:
        url: The URL to screenshot
        mode: "headless" or "headed"
        full_page: Capture full scrollable page

    Returns dict with screenshot path.
    """
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=mode == "headless")
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(url, timeout=30000)
            await page.wait_for_timeout(1000)

            # Save screenshot
            output_path = Path("vault") / "screenshots" / f"screenshot_{len(url)}.png"
            output_path.parent.mkdir(parents=True, exist_ok=True)

            await page.screenshot(path=str(output_path), full_page=full_page)
            await browser.close()

            return {
                "success": True,
                "url": url,
                "screenshot_path": str(output_path.resolve()),
                "full_page": full_page,
            }
    except Exception as e:
        return {"error": f"Screenshot failed: {str(e)}"}


def _parse_aria_snapshot(snapshot: str) -> List[Dict]:
    """Parse aria snapshot string into structured element list.

    Args:
        snapshot: Raw aria snapshot string from page.aria_snapshot()

    Returns list of dicts with ref, role, name, children.
    """
    elements = []
    lines = snapshot.split("\n")
    stack = []

    for line in lines:
        if not line.strip():
            continue

        # Parse line: [ref=ex] role: name - text
        import re
        ref_match = re.search(r'\[ref=e(\d+)\]', line)
        role_match = re.search(r'role:\s*([^\s:-]+)', line)
        name_match = re.search(r'name:\s*(.*?)(?:\s*-\s*)?$', line)

        if ref_match:
            ref = ref_match.group(1)
            role = role_match.group(1) if role_match else ""
            name = name_match.group(1).strip() if name_match else ""

            # Extract text after the role/name part
            text_match = re.search(r'-\s*(.*)', line)
            text = text_match.group(1).strip() if text_match else ""

            element = {
                "ref": f"e{ref}",
                "role": role,
                "name": name,
                "text": text,
                "children": [],
            }
            elements.append(element)
            stack.append(element)
        elif line.strip().startswith("-") and stack:
            # Text content for current element
            if stack:
                stack[-1]["text"] = line.strip()[1:].strip()

    return elements

"""The console's layout (Nov 2026 visual review): one page at a time, a
menu button on phones, details in the normal font, and plain words.
Read straight from the shipped page, so a later change can't quietly
undo them."""
import re
from html.parser import HTMLParser
from pathlib import Path

PAGE = (Path(__file__).parent.parent / "freehand" / "static" / "index.html").read_text(encoding="utf-8")


class Outline(HTMLParser):
    """Each element's id, its tag, and the ids of the elements around it."""

    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack, self.elements, self.nav_links, self.h2_in = [], {}, [], set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id"):
            self.elements[a["id"]] = {"tag": tag, "attrs": a, "inside": [s for s in self.stack if s]}
        if tag == "a" and "nav-list" in self.stack and a.get("href", "").startswith("#"):
            self.nav_links.append(a["href"][1:])
        if tag == "h2":
            self.h2_in.update(s for s in self.stack if s)
        if tag not in self.VOID:
            self.stack.append(a.get("id", ""))

    def handle_endtag(self, tag):
        if tag not in self.VOID and self.stack:
            self.stack.pop()


def outline():
    o = Outline()
    o.feed(PAGE)
    return o


def pages(o):
    return {i for i, e in o.elements.items() if "data-page" in e["attrs"]}


def test_every_menu_item_opens_a_page():
    o = outline()
    assert o.nav_links, "the menu has items"
    for target in o.nav_links:
        e = o.elements[target]
        assert target in pages(o) or set(e["inside"]) & pages(o), f"#{target} isn't a page or inside one"


def test_every_page_has_a_heading_for_its_title_and_focus():
    o = outline()
    assert {"command", "tasks", "connections", "settings", "computer-help"} <= pages(o)
    for page in pages(o):
        assert page in o.h2_in, f"#{page} has no h2"


def test_settings_is_split_into_short_pages():
    o = outline()
    assert {"settings", "ai-model", "gateways"} <= pages(o)
    for part in ("ai-model", "gateways", "llm-setup", "gateway-save-btn"):
        assert "settings" not in o.elements[part]["inside"], f"#{part} is still inside Settings"
    assert "settings" in o.elements["api-key"]["inside"] and "settings" in o.elements["tier-form"]["inside"]


def test_the_setup_signing_in_and_approvals_stay_above_every_page():
    o = outline()
    for always in ("welcome", "sign-in", "approvals-section"):
        assert always not in pages(o)


def test_phones_get_a_menu_button():
    o = outline()
    button = o.elements["nav-toggle"]["attrs"]
    assert button.get("aria-controls") == "nav-list" and button.get("aria-expanded") == "false"
    assert re.search(r"@media \(max-width: 600px\)\s*\{\s*#nav-toggle \{ display: flex", PAGE)


def test_details_are_in_the_normal_font():
    tidy = PAGE[PAGE.index("The look, tidied"):]
    assert re.search(r"\.task-meta, \.connection-meta, \.approval-item small \{\s*font-family: inherit", tidy)


def test_delete_buttons_are_outlined_not_filled():
    tidy = PAGE[PAGE.index("The look, tidied"):]
    assert re.search(r"button\.danger \{\s*background: var\(--surface\);", tidy)


def test_plain_words():
    for gone in ("Agent Prompt", "Send Command", "Permission Tier", "Sweep Now", "Save Scribble",
                 "API Keys &amp; OmniRoute", "API Keys & OmniRoute", "Tighter restrictions"):
        assert gone not in PAGE, gone
    assert "shown === 'default' ? 'Main account'" in PAGE
    assert "niceDate(conn.connected_at)" in PAGE

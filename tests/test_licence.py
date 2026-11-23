"""FreeHand is under the GNU AGPL-3.0, and says so everywhere it names its licence.

The AGPL asks anyone who runs a changed FreeHand for other people over a
network to offer them the code, so the page itself links to it.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(name):
    return (ROOT / name).read_text(encoding="utf-8")


def test_licence_file_is_the_agpl():
    text = read("LICENSE")
    assert text.lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE")
    assert "Version 3, 19 November 2007" in text
    assert "13. Remote Network Interaction" in text


def test_package_metadata_says_agpl():
    text = read("pyproject.toml")
    assert 'license = { text = "AGPL-3.0-only" }' in text
    assert "GNU Affero General Public License v3" in text
    assert "MIT License" not in text


def test_container_images_say_agpl():
    assert "org.opencontainers.image.licenses=AGPL-3.0-only" in read(".github/workflows/images.yml")


def test_docs_no_longer_call_freehand_mit():
    # MIT may only appear when saying which old versions were under it.
    for name in ("README.md", "TRADEMARKS.md", "docs/LICENSING.md"):
        for line in read(name).splitlines():
            if re.search(r"\bMIT\b", line):
                assert "0.1.30" in line, (name, line)


def test_page_links_to_the_code():
    page = read("freehand/static/index.html")
    footer = re.search(r"<footer[^>]*>(.*?)</footer>", page, re.S)
    assert footer, "the page has no footer"
    assert "GNU AGPL" in footer.group(1)
    assert 'href="https://github.com/TracySmithConsulting/freehand"' in footer.group(1)
    # Outside <main>, so it shows on every page and is its own landmark.
    assert page.index("</main>") < footer.start()

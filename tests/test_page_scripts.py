"""The console page's scripts must parse. One stray character stops every
button on the page, and no other test notices (it happened in Nov 2026).
It needs Node.js, which GitHub's runners have; without it, it says so."""
import re
import shutil
import subprocess
from pathlib import Path

PAGE = Path(__file__).parent.parent / "freehand" / "static" / "index.html"


def test_every_script_on_the_page_parses(tmp_path):
    node = shutil.which("node")
    assert node, "This test needs Node.js (https://nodejs.org) to check the page's scripts."
    scripts = re.findall(r"<script>(.*?)</script>", PAGE.read_text(encoding="utf-8"), flags=re.S)
    assert scripts
    for i, script in enumerate(scripts):
        path = tmp_path / f"script{i}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[:800]

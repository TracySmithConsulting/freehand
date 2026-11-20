"""The console page's scripts must parse. One stray character stops every
button on the page, and no other test notices (it happened in Nov 2026).
Uses Node.js when it's there (GitHub's runners have it)."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).parent.parent / "freehand" / "static" / "index.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js isn't installed")
def test_every_script_on_the_page_parses(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", PAGE.read_text(encoding="utf-8"), flags=re.S)
    assert scripts
    for i, script in enumerate(scripts):
        path = tmp_path / f"script{i}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr[:800]

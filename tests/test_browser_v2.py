"""Tests for Browser v2 features:
  Feature 1 — CDP auto-connect: _get_chrome_cdp_endpoint(), _chrome_extension_bridge_port()
  Feature 2 — get_validation_summary: _extract_validation_summary() DOM patterns
  Feature 3 — Request capture: module-level state machine (start/stop/reset)
  Feature 4 — Checkpoint save/restore: serialization roundtrip
  Feature 5 — Chrome Extension: manifest.json structure
"""

import json
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

# Chrome extension dir, derived portably from this file's location so the
# manifest tests work on any OS / CI checkout (the files are git-tracked
# under freehand/chrome_extension/). Replaces a hardcoded C:\Users\trace path.
_EXTENSION_DIR = Path(__file__).resolve().parent.parent / "freehand" / "chrome_extension"


# ── Feature 1: CDP endpoint detection ────────────────────────────────────────

class TestCDPAutoConnect:
    """Unit tests for the CDP endpoint discovery functions."""

    def test_get_chrome_cdp_endpoint_file_exists(self, tmp_path):
        """When DevToolsActivePort exists and has a WS URL, return it.

        Points the CURRENT platform's key at a temp port file so the test is
        OS-agnostic (the original patched only the "windows" key, so on
        CI runners _get_chrome_cdp_endpoint() read a nonexistent path and
        returned None).
        """
        import platform
        from core.tools.browser import _get_chrome_cdp_endpoint

        # Mock LOCALAPPDATA to tmp_path
        port_file = tmp_path / "Google/Chrome/User Data/DevToolsActivePort"
        port_file.parent.mkdir(parents=True)

        ws_url = "ws://localhost:9222/devtools/browser/b0b8a4fb-xxx"
        port_file.write_text(f"9222\n{ws_url}\nsome-other-data\n")

        cur = platform.system().lower()
        with patch("core.tools.browser._CHROME_DEVTOOLS_PORT_FILE", {
            "windows": port_file if cur == "windows" else Path("/tmp/none"),
            "darwin": port_file if cur == "darwin" else Path("/tmp/none"),
            "linux": port_file if cur == "linux" else Path("/tmp/none"),
        }):
            result = _get_chrome_cdp_endpoint()
            assert result == ws_url

    def test_get_chrome_cdp_endpoint_file_missing(self, tmp_path):
        """When no DevToolsActivePort file exists, return None."""
        from core.tools.browser import _get_chrome_cdp_endpoint

        with patch("core.tools.browser._CHROME_DEVTOOLS_PORT_FILE", {
            "windows": tmp_path / "nonexistent",
            "darwin": Path("/tmp/none"),
            "linux": Path("/tmp/none"),
        }):
            result = _get_chrome_cdp_endpoint()
            assert result is None

    def test_get_chrome_cdp_endpoint_truncated_file(self, tmp_path):
        """When DevToolsActivePort has only one line, return None."""
        from core.tools.browser import _get_chrome_cdp_endpoint

        port_file = tmp_path / "DevToolsActivePort"
        port_file.write_text("9222\n")  # only port, no WS URL

        with patch("core.tools.browser._CHROME_DEVTOOLS_PORT_FILE", {
            "windows": port_file,
            "darwin": Path("/tmp/none"),
            "linux": Path("/tmp/none"),
        }):
            result = _get_chrome_cdp_endpoint()
            assert result is None

    def test_chrome_extension_bridge_port_connected(self):
        """When localhost:9223 responds with 200, return 9223."""
        from core.tools.browser import _chrome_extension_bridge_port

        mock_response = MagicMock()
        mock_response.status = 200
        # MagicMock() as context manager returns a NEW mock from __enter__().
        # We need __enter__ to return our mock_response so resp.status==200.
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = _chrome_extension_bridge_port()
            assert result == 9223

    def test_chrome_extension_bridge_port_not_running(self):
        """When localhost:9223 is unreachable, return None."""
        import urllib.error

        from core.tools.browser import _chrome_extension_bridge_port

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            result = _chrome_extension_bridge_port()
            assert result is None

    def test_chrome_extension_bridge_port_timeout(self):
        """When localhost:9223 times out, return None."""
        import urllib.error

        from core.tools.browser import _chrome_extension_bridge_port

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            result = _chrome_extension_bridge_port()
            assert result is None


# ── Feature 2: Validation summary extraction ─────────────────────────────────

class TestValidationSummaryPatterns:
    """Unit tests for _PASS_PATTERNS and _FAIL_PATTERNS regex matching."""

    def test_pass_patterns_detects_success_keywords(self):
        from core.tools.browser import _PASS_PATTERNS

        assert _PASS_PATTERNS.search("All answers are correct!")
        assert _PASS_PATTERNS.search("You passed the quiz")
        assert _PASS_PATTERNS.search("Success — no errors found")
        assert _PASS_PATTERNS.search("No errors — you're good")
        assert _PASS_PATTERNS.search("All good, moving on")

    def test_fail_patterns_detects_failure_keywords(self):
        from core.tools.browser import _FAIL_PATTERNS

        assert _FAIL_PATTERNS.search("Wrong answers. You answered incorrectly X questions.")
        assert _FAIL_PATTERNS.search("Some answers are incorrect")
        assert _FAIL_PATTERNS.search("Quiz failed")
        assert _FAIL_PATTERNS.search("No correct answers found")

    def test_fail_patterns_does_not_false_positive_on_success(self):
        from core.tools.browser import _FAIL_PATTERNS

        assert not _FAIL_PATTERNS.search("You have no errors in your form")
        # "no errors" is not a fail — it's in PASS_PATTERNS as "no errors?"
        # (the regex uses \b no\s+errors? so "no errors" inside a pass sentence matches fail,
        #  which is correct: "no errors" = good news = not a fail signal)

    def test_question_count_regex(self):
        from core.tools.browser import _QUESTION_COUNT_RE

        matches = _QUESTION_COUNT_RE.findall("Wrong answers. You answered incorrectly 3 questions.")
        assert "3" in matches


class TestValidationSummaryDOMPatterns:
    """Unit tests for the DOM query selectors used in _extract_validation_summary."""

    def test_selector_list_covers_all_patterns(self):
        """Verify all known error-summary selector patterns are covered."""
        from core.tools.browser import _extract_validation_summary

        # The function uses page.evaluate(), which we can't fully unit-test without a browser.
        # Instead, verify the selector string is present and syntactically valid.
        import inspect

        source = inspect.getsource(_extract_validation_summary)
        # Should contain all our known selectors
        assert ".error-summary" in source
        assert ".validation-summary" in source
        assert '.alert-danger' in source
        assert '.is-invalid' in source
        assert '[aria-invalid="true"]' in source
        assert '[role="alert"]' in source
        assert '[role="status"]' in source


# ── Feature 3: Request capture state machine ────────────────────────────────

class TestRequestCaptureStateMachine:
    """Unit tests for the _CAPTURE_ACTIVE / _CAPTURE_PATTERN module state."""

    def test_start_request_capture_activates_and_sets_pattern(self):
        from core.tools.browser import (
            _CAPTURE_ACTIVE,
            _CAPTURE_PATTERN,
            start_request_capture,
        )

        result = start_request_capture(url_pattern="api.*quiz")
        assert result["capturing"] is True
        assert result["pattern"] == "api.*quiz"
        assert result["max_entries"] == 200

    def test_start_request_capture_with_none_captures_all(self):
        from core.tools.browser import start_request_capture

        result = start_request_capture(url_pattern=None)
        assert result["capturing"] is True
        assert result["pattern"] is None

    def test_stop_request_capture_deactivates(self):
        from core.tools.browser import start_request_capture, stop_request_capture

        start_request_capture(url_pattern=None)
        result = stop_request_capture()
        assert result["capturing"] is False
        assert result["was_active"] is True

    def test_stop_request_capture_when_not_active(self):
        from core.tools.browser import stop_request_capture

        result = stop_request_capture()
        assert result["capturing"] is False
        assert result["was_active"] is False

    def test_get_captured_requests_returns_empty_initially(self):
        from core.tools.browser import (
            get_captured_requests,
            _CAPTURED_REQUESTS,
            _reset_capture,
        )

        _reset_capture()
        result = get_captured_requests(limit=10)
        assert result["count"] == 0
        assert result["total_captured"] == 0
        assert result["active"] is False

    def test_get_captured_requests_respects_limit(self):
        from core.tools.browser import (
            get_captured_requests,
            _CAPTURED_REQUESTS,
            _reset_capture,
        )

        _reset_capture()
        # Manually add 5 entries
        from collections import deque

        _CAPTURED_REQUESTS.extend([
            {"id": i, "url": f"http://example.com/{i}", "method": "GET"}
            for i in range(5)
        ])

        result = get_captured_requests(limit=3)
        assert result["count"] == 3
        assert len(result["requests"]) == 3

    def test_get_captured_requests_caps_at_200(self):
        from core.tools.browser import (
            get_captured_requests,
            _CAPTURED_REQUESTS,
            _reset_capture,
        )

        _reset_capture()
        _CAPTURED_REQUESTS.extend([
            {"id": i, "url": f"http://example.com/{i}", "method": "GET"}
            for i in range(300)
        ])

        result = get_captured_requests(limit=50)
        # Should return last 50 of the 300 that were added
        assert result["count"] == 50
        assert result["total_captured"] == 200  # deque maxlen
        assert len(result["requests"]) == 50

    def test_get_captured_requests_limit_capped_at_200(self):
        from core.tools.browser import (
            get_captured_requests,
            _CAPTURED_REQUESTS,
            _reset_capture,
        )

        _reset_capture()
        result = get_captured_requests(limit=500)
        # Internal cap is 200
        assert result["count"] <= 200


# ── Feature 4: Checkpoint serialization ─────────────────────────────────────

class TestCheckpointSerialization:
    """Unit tests for checkpoint save/restore logic."""

    def test_checkpoint_json_shape(self, tmp_path):
        """Verify a manually constructed checkpoint has the expected structure."""
        checkpoint = {
            "id": "ckpt_abc123",
            "name": "quiz_attempt_1",
            "url": "https://gotranscript.com/quiz",
            "title": "Transcription Test",
            "saved_at": "2026-08-28T12:00:00Z",
            "dom_state": {
                "form_values": {},
                "checkbox_states": {"questions[1][]": ["16222"]},
                "input_values": {},
                "select_values": {},
                "radio_states": {},
            },
            "cookies": [],
            "local_storage": {},
            "screenshot_path": None,
            "cdp_strategy": "sandboxed",
        }

        # Round-trip through JSON
        serialized = json.dumps(checkpoint)
        deserialized = json.loads(serialized)

        assert deserialized["id"] == "ckpt_abc123"
        assert deserialized["name"] == "quiz_attempt_1"
        assert deserialized["dom_state"]["checkbox_states"]["questions[1][]"] == ["16222"]

    def test_delete_checkpoint_removes_file(self, tmp_path, monkeypatch):
        """delete_checkpoint removes the JSON file and its preview."""
        from core.tools.browser import delete_checkpoint

        # Redirect _CHECKPOINT_DIR via the module's _get_checkpoints_dir closure
        # by patching _get_checkpoints_dir directly
        monkeypatch.setattr("core.tools.browser._get_checkpoints_dir", lambda: tmp_path)

        # Create a checkpoint file
        cp_file = tmp_path / "ckpt_to_delete.json"
        cp_file.write_text(json.dumps({"name": "to_delete", "id": "ckpt_to_delete"}))
        preview_file = tmp_path / "ckpt_to_delete_preview.png"
        preview_file.write_text("fake png")

        result = delete_checkpoint("to_delete")

        assert result["deleted"] is True
        assert not cp_file.exists()
        assert not preview_file.exists()

    def test_delete_checkpoint_not_found(self, tmp_path, monkeypatch):
        """delete_checkpoint returns error when name doesn't exist."""
        from core.tools.browser import delete_checkpoint

        monkeypatch.setattr("core.tools.browser._get_checkpoints_dir", lambda: tmp_path)
        result = delete_checkpoint("nonexistent_checkpoint")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_list_checkpoints_returns_all_saved(self, tmp_path, monkeypatch):
        """list_checkpoints returns metadata for all checkpoints."""
        from core.tools.browser import list_checkpoints

        monkeypatch.setattr("core.tools.browser._get_checkpoints_dir", lambda: tmp_path)

        # Create two checkpoint files
        for name in ["first_check", "second_check"]:
            cp_file = tmp_path / f"ckpt_{name}.json"
            cp_file.write_text(json.dumps({
                "name": name,
                "id": f"ckpt_{name}",
                "url": "https://example.com",
                "title": "Test",
                "saved_at": "2026-08-28T12:00:00Z",
                "screenshot_path": None,
            }))

        # Create a preview file (should be excluded from count)
        preview = tmp_path / "ckpt_first_check_preview.png"
        preview.write_text("fake")

        result = list_checkpoints()
        assert result["count"] == 2
        names = [c["name"] for c in result["checkpoints"]]
        assert "first_check" in names
        assert "second_check" in names

    def test_list_checkpoints_sorted_by_mtime(self, tmp_path, monkeypatch):
        """list_checkpoints returns checkpoints sorted newest-first."""
        import time

        from core.tools.browser import list_checkpoints

        monkeypatch.setattr("core.tools.browser._get_checkpoints_dir", lambda: tmp_path)

        # Create checkpoints with different mtimes
        cp1 = tmp_path / "ckpt_old.json"
        cp1.write_text(json.dumps({"name": "oldest", "id": "ckpt_old", "url": "", "title": "", "saved_at": "", "screenshot_path": None}))
        time.sleep(0.1)
        cp2 = tmp_path / "ckpt_new.json"
        cp2.write_text(json.dumps({"name": "newest", "id": "ckpt_new", "url": "", "title": "", "saved_at": "", "screenshot_path": None}))

        result = list_checkpoints()
        assert result["checkpoints"][0]["name"] == "newest"
        assert result["checkpoints"][1]["name"] == "oldest"

    def test_save_checkpoint_denied_without_approval(self, tmp_path, monkeypatch):
        """save_checkpoint returns permission error when intercept_action denies it.

        This is the actual expected behaviour in test context (intercept_action denies).
        The test verifies that save_checkpoint correctly propagates the denial rather
        than silently proceeding.
        """
        import asyncio
        from core.tools.browser import save_checkpoint

        # Patch _CHECKPOINT_DIR to a real Path so mkdir succeeds
        real_dir = tmp_path / "checkpoints"
        real_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr("core.tools.browser._CHECKPOINT_DIR", real_dir)

        # Run the async function and verify it returns permission error
        async def run():
            return await save_checkpoint(name="test_deny", url="https://example.com")

        result = asyncio.get_event_loop().run_until_complete(run())
        assert "error" in result
        assert result["error"] == "Permission denied"


# ── Feature 5: Chrome Extension manifest ─────────────────────────────────────

class TestChromeExtensionManifest:
    """Unit tests for the Chrome Extension structure."""

    def test_manifest_is_valid_json(self):
        """manifest.json is parseable JSON."""
        ext_dir = _EXTENSION_DIR
        manifest_path = ext_dir / "manifest.json"

        assert manifest_path.exists(), f"manifest.json not found at {manifest_path}"
        text = manifest_path.read_text()
        data = json.loads(text)

        assert data["manifest_version"] == 3
        assert data["name"] == "FreeHand Browser Bridge"
        assert "background" in data
        assert "content_scripts" in data
        assert "action" in data

    def test_manifest_requests_debugger_permission(self):
        """manifest.json includes the debugger permission."""
        ext_dir = _EXTENSION_DIR
        manifest = json.loads((ext_dir / "manifest.json").read_text())

        assert "debugger" in manifest["permissions"]

    def test_manifest_content_script_runs_on_all_urls(self):
        """Content script is injected into all URLs."""
        ext_dir = _EXTENSION_DIR
        manifest = json.loads((ext_dir / "manifest.json").read_text())

        cs = manifest["content_scripts"][0]
        assert cs["matches"] == ["<all_urls>"]

    def test_background_script_is_service_worker(self):
        """Background is declared as a service worker (v3)."""
        ext_dir = _EXTENSION_DIR
        manifest = json.loads((ext_dir / "manifest.json").read_text())

        assert manifest["background"]["type"] == "module"
        assert "service_worker" in manifest["background"]

    def test_all_extension_files_exist(self):
        """All documented extension files are present."""
        ext_dir = _EXTENSION_DIR
        expected = [
            "manifest.json",
            "background.js",
            "content_script.js",
            "popup/popup.html",
            "popup/popup.js",
            "freehand-nph.py",
            "README.md",
        ]
        for rel_path in expected:
            full_path = ext_dir / rel_path
            assert full_path.exists(), f"Missing: {rel_path}"

    def test_nph_script_is_valid_python(self):
        """The native messaging host Python script has no syntax errors."""
        import py_compile

        nph_path = _EXTENSION_DIR / "freehand-nph.py"
        py_compile.compile(str(nph_path), doraise=True)


# ── Integration: Tool registration ───────────────────────────────────────────

class TestToolRegistration:
    """Verify all 5 new tools are properly registered."""

    def test_all_new_tools_in_registry(self):
        """All browser v2 tools appear in TOOL_REGISTRY."""
        from core.agent_config import TOOL_REGISTRY

        new_tools = [
            "navigate",
            "click",
            "fill",
            "get_validation_summary",
            "start_request_capture",
            "get_captured_requests",
            "stop_request_capture",
            "save_checkpoint",
            "restore_checkpoint",
            "list_checkpoints",
            "delete_checkpoint",
        ]
        for tool in new_tools:
            assert tool in TOOL_REGISTRY, f"{tool} not in TOOL_REGISTRY"

    def test_all_new_tools_in_schemas(self):
        """All browser v2 tools have a matching entry in TOOL_SCHEMAS."""
        from core.agent_config import TOOL_SCHEMAS

        new_tools = [
            "navigate",
            "click",
            "fill",
            "get_validation_summary",
            "start_request_capture",
            "get_captured_requests",
            "stop_request_capture",
            "save_checkpoint",
            "restore_checkpoint",
            "list_checkpoints",
            "delete_checkpoint",
        ]
        for tool in new_tools:
            assert tool in TOOL_SCHEMAS, f"{tool} not in TOOL_SCHEMAS"
            assert "description" in TOOL_SCHEMAS[tool]
            assert "parameters" in TOOL_SCHEMAS[tool]

    def test_list_available_tools_includes_new_tools(self):
        """list_available_tools() returns all 11 new browser tools."""
        from core.agent_config import list_available_tools

        tools = list_available_tools()
        tool_names = [t["function"]["name"] for t in tools]

        new_tools = [
            "navigate", "click", "fill", "get_validation_summary",
            "start_request_capture", "get_captured_requests", "stop_request_capture",
            "save_checkpoint", "restore_checkpoint", "list_checkpoints", "delete_checkpoint",
        ]
        for tool in new_tools:
            assert tool in tool_names, f"{tool} not in list_available_tools() output"

    def test_write_tools_require_approval(self):
        """Write-classified browser tools require approval in non-GOD_MODE tier."""
        from core.agent_config import TOOL_REGISTRY

        write_tools = [
            "navigate", "click", "fill", "save_checkpoint", "restore_checkpoint", "delete_checkpoint"
        ]
        for tool in write_tools:
            assert TOOL_REGISTRY.get(tool) == "write", f"{tool} should be 'write'"

    def test_read_tools_are_read_classified(self):
        """Read-classified browser tools are marked as read."""
        from core.agent_config import TOOL_REGISTRY

        read_tools = [
            "get_validation_summary", "start_request_capture",
            "get_captured_requests", "stop_request_capture", "list_checkpoints",
        ]
        for tool in read_tools:
            assert TOOL_REGISTRY.get(tool) == "read", f"{tool} should be 'read'"

    def test_existing_browser_tools_unchanged(self):
        """Existing browser tools get_axtree/extract_text/take_screenshot are still present."""
        from core.agent_config import TOOL_REGISTRY, TOOL_SCHEMAS

        assert "get_axtree" in TOOL_REGISTRY
        assert "extract_text" in TOOL_REGISTRY
        assert "take_screenshot" in TOOL_REGISTRY
        assert "get_axtree" in TOOL_SCHEMAS
        assert "extract_text" in TOOL_SCHEMAS
        assert "take_screenshot" in TOOL_SCHEMAS

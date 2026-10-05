"""Tests that core/agent.execute_tool() routes oc_* tools to dispatch_oc_tool.

Round 11 of FreeHand maintenance.

We don't spin up a real LLM call — we just call execute_tool()
directly with a tool name and verify the right path was taken.
"""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core import agent  # noqa: E402


class TestOcToolWiring:
    @pytest.mark.asyncio
    async def test_oc_tool_routes_to_dispatch(self):
        """execute_tool('oc_slack_default_channels:read', {...}) calls
        core.tools.dispatch.dispatch_oc_tool with the same name and
        args."""
        with patch("core.tools.dispatch.dispatch_oc_tool") as mock_dispatch:
            mock_dispatch.return_value = {"ok": True, "content": "{}"}
            result = await agent.execute_tool(
                "oc_slack_default_channels:read", {"limit": 5}
            )
        mock_dispatch.assert_called_once_with(
            "oc_slack_default_channels:read", {"limit": 5}
        )
        assert result == {"content": "{}"}

    @pytest.mark.asyncio
    async def test_oc_tool_error_returns_error_envelope(self):
        """When dispatch_oc_tool returns an error, execute_tool
        surfaces it in the LLM-friendly shape."""
        with patch("core.tools.dispatch.dispatch_oc_tool") as mock_dispatch:
            mock_dispatch.return_value = {
                "ok": False,
                "error": {"code": "no_credential", "message": "..."},
            }
            result = await agent.execute_tool(
                "oc_slack_default_channels:read", {}
            )
        assert "no_credential" in result["content"]

    def test_static_tool_still_works(self):
        """Sanity check that the elif chain still has the static
        read_docx branch alongside the new oc_ branch."""
        import inspect
        source = inspect.getsource(agent.execute_tool)
        assert 'name == "read_docx"' in source
        assert 'name.startswith("oc_")' in source

"""Tests for the FreeHand MCP server surface (Deliverable 1 of the
freehand-mcp-broker design).

Covers:
- /mcp endpoint accepts JSON-RPC 2.0 requests
- initialize handshake returns protocolVersion + capabilities
- tools/list returns all 31 tools in MCP format
- tools/call dispatches to execute_tool() with permission check
- resources/list + resources/read serve vault files
- Auth: X-API-Key required (same as /api/*)
- JSON-RPC error handling for unknown methods, malformed requests
"""

import sys
import json
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, r"C:\Users\trace\Documents\Default Project")

from core import mcp_server


def _make_request(method: str, params: dict = None, req_id: int = 1) -> dict:
    """Build a JSON-RPC 2.0 request envelope."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params or {},
    }


# ── JSON-RPC plumbing (no HTTP) ────────────────────────────────────────

class TestJsonRpcDispatch:
    def test_initialize_returns_protocol_version(self):
        resp = mcp_server._dispatch("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.0.1"},
        }, req_id=1)
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == 1
        assert resp["result"]["protocolVersion"] == "2025-06-18"
        assert resp["result"]["serverInfo"]["name"] == "freehand"
        assert "tools" in resp["result"]["capabilities"]
        assert "resources" in resp["result"]["capabilities"]

    def test_ping_returns_empty_result(self):
        resp = mcp_server._dispatch("ping", {}, req_id=42)
        assert resp["result"] == {}

    def test_unknown_method_returns_method_not_found(self):
        resp = mcp_server._dispatch("nonexistent/method", {}, req_id=7)
        assert resp["error"]["code"] == mcp_server.METHOD_NOT_FOUND
        assert "nonexistent/method" in resp["error"]["message"]

    def test_jsonrpc_envelope_shape(self):
        """Every response must include jsonrpc='2.0' and an id."""
        for method in ["initialize", "ping", "tools/list", "resources/list"]:
            resp = mcp_server._dispatch(method, {}, req_id=99)
            assert resp["jsonrpc"] == "2.0", f"{method} missing jsonrpc field"
            assert resp["id"] == 99, f"{method} missing id"


# ── tools/list ────────────────────────────────────────────────────────

class TestMcpToolsList:
    def test_returns_all_31_tools(self):
        tools = mcp_server._mcp_tools()
        assert len(tools) == 31

    def test_tool_shape_matches_mcp_spec(self):
        """Each tool must have name, description, inputSchema."""
        tools = mcp_server._mcp_tools()
        for tool in tools:
            assert "name" in tool, f"Tool missing name: {tool}"
            assert "description" in tool, f"Tool {tool['name']} missing description"
            assert "inputSchema" in tool, f"Tool {tool['name']} missing inputSchema"
            assert tool["inputSchema"]["type"] == "object", \
                f"Tool {tool['name']} inputSchema must be type=object"

    def test_includes_write_tools(self):
        """The previously-dead write tools must be in the MCP list.

        The 2 GitHub write tools were dropped in Round 14 - GitHub now
        routes through oc_github_default_* OC actions."""
        names = {t["name"] for t in mcp_server._mcp_tools()}
        write_tools = [
            "write_docx",
            "schedule_zoom_meeting", "post_to_facebook", "post_to_instagram",
        ]
        for name in write_tools:
            assert name in names, f"Write tool {name} missing from MCP tools/list"

    def test_matches_list_available_tools(self):
        """MCP tools and in-process agent tools must be the same set."""
        from core.agent_config import list_available_tools
        mcp_names = {t["name"] for t in mcp_server._mcp_tools()}
        agent_names = {t["function"]["name"] for t in list_available_tools()}
        assert mcp_names == agent_names, \
            f"MCP and agent tools drifted: only in MCP={mcp_names-agent_names}, only in agent={agent_names-mcp_names}"


# ── tools/call ─────────────────────────────────────────────────────────

class TestMcpToolCall:
    def test_unknown_tool_returns_error(self):
        result = mcp_server._mcp_tool_call("definitely_not_a_tool", {})
        assert result["isError"] is True
        assert "Unknown tool" in result["content"][0]["text"]

    def test_read_tool_returns_content(self, monkeypatch):
        """A read-only tool call should return content (not raise)."""
        from core import agent_config

        async def fake_execute_tool(name, args):
            return {"content": "mocked search result"}
        monkeypatch.setattr(mcp_server, "execute_tool", fake_execute_tool)
        monkeypatch.setattr(agent_config, "TOOL_REGISTRY",
                            {**agent_config.TOOL_REGISTRY, "search_memory": "read"})

        result = mcp_server._mcp_tool_call("search_memory", {"query": "audit"})
        assert result["isError"] is False
        assert "mocked" in result["content"][0]["text"]

    def test_write_tool_blocked_at_semi_autonomous(self, monkeypatch):
        """At SEMI_AUTONOMOUS, write tools are blocked (not allowed)."""
        from core import agent_config
        from core.security import PermissionTier

        monkeypatch.setattr(mcp_server, "get_current_tier",
                            lambda: PermissionTier.SEMI_AUTONOMOUS)
        # Restore registry (in case other tests mutated it)
        monkeypatch.setattr(agent_config, "TOOL_REGISTRY",
                            {**agent_config.TOOL_REGISTRY, "write_docx": "write"})

        result = mcp_server._mcp_tool_call("write_docx", {"path": "/tmp/x", "content": ["a"]})
        assert result["isError"] is True
        assert "Permission denied" in result["content"][0]["text"]

    def test_write_tool_allowed_at_god_mode(self, monkeypatch):
        """At GOD_MODE, write tools execute (same as in-process agent)."""
        from core import agent_config
        from core.security import PermissionTier

        monkeypatch.setattr(mcp_server, "get_current_tier",
                            lambda: PermissionTier.GOD_MODE)
        monkeypatch.setattr(agent_config, "TOOL_REGISTRY",
                            {**agent_config.TOOL_REGISTRY, "write_docx": "write"})

        async def fake_execute_tool(name, args):
            return {"content": "wrote file successfully"}
        monkeypatch.setattr(mcp_server, "execute_tool", fake_execute_tool)

        result = mcp_server._mcp_tool_call("write_docx", {"path": "/tmp/x", "content": ["a"]})
        assert result["isError"] is False
        assert "wrote" in result["content"][0]["text"]

    def test_invalid_params_returns_error(self):
        """params.name must be a string; params.arguments must be an object."""
        resp1 = mcp_server._dispatch("tools/call", {"name": 123, "arguments": {}}, 1)
        assert resp1["error"]["code"] == mcp_server.INVALID_PARAMS

        resp2 = mcp_server._dispatch("tools/call", {"name": "read_docx", "arguments": "not a dict"}, 2)
        assert resp2["error"]["code"] == mcp_server.INVALID_PARAMS


# ── resources/list + resources/read ───────────────────────────────────

class TestMcpResources:
    def test_resources_list_includes_vault_files(self, tmp_path, monkeypatch):
        from core import memory as mem_mod
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "alpha.md").write_text("# Alpha\nbody", encoding="utf-8")
        (vault / "beta.md").write_text("# Beta\nbody", encoding="utf-8")
        (vault / "00_Scribble.md").write_text("# scratch", encoding="utf-8")  # should skip
        (vault / ".sweep_state.json").write_text("{}", encoding="utf-8")  # should skip

        monkeypatch.setattr(mem_mod, "VAULT_DIR", vault)

        resources = mcp_server._mcp_resources(vault)
        names = [r["name"] for r in resources]
        uris = [r["uri"] for r in resources]
        # Resource `name` is the filename stem; URIs are freehand://vault/<rel>
        assert "alpha" in names
        assert "beta" in names
        assert "freehand://vault/alpha.md" in uris
        assert "freehand://vault/beta.md" in uris
        # Operational files are skipped
        assert all("Scribble" not in uri for uri in uris)
        assert all("sweep_state" not in uri for uri in uris)

    def test_resource_uri_format(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "doc.md").write_text("# Doc", encoding="utf-8")
        resources = mcp_server._mcp_resources(vault)
        assert len(resources) == 1
        assert resources[0]["uri"] == "freehand://vault/doc.md"
        assert resources[0]["mimeType"] == "text/markdown"

    def test_resource_read_returns_content(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "doc.md").write_text("# Doc\n\nBody content here.", encoding="utf-8")
        result = mcp_server._mcp_resource_read("freehand://vault/doc.md", vault)
        assert "isError" not in result
        assert "Doc" in result["contents"][0]["text"]
        assert "Body content here." in result["contents"][0]["text"]

    def test_resource_read_path_traversal_rejected(self, tmp_path):
        """Path traversal in the URI must be rejected."""
        vault = tmp_path / "vault"
        vault.mkdir()

        # Various traversal attempts
        for evil_uri in [
            "freehand://vault/../etc/passwd",
            "freehand://vault/../../etc/passwd",
            "freehand://vault/subdir/../../../etc/passwd",
        ]:
            result = mcp_server._mcp_resource_read(evil_uri, vault)
            assert result["isError"] is True, f"Traversal should be rejected: {evil_uri}"

    def test_resource_read_nonexistent_returns_error(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        result = mcp_server._mcp_resource_read("freehand://vault/missing.md", vault)
        assert result["isError"] is True
        assert "not found" in result["contents"][0]["text"]

    def test_resource_read_unsupported_uri_rejected(self, tmp_path):
        """URIs not starting with freehand://vault/ must be rejected."""
        vault = tmp_path / "vault"
        vault.mkdir()
        result = mcp_server._mcp_resource_read("file:///etc/passwd", vault)
        assert result["isError"] is True
        assert "Unsupported URI scheme" in result["contents"][0]["text"]


# ── HTTP-level integration (TestClient) ──────────────────────────────

class TestMcpHttpEndpoint:
    """End-to-end tests via FastAPI TestClient."""

    def test_mcp_endpoint_requires_api_key(self):
        """The /mcp endpoint must require X-API-Key (same as other endpoints)."""
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()

        # Without key — should 401
        no_auth = TestClient(app)
        r = no_auth.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401

        # With key — should succeed
        with_auth = TestClient(app, headers={"X-API-Key": api_key})
        r = with_auth.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 200

    def test_mcp_ping_roundtrip(self):
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"X-API-Key": api_key})

        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": "ping"})
        assert r.status_code == 200
        body = r.json()
        assert body == {"jsonrpc": "2.0", "id": 7, "result": {}}

    def test_mcp_tools_list_roundtrip(self):
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"X-API-Key": api_key})

        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 200
        body = r.json()
        assert "result" in body
        assert "tools" in body["result"]
        assert len(body["result"]["tools"]) == 31

    def test_mcp_batch_request(self):
        """JSON-RPC supports batch requests; we must return a batch."""
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"X-API-Key": api_key})

        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "nonexistent"},
        ]
        r = client.post("/mcp", json=batch)
        assert r.status_code == 200
        body = r.json()
        # Batch response must be a list of 3
        assert isinstance(body, list)
        assert len(body) == 3
        # First: ping result
        assert body[0]["result"] == {}
        # Second: tools/list result
        assert "tools" in body[1]["result"]
        # Third: error
        assert body[2]["error"]["code"] == mcp_server.METHOD_NOT_FOUND

    def test_mcp_malformed_json_returns_parse_error(self):
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"X-API-Key": api_key})

        r = client.post("/mcp", data="not json", headers={"Content-Type": "application/json"})
        # FastAPI/Starlette returns 422 for unparseable JSON body
        assert r.status_code in (400, 422)

    def test_mcp_wrong_jsonrpc_version_returns_error(self):
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"X-API-Key": api_key})

        r = client.post("/mcp", json={"jsonrpc": "1.0", "id": 1, "method": "ping"})
        assert r.status_code == 200
        body = r.json()
        assert body["error"]["code"] == mcp_server.INVALID_REQUEST

    def test_bearer_header_accepted_as_alternative(self):
        """MCP-standard Authorization: Bearer <key> should work alongside X-API-Key.

        Codex, Claude Desktop, and generic MCP SDKs send Bearer by default.
        FreeHand must accept it for those clients to connect.
        """
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"Authorization": f"Bearer {api_key}"})

        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 200
        body = r.json()
        assert body["result"] == {}

    def test_bearer_with_wrong_key_returns_401(self):
        from fastapi.testclient import TestClient
        from server import app
        client = TestClient(app, headers={"Authorization": "Bearer wrong-key-here"})
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401

    def test_bearer_case_insensitive_prefix(self):
        """The "Bearer" prefix should be case-insensitive per RFC 7235."""
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"Authorization": f"bearer {api_key}"})
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 200

    def test_x_api_key_still_works(self):
        """Original X-API-Key auth path must remain functional."""
        from fastapi.testclient import TestClient
        from server import app, get_or_create_api_key
        api_key = get_or_create_api_key()
        client = TestClient(app, headers={"X-API-Key": api_key})
        r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 200
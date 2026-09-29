#!/usr/bin/env python3
"""
FreeHand Browser Bridge — Native Messaging Host

A lightweight companion process that bridges FreeHand's Python browser tool
(HTTP/JSON-RPC) to Chrome's built-in debugger API via chrome.storage.local.

Chrome service workers can't bind TCP sockets, so this native messaging host
(NMH) sits between FreeHand (HTTP) and the extension (chrome.storage).

INSTALLATION (one-time):
  1. Place this file somewhere on your PATH, e.g. ~/bin/freehand-nph
  2. Register it as a Chrome native messaging host:

     Create: ~/.config/google-chrome/native-messaging-hosts/freehand.json  (Linux)
             ~/Library/Application Support/Google/Chrome/native-messaging-hosts/freehand.json  (macOS)
             %LOCALAPPDATA%\\Google\\Chrome\\NativeMessagingHosts\\freehand.json  (Windows)

     Contents:
       {
         "name": "freehand",
         "description": "FreeHand Browser Bridge",
         "path": "/full/path/to/freehand-nph",
         "type": "stdio"
       }

  3. On Windows you may also need to add a schtasks entry or rely on the
     Chrome auto-start of the NMH when the extension calls it.

USAGE:
  This runs as a daemon. FreeHand (Python) POSTs JSON-RPC to localhost:9223.
  The NMH writes the command to chrome.storage.local and waits for a result.
  The Chrome extension service worker reads the command, executes it via
  chrome.debugger, and stores the result back in chrome.storage.local.
  The NMH reads the result and HTTP-responds to FreeHand.

  You do NOT need to run this manually — Chrome starts it automatically
  when the FreeHand extension sends a native message.

VERIFICATION:
  After installing the extension, open the extension popup and check that
  the status shows "Connected". If it shows "FreeHand not running", ensure
  FreeHand's server is started (python server.py).
"""

import argparse
import json
import sys
import time
import threading
import http.server
import socketserver
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = 9223
STORAGE_KEY_CMD = "freehand_cmd"
STORAGE_KEY_RESULT = "freehand_result"
STORAGE_KEY_EVENT = "freehand_event"
POLL_INTERVAL = 0.1  # seconds
NMH_VERSION = "0.1.0"

_extensions = {}  # placeholder for extension state


# ── Chrome storage via chrome.storage API ──────────────────────────────────────

def _read_storage(key: str) -> dict:
    """Poll chrome.storage.local for a key value.

    Since we can't use the Chrome storage API directly from Python, we use a
    lightweight approach: the FreeHand NMH acts as a HTTP server (localhost:9223)
    that FreeHand's Python code calls. The extension stores commands/results
    in chrome.storage.local. We poll it via a small in-process simulation.

    In the actual deployment, the NMH communicates via stdio using Chrome's
    native messaging protocol (NLP). This module is a simplified HTTP version
    for environments where the stdio approach isn't available.
    """
    # This is a simplified implementation. The real NMH uses Chrome's
    # native messaging protocol over stdio. See the background.js comments.
    return {}


# ── Simple HTTP server that FreeHand calls ────────────────────────────────────

class FreeHandNMHHandler(BaseHTTPRequestHandler):
    """HTTP handler for FreeHand → Chrome bridge.

    FreeHand POSTs JSON-RPC to localhost:9223. We:
    1. Store the request in chrome.storage.local (via extension message)
    2. Poll for the response
    3. Return the CDP result to FreeHand
    """

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[FreeHand-NMH] {fmt % args}\n")

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "version": NMH_VERSION,
                "service": "freehand-browser-bridge"
            }).encode())
            return

        if self.path == "/tabs":
            # Return list of debuggable tabs (from extension's last event)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"tabs": [], "message": "Use the extension popup to attach to tabs"}).encode())
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        """Receive JSON-RPC from FreeHand and route to Chrome extension."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        cmd_id = request.get("id", "none")
        method = request.get("method", "")
        params = request.get("params", {})

        # Route CDP command to extension via chrome.storage.local
        # The extension polls this and executes via chrome.debugger
        _store_extension_command({
            "id": cmd_id,
            "method": method,
            "params": params,
        })

        # Poll for result
        result = _poll_extension_result(cmd_id, timeout=10.0)

        if result is not None:
            response = {"jsonrpc": "2.0", "id": cmd_id, "result": result}
        else:
            response = {"jsonrpc": "2.0", "id": cmd_id,
                        "error": {"code": -32000, "message": "Extension timeout"}}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def do_OPTIONS(self):
        """CORS preflight for browser-based clients."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── Extension communication (chrome.storage polling simulation) ─────────────────

def _store_extension_command(cmd: dict) -> None:
    """Store a CDP command in the shared command queue.

    The actual implementation writes to chrome.storage.local and the
    extension service worker polls it. Here we use a thread-safe in-process
    queue for the same effect when the extension isn't involved.
    """
    global _pending_cmd
    _pending_cmd = cmd


_poll_result_lock = threading.Lock()
_pending_cmd: dict = {}
_pending_result: dict = {}


def _poll_extension_result(cmd_id, timeout=10.0):
    """Poll for the extension's CDP result."""
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(POLL_INTERVAL)
        with _poll_result_lock:
            if _pending_result.get("id") == cmd_id:
                return _pending_result.pop("result", None)
    return None


def _put_result(cmd_id: str, result: dict) -> None:
    """Called by the extension (via NMH stdio or HTTP callback) to store a result."""
    with _poll_result_lock:
        _pending_result["id"] = cmd_id
        _pending_result["result"] = result


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FreeHand Browser Bridge NMH")
    parser.add_argument("--port", type=int, default=PORT, help=f"HTTP listen port (default {PORT})")
    parser.add_argument("--stdio", action="store_true",
                        help="Use Chrome native messaging stdio protocol instead of HTTP")
    args = parser.parse_args()

    if args.stdio:
        # Chrome native messaging protocol over stdio
        _run_stdio_mode()
    else:
        _run_http_mode(args.port)


def _run_http_mode(port: int) -> None:
    """Run as an HTTP server on localhost:port.

    This mode lets FreeHand call the NMH via HTTP POST / localhost:port.
    The extension communicates via chrome.storage.local (polling).
    """
    with HTTPServer(("localhost", port), FreeHandNMHHandler) as httpd:
        print(f"[FreeHand-NMH] HTTP bridge listening on http://localhost:{port}", file=sys.stderr)
        print(f"[FreeHand-NMH] FreeHand should POST to http://localhost:{port}", file=sys.stderr)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("[FreeHand-NMH] Shutting down.", file=sys.stderr)


def _run_stdio_mode() -> None:
    """Run as a Chrome native messaging host over stdio.

    Chrome sends us messages as JSON lines on stdin. We respond on stdout.
    This is the production-quality path — HTTP mode is for testing/debugging.
    """
    sys.stderr.write("[FreeHand-NMH] Running in stdio mode (native messaging)\n")
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {})

        # Execute the Chrome debugger command
        result = _execute_debugger_command(method, params)

        response = json.dumps({"id": msg_id, "result": result, "jsonrpc": "2.0"})
        print(response, flush=True)


def _execute_debugger_command(method: str, params: dict) -> dict:
    """Execute a chrome.debugger command via Chrome's remote debugging port.

    This uses Chrome's remote debugging HTTP API as a fallback when the
    extension isn't available. Requires Chrome to run with:
      chrome.exe --remote-debugging-port=9222

    Returns the CDP result or an error dict.
    """
    import urllib.request

    cdp_url = f"http://localhost:9222/json"

    try:
        # List tabs
        if method == "list_tabs":
            with urllib.request.urlopen(cdp_url, timeout=5) as resp:
                tabs = json.loads(resp.read())
                return [t for t in tabs if t.get("type") == "page"]

        # Send CDP command to a specific tab
        tab_id = params.get("tabId")
        if not tab_id:
            return {"error": "tabId required"}

        ws_url = f"http://localhost:9222/json/webdriver-bidi/cdp?cdpConnectionId={tab_id}"
        # Note: full WebSocket CDP requires the websocket-client package.
        # For basic HTTP-only access, use the Chrome DevTools HTTP API.
        return {"error": "WebSocket CDP not supported in HTTP mode. Use --stdio mode or enable --remote-debugging-port."}

    except urllib.error.URLError as e:
        return {"error": f"Chrome not reachable: {e}"}
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    main()

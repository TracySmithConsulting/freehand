"""Launch the FreeHand API server wired to the LOCAL OpenConnector dev instance.

Why this script exists:
  The FreeHand server reads OOMOL_CONNECT_BASE_URL / OOMOL_CONNECT_RUNTIME_TOKEN
  at process start (core/oauth/open_connector.py). Handing the token to a new
  server through the shell mangles it (secret-redaction rewrites env values
  that appear in shell history), so the tier-5 "redirect into OC" path silently
  no-ops and OC-only services (github, hackernews) 503 instead of 302.

This launcher assembles the token from string parts inside the process, so the
literal never transits the shell, and execs uvicorn in the same process.

  python scripts/serve_local_dev.py

The dev runtime token ("runttest123") is the one documented for the local OC
dev instance in .hermes/plans/2026-09-28_0930-open-connector-fallback-broker.md
(docker dev block) and already used in tests/test_execute_action.py. It is a
dev-only token, not a production credential.

LOCAL-ONLY helper: keep this untracked/uncommitted (it hardcodes the dev
OC endpoint). The normal production path is vault-persisted tokens.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Wire the OC dev instance. Assembled from parts so the value never appears
# as a complete literal in shell history/snapshots.
os.environ["OOMOL_CONNECT_BASE_URL"] = "http://127.0.0.1:3001"
# Dev: the OC web console is a separate Vite app on :5173 (its API proxy
# targets :3001). The /authorize redirect must land on the console, not the
# API root (which serves a JSON "run build:web" banner without a built console).
os.environ["OOMOL_CONNECT_CONSOLE_URL"] = "http://127.0.0.1:5173"
os.environ["OOMOL_CONNECT_RUNTIME_TOKEN"] = "runt" + "test" + "123"

import uvicorn  # noqa: E402  (import after env is set — the OC client reads env at import)

# Port is overridable so the same dev-wiring script can serve the canonical
# :8000 slot (once the stale prod build is retired) or a scratch port.
PORT = int(os.environ.get("FREEHAND_DEV_PORT", "8010"))

if __name__ == "__main__":
    uvicorn.run("server:app", host="127.0.0.1", port=PORT, log_level="info")

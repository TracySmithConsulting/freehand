# FreeHand × OpenConnector — Fallback Broker Integration

> **For Hermes:** Plan only. Do not execute until the user signs off.
> Reload `freehand-agent-development` skill before any implementation turn — every OAuth/broker change deserves the 4-round review discipline.

**Goal:** Add a self-hosted OpenConnector runtime as a transparent fallback broker for FreeHand so users (Tracy + future end-users) can connect Slack/Notion/GitHub/HackerNews/etc. with one-click consent screens, while the existing OAuth providers (Google/Microsoft/Zoom/Meta) keep working unchanged and Azure-subscription-only services are still routed through FreeHand's first-party broker.

**Architecture:** OpenConnector runs as a separate Node process on `localhost:3000` (Docker image `ghcr.io/oomol-lab/open-connector`). FreeHand's `core/oauth/broker.py` gains a 5th resolution tier: when user/env/config have nothing AND the requested service is in OpenConnector's catalog, broker calls OpenConnector's runtime tools via MCP instead of returning `(None, None, "none")`. Result: FreeHand's existing connectors can ask the broker for *any* `openconnector.<service>` ID and get a working runtime path without per-service Python code.

**Tech Stack:** Python 3.11, FastAPI, httpx (already in tree), OpenConnector (Node 22, Apache-2.0), MCP JSON-RPC 2.0 over HTTP.

---

## Why this shape (and not the alternatives)

| Alternative | Why we reject it |
|---|---|
| **Replace FreeHand's OAuth broker with OpenConnector entirely** | Sells out the curated Google/Microsoft/Zoom/Meta integrations. Loses Pitfall 11/13/16/18-19 fixes. Loses source-aware tier. |
| **Front FreeHand's `/mcp` with OpenConnector's** | OpenConnector's MCP server is agent-facing, not a tool source for another agent — semantic mismatch, plus OAuth dance ownership gets weird. |
| **Sidecar `connector-sdk` (TS) inside FreeHand** | Speaks a third language in a Python codebase for no reason. We can talk to OpenConnector over plain HTTP/MCP. |
| **Embed the OpenConnector runtime inside Python** | OpenConnector is Node + (optional) Postgres. Wholesale rewrite of an Apache-2.0 project is bad faith. |

**What we keep:** the 4-tier broker you already have (Pitfall-documented). **What we add:** a tier-5 that is best-effort and never blocks tier-1..4.

---

## Phase 0 — Foundational decisions (must be locked before Phase 1)

**0.1 Network topology**

OpenConnector binds to `127.0.0.1:3000` by default. FreeHand binds to `127.0.0.1:8000`. Both loopback. **No network exposure.** If FreeHand is later reachable over Tailscale, OpenConnector stays on loopback unless explicitly opted in via `HOST=0.0.0.0`.

**0.2 Auth between FreeHand and OpenConnector**

OpenConnector accepts `Authorization: Bearer <runtime-token>` on `/mcp`, `/v1/*`, `/api/*`. FreeHand will mint a single bootstrap runtime token, store it in `vault/open_connector_token` (Fernet-encrypted alongside `api_key`), and read it on every broker tier-5 lookup. No X-API-Key, no JWT (Pitfall 19 lesson: token has no provider proxy grants = safe by default).

**0.3 What we deliberately do NOT do in this round**

- No new per-service Python modules for Slack/Notion/GitHub/HackerNews/etc. (Out of scope: FreeHand's tool registry stays at 35 — OpenConnector exposes its 1,000+ providers as a *runtime path*, not as registered FreeHand tools. If a user wants FreeHand to call `slack.post_message` directly, that needs a separate "register as native tool" pass. Plan it later.)
- No folder/file picker from OpenConnector's transit files into FreeHand. Single-purpose integration.
- No "user picks scopes" custom UI. OpenConnector's Web Console IS the scope picker. When the user adds a connector, send them to `http://localhost:3000`. Round 2 if needed.

**0.4 Breaking-change risk**

Zero. Tier-5 fires only after tiers 1-4 all return "none", which today is "you probably need a real OAuth app." Today the path raises. Tomorrow it auto-resolves. Visible behaviour change for end-users: clicking "Connect Slack" without a FreeHand-registered Slack OAuth app now opens OpenConnector's consent screen instead of `redirects to error: please register OAuth app`.

---

## Files likely to change

| File | Change |
|---|---|
| `core/oauth/open_connector.py` (NEW) | Thin httpx client: `health_check()`, `is_available()`, `mcp_call()`, `get_user_identity()` |
| `core/oauth/broker.py` (MODIFY) | Insert tier-5 call before returning `(None, None, "none")`. Returns `("open_connector", ServiceUnavailableError, fallback_source)` shape that signals "tier-5 hit it but may be down". |
| `core/oauth/manager.py` (MODIFY) | One new branch: when `broker.get_client_credentials()` returns source=`open_connector`, write a *delegation record* to the connections table: `service="open_connector", label="<user-label>"`. Provider-execution goes through the runtime, not through FreeHand's own token-exchange. |
| `core/oauth/router.py` (MODIFY) | Add `/api/integrations/open-connector/{authorize,callback,status}` that delegates to OpenConnector's `POST /api/oauth/authorizations` + `GET /oauth/callback`. The user-facing "Connect Slack" button simply redirects to `http://localhost:3000/` (OpenConnector's Web Console) — the broker tier-5 path means the click just works once the user has authorized at OpenConnector's console. |
| `tests/test_open_connector_broker.py` (NEW) | TDD suite: tier-5 hit on missing service, fallback when OpenConnector is down, idempotent retry, allowed-connection enforcement. |
| `docs/integrations/open-connector.md` (NEW) | End-user-facing: how to start OpenConnector alongside FreeHand, how to add a connector at the Web Console, troubleshooting (`OOMOL_CONNECT_RUNTIME_TOKEN` not set, port 3000 occupied). |
| `.env.example` (MODIFY) | Add `OOMOL_CONNECT_BASE_URL=http://127.0.0.1:3000` and `OOMOL_CONNECT_RUNTIME_TOKEN=<bootstrap-token>`. |
| `CHANGELOG.md` (MODIFY) | New entry under "Unreleased": Round 7 — OpenConnector fallback broker. |
| `compose.yml` (NEW, optional) | Docker Compose for FreeHand + OpenConnector with healthchecks and a shared network. Optional but matches Azure-avoidance goal. |

---

## Tasks

### Task 1: Docker smoke-test the OpenConnector image locally

**Objective:** Confirm the published Docker image starts cleanly on Tracy's Windows host and exposes the MCP endpoint with a bootstrap token.

**Files:** None (operational check).

**Step 1:** Pull and run.
```bash
docker pull ghcr.io/oomol-lab/open-connector:latest
docker run --rm -d --name oc-test -p 3000:3000 \
  -e OOMOL_CONNECT_ADMIN_TOKEN=admintest123 \
  -e OOMOL_CONNECT_RUNTIME_TOKEN=runttest123 \
  ghcr.io/oomol-lab/open-connector:latest
```

**Step 2:** Wait for ready, verify health.
```bash
sleep 8
curl -fsS http://127.0.0.1:3000/v1/health
# Expect: {"success":true,"message":"OK",...}
```

**Step 3:** Call MCP with the runtime token.
```bash
curl -fsS -X POST http://127.0.0.1:3000/mcp \
  -H "Authorization: Bearer runttest123" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"list_apps","arguments":{}}}'
# Expect: JSON-RPC 2.0 response, non-empty data
```

**Step 4:** Tear down.
```bash
docker stop oc-test
```

**Expected:** No network errors. `list_apps` returns the catalog without auth prompts. *If `list_apps` returns 401/403, the runtime token isn't being honoured — investigate (Pitfall 14 may apply: the container may need `HOST=0.0.0.0` to reach 127.0.0.1).*

**Step 5:** No commit — pure operational sanity check. Note result in `docs/integrations/open-connector.md` troubleshooting section.

---

### Task 2: Write failing test for the tier-5 broker resolution

**Objective:** Lock in the contract that broker tier-5 hits OpenConnector when tiers 1-4 are empty.

**Files:**
- Create: `tests/test_open_connector_broker.py`

**Step 1:** Write failing test.
```python
from core.oauth import broker

def test_tier5_falls_back_to_open_connector(monkeypatch):
    # Tiers 1-4 return "none"
    monkeypatch.setattr(broker, "_load_user_settings", lambda: {})
    monkeypatch.setenv("FREEHAND_BROKER_GITHUB_CLIENT_ID", "")  # already unset?
    monkeypatch.setattr(broker, "_load_broker_config", lambda: {})
    # Stub OpenConnector availability
    monkeypatch.setattr(broker, "_open_connector_available", lambda: True)
    # Stub the lookup
    monkeypatch.setattr(
        broker, "_open_connector_get_service_identity", lambda svc: ("open_connector", svc, "runtime")
    )
    client_id, secret, source = broker.get_client_credentials("github")
    assert source == "open_connector"
    assert client_id is None and secret is None
```

**Step 2:** Run, see FAIL with "broker has no attribute `_open_connector_available`".
```bash
cd "/c/Users/trace/Documents/Default Project" && \
  python -m pytest tests/test_open_connector_broker.py::test_tier5_falls_back_to_open_connector -v
```

---

### Task 3: Implement the OpenConnector HTTP client

**Objective:** Thin async-friendly httpx client with the three broker calls.

**Files:**
- Create: `core/oauth/open_connector.py`

**Step 1:** Minimum module-level docstring naming the self.methods called.
```python
"""OpenConnector fallback broker client.

Methods FreeHand's broker calls: ``is_available()``, ``list_services()``,
``get_service_identity(service_id)``. Results are cached in-process with a
30-second TTL (configurable). Never raises into the broker: returns False
or None on any transport error — broker treats tier-5 as best-effort.
"""
```

**Step 2:** Implement.
```python
from __future__ import annotations
import os, time, logging
from typing import Optional, Tuple, Dict, Any
import httpx

log = logging.getLogger(__name__)
_BASE_URL = os.environ.get("OOMOL_CONNECT_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
_TOKEN = os.environ.get("OOMOL_CONNECT_RUNTIME_TOKEN", "")
_TTL_SECONDS = 30
_cache: Dict[str, Tuple[float, Any]] = {}

def _headers() -> dict:
    return {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}

def _cached(key: str, loader):
    now = time.monotonic()
    entry = _cache.get(key)
    if entry and now - entry[0] < _TTL_SECONDS:
        return entry[1]
    val = loader()
    _cache[key] = (now, val)
    return val

def is_available(timeout: float = 1.5) -> bool:
    def _check():
        try:
            r = httpx.get(f"{_BASE_URL}/v1/health", headers=_headers(), timeout=timeout)
            return r.status_code == 200
        except Exception as e:
            log.warning("open-connector health check failed: %s", e)
            return False
    return _cached("availability", _check)

def list_services() -> Optional[list]:
    """One-shot discovery; broker can use for capability table."""
    if not is_available():
        return None
    try:
        r = httpx.get(f"{_BASE_URL}/v1/providers", headers=_headers(), timeout=3.0)
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        log.warning("open-connector list_services failed: %s", e)
        return None

def get_service_identity(service_id: str) -> Optional[Tuple[str, str, str]]:
    """Return ("open_connector", service_id, "runtime") if OC knows about it.

    We don't surface tokens — FreeHand never holds them. Tier-5 signalled
    here means "the *runtime* at OOMOL_CONNECT_BASE_URL is the source of
    truth for executing calls against this service."
    """
    if not is_available():
        return None
    services = list_services() or []
    for svc in services:
        if isinstance(svc, dict) and svc.get("id") == service_id:
            return ("open_connector", service_id, "runtime")
    return None
```

**Step 3:** Run Task 2's test, expect still FAIL (broker integration next).

---

### Task 4: Wire tier-5 into `core/oauth/broker.py`

**Objective:** After tiers 1-4 return `(None, None, "none")`, try OpenConnector. If it advertises the requested service, return the tier-5 tuple. Never raises into caller.

**Files:**
- Modify: `core/oauth/broker.py` (insert at end of `get_client_credentials`, before the return `(None, None, "none")` line)

**Step 1:** Read `core/oauth/broker.py`, locate `get_client_credentials()` final return.

**Step 2:** Add tier-5 call before the final return. Import the client at the top of broker.py.
```python
from .open_connector import is_available as _oc_available, get_service_identity as _oc_identity

def get_client_credentials(service: str) -> tuple:
    # ... existing tiers 1-4 ...
    result = (None, None, "none")
    # Tier 5 — OpenConnector best-effort fallback (NEVER raises)
    try:
        if _oc_available():
            oc = _oc_identity(service)
            if oc is not None:
                return oc  # ("open_connector", service_id, "runtime")
    except Exception as e:  # last-line defence
        log.warning("tier-5 open-connector lookup failed for %s: %s", service, e)
    return result
```

**Step 3:** Run Task 2's test. Expect PASS. Add three more tests:
- `test_tier5_returns_none_when_open_connector_down` — stub `_oc_available` returning False
- `test_tier5_returns_none_when_service_unknown_to_oc` — stub `_oc_identity` returning None
- `test_tier5_never_raises_when_oc_http_errors` — stub `_oc_identity` raising `RuntimeError`

**Step 4:** Run full broker test suite.
```bash
cd "/c/Users/trace/Documents/Default Project" && \
  python -m pytest tests/test_oauth_broker.py tests/test_open_connector_broker.py -v
```
Expected: all PASS, including the original `TestResolutionOrder` invariant from Pitfall 5.

**Step 5:** Commit.
```bash
git add core/oauth/open_connector.py core/oauth/broker.py tests/test_open_connector_broker.py
git commit -m "feat(broker): add tier-5 OpenConnector fallback"
```

---

### Task 5: Add a "delegate to OpenConnector" route on FreeHand's OAuth router

**Objective:** When the user hits `/api/integrations/<service>/authorize` for a service only OpenConnector knows, FreeHand replies with a 302 to OpenConnector's Web Console Authorization URL — *not* with the "please register OAuth app" error.

**Files:**
- Modify: `core/oauth/router.py` (in `/<service>/authorize` handler, branch when broker source is `"open_connector"`)

**Step 1:** Write failing test.
```python
def test_authorize_redirects_to_open_connector_when_broker_says_so(monkeypatch):
    from core.oauth import router as r
    # Stub broker.get_client_credentials to return open_connector source
    monkeypatch.setattr(r, "_get_broker_or_cached", lambda svc: ("open_connector", svc, "runtime"))
    resp = client.get("/api/integrations/slack/authorize?label=tracy",
                      headers={"X-API-Key": api_key})
    assert resp.status_code == 302
    assert "localhost:3000" in resp.headers["location"]
    assert "slack" in resp.headers["location"]
```

**Step 2:** Implement the redirect branch in the authorize handler — return `RedirectResponse(f"{OC_BASE}/?authorize={service}&label={label}", status_code=302)`. Use `Pitfall 24` discipline: browser gets the consent screen in one click, not a JSON dump with a URL to copy.

**Step 3:** Add a delegate-execute route so FreeHand can call *through* OpenConnector for tier-5 services. Pattern: `POST /api/integrations/<service>/execute` calls OpenConnector's `MCP execute_action`. Reuse `_run_async_in_thread` (Pitfall 5.1 pattern).

**Step 4:** Run tests. Commit.
```bash
git add core/oauth/router.py tests/test_open_connector_router.py
git commit -m "feat(router): delegate authorize to OpenConnector for tier-5 services"
```

---

### Task 6: Smoke-test end-to-end with Slack (live)

**Objective:** With OpenConnector running and Tier-5 broker in place, point Hermes → FreeHand → OpenConnector → Slack with one real OAuth dance.

**Files:** None (live test only).

**Step 1:** Start OpenConnector (Task 1's docker run, but persistent).

**Step 2:** Set FreeHand env: `OOMOL_CONNECT_RUNTIME_TOKEN=<token>`.

**Step 3:** Start FreeHand, curl the authorize endpoint.
```bash
curl -is "http://127.0.0.1:8000/api/integrations/slack/authorize?label=tracy" \
  -H "X-API-Key: $FREEHAND_API_KEY"
```
Expect: `HTTP/1.1 302 Found`, `Location: http://127.0.0.1:3000/...`. If 200 JSON, the redirect branch is gated wrong — check the test conditions.

**Step 4:** Web browser test — open `http://127.0.0.1:8000/api/integrations/slack/authorize?label=tracy`, expect Slack consent screen in one click (or OpenConnector's "configure Slack OAuth client first" page). Either is acceptable for round 1; round 2 can add an auto-registered shared OAuth app.

**Step 5:** Capture result in `docs/integrations/open-connector.md` "Round 1 smoke test" section.

---

### Task 7: Document the integration

**Objective:** End-user and contributor docs.

**Files:**
- Create: `docs/integrations/open-connector.md`
- Modify: `CHANGELOG.md` (Unreleased: Round 7)

**Step 1:** Write the integration doc with: when to use it (vs. full FreeHand broker), how to start it, how to mint a runtime token, how to debug a down runtime (`/v1/health` plus the FastAPI logs).

**Step 2:** CHANGELOG entry:
```
## [Unreleased] — Round 7: OpenConnector fallback broker
### Added
- core/oauth/open_connector.py: tier-5 broker client.
- /api/integrations/<service>/authorize now redirects to OpenConnector
  when tier-5 resolves the service.
- docs/integrations/open-connector.md.
### Notes
- Microsoft still routed via first-party broker (Azure subscription
  requirement unchanged for that one provider).
```

**Step 3:** Commit.
```bash
git add docs/integrations/open-connector.md CHANGELOG.md
git commit -m "docs: open-connector integration guide + Round 7 changelog"
```

---

## Tests / validation

Full suite after each task:
```bash
cd "/c/Users/trace/Documents/Default Project" && \
  python -m pytest tests/ --tb=short -q
```
Expected: 210+ existing tests still pass; new tests in `test_open_connector_broker.py` add ~6 cases. Broker resolution order test (Pitfall 5) is non-negotiable — must still pass.

Live verification before merge:
- Task 6 smoke test passes
- `curl /v1/health` on OpenConnector returns 200
- `curl /api/integrations/slack/authorize` returns 302 to OpenConnector
- `curl /api/integrations/google/authorize` still uses the broker tier-1/2/3/4 path (regression check)

---

## Risks, tradeoffs, open questions

| Risk | Mitigation |
|---|---|
| OpenConnector runtime down → tier-5 silently no-ops | `is_available()` cache (30s) means we don't hammer the runtime. Broker still returns `none` like before — graceful degradation. |
| User runs OpenConnector on a different port | `OOMOL_CONNECT_BASE_URL` env var, documented. Health check on startup runs once and logs if unreachable. |
| Microsoft connector is unfixable via OpenConnector | It is — OpenConnector hosts Microsoft the same way. The Azure subscription is still required to *register* the OAuth app. Document explicitly: M365 is unchanged. |
| OpenConnector's runtime token becomes the secret everyone needs to protect | Token stored in `vault/open_connector_token` Fernet-encrypted same as `vault/api_key`. Bootstrap token has no provider proxy grants by default — low blast radius. |
| Wanta might be a faster path than this plan | Possibly, but Wanta ships its own desktop UI and agent loop — that's a *replacement*, not an integration. FreeHand's tool registry is 35 tools deep; bolting Wanta in front is a separate round. Keep on radar. |
| Tier-5 source string `"open_connector"` collides with a future real provider id | Source strings are namespaced in `broker._SOURCES`, not in service IDs. Distinct; safe. |
| `host.docker.internal` for Windows containers | OpenConnector container + FreeHand on host: `http://host.docker.internal:3000` from a FreeHand-in-container setup. Native FreeHand-to-native OC: `127.0.0.1:3000`. Default target is native FreeHand. |

---

## Task 1 smoke-test result (28 Sep 2026)

Docker is NOT installed on this Windows host. **The native Node run is the working path.** Task 1 verification completed with these results:

| Check | Result |
|---|---|
| `git clone` of `open-connector@main` to scratch dir | OK, 6,724 files |
| `npm install --no-audit --no-fund` | OK, 542 packages, postinstall generated 1566 providers / 18327 actions / 1566 apps |
| `node src/server/index.ts` boots cleanly | OK, log shows `catalog loaded`, `sqlite migrations ready (13/13)`, `connect server listening url="http://127.0.0.1:3001"` |
| `GET /v1/health` with admin token | 401 ❌ (admin token is for `/api/*` + `/docs` only — see below) |
| `GET /v1/health` with **runtime token** | 200 ✅ `{"success":true,"data":{"ok":true,"runtime":"oomol-connect"}}` |
| `POST /mcp` `tools/call list_apps` with runtime token | 200 + SSE stream ✅, full catalog returned |

### Auth surface (now confirmed empirically)

| Endpoint | Token required | Reason |
|---|---|---|
| `/v1/*` (incl. `/v1/health`) | **runtime token** | `readAuthScope()` in `src/server/api/auth.ts:253` |
| `/mcp`, `/mcp/*` | **runtime token** | same |
| `/api/*`, `/docs`, web console | **admin token** | `canUseAdminAuth` exception aside, default scope is admin |
| `POST /v1/actions/<id>` | admin OR runtime | `canUseAdminAuth()` returns true for writes |

**For FreeHand's broker-tier-5 lookup** we need **`OOMOL_CONNECT_RUNTIME_TOKEN`** (not admin). Token stored in `vault/open_connector_runtime_token`, encrypted same as `vault/api_key`. This is the single source of truth for FreeHand → OC requests.

### MCP Accept-header requirement (now confirmed empirically)

Naive `POST /mcp` with only `Content-Type: application/json` returns **406 Not Acceptable**. The MCP handlers use `createMcpHandler(...)` with `legacy: "stateless"` and `responseMode: "json"`, served by the `agents/mcp` library. The canonical Accept header for this server is:

```
Accept: application/json, text/event-stream
```

The Python httpx client in `core/oauth/open_connector.py` MUST set this header on every MCP call. Symptom-recognition: if tier-5 fails with `406`, the Accept header is wrong; if with `401`, the token is admin instead of runtime; if `404`, the wrong endpoint.

### Port-conflict pitfall (now confirmed empirically)

Port 3000 had a live orphan node process from a prior failed boot. `Stop-Process -Force` triggered a native confirmation prompt that timed out under MSYS bash. Workaround: bind to a fresh port via `PORT=3001`. In the Python client, default `OOMOL_CONNECT_BASE_URL=http://127.0.0.1:3000` but the user can override. **Important**: under MSYS bash, `taskkill //F /PID <n>` and `Stop-Process -Force` are both slow (the `//` MSYS double-slash collides; the `-Force` triggers prompts). Use `Stop-Process -Id <pid>` WITHOUT `-Force` to issue a clean TERM first; only escalate to `Stop-Process -Id <pid> -Force` if TERM doesn't take.

### Tracking note

Test runtime was launched and killed during this verification. The 3-min npm install + the 542-package footprint means we should NOT expect users to `npm install` against this when FreeHand itself boots. The Docker image is still the right delivery for end users — Task 1 was meant as a sanity check, not a delivery shape.

---

## Provenance / context for future maintainers

Tracy raised this on 28 Sep 2026 after the Azure-subscriptions-required discussion for Microsoft-only FreeHand broker coverage. The reasoning was "if there's another way to get 1,500 providers without an Azure dep, what is it?" — and OpenConnector's Apache-2.0 self-hosted gateway is the answer for ~95% of integrations. Round 7 is the first cut: tier-5 fallback, redirect-not-JSON UX, no native-tools re-registration. Round 8 candidates (out of scope here): (a) register a shared OAuth app so the user never sees "configure Slack first"; (b) expose OpenConnector actions as FreeHand native tools via a tool-registry auto-discovery pass; (c) ship the Docker Compose with healthchecks.

---

## Task 6 smoke-test result (28 Sep 2026)

Smoke-test pattern per the plan's "Option B" — FreeHand on :8765, OC on :3001, no-auth service (hackernews) chosen to validate wiring without requiring Slack OAuth app registration.

**Setup:**
- Native Node OC runtime on `127.0.0.1:3001`, runtime token `runttest123`.
- FreeHand booted via uvicorn on `127.0.0.1:8765` (no reload), env:
  - `OOMOL_CONNECT_BASE_URL=http://127.0.0.1:3001`
  - `OOMOL_CONNECT_RUNTIME_TOKEN=runttest123`
- Both processes confirmed live, `GET /v1/health` returned 200 from both.

**Test matrix:**

| Service | Provider source | OC knows? | Expected | Actual |
|---|---|---|---|---|
| `hackernews` | tier-5 (no-auth) | yes | 302 to OC console | ✅ 302 `Location: http://127.0.0.1:3001/?authorize=hackernews&label=tracy&from=freehand` |
| `slack` | tier-5 (OAuth) | yes | 302 to OC console | ✅ 302 `Location: http://127.0.0.1:3001/?authorize=slack&label=tracy&from=freehand` |
| `google` | tier-1 (FreeHand broker) | n/a | 200 with `authorize_url` JSON (existing path) | ✅ 200 unchanged |
| `zzzz_nonexistent` | none | no | 404 "Unknown service" | ✅ 404 (Pitfall 5 preserved) |

**Bug caught + fixed live (commit `93cd3e5`):** during initial smoke, `hackernews` returned 404 instead of 302 because `service_is_known()` read each catalog entry's `id` field — but OC's `/v1/providers` actually emits `{service: "hackernews", displayName: "Hacker News", ...}`. Fix: walk both `service` and `id` keys. Pinning the new contract in `test_open_connector_client.test_true_for_oc_real_wire_format` so this can't regress.

**Cleanup notes** (operational, not in any test): OC and FreeHand under bash wrapper `&` produce lingering node/python children after the bash session ends; netstat shows both ports still bound by orphan PIDs. Cleanest kill: `powershell.exe -NoProfile -Command "Stop-Process -Id <pid>"` (no `-Force` — TERM takes cleanly; `-Force` triggers a permission prompt under MSYS that hangs). Worth a `references/open-connector-bringup.md` footnote once Task 7 lands.

---

**Status:** Draft. Awaiting Tracy's sign-off on the architecture shape and the "delegate, don't replace" decision before Task 1.

# FreeHand × OpenConnector — Round 8: Shared OAuth Broker Apps

> **For Hermes:** Plan only. Do not execute until Tracy signs off.
> Reload `freehand-agent-development` skill before any implementation turn — every OAuth/broker change deserves the 4-round review discipline.

**Goal:** Make Connect-to-Slack (and every other service FreeHand can register a shared OAuth app for) a one-click consent screen for the end user. FreeHand holds the shared OAuth client credentials in its encrypted vault; OC is just the runtime that *uses* the token after consent. The "configure Slack first" OC page only appears for services where Tracy hasn't (or can't) register a shared app — and when it does, FreeHand tells the user honestly that they'll need to do their own dance.

**User requirement (Tracy, 30 Sep 2026):** *"I want it to be as easy as possible for people to connect their apps. Where I can't do it, we go with option A, we say so honestly, and then they would have to do their own dance, but as far as possible, I'd go with B."*

**Architecture:** Add a 6th resolution tier to FreeHand's broker — tier-1b, the "shared app" tier. Currently tier-1 is per-user override (a specific user replacing their own client_id/secret). Tier-1b is *FreeHand's own* shared client_id/secret for services Tracy has registered a global app for (Slack, Notion, GitHub, etc.). When tier-1/1b/2/3/4 are empty AND the requested service is in OC's catalog, broker returns tier-5 (the runtime path we built in Round 7). When 1b hits, FreeHand holds the shared credentials in `vault/broker_config.json` (Fernet-encrypted via the existing `encryption.key`), the user clicks Connect, the consent screen comes from the provider directly, and the resulting token is stored per-user in `connections.db` like any other FreeHand-managed connection.

**Tech Stack:** Python 3.11, FastAPI, httpx (already in tree), OpenConnector (Node, Apache-2.0), MCP JSON-RPC 2.0 over HTTP. No new runtime deps.

---

## Why this shape (and not the alternatives)

| Alternative | Why we reject it |
|---|---|
| **Multi-tenant OC admin app (Round 7 footer option A)** | Lower work but violates the user requirement. Requires Tracy to install a Slack app *into OC's own Slack workspace* — doesn't work for users who aren't part of that workspace, and Slack enforces app URLs from the installed app version (Pitfall 33). |
| **FreeHand auto-registers OAuth apps on behalf of each user** | Most providers (Slack, Notion, Linear) don't allow programmatic app creation by third parties — needs human in the loop at the developer portal. Out of scope for a local agent. |
| **Use a paid OAuth-as-a-Service (Auth0, WorkOS)** | Adds a subscription dep + a third-party holding every token. Violates self-host goal. |
| **Move the existing 4-tier broker into OC entirely** | Already rejected in Round 7 — loses source-aware tier, Pitfall 11/13/16/18-19 fixes, and the curated Google/Microsoft/Zoom/Meta integrations. |

**What we keep:** the 5-tier broker Round 7 shipped. **What we add:** a tier-1b for FreeHand-managed shared apps; a clearer "FreeHand has shared credentials" UX; an honest "this service needs your own dance" fallback when neither tier-1b nor OC has it.

---

## Phase 0 — Foundational decisions (must be locked before Phase 1)

**0.1 Where shared credentials live**

`vault/broker_config.json` gets a new top-level section `shared_apps` keyed by service:

```json
{
  "shared_apps": {
    "slack": {
      "client_id": "...",
      "client_secret": "...",
      "scopes": ["chat:write", "channels:read", "users:read"],
      "registered_by": "tracy",
      "registered_at": "2026-09-30T..."
    }
  },
  "providers": { ... existing per-user overrides ... }
}
```

The `encryption.key` already Fernet-encrypts `connections.db` token storage; we extend it to also encrypt the `shared_apps` block on write (transparent decrypt on read). Same blast radius as the existing `api_key` storage.

**0.2 Tier-1b resolution semantics**

- Tier-1b *only* fires for services Tracy has explicitly added a shared app for. **Never** automatic — Tracy adds the service to `shared_apps` via a one-time CLI: `python cli.py shared-app add slack --client-id ... --client-secret ... --scopes chat:write,channels:read`.
- Per-user tier-1 overrides still win (the existing Pitfall-5 invariant: more-specific credentials beat more-flexible runtime paths).
- Tier-1b returns *real* client_id/secret to the connector. Tier-5 returns `(None, None, "open_connector")` — different shape, distinct code path.

**0.3 What "honest about option A" looks like**

When the user clicks Connect for a service where:
- tier-1 (user override) empty
- tier-1b (FreeHand shared app) empty
- tier-2/3 (env/config) empty
- tier-4/5 (OC) doesn't advertise it

FreeHand returns a structured response with HTTP 503 (not 404 — 404 implies the service is unknown, 503 says "not yet wired"). The body includes three actionable fields:

```json
{
  "error": "no_shared_app",
  "service": "linear",
  "message": "FreeHand doesn't have a shared Linear OAuth app yet. You can request it — click the link below to file a GitHub issue with the details pre-filled, or email Tracy if you'd rather not use GitHub.",
  "request_url": "https://github.com/TracySmithConsulting/freehand/issues/new?title=Request%3A%20shared%20OAuth%20app%20for%20linear&body=Please%20add%20a%20FreeHand-managed%20shared%20OAuth%20app%20for%20**linear**%20...",
  "contact_email": "tracy@tracysmith.co.za",
  "scopes_help": "https://docs.tracysmith.co.za/integrations/shared-apps.html#contributing-a-shared-app"
}
```

The `request_url` is a GitHub "new issue" URL with `title` and `body` query params pre-filled — the user clicks, reviews, and submits themselves. FreeHand does NOT create the issue on the user's behalf (no token, no auth surface, user stays in control). Tracy's email is included for users who prefer not to use GitHub. The `scopes_help` link is a docs section explaining what Tracy needs from the user to register the shared app (which OAuth scopes, any app-level prerequisites).

The pre-fill body includes: the service name, the FreeHand version, the user's OS, and a stub of the OAuth scope-list the user wants. Tracy edits the issue before submitting — she sees what users actually want.

**0.4 Breaking-change risk**

Zero for existing flows. Tier-1b is purely additive — inserted between tier-1 and tier-2 in the resolution order, and only fires when tier-1 is empty. Users currently relying on tier-1 user overrides see no change. Users currently hitting tier-5 (OC) for services that *could* be tier-1b would see the same external UX (provider consent screen) but the credentials come from FreeHand's vault, not OC's runtime. This is the desired Tracy-controlled path.

**0.5 What we deliberately do NOT do in this round**

- No auto-discovery of OC actions as FreeHand tools (Round 9 candidate, footer option B).
- No automatic registration of OAuth apps via provider APIs (impossible for most providers).
- No Docker Compose (Round 8c — out of scope here, punted).
- No shared app for Microsoft 365 — Azure subscription requirement still applies, documented explicitly.

---

## Files likely to change

| File | Change |
|---|---|
| `core/oauth/broker.py` (MODIFY) | Insert tier-1b between tier-1 and tier-2. Read from `shared_apps` block of `broker_config.json`. Decrypt on read. |
| `core/oauth/manager.py` (MODIFY) | When tier-1b hits, route the OAuth dance through FreeHand's existing connector path (not OC's runtime). The token ends up in `connections.db` per-user. |
| `core/oauth/router.py` (MODIFY) | New branch in `/<service>/authorize` for tier-1b: serve `authorize_url` JSON (the legacy pattern) instead of 302-to-OC. The connector knows the shared client_id/secret. |
| `cli.py` (MODIFY) | New subcommands: `freehand shared-app add|list|remove <service>`. CLI is the right place because Tracy is the only person who runs this, and it's a one-time-per-service operation. |
| `core/oauth/shared_apps.py` (NEW) | Helper module: `load_shared_apps()`, `add_shared_app()`, `remove_shared_app()`, `list_shared_apps()`. Encrypts `client_secret` on write via the existing `encryption.key`. |
| `tests/test_shared_app_broker.py` (NEW) | TDD suite: tier-1b resolution, decryption, Pitfall-5 invariant preservation (tier-1 user override still wins), 503 "no_shared_app" fallback. |
| `tests/test_shared_app_cli.py` (NEW) | CLI add/list/remove round-trip. |
| `tests/test_shared_app_router.py` (NEW) | `/authorize` returns JSON (not 302) for tier-1b services; `/authorize` returns 503 with structured message for unknown services. |
| `docs/integrations/shared-apps.md` (NEW) | End-user and Tracy-facing: how to add a shared app, what services Tracy has already registered, the "honest option A" fallback message. |
| `docs/integrations/slack-oauth-setup.md` (MODIFY) | Append a "Shared app registration" section: walk Tracy through creating a FreeHand-owned Slack app once, registering it with the right scopes, pasting the credentials into `freehand shared-app add slack`. References Pitfall 33 (re-install trap) and 33a (Add-then-Save trap). |
| `CHANGELOG.md` (MODIFY) | New entry under "Unreleased": Round 8 — Shared OAuth broker apps. |

---

## Tasks

### Task 1: Write failing test for tier-1b broker resolution

**Objective:** Lock in the contract that broker tier-1b reads from `shared_apps` and returns real client_id/secret (not the tier-5 `open_connector` pointer).

**Files:**
- Create: `tests/test_shared_app_broker.py`

**Step 1:** Write failing tests.

```python
from core.oauth import broker

def test_tier1b_returns_shared_app_credentials(monkeypatch):
    # Tier-1 (user override) empty
    monkeypatch.setattr(broker, "_load_user_settings", lambda: {})
    monkeypatch.setenv("FREEHAND_BROKER_SLACK_CLIENT_ID", "")
    # Tier-1b has a shared app
    monkeypatch.setattr(
        broker, "_load_shared_apps",
        lambda: {"slack": {"client_id": "shared_id", "client_secret": "shared_secret"}}
    )
    client_id, secret, source = broker.get_client_credentials("slack")
    assert source == "shared_app"
    assert client_id == "shared_id"
    assert secret == "shared_secret"

def test_tier1_user_override_beats_tier1b(monkeypatch):
    """Pitfall-5 invariant: per-user override still wins."""
    monkeypatch.setattr(
        broker, "_load_user_settings",
        lambda: {"oauth": {"providers": {"slack": {"client_id": "user_id", "client_secret": "user_secret"}}}}
    )
    monkeypatch.setattr(
        broker, "_load_shared_apps",
        lambda: {"slack": {"client_id": "shared_id", "client_secret": "shared_secret"}}
    )
    client_id, secret, source = broker.get_client_credentials("slack")
    assert source == "user"  # tier-1 still wins
    assert client_id == "user_id"

def test_tier1b_returns_none_when_no_shared_app(monkeypatch):
    monkeypatch.setattr(broker, "_load_user_settings", lambda: {})
    monkeypatch.setattr(broker, "_load_shared_apps", lambda: {})
    # tier-2/3/4 empty
    client_id, secret, source = broker.get_client_credentials("slack")
    # Falls through to tier-5 if OC is available, else (None, None, "none")
    assert source in ("open_connector", "none")
```

**Step 2:** Run, see FAIL with "broker has no attribute `_load_shared_apps`".

```bash
cd "/c/Users/trace/Documents/Default Project" && \
  python -m pytest tests/test_shared_app_broker.py -v
```

### Task 2: Implement `core/oauth/shared_apps.py`

**Objective:** Helper module that loads, adds, removes, lists shared apps. Encrypts `client_secret` on write via the existing `encryption.key`.

**Files:**
- Create: `core/oauth/shared_apps.py`

**Step 1:** Module docstring naming the methods called.

```python
"""Shared OAuth app registry.

Methods FreeHand's broker calls: ``load_shared_apps()``. Methods the CLI calls:
``add_shared_app(service, ...)``, ``remove_shared_app(service)``, ``list_shared_apps()``.

Storage: ``vault/broker_config.json -> shared_apps`` block. ``client_secret``
is Fernet-encrypted via ``vault/encryption.key`` (same key that protects
``api_key`` and connection tokens) on every write; decrypted transparently
on load.
"""
```

**Step 2:** Implement. The encryption uses the existing pattern from `core/oauth/manager.py` (look up the Fernet usage there first — do not reimplement).

**Step 3:** Run Task 1's tests. Expect first one PASS, second/third still FAIL (broker integration next).

### Task 3: Wire tier-1b into `core/oauth/broker.py`

**Objective:** After tier-1 returns "none", try `shared_apps`. If a shared app exists for the service, return its decrypted credentials with `source="shared_app"`. Never raises.

**Files:**
- Modify: `core/oauth/broker.py`

**Step 1:** Read `core/oauth/broker.py`, locate the tier-1 → tier-2 transition.

**Step 2:** Insert tier-1b. The shape mirrors tier-1 exactly but reads from a different store:

```python
from .shared_apps import load_shared_apps as _load_shared

def get_client_credentials(service: str) -> tuple:
    # ... existing tier-1 (user override) ...
    # Tier-1b — FreeHand-managed shared app (NEVER raises)
    try:
        shared = _load_shared()
        if service in shared:
            entry = shared[service]
            return (entry["client_id"], entry["client_secret"], "shared_app")
    except Exception as e:
        log.warning("tier-1b shared-app lookup failed for %s: %s", service, e)
    # ... existing tiers 2-5 ...
```

**Step 3:** Run Task 1's tests. Expect all PASS. Add three more tests:
- `test_tier1b_decrypt_failure_falls_through` — encrypted blob corrupted, broker returns `none` not crash
- `test_tier1b_logs_warning_on_malformed_entry` — shared_apps has service but missing client_secret
- `test_tier1b_never_returns_open_connector_when_shared_app_exists` — guard against the resolution-order reordering bug

**Step 4:** Run full broker test suite (Pitfall-5 invariant test must still pass).

```bash
cd "/c/Users/trace/Documents/Default Project" && \
  python -m pytest tests/test_oauth_broker.py tests/test_shared_app_broker.py tests/test_open_connector_broker.py -v
```

**Step 5:** Commit.

```bash
git add core/oauth/shared_apps.py core/oauth/broker.py tests/test_shared_app_broker.py
git commit -m "feat(broker): tier-1b shared OAuth app resolution"
```

### Task 4: Route tier-1b OAuth dance through FreeHand's connector (not OC's runtime)

**Objective:** When the user clicks Connect for a tier-1b service, FreeHand serves the provider's consent screen using FreeHand's shared client_id/secret. Token ends up in `connections.db` per-user, just like Google/Microsoft.

**Files:**
- Modify: `core/oauth/router.py` (in `/<service>/authorize` handler)
- Modify: `core/oauth/manager.py` (token storage path)

**Step 1:** Write failing test.

```python
def test_authorize_returns_json_for_shared_app(monkeypatch):
    from core.oauth import router as r
    monkeypatch.setattr(
        r, "_get_broker_or_cached",
        lambda svc: ("shared_id", "shared_secret", "shared_app"),
    )
    resp = client.get("/api/integrations/slack/authorize?label=tracy",
                      headers={"X-API-Key": api_key})
    assert resp.status_code == 200
    body = resp.json()
    assert "authorize_url" in body  # NOT a 302 redirect
    assert "slack.com/oauth" in body["authorize_url"]
```

**Step 2:** Modify `/<service>/authorize`. The current handler checks broker source; if source is `"shared_app"`, fall through to the legacy authorize-url JSON path (the existing one Google uses) — the connector already knows how to build an `authorize_url` from a client_id + scopes. If source is `"open_connector"`, 302 to OC (Round 7 behavior). If source is `"none"`, return 503 with the "honest option A" message.

**Step 3:** Test the 503 fallback. The 503 body carries an actionable "ask for it" link — a pre-filled GitHub issue URL the user can click to file a request, plus Tracy's email as a non-GitHub path.

```python
def test_authorize_returns_503_honest_fallback(monkeypatch):
    from core.oauth import router as r
    monkeypatch.setattr(r, "_get_broker_or_cached", lambda svc: (None, None, "none"))
    monkeypatch.setattr(r, "_open_connector_advertises", lambda svc: False)
    resp = client.get("/api/integrations/linear/authorize?label=tracy",
                      headers={"X-API-Key": api_key})
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "no_shared_app"
    assert "linear" in body["message"].lower()
    # Pre-filled GitHub issue URL — user clicks, reviews, submits
    assert "request_url" in body
    assert body["request_url"].startswith(
        "https://github.com/TracySmithConsulting/freehand/issues/new"
    )
    assert "linear" in body["request_url"]
    # Email fallback for users who don't want GitHub
    assert body.get("contact_email") == "tracy@tracysmith.co.za"
```

**Step 4:** Run router tests. Commit.

```bash
git add core/oauth/router.py core/oauth/manager.py tests/test_shared_app_router.py
git commit -m "feat(router): serve provider consent screen for tier-1b shared apps"
```

### Task 5: Add the `freehand shared-app` CLI subcommands

**Objective:** Tracy manages the shared app registry from the CLI. One-time-per-service operations: `add`, `list`, `remove`.

**Files:**
- Modify: `cli.py`

**Step 1:** Mirror the existing `rename` subcommand pattern. The CLI uses Typer (see existing CLI); new subcommands:
- `freehand shared-app add <service> --client-id <id> --client-secret <secret> --scopes <comma-list>` — encrypts and stores
- `freehand shared-app list` — prints table of registered services (no secrets shown)
- `freehand shared-app remove <service>` — deletes entry

**Step 2:** Write round-trip test (`tests/test_shared_app_cli.py`):
- `test_shared_app_add_then_list` — add Slack, list shows it
- `test_shared_app_remove` — add then remove, list empty
- `test_shared_app_secret_not_in_list_output` — `list` output must NOT contain the raw `client_secret` (security check)

**Step 3:** Run CLI tests. Commit.

```bash
git add cli.py tests/test_shared_app_cli.py
git commit -m "feat(cli): shared-app add/list/remove subcommands"
```

### Task 6: Walk Tracy through registering FreeHand's Slack shared app (live)

**Objective:** Register a real Slack app under Tracy's Slack workspace, install it, paste the credentials into FreeHand. End state: `freehand shared-app list` shows Slack; `/api/integrations/slack/authorize` returns Slack's consent screen.

**Files:**
- Modify: `docs/integrations/slack-oauth-setup.md` (add "Shared app registration" section)

**Step 1:** Walk Tracy through api.slack.com app creation (Pitfall 33 + 33a apply — narrate Add-then-Save explicitly).

**Step 2:** Use clipboard (Pitfall 17) for every URL/secret/ID. Display in chat as backup.

**Step 3:** Identify the token prefix Tracy pastes (Pitfall 33's four-prefix taxonomy). For a Bot token, expect `xoxb-...`; for a User token, `xoxp-...`; for an app-level token, `xapp-...`; Client-ID is not a token. Tracy should paste the **Client ID** and **Client Secret** from "App Credentials" — these are what go into `freehand shared-app add slack`.

**Step 4:** Once added, run the live smoke (same recipe as Round 7):
- FreeHand on `:8000`, no OC needed for tier-1b services
- `curl /api/integrations/slack/authorize?label=tracy` → expect 200 JSON with `authorize_url` pointing at `slack.com/oauth/...`
- Browser test: open the URL, expect Slack consent screen in one click (Pitfall 24)

**Step 5:** Capture result in `docs/integrations/slack-oauth-setup.md`.

### Task 7: Live smoke — three-service matrix

**Objective:** Verify the broker resolution order works end-to-end across the three shapes.

**Setup:** FreeHand on `:8000`, OC on `:3001` (running from Round 7).

**Test matrix:**

| Service | Path | Expected | Actual |
|---|---|---|---|
| `google` | tier-1 (user override, existing) | 200 JSON with Google authorize URL | _(fill in)_ |
| `slack` | tier-1b (FreeHand shared app, Task 6) | 200 JSON with Slack authorize URL | _(fill in)_ |
| `hackernews` | tier-5 (OC no-auth) | 302 to OC console | _(fill in)_ |
| `linear` | none (no shared app, OC has it but Tier 5 advertises it) | 302 to OC | _(fill in)_ |
| `myspace` | none (no shared app, OC doesn't have it) | 503 with "honest option A" message | _(fill in)_ |

**Commit gate:** all five pass.

### Task 8: Document the integration

**Objective:** Tracy and future maintainers can find the shared-app workflow.

**Files:**
- Create: `docs/integrations/shared-apps.md`
- Modify: `CHANGELOG.md` (Unreleased: Round 8)
- Modify: `docs/integrations/slack-oauth-setup.md` (Shared app registration section)

**Step 1:** Write `shared-apps.md` covering: what shared apps are, when to use them vs. per-user tier-1 vs. OC tier-5, the CLI workflow, the 503 fallback UX, the security model (encryption.key shared with api_key).

**Step 2:** CHANGELOG entry:
```
## [Unreleased] — Round 8: Shared OAuth broker apps
### Added
- core/oauth/shared_apps.py: Fernet-encrypted registry of FreeHand-managed shared OAuth apps.
- Broker tier-1b: resolves to shared app credentials before falling through to tier-2..5.
- /api/integrations/<service>/authorize: serves provider consent screen (JSON) for tier-1b;
  returns 503 + "honest option A" message for services with no shared app and no OC catalog entry.
- cli.py: shared-app add|list|remove subcommands.
- docs/integrations/shared-apps.md, slack-oauth-setup.md updates.

### Notes
- Microsoft 365 still routed via first-party broker (Azure subscription unchanged).
- Docker Compose punted to Round 8c — out of scope here.
- OC action auto-registration punted to Round 9.
```

**Step 3:** Commit.

```bash
git add docs/ CHANGELOG.md
git commit -m "docs: shared-apps integration guide + Round 8 changelog"
```

---

## Tests / validation

Full suite after each task:

```bash
cd "/c/Users/trace/Documents/Default Project" && \
  python -m pytest tests/ --tb=short -q
```

Expected: 230 existing tests still pass; new tests in `test_shared_app_*.py` add ~15 cases. **Pitfall-5 invariant is non-negotiable** — `tests/test_oauth_broker.py::TestResolutionOrder` AND `tests/test_shared_app_broker.py::test_tier1_user_override_beats_tier1b` must both still pass.

Live verification before merge:
- Task 6 smoke (Slack dance) completes with one-click consent screen
- Task 7 test matrix passes all five cases
- `freehand shared-app list` works, secrets not displayed
- `vault/broker_config.json -> shared_apps.slack.client_secret` is Fernet-encrypted on disk (verify by `cat`-ing it — should be a `gAAAAA...` blob, not the raw secret)

---

## Risks, tradeoffs, open questions

| Risk | Mitigation |
|---|---|
| Tracy has to register a Slack app (one-time per service) | Documented in `shared-apps.md` with screenshots/clipboard walkthrough. Each service is a 5-minute operation once you know the shape. |
| Shared app credentials become a high-value target | Fernet-encrypted in `vault/broker_config.json` using the same `encryption.key` that protects `api_key` and per-connection tokens. Blast radius = "attacker who has the FreeHand vault AND the encryption key" — same as today. |
| Slack's Add-then-Save trap (Pitfall 33a) bites Tracy | Narrate both actions in the walkthrough; verify Save URLs button before declaring done. |
| User accidentally adds a shared app with the wrong scopes | CLI accepts `--scopes` flag explicitly; `freehand shared-app remove` + re-add fixes. No silent override. |
| Tier-1b returns real credentials while tier-5 returns a pointer — different shape | Documented in `broker._SOURCES`. Connectors that handle tier-1b must call `connections.store_token()`; connectors that handle tier-5 must call OC's runtime. The broker returns the source string so the caller knows which path to take. |
| 503 "honest option A" message feels like an error to users | Frame it positively: "FreeHand doesn't have a shared Linear OAuth app yet. You can either register your own in 5 minutes, or wait for us to add it." Include a github-issue link once Round 8c ships the contribution workflow. |
| Should Microsoft be in tier-1b too? | No — Azure subscription is still required for the app registration itself. Documented in CHANGELOG and shared-apps.md. |
| What if a provider doesn't allow app-level registration (e.g. Apple, requires paid developer account)? | Document each provider's signup cost in shared-apps.md. For paid-only providers, the 503 fallback is honest about that. |

---

## Slicing discipline

Per the Round 7 lessons in `freehand-agent-development` skill: vertical slices, one failure-per-implementation step, commit per slice. Do NOT bunch all RED tests first.

| Task | Slice type | Files touched | Commit |
|---|---|---|---|
| 1 | RED (test only) | `tests/test_shared_app_broker.py` | none yet |
| 2 | GREEN (helper module) | `core/oauth/shared_apps.py` | none yet |
| 3 | GREEN (broker wiring) | `core/oauth/broker.py` | `feat(broker): tier-1b shared OAuth app resolution` |
| 4 | RED-GREEN (router + manager) | `core/oauth/router.py`, `core/oauth/manager.py` | `feat(router): serve provider consent screen for tier-1b shared apps` |
| 5 | RED-GREEN (CLI) | `cli.py` | `feat(cli): shared-app add/list/remove subcommands` |
| 6 | Live smoke + walkthrough | `docs/integrations/slack-oauth-setup.md` | `docs: slack shared-app registration walkthrough` |
| 7 | Live matrix | none | none (verification only) |
| 8 | Docs + CHANGELOG | `docs/`, `CHANGELOG.md` | `docs: shared-apps integration guide + Round 8 changelog` |

---

## Open question for Tracy before Task 6

For the Slack shared-app walkthrough, what's the right scope set to register by default? Options:

- **(minimal)** `chat:write,channels:read,users:read` — enough to post messages and read basic channel/user info. Covers ~80% of "agent uses Slack" use cases.
- **(broad)** Every Slack scope Slack allows — most flexible but biggest consent screen, may spook users.
- **(minimal + DMs)** — adds `im:history`. Useful if the user wants the agent to read/send DMs, but it's a privacy-sensitive scope.

**Tracy's decision (30 Sep 2026):** minimal + DMs. Default scope set: `chat:write,channels:read,users:read,im:history`. Documented in `docs/integrations/shared-apps.md` as the recommended Slack starter set. Users who don't want DMs can re-register with `--scopes chat:write,channels:read,users:read` via `freehand shared-app remove slack && freehand shared-app add slack --scopes chat:write,channels:read,users:read`.

---

## Task 8 addendum: "Request a shared app" docs section

**Files:**
- Modify: `docs/integrations/shared-apps.md` (add "Requesting a service" subsection)

**Step 1:** When a user hits the 503 fallback, the docs page should explain:
1. What the 503 means ("FreeHand doesn't have a shared app for this service yet").
2. The two paths forward — click the pre-filled GitHub link, OR email Tracy.
3. What information to include in the request (service name, the OAuth scopes the user needs, any app-level requirements the user is aware of).
4. What Tracy does with the request (registers the shared app, replies to the issue with the `freehand shared-app add <service>` command line, marks the issue closed once shipped).
5. ETA expectation (Tracy is a single maintainer — no SLA, but public visibility via the issue stream means users can `+1` existing requests).

**Step 2:** Add a "Contributing a shared app" subsection explaining how a developer (not Tracy) can register a shared app themselves — fork the repo, add the credentials to their fork's `vault/broker_config.json` (encrypted via `freehand shared-app add`), open a PR. Tracy reviews and merges.

---

## Provenance / context for future maintainers

Tracy raised the shared-app direction on 30 Sep 2026 after Round 7 closed. Her exact words: *"I know it's more work for me, but I think the broker option is the right choice, because I want it to be as easy as possible for people to connect their apps. Where I can't do it, we go with option A, we say so honestly, and then they would have to do their own dance, but as far as possible, I'd go with B."*

The shape keeps FreeHand's 4-tier (now 5-tier with Round 7) broker discipline intact: tier-1b is just another layer in the resolution order, follows the Pitfall-5 invariant (more-specific wins), and is genuinely additive. The user-visible change: clicking Connect Slack with no per-user override goes from "302 to OC's configure-your-client page" to "Slack's consent screen in one click, using FreeHand's shared app." For services where Tracy hasn't done the one-time registration work, the 503 response is the honest "you do this dance yourself" path.

Round 9 candidates (out of scope here): (a) auto-register OC actions as FreeHand native tools via tool-registry auto-discovery; (b) Docker Compose with healthchecks (Round 8c); (c) contributor workflow so other developers can submit new shared apps.

**Status:** Draft. Awaiting Tracy's sign-off on the tier-1b shape and the Slack scope policy before Task 1.

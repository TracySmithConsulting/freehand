# Round 11 — Live smoke discovery (NOT verified live)

**Date:** 2026-10-05
**Status:** Plumbing + tests shipped, live verification deferred to Round 12

## What shipped in Round 11 (3 commits, +16 tests, 337 → 353 passing)

1. **`core/oauth/open_connector.py:get_provider_actions`** — real catalog probe.
   Closes the stub that Round 10's registry depended on. Returns the
   `authorizationOptions` list from `GET /v1/providers/<service>`. Empty
   list (not exception) on 404 / OC down / parse error.

2. **`core/tools/dispatch.py`** — `dispatch_oc_tool(name, args)` parses
   the `oc_<service>_<label>_<action>` triple, looks up the credential
   from `credential_store`, and routes the call through OC's MCP
   endpoint. Uniform `{ok, content|error}` envelope. 5 error codes:
   `malformed_name`, `no_credential`, `oc_unreachable`, `oc_error`,
   `exception`.

3. **`core/agent.py:execute_tool()`** — one new branch at the end of
   the dispatch chain. Any tool name starting with `oc_` routes to
   `dispatch_oc_tool`. Static tools (`read_docx`, `navigate`,
   `list_github_repos`, etc.) keep their explicit branches — Round 11
   doesn't touch them.

## What didn't work (live smoke, 04 Oct 2026)

Tracy started OpenConnector for the live smoke. Probing the running
OC with the runtime token revealed that the wire format my Round 11
plan assumed does not match OC's actual MCP interface.

### What my plan assumed

- OC's `/mcp` endpoint takes a JSON-RPC `tools/call` with
  `params.name = "<action_id>"` and `params.arguments = {...}`.
- The `action_id` is the `authorizationOptions[].id` string from
  the catalog, e.g. `"channels:read"` (Slack's OAuth scope name).
- FreeHand's `call_mcp_action(action, args)` does exactly this.

### What OC actually does

`POST /mcp` with `{"jsonrpc":"2.0","method":"tools/call","params":{"name":"...","arguments":{...}}}`
returns the OC **MCP tool list** (5 named tools), not the catalog's
authorizationOptions:

| Tool | What it does |
|---|---|
| `list_apps` | List every provider app in the catalog |
| `list_connections` | List configured provider connections |
| `search_actions` | Search actions by free-text query |
| `get_action_guide` | Get one action's full schema (capability, scopes, parameters) |
| `execute_action` | **Run one action by id against the provider's API** |

The action id format OC uses is `service.action_name` (e.g.
`slack.list_channels`), **not** the OAuth scope name (e.g.
`channels:read`). The shape is:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "execute_action",
    "arguments": {
      "actionId": "slack.list_channels",
      "input": { "limit": 50 }
    }
  }
}
```

OC also returned this capability for `slack.list_channels`:

```json
{
  "ok": true,
  "data": {
    "capability": {
      "operationType": "read",
      "execution": {
        "locallyExecutable": true,
        "needsCredential": true,
        "requiredAuthTypes": ["oauth2", "api_key"]
      },
      "requiredScopes": ["channels:read"],
      "policy": { "allowed": true }
    }
  }
}
```

### What this breaks

- **`core/oauth/open_connector.py:call_mcp_action`** sends
  `params.name = "<action_id>"` and the action_id is whatever
  our dispatch layer passes. For a Slack tool the action_id is
  `channels:read` (from `authorizationOptions[].id`). OC would
  return `{"error":{"code":"unknown_action"}}` because it expects
  `params.name = "execute_action"` and the action id nested
  inside `arguments.actionId`.
- **`core/tools/dispatch.py:dispatch_oc_tool`** injects the
  credential as `__credential__` and calls `call_mcp_action` with
  the action_id from the tool name's last segment (e.g.
  `oc_slack_default_channels:read` → `channels:read`). Neither
  the `__credential__` convention nor the action id format is
  what OC expects.
- **Action name translation** is needed: FreeHand's
  `authorizationOptions[].id` (e.g. `channels:read`, `chat:write`)
  must be mapped to OC's `execute_action.actionId` format
  (e.g. `slack.list_channels`, `slack.post_message`).

### The 1568-provider observation

The OC catalog now contains **1568 providers and 18345 actions**.
This is much larger than the authorizationOptions surface that
Round 10's `get_provider_actions` was designed to probe. The
proper Round 12 work needs to:

1. Switch the registry from `authorizationOptions[].risk` to the
   per-action `operationType` from `search_actions` / `get_action_guide`.
2. Add an action-name translation table (or per-service
   mapping) so `oc_<service>_<label>_<action_id>` from
   `authorizationOptions[].id` maps to the `service.action_name`
   that `execute_action` accepts.
3. Add the proper auth flow. `execute_action` runs against the
   "default connection" — Tracy's Slack credential needs to be
   registered as a named connection in OC's runtime, not just
   stored in FreeHand's `credential_store.json`. This is the
   actual onboarding round, not a one-line change.

## Why this is honest "ship" not "broken ship"

The Round 11 commits **don't break any existing FreeHand behavior**:

- Static tools (`read_docx`, `navigate`, `list_github_repos`,
  `create_github_issue`, etc.) keep their explicit branches.
- The new `oc_` branch is **additive**. If a tool name doesn't
  start with `oc_`, the elif chain is unchanged.
- The `oc_` branch returns a clean error envelope on
  `oc_unreachable` (OC down), `no_credential` (FreeHand's
  credential_store empty), or `oc_error` (OC returned an error).
  The LLM sees a clean error message, not a Python traceback.
- **All 353 tests pass.** No regression.

What's true: **a user who runs `freehand credential add slack
--token xoxb-...` then `freehand tools refresh` then asks the
agent to list Slack channels will get a clean `oc_error` envelope
back.** The agent won't list the channels. The dispatch layer
reached OC, OC rejected the call because of the wire format
mismatch, the error is surfaced cleanly.

That's not "shipped" in the sense of "works end-to-end" but it
is "shipped" in the sense of "code is in main, tests are green,
no regression, error handling is clean."

## What Round 12 needs to do (the real wire-up)

This is the deferred work. Per `multi-pr-feature-decomposition`
this is one feature with three layers (data translation, runtime
wire, UI feedback), so it can ship as a single PR or split into
prep + plumbing.

### Minimum viable Round 12 (estimate: 1-2 hours of work)

1. **`core/oauth/open_connector.py:execute_action(action_id, input)`**
   — new helper that does the right JSON-RPC call:
   `params.name = "execute_action"`, `params.arguments = {actionId, input, ...}`.
2. **`core/tools/dispatch.py:dispatch_oc_tool`** — call
   `execute_action` instead of `call_mcp_action`. Drop the
   `__credential__` injection (OC's runtime already has the
   credential via its `connections` table — Tracy registers
   Slack via OC's `connections/add` endpoint or the web
   console, NOT via FreeHand's `credential add`).
3. **Action-id translation.** Either:
   - A static mapping table (Round 12 minimum, error-prone at
     scale — 1568 providers × N actions each)
   - Or use OC's `search_actions` to look up the action id
     from a human-readable description at registry time
     (slower but more correct)
4. **Register Slack in OC's connections table** so the runtime
   has a default connection. This is the operational step that
   needs Tracy's hand on the keyboard.

### Stretch (estimate: half a day more)

5. **Drop `__credential__` from the dispatch layer entirely.**
   The credential lives in OC's runtime, not FreeHand's
   `credential_store`. The Round 11 `credential_store` is still
   useful for the OAuth dance (`freehand shared-app add`) but
   `credential add` for OC-backed services becomes a no-op
   (or a thin wrapper that delegates to OC's connections).
6. **Switch the registry from `authorizationOptions` to
   `search_actions`** so the action's `operationType` is the
   source of truth, not the OAuth scope's `risk` field.
7. **Round 11's Round 10.5 carryover** (still on the list):
   remove `core/oauth/providers/github_pat.py` and the static
   GitHub tool entries in `core/agent_config.py`. Now that
   `oc_github_*` is wired up, the static tools are redundant.

## Test status at end of Round 11

**353 passed, 2 skipped, 0 regressions.** Was 337 after Round 10 PR 2.

New tests in Round 11:
- `tests/test_open_connector_actions.py` (4) — `get_provider_actions`
  happy path, 404, no auth, OC down
- `tests/test_dispatch.py` (9) — name parsing (3-part, label with
  underscores, action ID with multiple colons, invalid prefix,
  too few parts) + dispatch result envelope (success, no-credential,
  OC error passthrough, action ID with special chars, fake-call
  coverage)
- `tests/test_dispatch_wiring.py` (3) — execute_tool routes `oc_`
  tools to dispatch, error envelope surfaces, static `read_docx`
  branch still in source

## Commits

- `84f24a3` — `feat(oauth): get_provider_actions for OC catalog probe`
- `91e25a3` — `feat(tools): dynamic OC tool dispatch module`
- `61c8085` — `feat(agent): route oc_ tool calls to dynamic OC dispatch`

All three are on `main` and ready to push.

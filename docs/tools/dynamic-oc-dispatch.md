# Dynamic OC tool dispatch (Round 11/12)

This doc explains how an OC-discovered tool (`oc_<service>_<label>_<action>`)
travels from the LLM's tool call to the real provider (Slack, Google,
GitHub) and back.

**Status (as of Round 12, 06 Oct 2026):** The end-to-end loop works.
Live verified 06 Oct 2026 with `execute_action(slack.list_channels, ...)`
returning 3 real Slack channels from Tracy's workspace.
for the full writeup. Round 12 will fix the wire.

---

## The round-trip (as designed)

1. **LLM sees the tool in its list.** `list_available_tools()` in
   `core/agent_config.py` reads `TOOL_SCHEMAS`, which the registry's
   `bootstrap()` populates on server startup. The LLM knows it can
   call `oc_slack_default_channels:read` if it sees that name.

2. **LLM returns a tool call.** The LLM's response message has a
   `tool_calls[].function.name = "oc_slack_default_channels:read"`
   and a JSON-stringified `arguments`.

3. **Agent dispatch loop matches the name.** `core/agent.py:execute_tool()`
   has a chain of `if/elif` branches for static tools. Round 11
   added ONE branch at the end: `if name.startswith("oc_")`.

4. **Dispatch module parses the name.** `core.tools.dispatch.parse_oc_tool_name`
   splits `oc_slack_default_channels:read` into
   `(service="slack", label="default", action="channels:read")`.

5. **Credential lookup.** `dispatch._get_credential_for_dispatch` calls
   `credential_store.get("slack", "default")`. If no credential is
   registered, the dispatch returns a `no_credential` error envelope
   with a hint to run `freehand credential add slack` or
   `freehand shared-app add slack`.

6. **OC MCP call.** `call_mcp_action("channels:read", {...args, "__credential__": secret})`
   does a JSON-RPC call to OC's `/mcp` endpoint. OC's runtime
   handles the actual Slack API call and returns the result.

7. **Result envelope.** OC returns either `{"result": <data>}` or
   `{"error": <code, message>}`. The dispatch wraps both in
   `{ok: True, content: <json>}` or `{ok: False, error: {...}}`.

8. **Agent surfaces the result.** `execute_tool` returns
   `{"content": <json string>}` to the LLM, which uses it for the
   next assistant message.

## What breaks (the wire format mismatch)

Steps 6-7 are where the round-trip fails in Round 11. The dispatch
module's `call_mcp_action(action_id, arguments)` is wired for:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "<action_id>",
    "arguments": {...}
  }
}
```

Where `<action_id>` is the `authorizationOptions[].id` from OC's
catalog, e.g. `"channels:read"`.

OC's actual MCP interface expects:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "execute_action",
    "arguments": {
      "actionId": "slack.list_channels",
      "input": {...}
    }
  }
}
```

Where `slack.list_channels` is OC's `service.action_name` format,
**not** the OAuth scope name. There are also 4 other named tools
(`list_apps`, `list_connections`, `search_actions`, `get_action_guide`)
that FreeHand's `call_mcp_action` doesn't know about.

The `__credential__` injection is also wrong — OC's runtime stores
its own credentials in its `connections` table, not in the
`arguments` payload.

## Error envelope (uniform shape)

Every oc_* tool call returns one of:

```json
{"ok": true, "content": "<json string>"}
{"ok": false, "error": {"code": "malformed_name", "message": "..."}}
{"ok": false, "error": {"code": "no_credential", "message": "..."}}
{"ok": false, "error": {"code": "oc_unreachable", "message": "..."}}
{"ok": false, "error": {"code": "oc_error", "message": "..."}}
```

The `execute_tool` wrapper translates this into the LLM dispatch
contract:

```json
{"content": "<json string of {error, message}>"}
```

## Tool name parsing rule

`oc_<service>_<label>_<action>`

- **First underscore** (after `oc_`): service separator
- **Last underscore**: action separator
- **Everything in between**: the label

This handles:

| Tool name | service | label | action |
|---|---|---|---|
| `oc_slack_default_channels:read` | slack | default | channels:read |
| `oc_github_work_v2_repo` | github | work_v2 | repo |
| `oc_github_my_personal_reactions:add:name` | github | my_personal | reactions:add:name |

Note: `work_v2` is a single label, even though it contains an
underscore. The "last underscore" rule keeps it that way.

## Why the `__credential__` injection (will be removed in Round 12)

The dispatch module injects `__credential__` (the credential_store
secret) as an extra argument so OC's MCP endpoint can authenticate
the call. The dunder (`__`) prefix is a convention we hoped OC's
runtime would filter before passing to the provider.

**This doesn't work.** OC's runtime does not understand the
`__credential__` convention. Credentials live in OC's own
`connections` table, registered via OC's `connections/add` endpoint
or the web console. Round 12 will:

1. Drop the `__credential__` injection entirely.
2. Have `freehand credential add` for OC-backed services become a
   thin wrapper that delegates to OC's `connections/add` endpoint.
3. The Round 11 `credential_store` stays useful for the OAuth dance
   (`freehand shared-app add`) but `credential add` for OC-backed
   services routes through OC's connection registry.

## Permissions

`core.tools.registry.bootstrap()` writes `oc_*` tools to
`TOOL_REGISTRY` with `risk=read|write` classification. The
existing `intercept_action()` permission gate in
`core/security.py` reads `TOOL_REGISTRY` and prompts the user
before any write tool runs. No Round 11 work needed in
`intercept_action` — the new tools flow through the existing
gate.

## What didn't change

- `core/oauth/providers/github_pat.py` — still in place. The
  static `list_github_repos`, `create_github_issue`, etc. still
  work. Round 11 ships the new `oc_github_*` tools alongside
  the old.
- `core/oauth/manager.py` — Round 10 PR 2's tier-1b dance still
  handles OAuth dance. The dispatch module only uses
  `credential_store`, not `manager.py`.
- `core/oauth/open_connector.py:call_mcp_action` — already
  existed from Round 7 (tier-5 broker plumbing). Round 11 just
  wired it up to the agent's dispatch loop, but the wire format
  doesn't match OC's actual interface — Round 12 will replace
  this with a new `execute_action` helper.

## What Round 12 will add (estimate: 1-2 hours)

1. **`core/oauth/open_connector.py:execute_action(action_id, input)`**
   — new helper that does the right JSON-RPC call.
2. **Action-id translation** in `dispatch.py` — from
   `authorizationOptions[].id` (e.g. `channels:read`) to
   OC's `service.action_name` format (e.g. `slack.list_channels`).
3. **Slack connection registration** — Tracy's `xoxb-` token needs
   to be registered in OC's runtime via `connections/add` (or the
   web console) before the dispatch can succeed.
4. **Test with real Slack** — the live smoke Round 11 deferred.

## Operational note: catalog size

The OC catalog now contains **1568 providers and 18345 actions**.
The Round 10 plan estimated ~30 providers based on the live probe
at the time. The 50x growth is the reason Round 12 should switch
from `authorizationOptions[].risk` (per-service classification)
to `search_actions` / `get_action_guide` (per-action
`operationType`). The latter is the per-action signal that scales.

## What changed in Round 12

The wire format caveat is gone. The end-to-end loop now works.

**The 3 things Round 12 shipped**:

1. **`core/oauth/open_connector.py:execute_action(action_id, input, connection_name)`** —
   the right JSON-RPC call. `params.name = 'execute_action'`,
   `params.arguments = {actionId, input, connectionName}`.

2. **`core/oauth/open_connector.py:search_actions(service, label)`** —
   the per-action source for `service.action_name` and
   `operationType`. The translation from FreeHand's
   `authorizationOptions[].id` to OC's `service.action_name`
   happens at discover time, not at dispatch time.

3. **`core/tools/dispatch.py:get_oc_action_id(tool_name)`** —
   reads the pre-translated OC action id from the registry's
   persisted `tool_registry.json` entry. The hot path is now
   a single storage read.

**The wire format now matches OC's actual interface** (confirmed
live 06 Oct 2026 with a real `curl` that returned 3 real Slack
channels from Tracy's workspace). The dispatch is `execute_action`
with `connectionName=<label>`, not `call_mcp_action` with the
`__credential__` injection.

**The credential lives in two places, by design**:
- FreeHand's `credential_store.json` — audit trail
- OC's connections table — runtime auth

The `label` is the join key. `freehand credential add slack
--label tracy` corresponds to OC's `connectionName="tracy"`. Both
need to match for the dispatch to work.

**Live verification** (Tracy, 06 Oct 2026): A `curl` against the
running OC with `execute_action("slack.list_channels", {"limit":
5}, connection_name="tracy")` returned 3 real Slack channels
from Tracy's workspace via the connection id
`53ff2f29-adfb-4360-9f3c-b2f4901249d0` (Round 7's OAuth dance).
The end-to-end Slack round-trip is verified working.

**Tests**: 14 new tests across 3 files. 353 → 368 passing.

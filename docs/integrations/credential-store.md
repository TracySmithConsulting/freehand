# Credential store + tool registry (Round 10 PR 2)

This doc explains how FreeHand handles credentials (API keys, OAuth
tokens) and how those credentials become LLM-visible tools.

## Two parallel registration paths

| Path | CLI | When to use |
|---|---|---|
| OAuth dance | `freehand shared-app add <svc>` | A service that needs the user to grant access on the provider's portal (Slack, Google, Microsoft). Tokens have `expires_in` and get refreshed. |
| Direct API key | `freehand credential add <svc> --token <key>` | A service that just needs a PAT or API key. The user pastes the key once and the credential is Fernet-encrypted. No dance, no refresh. |

Both paths write to the same underlying store: `vault/credential_store.json`.
The CLI subcommands are sibling subcommands of `freehand` — the user
picks the path that matches the service.

## Credential store: per-(service, label) namespace

Each credential is keyed by `(service, label)`. Two accounts for the
same service get two different labels and coexist:

```bash
freehand credential add github --label work     --token ghp_AAA
freehand credential add github --label personal --token ghp_BBB

freehand credential list
# Service     Label          Auth Type   Registered
# github      work           api_key     tracy
# github      personal       api_key     tracy
```

Default label is `default` if `--label` is omitted. The `auth_type`
column shows `api_key` (raw token) or `oauth` (full token_data dict,
used by the OAuth dance). Secrets are Fernet-encrypted on write; the
list output **never** displays them.

## From credentials to tools

Once credentials exist, the tool registry probes OpenConnector for
each `(service, label)` and turns OC's action catalog into LLM-visible
tools. The registry is the bridge — it doesn't talk to providers
directly, it just translates OC's catalog into schemas the LLM
already understands.

```bash
freehand credential add slack --token xoxb-...   # add credential
freehand tools refresh                            # probe OC, register tools
freehand tools list                               # see what's active
```

The `tools refresh` step is the only place OC is contacted. The
LLM dispatch loop reads the in-memory `TOOL_SCHEMAS` dict that the
registry populates.

## Risk-based classification

OC tags each action with `risk: standard | sensitive | destructive`.
The registry uses that field to decide what's safe to auto-register
and what needs explicit opt-in:

| OC risk | Auto-register? | What to do |
|---|---|---|
| `standard` | Yes (read tool) | Nothing — `tools refresh` handles it |
| `sensitive` | No | `freehand tools enable-writes <service>` to opt in |
| `destructive` | No | Same as sensitive — explicit opt-in |

Slack's `defaultSelected` field is **not** a safety signal — it's
OC's UX flag for the OAuth consent screen. Slack uses
`risk=sensitive` for read actions like `channels:history`. We don't
auto-register those; Tracy opts in via `enable-writes`.

The reasoning: at the agent's default tier, only "low-blast-radius
reads" should be visible. Sending a message (chat:write), reading
private channel history (channels:history), deleting a message
(chat:delete) — those all warrant a conscious decision. `enable-writes`
is the prompt for that decision.

## Tool naming

Every OC-discovered tool is named `oc_<service>_<label>_<action>`.
The label is always present, even for single-credential services
where it defaults to `default`. This uniform shape lets the LLM
learn one pattern and recognise any tool:

```
oc_slack_default_channels:read
oc_github_work_repo
oc_github_personal_repo
oc_google_work_drive:list
```

Action IDs preserve their case (including colons) so OC's
`channels:read` becomes `oc_slack_default_channels:read` rather
than `oc_slack_default_channels_read`.

## Schema format

Schemas use the OpenAI function-calling shape, matching the
existing `core/agent_config.py` baseline:

```json
{
  "type": "function",
  "function": {
    "name": "oc_slack_default_channels:read",
    "description": "Public channels: List public Slack channels and read their metadata.",
    "parameters": {
      "type": "object",
      "properties": {
        "channel": {"type": "string", "description": "Channel ID"}
      },
      "required": ["channel"]
    }
  }
}
```

The LLM dispatch loop already understands this shape; the registry
just injects new entries into the same dict.

## Persistence

Two on-disk files:

- `vault/credential_store.json` — Fernet-encrypted credentials,
  keyed by `(service, label)`. Sentinel `{"_migrated": true}` runs
  the legacy migration once.
- `vault/tool_registry.json` — currently-active tools. Each entry
  has `service`, `label`, `tool_name`, `schema`, and the OC `risk`
  field. Re-read by `registry.bootstrap()` at server startup so
  tools persist across FreeHand restarts.

Both files use the same Fernet key (`vault/encryption.key`) as
the existing `core/oauth/manager.py` and `core/oauth/shared_apps.py`.
Single source of truth for encryption.

## Server startup

`server.py:startup_event()` runs three migration steps on first boot:

1. `migrate_legacy_root_db()` — moves Round 9's `agent.db` into
   `vault/agent.db` (PR 1's job).
2. `migrate_legacy_connections_db()` — moves Round 5-9's
   `vault/connections.db` rows into `vault/credential_store.json`
   as `auth_type="oauth"`. Idempotent. Skips rows that already
   exist in the new store.
3. `registry.bootstrap()` — reads `vault/tool_registry.json` and
   injects stored tools into `core.agent_config.TOOL_SCHEMAS` +
   `TOOL_REGISTRY`. First boot with no file is a no-op.

After startup, `list_available_tools()` includes both the static
baseline tools (read_docx, navigate, click, etc.) and the
OC-discovered tools (oc_slack_*, oc_github_*, etc.).

## CLI reference

```bash
# ── Credentials ────────────────────────────────────────────────
freehand credential add <service> --token <key> [--label <name>]
freehand credential list
freehand credential remove <service> [--label <name>]
freehand credential rename <service> --from <old> --to <new>

# ── Tools ──────────────────────────────────────────────────────
freehand tools refresh
freehand tools list [service]
freehand tools enable-writes <service> [--label <name>]
freehand tools disable <service> [--label <name>]
```

## What's not done yet (Round 11+)

- **`core/oauth/providers/github_pat.py`** and the static
  `list_github_repos` / `create_github_issue` etc. in
  `core/agent_config.py` are still in place. PR 2 ships the new
  registry alongside the old; Round 11 will rename / re-namespace
  the dispatch loop to use the registry exclusively.
- **OC provider request**: Currently the registry reads
  `get_provider_actions(service, label)` which returns the
  authorizationOptions. When OC ships its full action catalog
  endpoint (`/v1/providers/<svc>/actions`), the registry will
  switch over — no user-facing change.
- **Per-tier gating**: `risk=write` tools go through
  `intercept_action()` like every other write. Round 11 will
  add a separate "tier-1c" gate so the agent has to confirm
  before invoking an enabled write tool.

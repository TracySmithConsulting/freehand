# FreeHand — Round 10: Agent tools from OpenConnector

> **For Hermes:** Plan only. Do not execute until Tracy signs off.
> Reload `freehand-agent-development` skill before any implementation turn — every OAuth/broker change deserves the 4-round review discipline.

**Goal:** Two deliverables in one round, shipped as two PRs.
- **PR 1: `agent.db` → `vault/` consolidation.** Single persistent state directory. Closes the Docker bind-mount UID issue — Linux compose-up runs FreeHand non-root without workarounds.
- **PR 2: Tool discovery + credential store.** Auto-register OC's action schemas as FreeHand tools. Two ways to wire a service: `freehand shared-app add <svc>` (OAuth) or `freehand credential add <svc> --token ...` (API key / PAT). Per-service, per-label credentials (e.g. two Google accounts under different labels). Read-only by default; explicit opt-in for writes. ~1500 lines, ~8 commits.

**User requirement (carried from Rounds 8 + 9):** Tracy's stated goal was *"I want it to be as easy as possible for people to connect their apps."* Round 8 made the OAuth dance friction-free. Round 9 added Slack connector end-to-end. Round 10 closes the loop with **credentials as first-class objects** (multi-account support) and **tools that surface from those credentials** — so the agent can actually *use* what the user has connected, not just store the credentials.

**Architecture:**

The broker stays where it is — Round 8's 5-tier resolution (user override / shared_app / broker_config / oc / none). What changes is **what each tier exposes to the tool layer**. The new `core/tools/registry.py` module is the bridge: it discovers OC's action catalog for a service, classifies each action as read or write, registers the read-actions as `oc_<service>[_<label>_]<action>` tools, and holds the writes in a "to enable" list.

**Tool vocabulary the LLM sees** (final, after all layers):
- `read_docx`, `gpt_call`, etc. — FreeHand's own Python tools (Layer 0, always present)
- `oc_<service>_<action>` — Tier-1b / Tier-5 services with read access (Layer 1)
- `oc_<service>_<label>_<action>` — Multi-credential services (Tracy has two Google accounts; LLM picks the label)
- `oc_<service>_<action>` for writes — only after `freehand tools enable-writes <service>`
- `*` for any of the above — explicitly disabled services (freehand tools disable) just don't appear in the schema list

**Tech Stack:** Python 3.11+, FastAPI (already in tree), OpenConnector via existing client (`core/oauth/open_connector.py`), `cryptography.fernet` for encryption (already in tree). No new runtime deps.

---

## Why this shape (and not the alternatives)

| Alternative | Why we reject it |
|---|---|
| **Auto-list all 18k OC actions unconditionally** | Context window collapse. 18k schemas ≈ 3.6M tokens; doesn't fit Claude/most models. Selection quality collapses past ~30-50 tools. Audit surface explodes. |
| **No per-credential labels; single credential per service** | Doesn't fit "two Google accounts" use case Tracy raised. Round 8's OAuth dance already supports labels — losing labels for the new path creates a worse experience for one flow than the other. |
| **Keep `core/oauth/providers/github_pat.py` as a FreeHand connector** | OC has full GitHub coverage (both OAuth2 and PAT auth types). A FreeHand first-party connector duplicates OC's work and adds ~95 lines of code for zero capability gain. Routing GitHub through OC is the natural choice and matches the Round 8 broker invariant: "FreeHand's first-party provider code is for OAuth-dance connectors; anything else goes through OC." |
| **Async periodic background refresh of OC catalog** | Local agent doesn't need it. Adds complexity. Boot + `refresh` subcommand covers the cases. |
| **One big commit for PR 2** | Vertical-slice discipline: each sub-task independently GREEN, reviewable in 10-15 min. Each commit a self-contained unit. The Round 7/8/9 pattern was 5-8 commits per round. |

**What we keep:** the 5-tier broker Round 8 shipped. **What we add:** tool discovery layer + credential store + multi-credential CLI + Docker bind-mount cleanup.

---

## Phase 0 — Foundational decisions (locked)

**0.1 Migration of legacy `vault/connections.db` Slack row.** Reads the existing row, writes it to `vault/credential_store.json` under `(service="slack", label="tracy")`, marks it migrated. One-time, idempotent — running it twice is a no-op. Skippable via `--skip-migration` flag. Round 8's broker falls back to the OAuth flow if the credential_store doesn't have the entry.

**0.2 Label is mandatory; defaults to `"default"` if omitted.** Aligns with Round 8's OAuth label field. `(service, label)` is the primary key for `credential_store` and for the `find_classification` / `find_tool_enabled` lists.

**0.3 Tool naming with labels.** When a service has multiple labels, tool names are prefixed: `oc_<service>_<label>_<action>`. When a service has a single credential, no prefix (`oc_<service>_<action>`). The LLM sees a deterministic schema either way.

**0.4 Risk classification from OC's `authorizationOptions[].risk` field.** OC's catalog already tags each authorization option as `standard` (read) or `sensitive` (write). Round 10 respects this — `standard` actions become available tools immediately; `sensitive` actions stay on the "to enable" list until `freehand tools enable-writes <service>` runs.

**0.5 No new runtime deps.** All implementation uses `pyproject.toml`'s existing `dependencies`. The `core/tools/` directory gains new modules; `pyproject.toml` does not.

**0.6 What we deliberately do NOT do in this round**

- Auto-register OC actions as tools **on first boot only** for services with credentials. `freehand tools refresh` re-runs it on demand.
- Tools for actions that have no OC catalog entry (truly custom providers). Out of scope.
- Email connector (IMAP/SMTP) — different auth shape, Round 11 candidate.
- Webhook / event subscription (Slack events, etc.) — separate code path, Round 11+ candidate.
- Auto-discovery of services that don't appear in OC's catalog. Round 8's 503 honest-option-A still handles these.
- Revoking a credential's stored tools if the credential gets revoked. The next `auth.test` call returns false; we don't proactively deregister. Round 11 candidate.

---

## Files likely to change

### PR 1 — `agent.db` → `vault/` consolidation

| File | Change |
|---|---|
| `core/database.py` | `DB_PATH` reads from `vault/agent.db` instead of project root. Adds `migrate_legacy_root_db()` that moves existing on-disk DB into vault/ on first import. |
| `Dockerfile` | Single vault bind mount — `./vault:/app/vault`. Drop the explicit `agent.db` mount. |
| `compose.yml` | Same. |
| `.dockerignore` | Remove the `*.db` exclusion for `agent.db` (now inside vault, which is already excluded via `vault/`). |
| `tests/test_database.py` (new) | Migration idempotency, DB_PATH constant, WAL sidecar journal handle. |
| `docs/deployment/docker-compose.md` | Update volume mounts table. |

### PR 2 — Tool discovery + credential store + multi-account CLI

| File | Change |
|---|---|
| `core/tools/__init__.py` (new) | `registry.py` lives here |
| `core/tools/registry.py` (new) | `discover_tools(service)`, `register_tools_classification(tools)`, `list_tools()`, `enable_writes(service)`, `disable(service)`, `refresh()`. Auto-classifies via OC's `risk` field. Generates schemas in OpenAI function-calling style (matches existing `TOOL_SCHEMAS` shape). |
| `core/oauth/credential_store.py` (new) | `add(service, label, secret, auth_type)`, `list_all()`, `get(service, label)`, `remove(service, label)`, `rename(service, old, new)`. Stores in `vault/credential_store.json` as `{auth_type, secret_enc, ...metadata}`. Fernet-encrypted via the existing `encryption.key`. `(service, label)` primary key. |
| `core/oauth/manager.py` | Add `migrate_legacy_connections_db()` — reads `vault/connections.db` rows, converts to `credential_store.json` entries. Idempotent, one-time. |
| `core/agent_config.py` | `TOOL_REGISTRY` and `TOOL_SCHEMAS` become dynamic. `list_available_tools()` queries the registry for current enabled tools. The hardcoded 35-tool dict stays as the "Layer 0" baseline; discovered tools append. |
| `core/oauth/open_connector.py` | Extend with `get_provider_actions(service)` returning action catalog. Stdlib urllib per Pitfall 32. |
| `cli.py` | New subcommands: `credential add/list/remove/rename`, `tools list/enable/enable-writes/disable/refresh`. Remove `github_pat.py` special-case in `connect()`. |
| `core/oauth/providers/__init__.py` | Remove `"github"` from `CONNECTORS`. The freehand GitHub connector is gone — OC has full GitHub coverage. |
| `core/oauth/providers/github_pat.py` | **DELETE.** Its job is OC's now. |
| `core/oauth/router.py` | No code change. Broker tier-5 already returns "this service is on OC" — the new tool layer reads that and surfaces tools. |
| `tests/test_credential_store.py` (new) | CRUD round-trip, label collision, encryption, list filtered by service. |
| `tests/test_tool_registry.py` (new) | Discovery, classification, schema generation, multi-label naming. |
| `tests/test_tool_cli.py` (new) | `credential add/list/remove/rename`, `tools list/enable/enable-writes/disable/refresh` round-trip. |
| `tests/test_migrate_legacy.py` (new) | Migration idempotency, partial-migration recovery, `--skip-migration` flag. |
| `tests/test_oauth_broker.py` | Update existing tests if Round 10 changes broker surface (likely no change — broker is read by registry, not modified). |
| `docs/tools/README.md` (new) | Layer model documentation (Layer 0/1/2/3), CLI reference, troubleshooting, when NOT to use. |
| `docs/integrations/shared-apps.md` | Update to mention Round 10's tool discovery happens automatically on `shared-app add`. |
| `CHANGELOG.md` | Round 10 entry covering both PRs. |

---

## Tasks

### PR 1 — `agent.db` → `vault/` consolidation

#### Task 1.1: Write failing test for `DB_PATH` and migration

**Files:** `tests/test_database.py` (new)

**Tests:**
- `DB_PATH` resolves to `vault/agent.db`, not `<project_root>/agent.db`
- `migrate_legacy_root_db()` moves an existing on-disk DB at the legacy path into `vault/agent.db`
- Migration is idempotent — running twice doesn't double-move the migration
- Migration is safe on empty vault dir (creates vault/, then runs)
- WAL sidecar files (`agent.db-wal`, `agent.db-shm`) move with the main file

**Expected RED:** `AttributeError: module 'core.database' has no attribute 'migrate_legacy_root_db'`; `AssertionError: DB_PATH does not point into vault/`.

#### Task 1.2: Implement `core/database.py` changes

**Files:** `core/database.py` (modify)

**Changes:**
- `DB_PATH = Path(__file__).parent.parent / "vault" / "agent.db"`
- New `migrate_legacy_root_db()`: checks for `<project_root>/agent.db`, moves to `vault/agent.db` with WAL sidecars, returns boolean
- `init_db()` calls `migrate_legacy_root_db()` once at startup (idempotent — no-op if already moved)

**Commit 1.1:** `feat(storage): consolidate agent.db into vault/`

#### Task 1.3: Update Dockerfile + compose.yml to drop the agent.db bind mount

**Files:** `Dockerfile` (modify), `compose.yml` (modify), `.dockerignore` (modify)

**Changes:**
- Dockerfile comment update ("Two persistent host paths" → "One persistent host path" — `./vault`)
- compose.yml: remove the `agent.db` bind-mount volume
- `.dockerignore`: drop the `agent.db` line (already excluded via `vault/`)

**Commit 1.2:** `chore(deploy): single vault mount — drop agent.db bind-mount`

#### Task 1.4: Live smoke — Linux compose-up runs FreeHand non-root

**Operational verification only:** build with `UID=1000 GID=1000`, run with `FREEHAND_USER=1000:1000`, confirm both containers healthy, run 5-service matrix from Round 9.

**Commit 1.3:** (none — verification only)

#### Task 1.5: Update `docs/deployment/docker-compose.md`

**Files:** `docs/deployment/docker-compose.md` (modify)

**Changes:**
- Volume mounts table: drop `agent.db` row, simplify vault row
- Update "Known issue: Windows bind-mount UID mapping" — note that the workaround is no longer needed for Linux hosts

**Commit 1.4:** `docs(deploy): vault consolidates agent.db — single mount`

#### Task 1.6: CHANGELOG entry for PR 1

**Files:** `CHANGELOG.md` (modify)

**Commit 1.5:** `privatecontent: Round 10.1 vault consolidation + non-root compose`

---

### PR 2 — Tool discovery + credential store

#### Task 2.1: Write failing test for credential store CRUD

**Files:** `tests/test_credential_store.py` (new)

**Tests:**
- `add(slack, tracy, secret, oauth)` encrypts and stores; `get(slack, tracy)` round-trips
- `add(slack, tracy, ...)` twice with same label is a no-op (or raises?)
- `list_all()` returns all credentials, secrets NEVER in output
- `remove(slack, tracy)` deletes; subsequent `get` returns None
- `rename(slack, tracy, shazacin)` moves the row; subsequent `get(slack, tracy)` returns None, `get(slack, shazacin)` returns the secret
- Fernet encryption: `vault/credential_store.json` does NOT contain plaintext `secret`

**Expected RED:** `ModuleNotFoundError: No module named 'core.oauth.credential_store'`

#### Task 2.2: Implement credential store

**Files:** `core/oauth/credential_store.py` (new)

**Public surface:**
- `add(service, label, secret, auth_type) -> dict`
- `list_all() -> List[dict]` — no secrets in output
- `get(service, label) -> Optional[dict]` — returns decrypted secret + metadata
- `remove(service, label) -> bool`
- `rename(service, old_label, new_label) -> bool`

**Storage layout** (`vault/credential_store.json`):
```json
{
  "version": 1,
  "credentials": {
    "slack": {
      "tracy": {
        "auth_type": "oauth",
        "secret_enc": "gAAAAA...",
        "scopes": ["chat:write", "channels:read"],
        "registered_at": "2026-10-02T..."
      }
    },
    "github": {
      "work": {
        "auth_type": "pat",
        "secret_enc": "gAAAAA...",
        "registered_at": "2026-10-02T..."
      },
      "personal": {
        "auth_type": "pat",
        "secret_enc": "gAAAAA...",
        "registered_at": "2026-10-02T..."
      }
    }
  }
}
```

**Commit 2.1:** `feat(oauth): credential store with multi-credential support`

#### Task 2.3: Migrate `connections.db` slack row on first boot

**Files:** `core/oauth/manager.py` (modify), `tests/test_migrate_legacy.py` (new)

**Changes:**
- Add `migrate_legacy_connections_db()` — reads `vault/connections.db`, finds rows that aren't already in `credential_store`, writes them with `auth_type="oauth"` and `label="<existing_label>"`
- Idempotent — checks for a `_migrated: true` marker in `vault/connections.json` (or similar) so it doesn't re-run
- Wired into `init_db()` after `migrate_legacy_root_db()` (from PR 1)

**Commit 2.2:** `feat(oauth): migrate legacy connections.db to credential_store`

#### Task 2.4: CLI subcommands for credentials

**Files:** `cli.py` (modify), `tests/test_tool_cli.py` (new)

**Subcommands:**
- `freehand credential add <service> --label <name> [--token <secret>]` — auth_type derived from `authTypes` in OC's catalog (oauth vs api_key); token prompt hidden by default
- `freehand credential list` — table of (service, label, auth_type, registered_at)
- `freehand credential remove <service> --label <name>`
- `freehand credential rename <service> --from <old> --to <new>`

**Commit 2.3:** `feat(cli): credential add/list/remove/rename subcommands`

#### Task 2.5: Write failing test for tool discovery

**Files:** `tests/test_tool_registry.py` (new)

**Tests:**
- `discover_tools("slack")` returns OC's action list for slack
- Classification: action with `risk=standard` is registered as read tool; `risk=sensitive` is NOT registered
- Tool naming: single credential → `oc_slack_<action>`; multi-credential → `oc_slack_<label>_<action>`
- Schema generation: returns OpenAI-style function-calling schema with `parameters` and `required` fields
- Refresh re-probes OC and adds/removes tools accordingly
- Tools for services without credentials don't appear (even if they would have read access via OC's no-auth catalog)

**Expected RED:** `ModuleNotFoundError: No module named 'core.tools.registry'`

#### Task 2.6: Implement tool discovery + registry

**Files:** `core/tools/registry.py` (new), `core/oauth/open_connector.py` (modify), `core/agent_config.py` (modify)

**`core/oauth/open_connector.py` additions:**
- `get_provider_actions(service_id) -> List[dict]` — returns OC's action catalog for one service, including risk and required scopes
- `get_provider_no_auth_services() -> List[str]` — services OC knows that don't require credentials (HackerNews, arXiv, etc.)

**`core/tools/registry.py` design:**
```python
def discover_tools(service: str, label: str = None) -> List[Tool]:
    """Probe OC for <service>'s action catalog. Register each as
    oc_<service>[_<label>_]<action>. Classify via risk field."""

def classify_action(action: dict) -> Literal["read", "write"]:
    """OC's risk field is 'standard' (read) or 'sensitive' (write).
    Returns 'write' for sensitive, 'read' for everything else."""

def enable_writes(service: str, label: str = None) -> int:
    """Move write-actions for the service from 'to enable' to active."""

def disable(service: str, label: str = None) -> int:
    """Remove all tools for (service, label)."""

def refresh() -> dict:
    """Re-probe OC for every credentialed service. Returns
    {added: N, removed: M, updated: K}."""

def list_tools(service: str = None, label: str = None) -> List[Tool]:
    """Filterable list of currently-active tools. No secrets."""

def _build_tool_name(service: str, label: str, action: str) -> str:
    """oc_<service>_<action> for single-credential,
    oc_<service>_<label>_<action> for multi-credential."""
```

**`core/agent_config.py` changes:**
- `TOOL_REGISTRY` becomes a dict that's loaded fresh on demand from the registry, not hardcoded at import
- `TOOL_SCHEMAS` similarly dynamic
- `list_available_tools()` calls `registry.list_tools()` and merges with the Layer 0 baseline
- The hardcoded baseline stays as the "always-present" set (read_docx, etc.); discovered tools append

**Commit 2.4:** `feat(tools): registry discovers OC actions as FreeHand tools`

#### Task 2.7: CLI subcommands for tools

**Files:** `cli.py` (modify), `tests/test_tool_cli.py` (modify, expand)

**Subcommands:**
- `freehand tools list [--service <name>]` — table of (service, label, action, risk, status)
- `freehand tools enable-writes <service> [--label <name>]` — flips writes on
- `freehand tools disable <service> [--label <name>]` — removes tools for that service/label
- `freehand tools refresh` — re-probes OC for every credentialed service

**Commit 2.5:** `feat(cli): tools list/enable-writes/disable/refresh subcommands`

#### Task 2.8: Wire `shared-app add` and `credential add` to trigger discovery

**Files:** `cli.py` (modify), `tests/test_tool_cli.py` (modify, expand)

**Behavior:**
- After `freehand shared-app add <svc>` succeeds, the CLI calls `registry.discover_tools(svc, label)` so the LLM sees the tools as soon as the OAuth flow completes
- After `freehand credential add <svc>` succeeds, same — discover tools

**Test:** `test_shared_app_add_triggers_tool_discovery` — assert that after `shared-app add`, `registry.list_tools("slack")` returns the read tools.

**Commit 2.6:** `feat(tools): shared-app add triggers tool discovery`

#### Task 2.9: Remove `github_pat.py` and the CLI GitHub special-case

**Files:** `core/oauth/providers/github_pat.py` (delete), `core/oauth/providers/__init__.py` (modify), `cli.py` (modify), `tests/test_github_pat.py` (delete), `tests/test_tier5_router_redirect.py` (verify still passes)

**Changes:**
- Delete `github_pat.py`
- Remove `"github": GitHubPATConnector` from `CONNECTORS`
- Remove the `service == "github"` block from `cli.py:connect()` — users now use `freehand credential add github`
- Delete or update tests that reference `GitHubPATConnector`

**Commit 2.7:** `refactor(oauth): route GitHub through OC, drop first-party PAT connector`

#### Task 2.10: Docs

**Files:** `docs/tools/README.md` (new), `docs/integrations/shared-apps.md` (modify), `docs/integrations/slack-oauth-setup.md` (modify if needed), `CHANGELOG.md` (modify)

**`docs/tools/README.md` content:**
- Layer model: Layer 0 (FreeHand built-in), Layer 1 (credentialed services), Layer 2 (no-auth public services), Layer 3 (requestable via 503)
- CLI reference: every subcommand with examples
- Multi-credential walkthrough (`freehand credential add github --token *** --label work` + `--label personal`)
- Troubleshooting: common errors (credential fails to validate, tool calls return 401, refresh didn't pick up a new OC action)
- When NOT to use: services requiring custom code paths, services without OC catalog entries, etc.

**`docs/integrations/shared-apps.md` update:**
- Note that `shared-app add <svc>` now triggers automatic tool discovery
- The "Slack tier-1b end-to-end" example shows the new tool names appearing after registration

**CHANGELOG entry** — Round 10 (PR 1 + PR 2):
```
## [Unreleased] — 2026-10-02

### Added — Round 10: Vault consolidation + agent tools from OpenConnector

- **PR 1: vault/ consolidation** — `agent.db` lives at `vault/agent.db`
  instead of project root. Single Docker bind mount (`./vault:/app/vault`).
  Linux compose-up runs FreeHand non-root (`FREEHAND_USER=${UID}:${GID}`)
  without the Round 9 Windows compatibility workaround.
  Migration of existing on-disk data is one-time, idempotent, runs on
  first boot.

- **PR 2: Tool discovery + credential store** — FreeHand now exposes
  OpenConnector's action catalog as its own tools. Two ways to wire a
  service: `freehand shared-app add <svc>` (OAuth) or `freehand
  credential add <svc> --token ...` (API key / PAT). Per-service,
  per-label credentials (e.g. two Google accounts under different
  labels). Tools are auto-classified via OC's `risk` field — standard
  actions become available immediately, sensitive (write) actions
  stay disabled until `freehand tools enable-writes <svc>` runs.

  New CLIs:
  - `freehand credential add/list/remove/rename`
  - `freehand tools list/enable-writes/disable/refresh`

  Round 8's `vault/connections.db` row for slack is preserved via
  one-time migration into `vault/credential_store.json`.

### Removed

- **`core/oauth/providers/github_pat.py`** — its job is OC's now. OC
  has full GitHub coverage (OAuth + PAT auth types). FreeHand loses
  ~95 lines of first-party code; gains zero loss in capability.

### Notes

- **Email (IMAP/SMTP)**: out of scope — different auth shape.
  Round 11 candidate.
- **Webhook / event subscriptions**: out of scope. Round 11+.
- **No runtime deps added**: all implementation uses
  `pyproject.toml`'s existing dependencies.
- **Test count**: net ~20 new tests across PR 1 (~5) and PR 2
  (~15). 268 → ~290 passing, 0 regressions.
```

**Commit 2.8:** `docs: Round 10 vault consolidation + tool discovery`

---

## Tests / validation

Full suite after each task:

```bash
cd "/c/Users/trace/Documents/Default Project" && \
  python -m pytest tests/ --tb=short -q
```

Expected: 268 → ~290 passing, 0 regressions.

PR 1 live verification:
- Build image with `UID=1000 GID=1000`, run with `FREEHAND_USER=1000:1000`
- Both containers healthy, 5-service matrix from Round 9 passes
- Vault has both `agent.db` and `connections.db` (after PR 2 migrates the slack row)
- No `agent.db` at the project root

PR 2 live verification:
- `freehand credential add github --token *** --label work` → success
- `freehand credential list` → shows the entry, secret never displayed
- `freehand tools list --service github` → shows OC's GitHub read tools as `oc_github_work_*`
- `freehand tools enable-writes github` → adds the write tools
- `freehand tools disable github` → clears them
- `freehand credential add github --token *** --label personal` → multi-credential case
- `freehand tools list` → shows both `oc_github_work_*` and `oc_github_personal_*`
- Existing slack connection (migrated) still tests in listing — `freehand test slack --label tracy`

---

## Risks, tradeoffs, open questions

| Risk | Mitigation |
|---|---|
| **OC catalog shape changes** | Pitfall 30 — capture live response, pin fixture shape before parsing. The new `get_provider_actions()` parses against pinned fixture, not imagination. |
| **Migration corrupts vault** | Two protections: (1) `migrate_legacy_connections_db()` reads from `vault/connections.db` and writes to `vault/credential_store.json` — separate files, rollback is "delete credential_store.json". (2) Migration is marked complete via a sentinel file (`vault/.migration_done`) so it never runs twice. |
| **`agent.db` migration misses WAL sidecars** | Task 1.1 test pins that `agent.db-wal` and `agent.db-shm` move with the main file. If we miss them, SQLite will refuse to open the migrated DB. |
| **Tool discovery is slow on boot if `OC`** has many services | Discovery is per-credentialed-service, not bulk. We only probe services in `credential_store.json`, not the full 1568. With 1-2 services per user, boot-time discovery is sub-second. |
| **Multi-credential label prefixes might confuse the LLM** | Tested with mock schemas. If confusion is observed in real use, the LLM tool-name format `oc_<service>_<label>_<action>` is unambiguous and the label is human-readable. |
| **GitHub connector removal breaks `freehand connect github` for users who depended on it** | Users now use `freehand credential add github --token ...`. Documentation explains the migration. The `freehand connect github` path becomes a 503 honest-option-A with a hint to use `freehand credential add`. |
| **Refresh doesn't pick up new OC actions immediately** | `freehand tools refresh` is the explicit escape hatch. Boot-time discovery catches new services. For background polling, Round 11. |
| **`enable-writes` accidentally exposes destructive operations** | OC's `risk=sensitive` is a hint, not a guarantee. We surface the names of the writes being enabled in the CLI output so they can be reviewed before confirmation. `--yes` flag bypasses the confirmation; documented as such. |

---

## Slicing discipline

| Task | Slice type | Files touched | Commit |
|---|---|---|---|
| 1.1 | RED (test only) | `tests/test_database.py` | none yet |
| 1.2 | GREEN (storage) | `core/database.py` | `feat(storage): consolidate agent.db into vault/` |
| 1.3 | GREEN (deploy) | `Dockerfile`, `compose.yml`, `.dockerignore` | `chore(deploy): single vault mount — drop agent.db bind-mount` |
| 1.4 | Live smoke | (no repos) | none (verification only) |
| 1.5 | Docs | `docs/deployment/docker-compose.md` | `docs(deploy): vault consolidates agent.db — single mount` |
| 1.6 | CHANGELOG | `CHANGELOG.md` | `privatecontent: Round 10.1 vault consolidation + non-root compose` |
| 2.1 | RED (test only) | `tests/test_credential_store.py` | none yet |
| 2.2 | GREEN (storage) | `core/oauth/credential_store.py` | `feat(oauth): credential store with multi-credential support` |
| 2.3 | GREEN (migration) | `core/oauth/manager.py` | `feat(oauth): migrate legacy connections.db to credential_store` |
| 2.4 | CLI | `cli.py` | `feat(cli): credential add/list/remove/rename subcommands` |
| 2.5 | RED (test only) | `tests/test_tool_registry.py` | none yet |
| 2.6 | GREEN (registry + open_connector + agent_config) | `core/tools/registry.py`, `core/oauth/open_connector.py`, `core/agent_config.py` | `feat(tools): registry discovers OC actions as FreeHand tools` |
| 2.7 | CLI | `cli.py` | `feat(cli): tools list/enable-writes/disable/refresh subcommands` |
| 2.8 | Wiring | `cli.py` | `feat(tools): shared-app add triggers tool discovery` |
| 2.9 | Cleanup | `core/oauth/providers/github_pat.py` (delete), `core/oauth/providers/__init__.py`, `cli.py` | `refactor(oauth): route GitHub through OC, drop first-party PAT connector` |
| 2.10 | Docs | `docs/tools/README.md`, `docs/integrations/shared-apps.md`, `CHANGELOG.md` | `docs: Round 10 vault consolidation + tool discovery` |

**Total**: 13 commits across 2 PRs. Each independently GREEN, each reviewable in 10-15 minutes.

---

## Open question for Tracy before Task 2.6 — RESOLVED 2026-10-02

**Tool name format: option (c) — always labeled.**

`oc_<service>_<label>_<action>` always, even for single-credential services. The label `default` is implicit; users don't write it.

Concrete examples:
- Single Slack credential under default label: `oc_slack_default_post_message`, `oc_slack_default_chat_delete`
- Two GitHub credentials under labels `work` and `personal`: `oc_github_work_get_repo`, `oc_github_personal_get_repo`

Rationale (from Tracy): uniform naming pattern, no surprises when going from 1 to 2 credentials, and we have no existing tools to migrate.

This is locked for Task 2.6.

---

## Provenance / context for future maintainers

Tracy raised this on 02 Oct 2026 after Round 9 closed. Three threads converged:

1. **Round 7 footer option B** ("auto-register OC actions as FreeHand tools") — on the board since 28 Sep 2026
2. **Pitfall 39** (tier-1b only works for services with FreeHand connectors) — Round 9 closed it for Slack, but Notion / Linear / GitHub all need either first-party connectors or OC auto-routing
4. **Round 9's `agent.db` consolidation into `vault/`** — the follow-up that was a "Round 10 candidate" in Round 9's CHANGELOG

The shape keeps Round 9 unchanged: tier-1b broker, ALLOWED_SERVICES derived from CONNECTORS, the 503 honest-option-A fallback for unwired services. Round 10 adds the *tool layer* on top of the broker — credentials and tools become first-class objects the agent can see and call, gated by user opt-in.

Round 11 candidates (out of scope here):
- **Email connector** (IMAP/SMTP) — different auth shape, web-only
- **Webhook / event subscriptions** — Slack events, GitHub webhooks
- **Background periodic catalog refresh** — every N minutes, automatic
- **Credential rotation flow** — proactively deregister tools when credential expires
- **Per-tool schema overrides** — let Tracy customize the JSON schema OC returns
- **Auto-discovery of services that don't appear in OC's catalog** — for the long tail of providers OC doesn't yet have

**Status:** Draft. Awaiting Tracy's sign-off on the tool-name-format question above before Task 2.6.
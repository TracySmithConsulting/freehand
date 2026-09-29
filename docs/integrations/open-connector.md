# FreeHand × OpenConnector

> **Status:** Round 7 (28 Sep 2026). Tier-5 fallback broker is live.
> Non-Azure providers resolve through OpenConnector's catalog automatically.

## What this integration does

FreeHand's broker resolves credentials in four tiers:

```
1. user settings        — settings.json oauth.providers.<svc>
2. env vars             — FREEHAND_BROKER_<SVC>_CLIENT_ID/SECRET
3. broker_config.json   — admin-managed shared credentials
4. (none)               — caller treats as "no credentials"
```

Round 7 added a fifth tier:

```
5. OpenConnector runtime — best-effort self-hosted gateway on 127.0.0.1:3000
                            covering ~1,500 providers (Slack, Notion,
                            GitHub, HackerNews, Trello, Airtable, etc.)
```

When the first four tiers find nothing AND the user hits a service the OC runtime knows, FreeHand 302s the browser to OC's one-click consent screen. After the user authorises at OC, calls flow through OC and FreeHand acts as the orchestrator without holding the per-service token. (Microsoft 365 / Outlook is unchanged — that app genuinely needs an Entra app registration, OC doesn't change that.)

## What you need

1. **OpenConnector runtime**, self-hosted.
2. **A runtime token** issued by OC. You mint it at OC's Web Console → Access tab, or via `POST /api/runtime-tokens` with the admin token.
3. **Two environment variables** on FreeHand.

### Recommended: Docker

The Docker image is the simplest bring-up. The image is published at `ghcr.io/oomol-lab/open-connector:latest`:

```bash
docker run -d --name open-connector -p 3000:3000 \
  -e OOMOL_CONNECT_ADMIN_TOKEN="$(openssl rand -hex 32)" \
  -e OOMOL_CONNECT_RUNTIME_TOKEN="$(openssl rand -hex 32)" \
  -e OOMOL_CONNECT_ENCRYPTION_KEY="$(openssl rand -hex 32)" \
  -v oc-data:/app/data \
  ghcr.io/oomol-lab/open-connector:latest
```

Verify with:

```bash
curl -fsS http://127.0.0.1:3000/v1/health \
  -H "Authorization: Bearer <your runtime token>"
# Expect: {"success":true,"data":{"ok":true,"runtime":"oomol-connect"}}
```

### Alternative: native Node (no Docker)

If Docker isn't installed (see Pitfall 28 below), clone the source and run it:

```bash
git clone --depth 1 https://github.com/oomol-lab/open-connector.git
cd open-connector
npm install
PORT=3001 OOMOL_CONNECT_ADMIN_TOKEN=admin-token \
  OOMOL_CONNECT_RUNTIME_TOKEN=runtime-token \
  OOMOL_CONNECT_ENCRYPTION_KEY=encryption-key \
  node src/server/index.ts
```

Set `PORT=3001` (or another free port) when port 3000 is already taken — OC's `PORT` env var is honoured.

## Configure FreeHand

Export these before launching FreeHand:

| Variable | Default | Required for | Purpose |
|---|---|---|---|
| `OOMOL_CONNECT_BASE_URL` | `http://127.0.0.1:3000` | tier-5 | Where the OC runtime is reachable from FreeHand. Use `http://host.docker.internal:3000` when both run in Docker on the same host. |
| `OOMOL_CONNECT_RUNTIME_TOKEN` | (unset) | tier-5 | The OC runtime token. If unset, tier-5 is a silent no-op — no harm, just no fallback. |
| `OOMOL_CONNECT_HEALTH_TIMEOUT_SECONDS` | `1.5` | performance | Per-request timeout for `/v1/health`. Lower if you want faster path resolution when OC is down. |
| `OOMOL_CONNECT_CACHE_TTL_SECONDS` | `30` | performance | Catalog cache TTL. Lower for snappier cold starts. |

If you don't want tier-5 fallback on a particular FreeHand instance, leave `OOMOL_CONNECT_RUNTIME_TOKEN` unset and the broker falls through to `("none")` exactly like before.

## Bring FreeHand up

```bash
# FreeHand on the default port
python -m uvicorn server:app --host 127.0.0.1 --port 8000

# OR with the tier-5 env wired
OOMOL_CONNECT_BASE_URL=http://127.0.0.1:3000 \
  OOMOL_CONNECT_RUNTIME_TOKEN=runtime-token \
  python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

## Smoke-test the wiring

Pick a no-auth provider (HackerNews, wttr.in) — no Slack/OAuth app registration needed:

```bash
# Browser flow
curl -is "http://127.0.0.1:8000/api/integrations/hackernews/authorize?label=tracy"
# Expect: HTTP 302 with Location: http://127.0.0.1:3000/?authorize=hackernews&label=tracy&from=freehand

# Verify the broker tier-5 path
python -c "
import sys; sys.path.insert(0, 'C:/path/to/Default Project')
from core.oauth import broker
cid, sec, source = broker.get_client_credentials('hackernews')
print(f'hackernews -> source={source}, creds={(cid, sec)}')
# source='open_connector', creds=(None, None)
"

# Try a real OAuth provider (Slack) — 302 first, then user has to register
# a Slack OAuth client at api.slack.com and inject the client_id/secret
# into OC's /api/oauth/configs/slack. After that, the consent screen
# actually fires. Round 8 may add a shared OAuth app so the user
# never sees this step.
curl -is "http://127.0.0.1:8000/api/integrations/slack/authorize?label=tracy"
# Expect: 302 to OC web console

# Make sure the legacy allowlist still works
curl -is "http://127.0.0.1:8000/api/integrations/google/authorize?label=tracy"
# Expect: 200 with {"authorize_url": ..., "state": ..., "label": "tracy"}

# Negative case — service FreeHand and OC don't know about
curl -is "http://127.0.0.1:8000/api/integrations/zzzz_nonexistent/authorize"
# Expect: 404 {"detail": "Unknown service: zzzz_nonexistent"}
```

## Troubleshooting

### "tier-5 says none but OC is running"

Symptoms:

- `broker.get_client_credentials("slack")` returns `(None, None, "none")`
- OC's `/v1/health` returns 200 to a manual curl
- Slack/Notion/etc. are in OC's catalog

Likely causes (in order of frequency):

1. **Wrong token scope.** OC separates admin token (for `/api/*`) and runtime token (for `/v1/*` + `/mcp`). FreeHand needs the **runtime** token. See `oomol-lab/open-connector` auth surface: `src/server/api/auth.ts:readAuthScope()` — `path === "/v1" || path.startsWith("/v1/") ? "runtime" : "admin"`.
2. **Wrong `OOMOL_CONNECT_BASE_URL`.** If FreeHand runs in Docker and OC runs natively (or vice-versa), `127.0.0.1` doesn't reach across. Use `http://host.docker.internal:3000` from inside a FreeHand container pointing at host-machine OC.
3. **OC isn't actually healthy.** The `/v1/health` body should include `"data": {"ok": true, "runtime": "oomol-connect"}`. If `ok` is `false`, OC is up but not ready — check the OC startup log.
4. **Wrong port.** `OOMOL_CONNECT_BASE_URL` must match OC's `PORT` env var. If you ran `PORT=3001 ... node src/server/index.ts`, FreeHand must use `:3001`.

### Pitfall 28 — port 3000 already bound on Windows hosts

Without Docker, port 3000 is often occupied by another node process from a prior session. Symptom: `OSError: [Errno 98] Address already in use` on OC startup, OR the OC process exits within seconds with "address already in use" in stderr.

Diagnostic:

```bash
powershell.exe -NoProfile -Command "Get-NetTCPConnection -LocalPort 3000 -State Listen | Select-Object OwningProcess"
# Note the PID; then
powershell.exe -NoProfile -Command "Get-Process -Id <pid> | Select-Object Id, ProcessName, StartTime"
# If ProcessName shows node but StartTime is from earlier in the session,
# that's an orphan worker — kill it cleanly:
powershell.exe -NoProfile -Command "Stop-Process -Id <pid>"
# Plain Stop-Process (no -Force) sends TERM; the process takes the
# hint and releases the port within 1-2 seconds.
```

Pitfall: `Stop-Process -Id <pid> -Force` triggers a confirmation prompt under MSYS bash that hangs. Use plain `Stop-Process -Id <pid>` (default TERM). If TERM doesn't take within 5 seconds, escalate to `taskkill.exe /F /PID <pid>` (use single-slash flags, not MSYS-double).

Workaround if you just want OC up *now*: bind to a fresh port via `PORT=3001 node src/server/index.ts`, then point FreeHand at `OOMOL_CONNECT_BASE_URL=http://127.0.0.1:3001`.

### Pitfall 29 — MCP calls return 406 Not Acceptable

Symptom: `POST /mcp` returns 406 even with a valid runtime token and `Content-Type: application/json`. Cause: missing or wrong `Accept` header.

OC's MCP server requires:

```
Accept: application/json, text/event-stream
```

Both content types, comma-separated. The Python client in `core/oauth/open_connector.py` sets this on every MCP call — if you're writing a custom client, set it explicitly.

### Pitfall 30 — tier-5 cache shows stale state for ≤30s

`service_is_known()` caches the OC catalog for 30s (`OOMOL_CONNECT_CACHE_TTL_SECONDS`). If you register a new OAuth client at OC, restart an OC runtime, or add an OC provider, FreeHand won't see the change for up to 30s. Symptom: "I just registered Slack but FreeHand says unknown service."

Resolution: wait 30s, or restart FreeHand (the cache is per-process and reset on cold start).

## What's NOT covered by tier-5

- **Microsoft 365 / Outlook**: still routed via FreeHand's first-party broker. Azure subscription + Entra app registration is genuinely required for that provider. OC doesn't change this — Microsoft's OAuth dance is its own beast.
- **Auto-registered shared OAuth apps**: tier-5 redirects to OC's web console, but Slack/Notion/etc. still need the user (or an admin) to register an OAuth client at each provider's developer portal and inject the client_id/secret into OC. **Round 8** candidate: ship a shared Slack/Notion/etc. OAuth app so end-users never see this step.
- **OpenConnector actions as FreeHand native tools**: Round 7 only adds the **broker** path. The FreeHand LLM doesn't automatically know how to call `slack.post_message` via OC; that requires `TOOL_REGISTRY` + `TOOL_SCHEMAS` entries which Round 8 may add via auto-discovery.

## References

- **OpenConnector home**: https://github.com/oomol-lab/open-connector
- **OpenConnector runtime API**: https://github.com/oomol-lab/open-connector/blob/main/docs/runtime-api.md
- **OpenConnector quickstart**: https://github.com/oomol-lab/open-connector/blob/main/docs/quickstart.md
- **Wanta (reference desktop agent that uses OC)**: https://github.com/oomol-lab/wanta
- **FreeHand broker skill (4-tier history)**: `Hermes skill: freehand-agent-development`

## Provenance

Round 7 (28 Sep 2026). Plan at `.hermes/plans/2026-09-28_0930-open-connector-fallback-broker.md`. Commits: `24de6cc`, `a4e8baf`, `28e7ba1`, `93cd3e5`. Tracy raised this after a discussion about Azure subscription requirements — Round 7 makes the 1,500+ non-Azure providers reachable without an Azure subscription.

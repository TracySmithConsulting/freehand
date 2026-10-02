# FreeHand × Shared OAuth Apps

> **Status:** Round 8 (01 Oct 2026). Tier-1b broker-managed shared apps are live.

## What this integration does

Round 7 added the **OpenConnector tier-5 fallback** — for any service OC's catalog knows about, FreeHand 302s the user to OC's Web Console and the dance happens through OC's runtime.

Round 8 adds a parallel path: **FreeHand-managed shared OAuth apps** stored in FreeHand's own vault. For services Tracy has registered a shared client for, FreeHand serves the provider's consent screen directly using its own credentials — no OC involvement at runtime.

## Resolution order (Round 8 — five tiers, then `none`)

```
1.   user override          — settings.json oauth.providers.<svc>          [per-user]
1b.  shared_app (NEW)       — vault/broker_config.json -> shared_apps     [FreeHand-managed]
2.   env vars               — FREEHAND_BROKER_<SVC>_CLIENT_ID/SECRET
3.   broker (legacy)        — vault/broker_config.json -> providers        [legacy shared admin]
4.   open_connector         — OC runtime advertises the service            [Round 7]
5.   none                   — caller treats as "no credentials configured"
```

**Pitfall-5 invariant**: more-specific credentials beat more-flexible runtime paths. Tier-1 (per-user override) always wins over tier-1b (FreeHand shared app). Tier-1b always wins over tiers 2-5.

## What tier-1b gives you

| Without tier-1b (Round 7 path) | With tier-1b (Round 8 path) |
|---|---|
| User clicks Connect Slack → FreeHand 302s to OC console → OC says "configure your client first" | User clicks Connect Slack → FreeHand serves Slack's consent screen directly → done |
| End user must register their own Slack app, install to workspace, paste Bot token to OC | Tracy registers ONE Slack app for the whole FreeHand instance; everyone gets the one-click flow |
| Slack token stored in OC's SQLite | Slack client_id/secret stored in FreeHand's Fernet-encrypted vault |
| Tracy runs OC | Tracy just edits `vault/broker_config.json` (or runs `freehand shared-app add`) |

## When to use which path

**Use FreeHand tier-1b** when:
- The service has a FreeHand connector (or you're willing to add one in Round 9+)
- You want one-click consent for everyone sharing this FreeHand instance
- You're willing to do the one-time OAuth app registration at the provider's developer portal

**Use OC tier-5** when:
- You want zero local setup — OC's runtime is a hosted catalog of 1500+ providers
- The service has no FreeHand connector yet (you can't tier-1b it without a connector)
- You're OK with OC's "configure your client first" page for first-time users

**Use per-user tier-1** when:
- Different FreeHand users have different OAuth apps at the same provider (multi-tenant setups)

## Adding a shared app (Tracy's workflow)

The CLI is the right home for these operations: Tracy is the only person who runs them, they're one-time-per-service, and there's no runtime cost.

### Add

```bash
freehand shared-app add <service> \
    --client-id <from developer portal> \
    --client-secret <from developer portal> \
    --scopes "scope1,scope2,scope3" \
    --registered-by tracy
```

The `client_secret` is Fernet-encrypted on disk immediately. Example for Slack:

```bash
freehand shared-app add slack \
    --client-id "<your-client-id-from-api.slack.com>" \
    --client-secret "<your-client-secret-from-api.slack.com>" \
    --scopes "chat:write,channels:read,users:read,im:history" \
    --registered-by tracy
```

### List

```bash
freehand shared-app list
# Service        Client ID                      Scopes                                  Registered
# -----------------------------------------------------------------------------------------------------
# slack          <your-client-id>               chat:write,channels:read,users:read,... tracy
```

**Secrets are NEVER displayed** — only client_id, scopes, and audit metadata. The encryption is intentional: there's no reason to surface a secret to a user who can re-add it from the portal if needed.

### Remove

```bash
freehand shared-app remove slack
# Removed shared app: slack
```

The shared app entry is deleted from `vault/broker_config.json`. The user secret at the provider's developer portal is unaffected — you only lose the local copy.

## Where the credentials live

| Field | Storage | Encryption |
|---|---|---|
| `client_id` | `vault/broker_config.json -> shared_apps.<service>.client_id` | plaintext (it's a public identifier) |
| `client_secret` | `vault/broker_config.json -> shared_apps.<service>.client_secret_enc` | **Fernet** via `vault/encryption.key` |
| `client_secret_enc` on disk | Reads as `gAAAAA...` (Fernet ciphertext) | Same key as `api_key`, connection tokens |
| Scopes / metadata | `vault/broker_config.json -> shared_apps.<service>.*` | plaintext |

The Fernet key in `vault/encryption.key` is auto-generated on first use (same code path as `api_key` storage). Blast radius = "attacker who has both the FreeHand vault AND the encryption key" — same as today's posture.

## The 503 honest-option-A fallback

When the user clicks Connect for a service that has neither:
- A per-user override (tier-1)
- A FreeHand shared app (tier-1b)
- Env / broker_config credentials (tiers 2-3)
- An OC catalog entry (tier-5)

…the router returns **HTTP 503** with a structured body (instead of the old 404 "Unknown service"):

```json
{
  "error": "no_shared_app",
  "service": "linear",
  "message": "FreeHand doesn't have a shared OAuth app for linear yet. You can request it — click the request_url to file a GitHub issue with the details pre-filled, or email Tracy if you'd rather not use GitHub.",
  "request_url": "https://github.com/TracySmithConsulting/freehand/issues/new?title=Request%3A%20shared%20OAuth%20app%20for%20linear&body=...",
  "contact_email": "tracy@tracysmith.co.za",
  "scopes_help": "https://docs.tracysmith.co.za/integrations/shared-apps.html#contributing-a-shared-app"
}
```

The `request_url` is a GitHub "new issue" URL with title and body query params pre-filled — the user clicks, reviews, and submits themselves. **FreeHand does NOT create the issue on the user's behalf** (no auth surface, user stays in control).

### Requesting a service

When you hit the 503 response, two paths forward:

**Path 1 — GitHub issue (recommended)**:
1. Click the `request_url` in the 503 body
2. GitHub opens a new issue form with title and body pre-filled
3. Fill in the OAuth scopes you need, any app-level prerequisites
4. Submit

Tracy triages issues in the public stream. Other users can `+1` existing requests, see if it's already planned, or subscribe for updates.

**Path 2 — Email Tracy**:
Send an email to `tracy@tracysmith.co.za` with:
- The service name (e.g. `linear`)
- The OAuth scopes your agent needs
- Any app-level prerequisites you're aware of (paid developer account, workspace install, tenant restrictions)

Use this if you'd rather not use GitHub or have a private context (e.g. an internal corporate tool).

### ETA expectation

Tracy is a single maintainer — no SLA on turnaround. The public issue stream is the visibility mechanism: if your request has `+5`s, it's more likely to get prioritized. If you have a tight deadline, the contributing path below is faster.

## Contributing a shared app (developer path)

If you're a developer (not Tracy) and want to register a shared app for your own FreeHand instance:

1. Fork the repo: `https://github.com/TracySmithConsulting/freehand`
2. Add the entry to YOUR `vault/broker_config.json`:
   ```bash
   freehand shared-app add linear \
       --client-id <from linear.app/settings/api> \
       --client-secret <from linear.app/settings/api> \
       --scopes "read,write" \
       --registered-by <your-github-handle>
   ```
3. Open a PR with the change to `vault/broker_config.json` and a brief note in the PR description:
   - Which service
   - Which scopes you registered at the provider's portal
   - Any setup gotchas you hit
4. Tracy reviews and merges if the registration looks correct

This is faster than the issue path for developers who already have a Slack-style developer account at the provider's portal. Tracy is happy to merge community-contributed shared apps as long as the scopes are reasonable and the registration is documented.

## Security model

| What | Who can see | Risk |
|---|---|---|
| `client_id` of a shared app | Anyone with `freehand shared-app list` (or read access to `vault/broker_config.json`) | Low — `client_id` is a public identifier |
| `client_secret` of a shared app | **No one**, in plaintext. Only encrypted on disk; never returned by the CLI | The encrypted blob is useless without `vault/encryption.key` |
| `vault/encryption.key` | Anyone with read access to the FreeHand vault directory | Decrypts `api_key`, connection tokens, AND shared app secrets — full FreeHand compromise |

If `vault/encryption.key` is compromised, rotate it via `freehand rekey` (Round 8 candidate, not yet implemented). Until then, re-register each shared app and re-authorize each connection after rotation.

## Round 8 live smoke (verified 01 Oct 2026)

| Service | Path | Result |
|---|---|---|
| `google` | tier-1 (per-user broker) | 200 JSON with Google authorize URL |
| `slack` | tier-5 (OC) | 302 to `http://127.0.0.1:3001/?authorize=slack&label=tracy&from=freehand` |
| `hackernews` | tier-5 (OC, no-auth) | 302 to OC console |
| `linear` | tier-5 (OC) | 302 to OC console |
| `myspace` | none → **503** | `error=no_shared_app`, `service=myspace`, pre-filled GitHub URL, `contact_email=tracy@tracysmith.co.za` |

Plus the **live Slack OAuth dance** through OC: Slack config PUT to OC, user consent screen, Slack user token stored (`accountId: U0C56R6LTD4`, `grantedScopes: [im:history, channels:read, users:read, chat:write]`).

## Scope limitation (honest)

Tier-1b works end-to-end for services that already have a FreeHand connector (e.g. Google, Microsoft — the existing `_get_credentials()` calls `broker.get_client_credentials()` and picks up tier-1b transparently). For services WITHOUT a FreeHand connector (Slack, Notion, Linear, etc.), tier-1b's broker side works but FreeHand has nothing to render the authorize URL — the route falls through to `get_connector(service)` which returns `None` and the user gets `501 Connector not implemented`. This is a Round 9 candidate: add per-service Python connectors for tier-1b services.

## Related

- `docs/integrations/open-connector.md` — Round 7 OC tier-5 fallback (alternative path)
- `docs/integrations/slack-oauth-setup.md` — Slack-portal walkthrough (Register at api.slack.com, OAuth scopes, Reinstall trap, Add-then-Save trap)
- `references/slack-oauth-setup.md` (in the `freehand-agent-development` skill) — Detailed Slack bring-up including Pitfall 33 + 33a traps

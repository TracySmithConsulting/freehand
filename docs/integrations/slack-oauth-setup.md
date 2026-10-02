# Slack OAuth setup — FreeHand shared app registration

> **Status:** Round 8 (01 Oct 2026). Slack can be wired into FreeHand via the
> shared-app CLI (`freehand shared-app add slack`) so users get a one-click
> consent screen.
>
> This doc walks through the **provider-portal side** — what to do at
> `api.slack.com` so the FreeHand shared-app registration actually works.
> Pair with `docs/integrations/shared-apps.md` for the FreeHand-side workflow.

## When to use this

You're registering a Slack OAuth app for **FreeHand to use as the shared
client_id/client_secret**. After this is done:

1. `freehand shared-app add slack --client-id ... --client-secret ... --scopes ...`
2. End users in your FreeHand instance get one-click "Connect Slack"
3. Tokens are stored in `vault/connections.db` per user

If you don't want to register an app at api.slack.com, see
`docs/integrations/open-connector.md` for the OpenConnector tier-5 path
(which doesn't require FreeHand to hold Slack credentials).

## Step 1 — Create the Slack app

1. Open `https://api.slack.com/apps` in your browser.
2. Click **Create New App** → **From scratch**.
3. Name it (e.g. `FreeHand Dev`) and pick the Slack workspace you'll
   authorise against. FreeHand can't authorise against a workspace you
   don't own.
4. After creation, you land on the app's **Basic Information** page.

## Step 2 — Copy the credentials

From the **Basic Information** page, you need two values. Push both to
your clipboard (Pitfall 17 — chat scrolls, screen-readers don't reliably
land on inline code spans, the clipboard is the channel of truth).

- **Client ID** — numeric, e.g. `<workspace_id>.<app_id>`. Already
  visible on the page.
- **Client Secret** — hex string. Click **Show** next to "Client Secret",
  then **Copy**. Looks like `<hex_string>`.

**Do NOT** use the Bot User OAuth Token (`xoxb-...`) or the Signing Secret
in this step. Both are wrong credentials for the OAuth dance:

| Field | What it is | Where it goes |
|---|---|---|
| `Client ID` | OAuth app identifier | `freehand shared-app add slack --client-id ...` |
| `Client Secret` | OAuth app secret (hex) | `freehand shared-app add slack --client-secret ...` |
| `Bot User OAuth Token` (`xoxb-...`) | Token granted **after** consent | `vault/connections.db` per-user (automatic, not via this CLI) |
| `Signing Secret` | For verifying Slack event webhooks | Not used by FreeHand OAuth dance |
| `App-Level Token` (`xapp-...`) | For socket-mode apps | Not used by FreeHand OAuth dance |

## Step 3 — Register the Redirect URL

In the left sidebar: **OAuth & Permissions** → **Redirect URLs** section.

1. Click **Add New Redirect URL**.
2. Paste exactly `http://localhost:8000/api/integrations/slack/callback`.
   - **Not** `:3001/oauth/callback` (that's the Round 7 OC path)
   - **Not** trailing slash
   - **Not** `127.0.0.1` — `localhost` is what FreeHand's `_get_redirect_uri()`
     returns at request time (Pitfall 11)
3. Click the inline **Add** button — the URL appears as a row in the list.
4. **CRITICAL: scroll to the bottom of the Redirect URLs section and click
   "Save URLs"**. The Add button inserts the row into the edit buffer; Save
   URLs commits the list to Slack. The commit is silently missed if you
   walk away after Add — the row looks present on screen but doesn't reach
   the server until Save is pressed. (Pitfall 33a)

## Step 4 — Set Bot Token Scopes

In the same **OAuth & Permissions** page, scroll to **Bot Token Scopes**:

1. Click **Add an OAuth Scope**.
2. Add at minimum: `chat:write`, `channels:read`, `users:read`.
3. Add `im:history` if you want the agent to read/send direct messages.

Pitfall 34: Slack's consent screen will list every scope you request. If
the user has registered scopes A, B, C, D at api.slack.com but the
request URL asks for A, B, C, D, E, F, Slack rejects with `missing_scope`.
The fix is to make sure `--scopes` to `freehand shared-app add` is a
**subset** of the scopes registered at api.slack.com.

Round 8 recommended default set: `chat:write,channels:read,users:read,im:history`.

## Step 5 — Install (or re-install) the app

In the left sidebar: **Install App**.

- **First install**: click **Install to Workspace** → grant consent →
  Slack shows the **Bot User OAuth Token** (`xoxb-...`) on the OAuth &
  Permissions page. You don't need to paste this anywhere — it's the
  result of the dance, not the input.

- **Re-install**: if you've changed Redirect URLs or Bot Token Scopes
  since the last install, Slack will NOT enforce those changes until you
  **Reinstall to Workspace**. This is the single most common cause of
  `redirect_uri did not match any configured URIs` even when the URL is
  byte-identical to what's registered. Slack enforces Redirect URLs from
  the *installed* app version, not the *app definition*. (Pitfall 33)

After Reinstall, you don't need to re-copy anything for the
`freehand shared-app add` workflow — FreeHand's stored client_id and
client_secret are unchanged. The Bot User OAuth Token shown on the page
may have rotated, but that token only appears AFTER the user runs the
dance, not before.

## Step 6 — Add to FreeHand's shared-app registry

Now register the credentials with FreeHand. From your terminal:

```bash
freehand shared-app add slack \
    --client-id "<from Step 2>" \
    --client-secret "<from Step 2>" \
    --scopes "chat:write,channels:read,users:read,im:history" \
    --registered-by tracy
```

The `client_secret` is Fernet-encrypted on disk in
`vault/broker_config.json -> shared_apps.slack.client_secret_enc`
immediately on write.

Verify with:

```bash
freehand shared-app list
# Service  Client ID          Scopes                                  Registered
# ----------------------------------------------------------------------------------------
# slack    <your-client-id>   chat:write,channels:read,users:read,... tracy
```

Secrets are never displayed in `list` output.

## Step 7 — Test the dance

End-user perspective: a FreeHand user clicks "Connect Slack" in the web
UI. The flow goes:

1. Browser hits `GET /api/integrations/slack/authorize?label=<user>`
2. FreeHand's broker resolves Slack to tier-1b (FreeHand shared app)
3. FreeHand serves Slack's consent screen directly using the stored
   client_id
4. User approves → Slack redirects to
   `http://localhost:8000/api/integrations/slack/callback?code=...&state=...`
5. FreeHand exchanges the code for a token at Slack's
   `oauth.v2.access` endpoint, stores it in `vault/connections.db`

If anything in this chain breaks, the failure mode tells you which
step hit a problem:

| Failure | Likely cause |
|---|---|
| `redirect_uri did not match any configured URIs` | Pitfall 33 — Reinstall not done after URL changes. Reinstall to Workspace. |
| `redirect_uri did not match` AND the URL "doesn't exist on the page" | Pitfall 33a — Add clicked but Save URLs not clicked. Scroll to bottom of section, click Save. |
| `missing_scope` from Slack | Pitfall 34 — `--scopes` includes a scope not registered at api.slack.com. Either register it there or trim `--scopes`. |
| `invalid_client` from Slack | `--client-secret` is wrong. Re-copy from Basic Information. |
| Slack returns the consent screen with no errors | Working. Click Allow. |

## Verification checklist

- [ ] Slack app exists at `api.slack.com/apps`
- [ ] Client ID and Client Secret extracted (NOT Bot token)
- [ ] Redirect URL `http://localhost:8000/api/integrations/slack/callback`
      added AND Save URLs clicked AND Reinstall done
- [ ] Bot Token Scopes registered: at minimum `chat:write, channels:read,
      users:read`; add `im:history` if DMs wanted
- [ ] `freehand shared-app add slack` ran with those scopes
- [ ] `freehand shared-app list` shows the entry without exposing secrets
- [ ] End-to-end test: `GET /api/integrations/slack/authorize?label=test`
      reaches Slack's consent screen
- [ ] After Allow, FreeHand stores the connection in `vault/connections.db`

## See also

- `docs/integrations/shared-apps.md` — FreeHand-side workflow (CLI, security,
  503 fallback, "Requesting a service", "Contributing a shared app")
- `references/slack-oauth-setup.md` (in the `freehand-agent-development` skill)
  — Detailed Slack pitfalls with extended diagnostics
- `docs/integrations/open-connector.md` — Alternative Round 7 OC tier-5 path

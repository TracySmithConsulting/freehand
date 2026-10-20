# OAuth Setup Guide — FreeHand

FreeHand connects to external services via OAuth 2.0. You need to register an app with each provider and paste the credentials into the FreeHand web UI (Settings → OAuth App Credentials).

---

## Google Workspace (Gmail, Calendar, Sheets, Meet)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select existing)
3. Navigate to **APIs & Services > Credentials**
4. Click **Create Credentials > OAuth client ID**
5. Application type: **Web application**
6. Name: `FreeHand` (or whatever you want)
7. Authorized redirect URIs:
   ```
   http://localhost:8000/api/integrations/callback/google
   ```
   (If using Tailscale, also add: `http://100.x.y.z:8000/api/integrations/callback/google`)
8. Click **Create**
9. Copy the **Client ID** and **Client Secret** into FreeHand settings

**⚠️ Google Workspace:** If your org uses Google Workspace, the admin may need to authorize the app. If you see "admin_policy_enforced", contact your admin or use a personal Gmail account.

---

## Microsoft 365 (Outlook, Teams, Calendar)

1. Go to [Microsoft Entra ID](https://entra.microsoft.com/)
2. Navigate to **Identity > Applications > App registrations**
3. Click **New registration**
4. Name: `FreeHand`
5. Supported account types: **Accounts in any organizational directory and personal Microsoft accounts**
6. Redirect URI: **Web** → `http://localhost:8000/api/integrations/callback/microsoft`
7. Click **Register**
8. Copy the **Application (client) ID**
9. Go to **Certificates & secrets** → **New client secret** → Copy the secret value
10. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**
    - Add: `Mail.Read`, `Mail.Send`, `Calendars.Read`, `Calendars.ReadWrite`, `User.Read`, `offline_access`
11. Paste Client ID, Client Secret, and Tenant (`common` for personal + work) into FreeHand settings

**⚠️ Microsoft 365:** Some admins restrict third-party app consent. If you see "consent_required", your admin needs to grant org-wide consent.

---

## Zoom (Meetings, Recordings, Chat)

1. Go to [Zoom Marketplace Developer Console](https://marketplace.zoom.us/)
2. Click **Build App** → **OAuth App** (NOT Server-to-Server)
3. App Name: `FreeHand`
4. Under **OAuth & Webhooks**:
   - Redirect URL: `http://localhost:8000/api/integrations/callback/zoom`
   - Scopes: `meeting:read`, `meeting:write`, `user:read`, `user:read:list`, `user:write`
5. Under **App Information**, copy **Client ID** and **Client Secret**
6. **Important:** You must click **Install** on the app page to generate an install link, then visit it to install the app on your account
7. Paste into FreeHand settings

**Note:** Zoom OAuth Apps are different from Server-to-Server apps. Use OAuth App for per-user access.

---

## Facebook and Instagram (Meta)

FreeHand uses one Meta app entry for both connectors. Create a **Facebook Login for Business** configuration that includes the Page and Instagram assets you want to expose, then copy its **Configuration ID**.

For a deployed broker, replace `<public-host>` with the HTTPS hostname that serves FreeHand. For local development, Meta may reject bare `localhost` in the app-domain validator; use a stable HTTPS tunnel or test against the deployed broker instead.

If the browser reaches FreeHand through a different hostname (for example, a reverse proxy or tunnel), set `FREEHAND_PUBLIC_BASE_URL` to the same HTTPS base URL registered with Meta:

```text
FREEHAND_PUBLIC_BASE_URL=https://<public-host>
```

1. Go to [Meta for Developers](https://developers.facebook.com/)
2. Open **My Apps** → `FreeHand` → **Facebook Login for Business** → **Configurations**.
3. Create or edit a configuration and record its **Configuration ID**.
4. Go to **Facebook Login for Business** → **Settings** → **Valid OAuth Redirect URIs** and add:
   ```
   https://<public-host>/api/integrations/facebook/callback
   https://<public-host>/api/integrations/instagram/callback
   ```
5. Go to **Settings → Basic** and copy the **App ID** and **App Secret**.
6. Register the shared app in FreeHand. The secret is encrypted immediately and is never shown by `shared-app list`:
   ```bash
   freehand shared-app add facebook \
     --client-id <meta-app-id> \
     --client-secret <meta-app-secret> \
     --config-id <meta-configuration-id> \
     --scopes "pages_read_engagement,pages_manage_posts,instagram_basic,instagram_content_publish,email,public_profile" \
     --registered-by tracy
   ```
7. In development, only app roles/testers can authorize. For public users, complete Meta’s app settings and App Review for each permission you request.

---

## GitHub (Repos, Issues, Gists)

GitHub doesn't use OAuth for this — use a **Personal Access Token (PAT)**:

1. Go to [GitHub Settings > Developer settings > Personal access tokens](https://github.com/settings/tokens)
2. Click **Generate new token (classic)** or **Fine-grained token**
3. For classic: select scopes `repo`, `gist`, `read:org`
4. For fine-grained: select only the repos you need + `contents: read/write`
5. Copy the token and paste it when prompted by FreeHand (`freehand connect github` or via the UI)

---

## Custom Email (IMAP/SMTP)

For any email provider not covered by Google or Microsoft (e.g., cPanel, Plesk, self-hosted):

1. In FreeHand UI, click **Connect** next to **Email (IMAP/SMTP)**
2. Enter your IMAP/SMTP host, port, username, and password
3. SSL is recommended (port 993 for IMAP, 465/587 for SMTP)

---

## Tailscale Users

If you access FreeHand via Tailscale (`http://100.x.y.z:8000`), the redirect URI auto-updates on first connection attempt. You still need to register the Tailscale URL with each provider:

```
http://100.x.y.z:8000/api/integrations/callback/{service}
```

Replace `100.x.y.z` with your actual Tailscale IP.

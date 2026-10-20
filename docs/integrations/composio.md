# Composio connector adapter

FreeHand can use Composio for Facebook and Instagram authentication. In this
mode Composio hosts the consent screen and stores the provider token. FreeHand
stores only a local anonymous user id, the account label, and Composio's
connected-account id.

## Configure the adapter

1. Create or open a Composio project.
2. Create a managed auth config for the Facebook toolkit and another for the
   Instagram toolkit. Copy each auth config id.
3. Set these variables before starting FreeHand:

   ```text
   COMPOSIO_API_KEY=your-project-key
   COMPOSIO_AUTH_CONFIG_FACEBOOK=your-facebook-auth-config-id
   COMPOSIO_AUTH_CONFIG_INSTAGRAM=your-instagram-auth-config-id
   ```

   `COMPOSIO_BASE_URL` is optional and defaults to
   `https://backend.composio.dev`.

4. Start FreeHand with `freehand start` and open Settings → Connections.
5. Click Connect Facebook or Connect Instagram. The button opens a Composio
   Connect Link and returns to FreeHand when the account is connected.
6. FreeHand then discovers the current Composio actions for that toolkit. Safe
   read actions become agent tools automatically; write and destructive actions
   stay pending until you explicitly enable them:

   ```text
   freehand tools list facebook
   freehand tools enable-writes facebook
   ```

   The same workflow applies to `instagram`. Refreshing tools also refreshes
   Composio-connected accounts with `freehand tools refresh`.

For a local installation, the callback is generated from the browser address,
for example:

```text
http://127.0.0.1:8000/api/integrations/facebook/composio/callback?label=default
```

For a hosted installation, set `FREEHAND_PUBLIC_BASE_URL` to the HTTPS public
address before starting FreeHand. The callback then uses that hostname.

## Backend selection

Composio is used for Facebook and Instagram only when both the API key and the
corresponding auth-config id are present. Otherwise FreeHand falls back to its
existing first-party Meta connector. OpenConnector remains the dynamic
connector backend for its catalog of services.

## Important hosted-service note

The local adapter uses one stable anonymous id per FreeHand installation. A
hosted FreeHand deployment must replace that identity with the authenticated
application user's id and enable Composio's callback identity verification so
one user cannot attach another user's provider account.

Official references:

- [Composio authentication](https://docs.composio.dev/docs/authentication)
- [Composio Connect Links](https://docs.composio.dev/docs/authentication/manually-authenticating)
- [Composio connected-account API](https://docs.composio.dev/reference/api-reference/connected-accounts)

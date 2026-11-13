# Google and Microsoft accounts

Gmail, Google Calendar, Google Sheets, Outlook and Outlook Calendar connect
through OpenConnector, not FreeHand's own OAuth broker. OpenConnector holds
the sign-ins, renews them, and runs the calls. FreeHand's email, calendar and
Sheets tools use them by name.

This setup is free. It uses your own Google Cloud and Microsoft Entra apps.
OOMOL's hosted service could replace those apps later, if FreeHand grows
past Google's limit of 100 test users.

## One-time setup: allow OpenConnector's sign-in address

Both apps must allow OpenConnector's sign-in return address:

    http://localhost:3001/oauth/callback

Use `localhost` exactly, not `127.0.0.1`. OpenConnector treats them as
different addresses.

### Google

1. Open Google Cloud console, then APIs & Services, then Credentials.
2. Open your OAuth client, the one FreeHand used before.
3. Under "Authorized redirect URIs", add the address above, then save.
4. Under APIs & Services, then Library, turn on the APIs you want: Gmail
   API, Google Calendar API, Google Sheets API, and optionally Google Drive
   API, Google Docs API, Google Slides API, Google Meet REST API and Google
   Tasks API.
5. While the app is in testing, add each Google account that will connect
   as a test user, under the OAuth consent screen. Google ends test users'
   sign-ins after 7 days, so they need to reconnect weekly.

### Microsoft

1. Open the Microsoft Entra admin center, then App registrations, then your
   app.
2. Under Authentication, add the address above as a Web redirect URI, then
   save.

The app settings (client ID and secret) are already in OpenConnector for
Gmail, Google Calendar, Sheets, Drive, Docs, Slides, Meet and Tasks, and for
Outlook, Outlook Calendar, Teams, OneDrive, Excel and To Do.

## Connect an account

In the FreeHand web console, open Connections, then choose Connect Gmail,
Connect Google Calendar, Connect Google Sheets, Connect Outlook or Connect
Outlook Calendar. Each opens OpenConnector's page for that app.

On that page, set **Connection name**. This is the name FreeHand uses to
pick the account:

- One account per app: name it `default`.
- Several accounts: give each a short name, such as `work` or `tracy`. Use
  the same name for the same account in every app, for example `work` for
  your work Gmail and your work Google Calendar.

Then choose Connect and sign in.

Each app is connected separately. To let FreeHand read your Gmail and your
Google Calendar, connect both.

## Using it

Ask FreeHand as usual, for example "read my work email" or "what's on my
calendar this week". If an account isn't connected yet, FreeHand says which
one, and links to the page where you can connect it.

## Notes

- The Microsoft apps' Tenant setting in OpenConnector decides which accounts
  can sign in: `common` (any), `consumers` (personal outlook.com and
  hotmail.com accounts only) or `organizations` (work or school only).
- If connecting Outlook fails with `credential_verification_failed` /
  `UnknownError`, the sign-in worked but Microsoft would not return the
  account's profile. First check that the account can open outlook.com and
  that Microsoft 365 has no outage.
- Microsoft Teams needs a work or school account and an administrator's
  consent for some permissions. Personal Microsoft accounts can't use it.
- Meeting times without a time zone are taken as UTC. Include the offset,
  for example `2026-11-13T10:00:00+02:00`.
- The Google and Microsoft app settings that were in the broker are kept in
  `vault/broker_config.before-openconnector.json`, in case they're needed.

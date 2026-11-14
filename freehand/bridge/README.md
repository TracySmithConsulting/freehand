# FreeHand WhatsApp bridge

A small local service that links one WhatsApp account to FreeHand, as a
linked device. Messages sent to that number from people on your
`whatsapp_allow_from` list go to the agent. Replies and approval requests
come back the same way.

Only one-to-one chats are handled. Group chats, broadcast lists, status
updates and channels are ignored.

## Which number to use

We recommend a separate WhatsApp number for FreeHand, such as a spare SIM
or a WhatsApp Business number. It keeps FreeHand's chats apart from your
own, so it's cleaner.

No second number? That's fine. Link your own number, and talk to FreeHand
in your "Message yourself" chat. To find that chat, start a new chat in
WhatsApp and pick your own name at the top of the contact list. Your other
chats are never sent to FreeHand.

## Set up

The web console walks you through steps 3 and 5, under Remote Gateways,
WhatsApp.

1. Install the bridge's dependencies. You need Node.js 20.12 or newer:

   ```
   cd freehand/bridge
   npm install
   ```

2. Create the shared secret that FreeHand and the bridge use to trust each
   other:

   ```
   freehand gateway secret
   ```

   Copy `.env.example` to `.env` in this folder, and set `BRIDGE_SECRET` to
   the value that command printed.

3. Say who may talk to FreeHand. In the web console, under Remote
   Gateways, WhatsApp, step 3, list the phone numbers, one per line, with
   country code. Spaces and the plus sign don't matter. (This is the
   `whatsapp_allow_from` setting.)

   - Linked a separate number? Put your own personal number on the list.
   - Linked your own number? You don't need to list anything. Leave "Let
     the linked number talk to FreeHand" switched on, and use your
     "Message yourself" chat.

4. Start the bridge, and leave it running:

   ```
   npm start
   ```

   On Windows, you can have it start by itself instead, with no window,
   every time you sign in. It also starts again by itself if it ever
   stops. Run this once from the FreeHand folder (no administrator
   rights needed):

   ```
   powershell -ExecutionPolicy Bypass -File scripts\windows-bridge-autostart.ps1
   ```

   Its messages go to `%LOCALAPPDATA%\FreeHand\whatsapp-bridge.log`. To
   turn this off again, run the same command with `-Remove` at the end.

5. Link WhatsApp. On the FreeHand computer, run:

   ```
   freehand gateway pair 27821234567
   ```

   On your phone, open WhatsApp, then go to Settings, Linked devices, Link a
   device, and choose to link with a phone number instead. Enter the code.

Check the result with `freehand gateway status`.

## Security

- The bridge listens on `127.0.0.1` only. Change `BRIDGE_HOST` only if
  FreeHand runs on another machine. Anyone who can reach the bridge and
  knows the secret can send messages from your number.
- Every request in both directions must carry the shared secret. The
  bridge refuses to start without one.
- Pairing can only be started from the FreeHand computer, never from a
  chat.
- The `auth_info_baileys` folder holds the linked-device session. Treat it
  like a password, and don't copy or commit it. It's in `.gitignore`. To
  unlink, remove the device in WhatsApp under Linked devices.

## Troubleshooting

- **"bridge_secret not configured"**: run `freehand gateway secret`, and
  put the value in this folder's `.env`.
- **"Invalid or missing X-Bridge-Secret"**: the two copies of the secret
  don't match. Copy it again from `freehand gateway secret`.
- **The web console says "bridge not running"**: open this folder and run
  `npm start`, then reload the page.
- **You linked your own number and messages get no reply**: make sure
  you're writing in your "Message yourself" chat, not someone else's chat,
  and that "Let the linked number talk to FreeHand" is switched on.
- **A message gets no reply**: check that the sender is on
  `whatsapp_allow_from`. Some chats arrive with a hidden-number ID ending in
  `@lid`. The FreeHand log shows the ID, and you can add that exact ID to
  the list.

## Tests

```
npm test
```

The tests use a fake WhatsApp connection, so no phone is needed.

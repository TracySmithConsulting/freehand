# FreeHand WhatsApp bridge

A small local service that links one WhatsApp account to FreeHand, as a
linked device. Messages sent to that number from people on your
`whatsapp_allow_from` list go to the agent. Replies and approval requests
come back the same way.

Only one-to-one chats are handled. Group chats, broadcast lists, status
updates and channels are ignored.

## Set up

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

3. Allow your own number. In FreeHand's settings, set
   `whatsapp_allow_from`, for example `["+27 82 123 4567"]`. Spaces and the
   plus sign don't matter.

4. Start the bridge, and leave it running:

   ```
   npm start
   ```

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
- **A message gets no reply**: check that the sender is on
  `whatsapp_allow_from`. Some chats arrive with a hidden-number ID ending in
  `@lid`. The FreeHand log shows the ID, and you can add that exact ID to
  the list.

## Tests

```
npm test
```

The tests use a fake WhatsApp connection, so no phone is needed.

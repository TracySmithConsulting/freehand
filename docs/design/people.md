# Separate users: the plan

Status: foundations built (November 2026). The full build waits for a first
office to try it with, so it's shaped by real needs, not guesses.

## The idea in plain words

Today one FreeHand belongs to one person. The aim is an office, where FreeHand
runs on a server and several people use it. Everyone shares the things a team
shares, and everyone also has their own private space.

Think of it like an office building:

- **The shared part** is like the shared drive and the team phone line.
  Everyone can use it.
- **Your workspace** is like your own desk and diary. Nobody else sees it,
  not even FreeHand on someone else's behalf.

## What's shared and what's private

| Shared by everyone | Private to each person |
|---|---|
| The AI model, and its backups | Your chats with FreeHand |
| Shared connections, such as a team mailbox (info@) or the office calendar | Your own connections, such as your own mailbox |
| The shared information pool: facts, notes and documents everyone can read and add to | Your own memories: what FreeHand remembers about you |
| Approved skills (saved ways of doing tasks) | Your scribble pad, notes and files |
| | Your tasks, and the approvals you asked for |

**Connections need care.** A shared connection is right for a team mailbox.
But each person's own mailbox must be a *personal* connection that only its
owner can use. Otherwise anyone could ask FreeHand to read the boss's email.

**Sharing is a choice.** What you tell FreeHand is private unless you say
otherwise, for example "share this with the office" or "add this to the shared
notes".

## Who can do what

- **The owner** (or administrators) set up the AI model and the shared
  connections, add and remove people, and decide what each person may do.
- **Members** use FreeHand, manage their own connections and workspace, and
  add to the shared pool.
- **Approvals:** each person sees and answers their own. Risky actions on a
  shared connection (for example sending from info@) need an administrator's
  approval. That's the same idea as today's rule that destructive actions are
  approved in the console.

## How FreeHand knows who's talking

- **The console:** each person signs in with their own account. That needs
  FreeHand on an https address, which the hosted setup already provides.
- **Telegram, WhatsApp and Slack:** each person's chat ID or phone number is
  linked to them. This replaces today's single "allowed" list. The office
  uses one bot, and FreeHand answers each person as themselves.

## Built now: the foundations

None of this changes how a one-person FreeHand behaves. Everything is simply
tagged as the owner's.

1. **People in settings.** There's always an owner. Other people can be listed
   with their Telegram, WhatsApp and Slack links:

   ```json
   "owner_name": "Tracy",
   "people": [
     {"id": "thandi", "name": "Thandi", "role": "member",
      "telegram": ["123456789"], "whatsapp": ["+27 82 123 4567"]}
   ]
   ```

   A linked chat ID or number is allowed in, and its messages are that
   person's. The existing allow-from lists still work, and belong to the
   owner. This is in `core/people.py`.
2. **Every message is linked to a person.** Each agent run knows whose message
   it's handling (`people.current()`).
3. **What FreeHand stores is tagged with whose it is:**
   - chat history (`conversation_turns.person`)
   - tasks (`tasks.person`)
   - approvals (`approvals.person`)
   - new memories (Hindsight tag `person:<id>`)

   Rows from before this change became the owner's.
4. **Memory tags are proven.** In a test bank, a memory tagged `person:thandi`
   is found when recalling with that tag, and not when recalling with
   `person:owner`. That's how private and shared memories will be kept apart
   in a single memory bank.

## Still to build (the full version)

In a sensible order:

1. **Sign-in per person** in the console, and a People page in Settings to add
   people and link their chats. That replaces editing settings by hand.
2. **Private memories and the shared pool:**
   - FreeHand recalls with `person:<me>` or `shared`;
   - "share this" saves with `shared`;
   - the Memory page shows yours and the shared pool separately.

   Before turning on filtering, tag the existing untagged memories
   `person:owner`, so they don't vanish from recall.
3. **Chats, tasks and approvals filtered by person.** Each person sees only
   their own. Administrators can see approvals for shared connections.
   Scheduled tasks run as the person who made them.
4. **Workspaces:** a scribble pad and notes folder each, plus a shared folder.
5. **Personal and shared connections:**
   - OpenConnector connections named per person (for example `thandi-work`);
   - a Composio user ID per person;
   - FreeHand only lets each person use their own connections and the shared
     ones.
6. **Roles and permissions:** what members may do, set per person.

## Questions for the first office

- Can a manager see their team's tasks or chats, or only approvals?
- Which connections are shared, and who may send from them?
- Should the shared pool need approval before something is added?
- Does anyone use FreeHand only by Telegram or WhatsApp, never the console?

## Why not one FreeHand per person?

That already works (`scripts/provision_tenant.py`), and it's the right choice
when people share nothing: for example, separate clients. It can't give an
office a shared pool or shared connections, which is why this plan exists.

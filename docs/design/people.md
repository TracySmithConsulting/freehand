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
5. **Before separating (Nov 2026), from a review.** Five things would have
   quietly undone the separation, each by running a person's work as the
   owner. They're fixed, with tests that work as Thandi
   (`tests/test_people_before_separation.py`):
   - Work handed to another thread keeps its person (`people.carry`): the
     Composio and MCP tool paths and the scheduler.
   - A scheduled job runs as the person who made it. If they're no longer
     on this FreeHand, the job doesn't run (it pauses after a few tries).
   - The settings file can't make a second owner, and an unknown role
     (a typo such as "chlid") isn't let in, rather than becoming a member.
   - Telegram and Slack go by who wrote, not the chat. A Telegram group is
     answered only if the owner allowed that group, and only for people
     FreeHand knows, each as themselves; a group can't be someone's
     personal link.
   - A chat approval can only be answered by the person who asked.

## Still to build (the full version)

In a sensible order:

1. **Sign-in per person** in the console, and a People page in Settings to add
   people and link their chats. That replaces editing settings by hand.

   **Built (November 2026): sign-in** (`core/sessions.py`), and **the
   People page** in the console (owner only):
   - **Add, change or remove** a person, with their Telegram chat ID
     and WhatsApp number.
   - **Mistakes are explained:** a group's chat ID, a number without a
     country code, or a chat that's already someone else's.
   - **"Wrote to FreeHand, but isn't on it"** (`core/newcomers.py`):
     people who wrote to the bot privately but aren't on FreeHand. They
     aren't answered, as before, but their name and chat ID are
     remembered for a day, the last 10 only, so the owner adds them in
     one click instead of hunting for a chat ID. Groups aren't listed.
   - **Make a sign-in link** for someone, and **sign them out
     everywhere**.
   - **Removing someone** signs them out and stops FreeHand answering
     their chats, and the owner chooses what of theirs to keep ("When
     someone leaves").
   - **On your own computer**, the console still signs itself in with
     FreeHand's API key. The key is always the owner.
   - **A code to your chat:** you choose your name, and FreeHand sends a
     6-digit code to your own Telegram or WhatsApp. The owner's are the
     old allow-from lists. A code is never sent to a group. Nothing to
     remember (the granny test). A code lasts 10 minutes, allows 5 tries
     and works once; a new one can be asked for once a minute.
   - **A one-time link** the owner makes (`POST
     /api/people/<id>/sign-in-link`), for someone with no chat linked.
     It works once, within 24 hours.
   - **A session** per device, kept only as a hash. It lasts 30 days
     from its last use and stops at once if the person is removed from
     settings. The owner can sign someone out everywhere.
   - **Signed in, the console is that person's.** Every request runs as
     them, so the separation below applies.
   - **A member only reaches their own screens** (`MEMBER_ROUTES` in
     `server.py`): chat, jobs, approvals, messages, memory and the pad.
     Everything else answers "Only the owner can do this", and the page
     hides those screens. It's a list of what's allowed, so a new screen
     is the owner's until someone decides otherwise.
   - **A member's chat message is always their own console chat.** They
     can't say it came from someone's Telegram.
   - **A member's browser doesn't change FreeHand's time zone.**

   On a hosted FreeHand this sits behind the office password at the
   front door (Caddy), which stays.
2. **Private memories and the shared pool:**
   - FreeHand recalls with `person:<me>` or `shared`;
   - "share this" saves with `shared`;
   - the Memory page shows yours and the shared pool separately.

   Before turning on filtering, tag the existing untagged memories
   `person:owner`, so they don't vanish from recall.

   Recall must ask for a **strict** tag match (`any_strict`, `all_strict`
   or `exact`). Hindsight's default, `any`, also returns memories with no
   tags, and a mistyped mode quietly falls back to it. Test that Thandi
   can't recall an untagged memory, and that Hindsight's summaries (built
   from several memories) don't mix a private one into someone else's
   answer: save a private and a shared memory on the same topic, recall
   as another person, and look. If summaries leak, use a memory bank per
   person plus one shared bank instead: simpler to trust, but more to run.

   **Built (November 2026): separation.** Recall, the Memory page,
   correcting and forgetting (also "forget that ..." in chat) reach only
   the person's own memories and the shared pool (`long_memory._scope`).
   Someone else's memory is "not found".
   - A member's match is `any_strict`, so untagged memories never reach
     them.
   - The owner's match is `any`, so it also takes the untagged ones.
     Those are from before people existed, so they're the owner's,
     the same rule as the rows in the database. That replaced tagging
     them: no one's existing memory bank is changed.
   - **Checked against a real Hindsight**, in a throwaway bank. With an
     owner's, a member's, a shared and an untagged memory on overlapping
     topics, the member recalled only their own and the shared facts and
     summaries. The owner recalled theirs, the shared and the untagged,
     and none of the member's. Hindsight builds summaries per tag set
     ("combined" scoping), so they don't mix. No separate banks needed.

   Not built yet: "share this" (saving with `shared`), and the Memory
   page showing yours and the shared pool separately.
3. **Chats, tasks and approvals filtered by person.** Each person sees only
   their own. Administrators can see approvals for shared connections.
   Scheduled tasks run as the person who made them.

   **Built (November 2026):**
   - **Chats:** chat history is the person's own, even in a shared chat;
     starting afresh clears only their turns.
   - **Jobs:** the Scheduled jobs screen, pausing, deleting and the
     one-time tidy show and touch only the person's own jobs. Anyone
     else's is "not found".
   - **Approvals:** a member sees, answers and clears only their own.
     For now the owner still sees and answers everyone's
     (`people.approvals_scope`). The console is the owner's until each
     person signs in (step 1), and a risky request can only be approved
     there, not from chat. With sign-in, this narrows to administrators
     seeing approvals for shared connections.
4. **Workspaces:** a scribble pad and notes folder each, plus a shared folder.

   **Built (November 2026): a folder each.** The owner's pad, Threads and
   Journal stay where they always were, in the vault. Everyone else's are
   in `vault/people/<id>/`, so the folder name `people` in the vault is
   FreeHand's. Writing on the pad (`/scribble`, the console) goes to the
   person's own pad.
   - **The sweep** goes through each person's pad as that person, so
     their entries become their own tasks, Threads and Journal.
   - **Searching notes, the Memory page's files and the MCP resources**
     show only the person's own (`people.may_read_vault_file`): a member
     their folder, the owner everything except other people's folders.
     Someone else's file is "not found".

   Not built yet: the shared folder.
5. **Personal and shared connections:**
   - OpenConnector connections named per person (for example `thandi-work`);
   - a Composio user ID per person;
   - FreeHand only lets each person use their own connections and the shared
     ones.

   **Built (November 2026): whose each account is** (`core/accounts.py`).
   - **An account is one person's, or shared.** Every account is the
     owner's until the owner says otherwise, so nothing becomes shared
     by accident. On the Connections screen, once there's someone
     besides the owner, each account has "Who can use it": the owner
     only, one person only, or everyone (shared). That's kept in
     settings (`account_owners`).
   - **A person's account is theirs alone.** The owner can't use
     Thandi's either.
   - **The check is made twice.**
     - Before FreeHand asks anyone to approve a step, so nobody
       approves something that can't run.
     - Where each account is actually used, so no route gets past it:
       OpenConnector (`execute_action`, which FreeHand's own email,
       calendar, sheets and Zoom tools use too), Composio, email on
       your own domain, and FreeHand's own sign-ins (Facebook, GitHub).
   - **An approved step runs as the person who asked**, not whoever
     approved it.
   - **A person isn't offered** tools for accounts they may not use,
     and isn't told those accounts' names.

   Not built yet: a member connecting their own account themselves. For
   now the owner connects it and chooses "Thandi only".
6. **Roles and permissions:** what members may do, set per person.

   **Built (November 2026): the limits every member has**
   (`core/member_limits.py`). There's no chat tool that changes
   settings, the AI model, the security level or connections. Those are
   console screens, already the owner's (`MEMBER_ROUTES`). These were
   the gaps in chat:
   - **The browser:** a member's browser is a clean one. It never drives
     the owner's own Chrome, with the owner's sign-ins.
   - **Saved browser states and request recordings** (checkpoints,
     request capture) are the owner's. They can hold a site's sign-in.
   - **Documents** (Word, Excel, PowerPoint): a member reads and writes
     only in their own folder, not the vault or the owner's extra
     folders. The owner doesn't reach into a member's folder either.
   - **A change on a shared account** (sending from info@, posting on
     the office page) waits for the owner's approval, even at Full
     access. The member can't approve it, in the console or from chat,
     but can cancel it. Reading a shared account needs nobody.

   **Built (November 2026): team members per person, and sharing them**
   (`core/team.py`). Each team member is its maker's; ones from before
   are the owner's. Members have the Team page for their own.
   - **Sharing:** with everyone, or with named people. The owner always
     can; members only if the owner switches it on under People.
     Switched off, members' shares stop at once.
   - **Only the maker** changes, pauses or deletes one.
   - **Someone using a shared team member uses it as themselves:** their
     own accounts, memories and chat. The log shows each person only
     their own requests. The monthly limit is for everyone together.
   - **Names** only need to be unique among the team members a person
     can use, so two people can each have their own "Writer".
   - **When someone leaves,** their team members go with their jobs:
     kept for the owner (unshared) or deleted.

   **Built (November 2026): a monthly limit on AI per person**
   (`core/ai_use.py`). On the People page the owner sets how often each
   member may use AI in a month. Empty means no limit, and the owner has
   none.
   - **One use** is one message FreeHand answered with AI: in a chat, a
     scheduled job, or through a team member, however many steps it
     took. Reminders and lists don't count.
   - **Over the limit,** FreeHand answers without calling the AI and
     says when it starts again. Their scheduled jobs wait, and say so
     once.
   - **Everyone's use this month** shows on the People page, and a member
     sees their own next to their name.
   - **The "all jobs together" limit** under Scheduled jobs now counts
     each person's jobs on their own, so one person's jobs can't use up
     everyone's.
7. **Families:** the child role, parents' approvals, limits and oversight
   (see "Families and children" below).
8. **When someone leaves** (see below).

   **Built (November 2026)** (`core/leaving.py`). Remove on the People
   page opens a short form instead of a yes/no box:
   - **It shows what they have:** notes and files, scheduled jobs,
     memories, and accounts that were theirs.
   - **Notes and files:** kept by default, in a folder "From <name>" in
     the owner's vault, or deleted.
   - **Scheduled jobs:** kept by default, as the owner's, paused, and
     never sent to the person's old chat. Or deleted.
   - **Memories:** deleted by default, or given to the owner. Each thing
     remembered is one Hindsight document, and changing or deleting it
     reaches its facts and summaries (checked against a real Hindsight).
     What they shared stays shared.
   - **Accounts that were theirs:** disconnected by default, or given to
     the owner.
   - **Always:** they're signed out, FreeHand stops answering their
     chats, and their chats and waiting approvals are deleted.
   - **Deleting everything about them** (a POPIA request) is unticking
     every "Keep".
   - **Nothing is left under their id, and the id is never given to
     anyone new** (`former_people` in settings, and any folder an older
     FreeHand left under `people/`). So a newcomer with the same name
     can't inherit anything.
   - **If memory can't be reached,** the rest is still done, and the
     person is listed with Finish, to do their memories later.

   Not built: telling the person what was kept. The page reminds the
   owner to.

The isolation tests were written first, and failed until each piece was
built (`tests/test_people_isolation.py`, November 2026). As Thandi,
nothing of the owner's may appear in chats, the Scheduled jobs screen,
approvals (seeing, answering or clearing them), recalled memories, the
Memory page or the scribble pad, also through work moved to another
thread and through her scheduled jobs. **All of them pass now.** A piece
still to build (connections, for example) gets its test first, marked
`NOT_BUILT`, and the marker comes off when it passes. Tests that Thandi
still has her own chats, jobs, approvals, memories and notes keep the
separation from being "built" by hiding everything from everyone.

## When someone leaves

Removing a person is a decision, not just deleting a line of settings, so
FreeHand asks the administrator what should happen and then does all of it:

- **Straight away:** their chat links (Telegram, WhatsApp, Slack) and
  their sign-in stop working, and their personal connections (their own
  mailbox, calendar and other sign-ins) are disconnected, so nothing can
  act as them any more. Their scheduled jobs stop (they already don't run
  for someone who's no longer listed).
- **The administrator chooses** what of theirs is kept for the office (for
  example their tasks, or notes they wrote for the team) and what is
  deleted. What they shared stays shared. Their private chats and
  memories are deleted unless the administrator keeps them, and the person
  should be told which.
- **Delete everything about a person:** one action that removes their
  chats, tasks, approvals, memories, files and connections. A privacy
  request under POPIA will need it.

**Recycled phone numbers.** WhatsApp links are matched by number, and
phone companies give old numbers to new people. Removing a person's links
when they leave is the main protection. For more, a new number could be
asked for a one-time code from the console the first time it writes.

## Families and children

A family is a small office: parents are the administrators, and children
are members with extra limits. It uses the same people, roles and tags as
above, plus a **child** role linked to one or more parents.

### Two kinds of control

**1. Hard limits, enforced by FreeHand's code.** These are the dependable
ones: FreeHand blocks the action itself, so clever asking can't get round them.

- **What FreeHand can do for the child.** Each child has a list of allowed
  tools and connections. For example: homework help, questions and their own
  scribble pad are allowed. Sending email, posting to social media, purchases
  and the family's connections are not.
- **Anything that reaches outside asks a parent.** If a child asks FreeHand to
  send or post something, the approval goes to a parent (for example on their
  phone through Telegram), never to the child.
- **Time limits:** quiet hours (for example, nothing after 9 pm on school
  nights) and a daily message limit.
- **Spending limits:** a daily cap on what the child's messages may cost with
  the AI model service.

**2. Guidance for the AI.** Answers suited to the child's age, gentler topics
for young children, helping them think rather than handing over homework
answers, and never asking for personal details such as an address. This helps,
but it isn't watertight: AI models can sometimes be talked round, which is why
the hard limits come first.

### How much parents see

There's a trade-off between oversight and the child's privacy, so this is a
choice for each child:

- **Full view:** parents can read the chats. This suits young children.
- **Summaries:** a weekly "what they used FreeHand for". This suits teenagers.
- **Alerts only:** parents are told only when something worrying comes up. If a
  child says they're being hurt or are in danger, FreeHand answers kindly, gives
  helpline numbers, and tells a parent. This depends on the AI noticing, so,
  like the guidance below, it isn't watertight, and the setup says so.

A child's request for a parent's approval that no parent answers is
**refused after a time limit**, never allowed by default. The quiet hours
and limits apply to everything done for the child, scheduled jobs too.

Whichever is chosen, FreeHand tells the child honestly what their parents can
see. Hidden monitoring breaks trust.

### Age rules

- **The AI model service's minimum age.** Most services set one in their terms,
  often 13 and sometimes 18. That limit comes from the service, not FreeHand,
  so a family needs a service whose terms allow children.
- **Privacy law.** Children's personal information has extra protection, such
  as POPIA in South Africa, where a parent must consent.

Setting up a child is therefore a parent's deliberate choice. FreeHand explains
these points when a child is added.

### How it fits the code

- The `child` role sits in `people` settings, with `parents: [...]` and a small
  rulebook:
  - `allowed_tools` and `allowed_connections`
  - `quiet_hours` and `daily_message_limit`
  - `daily_spend_limit`
  - `oversight`: `full`, `summary` or `alerts`
  - `age_band`
- It's enforced in the agent loop. The loop already knows whose message it's
  handling (`people.current()`), so it:
  - filters the child's tools before the model sees them;
  - sends approvals to the parents;
  - turns messages away during quiet hours, or once a limit is reached, with a
    friendly explanation.
- The child's guidance is added to the system prompt for that person only.

## Questions for the first office

- Can a manager see their team's tasks or chats, or only approvals?
- Which connections are shared, and who may send from them?
- Should the shared pool need approval before something is added?
- Does anyone use FreeHand only by Telegram or WhatsApp, never the console?

## Why not one FreeHand per person?

That already works (`scripts/provision_tenant.py`), and it's the right choice
when people share nothing: for example, separate clients. It can't give an
office a shared pool or shared connections, which is why this plan exists.

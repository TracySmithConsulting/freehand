# Scheduled jobs and team members: doing more for less

Status: design agreed (Nov 2026). Step 1 is built: results are delivered
(core/notices.py), a task with no time runs once, and a task that fails
three times in a row stops and says so. Step 2 is built: free "do" jobs
(reminders and the to-do list, core/schedule_job.py), one-off times, the
person's own time zone (from their browser), the schedule_job tool, the
Scheduled jobs screen with plain choices, and the one-time tidy-up of old
tasks. A job the model sets up is saved straight away, because the person
asked for it, and the reply says what was saved; it can be paused or
deleted on the Scheduled jobs screen. Tracy's answers to
the open questions are marked **Decided**, and listed at the end.

Two related ideas:

1. **Scheduled jobs:** things FreeHand does at a time or on a repeat. Plain
   code does the work, and wakes the AI model only when there's thinking to
   do.
2. **Team members:** assistants with one job each, such as marketing or
   sorting email, that the person can give their own names. The main
   FreeHand hands work to them.

Scheduled jobs come first, because team members are built on top of them.

Both follow FreeHand's principles: accessible, the granny test, safe, and
economical (see [Economy](economy.md)).

## Part 1: Scheduled jobs

### What happens today

Found while writing this (core/scheduler.py):

- **Every task goes through the full agent.** When a task is due,
  `run_due_tasks` runs it with `run_agent(..., max_turns=3)`: up to three
  model calls, with all of FreeHand's instructions and tools. That's the
  same for "remind me to phone the dentist" as for "write my weekly report".
- **The answer goes nowhere the person sees.** The run is filed as if it
  came from the command line (`source="cli"`), so the reply lands in that
  conversation's history. It isn't sent to the web chat, Telegram or
  anywhere else. So a reminder never actually reminds anyone.
- **A task with no time runs every night at 2am, forever.** An empty
  schedule becomes `0 2 * * *` (`DEFAULT_CRON`), which is a valid repeat,
  so it's never a one-off. The code's own description says it should be.
- **"Once, at a date and time" can't be said.** Cron (the repeat format)
  can only describe repeats, so "tomorrow at 9" has no way in.
- **The Tasks screen asks for cron.** People have to type something like
  `0 9 * * 1`. That fails the granny test.
- **Checks every 30 seconds**, which is fine: it's a database query, not a
  model call.

So the scheduler costs model calls it doesn't need, and doesn't deliver
what it does. Fixing that is worth it on its own, before team members.

### The idea: decide once, then let code do it

When someone asks for a scheduled job, the model is already reading the
request. At that moment it decides **what kind of job** this is and fills in
the details. After that, code runs the job, and the model is only asked
again if the job needs it.

There are three kinds of job:

| Kind | Examples | Model calls when it runs |
|---|---|---|
| **Just do it** | "Remind me to phone the dentist at 9 tomorrow." "Every Monday, send me my to-do list." "Back up my memories every night." | None |
| **Check, then wake the model** | "Tell me if my bank emails me." "Let me know when this website changes." "Tell me if the price drops below R500." | None, unless the check finds something |
| **Think every time** | "Every Friday, summarise my week's news." "Draft this week's marketing post." | One run, with a limit |

#### Just do it

Code does the whole job. There's a fixed list of actions, each written and
tested in FreeHand:

- **Remind:** send a message, word for word as set up.
- **Send a list:** my tasks, my calendar for today, my shopping list.
- **Run a FreeHand action:** back up, tidy the scribble pad, refresh a
  connection.

No model, no cost, however often it runs.

#### Check, then wake the model

Code does a cheap **check** first. Only if the check finds something does
the model wake up, with just what it needs: the job's instructions and what
the check found. Not the whole of FreeHand's instructions, and not the chat
history.

Checks are a fixed list too:

- **New emails** matching a sender or words.
- **A web page changed** (FreeHand remembers a fingerprint of the page).
- **A number crossed a line** (a price, a balance, a temperature), read
  from a page or a connected service.
- **A file appeared** in a folder.

Most days the check finds nothing, and it costs nothing. When it does find
something, there's a choice per job:

- **Tell me** (no model): "You have a new email from FNB: Statement
  ready." Often that's all anyone needs.
- **Think about it** (model): "Read it, and tell me if I need to do
  anything."

#### Think every time

Some jobs need the model every time, such as writing a summary or a draft.
These run as now, but:

- **with only what they need** (the job's instructions and the tools it
  uses, as in "Team members" below), not everything;
- **with a limit** on steps per run;
- **with their cost shown** when they're set up, and in the jobs list.

### What the person sees

Nobody has to know about "kinds of job" or cron. They just ask:

> Remind me to phone the dentist at 9 tomorrow.

> Done. I'll remind you tomorrow, Tuesday 17 November, at 9:00. (Free,
> no AI needed.)

> Every Friday, summarise the news about load shedding.

> Done. Every Friday at 8:00 I'll send you a summary. This uses AI, about
> R3 a month on your current model.

The **Scheduled jobs** screen (replacing Tasks) lists each job in plain
words:

- what it does, and when ("Every Monday at 7:00");
- "Free" or "Uses AI, about R3 a month";
- when it last ran, and whether it worked;
- buttons: Pause, Change, Delete, Run now.

Making a job on that screen has plain choices ("Once", "Every day", "Every
week on...", "Every month on the...") and a time. No cron. Tinkerers can
still type cron under "More options".

### Where the result goes

Every job's result is **delivered**, which fixes today's biggest gap:

- in FreeHand's chat, as a message from FreeHand, with a sound and a screen
  reader announcement if the page is open;
- and to Telegram, Slack or WhatsApp if they're connected.

**Decided:** by default, both: FreeHand's chat, and wherever the job was
asked for (for example Telegram). The person can change that per job, for
example "Telegram only".

### Safety

- A job can only do what the person could do in chat. Anything that sends,
  posts or deletes still follows FreeHand's permission rules.
- A job made by the model is shown to the person before it's saved, and
  they can say no.
- "Think every time" jobs have a step limit per run and a spending limit
  per month (see "Limits" below).
- Jobs belong to the person who made them (the `person` column already
  exists), so in a shared FreeHand one person's jobs don't run as someone
  else.
- If a job fails three times in a row, it pauses itself and tells the
  person, in plain words, why.

### How it's built (for developers)

- `tasks` table: new columns `kind` (`do`, `check`, `think`), `action` (for
  `do`), `check` (for `check`), `instructions`, `deliver_to`, `run_at` (for
  one-off jobs at a date and time), `limit_steps`, `paused`,
  `fail_count`.
- A tool for the model: `schedule_job`, which fills those in. It returns a
  plain-words summary to show the person.
- `run_due_tasks` looks at `kind`: `do` calls the action, `check` runs the
  check and only then (maybe) the model, `think` runs the model with a small
  context.
- Old rows become `think` jobs, so they keep working as before.
- Fix the 2am bug: no schedule means once, now (or at `run_at`).
- A `deliver()` function, used by all three kinds, that sends to the chat
  and the chosen channels.

**Decided:** tidy up old tasks. Once, after the update, FreeHand looks at
the old tasks and suggests a kind for each, in one list ("These 3 look
like reminders. Make them free?"). The person says yes or no to each, with
checkboxes. Nothing changes without a yes.

## Part 2: Team members

### What people want

Agent tools elsewhere let people build "bots": an orchestrator that hands
out work, a marketing bot, a research bot, an inbox bot. Underneath, each
one is three things:

1. **Its own instructions** ("You look after my business's social media.
   Friendly tone, South African English, no hashtags.").
2. **Its own tools**, only the ones it needs.
3. Often, **its own schedule**.

FreeHand can offer that, in a way that saves money rather than costing
more.

### The idea: a team member is a saved setup, not a bot that's always on

A team member is a small file of settings:

- **Name** and **job**, in plain words.
- **Instructions.**
- **Tools it may use** (for example: Facebook, Instagram, web search, no
  email).
- **Model:** the main one, a cheaper one, or a local one (Ollama).
- **Limits:** steps per run, and spending per month.
- **What it may do without asking** (default: nothing that sends, posts or
  deletes).
- **Scheduled jobs**, if any (from Part 1).

A team member does nothing, and costs nothing, until it's asked or its job is
due.

**Decided:** they're called **team members** (not "helpers", which would
be confused with the FreeHand Helper on the computer). The person gives
each one a name, such as "Thandi" for marketing. FreeHand says both, for
example "Thandi, your marketing team member", until the person is used to
it.

### How work flows

```
 You ──> FreeHand (the main one) ──> Thandi (marketing) ──> result
                 ^                                        │
                 └────────────────────────────────────────┘
```

In words: you talk to FreeHand as usual. When a request is a team member's job,
FreeHand hands it to that team member, with only what the team member needs. The
team member does the work and reports back to FreeHand, which tells you.

You can also talk to a team member directly ("Thandi, draft a post about
our December specials"), or a team member's scheduled job can start it.

### Why it's economical

- **Small context.** A team member gets its own short instructions and only its
  tools, not FreeHand's whole instructions, all the tools, the memories and
  the chat. That can be a tenth of the tokens.
- **Cheaper models for narrow jobs.** Sorting emails or a first draft
  doesn't need the best model. A team member can use a cheaper one, or a local
  one, while the main FreeHand uses the good one to plan.
- **Limits per team member.** Each team member has a step limit per run and a
  spending limit per month. If it hits one, it stops and says so.
- **No team members talking to team members.** Only the main FreeHand hands out work.
  A team member can't call another team member. This avoids the runaway loops and
  bills that multi-bot setups are known for.
- **Costs shown per team member**, on the team members screen: "Thandi (marketing):
  about R45 this month, 12 runs."

### Why it's safe

- A team member can't do more than its tools allow, and never more than the
  person could do themselves.
- Anything that sends, posts or deletes still asks the person, unless they
  switched on "may do this without asking" for that team member and that tool.
  That switch is per tool, and off by default.
- A team member's instructions can't change FreeHand's safety rules. They're
  added to FreeHand's own, not instead of them.
- A team member sees only the memories it's been given ("may read: my business
  notes"), not all of them.
- Everything a team member does is logged in plain words, under the team member's
  name.

**Decided:** it's the person's choice, per team member and per tool:

- **Ask me first** (the default). The team member prepares the post or
  message and sends it to the person to approve: "Thandi has a post ready
  for Facebook. Read it, then Post it, Change it or Don't post." Nothing
  goes out without a yes. Tracy's own choice.
- **Just do it.** The team member posts or sends by itself, for example
  every Monday while the person sleeps. Switching this on says plainly what
  it means ("Thandi will post to Facebook without asking you"), and needs
  a yes. Everything it does is still logged, and the person is told
  afterwards.

Deleting always asks, whichever is chosen.

### What the person sees

In Settings, **My team members**:

- a list of team members, each with its job, its cost this month, and Pause,
  Change, Delete;
- **Add a team member**, starting from a template, or from scratch.

Templates to start with:

| Template | Job | Tools | Model |
|---|---|---|---|
| Marketing | Drafts social media posts in your voice, including scripts and captions for videos, for you to approve. | Facebook, Instagram, web search | Main |
| Email sorter | Reads new email and tells you what needs you. | Email (read only) | Cheap or local |
| News summary | A short summary of a topic, on a schedule. | Web search | Cheap |
| Research | Finds and compares information, with sources. | Web search, browser | Main |
| Developer | Watches your code projects on GitHub, and tells you about new issues and pull requests, and when tests fail. | GitHub (read only) | Cheap |

The Developer template is mostly a "check, then wake" job (Part 1): code
checks GitHub for anything new, which is free. The model only wakes to
explain something when the person asks, or when they've chosen "summarise
it for me".

The person names each one when they add it. Anything the templates don't
cover, they make from scratch.

Making a team member is a short form with plain questions, in this order: what
should it be called, what's its job, which tools, how much may it spend.
Tinkerers get "More options": the model, the step limit and the full
instructions.

You can also make one in chat:

> Make me a team member that drafts a Facebook post every Monday about our
> weekly specials.

FreeHand fills in the form, shows it, and asks before saving.

### How it's built (for developers)

- Team members live in `vault/team/<name>.json` (instructions, tools, model,
  limits, permissions), one file each, so they're easy to back up, share
  and edit.
- `run_agent` gets a `team_member` option: it builds the system prompt from the
  team member's instructions plus FreeHand's safety rules, offers only the
  team member's tools, uses the team member's model, and applies its limits.
- A tool for the main FreeHand: `ask_team_member(name, request)`, offered only
  when team members exist. Team members don't get it.
- Spending: each run's tokens (already counted, see Economy) are added to
  the team member's monthly total, and turned into an estimate in the
  person's currency from the model's price (see "Limits").
- Scheduled jobs (Part 1) get a `team_member` column, so a job can run as a
  team member.

## Limits

Defaults, which the person can change:

| Limit | Default |
|---|---|
| Steps per run, "think" job | 5 |
| Steps per run, team member | 8 |
| Spending per team member per month | US$3 (about R50) |
| Spending on all scheduled jobs per month | US$6 (about R100) |
| Failures in a row before pausing | 3 |

When a spending limit is reached, the job or team member pauses and the
person is told: "Thandi has used this month's R50 and has paused.
Raise the limit, or Thandi starts again on 1 December."

**Decided:** the defaults stand for now, and are changed once there's real
use to go on. Money is shown in **the person's own currency**:

- Model prices are in US dollars, so limits are kept in US dollars inside
  FreeHand, and shown in the person's currency.
- The currency is guessed from the computer's region (South Africa: Rand)
  and can be changed in Settings.
- The exchange rate is fetched once a week from a free exchange-rate
  service, which needs no account and is sent nothing about the person.
  If that fails, FreeHand uses the last rate it had, or the person can
  type one in.
- Amounts are always "about", because the model service's own bill is the
  real figure.

## The order to build in

1. **Fix what's broken:** deliver results, and fix the 2am bug. Small, and
   worth doing straight away.
2. **"Just do it" jobs, and one-off times** ("tomorrow at 9"), with the
   `schedule_job` tool and the new Scheduled jobs screen.
3. **"Check, then wake" jobs**, starting with new emails and changed web
   pages.
4. **Small context and limits for "think" jobs.**
5. **Team members:** the settings files, `run_agent` with a team member, the team members
   screen and the templates.
6. **`ask_team_member`**, so the main FreeHand hands out work.

Each step is its own pull request, with tests, and is useful on its own.

## Decisions (Tracy, Nov 2026)

1. **Where results go:** both FreeHand's chat and where the job was asked
   for, by default; the person can choose per job.
2. **Old tasks:** tidy them up, once, with a yes for each.
3. **The name:** team members, and the person names each one.
4. **Posting without asking:** the person's choice. "Ask me first" is the
   default (and Tracy's choice); "Just do it" can be switched on.
5. **Limits:** the defaults are fine for now. Money is shown in the
   person's own currency.
6. **Templates:** the four, plus a Developer team member that watches
   GitHub. Video scripts and captions go under Marketing. Anything else,
   people make from scratch.

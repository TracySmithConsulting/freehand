# Scheduled jobs and helpers: doing more for less

Status: design only (Nov 2026). Nothing here is built yet. Questions for
Tracy are marked **Question** and gathered at the end too; answer them in
this file.

Two related ideas:

1. **Scheduled jobs:** things FreeHand does at a time or on a repeat. Plain
   code does the work, and wakes the AI model only when there's thinking to
   do.
2. **Helpers:** named assistants with one job each, such as a "Marketing
   helper" or an "Email sorter". The main FreeHand hands work to them.

Scheduled jobs come first, because helpers are built on top of them.

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
what it does. Fixing that is worth it on its own, before helpers.

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
  uses, as in "Helpers" below), not everything;
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
- and to Telegram, Slack or WhatsApp if they're connected, as chosen per
  job (default: wherever the job was asked for).

**Question:** If someone asked on Telegram, should the reminder come back
on Telegram only, or on Telegram and in FreeHand's chat?

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
- Old rows become `think` jobs, so nothing changes for them until they're
  edited.
- Fix the 2am bug: no schedule means once, now (or at `run_at`).
- A `deliver()` function, used by all three kinds, that sends to the chat
  and the chosen channels.

**Question:** Should old tasks (made before this) be left as "think" jobs,
or should FreeHand offer once to tidy them up ("This looks like a
reminder. Make it free?")?

## Part 2: Helpers

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

### The idea: a helper is a saved setup, not a bot that's always on

A helper is a small file of settings:

- **Name** and **job**, in plain words.
- **Instructions.**
- **Tools it may use** (for example: Facebook, Instagram, web search, no
  email).
- **Model:** the main one, a cheaper one, or a local one (Ollama).
- **Limits:** steps per run, and spending per month.
- **What it may do without asking** (default: nothing that sends, posts or
  deletes).
- **Scheduled jobs**, if any (from Part 1).

A helper does nothing, and costs nothing, until it's asked or its job is
due.

**Question:** Should helpers be called "helpers"? That's also the name of
the FreeHand Helper on your computer, which could confuse. Other ideas:
"assistants", "team members", "workers".

### How work flows

```
 You ──> FreeHand (the main one) ──> Marketing helper ──> result
                 ^                                          │
                 └──────────────────────────────────────────┘
```

In words: you talk to FreeHand as usual. When a request is a helper's job,
FreeHand hands it to that helper, with only what the helper needs. The
helper does the work and reports back to FreeHand, which tells you.

You can also talk to a helper directly ("Marketing helper, draft a post
about our December specials"), or a helper's scheduled job can start it.

### Why it's economical

- **Small context.** A helper gets its own short instructions and only its
  tools, not FreeHand's whole instructions, all the tools, the memories and
  the chat. That can be a tenth of the tokens.
- **Cheaper models for narrow jobs.** Sorting emails or a first draft
  doesn't need the best model. A helper can use a cheaper one, or a local
  one, while the main FreeHand uses the good one to plan.
- **Limits per helper.** Each helper has a step limit per run and a
  spending limit per month. If it hits one, it stops and says so.
- **No helpers talking to helpers.** Only the main FreeHand hands out work.
  A helper can't call another helper. This avoids the runaway loops and
  bills that multi-bot setups are known for.
- **Costs shown per helper**, on the helpers screen: "Marketing helper:
  about R45 this month, 12 runs."

### Why it's safe

- A helper can't do more than its tools allow, and never more than the
  person could do themselves.
- Anything that sends, posts or deletes still asks the person, unless they
  switched on "may do this without asking" for that helper and that tool.
  That switch is per tool, and off by default.
- A helper's instructions can't change FreeHand's safety rules. They're
  added to FreeHand's own, not instead of them.
- A helper sees only the memories it's been given ("may read: my business
  notes"), not all of them.
- Everything a helper does is logged in plain words, under the helper's
  name.

**Question:** Should a helper be able to post without asking at all, even
when switched on? For example a marketing helper posting to Facebook every
Monday while the person sleeps. Or should posting always wait for a yes,
with the post sent to the person to approve?

### What the person sees

In Settings, **My helpers**:

- a list of helpers, each with its job, its cost this month, and Pause,
  Change, Delete;
- **Add a helper**, starting from a template, or from scratch.

Templates to start with:

| Template | Job | Tools | Model |
|---|---|---|---|
| Marketing helper | Drafts social media posts in your voice, for you to approve. | Facebook, Instagram, web search | Main |
| Email sorter | Reads new email and tells you what needs you. | Email (read only) | Cheap or local |
| News summary | A short summary of a topic, on a schedule. | Web search | Cheap |
| Research helper | Finds and compares information, with sources. | Web search, browser | Main |

Making a helper is a short form with plain questions, in this order: what
should it be called, what's its job, which tools, how much may it spend.
Tinkerers get "More options": the model, the step limit and the full
instructions.

You can also make one in chat:

> Make me a helper that drafts a Facebook post every Monday about our
> weekly specials.

FreeHand fills in the form, shows it, and asks before saving.

### How it's built (for developers)

- Helpers live in `vault/helpers/<name>.json` (instructions, tools, model,
  limits, permissions), one file each, so they're easy to back up, share
  and edit.
- `run_agent` gets a `helper` option: it builds the system prompt from the
  helper's instructions plus FreeHand's safety rules, offers only the
  helper's tools, uses the helper's model, and applies its limits.
- A tool for the main FreeHand: `ask_helper(name, request)`, offered only
  when helpers exist. Helpers don't get it.
- Spending: each run's tokens (already counted, see Economy) are added to
  the helper's monthly total, and turned into an estimate in Rands from the
  model's price.
- Scheduled jobs (Part 1) get a `helper` column, so a job can run as a
  helper.

## Limits

Defaults, which the person can change:

| Limit | Default |
|---|---|
| Steps per run, "think" job | 5 |
| Steps per run, helper | 8 |
| Spending per helper per month | R50 |
| Spending on all scheduled jobs per month | R100 |
| Failures in a row before pausing | 3 |

When a spending limit is reached, the job or helper pauses and the person
is told: "Your Marketing helper has used its R50 for November and has
paused. Raise the limit, or it starts again on 1 December."

**Question:** Are these defaults right for the people FreeHand is for? And
should the limits be in Rands, or in the person's own currency (worked out
from their settings)?

## The order to build in

1. **Fix what's broken:** deliver results, and fix the 2am bug. Small, and
   worth doing straight away.
2. **"Just do it" jobs, and one-off times** ("tomorrow at 9"), with the
   `schedule_job` tool and the new Scheduled jobs screen.
3. **"Check, then wake" jobs**, starting with new emails and changed web
   pages.
4. **Small context and limits for "think" jobs.**
5. **Helpers:** the settings files, `run_agent` with a helper, the helpers
   screen and the templates.
6. **`ask_helper`**, so the main FreeHand hands out work.

Each step is its own pull request, with tests, and is useful on its own.

## Questions for Tracy

1. If someone asked on Telegram, should the result come back on Telegram
   only, or on Telegram and in FreeHand's chat?
2. Should old tasks stay as they are, or should FreeHand offer once to
   tidy them up?
3. What should helpers be called, given the FreeHand Helper on your
   computer?
4. May a helper ever post or send without asking, if the person switches
   that on? Or should posts always wait for a yes?
5. Are the default limits right, and should they be in Rands or the
   person's own currency?
6. Which templates matter most to you and your clients? Any missing?

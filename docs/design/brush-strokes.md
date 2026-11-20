# Brush strokes: sharing ways to make FreeHand your own

Status: **decided** (20 November 2026): Tracy chose the suggestion on all
four questions at the end. Step 1 is being built.

## The idea

FreeHand is named for drawing freehand, so the things people add to make it
their own are **brush strokes**. Someone makes one, shares it, and anyone
can add it to their FreeHand with one yes.

Tracy's examples were team-member templates shared on GitHub (like other
agents do now) and voice packs. A brush stroke is any of these four kinds:

1. **A team member:** a ready-made helper, such as "Marketing", with its
   job, its instructions and the tools it may use. Today FreeHand has five
   built-in templates (`core/team.py`); this lets anyone make more.
2. **A skill:** a `SKILL.md` file, the same format Hermes and other agents
   use (`core/skills.py`).
3. **A voice pack:** how FreeHand sounds and speaks. It holds its name,
   its greeting, how it talks (warm, brief, formal, playful...), and which
   reading-aloud voice and speed it prefers.
4. **A look:** colours and text size, from a set FreeHand checks for
   contrast, so a look can't make FreeHand hard to read.

## The rule that keeps it safe: words and settings, never code

The danger isn't the files: it's a stranger's words. A brush stroke's
instructions go to the AI, and a bad one could try to talk FreeHand into
sending someone's email to a stranger, or switching off its own checks.
So a brush stroke may contain **only words and settings**, and FreeHand
treats its words as a guest's, inside its own rules.

A brush stroke **can never**:

- **Run anything:** no programs, scripts, or code in a look (only colour
  values and sizes, checked one by one, never raw CSS or HTML).
- **Reach further than the person allows:**
  - **Tools:** it can't ask for any tool outside FreeHand's own list of
    tool choices (`team.TOOL_CHOICES`).
  - **Asking first:** a team member always arrives set to "Ask me first".
    It can never arrive set to "Just do it".
- **Change FreeHand's safety:** not the permission level, approvals, who
  can use FreeHand, or a child's limits.
- **Carry secrets or connections:** no keys, passwords, accounts, phone
  numbers or memories. Sharing your own team member leaves all of these
  out.
- **Update itself:** an update is a new brush stroke. FreeHand shows what
  changed and asks again.

Code plugins (new tools, real recorded voices) might come later, but only
with their own design and much stronger checks. Not in the first version.

## What a brush stroke looks like

A small folder with a `brush-stroke.json` that says what it is, and its
words. The folder is the same whether it's on GitHub or in a download.

```
marketing-helper/
  brush-stroke.json
  instructions.md
```

`brush-stroke.json`:

```json
{
  "kind": "team_member",
  "name": "Marketing helper",
  "about": "Drafts social media posts in your voice, for you to approve.",
  "made_by": "Tracy Smith",
  "version": "1.0",
  "tools": ["facebook", "instagram", "web"]
}
```

- **The kinds:** `team_member` (with `instructions.md`), `skill` (with
  `SKILL.md`), `voice` (settings and a few lines of how it talks) or
  `look` (colour values and a text size).
- **Size limits:** for example, 20,000 characters of instructions, so a
  brush stroke can't hide pages of words nobody reads.

## Finding them

- **The gallery:** a GitHub repository, for example
  `TracySmithConsulting/freehand-brush-strokes`, with one folder for each
  brush stroke.
  - **In FreeHand:** a **Brush strokes** page lists the gallery, with
    search by kind ("team members", "voices"...).
  - **Joining the gallery:** someone sends a pull request on GitHub, and
    it's added once it's reviewed.
- **From a link:** anyone can also paste a GitHub link to a brush stroke
  that isn't in the gallery. FreeHand says plainly that nobody has
  checked it.

## Before the person says yes

FreeHand checks it first, without AI (so it costs nothing):

1. **Its shape:** the right files, the right kinds, within the size
   limits.
2. **Its tools:** each one must be one of FreeHand's tool choices.
3. **Its words, for warning signs:** for example, "ignore your rules",
   "don't ask", "send to", "password", or a hidden email address or link.
   - **What a match means:** it doesn't refuse the brush stroke. It shows
     the person a CAREFUL warning, with the words quoted.
   - **Why only a warning:** a warning list can't catch everything, so it
     isn't the protection. The rules above are.
4. **For a look, its contrast:** every pair of text and background
   colours must be easy to read (at least 4.5 to 1, the usual
   accessibility standard). A look that fails is refused, saying which
   colours.

Then FreeHand shows one short card, made to be read aloud:

> **Marketing helper**, a team member, made by Tracy Smith. From the
> FreeHand gallery.
>
> Drafts social media posts in your voice, for you to approve.
>
> It can use: Facebook, Instagram, and searching the web. It asks you
> before it posts anything.
>
> [Show its instructions] [Add it] [Don't add it]

- **When it's not from the gallery,** the card says so first: "Nobody
  has checked this one. Read its instructions before you add it."

## After it's added

- **Skills start as drafts.** The skill system already does this: a
  skill earns its place by working.
- **Team members are the person's own:** they can change one, pause it,
  or share it with others, like any team member they made.
- **The source stays visible:** the Brush strokes page lists what was
  added, from where, and when. **Remove** undoes it in one step: a team
  member, skill, voice or look goes, and FreeHand goes back to how it
  was.
- **Updates are offered, never automatic:** when the gallery has a newer
  version, the page says so and shows what changed, and the person
  decides.

## Sharing your own

- **Making one:** a team member's page gets **Share as a brush stroke**.
  It saves the folder as a download. It leaves out the keys, accounts,
  memories, who it's shared with and its log.
- **Your voice and look** can be saved the same way.
- **Sending it to the gallery:**
  - **The usual way:** the page explains how, on GitHub.
  - **For someone new to GitHub:** FreeHand could open GitHub with the
    pull request already filled in. That needs a GitHub account, so it's
    explained in plain words.

## Who may add them

- **The owner:** decides for the whole FreeHand.
- **Members:** by default, they may add a brush stroke for themselves (a
  team member, voice or look). It changes nothing for anyone else.
  - **The owner can switch this off** on the People page. This follows
    Tracy's idea that the admin decides.
- **Children:** can't add brush strokes. A parent can add a voice or look
  for them.
- **Skills:** these change how FreeHand works for everyone, so only the
  owner adds them.

## Voice packs, honestly

FreeHand reads replies aloud with the voices already on the person's
computer or phone, so a voice pack can't bring a new voice with it.

- **What a voice pack says:** which voices it prefers, for example "a
  warm South African English voice, or else any English one". FreeHand
  picks the closest voice the computer has.
- **What it always brings:** its name, greeting and way of talking, which
  work everywhere.

Recorded voices FreeHand could download would be code and large files.
That's a later step, and every download would be asked first.

## How it fits the code

- **`core/brush_strokes.py`:**
  - `check(folder)` returns what it is, the warnings, or why it's
    refused;
  - `add(folder)`, `remove(id)` and `list_added()`;
  - `export_team_member(id)`.
- **Where each kind goes:**
  - **A team member:** through `team.save`, with its tools mapped
    through `TOOL_CHOICES`, and always "Ask me first".
  - **A skill:** into `vault/skills/brush-strokes-imports/<name>/SKILL.md`,
    so the skills system marks it imported and starts it as a draft.
  - **A voice:** saved per person (`voice_pack` in their settings). Its
    lines about how to talk are added to the AI's instructions inside
    FreeHand's rules, the way a team member's are.
  - **A look:** saved per person and applied as checked colour values on
    the page. Never raw CSS.
- **Fetching from GitHub:** only from `github.com` and
  `raw.githubusercontent.com`, only the files a brush stroke may have,
  with a size cap. Nothing is run.
- **Records:** in `agent.db` (table `brush_strokes`): what was added,
  from where, which version, by whom, and when.

## Building it, in steps

1. **Team members and skills,** from a link or a downloaded folder, with
   the checks and the card. Plus **Share as a brush stroke**.
2. **The gallery:** the GitHub repository, its review guide, and the
   Brush strokes page listing it.
3. **Voice packs.**
4. **Looks,** with the contrast check.

## Questions for Tracy

**Decided (20 November 2026): the suggestion on all four.** The gallery is
checked before anything is listed; members may add brush strokes for
themselves unless the owner switches it off; team members and skills
come first, then voices, then looks; and the name stays **brush strokes**.

1. **The gallery:** should it be checked by you (or people you trust)
   before anything is listed? Or open to anyone, with the "nobody has
   checked this" warning? Suggested: checked, because it's what makes
   "From the FreeHand gallery" mean something.
2. **Members adding brush strokes for themselves:** on by default, with a
   switch for the owner? Suggested: yes.
3. **The order:** team members and skills first, then voices, then looks?
4. **The name:** keep **brush strokes**? Suggested: yes. It fits
   FreeHand, it's friendly, and it isn't a word people already know from
   other apps. On the page it would read "Brush strokes: ways to make
   FreeHand your own".

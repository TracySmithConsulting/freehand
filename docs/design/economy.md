# Economy: send the model only what it needs

One of FreeHand's principles, alongside accessibility and the granny test:
**FreeHand must be economical with the AI model.** Everything sent with a
message is text the model has to read, and that the person pays for.
Too much also makes replies slower, and confuses small models.

## What's sent with every message

| Part | How it's kept small |
|---|---|
| FreeHand's instructions | About 1,700 tokens. Imported files are a one-line count per app, not a list. |
| Tools | FreeHand's own tools (about 3,000 tokens) always. Add-on tools, such as Composio's 71 Facebook and Instagram tools (about 26,000 tokens), only when the message or the one before it mentions that service. `load_tools` lets the model fetch a group the keywords missed. |
| Memories | The 15 that matter most for this message (5 in lean mode), not the whole memory. |
| Chat history | The last 6 exchanges, reset after 3 hours of quiet (the last 2 in lean mode). |

A typical message went from about **172,000 tokens to about 5,400**
(November 2026).

## Everyday chat (November 2026, second pass)

Measured with a real setup (`scripts/measure_prompt.py`), an everyday
message was still about 6,800 tokens: every tool (about 4,000), every skill
described (about 1,000), 15 memories (about 600). Now:

| Part | Change |
|---|---|
| Tools | By topic for every model, not only small ones: the core tools always, the rest when the words call for them, and `load_tools` for anything missed. `llm.all_tools: true` sends them all again. Saving skills and forgetting memories are only sent when the words are about them (saving how a skill went comes as soon as a skill is used). Reading a web page goes with searching, not with the form-filling tools. |
| Skills | Up to five that match the message are described; the rest are listed by name. |
| Memories | 8 instead of 15. |
| Emails | Trimmed to sender, subject, date, a preview and read or unread. Outlook sent about 132,500 tokens for 10 emails, so the model only ever saw the first one or two. |

Everyday messages came to about 3,400 to 4,600 tokens. "Thanks, that is all
for now" used 3,574; "What did I get in my inbox today?" went from 33,702
tokens in 6 calls to 17,833 in 3.

`tests/test_everyday_economy.py` fails if the always-sent tools grow past
about 1,300 tokens again.

## Lean mode

For small models, and models running on this computer through Ollama, which
have a small "context window" (how much text they can take in at once):

- **Tools:** only the core group goes every time; the rest go by topic.
- **Instructions:** the tool catalogue is left out, because each tool
  describes itself.
- **Memories and history:** fewer.

It's automatic for local Ollama models. To force it on or off for any model,
set `llm.lean` to `"on"` or `"off"` in settings.

## Checking it

With "Show all settings and tools" switched on, each reply ends with "This
reply used about N tokens (N sent, N written), in N model calls". The
figures come from the model service, or are estimated when it doesn't say.

`tests/test_economy.py` and `test_imported_files_dont_bloat_the_system_prompt`
fail if the bloat comes back.

## When adding something

- **New instructions:** ask whether the model needs it on *every* message.
  If it's only sometimes useful, make it a tool, or load it by topic.
- **New add-on tools** join a group by service automatically. Add keywords
  for the service in `core/tool_selection.py`.
- **New built-in tools** go in a group in `BUILTIN_GROUPS`. Ungrouped ones
  are always sent, so nothing silently disappears, but they cost tokens.

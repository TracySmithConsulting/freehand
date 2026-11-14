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

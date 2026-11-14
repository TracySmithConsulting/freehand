# Models on this computer (Ollama)

Ollama runs AI models on your own computer: free, and private, because
nothing you say leaves the computer. They're slower and less capable than
online models, especially at using tools, so they make a good backup, or a
model for simple chats.

FreeHand manages this in Settings, AI model, **Models on this computer
(Ollama)**.

## Setting it up

1. **Install Ollama** from [ollama.com/download](https://ollama.com/download),
   and open it once from the Start menu. It then runs quietly in the
   background.
2. **Let FreeHand check this computer.** FreeHand runs inside Docker, where
   it can't see your graphics card, so run this once on the computer, from
   the FreeHand folder:

   ```
   powershell -ExecutionPolicy Bypass -File scripts\check-computer.ps1
   ```

   It notes your graphics card, video memory and memory, and Ollama's
   address, in `vault\computer.json`. Nothing is sent anywhere. Add
   `-InstallOllama` to install Ollama as well, if it isn't installed yet.
3. **Press "Check again"** in FreeHand. You'll see:
   - whether Ollama is running;
   - the models already on this computer, and whether each can use tools;
   - up to three models recommended for this computer, best fit first.
4. **Download** a recommended model. A progress bar shows how far it's got,
   and a screen reader hears every 10 percent.
5. **Test it,** then **make it the main model** or **add it as a backup**.
   Making it the main model keeps your previous one as the first backup.

## How the recommendations work

- They only include models that can use tools, because FreeHand needs that.
- **Newer Ollama recommends models itself,** with the video memory each
  needs. FreeHand uses that list, without Ollama's cloud models, and keeps
  the ones that fit your graphics card. Older Ollama gets FreeHand's own
  list instead.
- **Graphics cards:** a model fits a graphics card if its size plus a
  little room fits in the card's memory. Built-in Intel graphics don't
  count, because they share the computer's memory.
- **No suitable graphics card:** only small models (3 GB or less) are
  suggested, and they run on the processor, which is slow.

## Good to know

- **The first answer is slow,** often up to a minute, while the model loads.
  Later answers are much quicker.
- **Lean mode** turns on by itself for local models: FreeHand sends fewer
  tools and shorter instructions, so a small model isn't overwhelmed. See
  [Economy](../design/economy.md).
- **Ollama on another port:** if `OLLAMA_HOST` moves Ollama to another port
  (for example 11435), `check-computer.ps1` records it, and FreeHand uses it.

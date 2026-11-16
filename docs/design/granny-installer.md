# The granny installer: FreeHand for people who've never installed anything

Status: design only (Nov 2026). Questions for Tracy are marked
**Question** and gathered at the end; answer them in this file.

## The goal

Someone who has never used an AI agent, and has never installed anything
more complicated than WhatsApp, gets FreeHand working on their own Windows
computer:

- with one download and a few clicks, or a few key presses with a screen
  reader;
- without a terminal, Docker, Python, or the words "container", "port"
  or "API key" unless they choose to look;
- ending at a FreeHand page that's ready to talk, with an AI model set up.

And updating later is one button.

## What exists today

Found while writing this:

- **FreeHand needs three services** to work properly (compose.yml):
  - **FreeHand** itself;
  - **OpenConnector**: Gmail, Outlook, calendars, Zoom and the other
    connections;
  - **Hindsight**: long-term memory.

  Plus, optionally, the WhatsApp bridge, and Caddy for hosted setups.
- **Only OpenConnector and Hindsight are published as ready-made images.**
  FreeHand's own image is built from the source code on the computer
  (`docker compose up --build`), which needs the code, and a few minutes.
- **The old Windows installer** (`scripts/install.ps1`, and `install.sh`)
  installs Python, Node and the FreeHand package with pip. It doesn't
  install OpenConnector or Hindsight, so connections and memory wouldn't
  work. It downloads from freehand.tracysmith.co.za, which doesn't exist
  yet, and ends by telling the person to type commands.
- **No first-run setup.** A new FreeHand has no AI model, and nothing
  walks the person through getting one. That's the biggest hurdle of
  all, for every kind of install, hosted included.
- **The FreeHand Helper** is a small, signed (once SignPath approves) Windows
  app, with a tray icon, a release pipeline on GitHub, and a fixed
  download address. It's a natural home for an installer.
- **Hosting:** `scripts/provision_tenant.py` already sets up a hosted
  FreeHand for a client, and the Helper pairs with it.

## The options

### A. FreeHand on the person's own computer, with Docker inside

The installer sets up the three services in the background and hides them.

There are two ways to get the container engine:

- **A1. Docker Desktop**, installed through winget.
  - Easy to build.
  - But Docker Desktop shows its own screens: terms to accept, a sign-in
    offer and a dashboard.
  - It needs a restart.
  - It's free only for personal use and small businesses (under 250 staff
    and under US$10 million a year). A client like DBSA would need paid
    licences.
- **A2. FreeHand's own small Linux inside Windows (WSL)**, with the free
  Docker engine in it, and no Docker Desktop.
  - Nothing extra for the person to see.
  - No licence questions.
  - Harder to build: the installer creates and looks after its own WSL
    system.
  - It still needs WSL switched on once. That means a Windows "allow
    changes?" prompt and possibly a restart.

Either way:
- **Download:** about 3 to 4 GB the first time (the images), then much
  less for updates.
- **The computer:** needs about 8 GB of memory to be comfortable, and
  Windows 10 (version 2004 or later) or Windows 11.

### B. FreeHand straight on Windows, without containers

FreeHand runs as an ordinary Windows program.

- **Lighter**, and with nothing to switch on.
- **But OpenConnector and Hindsight don't come as Windows programs.**
  Without them, there are no Gmail, Outlook or calendar connections and no
  long-term memory, until those are rebuilt or replaced. That's a lot of
  work, and it splits FreeHand into two different versions to maintain.

### C. Hosted: nothing to install except the Helper

FreeHand runs on a server, set up by Tracy or another provider.
- **The person** opens a web address, and installs the small Helper only
  if they want help on their computer.
- **The most granny-friendly by far**, and already mostly built
  (provision_tenant.py).
- **But it costs money each month** (the server), someone has to look
  after it, and the person's data is on someone else's server.

## Recommendation

Do **both C and A2**, sharing everything that can be shared:

- **C for the people FreeHand is most for:** people who aren't technical,
  and clients. They get a web address and, if they like, the Helper.
- **A2 for people who want FreeHand on their own computer:** home users
  and tinkerers. One installer, no Docker Desktop and no licence
  questions.
- **The first-run setup is needed by both,** so it comes first.

## The installer, in more detail (option A2)

The installer is the **FreeHand app**: the Helper grows into it. It's one
signed download, `FreeHand.exe`.

1. **Welcome:** what FreeHand is, what it will set up, how much it
   downloads, and how long that takes. "Install" or "Not now".
2. **Checks:** Windows version, memory and free space, and whether WSL is
   switched on. Anything missing is said in plain words, with what to do.
3. **WSL:** if it's off, one Windows "allow changes?" prompt, which the
   person answers. Then "Your computer needs to restart to finish. Save
   your work, then press Restart now". FreeHand carries on by itself
   after the restart.
4. **Setting up:** FreeHand creates its own small Linux (its name is
   "FreeHand"), puts the Docker engine in it, downloads the images, and
   starts them. A progress bar, read out as a percentage, with "Downloading
   about 3 GB: this can take half an hour on a slow connection".
5. **Done:** FreeHand opens in the browser, at its first-run setup.

Afterwards:
- **The tray icon:** Open FreeHand, Help on my computer (the Helper, as
  now), Update FreeHand, Stop FreeHand, Uninstall.
- **It starts with Windows** (it can be switched off), and opens nothing
  until asked.
- **Update FreeHand:** downloads new images and restarts the services. Your
  data stays.
- **Uninstall:** asks "Keep your FreeHand data (memories, connections,
  settings)?" Yes by default, saved in a folder it names.

Everything is reachable with the keyboard and a screen reader, as the
Helper is now.

## First-run setup (needed by every kind of install)

When FreeHand has no AI model yet, the page opens at a short setup, one
question at a time:

1. **"What's your name?"** So FreeHand can greet the person, and sign
   emails with it.
2. **"Choose how FreeHand thinks"**, in plain words, with what each
   costs:
   - **"A free model, to try FreeHand"**: free, slower and less clever.
     It needs a free account; the page explains how, step by step.
   - **"A paid model, the best results"**: about R50 to R200 a month for
     everyday use, paid to the AI company.
   - **"On this computer"** (Ollama): free and private, but it needs a
     powerful computer and a big download.
   - **"I've been given a key"**, for clients whose provider gives them
     one.
3. **The key:** where to get it, with the exact steps, a box to paste it
   into, and "Check it". FreeHand tries it and says plainly if it works.
4. **"Try it":** a first question to ask, already filled in, such as
   "What can you help me with?"

Then optional steps, each skippable: "Connect your email", "Help on my
computer", "Telegram or WhatsApp".

**Question:** Which way of getting an AI model should FreeHand suggest
first for a new person? A free account (for example OpenRouter's free
models)? A paid one you recommend? Or a key you provide to your clients
(and charge for)?

## What has to be built, in order

1. **Publish FreeHand's image.** GitHub builds it on each release and
   publishes it, next to OpenConnector's and Hindsight's, so no install
   needs the source code. Useful for hosting too.
2. **The first-run setup**, in the FreeHand page. It helps every kind of
   install straight away.
3. **The FreeHand app installer (A2):** WSL, the engine, the images, the
   tray icon, updates and uninstall. Built on the Helper and its signed
   release pipeline.
4. **Retire the old installer scripts**, or make them download the new
   installer.
5. **Hosted, polished (C):** a clear way for Tracy to set up a client,
   and a welcome email with their address and the Helper download.
6. **The website** (freehand.tracysmith.co.za) with the download, when
   there's something to download.

Each step is its own pull request, with tests, and is useful on its own.

## Questions for Tracy

1. **Who comes first?** People installing at home (option A), or clients
   on hosted FreeHand (option C)? Or both, as recommended?
2. **Docker Desktop or FreeHand's own WSL?** A1 is quicker to build, but
   shows Docker's own screens and needs paid licences for bigger
   businesses. A2 is more work, but invisible and free. (Recommended: A2.)
3. **The first AI model:** what should FreeHand suggest to a new person?
   (See "First-run setup".)
4. **Mac:** do any of your users or clients need FreeHand on a Mac, now
   or soon?
5. **The Helper becomes the FreeHand app:** happy for the Helper to grow
   into the installer and tray app, with "Help on my computer" as one of
   its parts?
6. **Hosting:** would you offer hosted FreeHand to clients as a paid
   service? That decides how much the hosted setup needs: for example
   billing, and a page for clients to manage their own account.

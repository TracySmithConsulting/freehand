# The granny installer: FreeHand for people who've never installed anything

Status: design agreed (Nov 2026). Step 1 is built: GitHub publishes
FreeHand's image and the WhatsApp bridge's (.github/workflows/images.yml),
and install/compose.yml runs FreeHand from published images only. Step 2
is built (core/first_run.py): while FreeHand has no AI model, its page
opens at a four-step setup (name; how FreeHand thinks: free OpenRouter,
paid OpenRouter, Ollama, or a given key; an optional free NVIDIA backup;
a first question). Every key is tested before it's kept, and NVIDIA is now
one of FreeHand's services. Step 3, part 1 (proving option A2) is done:
see "Proven on a real PC". Tracy's answers are
marked **Decided**, and listed at the end.

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

### Proven on a real PC (16 November 2026)

On Tracy's PC (Windows 11, WSL 2, with Docker Desktop also running), a
test Linux called FreeHand-test ran FreeHand without Docker Desktop:

1. **The system:** Alpine Linux's minimal system (`alpine-minirootfs-3.24.4-x86_64.tar.gz`, 3.7 MB), downloaded from `dl-cdn.alpinelinux.org` and checked against Alpine's published SHA-256, imported with `wsl --import FreeHand-test <folder> <file> --version 2`.
2. **The engine:** `apk add docker docker-cli-compose` from Alpine's own package store: Docker 29.9, Compose 5.6, about 100 MB.
3. **Starting it:** `dockerd`, with its own network ranges (`--bip 172.31.200.1/24 --default-address-pool base=172.31.208.0/20,size=24`) so it can't clash with Docker Desktop or the home network.
4. **FreeHand:** `install/compose.yml` (FreeHand and OpenConnector, for the test) started, and both were healthy.
5. **From Windows:** `http://localhost:18000` reached FreeHand inside it. WSL passes localhost ports through to Windows, so the installed FreeHand will be at `http://localhost:8000` as usual.
6. **The first-run setup:** the new FreeHand showed it (`/api/setup` said `needed: true`).
7. **Tracy's own FreeHand** (in Docker Desktop) kept running throughout. Everything was removed afterwards (`wsl --unregister`).

What the app has to do because of this:

- **Create the folder before `wsl --import`.** WSL says "path not found" otherwise.
- **Keep the engine running itself.** Alpine has no service manager under WSL, so `dockerd` only runs while something holds it. The FreeHand app starts `wsl -d FreeHand -- dockerd ...` hidden, and keeps it while the app runs. It starts with Windows.
- **Make the secrets with real randomness** (.NET `RandomNumberGenerator`). The test used PowerShell's `Get-Random`, which isn't meant for secrets.
- **The images must be public** on GitHub before an install can download them (they were still private at the time of the test, so the test copied them across from the PC's own Docker).
- **Downloads for a real install:** about 4 MB for the system, about 100 MB for the engine, then the images (about 2.7 GB for FreeHand and OpenConnector, and 3.8 GB more for Hindsight).

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

**Decided:** a provider with a good free tier; Tracy asked for the best
one to be worked out. Compared in November 2026, from independent guides
(the providers don't all publish their limits, and the guides don't always
agree). One FreeHand question is usually 2 to 3 AI calls.

| Provider | Free use | For FreeHand |
|---|---|---|
| OpenRouter | 50 requests a day; 1,000 a day after a one-off purchase of US$10 of credits; 20 a minute | The easiest sign-up (a Google account works), one key for many models. About 15 to 20 questions a day free. |
| Google Gemini | Flash models about 20 a day, Flash-Lite about 500 a day (cut in 2026) | Easy for anyone with Gmail, but very little free use, and free use may be used to improve Google's products. |
| Groq | About 1,000 a day, but only about 6,000 to 8,000 tokens a minute | Too tight a minute: a FreeHand message is about 4,000 tokens, so one question can hit the limit. |
| NVIDIA NIM | About 40 a minute, no fixed daily cap | The most free use, and strong models, but a developer sign-up with phone verification, and meant for trying things out. |

So the first-run setup **suggests OpenRouter first**, saying plainly that
the free 50 a day is enough to try FreeHand and that a one-off US$10
(about R180) makes it 1,000 a day. It then **offers a second free provider
as a backup** (NVIDIA NIM, or Groq), because FreeHand already switches to
a backup model when one runs out (Settings, AI model), so two free
accounts go much further than one. The other choices (a paid model, a
model on this computer, a key given by a provider) stay.

"Check it" in the setup tries the key for real, so the person knows it
works whatever the provider has changed since.
## What has to be built, in order

1. **Publish FreeHand's image.** GitHub builds it on each release and
   publishes it, next to OpenConnector's and Hindsight's, so no install
   needs the source code. Useful for hosting too.
2. **The first-run setup**, in the FreeHand page. It helps every kind of
   install straight away.
3. **The FreeHand app installer (A2):** WSL, the engine, the images, the
   tray icon, updates and uninstall. Built on the Helper and its signed
   release pipeline.
4. **The old installer scripts:** `install.ps1` downloads the new
   installer; `install.sh` becomes the one-line Linux install (the same
   services, the free Docker engine, the same first-run setup).
5. **Built (Nov 2026).** **Hosted, polished (C):** a clear way for Tracy to set up a client,
   and a welcome email with their address and the Helper download.
   `provision_tenant.py --start` does it in one command from the
   published images, and prints the welcome (docs/deployment/tenant-provisioning.md).
6. **The website** (freehand.tracysmith.co.za) with the download, when
   there's something to download.

Each step is its own pull request, with tests, and is useful on its own.

## Decisions (Tracy, Nov 2026)

1. **Who comes first:** both, home installs (A) and hosted clients (C).
2. **The engine:** A2, FreeHand's own WSL, with no Docker Desktop.
3. **The first AI model:** OpenRouter suggested first, with a second free
   provider as a backup (see "First-run setup").
4. **Mac and Linux:** on the roadmap, not now.
   - **Linux** is where the tinkerers are, and Linux installs are usually
     one command typed in a terminal. So Linux gets a one-line install
     (`curl ... | sh`) that sets up the same three services with the free
     Docker engine, and the same first-run setup in the browser. That's
     step 4 below (the old scripts become it).
   - **Mac** gets an installer later, when there are Mac users to try it.
5. **The Helper becomes the FreeHand app:** yes.
6. **Paid hosting:** maybe, depending on how FreeHand takes off. So for
   now, hosting needs only what makes it easy for Tracy to set up and
   hand over a client (step 5). Billing and a self-service account page
   wait.

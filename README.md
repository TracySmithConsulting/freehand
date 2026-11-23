# FreeHand

**An AI helper that works for you, on your own computer, and asks before it acts.**

FreeHand is a free, open source AI agent. You ask it things in plain words,
by typing or speaking, in its own window or from WhatsApp, Telegram or
Slack. It can read and write your email, keep your calendar, remember what
matters to you, do jobs on a schedule, and help on your computer. Before it
sends, posts or changes anything, it asks you first.

It's made to be easy for everyone: it works with a screen reader and a
keyboard, and you don't need to have used an AI agent before. It was built
in South Africa, and it's yours: no subscription, and no company in the
middle.

## What it can do

- **Talk:** type or speak, and have replies read aloud. From its own
  window, or from WhatsApp, Telegram and Slack.
- **Your accounts:** email (Gmail, Outlook, or your own domain), calendar,
  and more, connected with their own sign-in pages.
- **Remember:** what you tell it, and what it learns about you. You can see
  and forget anything on the Memory page.
- **Scheduled jobs:** reminders, a daily email summary, checking a page.
- **A team:** helpers with one job each, such as a marketing helper, that
  can only use what you give them.
- **Skills and brush strokes:** ways of doing things you can save, share
  and add from others.
- **People:** your family or office can each have their own FreeHand
  space, with children's settings for parents.
- **Your computer:** the FreeHand Helper for Windows (below).
- **Any AI model:** a model service you choose, or a model on your own
  computer with Ollama.

## FreeHand Helper for Windows

The FreeHand Helper is a small Windows program that lets FreeHand help on
your own computer, whether FreeHand runs on the same PC or on a server. It
uses Windows accessibility (what screen readers use), so it's especially
good with apps and websites that are hard to use with a screen reader.

- **Look:** read a window's buttons, fields and menus, say which have no
  name, take a picture of a window, and check the computer's details.
- **Act:** click, type, press keys, open apps and websites, and switch,
  close or minimise windows.
- **Change:** install, update and remove programs with winget (Windows' own
  installer), and run four known fixes: sound, printer, network and taskbar.

**It asks you first,** in a dialog a screen reader reads, and changes are
asked every time. **Ctrl+Alt+Escape stops it** at once. It never types
passwords, never acts in Windows' security screens or FreeHand's own
console, and can't run commands of its own.

**Download:** [FreeHand.exe](https://github.com/TracySmithConsulting/freehand/releases/download/helper-latest/FreeHand.exe)
(Windows 10 and 11, nothing else to install). When it asks where FreeHand
runs, choose **Somewhere else**. Then, in FreeHand, open Help on my
computer and press Add a computer. More in the
[Helper guide](helper/windows/README.md).

## Install

### Windows: the FreeHand app (easiest)

**Download [FreeHand.exe](https://github.com/TracySmithConsulting/freehand/releases/download/helper-latest/FreeHand.exe)** and open it.
It checks the computer, switches on what Windows needs (one "allow
changes?" and a restart), downloads FreeHand (about 1.8 GB), and opens it
in the browser. No Docker Desktop, terminal or licence needed, and every
screen works with a screen reader. Windows 10 (version 2004 or newer) or
11, with about 12 GB of free space.

Until it's signed, Windows may say "Windows protected your PC": choose
**More info**, then **Run anyway**. More in the
[FreeHand app guide](helper/windows/README.md).

### Linux: one line

```bash
curl -fsSL https://github.com/TracySmithConsulting/freehand/releases/latest/download/install.sh | bash
```

It sets up the same services as the Windows app, from ready-made images,
with the free Docker engine (from your Linux's own packages, after asking).
Then open http://localhost:8000 for the welcome setup. Running the line
again updates FreeHand. Mac comes later.

### Windows: one line (PowerShell)

```powershell
irm https://github.com/TracySmithConsulting/freehand/releases/latest/download/install.ps1 | iex
```

It downloads the FreeHand app above and opens it.

### From PyPI

```bash
pip install freehand-agent
freehand start
```

### From source

```bash
git clone https://github.com/TracySmithConsulting/freehand.git
cd freehand
pip install -e ".[all]"
freehand start
```

## Safety and privacy

- **It asks first.** Sending, posting, paying and deleting wait for your
  yes. Requests from chat apps ask even at the highest permission level, and
  deleting always asks.
- **Your keys stay locked.** Saved sign-ins are encrypted (Fernet:
  AES-128-CBC with HMAC-SHA256), with a key FreeHand makes on its first run
  (`vault/encryption.key`). On Linux and Mac, and inside Docker, only your
  user account can read that file. On Windows without Docker, that setting
  isn't available, so it relies on the folder's normal Windows permissions.
- **Chat apps are closed** until you add who may use them.
- **Nothing is sent to us.** No telemetry and no data collection.

## For developers

The commands, the connection setup, importing from other agents, how it
fits together and the API are in [FreeHand for developers](docs/developers.md).

## Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io),
certificate by [SignPath Foundation](https://signpath.org).

- **What's signed:** the FreeHand Helper for Windows, built by GitHub Actions
  from this repository's public source
  ([workflow](.github/workflows/helper.yml)). Nothing built on a personal
  computer is signed.
- **Authors and reviewers:** the members of
  [TracySmithConsulting](https://github.com/TracySmithConsulting).
  Every change goes through a pull request.
- **Approver:** Tracy Smith. Every release is approved by hand.
- **Privacy:** the Helper connects only to the FreeHand address the person
  pairs it with, and sends only what they allow. It sends nothing to anyone
  else.

The full policy: [Code signing policy](docs/code-signing-policy.md).

## License

Copyright (c) 2026 Tracy Smith Consulting.

FreeHand is free and open source under the GNU Affero General Public License,
version 3 (AGPL-3.0). See [LICENSE](LICENSE) for the legal text and
[Using, sharing and changing FreeHand](docs/LICENSING.md) for a plain-language
guide. Versions 0.1.30 and earlier were released under the MIT licence.

You're welcome to fork FreeHand. If you share your own version, please give it
its own name. See [The FreeHand name](TRADEMARKS.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md)

## Changelog

See [CHANGELOG.md](CHANGELOG.md)

## Links

- **Source:** https://github.com/TracySmithConsulting/freehand
- **PyPI:** https://pypi.org/project/freehand-agent/
- **Issues:** https://github.com/TracySmithConsulting/freehand/issues
- **Setup Guide:** docs/OAUTH_SETUP.md

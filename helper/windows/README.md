# FreeHand Helper for Windows

A small program that lets FreeHand help on this computer. It works with
FreeHand on the same computer (in Docker) or on a server.

**It can look, act and change.** FreeHand can:

- **look:** list the open windows, read a window the way a screen reader
  does (buttons, fields, menus, and which have no names), take a picture of
  a window, and check this computer's details (Windows version, free memory
  and disk space, battery, network);
- **act:** click, type, press keys, switch to, close, minimise and maximise
  windows, and open apps and websites;
- **change:** find, install, update (one, or everything that has an update,
  in one question that lists them all) and remove programs through winget
  (Windows' own installer), and run four known fixes: **sound** (restart the
  sound services), **printer** (clear stuck print jobs), **network** (refresh
  the connection) and **taskbar** (restart Windows Explorer).

It asks you first for everything except looking, and **changes are asked
every time**, never "for 30 minutes". FreeHand can't run its own commands:
it can only pick a program's id or one of the four fixes by name. See
[the design](../../docs/design/computer-helper.md).

## It's becoming the FreeHand app

The Helper is growing into **the FreeHand app**
([the design](../../docs/design/granny-installer.md)). Started for the
first time, it asks where FreeHand runs:

- **On this computer:** it installs FreeHand in its own small Linux inside
  Windows (WSL), with the free Docker engine and no Docker Desktop
  (`Engine.cs`). It checks the computer first, switches WSL on if needed
  (Windows asks "allow changes?", then a restart, after which it carries
  on by itself), downloads and starts FreeHand, and opens it in the
  browser. Then, by the clock: **Open FreeHand**, **Update FreeHand**,
  **Turn FreeHand off** and **on**, and **Uninstall FreeHand** (which offers
  to keep a copy of your data first). It starts with Windows, in the
  background, unless you switch that off.
- **On a server:** only the Helper part is used, as before.

If the Helper is already paired, it carries on exactly as before: no setup.

For checking a build: `--selftest-engine out.json` says what an install
would find on this computer, and `--selftest-setup out.json` builds every
setup screen and reports its words, buttons and first focus, and
`--selftest-pull reports.txt out.txt` feeds recorded download reports
(`docker compose --progress json pull`) through the install's progress
meter and writes what a person would hear. None of them
changes anything.

## Getting it

**Download** [FreeHandHelper.exe](https://github.com/TracySmithConsulting/freehand/releases/download/helper-latest/FreeHandHelper.exe)
and open it. GitHub builds it from the code here, and it will be signed
through [SignPath](../../docs/code-signing-policy.md) once that's set up.
Until then, Windows may say "Windows protected your PC": choose **More
info**, then **Run anyway**.

**Or build it** on the computer: open this folder, and double-click
`build.cmd`. It uses the C# compiler that comes with Windows 10 and 11, so
there's nothing to install, and it makes `FreeHandHelper.exe`.

## Pairing

1. In FreeHand: Settings view on ("Show all settings and tools"), then **Help
   on my computer**, then **Add a computer**. It shows a six-digit code and
   FreeHand's address.
2. In the Helper, type the address and the code, then press **Pair**.
   A FreeHand on the internet (hosted) needs an address starting with
   `https://`; the Helper won't send the code or its key without encryption.
   The site password isn't needed: the code is the proof.

The Helper then runs by the clock, and reconnects by itself.

## Staying in control

- **FreeHand asks first.** The first time it wants to look, a Windows dialog
  asks "allow FreeHand to look?". Yes lasts until you stop it or close the
  Helper.
- **Before it does anything,** a dialog says exactly what, in which window,
  and why, with three buttons: **Allow once**, **Allow in this app for 30
  minutes**, and **Don't allow** (Escape). Nobody answering counts as no.
  Opening an app always asks.
- **Before typing, FreeHand must look** at the window, so it knows what
  opened (Notepad, for example, can reopen a file from last time). The
  dialog then says exactly where the text will go.
- **Files that look secret** (like `.env`, or with "password", "token" or
  "secret" in the name) get a CAREFUL warning, always ask, and can't be
  allowed for 30 minutes.
- **Windows' own prompt.** Most fixes, and some installs, then show
  Windows' "Do you want to allow this app to make changes?". That's Windows
  itself; only you can answer it, and FreeHand can't see or press it.
- **Never:** typing into password fields, acting in Windows' security and
  sign-in screens, acting in FreeHand's own console (so it can't approve its
  own requests), or opening command tools like PowerShell.
- **Ctrl+Alt+Escape stops it** from anywhere, at once. So does the Stop
  button, and Stop in the icon's menu by the clock. Press "Let FreeHand help
  again" to undo.
- **Password fields** are never read, and are blacked out in pictures.
- **The list** in the Helper shows everything FreeHand asked for, and why.
- **Unpair** in the Helper, or **Remove** in FreeHand, ends access.

The pairing is kept in `%APPDATA%\FreeHand Helper\settings.json`, with the
key encrypted for your Windows account.

## Checking a build

`FreeHandHelper.exe --selftest out.json` looks at the window in front
without connecting or asking, and writes what it saw to `out.json`.
`--selftest-open notepad out.json` opens an app, reads it straight away and
works out where typing would go (without typing), with timings.
`--selftest-change out.json` checks this computer's details, searches for a
program, and shows what an install and a fix would ask, changing nothing.

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

## Getting it

There's no download yet: signed copies will come from FreeHand's releases,
signed through SignPath. For now, build it on the computer:

1. Open this folder (`helper\windows` in the FreeHand folder).
2. Double-click `build.cmd`. It uses the C# compiler that comes with
   Windows 10 and 11, so there's nothing to install. It makes
   `FreeHandHelper.exe`.
3. Open `FreeHandHelper.exe`. Windows may warn that it's from an unknown
   publisher, because this copy isn't signed.

## Pairing

1. In FreeHand: Settings view on ("Show all settings and tools"), then **Help
   on my computer**, then **Add a computer**. It shows a six-digit code and
   FreeHand's address.
2. In the Helper, type the address and the code, then press **Pair**.

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

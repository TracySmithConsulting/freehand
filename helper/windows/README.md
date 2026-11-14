# FreeHand Helper for Windows

A small program that lets FreeHand help on this computer. It works with
FreeHand on the same computer (in Docker) or on a server.

**Stage 1: it can only look.** FreeHand can list the open windows, read a
window the way a screen reader does (buttons, fields, menus, and which have
no names), and take a picture of a window. It can't click, type or change
anything yet. See [the design](../../docs/design/computer-helper.md).

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

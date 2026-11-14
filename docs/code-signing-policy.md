# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io),
certificate by [SignPath Foundation](https://signpath.org).

## What is signed

The **FreeHand Helper for Windows** (`FreeHandHelper.exe`, from
[helper/windows](../helper/windows)). It's built by GitHub from the public
source in this repository, by the
[FreeHand Helper workflow](../.github/workflows/helper.yml), and signed only
from that build. Nothing built on a personal computer is signed.

## Who

- **Authors and reviewers:** the members of
  [TracySmithConsulting](https://github.com/TracySmithConsulting) on GitHub.
  Every change goes through a pull request.
- **Approver:** Tracy Smith. Each release is a `helper-v*` tag pushed by an
  approver, and each signing request is approved in SignPath.

Everyone with these roles uses two-step sign-in on GitHub and SignPath.

## Privacy

The FreeHand Helper connects only to the FreeHand address the person pairs
it with, and sends only what that FreeHand asks for and the person allows:
what's on screen, and the results of actions they approve. It sends
nothing to anyone else, including Tracy Smith Consulting and SignPath.
Its pairing is stored on the computer, encrypted for the Windows user.

## Reporting a problem

If you think a signed FreeHand Helper is harmful or was signed by mistake,
open an issue at
[github.com/TracySmithConsulting/freehand/issues](https://github.com/TracySmithConsulting/freehand/issues)
or contact SignPath Foundation.

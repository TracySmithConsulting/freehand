"""The test guard: a pull request must not quietly weaken FreeHand's tests.

Run by .github/workflows/test-guard.yml on every pull request. It compares
the pull request with the branch it's going into and fails when:

- there are fewer tests than before (tests deleted or renamed away), or
- there are more "skip" or "xfail" markers than before (tests switched off).

Both are sometimes right, for example when a feature is removed. Then a
maintainer adds the label "tests-removed-on-purpose" to the pull request,
and the guard passes. Only people who can merge can add labels, so a
contributor (or their AI) can't wave a weakened test through.

Counting is done by reading the test files, not by running them, so it
needs nothing installed and takes seconds.

    python scripts/guard_tests.py <base-ref> [<head-ref>]
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

OVERRIDE_LABEL = "tests-removed-on-purpose"
SKIP_PATTERN = re.compile(r"pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail)\(")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True, encoding="utf-8").stdout


def list_test_files(ref: str) -> list[str]:
    names = _git("ls-tree", "-r", "--name-only", ref, "--", "tests").splitlines()
    return [n for n in names if os.path.basename(n).startswith("test_") and n.endswith(".py")]


def count_tests(source: str) -> int:
    """Test functions, and test methods in Test* classes (as pytest finds them)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    total = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            total += 1
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            total += sum(1 for item in node.body
                         if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"))
    return total


def measure(ref: str) -> dict:
    tests = skips = 0
    for name in list_test_files(ref):
        source = _git("show", f"{ref}:{name}")
        tests += count_tests(source)
        skips += len(SKIP_PATTERN.findall(source))
    return {"tests": tests, "skips": skips}


def verdict(base: dict, head: dict, overridden: bool) -> tuple[bool, str]:
    """(passes, plain-words explanation)."""
    problems = []
    if head["tests"] < base["tests"]:
        problems.append(f"has {base['tests'] - head['tests']} fewer tests ({base['tests']} before, {head['tests']} now)")
    if head["skips"] > base["skips"]:
        problems.append(f"switches off {head['skips'] - base['skips']} more tests with skip or xfail")
    summary = f"Tests: {base['tests']} before, {head['tests']} now. Skip/xfail markers: {base['skips']} before, {head['skips']} now."
    if not problems:
        return True, "The tests are as strong as before. " + summary
    if overridden:
        return True, (f"This pull request {' and '.join(problems)}, but a maintainer has marked it "
                      f"\"{OVERRIDE_LABEL}\". " + summary)
    return False, (f"This pull request {' and '.join(problems)}. Tests protect FreeHand from things breaking "
                   f"again, so they shouldn't be removed or switched off to make a change pass. If it's on purpose "
                   f"(for example a feature was removed), a maintainer can add the label \"{OVERRIDE_LABEL}\". "
                   + summary)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    base_ref, head_ref = argv[1], argv[2] if len(argv) > 2 else "HEAD"
    labels = [x.strip() for x in os.environ.get("PR_LABELS", "").split(",") if x.strip()]
    ok, message = verdict(measure(base_ref), measure(head_ref), OVERRIDE_LABEL in labels)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

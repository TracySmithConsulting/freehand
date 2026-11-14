#!/usr/bin/env python3
"""Create or repair FreeHand's .env with strong, random secrets.

Run this once before `docker compose up`:

    python scripts/setup_env.py

What it does:

- No .env yet: copies .env.example and fills in fresh random values for
  the OpenConnector tokens and encryption key.
- .env already there: keeps everything you set, fills in anything blank,
  and replaces the old public test values (admintest123, runttest123).

The OpenConnector encryption key is replaced only when OpenConnector has
no saved data yet. Changing it after apps are connected would make those
connections unreadable, so in that case the script tells you instead.

Nothing secret is printed.
"""
from __future__ import annotations

import argparse
import base64
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"
OC_VOLUME = "freehand-oc-data"

# Values that were once shipped as defaults and are therefore public.
TEST_VALUES = {
    "OC_ADMIN_TOKEN": {"admintest123"},
    "OC_RUNTIME_TOKEN": {"runttest123"},
    "OC_ENCRYPTION_KEY": {"this-is-a-thirty-two-byte-test-key"},
    # Lets FreeHand's memory service (Hindsight) use FreeHand's AI model
    # through FreeHand, so people set their model up once.
    "FREEHAND_LLM_PROXY_TOKEN": set(),
}


def new_value(name: str) -> str:
    if name == "OC_ENCRYPTION_KEY":
        # OpenConnector needs exactly 32 bytes of key material.
        return base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    return secrets.token_hex(32)


def oc_has_data() -> bool:
    """True if OpenConnector has saved data (or Docker can't say)."""
    try:
        exists = subprocess.run(
            ["docker", "volume", "inspect", OC_VOLUME],
            capture_output=True, timeout=30, check=False,
        )
        if exists.returncode != 0:
            return False
        # An existing but empty volume holds nothing a new key could break.
        probe = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{OC_VOLUME}:/d:ro", "alpine",
             "test", "-e", "/d/connect.sqlite"],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # can't tell: be safe and keep the key
    return probe.returncode != 1  # 1 = no database; anything else, be safe


def read_lines(path: Path) -> list[str]:
    return path.read_bytes().decode("utf-8").splitlines()


def current_values(lines: list[str]) -> dict[str, str]:
    values = {}
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def plan_changes(values: dict[str, str], oc_data: bool, force_key: bool) -> tuple[dict, list[str]]:
    """Return ({name: new value}, [notes for the user])."""
    changes, notes = {}, []
    for name, tests in TEST_VALUES.items():
        value = values.get(name, "")
        if value and value not in tests:
            continue
        if name == "OC_ENCRYPTION_KEY" and value and oc_data and not force_key:
            notes.append(
                "OC_ENCRYPTION_KEY is still the public test value, but OpenConnector "
                "already has saved data, so it was left alone. Changing it would make "
                "apps connected through OpenConnector unreadable. To start OpenConnector "
                "fresh with a new key: docker compose down, docker volume rm "
                f"{OC_VOLUME}, then run this script again with --new-encryption-key."
            )
            continue
        changes[name] = new_value(name)
    return changes, notes


def apply_changes(lines: list[str], changes: dict[str, str]) -> list[str]:
    out, done = [], set()
    for line in lines:
        s = line.strip()
        key = s.split("=", 1)[0].strip() if "=" in s and not s.startswith("#") else ""
        if key in changes:
            out.append(f"{key}={changes[key]}")
            done.add(key)
        else:
            out.append(line)
    for key, value in changes.items():
        if key not in done:
            out.append(f"{key}={value}")
    return out


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(text.encode("utf-8"))


# ── Memory: is Hindsight already on this computer? (Nov 2026) ─────────
# FreeHand brings its own memory service (Hindsight, compose "hindsight").
# Someone who already runs Hindsight, for example for Hermes, would get a
# port clash, and may want FreeHand to share it. So setup asks once.

HINDSIGHT_PROBE = "http://127.0.0.1:8888"
MEMORY_DECIDED_KEYS = ("FREEHAND_OWN_MEMORY", "FREEHAND_HINDSIGHT_URL")


def _get_json(url: str, timeout: float = 3.0):
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "null")
    except Exception:
        return None


def _is_freehands_own_hindsight() -> bool:
    import subprocess
    try:
        out = subprocess.run(["docker", "ps", "-q", "--filter", "name=^freehand-hindsight$"],
                             capture_output=True, text=True, timeout=10)
        return bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def find_other_hindsight() -> list | None:
    """The banks on a Hindsight that isn't FreeHand's own, or None."""
    if _get_json(HINDSIGHT_PROBE + "/health") is None or _is_freehands_own_hindsight():
        return None
    data = _get_json(HINDSIGHT_PROBE + "/v1/default/banks?limit=100") or {}
    return [{"bank_id": b.get("bank_id"), "facts": int(b.get("fact_count") or 0)}
            for b in (data.get("banks") or []) if isinstance(b, dict) and b.get("bank_id")]


def memory_choice(banks: list, ask=input, say=print) -> dict:
    """Ask how FreeHand should keep its memories; return the .env changes."""
    say("\nThis computer already runs Hindsight, a memory service (perhaps for another app, such as Hermes).")
    if banks:
        say("It has these memory banks:")
        for b in banks:
            say(f"  - {b['bank_id']} ({b['facts']} memories)")
    say("\nHow should FreeHand keep its memories?")
    say("  1. Use that Hindsight, with a new bank just for FreeHand (recommended)")
    say("  2. Use that Hindsight, sharing an existing bank: FreeHand and the other app will remember the same things")
    say("  3. Run FreeHand's own Hindsight instead (uses about 1 GB more memory)")
    while True:
        answer = (ask("Choose 1, 2 or 3 [1]: ") or "1").strip()
        if answer in ("1", "2", "3"):
            break
        say("Please type 1, 2 or 3.")
    if answer == "3":
        # Both can run: FreeHand's own takes other ports.
        return {"FREEHAND_OWN_MEMORY": "1", "HINDSIGHT_PORT": "8890", "HINDSIGHT_CONSOLE_PORT": "9990"}
    shared = {"FREEHAND_OWN_MEMORY": "0", "FREEHAND_HINDSIGHT_URL": "http://host.docker.internal:8888"}
    names = [b["bank_id"] for b in banks]
    if answer == "2" and names:
        for i, name in enumerate(names, 1):
            say(f"  {i}. {name}")
        while True:
            pick = (ask(f"Which bank? 1 to {len(names)}: ") or "").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(names):
                return {**shared, "FREEHAND_MEMORY_BANK": names[int(pick) - 1]}
            say(f"Please type a number from 1 to {len(names)}.")
    new = "freehand"
    while new in names:
        new = new + "-2" if not new[-1].isdigit() else new[:-1] + str(int(new[-1]) + 1)
    name = (ask(f"Name for FreeHand's new bank [{new}]: ") or new).strip()
    return {**shared, "FREEHAND_MEMORY_BANK": name}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or repair FreeHand's .env.")
    parser.add_argument(
        "--new-encryption-key", action="store_true",
        help="Replace a test OC_ENCRYPTION_KEY even if OpenConnector has data "
             "(those connections become unreadable).",
    )
    parser.add_argument(
        "--memory", action="store_true",
        help="Ask again how FreeHand keeps its memories, if Hindsight is already on this computer.",
    )
    args = parser.parse_args(argv)

    created = not ENV_PATH.exists()
    if created:
        if not EXAMPLE_PATH.exists():
            print("Can't find .env.example next to compose.yml.")
            return 1
        lines = read_lines(EXAMPLE_PATH)
    else:
        lines = read_lines(ENV_PATH)

    oc_data = False if created else oc_has_data()
    values = current_values(lines)
    changes, notes = plan_changes(values, oc_data, args.new_encryption_key)

    # Memory: asked once (on a new .env, or with --memory), and only when
    # another Hindsight is already running here.
    decided = any(values.get(k) for k in MEMORY_DECIDED_KEYS)
    if args.memory or (created and not decided):
        banks = find_other_hindsight()
        if banks is not None:
            if sys.stdin.isatty():
                changes.update(memory_choice(banks))
            else:
                notes.append("Hindsight (a memory service) is already running on this computer. Run "
                             "`python scripts/setup_env.py --memory` in a terminal to choose how FreeHand "
                             "shares it. Until then FreeHand's own memory service may not start.")

    if created or changes:
        write_private(ENV_PATH, "\n".join(apply_changes(lines, changes)) + "\n")

    if created:
        print("Created .env with new random passwords for OpenConnector.")
    elif changes:
        memory_keys = {"FREEHAND_OWN_MEMORY", "FREEHAND_HINDSIGHT_URL", "FREEHAND_MEMORY_BANK",
                       "HINDSIGHT_PORT", "HINDSIGHT_CONSOLE_PORT"}
        secrets = sorted(k for k in changes if k not in memory_keys)
        if secrets:
            print("Updated .env: replaced " + ", ".join(secrets) + " with new random values.")
        if any(k in memory_keys for k in changes):
            print("Updated .env: saved how FreeHand keeps its memories.")
    else:
        print(".env is already set up. Nothing changed.")
    for note in notes:
        print("\nNote: " + note)
    if created or changes:
        print("\nNext: docker compose up -d --build")
    return 0


if __name__ == "__main__":
    sys.exit(main())

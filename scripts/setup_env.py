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
}


def new_value(name: str) -> str:
    if name == "OC_ENCRYPTION_KEY":
        # OpenConnector needs exactly 32 bytes of key material.
        return base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    return secrets.token_hex(32)


def oc_has_data() -> bool:
    """True if OpenConnector's data volume exists (or Docker can't say)."""
    try:
        proc = subprocess.run(
            ["docker", "volume", "inspect", OC_VOLUME],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return True  # can't tell: be safe and keep the key
    return proc.returncode == 0


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or repair FreeHand's .env.")
    parser.add_argument(
        "--new-encryption-key", action="store_true",
        help="Replace a test OC_ENCRYPTION_KEY even if OpenConnector has data "
             "(those connections become unreadable).",
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
    changes, notes = plan_changes(current_values(lines), oc_data, args.new_encryption_key)

    if created or changes:
        write_private(ENV_PATH, "\n".join(apply_changes(lines, changes)) + "\n")

    if created:
        print("Created .env with new random passwords for OpenConnector.")
    elif changes:
        print("Updated .env: replaced " + ", ".join(sorted(changes)) + " with new random values.")
    else:
        print(".env is already set up. Nothing changed.")
    for note in notes:
        print("\nNote: " + note)
    if created or changes:
        print("\nNext: docker compose up -d --build")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Provision a FreeHand instance for one customer.

Box-per-customer model: every customer gets their OWN vault directory, OWN
Fernet encryption key, OWN container set, OWN ports, and OWN Caddy vhost with
basic auth. There is no shared state between customers - a customer losing
their box loses only their data, and no customer can read another's OAuth
tokens or sessions.

Why this exists
---------------
FreeHand resolves all per-user state from `VAULT_DIR`, a module-level constant
in `core/agent_config.py` (and again in `core/database.py`). It is computed at
import time and is NOT env-var driven. That means you cannot run two customers
out of one repo checkout with different vaults - the second process would fight
the first over the same directory.

Containers solve this cleanly: each customer gets their own container set with
its own bind-mounted vault. No FreeHand code changes required.

Why the auth layer
------------------
FreeHand has no login of its own. Without a reverse proxy in front, anyone who
guesses a port on the box can read that customer's vault, with no audit trail.
Caddy provides: TLS termination (needed anyway for Meta OAuth), per-tenant
basic auth, and a per-tenant access log. FreeHand's own port stays bound to
127.0.0.1 and is never exposed directly - the Caddy vhost is the only way in.

Password hashing uses Caddy's own `caddy hash-password` rather than a Python
bcrypt dependency, because the container already has the canonical
implementation and version drift between the two produces hashes Caddy
silently rejects. The plaintext is fed to Caddy on STDIN, never as a command
line argument - argv is world-readable in /proc on a shared VPS. If Caddy is
not running, the script still completes and tells you to hash the password
later.

Usage
-----
    python scripts/provision_tenant.py --customer acme --hostname acme.example.com --port 8100
    python scripts/provision_tenant.py --customer acme --hostname acme.example.com --port 8100 --dry-run
    python scripts/provision_tenant.py --list
    python scripts/provision_tenant.py --destroy acme --confirm

Design rules
------------
- The encryption key is generated HERE, not left to FreeHand's auto-generate,
  so it exists before first boot and can be backed up immediately.
- The key is never printed to stdout. It is written to the vault and to the
  operator backup directory, both mode 0600.
- The basic-auth password IS shown once, at provision time, because the
  operator has to hand it to the customer. Only the bcrypt hash is persisted.
- `--dry-run` prints the plan without touching the filesystem.
- Destructive operations (`--destroy`) require an explicit `--confirm`.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from cryptography.fernet import Fernet
except ImportError:  # pragma: no cover
    sys.exit(
        "cryptography is not installed in this Python.\n"
        "Install it with:  pip install cryptography\n"
        "FreeHand itself requires it, so use the same interpreter you run "
        "FreeHand with."
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
TENANTS_ROOT = Path.home() / "freehand-tenants"
BACKUP_ROOT = Path.home() / "freehand-key-backups"

DEFAULT_BASE_PORT = 8100
CADDY_CONTAINER = "freehand-caddy"

# Ports FreeHand/OpenConnector need per tenant.
RESERVED_PORTS = {8000, 3001, 8765, 5432, 6379, 80, 443}

# Docker container and volume names may only contain [a-z0-9_.-] and must
# not start with a separator. normalise_customer() guarantees the customer
# name is safe; this re-checks the derived suffix before it reaches Docker.
_DOCKER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class ProvisionError(RuntimeError):
    """Anything that should stop provisioning with a readable message."""


def log(msg: str) -> None:
    print(msg, flush=True)


def normalise_customer(raw: str) -> str:
    """Lowercase, hyphen-separated, safe as a directory and Docker name.

    Docker container names may only contain [a-z0-9_.-] and must not start
    with a separator. `str.isalnum()` is Unicode-aware, so "café" passes and
    produces an ILLEGAL container name - Docker rejects it at `up` time
    with a message that says nothing about the real cause. So the character
    class is ASCII-only by construction.
    """
    cleaned = "".join(
        ch if (ch.isascii() and ch.isalnum()) else "-" for ch in raw.strip().lower()
    ).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    if not cleaned:
        raise ProvisionError(
            f"Customer name '{raw}' has no usable characters. "
            "Use letters, digits, or hyphens (e.g. 'Acme Corp' -> 'acme-corp')."
        )
    if len(cleaned) > 40:
        raise ProvisionError(
            f"Customer name is too long ({len(cleaned)} chars, max 40)."
        )
    return cleaned


def container_suffix(customer: str) -> str:
    """Docker-safe suffix for per-tenant container and volume names."""
    safe = customer.replace("-", "_")
    if not _DOCKER_NAME_RE.match(safe):
        raise ProvisionError(
            f"Customer '{customer}' does not yield a legal Docker name "
            f"('{safe}'). Use letters, digits, and hyphens only."
        )
    return safe


def normalise_hostname(raw: str | None) -> str:
    """Validate an FQDN for a Caddy vhost. None means no vhost yet.

    A bare IP is rejected: the shape of an IPv4 address passes the FQDN
    regex, but Caddy cannot obtain a Let's Encrypt certificate for an IP
    address, so accepting one would produce a tenant whose vhost can never
    come up over TLS.
    """
    if not raw:
        return ""
    host = raw.strip().lower().rstrip(".")
    if not re.fullmatch(
        r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+",
        host,
    ):
        raise ProvisionError(
            f"'{raw}' is not a valid hostname. Use a real domain with at "
            "least one dot, e.g. acme.example.com. A vhost needs a domain "
            "so Caddy can obtain a TLS certificate."
        )
    if _is_ip_address(host):
        raise ProvisionError(
            f"'{raw}' is an IP address, not a domain. Caddy cannot obtain a "
            "Let's Encrypt certificate for an IP - use a real hostname "
            "that points at this box."
        )
    return host


def _is_ip_address(host: str) -> bool:
    """True if `host` is an IPv4 or IPv6 literal."""
    import ipaddress

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def tenant_dir(customer: str) -> Path:
    return TENANTS_ROOT / customer


def vault_dir(customer: str) -> Path:
    return tenant_dir(customer) / "vault"


def load_tenants() -> dict[str, dict]:
    """Read every tenant's metadata by scanning the tenants root."""
    found: dict[str, dict] = {}
    if not TENANTS_ROOT.exists():
        return found
    for child in sorted(TENANTS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        summary = child / "TENANT.md"
        port = None
        hostname = ""
        if summary.exists():
            for line in summary.read_text(encoding="utf-8").splitlines():
                low = line.lower()
                if low.startswith("- freehand port:"):
                    try:
                        port = int(line.split(":", 1)[1].strip())
                    except (ValueError, IndexError):
                        port = None
                elif low.startswith("- url:"):
                    hostname = line.split(":", 1)[1].strip()
                    if hostname.startswith("("):
                        hostname = ""
        found[child.name] = {
            "path": child,
            "freehand_port": port,
            "hostname": hostname,
            "vault_exists": (child / "vault" / "encryption.key").exists(),
            "caddy_site": (child / "Caddyfile.site").exists(),
        }
    return found


def port_in_use(port: int) -> bool:
    """Best-effort check that something is already listening on this port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def assert_ports_free(
    customer: str, freehand_port: int, oc_port: int
) -> None:
    claimed: dict[int, str] = {}
    for name, meta in load_tenants().items():
        if name == customer or meta["freehand_port"] is None:
            continue
        claimed[meta["freehand_port"]] = name

    for port in (freehand_port, oc_port):
        if port in RESERVED_PORTS:
            raise ProvisionError(
                f"Port {port} is reserved (Caddy, your dev box, or a system "
                "service). Pick a different --port."
            )
        if port in claimed:
            raise ProvisionError(
                f"Port {port} is already assigned to tenant "
                f"'{claimed[port]}'. Pick a different --port."
            )
        if port_in_use(port):
            raise ProvisionError(
                f"Port {port} is already in use by a running process. "
                "Stop it or pick a different --port."
            )
    if freehand_port == oc_port:
        raise ProvisionError(
            "FreeHand and OpenConnector must be on different ports."
        )


def write_secret_file(path: Path, content: str) -> None:
    """Write a secret file, owner-readable only, with no readable window.

    The naive version - write_text() then chmod 0600 - creates the file
    with the process umask (typically 0644) and only narrows it afterwards.
    On a shared VPS there is a real window in which any local user can read
    a customer's Fernet key and decrypt their whole vault.

    So the file is created with the right mode AT CREATION TIME using
    os.open, which takes the mode atomically.

    On Windows, os.chmod only toggles the read-only attribute - it cannot
    express POSIX 0600 - so the mode check warns loudly instead. Windows
    ACLs are a separate mechanism and are the operator's responsibility.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)

    # A pre-existing file keeps its old mode through O_TRUNC, so tighten it
    # again for the "re-provision over an existing tenant" case.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    if os.name != "nt":
        return

    # Windows can't enforce 0600 via chmod. Check what actually happened so
    # the operator knows the file is not protected the way the VPS will be.
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        log(
            f"  WARNING: {path.name} is mode {mode:o} (others can read). "
            "Windows cannot enforce 0600 via chmod."
        )
        log(
            "           On the Linux VPS this file WILL be 0600. If you "
            "stage it on Windows first, move it across, don't share it."
        )


def build_settings(provider: str, model: str, base_url: str) -> dict:
    """A minimal settings.json. The customer fills in their own API key."""
    llm: dict = {"provider": provider, "model": model}
    if base_url:
        llm["base_url"] = base_url
    return {"llm": llm}


def build_compose(
    customer: str, freehand_port: int, oc_port: int, repo_root: Path
) -> str:
    """A COMPLETE, self-contained compose file for one tenant.

    This deliberately does NOT extend or override the repo's compose.yml.
    Two reasons:

    1. `extends:` resolves relative paths against the file that declares
       them, so the vault bind would point at the repo's own ./vault -
       every tenant would share one vault. The single most important
       property of this script (one vault per customer) is exactly the
       thing `extends` silently breaks.

    2. Compose MERGES `ports` lists between files rather than replacing
       them. An override that adds 127.0.0.1:8100:8000 on top of a base
       that already publishes 127.0.0.1:8000:8000 yields BOTH ports
       bound, and the second tenant to come up collides with the first on
       port 8000. Writing the whole file avoids merge semantics entirely.

    The build context points back at the repo checkout so the image can be
    built from source; the vault bind is relative to THIS directory.
    """
    safe = container_suffix(customer)
    # Compose needs a path with forward slashes; on Windows a raw
    # backslash path breaks the YAML string.
    context = repo_root.as_posix()
    return f"""# Auto-generated by scripts/provision_tenant.py for customer '{customer}'.
# Self-contained: this file does NOT extend the repo's compose.yml, because
# compose merges `ports` lists across files and every tenant must own its
# own ports exactly.
#
# Safe to edit by hand afterwards.

services:
  open-connector:
    image: ghcr.io/oomol-lab/open-connector:latest
    container_name: freehand-oc-{safe}
    restart: unless-stopped
    # 127.0.0.1 keeps OC off the network. Caddy is the only way in.
    ports:
      - "127.0.0.1:{oc_port}:3001"
    environment:
      - PORT=3001
      - OOMOL_CONNECT_ADMIN_TOKEN=${{OC_ADMIN_TOKEN}}
      - OOMOL_CONNECT_RUNTIME_TOKEN=${{OC_RUNTIME_TOKEN}}
      - OOMOL_CONNECT_ENCRYPTION_KEY=${{OC_ENCRYPTION_KEY}}
    volumes:
      - oc-data:/data
    healthcheck:
      test: ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:3001/health"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 30s

  freehand:
    build:
      # The image is built from the repo checkout; the vault below is
      # relative to THIS directory, so each tenant gets its own data.
      context: "{context}"
      dockerfile: Dockerfile
      args:
        # 1000/1000 matches the repo's own compose.yml. Do NOT use 0:0
        # here: the Dockerfile's `groupadd --system --gid $GID` fails
        # because GID 0 (root) already exists in the base image, so the
        # build errors out with "GID '0' already exists".
        UID: ${{UID:-1000}}
        GID: ${{GID:-1000}}
    container_name: freehand-app-{safe}
    restart: unless-stopped
    # Match the in-container user to the host user that owns the vault
    # so the bind mount stays writable. Override in .env if needed.
    user: "${{FREEHAND_USER:-0:0}}"
    depends_on:
      open-connector:
        condition: service_healthy
    ports:
      - "127.0.0.1:{freehand_port}:8000"
    environment:
      # FreeHand talks to OC via the compose service name, never localhost.
      - OOMOL_CONNECT_BASE_URL=http://open-connector:3001
      - OOMOL_CONNECT_CONSOLE_URL=${{OOMOL_CONNECT_CONSOLE_URL}}
      - OOMOL_CONNECT_RUNTIME_TOKEN=${{OC_RUNTIME_TOKEN}}
      - COMPOSIO_API_KEY=${{COMPOSIO_API_KEY:-}}
      - COMPOSIO_AUTH_CONFIG_FACEBOOK=${{COMPOSIO_AUTH_CONFIG_FACEBOOK:-}}
      - COMPOSIO_AUTH_CONFIG_INSTAGRAM=${{COMPOSIO_AUTH_CONFIG_INSTAGRAM:-}}
      - COMPOSIO_BASE_URL=${{COMPOSIO_BASE_URL:-https://backend.composio.dev}}
    volumes:
      - ./vault:/app/vault
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/').read()"]
      interval: 10s
      timeout: 3s
      retries: 5
      start_period: 15s

volumes:
  oc-data:
    # Per-tenant OC data volume. Named (not anonymous) so --destroy
    # can find and remove it.
    name: freehand-oc-data-{safe}
"""


def build_env(customer: str, oc_port: int, console_url: str) -> str:
    """Per-customer secrets. Each gets its own runtime token and OC key."""
    runtime_token = secrets.token_urlsafe(32)
    oc_admin = secrets.token_urlsafe(24)
    # On a POSIX host the container's user must match the host user that
    # owns the vault, or the bind mount is not writable. On Windows there
    # is no meaningful uid, so the defaults apply.
    if hasattr(os, "getuid"):
        uid, gid = os.getuid(), os.getgid()
        uid_gid = f"\n# Match the in-container user to this host user so the\n# vault bind mount stays writable.\nUID={uid}\nGID={gid}\nFREEHAND_USER={uid}:{gid}\n"
    else:
        uid_gid = ""
    # OC_ENCRYPTION_KEY must be exactly 32 bytes (OpenConnector's contract,
    # per docs/deployment/docker-compose.md), so it is generated as 32 raw
    # bytes and base64-encoded - NOT with token_urlsafe(), which produces a
    # 43-character string that is not 32 bytes of key material.
    oc_encryption = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    return f"""# Auto-generated by scripts/provision_tenant.py for customer '{customer}'.
# Generated {datetime.date.today().isoformat()}.
# These are NOT shared with any other tenant.

# OpenConnector
OC_ADMIN_TOKEN={oc_admin}
OC_RUNTIME_TOKEN={runtime_token}
# Exactly 32 bytes, base64-encoded (openssl rand -base64 32 equivalent).
OC_ENCRYPTION_KEY={oc_encryption}

# FreeHand
OOMOL_CONNECT_CONSOLE_URL={console_url}{uid_gid}

# Composio (optional - leave blank unless the customer uses it)
COMPOSIO_API_KEY=
COMPOSIO_AUTH_CONFIG_FACEBOOK=
COMPOSIO_AUTH_CONFIG_INSTAGRAM=
"""


def hash_password() -> tuple[str | None, str]:
    """Return (bcrypt_hash_or_None, plaintext_password).

    Uses Caddy's own `caddy hash-password` so the hash is guaranteed to be
    one Caddy accepts. The plaintext is passed on STDIN, not as a command
    line argument: argv is visible to every user on the box via /proc, and
    this is a multi-tenant VPS. Caddy reads stdin when --plaintext is
    omitted, and requires a trailing newline.

    The stdin write must be BYTES, not text: with text=True Python
    translates "\n" to os.linesep, so on Windows Caddy receives
    "password\r\n" and hashes "password\r" - a hash that does not match
    the password the customer was given.

    Returns None for the hash when the container is not reachable - the
    caller then writes the plaintext to a 0600 file and tells the operator
    to hash it later, so provisioning never blocks on container state.
    """
    password = secrets.token_urlsafe(18)
    try:
        proc = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                CADDY_CONTAINER,
                "caddy",
                "hash-password",
            ],
            # BYTES, not text. With text=True, Python translates "\n" to
            # os.linesep when writing to the pipe - which is "\r\n" on
            # Windows. Caddy then hashes "password\r" instead of
            # "password", so the hash it returns does not match the
            # plaintext we recorded and every customer is locked out.
            # Verified: with text=True the hash MISMATCHES the password;
            # with bytes it MATCHES.
            input=(password + "\n").encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None, password
    if proc.returncode != 0:
        return None, password
    digest = proc.stdout.decode("utf-8", errors="replace").strip()
    if not digest.startswith("$2"):
        return None, password
    return digest, password


def build_caddy_site(
    hostname: str,
    freehand_port: int,
    auth_user: str,
    auth_hash: str,
    log_name: str,
) -> str:
    """One Caddy vhost: TLS + basic auth + access log in front of FreeHand.

    Caddy obtains a Let's Encrypt certificate for `hostname` automatically.
    The plain-HTTP block redirects to HTTPS, and the catch-all :80 abort in
    the shared Caddyfile stops unknown hosts being served a certificate.

    The upstream is addressed as host.docker.internal, NOT 127.0.0.1. Caddy
    runs in a container, so its own loopback is Caddy's - not the host's.
    A tenant's FreeHand port is published on the HOST's 127.0.0.1, so
    proxying to 127.0.0.1 from inside Caddy returns 502 (verified with
    curl against a real Caddy container). The compose service that runs
    Caddy adds the host-gateway mapping that makes the hostname resolve to
    the host.

    Note `basic_auth`, not `basicauth` - the latter is deprecated in Caddy 2
    and logs a deprecation warning on every load.
    """
    return f"""# Auto-generated by scripts/provision_tenant.py for customer '{hostname}'.
# Include this from the main Caddyfile:  import /etc/caddy/sites/*.site
# Safe to edit by hand afterwards.

http://{hostname} {{
\tredir https://{hostname}{{uri}} 308
}}

https://{hostname} {{
\t# Auth: bcrypt hash only. Never a plaintext password.
\tbasic_auth {{
\t\t{auth_user} {auth_hash}
\t}}

\t# Per-tenant access log - who hit this tenant, when, and what they asked
\t# for. This is the audit trail a leak test needs.
\tlog {{
\t\toutput file /var/log/caddy/{log_name}.log {{
\t\t\troll_size 10MiB
\t\t\troll_keep 5
\t\t}}
\t}}

\treverse_proxy host.docker.internal:{freehand_port} {{
\t\t# Agent actions can be slow; don't let the proxy cut them off.
\t\ttransport http {{
\t\t\tread_timeout 300s
\t\t\twrite_timeout 300s
\t\t}}
\t}}

\theader {{
\t\tX-Content-Type-Options nosniff
\t\tX-Frame-Options DENY
\t\tReferrer-Policy no-referrer
\t\t-Server
\t}}
}}
"""


def build_caddy_main() -> str:
    """The shared Caddyfile that imports every tenant vhost.

    `auto_https` is deliberately NOT disabled. Turning it off globally
    suppresses automatic certificate issuance for EVERY site, including
    named vhosts - Caddy's own `adapt` output shows
    `automatic_https: {disable: true}` and no TLS automation policies. The
    per-tenant sites use explicit `https://` addresses, so Caddy issues and
    renews their certificates on its own.

    The `:80` catch-all aborts requests for hosts we do not know, so an
    unknown hostname never gets a certificate.
    """
    return (
        "{\n"
        "\t# Shared Caddyfile - generated once, then hand-edited.\n"
        "\t# auto_https stays ON so named tenant vhosts get real certificates.\n"
        "}\n\n"
        ":80 {\n"
        "\t# Catch-all so an unknown host doesn't silently get a cert.\n"
        "\tabort\n"
        "}\n\n"
        "import /etc/caddy/sites/*.site\n"
    )


def build_summary(
    customer: str,
    freehand_port: int,
    oc_port: int,
    vault: Path,
    hostname: str,
    auth_user: str,
    hashed: bool,
) -> str:
    url_line = (
        f"- URL: https://{hostname}"
        if hostname
        else "- URL: (no hostname set - re-provision with --hostname to get TLS)"
    )
    auth_note = (
        "- Auth: bcrypt hash written to Caddyfile.site"
        if hashed
        else "- Auth: PLAINTEXT password written to vault/basic-auth.txt "
        "(hash it before exposing this tenant)"
    )
    return f"""# FreeHand tenant: {customer}

Provisioned {datetime.date.today().isoformat()}.

- FreeHand port: {freehand_port}
- OpenConnector port: {oc_port}
{url_line}
- Auth user: {auth_user}
{auth_note}
- Vault directory: {vault}
- Encryption key: {vault / 'encryption.key'} (BACK THIS UP)

FreeHand's port is bound to 127.0.0.1 only. The Caddy vhost is the only
route in.

## Start

    cd {tenant_dir(customer)}
    docker compose up -d

## Reload Caddy after adding a vhost

    docker exec {CADDY_CONTAINER} caddy reload --config /etc/caddy/Caddyfile

## Stop

    cd {tenant_dir(customer)}
    docker compose down

## Destroy (data loss!)

    python scripts/provision_tenant.py --destroy {customer} --confirm

## Critical: back up the encryption key

The key at `{vault / 'encryption.key'}` decrypts this customer's OAuth tokens
and credentials. Without it, a lost vault is permanently unreadable. A copy
was written to:

    {BACKUP_ROOT / customer / 'encryption.key'}

Store that copy somewhere Tracy controls, not only on this box.
"""


def provision(
    customer: str,
    hostname: str,
    freehand_port: int,
    dry_run: bool,
    auth_user: str,
) -> int:
    oc_port = freehand_port + 1

    existing = load_tenants()
    if customer in existing:
        raise ProvisionError(
            f"Tenant '{customer}' already exists at {existing[customer]['path']}.\n"
            "Use a different --customer name, or --destroy it first."
        )

    assert_ports_free(customer, freehand_port, oc_port)

    base = tenant_dir(customer)
    vault = base / "vault"
    key = Fernet.generate_key().decode()

    console_url = (
        f"https://{hostname}" if hostname else f"http://127.0.0.1:{oc_port}"
    )

    plan = [
        f"customer:    {customer}",
        f"tenant dir:  {base}",
        f"vault:       {vault}",
        f"freehand:    127.0.0.1:{freehand_port} (loopback only)",
        f"openconn:    127.0.0.1:{oc_port} (loopback only)",
        f"hostname:    {hostname or '(none - no TLS, no auth vhost)'}",
        f"auth user:   {auth_user}",
        "fernet key:  generated (never printed)",
        f"backups to:  {BACKUP_ROOT / customer}",
        f"compose:     {base / 'compose.yml'} (self-contained)",
    ]
    if dry_run:
        log("DRY RUN - nothing written.\nPlan:")
        for line in plan:
            log(f"  {line}")
        return 0

    log("Provisioning FreeHand tenant...\n")
    for line in plan:
        log(f"  {line}")

    vault.mkdir(parents=True, exist_ok=True)

    # 1. Encryption key, written before first boot so it is never
    #    auto-generated into a log or a race between two containers.
    write_secret_file(vault / "encryption.key", key)

    # 2. Immediate operator-side backup. The vault copy alone is not a
    #    backup: losing the box loses both.
    backup = BACKUP_ROOT / customer
    backup.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backup, 0o700)
    except OSError:
        pass
    write_secret_file(backup / "encryption.key", key)

    # 3. Settings skeleton. The customer supplies their own LLM key.
    settings = build_settings("openai", "gpt-4o-mini", "")
    write_secret_file(vault / "settings.json", json.dumps(settings, indent=2))

    # 4. Per-customer secrets.
    write_secret_file(base / ".env", build_env(customer, oc_port, console_url))

    # 5. Self-contained compose file. See build_compose() for why this is
    #    not an override of the repo's compose.yml.
    (base / "compose.yml").write_text(
        build_compose(customer, freehand_port, oc_port, REPO_ROOT),
        encoding="utf-8",
    )

    # 6. Auth + Caddy vhost, only when a hostname is available (TLS needs a
    #    real domain; Caddy cannot obtain a cert for an IP or bare hostname).
    auth_hash, auth_password = hash_password()
    hashed = auth_hash is not None
    if hostname:
        if hashed:
            (base / "Caddyfile.site").write_text(
                build_caddy_site(
                    hostname,
                    freehand_port,
                    auth_user,
                    auth_hash or "",
                    container_suffix(customer),
                ),
                encoding="utf-8",
            )
        else:
            # Caddy unavailable: persist the plaintext so the operator can
            # hash it once the container is up. Never lose the credential.
            write_secret_file(
                vault / "basic-auth.txt",
                f"{auth_user}:{auth_password}\n",
            )
            log(
                "\n  NOTE: Caddy container not reachable, so the password was "
                "not hashed."
            )
            log(
                "        Plaintext saved to "
                f"{vault / 'basic-auth.txt'} (owner-only on the VPS; see the"
            )
            log(
                "        permission warning above if you staged this on Windows)."
            )
            log(
                "        Hash it with:  docker exec "
                f"{CADDY_CONTAINER} caddy hash-password --plaintext '<pw>'"
            )
            log("        then re-run provisioning, or paste the hash into Caddyfile.site.")
    else:
        auth_password = ""

    # 7. Operator summary.
    (base / "TENANT.md").write_text(
        build_summary(
            customer,
            freehand_port,
            oc_port,
            vault,
            hostname,
            auth_user,
            hashed and bool(hostname),
        ),
        encoding="utf-8",
    )

    try:
        os.chmod(vault, 0o700)
    except OSError:
        pass

    log("\nDone. Nothing is running yet.\n")
    log(f"  cd {base}")
    log("  docker compose up -d")
    log("\nThe encryption key was written to:")
    log(f"  {vault / 'encryption.key'}")
    log(f"  {backup / 'encryption.key'}")

    if hostname and hashed:
        log(f"\nCustomer sign-in:\n  https://{hostname}\n  user: {auth_user}")
        log(f"  pass: {auth_password}")
        log("\nThis password is shown ONCE. Hand it over now; only the hash")
        log("is stored.")
    elif hostname:
        log(f"\nCustomer sign-in: https://{hostname} (password not hashed - see note above)")

    log("\nBack up the second key copy somewhere Tracy controls before the")
    log("customer connects a real account.")
    return 0


def stop_containers(customer: str) -> None:
    """Bring the tenant's containers down before removing their files.

    Without this, `docker compose down` is never run, so the containers keep
    running with a bind mount pointing at a directory we are about to delete.
    Docker then recreates the path as an empty directory, and the customer
    loses their data to a silent-looking rm.
    """
    base = tenant_dir(customer)
    if not (base / "compose.yml").exists():
        return
    try:
        subprocess.run(
            ["docker", "compose", "down", "--remove-orphans"],
            cwd=str(base),
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        log("  WARNING: could not run 'docker compose down' - stop the")
        log("           containers by hand before deleting their data.")


def destroy(customer: str, confirm: bool) -> int:
    base = tenant_dir(customer)
    if not base.exists():
        log(f"No tenant '{customer}' at {base}.")
        return 1
    if not confirm:
        log(f"Refusing to destroy {base} without --confirm.")
        log("This deletes the vault, the encryption key, and all customer data.")
        return 1

    stop_containers(customer)
    shutil.rmtree(base)

    # Remove the per-tenant OC volume too; it holds that customer's
    # OpenConnector state and is not inside the tenant directory.
    safe = container_suffix(customer)
    try:
        subprocess.run(
            ["docker", "volume", "rm", "-f", f"freehand-oc-data-{safe}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    backup = BACKUP_ROOT / customer
    if backup.exists():
        log(f"NOTE: key backup left in place at {backup}")
        log("      Delete it manually once you have another copy.")
    log(f"Destroyed {base}.")
    return 0


def list_tenants() -> int:
    tenants = load_tenants()
    if not tenants:
        log(f"No tenants under {TENANTS_ROOT}.")
        return 0
    log(f"Tenants under {TENANTS_ROOT}:\n")
    for name, meta in tenants.items():
        port = meta["freehand_port"] or "?"
        running = (
            "running"
            if meta["freehand_port"] and port_in_use(meta["freehand_port"])
            else "stopped"
        )
        key = "key ok" if meta["vault_exists"] else "NO KEY"
        # Three distinct auth states, not two. "hash pending" means a
        # hostname was requested but Caddy wasn't reachable to hash the
        # password — reporting that as "no vhost" hides a half-finished
        # tenant that must not be exposed yet.
        if meta["caddy_site"]:
            auth = "auth ok"
        elif (meta["path"] / "vault" / "basic-auth.txt").exists():
            auth = "HASH PENDING"
        elif meta["hostname"]:
            auth = "auth missing"
        else:
            auth = "no hostname"
        host = meta["hostname"] or "-"
        log(f"  {name:<22} {str(port):<6} {running:<8} {key:<8} {auth:<9} {host}")
    log(f"\n  {len(tenants)} tenant(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision a per-customer FreeHand instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--customer", help="Customer name (e.g. acme)")
    parser.add_argument(
        "--hostname",
        help="Public hostname for TLS + auth, e.g. acme.example.com",
    )
    parser.add_argument(
        "--auth-user", default="tenant", help="Basic-auth username"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_BASE_PORT,
        help=f"FreeHand port (default {DEFAULT_BASE_PORT}); "
        "OpenConnector gets port+1",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without writing anything",
    )
    parser.add_argument("--list", action="store_true", help="List tenants")
    parser.add_argument("--destroy", metavar="CUSTOMER", help="Destroy a tenant")
    parser.add_argument(
        "--confirm", action="store_true", help="Required with --destroy"
    )
    args = parser.parse_args()

    if args.list:
        return list_tenants()

    if args.destroy:
        try:
            return destroy(normalise_customer(args.destroy), args.confirm)
        except ProvisionError as exc:
            log(f"ERROR: {exc}")
            return 2

    if not args.customer:
        parser.error("--customer is required (or use --list / --destroy)")

    try:
        customer = normalise_customer(args.customer)
        hostname = normalise_hostname(args.hostname)
        return provision(
            customer, hostname, args.port, args.dry_run, args.auth_user
        )
    except ProvisionError as exc:
        log(f"ERROR: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
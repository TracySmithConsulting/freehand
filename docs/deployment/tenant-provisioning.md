# Tenant provisioning

FreeHand is a single-tenant application. Every customer gets their **own
box** - their own vault directory, their own encryption key, their own
container set, their own ports, and their own Caddy vhost. Nothing is
shared between customers.

This document describes `scripts/provision_tenant.py`, which sets that up.

## Why a separate box per customer

FreeHand resolves all per-customer state from `VAULT_DIR`, a module-level
constant in `core/agent_config.py` (and again in `core/database.py`). It is
computed at import time and is **not** environment-variable driven. Two
FreeHand processes sharing one checkout would therefore fight over the same
vault directory.

Containers solve this without touching FreeHand's code: each customer gets
their own container set with its own bind-mounted vault.

## What the script does

```
python scripts/provision_tenant.py --customer acme --hostname acme.example.com --port 8100
```

That creates, under `~/freehand-tenants/acme-corp/`:

| File | Purpose |
|---|---|
| `compose.yml` | The tenant's **complete** compose file (see below) |
| `.env` | Per-tenant secrets: OpenConnector tokens, the OC encryption key |
| `vault/encryption.key` | The tenant's Fernet key - **back this up** |
| `vault/settings.json` | Settings skeleton; the customer adds their LLM key |
| `Caddyfile.site` | The tenant's vhost: TLS, basic auth, access log |
| `TENANT.md` | Human-readable summary of the tenant |

The Fernet key is also copied to `~/freehand-key-backups/<customer>/`.
**Store that copy somewhere you control, not only on the box** - losing the
vault without the key means losing the customer's OAuth tokens permanently.

## The compose file is self-contained on purpose

Each tenant gets a **complete** `compose.yml`, not an override or an
`extends:` of the repo's `compose.yml`. Two reasons, both learned the hard
way:

1. **`extends:` resolves relative paths against the file that declares
   them.** A shared base would put every tenant's vault bind at the repo's
   own `./vault` - so every customer would share one vault. The single most
   important property of this setup is exactly what `extends` breaks.

2. **Compose merges `ports` lists; it does not replace them.** An override
   that adds `127.0.0.1:8100:8000` on top of a base that already publishes
   `127.0.0.1:8000:8000` results in **both** ports being bound. The second
   tenant to start then collides with the first on port 8000.

Writing the whole file avoids merge semantics entirely. The build context
still points back at the repo checkout (that is where the `Dockerfile` is);
the vault bind is relative to the tenant's own directory.

## Ports

Each tenant gets two loopback ports: FreeHand on `--port`, OpenConnector on
`--port + 1`. Both bind to `127.0.0.1` only - the Caddy vhost is the only
route in from the network.

Ports `8000`, `3001`, `8765`, `5432`, `6379`, `80` and `443` are reserved
and rejected.

## The shared Caddy

One Caddy container serves every tenant on the box. It is defined in the
repo's `compose.yml` behind the `proxy` profile:

```bash
docker compose --profile proxy up -d
```

The shared `caddy/Caddyfile` imports every tenant vhost from
`caddy/sites/*.site`. The script writes each tenant's `Caddyfile.site`; copy
it into the shared sites directory and reload Caddy:

```bash
cp ~/freehand-tenants/acme-corp/Caddyfile.site caddy/sites/acme.example.com.site
docker exec freehand-caddy caddy reload --config /etc/caddy/Caddyfile
```

### Three things that will bite you

**`auto_https off` breaks certificates.** The shared Caddyfile deliberately
leaves `auto_https` on. Turning it off globally suppresses automatic
certificate issuance for *every* site, including named vhosts - Caddy's own
`caddy adapt` output shows `automatic_https: {disable: true}` and no TLS
automation policies. The per-tenant vhosts use explicit `https://` addresses
so Caddy issues and renews their certificates itself.

**Caddy cannot reach a tenant on `127.0.0.1`.** Caddy runs in a container,
so its `127.0.0.1` is *its own* loopback, not the host's. A tenant's
FreeHand port is published on the **host's** `127.0.0.1`, so a vhost that
proxies to `127.0.0.1:8100` returns **502**. The vhosts therefore proxy to
`host.docker.internal:8100`, and the Caddy service carries
`extra_hosts: ["host.docker.internal:host-gateway"]` so that name resolves
to the host.

**`basicauth` is deprecated.** Use `basic_auth`. Caddy 2 logs a
deprecation warning on every load otherwise.

### Password hashing

The basic-auth password is hashed by Caddy itself (`caddy hash-password`),
so the hash is guaranteed to be one Caddy accepts - no second bcrypt
implementation to drift out of sync.

The plaintext is passed to Caddy on **stdin**, never as a command-line
argument. On a shared VPS, argv is visible to every local user via `/proc`.

If the Caddy container is not running, the script still completes: it
writes the plaintext to `vault/basic-auth.txt` (mode 0600) and tells you to
hash it later. `provision_tenant.py --list` then reports that tenant as
`HASH PENDING` rather than pretending it is ready.

## Security notes

- The Fernet key is written with mode `0600` **at creation time** (via
  `os.open`), not with a `write_text()` followed by a `chmod`. The naive
  version leaves a window where the file is `0644` and any local user can
  read the key and decrypt the whole vault. On Windows, `chmod` cannot
  express POSIX permissions, so the script warns loudly instead.
- The encryption key never appears in the Caddyfile, the `.env`, the
  `TENANT.md`, or any log output.
- The basic-auth password is shown once, at provision time, because you have
  to hand it to the customer. Only the hash is stored.
- Non-ASCII customer names are rejected: Docker container names may only
  contain `[a-z0-9_.-]`, and `str.isalnum()` is Unicode-aware, so "café"
  would otherwise pass validation and fail at `docker compose up` with a
  message that does not explain the cause.
- A bare IP address is rejected as a hostname: an IPv4 passes the FQDN
  regex, but Caddy cannot obtain a Let's Encrypt certificate for an IP.

## Destroying a tenant

```bash
python scripts/provision_tenant.py --destroy acme-corp --confirm
```

This stops the containers **first**, then removes the tenant directory and
the per-tenant OpenConnector volume. Stopping first matters: deleting the
directory out from under a running container leaves Docker to recreate an
empty vault path, and the customer's data disappears behind a
`rm` that looked ordinary.

The encryption-key backup in `~/freehand-key-backups/` is deliberately left
in place, with a note to delete it manually once you have another copy.

## Verifying a tenant

```bash
cd ~/freehand-tenants/acme-corp
docker compose config          # resolves? no merge surprises?
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:8100/ | head -3
```

Through Caddy (after the vhost is installed and Caddy reloaded):

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://acme.example.com/   # 401 without credentials
curl -s -u tenant:PASSWORD -o /dev/null -w "%{http_code}\n" https://acme.example.com/  # 200
```

The per-tenant access log is at `/var/log/caddy/<customer>.log` inside the
Caddy container.

## Running the tests

```bash
pytest tests/test_provision_tenant.py -v
```

The suite covers the behaviours above - self-contained compose, port
isolation between two tenants, argv-free password hashing, `0600` at
creation, `auto_https` left on, `basic_auth` over `basicauth`, ASCII-only
names, and `--destroy` stopping containers before deleting files. Two tests
assert POSIX permission bits and are skipped on Windows; they pass on
Linux.

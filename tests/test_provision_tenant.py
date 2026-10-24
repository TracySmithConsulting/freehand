"""Tests for scripts/provision_tenant.py.

These cover the behaviours that were wrong in the first draft of the
script and would silently break a multi-tenant box:

- Each tenant's compose file must be SELF-CONTAINED. The first draft
  wrote a `compose.override.yml` that had to be merged with the repo's
  `compose.yml`; compose MERGES `ports` lists rather than replacing them,
  so every tenant ended up also binding port 8000 and 3001 and the second
  tenant to come up collided with the first.
- The build context must point at the repo checkout (which has the
  Dockerfile), not at the empty tenant directory.
- Passwords must not be passed to `caddy hash-password` as argv.
- The Fernet key must be 0600 from the instant it exists, not after a
  chmod.
- `auto_https off` in the shared Caddyfile suppresses certificate
  issuance for every named vhost.
- `basicauth` is deprecated in Caddy 2; `basic_auth` is correct.
- Non-ASCII customer names produce illegal Docker container names.
- `--destroy` must stop the containers before deleting their files.

The module under test is a script, not a package, so it is loaded by
path. Docker-dependent behaviour is exercised by inspecting the GENERATED
files (and, where available, by running `docker compose config`); no test
here starts a container.
"""

import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "provision_tenant.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("provision_tenant", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["provision_tenant"] = module
    spec.loader.exec_module(module)
    return module


pt = _load_module()


# ── normalise_customer ──────────────────────────────────────────────────


class TestNormaliseCustomer:
    def test_spaces_become_hyphens(self):
        assert pt.normalise_customer("Acme Corp") == "acme-corp"

    def test_lowercases_and_strips(self):
        assert pt.normalise_customer("  ACME  ") == "acme"

    def test_collapses_repeated_separators(self):
        assert pt.normalise_customer("a---b") == "a-b"

    def test_empty_is_rejected(self):
        with pytest.raises(pt.ProvisionError, match="no usable characters"):
            pt.normalise_customer("   ")

    def test_punctuation_only_is_rejected(self):
        with pytest.raises(pt.ProvisionError, match="no usable characters"):
            pt.normalise_customer("!!!@@@")

    def test_overlong_is_rejected(self):
        with pytest.raises(pt.ProvisionError, match="too long"):
            pt.normalise_customer("a" * 41)

    def test_non_ascii_is_stripped(self):
        # `str.isalnum()` is Unicode-aware, so "café" would otherwise pass
        # through and produce a container name Docker rejects.
        assert pt.normalise_customer("café") == "caf"

    def test_path_traversal_cannot_escape(self, tmp_path, monkeypatch):
        # Traversal segments become hyphens, so the result is always a
        # single path component under TENANTS_ROOT. A name made only of
        # separators is rejected outright.
        monkeypatch.setattr(pt, "TENANTS_ROOT", tmp_path)
        for raw in ("../../etc", "C:/Windows", "a/b"):
            cleaned = pt.normalise_customer(raw)
            assert "/" not in cleaned
            assert "\\" not in cleaned
            assert ".." not in cleaned
            # The resolved path must stay under TENANTS_ROOT.
            resolved = (tmp_path / cleaned).resolve()
            assert tmp_path.resolve() in resolved.parents

    def test_dot_dot_alone_is_rejected(self):
        with pytest.raises(pt.ProvisionError, match="no usable characters"):
            pt.normalise_customer("..")

    def test_result_is_always_docker_safe(self):
        for raw in ("Acme Corp", "café", "Ünïcode Ltd", "a.b", "x"):
            suffix = pt.container_suffix(pt.normalise_customer(raw))
            assert re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", suffix), raw


# ── container_suffix ────────────────────────────────────────────────────


class TestContainerSuffix:
    def test_hyphens_become_underscores(self):
        assert pt.container_suffix("acme-corp") == "acme_corp"

    def test_rejects_illegal_docker_name(self):
        with pytest.raises(pt.ProvisionError, match="legal Docker name"):
            pt.container_suffix("bad name")


# ── normalise_hostname ──────────────────────────────────────────────────


class TestNormaliseHostname:
    def test_valid_hostname(self):
        assert pt.normalise_hostname("Acme.Example.COM") == "acme.example.com"

    def test_trailing_dot_stripped(self):
        assert pt.normalise_hostname("acme.example.com.") == "acme.example.com"

    def test_none_means_no_vhost(self):
        assert pt.normalise_hostname(None) == ""
        assert pt.normalise_hostname("") == ""

    def test_bare_hostname_rejected(self):
        # Caddy cannot issue a certificate without a real domain.
        with pytest.raises(pt.ProvisionError, match="not a valid hostname"):
            pt.normalise_hostname("localhost")

    def test_ip_rejected(self):
        # An IPv4 passes the FQDN regex, but Caddy cannot obtain a
        # Let's Encrypt certificate for an IP address.
        with pytest.raises(pt.ProvisionError, match="is an IP address"):
            pt.normalise_hostname("127.0.0.1")

    def test_ipv6_rejected(self):
        with pytest.raises(pt.ProvisionError):
            pt.normalise_hostname("2001:db8::1")


# ── write_secret_file ───────────────────────────────────────────────────


class TestWriteSecretFile:
    def test_writes_content(self, tmp_path):
        target = tmp_path / "secret.txt"
        pt.write_secret_file(target, "hunter2")
        assert target.read_text(encoding="utf-8") == "hunter2"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_mode_is_0600_at_creation(self, tmp_path, monkeypatch):
        """The key must never be readable by others, not even briefly.

        write_text() then chmod() leaves a window where the file has the
        process umask (0644 on a typical box). os.open() with the mode
        closes that window, so this test watches the mode DURING the
        write rather than after it.
        """
        target = tmp_path / "encryption.key"
        observed = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *args, **kwargs):
            fd = real_open(path, flags, mode, *args, **kwargs)
            observed.append(os.stat(path).st_mode & 0o777)
            return fd

        monkeypatch.setattr(os, "open", spy)
        pt.write_secret_file(target, "key-material")

        assert observed, "os.open was never called"
        assert all(m == 0o600 for m in observed), observed
        assert target.stat().st_mode & 0o777 == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_overwrites_existing_file_and_re_tightens_mode(self, tmp_path):
        target = tmp_path / "encryption.key"
        target.write_text("old", encoding="utf-8")
        os.chmod(target, 0o644)
        pt.write_secret_file(target, "new")
        assert target.read_text(encoding="utf-8") == "new"
        assert target.stat().st_mode & 0o777 == 0o600


# ── build_compose ───────────────────────────────────────────────────────


class TestBuildCompose:
    @pytest.fixture
    def compose(self, tmp_path):
        return pt.build_compose("acme-corp", 8100, 8101, tmp_path)

    def test_is_self_contained(self, compose):
        # It must NOT extend or override the repo compose file: `extends`
        # resolves relative paths against the declaring file, which would
        # point every tenant at the same vault.
        assert "extends:" not in compose
        assert "compose.override" not in compose

    def test_declares_both_services(self, compose):
        assert "open-connector:" in compose
        assert "freehand:" in compose

    def test_publishes_only_its_own_ports(self, compose):
        # The bug: an override merged with the repo's compose.yml also
        # published 8000 and 3001, so the second tenant collided.
        published = re.findall(r'127\.0\.0\.1:(\d+):(\d+)', compose)
        assert sorted(published) == [("8100", "8000"), ("8101", "3001")]

    def test_does_not_publish_the_shared_ports(self, compose):
        assert "127.0.0.1:8000:8000" not in compose
        assert "127.0.0.1:3001:3001" not in compose

    def test_build_context_points_at_the_repo(self, tmp_path):
        compose = pt.build_compose("acme-corp", 8100, 8101, tmp_path)
        assert f'context: "{tmp_path.as_posix()}"' in compose
        assert "dockerfile: Dockerfile" in compose

    def test_vault_bind_is_relative_to_the_tenant_dir(self, compose):
        assert "./vault:/app/vault" in compose

    def test_container_names_are_tenant_scoped(self, compose):
        assert "container_name: freehand-app-acme_corp" in compose
        assert "container_name: freehand-oc-acme_corp" in compose

    def test_oc_volume_is_tenant_scoped(self, compose):
        assert "name: freehand-oc-data-acme_corp" in compose

    def test_secrets_come_from_env_not_inline(self, compose):
        assert "OOMOL_CONNECT_ADMIN_TOKEN=${OC_ADMIN_TOKEN}" in compose
        # No literal secret material in the compose file.
        assert "admintest123" not in compose
        assert "runttest123" not in compose

    def test_repo_compose_gives_caddy_the_host_gateway_mapping(self):
        """Without it, every tenant proxy returns 502 (verified live)."""
        base = (PROJECT_ROOT / "compose.yml").read_text(encoding="utf-8")
        assert "host.docker.internal:host-gateway" in base

    def test_uses_service_name_not_localhost_for_oc(self, compose):
        # FreeHand reaches OC over compose DNS; localhost would be the
        # FreeHand container itself.
        assert "OOMOL_CONNECT_BASE_URL=http://open-connector:3001" in compose

    def test_different_tenants_get_different_names_and_ports(self, tmp_path):
        a = pt.build_compose("acme-corp", 8100, 8101, tmp_path)
        b = pt.build_compose("beta-corp", 8200, 8201, tmp_path)
        assert "freehand-app-acme_corp" in a and "freehand-app-beta_corp" in b
        assert "127.0.0.1:8100:8000" in a and "127.0.0.1:8200:8000" in b


# ── build_env ───────────────────────────────────────────────────────────


class TestBuildEnv:
    @pytest.fixture
    def env(self):
        return pt.build_env("acme-corp", 8101, "https://acme.example.com")

    def test_oc_encryption_key_is_32_bytes(self, env):
        # OpenConnector requires exactly 32 bytes of key material.
        value = re.search(r"^OC_ENCRYPTION_KEY=(.+)$", env, re.M).group(1)
        assert len(__import__("base64").b64decode(value)) == 32

    def test_oc_encryption_key_is_not_token_urlsafe(self, env):
        # token_urlsafe(32) yields 43 chars, which is not 32 bytes.
        value = re.search(r"^OC_ENCRYPTION_KEY=(.+)$", env, re.M).group(1)
        assert len(value) != 43

    def test_tokens_are_present_and_non_empty(self, env):
        assert re.search(r"^OC_ADMIN_TOKEN=\S+", env, re.M)
        assert re.search(r"^OC_RUNTIME_TOKEN=\S+", env, re.M)

    def test_console_url_is_set(self, env):
        assert "OOMOL_CONNECT_CONSOLE_URL=https://acme.example.com" in env

    def test_secrets_differ_between_tenants(self):
        a = pt.build_env("acme-corp", 8101, "https://a.example.com")
        b = pt.build_env("beta-corp", 8201, "https://b.example.com")

        def value(text, key):
            return re.search(rf"^{key}=(.+)$", text, re.M).group(1)

        for key in ("OC_ADMIN_TOKEN", "OC_RUNTIME_TOKEN", "OC_ENCRYPTION_KEY"):
            assert value(a, key) != value(b, key), key


# ── hash_password ───────────────────────────────────────────────────────


class TestHashPassword:
    def test_password_is_not_passed_in_argv(self, monkeypatch):
        """argv is world-readable via /proc on a shared VPS."""
        captured = {}

        class FakeProc:
            returncode = 0
            stdout = b"$2y$04$abcdefghijklmnopqrstuv"
            stderr = b""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return FakeProc()

        monkeypatch.setattr(pt.subprocess, "run", fake_run)
        digest, plaintext = pt.hash_password()

        assert digest == "$2y$04$abcdefghijklmnopqrstuv"
        assert plaintext
        assert "--plaintext" not in captured["cmd"]
        assert plaintext not in captured["cmd"]
        # It must go in on stdin instead (as bytes - see the next test).
        assert captured["kwargs"]["input"].startswith(plaintext.encode("utf-8"))

    def test_stdin_is_sent_as_bytes_not_text(self, monkeypatch):
        """text=True would send \r\n on Windows, so Caddy would hash
        "password\r" and the customer could never authenticate.

        The password must go over the pipe as bytes with exactly one \n.
        """
        captured = {}

        class FakeProc:
            returncode = 0
            stdout = b"$2y$04$abcdefghijklmnopqrstuv"
            stderr = b""

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)
            return FakeProc()

        monkeypatch.setattr(pt.subprocess, "run", fake_run)
        digest, plaintext = pt.hash_password()

        assert digest == "$2y$04$abcdefghijklmnopqrstuv"
        # Bytes, not str: text=True translates "\n" to os.linesep.
        assert isinstance(captured["input"], bytes)
        assert captured["input"] == (plaintext + "\n").encode("utf-8")
        assert b"\r" not in captured["input"]
        # text mode must stay off, or the newline gets translated.
        assert not captured.get("text")

    def test_falls_back_when_caddy_is_unreachable(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("docker")

        monkeypatch.setattr(pt.subprocess, "run", fake_run)
        digest, plaintext = pt.hash_password()
        assert digest is None
        assert plaintext

    def test_falls_back_on_nonzero_exit(self, monkeypatch):
        class FakeProc:
            returncode = 1
            stdout = ""
            stderr = "no such container"

        monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **kw: FakeProc())
        digest, plaintext = pt.hash_password()
        assert digest is None
        assert plaintext


# ── build_caddy_site ────────────────────────────────────────────────────


class TestBuildCaddySite:
    @pytest.fixture
    def site(self):
        return pt.build_caddy_site(
            "acme.example.com", 8100, "tenant", "$2y$04$abc", "acme_corp"
        )

    def test_uses_basic_auth_not_deprecated_basicauth(self, site):
        assert "basic_auth" in site
        assert "basicauth" not in site

    def test_uses_https_scheme(self, site):
        assert "https://acme.example.com" in site

    def test_redirects_plain_http(self, site):
        assert "http://acme.example.com" in site
        # 308, not 301: a 301 lets clients rewrite POST to GET, and
        # FreeHand's API is POST-heavy.
        assert "redir https://acme.example.com{uri} 308" in site

    def test_proxies_to_the_tenant_port(self, site):
        assert "reverse_proxy host.docker.internal:8100" in site

    def test_does_not_proxy_to_caddys_own_loopback(self, site):
        """Caddy runs in a container; its 127.0.0.1 is not the host's.

        The tenant's FreeHand port is published on the HOST's loopback, so
        proxying to 127.0.0.1 from inside Caddy returns 502. This was
        verified with a real Caddy container and curl.
        """
        assert "reverse_proxy 127.0.0.1" not in site

    def test_writes_only_the_hash_never_a_plaintext_password(self, site):
        assert "$2y$04$abc" in site
        # The password itself must never reach the vhost file.
        assert "hunter2" not in site

    def test_has_an_access_log(self, site):
        assert "/var/log/caddy/acme_corp.log" in site

    def test_has_security_headers(self, site):
        assert "X-Content-Type-Options nosniff" in site
        assert "X-Frame-Options DENY" in site


# ── build_caddy_main ────────────────────────────────────────────────────


class TestBuildCaddyMain:
    @pytest.fixture
    def main(self):
        return pt.build_caddy_main()

    def test_does_not_disable_auto_https(self, main):
        # `auto_https off` globally suppresses certificate issuance for
        # EVERY site, including named vhosts. Caddy's `adapt` output shows
        # `automatic_https: {disable: true}` in that case.
        assert "auto_https off" not in main
        assert "auto_https disable" not in main

    def test_imports_tenant_sites(self, main):
        assert "import /etc/caddy/sites/*.site" in main

    def test_aborts_unknown_hosts_on_port_80(self, main):
        assert ":80 {" in main
        assert "abort" in main


# ── destroy ─────────────────────────────────────────────────────────────


class TestDestroy:
    def test_refuses_without_confirm(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pt, "TENANTS_ROOT", tmp_path)
        tenant = tmp_path / "acme-corp"
        tenant.mkdir()
        assert pt.destroy("acme-corp", confirm=False) == 1
        assert tenant.exists()

    def test_stops_containers_before_deleting(self, tmp_path, monkeypatch):
        """Otherwise the containers keep running against a deleted vault."""
        monkeypatch.setattr(pt, "TENANTS_ROOT", tmp_path)
        tenant = tmp_path / "acme-corp"
        (tenant / "vault").mkdir(parents=True)
        (tenant / "compose.yml").write_text("services: {}\n", encoding="utf-8")

        calls = []

        class FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeProc()

        monkeypatch.setattr(pt.subprocess, "run", fake_run)
        assert pt.destroy("acme-corp", confirm=True) == 0

        assert calls, "docker was never called - containers would keep running"
        assert calls[0][:3] == ["docker", "compose", "down"]
        # The tenant directory is gone afterwards.
        assert not tenant.exists()

    def test_removes_the_tenant_oc_volume(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pt, "TENANTS_ROOT", tmp_path)
        tenant = tmp_path / "acme-corp"
        tenant.mkdir()

        calls = []

        class FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(
            pt.subprocess, "run", lambda cmd, **kw: (calls.append(cmd), FakeProc())[1]
        )
        pt.destroy("acme-corp", confirm=True)
        assert any("volume" in " ".join(c) for c in calls)
        assert any("freehand-oc-data-acme_corp" in " ".join(c) for c in calls)


# ── assert_ports_free ───────────────────────────────────────────────────


class TestAssertPortsFree:
    def test_rejects_reserved_ports(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pt, "TENANTS_ROOT", tmp_path)
        with pytest.raises(pt.ProvisionError, match="reserved"):
            pt.assert_ports_free("acme-corp", 8000, 8001)

    def test_rejects_ports_claimed_by_another_tenant(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pt, "TENANTS_ROOT", tmp_path)
        other = tmp_path / "beta-corp"
        other.mkdir()
        (other / "TENANT.md").write_text(
            "- FreeHand port: 8100\n", encoding="utf-8"
        )
        with pytest.raises(pt.ProvisionError, match="already assigned"):
            pt.assert_ports_free("acme-corp", 8100, 8101)

    def test_allows_a_free_port(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pt, "TENANTS_ROOT", tmp_path)
        # 8765 is reserved, 8766 is not; use a high port unlikely to be in use.
        pt.assert_ports_free("acme-corp", 8766, 8767)


# ── provision (dry run) ─────────────────────────────────────────────────


class TestProvisionDryRun:
    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pt, "TENANTS_ROOT", tmp_path)
        monkeypatch.setattr(pt, "BACKUP_ROOT", tmp_path / "backups")
        # A high port unlikely to be in use by anything else on the box.
        rc = pt.provision(
            "acme-corp", "acme.example.com", 21877, dry_run=True, auth_user="tenant"
        )
        assert rc == 0
        assert not (tmp_path / "acme-corp").exists()

    def test_refuses_to_overwrite_an_existing_tenant(self, tmp_path, monkeypatch):
        # The existing tenant is detected before any port check, so the
        # port here does not need to be free.
        monkeypatch.setattr(pt, "TENANTS_ROOT", tmp_path)
        monkeypatch.setattr(pt, "BACKUP_ROOT", tmp_path / "backups")
        existing = tmp_path / "acme-corp"
        existing.mkdir()
        (existing / "TENANT.md").write_text(
            "- FreeHand port: 8100\n- URL: https://acme.example.com\n",
            encoding="utf-8",
        )
        with pytest.raises(pt.ProvisionError, match="already exists"):
            pt.provision("acme-corp", "acme.example.com", 8100, dry_run=True, auth_user="tenant")


# ── integration with real docker compose (skipped when unavailable) ─────


def _docker_compose_available() -> bool:
    import shutil
    import subprocess

    if not shutil.which("docker"):
        return False
    try:
        proc = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=30,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(
    not _docker_compose_available(), reason="docker compose not available"
)
class TestComposeResolves:
    """The generated file must be usable by docker compose as-is."""

    def _write_tenant(self, tmp_path, customer, fh, oc):
        tenant = tmp_path / customer
        (tenant / "vault").mkdir(parents=True)
        (tenant / "compose.yml").write_text(
            pt.build_compose(customer, fh, oc, PROJECT_ROOT), encoding="utf-8"
        )
        (tenant / ".env").write_text(
            pt.build_env(customer, oc, "https://x.example.com"), encoding="utf-8"
        )
        return tenant

    def test_two_tenants_resolve_without_port_collisions(self, tmp_path):
        import subprocess

        a = self._write_tenant(tmp_path, "acme-corp", 8100, 8101)
        b = self._write_tenant(tmp_path, "beta-corp", 8200, 8201)

        published = []
        for tenant in (a, b):
            proc = subprocess.run(
                ["docker", "compose", "config", "--format", "json"],
                cwd=str(tenant),
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert proc.returncode == 0, proc.stderr
            config = json.loads(proc.stdout)
            for service in config["services"].values():
                for port in service.get("ports", []):
                    published.append((port.get("host_ip"), port.get("published")))

        # Every host port belongs to exactly one tenant.
        assert len(published) == len(set(published)), published
        assert ("127.0.0.1", "8000") not in published
        assert ("127.0.0.1", "3001") not in published
        assert ("127.0.0.1", "8100") in published
        assert ("127.0.0.1", "8200") in published

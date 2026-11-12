"""Release hardening (Nov 2026).

- Nothing secret is baked into the Docker image (.env, WhatsApp session).
- Tenant boxes get their own Composio file, never the operator's key.
- The OpenConnector console has its own address behind the password.
- compose.yml pins OpenConnector and has no public test passwords.
- scripts/setup_env.py creates .env with random secrets.
"""
import base64
import importlib.util
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pt = _load("provision_tenant", "scripts/provision_tenant.py")
setup_env = _load("setup_env", "scripts/setup_env.py")
REPO_COMPOSE = (PROJECT_ROOT / "compose.yml").read_text(encoding="utf-8")


# ── Docker image ───────────────────────────────────────────────────────

def _dockerignore():
    return [line.strip() for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("pattern", [".env", "**/.env", "**/auth_info_baileys/", "**/node_modules/"])
def test_secrets_are_not_baked_into_the_image(pattern):
    """The image used to carry the operator's .env (Composio key) and the
    linked WhatsApp session into every tenant's image."""
    assert pattern in _dockerignore()


def test_env_example_is_still_copied():
    assert "!.env.example" in _dockerignore()


# ── Tenant Composio isolation ──────────────────────────────────────────

def test_tenant_compose_reads_composio_from_its_own_file(tmp_path):
    compose = pt.build_compose("acme-corp", 8100, 8101, tmp_path)
    assert "path: ./composio.env" in compose
    # ${COMPOSIO_API_KEY} substitution comes from the operator's shell first.
    assert "- COMPOSIO_API_KEY=" not in compose
    assert "COMPOSIO_API_KEY" not in pt.build_env("acme-corp", 8101, "https://x")


def test_composio_env_starts_blank_and_warns_against_sharing():
    text = pt.build_composio_env("acme-corp")
    assert re.search(r"^COMPOSIO_API_KEY=$", text, re.MULTILINE)
    assert "FOR THIS CUSTOMER ONLY" in text


# ── OpenConnector console address ──────────────────────────────────────

def test_console_gets_its_own_password_protected_vhost():
    site = pt.build_caddy_site("acme.example.com", 8100, "tenant", "$2y$04$abc", "acme_corp",
                               8101, "connect.acme.example.com")
    connect = site.split("https://connect.acme.example.com {", 1)[1]
    assert connect.index("basic_auth {") < connect.index("reverse_proxy host.docker.internal:8101")
    assert "redir https://connect.acme.example.com{uri} 308" in site
    # FreeHand's own vhost is unchanged: still exactly one password there.
    freehand = site.split("https://connect.acme.example.com {", 1)[0]
    assert freehand.count("basic_auth {") == 1


def test_console_vhost_is_optional():
    site = pt.build_caddy_site("acme.example.com", 8100, "tenant", "$2y$04$abc", "acme_corp")
    assert "connect." not in site


def test_provision_points_the_console_link_at_the_connect_address(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "TENANTS_ROOT", tmp_path / "tenants")
    monkeypatch.setattr(pt, "BACKUP_ROOT", tmp_path / "backups")
    monkeypatch.setattr(pt, "hash_password", lambda: ("$2y$04$abc", "pw"))
    monkeypatch.setattr(pt, "port_in_use", lambda port: False)
    assert pt.provision("acme", "acme.example.com", 8100, False, "tenant") == 0
    base = tmp_path / "tenants" / "acme"
    env = (base / ".env").read_text(encoding="utf-8")
    assert "OOMOL_CONNECT_CONSOLE_URL=https://connect.acme.example.com" in env
    assert (base / "composio.env").exists()
    assert "https://connect.acme.example.com {" in (base / "Caddyfile.site").read_text(encoding="utf-8")
    assert "connect.acme.example.com" in (base / "TENANT.md").read_text(encoding="utf-8")


# ── Image pin and passwords ────────────────────────────────────────────

def test_open_connector_is_pinned_identically_everywhere(tmp_path):
    assert "image: ghcr.io/oomol-lab/open-connector:latest" not in REPO_COMPOSE
    assert f"image: {pt.OC_IMAGE}" in REPO_COMPOSE
    assert f"image: {pt.OC_IMAGE}" in pt.build_compose("acme", 8100, 8101, tmp_path)
    assert "@sha256:" in pt.OC_IMAGE


@pytest.mark.parametrize("test_value", ["admintest123", "runttest123", "this-is-a-thirty-two-byte-test-key"])
def test_no_public_test_passwords_as_defaults(test_value):
    assert test_value not in REPO_COMPOSE
    assert test_value not in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")


@pytest.mark.parametrize("compose", ["repo", "tenant"])
def test_open_connector_data_is_kept_where_oc_writes_it(compose, tmp_path):
    """OC writes to /app/data. Mounting /data left its data in an unnamed
    volume that was replaced, empty, on every rebuild."""
    text = REPO_COMPOSE if compose == "repo" else pt.build_compose("acme", 8100, 8101, tmp_path)
    assert "- oc-data:/app/data" in text
    assert "- oc-data:/data" not in text


def test_compose_requires_each_secret():
    for name in ("OC_ADMIN_TOKEN", "OC_RUNTIME_TOKEN", "OC_ENCRYPTION_KEY"):
        assert f"${{{name}:?" in REPO_COMPOSE, name


# ── setup_env.py ───────────────────────────────────────────────────────

@pytest.fixture
def env_paths(tmp_path, monkeypatch):
    example = tmp_path / ".env.example"
    example.write_bytes((PROJECT_ROOT / ".env.example").read_bytes())
    monkeypatch.setattr(setup_env, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(setup_env, "EXAMPLE_PATH", example)
    return tmp_path / ".env"


def _values(path):
    return setup_env.current_values(setup_env.read_lines(path))


def test_creates_env_with_strong_random_secrets(env_paths, monkeypatch):
    monkeypatch.setattr(setup_env, "oc_has_data", lambda: True)
    assert setup_env.main([]) == 0
    v = _values(env_paths)
    assert len(v["OC_ADMIN_TOKEN"]) == 64 and len(v["OC_RUNTIME_TOKEN"]) == 64
    assert v["OC_ADMIN_TOKEN"] != v["OC_RUNTIME_TOKEN"]
    assert len(base64.b64decode(v["OC_ENCRYPTION_KEY"])) == 32
    assert b"\r" not in env_paths.read_bytes()


def test_replaces_test_tokens_but_keeps_the_key_when_oc_has_data(env_paths, monkeypatch, capsys):
    env_paths.write_text(
        "OC_ADMIN_TOKEN=admintest123\nOC_RUNTIME_TOKEN=runttest123\n"
        "OC_ENCRYPTION_KEY=this-is-a-thirty-two-byte-test-key\nUID=1000\n", encoding="utf-8")
    monkeypatch.setattr(setup_env, "oc_has_data", lambda: True)
    setup_env.main([])
    v = _values(env_paths)
    assert v["OC_ADMIN_TOKEN"] != "admintest123" and v["OC_RUNTIME_TOKEN"] != "runttest123"
    assert v["OC_ENCRYPTION_KEY"] == "this-is-a-thirty-two-byte-test-key"
    assert v["UID"] == "1000"
    assert "unreadable" in capsys.readouterr().out


def test_replaces_the_test_key_when_oc_is_empty(env_paths, monkeypatch):
    env_paths.write_text("OC_ENCRYPTION_KEY=this-is-a-thirty-two-byte-test-key\n", encoding="utf-8")
    monkeypatch.setattr(setup_env, "oc_has_data", lambda: False)
    setup_env.main([])
    assert len(base64.b64decode(_values(env_paths)["OC_ENCRYPTION_KEY"])) == 32


def test_leaves_real_values_alone(env_paths, monkeypatch, capsys):
    env_paths.write_text("OC_ADMIN_TOKEN=mine\nOC_RUNTIME_TOKEN=also-mine\nOC_ENCRYPTION_KEY=k\n", encoding="utf-8")
    before = env_paths.read_bytes()
    monkeypatch.setattr(setup_env, "oc_has_data", lambda: True)
    setup_env.main([])
    assert env_paths.read_bytes() == before
    assert "Nothing changed" in capsys.readouterr().out


def test_never_prints_a_secret(env_paths, monkeypatch, capsys):
    monkeypatch.setattr(setup_env, "oc_has_data", lambda: False)
    setup_env.main([])
    out = capsys.readouterr().out
    for value in _values(env_paths).values():
        if len(value) > 8:
            assert value not in out

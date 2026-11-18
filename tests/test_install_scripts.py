"""The one-line installs (scripts/install.sh for Linux, scripts/install.ps1
for Windows), docs/design/granny-installer.md step 4.

install.sh is run for real on Linux, with a pretend Docker engine and
internet (small scripts on PATH that record what they're asked), so the
test needs no engine, downloads nothing and changes nothing outside a
temporary folder."""
import base64
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SH = ROOT / "scripts" / "install.sh"
PS1 = ROOT / "scripts" / "install.ps1"
SH_TEXT = SH.read_text(encoding="utf-8")
PS1_TEXT = PS1.read_text(encoding="utf-8")
LATEST = "https://github.com/TracySmithConsulting/freehand/releases/latest/download/"

on_linux = pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("bash"),
                              reason="install.sh runs on Linux")


# ── What they are ───────────────────────────────────────────────────────

def test_the_linux_install_uses_the_published_images_not_python():
    assert "compose pull" in SH_TEXT and "compose up -d" in SH_TEXT
    assert "pip install" not in SH_TEXT and "venv" not in SH_TEXT
    assert LATEST + "compose.yml" in SH_TEXT


def test_the_linux_install_never_runs_a_downloaded_script():
    # The engine comes from the Linux's own packages, after asking.
    assert "get.docker.com" not in SH_TEXT
    assert "| bash" not in SH_TEXT.split("set -euo pipefail", 1)[1]
    assert 'ask "Install it now?"' in SH_TEXT
    assert "< /dev/tty" in SH_TEXT  # it can ask even through curl | bash


def test_the_linux_install_has_unix_line_endings():
    assert b"\r\n" not in SH.read_bytes()


def test_the_windows_install_gets_the_freehand_app():
    assert "releases/download/helper-latest/FreeHand.exe" in PS1_TEXT
    assert "Start-Process -FilePath $exe" in PS1_TEXT
    assert "pip install" not in PS1_TEXT and "winget" not in PS1_TEXT


def test_the_windows_install_leaves_the_persons_window_open_and_as_it_was():
    # irm | iex runs in the person's own window: `exit` would close it before
    # they could read a problem, and settings changed outside a function stay.
    code = [line.split("#", 1)[0] for line in PS1_TEXT.splitlines()]
    assert not any(re.search(r"\bexit\b", line) for line in code)
    body = PS1_TEXT[PS1_TEXT.index("function Install-FreeHand {"):]
    assert '$ErrorActionPreference = "Stop"' in body
    assert PS1_TEXT.count("$ErrorActionPreference") == 1
    assert PS1_TEXT.rstrip().endswith("Install-FreeHand")


def test_the_windows_install_works_in_powershell_7_too():
    assert "-Encoding Byte" not in PS1_TEXT  # Windows PowerShell only
    assert "[IO.File]::OpenRead(" in PS1_TEXT


def test_the_windows_install_checks_what_it_downloaded():
    assert "0x4D" in PS1_TEXT and "0x5A" in PS1_TEXT  # a Windows program
    assert "Get-AuthenticodeSignature" in PS1_TEXT


def test_releases_carry_the_install_files_and_the_readme_points_at_them():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "cp scripts/install.sh scripts/install.ps1 install/compose.yml install-files/" in workflow
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"curl -fsSL {LATEST}install.sh | bash" in readme
    assert f"irm {LATEST}install.ps1 | iex" in readme
    assert "freehand.tracysmith.co.za/install" not in readme  # that site doesn't exist yet


# ── Running install.sh ──────────────────────────────────────────────────

def _pretend(tmp_path, *, docker=True, system="Linux"):
    """A folder of pretend commands, and the environment to run with them."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log.txt"

    def tool(name, body):
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    if docker:
        tool("docker", f'echo "docker $*" >> "{log}"\nexit 0\n')
    tool("curl", f'''echo "curl $*" >> "{log}"
out=""
while [ $# -gt 0 ]; do
    if [ "$1" = "-o" ]; then out="$2"; shift; fi
    shift
done
if [ -n "$out" ] && [ "$out" != /dev/null ]; then cp "{ROOT / 'install' / 'compose.yml'}" "$out"; fi
exit 0
''')
    tool("ss", "exit 0\n")
    # Waits between checks are skipped; the one-minute "still going" pause
    # really waits (it's stopped long before), so it doesn't repeat.
    tool("sleep", 'if [ "$1" = 60 ]; then exec /bin/sleep 30; fi\nexit 0\n')
    tool("xdg-open", f'echo "xdg-open $*" >> "{log}"\n')
    tool("uname", f'echo {system}\n')
    home = tmp_path / "freehand"
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env.update(PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}", FREEHAND_HOME=str(home), HOME=str(tmp_path))
    return env, home, log


def _run(env):
    return subprocess.run(["bash", str(SH)], env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)


@on_linux
def test_it_sets_up_and_starts_freehand(tmp_path):
    env, home, log = _pretend(tmp_path)
    r = _run(env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FreeHand is ready: http://localhost:8000" in r.stdout
    assert (home / "compose.yml").read_text(encoding="utf-8") == (ROOT / "install" / "compose.yml").read_text(encoding="utf-8")
    assert (home / "vault").is_dir()
    calls = log.read_text(encoding="utf-8")
    assert "docker compose pull --quiet" in calls and "docker compose up -d" in calls
    assert calls.index("compose pull") < calls.index("compose up -d")


@on_linux
def test_its_secrets_are_random_private_and_kept(tmp_path):
    env, home, _ = _pretend(tmp_path)
    assert _run(env).returncode == 0
    env_file = home / ".env"
    first = env_file.read_text(encoding="utf-8")
    values = dict(line.split("=", 1) for line in first.splitlines())
    assert set(values) == {"OC_ADMIN_TOKEN", "OC_RUNTIME_TOKEN", "OC_ENCRYPTION_KEY", "FREEHAND_LLM_PROXY_TOKEN", "BRIDGE_SECRET"}
    for name in ("OC_ADMIN_TOKEN", "OC_RUNTIME_TOKEN", "FREEHAND_LLM_PROXY_TOKEN", "BRIDGE_SECRET"):
        assert len(values[name]) == 64 and int(values[name], 16) >= 0, name
    assert len(base64.b64decode(values["OC_ENCRYPTION_KEY"])) == 32
    assert len(set(values.values())) == 5
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    # Running it again (an update) keeps them.
    assert _run(env).returncode == 0
    assert env_file.read_text(encoding="utf-8") == first


@on_linux
def test_it_says_so_plainly_on_a_mac(tmp_path):
    env, home, _ = _pretend(tmp_path, system="Darwin")
    r = _run(env)
    assert r.returncode == 1
    assert "FreeHand for Mac comes later" in r.stdout
    assert not home.exists()

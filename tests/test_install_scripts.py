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

ROOT = pathlib.Path(__file__).resolve().parent.parent
SH = ROOT / "scripts" / "install.sh"
PS1 = ROOT / "scripts" / "install.ps1"
SH_TEXT = SH.read_text(encoding="utf-8")
PS1_TEXT = PS1.read_text(encoding="utf-8")
LATEST = "https://github.com/TracySmithConsulting/freehand/releases/latest/download/"

# install.sh runs on Linux, so the tests that run it are only defined there.
RUNS_HERE = sys.platform.startswith("linux") and bool(shutil.which("bash"))


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


def test_the_linux_install_checks_memory_before_changing_anything():
    body = SH_TEXT.split("set -euo pipefail", 1)[1]
    assert body.index("MemTotal") < body.index("have_engine || install_engine")
    assert "needs at least 2 GB" in SH_TEXT


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

def _pretend(tmp_path, *, docker=True, system="Linux", memory_kb=8000000, os_release='ID=ubuntu\nPRETTY_NAME="Ubuntu 24.04 LTS"\n'):
    """A folder of pretend commands, and the environment to run with them.
    docker=False: a docker that isn't working, so the engine is needed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log.txt"
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal:       {memory_kb} kB\nMemFree:          100000 kB\n", encoding="utf-8")
    release = tmp_path / "os-release"
    release.write_text(os_release, encoding="utf-8")

    def tool(name, body):
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    if docker:
        # Like the real one, it talks: that goes to the install log, not the screen.
        tool("docker", f'echo "docker $*" >> "{log}"\necho "Container freehand-app Started"\nexit 0\n')
    else:
        tool("docker", "exit 1\n")
    for manager in ("apt-get", "dnf", "sudo", "systemctl", "service"):
        tool(manager, f'echo "{manager} $*" >> "{log}"\nexit 0\n')
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
    env.update(PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}", FREEHAND_HOME=str(home), HOME=str(tmp_path),
               FREEHAND_MEMINFO=str(meminfo), FREEHAND_OS_RELEASE=str(release))
    return env, home, log


def _run(env):
    return subprocess.run(["bash", str(SH)], env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)


if RUNS_HERE:
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
        # Docker's own messages are kept out of the way, in the log.
        assert "Container freehand-app Started" not in r.stdout
        assert "Container freehand-app Started" in (home / "install-log.txt").read_text(encoding="utf-8")
        assert "about 7 GB of space" in r.stdout


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


    def test_on_a_computer_with_too_little_memory_it_stops_first(tmp_path):
        env, home, log = _pretend(tmp_path, docker=False, memory_kb=510000)  # Oracle's free "Micro"
        r = _run(env)
        assert r.returncode == 1
        assert "about 0.5 GB of memory, and FreeHand needs at least 2 GB" in r.stdout
        assert not home.exists() and not log.exists()  # nothing installed, nothing downloaded


    def test_on_oracle_linux_it_says_where_docker_comes_from(tmp_path):
        env, home, log = _pretend(tmp_path, docker=False, os_release=(
            'NAME="Oracle Linux Server"\nID="ol"\nID_LIKE="fedora"\nPRETTY_NAME="Oracle Linux Server 9.8"\n'))
        env["FREEHAND_YES"] = "1"
        r = _run(env)
        assert r.returncode == 1
        assert "isn't in Oracle Linux Server 9.8's own packages" in r.stdout
        assert "docs.docker.com/engine/install/rhel" in r.stdout
        assert not log.exists()  # no package manager was run


    def test_on_ubuntu_without_docker_it_installs_it_from_ubuntus_packages(tmp_path):
        env, _, log = _pretend(tmp_path, docker=False)
        env["FREEHAND_YES"] = "1"
        r = _run(env)
        # The pretend engine still doesn't work afterwards, so it stops there.
        assert "apt-get install -y docker.io docker-compose-v2" in r.stdout
        assert "apt-get" in log.read_text(encoding="utf-8")


    def test_it_says_so_plainly_on_a_mac(tmp_path):
        env, home, _ = _pretend(tmp_path, system="Darwin")
        r = _run(env)
        assert r.returncode == 1
        assert "FreeHand for Mac comes later" in r.stdout
        assert not home.exists()

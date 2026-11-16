"""The FreeHand app's engine (helper/windows/Engine.cs): FreeHand's own
small Linux with the free Docker engine, as proven on a real PC
(docs/design/granny-installer.md, "Proven on a real PC"). These checks
read the code, so they run anywhere; the app itself is built and checked
on Windows (helper/windows/build.cmd, --selftest-engine)."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE = (ROOT / "helper" / "windows" / "Engine.cs").read_text(encoding="utf-8")
BUILD = (ROOT / "helper" / "windows" / "build.cmd").read_text(encoding="utf-8")
DESIGN = (ROOT / "docs" / "design" / "granny-installer.md").read_text(encoding="utf-8")


def test_the_linux_system_is_checked_before_it_is_used():
    sha = re.search(r'AlpineSha256 = "([0-9a-f]{64})"', ENGINE).group(1)
    name = re.search(r'AlpineFile = "([^"]+)"', ENGINE).group(1)
    assert name in DESIGN  # the file that was proven
    assert "Sha256(file) != AlpineSha256" in ENGINE and "File.Delete(file)" in ENGINE
    assert len(sha) == 64


def test_the_engine_gets_its_own_network_ranges():
    assert "--bip 172.31.200.1/24 --default-address-pool base=172.31.208.0/20,size=24" in ENGINE
    assert "--bip 172.31.200.1/24" in DESIGN


def test_secrets_are_made_with_real_randomness_and_kept():
    assert "RandomNumberGenerator.Create()" in ENGINE
    assert "Get-Random" not in ENGINE and "new Random(" not in ENGINE
    # Made once: changing them later would lock FreeHand out of its own connections.
    assert 'test -s " + AppDir + "/.env' in ENGINE and "chmod 600" in ENGINE


def test_the_install_setup_is_built_into_the_app():
    assert r'/resource:"%~dp0..\..\install\compose.yml",compose.yml' in BUILD
    assert '"%~dp0Engine.cs"' in BUILD
    assert 'GetManifestResourceStream("compose.yml")' in ENGINE


def test_the_folder_is_made_before_the_linux_is_imported():
    made = ENGINE.index('Directory.CreateDirectory(Path.Combine(Root, "linux"))')
    imported = ENGINE.index('"--import {0}')
    assert made < imported


def test_switching_wsl_on_is_the_persons_choice():
    # Windows asks "allow changes?"; a no (error 1223) is an answer, not a crash.
    assert 'Verb = "runas"' in ENGINE and "NativeErrorCode == 1223" in ENGINE


def test_uninstall_can_keep_the_data_first():
    keep = ENGINE[ENGINE.index("public static string Uninstall(bool keepData)"):]
    assert keep.index("tar -cf") < keep.index("--unregister")

"""The FreeHand app lets FreeHand read Hermes and OpenClaw (core/folders.py),
read-only, when the person chooses: a tick box when installing, or the
tray menu for a FreeHand already installed. These checks read the code, so
they run anywhere; the app is built and tried on Windows (build.cmd)."""
import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "helper" / "windows"
ENGINE = (APP / "Engine.cs").read_text(encoding="utf-8")
SETUP = (APP / "Setup.cs").read_text(encoding="utf-8")
TRAY = (APP / "FreeHandHelper.cs").read_text(encoding="utf-8")


def test_the_released_compose_shows_freehand_the_folders_read_only():
    compose = yaml.safe_load((ROOT / "install" / "compose.yml").read_text(encoding="utf-8"))
    mounts = compose["services"]["freehand"]["volumes"]
    for name, target in (("HERMES", "/host/hermes"), ("OPENCLAW", "/host/openclaw"), ("NOTES", "/host/notes")):
        assert f"${{FREEHAND_{name}_DIR:-./host-folders/none}}:{target}:ro" in mounts


def test_it_looks_where_hermes_and_openclaw_live_on_windows():
    found = ENGINE[ENGINE.index("public static Dictionary<string, string> FoundFolders()"):]
    assert 'Path.Combine(local, "hermes"), "SOUL.md"' in found
    assert 'Path.Combine(home, ".openclaw")' in found and 'Path.Combine("workspace", "SOUL.md")' in found


def test_windows_folders_become_the_linux_side_paths():
    wsl = ENGINE[ENGINE.index("public static string WslPath(string windowsPath)"):]
    assert '"/mnt/" + char.ToLowerInvariant(windowsPath[0])' in wsl and "Replace('\\\\', '/')" in wsl


def test_a_new_freehand_shares_them_only_if_the_box_stays_ticked():
    new_env = ENGINE[ENGINE.index('if (!Yes("test -s " + AppDir + "/.env", 60))'):]
    new_env = new_env[:new_env.index("// ── Hermes and OpenClaw")]
    assert "if (ShareFolders) AddFoldersToEnv(FoundFolders());" in new_env
    assert "Engine.ShareFolders = share == null || share.Checked; Install();" in SETUP
    assert "It never changes them." in SETUP and "if (found.Count > 0)" in SETUP


def test_each_folder_is_added_once_and_safely():
    add = ENGINE[ENGINE.index("static void AddFoldersToEnv("):]
    add = add[:add.index("public static Dictionary<string, string> UnsharedFolders()")]
    assert "InEnv(kv.Key)) continue;" in add
    assert "Convert.ToBase64String" in add and "base64 -d >> " in add  # a space or quote can't break it


def test_an_installed_freehand_asks_first_then_restarts():
    assert 'menu.Items.Add("Let FreeHand read " + Engine.FolderNames(found.Keys)' in TRAY
    share = TRAY[TRAY.index("void ShareFoldersLocal()"):TRAY.index("void UninstallLocal()")]
    asked = share.index("MessageBoxButtons.YesNo")
    assert asked < share.index("Engine.ShareFoldersNow(unshared)")
    assert "never changes them" in share and "restarts" in share
    now = ENGINE[ENGINE.index("public static void ShareFoldersNow("):]
    assert now.index("AddFoldersToEnv(folders)") < now.index('Compose("up -d", 900)')

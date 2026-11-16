"""The FreeHand app's setup screens and tray menu (helper/windows/Setup.cs,
FreeHandHelper.cs). These checks read the code, so they run anywhere; on
Windows, --selftest-setup builds every screen and reports its words,
buttons and first focus without changing anything."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SETUP = (ROOT / "helper" / "windows" / "Setup.cs").read_text(encoding="utf-8")
APP = (ROOT / "helper" / "windows" / "FreeHandHelper.cs").read_text(encoding="utf-8")
BUILD = (ROOT / "helper" / "windows" / "build.cmd").read_text(encoding="utf-8")


def test_the_screens_are_built_into_the_app():
    assert '"%~dp0Setup.cs"' in BUILD


def test_people_already_using_the_helper_see_no_setup():
    # A paired Helper (with a FreeHand elsewhere) carries on exactly as before.
    start = APP[APP.index("bool background = Array.IndexOf"):APP.index("Application.Run(new MainForm(background))")]
    assert 'if (settings.Mode.Length == 0 && settings.Paired) { settings.Mode = "server"; settings.Save(); }' in start
    assert start.index('settings.Mode = "server"') < start.index("new SetupForm(settings)")


def test_hosted_freehand_needs_only_the_helper():
    assert "On a server: I was given a web address for my FreeHand" in SETUP
    assert 'settings.Mode = "server"' in SETUP


def test_each_screen_is_read_out_by_its_heading():
    assert "AccessibleName = heading" in SETUP
    assert 'Text = "Set up FreeHand: " + heading' in SETUP
    assert SETUP.count("words.Focus();") >= 5


def test_progress_is_read_out_step_by_step():
    assert "steps.SelectedIndex = steps.Items.Count - 1" in SETUP


def test_the_main_button_comes_first_when_tabbing():
    assert "TabIndex = main ? 0 : 10 + buttons.Controls.Count" in SETUP


def test_switching_wsl_on_carries_on_after_the_restart():
    assert r'@"Software\Microsoft\Windows\CurrentVersion\RunOnce"' in SETUP and '" --setup"' in SETUP
    assert "Restart later" in SETUP  # never forced


def test_it_starts_with_windows_in_the_background_and_can_be_switched_off():
    assert r'@"Software\Microsoft\Windows\CurrentVersion\Run"' in SETUP and '" --background"' in SETUP
    assert 'key.DeleteValue("FreeHand", false)' in SETUP
    assert "Start FreeHand when Windows starts" in SETUP


def test_the_tray_menu_runs_freehand_on_this_computer():
    for item in ("Open FreeHand", "Update FreeHand", "Turn FreeHand off", "Turn FreeHand on", "Uninstall FreeHand"):
        assert f'menu.Items.Add("{item}"' in APP, item
    assert 'if (settings.Mode == "local") TurnOnLocal();' in APP


def test_uninstalling_asks_about_keeping_the_data():
    part = APP[APP.index("void UninstallLocal()"):]
    assert "MessageBoxButtons.YesNoCancel" in part and "Keep your FreeHand data" in part
    assert "Engine.Uninstall(keep)" in part


def test_messages_from_the_tray_come_to_the_front():
    assert "TopMost = true" in APP and "static DialogResult OnTop(" in APP


def test_the_preview_changes_nothing():
    done = SETUP[SETUP.index("void Done(string first)"):]
    assert done.index("if (!PreviewOnly)") < done.index("settings.Save()")
    assert "if (!PreviewOnly) StartWithWindows(auto.Checked)" in SETUP

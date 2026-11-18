"""The FreeHand app says how far it has got during the long steps
(helper/windows/Engine.cs). On Windows, --selftest-pull feeds recorded
download reports through the meter and writes what a person would hear."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE = (ROOT / "helper" / "windows" / "Engine.cs").read_text(encoding="utf-8")
SETUP = (ROOT / "helper" / "windows" / "Setup.cs").read_text(encoding="utf-8")
APP = (ROOT / "helper" / "windows" / "FreeHandHelper.cs").read_text(encoding="utf-8")


def _part(text, start, end):
    return text[text.index(start):text.index(end)]


def test_the_big_download_reports_its_progress():
    pull = _part(ENGINE, "static void Pull(", "public class PullMeter")
    assert 'ComposeLines("--progress json pull"' in pull
    install = _part(ENGINE, "public static void Install(", "static void Pull(")
    assert "Pull(progress, 30, 89)" in install
    assert 'Compose("pull --quiet"' not in install


def test_it_speaks_about_once_a_minute_and_the_bar_moves_in_between():
    meter = _part(ENGINE, "public class PullMeter", "static string Field(")
    assert "new PullMeter(progress, from, to, 60)" in ENGINE
    assert '"Downloaded " + Size(got) + " of about " + Size(all)' in meter
    assert "say ? " in meter and ": null" in meter  # between times: the bar only
    assert "Math.Max(Shown, pct)" in meter  # never goes backwards


def test_an_engine_that_cant_report_progress_still_downloads():
    pull = _part(ENGINE, "static void Pull(", "public class PullMeter")
    assert "if (o.Code != 0 && !meter.Heard)" in pull
    assert 'Compose("pull --quiet"' in pull and "Still downloading FreeHand" in pull


def test_the_engine_download_says_which_part_its_on():
    assert '"Downloading the engine: part {0} of {1}."' in ENGINE
    assert "Installing\")" in ENGINE or "Installing\"" in ENGINE


def test_silent_steps_say_theyre_still_going():
    assert "Still starting the engine." in ENGINE
    assert "Still starting FreeHand." in ENGINE
    assert "Still waiting for FreeHand to answer." in ENGINE


def test_a_bar_only_update_adds_nothing_to_the_list():
    install = _part(SETUP, "void Install()", "void Done(")
    assert install.index("bar.Value =") < install.index("if (text == null) return;") < install.index("steps.Items.Add(")
    assert "if (text != null) work.Say(" in APP
    assert "if (text != null) Note(" in ENGINE


def test_the_sizes_are_honest():
    assert "1.8 GB" in SETUP and "7 GB of space" in SETUP  # measured Nov 2026: 7.1 GB unpacked
    assert "about 6 GB the first time" not in SETUP


def test_switching_wsl_on_warns_it_can_take_hours_on_an_older_computer():
    assert "On an older computer it can take an hour or two" in SETUP
    assert "This takes a minute or two." not in SETUP


def test_the_meter_can_be_checked_with_recorded_reports():
    assert '"--selftest-pull"' in APP and "meter.Feed(line)" in APP

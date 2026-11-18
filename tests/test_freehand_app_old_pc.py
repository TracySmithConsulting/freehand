"""What the first try on an old PC (a 10-year-old Dell, 17 November 2026)
taught the FreeHand app's installer (helper/windows/Engine.cs)."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE = (ROOT / "helper" / "windows" / "Engine.cs").read_text(encoding="utf-8")
SETUP = (ROOT / "helper" / "windows" / "Setup.cs").read_text(encoding="utf-8")


def _part(start, end):
    return ENGINE[ENGINE.index(start):ENGINE.index(end)]


def test_the_engine_gets_minutes_not_seconds_to_start():
    start = _part("public static void StartEngine(", "public static void Start()")
    assert "AddMinutes(4)" in start
    assert "i < 30" not in start


def test_when_the_engine_doesnt_start_it_says_why():
    start = _part("public static void StartEngine(", "public static void Start()")
    assert "tail -n 25 /var/log/dockerd.log" in start
    assert "Reason(why)" in start
    assert "engine != null && engine.HasExited" in start


def test_downloads_are_tried_again():
    alpine = _part("static string DownloadAlpine(", "public static void WriteSettings()")
    assert "attempt >= 3" in alpine
    assert "couldn't reach the internet" in alpine
    assert "for i in 1 2 3; do apk update" in ENGINE


def test_a_stuck_engine_download_from_last_time_is_stopped_first():
    assert "pkill -x apk" in ENGINE


def test_there_is_a_log_to_send_and_it_holds_no_secrets():
    assert 'Path.Combine(Root, "install-log.txt")' in ENGINE
    assert "Engine.LogFile" in SETUP
    # Commands (which carry the secrets being written) never reach the log.
    run = _part("public static Output Run(", "static Output Sh(")
    assert "Note(" not in run
    write = _part("static void WriteFile(", "static void Must(")
    assert "Note(" not in write


def test_checks_read_only_the_commands_own_answer():
    # On the Dell, "is the engine up?" said no while the engine was running
    # (its log said it was ready), so a second one was started. WSL's own
    # notices mixed into the answer are the likely cause.
    assert 'Out = stdout.Result' in ENGINE
    assert 'Text.Trim() == "yes"' not in ENGINE and 'Text.Trim() != "yes"' not in ENGINE
    yes = _part("static bool Yes(", "public static Output Run(")
    assert "o.Out.Split" in yes


def test_a_second_engine_is_never_started():
    start = _part("public static void StartEngine(", "public static void Start()")
    assert start.index('Yes("pgrep -x dockerd') < start.index("Process.Start(psi)")
    assert "rm -f /var/run/docker.pid" in start
    assert "What the check saw" in start

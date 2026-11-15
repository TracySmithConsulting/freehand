"""The test guard (scripts/guard_tests.py): pull requests can't quietly
weaken FreeHand's tests."""
import importlib.util
import pathlib
import subprocess

spec = importlib.util.spec_from_file_location(
    "guard_tests", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "guard_tests.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def test_counts_functions_and_methods_like_pytest():
    source = '''
def test_one(): pass
async def test_two(): pass
def helper(): pass
class TestThings:
    def test_a(self): pass
    def setup(self): pass
class Helpers:
    def test_not_collected(self): pass
'''
    assert guard.count_tests(source) == 3


def test_fewer_tests_fail_unless_a_maintainer_says_it_is_on_purpose():
    ok, message = guard.verdict({"tests": 10, "skips": 0}, {"tests": 8, "skips": 0}, overridden=False)
    assert not ok and "2 fewer tests" in message and guard.OVERRIDE_LABEL in message
    ok, message = guard.verdict({"tests": 10, "skips": 0}, {"tests": 8, "skips": 0}, overridden=True)
    assert ok and "maintainer" in message


def test_switching_tests_off_fails():
    ok, message = guard.verdict({"tests": 10, "skips": 1}, {"tests": 10, "skips": 3}, overridden=False)
    assert not ok and "switches off 2 more tests" in message


def test_more_tests_pass():
    ok, message = guard.verdict({"tests": 10, "skips": 1}, {"tests": 12, "skips": 1}, overridden=False)
    assert ok and "as strong as before" in message


def test_skip_markers_are_counted():
    # Built from pieces, so this file doesn't trip the guard itself.
    mark, plain = "pytest" + ".mark.", "pytest" + "."
    source = f"@{mark}skip\ndef test_a(): pass\n@{mark}xfail(reason='x')\ndef test_b(): {plain}skip('no')\n"
    assert len(guard.SKIP_PATTERN.findall(source)) == 3


def test_it_reads_this_repository():
    root = pathlib.Path(__file__).resolve().parent.parent
    if subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True).returncode != 0:
        return  # not a git checkout (an installed copy)
    import os
    here = os.getcwd()
    try:
        os.chdir(root)
        assert guard.measure("HEAD")["tests"] > 500
    finally:
        os.chdir(here)

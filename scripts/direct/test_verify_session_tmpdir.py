"""Verify gives local session steps a leased temporary folder."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import subprocess
from typing import BinaryIO
from unittest.mock import patch

from scripts.direct.verify_step_routes_test_support import VerifyStepRoutesFixture
from scripts.lib import build_folders
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class VerifySessionTempTests(VerifyStepRoutesFixture):
    def prepare_session_test(self, *, assigned: bool = True, local: bool = True) -> tuple[Path, Path]:
        self.prepare_repository()
        fixture = self.fixture
        if assigned:
            with patch.dict("os.environ", fixture.environment, clear=True):
                output = build_folders.output_path("fixture", "test", "worktree")
            output.parent.mkdir(parents=True)
            target = fixture.root / "target"
            _ = target.rename(output)
            target.symlink_to(output)
        fixture.environment.update({"CODEX_THREAD_ID": "", "TEST_MAC_ALLOWED": "0" if local else "1"})
        run = fixture.board
        run.mkdir(exist_ok=True)
        _ = (run / "test-prompt.txt").write_text("Test the sample package.\n")
        observed = fixture.root / "observed-tmpdir"
        fixture.environment["TEST_SESSION_TEMP_OBSERVED"] = str(observed)
        return run, observed

    def instrument_cargo(self, body: str) -> None:
        cargo = self.fixture.bin_directory / "cargo"
        source = cargo.read_text()
        self.assertIn("arguments = sys.argv[1:]", source)
        _ = cargo.write_text(source.replace("arguments = sys.argv[1:]", """
arguments = sys.argv[1:]
if arguments[:2] == ["nextest", "run"]:
    temporary = Path(os.environ["TMPDIR"])
    observed = Path(os.environ["TEST_SESSION_TEMP_OBSERVED"])
""" + body, 1))

    def assert_temp_released(self, temporary: Path, parent: Path) -> None:
        self.assertEqual(temporary.parent, parent)
        self.assertFalse(temporary.exists())
        self.assertFalse(build_folders.lease_path(temporary).exists())
        self.assertFalse(temporary.with_name(temporary.name + ".lock").exists())

    def test_session_test_uses_assigned_output_and_keeps_the_run_records(self) -> None:
        self.check_session_temp(0)

    def test_failed_session_test_releases_its_temporary_folder(self) -> None:
        self.check_session_temp(41)

    def check_session_temp(self, status: int) -> None:
        run, observed = self.prepare_session_test()
        self.fixture.environment["TEST_LOCAL_STATUS"] = str(status)
        self.instrument_cargo("""
    (temporary / "fixture-output").write_text("test output")
    observed.write_text(str(temporary))
""")
        result = self.run_verify("--session-dir", str(run), "test", "sample", "--local", "test runs locally", "--no-cache")
        self.assertEqual(result.returncode, status, result.stdout + result.stderr)
        output = (self.fixture.root / "target").resolve(strict=True)
        self.assert_temp_released(Path(observed.read_text()), output / "test-runs")
        self.assertEqual(list(output.parent.iterdir()), [output])
        self.assertFalse((run / "tmp").exists())
        self.assertEqual(list(run.rglob("fixture-output")), [])
        self.assertEqual((run / "test-prompt.txt").read_text(), "Test the sample package.\n")

    def test_term_stops_allocation_while_its_lock_is_held(self) -> None:
        run, observed = self.prepare_session_test()
        lock = self.fixture.root / "allocation.lock"
        ready = self.fixture.root / "allocation-started"
        helper = self.fixture.verify_script.with_name("verify_temp.py")
        source = helper.read_text()
        entry = 'if __name__ == "__main__":'
        self.assertIn(entry, source)
        _ = helper.write_text(source.replace(entry, f"""
import fcntl

def prepare_test_runs_parent(checkout):
    with Path({str(lock)!r}).open("r+") as stream:
        Path({str(ready)!r}).touch()
        fcntl.flock(stream, fcntl.LOCK_EX)

testing_folders.prepare_test_runs_parent = prepare_test_runs_parent

{entry}""", 1))
        with lock.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            process = self.fixture.start_verify("--session-dir", str(run), "test", "sample", "--local", "test runs locally")
            self.addCleanup(self.fixture.stop_verify, process)
            self.fixture.wait_for_path(ready, (process,))
            process.terminate()
            try:
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            except subprocess.TimeoutExpired:
                self.fail("Verify did not exit after SIGTERM during temporary folder allocation.")
            self.assertEqual(process.returncode, 143, output + errors)
            self.assertFalse(observed.exists())
            self.assertEqual(self.fixture.local_runs(), [])
        self.fixture.assert_nothing_outlives()

    def test_term_keeps_the_lease_until_the_step_group_exits(self) -> None:
        self.check_term_group_lease()

    def test_interrupted_group_wait_keeps_the_lease_until_the_step_group_exits(self) -> None:
        script = self.fixture.verify_script
        source = script.read_text()
        entry = "wait_for_verify_step_group() {"
        self.assertIn(entry, source)
        _ = script.write_text(source.replace(entry, """
wait() {
    if [[ " ${FUNCNAME[*]} " == *" wait_for_verify_step_group "* && -z "${TEST_STEP_WAIT_INTERRUPTED:-}" ]]; then
        TEST_STEP_WAIT_INTERRUPTED=1
        : > "$TEST_SESSION_TEMP_OBSERVED.interrupted"
        kill -USR1 "$BASHPID"
        return "$((128 + $(kill -l USR1)))"
    fi
    builtin wait "$@"
}

""" + entry, 1))
        self.check_term_group_lease()
        self.assertTrue((self.fixture.root / "observed-tmpdir.interrupted").exists())

    def check_term_group_lease(self) -> None:
        run, observed = self.prepare_session_test()
        self.instrument_cargo("""
    import signal
    def stopping(signum, _frame):
        observed.with_suffix(".stopping").touch()
        with observed.with_suffix(".release").open() as release:
            release.read(1)
        (temporary / "after-term").touch()
        observed.with_suffix(".finished").touch()
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stopping)
    os.close(1)
    os.close(2)
    observed.with_suffix(".group").write_text(str(os.getpgrp()))
    observed.write_text(str(temporary))
    while True:
        signal.pause()
""")
        release = observed.with_suffix(".release")
        os.mkfifo(release)
        descriptor = os.open(release, os.O_RDWR)
        with os.fdopen(descriptor, "wb", buffering=0) as release_step:
            self.check_term_step(run, observed, release_step)

    def check_term_step(self, run: Path, observed: Path, release_step: BinaryIO) -> None:
        # Skip the board token so the signal reaches the step's own group.
        process = self.fixture.start_verify("--session-dir", str(run), "test", "sample", "--local", "test runs locally",
                                            extra={"UNIT_TEAM_ROLE": "fixture"})
        self.addCleanup(self.fixture.stop_verify, process)
        self.fixture.wait_for_path(observed, (process,))
        temporary = Path(observed.read_text())
        self.assertNotEqual(int(observed.with_suffix(".group").read_text()), os.getpgid(process.pid))
        process.terminate()
        self.fixture.wait_for_path(observed.with_suffix(".stopping"))
        with patch.dict("os.environ", self.fixture.environment, clear=True):
            self.assertEqual(build_folders.build_folder_state(temporary), "held")
            build_folders.sweep_temporary_build_folders(temporary.parent, wait=False)
        self.assertTrue(temporary.is_dir())
        self.assertIsNone(process.poll())
        self.assertEqual(release_step.write(b"x"), 1)
        try:
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        except subprocess.TimeoutExpired:
            self.fail("Verify did not exit after the step group received its release.")
        self.assertEqual(process.returncode, 143, output + errors)
        self.assertTrue(observed.with_suffix(".finished").exists())
        self.assert_temp_released(temporary, (self.fixture.root / "target").resolve(strict=True) / "test-runs")
        self.fixture.assert_nothing_outlives()

    def test_unassigned_checkout_uses_run_temp_and_a_cache_hit_needs_no_folder(self) -> None:
        run, observed = self.prepare_session_test(assigned=False)
        self.instrument_cargo("    observed.write_text(str(temporary))\n")
        words = ("--session-dir", str(run), "test", "sample", "--local", "test runs locally")
        result = self.run_verify(*words)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fallback = [line for line in result.stderr.splitlines() if "verify.sh: using TMPDIR" in line]
        self.assertEqual(len(fallback), 1, result.stderr)
        self.assertIn("no assigned build output", fallback[0])
        self.assertEqual(Path(observed.read_text()), run / "tmp")
        observed.unlink()
        cached = self.run_verify(*words)
        self.assertEqual(cached.returncode, 0, cached.stdout + cached.stderr)
        self.assertNotIn("verify.sh: using TMPDIR", cached.stderr)
        self.assertFalse(observed.exists())
        self.assertEqual(len(self.fixture.local_runs()), 1)

    def test_mac_only_session_does_not_request_a_local_folder(self) -> None:
        run, _ = self.prepare_session_test(assigned=False, local=False)
        result = self.run_verify("--session-dir", str(run), "test", "sample", "--mac", "test runs on macOS", "--no-cache")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.mac_rows()), 1)
        self.assertEqual(self.fixture.local_runs(), [])
        self.assertNotIn("verify.sh: using TMPDIR", result.stderr)
        self.assertFalse((run / "tmp").exists())

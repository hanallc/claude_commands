"""Real verify.sh Mac copy results with disposable tools."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from typing import final
from scripts.direct.verify_mac_offload_test_support import VERIFY, VerifyMacOffloadFixture
from scripts.lib.shell_test_support import shell_functions
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class VerifyMacCopyResultsTests(VerifyMacOffloadFixture):
    def assert_cargo_scope_is_empty(self) -> None:
        scope = Path((self.runner_state / "cargo-scope").read_text())
        try:
            events = (scope / "cgroup.events").read_text().splitlines()
        except FileNotFoundError:
            self.assertFalse(scope.exists(), scope)
        else:
            self.assertIn("populated 0", events)

    def test_copy_pass_does_not_hide_a_local_scope_stop_error(self) -> None:
        state = self.root / "scope-release"
        _ = state.write_text("cannot stop the test scope\n")
        script = shell_functions(VERIFY, "settle_placed_scope", "run_local_test") + r'''
PLACEMENT_SCOPE_STATE=$1 COPY_WATCH_PID=watcher NEXTEST_ARGS=()
close_scope_launch() { return 1; }
run_nextest() { settle_placed_scope; }
stop_copy_watch() { COPY_WORD=won COPY_STOPPED=1 COPY_MAC=passed; }
finish_copy_win() { exit 0; }
run_local_test
'''
        result = subprocess.run(["bash", "-c", script, "test", str(state)],
                                env=self.environment, capture_output=True, text=True,
                                timeout=TEST_HANG_GUARD_SECS, check=False)
        self.assertEqual(result.returncode, 125, result.stdout + result.stderr)
        self.assertIn("cannot stop the test scope", result.stderr)

    def test_copy_failure_first_stops_natedev_and_exits_with_the_copy_status(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_MAC": "failed", "TEST_COPY_STATUS": "42"})
        result = self.finish_verify(process)
        output, errors = result.stdout, result.stderr
        self.assertEqual(process.returncode, 42, output + errors)
        self.assert_cargo_scope_is_empty()
        self.assertEqual(self.saved_results(), [])
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_reason"], record["mac_s"]),
                         (42, "failed", "", 2.5))

    def test_copy_filtered_pass_first_stops_natedev_and_records_a_mac_pass(self) -> None:
        process, _ = self.start_copy_call("--filter", "alpha", copy={"TEST_COPY_MAC": "passed", "TEST_COPY_STATUS": "0"})
        result = self.finish_verify(process)
        output, errors = result.stdout, result.stderr
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", output)
        self.assert_cargo_scope_is_empty()
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_filter", 2.5))

    def test_copy_full_pass_first_stops_natedev_and_ends_the_call(self) -> None:
        process, _ = self.start_copy_call(copy={"TEST_COPY_MAC": "passed", "TEST_COPY_STATUS": "0"})
        result = self.finish_verify(process)
        output, errors = result.stdout, result.stderr
        self.assertEqual(process.returncode, 0, output + errors)
        self.assertIn("PASS on the Mac (macOS), so nothing ran on natedev.", output)
        self.assert_cargo_scope_is_empty()
        saved = self.saved_results()
        self.assertEqual(len(saved), 1)
        self.assertIn("host=mac\n", saved[0].read_text())
        self.assert_copy_record("won", "mac")
        record = self.last_call()
        self.assertEqual((record["status"], record["mac"], record["mac_s"]), (0, "passed_full", 2.5))


if __name__ == "__main__":
    _ = unittest.main()

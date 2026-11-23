"""Real verify.sh Mac signals with disposable tools."""
from __future__ import annotations

import unittest
from typing import final
from scripts.direct.verify_mac_offload_test_support import TARGETS_WRAPPER, VERIFY, VerifyMacOffloadFixture
import signal
from pathlib import Path
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


CLEAR_REASON = 'ended: mac cleared by bob — "Mac system update". A Mac run cannot go back to its lines yet.'


@final
class VerifyMacSignalsTests(VerifyMacOffloadFixture):
    def test_runner_receives_each_forwarded_signal_before_local_work(self) -> None:
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal_number=signal_number):
                self.clear_observations()
                process = self.start_verify(
                    "test",
                    "sample",
                    "--no-cache",
                    extra={"TEST_RUNNER_HOLD": "1"},
                )
                try:
                    self.wait_for_path(self.runner_state / "runner-signals-ready")
                    process.send_signal(signal_number)
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                except BaseException:
                    self.stop_verify(process)
                    raise
                self.assertEqual(
                    process.returncode, 128 + signal_number, output + errors
                )
                self.assertEqual(
                    (self.runner_state / "runner-signal").read_text(
                        encoding="utf-8"
                    ),
                    str(signal.SIGTERM if signal_number == signal.SIGINT else signal_number),
                )
                self.assertEqual(self.local_runs(), [])
                runners = self.runner_events()
                self.assertEqual(len(runners), 1)
                self.assertFalse(runners[0]["token_held"])

    def test_a_runner_that_a_clear_ended_stops_its_caller_with_the_reason(self) -> None:
        # The runner died with its claim kept; the run-end settle writes the refused result.
        self.check_caller_stops_for_a_clear({"TEST_RUNNER_MAC": "lost", "TEST_RUNNER_ENDED_BY": CLEAR_REASON})

    def test_a_runner_that_settled_itself_after_a_clear_stops_its_caller_with_the_reason(self) -> None:
        # The runner released its own claim after the clear and wrote the refused result; no kept claim remains.
        self.check_caller_stops_for_a_clear({"TEST_RUNNER_MAC": "refused", "TEST_RUNNER_REASON": CLEAR_REASON})

    def check_caller_stops_for_a_clear(self, runner: dict[str, str]) -> None:
        wrapper = self.root / "targets-wrapper.py"
        self.write_executable(wrapper, TARGETS_WRAPPER)
        result = self.verify("test", "sample", "--no-cache", extra={
            "BUILD_TARGETS_SCRIPT": str(wrapper),
            "TEST_REAL_TARGETS": str(VERIFY.parent.parent / "build_targets" / "build_targets.py"),
            "TEST_RUNNER_STATUS": "143", **runner})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr + repr(self.routing_events()))
        self.assertIn(f"verify.sh: {CLEAR_REASON}\n", result.stderr)
        # The caller stops: one Mac run, no local run, and no second placement or release wait.
        runners = self.runner_events()
        self.assertEqual(len(runners), 1)
        self.assertEqual(self.local_runs(), [])
        # The exit removes the result file and records the run as a refused Mac run.
        arguments = runners[0]["args"]
        self.assertFalse(Path(arguments[arguments.index("--result") + 1]).exists())
        call = self.last_call()
        self.assertEqual((call["mac"], call["mac_reason"], call["status"]), ("declined", "blocked", 2))
        commands = [event["command"] for event in self.routing_events()]
        self.assertEqual(commands.count("reserve"), 1, commands)
        self.assertNotIn("wait-local", commands)

if __name__ == "__main__":
    _ = unittest.main()

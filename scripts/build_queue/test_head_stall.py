"""Check the once-per-stall alert for a queue head behind idle holders."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import tempfile
from typing import override
import unittest
from unittest import mock

from scripts.build_queue import admission_records as records, head_stall
from scripts.build_queue.queue_snapshot import QueueSnapshot
from scripts.lint.memory_test_support import admission_policy_fixture

GIB = records.GIB
NOW = 1_800_000_000.0
LIMIT = 30 * 60


def holder(started_at: float, *, measured: records.StepMemory | None = None) -> records.RunningStep:
    return records.RunningStep("session", "", "check", "/home/u/rust/devtools-chooserux", 12 * GIB,
                               measured or records.MeasuredMemory(GIB // 10), pid=706237, started_at=started_at)


def waiter(since: float) -> records.Waiter:
    return {"pid": 1618939, "start_time": "19509458", "need": 15 * GIB, "since": since,
            "step": "nextest", "folder": "/build/claude/scratch/lfo-trigger-fires-bc6b76"}


def snapshot(waiters: list[records.Waiter], running: tuple[records.RunningStep, ...]) -> QueueSnapshot:
    inputs = records.QueueInputs([(Path(f"{row['pid']}.waiting"), row) for row in waiters], running, (),
        records.HostMemory(25 * GIB, 60 * GIB), records.NoPressureHold(), records.ReadyToStart(), NOW)
    return QueueSnapshot(NOW, inputs, admission_policy_fixture())


STALLED = snapshot([waiter(NOW - 3 * 3600)], (holder(NOW - 3 * 3600),))


class FindStallTests(unittest.TestCase):
    def test_head_behind_an_idle_holder_with_no_start_is_a_stall(self) -> None:
        stall = head_stall.find_stall(STALLED, NOW - 2 * 3600, NOW, LIMIT)
        assert isinstance(stall, head_stall.HeadStall)
        self.assertEqual((stall.key, stall.need, round(stall.waited), round(stall.quiet)),
                         (f"1618939:19509458:{int(NOW - 2 * 3600)}", 15 * GIB, 3 * 3600, 2 * 3600))
        self.assertEqual(stall.holders, (head_stall.Holder("check", "/home/u/rust/devtools-chooserux", 706237, 12 * GIB, GIB // 10),))
        self.assertEqual(head_stall.builds_text(stall).splitlines(), [
            "The build queue head has waited 180 min. No step started for 120 min.",
            "Head: nextest in lfo-trigger-fires-bc6b76 (pid 1618939) needs 15.0 GiB.",
            "Holders:",
            "- check in devtools-chooserux (pid 706237): promise 12.0 GiB, uses 0.1 GiB",
            "The head starts only when a holder ends. Check each holder that uses much less than its promise."])
        self.assertEqual(head_stall.user_body(stall), "The next build has waited 180 min for 15.0 GiB. "
                         + "No build started for 120 min. 1 build holds the memory it needs.")

    def test_short_waits_recent_starts_and_empty_queues_are_not_stalls(self) -> None:
        cases = {
            "head waits less than the limit": (snapshot([waiter(NOW - LIMIT + 60)], (holder(NOW - 3600),)), 0.0),
            "a step was admitted recently": (STALLED, NOW - 300),
            "a holder started recently": (snapshot([waiter(NOW - 3600)], (holder(NOW - 300),)), 0.0),
            "nothing holds memory": (snapshot([waiter(NOW - 3600)], ()), 0.0),
            "nothing waits": (snapshot([], (holder(NOW - 3600),)), 0.0),
            "only zero-need steps run": (snapshot([waiter(NOW - 3600)], (replace(holder(NOW - 3600), promise=0),)), 0.0),
        }
        for name, (queue, admitted) in cases.items():
            with self.subTest(name):
                self.assertEqual(head_stall.find_stall(queue, admitted, NOW, LIMIT), head_stall.NoStall())

    def test_a_recent_zero_need_start_does_not_hide_the_stall(self) -> None:
        gate = replace(holder(NOW - 60), step="merge-gate", promise=0)
        stall = head_stall.find_stall(snapshot([waiter(NOW - 3600)], (holder(NOW - 3600), gate)), 0.0, NOW, LIMIT)
        assert isinstance(stall, head_stall.HeadStall)
        self.assertEqual([row.step for row in stall.holders], ["check"])

    def test_a_holder_without_a_reading_shows_unknown_use(self) -> None:
        queue = snapshot([waiter(NOW - 3600)], (holder(NOW - 3600, measured=records.GoneMemory()),))
        stall = head_stall.find_stall(queue, 0.0, NOW, LIMIT)
        assert isinstance(stall, head_stall.HeadStall)
        self.assertIn("promise 12.0 GiB, uses unknown", head_stall.builds_text(stall))


class LastAdmissionTests(unittest.TestCase):
    def test_reads_the_newest_admitted_row_with_need_and_skips_other_rows(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            times = Path(folder) / "queue_times.jsonl"
            rows = [{"kind": "admitted", "admitted_at": NOW - 600, "need": GIB},
                    {"kind": "admitted", "admitted_at": NOW - 60, "need": GIB},
                    {"kind": "admitted", "admitted_at": NOW - 30, "need": 0},
                    {"kind": "released", "at": NOW}]
            _ = times.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows) + "{broken\n")
            self.assertEqual(head_stall.last_admission(times), NOW - 60)
            self.assertEqual(head_stall.last_admission(Path(folder) / "missing.jsonl"), 0.0)


class WatchTests(unittest.TestCase):
    paths: head_stall.StallPaths = head_stall.StallPaths(Path(), Path())
    sent: list[list[str]] = []

    @override
    def setUp(self) -> None:
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.paths = head_stall.StallPaths(Path(folder) / "admission", Path(folder) / "head_stall.json")
        _ = self.enterContext(mock.patch.object(head_stall, "configured_limits", return_value=(LIMIT, 120.0)))
        self.sent = []

    def stall(self, key: str) -> head_stall.HeadStall:
        found = head_stall.find_stall(STALLED, 0.0, NOW, LIMIT)
        assert isinstance(found, head_stall.HeadStall)
        return head_stall.HeadStall(key, found.step, found.folder, found.pid, found.need, found.waited, found.quiet, found.holders)

    def run_send(self, result: bool) -> Callable[[list[str], str], bool]:
        def run(command: list[str], _text: str) -> bool:
            self.sent.append(command)
            return result
        return run

    def test_one_alert_per_stall_and_a_new_stall_alerts_again(self) -> None:
        for key, expected in (("1:a", 2), ("1:a", 2), ("2:b", 4)):
            with self.subTest(key=key), mock.patch.object(head_stall, "current_stall", return_value=self.stall(key)):
                _ = head_stall.watch_once(self.paths, NOW, self.run_send(True))
                self.assertEqual(len(self.sent), expected)
        builds, user = self.sent[2], self.sent[3]
        self.assertEqual(builds[builds.index("--to") + 1], "builds")
        self.assertEqual(user[user.index("--text-id") + 1], "alert.blocked")
        self.assertEqual(user[user.index("--key") + 1], "head-stall-2:b")
        self.assertTrue(user[user.index("--clears-when") + 1].endswith("head_stall.py cleared --head 2:b"))

    def test_failed_sends_retry_on_the_next_minute(self) -> None:
        with mock.patch.object(head_stall, "current_stall", return_value=self.stall("1:a")):
            _ = head_stall.watch_once(self.paths, NOW, self.run_send(False))
            self.assertFalse(self.paths.state.exists())
            _ = head_stall.watch_once(self.paths, NOW + 60, self.run_send(True))
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(json.loads(self.paths.state.read_text())["alerted"], "1:a")

    def test_cleared_exits_zero_once_the_named_head_no_longer_stalls(self) -> None:
        for current, code in ((self.stall("1:a"), 1), (self.stall("2:b"), 0), (head_stall.NoStall(), 0)):
            with self.subTest(current=current), mock.patch.object(head_stall, "current_stall", return_value=current):
                self.assertEqual(head_stall.main(["cleared", "--head", "1:a"]), code)


if __name__ == "__main__":
    _ = unittest.main()

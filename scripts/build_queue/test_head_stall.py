"""Check the once-per-stall alert for a queue head behind idle holders."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
import tempfile
from typing import cast, override
import unittest
from unittest import mock

from scripts.build_queue import admission_records as records, head_stall
from scripts.build_queue.queue_fixture import fixture_reservation
from scripts.build_queue.queue_snapshot import QueueSnapshot, write_queue_snapshot
from scripts.lint.memory_test_support import admission_policy_fixture

GIB = records.GIB
NOW = 1_800_000_000.0
LIMIT = 30 * 60
IDLE_SCOPE = Path("/cgroup/builds.slice/idle.scope")
COMPILER_SCOPE = records.ScopeSource(IDLE_SCOPE.parent / "sccache.service", frozenset({7}))
COMPILER_SCOPE_UNAVAILABLE = records.NoSource()


def holder(started_at: float, *, measured: records.StepMemory | None = None) -> records.RunningStep:
    return records.RunningStep("session", "", "check", "/home/u/rust/devtools-chooserux", 12 * GIB,
                               measured or records.MeasuredMemory(GIB // 10,
                                   anonymous=records.AnonymousRunUse(GIB // 10),
                                   anonymous_source=records.ScopeSource(IDLE_SCOPE, frozenset({706237}))),
                               pid=706237, started_at=started_at)


def waiter(since: float) -> records.Waiter:
    return {"pid": 1618939, "start_time": "19509458", "need": 15 * GIB, "since": since,
            "step": "nextest", "folder": "/build/claude/scratch/lfo-trigger-fires-bc6b76"}


def snapshot(waiters: list[records.Waiter], running: tuple[records.RunningStep, ...]) -> QueueSnapshot:
    reservations = tuple(fixture_reservation(step.pid, step.promise, step=step.step, folder=step.folder)
                         for step in running if isinstance(step.pid, int)
                         and not isinstance(step.measured, records.CompilerServiceMemory))
    inputs = records.QueueInputs([(Path(f"{row['pid']}.waiting"), row) for row in waiters], running, reservations,
        records.HostMemory(25 * GIB, 60 * GIB), records.NoPressureHold(), records.ReadyToStart(), NOW)
    return QueueSnapshot(NOW, inputs, admission_policy_fixture())


def compiler_service(*jobs: records.CompilerJob,
                     source: records.ScopeSource | records.NoSource = COMPILER_SCOPE_UNAVAILABLE) -> records.RunningStep:
    return records.RunningStep("", "sccache.service", "compile", "builds.slice", 0,
                               records.CompilerServiceMemory(GIB, pid=7, jobs=jobs, anonymous_source=source), pid=7)


STALLED = snapshot([waiter(NOW - 3 * 3600)], (holder(NOW - 3 * 3600),
    compiler_service(source=COMPILER_SCOPE)))


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

    def test_two_compiling_holders_count_their_own_sccache_jobs(self) -> None:
        first_scope = Path("/cgroup/builds.slice/first.scope")
        second_scope = Path("/cgroup/builds.slice/second.scope")
        first = replace(holder(NOW - 3600), step="nextest", folder="/build/first", pid=4094003,
            promise=125 * GIB // 10, measured=records.MeasuredMemory(GIB // 5,
                anonymous=records.AnonymousRunUse(GIB // 5),
                anonymous_source=records.ScopeSource(first_scope, frozenset({4094003}))))
        second_use = records.AnonymousRunUse(3 * GIB // 10)
        second_memory = records.MeasuredMemory(second_use.bytes, anonymous=second_use,
            anonymous_source=records.ScopeSource(second_scope, frozenset({1090772})))
        second = replace(holder(NOW - 3600), step="nextest", folder="/build/second", pid=1090772,
            promise=139 * GIB // 10, measured=second_memory)
        second_tree = replace(second, measured=replace(second_memory,
            anonymous_source=records.ProcessSource((records.ProcessAnonymousUse(1090772, second_use),))))
        first_job = records.CompilerJob(first_scope, records.AnonymousRunUse(10 * GIB))
        second_job = records.CompilerJob(second_scope, records.AnonymousRunUse(12 * GIB))
        unreadable_job = replace(second_job, use=records.RunUseUnavailable("compiler reading unavailable"))

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = head_stall.StallPaths(root, root / "stall.json")
            builds = root / "builds.slice"
            cache = builds / "sccache.service"
            cache.mkdir(parents=True)
            _ = (cache / "cgroup.procs").write_text("7\n8\n9\n")
            proc = root / "proc"
            for pid, scope, status in (
                (7, b"HOME=/h\0", ""),
                (8, f"BUILDLOG_STEP_SCOPE={first_scope}\0".encode(),
                 "VmRSS: 1024 kB\nRssAnon: 1024 kB\nRssShmem: 0 kB\n"),
                (10, f"BUILDLOG_STEP_SCOPE={second_scope}\0".encode(), "VmRSS: 1024 kB\n"),
                (11, f"BUILDLOG_STEP_SCOPE={second_scope}\0".encode(), "VmRSS: 1024 kB\n"),
            ):
                process = proc / str(pid)
                process.mkdir(parents=True)
                _ = (process / "environ").write_bytes(scope)
                _ = (process / "status").write_text(status)
            (proc / "9").mkdir()
            unreadable_status = replace(COMPILER_SCOPE, members=frozenset({7, 10}))
            two_unreadable_statuses = replace(COMPILER_SCOPE, members=frozenset({7, 10, 11}))
            _ = self.enterContext(mock.patch.object(head_stall, "PROC", proc))
            _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"BUILDLOG_BUILDS_CGROUP": str(builds)})))
            one_unreadable = ("sccache: 1 compiler not readable; uses can be low",)
            for name, second_holder, service, amounts, header in (
                ("both jobs", second, (compiler_service(first_job, second_job, source=COMPILER_SCOPE),),
                 ("10.2 GiB", "12.3 GiB"), ()),
                ("unreadable job", second, (compiler_service(first_job, unreadable_job, source=unreadable_status),),
                 ("10.2 GiB", "unknown"), one_unreadable),
                ("two unreadable compilers in one job", second,
                 (compiler_service(first_job, unreadable_job, source=two_unreadable_statuses),),
                 ("10.2 GiB", "unknown"), ("sccache: 2 compilers not readable; uses can be low",)),
                ("first holder has two compilers with one unreadable environment", second, (compiler_service(first_job),),
                 ("10.2 GiB", "0.3 GiB"), one_unreadable),
                ("missing job", second_tree,
                 (compiler_service(first_job, second_job, source=COMPILER_SCOPE),),
                 ("10.2 GiB", "0.3 GiB"), ()),
                ("complete read with no job for the second holder", second,
                 (compiler_service(first_job, source=COMPILER_SCOPE),),
                 ("10.2 GiB", "0.3 GiB"), ()),
                ("the process-tree holder owns no compiler job with an unreadable memory reading", second_tree,
                 (compiler_service(unreadable_job, source=unreadable_status),),
                 ("0.2 GiB", "0.3 GiB"), one_unreadable),
                ("the process-tree holder can rule out a job assigned to another holder", second_tree,
                 (compiler_service(first_job, source=COMPILER_SCOPE),),
                 ("10.2 GiB", "0.3 GiB"), ()),
                ("missing service", second, (),
                 ("0.2 GiB", "0.3 GiB"), ("sccache: compiler scan incomplete; uses can be low",)),
            ):
                with self.subTest(name=name):
                    queue = snapshot([waiter(NOW - 3600)], (first, second_holder, *service))
                    write_queue_snapshot(paths.admission, queue.inputs, queue.policy)
                    stall = head_stall.current_stall(paths, NOW, LIMIT, 120)
                    assert isinstance(stall, head_stall.HeadStall)
                    self.assertEqual(head_stall.builds_text(stall).splitlines()[2:-1], [
                        *header,
                        "Holders:",
                        f"- nextest in first (pid 4094003): promise 12.5 GiB, uses {amounts[0]}",
                        f"- nextest in second (pid 1090772): promise 13.9 GiB, uses {amounts[1]}",
                    ])

    def test_process_tree_holder_with_no_compiler_jobs_has_no_warning(self) -> None:
        use = records.AnonymousRunUse(GIB // 10)
        source = records.ProcessSource((records.ProcessAnonymousUse(706237, use),))
        step = holder(NOW - 3600, measured=records.MeasuredMemory(use.bytes, anonymous=use, anonymous_source=source))
        queue = snapshot([waiter(NOW - 3600)], (step, compiler_service(source=COMPILER_SCOPE)))
        stall = head_stall.find_stall(queue, 0.0, NOW, LIMIT)
        assert isinstance(stall, head_stall.HeadStall)
        self.assertEqual(head_stall.builds_text(stall).splitlines()[3],
                         "- check in devtools-chooserux (pid 706237): promise 12.0 GiB, uses 0.1 GiB")

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

    def test_unreadable_child_scope_makes_parent_use_unknown(self) -> None:
        parent = replace(holder(NOW - 3600, measured=records.MeasuredMemory(
            8 * GIB, anonymous=records.AnonymousRunUse(8 * GIB),
            anonymous_source=records.ScopeSource(IDLE_SCOPE, frozenset({706237, 706238})))), folder="/build/parent")
        child = replace(holder(NOW - 3600, measured=records.MeasuredMemory(
            4 * GIB, anonymous=records.RunUseUnavailable("child scope memory unavailable"),
            anonymous_source=records.ScopeSource(IDLE_SCOPE / "child.scope", frozenset({706238})))),
            folder="/build/child", pid=706238)
        queue = snapshot([waiter(NOW - 3600)], (parent, child, compiler_service(source=COMPILER_SCOPE)))
        stall = head_stall.find_stall(queue, 0.0, NOW, LIMIT)
        assert isinstance(stall, head_stall.HeadStall)
        self.assertEqual(head_stall.builds_text(stall).splitlines()[3:5], [
            "- check in parent (pid 706237): promise 12.0 GiB, uses unknown",
            "- check in child (pid 706238): promise 12.0 GiB, uses unknown",
        ])


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
        return replace(found, key=key)

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

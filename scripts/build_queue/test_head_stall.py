"""Check the once-per-stall alert for a queue head behind idle holders."""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from typing import cast, override
import unittest
from unittest import mock

from scripts.build_queue import admission_records as records, head_stall
from scripts.build_queue.queue_fixture import fixture_reservation
from scripts.build_queue.queue_snapshot import QueueSnapshot, write_queue_snapshot
from scripts.buildlog import cli, mac_idle, sample
from scripts.lint.memory_test_support import admission_policy_fixture, config_environment

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
    def test_a_test_waiting_for_the_land_cannot_cause_a_stall_alert(self) -> None:
        protected = waiter(NOW - 3600)
        queue = snapshot([protected], (holder(NOW - 3600),))
        inputs = replace(queue.inputs, landing_test_waits=frozenset({records.waiter_owner(protected)}))
        self.assertEqual(head_stall.find_stall(replace(queue, inputs=inputs), 0.0, NOW, LIMIT), head_stall.NoStall())

    def test_holder_time_starts_at_admission(self) -> None:
        queue = snapshot([waiter(NOW - 3600)], (holder(NOW - 62 * 60),))
        stall = head_stall.find_stall(queue, NOW - 3600, NOW, LIMIT)
        assert isinstance(stall, head_stall.HeadStall)
        self.assertIn("held for 62 min", head_stall.builds_text(stall))

    def test_head_behind_an_idle_holder_with_no_start_is_a_stall(self) -> None:
        stall = head_stall.find_stall(STALLED, NOW - 2 * 3600, NOW, LIMIT)
        assert isinstance(stall, head_stall.HeadStall)
        self.assertEqual((stall.last_start, stall.need, round(stall.waited), round(stall.quiet)),
                         (NOW - 2 * 3600, 15 * GIB, 3 * 3600, 2 * 3600))
        self.assertEqual(stall.holders, (head_stall.Holder("check", "/home/u/rust/devtools-chooserux", 706237,
                                                         12 * GIB, GIB // 10, 3 * 3600.0),))
        self.assertEqual(head_stall.builds_text(stall).splitlines(), [
            "The build queue head has waited 180 min. No step started for 120 min.",
            "Head: nextest in lfo-trigger-fires-bc6b76 (pid 1618939) needs 15.0 GiB.",
            "Holders:",
            "- check in devtools-chooserux (pid 706237): promise 12.0 GiB, uses 0.1 GiB, held for 180 min",
            "The head starts only when a holder ends. Check each holder that uses much less than its promise."])

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
        second = replace(holder(NOW - 45 * 60), step="nextest", folder="/build/second", pid=1090772,
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
                        f"- nextest in first (pid 4094003): promise 12.5 GiB, uses {amounts[0]}, held for 60 min",
                        f"- nextest in second (pid 1090772): promise 13.9 GiB, uses {amounts[1]}, held for 45 min",
                    ])

    def test_process_tree_holder_with_no_compiler_jobs_has_no_warning(self) -> None:
        use = records.AnonymousRunUse(GIB // 10)
        source = records.ProcessSource((records.ProcessAnonymousUse(706237, use),))
        step = holder(NOW - 3600, measured=records.MeasuredMemory(use.bytes, anonymous=use, anonymous_source=source))
        queue = snapshot([waiter(NOW - 3600)], (step, compiler_service(source=COMPILER_SCOPE)))
        stall = head_stall.find_stall(queue, 0.0, NOW, LIMIT)
        assert isinstance(stall, head_stall.HeadStall)
        self.assertEqual(head_stall.builds_text(stall).splitlines()[3],
                         "- check in devtools-chooserux (pid 706237): promise 12.0 GiB, uses 0.1 GiB, held for 60 min")

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
            "- check in parent (pid 706237): promise 12.0 GiB, uses unknown, held for 60 min",
            "- check in child (pid 706238): promise 12.0 GiB, uses unknown, held for 60 min",
        ])

    def test_a_holder_without_a_start_time_shows_unknown_duration(self) -> None:
        queue = snapshot([waiter(NOW - 3600)], (holder(0.0),))
        stall = head_stall.find_stall(queue, NOW - 3600, NOW, LIMIT)
        assert isinstance(stall, head_stall.HeadStall)
        self.assertIn("held for unknown", head_stall.builds_text(stall))


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
    sent: list[tuple[list[str], str]] = []

    @override
    def setUp(self) -> None:
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.paths = head_stall.StallPaths(Path(folder) / "admission", Path(folder) / "head_stall.json")
        self.paths.admission.mkdir()
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, config_environment(Path(folder) / "config"))))
        _ = self.enterContext(mock.patch.object(head_stall, "configured_limits", return_value=(LIMIT, 120.0)))
        self.sent = []

    def run_send(self, result: bool) -> Callable[[list[str], str], bool]:
        def run(command: list[str], text: str) -> bool:
            self.sent.append((command, text))
            return result
        return run

    def admit_at(self, at: float) -> None:
        _ = (self.paths.admission / "queue_times.jsonl").write_text(
            json.dumps({"kind": "admitted", "admitted_at": at, "need": GIB}, separators=(",", ":")) + "\n")

    def observe(self, queue: QueueSnapshot, now: float, *, accept: bool = True) -> head_stall.HeadStall | head_stall.NoStall:
        write_queue_snapshot(self.paths.admission, replace(queue.inputs, now=now), queue.policy)
        return head_stall.watch_once(self.paths, now, self.run_send(accept))

    def mock_send_outcomes(self, *outcomes: tuple[int, str] | RuntimeError) -> None:
        replies = iter(outcomes)

        def reply(command: list[str], *, input: str, **_options: object) -> subprocess.CompletedProcess[str]:
            self.sent.append((command, input))
            outcome = next(replies)
            if isinstance(outcome, RuntimeError):
                raise outcome
            status, header = outcome
            return subprocess.CompletedProcess(command, status, f"{header}: fixture\n", "")

        _ = self.enterContext(mock.patch.object(head_stall, "subprocess",
            SimpleNamespace(run=reply, SubprocessError=subprocess.SubprocessError)))

    def sent_recipients(self) -> list[str]:
        return [command[command.index("--to") + 1] for command, _ in self.sent]

    def test_only_the_failed_recipient_retries(self) -> None:
        self.mock_send_outcomes((0, "SENT"), (3, "FAILED"), (0, "SENT"))
        write_queue_snapshot(self.paths.admission, STALLED.inputs, STALLED.policy)
        _ = head_stall.watch_once(self.paths, NOW)
        _ = head_stall.watch_once(self.paths, NOW + 60)
        self.assertEqual(self.sent_recipients(), ["builds", "bob", "bob"])
        self.assertEqual(self.sent[1][0], self.sent[2][0])

    def test_a_queued_alert_does_not_repeat(self) -> None:
        self.mock_send_outcomes((1, "QUEUED"), (1, "QUEUED"), (0, "SENT"), (0, "SENT"))
        write_queue_snapshot(self.paths.admission, STALLED.inputs, STALLED.policy)
        _ = head_stall.watch_once(self.paths, NOW)
        _ = head_stall.watch_once(self.paths, NOW + 60)
        self.assertEqual(self.sent_recipients(), ["builds", "bob"])
        self.assertEqual([argv[argv.index("--key") + 1] for argv, _ in self.sent],
                         [f"head-stall-{NOW - 3 * 3600}"] * 2)

    def test_a_stop_between_recipients_does_not_resend_to_builds(self) -> None:
        self.mock_send_outcomes((0, "SENT"), RuntimeError("watch stopped"), (0, "SENT"), (0, "SENT"))
        write_queue_snapshot(self.paths.admission, STALLED.inputs, STALLED.policy)
        with self.assertRaisesRegex(RuntimeError, "watch stopped"):
            _ = head_stall.watch_once(self.paths, NOW)
        self.assertEqual(json.loads(self.paths.state.read_text())["alerted"], {"builds": NOW - 3 * 3600})
        _ = head_stall.watch_once(self.paths, NOW + 60)
        self.assertEqual(self.sent_recipients(), ["builds", "bob", "bob"])

    def test_pending_and_retry_outcomes_retry_until_acceptance(self) -> None:
        self.mock_send_outcomes((5, "PENDING"), (6, "RETRY"), (0, "KEPT"), (0, "SKIPPED"))
        write_queue_snapshot(self.paths.admission, STALLED.inputs, STALLED.policy)
        _ = head_stall.watch_once(self.paths, NOW)
        self.assertFalse(self.paths.state.exists())
        _ = head_stall.watch_once(self.paths, NOW + 60)
        _ = head_stall.watch_once(self.paths, NOW + 120)
        self.assertEqual(self.sent_recipients(), ["builds", "bob", "builds", "bob"])

    def test_four_heads_in_one_stall_alert_builds_and_bob_once(self) -> None:
        self.admit_at(NOW - 32 * 60)
        for index, minutes in enumerate((0, 18, 19, 32)):
            now = NOW + minutes * 60
            head = waiter(NOW - 2 * 3600)
            head.update(pid=head["pid"] + index, start_time=str(100 + index))
            queue = snapshot([head], STALLED.inputs.running)
            stall = self.observe(queue, now)
            self.assertIsInstance(stall, head_stall.HeadStall)

        self.assertEqual([command[command.index("--to") + 1] for command, _ in self.sent], ["builds", "bob"])

    def test_one_alert_per_stall_and_a_new_stall_alerts_again(self) -> None:
        _ = self.observe(STALLED, NOW)
        self.assertEqual(len(self.sent), 2)
        _ = self.observe(STALLED, NOW + 60)
        self.assertEqual(len(self.sent), 2)
        admission = NOW + 120
        self.admit_at(admission)
        self.assertEqual(self.observe(STALLED, admission), head_stall.NoStall())
        self.assertEqual(len(self.sent), 2)
        later = self.observe(STALLED, admission + LIMIT)
        assert isinstance(later, head_stall.HeadStall)
        self.assertEqual(len(self.sent), 4)
        (builds, builds_message), (bob, bob_message) = self.sent[2:]
        recipient = builds.index("--to") + 1
        self.assertEqual(builds[recipient], "builds")
        self.assertEqual(bob, [*builds[:recipient], "bob", *builds[recipient + 1:]])
        self.assertEqual(builds_message, head_stall.builds_text(later))
        self.assertEqual(bob_message, builds_message)
        self.assertEqual(builds[builds.index("--key") + 1], f"head-stall-{admission}")

    def test_failed_sends_retry_on_the_next_minute(self) -> None:
        last_start = NOW - 3600 + 0.25
        self.admit_at(last_start)
        _ = self.observe(STALLED, NOW, accept=False)
        self.assertFalse(self.paths.state.exists())
        _ = self.observe(STALLED, NOW + 60)
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(json.loads(self.paths.state.read_text())["alerted"], {"builds": last_start, "bob": last_start})

    def test_a_snapshot_gap_and_an_older_holder_do_not_repeat_an_alert(self) -> None:
        later_holder = replace(holder(NOW - 3600), pid=706238)
        queue = snapshot([waiter(NOW - 3600)], (*STALLED.inputs.running, later_holder))
        self.assertIsInstance(self.observe(queue, NOW), head_stall.HeadStall)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(head_stall.watch_once(self.paths, NOW + 180, self.run_send(True)), head_stall.NoStall())
        self.assertIsInstance(self.observe(STALLED, NOW + 240), head_stall.HeadStall)
        self.assertEqual(len(self.sent), 2)

    def test_the_previous_state_format_suppresses_an_alert_for_the_same_stall(self) -> None:
        last_start = NOW - 3600 + 0.25
        self.admit_at(last_start)
        for saved in (f"1618939:19509458:{int(last_start)}", last_start):
            with self.subTest(saved=saved):
                _ = self.paths.state.write_text(json.dumps({"alerted": saved, "at": NOW}))
                self.assertIsInstance(self.observe(STALLED, NOW + 60), head_stall.HeadStall)
                self.assertEqual(self.sent_recipients(), ["bob"])
                self.sent.clear()
                self.assertIsInstance(self.observe(STALLED, NOW + 120), head_stall.HeadStall)
                self.assertEqual(self.sent, [])

    def test_a_legacy_delivery_record_still_allows_bobs_first_alert(self) -> None:
        last_start = NOW - 3600 + 0.25
        self.admit_at(last_start)
        _ = self.paths.state.write_text(json.dumps({"alerted": last_start, "at": NOW}))
        self.assertIsInstance(self.observe(STALLED, NOW), head_stall.HeadStall)
        self.assertEqual(self.sent_recipients(), ["bob"])
        self.assertEqual(json.loads(self.paths.state.read_text())["alerted"], {"builds": last_start, "bob": last_start})
        self.assertIsInstance(self.observe(STALLED, NOW + 60), head_stall.HeadStall)
        self.assertEqual(self.sent_recipients(), ["bob"])

    def test_state_write_failures_do_not_stop_sends_and_report_once(self) -> None:
        diagnostics = StringIO()
        self.mock_send_outcomes((0, "SENT"), (0, "SENT"))
        write_queue_snapshot(self.paths.admission, STALLED.inputs, STALLED.policy)
        attempts: list[list[str]] = []

        def fail_write(_path: Path, _text: str) -> None:
            self.assertEqual(diagnostics.getvalue(), "")
            attempts.append(self.sent_recipients())
            raise OSError("fixture cannot write alert state")

        with (mock.patch.object(sample, "sample", return_value=sample.SampleTaken(NOW, "natedev")),
              mock.patch.object(mac_idle, "watch_once"),
              mock.patch.object(mac_idle, "watch_paths"),
              mock.patch.object(head_stall, "default_paths", return_value=self.paths),
              mock.patch.object(head_stall, "write_text_atomic", side_effect=fail_write),
              redirect_stderr(diagnostics)):
            self.assertEqual(cli.main(["sample"]), 0)
        self.assertEqual(attempts, [["builds"], ["builds", "bob"]])
        self.assertEqual(diagnostics.getvalue().splitlines(),
                         ["buildlog sample: head-stall check failed: fixture cannot write alert state"])


if __name__ == "__main__":
    _ = unittest.main()

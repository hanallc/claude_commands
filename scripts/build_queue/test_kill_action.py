"""Use private process trees and message delivery for Builds stop actions."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
from typing import cast, override
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from scripts.agents import detached_runs
from scripts.build_queue import admission_client, admission_records as records, admission_wire, kill_action
from scripts.build_queue.admission_jobs import VerifyCall
from scripts.build_queue.test_memory_feed import reservation_reading
from scripts.buildlog import store
from scripts.lib import process_start
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import send_budget
from scripts.production import staff


FIXTURE_CARGO = """import os, signal, subprocess, sys

child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])

def stop(_number, _frame):
    child.terminate()
    child.wait()
    sys.exit(143)

signal.signal(signal.SIGTERM, stop)
print(os.getpid(), child.pid, flush=True)
signal.pause()
"""

FIXTURE_SUPERVISOR = """import json, os, signal, subprocess, sys
from pathlib import Path
from scripts.agents.detached_runs import record_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Started, process_start

path = Path(sys.argv[2])
command = [sys.executable, sys.argv[3]]
process = subprocess.Popen(command, start_new_session=True, stdout=subprocess.PIPE, text=True,
                           env={**os.environ, "UNIT_DETACHED_RECORD": str(path)})

def stop(_number, _frame):
    try:
        os.kill(process.pid, signal.SIGTERM)
        os.waitpid(process.pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass
    sys.exit(143)

signal.signal(signal.SIGTERM, stop)
started = process_start(process.pid)
supervisor = process_start(os.getpid())
assert isinstance(started, Started) and isinstance(supervisor, Started)
record = {
    "run_id": "fixture", "status": "running", "session_dir": str(path.parent.parent),
    "worker": "impl3", "staff_id": "worker-id", "label": "fixture", "command": command,
    "log": str(path.with_suffix(".log")), "started_at": 10, "pid": os.getpid(),
    "command_pid": process.pid, "command_group": process.pid, "command_started": started.start,
    "supervisor_started": supervisor.start, "director_dir": "", "done_message_file": "",
    "command_scope": {"unit": "detached-fixture.scope",
                      "path": "/user.slice/builds-detached.slice/detached-fixture.scope",
                      "device": 1, "inode": 2},
}
with record_lock(path):
    write_text_atomic(path, json.dumps(record))
print(process.stdout.readline().strip(), flush=True)
process.wait()
with record_lock(path):
    record = json.loads(path.read_text())
    record["status"] = "finished"
    write_text_atomic(path, json.dumps(record))
"""


class KillActionTests(unittest.TestCase):
    root: Path = Path()
    cargo: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = write_config_fixture(self.root / "review_links.conf")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.root)})))
        self.cargo = self.root / "cargo"
        _ = self.cargo.write_text(FIXTURE_CARGO)

    @contextmanager
    def process(self, *arguments: str, executable: str = sys.executable) -> Generator[subprocess.Popen[str]]:
        with subprocess.Popen([executable, *arguments], stdout=subprocess.PIPE, text=True) as child:
            try:
                yield child
            finally:
                if child.poll() is None:
                    child.terminate()
                    _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    def line(self, child: subprocess.Popen[str]) -> str:
        output = child.stdout
        assert output is not None
        self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0])
        return cast(str, output.readline()).strip()

    def started(self, pid: int) -> str:
        start = process_start.proc_table_start(pid, Path("/proc"))
        assert isinstance(start, process_start.Started)
        return start.start

    def test_only_local_running_cargo_is_a_candidate(self) -> None:
        reading = reservation_reading()
        (step,) = reading.view.running
        (saved,) = reading.explanation.decided_from.reservations
        reservation: records.Reservation = {**saved, "pid": 17, "start_time": "123"}
        step = replace(step, pid=17, session_id="session")
        verified = replace(step, step="verify step", verify=VerifyCall("check", "fixture", 1, 1))
        self.assertIsInstance(kill_action.candidate(step, reservation, "natedev"), kill_action.KillCandidate)
        cases = ((verified, "natedev", False), (step, "mac", False), (step, "aws", False), (step, "natedev", True),
                 (replace(step, ci_runner="runner"), "natedev", False),
                 (replace(step, session_id=""), "natedev", False),
                 (replace(step, assignment_host="mac"), "natedev", False),
                 (replace(step, mac_placement="mac-attempt"), "natedev", False),
                 (replace(step, pid=18), "natedev", False),
                 (replace(step, step="test-affected"), "natedev", False),
                 (replace(step, step="merge-gate"), "natedev", False),
                 (replace(step, measured=records.UnownedSliceMemory(1, pid=17)), "natedev", False),
                 (replace(step, measured=records.CompilerServiceMemory(1, pid=17)), "natedev", False))
        for current, machine, ci in cases:
            with self.subTest(current=current, machine=machine, ci=ci):
                self.assertIsInstance(kill_action.candidate(current, reservation, machine, ci=ci),
                                      kill_action.NoKillCandidate)

    def test_cargo_stops_through_the_exact_scope_and_uses_term(self) -> None:
        scope = self.root / "builds.slice" / "run-fixture.scope"
        scope.mkdir(parents=True)
        anchor = ("fixture lifetime", 42)
        stop = Mock()
        api = Mock(**{"ProcessLifetime.return_value": anchor, "pin_process.return_value": nullcontext(5),
                      "ScopeUnit.return_value": "fixture scope", "stop_command_tree": stop})
        identity = scope.stat()
        route = kill_action.ProgramScope(scope, identity.st_dev, identity.st_ino, 42, "123")
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        with (patch.object(kill_action, "process_stop_api", return_value=api),
              patch.object(kill_action, "anchor_in_scope", return_value=True),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "program_scope", return_value=route)):
            self.assertFalse(kill_action.stop_program_scope(replace(route, inode=identity.st_ino + 1), run))
            stop.assert_not_called()
            self.assertTrue(kill_action.stop_program_scope(route, run))
        stop.assert_called_once_with(anchor, "fixture scope", signal.SIGTERM)
        events = scope / "cgroup.events"
        _ = events.write_text("populated 1\n")
        self.assertFalse(kill_action.scope_empty(route))
        _ = events.write_text("populated 0\n")
        self.assertTrue(kill_action.scope_empty(route))

    def test_a_cargo_child_outside_the_scope_makes_kill_unavailable(self) -> None:
        scope = self.root / "builds.slice" / "run-fixture.scope"
        scope.mkdir(parents=True)
        sidecar = self.root / "scope"
        _ = sidecar.write_text(str(scope))
        members = scope / "cgroup.procs"
        _ = members.write_text("18\n19\n")
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", str(sidecar))
        processes = {17: memory_admit.ProcessIdentity(1, "123"), 18: memory_admit.ProcessIdentity(17, "124"),
                     19: memory_admit.ProcessIdentity(18, "125")}
        caller = staff.StaffId("fixture")

        def program(pid: int, _processes: dict[int, memory_admit.ProcessIdentity]) -> str:
            return "cargo" if pid == 18 else "helper"

        with (patch.object(kill_action, "process_stop_api", return_value=Mock()),
              patch.object(memory_admit, "builds_slice_cgroup", return_value=scope.parent),
              patch.object(kill_action, "program", new=program),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "anchor_in_scope", return_value=True),
              patch.object(kill_action, "detached_command") as detached):
            self.assertIsInstance(kill_action.stop_route(run, caller, processes), kill_action.ProgramScope)
            _ = members.write_text("18\n")
            self.assertIsInstance(kill_action.stop_route(run, caller, processes), kill_action.NoStopRoute)
        detached.assert_not_called()

    def test_routes_are_unavailable_without_the_shared_helper(self) -> None:
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        with (patch.object(kill_action, "process_stop_api", return_value=kill_action.NoStopRoute()),
              patch.object(kill_action, "program_scope") as scope,
              patch.object(kill_action, "detached_command") as detached):
            self.assertIsInstance(kill_action.stop_route(run, staff.StaffId("fixture"), {}), kill_action.NoStopRoute)
        scope.assert_not_called()
        detached.assert_not_called()

    def test_detached_stop_uses_the_supervisor_and_confirms_the_result(self) -> None:
        supervisor = self.root / "detached_runs.py"
        _ = supervisor.write_text(FIXTURE_SUPERVISOR)
        path = self.root / "detached_runs" / "fixture.json"
        path.parent.mkdir()
        with self.process(str(supervisor), "supervise", str(path), str(self.cargo)) as command:
            pid, child = map(int, self.line(command).split())
            started = self.started(pid)
            run = kill_action.KillCandidate(pid, started, pid, started, "session", "check", "at", "")
            def stable_stop(record: Path, reason: str, expected: detached_runs.CommandGroup) -> bool:
                self.assertEqual((record, expected), (path, detached_runs.CommandGroup(pid, started)))
                from scripts.agents import worker_lifecycle
                from scripts.lib.atomic_file import write_text_atomic
                import json

                with detached_runs.record_lock(record):
                    data = worker_lifecycle.read_json_object(record)
                    write_text_atomic(record, json.dumps({**data, "stop_reason": reason}))
                descriptor = os.pidfd_open(pid)
                try:
                    self.assertEqual(started, self.started(pid))
                    signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                finally:
                    os.close(descriptor)
                return True

            with (patch.object(detached_runs, "stop_verified_command", new=stable_stop, create=True),
                  patch.object(kill_action, "process_stop_api", return_value=Mock())):
                route = kill_action.stop_route(run, staff.StaffId("worker-id"), memory_admit.process_table())
                scope = kill_action.RunScope(Path("/sys/fs/cgroup/user.slice/builds-detached.slice/detached-fixture.scope"), 1, 2)
                self.assertEqual(route, kill_action.DetachedCommand(path, pid, started, command.pid,
                                                                   self.started(command.pid), scope))
                assert isinstance(route, kill_action.DetachedCommand)
                self.assertTrue(kill_action.stop_detached(route, run, "fixture stop"))
            self.assertEqual(command.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertIsInstance(process_start.proc_table_start(child, Path("/proc")), process_start.Gone)
            self.assertIsNone(detached_runs.command_group(path))

    def test_legacy_verify_rows_do_not_offer_a_step_scope(self) -> None:
        run = kill_action.KillCandidate(17, "123", 16, "122", "session", "check", "at", "")
        table = {17: memory_admit.ProcessIdentity(16, "123"), 16: memory_admit.ProcessIdentity(1, "122")}
        with (patch.object(kill_action, "process_stop_api", return_value=Mock()),
              patch.object(kill_action, "program", side_effect=("cargo", "verify.sh")),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "program_scope") as scope):
            self.assertIsInstance(kill_action.stop_route(run, staff.StaffId("fixture"), table), kill_action.NoStopRoute)
        scope.assert_not_called()

    def test_the_service_uses_the_recorded_session_for_each_staff_kind(self) -> None:
        reading = reservation_reading()
        scope = kill_action.ProgramScope(self.root / "run-fixture.scope", 1, 2, 17, "123")
        kinds: tuple[staff.Kind, ...] = ("worker", "unit", "showrunner", "producer")
        for kind in kinds:
            with self.subTest(kind=kind):
                caller = staff.StaffRow(staff.StaffId("stable-id"), kind, "current name", staff.StaffRoot(),
                                        current_session=staff.StaffId("session"))
                decoy = replace(caller, id=staff.StaffId("decoy"), name="fixture",
                                current_session=staff.StaffId("other session"))
                snapshot = staff.StaffSnapshot((decoy, caller))
                with (patch.object(store, "host_name", return_value="natedev"),
                      patch.object(admission_client, "view", return_value=reading),
                      patch.object(staff, "snapshot", return_value=snapshot),
                      patch.object(memory_admit, "process_table", return_value={}),
                      patch.object(kill_action, "stop_route", return_value=scope)):
                    (target,) = kill_action.live_targets().values()
                self.assertEqual(target.recipient, caller.id)
                self.assertEqual(target.starter, f"current name, a {'unit director' if kind == 'unit' else kind}")

    def test_handle_rejects_pid_reuse_and_sends_once_after_confirmed_stop(self) -> None:
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "nextest", "at", "")
        route = kill_action.ProgramScope(self.root / "run-fixture.scope", 1, 2, 17, "123")
        recipient = staff.StaffId("stable-worker-id")
        target = kill_action.KillTarget(run, route, recipient, "impl3, a worker", "impl3", "worker")
        actions = kill_action.KillActions(self.root)
        at = datetime(2026, 11, 18, 19, 41, tzinfo=ZoneInfo("America/New_York"))
        reason = 'nate killed your run "nextest" (pid 17) from the Builds page at 19:41 EST'
        class FixtureClock:
            @staticmethod
            def now(_zone: object) -> datetime:
                return at

        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}) as targets,
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "datetime", FixtureClock),
              patch.object(kill_action, "user_zone", return_value=at.tzinfo),
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as deliver):
            offers, _ = actions.options()
            self.assertEqual(offers[run.reference]["message"], reason)
            handle = offers[run.reference]["handle"]
            reused = replace(run, holder_start="456", row_start="456")
            targets.return_value = {reused.reference: replace(target, candidate=reused)}
            self.assertIsInstance(actions.stop(run.reference, handle, ""), kill_action.NoStopRoute)
            targets.return_value = {run.reference: target}
            with (patch.object(kill_action, "stop_program_scope", return_value=True),
                  patch.object(kill_action, "stop_complete") as observe):
                def confirm(_action: kill_action.SavedKillAction) -> bool:
                    deliver.assert_not_called()
                    self.assertEqual(actions.options()[1][0]["stop"], kill_action.StopState.STOPPING)
                    targets.return_value = {}
                    return True

                observe.side_effect = confirm
                result = actions.stop(run.reference, handle, "Use the next slot.")
                deliver.assert_called_once()
                (saved,) = cast(tuple[kill_action.SavedKillAction], deliver.call_args.args)
                self.assertEqual((saved.recipient, saved.message), (recipient, reason + "\nUse the next slot."))
            self.assertEqual(result, {"reference": run.reference, "step": "nextest", "pid": 17,
                                      "stop": kill_action.StopState.STOPPED, "delivery": kill_action.DeliveryState.SENT,
                                      "time": "19:41 EST", "recipient_name": "impl3", "recipient_role": "worker"})
            self.assertEqual(actions.options(), ({}, [result]))
            self.assertIsInstance(actions.stop(run.reference, handle, "Again."), kill_action.NoStopRoute)

    def test_restart_resumes_a_late_stop_without_losing_the_notice(self) -> None:
        _ = write_config_fixture(self.root / "review_links.conf", "kill_stop_seconds=0.5\nkill_pending_check_seconds=0.1")
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        scope = self.root / "run-fixture.scope"
        scope.mkdir()
        identity = scope.stat()
        route = kill_action.ProgramScope(scope, identity.st_dev, identity.st_ino, 17, "123")
        target = kill_action.KillTarget(run, route, staff.StaffId("producer"), "founder, a producer", "founder", "producer")
        events = scope / "cgroup.events"
        _ = events.write_text("populated 1\n")
        first = kill_action.KillActions(self.root)
        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "stop_program_scope", return_value=True) as stop,
              patch.object(first, "_start_watch") as watch,
              patch.object(kill_action, "deliver") as send):
            response = first.stop(run.reference, first.handle(target), "Use the next slot.")
        assert not isinstance(response, kill_action.NoStopRoute)
        self.assertEqual(response["stop"], kill_action.StopState.UNCONFIRMED)
        stop.assert_called_once_with(route, run)
        watch.assert_called_once()
        send.assert_not_called()
        record = self.root / ".review-link-build-kill" / f"{run.reference}.json"
        self.assertIn("Use the next slot.", record.read_text())
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)

        _ = events.write_text("populated 0\n")
        resumed = kill_action.KillActions(self.root)
        workers: list[threading.Thread] = []
        start = threading.Thread.start

        def record_thread(thread: threading.Thread) -> None:
            workers.append(thread)
            start(thread)

        with (patch.object(threading.Thread, "start", new=record_thread),
              patch.object(kill_action, "stop_program_scope") as repeated_stop,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.QUEUED) as send,
              patch.object(kill_action, "live_targets", return_value={})):
            resumed.resume()
            self.assertEqual(len(workers), 1)
            workers[0].join(TEST_HANG_GUARD_SECS)
            self.assertFalse(workers[0].is_alive())
            self.assertEqual(resumed.options()[1][0]["stop"], kill_action.StopState.STOPPED)
            self.assertEqual(resumed.options()[1][0]["delivery"], kill_action.DeliveryState.QUEUED)
        repeated_stop.assert_not_called()
        send.assert_called_once()
        (saved,) = cast(tuple[kill_action.SavedKillAction], send.call_args.args)
        self.assertEqual(saved.recipient, target.recipient)
        self.assertEqual(saved.message,
            f'nate killed your run "check" (pid 17) from the Builds page at {response["time"]}\nUse the next slot.')

    def test_restart_before_term_never_reports_a_natural_exit_as_a_kill(self) -> None:
        scope = self.root / "run-recorded.scope"
        scope.mkdir()
        events = scope / "cgroup.events"
        _ = events.write_text("populated 1\n")
        identity = scope.stat()
        route = kill_action.ProgramScope(scope, identity.st_dev, identity.st_ino, 20, "125")
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        recipient = staff.StaffId("worker-id")
        target = kill_action.KillTarget(run, route, recipient, "impl3, a worker", "impl3", "worker")
        actions = kill_action.KillActions(self.root)
        path = self.root / ".review-link-build-kill" / f"{run.reference}.json"

        class FixtureRestart(BaseException):
            pass

        def exit_before_term(*_arguments: object) -> bool:
            saved = admission_wire.parse_record(path.read_text(), kill_action.SavedKillAction)
            self.assertIsInstance(saved.stage, kill_action.StopRequested)
            self.assertEqual(saved.result["stop"], kill_action.StopState.STOPPING)
            self.assertFalse(kill_action.scope_empty(route))
            raise FixtureRestart

        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "stop_program_scope", new=exit_before_term),
              self.assertRaises(FixtureRestart)):
            _ = actions.stop(run.reference, actions.handle(target), "Use the next slot.")

        _ = events.write_text("populated 0\n")
        resumed = kill_action.KillActions(self.root)
        workers: list[threading.Thread] = []
        start = threading.Thread.start

        def record_thread(thread: threading.Thread) -> None:
            workers.append(thread)
            start(thread)

        with (patch.object(kill_action, "live_targets", return_value={}),
              patch.object(kill_action, "stop_program_scope") as repeated_stop,
              patch.object(kill_action, "deliver") as send,
              patch.object(threading.Thread, "start", new=record_thread)):
            resumed.resume()
            (worker,) = workers
            worker.join(TEST_HANG_GUARD_SECS)
            self.assertFalse(worker.is_alive())
            offers, (result,) = resumed.options()
            self.assertEqual(offers, {})
        self.assertEqual(result["stop"], kill_action.StopState.NOT_SENT)
        repeated_stop.assert_not_called()
        send.assert_not_called()

    def test_detached_restart_waits_for_the_child_and_handles_retirement_once(self) -> None:
        _ = write_config_fixture(self.root / "review_links.conf", "kill_stop_seconds=0.5\nkill_pending_check_seconds=0.1")
        folder = self.root / "detached-recorded.scope"
        child = folder / "child"
        child.mkdir(parents=True)
        _ = (folder / "cgroup.procs").write_text("")
        _ = (child / "cgroup.procs").write_text("20\n")
        events = folder / "cgroup.events"
        _ = events.write_text("populated 1\n")
        identity = folder.stat()
        scope = kill_action.RunScope(folder, identity.st_dev, identity.st_ino)
        route = kill_action.DetachedCommand(self.root / "pruned-detached-record.json", 18, "123", 19, "122", scope)
        run = kill_action.KillCandidate(18, "123", 18, "123", "session", "check", "at", "")
        target = kill_action.KillTarget(run, route, staff.StaffId("retired"), "founder, a producer", "founder", "producer")
        actions = kill_action.KillActions(self.root)
        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "stop_detached", return_value=True),
              patch.object(actions, "_start_watch"),
              patch.object(kill_action, "deliver") as send):
            response = actions.stop(run.reference, actions.handle(target), "Use the next slot.")
        assert not isinstance(response, kill_action.NoStopRoute)
        self.assertEqual(response["stop"], kill_action.StopState.UNCONFIRMED)
        send.assert_not_called()
        path = self.root / ".review-link-build-kill" / f"{run.reference}.json"
        saved = admission_wire.parse_record(path.read_text(), kill_action.SavedKillAction)
        self.assertIsInstance(saved.stage, kill_action.TermSent)
        self.assertFalse(route.record.exists())

        observed = threading.Event()
        workers: list[threading.Thread] = []
        start = threading.Thread.start
        read_scope = kill_action.scope_empty

        def record_thread(thread: threading.Thread) -> None:
            workers.append(thread)
            start(thread)

        def read(route: kill_action.RunScope, *, require_events: bool = False) -> bool:
            empty = read_scope(route, require_events=require_events)
            observed.set()
            return empty

        response_text = "send.py: staff:retired: founder is a retired producer"
        refusal = subprocess.CompletedProcess(["send.py"], 2, "", response_text)
        resumed = kill_action.KillActions(self.root)
        with (patch.object(kill_action, "scope_empty", new=read),
              patch.object(kill_action, "live_targets", return_value={}),
              patch.object(kill_action, "stop_detached") as repeated_stop,
              patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", return_value=refusal) as send,
              patch.object(threading.Thread, "start", new=record_thread)):
            resumed.resume()
            try:
                self.assertTrue(observed.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(read_scope(scope, require_events=True))
                self.assertEqual(resumed.options()[1][0]["stop"], kill_action.StopState.UNCONFIRMED)
                send.assert_not_called()
            finally:
                _ = (child / "cgroup.procs").write_text("")
                _ = events.write_text("populated 0\n")
                for worker in workers:
                    worker.join(TEST_HANG_GUARD_SECS)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            recovered = kill_action.KillActions(self.root)
            recovered.resume()
            self.assertEqual(len(workers), 1)
            (_, (result,)) = recovered.options()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.FAILED))
        repeated_stop.assert_not_called()
        send.assert_called_once()

    def test_recovery_retries_an_unconfirmed_send_with_the_same_key(self) -> None:
        scope = self.root / "run-recorded.scope"
        scope.mkdir()
        _ = (scope / "cgroup.events").write_text("populated 0\n")
        identity = scope.stat()
        route = kill_action.ProgramScope(scope, identity.st_dev, identity.st_ino, 20, "125")
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        target = kill_action.KillTarget(run, route, staff.StaffId("worker"), "impl3, a worker", "impl3", "worker")
        actions = kill_action.KillActions(self.root)
        attempts: list[list[str]] = []

        def send(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            attempts.append(argv)
            return (subprocess.CompletedProcess(argv, -signal.SIGTERM, "", "")
                    if len(attempts) == 1 else subprocess.CompletedProcess(argv, 0, "KEPT: review", ""))

        with (patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", new=send),
              patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "stop_program_scope", return_value=True),
              patch.object(actions, "_start_watch")):
            _ = actions.stop(run.reference, actions.handle(target), "Use the next slot.")
        path = self.root / ".review-link-build-kill" / f"{run.reference}.json"
        saved = admission_wire.parse_record(path.read_text(), kill_action.SavedKillAction)
        self.assertIsInstance(saved.stage, kill_action.SendingNotice)
        self.assertEqual(len(attempts), 1)

        workers: list[threading.Thread] = []
        start = threading.Thread.start

        def record_thread(thread: threading.Thread) -> None:
            workers.append(thread)
            start(thread)

        resumed = kill_action.KillActions(self.root)
        with (patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", new=send),
              patch.object(kill_action, "live_targets", return_value={}),
              patch.object(threading.Thread, "start", new=record_thread)):
            resumed.resume()
            (worker,) = workers
            worker.join(TEST_HANG_GUARD_SECS)
            self.assertFalse(worker.is_alive())
            (_, (result,)) = resumed.options()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.QUEUED))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[0][attempts[0].index("--key") + 1], f"builds-kill:{saved.id}")
        self.assertEqual(attempts[0][attempts[0].index("--text") + 1], saved.message)
        self.assertTrue(saved.message.endswith("\nUse the next slot."))

    def test_refused_stop_does_not_send_a_message(self) -> None:
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        route = kill_action.ProgramScope(self.root / "run-fixture.scope", 1, 2, 17, "123")
        target = kill_action.KillTarget(run, route, staff.StaffId("producer"), "founder, a producer", "founder", "producer")
        actions = kill_action.KillActions(self.root)
        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "stop_program_scope", return_value=False),
              patch.object(kill_action, "deliver") as deliver):
            outcome = actions.stop(run.reference, actions.handle(target), "")
        deliver.assert_not_called()
        assert not isinstance(outcome, kill_action.NoStopRoute)
        self.assertEqual((outcome["reference"], outcome["step"], outcome["pid"]), (run.reference, "check", 17))
        self.assertEqual((outcome["stop"], outcome["delivery"]),
                         (kill_action.StopState.NOT_SENT, kill_action.DeliveryState.PENDING))


if __name__ == "__main__":
    _ = unittest.main()

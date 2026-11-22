"""Use private process trees and message delivery for Builds stop actions."""
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime
import json
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
from unittest.mock import ANY, Mock, patch
from zoneinfo import ZoneInfo

from scripts.agents import detached_runs
from scripts.build_queue import admission_client, admission_records as records, admission_wire, kill_action
from scripts.build_queue.admission_jobs import VerifyCall
from scripts.build_queue.test_memory_feed import reservation_reading
from scripts.buildlog import store
from scripts.lib import process_start, process_stop
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import send_budget
from scripts.production import staff


def confirm_term(_target: kill_action.KillTarget, _reason: str, *, on_term: Callable[[int], None]) -> bool:
    on_term(1)
    return True


def unconfirmed_term(_target: kill_action.KillTarget, _reason: str, *, on_term: Callable[[int], None]) -> bool:
    on_term(1)
    return False


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

    def scope_identity(self, folder: Path) -> process_stop.ScopeIdentity:
        identity = folder.stat()
        return {"unit": folder.name, "path": "/" + str(folder.relative_to(self.root)),
                "device": identity.st_dev, "inode": identity.st_ino}

    def program_scope(self, pid: int = 17, start: str = "123") -> kill_action.ProgramScope:
        scope = self.root / "builds.slice" / "run-fixture.scope"
        scope.mkdir(parents=True)
        return kill_action.ProgramScope(self.scope_identity(scope), pid, start)

    def scope_has_ended(self, scope: process_stop.ScopeIdentity) -> bool:
        events = self.root / scope["path"].lstrip("/") / "cgroup.events"
        return "populated 0" in events.read_text().splitlines()

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

    def test_cargo_stop_rechecks_the_saved_scope(self) -> None:
        route = self.program_scope(42)
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        target = kill_action.KillTarget(run, route, staff.StaffId("worker-id"), "impl3", "impl3", "worker")
        _ = write_config_fixture(self.root / "shutdown.conf", "STOP_GRACE_SECONDS=2\nKILL_GRACE_SECONDS=5")
        on_term = Mock()
        with (patch.object(process_stop, "stop_scope") as stop,
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", return_value=route)):
            moved = replace(route, scope={**route.scope, "inode": route.scope["inode"] + 1})
            self.assertFalse(kill_action.stop_verified_scope(replace(target, route=moved), "Fixture reason",
                                                            on_term=on_term))
            stop.assert_not_called()
            self.assertTrue(kill_action.stop_verified_scope(target, "Fixture reason", on_term=on_term))
            stop.assert_called_once_with(route.scope, 2, 5, on_term=on_term)

    def test_detached_stop_uses_the_public_api_with_the_saved_route_check(self) -> None:
        folder = self.root / "builds-detached.slice" / "detached-fixture.scope"
        folder.mkdir(parents=True)
        route = kill_action.DetachedCommand(self.root / "fixture.json", 17, "123", 16, "122",
                                            self.scope_identity(folder))
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        target = kill_action.KillTarget(run, route, staff.StaffId("worker-id"), "impl3", "impl3", "worker")
        reason = "Stopped from the Builds page."
        moved = replace(route, scope={**route.scope, "inode": route.scope["inode"] + 1})
        counts: list[int] = []

        def stop_detached(_record: Path, _reason: str, *, check: Callable[[], bool],
                          on_term: Callable[[int], None]) -> bool:
            if not check():
                return False
            on_term(2)
            return True

        verified = Mock(side_effect=stop_detached)
        with (patch.object(process_stop, "stop_scope") as stop_scope,
              patch.object(detached_runs, "stop_command", verified),
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route") as read_route):
            read_route.side_effect = [route, route, route, moved]
            self.assertTrue(kill_action.stop_verified_scope(target, reason, on_term=counts.append))
            self.assertFalse(kill_action.stop_verified_scope(target, reason, on_term=counts.append))
        self.assertEqual([call.args for call in verified.call_args_list], [(route.record, reason)] * 2)
        self.assertEqual(counts, [2])
        stop_scope.assert_not_called()

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

        with (patch.object(process_stop, "pin_process", return_value=nullcontext(5)),
              patch.object(process_stop, "command_scope", return_value=self.scope_identity(scope)),
              patch.object(kill_action, "CGROUP_ROOT", self.root),
              patch.object(memory_admit, "builds_slice_cgroup", return_value=scope.parent),
              patch.object(kill_action, "program", new=program),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "detached_command") as detached):
            self.assertEqual(kill_action.stop_route(run, caller, processes),
                             kill_action.ProgramScope(self.scope_identity(scope), 18, "124"))
            _ = members.write_text("18\n")
            self.assertIsInstance(kill_action.stop_route(run, caller, processes), kill_action.NoStopRoute)
        detached.assert_not_called()

    def test_routes_are_unavailable_without_pidfd_support(self) -> None:
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        with (patch.object(process_stop, "supported", return_value=False),
              patch.object(kill_action, "program_scope") as scope,
              patch.object(kill_action, "detached_command") as detached):
            self.assertIsInstance(kill_action.stop_route(run, staff.StaffId("fixture"), {}), kill_action.NoStopRoute)
        scope.assert_not_called()
        detached.assert_not_called()

    def test_detached_route_uses_the_scope_saved_by_the_supervisor(self) -> None:
        supervisor = self.root / "detached_runs.py"
        _ = supervisor.write_text(FIXTURE_SUPERVISOR)
        path = self.root / "detached_runs" / "fixture.json"
        path.parent.mkdir()
        with self.process(str(supervisor), "supervise", str(path), str(self.cargo)) as command:
            pid, _child = map(int, self.line(command).split())
            started = self.started(pid)
            run = kill_action.KillCandidate(pid, started, pid, started, "session", "check", "at", "")
            scope: process_stop.ScopeIdentity = {"unit": "detached-fixture.scope", "device": 1, "inode": 2,
                "path": "/user.slice/builds-detached.slice/detached-fixture.scope"}
            with (patch.object(process_stop, "pin_process", return_value=nullcontext(5)),
                  patch.object(process_stop, "command_scope", return_value=scope)):
                route = kill_action.stop_route(run, staff.StaffId("worker-id"), memory_admit.process_table())
                self.assertEqual(route, kill_action.DetachedCommand(path, pid, started, command.pid,
                                                                   self.started(command.pid), scope))

    def test_legacy_verify_rows_do_not_offer_a_step_scope(self) -> None:
        run = kill_action.KillCandidate(17, "123", 16, "122", "session", "check", "at", "")
        table = {17: memory_admit.ProcessIdentity(16, "123"), 16: memory_admit.ProcessIdentity(1, "122")}
        with (patch.object(kill_action, "program", side_effect=("cargo", "verify.sh")),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "program_scope") as scope):
            self.assertIsInstance(kill_action.stop_route(run, staff.StaffId("fixture"), table), kill_action.NoStopRoute)
        scope.assert_not_called()

    def test_the_service_uses_the_recorded_session_for_each_staff_kind(self) -> None:
        reading = reservation_reading()
        scope = self.program_scope()
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
        route = self.program_scope()
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
            def confirm(_target: kill_action.KillTarget, _reason: str, *, on_term: Callable[[int], None]) -> bool:
                on_term(1)
                deliver.assert_not_called()
                self.assertEqual(actions.options()[1][0]["stop"], kill_action.StopState.STOPPING)
                targets.return_value = {}
                return True

            with patch.object(kill_action, "stop_verified_scope", new=confirm):
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
        _ = write_config_fixture(self.root / "review_links.conf", "kill_pending_check_seconds=0.1")
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        scope = self.root / "run-fixture.scope"
        scope.mkdir()
        route = kill_action.ProgramScope(self.scope_identity(scope), 17, "123")
        target = kill_action.KillTarget(run, route, staff.StaffId("producer"), "founder, a producer", "founder", "producer")
        events = scope / "cgroup.events"
        _ = events.write_text("populated 1\n")
        first = kill_action.KillActions(self.root)
        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "stop_verified_scope", side_effect=unconfirmed_term) as stop,
              patch.object(first, "_start_watch") as watch,
              patch.object(kill_action, "deliver") as send):
            response = first.stop(run.reference, first.handle(target), "Use the next slot.")
        assert not isinstance(response, kill_action.NoStopRoute)
        self.assertEqual(response["stop"], kill_action.StopState.UNCONFIRMED)
        reason = f'nate killed your run "check" (pid 17) from the Builds page at {response["time"]}'
        stop.assert_called_once_with(target, reason, on_term=ANY)
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
              patch.object(process_stop, "CGROUP_ROOT", self.root),
              patch.object(kill_action, "stop_verified_scope") as repeated_stop,
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
        route = kill_action.ProgramScope(self.scope_identity(scope), 20, "125")
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        recipient = staff.StaffId("worker-id")
        target = kill_action.KillTarget(run, route, recipient, "impl3, a worker", "impl3", "worker")
        actions = kill_action.KillActions(self.root)
        path = self.root / ".review-link-build-kill" / f"{run.reference}.json"

        class FixtureRestart(BaseException):
            pass

        def exit_before_term(*_arguments: object, **_options: object) -> bool:
            saved = admission_wire.parse_record(path.read_text(), kill_action.SavedKillAction)
            self.assertIsInstance(saved.stage, kill_action.StopRequested)
            self.assertEqual(saved.result["stop"], kill_action.StopState.STOPPING)
            self.assertEqual(saved.scope, route.scope)
            self.assertFalse(self.scope_has_ended(route.scope))
            raise FixtureRestart

        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "stop_verified_scope", new=exit_before_term),
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
              patch.object(kill_action, "stop_verified_scope") as repeated_stop,
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
        _ = write_config_fixture(self.root / "review_links.conf", "kill_pending_check_seconds=0.1")
        folder = self.root / "detached-recorded.scope"
        child = folder / "child"
        child.mkdir(parents=True)
        _ = (folder / "cgroup.procs").write_text("")
        _ = (child / "cgroup.procs").write_text("20\n")
        events = folder / "cgroup.events"
        _ = events.write_text("populated 1\n")
        scope = self.scope_identity(folder)
        route = kill_action.DetachedCommand(self.root / "pruned-detached-record.json", 18, "123", 19, "122", scope)
        run = kill_action.KillCandidate(18, "123", 18, "123", "session", "check", "at", "")
        target = kill_action.KillTarget(run, route, staff.StaffId("retired"), "founder, a producer", "founder", "producer")
        actions = kill_action.KillActions(self.root)
        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "stop_verified_scope", side_effect=unconfirmed_term),
              patch.object(actions, "_start_watch"),
              patch.object(kill_action, "deliver") as send):
            response = actions.stop(run.reference, actions.handle(target), "Use the next slot.")
        assert not isinstance(response, kill_action.NoStopRoute)
        self.assertEqual(response["stop"], kill_action.StopState.UNCONFIRMED)
        send.assert_not_called()
        path = self.root / ".review-link-build-kill" / f"{run.reference}.json"
        saved = admission_wire.parse_record(path.read_text(), kill_action.SavedKillAction)
        self.assertIsInstance(saved.stage, kill_action.TermSent)
        self.assertEqual(saved.scope, scope)
        self.assertFalse(route.record.exists())

        observed = threading.Event()
        released = threading.Event()
        workers: list[threading.Thread] = []
        start = threading.Thread.start

        def record_thread(thread: threading.Thread) -> None:
            workers.append(thread)
            start(thread)

        def stop(saved_scope: process_stop.ScopeIdentity, _grace: float, _kill_grace: float, *,
                 on_term: Callable[[int], None] | None) -> process_stop.ScopeStopReceipt:
            self.assertEqual(saved_scope, scope)
            self.assertIsNone(on_term)
            observed.set()
            self.assertTrue(released.wait(TEST_HANG_GUARD_SECS))
            self.assertTrue(self.scope_has_ended(saved_scope))
            return process_stop.ScopeStopReceipt(1)

        response_text = "send.py: staff:retired: founder is a retired producer"
        refusal = subprocess.CompletedProcess(["send.py"], 2, "", response_text)
        resumed = kill_action.KillActions(self.root)
        with (patch.object(process_stop, "stop_scope", new=stop),
              patch.object(kill_action, "live_targets", return_value={}),
              patch.object(kill_action, "stop_verified_scope") as repeated_stop,
              patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", return_value=refusal) as send,
              patch.object(threading.Thread, "start", new=record_thread)):
            resumed.resume()
            try:
                self.assertTrue(observed.wait(TEST_HANG_GUARD_SECS))
                self.assertFalse(self.scope_has_ended(scope))
                self.assertEqual(resumed.options()[1][0]["stop"], kill_action.StopState.UNCONFIRMED)
                send.assert_not_called()
            finally:
                _ = (child / "cgroup.procs").write_text("")
                _ = events.write_text("populated 0\n")
                released.set()
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
        route = kill_action.ProgramScope(self.scope_identity(scope), 20, "125")
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
              patch.object(kill_action, "stop_verified_scope", side_effect=confirm_term),
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
        route = self.program_scope()
        target = kill_action.KillTarget(run, route, staff.StaffId("producer"), "founder, a producer", "founder", "producer")
        actions = kill_action.KillActions(self.root)
        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "stop_verified_scope", return_value=False),
              patch.object(kill_action, "deliver") as deliver):
            outcome = actions.stop(run.reference, actions.handle(target), "")
        deliver.assert_not_called()
        assert not isinstance(outcome, kill_action.NoStopRoute)
        self.assertEqual((outcome["reference"], outcome["step"], outcome["pid"]), (run.reference, "check", 17))
        self.assertEqual((outcome["stop"], outcome["delivery"]),
                         (kill_action.StopState.NOT_SENT, kill_action.DeliveryState.PENDING))

    def test_only_a_saved_term_count_allows_a_kill_notice(self) -> None:
        _ = write_config_fixture(self.root / "shutdown.conf")
        _ = write_config_fixture(self.root / "codex_daemon.conf")
        for detached in (False, True):
            for scenario in ("natural exit", "restart", "error", "reused scope"):
                with self.subTest(detached=detached, scenario=scenario):
                    self.check_term_receipt(detached, scenario)

    def check_term_receipt(self, detached: bool, scenario: str) -> None:
        root = self.root / f"{detached}-{scenario}"
        folder = root / "builds-detached.slice" / "detached-fixture.scope"
        folder.mkdir(parents=True)
        events = folder / "cgroup.events"
        _ = events.write_text("populated 1\n")
        kill_file = folder / "cgroup.kill"
        kill_file.touch()
        scope = self.scope_identity(folder)
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        recipient = staff.StaffId("worker-id")
        record = root / "fixture.json"
        route = (kill_action.DetachedCommand(record, 17, "123", 16, "122", scope)
                 if detached else kill_action.ProgramScope(scope, 17, "123"))
        target = kill_action.KillTarget(run, route, recipient, "impl2", "impl2", "worker")
        if detached:
            _ = record.write_text(json.dumps({
                "run_id": "fixture", "status": "running", "session_dir": str(root),
                "worker": "impl2", "staff_id": recipient, "label": "fixture", "command": ["cargo", "check"],
                "log": str(root / "fixture.log"), "started_at": 10, "pid": 16, "director_dir": "",
                "done_message_file": "", "command_scope": scope, "command_pid": 17,
                "command_group": 17, "command_started": "123", "supervisor_started": "122",
            }))
        actions = kill_action.KillActions(root)
        journal = root / ".review-link-build-kill" / f"{run.reference}.json"
        count = 0 if scenario == "natural exit" else 2

        class Restart(BaseException):
            pass

        def enter_exit_wait(_scope: process_stop.ScopeIdentity, _descriptor: int, _deadline: float) -> bool:
            saved = admission_wire.parse_record(journal.read_text(), kill_action.SavedKillAction)
            if not count:
                self.assertIsInstance(saved.stage, kill_action.StopRequested)
                _ = events.write_text("populated 0\n")
                return True
            self.assertEqual(saved.stage, kill_action.TermSent(count))
            if detached:
                detached_record = cast(dict[str, object], json.loads(record.read_text()))
                self.assertEqual(detached_record.get("stop_reason"), saved.message)
            if scenario == "restart":
                if term.call_count == 1:
                    raise Restart
                if not kill_file.read_text():
                    self.assertFalse(self.scope_has_ended(scope))
                    return False
                self.assertEqual(kill_file.read_text(), "1\n")
                _ = events.write_text("populated 0\n")
                return True
            if scenario == "reused scope":
                _ = folder.rename(folder.with_suffix(".old"))
                folder.mkdir()
                _ = events.write_text("populated 1\n")
                return True
            raise process_stop.ScopeStopFailed("fixture failure after TERM")

        def start(target_actions: kill_action.KillActions) -> kill_action.KillResult | kill_action.NoStopRoute:
            return target_actions.stop(run.reference, target_actions.handle(target), "")

        def deliver(confirmed: kill_action.SavedKillAction) -> kill_action.DeliveryState:
            saved = admission_wire.parse_record(journal.read_text(), kill_action.SavedKillAction)
            self.assertEqual(saved, confirmed)
            self.assertEqual(saved.stage, kill_action.SendingNotice(kill_action.TermSent(count)))
            self.assertEqual(saved.scope, scope)
            if scenario == "reused scope":
                self.assertEqual(events.read_text(), "populated 1\n")
            else:
                self.assertTrue(self.scope_has_ended(scope))
            return kill_action.DeliveryState.SENT

        with (patch.object(process_stop, "CGROUP_ROOT", self.root),
              patch.object(process_stop, "_term_scope", return_value=count) as term,
              patch.object(process_stop, "_scope_ends_on", side_effect=enter_exit_wait) as wait,
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", return_value=route),
              patch.object(kill_action, "live_targets", return_value={run.reference: target}) as targets,
              patch.object(actions, "_start_watch"),
              patch.object(kill_action, "deliver", side_effect=deliver) as send):
            if scenario == "restart":
                with self.assertRaises(Restart):
                    _ = start(actions)
            else:
                outcome = start(actions)
                assert not isinstance(outcome, kill_action.NoStopRoute)
                expected = {
                    "error": kill_action.StopState.UNCONFIRMED,
                    "natural exit": kill_action.StopState.ALREADY_ENDED,
                    "reused scope": kill_action.StopState.STOPPED,
                }
                self.assertEqual(outcome["stop"], expected[scenario])
            wait.assert_called_once()
            if scenario == "reused scope":
                send.assert_called_once()
            else:
                send.assert_not_called()
            if not count and detached:
                self.assertNotIn("stop_reason", record.read_text())
                self.assertNotIn("stop_pass_requested", record.read_text())
            if scenario == "error":
                _ = events.write_text("populated 0\n")
            targets.return_value = {}
            recovered = kill_action.KillActions(root)
            workers: list[threading.Thread] = []
            start_thread = threading.Thread.start

            def record_thread(thread: threading.Thread) -> None:
                workers.append(thread)
                start_thread(thread)

            with patch.object(threading.Thread, "start", new=record_thread):
                recovered.resume()
                for worker in workers:
                    worker.join(TEST_HANG_GUARD_SECS)
                    self.assertFalse(worker.is_alive())
                recovered_again = kill_action.KillActions(root)
                recovered_again.resume()
                self.assertEqual(len(workers), int(scenario in ("restart", "error")))
            (_, (result,)) = recovered_again.options()
            self.assertEqual(result["stop"],
                             kill_action.StopState.STOPPED if count else kill_action.StopState.ALREADY_ENDED)
            self.assertEqual(term.call_count, 2 if scenario == "restart" else 1)
            if scenario == "restart":
                self.assertEqual(kill_file.read_text(), "1\n")
                self.assertTrue(self.scope_has_ended(scope))
            self.assertEqual(send.call_count, int(count > 0))


if __name__ == "__main__":
    _ = unittest.main()

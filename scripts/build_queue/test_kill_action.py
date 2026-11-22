"""Use private process trees and message delivery for Builds stop actions."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
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
from unittest.mock import patch
from zoneinfo import ZoneInfo

from scripts.agents import detached_runs
from scripts.build_queue import admission_client, admission_records as records, kill_action
from scripts.build_queue.admission_jobs import VerifyCall
from scripts.build_queue.test_memory_feed import reservation_reading
from scripts.buildlog import store
from scripts.lib import process_start
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit
from scripts.lint.memory_test_support import write_config_fixture
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

    def test_only_local_running_cargo_and_verify_are_candidates(self) -> None:
        reading = reservation_reading()
        (step,) = reading.view.running
        (saved,) = reading.explanation.decided_from.reservations
        reservation: records.Reservation = {**saved, "pid": 17, "start_time": "123"}
        step = replace(step, pid=17, session_id="session")
        verified = replace(step, step="verify step", verify=VerifyCall("check", "fixture", 1, 1))
        for current in (step, verified):
            self.assertIsInstance(kill_action.candidate(current, reservation, "natedev"), kill_action.KillCandidate)
        cases = ((step, "mac", False), (step, "aws", False), (step, "natedev", True),
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

    def test_scope_stop_uses_the_verified_scope_and_confirms_empty(self) -> None:
        scope = self.root / "builds.slice" / "run-fixture.scope"
        scope.mkdir(parents=True)
        events = scope / "cgroup.events"
        _ = events.write_text("populated 1\n")
        sidecar = self.root / "scope"
        _ = sidecar.write_text(str(scope))
        with self.process(str(self.cargo)) as program:
            pid, child = map(int, self.line(program).split())
            _ = (scope / "cgroup.procs").write_text(f"{pid}\n{child}\n")
            holder = os.getpid()
            started = self.started(holder)
            current = kill_action.KillCandidate(holder, started, holder, started, "session", "check", "at", str(sidecar))

            def stop(command: list[str], **_options: object) -> subprocess.CompletedProcess[bytes]:
                self.assertEqual(command, ["systemctl", "--user", "stop", "run-fixture.scope"])
                program.send_signal(signal.SIGTERM)
                self.assertEqual(program.wait(timeout=TEST_HANG_GUARD_SECS), 143)
                _ = events.write_text("populated 0\n")
                return subprocess.CompletedProcess(command, 0)

            with patch.object(memory_admit, "builds_slice_cgroup", return_value=scope.parent):
                processes = memory_admit.process_table()
                route = kill_action.stop_route(current, staff.StaffId("worker-id"), processes)
                assert isinstance(route, kill_action.ProgramScope)
                with patch.object(subprocess, "run", new=stop):
                    confirmation = kill_action.stop_program_scope(route, current, 2)
                    assert not isinstance(confirmation, kill_action.NoStopRoute)
                    self.assertTrue(confirmation())
                self.assertIsInstance(process_start.proc_table_start(child, Path("/proc")), process_start.Gone)

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

            with patch.object(detached_runs, "stop_verified_command", new=stable_stop, create=True):
                route = kill_action.stop_route(run, staff.StaffId("worker-id"), memory_admit.process_table())
                self.assertEqual(route, kill_action.DetachedCommand(path, pid, started, command.pid, self.started(command.pid)))
                assert isinstance(route, kill_action.DetachedCommand)
                confirmation = kill_action.stop_detached(route, run, "fixture stop")
                assert not isinstance(confirmation, kill_action.NoStopRoute)
                self.assertTrue(kill_action.observe(confirmation, 5, .02))
            self.assertEqual(command.wait(timeout=TEST_HANG_GUARD_SECS), 0)
            self.assertIsInstance(process_start.proc_table_start(child, Path("/proc")), process_start.Gone)
            self.assertIsNone(detached_runs.command_group(path))

    def test_verify_stop_prevents_the_next_step_and_waits_for_owned_scopes(self) -> None:
        script = self.root / "verify.sh"
        _ = script.write_text("""cleanup() {
    kill -TERM "$child"
    wait "$child"
    printf 'cleanup ran' > "$1"
    exit 143
}
trap 'cleanup "$1"' TERM
"$3" -c 'import signal; signal.pause()' &
child=$!
printf '%s\n' "$child"
wait "$child"
printf 'next step ran' > "$2"
""")
        cleaned, next_step = self.root / "cleaned", self.root / "next"
        scope = self.root / "run-fixture.scope"
        scope.mkdir()
        events = scope / "cgroup.events"
        _ = events.write_text("populated 1\n")
        stat = scope.stat()
        owned_scope = kill_action.ProgramScope(scope, stat.st_dev, stat.st_ino)
        with self.process(str(script), str(cleaned), str(next_step), sys.executable, executable="bash") as verify:
            child = int(self.line(verify))
            pid, started = verify.pid, self.started(verify.pid)
            run = kill_action.KillCandidate(pid, started, pid, started, "session", "lint", "at", "", verify_call=True)
            route = kill_action.stop_route(run, staff.StaffId("worker-id"), memory_admit.process_table())
            self.assertEqual(route, kill_action.VerifyRun(pid, started))
            assert isinstance(route, kill_action.VerifyRun)
            with patch.object(kill_action, "member_scope", return_value=owned_scope):
                confirmation = kill_action.stop_verify(route, run)
                assert not isinstance(confirmation, kill_action.NoStopRoute)
                self.assertEqual(verify.wait(timeout=TEST_HANG_GUARD_SECS), 143)
                self.assertFalse(confirmation())
                _ = events.write_text("populated 0\n")
                self.assertTrue(confirmation())
            self.assertEqual(cleaned.read_text(), "cleanup ran")
            self.assertFalse(next_step.exists())
            self.assertIsInstance(process_start.proc_table_start(child, Path("/proc")), process_start.Gone)

    def test_the_service_uses_the_recorded_session_for_each_staff_kind(self) -> None:
        reading = reservation_reading()
        scope = kill_action.ProgramScope(self.root / "run-fixture.scope", 1, 2)
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
        route = kill_action.ProgramScope(self.root / "run-fixture.scope", 1, 2)
        recipient = staff.StaffId("stable-worker-id")
        target = kill_action.KillTarget(run, route, recipient, "impl3, a worker", "impl3", "worker")
        actions = kill_action.KillActions()
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
            with patch.object(kill_action, "stop_program_scope") as stop:
                def confirm(*_arguments: object) -> bool:
                    deliver.assert_not_called()
                    self.assertEqual(actions.options()[1][0]["stop"], kill_action.StopState.STOPPING)
                    targets.return_value = {}
                    return True

                stop.return_value = confirm
                result = actions.stop(run.reference, handle, "Use the next slot.")
                deliver.assert_called_once_with(recipient, reason + "\nUse the next slot.")
            self.assertEqual(result, {"reference": run.reference, "step": "nextest", "pid": 17,
                                      "stop": kill_action.StopState.STOPPED, "delivery": kill_action.DeliveryState.SENT,
                                      "time": "19:41 EST", "recipient_name": "impl3", "recipient_role": "worker"})
            self.assertEqual(actions.options(), ({}, [result]))
            self.assertIsInstance(actions.stop(run.reference, handle, "Again."), kill_action.NoStopRoute)

    def test_late_stop_delivers_the_saved_words_and_updates_the_result(self) -> None:
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        route = kill_action.ProgramScope(self.root / "run-fixture.scope", 1, 2)
        target = kill_action.KillTarget(run, route, staff.StaffId("producer"), "founder, a producer", "founder", "producer")
        actions = kill_action.KillActions()
        ready = threading.Event()
        worker: list[threading.Thread] = []
        start = threading.Thread.start

        def record_thread(thread: threading.Thread) -> None:
            worker.append(thread)
            start(thread)

        def observe(confirmation: kill_action.StopConfirmation, seconds: float, _sample: float) -> bool:
            return seconds == 300 and ready.wait(TEST_HANG_GUARD_SECS) and confirmation()

        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "stop_program_scope", return_value=lambda: True),
              patch.object(kill_action, "observe", new=observe),
              patch.object(threading.Thread, "start", new=record_thread),
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.QUEUED) as deliver):
            try:
                first = actions.stop(run.reference, actions.handle(target), "Use the next slot.")
                assert not isinstance(first, kill_action.NoStopRoute)
                self.assertEqual(first["stop"], kill_action.StopState.UNCONFIRMED)
                self.assertTrue(actions.has_pending())
                deliver.assert_not_called()
            finally:
                ready.set()
                for thread in worker:
                    thread.join(TEST_HANG_GUARD_SECS)
            self.assertFalse(actions.has_pending())
            (_, outcomes) = actions.options()
            self.assertEqual((outcomes[0]["stop"], outcomes[0]["delivery"]),
                             (kill_action.StopState.STOPPED, kill_action.DeliveryState.QUEUED))
            deliver.assert_called_once_with(target.recipient,
                f'nate killed your run "check" (pid 17) from the Builds page at {first["time"]}\nUse the next slot.')

    def test_unconfirmed_stop_does_not_send_a_message(self) -> None:
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", "check", "at", "")
        route = kill_action.ProgramScope(self.root / "run-fixture.scope", 1, 2)
        target = kill_action.KillTarget(run, route, staff.StaffId("producer"), "founder, a producer", "founder", "producer")
        actions = kill_action.KillActions()
        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "stop_program_scope", return_value=kill_action.NoStopRoute()),
              patch.object(kill_action, "deliver") as deliver):
            outcome = actions.stop(run.reference, actions.handle(target), "")
        deliver.assert_not_called()
        assert not isinstance(outcome, kill_action.NoStopRoute)
        self.assertEqual((outcome["reference"], outcome["step"], outcome["pid"]), (run.reference, "check", 17))
        self.assertEqual((outcome["stop"], outcome["delivery"]),
                         (kill_action.StopState.UNCONFIRMED, kill_action.DeliveryState.PENDING))


if __name__ == "__main__":
    _ = unittest.main()

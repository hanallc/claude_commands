"""Use private process trees and message delivery for Builds stop actions."""
from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager, nullcontext, redirect_stdout, suppress
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone, tzinfo
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import IO, cast, override
import unittest
from unittest.mock import ANY, call, patch
from zoneinfo import ZoneInfo

from scripts.agents import detached_run_process, detached_runs, worker_lifecycle
from scripts.build_queue import admission_client, admission_records as records, admission_wire, kill_action
from scripts.build_queue.admission_jobs import VerifyCall
from scripts.build_queue.queue_fixture import write_process
from scripts.build_queue.test_memory_feed import reservation_reading
from scripts.buildlog import store
from scripts.direct import progress_history
from scripts.herdr import turn_end, worker_panes
from scripts.lib import atomic_file, process_group, process_start, process_stop, scratch
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import send, send_budget
from scripts.production import staff


class KillActionTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = self.enterContext(patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())))
        _ = self.enterContext(patch.object(process_stop, "CGROUP_ROOT", self.root / "cgroups"))
        # Each test owns its message state and sends nothing, so no test reads the machine's message state.
        _ = self.enterContext(patch.object(send, "STATE", self.root / "message"))
        _ = self.enterContext(patch.object(send, "send", return_value=send.Result("sent", "fixture delivery")))
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})))

    def launch_record(self, caller: detached_runs.Caller, command: list[str],
                      launch_supervisor: Callable[[list[str], IO[bytes]], object]) -> Path:
        runs = self.root / detached_runs.DETACHED_RUNS_DIR
        before = set(runs.glob("*.json"))
        _ = (self.root / "detached_runs.conf").write_text("# whole: 1..10\nlive_run_limit=2\n")
        popen = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)

        def launch(argv: list[str], **options: object) -> object:
            if len(argv) == 4 and Path(argv[1]).name == "detached_runs.py" and argv[2] == "supervise":
                return launch_supervisor(argv, cast(IO[bytes], options["stdout"]))
            return popen(argv, **options)

        with (patch.object(detached_runs, "caller_from_environment", return_value=caller),
              patch.object(subprocess, "Popen", side_effect=launch),
              patch.dict(os.environ, {"CONF_DIR": str(self.root)}),
              redirect_stdout(StringIO())):
            self.assertEqual(detached_runs.command_start(Namespace(
                session_dir=str(self.root), command=command, kind=detached_runs.DetachedRunKind.OTHER)), 0)
        (path,) = set(runs.glob("*.json")) - before
        return path

    def detached_target(self, *, name: str = "impl3", role: str = "worker",
                        step: str = "check", published: bool = True) -> kill_action.KillTarget:
        recipient = staff.StaffId("fixture")
        supervisor = os.getpid()
        started = process_start.process_start(supervisor)
        assert isinstance(started, process_start.Started)
        record = self.launch_record(detached_runs.DirectorCaller(name, recipient), ["cargo", step],
                                    lambda _argv, _log: SimpleNamespace(pid=supervisor))
        run_slice: process_stop.RunSliceIdentity = {"unit": "builds-detached-fixture.slice", "device": 1, "inode": 2,
            "path": "/user.slice/builds.slice/builds-detached.slice/builds-detached-fixture.slice"}
        route = kill_action.DetachedCommand(record, 17, "123", supervisor, started.start, run_slice)
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", step, "at")
        target = kill_action.KillTarget(run, route, recipient, f"{name}, {role}", name, role)
        if published:
            self.write_run(target)
        return target

    def write_run(self, target: kill_action.KillTarget, **fields: object) -> None:
        route = target.route
        record = worker_lifecycle.read_json_object(route.record)
        record.update({"command_slice": route.run_slice, "completion_after_slice_cleanup": True,
                       "supervisor_started": route.supervisor_start, **fields})
        _ = route.record.write_text(json.dumps(record))

    def finish_run(self, target: kill_action.KillTarget, **fields: object) -> None:
        path = target.route.record
        if fields:
            record = worker_lifecycle.read_json_object(path)
            record.update(fields)
            _ = path.write_text(json.dumps(record))
        with (patch.object(detached_runs, "_run", return_value=143),
              patch.object(detached_runs, "_completion_sources", return_value=nullcontext()),
              patch.object(detached_runs, "_finish_command_slice"),
              patch.object(detached_runs, "deliver_in_fresh_process", return_value=0),
              patch.object(detached_runs, "_complete_stopped_run", return_value="finished: fixture"),
              patch.object(detached_runs, "_complete_worker_dispatch", return_value=detached_runs.CompletionReplay.HANDLED)):
            self.assertEqual(detached_runs.command_supervise(Namespace(record=str(path))), 0)
        self.assertEqual(worker_lifecycle.read_json_object(path)["status"], "finished")

    def saved_action(self, target: kill_action.KillTarget) -> kill_action.SavedKillAction:
        path = self.root / ".review-link-build-kill" / f"{target.candidate.reference}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return admission_wire.parse_record(path.read_text(), kill_action.SavedKillAction)

    def resume(self) -> kill_action.KillResult:
        resumed = kill_action.KillActions(self.root)
        workers: list[threading.Thread] = []
        start = threading.Thread.start

        def record_thread(thread: threading.Thread) -> None:
            if thread.name == kill_action.RECOVERY_THREAD:
                workers.append(thread)
            start(thread)

        with (patch.object(threading.Thread, "start", new=record_thread),
              patch.object(kill_action, "live_targets", return_value={})):
            resumed.resume()
            (worker,) = workers
            worker.join(TEST_HANG_GUARD_SECS)
            self.assertFalse(worker.is_alive())
            recovered = kill_action.KillActions(self.root)
            recovered.resume()
            self.assertEqual(workers, [worker])
            offers, (result,) = recovered.options(15)
            self.assertEqual(offers, {})
        return result

    def test_outside_detached_command_requires_its_verified_route(self) -> None:
        queued, _, scope = self.scoped_target()
        run = kill_action.OutsideKillCandidate(queued.candidate.holder_pid, queued.candidate.holder_start, "test.py")
        self.assertEqual(kill_action.outside_stop_route(run, queued.recipient), queued.route)
        proc = kill_action.PROC / str(run.row_pid)
        _ = (proc / "cgroup").write_text(f"0::{scope.parent.with_name('other.slice') / scope.name}\n")
        self.assertIsInstance(kill_action.outside_stop_route(run, queued.recipient), kill_action.NoStopRoute)
        _ = (proc / "cgroup").write_text(f"0::{scope}\n")
        _ = (proc / "environ").write_text("")
        self.assertIsInstance(kill_action.outside_stop_route(run, queued.recipient), kill_action.NoStopRoute)
        _ = (proc / "cgroup").write_text("0::/app.slice/app-agents.slice/test.scope\n")
        self.assertEqual(kill_action.outside_stop_route(run, queued.recipient), kill_action.OutsideCommandTree())

    @unittest.skipUnless(process_stop.supported(), "The test needs Linux pidfds")
    def test_outside_kill_refuses_a_reused_pid(self) -> None:
        with subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"]) as process:
            try:
                started = process_start.process_start(process.pid)
                assert isinstance(started, process_start.Started)
                run = kill_action.OutsideKillCandidate(process.pid, str(int(started.start) + 1), "test.py")
                self.install_outside_stop_graces()
                with patch.object(signal, "pidfd_send_signal") as sent:
                    self.assertEqual(kill_action.stop_outside_tree(run),
                                     kill_action.OutsideIdentityUnconfirmed("The command PID changed before the stop."))
                sent.assert_not_called()
                self.assertIsNone(process.poll())
            finally:
                process.kill()

    def install_outside_stop_graces(self) -> None:
        _ = write_config_fixture(self.root / "shutdown.conf", "stop_grace_seconds=1\nkill_grace_seconds=1")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.root)})))

    def outside_stop_fixture(self) -> Path:
        self.install_outside_stop_graces()
        proc = self.root / "proc"
        write_process(proc, 810, parent=1, start="200")
        _ = self.enterContext(patch.object(kill_action, "PROC", proc))
        return proc

    def pin_fixture_process(self, process: process_stop.ProcessLifetime) -> AbstractContextManager[int]:
        if not kill_action.live(process.pid, process.start):
            raise ProcessLookupError(process.pid)
        return nullcontext(process.pid)

    def test_outside_tree_refuses_a_later_child_of_a_reused_parent_pid(self) -> None:
        proc = self.outside_stop_fixture()
        write_process(proc, 811, parent=1, start="202")
        write_process(proc, 812, parent=811, start="203")
        observed = {810: memory_admit.ProcessIdentity(1, "200"),
                    811: memory_admit.ProcessIdentity(810, "201"),
                    812: memory_admit.ProcessIdentity(811, "203")}
        run = kill_action.OutsideKillCandidate(810, "200", "test.py")
        with (patch.object(memory_admit, "process_table", return_value=observed),
              patch.object(process_stop, "pin_process", side_effect=self.pin_fixture_process),
              patch.object(signal, "pidfd_send_signal") as sent):
            self.assertEqual(kill_action.stop_outside_tree(run),
                             kill_action.OutsideIdentityUnconfirmed("Cannot verify the process tree at pid 812."))
        sent.assert_not_called()

    def test_outside_tree_checks_a_reparented_child_under_a_reused_pid(self) -> None:
        proc = self.outside_stop_fixture()
        run = kill_action.OutsideKillCandidate(810, "200", "test.py")
        before = {810: memory_admit.ProcessIdentity(1, "200"), 811: memory_admit.ProcessIdentity(810, "201")}
        after = {811: memory_admit.ProcessIdentity(1, "203"), 812: memory_admit.ProcessIdentity(811, "202")}

        for child_live in (False, True):
            with self.subTest(child_live=child_live):
                write_process(proc, 811, parent=810, start="201")
                reused = False

                def read_table(_proc: Path) -> dict[int, memory_admit.ProcessIdentity]:
                    if not reused:
                        return before
                    # The snapshot retains the child's old parent after it reparents.
                    if child_live:
                        write_process(proc, 812, parent=1, start="202")
                    write_process(proc, 811, parent=1, start="203")
                    return after

                def pidfd_live(pid: int) -> bool:
                    return not reused and pid in before

                def send(descriptor: int, _number: int) -> None:
                    nonlocal reused
                    if descriptor == run.row_pid:
                        reused = True

                with (patch.object(memory_admit, "process_table", side_effect=read_table),
                      patch.object(process_stop, "pin_process", side_effect=self.pin_fixture_process),
                      patch.object(kill_action, "pidfd_live", side_effect=pidfd_live),
                      patch.object(signal, "pidfd_send_signal", side_effect=send) as sent):
                    expected = (kill_action.OutsideIdentityUnconfirmed("Cannot verify the process tree at pid 812.")
                                if child_live else kill_action.OutsideTreeEmpty())
                    self.assertEqual(kill_action.stop_outside_tree(run), expected)
                self.assertEqual(sent.call_args_list, [call(811, signal.SIGTERM), call(810, signal.SIGTERM)])

    def test_outside_tree_reports_a_live_child_when_its_parent_exits_during_the_scan(self) -> None:
        proc = self.outside_stop_fixture()
        write_process(proc, 811, parent=810, start="201")
        write_process(proc, 812, parent=811, start="202")
        observed = {810: memory_admit.ProcessIdentity(1, "200"),
                    811: memory_admit.ProcessIdentity(810, "201"),
                    812: memory_admit.ProcessIdentity(811, "202")}
        run = kill_action.OutsideKillCandidate(810, "200", "test.py")
        open_pids: set[int] = set()
        exited: set[int] = set()

        @contextmanager
        def pin(process: process_stop.ProcessLifetime) -> Generator[int]:
            with self.pin_fixture_process(process) as descriptor:
                open_pids.add(process.pid)
                if process.pid == 812:
                    self.assertIn(811, open_pids)
                    # A stat read can still describe the parent after its pidfd reports exit.
                    exited.add(811)
                try:
                    yield descriptor
                finally:
                    open_pids.remove(process.pid)

        def pidfd_live(pid: int) -> bool:
            return pid in open_pids and pid not in exited

        with (patch.object(memory_admit, "process_table", return_value=observed),
              patch.object(process_stop, "pin_process", side_effect=pin),
              patch.object(kill_action, "pidfd_live", side_effect=pidfd_live),
              patch.object(signal, "pidfd_send_signal") as sent):
            self.assertEqual(kill_action.stop_outside_tree(run),
                             kill_action.OutsideIdentityUnconfirmed("Cannot verify the process tree at pid 812."))
        self.assertEqual(open_pids, set())
        sent.assert_not_called()

    def test_outside_tree_rereads_the_child_parent_after_opening_the_pidfd(self) -> None:
        proc = self.outside_stop_fixture()
        write_process(proc, 811, parent=810, start="200")
        observed = {810: memory_admit.ProcessIdentity(1, "200"),
                    811: memory_admit.ProcessIdentity(810, "200")}
        run = kill_action.OutsideKillCandidate(810, "200", "test.py")

        def pin(process: process_stop.ProcessLifetime) -> AbstractContextManager[int]:
            if process.pid == 811:
                write_process(proc, 811, parent=1, start="200")
            return self.pin_fixture_process(process)

        with (patch.object(memory_admit, "process_table", return_value=observed),
              patch.object(process_stop, "pin_process", side_effect=pin),
              patch.object(kill_action, "pidfd_live", return_value=True),
              patch.object(signal, "pidfd_send_signal") as sent):
            self.assertEqual(kill_action.stop_outside_tree(run),
                             kill_action.OutsideIdentityUnconfirmed("Cannot verify the process tree at pid 811."))
        sent.assert_not_called()

    def test_outside_tree_keeps_a_child_from_the_final_term_scan(self) -> None:
        proc = self.outside_stop_fixture()
        write_process(proc, 811, parent=810, start="201")
        run = kill_action.OutsideKillCandidate(810, "200", "test.py")
        root = {810: memory_admit.ProcessIdentity(1, "200")}
        empty: dict[int, memory_admit.ProcessIdentity] = {}
        snapshots = iter((root, root, {**root, 811: memory_admit.ProcessIdentity(810, "201")},
                          {811: memory_admit.ProcessIdentity(1, "201")}, empty))
        current: dict[int, memory_admit.ProcessIdentity] = {}

        def read_table(_proc: Path) -> dict[int, memory_admit.ProcessIdentity]:
            nonlocal current
            current = next(snapshots)
            return current

        def pidfd_live(pid: int) -> bool:
            return pid in current

        with (patch.object(memory_admit, "process_table", side_effect=read_table),
              patch.object(process_stop, "pin_process", side_effect=self.pin_fixture_process),
              patch.object(kill_action, "pidfd_live", side_effect=pidfd_live),
              patch.object(signal, "pidfd_send_signal") as sent,
              patch.object(time, "monotonic", side_effect=(0, 1, 2, 3))):
            self.assertEqual(kill_action.stop_outside_tree(run), kill_action.OutsideTreeEmpty())
        self.assertEqual(sent.call_args_list, [call(810, signal.SIGTERM), call(811, signal.SIGKILL)])

    @contextmanager
    def launch_outside_process(self, source: str) -> Generator[tuple[subprocess.Popen[bytes], IO[bytes]]]:
        reader, writer = socket.socketpair()
        with reader, writer:
            reader.settimeout(TEST_HANG_GUARD_SECS)
            environment = {name: value for name, value in os.environ.items() if name != "UNIT_DETACHED_RECORD"}
            command = [sys.executable, "-c", source, str(writer.fileno())]
            process = subprocess.Popen(command, env=environment, pass_fds=(writer.fileno(),), start_new_session=True)
            try:
                writer.close()
                with reader.makefile("rb") as stream:
                    yield process, stream
            finally:
                with suppress(ProcessLookupError):
                    process_group.send_process_group(process.pid, signal.SIGKILL)
                _ = process.wait(TEST_HANG_GUARD_SECS)

    def stop_outside_process(self, pid: int, *, words: str = "") -> tuple[kill_action.KillResult, list[str]]:
        started = process_start.process_start(pid)
        assert isinstance(started, process_start.Started)
        run = kill_action.OutsideKillCandidate(pid, started.start, "test.py")
        recipient = staff.StaffId("starter")
        with patch.object(kill_action, "in_detached_scope", return_value=False):
            route = kill_action.outside_stop_route(run, recipient)
        self.assertEqual(route, kill_action.OutsideCommandTree())
        assert isinstance(route, kill_action.OutsideCommandTree)
        target = kill_action.OutsideKillTarget(run, route, recipient, "test, worker", "test", "worker")
        actions = kill_action.KillActions(self.root, lambda: {run.reference: target})
        delivered: list[str] = []

        def deliver(action: kill_action.SavedKillAction) -> kill_action.DeliveryState:
            self.assertEqual(action.recipient, recipient)
            delivered.append(action.message)
            return kill_action.DeliveryState.SENT

        with (patch.object(kill_action, "live_targets", return_value={}),
              patch.object(kill_action, "deliver", side_effect=deliver)):
            offers, _ = actions.options(15)
            result = actions.stop(run.reference, offers[run.reference]["handle"], words)
        assert not isinstance(result, kill_action.NoStopRoute)
        record = self.root / ".review-link-build-kill" / f"{run.reference}.json"
        saved = admission_wire.parse_record(record.read_text(), kill_action.SavedKillAction)
        self.assertEqual((saved.route, saved.result), (route, result))
        return result, delivered

    @unittest.skipUnless(process_stop.supported(), "The test needs Linux pidfds")
    def test_outside_kill_keeps_the_starter_in_the_shared_group(self) -> None:
        self.install_outside_stop_graces()
        with self.launch_outside_process("""
import os, signal, socket, sys
with socket.socket(fileno=int(sys.argv[1])) as peer:
    command = os.fork()
    if command == 0:
        while True:
            signal.pause()
    peer.sendall(f"{command}\\n".encode())
    _, status = os.waitpid(command, 0)
    peer.sendall(f"{os.waitstatus_to_exitcode(status)}\\n".encode())
    peer.recv(1)
""") as (starter, stream):
            command_pid = int(stream.readline())
            self.assertEqual(os.getpgid(command_pid), os.getpgid(starter.pid))
            result, notices = self.stop_outside_process(command_pid, words="Run it again later.")
            self.assertEqual(result["stop"], kill_action.StopState.STOPPED)
            self.assertEqual(int(stream.readline()), -signal.SIGTERM)
            self.assertIsNone(starter.poll())
            message, = notices
            self.assertTrue(message.endswith("\nRun it again later."))

    @unittest.skipUnless(process_stop.supported(), "The test needs Linux pidfds")
    def test_outside_tree_kill_lets_the_command_handle_term(self) -> None:
        self.install_outside_stop_graces()
        with self.launch_outside_process("""
import signal, socket, sys
with socket.socket(fileno=int(sys.argv[1])) as peer:
    def finish(*_):
        peer.sendall(b"clean\\n")
        sys.exit(0)
    signal.signal(signal.SIGTERM, finish)
    peer.sendall(b"ready\\n")
    while True:
        signal.pause()
""") as (process, stream):
            self.assertEqual(stream.readline(), b"ready\n")
            result, notices = self.stop_outside_process(process.pid)
            self.assertEqual(stream.readline(), b"clean\n")
            self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), 0)
            self.assertEqual(result["stop"], kill_action.StopState.STOPPED)
            self.assertEqual(len(notices), 1)

    @unittest.skipUnless(process_stop.supported(), "The test needs Linux pidfds")
    def test_outside_tree_kills_a_reparented_child_that_ignores_term(self) -> None:
        self.install_outside_stop_graces()
        with self.launch_outside_process("""
import os, signal, socket, sys
with socket.socket(fileno=int(sys.argv[1])) as peer:
    if os.fork() == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        peer.sendall(f"{os.getpid()}\\n".encode())
    while True:
        signal.pause()
""") as (process, stream):
            child = int(stream.readline())
            result, notices = self.stop_outside_process(process.pid)
            self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), -signal.SIGTERM)
            self.assertEqual(stream.readline(), b"")
            self.assertNotIsInstance(process_start.process_start(child), process_start.Started)
            self.assertEqual(result["stop"], kill_action.StopState.STOPPED)
            self.assertEqual(len(notices), 1)

    @unittest.skipUnless(process_stop.supported(), "The test needs Linux pidfds")
    def test_outside_tree_reports_a_process_that_stays(self) -> None:
        self.install_outside_stop_graces()
        with self.launch_outside_process("""
import signal, socket, sys
with socket.socket(fileno=int(sys.argv[1])) as peer:
    peer.sendall(b"ready\\n")
    while True:
        signal.pause()
""") as (process, stream):
            self.assertEqual(stream.readline(), b"ready\n")
            with patch.object(signal, "pidfd_send_signal", return_value=None) as sent:
                result, notices = self.stop_outside_process(process.pid)
            self.assertEqual(sent.call_count, 2)
            self.assertIsNone(process.poll())
            self.assertEqual(result["stop"], kill_action.StopState.UNCONFIRMED)
            self.assertEqual(result.get("reason"), f"Still running after Kill: pid {process.pid}")
            self.assertEqual(notices, [])

    def test_saved_kill_numbers_advance_per_row_and_read_old_results_as_zero(self) -> None:
        run = kill_action.OutsideKillCandidate(17, "123", "test.py")
        recipient = staff.StaffId("fixture")
        target = kill_action.OutsideKillTarget(run, kill_action.OutsideCommandTree(), recipient,
                                               "test, worker", "test", "worker")
        other_run = replace(run, row_pid=18)
        other_target = replace(target, candidate=other_run)
        targets = {run.reference: target, other_run.reference: other_target}
        actions = kill_action.KillActions(self.root, lambda: targets)
        record = self.root / ".review-link-build-kill" / f"{run.reference}.json"

        def stop(service: kill_action.KillActions, reference: str) -> int | None:
            offers, _ = service.options(15)
            offer = offers[reference]
            result = service.stop(reference, offer["handle"], "", offer["kill_number"])
            assert not isinstance(result, kill_action.NoStopRoute)
            return result.get("kill_number")

        with (patch.object(kill_action, "live_targets", return_value={}),
              patch.object(kill_action, "stop_outside_tree", return_value=kill_action.OutsideProcessSurvived(17))):
            first = stop(actions, run.reference)
            legacy = worker_lifecycle.read_json_object(record)
            legacy_result = cast(dict[str, object], legacy["result"])
            _ = legacy_result.pop("kill_number", None)
            _ = record.write_text(json.dumps(legacy))
            resumed = kill_action.KillActions(self.root, lambda: targets)
            resumed.resume()
            _, (old_result,) = resumed.options(15)
            after_legacy = stop(resumed, run.reference)
            other_first = stop(resumed, other_run.reference)
            retry = stop(resumed, run.reference)
        saved = admission_wire.parse_record(record.read_text(), kill_action.SavedKillAction)
        self.assertEqual((first, old_result.get("kill_number"), after_legacy, other_first,
                          retry, saved.result.get("kill_number")), (1, 0, 1, 1, 2, 2))

    def test_outside_kill_waits_for_a_late_child_in_the_run_slice(self) -> None:
        queued, _, scope = self.scoped_target()
        run = kill_action.OutsideKillCandidate(queued.candidate.holder_pid, queued.candidate.holder_start, "test.py")
        route = kill_action.outside_stop_route(run, queued.recipient)
        assert isinstance(route, kill_action.DetachedCommand)
        target = kill_action.OutsideKillTarget(run, route, queued.recipient, queued.starter,
                                               queued.recipient_name, queued.recipient_role)
        folder = process_stop.CGROUP_ROOT / scope.parent.relative_to("/")
        events = folder / "cgroup.events"
        kill_file = folder / "cgroup.kill"
        members = folder / scope.name / "cgroup.procs"
        _ = events.write_text("populated 1\n")
        kill_file.touch()

        def term(_directory: int, identity: process_stop.RunSliceIdentity) -> None:
            self.assertEqual(identity, route.run_slice)
            # After TERM, the command exits and a child remains in the slice.
            _ = members.write_text("918\n")

        actions = kill_action.KillActions(self.root, lambda: {run.reference: target})
        with (patch.object(kill_action, "live_targets", return_value={}),
              patch.object(detached_runs, "_scope_stop_graces", return_value=(0.0, 0.0)),
              patch.object(detached_runs, "stop_command", wraps=detached_runs.stop_command) as stop,
              patch.object(process_stop, "_term_slice", side_effect=term) as send_term,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as deliver):
            offers, _ = actions.options(15)
            outcome = actions.stop(run.reference, offers[run.reference]["handle"], "")
            assert not isinstance(outcome, kill_action.NoStopRoute)
            self.assertEqual(outcome["stop"], kill_action.StopState.STOPPING)
            stop.assert_called_once()
            self.assertEqual(cast(object, stop.call_args.args[0]), route.record)
            send_term.assert_called_once()
            self.assertEqual(members.read_text(), "918\n")
            self.assertEqual(kill_file.read_text(), "1\n")
            deliver.assert_not_called()

            _ = members.write_text("")
            _ = events.write_text("populated 0\n")
            self.finish_run(queued)
            self.assertEqual(self.resume()["stop"], kill_action.StopState.STOPPED)
            deliver.assert_called_once()

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

    def test_detached_stop_uses_the_public_api_with_the_saved_route_check(self) -> None:
        target = self.detached_target()
        route = target.route
        reason = "Stopped from the Builds page."
        moved = replace(route, run_slice={**route.run_slice, "inode": route.run_slice["inode"] + 1})
        self.save_unfinished_request(target)
        action = replace(self.saved_action(target), message=reason)

        def stop_detached(_record: Path, _reason: str, *, check: Callable[[], bool], stop_notice_id: str) -> bool:
            self.assertEqual(stop_notice_id, self.saved_action(target).id)
            return check()

        with (patch.object(process_stop, "stop_slice") as stop_slice,
              patch.object(detached_runs, "stop_command", side_effect=stop_detached) as stop,
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", side_effect=[route, moved])):
            self.assertTrue(kill_action.request_stop(action))
            self.assertFalse(kill_action.request_stop(action))
        self.assertEqual([call.args for call in stop.call_args_list], [(route.record, reason)] * 2)
        self.assertEqual([call.kwargs["stop_notice_id"] for call in stop.call_args_list], [action.id] * 2)
        stop_slice.assert_not_called()

    def test_standalone_cargo_has_no_stop_route(self) -> None:
        target = self.detached_target()
        proc = self.root / "proc"
        owner = proc / "17"
        owner.mkdir(parents=True)
        _ = (owner / "environ").write_bytes(b"")
        table = {17: memory_admit.ProcessIdentity(1, "123")}
        with (patch.object(kill_action, "PROC", proc),
              patch.object(kill_action, "program", return_value="cargo"),
              patch.object(kill_action, "live", return_value=True)):
            self.assertIsInstance(kill_action.stop_route(target.candidate, target.recipient, table),
                                  kill_action.NoStopRoute)

    def test_routes_are_unavailable_without_pidfd_support(self) -> None:
        target = self.detached_target()
        with (patch.object(process_stop, "supported", return_value=False),
              patch.object(kill_action, "detached_command") as detached):
            self.assertIsInstance(kill_action.stop_route(target.candidate, target.recipient, {}), kill_action.NoStopRoute)
        detached.assert_not_called()

    def scoped_target(self, *, published: bool = True,
                      ) -> tuple[kill_action.KillTarget, dict[int, memory_admit.ProcessIdentity], Path]:
        """Match a verified detached command to a step scope in its run slice."""
        target = self.detached_target(published=False)
        proc = self.root / "proc"
        cgroups = self.root / "cgroups"
        route = target.route
        holder = 18
        run = replace(target.candidate, holder_pid=holder, holder_start="124")
        launcher = proc / str(route.group)
        step = proc / str(holder)
        for process in (launcher, step):
            process.mkdir(parents=True)
            _ = (process / "environ").write_text(f"UNIT_DETACHED_RECORD={route.record}\0")

        member_scope = Path(route.run_slice["path"]) / "run-holder.scope"
        scope_folder = cgroups / member_scope.relative_to("/")
        scope_folder.mkdir(parents=True)
        _ = (scope_folder / "cgroup.procs").write_text(f"{holder}\n")
        _ = (step / "cgroup").write_text(f"0::{member_scope}\n")
        identity = scope_folder.parent.stat()
        run_slice: process_stop.RunSliceIdentity = {**route.run_slice, "device": identity.st_dev, "inode": identity.st_ino}
        route = replace(route, run_slice=run_slice)
        target = replace(target, candidate=run, route=route)
        if published:
            self.write_run(target, command_pid=route.group, command_group=route.group, command_started=route.started)
        table = {route.group: memory_admit.ProcessIdentity(route.supervisor, route.started),
                 holder: memory_admit.ProcessIdentity(route.group, run.holder_start)}
        caller = staff.StaffRow(target.recipient, "worker", target.recipient_name, staff.StaffRoot(),
                                current_session=staff.StaffId(run.session))
        for context in (
            patch.object(kill_action, "PROC", proc),
            patch.object(kill_action, "live", return_value=True),
            patch.object(kill_action, "program", return_value="cargo"),
            patch.object(kill_action, "in_group", return_value=True),
            patch.object(detached_run_process, "supervisor_match", return_value=True),
            patch.object(process_stop, "pin_process", return_value=nullcontext(5)),
            patch.object(process_stop, "PROC", proc),
            patch.object(process_stop, "CGROUP_ROOT", cgroups),
            patch.object(store, "host_name", return_value="natedev"),
            patch.object(admission_client, "view", return_value=reservation_reading()),
            patch.object(kill_action, "candidate", return_value=run),
            patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((caller,))),
            patch.object(memory_admit, "process_table", return_value=table),
        ):
            _ = self.enterContext(context)
        return target, table, member_scope

    def test_detached_route_matches_the_saved_slice_for_a_step_scope(self) -> None:
        target, table, member_scope = self.scoped_target()
        run, route = target.candidate, target.route
        self.assertEqual(kill_action.stop_route(run, target.recipient, table), route)
        other_slice = member_scope.parent.with_name("builds-detached-other.slice")
        step = kill_action.PROC / str(run.holder_pid)
        _ = (step / "cgroup").write_text(f"0::{other_slice / member_scope.name}\n")
        self.assertIsInstance(kill_action.stop_route(run, target.recipient, table), kill_action.NoStopRoute)

    def test_record_without_the_completion_contract_has_no_kill_offer_and_rejects_a_stale_handle(self) -> None:
        target, _, _ = self.scoped_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        offers, _ = actions.options(15)
        path = target.route.record
        record = worker_lifecycle.read_json_object(path)
        self.assertIs(record.pop("completion_after_slice_cleanup"), True)
        _ = path.write_text(json.dumps(record))

        with (patch.object(detached_runs, "stop_command") as stop,
              patch.object(kill_action, "deliver") as send):
            self.assertEqual(actions.options(15), ({}, []))
            self.assertIsInstance(actions.stop(reference, offers[reference]["handle"], ""), kill_action.NoStopRoute)

        stop.assert_not_called()
        send.assert_not_called()

    def test_launcher_record_without_a_published_supervisor_has_no_kill_offer(self) -> None:
        target, _, _ = self.scoped_target(published=False)
        record = worker_lifecycle.read_json_object(target.route.record)
        self.assertNotIn("supervisor_started", record)
        self.assertNotIn("completion_after_slice_cleanup", record)

        with patch.object(detached_runs, "command_group") as group:
            self.assertEqual(kill_action.KillActions(self.root).options(15), ({}, []))
        group.assert_not_called()

    def test_record_with_the_completion_contract_gets_kill_and_stops_after_the_slice_empties(self) -> None:
        target, _, member_scope = self.scoped_target()
        events = process_stop.CGROUP_ROOT / member_scope.parent.relative_to("/") / "cgroup.events"
        _ = events.write_text("populated 1\n")
        _ = write_config_fixture(self.root / "shutdown.conf", "stop_grace_seconds=2\nkill_grace_seconds=5")
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        with (patch.object(process_stop, "stop_slice", side_effect=process_stop.SliceStopFailed("fixture slice busy"))
                  as stop,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            offers, _ = actions.options(15)
            pending = actions.stop(reference, offers[reference]["handle"], "")
            self.assertEqual(actions.options(15), ({}, [pending]))
            self.assertEqual(self.saved_action(target).result["stop"], kill_action.StopState.STOPPING)
            stop.assert_called_once_with(target.route.run_slice, 2, 5)
            send.assert_not_called()

            _ = events.write_text("populated 0\n")
            self.finish_run(target)
            result = self.resume()

        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))
        stop.assert_called_once()
        send.assert_called_once()

    def launch_live_run(
        self, caller: detached_runs.Caller, *, handle_term: bool = False,
    ) -> tuple[Path, subprocess.Popen[bytes], socket.socket]:
        listener = self.enterContext(socket.socket())
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(TEST_HANG_GUARD_SECS)
        port = cast(tuple[str, int], listener.getsockname())[1]
        command = [sys.executable, "-c", f"""
import signal, socket, sys
with socket.create_connection(("127.0.0.1", int(sys.argv[1])), {TEST_HANG_GUARD_SECS}) as peer:
    def finish(*_):
        peer.sendall(b"t")
        sys.exit(0)
    if sys.argv[2] == "1":
        signal.signal(signal.SIGTERM, finish)
    peer.sendall(b"r")
    peer.recv(1)
""", str(port), str(int(handle_term))]
        popen = subprocess.Popen
        supervisors: list[subprocess.Popen[bytes]] = []

        def supervise(argv: list[str], log: IO[bytes]) -> subprocess.Popen[bytes]:
            # Keep delivery and the independent pipe watcher outside this fixture.
            process = popen([sys.executable, "-c", """
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from scripts.agents import detached_runs
from scripts.lib import process_stop

with (patch.object(detached_runs, "COMMAND_GATE", 'read -r ready || exit 125; shift 2; exec "$@"'),
      patch.object(process_stop, "CGROUP_ROOT", Path(sys.argv[-1]).parent.parent / "cgroups"),
      patch.object(detached_runs, "_complete_stopped_run", return_value="finished: fixture"),
      patch.object(detached_runs, "_completion_sources", return_value=nullcontext()),
      patch.object(detached_runs, "deliver_in_fresh_process", return_value=0),
      patch.object(detached_runs, "_complete_worker_dispatch", return_value=detached_runs.CompletionReplay.HANDLED)):
    sys.exit(detached_runs.main(sys.argv[2:]))
""", *argv[-3:]], stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
            supervisors.append(process)
            self.addCleanup(self.reap_fixture_supervisor, process)
            return process

        try:
            with patch.dict(os.environ, {"BUILDLOG_SCOPE": "0"}):
                path = self.launch_record(caller, command, supervise)
            try:
                connection = listener.accept()[0]
            except TimeoutError as error:
                raise AssertionError(path.with_suffix(".log").read_text()) from error
        finally:
            listener.close()
        (supervisor,) = supervisors
        _ = self.enterContext(connection)
        connection.settimeout(TEST_HANG_GUARD_SECS)
        self.assertEqual(connection.recv(1), b"r", path.with_suffix(".log").read_text())
        return path, supervisor, connection

    def reap_fixture_supervisor(self, process: subprocess.Popen[bytes]) -> None:
        try:
            _ = process.wait(TEST_HANG_GUARD_SECS)
        except subprocess.TimeoutExpired:
            process_group.stop_process_group(process)
            raise

    def test_kill_leaves_the_live_supervisor_to_finish_the_record(self) -> None:
        path, supervisor, connection = self.launch_live_run(
            detached_runs.DirectorCaller("impl3", staff.StaffId("fixture")))
        target, events = self.live_target(path)
        actions = kill_action.KillActions(self.root)
        reference = target.candidate.reference

        def term(_directory: int, run_slice: process_stop.RunSliceIdentity) -> None:
            self.assertEqual(run_slice, target.route.run_slice)
            self.assertEqual(worker_lifecycle.read_json_object(path)["status"], "running")
            process_group.send_process_group(target.route.group, signal.SIGTERM)
            self.assertEqual(connection.recv(1), b"")
            _ = events.write_text("populated 0\n")

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "stop_route", return_value=target.route),
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(process_stop, "_term_slice", side_effect=term) as stopped,
              patch.object(detached_runs, "reap_orphan") as reaped,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as sent):
            offers, _ = actions.options(15)
            _ = actions.stop(reference, offers[reference]["handle"], "")
            self.assertEqual(supervisor.wait(TEST_HANG_GUARD_SECS), 0, path.with_suffix(".log").read_text())
            record = worker_lifecycle.read_json_object(path)
            self.assertEqual((record["status"], record["exit_code"]), ("finished", 128 + signal.SIGTERM))
            self.assertIs(record["completion_pending"], False)
            result = self.resume()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))
        stopped.assert_called_once()
        reaped.assert_not_called()
        sent.assert_called_once()

    def test_outside_kill_lets_the_command_handle_term(self) -> None:
        path, supervisor, connection = self.launch_live_run(
            detached_runs.DirectorCaller("impl3", staff.StaffId("fixture")), handle_term=True)
        queued, events = self.live_target(path)
        route = queued.route
        run = kill_action.OutsideKillCandidate(route.group, route.started, "test.py")
        target = kill_action.OutsideKillTarget(run, route, queued.recipient, queued.starter,
                                               queued.recipient_name, queued.recipient_role)
        actions = kill_action.KillActions(self.root, lambda: {run.reference: target})

        def term(_directory: int, run_slice: process_stop.RunSliceIdentity) -> None:
            self.assertEqual(run_slice, route.run_slice)
            with process_stop.pin_process(process_stop.ProcessLifetime(run.row_pid, run.row_start)) as descriptor:
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            self.assertEqual(connection.recv(1), b"t")
            self.assertEqual(connection.recv(1), b"")
            _ = events.write_text("populated 0\n")

        with (patch.object(kill_action, "live_targets", return_value={}),
              patch.object(kill_action, "outside_stop_route", return_value=route),
              patch.object(detached_runs, "stop_command", wraps=detached_runs.stop_command) as stop,
              patch.object(process_stop, "_term_slice", side_effect=term),
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as sent):
            offers, _ = actions.options(15)
            _ = actions.stop(run.reference, offers[run.reference]["handle"], "")
            self.assertEqual(supervisor.wait(TEST_HANG_GUARD_SECS), 0, path.with_suffix(".log").read_text())
            record = worker_lifecycle.read_json_object(path)
            self.assertEqual((record["status"], record["exit_code"]), ("finished", 0))
            result = self.resume() if not sent.called else actions.options(15)[1][0]
        self.assertEqual(result["stop"], kill_action.StopState.STOPPED)
        stop.assert_called()
        sent.assert_called_once()

    def live_target(self, path: Path) -> tuple[kill_action.KillTarget, Path]:
        raw = worker_lifecycle.read_json_object(path)
        supervisor, started = raw["pid"], raw["supervisor_started"]
        group, command_started = raw["command_group"], raw["command_started"]
        assert isinstance(supervisor, int) and isinstance(started, str)
        assert isinstance(group, int) and isinstance(command_started, str)
        self.assertEqual(process_start.process_start(supervisor), process_start.Started(started))
        self.assertIs(raw.get("completion_after_slice_cleanup"), True)
        unit = "builds-detached-fixture.slice"
        location = Path("/user.slice/builds.slice/builds-detached.slice") / unit
        folder = self.root / "cgroups" / location.relative_to("/")
        folder.mkdir(parents=True)
        identity = folder.stat()
        run_slice: process_stop.RunSliceIdentity = {"unit": unit, "device": identity.st_dev, "inode": identity.st_ino,
                                                    "path": str(location)}
        with patch.object(process_stop, "command_slice", return_value=run_slice):
            process_stop.publish_run_slice(path, process_stop.ScopeUnit(unit, "fixture.scope"))
        events = folder / "cgroup.events"
        _ = events.write_text("populated 1\n")
        _ = write_config_fixture(self.root / "shutdown.conf", "stop_grace_seconds=2\nkill_grace_seconds=5")
        route = kill_action.DetachedCommand(path, group, command_started, supervisor, started, run_slice)
        run = kill_action.KillCandidate(group, command_started, group, command_started, "session", "check", "at")
        target = kill_action.KillTarget(run, route, staff.StaffId(str(raw["staff_id"])), "impl3, worker", "impl3", "worker")
        _ = self.enterContext(patch.object(process_stop, "CGROUP_ROOT", self.root / "cgroups"))
        return target, events

    def unfinished_pass(self, row: staff.StaffRow, parent: staff.StaffRow, instance: str) -> None:
        reply = self.root / "reply.txt"
        _ = reply.write_text(f"DONE {instance}\n")
        completion = progress_history.HookCompletion(turn_end.TurnOutcome.DONE, str(reply), actions_done=True,
            pane_closed=True, role_released=True, recording_done=True, sender_native_id="codex:fixture")
        passed: dict[str, object] = {
            "status": "completed", "instance_id": instance, "hook_completion": asdict(completion),
        }

        def save_completion(run: Path, saved_instance: str, saved: progress_history.HookCompletion) -> None:
            self.assertEqual((run, saved_instance), (self.root, instance))
            passed["hook_completion"] = asdict(saved)

        snapshot = staff.StaffSnapshot((parent, row))
        for context in (
            patch.object(staff, "snapshot", return_value=snapshot),
            patch.object(staff, "local_snapshot", return_value=snapshot),
            patch.object(staff, "record_path", return_value=self.root / "staff.json"),
            patch.object(progress_history, "active_dispatch", return_value=progress_history.ActiveDispatch(instance, ())),
            patch.object(progress_history, "save_hook_completion", side_effect=save_completion),
            patch.object(worker_panes, "worker_pass", return_value=passed),
            patch.object(worker_panes, "worker_run", return_value=self.root),
            patch.object(worker_panes, "parent_run_index", return_value={}),
            patch.object(worker_panes, "observe_worker", return_value=worker_panes.ClosedWorker(row)),
            # The fixture pane has no herdr pane, so a close never reads the machine's live panes.
            patch.object(worker_panes, "close", return_value=worker_panes.Closed()),
            patch.object(scratch, "end", return_value=()),
            patch.object(turn_end, "_prepare_notice", return_value=turn_end.TurnDecision(turn_end.TurnOutcome.DONE,
                                                                                       lambda: None)),
            patch.object(turn_end, "_remove_completed_restart"),
        ):
            _ = self.enterContext(context)

    def test_kill_recovers_the_selected_orphan_once(self) -> None:
        parent = staff.StaffRow(staff.StaffId("director"), "unit", "director", staff.StaffRoot(),
                               current_session=staff.StaffId("director-session"))
        row = staff.StaffRow(staff.StaffId("fixture"), "worker", "impl3", staff.ReportsTo(parent.id),
                            current_session=staff.StaffId("session"))
        self.unfinished_pass(row, parent, "fixture-pass")
        peer, peer_supervisor, peer_connection = self.launch_live_run(
            detached_runs.DirectorCaller("peer", parent.id))
        path, supervisor, connection = self.launch_live_run(detached_runs.PaneCaller(str(self.root), row.name, row.id))
        target, events = self.live_target(path)
        self.assertEqual(worker_lifecycle.read_json_object(path)["pane_dispatch_id"], "fixture-pass")
        peer_connection.shutdown(socket.SHUT_WR)
        self.assertEqual(peer_supervisor.wait(TEST_HANG_GUARD_SECS), 0, peer.with_suffix(".log").read_text())
        self.assertEqual(worker_lifecycle.read_json_object(peer)["status"], "finished")

        actions = kill_action.KillActions(self.root)
        reference = target.candidate.reference
        hold_request = "1" * 32
        with (patch.object(process_stop, "stop_slice", side_effect=process_stop.SliceStopFailed("fixture slice busy")),
              patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "stop_route", return_value=target.route),
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(turn_end, "stop_pass", wraps=turn_end.stop_pass) as complete,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            offers, _ = actions.options(15)
            pending = actions.stop(reference, offers[reference]["handle"], "")
            assert isinstance(pending, dict)
            self.assertEqual(pending["stop"], kill_action.StopState.STOPPING)

            with self.assertRaises(process_stop.SliceStopFailed):
                _ = detached_runs.stop_command(path, "fixture hold", hold_request_id=hold_request)
            # Save the command result before the supervisor can finish the record.
            self.assertEqual(detached_runs.command_record_exit(
                Namespace(record=str(path), pid=target.route.group, exit_code=137)), 0)
            supervisor.kill()
            self.assertEqual(supervisor.wait(TEST_HANG_GUARD_SECS), -signal.SIGKILL)
            detached_runs.reap_orphan(path)
            record = worker_lifecycle.read_json_object(path)
            self.assertEqual(record["status"], "running")
            self.assertEqual(record["pending_hold_stop_requests"], [hold_request])
            self.assertNotIn("finished_at", record)
            complete.assert_not_called()
            send.assert_not_called()

            connection.shutdown(socket.SHUT_WR)
            self.assertEqual(connection.recv(1), b"")
            _ = events.write_text("populated 0\n")
            before = {file.name: file.read_bytes() for file in path.parent.iterdir()
                      if not file.name.lstrip(".").startswith(path.stem + ".")}
            result = self.resume()
            self.assertEqual((result["stop"], result["delivery"]),
                             (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))
            self.assertEqual(detached_runs.reap_orphans(str(self.root)), [])
            after = {file.name: file.read_bytes() for file in path.parent.iterdir()
                     if not file.name.lstrip(".").startswith(path.stem + ".")}
            self.assertIn(peer.name, after)
            self.assertEqual(after, before)
            record = worker_lifecycle.read_json_object(path)
            self.assertEqual((record["status"], record["exit_code"]), ("finished", 137))
            self.assertEqual(record["hold_stop_requests"], [hold_request])
            self.assertNotIn("pending_hold_stop_requests", record)
            self.assertIn("finished_at", record)
            self.assertIs(record.get("completion_pending"), False)

        complete.assert_called_once()
        send.assert_called_once()

    def test_reaper_checks_the_supervisor_start_time_for_the_saved_pid(self) -> None:
        path, supervisor, connection = self.launch_live_run(
            detached_runs.DirectorCaller("impl3", staff.StaffId("fixture")))
        target, events = self.live_target(path)
        self.save_unfinished_request(target, committed=True)
        _ = events.write_text("populated 0\n")
        original = path.read_bytes()
        route = target.route

        with (patch.object(detached_run_process, "supervises", return_value=True),
              patch.object(detached_runs, "_complete_stopped_run", return_value="finished: fixture") as complete):
            detached_runs.reap_orphan(path)
            self.assertEqual(path.read_bytes(), original)
            complete.assert_not_called()
            self.write_run(target, supervisor_started=str(int(route.supervisor_start) - 1))
            self.assertEqual(process_start.process_start(supervisor.pid), process_start.Started(route.supervisor_start))
            detached_runs.reap_orphan(path)
            self.assertEqual(worker_lifecycle.read_json_object(path)["status"], "finished")
            complete.assert_called_once()
        self.assertEqual(process_start.process_start(supervisor.pid), process_start.Started(route.supervisor_start))
        supervisor.kill()
        self.assertEqual(supervisor.wait(TEST_HANG_GUARD_SECS), -signal.SIGKILL)
        connection.shutdown(socket.SHUT_WR)
        self.assertEqual(connection.recv(1), b"")

    def test_legacy_verify_rows_do_not_offer_a_step_scope(self) -> None:
        run = kill_action.KillCandidate(17, "123", 16, "122", "session", "check", "at")
        table = {17: memory_admit.ProcessIdentity(16, "123"), 16: memory_admit.ProcessIdentity(1, "122")}
        with (patch.object(kill_action, "program", side_effect=("cargo", "verify.sh")),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "detached_command") as detached):
            self.assertIsInstance(kill_action.stop_route(run, staff.StaffId("fixture"), table), kill_action.NoStopRoute)
        detached.assert_not_called()

    def test_the_service_uses_the_recorded_session_for_each_staff_kind(self) -> None:
        reading = reservation_reading()
        route = self.detached_target().route
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
                      patch.object(kill_action, "stop_route", return_value=route)):
                    (target,) = kill_action.live_targets().values()
                self.assertEqual(target.recipient, caller.id)
                self.assertEqual(target.starter, f"current name, a {'unit director' if kind == 'unit' else kind}")

    def test_handle_rejects_pid_reuse_and_sends_once_after_the_run_ends(self) -> None:
        target = self.detached_target(step="nextest")
        run = target.candidate
        actions = kill_action.KillActions(self.root)
        at = datetime(2026, 11, 19, 0, 41, 0, 900_000, tzinfo=timezone.utc)
        reason = 'nate killed your run "nextest" (pid 17) from the Builds page at 16:41 PST.'

        class FixtureClock(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return at.astimezone(tz)

        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}) as targets,
              patch.object(kill_action, "datetime", FixtureClock),
              patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}),
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as deliver):
            offers, _ = actions.options(15)
            self.assertEqual(offers[run.reference]["message"], reason)
            handle = offers[run.reference]["handle"]
            reused = replace(run, holder_start="456", row_start="456")
            targets.return_value = {reused.reference: replace(target, candidate=reused)}
            self.assertIsInstance(actions.stop(run.reference, handle, ""), kill_action.NoStopRoute)
            targets.return_value = {run.reference: target}

            def complete(action: kill_action.SavedKillAction) -> bool:
                self.assertEqual((action.route, action.run), (target.route, run))
                self.assertEqual(action.message.partition("\n")[0], reason)
                deliver.assert_not_called()
                self.assertEqual(actions.options(15)[1][0]["stop"], kill_action.StopState.STOPPING)
                saved = self.saved_action(target)
                self.assertIsInstance(saved.stage, kill_action.StopRequested)
                self.assertEqual(actions.options(15, frozenset({run.reference})), ({}, [saved.result]))
                self.finish_run(target, stop_reason=reason, stop_pass_requested=True, stop_notice_id=action.id)
                targets.return_value = {}
                return False

            with patch.object(kill_action, "request_stop", side_effect=complete) as stop:
                result = actions.stop(run.reference, handle, "Use the next slot.")
            stop.assert_called_once()
            deliver.assert_called_once()
            (saved,) = cast(tuple[kill_action.SavedKillAction], deliver.call_args.args)
            self.assertEqual((saved.recipient, saved.message), (target.recipient, reason + "\nUse the next slot."))
            self.assertEqual(result, {"reference": run.reference, "kill_number": 1, "step": "nextest", "pid": 17,
                                      "stop": kill_action.StopState.STOPPED, "delivery": kill_action.DeliveryState.SENT,
                                      "time": "16:41 PST", "recipient_name": "impl3", "recipient_role": "worker"})
            self.assertEqual(actions.options(15), ({}, [result]))
            self.assertIsInstance(actions.stop(run.reference, handle, "Again."), kill_action.NoStopRoute)

    def test_first_read_accepts_no_stop_for_ended_or_pruned_runs(self) -> None:
        at = datetime(2026, 11, 18, 19, 41, 0, 900_000, tzinfo=ZoneInfo("UTC"))

        class FixtureClock(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return at.astimezone(tz)

        for status in ("finished", "pruned"):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=self.root) as folder:
                with patch.object(self, "root", Path(folder)):
                    target = self.detached_target()
                    reference = target.candidate.reference
                    actions = kill_action.KillActions(self.root)
                    with (patch.object(kill_action, "datetime", FixtureClock),
                          patch.object(kill_action, "live_targets", return_value={reference: target}) as targets,
                          patch.object(detached_runs, "stop_command") as stop,
                          patch.object(kill_action, "deliver") as send):
                        offers, _ = actions.options(15)
                        targets.return_value = {}
                        handle = offers[reference]["handle"]
                        self.assertIsInstance(actions.stop(reference, handle, ""), kill_action.NoStopRoute)
                        if status == "pruned":
                            target.route.record.unlink()
                        else:
                            self.finish_run(target)
                        outcome = actions.stop(reference, handle, "")
                        self.assertNotIsInstance(outcome, kill_action.NoStopRoute)
                        self.assertEqual(actions.options(15), ({}, [outcome]))
                    saved = self.saved_action(target)
                    self.assertEqual((saved.result["stop"], saved.result["delivery"]),
                                     (kill_action.StopState.ALREADY_ENDED, kill_action.DeliveryState.PENDING))
                    self.assertIsInstance(saved.stage, kill_action.FinishedStop)
                    stop.assert_not_called()
                    send.assert_not_called()

    def test_completion_during_route_check_reports_already_ended(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        def finish() -> dict[str, kill_action.KillTarget]:
            self.finish_run(target)
            return {}

        with patch.object(kill_action, "live_targets", return_value={reference: target}):
            offers, _ = actions.options(15)
        with (patch.object(kill_action, "live_targets", side_effect=finish),
              patch.object(detached_runs, "stop_command") as stop,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            outcome = actions.stop(reference, offers[reference]["handle"], "")
        saved = self.saved_action(target)
        self.assertEqual(outcome, saved.result)
        self.assertEqual((saved.result["stop"], saved.result["delivery"]),
                         (kill_action.StopState.ALREADY_ENDED, kill_action.DeliveryState.PENDING))
        stop.assert_not_called()
        send.assert_not_called()

    def test_restart_reports_already_ended_if_the_record_is_pruned_before_commit(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        class Restart(BaseException):
            pass

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "request_stop", side_effect=Restart)):
            offers, _ = actions.options(15)
            with self.assertRaises(Restart):
                _ = actions.stop(reference, offers[reference]["handle"], "Wait for release.")
        saved = self.saved_action(target)
        self.assertIsInstance(saved.stage, kill_action.StopRequested)
        self.assertEqual(saved.route, target.route)
        self.assertTrue(saved.message.endswith("\nWait for release."))
        self.finish_run(target)
        with (patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(process_stop, "slice_has_ended", return_value=True)):
            self.assertEqual(detached_runs.prune_finished(target.route.record.parent), [target.route.record.stem])
        with (patch.object(detached_runs, "stop_command") as stop,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            result = self.resume()
        stop.assert_not_called()
        send.assert_not_called()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.ALREADY_ENDED, kill_action.DeliveryState.PENDING))

    def test_prune_during_the_stop_lock_wait_reports_already_ended(self) -> None:
        target = self.detached_target()
        path = target.route.record
        lock_path = path.with_suffix(".scope-stop.lock")
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        flock = fcntl.flock
        _ = write_config_fixture(self.root / "shutdown.conf", "stop_grace_seconds=2\nkill_grace_seconds=5")

        def prune_before_lock(descriptor: int | IO[str], operation: int) -> None:
            fd = descriptor if isinstance(descriptor, int) else descriptor.fileno()
            if lock_path.exists() and os.path.samestat(os.fstat(fd), lock_path.stat()):
                self.finish_run(target)
                self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])
            flock(descriptor, operation)

        with (patch.object(fcntl, "flock", side_effect=prune_before_lock),
              patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(process_stop, "slice_has_ended", return_value=True),
              patch.object(process_stop, "stop_slice") as stop,
              patch.object(kill_action, "deliver") as send):
            offers, _ = actions.options(15)
            outcome = actions.stop(reference, offers[reference]["handle"], "")
        saved = self.saved_action(target)
        self.assertEqual(outcome, saved.result)
        self.assertIsInstance(saved.stage, kill_action.FinishedStop)
        self.assertEqual(saved.result["stop"], kill_action.StopState.ALREADY_ENDED)
        self.assertFalse(path.exists())
        stop.assert_not_called()
        send.assert_not_called()

    def test_long_restart_keeps_the_commit_until_builds_saves_the_notice(self) -> None:
        target = self.detached_target()
        path = target.route.record
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        _ = write_config_fixture(self.root / "shutdown.conf", "stop_grace_seconds=2\nkill_grace_seconds=5")

        class Restart(BaseException):
            pass

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "stop_route", return_value=target.route),
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(process_stop, "slice_has_ended", return_value=False),
              patch.object(process_stop, "stop_slice", side_effect=Restart) as stop):
            offers, _ = actions.options(15)
            with self.assertRaises(Restart):
                _ = actions.stop(reference, offers[reference]["handle"], "Use the next slot.")
        stop.assert_called_once()
        saved = self.saved_action(target)
        self.assertIsInstance(saved.stage, kill_action.StopRequested)
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(record["stop_notice_id"], saved.id)
        self.finish_run(target)
        finished = datetime.now(timezone.utc).replace(microsecond=0)

        def prune_before_send(action: kill_action.SavedKillAction) -> kill_action.DeliveryState:
            self.assertEqual(self.saved_action(target), action)
            self.assertIsInstance(action.stage, kill_action.SendingNotice)
            self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])
            return kill_action.DeliveryState.SENT

        with (patch.object(time, "time", return_value=(finished + timedelta(days=1)).timestamp()),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(process_stop, "slice_has_ended", return_value=True),
              patch.object(detached_runs, "stop_command") as retry,
              patch.object(kill_action, "deliver", side_effect=prune_before_send) as send):
            self.assertEqual(detached_runs.prune_finished(path.parent), [])
            result = self.resume()
        self.assertFalse(path.exists())
        retry.assert_not_called()
        send.assert_called_once()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))

    def test_recovered_notice_requires_a_durable_builds_state(self) -> None:
        target = self.detached_target()
        self.save_unfinished_request(target, committed=True)
        saved = self.saved_action(target)
        record = target.route.record
        self.finish_run(target)
        sync_folder = atomic_file.sync_folder
        failed_syncs = 0

        def fail_sending_notice_sync(folder: Path) -> None:
            nonlocal failed_syncs
            if folder == self.root / ".review-link-build-kill" and isinstance(
                    self.saved_action(target).stage, kill_action.SendingNotice):
                failed_syncs += 1
                raise OSError("fixture directory sync failed")
            sync_folder(folder)

        with (patch.object(atomic_file, "sync_folder", side_effect=fail_sending_notice_sync),
              patch.object(detached_runs, "acknowledge_stop_notice", wraps=detached_runs.acknowledge_stop_notice)
              as acknowledge,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send,
              patch.object(threading.Thread, "start", autospec=True) as start):
            actions = kill_action.KillActions(self.root)
            actions.resume()
            for attempt in range(1, 4):
                self.assertEqual(start.call_count, attempt)
                (worker,) = cast(tuple[threading.Thread], start.call_args.args)
                worker.run()
                if attempt < 3:
                    self.assertEqual(failed_syncs, attempt)
                    self.assertEqual(worker_lifecycle.read_json_object(record)["stop_notice_id"], saved.id)
                    acknowledge.assert_not_called()
                    self.assertEqual(send.call_count, attempt - 1)
                actions.retry_pending()
            self.assertEqual(start.call_count, 3)
        self.assertIsInstance(self.saved_action(target).stage, kill_action.FinishedStop)
        acknowledge.assert_called_once_with(record, saved.id)
        send.assert_called_once()
        self.assertNotIn("stop_notice_id", worker_lifecycle.read_json_object(record))

    def test_the_running_service_retries_acknowledgment_without_sending_again(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        record = target.route.record
        actions = kill_action.KillActions(self.root)
        acknowledge_stop_notice = detached_runs.acknowledge_stop_notice

        def finish_run(action: kill_action.SavedKillAction) -> bool:
            self.finish_run(target, stop_pass_requested=True, stop_notice_id=action.id)
            return True

        def acknowledge_notice(path: Path, notice_id: str) -> None:
            action = self.saved_action(target)
            self.assertEqual((path, notice_id), (record, action.id))
            if isinstance(action.stage, kill_action.SendingNotice):
                send.assert_not_called()
                raise PermissionError("fixture acknowledgment failed")
            self.assertIsInstance(action.stage, kill_action.FinishedStop)
            send.assert_called_once()
            acknowledge_stop_notice(path, notice_id)

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "request_stop", side_effect=finish_run),
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send,
              patch.object(detached_runs, "acknowledge_stop_notice", side_effect=acknowledge_notice) as acknowledge,
              patch.object(threading.Thread, "start", autospec=True) as start):
            offers, _ = actions.options(15)
            result = actions.stop(reference, offers[reference]["handle"], "")
            send.assert_called_once()
            acknowledge.assert_called_once()
            saved = self.saved_action(target)
            self.assertEqual(result, saved.result)
            self.assertEqual(saved.result["delivery"], kill_action.DeliveryState.SENT)
            self.assertEqual(worker_lifecycle.read_json_object(record)["stop_notice_id"], saved.id)
            start.assert_not_called()
            actions.retry_pending()
            start.assert_called_once()
            (worker,) = cast(tuple[threading.Thread], start.call_args.args)
            worker.run()
            actions.retry_pending()
            start.assert_called_once()
        send.assert_called_once()
        self.assertEqual(acknowledge.call_count, 2)
        self.assertNotIn("stop_notice_id", worker_lifecycle.read_json_object(record))

    def test_recovery_releases_the_notice_after_confirmed_abandonment(self) -> None:
        target = self.detached_target()
        self.save_unfinished_request(target, committed=True)
        saved = self.saved_action(target)
        folder = self.root / ".review-link-build-kill"
        action_file = folder / f"{target.candidate.reference}.json"
        _ = action_file.write_text("unreadable action")
        record = target.route.record

        with patch.object(kill_action, "deliver") as send:
            kill_action.KillActions(self.root).resume()
            self.assertEqual(worker_lifecycle.read_json_object(record)["stop_notice_id"], saved.id)
            action_file.unlink()
            kill_action.KillActions(self.root).resume()
        send.assert_not_called()
        raw = worker_lifecycle.read_json_object(record)
        self.assertNotIn("stop_notice_id", raw)
        self.assertEqual(raw["status"], "running")
        self.assertIs(raw["stop_pass_requested"], True)
        self.assertEqual(list(folder.glob("*.notice")), [])

    def save_unfinished_request(self, target: kill_action.KillTarget, *, committed: bool = False) -> None:
        actions = kill_action.KillActions(self.root)
        reference = target.candidate.reference

        def interrupt(action: kill_action.SavedKillAction) -> bool:
            if committed:
                self.write_run(target, stop_reason=action.message.partition("\n")[0], stop_pass_requested=True,
                               stop_notice_id=action.id)
            raise InterruptedError("fixture stopped before the notice")

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(detached_run_process, "supervises", return_value=True),
              patch.object(kill_action, "request_stop", side_effect=interrupt)):
            offers, _ = actions.options(15)
            _ = actions.stop(reference, offers[reference]["handle"], "Use the next slot.")
        self.assertIsInstance(self.saved_action(target).stage, kill_action.StopRequested)

    def test_restart_rejects_a_saved_request_when_the_locked_route_changed(self) -> None:
        target = self.detached_target()
        self.save_unfinished_request(target)
        path = target.route.record
        original = path.read_bytes()
        _ = write_config_fixture(self.root / "shutdown.conf", "stop_grace_seconds=2\nkill_grace_seconds=5")

        def changed_route(*_args: object) -> kill_action.NoStopRoute:
            with path.with_suffix(".scope-stop.lock").open("a") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return kill_action.NoStopRoute()

        with (patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", side_effect=changed_route) as route,
              patch.object(process_stop, "slice_has_ended", return_value=False),
              patch.object(process_stop, "stop_slice") as stop,
              patch.object(kill_action, "deliver") as send):
            result = self.resume()
        route.assert_called_once_with(target.candidate, target.recipient, {})
        stop.assert_not_called()
        send.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.UNCONFIRMED, kill_action.DeliveryState.PENDING))

    def test_restart_finishes_its_saved_commit_without_a_live_route(self) -> None:
        target = self.detached_target()
        self.save_unfinished_request(target, committed=True)
        saved = self.saved_action(target)

        def stop(path: Path, reason: str, *, check: Callable[[], bool], stop_notice_id: str) -> bool:
            self.assertEqual(path, target.route.record)
            self.assertEqual(reason, saved.message.partition("\n")[0])
            self.assertTrue(check())
            send.assert_not_called()
            self.finish_run(target, stop_reason=reason, stop_pass_requested=True, stop_notice_id=stop_notice_id)
            return True

        with (patch.object(detached_runs, "stop_command", side_effect=stop) as requested,
              patch.object(kill_action, "stop_route") as route,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.QUEUED) as send):
            result = self.resume()
        requested.assert_called_once_with(target.route.record, saved.message.partition("\n")[0], check=ANY,
                                          stop_notice_id=saved.id)
        route.assert_not_called()
        send.assert_called_once_with(replace(saved, stage=kill_action.SendingNotice(),
                                             result={**saved.result, "stop": kill_action.StopState.STOPPED}))
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.QUEUED))

    def test_slice_exit_waits_for_the_finished_record_and_reports_retirement_once(self) -> None:
        target = self.detached_target(name="retired", role="producer")
        cgroups = self.root / "cgroups"
        folder = cgroups / target.route.run_slice["path"].lstrip("/")
        folder.mkdir(parents=True)
        identity = folder.stat()
        run_slice: process_stop.RunSliceIdentity = {**target.route.run_slice,
                                                    "device": identity.st_dev, "inode": identity.st_ino}
        target = replace(target, route=replace(target.route, run_slice=run_slice))
        self.write_run(target)
        events = folder / "cgroup.events"
        _ = events.write_text("populated 1\n")
        _ = write_config_fixture(self.root / "shutdown.conf", "stop_grace_seconds=2\nkill_grace_seconds=5")
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        def term(_directory: int, stopped: process_stop.RunSliceIdentity) -> None:
            self.assertEqual(stopped, run_slice)
            record = cast(dict[str, object], json.loads(target.route.record.read_text()))
            self.assertEqual(record.get("stop_reason"), self.saved_action(target).message)
            self.assertIs(record.get("stop_pass_requested"), True)
            _ = events.write_text("populated 0\n")

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(process_stop, "_term_slice", side_effect=term) as send_term,
              patch.object(detached_run_process, "supervises", return_value=True),
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", return_value=target.route),
              patch.object(kill_action, "deliver") as send):
            offers, _ = actions.options(15)
            _ = actions.stop(reference, offers[reference]["handle"], "")
            self.assertTrue(process_stop.slice_has_ended(run_slice))
        send_term.assert_called_once_with(ANY, run_slice)
        send.assert_not_called()
        saved = self.saved_action(target)
        self.assertEqual(saved.result["stop"], kill_action.StopState.STOPPING)
        with patch.object(process_stop, "CGROUP_ROOT", cgroups):
            self.finish_run(target)
        response = subprocess.CompletedProcess(["send.py"], 2, "", "send.py: fixture: retired producer")
        with (patch.object(detached_runs, "stop_command") as retry,
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", return_value=response) as send):
            result = self.resume()
        retry.assert_not_called()
        send.assert_called_once()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.FAILED))

    def test_populated_slice_stays_pending_until_the_record_has_a_completion_stamp(self) -> None:
        target = self.detached_target()
        cgroups = self.root / "cgroups"
        folder = cgroups / Path(target.route.run_slice["path"]).relative_to("/")
        folder.mkdir(parents=True)
        identity = folder.stat()
        run_slice: process_stop.RunSliceIdentity = {**target.route.run_slice,
                                                    "device": identity.st_dev, "inode": identity.st_ino}
        target = replace(target, route=replace(target.route, run_slice=run_slice))
        self.write_run(target)
        events = folder / "cgroup.events"
        _ = events.write_text("populated 1\n")
        _ = write_config_fixture(self.root / "shutdown.conf", "stop_grace_seconds=2\nkill_grace_seconds=5")
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(process_stop, "_term_slice", side_effect=process_stop.SliceStopFailed("fixture stop failed")),
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", return_value=target.route),
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            offers, _ = actions.options(15)
            outcome = actions.stop(reference, offers[reference]["handle"], "")
            pending = self.saved_action(target)
            self.assertEqual(outcome, pending.result)
            self.assertIsInstance(pending.stage, kill_action.StopRequested)
            self.assertEqual((pending.result["stop"], pending.result["delivery"]),
                             (kill_action.StopState.STOPPING, kill_action.DeliveryState.PENDING))
            with (patch.object(time, "time", return_value=time.time() + 3600),
                  patch.object(threading.Thread, "start", autospec=True) as start):
                actions.retry_pending()
                start.assert_called_once()
                (worker,) = cast(tuple[threading.Thread], start.call_args.args)
                worker.run()
                self.assertEqual(actions.options(15), ({}, [pending.result]))
            self.assertFalse(process_stop.slice_has_ended(run_slice))
            send.assert_not_called()

            _ = events.write_text("populated 0\n")
            self.finish_run(target)
            result = self.resume()
            self.assertEqual((result["stop"], result["delivery"]),
                             (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))
            send.assert_called_once()

    def test_finished_record_stays_pending_while_the_saved_slice_is_populated(self) -> None:
        target = self.detached_target()
        self.save_unfinished_request(target, committed=True)
        pending = self.saved_action(target)
        self.finish_run(target)
        actions = kill_action.KillActions(self.root)

        with (patch.object(process_stop, "slice_has_ended", side_effect=(False, True)) as empty,
              patch.object(kill_action, "live_targets", return_value={}),
              patch.object(kill_action, "request_stop") as request,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send,
              patch.object(threading.Thread, "start", autospec=True) as start):
            actions.resume()
            start.assert_called_once()
            (first,) = cast(tuple[threading.Thread], start.call_args.args)
            first.run()

            self.assertEqual(actions.options(15), ({}, [pending.result]))
            self.assertEqual(self.saved_action(target), pending)
            send.assert_not_called()
            empty.assert_called_once_with(target.route.run_slice)

            actions.retry_pending()
            self.assertEqual(start.call_count, 2)
            (second,) = cast(tuple[threading.Thread], start.call_args.args)
            second.run()
            actions.retry_pending()
            self.assertEqual(start.call_count, 2)
            _, (result,) = actions.options(15)
            self.assertEqual(self.saved_action(target).result, result)

        request.assert_not_called()
        send.assert_called_once()
        self.assertEqual(empty.call_args_list, [((target.route.run_slice,), {})] * 2)
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))

    def test_recovery_retries_an_unconfirmed_send_with_the_same_key(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        attempts: list[list[str]] = []

        def stop(action: kill_action.SavedKillAction) -> bool:
            self.assertEqual(action, self.saved_action(target))
            self.finish_run(target, stop_reason=action.message.partition("\n")[0],
                            stop_pass_requested=True, stop_notice_id=action.id)
            return False

        def send(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            attempts.append(argv)
            if len(attempts) == 1:
                return subprocess.CompletedProcess(argv, -signal.SIGTERM, "", "")
            path = target.route.record
            self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])
            return subprocess.CompletedProcess(argv, 0, "KEPT: review", "")

        with (patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", new=send),
              patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "request_stop", side_effect=stop)):
            offers, _ = actions.options(15)
            with patch.object(detached_runs, "acknowledge_stop_notice", side_effect=OSError("fixture write failed")):
                _ = actions.stop(reference, offers[reference]["handle"], "Use the next slot.")
            saved = self.saved_action(target)
            self.assertIsInstance(saved.stage, kill_action.SendingNotice)
            self.assertEqual((saved.result["stop"], saved.result["delivery"]),
                             (kill_action.StopState.STOPPED, kill_action.DeliveryState.PENDING))
            self.assertEqual(len(attempts), 1)
            with (patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
                  patch.object(process_stop, "slice_has_ended", return_value=True)):
                self.assertEqual(detached_runs.prune_finished(target.route.record.parent), [])
                result = self.resume()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.QUEUED))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[0][attempts[0].index("--key") + 1], f"builds-kill:{saved.id}")
        self.assertEqual(attempts[0][attempts[0].index("--text") + 1], saved.message)
        self.assertTrue(saved.message.endswith("\nUse the next slot."))


if __name__ == "__main__":
    _ = unittest.main()

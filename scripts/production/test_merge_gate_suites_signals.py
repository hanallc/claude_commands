"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
from contextlib import ExitStack, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import select
import signal
import shutil
import subprocess
import sys
import tempfile
from typing import Literal, cast, final
from unittest import mock
from scripts.build_queue import admission_records
from scripts.build_queue.queue_place import QueuePlace
from scripts.lib import build_folders
from scripts.lib.process_group import send_process_group
from scripts.production import merge_gate, merge_gate_suites_test_support
from scripts.direct.wait_for_test_state import wait_until
from scripts.production.merge_gate_test_support import gate_suites
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class GateProcessStateTests(unittest.TestCase):
    def test_process_exit_counts_as_stopped_at_stat_read_or_pidfd_event(self) -> None:
        fixture = GateSuitesFixture()
        for initial_state in (FileNotFoundError(), ProcessLookupError(), "1 (fixture) S"):
            status_read = mock.Mock(side_effect=[initial_state, "1 (fixture) S"])
            status = mock.Mock(read_text=status_read)
            opened = mock.Mock(return_value=47)
            closed = mock.Mock()
            kernel = mock.Mock(pidfd_open=opened, close=closed)
            readiness = mock.Mock(select=mock.Mock(return_value=([47], [], [])))
            with self.subTest(initial_state=initial_state), \
                    mock.patch.object(merge_gate_suites_test_support, "Path", return_value=status), \
                    mock.patch.object(merge_gate_suites_test_support, "os", kernel), \
                    mock.patch.object(merge_gate_suites_test_support, "select", readiness):
                fixture.assert_process_stopped(1234)
                status_read.assert_called_once_with()
                self.assertEqual(opened.call_count, int(isinstance(initial_state, str)))
                if isinstance(initial_state, str):
                    closed.assert_called_once_with(47)
                else:
                    closed.assert_not_called()


@final
class MergeGateSignalsTests(GateSuitesFixture):
    def test_recovery_kills_a_term_ignoring_member_after_its_leader_ends(self) -> None:
        self.supply_jobserver(b"Q")
        source = """import subprocess, sys
child = subprocess.Popen([sys.executable, '-c',
    'import os, signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); print(os.getpid(), flush=True); signal.pause()'],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
print(child.stdout.readline(), end='', flush=True)
sys.stdin.read(1)
"""
        leader = subprocess.Popen([sys.executable, "-c", source], env=self.environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        member = 0
        try:
            output = leader.stdout
            assert output is not None
            member = int(cast(str, output.readline()))
            recovery = merge_gate.GateRecovery(self.state / "dead-gate.json", str(self.checkout),
                                               leader.pid, "ended-owner")
            recovery.add_group(leader.pid)
            stdout, stderr = leader.communicate(input="x", timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(leader.returncode, 0, stdout + stderr)
            folder = self.root / "abandoned-gate"
            folder.mkdir()
            self.write(folder / "module-output", "unfinished")
            recovery.folders.append(str(folder))
            _ = self.git(self.checkout, "merge", "--no-edit", self.checkpoint)
            recovery.rollback = self.base
            recovery.rollback_head = self.git(self.checkout, "rev-parse", "HEAD")
            recovery.rollback_source = self.checkpoint
            descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
            try:
                with mock.patch.dict(os.environ, self.environment, clear=True):
                    recovery.tokens.append(recovery.token(descriptor, os.read(descriptor, 1), "held"))
                    recovery.save()
                    recovery.recover()
            finally:
                os.close(descriptor)
            self.assert_process_stopped(member)
            self.assertEqual(self.jobserver_tokens(), b"Q")
            self.assertEqual(list(self.root.glob("abandoned-gate")), [])
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
            self.assertEqual(list(self.state.glob("dead-gate.json")), [])
        finally:
            if leader.poll() is None:
                leader.kill()
            _ = leader.communicate(timeout=TEST_HANG_GUARD_SECS)
            if member:
                try:
                    send_process_group(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        for live_holder in (False, True):
            with self.subTest(live_holder=live_holder), mock.patch.dict(os.environ, self.environment, clear=True):
                with ExitStack() as holder:
                    if live_holder:
                        _ = holder.enter_context(merge_gate.gate_recovery(self.checkout))
                    directory = merge_gate.gate_run_directory(self.checkout)
                    directory.mkdir(parents=True, exist_ok=True)
                    recovery = merge_gate.GateRecovery(directory / "dead-owner.json", str(self.checkout),
                                                       leader.pid, "ended-owner")
                    folder = self.root / "abandoned-gate"
                    folder.mkdir()
                    self.write(folder / "module-output", "unfinished")
                    recovery.folders.append(str(folder))
                    _ = self.git(self.checkout, "merge", "--no-edit", self.checkpoint)
                    merged = self.git(self.checkout, "rev-parse", "HEAD")
                    recovery.rollback = self.base
                    recovery.rollback_head = merged
                    recovery.rollback_source = self.checkpoint
                    descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
                    try:
                        recovery.tokens.append(recovery.token(descriptor, os.read(descriptor, 1), "held"))
                        recovery.save()
                    finally:
                        os.close(descriptor)
                    if live_holder:
                        output = io.StringIO()
                        with redirect_stdout(output):
                            recovery.recover()
                        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), merged)
                        record = cast(dict[str, object], json.loads(recovery.path.read_text()))
                        self.assertEqual(record["rollback"], self.base)
                        self.assertEqual(record["groups"], {})
                        self.assertEqual(record["tokens"], [])
                        self.assertEqual(record["folders"], [])
                        self.assertEqual(len(output.getvalue().splitlines()), 1)
                        self.assertIn("rollback waits for the live gate", output.getvalue())
                    else:
                        with merge_gate.gate_recovery(self.unit):
                            self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assertFalse(folder.exists())
                with merge_gate.gate_recovery(self.checkout):
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                    self.assertFalse(recovery.path.exists())
                self.assertEqual(list(directory.glob("*.json")), [])

    def test_recovery_drops_a_non_fifo_token_without_writing(self) -> None:
        pool = self.root / "token-pool"
        self.write(pool, "original pool")
        recovery = merge_gate.GateRecovery(self.state / "dead-gate.json", str(self.checkout),
                                           0, "ended-owner")
        environment = {**self.environment, "BUILDLOG_JOBSERVER_FIFO": str(pool)}
        with mock.patch.dict(os.environ, environment, clear=True), \
                tempfile.TemporaryDirectory(prefix="merge-gate-", dir="/dev/shm") as folder:
            path = Path(folder)
            self.write(path / "module-output", "unfinished")
            recovery.folders.append(folder)
            with pool.open("rb") as stream:
                recovery.tokens.append(recovery.token(stream.fileno(), b"Q", "held"))
            recovery.save()
            recovery.recover()
            self.assertEqual(recovery.tokens, [])
            self.assertFalse(recovery.path.exists())
            self.assertFalse(path.exists())
            self.assertEqual(pool.read_text(), "original pool")

    def test_fifo_recovery_resolves_full_pools_and_reports_ambiguous_slots(self) -> None:
        self.supply_jobserver(b"")
        with mock.patch.dict(os.environ, self.environment, clear=True):
            descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, descriptor)
            capacity = fcntl.fcntl(descriptor, fcntl.F_GETPIPE_SZ)
            directory = merge_gate.gate_run_directory(self.checkout)
            operations: tuple[tuple[Literal["acquiring", "returning"], bytes], ...] = (
                ("acquiring", b""), ("returning", b"Q"))
            for operation, value in operations:
                for full in (False, True):
                    with self.subTest(operation=operation, full=full):
                        contents = b"Q" * (capacity if full else 1)
                        self.assertEqual(os.write(descriptor, contents), len(contents))
                        recovery = merge_gate.GateRecovery(directory / "dead-owner.json", str(self.checkout),
                                                           os.getpid(), "ended-owner")
                        recovery.tokens.append(recovery.token(descriptor, value, operation))
                        recovery.save()
                        output = io.StringIO()
                        with redirect_stdout(output), merge_gate.gate_recovery(self.checkout):
                            self.assertEqual(os.read(descriptor, capacity), contents)
                        self.assertEqual(recovery.path.exists(), not full)
                        if not full:
                            self.assertIn(f"UNRESOLVED: CPU slot {operation}", output.getvalue())
                            record = cast(dict[str, object], json.loads(recovery.path.read_text()))
                            [token] = cast(list[dict[str, object]], record["tokens"])
                            self.assertEqual(token["operation"], operation)
                            recovery.path.unlink()

    def test_gate_scope_sidecar_names_the_slice_before_the_child_starts(self) -> None:
        sidecar = self.state / "gate.cgroup"
        sidecar.touch()
        record = self.state / "run.json"
        recovery = merge_gate.GateRecovery(record, str(self.checkout), os.getpid(), "fixture")
        with mock.patch.dict(os.environ, self.environment, clear=True):
            configuration = admission_records.read_memory_config()
            reservation = QueuePlace(self.state / "gate", sidecar, configuration=configuration)
            prefix = merge_gate.publish_gate_scope(recovery, reservation, ["--slice=builds.slice", "--"])
        scope = Path(sidecar.read_text().strip())
        self.assertEqual(str(scope), recovery.scope)
        self.assertEqual(scope.parent, Path(self.environment["BUILDLOG_BUILDS_CGROUP"]))
        self.assertEqual(prefix, [f"--slice={scope.name}", "--"])
        self.assertEqual(json.loads(record.read_text())["scope"], str(scope))
        self.assertEqual(sidecar.with_name(build_folders.RECOVERY_MARKER).readlink(), record)

    def test_cpu_wait_repairs_the_last_slot_after_one_bounded_slice(self) -> None:
        self.supply_jobserver(b"Q")
        with mock.patch.dict(os.environ, self.environment, clear=True), merge_gate.shared_cpu_pool() as pool:
            assert isinstance(pool, merge_gate.SharedCPUPool)
            path = merge_gate.gate_run_directory(self.checkout) / "dead-reader.json"
            recovery = merge_gate.GateRecovery(path, str(self.checkout), os.getpid(), "ended-owner")
            recovery.tokens.append(recovery.token(pool.descriptor, b"", "acquiring", available=1))
            recovery.save()
            self.assertEqual(os.read(pool.descriptor, 1), b"Q")
            ready = select.select
            stop_seconds = admission_records.read_memory_config().program_stop_seconds

            def expire_slice(readers: list[int], writers: list[int], errors: list[int],
                             timeout: float | None = None) -> tuple[list[int], list[int], list[int]]:
                if pool.blocking in readers:
                    assert timeout is not None
                    self.assertGreater(timeout, 0)
                    self.assertLessEqual(timeout, stop_seconds)
                    return [], [], []
                return ready(readers, writers, errors, timeout)

            with mock.patch.object(select, "select", side_effect=expire_slice):
                slot = pool.acquire()
            self.assertEqual(slot.token, b"+")
            self.assertFalse(path.exists())
        self.assertEqual(self.jobserver_tokens(), b"+")

    def test_held_nested_lease_keeps_folders_and_slot_without_stopping_the_scan(self) -> None:
        self.supply_jobserver(b"Q")
        with mock.patch.dict(os.environ, self.environment, clear=True):
            directory = merge_gate.gate_run_directory(self.checkout)
            first = merge_gate.GateRecovery(directory / "first.json", str(self.checkout),
                                            os.getpid(), "ended-owner")
            second = merge_gate.GateRecovery(directory / "second.json", str(self.checkout),
                                             os.getpid(), "ended-owner")
            descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, descriptor)
            parent = self.root / "recorded-work"
            parent.mkdir()
            first.folders.append(str(parent))
            first.tokens.append(first.token(descriptor, os.read(descriptor, 1), "held"))
            first.save()
            second.save()
            (parent / build_folders.RECOVERY_MARKER).symlink_to(first.path)
            nested = build_folders.BuildFolderSpec(kind="tool", parent=parent, purpose="nested", suffix_length=0)
            with build_folders.leased_build_folder(nested) as holder:
                output = io.StringIO()
                with redirect_stdout(output), merge_gate.gate_recovery(self.checkout):
                    self.assertIn("UNRESOLVED", output.getvalue())
                    self.assertTrue(holder.is_dir())
                    self.assertTrue(first.path.is_file())
                    self.assertFalse(second.path.exists())
                    self.assertEqual(self.jobserver_tokens(), b"")
            with merge_gate.gate_recovery(self.checkout):
                self.assertFalse(parent.exists())
                self.assertFalse(first.path.exists())
                self.assertEqual(self.jobserver_tokens(), b"Q")

    @gate_suites("hooks")
    def test_both_gate_modes_recover_the_private_run_record(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            root = build_folders.tmpfs_gate_root()
            for machine in (merge_gate.SHARED_MACHINE, merge_gate.WholeMachine()):
                with self.subTest(machine=type(machine).__name__):
                    tool = build_folders.GATE_RECOVERY_TOOL
                    spec = build_folders.BuildFolderSpec(kind="tool", parent=root, purpose=tool, suffix_length=0)
                    with build_folders.leased_build_folder(spec) as folder:
                        work = self.root / "abandoned-work"
                        work.mkdir()
                        self.write(work / "output", "unfinished")
                        recovery = merge_gate.GateRecovery(folder / "run.json", str(self.checkout), os.getpid(),
                                                           "ended-owner", folders=[str(work)])
                        recovery.save()
                    output = io.StringIO()
                    with redirect_stdout(output):
                        run = merge_gate.run_suites(self.checkout, machine=machine, full=True)
                    self.assertTrue(run.passed, output.getvalue())
                    self.assertFalse(folder.exists())
                    self.assertFalse(folder.with_name(folder.name + ".lease.json").exists())
                    self.assertFalse(folder.with_name(folder.name + ".lock").exists())
                    self.assertFalse(work.exists())

    def test_module_timeout_removes_fixture_and_nested_gate_folders(self) -> None:
        parent = self.root / "nested-temporary"
        parent.mkdir()
        self.environment["TMPDIR"] = str(parent)
        record = self.state / "nested-folders.json"
        module = self.checkout / "scripts" / "hooks" / "test_first.py"
        program = f'''import json
from pathlib import Path
import signal
import sys
from contextlib import contextmanager

with open(Path({str(Path(merge_gate.__file__).parents[2])!r}) / "scripts/lib/package_root.py") as package_root:
    exec(package_root.read(), {{"__file__": package_root.name}})
from scripts.lib import build_folders
from scripts.production import merge_gate
from scripts.production.merge_gate_test_support import MergeGateFixture

MergeGateFixture.setUpClass()
allocate = build_folders.temporary_build_folder

@contextmanager
def hold_nested_gate(request):
    with allocate(request) as folder:
        Path({str(record)!r}).write_text(json.dumps([
            str(MergeGateFixture.fixture_root.parent), str(folder)]))
        signal.pause()
        yield folder

build_folders.temporary_build_folder = hold_nested_gate
merge_gate.run_suites(Path.cwd(), full=True)
'''
        helper = self.bin / "nested-gate.py"
        self.write(helper, program)
        shim = self.bin.parent / "scripts/lib/py"
        self.write(module, "import os\n" + f"os.execv({str(shim)!r}, [{str(shim)!r}, {str(helper)!r}])\n")
        result = self.run_gate("--tests-only", "--module-time-limit-s", "10", own_process=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("killed: still running after 10 s, the module time limit", result.stdout)
        paths = cast(list[str], json.loads(record.read_text()))
        self.assertEqual(len(paths), 2)
        for value in paths:
            self.addCleanup(shutil.rmtree, Path(value), True)
        for value in paths:
            path = Path(value)
            self.assertTrue(path.is_relative_to(parent), str(path))
        self.assertEqual(list(parent.iterdir()), [])

    def test_cancellation_sends_term_to_all_jobs_before_reaping(self) -> None:
        jobs: list[merge_gate.RunningGateJob] = []
        for name in ("first", "second"):
            job = merge_gate.start_gate_job(
                "hooks", Path(f"test_{name}.py"),
                [sys.executable, "-c", "import signal; signal.pause()"],
                self.checkout, self.environment, self.state / f"{name}.log")
            self.addCleanup(job.output.close)
            self.addCleanup(merge_gate.kill_job_group, job)
            jobs.append(job)
        sent: list[int] = []
        reap = merge_gate.reap_gate_job

        def send_record(pid: int, number: int) -> None:
            if number == signal.SIGTERM:
                sent.append(pid)
            send_process_group(pid, number)

        def reap_after_all_terms(job: merge_gate.RunningGateJob) -> None:
            self.assertEqual(sent, [item.process.pid for item in jobs])
            reap(job)

        with mock.patch("scripts.production.merge_gate.process_group.send_process_group", side_effect=send_record), \
                mock.patch.object(merge_gate, "reap_gate_job", side_effect=reap_after_all_terms):
            merge_gate.stop_gate_jobs(jobs)
        for job in jobs:
            self.assertIsNotNone(job.process.returncode)

    def assert_gate_folder_removed(self, ending: str) -> None:
        parent = self.root / "gate-temporary"
        parent.mkdir()
        self.environment["TMPDIR"] = str(parent)
        if ending == "failure":
            self.write(self.state / "red-hooks", "red")
        number = 0 if ending == "failure" else signal.Signals[ending].value
        preamble = f'''import os
from pathlib import Path
import sys
def cleanup_event(event, arguments):
    if event != "shutil.rmtree":
        return
    if Path(arguments[0]).name != "merge-gate":
        return
    if {number}:
        os.kill(os.getpid(), {number})

sys.addaudithook(cleanup_event)
'''
        result = self.run_gate(own_process=True, preamble=preamble)
        if number:
            # The entry point converts the default SIGINT exception to status 1.
            expected = 1 if number == signal.SIGINT else -number
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            [folder] = [entry for entry in parent.iterdir() if entry.is_dir()]
            lock = folder.with_name(folder.name + ".lock")
            lease = folder.with_name(folder.name + ".lease.json")
            self.assertEqual(set(parent.iterdir()), {folder, lock, lease})
            record = cast(dict[str, object], json.loads(lease.read_text()))
            self.assertIsInstance(record["pid"], int)
            self.assertEqual(build_folders.build_folder_state(folder), "free")
            recovered = self.run_gate("--tests-only", "--full", own_process=True)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertIn("merge undone", result.stderr + recovered.stdout)
        else:
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("merge undone", result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()
        self.assertEqual(list(parent.iterdir()), [], result.stdout + result.stderr)

    def test_failed_gate_removes_its_folder(self) -> None:
        self.assert_gate_folder_removed("failure")

    def test_sigterm_during_folder_removal_leaves_a_recorded_folder(self) -> None:
        self.assert_gate_folder_removed("SIGTERM")

    def test_sighup_during_folder_removal_leaves_a_recorded_folder(self) -> None:
        self.assert_gate_folder_removed("SIGHUP")

    def test_sigint_during_folder_removal_leaves_a_recorded_folder(self) -> None:
        self.assert_gate_folder_removed("SIGINT")

    @gate_suites("hooks")
    def test_stop_after_launch_reaps_child_before_returning_capacity(self) -> None:
        self.supply_jobserver(b"Q")
        self.write(self.state / "hold-module", "hold")
        launched = self.state / "launched-job"
        preamble = f"""start = merge_gate.start_gate_job
def stop_after_start(*arguments, **options):
    job = start(*arguments, **options)
    if arguments[0] == "hooks":
        from scripts.direct.wait_for_test_state import wait_until
        wait_until((Path(os.environ["MERGE_GATE_TEST_STATE"]) / "module-held").exists,
                   Path(os.environ["MERGE_GATE_TEST_STATE"]), timeout={TEST_HANG_GUARD_SECS})
        Path({str(launched)!r}).write_text(str(job.process.pid))
        os.kill(os.getpid(), signal.SIGTERM)
    return job
merge_gate.start_gate_job = stop_after_start
"""
        result = self.run_gate("--tests-only", own_process=True, preamble=preamble)
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stdout + result.stderr)
        self.assertEqual(len(launched.read_text().splitlines()), 1)
        (self.state / "hold-module").unlink()
        recovered = self.run_gate("--tests-only", "--full", own_process=True)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assert_process_stopped(int(launched.read_text()))
        self.assertEqual(self.jobserver_tokens(), b"Q")
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_interrupt_after_module_starts_reaps_before_the_gate_returns(self) -> None:
        self.supply_jobserver(b"Q")
        self.write(self.state / "hold-module", "hold")
        launched = self.state / "launched-job"
        preamble = f"""import json
from scripts.direct.wait_for_test_state import wait_until

start = merge_gate.start_gate_job
release = merge_gate.SharedCPUSlot.release
launched = Path({str(launched)!r})

def release_after_stop(slot):
    if not slot.returned and launched.exists():
        assert not merge_gate.live_group(int(launched.read_text()))
    release(slot)

def interrupt_after_start(*arguments, **options):
    job = start(*arguments, **options)
    state = Path(os.environ["MERGE_GATE_TEST_STATE"])
    wait_until((state / "module-held").exists, state, timeout={TEST_HANG_GUARD_SECS})
    recovery = merge_gate.ACTIVE_GATE_RECOVERY.get()
    assert str(job.process.pid) in json.loads(recovery.path.read_text())["groups"]
    launched.write_text(str(job.process.pid))
    raise KeyboardInterrupt

merge_gate.SharedCPUSlot.release = release_after_stop
merge_gate.start_gate_job = interrupt_after_start
"""
        result = self.run_gate("--tests-only", own_process=True, preamble=preamble, type_dirs=())
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assert_process_stopped(int(launched.read_text()))
        self.assertEqual(self.jobserver_tokens(), b"Q")
        self.assert_gate_released()

    @gate_suites("hooks")
    def test_unresolved_module_cleanup_keeps_capacity_and_folders(self) -> None:
        self.supply_jobserver(b"Q")
        self.write(self.state / "hold-module", "hold")
        sidecar = self.state / "sidecar-path"
        proof = self.state / "live-group-proof"
        preamble = f"""from contextlib import contextmanager
import json
from scripts.direct.wait_for_test_state import wait_until

reservation = merge_gate.gate_reservation
reap = merge_gate.reap_gate_job
send = merge_gate.process_group.send_process_group
state = Path(os.environ["MERGE_GATE_TEST_STATE"])
attempts = []
held = None

def record_job(job):
    global held
    if job.suite == "hooks":
        wait_until((state / "module-held").exists, state, timeout={TEST_HANG_GUARD_SECS})
        held = job.process.pid
        job.process.returncode = 0
    else:
        reap(job)

def hold_fixture_group(pid, number):
    if pid != held or number == 0:
        send(pid, number)

def unresolved_finish(job, *, deadline=None):
    assert job.process.pid == held and merge_gate.live_group(held)
    attempts.append("finish" if deadline is None else "final")
    raise merge_gate.GateRefused("UNRESOLVED: fixture module did not stop")

@contextmanager
def record_sidecar(*arguments, **options):
    with reservation(*arguments, **options) as place:
        Path({str(sidecar)!r}).write_text(str(place.sidecar))
        try:
            yield place
        finally:
            recovery = merge_gate.ACTIVE_GATE_RECOVERY.get()
            record = json.loads(recovery.path.read_text())
            assert record["groups"][str(held)]
            assert merge_gate.live_group(held)
            assert place.keep_reservation
            assert all(Path(path).is_dir() for path in record["folders"])
            assert [token["operation"] for token in record["tokens"]] == ["held"]
            Path({str(proof)!r}).write_text(json.dumps(attempts))

merge_gate.reap_gate_job = record_job
merge_gate.process_group.send_process_group = hold_fixture_group
merge_gate.kill_job_group = unresolved_finish
merge_gate.gate_reservation = record_sidecar
"""
        try:
            result = self.run_gate("--tests-only", own_process=True, preamble=preamble, type_dirs=())
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("UNRESOLVED: fixture module did not stop", result.stderr)
            self.assertEqual(json.loads(proof.read_text()), ["finish", "final"])
            self.assertEqual(self.jobserver_tokens(), b"")
            self.assertTrue(Path(sidecar.read_text()).is_file())
            [path] = (self.state / "runner" / "active-runs").glob("*/*.json")
            record = cast(dict[str, object], json.loads(path.read_text()))
            folders = [Path(folder) for folder in cast(list[str], record["folders"])]
            self.assertTrue(folders)
            self.assertTrue(all(folder.is_dir() for folder in folders))
        finally:
            module = self.state / "module-held"
            if module.exists():
                try:
                    send_process_group(int(module.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @gate_suites("hooks", "lib")
    def test_stop_with_pending_modules_returns_owned_slot_and_reservation(self) -> None:
        self.supply_jobserver(b"S")
        self.write(self.state / "hold-module", "hold")
        process = subprocess.Popen(
            [*self.gate_command(), "--tests-only", "--repo", str(self.checkout)],
            env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            wait_until(lambda: (self.state / "module-held").exists() or process.poll() is not None,
                       self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
            self.assertTrue((self.state / "module-held").exists())
            self.assertEqual(self.jobserver_tokens(), b"")
            self.assertEqual(len(self.admission.server.reservations), 1)
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, -signal.SIGTERM, output + errors)
            (self.state / "hold-module").unlink()
            recovered = self.run_gate("--tests-only", "--full", own_process=True)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertEqual(self.jobserver_tokens(), b"S")
            self.assert_gate_released()
            self.assert_process_stopped(int((self.state / "module-held").read_text()))
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            held = self.state / "module-held"
            if held.exists():
                try:
                    send_process_group(int(held.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_finished_module_leaves_no_running_child(self) -> None:
        self.write(self.state / "leak-child", "leak")
        result = self.run_gate("--tests-only")
        child = int((self.state / "child-pid").read_text())
        try:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assert_process_stopped(child)
        finally:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_termination_signals_roll_back_and_clean_module_groups(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig):
                child = 0
                (self.state / "module-held").unlink(missing_ok=True)
                self.write(self.state / "hold-module", "hold")
                self.write(self.state / "leak-child", "leak")
                self.write(self.state / "detached-child", "detached")
                # Each signal gets its own pool, so a failed subtest leaves no token for the next.
                pool = self.root / f"jobserver-{sig.name}"
                os.mkfifo(pool)
                self.addCleanup(os.close, os.open(pool, os.O_RDWR | os.O_NONBLOCK))
                self.environment["BUILDLOG_JOBSERVER_FIFO"] = str(pool)
                self.supply_jobserver(b"Q")
                parent = self.root / f"interrupted-{sig.name}"
                parent.mkdir()
                self.environment["TMPDIR"] = str(parent)
                # The supported whole-machine path has no queue-owned handlers.
                preamble = """signal.signal(signal.SIGINT, signal.SIG_DFL)
def whole_gate(repo, type_dirs, module_time_limit_s, **options):
    with merge_gate.shared_cpu_pool() as pool:
        pool.try_acquire()
        return merge_gate.run_suites(repo, type_dirs, module_time_limit_s, machine=merge_gate.WholeMachine())
merge_gate.run_gate_suites = whole_gate
"""
                process = subprocess.Popen(
                    [*self.gate_command(preamble=preamble), "--repo", str(self.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
                     "--unit", "fixture", "--phase", "repair",
                     "--production", "quiet", "--type-dirs", "scripts"], env=self.environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    wait_until(lambda: (self.state / "module-held").exists() or process.poll() is not None,
                               self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
                    if not (self.state / "module-held").exists():
                        self.fail("fixture module never became ready")
                    child = int((self.state / "module-held").read_text())
                    leftover = int((self.state / "child-pid").read_text())
                    fields = Path(f"/proc/{leftover}/stat").read_text().rsplit(")", 1)[1].split()
                    self.assertEqual(fields[2:4], [str(leftover), str(leftover)])
                    self.assertEqual(self.jobserver_tokens(), b"")
                    process.send_signal(sig)
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, -sig, output + errors)
                    self.assert_process_stopped(child)
                    records = list((self.state / "runner/active-runs").glob("*/*.json"))
                    self.assertEqual(len(records), 1)
                    record = cast(dict[str, object], json.loads(records[0].read_text()))
                    self.assertEqual(record["pid"], process.pid)
                    self.assertEqual(record["rollback"], self.base)
                    folders = [Path(folder) for folder in cast(list[str], record["folders"])]
                    self.assertTrue(folders)
                    self.assertTrue(all(folder.is_dir() for folder in folders))
                    (self.state / "hold-module").unlink()
                    (self.state / "leak-child").unlink()
                    if sig == signal.SIGTERM:
                        merged = self.git(self.checkout, "rev-parse", "HEAD")
                        self.write(self.checkout / "later.txt", "an unrelated later commit")
                        later = self.commit(self.checkout, "Add an unrelated later commit")
                        refused = self.run_gate("--tests-only", "--full", own_process=True)
                        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
                        self.assertIn("differs from the recorded merge", refused.stderr)
                        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), later)
                        self.assertTrue(records[0].is_file())
                        _ = self.git(self.checkout, "reset", "--hard", merged)
                    recovered = self.run_gate("--tests-only", "--full", own_process=True)
                    self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                    self.assertIn("merge undone", recovered.stdout)
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                    self.assert_process_stopped(child)
                    self.assert_process_stopped(leftover)
                    self.assertTrue(all(not folder.exists() for folder in folders))
                    self.assertEqual(list((self.state / "runner/active-runs").glob("*/*.json")), [])
                    self.assertEqual(list(parent.iterdir()), [])
                    self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assert_not_pushed()
                finally:
                    if process.poll() is None:
                        process.kill()
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    if child:
                        try:
                            send_process_group(child, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    _ = self.git(self.checkout, "reset", "--keep", self.base)

    def test_signal_during_merge_aborts_pending_merge_and_its_hook(self) -> None:
        ready = self.state / "merge-hook-ready"
        hook = self.checkout / ".git" / "hooks" / "prepare-commit-msg"
        # The pid is published by rename, so the test never reads a created but
        # still empty file.
        self.write(hook, f"#!{sys.executable}\nimport os, signal\nfrom pathlib import Path\n"
                   + f"Path({str(ready) + '.tmp'!r}).write_text(str(os.getpid()))\n"
                   + f"os.replace({str(ready) + '.tmp'!r}, {str(ready)!r})\nsignal.pause()\n")
        hook.chmod(0o755)
        process = subprocess.Popen(
            [*self.gate_command(), "--repo", str(self.checkout), "--merge-branch", "integration",
             "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
             "--unit", "fixture", "--phase", "repair",
             "--production", "quiet"],
            env=self.environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        hook_pid = 0
        try:
            wait_until(lambda: ready.exists() or process.poll() is not None,
                       self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
            if not ready.exists():
                self.fail("merge hook did not become ready")
            hook_pid = int(ready.read_text())
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(process.returncode, -signal.SIGTERM, output + errors)
            hook.unlink()
            recovered = self.run_gate("--tests-only", "--full", own_process=True)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            self.assertIn("merge undone", recovered.stdout)
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
            self.assertFalse((self.checkout / ".git" / "MERGE_HEAD").exists())
            self.assert_process_stopped(hook_pid)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
            self.assert_not_pushed()
        finally:
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            if hook_pid:
                try:
                    os.kill(hook_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    _ = unittest.main()

"""Merge gate suites contracts."""

from __future__ import annotations

import unittest
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import socket
import shutil
import subprocess
import sys
import tempfile
from typing import cast, final
from unittest import mock
from scripts.build_queue import admission_protocol as wire, admission_records, queue_place
from scripts.lib import build_folders, descendants, process_start
from scripts.lib.process_start import Gone, Started, Unreadable, Zombie
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.process_group import send_process_group
from scripts.production import merge_gate, merge_gate_test_support
from scripts.production.gate_modules import GateRefused
from scripts.direct.wait_for_test_state import wait_until
from scripts.production.merge_gate_test_support import gate_suites
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


@final
class GateProcessStateTests(unittest.TestCase):
    @merge_gate_test_support.needs_default_sigint
    def test_library_restores_the_signal_mask_and_raises_the_cleanup_failure(self) -> None:
        incoming = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        failure = GateRefused("fixture group remains unresolved")
        with self.assertRaises(GateRefused) as caught, merge_gate.gate_failure_boundary() as boundary:
            boundary.begin_cleanup()
            boundary.cleanup_failure = failure
            signal.raise_signal(signal.SIGINT)
            self.assertIn(signal.SIGINT, cast(set[signal.Signals], signal.sigpending()))
            raise OSError("fixture context exit failed")
        self.assertIs(caught.exception, failure)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), incoming)

    def test_recovery_keeps_canaries_and_saved_gate_folders(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="gate-recovery-test-")).resolve()
        self.addCleanup(shutil.rmtree, root)
        home = root / "home"
        directory = root / "recovery"
        directory.mkdir()
        recovery = merge_gate.GateRecovery(directory / "dead.json", str(root), 424242, "fixture")
        for location in (home, home / "rust", home / "worktrees", home / ".claude", root / "gate"):
            folder = location / "merge-gate-fixture"
            folder.mkdir(parents=True)
            _ = (folder / "keep").write_text("fixture data")
            recovery.folders.append(str(folder))
        recovery.save()
        with (mock.patch.object(merge_gate, "process_start", return_value=Zombie("dead")),
              mock.patch.object(admission_records, "read_memory_config",
                                return_value=mock.Mock(program_stop_seconds=0)),
              redirect_stderr(io.StringIO()) as error):
            merge_gate.recover_gate_directory(directory)
            merge_gate.recover_gate_directory(directory)
        self.assertEqual(error.getvalue().splitlines(), [
            f"kept for a manual removal list: {folder} (merge_gate, the gate owner ended)"
            for folder in recovery.folders
        ])
        for folder in recovery.folders:
            self.assertEqual((Path(folder) / "keep").read_text(), "fixture data")
        saved = cast(dict[str, object], json.loads(recovery.path.read_text()))
        self.assertEqual(saved["folders"], recovery.folders)
        self.assertIs(saved["finished"], True)

    def test_process_exit_counts_as_stopped_at_stat_read_or_pidfd_event(self) -> None:
        fixture = GateSuitesFixture()
        rows = (
            ("linux", "gone", Gone(), False, True),
            ("linux", "zombie", Zombie("fixture"), False, True),
            ("linux", "pidfd", Started("fixture"), True, True),
            ("linux", "missing stat", Gone(), False, False),
            ("darwin", "gone", Gone(), False, True),
            ("darwin", "zombie", Zombie("fixture"), False, True),
            ("darwin", "kqueue", Started("fixture"), True, True),
            ("darwin", "live", Started("fixture"), True, False),
        )
        for platform, case, state, opened, stopped in rows:
            probe_pid = mock.Mock(side_effect=ProcessLookupError() if case == "gone" else None)
            open_pidfd = mock.Mock(return_value=47)
            close_pidfd = mock.Mock()
            kernel = mock.Mock(kill=probe_pid, pidfd_open=open_pidfd, close=close_pidfd)
            identity = mock.Mock(return_value=state)
            wait_for_pidfd = mock.Mock(return_value=([47], [], []))
            control = mock.Mock(side_effect=[[], [object()] if stopped else []])
            close_queue = mock.Mock()
            queue = mock.Mock(control=control, close=close_queue)
            open_queue = mock.Mock(return_value=queue)
            registered = object()
            event = mock.Mock(return_value=registered)
            readiness = mock.Mock(select=wait_for_pidfd, kqueue=open_queue, kevent=event,
                                  KQ_FILTER_PROC=1, KQ_EV_ADD=2, KQ_EV_ONESHOT=4, KQ_NOTE_EXIT=8)
            clock = mock.Mock(monotonic=mock.Mock(return_value=0))
            with (self.subTest(platform=platform, case=case),
                  mock.patch.object(process_start, "os", kernel),
                  mock.patch.object(process_start, "time", clock),
                  mock.patch.object(merge_gate_test_support, "time", clock),
                  mock.patch.object(process_start, "process_start", identity),
                  mock.patch.object(process_start, "_darwin_process_start", identity),
                  mock.patch.object(process_start, "sys", mock.Mock(platform=platform)),
                  mock.patch.object(process_start, "select", readiness)):
                if stopped:
                    fixture.assert_process_stopped(1234)
                else:
                    reason = "cannot observe gate child" if case == "missing stat" else "gate left running child"
                    with self.assertRaisesRegex(AssertionError, reason):
                        fixture.assert_process_stopped(1234)
                self.assertEqual(probe_pid.call_args_list, [mock.call(1234, 0)] if isinstance(state, Gone) else [])
                self.assertEqual(identity.call_count, 2 if opened else 1)
                if opened and platform == "linux":
                    open_pidfd.assert_called_once_with(1234)
                    wait_for_pidfd.assert_called_once_with([47], [], [], TEST_HANG_GUARD_SECS)
                    close_pidfd.assert_called_once_with(47)
                else:
                    open_pidfd.assert_not_called()
                    wait_for_pidfd.assert_not_called()
                    close_pidfd.assert_not_called()
                if opened and platform == "darwin":
                    open_queue.assert_called_once_with()
                    event.assert_called_once_with(1234, filter=1, flags=6, fflags=8)
                    self.assertEqual(control.call_args_list, [mock.call([registered], 0, 0),
                                                              mock.call(None, 1, TEST_HANG_GUARD_SECS)])
                    close_queue.assert_called_once_with()
                else:
                    open_queue.assert_not_called()


@final
class MergeGateSignalsTests(GateSuitesFixture):
    def abandoned_gate(self, recovery: merge_gate.GateRecovery) -> Path:
        folder = self.root / "gate" / "merge-gate-abandoned"
        folder.mkdir(parents=True, exist_ok=True)
        self.write(folder / "module-output", "unfinished")
        recovery.folders.append(str(folder))
        return folder

    def test_recovery_kills_a_member_that_survives_term_after_its_leader_ends(self) -> None:
        self.supply_jobserver(b"Q")
        source = """import subprocess, sys
child = subprocess.Popen([sys.executable, '-c',
    'import os, signal; print(os.getpid(), flush=True); signal.pause()'],
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
            folder = self.abandoned_gate(recovery)
            _ = self.git(self.checkout, "merge", "--no-edit", self.checkpoint)
            recovery.rollback = self.base
            recovery.rollback_head = self.git(self.checkout, "rev-parse", "HEAD")
            recovery.rollback_source = self.checkpoint
            descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)

            def send_without_term(pid: int, number: int) -> None:
                if number != signal.SIGTERM:
                    send_process_group(pid, number)

            try:
                with (mock.patch.dict(os.environ, self.environment, clear=True),
                      mock.patch("scripts.production.merge_gate.process_group.send_process_group",
                                 side_effect=send_without_term) as sent):
                    recovery.tokens.append(recovery.token(descriptor, os.read(descriptor, 1)))
                    recovery.save()
                    recovery.recover()
                self.assertEqual(sent.call_args_list, [mock.call(leader.pid, signal.SIGTERM),
                                                      mock.call(leader.pid, signal.SIGKILL)])
            finally:
                os.close(descriptor)
            self.assert_process_stopped(member)
            self.assertEqual(self.jobserver_tokens(), b"Q")
            self.assertTrue(folder.is_dir())
            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
            self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
            self.assertTrue(recovery.path.is_file())
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
                    folder = self.abandoned_gate(recovery)
                    _ = self.git(self.checkout, "merge", "--no-edit", self.checkpoint)
                    merged = self.git(self.checkout, "rev-parse", "HEAD")
                    recovery.rollback = self.base
                    recovery.rollback_head = merged
                    recovery.rollback_source = self.checkpoint
                    descriptor = os.open(self.environment["BUILDLOG_JOBSERVER_FIFO"], os.O_RDWR | os.O_NONBLOCK)
                    try:
                        recovery.tokens.append(recovery.token(descriptor, os.read(descriptor, 1)))
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
                        self.assertEqual(record["folders"], [str(folder)])
                        self.assertEqual(len(output.getvalue().splitlines()), 1)
                        self.assertIn("rollback waits for the live gate", output.getvalue())
                    else:
                        with merge_gate.gate_recovery(self.unit):
                            self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assertTrue(folder.is_dir())
                with merge_gate.gate_recovery(self.checkout):
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                    self.assertTrue(recovery.path.is_file())
                self.assertEqual(list(directory.glob("*.json")), [recovery.path])

    def test_recovery_drops_a_non_fifo_token_without_writing(self) -> None:
        pool = self.root / "token-pool"
        self.write(pool, "original pool")
        recovery = merge_gate.GateRecovery(self.state / "dead-gate.json", str(self.checkout),
                                           0, "ended-owner")
        environment = {**self.environment, "BUILDLOG_JOBSERVER_FIFO": str(pool)}
        with mock.patch.dict(os.environ, environment, clear=True):
            path = self.abandoned_gate(recovery)
            with pool.open("rb") as stream:
                recovery.tokens.append(recovery.token(stream.fileno(), b"Q"))
            recovery.save()
            recovery.recover()
            self.assertEqual(recovery.tokens, [])
            self.assertTrue(recovery.path.is_file())
            self.assertTrue(path.is_dir())
            self.assertEqual(pool.read_text(), "original pool")

    def test_module_timeout_removes_fixture_and_nested_gate_folders(self) -> None:
        parent = self.root / "nested-temporary"
        parent.mkdir()
        self.environment["TMPDIR"] = str(parent)
        host = socket.gethostname().partition(".")[0]
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_folders.conf",
                                 f"roots.gate.{host}={parent}\n")
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
        outer, nested = (Path(value).resolve() for value in paths)
        self.assertTrue(outer.is_relative_to(parent), str(outer))
        self.assertTrue(nested.is_relative_to(parent), str(nested))
        self.assertFalse(nested.exists())
        self.assertEqual(list(parent.iterdir()), [])

    def test_cancellation_sends_term_to_all_jobs_before_reaping(self) -> None:
        jobs: list[merge_gate.RunningGateJob] = []
        observe_signals = """from pathlib import Path
import json, signal, sys
numbers = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
def disposition(number):
    current = signal.getsignal(number)
    return 'python-default' if current is signal.default_int_handler else int(current)
ready = Path(sys.argv[1])
temporary = ready.with_suffix('.tmp')
temporary.write_text(json.dumps({
    'dispositions': [disposition(number) for number in numbers],
    'mask': sorted(signal.pthread_sigmask(signal.SIG_BLOCK, [])),
    'ignored': sorted(number for number in signal.valid_signals()
                      if number not in (signal.SIGPIPE, signal.SIGXFSZ)
                      and signal.getsignal(number) == signal.SIG_IGN),
}))
temporary.replace(ready)
signal.pause()
"""
        numbers = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
        dispositions = [int(signal.SIG_IGN) if signal.getsignal(number) == signal.SIG_IGN
                        else "python-default" if number == signal.SIGINT else int(signal.SIG_DFL)
                        for number in numbers]
        ignored = sorted(number for number in signal.valid_signals()
                         if number not in (signal.SIGPIPE, signal.SIGXFSZ)
                         and signal.getsignal(number) == signal.SIG_IGN)
        stop_signals = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
        module, types = merge_gate.GateJobKind.TEST_MODULE, merge_gate.GateJobKind.TYPE_CHECK
        pause = "import signal; signal.pause()"
        rows = (("first", module, pause, ()), ("second", module, pause, ()),
                ("signal-state", module, observe_signals, ()),
                ("module-blocked-term", module, observe_signals, (signal.SIGTERM,)),
                ("types-blocked-term", types, observe_signals, (signal.SIGTERM,)))
        for name, kind, source, blocked in rows:
            with self.subTest(job=name):
                ready = self.state / f"{name}.json"
                mask = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
                try:
                    job = merge_gate.start_gate_job(
                        "hooks", Path(f"test_{name}.py"), [sys.executable, "-c", source, str(ready)],
                        self.checkout, self.environment, self.state / f"{name}.log", kind=kind)
                finally:
                    _ = signal.pthread_sigmask(signal.SIG_SETMASK, mask)
                self.addCleanup(job.output.close)
                self.addCleanup(merge_gate.kill_job_group, job)
                jobs.append(job)
                if source == observe_signals:
                    wait_until(ready.exists, self.state, timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(json.loads(ready.read_text()), {
                        "dispositions": dispositions, "mask": sorted(mask - stop_signals), "ignored": ignored,
                    })
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

    def test_module_launch_preserves_the_inherited_sigint_disposition(self) -> None:
        observation = "import signal; print(signal.getsignal(signal.SIGINT) == signal.SIG_IGN)"
        log = self.state / "inherited-sigint.log"
        received = self.state / "gate-sigint"
        preamble = f"""Path({str(received)!r}).write_text(str(signal.getsignal(signal.SIGINT) == signal.SIG_IGN))
job = merge_gate.start_gate_job(
    "hooks", Path("test_first.py"), [sys.executable, "-c", {observation!r}],
    Path({str(self.checkout)!r}), dict(os.environ), Path({str(log)!r}))
try:
    raise SystemExit(job.process.wait(timeout={TEST_HANG_GUARD_SECS}))
finally:
    merge_gate.kill_job_group(job)
    job.output.close()
"""
        # A non-interactive shell starts its background jobs with SIGINT ignored.
        background = ("/bin/sh", "-c", '"$@" & wait "$!"', "background-gate")
        for command, ignored in ((self.gate_command(preamble=preamble), False),
                                 ([*background, *self.gate_entry_command(preamble=preamble)], True)):
            with self.subTest(ignored=ignored):
                result = subprocess.run(command,
                                        cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(received.read_text(), str(ignored))
                self.assertEqual(log.read_text().strip(), received.read_text())

    def test_gate_removes_its_folder_on_normal_exit(self) -> None:
        parent = self.root / "gate-temporary"
        parent.mkdir()
        with mock.patch.dict(os.environ, self.environment, clear=True):
            with merge_gate.private_gate_recovery(self.checkout, parent) as recovery:
                with merge_gate.gate_temporary(recovery, "merge-gate", parent) as folder:
                    self.assertTrue(folder.is_dir())
                    self.assertEqual(recovery.folders, [str(folder)])
                    self.assertEqual(build_folders.build_folder_state(folder), "held")
                self.assertFalse(folder.exists())
                saved = cast(dict[str, object], json.loads(recovery.path.read_text()))
                self.assertEqual(saved["folders"], [])
        self.assertEqual(list(parent.iterdir()), [])

    def test_private_recovery_retains_an_unreadable_group_without_folders(self) -> None:
        parent = self.root / "gate-unresolved"
        parent.mkdir()
        child = 987654321
        with mock.patch.dict(os.environ, self.environment, clear=True):
            with merge_gate.private_gate_recovery(self.checkout, parent) as recovery:
                with mock.patch.object(merge_gate, "process_start", return_value=Unreadable("fixture identity error")):
                    recovery.add_group(child)
                self.assertEqual(recovery.folders, [])
            original = recovery.path.read_text()
            saved = cast(dict[str, object], json.loads(original))
            self.assertEqual(saved["unresolved_groups"], {str(child): "fixture identity error"})
            for leader_alive in (True, False):
                with (self.subTest(leader_alive=leader_alive),
                      mock.patch.object(merge_gate, "process_start", return_value=Gone()),
                      mock.patch.object(merge_gate, "gate_pid_exists", return_value=leader_alive),
                      mock.patch.object(merge_gate, "gate_group_is_live", return_value=True),
                      self.assertRaisesRegex(GateRefused, "unresolved start identity: fixture identity error")):
                    merge_gate.recover_gate_directory(recovery.path.parent)
                self.assertEqual(recovery.path.read_text(), original)
            with mock.patch.object(merge_gate, "process_start", side_effect=(Gone(), Started("fixture"))):
                with merge_gate.private_gate_recovery(self.checkout, parent):
                    self.assertFalse(recovery.path.exists())

    def test_gate_keeps_its_folder_and_record_when_removal_refuses(self) -> None:
        parent = self.root / "kept-gate"
        parent.mkdir()
        with mock.patch.dict(os.environ, self.environment, clear=True):
            for scope in ("shared", "private"):
                with self.subTest(scope=scope):
                    recovery_scope = (merge_gate.gate_recovery(self.checkout) if scope == "shared"
                                      else merge_gate.private_gate_recovery(self.checkout, parent))
                    with (recovery_scope as recovery,
                          mock.patch.object(build_folders, "remove_build_folder", return_value=False) as remove,
                          redirect_stderr(io.StringIO()) as report):
                        with merge_gate.gate_temporary(recovery, "merge-gate", parent) as folder:
                            self.assertTrue(folder.is_dir())
                    remove.assert_called_once()
                    self.assertTrue(folder.is_dir())
                    saved = cast(dict[str, object], json.loads(recovery.path.read_text()))
                    self.assertEqual(saved["folders"], [str(folder)])
                    self.assertIs(saved["finished"], True)
                    self.assertEqual(report.getvalue().splitlines(), [
                        f"kept for a manual removal list: {folder} (merge_gate, the gate could not remove its folder)"
                    ])

    def test_gate_keeps_the_recovery_entry_when_a_lease_file_stays(self) -> None:
        parent = self.root / "gate-with-lease-file"
        parent.mkdir()
        unlink = os.unlink

        with mock.patch.dict(os.environ, self.environment, clear=True):
            for suffix in (".lease.json", ".lock"):
                with (self.subTest(suffix=suffix),
                      redirect_stderr(io.StringIO()) as report,
                      merge_gate.private_gate_recovery(self.checkout, parent) as recovery,
                      ExitStack() as gate):
                    folder = gate.enter_context(merge_gate.gate_temporary(recovery, "merge-gate", parent))
                    blocked = folder.with_name(folder.name + suffix)
                    self.assertTrue(blocked.is_file())

                    def refuse_lease_file(path: str | bytes | Path, *, dir_fd: int | None = None) -> None:
                        if dir_fd is not None and path == blocked.name:
                            raise PermissionError("fixture refuses the lease file")
                        unlink(path, dir_fd=dir_fd)

                    with (self.assertRaisesRegex(PermissionError, "fixture refuses the lease file"),
                          mock.patch.object(os, "unlink", side_effect=refuse_lease_file)):
                        gate.close()
                    self.assertFalse(folder.exists())
                    self.assertTrue(blocked.is_file())
                    self.assertEqual(recovery.folders, [str(folder)])
                saved = cast(dict[str, object], json.loads(recovery.path.read_text()))
                self.assertEqual(saved["folders"], [str(folder)])
                self.assertIs(saved["finished"], True)
                [notice] = report.getvalue().splitlines()
                self.assertTrue(notice.startswith("kept for a manual removal list: "))
                self.assertIn(str(blocked), notice)

    def test_failed_gate_removes_its_folder(self) -> None:
        parent = self.root / "gate-temporary"
        parent.mkdir()
        self.environment["TMPDIR"] = str(parent)
        host = socket.gethostname().partition(".")[0]
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "build_folders.conf",
                                 f"roots.gate.{host}={parent}\n")
        self.write(self.state / "red-hooks", "red")

        result = self.run_gate(own_process=True)

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(list(parent.iterdir()), [], result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()

    def test_stop_after_launch_reaps_child_before_returning_capacity(self) -> None:
        self.supply_jobserver(b"Q")
        launched = self.state / "launched-job"
        reaped = self.state / "reaped-job"
        released = self.state / "slot-release"
        closed = self.state / "start-pipe-closed"
        deferred = self.state / "second-sigint-pending"
        terminal = self.state / "masked-through-exit"
        timing_save = self.state / "timing-save-after-cleanup"
        reservation_release = self.state / "reservation-release-after-cleanup"
        held = self.state / "module-held"
        late_rows = ("after-fork-late-reap", "after-wait4-late-reap")
        deadline_rows = ("after-fork-deadline", *late_rows)
        rows = (("during-pipe-setup", 1, -signal.SIGKILL), ("after-fork", 1, -signal.SIGKILL),
                ("after-fork-stopped", 1, -signal.SIGKILL), ("after-fork-second-sigint", 1, -signal.SIGKILL),
                ("before-registration", 1, -signal.SIGKILL), ("before-running", 1, -signal.SIGTERM),
                ("after-launch", -signal.SIGTERM, None),
                *((point, 1, None) for point in deadline_rows))
        for point, expected, child_exit in rows:
            with self.subTest(point=point):
                for marker in (held, launched, reaped, released, closed, deferred, terminal,
                               timing_save, reservation_release):
                    marker.unlink(missing_ok=True)
                self.write(self.state / "hold-module", "hold")
                preamble = f"""import atexit
import errno
import threading
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch
from scripts.build_queue import queue_place

launched_processes = []
descriptors = []
cleanup_signals = []
point = {point!r}
late_reap = point in {late_rows!r}
passes_deadline = point in {deadline_rows!r}
release = merge_gate.SharedCPUSlot.release
fork = merge_gate.subprocess.Popen._execute_child
waitpid = os.waitpid
wait4 = os.wait4
monotonic = merge_gate.time.monotonic
late_offset = 0.0
send_group = merge_gate.process_group.send_process_group
pipe = os.pipe

def child_reaped(process):
    try:
        waitpid(process.pid, os.WNOHANG)
    except ChildProcessError:
        return True
    # A live child or an unreaped zombie means cleanup did not reap the child.
    return False

def remember_launch(process):
    launched_processes.append(process)
    Path({str(launched)!r}).write_text(str(process.pid))
    def report_reap():
        if point == "after-fork-deadline":
            waitpid(process.pid, 0)
            return
        if child_reaped(process):
            Path({str(reaped)!r}).write_text(str(process.returncode))
    atexit.register(report_reap)

def release_after_reap(slot):
    if launched_processes and not slot.returned:
        process, = launched_processes
        assert child_reaped(process), f"the gate returned the slot before reaping {{process.pid}}"
        if point.startswith("after-fork"):
            assert cleanup_signals == [signal.SIGKILL], cleanup_signals
        if point == "during-pipe-setup":
            assert len(descriptors) == 2, descriptors
            for descriptor in descriptors:
                try:
                    os.fstat(descriptor)
                except OSError as error:
                    assert error.errno == errno.EBADF
                else:
                    raise AssertionError(f"the start pipe kept descriptor {{descriptor}}")
            Path({str(closed)!r}).write_text(str(len(descriptors)))
        Path({str(released)!r}).write_text(str(process.pid))
    release(slot)

def observe_fork(process, *arguments, **options):
    fork(process, *arguments, **options)
    remember_launch(process)
    if point == "after-fork-stopped":
        os.kill(process.pid, signal.SIGSTOP)
        pid, status = waitpid(process.pid, os.WUNTRACED)
        assert pid == process.pid and os.WIFSTOPPED(status), (pid, status)
    if point.startswith("after-fork"):
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)

def observe_group_signal(pid, number):
    if launched_processes and launched_processes[0].pid == pid:
        cleanup_signals.append(number)
    return send_group(pid, number)

def request_second_sigint():
    if not Path({str(deferred)!r}).exists():
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)
        assert signal.SIGINT in signal.sigpending()
        Path({str(deferred)!r}).touch()

def observe_waitpid(pid, options):
    global late_offset
    if launched_processes and launched_processes[0].pid == pid and point.startswith("after-fork"):
        assert options & os.WNOHANG, "launch cleanup must use a nonblocking reap"
        if passes_deadline or point == "after-fork-second-sigint":
            request_second_sigint()
        if point == "after-fork-deadline":
            return 0, 0
        result = waitpid(pid, options)
        if late_reap and result[0]:
            late_offset = 1.0
        return result
    return waitpid(pid, options)

def observe_wait4(pid, options):
    global late_offset
    result = wait4(pid, options)
    if point == "after-wait4-late-reap" and launched_processes and launched_processes[0].pid == pid:
        assert options & os.WNOHANG, "launch cleanup must use a nonblocking reap"
        request_second_sigint()
        if result[0]:
            late_offset = 1.0
    return result

def interrupt_during_pipe_setup():
    opened = pipe()
    if not descriptors:
        descriptors.extend(opened)
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)
    return opened

def cancel_before_registration(suite, path, process, *arguments, **options):
    remember_launch(process)
    raise KeyboardInterrupt

start = merge_gate.start_gate_job
def stop_after_start(*arguments, **options):
    job = start(*arguments, **options)
    if arguments[0] == "hooks":
        from scripts.direct.wait_for_test_state import wait_until
        wait_until(Path({str(held)!r}).exists,
                   Path(os.environ["MERGE_GATE_TEST_STATE"]), timeout={TEST_HANG_GUARD_SECS})
        remember_launch(job.process)
        number = signal.SIGTERM if point == "after-launch" else signal.SIGINT
        signal.pthread_kill(threading.get_ident(), number)
        raise AssertionError("the signal did not stop the gate before registration in running")
    return job

def stop_during_start(*arguments, **options):
    with patch.object(merge_gate.subprocess.Popen, "_execute_child", new=observe_fork):
        if point == "during-pipe-setup":
            with patch.object(os, "pipe", new=interrupt_during_pipe_setup):
                return start(*arguments, **options)
        return start(*arguments, **options)

merge_gate.SharedCPUSlot.release = release_after_reap
merge_gate.process_group.send_process_group = observe_group_signal
os.waitpid = observe_waitpid
os.wait4 = observe_wait4
if passes_deadline:
    config = merge_gate.admission_records.read_memory_config()
    grace = 0.2 if not late_reap else 2.0
    merge_gate.admission_records.read_memory_config = lambda **options: replace(config, program_stop_seconds=grace)
    merge_gate.time.monotonic = lambda: monotonic() + late_offset * 3
    descriptors = []
    open_pool = merge_gate.shared_cpu_pool
    release_place = queue_place.release_place
    report_folder = merge_gate.report_retained_gate_folder

    @contextmanager
    def observe_pool():
        with open_pool() as pool:
            descriptors.extend((pool.descriptor, pool.blocking))
            yield pool

    def observe_reservation_release(*args, **kwargs):
        Path({str(reservation_release)!r}).touch()
        return release_place(*args, **kwargs)

    def observe_timing_save(*args, **kwargs):
        Path({str(timing_save)!r}).touch()
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)

    def observe_retained_folder(*args, **kwargs):
        assert signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, set())
        return report_folder(*args, **kwargs)

    @atexit.register
    def observe_exit():
        if late_reap:
            assert late_offset == 1.0, "the reap did not cross the deadline"
        assert len(descriptors) == 2, descriptors
        for descriptor in descriptors:
            os.fstat(descriptor)
        assert signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, set())
        Path({str(terminal)!r}).touch()

    merge_gate.shared_cpu_pool = observe_pool
    queue_place.release_place = observe_reservation_release
    merge_gate.save_measures = observe_timing_save
    merge_gate.report_retained_gate_folder = observe_retained_folder
if point == "after-fork-deadline":
    check_deadline = merge_gate.pause_gate_cleanup
    deadlines = set()
    def observe_deadline(deadline):
        deadlines.add(deadline)
        assert len(deadlines) == 1, deadlines
        assert deadline - merge_gate.time.monotonic() <= 0.2, deadline
        return check_deadline(deadline)
    merge_gate.pause_gate_cleanup = observe_deadline
if point == "before-registration":
    merge_gate.RunningGateJob = cancel_before_registration
elif point == "during-pipe-setup" or point.startswith("after-fork"):
    merge_gate.start_gate_job = stop_during_start
else:
    merge_gate.start_gate_job = stop_after_start
"""
                try:
                    route = () if point == "after-wait4-late-reap" else ("--tests-only",)
                    result = self.run_gate(*route, "--full", own_process=True, preamble=preamble)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertEqual(len(launched.read_text().splitlines()), 1)
                    pid = int(launched.read_text())
                    if child_exit is not None:
                        self.assertNotIn("Traceback (most recent call last)", result.stderr)
                        self.assertTrue(reaped.is_file(), result.stdout + result.stderr)
                        self.assertEqual(int(reaped.read_text()), child_exit)
                        self.assertEqual(int(released.read_text()), pid)
                        self.assert_process_stopped(pid)
                    if point == "during-pipe-setup":
                        self.assertEqual(closed.read_text(), "2")
                    if point == "after-fork-second-sigint" or point in deadline_rows:
                        self.assertTrue(deferred.is_file())
                    if point in deadline_rows:
                        self.assertIn(f"REFUSED: cannot stop and reap gate groups [{pid}]", result.stderr)
                        self.assertFalse(released.exists())
                        self.assertFalse(timing_save.exists())
                        self.assertFalse(reservation_release.exists())
                        self.assertTrue(terminal.is_file(), result.stderr)
                        self.assertEqual(self.jobserver_tokens(), b"")
                        directory = Path(self.environment["MERGE_GATE_STATE_DIR"]) / "active-runs"
                        records = [cast(dict[str, object], json.loads(record.read_text()))
                                   for record in directory.glob("*/*.json")]
                        if route:
                            [saved] = [record for record in records if record.get("unresolved_groups")]
                            self.assertEqual(saved["unresolved_groups"], {str(pid): "process start not read"})
                        else:
                            [saved] = [record for record in records if record.get("groups")]
                            self.assertIn(str(pid), cast(dict[str, str], saved["groups"]))
                            self.assertEqual(saved["rollback"], self.base)
                            self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), saved["rollback_head"])
                            self.assertNotEqual(saved["rollback_head"], self.base)
                        self.assertTrue(saved["tokens"])
                    if point in ("during-pipe-setup", "before-registration") or point.startswith("after-fork"):
                        self.assertFalse(held.exists())
                    (self.state / "hold-module").unlink()
                    recovered = self.run_gate("--tests-only", "--full", own_process=True)
                    self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                    if not route:
                        self.assertIn("merge undone", recovered.stdout)
                        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                        self.assert_not_pushed()
                    self.assert_process_stopped(pid)
                    self.assertEqual(self.jobserver_tokens(), b"Q")
                    self.assert_gate_released()
                finally:
                    if (point in ("after-fork-stopped", *deadline_rows)
                            and launched.exists() and not reaped.exists()):
                        try:
                            send_process_group(int(launched.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_later_library_gate_releases_capacity_after_a_cleanup_deadline(self) -> None:
        self.supply_jobserver(b"Q")
        descriptors: list[int] = []
        open_pool = merge_gate.shared_cpu_pool
        start_job = merge_gate.start_gate_job
        release_place = queue_place.release_place

        @contextmanager
        def observe_pool() -> Generator[merge_gate.SharedCPUPool | merge_gate.NoSharedCPU]:
            with open_pool() as pool:
                assert isinstance(pool, merge_gate.SharedCPUPool)
                descriptors.extend((pool.descriptor, pool.blocking))
                yield pool

        def cancel_after_start(suite: str, path: Path, argv: Sequence[str], repo: Path,
                               environment: dict[str, str], log: Path, *,
                               kind: merge_gate.GateJobKind = merge_gate.GateJobKind.TEST_MODULE
                               ) -> merge_gate.RunningGateJob:
            _ = start_job(suite, path, argv, repo, environment, log, kind=kind)
            raise GateRefused("fixture stops scheduling")

        with mock.patch.dict(os.environ, self.environment, clear=True), ExitStack() as cleanup:
            _ = cleanup.callback(merge_gate.recover_retained_gate_runs)
            with (mock.patch.object(merge_gate, "shared_cpu_pool", side_effect=observe_pool),
                  mock.patch.object(merge_gate, "start_gate_job", side_effect=cancel_after_start),
                  mock.patch.object(merge_gate.GateStopDeadline, "start",
                                    return_value=merge_gate.GateStopDeadline(0, 0)),
                  self.assertRaisesRegex(GateRefused, "cannot stop and reap gate groups")):
                _ = merge_gate.run_suites(self.checkout, full=True)
            self.assertEqual(len(descriptors), 2)
            (episode, record), = self.admission.server.reservations.items()
            self.assertEqual(self.jobserver_tokens(), b"")

            refuse = True

            def refuse_first_release(reservation: str, status: int, timing: wire.CleanupTiming) -> wire.Ok | wire.Refused:
                nonlocal refuse
                if refuse:
                    refuse = False
                    return wire.Refused("fixture release refused")
                return release_place(reservation, status, timing)

            with (mock.patch.object(queue_place, "release_place", side_effect=refuse_first_release) as release,
                  mock.patch.object(merge_gate, "gate_reservation",
                                    side_effect=AssertionError("admission preceded recovery")) as admit):
                with self.assertRaisesRegex(GateRefused, "fixture release refused"):
                    _ = merge_gate.run_suites(self.checkout, full=True)
                self.assertIn(episode, self.admission.server.reservations)
                for descriptor in descriptors:
                    _ = os.fstat(descriptor)
                merge_gate.recover_retained_gate_runs()
                admit.assert_not_called()
                self.assertEqual(release.call_args_list, [mock.call(record.reservation_id, 1, mock.ANY)] * 2)

            for descriptor in descriptors:
                with self.assertRaises(OSError):
                    _ = os.fstat(descriptor)
            self.assertEqual(self.jobserver_tokens(), b"Q")
            self.assert_gate_released()

    def test_type_check_stops_its_descendant_when_the_gate_dies(self) -> None:
        launched = self.state / "launched-type-job"
        ready = self.state / "type-descendant"
        checker = """import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])
child.wait()
"""
        descendant = """import os, signal, sys
from pathlib import Path
ready = Path(sys.argv[1])
temporary = ready.with_suffix('.tmp')
temporary.write_text(str(os.getpid()))
temporary.replace(ready)
signal.pause()
"""
        preamble = f"""from scripts.direct.wait_for_test_state import wait_until
job = merge_gate.start_gate_job(
    "types", Path("basedpyright"), [sys.executable, "-c", {checker!r}, {descendant!r}, {str(ready)!r}],
    Path({str(self.checkout)!r}), dict(os.environ), Path({str(self.state / "types.log")!r}),
    kind=merge_gate.GateJobKind.TYPE_CHECK)
Path({str(launched)!r}).write_text(str(job.process.pid))
wait_until(Path({str(ready)!r}).exists, Path({str(self.state)!r}), timeout={TEST_HANG_GUARD_SECS})
os.kill(os.getpid(), signal.SIGKILL)
"""
        try:
            result = self.run_gate("--tests-only", own_process=True, preamble=preamble)
            self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
            self.assert_process_stopped(int(ready.read_text()))
        finally:
            if launched.exists():
                try:
                    send_process_group(int(launched.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @gate_suites("hooks", "lib")
    def test_stop_with_pending_modules_returns_owned_slot_and_reservation(self) -> None:
        self.assertTrue(self.fixture_root.is_relative_to(Path("/tmp").resolve()))
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
        active_runs = self.state / "runner/active-runs"
        detached = descendants.descendant_adoption_available()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with self.subTest(sig=sig):
                child = 0
                previous_records = set(active_runs.glob("*/*.json"))
                (self.state / "module-held").unlink(missing_ok=True)
                self.write(self.state / "hold-module", "hold")
                self.write(self.state / "leak-child", "leak")
                if detached:
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
                preamble = """def whole_gate(repo, module_time_limit_s, **options):
    with merge_gate.shared_cpu_pool() as pool:
        pool.try_acquire()
        return merge_gate.run_suites(repo, module_time_limit_s=module_time_limit_s, machine=merge_gate.WholeMachine())
merge_gate.run_gate_suites = whole_gate
"""
                process = subprocess.Popen(
                    [*self.gate_command(preamble=preamble), "--repo", str(self.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint, "--previous-checkpoint", self.base,
                     "--unit", "fixture", "--phase", "repair",
                     "--production", "quiet"], env=self.environment, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    wait_until(lambda: (self.state / "module-held").exists() or process.poll() is not None,
                               self.state, timeout=TEST_HANG_GUARD_SECS, processes=(process,))
                    if not (self.state / "module-held").exists():
                        self.fail("fixture module never became ready")
                    child = int((self.state / "module-held").read_text())
                    leftover = int((self.state / "child-pid").read_text())
                    child_group = leftover if detached else child
                    self.assertEqual(os.getpgid(leftover), child_group)
                    self.assertEqual(os.getsid(leftover), child_group)
                    self.assertEqual(self.jobserver_tokens(), b"")
                    process.send_signal(sig)
                    output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, 1 if sig == signal.SIGINT else -sig, output + errors)
                    self.assert_process_stopped(child)
                    records = set(active_runs.glob("*/*.json"))
                    self.assertLessEqual(previous_records, records)
                    records = records - previous_records
                    self.assertEqual(len(records), 1)
                    record_path, = records
                    record = cast(dict[str, object], json.loads(record_path.read_text()))
                    self.assertEqual(record["pid"], process.pid)
                    self.assertEqual(record["rollback"], self.base)
                    folders = [Path(folder) for folder in cast(list[str], record["folders"])]
                    if sig == signal.SIGINT:
                        self.assertEqual(folders, [])
                        self.assertEqual(list(parent.iterdir()), [])
                    else:
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
                        self.assertTrue(record_path.is_file())
                        _ = self.git(self.checkout, "reset", "--hard", merged)
                    recovered = self.run_gate("--tests-only", "--full", own_process=True)
                    self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                    self.assertIn("merge undone", recovered.stdout)
                    self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
                    self.assert_process_stopped(child)
                    self.assert_process_stopped(leftover)
                    if sig == signal.SIGINT:
                        self.assertFalse(record_path.exists())
                    else:
                        self.assertTrue(all(folder.is_dir() for folder in folders))
                        saved = cast(dict[str, object], json.loads(record_path.read_text()))
                        self.assertEqual(saved["folders"], record["folders"])
                        self.assertIs(saved["finished"], True)
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

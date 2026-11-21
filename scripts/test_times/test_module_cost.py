"""Test process counts and module costs with private test modules."""
from __future__ import annotations

from scripts.lib.checkout_test_support import copy_test_runner

from contextlib import ExitStack, redirect_stderr, redirect_stdout, suppress
import ctypes
import errno
import fcntl
import io
import os
from pathlib import Path
from queue import Queue
import select
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import cast
import unittest
from unittest import mock

from scripts.lib.descendants import adopt_descendants
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.test_times import module_cost, task_cpu


class ModuleCostTests(unittest.TestCase):
    def test_reads_task_ids_without_decoding_the_task_name(self) -> None:
        samples = [
            (b"Name:\tworker\nTgid:\t401\nPid:\t402\nPPid:\t202\n", (401, 202)),
            (b"Name:\tworker\xff\nTgid:\t503\nPid:\t504\nPPid:\t303\n", (503, 303)),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "status"
            for contents, expected in samples:
                with self.subTest(contents=contents):
                    _ = status.write_bytes(contents)
                    with mock.patch.object(task_cpu, "Path", return_value=status):
                        self.assertEqual(task_cpu.task_ids(402), expected)

    def test_groups_interpreter_commands_by_the_script_or_module(self) -> None:
        samples = [
            (["python3", "-m", "unittest", "discover", "-s", "a"], "python3 -m unittest"),
            (["python3", "-W", "ignore", "-m", "pkg.mod", "x"], "python3 -m pkg.mod"),
            (["python3", "-X", "dev", "heavy.py"], "python3 heavy.py"),
            (["python3", "-Ic", "x"], "python3 -c"),
            (["bash", "-lc", "x"], "bash -c"),
            (["bash", "-o", "pipefail", "run.sh"], "bash run.sh"),
            (["bash", "-euo", "pipefail", "-c", "x"], "bash -c"),
            (["git", "status"], "git"),
            (["pypy3", "--", "-script.py"], "pypy3 -script.py"),
            (["zsh", "-O", "option", "run.sh"], "zsh run.sh"),
            (["python3", "--verbose", "heavy.py"], "python3 heavy.py"),
            (["node", "run.js"], "node"),
        ]
        for arguments, expected in samples:
            with self.subTest(arguments=arguments):
                self.assertEqual(task_cpu.command_name(arguments), expected)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_command_leftover_limit_names_and_stops_a_live_orphan(self) -> None:
        _ = adopt_descendants()
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            script = tree / "paused_orphan.py"
            pid_file = tree / "orphan.pid"
            _ = script.write_text(
                "import os, signal\nfrom pathlib import Path\n"
                + "reader, writer = os.pipe()\n"
                + "if os.fork():\n    os.read(reader, 1)\n    os._exit(0)\n"
                + "os.setsid()\n"
                + f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
                + "os.write(writer, b'1')\nsignal.pause()\n", encoding="utf-8")
            started = time.monotonic()
            with self.assertRaises(task_cpu.LeftoverProcesses) as failure:
                _ = task_cpu.measure([sys.executable, str(script)], cwd=tree,
                                     environment=dict(os.environ), timeout_s=60)
            self.assertLess(time.monotonic() - started, 10)
            pid = int(pid_file.read_text())
            self.assertIn(f"left processes after the test ended: {pid} ({sys.executable} {script})",
                          str(failure.exception))
            try:
                state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                state = "Z"
            self.assertEqual(state, "Z")

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_trace_deadline_does_not_allow_a_leftover_to_finish_late(self) -> None:
        _ = adopt_descendants()
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            script = tree / "late_exit.py"
            _ = script.write_text(
                "import os, time\n"
                + "reader, writer = os.pipe()\n"
                + "if os.fork():\n    os.read(reader, 1)\n    os._exit(0)\n"
                + "os.write(writer, b'1')\n"
                + "time.sleep(1.25)\n", encoding="utf-8")
            with self.assertRaisesRegex(task_cpu.MeasurementTimeout, "timed out after 0.75 s"):
                _ = task_cpu.measure([sys.executable, str(script)], cwd=tree,
                                     environment=dict(os.environ), timeout_s=0.75)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_cleanup_stops_a_thread_group_after_its_leader_exits(self) -> None:
        script = """\
import ctypes, os, signal, threading
from pathlib import Path

def worker():
    stat = Path(f'/proc/{os.getpid()}/stat')
    while stat.read_text().rsplit(')', 1)[1].split()[0] != 'Z':
        pass
    print('ready', flush=True)
    signal.pause()

threading.Thread(target=worker).start()
ctypes.CDLL(None).pthread_exit(None)
"""
        with subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True) as child:
            descriptor = os.pidfd_open(child.pid)
            try:
                stream = child.stdout
                assert stream is not None
                ready = select.poll()
                ready.register(stream, select.POLLIN)
                self.assertTrue(ready.poll(int(TEST_HANG_GUARD_SECS * 1000)))
                self.assertEqual(stream.readline(), "ready\n")
                pending = task_cpu.stop_pending()
                self.assertTrue(any(name.startswith(f"{child.pid} (") for name in pending), pending)
                ended = select.poll()
                ended.register(descriptor, select.POLLIN)
                self.assertTrue(ended.poll(0), "The thread group is still live")
            finally:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                os.close(descriptor)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_cleanup_waits_when_a_parent_retains_a_zombie(self) -> None:
        script = """\
import os, sys
pid = os.fork()
if not pid:
    os._exit(0)
os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
print(pid, flush=True)
sys.stdin.read(1)
os.waitpid(pid, 0)
"""
        with subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True) as child:
            assert child.stdin is not None and child.stdout is not None
            try:
                ready = select.poll()
                ready.register(child.stdout, select.POLLIN)
                self.assertTrue(ready.poll(int(TEST_HANG_GUARD_SECS * 1000)))
                zombie = int(os.read(child.stdout.fileno(), 32))
                self.assertEqual(task_cpu.process_location(zombie).state, "Z")
                scan = task_cpu.pending_processes
                scans = 0

                def count_scan(root_pid: int) -> dict[int, task_cpu.ProcessLocation]:
                    nonlocal scans
                    scans += 1
                    self.assertLessEqual(scans, 3, "Cleanup continued to rescan the same zombie")
                    return scan(root_pid)

                with (mock.patch.object(task_cpu, "pending_processes", side_effect=count_scan),
                      mock.patch.object(signal, "pidfd_send_signal", return_value=None),
                      mock.patch.object(task_cpu, "CLEANUP_LIMIT_S", 0.25)):
                    pending = task_cpu.stop_pending()
                self.assertTrue(any(name.startswith(f"{child.pid} (") for name in pending), pending)
                self.assertFalse(any(name.startswith(f"{zombie} (") for name in pending), pending)
            finally:
                child.stdin.close()

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_stage_deadlines_remove_the_tool_folder_and_lock(self) -> None:
        conditions = {
            "time": "True",
            "trace": "'TEST_TIMES_OUT' not in os.environ",
        }
        for stage, condition in conditions.items():
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                tree = Path(temporary)
                script = tree / "paused_stage.py"
                _ = script.write_text(
                    "import os, signal\n"
                    + f"if {condition}:\n    signal.pause()\n", encoding="utf-8")
                output = io.StringIO()
                with (mock.patch.dict(os.environ, {"TMPDIR": temporary}),
                      mock.patch.object(module_cost, "runner_command", return_value=[sys.executable, str(script)]),
                      mock.patch.object(task_cpu, "measurement_timeout_s", return_value=0.5),
                      redirect_stderr(output),
                      redirect_stdout(io.StringIO())):
                    status = module_cost.main(["--tree", temporary, "--commands", "10", "fixture/test_pause.py"])
                self.assertEqual(status, 1, output.getvalue())
                self.assertIn(f"starting {stage} run", output.getvalue())
                failures = [line for line in output.getvalue().splitlines() if line.startswith("module_cost:")]
                self.assertEqual(len(failures), 1, output.getvalue())
                self.assertIn("timed out after 0.5 s", failures[0])
                self.assertIn(f"{sys.executable} {script}", failures[0])
                self.assertEqual(list(tree.iterdir()), [script])

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_time_deadline_includes_a_stall_before_exec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            entered = Path(temporary) / "entered-chdir"
            real_chdir = os.chdir

            def slow_chdir(path: Path) -> None:
                entered.touch()
                _ = select.poll().poll(5000)
                real_chdir(path)

            with (mock.patch.dict(os.environ, {"TMPDIR": temporary}),
                  mock.patch.object(os, "chdir", side_effect=slow_chdir),
                  mock.patch.object(task_cpu, "measurement_timeout_s", return_value=0.75),
                  redirect_stderr(io.StringIO()) as output):
                status = module_cost.main(["--tree", temporary, "fixture/test_pause.py"])
            self.assertTrue(entered.exists())
            self.assertEqual(status, 1, output.getvalue())
            self.assertIn("timed out after 0.75 s", output.getvalue())
            self.assertEqual(list(Path(temporary).iterdir()), [entered])

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_time_stage_rejects_an_exit_read_after_the_deadline(self) -> None:
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read(1)"],
                              stdin=subprocess.PIPE, text=True) as child:
            assert child.stdin is not None
            deadline = time.monotonic()
            child.stdin.close()
            descriptor = os.pidfd_open(child.pid)
            try:
                ended = select.poll()
                ended.register(descriptor, select.POLLIN)
                self.assertTrue(ended.poll(int(TEST_HANG_GUARD_SECS * 1000)))
                with self.assertRaisesRegex(task_cpu.MeasurementTimeout, "timed out after 1 s"):
                    _ = module_cost.wait_for_stage(child.pid, deadline, 1)
            finally:
                os.close(descriptor)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_caller_stops_a_measurement_process_that_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            finished = Path(temporary) / "finished"

            def stall_at_start(*_args: object) -> int:
                _ = select.poll().poll(5000)
                finished.touch()
                return 0

            with (mock.patch.object(module_cost, "run_measurements", side_effect=stall_at_start),
                  mock.patch.object(task_cpu, "measurement_timeout_s", return_value=0.25),
                  mock.patch.object(task_cpu, "CLEANUP_LIMIT_S", 0.25),
                  redirect_stderr(io.StringIO()) as output):
                status = module_cost.main(["--tree", temporary, "fixture/test_pause.py"])
            self.assertEqual(status, 1, output.getvalue())
            self.assertIn("timed out after 0.25 s", output.getvalue())
            self.assertIn("(module_cost measurement)", output.getvalue())
            self.assertFalse(finished.exists())

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_supervisor_stops_the_time_stage_and_releases_the_folder_lock(self) -> None:
        self.check_supervisor_cleanup(exit_during_cleanup=False)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_supervisor_reaps_the_time_stage_when_the_worker_exits_during_cleanup(self) -> None:
        self.check_supervisor_cleanup(exit_during_cleanup=True)

    def check_supervisor_cleanup(self, *, exit_during_cleanup: bool) -> None:
        _ = adopt_descendants()
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as sockets:
            listener = sockets.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
            worker, supervisor = socket.socketpair()
            _ = sockets.enter_context(worker)
            _ = sockets.enter_context(supervisor)
            tree = Path(temporary)
            address = "\0module-cost-supervisor-" + tree.name
            listener.bind(address)
            listener.settimeout(TEST_HANG_GUARD_SECS)
            script = tree / "paused_stage.py"
            _ = script.write_text(
                "import os, select, socket\nfrom pathlib import Path\n"
                + "if not os.fork():\n    os.setsid()\n"
                + "folder = Path(os.environ['BUILDLOG_DIR']).parent\n"
                + "with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as peer:\n"
                + f"    peer.connect({address!r})\n"
                + "    fd = os.pidfd_open(os.getpid())\n"
                + "    socket.send_fds(peer, [f'{os.getpid()}\\n'.encode() + os.fsencode(folder)], [fd])\n"
                + "    os.close(fd)\n"
                + "select.poll().poll(10000)\n", encoding="utf-8")

            def stall_after_launch(*_args: object) -> tuple[int, float]:
                worker.settimeout(TEST_HANG_GUARD_SECS)
                if worker.recv(1) == b"1":
                    os._exit(1)
                raise AssertionError("The worker did not receive the exit request")

            send_signal = signal.pidfd_send_signal
            worker_exited = False

            def stop_after_worker_exits(descriptor: int, sig: int) -> None:
                nonlocal worker_exited
                if exit_during_cleanup and not worker_exited:
                    supervisor.settimeout(TEST_HANG_GUARD_SECS)
                    supervisor.sendall(b"1")
                    ended = select.poll()
                    ended.register(descriptor, select.POLLIN)
                    self.assertTrue(ended.poll(int(TEST_HANG_GUARD_SECS * 1000)))
                    worker_exited = True
                send_signal(descriptor, sig)

            descriptors: list[int] = []
            try:
                with (mock.patch.dict(os.environ, {"TMPDIR": temporary}),
                      mock.patch.object(module_cost, "runner_command", return_value=[sys.executable, str(script)]),
                      mock.patch.object(module_cost, "wait_for_stage", side_effect=stall_after_launch),
                      mock.patch.object(signal, "pidfd_send_signal", side_effect=stop_after_worker_exits),
                      mock.patch.object(task_cpu, "measurement_timeout_s", return_value=1),
                      mock.patch.object(task_cpu, "CLEANUP_LIMIT_S", 0.5),
                      redirect_stderr(io.StringIO()) as output):
                    status = module_cost.main(["--tree", temporary, "fixture/test_pause.py"])
                folders: set[Path] = set()
                pids: list[int] = []
                for _ in range(2):
                    packet = socket.recv_fds(listener, 4096, 1)
                    descriptors.extend(packet[1])
                    pid, folder_path = packet[0].split(b"\n", 1)
                    pids.append(int(pid))
                    folders.add(Path(os.fsdecode(folder_path)))
                self.assertEqual(status, 1, output.getvalue())
                self.assertIn("(module_cost measurement)", output.getvalue())
                self.assertEqual(len(descriptors), 2)
                ended = select.poll()
                for descriptor in descriptors:
                    ended.register(descriptor, select.POLLIN)
                self.assertEqual({fd for fd, _ in ended.poll(0)}, set(descriptors))
                for pid, descriptor in zip(pids, descriptors, strict=True):
                    self.assertIn(f"{pid} ({sys.executable} {script})", output.getvalue())
                    with self.assertRaises(ChildProcessError):
                        _ = os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                folder, = folders
                with folder.with_name(folder.name + ".lock").open("r+") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                for descriptor in descriptors:
                    try:
                        with suppress(ProcessLookupError):
                            send_signal(descriptor, signal.SIGKILL)
                        ended = select.poll()
                        ended.register(descriptor, select.POLLIN)
                        if ended.poll(int(TEST_HANG_GUARD_SECS * 1000)):
                            with suppress(ChildProcessError):
                                _ = os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG)
                    finally:
                        os.close(descriptor)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_timeout_leaves_the_callers_child_running_and_preserves_its_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            script = tree / "paused.py"
            _ = script.write_text("import signal\nsignal.pause()\n", encoding="utf-8")
            caller_child = subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read(1); sys.exit(37)"],
                stdin=subprocess.PIPE, text=True,
            )
            try:
                with (mock.patch.dict(os.environ, {"TMPDIR": temporary}),
                      mock.patch.object(module_cost, "runner_command", return_value=[sys.executable, str(script)]),
                      mock.patch.object(task_cpu, "measurement_timeout_s", return_value=0.5),
                      redirect_stderr(io.StringIO()) as output):
                    status = module_cost.main(["--tree", temporary, "fixture/test_pause.py"])
                self.assertEqual(status, 1, output.getvalue())
                self.assertIn("timed out after 0.5 s", output.getvalue())
                self.assertIsNone(caller_child.poll())
                _ = caller_child.communicate("1", timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(caller_child.returncode, 37)
            finally:
                if caller_child.poll() is None:
                    caller_child.kill()
                    _ = caller_child.communicate(timeout=TEST_HANG_GUARD_SECS)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_captured_cli_reaches_eof_while_the_supervisor_is_stalled(self) -> None:
        _ = adopt_descendants()
        with ExitStack() as cleanup:
            caller, child = socket.socketpair()
            _ = cleanup.enter_context(caller)
            _ = cleanup.enter_context(child)
            caller.settimeout(TEST_HANG_GUARD_SECS)
            script = """\
import os, socket, sys
from unittest import mock
from scripts.test_times import module_cost, task_cpu

aliases = [os.dup(1), os.dup(2)]
channel = socket.socket(fileno=int(sys.argv[1]))

def stall(*args):
    os.write(1, b'child stdout fd\\n')
    os.write(2, b'child stderr fd\\n')
    print('child stdout stream', flush=True)
    print('child stderr stream', file=sys.stderr, flush=True)
    offsets = [os.fstat(fd).st_size for fd in (1, 2)]
    with os.fdopen(os.pidfd_open(os.getpid()), 'rb') as identity:
        socket.send_fds(channel, [b'1'], [identity.fileno()])
    channel.recv(1)
    assert [os.lseek(fd, 0, os.SEEK_CUR) for fd in (1, 2)] == offsets
    assert not any(os.path.exists(f'/proc/self/fd/{fd}') for fd in aliases)
    return 0

with (mock.patch.object(module_cost, 'supervise', side_effect=stall),
      mock.patch.object(task_cpu, 'measurement_timeout_s', return_value=0.25),
      mock.patch.object(task_cpu, 'CLEANUP_LIMIT_S', 0.1)):
    raise SystemExit(module_cost.main(['--tree', '.', 'fixture/test_pause.py']))
"""
            with subprocess.Popen([sys.executable, "-c", script, str(child.fileno())], pass_fds=(child.fileno(),),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as cli:
                child.close()
                try:
                    packet = socket.recv_fds(caller, 1, 1)
                    descriptor, = packet[1]
                    _ = cleanup.callback(os.close, descriptor)
                    ended = select.poll()
                    ended.register(descriptor, select.POLLIN)
                    self.assertEqual(cli.wait(timeout=TEST_HANG_GUARD_SECS), 1)
                    assert cli.stdout is not None and cli.stderr is not None
                    capture = select.poll()
                    capture.register(cli.stdout, select.POLLHUP)
                    capture.register(cli.stderr, select.POLLHUP)
                    closed = {fd for fd, events in capture.poll(0) if events & select.POLLHUP}
                    self.assertEqual(closed, {cli.stdout.fileno(), cli.stderr.fileno()})
                    stdout, stderr = cli.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertFalse(ended.poll(0), "The supervisor exited before the CLI saw EOF")
                    self.assertEqual(stdout, "child stdout fd\nchild stdout stream\n")
                    self.assertIn("child stderr fd\nchild stderr stream\n", stderr)
                    self.assertIn("(module_cost supervisor)", stderr)
                finally:
                    # EOF lets the supervisor finish even if the CLI check fails.
                    caller.close()
            self.assertTrue(ended.poll(int(TEST_HANG_GUARD_SECS * 1000)))
            result = os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG)
            assert result is not None
            self.assertEqual(result.si_status, 0)

    def test_replay_caps_snapshot_bytes_and_prints_one_notice(self) -> None:
        limit = module_cost.OUTPUT_REPLAY_LIMIT_BYTES
        prefix = "a" * (limit - 2) + "é"
        stream = io.StringIO()
        with module_cost.child_output(stream) as output:
            _ = output.write(prefix + "discard this suffix\n")
            output.flush()
        replay, notice = stream.getvalue().splitlines()
        self.assertTrue(replay == prefix, "The replay changed or exceeded the byte limit")
        self.assertEqual(notice, f"module_cost: output truncated after {limit} bytes")

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_deferred_reaper_preserves_a_callers_child_after_another_reaper_collects_the_supervisor(self) -> None:
        with subprocess.Popen([sys.executable, "-c", "raise SystemExit(37)"]) as child:
            self.check_deferred_supervisor_reap(interrupted=False)
            self.assertEqual(child.wait(timeout=TEST_HANG_GUARD_SECS), 37)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_interrupt_hands_the_supervisor_pidfd_to_the_deferred_reaper(self) -> None:
        self.check_deferred_supervisor_reap(interrupted=True)

    def check_deferred_supervisor_reap(self, *, interrupted: bool) -> None:
        with ExitStack() as cleanup:
            caller, supervisor = socket.socketpair()
            _ = cleanup.enter_context(caller)
            _ = cleanup.enter_context(supervisor)
            reap = module_cost.reap_supervisor
            read_status = module_cost.supervisor_status
            permit_reap = threading.Event()
            finished = threading.Event()
            received = Queue[int]()
            errors: list[BaseException] = []
            interrupt = KeyboardInterrupt("fixture interrupt")

            def hold_supervisor(*_args: object) -> int:
                caller.close()
                supervisor.settimeout(TEST_HANG_GUARD_SECS)
                _ = supervisor.recv(1)
                return 19

            def terminate_wait(pid: int, descriptor: int, _deadline: float, timeout_s: float) -> int:
                if interrupted:
                    raise interrupt
                return read_status(pid, descriptor, time.monotonic(), timeout_s)

            def deferred_reap(descriptor: int) -> None:
                received.put(descriptor)
                try:
                    self.assertTrue(permit_reap.wait(TEST_HANG_GUARD_SECS))
                    reap(descriptor)
                except BaseException as error:
                    errors.append(error)
                finally:
                    finished.set()

            output = io.StringIO()
            with (mock.patch.object(module_cost, "supervise", side_effect=hold_supervisor),
                  mock.patch.object(module_cost, "supervisor_status", side_effect=terminate_wait),
                  mock.patch.object(module_cost, "reap_supervisor", side_effect=deferred_reap),
                  mock.patch.object(os, "waitpid", side_effect=AssertionError("Numeric PID wait")),
                  redirect_stderr(output)):
                try:
                    args = ["--tree", ".", "fixture/test_pause.py"]
                    if interrupted:
                        with self.assertRaises(KeyboardInterrupt) as raised:
                            _ = module_cost.main(args)
                        self.assertIs(raised.exception, interrupt)
                    else:
                        self.assertEqual(module_cost.main(args), 1)
                        self.assertIn("(module_cost supervisor)", output.getvalue())
                    descriptor = received.get(timeout=TEST_HANG_GUARD_SECS)
                    witness = os.dup(descriptor)
                    _ = cleanup.callback(os.close, witness)
                    caller.close()
                    ended = select.poll()
                    ended.register(witness, select.POLLIN)
                    self.assertTrue(ended.poll(int(TEST_HANG_GUARD_SECS * 1000)))
                    if not interrupted:
                        result = os.waitid(os.P_PIDFD, witness, os.WEXITED | os.WNOHANG)
                        assert result is not None
                        self.assertEqual(result.si_status, 19)
                finally:
                    caller.close()
                    permit_reap.set()
                self.assertTrue(finished.wait(TEST_HANG_GUARD_SECS))
                self.assertEqual(errors, [])
                with self.assertRaises(ChildProcessError):
                    _ = os.waitid(os.P_PIDFD, witness, os.WEXITED | os.WNOHANG)
                with self.assertRaises(OSError) as closed:
                    _ = os.fstat(descriptor)
                self.assertEqual(closed.exception.errno, errno.EBADF)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_time_deadline_stops_children_forked_during_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as sockets:
            tree = Path(temporary)
            address = "\0module-cost-" + tree.name
            listener = sockets.enter_context(socket.socket(socket.AF_UNIX))
            listener.bind(address)
            listener.listen()
            listener.settimeout(TEST_HANG_GUARD_SECS)
            incoming, outgoing = socket.socketpair(type=socket.SOCK_DGRAM)
            _ = sockets.enter_context(incoming)
            _ = sockets.enter_context(outgoing)
            incoming.setblocking(False)
            script = tree / "fork_during_cleanup.py"
            _ = script.write_text(
                "import os, signal, socket\n"
                + "with socket.socket(socket.AF_UNIX) as peer:\n"
                + f"    peer.connect({address!r})\n"
                + "    while peer.recv(1):\n"
                + "        if os.fork() == 0:\n"
                + "            os.setsid()\n"
                + "            peer.sendall(f'{os.getpid()}\\n'.encode())\n"
                + "            peer.close()\n"
                + "            signal.pause()\n"
                + "            os._exit(0)\n", encoding="utf-8")
            pid_file = tree / "children"
            send_signal = signal.pidfd_send_signal

            def fork_before_kill(descriptor: int, sig: int) -> None:
                if not pid_file.exists():
                    # Cleanup already took its first process snapshot.
                    with listener.accept()[0] as peer, peer.makefile("r") as replies, pid_file.open("w") as pids:
                        peer.settimeout(TEST_HANG_GUARD_SECS)
                        for _ in range(3):
                            peer.sendall(b"1")
                            reply = replies.readline()
                            child_fd = os.pidfd_open(int(reply))
                            try:
                                _ = socket.send_fds(outgoing, [reply.encode()], [child_fd])
                            finally:
                                os.close(child_fd)
                            _ = pids.write(reply)
                send_signal(descriptor, sig)

            pending_processes = task_cpu.pending_processes

            def skip_one_scan(root_pid: int) -> dict[int, task_cpu.ProcessLocation]:
                if pid_file.exists() and not (tree / "missed").exists():
                    # Simulate a scan that misses the children during reparenting.
                    (tree / "missed").touch()
                    return {}
                return pending_processes(root_pid)

            output = io.StringIO()
            try:
                with (mock.patch.dict(os.environ, {"TMPDIR": temporary}),
                      mock.patch.object(module_cost, "runner_command", return_value=[sys.executable, str(script)]),
                      mock.patch.object(task_cpu, "measurement_timeout_s", return_value=1),
                      mock.patch.object(signal, "pidfd_send_signal", side_effect=fork_before_kill),
                      mock.patch.object(task_cpu, "pending_processes", side_effect=skip_one_scan),
                      redirect_stderr(output)):
                    status = module_cost.main(["--tree", temporary, "fixture/test_fork.py"])
                self.assertEqual(status, 1, output.getvalue())
                self.assertIn("timed out after 1 s", output.getvalue())
                children = [int(pid) for pid in pid_file.read_text().splitlines()]
                self.assertEqual(len(children), 3)
                self.assertTrue((tree / "missed").exists())
                for pid in children:
                    with self.subTest(pid=pid):
                        self.assertFalse(Path(f"/proc/{pid}").exists(), f"Child {pid} is still present")
                        self.assertEqual(output.getvalue().count(f"{pid} ({sys.executable} {script})"), 1)
                        with self.assertRaises(ChildProcessError):
                            _ = os.waitpid(pid, os.WNOHANG)
            finally:
                while True:
                    try:
                        descriptors = socket.recv_fds(incoming, 32, 1)[1]
                    except BlockingIOError:
                        break
                    for descriptor in descriptors:
                        try:
                            with suppress(ProcessLookupError):
                                send_signal(descriptor, signal.SIGKILL)
                        finally:
                            os.close(descriptor)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_command_measure_preserves_a_child_group_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree / "scripts/lib/python_startup"), os.environ.get("PYTHONPATH", ""))),
            }))
            copy_test_runner(tree)
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            _ = (folder / "test_stop.py").write_text(
                "import os, signal, unittest\n"
                + "class Stop(unittest.TestCase):\n"
                + "    def test_stop(self):\n"
                + "        child = os.fork()\n"
                + "        if child == 0:\n"
                + "            os.kill(os.getpid(), signal.SIGSTOP)\n"
                + "            os._exit(0)\n"
                + "        try:\n"
                + "            pid, status = os.waitpid(child, os.WUNTRACED)\n"
                + "            self.assertEqual(pid, child)\n"
                + "            self.assertTrue(os.WIFSTOPPED(status))\n"
                + "            os.kill(child, signal.SIGCONT)\n"
                + "            self.assertEqual(os.waitpid(child, 0)[1], 0)\n"
                + "        finally:\n"
                + "            try: os.kill(child, signal.SIGKILL)\n"
                + "            except ProcessLookupError: pass\n"
                + "            try: os.waitpid(child, 0)\n"
                + "            except ChildProcessError: pass\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                status = module_cost.main(["--tree", str(tree), "--commands", "10", "fixture/test_stop.py"])
        self.assertEqual(status, 0, output.getvalue())

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_ending_the_tracer_ends_its_paused_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as sockets:
            reader, writer = socket.socketpair()
            _ = sockets.enter_context(reader)
            _ = sockets.enter_context(writer)
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree / "scripts/lib/python_startup"), os.environ.get("PYTHONPATH", ""))),
            }))
            paused = tree / "paused.py"
            _ = paused.write_text(
                "import os, signal\n"
                + f"os.write({writer.fileno()}, str(os.getpid()).encode())\n"
                + "signal.pause()\n", encoding="utf-8")
            tracer = tree / "tracer.py"
            _ = tracer.write_text(
                "import os, sys\nfrom pathlib import Path\n"
                + "from scripts.test_times.task_cpu import measure\n"
                + f"measure([sys.executable, {str(paused)!r}], cwd=Path({str(tree)!r}), environment=dict(os.environ))\n",
                encoding="utf-8")
            process = subprocess.Popen([str(Path(task_cpu.__file__).resolve().parents[1] / "lib/py"), str(tracer)], pass_fds=(writer.fileno(),),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            writer.close()
            child = 0
            try:
                reader.settimeout(TEST_HANG_GUARD_SECS)
                child = int(reader.recv(32))
                descriptor = os.pidfd_open(child)
                try:
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(process.returncode, -signal.SIGTERM, stdout + stderr)
                    ended = select.poll()
                    ended.register(descriptor, select.POLLIN)
                    self.assertTrue(ended.poll(int(TEST_HANG_GUARD_SECS * 1000)), "The traced task did not end")
                finally:
                    os.close(descriptor)
            finally:
                if child:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_counts_successful_process_and_exec_events_without_threads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            script = tree / "starts.py"
            _ = script.write_text(
                "import ctypes, os, subprocess, sys, threading\n"
                + "assert ctypes.CDLL(None).syscall(435, 0, 0) == -1  # clone3 rejects empty arguments.\n"
                + "child = os.fork()\n"
                + "if child == 0:\n    os._exit(0)\n"
                + "os.waitpid(child, 0)\n"
                + "try:\n    subprocess.run(['/nonexistent/cost-probe'], check=True)\n"
                + "except FileNotFoundError:\n    pass\n"
                + "def replace():\n    os.execv(sys.executable, [sys.executable, '-c', 'pass'])\n"
                + "thread = threading.Thread(target=replace)\n"
                + "thread.start()\nthread.join()\n", encoding="utf-8")
            for command_rows in (False, True):
                with self.subTest(command_rows=command_rows):
                    counts = task_cpu.measure([sys.executable, str(script)], cwd=tree, environment=dict(os.environ),
                                              timeout_s=TEST_HANG_GUARD_SECS, command_rows=command_rows)
                    self.assertEqual(counts.status, 0)
                    self.assertEqual(counts.processes, 2)
                    self.assertEqual(counts.execs, 2)
                    self.assertEqual(sum(row.starts for row in counts.commands), 3 if command_rows else 0)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_failed_trace_attachment_reaps_the_child_before_the_next_module(self) -> None:
        load_library = ctypes.CDLL
        refused_pid = 0

        def library(name: str | None, *, use_errno: bool) -> ctypes.CDLL:
            libc = load_library(name, use_errno=use_errno)
            ptrace = libc.ptrace

            def refuse_first_attach(request: int, pid: int, address: object, data: object) -> int:
                nonlocal refused_pid
                if request == task_cpu.PTRACE_SEIZE and not refused_pid:
                    refused_pid = pid
                    _ = ctypes.set_errno(errno.EPERM)
                    return -1
                return cast(int, ptrace(request, pid, address, data))

            setattr(libc, "ptrace", refuse_first_attach)
            return libc

        modules = ["fixture/test_first.py", "fixture/test_second.py"]

        def command(_tree: Path, module: str) -> list[str]:
            if module == modules[1]:
                self.assertGreater(refused_pid, 0)
                with self.assertRaises(ChildProcessError):
                    _ = os.waitid(os.P_PID, refused_pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            return [sys.executable, "-c", "pass"]

        with (tempfile.TemporaryDirectory() as temporary,
              mock.patch.dict(os.environ, {"TMPDIR": temporary}),
              mock.patch.object(ctypes, "CDLL", side_effect=library),
              mock.patch.object(module_cost, "runner_command", side_effect=command),
              redirect_stdout(io.StringIO()) as stdout,
              redirect_stderr(io.StringIO()) as stderr):
            status = module_cost.main(["--tree", temporary, *modules])
        self.assertEqual(status, 1, stdout.getvalue() + stderr.getvalue())
        self.assertIn(f"{modules[0]}: cannot attach tracer: [Errno {errno.EPERM}]", stderr.getvalue())
        first, second = stdout.getvalue().splitlines()[1:3]
        self.assertEqual([first.split()[0], second.split()[0]], modules)
        self.assertTrue(first.endswith("RED (test=0, trace=1)"), first)
        self.assertTrue(second.endswith("PASS"), second)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_reports_each_module_when_the_trace_run_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            script = tree / "trace_failure.py"
            _ = script.write_text(
                "import os, sys\n"
                + "assert sys.dont_write_bytecode\n"
                + "if 'TEST_TIMES_OUT' not in os.environ:\n    print('fixture trace failure')\n    sys.exit(7)\n",
                encoding="utf-8")
            modules = ["fixture/test_first.py", "fixture/test_second.py"]
            with (mock.patch.dict(os.environ, {"PATH": temporary, "TMPDIR": temporary}),
                  mock.patch.object(module_cost, "runner_command", return_value=[sys.executable, str(script)]),
                  redirect_stdout(io.StringIO()) as stdout,
                  redirect_stderr(io.StringIO()) as stderr):
                status = module_cost.main(["--tree", temporary, *modules])
        self.assertNotEqual(status, 0, stdout.getvalue() + stderr.getvalue())
        self.assertEqual(stderr.getvalue().count("fixture trace failure"), 2)
        rows = stdout.getvalue().splitlines()
        for module, row in zip(modules, rows[1:3], strict=True):
            self.assertEqual(row.split()[0], module)
            self.assertIn("RED (test=0, trace=7)", row)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_lists_the_test_with_a_cpu_heavy_child_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree / "scripts/lib/python_startup"), os.environ.get("PYTHONPATH", ""))),
            }))
            copy_test_runner(tree)
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            _ = (folder / "test_cpu.py").write_text(
                "import subprocess, sys, unittest\n"
                + "class CPU(unittest.TestCase):\n"
                + "    def test_heavy(self):\n"
                + "        subprocess.run([sys.executable, '-c', "
                + "\"import time; end=time.process_time()+0.2\\nwhile time.process_time()<end: pass\"], check=True)\n"
                + "    def test_light(self):\n"
                + "        import time\n"
                + "        end=time.process_time()+0.02\n"
                + "        while time.process_time()<end: pass\n", encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                status = module_cost.main(["--tree", str(tree), "--tests", "2", "fixture/test_cpu.py"])
        self.assertEqual(status, 0, output.getvalue())
        rows = [line.split() for line in output.getvalue().splitlines() if "CPU.test_" in line]
        self.assertEqual([row[1] for row in rows], ["CPU.test_heavy", "CPU.test_light"])
        self.assertGreater(float(rows[0][-2]), float(rows[1][-2]))
        self.assertGreater(float(rows[0][-1]), 0)

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_ends_a_detached_child_and_keeps_the_module_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree / "scripts/lib/python_startup"), os.environ.get("PYTHONPATH", ""))),
            }))
            copy_test_runner(tree)
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            pids = tree / "pids"
            _ = (folder / "test_orphan.py").write_text(
                "import pathlib, subprocess, sys, unittest\n"
                + "class Orphan(unittest.TestCase):\n"
                + "    def test_orphan(self):\n"
                + "        child=subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'], "
                + "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                + f"        with pathlib.Path({str(pids)!r}).open('a') as output: output.write(str(child.pid)+'\\n')\n"
                + "        self.assertIsNone(child.poll())\n", encoding="utf-8")
            try:
                output = io.StringIO()
                with redirect_stdout(output):
                    status = module_cost.main(["--tree", str(tree), "fixture/test_orphan.py"])
                self.assertEqual(status, 0, output.getvalue())
                children = [int(line) for line in pids.read_text().splitlines()]
                self.assertEqual(len(children), 2)
                for pid in children:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if pids.is_file():
                    for line in pids.read_text().splitlines():
                        try:
                            os.kill(int(line), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_ranks_commands_by_their_cpu_and_reports_unassigned_cpu(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree / "scripts/lib/python_startup"), os.environ.get("PYTHONPATH", ""))),
            }))
            copy_test_runner(tree)
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            heavy = tree / "heavy.py"
            light = tree / "light.py"
            _ = heavy.write_text(
                "import os, time\n"
                + "child=os.fork()\n"
                + "if child == 0:\n"
                + "    end=time.process_time()+0.4\n"
                + "    while time.process_time()<end: pass\n"
                + "    os._exit(0)\n"
                + "os.waitpid(child, 0)\n")
            _ = light.write_text("pass\n")
            _ = (folder / "test_commands.py").write_text(
                "import subprocess, sys, unittest\n"
                + "class Commands(unittest.TestCase):\n"
                + "    def test_heavy(self):\n"
                + f"        self.assertEqual(sys.modules['__main__'].__file__, {str(tree / 'scripts/test_times/module_run.py')!r})\n"
                + f"        subprocess.run([sys.executable, {str(heavy)!r}], check=True)\n"
                + "    def test_light(self):\n"
                + f"        subprocess.run([sys.executable, {str(light)!r}], check=True)\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(Path(module_cost.__file__)), "--tree", str(tree),
                 "--commands", "10", "fixture/test_commands.py"],
                capture_output=True, text=True, check=False, timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = result.stdout.splitlines()
        commands = rows[rows.index(next(line for line in rows if "test_commands.py command" in line)) + 1:]
        self.assertIn(str(heavy), commands[0])
        self.assertEqual(int(commands[0].split()[-2]), 2)
        light_row = next(line for line in commands if str(light) in line)
        self.assertGreater(float(commands[0].split()[-1]), float(light_row.split()[-1]))
        self.assertRegex(commands[-1], r"^unassigned [+-]\d+\.\d+ CPU s$")

    @unittest.skipUnless(sys.platform == "linux", "Linux is required")
    def test_reports_costs_for_a_module_that_starts_two_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            self.enterContext(mock.patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(tree / "scripts/lib/python_startup"), os.environ.get("PYTHONPATH", ""))),
                "HERDR_BIN": "/host/herdr",
            }))
            copy_test_runner(tree)
            folder = tree / "scripts" / "fixture"
            folder.mkdir(parents=True)
            leases = tree / "leases"
            _ = (folder / "test_children.py").write_text(
                'import fcntl, os, pathlib, subprocess, sys, unittest\n'
                + 'class Children(unittest.TestCase):\n'
                + '    def test_children(self):\n'
                + '        folder = pathlib.Path(os.environ["BUILDLOG_DIR"]).parent\n'
                + '        lock_path = folder.with_name(folder.name + ".lock")\n'
                + '        lock = lock_path.stat()\n'
                + '        inherited = []\n'
                + '        for entry in pathlib.Path("/dev/fd").iterdir():\n'
                + '            try:\n'
                + '                if os.path.samestat(os.fstat(int(entry.name)), lock): inherited.append(int(entry.name))\n'
                + '            except OSError: pass\n'
                + '        self.assertTrue(inherited, "The measurement child has no folder lease")\n'
                + '        fcntl.flock(inherited[0], fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
                + '        with lock_path.open("r+") as probe:\n'
                + '            with self.assertRaises(BlockingIOError): fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)\n'
                + f'        with open({str(leases)!r}, "a") as log: log.write("held\\n")\n'
                + '        self.assertEqual(os.environ["HERDR_BIN"], "/nonexistent/herdr")\n'
                + '        self.assertTrue(sys.dont_write_bytecode)\n'
                + '        for code in ("pass", "import os"):\n'
                + '            subprocess.run([sys.executable, "-c", code], check=True)\n',
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                status = module_cost.main(["--tree", str(tree), "--commands", "10", "fixture/test_children.py"])
            lease_states = leases.read_text().splitlines()
        self.assertEqual(status, 0, output.getvalue())
        lines = output.getvalue().splitlines()
        row = lines[1].split()
        self.assertEqual(row[0], "fixture/test_children.py")
        self.assertGreater(float(row[1]), 0)
        self.assertEqual(int(row[2]), 3)
        self.assertEqual(int(row[3]), 3)
        self.assertGreater(float(row[4]), 0)
        self.assertEqual(row[5], "PASS")
        self.assertEqual(lines[2].split(), ["total", *row[1:]])
        self.assertEqual(lease_states, ["held", "held"])


if __name__ == "__main__":
    _ = unittest.main()

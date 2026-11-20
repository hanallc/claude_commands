"""Exercise ownership across process groups, sessions, and owner death."""
from __future__ import annotations

from contextlib import suppress
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
from typing import BinaryIO, cast
import unittest
from unittest.mock import patch

from scripts.direct.wait_for_test_state import wait_until
from scripts.lib import process_supervisor
from scripts.lib.process_supervisor import ManagedProcess, launch
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


ROOT = Path(__file__).resolve().parents[2]
PAUSE = "import signal; signal.pause()"
TREE = """
import os, signal, subprocess, sys
leaf = "import os, signal; print(os.getpid(), os.getpgrp(), os.getsid(0), flush=True); signal.pause()"
branch = "import os, signal, subprocess, sys; print(os.getpid(), os.getpgrp(), os.getsid(0), flush=True); " + (
    "subprocess.Popen([sys.executable, '-c', sys.argv[1]]); signal.pause()")
print(os.getpid(), os.getpgrp(), os.getsid(0), flush=True)
subprocess.Popen([sys.executable, "-c", branch, leaf], process_group=0)
signal.pause()
"""
OWNER = """
import os, signal, subprocess, sys
from scripts.lib.process_supervisor import launch
mode, source = sys.argv[1:]
if mode == "several":
    workloads = [launch([sys.executable, "-c", source]) for _ in range(3)]
    print(os.getpid(), flush=True)
elif mode == "siblings":
    workload = launch([sys.executable, "-c", source])
    forked = os.fork()
    if forked == 0:
        signal.pause()
        os._exit(0)
    executed = subprocess.Popen([sys.executable, "-c", source])
    print(workload.pid, workload.session_id, forked, executed.pid, flush=True)
else:
    workload = launch([sys.executable, "-c", source])
    print(workload.pid, workload.session_id, flush=True)
signal.pause()
"""
NESTED = """
import os, signal, sys
from scripts.lib.process_supervisor import launch
nested = launch([sys.executable, "-c", "import signal; signal.pause()"])
print(os.getpid(), os.getsid(0), nested.pid, nested.session_id, flush=True)
signal.pause()
"""
SPAWN_BEFORE_ACK = """
import os, signal, subprocess, sys
from scripts.lib import process_supervisor

original = subprocess.Popen
def kill_owner(command, **options):
    child = original(command, **options)
    if options.get("process_group") == 0:
        print(child.pid, os.getpid(), flush=True)
        os.kill(os.getppid(), signal.SIGKILL)
    return child

subprocess.Popen = kill_owner
raise SystemExit(process_supervisor.supervise(*map(int, sys.argv[1:4]), sys.argv[4:]))
"""
INTERRUPT_STARTUP = """
import subprocess, sys
from unittest.mock import patch
from scripts.lib.process_supervisor import launch
original = subprocess.Popen
def use_test_guardian(arguments, **options):
    return original([sys.executable, "-c", sys.argv[1], *arguments[4:]], **options)
with patch.object(subprocess, "Popen", side_effect=use_test_guardian):
    launch([sys.executable, "-c", "import signal; signal.pause()"])
"""
FORK_DURING_CLEANUP = """
import os, select, subprocess, sys
from contextlib import closing
from scripts.lib import process_supervisor

original_popen = subprocess.Popen

def start_parent(command, **options):
    global parent
    if options.get("process_group") != 0:
        return original_popen(command, **options)
    release_read = os.dup(0)
    try:
        descriptors = (*options.pop("pass_fds", ()), release_read)
        parent = original_popen([*command, str(release_read)], stdin=subprocess.PIPE,
                                pass_fds=descriptors, **options)
        return parent
    finally:
        os.close(release_read)

def trigger_fork():
    assert parent.stdin.write(b"x") == 1
    parent.stdin.close()

def fork_and_observe_exit():
    if sys.platform == "linux":
        descriptor = os.pidfd_open(parent.pid)
        try:
            trigger_fork()
            assert select.select([descriptor], [], [], 10)[0]
        finally:
            os.close(descriptor)
    else:
        with closing(select.kqueue()) as events:
            note = select.kevent(parent.pid, filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)
            events.control([note], 0)
            trigger_fork()
            assert events.control(None, 1, 10)

if sys.platform == "linux":
    original_children = process_supervisor._linux_children
    def children():
        snapshot = original_children()
        assert parent.pid in snapshot
        process_supervisor._linux_children = original_children
        fork_and_observe_exit()
        return snapshot
    process_supervisor._linux_children = children
else:
    original_output = subprocess.check_output
    def list_pids(*args, **options):
        snapshot = original_output(*args, **options)
        assert str(parent.pid) in snapshot.split()
        subprocess.check_output = original_output
        fork_and_observe_exit()
        return snapshot
    subprocess.check_output = list_pids

subprocess.Popen = start_parent
raise SystemExit(process_supervisor.supervise(*map(int, sys.argv[1:4]), sys.argv[4:]))
"""
FORKING_PARENT = """
import os, sys
assert sys.stdin.read(1) == "x"
read, write = os.pipe()
if os.fork() == 0:
    os.close(read)
    if sys.platform == "linux":
        os.setsid()
    with open(sys.argv[1], "w") as record:
        print(os.getpid(), file=record)
    os.close(write)
    os.close(1)
    os.close(2)
    with os.fdopen(int(sys.argv[2]), "rb") as release:
        assert release.read(1) == b""
    os._exit(0)
os.close(write)
assert os.read(read, 1) == b""
"""
ADOPTED_CHILDREN = """
import os, sys

children = []
for _ in range(int(sys.argv[1])):
    pid = os.fork()
    if pid == 0:
        os.setsid()
        os.close(1)
        os.close(2)
        assert os.read(0, 1) == b""
        os._exit(0)
    children.append(pid)
print(*children, flush=True)
"""
CHECK_LINUX_CLEANUP = """
import resource, sys
from scripts.lib import process_supervisor

expected = int(sys.argv[-2])
check = sys.argv[-1]
clear = process_supervisor._clear_linux_children
def check_cleanup(child, status_write, deadline):
    assert child.returncode == 0
    assert len(process_supervisor._linux_children()) == expected
    if check == "descriptor-limit":
        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert expected > 8
        resource.setrlimit(resource.RLIMIT_NOFILE, (8, hard))

    clear(child, status_write, deadline)

process_supervisor.CLEANUP_SECONDS = 10
process_supervisor._clear_linux_children = check_cleanup
raise SystemExit(process_supervisor.supervise(*map(int, sys.argv[1:4]), sys.argv[4:]))
"""
UNRELATED_CHURN = r"""
import os, subprocess, sys
from scripts.lib import process_supervisor

original_popen = subprocess.Popen
original_output = subprocess.check_output
scans = 0
def record_parent(command, **options):
    global parent
    process = original_popen(command, **options)
    if options.get("process_group") == 0:
        parent = process
    return process

def list_with_churn(*args, **options):
    global scans
    if parent.returncode is not None:
        scans += 1
        assert scans <= 2, "An unrelated PID caused another scan."
    foreign = os.fork()
    if foreign == 0:
        os.setsid()
        os._exit(0)
    try:
        snapshot = original_output(*args, **options)
    finally:
        assert os.waitpid(foreign, 0) == (foreign, 0)
    return snapshot + str(foreign) + "\n"

process_supervisor._MAC_RESCAN_SECONDS = 0
subprocess.Popen = record_parent
subprocess.check_output = list_with_churn
status = process_supervisor.supervise(*map(int, sys.argv[1:4]), sys.argv[4:])
assert scans == 2, "The guardian must scan twice after the workload exits."
raise SystemExit(status)
"""
CUSTOM_GUARDIAN = """
import errno, signal, subprocess, sys
from pathlib import Path
from unittest.mock import patch
from scripts.lib.process_supervisor import launch
original = subprocess.Popen
def replace_guardian(arguments, **options):
    return original([sys.executable, "-c", sys.argv[1], *arguments[4:]], **options)
with patch.object(subprocess, "Popen", side_effect=replace_guardian):
    try:
        workload = launch([sys.executable, "-c", *sys.argv[3:]])
    except OSError as error:
        if sys.argv[2] != "refusal" or error.errno != errno.EINVAL or "SIGCHLD ignored" not in str(error):
            raise
        raise SystemExit(0)
if sys.argv[2] == "refusal":
    workload.close()
    raise AssertionError("The guardian must refuse to start.")
if sys.argv[2] == "release":
    assert sys.stdin.read(1) == "x"
elif sys.argv[2] == "pause":
    print(workload.pid, workload.session_id, flush=True)
    signal.pause()
elif sys.argv[2] == "wait":
    assert workload.wait(timeout=10) == 0
workload.close()
if sys.argv[2] == "release":
    (Path(sys.argv[-1]) / "outer-closed").touch()
"""
BARRIER_OUTER = """
import sys
from pathlib import Path
from scripts.lib import process_supervisor

directory = Path(sys.argv[-1])
read_cleanup_result = process_supervisor._read_cleanup_result
def announce_result_wait(descriptor, deadline):
    (directory / "outer-waiting").touch()
    return read_cleanup_result(descriptor, deadline)

process_supervisor._read_cleanup_result = announce_result_wait
raise SystemExit(process_supervisor.supervise(*map(int, sys.argv[1:4]), sys.argv[4:]))
"""
BARRIER_INNER = """
import sys
from pathlib import Path
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib import process_supervisor

directory = Path(sys.argv[-1])
clear = process_supervisor._clear_session
def wait_for_release(session, child, deadline):
    try:
        wait_until((directory / "end-nested").exists, directory, timeout=10)
    finally:
        clear(session, child, deadline)

process_supervisor._clear_session = wait_for_release
raise SystemExit(process_supervisor.supervise(*map(int, sys.argv[1:4]), sys.argv[4:]))
"""
MISSING_CLEANUP_RESULT = """
import os, sys
from scripts.lib import process_supervisor

clear = process_supervisor._clear_session
def exit_without_result(session, child, deadline):
    clear(session, child, deadline)
    os._exit(71)

process_supervisor._clear_session = exit_without_result
raise SystemExit(process_supervisor.supervise(*map(int, sys.argv[1:4]), sys.argv[4:]))
"""
RUNNER_FIXTURE = """
import sys, unittest
from scripts.lib.process_supervisor import launch

class ManagedFixture(unittest.TestCase):
    def test_scope(self):
        child = self.enterContext(launch([sys.executable, "-c", "import signal; signal.pause()"]))
        print(child.pid, child.session_id, flush=True)
        if sys.stdin.read(1) != "x":
            raise AssertionError("The fixture did not receive its release.")
"""


class ProcessSupervisorTests(unittest.TestCase):
    def owner(self, source: str, *args: str) -> ManagedProcess[bytes]:
        return self.enterContext(launch([sys.executable, "-c", source, *args], cwd=ROOT,
                                        stdout=subprocess.PIPE))

    def line(self, process: ManagedProcess[bytes]) -> list[int]:
        assert process.stdout is not None
        source = cast(BinaryIO, process.stdout)
        deadline = time.monotonic() + TEST_HANG_GUARD_SECS
        line = bytearray()
        while not line.endswith(b"\n"):
            self.assertTrue(select.select([source], [], [], max(0, deadline - time.monotonic()))[0],
                            "The process did not report readiness.")
            data = os.read(source.fileno(), 1)
            self.assertTrue(data, line)
            line.extend(data)
        return [int(field) for field in line.split()]

    def stopped(self, pids: set[int], timeout: float = TEST_HANG_GUARD_SECS) -> None:
        deadline = time.monotonic() + timeout
        for pid in sorted(pids):
            remaining = max(0, deadline - time.monotonic())
            with suppress(ProcessLookupError):
                if sys.platform == "linux":
                    descriptor = os.pidfd_open(pid)
                    try:
                        self.assertTrue(select.select([descriptor], [], [], remaining)[0], f"{pid} still runs.")
                    finally:
                        os.close(descriptor)
                else:
                    queue = select.kqueue()
                    try:
                        subscription = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)
                        self.assertTrue(queue.control([subscription], 1, remaining), f"{pid} still runs.")
                    finally:
                        queue.close()

    def test_owner_sigkill_stops_several_sessions_and_separate_groups(self) -> None:
        control = self.owner(PAUSE)
        owner = self.owner(OWNER, "several", TREE)
        pids: set[int] = set()
        groups: set[int] = set()
        sessions: set[int] = set()
        for _ in range(10):
            row = self.line(owner)
            if len(row) == 1:
                self.assertEqual(row[0], owner.pid)
            else:
                pid, group, session = row
                pids.add(pid)
                groups.add(group)
                sessions.add(session)
        self.assertEqual(len(pids), 9)
        self.assertEqual(len(groups), 6)
        self.assertEqual(len(sessions), 3)
        self.assertFalse(sessions & groups)
        owner.kill()
        self.assertEqual(owner.wait(TEST_HANG_GUARD_SECS), -signal.SIGKILL)
        self.stopped(pids | sessions)
        self.assertIsNone(control.poll())

    def test_owner_sigkill_also_stops_nested_managed_session(self) -> None:
        owner = self.owner(OWNER, "one", NESTED)
        rows = [self.line(owner), self.line(owner)]
        outer, = (row for row in rows if len(row) == 2)
        nested, = (row for row in rows if len(row) == 4)
        self.assertEqual(nested[:2], outer)
        self.assertNotEqual(nested[3], outer[1])
        owner.kill()
        self.assertEqual(owner.wait(TEST_HANG_GUARD_SECS), -signal.SIGKILL)
        self.stopped(set(nested))

    @unittest.skipUnless(sys.platform == "darwin", "macOS uses nested cleanup results.")
    def test_close_waits_for_the_nested_guardian_and_workload(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        outer = self.enterContext(launch(
            [sys.executable, "-c", CUSTOM_GUARDIAN, BARRIER_OUTER, "release",
             CUSTOM_GUARDIAN, BARRIER_INNER, "pause", PAUSE, str(root)],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE))
        nested, nested_guardian = self.line(outer)
        self.addCleanup(self.stopped, {nested, nested_guardian}, 10)
        try:
            assert outer.stdin is not None
            _ = outer.stdin.write(b"x")
            outer.stdin.flush()
            ready = root / "outer-waiting"
            closed = root / "outer-closed"
            wait_until(lambda: ready.exists() or closed.exists(), root, timeout=10, processes=(outer,))
            self.assertTrue(ready.exists(), "The outer guardian must wait after it scans its session.")
            self.assertFalse(closed.exists())
            self.assertEqual(os.getsid(nested), nested_guardian)
        finally:
            (root / "end-nested").touch()
        self.assertEqual(outer.wait(TEST_HANG_GUARD_SECS), 0)
        self.stopped({nested, nested_guardian}, timeout=0)

    @unittest.skipUnless(sys.platform == "darwin", "macOS uses nested cleanup results.")
    def test_close_requires_a_nested_cleanup_result(self) -> None:
        outer = self.owner(CUSTOM_GUARDIAN, MISSING_CLEANUP_RESULT, "pause", PAUSE)
        nested, nested_guardian = self.line(outer)
        with self.assertRaises(ChildProcessError):
            outer.close()
        self.stopped({nested, nested_guardian}, timeout=0)

    def test_close_handles_an_unregistered_session(self) -> None:
        outer = self.enterContext(launch([sys.executable, "-c", """
import os, signal, sys
if os.fork() == 0:
    os.setsid()
    print(os.getpid(), flush=True)
    sys.stdin.read(1)
    os._exit(0)
signal.pause()
"""], stdin=subprocess.PIPE, stdout=subprocess.PIPE))
        child, = self.line(outer)
        self.assertEqual(os.getsid(child), child)
        if sys.platform == "linux":
            outer.close()
            self.assertFalse(Path(f"/proc/{child}").exists(), "The detached child must be reaped.")
        else:
            # Keep a duplicate input writer to check the macOS ownership limit.
            assert outer.stdin is not None
            with os.fdopen(os.dup(outer.stdin.fileno()), "wb"):
                outer.close()
                self.assertEqual(os.getsid(child), child)
            self.stopped({child})

    def test_close_rescans_when_a_listed_parent_forks_and_exits(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        record = root / "child.pid"
        with launch([sys.executable, "-c", CUSTOM_GUARDIAN, FORK_DURING_CLEANUP, "close",
                     FORKING_PARENT, str(record)], cwd=ROOT, stdin=subprocess.PIPE) as owner:
            assert owner.stdin is not None
            try:
                self.assertEqual(owner.wait(15), 0)
                child = int(record.read_text())
                self.stopped({child}, timeout=5)
                if sys.platform == "linux":
                    self.assertFalse(Path(f"/proc/{child}").exists(), "The adopted child must be reaped.")
            finally:
                owner.stdin.close()

    def check_linux_cleanup(self, descendants: int, *, low_descriptor_limit: bool = False) -> None:
        with launch([sys.executable, "-c", CUSTOM_GUARDIAN, CHECK_LINUX_CLEANUP, "wait",
                     ADOPTED_CHILDREN, str(descendants), "descriptor-limit" if low_descriptor_limit else "default"],
                    cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as owner:
            assert owner.stdin is not None
            try:
                children = self.line(owner)
                self.assertEqual(len(children), descendants)
                self.assertEqual(owner.wait(15), 0)
                self.assertEqual([pid for pid in children if Path(f"/proc/{pid}").exists()], [])
            finally:
                owner.stdin.close()

    @unittest.skipUnless(sys.platform == "linux", "Linux guardians adopt orphaned descendants.")
    def test_cleanup_returns_when_the_guardian_has_no_children(self) -> None:
        self.check_linux_cleanup(0)

    @unittest.skipUnless(sys.platform == "linux", "Linux guardians adopt orphaned descendants.")
    def test_cleanup_reaps_adopted_descendants(self) -> None:
        self.check_linux_cleanup(2)

    @unittest.skipUnless(sys.platform == "linux", "Linux guardians adopt orphaned descendants.")
    def test_cleanup_reaps_children_with_a_low_descriptor_limit(self) -> None:
        self.check_linux_cleanup(9, low_descriptor_limit=True)

    @unittest.skipUnless(sys.platform == "darwin", "macOS scans the system process list.")
    def test_close_ignores_unrelated_pid_churn(self) -> None:
        owner = self.owner(CUSTOM_GUARDIAN, UNRELATED_CHURN, "wait", "pass")
        output, errors = owner.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(owner.returncode, 0, output + errors)

    def test_caught_launch_failure_does_not_fail_outer_cleanup(self) -> None:
        missing = Path(self.enterContext(tempfile.TemporaryDirectory())) / "missing"
        outer = self.owner("""
import sys
from pathlib import Path
from scripts.lib.process_supervisor import launch
try:
    _ = launch([sys.executable, "-c", "pass"], cwd=Path(sys.argv[1]))
except FileNotFoundError:
    print(1, flush=True)
else:
    raise AssertionError("The launch must fail.")
""", str(missing))
        self.assertEqual(self.line(outer), [1])
        self.assertEqual(outer.wait(TEST_HANG_GUARD_SECS), 0)
        outer.close()

    @unittest.skipUnless(sys.platform == "linux", "Linux guardians adopt orphaned descendants.")
    def test_launch_refuses_when_the_guardian_reads_sigchld_as_ignored(self) -> None:
        owner = self.owner(CUSTOM_GUARDIAN, """
import signal, sys
from unittest.mock import patch
from scripts.lib import process_supervisor

getsignal = signal.getsignal
def ignored_sigchld(signum):
    return signal.SIG_IGN if signum == signal.SIGCHLD else getsignal(signum)

with patch.object(signal, "getsignal", side_effect=ignored_sigchld):
    raise SystemExit(process_supervisor.supervise(*map(int, sys.argv[1:4]), sys.argv[4:]))
""", "refusal", "pass")
        self.assertEqual(owner.wait(TEST_HANG_GUARD_SECS), 0)

    @unittest.skipUnless(sys.platform == "linux", "Linux provides PID descriptors.")
    def test_workload_signal_uses_a_pidfd(self) -> None:
        child = self.owner("import os, signal; print(os.getpid(), flush=True); signal.pause()")
        self.assertEqual(self.line(child), [child.pid])
        with patch.object(os, "kill", side_effect=AssertionError("PID signal")):
            child.terminate()
        self.assertEqual(child.wait(TEST_HANG_GUARD_SECS), -signal.SIGTERM)

    @unittest.skipUnless(sys.platform == "linux", "Linux guardians adopt orphaned descendants.")
    def test_guardian_reaps_an_adopted_child_while_the_owner_pipe_stays_open(self) -> None:
        for workload_exits_first in (False, True):
            with self.subTest(workload_exits_first=workload_exits_first), launch([sys.executable, "-c", """
import os, signal, sys
parent = os.fork()
if parent == 0:
    if os.fork() == 0:
        print(os.getpid(), flush=True)
        sys.stdin.read(1)
    os._exit(0)
assert os.waitpid(parent, 0) == (parent, 0)
print(0, flush=True)
if sys.argv[1] == "running":
    signal.pause()
""", "exited" if workload_exits_first else "running"],
                    cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as workload:
                first, = self.line(workload)
                second, = self.line(workload)
                self.assertIn(0, (first, second))
                child = first or second
                descriptor = os.pidfd_open(child)
                try:
                    events = select.poll()
                    events.register(descriptor, select.POLLHUP)
                    if workload_exits_first:
                        self.assertEqual(workload.wait(TEST_HANG_GUARD_SECS), 0)
                    assert workload.stdin is not None
                    self.assertEqual(workload.stdin.write(b"x"), 1)
                    workload.stdin.flush()
                    self.assertTrue(any(flags & select.POLLHUP for _, flags in events.poll(10_000)),
                                    "The guardian must reap the adopted child before owner cleanup.")
                    if not workload_exits_first:
                        self.assertIsNone(workload.poll())
                    self.assertFalse(Path(f"/proc/{child}").exists())
                finally:
                    os.close(descriptor)

    def test_workload_exit_keeps_its_guardian_and_descendant_until_close(self) -> None:
        workload = self.owner("""
import os, signal
if os.fork() == 0:
    print(os.getpid(), flush=True)
    os.close(1)
    signal.pause()
    os._exit(0)
raise SystemExit(23)
""")
        descendant, = self.line(workload)
        self.assertEqual(workload.wait(TEST_HANG_GUARD_SECS), 23)
        self.assertEqual(os.getsid(descendant), workload.session_id)
        self.assertEqual(os.getsid(workload.session_id), workload.session_id)
        workload.close()
        self.stopped({descendant, workload.session_id})

    def test_owner_sigkill_before_startup_acknowledgement(self) -> None:
        owner = self.owner(INTERRUPT_STARTUP, SPAWN_BEFORE_ACK)
        pid, guardian = self.line(owner)
        self.assertEqual(owner.wait(TEST_HANG_GUARD_SECS), -signal.SIGKILL)
        self.stopped({pid, guardian})

    def test_owner_sigkill_after_acknowledgement_with_forked_and_executed_siblings(self) -> None:
        owner = self.owner(OWNER, "siblings", PAUSE)
        workload, guardian, forked, executed = self.line(owner)
        owner.kill()
        self.assertEqual(owner.wait(TEST_HANG_GUARD_SECS), -signal.SIGKILL)
        self.stopped({workload, guardian})
        self.assertEqual(os.getsid(forked), owner.session_id)
        self.assertEqual(os.getsid(executed), owner.session_id)
        owner.close()
        self.stopped({forked, executed, owner.session_id})

    def test_pid_signal_status_and_output_eof_belong_to_workload(self) -> None:
        child = self.owner("import os, signal; print(os.getpid(), flush=True); signal.pause()")
        pid, = self.line(child)
        self.assertEqual(pid, child.pid)
        self.assertNotEqual(pid, child.session_id)
        self.assertEqual(os.getpgid(pid), pid)
        child.send_group_signal(signal.SIGTERM)
        self.assertEqual(child.communicate(timeout=TEST_HANG_GUARD_SECS), (b"", b""))
        self.assertEqual(child.returncode, -signal.SIGTERM)
        self.assertEqual(os.getsid(child.session_id), child.session_id)

    def test_startup_failure_reports_the_failed_executable(self) -> None:
        with self.assertRaises(FileNotFoundError) as failure:
            _ = process_supervisor.launch(["/nonexistent/workload-fixture"])
        self.assertEqual(cast(object, failure.exception.filename), "/nonexistent/workload-fixture")

    @unittest.skipUnless(sys.platform == "linux", "The Linux guardian passes the caller environment as is.")
    def test_guardian_preserves_caller_environment_bytes(self) -> None:
        locales: tuple[dict[str, str], ...] = ({}, {"LC_CTYPE": ""}, {"LC_CTYPE": "C"})
        for locale in locales:
            environment = {"FIXTURE_VALUE": os.fsdecode(b"original\n\xff"), **locale}
            expected = b"".join(os.fsencode(f"{key}={value}") + b"\0" for key, value in environment.items())
            with self.subTest(locale=locale), launch(
                ["/usr/bin/env", "-0"], env=environment, stdout=subprocess.PIPE,
            ) as workload:
                output, _ = workload.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(workload.returncode, 0)
                self.assertEqual(output, expected)

    def test_unittest_and_module_runner_own_their_workloads(self) -> None:
        folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="supervisor-runner-")))
        fixture = folder / "test_supervised_fixture.py"
        _ = fixture.write_text(RUNNER_FIXTURE)
        python = str(ROOT / "scripts/lib/py")
        runner = str(ROOT / "scripts/test_times/module_run.py")
        for arguments, abort in (([python, "-m", "unittest", fixture.stem], False),
                                 ([python, runner, str(folder), fixture.name], False),
                                 ([python, runner, str(folder), fixture.name], True)):
            with self.subTest(arguments=arguments, abort=abort), process_supervisor.launch(
                    arguments, cwd=folder, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    env={**os.environ, "PYTHONPATH": str(folder)}) as process:
                pid, guardian = self.line(process)
                if abort:
                    process.send_group_signal(signal.SIGKILL)
                else:
                    assert process.stdin is not None
                    _ = process.stdin.write(b"x")
                    process.stdin.flush()
                self.assertEqual(process.wait(TEST_HANG_GUARD_SECS), -signal.SIGKILL if abort else 0)
                self.stopped({pid, guardian})

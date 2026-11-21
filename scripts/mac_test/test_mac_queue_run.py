"""Exercise placement return and SSH routing with executable stand-ins."""
from __future__ import annotations

import ast
from collections import defaultdict
from contextlib import redirect_stderr
import fcntl
import io
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from typing import TextIO, cast
from unittest import mock
import unittest

from scripts.direct.wait_for_test_state import wait_until
from scripts.lib import process_group
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import mac_queue_run
from scripts.lint.memory_test_support import config_environment, write_config_fixture


QUEUE = r'''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
root = Path(os.environ["FAKE_QUEUE_STATE"])
def option(name):
    return args[args.index(name)+1]
with (root / "events").open("a") as stream:
    stream.write(json.dumps(args)+"\n")
if args[0] == "place-step":
    if os.environ.get("FAKE_QUEUE_BUSY") == "1":
        print("waiting for the Mac")
        raise SystemExit(78)
    if os.environ.get("FAKE_QUEUE_WAIT") == "1":
        (root / "waiting").write_text("waiting")
        time.sleep(60)
    if os.environ.get("FAKE_QUEUE_REFUSE") == "1":
        print("refused\tclosed")
        raise SystemExit(2)
    Path(option("--assignment-file")).write_text(json.dumps({"episode":option("--episode"),"attempt":"attempt"}))
    print("mac\tmac\tfake-mac\t0\t"+option("--episode")+"\tattempt")
else:
    if os.environ.get("FAKE_RETURN_FAIL") == "1":
        raise SystemExit(3)
    print('{"state":"closed"}')
'''

SSH = r'''
import json, os, shlex, sys
from pathlib import Path
root = Path(os.environ["FAKE_QUEUE_STATE"])
args = shlex.split(sys.argv[-1])
if "mac_stopped=" in args[-1]:
    (root / "stop").write_text(args[-1])
    if os.environ.get("FAKE_STOP_FAIL") == "1":
        raise SystemExit(255)
    print("mac_stopped=4567", flush=True)
    raise SystemExit(0)
(root / "remote").write_text(json.dumps(args))
print("mac_group=4567", flush=True)
if os.environ.get("FAKE_SSH_HOLD") == "1":
    (root / "running").write_text(str(os.getpid()))
    print("Mac command started", flush=True)
    # The real ssh sends the end of its input to the Mac wrapper, which stops the work.
    _ = sys.stdin.read()
    (root / "input_closed").write_text("closed")
    raise SystemExit(255)
status = int(os.environ.get("FAKE_MAC_STATUS","0"))
print("Mac output", flush=True)
if status == 125:
    print("mac cargo gate refused: placement closed; run it through mac_queue_run.py", file=sys.stderr)
print(f"mac_exit={status}", flush=True)
'''


def plain_ssh_builds(root: Path) -> list[str]:
    forbidden = re.compile(r"\b(?:ssh|on_mac)\b[^\n]*(?:\bcargo(?:\s|$)|\brustc(?:\s|$)|\bnextest(?:\s|$)|scripts/lint/lint\b|/target/(?:debug|release)/)")
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".py", ".sh") or path.name.startswith("test_") or "test_support" in path.name:
            continue
        # Item 11 changes offload.py after the queue merge.
        if path.relative_to(root).as_posix() == "mac_test/offload.py":
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.lstrip().startswith("#") and forbidden.search(line):
                findings.append(f"{path.relative_to(root)}:{number}: {line.strip()}")
    return findings


# The queue releases on process end, so these files set no signal handler (user rule, 2026-11-14).
NO_HANDLER_FILES = ("lint/nextest_tokens.py", "mac_test/mac_queue_run.py", "mac_test/cargo_gate/gate.zsh")
SIGNAL_HANDLER = re.compile(r"\bsignal\.(?:signal|SIG_IGN|SIG_DFL|pthread_sigmask|setitimer)\b|\bfrom signal import\b"
                            + r"|^\s*trap\b|\bTRAP[A-Z]+\b")


def signal_handlers(root: Path, names: tuple[str, ...]) -> list[str]:
    findings: list[str] = []
    for name in names:
        for number, line in enumerate((root / name).read_text().splitlines(), 1):
            if not line.lstrip().startswith("#") and SIGNAL_HANDLER.search(line):
                findings.append(f"{name}:{number}: {line.strip()}")
    return findings


def queue_fixture(root: Path, ssh_source: str = SSH) -> dict[str, str]:
    queue = root / "queue.py"
    _ = queue.write_text(QUEUE)
    ssh = root / "ssh"
    _ = ssh.write_text(f"#!{sys.executable}\n" + ssh_source)
    ssh.chmod(0o755)
    environment = {**os.environ, **config_environment(root / "config"), "FAKE_QUEUE_STATE": str(root),
                   "PATH": str(root) + os.pathsep + os.environ["PATH"], "HOME": str(root)}
    _ = write_config_fixture(root / "config/mac_gate.conf", "admission_client_command="
                             + shlex.join([sys.executable, str(queue)]) + "\n")
    return environment


class QueueRunTests(unittest.TestCase):
    def command(self, root: Path, kind: str = "shot") -> list[str]:
        return [sys.executable, "-m", "scripts.mac_test.mac_queue_run", "--what", "camera shot", "--kind", kind,
                "--package", "hana", "--worktree", str(root), "--cwd", "/Mac scratch/checkout", "--",
                "/bin/zsh", "-c", "cargo build -p hana; printf '%s' 'quoted value'"]

    def events(self, root: Path) -> list[list[str]]:
        return [cast(list[str], json.loads(line)) for line in (root / "events").read_text().splitlines()]

    def test_every_run_returns_the_placement_and_keeps_the_mac_status(self) -> None:
        cases = ((0, False, False), (23, False, False), (125, False, False), (2, True, False), (0, False, True), (2, True, True))
        for kind in ("shot", "install", "probe", "run"):
            for status, queue_refuses, return_fails in cases:
                with (self.subTest(kind=kind, status=status, queue_refuses=queue_refuses, return_fails=return_fails),
                      tempfile.TemporaryDirectory() as temporary):
                    self.check_returned_placement(Path(temporary), kind, status, queue_refuses, return_fails)

    def check_returned_placement(self, root: Path, kind: str, status: int, queue_refuses: bool, return_fails: bool) -> None:
        environment = {**queue_fixture(root), "FAKE_MAC_STATUS": str(status),
                       "FAKE_QUEUE_REFUSE": "1" if queue_refuses else "0", "FAKE_RETURN_FAIL": "1" if return_fails else "0"}
        result = subprocess.run(self.command(root, kind), capture_output=True, text=True, env=environment,
                                timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(result.returncode, status or (1 if return_fails else 0), result.stdout + result.stderr)
        events = self.events(root)
        # A Mac placement has no local grant, so each run returns it with a cancel.
        self.assertEqual([event[0] for event in events], ["place-step", "cancel-placement"])
        placed, released = events
        self.assertIn("--once", placed)
        self.assertIn("--wait-events", placed)
        self.assertEqual(placed[placed.index("--target") + 1], "mac")
        self.assertEqual(placed[placed.index("--kind") + 1], kind)
        self.assertEqual(released[released.index("--episode") + 1], placed[placed.index("--episode") + 1])
        self.assertEqual(released[released.index("--pid") + 1], placed[placed.index("--pid") + 1])
        # No receipt outlives its run, a refused return included.
        self.assertEqual(list((root / mac_queue_run.RECEIPTS).iterdir()), [])
        if return_fails:
            self.assertIn("the placement was not returned", result.stderr)
        if queue_refuses:
            self.assertEqual(released[released.index("--reason") + 1], "the Mac run did not start")
            self.assertFalse((root / "remote").exists())
        else:
            self.assertEqual(released[released.index("--reason") + 1], f"the Mac run ended with status {status}")
            remote = cast(list[str], json.loads((root / "remote").read_text()))
            wrapper = shlex.split(remote[2])
            work = shlex.split(wrapper[0].removeprefix("work="))
            self.assertEqual(work[:3], ["cd", "/Mac scratch/checkout", "&&"])
            self.assertEqual(work[3], "MAC_PLACEMENT=" + placed[placed.index("--episode") + 1] + "/attempt")
            self.assertEqual(work[4:], self.command(root)[-3:])
            self.assertEqual(result.stdout.splitlines()[-1], f"mac_exit={status}")

    def test_no_wait_returns_the_busy_status_after_one_queue_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**queue_fixture(root), "FAKE_QUEUE_BUSY": "1"}
            command = self.command(root, "run")
            command.insert(command.index("--"), "--no-wait")
            result = subprocess.run(command, capture_output=True, text=True, env=environment,
                                    timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, mac_queue_run.MAC_BUSY_STATUS, result.stdout + result.stderr)
            self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])
            self.assertFalse((root / "remote").exists())

    def test_caller_watch_must_be_ready_before_placement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root)
            reader, writer = os.pipe()
            os.close(writer)
            try:
                command = self.command(root)
                command[3:3] = ["--caller-fd", str(reader)]
                result = subprocess.run(command, env=environment, start_new_session=True, pass_fds=(reader,),
                                        capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            finally:
                os.close(reader)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("caller watch did not start", result.stderr)
            self.assertFalse((root / "events").exists())

    def test_a_caller_watch_failure_after_ready_stops_the_queue_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {**queue_fixture(root), "FAKE_SSH_HOLD": "1"}
            reader, writer = os.pipe()
            try:
                command = self.command(root)
                command[3:3] = ["--caller-fd", str(reader)]
                with subprocess.Popen(command, env=environment, start_new_session=True, pass_fds=(reader,),
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as queue:
                    try:
                        assert queue.stdout is not None
                        queue_output = cast(TextIO, queue.stdout)
                        self.assertTrue(select.select([queue_output], [], [], TEST_HANG_GUARD_SECS)[0])
                        self.assertEqual(queue_output.readline().strip(), "Mac command started")
                        processes = subprocess.check_output(["ps", "-axo", "pid=,ppid=,args="], text=True)
                        watchers: list[int] = []
                        for line in processes.splitlines():
                            fields = line.split(maxsplit=2)
                            if len(fields) == 3 and fields[1] == str(queue.pid) and " watch-caller " in fields[2]:
                                watchers.append(int(fields[0]))
                        self.assertEqual(len(watchers), 1)
                        os.kill(watchers[0], signal.SIGKILL)
                        output, errors = queue.communicate(timeout=10)
                    finally:
                        if queue.poll() is None:
                            send_process_group(queue.pid, signal.SIGKILL)
                            _ = queue.communicate()
                self.assertEqual(queue.returncode, -signal.SIGTERM, output + errors)
                self.assertIn("caller watch ended unexpectedly", errors)
                self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])
                self.assertIn("signal_group TERM", (root / "stop").read_text())
            finally:
                os.close(reader)
                os.close(writer)

    def save_ended_run(self, root: Path, callback: Path) -> Path:
        directory = root / mac_queue_run.RUNS / "stale"
        directory.mkdir(parents=True)
        (directory / "lock").touch()
        record = directory / "run.json"
        _ = record.write_text(json.dumps({"lifecycle_script": str(callback),
                                         "arguments": [], "host": "fake-mac"}))
        return directory

    def test_missing_recovery_callback_fails_the_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root)
            stale = self.save_ended_run(root, root / "gone")
            result = subprocess.run(self.command(root, "run"), env=environment, capture_output=True,
                                    text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("cleanup of an ended run failed", result.stderr)
            self.assertTrue((stale / "run.json").exists())
            self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])

    def test_failed_cleanup_leaves_the_record_for_the_next_run(self) -> None:
        lifecycle = """
import json, os, sys
from pathlib import Path
phase, argv_file, *command = sys.argv[1:]
if phase == 'prepare':
    Path(argv_file).write_text(json.dumps(command))
else:
    with (Path(os.environ['FAKE_QUEUE_STATE']) / 'cleanup_calls').open('a') as out:
        print(argv_file, file=out)
    raise SystemExit(int(os.environ.get('FAKE_CLEANUP_STATUS', '0')))
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root)
            callback = root / "lifecycle"
            _ = callback.write_text(f"#!{sys.executable}\n" + lifecycle)
            callback.chmod(0o755)
            command = self.command(root, "run")
            command[command.index("--"):command.index("--")] = ["--lifecycle-script", str(callback)]
            result = subprocess.run(command, env={**environment, "FAKE_CLEANUP_STATUS": "19"},
                                    capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
            (saved,) = (root / mac_queue_run.RUNS).glob("*/run.json")
            recovered = subprocess.run(self.command(root, "run"), env=environment,
                                       capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
            called = str(saved.with_name("argv.json"))
            self.assertEqual((root / "cleanup_calls").read_text().splitlines(), [called, called])
            self.assertFalse(saved.exists())

    def test_a_signal_ends_the_run_with_its_default_action(self) -> None:
        for waiting, signum in ((False, signal.SIGTERM), (False, signal.SIGHUP), (True, signal.SIGTERM),
                                (False, signal.SIGINT)):
            with self.subTest(waiting=waiting, signum=signum), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                environment = {**queue_fixture(root), "FAKE_QUEUE_WAIT": "1" if waiting else "0", "FAKE_SSH_HOLD": "1"}
                with subprocess.Popen(self.command(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, env=environment, start_new_session=True) as process:
                    try:
                        wait_until((root / ("waiting" if waiting else "running")).exists, root, processes=(process,))
                        if signum == signal.SIGINT:
                            # The queue reads the group before it forwards this line.
                            assert process.stdout is not None
                            queue_output = cast(TextIO, process.stdout)
                            self.assertTrue(select.select([queue_output], [], [], TEST_HANG_GUARD_SECS)[0])
                            self.assertEqual(queue_output.readline().strip(), "Mac command started")
                        if waiting:
                            # A signal to the group also ends the queue call that waits.
                            send_process_group(process.pid, signum)
                        else:
                            process.send_signal(signum)
                        output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    finally:
                        if process.poll() is None:
                            send_process_group(process.pid, signal.SIGKILL)
                            _ = process.communicate()
                self.assertEqual(process.returncode, -signum, output + errors)
                events = [event[0] for event in self.events(root)]
                if signum == signal.SIGINT:
                    # Python's own KeyboardInterrupt runs the finally path, which returns the placement.
                    self.assertEqual(events, ["place-step", "cancel-placement"])
                    self.assertEqual(self.events(root)[-1][-1], "the Mac run ended with status 1")
                    self.assertIn("signal_group TERM", (root / "stop").read_text())
                else:
                    # The queue ends the placement when this process ends.
                    self.assertEqual(events, ["place-step"])
                    if not waiting:
                        wait_until((root / "input_closed").exists, root)
                    # The run's state folder stays until the next run cleans it.
                    runs = root / mac_queue_run.RUNS
                    self.assertEqual(len(list(runs.iterdir())), 1)
                    self.assertEqual(len(list((root / mac_queue_run.RECEIPTS).iterdir())), 1)
                    after = subprocess.run(self.command(root), capture_output=True, text=True,
                                           env={**environment, "FAKE_SSH_HOLD": "0", "FAKE_QUEUE_WAIT": "0"},
                                           timeout=TEST_HANG_GUARD_SECS)
                    self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
                    self.assertEqual(list(runs.iterdir()), [])
                    self.assertEqual(list((root / mac_queue_run.RECEIPTS).iterdir()), [])

    def test_the_mac_work_ends_when_the_ssh_input_closes(self) -> None:
        zsh, setsid = shutil.which("zsh"), shutil.which("setsid")
        if zsh is None or setsid is None:
            self.skipTest("zsh or setsid is not installed")
        for closes in (True, False):
            with self.subTest(closes=closes), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                # The app starts outside the group, as Terminal starts hana on the Mac.
                work = (f"{setsid} -f sh -c 'echo $$ > {root}/app.new; mv {root}/app.new {root}/app; exec sleep 60' </dev/null >/dev/null 2>&1; "
                        + f"while [[ ! -s {root}/app ]]; do sleep 0.05; done; echo started $$; "
                        + ("exec sleep 60" if closes else "exit 7"))
                on_close = (f"kill -0 $(<{root}/work) 2>/dev/null && print alive > {root}/order"
                            + f" || print ended > {root}/order; kill -TERM $(<{root}/app)")
                wrapper = mac_queue_run.remote_wrapper(work, on_close, 20).replace("/bin/zsh", zsh)
                # Its own session stands in for the SSH session on the Mac.
                with subprocess.Popen([zsh, "-c", wrapper], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      text=True, start_new_session=True) as process:
                    try:
                        assert process.stdout is not None and process.stdin is not None
                        self.assertEqual(process.stdout.readline(), f"mac_group={process.pid}\n")
                        started = cast(str, process.stdout.readline())
                        self.assertRegex(started, r"\Astarted \d+\n\Z")
                        _ = (root / "work").write_text(started.split()[1])
                        app_end = os.pidfd_open(int((root / "app").read_text()))
                        self.addCleanup(os.close, app_end)
                        if closes:
                            work_end = os.pidfd_open(int(started.split()[1]))
                            self.addCleanup(os.close, work_end)
                            process.stdin.close()
                            for end in (work_end, app_end):
                                self.assertEqual(select.select([end], [], [], TEST_HANG_GUARD_SECS)[0], [end])
                            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGTERM)
                            # The group ends first; then the close step stops the app.
                            self.assertEqual((root / "order").read_text(), "ended\n")
                        else:
                            output = cast(str, process.stdout.read())
                            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 0)
                            self.assertEqual(output.splitlines()[-1], "mac_exit=7")
                            # A run that ends by itself leaves the close step to its own cleanup.
                            self.assertFalse((root / "order").exists())
                            os.kill(int((root / "app").read_text()), signal.SIGTERM)
                    finally:
                        if process.poll() is None:
                            send_process_group(process.pid, signal.SIGKILL)
                            _ = process.wait()

    def test_callers_have_no_plain_ssh_build_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _ = (root / "caller.sh").write_text("ssh mac cargo build\n")
            self.assertEqual(len(plain_ssh_builds(root)), 1)
        self.assertEqual(plain_ssh_builds(Path(__file__).resolve().parents[1]), [])

    def test_the_queue_files_set_no_signal_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lines = ("signal.signal(signal.SIGTERM, stop)", "trap cleanup EXIT", "  trap - INT",
                     "TRAPINT() { exit }", "for each in (signal.SIG_IGN,): pass")
            for number, line in enumerate(lines):
                _ = (root / f"case{number}").write_text("# trap is fine in a comment\n" + line + "\n")
            self.assertEqual(len(signal_handlers(root, tuple(f"case{number}" for number in range(len(lines))))), len(lines))
        self.assertEqual(signal_handlers(Path(__file__).resolve().parents[1], NO_HANDLER_FILES), [])


    def test_every_registered_step_records_nonzero_statuses_and_exceptions(self) -> None:
        def refuse() -> int:
            return 23

        def crash() -> int:
            raise OSError("fixture action failed")

        for step in mac_queue_run.QueueFailureStep:
            for action, status in ((refuse, 23), (crash, 2)):
                with self.subTest(step=step, status=status), tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary) / "failure-record"
                    steps = mac_queue_run.QueueSteps(path)
                    with redirect_stderr(io.StringIO()):
                        self.assertEqual(steps.execute(step, action), status)
                    failure = mac_queue_run.QueueStepFailure(step, status)
                    self.assertEqual(mac_queue_run.read_queue_failures(path), (failure,))

    def test_every_registered_step_reports_a_failed_record_write(self) -> None:
        for step in mac_queue_run.QueueFailureStep:
            with self.subTest(step=step), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "missing" / "failure-record"
                steps = mac_queue_run.QueueSteps(path)
                output = io.StringIO()
                with redirect_stderr(output):
                    self.assertEqual(steps.execute(step, lambda: 23), 23)
                self.assertIn(f"cannot record {step.value} failure (status 23)", output.getvalue())
                self.assertEqual(mac_queue_run.read_queue_failures(path), ())
                path.parent.mkdir()
                with redirect_stderr(output):
                    self.assertEqual(steps.execute(step, lambda: 2), 2)
                self.assertEqual(mac_queue_run.read_queue_failures(path),
                                 (mac_queue_run.QueueStepFailure(step, 23), mac_queue_run.QueueStepFailure(step, 2)))


    def test_recovery_records_a_cleanup_status_and_fails_the_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = queue_fixture(root)
            callback = root / "cleanup"
            _ = callback.write_text(f"#!{sys.executable}\nraise SystemExit(19)\n")
            callback.chmod(0o755)
            stale = self.save_ended_run(root, callback)
            record = root / "failure"
            command = self.command(root, "run")
            command[3:3] = ["--failure-file", str(record)]
            result = subprocess.run(command, env=environment, capture_output=True,
                                    text=True, timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(result.returncode, 19, result.stdout + result.stderr)
            self.assertEqual(record.read_text(), "recovery-cleanup 19\n")
            self.assertTrue((stale / "run.json").is_file())
            self.assertFalse((root / "remote").exists())
            self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])

    def test_recovery_records_file_errors_and_closes_the_lock(self) -> None:
        for expected_step in ("receipt-removal", "run-directory-removal", "run-directory-closure"):
            with self.subTest(step=expected_step), tempfile.TemporaryDirectory() as temporary:
                self.check_recovery_file_error(Path(temporary), expected_step)

    def check_recovery_file_error(self, root: Path, expected_step: str) -> None:
        environment = queue_fixture(root)
        callback = root / "cleanup"
        _ = callback.write_text(f"#!{sys.executable}\n")
        callback.chmod(0o755)
        stale = self.save_ended_run(root, callback)
        receipt = root / mac_queue_run.RECEIPTS / "stale.json"
        record = root / "failure"
        original_open, original_close = os.open, os.close
        original_unlink, original_rmtree = Path.unlink, shutil.rmtree
        descriptors: set[int] = set()
        lock_closed = False

        def open_lock(path: Path, flags: int, mode: int = 0o777, *,
                      dir_fd: int | None = None) -> int:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            if path == stale / "lock":
                descriptors.add(descriptor)
            return descriptor

        def close_lock(descriptor: int) -> None:
            nonlocal lock_closed
            original_close(descriptor)
            if descriptor in descriptors:
                descriptors.remove(descriptor)
                lock_closed = True
                if expected_step == "run-directory-closure":
                    raise OSError("fixture lock closure failed")

        def unlink(path: Path, missing_ok: bool = False) -> None:
            if expected_step == "receipt-removal" and path == receipt:
                raise OSError("fixture receipt removal failed")
            original_unlink(path, missing_ok=missing_ok)

        def rmtree(path: Path, ignore_errors: bool = False, *,
                   dir_fd: int | None = None) -> None:
            if expected_step == "run-directory-removal" and path == stale:
                if ignore_errors:
                    return
                raise OSError("fixture directory removal failed")
            original_rmtree(path, ignore_errors=ignore_errors, dir_fd=dir_fd)

        with (mock.patch.dict(os.environ, environment),
              mock.patch.object(os, "open", side_effect=open_lock),
              mock.patch.object(os, "close", side_effect=close_lock),
              mock.patch.object(Path, "unlink", autospec=True, side_effect=unlink),
              mock.patch.object(shutil, "rmtree", side_effect=rmtree),
              redirect_stderr(io.StringIO())):
            status = mac_queue_run.main(["--failure-file", str(record), *self.command(root, "run")[3:]])
        self.assertEqual(status, 2)
        self.assertTrue(lock_closed)
        self.assertFalse(descriptors)
        self.assertEqual(record.read_text(), f"{expected_step} 2\n")
        self.assertFalse((root / "remote").exists())

    def test_queue_records_a_control_exit_before_running_finalizers(self) -> None:
        exits: tuple[BaseException, ...] = (KeyboardInterrupt(), SystemExit(23))
        for error in exits:
            with self.subTest(error=type(error)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                record = root / "failure"
                environment = queue_fixture(root)

                def check_record_before_release() -> list[str]:
                    if (root / "events").exists():
                        self.assertEqual(record.read_text() if record.exists() else "", "transport 1\n")
                    return admission_command()

                admission_command = mac_queue_run.admission_command
                with (mock.patch.dict(os.environ, environment),
                      mock.patch.object(mac_queue_run, "stream_remote", side_effect=error),
                      mock.patch.object(mac_queue_run, "admission_command", side_effect=check_record_before_release),
                      redirect_stderr(io.StringIO()),
                      self.assertRaises(type(error)) as raised):
                    _ = mac_queue_run.main(["--failure-file", str(record), *self.command(root)[3:]])
                self.assertIs(raised.exception, error)
                self.assertEqual(record.read_text(), "transport 1\n")
                self.assertEqual([event[0] for event in self.events(root)], ["place-step", "cancel-placement"])
                self.assertEqual(list((root / mac_queue_run.RECEIPTS).iterdir()), [])
                self.assertEqual(list((root / mac_queue_run.RUNS).iterdir()), [])

    def test_watcher_failure_stops_the_group_before_raising_the_original_interrupt(self) -> None:
        for termination_fails in (False, True):
            with self.subTest(termination_fails=termination_fails):
                steps = mac_queue_run.QueueSteps(None)
                interrupt = KeyboardInterrupt()
                actions: list[str] = []
                pending_at_termination: list[int] = []

                def start_watcher(_caller: int, failed: threading.Event, queue_steps: mac_queue_run.QueueSteps) -> None:
                    def stop_watcher() -> None:
                        actions.append("watcher shutdown")
                        failed.set()

                    _ = queue_steps.defer(mac_queue_run.QueueFailureStep.CALLER_WATCH_SHUTDOWN, stop_watcher)

                def interrupt_run(_args: mac_queue_run.Arguments, queue_steps: mac_queue_run.QueueSteps) -> int:
                    _ = queue_steps.defer(mac_queue_run.QueueFailureStep.CLEANUP, lambda: actions.append("cleanup"))
                    raise interrupt

                def stop_group() -> None:
                    actions.append("group termination")
                    pending_at_termination.append(steps.checkpoint())
                    if termination_fails:
                        raise SystemExit(23)

                with (mock.patch.object(mac_queue_run, "QueueSteps", return_value=steps),
                      mock.patch.object(mac_queue_run, "caller_watch", side_effect=start_watcher),
                      mock.patch.object(mac_queue_run, "run", side_effect=interrupt_run),
                      mock.patch.object(process_group, "terminate_owned_process_group", side_effect=stop_group),
                      redirect_stderr(io.StringIO()),
                      self.assertRaises(KeyboardInterrupt) as raised):
                    _ = mac_queue_run.main(["--caller-fd", "0", *self.command(Path.cwd())[3:]])

                self.assertIs(raised.exception, interrupt)
                self.assertEqual(actions, ["cleanup", "watcher shutdown", "group termination"],
                                 "Group termination must run after the other finalizers.")
                self.assertEqual(pending_at_termination, [0])
                self.assertEqual(steps.finalizers, [])

    def check_interrupted_run_lock(self, *, fail_finalizers: bool) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / "failure"
            interrupt = KeyboardInterrupt("fixture lock interrupt")
            original_rmtree, original_close = shutil.rmtree, os.close
            lock_descriptor = -1
            observations: list[tuple[str, str]] = []

            def acquire(descriptor: int, operation: int) -> None:
                nonlocal lock_descriptor
                self.assertEqual(operation, fcntl.LOCK_EX)
                lock_descriptor = descriptor
                raise interrupt

            def recorded() -> str:
                return record.read_text() if record.exists() else ""

            def remove(path: Path) -> None:
                observations.append(("directory", recorded()))
                original_rmtree(path)
                if fail_finalizers:
                    raise OSError("fixture directory removal error")

            def close(descriptor: int) -> None:
                original_close(descriptor)
                if descriptor == lock_descriptor:
                    observations.append(("lock", recorded()))
                    if fail_finalizers:
                        raise SystemExit("fixture lock closure error")

            environment = queue_fixture(root)
            observed_exit: BaseException | None = None
            with (mock.patch.dict(os.environ, environment),
                  mock.patch.object(fcntl, "flock", side_effect=acquire),
                  mock.patch.object(shutil, "rmtree", side_effect=remove),
                  mock.patch.object(os, "close", side_effect=close),
                  redirect_stderr(io.StringIO())):
                try:
                    _ = mac_queue_run.main(["--failure-file", str(record), *self.command(root)[3:]])
                except BaseException as error:
                    observed_exit = error
            self.assertIs(observed_exit, interrupt)
            opening = "run-directory-open 1\n"
            removal = "run-directory-removal 2\n" if fail_finalizers else ""
            closing = "run-directory-closure 1\n" if fail_finalizers else ""
            self.assertEqual(observations, [("directory", opening), ("lock", opening + removal)])
            self.assertEqual(record.read_text(), opening + removal + closing)
            self.assertEqual(list((root / mac_queue_run.RUNS).iterdir()), [])

    def test_run_directory_records_the_lock_interrupt_before_any_cleanup(self) -> None:
        self.check_interrupted_run_lock(fail_finalizers=False)

    def test_run_directory_preserves_the_lock_interrupt_through_finalizer_failures(self) -> None:
        self.check_interrupted_run_lock(fail_finalizers=True)

    def test_admission_records_a_subprocess_read_failure_before_closing_its_pipe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / "failure"
            observations: list[str] = []
            error = OSError("fixture subprocess output read failed")

            def observe() -> None:
                observations.append(record.read_text() if record.exists() else "")

            class UnreadableOutput:
                def read(self) -> str:
                    raise error

                def close(self) -> None:
                    observe()

            class Command:
                def __init__(self, stdout: UnreadableOutput | None) -> None:
                    self.args: tuple[str, ...] = ()
                    self.stdin: None = None
                    self.stderr: None = None
                    self.stdout: UnreadableOutput | None = stdout

                def __enter__(self) -> Command:
                    return self

                def __exit__(self, *_details: object) -> None:
                    if self.stdout is not None:
                        self.stdout.close()

                def communicate(self, _input: object = None, **_options: object) -> tuple[str, str]:
                    return (self.stdout.read() if self.stdout is not None else ""), ""

                def poll(self) -> int:
                    return 0

                def wait(self) -> int:
                    return 0

                def kill(self) -> None:
                    pass

            process = Command(UnreadableOutput())
            release = Command(None)
            environment = queue_fixture(root)
            with (mock.patch.dict(os.environ, environment),
                  mock.patch.object(subprocess, "Popen", side_effect=(process, release)),
                  redirect_stderr(io.StringIO())):
                status = mac_queue_run.main(["--failure-file", str(record), *self.command(root)[3:]])
            self.assertEqual(status, 2)
            self.assertEqual(observations, ["admission 2\n"])
            self.assertEqual(record.read_text(), "admission 2\n")

    def test_queue_effects_require_the_step_executor(self) -> None:
        source = Path(mac_queue_run.__file__)
        tree = ast.parse(source.read_text())
        nodes = tuple(ast.walk(tree))
        parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
        scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)

        def scope(node: ast.AST) -> ast.AST:
            node = parents[node]
            while not isinstance(node, scopes):
                node = parents[node]
            return node

        definitions = {(scope(node), node.name): node for node in nodes
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        executor = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "QueueSteps")
        execute = definitions[(executor, "execute")]
        calls: dict[ast.AST, list[tuple[ast.AST, bool]]] = defaultdict(list)
        imports = {"open": "builtins.open"}
        for node in nodes:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"

        def imported_name(node: ast.AST) -> str:
            if isinstance(node, ast.Name):
                return imports.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return f"{imported_name(node.value)}.{node.attr}"
            return ""

        def as_action(node: ast.AST) -> bool:
            parent = parents[node]
            return (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute)
                    and parent.func.attr in ("execute", "defer") and isinstance(parent.func.value, ast.Name)
                    and parent.func.value.id in ("steps", "self") and len(parent.args) == 2 and parent.args[1] is node)

        for node in nodes:
            if isinstance(node, ast.Lambda):
                calls[node].append((scope(node), as_action(node)))
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                caller = search = scope(node)
                while True:
                    callee = definitions.get((search, node.id))
                    if callee is not None:
                        # A new thread has its own execution scope.
                        parent = parents[node]
                        caller = tree if isinstance(parent, ast.keyword) and parent.arg == "target" else caller
                        calls[callee].append((caller, as_action(node)))
                        break
                    if search is tree:
                        break
                    search = scope(search)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                caller = scope(node)
                callee = definitions.get((scope(caller), node.attr)) if caller is not tree else None
                if callee is not None:
                    calls[callee].append((caller, as_action(node)))

        def guarded(owner: ast.AST, route: frozenset[ast.AST]) -> bool:
            if owner is execute:
                return True
            if owner in route:
                return False
            sites = calls.get(owner, [])
            return bool(sites) and all(direct or guarded(caller, route | {owner}) for caller, direct in sites)

        methods = {"unlink", "rmtree", "close", "open", "mkdir", "chmod", "rename", "replace",
                   "read_bytes", "read_text", "write_bytes", "write_text", "touch", "rmdir",
                   "symlink_to", "hardlink_to", "iterdir", "exists", "pipe", "read", "write",
                   "wait", "communicate", "terminate", "kill", "send_signal", "flock"}
        functions = {
            "subprocess": {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"},
            "shutil": {"rmtree", "copyfile", "copy", "copy2", "copytree", "copymode", "copystat", "move"},
            "os": methods | {"fdopen", "fchmod", "fsync", "link", "listdir", "makedirs",
                             "remove", "removedirs", "scandir", "stat", "symlink", "truncate", "dup",
                             "dup2", "fstat", "ftruncate", "lseek", "pread", "pwrite", "readlink"},
            "scripts.lib.atomic_file": {"atomic_sample", "create_text_atomic", "prepared_replacement",
                                        "sync_folder", "write_text_atomic", "write_bytes_atomic"},
        }
        effects = {"builtins.open"} | {f"{module}.{name}" for module, names in functions.items() for name in names}
        unguarded: list[str] = []
        for node in nodes:
            if not isinstance(node, (ast.Name, ast.Attribute)) or not isinstance(node.ctx, ast.Load):
                continue
            name = imported_name(node)
            method = node.attr if isinstance(node, ast.Attribute) else ""
            if name not in effects and method not in methods:
                continue
            owner = scope(node)
            parent = parents[node]
            if name == "shutil.rmtree" and isinstance(parent, ast.Call):
                self.assertFalse(any(keyword.arg == "ignore_errors" for keyword in parent.keywords),
                                 f"{source.name}:{node.lineno}: rmtree must report errors")
            if isinstance(owner, ast.FunctionDef) and owner.name == "read_queue_failures" and name == "path.read_text":
                continue
            # The package selector runs before the queue imports its dependencies.
            if owner is tree and (name == "package_root.read" or (
                    name == "builtins.open" and isinstance(parent, ast.Call) and parent.args
                    and isinstance(parent.args[0], ast.BinOp) and isinstance(parent.args[0].right, ast.Constant)
                    and parent.args[0].right.value == "../lib/package_root.py")):
                continue
            if name not in {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}:
                if as_action(node) or guarded(owner, frozenset()):
                    continue
            unguarded.append(f"{source.name}:{node.lineno}: {ast.unparse(node)}")
        self.assertEqual(unguarded, [])

    def assert_rejects_queue_source(self, extra: str) -> None:
        source = Path(mac_queue_run.__file__).read_text()
        with (mock.patch.object(Path, "read_text", return_value=source + "\n" + extra),
              self.assertRaises(AssertionError)):
            self.test_queue_effects_require_the_step_executor()

    def test_queue_source_rejects_calls_through_imported_subprocess_names(self) -> None:
        for source in (
            "from subprocess import Popen\nPopen(['fixture'])",
            "from subprocess import Popen as launch\nlaunch(['fixture'])",
            "import subprocess as commands\ncommands.Popen(['fixture'])",
        ):
            with self.subTest(source=source):
                self.assert_rejects_queue_source(source)

    def test_queue_source_rejects_calls_through_imported_file_names(self) -> None:
        for source in (
            "from shutil import rmtree\nrmtree('run')",
            "from shutil import rmtree as erase\nerase('run')",
            "import shutil as files\nfiles.copytree('one', 'two')",
            "from os import unlink\nunlink('receipt')",
            "from os import close as release\nrelease(3)",
            "import os as files\nfiles.replace('one', 'two')",
        ):
            with self.subTest(source=source):
                self.assert_rejects_queue_source(source)

    def test_queue_source_rejects_builtin_and_path_file_operations(self) -> None:
        for source in (
            "open('receipt', 'w')",
            "from builtins import open as create\ncreate('receipt', 'w')",
            "Path('receipt').write_bytes(b'fixture')",
            "Path('receipt').unlink()",
            "Path('receipt').rename('next')",
        ):
            with self.subTest(source=source):
                self.assert_rejects_queue_source(source)

    def test_queue_source_rejects_imported_file_helpers(self) -> None:
        for source in (
            "write_text_atomic(Path('receipt'), 'fixture')",
            "from scripts.lib.atomic_file import write_text_atomic as save\nsave(Path('receipt'), 'fixture')",
            "import scripts.lib.atomic_file as files\nfiles.write_bytes_atomic(Path('receipt'), b'fixture')",
        ):
            with self.subTest(source=source):
                self.assert_rejects_queue_source(source)


if __name__ == "__main__":
    _ = unittest.main()

"""Verify the Mac copy with private state and local stand-ins."""
from __future__ import annotations

import fcntl
import io
import os
import shutil
import signal
import socket
import subprocess
import sys
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager, redirect_stderr
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.agents import remote
from scripts.production import staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member

copy_to_mac = cast(Callable[..., None], getattr(staff, "_copy_to_mac"))
COPY_TEST_DEADLINE = 10
COPY_PROGRAM = """
import os
import sys
from unittest.mock import patch
from scripts.agents import remote
from scripts.production import staff
from scripts.production.staff_test_support import observe_operation_lock

if "COPY_REAL_FLOCK" in os.environ:
    observe_operation_lock()
with (patch.object(remote, "local_machine", return_value="natedev"),
      patch.object(remote, "other_machine", return_value="mac"),
      patch.dict(remote.STANDARD_TIME_LIMIT, seconds=float(sys.argv[1]))):
    staff._copy_to_mac(detached=False)
"""

FAKE_SSH = """
import os
import signal
import socket
import sys
from pathlib import Path

# Close inherited descriptors as OpenSSH does.
os.closerange(3, os.sysconf("SC_OPEN_MAX"))
if os.environ.get("COPY_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
payload = sys.stdin.buffer.read()
if gate := os.environ.get("COPY_RELEASE"):
    with open(gate, "rb") as release:
        if notice := os.environ.get("COPY_SSH_NOTICE"):
            with socket.socket(socket.AF_UNIX) as sender:
                sender.connect(notice)
                sender.sendall(b"ssh")
        release.read(1)
destination = Path(os.environ["COPY_DESTINATION"])
temporary = destination.with_suffix(".tmp")
temporary.write_bytes(payload)
temporary.replace(destination)
"""

FLOCK_OBSERVER = """
import os
import socket
import sys

with socket.socket(socket.AF_UNIX) as sender:
    sender.connect(os.environ["STAFF_TEST_LOCK_NOTICE"])
    sender.sendall(b"holder")
flock = os.environ["COPY_REAL_FLOCK"]
os.execv(flock, [flock, *sys.argv[1:]])
"""


def use_default() -> Path:
    _ = os.environ.pop("STAFF_FILE")
    return Path.home() / ".local/state/staff/staff.json"


@dataclass
class CopyRunner:
    launches: list[dict[str, object]] = field(default_factory=list)
    sends: list[list[str]] = field(default_factory=list)
    on_launch: Callable[[], None] = lambda: None
    result: subprocess.CompletedProcess[str] = field(default_factory=lambda: subprocess.CompletedProcess([], 0, "", ""))

    def launch(self, _command: list[str], **options: object) -> None:
        self.launches.append(options)
        self.on_launch()

    def send(self, command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
        self.sends.append(command)
        return self.result


class MacCopyTests(StaffTestCase):
    runner: CopyRunner = CopyRunner()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.runner = CopyRunner()
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", self.runner.launch))
        _ = self.enterContext(mock.patch.object(subprocess, "run", self.runner.send))

    def test_default_write_launches_after_the_record_exists(self) -> None:
        path = use_default()
        written: list[str] = []

        def launch() -> None:
            written.append(path.read_text())
            stderr = cast(io.TextIOWrapper, self.runner.launches[-1]["stderr"])
            self.assertEqual(stderr.name, str(path.with_name("mac-copy.log")))

        self.runner.on_launch = launch
        row = staff.ensure("showrunner", name="copy", runtime_id="019b3a40-4250-7000-8000-000000000001")
        self.assertEqual(len(written), 1)
        self.assertIn(row.id, written[0])
        self.assertEqual(self.runner.sends, [])
        options, = self.runner.launches
        self.assertTrue(options["start_new_session"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)

    def test_private_record_never_launches_or_sends(self) -> None:
        _ = fixture_member("showrunner", name="private")
        copy_to_mac(detached=False)
        self.assertEqual(self.runner.launches, [])
        self.assertEqual(self.runner.sends, [])

    def test_mac_write_forwards_and_never_launches_a_copy(self) -> None:
        with self.machine("mac"):
            with mock.patch.object(staff, "_remote_row") as forward:
                _ = staff.ensure("showrunner", name="forward", runtime_id="unused")
                copy_to_mac(detached=False)
        forward.assert_called_once()
        self.assertEqual(self.runner.launches, [])
        self.assertEqual(self.runner.sends, [])

    def test_launch_failure_prints_one_line_and_preserves_the_write(self) -> None:
        path = use_default()

        def fail() -> None:
            raise OSError("cannot start\ncopy")

        self.runner.on_launch = fail
        errors = io.StringIO()
        with redirect_stderr(errors):
            row = staff.ensure("showrunner", name="survives", runtime_id="019b3a40-4250-7000-8000-000000000002")
        self.assertIn(row.id, path.read_text())
        self.assertEqual(errors.getvalue(), "staff: Mac copy failed: cannot start copy\n")

    def test_send_failure_prints_one_line_and_preserves_the_write(self) -> None:
        path = use_default()
        self.runner.result = subprocess.CompletedProcess([], 255, "", "link failed\nmore detail\n")
        self.runner.on_launch = lambda: copy_to_mac(detached=False)
        errors = io.StringIO()
        with redirect_stderr(errors):
            row = staff.ensure("showrunner", name="survives", runtime_id="019b3a40-4250-7000-8000-000000000002")
        self.assertIn(row.id, path.read_text())
        self.assertEqual(errors.getvalue(), "staff: Mac copy failed: link failed more detail\n")

    def test_remote_command_uses_a_same_folder_temporary_and_atomic_rename(self) -> None:
        path = use_default()
        _ = staff.ensure("showrunner", name="atomic", runtime_id="019b3a40-4250-7000-8000-000000000003")
        copy_to_mac(detached=False)
        arguments, = self.runner.sends
        self.assertEqual(arguments[:4], ["flock", str(path.with_name("mac-copy.lock")), "sh", "-c"])
        self.assertEqual(arguments[5:8], ["sh", str(remote.STANDARD_TIME_LIMIT["seconds"]), "mac"])
        self.assertEqual(arguments[-1], str(path))
        command = arguments[-2]
        self.assertIn('directory="$HOME/.local/state/staff"', command)
        self.assertIn('mktemp "$directory/staff.json.mac-copy.XXXXXXXX"', command)
        self.assertLess(command.index('cat > "$temporary"'), command.index('mv -f'))
        self.assertIn('mv -f "$temporary" "$directory/staff.json"', command)
        self.assertIn('trap \'rm -f "$temporary"\' EXIT', command)


class MacCopyOrderTests(StaffTestCase):
    record: Path = Path()
    lock: Path = Path()
    destination: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.record = use_default()
        self.record.parent.mkdir(parents=True)
        _ = self.record.write_text("original staff record\n")
        self.lock = self.record.with_name("mac-copy.lock")
        self.destination = self.root / "copied"
        self.write_executable("ssh", FAKE_SSH)
        _ = self.enterContext(cast(AbstractContextManager[object],
            mock.patch.dict(os.environ, {"COPY_DESTINATION": str(self.destination)})))

    def write_executable(self, tool: str, program: str) -> None:
        path = self.root / "bin" / tool
        _ = path.write_text(f"#!{sys.executable}\n{program}")
        path.chmod(0o700)

    def start_copy(self, seconds: float) -> subprocess.Popen[str]:
        copier = subprocess.Popen([sys.executable, "-c", COPY_PROGRAM, str(seconds)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

        def clean_up() -> None:
            if copier.returncode is None:
                copier.kill()
                _ = copier.communicate(timeout=COPY_TEST_DEADLINE)

        self.addCleanup(clean_up)
        return copier

    def notice(self, listener: socket.socket) -> bytes:
        listener.settimeout(COPY_TEST_DEADLINE)
        with listener.accept()[0] as connection:
            connection.settimeout(COPY_TEST_DEADLINE)
            return connection.recv(16)

    def finish_copy(self, copier: subprocess.Popen[str]) -> str:
        try:
            _, errors = copier.communicate(timeout=COPY_TEST_DEADLINE)
        except subprocess.TimeoutExpired:
            self.fail("The copier did not exit before the test deadline.")
        return errors

    def test_holder_keeps_the_lock_when_the_copier_dies(self) -> None:
        release = self.root / "release"
        os.mkfifo(release)
        with self.lock_notice(self.lock) as listener, os.fdopen(os.open(release, os.O_RDWR), "wb", buffering=0) as gate:
            with mock.patch.dict(os.environ, {"COPY_RELEASE": str(release),
                                 "COPY_SSH_NOTICE": os.environ["STAFF_TEST_LOCK_NOTICE"]}):
                copier = self.start_copy(remote.STANDARD_TIME_LIMIT["seconds"])
            try:
                self.assertEqual(self.notice(listener), b"ssh")
                copier.terminate()
                _ = self.finish_copy(copier)
                with self.lock.open("a") as contender:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(copier.returncode, -signal.SIGTERM)
                self.assertFalse(self.destination.exists())
            finally:
                _ = gate.write(b"x")

        # A successful lock acquisition also proves that the SSH command ended.
        result = subprocess.run(["flock", "-w", str(COPY_TEST_DEADLINE), str(self.lock), "true"],
                                check=False, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.destination.read_text(), "original staff record\n")

    def test_holder_reads_the_staff_record_after_it_acquires_the_lock(self) -> None:
        flock = shutil.which("flock")
        assert flock is not None
        self.write_executable("flock", FLOCK_OBSERVER)
        with self.lock_notice(self.lock) as listener, self.lock.open("a") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX)
            with mock.patch.dict(os.environ, {"COPY_REAL_FLOCK": flock}):
                copier = self.start_copy(remote.STANDARD_TIME_LIMIT["seconds"])
            owner = self.notice(listener)
            _ = self.record.write_text("latest staff record\n")
        errors = self.finish_copy(copier)
        self.assertEqual(copier.returncode, 0)
        self.assertEqual(errors, "")
        self.assertEqual(self.destination.read_text(), "latest staff record\n")
        self.assertEqual(owner, b"holder")

    def test_timeout_kills_ssh_that_ignores_sigterm_and_releases_the_lock(self) -> None:
        timeout = shutil.which("timeout")
        assert timeout is not None
        timeout_pid = self.root / "timeout.pid"
        self.write_executable("timeout", f"""\
import os
import sys
from pathlib import Path

Path({str(timeout_pid)!r}).write_text(str(os.getpid()))
os.execv({timeout!r}, [{timeout!r}, *sys.argv[1:]])
""")
        seconds = 2 * COPY_TEST_DEADLINE
        release = self.root / "release"
        os.mkfifo(release)
        with (self.lock_notice(self.lock) as listener,
              os.fdopen(os.open(release, os.O_RDWR), "wb", buffering=0) as gate,
              mock.patch.dict(os.environ, {"COPY_RELEASE": str(release), "COPY_IGNORE_TERM": "1",
                                          "COPY_SSH_NOTICE": os.environ["STAFF_TEST_LOCK_NOTICE"]})):
            copier = self.start_copy(seconds)
            try:
                self.assertEqual(self.notice(listener), b"ssh")
                # Trigger the timeout after SSH starts to ignore SIGTERM.
                os.kill(int(timeout_pid.read_text()), signal.SIGALRM)
                contender = subprocess.run(["flock", "-w", "3", str(self.lock), "true"],
                                           check=False, stdin=subprocess.DEVNULL)
                self.assertEqual(contender.returncode, 0, "The contender could not take the lock before the deadline.")
                errors = self.finish_copy(copier)
            finally:
                _ = gate.write(b"x")
        self.assertEqual(copier.returncode, 0)
        self.assertEqual(errors, f"staff: Mac copy failed: ssh timed out after {seconds:g} s\n")
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    _ = unittest.main()

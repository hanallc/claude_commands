"""Test the portable command runner and its output deadline."""

from __future__ import annotations

import os
import signal
import subprocess
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lib import time_limit


class TimeLimitTests(unittest.TestCase):
    def test_capture_preserves_output_and_exit_status(self) -> None:
        command = [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"]
        result = time_limit.capture(command, deadline=time.monotonic() + 5, env=os.environ)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, "out\n", "err\n"))

    def test_capture_stops_a_child_that_keeps_a_stream_open_after_exit(self) -> None:
        script = """
import os
import signal
import socket
import subprocess
import sys

address, keep, remote = sys.argv[1:]
query = subprocess.run(["git", "ls-remote", "--", remote], check=False)
assert (query.returncode == 0) == (os.path.basename(remote) != "missing.git")
ready, notify = os.pipe()
if os.fork() == 0:
    os.close(ready)
    for fd in (1, 2):
        if str(fd) != keep:
            os.close(fd)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.alarm(8)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(address)
        connection.sendall(b"R")
        os.write(notify, b"R")
        connection.recv(1)
else:
    os.close(notify)
    os.read(ready, 1)
    sys.exit(query.returncode)
"""
        caller = """
import os
import sys
import time
from scripts.lib import time_limit

result = time_limit.capture(sys.argv[1:], deadline=time.monotonic() + 2, env=os.environ)
raise SystemExit(result.returncode)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            _ = subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            for inherited in ("1", "2", "neither"):
                modes = ("success", "failure") if inherited == "neither" else ("success", "failure", "caller terminated")
                for mode in modes:
                    with self.subTest(inherited=inherited, mode=mode), socket.socket(socket.AF_UNIX) as server:
                        address = root / "child.sock"
                        server.bind(str(address))
                        self.addCleanup(address.unlink, missing_ok=True)
                        server.listen()
                        server.settimeout(3)
                        target = root / "missing.git" if mode == "failure" else remote
                        command = [sys.executable, "-c", script, str(address), inherited, str(target)]
                        started = time.monotonic()
                        if mode == "caller terminated":
                            with subprocess.Popen([sys.executable, "-c", caller, *command]) as process:
                                with server.accept()[0] as connection:
                                    connection.settimeout(5)
                                    self.assertEqual(connection.recv(1), b"R")
                                    process.terminate()
                                    self.assertEqual(process.wait(timeout=2), -signal.SIGTERM)
                                    self.assertEqual(connection.recv(1), b"")
                        else:
                            result = time_limit.capture(command, deadline=started + 1, env=os.environ)
                            if inherited == "neither":
                                expected = 128 if mode == "failure" else 0
                            else:
                                expected = time_limit.TIMED_OUT
                            self.assertEqual(result.returncode, expected)
                            with server.accept()[0] as connection:
                                connection.settimeout(1)
                                self.assertEqual(connection.recv(1), b"R")
                                self.assertEqual(connection.recv(1), b"")
                        self.assertLess(time.monotonic() - started, 5)
                        address.unlink()

    def test_capture_ends_its_reads_when_the_runner_keeps_its_streams_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory, socket.socket(socket.AF_UNIX) as server:
            address = Path(directory) / "runner.sock"
            launcher = Path(directory) / "stalled-runner"
            _ = launcher.write_text(f"""#!{sys.executable}
import signal
import socket

signal.alarm(6)
with socket.socket(socket.AF_UNIX) as control:
    control.connect({str(address)!r})
    control.recv(1)
""")
            launcher.chmod(0o700)
            server.bind(str(address))
            server.listen()
            server.settimeout(2)
            started = time.monotonic()
            with patch.object(sys, "executable", str(launcher)):
                result = time_limit.capture(["unused"], deadline=started + 1, env=os.environ)
            self.assertEqual(result.returncode, time_limit.TIMED_OUT)
            self.assertLess(time.monotonic() - started, 4)
            with server.accept()[0] as connection:
                connection.settimeout(1)
                connection.sendall(b"R")
                self.assertEqual(connection.recv(1), b"")

"""Check that TERM stops cleanup when an operation hangs."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
from typing import TextIO, cast
import unittest
from unittest import mock

from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.queue_fixture import write_memory_config
from scripts.lib import cancellation
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture


CHECKOUT = Path(__file__).resolve().parents[2]
STOP_SECONDS = 1.0
TERM_MARGIN_SECONDS = 2.0
CHILD_SETUP = """import os, signal, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
from scripts.lib import cancellation
root = Path(os.environ["CONF_DIR"])

def announce():
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    print("masked" if mask.intersection(cancellation.CANCELLATION_SIGNALS) else "ready", flush=True)

def hang(*args, **kwargs):
    announce()
    sys.stdin.buffer.read(1)
"""

HANG_IN_FINISH = """
from scripts.build_queue import queue_place
read_members = queue_place.program_members
def read_or_hang(*args, deadline=None, **kwargs):
    if deadline is not None:
        hang()
    return read_members(*args, deadline=deadline, **kwargs)
"""


class CancellationTests(unittest.TestCase):
    def test_cancellation_signals_match_the_child_reset(self) -> None:
        self.assertEqual(cancellation.CANCELLATION_SIGNALS, (signal.SIGTERM, signal.SIGHUP, signal.SIGINT))

    def assert_term_stops_hung_cleanup(self, program: str, *, status: int = -signal.SIGTERM,
                                      admission: bool = False, sidecar: str = "") -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as setup:
            root = Path(directory)
            write_memory_config(root / "build_admission.conf", program_stop_seconds=str(STOP_SECONDS),
                                program_sample_seconds="0.05", **{"program_first_peak_gib.rust-analyzer": "4"})
            _ = write_config_fixture(root / "build_folders.conf")
            environment = {**os.environ, "HOME": directory, "TMPDIR": directory, "CONF_DIR": directory,
                           "BUILD_FOLDERS": directory, "BUILDLOG_DIR": str(root / "buildlog"),
                           "BUILDLOG_SCOPE": "0"}
            fixture: AdmissionFixture | None = None
            if admission:
                setup.enter_context(mock.patch.dict(os.environ, environment))
                fixture = setup.enter_context(AdmissionFixture(root=root, skip_idle_spacing=True))
                _ = setup.enter_context(fixture.serving())
                environment = dict(os.environ)
            with subprocess.Popen([sys.executable, "-I", "-S", "-c", CHILD_SETUP + program, str(CHECKOUT),
                                   str(STOP_SECONDS)],
                                  cwd=directory, env=environment, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as child:
                try:
                    stdout = cast(TextIO, child.stdout)
                    self.assertTrue(select.select([stdout], [], [], TEST_HANG_GUARD_SECS)[0])
                    answer = stdout.readline()
                    self.assertEqual(answer, "ready\n")
                    if fixture is not None:
                        with fixture.loop_turn():
                            self.assertTrue(any(record.owner.pid == child.pid
                                                for record in fixture.server.reservations.values()))
                    child.terminate()
                    self.assertEqual(child.wait(timeout=STOP_SECONDS + TERM_MARGIN_SECONDS), status)
                    if sidecar:
                        self.assertTrue(list(Path(environment["TMPDIR"]).rglob(sidecar)))
                finally:
                    if child.poll() is None:
                        child.kill()
                        _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_term_stops_gate_reaping(self) -> None:
        self.assert_term_stops_hung_cleanup("""
from types import SimpleNamespace
from scripts.production import gate
job = SimpleNamespace(process=SimpleNamespace(returncode=None, pid=1))
with mock.patch.object(os, "wait4", side_effect=hang):
    gate.reap_gate_job(job)
""")

    def test_term_stops_queue_place_cleanup(self) -> None:
        self.assert_term_stops_hung_cleanup(HANG_IN_FINISH + """
with mock.patch.object(queue_place, "program_members", side_effect=read_or_hang):
    with queue_place.queue_place("rust-analyzer", root) as place:
        place.popen(["/bin/true"]).wait(timeout=5)
""", admission=True, sidecar="program.cgroup")

    def test_term_stops_cargo_front_cleanup(self) -> None:
        for phase in ("finish", "release"):
            with self.subTest(phase=phase):
                self.assert_term_stops_hung_cleanup(HANG_IN_FINISH + f"phase = {phase!r}\n" + """
import socket
from scripts.build_queue import admission_client as client, admission_records as records, ci_cargo_front
from scripts.build_queue.admission_test_support import short_socket_directory

release = client.release
connect = client._connect
with short_socket_directory() as directory, socket.socket(socket.AF_UNIX) as listener:
    path = str(Path(directory) / "release.sock")
    listener.bind(path)
    listener.listen()

    def connect_release(timeout):
        connection = connect(timeout)
        announce()
        return connection

    def release_without_reply(reservation, status, timing, *, deadline):
        assert 0 < deadline - client.CLOCK.monotonic() <= float(sys.argv[2])
        with mock.patch.dict(os.environ, {"ADMISSION_SOCKET": path}), \\
                mock.patch.object(client, "_connect", side_effect=connect_release):
            return release(reservation, status, timing, deadline=deadline)

    boundary = (mock.patch.object(queue_place, "program_members", side_effect=read_or_hang)
                if phase == "finish" else mock.patch.object(client, "release", side_effect=release_without_reply))
    repository = records.RepositoryIdentity("github.com/fixture/project", "project")
    with boundary, mock.patch.object(ci_cargo_front.step_estimator, "git_identity",
                                     return_value=(repository, str(root))):
        raise SystemExit(ci_cargo_front.main(["--cargo", "/bin/true", "--", "build"]))
""", status=128 + signal.SIGTERM, admission=True, sidecar="cargo.cgroup")

    def test_term_stops_private_admission_worker_start(self) -> None:
        self.assert_term_stops_hung_cleanup("""
import threading
from scripts.build_queue.admission_test_support import AdmissionFixture
with AdmissionFixture(root=root) as fixture:
    with mock.patch.object(threading.Thread, "start", side_effect=hang), fixture.serving():
        pass
""")

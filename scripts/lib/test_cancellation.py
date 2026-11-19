"""Check that TERM stops cleanup when an operation hangs."""
from __future__ import annotations

import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
from typing import TextIO, cast
import unittest

from scripts.lib import cancellation
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


CHECKOUT = Path(__file__).resolve().parents[2]
STOP_SECONDS = 1.0
TERM_MARGIN_SECONDS = 2.0
CHILD_SETUP = """import os, signal, sys
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
from scripts.build_queue.admission_test_support import write_memory_config
from scripts.lib import cancellation
from scripts.lint.memory_test_support import write_config_fixture
root = Path(os.environ["CONF_DIR"])
write_memory_config(root / "build_admission.conf", program_stop_seconds=sys.argv[2],
                    program_sample_seconds="0.05", **{"program_probe_arguments.rust-analyzer": "--version"})
write_config_fixture(root / "build_folders.conf")

def hang(*args, **kwargs):
    mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    print("masked" if mask.intersection(cancellation.CANCELLATION_SIGNALS) else "ready", flush=True)
    sys.stdin.buffer.read(1)
"""


class CancellationTests(unittest.TestCase):
    def test_cancellation_signals_match_the_child_reset(self) -> None:
        self.assertEqual(cancellation.CANCELLATION_SIGNALS, (signal.SIGTERM, signal.SIGHUP, signal.SIGINT))

    def assert_term_stops_hung_cleanup(self, program: str, *, status: int = -signal.SIGTERM) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "HOME": directory, "TMPDIR": directory, "CONF_DIR": directory,
                           "BUILD_FOLDERS": directory, "BUILDLOG_DIR": str(Path(directory) / "buildlog")}
            with subprocess.Popen([sys.executable, "-I", "-S", "-c", CHILD_SETUP + program, str(CHECKOUT),
                                   str(STOP_SECONDS)], cwd=directory, env=environment, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as child:
                try:
                    stdout = cast(TextIO, child.stdout)
                    self.assertTrue(select.select([stdout], [], [], TEST_HANG_GUARD_SECS)[0])
                    answer = stdout.readline()
                    child.terminate()
                    self.assertEqual(child.wait(timeout=STOP_SECONDS + TERM_MARGIN_SECONDS), status)
                    self.assertEqual(answer, "ready\n")
                finally:
                    if child.poll() is None:
                        child.kill()
                        _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_term_stops_gate_reaping(self) -> None:
        self.assert_term_stops_hung_cleanup("""
from types import SimpleNamespace
from scripts.production import merge_gate
job = SimpleNamespace(process=SimpleNamespace(returncode=None, pid=1))
with mock.patch.object(os, "wait4", side_effect=hang):
    merge_gate.reap_gate_job(job)
""")

    def test_term_stops_queue_place_cleanup(self) -> None:
        self.assert_term_stops_hung_cleanup("""
from scripts.build_queue import queue_place
with mock.patch.object(queue_place.QueuePlace, "finish", side_effect=hang):
    with queue_place.queue_place(["rust-analyzer", "--version"], root):
        pass
""")

    def test_term_stops_cargo_front_cleanup(self) -> None:
        self.assert_term_stops_hung_cleanup("""
from contextlib import nullcontext
import subprocess
from scripts.build_queue import admission_protocol as wire, ci_cargo_front, queue_place
execution = wire.RunnerExecutionTiming(0.05, float(sys.argv[2]), 1.0)
grant = wire.RunnerAdmitted("fixture", 1, "fixture", "", "", {}, execution=execution)
held = queue_place.HeldReservation(grant)
with (
    subprocess.Popen(["/bin/true"], stderr=subprocess.PIPE) as command,
    mock.patch.object(queue_place.QueuePlace, "popen", return_value=command),
    mock.patch.object(queue_place.QueuePlace, "finish", side_effect=hang),
    mock.patch.object(queue_place, "held_reservation", return_value=nullcontext(held)),
    mock.patch.object(ci_cargo_front.step_estimator, "git_identity", return_value=(str(root), str(root))),
    mock.patch.object(ci_cargo_front, "cargo_path", return_value=("/bin/true", os.defpath)),
):
    raise SystemExit(ci_cargo_front.main(["build"]))
""", status=128 + signal.SIGTERM)

    def test_term_stops_private_admission_worker_start(self) -> None:
        self.assert_term_stops_hung_cleanup("""
import threading
from scripts.build_queue.admission_test_support import AdmissionFixture
with AdmissionFixture(root=root) as fixture:
    with mock.patch.object(threading.Thread, "start", side_effect=hang), fixture.serving():
        pass
""")

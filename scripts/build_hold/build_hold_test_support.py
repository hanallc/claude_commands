"""Private seeded hold fixtures and real CLI calls without interpreter startup."""

from __future__ import annotations


from collections.abc import Callable, Generator
from contextvars import ContextVar
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import threading
import time
from typing import ClassVar, TypedDict, cast, override
import unittest
from unittest import mock

from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.build_hold import build_hold
from scripts.buildlog import record, store
from scripts.build_queue import admission_test_support
from scripts.build_queue.admission_test_support import AdmissionFixture, caller_admission
from scripts.lib import test_hang_guard
from scripts.build_hold.override_guard import require_checkout

require_checkout(Path(__file__))

from scripts.lib.socket_test_support import short_socket_directory
from scripts.lint import memory_test_support
from scripts.production import staff
from scripts.production.cli_fixture import process_boundary_for_case
from scripts.production.staff_test_support import fixture_runtime_id

_finish_writers: ContextVar[Callable[[], None] | None] = ContextVar("finish_hold_writers", default=None)


FIXTURE_SHOWRUNNER = staff.StaffRow(staff.native_id(fixture_runtime_id("clear-member")), "showrunner",
                                    "clear-showrunner", staff.StaffRoot(),
                                    current_session=staff.native_id(fixture_runtime_id("clear-session")))


@contextmanager
def clear_caller(session: str, *rows: staff.StaffRow) -> Generator[None]:
    """Supply private staff rows and a runtime identity to clear."""
    with (mock.patch.object(staff, "authority_snapshot", return_value=staff.StaffSnapshot(rows, production=True)),
          mock.patch.object(staff, "runtime_id", return_value=session)):
        yield


@contextmanager
def showrunner_caller(*rows: staff.StaffRow) -> Generator[None]:
    """Use the fixture showrunner for a clear command."""
    with clear_caller(FIXTURE_SHOWRUNNER.current_session, FIXTURE_SHOWRUNNER, *rows):
        yield


def hold_config_environment(root: Path) -> dict[str, str]:
    """Seed fixed settings for the hold and admission fixtures."""
    root.mkdir(parents=True, exist_ok=True)
    targets = root / "build_targets.conf"
    if not targets.exists():
        _ = targets.write_text(
            "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\ndefault_target=natedev\n"
            + "sync.hosts.mac=fixture-mac\nsync.hosts.aws=fixture-aws\n"
            + "# range: 0.01..300\nhold_sender_timeout_seconds=1\n")
    admission = root / "build_admission.conf"
    if not admission.exists():
        _ = admission.write_text(memory_test_support.ADMISSION_CONFIG_FIXTURE.read_text()
                                 + "\n# whole: 0..50\npeak_history_share_percent=10\n"
                                 + "# range: 0..\nprogress_profile_sweep_seconds=0.05\n"
                                 + "# whole: 1..\nremote_start_hold_seconds=1200\n")
    detached = root / "detached_runs.conf"
    if not detached.exists():
        _ = detached.write_text("# whole: 1..\nRESULT_LINE_CHARS=300\n# whole: 1..\nRESULT_TEXT_CHARS=2000\n")
    staff_config = root / "staff.conf"
    if not staff_config.exists():
        _ = staff_config.write_text("# range: 0.001..\nOPERATION_LOCK_SECONDS=5\nPRODUCER_WORKSPACE=producers\n")
    return {"CONF_DIR": str(root), "BUILD_TARGETS_CONFIG": str(targets), "STAFF_CONFIG": str(staff_config),
            "USER_ZONE": "UTC", "VERIFY_STEPS_DIR": str(root / "steps")}


def invoke_hold(arguments: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    output, errors = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(output), redirect_stderr(errors):
        try:
            status = build_hold.main(arguments)
        except SystemExit as error:
            status = error.code if isinstance(error.code, int) else 0 if error.code is None else 1
    return subprocess.CompletedProcess(["python3", str(SCRIPT), *arguments], status, output.getvalue(), errors.getvalue())


class HoldEventRecord(TypedDict):
    target: str
    event: str
    hold_key: str
    holder: str
    reason: str
    at: str
    until: str


SCRIPT = Path(__file__).with_name("build_hold.py")
MEMORY_GATE = Path(__file__).resolve().parent.parent / "lint" / "memory_gate.sh"
# Loaded by every Python process a test starts. It opens a writer on the test's
# FIFO and passes it to each child it starts, so a detached child (the CLI's
# hold-event recorder) holds a writer until it exits, and the test's cleanup
# reads EOF only once nothing it started can still write into its scratch
# directory.
CHILD_LIFETIME = r'''import os
import subprocess

try:
    writer = os.open(os.environ["BUILD_HOLD_TEST_LIFETIME_FIFO"], os.O_WRONLY | os.O_NONBLOCK)
except (KeyError, OSError):
    writer = -1
if writer >= 0:
    original_popen = subprocess.Popen

    class LifetimeChild(original_popen):
        def __init__(self, *args, **kwargs):
            kwargs["pass_fds"] = (*kwargs.get("pass_fds", ()), writer)
            super().__init__(*args, **kwargs)

    subprocess.Popen = LifetimeChild
'''


def write_holder(folder: Path, name: str, since: str, purpose: str, release: str = "unknown") -> Path:
    path = folder / name
    _ = path.write_text(json.dumps({"holder": name, "since": since, "for": purpose, "release_eta": release}) + "\n")
    return path


def recorded_hold_events(root: Path, expected: int) -> list[HoldEventRecord]:
    # Completion, not a polling interval, tells us all records are available.
    # The assertions at the call sites still check their expected contents/count.
    _ = expected
    finish = _finish_writers.get()
    if finish is not None:
        finish()
    rows = [cast(HoldEventRecord, json.loads(line))
            for path in root.rglob("[0-9][0-9][0-9][0-9]-*.jsonl") for line in path.read_text().splitlines()]
    return sorted(rows, key=lambda row: (row["at"], row["event"] != "began"))


class IsolatedBuildHoldTest(unittest.TestCase):
    admission_fixture: AdmissionFixture = cast(AdmissionFixture, cast(object, None))
    scratch: Path = Path()
    socket_dir: Path = Path()
    lifetime_reader: int = -1
    lifetime_writer: int = -1

    seed: ClassVar[Path]
    case: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.seed = Path(temporary.name) / "seed"
        cls.case = Path(temporary.name) / "case"
        (cls.seed / "proc").mkdir(parents=True)
        (cls.seed / "child-lifetime").mkdir()
        _ = (cls.seed / "child-lifetime/sitecustomize.py").write_text(CHILD_LIFETIME)
        _ = (cls.seed / "staff.json").write_text(json.dumps({"schema": 2, "rows": []}))
        _ = hold_config_environment(cls.seed)
        (cls.seed / "tools").mkdir()
        systemctl = cls.seed / "tools/systemctl"
        _ = systemctl.write_text("#!/bin/sh\nexit 0\n")
        systemctl.chmod(0o755)

    @override
    def setUp(self) -> None:
        self.scratch = self.case
        _ = shutil.copytree(self.seed, self.scratch)
        self.addCleanup(shutil.rmtree, self.scratch)
        self.socket_dir = Path(self.enterContext(short_socket_directory()))
        proc = self.scratch / "proc"
        _ = self.enterContext(mock.patch.object(build_hold, "PROC_ROOT", proc))
        lifetime = self.scratch / "child-lifetime"
        tools = self.scratch / "tools"
        fifo = lifetime / "writers"
        os.mkfifo(fifo)
        self.lifetime_reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        # Held until cleanup, so the FIFO has had a writer and reports EOF
        # once the last one closes, even when a test starts no child.
        self.lifetime_writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        with mock.patch.object(memory_test_support, "config_environment", side_effect=hold_config_environment):
            memory = memory_test_support.fake_memory_readings(self.scratch)
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.scratch),
            **interpreter_cache_environment(self.scratch / "cache"),
            "BUILDLOG_DIR": str(self.scratch / "buildlog"),
            "BUILD_HOLD_DIR": str(self.scratch / "holders"),
            "BUILD_HOLD_RELEASE_DIR": str(self.scratch / "release"),
            "BUILD_HOLD_ENFORCER_DIR": str(self.scratch / "enforcer"),
            "BUILD_HOLD_CGROUP_ROOT": str(self.scratch / "cgroup"),
            "PATH": str(tools) + os.pathsep + os.environ.get("PATH", ""),
            "BUILDLOG_HOLD_WAIT_FILE": "", "BUILDLOG_HOLD_WAITER_FILE": "",
            "BUILDLOG_CALL_ID": "", "BUILDLOG_CALLER": "",
            "MAC_TEST_STATE_DIR": str(self.scratch / "mac-state"),
            **memory,
            # No test reads the machine's own session records, or passes for the session that runs it.
            "NOTIFIER_SESSIONS_DIR": str(self.scratch / "sessions"),
            "CLAUDE_CODE_SESSION_ID": "",
            "CODEX_THREAD_ID": "",
            "STAFF_FILE": str(self.scratch / "staff.json"),
            "UNIT_RUN_DIR": "",
            "UNIT_SESSION_DIR": "",
            "BUILD_HOLD_TEST_LIFETIME_FIFO": str(fifo),
            "PYTHONPATH": os.pathsep.join(filter(None, (str(lifetime), os.environ.get("PYTHONPATH", "")))),
        }))
        with (mock.patch.object(memory_test_support, "config_environment", side_effect=hold_config_environment),
              mock.patch.object(admission_test_support, "config_environment", side_effect=hold_config_environment)):
            self.admission_fixture = caller_admission(self, self.scratch, dict(os.environ))
        if not process_boundary_for_case(self):
            self.install_recorder_threads()
        token = _finish_writers.set(self.finish_writers)
        self.addCleanup(_finish_writers.reset, token)
        self.addCleanup(self.reap_recorders)
        self.addCleanup(self.wait_for_detached_writers)

    def install_recorder_threads(self) -> None:
        pending: ContextVar[tuple[Path, threading.Event]] = ContextVar("hold_record_append")
        original_root = store.root
        original_append = store.append_line

        def root() -> Path:
            try:
                return pending.get()[0]
            except LookupError:
                return original_root()

        def append(path: Path, row: object) -> None:
            try:
                pending.get()[1].set()
            except LookupError:
                pass
            original_append(path, row)

        def start_recorder(command: list[str]) -> None:
            log_root = original_root().absolute()
            ready = threading.Event()
            arguments = command[2:]

            def write() -> None:
                token = pending.set((log_root, ready))
                try:
                    _ = record.main(arguments)
                finally:
                    ready.set()
                    pending.reset(token)

            threading.Thread(target=write, name="hold-event-recorder", daemon=True).start()
            # Resolve the row and its path before the caller can change its environment or cwd.
            # Keep the module bindings unchanged while the file write continues.
            self.assertTrue(ready.wait(timeout=test_hang_guard.TEST_HANG_GUARD_SECS),
                            "The hold event recorder did not report its path")

        _ = self.enterContext(mock.patch.object(record, "detach"))
        _ = self.enterContext(mock.patch.object(store, "root", root))
        _ = self.enterContext(mock.patch.object(store, "append_line", append))
        _ = self.enterContext(mock.patch.object(build_hold, "start_recorder", start_recorder))

    def finish_writers(self) -> None:
        self.reap_recorders()
        self.wait_for_detached_writers()
        fifo = self.scratch / "child-lifetime/writers"
        self.lifetime_reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.lifetime_writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)

    def wait_for_detached_writers(self) -> None:
        """Wait until no process a test started holds its FIFO writer.

        A child opens its writer only when its interpreter loads
        sitecustomize. Under load the FIFO can report EOF before a slow child
        gets there, and the read then finds that child's writer and no data,
        so wait again until a read returns EOF.
        """
        os.close(self.lifetime_writer)
        deadline = time.monotonic() + test_hang_guard.TEST_HANG_GUARD_SECS
        try:
            while True:
                remaining = max(deadline - time.monotonic(), 0)
                readable, _, _ = select.select([self.lifetime_reader], [], [], remaining)
                self.assertTrue(readable, "Detached children did not finish before scratch cleanup")
                try:
                    if os.read(self.lifetime_reader, 1) == b"":
                        return
                except BlockingIOError:
                    pass
        finally:
            os.close(self.lifetime_reader)

    def reap_recorders(self) -> None:
        for thread in threading.enumerate():
            if thread.name == "hold-event-recorder":
                # Wait for each recorder before a test reads its rows or removes its files.
                thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                self.assertFalse(thread.is_alive())

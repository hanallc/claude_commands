"""Tests for detached_runs.py: detached runs that message the worker who started them."""

from __future__ import annotations

from scripts.direct import pass_record as pass_recorder
from scripts.lib.fixture_test_support import set_up_fixture
from scripts.lint.memory_test_support import copy_config_file_fixture, write_config_fixture

import contextlib
import fcntl
import hashlib
import io
import json
import math
import multiprocessing
import os
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from weakref import ref
from collections.abc import Callable, Generator, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from threading import Event, current_thread
from types import SimpleNamespace
from typing import IO, Literal, cast, override
from unittest.mock import ANY, MagicMock, call, patch

from scripts.build_hold.owning_session import NoOwningSession
from scripts.production.staff_test_support import isolated_staff_file
from scripts.lib.signal_test_support import install_signal_pid_guard
from scripts.agents import detached_run_process, detached_runs, pane_record, worker_lifecycle
from scripts.agents.detached_runs import deliver_in_fresh_process
from scripts.astra import astra_roles
from scripts.buildlog import treekey
from scripts.build_hold import build_hold, hold_enforcer, shared_state_defaults
from scripts.direct import progress_history, recorder_state
from scripts.lib import atomic_file, conf, process_group, process_start, process_stop, unit_runs
from scripts.lib.unit_runs_test_support import start_new_command
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.script_envelope import script_prompt
from scripts.herdr import herdr, herdr_types, pane_launch, pass_notices, turn_end, worker_panes
from scripts.herdr.worker_panes import worker_run
from scripts.herdr import test_worker_panes as pane_support
from scripts.message import roster, send, send_later, sessions
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lib.python_startup.sitecustomize import startup_pythonpath
from scripts.lib.fork_test_support import ForkChild, ForkChildren
from scripts.lib.socket_test_support import short_socket_directory
from scripts.message.repeat_guard_test_support import install_guard_test_budget
from scripts.production import staff, session_staff, test_worker, unit_lookup, worker, worker_admission
from scripts.production.staff_test_support import StaffTestCase, fixture_member, fixture_runtime_id, fixture_session

WORKER = "unit-impl"
THREAD_ONE = fixture_runtime_id("detached-run-thread-one")
THREAD_TWO = fixture_runtime_id("detached-run-thread-two")
LASTING_THREAD = fixture_runtime_id("detached-run-lasting-thread")
ADOPTED_THREAD = fixture_runtime_id("detached-run-adopted-thread")
SHARED_THREAD = fixture_runtime_id("detached-run-shared-thread")
DEAD_PID = 2 ** 22 + 12345
PASS_COMPLETION_ID = "pass-1-2"


def setUpModule() -> None:
    install_signal_pid_guard()
    install_guard_test_budget()


def private_supervisor(test: unittest.TestCase, path: Path) -> int:
    """Keep a private supervisor alive until the test closes its input."""
    guard = LauncherHangGuard("detached supervisor startup and cleanup")
    stand_in = subprocess.Popen([sys.executable, "-c",
                                 "import sys; print('ready', flush=True); sys.stdin.read()",
                                 "detached_runs.py", "supervise", str(path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    test.addCleanup(lambda: stand_in.wait(timeout=guard.remaining()))
    test.addCleanup(stand_in.kill)
    assert stand_in.stdin is not None and stand_in.stdout is not None
    test.addCleanup(stand_in.stdin.close)
    test.addCleanup(stand_in.stdout.close)
    test.assertTrue(select.select([stand_in.stdout], [], [], guard.remaining())[0], "supervisor did not start")
    test.assertEqual(stand_in.stdout.readline(), "ready\n")
    return stand_in.pid


def deliver_in_fixture(path: Path) -> int:
    """Keep fake pane and sender calls inside the test process."""
    return detached_runs.main(["deliver", str(path)])


class DetachedRunsTests(unittest.TestCase):
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    stack: contextlib.ExitStack  # pyright: ignore[reportUninitializedInstanceVariable]
    sent: list[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    unit: staff.StaffRow  # pyright: ignore[reportUninitializedInstanceVariable]
    worker_id: staff.StaffId  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.stack = self.enterContext(contextlib.ExitStack())
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        _ = self.stack.enter_context(isolated_staff_file(self.root / "staff.json", {}))
        self.session = self.root / "unit" / "run-1"
        self.session.mkdir(parents=True)
        self.stack.enter_context(patch.dict(os.environ, {
                                                        "HOME": str(self.root),
                                                        "XDG_STATE_HOME": str(self.root / "state"),
                                                        "DESIGN_ROLES_STATE": str(self.root / "astra/design-roles"),
                                                        "UNIT_RUN_DIR": str(self.session),
                                                        "BUILDLOG_SCOPE": "0"}))
        config = self.root / "config"
        config.mkdir()
        for name in ("detached_runs", "send", "postmaster", "codex_daemon", "direct", "shutdown"):
            _ = copy_config_file_fixture(Path(__file__).resolve().parents[2] / f"config/{name}.conf",
                                         config / f"{name}.conf")
        self.stack.enter_context(patch.dict(os.environ, {"CONF_DIR": str(config)}))
        self.sent = []
        _ = self.stack.enter_context(patch.object(treekey, "tree_key", return_value="key-1"))

        fixture = set_up_fixture(self.stack.callback, StaffTestCase())
        _, self.unit, _ = fixture.tree()
        self.unit = staff.set_worktree(self.unit.id, staff.PresentWorktree("natedev", self.root), expected=self.unit.worktree)
        self.stack.enter_context(patch.dict(os.environ, {
            "DESIGN_ROLES_STATE": str(self.root / "astra/design-roles"),
        }))
        write_text_atomic(self.session / unit_runs.STATE_FILE, json.dumps({"owner_staff_id": self.unit.id}))
        _ = self.stack.enter_context(patch("scripts.herdr.worker_panes.worker_run", return_value=self.session))
        self.worker_id = self.record_worker(WORKER, THREAD_ONE).id

        def deliver_fixture(member: staff.StaffRow, membership: staff.StaffSnapshot, text: str,
                            **options: object) -> send.Result:
            del membership, options
            path = session_staff.inbox_path(member.id)
            previous = cast(dict[str, object], json.loads(path.read_text())) if path.exists() else {}
            messages = cast(list[str], previous.get("messages", []))
            messages.append(text.split("\n", 1)[1])
            self.sent.append(text)
            write_text_atomic(path, json.dumps({"messages": messages}))
            return send.Result("queued", "private fixture")

        _ = self.stack.enter_context(patch.object(send, "deliver_worker_text", side_effect=deliver_fixture))
        _ = self.stack.enter_context(patch.object(detached_runs, "deliver_in_fresh_process",
                                                 side_effect=deliver_in_fixture))

    def record_worker(self, name: str, thread_id: str) -> staff.StaffRow:
        fixture_session(thread_id, staff.CodexBackend(self.root / "codex-home"))
        rows = [row for row in staff.find(kind="worker", parent=self.unit.id) if row.name == name]
        if rows:
            return staff.update_current_session(rows[0].current_session, staff.native_id(thread_id))
        return staff.ensure_worker(staff.native_id(thread_id), name, self.unit.id)

    def pending(self) -> dict[str, object]:
        path = session_staff.inbox_path(self.worker_id)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return cast(dict[str, object], json.loads(text)) if text.strip() else {}

    def record(self, command: list[str], *, run_id: str = "r1", **extra: str) -> Path:
        runs = self.session / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        record: detached_runs.RunRecord = {
            "run_id": run_id, "status": "running", "session_dir": str(self.session), "worker": WORKER,
            "staff_id": self.worker_id,
            "label": "verify.sh check", "command": command,
            "log": str(runs / f"{run_id}.log"), "started_at": time.time(), "pid": os.getpid(),
            "director_dir": str(self.session), "done_message_file": extra.get("done", ""),
        }
        path = runs / f"{run_id}.json"
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def live_run(self, command: list[str], pid: int = 0) -> Path:
        """A running record from this folder at tree key key-1, with a stand-in supervisor whose
        command line names detached_runs.py supervise and the record, unless pid names another process."""
        path = self.record(command)
        if not pid:
            pid = private_supervisor(self, path)
        self.update_record(path, tree_key="key-1", pid=pid)
        return path

    def test_damaged_record_does_not_hide_a_live_run(self) -> None:
        path = self.live_run(["fixture", "check"])
        record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        damaged = path.with_name("000-damaged.json")
        _ = damaged.write_text("{", encoding="utf-8")
        evidence = detached_runs.live_run_evidence_for_staff(path.parent, frozenset({self.worker_id}))
        self.assertEqual(evidence.runs, (record,))
        self.assertEqual(len(evidence.read_errors), 1)
        start_new_command(self)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            evidence.report_errors()
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertIn(str(damaged), errors.getvalue())
        for name in ("live_runs", "live_runs_for_staff"):
            with self.subTest(name=name), self.assertRaises(worker_lifecycle.WorkerStateUnreadable) as raised:
                if name == "live_runs":
                    _ = detached_runs.live_runs(path.parent, WORKER)
                else:
                    _ = detached_runs.live_runs_for_staff(path.parent, frozenset({self.worker_id}))
            self.assertEqual(raised.exception.path, damaged)

    def test_run_folder_errors_remain_unknown_and_list_readers_raise(self) -> None:
        runs = self.session / detached_runs.DETACHED_RUNS_DIR
        identities = frozenset({self.worker_id})
        absent = detached_runs.live_run_evidence_for_staff(runs, identities)
        self.assertEqual((absent.runs, absent.read_errors), ((), ()))
        self.assertEqual(detached_runs.live_runs(runs, WORKER), [])
        self.assertEqual(detached_runs.live_runs_for_staff(runs, identities), [])
        runs.mkdir()
        for error in (PermissionError(13, "Permission denied", str(runs)),
                      NotADirectoryError(20, "Not a directory", str(runs)),
                      OSError(5, "Input/output error", str(runs))):
            with (patch("os.scandir", side_effect=error),
                  patch.object(detached_run_process, "supervisor_records",
                               return_value=detached_run_process.ProcessTableUnavailable())):
                with self.subTest(error=type(error).__name__, reader="evidence"):
                    evidence = detached_runs.live_run_evidence_for_staff(runs, identities)
                    self.assertEqual(evidence.runs, ())
                    self.assertEqual(len(evidence.read_errors), 1)
                    self.assertEqual(evidence.read_errors[0].path, runs)
                    self.assertIs(evidence.read_errors[0].error, error)
                for name, reader in (
                        ("live_runs", lambda: detached_runs.live_runs(runs, WORKER)),
                        ("live_runs_for_staff", lambda: detached_runs.live_runs_for_staff(runs, identities)),
                        ("live_runs_in_folders", lambda: detached_runs.live_runs_in_folders({self.session: identities}))):
                    with self.subTest(error=type(error).__name__, reader=name):
                        with self.assertRaises(type(error)) as raised:
                            _ = reader()
                        self.assertIs(raised.exception, error)

    def test_twin_and_worker_records_require_a_readable_folder(self) -> None:
        runs = self.session / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir()
        error = PermissionError(13, "Permission denied", str(runs))
        for name, reader in (
                ("live_twin", lambda: detached_runs.live_twin(runs, self.worker_id, ["fixture"], "key-1")),
                ("worker_runs", lambda: detached_runs.worker_runs(runs, WORKER))):
            with self.subTest(reader=name), patch("os.scandir", side_effect=error):
                with self.assertRaises(PermissionError) as raised:
                    _ = reader()
                self.assertIs(raised.exception, error)

    def seat_runs(self, count: int) -> list[Path]:
        """Create live supervisors for distinct runs for the fixture worker."""
        source = self.record(["fixture", "run"])
        template = source.read_text()
        source.unlink()
        paths: list[Path] = []
        for index in range(count):
            path = source.with_name(f"live-{index}.json")
            _ = path.write_text(template)
            guard = LauncherHangGuard("live-run limit fixture")
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()",
                 "detached_runs.py", "supervise", str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            self.addCleanup(child.wait, timeout=guard.remaining())
            self.addCleanup(child.kill)
            assert child.stdin is not None and child.stdout is not None
            self.addCleanup(child.stdin.close)
            self.addCleanup(child.stdout.close)
            self.assertTrue(select.select([child.stdout], [], [], guard.remaining())[0])
            self.assertEqual(child.stdout.readline(), "ready\n")
            self.update_record(path, run_id=f"live-{index}", label=f"check {index}",
                               command=["fixture", str(index)], pid=child.pid,
                               tree_key="key-1")
            paths.append(path)
        return paths

    def update_record(self, path: Path, **fields: object) -> None:
        record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        record.update(fields)
        _ = path.write_text(json.dumps(record), encoding="utf-8")

    def test_resume_context_names_each_live_run_and_its_read_command(self) -> None:
        paths = self.seat_runs(2)
        text = detached_runs.resume_context(paths[0].parent, WORKER)
        for index, path in enumerate(paths):
            record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
            log = record["log"]
            self.assertIn(f"Detached run live-{index} remains live.", text)
            self.assertIn(f"Result: {path}; log: {log}", text)
            self.assertIn(f"`cat -- {path} {log}`", text)
        self.update_record(paths[0], status="finished")
        text = detached_runs.resume_context(paths[0].parent, WORKER)
        self.assertNotIn("Detached run live-0", text)
        self.assertIn("Detached run live-1", text)

    @contextlib.contextmanager
    def supervisor_spawns(self) -> Generator[MagicMock]:
        """Fake supervisor starts and keep other subprocess calls real."""
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        real_popen = cast(Callable[..., subprocess.Popen[bytes]], subprocess.Popen)

        def spawn(arguments: list[str], **options: object) -> subprocess.Popen[bytes]:
            if "supervise" in arguments:
                return cast(subprocess.Popen[bytes], popen(arguments, **options))
            return real_popen(arguments, **options)

        with patch.object(subprocess, "Popen", side_effect=spawn):
            yield popen

    def start(self, command: list[str], to: str = WORKER) -> tuple[MagicMock, str]:
        """Start the command for the seat and return its supervisor calls and output."""
        with self.supervisor_spawns() as popen, contextlib.redirect_stdout(io.StringIO()) as output:
            code = detached_runs.main(["start", "--session-dir", str(self.session), "--to", to, "--", *command])
        self.assertEqual(code, 0)
        return popen, output.getvalue()

    def supervise(self, path: Path) -> detached_runs.RunRecord:
        self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        return cast(detached_runs.RunRecord, json.loads(path.read_text(encoding="utf-8")))

    def test_supervisor_keeps_completion_when_the_command_changes_its_module(self) -> None:
        module_name = "_fixture_detached_completion"
        module = self.root / f"{module_name}.py"
        received = self.root / "completion-thread"
        settled = self.root / "settled-worker"
        write_text_atomic(module, "from pathlib import Path\n"
                          + "from scripts.herdr.worker_panes import StoppedDiagnosticRecovery\n"
                          + f"def run_finished(thread): Path({str(received)!r}).write_text(thread)\n"
                          + "def retry_stopped_diagnostics(run, worker):\n"
                          + f"    Path({str(settled)!r}).write_text(worker)\n"
                          + "    return StoppedDiagnosticRecovery.SETTLED\n")
        replacement = 'raise ImportError("completion API moved")\n'
        path = self.record([sys.executable, "-c",
            f"from pathlib import Path; Path({str(module)!r}).write_text({replacement!r})"])

        def completion_import(name: str) -> object:
            return import_module(module_name if name == "scripts.herdr.turn_end" else name)

        self.addCleanup(sys.modules.pop, module_name, None)
        with (patch.object(sys, "path", [str(self.root), *sys.path]),
              patch.object(detached_runs, "import_module", new=completion_import)):
            record = self.supervise(path)
        self.assertEqual(module.read_text(), replacement)
        self.assertIs(record.get("completion_after_slice_cleanup"), True)
        self.assertIn("supervisor_started", record)
        self.assertEqual(received.read_text(), self.worker_id)
        self.assertEqual(settled.read_text(), self.worker_id)
        self.assertIs(record.get("completion_pending"), False)
        message, = cast(list[str], self.pending()["messages"])
        self.assertIn("Result: PASS (exit 0)", message)

    # Criterion 1: start returns at once with a detached supervisor and a running record.
    def test_retired_worker_finishes_its_orphan_without_blocking_other_runs(self) -> None:
        path = self.record(["true"])
        self.update_record(path, pid=DEAD_PID, started_at=time.time() - 600)
        _ = staff.retire(self.worker_id)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), ["r1"])
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(record["status"], "finished")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(self.sent, [])

    def test_start_detaches_a_supervisor_and_records_the_run(self) -> None:
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        environment = {"CODEX_THREAD_ID": THREAD_ONE, "CLAUDE_CODE_SESSION_ID": "director-session",
                       "UNIT_TEAM_ROLE": "test"}
        with patch.dict(os.environ, environment), patch.object(subprocess, "Popen", popen), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = detached_runs.main(["start", "--label", "verify.sh check", "--", "bash", "verify.sh", "check"])
        self.assertEqual(code, 0)
        kwargs = cast(dict[str, object], popen.call_args.kwargs)
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertNotIn(kwargs["stdout"], (None, sys.stdout))
        records = list((self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(records), 1)
        record = cast(dict[str, object], json.loads(records[0].read_text(encoding="utf-8")))
        self.assertEqual((record["status"], record["pid"], record["worker"], record["staff_id"]),
                         ("running", 4321, WORKER, self.worker_id))
        self.assertTrue(records[0].stem.endswith(f"@{self.worker_id}"))
        self.assertNotIn("thread_id", record)
        self.assertEqual(record["command"], ["bash", "verify.sh", "check"])
        self.assertEqual(record.get("kind"), "detect")
        self.assertNotIn("port", record)
        self.assertIn("next message", output.getvalue())
        records[0].unlink()
        with contextlib.chdir(self.root), \
             patch.object(detached_runs, "caller_from_environment", return_value=detached_runs.NotDetachedWorker()), \
             patch.object(subprocess, "Popen", popen), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(detached_runs.main([
                "start", "--session-dir", str(self.session.relative_to(self.root)), "--to", WORKER,
                "--", "bash", "verify.sh", "check",
            ]), 0)
        arguments = cast(list[str], popen.call_args.args[0])
        path = Path(arguments[-1])
        self.assertTrue(path.is_absolute())
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(record["session_dir"], str(self.session))
        self.assertTrue(Path(cast(str, record["log"])).is_absolute())

        with self.subTest(target="registered after lookup"):
            name = "review-late"
            seat = detached_runs._run_seat  # pyright: ignore[reportPrivateUsage]
            registered: list[staff.StaffRow] = []

            def register_after_lookup(run: str, target: str) -> detached_runs.PaneRunSeat | detached_runs.UnregisteredRunSeat:
                unresolved = seat(run, target)
                self.assertIsInstance(unresolved, detached_runs.UnregisteredRunSeat)
                registered.append(self.record_worker(name, fixture_runtime_id("late-detached-target")))
                return unresolved

            runs = path.parent
            before = set(runs.iterdir())
            with (self.supervisor_spawns() as spawn,
                  patch.object(detached_runs, "caller_from_environment", return_value=detached_runs.NotDetachedWorker()),
                  patch.object(detached_runs, "_run_seat", side_effect=register_after_lookup),
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(detached_runs.main(["start", "--session-dir", str(self.session), "--to", name,
                                                    "--", "late-check"]), 2)
            spawn.assert_not_called()
            self.assertIn("worker is not registered", errors.getvalue())
            self.assertEqual(set(runs.iterdir()), before)
            member, = registered
            with patch.object(detached_runs, "caller_from_environment", return_value=detached_runs.NotDetachedWorker()):
                spawned, _ = self.start(["late-check"], to=name)
            spawned.assert_called_once()
            new_path = Path(cast(list[str], spawned.call_args.args[0])[-1])
            self.assertTrue(new_path.stem.endswith(f"@{member.id}"))
            self.assertEqual(worker_lifecycle.read_json_object(new_path)["staff_id"], member.id)

        with self.subTest(target="worker shares the director's name"):
            member = self.record_worker(self.unit.name, fixture_runtime_id("shared-detached-target"))
            _ = write_config_fixture(self.root / "config/detached_runs.conf", "live_run_limit=1\n")
            command = ["shared-name-check"]
            director_run = self.live_run(command)
            self.update_record(director_run, worker=self.unit.name, staff_id=self.unit.id)

            def spawn_while_locked(*_arguments: object, **_options: object) -> SimpleNamespace:
                with self.assertRaises(BlockingIOError), worker_panes.worker_id_lock(member.id):
                    self.fail("The worker lock is free during supervisor start.")
                return SimpleNamespace(pid=4321)

            with (patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": self.unit.current_session}),
                  self.supervisor_spawns() as spawn, contextlib.redirect_stdout(io.StringIO())):
                spawn.side_effect = spawn_while_locked
                self.assertEqual(detached_runs.main(["start", "--session-dir", str(self.session), "--to", member.name,
                                                    "--", *command]), 0)
            spawn.assert_called_once()
            new_path = Path(cast(list[str], spawn.call_args.args[0])[-1])
            self.assertTrue(new_path.stem.endswith(f"@{member.id}"))
            self.assertEqual(worker_lifecycle.read_json_object(new_path)["staff_id"], member.id)

        original, = self.seat_runs(1)
        command = ["rename-check"]
        self.update_record(original, command=command)
        renamed = staff.rename(self.worker_id, "renamed-worker")
        with patch.object(detached_runs, "caller_from_environment", return_value=detached_runs.NotDetachedWorker()):
            with self.subTest(target="renamed owner"):
                spawned, output = self.start(command, to=renamed.name)
                spawned.assert_not_called()
                self.assertIn(f"run {original.stem} is already running", output)
                with self.supervisor_spawns() as spawn, contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(detached_runs.main(["start", "--session-dir", str(self.session), "--to",
                                                        renamed.name, "--", "other command"]), 4)
                spawn.assert_not_called()
                self.assertIn(original.stem, errors.getvalue())

            with self.subTest(target="another owner receives the old name"):
                replacement = self.record_worker(WORKER, fixture_runtime_id("reused-detached-target"))
                spawned, _ = self.start(command, to=replacement.name)
                spawned.assert_called_once()
                new_path = Path(cast(list[str], spawned.call_args.args[0])[-1])
                self.assertEqual(worker_lifecycle.read_json_object(new_path)["staff_id"], replacement.id)

            legacy_command = ["legacy-owner-check"]
            self.update_record(original, staff_id="", command=legacy_command)
            legacy_before = original.read_bytes()
            for limit in (1, 3):
                with self.subTest(target="legacy record has no owner ID", limit=limit):
                    _ = write_config_fixture(self.root / "config/detached_runs.conf", f"live_run_limit={limit}\n")
                    with (self.supervisor_spawns() as spawn, contextlib.redirect_stderr(io.StringIO()) as errors,
                          contextlib.redirect_stdout(io.StringIO()) as output):
                        self.assertEqual(detached_runs.main(["start", "--session-dir", str(self.session), "--to",
                                                            WORKER, "--", *legacy_command]), 2)
                    spawn.assert_not_called()
                    self.assertEqual(output.getvalue(), "")
                    self.assertIn(f"{original}: a matching run has no owner staff ID", errors.getvalue())
                    self.assertEqual(original.read_bytes(), legacy_before)

    def test_start_reports_the_run_when_prune_fails(self) -> None:
        self.assert_start_reports_the_run_when_prune_fails(PermissionError("fixture cannot delete a run log"))

    def test_start_reports_the_run_when_prune_finds_old_recorder_state(self) -> None:
        self.assert_start_reports_the_run_when_prune_fails(
            recorder_state.OldRecorderState(self.session / recorder_state.STATE_FILENAME))

    def assert_start_reports_the_run_when_prune_fails(self, error: Exception) -> None:
        def fail_prune(runs: Path) -> list[str]:
            spawn.assert_called_once()
            record, = runs.glob("*.json")
            saved = cast(detached_runs.RunRecord, json.loads(record.read_text()))
            self.assertEqual((saved["status"], saved["pid"]), ("running", 4321))
            raise error

        with (patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD_ONE}),
              patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=4321)) as spawn,
              patch.object(detached_runs, "prune_finished", side_effect=fail_prune) as prune,
              contextlib.redirect_stdout(io.StringIO()) as output,
              contextlib.redirect_stderr(io.StringIO()) as errors):
            code = detached_runs.main(["start", "--", "fixture-command"])

        self.assertEqual(code, 0)
        runs = self.session / detached_runs.DETACHED_RUNS_DIR
        record, = runs.glob("*.json")
        prune.assert_called_once_with(runs)
        self.assertIn(f"run {record.stem} started detached for {WORKER} (pid 4321,", output.getvalue())
        self.assertEqual(errors.getvalue().splitlines(), [f"detached_runs: could not prune finished runs in {runs}: {error}"])

    def test_start_refuses_systemd_run_without_an_exact_scope_prefix(self) -> None:
        for command in (
            ["systemd-run", "--user", "--quiet", "--pipe", "--wait", "-p", "MemoryMax=4G", "true"],
            ["env", "/usr/bin/systemd-run", "--user", "--", "true", "--scope"],
            ["systemd-run", "--user", "true", "--scope"],
            ["systemd-run", "--description", "--scope", "true"],
            ["systemd-run", "--descript", "--scope", "--wait", "true"],
            ["systemd-run", "--scop", "true"],
            ["systemd-run", "--quiet", "--scope", "true"],
            ["env", "-u", "X", "A=B", "systemd-run", "--wait", "true"],
            ["env", "-S", "systemd-run --wait true"],
            ["env", "-vu", "X", "systemd-run", "--wait", "true"],
            ["env", "--split-string=systemd-run --wait true"],
        ):
            with (self.subTest(command=command), self.supervisor_spawns() as popen,
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(detached_runs.main(["start", "--", *command]), 2)
                popen.assert_not_called()
                self.assertFalse((self.session / detached_runs.DETACHED_RUNS_DIR).exists())
                self.assertEqual(errors.getvalue().splitlines(), [
                    "detached_runs: Start with systemd-run --scope --collect or systemd-run --user --scope --collect. "
                    + "Use the exact option names in that order.",
                ])

    def test_start_accepts_scope_plain_command_and_shell_text(self) -> None:
        for command in (
            ["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=2G", "--", "true"],
            ["env", "/usr/bin/systemd-run", "--scope", "true"],
            ["env", "-u", "X", "A=B", "systemd-run", "--scope", "true"],
            ["true"],
            ["printf", "%s", "/usr/bin/systemd-run"],
            ["env", "--unset", "X", "A=B", "printf", "%s", "/usr/bin/systemd-run"],
            ["env", "-i", "true"],
            ["sh", "-c", "systemd-run --user --wait true"],
            ["/bin/sh", "-c", "exec /usr/bin/systemd-run"],
        ):
            with self.subTest(command=command):
                popen, _ = self.start(command)
                popen.assert_called_once()
                arguments = cast(list[str], popen.call_args.args[0])
                path = Path(arguments[-1])
                record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                self.assertEqual(record["command"], command)
                path.unlink()

    def test_run_uses_the_start_folder_instead_of_the_staff_worktree(self) -> None:
        start_folder = self.root / "repair-checkout"
        start_folder.mkdir()
        result_file = self.root / "command-folder.txt"
        command = [sys.executable, "-c",
                   "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(str(Path.cwd()))",
                   str(result_file)]
        with contextlib.chdir(start_folder):
            supervisor, output = self.start(command)
        self.assertIn("started detached", output)
        records = list((self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(records), 1)
        with contextlib.chdir(cast(str, supervisor.call_args.kwargs["cwd"])):
            record = self.supervise(records[0])
        self.assertEqual(record.get("exit_code"), 0)
        self.assertEqual(result_file.read_text(), str(start_folder))

    def test_start_refuses_at_the_live_run_limit_without_a_record_or_supervisor(self) -> None:
        _ = write_config_fixture(self.root / "config/detached_runs.conf", "live_run_limit=3\n")
        paths = self.seat_runs(3)
        before = {path: path.read_bytes() for path in paths}
        with patch.object(subprocess, "Popen") as popen, \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = detached_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
                                   "--label", "fourth check", "--", "new-check"])
        self.assertEqual(code, 4)
        popen.assert_not_called()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(set(paths[0].parent.glob("*.json")), set(paths))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)
        self.assertEqual(list(paths[0].parent.glob("*.log")), [])
        self.assertIn("live-run limit 3", err.getvalue())
        for index in range(3):
            self.assertIn(f"live-{index}: check {index}", err.getvalue())
        self.assertIn("next message", err.getvalue())
        self.assertIn("more files in one run", err.getvalue())

    def test_a_finished_run_frees_a_place_for_the_next_start(self) -> None:
        _ = write_config_fixture(self.root / "config/detached_runs.conf", "live_run_limit=3\n")
        paths = self.seat_runs(3)
        self.update_record(paths[0], status="finished", exit_code=0)
        popen, _ = self.start(["new-check"])
        popen.assert_called_once()
        # The start also deletes the finished run: its worker has no open pass.
        self.assertEqual(len(list(paths[0].parent.glob("*.json"))), 3)
        self.assertFalse(paths[0].exists())

    def finished_run(self, run_id: str, worker: str = WORKER, **fields: object) -> Path:
        """A finished record with its log and lock files."""
        path = self.record(["fixture", run_id]).replace(self.session / detached_runs.DETACHED_RUNS_DIR / f"{run_id}.json")
        self.update_record(path, **{"run_id": run_id, "worker": worker, "status": "finished", "exit_code": 0,
                                    "log": str(path.with_suffix(".log")), **fields})
        for name in (f"{run_id}.log", f"{run_id}.record.lock", f".{run_id}.delivery.lock"):
            _ = (path.parent / name).write_text("output", encoding="utf-8")
        return path

    def set_pass(self, status: str, started_at: float) -> None:
        """Give the worker a pass in the run state."""
        write_text_atomic(self.session / unit_runs.STATE_FILE, json.dumps({
            "owner_staff_id": self.unit.id, "pass": {WORKER: {
                "instance_id": "pass-1", "worker_id": self.worker_id, "status": status, "started_at": started_at}}}))

    def save_pass_stage(self, *run_ids: str) -> Path:
        """Save the pass stage and its assignment transcript, as the launcher does before input."""
        row = staff.local_snapshot().by_id(self.worker_id)
        assert row is not None
        backend = staff.CodexBackend(self.root / "codex-home")
        fixture_session(row.current_session, backend)
        row = staff.local_snapshot().by_id(self.worker_id)
        assert row is not None
        observed = staff.backend(row)
        assert isinstance(observed, staff.CodexBackend)
        path = worker_admission.transcript_source(row, observed)
        line = f"End your final reply with DONE {PASS_COMPLETION_ID}."
        entries = [{"type": "user", "message": {"role": "user", "content": text}}
                 for text in (line, *(f"Detached run: {run_id}" for run_id in run_ids))]
        write_text_atomic(path, "".join(json.dumps(entry) + "\n" for entry in entries))
        progress_history.save_dispatch_delivery(self.session, "pass-1",
            pass_recorder.DispatchDelivery(line, PASS_COMPLETION_ID, str(self.session / "reply.md")))
        return path

    def save_pass_receipt(self, *run_ids: str) -> None:
        path = self.save_pass_stage(*run_ids)
        reply = f"All checks are complete.\nDONE {PASS_COMPLETION_ID}"
        entry = {"type": "assistant", "message": {"role": "assistant", "content": reply}}
        write_text_atomic(path, path.read_text() + json.dumps(entry) + "\n")
        write_text_atomic(self.session / "reply.md", reply + "\n")

    def run_files(self, run_id: str) -> list[str]:
        folder = self.session / detached_runs.DETACHED_RUNS_DIR
        return sorted(path.name for path in folder.iterdir() if path.name.lstrip(".").startswith(run_id + "."))

    @contextlib.contextmanager
    def completion_that_prunes(self, path: Path) -> Generator[list[str]]:
        completed: list[str] = []

        def complete(record: detached_runs.RunRecord) -> None:
            with worker_panes.worker_id_lock(self.worker_id), (path.parent / ".start.lock").open("a") as admission:
                fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
            saved = cast(dict[str, object], json.loads(path.read_text()))
            self.assertTrue(saved["completion_pending"])
            _ = detached_runs.prune_finished(path.parent)
            completed.append(record["run_id"])

        with patch.object(detached_runs, "_complete_worker_dispatch", side_effect=complete):
            yield completed

    def test_reap_completes_cancelled_run_after_releasing_start_lock(self) -> None:
        self.set_pass("active", 0)
        _ = self.save_pass_stage()
        for status in ("finished", "running"):
            with self.subTest(status=status):
                path = self.finished_run(f"cancelled-{status}", pid=DEAD_PID, exit_code=143,
                                         status=status, completion_pending=status == "finished")
                expected = [] if status == "finished" else [f"cancelled-{status}"]
                with self.completion_that_prunes(path) as completed:
                    self.assertEqual(detached_runs.reap_orphans(str(self.session)), expected)
                    self.assertEqual(completed, [f"cancelled-{status}"])
                    saved = cast(dict[str, object], json.loads(path.read_text()))
                    self.assertEqual((saved["status"], saved["exit_code"], saved["completion_pending"]),
                                     ("finished", 143, False))
                    self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
                    self.assertEqual(completed, [f"cancelled-{status}"])

    def test_reap_clears_delivered_completion_without_a_staff_owner(self) -> None:
        delivery = "sent: private stopped result"
        path = self.finished_run("unowned-stop", staff_id="", pid=DEAD_PID, exit_code=143,
                                 stop_pass_requested=True, completion_pending=True, delivery=delivery)
        with isolated_staff_file(self.root / "unavailable-staff.json", {}):
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual((saved["exit_code"], saved["delivery"], saved["completion_pending"]), (143, delivery, False))

    def test_start_completes_cancelled_run_after_releasing_start_lock(self) -> None:
        self.set_pass("active", 0)
        settings = self.root / "config/detached_runs.conf"
        _ = write_config_fixture(settings, "live_run_limit=3\n")
        original = settings.read_text()
        for outcome, expected in (("start", 0), ("twin", 0), ("limit", 4), ("unusable config", 2)):
            with self.subTest(outcome=outcome):
                path = self.finished_run("cancelled", pid=DEAD_PID, exit_code=143, completion_pending=True)
                if outcome in ("twin", "limit"):
                    _ = self.seat_runs(3)
                if outcome == "unusable config":
                    _ = settings.write_text("# whole: 1..\nlive_run_limit=invalid\n")
                command = ["fixture", "1"] if outcome == "twin" else ["new-check"]
                with (self.completion_that_prunes(path) as completed,
                      self.supervisor_spawns() as spawn,
                      contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
                    try:
                        code = detached_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
                                                   "--", *command])
                        self.assertEqual(code, expected)
                        self.assertEqual(completed, ["cancelled"])
                        saved = cast(dict[str, object], json.loads(path.read_text()))
                        self.assertFalse(saved["completion_pending"])
                        self.assertEqual(saved["exit_code"], 143)
                        self.assertEqual(spawn.call_count, int(outcome == "start"))
                    finally:
                        _ = settings.write_text(original)
                        for entry in path.parent.iterdir():
                            entry.unlink()

    def collect_completions(self, count: int) -> list[detached_runs.OrphanReapResult]:
        """Defer completion events through the public reap path."""
        collected: list[detached_runs.OrphanReapResult] = []
        with patch.object(detached_runs.OrphanReapResult, "complete", autospec=True, side_effect=collected.append):
            for _ in range(count):
                self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(len(collected), count)
        return collected

    def test_collected_completions_skip_settled_and_pruned_records(self) -> None:
        for pruned in (True, False):
            with self.subTest(pruned=pruned):
                self.set_pass("active", 0)
                path = self.finished_run("cancelled", pid=DEAD_PID, exit_code=143, completion_pending=True)
                first, second = self.collect_completions(2)
                self.update_record(path, label="fresh record")
                with patch.object(detached_runs, "_complete_worker_dispatch") as complete:
                    second.complete()
                    if pruned:
                        self.set_pass("completed", 0)
                        self.save_pass_receipt("cancelled")
                        self.assertEqual(detached_runs.prune_finished(path.parent), ["cancelled"])
                    first.complete()
                    complete.assert_called_once()
                    self.assertEqual(complete.call_args.args[0]["label"], "fresh record")
                if pruned:
                    self.assertFalse(path.exists())
                    self.assertEqual(self.run_files("cancelled"), [])

    def test_collected_completions_do_not_recreate_a_missing_record(self) -> None:
        path = self.finished_run("cancelled", pid=DEAD_PID, exit_code=143, completion_pending=True)
        collected, = self.collect_completions(1)
        path.unlink()
        with (patch.object(detached_runs, "_complete_worker_dispatch") as complete,
              contextlib.redirect_stderr(io.StringIO())):
            collected.complete()
            complete.assert_not_called()
        self.assertFalse(path.exists())

    def test_collected_completions_skip_invalid_records_and_complete_later_records(self) -> None:
        read = worker_lifecycle.read_json_object
        invalid = b"{"
        for damage in ("missing", "unreadable", "mismatched ID", "locked read"):
            with self.subTest(damage=damage):
                path = self.finished_run("cancelled", pid=DEAD_PID, exit_code=143, completion_pending=True)
                later = self.finished_run("later", pid=DEAD_PID, completion_pending=True)
                collected, = self.collect_completions(1)
                if damage == "missing":
                    path.unlink()
                elif damage == "unreadable":
                    _ = path.write_bytes(invalid)
                elif damage == "mismatched ID":
                    self.update_record(path, run_id="other-run")

                def damage_during_locked_read(target: Path) -> dict[str, object]:
                    if damage == "locked read" and target == path:
                        with target.with_suffix(".completion.lock").open("r") as claim:
                            with self.assertRaises(BlockingIOError):
                                fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        _ = target.write_bytes(invalid)
                    return read(target)

                saved = invalid if damage == "locked read" else path.read_bytes() if path.exists() else b""
                errors = io.StringIO()
                with (patch.object(detached_runs, "_complete_worker_dispatch",
                                   return_value=detached_runs.CompletionReplay.HANDLED) as complete,
                      patch.object(worker_lifecycle, "read_json_object", side_effect=damage_during_locked_read),
                      contextlib.redirect_stderr(errors)):
                    collected.complete()
                complete.assert_called_once()
                self.assertEqual(complete.call_args.args[0]["run_id"], later.stem)
                self.assertIn(str(path), errors.getvalue())
                self.assertFalse(worker_lifecycle.read_json_object(later)["completion_pending"])
                if damage == "missing":
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(path.read_bytes(), saved)
                    path.unlink()

    def test_reap_rejects_a_run_id_that_does_not_match_its_file(self) -> None:
        for status in ("running", "finished", "collected"):
            with self.subTest(status=status):
                path = self.finished_run("cancelled", pid=DEAD_PID, exit_code=143,
                                         status="running" if status == "running" else "finished",
                                         completion_pending=status != "running")
                collected = self.collect_completions(1) if status == "collected" else []
                self.update_record(path, run_id="other-run")
                saved = path.read_bytes()
                with (patch.object(detached_runs, "_complete_worker_dispatch") as complete,
                      patch.object(detached_runs, "deliver", return_value="fixture delivery") as deliver,
                      contextlib.redirect_stderr(io.StringIO())):
                    if collected:
                        collected[0].complete()
                    else:
                        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
                    complete.assert_not_called()
                    deliver.assert_not_called()
                self.assertEqual(path.read_bytes(), saved)
                path.unlink()

    def test_reap_skips_a_completion_claim_that_another_holder_has(self) -> None:
        self.set_pass("active", 0)
        path = self.finished_run("cancelled", pid=DEAD_PID, exit_code=143, completion_pending=True)
        with path.with_suffix(".completion.lock").open("a") as claim:
            fcntl.flock(claim, fcntl.LOCK_EX)
            with patch.object(detached_runs, "_complete_worker_dispatch") as complete:
                self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
                complete.assert_not_called()
            saved = cast(detached_runs.RunRecord, json.loads(path.read_text()))
            self.assertTrue(saved.get("completion_pending"))
        with patch.object(detached_runs, "_complete_worker_dispatch") as complete:
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
            complete.assert_called_once()
        saved = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertFalse(saved.get("completion_pending"))

    def test_hold_stop_lookup_uses_requests_from_both_confirmed_records(self) -> None:
        path = self.record(["fixture command"])
        older, newer, staged = ("1" * 32, "2" * 32, "3" * 32)
        fallback = {"run_id": path.stem, "stop_reason": "Older hold.", "hold_stop_requests": [older]}
        write_text_atomic(path.with_suffix(".stop-confirmed"), json.dumps(fallback))
        self.update_record(path, stop_reason="Newer hold.", hold_stop_requests=[newer])
        write_text_atomic(path.with_suffix(".stop-staged"), json.dumps({**fallback, "hold_stop_requests": [staged]}))

        for request_id in (older, newer):
            with self.subTest(request_id=request_id):
                self.assertTrue(detached_runs.confirms_hold_stop(path, path.stem, request_id))
        self.assertFalse(detached_runs.confirms_hold_stop(path, path.stem, staged))
        self.assertFalse(detached_runs.confirms_hold_stop(path, "different-run", older))

    def test_prune_keeps_only_the_finished_runs_that_a_reader_needs(self) -> None:
        runs = self.session / detached_runs.DETACHED_RUNS_DIR
        opened = time.time()
        self.set_pass("active", opened)
        _ = self.finished_run("before-pass", started_at=opened - 10)
        _ = self.finished_run("in-pass", started_at=opened + 1)
        _ = self.finished_run("pending", started_at=opened - 10, completion_pending=True)
        _ = self.finished_run("running", started_at=opened - 10, status="running")
        _ = self.finished_run("director-old", worker="director", staff_id="", started_at=opened)
        _ = self.finished_run("director-new", worker="director", staff_id="", started_at=opened + 5)
        _ = self.finished_run("owner-gone", worker="gone", staff_id="00000000-0000-4000-8000-000000000000")
        self.assertEqual(sorted(detached_runs.prune_finished(runs)), ["before-pass", "director-old", "owner-gone"])
        for removed in ("before-pass", "director-old", "owner-gone"):
            self.assertEqual(self.run_files(removed), [])
        for kept in ("in-pass", "pending", "running", "director-new"):
            self.assertEqual(self.run_files(kept), [f".{kept}.delivery.lock", f"{kept}.json", f"{kept}.log", f"{kept}.record.lock"])
        self.set_pass("completed", opened)
        self.assertEqual(detached_runs.prune_finished(runs), [])
        self.save_pass_receipt("in-pass")
        self.assertEqual(detached_runs.prune_finished(runs), ["in-pass"])
        self.assertEqual(detached_runs.prune_finished(self.session / "absent"), [])

        with self.subTest(held_record_lock=True):
            path = self.finished_run("locked")
            self.save_pass_receipt("locked")
            pending_stop = path.with_suffix(detached_runs.PENDING_STOP_SUFFIX)
            pending_stop.touch()
            lock_inode = path.with_suffix(".record.lock").stat().st_ino
            guard = LauncherHangGuard("pruning a locked record")
            checked, blocked = Event(), Event()
            flock = fcntl.flock

            def observe_flock(descriptor: int | IO[str], operation: int) -> None:
                fd = descriptor if isinstance(descriptor, int) else descriptor.fileno()
                if os.fstat(fd).st_ino == lock_inode:
                    try:
                        flock(descriptor, operation | fcntl.LOCK_NB)
                    except BlockingIOError:
                        blocked.set()
                        checked.set()
                    else:
                        return
                flock(descriptor, operation)

            def prune() -> list[str]:
                try:
                    return detached_runs.prune_finished(runs)
                finally:
                    checked.set()

            with ThreadPoolExecutor(max_workers=1) as executor:
                with detached_runs.record_lock(path), patch.object(fcntl, "flock", side_effect=observe_flock):
                    worker = executor.submit(prune)
                    guard.event(checked)
                    self.assertTrue(blocked.is_set())
                    self.assertTrue(path.is_file())
                    self.assertTrue(pending_stop.is_file())
                self.assertEqual(guard.result(worker), [path.stem])
            self.assertFalse(path.exists())
            self.assertFalse(pending_stop.exists())

    def test_prune_without_staff_keeps_records_with_staff_owners(self) -> None:
        runs = self.session / detached_runs.DETACHED_RUNS_DIR
        worker = self.finished_run("worker")
        older = self.finished_run("director-old", worker="director", staff_id="", started_at=1.0)
        newest = self.finished_run("director-new", worker="director", staff_id="", started_at=2.0)

        with patch.dict(os.environ, {"STAFF_FILE": str(self.root / "absent-staff.json")}):
            self.assertEqual(detached_runs.prune_finished(runs), [older.stem])

        self.assertEqual(self.run_files(older.stem), [])
        self.assertTrue(worker.exists())
        self.assertTrue(newest.exists())

    def test_prune_waits_for_the_matching_stop_notice_acknowledgment(self) -> None:
        path = self.finished_run("stop-notice", staff_id="retired-worker", stop_notice_id="notice",
                                 stop_pass_requested=True, stop_reason="fixture stop")
        self.assertEqual(detached_runs.prune_finished(path.parent), [])
        detached_runs.acknowledge_stop_notice(path, "different-notice")
        self.assertEqual(detached_runs.prune_finished(path.parent), [])

        detached_runs.acknowledge_stop_notice(path, "notice")
        saved = worker_lifecycle.read_json_object(path)
        self.assertNotIn("stop_notice_id", saved)
        self.assertIs(saved["stop_pass_requested"], True)
        self.assertEqual(saved["stop_reason"], "fixture stop")
        self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])
        self.assertEqual(self.run_files(path.stem), [])

    def test_acknowledgment_retries_the_sync_when_the_notice_id_is_absent(self) -> None:
        path = self.finished_run("stop-notice", stop_notice_id="notice", stop_pass_requested=True,
                                 stop_reason="fixture stop")
        with patch.object(atomic_file, "sync_folder", side_effect=OSError("fixture directory sync failed")) as sync:
            with self.assertRaises(atomic_file.AtomicDurabilityUnconfirmed):
                detached_runs.acknowledge_stop_notice(path, "notice")
            saved = path.read_bytes()
            self.assertNotIn("stop_notice_id", worker_lifecycle.read_json_object(path))
            with self.assertRaisesRegex(OSError, "fixture directory sync failed"):
                detached_runs.acknowledge_stop_notice(path, "notice")
            self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(sync.call_args_list, [call(path.parent), call(path.parent)])
        detached_runs.acknowledge_stop_notice(path, "notice")
        self.assertEqual(path.read_bytes(), saved)

    def test_prune_keeps_a_delivered_result_until_the_saved_stage_completes(self) -> None:
        opened = time.time()
        self.set_pass("completed", opened)
        path = self.finished_run("later-result", started_at=opened + 1,
                                 delivery=f"sent: worker {WORKER} at w1:p1")
        self.save_pass_receipt()
        self.assertEqual(detached_runs.prune_finished(path.parent), [])
        self.assertTrue(path.exists())
        self.save_pass_receipt("later-result")
        self.assertEqual(detached_runs.prune_finished(path.parent), ["later-result"])

    def test_prune_keeps_a_pending_push_without_a_completion_event(self) -> None:
        path = self.finished_run("pending-push", staff_id="retired-worker", completion_pending=False,
                                 delivery=detached_runs.FAILED_RESULT_PUSH + "refused: fixture error")
        self.assertEqual(detached_runs.prune_finished(path.parent), [])
        self.assertTrue(path.exists())
        self.assertTrue(path.with_suffix(".log").exists())

    def test_a_start_deletes_the_owners_earlier_finished_runs(self) -> None:
        _ = self.record_worker("director", fixture_runtime_id("detached-prune-worker"))
        _ = self.finished_run("director-done", worker="director", staff_id="")
        _ = self.start(["new-check"], to="director")
        self.assertEqual(self.run_files("director-done"), [])

    def test_start_keeps_an_old_run_when_its_hold_stop_confirmation_cannot_be_copied(self) -> None:
        _ = self.record_worker("director", fixture_runtime_id("detached-hold-stop-worker"))
        request_id = "a" * 32
        path = self.finished_run("director-stopped", worker="director", staff_id="",
                                 exit_code=143, hold_stop_requests=[request_id])
        files = self.run_files(path.stem)
        hold_paths = {"BUILD_HOLD_DIR": str(self.root / "holders"),
                      "BUILD_HOLD_RELEASE_DIR": str(self.root / "release")}
        with patch.dict(os.environ, hold_paths):
            with (patch.object(shared_state_defaults, "write_stop_confirmation", side_effect=OSError("fixture copy failed")),
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                popen, output = self.start(["new-check"], to="director")
            popen.assert_called_once()
            self.assertIn("started detached for director", output)
            self.assertIn(f"kept {path}: could not copy hold stop confirmations: fixture copy failed", errors.getvalue())
            self.assertEqual(self.run_files(path.stem), files)
            self.assertFalse(shared_state_defaults.stop_confirmation_path(request_id).exists())
            self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])
            self.assertEqual(self.run_files(path.stem), [])
            self.assertEqual(shared_state_defaults.read_stop_confirmation(request_id), "stopped")

    def test_a_gone_supervisor_holds_no_place_at_the_limit(self) -> None:
        _ = write_config_fixture(self.root / "config/detached_runs.conf", "live_run_limit=3\n")
        self.set_pass("active", 0.0)  # The open pass keeps the ended run for its reader.
        paths = self.seat_runs(3)
        self.update_record(paths[0], pid=DEAD_PID)
        popen, _ = self.start(["new-check"])
        popen.assert_called_once()
        self.assertEqual(json.loads(paths[0].read_text())["status"], "finished")

    def test_a_live_twin_still_wins_at_the_live_run_limit(self) -> None:
        _ = write_config_fixture(self.root / "config/detached_runs.conf", "live_run_limit=3\n")
        paths = self.seat_runs(3)
        for state in ("supervised", "orphan with a live command"):
            with self.subTest(state=state):
                if state == "orphan with a live command":
                    owner = os.getpid()
                    started = process_start.process_start(owner)
                    assert isinstance(started, process_start.Started)
                    self.update_record(paths[1], pid=DEAD_PID, command_pid=owner, command_started=started.start)
                popen, output = self.start(["fixture", "1"])
                popen.assert_not_called()
                self.assertIn("run live-1 is already running", output)
                self.assertEqual(set(paths[0].parent.glob("*.json")), set(paths))

    def test_start_reads_the_live_run_limit_from_config(self) -> None:
        paths = self.seat_runs(1)
        config = self.root / "config/detached_runs.conf"
        _ = config.write_text("# whole: 1..\nlive_run_limit=1\n")
        with patch.object(subprocess, "Popen") as popen, contextlib.redirect_stderr(io.StringIO()) as err:
            code = detached_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
                                   "--", "new-check"])
        self.assertEqual(code, 4)
        popen.assert_not_called()
        self.assertIn("live-run limit 1", err.getvalue())
        _ = config.write_text("# whole: 1..\nlive_run_limit=2\n")
        accepted, _ = self.start(["new-check"])
        accepted.assert_called_once()
        self.assertEqual(len(list(paths[0].parent.glob("*.json"))), 2)

    def test_another_seat_does_not_use_this_seats_live_run_places(self) -> None:
        _ = write_config_fixture(self.root / "config/detached_runs.conf", "live_run_limit=3\n")
        paths = self.seat_runs(3)
        reviewer = self.record_worker("unit-review", fixture_runtime_id("another-seat-review"))
        popen, _ = self.start(["new-check"], to=reviewer.name)
        popen.assert_called_once()
        self.assertEqual(len(list(paths[0].parent.glob("*.json"))), 4)

    def test_parallel_starts_cannot_exceed_one_configured_place(self) -> None:
        _ = (self.root / "config/detached_runs.conf").write_text("# whole: 1..\nlive_run_limit=1\n")
        runs = self.session / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir()
        context = multiprocessing.get_context("fork")
        ready = context.Barrier(5)
        guard = LauncherHangGuard("parallel detached starts")

        def start(index: int, announcement: int) -> int:
            _ = os.write(announcement, b"ready\n")
            os.close(announcement)
            _ = ready.wait(timeout=guard.remaining())
            with (patch.object(worker_panes, "worker_run", worker_run),
                  patch.object(detached_run_process, "supervisor_match", return_value=True),
                  patch.object(treekey, "tree_key", return_value="fixture-tree"),
                  patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=4321))):
                return detached_runs.main(["start", "--session-dir", str(self.session),
                                           "--to", WORKER, "--", "check", str(index)])

        with ForkChildren() as children, (runs / ".start.lock").open("a") as admission:
            fcntl.flock(admission, fcntl.LOCK_EX)
            processes: list[ForkChild] = []
            announcements: list[int] = []
            for index in range(4):
                reader, writer = children.pipe()
                processes.append(children.start(
                    lambda index=index, writer=writer: start(index, writer),
                    close_fds=(admission.fileno(),), pass_fds=(writer,)))
                announcements.append(reader)
            for announcement in announcements:
                self.assertTrue(select.select([announcement], [], [], guard.remaining())[0])
                self.assertEqual(os.read(announcement, 6).decode(), "ready\n")
            _ = ready.wait(timeout=guard.remaining())
            fcntl.flock(admission, fcntl.LOCK_UN)
            results = [child.result(timeout=guard.remaining()) for child in processes]
        self.assertCountEqual([result.returncode for result in results], [0, 4, 4, 4],
                              [(result.stdout, result.stderr) for result in results])
        self.assertEqual(len(list(runs.glob("*.json"))), 1)

    def test_start_and_reap_preserve_an_unpublished_record_under_the_start_lock(self) -> None:
        _ = (self.root / "config/detached_runs.conf").write_text("# whole: 1..\nlive_run_limit=1\n")
        script = """
import fcntl
import sys
from types import SimpleNamespace
from unittest.mock import patch
from scripts.agents import detached_runs
flock = fcntl.flock
def lock(fd, operation):
    print('locking', flush=True)
    return flock(fd, operation)
with (patch.object(fcntl, 'flock', lock),
      patch.object(detached_runs, 'deliver', return_value='fixture'),
      patch.object(detached_runs.treekey, 'tree_key', return_value='key-1'),
      patch.object(detached_runs.subprocess, 'Popen', return_value=SimpleNamespace(pid=4321))):
    args = [sys.argv[1], '--session-dir', sys.argv[2]]
    if sys.argv[1] == 'start':
        args += ['--to', sys.argv[3], '--', 'new-check']
    code = detached_runs.main(args)
sys.exit(code)
"""
        for verb, expected in (("start", 4), ("reap", 0)):
            with self.subTest(verb=verb):
                path = self.live_run(["fixture", "publishing"])
                supervisor = cast(int, json.loads(path.read_text())["pid"])
                guard = LauncherHangGuard("reap during supervisor publication")
                with contextlib.ExitStack() as children, (path.parent / ".start.lock").open("a") as admission:
                    fcntl.flock(admission, fcntl.LOCK_EX)
                    try:
                        self.update_record(path, pid=0, started_at=time.time() - 600)
                        unpublished = path.read_bytes()
                        child = children.enter_context(subprocess.Popen(
                            [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", script, verb, str(self.session), WORKER],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
                        assert child.stdout is not None
                        self.assertTrue(select.select([child.stdout], [], [], guard.remaining())[0])
                        self.assertEqual(child.stdout.readline(), "locking\n")
                        self.assertEqual(path.read_bytes(), unpublished)
                        self.update_record(path, pid=supervisor)
                    finally:
                        fcntl.flock(admission, fcntl.LOCK_UN)
                    output, errors = child.communicate(timeout=guard.remaining())
                self.assertEqual(child.returncode, expected, output + errors)
                self.assertEqual(json.loads(path.read_text())["status"], "running")
                self.assertEqual(list(path.parent.glob("*.json")), [path])

    def test_a_supervisor_holds_a_place_when_start_exits_before_its_pid_write(self) -> None:
        _ = (self.root / "config/detached_runs.conf").write_text("# whole: 1..\nlive_run_limit=1\n")
        script = """
import os
import sys
from unittest.mock import patch
from scripts.agents import detached_runs
write_record = detached_runs._write_record
def publish(path, record):
    if record['pid']:
        os._exit(99)
    write_record(path, record)
with (patch.object(detached_runs, '_write_record', publish),
      patch.object(detached_runs.treekey, 'tree_key', return_value='key-1')):
    detached_runs.main(sys.argv[1:])
"""
        command = """
import socket
import sys
with socket.socket(socket.AF_UNIX) as connection:
    connection.settimeout(float(sys.argv[2]))
    connection.connect(sys.argv[1])
    connection.sendall(b'ready')
    connection.recv(1)
sys.exit(1)
"""
        guard = LauncherHangGuard("supervisor publication after start exits")
        with short_socket_directory() as directory, socket.socket(socket.AF_UNIX) as listener:
            address = str(Path(directory) / "ready.sock")
            listener.bind(address)
            listener.listen()
            listener.settimeout(guard.remaining())
            started = subprocess.run(
                [str(Path(__file__).resolve().parents[2] / "scripts/lib/py"), "-c", script, "start", "--session-dir", str(self.session), "--to", WORKER,
                 "--done-message-file", str(self.root / "unused-done-message"), "--", sys.executable,
                 "-c", command, address, str(guard.remaining())],
                capture_output=True, text=True, timeout=guard.remaining(), check=False)
            self.assertEqual(started.returncode, 99, started.stdout + started.stderr)
            connection = listener.accept()[0]
            with connection:
                connection.settimeout(guard.remaining())
                self.assertEqual(connection.recv(5), b"ready")
                paths = list((self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json"))
                self.assertEqual(len(paths), 1)
                record = cast(detached_runs.RunRecord, json.loads(paths[0].read_text()))
                self.assertGreater(record["pid"], 0)
                self.assertTrue(detached_run_process.supervises(record["pid"], paths[0]))
                supervisor = os.pidfd_open(record["pid"])
                try:
                    with patch.object(subprocess, "Popen") as popen, \
                            contextlib.redirect_stderr(io.StringIO()) as errors:
                        code = detached_runs.main(["start", "--session-dir", str(self.session), "--to", WORKER,
                                               "--", "another-check"])
                    self.assertEqual(code, 4, errors.getvalue())
                    popen.assert_not_called()
                    self.assertEqual(list(paths[0].parent.glob("*.json")), paths)
                    connection.sendall(b"x")
                    self.assertTrue(select.select([supervisor], [], [], guard.remaining())[0])
                finally:
                    os.close(supervisor)

    # hana_catalyst-inspectors started one lint run three times after an interrupt (2026-10-21).
    def test_a_second_identical_start_names_the_live_run_and_starts_nothing(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        _ = self.live_run(command)
        popen, output = self.start(command)
        popen.assert_not_called()
        self.assertIn(f"run r1 is already running for {WORKER}", output)
        self.assertIn("next message", output)
        self.assertEqual(len(list((self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json"))), 1)

    def test_a_start_after_the_files_changed_or_with_no_tree_key_starts_a_new_run(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        _ = self.live_run(command)
        for key in ("key-2", None):
            with patch.object(treekey, "tree_key", return_value=key):
                popen, _ = self.start(command)
            popen.assert_called_once()
        records = [cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
                   for path in (self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json")]
        # The key-2 run has no live supervisor, so the last start reaps it and deletes it: its worker has no open pass.
        self.assertEqual(sorted(str(record.get("tree_key")) for record in records), ["", "key-1"])

    def test_another_command_seat_or_folder_starts_a_new_run(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        _ = self.live_run(command)
        _ = self.record_worker("unit-review", fixture_runtime_id("detached-run-other-reviewer"))
        for other_command, seat in ((["bash", "verify.sh", "check", "hana"], WORKER), (command, "unit-review")):
            popen, _ = self.start(other_command, seat)
            popen.assert_called_once()
        owner = staff.by_id(self.unit.id)
        assert owner is not None
        _ = staff.set_worktree(owner.id, staff.PresentWorktree("natedev", self.root), expected=owner.worktree)
        with patch.object(treekey, "tree_key", return_value="key-after-folder-move"):
            popen, _ = self.start(command)
        popen.assert_called_once()

    def test_a_run_whose_supervisor_is_dead_or_another_process_starts_a_new_run(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        for pid in (DEAD_PID, os.getpid()):
            _ = self.live_run(command, pid)
            popen, _ = self.start(command)
            popen.assert_called_once()

    # On 2026-10-22 a unit stopped 23 queued runs by their process group: a supervisor that dies
    # delivers nothing, and its worker waits for a result that never comes.
    def test_a_start_ends_a_run_whose_supervisor_is_gone_with_a_failure_result(self) -> None:
        self.set_pass("active", 0.0)  # The open pass keeps the ended run for its reader.
        _ = self.save_pass_stage()
        path = self.record(["bash", "verify.sh", "test", "hana"])
        self.update_record(path, pid=DEAD_PID)
        legacy = path.with_name("legacy.json")
        legacy_record = cast(dict[str, object], json.loads(path.read_text()))
        del legacy_record["director_dir"]
        legacy_record["run_id"] = legacy.stem
        write_text_atomic(legacy, json.dumps(legacy_record))
        with (patch.object(turn_end, "run_finished") as complete,
              contextlib.redirect_stderr(io.StringIO()) as errors):
            popen, _ = self.start(["bash", "verify.sh", "lint", "hana"])
        complete.assert_called_once_with(self.worker_id)
        lines = errors.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(str(legacy), lines[0])
        self.assertEqual(cast(object, json.loads(legacy.read_text())), legacy_record)
        popen.assert_called_once()
        record = cast(detached_runs.RunRecord, json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual((record["status"], record.get("ended"), record.get("exit_code")),
                         ("finished", "supervisor gone", None))
        messages = cast(list[str], self.pending()["messages"])
        self.assertEqual(len(messages), 1)
        self.assertIn("Run ended with no result: verify.sh check\nResult: FAIL (no exit status)", messages[0])
        self.assertIn("Start the run again", messages[0])
        # The new run's supervisor is a mock; drop it, then a second pass finds nothing to end.
        for started in path.parent.glob("*.json"):
            if started != path:
                started.unlink()
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(len(cast(list[str], self.pending()["messages"])), 1)

    def test_a_dead_run_that_a_newer_run_of_its_command_answers_delivers_nothing(self) -> None:
        command = ["bash", "verify.sh", "lint", "hana"]
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                session_staff.inbox_path(self.worker_id).unlink(missing_ok=True)
                older = self.record(command)
                self.update_record(older, pid=DEAD_PID, started_at=time.time() - 600)
                newer = older.with_name("r2.json")
                _ = newer.write_text(older.read_text(encoding="utf-8"), encoding="utf-8")
                self.update_record(newer, run_id="r2", started_at=time.time(), pid=DEAD_PID)
                if stopped:
                    self.update_record(older, stop_pass_requested=True, stop_reason="Private stop.", pane_dispatch_id="")
                saved_newer = newer.read_bytes()
                detached_runs.reap_orphan(older)
                self.assertEqual(newer.read_bytes(), saved_newer)
                older_record = cast(detached_runs.RunRecord, json.loads(older.read_text(encoding="utf-8")))
                self.assertIn("a newer run of the same command answers", older_record.get("delivery", ""))
                self.assertEqual(detached_runs.reap_orphans(str(self.session), WORKER), ["r2"])
                messages = cast(list[str], self.pending()["messages"])
                self.assertEqual(len(messages), 1, "only the newest dead run reports")


    def test_a_supervisor_for_a_different_record_is_not_trusted(self) -> None:
        path = self.live_run(["echo", "x"])
        original = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertTrue(detached_run_process.supervises(original["pid"], path))
        another = path.with_name("another.json")
        _ = another.write_text(path.read_text())
        self.assertFalse(detached_run_process.supervises(original["pid"], another))
        self.assertFalse(detached_run_process.supervises(os.getpid(), path))
        alias = self.root / "record-alias.json"
        alias.symlink_to(path)
        for recorded in (alias, Path(os.path.relpath(path))):
            with self.subTest(recorded=recorded):
                with patch.object(detached_run_process, "process_args",
                                  return_value=["python", "detached_runs.py", "supervise", str(recorded)]):
                    self.assertTrue(detached_run_process.supervises(4321, path))
        command = ["assign.py", "impl", "prompt with spaces"]
        for arguments, matches in ((["python", "/fixture/assign.py", *command[1:]], True),
                                   ([], False),
                                   (["python", "/fixture/other.py", *command[1:]], False),
                                   (["python", "/fixture/assign.py", "review", command[2]], False)):
            with self.subTest(arguments=arguments):
                with patch.object(detached_run_process, "process_args", return_value=arguments):
                    self.assertEqual(detached_run_process.runs_command(4321, command), matches)
        for argument in ("prompt with spaces", "prompt  with  spaces"):
            with self.subTest(argument=argument):
                output = subprocess.CompletedProcess(["ps"], 0, f"python /fixture/assign.py impl {argument}\n", "")
                with (patch.object(Path, "exists", return_value=False),
                      patch.object(subprocess, "run", return_value=output)):
                    self.assertTrue(detached_run_process.runs_command(4321, ["assign.py", "impl", argument]))
        record = self.root / "run folder/r1.json"
        _ = write_config_fixture(self.root / "config/detached_runs.conf", "process_check_seconds=7\n")
        for tail, matches in (("", True), (" backup.json", False)):
            with self.subTest(tail=tail):
                output = subprocess.CompletedProcess(["ps"], 0, f"python detached_runs.py supervise {record}{tail}\n", "")
                with (patch.object(Path, "exists", return_value=False),
                      patch.object(subprocess, "run", return_value=output) as ps):
                    self.assertEqual(detached_run_process.supervises(4321, record), matches)
                ps.assert_called_once_with(["ps", "-p", "4321", "-o", "args="],
                                           capture_output=True, text=True, check=False, timeout=7)

    def test_a_live_supervisor_is_not_ended(self) -> None:
        path = self.live_run(["bash", "verify.sh", "check"])
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "running")

    def test_failed_ps_does_not_reap_a_run(self) -> None:
        absent = subprocess.CompletedProcess(["ps"], 1, "", "")
        exists = Path.exists

        def exists_without_proc(file: Path) -> bool:
            return file != Path("/proc/self") and exists(file)

        failures = (subprocess.CompletedProcess(["ps"], 1, "", "ps: fixture failure"),
                    subprocess.CompletedProcess(["ps"], 2, "", ""), OSError("ps could not start"),
                    subprocess.CompletedProcess(["ps"], 0, "(python3.13)\n", ""),
                    subprocess.CompletedProcess(["ps"], 0, "(py(test))\n", ""))
        with patch.object(Path, "exists", exists_without_proc):
            for failure in failures:
                path = self.finished_run("active", status="running", pid=DEAD_PID,
                                         command_pid=0, command_group=0)
                saved = path.read_bytes()
                with self.subTest(failure=failure):
                    with patch.object(subprocess, "run", side_effect=[failure]):
                        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
                        self.assertEqual(path.read_bytes(), saved)
                    with patch.object(subprocess, "run", return_value=absent):
                        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [path.stem])

    def test_start_exits_3_for_a_caller_without_a_registered_worker(self) -> None:
        popen = MagicMock(return_value=SimpleNamespace(pid=4321))
        for environment in ({"CODEX_THREAD_ID": "thread-unknown"}, {"CODEX_THREAD_ID": ""}):
            with patch.dict(os.environ, environment), patch.object(subprocess, "Popen", popen):
                self.assertEqual(detached_runs.main(["start", "--", "true"]), detached_runs.NOT_A_DETACHED_WORKER)
        _ = staff.retire(self.worker_id)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD_ONE}), patch.object(subprocess, "Popen", popen):
            self.assertEqual(detached_runs.main(["start", "--", "true"]), detached_runs.NOT_A_DETACHED_WORKER)
        popen.assert_not_called()

    # Criterion 2: the result is held for the seat, then the record is marked finished.

    def test_scope_prefix_needs_a_user_manager_and_honours_the_off_switch(self) -> None:
        runtime = Path(self.stack.enter_context(short_socket_directory())) / "runtime"
        (runtime / "systemd").mkdir(parents=True)
        log = self.root / "r1.log"
        on = {"XDG_RUNTIME_DIR": str(runtime)}
        with patch("shutil.which", return_value="/bin/systemd-run"):
            self.assertEqual(detached_runs.scope_prefix(log, on), [])
            with socket.socket(socket.AF_UNIX) as manager:
                manager.bind(str(runtime / "systemd" / "private"))
                prefix = detached_runs.scope_prefix(log, on)
                self.assertEqual(detached_runs.scope_prefix(log, {**on, "BUILDLOG_SCOPE": "0"}), [])
        self.assertEqual(prefix[:2], ["systemd-run", "--user"])
        self.assertIn("--slice=builds-detached-r1.slice", prefix)
        self.assertIn("--unit=detached-detect-r1.scope", prefix)
        self.assertEqual(prefix[-7:], [sys.executable, "-IS", "-c", detached_runs.SCOPE_SCRIPT,
                                      str(Path(__file__).resolve().parents[2]),
                                      "builds-detached-r1.slice", "detached-detect-r1.scope"])
        with patch("shutil.which", return_value=None):
            self.assertEqual(detached_runs.scope_prefix(log, on), [])

    def test_declared_test_kind_names_the_scope_and_stays_in_the_record(self) -> None:
        runtime = Path(self.stack.enter_context(short_socket_directory())) / "runtime"
        (runtime / "systemd").mkdir(parents=True)
        with self.supervisor_spawns(), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(detached_runs.main([
                "start", "--session-dir", str(self.session), "--to", WORKER,
                "--kind", "python-tests", "--", "env", "PYTHONPATH=.", "sh", "tests.sh",
            ]), 0)
        path, = (self.session / detached_runs.DETACHED_RUNS_DIR).glob("*.json")

        run_id, separator, staff_id = path.stem.partition("@")
        self.assertEqual((separator, staff_id), ("@", self.worker_id))
        self.assertNotIn("completion_after_slice_cleanup", worker_lifecycle.read_json_object(path))

        def run(command: list[str], _environment: dict[str, str], _log: Path) -> int:
            self.assertIn(f"--unit=detached-python-tests-{run_id}.scope", command)
            self.assertEqual(command[-4:], ["env", "PYTHONPATH=.", "sh", "tests.sh"])
            slice_name = "builds-detached-" + run_id.replace("-", "") + ".slice"
            self.assertIn(f"--slice={slice_name}", command)
            self.update_record(path, command_slice={"unit": slice_name,
                "path": f"/builds.slice/builds-detached.slice/{slice_name}", "device": 0, "inode": 1})
            return 0

        with (socket.socket(socket.AF_UNIX) as manager,
              patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime), "BUILDLOG_SCOPE": "1"}),
              patch.object(process_stop, "CGROUP_ROOT", self.root / "cgroups"),
              patch("shutil.which", return_value="/bin/systemd-run"),
              patch("scripts.agents.detached_runs._run", side_effect=run)):
            manager.bind(str(runtime / "systemd" / "private"))
            record = self.supervise(path)
        self.assertEqual(record.get("kind"), "python-tests")

    def test_detached_slice_does_not_change_a_timer_service_launch(self) -> None:
        commands: list[tuple[str, ...]] = []

        def launch(command: Sequence[str]) -> int:
            commands.append(tuple(command))
            return 0

        argv = ["--in", "1min", "--", "--to", "fixture"]
        with patch.object(time, "time_ns", return_value=123):
            self.assertEqual(send_later.main(argv, runner=launch, system="Linux"), 0)
            with patch.dict(os.environ, {"DETACHED_RUN_SLICE": "builds-detached-r1.slice"}):
                self.assertEqual(send_later.main(argv, runner=launch, system="Linux"), 0)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0], commands[1])
        self.assertIn("--on-active=1min", commands[1])
        self.assertNotIn("--scope", commands[1])

    def test_direct_scope_wrapper_inherits_the_run_slice(self) -> None:
        options = ["-p", "MemoryMax=2G", "--", "true"]
        for user in ([], ["--user"]):
            command = ["env", "A=B", "systemd-run", *user, "--scope", *options]
            path = self.record(command)
            with (patch.object(detached_runs, "scope_prefix", return_value=["fixture-scope"]),
                  patch.object(detached_runs, "_run", return_value=0) as run):
                _ = self.supervise(path)
            run.assert_any_call(["fixture-scope", "env", "A=B", "systemd-run", "--user",
                                 "--scope", "--slice-inherit", *options], ANY, path.with_suffix(".log"))

    @unittest.skipUnless(process_stop.supported(), "pidfd stop needs Linux")
    def test_slice_stop_waits_for_nested_step_scopes(self) -> None:
        for outcome in ("TERM", "KILL", "scan error", "resume", "EPERM", "still populated",
                        "natural", "cleanup interrupted", "unreadable", "missing", "replaced"):
            with self.subTest(outcome=outcome), contextlib.ExitStack() as descriptors:
                path = self.record(["fixture command"])
                proc = self.root / f"proc-{outcome}"
                cgroups = self.root / f"cgroups-{outcome}"
                slice_name = "builds-detached-r1.slice"
                relative = f"/builds.slice/builds-detached.slice/{slice_name}"
                run_slice = cgroups / relative.lstrip("/")
                command_scope = run_slice / "detached-python-tests-r1.scope"
                command_scope.mkdir(parents=True)
                step = run_slice / "run-step.scope"
                step.mkdir()
                events = run_slice / "cgroup.events"
                kill = run_slice / "cgroup.kill"
                _ = (run_slice / "cgroup.procs").write_text("")
                _ = (command_scope / "cgroup.procs").write_text("701\n")
                _ = (step / "cgroup.procs").write_text("702\n")
                _ = events.write_text("populated 1\n")
                _ = kill.write_text("0\n")
                for pid, start, name in ((701, "11", command_scope.name), (702, "22", step.name)):
                    directory = proc / str(pid)
                    directory.mkdir(parents=True)
                    fields = ["S", "1", str(pid), str(pid), *(["0"] * 15), start]
                    _ = (directory / "stat").write_text(f"{pid} (test process) " + " ".join(fields))
                    _ = (directory / "cgroup").write_text(f"0::{relative}/{name}\n")
                self.update_record(path, kind="python-tests", command_pid=701, command_group=701,
                                   command_started="11", pane_dispatch_id="")
                root = Path(__file__).resolve().parents[2]
                setup = "\n".join(("import os, sys", "from pathlib import Path", f"sys.path.insert(0, {str(root)!r})",
                                   "from scripts.lib import process_stop", f"process_stop.PROC = Path({str(proc)!r})",
                                   f"process_stop.CGROUP_ROOT = Path({str(cgroups)!r})", "os.getpid = lambda: 701", ""))
                launch = subprocess.run(
                    [sys.executable, "-IS", "-c", setup + detached_runs.SCOPE_SCRIPT, str(root), slice_name,
                     command_scope.name, sys.executable, "-c", "import os; print(os.environ['DETACHED_RUN_SLICE'])"],
                    env={**os.environ, "UNIT_DETACHED_RECORD": str(path)}, check=False, capture_output=True, timeout=2)
                self.assertEqual(launch.returncode, 0, launch.stderr)
                self.assertEqual(launch.stdout.decode().strip(), slice_name)
                published = worker_lifecycle.read_json_object(path)["command_slice"]
                self.assertEqual(published, {"unit": slice_name, "path": relative,
                                            "device": run_slice.stat().st_dev, "inode": run_slice.stat().st_ino})
                assert process_stop.is_slice_identity(published)
                # The command scope has ended. The sibling step still runs.
                (proc / "701" / "stat").unlink()
                (proc / "701" / "cgroup").unlink()
                _ = (command_scope / "cgroup.procs").write_text("")
                self.update_record(path, exit_code=143)
                if outcome == "scan error":
                    _ = (step / "cgroup.procs").write_text("unreadable\n")
                if outcome == "resume":
                    self.update_record(path, stop_reason="fixture stop", stop_pass_requested=True)
                now = 0.0
                interrupted = False

                def open_pidfd(pid: int) -> int:
                    self.assertEqual(pid, 702)
                    read, write = os.pipe()
                    _ = descriptors.callback(os.close, write)
                    return read

                def term(_descriptor: int, number: int) -> None:
                    nonlocal interrupted
                    self.assertEqual(number, signal.SIGTERM)
                    saved = worker_lifecycle.read_json_object(path)
                    self.assertNotIn("finished_at", saved)
                    if outcome in ("natural", "cleanup interrupted"):
                        self.assertEqual(saved["exit_code"], 0)
                        self.assertIs(saved["slice_cleanup_pending"], True)
                        self.assertIs(saved["slice_leftovers"], True)
                        self.assertNotIn("stop_reason", saved)
                    else:
                        self.assertEqual(saved["stop_reason"], "fixture stop")
                        self.assertIs(saved["stop_pass_requested"], True)
                    if outcome == "cleanup interrupted" and not interrupted:
                        interrupted = True
                        raise SystemExit("fixture supervisor died during cleanup")
                    self.assertEqual(kill.read_text(), "0\n")
                    if outcome == "EPERM":
                        raise PermissionError("fixture denies TERM")
                    if outcome in ("natural", "TERM", "unreadable"):
                        _ = events.write_text("populated 0\n")

                def advance(_descriptor: int, seconds: float) -> None:
                    nonlocal now
                    now += seconds
                    with path.with_suffix(".record.lock").open("a") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if outcome in ("KILL", "scan error", "resume", "cleanup interrupted") and kill.read_text() == "1\n":
                        self.assertNotIn("finished_at", worker_lifecycle.read_json_object(path))
                        _ = events.write_text("populated 0\n")

                write = os.write
                kill_stat = kill.stat()

                def write_kill(descriptor: int, data: bytes) -> int:
                    if os.path.samestat(os.fstat(descriptor), kill_stat):
                        self.assertGreaterEqual(now, 2.0)
                        if outcome == "EPERM":
                            raise PermissionError("fixture denies KILL")
                    return write(descriptor, data)

                with (patch.object(detached_run_process, "supervises", return_value=False),
                      patch.object(process_stop, "PROC", proc),
                      patch.object(process_stop, "CGROUP_ROOT", cgroups),
                      patch.object(detached_runs, "_scope_stop_graces", return_value=(2.0, 3.0)),
                      patch.object(time, "monotonic", side_effect=lambda: now),
                      patch.object(process_stop, "_wait_slice_event", side_effect=advance),
                      patch.object(os, "pidfd_open", side_effect=open_pidfd),
                      patch.object(signal, "pidfd_send_signal", side_effect=term) as send_term,
                      patch.object(os, "write", side_effect=write_kill),
                      contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO()) as stderr):
                    if outcome in ("missing", "replaced"):
                        _ = events.write_text("populated 0\n")
                        _ = run_slice.rename(run_slice.with_name(slice_name + ".old"))
                        if outcome == "missing":
                            self.assertTrue(process_stop.slice_has_ended(published))
                            self.assertFalse(detached_runs.stop_command(path, "fixture stop"))
                            self.assertNotIn("stop_reason", worker_lifecycle.read_json_object(path))
                        else:
                            run_slice.mkdir()
                            _ = events.write_text("populated 0\n")
                            with self.assertRaisesRegex(process_stop.SliceStopFailed, "the directory identity changed"):
                                _ = process_stop.slice_has_ended(published)
                        send_term.assert_not_called()
                        continue
                    if outcome == "cleanup interrupted":
                        with (patch.object(detached_runs, "_run", return_value=0),
                              self.assertRaisesRegex(SystemExit, "fixture supervisor died during cleanup")):
                            _ = detached_runs.main(["supervise", str(path)])
                        saved = worker_lifecycle.read_json_object(path)
                        self.assertEqual(saved["status"], "running")
                        self.assertEqual(saved["exit_code"], 0)
                        self.assertIs(saved["slice_cleanup_pending"], True)
                        self.assertNotIn("scope_error", saved)
                        self.assertNotIn("finished_at", saved)
                        self.assertEqual(events.read_text(), "populated 1\n")
                        with patch.object(detached_run_process, "supervises", return_value=True):
                            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
                        self.assertEqual(send_term.call_count, 1)
                        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [path.stem])
                        self.assertEqual(send_term.call_count, 2)
                        self.assertEqual(kill.read_text(), "1\n")
                        self.assertEqual(events.read_text(), "populated 0\n")
                        saved = worker_lifecycle.read_json_object(path)
                        self.assertEqual(saved["status"], "finished")
                        self.assertEqual(saved["exit_code"], 0)
                        self.assertTrue(saved["finished_at"])
                        self.assertNotIn("slice_cleanup_pending", saved)
                        self.assertIn("Result: PASS (exit 0)", self.sent.pop())
                        continue
                    if outcome in ("natural", "unreadable"):
                        if outcome == "unreadable":
                            events.unlink()
                        with patch.object(detached_runs, "_run", return_value=0):
                            status = detached_runs.main(["supervise", str(path)])
                        self.assertEqual(status, 0 if outcome == "natural" else 1)
                        if outcome == "natural":
                            result = self.sent.pop()
                            self.assertIn("Result: PASS (exit 0)", result)
                            self.assertIn("Cleanup stopped the processes that remained in the run slice.", result)
                            self.assertTrue(worker_lifecycle.read_json_object(path)["finished_at"])
                        else:
                            saved = worker_lifecycle.read_json_object(path)
                            self.assertEqual(saved["status"], "running")
                            self.assertEqual(saved["exit_code"], 0)
                            self.assertIs(saved["slice_cleanup_pending"], True)
                            self.assertNotIn("finished_at", saved)
                            self.assertIn("cannot read cgroup.events", stderr.getvalue())
                            self.assertNotIn("scope_error", saved)
                            self.assertEqual(detached_runs.main(["deliver", str(path)]), 1)
                            self.assertEqual(self.sent, [])
                            send_term.assert_not_called()
                            _ = events.write_text("populated 1\n")
                            self.assertEqual(detached_runs.main(["stop", "--reason", "fixture stop", str(path)]), 0)
                            self.assertTrue(process_stop.slice_has_ended(published))
                            self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
                            self.assertIn("Result: STOPPED (exit 0)", self.sent.pop())
                            self.assertTrue(worker_lifecycle.read_json_object(path)["finished_at"])
                        continue
                    args = ["stop", "--reason", "fixture stop", str(path)]
                    if outcome in ("TERM", "KILL", "scan error", "resume"):
                        if outcome == "resume":
                            with patch.object(detached_run_process, "supervises", return_value=True):
                                self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
                            self.assertEqual(kill.read_text(), "1\n")
                            self.assertTrue(process_stop.slice_has_ended(published))
                        else:
                            self.assertEqual(detached_runs.main(args), 0)
                        self.assertEqual(detached_runs.main(args), 0)
                        self.assertTrue(process_stop.slice_has_ended(published))
                        self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
                        self.assertIn("Result: STOPPED (exit 143)", self.sent.pop())
                        self.assertTrue(worker_lifecycle.read_json_object(path)["finished_at"])
                    else:
                        self.assertEqual(detached_runs.main(args), 1)
                        detail = "cgroup.kill failed" if outcome == "EPERM" else "populated after KILL"
                        self.assertIn(detail, stderr.getvalue())
                        saved = worker_lifecycle.read_json_object(path)
                        self.assertEqual(saved["stop_reason"], "fixture stop")
                        self.assertIs(saved["stop_pass_requested"], True)
                        self.assertNotIn("scope_error", saved)
                        self.assertNotIn("finished_at", saved)
                if outcome == "scan error":
                    send_term.assert_not_called()
                else:
                    send_term.assert_called_once_with(ANY, signal.SIGTERM)
                killed = outcome in ("KILL", "scan error", "resume", "still populated")
                self.assertEqual(kill.read_text(), "1\n" if killed else "0\n")

    def record_populated_slice(self) -> tuple[Path, Path, Path]:
        path = self.record(["fixture command"])
        cgroups = self.root / "cgroups"
        scope = cgroups / "builds.slice/builds-detached.slice/builds-detached-r1.slice"
        scope.mkdir(parents=True)
        identity: process_stop.RunSliceIdentity = {
            "unit": scope.name, "path": "/" + str(scope.relative_to(cgroups)),
            "device": scope.stat().st_dev, "inode": scope.stat().st_ino,
        }
        self.update_record(path, command_slice=identity, exit_code=143, pane_dispatch_id="")
        events = scope / "cgroup.events"
        _ = events.write_text("populated 1\n")
        return path, cgroups, events

    def test_live_run_index_keeps_a_slice_after_its_supervisor_exits(self) -> None:
        path, cgroups, events = self.record_populated_slice()
        saved_slice = worker_lifecycle.read_json_object(path)["command_slice"]
        assert process_stop.is_slice_identity(saved_slice)
        scope = process_stop.ScopeUnit(saved_slice["unit"], "fixture.scope")
        identities = {self.session: frozenset({self.worker_id})}
        index = path.with_suffix(process_stop.RUN_SLICE_RECORD_SUFFIX)

        with (patch.object(process_stop, "command_slice", return_value=saved_slice),
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(detached_run_process, "supervisor_records", return_value={}),
              patch.object(detached_run_process, "supervises", return_value=False)):
            process_stop.publish_run_slice(path, scope)
            self.assertTrue(index.is_file())
            run, = detached_runs.live_runs_in_folders(identities)
            self.assertEqual(run["run_id"], path.stem)
            self.assertEqual(run.get("command_slice"), saved_slice)

            _ = events.write_text("populated 0\n")
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [path.stem])
            self.assertFalse(index.exists())
            self.assertEqual(detached_runs.live_runs_in_folders(identities), [])

    def test_live_run_reader_finds_a_legacy_slice_without_an_index(self) -> None:
        run_id = f"20261122T050535-2458258-a09b@{self.worker_id}"
        path = self.record(["fixture command"], run_id=run_id)
        cgroups = self.root / "cgroups"
        uid = os.getuid()
        scope = (cgroups / "user.slice" / f"user-{uid}.slice" / f"user@{uid}.service"
                 / "builds.slice" / "builds-detached.slice" / "builds-detached-20261122T0505352458258a09b.slice")
        scope.mkdir(parents=True)
        saved_slice: process_stop.RunSliceIdentity = {
            "unit": scope.name, "path": "/" + str(scope.relative_to(cgroups)),
            "device": scope.stat().st_dev, "inode": scope.stat().st_ino,
        }
        self.update_record(path, command_slice=saved_slice)
        self.assertFalse(path.with_suffix(process_stop.RUN_SLICE_RECORD_SUFFIX).exists())
        events = scope / "cgroup.events"
        _ = events.write_text("populated 1\n")
        identities = {self.session: frozenset({self.worker_id})}

        with (patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(detached_run_process, "supervisor_records", return_value={})):
            run, = detached_runs.live_runs_in_folders(identities)
            self.assertEqual(run["run_id"], path.stem)
            self.assertEqual(run.get("command_slice"), saved_slice)
            _ = events.write_text("populated 0\n")
            self.assertEqual(detached_runs.live_runs_in_folders(identities), [])

    def test_live_run_reader_reports_an_index_with_a_missing_record(self) -> None:
        identities = frozenset({self.worker_id, staff.StaffId("another-worker")})
        runs = self.session / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        missing = runs / f"missing@{self.worker_id}.json"
        missing.with_suffix(process_stop.RUN_SLICE_RECORD_SUFFIX).touch()
        cgroups = self.root / "cgroups"
        empty: dict[Path, frozenset[int]] = {}
        for supervisors in (empty, detached_run_process.ProcessTableUnavailable()):
            with (self.subTest(supervisors=supervisors),
                  patch.object(process_stop, "CGROUP_ROOT", cgroups),
                  patch.object(detached_run_process, "supervisor_records", return_value=supervisors)):
                evidence = detached_runs.live_run_evidence_in_folders({self.session: identities})
                self.assertEqual(evidence.runs, ())
                error, = evidence.read_errors
                self.assertEqual(error.path, missing)
                self.assertEqual(error.staff_ids, frozenset({self.worker_id}))
                with self.assertRaisesRegex(worker_lifecycle.WorkerStateUnreadable, "indexed run record is missing"):
                    _ = evidence.require_readable()
        self.assertEqual(detached_runs.live_run_evidence_for_staff(runs, identities).read_errors[0].path, missing)

    @unittest.skipUnless(process_stop.supported(), "pidfd stop needs Linux")
    def test_empty_stop_reason_refuses_before_configuration_or_lock(self) -> None:
        path, cgroups, _ = self.record_populated_slice()
        original = path.read_bytes()
        with (patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(detached_runs, "_scope_stop_graces") as settings,
              patch.object(detached_runs, "_scope_stop_lock") as lock):
            self.assertFalse(detached_runs.stop_command(path, ""))
        settings.assert_not_called()
        lock.assert_not_called()
        self.assertEqual(path.read_bytes(), original)

    @unittest.skipUnless(process_stop.supported(), "pidfd stop needs Linux")
    def test_retry_and_reaper_sync_the_saved_stop_before_signaling(self) -> None:
        path, cgroups, events = self.record_populated_slice()
        original = path.read_bytes()
        reason = "Stop the private command."

        def end_slice(_slice: process_stop.RunSliceIdentity, _grace: float, _kill_grace: float) -> None:
            _ = events.write_text("populated 0\n")

        for recovery in ("retry", "reaper"):
            with (self.subTest(recovery=recovery),
                  patch.object(process_stop, "CGROUP_ROOT", cgroups),
                  patch.object(detached_run_process, "supervises", return_value=False),
                  patch.object(process_stop, "stop_slice", side_effect=end_slice) as stop):
                _ = path.write_bytes(original)
                _ = events.write_text("populated 1\n")
                with patch.object(atomic_file, "sync_folder", side_effect=OSError("fixture directory sync failed")) as sync:
                    with self.assertRaises(atomic_file.AtomicDurabilityUnconfirmed):
                        _ = detached_runs.stop_command(path, reason)
                    saved = path.read_bytes()
                    self.assertEqual(worker_lifecycle.read_json_object(path)["stop_reason"], reason)
                    if recovery == "retry":
                        with self.assertRaisesRegex(OSError, "fixture directory sync failed"):
                            _ = detached_runs.stop_command(path, reason)
                    else:
                        with contextlib.redirect_stderr(io.StringIO()) as errors:
                            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
                        self.assertIn("fixture directory sync failed", errors.getvalue())
                    self.assertEqual(sync.call_args_list, [call(path.parent), call(path.parent)])
                    self.assertEqual(path.read_bytes(), saved)
                    self.assertEqual(events.read_text(), "populated 1\n")
                    stop.assert_not_called()
                if recovery == "retry":
                    self.assertTrue(detached_runs.stop_command(path, reason))
                else:
                    self.assertEqual(detached_runs.reap_orphans(str(self.session)), [path.stem])
                stop.assert_called_once()
                self.assertEqual(events.read_text(), "populated 0\n")

    @unittest.skipUnless(process_stop.supported(), "pidfd stop needs Linux")
    def test_stop_refuses_when_prune_removes_the_run_during_a_lock_wait(self) -> None:
        path, cgroups, events = self.record_populated_slice()
        original = path.read_bytes()
        for suffix in (".scope-stop.lock", ".record.lock"):
            with self.subTest(lock=suffix):
                _ = path.write_bytes(original)
                lock_path = path.with_suffix(suffix)
                flock = fcntl.flock

                def prune_before_lock(descriptor: int | IO[str], operation: int) -> None:
                    fd = descriptor if isinstance(descriptor, int) else descriptor.fileno()
                    if lock_path.exists() and os.path.samestat(os.fstat(fd), lock_path.stat()):
                        path.unlink()
                    flock(descriptor, operation)

                with (patch.object(fcntl, "flock", side_effect=prune_before_lock),
                      patch.object(process_stop, "CGROUP_ROOT", cgroups),
                      patch.object(process_stop, "stop_slice") as stop):
                    self.assertFalse(detached_runs.stop_command(path, "fixture stop"))
                self.assertFalse(path.exists())
                self.assertEqual(events.read_text(), "populated 1\n")
                stop.assert_not_called()

    @unittest.skipUnless(process_stop.supported(), "pidfd stop needs Linux")
    def test_changed_kill_route_saves_no_stop(self) -> None:
        path, cgroups, events = self.record_populated_slice()
        original = path.read_bytes()
        checked = False

        def route_is_current() -> bool:
            nonlocal checked
            checked = True
            with path.with_suffix(".scope-stop.lock").open("a") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(path.read_bytes(), original)
            return False

        with (patch.object(detached_run_process, "supervises", return_value=False),
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(process_stop, "stop_slice") as stop):
            self.assertFalse(detached_runs.stop_command(path, "Kill route changed.", check=route_is_current))
            self.assertTrue(checked)
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
            stop.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(events.read_text(), "populated 1\n")

    @unittest.skipUnless(process_stop.supported(), "pidfd stop needs Linux")
    def test_watcher_waits_for_the_stop_lock_then_commits(self) -> None:
        path, cgroups, events = self.record_populated_slice()
        lock_path = path.with_suffix(".scope-stop.lock")
        lock_attempt = Event()
        guard = LauncherHangGuard("watcher stop lock")
        flock = fcntl.flock

        def observe_lock(descriptor: int | IO[str], operation: int) -> None:
            fd = descriptor if isinstance(descriptor, int) else descriptor.fileno()
            if os.path.samestat(os.fstat(fd), lock_path.stat()):
                self.assertEqual(operation & fcntl.LOCK_NB, 0)
                lock_attempt.set()
            flock(descriptor, operation)

        def finish_scope(_directory: int, _identity: process_stop.RunSliceIdentity) -> None:
            saved = worker_lifecycle.read_json_object(path)
            self.assertEqual(saved.get("stop_reason"), detached_runs.SUPERVISOR_GONE_REASON)
            self.assertIs(saved.get("stop_pass_requested"), True)
            self.assertNotIn("finished_at", saved)
            _ = events.write_text("populated 0\n")

        original = path.read_bytes()
        with (patch.object(os, "setsid"),
              patch.object(detached_run_process, "supervises", return_value=False),
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(process_stop, "_term_slice", side_effect=finish_scope) as term,
              ThreadPoolExecutor(max_workers=1) as executor):
            with lock_path.open("a") as held:
                flock(held, fcntl.LOCK_EX)
                with patch.object(fcntl, "flock", side_effect=observe_lock):
                    watcher = executor.submit(detached_runs.main, ["supervisor-gone", str(path)])
                    self.assertTrue(lock_attempt.wait(timeout=guard.remaining()))
                    self.assertFalse(watcher.done())
                    self.assertEqual(path.read_bytes(), original)
                    term.assert_not_called()
            self.assertEqual(watcher.result(timeout=guard.remaining()), 0)
            term.assert_called_once()
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["status"], "finished")
        self.assertEqual(saved.get("stop_reason"), detached_runs.SUPERVISOR_GONE_REASON)
        self.assertTrue(saved["finished_at"])
        self.assertNotIn("scope_error", saved)
        self.assertEqual(events.read_text(), "populated 0\n")

    @unittest.skipUnless(process_stop.supported(), "pidfd stop needs Linux")
    def test_watcher_retries_a_read_error_before_committing(self) -> None:
        path, cgroups, events = self.record_populated_slice()
        original = path.read_bytes()
        read_end = process_stop.slice_has_ended
        first_read = True

        def fail_once(scope: process_stop.RunSliceIdentity) -> bool:
            nonlocal first_read
            if first_read:
                first_read = False
                self.assertEqual(path.read_bytes(), original)
                raise process_stop.SliceStopFailed("fixture cgroup.events read failed")
            return read_end(scope)

        def finish_scope(_directory: int, _scope: process_stop.RunSliceIdentity) -> None:
            record = worker_lifecycle.read_json_object(path)
            self.assertEqual(record.get("stop_reason"), detached_runs.SUPERVISOR_GONE_REASON)
            self.assertIs(record.get("stop_pass_requested"), True)
            _ = events.write_text("populated 0\n")

        with (patch.object(os, "setsid"),
              patch.object(detached_run_process, "supervises", return_value=False),
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(process_stop, "slice_has_ended", side_effect=fail_once),
              patch.object(process_stop, "_term_slice", side_effect=finish_scope) as term,
              patch.object(time, "sleep") as delay):
            self.assertEqual(detached_runs.main(["supervisor-gone", str(path)]), 0)
            delay.assert_called_once()
            term.assert_called_once()
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["status"], "finished")
        self.assertTrue(saved["finished_at"])
        self.assertEqual(events.read_text(), "populated 0\n")

    @unittest.skipUnless(process_stop.supported(), "pidfd stop needs Linux")
    def test_natural_exit_after_stop_commit_reports_stopped(self) -> None:
        path, cgroups, events = self.record_populated_slice()
        self.update_record(path, exit_code=0)
        stop_slice = process_stop.stop_slice
        reason = "Stop the private command."

        def end_before_term(scope: process_stop.RunSliceIdentity, grace: float, kill_grace: float) -> None:
            self.assertEqual(worker_lifecycle.read_json_object(path).get("stop_reason"), reason)
            _ = events.write_text("populated 0\n")
            stop_slice(scope, grace, kill_grace)

        with (patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(process_stop, "stop_slice", side_effect=end_before_term),
              patch.object(process_stop, "_term_slice") as term):
            self.assertTrue(detached_runs.stop_command(path, reason))
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
            term.assert_not_called()
        (result,) = self.sent
        self.assertIn("Result: STOPPED (exit 0)", result)

    def test_start_rejects_an_unknown_kind(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as raised:
            _ = detached_runs.main(["start", "--kind", "unknown", "--", "true"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--kind", error.getvalue())

    # On 2026-11-17 a director sent TERM to a lint run's supervisor group. The supervisor died and its
    # verify.sh ran on for 3 hours with the cargo token. Each supervisor end now stops the command group.
    def test_legacy_group_stop_delivers_an_unconfirmed_result(self) -> None:
        child_source = """import json, os, socket, sys
with socket.socket(socket.AF_UNIX) as connection:
    connection.connect(sys.argv[1])
    connection.sendall((json.dumps(dict(role='command', pid=os.getpid(), group=os.getpgrp(), session=os.getsid(0))) + '\\n').encode())
    connection.recv(1)
"""
        # The fake delivery process reports the result text over the socket instead of sending it.
        supervisor_source = """import json, os, runpy, signal, socket, subprocess, sys
from pathlib import Path
from unittest.mock import patch
from scripts.agents import detached_runs
from scripts.lib.cancellation import CANCELLATION_SIGNALS
wait = subprocess.Popen.wait
start = subprocess.Popen.__init__
run = subprocess.run
address, watcher = sys.argv[4:6]
def gate_start(process, arguments, *args, **kwargs):
    if arguments[3:4] == ['detached-command']:
        arguments = [*arguments[:4], sys.executable, watcher, *arguments[6:]]
    start(process, arguments, *args, **kwargs)
def report(row):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(address)
        connection.sendall((json.dumps(row) + '\\n').encode())
def checked_wait(process, *args, **kwargs):
    if process.args[3:4] == ['detached-command']:
        defaults = all(signal.getsignal(number) == signal.SIG_DFL for number in CANCELLATION_SIGNALS)
        report(dict(role='supervisor', defaults=defaults, group=os.getpgrp()))
    return wait(process, *args, **kwargs)
def delivery(arguments, *args, **kwargs):
    if arguments[-2] != 'deliver':
        return run(arguments, *args, **kwargs)
    record_path = Path(arguments[-1])
    def send_result(record, result):
        text = detached_runs.result_text(record, result)
        report(dict(role='delivery', verb='deliver', text=text))
        return detached_runs._save_delivery(record_path, 'fixture delivery')
    with patch.object(detached_runs, 'deliver', send_result):
        status = detached_runs.main(['deliver', str(record_path)])
    return subprocess.CompletedProcess(arguments, status)
with (patch.object(subprocess.Popen, 'wait', checked_wait), patch.object(subprocess.Popen, '__init__', gate_start),
      patch.object(subprocess, 'run', delivery)):
    sys.argv = sys.argv[1:4]
    runpy.run_path(detached_runs.__file__, run_name='__main__')
"""
        reason = "stopped for timed test; run again after the release"
        with short_socket_directory() as socket_root:
            address = Path(socket_root) / "ready.sock"
            # The watcher reports the result text over the socket instead of sending it.
            # It reports after its reap finishes, so the test reads the finished record.
            watcher = self.root / "watcher.py"
            _ = watcher.write_text(f"""import json, socket, sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
from unittest.mock import patch
from scripts.agents import detached_runs
texts = []
def deliver(record, result):
    texts.append(detached_runs.result_text(record, result))
    return 'fixture delivery'
with patch.object(detached_runs, 'deliver', deliver), patch.object(detached_runs, '_complete_worker_dispatch', lambda record: None):
    status = detached_runs.main(sys.argv[1:])
with socket.socket(socket.AF_UNIX) as connection:
    connection.connect({str(address)!r})
    connection.sendall((json.dumps(dict(role='delivery', verb=sys.argv[1], text=chr(10).join(texts))) + chr(10)).encode())
sys.exit(status)
""")
            runtime = Path(socket_root) / "runtime"
            (runtime / "systemd").mkdir(parents=True)
            tools = self.root / "tools"
            tools.mkdir()
            with socket.socket(socket.AF_UNIX) as manager:
                manager.bind(str(runtime / "systemd/private"))
                stopped = 128 + signal.SIGTERM
                cases: tuple[tuple[bool, signal.Signals | None], ...] = (
                    (False, None), (False, signal.SIGTERM), (False, signal.SIGKILL))
                for scoped, cancel in cases:
                    with self.subTest(scoped=scoped, cancel=cancel), socket.socket(socket.AF_UNIX) as ready:
                        guard = LauncherHangGuard("detached process group stop")
                        ready.bind(str(address))
                        ready.listen(3)
                        path = self.record([sys.executable, "-c", child_source, str(address)])
                        environment = {**os.environ, "BUILDLOG_SCOPE": "1" if scoped else "0",
                                       "XDG_RUNTIME_DIR": str(runtime), "PATH": str(tools) + os.pathsep + os.environ["PATH"]}
                        with (Path(self.root / "supervisor.log").open("ab") as output,
                              subprocess.Popen([sys.executable, "-c", supervisor_source, "detached_runs.py",
                                                "supervise", str(path), str(address), str(watcher)],
                                               env=environment,
                                               stdout=output, stderr=subprocess.STDOUT,
                                               start_new_session=True) as supervisor,
                              contextlib.ExitStack() as connections):
                            self.update_record(path, pid=supervisor.pid)

                            def accept() -> tuple[socket.socket, dict[str, object]]:
                                self.assertTrue(select.select([ready], [], [], guard.remaining())[0],
                                                "the run did not report")
                                connection = ready.accept()[0]
                                _ = connections.enter_context(connection)
                                connection.settimeout(guard.remaining())
                                stream = connections.enter_context(connection.makefile("rb"))
                                return connection, cast(dict[str, object], json.loads(stream.readline()))

                            command_pid = 0
                            try:
                                observed: dict[str, dict[str, object]] = {}
                                command_connection: socket.socket | None = None
                                for _ in range(2):
                                    connection, row = accept()
                                    observed[str(row["role"])] = row
                                    if row["role"] == "command":
                                        command_connection = connection
                                self.assertEqual(observed["supervisor"]["defaults"], True)
                                self.assertEqual(observed["supervisor"]["group"], supervisor.pid)
                                command_pid = cast(int, observed["command"]["pid"])
                                # The command leads its own session, so a stop of its group spares the supervisor.
                                self.assertEqual((observed["command"]["group"], observed["command"]["session"]),
                                                 (command_pid, command_pid))
                                saved = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                                self.assertEqual(saved.get("command_group"), command_pid)
                                self.assertEqual(process_start.process_start(supervisor.pid),
                                                 process_start.Started(saved.get("supervisor_started", "")))
                                self.assertEqual(saved.get("command_pid"), command_pid)
                                if cancel is None:
                                    self.assertFalse(detached_runs.stop_command(path, reason))
                                else:
                                    # The signal reaches the supervisor group only; the watcher stops the command.
                                    process_group.send_process_group(supervisor.pid, cancel)
                                assert command_connection is not None
                                self.assertEqual(command_connection.recv(1), b"")
                                _, delivered = accept()
                                self.assertEqual(supervisor.wait(timeout=guard.remaining()),
                                                 1 if cancel is None else -cancel)
                            finally:
                                for group in (supervisor.pid, command_pid):
                                    with contextlib.suppress(ProcessLookupError, ValueError):
                                        process_group.send_process_group(group, signal.SIGKILL)
                                _ = supervisor.wait(timeout=guard.remaining())
                        address.unlink()
                        record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                        if cancel is None:
                            self.assertEqual(delivered["verb"], "deliver")
                            self.assertIn(f"Result: FAIL (cleanup; exit {stopped})", str(delivered["text"]))
                            self.assertIn(f"Stop reason: {reason}", str(delivered["text"]))
                            self.assertEqual((record["status"], record.get("exit_code"), record.get("ended")),
                                             ("finished", stopped, None))
                        else:
                            self.assertEqual(delivered["verb"], "supervisor-gone")
                            self.assertIn("Result: FAIL (cleanup; no exit status)", str(delivered["text"]))
                            self.assertIn(f"Stop reason: {detached_runs.SUPERVISOR_GONE_REASON}",
                                          str(delivered["text"]))
                            self.assertEqual((record["status"], record.get("exit_code"), record.get("ended")),
                                             ("finished", None, "supervisor gone"))
                        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])

    @contextlib.contextmanager
    def darwin_without_pidfds(self) -> Generator[None]:
        """Use Darwin dispatch with no Linux process APIs."""
        with contextlib.ExitStack() as stack:
            _ = stack.enter_context(patch.object(sys, "platform", "darwin"))
            for module, name in ((os, "pidfd_open"), (signal, "pidfd_send_signal")):
                if hasattr(module, name):
                    _ = stack.enter_context(patch.object(module, name))
                    delattr(module, name)
            _ = stack.enter_context(patch.object(
                process_start, "proc_table_start", side_effect=AssertionError("Darwin read a Linux PID")))
            yield

    def test_mac_stop_checks_the_saved_leader_signals_the_group_and_reaps(self) -> None:
        path = self.record(["fixture command"])
        leader = DEAD_PID
        self.update_record(path, command_pid=leader, command_group=leader, command_started="original")
        starts: dict[int, process_start.ProcessStart] = {leader: process_start.Started("replacement")}
        reason = "fixture stop"

        def read_start(pid: int, **_options: object) -> process_start.ProcessStart:
            return starts[pid]

        with (self.darwin_without_pidfds(),
              patch.object(process_start, "process_start", side_effect=read_start),
              patch.object(detached_run_process, "supervisor_match", return_value=True) as supervisor,
              patch.object(detached_runs, "_stop_caller_is_owner", return_value=True),
              patch.object(os, "killpg") as send,
              patch.object(detached_runs, "_group_is_live", return_value=False),
              patch.object(detached_runs, "_replay_completion", return_value=detached_runs.CompletionReplay.HANDLED)):
            self.assertFalse(detached_runs.stop_command(path, reason))
            send.assert_not_called()
            self.assertNotIn("stop_reason", worker_lifecycle.read_json_object(path))

            starts[leader] = process_start.Started("original")
            self.assertTrue(detached_runs.stop_command(path, reason))
            send.assert_called_once_with(leader, signal.SIGTERM)
            saved = worker_lifecycle.read_json_object(path)
            self.assertEqual(saved.get("stop_reason"), reason)
            self.assertIs(saved.get("stop_pass_requested"), True)
            self.assertIs(saved.get("self_stop"), True)
            self.assertNotIn("scope_error", saved)
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])

            starts[leader] = process_start.Gone()
            supervisor.return_value = False
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [path.stem])
            self.assertEqual(worker_lifecycle.read_json_object(path).get("status"), "finished")

    def test_mac_watcher_waits_for_the_saved_leader_before_reaping(self) -> None:
        path = self.record(["fixture command"])
        leader = DEAD_PID
        self.update_record(path, command_pid=leader, command_group=leader, command_started="original")
        start = process_start.Started("original")

        def save_status(_seconds: float) -> None:
            self.assertEqual(worker_lifecycle.read_json_object(path).get("status"), "running")
            self.assertEqual(detached_runs.main(["record-exit", "--pid", str(leader), str(path), "143"]), 0)
            read_start.return_value = process_start.Started("replacement")

        with (self.darwin_without_pidfds(),
              patch.object(process_start, "process_start", return_value=start) as read_start,
              patch.object(detached_run_process, "supervisor_match", return_value=False),
              patch.object(detached_runs, "_scope_stop_graces", return_value=(2.0, 2.0)),
              patch.object(os, "getpgrp", side_effect=[leader, os.getpgrp()]),
              patch.object(os, "setsid"),
              patch.object(os, "killpg") as send,
              patch.object(time, "sleep", side_effect=save_status) as sleep,
              patch.object(detached_runs, "_group_is_live", side_effect=[True]) as scan,
              patch.object(detached_runs, "_replay_completion", return_value=detached_runs.CompletionReplay.HANDLED)):
            self.assertEqual(detached_runs.main(["supervisor-gone", str(path)]), 0)
        scan.assert_called_once_with(leader)
        send.assert_called_once_with(leader, signal.SIGTERM)
        sleep.assert_called_once_with(0.05)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("stop_reason"), detached_runs.SUPERVISOR_GONE_REASON)
        self.assertEqual(saved.get("exit_code"), 143)
        self.assertEqual(saved.get("status"), "finished")

    def test_mac_watcher_stops_surviving_group_after_the_leader_exits(self) -> None:
        leader = DEAD_PID
        original = process_start.Started("original")
        gone = process_start.Gone()
        replacement = process_start.Started("replacement")
        unreadable = process_start.Unreadable("fixture start read failed")
        cases = (
            ("leader already gone", gone, gone, leader, True, "finished"),
            ("leader exits as watcher leaves", original, gone, leader, True, "finished"),
            ("leader unreadable with live descendants", unreadable, unreadable, leader, True, "finished"),
            ("leader unreadable after the group exits", unreadable, unreadable, leader, False, "running"),
            ("group ID reused after watcher leaves", original, replacement, leader, False, "finished"),
            ("watcher belongs to another group", gone, gone, leader + 1, False, "running"),
        )
        for index, (case, before, after, group, expect_term, status) in enumerate(cases):
            with self.subTest(case=case):
                path = self.record(["fixture command"], run_id=f"watcher-{index}")
                self.update_record(path, command_pid=leader, command_group=leader, command_started=original.start)
                events: list[str] = []
                left_group = False
                descendants_live = case != "leader unreadable after the group exits"
                record_locked = False
                scan_lock_states: list[bool] = []
                record_lock = detached_runs.record_lock

                @contextlib.contextmanager
                def track_record_lock(record_path: Path) -> Generator[None]:
                    nonlocal record_locked
                    with record_lock(record_path):
                        previous = record_locked
                        record_locked = record_locked or record_path == path
                        try:
                            yield
                        finally:
                            record_locked = previous

                def read_group() -> int:
                    if left_group:
                        return leader + 2
                    events.append("read membership")
                    return group

                def leave_group() -> None:
                    nonlocal left_group
                    events.append("leave group")
                    left_group = True

                def read_start(_pid: int, **_options: object) -> process_start.ProcessStart:
                    if isinstance(after, process_start.Unreadable) and "send TERM" in events:
                        return gone
                    return after if left_group else before

                def send_term(pid: int, number: int) -> None:
                    nonlocal descendants_live
                    self.assertTrue(descendants_live)
                    self.assertEqual((pid, number), (leader, signal.SIGTERM))
                    events.append("send TERM")
                    descendants_live = False

                def group_is_live(group_id: int, **_options: object) -> bool:
                    self.assertEqual(group_id, leader)
                    if "send TERM" not in events:
                        scan_lock_states.append(record_locked)
                    return descendants_live

                with (self.darwin_without_pidfds(),
                      patch.object(process_start, "process_start", side_effect=read_start),
                      patch.object(detached_run_process, "supervisor_match", return_value=False),
                      patch.object(detached_runs, "_scope_stop_graces", return_value=(2.0, 2.0)),
                      patch.object(detached_runs, "record_lock", side_effect=track_record_lock),
                      patch.object(os, "getpgrp", side_effect=read_group),
                      patch.object(os, "setsid", side_effect=leave_group),
                      patch.object(os, "killpg", side_effect=send_term),
                      patch.object(detached_runs, "_group_is_live", side_effect=group_is_live),
                      patch.object(detached_runs, "_replay_completion",
                                   return_value=detached_runs.CompletionReplay.HANDLED)):
                    self.assertEqual(detached_runs.main(["supervisor-gone", str(path)]), 0)

                expected = ["read membership", "leave group"]
                if expect_term:
                    expected.append("send TERM")
                self.assertEqual(events, expected)
                if isinstance(after, process_start.Unreadable):
                    self.assertEqual(scan_lock_states, [False])
                saved = worker_lifecycle.read_json_object(path)
                self.assertEqual(saved.get("status"), status)
                if expect_term:
                    self.assertEqual(saved.get("stop_reason"), detached_runs.SUPERVISOR_GONE_REASON)
                else:
                    self.assertNotIn("stop_reason", saved)

    def test_mac_watcher_limits_process_reads_to_the_stop_grace(self) -> None:
        leader = DEAD_PID
        started = "Thu Nov 19 00:00:00 2026"
        for index, (case, grace, expected) in enumerate((
            ("leader read times out", 2.0, [("-p", 2.0)]),
            ("group read uses the remaining grace", 2.0, [("-p", 2.0), ("-A", 1.25)]),
            ("no grace remains", 0.0, []),
        )):
            with self.subTest(case=case):
                path = self.record(["fixture command"], run_id=f"deadline-{index}")
                self.update_record(path, command_pid=leader, command_group=leader, command_started=started)
                began = now = 20.0
                signaled = False
                reads: list[tuple[str, float]] = []

                def send_term(pid: int, signum: int) -> None:
                    nonlocal signaled
                    self.assertEqual((pid, signum), (leader, signal.SIGTERM))
                    signaled = True

                def ps(argv: list[str], *, timeout: float, **_options: object) -> subprocess.CompletedProcess[str]:
                    nonlocal now
                    if not signaled:
                        if argv[1] == "-A":
                            return subprocess.CompletedProcess(argv, 0, f"{leader} S\n", "")
                        return subprocess.CompletedProcess(argv, 0, started + " S\n", "")
                    reads.append((argv[1], timeout))
                    if case == "group read uses the remaining grace" and argv[1] == "-p":
                        now += 0.75
                        return subprocess.CompletedProcess(argv, 1, "", "")
                    now += timeout
                    raise subprocess.TimeoutExpired(argv, timeout)

                with (self.darwin_without_pidfds(),
                      patch.object(detached_runs, "_scope_stop_graces", return_value=(grace, 2.0)),
                      patch.object(detached_runs, "reap_orphans", return_value=[]) as reap,
                      patch.object(os, "getpgrp", side_effect=[leader, os.getpgrp()]),
                      patch.object(os, "setsid"),
                      patch.object(os, "killpg", side_effect=send_term),
                      patch.object(subprocess, "run", side_effect=ps),
                      patch.object(time, "monotonic", side_effect=lambda: now),
                      patch.object(time, "sleep") as sleep):
                    self.assertEqual(detached_runs.main(["supervisor-gone", str(path)]), 0)
                self.assertTrue(signaled)
                self.assertEqual(reads, expected)
                self.assertEqual(now, began + grace)
                sleep.assert_not_called()
                reap.assert_called_once_with(str(self.session.resolve()))

    def test_watcher_limits_the_unscoped_group_scan_to_the_stop_grace(self) -> None:
        leader = DEAD_PID
        path = self.record(["fixture command"])
        self.update_record(path, command_pid=leader, command_group=leader, command_started="original",
                           scope_error="no verified scope")
        now = 20.0

        def read_start(pid: int, _proc_root: Path) -> process_start.ProcessStart:
            nonlocal now
            self.assertEqual(pid, leader)
            now += 0.75
            return process_start.Gone()

        def scan(argv: list[str], *, timeout: float, **_options: object) -> subprocess.CompletedProcess[str]:
            nonlocal now
            self.assertEqual(argv[:2], ["ps", "-A"])
            self.assertEqual(timeout, 1.25)
            now += timeout
            raise subprocess.TimeoutExpired(argv, timeout)

        with (patch.object(sys, "platform", "linux"),
              patch.object(process_stop, "supported", return_value=True),
              patch.object(detached_runs, "stop_command", return_value=False),
              patch.object(detached_runs, "_scope_stop_graces", return_value=(2.0, 2.0)),
              patch.object(detached_runs, "reap_orphans", return_value=[]) as reap,
              patch.object(os, "setsid"),
              patch.object(process_start, "proc_table_start", side_effect=read_start) as read,
              patch.object(subprocess, "run", side_effect=scan) as ps,
              patch.object(time, "monotonic", side_effect=lambda: now),
              patch.object(time, "sleep") as sleep):
            self.assertEqual(detached_runs.main(["supervisor-gone", str(path)]), 0)
        self.assertEqual(now, 22.0)
        read.assert_called_once()
        ps.assert_called_once()
        sleep.assert_not_called()
        reap.assert_called_once_with(str(self.session.resolve()))

    def test_stop_refuses_a_record_without_a_current_command_group(self) -> None:
        path = self.record([sys.executable, "-c", "pass"])
        pending_stop = path.with_suffix(detached_runs.PENDING_STOP_SUFFIX)
        started = process_start.process_start(os.getpid())
        assert isinstance(started, process_start.Started)
        cases: tuple[tuple[str, dict[str, object]], ...] = (
            ("not published", {}),
            ("leader gone", {"command_group": DEAD_PID, "command_started": started.start}),
            ("finished", {"status": "finished", "command_group": DEAD_PID, "command_started": started.start}),
        )
        for name, fields in cases:
            # The supervisor check passes, so each refusal comes from the command group record.
            with (self.subTest(case=name), patch.object(detached_runs, "_signal_stopped_command") as send,
                  patch.object(detached_run_process, "supervisor_match", return_value=True)):
                self.update_record(path, pid=os.getpid(), **fields)
                with contextlib.redirect_stderr(io.StringIO()) as error:
                    self.assertEqual(detached_runs.main(["stop", "--reason", "fixture stop", str(path)]), 1)
                self.assertIn("nothing stopped", error.getvalue())
                send.assert_not_called()
                self.assertNotIn("stop_reason", cast(dict[str, object], json.loads(path.read_text())))

        with self.subTest(case="supervisor check times out"):
            self.update_record(path, status="running", command_group=os.getpid(), command_started=started.start)
            with (patch.object(detached_run_process, "process_args",
                               return_value=detached_run_process.ProcessArgumentsUnavailable()),
                  patch.object(detached_runs, "_signal_stopped_command") as send):
                self.assertFalse(detached_runs.stop_command(path, "fixture stop"))
            send.assert_not_called()
            self.assertNotIn("stop_reason", cast(dict[str, object], json.loads(path.read_text())))

        with self.subTest(case="supervisor timeout never publishes a stop reason"):
            saved_bytes = path.read_bytes()

            def time_out(_pid: int) -> detached_run_process.ProcessArgumentsUnavailable:
                self.assertEqual(path.read_bytes(), saved_bytes)
                return detached_run_process.ProcessArgumentsUnavailable()

            with (patch.object(detached_run_process, "process_args", side_effect=time_out) as observe,
                  patch.object(detached_runs, "write_text_atomic", wraps=write_text_atomic) as write,
                  patch.object(detached_runs, "_signal_stopped_command") as send):
                self.assertFalse(detached_runs.stop_command(path, "fixture stop"))
            observe.assert_called_once_with(os.getpid())
            write.assert_not_called()
            send.assert_not_called()
            self.assertEqual(path.read_bytes(), saved_bytes)

        with self.subTest(case="supervisor changes after the stop reason is staged"):
            self.update_record(path, stop_reason="earlier stop")
            saved = cast(dict[str, object], json.loads(path.read_text()))
            staged_stop = path.with_suffix(".stop-staged")

            def same_supervisor(_pid: int, _record_path: Path) -> bool:
                self.assertEqual(cast(object, json.loads(path.read_text())), saved)
                if not staged_stop.exists():
                    return True
                staged = cast(dict[str, object], json.loads(staged_stop.read_text()))
                self.assertEqual(staged["stop_reason"], "new stop")
                self.assertIn("without a verified scope", str(staged["scope_error"]))
                self.assertFalse(pending_stop.exists())
                return False

            with (patch.object(detached_run_process, "supervisor_match", side_effect=same_supervisor) as observe,
                  patch.object(detached_runs, "_signal_stopped_command") as send):
                self.assertFalse(detached_runs.stop_command(path, "new stop"))
            self.assertEqual(observe.call_count, 2)
            send.assert_not_called()
            self.assertEqual(cast(object, json.loads(path.read_text())), saved)
            self.assertFalse(pending_stop.exists())
            self.assertFalse(staged_stop.exists())

        with self.subTest(case="directory sync fails after the stop reason is staged"):
            self.update_record(path, stop_reason="earlier stop")
            saved = cast(dict[str, object], json.loads(path.read_text()))
            with (patch.object(atomic_file, "sync_folder", side_effect=[OSError("fixture sync failure"), None]),
                  patch.object(detached_run_process, "supervisor_match", return_value=True),
                  patch.object(detached_runs, "_signal_stopped_command") as send,
                  self.assertRaisesRegex(atomic_file.AtomicDurabilityUnconfirmed, "fixture sync failure")):
                _ = detached_runs.stop_command(path, "new stop")
            send.assert_not_called()
            self.assertEqual(cast(object, json.loads(path.read_text())), saved)
            self.assertFalse(pending_stop.exists())

        with self.subTest(case="stop uses the command from the final identity check"):
            checks = 0
            read_record = worker_lifecycle.read_json_object

            def check_start(group: int) -> process_start.Started:
                nonlocal checks
                self.assertEqual(group, os.getpid())
                checks += 1
                return started

            def read_before_signal(record_path: Path) -> dict[str, object]:
                self.assertLess(checks, 2, "The final identity check must be the last operation before TERM")
                return read_record(record_path)

            with (patch.object(detached_run_process, "supervisor_match", return_value=True),
                  patch.object(process_start, "process_start", side_effect=check_start),
                  patch.object(worker_lifecycle, "read_json_object", side_effect=read_before_signal),
                  patch.object(detached_runs, "_signal_stopped_command") as send):
                self.assertFalse(detached_runs.stop_command(path, "new stop"))
            self.assertEqual(checks, 2)
            send.assert_called_once_with(detached_runs.CommandGroup(os.getpid(), started.start), signal.SIGTERM)
            self.assertFalse(pending_stop.exists())

    def test_failed_stop_sync_leaves_the_original_record_when_rollback_would_fail(self) -> None:
        path = self.record(["fixture command"])
        saved = path.read_bytes()
        replace = os.replace
        sync_failed = False

        def fail_sync(_folder: Path) -> None:
            nonlocal sync_failed
            sync_failed = True
            raise OSError("fixture sync failure")

        def fail_rollback(source: Path, target: Path) -> None:
            if sync_failed and target == path:
                raise OSError("fixture rollback failure")
            replace(source, target)

        with (patch.object(atomic_file, "sync_folder", side_effect=fail_sync),
              patch.object(os, "replace", side_effect=fail_rollback),
              patch.object(process_group, "send_process_group") as send,
              self.assertRaises(OSError)):
            _ = detached_runs.stop_with_reason(
                path, "fixture stop", verify_group=lambda: detached_runs.CommandGroup(DEAD_PID, "fixture start"),
                signal_group=process_group.send_process_group)
        self.assertTrue(sync_failed)
        send.assert_not_called()
        self.assertEqual(path.read_bytes(), saved)

    def test_stop_reason_becomes_visible_after_the_signal(self) -> None:
        replace = os.replace
        sync_folder = atomic_file.sync_folder
        for failure in ("none", "rename", "sync"):
            with self.subTest(failure=failure):
                path = self.record(["fixture command"])
                saved = path.read_bytes()
                signaled = False

                def signal_fixture(group: int, signum: int) -> None:
                    nonlocal signaled
                    self.assertEqual((group, signum), (DEAD_PID, signal.SIGTERM))
                    self.assertEqual(path.read_bytes(), saved)
                    signaled = True

                def rename_fixture(source: Path, target: Path) -> None:
                    if target == path:
                        self.assertTrue(signaled)
                        if failure == "rename":
                            raise OSError("fixture rename failure")
                    replace(source, target)

                def sync_fixture(folder: Path) -> None:
                    if signaled and failure == "sync":
                        raise OSError("fixture sync failure")
                    sync_folder(folder)

                errors = io.StringIO()
                with (patch.object(os, "replace", side_effect=rename_fixture),
                      patch.object(atomic_file, "sync_folder", side_effect=sync_fixture),
                      contextlib.redirect_stderr(errors)):
                    self.assertTrue(detached_runs.stop_with_reason(
                        path, "fixture stop", verify_group=lambda: detached_runs.CommandGroup(DEAD_PID, "fixture start"),
                        signal_group=signal_fixture))
                self.assertTrue(signaled)
                self.assertFalse(path.with_suffix(".stop-staged").exists())
                if failure == "none":
                    self.assertEqual(errors.getvalue(), "")
                else:
                    self.assertIn(str(path), errors.getvalue())
                    self.assertIn(f"fixture {failure} failure", errors.getvalue())
                if failure == "rename":
                    self.assertEqual(path.read_bytes(), saved)
                    with (patch.object(detached_runs, "scope_prefix", return_value=["fixture-scope"]),
                          patch.object(detached_runs, "_run", return_value=143) as run):
                        record = self.supervise(path)
                    run.assert_called_once_with(["fixture-scope", "fixture command"], ANY, path.with_suffix(".log"))
                    self.assertEqual(record.get("stop_reason"), "fixture stop")
                    message, = cast(list[str], self.pending()["messages"])
                    self.assertIn("Result: STOPPED (exit 143)", message)
                    self.assertIn("Stop reason: fixture stop", message)
                else:
                    self.assertEqual(worker_lifecycle.read_json_object(path).get("stop_reason"), "fixture stop")

    def test_updates_do_not_write_missing_or_invalid_records(self) -> None:
        with self.subTest(update="stop after prune"):
            path = self.finished_run("pruned-before-stop")
            self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])
            with patch.object(detached_runs, "_signal_stopped_command") as send:
                self.assertFalse(detached_runs.stop_command(path, "fixture stop"))
            send.assert_not_called()
            self.assertFalse(path.exists())

        with self.subTest(update="stop on a partial record"):
            path = self.record(["true"])
            partial = json.dumps({"run_id": path.stem})
            write_text_atomic(path, partial)
            with (patch.object(detached_runs, "write_text_atomic", wraps=write_text_atomic) as write,
                  patch.object(detached_runs, "_signal_stopped_command") as send):
                self.assertFalse(detached_runs.stop_command(path, "fixture stop"))
            write.assert_not_called()
            send.assert_not_called()
            self.assertEqual(path.read_text(), partial)

        with self.subTest(update="stop on a record named for another run"):
            path = self.record(["true"])
            started = process_start.process_start(os.getpid())
            assert isinstance(started, process_start.Started)
            self.update_record(path, run_id="other-run", command_group=os.getpid(), command_started=started.start)
            original = path.read_text()
            with (patch.object(detached_run_process, "supervisor_match", return_value=True),
                  patch.object(detached_runs, "write_text_atomic", wraps=write_text_atomic) as write,
                  patch.object(detached_runs, "_signal_stopped_command") as send):
                self.assertFalse(detached_runs.stop_command(path, "fixture stop"))
            write.assert_not_called()
            send.assert_not_called()
            self.assertEqual(path.read_text(), original)

        invalid_fields = (("command_group", "123"), ("exit_code", True), ("delivered_at", True), ("stop_pass_requested", "true"),
                          ("pane_dispatch_id", {"unavailable": 1}), ("orphan_result_superseded", False))
        for field, value in invalid_fields:
            with self.subTest(update="stop and reap on an invalid optional field", field=field):
                path = self.record(["true"])
                self.update_record(path, pid=0, started_at=0, **{field: value})
                original = path.read_bytes()
                with (patch.object(detached_runs, "write_text_atomic", wraps=write_text_atomic) as write,
                      patch.object(detached_runs, "_signal_stopped_command") as send,
                      contextlib.redirect_stderr(io.StringIO()) as errors):
                    self.assertFalse(detached_runs.stop_command(path, "fixture stop"))
                    self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
                write.assert_not_called()
                send.assert_not_called()
                self.assertFalse(path.with_suffix(detached_runs.PENDING_STOP_SUFFIX).exists())
                self.assertIn(f"{path} is not a run record", errors.getvalue())
                self.assertEqual(path.read_bytes(), original)

        with self.subTest(update="delivery after prune"):
            path = self.finished_run("pruned-before-delivery-write")
            record_lock = detached_runs.record_lock

            @contextlib.contextmanager
            def prune_then_lock(path: Path) -> Generator[None]:
                with patch.object(detached_runs, "record_lock", record_lock):
                    self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])
                with record_lock(path):
                    yield

            with (patch.object(detached_runs, "record_lock", side_effect=prune_then_lock),
                  self.assertRaisesRegex(SystemExit, "is not a run record")):
                _ = deliver_in_fixture(path)
            self.assertEqual(self.sent, [])
            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix(".log").exists())

    def test_command_receives_the_recorded_staff_owner_without_a_session_environment(self) -> None:
        command = [sys.executable, "-c", "import os; print(os.environ['BUILDLOG_OWNER_STAFF_ID'])"]
        path = self.record(command)
        _ = self.record_worker(WORKER, THREAD_TWO)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "",
                                     "BUILDLOG_OWNER_STAFF_ID": "unrelated-owner"}):
            record = self.supervise(path)
        self.assertEqual(record.get("exit_code"), 0)
        self.assertEqual(Path(record["log"]).read_text().strip(), str(self.worker_id))

    def test_plain_python_command_uses_its_copy_and_keeps_startup_hooks(self) -> None:
        selected, other, startup = (self.root / name for name in ("copy", "other", "startup"))
        for root in (selected, other):
            (root / "scripts/lib").mkdir(parents=True)
            _ = (root / "scripts/lib/py").write_text("# Checkout marker.\n")
            _ = (root / "scripts/value.py").write_text("VALUE = 1\n")
        _ = (other / "scripts/__init__.py").write_text("")
        startup.mkdir()
        _ = (startup / "sitecustomize.py").write_text("import os; os.environ['COPY_STARTUP'] = 'loaded'\n")
        command = [sys.executable, "-c",
                   "import json, os; from scripts import value; "
                   + "print(json.dumps([value.__file__, os.environ['PYTHONPATH'], os.environ['COPY_STARTUP']]))"]
        path = self.record(command)
        with (contextlib.chdir(selected), patch.dict(os.environ, {
                "PYTHONPATH": os.pathsep.join((str(other), str(selected), str(startup))),
              })):
            record = self.supervise(path)
        self.assertEqual(record.get("exit_code"), 0, Path(record["log"]).read_text())
        self.assertEqual(json.loads(Path(record["log"]).read_text()),
                         [str(selected / "scripts/value.py"),
                          startup_pythonpath(str(startup)), "loaded"])

    def test_repeat_budget_hook_selects_the_test_copy_without_exporting_it(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import json, os; from scripts.message import repeat_guard; "
             + "print(json.dumps([repeat_guard.__file__, repeat_guard.GUARD_IO_CEILING_SECONDS, "
             + "os.environ['PYTHONPATH']]))"],
            cwd=self.root, capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        filename, budget, inherited = cast(tuple[str, int, str], json.loads(result.stdout))
        root = Path(__file__).resolve().parents[2]
        self.assertEqual(Path(filename).resolve(), root / "scripts/message/repeat_guard.py")
        self.assertEqual(budget, TEST_HANG_GUARD_SECS)
        self.assertFalse(any((Path(entry or os.curdir) / "scripts/lib/py").is_file()
                             for entry in inherited.split(os.pathsep)))

    def test_a_scope_runs_the_command_once_and_a_failed_scope_runs_it_plainly(self) -> None:
        count = self.root / "count"
        command = ["/bin/sh", "-c", f'echo run >> "{count}"']
        path = self.record(command)
        scope = {"unit": "builds-detached-fixture.slice",
                 "path": "/builds.slice/builds-detached.slice/builds-detached-fixture.slice", "device": 0, "inode": 1}

        def run(_command: list[str], _environment: dict[str, str], _log: Path) -> int:
            self.update_record(path, command_slice=scope)
            _ = count.write_text("run\n")
            return 0

        with (patch.object(detached_runs, "scope_prefix", return_value=["fixture-scope"]),
              patch.object(process_stop, "CGROUP_ROOT", self.root / "cgroups"),
              patch.object(detached_runs, "_run", side_effect=run) as launch):
            record = self.supervise(path)
        launch.assert_called_once_with(["fixture-scope", *command], ANY, path.with_suffix(".log"))
        self.assertEqual((record.get("exit_code"), count.read_text(encoding="utf-8")), (0, "run\n"))
        self.assertEqual(record.get("command_slice"), scope)
        count.unlink()
        (session_staff.inbox_path(self.worker_id)).unlink()
        # A failed launcher publishes no scope.
        with patch.object(detached_runs, "scope_prefix", return_value=["/bin/sh", "-c", "exit 1", "scope"]):
            record = self.supervise(self.record(command))
        self.assertEqual((record.get("exit_code"), count.read_text(encoding="utf-8")), (0, "run\n"))

    def test_record_exit_keeps_the_run_pending_until_orphan_reaping(self) -> None:
        path = self.record(["bash", "verify.sh", "lint", "sample"])
        owner = os.getpid()
        started = process_start.process_start(owner)
        assert isinstance(started, process_start.Started)
        self.update_record(path, pid=DEAD_PID, stop_reason="fixture group stop",
                           command_pid=owner, command_started=started.start)
        with patch.object(os, "getppid", return_value=DEAD_PID):
            self.assertEqual(detached_runs.main(["record-exit", str(path), "143"]), 0)
        self.assertNotIn("exit_code", cast(detached_runs.RunRecord, json.loads(path.read_text())))
        with patch.object(os, "getppid", return_value=owner):
            self.assertEqual(detached_runs.main(["record-exit", str(path), "143"]), 0)
        record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual((record["status"], record.get("exit_code"), record.get("stop_reason")),
                         ("running", 143, "fixture group stop"))
        self.assertEqual(self.pending(), {})
        with patch.object(process_start, "process_start", return_value=process_start.Gone()):
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), ["r1"])
        self.assertEqual(json.loads(path.read_text())["status"], "finished")
        messages = cast(list[str], self.pending()["messages"])
        self.assertEqual(len(messages), 1)
        self.assertIn("Result: STOPPED (exit 143)", messages[0])
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])

        with self.subTest(state="dead supervisor and command with a saved stop"):
            path = self.record(["bash", "verify.sh", "lint", "stopped"])
            self.update_record(path, pid=DEAD_PID, supervisor_started="old-supervisor",
                               command_pid=DEAD_PID + 1, command_started="old-command",
                               stop_pass_requested=True, exit_code=143, stop_reason="Stop the orphan.")
            sent = len(self.sent)
            self.assertEqual(detached_runs.live_runs(path.parent, WORKER, identity=self.worker_id), [])
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), ["r1"])
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
            self.assertEqual(len(self.sent), sent + 1)
            self.assertIn("Result: STOPPED (exit 143)", self.sent[-1])

        with self.subTest(state="dead supervisor with a live command"):
            path = self.record(["bash", "verify.sh", "lint", "orphan"])
            self.update_record(path, pid=DEAD_PID, command_pid=owner, command_started=started.start)
            identities = frozenset({self.worker_id})
            self.assertEqual(detached_runs.live_runs(path.parent, WORKER, identity=self.worker_id), [])
            with patch.object(detached_run_process, "supervisor_records", return_value={}):
                self.assertEqual(detached_runs.live_runs_in_folders({self.session: identities}), [])

    def test_record_exit_rejects_a_reused_pid_and_a_finished_record(self) -> None:
        path = self.record(["bash", "verify.sh"])
        owner = os.getpid()
        started = process_start.process_start(owner)
        assert isinstance(started, process_start.Started)
        self.update_record(path, command_pid=owner, command_started=started.start + "-previous")
        with patch.object(os, "getppid", return_value=owner):
            self.assertEqual(detached_runs.main(["record-exit", str(path), "0"]), 0)
            self.assertNotIn("exit_code", cast(detached_runs.RunRecord, json.loads(path.read_text())))
            self.update_record(path, status="finished", command_started=started.start, exit_code=143)
            saved = path.read_bytes()
            self.assertEqual(detached_runs.main(["record-exit", str(path), "0"]), 0)
        self.assertEqual(path.read_bytes(), saved)

    def test_a_nested_command_cannot_publish_the_owners_exit_status(self) -> None:
        script = self.root / "verify.sh"
        _ = script.write_text('''#!/bin/bash
if [ "${1:-}" = nested ]; then
    "$TEST_PYTHON" "$TEST_DETACHED_SCRIPT" record-exit "$UNIT_DETACHED_RECORD" 0
    cp "$UNIT_DETACHED_RECORD" "$TEST_NESTED_RECORD"
    exit 0
fi
bash "$0" nested
"$TEST_PYTHON" "$TEST_DETACHED_SCRIPT" record-exit "$UNIT_DETACHED_RECORD" 143
cp "$UNIT_DETACHED_RECORD" "$TEST_OWNER_RECORD"
exit 143
''')
        nested, owner = self.root / "nested.json", self.root / "owner.json"
        checkout = Path(__file__).resolve().parents[2]
        with patch.dict(os.environ, {
                "TEST_PYTHON": str(checkout / "scripts/lib/py"),
                "TEST_DETACHED_SCRIPT": str(checkout / "scripts/agents/detached_runs.py"),
                "TEST_NESTED_RECORD": str(nested), "TEST_OWNER_RECORD": str(owner)}):
            record = self.supervise(self.record(["bash", str(script)]))
        self.assertNotIn("exit_code", cast(detached_runs.RunRecord, json.loads(nested.read_text())))
        self.assertEqual(json.loads(owner.read_text())["exit_code"], 143)
        self.assertEqual(record.get("exit_code"), 143)

    def test_orphan_reaping_waits_for_live_group_members_and_ignores_zombies(self) -> None:
        path = self.record(["bash", "verify.sh"])
        self.update_record(path, pid=DEAD_PID, supervisor_started="fixture-start")
        _ = self.stack.enter_context(patch.object(process_start, "process_start",
                                                 return_value=process_start.Zombie("fixture-start")))
        listing = subprocess.CompletedProcess(["ps"], 0, f"{DEAD_PID} S\n", "")
        with patch.object(subprocess, "run", return_value=listing):
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(self.pending(), {})
        self.assertEqual(json.loads(path.read_text())["status"], "running")
        listing = subprocess.CompletedProcess(["ps"], 0, f"{DEAD_PID} Z\n", "")
        with patch.object(subprocess, "run", return_value=listing):
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), ["r1"])
        self.assertEqual(json.loads(path.read_text())["status"], "finished")

    def test_orphan_reaping_recovers_after_an_interrupted_scan(self) -> None:
        path = self.record(["fixture command"])
        self.update_record(path, pid=DEAD_PID, supervisor_started="fixture-start")
        # An interrupted scan could leave this claim under the old reaper.
        path.with_suffix(".reap").touch()
        listing = subprocess.CompletedProcess(["ps"], 0, f"{DEAD_PID} Z\n", "")
        with (patch.object(process_start, "process_start", return_value=process_start.Zombie("fixture-start")),
              patch.object(subprocess, "run", return_value=listing)):
            self.assertEqual(detached_runs.reap_orphans(str(self.session)), [path.stem])
        self.assertEqual(worker_lifecycle.read_json_object(path)["status"], "finished")

    def test_orphan_reaping_keeps_the_run_pending_when_the_group_is_unreadable(self) -> None:
        path = self.record(["bash", "verify.sh"])
        self.update_record(path, pid=DEAD_PID, supervisor_started="fixture-start")
        _ = self.stack.enter_context(patch.object(process_start, "process_start",
                                                 return_value=process_start.Zombie("fixture-start")))
        for scan in (OSError("unreadable process table"),
                     subprocess.CalledProcessError(1, ["ps"], "", ""),
                     UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
                     subprocess.CompletedProcess(["ps"], 0, "", ""),
                     subprocess.CompletedProcess(["ps"], 0, "", "ps: fixture failure"),
                     subprocess.CompletedProcess(["ps"], 0, f"{DEAD_PID} Z\n", "ps: partial listing")):
            with self.subTest(scan=scan):
                with (patch.object(detached_run_process, "supervises", return_value=False),
                      patch.object(subprocess, "run", side_effect=[scan]) as group_scan):
                    self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
                group_scan.assert_called_once()
                self.assertEqual(self.pending(), {})
                self.assertEqual(worker_lifecycle.read_json_object(path)["status"], "running")

        with self.subTest(case="ps times out for both running and finished records"):
            finished = self.finished_run("awaiting-completion", pid=DEAD_PID, completion_pending=True)
            running = self.record(["pending supervisor"]).replace(path.with_name("pending-supervisor.json"))
            self.update_record(running, run_id=running.stem, pid=DEAD_PID, tree_key="key-1")
            ready = self.record(["ready to reap"]).replace(path.with_name("ready-to-reap.json"))
            self.update_record(ready, run_id=ready.stem, pid=0, started_at=0)
            saved = (running.read_bytes(), finished.read_bytes())
            exists = Path.exists

            def exists_without_proc(file: Path) -> bool:
                return file != Path("/proc/self") and exists(file)

            with (patch.object(Path, "exists", exists_without_proc),
                  patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 5))):
                self.assertEqual(detached_runs.reap_orphans(str(self.session)), [ready.stem])
                self.assertIsNotNone(detached_runs.live_twin(running.parent, self.worker_id, ["pending supervisor"], "key-1"))
                popen, _ = self.start(["new command"])
                popen.assert_called_once()
            self.assertEqual((running.read_bytes(), finished.read_bytes()), saved)

    def test_orphan_reaping_waits_for_a_command_in_a_new_session(self) -> None:
        path = self.record(["bash", "verify.sh"])
        checkout = Path(__file__).resolve().parents[2]
        child = subprocess.Popen([
            sys.executable, "-c",
            "import os, subprocess, sys; os.setsid(); print('ready', flush=True); "
            + "sys.stdin.readline(); subprocess.run(sys.argv[1:], check=True); sys.exit(143)",
            str(checkout / "scripts/lib/py"), str(checkout / "scripts/agents/detached_runs.py"),
            "record-exit", str(path), "143"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        guard = LauncherHangGuard("command in a new session")
        self.addCleanup(child.wait, timeout=guard.remaining())
        self.addCleanup(child.kill)
        assert child.stdin is not None and child.stdout is not None
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.stdout.close)
        self.assertTrue(select.select([child.stdout], [], [], guard.remaining())[0])
        self.assertEqual(child.stdout.readline(), "ready\n")
        started = process_start.process_start(child.pid)
        assert isinstance(started, process_start.Started)
        self.update_record(path, pid=DEAD_PID, command_pid=child.pid, command_started=started.start)
        self.assertNotEqual(os.getpgid(child.pid), DEAD_PID)
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        self.assertEqual(self.pending(), {})
        self.assertEqual(json.loads(path.read_text())["status"], "running")
        output, _ = child.communicate("finish\n", timeout=guard.remaining())
        self.assertEqual(child.returncode, 143, output)
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), ["r1"])
        finished = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual((finished["status"], finished.get("exit_code")), ("finished", 143))
        messages = cast(list[str], self.pending()["messages"])
        self.assertEqual(len(messages), 1)
        self.assertIn("FAIL (exit 143)", messages[0])
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])

    def test_orphan_reaping_ignores_a_group_without_its_saved_owner(self) -> None:
        for observed in (process_start.Started("reused-start"), process_start.Gone()):
            with self.subTest(observed=type(observed).__name__):
                path = self.record(["bash", "verify.sh"])
                self.update_record(path, pid=DEAD_PID, supervisor_started="original-start",
                                   command_pid=DEAD_PID + 1, command_started="original-command-start")
                listing = subprocess.CompletedProcess(["ps"], 0, f"{DEAD_PID} S\n", "")
                with (patch.object(process_start, "process_start", return_value=observed),
                      patch.object(subprocess, "run", return_value=listing) as group_scan):
                    self.assertEqual(detached_runs.reap_orphans(str(self.session)), ["r1"])
                    group_scan.assert_not_called()
                self.assertEqual(json.loads(path.read_text())["status"], "finished")
                self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])

    def test_orphan_reaping_finishes_when_the_command_group_id_is_reused(self) -> None:
        group = DEAD_PID + 2
        reused = process_start.Started("reused-start")
        for before_scan in (reused, process_start.Gone()):
            with self.subTest(before_scan=type(before_scan).__name__):
                path = self.record(["bash", "verify.sh"])
                self.update_record(path, pid=DEAD_PID, command_pid=group, command_group=group,
                                   command_started="original-start")
                listing = subprocess.CompletedProcess(["ps"], 0, f"{group} S\n", "")
                with (patch.object(process_start, "process_start", side_effect=[before_scan, reused]),
                      patch.object(subprocess, "run", return_value=listing)):
                    self.assertEqual(detached_runs.reap_orphans(str(self.session)), [path.stem])
                self.assertEqual(worker_lifecycle.read_json_object(path)["status"], "finished")

    def test_orphan_reaping_waits_for_live_members_of_the_command_group(self) -> None:
        group = DEAD_PID + 2
        path = self.record(["bash", "verify.sh"])
        self.update_record(path, pid=DEAD_PID, supervisor_started="original-start", command_pid=group,
                           command_group=group, command_started="original-command-start")
        for members, reaped in ((f"{group} S\n{group} Z\n", []), (f"{group} Z\n{DEAD_PID} S\n", ["r1"])):
            with self.subTest(members=members):
                listing = subprocess.CompletedProcess(["ps"], 0, members, "")
                with (patch.object(process_start, "process_start", return_value=process_start.Gone()),
                      patch.object(subprocess, "run", return_value=listing) as group_scan):
                    self.assertEqual(detached_runs.reap_orphans(str(self.session)), reaped)
                    group_scan.assert_called_once()
        self.assertEqual(json.loads(path.read_text())["status"], "finished")

    def test_failure_and_done_message_rules(self) -> None:
        path = self.record([sys.executable, "-c", "raise SystemExit(4)"])
        record = self.supervise(path)
        self.assertIn("Result: FAIL (exit 4)", cast(list[str], self.pending()["messages"])[0])
        self.assertEqual(record.get("exit_code"), 4)
        done = self.root / "done.txt"
        _ = done.write_text("Stage 2: go", encoding="utf-8")
        reason = "stopped for service deploy; run again after the release"
        (session_staff.inbox_path(self.worker_id)).unlink()
        path = self.record([
            sys.executable, "-c",
            "import json, sys; from pathlib import Path; path = Path(sys.argv[1]); "
            + "record = json.loads(path.read_text()); record['stop_reason'] = sys.argv[2]; "
            + "path.write_text(json.dumps(record)); raise SystemExit(143)",
            str(self.session / detached_runs.DETACHED_RUNS_DIR / "r1.json"), reason,
        ], done=str(done))
        record = self.supervise(path)
        message, = cast(list[str], self.pending()["messages"])
        self.assertIn("Result: STOPPED (exit 143)", message)
        self.assertIn(f"Stop reason: {reason}", message)
        self.assertEqual(record.get("stop_reason"), reason)
        (session_staff.inbox_path(self.worker_id)).unlink()
        record = self.supervise(self.record([sys.executable, "-c", "raise SystemExit(1)"], done=str(done)))
        self.assertEqual(self.pending(), {})
        self.assertIn("nothing delivered", record.get("delivery", ""))
        _ = self.supervise(self.record([sys.executable, "-c", "pass"], done=str(done)))
        self.assertEqual(self.pending()["messages"], ["Stage 2: go"])

        with self.subTest(delivery="fresh Python process"):
            startup = self.root / "delivery-startup"
            startup.mkdir()
            sender_pid = self.root / "sender-pid"
            _ = (startup / "sitecustomize.py").write_text('''
import os
import sys
from pathlib import Path
import runpy
runpy.run_path(CHECKOUT_STARTUP, run_name='_delivery_python_startup')["select_checkout"](sys.argv)
from scripts.message import send
from scripts.production import staff

def fake_sender(*arguments, **options):
    Path(os.environ["FIXTURE_DELIVERY_PID"]).write_text(str(os.getpid()))
    return send.Result("queued", "private fresh process")

staff.local_snapshot = staff.initial_snapshot
send.deliver_worker_text = fake_sender
'''.replace("CHECKOUT_STARTUP", repr(str(Path(__file__).resolve().parents[1] / 'lib/python_startup/sitecustomize.py'))))
            path = self.record([sys.executable, "-c", "pass"])
            with (patch.dict(os.environ, {
                    "PYTHONPATH": str(startup) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                    "FIXTURE_DELIVERY_PID": str(sender_pid),
                  }),
                  patch.object(detached_runs, "deliver_in_fresh_process", deliver_in_fresh_process)):
                record = self.supervise(path)
            self.assertTrue(sender_pid.is_file(), "The fresh process did not call the fake sender.")
            self.assertNotEqual(int(sender_pid.read_text()), record["pid"])
            self.assertNotEqual(int(sender_pid.read_text()), os.getpid())
            self.assertEqual(record.get("delivery"), "queued: private fresh process")

    def test_stop_reason_after_supervisor_completion_preserves_the_saved_result(self) -> None:
        path = self.record([sys.executable, "-c", "raise SystemExit(4)"])
        writer_progress, writer_finished = Event(), Event()
        guard = LauncherHangGuard("stop reason after supervisor completion")
        reason = "stopped for service deploy; run again after the release"
        flock = fcntl.flock
        completed = cast(dict[str, object], json.loads(path.read_text()))
        completed.update(status="finished", exit_code=4, finished_at="2026-11-10T21:00:00+00:00", delivery="fixture")

        def observe_lock(descriptor: int, operation: int) -> None:
            if current_thread().name.startswith("stop-reason"):
                writer_progress.set()
            flock(descriptor, operation)

        def save_reason() -> None:
            try:
                self.assertTrue(detached_runs.stop_with_reason(
                    path, reason, verify_group=lambda: detached_runs.CommandGroup(10, "2000"),
                    signal_group=lambda _group, _signum: None))
            finally:
                writer_finished.set()
                writer_progress.set()

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop-reason") as executor:
            with detached_runs.record_lock(path), patch.object(fcntl, "flock", side_effect=observe_lock):
                writer = executor.submit(save_reason)
                self.assertTrue(writer_progress.wait(timeout=guard.remaining()))
                self.assertFalse(writer_finished.is_set())
                self.assertFalse(writer.done())
                write_text_atomic(path, json.dumps(completed))
                self.assertFalse(writer_finished.is_set())
            writer.result(timeout=guard.remaining())
        saved = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(saved["status"], "finished")
        self.assertEqual(saved["stop_reason"], reason)
        for field in ("exit_code", "finished_at", "delivery"):
            with self.subTest(field=field):
                self.assertEqual(saved[field], completed[field])

    def test_cancelled_stop_reason_is_absent_from_the_delivered_result(self) -> None:
        reason = "stopped for service deploy; run again after the release"
        record_lock = detached_runs.record_lock
        for timing, previous in (("before publication", ""), ("before publication", "stopped for an earlier hold"),
                                 ("during recheck", ""), ("during scope retry", "")):
            with self.subTest(cancellation=timing, previous=previous):
                session_staff.inbox_path(self.worker_id).unlink(missing_ok=True)
                path = self.record([sys.executable, "-c", "pass"])
                cancelled = Event()
                if timing == "before publication":
                    if previous:
                        self.assertTrue(detached_runs.stop_with_reason(
                            path, previous, verify_group=lambda: detached_runs.CommandGroup(10, "2000"),
                            signal_group=lambda _group, _signum: None))

                    @contextlib.contextmanager
                    def cancel_before_save(path: Path) -> Generator[None]:
                        if not cancelled.is_set():
                            cancelled.set()
                            self.assertFalse(detached_runs.stop_with_reason(
                                path, reason, verify_group=detached_runs.CommandGroupUnverified,
                                signal_group=lambda _group, _signum: self.fail("a cancelled stop must not signal")))
                        with record_lock(path):
                            yield

                    with patch.object(detached_runs, "record_lock", side_effect=cancel_before_save):
                        record = self.supervise(path)
                else:
                    done = self.root / "done.txt"
                    _ = done.write_text("Result: PASS (exit 0)\nStage 2: go")
                    self.update_record(path, done_message_file=str(done), command_group=10, command_started="2000")
                    proc = self.root / f"proc-{timing.replace(' ', '-')}"
                    for pid, parent, start in ((100, 1, "1000"), (10, 100, "2000")):
                        directory = proc / str(pid)
                        directory.mkdir(parents=True)
                        _ = (directory / "stat").write_text(
                            f"{pid} (gate) S {parent} " + "0 " * 17 + start)
                    supervisor = build_hold.AuditProcess(
                        build_hold.ProcessIdentity(100, "1000"), 1, "python3",
                        ("python3", "detached_runs.py", "supervise", str(path)), str(self.root), {}, False)
                    gate = build_hold.AuditProcess(
                        build_hold.ProcessIdentity(10, "2000"), 100, "bash",
                        ("bash", "verify.sh", "check"), str(self.root), {}, False)
                    holder = build_hold.Holder("holder", datetime.now(timezone.utc), "service deploy",
                                               build_hold.UnknownReleaseEta(), "holder")
                    run = hold_enforcer.WaitingRun(gate, NoOwningSession())
                    guard = LauncherHangGuard("cancelled stop blocks supervisor publication")
                    rechecking, supervisor_record_section = Event(), Event()
                    write_attempted, supervisor_blocked = Event(), Event()
                    flock = fcntl.flock
                    read_start = build_hold.process_start_time

                    @contextlib.contextmanager
                    def observe_record_lock(path: Path) -> Generator[None]:
                        if current_thread().name.startswith("supervisor-race"):
                            supervisor_record_section.set()
                        with record_lock(path):
                            yield

                    def observe_flock(descriptor: int, operation: int) -> None:
                        if (current_thread().name.startswith("supervisor-race")
                                and supervisor_record_section.is_set()):
                            try:
                                flock(descriptor, operation | fcntl.LOCK_NB)
                            except BlockingIOError:
                                supervisor_blocked.set()
                                write_attempted.set()
                                flock(descriptor, operation)
                            else:
                                write_attempted.set()
                        else:
                            flock(descriptor, operation)

                    def reuse_at_recheck(pid: int, proc_root: Path = build_hold.PROC_ROOT) -> str:
                        if pid == 10 and path.with_suffix(".stop-staged").exists():
                            rechecking.set()
                            self.assertTrue(write_attempted.wait(timeout=guard.remaining()))
                            self.assertTrue(supervisor_blocked.is_set(), "the drain must hold the record lock")
                            _ = (proc / "10/stat").write_text("10 (reused) S 100 " + "0 " * 17 + "3000")
                            cancelled.set()
                        return read_start(pid, proc_root)

                    def supervise_at_recheck() -> detached_runs.RunRecord:
                        self.assertTrue(rechecking.wait(timeout=guard.remaining()))
                        return self.supervise(path)

                    prefix = ["fixture-scope"] if timing == "during scope retry" else []
                    hold_paths = {
                        "BUILD_HOLD_DIR": str(self.root / "holders"),
                        "BUILD_HOLD_RELEASE_DIR": str(self.root / "release"),
                        "BUILD_HOLD_ENFORCER_DIR": str(self.root / "enforcer"),
                        "BUILDLOG_DIR": str(self.root / "buildlog"),
                    }
                    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="supervisor-race") as executor, \
                         patch.dict(os.environ, hold_paths), \
                         patch.object(build_hold, "natedev_holds", return_value=build_hold.NatedevHolds((holder,))), \
                         patch.object(detached_runs, "record_lock", side_effect=observe_record_lock), \
                         patch.object(fcntl, "flock", side_effect=observe_flock), \
                         patch.object(build_hold, "process_start_time", side_effect=reuse_at_recheck), \
                         patch.object(build_hold, "natedev_holds", return_value=build_hold.NatedevHolds((holder,))), \
                         patch.object(hold_enforcer, "record_build"), patch.object(hold_enforcer, "set_stop_status"), \
                         patch.object(detached_runs, "scope_prefix", return_value=prefix), \
                         patch.object(detached_runs, "_run", side_effect=[1, 0] if prefix else [0]) as run_command:
                        publication = executor.submit(supervise_at_recheck)
                        try:
                            result = hold_enforcer.stop_waiting_run(
                                run, holder, {10: gate, 100: supervisor}, proc,
                                supervisor_args=lambda pid, proc_root: supervisor.argv,
                                signal_process=lambda pid, signum: self.fail("a cancelled stop must not signal"),
                                signal_group=lambda pid, signum: self.fail("a cancelled stop must not signal"))
                            self.assertIsInstance(result, hold_enforcer.RunIdentityChanged)
                        finally:
                            rechecking.set()
                        record = publication.result(timeout=guard.remaining())
                        self.assertEqual(run_command.call_count, 2 if prefix else 1)
                        if prefix:
                            run_command.assert_called_with([sys.executable, "-c", "pass"], ANY, Path(record["log"]))
                message, = cast(list[str], self.pending()["messages"])
                self.assertTrue(cancelled.is_set())
                self.assertNotIn(reason, message)
                if previous:
                    self.assertIn("Result: STOPPED (exit 0)", message)
                    self.assertEqual(record.get("stop_reason"), previous)
                    self.assertIn("Stop reason: " + previous, message)
                else:
                    self.assertIn("Result: PASS (exit 0)", message)
                    self.assertNotIn("stop_reason", record)
                if timing == "during recheck":
                    self.assertIn("Stage 2: go", message)

    def test_a_superseded_run_delivers_a_result_that_is_not_a_failure(self) -> None:
        done = self.root / "done.txt"
        _ = done.write_text("Stage 2: go", encoding="utf-8")
        record = self.supervise(self.record([sys.executable, "-c", "raise SystemExit(76)"], done=str(done)))
        message = cast(list[str], self.pending()["messages"])[0]
        self.assertIn("Result: SUPERSEDED (exit 76)", message)
        self.assertIn("nothing needs a repair", message)
        self.assertNotIn("FAIL", message)
        self.assertNotIn("Stage 2: go", message)
        self.assertEqual(record.get("exit_code"), 76)

    def test_stopped_results_keep_the_exit_instructions(self) -> None:
        record = cast(detached_runs.RunRecord, json.loads(self.record(["true"]).read_text()))
        record["stop_reason"] = "Build hold."
        cancellation = "verify.sh: cancelled — The worktree changed."
        _ = Path(record["log"]).write_text(cancellation + "\n" + "cleanup\n" * 30, encoding="utf-8")

        for code, instruction in ((76, "Read the newer call's result."), (77, "Resubmit after your edits.")):
            with self.subTest(code=code):
                message = detached_runs.result_text(record, detached_runs.CompletedCommand(code))
                self.assertIn(f"Result: STOPPED (exit {code})", message)
                self.assertIn("Stop reason: Build hold.", message)
                self.assertIn(instruction, message)
                if code == 77:
                    self.assertIn(cancellation, message)

    def test_a_line_over_codexs_input_limit_delivers_cut_under_the_cap(self) -> None:
        _ = write_config_fixture(self.root / "config/detached_runs.conf",
                                 "result_line_chars=200\nresult_text_chars=1800\n")
        limits = conf.read("detached_runs")
        line_chars = limits.whole("result_line_chars")
        text_chars = limits.whole("result_text_chars")
        script = "print('x' * 1_100_000)\nfor n in range(30): print(str(n) * 5_000)\nprint('last line')"
        _ = self.supervise(self.record([sys.executable, "-c", script]))
        message = cast(list[str], self.pending()["messages"])[0]
        self.assertLessEqual(len(message), text_chars)
        self.assertIn(f"{'29' * (line_chars // 2)} [cut: {10_000 - line_chars} more characters]", message)
        self.assertTrue(message.endswith("\nlast line"))
        _ = self.supervise(self.record([sys.executable, "-c", "print('x' * 1_100_000)"]))
        message = cast(list[str], self.pending()["messages"])[-1]
        self.assertIn(f"{'x' * line_chars} [cut: {1_100_000 - line_chars} more characters]", message)
        self.assertLessEqual(len(message), text_chars)

    def test_result_text_caps_the_total_size_including_the_header(self) -> None:
        config = self.root / "config/detached_runs.conf"
        _ = write_config_fixture(config, "result_line_chars=200\nresult_text_chars=1800\n")
        record = cast(detached_runs.RunRecord, json.loads(self.record(["true"]).read_text()))
        record["label"] = "verification " * 1_000
        _ = Path(record["log"]).write_text("line " * 1_000 + "\n" + ("x" * 5_000 + "\n") * 20 + "last line\n")

        text = detached_runs.result_text(record, detached_runs.CompletedCommand(1))

        self.assertLessEqual(len(text), 1_800)
        self.assertIn("Result: FAIL (exit 1)", text)
        self.assertIn(f"Log: {record['log']}", text)
        self.assertTrue(text.endswith("\nlast line"))

        _ = write_config_fixture(config, "result_line_chars=100\nresult_text_chars=1200\n")
        text = detached_runs.result_text(record, detached_runs.CompletedCommand(1))
        self.assertLessEqual(len(text), 1_200)
        self.assertIn(f"{'x' * 100} [cut: {5_000 - 100} more characters]", text)
        self.assertIn("Result: FAIL (exit 1)", text)
        self.assertIn(f"Log: {record['log']}", text)
        self.assertTrue(text.endswith("\nlast line"))

    def test_result_text_collapses_consecutive_memory_wait_lines_before_taking_the_tail(self) -> None:
        record = cast(detached_runs.RunRecord, json.loads(self.record(["true"]).read_text()))
        lines = ["first diagnostic", *(f"still waiting for memory: {n}" for n in range(30)),
                 "second diagnostic", "still waiting for memory: retry", "still waiting for memory: ready", "last line"]
        _ = Path(record["log"]).write_text("\n".join(lines))

        text = detached_runs.result_text(record, detached_runs.CompletedCommand(0))

        self.assertIn("first diagnostic", text)
        self.assertIn("second diagnostic", text)
        self.assertEqual([line for line in text.splitlines() if line.startswith("still waiting for memory")],
                         ["still waiting for memory: 29", "still waiting for memory: ready"])
        self.assertTrue(text.endswith("\nlast line"))


    # Criterion 3: the server and thread change between start and finish.

    # Criterion 4: no launcher sends the result to the director; an attached launcher holds it.


    def test_a_record_without_a_pid_counts_as_live_only_at_first(self) -> None:
        path = self.record(["true"])
        record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
        record["pid"] = 0
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), [])
        record["started_at"] = time.time() - 120
        _ = path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(detached_runs.reap_orphans(str(self.session)), ["r1"])


@dataclass(frozen=True)
class StoppedDiagnosticFixture:
    agent: test_worker.DispatchAgent
    row: staff.StaffRow
    pane: str
    dispatch: str
    detached_record: Path
    diagnostics: Path


@dataclass(frozen=True)
class StoppedPanePass:
    agent: test_worker.DispatchAgent
    row: staff.StaffRow
    pane: str
    dispatch: str
    reply: Path
    detached_record: Path


class PaneRunDeliveryTests(unittest.TestCase):
    fixture: pane_support.WorkerPaneTests = pane_support.WorkerPaneTests()
    holder_sender: contextlib.ExitStack  # pyright: ignore[reportUninitializedInstanceVariable]
    inputs: list[tuple[str, str]] = []
    notices: list[tuple[str, str]] = []
    interrupt_after_input: bool = False
    defer_transcript: bool = False
    pending_transcripts: list[tuple[Path, str]] = []

    @override
    def setUp(self) -> None:
        self.fixture = set_up_fixture(self.addCleanup, pane_support.WorkerPaneTests())
        self.inputs = []
        self.notices = []
        self.defer_transcript = False
        self.pending_transcripts = []
        process_args = detached_run_process.process_args

        def fixture_arguments(pid: int) -> list[str] | detached_run_process.ProcessCommandText | detached_run_process.ProcessArgumentsUnavailable | detached_run_process.ProcessAbsent:
            if pid == DEAD_PID:
                return detached_run_process.ProcessAbsent()
            return process_args(pid)

        _ = self.enterContext(patch.object(detached_run_process, "process_args", side_effect=fixture_arguments))
        config = self.fixture.root / "conf"
        _ = write_config_fixture(config / "detached_runs.conf",
                                 "result_push_retries=3\nresult_push_retry_seconds=5\n")
        for name in ("send", "postmaster", "codex_daemon", "shutdown"):
            _ = copy_config_file_fixture(Path(__file__).resolve().parents[2] / f"config/{name}.conf",
                                         config / f"{name}.conf")
        for name, keys in (("send", {"delivery_timeout", "kill_grace", "remote_transport_seconds",
                                     "waits_review_seconds", "caller_margin_seconds"}),
                           ("postmaster", {"wait_seconds", "claim_grace_seconds", "poll_seconds"}),
                           ("codex_daemon", {"timeout_seconds"})):
            path = config / f"{name}.conf"
            write_text_atomic(path, "\n".join(line.split("=", 1)[0] + "=0.001"
                                             if line.split("=", 1)[0] in keys else line
                                             for line in path.read_text().splitlines()) + "\n")
        self.run_dir.mkdir()
        write_text_atomic(self.run_dir / unit_runs.STATE_FILE, json.dumps({"owner_staff_id": self.fixture.parent.id}))
        _ = staff.set_run(self.fixture.parent.id, staff.OpenRun(self.run_dir))
        server_run = self.fixture.world.run

        def input_line(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:2] == ("agent", "prompt"):
                pane, text = arguments[2:4]
                pid = self.fixture.world.foreground[pane][0]
                identity = self.fixture.world.processes[pid].current_identity
                row = staff.snapshot().by_current_session(identity)
                assert row is not None
                backend = staff.backend(row)
                assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
                source = worker_admission.transcript_source(row, backend)
                entry = json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"
                if self.defer_transcript:
                    self.pending_transcripts.append((source, entry))
                else:
                    write_text_atomic(source, source.read_text() + entry)
                self.inputs.append((pane, text))
                if self.interrupt_after_input:
                    raise SystemExit("delivery interrupted after pane input")
            return server_run(*arguments, deadline=deadline)

        _ = self.enterContext(patch.object(self.fixture.world, "run", input_line))
        self.holder_sender = self.enterContext(contextlib.ExitStack())
        _ = self.holder_sender.enter_context(patch.object(send, "_send", self.holder_notice))
        _ = self.enterContext(patch.object(detached_runs, "deliver_in_fresh_process",
                                          side_effect=deliver_in_fixture))

    def holder_notice(self, options: send.Options, *,
                      membership: staff.StaffSnapshot | None = None) -> send.Result:
        del membership
        self.notices.append((options.to, options.text or ""))
        return send.Result("sent", "holder received the result")

    @property
    def run_dir(self) -> Path:
        return self.fixture.root / "run"

    def record(self, row: staff.StaffRow, run_id: str = "pane-result") -> detached_runs.RunRecord:
        runs = self.run_dir / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        log = runs / f"{run_id}.log"
        write_text_atomic(log, "private verification output\n")
        record: detached_runs.RunRecord = {"run_id": run_id, "status": "running", "session_dir": str(self.run_dir),
                "worker": row.name, "staff_id": row.id, "label": "fixture gate", "command": ["true"],
                "log": str(log), "started_at": time.time(), "pid": 0,
                "director_dir": str(self.run_dir), "done_message_file": ""}
        write_text_atomic(log.with_suffix(".json"), json.dumps(record))
        return record

    def record_confirmed_group_stop(
        self, path: Path, reason: str, *, self_stop: bool = False,
        signal_group: Callable[[detached_runs.CommandGroup, int], None] = lambda _group, _signal: None,
    ) -> bool:
        """Create the confirmed group record used by the delivery fixtures."""
        group = detached_runs.command_group(path)
        if group is None:
            self.fail("the fixture has no command group")
        return detached_runs.stop_with_reason(
            path, reason, verify_group=lambda: group,
            signal_group=lambda pid, number: signal_group(detached_runs.CommandGroup(pid, group.started), number),
            stop_pass=True, self_stop=self_stop)

    def assert_holder_result(self, record: detached_runs.RunRecord, reason: str) -> None:
        result = detached_runs.deliver(record, detached_runs.CompletedCommand(1))
        self.assertIn("sent", result)
        self.assertEqual(self.inputs, [])
        self.assertEqual(len(self.notices), 1)
        target, text = self.notices[0]
        self.assertEqual(target, f"session:{self.fixture.parent.current_session}")
        self.assertIn(reason, text)
        self.assertIn(record["run_id"], text)
        self.assertIn("private verification output", text)

    def test_detached_callers_use_current_native_sessions_from_staff(self) -> None:
        families: tuple[tuple[Literal["claude", "codex"], str], ...] = (
            ("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"))
        for family, variable in families:
            with self.subTest(family=family):
                row = self.fixture.worker("worker-" + family, family=family)
                before = self.fixture.staff_state()
                with patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session}), \
                        patch.object(staff, "local_snapshot", wraps=staff.local_snapshot) as read:
                    caller = detached_runs.caller_from_environment({variable: row.current_session, "UNIT_RUN_DIR": str(self.run_dir)})
                self.assertEqual(read.call_count, 1)
                self.assertEqual(self.fixture.staff_state(), before)
                self.assertIsInstance(caller, detached_runs.PaneCaller)
                assert isinstance(caller, detached_runs.PaneCaller)
                self.assertEqual((caller.session_dir, caller.name, caller.staff_id), (str(self.run_dir), row.name, row.id))
                with patch.dict(os.environ, {"CODEX_THREAD_ID": row.id}):
                    self.assertIsInstance(detached_runs.caller_from_environment({variable: row.id}), detached_runs.NotDetachedWorker)

    def test_a_unit_director_with_a_run_folder_owns_its_run_and_hears_its_result(self) -> None:
        director = self.fixture.parent
        self.assertEqual(director.kind, "unit")
        no_recorder = self.fixture.root / "director-without-recorder"
        self.assertFalse((no_recorder / unit_runs.STATE_FILE).exists())
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": director.current_session}),
              patch.object(treekey, "tree_key", return_value="private-tree"),
              patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=0)) as spawn,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(detached_runs.main(["start", "--", "private-land"]), detached_runs.NOT_A_DETACHED_WORKER)
            spawn.assert_not_called()
            for session in (self.run_dir, no_recorder):
                with self.subTest(session=session):
                    spawn.reset_mock()
                    self.assertEqual(detached_runs.main(["start", "--session-dir", str(session),
                                                        "--", "private-land"]), 0, output.getvalue())
                    spawn.assert_called_once()
                    path, = (session / detached_runs.DETACHED_RUNS_DIR).glob("*.json")
                    record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                    self.assertEqual((record["session_dir"], record["worker"], record["staff_id"]),
                                     (str(session), director.name, director.id))
                    write_text_atomic(Path(record["log"]), "Private land completed.\n")
                    self.assertIn("sent", detached_runs.deliver(record, detached_runs.CompletedCommand(0)))
                    self.assertEqual(self.inputs, [])
                    target, text = self.notices[-1]
                    self.assertEqual((len(self.notices), target), (1, director.id))
                    self.assertIn(f"Detached run: {record['run_id']}", text)
                    self.assertIn("Private land completed.", text)
                    self.notices.clear()
                    path.unlink()
            self.assertFalse((no_recorder / unit_runs.STATE_FILE).exists())
            foreign_run = self.fixture.root / "foreign-run"
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(detached_runs.main(["start", "--session-dir", str(foreign_run), "--to", director.name,
                                                    "--", "private-land"]), 2)
            spawn.assert_called_once()
            self.assertFalse(foreign_run.exists())

    def test_director_completions_can_be_pruned_with_an_unreadable_stopped_receipt(self) -> None:
        director = self.fixture.parent
        state = unit_runs.read_run_fields(self.run_dir)
        state["stopped_pass_notices"] = {"bad": "unreadable receipt"}
        write_text_atomic(self.run_dir / unit_runs.STATE_FILE, json.dumps(state))
        paths: set[Path] = set()
        for stopped in (False, True):
            with (self.subTest(stopped=stopped),
                  patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": director.current_session}),
                  patch.object(treekey, "tree_key", return_value="private-tree"),
                  patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=0)),
                  contextlib.redirect_stdout(io.StringIO())):
                self.assertEqual(detached_runs.main(["start", "--session-dir", str(self.run_dir), "--",
                                                     "private-command", str(stopped)]), 0)
                runs = self.run_dir / detached_runs.DETACHED_RUNS_DIR
                path, = set(runs.glob("*.json")) - paths
                paths.add(path)
                saved = worker_lifecycle.read_json_object(path)
                self.assertEqual(saved["pane_dispatch_id"], "")
                saved.update(status="finished", exit_code=143 if stopped else 0, pid=DEAD_PID,
                             delivery=detached_runs.ORPHAN_RESULT_PENDING, completion_pending=True)
                if stopped:
                    saved.update(stop_pass_requested=True, stop_reason="Private stop.")
                write_text_atomic(path, json.dumps(saved))
                self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                result = worker_lifecycle.read_json_object(path)
                self.assertIs(result["completion_pending"], False)
                self.assertTrue(str(result["delivery"]).startswith("sent:"))
        self.assertEqual(len(self.notices), 2)
        self.assertIn("Result: PASS (exit 0)", self.notices[0][1])
        self.assertIn("Result: STOPPED (exit 143)", self.notices[1][1])
        _ = self.record(director, "later")
        remaining = {path.stem for path in paths if path.exists()}
        self.assertTrue(remaining)
        self.assertEqual(set(detached_runs.prune_finished(self.run_dir / detached_runs.DETACHED_RUNS_DIR)), remaining)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_a_showrunner_starts_a_new_session_and_hears_its_result_with_a_shared_name(self) -> None:
        showrunner, = staff.snapshot().find(kind="showrunner")
        unit = fixture_member("unit", name=showrunner.name, parent=showrunner.id)
        self.assertEqual(unit.name, showrunner.name)
        self.assertNotEqual(unit.id, showrunner.id)
        run_dir = self.fixture.root / "showrunner-state"
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": showrunner.current_session}),
              patch.object(treekey, "tree_key", return_value="private-tree"),
              patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=0)) as spawn,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(detached_runs.main(["start", "--session-dir", str(run_dir), "--", "private-land"]),
                             0, output.getvalue())
        spawn.assert_called_once()
        self.assertIs(cast(object, spawn.call_args.kwargs["start_new_session"]), True)
        path, = (run_dir / detached_runs.DETACHED_RUNS_DIR).glob("*.json")
        record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual((record["session_dir"], record["worker"], record["staff_id"], record.get("pane_dispatch_id")),
                         (str(run_dir), showrunner.name, showrunner.id, ""))
        write_text_atomic(Path(record["log"]), "Private land completed.\n")
        self.assertIn("sent", detached_runs.deliver(record, detached_runs.CompletedCommand(0)))
        self.assertEqual(self.inputs, [])
        (target, message), = self.notices
        self.assertEqual(target, showrunner.id)
        self.assertIn(f"Detached run: {record['run_id']}", message)
        self.assertIn("Private land completed.", message)

    def test_a_pane_workers_run_keeps_its_owner_when_options_name_another_worker(self) -> None:
        row = self.fixture.worker("TEST-command-owner", family="codex")
        other = self.fixture.worker("TEST-impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        self.fixture.world.add(other, "w1:p2", family="codex")
        other_run = self.fixture.root / "other-run"
        other_run.mkdir()
        write_text_atomic(other_run / unit_runs.STATE_FILE, json.dumps({"owner_staff_id": self.fixture.parent.id}))
        # The staff row names the current run.
        _ = staff.set_run(self.fixture.parent.id, staff.OpenRun(self.run_dir))
        runs = self.run_dir / detached_runs.DETACHED_RUNS_DIR
        for requested_run in (self.run_dir, other_run):
            with self.subTest(session_dir=requested_run):
                before = set(runs.glob("*.json"))
                with (patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session}),
                      patch.object(treekey, "tree_key", return_value="private-tree"),
                      patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=0)) as spawn,
                      contextlib.redirect_stdout(io.StringIO()),
                      contextlib.redirect_stderr(io.StringIO()) as errors):
                    code = detached_runs.main(["start", "--session-dir", str(requested_run), "--to", other.name,
                                           "--", "private-check"])
                self.assertEqual(code, 0, errors.getvalue())
                spawn.assert_called_once()
                path, = set(runs.glob("*.json")) - before
                record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                self.assertEqual((record["session_dir"], record["worker"], record["staff_id"]),
                                 (str(self.run_dir), row.name, row.id))
                self.assertEqual(errors.getvalue().splitlines(),
                    [f"detached_runs: --to {other.name} ignored; run belongs to worker {row.name}"])
        self.assertFalse((other_run / detached_runs.DETACHED_RUNS_DIR).exists())

    def test_run_started_for_a_pane_slot_stays_live_and_delivers_to_its_worker(self) -> None:
        _ = self.fixture.worker_run("a-first", "unit", self.fixture.parent.current_session,
                                director_run=True)
        self.fixture.world.add(self.fixture.worker("test", family="codex"), "w1:p3", family="codex")
        cases = (("detached-command-owner", "w1:p1", False, False), ("test2", "w1:p2", False, False),
                 ("implicit-command-owner", "w1:p4", True, False), ("completed-owner", "w1:p5", False, True))
        for name, pane, implicit, completed in cases:
            with self.subTest(name=name, implicit=implicit):
                row = self.fixture.worker(name, family="codex")
                self.fixture.world.add(row, pane, family="codex")
                prompt = self.fixture.root / "assignment.txt"
                write_text_atomic(prompt, "Run the private command.")
                write_text_atomic(self.run_dir / unit_runs.STATE_FILE, json.dumps({
                    "owner_staff_id": self.fixture.parent.id,
                    "history_file": str(self.fixture.root / "history.jsonl"),
                    "open_phases": [{"id": "40", "instance_id": "private-phase", "status": "active"}],
                }))
                dispatch_id = progress_history.begin_dispatch(self.run_dir, slot="test", phase="40", role="test",
                    worker_name=row.name, worker_id=row.id, launcher_command=["private-launcher"], prompt=prompt,
                    kind="test", lens="")
                # The launcher saves the stage before input, so a result has a known stage.
                line = f"Read the unchanged prompt file {json.dumps(str(prompt))}. End your final reply with DONE {dispatch_id}."
                reply = self.fixture.root / "finished-reply.txt"
                progress_history.save_dispatch_delivery(self.run_dir, dispatch_id,
                    pass_recorder.DispatchDelivery(line, dispatch_id, str(reply)))
                runs = self.run_dir / detached_runs.DETACHED_RUNS_DIR
                before = set(runs.glob("*.json"))
                input_count = len(self.inputs)
                output = io.StringIO()
                route = [] if implicit else ["--session-dir", str(self.run_dir), "--to", "test"]
                with (patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session}),
                      patch.object(treekey, "tree_key", return_value="private-tree"),
                      patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=0)),
                      contextlib.redirect_stdout(output)):
                    code = detached_runs.main(["start", *route, "--", "true"])
                self.assertEqual(code, 0, output.getvalue())
                path, = set(runs.glob("*.json")) - before
                record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                self.assertEqual((record["worker"], record["staff_id"]), (row.name, row.id))
                self.assertEqual(record.get("pane_dispatch_id"), dispatch_id)
                record["pid"] = private_supervisor(self, path)
                write_text_atomic(path, json.dumps(record))
                self.assertEqual(detached_runs.live_runs(runs, row.name), [record])
                self.assertEqual(detached_runs.worker_runs(runs, row.name), [record])
                self.assertEqual(detached_runs.live_runs(runs, "test"), [])
                write_text_atomic(Path(record["log"]), "Private command completed.\n")
                self.assertIn("sent", detached_runs.deliver(record, detached_runs.CompletedCommand(0)))
                self.assertEqual(len(self.inputs), input_count + 1)
                self.assertEqual(self.inputs[-1][0], pane)
                self.assertIn(f"Detached run: {record['run_id']}", self.inputs[-1][1])
                self.assertEqual(self.notices, [])
                active = progress_history.active_dispatch(row.id, self.run_dir)
                self.assertIsInstance(active, progress_history.ActiveDispatch)
                assert isinstance(active, progress_history.ActiveDispatch)
                self.assertEqual(active.instance_id, dispatch_id)
                if completed:
                    agent = test_worker.DispatchAgent(self.fixture)
                    agent.pane_commands(self)
                    agent.append(pane, "user", line)
                    agent.append(pane, "assistant", "The command is complete.\nDONE " + dispatch_id)
                    self.fixture.world.locations[pane]["agent_status"] = "done"
                    with patch.object(subprocess, "run", side_effect=agent.run), patch.dict(os.environ, {"BUILDLOG_SCOPE": "0"}):
                        self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
                    self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.NoActiveDispatch)
                    self.assertTrue(reply.read_text().endswith(f"DONE {dispatch_id}\n"))
                    self.assertEqual(len(agent.notices), 1)
                    self.assertIn("phase 40: done", agent.notices[0])
                    self.assertIn(f"; pass {dispatch_id}", agent.notices[0])
                    self.assertNotIn(pane, self.fixture.world.locations)

    def begin_pane_pass(self, row: staff.StaffRow) -> tuple[str, Path]:
        prompt = self.fixture.root / "assignment.txt"
        reply = self.fixture.root / "reply.txt"
        write_text_atomic(prompt, "Run the private command.")
        state = self.run_dir / unit_runs.STATE_FILE
        write_text_atomic(state, json.dumps({
            "owner_staff_id": self.fixture.parent.id,
            "history_file": str(self.fixture.root / "history.jsonl"),
            "open_phases": [{"id": "40", "instance_id": "private-phase", "status": "active"}],
        }))
        dispatch = progress_history.begin_dispatch(self.run_dir, slot=row.name, phase="40", role="impl",
            worker_name=row.name, worker_id=row.id, launcher_command=["private-launcher"], prompt=prompt,
            kind="impl", lens="")
        progress_history.save_dispatch_delivery(self.run_dir, dispatch,
            pass_recorder.DispatchDelivery(str(prompt), dispatch, str(reply)))
        return dispatch, reply

    def test_result_from_an_earlier_pass_does_not_reach_the_holder(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        self.assertNotEqual(dispatch, "earlier-pass")
        for state in ("closed", "blocked"):
            with self.subTest(state=state):
                if state == "blocked":
                    self.fixture.world.add(row, "w1:p1", family="codex")
                    self.fixture.world.locations["w1:p1"]["agent_status"] = state
                record = self.record(row, state)
                record.update(status="finished", pane_dispatch_id="earlier-pass")
                path = Path(record["log"]).with_suffix(".json")
                write_text_atomic(path, json.dumps(record))

                self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(0)),
                                 detached_runs.OTHER_PASS_DELIVERY)
                self.assertEqual(worker_lifecycle.read_json_object(path).get("delivery"),
                                 detached_runs.OTHER_PASS_DELIVERY)
                self.assertEqual(self.inputs, [])
                self.assertEqual(self.notices, [])

    def test_unconfirmed_result_from_an_earlier_pass_alerts_once(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        original, _ = self.begin_pane_pass(row)
        progress_history.complete_dispatch(self.run_dir, row.name, original)
        current, _ = self.begin_pane_pass(row)
        self.assertNotEqual(current, original)

        for name, prefix in (("worker", detached_runs.UNCONFIRMED_WORKER_RESULT),
                             ("push", detached_runs.UNCONFIRMED_RESULT_PUSH)):
            with self.subTest(name=name):
                pending = prefix + "the result has no receipt"
                record = self.record(row, f"earlier-unconfirmed-{name}")
                record.update(status="finished", pid=DEAD_PID, exit_code=7, pane_dispatch_id=original,
                              delivery=pending)
                path = Path(record["log"]).with_suffix(".json")
                write_text_atomic(path, json.dumps(record))

                with (patch.object(send, "deliver_worker_text") as resend,
                      patch.object(send, "send", return_value=send.Result("sent", "alert received")) as alert):
                    self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(7)), pending)
                    self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                    self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])

                resend.assert_not_called()
                alert.assert_called_once()
                options = cast(send.Options, alert.call_args.args[0])
                self.assertEqual(options.to, self.fixture.parent.id)
                self.assertIn("Result delivery is unconfirmed.", options.text or "")
                self.assertIn(record["run_id"], options.text or "")
                saved = worker_lifecycle.read_json_object(path)
                self.assertEqual(saved["delivery"], pending)
                self.assertIs(saved["push_alert_attempted"], True)
                self.assertNotIn("delivered_at", saved)

    def test_earlier_self_stop_does_not_block_done_in_a_stopped_pane(self) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        done = f"DONE {dispatch}"
        agent.append(pane, "user", delivery.line)
        agent.append(pane, "assistant", done)
        self.assertTrue(self.fixture.world.stop(row, self.fixture.world))
        self.assertIsInstance(worker_panes.observe_worker(self.fixture.parent.id, row, staff.snapshot()),
                              worker_panes.StoppedWorker)
        record = self.record(row)
        record.update(status="finished", exit_code=143, self_stop=True, stop_pass_requested=True,
                      stop_reason="The check is obsolete.", pane_dispatch_id="earlier-pass",
                      delivery="deferred: worker is stopped", completion_pending=True)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))

        self.assertIs(turn_end.run_finished(row.id), detached_runs.CompletionReplay.HANDLED)
        self.assertEqual(worker_lifecycle.read_json_object(path).get("delivery"), detached_runs.OTHER_PASS_DELIVERY)
        self.assertEqual(reply.read_text().rstrip(), done)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
        self.assertEqual(self.inputs, [])

    def test_linked_result_defers_when_the_pass_read_fails(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        record = self.record(row)
        record.update(status="finished", exit_code=0, pane_dispatch_id=dispatch)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))

        passed = worker_panes.worker_pass(row, self.run_dir)
        with patch.object(worker_panes, "worker_pass", side_effect=(passed, OSError("private pass read failure"))):
            delivery = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(delivery, "deferred: worker pass is unavailable: private pass read failure")
        self.assertEqual(saved.get("delivery"), delivery)
        self.assertNotIn("delivered_at", saved)
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.notices, [])

        result = detached_runs.submit_finished_worker_result(self.run_dir, row,
            worker_input=detached_runs.WorkerResultInput.TYPE)
        self.assertIsInstance(result, detached_runs.ConfirmedPaneResult)
        self.assertEqual(len(self.inputs), 1)

    def test_reaped_result_reaches_only_the_pass_that_started_it(self) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        done_before_reap = ("done-before-reap", "done-before-reap-no-link", "done-before-reap-unavailable-link",
                            "done-before-reap-saved-finish", "native_done_before_reap")
        result_after_done = ("result-after-done", "result-after-done-fallback")
        for case in ("earlier-pass", "current-pass", "current-done", "failed-with-done-message",
                     *result_after_done, *done_before_reap):
            with self.subTest(case=case):
                self.inputs.clear()
                row = self.fixture.worker(case, family="codex")
                pane = "w1:p1"
                self.fixture.world.add(row, pane, family="codex")
                if case in result_after_done:
                    self.check_result_after_done(agent, row, pane, fallback=case == "result-after-done-fallback")
                    continue
                original, reply = self.begin_pane_pass(row)
                record = self.record(row, case)
                record.update(pid=DEAD_PID, exit_code=0)
                if case == "done-before-reap-saved-finish":
                    # A saved pass link remains valid after a clock adjustment.
                    record["started_at"] = 0
                if case == "done-before-reap-unavailable-link":
                    reply.unlink(missing_ok=True)
                    record["pane_dispatch_id"] = {"unavailable": "the pass lookup failed"}
                elif case != "done-before-reap-no-link":
                    record["pane_dispatch_id"] = original
                if case == "failed-with-done-message":
                    done_message = self.fixture.root / "done-message.txt"
                    write_text_atomic(done_message, "Start stage two.")
                    record.update(exit_code=9, done_message_file=str(done_message))
                path = Path(record["log"]).with_suffix(".json")
                write_text_atomic(path, json.dumps(record))
                current = original
                if case == "earlier-pass":
                    progress_history.complete_dispatch(self.run_dir, row.name, original)
                    self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
                    current, reply = self.begin_pane_pass(row)
                    self.assertNotEqual(current, original)
                delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
                agent.append(pane, "user", delivery.line)
                done = f"DONE {current}"
                self.fixture.world.locations[pane]["agent_status"] = "idle"
                if case != "current-pass":
                    supervisor = (patch.object(detached_run_process, "supervises", return_value=True)
                                  if case in done_before_reap else contextlib.nullcontext())
                    with supervisor:
                        if case != "native_done_before_reap":
                            agent.append(pane, "assistant", done)
                        if case in done_before_reap:
                            self.assertEqual(detached_runs.live_runs(path.parent, row.name), [record])
                        if case == "done-before-reap-saved-finish":
                            record["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                            write_text_atomic(path, json.dumps(record))
                    self.fixture.world.locations[pane]["agent_status"] = "done"

                with patch.object(detached_run_process, "supervises", return_value=False):
                    if case in done_before_reap:
                        self.assertEqual(detached_runs.live_runs(path.parent, row.name), [])
                        event = turn_end.WorkerEvent(str(row.current_session), reply=done)
                        before = len(self.notices)
                        if case == "native_done_before_reap":
                            previous_reply = reply.read_bytes() if reply.exists() else b""
                            self.assertEqual(self.native_stop(row, done), 0)
                            self.assertEqual(reply.read_bytes() if reply.exists() else b"", previous_reply)
                            agent.append(pane, "assistant", done)
                        else:
                            self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.WAITING_ON_REAP)
                        self.assertFalse(any("waiting on run reply" in text for _, text in self.notices[before:]))
                        self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir),
                                              progress_history.ActiveDispatch)
                    self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [record["run_id"]])
                saved = worker_lifecycle.read_json_object(path)
                if case in ("current-done", "failed-with-done-message", "done-before-reap", "done-before-reap-saved-finish",
                            "native_done_before_reap"):
                    if case == "failed-with-done-message":
                        self.assertEqual(saved["delivery"], "nothing delivered: exit 9 with a done message")
                    else:
                        self.assertEqual(saved["delivery"], detached_runs.COVERED_BY_DONE)
                    self.assertEqual(self.inputs, [])
                    self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
                    saved_reply = reply.read_text()
                    self.assertIn(f"Detached run: {case}\n", saved_reply)
                    self.assertIn("Result: FAIL (exit 9)" if case == "failed-with-done-message"
                                  else "Result: PASS (exit 0)", saved_reply)
                    self.assertTrue(saved_reply.endswith(f"{done}\n"))
                    if case == "done-before-reap-saved-finish":
                        notices = len(self.notices)
                        self.assertIn("phase 40: done", self.notices[-1][1])
                        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                        self.assertEqual(len(self.notices), notices)
                        self.assertEqual(self.inputs, [])
                        self.assertEqual(reply.read_text(), saved_reply)
                    continue
                if case == "done-before-reap-unavailable-link":
                    self.assertEqual(saved["delivery"], "deferred: the result's pass ownership is unknown")
                    self.assertEqual(self.inputs, [])
                    self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
                    self.assertFalse(reply.exists())
                    continue
                if case == "earlier-pass":
                    self.assertEqual(saved["delivery"], detached_runs.OTHER_PASS_DELIVERY)
                    self.assertEqual(self.inputs, [])
                    self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
                    self.assertEqual(self.native_stop(row, done), 0)
                else:
                    self.assertEqual(saved["delivery"], f"sent: worker {row.name} at {pane}")
                    self.assertEqual(len(self.inputs), 1)
                    self.assertIn(f"Detached run: {record['run_id']}", self.inputs[0][1])
                    self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
                    if case == "done-before-reap-no-link":
                        result_body = self.inputs[0][1].removesuffix("\n</system-reminder>")
                        self.assertEqual(result_body.splitlines()[-1], f"End your final reply with this line alone: {done}")
                    else:
                        self.assertNotIn("End your final reply with this line alone:", self.inputs[0][1])
                    agent.append(pane, "assistant", done)
                    self.fixture.world.locations[pane]["agent_status"] = "done"
                    event = turn_end.WorkerEvent(str(row.current_session), reply=done)
                    self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.DONE)
                self.assertEqual(reply.read_text().rstrip(), done)
                self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")

        for order in ("delayed_stop_after_unlinked_result", "same_reply_with_later_time"):
            with self.subTest(order=order):
                self.check_stop_after_unlinked_result(agent, repeat_reply=order == "same_reply_with_later_time")

        for name, uncertainty in (("pane", detached_runs.WORKER_INPUT_CHECKPOINT),
                                  ("holder", detached_runs.UNCONFIRMED_RESULT_PUSH + "no receipt")):
            with self.subTest(alert_before_done=name):
                self.check_reap_after_unconfirmed_alert(agent, name, uncertainty)
            for family in ("claude", "codex"):
                for order in ("unconfirmed_done_before_alert", "unconfirmed_alert_before_done",
                              "unconfirmed_done_older_reply", "unconfirmed_done_torn_transcript"):
                    with self.subTest(order=order, family=family, transport=name):
                        self.check_native_done_with_unconfirmed_result(order, family, uncertainty)

        for case in ("acknowledge_mixed_results_once", "acknowledge_prompt_fallback_once",
                     "acknowledge_releases_unrelated_records"):
            with self.subTest(case=case):
                self.check_acknowledgement_reads_evidence_once(
                    agent, fallback=case == "acknowledge_prompt_fallback_once",
                    check_record_retention=case == "acknowledge_releases_unrelated_records")

    def check_result_after_done(
            self, agent: test_worker.DispatchAgent, row: staff.StaffRow, pane: str, *, fallback: bool) -> None:
        dispatch, reply = self.begin_pane_pass(row)
        stage = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        completion_id = worker_panes.stage_two_completion_id(dispatch)
        progress_history.save_dispatch_delivery(self.run_dir, dispatch, replace(stage, completion_id=completion_id))
        done = f"DONE {completion_id}"
        instruction = f"End your final reply with this line alone: {done}"
        prompt = (f"Read the unchanged prompt file {json.dumps(stage.line)}. {instruction}\n"
                  "A reply that waits on a detached run is not final.") if fallback else stage.line
        agent.append(pane, "user", prompt)
        agent.append(pane, "assistant", done)
        record = self.record(row, row.name)
        record.update(pid=DEAD_PID, exit_code=0, pane_dispatch_id=dispatch,
                      finished_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        self.fixture.world.locations[pane]["agent_status"] = "idle"
        with patch.object(detached_run_process, "supervises", return_value=False):
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [record["run_id"]])
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["delivery"], f"sent: worker {row.name} at {pane}")
        self.assertEqual(len(self.inputs), 1)
        text = self.inputs[0][1]
        self.assertIn(f"Detached run: {record['run_id']}", text)
        result_body = text.removesuffix("\n</system-reminder>")
        self.assertEqual(result_body.splitlines()[-1], instruction)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")

        ordinary = "I read the run result."
        agent.append(pane, "assistant", ordinary)
        self.fixture.world.locations[pane]["agent_status"] = "done"
        if fallback:
            active = progress_history.active_dispatch(row.id, self.run_dir)
            assert isinstance(active, progress_history.ActiveDispatch)
            self.assertIsInstance(worker.resume_receipt(row, active, run=self.run_dir), worker.DispatchAwaitingReply)
        self.assertEqual(self.native_stop(row, ordinary), 0)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")

        final = f"The verification passed.\n{done}"
        agent.append(pane, "assistant", final)
        self.assertEqual(self.native_stop(row, final), 0)
        self.assertEqual(reply.read_text().rstrip(), final)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")

    def check_stop_after_unlinked_result(self, agent: test_worker.DispatchAgent, *, repeat_reply: bool) -> None:
        row = self.fixture.worker("same-reply" if repeat_reply else "delayed-stop", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        instance, reply = self.begin_pane_pass(row)
        delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", delivery.line)
        record = self.record(row, "same-reply-result" if repeat_reply else "unlinked-result")
        started = record["started_at"]
        finished = datetime.fromtimestamp(started + 20, timezone.utc).isoformat(timespec="microseconds")
        record.update(status="finished", exit_code=0, finished_at=finished,
                      delivery=f"sent: worker {row.name} at {pane}")
        write_text_atomic(Path(record["log"]).with_suffix(".json"), json.dumps(record))
        earlier = f"The work is complete.\nDONE {instance}"
        later = earlier if repeat_reply else f"The verification passed.\nDONE {instance}"
        self.fixture.world.locations[pane]["agent_status"] = "done"
        previous = reply.read_bytes()
        inputs = list(self.inputs)
        notices = len(self.notices)
        elapsed = 10

        def transcript_time(zone: timezone) -> datetime:
            return datetime.fromtimestamp(started + elapsed, zone)

        backend = staff.backend(row)
        assert isinstance(backend, staff.CodexBackend)
        transcript = worker_admission.transcript_source(row, backend)
        event = turn_end.WorkerEvent(str(row.current_session), turn_end.EventKind.RUN_FINISHED)
        with patch.object(test_worker, "datetime", SimpleNamespace(now=transcript_time)):
            agent.append(pane, "assistant", earlier)
            elapsed = 20
            agent.append(pane, "user", f"Detached run: {record['run_id']}\nRun finished: private verification")
            with patch.object(time, "time", return_value=started + 30):
                self.assertEqual(self.native_stop(row, earlier), 0)
            self.assertEqual(reply.read_bytes(), previous)
            self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
            self.assertIn(pane, self.fixture.world.locations)
            self.assertFalse(any("phase 40: done" in text for _, text in self.notices[notices:]))
            with patch.object(worker_admission, "transcript_source", side_effect=OSError("transcript unavailable")):
                self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.WAITING_ON_REPLY)
            self.assertEqual(reply.read_bytes(), previous)
            partial = transcript.read_bytes()
            elapsed = 40
            agent.append(pane, "assistant", later)
        close = (patch.object(self.fixture.world, "close_terminal_pane", return_value=False)
                 if repeat_reply else contextlib.nullcontext())
        with close:
            self.assertEqual(self.native_stop(row, later), 0)
        if repeat_reply:
            self.assertEqual(reply.read_text(), later + "\n")
            self.assertIn(pane, self.fixture.world.locations)
            atomic_file.write_bytes_atomic(transcript, partial)
            self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.DONE)
            self.assertNotIn(pane, self.fixture.world.locations)
        self.assertEqual(reply.read_text(), later + "\n")
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
        self.assertEqual(self.inputs, inputs)
        self.assertEqual(sum("phase 40: done" in text for _, text in self.notices[notices:]), 1)

    def check_acknowledgement_reads_evidence_once(self, agent: test_worker.DispatchAgent, *,
                                                 fallback: bool, check_record_retention: bool) -> None:
        name = "ack-retention" if check_record_retention else "ack-fallback" if fallback else "ack-mixed"
        row = self.fixture.worker(name, family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        instance, _ = self.begin_pane_pass(row)
        passed = worker_panes.worker_pass(row, self.run_dir)
        delivery = progress_history.dispatch_delivery(passed)
        line = (f'Read the unchanged prompt file "{delivery.line}". End your final reply with DONE {instance}.'
                if fallback else delivery.line)
        agent.append(pane, "user", line)
        started = time.time()

        def done_time(zone: timezone) -> datetime:
            return datetime.fromtimestamp(started + 10, zone)

        with patch.object(test_worker, "datetime", SimpleNamespace(now=done_time)):
            agent.append(pane, "assistant", f"DONE {instance}")
        records: dict[str, tuple[Path, detached_runs.RunRecord]] = {}
        for kind in ("covered", "uncertain", "uncovered", "unknown", "other-pass"):
            record = self.record(row, f"{name}-{kind}")
            record.update(status="finished", pid=DEAD_PID, exit_code=0, completion_pending=True,
                          started_at=started, delivery="deferred: worker is working")
            if kind != "unknown":
                record["finished_at"] = datetime.fromtimestamp(started + 20, timezone.utc).isoformat(timespec="microseconds")
            if kind != "uncovered":
                record["pane_dispatch_id"] = "earlier-pass" if kind == "other-pass" else instance
            if kind == "uncertain":
                record["delivery"] = detached_runs.WORKER_INPUT_CHECKPOINT
            path = Path(record["log"]).with_suffix(".json")
            with detached_runs.record_lock(path):
                write_text_atomic(path, json.dumps(record))
            records[kind] = path, record

        read_records = worker_admission.transcript_records

        class UnrelatedRecord(dict[str, object]):
            pass

        def records_with_unrelated_entries(
                source: Path) -> Generator[dict[str, object] | worker_admission.TornTranscriptRecord]:
            for entry in read_records(source):
                yield entry
            first = UnrelatedRecord(type="unrelated")
            first_ref = ref(first)
            yield first
            del first
            yield UnrelatedRecord(type="unrelated")
            self.assertIsNone(first_ref())

        read_transcript = records_with_unrelated_entries if check_record_retention else read_records
        with patch.object(worker_admission, "transcript_records", wraps=read_transcript) as read:
            self.assertIs(detached_runs.acknowledge_worker_completion(self.run_dir, row, passed),
                          detached_runs.CompletionReplay.RETRY)
        self.assertEqual(read.call_count, 1)
        for kind, (path, before) in records.items():
            with self.subTest(result=kind):
                expected = dict(before)
                if kind in ("covered", "uncertain"):
                    expected["completion_pending"] = False
                if kind == "covered":
                    expected["delivery"] = detached_runs.COVERED_BY_DONE
                self.assertEqual(worker_lifecycle.read_json_object(path), expected)

    def native_stop(self, row: staff.StaffRow, reply: str) -> int:
        payload: dict[str, object] = {
            "hook_event_name": "Stop", "session_id": str(row.current_session),
            "transcript_path": "unused", "stop_hook_active": False, "last_assistant_message": reply,
        }
        if isinstance(staff.backend(row), staff.CodexBackend):
            payload.update(turn_id="fixture-turn", cwd=str(self.fixture.root), model="sol")
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            return turn_end.main(["--handle-hook"])

    def check_native_done_with_unconfirmed_result(
            self, order: str, family: Literal["claude", "codex"], uncertainty: str) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        target = "holder" if uncertainty.startswith(detached_runs.UNCONFIRMED_RESULT_PUSH) else "pane"
        row = self.fixture.worker(f"{order}-{family}-{target}", family=family)
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family=family)
        dispatch, reply = self.begin_pane_pass(row)
        delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", delivery.line)
        record = self.record(row, dispatch)
        record.update(status="finished", pid=DEAD_PID, exit_code=0, pane_dispatch_id=dispatch,
                      finished_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                      delivery=uncertainty, completion_pending=True)
        path = Path(record["log"]).with_suffix(".json")
        with detached_runs.record_lock(path):
            write_text_atomic(path, json.dumps(record))
        inputs = list(self.inputs)
        notices = len(self.notices)
        if order == "unconfirmed_alert_before_done":
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertIs(worker_lifecycle.read_json_object(path).get("push_alert_attempted"), True)

        done = f"The work is complete.\nDONE {dispatch}"
        self.fixture.world.locations[pane]["agent_status"] = "done"
        backend = staff.backend(row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        transcript = worker_admission.transcript_source(row, backend)
        stale_transcript = order in ("unconfirmed_done_older_reply", "unconfirmed_done_torn_transcript")
        if stale_transcript:
            agent.append(pane, "user", f"Detached run: {record['run_id']}\nRun finished: private verification")
            agent.append(pane, "assistant", "I am checking the result.")
            clean = transcript.read_bytes()
            if order == "unconfirmed_done_torn_transcript":
                write_text_atomic(transcript, clean.decode() + '{"type":')
            before_stop = transcript.read_bytes()
            self.assertEqual(self.native_stop(row, done), 0)
            self.assertEqual(transcript.read_bytes(), before_stop)
            self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
            self.assertIs(worker_lifecycle.read_json_object(path)["completion_pending"], True)
            self.assertEqual(self.inputs, inputs)
            write_text_atomic(transcript, clean.decode())
        agent.append(pane, "assistant", done)
        before_stop = transcript.read_bytes()
        self.assertEqual(self.native_stop(row, done), 0)
        self.assertEqual(transcript.read_bytes(), before_stop)

        self.assertEqual(reply.read_text().rstrip(), done)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
        saved = worker_lifecycle.read_json_object(path)
        self.assertIs(saved["completion_pending"], False)
        self.assertEqual(saved["delivery"], uncertainty)
        self.assertNotIn("delivered_at", saved)
        self.assertEqual(self.inputs, inputs)
        if order != "unconfirmed_alert_before_done":
            self.assertNotIn("push_alert_attempted", saved)
        if order == "unconfirmed_done_before_alert":
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        if not stale_transcript:
            self.assertIs(worker_lifecycle.read_json_object(path).get("push_alert_attempted"), True)
        self.assertEqual(len(self.notices[notices:]), 1 if stale_transcript else 2, self.notices[notices:])
        self.assertEqual(sum("phase 40: done" in text for _, text in self.notices[notices:]), 1)
        self.assertEqual(sum("Result delivery is unconfirmed." in text for _, text in self.notices[notices:]),
                         0 if stale_transcript else 1)
        self.assertEqual(self.inputs, inputs)

    def check_reap_after_unconfirmed_alert(self, agent: test_worker.DispatchAgent, name: str, uncertainty: str) -> None:
        row = self.fixture.worker(f"unconfirmed-{name}", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", delivery.line)
        record = self.record(row, f"unconfirmed-{name}")
        record.update(status="finished", pid=DEAD_PID, exit_code=0, pane_dispatch_id=dispatch,
                      finished_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                      delivery=uncertainty, completion_pending=True)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        inputs = list(self.inputs)

        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        saved = worker_lifecycle.read_json_object(path)
        self.assertIs(saved["push_alert_attempted"], True)
        self.assertIs(saved["completion_pending"], True)
        self.assertEqual(saved["delivery"], uncertainty)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
        self.assertEqual(self.inputs, inputs)

        done = f"DONE {dispatch}"
        agent.append(pane, "assistant", done)
        self.fixture.world.locations[pane]["agent_status"] = "done"
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
        self.assertEqual(reply.read_text().rstrip(), done)
        saved = worker_lifecycle.read_json_object(path)
        self.assertIs(saved["completion_pending"], False)
        self.assertEqual(saved["delivery"], uncertainty)
        self.assertNotIn("delivered_at", saved)
        self.assertEqual(self.inputs, inputs)
        alerts = [text for _, text in self.notices if f"Detached run {record['run_id']}. Result delivery is unconfirmed." in text]
        self.assertEqual(len(alerts), 1)

    def test_hook_recovers_done_after_a_result_from_an_earlier_pass(self) -> None:
        for delivery in ("confirmed", "running marker", "quoted in another dispatch", "later ordinary reply"):
            with self.subTest(delivery=delivery):
                case = PaneRunDeliveryTests("runTest")
                case.setUp()
                try:
                    case.check_earlier_result_reply(delivery)
                finally:
                    _ = case.doCleanups()

        for family in ("claude", "codex"):
            for pending in (True, False, None):
                for order in ("earlier_unconfirmed_then_later_done", "earlier_unconfirmed_then_later_result"):
                    with self.subTest(family=family, pending=pending, order=order):
                        self.check_earlier_uncertainty_during_native_stop(family, pending, order)

    def test_earlier_pass_receipt_cannot_prove_a_duplicate_result_reply(self) -> None:
        self.check_earlier_result_reply("later duplicate reply")

    def check_earlier_result_reply(self, result_case: str) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        confirmed = result_case in ("confirmed", "later ordinary reply", "later duplicate reply")
        original, _ = self.begin_pane_pass(row)
        record = self.record(row, "earlier-result")
        record["pane_dispatch_id"] = original
        if result_case != "running marker":
            record.update(status="finished", exit_code=0, finished_at=datetime.now(timezone.utc).isoformat())
        write_text_atomic(Path(record["log"]).with_suffix(".json"), json.dumps(record))
        dispatch = ""
        reply = self.fixture.root / "reply.txt"
        done = ""
        later = "The earlier check passed."

        def start_next_pass() -> None:
            nonlocal dispatch, reply, done
            dispatch, reply = self.begin_pane_pass(row)
            done = f"DONE {dispatch}"
            delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
            prompt = (f"End your final reply with DONE {dispatch}."
                      if result_case == "quoted in another dispatch" else delivery.line)
            agent.append(pane, "user", prompt)
            agent.append(pane, "assistant", done)

        if confirmed:
            send_input = self.fixture.world.run

            def submit(*arguments: str, deadline: float = math.inf) -> str:
                if arguments[:3] == ("agent", "prompt", pane):
                    start_next_pass()
                return send_input(*arguments, deadline=deadline)

            with patch.object(self.fixture.world, "run", new=submit):
                self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(0)),
                                 f"sent: worker {row.name} at {pane}")
            self.assertEqual(len(self.inputs), 1)
            self.inputs.clear()
        else:
            start_next_pass()
            message = "Detached run: earlier-result"
            if result_case != "running marker":
                message += "\n" + detached_runs.result_message(record, detached_runs.CompletedCommand(0))
                record["delivery"] = f"sent: worker {row.name} at {pane}"
                write_text_atomic(Path(record["log"]).with_suffix(".json"), json.dumps(record))
            if result_case == "quoted in another dispatch":
                message = 'Read the unchanged prompt file "other.txt". End your final reply with DONE other.\n' + message
            agent.append(pane, "user", message)
        agent.append(pane, "assistant", later)
        if result_case == "later ordinary reply":
            agent.append(pane, "user", "Check another file.")
            later = "I checked the file."
            agent.append(pane, "assistant", later)
        if result_case == "later duplicate reply":
            agent.append(pane, "user", "Detached run: earlier-result\n" + detached_runs.result_message(
                record, detached_runs.CompletedCommand(0)))
            later = "I replied to the duplicate."
            agent.append(pane, "assistant", later)
        self.fixture.world.locations[pane]["agent_status"] = "done"

        if result_case in ("running marker", "quoted in another dispatch", "later duplicate reply"):
            self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(row.current_session), reply=later)),
                             turn_end.TurnOutcome.IGNORED)
            self.assertFalse(reply.exists())
            self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
            return
        self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(row.current_session), reply=later)),
                         turn_end.TurnOutcome.DONE)
        self.assertEqual(reply.read_text().rstrip(), done)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
        self.assertEqual(self.inputs, [])

    def test_a_result_reply_requires_a_receipt_for_its_transcript_occurrence(self) -> None:
        for cause in ("earlier identical input", "ordinary user message", "later duplicate", "changed session",
                      "changed prefix", "interrupted receipt save"):
            with self.subTest(cause=cause):
                case = PaneRunDeliveryTests("runTest")
                case.setUp()
                try:
                    case.check_result_occurrence(cause)
                finally:
                    _ = case.doCleanups()

    def test_done_recovery_rejects_a_receipt_redirected_to_partial_result_input(self) -> None:
        self.check_result_occurrence("partial result input")

    def test_metadata_result_does_not_count_as_delivery(self) -> None:
        row = self.fixture.worker("metadata-result", family="claude")
        pane = "w1:p1"
        self.fixture.world.add(row, pane)
        record = self.record(row, "metadata-result")
        record.update(status="finished", exit_code=0, pane_dispatch_id="")
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        source = worker_admission.transcript_source(row, staff.ClaudeBackend())
        message = script_prompt(f"Detached run: {record['run_id']}\nThe command passed.")
        entry = {"type": "user", "isMeta": True, "message": {"role": "user", "content": message}}
        with source.open("a", encoding="utf-8") as stream:
            _ = stream.write(json.dumps(entry) + "\n")

        self.fixture.world.locations[pane]["agent_status"] = "working"
        result = detached_runs.CompletedCommand(0)
        self.assertEqual(detached_runs.deliver(record, result),
                         detached_runs.DEFERRED_WORKER_RESULT + "worker is working")
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("delivery"), detached_runs.DEFERRED_WORKER_RESULT + "worker is working")
        self.assertNotIn("delivered_at", saved)
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.notices, [])

        self.fixture.world.locations[pane]["agent_status"] = "idle"
        self.assertEqual(detached_runs.deliver(record, result), f"sent: worker {row.name} at {pane}")
        self.assertEqual(len(self.inputs), 1)
        saved = worker_lifecycle.read_json_object(path)
        evidence = detached_runs.read_worker_result_receipt(saved)
        assert isinstance(evidence, detached_runs.WorkerResultReceipt)
        self.assertTrue(send.worker_result_input_matches(row, staff.snapshot(), record["run_id"],
                                                          evidence.cursor, evidence.input_digest))
        write_text_atomic(source, source.read_text().replace(json.dumps(entry) + "\n", "", 1))
        self.assertFalse(send.worker_result_input_matches(row, staff.snapshot(), record["run_id"],
                                                           evidence.cursor, evidence.input_digest))

    def test_result_delivery_waits_for_the_worker_stage(self) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker("stage-pending", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        stage = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        progress_history.save_dispatch_delivery(self.run_dir, dispatch, replace(stage, completion_id=""))
        agent.append(pane, "user", stage.line)
        record = self.record(row, "stage-pending")
        record.update(status="finished", exit_code=0, pane_dispatch_id=dispatch,
                      finished_at=datetime.now(timezone.utc).isoformat())
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(0)),
                         detached_runs.DEFERRED_WORKER_RESULT + "the worker stage is unknown")
        self.assertEqual(self.inputs, [])
        self.assertNotIn("worker_result_receipt", worker_lifecycle.read_json_object(path))
        progress_history.save_dispatch_delivery(self.run_dir, dispatch, stage)
        self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(0)),
                         f"sent: worker {row.name} at {pane}")

    def test_holder_delivery_waits_for_the_worker_stage(self) -> None:
        for state in ("stopped", "closed"):
            for drops_during_selection in (False, True):
                with self.subTest(state=state, drops_during_selection=drops_during_selection):
                    case = PaneRunDeliveryTests("runTest")
                    case.setUp()
                    try:
                        case.check_holder_delivery_stage(state, drops_during_selection=drops_during_selection)
                    finally:
                        _ = case.doCleanups()

    def prepare_holder_pass(self, state: str) -> tuple[staff.StaffRow, pass_recorder.DispatchDelivery,
                                                       detached_runs.RunRecord, Path]:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker("holder-stage", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        stage = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", stage.line)
        if state == "stopped":
            _ = self.fixture.world.stop(row, self.fixture.world)
        else:
            self.fixture.world.remove_pane(pane)
        record = self.record(row, "holder-stage")
        record.update(status="finished", exit_code=0, pane_dispatch_id=dispatch,
                      finished_at=datetime.now(timezone.utc).isoformat())
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        return row, stage, record, path

    def check_holder_delivery_stage(self, state: str, *, drops_during_selection: bool) -> None:
        _row, stage, record, path = self.prepare_holder_pass(state)
        dispatch = stage.completion_id
        observe = worker_panes.observe

        def drop_stage() -> None:
            progress_history.save_dispatch_delivery(self.run_dir, dispatch, replace(stage, completion_id=""))

        def observe_and_drop(parent: staff.StaffId, membership: staff.StaffSnapshot) -> list[worker_panes.WorkerPane]:
            drop_stage()
            return observe(parent, membership)

        result = detached_runs.CompletedCommand(0)
        if drops_during_selection:
            with patch.object(worker_panes, "observe", new=observe_and_drop):
                status = detached_runs.deliver(record, result)
            expected = "the worker stage changed"
        else:
            drop_stage()
            status = detached_runs.deliver(record, result)
            expected = "the worker stage is unknown"
        self.assertEqual(status, detached_runs.DEFERRED_WORKER_RESULT + expected)
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.notices, [])
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("delivery"), status)
        self.assertNotIn("worker_result_receipt", saved)
        self.assertNotIn("delivered_at", saved)
        progress_history.save_dispatch_delivery(self.run_dir, dispatch, stage)
        self.assert_holder_result(record, "worker is stopped" if state == "stopped" else "worker pane is closed")

    def test_holder_delivery_keeps_the_worker_lock_through_the_state_write(self) -> None:
        row, _stage, record, path = self.prepare_holder_pass("closed")
        checks: list[str] = []

        def check_lock(label: str) -> None:
            with self.assertRaises(BlockingIOError), worker_panes.worker_id_lock(row.id):
                pass
            checks.append(label)

        def during_transport(options: send.Options, *, membership: staff.StaffSnapshot) -> send.Result:
            check_lock("transport")
            return self.holder_notice(options, membership=membership)

        def during_save(target: Path, text: str) -> None:
            if target == path and "sent: holder received the result" in text:
                check_lock("delivery save")
            write_text_atomic(target, text)

        with (patch.object(send, "_send", new=during_transport),
              patch.object(detached_runs, "write_text_atomic", new=during_save)):
            self.assert_holder_result(record, "worker pane is closed")
        self.assertEqual(checks, ["transport", "delivery save"])
        with worker_panes.worker_id_lock(row.id):
            pass

    def test_fixed_ordinary_worker_message_propagates_herdr_construction_error(self) -> None:
        row = self.fixture.worker("fixed-message", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        membership = staff.snapshot()
        observed = worker_panes.observe(self.fixture.parent.id, membership)
        fixed = send.FixedStaffLookup(row.id, "worker")
        with (patch.object(worker_panes, "observe", return_value=observed),
              patch.object(herdr, "Herdr", side_effect=OSError("Herdr construction failed"))):
            with self.assertRaisesRegex(OSError, "Herdr construction failed"):
                _ = send.deliver_worker_text(row, membership, "Continue.", sender="fixture", timeout=1, selection=fixed)
            result = send.deliver_worker_text(row, membership, "Continue.", sender="fixture", timeout=1)
            self.assertEqual(result, send.Result("sent", "holder received the result"))
            result = send.deliver_worker_text(row, membership, "Continue.", sender="fixture", timeout=1,
                                             selection=fixed, receipt=send.WorkerResultSubmission())
            self.assertIsInstance(result, send.WorkerResultNotSubmitted)
        self.assertEqual(self.inputs, [])

    def check_result_occurrence(self, cause: str) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker("receipt", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", delivery.line)
        record = self.record(row, "receipt-result")
        record.update(status="finished", exit_code=0, pane_dispatch_id=dispatch,
                      finished_at=datetime.now(timezone.utc).isoformat(), delivery="deferred: worker is working")
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        result = detached_runs.CompletedCommand(0)
        message = f"Detached run: {record['run_id']}\n" + detached_runs.result_message(record, result)
        done = f"DONE {dispatch}"
        later = "The check passed."
        send_input = self.fixture.world.run

        def accept_input(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:3] == ("agent", "prompt", pane):
                if cause == "earlier identical input":
                    return ""
                agent.append(pane, "assistant", done)
            return send_input(*arguments, deadline=deadline)

        def miss_earlier_input(*_args: object) -> bool:
            agent.append(pane, "user", message)
            return False

        def interrupt_save(target: Path, text: str) -> None:
            if target == path and f"sent: worker {row.name} at {pane}" in text:
                raise SystemExit("interrupted receipt save")
            write_text_atomic(target, text)

        def plain_prompt(_pane: str, text: str) -> str:
            return text

        def plain_envelope(text: str) -> str:
            return text

        if cause == "ordinary user message":
            agent.append(pane, "assistant", done)
            agent.append(pane, "user", message)
            self.assertEqual(detached_runs.deliver(record, result), f"already delivered: {record['run_id']}")
            self.assertEqual(self.inputs, [])
        else:
            # Use plain input so this probe exercises the occurrence checks in both versions.
            with (contextlib.ExitStack() as stack, patch.object(self.fixture.world, "run", new=accept_input),
                  patch.object(herdr, "_prepare_prompt", new=plain_prompt),
                  patch("scripts.lib.script_envelope.script_prompt", new=plain_envelope)):
                if cause == "earlier identical input":
                    _ = stack.enter_context(patch.object(detached_runs, "_result_received", new=miss_earlier_input))
                    self.assertTrue(detached_runs.deliver(record, result).startswith("unconfirmed: "))
                    self.assertNotIn("worker_result_receipt", worker_lifecycle.read_json_object(path))
                    self.assertEqual(self.inputs, [])
                    return
                if cause == "interrupted receipt save":
                    with (patch.object(detached_runs, "write_text_atomic", new=interrupt_save),
                          self.assertRaisesRegex(SystemExit, "interrupted receipt save")):
                        _ = detached_runs.deliver(record, result)
                    self.assertEqual(detached_runs.deliver(record, result), f"already delivered: {record['run_id']}")
                    self.assertNotIn("worker_result_receipt", worker_lifecycle.read_json_object(path))
                else:
                    self.assertEqual(detached_runs.deliver(record, result), f"sent: worker {row.name} at {pane}")
            self.assertEqual(len(self.inputs), 1)

        agent.append(pane, "assistant", later)
        backend = staff.backend(row)
        assert isinstance(backend, staff.CodexBackend)
        source = worker_admission.transcript_source(row, backend)
        if cause == "partial result input":
            agent.append(pane, "user", f"Detached run: {record['run_id']}\nPartial message.")
            _, cursor = list(worker_panes.transcript_record_cursors(
                worker_admission.transcript_records(source), worker_panes.transcript_stream(row, source)))[-1]
            saved = worker_lifecycle.read_json_object(path)
            fields = cast(dict[str, object], saved["worker_result_receipt"])
            if "cursor" in fields:
                fields["cursor"] = {"stream": cursor.stream, "position": cursor.position, "prefix": cursor.prefix}
            write_text_atomic(path, json.dumps(saved))
            later = "I read the partial input."
            agent.append(pane, "assistant", later)
        elif cause == "later duplicate":
            agent.append(pane, "user", message)
            later = "This is a reply to the duplicate."
            agent.append(pane, "assistant", later)
        elif cause == "changed prefix":
            first, separator, rest = source.read_text().partition("\n")
            prefix = cast(dict[str, object], json.loads(first))
            prefix["fixture_rewrite"] = True
            write_text_atomic(source, json.dumps(prefix) + separator + rest)
        elif cause == "changed session":
            transcript = source.read_text()
            self.fixture.world.remove_pane(pane)
            row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("receipt-moved")))
            self.fixture.world.add(row, pane, family="codex")
            write_text_atomic(worker_admission.transcript_source(row, backend), transcript)
        self.fixture.world.locations[pane]["agent_status"] = "done"
        self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(row.current_session), reply=later)),
                         turn_end.TurnOutcome.IGNORED)
        self.assertFalse(reply.exists())
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")

    def test_definite_transport_refusal_cannot_confirm_unrelated_input(self) -> None:
        for refusal in (herdr.HerdrCommandNotStarted, herdr.AgentBlocked):
            with self.subTest(refusal=refusal.__name__):
                case = PaneRunDeliveryTests("runTest")
                case.setUp()
                try:
                    case.check_refused_result_input(refusal)
                finally:
                    _ = case.doCleanups()

    def check_refused_result_input(self, refusal: type[herdr_types.HerdrFailure]) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker("refused-receipt", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        record = self.record(row)
        record.update(status="finished", exit_code=0, pane_dispatch_id="")
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        run_command = self.fixture.world.run

        def deny_input(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:3] == ("agent", "prompt", pane):
                agent.append(pane, "user", arguments[3])
                raise refusal("the transport refused input")
            return run_command(*arguments, deadline=deadline)

        with patch.object(self.fixture.world, "run", new=deny_input):
            self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(0)),
                             detached_runs.FAILED_WORKER_SUBMISSION + "the transport refused input")
        self.assertEqual(self.inputs, [])
        self.assertNotIn("worker_result_receipt", worker_lifecycle.read_json_object(path))

    def test_ambiguous_transport_requires_full_result_input(self) -> None:
        for arrival in ("full", "partial", "absent"):
            with self.subTest(arrival=arrival):
                case = PaneRunDeliveryTests("runTest")
                case.setUp()
                try:
                    case.check_ambiguous_result_input(arrival)
                finally:
                    _ = case.doCleanups()

    def check_ambiguous_result_input(self, arrival: str) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker("ambiguous-receipt", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        record = self.record(row)
        record.update(status="finished", exit_code=0, pane_dispatch_id="")
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        result = detached_runs.CompletedCommand(0)
        run_command = self.fixture.world.run
        attempts: list[str] = []

        def fail_after_launch(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:3] != ("agent", "prompt", pane):
                return run_command(*arguments, deadline=deadline)
            attempts.append(arguments[3])
            if arrival == "full":
                _ = run_command(*arguments, deadline=deadline)
            elif arrival == "partial":
                agent.append(pane, "user", "\n".join(arguments[3].splitlines()[:2]))
            # An I/O error can identify the executable even after launch.
            raise OSError(5, "transport I/O failed", self.fixture.world.binary)

        with patch.object(self.fixture.world, "run", new=fail_after_launch):
            delivery = detached_runs.deliver(record, result)
            if arrival == "full":
                self.assertEqual(delivery, f"sent: worker {row.name} at {pane}")
                self.assertIn("worker_result_receipt", worker_lifecycle.read_json_object(path))
                self.assertEqual(len(self.inputs), 1)
            else:
                self.assertTrue(delivery.startswith(detached_runs.UNCONFIRMED_WORKER_RESULT), delivery)
                self.assertEqual(detached_runs.deliver(record, result), delivery)
                self.assertNotIn("worker_result_receipt", worker_lifecycle.read_json_object(path))
            self.assertEqual(len(attempts), 1)

    def test_supervisor_keeps_the_worker_lock_through_the_receipt_save(self) -> None:
        for step in ("before check", "before boundary", "during transport", "during confirmation", "during save"):
            with self.subTest(step=step):
                case = PaneRunDeliveryTests("runTest")
                case.setUp()
                try:
                    case.check_result_stage_lock(step)
                finally:
                    _ = case.doCleanups()

    def check_result_stage_lock(self, step: str) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker("stage-lock", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        stage = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", stage.line)
        record = self.record(row)
        record.update(status="finished", exit_code=0, pane_dispatch_id=dispatch,
                      finished_at=datetime.now(timezone.utc).isoformat())
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        replacement = replace(stage, completion_id=f"{dispatch}-second-stage")
        changed: list[bool] = []

        def advance_stage() -> None:
            try:
                with worker_panes.worker_id_lock(row.id):
                    progress_history.save_dispatch_delivery(self.run_dir, dispatch, replacement)
            except BlockingIOError:
                changed.append(False)
            else:
                changed.append(True)

        transcript_records = worker_admission.transcript_records
        transport = self.fixture.world.run
        readiness = self.fixture.world.readiness

        def before_check(target: str) -> herdr.WorkerReadiness:
            advance_stage()
            return readiness(target)

        reads = 0

        def read_transcript(source: Path, *, offset: int = 0,
                            boundary: worker_admission.TranscriptRecordBoundary =
                                worker_admission.TranscriptRecordBoundary.ALL_RECORDS,
                            ) -> Generator[dict[str, object] | worker_admission.TornTranscriptRecord]:
            nonlocal reads
            if boundary is worker_admission.TranscriptRecordBoundary.COMPLETE_LINES_WITH_TAIL:
                reads += 1
                if (step == "before boundary" and reads == 1) or (step == "during confirmation" and reads == 2):
                    advance_stage()
            yield from transcript_records(source, offset=offset, boundary=boundary)

        def during_transport(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:3] == ("agent", "prompt", pane):
                advance_stage()
            return transport(*arguments, deadline=deadline)

        def during_save(target: Path, text: str) -> None:
            if target == path and "worker_result_receipt" in text:
                advance_stage()
            write_text_atomic(target, text)

        with contextlib.ExitStack() as stack:
            if step == "before check":
                _ = stack.enter_context(patch.object(self.fixture.world, "readiness", new=before_check))
            elif step in ("before boundary", "during confirmation"):
                _ = stack.enter_context(patch.object(worker_admission, "transcript_records", new=read_transcript))
            elif step == "during transport":
                _ = stack.enter_context(patch.object(self.fixture.world, "run", new=during_transport))
            else:
                _ = stack.enter_context(patch.object(detached_runs, "write_text_atomic", new=during_save))
            result = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
        saved = worker_lifecycle.read_json_object(path)
        if step == "before check":
            self.assertEqual(result, detached_runs.DEFERRED_WORKER_RESULT + "the worker stage changed")
            self.assertEqual(changed, [True])
            self.assertEqual(self.inputs, [])
            self.assertNotIn("worker_result_receipt", saved)
            return
        self.assertEqual(result, f"sent: worker {row.name} at {pane}")
        self.assertEqual(changed, [False])
        receipt = detached_runs.read_worker_result_receipt(saved)
        assert isinstance(receipt, detached_runs.WorkerResultReceipt)
        self.assertEqual(receipt.assignment, detached_runs.ResultSubmissionPass(dispatch, stage.completion_id))
        self.assertEqual(len(self.inputs), 1)

    def check_earlier_uncertainty_during_native_stop(
            self, family: Literal["claude", "codex"], pending: bool | None, order: str) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker(f"{order}-{family}-{pending}", family=family)
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family=family)
        original, _ = self.begin_pane_pass(row)
        uncertainty = (detached_runs.WORKER_INPUT_CHECKPOINT if family == "claude"
                       else detached_runs.UNCONFIRMED_RESULT_PUSH + "no receipt")
        earlier = self.record(row, f"{original}-a")
        earlier.update(status="finished", pid=DEAD_PID, exit_code=0, pane_dispatch_id=original,
                       finished_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"), delivery=uncertainty)
        if pending is not None:
            earlier["completion_pending"] = pending
        earlier_path = Path(earlier["log"]).with_suffix(".json")
        with detached_runs.record_lock(earlier_path):
            write_text_atomic(earlier_path, json.dumps(earlier))
        progress_history.complete_dispatch(self.run_dir, row.name, original)
        dispatch, reply = self.begin_pane_pass(row)
        delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", delivery.line)
        inputs = len(self.inputs)
        notices = len(self.notices)

        later_id = f"{original}-b"
        later_path = earlier_path.with_name(later_id + ".json")
        if order == "earlier_unconfirmed_then_later_result":
            later = self.record(row, later_id)
            later.update(status="finished", pid=DEAD_PID, exit_code=0, pane_dispatch_id=dispatch,
                         finished_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                         delivery="deferred: worker is working", completion_pending=True)
            with detached_runs.record_lock(later_path):
                write_text_atomic(later_path, json.dumps(later))
            continued = "I am waiting for the result."
            agent.append(pane, "assistant", continued)
            self.fixture.world.locations[pane]["agent_status"] = "done"
            self.assertEqual(self.native_stop(row, continued), 0)
            self.assertEqual(len(self.inputs), inputs + 1)
            (target, text), = self.inputs[inputs:]
            self.assertEqual(target, pane)
            self.assertIn(f"Detached run: {later['run_id']}", text)
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertEqual(len(self.inputs), inputs + 1)
            self.assertIs(worker_lifecycle.read_json_object(later_path).get("completion_pending"), True)

        done = f"DONE {dispatch}"
        agent.append(pane, "assistant", done)
        self.fixture.world.locations[pane]["agent_status"] = "done"
        if order == "earlier_unconfirmed_then_later_done" and pending is True:
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
        self.assertEqual(self.native_stop(row, done), 0)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
        self.assertEqual(reply.read_text().rstrip(), done)
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        if order == "earlier_unconfirmed_then_later_result":
            self.assertIs(worker_lifecycle.read_json_object(later_path).get("completion_pending"), False)
        saved = worker_lifecycle.read_json_object(earlier_path)
        self.assertEqual(saved["delivery"], uncertainty)
        self.assertNotIn("delivered_at", saved)
        if pending is None:
            self.assertNotIn("completion_pending", saved)
        else:
            self.assertIs(saved["completion_pending"], pending)
        self.assertEqual(len(self.inputs), inputs + int(order == "earlier_unconfirmed_then_later_result"))
        waiting = int(order == "earlier_unconfirmed_then_later_result")
        self.assertEqual(len(self.notices[notices:]), 2 + waiting)
        self.assertEqual(sum("waiting on run reply" in text for _, text in self.notices[notices:]), waiting)
        self.assertEqual(sum("phase 40: done" in text for _, text in self.notices[notices:]), 1)
        self.assertEqual(sum("Result delivery is unconfirmed." in text for _, text in self.notices[notices:]), 1)

    def test_a_worker_stop_delivers_the_result_and_keeps_its_pass(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        active = progress_history.active_dispatch(row.id, self.run_dir)
        self.assertIsInstance(active, progress_history.ActiveDispatch)
        record = self.record(row)
        record.update(pid=DEAD_PID, command_group=DEAD_PID, command_started="private-start",
                      pane_dispatch_id=dispatch)
        record["worker"] = "former-worker-name"
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        reason = "The newer check replaces this run."
        pid, _, arguments = self.fixture.world.foreground[pane]

        def caller_arguments(current: int) -> list[str]:
            return arguments if current == pid else ["python"]

        with (patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session, "CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(pane_record, "process_parent", return_value=pid),
              patch.object(pane_record, "process_arguments", side_effect=caller_arguments),
              patch.object(detached_run_process, "supervisor_match", return_value=True),
              patch.object(process_start, "process_start", return_value=process_start.Started("private-start")),
              patch.object(detached_runs, "_signal_stopped_command") as signal_group,
              contextlib.redirect_stdout(io.StringIO())):
            self.assertTrue(self.record_confirmed_group_stop(
                path, reason, self_stop=True, signal_group=signal_group))
            signal_group.assert_called_once_with(detached_runs.CommandGroup(DEAD_PID, "private-start"), signal.SIGTERM)
        stopped = worker_lifecycle.read_json_object(path)
        self.assertEqual(stopped.get("stop_reason"), reason)
        self.assertIs(stopped.get("self_stop"), True)
        self.assertIs(stopped.get("stop_pass_requested"), True)
        self.assertTrue(path.with_suffix(detached_runs.PENDING_STOP_SUFFIX).exists())
        group_live = True

        def group_is_live(_group: int, **_options: object) -> bool:
            return group_live

        def finish_group(_seconds: float) -> None:
            nonlocal group_live
            running = worker_lifecycle.read_json_object(path)
            self.assertEqual((running.get("status"), running.get("exit_code")), ("running", 143))
            with contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(detached_runs.main(["deliver", str(path)]), 1)
            self.assertIn("the command group has no confirmed end", errors.getvalue())
            self.assertEqual(self.inputs, [])
            self.assertEqual(self.notices, [])
            group_live = False

        with (patch.object(detached_runs, "_run", return_value=143),
              patch.object(detached_runs, "_group_is_live", side_effect=group_is_live),
              patch.object(time, "sleep", side_effect=finish_group) as wait_for_group,
              patch.object(detached_runs, "scope_prefix", return_value=[])):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        wait_for_group.assert_called_once()
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual((saved.get("status"), saved.get("exit_code")), ("finished", 143))
        self.assertFalse(path.with_suffix(detached_runs.PENDING_STOP_SUFFIX).exists())
        (target, text), = self.inputs
        self.assertEqual(target, pane)
        self.assertIn("Result: STOPPED (exit 143)", text)
        self.assertIn(f"Stop reason: {reason}", text)
        self.assertEqual(progress_history.active_dispatch(row.id, self.run_dir), active)
        self.assertIn(pane, self.fixture.world.locations)
        self.assertEqual(self.notices, [])
        self.assertFalse(reply.exists())

    def test_unreadable_stop_preserves_the_normal_result_after_empty_slice(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        record = self.record(row)
        cgroups = self.fixture.root / "cgroups"
        scope = cgroups / "builds.slice/builds-detached.slice/builds-detached-emptystop.slice"
        scope.mkdir(parents=True)
        events = scope / "cgroup.events"
        identity: process_stop.RunSliceIdentity = {
            "unit": scope.name, "path": "/" + str(scope.relative_to(cgroups)),
            "device": scope.stat().st_dev, "inode": scope.stat().st_ino,
        }
        record.update(pid=DEAD_PID, pane_dispatch_id=dispatch, exit_code=0, command_slice=identity)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        original = path.read_bytes()
        with (patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(process_stop, "_term_slice") as term,
              patch.object(detached_run_process, "supervises", return_value=True)):
            reason = "Stop the private command."
            with self.assertRaisesRegex(process_stop.SliceStopFailed, "cannot read cgroup.events"):
                _ = detached_runs.stop_command(path, reason)
            self.assertEqual(path.read_bytes(), original)
            _ = events.write_text("populated 0\n")
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
            (target, text), = self.inputs
            self.assertEqual(target, pane)
            self.assertIn("Result: PASS (exit 0)", text)
            self.assertNotIn("FAIL (cleanup", text)
            with (patch.object(detached_runs, "_run", return_value=0),
                  patch.object(detached_runs, "scope_prefix", return_value=[])):
                self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
            term.assert_not_called()
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(self.notices, [])
        self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.ActiveDispatch)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["status"], "finished")
        self.assertNotIn("stop_reason", saved)
        self.assertNotIn("stop_pass_requested", saved)
        self.assertNotIn("scope_error", saved)

    def test_director_stop_releases_the_owning_pass_without_done(self) -> None:
        self.assert_committed_stop_cancels_owning_pass(interrupted=False)

    def test_stop_commit_survives_stopper_exit_after_signal(self) -> None:
        self.assert_committed_stop_cancels_owning_pass(interrupted=True)

    def assert_committed_stop_cancels_owning_pass(self, *, interrupted: bool) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        record = self.record(row)
        cgroups = self.fixture.root / "cgroups"
        scope = cgroups / "builds.slice/builds-detached.slice/builds-detached-directorstop.slice"
        scope.mkdir(parents=True)
        events = scope / "cgroup.events"
        _ = events.write_text("populated 1\n")
        identity: process_stop.RunSliceIdentity = {
            "unit": scope.name, "path": "/" + str(scope.relative_to(cgroups)),
            "device": scope.stat().st_dev, "inode": scope.stat().st_ino,
        }
        record.update(pid=DEAD_PID, command_slice=identity, pane_dispatch_id=dispatch)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        reason = "Stop the private command."

        signals = 0

        def finish_scope(_directory: int, saved_scope: process_stop.RunSliceIdentity) -> None:
            nonlocal signals
            self.assertEqual(saved_scope, identity)
            pending = worker_lifecycle.read_json_object(path)
            self.assertEqual(pending.get("stop_reason"), reason)
            self.assertIs(pending.get("stop_pass_requested"), True)
            self.assertNotIn("finished_at", pending)
            signals += 1
            if interrupted and signals == 1:
                raise SystemExit("fixture stopper died after TERM")
            _ = events.write_text("populated 0\n")

        if not process_stop.supported():
            original = path.read_bytes()
            with patch.object(process_stop, "_term_slice") as term:
                self.assertFalse(detached_runs.stop_command(path, reason))
                term.assert_not_called()
            self.assertEqual(path.read_bytes(), original)
            self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.ActiveDispatch)
            return

        _ = self.enterContext(patch.object(process_stop, "CGROUP_ROOT", cgroups))
        with patch.object(process_stop, "_term_slice", side_effect=finish_scope):
            if interrupted:
                with self.assertRaisesRegex(SystemExit, "fixture stopper died after TERM"):
                    _ = detached_runs.stop_command(path, reason)
                self.assertEqual(events.read_text(), "populated 1\n")
                pending = worker_lifecycle.read_json_object(path)
                self.assertNotIn("finished_at", pending)
                self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir),
                                      progress_history.ActiveDispatch)
                with patch.object(detached_run_process, "supervises", return_value=False):
                    self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [path.stem])
            else:
                self.assertTrue(detached_runs.stop_command(path, reason))
                self.assertTrue(detached_runs.stop_command(path, "A repeat."))
                self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir),
                                      progress_history.ActiveDispatch)
        self.assertEqual(signals, 2 if interrupted else 1)
        saved = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(saved.get("stop_reason"), reason)
        self.assertIs(saved.get("stop_pass_requested"), True)
        self.assertTrue(process_stop.slice_has_ended(identity))
        self.assertNotIn("self_stop", saved)
        if interrupted:
            self.assertTrue(saved.get("finished_at"))
            self.assertIn("Result: STOPPED (no exit status)", detached_runs.result_text(
                saved, detached_runs.command_result(saved)))
        else:
            saved["exit_code"] = 143
            write_text_atomic(path, json.dumps(saved))
        self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.NoActiveDispatch)
        passed = worker_panes.worker_pass(row, self.run_dir)
        self.assertEqual(passed.get("status"), "canceled")
        completion = progress_history.hook_completion(passed)
        self.assertIsInstance(completion, progress_history.HookCompletion)
        assert isinstance(completion, progress_history.HookCompletion)
        self.assertEqual((completion.outcome, completion.details, completion.reply_file), ("stopped", (reason,), ""))
        self.assertTrue(completion.pane_closed)
        self.assertEqual(self.fixture.world.closed[-1], ("w1:p1", "terminal-w1:p1"))
        member = staff.snapshot().by_id(row.id)
        assert member is not None
        self.assertEqual(member.current_session, row.current_session)
        self.assertEqual(worker_panes.worker_dispatch_state(row, self.run_dir), worker_panes.ReleasedWorkerPass.ENDED)
        worker_name = staff.WorkerName.parse(row.name)
        self.assertEqual(worker.select_worker(self.fixture.parent.id, "impl", staff.snapshot()), worker_name)
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(worker.answer(self.fixture.parent.id, worker_name, "Private answer."),
                             worker.ExitCode.REFUSED)
        self.assertIn("no active pass", error.getvalue())
        self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(row.current_session), reply=f"DONE {dispatch}")),
                         turn_end.TurnOutcome.STOPPED)
        self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        final_record = path.read_bytes()
        self.assertFalse(detached_runs.stop_command(path, reason))
        self.assertEqual(path.read_bytes(), final_record)
        self.assertFalse(reply.exists())
        self.assertEqual(self.inputs, [])
        self.assertEqual(len(self.notices), 1)
        self.assertIn(f"{row.name}: phase 40: stopped", self.notices[0][1])
        self.assertIn(reason, self.notices[0][1])

    def test_explicit_stop_keeps_a_pass_when_ownership_or_end_is_unproved(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        original = (self.run_dir / unit_runs.STATE_FILE).read_bytes()
        cases = (
            ("no pane pass", "", False, ""),
            ("different pass", "older-pass", False, "the linked pane pass is no longer current"),
            ("live group", dispatch, True, "the command group has no confirmed end"),
        )
        for run_id, linked, live, diagnostic in cases:
            with self.subTest(run=run_id):
                record = self.record(row, run_id.replace(" ", "-"))
                record.update(pid=DEAD_PID, command_group=DEAD_PID, command_started="private-start",
                              pane_dispatch_id=linked)
                path = Path(record["log"]).with_suffix(".json")
                write_text_atomic(path, json.dumps(record))
                with (patch.object(detached_run_process, "supervisor_match", return_value=True),
                      patch.object(process_start, "process_start", return_value=process_start.Started("private-start")),
                      patch.object(detached_runs, "_signal_stopped_command"),
                      contextlib.redirect_stdout(io.StringIO())):
                    self.assertTrue(self.record_confirmed_group_stop(path, "Private stop."))
                saved = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                saved["exit_code"] = 143
                write_text_atomic(path, json.dumps(saved))
                with (patch.object(detached_runs, "_group_is_live", return_value=live),
                      contextlib.redirect_stderr(io.StringIO()) as error):
                    self.assertEqual(detached_runs.main(["deliver", str(path)]), 1 if live else 0)
                if diagnostic:
                    self.assertIn(diagnostic, error.getvalue())
                else:
                    self.assertEqual(error.getvalue(), "")
                self.assertEqual((self.run_dir / unit_runs.STATE_FILE).read_bytes(), original)
                self.assertFalse(reply.exists())
        self.assertEqual(len(self.inputs), 1)
        self.assertIn("Detached run: no-pane-pass", self.inputs[0][1])
        self.assertEqual(self.notices, [])

    def create_stopped_run(self, row: staff.StaffRow, dispatch: str) -> Path:
        record = self.record(row)
        record.update(pid=DEAD_PID, command_group=DEAD_PID, exit_code=143, stop_pass_requested=True,
                      stop_reason="Private stop.", pane_dispatch_id=dispatch)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        return path

    def test_canceled_pass_recovers_late_done_after_a_result_from_an_earlier_pass(self) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        line = f'Read the unchanged prompt file "private.txt". End your final reply with DONE {dispatch}.'
        progress_history.save_dispatch_delivery(self.run_dir, dispatch,
            progress_history.DispatchDelivery(line, dispatch, str(reply)))
        agent.append(pane, "user", line)
        self.fixture.world.locations[pane]["agent_status"] = "working"
        path = self.create_stopped_run(row, dispatch)
        with patch.object(detached_runs, "_group_is_live", return_value=False):
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir).get("status"), "canceled")

        record = self.record(row, "earlier-result")
        record.update(status="finished", pane_dispatch_id="earlier-pass",
                      delivery=f"sent: worker {row.name} at {pane}")
        write_text_atomic(Path(record["log"]).with_suffix(".json"), json.dumps(record))
        done = f"DONE {dispatch}"
        agent.append(pane, "assistant", done)
        agent.append(pane, "user", "Detached run: earlier-result")
        agent.append(pane, "assistant", "The earlier check passed.")
        self.fixture.world.locations[pane]["agent_status"] = "done"

        fallback_line = "An earlier version of the assignment prompt."
        receipt = progress_history.StoppedPassReceipt(dispatch, fallback_line, dispatch, str(reply))
        self.assertEqual(worker.dispatch_receipt(row, fallback_line, dispatch, run=self.run_dir, pane_pass=receipt),
                         worker.DispatchComplete(done))
        self.assertIs(turn_end.run_finished(row.id), detached_runs.CompletionReplay.HANDLED)
        self.assertEqual(reply.with_name(f"reply_late_{dispatch}.txt").read_text().rstrip(), done)

    def test_stop_and_turn_hook_keep_one_outcome_for_verified_done(self) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        agent.pane_commands(self)
        cases = ((True, ""), (False, ""), (False, "new prompt"), (False, "new DONE"), (False, "detached result"),
                 (False, "slot cleared"), (False, "slot cleared with damaged ledger"),
                 (False, "slot cleared with broken dispatch link"),
                 (False, "assign after result"), (False, "retry after result"), (False, "retry after reassignment"),
                 (False, "assign during retry"), (False, "restore with pending reply"), (True, "early DONE"))
        for before_stop, later in cases:
            with self.subTest(before_stop=before_stop, later=later):
                agent.notices.clear()
                row = self.fixture.worker("impl", family="codex")
                pane = "w1:p1"
                self.fixture.world.add(row, pane, family="codex")
                dispatch, reply = self.begin_pane_pass(row)
                reply.unlink(missing_ok=True)
                delivery = pass_recorder.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
                result = f"The work is complete.\nDONE {dispatch}"
                agent.append(pane, "user", delivery.line)
                path = self.create_stopped_run(row, dispatch)
                if before_stop:
                    if later != "early DONE":
                        finished = worker_lifecycle.read_json_object(path)
                        finished["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                        write_text_atomic(path, json.dumps(finished))
                    agent.append(pane, "assistant", result)
                    self.fixture.world.locations[pane]["agent_status"] = "done"
                else:
                    self.fixture.world.locations[pane]["agent_status"] = "working"
                if later == "early DONE":
                    self.fixture.world.locations[pane]["agent_status"] = "working"
                    with (patch.object(detached_runs, "_group_is_live", return_value=False),
                          patch.object(send, "deliver_worker_text",
                                       return_value=send.Result("retry", "worker is working")) as submit,
                          contextlib.redirect_stderr(io.StringIO())):
                        self.assertEqual(detached_runs.main(["deliver", str(path)]), 1)
                    submit.assert_called_once()
                    self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
                    self.assertFalse(reply.exists())
                    self.assertEqual(agent.notices, [])
                    self.assertIn(pane, self.fixture.world.locations)
                    self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), True)
                    with self.subTest(delivery="early DONE after holder fallback"):
                        self.fixture.world.locations[pane]["agent_status"] = "blocked"
                        finished = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                        self.assertEqual(detached_runs.deliver(finished, detached_runs.CompletedCommand(143)),
                                         "sent: private director")
                        self.assertIn(f"Detached run: {finished['run_id']}", agent.notices[-1])
                        self.fixture.world.locations[pane]["agent_status"] = "done"
                        event = turn_end.WorkerEvent(str(row.current_session), reply=result)
                        self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.WAITING_ON_REPLY)
                        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "active")
                        self.assertFalse(reply.exists())
                        self.assertIn(pane, self.fixture.world.locations)
                        final = f"The stopped run has ended.\nDONE {dispatch}"
                        agent.append(pane, "assistant", final)
                        final_event = turn_end.WorkerEvent(str(row.current_session), reply=final)
                        self.assertEqual(turn_end.handle(final_event), turn_end.TurnOutcome.DONE)
                        self.assertEqual(reply.read_text().rstrip(), final)
                        self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)
                    continue
                with patch.object(detached_runs, "_group_is_live", return_value=False):
                    self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
                if not before_stop:
                    self.fixture.world.locations[pane]["agent_status"] = "done"
                    agent.append(pane, "assistant", result)
                    late_reply = reply.with_name(f"reply_late_{dispatch}.txt")
                    if later in ("slot cleared", "slot cleared with damaged ledger",
                                 "slot cleared with broken dispatch link"):
                        passed = worker_panes.worker_pass(row, self.run_dir)
                        state = unit_runs.read_run_fields(self.run_dir)
                        state["finishing_passes"] = {dispatch: passed}
                        state["pass"] = {}
                        write_text_atomic(self.run_dir / unit_runs.STATE_FILE, json.dumps(state))
                        self.fixture.world.locations[pane]["agent_status"] = "done"
                        if later != "slot cleared":
                            newer = self.run_dir.with_name("later-dispatch")
                            newest_state = newer / unit_runs.STATE_FILE
                            if later == "slot cleared with damaged ledger":
                                newer.mkdir()
                                ledger = newer / "events.jsonl"
                                write_text_atomic(newest_state, json.dumps({
                                    unit_runs.OWNER_FIELD: self.fixture.parent.id, "history_file": str(ledger),
                                }))
                                _ = ledger.write_text("{an incomplete dispatch\n")
                                damaged = newest_state
                            else:
                                link = staff.record_path().parent / "dispatches" / row.id
                                link.unlink()
                                link.symlink_to(newest_state)
                                damaged = link
                            try:
                                self.assertEqual(turn_end.recover_stopped_in_run(self.run_dir, row.id),
                                                 worker.CompletionRecovery.PENDING_CLOSE)
                                self.assertIn(pane, self.fixture.world.locations)
                                self.assertFalse(late_reply.exists())
                            finally:
                                damaged.unlink()
                            continue
                        self.assertEqual(turn_end.recover_stopped_in_run(self.run_dir, row.id),
                                         worker.CompletionRecovery.RECOVERED)
                        self.assertNotIn(pane, self.fixture.world.locations)
                        self.assertEqual(late_reply.read_text().rstrip(), result)
                        saved = progress_history.hook_completion(progress_history.pass_for_finalization(self.run_dir, dispatch))
                        assert isinstance(saved, progress_history.HookCompletion)
                        self.assertTrue(saved.finished)
                        continue
                    if later in ("detached result", "assign after result", "restore with pending reply"):
                        write_text_atomic(late_reply, result)
                        finished = self.record(row, "late-result")
                        finished.update(status="finished", exit_code=0, pane_dispatch_id=dispatch,
                                        delivery=f"sent: worker {row.name} at {pane}")
                        write_text_atomic(Path(finished["log"]).with_suffix(".json"), json.dumps(finished))
                        agent.append(pane, "user", "Detached run: late-result")
                        if later == "restore with pending reply":
                            with contextlib.redirect_stderr(io.StringIO()) as errors, contextlib.redirect_stdout(io.StringIO()):
                                self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.OK, errors.getvalue())
                            self.assertEqual(errors.getvalue(), "")
                            self.assertNotIn(pane, self.fixture.world.locations)
                            self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "canceled")
                            member = staff.by_id(row.id)
                            assert member is not None
                            self.assertEqual(member.current_session, row.current_session)
                            continue
                        self.fixture.world.locations[pane]["agent_status"] = "working"
                        with contextlib.redirect_stderr(io.StringIO()) as errors, contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.UNCERTAIN)
                        self.assertEqual(errors.getvalue().splitlines(),
                                         ["impl: a detached run result awaits a matching DONE"])
                        self.assertIn(pane, self.fixture.world.locations)
                        self.fixture.world.locations[pane]["agent_status"] = "done"
                        if later == "assign after result":
                            prompt = reply.with_name("replacement-assignment.txt")
                            write_text_atomic(prompt, "Start the replacement pass.")
                            policy = worker_panes.launch_policy("impl")
                            pid, family, _ = self.fixture.world.foreground[pane]
                            self.fixture.world.foreground[pane] = (pid, family, [
                                family, *worker_panes.launch_arguments(policy, row.current_session, self.fixture.root)])
                            self.fixture.world.processes[pid].environment["CODEX_HOME"] = os.environ["CODEX_HOME"]
                            self.fixture.world.processes[pid].environment[pane_launch.BERTH_SESSION_ENV] = (
                                f"{self.fixture.parent.current_session}.phase-40")
                            config = self.fixture.root / "conf/direct.conf"
                            write_text_atomic(config, config.read_text() + "# range: 1..600\nworker_settle_seconds=1\n")
                            launches = len(self.fixture.world.launches)
                            with (patch.object(subprocess, "run", side_effect=agent.run),
                                  contextlib.redirect_stderr(io.StringIO()) as errors):
                                self.assertIs(worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(row.name),
                                    "impl", prompt, session_dir=self.run_dir, slot=row.name, phase="40", kind="impl",
                                    launcher_command=["replacement-launcher"]), worker.DispatchSubmission.SUBMITTED,
                                    errors.getvalue())
                            self.assertEqual(errors.getvalue(), "")
                            self.assertEqual(len(self.fixture.world.launches), launches)
                            self.assertEqual(agent.submitted[-1][0], pane)
                            replacement = worker_panes.worker_pass(row, self.run_dir)
                            self.assertNotEqual(replacement.get("instance_id"), dispatch)
                            self.assertEqual(replacement.get("status"), "active")
                            continue
                        agent.append(pane, "assistant", result)

                    lock = worker_panes.worker_lock
                    locks = 0
                    latest = f"The final check passed.\nDONE {dispatch}"

                    def lock_after_notice(parent: staff.StaffId, name: staff.WorkerName, *, blocking: bool = False
                                          ) -> contextlib.AbstractContextManager[worker_panes.WorkerLockHold | worker_panes.Kept]:
                        nonlocal locks
                        locks += 1
                        if locks == 2:
                            self.assertTrue(pass_notices.is_settled(self.run_dir, dispatch, "stopped"))
                            if later == "new prompt":
                                agent.append(pane, "user", "Check another file.")
                                self.fixture.world.locations[pane]["agent_status"] = "working"
                            else:
                                self.assertEqual(late_reply.read_text().rstrip(), result)
                                agent.append(pane, "assistant", latest)
                        return lock(parent, name, blocking=blocking)

                    event = turn_end.WorkerEvent(str(row.current_session), reply=result)
                    closed_before = len(self.fixture.world.closed)
                    if later in ("retry after result", "retry after reassignment", "assign during retry"):
                        self.check_stopped_diagnostic_retry(agent, row, pane, dispatch, event,
                                                            reassign=later == "retry after reassignment",
                                                            assign_during_retry=later == "assign during retry")
                        continue
                    with contextlib.ExitStack() as stack:
                        errors = stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                        if later in ("new prompt", "new DONE"):
                            _ = stack.enter_context(patch.object(worker_panes, "worker_lock", side_effect=lock_after_notice))
                        self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.STOPPED)
                    if later == "new prompt":
                        self.assertEqual(errors.getvalue().splitlines(), ["impl: kept pane: agent state is working"])
                        self.assertEqual(self.stopped_diagnostics(dispatch),
                                         {"impl: kept pane: agent state is working": True})
                        self.assertEqual(agent.notices[1], "impl: kept pane: agent state is working")
                        self.assertEqual(agent.notice_senders[-1], send.CodexSender(str(row.current_session)))
                        self.assertIn(pane, self.fixture.world.locations)
                        self.assertEqual(len(self.fixture.world.closed), closed_before)
                        agent.append(pane, "assistant", result)
                        self.fixture.world.locations[pane]["agent_status"] = "done"
                        self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.STOPPED)
                    if later == "new DONE":
                        self.assertEqual(errors.getvalue(), "")
                        result = latest
                    self.assertEqual(len(self.fixture.world.closed), closed_before + 1)
                passed = worker_panes.worker_pass(row, self.run_dir)
                outcome = "done" if before_stop else "stopped"
                self.assertEqual(passed.get("status"), "completed" if before_stop else "canceled")
                completion = progress_history.hook_completion(passed)
                assert isinstance(completion, progress_history.HookCompletion)
                self.assertEqual(completion.outcome, outcome)
                reply_path = reply if before_stop else reply.with_name(f"reply_late_{dispatch}.txt")
                self.assertEqual(reply_path.read_text().rstrip(), result)
                self.assertEqual(reply.exists(), before_stop)
                notice_count = 2 if later == "new prompt" else 1
                self.assertEqual(len(agent.notices), notice_count)
                self.assertIn(f"phase 40: {outcome}", agent.notices[0])
                if not before_stop:
                    self.assertEqual(completion.reply_file, str(reply_path))
                    self.assertTrue(completion.pane_closed)
                    self.assertNotIn(pane, self.fixture.world.locations)
                    saved_reply = reply_path.read_bytes()
                    with patch.object(worker, "save_reply", wraps=worker.save_reply) as save:
                        self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(row.current_session), reply=result)),
                                         turn_end.TurnOutcome.STOPPED)
                        save.assert_not_called()
                    self.assertEqual(len(agent.notices), notice_count)
                    self.assertEqual(reply_path.read_bytes(), saved_reply)

    def stopped_diagnostics(self, dispatch: str) -> dict[str, bool]:
        record = pass_notices.read(self.run_dir, dispatch)
        assert record is not None
        return {notice.text: notice.settled for notice in record.notices.values()
                if notice.kind == pass_notices.NoticeKind.DIAGNOSTIC}

    def check_stopped_diagnostic_retry(self, agent: test_worker.DispatchAgent, row: staff.StaffRow,
                                       pane: str, dispatch: str, event: turn_end.WorkerEvent,
                                       *, reassign: bool, assign_during_retry: bool = False) -> None:
        message = "impl: kept pane: agent state is working"
        self.fixture.world.locations[pane]["agent_status"] = "working"
        deliver = send.send

        def fail_diagnostic(options: send.Options) -> send.Result:
            if send.body_text(options) == message:
                return send.Result("failed", "private diagnostic send failed")
            return deliver(options)

        with (patch.object(send, "send", side_effect=fail_diagnostic),
              contextlib.redirect_stderr(io.StringIO()),
              self.assertRaisesRegex(ValueError, "private diagnostic send failed")):
            _ = turn_end.handle(event)
        self.assertEqual(self.stopped_diagnostics(dispatch), {message: False})
        self.assertEqual(len(agent.notices), 1)
        self.assertIn("phase 40: stopped", agent.notices[0])

        if reassign:
            prompt = self.fixture.root / "diagnostic-replacement.txt"
            write_text_atomic(prompt, "Start the replacement pass.")
            replacement = progress_history.begin_dispatch(
                self.run_dir, slot=row.name, phase="40", role="impl", worker_name=row.name, worker_id=row.id,
                launcher_command=["replacement-launcher"], prompt=prompt, kind="impl", lens="")
            active = progress_history.active_dispatch(row.id, self.run_dir)
            assert isinstance(active, progress_history.ActiveDispatch)
            self.assertEqual(active.instance_id, replacement)
        else:
            finished = self.record(row, "diagnostic-result")
            finished.update(status="finished", exit_code=0, pane_dispatch_id=dispatch,
                            delivery=f"sent: worker {row.name} at {pane}")
            write_text_atomic(Path(finished["log"]).with_suffix(".json"), json.dumps(finished))
            agent.append(pane, "user", "Detached run: diagnostic-result")

        if assign_during_retry:
            self.fixture.world.locations[pane]["agent_status"] = "idle"
            prompt = self.fixture.root / "assign-during-retry.txt"
            write_text_atomic(prompt, "Start the replacement pass.")
            policy = worker_panes.launch_policy("impl")
            pid, family, _ = self.fixture.world.foreground[pane]
            self.fixture.world.foreground[pane] = (pid, family, [
                family, *worker_panes.launch_arguments(policy, row.current_session, self.fixture.root)])
            self.fixture.world.processes[pid].environment["CODEX_HOME"] = os.environ["CODEX_HOME"]
            config = self.fixture.root / "conf/direct.conf"
            write_text_atomic(config, config.read_text() + "# range: 1..600\nworker_settle_seconds=1\n")

            def assign_before_delivery(options: send.Options) -> send.Result:
                if send.body_text(options) == message:
                    with (patch.object(subprocess, "run", side_effect=agent.run),
                          contextlib.redirect_stderr(io.StringIO()) as errors):
                        self.assertIs(worker.dispatch(self.fixture.parent.id, staff.WorkerName.parse(row.name),
                            "impl", prompt, session_dir=self.run_dir, slot=row.name, phase="40", kind="impl",
                            launcher_command=["replacement-launcher"]), worker.DispatchSubmission.SUBMITTED,
                            errors.getvalue())
                return deliver(options)

            with patch.object(send, "send", side_effect=assign_before_delivery):
                self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.STOPPED)
                self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.IGNORED)
            self.assertEqual(self.stopped_diagnostics(dispatch), {message: True})
            self.assertEqual(agent.notices[1:], [message])
            self.assertEqual(agent.submitted[-1][0], pane)
            passed = worker_panes.worker_pass(row, self.run_dir)
            self.assertNotEqual(passed.get("instance_id"), dispatch)
            self.assertEqual(passed.get("status"), "active")
            return

        before = (self.run_dir / unit_runs.STATE_FILE).read_bytes()
        self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.STOPPED)
        self.assertEqual(self.stopped_diagnostics(dispatch), {message: True})
        self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.STOPPED)
        self.assertEqual(agent.notices[1:], [message])
        self.assertIn(pane, self.fixture.world.locations)
        self.assertEqual((self.run_dir / unit_runs.STATE_FILE).read_bytes(), before)

    def stopped_diagnostic_fixture(self, *, end_worker: bool = False) -> StoppedDiagnosticFixture:
        agent = test_worker.DispatchAgent(self.fixture)
        agent.pane_commands(self)
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        delivery = pass_recorder.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        progress_history.save_dispatch_delivery(self.run_dir, dispatch,
            replace(delivery, actions=replace(delivery.actions, end_worker=end_worker)))
        agent.append(pane, "user", delivery.line)
        path = self.create_stopped_run(row, dispatch)
        with (patch.object(detached_runs, "_group_is_live", return_value=False),
              patch.object(worker_panes, "close", return_value=worker_panes.Kept("fixture defers pane close"))):
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        agent.append(pane, "assistant", f"The work is complete.\nDONE {dispatch}")
        self.fixture.world.locations[pane]["agent_status"] = "done"
        digest = hashlib.sha256(dispatch.encode()).hexdigest()
        diagnostics = self.run_dir / f".{digest}.completion-diagnostics.json"
        return StoppedDiagnosticFixture(agent, row, pane, dispatch, path, diagnostics)

    def saved_diagnostics(self, dispatch: str, legacy: Path) -> dict[str, bool]:
        """Read the pass diagnostics; a legacy file that no retry imported yet is on top."""
        record = pass_notices.read(self.run_dir, dispatch)
        saved = {} if record is None else {notice.text: notice.settled for notice in record.notices.values()
                                           if notice.kind == pass_notices.NoticeKind.DIAGNOSTIC}
        if legacy.is_file():
            saved |= {text: settled is True for text, settled in worker_lifecycle.read_json_object(legacy).items()}
        return saved

    def check_diagnostic_retry_after_close(self, *, end_worker: bool,
                                           retry: Literal["direct", "detached", "terminal run"]) -> None:
        case = self.stopped_diagnostic_fixture(end_worker=end_worker)
        parent = self.fixture.parent
        identity = case.row.id
        name = staff.WorkerName.parse(case.row.name)
        message = "detached run cleanup failed for impl: private cleanup failure"
        attempts: list[str] = []
        deliver = send.send

        def send_diagnostic(options: send.Options) -> send.Result:
            if send.body_text(options) == message:
                with worker_panes.worker_lock(parent.id, name) as hold:
                    self.assertIsInstance(hold, worker_panes.WorkerLockHold)
                self.assertEqual(options.sender_origin, send.ForwardedSender(send.CodexSender(str(case.row.current_session))))
                self.assertEqual(options.recipient_identity, send.LogicalRecipientIdentity(f"staff:{parent.id}"))
                attempts.append(message)
                if len(attempts) == 1:
                    return send.Result("failed", "private diagnostic send failed")
            return deliver(options)

        with patch.object(send, "send", side_effect=send_diagnostic):
            with (patch.object(detached_runs, "prune_finished", side_effect=OSError("private cleanup failure")),
                  contextlib.redirect_stderr(io.StringIO()) as errors,
                  worker_panes.worker_lock(parent.id, name) as hold):
                assert isinstance(hold, worker_panes.WorkerLockHold)
                self.assertEqual(turn_end.recover_stopped_in_run(self.run_dir, identity, hold=hold),
                                 worker.CompletionRecovery.RECOVERED)
                self.assertNotIn(case.pane, self.fixture.world.locations)
                self.assertEqual(attempts, [])
                if end_worker:
                    self.assertIsNone(staff.by_id(identity))
            self.assertIn("private diagnostic send failed", errors.getvalue())
            self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {message: False})
            saved = progress_history.hook_completion(progress_history.pass_for_finalization(self.run_dir, case.dispatch))
            assert isinstance(saved, progress_history.HookCompletion)
            self.assertTrue(saved.finished)
            self.assertEqual(attempts, [message])
            if end_worker:
                self.assertIsNone(staff.by_id(identity))
                replacement = self.fixture.worker("replacement", family="claude")
                prompt = self.fixture.root / "assignment.txt"
                _ = progress_history.begin_dispatch(self.run_dir, slot=name.value, phase="40", role="impl",
                    worker_name=replacement.name, worker_id=replacement.id, launcher_command=["replacement-launcher"],
                    prompt=prompt, kind="impl", lens="")
                self.assertEqual(progress_history.pass_for_finalization(self.run_dir, case.dispatch), {})
            if retry == "terminal run":
                _ = staff.clear_run(parent.id, self.run_dir)
                fields = unit_runs.read_run_fields(self.run_dir)
                fields.update(status="stopped", finished_at=1)
                write_text_atomic(self.run_dir / unit_runs.STATE_FILE, json.dumps(fields))
                case.detached_record.unlink()
            for _ in range(2):
                if retry == "direct":
                    result = turn_end.recover_stopped_in_run(self.run_dir, identity)
                    expected = worker.CompletionRecovery.PENDING_CLOSE if end_worker else worker.CompletionRecovery.NO_PENDING_NOTICE
                    self.assertEqual(result, expected)
                elif retry == "detached":
                    self.assertEqual(detached_runs.main(["deliver", str(case.detached_record)]), 0)
                else:
                    arguments = ["progress_history.py", "recover-run-ends", "--root", str(self.run_dir.parent)]
                    with patch.object(sys, "argv", arguments):
                        progress_history.main()
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {message: True})
        self.assertEqual(attempts, [message, message])
        self.assertEqual(case.agent.notices[1:], [message])

    def test_direct_recovery_retries_diagnostics_after_closing_the_pane(self) -> None:
        self.check_diagnostic_retry_after_close(end_worker=False, retry="direct")

    def test_direct_recovery_retries_after_retirement_and_pass_replacement(self) -> None:
        self.check_diagnostic_retry_after_close(end_worker=True, retry="direct")

    def test_detached_delivery_retries_diagnostics_after_retirement(self) -> None:
        self.check_diagnostic_retry_after_close(end_worker=True, retry="detached")

    def test_restore_delivers_the_diagnostic_after_detached_completion_closes_the_run(self) -> None:
        case = self.stopped_diagnostic_fixture(end_worker=True)
        parent = self.fixture.parent
        passed = progress_history.pass_for_finalization(self.run_dir, case.dispatch)
        delivery = progress_history.dispatch_delivery(passed)
        progress_history.save_dispatch_delivery(self.run_dir, case.dispatch,
            replace(delivery, actions=replace(delivery.actions, finish_run=True)))
        saved = worker_lifecycle.read_json_object(case.detached_record)
        self.assertEqual(saved["delivery"], "finished: the pane pass stopped")
        saved["completion_pending"] = True
        write_text_atomic(case.detached_record, json.dumps(saved))
        message = "detached run cleanup failed for impl: private cleanup failure"
        deliver = send.send

        def unavailable(options: send.Options) -> send.Result:
            if send.body_text(options) == message:
                return send.Result("failed", "private diagnostic send failed")
            return deliver(options)

        with (patch.object(send, "send", side_effect=unavailable),
              patch.object(detached_runs, "prune_finished", side_effect=OSError("private cleanup failure")),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertTrue(unit_runs.terminal_run(unit_runs.read_run_fields(self.run_dir)))
            self.assertIs(progress_history.end_run(self.run_dir, progress_history.RetryRunEnd.RECOVER),
                          progress_history.RunEndSettled.CLOSED)
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertIn("private diagnostic send failed", errors.getvalue())
        self.assertIsNone(staff.by_id(case.row.id))
        self.assertIsNone(unit_runs.open_run(str(parent.current_session)))
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics)[message], False)
        self.assertIs(worker_lifecycle.read_json_object(case.detached_record)["completion_pending"], True)

        _ = detached_runs.prune_finished(case.detached_record.parent)

        self.assertTrue(case.detached_record.is_file())
        self.assertEqual(worker.restore(parent.id), worker.ExitCode.OK)
        self.assertEqual(worker.restore(parent.id), worker.ExitCode.OK)
        self.assertIs(worker_lifecycle.read_json_object(case.detached_record)["completion_pending"], False)
        self.assertIs(self.saved_diagnostics(case.dispatch, case.diagnostics)[message], True)
        self.assertEqual(case.agent.notices.count(message), 1)

    def test_ordinary_completion_keeps_diagnostics_from_the_stopped_pass(self) -> None:
        case = self.stopped_diagnostic_fixture(end_worker=True)
        record = self.record(case.row, "ordinary-result")
        record.update(status="finished", exit_code=0, pid=DEAD_PID, completion_pending=True,
                      pane_dispatch_id=case.dispatch, delivery=f"sent: worker {case.row.name} at {case.pane}")
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        path.with_suffix(".record.lock").touch()
        case.agent.append(case.pane, "user", "Detached run: ordinary-result")
        case.agent.append(case.pane, "assistant", f"The check passed.\nDONE {case.dispatch}")
        message = "detached run cleanup failed for impl: private cleanup failure"
        deliver = send.send

        def unavailable(options: send.Options) -> send.Result:
            if send.body_text(options) == message:
                return send.Result("failed", "private diagnostic send failed")
            return deliver(options)

        with (patch.object(send, "send", side_effect=unavailable),
              patch.object(detached_runs, "prune_finished", side_effect=OSError("private cleanup failure")),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])

        self.assertIsNone(staff.by_id(case.row.id), errors.getvalue())
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {message: False})
        self.assertIs(worker_lifecycle.read_json_object(path)["completion_pending"], True)
        _ = detached_runs.prune_finished(path.parent)
        self.assertTrue(path.is_file())

        self.assertEqual(worker.restore(self.fixture.parent.id), worker.ExitCode.OK)

        self.assertIs(worker_lifecycle.read_json_object(path)["completion_pending"], False)
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {message: True})
        self.assertEqual(case.agent.notices[1:], [message])

    def test_finished_stop_preserves_an_unreadable_diagnostic_for_restore(self) -> None:
        case = self.stopped_diagnostic_fixture(end_worker=True)
        parent = self.fixture.parent
        with patch.object(detached_runs, "prune_finished", return_value=[]):
            self.assertIs(turn_end.recover_stopped_in_run(self.run_dir, case.row.id),
                          worker.CompletionRecovery.RECOVERED)
        self.assertIsNone(staff.by_id(case.row.id))
        self.assertIs(progress_history.end_run(self.run_dir, progress_history.RunEndRequested("stopped")),
                      progress_history.RunEndSettled.CLOSED)
        self.assertIsNone(unit_runs.open_run(str(parent.current_session)))
        self.assertEqual(detached_runs.main(["deliver", str(case.detached_record)]), 0)
        self.assertIs(worker_lifecycle.read_json_object(case.detached_record)["completion_pending"], False)
        write_text_atomic(case.diagnostics, "{")

        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(detached_runs.main(["deliver", str(case.detached_record)]), 1)
            self.assertIs(worker_lifecycle.read_json_object(case.detached_record)["completion_pending"], True)
            self.assertEqual(worker.restore(parent.id), worker.ExitCode.OK)

        self.assertIn("close diagnostic retry failed", errors.getvalue())
        self.assertIs(worker_lifecycle.read_json_object(case.detached_record)["completion_pending"], True)
        self.assertEqual(detached_runs.prune_finished(case.detached_record.parent), [])
        message = "Pending diagnostic."
        write_text_atomic(case.diagnostics, json.dumps({message: False}))
        self.assertEqual(worker.restore(parent.id), worker.ExitCode.OK)
        self.assertEqual(worker.restore(parent.id), worker.ExitCode.OK)
        self.assertIs(worker_lifecycle.read_json_object(case.detached_record)["completion_pending"], False)
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {message: True})
        self.assertEqual(case.agent.notices[1:], [message])

    def test_terminal_run_recovery_retries_diagnostics_without_a_run_pointer(self) -> None:
        self.check_diagnostic_retry_after_close(end_worker=True, retry="terminal run")

    def test_blocked_stopped_cleanup_retries_diagnostics_from_every_receipt(self) -> None:
        case = self.stopped_diagnostic_fixture()
        prompt = self.fixture.root / "assignment.txt"
        later = progress_history.begin_dispatch(self.run_dir, slot=case.row.name, phase="40", role="impl",
            worker_name=case.row.name, worker_id=case.row.id, launcher_command=["later-launcher"],
            prompt=prompt, kind="impl", lens="")
        self.assertEqual(progress_history.cancel_worker_passes(self.run_dir, case.row.id), 1)
        digest = hashlib.sha256(later.encode()).hexdigest()
        later_path = self.run_dir / f".{digest}.completion-diagnostics.json"
        first_message, later_message = "First pending diagnostic.", "Later pending diagnostic."
        write_text_atomic(case.diagnostics, json.dumps({first_message: False}))
        write_text_atomic(later_path, json.dumps({later_message: False}))
        deliver = send.send

        def fail_first(options: send.Options) -> send.Result:
            if send.body_text(options) == first_message:
                return send.Result("failed", "private first diagnostic failure")
            return deliver(options)

        with (patch.object(turn_end, "_stopped_pane_use", return_value=worker.DispatchEvidenceUnknown("missing pane evidence")),
              patch.object(send, "send", side_effect=fail_first), contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(turn_end.recover_stopped_in_run(self.run_dir, case.row.id), worker.CompletionRecovery.PENDING_CLOSE)
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {first_message: False})
        self.assertEqual(self.saved_diagnostics(later, later_path), {later_message: True})
        self.assertEqual(case.agent.notices[1:], [later_message])
        parent = self.fixture.parent
        with worker_panes.worker_lock(parent.id, staff.WorkerName.parse(case.row.name)) as hold:
            assert isinstance(hold, worker_panes.WorkerLockHold)
            self.assertEqual(turn_end.recover_completion(case.row, parent, self.run_dir, hold,
                record=staff.snapshot(), pane=worker.KeepCompletedPane.KEEP), worker.CompletionRecovery.NO_PENDING_NOTICE)
            self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {first_message: False})
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {first_message: True})
        self.assertEqual(case.agent.notices[1:], [later_message, first_message])

    def test_stopped_diagnostic_added_during_delivery_survives_the_success_write(self) -> None:
        case = self.stopped_diagnostic_fixture()
        first, second = "First pending diagnostic.", "impl: kept pane: private close failure"
        write_text_atomic(case.diagnostics, json.dumps({first: False}))
        deliver = send.send

        def record_close_failure(options: send.Options) -> send.Result:
            if send.body_text(options) == first:
                parent = self.fixture.parent
                with worker_panes.worker_lock(parent.id, staff.WorkerName.parse(case.row.name)) as hold:
                    assert isinstance(hold, worker_panes.WorkerLockHold)
                    with (patch.object(worker_panes, "close", return_value=worker_panes.Kept("private close failure")),
                          self.assertRaisesRegex(ValueError, "private close failure")):
                        _ = turn_end.recover_stopped_in_run(self.run_dir, case.row.id, hold=hold)
                    self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {first: False, second: False})
            return deliver(options)

        with patch.object(send, "send", side_effect=record_close_failure):
            self.assertIs(turn_end.retry_stopped_diagnostics(self.run_dir), worker_panes.StoppedDiagnosticRecovery.PENDING)
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {first: True, second: False})
        self.assertIs(turn_end.retry_stopped_diagnostics(self.run_dir), worker_panes.StoppedDiagnosticRecovery.SETTLED)
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {first: True, second: True})
        self.assertEqual(case.agent.notices[1:], [first, second])

    def test_stopped_diagnostic_retry_keeps_the_message_key_after_a_success_write_failure(self) -> None:
        case = self.stopped_diagnostic_fixture()
        message = "Pending diagnostic."
        write_text_atomic(case.diagnostics, json.dumps({message: False}))
        deliver = send.send
        attempts: list[tuple[str | None, send.TransportRecipientIdentity | send.LogicalRecipientIdentity]] = []

        def deliver_once(options: send.Options) -> send.Result:
            receipt = options.key, options.recipient_identity
            if receipt in attempts:
                attempts.append(receipt)
                return send.Result("skipped", "private recipient has the message")
            attempts.append(receipt)
            return deliver(options)

        with patch.object(send, "send", side_effect=deliver_once):
            with (patch.object(pass_notices, "_settle", side_effect=OSError("private diagnostic stamp failure")),
                  contextlib.redirect_stderr(io.StringIO())):
                self.assertIs(turn_end.retry_stopped_diagnostics(self.run_dir), worker_panes.StoppedDiagnosticRecovery.PENDING)
            self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {message: False})
            self.assertIs(turn_end.retry_stopped_diagnostics(self.run_dir), worker_panes.StoppedDiagnosticRecovery.SETTLED)
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {message: True})
        first, second = attempts
        self.assertEqual(first, second)
        self.assertTrue(first[0])
        self.assertEqual(case.agent.notices[1:], [message])

    def test_busy_notice_lock_and_unreadable_receipt_leave_diagnostics_pending(self) -> None:
        case = self.stopped_diagnostic_fixture()
        message = "Pending diagnostic."
        write_text_atomic(case.diagnostics, json.dumps({message: False}))
        lock = case.diagnostics.with_name(case.diagnostics.name.replace(".completion-diagnostics.json",
                                                                        ".pass-notices.delivery.lock"))
        with lock.open("a") as delivery:
            fcntl.flock(delivery, fcntl.LOCK_EX)
            self.assertIs(turn_end.retry_stopped_diagnostics(self.run_dir), worker_panes.StoppedDiagnosticRecovery.PENDING)
        state = self.run_dir / unit_runs.STATE_FILE
        saved = state.read_bytes()
        write_text_atomic(state, "{")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(turn_end.retry_stopped_diagnostics(self.run_dir), worker_panes.StoppedDiagnosticRecovery.UNREADABLE)
            self.assertEqual(turn_end.recover_stopped_in_run(self.run_dir, case.row.id), worker.CompletionRecovery.PENDING_CLOSE)
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {message: False})
        self.assertEqual(case.agent.notices[1:], [])
        _ = state.write_bytes(saved)
        fields = unit_runs.read_run_fields(self.run_dir)
        receipts = pass_recorder.object_dict(fields["stopped_pass_notices"])
        assert receipts is not None
        fields["stopped_pass_notices"] = {"unreadable": "invalid private receipt", **receipts}
        write_text_atomic(state, json.dumps(fields))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(turn_end.retry_stopped_diagnostics(self.run_dir), worker_panes.StoppedDiagnosticRecovery.UNREADABLE)
        self.assertEqual(self.saved_diagnostics(case.dispatch, case.diagnostics), {message: True})
        _ = state.write_bytes(saved)
        self.assertIs(turn_end.retry_stopped_diagnostics(self.run_dir), worker_panes.StoppedDiagnosticRecovery.SETTLED)
        self.assertEqual(case.agent.notices[1:], [message])

    def test_stopped_diagnostic_retry_uses_the_saved_sender_and_director(self) -> None:
        case = self.stopped_diagnostic_fixture()
        parent = self.fixture.parent
        message = "Pending diagnostic."
        write_text_atomic(case.diagnostics, json.dumps({message: False}))
        owner = fixture_member("showrunner", name="new-owner")
        updated = staff.native_id(fixture_runtime_id("replacement-worker-session"))
        _ = staff.update_current_session(case.row.current_session, updated)
        fixture_session(updated, staff.ClaudeBackend())
        _ = staff.move(case.row.id, staff.ReportsTo(owner.id))
        deliver = send.send

        def check_recipient(options: send.Options) -> send.Result:
            self.assertEqual(options.recipient_identity, send.LogicalRecipientIdentity(f"staff:{parent.id}"))
            self.assertEqual(options.to, f"session:{parent.current_session}")
            self.assertEqual(options.sender_origin, send.ForwardedSender(send.CodexSender(str(case.row.current_session))))
            return deliver(options)

        with patch.object(send, "send", side_effect=check_recipient):
            self.assertIs(turn_end.retry_stopped_diagnostics(self.run_dir), worker_panes.StoppedDiagnosticRecovery.SETTLED)
        self.assertEqual(case.agent.notices[1:], [message])

    def stopped_pane_pass(self) -> StoppedPanePass:
        """Start a pane pass with no reply and save its stopped detached run."""
        agent = test_worker.DispatchAgent(self.fixture)
        agent.pane_commands(self)
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        reply.unlink(missing_ok=True)
        delivery = pass_recorder.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", delivery.line)
        return StoppedPanePass(agent, row, pane, dispatch, reply, self.create_stopped_run(row, dispatch))

    def late_done(self, case: StoppedPanePass) -> Path:
        """Send a late DONE for the stopped pass and return its saved reply."""
        result = f"The work is complete.\nDONE {case.dispatch}"
        case.agent.append(case.pane, "assistant", result)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(case.row.current_session), reply=result)),
                             turn_end.TurnOutcome.STOPPED)
        late_reply = case.reply.with_name(f"reply_late_{case.dispatch}.txt")
        self.assertEqual(late_reply.read_text().rstrip(), result)
        return late_reply

    def assert_stop_notices(self, case: StoppedPanePass, late_reply: Path) -> None:
        """The director has the stop notice, then the late reply path, and the record settles the second."""
        stopped, replaced = case.agent.notices
        self.assertNotIn("reply", stopped)
        self.assertIn(f"stopped; reply {late_reply}\nPass {case.dispatch}.", replaced)
        record = pass_notices.read(self.run_dir, case.dispatch)
        assert record is not None
        self.assertIn(record.notices["stopped"].text, replaced)
        self.assertTrue(record.notices["stopped"].settled)

    def test_a_failed_stop_notice_retries_with_the_late_reply_path(self) -> None:
        case = self.stopped_pane_pass()
        case.agent.notice_failures = 1
        with (patch.object(detached_runs, "_group_is_live", return_value=False),
              contextlib.redirect_stderr(io.StringIO())):
            _ = detached_runs.main(["deliver", str(case.detached_record)])
        self.assertEqual(case.agent.notices, [])
        self.assertFalse(pass_notices.is_settled(self.run_dir, case.dispatch, "stopped"))

        late_reply = self.late_done(case)
        notice, = case.agent.notices
        self.assertIn(f"stopped; reply {late_reply}\nPass {case.dispatch}.", notice)
        self.assertTrue(pass_notices.is_settled(self.run_dir, case.dispatch, "stopped"))

    def test_a_late_reply_staged_during_the_stop_notice_send_reaches_the_director(self) -> None:
        case = self.stopped_pane_pass()
        deliver = send.send
        late_replies: list[Path] = []

        def stage_late_reply(options: send.Options) -> send.Result:
            result = deliver(options)
            if not late_replies:
                # The stop notice send holds the delivery lock, so the late reply only stages its text here.
                with patch.object(pass_notices, "_deliver"):
                    late_replies.append(self.late_done(case))
            return result

        with (patch.object(send, "send", side_effect=stage_late_reply),
              patch.object(detached_runs, "_group_is_live", return_value=False)):
            self.assertEqual(detached_runs.main(["deliver", str(case.detached_record)]), 1)
        late_reply, = late_replies
        self.assertEqual(len(case.agent.notices), 1)
        self.assertFalse(pass_notices.is_settled(self.run_dir, case.dispatch, "stopped"))
        self.assertIs(worker_lifecycle.read_json_object(case.detached_record)["completion_pending"], True)
        self.assertEqual(detached_runs.main(["deliver", str(case.detached_record)]), 0)
        self.assert_stop_notices(case, late_reply)

    def test_a_late_reply_after_a_stop_notice_crash_reaches_the_director_through_retry(self) -> None:
        case = self.stopped_pane_pass()
        deliver = send.send

        def accept(options: send.Options) -> send.Result:
            result = deliver(options)
            assert options.key is not None
            message = send.Message(send.staff_queue_key(self.fixture.parent.id), case.row.name, "", "", options.key)
            send.record_acceptance(message, send.now())
            return result

        _ = self.enterContext(patch.object(send, "STATE", self.fixture.root / "message-state"))
        _ = self.enterContext(patch.object(send, "send", side_effect=accept))
        with (patch.object(pass_notices, "_settle", side_effect=KeyboardInterrupt),
              patch.object(detached_runs, "_group_is_live", return_value=False),
              self.assertRaises(KeyboardInterrupt)):
            _ = detached_runs.main(["deliver", str(case.detached_record)])
        self.assertEqual(len(case.agent.notices), 1)
        with patch.object(pass_notices, "_deliver"):
            late_reply = self.late_done(case)
        pass_notices.retry(self.run_dir)
        self.assert_stop_notices(case, late_reply)

    def test_legacy_diagnostics_without_a_saved_sender_keep_the_record_readable(self) -> None:
        case = self.stopped_diagnostic_fixture()
        state_path = pass_recorder.state_path(self.run_dir)
        state = worker_lifecycle.read_json_object(state_path)
        notices = cast(dict[str, dict[str, dict[str, object]]], state["stopped_pass_notices"])
        del notices[case.dispatch]["receipt"]["sender_native_id"]
        write_text_atomic(state_path, json.dumps(state))
        # A new worker session leaves no current sender for the stopped pass.
        updated = staff.native_id(fixture_runtime_id("replacement-worker-session"))
        _ = staff.update_current_session(case.row.current_session, updated)
        message = "Pending diagnostic."
        write_text_atomic(case.diagnostics, json.dumps({message: False}))
        deliver = send.send

        def check_sender(options: send.Options) -> send.Result:
            self.assertEqual(options.sender_origin, send.ForwardedSender(send.UnavailableSender()))
            return deliver(options)

        with patch.object(send, "send", side_effect=check_sender):
            self.assertIs(turn_end.retry_stopped_diagnostics(self.run_dir), worker_panes.StoppedDiagnosticRecovery.SETTLED)
        self.assertFalse(case.diagnostics.exists())
        record = pass_notices.read(self.run_dir, case.dispatch)
        assert record is not None
        self.assertEqual([(notice.text, notice.sender_native_id, notice.settled) for notice in record.notices.values()
                          if notice.kind == pass_notices.NoticeKind.DIAGNOSTIC], [(message, "unavailable", True)])
        self.assertEqual(case.agent.notices[1:], [message])

    def test_late_done_after_reassignment_uses_the_stopped_pass_receipt(self) -> None:
        for status, shared_role in (("idle", False), ("done", False), ("working", True)):
            with self.subTest(status=status, shared_role=shared_role):
                self.check_late_done_after_reassignment(include_reply=True, pane_status=status, shared_role=shared_role)

    def test_stop_hook_without_reply_recovers_late_done_after_reassignment(self) -> None:
        self.check_late_done_after_reassignment(include_reply=False)

    def test_stop_hook_recovers_late_done_after_a_later_user_message(self) -> None:
        self.check_late_done_after_reassignment(include_reply=False, later_user_message=True)

    def test_stop_hook_saves_both_the_archived_and_current_replies(self) -> None:
        cases: tuple[tuple[Literal["", "directory", "invalid UTF-8"], bool], ...] = (
            ("", False), ("directory", False), ("invalid UTF-8", False),
            ("directory", True), ("invalid UTF-8", True))
        for archive_error, native_hook in cases:
            with self.subTest(archive_error=archive_error, native_hook=native_hook):
                self.check_late_done_after_reassignment(include_reply=False, replacement_reply="The work needs more time.",
                                                        archive_error=archive_error, native_hook=native_hook)

    def test_archived_stop_requires_done_after_its_detached_result(self) -> None:
        self.check_late_done_after_reassignment(include_reply=True, detached_result=True)

    def check_late_done_after_reassignment(self, *, include_reply: bool, later_user_message: bool = False,
                                           replacement_reply: str = "", detached_result: bool = False,
                                           pane_status: str = "idle",
                                           archive_error: Literal["", "directory", "invalid UTF-8"] = "",
                                           native_hook: bool = False, shared_role: bool = False) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        agent.pane_commands(self)
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        old_delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        if shared_role:
            old_delivery = replace(old_delivery, actions=progress_history.CompletionActions(held_role=row.id))
            progress_history.save_dispatch_delivery(self.run_dir, dispatch, old_delivery)
        agent.append(pane, "user", old_delivery.line)
        self.fixture.world.locations[pane]["agent_status"] = "working"
        path = self.create_stopped_run(row, dispatch)
        with patch.object(detached_runs, "_group_is_live", return_value=False):
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        prompt = reply.with_name("replacement-assignment.txt")
        write_text_atomic(prompt, "Start the replacement pass.")
        replacement = progress_history.begin_dispatch(
            self.run_dir, slot=row.name, phase="40", role="impl", worker_name=row.name, worker_id=row.id,
            launcher_command=["replacement-launcher"], prompt=prompt, kind="impl", lens="")
        new_reply = reply.with_name("replacement-reply.txt")
        new_line = f"Read the unchanged prompt file {prompt}. End your final reply with DONE {replacement}."
        progress_history.save_dispatch_delivery(self.run_dir, replacement,
            progress_history.DispatchDelivery(new_line, replacement, str(new_reply), actions=old_delivery.actions))
        agent.append(pane, "user", new_line)
        self.fixture.world.locations[pane]["agent_status"] = pane_status
        current_pass = worker_panes.worker_pass(row, self.run_dir)
        result = f"The stopped pass has a late reply.\nDONE {dispatch}"
        hook: dict[str, object] = {"hook_event_name": "Stop", "session_id": str(row.current_session)}
        if include_reply:
            hook["last_assistant_message"] = result
        event = turn_end.hook_event(hook)
        late_reply = reply.with_name(f"reply_late_{dispatch}.txt")
        with patch.object(astra_roles, "release", wraps=astra_roles.release) as release:
            self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.IGNORED)
        if shared_role:
            release.assert_not_called()
        self.assertFalse(late_reply.exists())
        agent.append(pane, "assistant", result)
        if detached_result:
            finished = self.record(row, "late-result")
            finished.update(status="finished", pane_dispatch_id=dispatch,
                            delivery=f"sent: worker {row.name} at {pane}")
            write_text_atomic(Path(finished["log"]).with_suffix(".json"), json.dumps(finished))
            self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.IGNORED)
            self.assertFalse(late_reply.exists())
            agent.append(pane, "user", "Detached run: late-result")
            self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.IGNORED)
            self.assertFalse(late_reply.exists())
            agent.append(pane, "assistant", result)
        if replacement_reply:
            agent.append(pane, "assistant", replacement_reply)
            hook["last_assistant_message"] = replacement_reply
            if archive_error:
                if archive_error == "directory":
                    late_reply.mkdir()
                else:
                    _ = late_reply.write_bytes(b"\xff")
            if native_hook:
                log = self.fixture.root / "state/herdr/turn-end-ignored.jsonl"
                before = log.read_text().splitlines() if log.exists() else []
                with (patch("sys.stdin", io.StringIO(json.dumps(hook))),
                      open(os.devnull, "w") as discarded, contextlib.redirect_stderr(discarded)):
                    self.assertEqual(turn_end.main(["--handle-hook"]), 0)
                after = log.read_text().splitlines() if log.exists() else []
                self.assertEqual(len(after), len(before) + 1)
                entry = cast(dict[str, str], json.loads(after[-1]))
                self.assertEqual(entry["worker_name"], row.name)
                self.assertEqual(entry["thread_id"], row.current_session)
                self.assertIn(str(late_reply), entry["reason"])
            else:
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(turn_end.handle(turn_end.hook_event(hook)), turn_end.TurnOutcome.NO_DONE)
                if archive_error:
                    lines = errors.getvalue().splitlines()
                    self.assertEqual(len(lines), 1)
                    self.assertIn(str(late_reply), lines[0])
                else:
                    self.assertEqual(late_reply.read_text().rstrip(), result)
            turn_reply = new_reply.with_stem(new_reply.stem + "_turn")
            self.assertEqual(turn_reply.read_text().rstrip(), replacement_reply)
            self.assertEqual(len(agent.notices), 2)
            self.assertIn(f"phase 40: no DONE; reply {turn_reply}", agent.notices[-1])
            self.assertIn(f"pass {replacement}", agent.notices[-1])
            return
        if later_user_message:
            agent.append(pane, "user", "Continue the replacement pass.")
        with patch.object(worker, "save_reply", wraps=worker.save_reply) as save:
            self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.STOPPED)
            if later_user_message:
                with patch.object(worker, "dispatch_receipt", wraps=worker.dispatch_receipt) as read_receipt:
                    _ = turn_end.handle(event)
                self.assertFalse(any(isinstance(call.kwargs.get("pane_pass"), progress_history.StoppedPassReceipt)
                                     for call in read_receipt.call_args_list))
                save.assert_called_once_with(late_reply, result)
        self.assertEqual(late_reply.read_text().rstrip(), result)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir), current_pass)
        retained = progress_history.hook_completion(progress_history.pass_for_finalization(self.run_dir, dispatch))
        assert isinstance(retained, progress_history.HookCompletion)
        self.assertTrue(retained.finished)
        self.assertFalse(reply.exists())
        self.assertFalse(new_reply.exists())
        self.assertEqual(len(agent.notices), 1)
        self.assertIn("phase 40: stopped", agent.notices[0])
        self.assertIn(pane, self.fixture.world.locations)

    def test_stop_hook_skips_another_workers_unreadable_dispatch(self) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        agent.pane_commands(self)
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        delivery = pass_recorder.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", delivery.line)
        result = "The work needs more time."
        agent.append(pane, "assistant", result)

        other = self.fixture.worker("other", family="codex")
        other_run = self.fixture.root / "other-dispatch" / "run"
        state = other_run / unit_runs.STATE_FILE
        write_text_atomic(state, json.dumps({
            "owner_staff_id": self.fixture.parent.id,
            "history_file": str(other_run / "history.jsonl"),
            "open_phases": [{"id": "40", "instance_id": "other-phase", "status": "active"}],
        }))
        prompt = reply.with_name("other-assignment.txt")
        write_text_atomic(prompt, "Run the other private command.")
        _ = progress_history.begin_dispatch(other_run, slot=other.name, phase="40", role="impl",
            worker_name=other.name, worker_id=other.id, launcher_command=["other-launcher"],
            prompt=prompt, kind="impl", lens="")
        write_text_atomic(state, "Invalid dispatch state.")

        fallback = self.fixture.root / "fallback" / "run"
        archive = fallback / unit_runs.STATE_FILE
        archive.mkdir(parents=True)
        hook = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(row.current_session),
                                    "last_assistant_message": result})
        with (patch.object(worker_panes, "worker_fallback_run", return_value=fallback),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(turn_end.handle(hook), turn_end.TurnOutcome.NO_DONE)
        lines = errors.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn(str(archive), lines[0])
        self.assertNotIn(str(state), lines[0])
        self.assertEqual(reply.with_stem("reply_turn").read_text().rstrip(), result)
        self.assertEqual(len(agent.notices), 1)
        self.assertIn("phase 40: no DONE", agent.notices[0])
        self.assertIn(f"pass {dispatch}", agent.notices[0])

    def test_supervisor_waits_for_the_stopped_group_after_the_leader_exits(self) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        agent.pane_commands(self)
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        record = self.record(row)
        record.update(pid=DEAD_PID, command_group=DEAD_PID, command_started="private-start",
                      pane_dispatch_id=dispatch)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        with (patch.object(detached_run_process, "supervisor_match", return_value=True),
              patch.object(process_start, "process_start", return_value=process_start.Started("private-start")),
              patch.object(detached_runs, "_signal_stopped_command"),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertTrue(self.record_confirmed_group_stop(path, "Private stop."))

        group_live = True

        def group_is_live(_group: int, **_options: object) -> bool:
            return group_live

        def release_group(_seconds: float) -> None:
            nonlocal group_live
            self.assertTrue(group_live)
            pending = cast(detached_runs.RunRecord, json.loads(path.read_text()))
            self.assertEqual((pending["status"], pending.get("exit_code")), ("running", 143))
            self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.ActiveDispatch)
            self.assertEqual(agent.notices, [])
            deliver.assert_not_called()
            group_live = False

        with (patch.object(detached_runs, "_run", return_value=143),
              patch.object(detached_runs, "scope_prefix", return_value=[]),
              patch.object(detached_runs, "_group_is_live", side_effect=group_is_live),
              patch.object(time, "sleep", side_effect=release_group),
              patch.object(detached_runs, "deliver_in_fresh_process", side_effect=deliver_in_fixture) as deliver):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
            deliver.assert_called_once_with(path)
        self.assertFalse(group_live)
        self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.NoActiveDispatch)
        self.assertEqual(json.loads(path.read_text())["status"], "finished")
        self.assertEqual(len(agent.notices), 1)
        self.assertIn("phase 40: stopped", agent.notices[0])

    def test_supervisor_recovers_a_stopped_pass_when_fresh_delivery_fails(self) -> None:
        for delivery_exit, retry_cleanup in ((0, False), (1, False), (1, True)):
            with self.subTest(delivery_exit=delivery_exit, retry_cleanup=retry_cleanup):
                self.notices.clear()
                row = self.fixture.worker("impl", family="codex")
                self.fixture.world.add(row, "w1:p1", family="codex")
                dispatch, _ = self.begin_pane_pass(row)
                path = self.create_stopped_run(row, dispatch)
                actual_stop = turn_end.stop_pass

                def stop_with_cleanup_retry(
                    run: Path, record: detached_runs.RunRecord,
                    guard: Callable[[], contextlib.AbstractContextManager[worker_panes.CloseAllowed | worker_panes.Kept]],
                ) -> worker_panes.StoppedRunPassCompletion | worker_panes.Kept | worker_panes.NoOwningPanePass:
                    completion = actual_stop(run, record, guard)
                    if not retry_cleanup or not isinstance(completion, worker_panes.StoppedRunPassCompletion):
                        return completion

                    def send_and_retry() -> worker_panes.StoppedPassCleanup:
                        _ = completion.send_notice()
                        return worker_panes.StoppedPassCleanup.RETRY

                    return replace(completion, send_notice=send_and_retry)

                with (patch.object(detached_runs, "_run", return_value=143),
                      patch.object(detached_runs, "scope_prefix", return_value=[]),
                      patch.object(detached_runs, "_group_is_live", return_value=False),
                      patch.object(turn_end, "stop_pass", side_effect=stop_with_cleanup_retry),
                      patch.object(detached_runs, "deliver_in_fresh_process", return_value=delivery_exit) as deliver):
                    self.assertEqual(detached_runs.main(["supervise", str(path)]), int(retry_cleanup))
                    deliver.assert_called_once_with(path)
                if retry_cleanup:
                    pending = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                    self.assertTrue(pending.get("completion_pending"))
                    self.assertFalse(pending.get("delivery"))
                    self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
                record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                self.assertEqual((record["status"], record.get("completion_pending")), ("finished", False))
                self.assertEqual(record.get("delivery"), "finished: the pane pass stopped")
                self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
                self.assertEqual(len(self.notices), 1)

    def test_deliver_keeps_done_pending_until_the_stopped_group_ends(self) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        agent.pane_commands(self)
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        delivery = pass_recorder.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", delivery.line)
        result = f"The work is complete.\nDONE {dispatch}"
        agent.append(pane, "assistant", result)
        self.fixture.world.locations[pane]["agent_status"] = "done"
        record = self.record(row)
        record.update(pid=DEAD_PID, command_group=DEAD_PID, command_started="private-start", exit_code=143,
                      pane_dispatch_id=dispatch)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        with (patch.object(detached_run_process, "supervisor_match", return_value=True),
              patch.object(process_start, "process_start", return_value=process_start.Started("private-start")),
              patch.object(detached_runs, "_signal_stopped_command"),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertTrue(self.record_confirmed_group_stop(path, "Private stop."))
        event = turn_end.WorkerEvent(str(row.current_session), reply=result)
        with patch.object(detached_run_process, "supervises", return_value=False):
            with (patch.object(detached_runs, "_group_is_live", return_value=True),
                  contextlib.redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(detached_runs.main(["deliver", str(path)]), 1)
                self.assertIn("the command group has no confirmed end", errors.getvalue())
                pending = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                self.assertEqual(pending["status"], "running")
                self.assertNotIn("finished_at", pending)
                identities = frozenset((row.id,))
                self.assertEqual(detached_runs.live_runs(path.parent, row.name), [pending])
                self.assertEqual(detached_runs.live_runs_for_staff(path.parent, identities), [pending])
                self.assertEqual(detached_runs.live_runs_in_folders({self.run_dir: identities}), [pending])
                self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.WAITING_ON_RUNS)
                self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.ActiveDispatch)
                self.assertFalse(reply.exists())
                self.assertEqual(len(agent.notices), 1)
                self.assertIn("phase 40: waiting on runs", agent.notices[0])
                self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            with patch.object(detached_runs, "_group_is_live", return_value=False):
                pending["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                write_text_atomic(path, json.dumps(pending))
                agent.append(pane, "assistant", result)
                self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [pending["run_id"]])
            self.assertEqual(detached_runs.live_runs_in_folders({self.run_dir: identities}), [])
        completion = progress_history.hook_completion(worker_panes.worker_pass(row, self.run_dir))
        assert isinstance(completion, progress_history.HookCompletion)
        self.assertEqual(completion.outcome, "done")
        self.assertEqual(reply.read_text().rstrip(), result)
        self.assertEqual(len(agent.notices), 2)
        self.assertIn("phase 40: done", agent.notices[1])

    def test_supervisor_recovers_a_pane_stop_staged_by_an_interrupted_stopper(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        record = self.record(row)
        record.update(pid=DEAD_PID, command_group=DEAD_PID, command_started="private-start",
                      pane_dispatch_id=dispatch)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        reason = "Interrupted pane stop."
        write_text_atomic(path.with_suffix(".stop-staged"),
                          json.dumps({**record, "stop_pass_requested": True, "stop_reason": reason}))
        path.with_suffix(detached_runs.PENDING_STOP_SUFFIX).touch()

        with (patch.object(detached_runs, "_run", return_value=143) as run,
              patch.object(detached_runs, "scope_prefix", return_value=["fixture-scope"]),
              patch.object(detached_runs, "_group_is_live", return_value=False)):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        self.assertEqual(run.call_count, 1)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("stop_reason"), reason)
        self.assertIs(saved.get("stop_pass_requested"), True)
        self.assertFalse(path.with_suffix(".stop-staged").exists())
        self.assertFalse(path.with_suffix(detached_runs.PENDING_STOP_SUFFIX).exists())
        self.assertEqual(len(self.notices), 1)
        self.assertIn("phase 40: stopped", self.notices[0][1])

    def test_a_failed_record_rename_preserves_the_latest_confirmed_pane_stop(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        path = Path(self.record(row)["log"]).with_suffix(".json")
        replace = os.replace

        def fail_record_rename(source: Path, target: Path) -> None:
            if target == path:
                raise OSError("fixture record rename failure")
            replace(source, target)

        def verify_group() -> detached_runs.CommandGroup:
            return detached_runs.CommandGroup(DEAD_PID, "private-start")

        with (patch.object(os, "replace", side_effect=fail_record_rename),
              patch.object(process_group, "send_process_group") as send_group,
              contextlib.redirect_stderr(io.StringIO())):
            self.assertTrue(detached_runs.stop_with_reason(
                path, "Build hold.", verify_group=verify_group, signal_group=process_group.send_process_group))
            self.assertTrue(detached_runs.stop_with_reason(
                path, "Pane stop.", verify_group=verify_group, signal_group=process_group.send_process_group,
                stop_pass=True))
        self.assertEqual(send_group.call_count, 2)
        confirmed = worker_lifecycle.read_json_object(path.with_suffix(".stop-confirmed"))
        self.assertEqual(confirmed.get("stop_reason"), "Pane stop.")
        self.assertIs(confirmed.get("stop_pass_requested"), True)

    def test_reap_recovers_a_staged_self_stop_without_canceling_the_pass(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        record = self.record(row)
        record.update(pid=DEAD_PID, command_group=DEAD_PID, command_started="private-start",
                      pane_dispatch_id=dispatch, exit_code=143)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        reason = "The check is obsolete."
        staged = {**record, "stop_pass_requested": True, "stop_reason": reason, "self_stop": True}
        write_text_atomic(path.with_suffix(".stop-staged"), json.dumps(staged))
        path.with_suffix(detached_runs.PENDING_STOP_SUFFIX).touch()

        with patch.object(detached_runs, "_group_is_live", return_value=False):
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [path.stem])
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual((saved.get("status"), saved.get("self_stop")), ("finished", True))
        self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir),
                              progress_history.ActiveDispatch)
        (pane, text), = self.inputs
        self.assertEqual(pane, "w1:p1")
        self.assertIn("Result: STOPPED (exit 143)", text)
        self.assertIn(f"Stop reason: {reason}", text)
        self.assertEqual(self.notices, [])

    def test_confirmed_stop_keeps_the_pane_request_after_a_failed_record_rename(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        record = self.record(row)
        record.update(pid=DEAD_PID, command_group=DEAD_PID, command_started="private-start",
                      pane_dispatch_id=dispatch, tree_key="private-tree")
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        saved = path.read_bytes()
        replace = os.replace

        def fail_record_rename(source: Path, target: Path) -> None:
            if target == path:
                raise OSError("fixture record rename failure")
            replace(source, target)

        reason = "Private stop."
        with (patch.object(os, "replace", side_effect=fail_record_rename),
              patch.object(detached_run_process, "supervisor_match", return_value=True),
              patch.object(process_start, "process_start", return_value=process_start.Started("private-start")),
              patch.object(detached_runs, "_signal_stopped_command") as send_group,
              contextlib.redirect_stderr(io.StringIO())):
            self.assertTrue(self.record_confirmed_group_stop(
                path, reason, signal_group=send_group))
        send_group.assert_called_once_with(detached_runs.CommandGroup(DEAD_PID, "private-start"), signal.SIGTERM)
        self.assertEqual(path.read_bytes(), saved)

        identities = frozenset({row.id})
        pending = {**record, "stop_pass_requested": True, "stop_reason": reason}
        with (patch.object(detached_run_process, "supervises", return_value=False),
              patch.object(detached_run_process, "supervisor_records", return_value={})):
            with patch.object(detached_runs, "_group_is_live", return_value=True):
                self.assertEqual(detached_runs.live_twin(path.parent, row.id, ["true"], "private-tree"), pending)
                self.assertEqual(detached_runs.live_runs(path.parent, row.name), [pending])
                self.assertEqual(detached_runs.live_runs_in_folders({self.run_dir: identities}), [pending])
                self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            with patch.object(detached_runs, "_group_is_live", return_value=False):
                self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [path.stem])
        self.assertFalse(path.with_suffix(detached_runs.PENDING_STOP_SUFFIX).exists())
        self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.NoActiveDispatch)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("phase 40: stopped", self.notices[0][1])

    def test_stop_keeps_pass_with_another_runs_live_group(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        path = self.create_stopped_run(row, dispatch)
        other = self.record(row, "orphan-with-live-group")
        group = DEAD_PID + 1
        other.update(pid=group, command_group=group, pane_dispatch_id=dispatch)
        other_path = Path(other["log"]).with_suffix(".json")
        write_text_atomic(other_path, json.dumps(other))

        def group_is_live(value: int, **_options: object) -> bool:
            return value == group

        with (patch.object(detached_run_process, "supervises", return_value=False),
              patch.object(detached_runs, "_group_is_live", side_effect=group_is_live),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        self.assertIn(f"another detached run can still be live: {other['run_id']}", errors.getvalue())
        self.assertIsInstance(progress_history.active_dispatch(row.id, self.run_dir), progress_history.ActiveDispatch)
        self.assertEqual(json.loads(other_path.read_text())["status"], "running")

    def test_unavailable_pass_lookup_does_not_allow_legacy_stop(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        reason = "The fixture pass lookup failed."
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session}),
              patch.object(progress_history, "active_dispatch", return_value=progress_history.ActiveDispatchUnavailable(reason)),
              patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=DEAD_PID)),
              patch.object(treekey, "tree_key", return_value="private-tree"),
              contextlib.redirect_stdout(io.StringIO())):
            self.assertEqual(detached_runs.main(["start", "--", "true"]), 0)
        runs = self.run_dir / detached_runs.DETACHED_RUNS_DIR
        path, = runs.glob("*.json")
        record = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(record.get("pane_dispatch_id"), {"unavailable": reason})
        record.update(stop_pass_requested=True, stop_reason="Private stop.", exit_code=143, command_group=DEAD_PID)
        write_text_atomic(path, json.dumps(record))
        # Round 18 rule: unknown ownership stays pending until restore or the director acts.
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 1)
        saved = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(saved.get("delivery"), "deferred: the result's pass ownership is unknown")
        self.assertIn(reason, errors.getvalue())
        active = progress_history.active_dispatch(row.id, self.run_dir)
        assert isinstance(active, progress_history.ActiveDispatch)
        self.assertEqual(active.instance_id, dispatch)

    def test_stopped_notice_survives_replacement_and_retries_after_failure(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        dispatch, reply = self.begin_pane_pass(row)
        path = self.create_stopped_run(row, dispatch)
        commit = progress_history.stop_dispatch
        replacement = ""

        def replace_after_commit(run: Path, slot: str, instance_id: str, owner: staff.StaffRow, director: staff.StaffId,
                                 *, run_id: str, reason: str,
                                 ) -> progress_history.StoppedPassNotice | progress_history.StopDispatchRefused:
            nonlocal replacement
            notice = commit(run, slot, instance_id, owner, director, run_id=run_id, reason=reason)
            replacement = progress_history.begin_dispatch(
                run, slot=slot, phase="40", role="impl", worker_name=row.name, worker_id=row.id,
                launcher_command=["new-launcher"], prompt=reply.with_name("assignment.txt"), kind="impl", lens="")
            return notice

        with (patch.object(detached_runs, "_group_is_live", return_value=False),
              patch.object(progress_history, "stop_dispatch", side_effect=replace_after_commit),
              patch.object(send, "_send", return_value=send.Result("failed", "Director unavailable.")) as sender,
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 1)
            sender.assert_called_once()
        self.assertIn("Director unavailable.", errors.getvalue())
        pending = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertTrue(pending.get("completion_pending"))
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        self.assertEqual(len(self.notices), 1)
        self.assertIn(f"Pass {dispatch}.", self.notices[0][1])
        active = progress_history.active_dispatch(row.id, self.run_dir)
        assert isinstance(active, progress_history.ActiveDispatch)
        self.assertEqual(active.instance_id, replacement)
        self.assertFalse(cast(detached_runs.RunRecord, json.loads(path.read_text())).get("completion_pending"))

    def test_stop_and_phase_finish_keep_the_pass_outcome_and_status_together(self) -> None:
        row = self.fixture.worker("impl", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        self.fixture.world.locations[pane]["agent_status"] = "working"
        agent = test_worker.DispatchAgent(self.fixture)
        commit = progress_history.stop_dispatch
        state_path = self.run_dir / unit_runs.STATE_FILE
        cases = ((True, False, ""), (False, False, ""), (True, True, ""),
                 (True, False, "before delivery"), (True, False, "before reap"))
        for phase_first, retry, done_at in cases:
            with self.subTest(phase_first=phase_first, retry=retry, done_at=done_at):
                self.inputs.clear()
                self.notices.clear()
                dispatch, reply = self.begin_pane_pass(row)
                stopped = self.create_stopped_run(row, dispatch)
                done = f"The check ended.\nDONE {dispatch}"
                if done_at:
                    delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
                    agent.append(pane, "user", delivery.line)
                writes: list[tuple[object, progress_history.HookCompletion | progress_history.NoHookCompletion]] = []

                def observe_write(path: Path, contents: str) -> None:
                    if path == state_path:
                        saved = cast(dict[str, object], json.loads(contents))
                        slots = cast(dict[str, dict[str, object]], saved.get("pass", {}))
                        passed = slots[row.name]
                        writes.append((passed.get("status"), progress_history.hook_completion(passed)))
                        for lock in (self.run_dir / ".progress_history.lock", stopped.parent / ".start.lock"):
                            with lock.open("a") as other, self.assertRaises(BlockingIOError):
                                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    write_text_atomic(path, contents)

                def finish_phase() -> None:
                    arguments = ["progress_history.py", "finish-phase", "--session-dir", str(self.run_dir),
                                 "--status", "completed"]
                    with patch.object(progress_history, "_close_interrupted_workers"), patch.object(sys, "argv", arguments):
                        progress_history.main()

                def race_phase_finish(run: Path, slot: str, instance_id: str, owner: staff.StaffRow, director: staff.StaffId,
                                      *, run_id: str, reason: str,
                                      ) -> progress_history.StoppedPassNotice | progress_history.StopDispatchRefused:
                    if phase_first:
                        finish_phase()
                    if done_at == "before delivery":
                        agent.append(pane, "assistant", done)
                    with patch.object(pass_recorder, "write_text_atomic", side_effect=observe_write):
                        return commit(run, slot, instance_id, owner, director, run_id=run_id, reason=reason)

                with (patch.object(detached_runs, "_group_is_live", return_value=False),
                      patch.object(progress_history, "stop_dispatch", side_effect=race_phase_finish),
                      contextlib.redirect_stderr(io.StringIO())):
                    if retry:
                        refusal = send.RefusedRelayRequest("failed", "Director unavailable.")
                        with patch.object(send, "_send", return_value=refusal):
                            self.assertEqual(detached_runs.main(["deliver", str(stopped)]), 1)
                        self.assertTrue(worker_lifecycle.read_json_object(stopped)["completion_pending"])
                    if done_at == "before reap":
                        with (patch.object(detached_runs, "_clear_completion_pending",
                                           side_effect=SystemExit("interrupted after result delivery")),
                              self.assertRaisesRegex(SystemExit, "interrupted after result delivery")):
                            _ = detached_runs.main(["deliver", str(stopped)])
                        saved = worker_lifecycle.read_json_object(stopped)
                        self.assertTrue(saved["completion_pending"])
                        self.assertTrue(str(saved["delivery"]).startswith(detached_runs.INTERRUPTED_PASS_DELIVERY))
                        agent.append(pane, "assistant", done)
                        self.fixture.world.locations[pane]["agent_status"] = "done"
                        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                    else:
                        self.assertEqual(detached_runs.main(["deliver", str(stopped)]), 0)
                        if phase_first:
                            self.assertEqual(detached_runs.main(["deliver", str(stopped)]), 0)
                self.assertEqual(self.inputs, [])
                if not phase_first:
                    finish_phase()
                passed = worker_panes.worker_pass(row, self.run_dir)
                self.assertEqual(passed.get("status"), "interrupted" if phase_first else "canceled")
                completion = progress_history.hook_completion(passed)
                if phase_first:
                    self.assertEqual(writes, [])
                    saved = worker_lifecycle.read_json_object(stopped)
                    self.assertFalse(saved["completion_pending"])
                    if done_at == "before delivery":
                        self.assertIs(completion, progress_history.NoHookCompletion.UNRECORDED)
                        self.assertEqual(saved["delivery"], detached_runs.COVERED_BY_DONE)
                        self.assertEqual(self.notices, [])
                        continue
                    if done_at == "before reap":
                        self.assertIsInstance(completion, progress_history.HookCompletion)
                        assert isinstance(completion, progress_history.HookCompletion)
                        self.assertEqual(completion.outcome, turn_end.TurnOutcome.LATE_DONE)
                        late = reply.with_name(f"reply_late_{dispatch}.txt")
                        self.assertEqual(late.read_text().rstrip(), done)
                        self.assertEqual(len(self.notices), 2)
                        self.assertIn("phase 40: late DONE", self.notices[1][1])
                    else:
                        self.assertIs(completion, progress_history.NoHookCompletion.UNRECORDED)
                        self.assertEqual(len(self.notices), 1)
                    target, message = self.notices[0]
                    self.assertEqual(target, f"session:{self.fixture.parent.current_session}")
                    self.assertIn(f"Detached run: {stopped.stem}", message)
                    self.assertIn("Private stop.", message)
                else:
                    assert isinstance(completion, progress_history.HookCompletion)
                    self.assertEqual(completion.outcome, "stopped")
                    self.assertEqual(writes, [("canceled", completion)])

    def test_native_caller_ignores_inherited_session_and_run(self) -> None:
        row = self.fixture.worker(family="claude")
        sibling = self.fixture.worker("sibling")
        registry = self.fixture.session_record(row.current_session, row.name)
        os.rename(registry, registry.with_name(f"{os.getpid()}.json"))
        stale_run = self.fixture.root / "stale-run"
        stale_run.mkdir()
        for inherited in ({"CLAUDE_CODE_SESSION_ID": sibling.current_session,
                           "UNIT_RUN_DIR": str(stale_run)}, {}):
            with self.subTest(inherited=bool(inherited)), patch.dict(os.environ, {
                    "CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": inherited.get("CLAUDE_CODE_SESSION_ID", ""),
                    "UNIT_RUN_DIR": str(stale_run)}):
                caller = detached_runs.caller_from_environment(inherited)
                self.assertIsInstance(caller, detached_runs.PaneCaller)
                assert isinstance(caller, detached_runs.PaneCaller)
                self.assertEqual((caller.staff_id, caller.session_dir), (row.id, str(self.run_dir)))

    def test_orphan_with_a_stale_reap_claim_delivers_the_saved_exit(self) -> None:
        row = self.fixture.worker()
        record = self.record(row, "dead-reaper-claim")
        record.update(pid=DEAD_PID, exit_code=9)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        path.with_suffix(".reap").touch()

        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [record["run_id"]])
        finished = worker_lifecycle.read_json_object(path)
        self.assertEqual((finished["status"], finished["exit_code"]), ("finished", 9))
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(len(self.notices), 1)
        self.assertIn("FAIL (exit 9)", self.notices[0][1])

    def test_refused_result_push_finishes_and_can_replay_the_saved_exit(self) -> None:
        row = self.fixture.worker(family="codex")
        record = self.record(row)
        dispatch, _ = self.begin_pane_pass(row)
        record.update(pid=DEAD_PID, tree_key="fixture-tree", pane_dispatch_id=dispatch)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))

        refusal = send.RefusedRelayRequest("failed", "the relay refused the fixture request")
        with (patch.object(send, "_send", return_value=refusal),
              patch.object(send, "send", return_value=send.Result("sent", "director received the alert")),
              patch.object(time, "sleep"),
              patch.object(detached_runs, "_run", return_value=9)):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 1)
        pending = worker_lifecycle.read_json_object(path)
        self.assertEqual((pending["status"], pending["exit_code"]), ("finished", 9))
        self.assertTrue(pending["finished_at"])
        self.assertEqual(pending["delivery"], detached_runs.FAILED_RESULT_PUSH + "failed: " + refusal.detail)
        self.assertNotIn("delivered_at", pending)
        with patch.object(detached_run_process, "supervises", return_value=True):
            self.assertEqual(detached_runs.live_runs(path.parent, row.name), [])
            self.assertIsNone(detached_runs.live_twin(path.parent, row.id, record["command"], "fixture-tree"))
        receipts, other_passes = detached_runs.worker_results(path.parent, row, since=0, pane_dispatch_id=dispatch)
        self.assertEqual(set(receipts), {record["run_id"]})
        self.assertEqual(other_passes, set())
        self.assertIs(receipts[record["run_id"]].delivered_at, detached_runs.WorkerResultDeferred.PENDING)

        self.fixture.world.add(row, "w1:p1", family="codex")
        self.assertIsInstance(detached_runs.submit_finished_worker_result(self.run_dir, row,
            worker_input=detached_runs.WorkerResultInput.TYPE), detached_runs.ConfirmedPaneResult)
        (_, text), = self.inputs
        self.assertIn("FAIL (exit 9)", text)
        self.assertEqual(worker_lifecycle.read_json_object(path)["delivery"], f"sent: worker {row.name} at w1:p1")

    def test_exhausted_result_push_alerts_the_director_once(self) -> None:
        row = self.fixture.worker()
        for outcome, response, retries, seconds in (
            ("sent", send.Result("sent", "fixture alert result"), 3, 5),
            ("failed", send.Result("failed", "fixture alert result"), 3, 5),
            ("sender-exit", SystemExit(2), 1, 0.25),
        ):
            with self.subTest(outcome=outcome):
                _ = write_config_fixture(
                    self.fixture.root / "conf/detached_runs.conf",
                    f"result_push_retries={retries}\nresult_push_retry_seconds={seconds}\n",
                )
                record = self.record(row, f"alert-{outcome}")
                path = Path(record["log"]).with_suffix(".json")
                refusal = send.RefusedRelayRequest("failed", "fixture push error")
                deferred = send.Result("retry", "worker is working")
                with (patch.object(send, "deliver_worker_text", side_effect=[refusal, deferred, refusal, deferred]) as deliver,
                      patch.object(send, "send", side_effect=[response]) as alert,
                      patch.object(time, "sleep") as delay,
                      patch.object(detached_runs, "_run", return_value=7),
                      contextlib.redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(detached_runs.main(["supervise", str(path)]), 1)
                    self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                self.assertEqual(deliver.call_count, 1 + retries)
                self.assertEqual(delay.call_args_list, [call(seconds)] * retries)
                alert.assert_called_once()
                options = cast(send.Options, alert.call_args.args[0])
                self.assertEqual(options.to, self.fixture.parent.id)
                self.assertIn("Result delivery failed.", options.text or "")
                for detail in (path.stem, record["label"], row.name, "exit 7", refusal.detail):
                    self.assertIn(detail, options.text or "")
                saved = worker_lifecycle.read_json_object(path)
                self.assertIs(saved["push_alert_attempted"], True)
                self.assertEqual(saved["push_retries"], retries)
                self.assertEqual(saved["delivery"], detached_runs.FAILED_RESULT_PUSH + "failed: " + refusal.detail)
                self.assertEqual(errors.getvalue().count("anomaly:"), int(outcome != "sent"))
                if outcome == "sender-exit":
                    self.assertIn("could not alert the director: 2", errors.getvalue())

    def test_alert_reports_one_anomaly_when_the_director_lookup_fails(self) -> None:
        row = self.fixture.worker()
        record = self.record(row, "alert-owner-unreadable")
        path = Path(record["log"]).with_suffix(".json")
        record.update(status="finished", exit_code=7, delivery=detached_runs.FAILED_RESULT_PUSH + "fixture refusal")
        write_text_atomic(path, json.dumps(record))
        self.assertIsNotNone(staff.retire(row.id))
        state_path = self.run_dir / unit_runs.STATE_FILE
        write_text_atomic(state_path, "{")

        with (patch.object(send, "send") as alert, contextlib.redirect_stderr(io.StringIO()) as errors):
            detached_runs._alert_failed_push(path)  # pyright: ignore[reportPrivateUsage]
            detached_runs._alert_failed_push(path)  # pyright: ignore[reportPrivateUsage]
        alert.assert_not_called()
        self.assertEqual(errors.getvalue().count("anomaly:"), 1)
        self.assertIn(str(state_path), errors.getvalue())
        saved = worker_lifecycle.read_json_object(path)
        self.assertIs(saved["push_alert_attempted"], True)
        self.assertEqual(saved["delivery"], record.get("delivery"))
        self.assertNotIn("delivered_at", saved)

    def test_result_delivery_matrix(self) -> None:
        all_owners = ("worker", "director", "retired", "self-stop")
        relay_owners = ("worker", "director", "retired")
        remote_owners = ("director", "retired")
        pane_owners = ("worker", "self-stop")
        mismatch = "SendMessage used a different target or payload"
        cases = (
            ("local-sent", all_owners, send.Result("sent", "received"), "sent"),
            ("local-refused", relay_owners, send.RefusedRelayRequest("failed", "fixture refusal"), "retry"),
            ("local-relay-failed", relay_owners, send.Result("failed", mismatch), "receipt"),
            ("pane-launch-failed", pane_owners, send.WorkerResultNotSubmitted("retry", "fixture launch failed"), "retry"),
            ("remote-sent", remote_owners, (0, "SENT: received\n", ""), "sent"),
            ("remote-refused", remote_owners, (2, "REFUSED: fixture refusal\n", ""), "retry"),
            ("remote-relay-failed", remote_owners, (3, f"FAILED: {mismatch}\n", ""), "receipt"),
            ("lost-ssh-reply", remote_owners, (255, "", "connection lost"), "receipt"),
            ("staff-lookup-failed", all_owners, None, "retry"),
            ("pane-unconfirmed", all_owners, send.WorkerResultUnconfirmed("retry", "no receipt"), "receipt"),
        )
        read_staff = staff.authority_snapshot
        director = self.fixture.parent
        for name, owners, first, action in cases:
            for owner in owners:
                for recovered in (True, False) if action == "retry" else (False,):
                    with self.subTest(outcome=name, owner=owner, recovered=recovered):
                        row = director if owner == "director" else self.fixture.worker()
                        retired_after_input = owner == "retired" and name == "pane-unconfirmed"
                        record = self.record(row, f"matrix-{name}-{owner}-{recovered}")
                        record["pid"] = DEAD_PID
                        if owner in ("retired", "self-stop"):
                            record.update(self_stop=True, stop_reason="The check is obsolete.")
                        if owner == "retired" and not retired_after_input:
                            self.assertIsNotNone(staff.retire(row.id))
                        path = Path(record["log"]).with_suffix(".json")
                        write_text_atomic(path, json.dumps(record))
                        results: list[send.Result] = []
                        alerts: list[str] = []
                        receipt_checks: list[staff.StaffId] = []
                        lookup_failures = 0

                        def lookup_staff() -> staff.StaffSnapshot:
                            nonlocal lookup_failures
                            if first is None and lookup_failures < (1 if recovered else 4):
                                lookup_failures += 1
                                raise staff.StaffUnreadable("fixture staff lookup failed")
                            return read_staff()

                        def transport_result(options: send.Options | None = None) -> send.Result:
                            reply = first
                            if reply is not None and not isinstance(reply, send.Result):
                                assert options is not None
                                message = send.Message(options.to, "detached-runs", name, options.text or "", None)
                                response = (0, "SENT: received\n", "") if results and recovered else reply
                                with patch.object(send, "run", return_value=response):
                                    reply = send.remote(message, "fixture-host", options)
                            elif reply is None or (results and recovered):
                                reply = send.Result("sent", "received")
                            elif isinstance(reply, send.WorkerResultUnconfirmed) and options is not None:
                                reply = send.WorkerMessageUnconfirmed("failed", reply.detail)
                            results.append(reply)
                            return reply

                        def send_result(options: send.Options, *, membership: staff.StaffSnapshot) -> send.Result:
                            del membership
                            self.assertEqual(options.to, director.id)
                            body = options.text or ""
                            if body.startswith(f"Detached run: {path.stem}\n"):
                                self.assertIn(owner, remote_owners)
                                return transport_result(options)
                            alerts.append(body)
                            return send.Result("sent", "alert received")

                        def send_to_worker(member: staff.StaffRow, _membership: staff.StaffSnapshot, _text: str, *,
                                           receipt: send.WorkerResultReceipt, **_options: object) -> send.Result:
                            self.assertEqual(member.id, row.id)
                            self.assertTrue(owner in pane_owners or retired_after_input)
                            self.assertEqual(receipt.worker_only, owner in ("self-stop", "retired"))
                            result = transport_result()
                            if retired_after_input:
                                self.assertIsNotNone(staff.retire(row.id))
                            return result

                        def check_receipt(member: staff.StaffRow, _membership: staff.StaffSnapshot, _marker: str) -> bool:
                            delivery = str(worker_lifecycle.read_json_object(path).get("delivery", ""))
                            if delivery.startswith(detached_runs.UNCONFIRMED_WORKER_RESULT):
                                receipt_checks.append(member.id)
                            return False

                        accepted = action == "sent" or recovered
                        with (patch.object(staff, "authority_snapshot", side_effect=lookup_staff),
                              patch.object(send, "_send", side_effect=send_result),
                              patch.object(send, "deliver_worker_text", side_effect=send_to_worker),
                              patch.object(detached_runs, "_result_received", side_effect=check_receipt),
                              patch.object(detached_runs, "_complete_worker_dispatch",
                                           return_value=detached_runs.CompletionReplay.HANDLED),
                              patch.object(detached_runs, "_run", return_value=7),
                              patch.object(time, "sleep") as delay):
                            self.assertEqual(detached_runs.main(["supervise", str(path)]), int(not accepted))
                            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])

                        attempts = 1 if action != "retry" else (2 if recovered else 4)
                        self.assertEqual(len(results) + lookup_failures, attempts)
                        self.assertEqual(sum(result.outcome == "sent" for result in results), int(accepted))
                        self.assertEqual(delay.call_args_list, [call(5)] * (0 if action == "sent" else
                                                                             1 if recovered else 3))
                        saved = worker_lifecycle.read_json_object(path)
                        self.assertEqual((saved["status"], saved["exit_code"]), ("finished", 7))
                        self.assertEqual(len(alerts), int(not accepted))
                        self.assertEqual("push_alert_attempted" in saved, not accepted)
                        self.assertEqual("delivered_at" in saved, accepted)
                        if action == "receipt":
                            targets = [director.id] if owner in remote_owners else [row.id]
                            if name == "local-relay-failed" and owner == "worker":
                                targets.append(director.id)
                            self.assertEqual(receipt_checks, targets * 3)
                            self.assertIn("The result may have arrived.", alerts[0])
                            if "relay-failed" in name:
                                self.assertIn(mismatch, alerts[0])
                            if name == "remote-relay-failed":
                                self.assertIn("on fixture-host:", alerts[0])
                        else:
                            self.assertEqual(receipt_checks, [])

    def test_ambiguous_result_push_checks_receipts_and_never_resends(self) -> None:
        director = self.fixture.parent
        cases = (
            ("timeout", send.Result("failed", "timed out after 1 s"), True),
            ("interrupted", TimeoutError("the transport started"), False),
        )
        for name, result, received in cases:
            with self.subTest(name=name):
                record = self.record(director, f"push-{name}")
                path = Path(record["log"]).with_suffix(".json")

                def send_result(options: send.Options) -> send.Result:
                    if (options.text or "").startswith("Detached run: "):
                        pending = worker_lifecycle.read_json_object(path)
                        self.assertTrue(str(pending["delivery"]).startswith(detached_runs.UNCONFIRMED_RESULT_PUSH))
                        if isinstance(result, TimeoutError):
                            raise result
                        return result
                    return send.Result("sent", "director received the alert")

                def first_delivery(record_path: Path) -> int:
                    try:
                        return deliver_in_fixture(record_path)
                    except TimeoutError:
                        return 1

                with (patch.object(send, "send", side_effect=send_result) as push,
                      patch.object(detached_runs, "_result_received", return_value=received) as receipt,
                      patch.object(detached_runs, "deliver_in_fresh_process", side_effect=first_delivery),
                      patch.object(detached_runs, "_run", return_value=7),
                      patch.object(time, "sleep") as delay):
                    self.assertEqual(detached_runs.main(["supervise", str(path)]), int(not received))
                    self.assertEqual(push.call_count, 1 if received else 2)
                    self.assertEqual(receipt.call_count, 1 if received else 3)
                    self.assertEqual(delay.call_args_list, [call(5)] * (1 if received else 3))
                    if not received:
                        alert = cast(send.Options, push.call_args.args[0])
                        self.assertIn("Result delivery is unconfirmed. The result may have arrived.", alert.text or "")
                        self.assertIn("the transport started", alert.text or "")
                saved = worker_lifecycle.read_json_object(path)
                if received:
                    self.assertEqual(saved["delivery"], f"already delivered: {path.stem}")
                else:
                    self.assertNotIn("delivered_at", saved)
                    self.assertIs(saved["push_alert_attempted"], True)
                    self.assertTrue(str(saved["delivery"]).startswith(detached_runs.UNCONFIRMED_RESULT_PUSH))
                    self.assertIn("the transport started", str(saved["delivery"]))

    def test_publish_finished_preserves_a_concurrent_delivery(self) -> None:
        publish = detached_runs._finish_record  # pyright: ignore[reportPrivateUsage]
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                record = self.record(self.fixture.parent, f"concurrent-delivery-{accepted}")
                path = Path(record["log"]).with_suffix(".json")
                refusal = send.RefusedRelayRequest("failed", "fixture refusal")
                concurrent = send.Result("sent", "received") if accepted else TimeoutError("reply lost")

                def publish_after_delivery(target: Path, stale: detached_runs.RunRecord) -> detached_runs.CompletionReplay:
                    self.assertEqual(stale.get("delivery"), detached_runs.FAILED_RESULT_PUSH + "failed: fixture refusal")
                    if accepted:
                        self.assertEqual(detached_runs.deliver(stale, detached_runs.CompletedCommand(7)), "sent: received")
                    else:
                        with self.assertRaisesRegex(TimeoutError, "reply lost"):
                            _ = detached_runs.deliver(stale, detached_runs.CompletedCommand(7))
                    return publish(target, stale)

                with (patch.object(send, "send", side_effect=[
                          refusal, concurrent, send.Result("sent", "alert received")]) as push,
                      patch.object(detached_runs, "_finish_record", side_effect=publish_after_delivery),
                      patch.object(detached_runs, "_run", return_value=7),
                      patch.object(detached_runs, "_result_received", return_value=False) as receipt,
                      patch.object(time, "sleep") as delay):
                    self.assertEqual(detached_runs.main(["supervise", str(path)]), int(not accepted))

                saved = worker_lifecycle.read_json_object(path)
                self.assertEqual((saved["status"], saved["exit_code"]), ("finished", 7))
                messages = [cast(send.Options, attempt.args[0]).text or "" for attempt in push.call_args_list]
                self.assertEqual(sum(body.startswith(f"Detached run: {path.stem}\n") for body in messages), 2)
                self.assertEqual(len(messages), 2 if accepted else 3)
                if accepted:
                    self.assertEqual(saved["delivery"], "sent: received")
                    delay.assert_not_called()
                    receipt.assert_not_called()
                else:
                    self.assertTrue(str(saved["delivery"]).startswith(detached_runs.UNCONFIRMED_RESULT_PUSH))
                    self.assertIn("The result may have arrived.", messages[-1])
                    self.assertEqual(receipt.call_count, 3)

    def test_late_pane_delivery_alerts_once_without_resending(self) -> None:
        row = self.fixture.worker("late-pane", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        stage = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        test_worker.DispatchAgent(self.fixture).append(pane, "user", stage.line)
        record = self.record(row)
        record["pane_dispatch_id"] = dispatch
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        self.fixture.world.locations[pane]["agent_status"] = "working"

        with (patch.object(detached_runs, "_run", return_value=0),
              patch.object(detached_runs, "scope_prefix", return_value=[]),
              patch.object(time, "sleep") as retry_delay):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        retry_delay.assert_not_called()
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual((saved["status"], saved["exit_code"]), ("finished", 0))
        self.assertTrue(str(saved["delivery"]).startswith(detached_runs.DEFERRED_WORKER_RESULT))
        self.assertNotIn("push_retries", saved)
        self.assertEqual(self.inputs, [])

        self.fixture.world.locations[pane]["agent_status"] = "idle"
        self.defer_transcript = True
        self.assertEqual(turn_end.run_finished(row.id, dispatch), detached_runs.CompletionReplay.RETRY)
        saved = worker_lifecycle.read_json_object(path)
        self.assertTrue(str(saved["delivery"]).startswith(detached_runs.UNCONFIRMED_WORKER_RESULT))
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(len(self.notices), 1)
        (recipient, alert), = self.notices
        self.assertEqual(recipient, self.fixture.parent.id)
        self.assertIn(f"Detached run {record['run_id']}. Result delivery is unconfirmed.", alert)
        self.assertIn("exit 0", alert)
        self.assertIs(saved["push_alert_attempted"], True)

        _ = turn_end.run_finished(row.id, dispatch)
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(self.notices, [(recipient, alert)])

    def test_late_pane_delivery_interrupted_after_input_checkpoint_alerts(self) -> None:
        row = self.fixture.worker("late-interruption", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        stage = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        test_worker.DispatchAgent(self.fixture).append(pane, "user", stage.line)
        record = self.record(row)
        record.update(status="finished", exit_code=0, pane_dispatch_id=dispatch, pid=DEAD_PID,
                      delivery=detached_runs.DEFERRED_WORKER_RESULT + "worker is working", completion_pending=True)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        server = self.fixture.world.run
        attempts = 0

        def interrupt_prompt(*arguments: str, deadline: float = math.inf) -> str:
            nonlocal attempts
            if arguments[:3] == ("agent", "prompt", pane):
                attempts += 1
                self.assertEqual(worker_lifecycle.read_json_object(path)["delivery"], detached_runs.WORKER_INPUT_CHECKPOINT)
                raise KeyboardInterrupt
            return server(*arguments, deadline=deadline)

        with patch.object(self.fixture.world, "run", new=interrupt_prompt):
            with self.assertRaises(KeyboardInterrupt):
                _ = turn_end.run_finished(row.id, dispatch)
            saved = worker_lifecycle.read_json_object(path)
            self.assertEqual(saved["delivery"], detached_runs.WORKER_INPUT_CHECKPOINT)
            self.assertIs(saved["push_alert_attempted"], True)
            _ = turn_end.run_finished(row.id, dispatch)
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(attempts, 1)
        (recipient, alert), = self.notices
        self.assertEqual(recipient, self.fixture.parent.id)
        self.assertIn("Result delivery is unconfirmed.", alert)

    def test_late_result_error_before_pane_input_alerts_once(self) -> None:
        row = self.fixture.worker("late-read-error", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        stage = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        test_worker.DispatchAgent(self.fixture).append(pane, "user", stage.line)
        message = self.run_dir / "done-message.txt"
        _ = message.write_bytes(b"\xff")
        record = self.record(row)
        deferred = detached_runs.DEFERRED_WORKER_RESULT + "worker is working"
        record.update(status="finished", exit_code=0, pane_dispatch_id=dispatch, pid=DEAD_PID,
                      done_message_file=str(message), delivery=deferred, completion_pending=True)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))

        with patch.object(detached_run_process, "supervises", return_value=True):
            with self.assertRaises(UnicodeDecodeError):
                _ = turn_end.run_finished(row.id, dispatch)
            self.assertEqual(worker_lifecycle.read_json_object(path)["delivery"], deferred)
            self.assertEqual(self.notices, [])

        with self.assertRaises(UnicodeDecodeError):
            _ = turn_end.run_finished(row.id, dispatch)
        saved = worker_lifecycle.read_json_object(path)
        self.assertTrue(str(saved["delivery"]).startswith(detached_runs.FAILED_WORKER_SUBMISSION))
        self.assertIs(saved["push_alert_attempted"], True)
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(self.inputs, [])
        (recipient, alert), = self.notices
        self.assertEqual(recipient, self.fixture.parent.id)
        self.assertIn("Result delivery failed.", alert)
        self.assertIn("utf-8", alert)

    def test_late_pane_refusal_leaves_the_live_supervisor_its_retry(self) -> None:
        row = self.fixture.worker("late-refusal", family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        dispatch, _ = self.begin_pane_pass(row)
        stage = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        test_worker.DispatchAgent(self.fixture).append(pane, "user", stage.line)
        record = self.record(row)
        record["pane_dispatch_id"] = dispatch
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        self.fixture.world.locations[pane]["agent_status"] = "working"
        server = self.fixture.world.run
        attempts = 0

        def defer_before_completion(record_path: Path) -> int:
            code = deliver_in_fixture(record_path)
            saved = worker_lifecycle.read_json_object(record_path)
            self.assertTrue(str(saved["delivery"]).startswith(detached_runs.DEFERRED_WORKER_RESULT))
            self.fixture.world.locations[pane]["agent_status"] = "idle"
            return code

        def refuse_first_prompt(*arguments: str, deadline: float = math.inf) -> str:
            nonlocal attempts
            if arguments[:3] == ("agent", "prompt", pane):
                attempts += 1
                if attempts == 1:
                    raise herdr.HerdrCommandNotStarted("fixture refused the prompt")
            return server(*arguments, deadline=deadline)

        def before_retry(_delay: float) -> None:
            saved = worker_lifecycle.read_json_object(path)
            self.assertTrue(str(saved["delivery"]).startswith(detached_runs.FAILED_WORKER_SUBMISSION))
            self.assertNotIn("push_alert_attempted", saved)
            self.assertEqual(self.inputs, [])
            self.assertEqual(self.notices, [])

        with (patch.object(detached_runs, "_run", return_value=0),
              patch.object(detached_runs, "scope_prefix", return_value=[]),
              patch.object(detached_runs, "deliver_in_fresh_process", side_effect=defer_before_completion),
              patch.object(detached_run_process, "supervises", return_value=True) as live,
              patch.object(self.fixture.world, "run", side_effect=refuse_first_prompt),
              patch.object(time, "sleep", side_effect=before_retry) as delay):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        live.assert_called_once_with(os.getpid(), path)
        delay.assert_called_once_with(5)
        self.assertEqual(attempts, 2)
        self.assertEqual(len(self.inputs), 1)
        self.assertFalse(any(text.startswith(f"Detached run {record['run_id']}.") for _, text in self.notices))
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["delivery"], f"sent: worker {row.name} at {pane}")
        self.assertNotIn("push_alert_attempted", saved)

    def test_reaper_alerts_after_the_supervisor_exits_during_a_retry_delay(self) -> None:
        record = self.record(self.fixture.parent, "orphan-during-delay")
        path = Path(record["log"]).with_suffix(".json")
        refusal = send.RefusedRelayRequest("failed", "fixture refusal")

        def exit_during_delay(_delay: float) -> None:
            saved = worker_lifecycle.read_json_object(path)
            self.assertEqual(saved["status"], "finished")
            self.assertNotIn("ended", saved)
            with patch.object(detached_run_process, "supervises", return_value=True) as live:
                self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                live.assert_called_once()
            self.assertNotIn("push_alert_attempted", worker_lifecycle.read_json_object(path))
            raise SystemExit("supervisor exited during retry delay")

        with (patch.object(send, "send", side_effect=[refusal, send.Result("sent", "alert received")]) as push,
              patch.object(detached_runs, "_run", return_value=7),
              patch.object(time, "sleep", side_effect=exit_during_delay) as delay):
            with self.assertRaisesRegex(SystemExit, "supervisor exited during retry delay"):
                _ = detached_runs.main(["supervise", str(path)])
            with patch.object(detached_run_process, "supervises", return_value=False) as gone:
                self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                gone.assert_called_once()
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])

        delay.assert_called_once_with(5)
        self.assertEqual(push.call_count, 2)
        alert = cast(send.Options, push.call_args.args[0])
        self.assertEqual(alert.to, self.fixture.parent.id)
        self.assertIn("Result delivery failed.", alert.text or "")
        saved = worker_lifecycle.read_json_object(path)
        self.assertIs(saved["push_alert_attempted"], True)
        self.assertNotIn("delivered_at", saved)

    def test_reaper_recovers_an_interrupted_alert(self) -> None:
        record = self.record(self.fixture.parent, "interrupted-alert")
        record.update(status="finished", pid=DEAD_PID, exit_code=7,
                      delivery=detached_runs.FAILED_RESULT_PUSH + "fixture refusal")
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        attempts = 0

        def alert_director(options: send.Options) -> send.Result:
            nonlocal attempts
            self.assertEqual(options.to, self.fixture.parent.id)
            self.assertNotIn("push_alert_attempted", worker_lifecycle.read_json_object(path))
            with path.with_name(f".{path.stem}.delivery.lock").open("r") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            attempts += 1
            return send.Result("sent", "director received the alert")

        def save_alert(target: Path, text: str) -> None:
            if target == path and attempts == 1 and '"push_alert_attempted": true' in text:
                raise SystemExit("process exited before it saved the alert outcome")
            write_text_atomic(target, text)

        with (patch.object(send, "send", side_effect=alert_director),
              patch.object(detached_runs, "write_text_atomic", side_effect=save_alert),
              contextlib.redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertNotIn("push_alert_attempted", worker_lifecycle.read_json_object(path))
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertIs(worker_lifecycle.read_json_object(path)["push_alert_attempted"], True)
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertIn("process exited before it saved the alert outcome", errors.getvalue())
        self.assertEqual(attempts, 2)

    def test_reaper_confirms_an_unconfirmed_result_before_alerting(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        record = self.record(row, "receipt-before-alert")
        record.update(status="finished", pid=DEAD_PID, exit_code=7)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        self.interrupt_after_input = True

        with patch.object(send, "send") as alert:
            with self.assertRaisesRegex(SystemExit, "interrupted after pane input"):
                _ = detached_runs.deliver(record, detached_runs.CompletedCommand(7))
            pending = worker_lifecycle.read_json_object(path)
            self.assertTrue(str(pending["delivery"]).startswith(detached_runs.UNCONFIRMED_WORKER_RESULT))
            self.assertIn("interrupted after pane input", str(pending["delivery"]))
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        alert.assert_not_called()
        self.assertEqual(len(self.inputs), 1)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["delivery"], f"already delivered: {path.stem}")
        self.assertIn("delivered_at", saved)
        self.assertNotIn("push_alert_attempted", saved)

    def test_reap_saves_finished_before_a_staff_lookup_fails(self) -> None:
        row = self.fixture.worker(family="codex")
        record = self.record(row, "orphan-lookup")
        finished_at = "2026-11-19T13:00:00+00:00"
        record.update(pid=DEAD_PID, exit_code=9, finished_at=finished_at)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))

        def unreadable_staff() -> staff.StaffSnapshot:
            saved = worker_lifecycle.read_json_object(path)
            self.assertEqual((saved["status"], saved["exit_code"], saved["finished_at"]),
                             ("finished", 9, finished_at))
            self.assertTrue(str(saved["delivery"]).startswith(detached_runs.DEFERRED_WORKER_RESULT))
            raise staff.StaffUnreadable("fixture staff lookup failed")

        def lookup_staff() -> staff.StaffSnapshot:
            if worker_lifecycle.read_json_object(path)["delivery"] == detached_runs.ORPHAN_RESULT_PENDING:
                return unreadable_staff()
            return staff.local_snapshot()

        with (patch.object(staff, "snapshot", side_effect=lookup_staff) as lookup,
              patch.object(send, "send", return_value=send.Result("sent", "alert received")) as alert,
              patch.object(detached_runs, "_retry_failed_push") as retry):
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [path.stem])
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(lookup.call_count, 2)
        alert.assert_called_once()
        retry.assert_not_called()
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["status"], "finished")
        self.assertEqual(saved["delivery"], detached_runs.FAILED_RESULT_PUSH + "fixture staff lookup failed")
        self.fixture.world.add(row, "w1:p1", family="codex")
        self.assertIsInstance(detached_runs.submit_finished_worker_result(self.run_dir, row,
            worker_input=detached_runs.WorkerResultInput.TYPE), detached_runs.ConfirmedPaneResult)

    def test_reap_alerts_once_when_a_director_result_push_fails(self) -> None:
        for retired in (False, True):
            with self.subTest(retired=retired):
                row = self.fixture.worker() if retired else self.fixture.parent
                record = self.record(row, f"orphan-director-result-{retired}")
                record.update(pid=DEAD_PID, exit_code=7)
                if retired:
                    record.update(self_stop=True, stop_reason="The check is obsolete.")
                    self.assertIsNotNone(staff.retire(row.id))
                path = Path(record["log"]).with_suffix(".json")
                write_text_atomic(path, json.dumps(record))

                refusal = send.RefusedRelayRequest("failed", "fixture refusal")
                with (patch.object(send, "send", side_effect=[
                          refusal, send.Result("sent", "alert received")]) as push,
                      patch.object(detached_runs, "_retry_failed_push") as retry):
                    self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [path.stem])
                    self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                retry.assert_not_called()
                self.assertEqual(push.call_count, 2)
                result, alert = [cast(send.Options, attempt.args[0]) for attempt in push.call_args_list]
                self.assertEqual(result.to, self.fixture.parent.id)
                self.assertEqual(alert.to, self.fixture.parent.id)
                self.assertIn(f"Detached run: {path.stem}", result.text or "")
                self.assertIn("Result delivery failed.", alert.text or "")
                self.assertIn("exit 7", alert.text or "")
                saved = worker_lifecycle.read_json_object(path)
                self.assertEqual(saved["status"], "finished")
                self.assertEqual(saved["delivery"], detached_runs.FAILED_RESULT_PUSH + "failed: " + refusal.detail)
                self.assertIs(saved["push_alert_attempted"], True)
                self.assertNotIn("push_retries", saved)
                self.assertNotIn("delivered_at", saved)

    def test_remote_staff_lookup_failure_precedes_the_result_checkpoint(self) -> None:
        record = self.record(self.fixture.parent, "remote-staff-lookup")
        path = Path(record["log"]).with_suffix(".json")
        error = f"{staff.OWNER_MACHINE} not reached"
        def fail_lookup(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertIn("show --snapshot", command[-1])
            self.assertNotIn("delivery", worker_lifecycle.read_json_object(path))
            return subprocess.CompletedProcess(command, 255, stdout="", stderr="connection lost")

        with (patch.object(staff, "is_owner", return_value=False),
              patch.object(subprocess, "run", side_effect=fail_lookup) as remote,
              patch.object(send, "send") as push):
            self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(7)),
                             detached_runs.FAILED_RESULT_PUSH + error)
        remote.assert_called_once()
        push.assert_not_called()
        self.assertNotIn("delivered_at", worker_lifecycle.read_json_object(path))

    def test_supervisor_retries_after_staff_lookup_fails_before_the_send(self) -> None:
        record = self.record(self.fixture.parent)
        path = Path(record["log"]).with_suffix(".json")
        snapshot = staff.local_snapshot()

        def check_failure(_delay: float) -> None:
            saved = worker_lifecycle.read_json_object(path)
            self.assertEqual((saved["status"], saved["exit_code"]), ("finished", 9))
            self.assertEqual(saved["delivery"], detached_runs.FAILED_RESULT_PUSH + "fixture staff lookup failed")

        with (patch.object(staff, "snapshot", side_effect=[
                  staff.StaffUnreadable("fixture staff lookup failed"), snapshot]),
              patch.object(send, "send", return_value=send.Result("sent", "result received")) as push,
              patch.object(time, "sleep", side_effect=check_failure) as delay,
              patch.object(detached_runs, "_run", return_value=9)):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        delay.assert_called_once_with(5)
        push.assert_called_once()
        self.assertEqual(worker_lifecycle.read_json_object(path)["delivery"], "sent: result received")

    def test_supervisor_retries_if_the_delivery_process_exits_before_the_send(self) -> None:
        for launched in (False, True):
            with self.subTest(launched=launched):
                record = self.record(self.fixture.parent, f"delivery-start-{launched}")
                path = Path(record["log"]).with_suffix(".json")

                def delivery_process(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    if not launched:
                        raise FileNotFoundError(2, "fixture launch error", command[0])
                    return subprocess.CompletedProcess(command, 1, stderr="fixture process error\n")

                def first_delivery(record_path: Path) -> int:
                    with patch.object(subprocess, "run", side_effect=delivery_process):
                        return deliver_in_fresh_process(record_path)

                def check_failure(_delay: float) -> None:
                    saved = worker_lifecycle.read_json_object(path)
                    self.assertEqual((saved["status"], saved["exit_code"]), ("finished", 9))
                    self.assertTrue(str(saved["delivery"]).startswith(detached_runs.FAILED_RESULT_PUSH))
                    self.assertIn("fixture process error" if launched else "fixture launch error", str(saved["delivery"]))

                with (patch.object(detached_runs, "deliver_in_fresh_process", side_effect=first_delivery),
                      patch.object(send, "send", return_value=send.Result("sent", "result received")) as push,
                      patch.object(time, "sleep", side_effect=check_failure) as delay,
                      patch.object(detached_runs, "_run", return_value=9)):
                    self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
                delay.assert_called_once_with(5)
                push.assert_called_once()
                self.assertEqual(worker_lifecycle.read_json_object(path)["delivery"], "sent: result received")

    def test_start_does_not_wait_for_another_runs_push_retry(self) -> None:
        director = self.fixture.parent
        record = self.record(director, "old-result")
        path = Path(record["log"]).with_suffix(".json")
        record["tree_key"] = "private-tree"
        write_text_atomic(path, json.dumps(record))

        def start_during_delay(_delay: float) -> None:
            saved = worker_lifecycle.read_json_object(path)
            self.assertEqual((saved["status"], saved["exit_code"]), ("finished", 9))
            for lock_path in (path.with_suffix(".completion.lock"), path.parent / ".start.lock",
                              path.with_name(f".{path.stem}.delivery.lock"), path.with_suffix(".record.lock")):
                with lock_path.open("r") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(detached_runs.live_runs(path.parent, director.name), [])
            with patch.object(detached_run_process, "supervises", return_value=True):
                self.assertEqual(detached_runs.main(["start", "--session-dir", str(self.run_dir), "--", "true"]), 0)

        refusal = send.Result("refused", "fixture refusal")
        accepted = send.Result("sent", "result received")
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": director.current_session}),
              patch.object(treekey, "tree_key", return_value="private-tree"),
              patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=DEAD_PID)) as spawn,
              patch.object(send, "send", side_effect=[refusal, accepted]),
              patch.object(detached_runs, "_run", return_value=9),
              patch.object(time, "sleep", side_effect=start_during_delay) as delay):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[0][2], "supervise")
        delay.assert_called_once_with(5)

    def test_a_failed_completion_event_keeps_other_orphans_and_retries_next_pass(self) -> None:
        row = self.fixture.worker(family="codex")
        records = [self.record(row, name) for name in ("first-result", "second-result")]
        for record in records:
            record["pid"] = DEAD_PID
            record["command"] = ["echo", record["run_id"]]
            write_text_atomic(Path(record["log"]).with_suffix(".json"), json.dumps(record))
        complete = turn_end.run_finished
        calls: list[str] = []

        def fail_once(owner: staff.StaffId) -> detached_runs.CompletionReplay:
            calls.append(owner)
            runs = self.run_dir / detached_runs.DETACHED_RUNS_DIR
            with (runs / ".start.lock").open("a") as admission:
                fcntl.flock(admission, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if len(calls) == 1:
                raise ValueError("director unavailable")
            return complete(owner)

        with patch.object(turn_end, "run_finished", side_effect=fail_once), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), ["first-result", "second-result"])
            self.assertEqual(len(calls), 2)
            self.assertEqual(errors.getvalue().count("anomaly:"), 1)
            self.assertIn("director unavailable", errors.getvalue())
            pending = cast(dict[str, object], json.loads(Path(records[0]["log"]).with_suffix(".json").read_text()))
            self.assertTrue(pending["completion_pending"])
            with patch.object(detached_run_process, "supervisor_match", return_value=True) as supervisor_match:
                self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                self.assertEqual(len(calls), 2)
                supervisor_match.assert_called_once_with(DEAD_PID, Path(records[0]["log"]).with_suffix(".json"))
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertEqual(len(calls), 3)
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.notices), 2)
        for record in records:
            finished = cast(dict[str, object], json.loads(Path(record["log"]).with_suffix(".json").read_text()))
            self.assertEqual(finished["status"], "finished")
            self.assertFalse(finished["completion_pending"])

    def test_delayed_transcript_publication_confirms_one_submission(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        record = self.record(row)
        self.defer_transcript = True
        read_records = worker_admission.transcript_records

        # Publish pending input only after a reader has listed the transcript.
        # The receipt and the submission confirmation both read records through this function.
        def delayed_records(source: Path, *, offset: int = 0,
                            boundary: worker_admission.TranscriptRecordBoundary = worker_admission.TranscriptRecordBoundary.ALL_RECORDS,
                            ) -> Iterator[dict[str, object] | worker_admission.TornTranscriptRecord]:
            records = list(read_records(source, offset=offset, boundary=boundary))
            for pending_source, entry in self.pending_transcripts:
                write_text_atomic(pending_source, pending_source.read_text() + entry)
            self.pending_transcripts.clear()
            yield from records

        with patch.object(worker_admission, "transcript_records", delayed_records), ThreadPoolExecutor(max_workers=2) as pool:
            futures = (pool.submit(detached_runs.deliver, record, detached_runs.CompletedCommand(0)), pool.submit(detached_runs.deliver, record, detached_runs.CompletedCommand(0)))
            results = [future.result() for future in futures]
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(sum("already delivered" in result for result in results), 1)
        self.assertEqual(sum(result.startswith("unconfirmed: ") for result in results), 1)

    def test_a_prompt_launch_failure_allows_one_retry(self) -> None:
        row = self.fixture.worker(family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        record = self.record(row)
        path = Path(record["log"]).with_suffix(".json")
        run = self.fixture.world.run
        attempts = 0

        def launch(*arguments: str, deadline: float = math.inf) -> str:
            nonlocal attempts
            if arguments[:3] == ("agent", "prompt", pane):
                attempts += 1
                if attempts == 1:
                    error = FileNotFoundError(2, "prompt command unavailable", self.fixture.world.binary)
                    raise herdr.HerdrCommandNotStarted(str(error)) from error
                saved = worker_lifecycle.read_json_object(path)
                self.assertEqual(saved.get("delivery"), "unconfirmed: the prompt may have sent input")
            return run(*arguments, deadline=deadline)

        with patch.object(self.fixture.world, "run", side_effect=launch):
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 1)
            saved = worker_lifecycle.read_json_object(path)
            self.assertTrue(str(saved.get("delivery")).startswith("submission failed: "))
            self.assertEqual(self.inputs, [])
            saved.update(status="finished", exit_code=0, completion_pending=True, pid=DEAD_PID)
            write_text_atomic(path, json.dumps(saved))
            self.assertIsInstance(detached_runs.submit_finished_worker_result(self.run_dir, row,
                worker_input=detached_runs.WorkerResultInput.TYPE), detached_runs.ConfirmedPaneResult)
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(attempts, 2)
        self.assertEqual(len(self.inputs), 1)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("delivery"), f"sent: worker {row.name} at {pane}")
        self.assertIs(saved.get("completion_pending"), False)

    def test_deadline_after_the_input_checkpoint_allows_one_retry(self) -> None:
        row = self.fixture.worker(family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        record = self.record(row)
        path = Path(record["log"]).with_suffix(".json")
        run = self.fixture.world.run
        herdr_run = super(pane_support.PaneWorld, self.fixture.world).run

        def expire_before_launch(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:3] == ("agent", "prompt", pane):
                saved = worker_lifecycle.read_json_object(path)
                self.assertEqual(saved.get("delivery"), "unconfirmed: the prompt may have sent input")
                with (worker_lifecycle.operation_deadline(deadline=0.0),
                      patch.object(herdr, "run_program", side_effect=AssertionError("the prompt started"))):
                    return herdr_run(*arguments, deadline=deadline)
            return run(*arguments, deadline=deadline)

        with patch.object(self.fixture.world, "run", side_effect=expire_before_launch):
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 1)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("delivery"), "submission failed: deadline passed")
        self.assertEqual(self.inputs, [])
        saved.update(status="finished", exit_code=0, completion_pending=True, pid=DEAD_PID)
        write_text_atomic(path, json.dumps(saved))
        self.assertIsInstance(detached_runs.submit_finished_worker_result(self.run_dir, row,
            worker_input=detached_runs.WorkerResultInput.TYPE), detached_runs.ConfirmedPaneResult)

        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(worker_lifecycle.read_json_object(path).get("delivery"), f"sent: worker {row.name} at {pane}")

    def test_a_late_transcript_does_not_repeat_input_on_turn_end(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        record = self.record(row)
        record["pid"] = DEAD_PID
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        self.defer_transcript = True

        self.assertEqual(detached_runs.main(["deliver", str(path)]), 1)
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [record["run_id"]])
        event = turn_end.WorkerEvent(str(row.current_session))
        self.assertIs(turn_end.handle(event), turn_end.TurnOutcome.RETRY_EVIDENCE)
        self.assertEqual(len(self.inputs), 1)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("status"), "finished")
        self.assertIs(saved.get("completion_pending"), True)

        (source, receipt), = self.pending_transcripts
        write_text_atomic(source, source.read_text() + receipt)
        self.assertIs(detached_runs.submit_finished_worker_result(self.run_dir, row,
            worker_input=detached_runs.WorkerResultInput.TYPE), detached_runs.WorkerResultReplay.NONE)
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(len(self.inputs), 1)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("delivery"), f"already delivered: {record['run_id']}")
        self.assertIs(saved.get("completion_pending"), False)

    def test_a_result_from_a_busy_turn_waits_for_idle_and_a_transcript_receipt(self) -> None:
        row = self.fixture.worker(family="claude")
        pane = "w1:p1"
        self.fixture.world.add(row, pane)
        self.fixture.world.locations[pane]["agent_status"] = "working"
        record = self.record(row)
        path = Path(record["log"]).with_suffix(".json")
        self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        finished = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(finished.get("delivery"), "deferred: worker is working")
        self.assertTrue(finished.get("completion_pending"))
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.pending_transcripts, [])

        self.fixture.world.locations[pane]["agent_status"] = "idle"
        event = turn_end.WorkerEvent(str(row.current_session))
        self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.SUBMITTED)
        self.assertEqual(len(self.inputs), 1)
        self.assertIn(f"Detached run: {record['run_id']}", self.inputs[0][1])
        self.assertEqual(self.pending_transcripts, [])
        saved = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertEqual(saved.get("delivery"), f"sent: worker {row.name} at {pane}")
        self.assertTrue(saved.get("completion_pending"))
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)
        self.assertEqual(self.notices, [])

    def test_interrupted_result_record_waits_for_a_receipt_without_retyping(self) -> None:
        row = self.fixture.worker(family="claude")
        self.fixture.world.add(row, "w1:p1")
        record = self.record(row)
        record["pid"] = DEAD_PID
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        self.defer_transcript = True

        def interrupt_finish(target: Path, text: str) -> None:
            if target == path:
                saved = cast(detached_runs.RunRecord, json.loads(text))
                if saved["status"] == "finished":
                    raise SystemExit("interrupted before the finished record")
            write_text_atomic(target, text)

        with patch("scripts.agents.detached_runs.write_text_atomic", interrupt_finish):
            with self.assertRaisesRegex(SystemExit, "interrupted before the finished record"):
                _ = detached_runs.main(["supervise", str(path)])

        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [record["run_id"]])
        self.assertEqual(len(self.inputs), 1)
        finished = worker_lifecycle.read_json_object(path)
        self.assertEqual(finished.get("status"), "finished")
        self.assertIs(finished.get("completion_pending"), True)
        (source, receipt), = self.pending_transcripts
        write_text_atomic(source, source.read_text() + receipt)
        self.assertIs(detached_runs.submit_finished_worker_result(self.run_dir, row,
            worker_input=detached_runs.WorkerResultInput.TYPE), detached_runs.WorkerResultReplay.NONE)
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(len(self.inputs), 1)
        self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)
        (target, alert), = self.notices
        self.assertEqual(target, self.fixture.parent.id)
        self.assertIn("The result may have arrived.", alert)

    def test_pane_result_arrives_after_the_launcher_exits(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        supervisors: list[object] = []

        def start_supervisor(*arguments: object, **options: object) -> SimpleNamespace:
            del options
            supervisors.extend(arguments)
            return SimpleNamespace(pid=998901)

        with patch.dict(os.environ, {"CODEX_THREAD_ID": row.current_session, "BUILDLOG_SCOPE": "0"}), \
                patch.object(detached_runs, "subprocess", SimpleNamespace(
                    Popen=start_supervisor, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT)):
            _ = os.environ.pop("UNIT_RUN_DIR", None)
            _ = os.environ.pop("PLAN_DELEGATE_BOARD_DIR", None)
            self.assertEqual(detached_runs.main(["start", "--label", "private result", "--",
                                             sys.executable, "-c", "print('result after launcher exit')"]), 0)
        paths = list((self.run_dir / detached_runs.DETACHED_RUNS_DIR).glob("*.json"))
        self.assertEqual(len(paths), 1)
        record = cast(detached_runs.RunRecord, json.loads(paths[0].read_text()))
        self.assertEqual(record["staff_id"], row.id)
        self.assertEqual(record["session_dir"], str(self.run_dir))
        self.assertIn("supervise", str(supervisors))

        self.assertEqual(detached_runs.main(["supervise", str(paths[0])]), 0)

        self.assertEqual([pane for pane, _ in self.inputs], ["w1:p1"])
        self.assertIn("result after launcher exit", self.inputs[0][1])
        self.assertNotIn("End your final reply with this line alone:", self.inputs[0][1])
        self.assertEqual(self.notices, [])

    def test_result_follows_the_current_session_after_rename_and_transfer_during_work(self) -> None:
        row = self.fixture.worker(family="codex")
        record = self.record(row)
        row = staff.rename(row.id, "renamed-worker")
        self.fixture.world.add(row, "w2:p7", family="codex", label="old-label", workspace="w2", tab="w2:t2")
        self.fixture.world.locations["w2:p7"]["agent_status"] = "working"
        sibling = self.fixture.worker("sibling", family="codex")
        self.fixture.world.add(sibling, "w1:p2", family="codex")

        result = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
        self.assertEqual(result, "deferred: worker is working")
        self.assertEqual(self.inputs, [])
        self.fixture.world.locations["w2:p7"]["agent_status"] = "idle"
        result = detached_runs.deliver(record, detached_runs.CompletedCommand(0))

        self.assertIn("w2:p7", result)
        self.assertEqual([pane for pane, _ in self.inputs], ["w2:p7"])
        self.assertIn(record["run_id"], self.inputs[0][1])
        self.assertIn("private verification output", self.inputs[0][1])
        self.assertEqual(self.notices, [])

    def test_delivery_rerun_after_type_types_nothing(self) -> None:
        row = self.fixture.worker(family="claude")
        pane = "w1:p1"
        self.fixture.world.add(row, pane)

        for timing in ("after", "before"):
            with self.subTest(stop=timing):
                self.inputs.clear()
                self.fixture.world.locations[pane]["agent_status"] = "idle"
                record = self.record(row, timing)
                self.interrupt_after_input = timing == "after"
                with contextlib.ExitStack() as fault, self.assertRaisesRegex(SystemExit, f"interrupted {timing} pane input"):
                    if timing == "before":
                        _ = fault.enter_context(patch.object(
                            herdr, "check_target", side_effect=SystemExit("interrupted before pane input")))
                    _ = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
                self.interrupt_after_input = False
                self.assertEqual(len(self.inputs), int(timing == "after"))

                result = detached_runs.deliver(record, detached_runs.CompletedCommand(0))

                if timing == "after":
                    self.assertIn("already delivered", result)
                else:
                    self.assertEqual(result, f"sent: worker {row.name} at {pane}")
                    self.assertIn(f"Detached run: {record['run_id']}", self.inputs[0][1])
                self.assertEqual(len(self.inputs), 1)
                self.assertEqual(self.notices, [])

        for family in ("claude", "codex"):
            with self.subTest(stop="checkpoint_interruption_then_done", family=family):
                self.check_checkpoint_interruption_then_done(family)

    def check_checkpoint_interruption_then_done(self, family: Literal["claude", "codex"]) -> None:
        agent = test_worker.DispatchAgent(self.fixture)
        row = self.fixture.worker(f"checkpoint-{family}", family=family)
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family=family)
        dispatch, reply = self.begin_pane_pass(row)
        delivery = progress_history.dispatch_delivery(worker_panes.worker_pass(row, self.run_dir))
        agent.append(pane, "user", delivery.line)
        record = self.record(row, dispatch)
        record.update(status="finished", pid=DEAD_PID, exit_code=0, pane_dispatch_id=dispatch,
                      finished_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                      completion_pending=True)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        inputs = list(self.inputs)
        notices = len(self.notices)

        def interrupt_after_checkpoint(*arguments: object, **keywords: object) -> None:
            self.assertEqual(arguments[0], pane)
            self.assertIn("proof", keywords)
            self.assertEqual(worker_lifecycle.read_json_object(path)["delivery"], detached_runs.WORKER_INPUT_CHECKPOINT)
            raise SystemExit("interrupted checkpoint pane input")

        with (patch.object(self.fixture.world, "_prompt", new=interrupt_after_checkpoint),
              self.assertRaisesRegex(SystemExit, "interrupted checkpoint pane input")):
            _ = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
        result = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
        self.assertEqual(result, detached_runs.WORKER_INPUT_CHECKPOINT + ": interrupted checkpoint pane input")
        self.assertEqual(worker_lifecycle.read_json_object(path)["delivery"], result)
        self.assertEqual(self.inputs, inputs)
        self.assertEqual(len(self.notices), notices)

        done = f"DONE {dispatch}"
        agent.append(pane, "assistant", done)
        self.fixture.world.locations[pane]["agent_status"] = "done"
        self.assertEqual(self.native_stop(row, done), 0)
        self.assertEqual(reply.read_text().rstrip(), done)
        self.assertEqual(worker_panes.worker_pass(row, self.run_dir)["status"], "completed")
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["delivery"], result)
        self.assertIs(saved["completion_pending"], False)
        self.assertNotIn("delivered_at", saved)
        self.assertNotIn("push_alert_attempted", saved)
        self.assertEqual(self.inputs, inputs)
        self.assertEqual(len(self.notices[notices:]), 1)
        self.assertIn("phase 40: done", self.notices[-1][1])

    def test_concurrent_deliveries_type_one_result(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        record = self.record(row)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(detached_runs.deliver, record, detached_runs.CompletedCommand(code)) for code in (0, 0)]
            results = [future.result() for future in futures]
        self.assertEqual(len(self.inputs), 1)
        self.assertEqual(sum("already delivered" in result for result in results), 1)
        self.assertEqual(self.notices, [])

    def test_distinct_run_ids_deliver_the_same_done_message_separately(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        done = self.fixture.root / "done.txt"
        write_text_atomic(done, "Read the verification report.")
        for run_id in ("first", "second"):
            record = self.record(row, run_id)
            record["done_message_file"] = str(done)
            _ = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
            _ = detached_runs.deliver(record, detached_runs.CompletedCommand(0))
        self.assertEqual(len(self.inputs), 2)
        self.assertIn("Detached run: first", self.inputs[0][1])
        self.assertIn("Detached run: second", self.inputs[1][1])
        self.assertTrue(all("Read the verification report." in text for _, text in self.inputs))

    def test_stopped_worker_result_reaches_its_holder(self) -> None:
        row = self.fixture.worker()
        self.fixture.world.add(row, "w1:p1", live=False)
        self.assert_holder_result(self.record(row), "worker is stopped")

    def test_closed_worker_result_reaches_its_holder(self) -> None:
        row = self.fixture.worker()
        self.assert_holder_result(self.record(row), "worker pane is closed")

    def test_unknown_process_result_reaches_its_holder_with_one_anomaly(self) -> None:
        row = self.fixture.worker()
        self.fixture.world.add(row, "w1:p1")
        self.fixture.world.unknown["w1:p1"] = "private process read failed"
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assert_holder_result(self.record(row), "private process read failed")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)

    def test_duplicate_native_identity_result_reaches_its_holder(self) -> None:
        row = self.fixture.worker()
        self.fixture.world.add(row, "w1:p1")
        self.fixture.world.add(row, "w2:p2", tab="w2:t2", workspace="w2")
        self.assert_holder_result(self.record(row), "duplicate native identities")

    def test_blocked_worker_result_reaches_its_holder(self) -> None:
        row = self.fixture.worker()
        pane = "w1:p1"
        self.fixture.world.add(row, pane)
        self.fixture.world.locations[pane]["agent_status"] = "blocked"
        record = self.record(row)
        path = Path(record["log"]).with_suffix(".json")

        self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("status"), "finished")
        self.assertEqual(saved.get("delivery"), "deferred: worker is blocked")
        self.assertIs(saved.get("completion_pending"), True)
        self.assertNotIn("delivered_at", saved)
        self.assertEqual(self.inputs, [])
        (target, notice), = self.notices
        self.assertEqual(target, f"session:{self.fixture.parent.current_session}")
        self.assertIn(f"Worker {row.name}:", notice)
        self.assertIn("needs input", notice)
        self.assertIn(record["label"], notice)
        self.assertIn(str(path), notice)
        self.assertNotIn("private verification output", notice)

        for status, manual in (("blocked", True), ("working", False), ("blocked", False)):
            with self.subTest(status=status, manual=manual):
                self.fixture.world.locations[pane]["agent_status"] = status
                if manual:
                    self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
                else:
                    self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                saved = worker_lifecycle.read_json_object(path)
                self.assertEqual(saved.get("delivery"), f"deferred: worker is {status}")
                self.assertIs(saved.get("completion_pending"), True)
                self.assertNotIn("delivered_at", saved)
                self.assertEqual(self.inputs, [])
                self.assertEqual(self.notices, [(target, notice)])

        self.fixture.world.locations[pane]["agent_status"] = "idle"
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        (destination, text), = self.inputs
        self.assertEqual(destination, pane)
        self.assertIn(f"Detached run: {record['run_id']}", text)
        self.assertIn("private verification output", text)
        self.assertEqual(self.notices, [(target, notice)])
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("delivery"), f"sent: worker {row.name} at {pane}")
        self.assertIn("delivered_at", saved)
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)
        self.assertEqual(self.inputs, [(destination, text)])

        for index, accepted in enumerate((True, False), start=8):
            with self.subTest(blocked_notice_accepted=accepted):
                self.check_blocked_notice_after_refused_push(f"w1:p{index}", accepted=accepted)

    def check_blocked_notice_after_refused_push(self, pane: str, *, accepted: bool) -> None:
        name = f"blocked-notice-{pane.rsplit('p', 1)[-1]}"
        row = self.fixture.worker(name)
        record = self.record(row, name)
        record.update(status="finished", exit_code=9, pid=DEAD_PID, completion_pending=True)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))
        refused = send.RefusedRelayRequest("failed", "the result relay refused the request")
        failure = detached_runs.FAILED_RESULT_PUSH + f"failed: {refused.detail}"
        with patch.object(send, "_send", return_value=refused) as push:
            self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(9)), failure)
        push.assert_called_once()

        self.fixture.world.add(row, pane)
        self.fixture.world.locations[pane]["agent_status"] = "blocked"
        notice = send.Result("sent", "notice accepted") if accepted else send.Result("failed", "notice failed")
        inputs = list(self.inputs)
        with patch.object(send, "_send", return_value=notice) as notify:
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 1)
        notify.assert_called_once()
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["delivery"], failure)
        self.assertIs(saved["completion_pending"], True)
        self.assertEqual(saved.get("blocked_notice_accepted", False), accepted)
        self.assertNotIn("delivered_at", saved)
        self.assertEqual(self.inputs, inputs)

        with patch.object(send, "send", return_value=send.Result("sent", "alert accepted")) as alert:
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        alert.assert_called_once()
        options = cast(send.Options, alert.call_args.args[0])
        self.assertIn(f"Delivery: {failure}", options.text or "")
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["delivery"], failure)
        self.assertIs(saved["push_alert_attempted"], True)
        self.assertNotIn("delivered_at", saved)

        self.fixture.world.locations[pane]["agent_status"] = "idle"
        self.assertIsInstance(detached_runs.submit_finished_worker_result(self.run_dir, row,
            worker_input=detached_runs.WorkerResultInput.TYPE), detached_runs.ConfirmedPaneResult)
        self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
        self.assertEqual(len(self.inputs), len(inputs) + 1)
        self.assertEqual(self.inputs[-1][0], pane)
        self.assertIn(f"Detached run: {record['run_id']}", self.inputs[-1][1])
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved["delivery"], f"sent: worker {row.name} at {pane}")
        self.assertIs(saved["completion_pending"], False)

    def test_unreadable_transcript_result_reaches_its_holder_without_input(self) -> None:
        row = self.fixture.worker(family="codex")
        self.fixture.world.add(row, "w1:p1", family="codex")
        source = worker_admission.transcript_source(row, staff.CodexBackend(self.fixture.root / "codex"))
        source.unlink()
        self.assert_holder_result(self.record(row), "worker transcript is unavailable")

    def test_self_stop_result_waits_for_the_worker_transcript(self) -> None:
        row = self.fixture.worker(family="codex")
        pane = "w1:p1"
        self.fixture.world.add(row, pane, family="codex")
        source = worker_admission.transcript_source(row, staff.CodexBackend(self.fixture.root / "codex"))
        transcript = source.read_text()
        source.unlink()
        record = self.record(row)
        record.update(status="finished", stop_reason="The check is obsolete.", self_stop=True, stop_pass_requested=True, exit_code=143)
        path = Path(record["log"]).with_suffix(".json")
        write_text_atomic(path, json.dumps(record))

        self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        saved = worker_lifecycle.read_json_object(path)
        self.assertIn("deferred: worker transcript is unavailable", str(saved.get("delivery")))
        self.assertNotIn("delivered_at", saved)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.inputs, [])

        write_text_atomic(source, transcript)
        self.fixture.world.remove_pane(pane)
        self.assertIs(detached_runs.submit_finished_worker_result(self.run_dir, row,
            worker_input=detached_runs.WorkerResultInput.TYPE), detached_runs.WorkerResultReplay.RETRY)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("delivery"), "deferred: worker pane is closed")
        self.assertNotIn("delivered_at", saved)
        self.assertEqual(self.notices, [])
        self.assertEqual(self.inputs, [])

        self.fixture.world.add(row, pane, family="codex")
        self.assertIsInstance(detached_runs.submit_finished_worker_result(self.run_dir, row,
            worker_input=detached_runs.WorkerResultInput.TYPE), detached_runs.ConfirmedPaneResult)
        self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        (target, text), = self.inputs
        self.assertEqual(target, pane)
        self.assertIn("Result: STOPPED (exit 143)", text)
        self.assertIn("Stop reason: The check is obsolete.", text)
        self.assertEqual(self.notices, [])

    def test_retired_worker_self_stop_reaches_the_director_and_can_be_pruned(self) -> None:
        for case in ("direct", "deferred", "unreadable", "offline"):
            with self.subTest(case=case):
                row = self.fixture.worker(f"retired-{case}")
                record = self.record(row, f"retired-result-{case}")
                record.update(status="finished", exit_code=143, stop_reason="The check is obsolete.",
                              self_stop=True, stop_pass_requested=True)
                if case == "deferred":
                    record.update(delivery="deferred: worker pane is closed", completion_pending=False)
                if case == "offline":
                    record.update(status="running", pid=DEAD_PID, tree_key="private-tree")
                path = Path(record["log"]).with_suffix(".json")
                write_text_atomic(path, json.dumps(record))
                self.assertIsNotNone(staff.retire(row.id))
                if case == "offline":
                    self.assert_offline_director_result(record, path)
                    continue
                if case == "unreadable":
                    state_path = self.run_dir / unit_runs.STATE_FILE
                    state = state_path.read_text()
                    write_text_atomic(state_path, "{")
                    try:
                        self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
                        self.assertEqual(self.notices, [])
                        saved = worker_lifecycle.read_json_object(path)
                        self.assertTrue(str(saved.get("delivery")).startswith("deferred: director unavailable"))
                        self.assertNotIn("delivered_at", saved)
                        self.assertEqual(detached_runs.prune_finished(path.parent), [])
                    finally:
                        write_text_atomic(state_path, state)
                if case in ("deferred", "unreadable"):
                    self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [])
                self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
                self.assertEqual(self.inputs, [])
                target, message = self.notices.pop()
                self.assertEqual(self.notices, [])
                self.assertEqual(target, self.fixture.parent.id)
                self.assertIn("Result: STOPPED (exit 143)", message)
                self.assertIn("Stop reason: The check is obsolete.", message)
                self.assertIn("delivered_at", worker_lifecycle.read_json_object(path))
                self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])

    def test_retired_worker_self_stop_checks_receipts_without_resending(self) -> None:
        self.defer_transcript = True
        for earlier_refusal in (False, True):
            with self.subTest(earlier_refusal=earlier_refusal):
                row = self.fixture.worker(f"retired-unconfirmed-{earlier_refusal}", family="codex")
                pane = "w1:p1"
                self.fixture.world.add(row, pane, family="codex")
                record = self.record(row, f"retired-unconfirmed-{earlier_refusal}")
                record.update(status="finished", exit_code=143, stop_reason="The check is obsolete.",
                              self_stop=True, stop_pass_requested=True)
                if earlier_refusal:
                    record["delivery"] = detached_runs.FAILED_RESULT_PUSH + "refused: fixture refusal"
                path = Path(record["log"]).with_suffix(".json")
                write_text_atomic(path, json.dumps(record))
                result = detached_runs.CompletedCommand(143)
                pending = detached_runs.deliver(record, result)
                prefix = (detached_runs.UNCONFIRMED_RESULT_PUSH if earlier_refusal
                          else detached_runs.UNCONFIRMED_WORKER_RESULT)
                self.assertEqual(pending, prefix + "the worker transcript has no result receipt")
                self.fixture.world.remove_pane(pane)
                self.assertIsNotNone(staff.retire(row.id))

                with (patch.object(send, "send") as push,
                      patch.object(detached_runs, "_result_received", side_effect=[False, True]) as receipt):
                    self.assertEqual(detached_runs.deliver(record, result), pending)
                    self.assertNotIn("delivered_at", worker_lifecycle.read_json_object(path))
                    self.assertEqual(detached_runs.deliver(record, result), f"already delivered: {path.stem}")
                push.assert_not_called()
                self.assertEqual(receipt.call_count, 2)
                for checked in receipt.call_args_list:
                    director = cast(staff.StaffRow, checked.args[0])
                    self.assertEqual(director.id, self.fixture.parent.id)
        self.assertEqual(len(self.inputs), 2)
        self.assertEqual(self.notices, [])

    def assert_offline_director_result(self, record: detached_runs.RunRecord, path: Path) -> None:
        self.holder_sender.close()
        self.fixture.world.remove_pane("w1:p0")
        tree_key = "private-tree"
        command = record["command"]
        name = record["worker"]
        destination = f"staff:{self.fixture.parent.id}"
        identity = staff.StaffId(record["staff_id"])
        with patch.object(detached_run_process, "supervises", return_value=False):
            for ended in (False, True):
                with (self.subTest(command_ended=ended),
                      patch.object(detached_runs, "_command_has_ended", return_value=ended)):
                    twin = detached_runs.live_twin(path.parent, identity, command, tree_key)
                    if ended:
                        self.assertIsNone(twin)
                    else:
                        self.assertIsNotNone(twin)
        with (patch.object(send, "STATE", self.fixture.root / "messages"),
              patch.object(roster, "roster", return_value=roster.Roster([], [])),
              patch.object(roster, "peer_roster", return_value=roster.Roster([], [])),
              patch.object(unit_lookup, "checked_conversation", return_value=unit_lookup.ProvedAbsentUnit()),
              patch.object(sessions, "read_registry", return_value=sessions.RegistryRead((), ()))):
            self.assertEqual(detached_runs.reap_orphans(str(self.run_dir)), [record["run_id"]])
            saved = worker_lifecycle.read_json_object(path)
            self.assertEqual(saved.get("status"), "finished")
            self.assertTrue(str(saved.get("delivery")).startswith("queued: "))
            self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
            queued, = send.read_queue(send.queue_path(destination))
            self.assertIn(f"Detached run: {record['run_id']}", queued["text"])
            self.assertIn("Result: STOPPED (exit 143)", queued["text"])

        _ = self.fixture.worker(name, family="codex")
        with (patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": ""}),
              patch.object(treekey, "tree_key", return_value=tree_key),
              patch.object(subprocess, "Popen", return_value=SimpleNamespace(pid=0)) as spawn):
            self.assertEqual(detached_runs.main(["start", "--session-dir", str(self.run_dir), "--to", name,
                                                "--", *command]), 0)
        spawn.assert_called_once()


if __name__ == "__main__":
    _ = unittest.main()

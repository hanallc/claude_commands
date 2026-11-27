"""Exercise native hook payloads against private staff and transcripts."""
from __future__ import annotations

from scripts.direct import pass_record as pass_recorder
from scripts.lint.memory_test_support import copy_config_file_fixture
from scripts.lib.checkout_test_support import python_tool_source

import hashlib
import json
import io
import math
import os
import shutil
import subprocess
import sys
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from functools import partial
from threading import Event, current_thread
from tempfile import TemporaryDirectory
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal, cast, final, override
from unittest.mock import patch

from scripts.accounts import refusals
from scripts.agents import detached_run_process, detached_runs, restart, shutdown, shutdown_record, stop_resume, worker_lifecycle
from scripts.astra import astra_roles
from scripts.direct import assign, progress_history
from scripts.herdr import herdr, pass_notices, side_pane, turn_end, worker_panes
from scripts.herdr.test_worker_panes import prepare_recorder_history
from scripts.lib import atomic_file, copy_record, process_start, scratch, unit_runs
from scripts.message import send
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lib.test_models_registry import write_registry
from scripts.production import staff, test_worker, worker, worker_admission
from scripts.production.staff_test_support import fixture_runtime_id, fixture_session, fixture_worker
from scripts.whoami import agent_accounts, note_file, quota_alert


class NativeHookEntryTests(unittest.TestCase):
    def test_native_entry_starts_a_separate_handler_without_calling_handle(self) -> None:
        real_popen = subprocess.Popen
        for family, size in (("codex", 1), ("claude", 1), ("codex", 5_000)):
            with self.subTest(family=family, size=size), TemporaryDirectory() as folder:
                reply = 'The work is complete.\nDONE "dispatch"' * size
                value: dict[str, object] = {"hook_event_name": "Stop", "session_id": "native-thread",
                                            "last_assistant_message": reply, "cwd": folder}
                if family == "codex":
                    value.update({"turn_id": "turn-1", "model": "sol", "transcript_path": None})
                payload = json.dumps(value)
                receiver = Path(folder) / "receive.py"
                receipt = Path(folder) / "receipt.json"
                atomic_file.write_text_atomic(receiver, python_tool_source("\n".join((
                    "from dataclasses import asdict",
                    "import json, sys",
                    "from pathlib import Path",
                    "from scripts.herdr import turn_end",
                    "from scripts.lib.atomic_file import write_text_atomic",
                    "turn_end.handle = lambda event: write_text_atomic(Path(sys.argv[1]), json.dumps(asdict(event)))",
                    "raise SystemExit(turn_end.main(sys.argv[2:]))", ""))))
                children: list[subprocess.Popen[bytes]] = []

                def receive(arguments: list[str], *, start_new_session: bool, stdin: int,
                            stdout: int, stderr: int) -> subprocess.Popen[bytes]:
                    child = real_popen([arguments[0], str(receiver), str(receipt), *arguments[2:]],
                        start_new_session=start_new_session, stdin=stdin, stdout=stdout, stderr=stderr)
                    self.addCleanup(child.wait, timeout=TEST_HANG_GUARD_SECS)
                    self.addCleanup(child.kill)
                    children.append(child)
                    return child

                output = io.StringIO()
                with (patch.object(turn_end, "handle") as handler,
                      patch.object(subprocess, "Popen", side_effect=receive) as start,
                      patch.object(sys, "stdin", io.StringIO(payload)),
                      patch.object(sys, "stdout", output)):
                    self.assertEqual(turn_end.main([]), 0)
                handler.assert_not_called()
                start.assert_called_once_with(
                    [sys.executable, str(Path(turn_end.__file__).resolve()), "--handle-hook"],
                    start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.assertEqual(children[0].wait(timeout=TEST_HANG_GUARD_SECS), 0)
                self.assertEqual(json.loads(receipt.read_text()),
                                 {"thread_id": "native-thread", "kind": "turn end", "reply": reply, "cwd": folder,
                                  "native_stop": value,
                                  "source_event_id": "native-thread:turn-1" if family == "codex" else ""})
                self.assertEqual(output.getvalue(), "")

    def test_separate_invocation_calls_handle_once_without_starting_another_process(self) -> None:
        cases: tuple[tuple[dict[str, object], turn_end.WorkerEvent], ...] = (
            ({"hook_event_name": "Stop", "session_id": "native-thread", "last_assistant_message": "The work is complete."},
             turn_end.WorkerEvent("native-thread", reply="The work is complete.")),
            ({"hook_event_name": "Stop", "session_id": "native-thread", "last_assistant_message": None},
             turn_end.WorkerEvent("native-thread")),
            ({"type": "agent-turn-complete", "thread-id": "native-thread", "last-assistant-message": "The work is complete."},
             turn_end.WorkerEvent("")),
        )
        for value, expected in cases:
            with (self.subTest(value=value),
                  patch.object(turn_end, "handle", return_value=turn_end.TurnOutcome.DONE) as handler,
                  patch.object(subprocess, "Popen") as start,
                  patch.object(sys, "stdin", io.StringIO(json.dumps(value))),
                  patch.object(sys, "stdout", io.StringIO()) as output):
                self.assertEqual(turn_end.main(["--handle-hook"]), 0)
            if value.get("hook_event_name") == "Stop":
                expected = replace(expected, native_stop=value)
            handler.assert_called_once_with(expected)
            start.assert_not_called()
            self.assertEqual(output.getvalue(), "")


@final
class PassNoticeOrderTests(unittest.TestCase):
    def test_a_newer_turn_notice_never_sends_the_turn_it_replaced(self) -> None:
        with TemporaryDirectory() as folder:
            run = Path(folder)
            route = pass_notices.Route("pass", staff.StaffId("director"), "worker")
            legacy = pass_notices.LegacyNoticeStamp("", "")
            sent: list[str] = []

            def record(_run: Path, _route: pass_notices.Route, _key: str, notice: pass_notices.Notice) -> send.Result:
                sent.append(notice.text)
                return send.Result("sent", "")

            def stage(text: str) -> str:
                notice = pass_notices.Notice(text, "codex:worker", pass_notices.NoticeKind.STATUS,
                                             transition=text, source_event_id="turn:" + text)
                return pass_notices.stage_status(run, route, notice, legacy=legacy)

            older = stage("older turn")
            with (patch.object(pass_notices, "_send", side_effect=OSError("director unreachable")),
                  self.assertRaisesRegex(OSError, "director unreachable")):
                pass_notices.deliver_status(run, route.instance_id, older)
            with (patch.object(pass_notices, "_send", side_effect=record),
                  patch.object(pass_notices, "_received", return_value=False)):
                pass_notices.deliver_status(run, route.instance_id, stage("newer turn"))
                pass_notices.retry(run)
            self.assertEqual(sent, ["newer turn"])


@final
class TurnEndTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.case = test_worker.WorkerCommandTests("runTest")

    @override
    def setUp(self) -> None:
        self.case = test_worker.WorkerCommandTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def parked_worker_with_run(self) -> tuple[staff.StaffRow, Path, detached_runs.RunRecord]:
        self.case.agent.finish_after = 1
        _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": "codex"}}, update=True)
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        passed = worker_panes.worker_pass(row, self.case.run_dir)
        path, record = self.case.detached_record(row, "parked-stop")
        record.update(status="running", pid=test_worker.DETACHED_FIXTURE_DEAD_PID,
                      pane_dispatch_id=str(passed["instance_id"]))
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.case.agent.without_done = True
        self.case.agent.complete(pane)
        with patch.object(detached_run_process, "supervises", return_value=True):
            self.assertEqual(self.case.finish(), turn_end.TurnOutcome.WAITING_ON_RUNS)
        self.assertNotIn(pane, self.case.fixture.world.locations)
        parked = worker_panes.worker_pass(row, self.case.run_dir)
        self.assertIsInstance(progress_history.parked_worker_launch(parked), progress_history.ParkedWorkerLaunch)
        return row, path, record

    def stop_closed_worker(self, row: staff.StaffRow) -> str:
        with (patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            code = shutdown.main(["stop", f"{self.case.fixture.parent.name}/{row.name}"])
        self.assertEqual(code, 0, errors.getvalue())
        return output.getvalue()

    def test_agents_stop_cancels_a_closed_workers_parked_pass(self) -> None:
        row, _, _ = self.parked_worker_with_run()
        self.assertEqual(self.stop_closed_worker(row), f"{row.name}: ended 1 open pass as canceled\n")
        passed = worker_panes.worker_pass(row, self.case.run_dir)
        self.assertEqual(passed["status"], "canceled")
        self.assertFalse(progress_history.parked_worker_pass(passed))

    def test_agents_stop_cancels_a_closed_workers_pass_waiting_for_a_reply(self) -> None:
        self.case.agent.without_done = True
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane, _ = self.case.agent.submitted[-1]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        path, result = self.case.detached_record(row, "finished-before-close")
        result.update(pane_dispatch_id=instance, delivery=f"sent: worker {row.name} at {pane}", exit_code=0,
                      finished_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        atomic_file.write_text_atomic(path, json.dumps(result))
        self.case.agent.append(pane, "user", "Detached run: finished-before-close\nRun finished: private verification")
        self.case.fixture.world.remove_pane(pane)
        self.assertIsInstance(worker_panes.observe_worker(self.case.fixture.parent.id, row, staff.snapshot()),
                              worker_panes.ClosedWorker)
        self.assertIsInstance(progress_history.active_dispatch(row.id, run), progress_history.ActiveDispatch)
        receipt = self.case.receipt()
        assert isinstance(receipt, worker.DispatchWaitingForRuns)
        self.assertIsInstance(receipt.reply, worker.DispatchAwaitingReply)
        self.assertFalse(receipt.has_live_run)

        following = run / "next-prompt.md"
        atomic_file.write_text_atomic(following, "Start the next assignment.\n")
        assign_next = ("assign", "impl", str(following), "--name", row.name, "--slot", "impl", "--phase", "8",
                       "--kind", "assign", "--session-dir", str(run), "--launcher-command", '["assign.sh"]')
        self.case.assert_assign_waits_for_result_reply(assign_next)

        self.assertEqual(self.stop_closed_worker(row), f"{row.name}: ended 1 open pass as canceled\n")
        self.assertEqual(worker_panes.worker_pass(row, run)["status"], "canceled")
        self.assertEqual(self.case.command(*assign_next), (0, "submitted\n", ""))
        self.assertNotEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)

    def test_result_for_a_canceled_parked_pass_does_not_restore_the_worker(self) -> None:
        row, path, record = self.parked_worker_with_run()
        pane = self.case.agent.submitted[-1][0]
        _ = self.stop_closed_worker(row)
        notices = len(self.case.agent.notices)
        launches = len(self.case.fixture.world.launches)
        record.update(status="finished", exit_code=0, delivery=detached_runs.PARKED_WORKER_RESULT,
                      finished_at=datetime.now().astimezone().isoformat(timespec="microseconds"),
                      completion_pending=True)
        atomic_file.write_text_atomic(path, json.dumps(record))
        with patch.object(worker, "restore_parked_worker", return_value=False) as restore:
            _ = self.case.finish(kind=turn_end.EventKind.RUN_FINISHED)
        restore.assert_not_called()
        result = worker_lifecycle.read_json_object(path)
        self.assertEqual(result.get("delivery"), "sent: private director")
        results = [notice for notice in self.case.agent.notices[notices:]
                   if f"Detached run: {record['run_id']}\n" in notice]
        self.assertEqual(len(results), 1, self.case.agent.notices[notices:])
        self.assertTrue(results[0].startswith(f"Worker {row.name}: worker pane is closed\n"), results[0])
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertEqual(len(self.case.fixture.world.launches), launches)

    def test_agents_stop_reports_unchanged_for_a_closed_worker_without_a_pass(self) -> None:
        row = self.case.fixture.worker("closed")
        self.assertEqual(self.stop_closed_worker(row), f"{row.current_session}: unchanged\n")

    def begin_newer_pass(self, row: staff.StaffRow) -> tuple[Path, str]:
        older = self.case.run_dir
        newer = older.with_name("newer-run")
        state = worker_panes.run_state(older)
        state.update({"pass": {}, "events": [], "history_file": str(newer / "history.jsonl")})
        atomic_file.write_text_atomic(newer / unit_runs.STATE_FILE, json.dumps(state))
        _ = staff.set_run(self.case.fixture.parent.id, staff.OpenRun(newer))
        instance = progress_history.begin_dispatch(newer, slot=row.name, phase="8", role="impl",
            worker_name=row.name, worker_id=row.id, launcher_command=["assign.sh"],
            prompt=self.case.prompt, kind="impl", lens="")
        return newer, instance

    def test_agents_stop_cancels_an_older_parked_pass(self) -> None:
        row, _, _ = self.parked_worker_with_run()
        older = self.case.run_dir
        newer, instance = self.begin_newer_pass(row)
        progress_history.complete_dispatch(newer, row.name, instance)
        self.assertTrue(progress_history.parked_worker_pass(worker_panes.worker_pass(row, older)))

        with patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(older.parent)}):
            self.assertEqual(worker_panes.worker_run(row), newer)
            self.assertEqual(self.stop_closed_worker(row), f"{row.name}: ended 1 open pass as canceled\n")
        self.assertEqual(worker_panes.worker_pass(row, older)["status"], "canceled")
        self.assertTrue(progress_history.dispatch_completed(newer, row.name, instance))

    def test_agents_stop_fails_if_another_pass_is_pending_completion(self) -> None:
        row, _, _ = self.parked_worker_with_run()
        older = self.case.run_dir
        newer, instance = self.begin_newer_pass(row)
        completion = progress_history.HookCompletion(turn_end.TurnOutcome.DONE, str(newer / "reply.md"))
        progress_history.save_hook_completion(newer, instance, completion)

        with (patch.dict(os.environ, {"ROSTER_RUN_ROOT": str(older.parent)}),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            code = shutdown.main(["stop", f"{self.case.fixture.parent.name}/{row.name}"])
        self.assertEqual(worker_panes.worker_pass(row, older)["status"], "canceled")
        self.assertEqual(worker_panes.worker_pass(row, newer)["status"], "active")
        self.assertEqual(output.getvalue(), f"{row.name}: ended 1 open pass as canceled\n")
        self.assertEqual(code, 1)
        self.assertEqual(errors.getvalue(), f"{row.current_session}: an open pass is pending completion\n")

    def test_agents_stop_keeps_the_pass_when_an_open_worker_exits_before_the_signal(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        self.assertIsInstance(worker_panes.observe_worker(self.case.fixture.parent.id, row, staff.snapshot()),
                              worker_panes.OpenWorker)

        def exited(*_args: object, **_kwargs: object) -> herdr.StoppedConversation:
            _ = self.case.fixture.world.locations.pop(pane, None)
            return herdr.StoppedConversation()

        with (patch.object(herdr, "read_conversation", side_effect=exited),
              patch.object(stop_resume, "_signal_process") as signal,
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            code = shutdown.main(["stop", f"{self.case.fixture.parent.name}/{row.name}"])
        self.assertEqual(code, 0, errors.getvalue())
        signal.assert_not_called()
        self.assertEqual(output.getvalue(), f"{row.current_session}: unchanged\n")
        self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "active")

    def test_native_hook_inputs_complete_each_family_once_and_close_its_pane(self) -> None:
        cases = [(family, close, "current") for family in ("codex", "claude")
                 for close in ("closed", "setup", "outside")]
        cases.extend((family, "closed", transcript) for family in ("codex", "claude")
                     for transcript in ("no final reply", "older reply"))
        cases.append(("codex", "scratch", "current"))
        cases.append(("codex", "closed", "trailing period"))
        cases.extend((family, "working", "current") for family in ("codex", "claude"))
        cases.append(("codex", "held-lock", "current"))
        cases.append(("codex", "send-unlocked", "current"))
        for family, close, transcript in cases:
            with self.subTest(family=family, close=close, transcript=transcript):
                _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
                name = "impl-" + family + "-" + close + "-" + transcript.replace(" ", "-")
                self.case.agent.finish_after = int(transcript != "current" or close == "scratch")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                kept_folders: list[Path] = []
                if close == "scratch":
                    peer = fixture_worker("scratch-peer", self.case.fixture.parent.id)
                    with patch.dict(os.environ, {"CODEX_THREAD_ID": str(row.current_session)}):
                        folder = scratch.new_worker()
                        atomic_file.write_text_atomic(folder / "trial.txt", "worker output")
                    with patch.dict(os.environ, {"CODEX_THREAD_ID": str(peer.current_session)}):
                        kept = scratch.new_worker()
                        atomic_file.write_text_atomic(kept / "peer.txt", "peer output")
                        kept_folders.extend((folder, kept))
                    runs = self.case.run_dir / "detached_runs"
                    runs.mkdir(exist_ok=True)
                    for run_id, owner, owner_id in (("own", name, str(row.id)), ("director", "director", "")):
                        log = runs / f"{run_id}.log"
                        record: detached_runs.RunRecord = {
                            "run_id": run_id, "worker": owner, "staff_id": owner_id, "status": "finished",
                            "started_at": datetime.now().timestamp(), "pid": 0, "command": ["true"],
                            "label": "private verification", "log": str(log), "session_dir": str(self.case.run_dir),
                            "director_dir": str(self.case.run_dir), "done_message_file": "",
                        }
                        record["finished_at"] = datetime.now().astimezone().isoformat(timespec="microseconds")
                        atomic_file.write_text_atomic(log.with_suffix(".json"), json.dumps(record))
                        atomic_file.write_text_atomic(log, "output")
                    self.case.agent.complete(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                self.assertIsInstance(active, progress_history.ActiveDispatch)
                assert isinstance(active, progress_history.ActiveDispatch)
                if transcript != "current":
                    if transcript == "older reply":
                        self.case.agent.append(pane, "assistant", "The previous result is pending.")
                    self.case.agent.replies.append("The work is complete.\nDONE " + active.instance_id
                                                  + ("." if transcript == "trailing period" else ""))
                    self.case.fixture.world.locations[pane]["agent_status"] = "done"
                payload: dict[str, object] = {
                    "hook_event_name": "Stop", "session_id": str(row.current_session), "transcript_path": "unused",
                    "stop_hook_active": False, "last_assistant_message": self.case.agent.replies[-1]}
                if family == "codex":
                    payload.update({"turn_id": "turn-1", "cwd": str(self.case.fixture.root), "model": "sol"})
                if close == "working":
                    self.case.fixture.world.locations[pane]["agent_status"] = "working"
                if close == "setup":
                    if family == "claude":
                        self.case.fixture.world.record(row, pane)
                    self.case.fixture.world.foreground[pane] = (998598, "direnv", ["direnv", "export", "zsh"])
                elif close == "outside":
                    self.case.fixture.world.remove_leaf(pane)
                    self.case.fixture.world.layouts["w1:outside"] = {"type": "pane", "pane_id": pane}
                    self.case.fixture.world.locations[pane]["tab_id"] = "w1:outside"
                notices = len(self.case.agent.notices)
                if close == "held-lock":
                    with ThreadPoolExecutor(max_workers=1) as hooks:
                        with worker_panes.worker_lock(self.case.fixture.parent.id, staff.WorkerName.parse(name)) as hold:
                            self.assertIsInstance(hold, worker_panes.WorkerLockHold)
                            pending = hooks.submit(turn_end.handle, turn_end.hook_event(payload))
                            with self.assertRaises(TimeoutError):
                                _ = pending.result(timeout=0.05)
                        self.assertEqual(pending.result(timeout=LauncherHangGuard("queued DONE hook").remaining()),
                                         turn_end.TurnOutcome.DONE)
                elif close == "scratch":
                    with io.BytesIO() as pipe:
                        child = SimpleNamespace(stdin=nullcontext(pipe))
                        with (patch.object(subprocess, "Popen", return_value=child) as start,
                              patch.object(sys, "stdin", io.StringIO(json.dumps(payload)))):
                            self.assertEqual(turn_end.main([]), 0)
                        start.assert_called_once_with(
                            [sys.executable, str(Path(turn_end.__file__).resolve()), "--handle-hook"],
                            start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
                        handler_input = io.StringIO(pipe.getvalue().decode())
                    # Use the fixture's pane and delivery stubs in the hook handler.
                    with (handler_input, patch.object(sys, "stdin", handler_input),
                          open(os.devnull, "w") as discard, redirect_stderr(discard), redirect_stdout(discard)):
                        self.assertEqual(turn_end.main(["--handle-hook"]), 0)
                else:
                    with ExitStack() as sends:
                        if close == "send-unlocked":
                            send_notice = send.send

                            def send_unlocked(options: send.Options) -> send.Result:
                                with worker_panes.worker_lock(self.case.fixture.parent.id, staff.WorkerName.parse(name)) as acquired:
                                    self.assertIsInstance(acquired, worker_panes.WorkerLockHold)
                                return send_notice(options)

                            _ = sends.enter_context(patch.object(send, "send", side_effect=send_unlocked))
                        expected = (turn_end.TurnOutcome.DONE if transcript == "current"
                                    else turn_end.TurnOutcome.RETRY_EVIDENCE)
                        self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), expected)
                    if transcript != "current":
                        self.assertNotIn((pane, "terminal-" + pane), self.case.fixture.world.closed)
                        self.assertIn(pane, self.case.fixture.world.locations)
                        self.case.agent.append(pane, "assistant", self.case.agent.replies[-1])
                        self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.DONE)
                self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.IGNORED)
                messages = self.case.agent.notices[notices:]
                done = f"{name}: phase 8: done; reply {self.case.reply}"
                self.assertIn(done, messages[0])
                self.assertEqual(len(messages), 1)
                self.assertEqual(self.case.reply.read_text().strip(), self.case.agent.replies[-1])
                self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.NoActiveDispatch)
                self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "completed")
                self.assertIn((pane, "terminal-" + pane), self.case.fixture.world.closed)
                self.assertNotIn(pane, self.case.fixture.world.locations)
                if close == "scratch":
                    self.assertCountEqual(Path(os.environ["TMPDIR"]).iterdir(), kept_folders)
                    own_folder, peer_folder = kept_folders
                    self.assertEqual((own_folder / "trial.txt").read_text(), "worker output")
                    self.assertEqual((peer_folder / "peer.txt").read_text(), "peer output")
                    task = scratch.worker_task(row.id)
                    own_record = scratch.task_of(own_folder, intent=scratch.RemovalIntentAccess.READ_ONLY)
                    peer_record = scratch.task_of(peer_folder, intent=scratch.RemovalIntentAccess.READ_ONLY)
                    assert isinstance(own_record, copy_record.Registered)
                    assert isinstance(peer_record, copy_record.Registered)
                    self.assertEqual(own_record.task, task)
                    self.assertTrue(scratch.task_has_end_evidence(own_record))
                    self.assertFalse(scratch.task_has_end_evidence(peer_record))
                    self.assertEqual([line for line in messages[0].splitlines()
                                      if line.startswith("kept for a manual removal list:")],
                                     [f"kept for a manual removal list: {own_folder} (scratch, task {task} ended)"])
                    # The pass end deletes the worker's finished runs; a director keeps its newest run.
                    runs = self.case.run_dir / "detached_runs"
                    self.assertEqual(sorted(path.name for path in runs.iterdir() if not path.name.startswith(".")),
                                     ["director.json", "director.log"])
                self.assertEqual(staff.by_id(row.id), row)
                log = self.case.fixture.root / "state/herdr/turn-end-ignored.jsonl"
                entry = cast(dict[str, object], json.loads(log.read_text().splitlines()[-1]))
                self.assertEqual((entry["thread_id"], entry["worker_name"], entry["reason"]),
                    (row.current_session, name, "the hook completion is already finished"))

    def test_a_codex_quota_refusal_is_reported_once_with_its_stopped_command(self) -> None:
        for shared_sessions in (False, True):
            with self.subTest(shared_sessions=shared_sessions):
                _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": "codex"}})
                _ = copy_config_file_fixture(Path(__file__).resolve().parents[2] / "config/accounts.conf",
                                             self.case.fixture.root / "conf/accounts.conf")
                self.case.agent.finish_after = 1
                self.assertIs(self.case.dispatch("impl-shared" if shared_sessions else "impl"), worker.DispatchSubmission.SUBMITTED)
                row = self.case.agent.row(self.case.agent.submitted[-1][0])
                backend = staff.backend(row)
                assert isinstance(backend, staff.CodexBackend)
                account_home = backend.codex_home
                if shared_sessions:
                    shared = Path.home() / ".codex" / "sessions"
                    shared.mkdir(parents=True, exist_ok=True)
                    transcript = worker_admission.transcript_source(row, backend)
                    if transcript.parent != shared:
                        _ = shutil.copyfile(transcript, shared / transcript.name)
                        transcript.unlink()
                    account_home = Path.home() / ".codex1"
                    account_home.mkdir(exist_ok=True)
                    folder = account_home / "sessions"
                    if folder.is_dir() and not folder.is_symlink():
                        shutil.rmtree(folder)
                    if not folder.is_symlink():
                        folder.symlink_to(shared, target_is_directory=True)
                    row = staff.set_home(row.id, staff.SessionHome(account_home))
                    backend = staff.CodexBackend(account_home)
                account = agent_accounts.KnownCodexAccount(account_home, "worker@example.com", "codex1")
                refused_at = "2026-11-10T19:30:38.842000+00:00"
                def account_at(home: Path) -> agent_accounts.KnownCodexAccount | agent_accounts.UnknownCodexAccount:
                    return account if home == account_home else agent_accounts.UnknownCodexAccount(home, "fixture has no sign-in")

                with (patch.object(quota_alert, "blocked", return_value=["workers moved"]) as blocked,
                      patch.object(agent_accounts, "codex_account", side_effect=account_at),
                      patch.dict(os.environ, {"CODEX_HOME": str(Path.home() / ".codex") if shared_sessions else str(account_home)}),
                      patch.object(note_file, "read_notes", return_value=iter(()))):
                    self.assertEqual(turn_end.report_quota_refusals(), [])
                    # A later refusal in the same pass sends no second notice.
                    for timestamp, expected in ((refused_at, ["workers moved"]), ("2026-11-10T19:35:00+00:00", [])):
                        with worker_admission.transcript_source(row, backend).open("a", encoding="utf-8") as stream:
                            _ = stream.write(json.dumps({"type": "event_msg", "timestamp": timestamp, "payload": {
                                "type": "task_complete", "last_agent_message": None, "error": {
                                    "message": "You’ve hit your usage limit. Try again at Nov 15th, 2026 1:34 PM.",
                                    "codex_error_info": "usage_limit_exceeded"}}}) + "\n")
                        self.assertEqual(turn_end.report_quota_refusals(), expected)
                        self.assertEqual(turn_end.report_quota_refusals(), [])
                blocked.assert_called_once_with([], account, window=refusals.UnknownWindow(),
                    refused_at=quota_alert.RefusedAt(datetime.fromisoformat(refused_at)),
                    stopped=quota_alert.StoppedWorker(str(self.case.fixture.parent.current_session), row.name))

    def test_turn_end_completes_a_dispatch_outside_the_run_root(self) -> None:
        original_run = self.case.run_dir
        original_state = (original_run / "progress_history_state.json").read_bytes()
        outside = Path(self.enterContext(TemporaryDirectory(prefix="outside-worker-run-")))
        atomic_file.write_text_atomic(outside / "progress_history_state.json", original_state.decode())
        self.case.run_dir = outside
        _ = staff.set_run(self.case.fixture.parent.id, staff.OpenRun(outside))
        opened = self.case.opened()
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(worker_panes.worker_run(opened.row), outside)
        self.assertEqual(len(self.case.fixture.world.launches), 1)
        self.assertEqual(self.case.finish(), turn_end.TurnOutcome.DONE)
        self.assertEqual(worker_panes.worker_pass(opened.row, outside)["status"], "completed")
        self.assertEqual((original_run / "progress_history_state.json").read_bytes(), original_state)
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertIn("phase 8: done", self.case.agent.notices[0])
        self.assertNotIn(opened.pane, self.case.fixture.world.locations)

    def test_completed_rerun_preserves_the_sent_kept_pane_anomaly(self) -> None:
        for family in ("codex", "claude"):
            name = "impl-" + family
            completions: list[str] = []
            for pass_number in (1, 2):
                with self.subTest(family=family, pass_number=pass_number):
                    prompt = self.case.run_dir / f"{name}-{pass_number}.md"
                    atomic_file.write_text_atomic(prompt, "Read the source and preserve its behavior.\n")
                    self.case.prompt = prompt
                    self.case.fixture.world.unknown.clear()
                    _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
                    submitted = len(self.case.agent.submitted)
                    self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                    pane = self.case.agent.submitted[-1][0]
                    row = self.case.agent.row(pane)
                    sender = (send.CodexSender(str(row.current_session)) if family == "codex"
                              else send.ClaudeSender(str(row.current_session)))
                    notices = len(self.case.agent.notices)
                    with patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False):
                        self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
                    notice, diagnostic = self.case.agent.notices[notices:]
                    passed = worker_panes.worker_pass(row, self.case.run_dir)
                    completions.append(notice)
                    self.assertIn(f"; pass {passed['instance_id']}", notice)
                    self.assertIn(f"anomaly: {name}: pane close is incomplete; kept pane {pane}", diagnostic)
                    reply = self.case.reply.read_bytes(), self.case.reply.stat().st_mtime_ns
                    self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                    self.assertEqual(self.case.agent.notices[notices:], [notice, diagnostic])
                    self.assertEqual(self.case.agent.notice_senders[notices:], [sender, sender])
                    self.assertEqual(len(self.case.agent.submitted), submitted + 1)
                    self.assertIn(pane, self.case.fixture.world.locations)
                    self.assertEqual((self.case.reply.read_bytes(), self.case.reply.stat().st_mtime_ns), reply)
            self.assertNotEqual(*completions)

    def test_next_hook_retries_a_held_diagnostic_after_pane_reuse(self) -> None:
        self.assert_retries_held_diagnostic(self.case.finish)

    def test_reap_retries_a_held_diagnostic_after_pane_reuse(self) -> None:
        self.assert_retries_held_diagnostic(lambda: detached_runs.reap_orphans(str(self.case.run_dir)))

    def assert_retries_held_diagnostic(self, retry: Callable[[], object]) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        diagnostic = f"anomaly: impl: pane close is incomplete; kept pane {pane}"
        deliver = send.send
        results = iter((send.Result("failed", "diagnostic delivery failed"),
                        send.Result("held", "diagnostic delivery is held")))
        outcomes: list[str] = []

        def deliver_diagnostic(options: send.Options) -> send.Result:
            if send.body_text(options) != diagnostic:
                return deliver(options)
            result = next(results, None)
            if result is None:
                result = deliver(options)
            outcomes.append(result.outcome)
            return result

        run = self.case.run_dir
        with patch.object(send, "send", side_effect=deliver_diagnostic):
            with (patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False),
                  self.assertRaisesRegex(ValueError, "diagnostic delivery failed")):
                _ = self.case.finish()
            self.case.prompt = run / "follow-up.md"
            atomic_file.write_text_atomic(self.case.prompt, "Read the next assignment.\n")
            before = len(self.case.agent.submitted)
            instance = worker_panes.worker_pass(row, run)["instance_id"]
            self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
            self.assertNotEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)
            self.assertEqual(outcomes, ["failed"])
            self.assertEqual(len(self.case.agent.submitted), before + 1)
            self.assertEqual(self.case.agent.submitted[-1][0], pane)
            notice_path, = run.glob(pass_notices.NOTICE_FILES)
            pending = pass_notices.PassNotices.read(notice_path)
            assert pending is not None
            self.assertFalse(next(notice.settled for notice in pending.notices.values() if notice.text == diagnostic))

            _ = retry()
            _ = retry()

        self.assertEqual(outcomes, ["failed", "held", "sent"])
        self.assertEqual(self.case.agent.notices.count(diagnostic), 1)
        settled = pass_notices.PassNotices.read(notice_path)
        assert settled is not None
        self.assertTrue(all(notice.settled for notice in settled.notices.values()))

    def test_retry_settles_a_held_notice_with_the_director_receipt(self) -> None:
        self.assert_dispatch_with_held_notice(received=True)

    def test_new_assignment_keeps_a_held_notice_without_the_director_receipt(self) -> None:
        self.assert_dispatch_with_held_notice(received=False)

    def test_idle_worker_reuses_a_pane_after_a_finished_pass_with_a_pending_notice(self) -> None:
        run = self.case.run_dir
        for received in (False, True):
            with self.subTest(received=received):
                name = "impl3-receipt" if received else "impl3"
                self.case.prompt = run / f"{name}.md"
                worker.save_reply(self.case.prompt, "Read the first assignment.")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                instance = str(worker_panes.worker_pass(row, run)["instance_id"])
                key = f"worker-completion:{instance}:done"
                with (patch.object(send, "send", return_value=send.Result("held", "delivery is held")),
                      self.assertRaisesRegex(ValueError, "delivery is held")):
                    _ = self.case.finish(name)

                self.case.fixture.world.locations[pane]["agent_status"] = "idle"
                self.case.prompt = run / f"{name}-next.md"
                worker.save_reply(self.case.prompt, "Read the next assignment.")
                with (patch.object(worker_panes, "start_or_resume", side_effect=OSError("launch unavailable")),
                      self.assertRaisesRegex(OSError, "launch unavailable")):
                    _ = self.case.dispatch(name)
                previous = worker_panes.worker_pass(row, run)
                completion = progress_history.hook_completion(previous)
                assert isinstance(completion, progress_history.HookCompletion)
                self.assertEqual((previous["instance_id"], previous["status"]), (instance, "completed"))
                self.assertTrue(completion.finished)
                state = worker_panes.observe_worker(self.case.fixture.parent.id, row, staff.snapshot())
                assert isinstance(state, worker_panes.OpenWorker)
                self.assertEqual(state.status, herdr.AgentStatus.IDLE)
                record = pass_notices.read(run, instance)
                assert record is not None
                self.assertFalse(record.notices["done"].settled)

                if received:
                    receipt = send.Message(send.staff_queue_key(self.case.fixture.parent.id), name, "", "", key)
                    send.record_acceptance(receipt, send.now())
                held = send.RepeatGuardHeld("held", "repeat guard: copy 5 held; owner notice queued")
                with patch.object(send, "send", return_value=held) as transport:
                    self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                    self.assertEqual(self.case.agent.submitted[-1][0], pane)
                    self.assertNotEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)
                    transport.assert_not_called()
                    _ = detached_runs.reap_orphans(str(run))
                    self.assertTrue(pass_notices.is_settled(run, instance, "done"))
                    _ = detached_runs.reap_orphans(str(run))
                    if received:
                        transport.assert_not_called()
                    else:
                        transport.assert_called_once()
                        self.assertEqual(cast(send.Options, transport.call_args.args[0]).key, key)
                self.assertIs(self.case.finish(name), turn_end.TurnOutcome.DONE)

    def test_recording_failure_remains_recoverable_if_the_notice_write_fails(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        diagnostic = "The run record could not close."
        record_results = (assign.CompletionFailed(diagnostic), assign.CompletionApplied.APPLIED)
        append = pass_notices._append  # pyright: ignore[reportPrivateUsage]
        write = os.write

        def short_write(descriptor: int, data: bytes) -> int:
            return write(descriptor, data[:len(data) // 2])

        def fail_diagnostic_write(path: Path, event: dict[str, object]) -> None:
            notices = pass_recorder.object_dict(event.get("notices")) or {}
            if any(key.startswith("diagnostic:") for key in notices):
                with patch.object(os, "write", side_effect=short_write):
                    append(path, event)
            else:
                append(path, event)

        with patch.object(assign, "finish_herdr_recording", side_effect=record_results) as record:
            with (patch.object(pass_notices, "_append", side_effect=fail_diagnostic_write),
                  self.assertRaisesRegex(OSError, "incomplete pass notice write")):
                _ = self.case.finish()
            saved = progress_history.hook_completion(worker_panes.worker_pass(row, run))
            assert isinstance(saved, progress_history.HookCompletion)
            self.assertFalse(saved.recording_done)
            self.assertFalse(saved.finished)
            self.assertEqual(saved.pending_diagnostics, (diagnostic,))
            self.assertEqual(self.case.finish(), turn_end.TurnOutcome.DONE)
            self.assertEqual(record.call_count, 2)
            saved = progress_history.hook_completion(worker_panes.worker_pass(row, run))
            assert isinstance(saved, progress_history.HookCompletion)
            self.assertTrue(saved.recording_done)
            self.assertEqual(saved.pending_diagnostics, ())

        notice_path, = run.glob(pass_notices.NOTICE_FILES)
        entry = pass_notices.PassNotices.read(notice_path)
        assert entry is not None
        self.assertEqual(len(entry.notices), 2)
        self.assertTrue(all(notice.settled for notice in entry.notices.values()))
        self.assertEqual(self.case.agent.notices.count(diagnostic), 1)

    def test_pending_notice_does_not_block_a_stopped_or_closed_worker(self) -> None:
        for closed in (False, True):
            with self.subTest(closed=closed):
                name = "closed" if closed else "stopped"
                run = self.case.run_dir
                self.case.prompt = run / f"{name}.md"
                worker.save_reply(self.case.prompt, "Read the first assignment.")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                instance = worker_panes.worker_pass(row, run)["instance_id"]
                with patch.object(send, "send", return_value=send.Result("failed", "director unavailable")) as deliver:
                    with self.assertRaisesRegex(ValueError, "director unavailable"):
                        _ = self.case.finish(name)
                    if closed:
                        self.case.fixture.world.remove_pane(pane)
                    else:
                        self.assertTrue(self.case.fixture.world.stop(row, self.case.fixture.world))
                    observed = worker_panes.observe_worker(self.case.fixture.parent.id, row, staff.snapshot())
                    self.assertIsInstance(observed, worker_panes.ClosedWorker if closed else worker_panes.StoppedWorker)
                    self.case.prompt = run / f"{name}-next.md"
                    worker.save_reply(self.case.prompt, "Read the next assignment.")
                    before = len(self.case.agent.submitted)
                    self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                    deliver.assert_called_once()
                    next_pane = self.case.agent.submitted[-1][0]
                    next_row = self.case.agent.row(next_pane)
                    self.assertEqual(len(self.case.agent.submitted), before + 1)
                    self.assertNotEqual(worker_panes.worker_pass(next_row, run)["instance_id"], instance)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)

    def test_rerun_and_reap_use_one_pending_notice_after_a_failed_launch(self) -> None:
        prompt = self.case.prompt
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        instance = worker_panes.worker_pass(row, run)["instance_id"]
        self.case.agent.notice_failures = 1
        with self.assertRaisesRegex(ValueError, "director message failed"):
            _ = self.case.finish()
        self.case.prompt = run / "next.md"
        worker.save_reply(self.case.prompt, "Read the next assignment.")
        with (patch.object(worker_panes, "start_or_resume", side_effect=OSError("launch unavailable")),
              self.assertRaisesRegex(OSError, "launch unavailable")):
            _ = self.case.dispatch()
        self.assertEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)
        self.case.prompt = prompt
        with patch.object(send, "send", wraps=send.send) as transport:
            self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
            _ = detached_runs.reap_orphans(str(run))
            transport.assert_called_once()
            self.assertEqual(cast(send.Options, transport.call_args.args[0]).key, f"worker-completion:{instance}:done")
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertEqual(len(self.case.agent.submitted), 1)

    def assert_dispatch_with_held_notice(self, *, received: bool) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        key = f"worker-completion:{instance}:done"
        with (patch.object(send, "STATE", self.case.fixture.root / "message-state"),
              patch.object(send, "send", return_value=send.Result("held", "completion delivery is held")) as deliver):
            with self.assertRaisesRegex(ValueError, "completion delivery is held"):
                _ = self.case.finish()
            notice_path, = run.glob(pass_notices.NOTICE_FILES)
            notice = cast(send.Options, deliver.call_args.args[0])
            self.assertEqual(notice.key, key)
            if received:
                receipt = send.Message(send.staff_queue_key(self.case.fixture.parent.id), row.name, "", "", key)
                send.record_acceptance(receipt, send.now())

            self.case.fixture.world.locations[pane]["agent_status"] = "idle"
            self.case.prompt = run / "follow-up.md"
            atomic_file.write_text_atomic(self.case.prompt, "Read the next assignment.\n")
            before = len(self.case.agent.submitted)
            with (patch.object(send, "confirmed_skip", side_effect=OSError("message lock unavailable")) as receipts,
                  patch.object(pass_notices.PassNotices, "read", side_effect=OSError("notice record unavailable"))):
                self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
                receipts.assert_not_called()
            self.assertNotEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)
            self.assertEqual(len(self.case.agent.submitted), before + 1)
            self.assertEqual(self.case.agent.submitted[-1][0], pane)
            saved = pass_notices.PassNotices.read(notice_path)
            assert saved is not None
            self.assertFalse(saved.notices["done"].settled)
            deliver.assert_called_once()
            with patch.object(send, "confirmed_skip", side_effect=OSError("message lock unavailable")) as receipts:
                _ = detached_runs.reap_orphans(str(run))
                receipts.assert_called_once()
            self.assertFalse(pass_notices.is_settled(run, instance, "done"))
            deliver.assert_called_once()

            deliver.return_value = send.RepeatGuardHeld("held", "repeat guard: copy 5 held; owner notice queued")
            _ = detached_runs.reap_orphans(str(run))
            _ = detached_runs.reap_orphans(str(run))
            saved = pass_notices.PassNotices.read(notice_path)
            assert saved is not None
            self.assertTrue(saved.notices["done"].settled)
            self.assertEqual(deliver.call_count, 1 if received else 2)

    def test_assignment_reports_pending_pane_cleanup_before_exit_eight(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        with patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False):
            self.assertEqual(self.case.finish(), turn_end.TurnOutcome.DONE)
        before = self.case.agent.submitted.copy()
        self.case.prompt = self.case.run_dir / "follow-up.md"
        atomic_file.write_text_atomic(self.case.prompt, "Read the next assignment.\n")
        with (patch.object(turn_end, "recover_completion", return_value=worker.CompletionRecovery.PENDING_CLOSE),
              redirect_stderr(io.StringIO()) as errors):
            self.assertIs(self.case.dispatch(), worker.ExitCode.UNCERTAIN)
        self.assertEqual(errors.getvalue().splitlines(), ["impl: completion has pending pane cleanup"])
        self.assertEqual(self.case.agent.submitted, before)

    def test_completion_retries_check_saved_scratch_paths(self) -> None:
        for attempt in ("hook", "resend", "reap"):
            with self.subTest(attempt=attempt):
                name = f"impl-{attempt}"
                run = self.case.run_dir
                self.case.prompt = run / f"{name}.md"
                worker.save_reply(self.case.prompt, "Read the assignment.")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                root = self.case.fixture.root.resolve()
                relative = root / f"{name} relative"
                missing = root / f"{name} scratch"
                retained = root / f"{name} retained"
                relative.mkdir()
                missing.mkdir()
                retained.symlink_to(root / "missing-target")
                parent = root / "nested"
                parent.mkdir(exist_ok=True)
                reason = "The task ended (scratch, a fixture reason)."
                folders = [scratch.KeptFolder(Path(os.path.relpath(relative)), "task ended"),
                           scratch.KeptFolder(missing, reason),
                           scratch.KeptFolder(parent / ".." / retained.name, reason)]
                missing_notices = (scratch.retained_task_folder_notice(relative, "task ended"),
                                   scratch.retained_task_folder_notice(missing, reason))
                retained_notice = scratch.retained_task_folder_notice(retained, reason)
                self.case.agent.notice_failures = 1
                with (patch.object(scratch, "end", return_value=folders),
                      patch.object(send, "send", wraps=send.send) as transport,
                      self.assertRaisesRegex(ValueError, "director message failed")):
                    _ = self.case.finish(name)
                initial = send.body_text(cast(send.Options, transport.call_args.args[0]))
                for notice in (*missing_notices, retained_notice):
                    self.assertIn(notice, initial)

                for folder in (relative, missing):
                    _ = folder.rename(root / f"{folder.name} moved")
                before = len(self.case.agent.notices)
                with patch.object(scratch, "end", return_value=[]):
                    if attempt == "hook":
                        self.assertIs(self.case.finish(name), turn_end.TurnOutcome.DONE)
                    elif attempt == "resend":
                        self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                    else:
                        _ = detached_runs.reap_orphans(str(run))
                self.assertEqual(len(self.case.agent.notices), before + 1)
                notice = self.case.agent.notices[-1]
                for missing_notice in missing_notices:
                    self.assertNotIn(missing_notice, notice)
                self.assertIn(retained_notice, notice)

    def test_retry_with_invalid_scratch_paths_does_not_send(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        row = self.case.agent.row(self.case.agent.submitted[-1][0])
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        retained = self.case.fixture.root / "retained"
        retained.mkdir()
        reason = "The task ended."
        self.case.agent.notice_failures = 1
        with (patch.object(scratch, "end", return_value=[scratch.KeptFolder(retained, reason)]),
              self.assertRaisesRegex(ValueError, "director message failed")):
            _ = self.case.finish()
        saved_paths, = run.glob(".*.completion-scratch.json")
        valid = saved_paths.read_text()
        oversized = str(retained.parent / ("x" * 512))
        invalid_records = (
            ("invalid JSON", "{"),
            ("relative path", json.dumps({str(retained): reason, "relative": reason})),
            ("oversized path component", json.dumps({str(retained): reason, oversized: reason})),
        )
        for case, invalid in invalid_records:
            with self.subTest(case=case):
                atomic_file.write_text_atomic(saved_paths, invalid)
                with (patch.object(send, "send", wraps=send.send) as transport,
                      patch.object(unit_runs, "report_anomaly") as anomaly):
                    _ = detached_runs.reap_orphans(str(run))
                transport.assert_not_called()
                anomaly.assert_called_once()
                self.assertFalse(pass_notices.is_settled(run, instance, "done"))

        atomic_file.write_text_atomic(saved_paths, valid)
        _ = detached_runs.reap_orphans(str(run))
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertIn(scratch.retained_task_folder_notice(retained, reason), self.case.agent.notices[0])
        self.assertTrue(pass_notices.is_settled(run, instance, "done"))

    def test_hook_recovers_a_saved_completion_with_legacy_scratch_text(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        row = self.case.agent.row(self.case.agent.submitted[-1][0])
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        retained = self.case.fixture.root / "retained"
        retained.mkdir()
        legacy = scratch.retained_task_folder_notice(retained, "The task ended.")
        diagnostic = "The fixture saved another diagnostic."
        worker.save_reply(self.case.reply, self.case.agent.replies[-1])
        progress_history.save_hook_completion(run, instance, progress_history.HookCompletion(
            turn_end.TurnOutcome.DONE, str(self.case.reply), (legacy, diagnostic)))
        self.assertIsNone(pass_notices.read(run, instance))

        self.assertIs(self.case.finish(), turn_end.TurnOutcome.DONE)
        notice, = self.case.agent.notices
        self.assertNotIn(legacy, notice)
        self.assertIn(diagnostic, notice)
        saved = pass_notices.read(run, instance)
        assert saved is not None
        self.assertNotIn(legacy, saved.notices["done"].text)
        self.assertTrue(saved.notices["done"].settled)

    def test_retry_omits_legacy_scratch_text_and_preserves_other_details(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        row = self.case.agent.row(self.case.agent.submitted[-1][0])
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        root = self.case.fixture.root.resolve()
        retained = root / "retained"
        retained.mkdir()
        legacy = scratch.retained_task_folder_notice(retained, "The task ended.")
        other_notice = f"kept for a manual removal list: {root / 'removed'} (remove_run_scratch, the run ended)"
        diagnostic = "The fixture saved another diagnostic."
        message = "\n".join(("impl: phase 8: done", legacy, other_notice, diagnostic))
        route = pass_notices.Route(instance, self.case.fixture.parent.id, row.name)
        pass_notices.stage_result(run, route, "done",
                                  pass_notices.Notice(message, f"codex:{row.current_session}", pass_notices.NoticeKind.RESULT),
                                  legacy=pass_notices.LegacyNoticeStamp("", ""))
        _ = detached_runs.reap_orphans(str(run))
        self.assertEqual(self.case.agent.notices, ["\n".join(("impl: phase 8: done", other_notice, diagnostic))])
        self.assertTrue(pass_notices.is_settled(run, instance, "done"))

    def test_a_director_or_an_idle_worker_hook_changes_no_pass(self) -> None:
        opened = self.case.opened()
        row = opened.row
        before = (self.case.run_dir / "progress_history_state.json").read_bytes()
        log = self.case.fixture.root / "state/herdr/turn-end-ignored.jsonl"
        for identity, name, reason in (
                (self.case.fixture.parent.current_session, self.case.fixture.parent.name, "the staff row is not a worker"),
                (row.current_session, row.name, "no active dispatch or recoverable completion exists"),
                ("unknown-thread", "unknown", "no staff row for the current thread"),
                (turn_end.hook_event({"type": "agent-turn-complete", "thread-id": str(row.current_session)}).thread_id,
                 "unknown", "no staff row for the current thread"),
                ("", "unknown", "no staff row for the current thread")):
            with self.subTest(identity=identity):
                self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(identity))), turn_end.TurnOutcome.IGNORED)
                entry = cast(dict[str, object], json.loads(log.read_text().splitlines()[-1]))
                self.assertEqual((entry["thread_id"], entry["worker_name"], entry["reason"]), (identity, name, reason))
        self.assertEqual(len(log.read_text().splitlines()), 5)
        self.assertEqual((self.case.run_dir / "progress_history_state.json").read_bytes(), before)
        self.assertEqual(self.case.agent.notices, [])
        self.assertEqual(self.case.agent.submitted, [])
        self.assertNotIn(opened.pane, self.case.fixture.world.locations)
        self.assertEqual(self.case.fixture.world.closed, [(opened.pane, opened.terminal)])

        for submission, reply, count in (
                ("startup", "Ready.", 0), ("unknown thread", "The check is complete.\nDONE missing-pass", 0),
                ("submitted worker", "The check is complete.\nDONE missing-pass", 1),
                ("submitted worker replies Ready", "Ready.", 1)):
            with self.subTest(missing_staff_reply=submission):
                name = submission.replace(" ", "-").lower()
                if submission.startswith("submitted worker"):
                    self.case.agent.finish_after = 100
                    self.assertIs(worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "impl",
                        self.case.prompt, session_dir=self.case.run_dir, slot=name, phase="8", kind="impl",
                        launcher_command=["assign.sh", name], reply=self.case.reply), worker.DispatchSubmission.SUBMITTED)
                    worker_row = self.case.agent.row(self.case.agent.submitted[-1][0])
                elif submission == "startup":
                    worker_row = self.case.opened(name).row
                else:
                    worker_row = None
                membership = staff.snapshot()
                missing = replace(membership, rows=tuple(row for row in membership.rows if row != worker_row))
                thread = str(worker_row.current_session) if worker_row is not None else "unknown-thread"
                notices = len(self.case.agent.notices)
                event = turn_end.hook_event({"hook_event_name": "Stop", "session_id": thread,
                    "cwd": str(self.case.fixture.root), "last_assistant_message": reply})
                with (staff.supplied_snapshot(missing),
                      patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.case.run_dir)})):
                    self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.IGNORED)
                self.assertEqual(len(self.case.agent.notices), notices + count)
                if count:
                    message = self.case.agent.notices[-1]
                    self.assertIn(f"Thread {thread}", message)
                    self.assertIn(f"Directory {self.case.fixture.root}", message)
                    self.assertTrue(message.endswith(reply))
        self.case.agent.finish_after = 0

        for cause, event, closes in (
                ("empty delivery", turn_end.EventKind.TURN_END, True),
                ("missing prompt", turn_end.EventKind.TURN_END, False),
                ("ended pass", turn_end.EventKind.TURN_END, True),
                ("working turn end", turn_end.EventKind.TURN_END, True),
                ("live run", turn_end.EventKind.TURN_END, False),
                ("pending bootstrap", turn_end.EventKind.TURN_END, False),
                ("no pending assignment", turn_end.EventKind.TURN_END, True),
                ("live launcher", turn_end.EventKind.TURN_END, False),
                ("pending assignment", turn_end.EventKind.TURN_END, False),
                ("empty delivery", turn_end.EventKind.RUN_FINISHED, False),
                ("empty delivery", turn_end.EventKind.STAGE_READY, False)):
            with self.subTest(cause=cause, event=event), ExitStack() as cleanup:
                name = (cause + "-" + event.value).replace(" ", "-")
                opened = self.case.opened(name)
                if cause == "pending assignment":
                    self.case.agent.finish_after = 100
                    self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                elif cause not in ("pending bootstrap", "no pending assignment"):
                    instance = progress_history.begin_dispatch(self.case.run_dir, slot=name, phase="8", role="impl",
                        worker_name=name, worker_id=opened.row.id, launcher_command=["assign.sh"], prompt=self.case.prompt, kind="impl", lens="")
                    if cause == "missing prompt":
                        progress_history.save_dispatch_delivery(self.case.run_dir, instance,
                            pass_recorder.DispatchDelivery(f"Read the task. DONE {instance}", instance,
                                                              str(self.case.reply)))
                    elif cause == "ended pass":
                        progress_history.complete_dispatch(self.case.run_dir, name, instance, status="error")
                    elif cause == "live launcher":
                        supervisor = cleanup.enter_context(subprocess.Popen(
                            [sys.executable, "-c", "import sys; sys.stdin.read()", "/fixture/assign.py", "impl"],
                            stdin=subprocess.PIPE, text=True))
                        _ = cleanup.callback(supervisor.kill)
                        assert supervisor.stdin is not None
                        _ = cleanup.callback(supervisor.stdin.close)
                        path = self.case.run_dir / "progress_history_state.json"
                        fields = cast(dict[str, object], json.loads(path.read_text()))
                        passed = cast(dict[str, dict[str, object]], fields["pass"])[name]
                        passed.update(launcher_pid=supervisor.pid, launcher_command=["bash", "/fixture/assign.sh", "impl"])
                        atomic_file.write_text_atomic(path, json.dumps(fields))
                    elif cause == "live run":
                        path, record = self.case.detached_record(opened.row, "idle-live-run")
                        supervisor = cleanup.enter_context(subprocess.Popen(
                            [sys.executable, "-c", "import sys; sys.stdin.read()", "detached_runs.py", "supervise", str(path)],
                            stdin=subprocess.PIPE, text=True))
                        _ = cleanup.callback(supervisor.kill)
                        assert supervisor.stdin is not None
                        _ = cleanup.callback(supervisor.stdin.close)
                        record.update(status="running", pid=supervisor.pid)
                        atomic_file.write_text_atomic(path, json.dumps(record))
                self.case.fixture.world.locations[opened.pane]["agent_status"] = (
                    "working" if cause == "working turn end" else "idle")
                notices = list(self.case.agent.notices)
                if cause == "pending bootstrap":
                    with ThreadPoolExecutor(max_workers=1) as hooks:
                        with worker_panes.worker_lock(self.case.fixture.parent.id, staff.WorkerName.parse(name)) as hold:
                            self.assertIsInstance(hold, worker_panes.WorkerLockHold)
                            pending = hooks.submit(turn_end.handle, turn_end.WorkerEvent(str(opened.row.current_session), reply="ready"))
                            with self.assertRaises(TimeoutError):
                                _ = pending.result(timeout=0.05)
                            instance = progress_history.begin_dispatch(self.case.run_dir, slot=name, phase="8", role="impl",
                                worker_name=name, worker_id=opened.row.id, launcher_command=["assign.sh"],
                                prompt=self.case.prompt, kind="impl", lens="")
                            progress_history.save_dispatch_delivery(self.case.run_dir, instance,
                                pass_recorder.DispatchDelivery("The pending assignment.", instance, str(self.case.reply)))
                            before = (self.case.run_dir / "progress_history_state.json").read_bytes()
                        self.assertEqual(pending.result(timeout=LauncherHangGuard("queued bootstrap hook").remaining()),
                                         turn_end.TurnOutcome.IGNORED)
                else:
                    before = (self.case.run_dir / "progress_history_state.json").read_bytes()
                    expected = turn_end.TurnOutcome.NO_DONE if cause == "pending assignment" else turn_end.TurnOutcome.IGNORED
                    self.assertEqual(self.case.finish(name, event), expected)
                self.assertEqual(opened.pane not in self.case.fixture.world.locations, closes)
                saved = (self.case.run_dir / "progress_history_state.json").read_bytes()
                if cause == "pending assignment":
                    self.assertEqual(saved, before)
                    self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.NO_DONE)
                    self.assertEqual(len(self.case.agent.notices), len(notices) + 1)
                    self.assertIn("phase 8: no DONE", self.case.agent.notices[-1])
                else:
                    self.assertEqual(saved, before)
                    self.assertEqual(self.case.agent.notices, notices)
                self.assertEqual(staff.by_id(opened.row.id), opened.row)

        with self.subTest(cause="consultation awaits its reply"):
            caller = self.case.opened("consultation-caller")
            with patch.dict(os.environ, {"HERDR_PANE_ID": caller.pane, "CODEX_THREAD_ID": str(caller.row.current_session)}):
                consultation = side_pane.open("private-consultation", worker_panes.launch_policy("impl"),
                                              cwd=self.case.fixture.root)
            self.assertIsInstance(consultation, side_pane.OpenSidePane)
            assert isinstance(consultation, side_pane.OpenSidePane)
            pid = self.case.fixture.world.foreground[consultation.pane][0]
            process = self.case.fixture.world.processes[pid]
            process = replace(process, identity=herdr.PaneSessionReport(process.identity.id))
            self.case.fixture.world.processes[pid] = process
            self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(process.current_identity))), turn_end.TurnOutcome.IGNORED)
            self.assertIn(consultation.pane, self.case.fixture.world.locations)

    def test_a_stopped_worker_pane_held_by_a_detached_run_closes_when_the_run_ends(self) -> None:
        opened = self.case.opened("held")
        world = self.case.fixture.world
        _ = progress_history.begin_dispatch(self.case.run_dir, slot="held", phase="8", role="impl", worker_name="held",
            worker_id=opened.row.id, launcher_command=["assign.sh"], prompt=self.case.prompt, kind="impl", lens="")
        _ = world.stop(opened.row, world)
        self.assertEqual(progress_history.cancel_worker_passes(self.case.run_dir, opened.row.id), 1)
        path, record = self.case.detached_record(opened.row, "held-run")
        with ExitStack() as cleanup:
            supervisor = cleanup.enter_context(subprocess.Popen(
                [sys.executable, "-c", "import sys; sys.stdin.read()", "detached_runs.py", "supervise", str(path)],
                stdin=subprocess.PIPE, text=True))
            _ = cleanup.callback(supervisor.kill)
            assert supervisor.stdin is not None
            _ = cleanup.callback(supervisor.stdin.close)
            record.update(status="running", pid=supervisor.pid)
            atomic_file.write_text_atomic(path, json.dumps(record))
            self.assertEqual(worker_panes.close(self.case.fixture.parent.id, staff.WorkerName.parse("held")),
                             worker_panes.Kept("a detached run is live"))
            self.assertEqual(self.case.finish("held", turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_RUNS)
            self.assertIn(opened.pane, world.locations)
        record.update(status="finished", exit_code=0)
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.assertEqual(self.case.finish("held", turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.STOPPED)
        self.assertNotIn(opened.pane, world.locations)
        self.assertEqual(world.closed[-1], (opened.pane, opened.terminal))

    def test_ready_hook_waits_from_publication_through_dispatch(self) -> None:
        for family in ("codex", "claude"):
            with self.subTest(family=family), ThreadPoolExecutor(max_workers=1) as hooks:
                _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
                name = "launching-" + family
                launcher = current_thread()
                hook_started = Event()
                pending: list[Future[turn_end.TurnOutcome]] = []
                publication_checked = False
                acquire = worker_panes.worker_lock
                bind = worker_panes.WorkerLockHold.bind
                guard = LauncherHangGuard("hook before fixed-id binding")
                closed = self.case.fixture.world.closed.copy()

                @contextmanager
                def acquire_lock(parent: staff.StaffId, name: staff.WorkerName, *, blocking: bool = False,
                                 deadline: float = worker_lifecycle.NO_LOCK_DEADLINE
                                 ) -> Generator[worker_panes.WorkerLockHold | worker_panes.Kept]:
                    if current_thread() is not launcher:
                        hook_started.set()
                    with acquire(parent, name, blocking=blocking, deadline=deadline) as hold:
                        yield hold

                def before_bind(hold: worker_panes.WorkerLockHold, row: staff.StaffRow, *,
                                deadline: float = worker_lifecycle.NO_LOCK_DEADLINE) -> None:
                    nonlocal publication_checked
                    if current_thread() is launcher and not publication_checked:
                        publication_checked = True
                        self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir), {})
                        with acquire(hold.parent, hold.name) as competing:
                            self.assertIsInstance(competing, worker_panes.Kept)
                        pending.append(hooks.submit(turn_end.handle,
                            turn_end.WorkerEvent(str(row.current_session), reply="ready")))
                        self.assertTrue(hook_started.wait(guard.remaining()))
                        self.assertFalse(pending[0].done())
                    bind(hold, row, deadline=deadline)

                def during_prompt() -> None:
                    self.assertEqual(len(pending), 1)
                    self.assertFalse(pending[0].done())
                    self.assertEqual(self.case.fixture.world.closed, closed)

                self.case.agent.finish_after = 100
                with (patch.object(worker_panes, "worker_lock", acquire_lock),
                      patch.object(worker_panes.WorkerLockHold, "bind", before_bind),
                      patch.object(self.case.agent, "during_prompt", during_prompt)):
                    self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                self.assertEqual(pending[0].result(timeout=guard.remaining()), turn_end.TurnOutcome.IGNORED)
                pane = self.case.agent.submitted[-1][0]
                self.assertIn(pane, self.case.fixture.world.locations)
                self.assertEqual(self.case.fixture.world.closed, closed)
                self.case.agent.complete(pane)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
                self.assertEqual(self.case.fixture.world.closed, [*closed, (pane, "terminal-" + pane)])

    def test_both_completion_events_apply_the_finished_run_exception(self) -> None:
        cases = [(event, reply_state) for event in (turn_end.EventKind.TURN_END, turn_end.EventKind.RUN_FINISHED)
                 for reply_state in ("done", "waiting for delivery", "waiting for reply")]
        cases.append((turn_end.EventKind.RUN_FINISHED, "empty reply"))
        cases.append((turn_end.EventKind.RUN_FINISHED, "active result turn"))
        for index, (event, reply_state) in enumerate(cases):
            with self.subTest(event=event, reply_state=reply_state):
                name = f"impl{index + 2}"
                self.case.reply = self.case.run_dir / f"{name}-reply.md"
                self.case.agent.without_done = reply_state != "done"
                self.case.agent.finish_after = int(reply_state == "empty reply")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                if reply_state == "empty reply":
                    self.case.fixture.world.locations[pane]["agent_status"] = "idle"
                if reply_state == "active result turn":
                    path, record = self.case.detached_record(row, "active-result-check")
                    record["delivery"] = f"sent: worker {row.name} at {pane}"
                    atomic_file.write_text_atomic(path, json.dumps(record))
                    self.case.agent.append(pane, "user", "Detached run: active-result-check\nRun finished: private verification")
                    self.case.fixture.world.locations[pane]["agent_status"] = "working"
                    before = len(self.case.agent.notices)
                    self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.WAITING_ON_REPLY)
                    self.assertEqual(len(self.case.agent.notices), before)
                    self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                    self.case.agent.without_done = False
                    self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
                    self.assertEqual(len(self.case.agent.notices), before + 1)
                    self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.NoActiveDispatch)
                    self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "completed")
                elif reply_state == "done":
                    path, record = self.case.detached_record(row, "private-check")
                    record["delivery"] = f"sent: worker {row.name} at {pane}"
                    record["delivered_at"] = datetime.now().timestamp()
                    atomic_file.write_text_atomic(path, json.dumps(record))
                    self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.DONE)
                    anomaly = "run private-check finished, but its result is not in the transcript"
                    self.assertEqual(self.case.agent.notices[-1].count(anomaly), 1)
                    self.assertIn("DONE ", self.case.reply.read_text())
                else:
                    notices: list[str] = []
                    for number in (1, 2):
                        run_id = f"{name}-check-{number}"
                        path, record = self.case.detached_record(row, run_id)
                        record["delivery"] = f"sent: worker {row.name} at {pane}"
                        atomic_file.write_text_atomic(path, json.dumps(record))
                        if reply_state != "waiting for delivery":
                            self.case.agent.append(pane, "user", f"Detached run: {run_id}\nRun finished: private verification")
                        before = len(self.case.agent.notices)
                        for current_event in (event, event):
                            self.assertEqual(self.case.finish(name, current_event), turn_end.TurnOutcome.WAITING_ON_REPLY)
                            self.assertEqual(len(self.case.agent.notices), before + 1)
                        notice = self.case.agent.notices[-1]
                        self.assertIn("; runs ", notice)
                        self.assertIn(run_id, notice.partition("; runs ")[2].splitlines()[0])
                        notices.append(notice)
                        turn_path = self.case.reply.with_name(self.case.reply.stem + "_turn" + self.case.reply.suffix)
                        if reply_state == "empty reply":
                            self.assertFalse(turn_path.exists())
                            self.assertNotIn("; reply ", notice)
                        else:
                            self.assertIn(f"; reply {turn_path}", notice)
                            self.assertEqual(turn_path.read_text().strip(), self.case.agent.final)
                        self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                        if reply_state == "waiting for delivery":
                            self.case.agent.append(pane, "user", f"Detached run: {run_id}\nRun finished: private verification")
                        if reply_state != "empty reply":
                            self.case.agent.append(pane, "assistant", self.case.agent.final)
                    self.assertNotEqual(*notices)
                    self.case.agent.without_done = False
                    self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)

    def test_a_live_run_keeps_done_active_for_both_completion_events(self) -> None:
        for receipt in ("done", "awaiting reply", "launcher error"):
            with self.subTest(receipt=receipt):
                name = "impl-" + receipt.replace(" ", "-")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                assert isinstance(active, progress_history.ActiveDispatch)
                path, record = self.case.detached_record(row, "private-check-" + name)
                notices = len(self.case.agent.notices)
                reply = self.case.agent.replies[-1]
                if receipt == "awaiting reply":
                    self.case.agent.append(pane, "user", "Detached run: earlier-check\nRun finished: an earlier check")
                with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", "detached_runs.py", "supervise", str(path)],
                                      stdin=subprocess.PIPE, text=True) as supervisor:
                    record.update(status="running", pid=supervisor.pid)
                    atomic_file.write_text_atomic(path, json.dumps(record))
                    for event in (turn_end.EventKind.TURN_END, turn_end.EventKind.RUN_FINISHED):
                        self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.WAITING_ON_RUNS)
                        self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                        self.assertNotIn(pane, [closed[0] for closed in self.case.fixture.world.closed])
                        turn_path = self.case.reply.with_name(self.case.reply.stem + "_turn" + self.case.reply.suffix)
                        self.assertEqual(turn_path.read_text().strip(), reply)
                        self.assertEqual(len(self.case.agent.notices), notices + 1)
                    if receipt == "launcher error":
                        progress_history.complete_dispatch(self.case.run_dir, "impl", active.instance_id, status="error")
                    if receipt == "awaiting reply":
                        reply = "The final check is still running."
                        self.case.agent.append(pane, "assistant", reply)
                        for event in (turn_end.EventKind.TURN_END, turn_end.EventKind.RUN_FINISHED):
                            self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.WAITING_ON_RUNS)
                            self.assertEqual(len(self.case.agent.notices), notices + 2)
                    self.case.agent.append(pane, "user", f"Detached run: {record['run_id']}\nRun finished: the final check")
                    record.update(status="finished", delivery=f"sent: worker {row.name} at {pane}")
                    atomic_file.write_text_atomic(path, json.dumps(record))
                    if receipt == "awaiting reply":
                        for event in (turn_end.EventKind.RUN_FINISHED, turn_end.EventKind.RUN_FINISHED):
                            self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.WAITING_ON_REPLY)
                            self.assertEqual(len(self.case.agent.notices), notices + 3)
                            self.assertEqual(turn_path.read_text().strip(), reply)
                    self.case.agent.complete(pane)
                    if receipt == "launcher error":
                        self.assertEqual(self.case.finish(name, turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.DONE)
                        self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "error")
                        self.assertIn("The earlier pass ended with state error.", self.case.agent.notices[-1])
                    else:
                        self.assertEqual(self.case.finish(name, turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.DONE)
                        self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "completed")
                    self.assertNotIn(pane, self.case.fixture.world.locations)
                    assert supervisor.stdin is not None
                    supervisor.stdin.close()
                    _ = supervisor.wait(timeout=LauncherHangGuard("private run exit").remaining())
        self.assertEqual(len(self.case.agent.submitted), 3)
        self.assertEqual(self.case.agent.waited, [])

        source = Path(__file__).resolve().parents[2] / "config"
        for config in ("detached_runs.conf", "postmaster.conf", "codex_daemon.conf"):
            _ = shutil.copyfile(source / config, self.case.fixture.root / "conf" / config)
        for case in ("done", "result", "retry-close", "role-change", "stage-one"):
            with self.subTest(park=case):
                self.check_parked_pass(case)

    def check_parked_pass(self, case: Literal["done", "result", "retry-close", "role-change", "stage-one"]) -> None:
        done = case in ("done", "stage-one")
        name = f"parked-{case}"
        run = self.case.run_dir
        ready = run / f"{name}.ready"
        self.case.reply = self.case.run_dir / f"{name}.md"
        self.case.agent.finish_after = 1
        registry = self.case.fixture.root / "conf/models.conf"
        _ = write_registry(registry, {"assignments": {"direct": "codex"}}, update=True)
        role: staff.WorkerRole = "review" if case == "stage-one" else "impl"
        launched = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), role, self.case.prompt,
            session_dir=run, slot=name, phase="8", kind=role, launcher_command=[name],
            early_ready=ready if case == "stage-one" else worker.SINGLE_STAGE_REVIEW, reply=self.case.reply)
        self.assertIs(launched, worker.DispatchSubmission.SUBMITTED)
        pane, _ = self.case.agent.submitted[-1]
        row = self.case.agent.row(pane)
        original_family, original_args, _ = self.case.fixture.world.requests[-1]
        active = progress_history.active_dispatch(row.id, self.case.run_dir)
        assert isinstance(active, progress_history.ActiveDispatch)
        path, record = self.case.detached_record(row, name)
        atomic_file.write_text_atomic(Path(record["log"]), "The final type check passed.\n")
        label = "TEST final type check"
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", "detached_runs.py", "supervise", str(path)],
                              stdin=subprocess.PIPE, text=True) as supervisor:
            try:
                record.update(status="running", pid=supervisor.pid, label=label, pane_dispatch_id=active.instance_id)
                atomic_file.write_text_atomic(path, json.dumps(record))
                self.case.agent.without_done = not done
                self.case.agent.complete(pane)
                self.case.agent.without_done = False
                final = self.case.agent.replies[-1]
                if case == "retry-close":
                    close = self.case.fixture.world.close_terminal_pane

                    def fail_close(pane_id: str, terminal: str) -> bool:
                        return pane_id != pane and close(pane_id, terminal)

                    with patch.object(self.case.fixture.world, "close_terminal_pane", side_effect=fail_close):
                        self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.WAITING_ON_RUNS)
                        self.assertIn(pane, self.case.fixture.world.locations)
                        self.assertIs(worker.restore(self.case.fixture.parent.id, only=frozenset({row.id})),
                                      worker.ExitCode.UNCERTAIN)
                    self.assertIn(pane, self.case.fixture.world.locations)
                    self.assertIs(worker.restore(self.case.fixture.parent.id, only=frozenset({row.id})), worker.ExitCode.OK)
                else:
                    self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.WAITING_ON_RUNS)
                if case == "stage-one":
                    self.assertIn(pane, self.case.fixture.world.locations)
                    atomic_file.write_text_atomic(ready, "ready\n")
                else:
                    self.assertNotIn(pane, self.case.fixture.world.locations)
                    for scope in ((), ("--all-parents",)):
                        code, rows, error = self.case.text_command("list", *scope)
                        self.assertEqual(code, 0, error)
                        listed = next(cells for cells in rows if cells[1] == name)
                        self.assertEqual(listed[4:], ["closed", "parked", label])
                self.assertEqual(staff.by_id(row.id), row)
                self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                launches = len(self.case.fixture.world.launches)
                if case != "stage-one":
                    self.assertIs(worker.restore(self.case.fixture.parent.id, only=frozenset({row.id})), worker.ExitCode.OK)
                    self.assertEqual(len(self.case.fixture.world.launches), launches)
            finally:
                assert supervisor.stdin is not None
                supervisor.stdin.close()
                _ = supervisor.wait(timeout=LauncherHangGuard("parked run supervisor exit").remaining())

        record.update(status="finished", exit_code=0, completion_pending=True,
                      finished_at=datetime.now().astimezone().isoformat(timespec="microseconds"))
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.assertEqual(detached_runs.main(["deliver", str(path)]), 0)
        run_command = self.case.fixture.world.run
        submissions: list[tuple[str, str]] = []

        def submit(*arguments: str, deadline: float = math.inf) -> str:
            result = run_command(*arguments, deadline=deadline)
            if arguments[:2] == ("agent", "prompt") and f"Detached run: {name}\n" in arguments[3]:
                target, text = arguments[2:4]
                submissions.append((target, text))
                self.case.agent.append(target, "user", text)
                self.case.fixture.world.locations[target]["agent_status"] = "working"
            return result

        if case == "role-change":
            _ = write_registry(registry, {"assignments": {"direct": "claude"}}, update=True)
        with patch.object(self.case.fixture.world, "run", side_effect=submit):
            if done:
                self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
            else:
                with redirect_stderr(io.StringIO()) as errors:
                    code = worker.restore(self.case.fixture.parent.id, only=frozenset({row.id}))
                self.assertIs(code, worker.ExitCode.OK, errors.getvalue())
            self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
        if done:
            self.assertEqual(submissions, [])
            self.assertEqual(len(self.case.fixture.world.launches), launches)
            reply_file = self.case.reply
            if case == "stage-one":
                target, line = self.case.agent.submitted[-1]
                self.assertEqual(target, pane)
                self.assertIn(f"DONE {worker_panes.stage_two_completion_id(active.instance_id)}", line)
                self.case.agent.complete(pane)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
                self.assertEqual(self.case.reply.read_text(), self.case.agent.replies[-1] + "\n")
                reply_file = self.case.reply.with_name(f"{self.case.reply.stem}_stage1_{active.instance_id}.md")
            reply = reply_file.read_text()
            self.assertTrue(reply.startswith(final.splitlines()[0]))
            self.assertTrue(reply.rstrip().endswith(f"DONE {active.instance_id}"))
            self.assertIn("The final type check passed.", reply)
            self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "completed")
            self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)
        else:
            self.assertEqual(len(submissions), 1, worker_lifecycle.read_json_object(path).get("delivery"))
            (target, text), = submissions
            self.assertIn("The final type check passed.", text)
            self.assertEqual(len(self.case.fixture.world.launches), launches + 1)
            self.assertEqual(self.case.agent.row(target).current_session, row.current_session)
            self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
            if case == "role-change":
                family, arguments, _ = self.case.fixture.world.requests[-1]
                self.assertEqual((family, arguments[arguments.index("--model"):]),
                                 (original_family, original_args[original_args.index("--model"):]))
            self.case.agent.append(target, "assistant", f"DONE {active.instance_id}")
            self.case.fixture.world.locations[target]["agent_status"] = "done"
            self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)

    def test_first_hook_imports_the_legacy_status_stamp_without_sending_again(self) -> None:
        self.case.agent.without_done = True
        reply = "Use JSON."
        self.case.agent.final = reply
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        state = pass_recorder.read_state(run)
        passed = pass_recorder.pass_slots(state)["impl"]
        instance = str(passed["instance_id"])
        stamp = hashlib.sha256(f"no DONE\n{reply}".encode()).hexdigest()
        passed["hook_notice"] = stamp
        pass_recorder.write_state(run, state)
        self.assertIsNone(pass_notices.read(run, instance))

        event = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(row.current_session),
                                     "turn_id": "legacy-turn", "last_assistant_message": reply})
        with patch.object(send, "send") as transport:
            self.assertIs(turn_end.handle(event), turn_end.TurnOutcome.NO_DONE)
            notice_path, = run.glob(pass_notices.NOTICE_FILES)
            before = notice_path.read_bytes()
            self.assertIs(turn_end.handle(event), turn_end.TurnOutcome.NO_DONE)
            transport.assert_not_called()
        self.assertEqual(notice_path.read_bytes(), before)
        record = pass_notices.read(run, instance)
        assert record is not None
        imported, = record.notices.values()
        self.assertEqual(imported.legacy_stamp, stamp)
        self.assertTrue(imported.settled)

    def test_delayed_legacy_status_replay_sends_nothing_after_a_different_status(self) -> None:
        self.case.agent.without_done = True
        self.case.agent.final = "Use Markdown."
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        state = pass_recorder.read_state(run)
        passed = pass_recorder.pass_slots(state)["impl"]
        instance = str(passed["instance_id"])
        legacy_reply = "Use JSON."
        stamp = hashlib.sha256(f"no DONE\n{legacy_reply}".encode()).hexdigest()
        passed["hook_notice"] = stamp
        pass_recorder.write_state(run, state)

        self.assertIs(self.case.finish(), turn_end.TurnOutcome.NO_DONE)
        record = pass_notices.read(run, instance)
        assert record is not None
        self.assertEqual(record.notices["status:1"].legacy_stamp, stamp)
        self.assertTrue(record.notices["status:1"].settled)
        self.assertTrue(record.notices["status:2"].settled)
        notice_path, = run.glob(pass_notices.NOTICE_FILES)
        before = notice_path.read_bytes()

        self.case.agent.append(pane, "assistant", legacy_reply)
        event = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(row.current_session),
                                     "turn_id": "legacy-turn", "last_assistant_message": legacy_reply})
        self.assertIs(turn_end.handle(event), turn_end.TurnOutcome.NO_DONE)
        self.assertEqual([notice.splitlines()[-1] for notice in self.case.agent.notices], ["Use Markdown."])
        self.assertEqual(notice_path.read_bytes(), before)

    def test_status_events_send_a_returning_status_and_skip_a_replay(self) -> None:
        self.case.agent.without_done = True
        self.case.agent.final = "Use JSON."
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        for turn, reply in (("first", "Use JSON."), ("second", "Use Markdown."), ("third", "Use JSON.")):
            self.case.agent.append(pane, "user", "Check the format.")
            self.case.agent.append(pane, "assistant", reply)
            event = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(row.current_session),
                                        "turn_id": turn, "last_assistant_message": reply})
            self.assertIs(turn_end.handle(event), turn_end.TurnOutcome.NO_DONE)
            self.assertIs(turn_end.handle(event), turn_end.TurnOutcome.NO_DONE)
        replay = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(row.current_session),
                                     "turn_id": "first", "last_assistant_message": "Use JSON."})
        self.assertIs(turn_end.handle(replay), turn_end.TurnOutcome.NO_DONE)
        self.assertEqual([notice.splitlines()[-1] for notice in self.case.agent.notices],
                         ["Use JSON.", "Use Markdown.", "Use JSON."])
        record = pass_notices.read(run, instance)
        assert record is not None
        self.assertEqual(list(record.notices), ["status:1", "status:2", "status:3"])
        self.assertTrue(all(notice.settled for notice in record.notices.values()))

    def test_a_friend_reply_completes_the_same_pass_without_another_prompt(self) -> None:
        self.case.agent.without_done = True
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        active = progress_history.active_dispatch(row.id, self.case.run_dir)
        with patch.dict(os.environ, {"HERDR_PANE_ID": pane, "CODEX_THREAD_ID": str(row.current_session)}):
            friend = side_pane.open("friend-private", worker_panes.launch_policy("impl"), cwd=self.case.fixture.root)
        self.assertIsInstance(friend, side_pane.OpenSidePane)
        assert isinstance(friend, side_pane.OpenSidePane)
        pid = self.case.fixture.world.foreground[friend.pane][0]
        process = self.case.fixture.world.processes[pid]
        self.case.fixture.world.processes[pid] = replace(process, identity=herdr.PaneSessionReport(process.identity.id))
        self.case.fixture.world.locations[friend.pane]["agent_status"] = "working"
        self.assertIsNone(staff.snapshot().by_current_session(process.identity.id))
        self.case.fixture.world.locations[friend.pane]["label"] = "unrelated pane label"
        for address, expected in (("codex:another-worker", turn_end.TurnOutcome.NO_DONE),
                                  (f"codex:{row.current_session}", turn_end.TurnOutcome.WAITING_ON_FRIEND)):
            with self.subTest(recipient=address):
                self.case.agent.append(friend.pane, "user", f'Review the change. Reply with send.py --to {address} --text "<answer>".')
                self.assertEqual(self.case.finish(), expected)
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
        self.assertEqual(len(self.case.agent.submitted), 1)
        self.assertNotIn("question", self.case.agent.notices[-1])
        self.assertNotIn("no DONE", self.case.agent.notices[-1])
        self.case.fixture.world.locations[friend.pane]["agent_status"] = "done"
        self.case.agent.append(pane, "user", "Message from friend-private: The design is sound.")
        self.case.agent.without_done = False
        self.case.agent.complete(pane)
        self.assertEqual(self.case.finish(), turn_end.TurnOutcome.DONE)
        assert isinstance(active, progress_history.ActiveDispatch)
        self.assertTrue(self.case.reply.read_text().endswith(f"DONE {active.instance_id}\n"))

    def test_a_final_diff_event_before_the_review_reply_keeps_the_same_pass(self) -> None:
        self.case.agent.finish_after = 1
        for ready_first, hook_only, instruction in (
                (True, False, "new"), (False, False, "new"), (True, True, "new"), (True, False, "old")):
            with self.subTest(ready_first=ready_first, hook_only=hook_only, instruction=instruction):
                name = ("review-hook" if hook_only else "review-first" if ready_first else "review-last") + "-" + instruction
                ready = self.case.run_dir / f"{name}.ready"
                code = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "review", self.case.prompt,
                    session_dir=self.case.run_dir, slot=name, phase="8", kind="review", launcher_command=[name],
                    early_ready=ready, reply=self.case.reply)
                self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                assert isinstance(active, progress_history.ActiveDispatch)
                if instruction == "old":
                    passed = worker_panes.worker_pass(row, self.case.run_dir)
                    delivery = pass_recorder.dispatch_delivery(passed)
                    old_line = (f'Read the unchanged prompt file "{self.case.prompt}". '
                                f"End your final reply with DONE {active.instance_id}. "
                                "A reply that waits on a detached run is not final.")
                    progress_history.save_dispatch_delivery(self.case.run_dir, active.instance_id,
                                                           replace(delivery, line=old_line))
                before = len(self.case.agent.submitted)
                notices = len(self.case.agent.notices)
                if ready_first:
                    atomic_file.write_text_atomic(ready, "ready\n")
                    with patch.dict(os.environ, {"CODEX_THREAD_ID": str(self.case.fixture.parent.current_session)}):
                        self.assertEqual(turn_end.main(["stage-ready", name]), 0)
                    self.assertEqual(self.case.agent.notices[notices:], [])
                else:
                    self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.WAITING_ON_STAGE_TWO)
                    atomic_file.write_text_atomic(ready, "ready\n")
                self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                self.assertEqual(len(self.case.agent.submitted), before)
                if ready_first:
                    if hook_only:
                        reply = "The review is complete.\nDONE " + active.instance_id
                        self.case.agent.last_replies[str(row.current_session)] = reply
                        self.case.fixture.world.locations[pane]["agent_status"] = "done"
                        self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.IGNORED)
                        self.assertEqual(len(self.case.agent.submitted), before)
                        self.case.agent.append(pane, "assistant", reply)
                    else:
                        self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.SUBMITTED)
                else:
                    with patch.dict(os.environ, {"CODEX_THREAD_ID": str(self.case.fixture.parent.current_session)}):
                        self.assertEqual(turn_end.main(["stage-ready", name]), 0)
                self.assertEqual(len(self.case.agent.submitted), before + 1)
                stage_line = self.case.agent.submitted[-1][1]
                ending = ". " if instruction == "old" else "\n"
                self.assertIn(f"DONE {active.instance_id}-2{ending}", stage_line)
                with patch.dict(os.environ, {"CODEX_THREAD_ID": str(self.case.fixture.parent.current_session)}):
                    self.assertEqual(turn_end.main(["stage-ready", name]), 0)
                self.assertEqual(len(self.case.agent.submitted), before + 1)
                self.case.agent.complete(pane)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)

    def test_a_turn_ending_question_reaches_the_director_without_ending_the_pass(self) -> None:
        for name, reply in (("question", "Which output format must I use? JSON or Markdown?"),
                            ("recorder", "The two file-clock changes are complete. Results are pending."),
                            ("subagent", "The worker result is pending.")):
            with self.subTest(name=name):
                self.case.agent.without_done = True
                self.case.agent.final = reply
                notices = len(self.case.agent.notices)
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                event = turn_end.EventKind.TURN_END
                if name == "question":
                    _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": "claude"}})
                    with patch.dict(os.environ, {"HERDR_PANE_ID": pane, "CODEX_THREAD_ID": str(row.current_session)}):
                        unrelated = side_pane.open("unrelated", worker_panes.launch_policy("impl"), cwd=self.case.fixture.root)
                    self.assertIsInstance(unrelated, side_pane.OpenSidePane)
                    assert isinstance(unrelated, side_pane.OpenSidePane)
                    self.case.fixture.world.locations[unrelated.pane]["agent_status"] = "working"
                    _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": "codex"}})
                if name == "subagent":
                    assert isinstance(active, progress_history.ActiveDispatch)
                    backend = staff.backend(row)
                    assert isinstance(backend, (staff.CodexBackend, staff.ClaudeBackend))
                    source = worker_admission.transcript_source(row, backend)
                    with source.open("a") as stream:
                        _ = stream.write(json.dumps({"type": "response_item", "payload": {
                            "type": "agent_message", "author": "/root/reviewer", "recipient": "/root",
                            "content": [{"type": "input_text", "text": "The review is complete.\nDONE " + active.instance_id}],
                        }}) + "\n")
                    event = turn_end.EventKind.RUN_FINISHED
                self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.NO_DONE)
                self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.NO_DONE)
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                self.assertIn(self.case.agent.final, self.case.agent.notices[-1])
                self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                self.assertIn(pane, self.case.fixture.world.locations)
                self.case.agent.without_done = False
                self.case.agent.complete(pane)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)

        for family in ("claude", "codex"):
            for case in ("question", "empty-turn", "old-marker", "stale-transcript", "outage", "end-worker"):
                with self.subTest(family=family, case=case):
                    self.check_native_stop_notifies_while_pending(family, case)
            for case in ("reversed-handlers", "delayed-callback", "older-snapshot", "empty-then-question",
                         "question-then-question", "verified-done", "failed-send"):
                with self.subTest(family=family, case=case):
                    self.check_transcript_notice_order(family, case)
            with self.subTest(family=family, case="changed-path"):
                self.check_notice_stream_change(family, "changed-path")

        decision = turn_end._prepare_notice("worker", self.case.fixture.parent, self.case.run_dir,  # pyright: ignore[reportPrivateUsage]
            {"worker_id": str(row.id)}, "retarget-proof", turn_end.TurnOutcome.NO_DONE,
            self.case.reply, "question", "")
        current = staff.native_id("019b3a40-4250-7000-8000-000000009101")
        _ = staff.rename(self.case.fixture.parent.id, "renamed-director")
        _ = staff.update_current_session(self.case.fixture.parent.current_session, current)
        with patch.object(send, "send", return_value=send.Result("sent", "private")) as notice:
            _ = decision.send_notice()
        delivered = cast(send.Options, notice.call_args.args[0])
        self.assertEqual(delivered.to, "session:" + current)

    def check_native_stop_notifies_while_pending(self, family: str, case: str) -> None:
        _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
        self.write_result_retry_config()
        self.case.agent.finish_after = 1
        run = self.case.run_dir
        name = staff.WorkerName.parse(f"{family}-notice-{case}")
        reply = run / f"{name.value}-reply.md"
        status = run / f"{name.value}.status"
        self.assertIs(worker.dispatch(self.case.fixture.parent.id, name, "impl", self.case.prompt,
            session_dir=run, slot=name.value, phase="8", kind="impl", launcher_command=["assign.sh"],
            reply=reply, completion_actions=progress_history.CompletionActions(
                end_worker=case == "end-worker", status_file=str(status))), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        backend = staff.backend(row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        self.assertEqual("codex" if isinstance(backend, staff.CodexBackend) else "claude", family)
        transcript = worker_admission.transcript_source(row, backend)
        pending = worker.pending_done_path(run, row.id)
        passed = worker_panes.worker_pass(row, run)
        instance = str(passed["instance_id"])
        older = "older-stage"
        question = "Which output format must I use?"
        stop_text = {"empty-turn": "", "outage": "", "old-marker": f"The first stage passed.\nDONE {older}"}.get(
            case, question)
        if stop_text and case != "stale-transcript":
            self.case.agent.append(pane, "assistant", stop_text)
        elif case != "stale-transcript":
            self.append_empty_transcript_turn(transcript, family)
        prompt = transcript.read_bytes()
        if case == "stale-transcript":
            atomic_file.write_bytes_atomic(transcript, b"")
            self.case.agent.append(pane, "assistant", "The previous check passed.")
        self.case.fixture.world.locations[pane]["agent_status"] = "idle"
        notices = len(self.case.agent.notices)

        if case == "question":
            self.check_obsolete_interim_notice(row, passed, question)
        with ExitStack() as patches:
            _ = patches.enter_context(patch.object(turn_end, "time", SimpleNamespace(time=lambda: 10.0)))
            if case == "outage":
                _ = patches.enter_context(
                    patch.object(worker_admission, "transcript_source", side_effect=OSError("transcript unavailable")))
            self.native_stop(row, stop_text)
        self.assertTrue(pending.is_file())
        if case in ("stale-transcript", "outage"):
            self.assertEqual(len(self.case.agent.notices), notices)
            if case == "outage":
                atomic_file.write_bytes_atomic(transcript, b"")
            with patch.object(turn_end, "time", SimpleNamespace(time=lambda: 15.0)):
                self.assertEqual(detached_runs.reap_orphans(str(run)), [])
            self.assertEqual(len(self.case.agent.notices), notices)
            self.assertTrue(pending.is_file())
            atomic_file.write_bytes_atomic(transcript, prompt)
            if case == "stale-transcript":
                self.case.agent.append(pane, "assistant", stop_text)
        else:
            self.assertEqual(len(self.case.agent.notices), notices + 1)
        for now in (20.0, 25.0):
            with patch.object(turn_end, "time", SimpleNamespace(time=lambda now=now: now)):
                self.assertEqual(detached_runs.reap_orphans(str(run)), [])
            interim, = self.case.agent.notices[notices:]
            self.assertIn(": no DONE", interim)
            self.assertTrue(pending.is_file())
            current = worker_panes.worker_pass(row, run)
            self.assertEqual(current["status"], "active")
            self.assertIs(progress_history.hook_completion(current), progress_history.NoHookCompletion.UNRECORDED)
            self.assertIn(pane, self.case.fixture.world.locations)
            self.assertEqual(status.read_text(), "running\n")
        if case == "old-marker":
            self.assertIn(f"DONE {older} from another pass", interim)
        elif stop_text:
            self.assertIn(stop_text, interim)
        else:
            self.assertNotIn("; reply ", interim)

        final = f"The verification passed.\nDONE {instance}"
        self.case.agent.append(pane, "assistant", final)
        self.case.fixture.world.locations[pane]["agent_status"] = "done"
        for now in (30.0, 35.0):
            with patch.object(turn_end, "time", SimpleNamespace(time=lambda now=now: now)):
                self.assertEqual(detached_runs.reap_orphans(str(run)), [])
        self.assertFalse(pending.exists())
        self.assertTrue(progress_history.reply_completed(run, reply))
        self.assertEqual(reply.read_text(), final + "\n")
        self.assertEqual(status.read_text(), "done\n")
        self.assertNotIn(pane, self.case.fixture.world.locations)
        first, done = self.case.agent.notices[notices:]
        self.assertEqual(first, interim)
        self.assertIn(": done; reply ", done)

    def append_empty_transcript_turn(self, transcript: Path, family: str) -> None:
        record = ({"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": None}}
                  if family == "codex" else {"type": "system", "subtype": "turn_duration"})
        with transcript.open("a", encoding="utf-8") as stream:
            _ = stream.write(json.dumps(record) + "\n")

    def check_transcript_notice_order(self, family: str, case: str) -> None:
        _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
        self.write_result_retry_config()
        self.case.agent.finish_after = 1
        name = f"{family}-notice-{case}"
        run = self.case.run_dir
        self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        backend = staff.backend(row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        transcript = worker_admission.transcript_source(row, backend)
        passed = worker_panes.worker_pass(row, run)
        instance = str(passed["instance_id"])
        pending = worker.pending_done_path(run, row.id)
        self.case.fixture.world.locations[pane]["agent_status"] = "idle"
        start = len(self.case.agent.notices)
        a = "Which output format must I use?"
        b = "Which file must I write?"
        obsolete: turn_end.TurnDecision | None = None

        def reap(now: float) -> None:
            with patch.object(turn_end, "time", SimpleNamespace(time=lambda: now)):
                self.assertEqual(detached_runs.reap_orphans(str(run)), [])

        def check_notices(*replies: str) -> None:
            notices = self.case.agent.notices[start:]
            self.assertEqual(len(notices), len(replies))
            for notice, reply in zip(notices, replies, strict=True):
                self.assertIn(": no DONE", notice)
                self.assertTrue(notice.endswith("\n" + reply), notice)

        def prepare(reply: str) -> turn_end.TurnDecision:
            event = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(row.current_session),
                                         "last_assistant_message": reply})
            decision = turn_end._handle_locked(event)  # pyright: ignore[reportPrivateUsage]
            assert isinstance(decision, turn_end.TurnDecision)
            return decision

        with patch.object(turn_end, "time", SimpleNamespace(time=lambda: 10.0)):
            if case == "empty-then-question":
                self.append_empty_transcript_turn(transcript, family)
                with patch.object(worker_admission, "transcript_source", side_effect=OSError("unavailable")):
                    self.native_stop(row, "")
                self.case.agent.append(pane, "assistant", b)
                with patch.object(worker_admission, "transcript_source", side_effect=OSError("unavailable")):
                    self.native_stop(row, b)
            else:
                self.case.agent.append(pane, "assistant", a)
                earlier = transcript.read_bytes()
                if case == "delayed-callback":
                    first = prepare(a)
                    self.case.agent.append(pane, "assistant", b)
                    second = prepare(b)
                    _ = first.send_notice()
                    check_notices()
                    _ = second.send_notice()
                    _ = first.send_notice()
                elif case == "reversed-handlers":
                    self.case.agent.append(pane, "assistant", b)
                    with patch.object(worker_admission, "transcript_source", side_effect=OSError("unavailable")):
                        self.native_stop(row, b)
                    self.native_stop(row, a)
                    check_notices(b)
                elif case == "failed-send":
                    # retry_pending_done is the one retry path for a turn notice (F038).
                    failed = prepare(a)
                    with (patch.object(pass_notices, "_send", side_effect=ValueError("send failed")),
                          self.assertRaises(ValueError)):
                        _ = failed.send_notice()
                    check_notices()
                elif case == "verified-done":
                    self.native_stop(row, "An older Stop payload.")
                    check_notices(a)
                    self.case.agent.append(pane, "assistant", b)
                    obsolete = prepare(b)
                else:
                    self.native_stop(row, a)
                    check_notices(a)
                    self.case.agent.append(pane, "assistant", b)
                    if case == "older-snapshot":
                        self.native_stop(row, b)
                        newest = transcript.read_bytes()
                        atomic_file.write_bytes_atomic(transcript, earlier)
                        self.native_stop(row, a)
                        check_notices(a, b)
                        self.case.agent.append(pane, "assistant", "An unrelated reply in the old snapshot.")
                        self.case.agent.append(pane, "assistant", a)
                        self.native_stop(row, a)
                        check_notices(a, b)
                        atomic_file.write_bytes_atomic(transcript, newest)
                        self.case.agent.append(pane, "assistant", a)
                        self.native_stop(row, a)
                    else:
                        with patch.object(worker_admission, "transcript_source", side_effect=OSError("unavailable")):
                            self.native_stop(row, b)

        expected = {"older-snapshot": (a, b, a), "question-then-question": (a, b),
                    "verified-done": (a,), "failed-send": (a,)}.get(case, (b,))
        if case != "verified-done":
            for now in (20.0, 25.0):
                reap(now)
                check_notices(*expected)
        self.assertIn(pane, self.case.fixture.world.locations)
        self.assertTrue(pending.is_file())
        self.assertIs(progress_history.hook_completion(worker_panes.worker_pass(row, run)),
                      progress_history.NoHookCompletion.UNRECORDED)
        saved = worker_lifecycle.read_json_object(pending)
        self.assertNotIn("payload", saved)
        self.assertIn("delivered_notice", saved)

        final = f"The verification passed.\nDONE {instance}"
        self.case.agent.append(pane, "assistant", final)
        self.case.fixture.world.locations[pane]["agent_status"] = "done"
        reap(30.0)
        if obsolete is not None:
            _ = obsolete.send_notice()
        reap(35.0)
        self.assertFalse(pending.exists())
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertEqual(len(self.case.agent.notices[start:]), len(expected) + 1)
        self.assertIn(": done; reply ", self.case.agent.notices[-1])
        self.assertEqual(self.case.reply.read_text(), final + "\n")

    def check_obsolete_interim_notice(self, row: staff.StaffRow, passed: dict[str, object], question: str) -> None:
        event = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(row.current_session),
                                     "last_assistant_message": question})
        with patch.object(turn_end, "time", SimpleNamespace(time=lambda: 10.0)):
            decision = turn_end._handle_locked(event)  # pyright: ignore[reportPrivateUsage]
        self.assertIsInstance(decision, turn_end.TurnDecision)
        assert isinstance(decision, turn_end.TurnDecision)
        delivery = progress_history.dispatch_delivery(passed)
        notices = len(self.case.agent.notices)
        for state in ({**passed, "instance_id": "replacement-pass"},
                      {**passed, "hook_completion": {"outcome": "done", "reply_file": delivery.reply_file}}):
            with patch.object(worker_panes, "worker_pass", return_value=state):
                _ = decision.send_notice()
            self.assertEqual(len(self.case.agent.notices), notices)
        with patch.object(pass_recorder, "dispatch_delivery", return_value=replace(delivery, completion_id="stage-two")):
            _ = decision.send_notice()
        self.assertEqual(len(self.case.agent.notices), notices)

    def test_done_preserves_the_raw_receipt_when_saving_a_report(self) -> None:
        run = self.case.run_dir
        alias = run / "report-alias"
        alias.symlink_to(run, target_is_directory=True)
        parent = self.case.fixture.parent
        for aliased in (True, False):
            with self.subTest(aliased=aliased):
                name = "impl-aliased" if aliased else "impl-distinct"
                reply = run / f"{name}-reply.txt"
                summary = run / f"{name}-summary.txt"
                report = alias / reply.name if aliased else run / f"{name}-report.txt"
                actions = pass_recorder.CompletionActions(summary_file=str(summary), report_file=str(report))
                self.assertIs(worker.dispatch(parent.id, staff.WorkerName.parse(name), "impl", self.case.prompt,
                    session_dir=run, slot=name, phase="8", kind="impl", launcher_command=["assign.sh"],
                    reply=reply, completion_actions=actions), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                receipt = self.case.receipt(name)
                assert isinstance(receipt, worker.DispatchComplete)
                raw = (receipt.reply.rstrip() + "\n").encode()
                decorated = f"{parent.name} · impl\n\n".encode() + raw

                self.assertIs(self.case.finish(name), turn_end.TurnOutcome.DONE)
                self.assertEqual(reply.read_bytes(), raw)
                self.assertEqual(summary.read_bytes(), decorated)
                self.assertEqual(report.read_bytes(), raw if aliased else decorated)
                self.assertNotIn(pane, self.case.fixture.world.locations)

    def test_done_preserves_launcher_error_and_finishes_saved_actions(self) -> None:
        status = self.case.run_dir / "launcher.status"
        report = self.case.run_dir / "launcher-report.md"
        name = staff.WorkerName.parse("impl")
        self.assertIs(worker.dispatch(self.case.fixture.parent.id, name, "impl", self.case.prompt,
            session_dir=self.case.run_dir, slot=name.value, phase="8", kind="impl", launcher_command=["assign.sh"],
            reply=self.case.reply, completion_actions=pass_recorder.CompletionActions(
                status_file=str(status), report_file=str(report), end_worker=True)), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        passed = worker_panes.worker_pass(row, self.case.run_dir)
        instance = str(passed["instance_id"])
        error_reply = self.case.run_dir / "launcher-error.txt"
        worker.save_reply(error_reply, "Dispatch ended at the compaction bound.")
        progress_history.complete_dispatch(self.case.run_dir, name.value, instance, status="error")
        ended = worker_panes.worker_pass(row, self.case.run_dir)
        history = self.case.run_dir / "history.jsonl"
        recorded = history.read_bytes()
        progress_history.save_hook_completion(self.case.run_dir, instance,
            progress_history.HookCompletion("error", str(error_reply), ("Dispatch ended at the compaction bound.",)))
        self.assertEqual(self.case.finish(name.value), turn_end.TurnOutcome.DONE)
        self.assertTrue(progress_history.reply_completed(self.case.run_dir, self.case.reply))
        state = self.case.run_dir / "progress_history_state.json"
        saved = cast(dict[str, dict[str, dict[str, object]]], json.loads(state.read_text()))["pass"][name.value]
        self.assertEqual((saved["status"], saved["finished_at"]), ("error", ended["finished_at"]))
        self.assertEqual(history.read_bytes(), recorded)
        self.assertEqual(status.read_text(), "done\n")
        self.assertEqual(report.read_text(), self.case.reply.read_text())
        self.assertIn("The earlier pass ended with state error.", self.case.agent.notices[-1])
        self.assertIsNone(staff.by_id(row.id))

    def test_done_supersedes_a_prepared_launcher_error_notice(self) -> None:
        self.case.agent.finish_after = 1
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        error_reply = run / "launcher-error.txt"
        worker.save_reply(error_reply, "The launcher reported an error.")
        progress_history.complete_dispatch(run, row.name, instance, status="error")
        progress_history.save_hook_completion(run, instance, progress_history.HookCompletion("error", str(error_reply)))
        prepare = turn_end._handle_locked  # pyright: ignore[reportPrivateUsage]
        earlier = prepare(turn_end.WorkerEvent(str(row.current_session), turn_end.EventKind.RUN_FINISHED))
        assert isinstance(earlier, turn_end.TurnDecision)
        self.case.agent.complete(pane)
        done = prepare(turn_end.WorkerEvent(str(row.current_session), reply=self.case.agent.replies[-1]))
        assert isinstance(done, turn_end.TurnDecision)
        _ = earlier.send_notice()
        self.assertEqual(self.case.agent.notices, [])
        self.assertIn(pane, self.case.fixture.world.locations)
        self.assertEqual(done.send_notice(), turn_end.TurnOutcome.DONE)
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertIn("The earlier pass ended with state error.", self.case.agent.notices[0])
        self.assertNotIn(pane, self.case.fixture.world.locations)

    def test_late_done_delivers_a_notice_after_the_hook_records_error(self) -> None:
        run = self.case.run_dir
        name = staff.WorkerName.parse("impl")
        self.case.agent.without_done = True
        self.assertIs(worker.dispatch(self.case.fixture.parent.id, name, "impl", self.case.prompt,
            session_dir=run, slot=name.value, phase="8", kind="impl", launcher_command=["assign.sh"],
            reply=self.case.reply, completion_actions=pass_recorder.CompletionActions(end_worker=True)),
            worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        self.case.agent.notice_failures = 1
        with self.assertRaisesRegex(ValueError, "director message failed"):
            _ = self.case.finish()
        failed = worker_panes.worker_pass(row, run)
        instance = str(failed["instance_id"])
        self.assertEqual(failed["status"], "error")
        self.assertFalse(pass_notices.is_settled(run, instance, "no DONE"))
        self.assertIn(pane, self.case.fixture.world.locations)

        self.case.agent.without_done = False
        self.case.agent.complete(pane)
        self.assertIs(self.case.finish(), turn_end.TurnOutcome.DONE)
        notice, = self.case.agent.notices
        self.assertIn(f"done; reply {self.case.reply}; pass {instance}", notice)
        self.assertIn("The earlier pass ended with state error.", notice)
        stored = cast(dict[str, dict[str, dict[str, object]]],
                      json.loads((run / "progress_history_state.json").read_text()))["pass"][name.value]
        self.assertTrue(pass_notices.is_settled(run, instance, "done"))
        self.assertEqual((stored["instance_id"], stored["status"], stored["finished_at"]),
                         (instance, "error", failed["finished_at"]))
        self.assertEqual(len(self.case.agent.submitted), 1)

    def test_restore_and_assign_recover_late_done_before_close_or_reuse(self) -> None:
        for action in ("restore", "assign"):
            with self.subTest(action=action):
                name = "late-" + action
                self.case.agent.finish_after = 1
                opened = self.case.opened(name)
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                run = self.case.run_dir
                passed = worker_panes.worker_pass(opened.row, run)
                instance = str(passed["instance_id"])
                self.assertEqual(progress_history.cancel_worker_passes(run, opened.row.id), 1)
                self.case.agent.complete(opened.pane)

                with patch.object(progress_history, "save_hook_completion", side_effect=OSError("recording stopped")):
                    with self.assertRaisesRegex(OSError, "recording stopped"):
                        _ = self.case.finish(name)
                saved = self.case.reply.with_name(f"{self.case.reply.stem}_late_{instance}{self.case.reply.suffix}")
                self.assertIn(f"DONE {instance}", saved.read_text())
                self.assertIn(opened.pane, self.case.fixture.world.locations)

                next_prompt = run / f"{name}-next.md"
                worker.save_reply(next_prompt, "Next assignment.")
                self.case.prompt = next_prompt
                parent = self.case.fixture.parent.id
                submitted = len(self.case.agent.submitted)
                notices = len(self.case.agent.notices)
                self.case.agent.notice_failures = 1
                if action == "assign":
                    with self.assertRaisesRegex(ValueError, "director message failed"):
                        _ = self.case.finish(name)
                    with patch.object(send, "send") as send_during_assignment:
                        self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                    send_during_assignment.assert_not_called()
                    self.assertEqual(len(self.case.agent.submitted), submitted + 1)
                    self.assertNotEqual(worker_panes.worker_pass(opened.row, run)["instance_id"], instance)
                    self.assertEqual(len(self.case.agent.notices), notices)
                    _ = detached_runs.reap_orphans(str(run))
                    self.assertIn(opened.pane, self.case.fixture.world.locations)
                else:
                    with redirect_stderr(io.StringIO()):
                        self.assertEqual(worker.restore(parent, only=frozenset({opened.row.id})),
                                         worker.ExitCode.UNCERTAIN)
                    pending = worker_panes.worker_pass(opened.row, run)
                    completion = progress_history.hook_completion(pending)
                    assert isinstance(completion, progress_history.HookCompletion)
                    self.assertEqual(completion.reply_file, str(saved))
                    self.assertEqual(pending["instance_id"], instance)
                    self.assertIn(opened.pane, self.case.fixture.world.locations)
                    self.assertEqual(len(self.case.agent.submitted), submitted)
                    self.assertEqual(len(self.case.agent.notices), notices)
                    original_send = send.send

                    def send_before_close(options: send.Options) -> send.Result:
                        self.assertIn(opened.pane, self.case.fixture.world.locations)
                        self.assertEqual(len(self.case.agent.submitted), submitted)
                        self.assertEqual(worker_panes.worker_pass(opened.row, run)["instance_id"], instance)
                        return original_send(options)

                    with patch.object(send, "send", side_effect=send_before_close) as delivered:
                        self.assertEqual(worker.restore(parent, only=frozenset({opened.row.id})), worker.ExitCode.OK)
                    delivered.assert_called()
                    self.assertNotIn(opened.pane, self.case.fixture.world.locations)
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                self.assertIn(f"stopped; reply {saved}\nPass {instance}.", self.case.agent.notices[-1])

    def test_restart_keeps_a_pass_with_an_undelivered_completion(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        self.case.agent.notice_failures = 1
        with self.assertRaisesRegex(ValueError, "director message failed"):
            _ = self.case.finish()
        self.assertIsInstance(progress_history.active_dispatch(row.id, run), progress_history.NoActiveDispatch)
        self.assertTrue(self.case.fixture.world.stop(row, self.case.fixture.world))
        launches = len(self.case.fixture.world.launches)
        self.assertTrue(shutdown_record.record_path().is_relative_to(self.case.fixture.root))
        shutdown_record.save([str(row.id)])
        with (patch.object(herdr, "restore_server", return_value=herdr.ServerAdopted()),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
            self.case.agent.notice_failures = 2
            self.assertEqual(restart.restart(), 0)
            self.assertEqual(shutdown_record.read(), [str(row.id)])
            self.assertEqual(self.case.agent.notices, [])
            self.assertIn(pane, self.case.fixture.world.locations)
            self.assertEqual(restart.restart(), 0)
        self.assertEqual(shutdown_record.read(), [])
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertIn("phase 8: done", self.case.agent.notices[0])
        self.assertEqual(self.case.fixture.world.closed, [(pane, "terminal-" + pane)])
        self.assertEqual(len(self.case.fixture.world.launches), launches)

    def test_selected_live_restart_recovers_before_removing_the_worker(self) -> None:
        for stop_all in (False, True):
            with self.subTest(unconfirmed_stop=stop_all):
                name = "unconfirmed-worker" if stop_all else "live-worker"
                self.case.agent.finish_after = 1
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                self.assertEqual(progress_history.cancel_worker_passes(self.case.run_dir, row.id), 1)
                self.case.agent.complete(pane)
                shutdown_record.save([str(row.id)])
                launches = len(self.case.fixture.world.launches)
                notices = len(self.case.agent.notices)
                live = herdr.LiveConversation("codex", ())
                with (patch.object(herdr, "restore_server", return_value=herdr.ServerAdopted()),
                      patch.object(herdr, "read_conversation", return_value=live),
                      patch.object(restart, "stop_row", return_value=False) as stop,
                      patch.object(restart, "_after_unconfirmed_stop", return_value=live) as observe,
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                    self.case.agent.notice_failures = 1
                    self.assertEqual(restart.restart(stop_all=stop_all, only=(str(row.id),)), 0)
                    self.assertEqual(stop.call_count, int(stop_all))
                    self.assertEqual(observe.call_count, int(stop_all))
                    self.assertEqual(shutdown_record.read(), [str(row.id)])
                    self.assertIn(pane, self.case.fixture.world.locations)
                    self.assertEqual(len(self.case.agent.notices), notices)
                    self.assertEqual(restart.restart(only=(str(row.id),)), 0)
                self.assertEqual(shutdown_record.read(), [])
                self.assertNotIn(pane, self.case.fixture.world.locations)
                self.assertEqual(len(self.case.fixture.world.launches), launches)
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                self.assertIn("stopped; reply", self.case.agent.notices[-1])

    def test_restart_recovers_completion_at_each_durable_boundary(self) -> None:
        for boundary in ("before the first record", "after the reply file", "after pane close"):
            with self.subTest(boundary=boundary):
                name = "interrupted-" + boundary.replace(" ", "-")
                self.case.agent.finish_after = 1
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                run = self.case.run_dir
                instance = str(worker_panes.worker_pass(row, run)["instance_id"])
                self.assertEqual(progress_history.cancel_worker_passes(run, row.id), 1)
                self.case.agent.complete(pane)
                path = self.case.reply.with_name(f"{self.case.reply.stem}_late_{instance}{self.case.reply.suffix}")
                shutdown_record.save([str(row.id)])
                save = progress_history.save_hook_completion

                def stop_at_boundary(directory: Path, pass_id: str,
                                     completion: progress_history.HookCompletion) -> None:
                    if boundary == "after the reply file" or completion.pane_closed:
                        raise KeyboardInterrupt
                    save(directory, pass_id, completion)

                notices = len(self.case.agent.notices)
                launches = len(self.case.fixture.world.launches)
                closed = len(self.case.fixture.world.closed)
                if boundary != "before the first record":
                    with patch.object(progress_history, "save_hook_completion", side_effect=stop_at_boundary):
                        with self.assertRaises(KeyboardInterrupt):
                            _ = self.case.finish(name)
                stopped_after_close = boundary == "after pane close"
                saved = progress_history.hook_completion(worker_panes.worker_pass(row, run))
                assert isinstance(saved, progress_history.HookCompletion)
                self.assertEqual(saved.outcome, turn_end.TurnOutcome.STOPPED)
                self.assertFalse(saved.finished)
                self.assertEqual(path.is_file(), boundary != "before the first record")
                self.assertEqual(pane not in self.case.fixture.world.locations, stopped_after_close)
                self.assertEqual(shutdown_record.read(), [str(row.id)])
                if not stopped_after_close:
                    self.assertTrue(self.case.fixture.world.stop(row, self.case.fixture.world))
                with (patch.object(herdr, "restore_server", return_value=herdr.ServerAdopted()),
                      patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                    self.assertEqual(restart.restart(), 0)
                self.assertEqual(shutdown_record.read(), [])
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                self.assertIn(f"stopped; reply {path}\nPass {instance}.", self.case.agent.notices[-1])
                finished = progress_history.hook_completion(worker_panes.worker_pass(row, run))
                assert isinstance(finished, progress_history.HookCompletion)
                self.assertTrue(finished.finished)
                self.assertEqual(self.case.fixture.world.closed[closed:], [(pane, "terminal-" + pane)])
                self.assertEqual(len(self.case.fixture.world.launches), launches)

    def test_restart_uses_the_same_delivery_key_after_a_stop_before_settlement(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        shutdown_record.save([str(row.id)])
        keys: list[str] = []

        def deliver(options: send.Options, *, membership: staff.StaffSnapshot) -> send.Result:
            del membership
            recipient = options.recipient_identity
            assert isinstance(recipient, send.LogicalRecipientIdentity) and options.key is not None
            assert options.repeat_minutes is not None
            keys.append(options.key)
            message = send.Message(recipient.name, row.name, "", send.body_text(options), options.key)
            now = send.now()
            if send.confirmed_skip(message, timedelta(minutes=options.repeat_minutes), now) is not None:
                return send.Result("skipped", "the director received this notice")
            self.case.agent.notices.append(message.text)
            send.record_acceptance(message, now)
            return send.Result("sent", "private director")

        with (patch.object(send, "STATE", self.case.fixture.root / "message-state"),
              patch.object(send, "_send", side_effect=deliver)):
            with patch.object(pass_notices, "_settle", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    _ = self.case.finish()
            self.assertFalse(pass_notices.is_settled(run, instance, "done"))
            self.assertEqual(shutdown_record.read(), [str(row.id)])
            self.assertTrue(self.case.fixture.world.stop(row, self.case.fixture.world))
            with (patch.object(herdr, "restore_server", return_value=herdr.ServerAdopted()),
                  patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(restart.restart(), 0)
        self.assertEqual(keys, [f"worker-completion:{instance}:done"])
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertEqual(shutdown_record.read(), [])
        self.assertEqual(self.case.fixture.world.closed, [(pane, "terminal-" + pane)])

    def test_restart_keeps_a_pass_when_its_transcript_is_unavailable(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        self.assertEqual(progress_history.cancel_worker_passes(self.case.run_dir, row.id), 1)
        self.assertTrue(self.case.fixture.world.stop(row, self.case.fixture.world))
        shutdown_record.save([str(row.id)])
        with (patch.object(herdr, "restore_server", return_value=herdr.ServerAdopted()),
              patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
              patch.object(worker_admission, "transcript_source", side_effect=OSError("transcript is unreadable")),
              redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(restart.restart(), 0)
        self.assertIn("transcript is unreadable", errors.getvalue())
        self.assertEqual(shutdown_record.read(), [str(row.id)])
        self.assertIn(pane, self.case.fixture.world.locations)
        self.assertEqual(self.case.agent.notices, [])

    def test_restore_and_assign_find_late_done_in_the_transcript_before_the_hook(self) -> None:
        for action in ("restore", "assign"):
            with self.subTest(action=action):
                name = "before-hook-" + action
                self.case.agent.finish_after = 1
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                run = self.case.run_dir
                instance = str(worker_panes.worker_pass(row, run)["instance_id"])
                self.assertEqual(progress_history.cancel_worker_passes(run, row.id), 1)
                self.case.agent.complete(pane)
                self.case.agent.append(pane, "assistant", "A later message has no completion marker.")
                late = self.case.reply.with_name(f"{self.case.reply.stem}_late_{instance}{self.case.reply.suffix}")
                self.assertFalse(late.exists())
                stopped = progress_history.hook_completion(worker_panes.worker_pass(row, run))
                assert isinstance(stopped, progress_history.HookCompletion)
                self.assertEqual(stopped.stopped_reply, progress_history.StoppedReplyDisposition.PENDING)
                self.case.prompt = run / f"{name}-next.md"
                worker.save_reply(self.case.prompt, "Read the next assignment.")
                notices = len(self.case.agent.notices)
                submitted = len(self.case.agent.submitted)
                parent = self.case.fixture.parent.id

                def recover() -> worker.DispatchSubmission | worker.ExitCode:
                    if action == "assign":
                        return self.case.dispatch(name)
                    return worker.restore(parent, only=frozenset({row.id}))

                _, live = self.case.detached_record(row, "late-" + action)
                live["status"] = "running"
                with patch.object(detached_runs, "live_runs", return_value=[live]):
                    self.assertIs(recover(), worker.ExitCode.BUSY if action == "assign" else worker.ExitCode.UNCERTAIN)
                self.assertEqual(len(self.case.agent.notices), notices)
                self.assertEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)
                self.assertIn(pane, self.case.fixture.world.locations)
                self.assertFalse(late.exists())

                if action == "assign":
                    with patch.object(send, "send") as send_during_assignment:
                        self.assertIs(recover(), worker.DispatchSubmission.SUBMITTED)
                    send_during_assignment.assert_not_called()
                    self.assertEqual(len(self.case.agent.submitted), submitted + 1)
                    _ = detached_runs.reap_orphans(str(run))
                else:
                    self.assertIs(recover(), worker.ExitCode.OK)
                    self.assertEqual(len(self.case.agent.submitted), submitted)
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                self.assertIn(f"stopped; reply {late}\nPass {instance}.", self.case.agent.notices[-1])
                self.assertTrue(late.read_text().endswith(f"DONE {instance}\n"))
                if action == "assign":
                    self.assertNotEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)
                else:
                    self.assertNotIn(pane, self.case.fixture.world.locations)

    def test_assignment_reuses_an_idle_pane_after_a_linked_stop(self) -> None:
        self.case.agent.finish_after = 1
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        parent = self.case.fixture.parent.id
        passed = worker_panes.worker_pass(row, run)
        instance = str(passed["instance_id"])
        notice = progress_history.stop_dispatch(run, str(passed["team_slot"]), instance, row, parent,
                                               run_id="stopped-run", reason="Stop the worker pass.")
        self.assertIsInstance(notice, progress_history.StoppedPassNotice)
        path, result = self.case.detached_record(row, "linked-result")
        result.update(pane_dispatch_id=instance, delivery=f"sent: worker {row.name} at {pane}")
        atomic_file.write_text_atomic(path, json.dumps(result))
        self.case.agent.append(pane, "user", "Detached run: linked-result\nRun finished: verification")
        self.case.prompt = run / "next-assignment.md"
        worker.save_reply(self.case.prompt, "Read the next assignment.")
        self.case.fixture.world.locations[pane]["agent_status"] = "working"
        self.assertIs(self.case.dispatch(), worker.ExitCode.BUSY)
        self.assertEqual(len(self.case.agent.submitted), 1)
        self.case.fixture.world.locations[pane]["agent_status"] = "idle"
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        self.assertEqual(len(self.case.agent.submitted), 2)
        self.assertEqual(self.case.agent.submitted[-1][0], pane)
        self.assertEqual(self.case.agent.row(pane).id, row.id)
        self.assertNotEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)
        self.assertEqual(self.case.fixture.world.closed, [])

    def test_detached_result_keeps_an_ended_pass_until_a_later_matching_done(self) -> None:
        self.case.agent.finish_after = 1
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        self.assertEqual(progress_history.cancel_worker_passes(run, row.id), 1)
        self.case.agent.complete(pane)
        earlier_done = self.case.agent.replies[-1]
        self.case.agent.append(pane, "assistant", "A later unrelated reply.")
        self.assertEqual(self.case.receipt(), worker.DispatchComplete(earlier_done))
        path, record = self.case.detached_record(row, "after-done")
        record["delivery"] = f"sent: worker {row.name} at {pane}"
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.case.agent.append(pane, "user", "Detached run: after-done\nRun finished: private verification")
        self.case.fixture.world.locations[pane]["agent_status"] = "working"
        parent = self.case.fixture.parent.id
        name = staff.WorkerName.parse(row.name)
        submissions = self.case.agent.submitted.copy()
        notices = list(self.case.agent.notices)
        self.case.prompt = run / "next-assignment.md"
        worker.save_reply(self.case.prompt, "Read the next assignment.")
        shutdown_record.save([str(row.id)])
        for reply in ("", "The run passed."):
            with self.subTest(reply=reply), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                if reply:
                    self.case.agent.append(pane, "assistant", reply)
                receipt = self.case.receipt()
                assert isinstance(receipt, worker.DispatchWaitingForRuns)
                self.assertFalse(receipt.has_live_run)
                assert isinstance(receipt.reply, worker.DispatchAwaitingReply)
                self.assertEqual(receipt.reply.matching_done, worker.DispatchComplete(earlier_done))
                event = turn_end.WorkerEvent(str(row.current_session), reply=reply) if reply else turn_end.WorkerEvent(
                    str(row.current_session), turn_end.EventKind.RUN_FINISHED)
                self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.WAITING_ON_REPLY)
                self.assertIs(worker.restore(parent, only=frozenset({row.id})), worker.ExitCode.UNCERTAIN)
                self.assertIs(self.case.dispatch(), worker.ExitCode.BUSY)
                self.assertEqual(worker_panes.close(parent, name),
                                 worker_panes.Kept("agent state is working"))
                with (patch.object(herdr, "restore_server", return_value=herdr.ServerAdopted()),
                      patch.object(herdr, "read_conversation", return_value=herdr.LiveConversation("codex", ()))):
                    self.assertEqual(restart.restart(only=(str(row.id),)), 0)
                self.assertEqual(shutdown_record.read(), [str(row.id)])
                self.assertEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)
                self.assertIn(pane, self.case.fixture.world.locations)
                self.assertEqual(self.case.agent.submitted, submissions)
                self.assertEqual(self.case.agent.notices, notices)
        final = f"The run passed.\nDONE {instance}"
        self.case.agent.append(pane, "assistant", final)
        self.case.fixture.world.locations[pane]["agent_status"] = "done"
        event = turn_end.WorkerEvent(str(row.current_session), reply=final)
        self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.STOPPED)
        self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.STOPPED)
        self.assertEqual(len(self.case.agent.notices), len(notices) + 1)
        late = self.case.reply.with_name(f"{self.case.reply.stem}_late_{instance}{self.case.reply.suffix}")
        self.assertEqual(late.read_text(), final + "\n")
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertEqual(shutdown_record.read(), [])

    def test_later_done_replaces_the_saved_reply_after_the_notice_was_sent(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        with patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False):
            self.assertEqual(self.case.finish(), turn_end.TurnOutcome.DONE)
        self.assertIn(pane, self.case.fixture.world.locations)
        previous = self.case.reply.read_text()
        instance = str(worker_panes.worker_pass(row, self.case.run_dir)["instance_id"])
        path, record = self.case.detached_record(row, "after-notice")
        record["delivery"] = f"sent: worker {row.name} at {pane}"
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.case.agent.append(pane, "user", "Detached run: after-notice\nRun finished: private verification")
        self.assertEqual(self.case.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_REPLY)
        self.assertEqual(self.case.reply.read_text(), previous)
        self.assertIn(pane, self.case.fixture.world.locations)
        self.case.agent.append(pane, "assistant", f"The additional verification passed.\nDONE {instance}")
        self.assertEqual(self.case.finish(), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.case.reply.read_text(), f"The additional verification passed.\nDONE {instance}\n")
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertEqual(sum(": done; reply " in message for message in self.case.agent.notices), 1)

        for family in ("claude", "codex"):
            for case in ("no further result", "later result", "torn without saved evidence"):
                with self.subTest(family=family, case=case):
                    self.check_native_done_after_notice(family, case)
            for order in ("older_transcript_after_native_outage", "later_transcript_after_saved_done"):
                with self.subTest(family=family, order=order):
                    self.check_done_recovery_after_transcript_outage(family, order)
            for order, stop_text in (("markerless-stop", ""), ("old-stop", "DONE older-stage")):
                with self.subTest(family=family, case="markerless_or_old_stop_recovers_without_runs", order=order):
                    self.check_done_recovery_after_transcript_outage(family, order, stop_text=stop_text)
            for source in ("native", "reap"):
                with self.subTest(family=family, case="pending_verification_allows_finished_result", source=source):
                    self.check_pending_verification_allows_finished_result(family, source)
            for order in ("repeated_done_after_native_outage", "same_reply_after_native_outage_and_new_result",
                          "same_reply_after_delayed_stop_failed_retry_and_live_run"):
                with self.subTest(family=family, order=order):
                    self.check_repeated_done_after_native_outage(family, order)

    def check_native_done_after_notice(self, family: str, case: str) -> None:
        _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
        self.case.agent.finish_after = 1
        name = f"{family}-{case.replace(' ', '-')}"
        notices = len(self.case.agent.notices)
        self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        backend = staff.backend(row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        self.assertEqual("codex" if isinstance(backend, staff.CodexBackend) else "claude", family)
        self.case.agent.complete(pane)
        with patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False):
            self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
        self.assertIn(pane, self.case.fixture.world.locations)
        run = self.case.run_dir
        passed = worker_panes.worker_pass(row, run)
        instance = str(passed["instance_id"])
        previous = self.case.reply.read_text()
        final = f"The final verification passed.\nDONE {instance}"
        if case == "later result":
            path, record = self.case.detached_record(row, f"after-notice-{family}")
            record["delivery"] = f"sent: worker {row.name} at {pane}"
            atomic_file.write_text_atomic(path, json.dumps(record))
            self.case.agent.append(pane, "user", f"Detached run: {record['run_id']}\nRun finished: verification")
            self.assertEqual(self.case.finish(name, turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.WAITING_ON_REPLY)
            self.assertEqual(self.case.reply.read_text(), previous)
        elif case == "torn without saved evidence":
            evidence, = (path for path in run.glob(".*.pass-done.json")
                         if worker_lifecycle.read_json_object(path).get("instance") == instance)
            evidence.unlink()
            transcript = worker_admission.transcript_source(row, backend)
            clean = transcript.read_bytes()
            atomic_file.write_bytes_atomic(transcript, clean + b'{"type":')
            self.native_stop(row, final)
            self.assertEqual(self.case.reply.read_text(), previous)
            self.assertIn(pane, self.case.fixture.world.locations)
            atomic_file.write_bytes_atomic(transcript, clean)
        expected = previous
        if case != "no further result":
            self.case.agent.append(pane, "assistant", final)
            expected = final + "\n"
        self.native_stop(row, final)
        self.assertNotEqual(previous, final + "\n")
        self.assertEqual(self.case.reply.read_text(), expected)
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertTrue(pass_notices.is_settled(run, instance, "done"))
        self.assertEqual(sum(": done; reply " in message for message in self.case.agent.notices[notices:]), 1)

    def native_stop(self, row: staff.StaffRow, reply: str) -> None:
        payload: dict[str, object] = {
            "hook_event_name": "Stop", "session_id": str(row.current_session),
            "transcript_path": "unused", "stop_hook_active": False, "last_assistant_message": reply,
        }
        if isinstance(staff.backend(row), staff.CodexBackend):
            payload.update(turn_id="fixture-turn", cwd=str(self.case.fixture.root), model="sol")
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(turn_end.main(["--handle-hook"]), 0)

    def check_done_recovery_after_transcript_outage(self, family: str, order: str, *, stop_text: str | None = None) -> None:
        _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
        self.write_result_retry_config()
        name = f"{family}-{order.replace('_', '-')}"
        self.case.agent.finish_after = 1
        self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        backend = staff.backend(row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        self.assertEqual("codex" if isinstance(backend, staff.CodexBackend) else "claude", family)
        transcript = worker_admission.transcript_source(row, backend)
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        saved = f"The work passed during the outage.\nDONE {instance}"
        later = f"The additional verification passed.\nDONE {instance}"
        notices = len(self.case.agent.notices)
        submissions = list(self.case.agent.submitted)
        previous = self.case.reply.read_bytes() if self.case.reply.exists() else b""
        pending = worker.pending_done_path(run, row.id)
        native_reply = saved if stop_text is None else stop_text

        with (patch.object(worker_admission, "transcript_source", side_effect=OSError("transcript unavailable")),
              patch.object(turn_end, "time", SimpleNamespace(time=lambda: 10.0))):
            self.native_stop(row, native_reply)
        self.assertEqual(self.case.reply.read_bytes() if self.case.reply.exists() else b"", previous)
        self.assertIn(pane, self.case.fixture.world.locations)
        self.assertEqual(worker_panes.worker_pass(row, run)["status"], "active")
        self.assertNotIn("payload", worker_lifecycle.read_json_object(pending))
        code, listing, _ = self.case.command("list", "--json")
        self.assertEqual(code, 0)
        self.assertIn("transcript verification", listing)

        if stop_text is not None:
            self.assertEqual(detached_runs.worker_results(run / detached_runs.DETACHED_RUNS_DIR,
                row, since=0, pane_dispatch_id=instance, include_legacy_pass=True), ({}, set()))
            self.assertEqual(self.case.receipt(name), worker.DispatchSubmitted())
        with patch.object(turn_end, "time", SimpleNamespace(time=lambda: 15.0)):
            self.assertEqual(detached_runs.reap_orphans(str(run)), [])
        self.assertTrue(pending.is_file())
        self.assertEqual(worker_panes.worker_pass(row, run)["status"], "active")

        earlier = transcript.read_bytes()
        self.append_untimed_done(pane, transcript, saved)
        self.case.fixture.world.locations[pane]["agent_status"] = "done"
        expected = saved
        with ExitStack() as patches:
            if stop_text is None:
                _ = patches.enter_context(patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False))
            _ = patches.enter_context(patch.object(turn_end, "time", SimpleNamespace(time=lambda: 20.0)))
            self.assertEqual(detached_runs.reap_orphans(str(run)), [])
        if stop_text is None:
            self.assertEqual(self.case.reply.read_text(), saved + "\n")
            self.assertIn(pane, self.case.fixture.world.locations)
            if order == "later_transcript_after_saved_done":
                self.append_untimed_done(pane, transcript, later)
                expected = later
            else:
                atomic_file.write_bytes_atomic(transcript, earlier)
            self.assertEqual(self.case.finish(name, turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.DONE)
        self.assertEqual(worker_panes.worker_pass(row, run)["status"], "completed")
        self.assertEqual(self.case.reply.read_text(), expected + "\n")
        self.assertFalse(pending.exists())
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertEqual(self.case.agent.submitted, submissions)
        self.assertEqual(sum(": done; reply " in message for message in self.case.agent.notices[notices:]), 1)

    def check_pending_verification_allows_finished_result(self, family: str, source: str) -> None:
        _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
        self.write_result_retry_config()
        self.case.agent.finish_after = 1
        name = f"{family}-verify-result-{source}"
        self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        final = f"The verification passed.\nDONE {instance}"
        pending = worker.pending_done_path(run, row.id)
        previous = self.case.reply.read_bytes() if self.case.reply.exists() else b""
        notices = len(self.case.agent.notices)

        with (patch.object(worker_admission, "transcript_source", side_effect=OSError("transcript unavailable")),
              patch.object(turn_end, "time", SimpleNamespace(time=lambda: 10.0))):
            self.native_stop(row, final)
        self.assertTrue(pending.is_file())

        run_id = f"{name}-check"
        path, record = self.case.detached_record(row, run_id)
        # The result can reach the worker without holding completion for this pass.
        record.update(pane_dispatch_id="", exit_code=0, completion_pending=False,
                      delivery="deferred: the worker was busy")
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.assertEqual(self.case.receipt(name), worker.DispatchSubmitted())
        self.case.fixture.world.locations[pane]["agent_status"] = "idle"
        sent = f"worker {row.name} at {pane}"

        def accept(member: staff.StaffRow, _membership: staff.StaffSnapshot, text: str, *,
                   receipt: send.WorkerResultReceipt, **_kwargs: object) -> send.PaneTextDelivered:
            self.assertEqual(member.id, row.id)
            self.assertEqual(receipt.wait_for_idle, source == "native")
            self.assertTrue(pending.is_file())
            self.assertIn(f"Detached run: {run_id}", text.splitlines())
            _ = receipt.before_input()
            self.case.agent.append(pane, "user", text)
            return send.PaneTextDelivered("sent", sent)

        with patch.object(send, "deliver_worker_text", side_effect=accept) as submit:
            with patch.object(turn_end, "time", SimpleNamespace(time=lambda: 15.0)):
                if source == "native":
                    self.native_stop(row, "")
                else:
                    self.assertEqual(detached_runs.reap_orphans(str(run)), [])
            submit.assert_called_once()
            self.assertEqual(worker_lifecycle.read_json_object(path)["delivery"], f"sent: {sent}")
            self.assertTrue(pending.is_file())
            self.assertEqual(worker_panes.worker_pass(row, run)["status"], "active")
            self.assertEqual(self.case.reply.read_bytes() if self.case.reply.exists() else b"", previous)
            self.assertEqual(len(self.case.agent.notices), notices)

            self.case.agent.append(pane, "assistant", final)
            self.case.fixture.world.locations[pane]["agent_status"] = "done"
            with patch.object(turn_end, "time", SimpleNamespace(time=lambda: 20.0)):
                self.assertEqual(detached_runs.reap_orphans(str(run)), [])
            self.assertEqual(detached_runs.reap_orphans(str(run)), [])
            submit.assert_called_once()
        self.assertEqual(worker_panes.worker_pass(row, run)["status"], "completed")
        self.assertFalse(pending.exists())
        self.assertEqual(self.case.reply.read_text(), final + "\n")
        self.assertEqual(sum(": done; reply " in message for message in self.case.agent.notices[notices:]), 1)

    def append_untimed_done(self, pane: str, transcript: Path, reply: str) -> None:
        self.case.agent.append(pane, "assistant", reply)
        earlier, last = transcript.read_text().rstrip("\n").rsplit("\n", 1)
        fields = cast(dict[str, object], json.loads(last))
        del fields["timestamp"]
        atomic_file.write_text_atomic(transcript, f"{earlier}\n{json.dumps(fields)}\n")

    def write_result_retry_config(self) -> None:
        source = Path(__file__).resolve().parents[2] / "config/detached_runs.conf"
        _ = shutil.copyfile(source, self.case.fixture.root / "conf/detached_runs.conf")

    def check_repeated_done_after_native_outage(self, family: str, order: str) -> None:
        _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
        self.write_result_retry_config()
        failed_retry = order == "same_reply_after_delayed_stop_failed_retry_and_live_run"
        name = f"{family}-{order.replace('_', '-')}"
        self.case.agent.finish_after = 1
        self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        backend = staff.backend(row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        self.assertEqual("codex" if isinstance(backend, staff.CodexBackend) else "claude", family)
        run = self.case.run_dir
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        transcript = worker_admission.transcript_source(row, backend)
        saved = f"The work passed.\nDONE {instance}"
        latest = (f"The final verification passed.\nDONE {instance}"
                  if order == "repeated_done_after_native_outage" else saved)
        notices = len(self.case.agent.notices)
        submissions = list(self.case.agent.submitted)
        run_id = f"{name}-verification"
        path, record = self.case.detached_record(row, run_id)
        started = record["started_at"]
        record.update(exit_code=0, pane_dispatch_id=instance, self_stop=True,
                      finished_at=datetime.fromtimestamp(started + 5, timezone.utc).isoformat())
        atomic_file.write_text_atomic(path, json.dumps(record))
        if failed_retry:
            def refuse(*_args: object, receipt: send.WorkerResultReceipt, **_kwargs: object) -> send.WorkerResultNotSubmitted:
                _ = receipt.before_input()
                return send.WorkerResultNotSubmitted("retry", "fixture launch failed")

            with patch.object(send, "deliver_worker_text", side_effect=refuse):
                self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(0)),
                                 detached_runs.FAILED_WORKER_SUBMISSION + "fixture launch failed")

        self.append_untimed_done(pane, transcript, saved)
        older = transcript.read_bytes()
        if order == "repeated_done_after_native_outage":
            self.append_untimed_done(pane, transcript, f"The first verification passed.\nDONE {instance}")
        self.deliver_completion_result(row, pane, record)
        self.case.fixture.world.locations[pane]["agent_status"] = "done"
        with (patch.object(worker_admission, "transcript_source", side_effect=OSError("transcript unavailable")),
              patch.object(assign, "complete_herdr", wraps=assign.complete_herdr) as actions):
            self.native_stop(row, saved)
            self.assertEqual(worker_panes.worker_pass(row, run)["status"], "active")
            actions.assert_not_called()
        self.assertTrue(worker.pending_done_path(run, row.id).is_file())
        self.assertIsInstance(self.case.receipt(name), worker.DispatchWaitingForRuns)

        ordinary_id = f"{name}-ordinary"
        ordinary_path, ordinary = self.case.detached_record(row, ordinary_id)
        ordinary.update(status="running", pane_dispatch_id=instance, started_at=started + 20)
        if failed_retry:
            atomic_file.write_text_atomic(ordinary_path, json.dumps(ordinary))
        with patch.object(test_worker, "datetime", SimpleNamespace(now=partial(datetime.fromtimestamp, started + 40))):
            self.case.agent.append(pane, "assistant", latest)
        self.case.fixture.world.locations[pane]["agent_status"] = "done"
        with (patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False),
              patch.object(assign, "complete_herdr", wraps=assign.complete_herdr) as actions):
            if failed_retry:
                with patch.object(detached_run_process, "supervises", return_value=True):
                    event = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(row.current_session),
                                                "last_assistant_message": saved})
                    self.assertIs(turn_end.handle(event), turn_end.TurnOutcome.WAITING_ON_RUNS)
                    actions.assert_not_called()
                ordinary.update(status="finished", exit_code=0,
                                finished_at=datetime.fromtimestamp(started + 50, timezone.utc).isoformat())
                atomic_file.write_text_atomic(ordinary_path, json.dumps(ordinary))
            with patch.object(detached_runs, "submit_finished_worker_result",
                              wraps=detached_runs.submit_finished_worker_result) as submit:
                self.assertEqual(self.case.finish(name, turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.DONE)
                submit.assert_not_called()
            self.assertIsInstance(self.case.receipt(name), worker.DispatchComplete)
            evidence, = (record_path for record_path in run.glob(".*.pass-done.json")
                         if worker_lifecycle.read_json_object(record_path).get("instance") == instance)
            proof = worker_lifecycle.read_json_object(evidence)
            self.assertEqual(proof["reply"], latest)
            self.assertEqual(proof["pending_runs"], [])
            self.assertEqual(proof["results_before_done"], [run_id])
            self.assertEqual(proof["transcript_done_at"], datetime.fromtimestamp(started + 40, timezone.utc).timestamp())
            self.assertIn("cursor", proof)
            if failed_retry:
                self.assertIn(f"Detached run: {ordinary_id}\n", self.case.reply.read_text())
            else:
                self.assertEqual(self.case.reply.read_text(), latest + "\n")
            completed = self.case.reply.read_bytes()
            atomic_file.write_bytes_atomic(transcript, older)
            self.assertEqual(self.case.finish(name, turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.DONE)
            self.assertEqual(worker_lifecycle.read_json_object(evidence), proof)
            self.assertEqual(self.case.reply.read_bytes(), completed)
            actions.assert_called_once()
        self.assertTrue(self.case.reply.read_text().endswith(f"DONE {instance}\n"))
        self.assertEqual(self.case.agent.submitted, submissions)
        self.assertEqual(sum(": done; reply " in message for message in self.case.agent.notices[notices:]), 1)

    def deliver_completion_result(self, row: staff.StaffRow, pane: str, record: detached_runs.RunRecord) -> None:
        run_id = record["run_id"]
        world = self.case.fixture.world
        run = world.run
        world.locations[pane]["agent_status"] = "idle"
        inputs: list[str] = []

        def send_result(*arguments: str, deadline: float = math.inf) -> str:
            result = run(*arguments, deadline=deadline)
            if arguments[:3] == ("agent", "prompt", pane):
                self.assertIn(f"Detached run: {run_id}", arguments[3].splitlines())
                inputs.append(arguments[3])
                self.case.agent.append(pane, "user", arguments[3])
                world.locations[pane]["agent_status"] = "working"
            return result

        with patch.object(world, "run", side_effect=send_result):
            self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(0)),
                             f"sent: worker {row.name} at {pane}")
        self.assertEqual(len(inputs), 1)

    def test_later_done_updates_an_action_error_without_repeating_its_notice(self) -> None:
        status = self.case.run_dir / "fix.status"
        original = self.case.agent.run_process

        def fail_landed(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            if len(command) > 2 and command[1].endswith("/findings.py") and command[2] == "landed":
                return subprocess.CompletedProcess(command, 2, "", "ledger write failed")
            return original(command, **options)

        self.case.agent.run_process = fail_landed
        self.assertIs(worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse("fix"), "fix",
            self.case.prompt, session_dir=self.case.run_dir, slot="fix", phase="8", kind="fix",
            launcher_command=["fix"], reply=self.case.reply, fix_cause="Repair the failed gate",
            completion_actions=pass_recorder.CompletionActions(status_file=str(status), resolve_fix=True)),
            worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        with patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False):
            self.assertEqual(self.case.finish("fix"), turn_end.TurnOutcome.ERROR)
        before = worker_panes.worker_pass(row, self.case.run_dir)
        completion = progress_history.hook_completion(before)
        assert isinstance(completion, progress_history.HookCompletion)
        saved = self.case.reply.read_text()
        self.case.agent.append(pane, "user", "Detached run: additional-check\nRun finished: additional check")
        final = f"The additional check passed.\nDONE {before['instance_id']}"
        self.case.agent.append(pane, "assistant", final)

        self.assertEqual(self.case.finish("fix"), turn_end.TurnOutcome.ERROR)

        self.assertNotEqual(saved, final + "\n")
        self.assertEqual(self.case.reply.read_text(), final + "\n")
        after = worker_panes.worker_pass(row, self.case.run_dir)
        recovered = progress_history.hook_completion(after)
        assert isinstance(recovered, progress_history.HookCompletion)
        self.assertEqual(after["status"], "error")
        self.assertTrue(pass_notices.is_settled(self.case.run_dir, str(after["instance_id"]), "done"))
        self.assertEqual((recovered.outcome, recovered.details), (completion.outcome, completion.details))
        self.assertEqual(status.read_text(), "error\n")
        self.assertEqual(sum(": error; reply " in message for message in self.case.agent.notices), 1)

    def test_run_completion_replays_for_the_current_session_after_a_worker_moves(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        path, record = self.case.detached_record(row, "session-change")
        record.update(delivery="sent: holder received the result", completion_pending=True)
        atomic_file.write_text_atomic(path, json.dumps(record))
        new_session = staff.native_id(fixture_runtime_id("run-result-after-session-change"))
        backend = staff.backend(row)
        assert isinstance(backend, (staff.CodexBackend, staff.ClaudeBackend))
        fixture_session(new_session, backend)
        old_transcript = worker_admission.transcript_source(row, backend).read_text()
        lock = worker_panes.worker_lock

        @contextmanager
        def move_during_lock(parent: staff.StaffId, name: staff.WorkerName, *, blocking: bool
                             ) -> Generator[worker_panes.WorkerLockHold | worker_panes.Kept]:
            with lock(parent, name, blocking=blocking) as hold:
                moved = staff.update_current_session(row.current_session, new_session)
                source = worker_admission.transcript_source(moved, backend)
                atomic_file.write_text_atomic(source, old_transcript)
                yield hold

        with patch.object(worker_panes, "worker_lock", side_effect=move_during_lock):
            self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
        pending = cast(detached_runs.RunRecord, json.loads(path.read_text()))
        self.assertTrue(pending.get("completion_pending"))
        self.assertFalse(self.case.reply.exists())

        family = "codex" if isinstance(backend, staff.CodexBackend) else "claude"
        self.case.fixture.world.activate(str(new_session), pane, family)
        self.case.agent.complete(pane)
        self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
        self.assertEqual(self.case.reply.read_text(), self.case.agent.replies[-1] + "\n")
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertEqual(sum(": done; reply " in message for message in self.case.agent.notices), 1)

        for family in ("claude", "codex"):
            for change in ("preserved-prefix", "divergent-prefix"):
                with self.subTest(family=family, change=change):
                    self.check_notice_stream_change(family, change)

    def check_notice_stream_change(self, family: str, change: str) -> None:
        _ = write_registry(self.case.fixture.root / "conf/models.conf", {"assignments": {"direct": family}})
        self.write_result_retry_config()
        self.case.agent.finish_after = 1
        run = self.case.run_dir
        name = staff.WorkerName.parse(f"{family}-notice-{change}")
        reply = run / f"{name.value}.md"
        self.assertIs(worker.dispatch(self.case.fixture.parent.id, name, "impl", self.case.prompt,
            session_dir=run, slot=name.value, phase="8", kind="impl", launcher_command=["assign.sh"],
            reply=reply), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        backend = staff.backend(row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        self.assertEqual("codex" if isinstance(backend, staff.CodexBackend) else "claude", family)
        instance = str(worker_panes.worker_pass(row, run)["instance_id"])
        pending = worker.pending_done_path(run, row.id)
        resolve = worker_admission.transcript_source
        source = resolve(row, backend)
        prompt = source.read_bytes()
        first = "Which format must I use?"
        second = "Which file must I write?"
        start = len(self.case.agent.notices)
        self.case.fixture.world.locations[pane]["agent_status"] = "idle"
        if change == "divergent-prefix":
            for text in ("The check started.", "The check passed."):
                self.case.agent.append(pane, "assistant", text)
        self.case.agent.append(pane, "assistant", first)
        prefix = source.read_bytes()
        with patch.object(turn_end, "time", SimpleNamespace(time=lambda: 10.0)):
            self.native_stop(row, first)
        self.assertEqual(len(self.case.agent.notices[start:]), 1)
        old_delivered = worker.TranscriptCursor.from_json(worker_lifecycle.read_json_object(pending)["delivered_notice"])
        assert isinstance(old_delivered, worker.TranscriptCursor)
        previous = row
        with ExitStack() as patches:
            if change == "changed-path":
                self.case.agent.append(pane, "assistant", "A question from the previous transcript path.")
                event = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(row.current_session)})
                obsolete = turn_end._handle_locked(event)  # pyright: ignore[reportPrivateUsage]
                assert isinstance(obsolete, turn_end.TurnDecision)
                new_source = source.with_name(f"{name.value}.jsonl")
                atomic_file.write_bytes_atomic(new_source, prefix)
                _ = patches.enter_context(patch.object(worker_admission, "transcript_source", return_value=new_source))
                _ = obsolete.send_notice()
                self.assertEqual(len(self.case.agent.notices[start:]), 1)
            else:
                new_session = staff.native_id(fixture_runtime_id(name.value))
                fixture_session(new_session, backend)
                row = staff.update_current_session(previous.current_session, new_session)
                new_source = resolve(row, backend)
                self.case.fixture.world.activate(str(new_session), pane,
                                                "codex" if isinstance(backend, staff.CodexBackend) else "claude")
                atomic_file.write_bytes_atomic(new_source, b"")
                self.native_stop(row, second)
                self.assertEqual(len(self.case.agent.notices[start:]), 1)
                atomic_file.write_bytes_atomic(new_source, prompt if change == "divergent-prefix" else prefix)
            if change != "divergent-prefix":
                self.native_stop(row, first)
                self.assertEqual(len(self.case.agent.notices[start:]), 1)
                moved = worker.TranscriptCursor.from_json(worker_lifecycle.read_json_object(pending)["delivered_notice"])
                self.assertEqual(moved, replace(old_delivered, stream=worker.transcript_stream(row, new_source)))

            self.case.agent.append(pane, "assistant", second)
            if change != "divergent-prefix":
                self.native_stop(row, second)
            for now in (20.0, 25.0):
                with patch.object(turn_end, "time", SimpleNamespace(time=lambda now=now: now)):
                    self.assertEqual(detached_runs.reap_orphans(str(run)), [])
            self.native_stop(row, second)
            first_notice, second_notice = self.case.agent.notices[start:]
            self.assertTrue(first_notice.endswith("\n" + first))
            self.assertTrue(second_notice.endswith("\n" + second))
            delivered = worker.TranscriptCursor.from_json(worker_lifecycle.read_json_object(pending)["delivered_notice"])
            assert isinstance(delivered, worker.TranscriptCursor)
            self.assertEqual(delivered.stream, worker.transcript_stream(row, new_source))
            if change == "divergent-prefix":
                self.assertLess(delivered.position, old_delivered.position)
            self.assertTrue(pending.is_file())
            self.assertIs(progress_history.hook_completion(worker_panes.worker_pass(row, run)),
                          progress_history.NoHookCompletion.UNRECORDED)

            final = f"The verification passed.\nDONE {instance}"
            self.case.agent.append(pane, "assistant", final)
            self.case.fixture.world.locations[pane]["agent_status"] = "done"
            for now in (30.0, 35.0):
                with patch.object(turn_end, "time", SimpleNamespace(time=lambda now=now: now)):
                    self.assertEqual(detached_runs.reap_orphans(str(run)), [])
            self.assertEqual(reply.read_text(), final + "\n")
            self.assertFalse(pending.exists())
            self.assertNotIn(pane, self.case.fixture.world.locations)
            self.assertEqual(len(self.case.agent.notices[start:]), 3)
            self.assertIn(": done; reply ", self.case.agent.notices[-1])

    def test_a_detached_result_reaches_the_idle_worker_in_both_completion_orders(self) -> None:
        source = Path(__file__).resolve().parents[2] / "config"
        config = self.case.fixture.root / "conf"
        for name in ("detached_runs.conf", "postmaster.conf", "codex_daemon.conf"):
            _ = shutil.copyfile(source / name, config / name)
        cases = ((True, False, False), (False, False, False), (True, True, False), (True, True, True))
        for finishes_during_turn, self_stop, transcript_unavailable in cases:
            with self.subTest(finishes_during_turn=finishes_during_turn, self_stop=self_stop,
                              transcript_unavailable=transcript_unavailable):
                name = f"impl-result-{int(finishes_during_turn)}-{int(self_stop)}-{int(transcript_unavailable)}"
                self.case.reply = self.case.run_dir / f"{name}.md"
                self.case.agent.finish_after = 1
                self.assertIs(worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "impl",
                    self.case.prompt, session_dir=self.case.run_dir, slot=name, phase="8", kind="impl",
                    launcher_command=["assign.sh"], reply=self.case.reply), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                path, record = self.case.detached_record(row, name)
                record["status"] = "running"
                if self_stop:
                    record.update(stop_reason="The newer check replaces this run.",
                                  stop_pass_requested=True, self_stop=True)
                log = "The private check began." if self_stop else "The private check passed."
                atomic_file.write_text_atomic(path, json.dumps(record))
                atomic_file.write_text_atomic(Path(record["log"]), log + "\n")
                submitted: list[str] = []
                notices = len(self.case.agent.notices)
                run = self.case.fixture.world.run

                def submit(*arguments: str, deadline: float = math.inf) -> str:
                    if arguments[:3] == ("agent", "prompt", pane):
                        self.assertTrue(herdr.AgentStatus(
                            self.case.fixture.world.locations[pane]["agent_status"]).idle)
                        submitted.append(arguments[3])
                        self.case.agent.append(pane, "user", arguments[3])
                        self.case.fixture.world.locations[pane]["agent_status"] = "working"
                        return "{}"
                    return run(*arguments, deadline=deadline)

                def deliver(result_path: Path) -> int:
                    return detached_runs.main(["deliver", str(result_path)])

                def finish_turn() -> turn_end.TurnOutcome:
                    self.case.agent.without_done = True
                    self.case.agent.complete(pane)
                    self.case.agent.without_done = False
                    if finishes_during_turn:
                        self.case.fixture.world.locations[pane]["agent_status"] = "working"
                    return self.case.finish(name)

                def wait_for_idle(target: str, _timeout: int) -> herdr.ObservedAgentState:
                    self.assertEqual(target, pane)
                    self.assertEqual(submitted, [])
                    self.case.fixture.world.locations[pane]["agent_status"] = "idle"
                    return herdr.ObservedAgentState(herdr.AgentStatus.IDLE)

                with (patch.object(self.case.fixture.world, "run", side_effect=submit),
                      patch.object(self.case.fixture.world, "wait_turn_end", side_effect=wait_for_idle),
                      patch.object(detached_runs, "_stopped_group_has_ended", return_value=True),
                      patch.object(detached_runs, "_run", return_value=143 if self_stop else 0),
                      patch.object(detached_runs, "deliver_in_fresh_process", side_effect=deliver)):
                    if not finishes_during_turn:
                        with patch.object(detached_run_process, "supervises", return_value=True):
                            self.assertEqual(finish_turn(), turn_end.TurnOutcome.WAITING_ON_RUNS)
                    if transcript_unavailable:
                        with patch.object(detached_runs, "_result_received", side_effect=OSError("fixture read failure")):
                            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
                        saved = worker_lifecycle.read_json_object(path)
                        self.assertEqual(saved.get("delivery"),
                                         "deferred: worker transcript is unavailable: fixture read failure")
                        self.assertNotIn("delivered_at", saved)
                        self.assertEqual(len(self.case.agent.notices), notices)
                        with patch.object(worker_panes, "observe", return_value=(worker_panes.ClosedWorker(row),)):
                            self.assertEqual(self.case.finish(name, turn_end.EventKind.RUN_FINISHED),
                                             turn_end.TurnOutcome.RETRY_EVIDENCE)
                        saved = worker_lifecycle.read_json_object(path)
                        self.assertEqual(saved.get("delivery"), "deferred: worker pane is closed")
                        self.assertNotIn("delivered_at", saved)
                        self.assertEqual(len(self.case.agent.notices), notices)
                    else:
                        self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
                    if finishes_during_turn:
                        saved = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                        self.assertTrue(saved.get("completion_pending"))
                        self.assertEqual(submitted, [])
                        self.assertEqual(finish_turn(), turn_end.TurnOutcome.SUBMITTED)
                    result, = submitted
                    self.assertIn(f"Detached run: {name}", result)
                    self.assertIn(log, result)
                    if self_stop:
                        self.assertIn("Result: STOPPED (exit 143)", result)
                        self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir),
                                              progress_history.ActiveDispatch)
                        self.assertIn(pane, self.case.fixture.world.locations)
                        self.assertEqual(len(self.case.agent.notices), notices)
                    saved = cast(detached_runs.RunRecord, json.loads(path.read_text()))
                    self.assertEqual(saved.get("delivery"), f"sent: worker {name} at {pane}")
                    self.assertTrue(saved.get("completion_pending"))
                    self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
                    self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
                    self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)
                self.assertEqual(self.case.reply.read_text(), self.case.agent.replies[-1] + "\n")
                self.assertNotIn(pane, self.case.fixture.world.locations)
                self.assertFalse(any("waiting on run reply" in text for text in self.case.agent.notices[notices:]))

    def test_a_delivery_after_done_waits_when_the_transcript_has_no_result_yet(self) -> None:
        for event in (turn_end.EventKind.TURN_END, turn_end.EventKind.RUN_FINISHED):
            for stamped in (False, True):
                with self.subTest(event=event, stamped=stamped):
                    name = f"impl-{event.name.lower().replace('_', '-')}-{int(stamped)}"
                    self.case.reply = self.case.run_dir / f"{name}-reply.md"
                    self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                    pane = self.case.agent.submitted[-1][0]
                    row = self.case.agent.row(pane)
                    path, record = self.case.detached_record(row, name)
                    record["delivery"] = f"sent: worker {row.name} at {pane}"
                    if stamped:
                        record["delivered_at"] = float(record["started_at"])
                        record["finished_at"] = datetime.now().astimezone().isoformat(timespec="microseconds")
                    atomic_file.write_text_atomic(path, json.dumps(record))
                    receipt = self.case.receipt(name)
                    self.assertIsInstance(receipt, worker.DispatchWaitingForRuns)
                    assert isinstance(receipt, worker.DispatchWaitingForRuns)
                    self.assertIsInstance(receipt.reply, worker.DispatchAwaitingReply)
                    self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.WAITING_ON_REPLY)
                    self.assertFalse(self.case.reply.exists())
                    self.assertIsInstance(worker_panes.close(self.case.fixture.parent.id, staff.WorkerName.parse(name)),
                                          worker_panes.Kept)
                    self.case.agent.append(pane, "user", f"Detached run: {name}\nRun finished: private verification")
                    self.case.agent.complete(pane)
                    self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.DONE)
                    self.assertEqual(self.case.reply.read_text(), self.case.agent.replies[-1] + "\n")
                    self.assertNotIn(pane, self.case.fixture.world.locations)

    def test_supervisor_sends_the_notice_after_the_command_changes_completion_sources(self) -> None:
        status = self.case.run_dir / "completion.status"
        self.assertIs(worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse("impl"), "impl",
            self.case.prompt, session_dir=self.case.run_dir, slot="impl", phase="8", kind="impl",
            launcher_command=["assign.sh"], reply=self.case.reply,
            completion_actions=pass_recorder.CompletionActions(status_file=str(status))),
            worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        path, record = self.case.detached_record(row, "changes-completion")
        private_sources = self.case.fixture.root / "scripts"
        module = private_sources / "direct/assign.py"
        module.parent.mkdir(parents=True)
        imported = self.case.fixture.root / "loaded-deferred-source"
        deferred = module.with_name("detached_completion_fixture.py")
        atomic_file.write_text_atomic(deferred, "from pathlib import Path\n"
            + f"_ = Path({str(imported)!r}).write_text('original source')\n")
        hook = '    """Run the saved completion actions after the hook proves DONE."""\n'
        source = Path(str(assign.__file__)).read_text()
        self.assertIn(hook, source)
        atomic_file.write_text_atomic(module, source.replace(hook,
            hook + "    from scripts.direct import detached_completion_fixture\n"
                   + "    assert detached_completion_fixture.__file__\n", 1))
        replacement = 'raise ImportError("the command replaced this dependency")\n'
        record["command"] = [sys.executable, "-c", "from pathlib import Path; "
            + f"Path({str(module)!r}).write_text({replacement!r}); "
            + f"Path({str(deferred)!r}).write_text({replacement!r})"]
        record["status"] = "running"
        atomic_file.write_text_atomic(path, json.dumps(record))
        completion_sources = detached_runs.CompletionSources

        def load_fixture_sources(_root: Path) -> detached_runs.CompletionSources:
            return completion_sources(private_sources)

        def deliver_fixture(record_path: Path) -> int:
            self.assertEqual(record_path, path)
            self.assertEqual(module.read_text(), replacement)
            self.assertEqual(deferred.read_text(), replacement)
            self.assertFalse(imported.exists())
            self.case.agent.append(pane, "user", "Detached run: changes-completion\nRun finished: private verification")
            self.case.agent.complete(pane)
            return 0

        package = sys.modules["scripts.direct"]
        with (patch.dict(sys.modules), patch.object(package, "assign"),
              patch.object(package, "detached_completion_fixture", create=True),
              patch.object(detached_runs, "CompletionSources", side_effect=load_fixture_sources),
              patch.object(detached_runs, "deliver_in_fresh_process", side_effect=deliver_fixture)):
            delattr(package, "assign")
            delattr(package, "detached_completion_fixture")
            _ = sys.modules.pop("scripts.direct.assign")
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        self.assertEqual(imported.read_text(), "original source")
        self.assertEqual(status.read_text(), "done\n")
        self.assertEqual(self.case.reply.read_text(), self.case.agent.replies[-1] + "\n")
        self.assertEqual(sum(": done; reply " in message for message in self.case.agent.notices), 1)
        self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)
        self.assertNotIn(pane, self.case.fixture.world.locations)

    def test_reap_retries_completion_after_the_transcript_becomes_readable(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        path, record = self.case.detached_record(row, "unreadable-transcript")
        record["delivery"] = f"sent: worker {row.name} at {pane}"
        self.case.agent.append(pane, "user", "Detached run: unreadable-transcript\nRun finished: private verification")
        self.case.agent.complete(pane)
        record.update(status="running", command=["true"])
        atomic_file.write_text_atomic(path, json.dumps(record))
        with (patch.object(worker_admission, "transcript_source", side_effect=OSError("transcript is unreadable")),
              patch.object(detached_runs, "deliver_in_fresh_process", return_value=0)):
            self.assertEqual(detached_runs.main(["supervise", str(path)]), 0)
        self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), True)
        self.assertFalse(self.case.reply.exists())
        self.assertEqual(self.case.agent.notices, [])
        self.assertIn(pane, self.case.fixture.world.locations)
        with self.subTest(evidence="old recorder state"):
            state = self.case.run_dir / unit_runs.STATE_FILE
            original = state.read_bytes()
            old = worker_lifecycle.read_json_object(state)
            old["phase_last_percent"] = {}
            atomic_file.write_text_atomic(state, json.dumps(old))
            with redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
            self.assertIn("old shape", errors.getvalue())
            self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), True)
            self.assertIn(pane, self.case.fixture.world.locations)
            atomic_file.write_bytes_atomic(state, original)
        self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
        self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)
        self.assertEqual(self.case.reply.read_text(), self.case.agent.replies[-1] + "\n")
        self.assertEqual(sum(": done; reply " in message for message in self.case.agent.notices), 1)
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
        self.assertEqual(len(self.case.agent.notices), 1)

        source = Path(__file__).resolve().parents[2] / "config"
        for config in ("detached_runs.conf", "postmaster.conf", "codex_daemon.conf"):
            _ = shutil.copyfile(source / config, self.case.fixture.root / "conf" / config)
        for order in ("result follows DONE", "reap after a late result", "result still deferred",
                      "result still deferred with a torn record", "legacy record",
                      "reopen after prune", "working after reopen"):
            with self.subTest(order=order):
                self.check_done_covers_finished_result(order)

    def check_done_covers_finished_result(self, order: str) -> None:
        name = order.replace(" ", "-").lower()
        self.case.reply = self.case.run_dir / f"{name}-reply.md"
        self.case.agent.finish_after = 1
        self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
        pane, _ = self.case.agent.submitted[-1]
        row = self.case.agent.row(pane)
        passed = worker_panes.worker_pass(row, self.case.run_dir)
        instance = str(passed["instance_id"])
        path, record = self.case.detached_record(row, name)
        record.update(pane_dispatch_id=instance, exit_code=0, completion_pending=True,
                      finished_at=datetime.now().astimezone().isoformat(timespec="microseconds"),
                      delivery="deferred: worker is working")
        self.case.fixture.world.locations[pane]["agent_status"] = "working"
        with detached_runs.record_lock(path):
            atomic_file.write_text_atomic(path, json.dumps(record))
        deferred = order.startswith("result still deferred") or order == "legacy record"
        if deferred:
            self.case.agent.complete(pane)
        else:
            send_input = self.case.fixture.world.run

            def accept_result(*arguments: str, deadline: float = math.inf) -> str:
                if arguments[:3] == ("agent", "prompt", pane):
                    self.case.agent.complete(pane)
                    self.case.agent.append(pane, "user", arguments[3])
                return send_input(*arguments, deadline=deadline)

            self.case.fixture.world.locations[pane]["agent_status"] = "idle"
            with patch.object(self.case.fixture.world, "run", new=accept_result):
                self.assertEqual(detached_runs.deliver(record, detached_runs.command_result(record)),
                                 f"sent: worker {name} at {pane}")
            self.case.agent.append(pane, "assistant", "I already recorded that check.")
        done = self.case.agent.replies[-1]
        self.case.fixture.world.locations[pane]["agent_status"] = "done"
        submissions = list(self.case.agent.submitted)
        notices = len(self.case.agent.notices)
        if order == "result still deferred with a torn record":
            backend = staff.backend(row)
            assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
            transcript = worker_admission.transcript_source(row, backend)
            accepted = transcript.read_text()
            atomic_file.write_text_atomic(transcript, accepted + '{"type":')
            with patch.object(send, "deliver_worker_text", return_value=send.Result("retry", "fixture")) as submit:
                self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(0)),
                                 "deferred: worker transcript is unavailable: worker transcript has incomplete records")
            submit.assert_not_called()
            self.assertTrue(str(worker_lifecycle.read_json_object(path).get("delivery")).startswith("deferred: "))
            atomic_file.write_text_atomic(transcript, accepted)
        if order == "legacy record":
            legacy = record.copy()
            del legacy["pane_dispatch_id"], legacy["finished_at"]
            with detached_runs.record_lock(path):
                atomic_file.write_text_atomic(path, json.dumps(legacy))
            with patch.object(send, "deliver_worker_text", return_value=send.Result("retry", "fixture")) as submit:
                self.assertEqual(detached_runs.deliver(legacy, detached_runs.CompletedCommand(0)),
                                 "deferred: worker transcript is unavailable: the run has no recorded finish time")
            submit.assert_not_called()
            with detached_runs.record_lock(path):
                atomic_file.write_text_atomic(path, json.dumps(record))
        if order == "reap after a late result":
            self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
        else:
            self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
        self.assertEqual(self.case.reply.read_text().rstrip(), done)
        self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "completed")
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertEqual(self.case.agent.submitted, submissions)
        self.assertEqual(len(self.case.agent.notices), notices + 1)
        saved = worker_lifecycle.read_json_object(path)
        self.assertIs(saved.get("completion_pending"), False)
        if deferred:
            self.assertEqual(saved.get("delivery"), detached_runs.COVERED_BY_DONE)
        state_path = self.case.run_dir / unit_runs.STATE_FILE
        before = (path.read_bytes(), state_path.read_bytes(), list(self.case.agent.notices))
        self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
        self.assertEqual((path.read_bytes(), state_path.read_bytes(), self.case.agent.notices), before)
        if order in ("reopen after prune", "working after reopen"):
            self.assertIn(name, detached_runs.prune_finished(path.parent))
            self.assertFalse(path.exists())
            backend = staff.backend(row)
            assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
            self.case.fixture.world.add(row, pane, family="codex" if isinstance(backend, staff.CodexBackend) else "claude")
            self.assertIn(pane, self.case.fixture.world.locations)
            parent = self.case.fixture.parent.id
            worker_name = staff.WorkerName.parse(name)
            if order == "working after reopen":
                self.case.agent.append(pane, "user", "Check another file.")
                self.case.fixture.world.locations[pane]["agent_status"] = "working"
                self.assertEqual(worker_panes.close(parent, worker_name), worker_panes.Kept("agent state is working"))
                self.assertIn(pane, self.case.fixture.world.locations)
            else:
                self.assertIsInstance(worker_panes.close(parent, worker_name), worker_panes.Closed)
                self.assertNotIn(pane, self.case.fixture.world.locations)

    def test_a_torn_final_reply_keeps_run_completion_pending_after_receipt_recovery(self) -> None:
        config = self.case.fixture.root / "conf"
        source = Path(__file__).resolve().parents[2] / "config"
        for name in ("detached_runs.conf", "postmaster.conf", "codex_daemon.conf"):
            _ = shutil.copyfile(source / name, config / name)
        self.case.agent.finish_after = 1
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        path, record = self.case.detached_record(row, "torn-final-reply")
        record.update(delivery="unconfirmed: the worker transcript has no result receipt", completion_pending=True)
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.case.agent.append(pane, "user", "Detached run: torn-final-reply\nRun finished: private verification")
        backend = staff.backend(row)
        assert isinstance(backend, (staff.ClaudeBackend, staff.CodexBackend))
        transcript = worker_admission.transcript_source(row, backend)
        accepted = transcript.read_text()
        atomic_file.write_text_atomic(transcript, accepted + '{"type":')
        self.assertIs(turn_end.run_finished(row.id), detached_runs.CompletionReplay.RETRY)
        saved = worker_lifecycle.read_json_object(path)
        self.assertEqual(saved.get("delivery"), "already delivered: torn-final-reply")
        self.assertIs(saved.get("completion_pending"), True)
        self.assertFalse(self.case.reply.exists())
        self.assertEqual(self.case.agent.notices, [])

        atomic_file.write_text_atomic(transcript, accepted)
        self.case.agent.complete(pane)
        self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
        self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)
        self.assertEqual(self.case.reply.read_text(), self.case.agent.replies[-1] + "\n")
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertEqual(len(self.case.agent.notices), 1)

    def test_restore_reports_pending_close_and_retries_without_resending_done(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        self.case.agent.notice_failures = 1
        with self.assertRaisesRegex(ValueError, "director message failed"):
            _ = self.case.finish()
        with (patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False),
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO())):
            self.assertIs(worker.restore(self.case.fixture.parent.id), worker.ExitCode.UNCERTAIN)
        self.assertNotIn("workers restored", output.getvalue())
        self.assertIn(pane, self.case.fixture.world.locations)
        passed = worker_panes.worker_pass(row, self.case.run_dir)
        self.assertTrue(pass_notices.is_settled(self.case.run_dir, str(passed["instance_id"]), "done"))
        completion = progress_history.hook_completion(passed)
        assert isinstance(completion, progress_history.HookCompletion)
        self.assertFalse(completion.pane_closed)
        self.assertFalse(completion.finished)
        notice, diagnostic = self.case.agent.notices
        self.assertIn("phase 8: done", notice)
        self.assertIn("pane close is incomplete", diagnostic)
        self.assertIs(worker.restore(self.case.fixture.parent.id), worker.ExitCode.OK)
        self.assertEqual(self.case.agent.notices, [notice, diagnostic])
        self.assertEqual(self.case.fixture.world.closed, [(pane, "terminal-" + pane)])
        finished = progress_history.hook_completion(worker_panes.worker_pass(row, self.case.run_dir))
        assert isinstance(finished, progress_history.HookCompletion)
        self.assertTrue(finished.finished)

        with self.subTest(retry="reap after a failed close"):
            name = "retry-close"
            self.case.agent.finish_after = 1
            self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
            pane, _ = self.case.agent.submitted[-1]
            row = self.case.agent.row(pane)
            path, record = self.case.detached_record(row, "retry-close-result")
            record.update(delivery=f"sent: worker {name} at {pane}", completion_pending=True)
            with detached_runs.record_lock(path):
                atomic_file.write_text_atomic(path, json.dumps(record))
            self.case.agent.append(pane, "user", f"Detached run: {record['run_id']}\nRun finished: tests")
            self.case.agent.complete(pane)
            with patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False):
                self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
            self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), True)
            self.assertIn(pane, self.case.fixture.world.locations)
            notices = list(self.case.agent.notices)
            self.assertEqual(detached_runs.reap_orphans(str(self.case.run_dir)), [])
            self.assertNotIn(pane, self.case.fixture.world.locations)
            self.assertIs(worker_lifecycle.read_json_object(path).get("completion_pending"), False)
            self.assertEqual(self.case.agent.notices, notices)

        for failure in ("pane close", "state write"):
            with self.subTest(retry="finish-pass after recorder cancellation", failure=failure):
                name = "retry-cancel-" + failure.replace(" ", "-")
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                pane, _ = self.case.agent.submitted[-1]
                row = self.case.agent.row(pane)
                run = self.case.run_dir
                instance = str(worker_panes.worker_pass(row, run)["instance_id"])
                history = Path(str(worker_panes.run_state(run)["history_file"]))
                state_file = run / unit_runs.STATE_FILE
                self.case.fixture.world.locations[pane]["agent_status"] = "idle"
                args = ["progress_history.py", "finish-pass", "--session-dir", str(run),
                        "--status", "canceled", "--pass-instance-id", instance]

                def cancel_events() -> list[dict[str, object]]:
                    events = [cast(dict[str, object], json.loads(line)) for line in history.read_text().splitlines()]
                    return [event for event in events if event.get("pass_instance_id") == instance
                            and event.get("event_type") == "pass_finished"]

                def fail_state_write(path: Path, value: str) -> None:
                    if path == state_file:
                        raise OSError("state write failed")
                    atomic_file.write_text_atomic(path, value)

                with patch.object(sys, "argv", args):
                    if failure == "state write":
                        with (patch.object(pass_recorder, "write_text_atomic", side_effect=fail_state_write),
                              self.assertRaisesRegex(SystemExit, "state write failed")):
                            progress_history.main()
                        self.assertEqual(worker_panes.worker_pass(row, run)["status"], "active")
                    else:
                        with (patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False),
                              redirect_stderr(io.StringIO())):
                            progress_history.main()
                        self.assertEqual(worker_panes.worker_pass(row, run)["status"], "canceled")
                    self.assertIn(pane, self.case.fixture.world.locations)
                    state = state_file.read_bytes()
                    original, = cancel_events()
                    progress_history.main()
                self.assertNotIn(pane, self.case.fixture.world.locations)
                if failure == "pane close":
                    self.assertEqual(state_file.read_bytes(), state)
                self.assertEqual(cancel_events(), [original])
                canceled = worker_panes.worker_pass(row, run)
                self.assertEqual(canceled["status"], "canceled")
                self.assertEqual(canceled["finished_at"], original["timestamp_epoch"])
                self.assertEqual(staff.by_id(row.id), row)

    def test_restore_retries_a_saved_close_diagnostic_after_delivery_fails(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        shutdown_record.save([str(row.id)])
        deliver = send.send

        def fail_close_diagnostic(options: send.Options) -> send.Result:
            if "kept pane" in send.body_text(options):
                return send.Result("failed", "close diagnostic could not reach the director")
            return deliver(options)

        with (patch.object(self.case.fixture.world, "close_terminal_pane", return_value=False),
              patch.object(send, "send", side_effect=fail_close_diagnostic),
              redirect_stderr(io.StringIO())):
            with self.assertRaisesRegex(ValueError, "close diagnostic could not reach the director"):
                _ = self.case.finish()
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertEqual(shutdown_record.read(), [str(row.id)])
        self.assertIn(pane, self.case.fixture.world.locations)

        self.assertIs(worker.restore(self.case.fixture.parent.id), worker.ExitCode.OK)
        done, diagnostic = self.case.agent.notices
        self.assertIn("phase 8: done", done)
        self.assertIn("pane close is incomplete", diagnostic)
        self.assertEqual(shutdown_record.read(), [])
        self.assertNotIn(pane, self.case.fixture.world.locations)
        self.assertIs(worker.restore(self.case.fixture.parent.id), worker.ExitCode.OK)
        self.assertEqual(self.case.agent.notices, [done, diagnostic])

    def test_stale_done_names_both_ids_and_keeps_the_pass_open(self) -> None:
        earlier = "fb50262d-03af-4ad7-919f-87baa7c73fb1"
        for managed in (False, True):
            with self.subTest(managed=managed):
                name = staff.WorkerName.parse("review" if managed else "impl")
                self.case.agent.finish_after = 1
                reply = self.case.run_dir / f"{name.value}_reply.md"
                self.assertIs(worker.dispatch(self.case.fixture.parent.id, name, "impl", self.case.prompt,
                    session_dir=self.case.run_dir, slot=name.value, phase="8", kind="impl", launcher_command=["assign.sh", "--to", name.value],
                    reply=reply, completion_actions=pass_recorder.CompletionActions(end_worker=managed)),
                    worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                active = progress_history.active_dispatch(row.id, self.case.run_dir)
                assert isinstance(active, progress_history.ActiveDispatch)
                expected = active.instance_id
                turn = reply.with_name(reply.stem + "_turn" + reply.suffix)
                header = f"{name.value}: phase 8: no DONE; reply {turn}; pass {expected}"
                self.case.agent.without_done = True
                if not managed:
                    self.case.agent.final = "The work is complete."
                    self.case.agent.complete(pane)
                    self.assertIs(self.case.finish(name.value), turn_end.TurnOutcome.NO_DONE)
                    self.assertEqual(self.case.agent.notices[-1], f"{header}\nThe work is complete.")
                period = "." if managed else ""
                self.case.agent.final = f"The work is complete.\nDONE {earlier}{period}"
                self.case.agent.complete(pane)
                self.assertIs(self.case.finish(name.value), turn_end.TurnOutcome.NO_DONE)
                self.assertEqual(self.case.agent.notices[-1], f"{header}\n"
                    + f"no DONE for pass {expected}; the reply ends with DONE {earlier} from another pass")
                self.assertEqual(turn.read_text(), self.case.agent.final + "\n")
                self.assertEqual(progress_history.active_dispatch(row.id, self.case.run_dir), active)
                self.assertIn(pane, self.case.fixture.world.locations)
                self.assertFalse(reply.exists())

                self.case.agent.without_done = False
                self.case.agent.final = "The work is complete."
                self.case.agent.complete(pane)
                self.assertIs(self.case.finish(name.value), turn_end.TurnOutcome.DONE)
                self.assertTrue(progress_history.reply_completed(self.case.run_dir, reply))
                self.assertEqual(self.case.fixture.world.closed.count((pane, "terminal-" + pane)), 1)

    def test_overlapping_done_handlers_use_one_delivery_key_and_send_once(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        instance = worker_panes.worker_pass(row, self.case.run_dir)["instance_id"]
        event = turn_end.WorkerEvent(str(row.current_session), reply=self.case.agent.replies[-1])
        prepare = turn_end._handle_locked  # pyright: ignore[reportPrivateUsage]
        first = prepare(event)
        second = prepare(event)
        assert isinstance(first, turn_end.TurnDecision) and isinstance(second, turn_end.TurnDecision)
        with patch.object(send, "send", wraps=send.send) as deliver:
            _ = first.send_notice()
            _ = second.send_notice()
        deliver.assert_called_once()
        self.assertEqual(cast(send.Options, deliver.call_args.args[0]).key, f"worker-completion:{instance}:done")
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertEqual(self.case.fixture.world.closed, [(pane, "terminal-" + pane)])

    def test_late_done_waits_for_live_detached_work(self) -> None:
        self.assertIs(self.case.dispatch(), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        self.assertEqual(progress_history.cancel_worker_passes(self.case.run_dir, row.id), 1)
        _, live = self.case.detached_record(row, "still-working")
        live["status"] = "running"
        with patch.object(detached_runs, "live_runs", return_value=[live]):
            for kind in (turn_end.EventKind.TURN_END, turn_end.EventKind.RUN_FINISHED):
                self.assertEqual(self.case.finish(kind=kind), turn_end.TurnOutcome.WAITING_ON_RUNS)
                self.assertEqual(self.case.agent.notices, [])
                self.assertIn(pane, self.case.fixture.world.locations)
        self.assertEqual(self.case.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.STOPPED)
        self.assertEqual(len(self.case.agent.notices), 1)

    def test_done_after_pass_end_reports_the_saved_reply_once(self) -> None:
        for earlier in ("error", "canceled"):
            with self.subTest(earlier=earlier):
                name = "late-" + earlier
                self.case.agent.finish_after = 1
                opened = self.case.opened(name)
                self.assertIs(self.case.dispatch(name), worker.DispatchSubmission.SUBMITTED)
                run = self.case.run_dir
                passed = worker_panes.worker_pass(opened.row, run)
                instance = str(passed["instance_id"])
                if earlier == "error":
                    progress_history.complete_dispatch(run, "impl", instance, status="error")
                    error_reply = run / "error.txt"
                    worker.save_reply(error_reply, "Dispatch ended at the compaction bound.")
                    progress_history.save_hook_completion(run, instance, progress_history.HookCompletion(
                        "error", str(error_reply), actions_done=True, role_released=True, finished=True, recording_done=True))
                else:
                    self.assertEqual(progress_history.cancel_worker_passes(run, opened.row.id), 1)

                self.case.agent.complete(opened.pane)
                reply = self.case.agent.replies[-1]
                notices = len(self.case.agent.notices)
                self.case.agent.notice_failures = 1
                with self.assertRaisesRegex(ValueError, "director message failed"):
                    _ = self.case.finish(name)
                self.assertIn(opened.pane, self.case.fixture.world.locations)
                original_send = send.send

                def send_before_close(options: send.Options) -> send.Result:
                    self.assertIn(opened.pane, self.case.fixture.world.locations)
                    return original_send(options)

                with patch.object(send, "send", side_effect=send_before_close):
                    _ = self.case.finish(name)
                    _ = self.case.finish(name)
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                notice = self.case.agent.notices[-1]
                path = self.case.reply.with_name(f"{self.case.reply.stem}_late_{instance}{self.case.reply.suffix}")
                if earlier == "canceled":
                    self.assertIn(f"{name}: phase 8: stopped; reply {path}\nPass {instance}.", notice)
                    self.assertIn("The worker was canceled.", notice)
                else:
                    self.assertIn(f"{name}: phase 8: late DONE; reply {path}; pass {instance}", notice)
                    self.assertIn(f"The earlier pass ended with state {earlier}.", notice)
                self.assertEqual(path.read_text(), reply + "\n")
                self.assertFalse(self.case.reply.exists())
                recorded = worker_panes.worker_pass(opened.row, run)
                self.assertEqual(recorded["status"], earlier)
                self.assertTrue(pass_notices.is_settled(run, instance, "stopped" if earlier == "canceled" else "late DONE"))
                self.assertNotIn(opened.pane, self.case.fixture.world.locations)

    def test_a_failed_notice_recovers_the_completed_pass_without_another_close(self) -> None:
        result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse("impl"), "impl", self.case.prompt,
            session_dir=self.case.run_dir, slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"],
            reply=self.case.reply, completion_actions=pass_recorder.CompletionActions(end_worker=True))
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        self.case.agent.notice_failures = 1
        with self.assertRaisesRegex(ValueError, "director message failed"):
            _ = self.case.finish()
        passed = worker_panes.worker_pass(row, self.case.run_dir)
        self.assertEqual(passed["status"], "completed")
        instance = str(passed["instance_id"])
        self.assertFalse(pass_notices.is_settled(self.case.run_dir, instance, "done"))
        self.assertEqual(staff.by_id(row.id), row)
        self.assertIn(pane, self.case.fixture.world.locations)
        self.assertEqual(self.case.fixture.world.closed, [])
        close = self.case.fixture.world.close_terminal_pane

        def close_after_delivery(pane: str, terminal: str) -> bool:
            self.assertEqual(len(self.case.agent.notices), 1)
            self.assertTrue(pass_notices.is_settled(self.case.run_dir, instance, "done"))
            return close(pane, terminal)

        with patch.object(self.case.fixture.world, "close_terminal_pane", side_effect=close_after_delivery):
            self.assertEqual(self.case.finish(), turn_end.TurnOutcome.DONE)
        self.assertEqual(len(self.case.agent.notices), 1)
        self.assertEqual(self.case.fixture.world.closed, [(pane, "terminal-" + pane)])
        self.assertIsNone(staff.by_id(row.id))
        self.assertEqual(turn_end.handle(turn_end.WorkerEvent(str(row.current_session))), turn_end.TurnOutcome.IGNORED)

        for recovery in ("plain", "same hold", "failed notice"):
            with (self.subTest(recovery="dispatch in another run", case=recovery),
                  patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.case.fixture.root / "holds")}),
                  self.case.fixture.world.process_starts()):
                name = "late-" + recovery.replace(" ", "-")
                role: staff.WorkerRole = "astra-reviewer" if recovery == "same hold" else "impl"
                self.case.agent.finish_after = 0
                opened = self.case.opened(name, role)
                row = opened.row
                actions = (pass_recorder.CompletionActions(end_worker=True, held_role=str(row.id))
                           if recovery == "same hold" else progress_history.NO_COMPLETION_ACTIONS)
                if recovery == "same hold":
                    pid = self.case.fixture.world.foreground[opened.pane][0]
                    held = astra_roles.hold("reviewer", astra_roles.ExecProcess(pid), str(row.id))
                    self.assertNotIsInstance(held, astra_roles.HoldWorkerUnavailable)
                first_run = self.case.run_dir
                next_run = self.case.fixture.root / ("next-run-" + recovery.replace(" ", "-"))
                next_state = cast(dict[str, object], json.loads((first_run / "progress_history_state.json").read_text()))
                next_history = prepare_recorder_history(next_run / "history.jsonl")
                next_state["history_file"] = str(next_history)
                atomic_file.write_text_atomic(next_run / "progress_history_state.json", json.dumps(next_state))
                first_reply = first_run / (name + "-reply.md")
                self.assertIs(worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), role, self.case.prompt,
                    session_dir=first_run, slot=name, phase="8", kind=role, launcher_command=["first-assign"],
                    reply=first_reply, completion_actions=actions), worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                row = self.case.agent.row(pane)
                passed = worker_panes.worker_pass(row, first_run)
                instance = str(passed["instance_id"])
                worker.save_reply(first_reply, self.case.agent.replies[-1])
                progress_history.save_hook_completion(first_run, instance,
                    progress_history.HookCompletion(turn_end.TurnOutcome.DONE, str(first_reply), role_released=False))
                self.assertEqual(worker_panes.worker_run(row), first_run)
                notices = len(self.case.agent.notices)
                closed = self.case.fixture.world.closed.copy()
                self.case.agent.finish_after = 1
                self.case.agent.notice_failures = int(recovery == "failed notice")
                if recovery == "same hold":
                    pid = self.case.fixture.world.foreground[pane][0]
                    held = astra_roles.hold("reviewer", astra_roles.ExecProcess(pid), str(row.id))
                    self.assertNotIsInstance(held, astra_roles.HoldWorkerUnavailable)
                with redirect_stderr(io.StringIO()) as errors:
                    result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), role, self.case.prompt,
                        session_dir=next_run, slot=name, phase="8", kind=role, launcher_command=["next-assign"],
                        reply=next_run / "reply.md", completion_actions=actions)
                self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(len(self.case.agent.notices), notices)
                record_path, = (path for path in first_run.glob(pass_notices.NOTICE_FILES)
                                if (entry := pass_notices.PassNotices.read(path)) is not None
                                and entry.route.instance_id == instance)
                pending = pass_notices.PassNotices.read(record_path)
                assert pending is not None
                self.assertFalse(pending.notices["done"].settled)
                if recovery == "failed notice":
                    _ = detached_runs.reap_orphans(str(first_run))
                    self.assertEqual(len(self.case.agent.notices), notices)
                _ = detached_runs.reap_orphans(str(first_run))
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                notice = self.case.agent.notices[-1]
                self.assertIn(f"; pass {instance}", notice)
                self.assertIn(f"late result of pass {instance}", notice)
                if recovery == "same hold":
                    live, = astra_roles.live_roles(staff.snapshot())
                    self.assertEqual(live["staff_id"], row.id)
                    self.assertIsNotNone(staff.by_id(row.id))
                    self.assertEqual(errors.getvalue(), "")
                self.assertIn(pane, self.case.fixture.world.locations)
                self.assertEqual(self.case.fixture.world.closed, closed)
                self.assertEqual(worker_panes.worker_run(row), next_run)

    def test_restored_stage_one_done_submits_stage_two(self) -> None:
        name = "review"
        run = self.case.run_dir
        ready = run / "review.ready"
        self.case.agent.finish_after = 1
        result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "review", self.case.prompt,
            session_dir=run, slot=name, phase="8", kind="review", launcher_command=[name],
            early_ready=ready, reply=self.case.reply)
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        passed = worker_panes.worker_pass(row, run)
        delivery = progress_history.dispatch_delivery(passed)
        runs = run / detached_runs.DETACHED_RUNS_DIR
        runs.mkdir(exist_ok=True)
        log = runs / "earlier-result.log"
        record: detached_runs.RunRecord = {
            "run_id": log.stem, "worker": row.name, "staff_id": row.id, "status": "finished",
            "started_at": 0, "pid": 0, "command": ["true"], "label": "private verification",
            "log": str(log), "session_dir": str(run), "director_dir": str(run), "done_message_file": "",
            "pane_dispatch_id": "earlier-pass", "delivery": "deferred: worker is working",
            "exit_code": 0, "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_file.write_text_atomic(log.with_suffix(".json"), json.dumps(record))
        send_input = self.case.fixture.world.run

        def accept_result(*arguments: str, deadline: float = math.inf) -> str:
            if arguments[:3] == ("agent", "prompt", pane):
                self.case.agent.complete(pane)
                self.case.agent.append(pane, "user", arguments[3])
            return send_input(*arguments, deadline=deadline)

        self.case.fixture.world.locations[pane]["agent_status"] = "idle"
        with (patch.object(worker_panes, "worker_pass", return_value={**passed, "instance_id": "earlier-pass"}),
              patch.object(self.case.fixture.world, "run", new=accept_result)):
            self.assertEqual(detached_runs.deliver(record, detached_runs.CompletedCommand(0)),
                             f"sent: worker {row.name} at {pane}")
        self.case.agent.append(pane, "assistant", "The earlier check passed.")
        atomic_file.write_text_atomic(ready, "ready\n")

        before = len(self.case.agent.submitted)
        self.assertIs(self.case.finish(name), turn_end.TurnOutcome.SUBMITTED)
        self.assertEqual(len(self.case.agent.submitted), before + 1)
        self.assertIn(f"DONE {worker_panes.stage_two_completion_id(delivery.completion_id)}",
                      self.case.agent.submitted[-1][1])

    def start_stage_two(self, name: str) -> str:
        """Submit a review with an early-ready stage, mark the stage ready, and return the pane."""
        ready = self.case.run_dir / f"{name}.ready"
        result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "review", self.case.prompt,
            session_dir=self.case.run_dir, slot=name, phase="8", kind="review", launcher_command=[name],
            early_ready=ready, reply=self.case.reply)
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        atomic_file.write_text_atomic(ready, "ready\n")
        return self.case.agent.submitted[-1][0]

    def start_busy_stage_two(self, name: str) -> tuple[staff.StaffRow, str]:
        pane = self.start_stage_two(name)
        self.case.fixture.world.locations[pane]["agent_status"] = "working"
        self.case.fixture.world.first_turn_timeout = True
        row = self.case.agent.row(pane)
        return row, str(worker_panes.worker_pass(row, self.case.run_dir)["instance_id"])

    def test_a_failed_busy_notice_is_not_resent_after_its_delivery_is_confirmed(self) -> None:
        row, instance = self.start_busy_stage_two("review-busy-retry")
        with (patch.object(pass_notices, "_send", side_effect=OSError("director unreachable")),
              self.assertRaisesRegex(OSError, "director unreachable")):
            _ = self.case.finish(row.name, turn_end.EventKind.STAGE_READY)
        self.case.fixture.world.first_turn_timeout = False
        notices = len(self.case.agent.notices)
        self.assertEqual(self.case.finish(row.name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
        pass_notices.retry(self.case.run_dir)
        self.assertEqual(len(self.case.agent.notices), notices)
        record = pass_notices.read(self.case.run_dir, instance)
        assert record is not None
        busy, = (notice for notice in record.notices.values() if notice.source_event_id.startswith("busy:"))
        self.assertTrue(busy.settled)

    def test_a_stop_after_the_busy_check_imports_no_legacy_notice(self) -> None:
        row, instance = self.start_busy_stage_two("review-busy-stop")
        with (patch.object(pass_notices, "stage_status", side_effect=OSError("stopped before staging")),
              self.assertRaisesRegex(OSError, "stopped before staging")):
            _ = self.case.finish(row.name, turn_end.EventKind.STAGE_READY)
        self.assertNotIn("hook_notice", worker_panes.worker_pass(row, self.case.run_dir))
        self.case.fixture.world.first_turn_timeout = False
        self.assertEqual(self.case.finish(row.name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
        self.assertEqual(self.case.finish(row.name), turn_end.TurnOutcome.DONE)
        record = pass_notices.read(self.case.run_dir, instance)
        assert record is not None
        self.assertEqual([notice.text for notice in record.notices.values() if notice.legacy_stamp], [])

    def test_stage_two_recovers_busy_and_interrupted_submission(self) -> None:
        for reason in ("busy", "interrupt_before_submit", "interrupt_stage_two"):
            with self.subTest(reason=reason):
                name = "review-" + reason.replace("_", "-")
                pane = self.start_stage_two(name)
                before = len(self.case.agent.submitted)
                if reason == "busy":
                    self.case.fixture.world.locations[pane]["agent_status"] = "working"
                    self.case.fixture.world.first_turn_timeout = True
                    self.assertEqual(self.case.finish(name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.BUSY)
                    self.assertIn("busy", self.case.agent.notices[-1])
                    self.case.fixture.world.first_turn_timeout = False
                elif reason == "interrupt_stage_two":
                    self.case.agent.interrupt = "prompt"
                    self.assertEqual(self.case.finish(name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.UNCERTAIN)
                    self.assertIn("prompt delivered to pane", self.case.agent.notices[-1])
                else:
                    save = progress_history.save_dispatch_delivery

                    def interrupt(run: Path, instance: str, delivery: pass_recorder.DispatchDelivery) -> None:
                        save(run, instance, delivery)
                        raise OSError("interrupted after Stage 2 save")

                    with patch.object(progress_history, "save_dispatch_delivery", side_effect=interrupt):
                        with self.assertRaisesRegex(OSError, "Stage 2 save"):
                            _ = self.case.finish(name, turn_end.EventKind.STAGE_READY)
                self.assertEqual(len(self.case.agent.submitted), before + (reason == "interrupt_stage_two"))
                self.assertEqual(self.case.finish(name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.SUBMITTED)
                self.assertEqual(len(self.case.agent.submitted), before + 1)
                if reason == "interrupt_stage_two":
                    self.case.agent.complete(pane)
                self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
                self.assertEqual(self.case.finish(name, turn_end.EventKind.STAGE_READY), turn_end.TurnOutcome.IGNORED)
                self.assertEqual(len(self.case.agent.submitted), before + 1)

    def test_a_late_hook_does_not_end_the_next_managed_review(self) -> None:
        name = "astra-reviewer"
        self.assertIs(self.case.dispatch(name, "astra-reviewer"), worker.DispatchSubmission.SUBMITTED)
        old_reply = self.case.agent.replies[-1]
        self.assertEqual(self.case.finish(name), turn_end.TurnOutcome.DONE)
        current = self.case.opened(name, "astra-reviewer")
        following = self.case.prompt.with_name("review-b.md")
        atomic_file.write_text_atomic(following, "Review B.")
        self.case.agent.finish_after = 1
        with (patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.case.fixture.root / "holds")}),
              self.case.fixture.world.process_starts()):
            pid = self.case.fixture.world.foreground[current.pane][0]
            held = astra_roles.hold("reviewer", astra_roles.ExecProcess(pid), str(current.row.id))
            self.assertNotIsInstance(held, astra_roles.HoldWorkerUnavailable)
            result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "astra-reviewer", following,
                session_dir=self.case.run_dir, slot="impl", phase="8", kind="astra", launcher_command=["review-b"],
                reply=self.case.reply, started=current,
                completion_actions=pass_recorder.CompletionActions(end_worker=True, held_role=str(current.row.id)))
            self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
            active = progress_history.active_dispatch(current.row.id, self.case.run_dir)
            event = turn_end.hook_event({"hook_event_name": "Stop", "session_id": str(current.row.current_session),
                                         "last_assistant_message": old_reply})
            stops = self.case.fixture.world.stops.copy()
            cases = (("", old_reply), ("Still reviewing.", old_reply),
                     ("The older result is pending.", "The work is complete.\nDONE another-dispatch"))
            for partial, hook_reply in cases:
                with self.subTest(partial=partial, hook_reply=hook_reply):
                    event = replace(event, reply=hook_reply)
                    if partial:
                        self.case.agent.append(current.pane, "assistant", partial)
                    notices = len(self.case.agent.notices)
                    expected = turn_end.TurnOutcome.NO_DONE if partial else turn_end.TurnOutcome.RETRY_EVIDENCE
                    self.assertEqual(turn_end.handle(event), expected)
                    self.assertTrue(worker.pending_done_path(self.case.run_dir, current.row.id).is_file())
                    if partial:
                        notice, = self.case.agent.notices[notices:]
                        self.assertIn(partial, notice)
                        self.assertNotIn(hook_reply, notice)
                    else:
                        self.assertEqual(len(self.case.agent.notices), notices)
                    self.assertEqual(progress_history.active_dispatch(current.row.id, self.case.run_dir), active)
                    self.assertIn(current.pane, self.case.fixture.world.locations)
                    self.assertEqual(staff.by_id(current.row.id), current.row)
                    live, = astra_roles.live_roles(staff.snapshot())
                    self.assertEqual(live["staff_id"], current.row.id)
                    self.assertEqual(self.case.fixture.world.stops, stops)

    def test_a_failed_repair_action_records_error_and_sends_the_director_its_reason(self) -> None:
        status = self.case.run_dir / "fix.status"
        original = self.case.agent.run_process

        def fail_landed(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            if len(command) > 2 and command[1].endswith("/findings.py") and command[2] == "landed":
                return subprocess.CompletedProcess(command, 2, "", "ledger write failed")
            return original(command, **options)

        self.case.agent.run_process = fail_landed
        result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse("fix"), "fix", self.case.prompt,
            session_dir=self.case.run_dir, slot="fix", phase="8", kind="fix", launcher_command=["fix"], reply=self.case.reply,
            fix_cause="Repair the failed gate",
            completion_actions=pass_recorder.CompletionActions(status_file=str(status), resolve_fix=True))
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        row = self.case.agent.row(self.case.agent.submitted[-1][0])
        self.assertEqual(self.case.finish("fix"), turn_end.TurnOutcome.ERROR)
        self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "error")
        self.assertEqual(status.read_text(), "error\n")
        self.assertIn("ledger write failed", self.case.agent.notices[-1])
        self.assertEqual(len(self.case.agent.notices), 1)

        with self.subTest(completion="error reply is missing"):
            current = self.case.opened("missing-error-reply")
            instance = progress_history.begin_dispatch(self.case.run_dir, slot=current.row.name, phase="8", role="impl",
                worker_name=current.row.name, worker_id=current.row.id, launcher_command=["assign.sh"],
                prompt=self.case.prompt, kind="impl", lens="")
            missing = self.case.run_dir / "missing-error-reply.md"
            progress_history.save_dispatch_delivery(self.case.run_dir, instance,
                pass_recorder.DispatchDelivery("saved prompt", instance, str(missing)))
            progress_history.complete_dispatch(self.case.run_dir, current.row.name, instance, status="error")
            progress_history.save_hook_completion(self.case.run_dir, instance,
                progress_history.HookCompletion("error", str(missing), ("error reply write failed",)))
            self.assertIs(turn_end.run_finished(current.row.id), detached_runs.CompletionReplay.HANDLED)
            self.assertEqual(worker_panes.worker_pass(current.row, self.case.run_dir)["status"], "error")
            self.assertEqual(len(self.case.agent.notices), 2)
            self.assertIn("error reply write failed", self.case.agent.notices[-1])
            self.assertFalse(missing.exists())

    def test_submission_keeps_the_held_role_until_the_hook_completes(self) -> None:
        self.case.agent.finish_after = 1
        row = self.case.opened("astra-reviewer", "astra-reviewer").row
        process = self.case.fixture.world.foreground[next(pane for pane in self.case.fixture.world.foreground
            if self.case.fixture.world.processes[self.case.fixture.world.foreground[pane][0]].current_identity == row.current_session)][0]
        with patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.case.fixture.root / "holds")}):
            held = astra_roles.hold("reviewer", astra_roles.ExecProcess(process, process_start.Started("private-start")), str(row.id))
            self.assertNotIsInstance(held, astra_roles.HoldWorkerUnavailable)
            result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(row.name), "astra-reviewer", self.case.prompt,
                session_dir=self.case.run_dir, slot=row.name, phase="8", kind="astra", launcher_command=["assign.sh"],
                reply=self.case.reply, completion_actions=pass_recorder.CompletionActions(end_worker=True, held_role=str(row.id)))
            self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
            hold = self.case.fixture.root / "holds" / f"{row.id}.json"
            before = hold.read_bytes()
            self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.ActiveDispatch)
            self.assertEqual(hold.read_bytes(), before)
            self.assertEqual(staff.by_id(row.id), replace(row, role=staff.KnownWorkerRole("astra-reviewer")))
            self.case.agent.complete(self.case.agent.submitted[-1][0])
            self.assertEqual(self.case.finish(row.name), turn_end.TurnOutcome.DONE)
            self.assertFalse(hold.exists())
            self.assertIsNone(staff.by_id(row.id))

    def test_the_astra_launcher_returns_with_an_active_pass_and_a_worker_process_hold(self) -> None:
        router = test_worker.PaneCommandRouter()
        router.setup(self, self.case.fixture.root)
        args = assign.Arguments(new_worker="astra-reviewer", reply_file=str(self.case.reply), policy="astra-reviewer")
        hold_role = astra_roles.hold
        bootstrap_hooks: list[Future[turn_end.TurnOutcome]] = []

        def bootstrap_during_hold(role: astra_roles.AstraRole, lifetime: astra_roles.ExecProcess | astra_roles.HeldDesignRole,
                                  staff_id: str = "") -> astra_roles.HeldRole | astra_roles.HoldWorkerUnavailable:
            row = staff.by_id(staff.StaffId(staff_id))
            assert row is not None
            state = worker_panes.observe_worker(self.case.fixture.parent.id, row, staff.snapshot())
            assert isinstance(state, worker_panes.OpenWorker)
            self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.NoActiveDispatch)
            with worker_panes.worker_lock(self.case.fixture.parent.id, staff.WorkerName.parse(row.name)) as competing:
                self.assertIsInstance(competing, worker_panes.Kept)
            bootstrap_hooks.append(hooks.submit(turn_end.handle, turn_end.WorkerEvent(str(row.current_session), reply="ready")))
            self.assertIn(state.pane, self.case.fixture.world.locations)
            return hold_role(role, lifetime, staff_id)

        with (ThreadPoolExecutor(max_workers=1) as hooks,
              patch.dict(os.environ, {"ASTRA_ROLES_STATE": str(self.case.fixture.root / "holds")}),
              patch.object(astra_roles, "hold", side_effect=bootstrap_during_hold),
              self.case.fixture.world.process_starts()):
            code = assign.execute_astra_review_herdr(args, self.case.run_dir, self.case.fixture.root,
                self.case.prompt, dict(os.environ))
            self.assertIs(code, worker.DispatchSubmission.SUBMITTED)
            self.assertEqual(bootstrap_hooks.pop().result(timeout=LauncherHangGuard("review bootstrap hook").remaining()),
                             turn_end.TurnOutcome.IGNORED)
            pane = self.case.agent.submitted[-1][0]
            row = self.case.agent.row(pane)
            hold = self.case.fixture.root / "holds" / f"{row.id}.json"
            record = cast(dict[str, object], json.loads(hold.read_text()))
            self.assertEqual(record["pid"], self.case.fixture.world.foreground[pane][0])
            live, = astra_roles.live_roles(staff.snapshot())
            self.assertEqual((live["staff_id"], live["thread_id"]), (row.id, row.current_session))
            self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.ActiveDispatch)
            self.assertEqual(staff.by_id(row.id), row)
            self.assertEqual(self.case.finish(row.name), turn_end.TurnOutcome.DONE)
            self.assertFalse(hold.exists())
            self.assertIsNone(staff.by_id(row.id))


if __name__ == "__main__":
    _ = unittest.main()

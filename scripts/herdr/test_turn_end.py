"""Exercise native hook payloads against private staff and transcripts."""
from __future__ import annotations

from scripts.lib.checkout_test_support import python_tool_source

import json
import io
import os
import shutil
import subprocess
import sys
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from threading import Event, current_thread
from tempfile import TemporaryDirectory
from pathlib import Path
from dataclasses import replace
from typing import cast, final, override
from unittest.mock import patch

from scripts.accounts import refusals
from scripts.agents import detached_runs, restart, shutdown_record, worker_lifecycle
from scripts.astra import astra_roles
from scripts.direct import assign, progress_history
from scripts.herdr import herdr, side_pane, turn_end, worker_panes
from scripts.lib import atomic_file, process_start, scratch
from scripts.message import send
from scripts.lib.test_hang_guard import LauncherHangGuard, TEST_HANG_GUARD_SECS
from scripts.lib.test_models_registry import write_registry
from scripts.production import staff, test_worker, worker, worker_admission
from scripts.production.staff_test_support import fixture_worker
from scripts.whoami import agent_accounts, note_file, quota_alert


class NativeHookEntryTests(unittest.TestCase):
    def test_native_entry_starts_a_separate_handler_without_calling_handle(self) -> None:
        real_popen = subprocess.Popen
        for family, size in (("codex", 1), ("claude", 1), ("codex", 5_000)):
            with self.subTest(family=family, size=size), TemporaryDirectory() as folder:
                reply = 'The work is complete.\nDONE "dispatch"' * size
                value: dict[str, object] = {"hook_event_name": "Stop", "session_id": "native-thread", "last_assistant_message": reply}
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
                                 {"thread_id": "native-thread", "kind": "turn end", "reply": reply})
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
            handler.assert_called_once_with(expected)
            start.assert_not_called()
            self.assertEqual(output.getvalue(), "")


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
                self.case.agent.finish_after = int(transcript != "current")
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
                        kept_folders.append(kept)
                    runs = self.case.run_dir / "detached_runs"
                    runs.mkdir(exist_ok=True)
                    for run_id, owner, owner_id in (("own", name, str(row.id)), ("director", "director", "")):
                        atomic_file.write_text_atomic(runs / f"{run_id}.json", json.dumps(
                            {"run_id": run_id, "worker": owner, "staff_id": owner_id, "status": "finished",
                             "started_at": datetime.now().timestamp()}))
                        atomic_file.write_text_atomic(runs / f"{run_id}.log", "output")
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
                else:
                    with ExitStack() as sends:
                        if close == "send-unlocked":
                            send_notice = send.send

                            def send_unlocked(options: send.Options) -> send.Result:
                                with worker_panes.worker_lock(self.case.fixture.parent.id, staff.WorkerName.parse(name)) as acquired:
                                    self.assertIsInstance(acquired, worker_panes.WorkerLockHold)
                                return send_notice(options)

                            _ = sends.enter_context(patch.object(send, "send", side_effect=send_unlocked))
                        self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.DONE)
                    if transcript != "current":
                        self.assertNotIn((pane, "terminal-" + pane), self.case.fixture.world.closed)
                        self.assertIn(pane, self.case.fixture.world.locations)
                        self.case.agent.append(pane, "assistant", self.case.agent.replies[-1])
                        self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.DONE)
                self.assertEqual(turn_end.handle(turn_end.hook_event(payload)), turn_end.TurnOutcome.IGNORED)
                messages = self.case.agent.notices[notices:]
                done = f"{name}: phase 8: done; reply {self.case.reply}"
                self.assertIn(done, messages[0])
                if transcript == "current":
                    self.assertEqual(len(messages), 1)
                else:
                    self.assertEqual(len(messages), 2)
                    self.assertIn(f"kept pane {pane}", messages[1])
                self.assertEqual(self.case.reply.read_text().strip(), self.case.agent.replies[-1])
                self.assertIsInstance(progress_history.active_dispatch(row.id, self.case.run_dir), progress_history.NoActiveDispatch)
                self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "completed")
                self.assertIn((pane, "terminal-" + pane), self.case.fixture.world.closed)
                self.assertNotIn(pane, self.case.fixture.world.locations)
                if close == "scratch":
                    self.assertEqual(list(Path(os.environ["TMPDIR"]).iterdir()), kept_folders)
                    self.assertEqual((kept_folders[0] / "peer.txt").read_text(), "peer output")
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
                _ = shutil.copyfile(Path(__file__).resolve().parents[2] / "config/accounts.conf",
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

    def test_completed_rerun_repeats_the_saved_kept_pane_anomaly(self) -> None:
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
                    self.assertEqual(self.case.agent.notices[notices:], [notice, diagnostic, notice + "\n" + diagnostic])
                    self.assertEqual(self.case.agent.notice_senders[notices:], [sender, sender, sender])
                    self.assertEqual(len(self.case.agent.submitted), submitted + 1)
                    self.assertIn(pane, self.case.fixture.world.locations)
                    self.assertEqual((self.case.reply.read_bytes(), self.case.reply.stat().st_mtime_ns), reply)
            self.assertNotEqual(*completions)

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
                            progress_history.DispatchDelivery(f"Read the task. DONE {instance}", instance,
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
                                progress_history.DispatchDelivery("The pending assignment.", instance, str(self.case.reply)))
                            before = (self.case.run_dir / "progress_history_state.json").read_bytes()
                        self.assertEqual(pending.result(timeout=LauncherHangGuard("queued bootstrap hook").remaining()),
                                         turn_end.TurnOutcome.IGNORED)
                else:
                    before = (self.case.run_dir / "progress_history_state.json").read_bytes()
                    self.assertEqual(self.case.finish(name, event), turn_end.TurnOutcome.IGNORED)
                self.assertEqual(opened.pane not in self.case.fixture.world.locations, closes)
                self.assertEqual((self.case.run_dir / "progress_history_state.json").read_bytes(), before)
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
            self.assertEqual(self.case.finish("held", turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.IGNORED)
            self.assertIn(opened.pane, world.locations)
        record.update(status="finished", exit_code=0)
        atomic_file.write_text_atomic(path, json.dumps(record))
        self.assertEqual(self.case.finish("held", turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.IGNORED)
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
                    atomic_file.write_text_atomic(path, json.dumps(record))
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
                        self.assertEqual(worker_panes.worker_pass(row, self.case.run_dir)["status"], "completed")
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
                    delivery = progress_history.dispatch_delivery(passed)
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
                        assert isinstance(active, progress_history.ActiveDispatch)
                        self.case.agent.last_replies[str(row.current_session)] = "The review is complete.\nDONE " + active.instance_id
                        self.case.fixture.world.locations[pane]["agent_status"] = "done"
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

        decision = turn_end._prepare_notice("worker", self.case.fixture.parent, self.case.run_dir,  # pyright: ignore[reportPrivateUsage]
            {"worker_id": str(row.id)}, "retarget-proof", turn_end.TurnOutcome.NO_DONE,
            self.case.reply, "question", "")
        current = staff.native_id("019b3a40-4250-7000-8000-000000009101")
        _ = staff.rename(self.case.fixture.parent.id, "renamed-director")
        _ = staff.update_current_session(self.case.fixture.parent.current_session, current)
        with (patch.object(send, "send", return_value=send.Result("sent", "private")) as notice,
              patch.object(progress_history, "record_dispatch_notice")):
            _ = decision.send_notice()
        delivered = cast(send.Options, notice.call_args.args[0])
        self.assertEqual(delivered.to, "session:" + current)

    def test_done_corrects_launcher_error_and_finishes_saved_actions(self) -> None:
        status = self.case.run_dir / "launcher.status"
        report = self.case.run_dir / "launcher-report.md"
        name = staff.WorkerName.parse("impl")
        self.assertIs(worker.dispatch(self.case.fixture.parent.id, name, "impl", self.case.prompt,
            session_dir=self.case.run_dir, slot=name.value, phase="8", kind="impl", launcher_command=["assign.sh"],
            reply=self.case.reply, completion_actions=progress_history.CompletionActions(
                status_file=str(status), report_file=str(report), end_worker=True)), worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        passed = worker_panes.worker_pass(row, self.case.run_dir)
        instance = str(passed["instance_id"])
        error_reply = self.case.run_dir / "launcher-error.txt"
        worker.save_reply(error_reply, "Dispatch ended at the compaction bound.")
        progress_history.complete_dispatch(self.case.run_dir, name.value, instance, status="error")
        progress_history.save_hook_completion(self.case.run_dir, instance,
            progress_history.HookCompletion("error", str(error_reply), ("Dispatch ended at the compaction bound.",)))
        self.assertEqual(self.case.finish(name.value), turn_end.TurnOutcome.DONE)
        self.assertTrue(progress_history.reply_completed(self.case.run_dir, self.case.reply))
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
                if action == "assign":
                    recover = lambda: self.case.dispatch(name)
                else:
                    recover = lambda: worker.restore(parent, only=frozenset({opened.row.id}))
                submitted = len(self.case.agent.submitted)
                notices = len(self.case.agent.notices)
                self.case.agent.notice_failures = 1
                with redirect_stderr(io.StringIO()):
                    self.assertEqual(recover(), worker.ExitCode.UNCERTAIN)
                pending = worker_panes.worker_pass(opened.row, run)
                self.assertEqual(pending["instance_id"], instance)
                completion = progress_history.hook_completion(pending)
                assert isinstance(completion, progress_history.HookCompletion)
                self.assertEqual(completion.reply_file, str(saved))
                self.assertIn(opened.pane, self.case.fixture.world.locations)
                self.assertEqual(len(self.case.agent.submitted), submitted)
                self.assertEqual(len(self.case.agent.notices), notices)

                original_send = send.send

                def send_before_use(options: send.Options) -> send.Result:
                    self.assertIn(opened.pane, self.case.fixture.world.locations)
                    self.assertEqual(len(self.case.agent.submitted), submitted)
                    self.assertEqual(worker_panes.worker_pass(opened.row, run)["instance_id"], instance)
                    return original_send(options)

                with patch.object(send, "send", side_effect=send_before_use):
                    self.assertEqual(recover(), worker.DispatchSubmission.SUBMITTED if action == "assign" else worker.ExitCode.OK)
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                self.assertIn(f"late DONE; reply {saved}; pass {instance}", self.case.agent.notices[-1])
                if action == "assign":
                    self.assertEqual(len(self.case.agent.submitted), submitted + 1)
                    self.assertNotEqual(worker_panes.worker_pass(opened.row, run)["instance_id"], instance)
                else:
                    self.assertNotIn(opened.pane, self.case.fixture.world.locations)

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
            self.case.agent.notice_failures = 1
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
                self.assertEqual(isinstance(saved, progress_history.HookCompletion), stopped_after_close)
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
                self.assertIn(f"late DONE; reply {path}; pass {instance}", self.case.agent.notices[-1])
                finished = progress_history.hook_completion(worker_panes.worker_pass(row, run))
                assert isinstance(finished, progress_history.HookCompletion)
                self.assertTrue(finished.finished)
                self.assertEqual(self.case.fixture.world.closed[closed:], [(pane, "terminal-" + pane)])
                self.assertEqual(len(self.case.fixture.world.launches), launches)

    def test_restart_uses_the_same_delivery_key_after_a_stop_before_the_notice_stamp(self) -> None:
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
            with patch.object(progress_history, "record_dispatch_notice", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    _ = self.case.finish()
            self.assertNotIn("hook_notice", worker_panes.worker_pass(row, run))
            self.assertEqual(shutdown_record.read(), [str(row.id)])
            self.assertTrue(self.case.fixture.world.stop(row, self.case.fixture.world))
            with (patch.object(herdr, "restore_server", return_value=herdr.ServerAdopted()),
                  patch.object(herdr, "read_conversation", return_value=herdr.StoppedConversation()),
                  redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())):
                self.assertEqual(restart.restart(), 0)
        self.assertEqual(keys, [f"worker-completion:{instance}:done"] * 2)
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
                self.assertIsInstance(progress_history.hook_completion(worker_panes.worker_pass(row, run)),
                                      progress_history.NoHookCompletion)
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

                send_notice = send.send

                def send_before_close_or_dispatch(options: send.Options) -> send.Result:
                    self.assertEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)
                    self.assertEqual(len(self.case.agent.submitted), submitted)
                    self.assertIn(pane, self.case.fixture.world.locations)
                    return send_notice(options)

                with patch.object(send, "send", side_effect=send_before_close_or_dispatch):
                    self.assertIs(recover(), worker.DispatchSubmission.SUBMITTED if action == "assign" else worker.ExitCode.OK)
                self.assertEqual(len(self.case.agent.notices), notices + 1)
                self.assertIn(f"late DONE; reply {late}; pass {instance}", self.case.agent.notices[-1])
                self.assertTrue(late.read_text().endswith(f"DONE {instance}\n"))
                if action == "assign":
                    self.assertNotEqual(worker_panes.worker_pass(row, run)["instance_id"], instance)
                else:
                    self.assertNotIn(pane, self.case.fixture.world.locations)

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
        self.assertTrue(passed["hook_notice"])
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
                    reply=reply, completion_actions=progress_history.CompletionActions(end_worker=managed)),
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
        self.assertEqual(self.case.finish(kind=turn_end.EventKind.RUN_FINISHED), turn_end.TurnOutcome.LATE_DONE)
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
                    progress_history.record_dispatch_notice(run, instance, "earlier notice")
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
                self.assertIn(f"{name}: phase 8: late DONE; reply {path}; pass {instance}", notice)
                self.assertIn(f"The earlier pass ended with state {earlier}.", notice)
                self.assertEqual(path.read_text(), reply + "\n")
                self.assertFalse(self.case.reply.exists())
                recorded = worker_panes.worker_pass(opened.row, run)
                self.assertEqual(recorded["status"], earlier)
                self.assertTrue(recorded["hook_notice"])
                self.assertNotEqual(recorded["hook_notice"], "earlier notice")
                self.assertNotIn(opened.pane, self.case.fixture.world.locations)

    def test_a_failed_notice_recovers_the_completed_pass_without_another_close(self) -> None:
        result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse("impl"), "impl", self.case.prompt,
            session_dir=self.case.run_dir, slot="impl", phase="8", kind="impl", launcher_command=["assign.sh"],
            reply=self.case.reply, completion_actions=progress_history.CompletionActions(end_worker=True))
        self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
        pane = self.case.agent.submitted[-1][0]
        row = self.case.agent.row(pane)
        self.case.agent.notice_failures = 1
        with self.assertRaisesRegex(ValueError, "director message failed"):
            _ = self.case.finish()
        passed = worker_panes.worker_pass(row, self.case.run_dir)
        self.assertEqual(passed["status"], "completed")
        self.assertNotIn("hook_notice", passed)
        self.assertEqual(staff.by_id(row.id), row)
        self.assertIn(pane, self.case.fixture.world.locations)
        self.assertEqual(self.case.fixture.world.closed, [])
        close = self.case.fixture.world.close_terminal_pane

        def close_after_delivery(pane: str, terminal: str) -> bool:
            self.assertEqual(len(self.case.agent.notices), 1)
            self.assertTrue(worker_panes.worker_pass(row, self.case.run_dir)["hook_notice"])
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
                actions = (progress_history.CompletionActions(end_worker=True, held_role=str(row.id))
                           if recovery == "same hold" else progress_history.NO_COMPLETION_ACTIONS)
                if recovery == "same hold":
                    pid = self.case.fixture.world.foreground[opened.pane][0]
                    held = astra_roles.hold("reviewer", astra_roles.ExecProcess(pid), str(row.id))
                    self.assertNotIsInstance(held, astra_roles.HoldWorkerUnavailable)
                first_run = self.case.run_dir
                next_run = self.case.fixture.root / ("next-run-" + recovery.replace(" ", "-"))
                next_state = cast(dict[str, object], json.loads((first_run / "progress_history_state.json").read_text()))
                next_state["history_file"] = str(next_run / "history.jsonl")
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
                if recovery == "failed notice":
                    self.assertIs(result, worker.ExitCode.UNCERTAIN)
                    self.assertIn(f"pending completion notice for pass {instance}", errors.getvalue())
                    self.assertIn("director message failed", errors.getvalue())
                    self.assertEqual(worker_panes.worker_run(row), first_run)
                    self.assertEqual(self.case.fixture.world.closed, closed)
                    self.assertIn(pane, self.case.fixture.world.locations)
                    continue
                else:
                    self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
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

    def test_stage_two_recovers_busy_and_interrupted_submission(self) -> None:
        for reason in ("busy", "interrupt_before_submit", "interrupt_stage_two"):
            with self.subTest(reason=reason):
                name = "review-" + reason.replace("_", "-")
                ready = self.case.run_dir / f"{name}.ready"
                result = worker.dispatch(self.case.fixture.parent.id, staff.WorkerName.parse(name), "review", self.case.prompt,
                    session_dir=self.case.run_dir, slot=name, phase="8", kind="review", launcher_command=[name],
                    early_ready=ready, reply=self.case.reply)
                self.assertIs(result, worker.DispatchSubmission.SUBMITTED)
                pane = self.case.agent.submitted[-1][0]
                before = len(self.case.agent.submitted)
                atomic_file.write_text_atomic(ready, "ready\n")
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

                    def interrupt(run: Path, instance: str, delivery: progress_history.DispatchDelivery) -> None:
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
                completion_actions=progress_history.CompletionActions(end_worker=True, held_role=str(current.row.id)))
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
                    log = self.case.fixture.root / "state/herdr/turn-end-ignored.jsonl"
                    previous = len(log.read_text().splitlines()) if log.is_file() else 0
                    self.assertEqual(turn_end.handle(event), turn_end.TurnOutcome.IGNORED)
                    entries = [cast(dict[str, object], json.loads(line)) for line in log.read_text().splitlines()]
                    self.assertEqual(len(entries), previous + 1)
                    entry = entries[-1]
                    self.assertEqual((entry["thread_id"], entry["worker_name"]), (current.row.current_session, name))
                    reason = ("the transcript reply differs from the turn reply" if partial
                              else "the submitted dispatch has no final reply")
                    self.assertEqual(entry["reason"], reason)
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
            completion_actions=progress_history.CompletionActions(status_file=str(status), resolve_fix=True))
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
                progress_history.DispatchDelivery("saved prompt", instance, str(missing)))
            progress_history.complete_dispatch(self.case.run_dir, current.row.name, instance, status="error")
            progress_history.save_hook_completion(self.case.run_dir, instance,
                progress_history.HookCompletion("error", str(missing), ("error reply write failed",)))
            turn_end.run_finished(str(current.row.current_session))
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
                reply=self.case.reply, completion_actions=progress_history.CompletionActions(end_worker=True, held_role=str(row.id)))
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

"""A full-context director stops only for a real wait or a later CLI wake-up."""

from __future__ import annotations

from scripts.lib.test_models_registry import write_registry

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import cast
from unittest import mock
from scripts.production import staff
from scripts.production.staff_test_support import fixture_runtime_id
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.herdr.missing_binary import MISSING_HERDR_BIN
from scripts.lint.memory_test_support import write_config_fixture


HOOK = Path(__file__).with_name("stop-direct-continue.py")
SESSION = fixture_runtime_id("fixture-director")
# A 200k window reserves 33k for compaction and 25k for the handoff.
THRESHOLD = 142_000


@dataclass(frozen=True)
class DirectorStopFixture:
    root: Path
    run: Path
    staff_file: Path
    transcript: Path
    environment: dict[str, str]

    def write_usage(self, tokens: int) -> None:
        _ = self.transcript.write_text(json.dumps({
            "type": "assistant", "message": {
                "model": "claude-sonnet-4", "usage": {"input_tokens": tokens},
            },
        }) + "\n", encoding="utf-8")

    def set_unit_type(self, unit_type: staff.UnitType) -> None:
        document = cast(dict[str, object], json.loads(self.staff_file.read_text()))
        rows = cast(list[dict[str, object]], document["rows"])
        parent_id = staff.StaffId(fixture_runtime_id("fixture-parent"))
        parent = staff.StaffRow(parent_id, "showrunner", "TEST-parent", staff.StaffRoot(),
                                current_session=parent_id)
        rows[0].update(kind="unit", unit_type=unit_type, parent=str(parent.id))
        rows.append(parent.to_json())
        _ = self.staff_file.write_text(json.dumps(document))

    def queue_order(self, key: str) -> Path:
        from scripts.message import send

        path = self.root / "state/message/queue" / send.queue_path(key).name
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(json.dumps({
            "time": "2026-11-16T22:15:00-05:00", "from": "TEST-showrunner",
            "to": f"staff:{SESSION}", "summary": "Merge order",
            "text": "Merge the authorized checkpoint.", "reason": "queued",
            "stable_recipient": f"staff:{SESSION}",
        }) + "\n")
        return path

    def add_merge_worker(self) -> staff.StaffId:
        worker_id = staff.StaffId(fixture_runtime_id("fixture-merge-worker"))
        worker = staff.StaffRow(worker_id, "worker", "TEST-merge-worker", staff.ReportsTo(staff.StaffId(SESSION)),
                                current_session=worker_id, role=staff.KnownWorkerRole("test"))
        document = cast(dict[str, object], json.loads(self.staff_file.read_text()))
        cast(list[dict[str, object]], document["rows"]).append(worker.to_json())
        _ = self.staff_file.write_text(json.dumps(document))
        return worker_id

    @contextmanager
    def live_runner(self, owner: staff.StaffId) -> Generator[None]:
        record = self.run / "detached_runs" / f"{owner}.json"
        record.parent.mkdir(exist_ok=True)
        supervisor = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
                                       "detached_runs.py", "supervise", str(record.resolve())], stdin=subprocess.PIPE)
        _ = record.write_text(json.dumps({"run_id": str(owner), "status": "running", "staff_id": owner,
                                          "worker": "fixture-runner", "pid": supervisor.pid}))
        try:
            yield
        finally:
            _ = supervisor.communicate(timeout=TEST_HANG_GUARD_SECS)

    def call(self, **fields: object) -> subprocess.CompletedProcess[str]:
        payload: dict[str, object] = {
            "session_id": SESSION, "transcript_path": str(self.transcript),
            "background_tasks": [], **fields,
        }
        return subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
            capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS,
            env=self.environment, cwd=self.root,
        )

    def call_with_mocked_reads(self) -> subprocess.CompletedProcess[str]:
        from scripts.message import send

        hook = import_module("scripts.hooks.stop-direct-continue")
        payload: dict[str, object] = {
            "session_id": SESSION, "transcript_path": str(self.transcript), "background_tasks": [],
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with (mock.patch.dict(os.environ, self.environment, clear=True),
              mock.patch.object(send, "STATE", self.root / "state/message"),
              mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            try:
                cast(Callable[[], None], hook.main)()
            except SystemExit as error:
                code = int(str(error.code))
            except Exception:
                # Match the script's outer handler for hook errors.
                pass
        return subprocess.CompletedProcess([str(HOOK)], code, stdout.getvalue(), stderr.getvalue())


@contextmanager
def director_stop_fixture() -> Generator[DirectorStopFixture]:
    with tempfile.TemporaryDirectory(prefix="stop-hook-") as temporary:
        root = Path(temporary)
        run = root / "unit" / "run"
        run.mkdir(parents=True)
        history = root / "history" / "run.jsonl"
        history.parent.mkdir()
        _ = history.write_text("", encoding="utf-8")
        _ = (run / "progress_history_state.json").write_text(json.dumps({
            "run_id": "fixture-run", "history_file": str(history),
            "owner_staff_id": SESSION,
            "working_dir": str(root),
            "main_agent": {"session_id": SESSION},
            "open_phases": [{"id": "1", "instance_id": "fixture-phase", "status": "active"}],
            "pass": {},
        }), encoding="utf-8")
        config = root / "models.conf"
        _ = write_registry(config, {'claude.context': {'default': '200k'}}, replace_sections=('claude.context',))
        _ = write_config_fixture(root / "context_usage.conf", "transcript.tail_bytes=524288\n")
        _ = write_config_fixture(root / "stall_watch.conf")
        from scripts.lib.atomic_file import write_text_atomic

        row = staff.StaffRow(staff.StaffId(SESSION), "showrunner", "director", staff.StaffRoot(),
                             current_session=staff.StaffId(SESSION), run=staff.OpenRun(run))
        write_text_atomic(root / "staff.json", json.dumps({"schema": 2, "rows": [row.to_json()]}))
        environment = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(root),
            "PYTHONDONTWRITEBYTECODE": "1", "HERDR_BIN": MISSING_HERDR_BIN,
            "XDG_STATE_HOME": str(root / "state"), 
            "UNIT_HISTORY_DIR": str(history.parent), "UNIT_SYNC_WRITES": "0",
            "SCRATCH_ROOT": str(root / "scratch"),
            "CLAUDE_PROBE_SESSION": "0",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200k", "STAFF_FILE": str(root / "staff.json"), "CONF_DIR": str(root),
        }
        fixture = DirectorStopFixture(root, run, root / "staff.json", root / "transcript.jsonl", environment)
        fixture.write_usage(160_000)
        yield fixture


class DirectorStopTests(unittest.TestCase):
    def test_unreadable_queue_does_not_hide_a_readable_pending_order(self) -> None:
        from scripts.message import send

        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            queued = fixture.queue_order(f"staff:{SESSION}")
            messages = send.read_queue(queued)
            unreadable = queued.with_name("unreadable.jsonl")
            with mock.patch.object(send, "read_queue", side_effect=[
                    PermissionError(13, "Permission denied", str(unreadable)), messages]):
                result = fixture.call_with_mocked_reads()
            self.assertEqual(result.returncode, 2)
            self.assertIn("/unit:merge run active", result.stderr)

    def test_unreadable_queue_without_work_allows_stop_with_one_anomaly(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            unreadable = fixture.root / "state/message/queue/unreadable.jsonl"
            unreadable.parent.mkdir(parents=True)
            _ = unreadable.write_bytes(b"\xff\n")
            result = fixture.call()
            self.assertEqual((result.returncode, result.stdout), (0, ""))
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertIn("anomaly:", result.stderr)
            self.assertIn(str(unreadable), result.stderr)
            self.assertFalse((fixture.run / "compaction_wake_line").exists())

    def test_unknown_dispatch_without_work_allows_stop_and_reports_its_reason(self) -> None:
        from scripts.direct import progress_history

        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            _ = fixture.add_merge_worker()
            reason = f"{fixture.run}/progress_history_state.json: Permission denied"
            with mock.patch.object(progress_history, "active_dispatch",
                                   return_value=progress_history.ActiveDispatchUnavailable(reason)):
                result = fixture.call_with_mocked_reads()
            self.assertEqual((result.returncode, result.stdout), (0, ""))
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertIn("anomaly:", result.stderr)
            self.assertIn(reason, result.stderr)
            self.assertFalse((fixture.run / "compaction_wake_line").exists())

    def test_unknown_dispatch_does_not_hide_a_pending_order(self) -> None:
        from scripts.direct import progress_history

        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            _ = fixture.add_merge_worker()
            _ = fixture.queue_order(f"staff:{SESSION}")
            with mock.patch.object(progress_history, "active_dispatch",
                                   return_value=progress_history.ActiveDispatchUnavailable("Cannot read the dispatch.")):
                result = fixture.call_with_mocked_reads()
            self.assertEqual(result.returncode, 2)
            self.assertIn("/unit:merge run active", result.stderr)

    def test_stop_names_the_recorded_unit_command(self) -> None:
        for unit_type in staff.UNIT_TYPES:
            with self.subTest(unit_type=unit_type), director_stop_fixture() as fixture:
                fixture.set_unit_type(unit_type)
                if unit_type == "merge":
                    _ = fixture.queue_order(f"staff:{SESSION}")
                result = fixture.call()
                self.assertEqual(result.returncode, 2)
                self.assertIn(f"/unit:{unit_type} run active", result.stderr)
                self.assertIn(f"commands/unit/{unit_type}.md", result.stderr)

    def test_waiting_merge_unit_with_an_open_phase_stops_without_a_wake(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            worker_id = fixture.add_merge_worker()
            state_path = fixture.run / "progress_history_state.json"
            state = cast(dict[str, object], json.loads(state_path.read_text()))
            state["pass"] = {"test": {"status": "canceled", "worker_id": worker_id}}
            _ = state_path.write_text(json.dumps(state))
            stale = fixture.run / "phases/old-order/test_status"
            stale.parent.mkdir(parents=True)
            _ = stale.write_text("running\n")
            self.assert_allowed(fixture.call(last_assistant_message="The merge unit waits for an order."))
            self.assertFalse((fixture.run / "compaction_wake_line").exists())

    def test_merge_unit_with_its_own_runner_stops_without_a_wake(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            state_path = fixture.run / "progress_history_state.json"
            state = cast(dict[str, object], json.loads(state_path.read_text()))
            state["open_phases"] = []
            _ = state_path.write_text(json.dumps(state))
            with fixture.live_runner(staff.StaffId(SESSION)):
                for status in ("The runner is live.", "The runner has no result yet."):
                    with self.subTest(status=status):
                        self.assert_allowed(fixture.call(last_assistant_message=status))
                        self.assertFalse((fixture.run / "compaction_wake_line").exists())
            self.assert_allowed(fixture.call(last_assistant_message="The merge runner stopped."))

    def test_merge_unit_with_a_worker_runner_still_gets_a_wake(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            worker_id = fixture.add_merge_worker()
            with fixture.live_runner(worker_id):
                result = fixture.call()
            self.assertEqual(result.returncode, 2)
            self.assertIn("/unit:merge run active", result.stderr)

    def test_own_runner_does_not_hide_other_merge_work(self) -> None:
        for work in ("worker_runner", "worker_dispatch", "background_task", "queued_message"):
            with self.subTest(work=work), director_stop_fixture() as fixture:
                fixture.set_unit_type("merge")
                with fixture.live_runner(staff.StaffId(SESSION)):
                    fields: dict[str, object] = {}
                    if work == "queued_message":
                        _ = fixture.queue_order(f"staff:{SESSION}")
                    elif work == "background_task":
                        fields["background_tasks"] = [{"id": "fixture", "status": "running"}]
                    elif work == "worker_dispatch":
                        worker_id = fixture.add_merge_worker()
                        path = fixture.run / "progress_history_state.json"
                        state = cast(dict[str, object], json.loads(path.read_text()))
                        state["pass"] = {"test": {"status": "active", "worker_id": worker_id,
                                                 "instance_id": "fixture-dispatch", "launcher_command": ["assign.sh"]}}
                        _ = path.write_text(json.dumps(state))
                    else:
                        worker_id = fixture.add_merge_worker()
                        with fixture.live_runner(worker_id):
                            result = fixture.call()
                        self.assertEqual(result.returncode, 2)
                        self.assertIn("/unit:merge run active", result.stderr)
                        continue
                    result = fixture.call(**fields)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("/unit:merge run active", result.stderr)

    def test_damaged_record_does_not_hide_a_live_merge_runner(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            worker_id = fixture.add_merge_worker()
            runs = fixture.run / "detached_runs"
            runs.mkdir()
            damaged = runs / "000-damaged.json"
            _ = damaged.write_text("{", encoding="utf-8")
            with fixture.live_runner(worker_id):
                result = fixture.call()
            self.assertEqual(result.returncode, 2)
            self.assertIn("/unit:merge run active", result.stderr)
            self.assertEqual(sum(line.startswith("anomaly:") for line in result.stderr.splitlines()), 1)
            self.assertIn(str(damaged), result.stderr)

    def test_own_runner_with_a_damaged_record_allows_stop_with_one_anomaly(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            with fixture.live_runner(staff.StaffId(SESSION)):
                damaged = fixture.run / "detached_runs/000-damaged.json"
                _ = damaged.write_text("{", encoding="utf-8")
                result = fixture.call()
            self.assertEqual((result.returncode, result.stdout), (0, ""))
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertIn("anomaly:", result.stderr)
            self.assertIn(str(damaged), result.stderr)
            self.assertFalse((fixture.run / "compaction_wake_line").exists())

    def test_damaged_record_without_work_reports_one_anomaly_and_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            damaged = fixture.run / "detached_runs/000-damaged.json"
            damaged.parent.mkdir()
            _ = damaged.write_text("{", encoding="utf-8")
            result = fixture.call()
            self.assertEqual((result.returncode, result.stdout), (0, ""))
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertIn("anomaly:", result.stderr)
            self.assertIn(str(damaged), result.stderr)
            self.assertFalse((fixture.run / "compaction_wake_line").exists())

    def test_unreadable_run_folder_reports_one_anomaly_and_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            runs = fixture.run / "detached_runs"
            runs.mkdir()
            scandir = os.scandir

            def read_folder(path: str | os.PathLike[str]) -> object:
                if Path(path) == runs:
                    raise PermissionError(13, "Permission denied", str(runs))
                return scandir(path)

            with mock.patch("os.scandir", side_effect=read_folder):
                result = fixture.call_with_mocked_reads()
            self.assertEqual((result.returncode, result.stdout), (0, ""))
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertIn("anomaly:", result.stderr)
            self.assertIn(str(runs), result.stderr)
            self.assertFalse((fixture.run / "compaction_wake_line").exists())

    def test_merge_unit_with_an_active_dispatch_still_gets_a_wake(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            worker_id = fixture.add_merge_worker()
            state_path = fixture.run / "progress_history_state.json"
            state = cast(dict[str, object], json.loads(state_path.read_text()))
            state["open_phases"] = []
            state["pass"] = {"test": {"status": "active", "worker_id": worker_id,
                                       "instance_id": "fixture-dispatch", "launcher_command": ["assign.sh"]}}
            _ = state_path.write_text(json.dumps(state))
            result = fixture.call()
            self.assertEqual(result.returncode, 2)
            self.assertIn("/unit:merge run active", result.stderr)

    def test_merge_unit_with_a_background_runner_still_gets_a_wake(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            result = fixture.call(background_tasks=[{"id": "land", "status": "running"}])
            self.assertEqual(result.returncode, 2)
            self.assertIn("/unit:merge run active", result.stderr)

    def test_merge_unit_with_a_pending_message_still_gets_a_wake(self) -> None:
        for key in (f"staff:{SESSION}", f"session-{SESSION}", "legacy-recipient"):
            with self.subTest(key=key), director_stop_fixture() as fixture:
                fixture.set_unit_type("merge")
                state_path = fixture.run / "progress_history_state.json"
                state = cast(dict[str, object], json.loads(state_path.read_text()))
                state["open_phases"] = []
                _ = state_path.write_text(json.dumps(state))
                queued = fixture.queue_order(key)
                before = queued.read_bytes()
                result = fixture.call()
                self.assertEqual(result.returncode, 2)
                self.assertIn("/unit:merge run active", result.stderr)
                self.assertEqual(queued.read_bytes(), before)

    def test_another_units_queued_message_does_not_wake_a_waiting_merge_unit(self) -> None:
        for recipient in (f"staff:{fixture_runtime_id('another-unit')}", ""):
            with self.subTest(recipient=recipient), director_stop_fixture() as fixture:
                fixture.set_unit_type("merge")
                queued = fixture.queue_order("another-unit")
                message = cast(dict[str, object], json.loads(queued.read_text()))
                if recipient:
                    message["stable_recipient"] = recipient
                else:
                    _ = message.pop("stable_recipient")
                _ = queued.write_text(json.dumps(message) + "\n")
                before = queued.read_bytes()
                self.assert_allowed(fixture.call())
                self.assertEqual(queued.read_bytes(), before)

    def test_merge_gate_with_no_open_order_phase_stops_without_a_wake(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.set_unit_type("merge")
            state_path = fixture.run / "progress_history_state.json"
            state = cast(dict[str, object], json.loads(state_path.read_text()))
            state["open_phases"] = []
            state["phase"] = {"id": "1", "instance_id": "fixture-phase", "status": "completed"}
            _ = state_path.write_text(json.dumps(state))
            self.assert_allowed(fixture.call(last_assistant_message="— holding: queue empty"))
            self.assertFalse((fixture.run / "compaction_wake_line").exists())

    def test_live_detached_run_of_the_director_stops_without_a_wake(self) -> None:
        with director_stop_fixture() as fixture:
            record = fixture.run / "detached_runs" / "land.json"
            record.parent.mkdir()
            supervisor = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
                                           "detached_runs.py", "supervise", str(record.resolve())], stdin=subprocess.PIPE)
            _ = record.write_text(json.dumps({"run_id": "land", "status": "running", "staff_id": SESSION,
                                              "worker": "fixture-director", "pid": supervisor.pid}))
            try:
                self.assert_allowed(fixture.call(last_assistant_message="— holding: waiting on the land run"))
            finally:
                _ = supervisor.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assert_blocked(fixture.call(last_assistant_message="— holding: waiting on the land run"))

    def assert_blocked(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual((result.returncode, result.stdout), (2, ""))
        self.assertIn(
            "Compaction runs when the context reaches 167,000 tokens. It does not run while the session is idle.\n"
            + "Take the next workflow action now. Do not wait for compaction.\n"
            + "After compaction, read ~/.claude/commands/unit/direct.md in full.\n"
            + "Then read each stub file whose moment is live.\n"
            + "If you need a user decision, state it in one line and end the turn.\n", result.stderr)

    def assert_allowed(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_finished_worker_heartbeat_does_not_allow_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            _ = (fixture.run / "heartbeat.log").write_text(
                "[wrapper] fix:impl agent finished\n", encoding="utf-8")
            self.assert_blocked(fixture.call())

    def test_running_background_task_allows_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(background_tasks=[{"id": "launcher", "status": "running"}]))

    def test_finished_background_tasks_do_not_allow_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            for status in ("completed", "failed", "cancelled"):
                with self.subTest(status=status):
                    self.assert_blocked(fixture.call(
                        background_tasks=[{"status": status}], last_assistant_message=status))

    def test_unreported_background_tasks_do_not_allow_a_full_context_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call(background_tasks=None))

    def test_handoff_threshold_blocks_without_a_wake_source(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.write_usage(THRESHOLD)
            self.assert_blocked(fixture.call())

    def test_context_below_handoff_threshold_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.write_usage(THRESHOLD - 1)
            self.assert_allowed(fixture.call())

    def test_unmeasurable_transcript_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.transcript.unlink()
            self.assert_allowed(fixture.call())

    def test_unconfigured_context_window_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            del fixture.environment["CLAUDE_CODE_AUTO_COMPACT_WINDOW"]
            self.assert_allowed(fixture.call())

    def test_subagent_allows_stop_even_with_full_context(self) -> None:
        with director_stop_fixture() as fixture:
            own = fixture.transcript.with_suffix("") / "subagents" / "agent-worker.jsonl"
            own.parent.mkdir(parents=True)
            _ = own.write_bytes(fixture.transcript.read_bytes())
            self.assert_allowed(fixture.call(agent_id="worker"))

    def test_missing_session_id_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(session_id=""))

    def test_no_active_run_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            _ = fixture.staff_file.write_text(json.dumps({"schema": 2, "rows": []}))
            self.assert_allowed(fixture.call())

    def test_unreadable_staff_allows_stop_without_output(self) -> None:
        with director_stop_fixture() as fixture:
            fixture.environment["STAFF_FILE"] = str(fixture.root)
            self.assert_allowed(fixture.call())

    def test_declared_user_wait_is_recorded_and_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(last_assistant_message="decision: approve the checkpoint"))
            self.assertTrue((fixture.run / "turn_wait.json").is_file())
            self.assertIn('"WaitingForUser"', (fixture.root / "history/run.jsonl").read_text())

    def test_declared_supplier_wait_is_recorded_and_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(last_assistant_message="blocked: waiting on session:supplier"))
            self.assertTrue((fixture.run / "turn_wait.json").is_file())
            self.assertIn('"WaitingForSupplier"', (fixture.root / "history/run.jsonl").read_text())

    def test_same_final_line_above_the_threshold_does_not_wake_again(self) -> None:
        with director_stop_fixture() as fixture:
            status = "holding: waiting on the showrunner's restart order"
            self.assert_blocked(fixture.call(last_assistant_message=f"First turn.\n— {status}"))
            self.assert_allowed(fixture.call(last_assistant_message=f"Second turn.\n  — {status}  \n"))
            self.assertEqual((fixture.run / "compaction_wake_line").read_text(encoding="utf-8"), status)

    def test_continuation_with_a_different_final_line_above_the_threshold_wakes_again(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call(last_assistant_message="The first action is complete."))
            status = "The next action is complete."
            self.assert_blocked(fixture.call(stop_hook_active=True, last_assistant_message=status))
            self.assertEqual((fixture.run / "compaction_wake_line").read_text(encoding="utf-8"), status)
            self.assert_allowed(fixture.call(stop_hook_active=True, last_assistant_message=status))

    def test_holding_status_without_pause_above_the_threshold_wakes(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call(last_assistant_message="Work is ready.\n  — holding: the next workflow action  \n"))

    def test_blocked_status_without_pause_above_the_threshold_wakes(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call(last_assistant_message="blocked: the next test needs a fixture"))
            self.assertFalse((fixture.run / "turn_wait.json").exists())

    def test_blocked_pause_order_above_the_threshold_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(last_assistant_message=(
                "blocked: pause order from the user through the showrunner; only the showrunner restarts this work"
            )))
            # A pause opens a wait, so the paused time leaves active phase time.
            self.assertTrue((fixture.run / "turn_wait.json").exists())

    def test_paused_status_without_a_prefix_above_the_threshold_allows_stop(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_allowed(fixture.call(last_assistant_message="The production is PAUSED by the user."))

    def test_blocked_within_a_sentence_above_the_threshold_wakes(self) -> None:
        with director_stop_fixture() as fixture:
            self.assert_blocked(fixture.call(last_assistant_message="The first test was blocked by a missing fixture."))


if __name__ == "__main__":
    _ = unittest.main()

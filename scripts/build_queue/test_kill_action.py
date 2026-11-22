"""Use private process trees and message delivery for Builds stop actions."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, tzinfo
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
from typing import cast, override
import unittest
from unittest.mock import ANY, patch
from zoneinfo import ZoneInfo

from scripts.agents import detached_run_process, detached_runs
from scripts.build_queue import admission_client, admission_records as records, admission_wire, kill_action
from scripts.build_queue.admission_jobs import VerifyCall
from scripts.build_queue.test_memory_feed import reservation_reading
from scripts.buildlog import store
from scripts.lib import process_stop
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import send_budget
from scripts.production import staff


class KillActionTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = write_config_fixture(self.root / "review_links.conf")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.root)})))

    def detached_target(self, *, name: str = "impl3", role: str = "worker",
                        step: str = "check") -> kill_action.KillTarget:
        record = self.root / "detached_runs" / "fixture.json"
        record.parent.mkdir()
        scope: process_stop.ScopeIdentity = {"unit": "detached-fixture.scope", "device": 1, "inode": 2,
            "path": "/user.slice/builds-detached.slice/detached-fixture.scope"}
        route = kill_action.DetachedCommand(record, 17, "123", 16, "122", scope)
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", step, "at")
        target = kill_action.KillTarget(run, route, staff.StaffId("fixture"), f"{name}, {role}", name, role)
        self.write_run(target)
        return target

    def write_run(self, target: kill_action.KillTarget, **fields: object) -> None:
        route = target.route
        _ = route.record.write_text(json.dumps({
            "run_id": route.record.stem, "status": "running", "staff_id": target.recipient,
            "command_scope": route.scope, "session_dir": str(self.root), "worker": target.recipient_name,
            "label": "fixture", "command": ["cargo", "check"], "log": str(route.record.with_suffix(".log")),
            "started_at": 10, "pid": route.supervisor, "director_dir": "", "done_message_file": "", **fields,
        }))

    def saved_action(self, target: kill_action.KillTarget) -> kill_action.SavedKillAction:
        path = self.root / ".review-link-build-kill" / f"{target.candidate.reference}.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        return admission_wire.parse_record(path.read_text(), kill_action.SavedKillAction)

    def resume(self) -> kill_action.KillResult:
        resumed = kill_action.KillActions(self.root)
        workers: list[threading.Thread] = []
        start = threading.Thread.start

        def record_thread(thread: threading.Thread) -> None:
            workers.append(thread)
            start(thread)

        with (patch.object(threading.Thread, "start", new=record_thread),
              patch.object(kill_action, "live_targets", return_value={})):
            resumed.resume()
            (worker,) = workers
            worker.join(TEST_HANG_GUARD_SECS)
            self.assertFalse(worker.is_alive())
            recovered = kill_action.KillActions(self.root)
            recovered.resume()
            self.assertEqual(workers, [worker])
            offers, (result,) = recovered.options()
            self.assertEqual(offers, {})
        return result

    def test_only_local_running_cargo_is_a_candidate(self) -> None:
        reading = reservation_reading()
        (step,) = reading.view.running
        (saved,) = reading.explanation.decided_from.reservations
        reservation: records.Reservation = {**saved, "pid": 17, "start_time": "123"}
        step = replace(step, pid=17, session_id="session")
        verified = replace(step, step="verify step", verify=VerifyCall("check", "fixture", 1, 1))
        self.assertIsInstance(kill_action.candidate(step, reservation, "natedev"), kill_action.KillCandidate)
        cases = ((verified, "natedev", False), (step, "mac", False), (step, "aws", False), (step, "natedev", True),
                 (replace(step, ci_runner="runner"), "natedev", False),
                 (replace(step, session_id=""), "natedev", False),
                 (replace(step, assignment_host="mac"), "natedev", False),
                 (replace(step, mac_placement="mac-attempt"), "natedev", False),
                 (replace(step, pid=18), "natedev", False),
                 (replace(step, step="test-affected"), "natedev", False),
                 (replace(step, step="merge-gate"), "natedev", False),
                 (replace(step, measured=records.UnownedSliceMemory(1, pid=17)), "natedev", False),
                 (replace(step, measured=records.CompilerServiceMemory(1, pid=17)), "natedev", False))
        for current, machine, ci in cases:
            with self.subTest(current=current, machine=machine, ci=ci):
                self.assertIsInstance(kill_action.candidate(current, reservation, machine, ci=ci),
                                      kill_action.NoKillCandidate)

    def test_detached_stop_uses_the_public_api_with_the_saved_route_check(self) -> None:
        target = self.detached_target()
        route = target.route
        reason = "Stopped from the Builds page."
        moved = replace(route, scope={**route.scope, "inode": route.scope["inode"] + 1})

        def stop_detached(_record: Path, _reason: str, *, check: Callable[[], bool]) -> bool:
            return check()

        with (patch.object(process_stop, "stop_scope") as stop_scope,
              patch.object(detached_runs, "stop_command", side_effect=stop_detached) as stop,
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route") as read_route):
            read_route.side_effect = [route, route, route, moved]
            self.assertTrue(kill_action.stop_verified_scope(target, reason))
            self.assertFalse(kill_action.stop_verified_scope(target, reason))
        self.assertEqual([call.args for call in stop.call_args_list], [(route.record, reason)] * 2)
        self.assertEqual([set(call.kwargs) for call in stop.call_args_list], [{"check"}, {"check"}])
        stop_scope.assert_not_called()

    def test_standalone_cargo_has_no_stop_route(self) -> None:
        target = self.detached_target()
        proc = self.root / "proc"
        owner = proc / "17"
        owner.mkdir(parents=True)
        _ = (owner / "environ").write_bytes(b"")
        table = {17: memory_admit.ProcessIdentity(1, "123")}
        with (patch.object(kill_action, "PROC", proc),
              patch.object(kill_action, "program", return_value="cargo"),
              patch.object(kill_action, "live", return_value=True)):
            self.assertIsInstance(kill_action.stop_route(target.candidate, target.recipient, table),
                                  kill_action.NoStopRoute)

    def test_routes_are_unavailable_without_pidfd_support(self) -> None:
        target = self.detached_target()
        with (patch.object(process_stop, "supported", return_value=False),
              patch.object(kill_action, "detached_command") as detached):
            self.assertIsInstance(kill_action.stop_route(target.candidate, target.recipient, {}), kill_action.NoStopRoute)
        detached.assert_not_called()

    def test_detached_route_uses_the_scope_saved_by_the_supervisor(self) -> None:
        target = self.detached_target()
        route = target.route
        self.write_run(target, command_pid=route.group, command_group=route.group, command_started=route.started,
                       pid=route.supervisor, supervisor_started=route.supervisor_start, command=["cargo", "check"])
        proc = self.root / "proc"
        owner = proc / str(route.group)
        owner.mkdir(parents=True)
        _ = (owner / "environ").write_text(f"UNIT_DETACHED_RECORD={route.record}\0")
        table = {route.group: memory_admit.ProcessIdentity(route.supervisor, route.started)}
        with (patch.object(kill_action, "PROC", proc),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "program", return_value="cargo"),
              patch.object(kill_action, "in_group", return_value=True),
              patch.object(detached_run_process, "supervisor_match", return_value=True),
              patch.object(process_stop, "pin_process", return_value=nullcontext(5)),
              patch.object(process_stop, "command_scope", return_value=route.scope)):
            self.assertEqual(kill_action.stop_route(target.candidate, target.recipient, table), route)

    def test_legacy_verify_rows_do_not_offer_a_step_scope(self) -> None:
        run = kill_action.KillCandidate(17, "123", 16, "122", "session", "check", "at")
        table = {17: memory_admit.ProcessIdentity(16, "123"), 16: memory_admit.ProcessIdentity(1, "122")}
        with (patch.object(kill_action, "program", side_effect=("cargo", "verify.sh")),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "detached_command") as detached):
            self.assertIsInstance(kill_action.stop_route(run, staff.StaffId("fixture"), table), kill_action.NoStopRoute)
        detached.assert_not_called()

    def test_the_service_uses_the_recorded_session_for_each_staff_kind(self) -> None:
        reading = reservation_reading()
        route = self.detached_target().route
        kinds: tuple[staff.Kind, ...] = ("worker", "unit", "showrunner", "producer")
        for kind in kinds:
            with self.subTest(kind=kind):
                caller = staff.StaffRow(staff.StaffId("stable-id"), kind, "current name", staff.StaffRoot(),
                                        current_session=staff.StaffId("session"))
                decoy = replace(caller, id=staff.StaffId("decoy"), name="fixture",
                                current_session=staff.StaffId("other session"))
                snapshot = staff.StaffSnapshot((decoy, caller))
                with (patch.object(store, "host_name", return_value="natedev"),
                      patch.object(admission_client, "view", return_value=reading),
                      patch.object(staff, "snapshot", return_value=snapshot),
                      patch.object(memory_admit, "process_table", return_value={}),
                      patch.object(kill_action, "stop_route", return_value=route)):
                    (target,) = kill_action.live_targets().values()
                self.assertEqual(target.recipient, caller.id)
                self.assertEqual(target.starter, f"current name, a {'unit director' if kind == 'unit' else kind}")

    def test_handle_rejects_pid_reuse_and_sends_once_after_the_run_ends(self) -> None:
        target = self.detached_target(step="nextest")
        run = target.candidate
        actions = kill_action.KillActions(self.root)
        at = datetime(2026, 11, 18, 19, 41, 0, 900_000, tzinfo=ZoneInfo("America/New_York"))
        reason = 'nate killed your run "nextest" (pid 17) from the Builds page at 19:41 EST'

        class FixtureClock(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return at.astimezone(tz)

        with (patch.object(kill_action, "live_targets", return_value={run.reference: target}) as targets,
              patch.object(kill_action, "datetime", FixtureClock),
              patch.object(kill_action, "user_zone", return_value=at.tzinfo),
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as deliver):
            offers, _ = actions.options()
            self.assertEqual(offers[run.reference]["message"], reason)
            handle = offers[run.reference]["handle"]
            reused = replace(run, holder_start="456", row_start="456")
            targets.return_value = {reused.reference: replace(target, candidate=reused)}
            self.assertIsInstance(actions.stop(run.reference, handle, ""), kill_action.NoStopRoute)
            targets.return_value = {run.reference: target}

            def complete(current: kill_action.KillTarget, received: str) -> bool:
                self.assertEqual((current, received), (target, reason))
                deliver.assert_not_called()
                self.assertEqual(actions.options()[1][0]["stop"], kill_action.StopState.STOPPING)
                saved = self.saved_action(target)
                self.assertEqual(saved.requested_at, int(at.timestamp()))
                self.write_run(target, status="finished", finished_at=at.isoformat(timespec="seconds"))
                targets.return_value = {}
                return False

            with patch.object(kill_action, "stop_verified_scope", side_effect=complete) as stop:
                result = actions.stop(run.reference, handle, "Use the next slot.")
            stop.assert_called_once_with(target, reason)
            deliver.assert_called_once()
            (saved,) = cast(tuple[kill_action.SavedKillAction], deliver.call_args.args)
            self.assertEqual((saved.recipient, saved.message), (target.recipient, reason + "\nUse the next slot."))
            self.assertEqual(result, {"reference": run.reference, "step": "nextest", "pid": 17,
                                      "stop": kill_action.StopState.STOPPED, "delivery": kill_action.DeliveryState.SENT,
                                      "time": "19:41 EST", "recipient_name": "impl3", "recipient_role": "worker"})
            self.assertEqual(actions.options(), ({}, [result]))
            self.assertIsInstance(actions.stop(run.reference, handle, "Again."), kill_action.NoStopRoute)

    def test_run_ended_before_the_request_has_no_notice(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        with (patch.object(kill_action, "live_targets", return_value={reference: target}) as targets,
              patch.object(detached_runs, "stop_command") as stop,
              patch.object(kill_action, "deliver") as send):
            offers, _ = actions.options()
            targets.return_value = {}
            handle = offers[reference]["handle"]
            self.assertIsInstance(actions.stop(reference, handle, ""), kill_action.NoStopRoute)
            self.write_run(target, status="finished", finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))
            outcome = actions.stop(reference, handle, "")
            self.assertNotIsInstance(outcome, kill_action.NoStopRoute)
            self.assertEqual(actions.options(), ({}, [outcome]))
        saved = self.saved_action(target)
        self.assertEqual((saved.result["stop"], saved.result["delivery"]),
                         (kill_action.StopState.ALREADY_ENDED, kill_action.DeliveryState.PENDING))
        self.assertIsInstance(saved.stage, kill_action.FinishedStop)
        stop.assert_not_called()
        send.assert_not_called()

    def test_restart_uses_the_saved_request_time_to_classify_the_finished_run(self) -> None:
        for offset in (-1, 1):
            with self.subTest(offset=offset), tempfile.TemporaryDirectory(dir=self.root) as folder:
                with patch.object(self, "root", Path(folder)):
                    self.check_finished_run_after_restart(offset)

    def check_finished_run_after_restart(self, offset: int) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        class Restart(BaseException):
            pass

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "stop_verified_scope", side_effect=Restart), self.assertRaises(Restart)):
            _ = actions.stop(reference, actions.handle(target), "Wait for release.")
        saved = self.saved_action(target)
        self.assertIsInstance(saved.stage, kill_action.StopRequested)
        self.assertEqual(saved.route, target.route)
        self.assertTrue(saved.message.endswith("\nWait for release."))
        finished_at = datetime.fromtimestamp(saved.requested_at + offset, tz=ZoneInfo("UTC"))
        self.write_run(target, status="finished", finished_at=finished_at.isoformat(timespec="seconds"))
        with (patch.object(detached_runs, "stop_command") as stop,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            result = self.resume()
        stop.assert_not_called()
        if offset < 0:
            send.assert_not_called()
            self.assertEqual((result["stop"], result["delivery"]),
                             (kill_action.StopState.ALREADY_ENDED, kill_action.DeliveryState.PENDING))
        else:
            send.assert_called_once_with(replace(saved, stage=kill_action.SendingNotice()))
            self.assertEqual((result["stop"], result["delivery"]),
                             (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))

    def test_restart_requests_stop_again_for_the_saved_running_record(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "stop_verified_scope", return_value=False) as first_stop,
              patch.object(actions, "_start_watch") as watch,
              patch.object(kill_action, "deliver") as send):
            outcome = actions.stop(reference, actions.handle(target), "Use the next slot.")
        self.assertNotIsInstance(outcome, kill_action.NoStopRoute)
        saved = self.saved_action(target)
        self.assertEqual(saved.result["stop"], kill_action.StopState.UNCONFIRMED)
        first_stop.assert_called_once_with(target, saved.message.partition("\n")[0])
        watch.assert_called_once()
        send.assert_not_called()

        def stop(path: Path, reason: str, *, check: Callable[[], bool]) -> bool:
            self.assertEqual(path, target.route.record)
            self.assertEqual(reason, saved.message.partition("\n")[0])
            self.assertTrue(check())
            send.assert_not_called()
            finished_at = datetime.fromtimestamp(saved.requested_at + 1, tz=ZoneInfo("UTC"))
            self.write_run(target, status="finished", finished_at=finished_at.isoformat(timespec="seconds"))
            return True

        with (patch.object(detached_runs, "stop_command", side_effect=stop) as requested,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.QUEUED) as send):
            result = self.resume()
        requested.assert_called_once_with(target.route.record, saved.message.partition("\n")[0], check=ANY)
        send.assert_called_once_with(replace(saved, stage=kill_action.SendingNotice()))
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.QUEUED))

    def test_scope_exit_waits_for_the_finished_record_and_reports_retirement_once(self) -> None:
        target = self.detached_target(name="retired", role="producer")
        cgroups = self.root / "cgroups"
        folder = cgroups / target.route.scope["path"].lstrip("/")
        folder.mkdir(parents=True)
        identity = folder.stat()
        scope: process_stop.ScopeIdentity = {**target.route.scope, "device": identity.st_dev, "inode": identity.st_ino}
        target = replace(target, route=replace(target.route, scope=scope))
        self.write_run(target)
        freeze = folder / "cgroup.freeze"
        _ = freeze.write_text("0\n")
        events = folder / "cgroup.events"
        _ = events.write_text("populated 1\nfrozen 1\n")
        _ = write_config_fixture(self.root / "shutdown.conf", "STOP_GRACE_SECONDS=2\nKILL_GRACE_SECONDS=5")
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        def term(_directory: int, stopped: process_stop.ScopeIdentity) -> int:
            self.assertEqual(stopped, scope)
            self.assertEqual(freeze.read_text(), "1\n")
            record = cast(dict[str, object], json.loads(target.route.record.read_text()))
            self.assertEqual(record.get("stop_reason"), self.saved_action(target).message)
            self.assertIs(record.get("stop_pass_requested"), True)
            _ = events.write_text("populated 0\nfrozen 1\n")
            return 1

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(process_stop, "_term_scope", side_effect=term) as send_term,
              patch.object(detached_runs, "_stop_caller_is_owner", return_value=False),
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", return_value=target.route),
              patch.object(actions, "_start_watch"), patch.object(kill_action, "deliver") as send):
            _ = actions.stop(reference, actions.handle(target), "")
        send_term.assert_called_once_with(ANY, scope)
        self.assertEqual(freeze.read_text(), "0\n")
        record = cast(dict[str, object], json.loads(target.route.record.read_text()))
        self.assertIs(record.get("scope_end_proved"), True)
        send.assert_not_called()
        saved = self.saved_action(target)
        self.assertEqual(saved.result["stop"], kill_action.StopState.STOPPING)
        ended = datetime.fromtimestamp(saved.requested_at + 1, tz=ZoneInfo("UTC"))
        self.write_run(target, status="finished", finished_at=ended.isoformat(timespec="seconds"))
        response = subprocess.CompletedProcess(["send.py"], 2, "", "send.py: fixture: retired producer")
        with (patch.object(detached_runs, "stop_command") as retry,
              patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", return_value=response) as send):
            result = self.resume()
        retry.assert_not_called()
        send.assert_called_once()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.FAILED))

    def test_recovery_retries_an_unconfirmed_send_with_the_same_key(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        attempts: list[list[str]] = []

        def stop(_target: kill_action.KillTarget, _reason: str) -> bool:
            saved = self.saved_action(target)
            finished_at = datetime.fromtimestamp(saved.requested_at + 1, tz=ZoneInfo("UTC"))
            self.write_run(target, status="finished", finished_at=finished_at.isoformat(timespec="seconds"))
            return True

        def send(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            attempts.append(argv)
            return (subprocess.CompletedProcess(argv, -signal.SIGTERM, "", "")
                    if len(attempts) == 1 else subprocess.CompletedProcess(argv, 0, "KEPT: review", ""))

        with (patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", new=send),
              patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "stop_verified_scope", side_effect=stop),
              patch.object(actions, "_start_watch")):
            _ = actions.stop(reference, actions.handle(target), "Use the next slot.")
            saved = self.saved_action(target)
            self.assertIsInstance(saved.stage, kill_action.SendingNotice)
            self.assertEqual(len(attempts), 1)
            result = self.resume()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.QUEUED))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(attempts[0][attempts[0].index("--key") + 1], f"builds-kill:{saved.id}")
        self.assertEqual(attempts[0][attempts[0].index("--text") + 1], saved.message)
        self.assertTrue(saved.message.endswith("\nUse the next slot."))


if __name__ == "__main__":
    _ = unittest.main()

"""Use private process trees and message delivery for Builds stop actions."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, tzinfo
import fcntl
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
        _ = write_config_fixture(self.root / "review_links.conf", """\
# range: 5..300
kill_result_seconds=30
# range: 0.1..60
kill_pending_check_seconds=2
""")
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(self.root)})))

    def detached_target(self, *, name: str = "impl3", role: str = "worker",
                        step: str = "check") -> kill_action.KillTarget:
        record = self.root / "detached_runs" / "fixture.json"
        record.parent.mkdir()
        run_slice: process_stop.RunSliceIdentity = {"unit": "builds-detached-fixture.slice", "device": 1, "inode": 2,
            "path": "/user.slice/builds.slice/builds-detached.slice/builds-detached-fixture.slice"}
        route = kill_action.DetachedCommand(record, 17, "123", 16, "122", run_slice)
        run = kill_action.KillCandidate(17, "123", 17, "123", "session", step, "at")
        target = kill_action.KillTarget(run, route, staff.StaffId("fixture"), f"{name}, {role}", name, role)
        self.write_run(target)
        return target

    def write_run(self, target: kill_action.KillTarget, **fields: object) -> None:
        route = target.route
        _ = route.record.write_text(json.dumps({
            "run_id": route.record.stem, "status": "running", "staff_id": target.recipient,
            "command_slice": route.run_slice, "session_dir": str(self.root), "worker": target.recipient_name,
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
        moved = replace(route, run_slice={**route.run_slice, "inode": route.run_slice["inode"] + 1})

        def stop_detached(_record: Path, _reason: str, *, check: Callable[[], bool]) -> bool:
            return check()

        with (patch.object(process_stop, "stop_slice") as stop_slice,
              patch.object(detached_runs, "stop_command", side_effect=stop_detached) as stop,
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route") as read_route):
            read_route.side_effect = [route, moved]
            self.assertTrue(kill_action.stop_verified_run(target, reason))
            self.assertFalse(kill_action.stop_verified_run(target, reason))
        self.assertEqual([call.args for call in stop.call_args_list], [(route.record, reason)] * 2)
        self.assertEqual([set(call.kwargs) for call in stop.call_args_list], [{"check"}, {"check"}])
        stop_slice.assert_not_called()

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

    def test_detached_route_matches_the_saved_slice_for_a_step_scope(self) -> None:
        target = self.detached_target()
        proc = self.root / "proc"
        cgroups = self.root / "cgroups"
        route = target.route
        holder = 18
        run = replace(target.candidate, holder_pid=holder, holder_start="124")
        launcher = proc / str(route.group)
        step = proc / str(holder)
        for process in (launcher, step):
            process.mkdir(parents=True)
            _ = (process / "environ").write_text(f"UNIT_DETACHED_RECORD={route.record}\0")

        member_scope = Path(route.run_slice["path"]) / "run-holder.scope"
        scope_folder = cgroups / member_scope.relative_to("/")
        scope_folder.mkdir(parents=True)
        _ = (scope_folder / "cgroup.procs").write_text(f"{holder}\n")
        _ = (step / "cgroup").write_text(f"0::{member_scope}\n")
        identity = scope_folder.parent.stat()
        run_slice: process_stop.RunSliceIdentity = {**route.run_slice, "device": identity.st_dev, "inode": identity.st_ino}
        route = replace(route, run_slice=run_slice)
        target = replace(target, route=route)
        self.write_run(target, command_pid=route.group, command_group=route.group, command_started=route.started,
                       pid=route.supervisor, supervisor_started=route.supervisor_start, command=["cargo", "check"])
        table = {route.group: memory_admit.ProcessIdentity(route.supervisor, route.started),
                 holder: memory_admit.ProcessIdentity(route.group, run.holder_start)}
        with (patch.object(kill_action, "PROC", proc),
              patch.object(kill_action, "live", return_value=True),
              patch.object(kill_action, "program", return_value="cargo"),
              patch.object(kill_action, "in_group", return_value=True),
              patch.object(detached_run_process, "supervisor_match", return_value=True),
              patch.object(process_stop, "pin_process", return_value=nullcontext(5)),
              patch.object(process_stop, "PROC", proc),
              patch.object(process_stop, "CGROUP_ROOT", cgroups)):
            self.assertEqual(kill_action.stop_route(run, target.recipient, table), route)
            other_slice = member_scope.parent.with_name("builds-detached-other.slice")
            _ = (step / "cgroup").write_text(f"0::{other_slice / member_scope.name}\n")
            self.assertIsInstance(kill_action.stop_route(run, target.recipient, table), kill_action.NoStopRoute)

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
                self.assertIsInstance(saved.stage, kill_action.StopRequested)
                self.write_run(target, status="finished", finished_at=at.isoformat(timespec="seconds"),
                               stop_reason=received, stop_pass_requested=True)
                targets.return_value = {}
                return False

            with patch.object(kill_action, "stop_verified_run", side_effect=complete) as stop:
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

    def test_first_read_accepts_no_stop_for_ended_or_pruned_runs(self) -> None:
        at = datetime(2026, 11, 18, 19, 41, 0, 900_000, tzinfo=ZoneInfo("UTC"))

        class FixtureClock(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return at.astimezone(tz)

        for status in ("running", "finished", "pruned"):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=self.root) as folder:
                with patch.object(self, "root", Path(folder)):
                    target = self.detached_target()
                    reference = target.candidate.reference
                    actions = kill_action.KillActions(self.root)
                    with (patch.object(kill_action, "datetime", FixtureClock),
                          patch.object(kill_action, "live_targets", return_value={reference: target}) as targets,
                          patch.object(detached_runs, "stop_command") as stop,
                          patch.object(kill_action, "deliver") as send):
                        offers, _ = actions.options()
                        targets.return_value = {}
                        handle = offers[reference]["handle"]
                        self.assertIsInstance(actions.stop(reference, handle, ""), kill_action.NoStopRoute)
                        if status == "pruned":
                            target.route.record.unlink()
                        else:
                            self.write_run(target, status=status, finished_at=at.isoformat(timespec="seconds"))
                        outcome = actions.stop(reference, handle, "")
                        self.assertNotIsInstance(outcome, kill_action.NoStopRoute)
                        self.assertEqual(actions.options(), ({}, [outcome]))
                    saved = self.saved_action(target)
                    self.assertEqual((saved.result["stop"], saved.result["delivery"]),
                                     (kill_action.StopState.ALREADY_ENDED, kill_action.DeliveryState.PENDING))
                    self.assertIsInstance(saved.stage, kill_action.FinishedStop)
                    stop.assert_not_called()
                    send.assert_not_called()

    def test_completion_during_route_check_reports_already_ended(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        def finish() -> dict[str, kill_action.KillTarget]:
            self.write_run(target, status="finished")
            return {}

        with patch.object(kill_action, "live_targets", return_value={reference: target}):
            offers, _ = actions.options()
        with (patch.object(kill_action, "live_targets", side_effect=finish),
              patch.object(detached_runs, "stop_command") as stop,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            outcome = actions.stop(reference, offers[reference]["handle"], "")
        saved = self.saved_action(target)
        self.assertEqual(outcome, saved.result)
        self.assertEqual((saved.result["stop"], saved.result["delivery"]),
                         (kill_action.StopState.ALREADY_ENDED, kill_action.DeliveryState.PENDING))
        stop.assert_not_called()
        send.assert_not_called()

    def test_restart_reports_already_ended_if_the_record_is_pruned_before_commit(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        class Restart(BaseException):
            pass

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "stop_verified_run", side_effect=Restart)):
            offers, _ = actions.options()
            with self.assertRaises(Restart):
                _ = actions.stop(reference, offers[reference]["handle"], "Wait for release.")
        saved = self.saved_action(target)
        self.assertIsInstance(saved.stage, kill_action.StopRequested)
        self.assertEqual(saved.route, target.route)
        self.assertTrue(saved.message.endswith("\nWait for release."))
        target.route.record.unlink()
        with (patch.object(detached_runs, "stop_command") as stop,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            result = self.resume()
        stop.assert_not_called()
        send.assert_not_called()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.ALREADY_ENDED, kill_action.DeliveryState.PENDING))

    def save_unfinished_request(self, target: kill_action.KillTarget, *, committed: bool = False) -> None:
        actions = kill_action.KillActions(self.root)
        reference = target.candidate.reference

        def interrupt(_target: kill_action.KillTarget, reason: str) -> bool:
            if committed:
                self.write_run(target, stop_reason=reason, stop_pass_requested=True)
            raise InterruptedError("fixture stopped before the notice")

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "stop_verified_run", side_effect=interrupt),
              patch.object(actions, "_start_watch")):
            offers, _ = actions.options()
            _ = actions.stop(reference, offers[reference]["handle"], "Use the next slot.")
        self.assertIsInstance(self.saved_action(target).stage, kill_action.StopRequested)

    def test_restart_rejects_a_saved_request_when_the_locked_route_changed(self) -> None:
        target = self.detached_target()
        self.save_unfinished_request(target)
        path = target.route.record
        original = path.read_bytes()
        _ = write_config_fixture(self.root / "shutdown.conf", "STOP_GRACE_SECONDS=2\nKILL_GRACE_SECONDS=5")

        def changed_route(*_args: object) -> kill_action.NoStopRoute:
            with path.with_suffix(".scope-stop.lock").open("a") as lock, self.assertRaises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return kill_action.NoStopRoute()

        with (patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", side_effect=changed_route) as route,
              patch.object(process_stop, "slice_has_ended", return_value=False),
              patch.object(process_stop, "stop_slice") as stop,
              patch.object(kill_action, "deliver") as send):
            result = self.resume()
        route.assert_called_once_with(target.candidate, target.recipient, {})
        stop.assert_not_called()
        send.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.UNCONFIRMED, kill_action.DeliveryState.PENDING))

    def test_restart_finishes_its_saved_commit_without_a_live_route(self) -> None:
        target = self.detached_target()
        self.save_unfinished_request(target, committed=True)
        saved = self.saved_action(target)

        def stop(path: Path, reason: str, *, check: Callable[[], bool]) -> bool:
            self.assertEqual(path, target.route.record)
            self.assertEqual(reason, saved.message.partition("\n")[0])
            self.assertTrue(check())
            send.assert_not_called()
            self.write_run(target, status="finished", stop_reason=reason, stop_pass_requested=True)
            return True

        with (patch.object(detached_runs, "stop_command", side_effect=stop) as requested,
              patch.object(kill_action, "stop_route") as route,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.QUEUED) as send):
            result = self.resume()
        requested.assert_called_once_with(target.route.record, saved.message.partition("\n")[0], check=ANY)
        route.assert_not_called()
        send.assert_called_once_with(replace(saved, stage=kill_action.SendingNotice(),
                                             result={**saved.result, "stop": kill_action.StopState.STOPPED}))
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.QUEUED))

    def test_slice_exit_waits_for_the_finished_record_and_reports_retirement_once(self) -> None:
        target = self.detached_target(name="retired", role="producer")
        cgroups = self.root / "cgroups"
        folder = cgroups / target.route.run_slice["path"].lstrip("/")
        folder.mkdir(parents=True)
        identity = folder.stat()
        run_slice: process_stop.RunSliceIdentity = {**target.route.run_slice,
                                                    "device": identity.st_dev, "inode": identity.st_ino}
        target = replace(target, route=replace(target.route, run_slice=run_slice))
        self.write_run(target)
        events = folder / "cgroup.events"
        _ = events.write_text("populated 1\n")
        _ = write_config_fixture(self.root / "shutdown.conf", "STOP_GRACE_SECONDS=2\nKILL_GRACE_SECONDS=5")
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        def term(_directory: int, stopped: process_stop.RunSliceIdentity) -> None:
            self.assertEqual(stopped, run_slice)
            record = cast(dict[str, object], json.loads(target.route.record.read_text()))
            self.assertEqual(record.get("stop_reason"), self.saved_action(target).message)
            self.assertIs(record.get("stop_pass_requested"), True)
            _ = events.write_text("populated 0\n")

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(process_stop, "_term_slice", side_effect=term) as send_term,
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", return_value=target.route),
              patch.object(actions, "_start_watch"), patch.object(kill_action, "deliver") as send):
            offers, _ = actions.options()
            _ = actions.stop(reference, offers[reference]["handle"], "")
            self.assertTrue(process_stop.slice_has_ended(run_slice))
        send_term.assert_called_once_with(ANY, run_slice)
        send.assert_not_called()
        saved = self.saved_action(target)
        self.assertEqual(saved.result["stop"], kill_action.StopState.STOPPING)
        self.write_run(target, status="finished", stop_reason=saved.message, stop_pass_requested=True)
        response = subprocess.CompletedProcess(["send.py"], 2, "", "send.py: fixture: retired producer")
        with (patch.object(detached_runs, "stop_command") as retry,
              patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", return_value=response) as send):
            result = self.resume()
        retry.assert_not_called()
        send.assert_called_once()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.FAILED))

    def test_populated_slice_stays_pending_until_the_record_has_a_completion_stamp(self) -> None:
        target = self.detached_target()
        cgroups = self.root / "cgroups"
        folder = cgroups / Path(target.route.run_slice["path"]).relative_to("/")
        folder.mkdir(parents=True)
        identity = folder.stat()
        run_slice: process_stop.RunSliceIdentity = {**target.route.run_slice,
                                                    "device": identity.st_dev, "inode": identity.st_ino}
        target = replace(target, route=replace(target.route, run_slice=run_slice))
        self.write_run(target)
        events = folder / "cgroup.events"
        _ = events.write_text("populated 1\n")
        _ = write_config_fixture(self.root / "shutdown.conf", "STOP_GRACE_SECONDS=2\nKILL_GRACE_SECONDS=5")
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
              patch.object(process_stop, "_term_slice", side_effect=process_stop.SliceStopFailed("fixture stop failed")),
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", return_value=target.route),
              patch.object(actions, "_start_watch") as watch,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            offers, _ = actions.options()
            outcome = actions.stop(reference, offers[reference]["handle"], "")
            pending = self.saved_action(target)
            self.assertEqual(outcome, pending.result)
            self.assertIsInstance(pending.stage, kill_action.StopRequested)
            self.assertEqual((pending.result["stop"], pending.result["delivery"]),
                             (kill_action.StopState.UNCONFIRMED, kill_action.DeliveryState.PENDING))
            self.assertFalse(process_stop.slice_has_ended(run_slice))
            watch.assert_called_once()
            send.assert_not_called()

            _ = events.write_text("populated 0\n")
            self.write_run(target, finished_at="2026-11-18T19:41:00+00:00",
                           stop_reason=pending.message, stop_pass_requested=True)
            result = self.resume()
            self.assertEqual((result["stop"], result["delivery"]),
                             (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))
            send.assert_called_once()

    def test_recovery_retries_an_unconfirmed_send_with_the_same_key(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        attempts: list[list[str]] = []

        def stop(current: kill_action.KillTarget, reason: str) -> bool:
            self.assertEqual(current, target)
            self.assertEqual(reason, self.saved_action(target).message.partition("\n")[0])
            self.write_run(target, status="finished", stop_reason=reason, stop_pass_requested=True)
            return False

        def send(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            attempts.append(argv)
            return (subprocess.CompletedProcess(argv, -signal.SIGTERM, "", "")
                    if len(attempts) == 1 else subprocess.CompletedProcess(argv, 0, "KEPT: review", ""))

        with (patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", new=send),
              patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "stop_verified_run", side_effect=stop),
              patch.object(actions, "_start_watch")):
            offers, _ = actions.options()
            _ = actions.stop(reference, offers[reference]["handle"], "Use the next slot.")
            saved = self.saved_action(target)
            self.assertIsInstance(saved.stage, kill_action.SendingNotice)
            self.assertEqual((saved.result["stop"], saved.result["delivery"]),
                             (kill_action.StopState.STOPPED, kill_action.DeliveryState.PENDING))
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

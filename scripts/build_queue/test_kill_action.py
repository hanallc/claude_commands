"""Use private process trees and message delivery for Builds stop actions."""
from __future__ import annotations

from argparse import Namespace
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import IO, cast, override
import unittest
from unittest.mock import ANY, patch
from zoneinfo import ZoneInfo

from scripts.agents import detached_run_process, detached_runs, worker_lifecycle
from scripts.build_queue import admission_client, admission_records as records, admission_wire, kill_action
from scripts.build_queue.admission_jobs import VerifyCall
from scripts.build_queue.test_memory_feed import reservation_reading
from scripts.buildlog import store
from scripts.lib import atomic_file, process_start, process_stop
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
            "command_slice": route.run_slice, "completion_after_slice_cleanup": True,
            "session_dir": str(self.root), "worker": target.recipient_name,
            "label": "fixture", "command": ["cargo", "check"], "log": str(route.record.with_suffix(".log")),
            "started_at": 10, "pid": route.supervisor, "director_dir": "", "done_message_file": "", **fields,
        }))

    def finish_run(self, target: kill_action.KillTarget, **fields: object) -> None:
        path = target.route.record
        if fields:
            record = worker_lifecycle.read_json_object(path)
            record.update(fields)
            _ = path.write_text(json.dumps(record))
        with (patch.object(detached_runs, "_run", return_value=143),
              patch.object(detached_runs, "_completion_sources", return_value=nullcontext()),
              patch.object(detached_runs, "_finish_command_slice"),
              patch.object(detached_runs, "deliver_in_fresh_process", return_value=0),
              patch.object(detached_runs, "_complete_stopped_run", return_value="finished: fixture"),
              patch.object(detached_runs, "_complete_worker_dispatch", return_value=detached_runs.CompletionReplay.HANDLED)):
            self.assertEqual(detached_runs.command_supervise(Namespace(record=str(path))), 0)
        self.assertEqual(worker_lifecycle.read_json_object(path)["status"], "finished")

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
            offers, (result,) = recovered.options(15)
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
        self.save_unfinished_request(target)
        action = replace(self.saved_action(target), message=reason)

        def stop_detached(_record: Path, _reason: str, *, check: Callable[[], bool], stop_notice_id: str) -> bool:
            self.assertEqual(stop_notice_id, self.saved_action(target).id)
            return check()

        with (patch.object(process_stop, "stop_slice") as stop_slice,
              patch.object(detached_runs, "stop_command", side_effect=stop_detached) as stop,
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", side_effect=[route, moved])):
            self.assertTrue(kill_action.request_stop(action))
            self.assertFalse(kill_action.request_stop(action))
        self.assertEqual([call.args for call in stop.call_args_list], [(route.record, reason)] * 2)
        self.assertEqual([call.kwargs["stop_notice_id"] for call in stop.call_args_list], [action.id] * 2)
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

    def scoped_target(self) -> tuple[kill_action.KillTarget, dict[int, memory_admit.ProcessIdentity], Path]:
        """Match a verified detached command to a step scope in its run slice."""
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
        target = replace(target, candidate=run, route=route)
        self.write_run(target, command_pid=route.group, command_group=route.group, command_started=route.started,
                       pid=route.supervisor, supervisor_started=route.supervisor_start, command=["cargo", "check"])
        table = {route.group: memory_admit.ProcessIdentity(route.supervisor, route.started),
                 holder: memory_admit.ProcessIdentity(route.group, run.holder_start)}
        caller = staff.StaffRow(target.recipient, "worker", target.recipient_name, staff.StaffRoot(),
                                current_session=staff.StaffId(run.session))
        for context in (
            patch.object(kill_action, "PROC", proc),
            patch.object(kill_action, "live", return_value=True),
            patch.object(kill_action, "program", return_value="cargo"),
            patch.object(kill_action, "in_group", return_value=True),
            patch.object(detached_run_process, "supervisor_match", return_value=True),
            patch.object(process_stop, "pin_process", return_value=nullcontext(5)),
            patch.object(process_stop, "PROC", proc),
            patch.object(process_stop, "CGROUP_ROOT", cgroups),
            patch.object(store, "host_name", return_value="natedev"),
            patch.object(admission_client, "view", return_value=reservation_reading()),
            patch.object(kill_action, "candidate", return_value=run),
            patch.object(staff, "snapshot", return_value=staff.StaffSnapshot((caller,))),
            patch.object(memory_admit, "process_table", return_value=table),
        ):
            _ = self.enterContext(context)
        return target, table, member_scope

    def test_detached_route_matches_the_saved_slice_for_a_step_scope(self) -> None:
        target, table, member_scope = self.scoped_target()
        run, route = target.candidate, target.route
        self.assertEqual(kill_action.stop_route(run, target.recipient, table), route)
        other_slice = member_scope.parent.with_name("builds-detached-other.slice")
        step = kill_action.PROC / str(run.holder_pid)
        _ = (step / "cgroup").write_text(f"0::{other_slice / member_scope.name}\n")
        self.assertIsInstance(kill_action.stop_route(run, target.recipient, table), kill_action.NoStopRoute)

    def test_record_without_the_completion_contract_has_no_kill_offer_and_rejects_a_stale_handle(self) -> None:
        target, _, _ = self.scoped_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        offers, _ = actions.options(15)
        path = target.route.record
        record = worker_lifecycle.read_json_object(path)
        self.assertIs(record.pop("completion_after_slice_cleanup"), True)
        _ = path.write_text(json.dumps(record))

        with (patch.object(detached_runs, "stop_command") as stop,
              patch.object(kill_action, "deliver") as send):
            self.assertEqual(actions.options(15), ({}, []))
            self.assertIsInstance(actions.stop(reference, offers[reference]["handle"], ""), kill_action.NoStopRoute)

        stop.assert_not_called()
        send.assert_not_called()

    def test_launcher_record_without_a_published_supervisor_has_no_kill_offer(self) -> None:
        target, _, _ = self.scoped_target()
        path = target.route.record
        record = worker_lifecycle.read_json_object(path)
        for field in ("completion_after_slice_cleanup", "supervisor_started", "command_pid",
                      "command_group", "command_started", "command_slice"):
            del record[field]
        record["pid"] = 0
        _ = path.write_text(json.dumps(record))

        with patch.object(detached_runs, "command_group") as group:
            self.assertEqual(kill_action.KillActions(self.root).options(15), ({}, []))
        group.assert_not_called()

    def test_record_with_the_completion_contract_gets_kill_and_stops_after_the_slice_empties(self) -> None:
        target, _, member_scope = self.scoped_target()
        events = process_stop.CGROUP_ROOT / member_scope.parent.relative_to("/") / "cgroup.events"
        _ = events.write_text("populated 1\n")
        _ = write_config_fixture(self.root / "shutdown.conf", "STOP_GRACE_SECONDS=2\nKILL_GRACE_SECONDS=5")
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)

        with (patch.object(process_stop, "stop_slice", side_effect=process_stop.SliceStopFailed("fixture slice busy"))
                  as stop,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            offers, _ = actions.options(15)
            pending = actions.stop(reference, offers[reference]["handle"], "")
            self.assertEqual(actions.options(15), ({}, [pending]))
            self.assertEqual(self.saved_action(target).result["stop"], kill_action.StopState.STOPPING)
            stop.assert_called_once_with(target.route.run_slice, 2, 5)
            send.assert_not_called()

            _ = events.write_text("populated 0\n")
            self.finish_run(target)
            result = self.resume()

        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))
        stop.assert_called_once()
        send.assert_called_once()

    def test_recovery_finishes_the_stop_after_supervisor_exit_and_leaves_the_other_run(self) -> None:
        target, _, member_scope = self.scoped_target()
        path = target.route.record
        events = process_stop.CGROUP_ROOT / member_scope.parent.relative_to("/") / "cgroup.events"
        _ = events.write_text("populated 1\n")
        _ = write_config_fixture(self.root / "shutdown.conf", "STOP_GRACE_SECONDS=2\nKILL_GRACE_SECONDS=5")
        actions = kill_action.KillActions(self.root)
        reference = target.candidate.reference

        with (subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as peer,
              patch.object(process_stop, "stop_slice", side_effect=process_stop.SliceStopFailed("fixture slice busy"))
                  as stop,
              patch.object(detached_runs, "_complete_stopped_run", return_value="finished: fixture") as complete,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            peer_start = process_start.process_start(peer.pid)
            self.assertIsInstance(peer_start, process_start.Started)
            other_unit = "builds-detached-other.slice"
            other_slice = member_scope.parent.with_name(other_unit)
            other_folder = process_stop.CGROUP_ROOT / other_slice.relative_to("/")
            other_folder.mkdir()
            identity = other_folder.stat()
            _ = (other_folder / "cgroup.events").write_text("populated 1\n")
            _ = (other_folder / "cgroup.procs").write_text(f"{peer.pid}\n")
            other_path = path.with_name("other.json")
            other = replace(target, route=replace(target.route, record=other_path, supervisor=peer.pid,
                run_slice={"unit": other_unit, "path": str(other_slice), "device": identity.st_dev,
                           "inode": identity.st_ino}))
            self.write_run(other, stop_pass_requested=True, stop_reason="fixture peer stop")
            other_record = other_path.read_bytes()
            other_log = other_path.with_suffix(".log")
            _ = other_log.write_text("other run log\n")

            offers, _ = actions.options(15)
            _ = actions.stop(reference, offers[reference]["handle"], "")
            self.assertEqual(self.saved_action(target).result["stop"], kill_action.StopState.STOPPING)
            send.assert_not_called()

            _ = events.write_text("populated 0\n")
            with (patch.object(detached_runs, "_run", return_value=143),
                  patch.object(detached_runs, "_completion_sources", return_value=nullcontext()),
                  patch.object(detached_runs, "deliver_in_fresh_process", side_effect=InterruptedError("fixture exit")),
                  self.assertRaises(InterruptedError)):
                _ = detached_runs.command_supervise(Namespace(record=str(path)))
            record = worker_lifecycle.read_json_object(path)
            self.assertEqual((record["status"], record["exit_code"]), ("running", 143))
            self.assertNotIn("finished_at", record)

            def supervises(pid: int, record: Path) -> bool:
                return (pid, record) == (peer.pid, other_path)

            with patch.object(detached_run_process, "supervises", side_effect=supervises):
                result = self.resume()
            record = worker_lifecycle.read_json_object(path)
            self.assertEqual((record["status"], record["ended"]), ("finished", "supervisor gone"))
            self.assertIn("finished_at", record)
            self.assertEqual((result["stop"], result["delivery"]),
                             (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))
            self.assertEqual(other_path.read_bytes(), other_record)
            self.assertEqual(other_log.read_text(), "other run log\n")
            self.assertEqual(process_start.process_start(peer.pid), peer_start)
            self.assertEqual(set(other_path.parent.glob("other.*")), {other_path, other_log})

        stop.assert_called_once_with(target.route.run_slice, 2, 5)
        complete.assert_called_once_with(path, ANY)
        send.assert_called_once()

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
            offers, _ = actions.options(15)
            self.assertEqual(offers[run.reference]["message"], reason)
            handle = offers[run.reference]["handle"]
            reused = replace(run, holder_start="456", row_start="456")
            targets.return_value = {reused.reference: replace(target, candidate=reused)}
            self.assertIsInstance(actions.stop(run.reference, handle, ""), kill_action.NoStopRoute)
            targets.return_value = {run.reference: target}

            def complete(action: kill_action.SavedKillAction) -> bool:
                self.assertEqual((action.route, action.run), (target.route, run))
                self.assertEqual(action.message.partition("\n")[0], reason)
                deliver.assert_not_called()
                self.assertEqual(actions.options(15)[1][0]["stop"], kill_action.StopState.STOPPING)
                saved = self.saved_action(target)
                self.assertIsInstance(saved.stage, kill_action.StopRequested)
                self.finish_run(target, stop_reason=reason, stop_pass_requested=True, stop_notice_id=action.id)
                targets.return_value = {}
                return False

            with patch.object(kill_action, "request_stop", side_effect=complete) as stop:
                result = actions.stop(run.reference, handle, "Use the next slot.")
            stop.assert_called_once()
            deliver.assert_called_once()
            (saved,) = cast(tuple[kill_action.SavedKillAction], deliver.call_args.args)
            self.assertEqual((saved.recipient, saved.message), (target.recipient, reason + "\nUse the next slot."))
            self.assertEqual(result, {"reference": run.reference, "step": "nextest", "pid": 17,
                                      "stop": kill_action.StopState.STOPPED, "delivery": kill_action.DeliveryState.SENT,
                                      "time": "19:41 EST", "recipient_name": "impl3", "recipient_role": "worker"})
            self.assertEqual(actions.options(15), ({}, [result]))
            self.assertIsInstance(actions.stop(run.reference, handle, "Again."), kill_action.NoStopRoute)

    def test_first_read_accepts_no_stop_for_ended_or_pruned_runs(self) -> None:
        at = datetime(2026, 11, 18, 19, 41, 0, 900_000, tzinfo=ZoneInfo("UTC"))

        class FixtureClock(datetime):
            @classmethod
            @override
            def now(cls, tz: tzinfo | None = None) -> datetime:
                return at.astimezone(tz)

        for status in ("finished", "pruned"):
            with self.subTest(status=status), tempfile.TemporaryDirectory(dir=self.root) as folder:
                with patch.object(self, "root", Path(folder)):
                    target = self.detached_target()
                    reference = target.candidate.reference
                    actions = kill_action.KillActions(self.root)
                    with (patch.object(kill_action, "datetime", FixtureClock),
                          patch.object(kill_action, "live_targets", return_value={reference: target}) as targets,
                          patch.object(detached_runs, "stop_command") as stop,
                          patch.object(kill_action, "deliver") as send):
                        offers, _ = actions.options(15)
                        targets.return_value = {}
                        handle = offers[reference]["handle"]
                        self.assertIsInstance(actions.stop(reference, handle, ""), kill_action.NoStopRoute)
                        if status == "pruned":
                            target.route.record.unlink()
                        else:
                            self.finish_run(target)
                        outcome = actions.stop(reference, handle, "")
                        self.assertNotIsInstance(outcome, kill_action.NoStopRoute)
                        self.assertEqual(actions.options(15), ({}, [outcome]))
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
            self.finish_run(target)
            return {}

        with patch.object(kill_action, "live_targets", return_value={reference: target}):
            offers, _ = actions.options(15)
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
              patch.object(kill_action, "request_stop", side_effect=Restart)):
            offers, _ = actions.options(15)
            with self.assertRaises(Restart):
                _ = actions.stop(reference, offers[reference]["handle"], "Wait for release.")
        saved = self.saved_action(target)
        self.assertIsInstance(saved.stage, kill_action.StopRequested)
        self.assertEqual(saved.route, target.route)
        self.assertTrue(saved.message.endswith("\nWait for release."))
        self.finish_run(target)
        with (patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(process_stop, "slice_has_ended", return_value=True)):
            self.assertEqual(detached_runs.prune_finished(target.route.record.parent), [target.route.record.stem])
        with (patch.object(detached_runs, "stop_command") as stop,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            result = self.resume()
        stop.assert_not_called()
        send.assert_not_called()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.ALREADY_ENDED, kill_action.DeliveryState.PENDING))

    def test_prune_during_the_stop_lock_wait_reports_already_ended(self) -> None:
        target = self.detached_target()
        path = target.route.record
        lock_path = path.with_suffix(".scope-stop.lock")
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        flock = fcntl.flock
        _ = write_config_fixture(self.root / "shutdown.conf", "STOP_GRACE_SECONDS=2\nKILL_GRACE_SECONDS=5")

        def prune_before_lock(descriptor: int | IO[str], operation: int) -> None:
            fd = descriptor if isinstance(descriptor, int) else descriptor.fileno()
            if lock_path.exists() and os.path.samestat(os.fstat(fd), lock_path.stat()):
                self.finish_run(target)
                self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])
            flock(descriptor, operation)

        with (patch.object(fcntl, "flock", side_effect=prune_before_lock),
              patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(process_stop, "slice_has_ended", return_value=True),
              patch.object(process_stop, "stop_slice") as stop,
              patch.object(kill_action, "deliver") as send):
            offers, _ = actions.options(15)
            outcome = actions.stop(reference, offers[reference]["handle"], "")
        saved = self.saved_action(target)
        self.assertEqual(outcome, saved.result)
        self.assertIsInstance(saved.stage, kill_action.FinishedStop)
        self.assertEqual(saved.result["stop"], kill_action.StopState.ALREADY_ENDED)
        self.assertFalse(path.exists())
        stop.assert_not_called()
        send.assert_not_called()

    def test_long_restart_keeps_the_commit_until_builds_saves_the_notice(self) -> None:
        target = self.detached_target()
        path = target.route.record
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        _ = write_config_fixture(self.root / "shutdown.conf", "STOP_GRACE_SECONDS=2\nKILL_GRACE_SECONDS=5")

        class Restart(BaseException):
            pass

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "stop_route", return_value=target.route),
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(process_stop, "slice_has_ended", return_value=False),
              patch.object(process_stop, "stop_slice", side_effect=Restart) as stop):
            offers, _ = actions.options(15)
            with self.assertRaises(Restart):
                _ = actions.stop(reference, offers[reference]["handle"], "Use the next slot.")
        stop.assert_called_once()
        saved = self.saved_action(target)
        self.assertIsInstance(saved.stage, kill_action.StopRequested)
        record = cast(dict[str, object], json.loads(path.read_text()))
        self.assertEqual(record["stop_notice_id"], saved.id)
        self.finish_run(target)
        finished = datetime.now(timezone.utc).replace(microsecond=0)

        def prune_before_send(action: kill_action.SavedKillAction) -> kill_action.DeliveryState:
            self.assertEqual(self.saved_action(target), action)
            self.assertIsInstance(action.stage, kill_action.SendingNotice)
            self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])
            return kill_action.DeliveryState.SENT

        with (patch.object(time, "time", return_value=(finished + timedelta(days=1)).timestamp()),
              patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
              patch.object(process_stop, "slice_has_ended", return_value=True),
              patch.object(detached_runs, "stop_command") as retry,
              patch.object(kill_action, "deliver", side_effect=prune_before_send) as send):
            self.assertEqual(detached_runs.prune_finished(path.parent), [])
            result = self.resume()
        self.assertFalse(path.exists())
        retry.assert_not_called()
        send.assert_called_once()
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))

    def test_recovered_notice_requires_a_durable_builds_state(self) -> None:
        target = self.detached_target()
        self.save_unfinished_request(target, committed=True)
        saved = self.saved_action(target)
        record = target.route.record
        self.finish_run(target)
        sync_folder = atomic_file.sync_folder
        failed_syncs = 0

        def fail_sending_notice_sync(folder: Path) -> None:
            nonlocal failed_syncs
            if folder == self.root / ".review-link-build-kill" and isinstance(
                    self.saved_action(target).stage, kill_action.SendingNotice):
                failed_syncs += 1
                raise OSError("fixture directory sync failed")
            sync_folder(folder)

        with (patch.object(atomic_file, "sync_folder", side_effect=fail_sending_notice_sync),
              patch.object(detached_runs, "acknowledge_stop_notice", wraps=detached_runs.acknowledge_stop_notice)
              as acknowledge,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send,
              patch.object(threading.Thread, "start", autospec=True) as start):
            actions = kill_action.KillActions(self.root)
            actions.resume()
            for attempt in range(1, 4):
                self.assertEqual(start.call_count, attempt)
                (worker,) = cast(tuple[threading.Thread], start.call_args.args)
                worker.run()
                if attempt < 3:
                    self.assertEqual(failed_syncs, attempt)
                    self.assertEqual(worker_lifecycle.read_json_object(record)["stop_notice_id"], saved.id)
                    acknowledge.assert_not_called()
                    self.assertEqual(send.call_count, attempt - 1)
                actions.retry_pending()
            self.assertEqual(start.call_count, 3)
        self.assertIsInstance(self.saved_action(target).stage, kill_action.FinishedStop)
        acknowledge.assert_called_once_with(record, saved.id)
        send.assert_called_once()
        self.assertNotIn("stop_notice_id", worker_lifecycle.read_json_object(record))

    def test_the_running_service_retries_acknowledgment_without_sending_again(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        record = target.route.record
        actions = kill_action.KillActions(self.root)
        acknowledge_stop_notice = detached_runs.acknowledge_stop_notice

        def finish_run(action: kill_action.SavedKillAction) -> bool:
            self.finish_run(target, stop_pass_requested=True, stop_notice_id=action.id)
            return True

        def acknowledge_notice(path: Path, notice_id: str) -> None:
            action = self.saved_action(target)
            self.assertEqual((path, notice_id), (record, action.id))
            if isinstance(action.stage, kill_action.SendingNotice):
                send.assert_not_called()
                raise PermissionError("fixture acknowledgment failed")
            self.assertIsInstance(action.stage, kill_action.FinishedStop)
            send.assert_called_once()
            acknowledge_stop_notice(path, notice_id)

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "request_stop", side_effect=finish_run),
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send,
              patch.object(detached_runs, "acknowledge_stop_notice", side_effect=acknowledge_notice) as acknowledge,
              patch.object(threading.Thread, "start", autospec=True) as start):
            offers, _ = actions.options(15)
            result = actions.stop(reference, offers[reference]["handle"], "")
            send.assert_called_once()
            acknowledge.assert_called_once()
            saved = self.saved_action(target)
            self.assertEqual(result, saved.result)
            self.assertEqual(saved.result["delivery"], kill_action.DeliveryState.SENT)
            self.assertEqual(worker_lifecycle.read_json_object(record)["stop_notice_id"], saved.id)
            start.assert_not_called()
            actions.retry_pending()
            start.assert_called_once()
            (worker,) = cast(tuple[threading.Thread], start.call_args.args)
            worker.run()
            actions.retry_pending()
            start.assert_called_once()
        send.assert_called_once()
        self.assertEqual(acknowledge.call_count, 2)
        self.assertNotIn("stop_notice_id", worker_lifecycle.read_json_object(record))

    def test_recovery_releases_the_notice_after_confirmed_abandonment(self) -> None:
        target = self.detached_target()
        self.save_unfinished_request(target, committed=True)
        saved = self.saved_action(target)
        folder = self.root / ".review-link-build-kill"
        action_file = folder / f"{target.candidate.reference}.json"
        _ = action_file.write_text("unreadable action")
        record = target.route.record

        with patch.object(kill_action, "deliver") as send:
            kill_action.KillActions(self.root).resume()
            self.assertEqual(worker_lifecycle.read_json_object(record)["stop_notice_id"], saved.id)
            action_file.unlink()
            kill_action.KillActions(self.root).resume()
        send.assert_not_called()
        raw = worker_lifecycle.read_json_object(record)
        self.assertNotIn("stop_notice_id", raw)
        self.assertEqual(raw["status"], "running")
        self.assertIs(raw["stop_pass_requested"], True)
        self.assertEqual(list(folder.glob("*.notice")), [])

    def save_unfinished_request(self, target: kill_action.KillTarget, *, committed: bool = False) -> None:
        actions = kill_action.KillActions(self.root)
        reference = target.candidate.reference

        def interrupt(action: kill_action.SavedKillAction) -> bool:
            if committed:
                self.write_run(target, stop_reason=action.message.partition("\n")[0], stop_pass_requested=True,
                               stop_notice_id=action.id)
            raise InterruptedError("fixture stopped before the notice")

        with (patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(detached_run_process, "supervises", return_value=True),
              patch.object(kill_action, "request_stop", side_effect=interrupt)):
            offers, _ = actions.options(15)
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

        def stop(path: Path, reason: str, *, check: Callable[[], bool], stop_notice_id: str) -> bool:
            self.assertEqual(path, target.route.record)
            self.assertEqual(reason, saved.message.partition("\n")[0])
            self.assertTrue(check())
            send.assert_not_called()
            self.finish_run(target, stop_reason=reason, stop_pass_requested=True, stop_notice_id=stop_notice_id)
            return True

        with (patch.object(detached_runs, "stop_command", side_effect=stop) as requested,
              patch.object(kill_action, "stop_route") as route,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.QUEUED) as send):
            result = self.resume()
        requested.assert_called_once_with(target.route.record, saved.message.partition("\n")[0], check=ANY,
                                          stop_notice_id=saved.id)
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
              patch.object(detached_run_process, "supervises", return_value=True),
              patch.object(memory_admit, "process_table", return_value={}),
              patch.object(kill_action, "stop_route", return_value=target.route),
              patch.object(kill_action, "deliver") as send):
            offers, _ = actions.options(15)
            _ = actions.stop(reference, offers[reference]["handle"], "")
            self.assertTrue(process_stop.slice_has_ended(run_slice))
        send_term.assert_called_once_with(ANY, run_slice)
        send.assert_not_called()
        saved = self.saved_action(target)
        self.assertEqual(saved.result["stop"], kill_action.StopState.STOPPING)
        with patch.object(process_stop, "CGROUP_ROOT", cgroups):
            self.finish_run(target)
        response = subprocess.CompletedProcess(["send.py"], 2, "", "send.py: fixture: retired producer")
        with (patch.object(detached_runs, "stop_command") as retry,
              patch.object(process_stop, "CGROUP_ROOT", cgroups),
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
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send):
            offers, _ = actions.options(15)
            outcome = actions.stop(reference, offers[reference]["handle"], "")
            pending = self.saved_action(target)
            self.assertEqual(outcome, pending.result)
            self.assertIsInstance(pending.stage, kill_action.StopRequested)
            self.assertEqual((pending.result["stop"], pending.result["delivery"]),
                             (kill_action.StopState.STOPPING, kill_action.DeliveryState.PENDING))
            with (patch.object(time, "time", return_value=time.time() + 3600),
                  patch.object(threading.Thread, "start", autospec=True) as start):
                actions.retry_pending()
                start.assert_called_once()
                (worker,) = cast(tuple[threading.Thread], start.call_args.args)
                worker.run()
                self.assertEqual(actions.options(15), ({}, [pending.result]))
            self.assertFalse(process_stop.slice_has_ended(run_slice))
            send.assert_not_called()

            _ = events.write_text("populated 0\n")
            self.finish_run(target)
            result = self.resume()
            self.assertEqual((result["stop"], result["delivery"]),
                             (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))
            send.assert_called_once()

    def test_finished_record_stays_pending_while_the_saved_slice_is_populated(self) -> None:
        target = self.detached_target()
        self.save_unfinished_request(target, committed=True)
        pending = self.saved_action(target)
        self.finish_run(target)
        actions = kill_action.KillActions(self.root)

        with (patch.object(process_stop, "slice_has_ended", side_effect=(False, True)) as empty,
              patch.object(kill_action, "live_targets", return_value={}),
              patch.object(kill_action, "request_stop") as request,
              patch.object(kill_action, "deliver", return_value=kill_action.DeliveryState.SENT) as send,
              patch.object(threading.Thread, "start", autospec=True) as start):
            actions.resume()
            start.assert_called_once()
            (first,) = cast(tuple[threading.Thread], start.call_args.args)
            first.run()

            self.assertEqual(actions.options(15), ({}, [pending.result]))
            self.assertEqual(self.saved_action(target), pending)
            send.assert_not_called()
            empty.assert_called_once_with(target.route.run_slice)

            actions.retry_pending()
            self.assertEqual(start.call_count, 2)
            (second,) = cast(tuple[threading.Thread], start.call_args.args)
            second.run()
            actions.retry_pending()
            self.assertEqual(start.call_count, 2)
            _, (result,) = actions.options(15)
            self.assertEqual(self.saved_action(target).result, result)

        request.assert_not_called()
        send.assert_called_once()
        self.assertEqual(empty.call_args_list, [((target.route.run_slice,), {})] * 2)
        self.assertEqual((result["stop"], result["delivery"]),
                         (kill_action.StopState.STOPPED, kill_action.DeliveryState.SENT))

    def test_recovery_retries_an_unconfirmed_send_with_the_same_key(self) -> None:
        target = self.detached_target()
        reference = target.candidate.reference
        actions = kill_action.KillActions(self.root)
        attempts: list[list[str]] = []

        def stop(action: kill_action.SavedKillAction) -> bool:
            self.assertEqual(action, self.saved_action(target))
            self.finish_run(target, stop_reason=action.message.partition("\n")[0],
                            stop_pass_requested=True, stop_notice_id=action.id)
            return False

        def send(argv: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            attempts.append(argv)
            if len(attempts) == 1:
                return subprocess.CompletedProcess(argv, -signal.SIGTERM, "", "")
            path = target.route.record
            self.assertEqual(detached_runs.prune_finished(path.parent), [path.stem])
            return subprocess.CompletedProcess(argv, 0, "KEPT: review", "")

        with (patch.object(send_budget, "seconds", return_value=1),
              patch.object(subprocess, "run", new=send),
              patch.object(kill_action, "live_targets", return_value={reference: target}),
              patch.object(kill_action, "request_stop", side_effect=stop)):
            offers, _ = actions.options(15)
            with patch.object(detached_runs, "acknowledge_stop_notice", side_effect=OSError("fixture write failed")):
                _ = actions.stop(reference, offers[reference]["handle"], "Use the next slot.")
            saved = self.saved_action(target)
            self.assertIsInstance(saved.stage, kill_action.SendingNotice)
            self.assertEqual((saved.result["stop"], saved.result["delivery"]),
                             (kill_action.StopState.STOPPED, kill_action.DeliveryState.PENDING))
            self.assertEqual(len(attempts), 1)
            with (patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())),
                  patch.object(process_stop, "slice_has_ended", return_value=True)):
                self.assertEqual(detached_runs.prune_finished(target.route.record.parent), [])
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

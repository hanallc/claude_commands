"""Check the Build limits reading with private settings and queue records."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from unittest import mock

from scripts.build_hold.owning_session import OwningSession
from scripts.build_queue import admission_protocol as wire, admission_records as records, admission_service, limits_view, memory_feed, placement, queue_view
from scripts.build_queue import test_placement
from scripts.build_queue.cloud_progress import NoCloudProgress
from scripts.build_queue.progress_profile import NoCounter
from scripts.build_queue.queue_fixture import fixture_reservation, head_reading
from scripts.build_targets import target_config, targets
from scripts.build_targets.execution_transport import HanaControlTransport
from scripts.buildlog import charts, repository, store
from scripts.lib import conf, process_start
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import claim_state, eligibility


class BuildLimitsTests(unittest.TestCase):
    def test_limits_use_real_policy_shapes_and_keep_each_wait_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as index:
            root = Path(directory)
            workspace = root / "scratch-checkout"
            clone = root / "clone"
            worktree = root / "worktree"
            fixture = root / "fixture"
            old_checkout = root / "old-checkout"
            workspace.mkdir()
            fixture.mkdir()
            old_checkout.mkdir()
            env = fixture_git_environment(root)

            def git(folder: Path, *arguments: str) -> None:
                _ = subprocess.run(["git", "-C", str(folder), *arguments], env=env,
                                   timeout=TEST_HANG_GUARD_SECS, capture_output=True, check=True)

            git(workspace, "init", "-q")
            git(fixture, "init", "-q")
            git(old_checkout, "init", "-q")
            git(old_checkout, "remote", "add", "origin", "https://example.invalid/old-team/workspace")
            git(workspace, "remote", "add", "origin", "git@example.invalid:team/workspace.git")
            _ = commit_repository(workspace, "Fixture", allow_empty=True, env=env)
            git(root, "clone", "-q", str(workspace), str(clone))
            git(workspace, "worktree", "add", "--detach", str(worktree), "HEAD")
            _ = index.execute("CREATE TABLE steps (repo_identity TEXT, repo_path TEXT, target TEXT, host TEXT, started_at TEXT)")
            reading = head_reading()
            now = reading.explanation.now
            _ = index.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?)", [
                ("https://example.invalid/quiet", "", "natedev", "", store.utc_iso(now - 29 * 86400)),
                ("https://example.invalid/old", "", "natedev", "", store.utc_iso(now - 31 * 86400)),
                ("https://example.invalid/claude_commands", "", "aws", "", store.utc_iso(now)),
                ("https://example.invalid/hana", "", "aws", "", store.utc_iso(now)),
                ("git-root:workspace", str(clone), "natedev", "", store.utc_iso(now)),
                ("git-root:workspace", str(worktree), "natedev", "", store.utc_iso(now)),
                ("https://example.invalid/old-team/workspace", str(old_checkout), "mac", "", store.utc_iso(now)),
                ("https://example.invalid/old-team/workspace", str(workspace), "mac", "", store.utc_iso(now)),
                ("https://example.invalid/team/workspace", str(workspace), "natedev", "", store.utc_iso(now)),
                ("git-root:fixture", str(fixture), "natedev", "", store.utc_iso(now)),
                ("git-root:missing", str(root / "missing"), "natedev", "", store.utc_iso(now)),
            ])
            with mock.patch.object(repository, "output", wraps=repository.output) as lookup:
                history = limits_view.recent_steps(index, now)
                self.assertEqual(history.origins.resolve("", str(workspace)), "https://example.invalid/team/workspace")
                self.assertEqual(history.machines["https://example.invalid/team/workspace"], {"natedev", "mac"})
                self.assertEqual(lookup.call_count, 4)
                unseen = str(root / "unseen")
                self.assertEqual(history.origins.resolve("", unseen), "")
                self.assertIn(unseen, history.origins.active_folders)
                self.assertEqual(lookup.call_count, 4)
            saved = limits_view.Background(history, limits_view.Metric("Money today", 8, 25, "USD"),
                                           limits_view.Metric("Money today", 2, 5, "USD"), origins=history.origins)
            gate_source = workspace / "scripts/production/gate_routes.py"
            gate_source.parent.mkdir(parents=True)
            _ = gate_source.write_text('class SuiteMode:\n    GATE = "merge-gate"\n')
            git(workspace, "add", str(gate_source))
            _ = commit_repository(workspace, "Fixture route", env=env)
            route_origin = "https://example.invalid/team/workspace"
            routes = replace(history, checkouts={route_origin: workspace})
            with mock.patch.object(repository, "output", wraps=repository.output) as route_read:
                self.assertEqual(limits_view.aws_gate_routes(routes, True), {route_origin: True})
                route_read.assert_called_once_with(
                    ["git", "show", "HEAD:scripts/production/gate_routes.py"], str(workspace), repository.GIT_TIMEOUT_S)

            started = Event()
            release = Event()
            updated = replace(saved, aws_money=limits_view.Metric("Money today", 9, 25, "USD"))

            def read_limits(_now: float, _folders: tuple[str, ...]) -> limits_view.Background:
                started.set()
                if not release.wait(TEST_HANG_GUARD_SECS):
                    raise RuntimeError("Fixture refresh did not finish")
                return updated

            refresh = memory_feed.LimitsRefresh()
            refresh.saved = saved
            with mock.patch.object(limits_view, "background", side_effect=read_limits) as refresh_read:
                try:
                    self.assertIs(refresh.current(10, 60), saved)
                    self.assertTrue(started.wait(TEST_HANG_GUARD_SECS))
                    self.assertIs(refresh.current(11, 60), saved)
                    refresh_read.assert_called_once()
                finally:
                    release.set()
                pending = refresh.pending
                assert pending is not None
                self.assertIs(pending.result(timeout=TEST_HANG_GUARD_SECS), updated)
                self.assertIs(refresh.current(12, 60), updated)
            failed = replace(updated, history=charts.IndexUnavailable("fixture index failed"),
                             cloud_routes={}, origins=limits_view.Origins(), aws_money=saved.aws_money)
            refresh.pending = Future()
            refresh.pending.set_result(failed)
            stale_saved = refresh.current(13, 60)
            assert stale_saved is not None
            self.assertTrue(stale_saved.history_stale)
            self.assertIs(stale_saved.history, history)
            self.assertEqual(stale_saved.aws_money, failed.aws_money)
            refresh.pending = Future()
            refresh.pending.set_result(updated)
            self.assertIs(refresh.current(14, 60), updated)
            self.assertFalse(updated.history_stale)

            _ = (root / "memory_feed.conf").write_text("# whole: 1..\nmac_activity_refresh_seconds=10\n")
            _ = (root / "build_admission.conf").write_text(
                "[disk]\n# whole: 1..\nfree_floor_gib=31\n[]\n# whole: 1..\ncloud_gate_slots=4\n")
            _ = (root / "cloud_gate.conf").write_text("enabled=on\n")
            _ = (root / "mac_test.conf").write_text("offload=on\nlinux_only..claude=\nlinux_only.workspace=\n" +
                                                   "# range: 1..\nmac_budget_gib=18\n# range: 1..\nmac_budget_gib.workspace=27\n" +
                                                   "# range: 1..\nfree_floor_gib=100\n")
            _ = (root / "build_folders.conf").write_text(f"roots.slot_cargo={root}\n")
            baseline = reading.explanation
            policy = replace(baseline.policy,
                repository_runs=records.RepositoryRunLimits({"natedev.claude_commands": 2}, "merge-owner"),
                gpu_tests=records.GpuTestSlots(3, frozenset({"workspace"})))
            reserve: records.Reservation = {**fixture_reservation(101, 0, step="gate"),
                "repo": "claude_commands", "queue_waiter": {"pid": 101, "start_time": "101", "since": now,
                    "need": 0, "unit": "regular", "repo_identity": "https://example.invalid/claude_commands"}}
            merge: records.Reservation = {**reserve, "pid": 102,
                "queue_waiter": {**reserve["queue_waiter"], "pid": 102, "unit": "merge-owner"}}
            limits = records.RunLimitWait(2, 2, "claude_commands", "natedev")
            waiting = (
                records.WaitingStep(1, "", "", "gate", "", now, 0, limits, project="claude_commands"),
                records.WaitingStep(2, "", "", "nextest", str(worktree), now, 0, records.ReadyForAdmission(),
                    placement_pending=True, placement_episode="mac-episode", caller_start="matched"),
                records.WaitingStep(3, "", "", "check", str(clone), now, 0, records.BuildSpaceLow(20, 31)),
                records.WaitingStep(6, "", "", "check", "", now, 0, records.ReadyForAdmission(),
                    placement_block=records.PlacementBlock("ssh", "hana-aws", "memory", "hana-episode")),
                records.WaitingStep(9, "", "", "check", str(worktree), now, 0, limits),
                records.WaitingStep(10, "", "", "check", str(worktree), now, 0, records.ReadyForAdmission(),
                    placement_block=records.PlacementBlock("mac", "mac", "disk", "mac-disk")),
                records.WaitingStep(11, "", "", "check", str(worktree), now, 0, records.EstimateExpired(now)),
                records.WaitingStep(12, "", "", "", str(worktree), now, 0, records.ReadyForAdmission()),
                records.WaitingStep(1, "", "", "gate", "", now, 0, limits, project="claude_commands"),
                records.WaitingStep(13, "", "", "check", str(worktree), now, 0, records.NeedsMemory(),
                    placement_pending=True),
                records.WaitingStep(2, "", "", "nextest", str(worktree), now, 0, records.ReadyForAdmission(),
                    placement_pending=True, placement_episode="other-episode", caller_start="matched"),
                records.WaitingStep(7, "", "", "nextest", str(worktree), now, 0, records.NeedsMemory(),
                    caller_start="old"),
                records.WaitingStep(7, "", "", "nextest", str(worktree), now + 1, 0, records.ReadyForAdmission(),
                    caller_start="direct"),
                records.WaitingStep(14, "", "", "check", str(worktree), now, 0, records.BehindHead("older check", now, "")),
            )
            explanation = replace(baseline, policy=policy, decided_from=replace(baseline.decided_from,
                reservations=(reserve, merge), build_space=records.BuildSpaceLow(20, 31)))
            view = replace(reading.view, waiting=waiting)
            cloud_owner = wire.Owner(4, "4")
            cloud = wire.CloudRow(cloud_owner, wire.Identity("", "", "gate", "", "cloud"),
                                  wire.CloudPlace("aws"), now, 0, NoCloudProgress(), NoCounter(), wire.CloudSlotWait(4, 4))
            slot = wire.RepositorySlotRow(wire.Owner(5, "5"), wire.Identity("", "", "check", str(worktree), "slot"),
                now, "mac", wire.RepositorySlotWaiting("workspace", (1,), 1, "fixture"))
            repository_keys = {1: "https://example.invalid/claude_commands", 6: "https://example.invalid/hana"}
            order = [(root / str(step.pid), records.RankedWaiter(pid=step.pid, start_time=step.caller_start,
                        since=step.since, need=step.need, folder=step.folder,
                        repo_identity=repository_keys.get(step.pid, ""), order=records.NormalOrder()))
                     for step in waiting]
            queue = wire.CloudQueueRead(view, explanation, order, cloud=(cloud,), cloud_slots=4,
                                        cloud_gate_stale_pushes=1, repository_slots=(slot,))
            until = datetime.fromtimestamp(now, UTC) + timedelta(hours=1)
            mac = targets.TargetSnapshot(target_config.SshTarget("mac", "macos", root / "mac_test.conf"),
                (targets.Held("holder", "fixture", targets.StartNotRecorded(), targets.ExpiresAt(until)),),
                (), eligibility.NO_BACKOFF)
            hana = targets.TargetSnapshot(target_config.SshTarget("hana-aws", "linux", root / "hana.conf"),
                (), (), eligibility.NO_BACKOFF)
            paths = claim_state.StatePaths(root / "mac")
            mac_activity = root / "mac-builds.json"
            _ = mac_activity.write_text(json.dumps({"read_at": now, "builds": [], "disk_free_bytes": 80 * records.GIB}))
            with mock.patch.object(queue_view, "mac_reading_path", return_value=mac_activity):
                activity = queue_view.read_mac_builds()
            processes: dict[int, process_start.ProcessStart] = {
                os.getpid(): process_start.Started("helper"), 2: process_start.Started("matched"),
                7: process_start.Started("direct"), 8: process_start.Started("ends")}

            def process(pid: int) -> process_start.ProcessStart:
                return processes[pid]

            with (mock.patch.dict(os.environ, {"CONF_DIR": directory}),
                    mock.patch.object(process_start, "process_start", side_effect=process),
                    mock.patch.object(target_config, "ssh_target_state_directory", return_value=paths.directory),
                    mock.patch.object(target_config, "read_execution_transport",
                                      return_value=HanaControlTransport(root / "hana.conf", "aws-hana")),
                    mock.patch.object(limits_view, "hana_route", return_value=None),
                    claim_state.record_claim_wait(paths, 2, str(worktree), "nextest", OwningSession("mac"), "mac-episode"),
                    claim_state.record_claim_wait(paths, 7, str(worktree), "nextest", OwningSession("direct"), "")):
                with claim_state.record_claim_wait(paths, 8, str(worktree), "nextest", OwningSession("ended"), ""):
                    self.assertEqual({wait.caller for wait in claim_state.read_claim_waits(paths)}, {2, 7, 8})
                    processes[8] = process_start.Gone()
                    self.assertEqual({wait.caller for wait in claim_state.read_claim_waits(paths)}, {2, 7})
                    self.assertEqual(len(list(paths.claim_waits.iterdir())), 2)
                reply = wire.parse_reply(wire.encode_reply(wire.ViewReply(queue)))
                self.assertIsInstance(reply, wire.ViewReply)
                assert isinstance(reply, wire.ViewReply)
                current = limits_view.with_mac_claim_waits(reply.reading, (mac, hana))
                self.assertEqual(len(current.view.waiting), len(waiting))
                self.assertEqual(current.view.waiting[1].episode, "mac-episode")
                self.assertEqual(current.view.waiting[1].placement_block,
                                 records.PlacementBlock("mac", "mac", "busy", "mac-episode"))
                self.assertEqual(current.view.waiting[10].episode, "other-episode")
                self.assertTrue(current.view.waiting[10].placement_pending)
                self.assertEqual(current.view.waiting[11].caller_start, "old")
                self.assertIsNone(current.view.waiting[11].placement_block)
                self.assertEqual(current.view.waiting[12].placement_block, records.PlacementBlock("mac", "mac", "busy"))
                cloud_names = {cloud_owner: "cloud wait"}
                visible = limits_view.visible_waits(queue_view.QueueModel(current, {}), {
                    1: "local wait", 2: "mac wait", 3: "disk wait", 4: "hana wait", 5: "other repo",
                    6: "Mac disk wait", 7: "expired", 9: "duplicate", 10: "pending", 11: "other episode",
                    12: "old caller", 13: "direct wait", 14: "queued step",
                }, cloud_names)
                self.assertCountEqual([row.name for row in visible],
                    ["local wait", "mac wait", "disk wait", "hana wait", "other repo", "Mac disk wait",
                     "expired", "pending", "other episode", "old caller", "direct wait", "queued step", "cloud wait"])
                names = limits_view.QueueNames(visible, cloud_names, {})
                memory = limits_view.Metric("Memory for builds", 7, 12, "GiB")
                folders = conf.read("build_folders")
                status = queue_view.mac_reading_status(activity, now)
                self.assertEqual(status, "")
                disk_free = queue_view.current_mac_disk_bytes(activity, status)
                assert disk_free is not None
                result = limits_view.build_limits(current, (mac, hana), names, memory, folders,
                                                  stale_saved, UTC, (), disk_free / records.GIB)
                for stale in (replace(activity, error="fixture SSH error"), replace(activity, read_at=now - 21)):
                    status = queue_view.mac_reading_status(stale, now)
                    self.assertTrue(status)
                    self.assertIsNone(queue_view.current_mac_disk_bytes(stale, status))
                    unavailable = limits_view.build_limits(current, (mac, hana), names, memory, folders,
                                                           saved, UTC, ())
                    self.assertEqual(unavailable.mac.shared, (limits_view.Metric(
                        "Free disk", None, 100, "GiB", "minimum"),))
            self.assertEqual(claim_state.read_claim_waits(paths), ())
            self.assertTrue(result.history_stale)
            self.assertFalse(result.history_unavailable)
            rows = {row.name: row for row in result.repositories}
            self.assertEqual(set(rows), {"claude_commands", "quiet", "workspace", "hana"})
            self.assertEqual((rows["claude_commands"].natedev.runs, rows["claude_commands"].natedev.exempt),
                             (["regular"], ["merge-owner"]))
            self.assertEqual(rows["workspace"].mac.budgets[0].limit, 27)
            self.assertEqual(rows["claude_commands"].mac.budgets[0].limit, 18)
            self.assertEqual(result.mac.state, f"Held until {until:%H:%M UTC}")
            self.assertEqual(result.natedev.shared[-1], limits_view.Metric("Free disk", 20, 31, "GiB", "minimum"))
            self.assertEqual(result.aws.shared[0], limits_view.Metric("Cloud gates", 0, 4))
            self.assertEqual(rows["claude_commands"].natedev.waits, {"Run limit full": ["local wait", "other repo"]})
            self.assertEqual(result.natedev.waits, {"Disk floor": ["disk wait"], "Memory": ["old caller"]})
            self.assertEqual(rows["workspace"].mac.waits, {"Limit full": ["mac wait", "direct wait"]})
            self.assertEqual(result.mac.shared, (limits_view.Metric("Free disk", 80, 100, "GiB", "minimum"),))
            self.assertEqual(result.mac.waits, {"Disk floor": ["Mac disk wait"]})
            self.assertEqual(rows["workspace"].natedev.waits,
                             {"Evidence pending": ["expired"], "Behind older check in the queue": ["queued step"]})
            self.assertEqual(rows["workspace"].unplaced, {"Evidence pending": ["pending", "other episode"]})
            self.assertEqual(rows["hana"].aws.waits, {"Memory": ["hana wait"]})
            self.assertFalse(rows["quiet"].aws.available)
            self.assertEqual(result.aws.waits, {"Gate limit full": ["cloud wait"]})

        self.check_placement_replies()

    def check_placement_replies(self) -> None:
        with test_placement.PlacementFixture() as fixture:
            request = test_placement.placement_request(fixture)
            target = placement.PlacementDestination("ssh", "aws", "fixture-aws",
                                                    claim_state.RepositoryScope("hana"))
            request = replace(request, local=wire.LocalAdmissionUnavailable("fixture"),
                constraints=replace(request.constraints, candidates=(placement.PlacementCandidate(target),)))
            with mock.patch.object(fixture.server, "host_evidence_key") as unused_key:
                unused_key.side_effect = OSError("No provider key in this fixture")
                reply, = fixture.server.handle_request(request)
            self.assertIsInstance(reply, wire.PlacementWait)
            alternate = replace(target, target="mac", host="fixture-mac", provider="mac")
            memory = placement.CandidateEligibility(target, placement.TemporaryExclusion("memory"))
            disk = placement.CandidateEligibility(alternate, placement.TemporaryExclusion("disk"))
            fixture.server.placement_eligibility[request.episode] = (memory, disk)
            actual = fixture.server._placement_wait_reading(head_reading())  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(actual.view.waiting[-1].episode, request.episode)
            self.assertEqual(actual.view.waiting[-1].placement_block,
                             records.PlacementBlock("ssh", "aws", "memory", request.episode))
            reply = fixture.server._placement_reply(request, placement.ResourceHolderUnknown())  # pyright: ignore[reportPrivateUsage]
            assert isinstance(reply, wire.PlacementWait)
            self.assertEqual(reply.wait, wire.PlacementResourceWait("aws", "memory"))
            fixture.server.placement_eligibility[request.episode] = (
                placement.CandidateEligibility(target, placement.EvidenceNeeded("no reading")), disk)
            pending = fixture.server._placement_wait_reading(head_reading())  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(pending.view.waiting[-1].episode, request.episode)
            self.assertTrue(pending.view.waiting[-1].placement_pending)
            self.assertIsNone(pending.view.waiting[-1].placement_block)
            decoded = wire.parse_reply(wire.encode_reply(wire.ViewReply(pending)))
            assert isinstance(decoded, wire.ViewReply)
            self.assertEqual(decoded.reading.ordered[-1][1].get("placement_episode"), request.episode)
            mac_only = replace(request, constraints=replace(request.constraints, selection=placement.MacOnlyHost()))
            fixture.server.placement_requests[request.episode] = mac_only
            reply = fixture.server._placement_reply(mac_only, placement.ResourceHolderUnknown())  # pyright: ignore[reportPrivateUsage]
            assert isinstance(reply, wire.DispatchStatus)
            self.assertEqual(reply.state, placement.ClosedAssignment("disk"))
            for reading in (head_reading(), pending):
                filtered = fixture.server._placement_wait_reading(reading)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(filtered.view.waiting, head_reading().view.waiting)
                self.assertEqual(filtered.ordered, head_reading().ordered)

        self.check_older_placement()

    def check_older_placement(self) -> None:
        with (test_placement.PlacementFixture() as fixture,
              mock.patch.object(admission_service, "HostEvidenceWorker", test_placement.ManualEvidenceWorker)):
            order = test_placement.MacOrderTests().mac_order(fixture)
            initial, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(initial, wire.ViewReply)
            blocks = {step.episode: step.placement_block for step in initial.reading.view.waiting}
            older = blocks[order.older.episode]
            younger = blocks[order.younger.episode]
            assert older is not None and younger is not None
            self.assertEqual(limits_view.placement_reason(older.reason), "Limit full")
            self.assertTrue(younger.reason.startswith("Behind "))
            self.assertTrue(younger.reason.endswith(" in the queue"))
            self.assertEqual(younger.target, "mac")
            reply = fixture.server._placement_reply(order.younger, placement.ResourceHolderUnknown())  # pyright: ignore[reportPrivateUsage]
            assert isinstance(reply, wire.PlacementWait)
            self.assertEqual(reply.wait, wire.PlacementResourceWait("mac", younger.reason))

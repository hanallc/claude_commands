"""Check the Build limits reading with private settings and queue records."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from threading import Event
from unittest import mock

from scripts.aws_target import hana_box, hana_placement_adapter
from scripts.build_queue import admission_protocol as wire, admission_records as records
from scripts.build_queue import execution_identity, limits_view, memory_feed, placement, queue_view
from scripts.build_queue import test_placement
from scripts.build_queue.cloud_progress import NoCloudProgress
from scripts.build_queue.progress_profile import NoCounter
from scripts.build_queue.queue_fixture import fixture_reservation, head_reading
from scripts.build_targets import target_config
from scripts.buildlog import charts, repository, store
from scripts.lib import conf
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


class BuildLimitsTests(unittest.TestCase):
    def test_limits_use_real_policy_shapes_and_keep_each_wait_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as index:
            root = Path(directory)
            workspace = root / "workspace"
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
            claude_checkout = root / ".claude"
            claude_checkout.mkdir()
            git(claude_checkout, "init", "-q")
            git(claude_checkout, "remote", "add", "origin", "https://example.invalid/claude_commands")
            _ = history.origins.resolve("", str(claude_checkout), read_checkout=True)
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

            _ = (root / "build_admission.conf").write_text(
                "[disk]\n# whole: 1..\nfree_floor_gib=31\n[]\n# whole: 1..\ncloud_gate_slots=4\n")
            _ = (root / "cloud_gate.conf").write_text("enabled=on\n")
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
                    route=target_config.MAC_ONLY),
                records.WaitingStep(3, "", "", "check", str(clone), now, 0, records.BuildSpaceLow(20, 31)),
                records.WaitingStep(6, "", "", "check", "", now, 0, records.ReadyForAdmission(),
                    placement_block=records.PlacementBlock("ssh", "hana-aws", "memory", "hana-episode")),
                records.WaitingStep(9, "", "", "check", str(worktree), now, 0, limits),
                records.WaitingStep(11, "", "", "check", str(worktree), now, 0, records.EstimateExpired(now)),
                records.WaitingStep(12, "", "", "", str(worktree), now, 0, records.ReadyForAdmission()),
                records.WaitingStep(1, "", "", "gate", "", now, 0, limits, project="claude_commands"),
                records.WaitingStep(13, "", "", "check", str(worktree), now, 0, records.NeedsMemory(),
                    placement_pending=True),
                records.WaitingStep(14, "", "", "check", str(worktree), now, 0, records.BehindHead("older check", now, "")),
            )
            explanation = replace(baseline, policy=policy, decided_from=replace(baseline.decided_from,
                reservations=(reserve, merge), build_space=records.BuildSpaceLow(20, 31)))
            view = replace(reading.view, waiting=waiting)
            cloud_owner = wire.Owner(4, "4")
            cloud = wire.CloudRow(cloud_owner, wire.Identity("", "", "gate", "", "cloud"),
                                  wire.CloudPlace("aws"), now, 0, NoCloudProgress(), NoCounter(), wire.CloudSlotWait(4, 4))
            slot = wire.RepositorySlotRow(wire.Owner(5, "5"), wire.Identity("", "", "check", str(worktree), "slot"),
                now, "natedev", wire.RepositorySlotWaiting("workspace", (1,), 1, "fixture"))
            repository_keys = {1: "https://example.invalid/claude_commands", 6: "https://example.invalid/hana"}
            order = [(root / str(step.pid), records.RankedWaiter(pid=step.pid, start_time=str(step.pid),
                        since=step.since, need=step.need, folder=step.folder,
                        repo_identity=repository_keys.get(step.pid, ""), order=records.NormalOrder()))
                     for step in waiting]
            queue = wire.CloudQueueRead(view, explanation, order, cloud=(cloud,), cloud_slots=4,
                                        cloud_gate_stale_pushes=1, repository_slots=(slot,))

            cloud_names = {cloud_owner: "cloud wait"}
            local_names = dict(enumerate((
                "local wait", "Mac only", "disk wait", "AWS wait", "other checkout",
                "expired", "ready", "duplicate", "pending", "queued step"), 1))
            visible = limits_view.visible_waits(queue_view.QueueModel(queue, {}), local_names, cloud_names)
            self.assertCountEqual([item.name for item in visible], [
                "local wait", "Mac only", "disk wait", "AWS wait", "other checkout",
                "expired", "ready", "pending", "queued step", "cloud wait"])
            names = limits_view.QueueNames(visible, cloud_names, {})
            memory = limits_view.Metric("Memory for builds", 7, 12, "GiB")
            with (mock.patch.dict(os.environ, {"CONF_DIR": directory}),
                  mock.patch.object(limits_view, "aws_placement_targets", return_value=frozenset({"hana-aws"}))):
                folders = conf.read("build_folders")
                result = limits_view.build_limits(queue, (), names, memory, folders, stale_saved, UTC)
                self.check_exempt_run(result)
                other_origin = "https://other.invalid/team/claude_commands"
                other_run: records.Reservation = {**merge,
                    "queue_waiter": {**merge["queue_waiter"], "repo_identity": other_origin}}
                shared_queue = replace(queue, explanation=replace(explanation,
                    decided_from=replace(explanation.decided_from, reservations=(reserve, other_run))))
                shared = limits_view.build_limits(shared_queue, (), names, memory, folders, saved, UTC)
                same_limit = [row.natedev for row in shared.repositories if row.name.endswith("/claude_commands")]
                self.assertEqual([(cell.used, cell.limit, cell.note) for cell in same_limit],
                                 [(2, 2, "Shared claude_commands run limit")] * 2)

            self.assertTrue(result.history_stale)
            self.assertFalse(result.history_unavailable)
            rows = {row.name: row for row in result.repositories}
            self.assertEqual(set(rows), {"claude_commands", "quiet", "workspace", "hana"})
            self.assertEqual(result.natedev.shared[-1], limits_view.Metric("Free disk", 20, 31, "GiB", "minimum"))
            self.assertEqual(result.aws.shared[0], limits_view.Metric("Cloud gates", 0, 4))
            self.assertEqual(rows["claude_commands"].natedev.waits,
                             {"Run limit full": ["local wait", "other checkout"]})
            self.assertEqual(result.natedev.waits, {"Disk floor": ["disk wait"]})
            self.assertEqual(rows["workspace"].natedev.waits,
                             {"Evidence pending": ["expired"], "Behind older check in the queue": ["queued step"]})
            self.assertEqual(rows["workspace"].unplaced, {"Evidence pending": ["ready", "pending"]})
            self.assertEqual(rows["hana"].aws.waits, {"Memory": ["AWS wait"]})
            self.assertFalse(rows["quiet"].aws.available)
            self.assertEqual(result.aws.waits, {"Gate limit full": ["cloud wait"]})

        self.check_claim_during_remote_start_hold()
        self.check_placement_replies()

    def check_exempt_run(self, result: limits_view.BuildLimits) -> None:
        cell = next(row.natedev for row in result.repositories if row.name == "claude_commands")
        self.assertEqual((cell.used, cell.limit), (2, 2))
        self.assertEqual(cell.runs, ["regular"])
        self.assertEqual(cell.exempt, ["merge-owner"])

    def check_claim_during_remote_start_hold(self) -> None:
        with test_placement.PlacementFixture() as fixture:
            fixture.configure(remote_start_hold_seconds=60)
            request, transport = test_placement.hana_policy_request(fixture)
            repo = test_placement.HANA_REPOSITORY
            assert isinstance(request.profile, records.StepProfile)
            request = replace(request, profile=replace(request.profile, repo="hana",
                repository=records.RepositoryIdentity(repo, "hana")), constraints=replace(request.constraints,
                candidates=(*request.constraints.candidates, test_placement.local_candidate(fixture))))
            origin = limits_view.recorded_origin(repo)
            reading_at = int(fixture.clock.now())
            history = limits_view.RecentRepositories({origin: {"aws"}}, {})
            money = limits_view.Metric("Money today", 2, 5, "USD")
            saved = limits_view.Background(history, money, money, origins=history.origins)
            _ = test_placement.publish_candidate(fixture, request)
            assignment = test_placement.assignment_for(fixture, request)
            claim = test_placement.adopt_assignment(fixture, request, assignment)
            holder = test_placement.coordinator_holder(assignment)
            adopted = placement.AdoptedProviderClaim(claim, holder, assignment.session, transport.host, "host-generation-a")
            with mock.patch.object(hana_placement_adapter, "read_assigned_adoption", return_value=adopted):
                reply, = fixture.server.handle_request(test_placement.consume_permission("authorized", assignment, claim))
            self.assertEqual(reply, wire.FirstAuthorization(assignment, claim))
            owner = records.run_owner(request.owner.pid, request.owner.start_time)

            def view() -> records.QueueRead:
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                return reply.reading

            reading = view()
            self.assertIn(owner, reading.explanation.decided_from.pending_remote_starts)
            step, = (step for step in reading.view.waiting if step.episode == request.episode)
            self.assertEqual(step.placement_block, records.PlacementBlock("ssh", "hana-aws", "starting", request.episode))
            position = reading.view.waiting.index(step) + 1
            waits = limits_view.visible_waits(queue_view.QueueModel(reading, {}), {position: "Starting step"}, {})
            lifecycle = hana_box.HanaLifecycleEvidence(hana_box.DeploymentIdentity("d", "i", "s", "r"),
                hana_box.Lifecycle.STARTING, hana_box.BootUnverified(), hana_box.NoOperation(),
                hana_box.NoDeliveredPermit(), (hana_box.WorkClaim("execution", claim.claim_id, reading_at),), reading_at)
            route = limits_view.HanaRoute(repo, transport.host, 1, lifecycle)
            with (mock.patch.object(limits_view, "hana_route", return_value=route),
                  mock.patch.object(limits_view, "aws_placement_targets", return_value=frozenset({"hana-aws"}))):
                result = limits_view.build_limits(reading, (), limits_view.QueueNames(waits, {}, {}), memory=money,
                                                 folders=conf.read("build_folders"), saved=saved, zone=UTC)
            cell = next(row.aws for row in result.repositories if row.name == "hana")
            self.assertEqual((cell.used, cell.limit, cell.runs, cell.waits),
                             (1, 1, [], {"Starting": ["Starting step"]}))

            evidence = execution_identity.ObservedExecution(
                execution_identity.ProcessExecution(transport.host, "host-generation-a", 700001, "start"))
            _ = fixture.server.handle_request(wire.ExecutionReport("started", assignment, claim,
                                                                  evidence, placement.AttemptRunning()))
            _ = fixture.run_pass()
            reading = view()
            self.assertNotIn(owner, reading.explanation.decided_from.pending_remote_starts)
            self.assertFalse(any(step.episode == request.episode for step in reading.view.waiting))

    def check_placement_replies(self) -> None:
        with test_placement.PlacementFixture() as fixture:
            request, _ = test_placement.hana_policy_request(fixture)
            request = replace(request, local=wire.LocalAdmissionUnavailable("fixture"))
            _ = test_placement.publish_candidate(fixture, request, placement.TemporaryExclusion("memory"))
            reply, = fixture.server.handle_request(request)
            assert isinstance(reply, wire.PlacementWait)
            self.assertIsInstance(reply.wait, wire.PlacementEvidenceWait)
            current, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(current, wire.ViewReply)
            decoded = wire.parse_reply(wire.encode_reply(current))
            assert isinstance(decoded, wire.ViewReply)
            step, = (step for step in decoded.reading.view.waiting if step.episode == request.episode)
            self.assertEqual(step.placement_block,
                             records.PlacementBlock("ssh", "hana-aws", "memory", request.episode))

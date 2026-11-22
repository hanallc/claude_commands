"""Check the Build limits reading with private settings and queue records."""
from __future__ import annotations

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

from scripts.aws_target import hana_box, hana_placement, hana_placement_adapter
from scripts.build_queue import admission_protocol as wire, admission_records as records, admission_service as service
from scripts.build_queue import execution_identity, limits_view, memory_feed, placement, queue_view
from scripts.build_queue import test_placement
from scripts.build_queue.cloud_progress import NoCloudProgress
from scripts.build_queue.progress_profile import NoCounter
from scripts.build_queue.queue_fixture import fixture_reservation, head_reading
from scripts.build_targets import target_config
from scripts.buildlog import charts, repository, store
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit as gate
from scripts.mac_test import claim_state


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
            _ = index.execute("CREATE TABLE steps (repo_identity TEXT, repo_path TEXT, target TEXT, host TEXT, started_at TEXT, repo TEXT)")
            reading = head_reading()
            now = reading.explanation.now
            _ = index.executemany("INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?)", [
                ("https://example.invalid/quiet", "", "natedev", "", store.utc_iso(now - 29 * 86400), "quiet-policy"),
                ("https://example.invalid/old", "", "natedev", "", store.utc_iso(now - 31 * 86400), "old"),
                ("https://example.invalid/claude_commands", "", "aws", "", store.utc_iso(now), ""),
                ("https://example.invalid/hana", "", "aws", "", store.utc_iso(now), ""),
                ("git-root:workspace", str(clone), "natedev", "", store.utc_iso(now), ""),
                ("git-root:workspace", str(worktree), "natedev", "", store.utc_iso(now), ""),
                ("https://example.invalid/old-team/workspace", str(old_checkout), "mac", "", store.utc_iso(now), ""),
                ("https://example.invalid/old-team/workspace", str(workspace), "mac", "", store.utc_iso(now), ""),
                ("https://example.invalid/team/workspace", str(workspace), "natedev", "", store.utc_iso(now), ""),
                ("git-root:fixture", str(fixture), "natedev", "", store.utc_iso(now), ""),
                ("git-root:missing", str(root / "missing"), "natedev", "", store.utc_iso(now), ""),
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
                             origins=limits_view.Origins(), aws_money=saved.aws_money)
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

            baseline = reading.explanation
            policy = replace(baseline.policy,
                repository_runs=records.RepositoryRunLimits({"natedev.claude_commands": 2, "natedev.quiet-policy": 3}, "merge-owner"),
                gpu_tests=records.GpuTestSlots(3, frozenset({"workspace"})))
            reserve: records.Reservation = {**fixture_reservation(101, 0, step="gate"),
                "repo": "claude_commands", "queue_waiter": {"pid": 101, "start_time": "101", "since": now,
                    "need": 0, "unit": "regular", "repo_identity": "https://example.invalid/claude_commands"}}
            merge: records.Reservation = {**reserve, "pid": 102,
                "queue_waiter": {**reserve["queue_waiter"], "pid": 102, "unit": "merge-owner"}}
            limits = records.RunLimitWait(2, 2, "claude_commands", "natedev")
            primary = "https://example.invalid/claude_commands"
            other_origin = "https://other.invalid/team/claude_commands"
            hana_origin = "https://example.invalid/hana"
            waiting = (
                records.WaitingStep(1, "", "", "gate", "", now, 0, limits, project="claude_commands"),
                records.WaitingStep(3, "", "", "check", str(clone), now, 0, records.BuildSpaceLow(20, 31)),
                records.WaitingStep(6, "", "", "check", "", now, 0, records.ReadyForAdmission(),
                    route=target_config.PinnedTarget("hana-gpu")),
                records.WaitingStep(9, "", "", "check", str(worktree), now, 0, limits),
                records.WaitingStep(11, "", "", "check", str(worktree), now, 0, records.EstimateExpired(now)),
                records.WaitingStep(12, "", "", "check", str(worktree), now, 0, records.ReadyForAdmission()),
                records.WaitingStep(1, "", "", "gate", "", now, 0, limits, project="claude_commands"),
                records.WaitingStep(13, "", "", "check", str(worktree), now, 0, records.NeedsMemory()),
                records.WaitingStep(14, "", "", "check", str(worktree), now, 0, records.StartSpacingWait(now)),
                records.WaitingStep(15, "", "", "check", str(worktree), now, 0, limits),
            )
            explanation = replace(baseline, policy=policy, decided_from=replace(baseline.decided_from,
                reservations=(reserve, merge), build_space=records.NoBuildSpaceHold()))
            view = replace(reading.view, waiting=waiting)
            cloud_owner = wire.Owner(4, "4")
            cloud = wire.CloudRow(cloud_owner, wire.Identity("", "", "gate", "", ""), wire.CloudPlace("aws"),
                now, 0, NoCloudProgress(), NoCounter(), wire.CloudSlotWait(2, 4))
            repository_keys = {1: primary, 6: hana_origin, 9: other_origin}
            order = [(root / str(step.pid), records.RankedWaiter(pid=step.pid, start_time=str(step.pid),
                        since=step.since, need=step.need, folder=step.folder,
                        repo_identity=repository_keys.get(step.pid, ""), order=records.NormalOrder()))
                     for step in waiting]
            queue = wire.CloudQueueRead(view, explanation, order, cloud=(cloud,), cloud_slots=4,
                                        cloud_gate_stale_pushes=1)
            facts = records.LimitsFacts(now, tuple(records.LimitsWait(step, position,
                records.RepositoryIdentity(repository_keys.get(step.pid, ""), limits.repository if step.reason == limits else ""),
                (records.PlacementBlock("ssh", "hana-gpu", "memory"),) if step.pid == 6 else (), pending=step.pid == 13)
                for position, step in enumerate(waiting, 1)),
                (records.LimitsCloud(4, "4", primary, "Group needs more free slots"),), 4, 31, 95,
                frozenset({"hana-gpu"}), frozenset())

            cloud_names = {cloud_owner: "cloud wait"}
            local_names = dict(enumerate((
                "local wait", "disk wait", "AWS wait", "other checkout", "expired",
                "ready", "duplicate", "pending", "start spacing", "unknown origin"), 1))
            visible = limits_view.visible_waits(queue_view.QueueModel(queue, {}), local_names, cloud_names)
            names = limits_view.QueueNames(visible, cloud_names, {})
            memory = limits_view.Metric("Memory for builds", 7, 12, "GiB")
            result = limits_view.build_limits(queue, (), names, memory, facts, stale_saved, UTC)
            self.check_exempt_run(result)
            self.assertTrue(result.history_stale)
            self.assertFalse(result.history_unavailable)
            rows = {row.name: row for row in result.repositories}
            self.assertEqual(rows["quiet"].natedev.limit, 3)
            self.assertIsNone(rows["quiet"].aws.available)
            self.assertIsNone(rows["quiet"].aws.used)
            self.assertEqual(result.natedev.shared[-1], limits_view.Metric("Free disk", 95, 31, "GiB", "minimum"))
            self.assertEqual(result.aws.shared[0], limits_view.Metric("Cloud gates", 0, 4))
            same_limit = [row.natedev for row in result.repositories if row.name.endswith("/claude_commands")]
            self.assertEqual([(cell.used, cell.limit, cell.note) for cell in same_limit],
                             [(2, 2, "Shared claude_commands run limit")] * 2)
            self.assertEqual(rows[primary.removeprefix("https://")].natedev.waits, {"Run limit full": ["local wait"]})
            self.assertEqual(rows[other_origin.removeprefix("https://")].natedev.waits, {"Run limit full": ["other checkout"]})
            self.assertEqual(result.natedev.waits, {"Disk floor": ["disk wait"], "Waiting for start spacing": ["start spacing"],
                                                   "claude_commands run limit full": ["unknown origin"]})
            self.assertEqual(rows["workspace"].natedev.waits,
                             {"Evidence pending": ["expired"], "Ready for admission": ["ready"]})
            self.assertEqual(rows["workspace"].unplaced, {"Evidence pending": ["pending"]})
            self.assertEqual(rows["hana"].aws.waits, {"Memory": ["AWS wait"]})
            self.assertEqual(result.aws.waits, {"Group needs more free slots": ["cloud wait"]})

        self.check_claim_during_remote_start_hold()
        self.check_placement_replies()
        self.check_disk_reading()

    def check_exempt_run(self, result: limits_view.BuildLimits) -> None:
        cell = next(row.natedev for row in result.repositories if row.name == "example.invalid/claude_commands")
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
            policy = hana_placement.EnabledHanaRoute(repo, ("check",), 1, 100)
            lifecycle = hana_box.HanaLifecycleEvidence(hana_box.DeploymentIdentity("d", "i", "s", "r"),
                hana_box.Lifecycle.STARTING, hana_box.BootUnverified(), hana_box.NoOperation(),
                hana_box.NoDeliveredPermit(), (), reading_at)
            target_claim: claim_state.RemoteJobClaim = {
                "claim_id": claim.claim_id, "episode": claim.episode, "attempt": claim.attempt,
                "remote_host": transport.host, "submitting_session": "", "pid": holder.pid,
                "proc_start": holder.start_time, "what": "check", "since": "", "repo_name": "hana", "repo_key": repo,
                "worktree": request.profile.worktree,
            }
            held = [claim_state.HeldClaim(claim.scope, fixture.root / "claim", target_claim)]
            capacity = hana_placement_adapter.hana_capacity(policy, lifecycle, held)
            self.assertEqual(capacity.claim_ids, frozenset({claim.claim_id}))
            active = replace(lifecycle, claims=(hana_box.WorkClaim(claim.claim_id, "token", reading_at, active=True),))
            self.assertEqual(hana_placement_adapter.hana_capacity(policy, active, held), capacity)
            self.assertEqual(hana_placement_adapter.hana_capacity(policy, active, []), capacity)
            published = test_placement.publish_candidate(fixture, request, claims=(adopted,))
            published = replace(published, hana=capacity)
            fixture.provider_evidence[published.key] = published
            fixture.server.publish_host_evidence(published)
            _ = fixture.run_pass()

            def view() -> records.QueueRead:
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                return reply.reading

            def report() -> limits_view.Cell:
                reading = view()
                facts = limits_view.read_facts(fixture.state_dir, reading.explanation.now)
                assert facts is not None
                rests = {wait.position: "Starting step" for wait in facts.waits}
                waits = limits_view.visible_waits(queue_view.QueueModel(reading, {}), rests, {})
                result = limits_view.build_limits(reading, (), limits_view.QueueNames(waits, {}, {}), money, facts, saved, UTC)
                return next(row.aws for row in result.repositories if row.name == "hana")

            reading = view()
            self.assertIn(owner, reading.explanation.decided_from.pending_remote_starts)
            cell = report()
            self.assertEqual((cell.used, cell.limit, cell.runs, cell.waits),
                             (1, 1, [], {"Starting": ["Starting step"]}))

            evidence = execution_identity.ObservedExecution(
                execution_identity.ProcessExecution(transport.host, "host-generation-a", 700001, "start"))
            _ = fixture.server.handle_request(wire.ExecutionReport("started", assignment, claim,
                                                                  evidence, placement.AttemptRunning()))
            _ = fixture.run_pass()
            reading = view()
            self.assertNotIn(owner, reading.explanation.decided_from.pending_remote_starts)
            cell = report()
            self.assertEqual(cell.waits, {})
            self.assertEqual(len(cell.runs), 1)

    def check_placement_replies(self) -> None:
        with test_placement.PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first = test_placement.local_cargo_request(fixture)
            second = fixture.request()
            _ = fixture.server.handle_request(first)
            _ = fixture.server.handle_request(second)
            inputs = fixture.server.last_inputs
            result = fixture.server.last_result
            policy = fixture.server.last_policy
            assert isinstance(inputs, records.QueueInputs) and isinstance(result, gate.PassResult)
            assert isinstance(policy, records.AdmissionPolicy)
            expected = gate.queue_read_from_pass(inputs, policy, result)
            for request, reply in (
                    (wire.ViewRequest(), wire.ViewReply(expected)),
                    (wire.MineRequest(first.identity.session_id), wire.MineReply(expected)),
                    (wire.ExplainRequest(), wire.ExplainReply(expected.explanation))):
                sent, = fixture.server.handle_request(request)
                self.assertEqual(wire.encode_reply(sent), wire.encode_reply(reply))
            waiting = fixture.server.waiters[second.episode].last_reply
            assert isinstance(waiting, wire.Wait)
            owner = records.run_owner(second.owner.pid, second.owner.start_time)
            position, decided = next((index, row) for index, row in enumerate(result.rows, 1) if row.owner == owner)
            order = decided.decision.order_hold
            assert isinstance(order, records.HeldBehindHead)
            ahead = records.waiter_owner(order.waiter)
            ahead_position = next(index for index, row in enumerate(result.rows, 1) if row.owner == ahead)
            head = result.policy.queued_head
            assert isinstance(head, gate.QueuedHead) and isinstance(inputs.host, records.HostMemory)
            label = service.wait_line_name(fixture.server.waiters[first.episode])
            snapshot = records.AdmissionWaitSnapshot(decided.decision, second.profile.peak, inputs.running,
                inputs.host.available, second.profile.repo, second.profile.step, policy, expected.diagnostics,
                head.waiter["need"], label, decided.reason, decided.holder)
            reference = wire.Wait(decided.reason, position, label, f"behind #{ahead_position}", snapshot)
            encoded = wire.encode_reply(waiting)
            self.assertEqual(encoded, wire.encode_reply(reference))
            self.assertNotIn("placement_episode", encoded)
            self.assertNotIn("limits_facts", encoded)

        with test_placement.PlacementFixture() as fixture:
            request, _ = test_placement.hana_policy_request(fixture)
            request = replace(request, local=wire.LocalAdmissionUnavailable("fixture"))
            observation = test_placement.publish_candidate(fixture, request, placement.TemporaryExclusion("memory"))
            reply, = fixture.server.handle_request(request)
            assert isinstance(reply, wire.PlacementWait)
            self.assertIsInstance(reply.wait, wire.PlacementEvidenceWait)
            current, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(current, wire.ViewReply)
            self.assertFalse(current.reading.view.waiting)
            facts = limits_view.read_facts(fixture.state_dir, current.reading.explanation.now)
            assert facts is not None
            wait, = facts.waits
            self.assertEqual(wait.blocks, (records.PlacementBlock("ssh", "hana-aws", "memory"),))
            self.assertFalse(wait.pending)

            server = fixture.server
            aws = observation.key.destination
            mac = replace(aws, provider="mac", target="mac")
            local = test_placement.local_candidate(fixture).destination
            blocked = placement.TemporaryExclusion("blocked")
            server.placement_eligibility[request.episode] = (
                placement.CandidateEligibility(local, blocked), placement.CandidateEligibility(aws, placement.EvidenceNeeded("pending")))
            wait, = self.published_facts(fixture).waits
            self.assertTrue(wait.pending)
            self.assertEqual(wait.blocks, ())

            server.placement_eligibility[request.episode] = (
                placement.CandidateEligibility(mac, blocked), placement.CandidateEligibility(aws, blocked))
            confirmed = self.published_facts(fixture)
            wait, = confirmed.waits
            self.assertEqual(limits_view.queue_wait(wait, records.ReadyForAdmission(), "natedev", confirmed),
                             ("aws", "Machine held"))
            self.check_pinned_target(fixture, request, aws)

            _ = server.current_assignments.pop(request.episode, None)
            assignment = placement.PlacementAssignment(request.episode, "ended", aws, request.constraints.source_key,
                                                       request.owner, request.constraints.session, request.since)
            server.assignments["ended"] = records.SavedPlacementAssignment(assignment, state=placement.ClosedAssignment("completed"))
            self.assertIn(request.episode, server.active_placement_waits)
            self.assertEqual(self.published_facts(fixture).waits, ())

    def published_facts(self, fixture: test_placement.PlacementFixture) -> records.LimitsFacts:
        fixture.server.pass_stale = True
        view, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(view, wire.ViewReply)
        facts = limits_view.read_facts(fixture.state_dir, view.reading.explanation.now)
        assert facts is not None
        return facts

    def check_pinned_target(self, fixture: test_placement.PlacementFixture,
                            request: wire.PlacementRequest, aws: placement.PlacementDestination) -> None:
        _ = (fixture.root / "build_targets.conf").write_text(
            "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\npinned_targets=hana-gpu\n"
            + "kind.hana-gpu=ssh\nplatform.hana-gpu=linux\nsettings.hana-gpu=gpu.conf\n")
        _ = (fixture.root / "gpu.conf").write_text("execution_transport=hana-gpu\n")
        gpu = replace(aws, target="hana-gpu")
        server = fixture.server
        server.placement_eligibility[request.episode] = (placement.CandidateEligibility(gpu, placement.TemporaryExclusion("busy")),)
        facts = self.published_facts(fixture)
        self.assertEqual(facts.aws_targets, frozenset({"hana-gpu"}))
        wait, = facts.waits
        self.assertEqual(limits_view.queue_wait(wait, records.ReadyForAdmission(), "natedev", facts), ("aws", "Limit full"))

    def check_disk_reading(self) -> None:
        with test_placement.PlacementFixture() as fixture:
            fixture.configure(free_floor_gib=30)
            proc = service.SystemReadings()
            reading = type("Disk", (), {"f_bavail": 64, "f_frsize": records.GIB})()
            with (mock.patch("scripts.lib.build_slots.slot_cargo_root", return_value=fixture.root),
                  mock.patch("os.statvfs", return_value=reading) as read_disk):
                self.assertIsInstance(proc.build_space(30), records.NoBuildSpaceHold)
            read_disk.assert_called_once_with(fixture.root)
            self.assertEqual(proc.free_disk, 64)

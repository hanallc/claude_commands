"""Check the Build limits section with published records and private fixture settings."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date
from pathlib import Path
from threading import Thread
from unittest import mock

from scripts.aws_target import hana_box, hana_placement
from scripts.build_queue import admission_protocol as wire, admission_records as records, limits_view, queue_view
from scripts.build_queue.admission_wire import ReservedCloudSlot
from scripts.build_queue.cloud_progress import NoCloudProgress
from scripts.build_queue.progress_profile import NoCounter
from scripts.build_queue.queue_fixture import fixture_reservation, head_reading
from scripts.build_queue.scope_activity import LiveBuildRun
from scripts.build_targets import target_config
from scripts.build_targets.execution_transport import HanaControlTransport
from scripts.buildlog import repository, store
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import claim_state


class BuildLimitsTests(unittest.TestCase):
    def test_limits_use_real_policy_shapes_and_keep_each_wait_once(self) -> None:
        reading = head_reading()
        now = reading.explanation.now
        primary = "https://example.invalid/team/project"
        hana = "https://example.invalid/team/hana"
        fork = "https://fork.invalid/team/project"
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as index:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            scratch = root / "scratch"
            copied = root / "copied"
            env = fixture_git_environment(root)

            def git(folder: Path, *args: str) -> None:
                _ = subprocess.run(["git", "-C", str(folder), *args], env=env, check=True,
                                   capture_output=True, timeout=TEST_HANG_GUARD_SECS)

            git(source, "init", "-q")
            git(source, "remote", "add", "origin", "git@example.invalid:team/project.git")
            _ = commit_repository(source, "Fixture", allow_empty=True, env=env)
            git(root, "clone", "-q", "--shared", str(source), str(scratch))
            git(root, "clone", "-q", "--shared", str(scratch), str(copied))
            git(scratch, "remote", "set-url", "origin", fork)
            _ = index.execute("CREATE TABLE steps (repo_identity TEXT, repo_path TEXT, started_at TEXT)")
            _ = index.executemany("INSERT INTO steps VALUES (?, ?, ?)", [
                (primary, "", store.utc_iso(now)),
                ("https://example.invalid/other/project", "", store.utc_iso(now)),
                (fork, str(source), store.utc_iso(now)),
                ("https://example.invalid/team/quiet", "", store.utc_iso(now - 29 * 86400)),
                ("https://example.invalid/team/old", "", store.utc_iso(now - 31 * 86400)),
                (str(source), str(source), store.utc_iso(now)),
                ("unknown", str(scratch), store.utc_iso(now)),
                ("unknown", str(root / "missing"), store.utc_iso(now)),
            ])
            with mock.patch.object(repository, "output", wraps=repository.output) as git_read:
                history = limits_view.recent_steps(index, now)
                git_read.assert_not_called()
                origins = limits_view.Origins().refresh((str(scratch),))
                self.assertEqual(origins.resolve("", str(scratch)), fork)
                self.assertEqual(origins.resolve(primary, str(scratch)), primary)
                self.assertEqual(origins.refresh((str(scratch),)), origins)
                self.assertEqual(git_read.call_count, 1)
                git(scratch, "remote", "set-url", "origin", str(source))
                copies = (str(copied), str(scratch))
                origins = origins.refresh(copies)
                self.assertEqual(origins.resolve("", str(copied)), primary)
                self.assertEqual(origins.refresh(copies), origins)
                self.assertEqual(git_read.call_count, 4)
                config = source / ".git" / "config"
                original = config.stat()
                git(source, "remote", "set-url", "origin", fork)
                self.assertNotEqual(config.stat().st_ino, original.st_ino)
                os.utime(config, ns=(original.st_atime_ns, original.st_mtime_ns))
                self.assertEqual(origins.refresh(copies).resolve("", str(copied)), fork)
                self.assertEqual(git_read.call_count, 5)
        hidden_key = "/private/policy-key"
        policy = records.RepositoryRunLimits({
            "natedev.project": 1, f"natedev.{hidden_key}": 2, "natedev.unmatched": 1,
        }, "merge-owner")
        reservations: tuple[records.Reservation, ...] = (
            {**fixture_reservation(101, 0, step="gate"), "repo": "project", "repo_identity": primary, "unit": "regular"},
            {**fixture_reservation(102, 0, step="gate", folder=str(copied)), "repo": "project", "unit": "merge-owner"},
            {**fixture_reservation(103, 0, step="check", folder=str(root / "unknown")), "repo": "scratch"},
            {**fixture_reservation(104, 0, step="gate", folder=str(root / "unknown")), "repo": hidden_key},
        )
        explanation = replace(reading.explanation,
            policy=replace(reading.explanation.policy, repository_runs=policy),
            decided_from=replace(reading.explanation.decided_from, reservations=reservations, build_space=records.BuildSpaceLow(20, 31)))
        full = records.RunLimitWait(2, 1, "project", "natedev")
        waiting = (
            records.WaitingStep(1, "", "", "gate", "", now, 0, full),
            records.WaitingStep(2, "", "", "check", "", now, 0, records.BuildSpaceLow(20, 31)),
            records.WaitingStep(3, "", "", "test", "", now, 0, records.NeedsMemory()),
            records.WaitingStep(4, "", "", "check", "", now, 0, records.ReadingUnavailable(),
                                route=target_config.PinnedTarget("fixture-aws")),
            records.WaitingStep(5, "", "", "check", "", now, 0, records.ReadyForAdmission(), route=target_config.MAC_ONLY),
            records.WaitingStep(8, "", "", "test", "", now, 0, records.NeedsMemory()),
            records.WaitingStep(9, "", "", "gate", str(scratch), now, 0,
                                records.RunLimitWait(1, 2, hidden_key, "natedev")),
            records.WaitingStep(10, "", "", "check", "", now, 0, records.ReadyForAdmission(),
                                route=target_config.PinnedTarget("other-target")),
            records.WaitingStep(11, "", "", "gate", "", now, 0,
                                records.RunLimitWait(1, 1, "unmatched", "natedev")),
        )
        cloud = tuple(wire.CloudRow(wire.Owner(pid, str(pid)), wire.Identity("", "", "gate", "", ""), wire.CloudPlace("aws"),
            now, 0, NoCloudProgress(), NoCounter(), reason) for pid, reason in (
                (6, wire.CloudSlotWait(1, 4)),
                (7, wire.CloudSlotWait(1, 4, ReservedCloudSlot())),
            ))
        ranked: records.RankedWaiter = {"pid": 1, "start_time": "1", "since": now, "need": 0,
                                        "order": records.NormalOrder()}
        queue = wire.CloudQueueRead(replace(reading.view, waiting=waiting), explanation, [(Path("one"), ranked)],
                                    cloud=cloud, cloud_slots=4, cloud_gate_stale_pushes=1)
        names = {1: "Run limit", 2: "Disk wait", 3: "Memory wait", 4: "AWS target wait", 5: "Mac wait",
                 7: "/private/caller", 8: "Unplaced wait", 9: "Unbound wait"}
        cloud_names = {cloud[0].owner: "Cloud wait", cloud[1].owner: "Starting wait"}

        def show(current: wire.CloudQueueRead) -> limits_view.BuildLimits:
            waits = limits_view.visible_waits(queue_view.QueueModel(current, {}), names, cloud_names)
            return limits_view.build_limits(current, (), waits, cloud_names, {}, saved, UTC)

        saved = limits_view.Background(history, limits_view.Metric("money", 2, 5, "USD", "estimate"),
            cloud_slots=9, hana=(limits_view.HanaCapacity(hana, 1, limits_view.ReadingState.OMITTED),),
            aws_targets=frozenset({"fixture-aws"}), origins=origins)

        with mock.patch.object(repository, "output", side_effect=AssertionError("Git read in the feed tick")):
            shown = show(queue)
        self.assertEqual([(row.name, row.owner, row.quiet) for row in shown.repositories], [
            ("hana", "", False), ("project", "team · example.invalid", False), ("project", "other", True),
            ("project", "team · fork.invalid", True), ("quiet", "", True),
        ])
        hana_row, project_row, spare_project, fork_row, quiet = shown.repositories
        self.assertIsNone(fork_row.natedev.limit)
        cell = project_row.natedev
        self.assertEqual((cell.used, cell.limit, cell.runs, cell.merge_above), (2, 1, ["regular"], ["merge-owner"]))
        self.assertEqual(cell.waits, {"The run limit for project on natedev is full.": ["Run limit"]})
        self.assertIsNone(quiet.natedev.limit)
        self.assertEqual(spare_project.natedev.used, 0)
        self.assertEqual(shown.natedev.unattributed, 1)
        meters = {metric.label: (metric.used, metric.limit) for metric in shown.natedev.shared}
        self.assertEqual(meters["Repository run limit"], (1, 2))
        self.assertNotIn("unmatched run limit", meters)
        self.assertEqual(shown.natedev.waits, {
            "The repository run limit is full.": [""],
            "The run limit for unmatched on natedev is full.": ["Unbound wait"],
        })
        self.assertEqual((hana_row.aws.used, hana_row.aws.limit), (None, 1))
        self.assertEqual(next(metric for metric in shown.natedev.shared if metric.label == "disk").waits,
                         {"The build disk has less free space than the queue requires.": ["Disk wait"]})
        self.assertEqual(shown.natedev.shared[0].waits,
                         {"This run needs more free memory to start.": ["Memory wait", ""]})
        self.assertEqual(shown.aws.waits, {"The queue cannot check whether this run can start.": ["AWS target wait"]})
        self.assertEqual(shown.unplaced, {"The queue can start this run at its next check.": ["Unplaced wait"]})
        self.assertEqual((shown.aws.shared[0].used, shown.aws.shared[0].limit,
                          shown.aws.shared[0].reserved_waiting), (1, 4, 1))
        self.assertEqual(shown.aws.shared[0].waits, {
            "This run waits for a place on AWS.": ["Cloud wait"],
            "This run has a place on AWS and waits for its machine to start.": ["Starting wait"],
        })
        idle = limits_view.build_limits(reading, (), (), {}, {}, saved, UTC)
        self.assertEqual((idle.aws.shared[0].used, idle.aws.shared[0].limit), (0, 9))
        self.assertEqual(idle.aws.state, "")
        self.assertFalse(any(metric.label == "disk" for metric in idle.natedev.shared))
        copy_only = replace(queue,
            view=replace(queue.view, waiting=(replace(waiting[0], reason=replace(full, used=1)), *waiting[1:])),
            explanation=replace(explanation,
            decided_from=replace(explanation.decided_from, reservations=reservations[1:2])))
        copy_limits = show(copy_only)
        copy_cell = next(row.natedev for row in copy_limits.repositories if row.owner == "team · example.invalid")
        self.assertEqual((copy_cell.used, copy_cell.limit, copy_cell.runs), (1, 1, ["merge-owner"]))
        self.assertEqual(copy_cell.waits, cell.waits)
        self.assertEqual(copy_limits.natedev.waits, shown.natedev.waits)
        matching = replace(full, limit=2)
        within_limit = replace(queue,
            view=replace(queue.view, waiting=(replace(waiting[0], reason=matching), *waiting[1:])),
            explanation=replace(explanation, policy=replace(explanation.policy,
                repository_runs=records.RepositoryRunLimits({"natedev.project": 2}, "merge-owner"))))
        within = show(within_limit)
        merge_cell = next(row.natedev for row in within.repositories if row.owner == "team · example.invalid")
        self.assertEqual(merge_cell.merge_above, [])
        self.assertEqual(merge_cell.runs, ["regular", "merge-owner"])
        gpu = records.GpuTestSlots(1, frozenset({"gpu-project"}))
        conflicting_wait = replace(within_limit,
            view=replace(queue.view, waiting=(
                replace(waiting[0], reason=replace(matching, used=3)),
                waiting[1],
                replace(waiting[2], reason=records.GpuSlotWait(1, 1)),
                *waiting[3:],
            )),
            explanation=replace(within_limit.explanation, policy=replace(within_limit.explanation.policy, gpu_tests=gpu)))
        wait_limits = show(conflicting_wait)
        wait_cell = next(row.natedev for row in wait_limits.repositories if row.owner == "team · example.invalid")
        self.assertEqual(wait_cell.waits, cell.waits)
        self.assertIsNone(wait_cell.limit)
        self.assertEqual(wait_cell.used, 0)
        self.assertEqual(wait_cell.runs, [])
        gpu_meter = next(metric for metric in wait_limits.natedev.shared if metric.label == "gpu")
        self.assertEqual((gpu_meter.used, gpu_meter.limit, gpu_meter.waits),
                         (None, None, {"All GPU test places are in use.": ["Memory wait"]}))
        self.check_policy_evidence(queue, saved, reservations[0], primary, fork)
        self.check_hana_claim(hana)
        self.check_cache_expiry()

    def check_policy_evidence(self, queue: records.QueueRead, saved: limits_view.Background,
                              admitted: records.Reservation, primary: str, fork: str) -> None:
        root_key = "git-root:fixture"
        policy = records.RepositoryRunLimits({
            "natedev.project": 2, "natedev.other": 2, "natedev.project-local": 1, f"natedev.{root_key}": 1,
        }, "merge-owner")
        run = LiveBuildRun(501, "fixture", 0)
        first: records.Reservation = {**admitted, "build_run": run}
        unrelated: records.Reservation = {**first, "repo": "unrestricted", "repo_identity": fork}
        second_origin = "https://example.invalid/team/other"
        second: records.Reservation = {**first, "repo_identity": second_origin, "queue_waiter": {
            "pid": 502, "start_time": "502", "since": 0, "need": 0, "repository_name": "other",
        }}

        def show(*reservations: records.Reservation) -> limits_view.BuildLimits:
            reading = replace(queue, explanation=replace(queue.explanation,
                policy=replace(queue.explanation.policy, repository_runs=policy),
                decided_from=replace(queue.explanation.decided_from, reservations=reservations)))
            return limits_view.build_limits(reading, (), (), {}, {}, saved, UTC)

        def project(shown: limits_view.BuildLimits) -> limits_view.Cell:
            return next(row.natedev for row in shown.repositories if row.owner == "team · example.invalid")

        one_key = project(show(first, unrelated))
        self.assertEqual((one_key.used, one_key.limit), (1, 2))
        shown = show(first, unrelated, second)
        self.assertEqual((project(shown).used, project(shown).limit), (1, 2))
        other = next(row.natedev for row in shown.repositories if row.name == "other")
        self.assertEqual((other.used, other.limit), (1, 2))
        self.assertEqual(next(row.natedev.used for row in shown.repositories if row.owner == "team · fork.invalid"), 0)
        self.assertEqual(sum(row.natedev.other_used for row in shown.repositories), 0)
        self.assertEqual(shown.natedev.unattributed, 0)
        self.assertEqual(shown.natedev.shared[-1].label, "Repository run limit")
        self.assertEqual(limits_view.wait_text(records.RunLimitWait(1, 1, root_key, "natedev")),
                         "The repository run limit is full.")
        for origin in ("", fork):
            with self.subTest(origin=origin):
                conflicting: records.Reservation = {**fixture_reservation(503, 0, step="gate"), "repo": "project",
                                                    "repo_identity": origin}
                shown = show(first, conflicting)
                self.assertIsNone(project(shown).limit)
                meter = next(metric for metric in shown.natedev.shared if metric.label == "project run limit")
                self.assertEqual((meter.used, meter.limit), (2, 2))
                third: records.Reservation = {**fixture_reservation(504, 0, step="gate"), "repo": "project",
                                              "repo_identity": primary}
                without_exempt = show(first, conflicting, third)
                self.assertNotIn("project run limit", [metric.label for metric in without_exempt.natedev.shared])
                with_exempt = show(first, conflicting, {**third, "unit": "merge-owner"})
                meter = next(metric for metric in with_exempt.natedev.shared if metric.label == "project run limit")
                self.assertEqual((meter.used, meter.limit, meter.exempt_runs), (3, 2, 1))
        unrestricted = next(row.natedev for row in show({
            **first, "step": "check", "repo": "project-local", "repo_identity": "https://example.invalid/team/upstream",
        }).repositories if row.name == "upstream")
        self.assertEqual((unrestricted.used, unrestricted.limit, unrestricted.has_limit_key), (1, None, True))

    def check_cache_expiry(self) -> None:
        cache = limits_view.SourceCache[int]()
        today, tomorrow = date(2026, 1, 1), date(2026, 1, 2)

        def read() -> int:
            raise AssertionError("Run the refresh under fixture control")

        with mock.patch.object(Thread, "start"):
            self.assertIs(cache.current(0, 10, read, key=today), limits_view.ReadingState.OMITTED)
            result: Future[limits_view.SourceSample[int]] = Future()
            result.set_result(limits_view.SourceSample(5, 0, today))
            cache.pending = result
            self.assertEqual(cache.current(9, 10, read, key=today), 5)
            self.assertIs(cache.current(9, 10, read, key=tomorrow), limits_view.ReadingState.OMITTED)
            self.assertIs(cache.current(20, 10, read, key=today), limits_view.ReadingState.OMITTED)
            result = Future()
            result.set_result(limits_view.SourceSample(limits_view.ReadingState.UNAVAILABLE, 20, today))
            cache.pending = result
            self.assertIs(cache.current(21, 10, read, key=today), limits_view.ReadingState.UNAVAILABLE)

    def check_hana_claim(self, origin: str) -> None:
        transport = HanaControlTransport(Path("private-hana.conf"), "fixture-host")
        policy = hana_placement.EnabledHanaRoute(origin, ("check",), 1, 100)
        identity = hana_box.DeploymentIdentity("deployment", "instance", "request", "region")
        boot = hana_box.VerifiedBoot("boot", "token")
        generation = hana_placement.generation_text(hana_placement.HanaHostGeneration("deployment", "token", "boot"))
        lifecycle = hana_box.HanaLifecycleEvidence(identity, hana_box.Lifecycle.STARTING, boot, hana_box.NoOperation(),
            hana_box.NoDeliveredPermit(), (hana_box.WorkClaim("claim", "token", 10),), 10)
        record: claim_state.RemoteJobClaim = {"claim_id": "claim", "episode": "episode", "attempt": "attempt",
            "remote_host": transport.host, "submitting_session": "", "pid": 5, "proc_start": "start",
            "what": "check", "since": "", "repo_name": "hana", "repo_key": origin, "worktree": "",
            "instance_generation": generation}
        claim = claim_state.HeldClaim(claim_state.RepositoryScope("hana", origin, 1), Path("claim"), record)
        controller = mock.Mock(spec=hana_box.HanaController, read_saved_lifecycle=mock.Mock(return_value=lifecycle))
        with (mock.patch.object(hana_placement, "controller_for", return_value=controller),
              mock.patch.object(claim_state, "state_paths"),
              mock.patch.object(claim_state, "read_claims", side_effect=[(claim,), (claim,), (), ()])):
            self.assertEqual(limits_view.hana_use("fixture", transport, policy), 1)
            self.assertIs(limits_view.hana_use("fixture", transport, policy), limits_view.ReadingState.OMITTED)

        route = limits_view.HanaRoute("fixture", transport, policy)
        routes = limits_view.AwsRoutes((route,), frozenset({"fixture"}))
        refresh = limits_view.LimitsRefresh(10, 10)
        refresh.routes.sample = limits_view.SourceSample(routes, 19, None)
        refresh.hana_occupancy.sample = limits_view.SourceSample({route: 1}, 0, routes.hana)
        refresh.hana_occupancy.pending = Future()
        with mock.patch.object(Thread, "start"):
            shown = refresh.current(head_reading(), 20, 20)
        self.assertEqual(shown.aws_targets, routes.targets)
        self.assertEqual(shown.hana, (limits_view.HanaCapacity(origin, 1, limits_view.ReadingState.OMITTED),))

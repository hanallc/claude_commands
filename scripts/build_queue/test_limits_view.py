"""Check the Build limits section with published records and private fixture settings."""
from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from unittest import mock

from scripts.aws_target import hana_box, hana_placement
from scripts.build_queue import admission_protocol as wire, admission_records as records, limits_view, queue_view
from scripts.build_queue.admission_wire import ReservedCloudSlot
from scripts.build_queue.cloud_progress import NoCloudProgress
from scripts.build_queue.progress_profile import NoCounter
from scripts.build_queue.queue_fixture import fixture_reservation, head_reading
from scripts.build_targets import target_config
from scripts.build_targets.execution_transport import HanaControlTransport
from scripts.buildlog import store
from scripts.mac_test import claim_state


class BuildLimitsTests(unittest.TestCase):
    def test_limits_use_real_policy_shapes_and_keep_each_wait_once(self) -> None:
        reading = head_reading()
        now = reading.explanation.now
        primary = "https://example.invalid/team/project"
        hana = "https://example.invalid/team/hana"
        with closing(sqlite3.connect(":memory:")) as index:
            _ = index.execute("CREATE TABLE steps (repo_identity TEXT, repo_path TEXT, repo TEXT, started_at TEXT)")
            _ = index.executemany("INSERT INTO steps VALUES (?, ?, ?, ?)", [
                (primary, "", "project", store.utc_iso(now)),
                ("https://example.invalid/team/quiet", "", "quiet", store.utc_iso(now - 29 * 86400)),
                ("https://example.invalid/team/old", "", "old", store.utc_iso(now - 31 * 86400)),
                ("unknown", "", "Record without origin", store.utc_iso(now)),
            ])
            origins = limits_view.Origins()
            history = limits_view.recent_steps(index, now, origins)
        policy = records.RepositoryRunLimits({"natedev.project": 1, "natedev.other": 2}, "merge-owner")
        reservations = (
            {**fixture_reservation(101, 0, step="gate"), "repo": "project", "repo_identity": primary, "unit": "regular"},
            {**fixture_reservation(102, 0, step="gate"), "repo": "project", "repo_identity": primary, "unit": "merge-owner"},
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
        )
        cloud = tuple(wire.CloudRow(wire.Owner(pid, str(pid)), wire.Identity("", "", "gate", "", ""), wire.CloudPlace("aws"),
            now, 0, NoCloudProgress(), NoCounter(), reason) for pid, reason in (
                (6, wire.CloudSlotWait(1, 4)),
                (7, wire.CloudSlotWait(1, 4, ReservedCloudSlot())),
            ))
        ranked: records.RankedWaiter = {"pid": 1, "start_time": "1", "since": now, "need": 0,
                                        "repo_identity": primary, "order": records.NormalOrder()}
        queue = wire.CloudQueueRead(replace(reading.view, waiting=waiting), explanation, [(Path("one"), ranked)],
                                    cloud=cloud, cloud_slots=4, cloud_gate_stale_pushes=1)
        names = {1: "Run limit", 2: "Disk wait", 3: "Memory wait", 4: "AWS target wait", 5: "Unplaced wait"}
        cloud_names = {cloud[0].owner: "Cloud wait", cloud[1].owner: "Starting wait"}
        waits = limits_view.visible_waits(queue_view.QueueModel(queue, {}), names, cloud_names)
        saved = limits_view.Background(history, limits_view.Metric("money", 2, 5, "USD", "estimate"),
            cloud_slots=9, hana=(limits_view.HanaCapacity(hana, "fixture-host", 1, None),),
            aws_targets=frozenset({"fixture-aws"}), origins=origins)

        shown = limits_view.build_limits(queue, (), waits, cloud_names, {}, saved, UTC)
        rows = {row.name: row for row in shown.repositories}
        self.assertEqual(set(rows), {"project", "quiet", "Record without origin", "other (run limit)", "hana"})
        cell = rows["project"].natedev
        self.assertEqual((cell.used, cell.limit, cell.runs, cell.exempt), (2, 1, ["regular"], ["merge-owner"]))
        self.assertEqual(cell.waits, {"The run limit for project on natedev is full.": ["Run limit"]})
        self.assertIsNone(rows["quiet"].natedev.limit)
        self.assertEqual(rows["other (run limit)"].natedev.limit, 2)
        self.assertEqual((rows["hana"].aws.used, rows["hana"].aws.limit), (None, 1))
        self.assertEqual(shown.natedev.shared[-1].waits,
                         {"The build disk has less free space than the queue requires.": ["Disk wait"]})
        self.assertEqual(shown.natedev.shared[0].waits, {"This run needs more free memory to start.": ["Memory wait"]})
        self.assertEqual(shown.aws.waits, {"The queue cannot check whether this run can start.": ["AWS target wait"]})
        self.assertEqual(shown.unplaced, {"The queue can start this run at its next check.": ["Unplaced wait"]})
        self.assertEqual((shown.aws.shared[0].used, shown.aws.shared[0].limit), (1, 4))
        self.assertEqual(shown.aws.shared[0].waits, {
            "This run waits for a place on AWS.": ["Cloud wait"],
            "This run has a place on AWS and waits for its machine to start.": ["Starting wait"],
        })
        idle = limits_view.build_limits(reading, (), (), {}, {}, saved, UTC)
        self.assertEqual((idle.aws.shared[0].used, idle.aws.shared[0].limit), (0, 9))
        self.assertEqual(idle.aws.state, "State unavailable")
        self.assertIsNone(idle.natedev.shared[-1].used)
        self.check_hana_claim(hana)

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
              mock.patch.object(claim_state, "read_claims", side_effect=[(claim,), ()])):
            self.assertEqual(limits_view.hana_use("fixture", transport, policy), 1)
            self.assertIsNone(limits_view.hana_use("fixture", transport, policy))

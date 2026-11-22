"""Check the Build limits section with published records and private fixture settings."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from scripts.aws_target import hana_box, hana_placement
from scripts.build_queue import admission_protocol as wire, admission_records as records, limits_view, queue_view
from scripts.build_queue.admission_wire import ReservedCloudSlot
from scripts.build_queue.cloud_progress import NoCloudProgress
from scripts.build_queue.progress_profile import NoCounter
from scripts.build_queue.queue_fixture import fixture_reservation, head_reading
from scripts.build_targets import target_config, targets
from scripts.build_targets.execution_transport import HanaControlTransport
from scripts.buildlog import repository, store
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import claim_state, mac_test


class BuildLimitsTests(unittest.TestCase):
    def test_limits_use_real_policy_shapes_and_keep_each_wait_once(self) -> None:
        reading = head_reading()
        now = reading.explanation.now
        primary = "https://example.invalid/team/project"
        hana = "https://example.invalid/team/hana"
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as index:
            root = Path(directory)
            source = root / "project"
            source.mkdir()
            scratch = root / "scratch"
            env = fixture_git_environment(root)

            def git(folder: Path, *args: str) -> None:
                _ = subprocess.run(["git", "-C", str(folder), *args], env=env, check=True,
                                   capture_output=True, timeout=TEST_HANG_GUARD_SECS)

            git(source, "init", "-q")
            git(source, "remote", "add", "origin", "git@example.invalid:team/project.git")
            _ = commit_repository(source, "Fixture", allow_empty=True, env=env)
            git(root, "clone", "-q", "--shared", str(source), str(scratch))
            allowed = root / "mac-quiet"
            alias = root / "quiet-copy"
            denied = root / "local-only"
            for folder, name in ((allowed, "quiet"), (alias, "quiet"), (denied, "denied")):
                git(root, "init", "-q", str(folder))
                git(folder, "remote", "add", "origin", f"https://example.invalid/team/{name}.git")
            _ = index.execute("CREATE TABLE steps (repo_identity TEXT, repo_path TEXT, started_at TEXT)")
            _ = index.executemany("INSERT INTO steps VALUES (?, ?, ?)", [
                (primary, "", store.utc_iso(now)),
                ("https://example.invalid/other/project", "", store.utc_iso(now)),
                ("https://example.invalid/team/quiet", str(allowed), store.utc_iso(now - 29 * 86400)),
                ("https://example.invalid/team/quiet", str(alias), store.utc_iso(now - 29 * 86400)),
                ("https://example.invalid/team/denied", str(denied), store.utc_iso(now - 29 * 86400)),
                ("https://example.invalid/team/old", "", store.utc_iso(now - 31 * 86400)),
                (str(source), str(source), store.utc_iso(now)),
                ("unknown", str(scratch), store.utc_iso(now)),
                ("unknown", str(root / "missing"), store.utc_iso(now)),
            ])
            origins = limits_view.Origins()
            with mock.patch.object(repository, "output", wraps=repository.output) as git_read:
                history = limits_view.recent_steps(index, now, origins)
                self.assertEqual(git_read.call_count, 4)
            self.assertEqual(origins.folders[str(scratch)], primary)
            mac_snapshot = self.read_mac_fixture(root, scratch)
            mac = limits_view.read_mac((mac_snapshot,))
            self.assertIsNotNone(mac)
        policy = records.RepositoryRunLimits({"natedev.project": 1, "natedev.other": 2}, "merge-owner")
        reservations = (
            {**fixture_reservation(101, 0, step="gate"), "repo": "project", "repo_identity": primary, "unit": "regular"},
            {**fixture_reservation(102, 0, step="gate", folder=str(scratch)), "repo": "project", "unit": "merge-owner"},
            {**fixture_reservation(103, 0, step="check", folder=str(root / "unknown")), "repo": "scratch"},
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
            records.WaitingStep(5, "", "", "check", "", now, 0, records.ReadingUnavailable(), route=target_config.MAC_ONLY),
            records.WaitingStep(8, "", "", "test", str(scratch), now, 0, records.RunLimitWait(9, 9, "project", "mac"),
                                route=target_config.PinnedTarget("mac")),
            records.WaitingStep(9, "", "", "test", "", now, 0, records.ReadyForAdmission(),
                                route=target_config.PinnedTarget("other-mac")),
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
        names = {1: "Run limit", 2: "Disk wait", 3: "Memory wait", 4: "AWS target wait",
                 5: "Mac wait", 6: "Mac run limit", 7: "Unplaced wait"}
        cloud_names = {cloud[0].owner: "Cloud wait", cloud[1].owner: "Starting wait"}
        waits = limits_view.visible_waits(queue_view.QueueModel(queue, {}), names, cloud_names)
        saved = limits_view.Background(history, limits_view.Metric("money", 2, 5, "USD", "estimate"),
            cloud_slots=9, hana=(limits_view.HanaCapacity(hana, "fixture-host", 1, None),),
            aws_targets=frozenset({"fixture-aws"}), origins=origins)

        with mock.patch.object(repository, "output", side_effect=AssertionError("Git read in the feed tick")):
            shown = limits_view.build_limits(queue, (mac_snapshot,), waits, cloud_names,
                                             {"session": "Mac build"}, saved, UTC, mac=mac)
        self.assertEqual([(row.name, row.owner, row.quiet) for row in shown.repositories], [
            ("hana", "", False), ("project", "team", False), ("denied", "", True),
            ("project", "other", True), ("quiet", "", True),
        ])
        hana_row, project_row, denied_row, spare_project, quiet = shown.repositories
        cell = project_row.natedev
        self.assertEqual((cell.used, cell.limit, cell.runs, cell.merge_above), (2, 1, ["regular"], ["merge-owner"]))
        self.assertEqual(cell.waits, {"The run limit for project on natedev is full.": ["Run limit"]})
        self.assertIsNone(quiet.natedev.limit)
        self.assertEqual(spare_project.natedev.used, 0)
        self.assertEqual(shown.natedev.unattributed, 1)
        self.assertEqual(shown.natedev.shared[-1].label, "other run limit")
        self.assertEqual(shown.natedev.shared[-1].limit, 2)
        self.assertEqual((hana_row.aws.used, hana_row.aws.limit), (None, 1))
        self.assertEqual(shown.mac.state, "Held until 13:00 UTC")
        self.assertEqual(shown.mac.shared, ())
        self.assertEqual(shown.mac.unattributed, 1)
        self.assertEqual(shown.mac.waits, {"The queue cannot check whether this run can start.": ["Mac wait"]})
        self.assertEqual((project_row.mac.used, project_row.mac.limit, project_row.mac.runs), (1, 1, ["Mac build"]))
        self.assertEqual(project_row.mac.waits, {"The run limit for project on Mac is full.": ["Mac run limit"]})
        self.assertEqual((quiet.mac.used, quiet.mac.limit, quiet.mac.note), (0, 1, ""))
        self.assertEqual((denied_row.mac.limit, denied_row.mac.note), (None, "Does not run here"))
        self.assertIsNone(spare_project.mac.limit)
        self.assertEqual(spare_project.mac.note, "")
        self.assertEqual(next(metric for metric in shown.natedev.shared if metric.label == "disk").waits,
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
        self.assertEqual(idle.aws.state, "")
        self.assertIsNone(next(metric for metric in idle.natedev.shared if metric.label == "disk").used)
        within_limit = replace(queue, explanation=replace(explanation,
            policy=replace(explanation.policy, repository_runs=records.RepositoryRunLimits({"natedev.project": 2}, "merge-owner"))))
        within = limits_view.build_limits(within_limit, (), waits, cloud_names, {}, saved, UTC)
        merge_cell = next(row.natedev for row in within.repositories if row.owner == "team")
        self.assertEqual(merge_cell.merge_above, [])
        self.assertEqual(merge_cell.runs, ["regular", "merge-owner"])
        self.check_hana_claim(hana)

    def read_mac_fixture(self, root: Path, scratch: Path) -> targets.TargetSnapshot:
        settings = root / "mac.conf"
        _ = settings.write_text(
            "# range: 1..1000\nfree_floor_gib=47\nlinux_only.project=linux_package\nlinux_only.mac-quiet=\n",
            encoding="utf-8")
        _ = (root / "build_targets.conf").write_text(
            "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac.conf\n", encoding="utf-8")
        state = root / "state"
        claims = state / "claims"
        claims.mkdir(parents=True)
        since = datetime(2026, 11, 22, 12, tzinfo=UTC)
        block = claim_state.ActiveMacBlock("holder", "private reason", since, since + timedelta(hours=1),
            claim_state.HolderQueue("holder"), claim_state.NoShowrunner(), "not_configured",
            claim_state.FreeMessageNotNeeded())
        mac_test.write_block(state / "block.json", block)
        saved: claim_state.RemoteJobClaim = {"claim_id": "project-claim", "episode": "episode", "attempt": "attempt",
            "submitting_session": "session", "pid": 9, "proc_start": "", "what": "test", "worktree": str(scratch),
            "since": since.isoformat(), "repo_name": "project", "repo_key": "", "remote_host": "fixture-host"}
        mac_test.write_json(claims / "e5870be3.json", saved)
        mac_test.write_json(claims / "copied-record.json", saved)
        other: claim_state.RemoteJobClaim = {**saved, "claim_id": "whole-claim", "worktree": "private relative folder"}
        del other["repo_name"], other["repo_key"]
        mac_test.write_json(state / "run.json", other)
        dead: claim_state.ProcessBoundClaim = {"claim_id": "dead", "episode": "episode", "attempt": "attempt",
            "submitting_session": "", "pid": 2**30, "proc_start": "", "what": "check", "worktree": str(scratch),
            "since": since.isoformat(), "repo_name": "project", "repo_key": ""}
        mac_test.write_json(claims / "dead.json", dead)
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(root), "MAC_TEST_STATE_DIR": str(state)}),
              mock.patch.object(mac_test, "holder_name", return_value="holder"),
              mock.patch.object(targets, "utc_now", return_value=since)):
            snapshot = targets.ssh_target_snapshot(target_config.configured_ssh_target("mac"))
        self.assertIsInstance(snapshot.states[0], targets.Held, str(snapshot.states[0]))
        return snapshot

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

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from scripts.aws_target import hana_box, hana_placement
from scripts.build_queue import admission_protocol as wire, admission_records as records, limits_view, memory_feed
from scripts.build_queue import queue_view, target_availability
from scripts.build_queue.admission_wire import ReservedCloudSlot
from scripts.build_queue.cloud_progress import NoCloudProgress
from scripts.build_queue.progress_profile import NoCounter
from scripts.build_queue.queue_fixture import fixture_reservation, head_reading
from scripts.build_targets import target_config, targets
from scripts.build_targets.execution_transport import HanaControlTransport
from scripts.buildlog import repository, store
from scripts.lib import conf, process_start
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import claim_state, mac_test


class BuildLimitsTests(unittest.TestCase):
    def test_limits_use_real_policy_shapes_and_keep_each_wait_once(self) -> None:
        reading = head_reading()
        now = reading.explanation.now
        primary = "https://example.invalid/team/project"
        secondary = "https://example.invalid/other/project"
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
            other_project = root / "other" / "project"
            other_project.parent.mkdir()
            git(root, "init", "-q", str(other_project))
            git(other_project, "remote", "add", "origin", secondary)
            _ = index.execute("CREATE TABLE steps (repo_identity TEXT, repo_path TEXT, started_at TEXT)")
            _ = index.executemany("INSERT INTO steps VALUES (?, ?, ?)", [
                (primary, "", store.utc_iso(now)),
                (secondary, str(other_project), store.utc_iso(now)),
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
                self.assertEqual(git_read.call_count, 6)
            self.assertEqual(origins.resolve("", str(scratch)), primary)
            mac_snapshot, mac, whole_mac, bad_block, bad_claims = self.read_mac_fixture(root, scratch, origins)
            assert mac is not None
            assert whole_mac is not None
            assert bad_block is not None
            assert bad_claims is not None
            worktree_config = scratch / ".git" / "config"
            config_stamp = worktree_config.stat()
            prior = limits_view.Origins()
            self.assertEqual(prior.read(str(scratch), origins), primary)
            git(scratch, "remote", "set-url", "origin", secondary)
            os.utime(worktree_config, ns=(config_stamp.st_atime_ns, config_stamp.st_mtime_ns))
            self.assertNotEqual(worktree_config.stat().st_ino, config_stamp.st_ino)
            updated = limits_view.Origins()
            changed_mac = limits_view.read_mac((mac_snapshot,), prior)
            assert changed_mac is not None
            self.assertIn(secondary, [claim.origin for claim in changed_mac.claims])
            self.assertEqual(updated.read(str(scratch), prior), secondary)
            git(scratch, "remote", "remove", "origin")
            no_origin = limits_view.read_mac((mac_snapshot,), updated)
            assert no_origin is not None
            self.assertCountEqual([claim.origin for claim in no_origin.claims],
                                  ["", "https://example.invalid/team/cleanup"])
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
            records.WaitingStep(5, "", "", "check", "", now, 0, records.BehindHead("remote startup", now, ""),
                                route=target_config.MAC_ONLY),
            records.WaitingStep(8, "", "", "test", str(scratch), now, 0, records.BehindHead("remote startup", now, ""),
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
                 5: "Mac wait", 7: "Unplaced wait"}
        cloud_names = {cloud[0].owner: "Cloud wait", cloud[1].owner: "Starting wait"}
        waits = limits_view.visible_waits(queue_view.QueueModel(queue, {}), names, cloud_names)
        saved = limits_view.Background(history, limits_view.Metric("money", 2, 5, "USD", "estimate"),
            cloud_slots=9, hana=(limits_view.HanaCapacity(hana, "fixture-host", 1, None),),
            aws_targets=frozenset({"fixture-aws"}), origins=origins)

        with (mock.patch.object(repository, "output", side_effect=AssertionError("Git read in the feed tick")),
              mock.patch.object(conf, "read", side_effect=AssertionError("Config read in the feed tick"))):
            shown = limits_view.build_limits(queue, (mac_snapshot,), waits, cloud_names,
                                             {"session": "Mac build", "cleanup": "Old name"}, saved, UTC, mac=mac)
        self.assertEqual([(row.name, row.owner, row.quiet) for row in shown.repositories], [
            ("cleanup", "", False), ("hana", "", False), ("project", "team", False), ("denied", "", True),
            ("project", "other", True), ("quiet", "", True),
        ])
        cleanup, hana_row, project_row, denied_row, spare_project, quiet = shown.repositories
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
        self.assertEqual(shown.mac.unattributed, 0)
        self.assertEqual(shown.mac.waits, {"This run waits while a step starts on another machine.": ["Mac wait", ""]})
        self.assertEqual((project_row.mac.used, project_row.mac.limit, project_row.mac.runs), (1, 1, ["Mac build"]))
        self.assertEqual((cleanup.mac.used, cleanup.mac.limit, cleanup.mac.runs), (1, 1, []))
        self.assertEqual((quiet.mac.used, quiet.mac.limit, quiet.mac.note), (0, 1, ""))
        self.assertEqual(denied_row.mac, limits_view.Cell())
        self.assertEqual(spare_project.mac, limits_view.Cell())
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
        idle_mac = limits_view.build_limits(reading, (mac_snapshot,), (), {}, {}, saved, UTC, mac=replace(mac, claims=()))
        self.assertEqual([(row.owner, row.mac.used, row.mac.limit) for row in idle_mac.repositories if row.name == "project"],
                         [("other", 0, 1), ("team", None, None)])
        self.assertFalse(bad_claims.claims_readable)
        corrupt = limits_view.build_limits(queue, (), waits, cloud_names, {}, saved, UTC, mac=bad_claims)
        self.assertEqual(corrupt.mac.state, "Not available")
        self.assertEqual(corrupt.mac.waits, shown.mac.waits)
        self.assertTrue(all(row.mac == limits_view.Cell() for row in corrupt.repositories))
        self.assertTrue(bad_block.claims_readable)
        unreadable = limits_view.build_limits(queue, (), waits, cloud_names, {}, saved, UTC, mac=bad_block)
        self.assertEqual(unreadable.mac.state, "Not available")
        self.assertEqual([(row.name, row.mac.used) for row in unreadable.repositories if row.mac.used is not None],
                         [("cleanup", 1), ("project", 1), ("quiet", 0)])
        whole = limits_view.build_limits(queue, (), waits, cloud_names, {}, saved, UTC, mac=whole_mac)
        self.assertEqual(whole.mac.unattributed, 1)
        self.assertTrue(all(row.mac.limit is None and row.mac.used != 0 for row in whole.repositories))
        self.assertEqual(sum(row.mac.used or 0 for row in whole.repositories), 2)
        self.check_mac_expiry(mac_snapshot, mac, saved)
        within_limit = replace(queue, explanation=replace(explanation,
            policy=replace(explanation.policy, repository_runs=records.RepositoryRunLimits({"natedev.project": 2}, "merge-owner"))))
        within = limits_view.build_limits(within_limit, (), waits, cloud_names, {}, saved, UTC)
        merge_cell = next(row.natedev for row in within.repositories if row.owner == "team")
        self.assertEqual(merge_cell.merge_above, [])
        self.assertEqual(merge_cell.runs, ["regular", "merge-owner"])
        self.check_hana_claim(hana)

    def check_mac_expiry(self, snapshot: targets.TargetSnapshot, mac: limits_view.MacReading,
                         saved: limits_view.Background) -> None:
        refresher = memory_feed.TargetRefresh()
        local = replace(snapshot, target=target_config.LocalTarget("natedev", "linux"), claims=())
        reading = memory_feed.TargetReading((local, snapshot), mac)
        refresher.saved = (10.0, reading)
        pending: Future[tuple[float, memory_feed.TargetReading]] = Future()
        refresher.pending = pending
        self.assertIs(refresher.current(13.9, 2.0, saved), reading)
        expired = refresher.current(14.0, 2.0, saved)
        self.assertEqual(expired.mac, limits_view.MacReading("mac", None))
        self.assertIsInstance(expired.snapshots, tuple)
        assert isinstance(expired.snapshots, tuple)
        self.assertEqual([item.target for item in expired.snapshots], [local.target, snapshot.target])
        self.assertTrue(all(not item.claims and isinstance(item.states[0], targets.StateUnreadable)
                            for item in expired.snapshots))
        routes = target_availability.MacRoutes(expired.snapshots, {}, UTC)
        self.assertTrue(routes.is_mac("mac"))
        self.assertIsInstance(routes.route_fit(target_config.MAC_ONLY, "check", "", ""),
                              target_availability.FitUnreadable)
        pending.set_result((14.0, reading))
        refresher.next_at = 16.0
        self.assertIs(refresher.current(14.1, 2.0, saved), reading)

    def read_mac_fixture(self, root: Path, scratch: Path, origins: limits_view.Origins) -> tuple[
            targets.TargetSnapshot, limits_view.MacReading | None, limits_view.MacReading | None,
            limits_view.MacReading | None, limits_view.MacReading | None]:
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
        started = process_start.process_start(os.getpid())
        assert isinstance(started, process_start.Started)
        saved: claim_state.RemoteJobClaim = {"claim_id": "project-claim", "episode": "episode", "attempt": "attempt",
            "submitting_session": "session", "pid": os.getpid(), "proc_start": started.start, "what": "test",
            "worktree": str(scratch),
            "since": since.isoformat(), "repo_name": "project", "repo_key": "", "remote_host": "fixture-host"}
        mac_test.write_json(claims / "e5870be3.json", saved)
        mac_test.write_json(claims / "copied-record.json", saved)
        cleanup: claim_state.RemoteJobClaim = {**saved, "claim_id": "cleanup", "cleanup_pending": True,
            "submitting_session": "cleanup", "repo_name": "cleanup", "repo_key": "https://example.invalid/team/cleanup"}
        mac_test.write_json(claims / "cleanup.json", cleanup)
        other: claim_state.RemoteJobClaim = {**saved, "claim_id": "whole-claim", "worktree": "private relative folder"}
        del other["repo_name"], other["repo_key"]
        dead: claim_state.ProcessBoundClaim = {"claim_id": "dead", "episode": "episode", "attempt": "attempt",
            "submitting_session": "", "pid": 2**30, "proc_start": "", "what": "check", "worktree": str(scratch),
            "since": since.isoformat(), "repo_name": "project", "repo_key": ""}
        mac_test.write_json(claims / "dead.json", dead)
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(root), "MAC_TEST_STATE_DIR": str(state)}),
              mock.patch.object(mac_test, "holder_name", return_value="holder"),
              mock.patch.object(targets, "utc_now", return_value=since)):
            target = target_config.configured_ssh_target("mac")
            snapshot = targets.ssh_target_snapshot(target)
            mac = limits_view.read_mac((snapshot,), origins)
            _ = (state / "block.json").write_text("{", encoding="utf-8")
            unreadable_block = targets.ssh_target_snapshot(target)
            bad_block = limits_view.read_mac((unreadable_block,), origins)
            mac_test.write_block(state / "block.json", block)
            mac_test.write_json(state / "run.json", other)
            whole_snapshot = targets.ssh_target_snapshot(target)
            whole_mac = limits_view.read_mac((whole_snapshot,), origins)
            _ = (claims / "corrupt.json").write_text("{", encoding="utf-8")
            corrupt = targets.ssh_target_snapshot(target)
            bad_claims = limits_view.read_mac((corrupt,), origins)
        self.assertIsInstance(snapshot.states[0], targets.Held, str(snapshot.states[0]))
        self.assertIsInstance(unreadable_block.states[0], targets.StateUnreadable)
        self.assertIsInstance(corrupt.states[0], targets.StateUnreadable)
        self.assertTrue(mac_test.run_holds_work(cleanup))
        return snapshot, mac, whole_mac, bad_block, bad_claims

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

"""Check the Build limits reading with private settings and queue records."""
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

from scripts.build_hold.owning_session import OwningSession
from scripts.build_queue import admission_protocol as wire, admission_records as records, limits_view, placement
from scripts.build_queue.test_placement import PlacementFixture, placement_request
from scripts.build_queue.cloud_progress import NoCloudProgress
from scripts.build_queue.progress_profile import NoCounter
from scripts.build_queue.queue_fixture import fixture_reservation, head_reading
from scripts.build_targets import target_config, targets
from scripts.build_targets.execution_transport import HanaControlTransport
from scripts.buildlog import repository, store
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
            _ = (root / "build_admission.conf").write_text(
                "[disk]\n# whole: 1..\nfree_floor_gib=31\n[]\n# whole: 1..\ncloud_gate_slots=4\n")
            _ = (root / "cloud_gate.conf").write_text("enabled=on\n")
            _ = (root / "mac_test.conf").write_text("offload=on\nlinux_only..claude=\nlinux_only.workspace=\n" +
                                                   "# range: 1..\nmac_budget_gib=18\n# range: 1..\nmac_budget_gib.workspace=27\n")
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
                    placement_block=records.PlacementBlock("mac", "mac", "blocked", "mac-episode")),
                records.WaitingStep(3, "", "", "check", str(clone), now, 0, records.BuildSpaceLow(20, 31)),
                records.WaitingStep(6, "", "", "check", "", now, 0, records.ReadyForAdmission(),
                    placement_block=records.PlacementBlock("ssh", "hana-aws", "memory", "hana-episode")),
                records.WaitingStep(9, "", "", "check", str(worktree), now, 0, limits),
            )
            explanation = replace(baseline, policy=policy, decided_from=replace(baseline.decided_from,
                reservations=(reserve, merge), build_space=records.BuildSpaceLow(20, 31)))
            view = replace(reading.view, waiting=waiting)
            cloud_owner = wire.Owner(4, "4")
            cloud = wire.CloudRow(cloud_owner, wire.Identity("", "", "gate", "", "cloud"),
                                  wire.CloudPlace("aws"), now, 0, NoCloudProgress(), NoCounter(), wire.CloudSlotWait(4, 4))
            slot = wire.RepositorySlotRow(wire.Owner(5, "5"), wire.Identity("", "", "check", str(worktree), "slot"),
                now, "mac", wire.RepositorySlotWaiting("workspace", (1,), 1, "fixture"))
            order = [(root / "1", records.RankedWaiter(pid=1, start_time="1", since=now, need=0, folder="",
                        repo_identity="https://example.invalid/claude_commands", order=records.NormalOrder())),
                     (root / "6", records.RankedWaiter(pid=6, start_time="6", since=now, need=0, folder="",
                        repo_identity="https://example.invalid/hana", order=records.NormalOrder()))]
            queue = wire.CloudQueueRead(view, explanation, order, cloud=(cloud,), cloud_slots=4,
                                        cloud_gate_stale_pushes=1, repository_slots=(slot,))
            until = datetime.fromtimestamp(now, UTC) + timedelta(hours=1)
            mac = targets.TargetSnapshot(target_config.SshTarget("mac", "macos", root / "mac_test.conf"),
                (targets.Held("holder", "fixture", targets.StartNotRecorded(), targets.ExpiresAt(until)),),
                (), eligibility.NO_BACKOFF)
            hana = targets.TargetSnapshot(target_config.SshTarget("hana-aws", "linux", root / "hana.conf"),
                (), (), eligibility.NO_BACKOFF)
            paths = claim_state.StatePaths(root / "mac")
            with (mock.patch.dict(os.environ, {"CONF_DIR": directory}),
                    mock.patch.object(target_config, "ssh_target_state_directory", return_value=paths.directory),
                    mock.patch.object(target_config, "read_execution_transport",
                                      return_value=HanaControlTransport(root / "hana.conf", "aws-hana")),
                    mock.patch.object(limits_view, "hana_route", return_value=None),
                    claim_state.record_claim_wait(paths, 2, str(worktree), "nextest", OwningSession("mac"), "mac-episode"),
                    claim_state.record_claim_wait(paths, 7, str(worktree), "nextest", OwningSession("direct"), "")):
                reply = wire.parse_reply(wire.encode_reply(wire.ViewReply(queue)))
                self.assertIsInstance(reply, wire.ViewReply)
                assert isinstance(reply, wire.ViewReply)
                current = limits_view.with_mac_claim_waits(reply.reading, (mac, hana))
                self.assertEqual(len(current.view.waiting), 6)
                result = limits_view.build_limits(current, (mac, hana), limits_view.QueueNames(
                    {1: "local wait", 2: "mac wait", 3: "disk wait", 4: "hana wait", 5: "other repo", 6: "direct wait"},
                    {cloud_owner: "cloud wait"}, {}),
                    limits_view.Metric("Memory for builds", 7, 12, "GiB"), conf.read("build_folders"), saved, UTC, ())
            self.assertEqual(claim_state.read_claim_waits(paths), ())
            with claim_state.record_claim_wait(paths, 8, str(worktree), "nextest", OwningSession("ended"), ""):
                with mock.patch.object(process_start, "process_start", return_value=process_start.Gone()):
                    self.assertEqual(claim_state.read_claim_waits(paths), ())
                self.assertEqual(list(paths.claim_waits.iterdir()), [])
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
            self.assertEqual(result.natedev.waits, {"Disk floor": ["disk wait"]})
            self.assertEqual(rows["workspace"].mac.waits,
                             {"Repository slot limit full": ["slot"], "Limit full": ["mac wait", "direct wait"]})
            self.assertEqual(rows["hana"].aws.waits, {"Memory": ["hana wait"]})
            self.assertFalse(rows["quiet"].aws.available)
            self.assertEqual(result.aws.waits, {"Gate limit full": ["cloud wait"]})

        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            target = placement.PlacementDestination("ssh", "aws", "fixture-aws",
                                                    claim_state.RepositoryScope("hana"))
            request = replace(request, local=wire.LocalAdmissionUnavailable("fixture"),
                constraints=replace(request.constraints, candidates=(placement.PlacementCandidate(target),)))
            with mock.patch.object(fixture.server, "host_evidence_key") as unused_key:
                unused_key.side_effect = OSError("No provider key in this fixture")
                reply, = fixture.server.handle_request(request)
            self.assertIsInstance(reply, wire.PlacementWait)
            fixture.server.placement_eligibility[request.episode] = (
                placement.CandidateEligibility(target, placement.TemporaryExclusion("busy")),)
            actual = fixture.server._placement_wait_reading(head_reading())  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(actual.view.waiting[-1].placement_block,
                             records.PlacementBlock("ssh", "aws", "busy", request.episode))

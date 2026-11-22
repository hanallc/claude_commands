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
from scripts.build_queue import admission_client, test_memory_feed, test_placement
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
            with mock.patch.object(repository, "repo_name", return_value="workspace"), mock.patch.object(
                    repository, "git_common_dir", return_value=None):
                self.assertIs(claim_state.checkout_claim_scope(str(workspace), "macos"), claim_state.WHOLE_MAC)
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
            self.assertEqual(stale_saved.origins.mac_folders, history.origins.mac_folders)
            refresh.pending = Future()
            refresh.pending.set_result(updated)
            self.assertIs(refresh.current(14, 60), updated)
            self.assertFalse(updated.history_stale)

            _ = (root / "memory_feed.conf").write_text("# whole: 1..\nmac_activity_refresh_seconds=10\n")
            _ = (root / "build_admission.conf").write_text(
                "[disk]\n# whole: 1..\nfree_floor_gib=31\n[]\n# whole: 1..\ncloud_gate_slots=4\n")
            _ = (root / "cloud_gate.conf").write_text("enabled=on\n")
            mac_file = root / "mac_test.conf"
            mac_settings = (
                "offload=on\nhost=fixture-mac\nlinux_only..claude=\nlinux_only.workspace=\nlinux_only.tools=\n"
                "# range: 1..\nconnect_timeout_s=6\n# range: 1..\nleftover_timeout_s=10\n"
                "# range: 1..\nprobe_timeout_s=15\n# range: 1..\nunreachable_backoff_s=30\n"
                "# range: 1..\nmirror_busy_backoff_s=10\n# whole: 1..\nabandoned_session_minutes=9\n"
                "# range: 1..\nmac_budget_gib=18\n# range: 1..\ntarget_budget_gib=21\n"
                "# range: 1..\nmac_budget_gib.workspace=27\n# range: 1..\nmac_budget_gib.tools=88\n"
                "# range: 1..\nfree_floor_gib=100\n")
            _ = mac_file.write_text(mac_settings)
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
            claim: claim_state.ProcessBoundClaim = {
                "claim_id": "fixture-claim", "episode": "mac-episode", "attempt": "attempt-1",
                "submitting_session": "mac", "holder_session": "same-session", "pid": 2,
                "proc_start": "matched", "what": "nextest", "worktree": str(worktree),
                "since": store.utc_iso(now),
            }
            held = claim_state.HeldClaim(claim_state.RepositoryScope("workspace", route_origin),
                                        root / "fixture-claim", claim)
            mac = targets.TargetSnapshot(target_config.SshTarget("mac", "macos", root / "mac_test.conf"),
                (targets.Held("holder", "fixture", targets.StartNotRecorded(), targets.ExpiresAt(until)),),
                (held,), eligibility.NO_BACKOFF)
            hana = targets.TargetSnapshot(target_config.SshTarget("hana-aws", "linux", root / "hana.conf"),
                (), (), eligibility.NO_BACKOFF)
            paths = claim_state.StatePaths(root / "mac")
            mac_activity = root / "mac-builds.json"
            build = queue_view.MacBuild(21, str(worktree), now, "cargo nextest run", "workspace",
                session="different-session", gate_label=queue_view.MacGateLabel.PLACEMENT,
                gate_placement="mac-episode/attempt-1")
            other = replace(build, pid=22, session="same-session", gate_placement="mac-episode/attempt-2")
            _ = mac_activity.write_text(json.dumps({"read_at": now,
                "builds": [queue_view.mac_build_record(run) for run in (build, other)],
                "disk_free_bytes": 80 * records.GIB, "target": "mac"}))
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
                    claim_state.record_claim_wait(paths, 2, str(worktree), "nextest", OwningSession("mac"), "mac-episode",
                        decision=claim_state.MacDecision("mac", held.scope, "limit-full")),
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
                                 records.PlacementBlock("mac", "mac", "busy", "mac-episode",
                                                        claim_state.MacDecision("mac", held.scope, "limit-full")))
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
                mac_runs = tuple(zip(activity.builds, ("matching run", "other attempt"), strict=True))
                result = limits_view.build_limits(current, (mac, hana), names, memory, folders,
                                                  stale_saved, UTC, mac_runs, disk_free / records.GIB,
                                                  mac_activity_target=activity.target)
                self.check_mac_decisions(current, mac, hana, names, memory, folders, saved, worktree, build, held, claim)
                kept: claim_state.RemoteJobClaim = {
                    **claim, "pid": 8, "proc_start": "ends", "remote_host": "fixture-mac",
                }
                for scope, note in ((held.scope, "Claim held for cleanup"),
                                    (claim_state.WHOLE_MAC, "Whole Mac in use")):
                    with self.subTest(scope=scope):
                        retained = replace(mac, claims=(replace(held, scope=scope, record=kept),))
                        retained_limits = limits_view.build_limits(current, (retained, hana), names, memory,
                                                                  folders, saved, UTC, mac_runs,
                                                                  mac_activity_target=activity.target)
                        mac_cell = next(row.mac for row in retained_limits.repositories if row.name == "workspace")
                        self.assertEqual((mac_cell.used, mac_cell.limit), (1, 1))
                        self.assertEqual(retained_limits.mac.other, ("other attempt",))
                        if scope is claim_state.WHOLE_MAC:
                            self.assertEqual(retained_limits.mac.runs, ("matching run",))
                            self.assertTrue(retained_limits.mac.cleanup)
                        else:
                            self.assertEqual(mac_cell.runs, ["matching run"])
                        self.assertEqual(mac_cell.note, note)
                unknown_folder = str(root / "unknown-mac-checkout")
                unknown_run = replace(build, folder=unknown_folder)
                unknown_claim: claim_state.RemoteJobClaim = {**kept, "worktree": unknown_folder}
                same_name = "https://example.invalid/other-team/workspace"
                other_workspace = root / "other-team" / "workspace"
                other_workspace.mkdir(parents=True)
                git(other_workspace, "init", "-q")
                git(other_workspace, "remote", "add", "origin", same_name)
                self.assertEqual(history.origins.resolve("", str(other_workspace), read_checkout=True), same_name)
                duplicate = replace(saved, history=replace(history, machines={**history.machines, same_name: {"mac"}}))
                for background in (saved, duplicate):
                    for scope in (claim_state.RepositoryScope("workspace"), claim_state.WHOLE_MAC):
                        with self.subTest(scope=scope, duplicate=background is duplicate):
                            retained = replace(mac, claims=(replace(held, scope=scope, record=unknown_claim),))
                            shared_limits = limits_view.build_limits(current, (retained, hana), names, memory,
                                folders, background, UTC, ((unknown_run, "unknown origin"),), mac_activity_target=activity.target)
                            self.assertEqual(shared_limits.mac.claims, 1)
                            self.assertEqual(shared_limits.mac.runs, ("unknown origin",))
                            self.assertEqual(len(shared_limits.repositories), 5 if background is duplicate else 4)
                            self.assertTrue(all(not row.mac.runs for row in shared_limits.repositories))
                            if isinstance(scope, claim_state.RepositoryScope):
                                affected = ({route_origin.removeprefix("https://"), same_name.removeprefix("https://")}
                                            if background is duplicate else {"workspace"})
                                self.assertEqual({row.name for row in shared_limits.repositories if row.mac.used}, affected)
                                for row in shared_limits.repositories:
                                    if row.name in affected:
                                        self.assertEqual((row.mac.used, row.mac.limit, row.mac.note),
                                                         (1, 1, "Mac slot claimed"))
                for record in (claim, kept):
                    named_scope = replace(held, scope=claim_state.RepositoryScope("workspace"), record=record)
                    occupied = limits_view.build_limits(current, (replace(mac, claims=(named_scope,)), hana), names,
                        memory, folders, duplicate, UTC, ((build, "matching run"),), mac_activity_target=activity.target)
                    self.assertEqual(occupied.mac.runs, ())
                    cells = {row.name: row.mac for row in occupied.repositories}
                    original = cells[route_origin.removeprefix("https://")]
                    other_cell = cells[same_name.removeprefix("https://")]
                    self.assertEqual((original.used, original.runs), (1, ["matching run"]))
                    self.assertEqual((other_cell.used, other_cell.runs, other_cell.note), (1, [], "Mac slot claimed"))
                tools_origin = "https://example.invalid/team/tools"
                tools_checkout = root / "tools-local"
                tools_checkout.mkdir()
                git(tools_checkout, "init", "-q")
                git(tools_checkout, "remote", "add", "origin", tools_origin)
                self.assertEqual(history.origins.resolve("", str(tools_checkout), read_checkout=True), tools_origin)
                tools_background = replace(saved, history=replace(history, machines={**history.machines, tools_origin: {"mac"}}))
                unrelated = replace(held, scope=claim_state.RepositoryScope("tools"), record=unknown_claim)
                unclaimed_run = replace(unknown_run, pid=23, gate_placement="another-episode/attempt-1")
                tools_limits = limits_view.build_limits(current, (replace(mac, claims=(unrelated,)), hana), names,
                    memory, folders, tools_background, UTC, ((unknown_run, "tools run"), (unclaimed_run, "other run")),
                    mac_activity_target=activity.target)
                self.assertEqual(tools_limits.mac.runs, ("tools run",))
                self.assertEqual(tools_limits.mac.other, ("other run",))
                self.assertTrue(all(not row.mac.other for row in tools_limits.repositories))
                tools_cell = next(row.mac for row in tools_limits.repositories if row.name == "tools")
                self.assertFalse(tools_cell.available)
                self.assertIsNone(tools_cell.limit)
                self.assertEqual(tools_cell.budgets, ())

                _ = mac_file.write_text(mac_settings + (
                    "linux_only.tools-local=\nlinux_only.tools-ci=\n# range: 1..\nmac_budget_gib.tools-local=29\n"))
                second_checkout = root / "tools-ci"
                second_checkout.mkdir()
                git(second_checkout, "init", "-q")
                git(second_checkout, "remote", "add", "origin", tools_origin)
                _ = history.origins.resolve("", str(second_checkout), read_checkout=True)
                tools_claim = replace(held, scope=claim_state.RepositoryScope("tools-local"),
                                      record={**claim, "worktree": str(tools_checkout)})
                mac_wait = next(entry for entry in names.waits if entry.name == "mac wait").source
                assert isinstance(mac_wait, queue_view.WaitingBuild)
                tools_block = records.PlacementBlock("mac", "mac", "busy", "tools-episode",
                    claim_state.MacDecision("mac", tools_claim.scope, "limit-full"))
                tools_wait = replace(mac_wait, step=replace(mac_wait.step, folder=str(second_checkout),
                    placement_episode="tools-episode", placement_block=tools_block))
                multi_names = replace(names, waits=(*names.waits, limits_view.VisibleWait(tools_wait, "tools wait")))
                multi = limits_view.build_limits(current, (replace(mac, claims=(tools_claim,)), hana), multi_names,
                    memory, folders, tools_background, UTC, ())
                tools = next(row for row in multi.repositories if row.name == "tools")
                self.assertEqual([(slot.name, slot.cell.used, slot.cell.limit, slot.cell.budgets[0].limit)
                                  for slot in tools.mac_slots], [("tools-ci", 0, 1, 21), ("tools-local", 1, 1, 29)])
                self.assertEqual([slot.cell.runs for slot in tools.mac_slots], [[], ["nextest"]])
                self.assertEqual([slot.cell.waits for slot in tools.mac_slots], [{}, {"Limit full": ["tools wait"]}])
                self.assertEqual(tools.mac.waits, {})
                unreadable = replace(mac, states=(targets.StateUnreadable("Cannot read fixture claims"),), claims=())
                claim_status = limits_view.build_limits(current, (unreadable, hana), names, memory,
                                                       folders, saved, UTC, mac_runs, mac_activity_target=activity.target)
                self.assertEqual(claim_status.mac.other, ("matching run", "other attempt"))
                self.assertEqual(claim_status.mac.other_note, "Claim status unavailable")
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
            self.assertEqual(rows["workspace"].mac.used, 1)
            self.assertEqual(result.mac.other, ("other attempt",))
            self.assertEqual(result.mac.other_note, "No matching claim")
            self.assertEqual(rows["claude_commands"].mac.budgets[0].limit, 21)
            self.assertEqual(result.mac.state, f"Held until {until:%H:%M UTC}")
            self.assertEqual(result.natedev.shared[-1], limits_view.Metric("Free disk", 20, 31, "GiB", "minimum"))
            self.assertEqual(result.aws.shared[0], limits_view.Metric("Cloud gates", 0, 4))
            self.assertEqual(rows["claude_commands"].natedev.waits, {"Run limit full": ["local wait", "other repo"]})
            self.assertEqual(result.natedev.waits, {"Disk floor": ["disk wait"], "Memory": ["old caller"]})
            self.assertEqual(rows["workspace"].mac.waits, {"Limit full": ["mac wait"]})
            self.assertEqual(result.mac.shared, (limits_view.Metric("Free disk", 80, 100, "GiB", "minimum"),))
            self.assertEqual(result.mac.waits, {"Disk floor": ["Mac disk wait"], "Busy": ["direct wait"]})
            self.assertEqual(rows["workspace"].natedev.waits,
                             {"Evidence pending": ["expired"], "Behind older check in the queue": ["queued step"]})
            self.assertEqual(rows["workspace"].unplaced, {"Evidence pending": ["pending", "other episode"]})
            self.assertEqual(rows["hana"].aws.waits, {"Memory": ["hana wait"]})
            self.assertFalse(rows["quiet"].aws.available)
            self.assertEqual(result.aws.waits, {"Gate limit full": ["cloud wait"]})

        self.check_provider_decisions()
        self.check_placement_replies()
        self.check_refusal_after_mac_is_free()
        self.check_placement_tooltips()

    def check_mac_decisions(self, reading: records.QueueRead, mac: targets.TargetSnapshot,
                            hana: targets.TargetSnapshot, names: limits_view.QueueNames,
                            memory: limits_view.Metric, folders: conf.ConfigRead,
                            saved: limits_view.Background, worktree: Path, build: queue_view.MacBuild,
                            claim: claim_state.HeldClaim, claim_record: claim_state.ProcessBoundClaim) -> None:
        def waiting(target: str, blocker: claim_state.MacBlocker, name: str) -> limits_view.VisibleWait:
            step = records.WaitingStep(30, "", "", "nextest", str(worktree), 1, 0, records.ReadyForAdmission(),
                placement_block=records.PlacementBlock("mac", target, "busy", name,
                    claim_state.MacDecision(target, claim_state.RepositoryScope("saved-slot"), blocker)))
            return limits_view.VisibleWait(queue_view.WaitingBuild(1, step), name)

        other_file = worktree.parent / "other-mac.conf"
        _ = other_file.write_text("# range: 1..\nfree_floor_gib=200\n")
        other = replace(mac, target=target_config.SshTarget("other-mac", "macos", other_file))
        self.assertEqual(queue_view.selected_mac_target((other.target, mac.target)), mac.target)
        startup = records.RunningStep("", "", "explicit command", str(worktree), 0,
            records.RemoteMemoryUnavailable("fixture"), pid=records.PidNotRecorded(), mac_placement="start/attempt",
            mac_startup=claim_state.MacDecision("mac", claim_state.RepositoryScope("saved-slot"), "starting"))
        with_startup = replace(reading, view=replace(reading.view, running=(startup,)))
        wait_names = replace(names, waits=(waiting("mac", "mirror-backoff", "mirror"),
                                          waiting("mac", "limit-full", "full"),
                                          waiting("other-mac", "limit-full", "other target")))
        no_claim = replace(mac, claims=())
        result = limits_view.build_limits(with_startup, (other, no_claim, hana), wait_names, memory, folders,
                                           saved, UTC, ((build, "other host process"),), 199,
                                           mac_activity_target="other-mac")
        current = next(row for row in result.repositories if row.name == "workspace")
        slots = {slot.name: slot.cell for slot in current.mac_slots}
        recorded = slots["saved-slot"]
        self.assertEqual(recorded.waits, {"Waiting for the mirror": ["mirror"], "Limit full": ["full"]})
        self.assertEqual((recorded.used, recorded.limit, recorded.starting), (1, 1, ["explicit command"]))
        self.assertIs(recorded.standard_mac_builds, False)
        self.assertEqual(slots["workspace"].waits, {})
        self.assertEqual(current.unplaced, {"other-mac · Limit full": ["other target"]})
        self.assertEqual(result.mac.target, "mac")
        self.assertEqual(result.mac.claims, 0)
        self.assertEqual(result.mac.other, ())
        self.assertEqual(result.mac.shared, (limits_view.Metric("Free disk", None, 100, "GiB", "minimum"),))

        cleanup: claim_state.RemoteJobClaim = {**claim_record, "remote_host": "fixture-mac", "cleanup_pending": True}
        with_cleanup = replace(mac, claims=(replace(claim, record=cleanup),))
        for runs, expected in (((), []), (((build, "verified process"),), ["verified process"])):
            result = limits_view.build_limits(reading, (with_cleanup, hana), replace(names, waits=()), memory,
                folders, saved, UTC, runs, mac_activity_target="mac")
            cell = next(row.mac for row in result.repositories if row.name == "workspace")
            self.assertEqual((cell.note, cell.runs), ("Claim held for cleanup", expected))
        whole = replace(mac, claims=(replace(claim, scope=claim_state.WHOLE_MAC),))
        result = limits_view.build_limits(reading, (whole, hana), names, memory, folders, saved, UTC, ())
        self.assertEqual((result.mac.claims, result.mac.runs), (1, ("nextest",)))

    def check_provider_decisions(self) -> None:
        with test_placement.PlacementFixture() as fixture:
            request = test_placement.placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, command_kind="install"))
            destination = request.constraints.candidates[0].destination
            target = target_config.SshTarget("mac", "macos", fixture.root / "mac_test.conf")
            paths = claim_state.StatePaths(fixture.root / "mac")
            key = fixture.server.host_evidence_key(request, destination)
            fixture.server.host_requests[key] = request
            with (mock.patch.object(target_config, "read_targets", return_value=(target,)),
                  mock.patch.object(claim_state, "state_paths", return_value=paths),
                  mock.patch.object(claim_state, "target_claim_scope", return_value=destination.scope),
                  mock.patch.object(claim_state, "read_claims", return_value=()),
                  mock.patch.object(repository, "repo_name", return_value="hana"),
                  mock.patch.object(eligibility, "read_backoff", return_value=eligibility.NO_BACKOFF),
                  mock.patch.object(eligibility, "read_mirror_busy", return_value=eligibility.ActiveBackoff())):
                evidence = fixture.read_host_evidence(key)
            assert isinstance(evidence, placement.FreshHostEvidence)
            self.assertEqual(evidence.eligibility, placement.TemporaryExclusion("busy"))
            self.assertEqual(evidence.mac_blocker, "mirror-backoff")
            fixture.provider_evidence[key] = evidence
            fixture.server.publish_host_evidence(evidence)
            reply, = fixture.server.handle_request(request)
            assert isinstance(reply, wire.PlacementWait)
            reply = wire.parse_reply(wire.encode_reply(reply))
            assert isinstance(reply, wire.PlacementWait)
            self.assertEqual(reply.eligibility[0].mac_decision,
                             claim_state.MacDecision("mac", destination.scope, "mirror-backoff"))
            current = fixture.server._placement_wait_reading(head_reading())  # pyright: ignore[reportPrivateUsage]
            block = current.view.waiting[-1].placement_block
            assert block is not None
            self.assertEqual(block.mac_decision, reply.eligibility[0].mac_decision)

            _ = test_placement.publish_candidate(fixture, request)
            _ = fixture.run_pass()
            _ = test_placement.assignment_for(fixture, request)
            current = fixture.server._placement_reading(head_reading())  # pyright: ignore[reportPrivateUsage]
            self.assertEqual(current.view.running[-1].mac_startup,
                             claim_state.MacDecision("mac", destination.scope, "starting"))

    def check_refusal_after_mac_is_free(self) -> None:
        with (test_placement.PlacementFixture() as fixture,
              mock.patch.object(admission_service, "HostEvidenceWorker", test_placement.ManualEvidenceWorker)):
            request = test_placement.placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, selection=placement.MacOnlyHost()))
            blocked = placement.TemporaryExclusion("disk")
            _ = test_placement.MacOrderTests.provide(fixture, request, blocked)
            reply, = fixture.server.handle_request(request)
            self.assertIsInstance(reply, wire.PlacementWait)
            worker = fixture.server.host_targets[("mac", "mac")]
            assert isinstance(worker, test_placement.ManualEvidenceWorker)
            self.assertIsNotNone(worker.probe_next())
            refused, = fixture.server.handle_request(request)
            assert isinstance(refused, wire.DispatchStatus)
            self.assertEqual(refused.state, placement.ClosedAssignment("disk"))

            fixture.clock.advance(1)
            next_request = test_placement.placement_request(fixture)
            _ = test_placement.publish_candidate(fixture, request)
            _ = test_placement.publish_candidate(fixture, next_request)
            _ = fixture.run_pass()
            _ = test_placement.assignment_for(fixture, next_request)
            for restart in (False, True):
                if restart:
                    fixture.restart()
                    _ = test_placement.publish_candidate(fixture, request)
                    _ = fixture.run_pass()
                replay, = fixture.server.handle_request(request)
                self.assertEqual(replay, refused)
                self.assertNotIn(request.episode, fixture.server.current_assignments)
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                self.assertNotIn(request.episode, {step.episode for step in view.reading.view.waiting})

    def check_placement_tooltips(self) -> None:
        fixture = test_memory_feed.MemoryFeedTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        _ = (fixture.root / "machines.conf").write_text("mac.hosts=fixture-mac\n")
        reading = test_memory_feed.with_wait_reason(test_memory_feed.reservation_reading(), records.NeedsMemory())
        waiter, = reading.view.waiting
        waits = (replace(waiter, placement_episode="first"), replace(waiter, placement_episode="second"))
        reading = replace(reading, view=replace(reading.view, waiting=waits))
        with mock.patch.object(admission_client, "view", return_value=reading):
            page = memory_feed.reading()
        first, second = page["waiters"]
        self.assertEqual((first["pid"], first["since"]), (second["pid"], second["since"]))
        first_key, second_key = first.get("key"), second.get("key")
        self.assertTrue(first_key and second_key)
        self.assertNotEqual(first_key, second_key)

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
            self.assertEqual(limits_view.placement_reason(older.reason), "Busy")
            self.assertTrue(younger.reason.startswith("Behind "))
            self.assertTrue(younger.reason.endswith(" in the queue"))
            self.assertEqual(younger.target, "mac")
            reply = fixture.server._placement_reply(order.younger, placement.ResourceHolderUnknown())  # pyright: ignore[reportPrivateUsage]
            assert isinstance(reply, wire.PlacementWait)
            self.assertEqual(reply.wait, wire.PlacementResourceWait("mac", younger.reason))

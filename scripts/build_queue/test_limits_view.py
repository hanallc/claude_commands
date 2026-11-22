"""Check the Build limits section with published records and private fixture settings."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date
from pathlib import Path
from threading import Thread
from unittest import mock
from xml.etree import ElementTree

from scripts.aws_target import hana_box, hana_placement
from scripts.build_queue import admission_protocol as wire, admission_records as records, limits_view, queue_view
from scripts.build_queue import snapshot_codec, test_memory_hover
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


def wait_names(groups: Mapping[str, limits_view.WaitGroup]) -> dict[str, list[str]]:
    return {group.words.one: group.names for group in groups.values()}


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
        )
        unbound_reservation: records.Reservation = {
            **fixture_reservation(104, 0, step="gate", folder=str(root / "unknown")), "repo": hidden_key,
        }
        explanation = replace(reading.explanation,
            policy=replace(reading.explanation.policy, repository_runs=policy),
            decided_from=replace(reading.explanation.decided_from, reservations=reservations, build_space=records.BuildSpaceLow(20, 31)))
        full = records.RunLimitWait(2, 1, "project", "natedev")
        waiting = (
            records.WaitingStep(1, "", "", "gate", str(copied), now, 0, full),
            records.WaitingStep(2, "", "", "check", "", now, 0, records.BuildSpaceLow(20, 31)),
            records.WaitingStep(3, "", "", "test", "", now, 0, records.NeedsMemory()),
            records.WaitingStep(4, "", "", "check", "", now, 0, records.ReadingUnavailable(),
                                route=target_config.PinnedTarget("fixture-aws")),
            records.WaitingStep(5, "", "", "check", "", now, 0, records.ReadyForAdmission(), route=target_config.MAC_ONLY),
            records.WaitingStep(8, "", "", "test", "", now, 0, records.NeedsMemory()),
            records.WaitingStep(10, "", "", "check", "", now, 0, records.ReadyForAdmission(),
                                route=target_config.PinnedTarget("other-target")),
        )
        unbound_waits = (
            records.WaitingStep(9, "", "", "gate", str(root / "unknown"), now, 0,
                                records.RunLimitWait(1, 2, hidden_key, "natedev")),
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
                 7: "Unplaced wait"}
        cloud_names = {cloud[0].owner: "Cloud wait", cloud[1].owner: "Starting wait"}

        def show(current: wire.CloudQueueRead) -> limits_view.BuildLimits:
            waits = limits_view.visible_waits(queue_view.QueueModel(current, {}), names, cloud_names)
            return limits_view.build_limits(current, (), waits, cloud_names, {}, saved, UTC)

        saved = limits_view.Background(history, limits_view.Metric("money", 2, 5, "USD", "estimate"),
            cloud_slots=9, hana=(limits_view.HanaCapacity(hana, 1, limits_view.ReadingState.OMITTED),),
            aws_targets=frozenset({"fixture-aws"}), origins=origins)

        with mock.patch.object(repository, "output", side_effect=AssertionError("Git read in the feed tick")):
            shown = show(queue)
        self.assertEqual([(row.name, row.quiet) for row in shown.repositories], [
            ("hana", False), ("project", False), ("quiet", True),
        ])
        hana_row, project_row, quiet = shown.repositories
        cell = project_row.natedev
        self.assertEqual((cell.used, cell.limit, cell.runs, cell.merge_above), (2, 1, ["regular"], ["merge-owner"]))
        self.assertEqual(wait_names(cell.waits), {"1 run waits because the run limit for project on natedev is full.": ["Run limit"]})
        self.assertIsNone(quiet.natedev.limit)
        self.assertEqual(quiet.natedev.used, 0)
        self.assertEqual(shown.natedev.unattributed, 1)
        self.assertEqual(shown.natedev.waits, {})
        unbound = show(replace(queue, view=replace(queue.view, waiting=unbound_waits),
            explanation=replace(explanation,
                decided_from=replace(explanation.decided_from, reservations=(*reservations, unbound_reservation)))))
        meters = {metric.label: (metric.used, metric.limit) for metric in unbound.natedev.shared}
        self.assertEqual(meters["Repository run limit"], (1, 2))
        self.assertNotIn("unmatched run limit", meters)
        self.assertEqual(wait_names(unbound.natedev.waits), {
            "1 run waits because the repository run limit is full.": ["Run limit"],
            "1 run waits because the run limit for unmatched on natedev is full.": ["Disk wait"],
        })
        self.assertEqual(next(row.natedev for row in unbound.repositories if row.name == "project"), limits_view.Cell())
        self.assertEqual((hana_row.aws.used, hana_row.aws.limit), (None, 1))
        disk = next(metric for metric in shown.natedev.shared if metric.label == "disk")
        self.assertEqual(wait_names(disk.waits),
                         {"1 run waits because the build disk has less free space than the queue requires.": ["Disk wait"]})
        self.assertEqual(wait_names(shown.natedev.shared[0].waits),
                         {"1 run needs more free memory to start.": ["Memory wait", ""]})
        self.assertEqual(wait_names(shown.aws.waits),
                         {"1 run waits because the queue cannot check whether it can start.": ["AWS target wait"]})
        self.assertEqual(wait_names(shown.unplaced), {"1 run can start at the queue's next check.": ["Unplaced wait"]})
        self.assertEqual((shown.aws.shared[0].used, shown.aws.shared[0].limit,
                          shown.aws.shared[0].reserved_waiting), (1, 4, 1))
        self.assertEqual(wait_names(shown.aws.shared[0].waits), {
            "1 run waits for a place on AWS.": ["Cloud wait"],
            "1 run has a place on AWS and waits for its machine to start.": ["Starting wait"],
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
        copy_cell = next(row.natedev for row in copy_limits.repositories if row.name == "project")
        self.assertEqual((copy_cell.used, copy_cell.limit, copy_cell.runs), (1, 1, ["merge-owner"]))
        self.assertEqual(copy_cell.waits, cell.waits)
        self.assertEqual(copy_limits.natedev.waits, shown.natedev.waits)
        matching = replace(full, limit=2)
        check: records.Reservation = {**fixture_reservation(105, 0, step="check"), "repo": "project",
                                      "repo_identity": fork, "unit": "check-owner"}
        within_limit = replace(queue,
            view=replace(queue.view, waiting=(replace(waiting[0], reason=matching), *waiting[1:])),
            explanation=replace(explanation,
                decided_from=replace(explanation.decided_from, reservations=(*reservations, check)),
                policy=replace(explanation.policy,
                    repository_runs=records.RepositoryRunLimits({"natedev.project": 2}, "merge-owner"))))
        within = show(within_limit)
        merge_cell = next(row.natedev for row in within.repositories if row.name == "project")
        self.assertEqual(merge_cell.merge_above, [])
        self.assertEqual(merge_cell.runs, ["regular", "merge-owner"])
        self.assertEqual((merge_cell.used, merge_cell.other_used), (2, 1))
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
        wait_cell = next(row.natedev for row in wait_limits.repositories if row.name == "project")
        self.assertEqual(wait_cell.waits, cell.waits)
        self.assertIsNone(wait_cell.limit)
        self.assertIsNone(wait_cell.used)
        self.assertEqual(wait_cell.runs, [])
        self.assertEqual((wait_cell.other_used, wait_cell.other), (1, ["check-owner"]))
        gpu_meter = next(metric for metric in wait_limits.natedev.shared if metric.label == "gpu")
        self.assertEqual((gpu_meter.used, gpu_meter.limit, wait_names(gpu_meter.waits)),
                         (None, None, {"1 run waits because all GPU test places are in use.": ["Memory wait"]}))
        behind = records.BehindHead("fixture", now, "")
        self.check_page(show(replace(queue, view=replace(queue.view, waiting=(
            replace(waiting[2], reason=behind), replace(waiting[5], reason=behind),
            replace(waiting[3], reason=behind),
        )))))
        self.check_policy_evidence(queue, saved, reservations[0], primary, fork)
        self.check_hana_claim(hana, queue)
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

        def show(*reservations: records.Reservation, waits: tuple[limits_view.VisibleWait, ...] = (),
                 origins: limits_view.Origins = saved.origins,
                 run_limits: records.RepositoryRunLimits = policy,
                 history: frozenset[str] | limits_view.ReadingState = saved.history,
                 ordered: list[tuple[Path, records.RankedWaiter]] = queue.ordered) -> limits_view.BuildLimits:
            reading = replace(queue, ordered=ordered, explanation=replace(queue.explanation,
                policy=replace(queue.explanation.policy, repository_runs=run_limits),
                decided_from=replace(queue.explanation.decided_from, reservations=reservations)))
            return limits_view.build_limits(reading, (), waits, {}, {}, replace(saved, origins=origins, history=history), UTC)

        def project(shown: limits_view.BuildLimits) -> limits_view.Cell:
            return next(row.natedev for row in shown.repositories if row.name == "project")

        def limit_wait(position: int, key: str, limit: int, name: str, *, folder: str = "") -> limits_view.VisibleWait:
            step = replace(queue.view.waiting[0], folder=folder, reason=records.RunLimitWait(limit, limit, key, "natedev"))
            return limits_view.VisibleWait(queue_view.WaitingBuild(position, step), name)

        one_key = project(show(first, unrelated))
        self.assertEqual((one_key.used, one_key.limit), (1, 2))
        shown = show(first, unrelated, second)
        self.assertEqual((project(shown).used, project(shown).limit), (1, 2))
        other = next(row.natedev for row in shown.repositories if row.name == "other")
        self.assertEqual((other.used, other.limit), (1, 2))
        self.assertEqual(sum(row.natedev.other_used for row in shown.repositories), 0)
        self.assertEqual(shown.natedev.unattributed, 0)
        self.assertEqual(shown.natedev.shared[-1].label, "Repository run limit")
        self.assertEqual(limits_view.wait_words(records.RunLimitWait(1, 1, root_key, "natedev")).one,
                         "1 run waits because the repository run limit is full.")
        history_key = "https://example.invalid/other/project"
        with_history_key = replace(policy, by_machine_and_repository={
            **policy.by_machine_and_repository, f"natedev.{history_key}": 1,
        })
        for history in (saved.history, frozenset[str](), limits_view.ReadingState.UNAVAILABLE):
            with self.subTest(history=history):
                historical = show(first, run_limits=with_history_key, history=history)
                self.assertIsNone(project(historical).limit)
                meters = {metric.label: (metric.used, metric.limit) for metric in historical.natedev.shared}
                self.assertEqual(meters["project run limit"], (1, 2))
                self.assertEqual(meters["example.invalid/other/project run limit"], (0, 1))
        origin_only = records.RepositoryRunLimits({f"natedev.{history_key}": 1})
        origin_cell = project(show(first, run_limits=origin_only, history=limits_view.ReadingState.OMITTED))
        self.assertEqual((origin_cell.policy, origin_cell.used, origin_cell.limit),
                         (limits_view.CellPolicy.CONFIGURED, 0, 1))

        folder = queue.view.waiting[0].folder
        wait_only = limit_wait(1, "project-local", 1, "Alias wait", folder=folder)
        assert isinstance(wait_only.source, queue_view.WaitingBuild)
        waiting_read = replace(queue, view=replace(queue.view, waiting=(wait_only.source.step,)))
        self.assertIn(limits_view.WaitingCheckout(folder, wait_only.source.step.pid, wait_only.source.step.since),
                      limits_view.active_checkouts(waiting_read))
        only_wait = show(first, waits=(wait_only,))
        self.assertIsNone(project(only_wait).limit)
        self.assertIs(project(only_wait).policy, limits_view.CellPolicy.CONFIGURED)
        self.assertEqual(wait_names(project(only_wait).waits), {
            "1 run waits because the run limit for project-local on natedev is full.": ["Alias wait"],
        })
        meters = {metric.label: (metric.used, metric.limit) for metric in only_wait.natedev.shared}
        self.assertEqual(meters["project run limit"], (1, 2))
        ranked: records.RankedWaiter = {**queue.ordered[0][1], "folder": folder, "repo_identity": primary}
        ordered = [(Path("waiter"), ranked)]
        recorded = show(first, waits=(wait_only,), ordered=ordered, origins=limits_view.Origins())
        self.assertEqual(project(recorded).waits, project(only_wait).waits)
        changed_origin = limits_view.Origins(resolved={folder: second_origin})
        conflict = show(first, waits=(wait_only,), ordered=ordered, origins=changed_origin)
        self.assertEqual(conflict.natedev.waits, project(only_wait).waits)
        self.assertFalse(any(row.natedev.waits for row in conflict.repositories))
        fork_gate: records.Reservation = {**fixture_reservation(505, 0, step="gate"), "repo": "project",
                                          "repo_identity": fork, "unit": "fork-owner"}
        joined = project(show(first, fork_gate))
        self.assertEqual((joined.used, joined.limit, joined.runs), (2, 2, ["regular", "fork-owner"]))
        separate = show(first, {**fork_gate, "repo": "project-local"})
        self.assertIsNone(project(separate).limit)
        meters = {metric.label: (metric.used, metric.limit) for metric in separate.natedev.shared}
        self.assertEqual(meters["project run limit"], (1, 2))
        self.assertEqual(meters["project-local run limit"], (1, 1))
        alias_check: records.Reservation = {**fork_gate, "step": "check", "repo": "project-local"}
        checked = show(first, alias_check)
        self.assertIsNone(project(checked).limit)
        self.assertEqual(project(checked).other_used, 1)
        meters = {metric.label: (metric.used, metric.limit) for metric in checked.natedev.shared}
        self.assertEqual(meters["project run limit"], (1, 2))
        self.assertEqual(meters["project-local run limit"], (0, 1))
        alias_gate: records.Reservation = {**fixture_reservation(506, 0, step="gate"), "repo": "project-local",
                                           "repo_identity": fork}
        full = project(show(first, fork_gate, alias_gate, waits=(
            limit_wait(1, "project", 2, "project", folder=folder),
            limit_wait(2, "project-local", 1, "project-local", folder=folder),
        )))
        self.assertEqual(wait_names(full.waits), {
            "1 run waits because the run limit for project on natedev is full.": ["project"],
            "1 run waits because the run limit for project-local on natedev is full.": ["project-local"],
        })
        private = show(first, waits=(
            limit_wait(1, root_key, 1, "First"), limit_wait(2, root_key + "-other", 1, "Second"),
        ))
        self.assertEqual([group.names for group in private.natedev.waits.values()], [["First"], ["Second"]])
        for known in (False, True):
            with self.subTest(alias_resolved=known):
                folder = "/fixture/pending"
                pending: records.Reservation = {**alias_check, "repo_identity": "", "worktree": folder}
                origins = limits_view.Origins(resolved={folder: fork} if known else {})
                quiet = next(row.natedev for row in show(first, pending, origins=origins).repositories
                             if row.name == "quiet")
                self.assertIs(quiet.policy, limits_view.CellPolicy.NO_LIMIT if known
                              else limits_view.CellPolicy.UNRESOLVED)
        for origin in ("", second_origin):
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
        self.assertEqual((unrestricted.used, unrestricted.limit, unrestricted.other_used, unrestricted.policy),
                         (None, None, 1, limits_view.CellPolicy.CONFIGURED))

    def check_page(self, shown: limits_view.BuildLimits) -> None:
        rendered = test_memory_hover.MemoryHoverTests().page_expression(
            "(renderBuildLimits(data.build_limits), $('build-limits-content').innerHTML)",
            {"build_limits": snapshot_codec.snapshot_value(shown)}, limits=True)
        assert isinstance(rendered, str)
        table = ElementTree.fromstring(f"<root>{rendered}</root>")
        headings = table.findall(".//thead/tr/th")
        self.assertIn("2 runs wait behind an earlier step in the queue.", "".join(headings[1].itertext()))
        self.assertIn("1 run waits behind an earlier step in the queue.", "".join(headings[2].itertext()))
        self.assertNotIn("2 waiting", rendered)
        self.assertNotIn("1 waiting", rendered)
        self.assertIsNone(table.find(".//tbody/tr/th/small"))
        quiet = next(row for row in table.findall(".//tbody/tr") if row.findtext("th") == "quiet")
        cells = quiet.findall("td")
        self.assertEqual(cells[0].findtext("span[@class='build-limit-note']"), "No limit")
        self.assertEqual("".join(cells[1].itertext()), "")

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

    def check_hana_claim(self, origin: str, queue: wire.CloudQueueRead) -> None:
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

        alias = replace(route, target="fixture-alias")
        both = limits_view.AwsRoutes((route, alias), frozenset({route.target, alias.target}))
        folder = "/fixture/hana-cloud"
        gate = replace(queue.cloud[0], wait=records.ReadyForAdmission(),
                       identity=replace(queue.cloud[0].identity, folder=folder))
        cloud_queue = replace(queue, cloud=(gate,))
        origins = limits_view.Origins(resolved={folder: origin})

        def aws_cells(routes: limits_view.AwsRoutes | limits_view.ReadingState,
                      occupancy: Mapping[limits_view.HanaRoute, int | limits_view.ReadingState]
                      | limits_view.ReadingState) -> dict[str, limits_view.Cell]:
            saved = limits_view.background(frozenset[str](), shown.aws_money, 1, routes, occupancy, origins)
            limits = limits_view.build_limits(cloud_queue, (), (), {}, {}, saved, UTC)
            return {row.name: row.aws for row in limits.repositories}

        for missing in limits_view.ReadingState:
            with self.subTest(routes=missing):
                self.assertEqual(aws_cells(missing, missing)["hana"], limits_view.Cell())
        cloud_only = aws_cells(limits_view.AwsRoutes(), limits_view.ReadingState.OMITTED)["hana"]
        self.assertEqual((cloud_only.policy, cloud_only.used, cloud_only.runs, cloud_only.other_used),
                         (limits_view.CellPolicy.NO_LIMIT, 1, ["hana"], 0))
        for alias_used in (0, 1):
            with self.subTest(alias_used=alias_used):
                cell = aws_cells(both, {route: 0, alias: alias_used})["hana"]
                self.assertEqual((cell.used, cell.limit), (0, 1) if alias_used == 0 else (None, None))
                self.assertEqual(cell.other_used, 1)
        other = replace(alias, settings=replace(policy, repository_key="https://example.invalid/team/other"))
        across_rows = replace(both, hana=(route, other))
        cells = aws_cells(across_rows, {route: 0, other: 0})
        for name in ("hana", "other"):
            self.assertEqual((cells[name].policy, cells[name].used, cells[name].limit),
                             (limits_view.CellPolicy.UNRESOLVED, None, None))

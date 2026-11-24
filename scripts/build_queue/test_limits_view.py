"""Check the Build limits section with published records and private fixture settings."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from concurrent.futures import Future
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import cast
from unittest import mock
from xml.etree import ElementTree

from scripts.aws_target import hana_box, hana_placement
from scripts.build_queue import admission_protocol as wire, admission_records as records, limits_view, queue_view
from scripts.build_queue import snapshot_codec, test_memory_hover
from scripts.build_queue.admission_test_support import AdmissionFixture, only_admitted
from scripts.build_queue.admission_wire import ReservedCloudSlot
from scripts.build_queue.cloud_progress import NoCloudProgress
from scripts.build_queue.progress_profile import NoCounter
from scripts.build_queue.queue_fixture import fixture_reservation, head_reading, write_memory_config, write_process
from scripts.build_queue.scope_activity import LiveBuildRun
from scripts.build_targets import target_config, targets
from scripts.build_targets.execution_transport import HanaControlTransport
from scripts.buildlog import machine_facts, repository, store
from scripts.lib import conf, process_start
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.mac_test import claim_state, eligibility, mac_test
from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE


def wait_names(groups: Mapping[str, limits_view.WaitGroup]) -> dict[str, list[str]]:
    return {group.words.one: group.names for group in groups.values()}


class BuildLimitsTests(unittest.TestCase):
    hana_origin: str = "https://example.invalid/team/hana"

    def configure_hana(self, fixture: AdmissionFixture, *, maximum: int = 2) -> None:
        config = fixture.root / "build_admission.conf"
        write_memory_config(config, gpu_test_repositories="hana", gpu_test_slots="2")
        _ = config.write_text(config.read_text() +
            f"\nmax_runs.{fixture.server.host}.hana={maximum}\nmax_runs.exempt_unit=natemerge\n")

    def hana_request(self, fixture: AdmissionFixture, step: str = "gate") -> wire.AdmitRequest:
        request = fixture.request(need=0, step=step)
        return replace(request, profile=replace(request.profile, repo="hana",
            repository=records.RepositoryIdentity(self.hana_origin, "hana")))

    def hana_cell(self, reading: records.QueueRead) -> limits_view.Cell:
        saved = limits_view.Background(frozenset({self.hana_origin}), limits_view.Metric("money", None, None))
        shown = limits_view.build_limits(reading, (), (), {}, {}, saved, UTC)
        return next(row.natedev for row in shown.repositories if row.name == "hana")

    def test_counts_include_starts_granted_in_the_published_pass(self) -> None:
        with AdmissionFixture() as fixture:
            self.configure_hana(fixture)
            verify = self.hana_request(fixture, "test")
            verify = replace(verify, identity=replace(verify.identity, verify=records.VerifyCall("test", "verify test", 1, 1)))
            _ = only_admitted(fixture.server.handle_request(verify))
            fixture.allow_next_start()
            grant = only_admitted(fixture.server.handle_request(self.hana_request(fixture)))
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            reply = wire.parse_reply(wire.encode_reply(reply))
            assert isinstance(reply, wire.ViewReply)
            self.assertNotIn(grant.reservation,
                [row.get("reservation_id") for row in reply.reading.explanation.decided_from.reservations])
            cell = self.hana_cell(reply.reading)
            self.assertEqual((cell.used, cell.limit, cell.verify_used), (2, 2, 1))
            self.assertEqual(cell.gpu, limits_view.Metric("gpu", 2, 2))

    def test_one_build_with_a_gate_and_a_verify_slot_counts_once(self) -> None:
        with AdmissionFixture() as fixture:
            self.configure_hana(fixture, maximum=1)
            verify = self.hana_request(fixture, "test")
            verify = replace(verify, identity=replace(verify.identity, verify=records.VerifyCall("test", "verify test", 1, 1)))
            _ = only_admitted(fixture.server.handle_request(verify))
            fixture.allow_next_start()
            gate_request = self.hana_request(fixture)
            gate_request = replace(gate_request, run=wire.RunIdentity(verify.owner.pid, verify.owner.start_time))
            _ = only_admitted(fixture.server.handle_request(gate_request))
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            cell = self.hana_cell(reply.reading)
            self.assertEqual((cell.used, cell.limit, cell.verify_used), (1, 1, 1))
            self.assertEqual(cell.gpu, limits_view.Metric("gpu", 2, 2))

    def test_release_refresh_does_not_count_a_recomputed_start(self) -> None:
        with AdmissionFixture() as fixture:
            self.configure_hana(fixture)
            request = self.hana_request(fixture)
            grant = only_admitted(fixture.server.handle_request(request))
            before, = fixture.server.handle_request(wire.ViewRequest())
            fixture.release(grant)
            self.assertEqual(fixture.server.handle_request(wire.ViewRequest()), [before])
            _ = fixture.run_pass()
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            reading = reply.reading
            self.assertIsInstance(reading.explanation.head, records.NoWaiter)
            self.assertEqual(reading.explanation.decided_from.reservations, ())
            cell = self.hana_cell(reading)
            self.assertEqual((cell.used, cell.limit), (0, 2))
            self.assertEqual(cell.gpu, limits_view.Metric("gpu", 0, 2))

    def test_counts_stay_with_the_reply_after_release_and_replacement(self) -> None:
        with AdmissionFixture() as fixture:
            self.configure_hana(fixture)
            released = only_admitted(fixture.server.handle_request(self.hana_request(fixture)))
            fixture.allow_next_start()
            retained = only_admitted(fixture.server.handle_request(self.hana_request(fixture)))
            third = self.hana_request(fixture)
            wait, = fixture.server.handle_request(third)
            self.assertIsInstance(wait, wire.Wait)
            before, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(before, wire.ViewReply)
            fixture.release(released)
            fixture.allow_next_start()
            _ = fixture.run_pass()
            after, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(after, wire.ViewReply)
            replacement = fixture.server.reservations[third.episode].reservation_id
            for reply, expected in ((before, released.reservation), (after, replacement)):
                occupancy = reply.reading.explanation.local_occupancy
                assert occupancy is not None
                self.assertEqual({row.get("reservation_id") for row in occupancy.reservations},
                                 {expected, retained.reservation})
                cell = self.hana_cell(reply.reading)
                self.assertEqual((cell.used, cell.limit), (2, 2))
                self.assertEqual(cell.gpu, limits_view.Metric("gpu", 2, 2))

    def test_two_gate_steps_share_the_build_owner_from_admission(self) -> None:
        with AdmissionFixture() as fixture:
            self.configure_hana(fixture)
            run = fixture.run_owner()
            _ = (fixture.proc.path / str(run.pid) / "environ").write_bytes(
                f"BUILDLOG_RUN={run.pid}:{run.start_time}\0".encode())
            for step in ("gate", "test-affected"):
                request = self.hana_request(fixture, step)
                write_process(fixture.proc.path, request.owner.pid, parent=run.pid, start=request.owner.start_time)
                _ = only_admitted(fixture.server.handle_request(request))
                fixture.allow_next_start()
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            cell = self.hana_cell(reply.reading)
            self.assertEqual((cell.used, cell.limit), (1, 2))
            self.assertEqual(cell.gpu, limits_view.Metric("gpu", 2, 2))

    def test_new_grant_keeps_the_waiter_build_owner_after_reparenting(self) -> None:
        with AdmissionFixture() as fixture:
            self.configure_hana(fixture, maximum=1)
            run = fixture.run_owner()
            _ = (fixture.proc.path / str(run.pid) / "environ").write_bytes(
                f"BUILDLOG_RUN={run.pid}:{run.start_time}\0".encode())
            first = self.hana_request(fixture)
            write_process(fixture.proc.path, first.owner.pid, parent=run.pid, start=first.owner.start_time)
            _ = only_admitted(fixture.server.handle_request(first))
            fixture.allow_next_start()
            fixture.hold_starts()
            second = self.hana_request(fixture, "test-affected")
            write_process(fixture.proc.path, second.owner.pid, parent=run.pid, start=second.owner.start_time)
            wait, = fixture.server.handle_request(second)
            self.assertIsInstance(wait, wire.Wait)
            write_process(fixture.proc.path, second.owner.pid, start=second.owner.start_time)
            fixture.free(60 * records.GIB)
            _ = fixture.run_pass()
            self.assertIn(second.episode, fixture.server.reservations)
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            occupancy = reply.reading.explanation.local_occupancy
            assert occupancy is not None
            row = next(row for row in occupancy.reservations if row["pid"] == second.owner.pid)
            build_run = row.get("build_run")
            assert isinstance(build_run, LiveBuildRun)
            self.assertEqual((build_run.pid, build_run.start_time), (run.pid, run.start_time))
            cell = self.hana_cell(reply.reading)
            self.assertEqual((cell.used, cell.limit), (1, 1))
            self.assertEqual(cell.gpu, limits_view.Metric("gpu", 2, 2))

    def test_old_reply_shows_restart_note_without_a_count_or_null(self) -> None:
        with AdmissionFixture() as fixture:
            self.configure_hana(fixture)
            _ = only_admitted(fixture.server.handle_request(self.hana_request(fixture)))
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
        message = cast(dict[str, object], json.loads(wire.encode_reply(reply)))
        data = cast(dict[str, object], message["data"])
        reading = cast(dict[str, object], data["reading"])
        explanation = cast(dict[str, object], reading["explanation"])
        del explanation["local_occupancy"]
        old = wire.parse_reply(json.dumps(message))
        assert isinstance(old, wire.ViewReply)
        cell = self.hana_cell(old.reading)
        self.assertIsNone(cell.used)
        self.assertEqual(cell.gpu, limits_view.Metric("gpu", None, 2))
        empty = limits_view.MachineLimits("", (), {})
        section = limits_view.BuildLimits(empty, empty, empty,
            (limits_view.RepositoryRow("hana", natedev=cell),), False, {})
        rendered, tooltip = cast(list[str], test_memory_hover.MemoryHoverTests().page_expression("""(() => {
            renderBuildLimits(data.build_limits);
            return [$('build-limits-content').innerHTML, pageTooltipHtml('limit:cell:natedev:hana', data)];
        })()""", {"build_limits": snapshot_codec.snapshot_value(section)}, limits=True))
        self.assertIn("The admission service needs a restart.", tooltip)
        for output in (rendered, tooltip):
            self.assertNotIn("null", output)
            self.assertNotIn("0 of 2", output)
        self.assertNotIn("build-limit-gpu", rendered)

    def test_mac_only_verify_wait_uses_the_machine_that_holds_the_run(self) -> None:
        base = head_reading()
        reason = records.RunLimitWait(2, 2, "hana", "natedev")
        step = replace(base.view.waiting[0], project="hana", route=target_config.MAC_ONLY, reason=reason,
                       verify=records.VerifyCall("test", "fixture", 1, 1))
        saved = limits_view.Background(frozenset(), limits_view.Metric("money", None, None),
            origins=limits_view.Origins(mac_scopes={step.folder: "hana"}))
        wait = limits_view.VisibleWait(queue_view.WaitingBuild(1, step), "Verify")
        shown = limits_view.build_limits(base, (), (wait,), {}, {}, saved, UTC)
        row, = shown.repositories
        self.assertEqual(wait_names(row.natedev.waits), {limits_view.wait_words(reason).one: ["Verify"]})
        self.assertEqual(row.mac.waits, {})
        self.assertEqual(shown.mac.waits, {})

    def test_mac_claim_and_wait_use_the_source_repository_scope(self) -> None:
        base = head_reading()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "hana"
            env = fixture_git_environment(root)
            _ = subprocess.run(["git", "init", "-q", str(source)], env=env, check=True, capture_output=True,
                               timeout=TEST_HANG_GUARD_SECS)
            origin = "https://example.invalid/team/other"
            _ = subprocess.run(["git", "-C", str(source), "remote", "add", "origin", origin], env=env, check=True,
                               capture_output=True, timeout=TEST_HANG_GUARD_SECS)
            step = replace(base.view.waiting[0], folder=str(source), project="other", route=target_config.MAC_ONLY)
            reading = replace(base, view=replace(base.view, waiting=(step,)))
            scopes = limits_view.refresh_queue_origins(limits_view.Origins(), limits_view.active_checkouts(reading))
            paths = claim_state.StatePaths(root, "mac", platform="macos")
            scope = claim_state.target_claim_scope(paths, str(source))
        claim = limits_view.MacClaim(scope, 1, "builder")
        mac = limits_view.MacReading((claim,), claims_readable=True)
        saved = limits_view.Background(frozenset({origin}), limits_view.Metric("money", None, None), origins=scopes)
        wait = limits_view.VisibleWait(queue_view.WaitingBuild(1, step), "Mac step")
        shown = limits_view.build_limits(reading, (), (wait,), {}, {"builder": "Mac run"}, saved, UTC, mac=mac)
        rows = {row.name: row.mac for row in shown.repositories}
        self.assertEqual((rows["hana"].used, rows["hana"].limit, rows["hana"].runs), (1, 1, ["Mac run"]))
        words = limits_view.wait_words(limits_view.waiting_cause(step))
        self.assertEqual(wait_names(rows["hana"].waits), {words.one: ["Mac step"]})
        self.assertEqual(rows["other"], limits_view.Cell())
        self.assertEqual(shown.mac.waits, {})

    def test_kept_mac_claim_counts_in_the_limit_but_is_not_in_use(self) -> None:
        started = process_start.process_start(os.getpid())
        assert isinstance(started, process_start.Started)
        working: claim_state.RemoteJobClaim = {"claim_id": "working", "episode": "episode", "attempt": "attempt",
            "submitting_session": "build", "pid": os.getpid(), "proc_start": started.start, "what": "test",
            "worktree": "relative", "since": "2026-11-22T12:00:00+00:00", "remote_host": "fixture-host"}
        # The saved owner process is gone, so mac_test keeps the claim for the leftovers of its run.
        kept: claim_state.RemoteJobClaim = {**working, "claim_id": "kept", "proc_start": started.start + "0"}
        self.assertTrue(mac_test.run_is_kept(kept))
        self.assertFalse(mac_test.run_is_kept(working))

        def heading(*records: claim_state.RemoteJobClaim) -> tuple[limits_view.BuildLimits, str]:
            held = tuple(claim_state.HeldClaim(claim_state.WHOLE_MAC, Path(f"/{record['claim_id']}.json"), record)
                         for record in records)
            snapshot = targets.TargetSnapshot(target_config.SshTarget("mac", "macos", Path("mac.conf")),
                                              (targets.Free(),), held, eligibility.NO_BACKOFF)
            with mock.patch.dict(os.environ, {"MAC_TEST_STATE_DIR": "/nonexistent"}):
                mac = limits_view.read_mac(snapshot, limits_view.Origins())
            # A kept claim has no working session, so it never shows under Running.
            self.assertEqual([claim.working_session for claim in mac.claims],
                             ["" if claim.use is limits_view.MacClaimUse.KEPT else "build" for claim in mac.claims])
            shown = limits_view.build_limits(head_reading(), (), (), {}, {}, background, UTC, mac=mac)
            rendered = test_memory_hover.MemoryHoverTests().page_expression("""(() => {
                renderBuildLimits(data.build_limits);
                return $('build-limits-content').innerHTML;
            })()""", {"build_limits": snapshot_codec.snapshot_value(shown)}, limits=True)
            assert isinstance(rendered, str)
            mac_heading = ElementTree.fromstring(f"<root>{rendered}</root>").find(".//th[3]")
            assert mac_heading is not None
            return shown, ElementTree.tostring(mac_heading, encoding="unicode")

        background = limits_view.Background(frozenset(), limits_view.Metric("money", None, None))
        kept_mark = 'class="build-limit-mark kept"'
        alone, alone_html = heading(kept)
        self.assertEqual((alone.mac.unattributed, alone.mac.unattributed_kept), (1, 1))
        self.assertIn("1 kept (leftover of a stopped run)", alone_html)
        self.assertNotIn("in use", alone_html)
        self.assertNotIn("lucide-loader-circle", alone_html)
        self.assertEqual(alone_html.count(kept_mark), 1)
        both, both_html = heading(working, kept)
        self.assertEqual((both.mac.unattributed, both.mac.unattributed_kept), (2, 1))
        self.assertIn("1 in use · 1 kept (leftover of a stopped run)", both_html)
        self.assertEqual((both_html.count("lucide-loader-circle"), both_html.count(kept_mark)), (1, 1))
        two, two_html = heading(kept, {**kept, "claim_id": "kept-2"})
        self.assertEqual((two.mac.unattributed, two.mac.unattributed_kept), (2, 2))
        self.assertIn("2 kept (leftovers of stopped runs)", two_html)
        self.assertEqual(two_html.count(kept_mark), 2)

        def hana_row(limit: int, *uses: limits_view.MacClaimUse) -> tuple[limits_view.Cell, str, str]:
            claims = tuple(limits_view.MacClaim(claim_state.RepositoryScope("hana"), limit, "", use) for use in uses)
            mac = limits_view.MacReading(claims, {"hana": limit}, claims_readable=True)
            shown = limits_view.build_limits(head_reading(), (), (), {}, {}, background, UTC, mac=mac)
            cell = next(row.mac for row in shown.repositories if row.name == "hana")
            rendered, tooltip = cast(list[str], test_memory_hover.MemoryHoverTests().page_expression("""(() => [
                limitCell(data.build_limits.repositories.find(row => row.name === 'hana').mac, data.build_limits.words, 'mac'),
                pageTooltipHtml('limit:cell:mac:hana', data)])()""",
                {"build_limits": snapshot_codec.snapshot_value(shown)}, limits=True))
            return cell, rendered, tooltip

        # A kept claim in a repository row still fills its place in the limit.
        cell, rendered, tooltip = hana_row(1, limits_view.MacClaimUse.KEPT)
        self.assertEqual((cell.used, cell.limit, cell.kept, cell.runs), (1, 1, 1, []))
        self.assertIn("1 of 1", rendered)
        self.assertNotIn("lucide-loader-circle", rendered)
        self.assertEqual(rendered.count(kept_mark), 1)
        self.assertIn("<dt>Places</dt>", tooltip)
        self.assertIn("1 kept (leftover of a stopped run)", tooltip)
        self.assertNotIn("in use", tooltip)

        # Two claims above a limit of 1 leave the row with no limit; the hover still names each kind.
        cell, rendered, tooltip = hana_row(1, limits_view.MacClaimUse.KEPT, limits_view.MacClaimUse.KEPT)
        self.assertEqual((cell.used, cell.limit, cell.kept), (2, None, 2))
        self.assertEqual(rendered.count(kept_mark), 2)
        self.assertIn("2 kept (leftovers of stopped runs)", tooltip)

        cell, rendered, tooltip = hana_row(2, limits_view.MacClaimUse.WORKING, limits_view.MacClaimUse.KEPT)
        self.assertEqual((cell.used, cell.limit, cell.kept), (2, 2, 1))
        self.assertIn("2 of 2", rendered)
        self.assertEqual((rendered.count("lucide-loader-circle"), rendered.count(kept_mark)), (1, 1))
        self.assertIn("1 in use · 1 kept (leftover of a stopped run)", tooltip)

    def test_claude_mac_scope_uses_the_claude_commands_row(self) -> None:
        base = head_reading()
        origin = "https://example.invalid/team/claude_commands"
        step = replace(base.view.waiting[0], project="claude_commands", route=target_config.MAC_ONLY)
        saved = limits_view.Background(frozenset({origin}), limits_view.Metric("money", None, None),
            origins=limits_view.Origins(mac_scopes={step.folder: ".claude"}))
        mac = limits_view.MacReading(limits={".claude": 1}, claims_readable=True)
        shown = limits_view.build_limits(base, (), (), {}, {}, saved, UTC, mac=mac)
        row = next(item for item in shown.repositories if item.name == "claude_commands")
        self.assertEqual((row.name, row.mac.used, row.mac.limit), ("claude_commands", 0, 1))
        claim = limits_view.MacClaim(claim_state.RepositoryScope(".claude"), 1, "")
        wait = limits_view.VisibleWait(queue_view.WaitingBuild(1, step), "Mac step")
        shown = limits_view.build_limits(base, (), (wait,), {}, {}, saved, UTC, mac=replace(mac, claims=(claim,)))
        self.assertNotIn(".claude", {item.name for item in shown.repositories})
        row = next(item for item in shown.repositories if item.name == "claude_commands")
        self.assertEqual((row.name, row.mac.used, row.mac.limit), ("claude_commands", 1, 1))
        words = limits_view.wait_words(limits_view.waiting_cause(step))
        self.assertEqual(wait_names(row.mac.waits), {words.one: ["Mac step"]})

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
            decided_from=replace(reading.explanation.decided_from, build_space=records.BuildSpaceLow(20, 31)),
            local_occupancy=records.LocalAdmissionOccupancy(reservations, {}))
        full = records.RunLimitWait(2, 1, "project", "natedev")
        waiting = (
            records.WaitingStep(1, "", "", "gate", str(copied), now, 0, full),
            records.WaitingStep(2, "", "", "check", "", now, 0, records.BuildSpaceLow(20, 31)),
            records.WaitingStep(3, "", "", "test", "", now, 0, records.NeedsMemory()),
            records.WaitingStep(4, "", "", "check", "", now, 0, records.ReadingUnavailable(),
                                project="hana", route=target_config.PinnedTarget("fixture-aws")),
            records.WaitingStep(5, "", "", "check", str(copied), now, 0, records.ReadyForAdmission(),
                                project="project", route=target_config.MAC_ONLY),
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
            aws_targets=frozenset({"fixture-aws"}), origins=replace(origins, mac_scopes={str(copied): "project"}))

        with mock.patch.object(repository, "output", side_effect=AssertionError("Git read in the feed tick")):
            shown = show(queue)
        self.assertEqual([(row.name, row.quiet) for row in shown.repositories], [
            ("hana", False), ("project", False), ("quiet", True),
        ])
        extra_history = frozenset({"https://example.invalid/team/aaa", "https://example.invalid/team/claude_commands"})
        ordered = limits_view.build_limits(queue, (), limits_view.visible_waits(queue_view.QueueModel(queue, {}), names,
            cloud_names), cloud_names, {}, replace(saved, history=extra_history), UTC)
        self.assertEqual([row.name for row in ordered.repositories], ["claude_commands", "hana", "aaa", "project"])
        self.assertTrue(ordered.repositories[2].quiet)
        self.assertFalse(ordered.repositories[3].quiet)
        hana_row, project_row, quiet = shown.repositories
        cell = project_row.natedev
        self.assertEqual((cell.used, cell.limit, cell.runs, cell.merge_above), (2, 1, ["regular"], ["merge-owner"]))
        self.assertEqual(wait_names(cell.waits), {"1 run waits because the run limit for project on natedev is full.": ["Run limit"]})
        self.assertEqual(quiet.natedev, limits_view.Cell(policy=limits_view.CellPolicy.NO_LIMIT))
        self.assertEqual(shown.natedev.unattributed, 1)
        self.assertEqual(shown.natedev.waits, {})
        self.assertEqual(shown.mac.waits, {})
        self.assertEqual(wait_names(project_row.mac.waits), {"1 run can start at the queue's next check.": ["Mac wait"]})
        with self.subTest(origin="unresolved natedev run"):
            check_quiet: records.Reservation = {
                **fixture_reservation(106, 0, step="check"), "repo": "quiet",
                "repo_identity": "https://example.invalid/team/quiet", "unit": "quiet-check",
            }
            running = show(replace(queue, explanation=replace(explanation,
                local_occupancy=records.LocalAdmissionOccupancy((*reservations, check_quiet), {}))))
            self.assertEqual(running.natedev.unattributed, 1)
            self.assertEqual(next(row.natedev for row in running.repositories if row.name == "quiet"), quiet.natedev)
        unbound = show(replace(queue, view=replace(queue.view, waiting=unbound_waits),
            explanation=replace(explanation,
                local_occupancy=records.LocalAdmissionOccupancy((*reservations, unbound_reservation), {}))))
        meters = {metric.label: (metric.used, metric.limit) for metric in unbound.natedev.shared}
        self.assertEqual(meters["Repository run limit"], (1, 2))
        self.assertEqual(meters["unmatched run limit"], (0, 1))
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
        self.assertEqual(shown.aws.waits, {})
        self.assertEqual(wait_names(hana_row.aws.waits),
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
                local_occupancy=records.LocalAdmissionOccupancy(reservations[1:2], {})))
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
                local_occupancy=records.LocalAdmissionOccupancy((*reservations, check), {}),
                policy=replace(explanation.policy,
                    repository_runs=records.RepositoryRunLimits({"natedev.project": 2}, "merge-owner"))))
        within = show(within_limit)
        merge_cell = next(row.natedev for row in within.repositories if row.name == "project")
        self.assertEqual(merge_cell.merge_above, [])
        self.assertEqual(merge_cell.runs, ["regular", "merge-owner"])
        self.assertEqual((merge_cell.used, merge_cell.other_used), (2, 1))
        gpu = records.GpuTestSlots(2, frozenset({"hana"}))
        verify_wait = records.RunLimitWait(2, 2, "hana", "natedev")
        gpu_runs: tuple[records.Reservation, ...] = tuple(
            {**fixture_reservation(pid, 0, step="test"), "repo": "hana", "repo_identity": hana}
            for pid in (107, 108))
        with_verify = replace(within_limit,
            view=replace(queue.view, waiting=(
                replace(waiting[0], folder="", reason=verify_wait, project="hana"),
                waiting[1],
                replace(waiting[2], reason=records.GpuSlotWait(2, 2)),
                *waiting[3:],
            )),
            ordered=[],
            explanation=replace(within_limit.explanation,
                policy=replace(within_limit.explanation.policy, gpu_tests=gpu,
                    repository_runs=records.RepositoryRunLimits({"natedev.hana": 2})),
                local_occupancy=records.LocalAdmissionOccupancy(gpu_runs, {"hana": frozenset({"501-501", "502-502"})})))
        wait_limits = show(with_verify)
        wait_cell = next(row.natedev for row in wait_limits.repositories if row.name == "hana")
        self.assertEqual((wait_cell.used, wait_cell.limit, wait_cell.verify_used), (2, 2, 2))
        self.assertEqual(wait_cell.runs, [])
        self.assertEqual(wait_names(wait_cell.waits), {
            "1 run waits because the run limit for hana on natedev is full.": ["Run limit"],
            "1 run waits because all GPU test places are in use.": ["Memory wait"],
        })
        self.assertEqual(wait_cell.gpu, limits_view.Metric("gpu", 2, 2))
        self.assertTrue(all(metric.label != "gpu" for metric in wait_limits.natedev.shared))
        behind = records.BehindHead("fixture", now, "")
        self.check_page(show(replace(queue, view=replace(queue.view, waiting=(
            replace(waiting[2], reason=behind), replace(waiting[5], reason=behind), replace(waiting[4], reason=behind),
            replace(waiting[3], reason=behind),
        )))))
        self.check_policy_evidence(queue, saved, reservations[0], primary, fork)
        self.check_hana_claim(hana, queue)
        self.check_cache_expiry()
        self.check_mac_claims(queue, saved)

    def check_mac_claims(self, queue: records.QueueRead, saved: limits_view.Background) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, quiet = root / "project", root / "quiet"
            env = fixture_git_environment(root)
            for folder in (source, quiet):
                _ = subprocess.run(["git", "init", "-q", str(folder)], env=env, check=True, capture_output=True,
                                   timeout=TEST_HANG_GUARD_SECS)
                _ = subprocess.run(["git", "-C", str(folder), "remote", "add", "origin",
                                    f"https://example.invalid/team/{folder.name}"], env=env, check=True,
                                   capture_output=True, timeout=TEST_HANG_GUARD_SECS)
            origins = limits_view.Origins().refresh((str(source), str(quiet)))
            settings = root / "mac.conf"
            _ = settings.write_text("linux_only.project=linux_package\nlinux_only.quiet=\n", encoding="utf-8")
            _ = (root / "build_targets.conf").write_text(
                "targets=mac\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac.conf\n" + UNREACHABLE_WAIT_FIXTURE,
                encoding="utf-8")
            claims = root / "claims"
            claims.mkdir()
            since = datetime(2026, 11, 22, 12, tzinfo=UTC)
            block = claim_state.ActiveMacBlock("holder", "fixture", since, since + timedelta(hours=1),
                claim_state.HolderQueue("holder"), claim_state.NoShowrunner(), "not_configured",
                claim_state.FreeMessageNotNeeded())
            mac_test.write_block(root / "block.json", block)
            started = process_start.process_start(os.getpid())
            assert isinstance(started, process_start.Started)
            record: claim_state.RemoteJobClaim = {"claim_id": "project-claim", "episode": "episode", "attempt": "attempt",
                "submitting_session": "build", "pid": os.getpid(), "proc_start": started.start, "what": "test",
                "worktree": str(source), "since": since.isoformat(), "repo_name": "project",
                "repo_key": "https://example.invalid/team/project",
                "remote_host": "fixture-host"}
            for path in (claims / "one.json", claims / "copy.json"):
                mac_test.write_json(path, record)
            cleanup: claim_state.RemoteJobClaim = {**record, "claim_id": "cleanup", "submitting_session": "cleanup",
                "repo_name": "cleanup", "repo_key": "https://example.invalid/team/cleanup", "cleanup_pending": True}
            mac_test.write_json(claims / "cleanup.json", cleanup)
            whole: claim_state.RemoteJobClaim = {**record, "claim_id": "whole", "worktree": "relative"}
            del whole["repo_name"], whole["repo_key"]
            waits = limits_view.visible_waits(queue_view.QueueModel(queue, {}), {}, {})

            def show(snapshot: targets.TargetSnapshot) -> limits_view.BuildLimits:
                mac = limits_view.read_mac(snapshot, origins)
                return limits_view.build_limits(queue, (snapshot,), waits, {},
                    {"build": "Build name", "cleanup": "Cleanup name"}, saved, UTC, mac=mac)

            with (mock.patch.dict(os.environ, {"CONF_DIR": str(root), "MAC_TEST_STATE_DIR": str(root)}),
                  mock.patch.object(mac_test, "holder_name", return_value="holder"),
                  mock.patch.object(targets, "utc_now", return_value=since)):
                target = target_config.configured_ssh_target("mac")
                normal = show(targets.ssh_target_snapshot(target))
                _ = (root / "block.json").write_text("{", encoding="utf-8")
                bad_block = show(targets.ssh_target_snapshot(target))
                _ = subprocess.run(["git", "-C", str(source), "remote", "set-url", "origin",
                                    "https://example.invalid/team/other"], env=env, check=True,
                                    capture_output=True, timeout=TEST_HANG_GUARD_SECS)
                for path in (claims / "one.json", claims / "copy.json"):
                    mac_test.write_json(path, {**record, "repo_key": ""})
                keyless = show(targets.ssh_target_snapshot(target))
                for path in (claims / "one.json", claims / "copy.json"):
                    mac_test.write_json(path, {**record, "repo_key": "https://example.invalid/team/other"})
                mac_test.write_json(claims / "unknown.json",
                                    {**record, "claim_id": "unknown", "repo_key": "https://example.invalid/"})
                unresolved = show(targets.ssh_target_snapshot(target))
                (claims / "unknown.json").unlink()
                mac_test.write_json(root / "run.json", whole)
                full_mac = show(targets.ssh_target_snapshot(target))
                _ = (claims / "corrupt.json").write_text("{", encoding="utf-8")
                bad_claims = show(targets.ssh_target_snapshot(target))
        self.assertEqual(normal.mac.state, "Held until 13:00 UTC")
        self.assertEqual(bad_block.mac.state, "Not available")
        for shown in (normal, bad_block):
            mac_cells = {row.name: row.mac for row in shown.repositories}
            self.assertEqual((mac_cells["project"].used, mac_cells["project"].limit, mac_cells["project"].runs),
                             (1, 1, ["Build name"]))
            self.assertEqual((mac_cells["cleanup"].used, mac_cells["cleanup"].runs), (1, []))
            self.assertEqual((mac_cells["quiet"].used, mac_cells["quiet"].limit), (0, 1))
        # A Mac claim with no repository key holds the whole Mac, so the heading counts it.
        self.assertEqual((keyless.mac.unattributed, keyless.mac.unattributed_note, keyless.mac.waits),
                         (1, "", normal.mac.waits))
        keyless_cells = {row.name: row.mac for row in keyless.repositories}
        self.assertEqual((keyless_cells["project"].used, keyless_cells["cleanup"].used), (None, 1))
        self.assertEqual(unresolved.mac.unattributed, 0)
        self.assertEqual(unresolved.mac.waits, normal.mac.waits)
        unresolved_cells = {row.name: row.mac for row in unresolved.repositories}
        self.assertEqual((unresolved_cells["project"].used, unresolved_cells["cleanup"].used), (2, 1))
        self.assertNotIn("other", unresolved_cells)
        self.assertTrue(mac_test.run_holds_work(cleanup))
        self.assertEqual(full_mac.mac.unattributed, 1)
        self.assertEqual(full_mac.mac.unattributed_note, "")
        self.assertTrue(all(row.mac.limit is None and row.mac.used != 0 for row in full_mac.repositories))
        self.assertEqual(sum(row.mac.used or 0 for row in full_mac.repositories), 2)
        self.assertTrue(all(row.mac == limits_view.Cell(waits=row.mac.waits) for row in bad_claims.repositories))
        self.assertEqual(full_mac.mac.waits, bad_claims.mac.waits)

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
                local_occupancy=records.LocalAdmissionOccupancy(reservations, {})))
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
                meter = next(metric for metric in without_exempt.natedev.shared if metric.label == "project run limit")
                self.assertEqual((meter.used, meter.limit, meter.exempt_runs), (3, 2, 0))
                with_exempt = show(first, conflicting, {**third, "unit": "merge-owner"})
                meter = next(metric for metric in with_exempt.natedev.shared if metric.label == "project run limit")
                self.assertEqual((meter.used, meter.limit, meter.exempt_runs), (3, 2, 1))
        unrestricted = next(row.natedev for row in show({
            **first, "step": "check", "repo": "project-local", "repo_identity": "https://example.invalid/team/upstream",
        }).repositories if row.name == "upstream")
        self.assertEqual((unrestricted.used, unrestricted.limit, unrestricted.other_used, unrestricted.policy),
                         (None, None, 1, limits_view.CellPolicy.CONFIGURED))

    def check_page(self, shown: limits_view.BuildLimits) -> None:
        current, previous = cast(list[str], test_memory_hover.MemoryHoverTests().page_expression("""(() => {
            const limits = data.build_limits;
            renderBuildLimits(limits);
            const current = $('build-limits-content').innerHTML;
            delete limits.mac;
            delete limits.words.mac;
            limits.repositories.forEach(row => { delete row.mac; });
            renderBuildLimits(limits);
            return [current, $('build-limits-content').innerHTML];
        })()""",
            {"build_limits": snapshot_codec.snapshot_value(shown)}, limits=True))
        table = ElementTree.fromstring(f"<root>{current}</root>")
        old_table = ElementTree.fromstring(f"<root>{previous}</root>")
        headings = table.findall(".//thead/tr/th")
        self.assertEqual([heading.text for heading in old_table.findall(".//thead/tr/th")],
                         [headings[index].text for index in (0, 1, 3)])
        for row, old_row in zip(table.findall(".//tbody/tr"), old_table.findall(".//tbody/tr"), strict=True):
            self.assertEqual(row.findtext("th"), old_row.findtext("th"))
            self.assertEqual(["".join(cell.itertext()) for cell in old_row.findall("td")],
                             ["".join(row.findall("td")[index].itertext()) for index in (0, 2)])
        for selector, count in (
            ("th/div[@data-tip='limit:machine:natedev']", 2),
            ("td[@data-tip='limit:cell:mac:project']/span", 1),
            ("td[@data-tip='limit:cell:aws:hana']/span", 1),
        ):
            badge = table.find(f".//{selector}/span[@data-wait-count='{count}']")
            assert badge is not None
            self.assertEqual(badge.findtext("span"), str(count))
            self.assertIsNotNone(badge.find("{http://www.w3.org/2000/svg}svg[@class='lucide lucide-hourglass']"))
        self.assertNotIn("wait behind an earlier step", current)
        self.assertNotIn("Regular step", current)
        self.assertIsNone(table.find(".//tbody/tr/th/small"))
        project = next(row for row in table.findall(".//tbody/tr") if row.findtext("th") == "project")
        self.assertEqual("".join("".join(project.findall("td")[0].itertext()).split()), "2of1")
        self.assertNotIn("regular", current)
        quiet = next(row for row in table.findall(".//tbody/tr") if row.findtext("th") == "quiet")
        cells = quiet.findall("td")
        self.assertIsNotNone(cells[0].find(".//{http://www.w3.org/2000/svg}svg[@class='lucide lucide-infinity']"))
        self.assertEqual("".join(cells[1].itertext()), "")
        self.assertEqual("".join(cells[2].itertext()), "")
        self.assertIn("No limit", current)

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
        hana_routes = (route,)
        routes = limits_view.AwsRoutes(hana_routes, frozenset({"fixture"}))
        refresh = limits_view.LimitsRefresh(10, 10)
        refresh.routes.sample = limits_view.SourceSample(routes, 19, None)
        occupancy: dict[limits_view.HanaRoute, int | limits_view.ReadingState] = {route: 1}
        refresh.hana_occupancy.sample = limits_view.SourceSample(occupancy, 0, hana_routes)
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

        def cloud_limits(routes: limits_view.AwsRoutes | limits_view.ReadingState,
                         occupancy: Mapping[limits_view.HanaRoute, int | limits_view.ReadingState]
                         | limits_view.ReadingState, *,
                         current: wire.CloudQueueRead = cloud_queue) -> limits_view.BuildLimits:
            saved = limits_view.background(frozenset[str](), shown.aws_money, 1, routes, occupancy, origins)
            return limits_view.build_limits(current, (), (), {}, {}, saved, UTC)

        def hana_cell(limits: limits_view.BuildLimits) -> limits_view.Cell:
            return next(row.aws for row in limits.repositories if row.name == "hana")

        for missing in limits_view.ReadingState:
            with self.subTest(routes=missing):
                self.assertEqual(hana_cell(cloud_limits(missing, missing)), limits_view.Cell())
        cloud_only = hana_cell(cloud_limits(limits_view.AwsRoutes(), limits_view.ReadingState.OMITTED))
        self.assertEqual((cloud_only.policy, cloud_only.used, cloud_only.runs, cloud_only.other_used),
                         (limits_view.CellPolicy.NO_LIMIT, 1, ["hana"], 0))
        for alias_used in (0, 1):
            with self.subTest(alias_used=alias_used):
                cell = hana_cell(cloud_limits(both, {route: 0, alias: alias_used}))
                self.assertEqual((cell.used, cell.limit), (0, 1) if alias_used == 0 else (None, None))
                self.assertEqual(cell.other_used, 1)
        other = replace(alias, settings=replace(policy, repository_key="https://example.invalid/team/other"))
        across_rows = replace(both, hana=(route, other))
        across = cloud_limits(across_rows, {route: 0, other: 0})
        cells = {row.name: row.aws for row in across.repositories}
        for name in ("hana", "other"):
            self.assertEqual((cells[name].policy, cells[name].used, cells[name].limit),
                             (limits_view.CellPolicy.UNRESOLVED, None, None))

        with self.subTest(routes="one unreadable Hana route"):
            targets = tuple(target_config.SshTarget(name, "linux", transport.settings) for name in (route.target, alias.target))
            config = conf.ConfigRead(Path("fixture.conf"), {})
            with (mock.patch.object(conf, "read", return_value=config),
                  mock.patch.object(target_config, "read_targets", return_value=targets),
                  mock.patch.object(target_config, "read_execution_transport", return_value=transport),
                  mock.patch.object(hana_placement, "read_settings", side_effect=[policy, ValueError("unreadable route")])):
                partial_routes = limits_view.aws_routes()
            partial = cloud_limits(partial_routes, {route: 1})
            self.assertEqual(hana_cell(partial), limits_view.Cell())
            self.assertEqual(partial.aws.shared[0].used, 1)

        with self.subTest(origin="unresolved AWS run"):
            unknown = replace(gate, owner=queue.cloud[1].owner, identity=replace(gate.identity, folder=""))
            current = replace(cloud_queue, cloud=(gate, unknown))
            for known_routes in (limits_view.AwsRoutes(), routes):
                hidden = cloud_limits(known_routes, {route: 1}, current=current)
                cell = hana_cell(hidden)
                self.assertEqual((cell.used, cell.runs, cell.other_used, cell.other), (None, [], 0, []))
                self.assertEqual(hidden.aws.shared[0].used, 2)
                self.assertEqual(cell.limit, 1 if known_routes is routes else None)
                rendered = test_memory_hover.MemoryHoverTests().page_expression(
                    "limitCell(data.cell, data.words, 'aws')",
                    {"cell": snapshot_codec.snapshot_value(cell), "words": limits_view.PAGE_WORDS}, limits=True)
                assert isinstance(rendered, str)
                self.assertNotIn('class="build-limit-mark ', rendered)
                self.assertNotIn("lucide-loader-circle", rendered)
                self.assertNotIn("running", rendered)


class MachineFactsLineTests(unittest.TestCase):
    """The facts line under each machine heading, from the saved machine facts."""

    def saved(self, written: float = 1000.0) -> machine_facts.SavedFacts:
        return {"version": machine_facts.VERSION, "written": written, "machines": {
            "natedev": machine_facts.facts("read", load=52.71, cpus=32, battery={"percent": 100, "mains": True}),
            "mac": machine_facts.facts("read", link_ms=3.2, load=19.56, cpus=12, battery={"percent": 85, "mains": False}),
            "aws": machine_facts.facts("no_box")}}

    def line(self, saved: machine_facts.SavedFacts | machine_facts.FactsUnread, machine: machine_facts.Machine,
             now: float = 1000.0) -> str:
        return limits_view.facts_line(saved, machine, now, UTC)

    def test_each_machine_shows_its_line(self) -> None:
        saved = self.saved()
        self.assertEqual(self.line(saved, "natedev"), "load 52.7/32 · battery 100% charged · not in use")
        self.assertEqual(self.line(saved, "mac"), "link 3 ms · load 19.6/12 · battery 85% · in use")
        self.assertEqual(self.line(saved, "aws"), "no box running")

    def test_a_charging_battery_on_mains_is_not_in_use(self) -> None:
        saved = self.saved()
        saved["machines"]["natedev"] = machine_facts.facts("read", load=52.71, cpus=32,
                                                           battery={"percent": 80, "mains": True})
        self.assertEqual(self.line(saved, "natedev"), "load 52.7/32 · battery 80% · not in use")

    def test_failures_show_their_words(self) -> None:
        saved = self.saved()
        saved["machines"].update({
            "natedev": machine_facts.facts("read", load=52.71, cpus=32),
            "mac": machine_facts.facts("unreachable", detail="timed out"),
            "aws": machine_facts.facts("failed", detail="ledger evidence is unavailable")})
        self.assertEqual(self.line(saved, "natedev"), "load 52.7/32")
        self.assertEqual(self.line(saved, "mac"), "link unreachable")
        self.assertEqual(self.line(saved, "aws"), "facts unreadable")
        saved["machines"]["mac"] = machine_facts.facts("load_unreadable", link_ms=0.4,
                                                       detail="Permission denied (publickey).")
        self.assertEqual(self.line(saved, "mac"), "link <1 ms · load unreadable")
        saved["machines"]["natedev"] = machine_facts.facts("read", load=52.71, cpus=32, battery="unreadable")
        self.assertEqual(self.line(saved, "natedev"), "load 52.7/32 · battery unreadable")

    def test_stale_and_missing_facts(self) -> None:
        saved = self.saved(written=datetime(2026, 11, 22, 14, 5, tzinfo=UTC).timestamp())
        fresh = saved["written"] + machine_facts.STALE_SECONDS
        self.assertEqual(self.line(saved, "mac", fresh), "link 3 ms · load 19.6/12 · battery 85% · in use")
        self.assertEqual(self.line(saved, "mac", fresh + 1), "facts not sampled since 14:05")
        self.assertEqual(self.line(machine_facts.FactsUnread.VALUE, "natedev"), "facts not sampled")
        del saved["machines"]["aws"]
        self.assertEqual(self.line(saved, "aws", saved["written"]), "facts not sampled")

    def test_build_limits_carries_the_lines_to_the_page(self) -> None:
        reading = head_reading()
        saved = self.saved(written=reading.explanation.now)
        background = limits_view.Background(frozenset(), limits_view.Metric("money", None, None))
        shown = limits_view.build_limits(reading, (), (), {}, {}, background, UTC, facts=saved)
        self.assertEqual((shown.natedev.facts, shown.mac.facts, shown.aws.facts),
                         ("load 52.7/32 · battery 100% charged · not in use",
                          "link 3 ms · load 19.6/12 · battery 85% · in use", "no box running"))
        missing = limits_view.build_limits(reading, (), (), {}, {}, background, UTC)
        self.assertEqual(missing.aws.facts, "facts not sampled")
        rendered = test_memory_hover.MemoryHoverTests().page_expression("""(() => {
            renderBuildLimits(data.build_limits);
            return $('build-limits-content').innerHTML;
        })()""", {"build_limits": snapshot_codec.snapshot_value(shown)}, limits=True)
        assert isinstance(rendered, str)
        self.assertIn('<div class="build-limit-facts">link 3 ms · load 19.6/12 · battery 85% · in use</div>', rendered)
        self.assertIn('<div class="build-limit-facts">no box running</div>', rendered)

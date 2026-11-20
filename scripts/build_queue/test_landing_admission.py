"""Exercise landing precedence through private admission sockets."""

from __future__ import annotations

from contextlib import nullcontext, redirect_stderr
from dataclasses import replace
import io
import json
import os
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_records as records
from scripts.build_queue import first_step, memory_feed, placement, queue_place, queue_view
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, only_admitted
from scripts.build_queue.test_placement import (PlacementFixture, adopt_assignment, assignment_for, consume_permission,
    placement_request, publish_candidate)
from scripts.buildlog import repository, store, treekey
from scripts.lib import build_folders
from scripts.lib.process_start import Started
from scripts.lint import memory_admit as gate
from scripts.lint.memory_test_support import write_pressure_reading
from scripts.production import land_queue, land_runs, staff


REPOSITORY = records.RepositoryIdentity("https://github.com/natepiano/claude_commands", "claude_commands")
COMMIT = "a" * 40
WAIT_TEXT = "waits for the merge test"


def queue_land() -> None:
    _ = land_queue.take_next(os.getpid())
    land_queue.add("builds first", batch="", commits=[COMMIT], first=False, before=None)


class LandingAdmissionTests(unittest.TestCase):
    def test_a_copy_first_step_has_the_landing_reference_before_its_child_starts(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            fixture.configure(**{"program_first_peak_gib.merge-gate": 4.0})
            queue_land()

            def admit(refreshed: queue_place.RefreshedRequest, _output: client.WaitOutput) -> wire.Admitted:
                request = refreshed.refresh()
                grant = only_admitted(fixture.connect(request).replies())
                reference = request.identity.landing_test
                assert isinstance(reference, land_runs.LandingTestReference)
                self.assertEqual(os.environ[land_runs.TEST_REFERENCE_ENV], reference.token)
                self.assertIn(reference, land_runs.running_test_references())
                return grant

            command = ("python3", "/fixture/merge_gate.py", "--merge-branch", "merge", "--unit-branch", "topic",
                       "--checkpoint", COMMIT, "--previous-checkpoint", "b" * 40, "--unit", "queue", "--phase", "44eb")
            with (patch.object(client, "CLOCK", fixture.clock),
                  patch.object(repository, "repository_key", return_value=REPOSITORY.key),
                  patch.object(build_folders, "tool_folder", return_value=nullcontext(fixture.temporary)),
                  patch.object(queue_place, "request_place", side_effect=admit),
                  patch.object(queue_place, "release_place", side_effect=fixture_release(fixture))):
                with first_step.admitted_run(command, fixture.root) as admission:
                    self.assertIsInstance(admission.grant, wire.Admitted)
                    admission.handoff(fixture.root)
                    fixture.allow_next_start()
                    child = fixture.request(step="merge-gate", need=GIB)
                    sizing = records.HistoryPeakSizing(GIB, records.FirstProgramPeak(GIB), fixture.clock.now())
                    profile = records.ProgramProfile("merge-gate", "fixture", child.cwd, sizing)
                    child = client.step_request(["merge-gate"], child.cwd, child.owner, child.since, "",
                                                child.episode, profile)
                    fixture.release(only_admitted(fixture.connect(child).replies()))
            self.assertFalse(land_runs.running_test_references())
            self.assertEqual(fixture.caller_reservations(), {})

    def test_a_landing_that_enters_mid_pass_holds_local_and_cloud_grants(self) -> None:
        for cloud in (False, True):
            with self.subTest(cloud=cloud), AdmissionFixture(skip_idle_spacing=True) as fixture:
                request = fixture.request(step="test", need=GIB)
                request = replace(request, profile=replace(request.profile, repository=REPOSITORY),
                                  place=wire.CloudPlace("aws") if cloud else wire.LocalPlace())
                decide = gate.admission_pass
                queued = False

                def decide_then_queue(inputs: records.QueueInputs, now: float, policy: records.AdmissionPolicy) -> gate.PassResult:
                    nonlocal queued
                    result = decide(inputs, now, policy)
                    if not queued:
                        queue_land()
                        queued = True
                    return result

                with patch.object(gate, "admission_pass", side_effect=decide_then_queue):
                    replies = fixture.connect(request).replies()
                assert isinstance(reply := replies[-1], wire.Wait), replies
                self.assertEqual(reply.reason, records.WaitsForLandingTest())
                self.assertEqual(fixture.caller_reservations(), {})

    def test_a_stopped_test_reclaims_its_queue_time_after_a_newer_waiter(self) -> None:
        for landing in (True, False):
            with self.subTest(landing=landing), AdmissionFixture(skip_idle_spacing=True) as fixture:
                if landing:
                    queue_land()
                fixture.hold_starts()
                old = fixture.request(step="test", need=GIB, since=fixture.clock.now() - 40)
                old = replace(old, profile=replace(old.profile, repository=REPOSITORY))
                member = SimpleNamespace(id=staff.StaffId("fixed-test-owner"))

                def staff_member(session: str) -> SimpleNamespace | None:
                    return member if session in ("old-session", "new-session") else None

                with (patch.object(staff, "local_by_current_session", side_effect=staff_member),
                      patch.object(treekey, "tree_key", return_value="test-tree")):
                    old = replace(old, identity=replace(old.identity, session_id="old-session"))
                    peer = fixture.connect(old)
                    self.assertIsInstance(peer.replies()[-1], wire.Wait)
                    if not landing:
                        boot_file = fixture.proc.path / "sys/kernel/random/boot_id"
                        boot_file.parent.mkdir(parents=True)
                        _ = boot_file.write_text("fixture-boot\n")
                        stop_log = store.root() / store.host_name() / "hold-builds-fixture.jsonl"
                        stop_log.parent.mkdir(parents=True, exist_ok=True)
                        _ = stop_log.write_text(json.dumps({"boot_id": "fixture-boot", "action": "stopped",
                            "pid": old.owner.pid, "proc_start": old.owner.start_time}) + "\n")
                        os.utime(stop_log, (fixture.clock.now(), fixture.clock.now()))
                    peer.close()
                    fixture.end_owner(old.owner)
                    fixture.restart()
                    newer = fixture.request(step="test", need=GIB, since=fixture.clock.now() - 2)
                    newer = replace(newer, profile=old.profile)
                    new_peer = fixture.connect(newer)
                    self.assertIsInstance(new_peer.replies()[-1], wire.Wait)
                    again = fixture.request(step="test", need=GIB)
                    again = replace(again, profile=old.profile, identity=replace(again.identity, session_id="new-session"))
                    retry = fixture.connect(again)
                    self.assertIsInstance(retry.replies()[-1], wire.Wait)
                    view, = fixture.connect(wire.ViewRequest()).replies()
                    assert isinstance(view, wire.ViewReply)
                    self.assertEqual([(row["pid"], row["since"]) for _, row in view.reading.ordered],
                                     [(again.owner.pid, old.since), (newer.owner.pid, newer.since)])
                    if landing:
                        land_queue.remove("builds first")
                    write_pressure_reading(fixture.root / "pressure")
                    _ = fixture.run_pass()
                    self.assertEqual(list(fixture.caller_reservations()), [again.episode])
                    fixture.release(only_admitted(retry.replies()))

    def test_a_relative_local_clone_waits_and_its_repository_is_resolved_once(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            source = fixture.root / "source"
            clone = fixture.root / "clone"
            git("init", "-q", str(source))
            git("-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--allow-empty", "-qm", "Source")
            git("-C", str(source), "remote", "add", "origin", REPOSITORY.key)
            git("clone", "-q", "--no-local", str(source), str(clone))
            git("-C", str(clone), "remote", "set-url", "origin", "../source")
            self.assertFalse((clone / ".git/objects/info/alternates").exists())
            workdir = clone / "nested"
            workdir.mkdir()
            queue_land()
            request = replace(fixture.request(step="test", need=GIB), cwd=str(workdir))
            with patch.object(repository, "repository_key", wraps=repository.repository_key) as lookup:
                wait, = fixture.connect(request).replies()
                assert isinstance(wait, wire.Wait)
                self.assertEqual(wait.reason, records.WaitsForLandingTest())
                resolved = lookup.call_count
                self.assertGreater(resolved, 0)
                _ = fixture.run_pass()
                _ = fixture.run_pass()
                self.assertEqual(lookup.call_count, resolved)

    def test_four_older_tests_wait_and_keep_their_order_after_the_land(self) -> None:
        with AdmissionFixture(skip_idle_spacing=True) as fixture:
            queue_land()
            requests: list[wire.AdmitRequest] = []
            for index, step in enumerate(("merge-gate", "targeted-test", "test", "t")):
                request = fixture.request(need=GIB, step=step, since=fixture.clock.now() - 40 + index)
                requests.append(replace(request, profile=replace(request.profile, repository=REPOSITORY)))
            peers = [fixture.connect(request) for request in reversed(requests)]
            for peer in peers:
                reply = peer.replies()[-1]
                assert isinstance(reply, wire.Wait)
                self.assertEqual((reply.reason, reply.text), (records.WaitsForLandingTest(), WAIT_TEXT))
            self.assertEqual(fixture.caller_reservations(), {})
            view, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(view, wire.ViewReply)
            self.assertIsInstance(view.reading.explanation.head, records.NoWaiter)
            self.assertEqual([row.since for row in view.reading.view.waiting], [request.since for request in requests])
            reason = view.reading.view.waiting[0].reason
            self.assertEqual(records.reason_phrase(reason, ()), WAIT_TEXT)
            self.assertEqual(memory_feed.wait_why(reason), "the merge test")
            other = only_admitted(fixture.connect(fixture.request(need=GIB, step="test")).replies())
            fixture.release(other)
            fixture.allow_next_start()
            with patch.object(land_runs, "process_start", return_value=Started("10")):
                self.assertEqual(land_queue.take_next(123), "builds first")
                with land_runs.landing([COMMIT]):
                    request = fixture.request(need=GIB, step="merge-gate")
                    sizing = records.HistoryPeakSizing(GIB, records.FirstProgramPeak(GIB), fixture.clock.now())
                    profile = records.ProgramProfile("merge-gate", "fixture", request.cwd, sizing)
                    with (patch.object(client, "CLOCK", fixture.clock),
                          patch.object(repository, "repository_key", return_value=REPOSITORY.key)):
                        landing = client.step_request(["merge-gate"], request.cwd, request.owner, request.since,
                                                      "", request.episode, profile)
                    self.assertEqual(landing.profile.repository, REPOSITORY)
                    self.assertEqual(landing.identity.landing_test, land_runs.test_reference())
                    grant = only_admitted(fixture.connect(landing).replies())
                    self.assertEqual(list(fixture.caller_reservations()), [landing.episode])
                    fixture.release(grant)
                    _ = fixture.run_pass()
                    self.assertEqual(fixture.caller_reservations(), {})
            for index, request in enumerate(requests):
                fixture.allow_next_start()
                _ = fixture.run_pass()
                self.assertEqual(list(fixture.caller_reservations()), [request.episode])
                fixture.release(only_admitted(peers[-index - 1].replies()))

    def test_cloud_work_reports_the_landing_wait_and_yields_the_slot(self) -> None:
        with AdmissionFixture() as fixture, patch.object(land_runs, "process_start", return_value=Started("10")):
            queue_land()
            request = fixture.request(step="merge-gate")
            request = replace(request, profile=replace(request.profile, repository=REPOSITORY), place=wire.CloudPlace("aws"))
            wait, = fixture.connect(request).replies()
            assert isinstance(wait, wire.Wait)
            self.assertEqual(wait.reason, records.WaitsForLandingTest())
            with redirect_stderr(io.StringIO()) as output:
                client.WaitOutput()(wait)
            self.assertIn(WAIT_TEXT, output.getvalue())
            view, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(view, wire.ViewReply) and isinstance(view.reading, wire.CloudQueueRead)
            row, = view.reading.cloud
            self.assertEqual(row.wait, records.WaitsForLandingTest())
            work = queue_view.cloud_work(row, fixture.clock.now(), 1, queue_view.CloudPushIntervalUnavailable("fixture"))
            self.assertEqual(work.state, WAIT_TEXT)
            _ = land_queue.take_next(123)
            with land_runs.landing([COMMIT]):
                landing = fixture.request(step="merge-gate")
                identity = replace(landing.identity, landing_test=land_runs.test_reference())
                landing = replace(landing, identity=identity, profile=request.profile, place=request.place)
                _ = only_admitted(fixture.connect(landing).replies())
                self.assertEqual(list(fixture.caller_reservations()), [landing.episode])

    def test_routed_tests_wait_before_assignment_and_before_start_permission(self) -> None:
        with PlacementFixture() as fixture:
            earlier = placement_request(fixture)
            earlier = replace(earlier, profile=replace(earlier.profile, repository=REPOSITORY))
            _ = publish_candidate(fixture, earlier)
            assignment = assignment_for(fixture, earlier)
            claim = adopt_assignment(fixture, earlier, assignment)
            queue_land()
            reply, = fixture.server.handle_request(consume_permission("unit-start", assignment, claim))
            assert isinstance(reply, wire.DispatchStatus)
            self.assertEqual(reply.state, placement.ClosedAssignment(WAIT_TEXT))
            self.assertIsInstance(reply.permission, placement.PermissionAvailable)
            later = placement_request(fixture)
            later = replace(later, profile=replace(later.profile, repository=REPOSITORY))
            _ = publish_candidate(fixture, later)
            fixture.clock.advance(1)
            waiting, = fixture.connect(later).replies()
            assert isinstance(waiting, wire.PlacementWait) and isinstance(waiting.wait, wire.Wait)
            self.assertEqual(waiting.wait.reason, records.WaitsForLandingTest())
            self.assertNotIn(later.episode, fixture.server.current_assignments)


def git(*arguments: str) -> None:
    _ = subprocess.run(["git", *arguments], check=True, capture_output=True, timeout=10)


def fixture_release(fixture: AdmissionFixture):
    def release(reservation: str, status: int, _timing: wire.CleanupTiming) -> wire.Ok:
        response, = fixture.server.handle_request(wire.ReleaseRequest("release-copy", reservation, status))
        assert isinstance(response, wire.Ok)
        return response
    return release

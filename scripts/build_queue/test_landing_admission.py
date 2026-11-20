"""Exercise landing precedence through private admission sockets."""

from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import replace
import io
import unittest
from unittest.mock import patch

from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_records as records
from scripts.build_queue import memory_feed, placement, queue_view
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, only_admitted
from scripts.build_queue.test_placement import (PlacementFixture, adopt_assignment, assignment_for, consume_permission,
    placement_request, publish_candidate)
from scripts.buildlog import repository
from scripts.lib.process_start import Started
from scripts.production import land_queue, land_runs


REPOSITORY = records.RepositoryIdentity("https://github.com/natepiano/claude_commands", "claude_commands")
COMMIT = "a" * 40
WAIT_TEXT = "waits for the merge test"


def queue_land() -> None:
    land_queue.add("builds first", batch="", commits=[COMMIT], first=False, before=None)


class LandingAdmissionTests(unittest.TestCase):
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

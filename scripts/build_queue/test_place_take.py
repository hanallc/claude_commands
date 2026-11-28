"""Rules 13 and 20: a new run of a unit takes the place of its waiting run or of its failed run."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import cast
import unittest

from scripts.build_queue import admission_protocol as wire, admission_records as records, admission_service as service
from scripts.build_queue import admission_wire as status_wire, placement, execution_identity
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, NotVerifyCall, StepJob, VerifyCall
from scripts.build_hold.owning_session import NoOwningSession, OwningSession
from scripts.build_targets.target_config import PinnedTarget
from scripts.build_queue.admission_test_support import AdmissionFixture, GIB, only_admitted
from scripts.build_queue.test_placement import (PlacementFixture, adopt_assignment, assignment_for,
    consume_permission, local_candidate, placement_request, publish_candidate)
from scripts.mac_test.claim_state import WholeMacScope
from scripts.lint import memory_admit as gate

UNIT = "queue"
JOB = "test-affected"
REPLACED = wire.Refused(status_wire.REPLACED_REASON)


def place_rules(fixture: AdmissionFixture, *, max_times: int = 1, minutes: int = 30, exempt: str = "") -> None:
    """Write this test's own rule values into the private admission configuration."""
    path = fixture.root / "build_admission.conf"
    lines = [f"# whole: 0..\nreplace_after_fail.max_times={max_times}",
             f"# whole: 1..\nreplace_after_fail.time_limit_minutes={minutes}"]
    if exempt:
        lines += ["# whole: 1..\nmax_runs.natedev.fixture=9", f"max_runs.exempt_unit={exempt}"]
    _ = path.write_text(path.read_text() + "\n".join(lines) + "\n")
    fixture.restart()


def unit_request(fixture: AdmissionFixture, run: wire.RunIdentity, *, unit: str = UNIT, repository: str = "fixture",
                 job: str = JOB, since: float | None = None, owner: wire.Owner | None = None,
                 step: str = "nextest", episode: str = "") -> wire.AdmitRequest:
    """A step of the unit; a verify job comes as verify.sh names it, and "" is a step with no job."""
    request = fixture.request(run=run, since=since, owner=owner, step=step, episode=episode)
    step_job = StepJob(job.removeprefix("verify-")) if job else GATE_JOB_NOT_RECORDED
    return replace(request, identity=replace(request.identity, unit=unit, job=step_job),
                   profile=replace(request.profile, repo=repository))


def unit_place(fixture: AdmissionFixture, run: wire.RunIdentity) -> status_wire.UnitPlace:
    place = fixture.server.run_places[records.run_owner(run.pid, run.start_time)]
    line = place.unit_place
    assert isinstance(line, status_wire.UnitPlace)
    return line


def run_since(fixture: AdmissionFixture, run: wire.RunIdentity) -> float:
    return fixture.server.run_places[records.run_owner(run.pid, run.start_time)].since


def wait_in_line(fixture: AdmissionFixture, request: wire.AdmitRequest) -> None:
    fixture.free(0)
    fixture.hold_starts()
    reply, = fixture.server.handle_request(request)
    assert isinstance(reply, wire.Wait), reply


def first_step(fixture: AdmissionFixture, request: wire.AdmitRequest, status: int) -> wire.Admitted:
    """Grant the step and release it with this status."""
    fixture.free(60 * GIB)
    fixture.allow_next_start()
    grant = only_admitted(fixture.server.handle_request(request))
    assert fixture.server.handle_request(wire.ReleaseRequest(f"release-{grant.reservation}",
                                                             grant.reservation, status)) == [wire.Ok()]
    return grant


def failed_run(fixture: AdmissionFixture, *, status: int = 1,
               job: str = JOB) -> tuple[wire.RunIdentity, wire.AdmitRequest]:
    """A run of the unit that fails at its first step and ends."""
    run = fixture.run_owner()
    request = unit_request(fixture, run, job=job)
    _ = first_step(fixture, request, status)
    fixture.end_run(run)
    return run, request


def run_steps(fixture: AdmissionFixture, run: wire.RunIdentity, *steps: tuple[str, int]) -> float:
    """Grant and release each step of one run in order, and give the arrival time of the run.

    verify.sh sends each step of a run with one owner and one arrival time.
    """
    owner, since = fixture.proc.spawn(), fixture.clock.now()
    for step, status in steps:
        _ = first_step(fixture, unit_request(fixture, run, owner=owner, since=since, step=step, episode=step), status)
        fixture.clock.advance(1)
    return since


def held(fixture: AdmissionFixture) -> tuple[records.HeldPlace, ...]:
    return fixture.server.held_places


class WaitingRunTests(unittest.TestCase):
    """Rule 13: a unit has one waiting run for each repository."""

    def test_a_new_run_takes_the_waiting_arrival_and_the_waiting_run_gets_the_replaced_answer(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            fixture.free(0)
            fixture.hold_starts()
            old_run = fixture.run_owner()
            old = unit_request(fixture, old_run)
            peer = fixture.connect(old)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            fixture.clock.advance(10)
            new_run = fixture.run_owner()
            new = unit_request(fixture, new_run)
            self.assertIsInstance(fixture.server.handle_request(new)[0], wire.Wait)
            self.assertEqual(peer.replies(), [REPLACED])
            self.assertEqual(run_since(fixture, new_run), old.since)
            self.assertEqual(unit_place(fixture, old_run).replaced_by, new_run)
            self.assertEqual(unit_place(fixture, new_run).took_place, "waiting")
            self.assertEqual(fixture.queue_pids(), [new.owner.pid])
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            step, = reply.reading.view.waiting
            self.assertEqual(step.took_place, "waiting")

    def test_a_replaced_run_with_no_open_request_gets_the_replaced_answer_at_its_next_request(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            old_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, old_run))
            fixture.clock.advance(5)
            wait_in_line(fixture, unit_request(fixture, fixture.run_owner()))
            self.assertEqual(fixture.server.handle_request(unit_request(fixture, old_run)), [REPLACED])

    def test_the_replaced_mark_stays_after_a_reload(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            old_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, old_run))
            new_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, new_run))
            fixture.restart()
            self.assertEqual(unit_place(fixture, old_run).replaced_by, new_run)
            self.assertEqual(fixture.server.handle_request(unit_request(fixture, old_run)), [REPLACED])

    def test_another_unit_another_repository_or_the_exempt_unit_replaces_no_run(self) -> None:
        cases = {"another unit": ("other", "fixture", UNIT), "another repository": (UNIT, "other", UNIT),
                 "exempt unit": ("natemerge", "fixture", "natemerge")}
        for name, (unit, repository, first_unit) in cases.items():
            with self.subTest(name), AdmissionFixture() as fixture:
                place_rules(fixture, exempt="natemerge")
                old_run = fixture.run_owner()
                old = unit_request(fixture, old_run, unit=first_unit)
                wait_in_line(fixture, old)
                fixture.clock.advance(5)
                new_run = fixture.run_owner()
                new = unit_request(fixture, new_run, unit=unit, repository=repository)
                wait_in_line(fixture, new)
                self.assertEqual(run_since(fixture, new_run), new.since)
                self.assertIn(old.episode, fixture.server.waiters)
                line = fixture.server.run_places[records.run_owner(old_run.pid, old_run.start_time)].unit_place
                self.assertNotIsInstance(getattr(line, "replaced_by", None), wire.RunIdentity)

    def test_a_running_run_is_not_replaced(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            old_run = fixture.run_owner()
            _ = only_admitted(fixture.server.handle_request(unit_request(fixture, old_run)))
            fixture.clock.advance(5)
            new_run = fixture.run_owner()
            new = unit_request(fixture, new_run)
            wait_in_line(fixture, new)
            self.assertEqual(run_since(fixture, new_run), new.since)
            self.assertIsInstance(unit_place(fixture, old_run).replaced_by, status_wire.NotReplaced)
            self.assertEqual(unit_place(fixture, old_run).reservations, 1)

    def test_a_replaced_run_makes_no_held_place_when_it_ends(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            old_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, old_run))
            wait_in_line(fixture, unit_request(fixture, fixture.run_owner()))
            fixture.end_run(old_run)
            self.assertFalse(held(fixture))


def mac_step(fixture: PlacementFixture, run: wire.RunIdentity) -> wire.PlacementRequest:
    """A Mac step of the unit's run."""
    request = placement_request(fixture)
    return replace(request, run=run, identity=replace(request.identity, unit=UNIT, job=StepJob(JOB)))


def start_on_mac(fixture: PlacementFixture, request: wire.PlacementRequest,
                 ) -> tuple[placement.PlacementAssignment, placement.ProviderClaimReference]:
    """Assign the step to the Mac and start it there."""
    _ = publish_candidate(fixture, request)
    assignment = assignment_for(fixture, request)
    claim = adopt_assignment(fixture, request, assignment)
    reply, = fixture.server.handle_request(consume_permission(f"consume-{request.episode}", assignment, claim))
    assert isinstance(reply, wire.FirstAuthorization), reply
    return assignment, claim


def finish_on_mac(fixture: PlacementFixture, request: wire.PlacementRequest, assignment: placement.PlacementAssignment,
                  claim: placement.ProviderClaimReference, status: int) -> None:
    """Report the result of the Mac step."""
    _ = publish_candidate(fixture, request)
    _ = fixture.server.handle_request(wire.ExecutionReport(f"completed-{request.episode}", assignment, claim,
        execution_identity.ExecutionNotStarted(), placement.AttemptCompleted(status), placement.DispatchPathSettled()))


class FailedFirstStepTests(unittest.TestCase):
    """Rule 20: a run that fails at its only step, or at a step before its tests, keeps its place for one new run."""

    def test_a_new_run_takes_the_arrival_of_a_failed_first_step(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            failed, request = failed_run(fixture)
            kept, = held(fixture)
            self.assertEqual((kept.unit, kept.repository, kept.job, kept.since, kept.replaces, kept.ended),
                             (UNIT, "fixture", JOB, request.since, 1, "failed"))
            self.assertEqual(kept.failed_run, records.run_owner(failed.pid, failed.start_time))
            self.assertEqual(kept.ends_at, kept.failed_at + 30 * 60)
            fixture.clock.advance(60)
            new_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, new_run))
            self.assertEqual(run_since(fixture, new_run), request.since)
            self.assertEqual(unit_place(fixture, new_run).replaces, 1)
            self.assertEqual(unit_place(fixture, new_run).took_place, "failed")
            self.assertFalse(held(fixture))

    def test_a_second_failure_loses_the_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            _ = failed_run(fixture)
            fixture.clock.advance(60)
            second = fixture.run_owner()
            _ = first_step(fixture, unit_request(fixture, second), 1)
            fixture.end_run(second)
            self.assertFalse(held(fixture))
            fixture.clock.advance(60)
            third = fixture.run_owner()
            request = unit_request(fixture, third)
            wait_in_line(fixture, request)
            self.assertEqual(run_since(fixture, third), request.since)

    def test_a_second_red_affected_gate_rejoins_the_line_after_a_reload(self) -> None:
        with PlacementFixture() as fixture:
            place_rules(fixture)

            def request_gate() -> wire.PlacementRequest:
                request = placement_request(fixture, session=NoOwningSession())
                sizing = records.HistoryPeakSizing(8 * GIB, records.FirstProgramPeak(8 * GIB), fixture.clock.now())
                profile = records.ProgramProfile(JOB, "claude_commands", str(fixture.root), sizing)
                return replace(request, profile=profile,
                    identity=replace(request.identity, unit=UNIT, step=JOB, job=StepJob(JOB)),
                    constraints=replace(request.constraints, candidates=(local_candidate(fixture),), command_kind="final"),
                    route=PinnedTarget("natedev"), prerequisite=placement.TokenNotRequired())

            def start_gate(request: wire.PlacementRequest) -> wire.LocalPlacementGranted:
                fixture.allow_next_start()
                reply, = fixture.server.handle_request(request)
                assert isinstance(reply, wire.LocalPlacementGranted), reply
                return reply

            def fail_gate(request: wire.PlacementRequest, grant: wire.LocalPlacementGranted) -> None:
                assert isinstance(request.run, wire.RunIdentity)
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "release-" + request.episode, grant.grant.reservation, 1)), [wire.Ok()])
                line = unit_place(fixture, request.run)
                self.assertEqual((line.reservations, line.first_status, line.failed_step), (1, 1, ""))
                self.assertEqual(fixture.server.handle_request(wire.CancelPlacement(
                    "cancel-" + request.episode, request.episode, request.owner, "the gate caller ended")), [wire.Ok()])
                fixture.end_run(request.run)
                _ = fixture.run_pass()

            original = request_gate()
            fail_gate(original, start_gate(original))
            kept, = held(fixture)
            self.assertEqual((kept.since, kept.replaces, kept.failed_step), (original.since, 1, JOB))

            fixture.clock.advance(10 * 60)
            retry = request_gate()
            grant = start_gate(retry)
            assert isinstance(retry.run, wire.RunIdentity)
            self.assertEqual(run_since(fixture, retry.run), original.since)
            fixture.clock.advance(10 * 60)
            fixture.restart()
            line = unit_place(fixture, retry.run)
            self.assertEqual((line.replaces, line.took_place), (1, "failed"))

            fixture.clock.advance(54 * 60)
            fail_gate(retry, grant)
            self.assertFalse(held(fixture))

            fixture.clock.advance(130)
            fixture.hold_starts()
            resubmitted = request_gate()
            reply, = fixture.server.handle_request(resubmitted)
            self.assertIsInstance(reply, wire.PlacementWait)
            assert isinstance(resubmitted.run, wire.RunIdentity)
            self.assertEqual(run_since(fixture, resubmitted.run), resubmitted.since)
            self.assertEqual(unit_place(fixture, resubmitted.run).took_place, "")

    def test_a_fail_at_a_lint_step_after_a_passed_step_keeps_the_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            since = run_steps(fixture, run, ("mend", 0), ("clippy", 1))
            self.assertEqual(unit_place(fixture, run).reservations, 2)
            fixture.end_run(run)
            kept, = held(fixture)
            self.assertEqual((kept.since, kept.replaces, kept.ended), (since, 1, "failed"))
            fixture.clock.advance(60)
            new_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, new_run))
            self.assertEqual(run_since(fixture, new_run), since)
            self.assertEqual(unit_place(fixture, new_run).took_place, "failed")
            self.assertFalse(held(fixture))

    def test_a_fail_at_a_lint_step_after_a_test_step_started_keeps_no_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            _ = run_steps(fixture, run, ("mend", 0), ("nextest", 0), ("clippy", 1))
            fixture.end_run(run)
            self.assertFalse(held(fixture))

    def test_a_failed_test_step_after_passed_lint_steps_keeps_no_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            _ = run_steps(fixture, run, ("mend", 0), ("clippy", 0), ("nextest", 101))
            fixture.end_run(run)
            self.assertFalse(held(fixture))

    def test_a_lint_grant_given_back_with_a_failed_status_and_then_passed_keeps_no_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            owner, since = fixture.proc.spawn(), fixture.clock.now()
            _ = first_step(fixture, unit_request(fixture, run, owner=owner, since=since, step="mend", episode="mend"), 0)
            # The caller gives the clippy grant back before its command runs, then waits again for clippy.
            for episode, status in (("given-back", 1), ("ran", 0)):
                fixture.clock.advance(1)
                _ = first_step(fixture, unit_request(fixture, run, owner=owner, since=since, step="clippy",
                                                     episode=episode), status)
            fixture.end_run(run)
            self.assertFalse(held(fixture))

    def test_a_grant_given_back_before_its_step_runs_is_not_the_result_of_the_step(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            owner, since = fixture.proc.spawn(), fixture.clock.now()
            # The local hold check fails after the grant, so the caller gives the grant back with status 0.
            _ = first_step(fixture, unit_request(fixture, run, owner=owner, since=since, episode="given-back"), 0)
            fixture.clock.advance(1)
            _ = first_step(fixture, unit_request(fixture, run, owner=owner, since=since, episode="ran"), 101)
            self.assertEqual(unit_place(fixture, run).reservations, 1)
            fixture.end_run(run)
            kept, = held(fixture)
            self.assertEqual((kept.since, kept.ended), (since, "failed"))

    def test_a_first_step_grant_handed_off_to_the_command_is_not_a_step(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            # copies.py holds the first grant while it prepares the copy, then hands it off with status 0.
            prepared = unit_request(fixture, run, episode="prepared")
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            grant = only_admitted(fixture.server.handle_request(prepared))
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                "handed-off", grant.reservation, 0, "handed_off")), [wire.Ok()])
            self.assertEqual(unit_place(fixture, run).reservations, 0)
            fixture.clock.advance(1)
            # The command sends its first step with its own owner and arrival time.
            step = unit_request(fixture, run, owner=fixture.proc.spawn(), since=fixture.clock.now(), episode="step")
            _ = first_step(fixture, step, 101)
            self.assertEqual(unit_place(fixture, run).reservations, 1)
            fixture.end_run(run)
            kept, = held(fixture)
            self.assertEqual((kept.since, kept.ended), (prepared.since, "failed"))

    def test_a_lint_grant_handed_off_with_a_failed_status_keeps_no_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            grant = only_admitted(fixture.server.handle_request(unit_request(fixture, run, step="clippy",
                                                                             episode="prepared")))
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                "handed-off", grant.reservation, 1, "handed_off")), [wire.Ok()])
            fixture.clock.advance(1)
            _ = run_steps(fixture, run, ("clippy", 0), ("nextest", 0))
            fixture.end_run(run)
            self.assertFalse(held(fixture))

    def test_a_mac_step_counts_as_a_step_and_its_speculative_copy_does_not(self) -> None:
        for copy in (False, True):
            with self.subTest(copy=copy), PlacementFixture() as fixture:
                place_rules(fixture)
                run = fixture.run_owner()
                request = mac_step(fixture, run)
                if copy:
                    # The local step of the run holds its grant while its Mac copy runs.
                    parent = unit_request(fixture, run)
                    fixture.free(60 * GIB)
                    fixture.allow_next_start()
                    _ = only_admitted(fixture.connect(parent).replies())
                    request = replace(request, purpose=placement.SpeculativeCopy(parent.episode, fixture.clock.now()))
                _ = start_on_mac(fixture, request)
                self.assertEqual(unit_place(fixture, run).reservations, 1)

    def test_a_step_that_moves_from_natedev_to_the_mac_counts_once_and_its_mac_failure_keeps_the_place(self) -> None:
        with PlacementFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            request = mac_step(fixture, run)
            # natedev gives its grant back before the step runs, and the same step goes to the Mac.
            local = unit_request(fixture, run, owner=wire.Owner(request.owner.pid, request.owner.start_time),
                                 since=request.since, step=request.profile.step, episode="local")
            _ = first_step(fixture, local, 0)
            assignment, claim = start_on_mac(fixture, request)
            self.assertEqual(unit_place(fixture, run).reservations, 1)
            finish_on_mac(fixture, request, assignment, claim, 101)
            fixture.end_run(run)
            kept, = held(fixture)
            self.assertEqual((kept.since, kept.ended), (request.since, "failed"))

    def test_a_passed_mac_copy_is_the_result_of_the_step_that_the_copy_watch_stops(self) -> None:
        for copy_first in (True, False):
            with self.subTest(copy_first=copy_first), PlacementFixture() as fixture:
                place_rules(fixture)
                run = fixture.run_owner()
                parent = unit_request(fixture, run)
                fixture.free(60 * GIB)
                fixture.allow_next_start()
                grant = only_admitted(fixture.connect(parent).replies())
                copy = replace(mac_step(fixture, run),
                               purpose=placement.SpeculativeCopy(parent.episode, fixture.clock.now()))
                assignment, claim = start_on_mac(fixture, copy)
                # The copy watch stops the local step when the copy passes, so the local status is not 0.
                stop_local = wire.ReleaseRequest("release-stopped", grant.reservation, 143)
                if not copy_first:
                    self.assertEqual(fixture.server.handle_request(stop_local), [wire.Ok()])
                finish_on_mac(fixture, copy, assignment, claim, 0)
                if copy_first:
                    self.assertEqual(fixture.server.handle_request(stop_local), [wire.Ok()])
                self.assertEqual(unit_place(fixture, run).reservations, 1)
                fixture.end_run(run)
                self.assertFalse(held(fixture))

    def test_a_red_mac_lint_step_after_a_passed_step_keeps_the_place(self) -> None:
        with PlacementFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            _ = first_step(fixture, unit_request(fixture, run, step="mend", episode="mend"), 0)
            request = mac_step(fixture, run)
            request = replace(request, profile=replace(request.profile, step="clippy"),
                              identity=replace(request.identity, step="clippy"))
            assignment, claim = start_on_mac(fixture, request)
            self.assertEqual(unit_place(fixture, run).reservations, 2)
            finish_on_mac(fixture, request, assignment, claim, 101)
            fixture.end_run(run)
            kept, = held(fixture)
            self.assertEqual(kept.ended, "failed")

    def test_a_run_that_ends_while_it_waits_keeps_no_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, run))
            fixture.end_run(run)
            self.assertFalse(held(fixture))

    def test_an_owner_that_dies_with_no_release_keeps_the_place_as_an_end_with_no_result(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            request = unit_request(fixture, run)
            _ = only_admitted(fixture.server.handle_request(request))
            fixture.end_owner(request.owner)
            fixture.end_run(run)
            kept, = held(fixture)
            self.assertEqual((kept.since, kept.replaces, kept.ended), (request.since, 1, "no_result"))
            new_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, new_run))
            self.assertEqual(run_since(fixture, new_run), request.since)
            self.assertEqual(unit_place(fixture, new_run).took_place, "failed")

    def test_a_waiting_run_takes_the_older_arrival_when_another_run_fails(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            failing = fixture.run_owner()
            first = unit_request(fixture, failing)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.clock.advance(10)
            waiting = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, waiting))
            self.assertEqual(fixture.server.handle_request(
                wire.ReleaseRequest("release-failing", grant.reservation, 1)), [wire.Ok()])
            fixture.end_run(failing)
            self.assertFalse(held(fixture))
            self.assertEqual(run_since(fixture, waiting), first.since)
            self.assertEqual(unit_place(fixture, waiting).took_place, "failed")

    def test_an_old_state_with_no_new_fields_reads(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, run))
            _ = failed_run(fixture)
            fixture.server.persist()
            path = fixture.state_dir / "state.json"
            saved = cast(dict[str, object], json.loads(path.read_text()))
            del saved["held_places"]
            for place in cast(dict[str, dict[str, object]], saved["run_places"]).values():
                del place["unit_place"]
            _ = path.write_text(json.dumps(saved))
            self.assertEqual(service.state_check(fixture.state_dir), 0)
            state = service.read_state(fixture.state_dir)
            self.assertFalse(state.held_places)
            self.assertTrue(all(isinstance(place.unit_place, status_wire.OutsideUnitRules)
                                for place in state.run_places.values()))

    def test_a_saved_unit_place_with_no_test_or_failed_step_fields_reads(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, run))
            fixture.server.persist()
            path = fixture.state_dir / "state.json"
            saved = cast(dict[str, object], json.loads(path.read_text()))
            for place in cast(dict[str, dict[str, dict[str, object]]], saved["run_places"]).values():
                del place["unit_place"]["tests_started"], place["unit_place"]["failed_step"]
            _ = path.write_text(json.dumps(saved))
            self.assertEqual(service.state_check(fixture.state_dir), 0)
            line = service.read_state(fixture.state_dir).run_places[records.run_owner(run.pid, run.start_time)].unit_place
            assert isinstance(line, status_wire.UnitPlace)
            self.assertEqual((line.tests_started, line.failed_step), (False, ""))

    def test_another_units_new_run_does_not_take_the_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            _ = failed_run(fixture)
            other = fixture.run_owner()
            request = unit_request(fixture, other, unit="other")
            wait_in_line(fixture, request)
            self.assertEqual(run_since(fixture, other), request.since)
            self.assertEqual(len(held(fixture)), 1)

    def test_a_reload_keeps_the_held_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            _, request = failed_run(fixture)
            kept = held(fixture)
            fixture.restart()
            self.assertEqual(held(fixture), kept)
            self.assertEqual(service.read_state(fixture.state_dir).held_places, kept)
            new_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, new_run))
            self.assertEqual(run_since(fixture, new_run), request.since)

    def test_the_time_limit_ends_the_held_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture, minutes=30)
            _, request = failed_run(fixture)
            fixture.clock.advance(30 * 60 + 1)
            _ = fixture.run_pass()
            self.assertFalse(held(fixture))
            with (fixture.state_dir / "queue_times.jsonl").open("rb") as stream:
                ended = [row for row in gate.queue_times_backward(stream) if row.get("kind") == "held_place_ended"]
            self.assertEqual(len(ended), 1)
            self.assertEqual((ended[0]["unit"], ended[0]["job"], ended[0]["since"], ended[0]["ended"]),
                             (UNIT, JOB, request.since, "failed"))
            new_run = fixture.run_owner()
            later = unit_request(fixture, new_run)
            wait_in_line(fixture, later)
            self.assertEqual(run_since(fixture, new_run), later.since)

    def test_a_hold_tool_that_ends_a_first_step_keeps_the_place(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            _, request = failed_run(fixture, status=143)
            kept, = held(fixture)
            self.assertEqual((kept.since, kept.ended), (request.since, "failed"))

    def test_a_held_place_keeps_both_named_machines_and_the_new_run_stands_in_both_lines(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            machines = ("natedev", "mini")
            failing = fixture.run_owner()
            failing_request = self.placement(fixture, failing, machines)
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            # The first step holds its grant while the run also asks for placement on both machines.
            grant = only_admitted(fixture.server.handle_request(unit_request(fixture, failing)))
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(failing_request)
            _ = fixture.run_pass()
            self.assertLessEqual(set(machines), set(unit_place(fixture, failing).machines))
            self.assertEqual(fixture.server.handle_request(
                wire.ReleaseRequest("release-failing", grant.reservation, 1)), [wire.Ok()])
            fixture.end_run(failing)
            kept, = held(fixture)
            self.assertLessEqual(set(machines), set(kept.machines))
            fixture.clock.advance(60)
            new_run = fixture.run_owner()
            new = self.placement(fixture, new_run, machines)
            _ = fixture.server.handle_request(new)
            _ = fixture.run_pass()
            self.assertEqual(run_since(fixture, new_run), failing_request.since)
            self.assertLessEqual(set(machines), set(unit_place(fixture, new_run).machines))
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            row, = (row for _, row in reply.reading.ordered if row["pid"] == new.owner.pid)
            assert "order_since" in row
            self.assertEqual(row["order_since"], failing_request.since)

    @staticmethod
    def placement(fixture: AdmissionFixture, run: wire.RunIdentity, machines: tuple[str, ...]) -> wire.PlacementRequest:
        local = unit_request(fixture, run)
        destinations = (placement.PlacementDestination("local", machines[0], fixture.server.host, WholeMacScope()),
                        placement.PlacementDestination("mac", machines[1], machines[1], WholeMacScope()))
        constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown("private-source"),
            local.argv, "fixture", "fixture", OwningSession("caller"),
            tuple(placement.PlacementCandidate(destination) for destination in destinations))
        return wire.PlacementRequest(local.episode,
            placement.SubmittingOwner(fixture.server.host, local.owner.pid, local.owner.start_time), local.since,
            local.profile, constraints, identity=local.identity, run=run,
            local=wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar, local.call, local.test_width,
                                             local.fresh_test), code_version=local.code_version)


class BothSourcesTests(unittest.TestCase):
    def test_a_new_run_takes_the_older_of_a_held_place_and_a_waiting_run_and_both_end(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            held_since = fixture.clock.now()
            fixture.clock.advance(10)
            waiting_run = fixture.run_owner()
            waiting = unit_request(fixture, waiting_run)
            wait_in_line(fixture, waiting)
            # The service never makes both at once; the test writes the held place beside the waiting run.
            fixture.server.held_places = (records.HeldPlace(UNIT, "fixture", JOB, held_since, held_since,
                held_since + 1800, "1-2", ("natedev",), 1, "failed"),)
            fixture.clock.advance(10)
            new_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, new_run))
            self.assertEqual(run_since(fixture, new_run), min(held_since, waiting.since))
            self.assertFalse(held(fixture))
            self.assertEqual(unit_place(fixture, waiting_run).replaced_by, new_run)
            self.assertEqual(unit_place(fixture, new_run).took_place, "failed")

    def test_an_older_service_reads_a_run_place_with_the_new_fields(self) -> None:
        @dataclass(frozen=True)
        class RunPlace:
            run: wire.RunIdentity
            since: float
            first_owner: wire.Owner
            unit: str = ""

        place = wire.RunPlace(wire.RunIdentity(1, "2"), 3.0, wire.Owner(1, "2"), UNIT,
                              unit_place=status_wire.UnitPlace("fixture", JOB, ("natedev",), reservations=2,
                                  first_step="step", first_status=0, tests_started=True, failed_step="lint",
                                  replaces=1))
        older = wire.parse_record(wire.encode_record(place), RunPlace)
        self.assertEqual((older.run, older.since, older.unit), (place.run, place.since, UNIT))
        self.assertEqual(wire.parse_record(wire.encode_record(place), wire.RunPlace), place)


class JobKeyTests(unittest.TestCase):
    """The key has the job kind: the test jobs are one kind, and every other job is its own kind."""

    def replaces(self, old_job: str, new_job: str) -> bool:
        """Start a waiting run with one job and a new run with the other; tell if the new run took the place."""
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            old_run = fixture.run_owner()
            old = unit_request(fixture, old_run, job=old_job)
            wait_in_line(fixture, old)
            fixture.clock.advance(5)
            new_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, new_run, job=new_job))
            replaced = old.episode not in fixture.server.waiters
            self.assertEqual(run_since(fixture, new_run) == old.since, replaced)
            return replaced

    def test_each_test_job_takes_the_place_of_a_waiting_test_affected(self) -> None:
        for job in sorted(records.TEST_JOB_FAMILY):
            with self.subTest(job):
                self.assertTrue(self.replaces("test-affected", job))

    def test_check_lint_and_other_jobs_take_only_the_place_of_the_same_job(self) -> None:
        cases = {("verify-check", "verify-check"): True, ("verify-lint", "verify-lint"): True,
                 ("verify-check", "verify-lint"): False, ("verify-doc", "verify-test"): False,
                 ("test-all", "test-nightly"): False, ("integrate", "test-all"): False}
        for (old_job, new_job), replaced in cases.items():
            with self.subTest(old=old_job, new=new_job):
                self.assertEqual(self.replaces(old_job, new_job), replaced)

    def test_a_run_with_no_job_neither_takes_nor_gives_a_place(self) -> None:
        for old_job, new_job in (("", JOB), (JOB, "")):
            with self.subTest(old=old_job, new=new_job):
                self.assertFalse(self.replaces(old_job, new_job))
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            _ = first_step(fixture, unit_request(fixture, run, job=""), 1)
            place = fixture.server.run_places[records.run_owner(run.pid, run.start_time)]
            self.assertIsInstance(place.unit_place, status_wire.OutsideUnitRules)
            fixture.end_run(run)
            self.assertFalse(held(fixture))

    def test_a_held_place_goes_only_to_a_new_run_of_the_same_job_kind(self) -> None:
        with AdmissionFixture() as fixture:
            place_rules(fixture)
            _, request = failed_run(fixture, job="test-affected")
            fixture.clock.advance(60)
            lint_run = fixture.run_owner()
            lint = unit_request(fixture, lint_run, job="verify-lint")
            wait_in_line(fixture, lint)
            self.assertEqual(run_since(fixture, lint_run), lint.since)
            self.assertEqual(len(held(fixture)), 1)
            test_run = fixture.run_owner()
            wait_in_line(fixture, unit_request(fixture, test_run, job="test-all"))
            self.assertEqual(run_since(fixture, test_run), request.since)
            self.assertFalse(held(fixture))

    def test_the_job_name_reads_the_step_job_then_the_verify_call(self) -> None:
        cases = {(StepJob("test-all"), NotVerifyCall()): "test-all",
                 (StepJob("lint"), NotVerifyCall()): "verify-lint",
                 (GATE_JOB_NOT_RECORDED, VerifyCall("test", "call", 1, 3)): "verify-test",
                 (GATE_JOB_NOT_RECORDED, NotVerifyCall()): ""}
        for (job, verify), name in cases.items():
            with self.subTest(name):
                self.assertEqual(records.run_job_name(job, verify), name)
        self.assertEqual({records.place_kind(job) for job in records.TEST_JOB_FAMILY}, {"test"})
        self.assertEqual(records.place_kind("verify-lint"), "verify-lint")


if __name__ == "__main__":
    _ = unittest.main()

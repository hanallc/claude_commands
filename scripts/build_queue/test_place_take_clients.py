"""The clients of a replaced run show one line, and the Builds page shows held places and taken places."""
from __future__ import annotations

from contextlib import nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
import io
import os
from pathlib import Path
from typing import override
import unittest
from unittest import mock

from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_records as records
from scripts.build_queue import first_step, memory_feed, placement, queue_place, test_memory_feed
from scripts.build_queue.admission_test_support import (
    AdmissionFixture, Exchange, GIB, ScriptedTransport, admitted_reply, publish_fixture_view, reply_bytes,
)
from scripts.build_queue.admission_wire import REPLACED_REASON
from scripts.build_queue.test_placement import local_candidate, placement_request
from scripts.lib import build_folders
from scripts.lib.build_slots import SlotQueuePosition
from scripts.message.session_send_test_support import forbid_real_sender

UNIT_LINE = "Replaced: a newer run of queue took the place of this run."
NO_UNIT_LINE = "Replaced: a newer run of this unit took the place of this run."
OTHER_REASON = "fixture refusal"


def setUpModule() -> None:
    forbid_real_sender()


def answer_with(fixture: AdmissionFixture, *exchanges: Exchange) -> None:
    """Answer each client connection with the next scripted exchange until the fixture closes."""
    transport = ScriptedTransport(fixture.clock, *exchanges)
    _ = fixture.enter_context(mock.patch.object(client, "CLOCK", fixture.clock))
    _ = fixture.enter_context(mock.patch("scripts.build_queue.admission_client.socket.socket", side_effect=transport.socket))
    _ = fixture.enter_context(mock.patch.object(client, "DefaultSelector", side_effect=transport.selector))


def queue_request(fixture: AdmissionFixture) -> wire.AdmitRequest:
    request = fixture.request()
    return replace(request, identity=replace(request.identity, unit="queue"))


class ReplacedClientTests(unittest.TestCase):
    def test_verify_client_prints_the_replaced_line_once_and_exits_with_its_refusal_status(self) -> None:
        for reason, shown, gate_reason in (
                (REPLACED_REASON, UNIT_LINE, f"'{UNIT_LINE}'"),
                (OTHER_REASON, f"memory admission check failed ({OTHER_REASON}); step refused", "''")):
            with self.subTest(reason=reason), AdmissionFixture() as fixture:
                request = queue_request(fixture)
                shell_state = fixture.root / "grant.sh"
                answer_with(fixture, Exchange(reply_bytes(wire.Refused(reason))))
                with mock.patch.object(client, "step_request", return_value=request), \
                     mock.patch.object(records, "process_start_time", return_value=request.owner.start_time), \
                     redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()) as output:
                    status = queue_place.main(["admit", "--pid", str(request.owner.pid),
                                               "--shell-state", str(shell_state), "--", "cargo", "check"])
                self.assertEqual(status, 2)
                self.assertEqual(errors.getvalue(), shown + "\n")
                self.assertEqual(output.getvalue(), "")
                self.assertIn("BUILDLOG_MEM_OUTCOME=Refused", shell_state.read_text())
                # The gate records this line as the reason the step did not run.
                self.assertIn(f"BUILDLOG_MEM_REPLACED={gate_reason}\n", shell_state.read_text())

    def test_gate_copy_client_refuses_with_the_replaced_line_and_makes_no_copy(self) -> None:
        for reason, shown, printed in ((REPLACED_REASON, UNIT_LINE, ""), (OTHER_REASON, OTHER_REASON, OTHER_REASON + "\n")):
            with self.subTest(reason=reason), AdmissionFixture() as fixture:
                request = placement_request(fixture)
                request = replace(request, identity=replace(request.identity, unit="queue"))
                folder = fixture.root / "copies-admission"
                folder.mkdir()
                # A replaced run cancels nothing: a cancel would need a second connection.
                exchanges = [Exchange(reply_bytes(wire.Refused(reason)))]
                if reason == OTHER_REASON:
                    exchanges.append(Exchange(reply_bytes(wire.Ok())))
                answer_with(fixture, *exchanges)
                with mock.patch.object(first_step, "_verify_request", return_value=request), \
                     mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(folder)), \
                     redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(first_step.FirstStepRefused) as refused:
                        with first_step.admitted_run(["verify.sh", "check"], fixture.root):
                            self.fail("the refused run made its copy")
                self.assertEqual(str(refused.exception), shown)
                self.assertEqual(refused.exception.exit_code, 2)
                self.assertEqual(errors.getvalue(), printed)

    def test_mac_placement_client_prints_the_replaced_line_once_and_exits_with_its_refusal_status(self) -> None:
        for reason, error, shown in ((REPLACED_REASON, client.RunReplaced, NO_UNIT_LINE),
                                     (OTHER_REASON, wire.AdmissionUnavailable, OTHER_REASON)):
            with self.subTest(reason=reason), AdmissionFixture() as fixture:
                answer_with(fixture, Exchange(reply_bytes(wire.Refused(reason))))
                with redirect_stderr(io.StringIO()) as errors, self.assertRaises(wire.AdmissionUnavailable) as refused:
                    _ = client.request_placement(placement_request(fixture))
                self.assertIs(type(refused.exception), error)
                self.assertEqual(refused.exception.reason, shown)
                self.assertEqual(errors.getvalue(), "" if error is client.RunReplaced else OTHER_REASON + "\n")
        arguments = ["place-step", "--since", "1", "--assignment-file", "/nonexistent/assignment.json"]
        for raised, pinned, errors_text, output_text in (
                (client.RunReplaced(UNIT_LINE), False, UNIT_LINE + "\n", ""),
                (client.RunReplaced(UNIT_LINE), True, "", f"refused\t{UNIT_LINE}\n"),
                (wire.AdmissionUnavailable(OTHER_REASON), False,
                 f"admission service unavailable: {OTHER_REASON}; step refused\n", "")):
            with self.subTest(raised=raised.reason, pinned=pinned), \
                 mock.patch.dict(os.environ, {client.RESERVED_STEP_ENV: ""}), \
                 mock.patch.object(client, "_place_step", side_effect=raised), \
                 redirect_stderr(io.StringIO()) as errors, redirect_stdout(io.StringIO()) as output:
                status = client.main([*arguments, *(["--pinned-refusals"] if pinned else [])])
            self.assertEqual(status, 2)
            self.assertEqual(errors.getvalue(), errors_text)
            self.assertEqual(output.getvalue(), output_text)

    def test_step_job_client_refuses_with_the_replaced_line_not_the_failure_text(self) -> None:
        for reason, shown, printed in ((REPLACED_REASON, UNIT_LINE, ""),
                                       (OTHER_REASON, OTHER_REASON,
                                        f"memory admission check failed ({OTHER_REASON}); step refused\n")):
            with self.subTest(reason=reason), AdmissionFixture() as fixture:
                request = queue_request(fixture)
                configuration = replace(records.read_memory_config(), program_first_peaks={"gate": GIB})
                answer_with(fixture, Exchange(reply_bytes(wire.Refused(reason))))
                with mock.patch.object(client, "step_request", return_value=request), \
                     mock.patch.object(records, "read_memory_config", return_value=configuration), \
                     redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(first_step.FirstStepRefused) as refused:
                        _ = first_step._program_grant(  # pyright: ignore[reportPrivateUsage]
                            ["python3", "gate.py", "--tests-only"], fixture.root, request.owner,
                            SlotQueuePosition(request.episode, request.since),
                            wire.RunIdentity(request.owner.pid, request.owner.start_time))
                self.assertEqual(str(refused.exception), shown)
                self.assertEqual(refused.exception.exit_code, 2)
                self.assertEqual(errors.getvalue(), printed)


def clock_time(hour: int, minute: int) -> float:
    return datetime(2026, 11, 22, hour, minute, tzinfo=UTC).timestamp()


HELD = records.HeldPlace("queue", "claude", "gate", 85, clock_time(16, 40), clock_time(17, 10), "4242-99", ("mac",), 0,
                         "failed")


class PlaceTakePageTests(unittest.TestCase):
    view_directory: Path = Path()

    @override
    def setUp(self) -> None:
        feed = test_memory_feed.MemoryFeedTests()
        self.addCleanup(feed.doCleanups)
        feed.setUp()
        self.view_directory = feed.root / "buildlog" / "admission"

    def page(self, reading: records.QueueRead) -> memory_feed.MemoryPage:
        publish_fixture_view(self.view_directory, reading, reading.explanation.now)
        return memory_feed.reading()

    def held_page(self, *places: records.HeldPlace) -> memory_feed.MemoryPage:
        reading = test_memory_feed.queued_reading(test_memory_feed.reservation_reading(), ())
        return self.page(replace(reading, held_places=places))

    def test_a_held_place_shows_one_row_in_arrival_order_with_no_machine_and_no_memory(self) -> None:
        later = replace(HELD, since=95, failed_run="4343-99")
        page = self.held_page(later, HELD)
        self.assertEqual([(row["since"], row["key"], row["failed_run"]) for row in page["held_places"]],
                         [(85, "4242-99", "4242-99"), (95, "4343-99", "4343-99")])
        row = page["held_places"][0]
        self.assertEqual(row["unit"], "queue")
        self.assertEqual(row["targets"], ("mac",))
        self.assertFalse({"machine", "need", "used", "reserved"} & row.keys())
        self.assertEqual(len(page["waiters"]), 4)

    def test_a_held_place_whose_run_failed_says_so(self) -> None:
        row = self.held_page(HELD)["held_places"][0]
        self.assertEqual(row["ended"], "failed")
        self.assertEqual(row["failed_at"], "16:40 UTC")
        self.assertEqual(row["kept_until"], "17:10 UTC")

    def test_a_held_place_whose_owner_ended_with_no_result_says_so(self) -> None:
        row = self.held_page(replace(HELD, ended="no_result"))["held_places"][0]
        self.assertEqual(row["ended"], "no_result")
        self.assertEqual(row["failed_at"], "16:40 UTC")
        self.assertEqual(row["kept_until"], "17:10 UTC")

    def test_a_run_that_took_a_place_shows_its_order_note(self) -> None:
        reading = test_memory_feed.queued_reading(test_memory_feed.reservation_reading(), ())
        waiting = reading.view.waiting
        took = (replace(waiting[0], took_place="failed"), replace(waiting[1], took_place="waiting"), *waiting[2:])
        reading = replace(reading, view=replace(reading.view, waiting=took))
        page = self.page(reading)
        notes = {row["pid"]: row.get("order_note", "") for row in page["waiters"]}
        self.assertEqual(notes, {200: "Took the place of its failed run.", 201: "Took the place of its waiting run.",
                                 202: "", 203: ""})


class HandoffClientTests(unittest.TestCase):
    def test_a_handoff_gives_each_local_grant_back_as_handed_off_and_a_failed_preparation_as_a_result(self) -> None:
        with AdmissionFixture() as fixture:
            initial = placement_request(fixture)
            candidate = local_candidate(fixture)
            request = replace(initial, constraints=replace(initial.constraints, candidates=(candidate,)))
            assignment = placement.PlacementAssignment(request.episode, "local-attempt", candidate.destination,
                request.constraints.source_key, request.owner, request.constraints.session, request.since)
            granted = admitted_reply()
            path = fixture.root / "assignment.json"
            client.write_assignment(path, wire.LocalPlacementGranted(assignment, granted))
            closed = mock.Mock(assignment=assignment, state=placement.ClosedAssignment("completed"))
            position = SlotQueuePosition(request.episode, request.since)
            local = wire.LocalPlacementGranted(assignment, granted)
            grants: tuple[tuple[wire.Admitted | wire.LocalPlacementGranted, bool,
                                tuple[int, wire.ReleaseEnding]], ...] = (
                (granted, True, (0, "handed_off")),
                (local, True, (0, "handed_off")),
                (granted, False, (1, "result")))
            for grant, handoff, expected in grants:
                with (self.subTest(grant=type(grant).__name__, expected=expected),
                      mock.patch.dict(os.environ),
                      mock.patch.object(client, "release", return_value=wire.Ok()) as released,
                      mock.patch.object(client, "report_execution", return_value=closed)):
                    admitted = first_step.FirstStepAdmission(position, grant, path)
                    if handoff:
                        admitted.handoff(fixture.root)
                    else:
                        # A failed preparation releases its grant as the failed result of the first step.
                        admitted.close()
                    status, ending = expected
                    released.assert_called_once_with(granted.reservation, status, granted.cleanup_timing, ending)
            with (mock.patch.dict(os.environ),
                  mock.patch.object(client, "release", return_value=wire.Ok()) as released):
                # A gate keeps the grant through the handoff; gate.py ends it after its assignment check.
                first_step.FirstStepAdmission(position, first_step.OwnedGatePlacement(local, request), path).handoff(fixture.root)
                released.assert_not_called()

    def test_a_release_carries_its_ending_and_an_older_release_reads_as_a_result(self) -> None:
        handed_off = wire.ReleaseRequest("handoff", "stem", 0, "handed_off")
        self.assertEqual(wire.parse_request(wire.encode_request(handed_off)), handed_off)
        older = wire.encode_request(wire.ReleaseRequest("older", "stem", 1)).replace(',"ending":"result"', "")
        self.assertNotIn("ending", older)
        self.assertEqual(wire.parse_request(older), wire.ReleaseRequest("older", "stem", 1, "result"))


if __name__ == "__main__":
    _ = unittest.main()

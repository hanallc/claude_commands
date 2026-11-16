"""Placement polls share queue work and leave status requests independent."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import select
import threading
import unittest
from typing import cast
from unittest import mock

from scripts.build_queue import admission_service as service, admission_protocol as wire, admission_wire as status_wire, placement
from scripts.build_queue import admission_records as records
from scripts.lint import memory_admit as gate
from scripts.build_queue.test_admission_service import FixedSystemReader
from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate, assignment_for, local_candidate


class PlacementWorkTests(unittest.TestCase):
    def test_150_unchanged_polls_do_not_persist_or_build_a_queue(self) -> None:
        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints,
                candidates=(local_candidate(fixture),)), code_version="poll-version")
            initial, = fixture.connect(request).replies()
            self.assertIsInstance(initial, wire.PlacementWait)
            evidence = fixture.server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            fixture.server.system_reader = FixedSystemReader(evidence)
            state = fixture.state_text()
            with mock.patch.object(fixture.server, "persist", wraps=fixture.server.persist) as persisted, \
                    mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed, \
                    mock.patch.object(fixture.server, "_publish_replies") as published:
                for _poll in range(150):
                    self.assertEqual(fixture.connect(request).replies(), [initial])
                persisted.assert_not_called()
                passed.assert_not_called()
                published.assert_not_called()
            self.assertEqual(fixture.state_text(), state)

    def test_socket_status_answers_before_a_ready_placement_pass(self) -> None:
        with PlacementFixture() as fixture:
            first = placement_request(fixture)
            _ = publish_candidate(fixture, first)
            assignment = assignment_for(fixture, first)
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            _ = fixture.connect(request)
            status = fixture.connect(status_wire.PlacementStatusRequest(assignment.episode, assignment.attempt))
            run_pass = fixture.server.run_pass
            observed: list[wire.Reply] = []

            def pass_after_status() -> object:
                # A missing answer raises BlockingIOError before any pass can start.
                observed.append(wire.parse_reply(status.socket.recv(65536)))
                return run_pass()

            with mock.patch.object(fixture.server, "run_pass", side_effect=pass_after_status) as passed:
                fixture.server.run_once(0)
                passed.assert_called_once()
            self.assertEqual(len(observed), 1)
            self.assertIsInstance(observed[0], status_wire.PlacementStatusReply)
            assert isinstance(observed[0], status_wire.PlacementStatusReply)
            self.assertIsInstance(observed[0].status, status_wire.KnownPlacementStatus)
            assert isinstance(observed[0].status, status_wire.KnownPlacementStatus)
            self.assertEqual(observed[0].status.assignment, assignment)
            self.assertFalse(fixture.server.parked_placements)

    def test_socket_status_answers_during_a_running_pass(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            view = fixture.connect("")
            entered = threading.Event()
            release = threading.Event()
            ended = threading.Event()
            failures: list[BaseException] = []
            original = cast(Callable[[gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration],
                gate.PassResult | gate.UnusableAdmissionConfiguration], getattr(fixture.server, "_pass"))

            def held_pass(configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration
                          ) -> gate.PassResult | gate.UnusableAdmissionConfiguration:
                result = original(configuration)
                key = placement.attempt_key(assignment.episode, assignment.attempt)
                fixture.server.assignments[key] = replace(fixture.server.assignments[key],
                    state=placement.ClosedAssignment("closed in the running pass"))
                entered.set()
                if not release.wait(5):
                    raise AssertionError("the test did not release the held pass")
                return result

            def run_held_pass() -> None:
                try:
                    _ = fixture.run_pass()
                except BaseException as error:
                    failures.append(error)
                finally:
                    ended.set()

            with mock.patch.object(fixture.server, "_pass", side_effect=held_pass):
                thread = threading.Thread(target=run_held_pass)
                thread.start()
                try:
                    self.assertTrue(entered.wait(5), failures)
                    status = fixture.connect(status_wire.PlacementStatusRequest(assignment.episode, assignment.attempt))
                    status.socket.settimeout(0.5)
                    with status.socket.makefile("rb") as stream:
                        reply = wire.parse_reply(stream.readline())
                    self.assertFalse(ended.is_set())
                    self.assertIsInstance(reply, status_wire.PlacementStatusReply)
                    assert isinstance(reply, status_wire.PlacementStatusReply)
                    self.assertIsInstance(reply.status, status_wire.KnownPlacementStatus)
                    assert isinstance(reply.status, status_wire.KnownPlacementStatus)
                    self.assertEqual(reply.status.assignment, assignment)
                    self.assertIsInstance(reply.status.state, placement.CurrentAssignment)
                    # Other requests remain with the admission loop until the pass ends.
                    view.socket.sendall(wire.encode_request(wire.ViewRequest()).encode())
                finally:
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(failures, failures)
            self.assertTrue(any(isinstance(reply, wire.ViewReply) for reply in view.replies()))
            latest, = fixture.connect(status_wire.PlacementStatusRequest(assignment.episode, assignment.attempt)).replies()
            assert isinstance(latest, status_wire.PlacementStatusReply)
            assert isinstance(latest.status, status_wire.KnownPlacementStatus)
            self.assertEqual(latest.status.state, placement.ClosedAssignment("closed in the running pass"))

    def test_placement_received_during_a_pass_waits_for_the_next_pass(self) -> None:
        with PlacementFixture() as fixture:
            _ = fixture.occupy_host()
            fixture.free(0)
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints,
                candidates=(local_candidate(fixture),)))
            entered, release, received = (threading.Event() for _event in range(3))
            failures: list[BaseException] = []
            original_pass = cast(Callable[[gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration],
                gate.PassResult | gate.UnusableAdmissionConfiguration], getattr(fixture.server, "_pass"))
            original_event = cast(Callable[[service.PassStatusWork, service.Connection, int], None],
                getattr(service.PassStatusWork, "_connection_event"))

            def held_pass(configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration
                          ) -> gate.PassResult | gate.UnusableAdmissionConfiguration:
                result = original_pass(configuration)
                entered.set()
                if not release.wait(5):
                    raise AssertionError("the test did not release the held pass")
                return result

            def receive(work: service.PassStatusWork, connection: service.Connection, events: int) -> None:
                original_event(work, connection, events)
                if b"\n" in connection.received:
                    received.set()

            def run_held_pass() -> None:
                try:
                    _ = fixture.run_pass()
                except BaseException as error:
                    failures.append(error)

            with mock.patch.object(fixture.server, "_pass", side_effect=held_pass), \
                    mock.patch.object(service.PassStatusWork, "_connection_event", new=receive):
                thread = threading.Thread(target=run_held_pass)
                thread.start()
                try:
                    self.assertTrue(entered.wait(5), failures)
                    peer = fixture.connect(request)
                    self.assertTrue(received.wait(5), failures)
                finally:
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(failures, failures)
            self.assertFalse(select.select([peer.socket], [], [], 0)[0])
            fixture.server.run_once(0)
            _ = fixture.run_pass()
            reply, = peer.replies()
            self.assertIsInstance(reply, wire.PlacementWait)
            self.assertIn(request.episode, fixture.server.waiters)

    def test_oversized_buffer_without_a_newline_is_refused_after_a_pass(self) -> None:
        with PlacementFixture() as fixture:
            entered, release, received = (threading.Event() for _event in range(3))
            failures: list[BaseException] = []
            buffered: list[service.Connection] = []
            config = fixture.server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            limit = max(config.request_max_bytes, wire.CLEANUP_REQUEST_MAX_BYTES)
            original_pass = cast(Callable[[gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration],
                gate.PassResult | gate.UnusableAdmissionConfiguration], getattr(fixture.server, "_pass"))
            original_event = cast(Callable[[service.PassStatusWork, service.Connection, int], None],
                getattr(service.PassStatusWork, "_connection_event"))

            def held_pass(configuration: gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration
                          ) -> gate.PassResult | gate.UnusableAdmissionConfiguration:
                result = original_pass(configuration)
                entered.set()
                if not release.wait(5):
                    raise AssertionError("the test did not release the held pass")
                return result

            def receive(work: service.PassStatusWork, connection: service.Connection, events: int) -> None:
                original_event(work, connection, events)
                if len(connection.received) > limit:
                    buffered.append(connection)
                    received.set()

            def run_held_pass() -> None:
                try:
                    _ = fixture.run_pass()
                except BaseException as error:
                    failures.append(error)

            with mock.patch.object(fixture.server, "_pass", side_effect=held_pass), \
                    mock.patch.object(service.PassStatusWork, "_connection_event", new=receive):
                thread = threading.Thread(target=run_held_pass)
                thread.start()
                try:
                    self.assertTrue(entered.wait(5), failures)
                    peer = fixture.connect(" " * (limit + 1))
                    self.assertTrue(received.wait(5), failures)
                finally:
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertFalse(failures, failures)
            connection, = buffered
            self.assertEqual(len(connection.received), limit + 1)
            self.assertNotIn(b"\n", connection.received)
            # The kernel holds no unread bytes that could make the main loop handle this socket.
            self.assertFalse(select.select([connection.socket], [], [], 0)[0])
            descriptor = connection.socket.fileno()
            replies = peer.replies()
            self.assertEqual(replies, [wire.Refused(f"admission request exceeds {limit} bytes")])
            self.assertNotIn(descriptor, fixture.server.connections)
            self.assertEqual(connection.socket.fileno(), -1)

    def test_historical_attempts_do_not_repeat_host_configuration_reads(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            with mock.patch.object(fixture.server, "host_evidence_key",
                    wraps=fixture.server.host_evidence_key) as keys:
                _ = fixture.run_pass()
                current_reads = keys.call_count
                for index in range(150):
                    old = replace(assignment, attempt=f"old-{index}")
                    fixture.server.assignments[placement.attempt_key(old.episode, old.attempt)] = records.SavedPlacementAssignment(
                        old, placement.ClosedAssignment("busy"), placement.PermissionAvailable())
                keys.reset_mock()
                _ = fixture.run_pass()
                self.assertEqual(keys.call_count, current_reads)


if __name__ == "__main__":
    _ = unittest.main()

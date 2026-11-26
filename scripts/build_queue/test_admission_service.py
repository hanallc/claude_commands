"""In-process service decisions, persistence and owner events."""
from __future__ import annotations

from scripts.build_queue import execution_identity
from scripts.aws_target import hana_protocol
from scripts.lib import test_phase

from scripts.build_queue import snapshot_codec as codec
from scripts.build_queue.admission_protocol_v1_fixture import parse_reply as decode_legacy
from scripts.build_queue.scope_activity import LiveBuildRun, NoLiveBuildRun, ScopeActivityReader

from scripts.lint.memory_test_support import UNREACHABLE_WAIT_FIXTURE, admission_configuration_fixture, admission_policy_fixture, fake_memory_readings, write_pressure_reading

from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import io
import json
import math
import errno
import os
from pathlib import Path
from queue import Queue
import stat
import select
import shutil
import signal
import shlex
import socket
import sys
import tempfile
import threading
import time
import types
import subprocess
import unittest
from typing import TYPE_CHECKING, cast, override
from unittest import mock

if TYPE_CHECKING:
    from scripts.build_queue.test_placement import PlacementFixture
    from scripts.mac_test import offload

from scripts.lint.memory_test_support import write_config_fixture, commit_config_fixture
from scripts.lint import memory_admit as gate
from scripts.accounts import outage_alerts

from scripts.buildlog import store
from scripts.buildlog import record as buildlog_record
from scripts.build_hold import override_guard
from scripts.build_hold.owning_session import OwningSession

from scripts.lib import atomic_file, conf
from scripts.lib import inotify, test_hang_guard
from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.lib.user_zone import user_zone
from scripts.lib.atomic_file import write_text_atomic
from scripts.message import session_send
from scripts.production import staff
from scripts.message.session_send_test_support import forbid_real_sender
from scripts.message.roster import UnitLabels
from scripts.lint.build_shape import build_shape
from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_service as service
from scripts.build_queue import admission_wire as status_wire
from scripts.build_queue import admission_code, queue_snapshot, queue_view, head_facts, memory_trace, person_actions, placement
from scripts.build_queue import admission_records as records, progress_profile as progress, step_estimator
from zoneinfo import ZoneInfo
from scripts.build_queue.queue_fixture import write_memory_config, write_process, write_scope
from scripts.build_queue.admission_test_support import (
    AdmissionFixture, FixedSystemReader, GIB, InjectedCrash, SocketPeer, caller_admission, only_admitted,
)


TRACE_POLICY = memory_trace.MemoryTracePolicy(2, 100, 8192, 7, 16, 1024,
                                            final_replay_bytes=131072,
                                            max_pending_commands=8, general_trace_share_percent=50,
                                            scope_loss_grace_seconds=5, seal_retry_count=4, shutdown_timeout_seconds=3)


def setUpModule() -> None:
    forbid_real_sender()


class PlacementStatusTests(unittest.TestCase):
    def replace_waiting_placement(self, fixture: PlacementFixture) -> wire.PlacementRequest:
        from scripts.build_queue.test_place_take import mac_step, unit_request, wait_in_line
        from scripts.build_queue.test_placement import local_candidate

        fixture.free(0)
        fixture.hold_starts()
        old = mac_step(fixture, fixture.run_owner())
        old = replace(old, constraints=replace(old.constraints, candidates=(local_candidate(fixture),)))
        self.assertIsInstance(fixture.server.handle_request(old)[0], wire.PlacementWait)
        fixture.clock.advance(1)
        wait_in_line(fixture, unit_request(fixture, fixture.run_owner()))
        self.assertNotIn(old.episode, fixture.server.placement_requests)
        return old

    def test_replaced_waiting_placement_refuses_a_prerequisite_and_cancel(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture

        with PlacementFixture() as fixture:
            old = self.replace_waiting_placement(fixture)

            prerequisite = wire.PrerequisiteReport("prerequisite", old.episode, old.owner, placement.TokenHeld())
            cancel = wire.CancelPlacement("cancel", old.episode, old.owner, "the caller left")
            refused = [wire.Refused(status_wire.REPLACED_REASON)]
            with self.subTest(request="prerequisite"):
                self.assertEqual(fixture.connect(prerequisite).replies(), refused)
            with self.subTest(request="cancel"):
                self.assertEqual(fixture.connect(cancel).replies(), refused)

            fixture.restart()
            with fixture.serving():
                with self.assertRaises(client.RunReplaced) as rejected_prerequisite:
                    _ = client.report_prerequisite(prerequisite)
                with self.assertRaises(client.RunReplaced) as rejected_cancel:
                    _ = client.cancel_placement(cancel)
            message = "Replaced: a newer run of this unit took the place of this run."
            self.assertEqual(rejected_prerequisite.exception.reason, message)
            self.assertEqual(rejected_cancel.exception.reason, message)

            missing = [wire.Refused(wire.PLACEMENT_REQUEST_MISSING_REASON)]
            other_owner = replace(old.owner, pid=old.owner.pid + 1)
            self.assertEqual(fixture.connect(replace(cancel, owner=other_owner)).replies(), missing)
            fixture.clock.advance(records.read_memory_config().replay_retention_seconds + 1)
            self.assertEqual(fixture.server.handle_request(prerequisite), missing)

    def test_replaced_placement_replay_does_not_match_an_unrelated_request(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture

        with PlacementFixture() as fixture:
            old = self.replace_waiting_placement(fixture)
            owner = old.owner
            request_id = "placement-replaced:" + json.dumps((old.episode, owner.host, owner.pid, owner.start_time))
            release = wire.ReleaseRequest(request_id, "unrecorded-reservation", 0)
            self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            cancel = wire.CancelPlacement("cancel", old.episode, owner, "the caller left")
            self.assertEqual(fixture.server.handle_request(cancel), [wire.Refused(status_wire.REPLACED_REASON)])

    def test_replaced_placement_expires_before_the_next_pass(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture

        with PlacementFixture() as fixture:
            fixture.configure(replay_retention_seconds=2)
            fixture.restart()
            old = self.replace_waiting_placement(fixture)
            cancel = wire.CancelPlacement("cancel", old.episode, old.owner, "the caller left")
            fixture.clock.advance(1)
            self.assertEqual(fixture.server.handle_request(cancel), [wire.Refused(status_wire.REPLACED_REASON)])
            saved = (fixture.state_dir / "state.json").read_bytes()
            fixture.clock.advance(1)
            self.assertEqual(fixture.server.handle_request(cancel), [wire.Refused(wire.PLACEMENT_REQUEST_MISSING_REASON)])
            self.assertEqual((fixture.state_dir / "state.json").read_bytes(), saved)

    def test_kept_place_restores_its_failed_step_and_reads_an_old_record(self) -> None:
        from scripts.build_queue.test_place_take import place_rules, run_steps

        with AdmissionFixture(private=True) as fixture:
            place_rules(fixture)
            run = fixture.run_owner()
            _ = run_steps(fixture, run, ("check", 0), ("lint", 1))
            fixture.end_run(run)
            _ = fixture.run_pass()
            kept, = service.read_state(fixture.state_dir).held_places
            self.assertEqual(kept.failed_step, "lint")
            fixture.restart()
            self.assertEqual(fixture.server.held_places, (kept,))

            path = fixture.state_dir / "state.json"
            saved = cast(dict[str, object], json.loads(path.read_text()))
            saved_place, = cast(list[dict[str, object]], saved["held_places"])
            self.assertEqual(saved_place["failed_step"], "lint")
            saved_place["failed_step"] = "test"
            _ = path.write_text(json.dumps(saved))
            fixture.restart()
            self.assertEqual(fixture.server.held_places, (replace(kept, failed_step="test"),))

            del saved_place["failed_step"]
            _ = path.write_text(json.dumps(saved))
            fixture.restart()
            self.assertEqual(fixture.server.held_places, (replace(kept, failed_step=""),))

    def test_published_view_places_a_kept_place_between_two_waiting_steps(self) -> None:
        from scripts.build_queue.test_place_take import place_rules, unit_request, wait_in_line

        with AdmissionFixture(private=True) as fixture:
            place_rules(fixture)
            now = fixture.clock.now()
            later = unit_request(fixture, fixture.run_owner(), unit="research", since=now - 10)
            earlier = unit_request(fixture, fixture.run_owner(), unit="phases", since=now - 30)
            wait_in_line(fixture, earlier)
            wait_in_line(fixture, later)
            fixture.server.held_places = (records.HeldPlace("queue", "fixture", "lint", now - 20,
                now, now + 100, "4242-99", ("natedev",), 1, "failed"),)
            _ = fixture.run_pass()
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            self.assertEqual([step.unit for step in reply.reading.view.waiting], ["phases", "research"])
            kept, = reply.reading.held_places
            self.assertEqual(kept.position, 2)
            self.assertEqual(queue_snapshot.read_view_snapshot(fixture.state_dir).reading.held_places, (kept,))

    def test_a_newer_priority_waiter_precedes_a_kept_place(self) -> None:
        from scripts.build_queue.test_place_take import unit_request, wait_in_line

        with AdmissionFixture(private=True) as fixture:
            now = fixture.clock.now()
            earlier = unit_request(fixture, fixture.run_owner(), unit="earlier", since=now - 30)
            newer = unit_request(fixture, fixture.run_owner(), unit="newer", since=now - 10)
            wait_in_line(fixture, earlier)
            wait_in_line(fixture, newer)
            fixture.server.held_places = (records.HeldPlace("queue", "fixture", "lint", now - 20,
                now, now + 100, "4242-99", ("natedev",), 1, "failed"),)
            fixture.server.priority = records.QueuePriority(({
                "unit": "newer", "why": "fixture", "set_at": now, "set_by": "director"},))
            _ = fixture.run_pass()
            reading = queue_snapshot.read_view_snapshot(fixture.state_dir).reading
            self.assertEqual([step.unit for step in reading.view.waiting], ["newer", "earlier"])
            kept, = reading.held_places
            self.assertEqual(kept.position, 3)
            line = reading.view.move_line
            assert isinstance(line, records.QueueMoveLine)
            self.assertEqual(line.kept_runs, (kept.failed_run,))
            self.assertEqual([member.kept_before for member in line.members], [0, 0])

            fixture.server.priority = records.QueuePriority(({
                "unit": "queue", "why": "fixture", "set_at": now, "set_by": "director"},))
            _ = fixture.run_pass()
            reading = queue_snapshot.read_view_snapshot(fixture.state_dir).reading
            self.assertEqual([step.unit for step in reading.view.waiting], ["earlier", "newer"])
            kept, = reading.held_places
            self.assertEqual(kept.position, 1)
            line = reading.view.move_line
            assert isinstance(line, records.QueueMoveLine)
            self.assertEqual([member.kept_before for member in line.members], [1, 1])

    def assert_unchanged_placement_poll(self, fixture: AdmissionFixture, request: wire.PlacementRequest | str,
                                        initial: wire.PlacementWait) -> None:
        with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed, \
                mock.patch.object(fixture.server, "persist", wraps=fixture.server.persist) as persisted:
            self.assertEqual(fixture.connect(request).replies(), [initial])
            self.assertEqual((passed.call_count, persisted.call_count), (0, 0))

    def run_placement_timer(self, fixture: AdmissionFixture) -> None:
        fixture.clock.advance(fixture.server.next_pass - fixture.clock.monotonic())
        fixture.server.run_once(0)

    def test_identical_placement_poll_returns_the_published_position_without_writes(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        with PlacementFixture() as fixture:
            peer = fixture.fixture_owner(os.getpid(), "fixture-peer")
            fixture.free(0)
            fixture.hold_starts()
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints,
                candidates=(local_candidate(fixture),)))
            initial, = fixture.connect(request).replies()
            assert isinstance(initial, wire.PlacementWait)
            self.assertEqual(initial.position, 1)
            caller = records.run_owner(peer.pid, peer.start_time)
            self.assertEqual(fixture.server.caller_versions[caller], request.code_version)
            self.assert_unchanged_placement_poll(fixture, request, initial)

    def test_cached_placement_poll_does_not_read_admission_configuration(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        service_uid = os.getuid()
        for uid in (service_uid, service_uid + 10000):
            with self.subTest(uid=uid), PlacementFixture() as fixture, fixture.peer_account(uid):
                fixture.hold_starts()
                owner = fixture.account_owner(uid)
                request = placement_request(fixture)
                request = replace(request, episode=f"uid:{uid}:{request.episode}",
                    owner=replace(request.owner, pid=owner.pid, start_time=owner.start_time),
                    run=wire.RunIdentity(owner.pid, owner.start_time),
                    constraints=replace(request.constraints, candidates=(local_candidate(fixture),)))
                initial, = fixture.connect(request).replies()
                assert isinstance(initial, wire.PlacementWait)
                with (mock.patch.object(conf, "read", wraps=conf.read) as read,
                      mock.patch.object(service, "process_uid", wraps=service.process_uid) as account):
                    self.assert_unchanged_placement_poll(fixture, request, initial)
                    read.assert_not_called()
                    if uid != service_uid:
                        account.assert_called_once_with(owner.pid, fixture.proc.path)
                    else:
                        account.assert_not_called()

    def test_identical_placement_poll_without_identity_uses_the_published_wait(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        with PlacementFixture() as fixture:
            _ = fixture.fixture_owner(os.getpid(), "fixture-peer")
            fixture.hold_starts()
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(local_candidate(fixture),)))
            envelope = cast(dict[str, object], json.loads(wire.encode_request(request)))
            del cast(dict[str, object], envelope["data"])["identity"]
            encoded = json.dumps(envelope) + "\n"
            initial, = fixture.connect(encoded).replies()
            assert isinstance(initial, wire.PlacementWait)
            self.assertEqual(fixture.server.placement_requests[request.episode].identity.step, request.profile.step)
            self.assert_unchanged_placement_poll(fixture, encoded, initial)

    def test_identical_placement_poll_with_a_canonicalized_cargo_folder_uses_the_published_wait(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        with PlacementFixture() as fixture:
            _ = fixture.fixture_owner(os.getpid(), "fixture-peer")
            fixture.hold_starts()
            cargo = fixture.root / "cargo"
            cargo.mkdir()
            alias = fixture.root / "alias"
            alias.symlink_to(cargo, target_is_directory=True)
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(local_candidate(fixture),),
                cargo_folder=placement.CargoFolder(alias)))
            initial, = fixture.connect(request).replies()
            assert isinstance(initial, wire.PlacementWait)
            self.assertEqual(fixture.server.placement_requests[request.episode].constraints.cargo_folder,
                             placement.CargoFolder(cargo))
            self.assert_unchanged_placement_poll(fixture, request, initial)

    def test_placement_poll_with_an_ended_execution_reading_returns_closed(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        with PlacementFixture() as fixture:
            _ = fixture.fixture_owner(os.getpid(), "fixture-peer")
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(local_candidate(fixture),)))
            initial, = fixture.connect(request).replies()
            assert isinstance(initial, wire.LocalPlacementGranted)
            server = fixture.server
            evidence = server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            self.assertEqual(evidence.processes[request.owner.pid].start, request.owner.start_time)
            self.assertIn(request.episode, server.reservations)
            fixture.clock.advance(1)
            peer = fixture.connect(request)
            server.system_reader = FixedSystemReader(replace(evidence, lifetimes={request.episode: gate.EndedStep()},
                read_at=fixture.clock.now(), started_monotonic=fixture.clock.monotonic()))
            os.eventfd_write(server.reading_events, 1)
            closed, = peer.replies()
            assert isinstance(closed, wire.DispatchStatus), type(closed)
            self.assertEqual(closed.state, placement.ClosedAssignment("local execution has ended"))

    def test_placement_poll_with_a_dead_coordinator_reading_drops_the_published_wait(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        with PlacementFixture() as fixture:
            fixture.hold_starts()
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(local_candidate(fixture),)))
            initial, = fixture.connect(request).replies()
            assert isinstance(initial, wire.PlacementWait)
            self.assertEqual(initial.position, 1)
            server = fixture.server
            evidence = server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            owner = wire.Owner(request.owner.pid, request.owner.start_time)
            peer = fixture.connect(request)
            fixture.proc.kill(owner)
            server.system_reader = FixedSystemReader(replace(evidence,
                processes={pid: state for pid, state in evidence.processes.items() if pid != owner.pid},
                read_at=fixture.clock.now(), started_monotonic=fixture.clock.monotonic()))
            os.eventfd_write(server.reading_events, 1)
            waiting, = peer.replies()
            assert isinstance(waiting, wire.PlacementWait)
            self.assertEqual(waiting.position, 0)

    def test_identical_placement_poll_returns_the_grant_after_the_timer_pass(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        with PlacementFixture() as fixture:
            fixture.configure(waiting_pass_seconds=1, min_pass_gap_seconds=0)
            fixture.free(0)
            fixture.hold_starts()
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints,
                candidates=(local_candidate(fixture),)))
            initial, = fixture.connect(request).replies()
            self.assertIsInstance(initial, wire.PlacementWait)
            fixture.free(60 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            reservation = fixture.server.reservations[request.episode]
            with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed:
                granted, = fixture.connect(request).replies()
                assert isinstance(granted, wire.LocalPlacementGranted)
                self.assertEqual(granted.grant.reservation, reservation.reservation_id)
                passed.assert_called_once()

    def test_identical_placement_poll_for_an_assignment_or_closed_runs_a_pass(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate

        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assigned, = fixture.server.handle_request(request)
            assert isinstance(assigned, wire.Assignment)
            with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed:
                self.assertEqual(fixture.server.handle_request(request), [assigned])
                passed.assert_called_once()

            cancel = wire.CancelPlacement("cancel-remote", request.episode, request.owner, "cancelled")
            self.assertEqual(fixture.server.handle_request(cancel), [wire.Ok()])
            closed, = fixture.server.handle_request(request)
            assert isinstance(closed, wire.DispatchStatus)
            self.assertEqual(closed.state, placement.ClosedAssignment("cancelled"))
            with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed:
                self.assertEqual(fixture.server.handle_request(request), [closed])
                passed.assert_called_once()

    def test_placement_poll_with_changed_constraints_runs_a_pass(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        with PlacementFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints,
                candidates=(local_candidate(fixture),)))
            self.assertIsInstance(fixture.connect(request).replies()[0], wire.PlacementWait)
            evidence = fixture.server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            fixture.server.system_reader = FixedSystemReader(evidence)
            changed = replace(request, constraints=replace(request.constraints, package="another-package"))
            with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed:
                self.assertIsInstance(fixture.connect(changed).replies()[0], wire.PlacementWait)
                passed.assert_called_once()
            self.assertEqual(fixture.server.placement_requests[request.episode].constraints, request.constraints)

    def test_identical_placement_poll_runs_a_pass_for_pending_configuration(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        with PlacementFixture() as fixture:
            fixture.configure(waiting_pass_seconds=10, min_pass_gap_seconds=0.25)
            fixture.clock.advance(0.25)
            fixture.free(0)
            fixture.hold_starts()
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints,
                candidates=(local_candidate(fixture),)))
            initial, = fixture.connect(request).replies()
            assert isinstance(initial, wire.PlacementWait)
            with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed:
                with self.subTest(pending=False):
                    self.assertEqual(fixture.server.handle_request(request), [initial])
                    passed.assert_not_called()
                passed.reset_mock()
                fixture.configure(waiting_pass_seconds=2)
                fixture.clock.advance(0.25)
                with self.subTest(pending=True):
                    waiting, = fixture.connect(request).replies()
                    assert isinstance(waiting, wire.PlacementWait)
                    self.assertEqual(waiting.position, initial.position)
                    passed.assert_called_once()
                    configuration = fixture.server.configuration
                    assert isinstance(configuration, gate.UsableAdmissionConfiguration)
                    self.assertEqual(configuration.config.waiting_pass_seconds, 2)

    def test_placement_poll_sees_expired_provider_evidence_after_the_timer_pass(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate

        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            blocked = placement.TemporaryExclusion("busy")
            _ = publish_candidate(fixture, request, blocked)
            with mock.patch.object(fixture.server, "_evidence_lifetime", return_value=1.25):
                fixture.clock.advance(1)
                initial, = fixture.connect(request).replies()
                assert isinstance(initial, wire.PlacementWait)
                self.assertEqual(initial.eligibility[0].eligibility, blocked)
                self.assertEqual(fixture.server.handle_request(request), [initial])
                fixture.clock.advance(0.5)
                self.assert_unchanged_placement_poll(fixture, request, initial)
                # The timer refreshes provider evidence for a cached wait.
                self.run_placement_timer(fixture)
                repeated, = fixture.connect(request).replies()
                assert isinstance(repeated, wire.PlacementWait)
                self.assertEqual(repeated.eligibility[0].eligibility,
                                 placement.EvidenceNeeded("provider evidence expired"))

    def test_placement_waits_for_the_preferred_mac_linux_reading(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, local_candidate, mac_linux_candidate, mac_linux_fixture, placement_request, publish_candidate,
        )

        with PlacementFixture() as fixture:
            _ = mac_linux_fixture(fixture)
            guest = mac_linux_candidate()
            local = local_candidate(fixture)
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(guest, local)))

            waiting, = fixture.connect(request).replies()
            assert isinstance(waiting, wire.PlacementWait), waiting
            self.assertEqual(waiting.eligibility, (
                placement.CandidateEligibility(guest.destination, placement.EvidenceNeeded("not read")),
                placement.CandidateEligibility(local.destination, placement.AllowedHost()),
            ))
            self.assertNotIn(request.episode, fixture.server.reservations)

            _ = publish_candidate(fixture, request)
            self.run_placement_timer(fixture)
            granted, = fixture.connect(request).replies()
            assert isinstance(granted, wire.Assignment), granted
            self.assertEqual(granted.assignment.destination, guest.destination)

    def test_placement_uses_natedev_for_a_busy_or_unreachable_mac_linux_reading(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, local_candidate, mac_linux_candidate, mac_linux_fixture, placement_request, publish_candidate,
        )

        for reason in ("busy", placement.MAC_LINUX_NO_ANSWER):
            with self.subTest(reason=reason), PlacementFixture() as fixture:
                _ = mac_linux_fixture(fixture)
                guest = mac_linux_candidate()
                local = local_candidate(fixture)
                request = placement_request(fixture)
                request = replace(request, constraints=replace(request.constraints, candidates=(guest, local)))
                exclusion = placement.TemporaryExclusion(reason)
                _ = publish_candidate(fixture, request, exclusion)

                granted, = fixture.connect(request).replies()
                assert isinstance(granted, wire.LocalPlacementGranted), granted
                self.assertEqual(granted.assignment.destination, local.destination)
                self.assertEqual(fixture.server.placement_eligibility[request.episode][0],
                                 placement.CandidateEligibility(guest.destination, exclusion))

    def test_placement_uses_natedev_at_the_mac_linux_reading_deadline(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, local_candidate, mac_linux_candidate, mac_linux_fixture, placement_request,
        )
        from scripts.build_targets.mac_linux import GUEST_REF_TIMEOUT_SECONDS

        with PlacementFixture() as fixture:
            _ = mac_linux_fixture(fixture)
            guest = mac_linux_candidate()
            local = local_candidate(fixture)
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(guest, local)))

            waiting, = fixture.connect(request).replies()
            assert isinstance(waiting, wire.PlacementWait), waiting
            fixture.clock.advance(GUEST_REF_TIMEOUT_SECONDS - 1)
            _ = fixture.run_pass()
            still_waiting, = fixture.connect(request).replies()
            self.assertIsInstance(still_waiting, wire.PlacementWait)

            fixture.clock.advance(1)
            _ = fixture.run_pass()
            granted, = fixture.connect(request).replies()
            assert isinstance(granted, wire.LocalPlacementGranted), granted
            self.assertEqual(granted.assignment.destination, local.destination)
            self.assertEqual(fixture.server.placement_eligibility[request.episode][0],
                             placement.CandidateEligibility(
                                 guest.destination, placement.TemporaryExclusion(placement.MAC_LINUX_NO_ANSWER)))

    def test_placement_uses_natedev_before_reading_an_assigned_guest(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, assignment_for, local_candidate, mac_linux_candidate, mac_linux_fixture,
            placement_request, publish_candidate,
        )

        with PlacementFixture() as fixture:
            _ = mac_linux_fixture(fixture)
            guest = mac_linux_candidate()
            local = local_candidate(fixture)
            holder = placement_request(fixture, session=OwningSession("guest-holder"))
            holder = replace(holder, constraints=replace(holder.constraints, candidates=(guest,)))
            _ = publish_candidate(fixture, holder)
            assignment = assignment_for(fixture, holder)
            self.assertEqual(assignment.destination, guest.destination)
            saved = fixture.server.assignments[fixture.server.current_assignments[holder.episode]]
            self.assertNotIsInstance(saved.permission, placement.PermissionConsumed)

            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(guest, local)))
            key = fixture.server.host_evidence_key(request, guest.destination)
            self.assertIsInstance(fixture.server.host_evidence(key), placement.HostEvidenceUnavailable)
            granted, = fixture.connect(request).replies()
            assert isinstance(granted, wire.LocalPlacementGranted), granted
            self.assertEqual(granted.assignment.destination, local.destination)
            self.assertEqual(fixture.server.placement_eligibility[request.episode][0],
                             placement.CandidateEligibility(guest.destination, placement.TemporaryExclusion("busy")))
            self.assertNotIn(key, fixture.server.host_readers)

    def test_placement_waits_after_a_cached_guest_backoff_expires(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, local_candidate, mac_linux_candidate, mac_linux_fixture, placement_request, publish_candidate,
        )

        with PlacementFixture() as fixture:
            backoff = mac_linux_fixture(fixture)
            now = fixture.clock.now()
            _ = backoff.write_text(str(now + 1))
            os.utime(backoff, (now - 1, now - 1))
            guest = mac_linux_candidate()
            local = local_candidate(fixture)
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(guest, local)))
            key = fixture.server.host_evidence_key(request, guest.destination)
            fixture.server.host_requests[key] = request
            reading = fixture.server.read_provider_evidence(key)
            assert isinstance(reading, placement.FreshHostEvidence), reading
            self.assertEqual(reading.eligibility, placement.TemporaryExclusion("mac_linux_backoff"))
            self.assertEqual(reading.observed_at, now)
            fixture.server.publish_host_evidence(reading)

            fixture.clock.advance(2)
            waiting, = fixture.connect(request).replies()
            assert isinstance(waiting, wire.PlacementWait), waiting
            self.assertEqual(waiting.eligibility, (
                placement.CandidateEligibility(guest.destination, placement.EvidenceNeeded("mac_linux_reading_required")),
                placement.CandidateEligibility(local.destination, placement.AllowedHost()),
            ))
            _ = publish_candidate(fixture, request)
            self.run_placement_timer(fixture)
            granted, = fixture.connect(request).replies()
            assert isinstance(granted, wire.Assignment), granted
            self.assertEqual(granted.assignment.destination, guest.destination)

    def test_placement_uses_natedev_before_reading_a_blocked_guest(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, local_candidate, mac_linux_candidate, mac_linux_fixture, placement_request,
        )
        from scripts.mac_test import mac_test

        with PlacementFixture() as fixture:
            _ = mac_linux_fixture(fixture)
            paths = mac_test.state_paths()
            self.assertTrue(paths.block.is_relative_to(fixture.root))
            paths.block.parent.mkdir(parents=True, exist_ok=True)
            since = datetime.fromtimestamp(fixture.clock.now(), UTC)
            mac_test.write_block(paths.block, mac_test.ActiveMacBlock(
                "fixture", "private hold", since, since + timedelta(hours=1),
                mac_test.RecordedSession("guest-block-holder", "fixture"), mac_test.NO_SHOWRUNNER,
                "off_before", mac_test.FREE_MESSAGE_NOT_NEEDED))
            guest = mac_linux_candidate()
            local = local_candidate(fixture)
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(guest, local)))
            key = fixture.server.host_evidence_key(request, guest.destination)
            self.assertIsInstance(fixture.server.host_evidence(key), placement.HostEvidenceUnavailable)

            granted, = fixture.connect(request).replies()
            assert isinstance(granted, wire.LocalPlacementGranted), granted
            self.assertEqual(granted.assignment.destination, local.destination)
            self.assertEqual(fixture.server.placement_eligibility[request.episode][0],
                             placement.CandidateEligibility(guest.destination, placement.TemporaryExclusion("blocked")))
            self.assertNotIn(key, fixture.server.host_readers)

    def test_placement_excludes_the_guest_when_its_block_file_is_invalid(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, local_candidate, mac_linux_candidate, mac_linux_fixture, placement_request,
        )
        from scripts.mac_test import mac_test

        with PlacementFixture() as fixture:
            _ = mac_linux_fixture(fixture)
            block = mac_test.state_paths().block
            self.assertTrue(block.is_relative_to(fixture.root))
            block.parent.mkdir(parents=True, exist_ok=True)
            _ = block.write_text("{invalid json", encoding="utf-8")
            guest = mac_linux_candidate()
            local = local_candidate(fixture)
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(guest, local)))

            granted, = fixture.connect(request).replies()
            assert isinstance(granted, wire.LocalPlacementGranted), granted
            self.assertEqual(granted.assignment.destination, local.destination)
            candidate = fixture.server.placement_eligibility[request.episode][0]
            self.assertEqual(candidate.destination, guest.destination)
            assert isinstance(candidate.eligibility, placement.TemporaryExclusion), candidate
            self.assertIn("The guest block file is unreadable.", candidate.eligibility.reason)
            self.assertIn(str(block), candidate.eligibility.reason)

    def test_a_guest_reading_wait_leaves_natedev_room_for_the_next_request(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, aws_gate_request, local_candidate, mac_linux_candidate, mac_linux_fixture,
            placement_request, read_aws_gate_evidence,
        )

        with PlacementFixture() as fixture:
            request = aws_gate_request(fixture)
            registry = fixture.root / "build_targets.conf"
            scaler_settings = registry.read_text(encoding="utf-8").split("\n", 1)[1]
            _ = mac_linux_fixture(fixture)
            settings = registry.read_text(encoding="utf-8").replace(
                "targets=mac-linux,natedev\n", "targets=mac-linux,natedev,claude-aws\n", 1)
            _ = registry.write_text(settings + scaler_settings, encoding="utf-8")
            _ = read_aws_gate_evidence(fixture, request)
            fixture.free(12 * GIB)
            guest = mac_linux_candidate()
            local = local_candidate(fixture)
            older = placement_request(fixture, since=fixture.clock.now() - 1)
            older = replace(older, constraints=replace(older.constraints, candidates=(guest, local)))
            initial, = fixture.connect(older).replies()
            assert isinstance(initial, wire.PlacementWait), initial
            self.assertEqual(initial.eligibility[0], placement.CandidateEligibility(
                guest.destination, placement.EvidenceNeeded("not read")))

            request = replace(request, route=older.route, constraints=replace(request.constraints,
                candidates=(local, *request.constraints.candidates)))
            with mock.patch.object(fixture.server, "_aws_gate_spending", return_value=placement.AllowedHost()) as spending:
                granted, = fixture.connect(request).replies()
            assert isinstance(granted, wire.LocalPlacementGranted), granted
            self.assertEqual(granted.assignment.destination, local.destination)
            self.assertNotIn(older.episode, fixture.server.reservations)
            spending.assert_not_called()

    def test_waiting_for_the_guest_preserves_an_earlier_mac_hold(self) -> None:
        from scripts.build_queue.test_placement import (
            ManualEvidenceWorker, PlacementFixture, mac_linux_candidate, mac_linux_fixture,
            placement_request, publish_candidate, write_config_fixture,
        )

        with PlacementFixture() as fixture, mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker):
            fixture.configure(waiting_pass_seconds=1)
            _ = write_config_fixture(fixture.root / "mac_test.conf", "probe_timeout_s=5\n", template="mac_test")
            _ = mac_linux_fixture(fixture)
            older = placement_request(fixture, since=fixture.clock.now() - 1)
            mac, = older.constraints.candidates
            older = replace(older, constraints=replace(older.constraints, candidates=(mac, mac_linux_candidate())))
            waiting, = fixture.connect(older).replies()
            assert isinstance(waiting, wire.PlacementWait), waiting
            self.assertTrue(all(isinstance(candidate.eligibility, placement.EvidenceNeeded)
                                for candidate in waiting.eligibility), waiting.eligibility)

            _ = publish_candidate(fixture, older)
            fixture.clock.advance(7)
            younger = placement_request(fixture, session=OwningSession("younger-session"))
            _ = publish_candidate(fixture, younger)
            reply, = fixture.connect(younger).replies()
            self.assertIsInstance(reply, wire.PlacementWait)
            self.assertIn((older.episode, mac.destination), fixture.server.placement_holds)
            self.assertNotIn(younger.episode, fixture.server.current_assignments)

    def test_guest_wait_preserves_the_local_return_of_a_younger_continuation_claim(self) -> None:
        from scripts.build_queue.test_placement import (
            ManualEvidenceWorker, PlacementFixture, local_candidate, mac_linux_candidate, mac_linux_fixture,
            placement_request,
        )

        with PlacementFixture() as fixture, mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker):
            _ = mac_linux_fixture(fixture)
            older_since = fixture.clock.now() - 1
            run = fixture.run_owner()
            prior = fixture.finish_step(fixture.request(run=run))
            fixture.allow_next_start()
            fixture.free(12 * GIB)
            local = local_candidate(fixture)
            older = placement_request(fixture, since=older_since)
            older = replace(older, constraints=replace(older.constraints, candidates=(local,)))
            self.assertIsInstance(fixture.connect(older).replies()[0], wire.PlacementWait)
            self.assertIsInstance(fixture.server.slots[prior.reservation], records.BetweenStepsSlot)

            younger = placement_request(fixture)
            younger = replace(younger, run=run,
                constraints=replace(younger.constraints, candidates=(mac_linux_candidate(), local)))
            waiting, = fixture.connect(younger).replies()
            assert isinstance(waiting, wire.PlacementWait), waiting
            self.assertIsInstance(waiting.eligibility[0].eligibility, placement.EvidenceNeeded)

            granted, = fixture.connect(older).replies()
            assert isinstance(granted, wire.LocalPlacementGranted), granted
            self.assertEqual(granted.assignment.destination, local.destination)
            self.assertNotIn(prior.reservation, fixture.server.slots)
            self.assertNotIn(younger.episode, fixture.server.reservations)

    def test_two_guest_waits_keep_the_continuation_for_local_fallback(self) -> None:
        from scripts.build_queue.test_placement import (
            ManualEvidenceWorker, PlacementFixture, local_candidate, mac_linux_candidate, mac_linux_fixture,
            placement_request, publish_candidate,
        )

        with PlacementFixture() as fixture, mock.patch.object(service, "HostEvidenceWorker", ManualEvidenceWorker):
            _ = mac_linux_fixture(fixture)
            older_since = fixture.clock.now() - 1
            run = fixture.run_owner()
            prior = fixture.finish_step(fixture.request(run=run))
            fixture.hold_starts()
            guest, local = mac_linux_candidate(), local_candidate(fixture)
            older = placement_request(fixture, since=older_since)
            older = replace(older, constraints=replace(older.constraints, candidates=(guest, local)))
            younger = placement_request(fixture, session=OwningSession("younger-session"))
            younger = replace(younger, run=run, constraints=replace(younger.constraints, candidates=(guest, local)))

            for request in (older, younger):
                waiting, = fixture.connect(request).replies()
                assert isinstance(waiting, wire.PlacementWait), waiting
                self.assertIsInstance(waiting.eligibility[0].eligibility, placement.EvidenceNeeded)
            continuation = fixture.server.slots.get(prior.reservation)

            _ = publish_candidate(fixture, younger, placement.TemporaryExclusion(placement.MAC_LINUX_NO_ANSWER))
            _ = fixture.run_pass()
            granted, = fixture.connect(younger).replies()
            assert isinstance(granted, wire.LocalPlacementGranted), granted
            self.assertEqual(granted.assignment.destination, local.destination)
            self.assertIsInstance(continuation, records.ContinuationSlot)
            self.assertNotIn(older.episode, fixture.server.reservations)

    def test_mac_linux_placement_probe_shares_one_deadline_across_connect_retries(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, mac_linux_candidate, mac_linux_fixture, placement_request,
        )
        from scripts.build_targets import mac_linux

        with PlacementFixture() as fixture:
            _ = mac_linux_fixture(fixture)
            guest = mac_linux_candidate()
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(guest,)))
            key = fixture.server.host_evidence_key(request, guest.destination)
            fixture.server.host_requests[key] = request
            failed = subprocess.CompletedProcess(("ssh",), mac_linux.INNER_CONNECT_STATUS, "",
                mac_linux.INNER_CONNECT_MARKER + "\nssh: connect to host fixture port 22: Connection refused\n")
            expired = subprocess.TimeoutExpired(("ssh",), 5)
            with (mock.patch.object(mac_linux, "run_guest_ssh", side_effect=(failed, expired)) as ssh,
                  mock.patch.object(mac_linux, "monotonic", side_effect=(0, 0, 24, 25)),
                  mock.patch.object(mac_linux, "sleep") as retry_delay):
                reading = fixture.server.read_provider_evidence(key)
            assert isinstance(reading, placement.FreshHostEvidence), reading
            self.assertEqual(reading.eligibility, placement.TemporaryExclusion(placement.MAC_LINUX_NO_ANSWER))
            self.assertEqual([call.args[1] for call in ssh.call_args_list], [30, 5])
            retry_delay.assert_called_once_with(mac_linux.CONNECT_RETRY_SECONDS)

    def test_placement_poll_sees_a_provider_settings_edit_after_the_timer_pass(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, mac_linux_candidate, mac_linux_fixture, placement_request,
        )

        with PlacementFixture() as fixture:
            backoff = mac_linux_fixture(fixture)
            _ = backoff.write_text(str(fixture.clock.now() + 10))
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(mac_linux_candidate(),)))
            with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(backoff.parents[1])}):
                initial, = fixture.connect(request).replies()
                assert isinstance(initial, wire.PlacementWait)
                self.assertEqual(initial.eligibility[0].eligibility, placement.TemporaryExclusion("mac_linux_backoff"))
                self.assert_unchanged_placement_poll(fixture, request, initial)
                _ = mac_linux_fixture(fixture, enabled="off")
                fixture.clock.advance(0.25)
                self.assert_unchanged_placement_poll(fixture, request, initial)
                # The timer reads provider settings for a cached wait.
                self.run_placement_timer(fixture)
                repeated, = fixture.connect(request).replies()
                assert isinstance(repeated, wire.PlacementWait)
                self.assertEqual(repeated.eligibility[0].eligibility, placement.TemporaryExclusion("mac_linux_off"))

    def test_placement_poll_sees_expired_guest_backoff_after_the_timer_pass(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, mac_linux_candidate, mac_linux_fixture, placement_request,
        )

        with PlacementFixture() as fixture:
            fixture.configure(waiting_pass_seconds=10)
            backoff = mac_linux_fixture(fixture)
            _ = backoff.write_text(str(fixture.clock.now() + 1))
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(mac_linux_candidate(),)))
            with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(backoff.parents[1])}):
                initial, = fixture.connect(request).replies()
                assert isinstance(initial, wire.PlacementWait)
                self.assertEqual(initial.eligibility[0].eligibility, placement.TemporaryExclusion("mac_linux_backoff"))
                fixture.clock.advance(1)
                self.assert_unchanged_placement_poll(fixture, request, initial)
                # The timer checks guest backoff for a cached wait.
                self.run_placement_timer(fixture)
                repeated, = fixture.connect(request).replies()
                assert isinstance(repeated, wire.PlacementWait)
                self.assertEqual(repeated.eligibility[0].eligibility, placement.EvidenceNeeded("not read"))

    def test_placement_poll_sees_an_expired_attempt_retry_after_the_timer_pass(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, assignment_for, placement_request, publish_candidate,
        )

        with PlacementFixture() as fixture:
            fixture.configure(waiting_pass_seconds=10)
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            closed, = fixture.server.handle_request(wire.ClaimFailure("mac-refusal", assignment, "lost"))
            assert isinstance(closed, wire.DispatchStatus)
            assert isinstance(closed.retry, placement.RetryAt)
            fixture.clock.advance(closed.retry.deadline - fixture.clock.now() - 1)
            _ = publish_candidate(fixture, request)
            initial, = fixture.connect(request).replies()
            assert isinstance(initial, wire.PlacementWait)
            self.assertEqual(initial.eligibility[0].eligibility,
                             placement.TemporaryExclusion("retry deadline has not passed"))
            self.assertEqual(len(initial.retry_notices), 1)
            fixture.clock.advance(1)
            self.assert_unchanged_placement_poll(fixture, request, initial)
            # The timer checks the retry deadline for a cached wait.
            fixture.clock.advance(fixture.server.next_pass - fixture.clock.monotonic())
            _ = publish_candidate(fixture, request)
            fixture.server.run_once(0)
            repeated, = fixture.connect(request).replies()
            assert isinstance(repeated, wire.Assignment), repeated
            self.assertNotEqual(repeated.assignment.attempt, assignment.attempt)

    def test_placement_poll_sees_new_provider_evidence_after_the_timer_pass(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, placement_request

        with PlacementFixture() as fixture:
            fixture.configure(waiting_pass_seconds=10)
            request = placement_request(fixture)
            key = fixture.server.host_evidence_key(request, request.constraints.candidates[0].destination)
            busy = placement.FreshHostEvidence(key, fixture.clock.now(), "host", placement.TemporaryExclusion("busy"))
            host_reader = cast(Callable[[placement.HostEvidenceKey, float], service.HostEvidenceReader],
                               getattr(fixture.server, "_host_evidence_reader"))
            with mock.patch.object(service.HostEvidenceWorker, "_read"):
                reader = host_reader(key, 10)
            reader.publish(busy)
            initial, = fixture.connect(request).replies()
            assert isinstance(initial, wire.PlacementWait)
            self.assertEqual(initial.eligibility[0].eligibility, busy.eligibility)
            fixture.clock.advance(0.25)
            receive = cast(Callable[[dict[int, service.ConnectionReadiness], bool], None],
                           getattr(fixture.server, "_receive_requests"))

            def store_then_receive(ready: dict[int, service.ConnectionReadiness], listener_ready: bool) -> None:
                reader.publish(replace(busy, eligibility=placement.AllowedHost()))
                receive(ready, listener_ready)

            with mock.patch.object(fixture.server, "_receive_requests", side_effect=store_then_receive):
                self.assert_unchanged_placement_poll(fixture, request, initial)
            # The timer reads evidence stored after the loop checks its events.
            fixture.clock.advance(fixture.server.next_pass - fixture.clock.monotonic())
            reader.publish(replace(busy, observed_at=fixture.clock.now(), eligibility=placement.AllowedHost()))
            fixture.server.run_once(0)
            repeated, = fixture.connect(request).replies()
            self.assertIsInstance(repeated, wire.Assignment)

    def test_placement_control_distinguishes_missing_requests_from_owner_mismatches(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, assignment_for, placement_request, publish_candidate

        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            _ = assignment_for(fixture, request)
            other_owner = replace(request.owner, start_time=request.owner.start_time + "0")
            missing = wire.PLACEMENT_REQUEST_MISSING_REASON
            cases = (
                (wire.CancelPlacement("wrong-owner", request.episode, other_owner, "ended"),
                    "placement coordinator does not match"),
                (wire.CancelPlacement("missing-cancel", "missing-episode", request.owner, "ended"), missing),
                (wire.PrerequisiteReport("missing-report", "missing-episode", request.owner, request.prerequisite), missing),
            )
            for control, reason in cases:
                with self.subTest(control=type(control).__name__, episode=control.episode):
                    self.assertEqual(fixture.connect(control).replies(), [wire.Refused(reason)])
            self.assertEqual(fixture.server.placement_requests.get(request.episode), request)

    def test_repeated_placement_polls_share_the_pass_until_the_next_reading(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        for report_prerequisite in (False, True):
            with self.subTest(report_prerequisite=report_prerequisite), PlacementFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = placement_request(fixture)
                request = replace(request, constraints=replace(request.constraints,
                    candidates=(local_candidate(fixture),)))
                initial, = fixture.connect(request).replies()
                assert isinstance(initial, wire.PlacementWait)
                assert isinstance(initial.wait, wire.Wait)
                evidence = fixture.server.system_evidence
                assert isinstance(evidence, service.SystemEvidence)
                reader = FixedSystemReader(evidence)
                fixture.server.system_reader = reader
                state = fixture.state_text()
                with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed:
                    for index in range(20):
                        fixture.clock.advance(0.02)
                        if report_prerequisite:
                            report = wire.PrerequisiteReport(f"same-prerequisite-{index}", request.episode,
                                request.owner, request.prerequisite)
                            self.assertEqual(fixture.connect(report).replies(), [wire.Ok()])
                        reply, = fixture.connect(request).replies()
                        self.assertEqual(reply, initial)
                    passed.assert_not_called()
                self.assertEqual(fixture.state_text(), state)
                reader.latest = replace(evidence, host=records.HostMemory(60 * GIB, 60 * GIB),
                    pressure=records.NO_PRESSURE_HOLD, read_at=fixture.clock.now(),
                    started_monotonic=fixture.clock.monotonic())
                os.eventfd_write(fixture.server.reading_events, 1)
                fixture.server.run_once(0)
                admitted, = fixture.connect(request).replies()
                self.assertIsInstance(admitted, wire.LocalPlacementGranted)
                self.assertIn(request.episode, fixture.server.reservations)

    def read_only_status(self, fixture: AdmissionFixture, assignment: placement.PlacementAssignment
                         ) -> status_wire.KnownPlacementStatus | status_wire.UnknownPlacementStatus:
        fields = tuple(service.ServiceState.__dataclass_fields__)
        before = wire.encode_record({name: getattr(fixture.server, name) for name in fields})
        files = {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in fixture.state_dir.rglob("*") if path.is_file()}
        next_pass = fixture.server.next_pass
        with mock.patch.object(fixture.server, "persist") as persist, \
                mock.patch.object(fixture.server, "_record_caller_version") as record_version, \
                mock.patch.object(fixture.server, "_request_host_evidence",
                    side_effect=AssertionError("status must not request evidence")):
            reply, = fixture.server.handle_request(status_wire.PlacementStatusRequest(
                assignment.episode, assignment.attempt, code_version="status-reader"))
            self.assertEqual(fixture.server.handle_request(status_wire.PlacementStatusRequest(
                assignment.episode, assignment.attempt)), [reply])
            persist.assert_not_called()
            record_version.assert_not_called()
        self.assertEqual(wire.encode_record({name: getattr(fixture.server, name) for name in fields}), before)
        self.assertEqual({str(path): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in fixture.state_dir.rglob("*") if path.is_file()}, files)
        self.assertEqual(fixture.server.next_pass, next_pass)
        self.assertIsInstance(reply, status_wire.PlacementStatusReply)
        assert isinstance(reply, status_wire.PlacementStatusReply)
        return reply.status

    def test_released_failed_cancelled_and_ended_local_placements_answer_closed(self) -> None:
        from scripts.build_queue import admission_wire as status_wire, placement
        from scripts.build_queue.test_placement import PlacementFixture, placement_request, local_candidate

        for ending in ("released", "failed", "cancelled", "run-ended"):
            with self.subTest(ending=ending), PlacementFixture() as fixture:
                request = placement_request(fixture)
                request = replace(request, run=fixture.run_owner(), constraints=replace(request.constraints,
                    candidates=(local_candidate(fixture),)))
                granted, = fixture.connect(request).replies()
                assert isinstance(granted, wire.LocalPlacementGranted)
                status = self.read_only_status(fixture, granted.assignment)
                assert isinstance(status, status_wire.KnownPlacementStatus)
                self.assertEqual(status.state, placement.CurrentAssignment())
                if ending in ("released", "failed"):
                    self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                        "local-release", granted.grant.reservation, 1 if ending == "failed" else 0)), [wire.Ok()])
                elif ending == "cancelled":
                    self.assertEqual(fixture.server.handle_request(wire.CancelPlacement(
                        "local-cancel", request.episode, request.owner, "caller cancelled")), [wire.Ok()])
                else:
                    assert isinstance(request.run, wire.RunIdentity)
                    fixture.end_run(request.run)
                status = self.read_only_status(fixture, granted.assignment)
                assert isinstance(status, status_wire.KnownPlacementStatus)
                self.assertIsInstance(status.state, placement.ClosedAssignment)
                assert isinstance(status.state, placement.ClosedAssignment)
                self.assertTrue(status.state.reason)
                if ending == "cancelled":
                    self.assertEqual(status.state.reason, "cancelled")
                replay, = fixture.server.handle_request(request)
                assert isinstance(replay, wire.DispatchStatus)
                self.assertEqual(replay.state, status.state)
                with mock.patch.object(client, "placement_status", return_value=status_wire.PlacementStatusReply(status)), \
                        redirect_stderr(io.StringIO()), io.StringIO() as output:
                    with redirect_stdout(output):
                        self.assertEqual(client.main(["placement-status", "--episode", request.episode,
                            "--attempt", granted.assignment.attempt]), 1)
                    self.assertEqual(output.getvalue().split("\t")[0], "closed")

    def test_released_mac_claim_answers_closed_from_cached_evidence(self) -> None:
        from scripts.build_queue import admission_wire as status_wire, placement
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
        )

        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assignment)
            self.assertEqual(fixture.server.handle_request(consume_permission("consume-mac", assignment, claim)),
                [wire.FirstAuthorization(assignment, claim)])
            status = self.read_only_status(fixture, assignment)
            assert isinstance(status, status_wire.KnownPlacementStatus)
            self.assertEqual(status.state, placement.CurrentAssignment())
            fixture.proc.kill(wire.Owner(request.owner.pid, request.owner.start_time))
            fixture.clock.advance(1)
            _ = publish_candidate(fixture, request, claims=())
            status = self.read_only_status(fixture, assignment)
            assert isinstance(status, status_wire.KnownPlacementStatus)
            self.assertEqual(status.state, placement.ClosedAssignment("provider claim released"))
            self.assertEqual(status.claim, claim)
            replay, = fixture.server.handle_request(consume_permission("repeat-consume-mac", assignment, claim))
            assert isinstance(replay, wire.DispatchStatus)
            self.assertEqual(replay.state, status.state)

    def test_empty_provider_reading_keeps_a_live_holder_or_pending_start_current(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
        )
        from scripts.mac_test import claim_state, offload

        for blocker in ("live holder", "pending start", "pending start with generation"):
            with self.subTest(blocker=blocker), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                self.assertEqual(fixture.server.handle_request(consume_permission("consume-current", assignment, claim)),
                    [wire.FirstAuthorization(assignment, claim)])
                pending: Path | None = None
                if blocker != "live holder":
                    fixture.proc.kill(wire.Owner(request.owner.pid, request.owner.start_time))
                    identity = claim_state.ClaimIdentity(claim.claim_id, claim.episode, claim.attempt)
                    scope_key = "run" if blocker == "pending start" else "mirror/" + "a" * 32
                    pending = offload.pending_start_path(assignment.destination.target, identity, scope_key=scope_key)
                    pending.parent.mkdir(parents=True, exist_ok=True)
                    _ = pending.write_text("pending startup\n")
                fixture.clock.advance(1)
                _ = publish_candidate(fixture, request, claims=())
                status = self.read_only_status(fixture, assignment)
                assert isinstance(status, status_wire.KnownPlacementStatus)
                self.assertEqual(status.state, placement.CurrentAssignment())
                replay, = fixture.server.handle_request(consume_permission("repeat-current", assignment, claim))
                assert isinstance(replay, wire.DispatchStatus)
                self.assertEqual(replay.state, placement.CurrentAssignment())
                if blocker == "live holder":
                    waiting, = fixture.server.handle_request(request)
                    self.assertIsInstance(waiting, wire.PlacementWait)
                if blocker == "pending start with generation":
                    assert pending is not None
                    key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                    _ = fixture.run_pass()
                    self.assertEqual(fixture.server.current_assignments[assignment.episode], key)
                    pending.unlink()
                    _ = fixture.run_pass()
                    self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("lost"))

    def test_a_kept_claim_frees_its_assignment_when_holder_and_owner_end(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
            coordinator_holder,
        )

        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assignment)
            self.assertEqual(fixture.server.handle_request(consume_permission("consume-kept", assignment, claim)),
                [wire.FirstAuthorization(assignment, claim)])
            kept = placement.AdoptedProviderClaim(claim, coordinator_holder(assignment), assignment.session,
                                                  assignment.destination.host, "host-generation-a")
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            fixture.proc.kill(wire.Owner(request.owner.pid, request.owner.start_time))
            fixture.clock.advance(1)
            _ = publish_candidate(fixture, request, claims=(kept,))
            status = self.read_only_status(fixture, assignment)
            assert isinstance(status, status_wire.KnownPlacementStatus)
            self.assertEqual(status.state, placement.ClosedAssignment("provider claim released"))
            _ = fixture.run_pass()
            self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("lost"))
            self.assertNotIn(assignment.episode, fixture.server.current_assignments)
            following = placement_request(fixture)
            _ = publish_candidate(fixture, following, claims=(kept,))
            reply, = fixture.server.handle_request(following)
            assert isinstance(reply, wire.Assignment)
            self.assertEqual(reply.assignment.destination, assignment.destination)

    def test_an_owner_that_asks_again_after_its_claim_holder_ends_goes_back_to_its_lines(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
        )
        from scripts.mac_test import claim_state, offload

        for case in ("holder ended", "holder ended, start pending", "holder live"):
            with self.subTest(case=case), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                self.assertEqual(fixture.server.handle_request(consume_permission("consume-runner", assignment, claim)),
                    [wire.FirstAuthorization(assignment, claim)])
                # The claim holder is a runner process below the owner, so the owner outlives it.
                runner = fixture.proc.spawn()
                holder = placement.ClaimHolderIdentity(assignment.owner.host, runner.pid, runner.start_time)
                key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                fixture.server.assignments[key] = replace(fixture.server.assignments[key], holder=holder)
                fixture.server.persist()
                kept = placement.AdoptedProviderClaim(claim, holder, assignment.session,
                                                      assignment.destination.host, "host-generation-a")
                pending = offload.pending_start_path(assignment.destination.target,
                    claim_state.ClaimIdentity(claim.claim_id, claim.episode, claim.attempt))
                if case != "holder live":
                    fixture.proc.kill(runner)
                if case == "holder ended, start pending":
                    pending.parent.mkdir(parents=True, exist_ok=True)
                    _ = pending.write_text("pending startup\n")
                fixture.clock.advance(1)
                _ = publish_candidate(fixture, request, claims=(kept,))
                waiting, = fixture.server.handle_request(request)
                self.assertIsInstance(waiting, wire.PlacementWait)
                if case == "holder live":
                    self.assertEqual(fixture.server.assignments[key].state, placement.CurrentAssignment())
                    self.assertEqual(fixture.server.current_assignments[assignment.episode], key)
                    continue
                self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("lost"))
                self.assertNotIn(assignment.episode, fixture.server.current_assignments)
                self.assertEqual(pending.exists(), case == "holder ended, start pending")
                fixture.clock.advance(181)
                _ = publish_candidate(fixture, request, claims=(kept,))
                reply, = fixture.server.handle_request(request)
                assert isinstance(reply, wire.Assignment)
                self.assertEqual(reply.assignment.episode, assignment.episode)
                self.assertNotEqual(reply.assignment.attempt, assignment.attempt)

    def test_an_unconfirmed_claim_holder_frees_its_assignment_after_its_owner_ends(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
            coordinator_holder,
        )
        from scripts.mac_test import claim_state, offload

        for case in ("pass", "restart", "owner live", "claim listed", "start pending"):
            with self.subTest(case=case), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                self.assertEqual(fixture.server.handle_request(consume_permission("consume-unconfirmed", assignment, claim)),
                    [wire.FirstAuthorization(assignment, claim)])
                key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                fixture.server.assignments[key] = replace(fixture.server.assignments[key],
                    holder=placement.ClaimHolderUnconfirmed("not consumed"))
                fixture.server.persist()
                if case != "owner live":
                    fixture.proc.kill(wire.Owner(request.owner.pid, request.owner.start_time))
                if case == "start pending":
                    pending = offload.pending_start_path(assignment.destination.target,
                        claim_state.ClaimIdentity(claim.claim_id, claim.episode, claim.attempt))
                    pending.parent.mkdir(parents=True, exist_ok=True)
                    _ = pending.write_text("")
                if case == "restart":
                    fixture.restart()
                fixture.clock.advance(1)
                claims = ((placement.AdoptedProviderClaim(claim, coordinator_holder(assignment), assignment.session,
                                                          assignment.destination.host, "host-generation-a"),)
                          if case == "claim listed" else ())
                _ = publish_candidate(fixture, request, claims=claims)
                _ = fixture.run_pass()
                if case in ("pass", "restart"):
                    self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("lost"))
                    self.assertNotIn(assignment.episode, fixture.server.current_assignments)
                else:
                    self.assertEqual(fixture.server.assignments[key].state, placement.CurrentAssignment())
                    self.assertEqual(fixture.server.current_assignments[assignment.episode], key)

    def test_a_local_assignment_closes_after_its_owner_ends_without_a_report(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, placement_request, local_candidate

        for case in ("owner ended", "owner live"):
            with self.subTest(case=case), PlacementFixture() as fixture:
                request = placement_request(fixture)
                request = replace(request, constraints=replace(request.constraints,
                    candidates=(local_candidate(fixture),)))
                granted, = fixture.connect(request).replies()
                assert isinstance(granted, wire.LocalPlacementGranted)
                key = execution_identity.attempt_key(granted.assignment.episode, granted.assignment.attempt)
                fixture.release(granted.grant)
                if case == "owner ended":
                    fixture.proc.kill(wire.Owner(request.owner.pid, request.owner.start_time))
                _ = fixture.run_pass()
                if case == "owner ended":
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.ClosedAssignment)
                    self.assertNotIn(request.episode, fixture.server.current_assignments)
                else:
                    self.assertEqual(fixture.server.assignments[key].state, placement.CurrentAssignment())
                    self.assertEqual(fixture.server.current_assignments[request.episode], key)

    def test_status_and_replay_keep_pending_terminal_reports_current(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
        )
        from scripts.mac_test import claim_state, offload

        for outcome in (placement.AttemptCompleted(0), placement.AttemptCancelled("cancelled"),
                placement.PrelaunchRefused("busy"), placement.AttemptDeclined("lost")):
            with self.subTest(outcome=outcome), PlacementFixture() as fixture:
                request = placement_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                self.assertEqual(fixture.server.handle_request(consume_permission("consume-pending", assignment, claim)),
                    [wire.FirstAuthorization(assignment, claim)])
                holder = fixture.proc.spawn(used=0)
                key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                saved = fixture.server.assignments[key]
                report = placement.TerminalAttemptReport(outcome,
                    placement.DispatchPathPending("adapter cleanup remains unresolved"), fixture.clock.now())
                fixture.server.assignments[key] = replace(saved, terminal_report=report,
                    holder=placement.ClaimHolderIdentity(fixture.server.host, holder.pid, holder.start_time))
                fixture.proc.kill(holder)
                identity = claim_state.ClaimIdentity(claim.claim_id, claim.episode, claim.attempt)
                self.assertFalse(offload.pending_start_path(assignment.destination.target, identity).exists())
                fixture.clock.advance(1)
                _ = publish_candidate(fixture, request, claims=())
                status = self.read_only_status(fixture, assignment)
                assert isinstance(status, status_wire.KnownPlacementStatus)
                self.assertEqual(status.state, placement.CurrentAssignment())
                replay, = fixture.server.handle_request(request)
                self.assertIsInstance(replay, wire.PlacementWait)
                self.assertEqual(fixture.server.assignments[key].state, placement.CurrentAssignment())
                self.assertEqual(fixture.server.assignments[key].terminal_report, report)

    def test_hana_claim_failure_uses_the_exact_dispatch_journal(self) -> None:
        from scripts.aws_target import hana_box, hana_execution, hana_placement, hana_placement_adapter, hana_result
        from scripts.aws_target.test_hana_execution import FakeExecutionObjects
        from scripts.build_queue.test_placement import (
            PlacementFixture, assignment_for, coordinator_holder, hana_policy_request, local_candidate,
            publish_candidate,
        )
        from scripts.mac_test import claim_state

        cases = (("settled command path", "The Hana attempt already has a settled command path"),
                 ("failed start", "The Hana start deadline expired"),
                 ("fenced start refusal", "The Hana start deadline expired"),
                 ("cancelled fenced start", "The Hana start deadline expired"),
                 ("command published", ""),
                 ("restored startup refusal", "The Hana start deadline expired"),
                 ("restored claim failure", "The Hana attempt already has a settled command path"))
        for case, reason in cases:
            with self.subTest(case=case), PlacementFixture() as fixture:
                target_state = fixture.root / "targets"
                with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(target_state)}):
                    request, transport = hana_policy_request(fixture)
                    local = local_candidate(fixture)
                    request = replace(request, constraints=replace(request.constraints,
                        candidates=(*request.constraints.candidates, local)))
                    _ = publish_candidate(fixture, request)
                    assignment = assignment_for(fixture, request)
                    destination = assignment.destination
                    self.assertEqual(destination, request.constraints.candidates[0].destination)
                    key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                    claim = placement.ProviderClaimReference("ssh", destination.target, destination.scope,
                        execution_identity.assigned_claim_id(assignment.episode, assignment.attempt),
                        assignment.episode, assignment.attempt)
                    controller = hana_placement.controller_for(transport, read_only=True)
                    controller.bind_execution(claim.claim_id)
                    if case == "command published":
                        generation = hana_placement.HanaHostGeneration("deployment", "start", "boot")
                        identity = hana_result.HanaAttemptKey(claim.episode, claim.attempt, claim.claim_id, generation)
                        manifest = hana_protocol.SourceManifest(1, "hana-source", "a" * 40, "b" * 64,
                            "https://bucket.s3.us-east-2.amazonaws.com/source", (), ("cargo", "check"), (), (), "c" * 64,
                            runtime_abi=hana_protocol.RUNTIME_ABI)
                        source_key = assignment.source_key
                        assert isinstance(source_key, execution_identity.SourceKeyKnown)
                        dispatch = hana_protocol.HanaExecutionRequest(identity, manifest, 1, source_key,
                            manifest.argv, hana_result.LogicalStepLink("call", claim.episode))
                        holder = coordinator_holder(assignment)
                        adopted_claim = placement.AdoptedProviderClaim(claim, holder, assignment.session,
                            destination.host, "host-generation-a")
                        _ = publish_candidate(fixture, request, claims=(adopted_claim,))
                        adopted = hana_placement.HanaClaimAdopted(placement.ReservedHost(assignment, adopted_claim), generation)
                        objects = FakeExecutionObjects(dispatch)

                        def consume(message: wire.ConsumePermission) -> wire.FirstAuthorization:
                            with mock.patch.object(hana_placement_adapter, "read_assigned_adoption", return_value=adopted_claim):
                                authorized, = fixture.server.handle_request(message)
                            assert isinstance(authorized, wire.FirstAuthorization), authorized
                            return authorized

                        state = hana_execution.dispatch_execution(controller.directory, dispatch, adopted,
                            objects, upload_source=lambda: None, consume=consume, current_permission=lambda: hana_box.CommandPermission(
                                claim.claim_id, hana_box.VerifiedBoot("boot", "start"), 1, int(fixture.clock.now()) + 60),
                            current_source_key=lambda: assignment.source_key)
                        self.assertIsInstance(state, hana_execution.DispatchSent)
                        self.assertEqual(len(objects.envelopes), 1)
                        self.assertIsInstance(objects.envelopes[0], hana_protocol.HanaCommandEnvelope)
                        self.assertIsInstance(hana_execution.observe_execution(controller.directory, dispatch, objects),
                            execution_identity.ObservedExecution)
                        self.assertTrue(hana_execution.claim_dispatch_pending(controller.directory, claim))
                        refusal, = fixture.server.handle_request(wire.ClaimFailure("refusal", assignment, "state"))
                        assert isinstance(refusal, wire.DispatchStatus)
                        self.assertEqual(refusal.state, placement.CurrentAssignment())
                        self.assertIsInstance(refusal.permission, placement.PermissionConsumed)
                        waiting, = fixture.server.handle_request(request)
                        self.assertIsInstance(waiting, wire.PlacementWait)
                        self.assertEqual(fixture.server.current_assignments[assignment.episode], key)
                        # Only recovery of the current attempt settles a started Hana command, so a request
                        # after the claim holder ends keeps the attempt current.
                        runner = fixture.proc.spawn()
                        fixture.server.assignments[key] = replace(fixture.server.assignments[key],
                            holder=placement.ClaimHolderIdentity(fixture.server.host, runner.pid, runner.start_time))
                        fixture.proc.kill(runner)
                        waiting, = fixture.server.handle_request(request)
                        self.assertIsInstance(waiting, wire.PlacementWait)
                        self.assertEqual(fixture.server.assignments[key].state, placement.CurrentAssignment())
                        self.assertEqual(fixture.server.current_assignments[assignment.episode], key)
                        continue
                    if case in ("fenced start refusal", "cancelled fenced start"):
                        def release() -> hana_placement.HanaClaimRelease:
                            return hana_placement.release_assigned(assignment, coordinator_holder(assignment), controller)

                        fenced = hana_execution.settle_unpublished_claim(controller.directory, claim,
                            release_claim=release, reason=reason)
                        self.assertIsInstance(fenced, hana_execution.HanaSettlementPending)
                        self.assertTrue(hana_execution.execution_command_settled(controller.directory, claim.claim_id))
                        self.assertTrue(hana_execution.claim_dispatch_pending(controller.directory, claim))
                        self.assertEqual(claim_state.read_claims(claim_state.state_paths(destination.target)), ())
                        refusal, = fixture.server.handle_request(wire.ClaimFailure("refusal", assignment, "state"))
                        assert isinstance(refusal, wire.DispatchStatus)
                        self.assertEqual(refusal.state, placement.CurrentAssignment())
                        if case == "cancelled fenced start":
                            cancel = wire.CancelPlacement("cancel", request.episode, request.owner, "stopped")
                            self.assertEqual(fixture.server.handle_request(cancel), [wire.Ok()])
                        controller.register(hana_box.DeploymentIdentity("deployment", "instance", "spot", "us-east-2"))
                        retry, = fixture.server.handle_request(request)
                        self.assertEqual(retry, wire.Assignment(assignment))
                        settlement = hana_execution.settle_unpublished_claim(controller.directory, claim,
                            release_claim=release, reason=reason)
                    else:
                        settlement = hana_execution.settle_unpublished_claim(controller.directory, claim,
                            release_claim=hana_placement.HanaClaimReleased, reason=reason)
                    self.assertIsInstance(settlement, hana_execution.HanaSettled)
                    self.assertFalse(hana_execution.claim_dispatch_pending(controller.directory, claim))
                    if case == "cancelled fenced start":
                        # Read the journal when a poll arrives in the same turn as the pass timer.
                        fixture.clock.advance(1)
                        closed, = fixture.connect(request).replies()
                        self.assertIsInstance(closed, wire.DispatchStatus)
                        self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("cancelled"))
                        continue
                    if case == "fenced start refusal":
                        _ = fixture.run_pass()
                        self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("state"))
                    elif case.startswith("restored"):
                        startup = case == "restored startup refusal"
                        refused_reason = reason if startup else "state"
                        report = placement.TerminalAttemptReport(placement.PrelaunchRefused(refused_reason),
                            placement.DispatchPathPending("The Hana claim remains retained" if startup
                                else "provider settlement remains pending"), fixture.clock.now())
                        fixture.server.assignments[key] = replace(fixture.server.assignments[key], terminal_report=report,
                            provider=claim if startup else placement.NoProviderClaim(), retry=placement.RetryAt(report.received_at))
                        fixture.server.persist()
                        fixture.restart()
                        local_key = fixture.server.current_assignments[assignment.episode]
                        self.assertEqual(fixture.server.assignments[local_key].assignment.destination, local.destination)
                        self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment(refused_reason))
                    else:
                        # The adapter reports the reason code and prints the detailed provider reason.
                        refusal, = fixture.server.handle_request(wire.ClaimFailure("refusal", assignment, "state"))
                        assert isinstance(refusal, wire.DispatchStatus)
                        self.assertEqual(refusal.state, placement.ClosedAssignment("state"))
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.LocalPlacementGranted)
                    assert isinstance(reply, wire.LocalPlacementGranted)
                    self.assertEqual(reply.assignment.destination, local.destination)
                    self.assertEqual((reply.assignment.episode, reply.assignment.since), (assignment.episode, assignment.since))
                    self.assertNotEqual(reply.assignment.attempt, assignment.attempt)

    def test_a_settled_decline_waits_for_evidence_at_or_after_its_report(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
        )

        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assignment)
            self.assertEqual(fixture.server.handle_request(consume_permission("consume-declined", assignment, claim)),
                [wire.FirstAuthorization(assignment, claim)])
            fixture.clock.advance(1)
            _ = publish_candidate(fixture, request, claims=())
            fixture.clock.advance(1)
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            report = placement.TerminalAttemptReport(placement.AttemptDeclined("lost"),
                placement.DispatchPathSettled(), fixture.clock.now())
            fixture.server.assignments[key] = replace(fixture.server.assignments[key], terminal_report=report)
            status = self.read_only_status(fixture, assignment)
            assert isinstance(status, status_wire.KnownPlacementStatus)
            self.assertEqual(status.state, placement.CurrentAssignment())
            replay, = fixture.server.handle_request(request)
            self.assertIsInstance(replay, wire.PlacementWait)
            _ = publish_candidate(fixture, request, claims=())
            status = self.read_only_status(fixture, assignment)
            assert isinstance(status, status_wire.KnownPlacementStatus)
            self.assertEqual(status.state, placement.ClosedAssignment("provider claim released"))
            _ = fixture.run_pass()
            self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment("lost"))

    def test_expired_empty_evidence_keeps_status_current_until_the_owner_asks_again(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
        )

        with PlacementFixture() as fixture:
            entered, release = threading.Event(), threading.Event()

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the probe")
                return placement.HostEvidenceUnavailable(key, fixture.clock.now(), "private blocked probe")

            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assignment)
            self.assertEqual(fixture.server.handle_request(consume_permission("consume-expiry", assignment, claim)),
                [wire.FirstAuthorization(assignment, claim)])
            holder = fixture.proc.spawn(used=0)
            attempt = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            fixture.server.assignments[attempt] = replace(fixture.server.assignments[attempt],
                holder=placement.ClaimHolderIdentity(fixture.server.host, holder.pid, holder.start_time))
            fixture.clock.advance(1)
            evidence = publish_candidate(fixture, request, claims=())
            status = self.read_only_status(fixture, assignment)
            assert isinstance(status, status_wire.KnownPlacementStatus)
            self.assertEqual(status.state, placement.CurrentAssignment())
            config = fixture.server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            fixture.clock.advance(config.waiting_pass_seconds + conf.read("mac_test").number("probe_timeout_s") + 1)
            self.assertIsInstance(fixture.server.host_evidence(evidence.key), placement.StaleHostEvidence)
            fixture.proc.kill(holder)
            fixture.server.host_probe = probe
            try:
                status = self.read_only_status(fixture, assignment)
                assert isinstance(status, status_wire.KnownPlacementStatus)
                self.assertEqual(status.state, placement.CurrentAssignment())
                _ = fixture.run_pass()
                self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                status = self.read_only_status(fixture, assignment)
                assert isinstance(status, status_wire.KnownPlacementStatus)
                self.assertEqual(status.state, placement.CurrentAssignment())
                self.assertFalse(release.is_set())
                self.assertEqual(fixture.server.assignments[attempt].state, placement.CurrentAssignment())
            finally:
                release.set()
            # The owner asks again only after its attempt ends, so its request closes the attempt as lost.
            replay, = fixture.server.handle_request(request)
            self.assertIsInstance(replay, wire.PlacementWait)
            self.assertEqual(fixture.server.assignments[attempt].state, placement.ClosedAssignment("lost"))

    def test_configuration_change_keeps_status_current_until_the_owner_asks_again(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
        )

        with PlacementFixture() as fixture:
            entered, release = threading.Event(), threading.Event()

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the changed configuration probe")
                return placement.HostEvidenceUnavailable(key, fixture.clock.now(), "private blocked probe")

            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            claim = adopt_assignment(fixture, request, assignment)
            self.assertEqual(fixture.server.handle_request(consume_permission("consume-configuration", assignment, claim)),
                [wire.FirstAuthorization(assignment, claim)])
            holder = fixture.proc.spawn(used=0)
            attempt = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            fixture.server.assignments[attempt] = replace(fixture.server.assignments[attempt],
                holder=placement.ClaimHolderIdentity(fixture.server.host, holder.pid, holder.start_time))
            fixture.clock.advance(1)
            old_evidence = publish_candidate(fixture, request, claims=())
            settings = conf.path("mac_test")
            _ = settings.write_text(settings.read_text() + "\n# private configuration generation\n")
            current_key = fixture.server.host_evidence_key(request, assignment.destination)
            self.assertNotEqual(current_key, old_evidence.key)
            self.assertIsInstance(fixture.server.host_evidence(old_evidence.key), placement.FreshHostEvidence)
            self.assertIsInstance(fixture.server.host_evidence(current_key), placement.HostEvidenceUnavailable)
            fixture.proc.kill(holder)
            status = self.read_only_status(fixture, assignment)
            assert isinstance(status, status_wire.KnownPlacementStatus)
            self.assertEqual(status.state, placement.CurrentAssignment())
            self.assertEqual(fixture.server.placement_host_keys[(request.episode, assignment.destination)], old_evidence.key)
            with mock.patch.object(fixture.server, "_request_host_evidence", return_value=old_evidence):
                _ = fixture.run_pass()
                self.assertEqual(fixture.server.assignments[attempt].state, placement.CurrentAssignment())
            fixture.server.host_probe = probe
            try:
                _ = fixture.run_pass()
                self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                status = self.read_only_status(fixture, assignment)
                assert isinstance(status, status_wire.KnownPlacementStatus)
                self.assertEqual(status.state, placement.CurrentAssignment())
                self.assertFalse(release.is_set())
                self.assertEqual(fixture.server.assignments[attempt].state, placement.CurrentAssignment())
            finally:
                release.set()
            # The owner asks again only after its attempt ends, so its request closes the attempt as lost.
            replay, = fixture.server.handle_request(request)
            self.assertIsInstance(replay, wire.PlacementWait)
            self.assertEqual(fixture.server.assignments[attempt].state, placement.ClosedAssignment("lost"))

    def test_released_local_placement_requests_no_evidence_and_keeps_no_subscription(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, placement_request, local_candidate

        with PlacementFixture() as fixture:
            entered, release = threading.Event(), threading.Event()
            fixture.server.background_local_evidence = True

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the local probe")
                return placement.HostEvidenceUnavailable(key, fixture.clock.now(), "private blocked local probe")

            fixture.server.host_probe = probe
            request = placement_request(fixture)
            request = replace(request, constraints=replace(request.constraints, candidates=(local_candidate(fixture),)))
            try:
                waiting, = fixture.server.handle_request(request)
                self.assertIsInstance(waiting, wire.PlacementWait)
                self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                key, = fixture.server.host_readers
                worker = fixture.server.host_readers[key].worker
                with worker.lock:
                    self.assertIn(key, worker.subscriptions)
                fixture.server.publish_host_evidence(placement.FreshHostEvidence(
                    key, fixture.clock.now(), "", placement.AllowedHost()))
                granted, = fixture.server.handle_request(request)
                assert isinstance(granted, wire.LocalPlacementGranted)
                with mock.patch.object(fixture.server, "_request_host_evidence",
                        side_effect=AssertionError("a released local placement must not request evidence")) as request_evidence:
                    self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                        "release-local-subscription", granted.grant.reservation, 0)), [wire.Ok()])
                    _ = fixture.run_pass()
                    request_evidence.assert_not_called()
                self.assertNotIn(key, fixture.server.host_readers)
                self.assertNotIn(key, fixture.server.host_requests)
                self.assertNotIn(key, fixture.server.host_observations)
                self.assertNotIn(key, fixture.server.placement_host_keys.values())
                with worker.lock:
                    self.assertNotIn(key, worker.subscriptions)
                    self.assertNotIn(key, worker.due)
            finally:
                release.set()

    def test_saved_attempt_status_is_read_only_for_current_closed_and_unknown(self) -> None:
        from scripts.build_queue import admission_wire as status_wire, placement
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            request = placement_request(fixture)
            destination = request.constraints.candidates[0].destination
            assignment = placement.PlacementAssignment(request.episode, "attempt-a", destination,
                request.constraints.source_key, request.owner, request.constraints.session, request.since)
            claim = placement.ProviderClaimReference(destination.provider, destination.target, destination.scope,
                "claim-a", assignment.episode, assignment.attempt)
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            for state in (placement.CurrentAssignment(), placement.ClosedAssignment("source changed")):
                for provider in (placement.NoProviderClaim(), claim):
                    with self.subTest(state=state, provider=provider):
                        saved = records.SavedPlacementAssignment(assignment, state,
                            placement.PermissionConsumed(fixture.clock.now()), provider)
                        fixture.server.assignments[key] = saved
                        fixture.server.current_assignments[assignment.episode] = key
                        fixture.server.persist()
                        fields = tuple(service.ServiceState.__dataclass_fields__)
                        before = wire.encode_record({name: getattr(fixture.server, name) for name in fields})
                        files = {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                            for path in fixture.state_dir.rglob("*") if path.is_file()}
                        peer, other = socket.socketpair()
                        with peer, other, mock.patch.object(fixture.server, "persist") as persist, \
                                mock.patch.object(fixture.server, "_record_caller_version") as record_version:
                            connection = service.Connection(peer)
                            for episode, attempt in ((assignment.episode, assignment.attempt),
                                    (assignment.episode, "unknown-attempt"), ("unknown-episode", assignment.attempt)):
                                reply, = fixture.server.handle_request(status_wire.PlacementStatusRequest(
                                    episode, attempt, code_version="new-caller"), connection)
                                expected = (status_wire.KnownPlacementStatus(assignment, state, provider)
                                    if (episode, attempt) == (assignment.episode, assignment.attempt) else
                                    status_wire.UnknownPlacementStatus(episode, attempt))
                                self.assertEqual(reply, status_wire.PlacementStatusReply(expected))
                            persist.assert_not_called()
                            record_version.assert_not_called()
                        self.assertEqual(wire.encode_record({name: getattr(fixture.server, name) for name in fields}), before)
                        self.assertEqual({str(path): (path.read_bytes(), path.stat().st_mtime_ns)
                            for path in fixture.state_dir.rglob("*") if path.is_file()}, files)


class EndedPlacementPruneTests(unittest.TestCase):
    def test_a_dead_owner_close_prunes_other_episodes_without_stopping_the_pass(self) -> None:
        from scripts.build_queue.test_placement import placement_request

        for close_is_old in (False, True):
            with self.subTest(close_is_old=close_is_old), AdmissionFixture() as fixture:
                server = fixture.server
                config = records.read_memory_config()
                old = fixture.clock.now() - config.ended_placement_seconds - 1
                closing = replace(placement_request(fixture, since=old if close_is_old else None), episode="closing")
                ended = replace(placement_request(fixture, since=old), episode="ended")
                for request in (closing, ended):
                    server.placement_requests[request.episode] = request
                    assignment = placement.PlacementAssignment(request.episode, "attempt",
                        request.constraints.candidates[0].destination, request.constraints.source_key,
                        request.owner, request.constraints.session, request.since)
                    key = execution_identity.attempt_key(request.episode, assignment.attempt)
                    state = placement.ClosedAssignment("busy") if request is ended else placement.CurrentAssignment()
                    server.assignments[key] = records.SavedPlacementAssignment(assignment, state)
                    if request is closing:
                        server.current_assignments[request.episode] = key
                    fixture.proc.kill(wire.Owner(request.owner.pid, request.owner.start_time))

                self.assertIsInstance(fixture.run_pass(), gate.PassResult)
                self.assertNotIn(ended.episode, server.placement_requests)
                self.assertNotIn(closing.episode, server.current_assignments)
                if close_is_old:
                    self.assertNotIn(closing.episode, server.placement_requests)
                else:
                    saved = next(saved for saved in server.assignments.values()
                                 if saved.assignment.episode == closing.episode)
                    self.assertEqual(saved.state, placement.ClosedAssignment("cancelled"))
                state = service.read_state(fixture.state_dir)
                self.assertNotIn(ended.episode, state.placement_requests)
                self.assertFalse(any(saved.assignment.episode == ended.episode for saved in state.assignments.values()))
                self.assertEqual(fixture.connect(wire.BuildHistoryRequest()).replies(),
                                 [wire.BuildHistoryReply(server.build_history_generation)])
                self.assertFalse(server.stopped)

    def test_configuration_refusal_skips_waiters_removed_by_a_shared_connection(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.hold_starts()
            first, second = fixture.request(), fixture.request()
            for request in (first, second):
                self.assertIsInstance(fixture.connect(request).replies()[0], wire.Wait)
            connection = fixture.server.waiters[first.episode].connection
            assert isinstance(connection, service.Connection)
            fixture.server.waiters[second.episode].connection = connection
            refused = gate.UnusableAdmissionConfiguration("fixture configuration failure")
            with mock.patch.object(fixture.server, "_take_configuration", return_value=refused):
                self.assertEqual(fixture.server.run_pass(), refused)
            self.assertFalse(fixture.server.waiters)
            self.assertEqual(connection.sending.count(b'"type":"refused"'), 1)

    def test_persist_removes_an_ended_episode_after_its_retention_and_keeps_the_rest(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            config = records.read_memory_config()
            fixture.server.last_usable_configuration = config
            server = fixture.server
            old = fixture.clock.now() - config.ended_placement_seconds - 1
            requests = {name: replace(placement_request(fixture, since=old), episode=name)
                        for name in ("ended", "live-owner", "current", "recent")}
            requests["recent"] = replace(requests["recent"], since=fixture.clock.now())
            requests["live-owner"] = replace(requests["live-owner"],
                                             owner=replace(requests["live-owner"].owner, start_time="live-start"))
            for name, request in requests.items():
                destination = request.constraints.candidates[0].destination
                assignment = placement.PlacementAssignment(name, "attempt", destination, request.constraints.source_key,
                    request.owner, request.constraints.session, request.since)
                key = execution_identity.attempt_key(name, "attempt")
                state = placement.CurrentAssignment() if name == "current" else placement.ClosedAssignment("busy")
                server.placement_requests[name] = request
                server.assignments[key] = records.SavedPlacementAssignment(assignment, state)
                if name == "current":
                    server.current_assignments[name] = key
            def owner_live(owner: placement.SubmittingOwner) -> bool:
                return owner.start_time == "live-start"

            with mock.patch.object(server, "_placement_owner_live", side_effect=owner_live):
                server.persist()
            kept = {"live-owner", "current", "recent"}
            self.assertEqual(set(server.placement_requests), kept)
            self.assertEqual({saved.assignment.episode for saved in server.assignments.values()}, kept)
            self.assertEqual(set(service.read_state(fixture.state_dir).placement_requests), kept)


class ProcessTextTests(unittest.TestCase):
    def test_owner_start_uses_the_private_proc_root(self) -> None:
        with AdmissionFixture() as fixture:
            owner = fixture.request().owner
            stat = fixture.proc.path / str(owner.pid) / "stat"
            tail = b"0 " * 18 + owner.start_time.encode() + b"\n"
            _ = stat.write_bytes(f"{owner.pid} (".encode() + b"step\xff) S " + tail)
            proc = service.SystemProc()
            self.assertEqual(proc.owner_start(owner.pid), service.LiveOwnerIdentity(owner.start_time))
            _ = stat.write_bytes(b"incomplete stat")
            self.assertIsInstance(proc.owner_start(owner.pid), service.OwnerProcessUnreadable)
            stat.unlink()
            self.assertEqual(proc.owner_start(owner.pid), service.OwnerProcessGone())

    def test_process_names_with_invalid_utf8_preserve_memory_readings(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            _ = only_admitted(fixture.server.handle_request(request))
            record = fixture.server.reservations[request.episode]
            row = service.reservation_row(record)
            proc = fixture.proc.path
            pid = request.owner.pid
            directory = proc / str(pid)
            write_process(proc, pid, 8 * 1024, start=request.owner.start_time, anonymous=5 * 1024, swap=3 * 1024)
            status = directory / "status"
            memory_fields = status.read_bytes()
            for field in ("status", "comm"):
                with self.subTest(field=field):
                    name = b"verify\xff" if field == "status" else b"rustc\xff"
                    _ = status.write_bytes(b"Name:\t" + (name if field == "status" else b"verify")
                                           + b"\nUid:\t1234\n" + memory_fields)
                    _ = (directory / "comm").write_bytes((name if field == "comm" else b"verify") + b"\n")
                    reading = service.raw_reservation_memory(record, row, gate.LiveOwnerProcess(),
                                                            gate.process_table(proc), proc)
                    assert isinstance(reading, records.MeasuredMemory)
                    self.assertEqual(reading.bytes, 8 * 1024)
                    self.assertEqual(reading.anonymous, records.AnonymousRunUse(5 * 1024))
                    self.assertEqual(reading.swap_bytes, 3 * 1024)
                    self.assertEqual(service.process_uid(pid, proc), 1234)
                    self.assertEqual(gate.member_programs([pid], proc),
                                     {pid: "rustc\ufffd" if field == "comm" else "verify"})


class ImportTests(unittest.TestCase):
    def test_clean_import_omits_cli_and_reading_alert_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {**os.environ, "HOME": directory, "BUILDLOG_DIR": directory}
            result = subprocess.run(
                [sys.executable, "-c", "import sys; from scripts.build_queue import admission_service; "
                 + "assert not {'argparse', 'scripts.message.send_budget', 'scripts.message.send_result', "
                 + "'scripts.message.session_send'}.intersection(sys.modules)"],
                cwd=Path(__file__).resolve().parents[2], env=environment,
                capture_output=True, text=True, timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class StateCheckTests(unittest.TestCase):
    def test_stored_reservation_without_trace_sampling_uses_the_scope(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            _ = only_admitted(fixture.server.handle_request(request))
            state_path = fixture.state_dir / "state.json"
            stored = cast(dict[str, object], json.loads(state_path.read_text()))
            reservations = cast(dict[str, dict[str, object]], stored["reservations"])
            del reservations[request.episode]["trace_sampling"]
            write_text_atomic(state_path, json.dumps(stored))
            record = service.read_state(fixture.state_dir).reservations[request.episode]
            self.assertEqual(record.trace_sampling, wire.ScopeTraceSampling())
            self.assertEqual(record, fixture.server.reservations[request.episode])

    def test_state_check_reads_current_state_and_refuses_an_old_placement_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = io.StringIO()
            with redirect_stderr(output), mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(service.state_check(root), 0)
                state = service.ServiceState({}, {}, {}, records.ReadyToStart())
                write_text_atomic(root / "state.json", wire.encode_record(state))
                self.assertEqual(service.state_check(root), 0)
                stored = cast(dict[str, object], json.loads((root / "state.json").read_text()))
                stored["placement_requests"] = {"episode": {"kind": "PlacementRequest", "episode": "episode"}}
                write_text_atomic(root / "state.json", json.dumps(stored))
                before = (root / "state.json").read_bytes()
                self.assertEqual(service.state_check(root), 2)
            self.assertIn("stored placement records require external conversion", output.getvalue())
            self.assertEqual((root / "state.json").read_bytes(), before)


    def test_verify_metadata_in_stored_placement_is_optional_in_both_versions(self) -> None:
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            request = placement_request(fixture)
            old = service.ServiceState({}, {}, {}, records.ReadyToStart(),
                placement_requests={request.episode: request})
            state = fixture.state_dir / "state.json"
            write_text_atomic(state, wire.encode_record(old))
            self.assertEqual(service.read_state(fixture.state_dir), old)
            facts = records.VerifyCall("test", "verify test sample --no-cache", 1, 1)
            request = replace(request, identity=replace(request.identity, verify=facts))
            current = replace(old, placement_requests={request.episode: request})
            write_text_atomic(state, wire.encode_record(current))
            self.assertEqual(service.read_state(fixture.state_dir), current)

            current_fields = codec.snapshot_fields

            def fields_before_verify(schema: type) -> dict[str, object]:
                fields = current_fields(schema)
                if schema is wire.Identity:
                    return {name: kind for name, kind in fields.items() if name != "verify"}
                return fields

            with mock.patch.object(codec, "snapshot_fields", side_effect=fields_before_verify):
                self.assertEqual(service.read_state(fixture.state_dir), old)


class MacProbe:
    """Answer the Mac probe command in the test and count the calls."""

    def __init__(self) -> None:
        self.answer: Callable[[Sequence[str]], offload.CapturedCommandResult] | None = None
        self.call_count: int = 0

    def reply(self, output: offload.CapturedCommandResult) -> None:
        self.answer = lambda _arguments: output

    def capture(self, arguments: Sequence[str], _deadline: object = None) -> offload.CapturedCommandResult:
        self.call_count += 1
        assert self.answer is not None, "the Mac probe has no answer"
        return self.answer(arguments)


@dataclass(frozen=True)
class MacBackoffTestFixture:
    admission: AdmissionFixture
    backoff: Path
    key: placement.HostEvidenceKey
    probe: MacProbe

    def eligibility(self) -> placement.HostEligibility:
        evidence = self.admission.server.read_provider_evidence(self.key)
        assert isinstance(evidence, placement.FreshHostEvidence)
        return evidence.eligibility


def mac_cycle_probe_ok(_fixture: MacBackoffTestFixture) -> Callable[[Sequence[str]], offload.CommandOutput]:
    from scripts.mac_test import offload

    return lambda _arguments: offload.CommandOutput(
        0, "free_gib=60\nhost=fixture-mac\nrc=0\nmirror_setup=0\tready\n", "")


@dataclass(frozen=True)
class MacDiskSweepFixture:
    admission: AdmissionFixture
    key: placement.HostEvidenceKey
    started: threading.Event
    release: threading.Event
    inline: threading.Event
    disk_ready: threading.Event
    readings: Queue[placement.HostEvidence]

    def subscribe(self, key: placement.HostEvidenceKey) -> service.HostEvidenceReader:
        reader = cast(Callable[[placement.HostEvidenceKey, float], service.HostEvidenceReader],
                      getattr(self.admission.server, "_host_evidence_reader"))
        return reader(key, test_hang_guard.TEST_HANG_GUARD_SECS)

    def other_key(self) -> placement.HostEvidenceKey:
        server = self.admission.server
        other = replace(self.key, package="another-package")
        request = server.host_requests[self.key]
        server.host_requests[other] = replace(request, constraints=replace(request.constraints, package=other.package))
        return other


class LocalHoldPlacementTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        # The service applies natedev holds only on natedev; the guest has another host name.
        _ = self.enterContext(mock.patch.object(store, "host_name", return_value="natedev"))

    def test_close_between_the_loop_check_and_clear_does_not_lose_its_wake(self) -> None:
        from queue import Queue
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            entered, release, clearing, allow_clear = (threading.Event() for _ in range(4))
            outcomes: Queue[str] = Queue()

            class ObservedWorker(service.HostEvidenceWorker):
                @override
                def _read(self) -> None:
                    try:
                        super()._read()
                    finally:
                        outcomes.put("ended")

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the probe")
                return placement.HostEvidenceUnavailable(key, fixture.clock.now(), "private probe")

            request = placement_request(fixture)
            key = fixture.server.host_evidence_key(request, request.constraints.candidates[0].destination)
            worker = ObservedWorker(key, probe, fixture.clock, fixture.server.reading_events, 60)
            try:
                self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                original_clear = worker.requested.clear
                original_wait = worker.requested.wait

                def clear() -> None:
                    clearing.set()
                    if not allow_clear.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                        raise AssertionError("the test did not permit the event clear")
                    original_clear()

                def wait(timeout: float | None = None) -> bool:
                    if worker.stopped.is_set():
                        outcomes.put("waited after close")
                        return True
                    return original_wait(timeout)

                with mock.patch.object(worker.requested, "clear", side_effect=clear), \
                     mock.patch.object(worker.requested, "wait", side_effect=wait):
                    worker.remove(key)
                    release.set()
                    self.assertTrue(clearing.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                    worker.close()
                    allow_clear.set()
                    self.assertEqual(outcomes.get(timeout=test_hang_guard.TEST_HANG_GUARD_SECS), "ended")
                    worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                    self.assertFalse(worker.thread.is_alive())
            finally:
                worker.close()
                release.set()
                allow_clear.set()
                worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)

    def test_an_empty_probe_schedule_reuses_its_live_worker_for_a_new_request(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            entered, release, empty_wait, published = (threading.Event() for _ in range(4))
            delays: list[float | None] = []

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the probe")
                return placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())

            fixture.server.host_probe = probe
            request = placement_request(fixture)
            try:
                _ = fixture.server.handle_request(request)
                self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                key, = fixture.server.host_readers
                worker = fixture.server.host_readers[key].worker
                original_wait = worker.requested.wait
                original_write = os.eventfd_write

                def wait(timeout: float | None = None) -> bool:
                    delays.append(timeout)
                    empty_wait.set()
                    return original_wait(timeout)

                def notify(fd: int, value: int) -> None:
                    original_write(fd, value)
                    if threading.current_thread() is worker.thread:
                        published.set()

                with mock.patch.object(worker.requested, "wait", side_effect=wait), \
                     mock.patch.object(os, "eventfd_write", side_effect=notify):
                    _ = fixture.server.handle_request(wire.CancelPlacement(
                        "cancel", request.episode, request.owner, "ended"))
                    with worker.lock:
                        self.assertEqual(worker.due, {})
                    release.set()
                    self.assertTrue(empty_wait.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                    self.assertIsNone(delays[0])
                    self.assertTrue(worker.thread.is_alive())
                    next_request = replace(request, episode="later", constraints=replace(
                        request.constraints, command=("cargo", "check")))
                    _ = fixture.server.handle_request(next_request)
                    next_key = fixture.server.host_evidence_key(next_request, key.destination)
                    self.assertIs(fixture.server.host_readers[next_key].worker, worker)
                    self.assertTrue(published.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                    self.assertIsInstance(fixture.server.host_evidence(next_key), placement.FreshHostEvidence)
                    self.assertTrue(worker.thread.is_alive())
            finally:
                for worker in fixture.server.host_targets.values():
                    worker.close()
                release.set()
                for worker in fixture.server.host_targets.values():
                    worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)

    def test_ended_requests_remove_their_probe_schedule_keys(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for,
            adopt_assignment, consume_permission,
        )

        for ending in ("cancelled", "completed"):
            with self.subTest(ending=ending), PlacementFixture() as fixture:
                entered, release = threading.Event(), threading.Event()

                def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                    entered.set()
                    if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                        raise AssertionError("the test did not release the probe")
                    return placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())

                fixture.server.host_probe = probe
                request = placement_request(fixture)
                try:
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.PlacementWait)
                    self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                    key, = fixture.server.host_readers
                    worker = fixture.server.host_readers[key].worker
                    if ending == "completed":
                        _ = publish_candidate(fixture, request)
                        assignment = assignment_for(fixture, request)
                        claim = adopt_assignment(fixture, request, assignment)
                        authorized, = fixture.server.handle_request(consume_permission("launch", assignment, claim))
                        self.assertIsInstance(authorized, wire.FirstAuthorization)
                        _ = fixture.server.handle_request(wire.ExecutionReport("done", assignment, claim,
                            execution_identity.ExecutionNotStarted(), placement.AttemptCompleted(0), placement.DispatchPathSettled()))
                    else:
                        _ = fixture.server.handle_request(wire.CancelPlacement("cancel", request.episode, request.owner, "ended"))
                    self.assertNotIn(key, fixture.server.host_readers)
                    self.assertNotIn(key, fixture.server.host_requests)
                    with worker.lock:
                        self.assertNotIn(key, worker.due)
                    worker.close()
                    release.set()
                    worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                    self.assertFalse(worker.thread.is_alive())
                    with worker.lock:
                        self.assertNotIn(key, worker.due)
                        self.assertNotIn(key, worker.observations)
                finally:
                    for worker in fixture.server.host_targets.values():
                        worker.close()
                    release.set()
                    for worker in fixture.server.host_targets.values():
                        worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)

    def test_pass_reads_provider_configuration_once_as_the_queue_grows(self) -> None:
        from scripts.build_queue.test_placement import placement_request
        from scripts.build_targets import target_config

        with AdmissionFixture() as fixture:
            settings = fixture.root / "linux.conf"
            _ = write_config_fixture(settings, "probe_timeout_s=1\n", template="mac_test")
            _ = (fixture.root / "build_targets.conf").write_text(
                "targets=linux\nkind.linux=ssh\nplatform.linux=linux\nsettings.linux=linux.conf\n"
                + UNREACHABLE_WAIT_FIXTURE)
            request = placement_request(fixture)
            candidate, = request.constraints.candidates
            destination = replace(candidate.destination, provider="ssh", target="linux")
            request = replace(request, constraints=replace(request.constraints,
                candidates=(replace(candidate, destination=destination),)))
            fixture.server.placement_requests[request.episode] = request

            with mock.patch.object(io, "open", wraps=io.open) as io_open, \
                    mock.patch("builtins.open", wraps=open) as builtin_open, \
                    mock.patch.object(target_config, "read_execution_transport",
                                      wraps=target_config.read_execution_transport) as read_transport:
                def settings_opens() -> int:
                    return sum(cast(tuple[object, ...], call.args)[0] in (settings, str(settings))
                               for opener in (io_open, builtin_open) for call in opener.call_args_list)

                _ = fixture.run_pass()
                first = fixture.server.placement_host_keys[(request.episode, destination)]
                first_counts = settings_opens(), read_transport.call_count
                self.assertEqual(fixture.server.host_readers[first].timeout, 1)
                for index in range(12):
                    extra = replace(request, episode=f"extra-{index}")
                    fixture.server.placement_requests[extra.episode] = extra
                _ = write_config_fixture(settings, "probe_timeout_s=4\n", template="mac_test")
                io_open.reset_mock()
                builtin_open.reset_mock()
                read_transport.reset_mock()
                _ = fixture.run_pass()
                self.assertEqual((settings_opens(), read_transport.call_count), first_counts)
                self.assertEqual(first_counts, (1, 1))
                current = fixture.server.placement_host_keys[(request.episode, destination)]
                self.assertNotEqual(current.configuration_generation, first.configuration_generation)
                self.assertEqual(fixture.server.host_readers[current].timeout, 4)
                self.assertEqual(len(fixture.server.placement_host_keys), 13)
                self.assertEqual(set(fixture.server.placement_host_keys.values()), {current})

    def test_each_settings_file_controls_its_evidence_age(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture:
            entered, release = threading.Event(), threading.Event()

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the probe")
                return placement.HostEvidenceUnavailable(key, fixture.clock.now(), "private probe")

            fixture.server.host_probe = probe
            keys: list[placement.HostEvidenceKey] = []
            try:
                for name, timeout in (("long", 10), ("short", 1)):
                    path = fixture.root / (name + ".conf")
                    _ = write_config_fixture(path, f"probe_timeout_s={timeout}\n", template="mac_test")
                    request = placement_request(fixture)
                    candidate = request.constraints.candidates[0]
                    request = replace(request, episode=name, constraints=replace(request.constraints,
                        candidates=(replace(candidate, settings=placement.ExplicitProviderSettings(path)),)))
                    _ = fixture.server.handle_request(request)
                    keys.append(fixture.server.host_evidence_key(request, candidate.destination))
                self.assertTrue(entered.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                self.assertIs(fixture.server.host_readers[keys[0]].worker, fixture.server.host_readers[keys[1]].worker)
                for key in keys:
                    fixture.server.publish_host_evidence(placement.FreshHostEvidence(
                        key, fixture.clock.now(), "", placement.AllowedHost()))
                config = fixture.server.last_usable_configuration
                assert isinstance(config, records.MemoryConfig)
                fixture.clock.advance(config.waiting_pass_seconds + 2)
                self.assertIsInstance(fixture.server.host_evidence(keys[0]), placement.FreshHostEvidence)
                self.assertIsInstance(fixture.server.host_evidence(keys[1]), placement.StaleHostEvidence)
            finally:
                for worker in fixture.server.host_targets.values():
                    worker.close()
                release.set()
                for worker in fixture.server.host_targets.values():
                    worker.thread.join(timeout=test_hang_guard.TEST_HANG_GUARD_SECS)

    def test_failed_execution_probes_start_the_provider_backoff(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request
        from scripts.build_targets import build_targets, target_config
        from scripts.mac_test import claim_state, eligibility, mac_test

        for failure in (OSError("ssh missing"), subprocess.TimeoutExpired("ssh", 1),
                        subprocess.CompletedProcess(["ssh"], 255, "", "unreachable")):
            with self.subTest(failure=failure), AdmissionFixture() as fixture:
                settings = fixture.root / "linux.conf"
                _ = write_config_fixture(settings, "probe_timeout_s=1\nunreachable_backoff_s=30\n", template="mac_test")
                target = target_config.SshTarget("linux", "linux", settings)
                request = placement_request(fixture)
                destination = replace(request.constraints.candidates[0].destination,
                                      provider="ssh", target="linux", host="private-linux")
                request = replace(request, constraints=replace(request.constraints, candidates=(
                    placement.PlacementCandidate(destination, settings=placement.ExplicitProviderSettings(settings)),)))
                paths = claim_state.StatePaths(fixture.root / "claims", platform="linux")
                record: claim_state.RemoteJobClaim = {"claim_id": "retained", "episode": "old", "attempt": "old-attempt",
                    "submitting_session": "", "pid": request.owner.pid, "proc_start": request.owner.start_time,
                    "remote_host": destination.host, "what": "busy claim", "worktree": str(fixture.root), "since": "private"}
                held = claim_state.HeldClaim(destination.scope, paths.run, record)
                with mock.patch.object(target_config, "read_targets", return_value=(target,)), \
                     mock.patch.object(build_targets, "ssh_target_eligibility", return_value=target_config.ExcludedTarget(target, "busy")), \
                     mock.patch.object(claim_state, "state_paths", return_value=paths), \
                     mock.patch.object(claim_state, "read_claims", return_value=(held,)), \
                     mock.patch.object(claim_state, "target_claim_scope", return_value=destination.scope), \
                     mock.patch.object(mac_test, "run_is_live", return_value=False), \
                     mock.patch.object(eligibility, "repository_identity", return_value=eligibility.KnownRepository("hana")), \
                     mock.patch.object(subprocess, "run") as probe:
                    if isinstance(failure, BaseException):
                        probe.side_effect = failure
                    else:
                        probe.return_value = failure
                    key = fixture.server.host_evidence_key(request, destination)
                    fixture.server.host_requests[key] = request
                    first = fixture.server.read_provider_evidence(key)
                    second = fixture.server.read_provider_evidence(key)
                    self.assertIsInstance(first, placement.FreshHostEvidence)
                    self.assertIsInstance(second, placement.FreshHostEvidence)
                    assert isinstance(second, placement.FreshHostEvidence)
                    self.assertEqual(len(second.claims), 1)
                    self.assertEqual(second.claims[0].reference.claim_id, "retained")
                    self.assertIsInstance(second.executions[0][1], execution_identity.ExecutionEvidenceUnavailable)
                    self.assertEqual(probe.call_count, 1)
                    self.assertIsInstance(eligibility.read_backoff(time.time(), "linux"), eligibility.ActiveBackoff)

    def test_execution_observer_reads_the_saved_mac_mirror_after_a_checkout_moves(self) -> None:
        from scripts.build_queue.test_placement import placement_request
        from scripts.build_targets import build_targets, target_config
        from scripts.mac_test import claim_state, eligibility, mac_test

        with AdmissionFixture() as fixture:
            settings = fixture.root / "mac_test.conf"
            mirror_root = ".local/state/mac-test/test-mirror"
            _ = write_config_fixture(settings, f"mirror_root=~/{mirror_root}\n", template="mac_test")
            target = target_config.SshTarget("mac", "macos", settings)
            request = placement_request(fixture)
            repo_key = "https://fixture.invalid/team/hana"
            candidate, = request.constraints.candidates
            destination = replace(candidate.destination, scope=claim_state.RepositoryScope("new_checkout", repo_key))
            request = replace(request, constraints=replace(request.constraints,
                candidates=(replace(candidate, destination=destination),)))
            paths = claim_state.StatePaths(fixture.root / "claims")
            paths.claims.mkdir(parents=True)
            saved_mirror = claim_state.CleanupOnlyMacRepository(repo_key, "old_checkout", "saved.json", "saved")
            record: claim_state.RemoteJobClaim = {
                "claim_id": "saved", "episode": request.episode, "attempt": "saved-attempt", "submitting_session": "",
                "pid": request.owner.pid, "proc_start": request.owner.start_time, "remote_host": destination.host,
                "what": "check", "worktree": str(fixture.root / "removed"), "since": "2026-11-17T11:00:00+00:00",
                "repo_name": "old_checkout", "repo_key": repo_key, "mac_repository": saved_mirror.record(),
            }
            mac_test.write_json(paths.claims / saved_mirror.claim_file, record)
            run = subprocess.run

            def local_ssh(command: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
                self.assertEqual((command[0], command[-2]), ("ssh", destination.host))
                shell, flag, script = shlex.split(command[-1])
                fixture_commands = (
                    "pgrep() { return 1; }\n"
                    + f"lsof() {{ printf 'p41\\nn/home/fixture/{mirror_root}/old_checkout\\n"
                    + f"p42\\nn/home/fixture/{mirror_root}/new_checkout\\n'; }}\n"
                    + "ps() { printf 'fixture-start-%s\\n' \"$2\"; }\n"
                )
                return run([shell, flag, fixture_commands + script], capture_output=True, text=True, timeout=5)

            with mock.patch.object(target_config, "read_targets", return_value=(target,)), \
                 mock.patch.object(build_targets, "ssh_target_eligibility", return_value=target_config.ExcludedTarget(target, "busy")), \
                 mock.patch.object(claim_state, "state_paths", return_value=paths), \
                 mock.patch.object(eligibility, "repository_identity", side_effect=AssertionError("Use the saved Mac mirror")), \
                 mock.patch.object(subprocess, "run", side_effect=local_ssh) as probe:
                key = fixture.server.host_evidence_key(request, destination)
                fixture.server.host_requests[key] = request
                evidence = fixture.server.read_provider_evidence(key)
                _ = record.pop("mac_repository")
                mac_test.write_json(paths.claims / saved_mirror.claim_file, record)
                legacy = fixture.server.read_provider_evidence(key)
            self.assertEqual(probe.call_count, 1)
            assert isinstance(evidence, placement.FreshHostEvidence)
            adopted, = evidence.claims
            self.assertEqual(evidence.executions, ((adopted.reference, execution_identity.ObservedExecution(
                execution_identity.ProcessExecution(destination.host, "", 41, "fixture-start-41"))),))
            assert isinstance(legacy, placement.FreshHostEvidence)
            self.assertEqual(legacy.executions, ((adopted.reference, execution_identity.ExecutionEvidenceUnavailable(
                "the claim has no saved Mac mirror", fixture.clock.now())),))

    @contextmanager
    def mac_backoff_fixture(self, *, backoff: bool = True, probe_timeout: int = 1) -> Generator[MacBackoffTestFixture]:
        from scripts.build_queue.test_placement import placement_request
        from scripts.build_targets import target_config
        from scripts.mac_test import claim_state, eligibility, offload

        with AdmissionFixture() as fixture:
            settings = fixture.root / "mac_test.conf"
            _ = write_config_fixture(settings,
                f"offload=on\nhost=fixture-mac\nlinux_only.hana=\nprobe_timeout_s={probe_timeout}\nfree_floor_gib=60\n")
            target = target_config.SshTarget("mac", "macos", settings)
            request = placement_request(fixture)
            destination = request.constraints.candidates[0].destination
            mirror = claim_state.ResolvedMacMirror("fixture-hana", "hana", "hana.json", "hana", request.constraints.repository)
            probe = MacProbe()
            with (mock.patch.object(target_config, "read_targets", return_value=(target,)),
                  mock.patch.object(claim_state, "mac_repository_identity", return_value=mirror),
                  mock.patch.object(eligibility, "state_directory", return_value=fixture.root / "mac-state"),
                  mock.patch.object(offload.ProcessController, "capture", probe.capture)):
                if backoff:
                    offload.write_backoff(time.time() + 300, target.name)
                key = fixture.server.host_evidence_key(request, destination)
                fixture.server.host_requests[key] = request
                yield MacBackoffTestFixture(fixture, eligibility.backoff_path(target.name), key, probe)

    @contextmanager
    def mac_cycle_fixture(self, *, keys: int, probe_seconds: int = 0, blocked_key: bool = False,
                          repositories: Sequence[str] = (),
                          configure_probe: Callable[[MacBackoffTestFixture], Callable[[Sequence[str]], offload.CommandOutput]]
                          = mac_cycle_probe_ok,
                          ) -> Generator[tuple[MacBackoffTestFixture, tuple[placement.HostEvidenceKey, ...]]]:
        from scripts.mac_test import claim_state, mac_test

        start = threading.Event()
        readings: Queue[placement.HostEvidence] = Queue()
        guard = test_hang_guard.TEST_HANG_GUARD_SECS

        class ObservedWorker(service.HostEvidenceWorker):
            @override
            def _read(self) -> None:
                if not start.wait(guard):
                    raise AssertionError("the test did not start the probe cycle")
                super()._read()

            @override
            def _probe(self, key: placement.HostEvidenceKey, subscription: object) -> bool:
                running = super()._probe(key, subscription)
                readings.put(self.evidence(key))
                return running

        with (self.mac_backoff_fixture(backoff=False, probe_timeout=15) as fixture,
              mock.patch.object(service, "HostEvidenceWorker", ObservedWorker)):
            server = fixture.admission.server
            server.host_probe = server.read_provider_evidence
            if repositories:
                _ = write_config_fixture(conf.path("mac_test"), "".join(f"linux_only.{name}=\n" for name in repositories))
            probe = configure_probe(fixture)

            def capture(arguments: Sequence[str]) -> offload.CommandOutput:
                fixture.admission.clock.advance(probe_seconds)
                return probe(arguments)

            def mirror(root: str) -> claim_state.ResolvedMacMirror:
                name = Path(root).name
                return claim_state.ResolvedMacMirror("https://fixture.invalid/team/hana", name, f"{name}.json", name, root)

            _ = fixture.admission.enter_context(mock.patch.object(
                claim_state, "mac_repository_identity", side_effect=mirror))

            fixture.probe.answer = capture
            request = server.host_requests[fixture.key]
            subscribed: list[placement.HostEvidenceKey] = []
            read = cast(Callable[[placement.HostEvidenceKey, float], service.HostEvidenceReader],
                        getattr(server, "_host_evidence_reader"))
            try:
                for index in range(keys):
                    session = (OwningSession("blocked-session") if blocked_key and index == 0
                               else fixture.key.session)
                    root = fixture.admission.root / (repositories[index] if repositories else "hana")
                    key = replace(fixture.key, repository=str(root), package=f"package-{index}", session=session)
                    server.host_requests[key] = replace(request, constraints=replace(
                        request.constraints, repository=key.repository, package=key.package, session=session))
                    _ = read(key, guard)
                    subscribed.append(key)
                if blocked_key:
                    paths = mac_test.state_paths(fixture.key.destination.target)
                    paths.block.parent.mkdir(parents=True, exist_ok=True)
                    since = datetime.now(UTC)
                    mac_test.write_block(paths.block, claim_state.ActiveMacBlock(
                        "nate", "release work", since, since + timedelta(hours=1),
                        claim_state.RecordedSession("submitting-session", "nate"), mac_test.NO_SHOWRUNNER,
                        "off_by_this_block", mac_test.FREE_MESSAGE_NOT_NEEDED))
                start.set()
                completed = {readings.get(timeout=guard).key for _ in subscribed}
                self.assertEqual(completed, set(subscribed))
                worker, = server.host_targets.values()
                worker.close()
                worker.thread.join(guard)
                self.assertFalse(worker.thread.is_alive())
                yield fixture, tuple(subscribed)
            finally:
                server.close()
                start.set()
                for worker in server.host_targets.values():
                    worker.thread.join(guard)
                    self.assertFalse(worker.thread.is_alive())

    def test_mac_cycle_reads_the_host_once_for_many_policy_keys(self) -> None:
        with self.mac_cycle_fixture(keys=5) as (fixture, _keys):
            self.assertEqual(fixture.probe.call_count, 1)

    def test_mac_cycle_keeps_sixteen_slow_readings_fresh_together(self) -> None:
        with self.mac_cycle_fixture(keys=16, probe_seconds=12) as (fixture, keys):
            evidence = [fixture.admission.server.host_evidence(key) for key in keys]
            for reading in evidence:
                self.assertIsInstance(reading, placement.FreshHostEvidence)
                assert isinstance(reading, placement.FreshHostEvidence)
                self.assertEqual(reading.eligibility, placement.AllowedHost())
            self.assertEqual(len({reading.observed_at for reading in evidence}), 1)

    def test_mac_cycle_keeps_the_session_block_for_each_key(self) -> None:
        with self.mac_cycle_fixture(keys=3, blocked_key=True) as (fixture, keys):
            evidence = [fixture.admission.server.host_evidence(key) for key in keys]
            results = [reading.eligibility for reading in evidence if isinstance(reading, placement.FreshHostEvidence)]
            self.assertEqual(results, [placement.TemporaryExclusion("blocked"), placement.AllowedHost(),
                                       placement.AllowedHost()])
            self.assertEqual(fixture.probe.call_count, 1)

    def test_mac_cycle_includes_a_cleanup_claim_written_during_ssh(self) -> None:
        from scripts.mac_test import claim_state, mac_test, offload

        def configure_probe(fixture: MacBackoffTestFixture) -> Callable[[Sequence[str]], offload.CommandOutput]:
            paths = mac_test.state_paths(fixture.key.destination.target)
            request = fixture.admission.server.host_requests[fixture.key]
            paths.claims.mkdir(parents=True, exist_ok=True)
            cleanup: claim_state.RemoteJobClaim = {
                "claim_id": "cleanup", "episode": "previous", "attempt": "previous-attempt",
                "submitting_session": "", "pid": request.owner.pid, "proc_start": "ended",
                "remote_host": fixture.key.destination.host, "what": "check", "cleanup_pending": True,
                "worktree": str(fixture.admission.root / "hana"), "since": "2026-11-17T11:00:00+00:00",
            }
            kept = cleanup.copy()
            del kept["cleanup_pending"]
            kept.update(claim_id="kept", repo_key="kept", repo_name="kept",
                        mac_repository=claim_state.MacMirrorIdentity("kept", "kept", "kept.json", "kept").record())
            mac_test.write_json(paths.claims / "kept.json", kept)
            replaced = kept.copy()
            replaced.update(claim_id="old", repo_key="replaced", repo_name="replaced",
                            mac_repository=claim_state.MacMirrorIdentity(
                                "replaced", "replaced", "replaced.json", "replaced").record())
            mac_test.write_json(paths.claims / "replaced.json", replaced)

            def capture(_arguments: Sequence[str]) -> offload.CommandOutput:
                mac_test.write_json(paths.claims / "hana.json", cleanup)
                mac_test.write_json(paths.claims / "replaced.json", {**replaced, "claim_id": "new"})
                return offload.CommandOutput(0,
                    "free_gib=60\nhost=fixture-mac\nrc=0\nmirror_setup=0\tready\n"
                    + "mirror_process=0\t41\told-kept-start\nmirror_process=1\t42\told-replaced-start\n", "")

            return capture

        with self.mac_cycle_fixture(keys=2, configure_probe=configure_probe) as (fixture, keys):
            server = fixture.admission.server
            for key in keys:
                evidence = server.host_evidence(key)
                assert isinstance(evidence, placement.FreshHostEvidence)
                self.assertEqual([claim.reference.claim_id for claim in evidence.cleanup_claims], ["cleanup"])
                self.assertEqual(evidence.eligibility, placement.AllowedHost())
                executions = {reference.claim_id: execution for reference, execution in evidence.executions}
                self.assertEqual(executions["kept"], execution_identity.ObservedExecution(
                    execution_identity.ProcessExecution(key.destination.host, "", 41, "old-kept-start")))
                self.assertEqual(executions["new"], execution_identity.ExecutionEvidenceUnavailable(
                    "remote execution remains unresolved", evidence.observed_at))
                self.assertNotIn("old", executions)
            request = server.host_requests[keys[0]]
            reply, = server.handle_request(request)
            assert isinstance(reply, wire.PlacementWait)
            self.assertEqual(reply.eligibility, (placement.CandidateEligibility(
                keys[0].destination, placement.TemporaryExclusion("busy")),))

    def test_mac_cycle_applies_each_mirrors_setup_result(self) -> None:
        from scripts.mac_test import floor_claims, offload

        setup_reports: list[str] = []

        def configure_probe(fixture: MacBackoffTestFixture) -> Callable[[Sequence[str]], offload.CommandOutput]:
            root = fixture.admission.root
            ready = root / offload.mac_mirror_root().relative / "hana"
            ready.mkdir(parents=True)
            (ready / floor_claims.MIRROR_MARK).touch()
            runner = root / ".claude/scripts/lib/py"
            runner.parent.mkdir(parents=True)
            _ = runner.write_text("#!/bin/sh\nexit 1\n")
            runner.chmod(0o700)
            commands = "df() { printf 'header\\nfs a b 60\\n'; }\nhostname() { printf 'fixture-mac\\n'; }\n"

            def capture(arguments: Sequence[str]) -> offload.CommandOutput:
                result = subprocess.run(["/bin/sh", "-c", commands + arguments[-1]], capture_output=True,
                                        text=True, timeout=5, check=False)
                setup_reports.extend(line for line in result.stdout.splitlines() if line.startswith("mirror_setup="))
                return offload.CommandOutput(result.returncode, result.stdout, result.stderr)

            return capture

        with self.mac_cycle_fixture(keys=2, repositories=("hana", "other"),
                                    configure_probe=configure_probe) as (fixture, keys):
            evidence = [fixture.admission.server.host_evidence(key) for key in keys]
            results = [reading.eligibility for reading in evidence if isinstance(reading, placement.FreshHostEvidence)]
            self.assertEqual(results, [placement.AllowedHost(), placement.TemporaryExclusion("copy")])
            self.assertEqual(fixture.probe.call_count, 1)
            self.assertEqual(setup_reports, ["mirror_setup=0\tready", "mirror_setup=1\tfailed"])

    @contextmanager
    def mac_disk_sweep_fixture(self) -> Generator[MacDiskSweepFixture]:
        from scripts.mac_test import offload

        started, release, inline, disk_ready = (threading.Event() for _ in range(4))
        readings: Queue[placement.HostEvidence] = Queue()
        recoveries: list[threading.Thread] = []
        guard = test_hang_guard.TEST_HANG_GUARD_SECS
        caller = threading.current_thread()
        start = threading.Thread.start

        class ObservedWorker(service.HostEvidenceWorker):
            @override
            def _probe(self, key: placement.HostEvidenceKey, subscription: object) -> bool:
                running = super()._probe(key, subscription)
                readings.put(self.evidence(key))
                return running

        def capture(arguments: Sequence[str]) -> offload.CommandOutput:
            if arguments[-1].endswith("--floor-only --host mac"):
                started.set()
                thread = threading.current_thread()
                if thread is caller or thread.name.startswith("placement-evidence-"):
                    inline.set()
                elif not release.wait(guard):
                    raise AssertionError("the test did not release the sweep")
                disk_ready.set()
                return offload.CommandOutput(0, "", "")
            free = 60 if disk_ready.is_set() else 59
            return offload.CommandOutput(0, f"free_gib={free}\nhost=fixture-mac\nrc=0\nmirror_setup=0\tready\n", "")

        def start_thread(thread: threading.Thread) -> None:
            start(thread)
            if thread.name == "mac-disk-recovery":
                recoveries.append(thread)

        with (self.mac_backoff_fixture(backoff=False) as fixture,
              mock.patch.object(service, "HostEvidenceWorker", ObservedWorker),
              mock.patch.object(threading.Thread, "start", start_thread)):
            fixture.probe.answer = capture
            server = fixture.admission.server
            server.host_probe = server.read_provider_evidence
            try:
                yield MacDiskSweepFixture(fixture.admission, fixture.key, started, release, inline, disk_ready, readings)
            finally:
                release.set()
                server.close()
                for worker in server.host_targets.values():
                    worker.thread.join(guard)
                for recovery in recoveries:
                    recovery.join(guard)
                self.assertFalse(any(worker.thread.is_alive() for worker in server.host_targets.values()))
                self.assertFalse(any(recovery.is_alive() for recovery in recoveries))

    def test_mac_disk_probe_returns_pending_before_the_sweep_ends(self) -> None:
        from scripts.mac_test import offload

        with self.mac_disk_sweep_fixture() as fixture:
            evidence = fixture.admission.server.read_provider_evidence(fixture.key)
            self.assertFalse(fixture.inline.is_set(), "the probe ran the sweep inline")
            assert isinstance(evidence, service.MacDiskRecoveryPendingEvidence)
            self.assertEqual(evidence.disk, offload.MacDiskShort(59, 60))
            self.assertEqual(evidence.eligibility, placement.TemporaryExclusion("disk"))
            self.assertTrue(fixture.started.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
            self.assertFalse(fixture.release.is_set())

    def test_another_mac_key_gets_fresh_disk_evidence_during_the_sweep(self) -> None:
        with self.mac_disk_sweep_fixture() as fixture:
            server = fixture.admission.server
            guard = test_hang_guard.TEST_HANG_GUARD_SECS
            first = fixture.subscribe(fixture.key)
            self.assertEqual(fixture.readings.get(timeout=guard).key, fixture.key)
            self.assertFalse(fixture.inline.is_set(), "the first probe ran the sweep inline")
            self.assertTrue(fixture.started.wait(guard))
            fixture.admission.clock.advance(1)
            other = fixture.other_key()
            second = fixture.subscribe(other)
            self.assertIs(first.worker, second.worker)
            evidence = fixture.readings.get(timeout=guard)
            self.assertEqual(evidence.key, other)
            self.assertIsInstance(evidence, service.MacDiskRecoveryPendingEvidence)
            self.assertEqual(evidence.observed_at, fixture.admission.clock.now())
            self.assertEqual(server.host_evidence(other), evidence)
            self.assertFalse(fixture.release.is_set())

    def test_mac_at_the_disk_floor_stays_excluded_until_the_sweep_ends(self) -> None:
        from scripts.mac_test import offload

        with self.mac_disk_sweep_fixture() as fixture:
            server = fixture.admission.server
            first = server.read_provider_evidence(fixture.key)
            assert isinstance(first, service.MacDiskRecoveryPendingEvidence)
            self.assertTrue(fixture.started.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
            fixture.disk_ready.set()
            second = server.read_provider_evidence(fixture.other_key())
            assert isinstance(second, placement.FreshHostEvidence)
            self.assertEqual(second.eligibility, placement.TemporaryExclusion("disk"))
            assert isinstance(second, service.MacDiskRecoveryPendingEvidence)
            self.assertEqual(second.disk, offload.MacDiskShort(59, 60))
            self.assertFalse(fixture.release.is_set())

    def test_mac_disk_recovery_excludes_another_keys_cached_allowed_reading(self) -> None:
        for during_pass in (False, True):
            with self.subTest(during_pass=during_pass):
                self.assert_mac_disk_recovery_blocks_cached_reading(during_pass)

    def assert_mac_disk_recovery_blocks_cached_reading(self, during_pass: bool) -> None:
        with self.mac_disk_sweep_fixture() as fixture:
            server = fixture.admission.server
            fixture.disk_ready.set()
            cached = server.read_provider_evidence(fixture.key)
            assert isinstance(cached, placement.FreshHostEvidence)
            self.assertEqual(cached.eligibility, placement.AllowedHost())
            server.publish_host_evidence(cached)

            def start_recovery() -> None:
                fixture.disk_ready.clear()
                other = server.read_provider_evidence(fixture.other_key())
                self.assertIsInstance(other, service.MacDiskRecoveryPendingEvidence)
                self.assertTrue(fixture.started.wait(test_hang_guard.TEST_HANG_GUARD_SECS))

            request = server.host_requests[fixture.key]
            if during_pass:
                read = server.host_evidence

                def read_before_recovery(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                    evidence = read(key)
                    if key == fixture.key and not fixture.started.is_set():
                        self.assertEqual(evidence, cached)
                        start_recovery()
                    return evidence

                with mock.patch.object(server, "host_evidence", side_effect=read_before_recovery):
                    reply, = server.handle_request(request)
            else:
                start_recovery()
                reply, = server.handle_request(request)
            self.assertIsInstance(reply, wire.PlacementWait)
            assert isinstance(reply, wire.PlacementWait)
            self.assertEqual(reply.eligibility, (placement.CandidateEligibility(
                fixture.key.destination, placement.TemporaryExclusion("disk")),))
            self.assertNotIn(request.episode, server.current_assignments)
            self.assertEqual(server.host_observations[fixture.key], cached)

    def test_mac_sweep_completion_refreshes_every_key_during_a_probe(self) -> None:
        with self.mac_disk_sweep_fixture() as fixture:
            server = fixture.admission.server
            guard = test_hang_guard.TEST_HANG_GUARD_SECS
            first_read, publish_first, refresh = (threading.Event() for _ in range(3))
            read = server.host_probe

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                evidence = read(key)
                if key == fixture.key and not first_read.is_set():
                    first_read.set()
                    if not publish_first.wait(guard):
                        raise AssertionError("the test did not release the probe")
                return evidence

            server.host_probe = probe
            reader = fixture.subscribe(fixture.key)
            try:
                self.assertTrue(first_read.wait(guard))
                self.assertFalse(fixture.inline.is_set(), "the probe ran the sweep inline")
                self.assertTrue(fixture.started.wait(guard))
                other = fixture.other_key()
                _ = fixture.subscribe(other)
                request = reader.worker.requested.set

                def notify() -> None:
                    request()
                    refresh.set()

                with mock.patch.object(reader.worker.requested, "set", side_effect=notify):
                    fixture.release.set()
                    self.assertTrue(refresh.wait(guard))
                publish_first.set()
                self.assertIsInstance(fixture.readings.get(timeout=guard), service.MacDiskRecoveryPendingEvidence)
                refreshed = [fixture.readings.get(timeout=guard) for _ in range(2)]
                self.assertEqual({reading.key for reading in refreshed}, {fixture.key, other})
                for evidence in refreshed:
                    assert isinstance(evidence, placement.FreshHostEvidence)
                    self.assertEqual(evidence.eligibility, placement.AllowedHost())
            finally:
                publish_first.set()

    def test_good_mac_probe_ends_active_backoff(self) -> None:
        from scripts.mac_test import offload

        with self.mac_backoff_fixture() as fixture:
            deadline = fixture.backoff.read_text()
            fixture.probe.reply(offload.CommandOutput(255, "", "unreachable"))
            self.assertEqual(fixture.eligibility(), placement.TemporaryExclusion("backoff"))
            self.assertEqual(fixture.backoff.read_text(), deadline)

            fixture.admission.clock.advance(60)
            fixture.probe.reply(offload.CommandOutput(0, "free_gib=1000\nhost=fixture-mac\nrc=0\n", ""))
            self.assertEqual(fixture.eligibility(), placement.AllowedHost())
            self.assertFalse(fixture.backoff.exists())

    def test_good_mac_probe_preserves_a_backoff_written_during_the_probe(self) -> None:
        from scripts.mac_test import offload

        with self.mac_backoff_fixture() as fixture:
            newer_deadline = time.time() + 600

            def answer(_arguments: Sequence[str]) -> offload.CommandOutput:
                offload.write_backoff(newer_deadline)
                return offload.CommandOutput(0, "free_gib=1000\nhost=fixture-mac\nrc=0\n", "")

            fixture.probe.answer = answer
            self.assertEqual(fixture.eligibility(), placement.TemporaryExclusion("backoff"))
            self.assertEqual(fixture.backoff.read_text().strip(), f"{newer_deadline:.6f}")

    def test_mac_backoff_probes_each_target_once_across_policy_keys(self) -> None:
        from scripts.mac_test import offload

        with self.mac_backoff_fixture() as fixture:
            server = fixture.admission.server
            other = replace(fixture.key, package="another-package")
            request = server.host_requests[fixture.key]
            server.host_requests[other] = replace(request, constraints=replace(request.constraints, package=other.package))

            def answer(arguments: Sequence[str]) -> offload.CommandOutput:
                if arguments[-1] == "true":
                    return offload.CommandOutput(0, "", "")
                return offload.CommandOutput(255, "", "the full probe failed")

            fixture.probe.answer = answer
            _ = fixture.eligibility()
            _ = server.read_provider_evidence(other)
            self.assertEqual(fixture.probe.call_count, 1)
            self.assertTrue(fixture.backoff.exists())
            fixture.admission.clock.advance(60)
            _ = server.read_provider_evidence(other)
            self.assertEqual(fixture.probe.call_count, 2)

    def test_mac_recovery_returns_when_the_probe_result_lock_is_busy(self) -> None:
        from scripts.mac_test import claim_state, offload

        with self.mac_backoff_fixture() as fixture:
            fixture.probe.reply(offload.CommandOutput(0, "free_gib=1000\nhost=fixture-mac\nrc=0\n", ""))
            deadline = fixture.backoff.read_text()
            with claim_state.locked(fixture.backoff.parent / "probe-result.lock"):
                self.assertEqual(fixture.eligibility(), placement.TemporaryExclusion("backoff"))
            self.assertEqual(fixture.backoff.read_text(), deadline)

    def test_busy_diagnostic_skips_an_older_kept_claim_and_names_the_live_holder(self) -> None:
        from scripts.build_queue.test_placement import placement_request
        from scripts.build_targets import build_targets, target_config
        from scripts.lib import process_start as process_identity
        from scripts.mac_test import claim_state, mac_test

        with AdmissionFixture() as fixture:
            target = target_config.SshTarget("mac", "macos", fixture.root / "mac_test.conf")
            request = placement_request(fixture)
            candidate, = request.constraints.candidates
            repo_key = "https://fixture.invalid/team/hana"
            destination = replace(candidate.destination, scope=claim_state.RepositoryScope("hana", repo_key))
            request = replace(request, constraints=replace(request.constraints,
                candidates=(replace(candidate, destination=destination),)))
            paths = claim_state.StatePaths(fixture.root / "claims")
            paths.claims.mkdir(parents=True)
            kept: claim_state.RemoteJobClaim = {
                "claim_id": "older-kept", "episode": "old", "attempt": "old-attempt",
                "submitting_session": "old-session", "holder_session": "dead-holder",
                "pid": request.owner.pid, "proc_start": "dead-start", "remote_host": destination.host,
                "what": "dead check", "worktree": str(fixture.root), "since": "2026-11-17T10:00:00+00:00",
                "cleanup_pending": True, "repo_name": "hana", "repo_key": repo_key}
            live: claim_state.RemoteJobClaim = {
                "claim_id": "newer-live", "episode": "new", "attempt": "new-attempt",
                "submitting_session": "live-session", "holder_session": "live-holder",
                "pid": request.owner.pid, "proc_start": request.owner.start_time, "remote_host": destination.host,
                "what": "live check", "worktree": str(fixture.root / "removed"),
                "since": "2026-11-17T11:00:00+00:00", "repo_name": "checkout", "repo_key": repo_key}
            originals = {paths.claims / name: (json.dumps(record, indent=3) + "\n").encode()
                         for name, record in (("older.json", kept), ("newer.json", live))}
            for path, original in originals.items():
                _ = path.write_bytes(original)
            with mock.patch.object(target_config, "read_targets", return_value=(target,)), \
                 mock.patch.object(build_targets, "ssh_target_eligibility", return_value=target_config.EligibleTarget(target)), \
                 mock.patch.object(claim_state, "state_paths", return_value=paths), \
                 mock.patch.object(claim_state, "target_claim_scope", side_effect=AssertionError("The key is saved")), \
                 mock.patch.object(mac_test, "free_now", return_value="busy"), \
                 mock.patch.object(process_identity, "process_start", return_value=process_identity.Started(request.owner.start_time)), \
                 mock.patch.object(fixture.server, "_observe_provider_execution", return_value=execution_identity.ExecutionNotStarted()):
                key = fixture.server.host_evidence_key(request, destination)
                fixture.server.host_requests[key] = request
                evidence = fixture.server.read_provider_evidence(key)
                for path, original in originals.items():
                    self.assertEqual(path.read_bytes(), original)
                no_key = kept.copy()
                _ = no_key.pop("repo_key")
                mac_test.write_json(paths.claims / "older.json", no_key)
                (paths.claims / "newer.json").unlink()
                kept_evidence = fixture.server.read_provider_evidence(key)
            assert isinstance(kept_evidence, placement.FreshHostEvidence)
            self.assertEqual(kept_evidence.provider_diagnostic,
                placement.BusyProviderDiagnostic(destination.target, "dead check (kept for cleanup)", kept["since"]))
            assert isinstance(evidence, placement.FreshHostEvidence)
            self.assertEqual(evidence.provider_diagnostic,
                placement.BusyProviderDiagnostic(destination.target, live["what"], live["since"]))
            self.assertEqual(evidence.holder, placement.ResourceHolderSession("live-holder"))
            self.assertEqual([claim.reference.claim_id for claim in evidence.cleanup_claims], ["older-kept"])
            self.assertEqual([claim.reference.claim_id for claim in evidence.claims], ["newer-live"])

    def test_a_busy_mac_is_a_busy_exclusion_and_a_free_mac_is_allowed(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request
        from scripts.build_targets import build_targets, target_config
        from scripts.mac_test import claim_state, mac_test

        for answer, expected in (("busy", placement.TemporaryExclusion("busy")), ("free", placement.AllowedHost())):
            with self.subTest(answer=answer), AdmissionFixture() as fixture:
                target = target_config.SshTarget("mac", "macos", fixture.root / "mac_test.conf")
                request = placement_request(fixture)
                destination = request.constraints.candidates[0].destination
                with mock.patch.object(target_config, "read_targets", return_value=(target,)), \
                     mock.patch.object(build_targets, "ssh_target_eligibility", return_value=target_config.EligibleTarget(target)), \
                     mock.patch.object(claim_state, "state_paths", return_value=claim_state.StatePaths(fixture.root / "claims")), \
                     mock.patch.object(claim_state, "read_claims", return_value=()), \
                     mock.patch.object(mac_test, "free_now", return_value=answer) as free_now:
                    key = fixture.server.host_evidence_key(request, destination)
                    fixture.server.host_requests[key] = request
                    evidence = fixture.server.read_provider_evidence(key)
                assert isinstance(evidence, placement.FreshHostEvidence)
                self.assertEqual(evidence.eligibility, expected)
                self.assertEqual(free_now.call_args.args[1], target.settings)
                self.assertEqual(free_now.call_args.kwargs["mac_scope"], destination.scope)

    def test_a_mac_run_placed_before_a_mac_block_gets_no_mac_after_it(self) -> None:
        from scripts.build_hold.owning_session import OwningSession
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request
        from scripts.build_targets import target_config
        from scripts.mac_test import claim_state, mac_test

        with AdmissionFixture() as fixture:
            settings = fixture.root / "mac_test.conf"
            _ = write_config_fixture(settings, "offload=on\n", template="mac_test")
            target = target_config.SshTarget("mac", "macos", settings)
            paths = claim_state.StatePaths(fixture.root / "claims")
            paths.directory.mkdir()

            def install(session: OwningSession) -> placement.HostEvidenceKey:
                request = placement_request(fixture, session=session)
                request = replace(request, constraints=replace(request.constraints, command_kind="install"))
                key = fixture.server.host_evidence_key(request, request.constraints.candidates[0].destination)
                fixture.server.host_requests[key] = request
                return key

            def eligibility(key: placement.HostEvidenceKey) -> placement.HostEligibility:
                evidence = fixture.server.read_provider_evidence(key)
                assert isinstance(evidence, placement.FreshHostEvidence)
                return evidence.eligibility

            with mock.patch.object(target_config, "read_targets", return_value=(target,)), \
                 mock.patch.object(claim_state, "state_paths", return_value=paths):
                waiting = install(OwningSession("waiting-session"))
                self.assertEqual(eligibility(waiting), placement.AllowedHost())
                since = datetime.now(UTC)
                mac_test.write_block(paths.block, claim_state.ActiveMacBlock(
                    "nate", "release work", since, since + timedelta(hours=1),
                    claim_state.RecordedSession("holder-session", "nate"), mac_test.NO_SHOWRUNNER,
                    "off_by_this_block", mac_test.FREE_MESSAGE_NOT_NEEDED))
                self.assertEqual(eligibility(waiting), placement.TemporaryExclusion("blocked"))
                self.assertEqual(eligibility(install(OwningSession("holder-session"))), placement.AllowedHost())
                _ = paths.block.write_text("{", encoding="utf-8")
                self.assertEqual(eligibility(waiting), placement.TemporaryExclusion("state"))

    def test_hanging_or_failing_target_readers_do_not_delay_a_view(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import placement_request, local_candidate

        for fails in (False, True):
            with self.subTest(fails=fails), AdmissionFixture() as fixture:
                entered = {name: threading.Event() for name in ("mac", "natedev")}
                release = threading.Event()
                fixture.server.background_local_evidence = True

                def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                    entered[key.destination.target].set()
                    if fails:
                        raise OSError("private provider unavailable")
                    if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                        raise AssertionError("the test did not release the hanging probe")
                    return placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())

                fixture.server.host_probe = probe
                request = placement_request(fixture)
                remote = request.constraints.candidates[0].destination
                local = local_candidate(fixture).destination
                request = replace(request, constraints=replace(request.constraints,
                    candidates=(*request.constraints.candidates, placement.PlacementCandidate(local))))
                second = replace(request, episode="second-placement",
                    constraints=replace(request.constraints, command=("cargo", "check")))
                try:
                    for item in (request, second):
                        reply, = fixture.server.handle_request(item)
                        self.assertIsInstance(reply, wire.PlacementWait)
                    for event in entered.values():
                        self.assertTrue(event.wait(2))
                    self.assertEqual(len(fixture.server.host_targets), 2)
                    remote_keys = [key for key in fixture.server.host_readers if key.destination == remote]
                    self.assertEqual(len(remote_keys), 2)
                    self.assertIs(fixture.server.host_readers[remote_keys[0]].thread,
                                  fixture.server.host_readers[remote_keys[1]].thread)
                    with fixture.serving():
                        reading = client.view()
                        self.assertIsInstance(reading, records.QueueRead)
                        self.assertFalse(release.is_set())
                    if fails:
                        key = fixture.server.host_evidence_key(request, remote)
                        evidence = fixture.server.host_evidence(key)
                        self.assertIsInstance(evidence, placement.HostEvidenceUnavailable)
                finally:
                    for worker in fixture.server.host_targets.values():
                        worker.close()
                    release.set()
                    for worker in fixture.server.host_targets.values():
                        worker.thread.join(timeout=5)
                        self.assertFalse(worker.thread.is_alive())

    def test_local_hold_evidence_uses_each_requesters_checked_lineage(self) -> None:
        from scripts.build_hold import build_hold
        from scripts.build_hold.owning_session import OwningSession
        from scripts.build_queue import placement
        from scripts.mac_test.claim_state import WholeMacScope

        for case, callers, holds in (
            ("kept", ((110, "1100", True),), "single"),
            ("descendant", ((111, "1110", True),), "single"),
            ("unrelated", ((112, "1120", False),), "single"),
            ("service-kept", ((112, "1120", False),), "service-kept"),
            ("reused", ((114, "1140", False),), "single"),
            ("unavailable", ((115, "1150", False),), "single"),
            ("ended-hold-invalid-config", ((112, "1120", False),), "single"),
            ("one-hold", ((111, "1110", False),), "blocking-second"),
            ("both-holds", ((111, "1110", True),), "kept-second"),
            ("cache-kept-first", ((111, "1110", True), (112, "1120", False)), "single"),
            ("cache-unrelated-first", ((112, "1120", False), (111, "1110", True)), "single"),
            ("cache-reused", ((111, "1110", True), (111, "1111", False)), "single"),
            ("completed", ((111, "1110", True),), "single"),
            ("cancelled", ((111, "1110", True),), "single"),
        ):
            with self.subTest(case=case), AdmissionFixture() as fixture:
                directory = fixture.root / "holders"
                directory.mkdir()
                keep = [{"pid": 110, "start_time": "1100"}]
                if holds == "service-kept":
                    keep.append({"pid": os.getpid(), "start_time": "1"})
                for key in ("holder", "second") if holds.endswith("second") else ("holder",):
                    _ = (directory / key).write_text(json.dumps({
                        "holder": key, "since": "2026-11-12T12:00:00+00:00",
                        "for": "private test", "release_eta": "unknown",
                        "keep": [] if key == "second" and holds == "blocking-second" else keep,
                    }))
                _ = (fixture.root / "build_targets.conf").write_text(
                    "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
                write_process(fixture.proc.path, os.getpid(), start="1")
                write_process(fixture.proc.path, 110, start="1100")
                destination = placement.PlacementDestination("local", "natedev", fixture.server.host, WholeMacScope())
                fixture.server.host_probe = fixture.server.read_provider_evidence
                with mock.patch.dict(os.environ, {"BUILD_HOLD_DIR": str(directory)}), \
                     mock.patch.object(build_hold, "PROC_ROOT", fixture.proc.path):
                    for index, (pid, start, admitted) in enumerate(callers):
                        with self.subTest(pid=pid, start=start):
                            owner = fixture.fixture_owner(pid, start)
                            write_process(fixture.proc.path, pid,
                                parent=110 if pid in (111, 114) and start != "1111" else 1,
                                start="1141" if case == "reused" else start)
                            if case == "unavailable":
                                (fixture.proc.path / str(pid) / "stat").unlink()
                            local = fixture.request(owner=owner, episode=f"{case}-{index}")
                            constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown("private-source"),
                                local.argv, "fixture", "fixture", OwningSession("caller"),
                                (placement.PlacementCandidate(destination),))
                            request = wire.PlacementRequest(local.episode,
                                placement.SubmittingOwner(fixture.server.host, pid, start), local.since,
                                local.profile, constraints, identity=local.identity,
                                local=wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar,
                                    local.call, local.test_width, local.fresh_test), code_version=local.code_version)
                            key = fixture.server.host_evidence_key(request, destination)
                            self.assertIsInstance(fixture.server.host_evidence(key), placement.HostEvidenceUnavailable)
                            evidence = fixture.server.read_provider_evidence(key)
                            self.assertIsInstance(evidence, placement.FreshHostEvidence)
                            assert isinstance(evidence, placement.FreshHostEvidence)
                            self.assertEqual(evidence.eligibility,
                                placement.AllowedHost() if admitted else placement.TemporaryExclusion("blocked"))
                            fixture.allow_next_start()
                            reply, = fixture.server.handle_request(request)
                            if admitted:
                                self.assertIsInstance(reply, wire.LocalPlacementGranted)
                                assert isinstance(reply, wire.LocalPlacementGranted)
                                if case == "completed":
                                    fixture.release(reply.grant)
                                elif case == "cancelled":
                                    self.assertEqual(fixture.server.handle_request(wire.CancelPlacement(
                                        "cancel-local", request.episode, request.owner, "the caller cancelled")), [wire.Ok()])
                                if case in ("completed", "cancelled"):
                                    for retained in (fixture.server.host_readers, fixture.server.host_requests,
                                                     fixture.server.host_observations):
                                        self.assertNotIn(request.owner, [item.local_hold_owner for item in retained])
                            else:
                                self.assertIsInstance(reply, wire.PlacementWait)
                                assert isinstance(reply, wire.PlacementWait)
                                self.assertIsInstance(reply.wait, wire.PlacementResourceWait)
                                assert isinstance(reply.wait, wire.PlacementResourceWait)
                                self.assertEqual(reply.wait.holder,
                                    placement.ResourceHolderUnit("second" if holds == "blocking-second" else "holder"))
                                if case == "ended-hold-invalid-config":
                                    (directory / "holder").unlink()
                                    fixture.server.pending_configuration = gate.UnusableAdmissionConfiguration(
                                        "private invalid configuration")
                                    repeated, = fixture.server.handle_request(request)
                                    self.assertIsInstance(repeated, wire.PlacementWait)
                                    assert isinstance(repeated, wire.PlacementWait)
                                    self.assertIsInstance(repeated.wait, wire.PlacementResourceWait)
                                    assert isinstance(repeated.wait, wire.PlacementResourceWait)
                                    self.assertEqual(repeated.wait.holder, placement.ResourceHolderUnknown())


    def test_ci_admission_waits_for_release_and_status_names_each_job(self) -> None:
        from scripts.build_hold import build_hold
        from contextlib import redirect_stdout

        for draining in (False, True):
            with self.subTest(draining=draining), AdmissionFixture() as fixture:
                directory = fixture.root / "holders"
                directory.mkdir()
                ci_uid = os.getuid() + 10000
                requests: list[wire.ServiceEstimateRequest] = []
                for worker, job in ((110, "compile"), (120, "test")):
                    write_process(fixture.proc.path, worker, start=str(worker * 10))
                    _ = (fixture.proc.path / str(worker) / "comm").write_text("Runner.Worker\n")
                    owner = fixture.account_owner(ci_uid)
                    write_process(fixture.proc.path, owner.pid, parent=worker, start=owner.start_time)
                    status = fixture.proc.path / str(owner.pid) / "status"
                    _ = status.write_text(status.read_text() + f"Uid:\t{ci_uid}\t{ci_uid}\t{ci_uid}\t{ci_uid}\n")
                    requests.append(replace(fixture.service_request(owner=owner, job=job),
                        episode=f"uid:{ci_uid}:ci-{owner.pid}"))
                with mock.patch.dict(os.environ, {"BUILD_HOLD_DIR": str(directory)}), \
                     fixture.peer_account(ci_uid):
                    fixture.allow_next_start()
                    initial, = fixture.connect(requests[0]).replies()
                    self.assertIsInstance(initial, wire.RunnerAdmitted)
                    _ = (directory / "holder").write_text(json.dumps({
                        "holder": "fixture", "since": "2026-11-13T12:00:00+00:00", "for": "private drain",
                        "release_eta": "unknown",
                        "keep": [{"pid": 110, "start_time": "1100"}] if draining else [],
                    }))
                    fixture.allow_next_start()
                    waiting, = fixture.connect(requests[1]).replies()
                    self.assertIsInstance(waiting, wire.Wait)
                    assert isinstance(waiting, wire.Wait)
                    self.assertIn("waiting for release", waiting.text)
                    self.assertNotIn(requests[1].episode, fixture.server.reservations)
                    _ = fixture.run_pass()
                    view, = fixture.server.handle_request(wire.ViewRequest())
                    assert isinstance(view, wire.ViewReply)
                    self.assertIn(requests[1].owner.pid, [row.pid for row in view.reading.view.waiting])
                    with mock.patch.object(build_hold, "print_hold_audit"), redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(build_hold.main(["status", "--proc-root", str(fixture.proc.path)]), 0)
                    self.assertIn("CI running: hana CI · runner-a · compile since ", output.getvalue())
                    self.assertIn("CI queued: hana CI · runner-a · test since ", output.getvalue())
                    since = datetime.fromtimestamp(requests[1].since, ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S UTC")
                    self.assertIn(since, output.getvalue())
                    self.assertIn("draining (1 runs left)" if draining else " - held", output.getvalue())
                    if draining:
                        owner = fixture.account_owner(ci_uid)
                        write_process(fixture.proc.path, owner.pid, parent=110, start=owner.start_time)
                        status = fixture.proc.path / str(owner.pid) / "status"
                        _ = status.write_text(status.read_text() + f"Uid:\t{ci_uid}\t{ci_uid}\t{ci_uid}\t{ci_uid}\n")
                        continued = replace(fixture.service_request(owner=owner, job="compile-next"),
                                            episode=f"uid:{ci_uid}:ci-{owner.pid}")
                        fixture.allow_next_start()
                        reply, = fixture.connect(continued).replies()
                        self.assertIsInstance(reply, wire.RunnerAdmitted)
                    (directory / "holder").unlink()
                    fixture.allow_next_start()
                    _ = fixture.run_pass()
                    self.assertIn(requests[1].episode, fixture.server.reservations)

    def test_drain_keeps_another_users_ci_lineage_and_blocks_a_new_job(self) -> None:
        from scripts.build_hold import build_hold
        from scripts.build_hold.owning_session import NoOwningSession
        from scripts.build_queue import placement
        from scripts.mac_test.claim_state import WholeMacScope

        with AdmissionFixture() as fixture:
            directory = fixture.root / "holders"
            directory.mkdir()
            _ = (directory / "holder").write_text(json.dumps({
                "holder": "fixture", "since": "2026-11-13T12:00:00+00:00", "for": "private drain",
                "release_eta": "unknown", "keep": [{"pid": 110, "start_time": "1100"}],
            }))
            _ = (fixture.root / "build_targets.conf").write_text(
                "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")
            for pid, parent in ((110, 1), (111, 110), (120, 1), (121, 120)):
                write_process(fixture.proc.path, pid, parent=parent, start=str(pid * 10))
                _ = (fixture.proc.path / str(pid) / "comm").write_text(
                    "Runner.Worker" if pid in (110, 120) else "cargo")
            original_stat, original_bytes = Path.stat, Path.read_bytes
            ci_uid = os.getuid() + 1

            def ci_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
                result = original_stat(path, follow_symlinks=follow_symlinks)
                if path.parent == fixture.proc.path and path.name in {"110", "111", "120", "121"}:
                    fields = list(result)
                    fields[4] = ci_uid
                    return os.stat_result(fields)
                return result

            def private_bytes(path: Path) -> bytes:
                if path.parent.parent == fixture.proc.path and path.name in {"environ", "cmdline"}:
                    raise PermissionError("hana-linux process details are private")
                return original_bytes(path)

            destination = placement.PlacementDestination("local", "natedev", fixture.server.host, WholeMacScope())
            fixture.server.host_probe = fixture.server.read_provider_evidence
            with mock.patch.dict(os.environ, {"BUILD_HOLD_DIR": str(directory)}), \
                 mock.patch.object(build_hold, "PROC_ROOT", fixture.proc.path), \
                 mock.patch.object(Path, "stat", ci_stat), mock.patch.object(Path, "read_bytes", private_bytes):
                self.assertEqual((fixture.proc.path / "110").stat().st_uid, ci_uid)
                self.assertNotEqual(ci_uid, os.getuid())
                with self.assertRaises(PermissionError):
                    _ = (fixture.proc.path / "111/environ").read_bytes()
                for pid, expected in ((111, wire.LocalPlacementGranted), (121, wire.PlacementWait)):
                    with self.subTest(pid=pid):
                        local = fixture.request(owner=fixture.fixture_owner(pid, str(pid * 10)), episode=f"ci-{pid}")
                        local = replace(local, identity=replace(local.identity, name=f"CI runner job-{pid}"))
                        constraints = placement.PlacementConstraints(execution_identity.SourceKeyKnown("private-ci-source"),
                            local.argv, "fixture", "fixture", NoOwningSession(),
                            (placement.PlacementCandidate(destination),))
                        request = wire.PlacementRequest(local.episode,
                            placement.SubmittingOwner(fixture.server.host, pid, str(pid * 10)), local.since,
                            local.profile, constraints, identity=local.identity,
                            local=wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar,
                                local.call, local.test_width, local.fresh_test), code_version=local.code_version)
                        fixture.allow_next_start()
                        reply, = fixture.server.handle_request(request)
                        self.assertIsInstance(reply, expected)
                        if isinstance(reply, wire.PlacementWait):
                            self.assertIsInstance(reply.wait, wire.PlacementResourceWait)
                        lines = "\n".join(build_hold.ci_placement_lines(fixture.proc.path))
                        self.assertIn(f"CI {'running' if pid == 111 else 'queued'}: CI runner job-{pid} since ", lines)


class SwapInReadingsTests(unittest.TestCase):
    def test_system_readings_publish_one_window_total_for_snapshot_readers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = fake_memory_readings(root, available_kib=20 * records.GIB // 1024,
                                                swap_total_kib=16 * records.GIB // 1024,
                                                swap_free_kib=8 * records.GIB // 1024)
            with mock.patch.dict(os.environ, environment):
                reader = service.SystemReadings()
                with mock.patch.object(time, "monotonic", return_value=0):
                    first = reader.host()
                assert isinstance(first, records.HostMemory)
                self.assertEqual(first.swap_in_window_bytes, 0)
                pages = records.GIB // int(os.sysconf("SC_PAGE_SIZE"))
                _ = (root / "vmstat").write_text(f"pswpin {pages}\n")
                with mock.patch.object(time, "monotonic", return_value=100):
                    host = reader.host()
                assert isinstance(host, records.HostMemory)
                self.assertEqual(host.swap_in_window_bytes, records.GIB)
                inputs = records.QueueInputs((), (), (), host, records.NoPressureHold(), records.ReadyToStart(), 100)
                policy = admission_policy_fixture()
                queue_snapshot.write_queue_snapshot(root, inputs, policy)
                snapshot = queue_snapshot.read_queue_snapshot(root)
                assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
                self.assertEqual(snapshot.inputs.host, host)
                room = gate.inputs_room(snapshot.inputs, snapshot.policy)
                assert isinstance(room, records.MemoryRoom)
                self.assertEqual(room.agent_swap_demand, records.GIB)
                self.assertEqual(room.cap, 16 * records.GIB)
                _ = (root / "meminfo").write_text("MemAvailable: 10485760 kB\nMemTotal: 67108864 kB\n"
                                                   + "SwapTotal: 16777216 kB\nSwapFree: 8388608 kB\n")
                with mock.patch.object(time, "monotonic", return_value=101):
                    current = reader.host()
                room = gate.memory_room(current, (), policy)
                assert isinstance(room, records.MemoryRoom)
                self.assertEqual(room.cap, 6 * records.GIB)

    def test_system_readings_refuse_missing_and_unreadable_vmstat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, fake_memory_readings(root)):
                reader = service.SystemReadings()
                path = root / "vmstat"
                path.unlink()
                for is_directory in (False, True):
                    with self.subTest(is_directory=is_directory):
                        if is_directory:
                            path.mkdir()
                        reading = reader.host()
                        self.assertIsInstance(reading, records.ReadingUnavailable)
                        assert isinstance(reading, records.ReadingUnavailable)
                        self.assertIn("swap-in reading unavailable", reading.reason)
                        self.assertIn(str(path), reading.reason)


class ReferenceActivityReader(ScopeActivityReader):
    """Keep the original ancestry traversal as an independent test reference."""

    @override
    def run(self, pid: int) -> LiveBuildRun | NoLiveBuildRun:
        for ancestor in self.ancestors(pid):
            run = self._run_at(ancestor, NoLiveBuildRun())
            if isinstance(run, LiveBuildRun) or self.environment(ancestor).get("BUILDLOG_RUN"):
                return run
        return NoLiveBuildRun()


class ProcessRunEvidenceTests(unittest.TestCase):
    def test_process_run_evidence_reuses_ancestors_and_keeps_run_boundaries(self) -> None:
        for cyclic in (False, True):
            with self.subTest(cyclic=cyclic), AdmissionFixture() as fixture:
                for pid in range(1, 201):
                    parent = 3 if cyclic and pid == 1 else pid - 1
                    write_process(fixture.proc.path, pid, parent=parent)
                    directory = fixture.proc.path / str(pid)
                    _ = (directory / "cmdline").write_bytes(
                        b"bash\0verify.sh\0" if pid in (2, 17) else b"python3\0fixture.py\0")
                    environment = b"BUILDLOG_RUN=absent\0" if pid == 41 else b""
                    if pid == 88:
                        environment = b"BUILDLOG_RUN=17:17\0"
                    _ = (directory / "environ").write_bytes(environment)
                processes = fixture.proc.processes()
                activity = ReferenceActivityReader(processes, fixture.proc.path)
                expected = {pid: activity.run(pid) for pid in processes}
                reader = ScopeActivityReader(processes, fixture.proc.path)
                with mock.patch.object(reader, "command", wraps=reader.command) as command:
                    actual = {pid: reader.run(pid) for pid in processes}
                self.assertEqual(actual, expected)
                if not cyclic:
                    self.assertLessEqual(command.call_count, len(processes))
                self.assertIsInstance(actual[41], NoLiveBuildRun)
                self.assertEqual(actual[88], actual[17])


class ReplyPublicationTests(unittest.TestCase):
    def test_view_captures_granted_local_reservations_and_verify_owners(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=0, step="test")
            request = replace(request, identity=replace(request.identity, verify=records.VerifyCall("test", "verify test", 1, 1)))
            grant = only_admitted(fixture.server.handle_request(request))
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            self.assertEqual(reply.reading.explanation.decided_from.reservations, ())
            decoded = wire.parse_reply(wire.encode_reply(reply))
            assert isinstance(decoded, wire.ViewReply)
            occupancy = decoded.reading.explanation.local_occupancy
            assert occupancy is not None
            row, = occupancy.reservations
            self.assertEqual(row["pid"], request.owner.pid)
            self.assertEqual(row.get("reservation_id"), grant.reservation)
            owner = records.run_owner(request.owner.pid, request.owner.start_time)
            self.assertEqual(occupancy.verify_run_owners, {"fixture": frozenset({owner})})

    def test_publish_writes_the_view_reply_and_publication_time(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            path = fixture.state_dir / queue_snapshot.VIEW_NAME
            for running, waiting, gap in ((30, 3, 0.25), (3, 30, 0.25), (3, 2, 30)):
                fixture.configure(running_pass_seconds=running, waiting_pass_seconds=waiting, min_pass_gap_seconds=gap)
                fixture.clock.advance(1)
                with mock.patch.object(queue_snapshot, "write_view_snapshot", wraps=queue_snapshot.write_view_snapshot) as write:
                    _ = server.run_pass()
                reply, = server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                saved = cast(dict[str, object], json.loads(path.read_text()))
                self.assertEqual(saved["written_at"], fixture.clock.now())
                fresh_until = fixture.clock.now() + max(running, waiting, gap)
                write.assert_called_once_with(fixture.state_dir, fixture.clock.now(), fresh_until, server.reply_bytes(reply))
                self.assertEqual(saved["fresh_until"], fresh_until)
                self.assertEqual(saved["reply"], wire.encode_reply(reply))
                self.assertEqual(queue_snapshot.read_view_snapshot(fixture.state_dir),
                                 queue_snapshot.PublishedView(fixture.clock.now(), fresh_until, reply.reading))

    def test_next_loop_pass_publishes_consumed_guest_permission(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, assignment_for, consume_guest_permission, guest_gate,
            mac_linux_and_natedev_request, publish_candidate,
        )

        with PlacementFixture() as fixture:
            server = fixture.server
            request = mac_linux_and_natedev_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            holder = guest_gate(fixture, assignment)
            configuration = server.configuration
            assert isinstance(configuration, gate.UsableAdmissionConfiguration)
            gap = configuration.config.min_pass_gap_seconds
            fixture.clock.advance(gap)
            server.run_once()
            before = queue_snapshot.read_view_snapshot(fixture.state_dir)
            guest, = (row for row in before.reading.view.running if row.assignment_target == "mac-linux")
            self.assertEqual(guest.started_at, 0)
            self.assertGreater(server.next_pass, fixture.clock.monotonic() + gap)
            consumed_at = fixture.clock.now()
            input_event_count = server.input_event_count
            granted, = consume_guest_permission(fixture, assignment, holder)
            self.assertIsInstance(granted, wire.FirstAuthorization)
            self.assertGreater(server.input_event_count, input_event_count)
            self.assertTrue(server.pass_stale)
            self.assertEqual(queue_snapshot.read_view_snapshot(fixture.state_dir), before)
            fixture.clock.advance(gap)
            server.run_once()
            after = queue_snapshot.read_view_snapshot(fixture.state_dir)
            guest, = (row for row in after.reading.view.running if row.assignment_target == "mac-linux")
            self.assertEqual(guest.started_at, consumed_at)
            self.assertIsNone(guest.confirmed_guest_run)

    def test_view_request_does_not_run_a_pending_or_stale_pass(self) -> None:
        for reason in ("pending configuration", "stale pass"):
            with self.subTest(reason=reason), AdmissionFixture() as fixture:
                server = fixture.server
                previous, = server.handle_request(wire.ViewRequest())
                configuration = server.configuration
                assert isinstance(configuration, gate.UsableAdmissionConfiguration)
                if reason == "pending configuration":
                    server.pending_configuration = configuration
                else:
                    server.pass_stale = True
                with (mock.patch.object(server, "run_pass", wraps=server.run_pass) as passed,
                      mock.patch.object(gate, "admission_pass", wraps=gate.admission_pass) as refreshed):
                    current, = server.handle_request(wire.ViewRequest())
                passed.assert_not_called()
                refreshed.assert_not_called()
                self.assertIs(current, previous)

    def test_view_and_explain_reuse_the_bytes_of_the_completed_pass(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            server = fixture.server
            inputs, result, policy = server.last_inputs, server.last_result, server.last_policy
            assert isinstance(inputs, records.QueueInputs)
            assert isinstance(result, gate.PassResult)
            assert isinstance(policy, records.AdmissionPolicy)
            reading = replace(gate.queue_read_from_pass(inputs, policy, result), diagnostics=server.last_diagnostics)
            reading = replace(reading, view=replace(reading.view, move_line=server.move_line))
            view_reading = replace(reading, explanation=replace(reading.explanation,
                local_occupancy=records.LocalAdmissionOccupancy((), {})))
            expected_view = wire.encode_reply(wire.ViewReply(view_reading)).encode()
            expected_explain = wire.encode_reply(wire.ExplainReply(reading.explanation, reading.diagnostics)).encode()
            with mock.patch.object(wire, "encode_reply", wraps=wire.encode_reply) as encode:
                _ = server.run_pass()
                self.assertEqual(encode.call_count, 1)
                view, = server.handle_request(wire.ViewRequest())
                explain, = server.handle_request(wire.ExplainRequest())
                self.assertEqual(server.reply_bytes(view), expected_view)
                self.assertEqual(server.reply_bytes(explain), expected_explain)
                self.assertEqual(encode.call_count, 2)
                encode.reset_mock()
                self.assertEqual(server.reply_bytes(view), expected_view)
                self.assertEqual(server.reply_bytes(explain), expected_explain)
                encode.assert_not_called()

    def test_pending_configuration_uses_new_values_with_the_published_reading(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            evidence = server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            reader = FixedSystemReader(evidence)
            server.system_reader = reader
            configuration = server.configuration
            assert isinstance(configuration, gate.UsableAdmissionConfiguration)
            policy = replace(configuration.config.policy, safety_bytes=9 * GIB)
            server.pending_configuration = replace(configuration, config=replace(configuration.config, policy=policy))
            _ = server.run_pass()
            reply, = server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            self.assertEqual(reply.reading.explanation.policy.safety_bytes, 9 * GIB)
            self.assertEqual(server.reply_bytes(reply), wire.encode_reply(reply).encode())
            self.assertIsInstance(server.pending_configuration, service.NoPass)

    def test_pass_uses_the_reader_evidence_without_operating_system_reads(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            server = fixture.server
            evidence = server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            reader = FixedSystemReader(replace(evidence, host=records.HostMemory(7 * GIB, 64 * GIB)))
            server.system_reader = reader
            with mock.patch.object(fixture.proc, "processes", side_effect=AssertionError("process table read")), mock.patch.object(
                    fixture.proc, "owner_start", side_effect=AssertionError("owner read")), mock.patch.object(
                    fixture.proc, "lifetime", side_effect=AssertionError("scope read")), mock.patch.object(
                    fixture.readings, "host", side_effect=AssertionError("memory read")), mock.patch.object(
                    fixture.readings, "pressure", side_effect=AssertionError("pressure read")), mock.patch.object(
                    service, "ScopeActivityReader", side_effect=AssertionError("activity read")):
                _ = server.run_pass()
            inputs = server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(inputs.host, reader.latest.host)
            self.assertTrue(reader.plans)
            used_evidence = server.system_evidence
            assert isinstance(used_evidence, service.SystemEvidence)
            self.assertEqual(used_evidence.read_at, evidence.read_at)

    def test_reader_publishes_a_complete_reading_and_wakes_the_loop(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            initial = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(initial, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            expected_plan = service.SystemReadingPlan(replace(config,
                running_pass_seconds=0.001, waiting_pass_seconds=0.001), (), (), False)
            entered = threading.Event()
            release = threading.Event()
            updated = replace(initial, read_at=initial.read_at + 1,
                              host=records.HostMemory(2 * GIB, 64 * GIB))
            threads: list[int] = []

            def read(plan: service.SystemReadingPlan, readings: service.Readings,
                     proc: service.Proc, clock: service.Clock) -> service.SystemEvidence:
                self.assertEqual(plan, expected_plan)
                self.assertIs(readings, fixture.readings)
                self.assertIs(proc, fixture.proc)
                self.assertIs(clock, fixture.clock)
                threads.append(threading.get_ident())
                entered.set()
                if not release.wait(3):
                    raise AssertionError("the test did not release the reader")
                return updated

            with mock.patch.object(service, "read_system", side_effect=read):
                reader = service.OperatingSystemReader(expected_plan, initial, fixture.readings,
                    fixture.proc, fixture.clock, server.reading_events)
                try:
                    self.assertTrue(entered.wait(3))
                    self.assertIs(reader.latest, initial)
                    release.set()
                    self.assertTrue(select.select([server.reading_events], [], [], 3)[0])
                    self.assertIs(reader.latest, updated)
                    self.assertTrue(threads)
                    self.assertTrue(all(identity != threading.get_ident() for identity in threads))
                finally:
                    release.set()
                    reader.close()

    def test_mine_keeps_the_full_reading_and_reuses_view_bytes(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request()
            _ = fixture.server.handle_request(first)
            fixture.free(0)
            second = fixture.request()
            second = replace(second, identity=replace(second.identity, session_id="other-session"))
            _ = fixture.server.handle_request(second)
            server = fixture.server
            own, = server.handle_request(wire.MineRequest(first.identity.session_id))
            view, = server.handle_request(wire.ViewRequest())
            assert isinstance(own, wire.MineReply)
            assert isinstance(view, wire.ViewReply)
            self.assertEqual(own.reading, view.reading)
            expected = wire.encode_reply(own).encode()
            _ = server.reply_bytes(view)
            with mock.patch.object(wire, "encode_reply", side_effect=AssertionError("Mine repeated the encoding")):
                self.assertEqual(server.reply_bytes(own), expected)
                other, = server.handle_request(wire.MineRequest("other-session"))
                self.assertEqual(server.reply_bytes(other), expected)
            self.assertIn(b"other-session", expected)

    def test_reader_starts_when_an_unusable_configuration_becomes_usable(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            usable = server.configuration
            initial = server.system_evidence
            assert isinstance(initial, service.SystemEvidence)
            server.configuration = gate.UnusableAdmissionConfiguration("private invalid configuration")
            server.last_usable_configuration = service.NoPass()
            server.system_evidence = service.NoPass()
            reader = FixedSystemReader(initial)
            with mock.patch.object(service, "OperatingSystemReader", return_value=reader) as start:
                server.start_reader()
                start.assert_not_called()
                server.pending_configuration = usable
                _ = server.run_pass()
                start.assert_called_once()
                self.assertIs(server.system_reader, reader)
                self.assertTrue(reader.plans)

    def test_reader_sends_a_failure_to_the_pass_and_continues_after_an_early_read(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            initial = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(initial, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            plan = service.SystemReadingPlan(replace(config, running_pass_seconds=3600), (), (), False)
            failure = OSError("private memory read failed")
            updated = replace(initial, read_at=initial.read_at + 1)
            with mock.patch.object(service, "read_system", side_effect=[failure, updated]) as read:
                reader = service.OperatingSystemReader(plan, initial, fixture.readings,
                    fixture.proc, fixture.clock, server.reading_events)
                server.system_reader = reader
                try:
                    reader.request_read()
                    self.assertTrue(select.select([server.reading_events], [], [], 3)[0])
                    _ = os.eventfd_read(server.reading_events)
                    with self.assertRaises(OSError) as raised:
                        _ = server.run_pass()
                    self.assertIs(raised.exception, failure)
                    self.assertTrue(reader.thread.is_alive())
                    reader.request_read()
                    self.assertTrue(select.select([server.reading_events], [], [], 3)[0])
                    _ = os.eventfd_read(server.reading_events)
                    _ = server.run_pass()
                    self.assertEqual(server.system_evidence, updated)
                    self.assertEqual(read.call_count, 2)
                finally:
                    reader.close()

    def test_a_reading_finds_build_runs_and_accounts_only_for_the_owners_a_pass_asks_about(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            config = fixture.server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            original = ScopeActivityReader.run
            asked: list[int] = []

            def run(reader: ScopeActivityReader, pid: int) -> LiveBuildRun | NoLiveBuildRun:
                asked.append(pid)
                return original(reader, pid)

            with mock.patch.object(ScopeActivityReader, "run", run), mock.patch.object(
                    service, "process_uid", side_effect=AssertionError("a reading reads no account")):
                evidence = service.read_system(service.SystemReadingPlan(config, (), (), False),
                                               fixture.readings, fixture.proc, fixture.clock)
                self.assertIn(request.owner.pid, evidence.processes)
                self.assertEqual(asked, [])
                expected = original(ScopeActivityReader(evidence.processes, fixture.proc.path), request.owner.pid)
                self.assertEqual(evidence.build_run(request.owner.pid), expected)
                self.assertEqual(asked, [request.owner.pid])

    def test_unknown_admit_and_launch_owners_request_one_early_read(self) -> None:
        for launch in (False, True):
            with self.subTest(launch=launch), AdmissionFixture() as fixture:
                server = fixture.server
                initial = server.system_evidence
                config = server.last_usable_configuration
                assert isinstance(initial, service.SystemEvidence)
                assert isinstance(config, records.MemoryConfig)
                reader = FixedSystemReader(initial)
                server.system_reader = reader
                request = fixture.launch() if launch else fixture.request()
                self.assertNotIn(request.owner.pid, initial.processes)
                peer = fixture.connect(request)
                self.assertEqual(peer.replies(), [])
                self.assertEqual(reader.requests, 1)
                self.assertEqual(len(server.deferred_requests), 1)
                reader.latest = service.read_system(service.SystemReadingPlan(config, (), (), False),
                    fixture.readings, fixture.proc, fixture.clock)
                os.eventfd_write(server.reading_events, 1)
                replies = peer.replies()
                self.assertTrue(replies)
                self.assertFalse(any(isinstance(reply, wire.Refused) for reply in replies))
                self.assertFalse(server.deferred_requests)
                self.assertEqual(reader.requests, 1)

    def test_new_placement_coordinator_waits_for_fresh_process_evidence(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate

        with PlacementFixture() as fixture:
            server = fixture.server
            initial = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(initial, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            reader = FixedSystemReader(initial)
            server.system_reader = reader
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            self.assertNotIn(request.owner.pid, initial.processes)
            replies = fixture.connect(request).replies()
            self.assertFalse(any(isinstance(reply, wire.Refused) for reply in replies), replies)
            self.assertEqual(len(replies), 1)
            self.assertIsInstance(replies[0], wire.PlacementWait)
            self.assertNotIn(request.episode, server.current_assignments)
            reader.latest = service.read_system(service.SystemReadingPlan(config, (), (), False),
                fixture.readings, fixture.proc, fixture.clock)
            os.eventfd_write(server.reading_events, 1)
            server.run_once(0)
            reply, = fixture.connect(request).replies()
            self.assertIsInstance(reply, wire.Assignment)

    def test_refused_deferred_connection_cannot_admit_on_a_later_read(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            initial = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(initial, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            reader = FixedSystemReader(initial)
            server.system_reader = reader
            request = fixture.request()
            peer = fixture.connect(request)
            self.assertEqual(peer.replies(), [])
            self.assertEqual(len(server.deferred_requests), 1)
            connection = next(iter(server.connections.values()))
            peer.socket.sendall(wire.encode_request(wire.ViewRequest()).encode())
            server.run_once(0)
            self.assertTrue(connection.finished)
            self.assertIn(b"one request", connection.sending)
            self.assertFalse(server.deferred_requests)
            reader.latest = service.read_system(service.SystemReadingPlan(config, (), (), False),
                fixture.readings, fixture.proc, fixture.clock)
            os.eventfd_write(server.reading_events, 1)
            server.run_once(0)
            self.assertNotIn(request.episode, server.reservations)
            self.assertNotIn(request.episode, server.ledger)
            self.assertFalse(server.deferred_requests)
            replies = peer.replies()
            self.assertEqual(len(replies), 1)
            self.assertIsInstance(replies[0], wire.Refused)

    def test_reused_pid_defers_until_the_reader_has_the_current_owner_identity(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            config = server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            request = fixture.request()
            current = service.read_system(service.SystemReadingPlan(config, (), (), False),
                fixture.readings, fixture.proc, fixture.clock)
            processes = dict(current.processes)
            process = processes[request.owner.pid]
            processes[request.owner.pid] = replace(process, start="previous-process-start")
            reader = FixedSystemReader(replace(current, processes=processes))
            server.system_reader = reader
            peer = fixture.connect(request)
            self.assertEqual(peer.replies(), [])
            self.assertEqual(reader.requests, 1)
            self.assertEqual(len(server.deferred_requests), 1)
            self.assertNotIn(request.episode, server.ledger)
            reader.latest = current
            os.eventfd_write(server.reading_events, 1)
            replies = peer.replies()
            self.assertTrue(replies)
            self.assertFalse(any(isinstance(reply, wire.Refused) for reply in replies))
            self.assertFalse(server.deferred_requests)

    def test_pending_peak_hold_restores_the_charge_from_the_same_raw_reading(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(peak_hold_seconds=1)
            request = fixture.request(need=8 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 8 * GIB)
            record = fixture.server.reservations[request.episode]
            _ = Path(record.path).with_suffix(".wave").write_text(f"{8 * GIB} {int(fixture.clock.now())}\n")
            fixture.clock.advance(10)
            server = fixture.server
            _ = server.run_pass()
            evidence = server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            server.system_reader = FixedSystemReader(evidence)
            configuration = server.configuration
            assert isinstance(configuration, gate.UsableAdmissionConfiguration)
            policy = replace(configuration.config.policy, peak_hold=records.PeakHold(1000))
            server.pending_configuration = replace(configuration, config=replace(configuration.config, policy=policy))
            _ = server.run_pass()
            reply, = server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            inputs = server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            memory, = (step.measured for step in inputs.running if gate.running_has_reservation(step))
            assert isinstance(memory, records.MeasuredMemory)
            self.assertEqual(memory.observed_peak, 8 * GIB)

    def test_width_change_uses_the_current_frozen_need_with_the_same_raw_reading(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=12 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            server = fixture.server
            old_need = records.TestPhaseNeed(GIB, 1, 8, 8, "same_width", 1.0)
            record = replace(server.reservations[request.episode], test_phase_need=old_need)
            server.reservations[request.episode] = record
            phase = gate.test_phase_path(service.reservation_row(record))
            assert isinstance(phase, Path)
            _ = phase.write_text(wire.encode_record({"test_phase_peak_bytes": GIB,
                "test_phase_started_at": fixture.clock.now(), "test_threads": 8,
                "call_id": "call-1", "test_scope": "full", "repo_identity": "fixture",
                "package": "", "filters": []}))
            _ = server.run_pass()
            evidence = server.system_evidence
            assert isinstance(evidence, service.SystemEvidence)
            server.system_reader = FixedSystemReader(evidence)
            new_need = records.TestPhaseNeed(8 * GIB, 1, 16, 8, "scaled_up", 1.0)
            with mock.patch.object(gate, "test_phase_need", return_value=new_need):
                self.assertEqual(server.handle_request(wire.WidthRequest("review-width", grant.reservation, 16)), [wire.Ok()])
            self.assertEqual(server.reservations[request.episode].test_phase_need, new_need)
            _ = server.run_pass()
            inputs = server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            memory, = (step.measured for step in inputs.running if gate.running_has_reservation(step))
            assert isinstance(memory, records.MeasuredMemory)
            self.assertIsInstance(memory.peak_history, records.TestPhaseStarted)
            assert isinstance(memory.peak_history, records.TestPhaseStarted)
            self.assertEqual(memory.peak_history.need, new_need)
            fixture.free(6 * GIB)
            fixture.allow_next_start()
            following = fixture.request(need=4 * GIB)
            config = server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            assert isinstance(server.system_reader, FixedSystemReader)
            fresh = service.read_system(service.SystemReadingPlan(config, (), (), False),
                fixture.readings, fixture.proc, fixture.clock)
            server.system_reader.latest = replace(fresh, reservations=evidence.reservations)
            replies = server.handle_request(following)
            self.assertTrue(any(isinstance(reply, wire.Wait) for reply in replies))
            self.assertFalse(any(isinstance(reply, wire.Admitted) for reply in replies))

    def test_read_started_before_the_request_does_not_refuse_a_new_owner(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            evidence = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(evidence, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            reader = FixedSystemReader(evidence)
            server.system_reader = reader
            fixture.clock.advance(1)
            request = fixture.request()
            peer = fixture.connect(request)
            self.assertEqual(peer.replies(), [])
            reader.latest = replace(evidence, read_at=fixture.clock.now())
            os.eventfd_write(server.reading_events, 1)
            self.assertEqual(peer.replies(), [])
            self.assertEqual(len(server.deferred_requests), 1)
            self.assertNotIn(request.episode, server.ledger)
            fixture.clock.advance(1)
            reader.latest = service.read_system(service.SystemReadingPlan(config, (), (), False),
                fixture.readings, fixture.proc, fixture.clock)
            os.eventfd_write(server.reading_events, 1)
            replies = peer.replies()
            self.assertTrue(replies)
            self.assertFalse(any(isinstance(reply, wire.Refused) for reply in replies))

    def test_waiting_cadence_wakes_a_reader_in_its_running_delay(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server
            initial = server.system_evidence
            config = server.last_usable_configuration
            assert isinstance(initial, service.SystemEvidence)
            assert isinstance(config, records.MemoryConfig)
            plan = service.SystemReadingPlan(replace(config,
                running_pass_seconds=5, waiting_pass_seconds=1), (), (), False)
            with mock.patch.object(threading.Thread, "start"):
                reader = service.OperatingSystemReader(plan, initial, fixture.readings,
                    fixture.proc, fixture.clock, server.reading_events)
            started: list[float] = []

            def delay(seconds: float) -> bool:
                self.assertEqual(seconds, 5)
                reader.update(replace(plan, waiting=True))
                fixture.clock.advance(1 if reader.requested.is_set() else seconds)
                return reader.requested.is_set()

            def read(*_args: object) -> service.SystemEvidence:
                started.append(fixture.clock.monotonic())
                reader.stopped.set()
                return initial

            with mock.patch.object(reader.requested, "wait", side_effect=delay), mock.patch.object(
                    service, "read_system", side_effect=read):
                reader.thread.run()
            self.assertEqual(started, [1])

    def test_raw_reader_does_not_apply_reservation_policy(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=8 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 8 * GIB)
            config = fixture.server.last_usable_configuration
            assert isinstance(config, records.MemoryConfig)
            record = fixture.server.reservations[request.episode]
            plan = service.SystemReadingPlan(config, (record,), (), False)
            with mock.patch.object(gate, "reservation_memory", side_effect=AssertionError("reservation policy")), mock.patch.object(
                    gate, "step_promise", side_effect=AssertionError("reservation promise")), mock.patch.object(
                    gate, "test_phase_history", side_effect=AssertionError("test-phase policy")):
                reading = service.read_system(plan, fixture.readings, fixture.proc, fixture.clock)
            memory = reading.reservations[request.episode].memory
            assert isinstance(memory, records.MeasuredMemory)
            self.assertEqual(memory.promise, records.NoPromise())
            self.assertEqual(memory.observed_peak, 0)
            self.assertEqual(reading.started_monotonic, fixture.clock.monotonic())
            self.assertEqual(service.reservation_charge(reading.reservations[request.episode],
                service.reservation_row(record), fixture.clock.now(), config.policy),
                gate.reservation_memory(Path(record.path), service.reservation_row(record),
                    datetime.fromtimestamp(fixture.clock.now()), fixture.proc.processes(), fixture.proc.path, config.policy))

    def test_persist_matches_full_encoding_after_each_durable_change(self) -> None:
        with AdmissionFixture() as fixture:
            server = fixture.server

            def assert_full_state() -> None:
                expected = service.ServiceState(server.reservations, server.places, server.ledger,
                    server.spacing, server.priority, server.build_history_generation,
                    server.run_places, server.caller_versions, server.lifecycle_outbox)
                self.assertEqual((fixture.state_dir / "state.json").read_text(), wire.encode_record(expected))

            assert_full_state()
            request = fixture.request()
            grant = only_admitted(server.handle_request(request))
            assert_full_state()
            _ = server.handle_request(wire.WidthRequest("cached-width", grant.reservation, 3))
            assert_full_state()
            record = server.reservations[request.episode]
            record.queue_waiter["session_name"] = "changed nested value"
            server.persist()
            assert_full_state()
            _ = server.handle_request(wire.TokenWaitRequest("cached-token", grant.reservation))
            assert_full_state()
            fixture.allow_next_start()
            next_request = fixture.request()
            next_grant = only_admitted(server.handle_request(next_request))
            _ = server.handle_request(wire.ReleaseRequest("cached-release", next_grant.reservation, 0))
            assert_full_state()
            _ = server.handle_request(wire.PriorityClearRequest("cached-priority", "fixture", why="clear words"))
            assert_full_state()

    def test_unchanged_persist_does_not_encode_durable_records_again(self) -> None:
        with AdmissionFixture() as fixture:
            _ = fixture.server.handle_request(fixture.request())
            ledger = tuple(fixture.server.ledger.values())
            encode = wire.encode_record
            seen: list[object] = []

            def observe(value: object) -> str:
                seen.append(value)
                return encode(value)

            with mock.patch.object(wire, "encode_record", side_effect=observe):
                fixture.server.persist()
            self.assertFalse(seen)
            self.assertEqual(tuple(fixture.server.ledger.values()), ledger)


class RunnerAuthorizationTests(unittest.TestCase):
    def test_imported_mutations_validate_shared_state_and_each_reservation_path(self) -> None:
        operations = ("construction", "restart", "registration", "persist", "admission", "cancellation", "release", "token return",
                      "priority", "finalization boundary", "finalization save", "finalization publish",
                      "finalization clear", "export")
        for operation in operations:
            entry = operation in ("construction", "restart", "registration", "export", "finalization boundary",
                                  "finalization save", "finalization publish", "finalization clear")
            for access in (("private", "shared", "mixed reservation", "mixed sidecar") if entry else ("private",)):
                with self.subTest(operation=operation, access=access), AdmissionFixture() as fixture:
                    request = fixture.request(step="build")
                    grant = only_admitted(fixture.server.handle_request(request))
                    home = fixture.root / "guard-home"
                    state = home / ".local/state"
                    state.mkdir(parents=True)
                    shared_log = fixture.root / "shared-log"
                    shared_log.mkdir()
                    (state / "buildlog").symlink_to(
                        fixture.state_dir.parent if access == "shared" else shared_log, target_is_directory=True)
                    marker = shared_log / "reservation"
                    _ = marker.write_bytes(b"saved reservation\n")
                    if access.startswith("mixed"):
                        record = fixture.server.reservations[request.episode]
                        fixture.server.reservations[request.episode] = (
                            replace(record, path=str(marker)) if access == "mixed reservation"
                            else replace(record, sidecar=str(marker)))
                        fixture.server.persist()
                    record = fixture.server.reservations[request.episode]
                    intent: records.FinalizationIntent = {
                        "reservation_id": grant.reservation, "peak": GIB, "scoped": False,
                        "outcome": "completed", "ended_at": datetime.fromtimestamp(fixture.clock.now(), UTC).isoformat(),
                    }
                    def advance_generation() -> None:
                        fixture.server.build_history_generation += 1
                        fixture.server.persist()

                    intents = service.StateIntents(fixture.server.reservations, advance_generation,
                        request.episode, fixture.server.persist, fixture.fault, fixture.server.places,
                        service.ServiceStateWrites(fixture.state_dir))
                    next_request = fixture.request()
                    fixture.allow_next_start()
                    peer: SocketPeer | service.NoConnection = service.NO_CONNECTION
                    if operation == "cancellation":
                        fixture.free(0)
                        fixture.hold_starts()
                        peer = fixture.connect(next_request)
                        self.assertIsInstance(peer.replies()[0], wire.Wait)
                    if operation in ("construction", "restart", "export"):
                        fixture.server.close()
                    if operation == "export":
                        fixture.proc.kill(request.owner)

                    def mutate() -> None:
                        if operation == "construction":
                            server = service.AdmissionServer(fixture.state_dir, fixture.clock, fixture.readings,
                                fixture.proc, socket_path=fixture.socket_path, host_probe=fixture.read_provider)
                            try:
                                self.assertIn(request.episode, server.reservations)
                            finally:
                                server.close()
                        elif operation == "restart":
                            fixture.restart()
                            self.assertIn(request.episode, fixture.server.reservations)
                        elif operation == "registration":
                            fixture.server.writes.register(record)
                        elif operation == "persist":
                            fixture.server.lifecycle_outbox = (service.PendingLeft(
                                gate.queue_life_identity(record.queue_waiter), "cancelled-waiter", "cancelled",
                                fixture.clock.now()),)
                            fixture.server.persist()
                            self.assertEqual(fixture.server.lifecycle_outbox, ())
                            fixture.server.persist()
                            self.assertEqual(service.read_state(fixture.state_dir).lifecycle_outbox, ())
                            cancelled = [row for row in step_estimator.anonymous_history_rows(
                                fixture.state_dir / "queue_times.jsonl") if row.get("kind") == "left"]
                            self.assertEqual([(row.get("reason"), row.get("ended_at")) for row in cancelled],
                                             [("cancelled", fixture.clock.now())])
                        elif operation == "admission":
                            accepted = only_admitted(fixture.server.handle_request(next_request))
                            self.assertEqual(service.read_state(fixture.state_dir).reservations[
                                next_request.episode].reservation_id, accepted.reservation)
                        elif operation == "cancellation":
                            assert isinstance(peer, SocketPeer)
                            peer.close()
                            fixture.server.run_once(0)
                            self.assertNotIn(next_request.episode, fixture.server.waiters)
                            self.assertTrue(any(row.get("kind") == "left" and row.get("reason") == "cancelled"
                                                for row in step_estimator.anonymous_history_rows(fixture.state_dir / "queue_times.jsonl")))
                        elif operation in ("release", "token return"):
                            ending = (wire.ReleaseRequest("guard-release", grant.reservation, 0)
                                      if operation == "release" else wire.TokenWaitRequest("guard-token", grant.reservation))
                            self.assertEqual(fixture.server.handle_request(ending), [wire.Ok()])
                            self.assertNotIn(request.episode, service.read_state(fixture.state_dir).reservations)
                        elif operation == "priority":
                            self.assertEqual(fixture.completed_queue_change(
                                wire.PriorityClearRequest("guard-priority", "fixture", why="clear words")), [wire.Ok()])
                        elif operation == "finalization boundary":
                            boundary = service.StateIntents(fixture.server.reservations, advance_generation,
                                request.episode, fixture.server.persist, fixture.fault, fixture.server.places,
                                service.ServiceStateWrites(fixture.state_dir))
                            boundary.save(service.reservation_row(record), intent)
                            self.assertEqual(service.read_state(fixture.state_dir).reservations[
                                request.episode].finalization, intent)
                        elif operation == "finalization save":
                            intents.save(service.reservation_row(record), intent)
                            self.assertEqual(service.read_state(fixture.state_dir).reservations[
                                request.episode].finalization, intent)
                        elif operation == "finalization publish":
                            intents.published(service.reservation_row(record))
                            self.assertEqual(service.read_state(fixture.state_dir).build_history_generation, 1)
                        elif operation == "finalization clear":
                            intents.clear(service.reservation_row(record))
                            self.assertNotIn(request.episode, service.read_state(fixture.state_dir).reservations)
                        else:
                            with mock.patch.object(service, "SystemProc", return_value=fixture.proc):
                                service.export_priority(fixture.state_dir)
                            self.assertFalse((fixture.state_dir / "state.json").exists())

                    def saved_files() -> dict[Path, bytes]:
                        return {path.relative_to(fixture.root): path.read_bytes()
                                for path in fixture.root.rglob("*") if path.is_file() and not path.is_symlink()}

                    before = saved_files()
                    with mock.patch.object(override_guard, "user_home", return_value=home):
                        if access == "private":
                            mutate()
                        else:
                            with self.assertRaises(override_guard.SharedStateWriteRefused) as raised:
                                mutate()
                            self.assertIn("build_queue/admission_service.py", str(raised.exception))
                            self.assertEqual(saved_files(), before)

        with AdmissionFixture() as fixture:
            calls: list[tuple[Path, ...]] = []

            def checked(_script: Path, _relative: str, *paths: Path) -> None:
                calls.append(paths)

            request = fixture.service_request()
            with mock.patch.object(override_guard, "require_shared_state_write", side_effect=checked):
                grant = only_admitted(fixture.server.handle_request(request))
                record = fixture.server.reservations[request.episode]
                path = Path(record.path)
                self.assertEqual(sum(path in paths for paths in calls), 1)
                self.assertTrue(any(path.with_suffix(".max") in paths for paths in calls))
                self.assertTrue(any(path.with_suffix(".wave") in paths for paths in calls))
                self.assertTrue(any(Path(record.sidecar) in paths for paths in calls))
                calls.clear()
                fixture.restart()
                self.assertEqual(sum(path in paths for paths in calls), 1)
                self.assertEqual(fixture.server.reservations[request.episode].reservation_id, grant.reservation)
                calls.clear()
                for _ in range(3):
                    self.assertIsInstance(fixture.server.handle_request(wire.BuildHistoryRequest())[0], wire.BuildHistoryReply)
                    _ = fixture.server.run_pass()
                    fixture.server.persist()
                self.assertEqual(calls, [])

        with AdmissionFixture() as fixture:
            requests: list[wire.ServiceEstimateRequest] = []
            grants: list[wire.Admitted] = []
            for _ in range(3):
                fixture.allow_next_start()
                request = fixture.service_request()
                requests.append(request)
                grants.append(only_admitted(fixture.server.handle_request(request)))
            selected = fixture.server.reservations[requests[0].episode]
            unrelated = [fixture.server.reservations[request.episode] for request in requests[1:]]
            calls = []
            with mock.patch.object(override_guard, "require_shared_state_write", side_effect=checked):
                self.assertEqual(fixture.server.handle_request(
                    wire.ReleaseRequest("one-registered-release", grants[0].reservation, 0)), [wire.Ok()])
            self.assertTrue(any(Path(selected.path) in paths for paths in calls))
            self.assertTrue(any(Path(selected.sidecar) in paths for paths in calls))
            for record in unrelated:
                self.assertFalse(any(Path(record.path) in paths or Path(record.sidecar) in paths for paths in calls))
                self.assertIn(record.episode, fixture.server.reservations)

            home = fixture.root / "direct-guard-home"
            shared = home / ".local/state/buildlog/reservation"
            shared.parent.mkdir(parents=True)
            _ = shared.write_bytes(b"unrelated shared reservation\n")
            record = unrelated[-1]
            fixture.server.reservations[record.episode] = replace(record, path=str(shared))
            saved = (fixture.state_dir / "state.json").read_bytes()
            with mock.patch.object(override_guard, "user_home", return_value=home), \
                 self.assertRaises(override_guard.SharedStateWriteRefused):
                _ = service.StateIntents(fixture.server.reservations, lambda: None, unrelated[0].episode,
                    fixture.server.persist, fixture.fault, fixture.server.places,
                    service.ServiceStateWrites(fixture.state_dir))
            self.assertEqual(shared.read_bytes(), b"unrelated shared reservation\n")
            self.assertEqual((fixture.state_dir / "state.json").read_bytes(), saved)

    def test_refused_socket_request_keeps_saved_versions_and_state_unchanged(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.persist()
            saved = fixture.state_dir / "state.json"
            before = saved.read_bytes()
            versions = dict(fixture.server.caller_versions)
            forbidden = wire.PrioritySetRequest("runner-priority", "unit", "why", "runner",
                code_version="runner-version")
            with fixture.peer_account(os.getuid() + 10000):
                reply, = fixture.connect(forbidden).replies()
            self.assertIsInstance(reply, wire.Refused)
            self.assertEqual(fixture.server.caller_versions, versions)
            self.assertEqual(saved.read_bytes(), before)
            allowed = wire.BuildHistoryRequest(code_version="allowed-version")
            reply, = fixture.connect(allowed).replies()
            self.assertIsInstance(reply, wire.BuildHistoryReply)
            start = records.process_start_time(os.getpid())
            self.assertEqual(fixture.server.caller_versions[records.run_owner(os.getpid(), str(start))], "allowed-version")
            self.assertEqual(service.read_state(fixture.state_dir).caller_versions, fixture.server.caller_versions)

    def test_ci_estimates_reject_incomplete_label_templates(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            for template, valid in (("CI {runner} {job}", "CI runner-a compile"),
                                    ("CI {runner} ({job})", "CI runner-a (compile)")):
                write_memory_config(config, ci_label=template)
                for job, expected in (("compile", valid), ("—", "hana CI"), ("", "hana CI"), ("  ", "hana CI")):
                    with self.subTest(template=template, job=job):
                        fixture.allow_next_start()
                        request = fixture.service_request(job=job)
                        grant = only_admitted(fixture.connect(request).replies())
                        record = fixture.server.reservations[request.episode]
                        fixture.release(grant)
                        self.assertEqual(record.queue_waiter.get("session_name"), expected)

    def test_invalid_ci_label_uses_committed_configuration_and_keeps_serving(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, ci_label="CI {runner} {job}")
            commit_config_fixture(config)
            for label in ("CI {unknown}", "CI {", "CI {}", "CI {runner.missing}"):
                with self.subTest(label=label):
                    write_memory_config(config, ci_label=label)
                    resolved = records.read_memory_config(report=False)
                    self.assertEqual(resolved.ci_label, "CI {runner} {job}")
                    self.assertTrue(any("ci_label" in item.diagnostic for item in resolved.diagnostics))
                    fixture.allow_next_start()
                    request = fixture.service_request()
                    grant = only_admitted(fixture.connect(request).replies())
                    self.assertEqual(fixture.server.reservations[request.episode].queue_waiter.get("session_name", ""),
                        f"CI {request.runner} {request.job}")
                    self.assertIsInstance(fixture.connect(wire.BuildHistoryRequest()).replies()[0], wire.BuildHistoryReply)
                    fixture.release(grant)

    def test_release_completes_when_the_runner_owns_a_read_only_sample_folder(self) -> None:
        with AdmissionFixture() as fixture:
            folder = fixture.root / "runner-samples"
            folder.mkdir()
            sidecar = folder / "cargo.cgroup"
            _ = sidecar.write_text("")
            samples = (progress.progress_path(sidecar, "json"), progress.progress_path(sidecar, "log"), sidecar)
            for sample in samples[:-1]:
                _ = sample.write_text("")
            request = replace(fixture.service_request(), sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant)
            folder.chmod(0o555)
            try:
                reply, = fixture.server.handle_request(wire.ReleaseRequest("read-only-release", grant.reservation, 0))
                self.assertEqual(reply, wire.Ok())
                self.assertFalse(fixture.server.reservations)
                row, = fixture.history()
                self.assertEqual(row["status"], 0)
                self.assertTrue(all(sample.is_file() for sample in samples))
            finally:
                folder.chmod(0o755)

    def test_a_ci_step_waits_during_a_natedev_hold_and_starts_after_the_release(self) -> None:
        # CI run 43737288332 refused four steps 60 s into the fps hold on 2026-11-15.
        held = records.NatedevHeld("fps", "Phase 3 timing")
        for priority_source in ("existing", "request"):
            with self.subTest(priority_source=priority_source), mock.patch.object(store, "host_name", return_value="natedev"), AdmissionFixture() as fixture, fixture.loop_turn():
                directory = fixture.root / "holders"
                directory.mkdir()
                _ = (directory / "fps").write_text(json.dumps({"holder": "fps", "since": "2026-11-15T12:00:00+00:00",
                    "for": "Phase 3 timing", "release_eta": "unknown", "keep": []}))
                request = fixture.service_request()
                priority = records.QueuePriority(({"kind": "UnitPriorityEntry", "unit": request.runner, "why": "fixture",
                    "set_at": fixture.clock.now(), "set_by": "fixture", "asker": records.UnstaffedSession("fixture")},))
                if priority_source == "existing":
                    fixture.server.priority = priority
                waiting, = fixture.server.handle_request(request)
                assert isinstance(waiting, wire.Wait)
                self.assertEqual(waiting.reason, held)
                self.assertEqual(waiting.text, "natedev is held by fps for Phase 3 timing; waiting for release")
                if priority_source == "request":
                    with mock.patch.object(gate, "priority_labels", return_value={}):
                        self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                            "bump-ci-runner", request.runner, "fixture", "fixture")), [wire.Ok()])
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                step, = view.reading.view.waiting
                self.assertEqual(step.reason, held)
                snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
                assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
                self.assertEqual([row["pid"] for _, row in snapshot.inputs.ordered], [request.owner.pid])
                self.assertEqual(snapshot.inputs.priority, priority)
                self.assertEqual(list(snapshot.inputs.waiter_orders.values()), [records.PriorityUnit(request.runner)])
                (directory / "fps").unlink()
                fixture.allow_next_start()
                fixture.release(only_admitted(fixture.server.handle_request(request)))

    def test_a_waiter_that_joined_before_a_drain_waits_in_the_hold_and_starts_first_at_the_release(self) -> None:
        # fps drain 2026-11-22 17:33: a smoke step that joined at 14:12 started in the hold, and the enforcer ended it.
        from scripts.build_hold import build_hold

        with mock.patch.object(store, "host_name", return_value="natedev"), AdmissionFixture() as fixture, fixture.loop_turn():
            directory = fixture.root / "holders"
            directory.mkdir()
            fixture.hold_starts()
            older = fixture.request(since=fixture.clock.now() - 20)
            newer = fixture.request(since=fixture.clock.now() - 10)
            for request in (older, newer):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            kept = fixture.request(owner=fixture.fixture_owner(4100, "41000"))
            write_process(fixture.proc.path, 4100, start="41000")
            _ = (directory / "fps").write_text(json.dumps({
                "holder": "fps", "since": "2026-11-22T22:33:19+00:00", "for": "a traced timeline",
                "release_eta": "unknown", "keep": [{"pid": 4100, "start_time": "41000"}]}))
            write_pressure_reading(fixture.root / "pressure")
            fixture.free(9 * GIB)
            with mock.patch.object(build_hold, "PROC_ROOT", fixture.proc.path):
                fixture.allow_next_start()
                kept_grant = only_admitted(fixture.server.handle_request(kept))
                fixture.allow_next_start()
                _ = fixture.server.run_pass()
                self.assertEqual(set(fixture.server.reservations), {kept.episode})
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                held = records.NatedevHeld("fps", "a traced timeline")
                self.assertEqual([(step.pid, step.reason) for step in view.reading.view.waiting],
                                 [(older.owner.pid, held), (newer.owner.pid, held)])
                fixture.release(kept_grant)
                (directory / "fps").unlink()
                fixture.allow_next_start()
                _ = fixture.server.run_pass()
            self.assertIn(older.episode, fixture.server.reservations)
            self.assertIn(newer.episode, fixture.server.waiters)

    def test_a_step_of_the_holders_own_run_starts_in_the_hold(self) -> None:
        # A worker in the run has its own session; the run's owning session holds natedev.
        with mock.patch.object(store, "host_name", return_value="natedev"), AdmissionFixture() as fixture, fixture.loop_turn():
            directory = fixture.root / "holders"
            directory.mkdir()
            fixture.hold_starts()
            worker = replace(fixture.request(), session=OwningSession("fps-run"))
            self.assertIsInstance(fixture.server.handle_request(worker)[0], wire.Wait)
            self.assertNotEqual(fixture.server.waiters[worker.episode].row.get("session_id"), "fps-run")
            _ = (directory / "fps-run").write_text(json.dumps({
                "holder": "fps-run", "since": "2026-11-22T22:33:19+00:00", "for": "a traced timeline",
                "release_eta": "unknown", "keep": []}))
            write_pressure_reading(fixture.root / "pressure")
            fixture.free(9 * GIB)
            fixture.allow_next_start()
            _ = fixture.server.run_pass()
            self.assertIn(worker.episode, fixture.server.reservations)

    def test_a_held_step_gives_back_its_runs_slot_between_steps(self) -> None:
        with mock.patch.object(store, "host_name", return_value="natedev"), AdmissionFixture() as fixture:
            directory = fixture.root / "holders"
            directory.mkdir()
            run = fixture.run_owner()
            first = only_admitted(fixture.server.handle_request(fixture.request(run=run, need=6 * GIB)))
            fixture.allow_next_start()
            other = only_admitted(fixture.server.handle_request(fixture.request(run=fixture.run_owner(), need=GIB)))
            fixture.release(first)
            fixture.release(other)
            fixture.free(11 * GIB)
            head = fixture.request(run=run, need=16 * GIB)
            self.assertIsInstance(fixture.server.handle_request(head)[0], wire.Wait)
            self.assertIsInstance(fixture.server.slots[first.reservation], records.ContinuationSlot)
            _ = (directory / "fps").write_text(json.dumps({
                "holder": "fps", "since": "2026-11-22T22:33:19+00:00", "for": "a traced timeline",
                "release_eta": "unknown", "keep": []}))
            _ = fixture.run_pass()
            self.assertIsInstance(fixture.server.slots[first.reservation], records.BetweenStepsSlot)
            self.assertIsInstance(fixture.server.waiters[head.episode].continuation_claim, service.NoContinuationClaim)

    def test_a_plain_step_starts_while_the_hold_state_is_unreadable(self) -> None:
        # The enforcer stays the guard; a ReadingUnavailable wait would end the step at the client.
        for host in ("natedev", "mac"):
            with (self.subTest(host=host), mock.patch.object(store, "host_name", return_value=host),
                  AdmissionFixture() as fixture, fixture.loop_turn()):
                _ = (fixture.root / "holders").write_text("not a folder")
                fixture.free(9 * GIB)
                fixture.allow_next_start()
                _ = only_admitted(fixture.server.handle_request(fixture.request()))

    def test_a_mac_service_does_not_hold_for_a_natedev_hold(self) -> None:
        with mock.patch.object(store, "host_name", return_value="mac"), AdmissionFixture() as fixture, fixture.loop_turn():
            directory = fixture.root / "holders"
            directory.mkdir()
            _ = (directory / "fps").write_text(json.dumps({
                "holder": "fps", "since": "2026-11-22T22:33:19+00:00", "for": "a traced timeline",
                "release_eta": "unknown", "keep": []}))
            fixture.free(9 * GIB)
            fixture.allow_next_start()
            _ = only_admitted(fixture.server.handle_request(fixture.request()))

    def test_an_unreadable_staff_record_holds_the_step_and_the_pass_goes_on(self) -> None:
        from scripts.build_targets import build_targets

        with (mock.patch.object(store, "host_name", return_value="natedev"), AdmissionFixture() as fixture,
              fixture.loop_turn(), mock.patch.object(build_targets, "own_hold_key", side_effect=OSError("staff record"))):
            directory = fixture.root / "holders"
            directory.mkdir()
            _ = (directory / "fps-run").write_text(json.dumps({
                "holder": "fps-run", "since": "2026-11-22T22:33:19+00:00", "for": "a traced timeline",
                "release_eta": "unknown", "keep": []}))
            fixture.free(9 * GIB)
            fixture.allow_next_start()
            step = replace(fixture.request(), session=OwningSession("fps-run"))
            reply, = fixture.server.handle_request(step)
            self.assertIsInstance(reply, wire.Wait)
            self.assertEqual(fixture.server.local_holds[step.episode], records.NatedevHeld("fps-run", "a traced timeline"))

    def test_no_hold_in_force_reads_no_process_lineage(self) -> None:
        from scripts.build_targets import build_targets

        with (mock.patch.object(store, "host_name", return_value="natedev"), AdmissionFixture() as fixture,
              fixture.loop_turn(), mock.patch.object(build_targets, "read_caller_lineage",
                                                     side_effect=AssertionError("lineage read with no hold"))):
            (fixture.root / "holders").mkdir()
            fixture.hold_starts()
            self.assertIsInstance(fixture.server.handle_request(fixture.request())[0], wire.Wait)
            write_pressure_reading(fixture.root / "pressure")
            fixture.free(9 * GIB)
            fixture.allow_next_start()
            _ = fixture.server.run_pass()
            self.assertTrue(fixture.server.reservations)

    def test_service_estimate_refreshes_history_and_replays_without_a_caller_run(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            fixture.configure(**{"program_first_peak_gib.cargo": 4})
            first = fixture.service_request()
            first_grant = only_admitted(fixture.server.handle_request(first))
            self.assertIsInstance(fixture.server.reservations[first.episode].run, wire.NoRun)
            fixture.allow_next_start()
            fixture.free(7 * GIB)
            second = fixture.service_request()
            waiting, = fixture.server.handle_request(second)
            self.assertIsInstance(waiting, wire.Wait)
            before = fixture.server.waiters[second.episode].row.copy()
            generation = fixture.server.build_history_generation
            fixture.scoped_peak(first_grant, 16 * GIB)
            self.assertEqual(fixture.server.handle_request(
                wire.ReleaseRequest("failed-runner", first_grant.reservation, 137)), [wire.Ok()])
            self.assertFalse(fixture.server.slots)
            self.assertGreater(fixture.server.build_history_generation, generation)
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            second_grant = only_admitted(fixture.server.handle_request(second))
            self.assertGreater(second_grant.need, first_grant.need)
            record = fixture.server.reservations[second.episode]
            self.assertEqual(record.queue_waiter["since"], before["since"])
            fixture.restart()
            self.assertEqual(only_admitted(fixture.server.handle_request(second)), second_grant)
            self.assertEqual(set(fixture.server.reservations), {second.episode})
            fixture.release(second_grant)

    def test_runner_counter_selects_the_history_window_instead_of_the_full_need(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            uid = os.getuid() + 10000
            owner = fixture.account_owner(uid)
            write_process(fixture.proc.path, owner.pid, 2 * GIB, start=owner.start_time)
            status = fixture.proc.path / str(owner.pid) / "status"
            _ = status.write_text(status.read_text() + f"Uid: {uid} {uid} {uid} {uid}\n")
            sidecar = fixture.root / "runner-counter/cargo.cgroup"
            sidecar.parent.mkdir(mode=0o755)
            _ = sidecar.write_text("")
            sidecar.chmod(0o644)
            request = replace(fixture.service_request(owner=owner), episode=f"uid:{uid}:progress", sidecar=str(sidecar))
            with fixture.peer_account(uid):
                grant = only_admitted(fixture.server.handle_request(request))
            record = fixture.server.reservations[request.episode]
            row = service.reservation_row(record)
            key = gate.reservation_profile_key(row, fixture.server.host)
            counter = progress.ProgressCounter("build:1", 40, 100)
            progress.publish_counter(sidecar, counter)
            _ = fixture.run_pass()
            without_profile, = fixture.server.handle_request(wire.ViewRequest())
            self.assertIsInstance(without_profile, wire.ViewReply)
            if isinstance(without_profile, wire.ViewReply):
                room = without_profile.reading.explanation.room
                self.assertIsInstance(room, records.MemoryRoom)
                if isinstance(room, records.MemoryRoom):
                    hold, = room.runs
                    self.assertEqual((hold.memory.need, hold.bytes), (grant.need, grant.need - 2 * GIB))
            progress.progress_path(sidecar, "json").unlink()
            for done in range(0, 100, 5):
                fixture.server.progress_history.observe("previous-ci", key,
                    progress.ProgressCounter("build:1", done, 100), 6 * GIB, fixture.clock.now())
            fixture.server.progress_history.finish("previous-ci", fixture.clock.now(), fixture.clock.now() - 86400)
            _ = fixture.run_pass()
            without_counter, = fixture.server.handle_request(wire.ViewRequest())
            self.assertIsInstance(without_counter, wire.ViewReply)
            if isinstance(without_counter, wire.ViewReply):
                room = without_counter.reading.explanation.room
                self.assertIsInstance(room, records.MemoryRoom)
                if isinstance(room, records.MemoryRoom):
                    hold, = room.runs
                    self.assertEqual((hold.memory.need, hold.bytes), (grant.need, grant.need - 2 * GIB))
            progress.publish_counter(sidecar, counter)
            _ = fixture.run_pass()
            reply, = fixture.server.handle_request(wire.ViewRequest())
            self.assertIsInstance(reply, wire.ViewReply)
            if isinstance(reply, wire.ViewReply):
                room = reply.reading.explanation.room
                self.assertIsInstance(room, records.MemoryRoom)
                if isinstance(room, records.MemoryRoom):
                    hold, = room.runs
                    self.assertEqual(hold.memory.use, records.AnonymousRunUse(2 * GIB))
                    self.assertEqual(hold.memory.need, 6 * GIB)
                    self.assertEqual(hold.bytes, 4 * GIB)
                    self.assertLess(hold.bytes, grant.need - 2 * GIB)
                measured = reply.reading.view.running[0].measured
                self.assertIsInstance(measured, records.ProgressAdjustedMemory)
                if isinstance(measured, records.ProgressAdjustedMemory):
                    self.assertEqual(measured.counter, counter)
            fixture.release(grant)

    def test_a_token_wait_in_the_step_log_is_the_step_counter(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            uid = os.getuid() + 10000
            owner = fixture.account_owner(uid)
            write_process(fixture.proc.path, owner.pid, 2 * GIB, start=owner.start_time)
            status = fixture.proc.path / str(owner.pid) / "status"
            _ = status.write_text(status.read_text() + f"Uid: {uid} {uid} {uid} {uid}\n")
            sidecar = fixture.root / "runner-wait/cargo.cgroup"
            sidecar.parent.mkdir(mode=0o755)
            _ = sidecar.write_text("")
            _ = progress.progress_path(sidecar, "log").write_text(
                "Finished `test` profile in 29s\n" + progress.token_wait_line(4) + "\n")
            request = replace(fixture.service_request(owner=owner), episode=f"uid:{uid}:wait", sidecar=str(sidecar))
            with fixture.peer_account(uid):
                grant = only_admitted(fixture.server.handle_request(request))
            # The capture fallback runs only with a boot identity.
            fixture.server.boot_id = "fixture-boot"
            _ = fixture.run_pass()
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            measured = reply.reading.view.running[0].measured
            assert isinstance(measured, records.ProgressAdjustedMemory)
            self.assertEqual(measured.counter, progress.NoCounter(progress.TOKEN_WAIT_REASON, progress.TokenWait(4)))
            fixture.release(grant)

    def test_runner_cannot_change_priority(self) -> None:
        with AdmissionFixture() as fixture, fixture.peer_account(os.getuid() + 10000):
            for request in (wire.PrioritySetRequest("set", "unit", "why", "director"),
                            wire.PriorityClearRequest("clear", "director", why="clear words")):
                with self.subTest(request=type(request).__name__):
                    replies = fixture.completed_queue_change(request)
                    self.assertEqual(len(replies), 1)
                    self.assertIsInstance(replies[0], wire.Refused)
                    self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)

    def test_runner_cannot_use_another_accounts_owner_run_or_replay(self) -> None:
        with AdmissionFixture() as fixture:
            uid = os.getuid() + 10000
            owner = fixture.account_owner(uid)
            foreign = fixture.account_owner(uid + 1)
            request = replace(fixture.request(owner=owner), episode=f"uid:{uid}:own")
            grant = only_admitted(fixture.server.handle_request(request))
            run = wire.RunIdentity(foreign.pid, foreign.start_time)
            attempts: tuple[wire.Request, ...] = (
                replace(request, episode=f"uid:{uid}:foreign-owner", owner=foreign),
                replace(request, episode=f"uid:{uid}:foreign-run", owner=fixture.account_owner(uid), run=run),
                request,
                wire.ReleaseRequest(f"uid:{uid + 1}:release", grant.reservation, 137),
                wire.WidthRequest(f"uid:{uid + 1}:width", grant.reservation, 1),
                wire.TokenWaitRequest(f"uid:{uid + 1}:token", grant.reservation),
            )
            for index, attempt in enumerate(attempts):
                peer_uid = uid if index < 2 else uid + 1
                with self.subTest(request=attempt), fixture.peer_account(peer_uid), fixture.loop_turn():
                    reply, = fixture.server.handle_request(attempt)
                    self.assertIsInstance(reply, wire.Refused)
                    self.assertEqual(set(fixture.server.reservations), {request.episode})
                    self.assertFalse(fixture.server.waiters)
            self.assertEqual(fixture.server.reservations[request.episode].test_width, request.test_width)
            fixture.release(grant)

    def test_runner_owner_identity_reads_the_private_process_start_and_account(self) -> None:
        with AdmissionFixture() as fixture:
            uid = os.getuid() + 10000
            owner = fixture.account_owner(uid)
            request = replace(fixture.request(owner=owner), episode=f"uid:{uid}:start")
            with fixture.peer_account(uid):
                granted = only_admitted(fixture.connect(request).replies())
            self.assertEqual(service.process_uid(owner.pid, fixture.proc.path), uid)
            self.assertEqual(fixture.proc.owner_start(owner.pid), service.LiveOwnerIdentity(owner.start_time))
            self.assertEqual(fixture.server.reservations[request.episode].owner, owner)
            with fixture.peer_account(uid + 1):
                refused, = fixture.connect(replace(request, owner=replace(owner, start_time="reused"))).replies()
            self.assertIsInstance(refused, wire.Refused)
            fixture.release(granted)


class WaitHolderServiceTests(unittest.TestCase):
    def test_the_wait_socket_receives_the_holder_from_the_grouped_pass(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            for need, session, owner_run in ((5, "group", run), (5, "group", run), (8, "single", wire.NO_RUN)):
                fixture.allow_next_start()
                request = fixture.request(need=need * GIB, run=owner_run)
                request = replace(request, identity=replace(request.identity, session_id=session, name=session))
                with fixture.loop_turn():
                    _ = only_admitted(fixture.server.handle_request(request))
            fixture.allow_next_start()
            fixture.free(4 * GIB)
            request = fixture.request(need=20 * GIB)
            with fixture.loop_turn():
                peer = fixture.connect(request)
                replies = peer.replies()
                waits = [reply for reply in replies if isinstance(reply, wire.Wait)]
                self.assertTrue(waits)
                self.assertEqual(waits[-1].snapshot.holder, records.SessionHolder("group", "group"))
                result = fixture.server.last_result
                assert isinstance(result, gate.PassResult)
                row = next(row for row in result.rows if row.owner == records.run_owner(
                    request.owner.pid, request.owner.start_time))
                self.assertEqual(waits[-1].snapshot.holder, row.holder)
                self.assertEqual(wire.parse_reply(wire.encode_reply(waits[-1])), waits[-1])

    def test_a_released_steps_held_slot_keeps_its_holder_identity(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            _ = (fixture.proc.path / str(run.pid) / "environ").write_bytes(
                b"CLAUDE_CODE_SESSION_ID=gap-session\0CLAUDE_SESSION_NAME=gap-worker\0")
            fixture.allow_next_start()
            request = fixture.request(need=12 * GIB, run=run)
            request = replace(request, identity=replace(request.identity, session_id="gap-session", name="gap-worker"))
            with fixture.loop_turn():
                granted = only_admitted(fixture.server.handle_request(request))
                fixture.allow_next_start()
                fixture.release(granted)
                self.assertFalse(fixture.server.reservations)
            fixture.allow_next_start()
            other = fixture.request(need=8 * GIB)
            other = replace(other, identity=replace(other.identity, session_id="other", name="other"))
            with fixture.loop_turn():
                _ = only_admitted(fixture.server.handle_request(other))
            fixture.allow_next_start()
            fixture.free(4 * GIB)
            with fixture.loop_turn():
                peer = fixture.connect(fixture.request(need=20 * GIB))
                waits = [reply for reply in peer.replies() if isinstance(reply, wire.Wait)]
                self.assertTrue(waits)
                self.assertEqual(waits[-1].snapshot.holder, records.SessionHolder("gap-session", "gap-worker"))

    def test_gate_hold_keeps_the_admitted_job_after_restart_and_command_loss(self) -> None:
        job = StepJob("test-all")
        for source, expected in (("request", "test-all"), ("process", "test-all"),
                                 ("process with invalid stat", "test-all"), ("old record", "gate")):
            with self.subTest(source=source), AdmissionFixture() as fixture:
                run = fixture.run_owner()
                request = fixture.request(step="gate", run=run)
                cmdline = fixture.proc.path / str(request.owner.pid) / "cmdline"
                argv = ("python3", "copies.py", "run", "--repo", str(fixture.root), "--",
                        "python3", "gate.py", "--tests-only", "--full")
                if source == "request":
                    request = replace(request, identity=replace(request.identity, job=job))
                else:
                    _ = cmdline.write_bytes(b"\0".join(map(os.fsencode, argv)) + b"\0")
                if source == "process with invalid stat":
                    stat = cmdline.with_name("stat")
                    name, separator, fields = stat.read_bytes().partition(b") ")
                    _ = stat.write_bytes(name + b"\xff" + separator + fields)
                with fixture.loop_turn():
                    granted = only_admitted(fixture.server.handle_request(request))
                cmdline.unlink(missing_ok=True)
                if source == "old record":
                    path = fixture.state_dir / "state.json"
                    state = cast(dict[str, object], json.loads(path.read_text()))
                    reservations = cast(dict[str, dict[str, object]], state["reservations"])
                    del reservations[request.episode]["gate_job"]
                    _ = path.write_text(json.dumps(state))
                fixture.restart()
                _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                running, = [step for step in reply.reading.view.running if step.pid == request.owner.pid]
                expected_job = GateJobNotRecorded() if source == "old record" else job
                self.assertEqual(running.job, expected_job)
                fixture.release(granted)
                hold, = fixture.server.slots.values()
                assert isinstance(hold, records.BetweenStepsSlot)
                assert isinstance(hold.identity, records.EndedStepFacts)
                self.assertEqual(hold.identity.step, expected)
                self.assertEqual(hold.identity.job, expected_job)
                self.assertEqual(wire.parse_record(wire.encode_record(hold), records.BetweenStepsSlot), hold)

    def test_a_released_unit_steps_held_slot_keeps_its_unit_without_a_session(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            fixture.allow_next_start()
            request = fixture.request(need=12 * GIB, run=run)
            request = replace(request, identity=replace(request.identity, session_id="", name="named caller", unit="gap-unit"))
            with fixture.loop_turn():
                granted = only_admitted(fixture.server.handle_request(request))
            fixture.restart()
            with fixture.loop_turn():
                fixture.allow_next_start()
                fixture.release(granted)
                self.assertFalse(fixture.server.reservations)
            fixture.allow_next_start()
            other = fixture.request(need=8 * GIB)
            with fixture.loop_turn():
                _ = only_admitted(fixture.server.handle_request(other))
            fixture.allow_next_start()
            fixture.free(4 * GIB)
            with fixture.loop_turn():
                peer = fixture.connect(fixture.request(need=20 * GIB))
                waits = [reply for reply in peer.replies() if isinstance(reply, wire.Wait)]
                self.assertTrue(waits)
                self.assertEqual(waits[-1].snapshot.holder, records.UnitHolder("gap-unit"))

    def test_a_unit_holder_reaches_the_wait_socket_without_a_session(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.allow_next_start()
            request = fixture.request(need=12 * GIB)
            request = replace(request, identity=replace(request.identity, session_id="", name="named caller", unit="compile-unit"))
            with fixture.loop_turn():
                _ = only_admitted(fixture.server.handle_request(request))
            fixture.allow_next_start()
            fixture.free(4 * GIB)
            with fixture.loop_turn():
                peer = fixture.connect(fixture.request(need=20 * GIB))
                waits = [reply for reply in peer.replies() if isinstance(reply, wire.Wait)]
                self.assertTrue(waits)
                self.assertEqual(waits[-1].snapshot.holder, records.UnitHolder("compile-unit"))


class AdmissionCallerPersistenceTests(unittest.TestCase):

    def test_serving_rejects_direct_calls_without_loop_turn(self) -> None:
        with AdmissionFixture() as fixture, fixture.serving():
            methods: tuple[tuple[str, Callable[[], object]], ...] = (
                ("handle_request", lambda: fixture.server.handle_request(wire.ViewRequest())),
                ("persist", fixture.server.persist), ("run_pass", fixture.server.run_pass))
            for name, method in methods:
                with self.subTest(method=name):
                    with self.assertRaisesRegex(AssertionError, name + r".*use loop_turn\(\)"):
                        _ = method()
                    with fixture.loop_turn():
                        _ = method()

    def test_persistence_keeps_every_live_caller_version(self) -> None:
        with AdmissionFixture() as fixture:
            requests = [fixture.request(owner=fixture.fixture_owner(1000 + index, str(index)))
                        for index in range(64)]
            with fixture.loop_turn():
                for request in requests:
                    _ = fixture.server.handle_request(request)
                fixture.server.persist()
                expected = {f"{request.owner.pid}-{request.owner.start_time}": request.code_version
                            for request in requests}
                self.assertEqual(fixture.server.caller_versions, expected)
                self.assertEqual(service.read_state(fixture.state_dir).caller_versions, expected)


class PendingPriorityTests(unittest.TestCase):
    def test_priority_labels_use_recorded_units_without_reading_transcripts(self) -> None:
        from scripts.message import roster, unit_labels
        from scripts.production import staff

        producer = staff.StaffRow(staff.StaffId("producer"), "showrunner", "production", staff.StaffRoot(),
                                 current_session=staff.StaffId("producer-session"))
        director = staff.StaffRow(staff.StaffId("director"), "unit", "chosen", staff.ReportsTo(producer.id),
                                 current_session=staff.StaffId("director-session"))
        worker = staff.StaffRow(staff.StaffId("worker"), "worker", "builder", staff.ReportsTo(director.id),
                               current_session=staff.StaffId("worker-session"))
        snapshot = staff.StaffSnapshot((producer, director, worker))
        expected = {"director-session": UnitLabels("chosen", "production", "chosen", unit_labels.OwnerRole.UNIT, unit_labels.Runner.UNIT_DIRECTOR),
                    "worker-session": UnitLabels("builder", "production", "chosen", unit_labels.OwnerRole.UNIT, unit_labels.Runner.WORKER)}
        with mock.patch.object(staff, "snapshot", return_value=snapshot), \
                mock.patch.object(roster, "unit_labels_for", side_effect=AssertionError("transcript lookup")):
            self.assertEqual(gate.priority_labels(["producer-session", "director-session", "worker-session"]), expected)
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable), \
                    mock.patch.object(staff, "snapshot", side_effect=OSError("unreadable") if unreadable else None,
                                      return_value=snapshot), \
                    mock.patch.object(roster, "unit_labels_for", return_value={**expected,
                                      "unrecorded": UnitLabels("old", unit="other")}) as fallback:
                actual = gate.priority_labels(["director-session", "worker-session", "unrecorded"])
                self.assertEqual(actual, {**expected, "unrecorded": UnitLabels("old", unit="other")})
                fallback.assert_called_once_with(
                    ["director-session", "worker-session", "unrecorded"] if unreadable else ["unrecorded"], os.environ)

    def test_explicit_waiter_units_keep_order_without_a_roster_lookup(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            requests = [replace(fixture.request(), identity=wire.Identity("old", "older", "check", "", "other")),
                        replace(fixture.request(), identity=wire.Identity("new", "newer", "check", "", "chosen"))]
            for request in requests:
                _ = fixture.server.handle_request(request)
            fixture.server.priority = records.QueuePriority(({
                "unit": "chosen", "why": "urgent", "set_at": fixture.clock.now(), "set_by": "director"},))
            rows = [(fixture.state_dir / records.waiter_owner(waiting.row), waiting.row)
                    for waiting in fixture.server.waiters.values()]
            expected = gate.apply_queue_priority(rows, gate.QueueOrderContext(
                fixture.server.priority, {"old": UnitLabels("older", unit="other"),
                                          "new": UnitLabels("newer", unit="chosen")}))
            with mock.patch.object(gate, "priority_labels", side_effect=AssertionError("roster lookup")):
                _ = fixture.server.run_pass()
            assert isinstance(fixture.server.last_inputs, records.QueueInputs)
            self.assertEqual(fixture.server.last_inputs.ordered,
                             [(path, records.stored_waiter(row)) for path, row in expected[0]])

    def test_runner_retry_cannot_attach_to_a_pending_priority_request(self) -> None:
        with AdmissionFixture() as fixture, self.delayed_roster() as (entered, finish), fixture.serving():
            try:
                request = wire.PrioritySetRequest("pending-owner", "chosen", "urgent", "director")
                first = self.connect(fixture, request)
                self.assertTrue(entered.wait(timeout=3))
                with fixture.peer_account(os.getuid() + 10000):
                    reply, = fixture.completed_queue_change(request)
                self.assertIsInstance(reply, wire.Refused)
                with fixture.loop_turn():
                    pending = fixture.server.pending_priority
                    self.assertIsInstance(pending, service.PendingPriorityRoster)
                    if isinstance(pending, service.PendingPriorityRoster):
                        self.assertEqual(len(pending.change.connections), 1)
                finish.set()
                self.assertEqual(self.reply(first), wire.Ok())
                self.assertEqual(fixture.completed_queue_change(request), [wire.Ok()])
            finally:
                finish.set()

    def connect(self, fixture: AdmissionFixture, request: wire.Request) -> socket.socket:
        peer = self.enterContext(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        peer.connect(str(fixture.socket_path))
        peer.sendall(wire.encode_request(request).encode())
        return peer

    def reply(self, peer: socket.socket) -> wire.Reply:
        stream = self.enterContext(peer.makefile("rb"))
        return wire.parse_reply(stream.readline())

    @contextmanager
    def delayed_roster(self, session_id: str = "worker") -> Generator[tuple[threading.Event, threading.Event]]:
        entered, finish = threading.Event(), threading.Event()

        def read_roster(sessions: list[str] | gate.RosterUnits) -> dict[str, UnitLabels]:
            if isinstance(sessions, gate.RosterUnits):
                entered.set()
                _ = finish.wait()
            return {session_id: UnitLabels("worker", unit="chosen")}

        with mock.patch.object(gate, "priority_labels", side_effect=read_roster):
            try:
                yield entered, finish
            finally:
                finish.set()

    def test_query_and_release_reply_while_a_priority_roster_read_is_pending(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            with self.delayed_roster() as (entered, finish), fixture.serving():
                try:
                    priority = self.connect(fixture, wire.PrioritySetRequest("set", "chosen", "urgent", "director"))
                    self.assertTrue(entered.wait())
                    viewed = self.reply(self.connect(fixture, wire.ViewRequest()))
                    self.assertIsInstance(viewed, wire.ViewReply)
                    self.assertEqual(self.reply(self.connect(fixture,
                        wire.ReleaseRequest("release", grant.reservation, 0))), wire.Ok())
                    with fixture.loop_turn():
                        self.assertNotIn(request.episode, fixture.server.reservations)
                        self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
                    self.assertEqual(select.select([priority], [], [], 0)[0], [])
                    finish.set()
                    self.assertEqual(self.reply(priority), wire.Ok())
                    viewed = self.reply(self.connect(fixture, wire.ViewRequest()))
                    assert isinstance(viewed, wire.ViewReply)
                    applied = viewed.reading.explanation.priority
                    assert not isinstance(applied, records.NoQueuePriority)
                    self.assertEqual((applied.entries[-1]["unit"], applied.entries[-1]["why"], applied.entries[-1]["set_by"]),
                                     ("chosen", "urgent", "director"))
                finally:
                    finish.set()

    def test_clear_waits_for_a_pending_set_and_leaves_no_priority(self) -> None:
        with AdmissionFixture() as fixture, self.delayed_roster() as (entered, finish), fixture.serving():
            try:
                priority = self.connect(fixture, wire.PrioritySetRequest("set", "chosen", "urgent", "director"))
                self.assertTrue(entered.wait())
                cleared = self.connect(fixture, wire.PriorityClearRequest("clear", "director", why="clear words"))
                self.assertIsInstance(self.reply(self.connect(fixture, wire.ViewRequest())), wire.ViewReply)
                self.assertEqual(select.select([cleared], [], [], 0)[0], [])
                finish.set()
                self.assertEqual(self.reply(priority), wire.Ok())
                self.assertEqual(self.reply(cleared), wire.Ok())
                viewed = self.reply(self.connect(fixture, wire.ViewRequest()))
                assert isinstance(viewed, wire.ViewReply)
                self.assertIsInstance(viewed.reading.explanation.priority, records.NoQueuePriority)
                with fixture.loop_turn():
                    self.assertIsInstance(service.read_state(fixture.state_dir).priority, records.NoQueuePriority)
            finally:
                finish.set()

    def test_pending_priority_retry_gets_one_reply_and_records_one_transition(self) -> None:
        with AdmissionFixture() as fixture, self.delayed_roster() as (entered, finish), \
             mock.patch.object(buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged, \
             fixture.serving():
            try:
                request = wire.PrioritySetRequest("same-key", "chosen", "urgent", "director")
                first = self.connect(fixture, request)
                self.assertTrue(entered.wait())
                retry = self.connect(fixture, request)
                self.assertIsInstance(self.reply(self.connect(fixture, wire.ViewRequest())), wire.ViewReply)
                self.assertEqual(select.select([first, retry], [], [], 0)[0], [])
                finish.set()
                for peer in (first, retry):
                    self.assertEqual(self.reply(peer), wire.Ok())
                    self.assertEqual(peer.recv(1), b"")
                self.assertEqual(self.reply(self.connect(fixture, request)), wire.Ok())
                self.assertEqual(logged.call_count, 1)
                self.assertEqual(logged.call_args.kwargs["transition_id"], request.request_id)
            finally:
                finish.set()


    def test_restart_during_a_roster_read_keeps_priority_and_allows_the_same_key_retry(self) -> None:
        with AdmissionFixture() as fixture:
            labels = {"worker": UnitLabels("worker", unit="original")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                    "initial", "original", "keep", "director")), [wire.Ok()])
            with self.delayed_roster() as (entered, finish), fixture.serving():
                try:
                    request = wire.PrioritySetRequest("retry-after-restart", "chosen", "urgent", "director")
                    pending = self.connect(fixture, request)
                    self.assertTrue(entered.wait())
                    with fixture.loop_turn():
                        saved = service.read_state(fixture.state_dir)
                        assert not isinstance(saved.priority, records.NoQueuePriority)
                        self.assertEqual(saved.priority.entries[0]["unit"], "original")
                        self.assertNotIn(request.request_id, saved.ledger)
                        fixture.restart()
                        restored = fixture.server.priority
                        assert not isinstance(restored, records.NoQueuePriority)
                        self.assertEqual(restored.entries[0]["unit"], "original")
                    self.assertEqual(pending.recv(1), b"")
                    finish.set()
                    self.assertEqual(self.reply(self.connect(fixture, request)), wire.Ok())
                    viewed = self.reply(self.connect(fixture, wire.ViewRequest()))
                    assert isinstance(viewed, wire.ViewReply)
                    applied = viewed.reading.explanation.priority
                    assert not isinstance(applied, records.NoQueuePriority)
                    self.assertEqual(applied.entries[-1]["unit"], "chosen")
                finally:
                    finish.set()

    def test_roster_error_refuses_without_replay_and_allows_a_same_key_retry(self) -> None:
        with AdmissionFixture() as fixture:
            request = wire.PrioritySetRequest("roster-retry", "chosen", "urgent", "director")

            def unreadable_roster(sessions: list[str] | gate.RosterUnits) -> dict[str, UnitLabels]:
                if isinstance(sessions, gate.RosterUnits):
                    raise ValueError("roster unreadable")
                return {}

            with mock.patch.object(gate, "priority_labels", side_effect=unreadable_roster):
                self.assertEqual(fixture.completed_queue_change(request), [wire.Refused("roster unreadable")])
            self.assertIsInstance(fixture.completed_queue_change(wire.ViewRequest())[0], wire.ViewReply)
            self.assertNotIn(request.request_id, service.read_state(fixture.state_dir).ledger)
            with mock.patch.object(gate, "priority_labels", return_value={
                    "worker": UnitLabels("worker", unit="chosen")}):
                self.assertEqual(fixture.completed_queue_change(request), [wire.Ok()])
            priority = fixture.server.priority
            assert not isinstance(priority, records.NoQueuePriority)
            self.assertEqual(priority.entries[-1]["unit"], "chosen")

    def test_apply_errors_refuse_pending_and_queued_changes_and_allow_later_requests(self) -> None:
        with AdmissionFixture() as fixture, self.delayed_roster() as (entered, finish), fixture.serving():
            try:
                request = wire.PrioritySetRequest("apply-set", "chosen", "urgent", "director")
                clear = wire.PriorityClearRequest("apply-clear", "director", why="clear words")
                priority = self.connect(fixture, request)
                self.assertTrue(entered.wait())
                cleared = self.connect(fixture, clear)
                following = self.connect(fixture, wire.PriorityClearRequest("following", "director", "missing", why="clear words"))
                self.assertIsInstance(self.reply(self.connect(fixture, wire.ViewRequest())), wire.ViewReply)
                self.assertEqual(select.select([priority, cleared, following], [], [], 0)[0], [])
                with mock.patch.object(store, "read_last_whole_row_end", side_effect=ValueError("history unreadable")):
                    finish.set()
                    self.assertEqual(self.reply(priority), wire.Refused("history unreadable"))
                    self.assertEqual(self.reply(cleared), wire.Refused("history unreadable"))
                    self.assertEqual(self.reply(following), wire.Refused("unknown unit missing; the queue priority did not change"))
                self.assertIsInstance(self.reply(self.connect(fixture, wire.ViewRequest())), wire.ViewReply)
                with fixture.loop_turn():
                    saved = service.read_state(fixture.state_dir)
                    self.assertNotIn(request.request_id, saved.ledger)
                    self.assertNotIn(clear.request_id, saved.ledger)
                self.assertEqual(self.reply(self.connect(fixture, request)), wire.Ok())
                self.assertEqual(self.reply(self.connect(fixture, clear)), wire.Ok())
                viewed = self.reply(self.connect(fixture, wire.ViewRequest()))
                assert isinstance(viewed, wire.ViewReply)
                self.assertIsInstance(viewed.reading.explanation.priority, records.NoQueuePriority)
            finally:
                finish.set()

    def test_saved_empty_clear_advances_the_offset_without_reading_history(self) -> None:
        with AdmissionFixture(private=True) as fixture:
            bump = person_actions.Bump("clear-all", person_actions.ClearBump(), "", "director",
                                       fixture.clock.now(), "clear priorities")
            person_actions.save_bump(fixture.state_dir, bump)
            (offset, _), = person_actions.pending_bumps(fixture.state_dir, 0)
            with mock.patch.object(store, "read_last_whole_row_end", side_effect=OSError("history unreadable")) as read:
                _ = fixture.run_pass()
                read.assert_not_called()
            saved = service.read_state(fixture.state_dir)
            self.assertEqual(saved.bumps, person_actions.BumpProgress(offset=offset))
            self.assertIsInstance(saved.priority, records.NoQueuePriority)


class OutageClaimTests(unittest.TestCase):
    def test_outage_claim_clears_once_after_listener_uptime_using_the_last_usable_config(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = time.time()
            fixture.configure(silence_seconds=7)
            claimed = outage_alerts.claim("build-admission")
            assert isinstance(claimed, outage_alerts.AlertClaim)
            outage_alerts.delivered(claimed)
            fixture.clock.advance(100)
            with fixture.loop_turn():
                fixture.restart()
                fixture.server.run_once()
                fixture.clock.advance(6)
                fixture.server.run_once()
                self.assertIsInstance(outage_alerts.claim("build-admission"), outage_alerts.AlertDelivered)
                fixture.configure(silence_seconds=float("nan"))
                fixture.server.run_once()
                self.assertIsInstance(fixture.server.configuration, gate.UnusableAdmissionConfiguration)
                fixture.clock.advance(1)
                with mock.patch.object(outage_alerts, "clear", wraps=outage_alerts.clear) as clear:
                    fixture.server.run_once()
                    fixture.clock.advance(7)
                    fixture.server.run_once()
                self.assertEqual([call for call in clear.call_args_list if call.args[0] == "build-admission"],
                    [mock.call("build-admission", fixture.clock.now() - 7)])
            next_claim = outage_alerts.claim("build-admission")
            self.assertIsInstance(next_claim, outage_alerts.AlertClaim)

    def test_failed_outage_claim_clear_reports_once_per_attempt_and_retries_in_the_loop(self) -> None:
        for failure in (False, OSError("ledger unavailable")):
            with self.subTest(failure=failure), AdmissionFixture() as fixture:
                fixture.configure(silence_seconds=7)
                with fixture.loop_turn():
                    fixture.restart()
                    fixture.server.run_once()
                    fixture.clock.advance(7)
                    with mock.patch.object(outage_alerts, "clear", side_effect=[failure, True]) as clear, \
                            redirect_stderr(io.StringIO()) as errors:
                        fixture.server.run_once()
                        self.assertEqual(clear.call_args_list, [mock.call("build-admission", fixture.clock.now())])
                        self.assertEqual(len(errors.getvalue().splitlines()), 1)
                        self.assertIn("cannot clear outage alert", errors.getvalue())
                        fixture.server.run_once()
                        fixture.server.run_once()
                        self.assertEqual(clear.call_args_list,
                            [mock.call("build-admission", fixture.clock.now())] * 2)
                        self.assertEqual(len(errors.getvalue().splitlines()), 1)


class ProgressProfileServiceTests(unittest.TestCase):
    def test_configured_rank_sets_the_remaining_memory_need(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            write_memory_config(fixture.root / "build_admission.conf", progress_profile_rank="2")
            owner = fixture.proc.spawn(used=GIB)
            sidecar = fixture.scope(owner, current=GIB, anonymous=GIB)
            request = replace(fixture.request(owner=owner, step="build", need=10 * GIB), sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            row = service.reservation_row(fixture.server.reservations[request.episode])
            key = gate.reservation_profile_key(row, fixture.server.host)
            for index, need in enumerate((9 * GIB, 3 * GIB, 6 * GIB)):
                for done in range(0, 100, 5):
                    fixture.server.progress_history.observe(f"previous-{index}", key,
                        progress.ProgressCounter("build:1", done, 100), need, fixture.clock.now())
                fixture.server.progress_history.finish(f"previous-{index}", fixture.clock.now(), fixture.clock.now() - 1)
            progress.publish_counter(sidecar, progress.ProgressCounter("build:1", 50, 100))
            checked = gate.read_admission_configuration()
            self.assertIsInstance(checked, gate.UsableAdmissionConfiguration)
            write_memory_config(fixture.root / "build_admission.conf", peak_history_share_percent="70")
            with mock.patch.object(gate, "read_admission_configuration", return_value=checked):
                _ = fixture.run_pass()
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            room = reply.reading.explanation.room
            assert isinstance(room, records.MemoryRoom)
            hold, = room.runs
            self.assertEqual(hold.memory.need, 6 * GIB)
            self.assertEqual(hold.bytes, 5 * GIB)
            fixture.release(grant)

    def test_saved_highest_bin_names_the_scope_processes_and_reading_source(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            owner = fixture.proc.spawn(used=GIB // 4)
            helper = fixture.proc.spawn(used=11 * GIB // 4)
            helper_proc = fixture.proc.path / str(helper.pid)
            _ = (helper_proc / "cmdline").write_bytes(b"rustc-outlier\0--private-fixture\0")
            _ = (helper_proc / "comm").write_text("rustc-outlier\n")
            sidecar = fixture.scope(owner, members=(owner.pid, helper.pid), current=4 * GIB, anonymous=3 * GIB)
            scope = Path(sidecar.read_text().strip())
            request = replace(fixture.request(owner=owner, step="gate", need=4 * GIB), sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            progress.publish_counter(sidecar, progress.ProgressCounter("suites:1", 20, 100))
            _ = fixture.run_pass()
            _ = (scope / "cgroup.procs").write_text(f"{owner.pid}\n")
            _ = (scope / "memory.stat").write_text(f"anon {GIB}\nshmem 0\ninactive_file 0\n")
            progress.publish_counter(sidecar, progress.ProgressCounter("suites:1", 80, 100))
            _ = fixture.run_pass()
            fixture.release(grant)
            path, = fixture.server.progress_history.root.glob("*/*.json")
            record = progress.read_profile_record(path)
            assert isinstance(record, progress.RecordedProfile)
            self.assertEqual(record.profile.phases[0].bins[4], 3 * GIB)
            self.assertEqual(record.profile.phases[0].bins[16], GIB)
            self.assertEqual(record.reading_source, progress.ScopeReading(scope))
            peak = record.peak_processes
            assert isinstance(peak, progress.PeakProcesses)
            self.assertEqual((peak.phase, peak.bin, peak.anonymous_bytes), ("suites:1", 4, 3 * GIB))
            self.assertEqual({process.pid for process in peak.processes}, {owner.pid, helper.pid})
            helper_sample, = (process for process in peak.processes if process.pid == helper.pid)
            self.assertEqual(helper_sample.command, ("rustc-outlier", "--private-fixture"))
            self.assertEqual(helper_sample.rss_anon, 11 * GIB // 4)

    def test_a_version_four_snapshot_is_rejected_until_a_new_pass_publishes_current_evidence(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            _ = fixture.run_pass()
            path = fixture.state_dir / "queue.json"
            envelope = cast(dict[str, object], json.loads(path.read_text()))
            envelope["v"] = 4
            _ = path.write_text(json.dumps(envelope))
            self.assertIsInstance(queue_snapshot.read_queue_snapshot(fixture.state_dir),
                queue_snapshot.NoQueueSnapshot)
            _ = fixture.run_pass()
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.inputs, fixture.server.last_inputs)

    def test_shared_scope_writes_one_anonymous_sample_across_distinct_step_keys(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            owners = (fixture.proc.spawn(), fixture.proc.spawn())
            run = fixture.run_owner()
            sidecars = tuple(fixture.scope(owner, folder="shared-progress", members=tuple(item.pid for item in owners),
                current=20 * GIB, anonymous=3 * GIB, swap=9 * GIB) for owner in owners)
            grants: list[wire.Admitted] = []
            for step, owner, sidecar in zip(("build", "clippy"), owners, sidecars, strict=True):
                request = replace(fixture.request(owner=owner, step=step, episode=step,
                    need=4 * GIB, run=run), sidecar=str(sidecar))
                grants.append(only_admitted(fixture.server.handle_request(request)))
                fixture.allow_next_start()
            for sidecar in sidecars:
                progress.publish_counter(sidecar, progress.ProgressCounter("build:1", 80, 100))
            _ = fixture.run_pass()
            rows = [service.reservation_row(record) for record in fixture.server.reservations.values()]
            for grant in grants:
                fixture.release(grant)
            samples: list[int] = []
            for row in rows:
                profile = fixture.server.progress_history.matching(
                    gate.reservation_profile_key(row, store.host_name()), fixture.clock.now() - 1)
                peak = progress.window_peak(profile, progress.ProgressCounter("build:1", 80, 100), 1)
                if isinstance(peak, progress.WindowPeak):
                    samples.append(peak.bytes)
            self.assertEqual(samples, [3 * GIB])

    def test_unavailable_anonymous_use_keeps_the_full_need_and_writes_no_profile(self) -> None:
        with AdmissionFixture() as fixture, fixture.loop_turn():
            owner = fixture.proc.spawn()
            sidecar = fixture.scope(owner, current=20 * GIB, anonymous=3 * GIB, swap=9 * GIB)
            request = replace(fixture.request(owner=owner, step="build", need=4 * GIB),
                sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            progress.publish_counter(sidecar, progress.ProgressCounter("build:1", 80, 100))
            _ = (Path(sidecar.read_text().strip()) / "memory.stat").write_text("inactive_file 0\n")
            _ = fixture.run_pass()
            record, = fixture.server.reservations.values()
            row = service.reservation_row(record)
            inputs, policy = fixture.server.last_inputs, fixture.server.last_policy
            assert isinstance(inputs, records.QueueInputs)
            assert isinstance(policy, records.AdmissionPolicy)
            room = gate.inputs_room(inputs, policy)
            assert isinstance(room, records.MemoryRoom)
            self.assertEqual(room.runs[0].memory.need, 4 * GIB)
            fixture.release(grant)
            profile = fixture.server.progress_history.matching(
                gate.reservation_profile_key(row, store.host_name()), fixture.clock.now() - 1)
            self.assertIsInstance(profile, progress.NoMatchingProfile)


class ReadingAlertTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        sender = Path(self.enterContext(tempfile.TemporaryDirectory())) / "message/send.py"
        sender.parent.mkdir()
        _ = sender.write_text("raise AssertionError('unexpected fixture sender execution')\n")
        _ = self.enterContext(mock.patch.object(session_send, "SEND_SCRIPT", sender))

    @staticmethod
    def finish_delivery(fixture: AdmissionFixture) -> None:
        delivery = fixture.server.reading_alert_delivery
        if isinstance(delivery, service.ReadingAlertDelivery):
            delivery.thread.join(timeout=3)
            if delivery.thread.is_alive():
                raise AssertionError("the stub sender did not finish")

    def test_one_message_per_unreadable_stretch_and_another_after_recovery(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = time.time()
            fixture.configure(reading_alert_seconds=10)
            request = fixture.request()
            path = fixture.root / "pressure"
            path.unlink()
            messages: list[tuple[list[str], str]] = []

            def send(command: list[str], **kwargs: str | bool | float) -> subprocess.CompletedProcess[str]:
                if any(part.endswith("message/send.py") for part in command):
                    messages.append((command, str(kwargs["input"])))
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"unexpected subprocess during alert delivery: {command}")

            with mock.patch("scripts.build_queue.admission_service.subprocess.run", side_effect=send):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
                fixture.clock.advance(9)
                _ = fixture.run_pass()
                self.assertEqual(messages, [])
                fixture.clock.advance(1)
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(messages), 1)
                command, message = messages[0]
                self.assertEqual(command[command.index("--to") + 1], "natedev")
                self.assertEqual(command[command.index("--from") + 1], "build-admission")
                self.assertIn(str(path), message)
                self.assertIn("10 seconds", message)
                fixture.clock.advance(10)
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(messages), 1)
                write_pressure_reading(path)
                _ = fixture.run_pass()
                self.assertIsInstance(fixture.server.reading_hold, service.NoReadingHold)
                path.unlink()
                self.assertIsInstance(fixture.server.handle_request(fixture.request())[0], wire.Wait)
                fixture.clock.advance(10)
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(messages), 2)

    def test_failed_delivery_releases_the_claim_for_retry(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(reading_alert_seconds=1)
            request = fixture.request()
            (fixture.root / "pressure").unlink()
            _ = fixture.server.handle_request(request)
            fixture.clock.advance(1)
            attempts: list[str] = []

            def send(command: list[str], **kwargs: str | bool | float) -> subprocess.CompletedProcess[str]:
                if any(part.endswith("message/send.py") for part in command):
                    attempts.append(str(kwargs["input"]))
                    return subprocess.CompletedProcess(command, 2 if len(attempts) == 1 else 0, "", "stub failure")
                raise AssertionError(f"unexpected subprocess during alert delivery: {command}")

            with mock.patch("scripts.build_queue.admission_service.subprocess.run", side_effect=send):
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(attempts), 1)
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(attempts), 2)
                _ = fixture.run_pass()
                self.finish_delivery(fixture)
                self.assertEqual(len(attempts), 2)

    def test_admission_pass_does_not_wait_for_the_sender(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(reading_alert_seconds=1)
            request = fixture.request()
            (fixture.root / "pressure").unlink()
            _ = fixture.server.handle_request(request)
            fixture.clock.advance(1)
            entered = threading.Event()
            release = threading.Event()

            def send(command: list[str], **kwargs: str | bool | float) -> subprocess.CompletedProcess[str]:
                if any(part.endswith("message/send.py") for part in command):
                    self.assertIn("unreadable", str(kwargs["input"]))
                    entered.set()
                    if not release.wait(timeout=3):
                        raise AssertionError("the admission pass waited for the sender")
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise AssertionError(f"unexpected subprocess during alert delivery: {command}")

            with mock.patch("scripts.build_queue.admission_service.subprocess.run", side_effect=send):
                try:
                    result = fixture.run_pass()
                    self.assertTrue(entered.wait(timeout=2))
                    self.assertEqual(result.rows[0].decision.state, "hold")
                    delivery = fixture.server.reading_alert_delivery
                    assert isinstance(delivery, service.ReadingAlertDelivery)
                    self.assertTrue(delivery.thread.is_alive())
                    _ = fixture.run_pass()
                    self.assertIs(fixture.server.reading_alert_delivery, delivery)
                finally:
                    release.set()
                    self.finish_delivery(fixture)

    def test_no_timer_for_an_empty_queue_a_token_wait_or_a_slot_continuation(self) -> None:
        for state in ("empty", "token", "slot"):
            with self.subTest(state=state), AdmissionFixture() as fixture:
                fixture.configure(reading_alert_seconds=1)
                run = fixture.run_owner()
                request = fixture.request(run=run)
                if state != "empty":
                    grant = only_admitted(fixture.server.handle_request(request))
                    if state == "token":
                        self.assertEqual(fixture.server.handle_request(wire.TokenWaitRequest("token", grant.reservation)), [wire.Ok()])
                    else:
                        self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0)), [wire.Ok()])
                _ = fixture.enter_context(mock.patch("scripts.build_queue.admission_service.subprocess.run",
                    return_value=subprocess.CompletedProcess(["stub"], 0, "", "")))
                (fixture.root / "pressure").unlink()
                if state == "slot":
                    held, = fixture.server.handle_request(fixture.request(run=run, age=600))
                    assert isinstance(held, wire.Wait)
                    assert isinstance(held.reason, records.HeadAdmissionStatus)
                    self.assertIsInstance(held.reason.decision.room.basis, records.WithinSlot)
                    self.assertIsInstance(held.reason.cause, records.EstimateExpired)
                _ = fixture.run_pass()
                fixture.clock.advance(2)
                _ = fixture.run_pass()
                self.assertIsInstance(fixture.server.reading_hold, service.NoReadingHold)
                self.assertIsInstance(fixture.server.reading_alert_delivery, service.NoReadingAlertDelivery)


class PlacementRestartTests(unittest.TestCase):
    def test_first_view_after_restart_uses_the_snapshot_while_a_probe_succeeds_slowly(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import PlacementFixture, placement_request

        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            fixture.server.placement_requests = {
                f"saved-{index}": replace(request, episode=f"saved-{index}",
                    owner=request.owner if index == 0 else placement.SubmittingOwner(
                        fixture.server.host, 700000 + index, "missing"))
                for index in range(30)}
            fixture.server.persist()
            fixture.server.close()
            entered, succeeded = threading.Event(), threading.Event()
            release = threading.Event()

            def probe(key: placement.HostEvidenceKey) -> placement.HostEvidence:
                entered.set()
                if not release.wait(test_hang_guard.TEST_HANG_GUARD_SECS):
                    raise AssertionError("the test did not release the probe")
                succeeded.set()
                return placement.FreshHostEvidence(key, fixture.clock.now(), "", placement.AllowedHost())

            with mock.patch.object(fixture, "read_provider", side_effect=probe):
                fixture.server = fixture.new_server()
                try:
                    self.assertEqual(len(fixture.server.placement_requests), 30)
                    self.assertTrue(entered.wait(1))
                    with fixture.serving():
                        reading = client.view()
                        self.assertIsInstance(reading, records.QueueRead)
                        self.assertFalse(succeeded.is_set())
                        release.set()
                        self.assertTrue(succeeded.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                    with mock.patch.object(fixture.server, "host_evidence_key",
                                           side_effect=AssertionError("a view must use cached evidence")):
                        reply, = fixture.server.handle_request(wire.ViewRequest())
                        self.assertIsInstance(reply, wire.ViewReply)
                finally:
                    for worker in fixture.server.host_targets.values():
                        worker.close()
                    release.set()
                    for worker in fixture.server.host_targets.values():
                        worker.thread.join(timeout=5)
                        self.assertFalse(worker.thread.is_alive())

    def test_restart_preserves_old_waiters_and_both_placement_permission_states(self) -> None:
        from scripts.build_queue import placement
        from scripts.build_queue.test_placement import (
            PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, consume_permission,
        )

        for version in (1, 2):
            with self.subTest(version=version), PlacementFixture() as fixture:
                fixture.configure(cloud_gate_slots=1)
                fixture.free(0)
                blocker = replace(fixture.request(), place=wire.CloudPlace("aws"))
                _ = only_admitted(fixture.server.handle_request(blocker))
                fixture.hold_starts()
                local = fixture.request(since=fixture.clock.now() - 100)
                cloud = replace(fixture.request(since=fixture.clock.now() - 50), place=wire.CloudPlace("aws"))
                legacy_messages: list[str] = []
                for request in (local, cloud):
                    envelope = cast(dict[str, object], json.loads(wire.encode_request(request)))
                    envelope["v"] = version
                    message = json.dumps(envelope) + "\n"
                    legacy_messages.append(message)
                    reply, = fixture.connect(message).replies()
                    self.assertIsInstance(reply, wire.Wait)
                pending = placement_request(fixture, since=fixture.clock.now() - 25)
                consumed = placement_request(fixture, since=fixture.clock.now() - 10)
                _ = publish_candidate(fixture, pending)
                pending_assignment = assignment_for(fixture, pending)
                consumed_assignment = assignment_for(fixture, consumed)
                claim = adopt_assignment(fixture, consumed, consumed_assignment)
                consume = consume_permission("first-consumption", consumed_assignment, claim)
                self.assertEqual(fixture.server.handle_request(consume),
                                 [wire.FirstAuthorization(consumed_assignment, claim)])
                positions = fixture.server.places.copy()
                waiters = {episode: fixture.server.waiters[episode].request for episode in (local.episode, cloud.episode)}
                fixture.restart()
                self.assertEqual(fixture.server.places, positions)
                for message in legacy_messages:
                    reply, = fixture.connect(message).replies()
                    self.assertIsInstance(reply, wire.Wait)
                self.assertEqual({episode: fixture.server.waiters[episode].request for episode in waiters}, waiters)
                self.assertEqual(assignment_for(fixture, pending), pending_assignment)
                consumed_reply, = fixture.connect(consumed).replies()
                self.assertIsInstance(consumed_reply, wire.PlacementWait)
                saved = fixture.server.assignments[execution_identity.attempt_key(
                    consumed_assignment.episode, consumed_assignment.attempt)]
                self.assertEqual(saved.assignment, consumed_assignment)
                self.assertIsInstance(saved.state, placement.CurrentAssignment)
                self.assertIsInstance(saved.permission, placement.PermissionConsumed)
                status, = fixture.server.handle_request(consume)
                self.assertIsInstance(status, wire.DispatchStatus)
                assert isinstance(status, wire.DispatchStatus)
                self.assertIsInstance(status.permission, placement.PermissionConsumed)
                pending_claim = adopt_assignment(fixture, pending, pending_assignment)
                self.assertEqual(fixture.server.handle_request(consume_permission(
                    "pending-consumption", pending_assignment, pending_claim)),
                    [wire.FirstAuthorization(pending_assignment, pending_claim)])


class CloudAdmissionTests(unittest.TestCase):
    def test_aws_grant_requires_a_registered_enabled_scaler_with_slots_and_daily_room(self) -> None:
        from scripts.aws_target import ledger
        from scripts.build_queue.test_placement import PlacementFixture, aws_gate_request, read_aws_gate_evidence

        for cause in ("off", "slots", "daily", "absent", "ready"):
            with self.subTest(cause=cause), PlacementFixture() as fixture:
                fixture.configure(cloud_gate_slots=16)
                if cause == "slots":
                    for _ in range(16):
                        _ = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
                request = aws_gate_request(fixture, enabled=cause != "off", daily_room=cause != "daily")
                if cause == "absent":
                    _ = write_config_fixture(fixture.root / "build_targets.conf", "targets=natedev\n")
                directory = fixture.root / "queue-target-state"
                book = ledger.CostLedger(directory / "aws")
                saved = book.path.read_bytes()
                with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(directory),
                                                  "AWS_TARGET_CONFIG": str(fixture.root / "aws_target.conf")}):
                    _ = read_aws_gate_evidence(fixture, request)
                    reply, = fixture.server.handle_request(request)
                    if cause == "ready":
                        assert isinstance(reply, wire.Assignment)
                        self.assertEqual(reply.assignment.destination.target, "claude-aws")
                        self.assertEqual(fixture.server.current_assignments[request.episode],
                            execution_identity.attempt_key(request.episode, reply.assignment.attempt))
                    else:
                        self.assertNotIsInstance(reply, wire.Assignment)
                        self.assertNotIn(request.episode, fixture.server.current_assignments)
                        candidate, = fixture.server.placement_eligibility[request.episode]
                        self.assertNotIsInstance(candidate.eligibility, placement.AllowedHost)
                self.assertEqual(book.path.read_bytes(), saved)

    def test_aws_grants_count_against_daily_room_until_the_worker_books(self) -> None:
        from decimal import Decimal
        from scripts.aws_target import ledger
        from scripts.aws_target.ledger_test_support import override_fixture_settings
        from scripts.build_queue.test_placement import PlacementFixture, aws_gate_request, read_aws_gate_evidence

        with PlacementFixture() as fixture:
            directory = fixture.root / "queue-target-state"
            with mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(directory),
                                              "AWS_TARGET_CONFIG": str(fixture.root / "aws_target.conf")}):
                first = aws_gate_request(fixture)
                second = aws_gate_request(fixture)
                gate_settings = fixture.root / "cloud_gate.conf"
                override_fixture_settings(gate_settings, "# range: 0..\nattempt_allowance_usd=20\n")
                _ = read_aws_gate_evidence(fixture, first)
                _ = read_aws_gate_evidence(fixture, second)
                reply, = fixture.server.handle_request(first)
                self.assertIsInstance(reply, wire.Assignment)
                assert isinstance(reply, wire.Assignment)
                self.assertNotIsInstance(fixture.server.handle_request(second)[0], wire.Assignment)
                blocked, = fixture.server.placement_eligibility[second.episode]
                self.assertEqual(blocked.eligibility, placement.TemporaryExclusion("daily_limit"))

                assert isinstance(first.local, wire.LocalAdmissionContext)
                binding = ledger.GateDispatchBinding.read(ledger.gate_dispatch_binding_path(Path(first.local.sidecar)))
                holder = placement.ClaimHolderIdentity(first.owner.host, first.owner.pid, first.owner.start_time)
                journal = ledger.GateDispatchJournal(binding.journal)
                journal.begin(binding, holder)
                permission, = fixture.server.handle_request(wire.ConsumePermission("consume", reply.assignment, binding.claim, holder))
                self.assertIsInstance(permission, wire.FirstAuthorization)
                folder = fixture.root / "worker"
                journal.register(binding.claim, holder, folder)
                book = ledger.CostLedger(binding.directory)
                now = int(fixture.clock.now())
                with book.transaction() as evidence:
                    evidence.gate_reservations.append(ledger.GateReservation(folder.name, now, now + 60,
                                                                             Decimal(0), Decimal(20)))
                override_fixture_settings(gate_settings, "attempt_allowance_usd=10\n")
                _ = read_aws_gate_evidence(fixture, second)
                self.assertIsInstance(fixture.server.handle_request(second)[0], wire.Assignment)
                self.assertEqual(len(book.read_snapshot().gate_reservations), 1)

    def test_cloud_and_memory_settings_share_the_checked_pass_and_failure_path(self) -> None:
        from scripts.build_queue import cloud_progress

        for key in ("cloud_gate_slots", "cloud_gate_stale_pushes", "safety_gib", "peak_history_share_percent"):
            with self.subTest(key=key), AdmissionFixture() as fixture:
                config_path = fixture.root / "build_admission.conf"
                write_memory_config(config_path, cloud_gate_slots="1", cloud_gate_stale_pushes="3")
                sidecar = fixture.root / "cloud.cgroup"
                _ = sidecar.write_text("")
                request = replace(self.cloud_request(fixture), sidecar=str(sidecar))
                grant = only_admitted(fixture.server.handle_request(request))
                cloud_progress.publish(sidecar, cloud_progress.CloudProgress("running", 32, 0, fixture.clock.now()))
                fixture.clock.advance(15)
                waiting = self.cloud_request(fixture)
                self.assertIsInstance(fixture.server.handle_request(waiting)[0], wire.Wait)
                local_requests: list[wire.AdmitRequest] = []
                if key == "peak_history_share_percent":
                    local = fixture.request()
                    assert isinstance(local.profile, records.StepProfile)
                    local = replace(local, profile=replace(local.profile,
                        repository=records.RepositoryIdentity("fixture-repository", "fixture")))
                    store.append_line(fixture.state_dir / "anon_peaks.jsonl", {
                        "host": fixture.server.host, "repo_identity": "fixture-repository", "step": "nextest",
                        "test_threads": 8, "test_phase_peak_bytes": 3 * GIB,
                        "ended_at": store.utc_iso(fixture.clock.now() - 1)})
                    fixture.hold_starts()
                    self.assertIsInstance(fixture.server.handle_request(local)[0], wire.Wait)
                    local_requests.append(local)
                    write_pressure_reading(fixture.root / "pressure")
                    fixture.allow_next_start()
                read_configuration = gate.read_admission_configuration

                def change_after_check() -> gate.UsableAdmissionConfiguration | gate.UnusableAdmissionConfiguration:
                    configuration = read_configuration()
                    write_memory_config(config_path, **{key: "invalid"})
                    return configuration

                with mock.patch.object(gate, "read_admission_configuration", side_effect=change_after_check):
                    fixture.server.next_pass = fixture.clock.monotonic()
                    fixture.server.run_once(0)
                self.assertIsInstance(fixture.server.configuration, gate.UsableAdmissionConfiguration)
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply) and isinstance(reply.reading, wire.CloudQueueRead)
                self.assertEqual(reply.reading.cloud_slots, 1)
                self.assertEqual(reply.reading.cloud_gate_stale_pushes, 3)
                self.assertIn(waiting.episode, fixture.server.waiters)
                for local in local_requests:
                    self.assertEqual(fixture.server.reservations[local.episode].test_phase_need,
                                     records.TestPhaseNeed(3 * GIB, 1, 8, 8, "same_width", margin=1.2))
                cloud = next(row for row in reply.reading.cloud if row.owner == request.owner)
                information = queue_view.cloud_work(cloud, reply.reading.explanation.now,
                    reply.reading.cloud_gate_stale_pushes, queue_view.CloudPushInterval(5))
                self.assertIsInstance(information.update, queue_view.CloudUpdateLate)
                fixture.server.next_pass = fixture.clock.monotonic()
                fixture.server.run_once(0)
                self.assertIsInstance(fixture.server.configuration, gate.UnusableAdmissionConfiguration)
                refused, = fixture.connect(wire.ViewRequest()).replies()
                self.assertIsInstance(refused, wire.Refused)
                assert isinstance(refused, wire.Refused)
                self.assertIn(key, refused.reason)
                self.assertIsInstance(fixture.connect(wire.BuildHistoryRequest()).replies()[0], wire.BuildHistoryReply)
                fixture.release(grant)
                for local in local_requests:
                    local_grant = fixture.server.ledger[local.episode].reply
                    assert isinstance(local_grant, wire.Admitted)
                    fixture.release(local_grant)
                self.assertFalse(fixture.server.reservations)

    def test_view_and_mine_keep_version_one_readings_plain_with_a_cloud_gate(self) -> None:
        with AdmissionFixture() as fixture:
            _ = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            sent: list[str] = []

            def capture(line: str) -> str:
                sent.append(line)
                return line

            with mock.patch.object(fixture.server, "line_filter", side_effect=capture):
                for version in (2, 1, 2, 1):
                    for kind, request in (("view", wire.ViewRequest()), ("mine", wire.MineRequest("cloud-session"))):
                        with self.subTest(version=version, kind=kind):
                            line = wire.encode_record({"v": version, "type": kind, "data": request}) + "\n"
                            reply, = fixture.connect(line).replies()
                            assert isinstance(reply, (wire.ViewReply, wire.MineReply))
                            self.assertEqual(reply.reading.explanation.local_occupancy,
                                             records.LocalAdmissionOccupancy((), {}))
                            message = cast(dict[str, object], json.loads(sent[-1]))
                            self.assertEqual(message["v"], version)
                            if version == 1:
                                self.assertIs(type(reply.reading), records.QueueRead)
                                data = cast(dict[str, object], message["data"])
                                old_reading = wire.parse_record(json.dumps(data["reading"]), records.QueueRead)
                                self.assertEqual(old_reading, reply.reading)
                            else:
                                self.assertIsInstance(reply.reading, wire.CloudQueueRead)
                                assert isinstance(reply.reading, wire.CloudQueueRead)
                                self.assertEqual(len(reply.reading.cloud), 1)

    def test_bad_admission_settings_hold_admission_and_keep_the_service_available(self) -> None:
        for key, invalid_values, repaired_value in (
            ("cloud_gate_slots", ("bad", "0"), "9"),
            ("cloud_gate_stale_pushes", ("bad", "0"), "3"),
            ("peak_history_share_percent", ("bad", "70"), "10"),
        ):
            for value in invalid_values:
                with self.subTest(key=key, value=value), AdmissionFixture() as fixture:
                    grant = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
                    config = fixture.root / "build_admission.conf"
                    write_memory_config(config, **{key: value})
                    fixture.server.next_pass = fixture.clock.monotonic()
                    fixture.server.run_once(0)
                    self.assertIsInstance(fixture.server.configuration, gate.UnusableAdmissionConfiguration)
                    reply, = fixture.connect(wire.ViewRequest()).replies()
                    self.assertIsInstance(reply, wire.Refused)
                    assert isinstance(reply, wire.Refused)
                    self.assertIn(key, reply.reason)
                    fixture.release(grant)
                    self.assertFalse(fixture.server.reservations)
                    write_memory_config(config, **{key: repaired_value})
                    fixture.server.run_once(0)
                    self.assertIsInstance(fixture.server.configuration, gate.UsableAdmissionConfiguration)
                    self.assertIsInstance(fixture.connect(wire.ViewRequest()).replies()[0], wire.ViewReply)

    def reading(self, fixture: AdmissionFixture) -> records.QueueRead:
        reply, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        return reply.reading

    def cloud_request(self, fixture: AdmissionFixture, *, step: str = "gate",
                      run: wire.RunIdentity | wire.NoRun = wire.NO_RUN,
                      group: status_wire.CloudGroup | status_wire.NoCloudGroup = status_wire.NO_CLOUD_GROUP
                      ) -> wire.AdmitRequest:
        return replace(fixture.request(need=40 * GIB, step=step, run=run), place=wire.CloudPlace("aws", group))

    def cloud_group(self, fixture: AdmissionFixture, key: str, size: int) -> status_wire.CloudGroup:
        return status_wire.CloudGroup(key, size, fixture.clock.now(), fixture.proc.spawn())

    def ordered_cloud_waits(self, fixture: AdmissionFixture) -> list[wire.Owner]:
        reading = self.reading(fixture)
        assert isinstance(reading, wire.CloudQueueRead)
        return [row.owner for row in reading.cloud if isinstance(row.wait, wire.CloudSlotWait)
                and isinstance(row.wait.state, status_wire.WaitingForCloudSlot)]

    def test_cloud_grants_a_later_ranked_unit_before_an_earlier_unranked_unit(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(cloud_gate_slots=1)
            blocker = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            older = replace(self.cloud_request(fixture), since=fixture.clock.now() - 30)
            ranked = self.cloud_request(fixture)
            labels = {ranked.identity.session_id: UnitLabels("ranked", unit="urgent")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                for request in (older, ranked):
                    _ = fixture.server.handle_request(request)
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                    "cloud-priority", "urgent", "user work", "director")), [wire.Ok()])
                self.assertEqual(self.ordered_cloud_waits(fixture), [ranked.owner, older.owner])
                fixture.release(blocker)
                _ = fixture.run_pass()
                self.assertIn(ranked.episode, fixture.server.reservations)
                self.assertIn(older.episode, fixture.server.waiters)

    def test_cloud_keeps_time_order_for_two_waits_of_the_same_rank(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(cloud_gate_slots=1)
            _ = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            newer = self.cloud_request(fixture)
            older = replace(self.cloud_request(fixture), since=fixture.clock.now() - 30)
            for request in (newer, older):
                _ = fixture.server.handle_request(replace(request, identity=replace(request.identity, unit="urgent")))
            self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                "cloud-priority", "urgent", "user work", "director")), [wire.Ok()])
            self.assertEqual(self.ordered_cloud_waits(fixture), [older.owner, newer.owner])

    def test_cloud_keeps_time_order_without_saved_priority(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(cloud_gate_slots=1)
            blocker = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            newer = self.cloud_request(fixture)
            older = replace(self.cloud_request(fixture), since=fixture.clock.now() - 30)
            for request in (newer, older):
                _ = fixture.server.handle_request(request)
            self.assertEqual(self.ordered_cloud_waits(fixture), [older.owner, newer.owner])
            fixture.release(blocker)
            _ = fixture.run_pass()
            self.assertIn(older.episode, fixture.server.reservations)

    def test_cloud_and_natedev_rank_the_same_units_in_the_same_order(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(cloud_gate_slots=1)
            _ = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            fixture.free(0)
            fixture.hold_starts()
            cloud_units: dict[wire.Owner, str] = {}
            for age, unit in ((30, "unranked"), (20, "second"), (10, "first")):
                since = fixture.clock.now() - age
                local = fixture.request(since=since)
                cloud = replace(self.cloud_request(fixture), since=since)
                for request in (local, cloud):
                    _ = fixture.server.handle_request(replace(request, identity=replace(request.identity, unit=unit)))
                cloud_units[cloud.owner] = unit
            for unit in ("first", "second"):
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                    f"rank-{unit}", unit, "user work", "director")), [wire.Ok()])
            local_order = [step.unit for step in self.reading(fixture).view.waiting]
            self.assertEqual(local_order, ["first", "second", "unranked"])
            self.assertEqual([cloud_units[owner] for owner in self.ordered_cloud_waits(fixture)], local_order)

    def test_disconnected_cloud_group_keeps_its_unit_rank_after_restart(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(cloud_gate_slots=2)
            group = self.cloud_group(fixture, "trial", 2)
            first = self.cloud_request(fixture, group=group)
            first = replace(first, identity=replace(first.identity, unit="urgent"))
            self.assertIsInstance(fixture.connect(first).replies()[0], wire.Wait)
            self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                "cloud-priority", "urgent", "user work", "director")), [wire.Ok()])
            fixture.restart()
            older = replace(self.cloud_request(fixture), since=group.started_at - 30)
            self.assertIsInstance(fixture.server.handle_request(older)[0], wire.Wait)
            self.assertEqual(self.ordered_cloud_waits(fixture), [older.owner])
            self.assertFalse(fixture.server.reservations)

    def test_cloud_group_keeps_its_roster_unit_after_restart(self) -> None:
        for label_at_registration in (True, False):
            with self.subTest(label_at_registration=label_at_registration), AdmissionFixture() as fixture:
                fixture.configure(cloud_gate_slots=2)
                group = self.cloud_group(fixture, "trial", 2)
                first = self.cloud_request(fixture, group=group)
                roster = {first.identity.session_id: UnitLabels("first", unit="urgent")}
                labels = roster.copy() if label_at_registration else {}
                with mock.patch.object(gate, "priority_labels", return_value=labels):
                    self.assertIsInstance(fixture.connect(first).replies()[0], wire.Wait)
                    labels.update(roster)
                    self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                        "cloud-priority", "urgent", "user work", "director")), [wire.Ok()])
                    labels.clear()
                    fixture.restart()
                    older = replace(self.cloud_request(fixture), since=group.started_at - 30)
                    self.assertIsInstance(fixture.server.handle_request(older)[0], wire.Wait)
                    pending = service.read_state(fixture.state_dir).pending_cloud_groups[group.key]
                    self.assertEqual(pending.unit, "urgent")
                    self.assertIsInstance(fixture.server.handle_request(first)[0], wire.Wait)
                    second = self.cloud_request(fixture, group=group)
                    _ = only_admitted(fixture.server.handle_request(second))
                    self.assertEqual(set(fixture.server.reservations), {first.episode, second.episode})
                    self.assertEqual(self.ordered_cloud_waits(fixture), [older.owner])

    def test_placement_report_keeps_cloud_waits_in_the_last_pass_order(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, adopt_assignment, assignment_for, consume_permission, placement_request, publish_candidate,
        )

        with PlacementFixture() as fixture:
            fixture.configure(cloud_gate_slots=1)
            remote = placement_request(fixture)
            _ = publish_candidate(fixture, remote)
            assignment = assignment_for(fixture, remote)
            claim = adopt_assignment(fixture, remote, assignment)
            self.assertEqual(fixture.server.handle_request(consume_permission("place-consume", assignment, claim)),
                             [wire.FirstAuthorization(assignment, claim)])
            _ = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            older = replace(self.cloud_request(fixture), since=fixture.clock.now() - 30)
            newer = self.cloud_request(fixture)
            labels = {newer.identity.session_id: UnitLabels("ranked", unit="urgent")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                for request in (older, newer):
                    _ = fixture.server.handle_request(request)
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                    "cloud-priority", "urgent", "user work", "director")), [wire.Ok()])
                order = [newer.owner, older.owner]
                for position, request in enumerate((newer, older), 1):
                    wait = fixture.server.waiters[request.episode].last_reply
                    assert isinstance(wait, wire.Wait)
                    self.assertEqual(wait.position, position)
                before = self.reading(fixture)
                self.assertEqual(self.ordered_cloud_waits(fixture), order)
                labels.clear()
                labels[older.identity.session_id] = UnitLabels("ranked", unit="urgent")
                passed = fixture.server.last_result
                evidence = execution_identity.ObservedExecution(execution_identity.ProcessExecution(
                    assignment.destination.host, "host-generation-a", 700001, "start"))
                _ = fixture.server.handle_request(wire.ExecutionReport("remote-started", assignment, claim, evidence))
                self.assertIs(fixture.server.last_result, passed)
                self.assertIsNot(self.reading(fixture), before)
                self.assertEqual(self.ordered_cloud_waits(fixture), order)
                _ = fixture.run_pass()
                self.assertEqual(self.ordered_cloud_waits(fixture), [older.owner, newer.owner])

    def test_cloud_group_precedes_an_earlier_submission_at_the_same_time(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(cloud_gate_slots=1)
            blocker = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            single = self.cloud_request(fixture)
            self.assertIsInstance(fixture.server.handle_request(single)[0], wire.Wait)
            group = replace(self.cloud_group(fixture, "trial", 1), started_at=single.since)
            grouped = replace(self.cloud_request(fixture, group=group), since=single.since)
            self.assertIsInstance(fixture.server.handle_request(grouped)[0], wire.Wait)
            self.assertEqual(self.ordered_cloud_waits(fixture), [grouped.owner, single.owner])
            fixture.release(blocker)
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {grouped.episode})
            self.assertEqual(self.ordered_cloud_waits(fixture), [single.owner])

    def test_cloud_group_waits_for_all_members_and_slots_before_it_admits(self) -> None:
        from scripts.build_queue import cloud_progress

        with AdmissionFixture() as fixture:
            fixture.configure(cloud_gate_slots=3)
            blocker = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            group = self.cloud_group(fixture, "trial", 3)
            requests = [replace(self.cloud_request(fixture, group=group), sidecar=str(fixture.root / f"{i}.cgroup"))
                        for i in range(3)]
            for request in requests:
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            follower = self.cloud_request(fixture)
            self.assertIsInstance(fixture.server.handle_request(follower)[0], wire.Wait)
            self.assertEqual(len(fixture.server.reservations), 1)
            queued = self.reading(fixture)
            assert isinstance(queued, wire.CloudQueueRead)
            self.assertEqual(sum(isinstance(row.wait.state, status_wire.WaitingForCloudSlot)
                                 for row in queued.cloud if isinstance(row.wait, wire.CloudSlotWait)), 4)

            fixture.release(blocker)
            _ = fixture.run_pass()
            episodes = {request.episode for request in requests}
            self.assertEqual(set(fixture.server.reservations), episodes)
            saved = wire.parse_record(fixture.state_text(), service.ServiceState)
            self.assertEqual(set(saved.reservations), episodes)
            self.assertEqual(len({record.admitted_at for record in saved.reservations.values()}), 1)
            self.assertFalse(saved.pending_cloud_groups)
            self.assertIn(follower.episode, fixture.server.waiters)
            waiting = self.reading(fixture)
            assert isinstance(waiting, wire.CloudQueueRead)
            self.assertTrue(all(isinstance(row.wait, wire.CloudSlotWait) for row in waiting.cloud))
            self.assertEqual(sum(isinstance(row.wait.state, status_wire.ReservedCloudSlot) for row in waiting.cloud
                                 if isinstance(row.wait, wire.CloudSlotWait)), 3)

            launched = requests[0]
            requesting = cloud_progress.CloudProgress("requesting", 0, 0, fixture.clock.now(), stage="launch")
            cloud_progress.publish(Path(launched.sidecar), requesting)
            _ = fixture.run_pass()
            reading = self.reading(fixture)
            assert isinstance(reading, wire.CloudQueueRead)
            running, = (row for row in reading.cloud if isinstance(row.wait, records.ReadyForAdmission))
            self.assertEqual(running.owner, launched.owner)
            self.assertEqual(running.progress, requesting)

    def test_cloud_progress_failure_preserves_the_launch_state(self) -> None:
        from scripts.build_queue import cloud_progress

        for launch_seen in (False, True):
            with self.subTest(launch_seen=launch_seen), AdmissionFixture() as fixture:
                sidecar = fixture.root / "cloud.cgroup"
                request = replace(self.cloud_request(fixture), sidecar=str(sidecar))
                _ = only_admitted(fixture.server.handle_request(request))
                if launch_seen:
                    cloud_progress.publish(sidecar,
                        cloud_progress.CloudProgress("requesting", 0, 0, fixture.clock.now()))
                    _ = fixture.run_pass()
                    visible = self.reading(fixture)
                    assert isinstance(visible, wire.CloudQueueRead)
                    self.assertIsInstance(visible.cloud[0].wait, records.ReadyForAdmission)

                _ = sidecar.with_suffix(".cgroup.cloud.json").write_text("{")
                for restart in (False, True):
                    if restart:
                        fixture.restart()
                    else:
                        _ = fixture.run_pass()
                    reading = self.reading(fixture)
                    assert isinstance(reading, wire.CloudQueueRead)
                    row, = reading.cloud
                    self.assertEqual(row.progress, cloud_progress.NoCloudProgress("missing"))
                    if launch_seen:
                        self.assertIsInstance(row.wait, records.ReadyForAdmission)
                    else:
                        assert isinstance(row.wait, wire.CloudSlotWait)
                        self.assertEqual(row.wait, wire.CloudSlotWait(1, reading.cloud_slots,
                            status_wire.ReservedCloudSlot()))
                        self.assertEqual(row.wait.text, "waits for launch")

    def test_cloud_group_refuses_if_it_is_larger_than_the_limit(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(cloud_gate_slots=2)
            waiting = self.cloud_request(fixture, group=self.cloud_group(fixture, "blocked", 2))
            self.assertIsInstance(fixture.server.handle_request(waiting)[0], wire.Wait)
            too_large = self.cloud_request(fixture, group=self.cloud_group(fixture, "too-large", 3))
            self.assertEqual(fixture.server.handle_request(too_large),
                             [wire.Refused("cloud group needs 3 slots but only 2 are configured")])
            self.assertEqual(set(fixture.server.waiters), {waiting.episode})
            self.assertFalse(fixture.server.reservations)

    def test_cloud_group_keeps_order_during_reconnect_and_restart(self) -> None:
        for restart in (False, True):
            with self.subTest(restart=restart), AdmissionFixture() as fixture:
                fixture.configure(cloud_gate_slots=2)
                group = self.cloud_group(fixture, "first", 2)
                first = self.cloud_request(fixture, group=group)
                peer = fixture.connect(first)
                self.assertIsInstance(peer.replies()[0], wire.Wait)
                if restart:
                    fixture.restart()
                else:
                    peer.close()
                    fixture.server.run_once(0)
                self.assertFalse(fixture.server.waiters)
                fixture.clock.advance(1)
                later = self.cloud_group(fixture, "second", 1)
                follower = self.cloud_request(fixture, group=later)
                self.assertIsInstance(fixture.server.handle_request(follower)[0], wire.Wait)
                fixture.clock.advance(299)
                second = self.cloud_request(fixture, group=group)
                self.assertIsInstance(fixture.server.handle_request(second)[0], wire.Wait)
                reading = self.reading(fixture)
                assert isinstance(reading, wire.CloudQueueRead)
                self.assertEqual([row.owner for row in reading.cloud], [second.owner, follower.owner])
                self.assertTrue(all(isinstance(row.wait, wire.CloudSlotWait) for row in reading.cloud))
                reconnected = replace(first, since=fixture.clock.now())
                _ = only_admitted(fixture.server.handle_request(reconnected))
                self.assertEqual(set(fixture.server.reservations), {first.episode, second.episode})
                self.assertIn(follower.episode, fixture.server.waiters)

    def test_dead_cloud_member_is_dropped_and_dead_coordinator_refuses_the_group(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(cloud_gate_slots=3)
            blocker = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            group = self.cloud_group(fixture, "trial", 3)
            first, second, dead = (self.cloud_request(fixture, group=group) for _ in range(3))
            for request in (first, second, dead):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            fixture.end_owner(dead.owner)
            fixture.release(blocker)
            _ = fixture.run_pass()
            self.assertNotIn(dead.episode, fixture.server.waiters)
            self.assertEqual(set(fixture.server.waiters), {first.episode, second.episode})
            self.assertFalse(fixture.server.reservations)
            fixture.clock.advance(300)
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.waiters), {first.episode, second.episode})
            fixture.end_owner(group.coordinator)
            _ = fixture.run_pass()
            for surviving in (first, second):
                self.assertEqual(fixture.server.handle_request(surviving),
                    [wire.Refused("cloud group coordinator no longer exists")])
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)
            self.assertFalse(fixture.server.pending_cloud_groups)

    def test_cloud_request_wire_keeps_the_legacy_single_slot_shape(self) -> None:
        with AdmissionFixture() as fixture:
            single = self.cloud_request(fixture)
            encoded = status_wire.encode_request(single)
            self.assertIn('"place":{"kind":"CloudPlace","target":"aws"}', encoded)
            self.assertEqual(status_wire.parse_request(encoded), single)
            grouped = self.cloud_request(fixture, group=self.cloud_group(fixture, "trial", 3))
            encoded_group = status_wire.encode_request(grouped)
            self.assertEqual(status_wire.parse_request(encoded_group), grouped)
            with self.assertRaisesRegex(ValueError, "invalid admission envelope"):
                _ = status_wire.parse_envelope(encoded_group, {"admit": wire.AdmitRequest})

    def test_cloud_starts_with_full_local_memory_and_charges_zero(self) -> None:
        for step in ("gate", "test-affected"):
            with self.subTest(step=step), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                _ = fixture.run_pass()
                before = self.reading(fixture)
                request = self.cloud_request(fixture, step=step)
                grant = only_admitted(fixture.server.handle_request(request))
                self.assertEqual(grant.need, 0)
                self.assertEqual(fixture.server.reservations[request.episode].need, 0)
                after = self.reading(fixture)
                self.assertEqual(after.view.promised, before.view.promised)
                self.assertEqual(after.explanation.room, before.explanation.room)

    def test_cloud_module_progress_updates_without_a_memory_charge(self) -> None:
        from scripts.build_queue import cloud_progress

        with AdmissionFixture() as fixture:
            request = self.cloud_request(fixture)
            sidecar = fixture.root / "cloud.cgroup"
            _ = sidecar.write_text("")
            request = replace(request, sidecar=str(sidecar))
            grant = only_admitted(fixture.server.handle_request(request))
            before = self.reading(fixture)
            for done, state in ((3, "running"), (12, "uploading")):
                current = cloud_progress.CloudProgress(
                    cast(cloud_progress.CloudState, state), 32, 1, fixture.clock.now())
                cloud_progress.publish(sidecar, current)
                counter = progress.ProgressCounter("modules", done, 12)
                progress.publish_counter(sidecar, counter)
                _ = fixture.run_pass()
                reading = self.reading(fixture)
                assert isinstance(reading, wire.CloudQueueRead)
                row, = reading.cloud
                self.assertEqual(row.progress, current)
                self.assertEqual(row.counter, counter)
                self.assertEqual(reading.explanation.room, before.explanation.room)
                self.assertEqual(fixture.server.reservations[request.episode].need, 0)
            fixture.release(grant)

    def test_tenth_cloud_gate_waits_and_starts_after_a_release(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.configure(cloud_gate_slots=9)
            requests = [self.cloud_request(fixture) for _ in range(11)]
            grants = [only_admitted(fixture.server.handle_request(request)) for request in requests[:9]]
            for request in requests[9:]:
                reply, = fixture.server.handle_request(request)
                assert isinstance(reply, wire.Wait)
                self.assertIn("cloud slot", reply.text)
                self.assertIn("9 of 9", reply.text)
            self.assertEqual(len(fixture.server.reservations), 9)
            fixture.release(grants[0])
            _ = fixture.run_pass()
            self.assertIn(requests[9].episode, fixture.server.reservations)
            self.assertNotIn(requests[10].episode, fixture.server.reservations)
            self.assertEqual(len(fixture.server.reservations), 9)
            fixture.release(grants[1])
            _ = fixture.run_pass()
            self.assertIn(requests[10].episode, fixture.server.reservations)

    def test_cloud_slot_limit_reads_configuration_at_use(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, cloud_gate_slots="1")
            _ = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            waiting = self.cloud_request(fixture)
            reply, = fixture.server.handle_request(waiting)
            assert isinstance(reply, wire.Wait)
            self.assertIn("1 of 1", reply.text)
            write_memory_config(config, cloud_gate_slots="2")
            _ = fixture.run_pass()
            self.assertIn(waiting.episode, fixture.server.reservations)
            self.assertEqual(len(fixture.server.reservations), 2)

    def test_dead_route_releases_the_cloud_slot_while_its_child_lives(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", cloud_gate_slots="1")
            route = fixture.run_owner()
            running = self.cloud_request(fixture, run=route)
            _ = only_admitted(fixture.server.handle_request(running))
            waiting = self.cloud_request(fixture)
            reply, = fixture.server.handle_request(waiting)
            self.assertIsInstance(reply, wire.Wait)
            fixture.end_run(route)
            _ = fixture.run_pass()
            self.assertNotIn(running.episode, fixture.server.reservations)
            self.assertIn(waiting.episode, fixture.server.reservations)
            self.assertEqual(fixture.proc.owner_start(running.owner.pid), service.LiveOwnerIdentity(running.owner.start_time))

    def test_dead_cloud_owner_releases_its_slot(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", cloud_gate_slots="1")
            running = self.cloud_request(fixture)
            _ = only_admitted(fixture.server.handle_request(running))
            waiting = self.cloud_request(fixture)
            self.assertIsInstance(fixture.server.handle_request(waiting)[0], wire.Wait)
            fixture.end_owner(running.owner)
            _ = fixture.run_pass()
            self.assertNotIn(running.episode, fixture.server.reservations)
            self.assertIn(waiting.episode, fixture.server.reservations)

    def test_waiting_cloud_gate_does_not_block_a_local_step(self) -> None:
        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", cloud_gate_slots="1")
            _ = only_admitted(fixture.server.handle_request(self.cloud_request(fixture)))
            cloud_waiter = self.cloud_request(fixture)
            self.assertIsInstance(fixture.server.handle_request(cloud_waiter)[0], wire.Wait)
            local = fixture.request(need=GIB)
            grant = only_admitted(fixture.server.handle_request(local))
            self.assertEqual(grant.need, GIB)
            self.assertIn(cloud_waiter.episode, fixture.server.waiters)

    def test_local_memory_wait_does_not_block_a_cloud_gate(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            local = fixture.request(need=GIB)
            self.assertIsInstance(fixture.server.handle_request(local)[0], wire.Wait)
            cloud = self.cloud_request(fixture)
            grant = only_admitted(fixture.server.handle_request(cloud))
            self.assertEqual(grant.need, 0)
            self.assertIn(local.episode, fixture.server.waiters)

    def test_fallback_releases_cloud_and_waits_for_local_memory(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            cloud = self.cloud_request(fixture)
            grant = only_admitted(fixture.server.handle_request(cloud))
            fixture.release(grant)
            local = fixture.request(need=4 * GIB, step="gate")
            reply, = fixture.server.handle_request(local)
            self.assertIsInstance(reply, wire.Wait)
            self.assertFalse(fixture.server.reservations)
            self.assertIn(local.episode, fixture.server.waiters)


class RoomAndSlotTests(unittest.TestCase):

    def test_an_oversized_head_starts_alone_but_unreadable_host_or_pressure_holds_it(self) -> None:
        for blocked in ("clear", "pressure", "unreadable pressure", "unreadable host"):
            with self.subTest(blocked=blocked), AdmissionFixture() as fixture:
                fixture.free(GIB)
                if blocked == "pressure":
                    fixture.hold_starts()
                elif blocked == "unreadable pressure":
                    fixture.hold_starts(unreadable=True)
                elif blocked == "unreadable host":
                    (fixture.root / "meminfo").unlink()
                request = fixture.request(need=100 * GIB)
                replies = fixture.server.handle_request(request)
                if blocked == "clear":
                    grant = only_admitted(replies)
                    self.assertEqual(fixture.server.reservations[request.episode].need, 100 * GIB)
                    self.assertEqual(grant.need, 100 * GIB)
                    result = fixture.server.last_result
                    assert isinstance(result, gate.PassResult)
                    self.assertIsInstance(result.rows[0].decision.room.basis, records.StartsAlone)
                else:
                    self.assertIsInstance(replies[0], wire.Wait)
                    self.assertFalse(fixture.server.reservations)

    def test_an_oversized_head_with_only_its_own_claim_starts_alone(self) -> None:
        for bypass in (False, True):
            with self.subTest(bypass=bypass), AdmissionFixture() as fixture:
                run = fixture.run_owner()
                first = only_admitted(fixture.server.handle_request(fixture.request(run=run, need=6 * GIB)))
                fixture.release(first)
                fixture.free(11 * GIB)
                head = fixture.request(run=run, need=16 * GIB)

                with mock.patch.object(gate, "CONTINUATION_MAY_PASS", bypass):
                    replies = fixture.server.handle_request(head)

                result = fixture.server.last_result
                assert isinstance(result, gate.PassResult)
                decision = result.rows[0].decision
                self.assertEqual(decision.room.room, 2 * GIB)
                self.assertEqual(decision.room.need, 10 * GIB)
                self.assertEqual(decision.room.basis, records.StartsAlone())
                grant = only_admitted(replies)
                self.assertEqual(grant.need, 16 * GIB)
                self.assertEqual(set(fixture.server.reservations), {head.episode})
                self.assertFalse(fixture.server.slots)
                room = fixture.run_pass().policy.room
                assert isinstance(room, records.MemoryRoom)
                self.assertEqual(room.used + room.kept, 16 * GIB)

    def test_an_oversized_head_waits_for_another_runs_slot_to_expire(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = only_admitted(fixture.server.handle_request(fixture.request(run=run, need=6 * GIB)))
            fixture.allow_next_start()
            other = only_admitted(fixture.server.handle_request(fixture.request(run=fixture.run_owner(), need=GIB)))
            fixture.release(first)
            fixture.release(other)
            other_slot = gate.between_steps(fixture.server.slots[other.reservation])
            fixture.free(11 * GIB)
            head = fixture.request(run=run, need=16 * GIB)

            reply, = fixture.server.handle_request(head)
            self.assertIsInstance(reply, wire.Wait)
            self.assertFalse(fixture.server.reservations)
            self.assertIsInstance(fixture.server.slots[first.reservation], records.ContinuationSlot)

            fixture.clock.advance(other_slot.expires_at - fixture.clock.now())
            alone = fixture.run_pass()
            self.assertEqual(alone.rows[0].decision.room.basis, records.StartsAlone())
            self.assertEqual(fixture.server.reservations[head.episode].need, 16 * GIB)
            self.assertFalse(fixture.server.slots)

    def test_existing_host_reading_gets_calm_pressure_without_erasing_deliberate_pressure(self) -> None:
        for blocked in (False, True):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _ = fake_memory_readings(root)
                pressure = root / "pressure"
                if blocked:
                    write_pressure_reading(pressure, full_avg10=20.0)
                else:
                    pressure.unlink()
                with AdmissionFixture(root=root) as fixture:
                    reply = fixture.server.handle_request(fixture.request(need=12 * GIB))[0]
                    if blocked:
                        assert isinstance(reply, wire.Wait)
                        assert isinstance(reply.reason, records.HeadAdmissionStatus)
                        assert isinstance(reply.reason.cause, records.MemoryPressure)
                        self.assertEqual(reply.reason.cause.full_avg10, 20.0)
                        self.assertFalse(fixture.server.reservations)
                    else:
                        self.assertIsInstance(reply, wire.Admitted)

    def test_a_standalone_admission_prevents_another_alone_start(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request(need=6 * GIB)
            _ = only_admitted(fixture.server.handle_request(first))
            fixture.clock.advance(5)
            fixture.free(GIB)
            second = fixture.request(need=100 * GIB)
            self.assertIsInstance(fixture.server.handle_request(second)[0], wire.Wait)
            self.assertEqual(set(fixture.server.reservations), {first.episode})

    def test_new_starts_are_five_seconds_apart_without_a_pressure_event(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request(need=GIB)
            _ = only_admitted(fixture.server.handle_request(first))
            second = fixture.request(need=GIB)
            self.assertIsInstance(fixture.server.handle_request(second)[0], wire.Wait)
            fixture.clock.advance(4.999)
            _ = fixture.run_pass()
            self.assertNotIn(second.episode, fixture.server.reservations)
            fixture.clock.advance(.001)
            _ = fixture.run_pass()
            self.assertIn(second.episode, fixture.server.reservations)

    def test_a_launch_start_spaces_the_next_new_run(self) -> None:
        with AdmissionFixture() as fixture:
            launch = fixture.launch(need=GIB)
            reply, = fixture.server.handle_request(launch)
            self.assertIsInstance(reply, wire.LaunchAdmitted)
            deadline = fixture.clock.now() + admission_policy_fixture().timing.recovery_start_seconds
            self.assertEqual(fixture.server.spacing, records.NextStartAt(deadline))
            request = fixture.request(need=GIB)
            waiting, = fixture.server.handle_request(request)
            assert isinstance(waiting, wire.Wait)
            assert isinstance(waiting.reason, records.HeadAdmissionStatus)
            self.assertEqual(waiting.reason.cause, records.StartSpacingWait(deadline))
            fixture.clock.advance(4.999)
            _ = fixture.run_pass()
            self.assertNotIn(request.episode, fixture.server.reservations)
            fixture.clock.advance(.001)
            _ = fixture.run_pass()
            self.assertIn(request.episode, fixture.server.reservations)

    def test_head_protection_and_a_held_claim_show_the_walks_remaining_room(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.allow_next_start()
            # A live reservation prevents the continuation from starting alone.
            _ = only_admitted(fixture.server.handle_request(fixture.request(need=0)))
            fixture.release(grant)
            fixture.allow_next_start()
            fixture.free(10 * GIB)
            head = fixture.request(need=2 * GIB)
            initial, = fixture.server.handle_request(head)
            assert isinstance(initial, wire.Wait)
            assert isinstance(initial.reason, records.HeadAdmissionStatus)
            self.assertEqual(initial.reason.decision.room.room, GIB)
            self.assertIn("room 1.0 GiB", records.head_wait_text(initial.reason, ZoneInfo("UTC")))
            successor = fixture.request(run=run, need=8 * GIB)
            _ = fixture.server.handle_request(successor)
            result = fixture.run_pass()
            held = fixture.server.waiters[head.episode].last_reply
            assert isinstance(held, wire.Wait)
            self.assertIsInstance(held.reason, records.BehindHead)
            protected = next(row for row in result.rows if row.owner ==
                f"{head.owner.pid}-{head.owner.start_time}")
            self.assertEqual(protected.decision.room.room, GIB)
            claim = fixture.server.waiters[successor.episode].last_reply
            assert isinstance(claim, wire.Wait)
            assert isinstance(claim.reason, records.HeadAdmissionStatus)
            self.assertIsInstance(claim.reason.cause, records.NeedsMemory)
            self.assertEqual(claim.reason.decision.room.room, GIB)
            with mock.patch.object(client, "CLOCK", fixture.clock), mock.patch("sys.stderr", new=io.StringIO()) as output:
                client.WaitOutput()(claim)
            self.assertIn("head: needs 8.0 GiB; room 1.0 GiB", output.getvalue())
            inputs = fixture.server.last_inputs
            policy = fixture.server.last_policy
            assert isinstance(inputs, records.QueueInputs)
            assert isinstance(policy, records.AdmissionPolicy)
            reading = gate.queue_read_from_pass(inputs, policy, result)
            explained = reading.explanation.head
            assert isinstance(explained, records.HeadExplanation)
            self.assertEqual(explained.room, claim.reason.decision.room)
            self.assertEqual(explained.cause, claim.reason.cause)
            self.assertEqual([row.decision.state for row in result.rows], ["hold", "hold"])

    def test_a_held_head_keeps_one_cause_in_the_wait_line_view_and_client(self) -> None:
        for hold, client_reason in (
            ("pressure", "memory pressure"),
            ("spacing", "another step just started; the queue waits a few seconds between starts"),
            ("memory", "free memory"),
            ("expired", "estimate expired"),
            ("unreadable", "memory reading unavailable"),
        ):
            with self.subTest(hold=hold), AdmissionFixture() as fixture:
                _ = only_admitted(fixture.server.handle_request(fixture.request(need=6 * GIB)))
                if hold != "spacing":
                    fixture.allow_next_start()
                if hold == "pressure":
                    fixture.hold_starts()
                elif hold == "memory":
                    fixture.free(10 * GIB)
                elif hold == "unreadable":
                    (fixture.root / "meminfo").unlink()
                request = fixture.request(need=8 * GIB, age=600 if hold == "expired" else 0)
                reply, = fixture.server.handle_request(request)
                assert isinstance(reply, wire.Wait)
                assert isinstance(reply.reason, records.HeadAdmissionStatus)
                viewed, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(viewed, wire.ViewReply)
                explanation = viewed.reading.explanation
                head = explanation.head
                assert isinstance(head, records.HeadExplanation)
                self.assertEqual(head.cause, reply.reason.cause)
                self.assertEqual(head.waiter.reason, reply.reason)
                if hold == "unreadable":
                    client_reason = f"cannot read MemAvailable from {fixture.root / 'meminfo'}"
                self.assertEqual(reply.text, client_reason)
                with mock.patch.object(client, "CLOCK", fixture.clock), mock.patch("sys.stderr", new=io.StringIO()) as output:
                    client.WaitOutput()(reply)
                self.assertIn(records.head_wait_text(reply.reason, user_zone()), output.getvalue())
                self.assertEqual(client.wait_reasons(reply.snapshot), (client_reason,))

    def test_unreadable_pressure_keeps_its_path_in_the_client(self) -> None:
        for unreadable in (True, False):
            with self.subTest(unreadable=unreadable), AdmissionFixture() as fixture:
                path = fixture.root / "pressure"
                if unreadable:
                    _ = path.write_text("full avg10=0\n")
                else:
                    write_pressure_reading(path, full_avg10=20)
                reply, = fixture.server.handle_request(fixture.request())
                assert isinstance(reply, wire.Wait)
                assert isinstance(reply.reason, records.HeadAdmissionStatus)
                viewed, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(viewed, wire.ViewReply)
                with mock.patch.object(client, "CLOCK", fixture.clock), mock.patch("sys.stderr", new=io.StringIO()) as output:
                    client.WaitOutput()(reply)
                if unreadable:
                    expected = f"pressure unreadable: {path} (incomplete)"
                    self.assertEqual(reply.reason.cause, records.PressureUnavailable(expected))
                    self.assertIn(expected, output.getvalue())
                else:
                    self.assertIsInstance(reply.reason.cause, records.MemoryPressure)

    def test_release_keeps_the_hold_and_its_charge_for_all_recorded_step_text(self) -> None:
        cases = (("", "hana"), (" ", "hana"), ("556caf6e-713d-47c0-a8fc-5dd5e8c26ce4", "hana"),
                 ("umask", "hana"), ("$UNIT", "hana"), ("unrecognized", "hana"), ("test", "hana"),
                 ("unrecognized", "local repo"), ("mend", "local repo"))
        for step, repository in cases:
            with self.subTest(step=step, repository=repository), AdmissionFixture() as fixture:
                folder = str(fixture.root / repository)
                request = fixture.request(run=fixture.run_owner(), step=step, need=GIB)
                assert isinstance(request.profile, records.StepProfile)
                request = replace(request, profile=replace(request.profile, repo=repository, worktree=folder),
                                  identity=replace(request.identity, folder=folder))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.release(grant)
                self.assertEqual(len(fixture.server.slots), 1)
                _ = fixture.run_pass()
                inputs = fixture.server.last_inputs
                policy = fixture.server.last_policy
                assert isinstance(inputs, records.QueueInputs) and isinstance(policy, records.AdmissionPolicy)
                room = gate.inputs_room(inputs, policy)
                assert isinstance(room, records.MemoryRoom)
                self.assertEqual(room.kept, GIB)

    def test_release_records_ended_step_identity(self) -> None:
        with AdmissionFixture() as fixture:
            member = staff.StaffRow(staff.StaffId("fixed-director"), "unit", "startup", staff.StaffRoot(),
                                    current_session=staff.StaffId("old-session"))
            first = fixture.request(run=fixture.run_owner(), step="nextest")
            first = replace(first, argv=("cargo", "nextest", "run", "-E", "package(hana)"),
                            identity=replace(first.identity, session_id=member.current_session))
            with mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((member,))):
                grant = only_admitted(fixture.server.handle_request(first))
            fixture.restart()
            fixture.release(grant)
            slot, = fixture.server.slots.values()
            self.assertEqual(cast(dict[str, object], codec.snapshot_value(slot)).get("identity"),
                             {"kind": "EndedStepFacts", "step": "nextest", "package": "hana",
                              "project": "fixture", "folder": str(fixture.root), "staff_id": "fixed-director",
                              "caller_name": "fixture"})
            _ = fixture.run_pass()
            saved = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(saved, queue_snapshot.QueueSnapshot)
            self.assertEqual(saved.inputs.slots, (slot,))
            fixture.restart()
            self.assertEqual(fixture.server.slots, {})

    def test_view_release_and_admit_pass_capture_the_same_continuation_slot(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.allow_next_start()
            other = only_admitted(fixture.server.handle_request(fixture.request(need=GIB)))
            fixture.free(0)
            successor = fixture.request(run=run, need=8 * GIB)
            _ = fixture.server.handle_request(successor)
            fixture.release(grant)
            _ = fixture.run_pass()
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            viewed, = reply.reading.explanation.decided_from.slots
            self.assertIsInstance(viewed, records.ContinuationSlot)
            assert isinstance(viewed, records.ContinuationSlot)
            self.assertEqual(viewed.owner, f"{successor.owner.pid}-{successor.owner.start_time}")
            fixture.release(other)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(inputs.slots, (viewed,))
            result = fixture.run_pass()
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(inputs.slots, (viewed,))
            continuation = next(row for row in result.rows if row.owner == viewed.owner)
            self.assertTrue(continuation.decision.reused_slot)
            self.assertEqual(continuation.decision.room.need, 2 * GIB)


    def test_a_continuation_held_behind_an_earlier_row_gives_its_claim_back(self) -> None:
        for head_gib in (8, 16):
            with self.subTest(head_gib=head_gib), AdmissionFixture() as fixture:
                run = fixture.run_owner()
                first = only_admitted(fixture.server.handle_request(fixture.request(run=run, need=6 * GIB)))
                fixture.allow_next_start()
                running = only_admitted(fixture.server.handle_request(fixture.request(need=GIB)))
                fixture.release(first)
                fixture.allow_next_start()
                fixture.free(11 * GIB)
                head = fixture.request(need=head_gib * GIB, since=fixture.clock.now() - 120)
                initial, = fixture.server.handle_request(head)
                assert isinstance(initial, wire.Wait)
                self.assertEqual(initial.snapshot.decision.room.room, 2 * GIB)

                successor = fixture.request(run=run, need=6 * GIB)
                _ = fixture.server.handle_request(successor)
                self.assertFalse(fixture.server.slots, "the earlier row needs the claim's memory")
                result = fixture.server.last_result
                assert isinstance(result, gate.PassResult)
                self.assertEqual(result.rows[0].decision.room.room, 8 * GIB)
                self.assertFalse(result.rows[1].decision.reused_slot)
                self.assertEqual(result.rows[1].decision.room.need, 6 * GIB)
                self.assertIn(successor.episode, fixture.server.waiters)
                if head_gib == 8:
                    self.assertIn(head.episode, fixture.server.reservations)
                else:
                    self.assertNotIn(head.episode, fixture.server.reservations)
                    fixture.release(running)
                    alone = fixture.run_pass()
                    self.assertEqual(alone.rows[0].decision.room.basis, records.StartsAlone())
                    self.assertIn(head.episode, fixture.server.reservations)

    def test_slot_reuse_can_pass_under_pressure_when_the_switch_is_on(self) -> None:
        for need in (4 * GIB, 6 * GIB):
            for unreadable in (False, True):
                with self.subTest(need=need, unreadable=unreadable), AdmissionFixture() as fixture:
                    run = fixture.run_owner()
                    first = fixture.request(run=run, need=6 * GIB)
                    grant = only_admitted(fixture.server.handle_request(first))
                    fixture.release(grant)
                    fixture.hold_starts(unreadable=unreadable)
                    head = fixture.request(need=GIB)
                    self.assertIsInstance(fixture.server.handle_request(head)[0], wire.Wait)
                    successor = fixture.request(run=run, need=need)
                    with mock.patch.object(gate, "CONTINUATION_MAY_PASS", True):
                        next_grant = only_admitted(fixture.server.handle_request(successor))
                    result = fixture.server.last_result
                    assert isinstance(result, gate.PassResult)
                    reused = next(row for row in result.rows if row.owner ==
                        f"{successor.owner.pid}-{successor.owner.start_time}")
                    self.assertIsInstance(reused.decision.room.basis, records.WithinSlot)
                    self.assertNotEqual(next_grant.reservation, grant.reservation)
                    self.assertEqual(set(fixture.server.reservations), {successor.episode})
                    self.assertEqual(fixture.server.reservations[successor.episode].need, need)
                    self.assertFalse(fixture.server.slots)
                    self.assertIn(head.episode, fixture.server.waiters)
                    fixture.release(next_grant)
                    slot, = fixture.server.slots.values()
                    self.assertEqual(gate.between_steps(slot).bytes, need)

    def test_forecast_and_release_keep_slots_only_for_caller_named_runs(self) -> None:
        from scripts.build_queue.scope_activity import LiveBuildRun

        for caller_named in (False, True):
            with self.subTest(caller_named=caller_named), AdmissionFixture() as fixture:
                run = fixture.run_owner()
                owner = fixture.proc.spawn()
                write_process(fixture.proc.path, owner.pid, parent=run.pid, start=owner.start_time)
                _ = (fixture.proc.path / str(run.pid) / "cmdline").write_bytes(b"bash\0verify.sh\0")
                request = fixture.request(owner=owner, need=6 * GIB, run=run if caller_named else wire.NO_RUN)
                with fixture.loop_turn():
                    grant = only_admitted(fixture.server.handle_request(request))
                    _ = fixture.server.run_pass()
                    inputs = cast(records.QueueInputs, fixture.server.last_inputs)
                    self.assertIsInstance(inputs.reservations[0].get("build_run"), LiveBuildRun)
                    predicted = gate.inputs_after_steps_end(inputs, frozenset({0}), admission_policy_fixture())
                    fixture.release(grant)
                    self.assertEqual(predicted.slots, tuple(fixture.server.slots.values()))
                    if caller_named:
                        self.assertEqual(len(predicted.slots), 1)
                        self.assertEqual(gate.between_steps(predicted.slots[0]).bytes, 6 * GIB)
                    else:
                        self.assertEqual(predicted.slots, ())

    def test_a_larger_next_step_keeps_its_slot_and_gets_extra_before_the_head(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.allow_next_start()
            # A live reservation prevents the continuation from starting alone.
            _ = only_admitted(fixture.server.handle_request(fixture.request(need=0)))
            fixture.release(grant)
            fixture.clock.advance(5)
            fixture.free(10 * GIB)
            fixture.hold_starts()
            head = fixture.request(need=GIB)
            self.assertIsInstance(fixture.server.handle_request(head)[0], wire.Wait)
            successor = fixture.request(run=run, need=8 * GIB)
            self.assertIsInstance(fixture.server.handle_request(successor)[0], wire.Wait)
            fixture.free(10 * GIB)
            _ = fixture.run_pass()
            self.assertNotIn(successor.episode, fixture.server.reservations)
            fixture.free(11 * GIB)
            _ = fixture.run_pass()
            self.assertIn(successor.episode, fixture.server.reservations)
            self.assertNotIn(head.episode, fixture.server.reservations)
            self.assertEqual(fixture.server.reservations[successor.episode].need, 8 * GIB)

    def test_a_gap_expires_at_ten_seconds_and_a_late_step_needs_a_new_start(self) -> None:
        for elapsed, reuse in ((9.999, True), (10, False)):
            with self.subTest(elapsed=elapsed), AdmissionFixture() as fixture:
                run = fixture.run_owner()
                first = fixture.request(run=run, need=6 * GIB)
                grant = only_admitted(fixture.server.handle_request(first))
                fixture.release(grant)
                fixture.clock.advance(elapsed)
                fixture.hold_starts()
                successor = fixture.request(run=run, need=4 * GIB)
                replies = fixture.server.handle_request(successor)
                if reuse:
                    self.assertEqual(only_admitted(replies).need, 4 * GIB)
                else:
                    self.assertIsInstance(replies[0], wire.Wait)
                    self.assertFalse(fixture.server.reservations)

    def test_an_ended_run_cannot_reuse_its_held_slot(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.release(grant)
            fixture.end_run(run)
            fixture.hold_starts()
            successor = fixture.request(run=run, need=4 * GIB)
            self.assertIsInstance(fixture.server.handle_request(successor)[0], wire.Wait)
            self.assertFalse(fixture.server.reservations)

    def test_restart_drops_a_gap_instead_of_restoring_memory_charge(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.release(grant)
            fixture.restart()
            fixture.hold_starts()
            successor = fixture.request(run=run, need=4 * GIB)
            self.assertIsInstance(fixture.server.handle_request(successor)[0], wire.Wait)
            self.assertFalse(fixture.server.reservations)
            self.assertNotIn('"slots"', fixture.state_text())

    def test_closing_a_waiter_connection_frees_its_claim_while_the_run_lives(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, need=6 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.release(grant)
            fixture.hold_starts()
            successor = fixture.request(run=run, need=8 * GIB)
            peer = fixture.connect(successor)
            self.assertTrue(any(isinstance(reply, wire.Wait) for reply in peer.replies()))
            fixture.proc.kill(successor.owner)
            peer.close()
            fixture.server.run_once(0)
            self.assertNotIn(successor.episode, fixture.server.waiters)
            self.assertIsInstance(fixture.proc.owner_start(run.pid), service.LiveOwnerIdentity)
            fixture.clock.advance(10)
            fixture.server.run_once(0)
            later = fixture.request(run=run, need=4 * GIB)
            self.assertIsInstance(fixture.server.handle_request(later)[0], wire.Wait)
            self.assertFalse(fixture.server.reservations)

    def test_a_younger_run_cannot_take_the_room_protected_for_the_head(self) -> None:
        with AdmissionFixture() as fixture:
            active = fixture.request(need=6 * GIB, owner=fixture.proc.spawn(used=0))
            _ = only_admitted(fixture.server.handle_request(active))
            fixture.clock.advance(5)
            fixture.free(15 * GIB)
            head = fixture.request(need=7 * GIB)
            self.assertIsInstance(fixture.server.handle_request(head)[0], wire.Wait)
            younger = fixture.request(need=2 * GIB)
            self.assertIsInstance(fixture.server.handle_request(younger)[0], wire.Wait)
            self.assertEqual(set(fixture.server.reservations), {active.episode})
            fixture.free(16 * GIB)
            _ = fixture.run_pass()
            self.assertIn(head.episode, fixture.server.reservations)
            self.assertNotIn(younger.episode, fixture.server.reservations)

    def test_idle_services_do_not_block_the_alone_start(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.compiler(GIB // 10)
            fixture.free(GIB)
            request = fixture.request(need=100 * GIB)
            grant = only_admitted(fixture.server.handle_request(request))
            self.assertEqual(grant.need, 100 * GIB)
            self.assertEqual(set(fixture.server.reservations), {request.episode})

    def test_the_1222_mend_head_starts_before_three_merge_gates(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.hold_starts()
            mend = fixture.request(need=int(7.3 * GIB), step="mend")
            self.assertIsInstance(fixture.server.handle_request(mend)[0], wire.Wait)
            fixture.clock.advance(1)
            gates = [fixture.request(need=6 * GIB, step="gate") for _ in range(3)]
            for request in gates:
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            fixture.free(11 * GIB)
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {mend.episode})
            self.assertEqual(set(fixture.server.waiters), {request.episode for request in gates})


class QueueLifecycleTests(unittest.TestCase):
    @staticmethod
    def interrupt_append(kind: str) -> Callable[[Path, dict[str, object], float], None]:
        append = gate.append_queue_time

        def append_then_stop(directory: Path, event: dict[str, object], now: float) -> None:
            append(directory, event, now)
            if event.get("kind") == kind:
                raise InjectedCrash("after " + kind + " append")

        return append_then_stop

    def events(self, fixture: AdmissionFixture, kind: str) -> list[dict[str, object]]:
        path = fixture.state_dir / "queue_times.jsonl"
        if not path.exists():
            return []
        with path.open("rb") as stream:
            return [row for row in reversed(list(gate.queue_times_backward(stream))) if row.get("kind") == kind]

    def test_gate_cap_uses_the_same_repository_name_in_the_checkout_and_slot(self) -> None:
        from scripts.production.cli_fixture import ImmutableGitSeed

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed = ImmutableGitSeed.create(root / "seed", "main", "Fixture", "fixture@example.invalid",
                {"tracked": "fixture\n"})
            slot = root / "1"
            _ = subprocess.run(["git", "remote", "set-url", "origin",
                "git@github.com:hanallc/claude_commands.git"], cwd=seed.checkout, check=True, capture_output=True)
            _ = subprocess.run(["git", "worktree", "add", "--detach", str(slot)], cwd=seed.checkout,
                               check=True, capture_output=True)
            given = str(root / ".claude")
            for worktree in (seed.checkout, slot):
                for step in ("gate", "test-affected"):
                    with self.subTest(worktree=worktree, step=step):
                        self.assertEqual(service.gate_repository_name(step, given, str(worktree)), "claude_commands")
            self.assertEqual(service.gate_repository_name("gate", given, str(root / "missing")), given)

    def test_gate_admission_keeps_original_arrival_and_uncut_need(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request(step="gate", since=fixture.clock.now() - 30)
            self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            fixture.clock.advance(10)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            granted = only_admitted([fixture.server.ledger[request.episode].reply])
            rows = self.events(fixture, "admitted")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual((row["pid"], row["start_time"], row["since"]),
                             (request.owner.pid, request.owner.start_time, request.since))
            self.assertEqual(row["reservation_id"], granted.reservation)
            self.assertEqual(row["admitted_at"], fixture.clock.now())
            self.assertEqual(row["wait_seconds"], 40)
            self.assertEqual(row["need"], request.profile.peak.bytes)
            self.assertEqual(row["need_source"], request.profile.peak.source)
            self.assertNotIn("budget", row)
            self.assertTrue(row["is_head"])

    def test_admitted_row_counts_the_token_waiting_row_it_passed(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request(since=fixture.clock.now() - 5)
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.clock.advance(10)
            _ = fixture.server.handle_request(wire.TokenWaitRequest("token-wait", grant.reservation))
            waiting = fixture.server.handle_request(replace(first, episode="token-wait-reconnect"))[0]
            assert isinstance(waiting, wire.Wait)
            self.assertIsInstance(waiting.reason, records.TokenWaitingWaiter)
            later = fixture.request(since=fixture.clock.now())
            _ = only_admitted(fixture.server.handle_request(later))
            rows = {row["pid"]: row for row in self.events(fixture, "admitted")}
            self.assertEqual(rows[later.owner.pid]["passed"],
                             codec.snapshot_value(gate.PassedRows(0, 1, 0, 0, 0, 0)))
            # A token-waiting row is not a queued row, so the later step is still the first queued row.
            self.assertTrue(rows[later.owner.pid]["is_head"])

    def test_admitted_event_records_the_room_and_growth_loan_from_the_walk(self) -> None:
        _ = self.enterContext(mock.patch.object(store, "host_name", return_value="natedev"))
        for need, fit in ((GIB, "free"), (2 * GIB, "loan")):
            with self.subTest(fit=fit), AdmissionFixture() as fixture:
                fixture.configure(build_reserve_share_percent=80)
                owner = fixture.proc.spawn()
                sidecar = fixture.scope(owner)
                running = replace(fixture.request(owner=owner, need=6 * GIB, step="lint"), sidecar=str(sidecar))
                _ = only_admitted(fixture.server.handle_request(running))
                fixture.allow_next_start()
                fixture.free(9 * GIB)
                grant = only_admitted(fixture.server.handle_request(fixture.request(need=need, step="lint")))
                row = self.events(fixture, "admitted")[-1]
                self.assertEqual(row["reservation_id"], grant.reservation)
                self.assertEqual((row["need"], row["room"], row["loan"], row["fit"]), (need, 2 * GIB, GIB, fit))

    def test_loan_calculation_failure_keeps_the_grant(self) -> None:
        _ = self.enterContext(mock.patch.object(store, "host_name", return_value="natedev"))
        with AdmissionFixture() as fixture:
            with (mock.patch.object(records, "growth_loan_bytes", side_effect=RuntimeError("fixture fault")) as loan,
                  redirect_stderr(io.StringIO()) as journal):
                grant = only_admitted(fixture.server.handle_request(fixture.request(step="lint")))
            loan.assert_called_once()
            self.assertEqual(journal.getvalue().splitlines().count(
                "memory admission: cannot calculate the growth loan: fixture fault"), 1)
            row, = self.events(fixture, "admitted")
            self.assertEqual(row["reservation_id"], grant.reservation)
            self.assertFalse({"room", "loan", "fit"} & row.keys())

    def test_continuation_grant_omits_fit_when_memory_is_unavailable(self) -> None:
        _ = self.enterContext(mock.patch.object(store, "host_name", return_value="natedev"))
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            grant = only_admitted(fixture.server.handle_request(fixture.request(run=run, need=6 * GIB)))
            fixture.release(grant)
            fixture.readings.host_path.unlink()
            successor = fixture.request(run=run, need=6 * GIB)
            continuation = only_admitted(fixture.server.handle_request(successor))
            result = fixture.server.last_result
            assert isinstance(result, gate.PassResult)
            self.assertIsInstance(result.policy.room, records.ReadingUnavailable)
            row = self.events(fixture, "admitted")[-1]
            self.assertEqual(row["reservation_id"], continuation.reservation)
            self.assertFalse({"room", "loan", "fit"} & row.keys())

    def test_admitted_event_omits_fit_on_another_host(self) -> None:
        _ = self.enterContext(mock.patch.object(store, "host_name", return_value="linux-guest"))
        with AdmissionFixture() as fixture:
            grant = only_admitted(fixture.server.handle_request(fixture.request(step="lint")))
            result = fixture.server.last_result
            assert isinstance(result, gate.PassResult)
            self.assertIsInstance(result.policy.room, records.MemoryRoom)
            row, = self.events(fixture, "admitted")
            self.assertEqual(row["reservation_id"], grant.reservation)
            self.assertFalse({"room", "loan", "fit"} & row.keys())

    def test_outbox_admission_loads_with_and_without_telemetry(self) -> None:
        current = service.PendingAdmission("episode", "measured", True, 1.0, gate.PassedRows(1, 0, 0, 0, 0, 0),
                                           service.AdmissionFit(11, 3, "loan"))
        state = service.ServiceState({}, {}, {}, records.ReadyToStart(), lifecycle_outbox=(current,))
        saved = cast(dict[str, object], codec.snapshot_value(state))
        entries = cast(list[dict[str, object]], saved["lifecycle_outbox"])
        entries.append({key: value for key, value in entries[0].items() if key not in ("passed", "memory_fit")})
        loaded = service.parse_service_state(json.dumps(saved))
        self.assertEqual(loaded.lifecycle_outbox, (current,
            replace(current, passed=gate.PassedNotRecorded(), memory_fit=None)))

    def test_admission_recovery_keeps_one_event_for_the_durable_grant(self) -> None:
        for boundary in ("after_state_write", "after_admitted_append"):
            with self.subTest(boundary=boundary), AdmissionFixture() as fixture:
                request = fixture.request(step="gate", since=fixture.clock.now() - 7)
                fixture.fault.stage = boundary
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(request)
                admitted_at = fixture.clock.now()
                fixture.clock.advance(10)
                fixture.restart()
                grant = only_admitted(fixture.server.handle_request(request))
                if request.episode not in fixture.server.reservations:
                    fixture.allow_next_start()
                self.assertEqual(only_admitted(fixture.server.handle_request(request)), grant)
                rows = self.events(fixture, "admitted")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reservation_id"], grant.reservation)
                self.assertEqual(rows[0]["admitted_at"], admitted_at)
                self.assertEqual(rows[0]["since"], request.since)
                self.assertEqual(rows[0]["wait_seconds"], 7)

    def test_gate_exit_before_a_positive_sample_still_records_its_end(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="gate")
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.clock.advance(3)
            _ = fixture.server.handle_request(wire.ReleaseRequest("gate-end", grant.reservation, 0))
            rows = self.events(fixture, "ended")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reservation_id"], grant.reservation)
            self.assertEqual(rows[0]["since"], request.since)
            self.assertEqual(rows[0]["ended_at"], fixture.clock.now())
            self.assertEqual(rows[0]["end_kind"], {"kind": "StepEnd"})
            self.assertEqual(rows[0]["peak"], {"kind": "NoPositiveSample"})
            self.assertFalse(fixture.history())

    def test_token_return_recovery_publishes_one_end_for_each_reservation(self) -> None:
        for boundary in ("after_finalization_intent", "after_history_append"):
            with self.subTest(boundary=boundary), AdmissionFixture() as fixture:
                request = fixture.request()
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(grant)
                fixture.clock.advance(2)
                token = wire.TokenWaitRequest("token-crash", grant.reservation)
                fixture.fault.stage = boundary
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(token)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(token), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(token), [wire.Ok()])
                rows = self.events(fixture, "ended")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reservation_id"], grant.reservation)
                self.assertEqual(rows[0]["end_kind"], {"kind": "TokenReturn"})
                fixture.clock.advance(3)
                again = replace(request, episode="recovered", token_back_at=fixture.clock.now())
                if again.episode not in fixture.server.reservations:
                    fixture.allow_next_start()
                later = only_admitted(fixture.server.handle_request(again))
                self.assertNotEqual(later.reservation, grant.reservation)
                self.assertEqual(len(self.events(fixture, "admitted")), 2)
                lifetime, = gate.read_queue_lifetimes(fixture.state_dir)
                self.assertEqual((lifetime.pid, lifetime.start_time, lifetime.since),
                                 (request.owner.pid, request.owner.start_time, request.since))
                self.assertEqual([interval.reservation_id for interval in lifetime.intervals],
                                 [grant.reservation, later.reservation])
                self.assertEqual(len(lifetime.token_gaps), 1)
                self.assertIsInstance(lifetime.end, gate.StepStillOpen)

    def test_a_handoff_saved_before_a_crash_ends_as_a_handoff_after_the_restart(self) -> None:
        for boundary in ("after_state_write", "after_finalization_intent"):
            with self.subTest(boundary=boundary), AdmissionFixture() as fixture:
                request = fixture.request()
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(grant)
                fixture.clock.advance(2)
                handoff = wire.ReleaseRequest("handoff-crash", grant.reservation, 0, "handed_off")
                fixture.fault.stage = boundary
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(handoff)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(handoff), [wire.Ok()])
                ended, = self.events(fixture, "ended")
                self.assertEqual((ended["reservation_id"], ended["outcome"], ended["end_kind"]),
                                 (grant.reservation, "handed_off", {"kind": "HandOff"}))
                self.assertFalse(fixture.history())

    def test_an_end_append_retry_preserves_the_saved_time_and_publishes_once(self) -> None:
        for sampled in (False, True):
            with self.subTest(sampled=sampled), AdmissionFixture() as fixture:
                request = fixture.request(step="gate")
                if sampled:
                    request = replace(request, sidecar=str(fixture.scope(request.owner)))
                grant = only_admitted(fixture.server.handle_request(request))
                if sampled:
                    fixture.scoped_peak(grant, 2 * GIB)
                fixture.clock.advance(5)
                ended_at = fixture.clock.now()
                release = wire.ReleaseRequest("end-crash", grant.reservation, 137)
                with mock.patch.object(gate, "append_queue_time", side_effect=self.interrupt_append("ended")):
                    with self.assertRaises(InjectedCrash):
                        _ = fixture.server.handle_request(release)
                fixture.clock.advance(100)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                rows = self.events(fixture, "ended")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reservation_id"], grant.reservation)
                self.assertEqual(rows[0]["ended_at"], ended_at)
                self.assertEqual(rows[0]["outcome"], "killed")
                self.assertEqual(rows[0]["end_kind"], {"kind": "StepEnd"})
                self.assertEqual(len(fixture.history()), int(sampled))

    def test_cancellation_keeps_its_end_after_the_owner_exits(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request(step="gate")
            peer = fixture.connect(request)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            fixture.clock.advance(10)
            cancelled_at = fixture.clock.now()
            peer.close()
            fixture.server.run_once(0)
            fixture.clock.advance(100)
            fixture.end_owner(request.owner)
            fixture.restart()
            rows = self.events(fixture, "left")
            self.assertEqual([(row["reason"], row["ended_at"]) for row in rows],
                             [("cancelled", cancelled_at)])
            lifetime, = gate.read_queue_lifetimes(fixture.state_dir)
            self.assertEqual(lifetime.end, gate.LeftQueue(cancelled_at, "cancelled"))
            self.assertEqual(lifetime.memory_wait_s, 10)
            self.assertFalse(self.events(fixture, "ended"))

    def test_estimate_refresh_on_its_connection_keeps_the_place(self) -> None:
        for estimator in ("client", "service"):
            with self.subTest(estimator=estimator), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                age = records.read_memory_config().policy.timing.estimate_refresh_seconds + 1
                request = (fixture.request(step="gate", age=age) if estimator == "client"
                           else fixture.service_request())
                peer = fixture.connect(request)
                initial = peer.replies()[-1]
                self.assertIsInstance(initial, wire.Wait)
                if isinstance(request, wire.ServiceEstimateRequest):
                    fixture.clock.advance(age)
                    _ = fixture.run_pass()
                    expired = peer.replies()[-1]
                else:
                    expired = initial
                assert isinstance(expired, wire.Wait)
                self.assertIsInstance((expired.reason.cause if isinstance(expired.reason, records.HeadAdmissionStatus)
                                       else expired.reason), records.EstimateExpired)
                since = fixture.server.waiters[request.episode].row["since"]
                places = set(fixture.server.places)
                estimated_at = fixture.server.waiters[request.episode].request.profile.estimated_at
                fixture.clock.advance(10)
                refreshed = (replace(request, profile=replace(request.profile, estimated_at=fixture.clock.now()))
                             if isinstance(request, wire.AdmitRequest) else request)
                peer.socket.sendall(wire.encode_request(refreshed).encode())
                reply = peer.replies()[-1]
                assert isinstance(reply, wire.Wait)
                self.assertNotIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus)
                                          else reply.reason), records.EstimateExpired)
                self.assertGreater(fixture.server.waiters[request.episode].request.profile.estimated_at, estimated_at)
                self.assertEqual(self.events(fixture, "left"), [])
                self.assertEqual(fixture.server.waiters[request.episode].row["since"], since)
                self.assertEqual(set(fixture.server.places), places)

    def test_restart_after_reservation_removal_keeps_the_completed_end(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="gate")
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.clock.advance(5)
            ended_at = fixture.clock.now()
            clear = service.StateIntents.clear

            def clear_then_stop(intents: service.StateIntents, row: records.Reservation) -> None:
                clear(intents, row)
                raise InjectedCrash("after reservation removal")

            with mock.patch.object(service.StateIntents, "clear", new=clear_then_stop):
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("clear-stop", grant.reservation, 0))
            fixture.proc.kill(request.owner)
            fixture.clock.advance(100)
            fixture.restart()
            self.assertFalse(self.events(fixture, "left"))
            rows = self.events(fixture, "ended")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reservation_id"], grant.reservation)
            lifetime, = gate.read_queue_lifetimes(fixture.state_dir)
            self.assertEqual(lifetime.end,
                gate.ReservationEnded(ended_at, "completed", gate.StepEnd(), gate.NoPositiveSample()))

    def test_restart_retries_an_interrupted_cancellation_with_its_original_time(self) -> None:
        for appended in (False, True):
            with self.subTest(appended=appended), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request(step="gate")
                peer = fixture.connect(request)
                self.assertIsInstance(peer.replies()[0], wire.Wait)
                fixture.clock.advance(10)
                cancelled_at = fixture.clock.now()
                failure = self.interrupt_append("left") if appended else InjectedCrash("before cancellation append")
                with mock.patch.object(gate, "append_queue_time", side_effect=failure):
                    peer.close()
                    with self.assertRaises(InjectedCrash):
                        fixture.server.run_once(0)
                fixture.clock.advance(100)
                fixture.restart()
                fixture.end_owner(request.owner)
                fixture.restart()
                rows = self.events(fixture, "left")
                self.assertEqual([(row["reason"], row["ended_at"]) for row in rows],
                                 [("cancelled", cancelled_at)])
                lifetime, = gate.read_queue_lifetimes(fixture.state_dir)
                self.assertEqual(lifetime.end, gate.LeftQueue(cancelled_at, "cancelled"))
                self.assertEqual(lifetime.memory_wait_s, 10)
                self.assertFalse(self.events(fixture, "ended"))


    def test_a_waiting_owner_exit_and_socket_cancel_record_distinct_left_reasons(self) -> None:
        for reason in ("cancelled", "owner_gone"):
            with self.subTest(reason=reason), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request(step="gate")
                peer = fixture.connect(request)
                self.assertIsInstance(peer.replies()[0], wire.Wait)
                place, = fixture.server.places.values()
                identity = place.queue_identity
                fixture.restart()
                restored, = fixture.server.places.values()
                self.assertEqual(restored.queue_identity, identity)
                peer = fixture.connect(request)
                self.assertIsInstance(peer.replies()[0], wire.Wait)
                fixture.clock.advance(4)
                if reason == "cancelled":
                    peer.close()
                    fixture.server.run_once(0)
                else:
                    fixture.end_owner(request.owner)
                rows = self.events(fixture, "left")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reason"], reason)
                self.assertEqual((rows[0]["pid"], rows[0]["start_time"], rows[0]["since"]),
                                 (request.owner.pid, request.owner.start_time, request.since))
                self.assertEqual({name: rows[0][name] for name in identity}, identity)
                self.assertFalse(self.events(fixture, "ended"))
                self.assertFalse(fixture.history())


class CiSliceAdmissionTests(unittest.TestCase):

    def test_ci_worker_uses_the_current_reservation_and_waiter_identity(self) -> None:
        from scripts.build_queue.scope_activity import IdleService, RunningServiceJob, WaitingServiceJob, UnmatchedServiceJob

        for admitted, expected in ((True, RunningServiceJob), (False, WaitingServiceJob)):
            with self.subTest(admitted=admitted), AdmissionFixture() as fixture:
                owner = fixture.fixture_owner(700001, "100")
                write_process(fixture.proc.path, 700010)
                _ = (fixture.proc.path / "700010/comm").write_text("Runner.Worker\n")
                write_process(fixture.proc.path, owner.pid, parent=700010, start=owner.start_time)
                _ = (fixture.root / "ci.slice/cgroup.procs").write_text(f"700010\n{owner.pid}\n")
                if not admitted:
                    _ = only_admitted(fixture.server.handle_request(
                        fixture.request(owner=fixture.fixture_owner(700020, "200"), need=GIB)))
                request = fixture.request(owner=owner, need=GIB if admitted else 100 * GIB)
                reply = fixture.server.handle_request(request)
                self.assertEqual(any(isinstance(item, wire.Admitted) for item in reply), admitted)
                _ = fixture.run_pass()
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                ci, = (step.measured for step in view.reading.view.running
                       if isinstance(step.measured, records.UnownedSliceMemory)
                       and step.measured.cgroup == str(fixture.root / "ci.slice"))
                self.assertIsInstance(ci.service_job_state, expected)
                self.assertIsInstance(ci.job_state, IdleService if admitted else RunningServiceJob)
                self.assertEqual(len(view.reading.view.waiting), int(not admitted))
                if admitted:
                    grant = only_admitted(reply)
                    fixture.release(grant)
                else:
                    fixture.server.waiters.clear()
                _ = fixture.run_pass()
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                ci, = (step.measured for step in view.reading.view.running
                       if isinstance(step.measured, records.UnownedSliceMemory)
                       and step.measured.cgroup == str(fixture.root / "ci.slice"))
                self.assertIsInstance(ci.service_job_state, UnmatchedServiceJob)
                self.assertIsInstance(ci.job_state, RunningServiceJob)

    def test_ci_evidence_does_not_subtract_host_free_memory_twice(self) -> None:
        for shared, ci_bytes, run_bytes in ((0, 4, 0), (4, 0, 4)):
            with self.subTest(shared=shared), AdmissionFixture() as fixture:
                _ = (fixture.root / "ci.slice/memory.current").write_text(str(4 * GIB))
                _ = (fixture.root / "ci.slice/memory.stat").write_text(
                    f"anon {(4 - shared) * GIB}\nshmem {shared * GIB}\ninactive_file 0\n")
                request = fixture.request(need=18 * GIB)
                if shared:
                    sidecar = fixture.scope(request.owner, folder="ci.slice/job.scope", current=4 * GIB, anonymous=0)
                    scope = Path(sidecar.read_text())
                    _ = (scope / "memory.stat").write_text(f"anon 0\nshmem {4 * GIB}\ninactive_file 0\n")
                    _ = (fixture.root / "ci.slice/cgroup.procs").write_text(str(request.owner.pid))
                    request = replace(request, sidecar=str(sidecar))
                grant = only_admitted(fixture.server.handle_request(request))
                self.assertEqual(grant.need, 18 * GIB)
                if shared:
                    _ = fixture.run_pass()
                reply, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(reply, wire.ViewReply)
                ci, = (step for step in reply.reading.view.running
                       if isinstance(step.measured, records.UnownedSliceMemory)
                       and step.measured.cgroup == str(fixture.root / "ci.slice"))
                assert isinstance(ci.measured, records.UnownedSliceMemory)
                self.assertEqual(ci.measured.bytes, ci_bytes * GIB)
                # The swap-backed use takes out the same queued scope as the working set.
                self.assertEqual(ci.measured.anonymous, records.AnonymousRunUse(ci_bytes * GIB))
                room = reply.reading.explanation.room
                assert isinstance(room, records.MemoryRoom)
                self.assertEqual(room.available, 60 * GIB)
                self.assertEqual(room.used, run_bytes * GIB)
                self.assertEqual(room.ci_use, ci_bytes * GIB)
                self.assertEqual(room.cap, (60 + ci_bytes + run_bytes - 3) * GIB)

    def test_missing_ci_limit_does_not_block_grants_or_cleanup(self) -> None:
        with AdmissionFixture() as fixture:
            (fixture.root / "ci.slice/memory.max").unlink()
            request = fixture.request(need=GIB)
            grant = only_admitted(fixture.server.handle_request(request))
            self.assertEqual(fixture.server.handle_request(
                wire.ReleaseRequest("ci-cleanup", grant.reservation, 0)), [wire.Ok()])
            self.assertNotIn(request.episode, fixture.server.reservations)


class ServiceTests(unittest.TestCase):

    def test_merge_gate_keeps_the_explicit_unit_from_another_director(self) -> None:
        runner = staff.StaffRow(staff.StaffId("runner"), "showrunner", "fixture", staff.StaffRoot(),
                                current_session=staff.StaffId("runner-session"))
        units = tuple(staff.StaffRow(staff.StaffId(f"director-{name}"), "unit", name, staff.ReportsTo(runner.id),
                                    current_session=staff.StaffId(f"session-{name}"),
                                    unit_type=staff.UnitDetails("direct")) for name in ("a", "b"))
        members = staff.StaffSnapshot((runner, *units))
        with AdmissionFixture() as fixture, mock.patch.object(staff, "local_snapshot", return_value=members), \
                mock.patch.object(staff, "snapshot", return_value=members):
            fixture.free(0)
            fixture.hold_starts()
            older = fixture.request(since=fixture.clock.now() - 1)
            _ = fixture.server.handle_request(older)
            request = fixture.request(step="gate")
            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": "session-b",
                                              "BUILDLOG_UNIT": "a"}):
                request = replace(request, identity=client.step_identity("gate", request.cwd))
            _ = fixture.server.handle_request(request)
            younger = fixture.request(since=fixture.clock.now() + 1)
            younger = replace(younger, identity=replace(younger.identity, unit="a"))
            _ = fixture.server.handle_request(younger)
            self.assertEqual(fixture.completed_queue_change(
                wire.PrioritySetRequest("gate-priority", "a", "user work", "director")), [wire.Ok()])
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            self.assertEqual([(step.pid, step.unit) for step in reply.reading.view.waiting],
                             [(request.owner.pid, "a"), (younger.owner.pid, "a"), (older.owner.pid, "")])

    def test_unit_priority_leaves_a_same_name_showrunner_in_normal_order(self) -> None:
        production = staff.StaffRow(staff.StaffId("production-id"), "showrunner", "research", staff.StaffRoot(),
                                    current_session=staff.StaffId("production-session"))
        unit = staff.StaffRow(staff.StaffId("unit-id"), "unit", "research", staff.ReportsTo(production.id),
                              current_session=staff.StaffId("unit-session"), unit_type=staff.UnitDetails("direct"))
        members = staff.StaffSnapshot((production, unit))
        with AdmissionFixture() as fixture, mock.patch.object(staff, "local_snapshot", return_value=members), \
                mock.patch.object(staff, "snapshot", return_value=members):
            fixture.free(0)
            fixture.hold_starts()
            owners: list[int] = []
            for member in (production, unit):
                with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": member.current_session,
                                                  "BUILDLOG_UNIT": ""}):
                    request = fixture.request(since=fixture.clock.now() + len(owners))
                    request = replace(request, identity=client.step_identity("nextest", request.cwd))
                owners.append(request.owner.pid)
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.completed_queue_change(
                wire.PrioritySetRequest("priority", unit.name, "user work", "director")), [wire.Ok()])
            reply, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(reply, wire.ViewReply)
            self.assertEqual([step.pid for step in reply.reading.view.waiting], owners[::-1])
            self.assertEqual([(step.session_name, step.unit) for step in reply.reading.view.waiting],
                             [("research", "research"), ("research", "")])

    def test_an_unknown_request_caller_recovers_when_staff_data_is_available(self) -> None:
        member = staff.StaffRow(staff.StaffId("director"), "unit", "known director", staff.StaffRoot(),
                                current_session=staff.StaffId("director-session"))
        members = staff.StaffSnapshot((member,))
        for supplied in ("—", " \t", "$CALLER", "unregistered session", "recorded caller"):
            with self.subTest(supplied=supplied), AdmissionFixture() as fixture:
                request = fixture.request(need=GIB)
                with mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())), \
                        mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": member.current_session,
                                                  "CLAUDE_SESSION_NAME": "", "CLAUDE_SESSION_FOLDER": "", "BUILDLOG_UNIT": ""}):
                    identity = client.step_identity(request.identity.step, request.cwd)
                self.assertEqual(identity.name, "unregistered session")
                request = replace(request, identity=replace(identity, name=supplied))
                with mock.patch.object(staff, "local_snapshot", return_value=members):
                    _ = only_admitted(fixture.connect(request).replies())
                reply, = fixture.connect(wire.ViewRequest()).replies()
                assert isinstance(reply, wire.ViewReply)
                row, = [step for step in reply.reading.view.running if step.pid == request.owner.pid]
                expected = supplied if supplied == "recorded caller" else member.name
                self.assertEqual((row.session_name, row.unit), (expected, member.name))
                saved = fixture.server.reservations[request.episode]
                self.assertEqual(saved.queue_waiter.get("session_name"), expected)

    def test_service_names_an_empty_admission(self) -> None:
        with AdmissionFixture() as fixture, mock.patch.object(service, "deliver_source_refusal"):
            request = fixture.request(need=GIB)
            request = replace(request, identity=replace(request.identity, session_id="", name=""))
            self.assertEqual(fixture.connect(request).replies(), [
                wire.Refused("Refused: this request names no source. It gives no session id, staff id or name.")])
            self.assertNotIn(request.episode, fixture.server.waiters)

    def test_service_refuses_and_reports_an_unresolved_source_until_admitted(self) -> None:
        with AdmissionFixture() as fixture, \
                mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())):
            fixture.clock.epoch = time.time()
            threads: list[threading.Thread] = []
            messages: list[tuple[str, str]] = []

            def send(recipient: str, message: str) -> bool:
                messages.append((recipient, message))
                return True

            def record_thread(*, target: Callable[..., None], args: tuple[object, ...],
                              name: str, daemon: bool) -> threading.Thread:
                thread = threading.Thread(target=target, args=args, name=name, daemon=daemon)
                threads.append(thread)
                return thread

            def refuse(request: wire.AdmitRequest, given: str) -> None:
                request = replace(request, identity=replace(request.identity, session_id=given, name=""))
                self.assertEqual(fixture.connect(request).replies(), [wire.Refused(
                    f"Refused: this request names no source. The session id '{given}' resolves to no staff row "
                    + "and no session name. Run it from a named session, or set CLAUDE_SESSION_NAME.")])
                for thread in threads:
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive())

            with mock.patch.object(service, "threading", mock.Mock(wraps=threading, Thread=record_thread)), \
                    mock.patch.object(service, "send_admission_alert", side_effect=send):
                causes = (("missing-session", "cargo"), ("another-session", "cargo"), ("missing-session", "rustc"))

                def refuse_causes() -> None:
                    for given, program in causes:
                        refuse(replace(fixture.request(need=GIB), argv=(program, "build")), given)

                refuse_causes()
                self.assertEqual(len(messages), 3)
                for (given, program), (recipient, message) in zip(causes, messages, strict=True):
                    self.assertEqual(recipient, "bob")
                    self.assertIn(f"Given: session id '{given}' (resolves to nothing).", message)
                    self.assertIn(f"Command: {program} build.", message)
                    self.assertIn(f"Repository: fixture. Folder: {fixture.root}.", message)
                refuse(replace(fixture.request(need=GIB), argv=("cargo", "check")), "missing-session")
                self.assertEqual(len(messages), 3)

                fixture.clock.advance(5)
                _ = only_admitted(fixture.connect(fixture.request(need=GIB)).replies())
                refuse_causes()
                self.assertEqual(len(messages), 6)

    def test_service_admits_a_showrunner_without_a_unit(self) -> None:
        member = staff.StaffRow(staff.StaffId("showrunner"), "showrunner", "cargo-liner", staff.StaffRoot(),
                                current_session=staff.StaffId("4dbdb78e-a83b-4248-b8f9-ff278dcbc60c"))
        with AdmissionFixture() as fixture, \
                mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot((member,))), \
                mock.patch.object(service, "send_admission_alert") as send:
            request = fixture.request(need=GIB)
            request = replace(request, identity=replace(request.identity, session_id=member.current_session, name=""))
            _ = only_admitted(fixture.connect(request).replies())
            row = fixture.server.reservations[request.episode].queue_waiter
            self.assertEqual((row.get("session_name"), row.get("unit")), ("cargo-liner", ""))
            send.assert_not_called()

    def test_service_admits_named_sources_without_a_staff_row(self) -> None:
        for name, session in (("terminal", ""), ("nightly", ""), ("manual session", "missing-session")):
            with self.subTest(name=name), AdmissionFixture() as fixture, \
                    mock.patch.object(staff, "local_snapshot", return_value=staff.StaffSnapshot(())), \
                    mock.patch.object(service, "send_admission_alert") as send:
                request = fixture.request(need=GIB)
                request = replace(request, identity=replace(request.identity, session_id=session, name=name))
                _ = only_admitted(fixture.connect(request).replies())
                row = fixture.server.reservations[request.episode].queue_waiter
                self.assertEqual(row.get("session_name"), name)
                send.assert_not_called()

    def test_service_restores_an_empty_saved_name(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=GIB)
            _ = only_admitted(fixture.connect(request).replies())
            record = fixture.server.reservations[request.episode]
            state = service.read_state(fixture.state_dir)
            state.reservations[request.episode] = replace(record, queue_waiter={**record.queue_waiter, "session_name": ""})
            _ = (fixture.state_dir / "state.json").write_text(wire.encode_record(state))
            fixture.restart()
            reply, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(reply, wire.ViewReply)
            row, = [step for step in reply.reading.view.running if step.pid == request.owner.pid]
            self.assertEqual(row.session_name, "unregistered session")
            saved = service.read_state(fixture.state_dir).reservations[request.episode]
            self.assertEqual(saved.queue_waiter.get("session_name"), row.session_name)

    def test_restore_uses_one_staff_snapshot_for_all_saved_callers(self) -> None:
        director = staff.StaffRow(staff.StaffId("director"), "unit", "fixture unit", staff.StaffRoot(),
                                  current_session=staff.StaffId("fixture-session"))
        members = staff.StaffSnapshot((director,))
        with AdmissionFixture() as fixture:
            for _ in range(2):
                request = fixture.request(need=GIB)
                _ = only_admitted(fixture.connect(request).replies())
                fixture.clock.advance(5)
            state = service.read_state(fixture.state_dir)
            for episode, record in state.reservations.items():
                state.reservations[episode] = replace(record, queue_waiter={
                    **record.queue_waiter, "session_name": "", "session_id": director.current_session})
            _ = (fixture.state_dir / "state.json").write_text(wire.encode_record(state))
            with mock.patch.object(staff, "local_snapshot", return_value=members) as read_staff:
                fixture.restart()
            read_staff.assert_called_once_with()
            self.assertEqual([record.queue_waiter.get("session_name") for record in fixture.server.reservations.values()],
                             ["fixture unit", "fixture unit"])

    def test_service_names_placement_without_an_identity(self) -> None:
        from scripts.build_hold.owning_session import NoOwningSession
        from scripts.build_queue.test_placement import placement_request

        with AdmissionFixture() as fixture, mock.patch.object(service, "deliver_source_refusal"):
            request = replace(placement_request(fixture, session=NoOwningSession()),
                              identity=wire.Identity("", "", "", ""))
            self.assertEqual(fixture.server.handle_request(request), [
                wire.Refused("Refused: this request names no source. It gives no session id, staff id or name.")])
            self.assertNotIn(request.episode, fixture.server.placement_requests)

    def assert_reload_hash_journal(self, journal: str, next_check: float) -> str:
        provision, schedule, ready, revision = journal.splitlines()
        self.assertEqual(provision, f"memory admission: Mac Linux state root ready at {Path.home() / '.local/state/build-targets'}")
        self.assertEqual(schedule, f"memory admission: source watch checks 1 paths first at monotonic {next_check:.3f}")
        self.assertRegex(ready, r"^memory admission: reload ready, source hash [a-f0-9]{64}$")
        self.assertEqual(revision, "memory admission: ready with git revision unavailable")
        return ready

    def test_reload_hash_changes_when_a_watched_staff_source_changes(self) -> None:
        with AdmissionFixture() as fixture:
            sources = fixture.root / "scripts"
            for relative in (*admission_code.ADMISSION_CODE_PATHS, "production/staff.py"):
                path = sources / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text("old\n")
            watched = sources / "production/staff.py"

            def ready_line() -> str:
                server = fixture.new_server(start=False)
                server.source_root = sources
                info = watched.stat()
                server.sources = {watched: (info.st_ino, info.st_mtime_ns)}
                try:
                    with mock.patch.dict(os.environ, {service.ADMISSION_SOURCE_RELOAD: "1"}), \
                            redirect_stderr(io.StringIO()) as journal:
                        server.start()
                    return self.assert_reload_hash_journal(journal.getvalue(), server.next_source_check)
                finally:
                    server.close()

            fixture.server.close()
            before = ready_line()
            _ = watched.write_text("new\n")
            after = ready_line()
            self.assertNotEqual(before, after)

    def test_reload_hash_includes_a_source_that_loads_after_ready(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            sources = fixture.root / "scripts"
            startup = sources / "build_queue" / "admission_service.py"
            late = sources / "mac_test" / "eligibility.py"
            for path in (startup, late):
                path.parent.mkdir(parents=True, exist_ok=True)
                _ = path.write_text("old\n")
            ready: list[str] = []
            for text in ("old\n", "new\n"):
                _ = late.write_text(text)
                server = fixture.new_server(start=False)
                server.source_root = sources
                info = startup.stat()
                server.sources = {startup: (info.st_ino, info.st_mtime_ns)}
                try:
                    with mock.patch.dict(os.environ, {service.ADMISSION_SOURCE_RELOAD: "1"}), \
                            redirect_stderr(io.StringIO()) as journal:
                        server.start()
                    ready.append(self.assert_reload_hash_journal(journal.getvalue(), server.next_source_check))
                    if text == "old\n":
                        module = types.ModuleType("reload_hash_late_source")
                        module.__file__ = str(late)
                        with mock.patch.dict(sys.modules, {module.__name__: module}):
                            fixture.clock.advance(1)
                            server.run_once(0)
                            self.assertIn(late, server.sources)
                finally:
                    server.close()
            self.assertNotEqual(ready[0], ready[1])

    def test_showrunner_ci_nextest_records_its_caller_on_the_running_row(self) -> None:
        session = staff.StaffId("b6374931-fe3a-4f6d-96b7-ad20415a8346")
        runner = staff.StaffRow(session, "showrunner", "spacetime", staff.StaffRoot(),
                                current_session=session)
        with AdmissionFixture() as fixture:
            staff_file = fixture.root / "staff.json"
            _ = staff_file.write_text(json.dumps({"schema": 2, "rows": [runner.to_json()]}))
            request = fixture.request(need=GIB)
            parent = 1
            chain = (
                ("python3", "ci_points.py", "ci", "start", "--production", "spacetime"),
                ("bash", "validate_and_push.sh", "--validate-only"),
                ("bash", "run_validation.sh"),
                ("bash", "validate_ci.sh"),
                ("lint", "nextest", "--workspace", "--all-features", "--tests"),
            )
            environment = {"STAFF_FILE": str(staff_file), "CODEX_THREAD_ID": "",
                           "CLAUDE_CODE_SESSION_ID": session, "CLAUDE_SESSION_FOLDER": "spacetime",
                           "CLAUDE_SESSION_NAME": "", "BUILDLOG_UNIT": "", "BUILDLOG_OWNER_STAFF_ID": ""}
            for index, argv in enumerate(chain):
                pid = request.owner.pid if index == len(chain) - 1 else 100 + index
                start = request.owner.start_time if pid == request.owner.pid else str(pid)
                write_process(fixture.proc.path, pid, parent=parent, start=start)
                process = fixture.proc.path / str(pid)
                _ = (process / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
                _ = (process / "environ").write_bytes(
                    ("\0".join(f"{key}={value}" for key, value in environment.items()) + "\0").encode())
                parent = pid
            with mock.patch.dict(os.environ, environment), \
                    mock.patch.object(step_estimator, "step_estimate", return_value=step_estimator.StepEstimate(
                        request.profile, step_estimator.ReusableEstimate())):
                caller = client.step_request(["cargo", "nextest", "run", *chain[-1][2:]], request.cwd,
                    request.owner, request.since, str(fixture.scope(request.owner)), request.episode)
                _ = only_admitted(fixture.connect(caller).replies())
                reply, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(reply, wire.ViewReply)
            step, = [row for row in reply.reading.view.running if row.pid == request.owner.pid]
            self.assertEqual((step.session_id, step.session_name, step.unit), (session, "spacetime", ""))

    def test_caller_admission_sets_the_child_zone(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = {"BUILDLOG_DIR": str(root / "log"), "USER_ZONE": "Pacific/Pago_Pago", "HOME": str(root / "home")}
        _ = caller_admission(self, root, environment)
        self.assertEqual(environment["USER_ZONE"], "UTC")
        self.assertEqual(environment["USER_ZONE"], os.environ["USER_ZONE"])

    def test_caller_admission_serves_without_a_home_override(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = {"BUILDLOG_DIR": str(root / "log")}
        _ = caller_admission(self, root, environment)
        self.assertIsInstance(client.build_history_generation(), records.BuildHistoryGeneration)
        self.assertNotIn("HOME", environment)

    def test_fixture_uses_utc_and_restores_the_callers_zone(self) -> None:
        for zone in ("Pacific/Pago_Pago", ""):
            with self.subTest(zone=zone), mock.patch.dict(os.environ):
                if zone:
                    os.environ["USER_ZONE"] = zone
                else:
                    _ = os.environ.pop("USER_ZONE", None)
                with AdmissionFixture():
                    self.assertEqual(os.environ["USER_ZONE"], "UTC")
                self.assertEqual(os.environ.get("USER_ZONE", ""), zone)
                self.assertEqual("USER_ZONE" in os.environ, bool(zone))

    def test_serving_loop_never_reads_the_empty_environment_inside_a_patch_dict_exit(self) -> None:
        with AdmissionFixture() as fixture:
            fixture_cgroup = gate.builds_slice_cgroup()
            probing = threading.Event()
            probed = threading.Event()
            probe_finished: list[bool] = []
            loop_reads: list[Path] = []
            child_prints: list[str] = []
            # An empty PATH cannot find printenv; the child reads only the variable under test.
            printenv = shutil.which("printenv")
            assert printenv is not None
            serve_once = fixture.server.run_once

            def probe_or_serve(timeout: float) -> None:
                # The probe turn reads the environment and does no other work.
                if probing.is_set() and not probed.is_set():
                    try:
                        loop_reads.append(gate.builds_slice_cgroup())
                        # The loop starts its own threads too, for example the reading alert and the roster.
                        started = threading.Thread(target=lambda: loop_reads.append(gate.builds_slice_cgroup()))
                        started.start()
                        started.join(test_hang_guard.TEST_HANG_GUARD_SECS)
                        # The loop starts child processes with env=None, for example session_send and systemctl.
                        child = subprocess.run([printenv, "BUILDLOG_BUILDS_CGROUP"], capture_output=True, text=True,
                                               check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS)
                        child_prints.append(child.stdout)
                    finally:
                        probed.set()
                else:
                    serve_once(timeout)

            refill_update = os.environ.update

            def probe_update(values: Mapping[str, str] | Iterable[tuple[str, str]] = (), /, **keywords: str) -> None:
                # A patch.dict exit calls clear() and then update(); the probe reads between the two calls.
                probing.set()
                probe_finished.append(probed.wait(test_hang_guard.TEST_HANG_GUARD_SECS))
                refill_update(values, **keywords)

            with mock.patch.object(fixture.server, "run_once", probe_or_serve), fixture.serving():
                update_probe = mock.patch.object(os.environ, "update", probe_update)
                try:
                    with mock.patch.dict(os.environ, {"ADMISSION_PATCH_DICT_PROBE": "1"}):
                        # The probe wraps only the update of this exit; the patch.dict entry already ran.
                        _ = update_probe.start()
                finally:
                    _ = update_probe.stop()
                self.assertNotIn("ADMISSION_PATCH_DICT_PROBE", os.environ)
            self.assertEqual(probe_finished, [True])
            self.assertEqual(loop_reads, [fixture_cgroup, fixture_cgroup])
            self.assertEqual(child_prints, [f"{fixture_cgroup}\n"])

    def test_each_pass_publishes_captured_readings_and_remaining_waiters(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request()
            _ = fixture.server.handle_request(request)
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            self.assertIsInstance(snapshot, queue_snapshot.QueueSnapshot)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(snapshot.inputs.host, inputs.host)
            self.assertEqual(snapshot.inputs.pressure, inputs.pressure)
            self.assertEqual(snapshot.inputs.running, inputs.running)
            self.assertEqual(snapshot.inputs.reservations, inputs.reservations)
            self.assertEqual([row for _, row in snapshot.inputs.ordered],
                             [{**waiting.row, "order_since": waiting.row["since"]}
                              for waiting in fixture.server.waiters.values()])
            self.assertEqual(snapshot.written_at, fixture.clock.now())
            self.assertEqual(snapshot.policy, fixture.server.last_policy)
            fixture.clock.advance(1)
            fixture.free(60 * GIB)
            counts = fixture.readings.counts.copy()
            _ = fixture.server.run_pass()
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.written_at, fixture.clock.now())
            self.assertEqual(snapshot.inputs.ordered, [])
            self.assertIn(request.episode, fixture.server.reservations)
            self.assertEqual(fixture.readings.counts["host"] - counts["host"], 1)
            self.assertEqual(fixture.readings.counts["pressure"] - counts["pressure"], 1)
            self.assertEqual(fixture.readings.counts["compilers"] - counts["compilers"], 1)

    def test_snapshot_keeps_priority_metadata_outside_waiter_rows(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first, second = fixture.request(), fixture.request()
            labels = {first.identity.session_id: UnitLabels("first", unit="other"),
                      second.identity.session_id: UnitLabels("second", unit="urgent")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                _ = fixture.server.handle_request(first)
                _ = fixture.server.handle_request(second)
                _ = fixture.completed_queue_change(wire.PrioritySetRequest("ranked-snapshot", "urgent", "urgent", "director"))
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            rows = [row for _, row in snapshot.inputs.ordered]
            self.assertEqual(rows, [fixture.server.waiters[request.episode].row for request in (second, first)])
            for row in rows:
                self.assertNotIn("order", row)
                self.assertNotIn("priority_rank", row)
            self.assertEqual([records.waiter_priority(row) for _, row in gate.ranked_queue_waiters(snapshot.inputs)],
                             [records.PriorityUnit("urgent"), records.NormalOrder()])
            assert isinstance(fixture.server.last_policy, records.AdmissionPolicy)
            reading = gate.queue_read_from(snapshot.inputs, snapshot.policy)
            self.assertEqual([row["pid"] for _, row in reading.ordered], [second.owner.pid, first.owner.pid])

    def test_snapshot_keeps_the_pass_order_when_staff_labels_change_after_admission(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            starter, older, chosen, removed = (fixture.request(since=fixture.clock.now() - age)
                                                for age in (90, 60, 30, 10))
            labels = {starter.identity.session_id: UnitLabels("starter", unit="urgent"),
                      older.identity.session_id: UnitLabels("older", unit="other"),
                      chosen.identity.session_id: UnitLabels("chosen", unit="urgent")}
            admit = cast(Callable[..., None], getattr(fixture.server, "_admit"))

            def admit_and_change_labels(waiters: list[service.WaitingAdmission], config: records.MemoryConfig,
                                        share_percent: int, *,
                                        cargo_folder: placement.CargoFolder | placement.NoCargoFolder,
                                        verify_slots: Mapping[str, service.VerifyRunSlot],
                                        room: int | None = None, loan: int | None = None) -> None:
                admit(waiters, config, share_percent, cargo_folder=cargo_folder, verify_slots=verify_slots,
                      room=room, loan=loan)
                labels[older.identity.session_id] = UnitLabels("older", unit="urgent")
                labels[chosen.identity.session_id] = UnitLabels("chosen", unit="other")
                del fixture.server.waiters[removed.episode]

            with mock.patch.object(gate, "priority_labels", return_value=labels):
                for request in (starter, older, chosen, removed):
                    _ = fixture.server.handle_request(request)
                _ = fixture.completed_queue_change(wire.PrioritySetRequest("ranked-snapshot", "urgent", "urgent", "director"))
                fixture.free(60 * GIB)
                write_pressure_reading(fixture.root / "pressure")
                with mock.patch.object(fixture.server, "_admit", side_effect=admit_and_change_labels):
                    _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {starter.episode})
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual([row["pid"] for _, row in snapshot.inputs.ordered], [chosen.owner.pid, older.owner.pid])
            self.assertEqual([records.waiter_priority(row) for _, row in gate.ranked_queue_waiters(snapshot.inputs)],
                             [records.PriorityUnit("urgent"), records.NormalOrder()])

    def test_snapshot_replacement_failure_keeps_the_last_complete_snapshot(self) -> None:
        with AdmissionFixture() as fixture:
            _ = fixture.server.run_pass()
            previous = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            policy = fixture.server.last_policy
            assert isinstance(policy, records.AdmissionPolicy)
            with mock.patch.object(os, "replace", side_effect=OSError("snapshot unavailable")):
                with self.assertRaises(OSError):
                    queue_snapshot.write_queue_snapshot(fixture.state_dir, replace(inputs, now=inputs.now + 1),
                                                        policy)
            self.assertEqual(queue_snapshot.read_queue_snapshot(fixture.state_dir), previous)
            self.assertFalse((fixture.state_dir / "queue.json.tmp").exists())
            with mock.patch.object(queue_snapshot, "write_queue_snapshot", side_effect=OSError("snapshot unavailable")):
                _ = fixture.server.run_pass()
            self.assertEqual(queue_snapshot.read_queue_snapshot(fixture.state_dir), previous)

    def test_snapshot_accepts_unowned_memory_without_a_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            unowned = fixture.root / "builds.slice/unowned.scope"
            unowned.mkdir(parents=True)
            _ = (unowned / "memory.current").write_text(str(GIB))
            _ = (unowned / "memory.stat").write_text("inactive_file 0\n")
            _ = (unowned / "cgroup.procs").write_text("")
            _ = fixture.server.run_pass()
            snapshot = queue_snapshot.read_queue_snapshot(fixture.state_dir)
            assert isinstance(snapshot, queue_snapshot.QueueSnapshot)
            self.assertEqual(snapshot.inputs, fixture.server.last_inputs)
            self.assertTrue(any(isinstance(step.measured, records.UnownedSliceMemory) for step in snapshot.inputs.running))

    def test_unreadable_owner_refuses_without_waiter_or_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            with mock.patch.object(fixture.proc, "owner_start", return_value=service.OwnerProcessGone()):
                self.assertEqual(fixture.server.handle_request(request), [wire.Refused("owner no longer exists")])
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)

    def test_admission_fixture_leaves_an_inherited_config_folder_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inherited = Path(directory)
            existing = inherited / "build_targets.conf"
            contents = b"# Caller configuration\n"
            _ = existing.write_bytes(contents)
            with mock.patch.dict(os.environ, {"CONF_DIR": str(inherited)}):
                with AdmissionFixture() as fixture:
                    self.assertEqual(Path(os.environ["CONF_DIR"]), fixture.root)
                    self.assertTrue((fixture.root / existing.name).is_file())
                self.assertEqual(Path(os.environ["CONF_DIR"]), inherited)
            self.assertEqual(list(inherited.iterdir()), [existing])
            self.assertEqual(existing.read_bytes(), contents)

    def test_socket_fixture_works_with_a_long_tmpdir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="admit-long-", dir="/tmp") as root:
            long_tmpdir = Path(root) / ("nested-" * 24)
            long_tmpdir.mkdir()
            with mock.patch.dict(os.environ, {"TMPDIR": str(long_tmpdir)}), \
                 mock.patch.object(tempfile, "tempdir", str(long_tmpdir)), \
                 AdmissionFixture() as fixture:
                self.assertEqual(fixture.root.parent, long_tmpdir)
                self.assertGreater(len(os.fsencode(fixture.root / "admission.sock")), 108)
                self.assertLess(len(os.fsencode(fixture.socket_path)), 108)
                self.assertTrue(fixture.socket_path.is_socket())
                peer = fixture.connect(wire.ViewRequest())
                self.assertTrue(any(isinstance(reply, wire.ViewReply) for reply in peer.replies()))


    def test_second_request_and_oversized_input_end_admission_before_next_pass(self) -> None:
        for violation in ("second-request", "oversize-newline", "oversize-incomplete"):
            with self.subTest(violation=violation), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request()
                if violation == "second-request":
                    peer = fixture.connect(request)
                    self.assertIsInstance(peer.replies()[0], wire.Wait)
                    connection = next(iter(fixture.server.connections.values()))
                    original_wait = fixture.server.waiters[request.episode].last_reply
                    assert isinstance(original_wait, wire.Wait)
                    peer.socket.sendall(wire.encode_request(wire.ViewRequest()).encode())
                else:
                    encoded = wire.encode_request(request)
                    if violation == "oversize-incomplete":
                        encoded = encoded[:-1]
                    fixture.configure(request_max_bytes=len(encoded.encode()) - 1)
                    peer = fixture.connect(encoded)
                    # Accept and read it during the next receive phase.
                    connection = None
                    original_wait = None
                # A due pass can admit the step as soon as reception completes.
                fixture.free(60 * GIB)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertNotIn(request.episode, fixture.server.waiters)
                self.assertNotIn(request.episode, fixture.server.reservations)
                self.assertNotIn(request.episode, fixture.server.ledger)
                if connection is None:
                    connection = next(iter(fixture.server.connections.values()))
                self.assertTrue(connection.finished)
                if original_wait is not None:
                    sending = connection.sending
                    fixture.server.send_wait(connection, original_wait)
                    self.assertTrue(connection.finished)
                    self.assertEqual(connection.sending, sending)
                replies = peer.replies()
                self.assertEqual(len(replies), 1)
                refused = replies[0]
                self.assertIsInstance(refused, wire.Refused)
                assert isinstance(refused, wire.Refused)
                self.assertIn("one request" if violation == "second-request" else "exceeds", refused.reason)
                self.assertEqual(fixture.server.connections, {})
                self.assertEqual(peer.socket.recv(1), b"")
                with mock.patch.object(fixture.server, "run_pass", wraps=fixture.server.run_pass) as passed:
                    for _ in range(3):
                        self.assertEqual(fixture.server.epoll.poll(0), [])
                        fixture.server.run_once(0)
                    self.assertEqual(passed.call_count, 0)

    def test_scope_that_empties_after_lifetime_capture_ends_in_the_same_pass(self) -> None:
        for stage in ("restore", "pending ending", "memory reading"):
            with self.subTest(stage=stage), AdmissionFixture() as fixture:
                request = fixture.request()
                sidecar = fixture.scope(request.owner)
                scope = Path(sidecar.read_text())
                request = replace(request, sidecar=str(sidecar))
                with fixture.loop_turn():
                    grant = only_admitted(fixture.server.handle_request(request))
                    fixture.scoped_peak(grant)
                    fixture.proc.kill(request.owner)
                    (fixture.proc.path / str(request.owner.pid) / "stat").unlink()
                    if stage != "memory reading":
                        fixture.server.reservations[request.episode] = replace(
                            fixture.server.reservations[request.episode], ending=service.OwnerExited())
                        fixture.server.persist()
                    lifetime = fixture.proc.lifetime
                    captures = 0
                    def empty_after_capture(row: records.Reservation, owner_live: bool) -> gate.StepLifetime:
                        nonlocal captures
                        captured = lifetime(row, owner_live)
                        if isinstance(captured, gate.PopulatedStepScope):
                            captures += 1
                            _ = (scope / "cgroup.procs").write_text("")
                        return captured
                    with mock.patch.object(fixture.proc, "lifetime", side_effect=empty_after_capture):
                        if stage == "restore":
                            fixture.restart()
                        else:
                            _ = fixture.server.run_pass()
                    self.assertEqual(captures, 1)
                    self.assertNotIn(request.episode, fixture.server.reservations)
                    self.assertEqual(len(fixture.history()), 1)

    def test_scope_watch_closes_on_release_and_reused_scope_is_watched_again(self) -> None:
        with AdmissionFixture() as fixture:
            events = fixture.scope_events()
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            _ = fixture.server.run_pass()
            descriptor = next(fd for fd, watch in fixture.server.watchers.items()
                              if isinstance(watch, service.ScopeWatch))
            watch = fixture.server.watchers[descriptor]
            assert isinstance(watch, service.ScopeWatch)
            self.assertEqual(watch.episode, request.episode)
            _ = fixture.server.handle_request(wire.ReleaseRequest("release-scope", grant.reservation, 0))
            self.assertIn(descriptor, events.unregistered)
            with self.assertRaises(OSError):
                _ = os.fstat(descriptor)
            fixture.clock.advance(0.001)
            second = replace(request, episode="scope-reused", sidecar=str(fixture.scope(request.owner)))
            if second.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            _ = only_admitted(fixture.server.handle_request(second))
            _ = fixture.server.run_pass()
            self.assertEqual(len(events.registered), 2)
            scoped = [watch for watch in fixture.server.watchers.values() if isinstance(watch, service.ScopeWatch)]
            self.assertEqual(scoped, [service.ScopeWatch(watch.path, second.episode)])

    def test_removed_scope_wakeup_closes_watch_and_continues_the_loop(self) -> None:
        for error in (errno.ENODEV, errno.ENOENT):
            with self.subTest(error=error), AdmissionFixture() as fixture:
                events = fixture.scope_events()
                request = fixture.request()
                sidecar = fixture.scope(request.owner)
                request = replace(request, sidecar=str(sidecar))
                _ = only_admitted(fixture.server.handle_request(request))
                _ = fixture.server.run_pass()
                descriptor = next(fd for fd, watch in fixture.server.watchers.items()
                                  if isinstance(watch, service.ScopeWatch))
                scope = Path(sidecar.read_text())
                (scope / "cgroup.procs").unlink()
                (scope / "cgroup.events").unlink()
                original_read = os.read
                def removed_read(fd: int, size: int) -> bytes:
                    if fd == descriptor:
                        raise OSError(error, "scope removed")
                    return original_read(fd, size)
                events.events.append((descriptor, select.EPOLLERR))
                fixture.readings.counts.clear()
                with mock.patch.object(os, "read", side_effect=removed_read):
                    fixture.server.run_once(0)
                self.assertIn(descriptor, events.unregistered)
                self.assertNotIn(descriptor, fixture.server.watchers)
                self.assertEqual(fixture.readings.counts["host"], 1)
                self.assertFalse(fixture.server.stopped)

    def test_zero_wait_socket_restart_recovers_after_sigkill_and_rejects_a_live_listener(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.kill_socket_server()
            self.assertTrue(fixture.socket_path.exists())
            fixture.server = fixture.new_server()
            peer = fixture.connect(wire.ViewRequest())
            self.assertIsInstance(peer.replies()[0], wire.ViewReply)
            contender = service.AdmissionServer(fixture.state_dir, fixture.clock, fixture.readings,
                fixture.proc, socket_path=fixture.socket_path)
            try:
                with self.assertRaisesRegex(RuntimeError, "already listening"):
                    contender.start()
                self.assertTrue(fixture.socket_path.exists())
            finally:
                contender.close()

    def test_socket_permissions_are_private_at_bind_before_listen(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            bind = socket.socket.bind
            modes: list[int] = []
            def observe_bind(listener: socket.socket, address: str) -> None:
                bind(listener, address)
                modes.append(stat.S_IMODE(Path(address).stat().st_mode))
            previous = os.umask(0)
            try:
                with mock.patch.object(socket.socket, "bind", observe_bind):
                    fixture.server = fixture.new_server()
            finally:
                _ = os.umask(previous)
            self.assertEqual(modes, [0o600])


    def test_mutation_refusals_replay_durably_after_state_changes(self) -> None:
        with AdmissionFixture() as fixture:
            empty = wire.PriorityClearRequest("refused-unit", "director", "missing", why="clear words")
            refused = fixture.server.handle_request(empty)
            fixture.restart()
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            self.assertEqual(fixture.server.handle_request(empty), refused)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            fixture.allow_next_start()
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            width = wire.WidthRequest("refused-width", grant.reservation, 0)
            refused_width = fixture.server.handle_request(width)
            self.assertIsInstance(refused_width[0], wire.Refused)
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(replace(width, width=2)), refused_width)
            self.assertEqual(fixture.server.reservations[request.episode].test_width, request.test_width)

    def test_width_replays_live_through_retention_then_expire_after_release(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(replay_retention_seconds=2)
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            old = wire.WidthRequest("old-width", grant.reservation, 4)
            _ = fixture.server.handle_request(old)
            _ = fixture.server.handle_request(replace(old, request_id="new-width", width=2))
            fixture.clock.advance(3)
            _ = fixture.server.run_pass()
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(old), [wire.Ok()])
            self.assertEqual(fixture.server.reservations[request.episode].test_width, records.RequestedTestWidth(2))
            _ = fixture.server.handle_request(wire.ReleaseRequest("end-width", grant.reservation, 0))
            fixture.clock.advance(1)
            _ = fixture.server.run_pass()
            self.assertIn(old.request_id, fixture.server.ledger)
            fixture.clock.advance(2)
            _ = fixture.server.run_pass()
            self.assertNotIn(old.request_id, fixture.server.ledger)

    def test_admission_and_width_update_freeze_the_matching_history_need_and_margin(self) -> None:
        with mock.patch.object(store, "host_name", return_value="test-host"), AdmissionFixture() as fixture:
            fixture.configure(test_phase_margin=1.4)
            request = fixture.request(need=12 * GIB)
            assert isinstance(request.profile, records.StepProfile)
            request = replace(request, profile=replace(request.profile,
                repository=records.RepositoryIdentity("fixture-repository", "fixture")))
            for width, peak in ((8, 3 * GIB), (4, 2 * GIB)):
                store.append_line(fixture.state_dir / "anon_peaks.jsonl", {
                    "host": "test-host", "repo_identity": "fixture-repository", "step": "nextest",
                    "test_threads": width, "test_phase_peak_bytes": peak,
                    "ended_at": store.utc_iso(fixture.clock.now() - 1)})
            grant = only_admitted(fixture.server.handle_request(request))
            captured = fixture.server.reservations[request.episode].test_phase_need
            self.assertEqual(captured, records.TestPhaseNeed(3 * GIB, 1, 8, 8, "same_width", margin=1.4))
            fixture.configure(test_phase_margin=1.6)
            _ = fixture.run_pass()
            self.assertEqual(fixture.server.reservations[request.episode].test_phase_need, captured)
            self.assertEqual(fixture.server.handle_request(wire.WidthRequest("width", grant.reservation, 4)), [wire.Ok()])
            updated = fixture.server.reservations[request.episode]
            self.assertEqual(updated.test_phase_need, records.TestPhaseNeed(2 * GIB, 1, 4, 4, "same_width", margin=1.6))
            self.assertEqual(updated.need, 12 * GIB)

    def test_width_update_preserves_accepted_need_and_measured_test_phase_history(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=11 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 3 * GIB)
            record = fixture.server.reservations[request.episode]
            phase = gate.test_phase_path(service.reservation_row(record))
            assert isinstance(phase, Path)
            _ = phase.write_text(wire.encode_record({"test_phase_peak_bytes": 3 * GIB,
                "test_phase_started_at": fixture.clock.now(), "test_threads": 4,
                "call_id": "call-1", "test_scope": "full", "repo_identity": "fixture",
                "package": "", "filters": []}))
            self.assertEqual(fixture.server.handle_request(wire.WidthRequest("width", grant.reservation, 4)), [wire.Ok()])
            changed = fixture.server.reservations[request.episode]
            self.assertEqual(changed.need, 11 * GIB)
            self.assertEqual(changed.test_width, records.RequestedTestWidth(4))
            if request.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            self.assertEqual(only_admitted(fixture.server.handle_request(request)), grant)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("end", grant.reservation, 0)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual(row["need"], 11 * GIB)
            self.assertEqual(row["test_threads"], 4)
            self.assertEqual(row["test_phase_peak_bytes"], 3 * GIB)

    def test_promoted_launch_and_input_composition_share_waiter_ordering(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            launch = fixture.launch(since=fixture.clock.now() - 10)
            _ = fixture.server.handle_request(launch)
            with fixture.server.observe_waiter_ordering() as ordered:
                later = fixture.request()
                _ = fixture.server.handle_request(later)
            self.assertGreaterEqual(ordered.call_count, 2)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual([row["pid"] for _, row in inputs.ordered], [launch.owner.pid, later.owner.pid])

    def test_launch_hold_reason_comes_from_the_shared_reason_rule(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            reason = records.EstimateExpired(fixture.clock.now())
            with mock.patch.object(gate, "queue_wait_reason", return_value=reason) as explain:
                reply = fixture.server.handle_request(fixture.launch())[0]
            assert isinstance(reply, wire.Wait)
            assert isinstance(reply.reason, records.HeadAdmissionStatus)
            self.assertEqual(reply.reason.cause, reason)
            self.assertEqual(reply.text, queue_view.reason_phrase(reason, ()))
            self.assertEqual(explain.call_count, 1)

    def test_unchanged_passes_do_not_rewrite_durable_state(self) -> None:
        for ended_replies in (0, 64):
            with self.subTest(ended_replies=ended_replies), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                _ = fixture.server.handle_request(fixture.request())
                for index in range(ended_replies):
                    self.assertEqual(fixture.server.handle_request(
                        wire.ReleaseRequest(f"ended-{index}", "absent", 0)), [wire.Ok()])
                state = fixture.state_text()
                with (mock.patch.object(service, "write_text_atomic", wraps=write_text_atomic) as write,
                      mock.patch.object(fixture.server.progress_history, "prune",
                                        wraps=fixture.server.progress_history.prune) as swept):
                    with mock.patch.object(service.ReplayEntry, "__eq__", autospec=True,
                                           side_effect=service.ReplayEntry.__eq__) as compared:
                        for _ in range(20):
                            fixture.clock.advance(1)
                            _ = fixture.server.run_pass()
                        compared.assert_not_called()
                    self.assertLess(swept.call_count, 20)
                    self.assertEqual(write.call_count, 0)
                    self.assertEqual(fixture.state_text(), state)
                    _ = fixture.server.handle_request(fixture.request())
                    self.assertGreater(write.call_count, 0)

    def test_delivered_waits_leave_only_the_current_reply_and_no_pending_history(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request()
            first = fixture.server.handle_request(request)
            self.assertIsInstance(first[0], wire.Wait)
            for available in (GIB, 2 * GIB, 0):
                fixture.free(available)
                fixture.hold_starts()
                fixture.clock.advance(60)
                _ = fixture.server.run_pass()
                self.assertIsInstance(fixture.server.waiters[request.episode].last_reply, wire.Wait)
                self.assertEqual(fixture.server.pending_replies, {})
                self.assertNotIn(request.episode, fixture.server.ledger)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            self.assertNotIn(request.episode, fixture.server.waiters)
            self.assertIsInstance(fixture.server.ledger[request.episode].reply, wire.Admitted)
            self.assertEqual(fixture.server.pending_replies, {})

    def test_wait_and_source_lifetimes_use_distinct_states(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            with fixture.server.observe_wait_initial_states() as before_delivery:
                request = fixture.request()
                _ = fixture.server.handle_request(request)
            self.assertEqual(before_delivery, [service.NoWaitReply()])
            self.assertIsInstance(fixture.server.waiters[request.episode].last_reply, wire.Wait)
            self.assertIsInstance(fixture.server.source_change_at, service.SourcesUnchanged)
            source = fixture.root / "removed-source.py"
            _ = source.write_text("loaded")
            info = source.stat()
            fixture.server.sources = {source: (info.st_ino, info.st_mtime_ns)}
            source.unlink()
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            self.assertIsInstance(fixture.server.sources[source], service.SourceMissing)
            self.assertEqual(fixture.server.source_change_at, fixture.clock.monotonic())
            self.assertTrue(all(isinstance(watch, service.OwnerWatch) for watch in fixture.server.watchers.values()))

    def test_wait_poll_and_heartbeat_intervals_follow_config_changes(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(waiting_pass_seconds=3, wait_reply_seconds=7)
            fixture.free(0)
            fixture.hold_starts()
            peer = fixture.connect(fixture.request())
            self.assertEqual(len(peer.replies()), 1)
            fixture.readings.counts.clear()
            fixture.clock.advance(2)
            self.assertFalse(peer.replies())
            self.assertEqual(fixture.readings.counts["host"], 0)
            fixture.clock.advance(1)
            self.assertFalse(peer.replies())
            self.assertEqual(fixture.readings.counts["host"], 1)
            fixture.configure(waiting_pass_seconds=1, wait_reply_seconds=4)
            fixture.clock.advance(3)
            self.assertEqual(len(peer.replies()), 1)
            self.assertEqual(fixture.server.next_pass, fixture.clock.monotonic() + 1)

    def test_source_check_and_quiet_window_follow_config(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.configure(source_check_seconds=2, source_settle_seconds=4)
            fixture.server.next_source_check = fixture.clock.monotonic() + 2
            source = fixture.root / "loaded-config.py"
            _ = source.write_text("old")
            info = source.stat()
            fixture.server.sources = {source: (info.st_ino, info.st_mtime_ns)}
            source.unlink()
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            self.assertIsInstance(fixture.server.source_change_at, service.SourcesUnchanged)
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            fixture.clock.advance(2)
            fixture.server.run_once(0)
            self.assertFalse(fixture.server.stopped)
            fixture.clock.advance(2)
            fixture.server.run_once(0)
            self.assertTrue(fixture.server.stopped)

    def test_start_provisions_the_mac_linux_state_root_before_ready(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            root = fixture.root / "target-state" / "guest-state"
            server = fixture.new_server(start=False)
            try:
                with (mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(root)}),
                      mock.patch.object(atomic_file, "sync_folder", wraps=atomic_file.sync_folder) as sync,
                      mock.patch.object(service, "_notify") as notify,
                      redirect_stderr(io.StringIO()) as journal):
                    def ready(message: str) -> None:
                        self.assertEqual(message, "READY=1")
                        self.assertTrue(root.is_dir())
                        self.assertEqual(sync.call_args_list, [mock.call(root.parent), mock.call(fixture.root)])

                    notify.side_effect = ready
                    self.assertFalse(root.exists())
                    server.start()
                    server.start()
                    notify.assert_called_once_with("READY=1")
                self.assertEqual(journal.getvalue().splitlines()[0],
                                 f"memory admission: Mac Linux state root ready at {root}")
            finally:
                server.close()

    def test_start_continues_after_mac_linux_state_setup_fails(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            root = fixture.root / "guest-state"
            root.mkdir()
            failure = OSError(errno.EIO, "fixture parent sync failed")
            with (mock.patch.dict(os.environ, {"BUILD_TARGETS_STATE_DIR": str(root)}),
                  mock.patch.object(atomic_file, "sync_folder", side_effect=failure) as sync,
                  redirect_stderr(io.StringIO()) as journal):
                fixture.server = fixture.new_server()
            self.assertTrue(root.is_dir())
            sync.assert_called_once_with(root.parent)
            self.assertEqual(journal.getvalue().splitlines()[0],
                             f"memory admission: Cannot prepare the Mac Linux state root {root}: {failure}. "
                             + "Run build_targets.py setup-mac-linux on natedev.")
            self.assertIsInstance(fixture.connect(wire.ViewRequest()).replies()[0], wire.ViewReply)

    def test_serve_binds_the_environment_socket_without_listen_fds(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            socket_path = fixture.socket_dir / "from-environment.sock"
            run_once = service.AdmissionServer.run_once
            def one_turn(server: service.AdmissionServer) -> None:
                run_once(server, 0)
                self.assertEqual(server.socket_path, socket_path)
                self.assertTrue(socket_path.is_socket())
                self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)
            with mock.patch.dict(os.environ, {"ADMISSION_SOCKET": str(socket_path), "LISTEN_FDS": ""}), \
                 mock.patch.object(service, "SystemClock", return_value=fixture.clock), \
                 mock.patch.object(service, "SystemReadings", return_value=fixture.readings), \
                 mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(service.AdmissionServer, "serve_forever", one_turn), \
                 mock.patch.object(sys, "argv", ["admission_service", "serve", "--state-dir", str(fixture.state_dir)]):
                self.assertEqual(service.main(), 0)
            self.assertFalse(socket_path.exists())

    def test_a_source_change_under_systemd_runs_the_new_code_in_place_on_the_same_socket(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            fixture.configure(source_check_seconds=1, source_settle_seconds=1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener, \
                    socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notices:
                listener.bind(str(fixture.socket_path))
                listener.listen()
                inherited = os.dup(listener.fileno())
                notify_path = fixture.socket_dir / "notify.sock"
                notices.bind(str(notify_path))
                notices.setblocking(False)

                source = fixture.root / "loaded-source.py"
                revision = subprocess.check_output(
                    ["git", "-C", str(Path(service.__file__).resolve().parent), "rev-parse", "HEAD"],
                    text=True).strip()
                ready_revision = f"memory admission: ready with git revision {revision}"
                state_root = fixture.root / ".local/state/build-targets"
                state_ready = f"memory admission: Mac Linux state root ready at {state_root}"
                original_watch = ""
                changed_at = fixture.clock.monotonic()

                def serve(server: service.AdmissionServer) -> None:
                    nonlocal original_watch
                    original_watch = (f"memory admission: source watch checks {len(server.sources)} paths "
                                      f"first at monotonic {server.next_source_check:.3f}")
                    server.start()
                    _ = source.write_text("old")
                    info = source.stat()
                    server.sources = {source: (info.st_ino, info.st_mtime_ns)}
                    source.unlink()
                    server.next_source_check = fixture.clock.monotonic()
                    for _turn in range(3):
                        if not server.stopped:
                            server.run_once(0)
                            fixture.clock.advance(1)
                    self.assertTrue(server.stopped)

                execv = mock.Mock(side_effect=OSError("stop before replacement"))
                environment = {"LISTEN_FDS": "1", "LISTEN_PID": str(os.getpid()),
                               "NOTIFY_SOCKET": str(notify_path), service.ADMISSION_SOURCE_RELOAD: ""}
                with mock.patch.dict(os.environ, environment), redirect_stderr(io.StringIO()) as journal, \
                     mock.patch.object(service, "LISTEN_FD", inherited), \
                     mock.patch.object(service, "SystemClock", return_value=fixture.clock), \
                     mock.patch.object(service, "SystemReadings", return_value=fixture.readings), \
                     mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                     mock.patch.object(service.AdmissionServer, "serve_forever", serve), \
                     mock.patch.object(os, "execv", execv), \
                     mock.patch.object(sys, "argv", ["admission_service", "serve", "--state-dir", str(fixture.state_dir)]):
                    with self.assertRaisesRegex(OSError, "stop before replacement"):
                        _ = service.main()
                    reload_marker = os.environ.get(service.ADMISSION_SOURCE_RELOAD, "")
                self.assertEqual(journal.getvalue().splitlines(),
                    [state_ready, original_watch, ready_revision,
                     f"memory admission: source watch detected changes at monotonic {changed_at:.3f} in {source}",
                     "memory admission: source watch will reload after the changes settled",
                     f"memory admission: reload for changed source {source}"])
                self.assertTrue(reload_marker)
                execv.assert_called_once_with(sys.executable, sys.orig_argv)
                self.assertTrue(os.get_inheritable(inherited))
                os.close(inherited)
                messages: list[str] = []
                with suppress(BlockingIOError):
                    while True:
                        messages.append(notices.recv(4096).decode())
                states = [message for message in messages if not message.startswith("WATCHDOG=")]
                self.assertEqual(len(states), 2)
                self.assertEqual(states[0], "READY=1")
                self.assertRegex(states[1], r"^RELOADING=1\nMONOTONIC_USEC=\d+$")
                state_root.rmdir()
                with mock.patch.dict(os.environ, {**environment, service.ADMISSION_SOURCE_RELOAD: reload_marker}), \
                     mock.patch.object(service, "LISTEN_FD", os.dup(listener.fileno())), \
                     redirect_stderr(io.StringIO()) as journal:
                    replacement = fixture.new_server(start=False)
                    notify = cast(Callable[[str], None], getattr(service, "_notify"))
                    replacement_watch = (f"memory admission: source watch checks {len(replacement.sources)} paths "
                                         f"first at monotonic {replacement.next_source_check:.3f}")

                    def notify_before_journal(message: str) -> None:
                        self.assertEqual(message, "READY=1")
                        self.assertTrue(state_root.is_dir())
                        self.assertEqual(journal.getvalue().splitlines(), [state_ready, replacement_watch])
                        notify(message)

                    try:
                        with mock.patch.object(service, "_notify", side_effect=notify_before_journal):
                            replacement.start()
                        self.assertEqual(notices.recv(4096).decode(), "READY=1")
                        version = admission_code.reload_source_version(replacement.source_root)
                        self.assertRegex(version, r"^[0-9a-f]{64}$")
                        self.assertEqual(journal.getvalue().splitlines(),
                            [state_ready, replacement_watch, f"memory admission: reload ready, source hash {version}",
                             ready_revision])
                        self.assertNotIn(service.ADMISSION_SOURCE_RELOAD, os.environ)
                    finally:
                        replacement.close()
            self.assertTrue(fixture.socket_path.exists())

    def test_each_pass_matches_the_shared_rule_and_view_uses_its_frozen_limits(self) -> None:
        for free in (0, 10 * GIB, 60 * GIB):
            with self.subTest(free=free), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                requests = [fixture.request(need=need * GIB, since=fixture.clock.now() - age)
                            for need, age in ((12, 400), (4, 30), (2, 10))]
                for request in requests:
                    _ = fixture.server.handle_request(request)
                fixture.free(free)
                with mock.patch.object(gate, "admission_pass", wraps=gate.admission_pass) as run:
                    result = fixture.server.run_pass()
                assert isinstance(result, gate.PassResult)
                self.assertEqual(run.call_count, 1)
                inputs = fixture.server.last_inputs
                policy = fixture.server.last_policy
                assert isinstance(policy, records.AdmissionPolicy)
                assert isinstance(inputs, records.QueueInputs)
                self.assertEqual(result, gate.admission_pass(inputs, inputs.now, policy=policy))
                starts = sorted((row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)),
                    key=lambda row: row.admission.rank if isinstance(row.admission, gate.ProposedAdmission) else -1)
                committed = list(fixture.server.reservations.values())
                self.assertEqual([f"{record.owner.pid}-{record.owner.start_time}" for record in committed],
                                 [row.owner for row in starts])
                for row in result.rows:
                    request = next(request for request in requests if records.waiter_owner({
                        "pid": request.owner.pid, "start_time": request.owner.start_time,
                        "since": request.since, "need": request.profile.peak.bytes}) == row.owner)
                    if isinstance(row.admission, gate.NotAdmitted):
                        waiting = fixture.server.waiters[request.episode].last_reply
                        assert isinstance(waiting, wire.Wait)
                        self.assertEqual(waiting.reason, row.reason)
                        self.assertEqual(waiting.text, queue_view.reason_phrase(row.reason,
                            gate.queue_read_from(inputs, policy=policy).view.waiting))
                expected = gate.queue_read_from(inputs, policy=policy)
                viewed, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(viewed, wire.ViewReply)
                occupancy = viewed.reading.explanation.local_occupancy
                assert occupancy is not None
                self.assertEqual([row.get("reservation_id") for row in occupancy.reservations],
                                 [record.reservation_id for record in committed])
                line = viewed.reading.view.move_line
                assert isinstance(line, records.QueueMoveLine)
                started = {row.owner for row in starts}
                self.assertEqual(line.members, tuple(
                    records.QueueMoveMember(row.get("move_id", ""), movable=records.waiter_owner(row) not in started)
                    for _, row in expected.ordered))
                self.assertTrue(line.revision)
                self.assertEqual(viewed, wire.ViewReply(replace(expected, view=replace(expected.view, move_line=line),
                    explanation=replace(expected.explanation, local_occupancy=occupancy))))
                explained = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(explained, wire.ExplainReply)
                self.assertEqual(explained.explanation.decided_from, inputs)
                self.assertEqual(explained.explanation.policy, policy)

    def test_queries_project_the_stored_pass_without_another_admission_walk(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request()
            _ = fixture.server.handle_request(request)
            inputs = fixture.server.last_inputs
            result, policy = fixture.server.last_result, fixture.server.last_policy
            assert isinstance(policy, records.AdmissionPolicy)
            assert isinstance(inputs, records.QueueInputs)
            assert isinstance(result, gate.PassResult)
            projected = gate.queue_read_from_pass(inputs, policy, result)
            self.assertEqual(projected, gate.queue_read_from(inputs, policy))
            with mock.patch.object(gate, "admission_pass", wraps=gate.admission_pass) as walk:
                viewed, = fixture.server.handle_request(wire.ViewRequest())
                explained = fixture.server.handle_request(wire.ExplainRequest())
                mine = fixture.server.handle_request(wire.MineRequest(request.identity.session_id))
            walk.assert_not_called()
            assert isinstance(viewed, wire.ViewReply)
            line = viewed.reading.view.move_line
            assert isinstance(line, records.QueueMoveLine)
            self.assertEqual(line.members, (records.QueueMoveMember(fixture.server.waiters[request.episode].row.get("move_id", "")),))
            self.assertTrue(line.revision)
            expected_view = replace(projected, view=replace(projected.view, move_line=line), explanation=replace(
                projected.explanation, local_occupancy=records.LocalAdmissionOccupancy((), {})))
            self.assertEqual(viewed, wire.ViewReply(expected_view))
            self.assertEqual(explained, [wire.ExplainReply(projected.explanation)])
            own = mine[0]
            assert isinstance(own, wire.MineReply)
            self.assertEqual(own.reading.explanation.decided_from, inputs)


    def test_wait_line_skips_unusable_names_in_order(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            for unit, caller, folder, expected in (
                ("explicit unit", "saved caller", "/private/folder", "explicit unit"),
                ("—", "saved caller", "/private/folder", "saved caller"),
                ("$UNIT", "owner not recorded", "/private/folder", "folder"),
                ("", "unregistered session", "/private/widget", "widget"),
                ("unregistered session", "saved caller", "/private/widget", "saved caller"),
                ("  ", "556caf6e-713d-47c0-a8fc-5dd5e8c26ce4", "/private/$FOLDER", "unregistered session"),
            ):
                with self.subTest(unit=unit, caller=caller, folder=folder):
                    row: records.Waiter = {"pid": request.owner.pid, "start_time": request.owner.start_time,
                        "since": request.since, "need": GIB, "step": "nextest", "session_name": caller,
                        "unit": unit, "folder": folder}
                    waiting = service.WaitingAdmission(request, row, service.NO_CONNECTION)
                    self.assertEqual(service.wait_line_name(waiting), f"{expected}: nextest")

    def test_wait_lines_then_ranked_admissions_and_release_frees_charge(self) -> None:
        for owner_name in ("session", "CI", "unit", "continuation"):
            with self.subTest(owner=owner_name), AdmissionFixture() as fixture:
                run: wire.RunIdentity | wire.NoRun = wire.NO_RUN
                if owner_name == "continuation":
                    run = fixture.run_owner()
                    grant = only_admitted(fixture.server.handle_request(fixture.request(run=run, need=6 * GIB)))
                    fixture.release(grant)
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request(need=12 * GIB if owner_name == "continuation" else 8 * GIB, run=run)
                head: wire.AdmitRequest | wire.ServiceEstimateRequest = request
                label = "fixture: nextest"
                if owner_name == "CI":
                    head = replace(fixture.service_request(runner="hana-linux-2"), argv=("cargo", "test"))
                    label = "hana CI · hana-linux-2 · compile: test"
                elif owner_name == "unit":
                    head = replace(request, identity=replace(request.identity, unit="compiler"))
                    label = "compiler: nextest"
                if owner_name == "continuation":
                    head = replace(request, identity=replace(request.identity, name="continuation"))
                    label = "ordinary: nextest"
                    ordinary = fixture.request(need=8 * GIB, since=fixture.clock.now() - 1)
                    ordinary = replace(ordinary, identity=replace(ordinary.identity, name="ordinary"))
                    ordinary_peer = fixture.connect(ordinary)
                    peers = [ordinary_peer, fixture.connect(head)]
                else:
                    peers = [fixture.connect(head), fixture.connect(fixture.request(need=8 * GIB))]
                for position, peer in enumerate(peers, 1):
                    waits = [reply for reply in peer.replies() if isinstance(reply, wire.Wait)]
                    self.assertTrue(waits)
                    self.assertEqual(waits[-1].head, label)
                    self.assertEqual(waits[-1].snapshot.head_label, label)
                    if owner_name == "continuation":
                        inputs = fixture.server.last_inputs
                        assert isinstance(inputs, records.QueueInputs)
                        self.assertEqual(inputs.ordered[0][1].get("session_name"), "ordinary")
                        self.assertEqual(waits[-1].position, position)
                        with mock.patch.object(client, "CLOCK", fixture.clock), redirect_stderr(io.StringIO()) as output:
                            client.WaitOutput()(waits[-1])
                        if position == 1:
                            self.assertIn("this build is the queue head", output.getvalue())
                        else:
                            self.assertEqual(waits[-1].snapshot.head_need, 8 * GIB)
                            self.assertIn("head ordinary: nextest needs 8.0 GiB", output.getvalue())
                            self.assertNotIn("this build is the queue head", output.getvalue())
                fixture.free((20 if owner_name == "continuation" else 60) * GIB)
                if owner_name == "continuation":
                    fixture.allow_next_start()
                result = fixture.server.run_pass()
                assert isinstance(result, gate.PassResult)
                expected = sorted((row for row in result.rows if isinstance(row.admission, gate.ProposedAdmission)),
                                  key=lambda row: row.admission.rank if isinstance(row.admission, gate.ProposedAdmission) else -1)
                first = only_admitted(peers[0].replies())
                self.assertTrue(any(isinstance(reply, wire.Wait) for reply in peers[1].replies()))
                fixture.free(60 * GIB)
                fixture.allow_next_start()
                next_result = fixture.server.run_pass()
                assert isinstance(next_result, gate.PassResult)
                expected.extend(row for row in next_result.rows if isinstance(row.admission, gate.ProposedAdmission))
                grants = [first, only_admitted(peers[1].replies())]
                self.assertEqual([record.owner.pid for record in fixture.server.reservations.values()],
                                 [int(row.owner.split('-')[0]) for row in expected])
                before = fixture.clock.monotonic()
                _ = fixture.server.handle_request(wire.ReleaseRequest("end-first", grants[0].reservation, 0))
                self.assertLess(fixture.clock.monotonic() - before, 0.2)
                self.assertEqual(len(fixture.server.reservations), 1)
                inputs = fixture.server.last_inputs
                assert isinstance(inputs, records.QueueInputs)
                self.assertEqual(len(inputs.reservations), 1)

    def test_width_and_call_states_survive_restart_and_replay(self) -> None:
        cases: tuple[tuple[records.TestWidth, records.Call, str], ...] = (
            (records.RequestedTestWidth(3), test_phase.CallRecord("recorded"), "nextest"),
            (records.UnspecifiedTestWidth(), test_phase.NoCallRecord(), "nextest"),
            (records.NoTestWidth(), test_phase.NoCallRecord(), "clippy"))
        for width, call, step in cases:
            with self.subTest(width=width), AdmissionFixture() as fixture:
                request = fixture.request(width=width, call=call, step=step)
                granted = only_admitted(fixture.server.handle_request(request))
                record = fixture.server.reservations[request.episode]
                row = service.reservation_row(record)
                self.assertEqual(row["test_threads"], width)
                self.assertEqual(row["call_id"], call)
                fixture.restart()
                self.assertEqual(fixture.server.reservations[request.episode], record)
                self.assertNotIn('null', fixture.state_text())
                if request.episode not in fixture.server.reservations:
                    fixture.allow_next_start()
                self.assertEqual(only_admitted(fixture.server.handle_request(request)), granted)
                self.assertEqual(len(fixture.server.reservations), 1)

    def test_running_and_idle_views_refresh_at_the_configured_interval(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            _ = write_config_fixture(config, "running_pass_seconds=3\n")
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            granted = only_admitted(fixture.server.handle_request(request))
            for resident in (GIB, 5 * GIB):
                record = fixture.server.reservations[request.episode]
                scope = Path(Path(record.sidecar).read_text())
                _ = (scope / "memory.current").write_text(str(resident))
                fixture.clock.advance(3)
                fixture.server.run_once(0)
                reply = fixture.server.handle_request(wire.ViewRequest())[0]
                assert isinstance(reply, wire.ViewReply)
                self.assertFalse(reply.reading.view.waiting)
                step, = (step for step in reply.reading.view.running if gate.running_has_reservation(step))
                assert isinstance(step.measured, records.MeasuredMemory)
                self.assertEqual(step.measured.bytes, resident)
            _ = fixture.server.handle_request(wire.ReleaseRequest("end", granted.reservation, 0))
            fixture.readings.counts.clear()
            fixture.clock.advance(100)
            fixture.server.run_once(0)
            self.assertEqual(fixture.readings.counts["host"], 1)
            fixture.server.run_once(0)
            self.assertEqual(fixture.readings.counts["host"], 1)

    def test_supplied_readings_are_taken_once_per_pass(self) -> None:
        with AdmissionFixture() as fixture:
            _ = fixture.server.handle_request(fixture.request())
            fixture.readings.counts.clear()
            _ = fixture.server.run_pass()
            self.assertEqual(set(fixture.readings.counts.values()), {1})
            self.assertEqual(fixture.readings.counts["host"], 1)
            self.assertEqual(fixture.readings.counts["pressure"], 1)
            self.assertEqual(fixture.readings.counts["compilers"], 1)

    def test_crash_after_grant_persistence_replays_one_reservation_and_place(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            fixture.fault.stage = "after_state_write"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(request)
            fixture.restart()
            granted = only_admitted(fixture.server.handle_request(request))
            if request.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            self.assertEqual(granted, only_admitted(fixture.server.handle_request(request)))
            self.assertEqual(len(fixture.server.reservations), 1)
            self.assertEqual(len(fixture.server.places), 1)
            record = fixture.server.reservations[request.episode]
            self.assertEqual(granted.reservation, record.reservation_id)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertGreaterEqual(gate.run_memories(inputs)[0].need, request.profile.peak.bytes)

    def test_release_crashes_finish_once_from_saved_intent_or_history(self) -> None:
        for boundary in ("after_state_write", "after_finalization_intent", "after_history_append"):
            with self.subTest(boundary=boundary), AdmissionFixture() as fixture:
                request = fixture.request()
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                granted = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(granted)
                record = fixture.server.reservations[request.episode]
                release = wire.ReleaseRequest("release-crashed", granted.reservation, 137)
                if boundary == "after_finalization_intent":
                    fixture.fault.stage = boundary
                    with self.assertRaises(InjectedCrash):
                        _ = fixture.server.handle_request(release)
                    saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                    self.assertIsInstance(saved.reservations[request.episode].finalization, dict)
                    self.assertFalse(list(fixture.state_dir.glob("*.reservation")))
                    self.assertFalse(fixture.history())
                else:
                    def stop_after_durable_release(stage: str) -> None:
                        if stage != boundary:
                            return
                        saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                        if boundary == "after_state_write" and release.request_id not in saved.ledger:
                            return
                        raise InjectedCrash(boundary)
                    with mock.patch.object(fixture.server, "fault", side_effect=stop_after_durable_release):
                        with self.assertRaises(InjectedCrash):
                            _ = fixture.server.handle_request(release)
                    if boundary == "after_state_write":
                        saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                        self.assertEqual(saved.ledger[release.request_id].reply, wire.Ok())
                        self.assertFalse(saved.reservations)
                        self.assertEqual(len(fixture.history()), 1)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                rows = fixture.history()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["reservation_id"], record.reservation_id)
                self.assertEqual(rows[0]["outcome"], "killed")
                self.assertFalse(Path(record.path).exists())
                self.assertFalse(Path(granted.max_path).exists())
                self.assertFalse(fixture.server.reservations)

    def test_normal_release_appends_without_reading_history_rows(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 9 * GIB)
            history = fixture.state_dir / "anon_peaks.jsonl"
            store.append_line(history, {"reservation_id": "earlier"})
            scanned = AssertionError("normal release scanned history")
            with (mock.patch.object(store, "whole_rows", side_effect=scanned),
                  mock.patch.object(store, "locked_rows", side_effect=scanned)):
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("end", grant.reservation, 0)), [wire.Ok()])
            rows = fixture.history()
            self.assertEqual([row["reservation_id"] for row in rows], ["earlier", grant.reservation])
            self.assertEqual(rows[1]["anon_peak_bytes"], 9 * GIB)
            self.assertFalse(fixture.server.reservations)

    def test_crash_recovery_finds_the_existing_record_after_the_saved_history_offset(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 9 * GIB)
            history = fixture.state_dir / "anon_peaks.jsonl"
            store.append_line(history, {"reservation_id": "earlier"})
            offset = history.stat().st_size
            fixture.fault.stage = "after_history_append"
            release = wire.ReleaseRequest("end", grant.reservation, 137)
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(release)
            saved = service.read_state(fixture.state_dir).reservations[request.episode].finalization
            assert isinstance(saved, dict)
            self.assertEqual(saved.get("history_start"), offset)
            with mock.patch.object(store, "whole_rows", wraps=store.whole_rows) as scan:
                fixture.restart()
                scan.assert_any_call(mock.ANY, offset)
            self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            rows = fixture.history()
            self.assertEqual([row["reservation_id"] for row in rows], ["earlier", grant.reservation])
            self.assertEqual((rows[1]["outcome"], rows[1]["status"]), ("killed", 137))
            self.assertFalse(fixture.server.reservations)

    def test_mutation_request_ids_replay_without_repeating_changes(self) -> None:
        with AdmissionFixture() as fixture:
            granted = only_admitted(fixture.server.handle_request(fixture.request()))
            width = wire.WidthRequest("width-once", granted.reservation, 4)
            self.assertEqual(fixture.server.handle_request(width), [wire.Ok()])
            state = fixture.state_text()
            self.assertEqual(fixture.server.handle_request(replace(width, width=2)), [wire.Ok()])
            self.assertEqual(fixture.state_text(), state)
            release = wire.ReleaseRequest("release-once", granted.reservation, 0)
            self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            state = fixture.state_text()
            self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            self.assertEqual(fixture.state_text(), state)

            for process in ("current", "fresh"):
                with self.subTest(process=process):
                    if process == "current":
                        fixture.restart()
                        restored = wire.encode_record(fixture.server.ledger[release.request_id].reply)
                    else:
                        result = subprocess.run([sys.executable, "-c", "\n".join((
                            "import sys",
                            "from scripts.build_queue import admission_protocol as wire",
                            "from scripts.build_queue.admission_test_support import AdmissionFixture",
                            "with AdmissionFixture() as fixture:",
                            "    (fixture.state_dir / 'state.json').write_text(sys.stdin.read())",
                            "    fixture.restart()",
                            "    print(wire.encode_record(fixture.server.ledger['release-once'].reply))",
                        ))], input=state, capture_output=True, text=True,
                            cwd=Path(__file__).resolve().parents[2],
                            timeout=test_hang_guard.TEST_HANG_GUARD_SECS, check=False)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        restored = result.stdout.strip()
                    self.assertEqual(restored, wire.encode_record(wire.Ok()))

    def test_restart_thirty_waiters_keeps_places_and_order_fields(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            requests = [fixture.request(since=fixture.clock.now() - 600 + index) for index in range(30)]
            for request in requests:
                _ = fixture.server.handle_request(request)
            places = fixture.server.places.copy()
            fixture.restart()
            for request in reversed(requests):
                _ = fixture.server.handle_request(request)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual([row["pid"] for _, row in inputs.ordered], [request.owner.pid for request in requests])
            self.assertEqual(fixture.server.places, places)
            for _, row in gate.ranked_queue_waiters(inputs):
                self.assertEqual(records.waiter_priority(row), records.NormalOrder())
                self.assertNotIn("head_since", row)
                self.assertNotIn("head_state", row)
            for forbidden in ('"head_since"', '"head_state"', '"bump_until"', '"bump_ends_at"'):
                self.assertNotIn(forbidden, fixture.state_text())

    def test_snapshot_ranking_uses_priority_then_run_place_then_step_arrival_then_pid(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            joined = fixture.clock.now() - 600
            first = fixture.request(since=joined, owner=fixture.fixture_owner(700002, "4000"))
            tied = fixture.request(since=joined, owner=fixture.fixture_owner(700003, "4000"))
            younger = fixture.request(since=joined + 1, owner=fixture.fixture_owner(700001, "4000"))
            for request in (younger, tied, first):
                _ = fixture.server.handle_request(request)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual([row["pid"] for _, row in inputs.ordered],
                             [first.owner.pid, tied.owner.pid, younger.owner.pid])
            rows = {row["pid"]: row for _, row in inputs.ordered}
            self.assertEqual([rows[request.owner.pid]["since"] for request in (first, tied, younger)],
                             [joined, joined, joined + 1])
            priority = gate.rank_waiter(rows[younger.owner.pid], records.PriorityUnit("urgent"))
            ranked = sorted([rows[tied.owner.pid], priority, rows[first.owner.pid]], key=gate.waiter_order)
            self.assertEqual([row["pid"] for row in ranked],
                             [younger.owner.pid, first.owner.pid, tied.owner.pid])
            self.assertEqual(priority["since"], younger.since)
            for request in (first, tied, younger):
                self.assertEqual(rows[request.owner.pid].get("order_since"), request.since)
            moved = records.stored_waiter({**rows[first.owner.pid], "order_since": younger.since + 1})
            self.assertEqual(moved.get("order_since"), younger.since + 1)
            self.assertGreater(gate.waiter_order(moved), gate.waiter_order(rows[younger.owner.pid]))
            same_run_time = records.stored_waiter({**moved, "order_since": younger.since})
            self.assertLess(gate.waiter_order(same_run_time), gate.waiter_order(rows[younger.owner.pid]))

    def test_two_episodes_of_one_step_reuse_its_first_place(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request(since=fixture.clock.now() - 200)
            granted = only_admitted(fixture.server.handle_request(first))
            places = tuple(fixture.server.places.values())
            _ = fixture.server.handle_request(wire.ReleaseRequest("token-release", granted.reservation, 0))
            fixture.free(0)
            fixture.hold_starts()
            second = replace(first, episode="after-token")
            _ = fixture.server.handle_request(second)
            waiting = fixture.server.waiters[second.episode]
            self.assertEqual(waiting.row["since"], first.since)
            place = tuple(fixture.server.places.values())[0]
            self.assertEqual((place.since, place.priority_since),
                             (places[0].since, places[0].priority_since))
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            fixture.clock.advance(0.001)
            _ = fixture.server.run_pass()
            later = only_admitted([fixture.server.ledger[second.episode].reply])
            self.assertNotEqual(granted.reservation, later.reservation)
            self.assertEqual(set(fixture.server.reservations), {second.episode})

    def test_owner_exit_removes_waiter_and_unscoped_reservation_through_pidfd(self) -> None:
        for waiting in (True, False):
            with self.subTest(waiting=waiting), AdmissionFixture() as fixture:
                fixture.free(0 if waiting else 60 * GIB)
                request = fixture.request()
                _ = fixture.server.handle_request(request)
                fixture.proc.kill(request.owner)
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.waiters)
                self.assertFalse(fixture.server.reservations)
                self.assertFalse(fixture.server.places)

    def test_scope_outlives_its_shell_and_empty_scope_event_releases_charge(self) -> None:
        with AdmissionFixture() as fixture:
            owner, scope, stop = fixture.transient_scope()
            sidecar = fixture.root / "real-scope.cgroup"
            _ = sidecar.write_text(str(scope))
            request = replace(fixture.request(owner=owner), sidecar=str(sidecar))
            guard = gate.check_memory_reading
            def membership_only(path: Path) -> None:
                # cgroup.procs is lifecycle evidence; no live memory file is allowed.
                if path != scope / "cgroup.procs":
                    guard(path)
            def supplied_scope_memory(_path: Path, row: records.Reservation, _now: object,
                                      _processes: object, _proc: Path, policy: records.AdmissionPolicy) -> records.StepMemory:
                _ = policy
                lifetime = fixture.proc.lifetime(row, fixture.proc.owner_start(owner.pid) == service.LiveOwnerIdentity(owner.start_time))
                return records.GoneMemory() if isinstance(lifetime, gate.EndedStep) else records.MeasuredMemory(GIB)
            with mock.patch.object(gate, "check_memory_reading", side_effect=membership_only), \
                 mock.patch.object(fixture.readings, "reservation", side_effect=supplied_scope_memory):
                _ = only_admitted(fixture.server.handle_request(request))
                _ = fixture.server.run_pass()
                descriptor = os.pidfd_open(owner.pid)
                try:
                    os.kill(owner.pid, signal.SIGKILL)
                    self.assertTrue(select.select([descriptor], [], [], 3)[0], "owner must exit")
                finally:
                    os.close(descriptor)
                fixture.server.run_once(0)
                self.assertEqual(len(fixture.server.reservations), 1)
                stop()
                fixture.server.run_once(0)
                # Ended lifetime comes from the real empty scope, not owner polling.
                self.assertFalse(fixture.server.reservations)

    def test_owner_scan_omission_keeps_a_waiter_and_its_saved_place(self) -> None:
        from scripts.lib.proc_text import read_proc_text

        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                older = fixture.request(age=20)
                younger = fixture.request()
                peer = fixture.connect(older)
                self.assertIsInstance(peer.replies()[0], wire.Wait)
                other = fixture.connect(younger)
                self.assertIsInstance(other.replies()[0], wire.Wait)
                server = fixture.server
                config = server.last_usable_configuration
                assert isinstance(config, records.MemoryConfig)
                evidence = service.read_system(service.SystemReadingPlan(config, (), (), False),
                    fixture.readings, fixture.proc, fixture.clock)
                processes = dict(evidence.processes)
                del processes[older.owner.pid]
                server.system_reader = FixedSystemReader(replace(evidence, processes=processes))
                read_owner = fixture.proc.owner_start

                def confirm_owner(pid: int) -> service.OwnerStartReading:
                    if unreadable and pid == older.owner.pid:
                        return service.SystemProc().owner_start(pid)
                    return read_owner(pid)

                older_stat = fixture.proc.path / str(older.owner.pid) / "stat"

                def read_with_unreadable_stat(path: Path) -> str:
                    if path == older_stat:
                        raise PermissionError("temporary read failure")
                    return read_proc_text(path)

                with (mock.patch.object(fixture.proc, "owner_start", side_effect=confirm_owner) as confirm,
                      mock.patch.object(service, "read_proc_text", read_with_unreadable_stat)):
                    fixture.end_owner(younger.owner)
                self.assertIn(older.episode, set(server.waiters))
                self.assertTrue(any(place.episode == older.episode and place.since == older.since
                                    for place in service.read_state(fixture.state_dir).places.values()))
                self.assertEqual(confirm.call_args_list.count(mock.call(older.owner.pid)), 1)

    def test_removing_a_dead_waiter_closes_its_connection(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request()
            peer = fixture.connect(request)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            fixture.end_owner(request.owner)
            self.assertNotIn(request.episode, fixture.server.waiters)
            self.assertEqual(peer.socket.recv(1), b"")

    def test_replacing_a_waiter_connection_keeps_its_order_and_closes_the_old_connection(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            older = fixture.request(age=20)
            younger = fixture.request()
            old = fixture.connect(older)
            self.assertIsInstance(old.replies()[0], wire.Wait)
            self.assertIsInstance(fixture.connect(younger).replies()[0], wire.Wait)
            replacement = fixture.connect(older)
            self.assertIsInstance(replacement.replies()[0], wire.Wait)
            self.assertEqual(fixture.queue_pids(), [older.owner.pid, younger.owner.pid])
            waiting = fixture.server.waiters[older.episode]
            self.assertEqual((waiting.request.episode, waiting.row["since"]), (older.episode, older.since))
            self.assertEqual(old.socket.recv(1), b"")

    def test_a_stale_socket_event_cannot_end_the_owner_of_a_reused_descriptor(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            older = fixture.request(age=20)
            self.assertIsInstance(fixture.connect(older).replies()[0], wire.Wait)
            server = fixture.server
            old_connection = server.waiters[older.episode].connection
            assert isinstance(old_connection, service.Connection)
            descriptor = old_connection.socket.fileno()
            events = fixture.scope_events()
            events.events = [(descriptor, select.EPOLLHUP)]
            replacement = fixture.connect(older)
            younger = fixture.request()
            _ = fixture.connect(younger)
            open_pidfd = fixture.proc.pidfd
            reused: list[int] = []

            def open_owner_watch(pid: int) -> int:
                owner_descriptor = open_pidfd(pid)
                if pid == younger.owner.pid:
                    reused.append(owner_descriptor)
                return owner_descriptor

            with mock.patch.object(fixture.proc, "pidfd", side_effect=open_owner_watch):
                server.run_once(0)
            self.assertEqual(reused, [descriptor])
            self.assertIn(younger.episode, set(server.waiters))
            self.assertEqual(server.watchers[descriptor], service.OwnerWatch(younger.owner))
            saved = service.read_state(fixture.state_dir)
            self.assertIn(younger.episode, {place.episode for place in saved.places.values()})
            self.assertIsInstance(replacement.replies()[0], wire.Wait)

    def test_closed_waiting_connection_leaves_queue_but_grant_survives_disconnect(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            peer = fixture.connect(fixture.request())
            _ = peer.replies()
            peer.close()
            fixture.server.run_once(0)
            self.assertFalse(fixture.server.waiters)
            fixture.free(60 * GIB)
            peer = fixture.connect(fixture.request())
            _ = only_admitted(peer.replies())
            peer.close()
            fixture.server.run_once(0)
            self.assertEqual(len(fixture.server.reservations), 1)

    def test_unparsed_request_refusals_decode_with_both_protocol_versions(self) -> None:
        for version in (1, 2):
            for kind in ("malformed", "fragmented", "oversize-newline", "oversize-incomplete"):
                with self.subTest(version=version, kind=kind), AdmissionFixture() as fixture:
                    fixture.configure(request_max_bytes=512)
                    _ = fixture.run_pass()
                    prefix = f'{{"v":{version},'
                    if kind.startswith("oversize"):
                        line = prefix + '"data":"'
                        tail = '","type":"view"}\n' if kind == "oversize-newline" else ""
                        limit = max(512, wire.CLEANUP_REQUEST_MAX_BYTES)
                        line += "x" * (limit + 1 - len(line) - len(tail)) + tail
                    else:
                        line = prefix if kind == "fragmented" else prefix + '"type":"view","data": }\n'
                    sent: list[str] = []

                    def capture(line: str) -> str:
                        sent.append(line)
                        return line

                    with mock.patch.object(fixture.server, "line_filter", side_effect=capture):
                        peer = fixture.connect(line)
                        if kind == "fragmented":
                            self.assertEqual(peer.replies(), [])
                            peer.socket.sendall(b'"type":"view","data": }\n')
                        reply, = peer.replies()
                    self.assertIsInstance(reply, wire.Refused)
                    self.assertEqual(codec.snapshot_value(decode_legacy(sent[-1])), codec.snapshot_value(reply))
                    self.assertEqual(wire.parse_reply(sent[-1]), reply)

    def test_unknown_version_gets_explicit_refusal_and_socket_is_private(self) -> None:
        with AdmissionFixture() as fixture:
            peer = fixture.connect('{"v":4,"type":"view","data":{}}\n')
            self.assertEqual(peer.replies(), [wire.Refused("client version 4, service supports 1..3")])
            self.assertEqual(stat.S_IMODE(fixture.socket_path.stat().st_mode), 0o600)

    def test_aged_launch_alone_does_not_create_a_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(4 * GIB)
            fixture.compiler(8 * GIB)
            request = fixture.launch(since=fixture.clock.now() - 900)
            self.assertIsInstance(fixture.server.handle_request(request)[0], wire.LaunchAdmitted)
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)
            self.assertEqual(wire.parse_record(fixture.state_text(), service.ServiceState).reservations, {})
            fixture.hold_starts()
            held, = fixture.server.handle_request(replace(request, episode="held-launch"))
            self.assertIsInstance(held, wire.Wait)
            self.assertFalse(fixture.server.reservations)

    def test_expiry_and_refresh_keep_episode_place_and_order_fields(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request(age=admission_policy_fixture().timing.estimate_refresh_seconds - 1)
            young = fixture.server.handle_request(request)[0]
            assert isinstance(young, wire.Wait)
            self.assertNotIsInstance((young.reason.cause if isinstance(young.reason, records.HeadAdmissionStatus) else young.reason), records.EstimateExpired)
            fixture.clock.advance(1)
            _ = fixture.server.run_pass()
            expired = fixture.server.waiters[request.episode].last_reply
            assert isinstance(expired, wire.Wait)
            self.assertIsInstance((expired.reason.cause if isinstance(expired.reason, records.HeadAdmissionStatus) else expired.reason), records.EstimateExpired)
            place = tuple(fixture.server.places.values())[0]
            refreshed = replace(request, profile=replace(request.profile, estimated_at=fixture.clock.now()))
            _ = fixture.server.handle_request(refreshed)
            self.assertEqual(tuple(fixture.server.places.values()), (place,))
            self.assertEqual(len(fixture.server.waiters), 1)


    def test_loaded_source_change_or_deletion_stops_after_two_quiet_seconds(self) -> None:
        for delete in (False, True):
            with self.subTest(delete=delete), AdmissionFixture() as fixture:
                source = fixture.root / "loaded.py"
                _ = source.write_text("old")
                info = source.stat()
                fixture.server.sources = {source: (info.st_ino, info.st_mtime_ns)}
                if delete:
                    source.unlink()
                else:
                    _ = source.write_text("new code")
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.stopped)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.stopped)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertTrue(fixture.server.stopped)
                self.assertTrue((fixture.state_dir / "state.json").exists())

    def test_source_watch_logs_start_change_stop_and_replacement_revision(self) -> None:
        with AdmissionFixture() as fixture, redirect_stderr(io.StringIO()) as journal:
            fixture.restart()
            server = fixture.server
            count = len(server.sources)
            first_check = server.next_source_check
            source = fixture.root / "loaded.py"
            _ = source.write_text("old")
            stamp = source.stat()
            server.sources = {source: (stamp.st_ino, stamp.st_mtime_ns)}
            _ = source.write_text("changed")
            fixture.clock.advance(1)
            server.run_once(0)
            fixture.clock.advance(2)
            server.run_once(0)
            self.assertTrue(server.stopped)
            fixture.restart()
            revision = subprocess.check_output(
                ["git", "-C", str(Path(service.__file__).resolve().parent), "rev-parse", "HEAD"],
                text=True).strip()
            entries = journal.getvalue().splitlines()
            self.assertIn(f"memory admission: source watch checks {count} paths first at monotonic {first_check:.3f}",
                          entries)
            self.assertTrue(any("source watch detected changes" in entry and str(source) in entry
                                for entry in entries))
            self.assertIn("memory admission: source watch will stop after the changes settled", entries)
            self.assertEqual(entries[-1], f"memory admission: ready with git revision {revision}")

    def test_a_module_loaded_after_start_stops_the_service_when_it_changes(self) -> None:
        from scripts.mac_test import eligibility

        with AdmissionFixture() as fixture:
            # eligibility.py loads inside a function on the first Mac read, after the service starts.
            fixture.server.source_root = fixture.root
            source = fixture.root / "mac_test" / "eligibility.py"
            source.parent.mkdir()
            _ = source.write_text(Path(eligibility.__file__).read_text())
            module = types.ModuleType("loaded_after_start")
            module.__file__ = str(source)
            with mock.patch.dict(sys.modules, {module.__name__: module}):
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertIn(source, fixture.server.sources)
                self.assertIsInstance(fixture.server.source_change_at, service.SourcesUnchanged)
                _ = source.write_text(source.read_text() + "\n# changed\n")
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.stopped)
                fixture.clock.advance(1)
                fixture.server.run_once(0)
                self.assertTrue(fixture.server.stopped)

    def test_serve_defaults_to_the_private_admission_directory(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.server.close()
            with mock.patch.object(sys, "argv", ["admission_service", "serve"]), \
                 mock.patch.object(service, "SystemClock", return_value=fixture.clock), \
                 mock.patch.object(service, "SystemReadings", return_value=fixture.readings), \
                 mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(service.AdmissionServer, "serve_forever") as serve:
                self.assertEqual(service.main(), 0)
            serve.assert_called_once_with()
            self.assertTrue((fixture.root / "log/admission/state.json").exists())


class CutoverServiceTests(unittest.TestCase):


    def test_release_records_each_closed_outcome_once_and_removes_the_sampler_paths(self) -> None:
        for status, outcome in ((0, "completed"), (1, "failed"), (127, "failed"),
                                (128, "killed"), (137, "killed")):
            with self.subTest(status=status), AdmissionFixture() as fixture:
                request = fixture.request(need=12 * GIB, step="build")
                request = replace(request, argv=("cargo", "build", "--release", "--bin", "fixture"))
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                granted = only_admitted(fixture.server.handle_request(request))
                record = fixture.server.reservations[request.episode]
                fixture.scoped_peak(granted, 10 * GIB)
                release = wire.ReleaseRequest("once", granted.reservation, status)
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                row, = fixture.history()
                self.assertEqual((row["status"], row["outcome"], row["need"], row["reservation_id"]),
                                 (status, outcome, 12 * GIB, granted.reservation))
                for path in (record.path, granted.max_path, granted.wave_path, record.sidecar):
                    self.assertFalse(Path(path).exists(), path)
                self.assertEqual(row["shape"], build_shape(list(request.argv)).key)
                self.assertFalse(fixture.server.reservations)

    def test_two_release_builds_in_one_call_keep_shape_and_separate_history(self) -> None:
        with AdmissionFixture() as fixture:
            argv = ("cargo", "build", "--release", "--features=bevy/trace_chrome", "--bin", "hana")
            ids: list[str] = []
            for index in range(2):
                request = fixture.request(need=12 * GIB, step="build", call=test_phase.CallRecord("one-call"))
                request = replace(request, argv=argv, sidecar=str(fixture.scope(request.owner)))
                fixture.allow_next_start()
                grant = only_admitted(fixture.server.handle_request(request))
                ids.append(grant.reservation)
                record = fixture.server.reservations[request.episode]
                self.assertEqual(record.shape, build_shape(list(argv)).key)
                fixture.scoped_peak(grant, (10 + index) * GIB)
                release = wire.ReleaseRequest(f"release-{index}", grant.reservation, 0)
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
                self.assertEqual(fixture.server.handle_request(release), [wire.Ok()])
            rows = fixture.history()
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["reservation_id"] for row in rows], ids)
            self.assertEqual([row["call_id"] for row in rows], ["one-call", "one-call"])
            self.assertEqual([row["anon_peak_bytes"] for row in rows], [10 * GIB, 11 * GIB])
            self.assertTrue(all(row["shape"] == build_shape(list(argv)).key for row in rows))

    def test_unfinished_history_append_retries_the_saved_outcome_once(self) -> None:
        from scripts.lint.test_memory_admit import stop_history_append, HISTORY_APPEND_STOPS
        for tail in HISTORY_APPEND_STOPS:
            with self.subTest(tail=tail), AdmissionFixture() as fixture:
                request = fixture.request(step="build")
                request = replace(request, sidecar=str(fixture.scope(request.owner)),
                    profile=replace(request.profile, worktree="build-Δ"))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(grant, 15 * GIB)
                release = wire.ReleaseRequest("stopped-release", grant.reservation, 137)
                with stop_history_append(store, tail):
                    with self.assertRaisesRegex(OSError, "injected stop"):
                        _ = fixture.server.handle_request(release)
                saved = service.read_state(fixture.state_dir).reservations[request.episode]
                self.assertIsInstance(saved.finalization, dict)
                fixture.restart()
                self.assertEqual(fixture.server.handle_request(replace(release, status=0)), [wire.Ok()])
                row, = fixture.history()
                self.assertEqual((row["outcome"], row["status"], row["anon_peak_bytes"]),
                                 ("killed", 137, 15 * GIB))
                self.assertEqual(row["worktree"], "build-Δ")
                self.assertFalse(fixture.server.reservations)

    def test_saved_finalization_keeps_scope_evidence_after_sidecar_removal(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 14 * GIB)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("stop", grant.reservation, 137))
            Path(request.sidecar).unlink()
            fixture.restart()
            row, = fixture.history()
            self.assertEqual((row["outcome"], row["anon_peak_bytes"]), ("killed", 14 * GIB))
            self.assertFalse(fixture.server.reservations)

    def test_peak_read_error_keeps_all_sampler_evidence_for_retry(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 14 * GIB)
            _ = Path(grant.wave_path).write_text(f"{14 * GIB} {fixture.clock.now()}")
            paths = (Path(grant.max_path), Path(grant.wave_path), Path(request.sidecar))
            before = {path: path.read_bytes() for path in paths}
            read = Path.read_text
            def fail_peak(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
                if path == Path(grant.max_path):
                    raise PermissionError("the peak is unreadable")
                return read(path, encoding=encoding, errors=errors)
            with mock.patch.object(Path, "read_text", fail_peak):
                with self.assertRaisesRegex(PermissionError, "peak is unreadable"):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("retry", grant.reservation, 0))
            self.assertEqual({path: path.read_bytes() for path in paths}, before)
            self.assertFalse(fixture.history())
            fixture.restart()
            row, = fixture.history()
            self.assertEqual(row["anon_peak_bytes"], 14 * GIB)
            self.assertFalse(fixture.server.reservations)

    def test_cleanup_retries_after_each_sampler_file_removal_without_duplicate_history(self) -> None:
        for suffix in (".max", ".wave", ".cgroup"):
            with self.subTest(suffix=suffix), AdmissionFixture() as fixture:
                request = fixture.request()
                request = replace(request, sidecar=str(fixture.scope(request.owner)))
                grant = only_admitted(fixture.server.handle_request(request))
                fixture.scoped_peak(grant)
                stop = Path(request.sidecar) if suffix == ".cgroup" else Path(grant.max_path).with_suffix(suffix)
                unlink = Path.unlink
                def stop_after_removal(path: Path, missing_ok: bool = False) -> None:
                    unlink(path, missing_ok=missing_ok)
                    if path == stop:
                        raise InterruptedError("cleanup stopped")
                with mock.patch.object(Path, "unlink", stop_after_removal):
                    with self.assertRaisesRegex(InterruptedError, "cleanup stopped"):
                        _ = fixture.server.handle_request(wire.ReleaseRequest("cleanup", grant.reservation, 0))
                self.assertEqual(len(fixture.history()), 1)
                fixture.restart()
                self.assertEqual(len(fixture.history()), 1)
                self.assertFalse(fixture.server.reservations)
                for path in (Path(grant.max_path), Path(grant.wave_path), Path(request.sidecar)):
                    self.assertFalse(path.exists(), str(path))

    def test_unscoped_release_writes_no_peak_history(self) -> None:
        with AdmissionFixture() as fixture:
            grant = only_admitted(fixture.server.handle_request(fixture.request()))
            _ = Path(grant.max_path).write_text(str(14 * GIB))
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("unscoped", grant.reservation, 0)), [wire.Ok()])
            self.assertFalse(fixture.history())
            self.assertFalse(Path(grant.max_path).exists())


    def test_priority_and_frozen_release_keep_the_resolved_head(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            older = fixture.request(need=20 * GIB, since=fixture.clock.now() - 600)
            chosen = fixture.request(need=12 * GIB, since=fixture.clock.now() - 60)
            for request in (older, chosen):
                _ = fixture.server.handle_request(request)
            with mock.patch.object(gate, "priority_labels", return_value={
                    older.identity.session_id: UnitLabels("old", unit="other"),
                    chosen.identity.session_id: UnitLabels("chosen", unit="chosen")}):
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                    "choose", "chosen", "urgent", "director")), [wire.Ok()])
                explanation = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(explanation, wire.ExplainReply)
                self.assertEqual(explanation.explanation.ordered[0]["pid"], chosen.owner.pid)
                head = explanation.explanation.head
                assert isinstance(head, records.HeadExplanation)
                self.assertEqual(head.waiter.pid, chosen.owner.pid)
                frozen = explanation.explanation
                with mock.patch.object(gate, "priority_labels", side_effect=AssertionError("frozen roster")), \
                     mock.patch.object(Path, "read_text", side_effect=AssertionError("frozen files")):
                    self.assertIsInstance(gate.smallest_head_release(frozen), gate.HeadStillHeld)
                    self.assertIsInstance(gate.head_after_steps_end(frozen, ()), gate.HeadStillHeld)
                fixture.free(60 * GIB)
                _ = fixture.server.run_pass()
                self.assertIn(chosen.episode, fixture.server.reservations)

    def test_policy_edits_reclassify_pressure_in_both_directions(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="gate", need=8 * GIB)
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 8 * GIB)
            pressure = Path(os.environ["BUILDLOG_MEMORY_PRESSURE"])
            fixture.free(0)
            fixture.hold_starts()
            _ = pressure.write_text("full avg10=7.0 avg60=0 avg300=0 total=0\nsome avg10=0\n")
            waiting = fixture.request()
            _ = fixture.server.handle_request(waiting)
            for old, new, held in ((5, 10, False), (10, 5, True)):
                fixture.configure(full_pressure_limit=old)
                _ = fixture.server.run_pass()
                before = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(before, wire.ExplainReply)
                fixture.configure(full_pressure_limit=new)
                _ = fixture.server.run_pass()
                after = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(after, wire.ExplainReply)
                self.assertEqual(isinstance(before.explanation.decided_from.pressure, records.MemoryPressure), not held)
                self.assertEqual(isinstance(after.explanation.decided_from.pressure, records.MemoryPressure), held)

    def test_ramp_growth_and_release_keep_peak_width_call_and_repository(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=12 * GIB, width=records.RequestedTestWidth(16), call=test_phase.CallRecord("call-width-16"))
            request = replace(request, sidecar=str(fixture.scope(request.owner, current=8 * GIB, anonymous=8 * GIB)),
                profile=records.StepProfile("hana", "nextest", "tree", records.ExpectedPeak(12 * GIB, "measured", 5),
                    records.RepositoryIdentity("https://example.org/hana", "hana"), "full", fixture.clock.now()))
            grant = only_admitted(fixture.server.handle_request(request))
            _ = fixture.server.run_pass()
            reading = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(reading, wire.ViewReply)
            running = reading.reading.view.running[0].measured
            assert isinstance(running, records.MeasuredMemory)
            room = reading.reading.explanation.room
            assert isinstance(room, records.MemoryRoom)
            self.assertEqual((room.kept, room.used), (4 * GIB, 8 * GIB))
            fixture.scoped_peak(grant, 8 * GIB)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("end-ramp", grant.reservation, 7)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual((row["anon_peak_bytes"], row["status"], row["test_threads"], row["call_id"]),
                (8 * GIB, 7, 16, "call-width-16"))
            self.assertEqual((row["repo_identity"], row["test_scope"]), ("https://example.org/hana", "full"))
            self.assertFalse(Path(request.sidecar).exists())
            self.assertFalse(Path(grant.max_path).exists())
            self.assertFalse(fixture.server.reservations)


    def test_priority_list_appends_repeats_and_clears_units_without_changing_group_order(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            requests = [fixture.request(since=fixture.clock.now() - age) for age in (900, 600, 400, 200, 100)]
            units = ("other", "cables", "demo", "demo", "cables")
            labels = {request.identity.session_id: UnitLabels("worker", unit=unit)
                      for request, unit in zip(requests, units)}
            for request in requests:
                _ = fixture.server.handle_request(request)
            with mock.patch.object(gate, "priority_labels", return_value=labels), mock.patch.object(
                    buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged:
                for unit in ("demo", "cables"):
                    self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                        "bump-" + unit, unit, "why-" + unit, "actor-" + unit)), [wire.Ok()])
                saved = fixture.server.priority
                assert isinstance(saved, records.QueuePriority)
                self.assertEqual([entry["unit"] for entry in saved.entries], ["demo", "cables"])
                self.assertEqual([entry["why"] for entry in saved.entries], ["why-demo", "why-cables"])
                self.assertEqual([entry["set_by"] for entry in saved.entries], ["actor-demo", "actor-cables"])
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                    "repeat-demo", "demo", "replacement reason", "another actor")), [wire.Ok()])
                self.assertEqual(fixture.server.priority, saved)
                self.assertEqual(logged.call_count, 2)
                fixture.restart()
                for request in requests:
                    _ = fixture.server.handle_request(request)
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                    [requests[index].owner.pid for index in (2, 3, 1, 4, 0)])
                text = 'queue priority: demo (actor-demo: "why-demo"), cables (actor-cables: "why-cables")'
                self.assertIn(text, head_facts.priority_text(view.reading.explanation.priority))
                self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest(
                    "remove-demo", "director", "demo", why="clear words")), [wire.Ok()])
                priority = fixture.server.priority
                assert isinstance(priority, records.QueuePriority)
                self.assertEqual(priority.entries, saved.entries[1:])
                view, = fixture.server.handle_request(wire.ViewRequest())
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                    [requests[index].owner.pid for index in (1, 4, 0, 2, 3)])
                self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest(
                    "unknown-clear", "director", "missing", why="clear words")), [wire.Refused(
                        "unknown unit missing; the queue priority did not change")])
                self.assertEqual(fixture.server.priority, priority)
                self.assertEqual(logged.call_count, 3)
                self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest(
                    "clear-all", "director", why="clear words")), [wire.Ok()])
                self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
                self.assertEqual(logged.call_count, 4)
                self.assertEqual([call.args[:2] for call in logged.call_args_list],
                    [("set", "demo"), ("set", "cables"), ("clear", "demo"), ("clear", "cables")])

    def test_old_priority_file_and_service_state_read_as_one_entry(self) -> None:
        with AdmissionFixture() as fixture:
            old = {"unit": "demo", "why": "user work", "set_at": fixture.clock.now(),
                   "set_by": "director", "transition": {"kind": "SettledPriority"}}
            path = gate.queue_priority_path(fixture.state_dir)
            _ = path.write_text(json.dumps(old))
            before = path.read_bytes()
            priority = gate.load_queue_priority(fixture.state_dir)
            assert isinstance(priority, records.QueuePriority)
            self.assertEqual(priority.entries, ({"unit": "demo", "why": "user work",
                "set_at": fixture.clock.now(), "set_by": "director"},))
            self.assertEqual(records.entry_asker(priority.entries[0]), records.AskerNotRecorded())
            self.assertEqual(head_facts.priority_text(priority), "queue priority: demo (user work)")
            self.assertEqual(path.read_bytes(), before)
            path = fixture.state_dir / "state.json"
            state = cast(dict[str, object], json.loads(path.read_text()))
            state["priority"] = old
            _ = path.write_text(json.dumps(state))
            before = path.read_bytes()
            restored = service.read_state(fixture.state_dir)
            self.assertEqual(restored.priority, priority)
            self.assertEqual(path.read_bytes(), before)
            fixture.restart()
            self.assertEqual(fixture.server.priority, priority)
            viewed, = fixture.server.handle_request(wire.ViewRequest())
            assert isinstance(viewed, wire.ViewReply)
            self.assertEqual(viewed.reading.explanation.priority, priority)
            encoded = wire.encode_reply(viewed)
            self.assertEqual(wire.parse_reply(encoded), viewed)

    def test_priority_requires_a_roster_unit_and_replays_refusals(self) -> None:
        for running in (False, True):
            with self.subTest(running=running), AdmissionFixture() as fixture:
                request = fixture.request()
                fixture.free(60 * GIB if running else 0)
                _ = fixture.server.handle_request(request)
                absent = wire.PrioritySetRequest("absent", "missing", "urgent", "director")
                refusal = wire.Refused("unknown unit missing; the queue priority did not change")
                with mock.patch.object(gate, "priority_labels", return_value={
                        "unrelated": UnitLabels("other", unit="roster-unit"),
                        request.identity.session_id: UnitLabels("worker", unit="present")}):
                    self.assertEqual(fixture.completed_queue_change(absent), [refusal])
                    self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
                    self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                        "present", "present", "urgent", "director")), [wire.Ok()])
                    self.assertEqual(fixture.completed_queue_change(absent), [refusal])
                    priority = fixture.server.priority
                    assert not isinstance(priority, records.NoQueuePriority)
                    self.assertEqual(priority.entries[0]["unit"], "present")

    def test_retired_priority_requests_leave_priority_unchanged(self) -> None:
        with AdmissionFixture() as fixture:
            with mock.patch.object(gate, "priority_labels", return_value={
                    "session": UnitLabels("worker", unit="present")}):
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest(
                    "initial", "present", "urgent", "director")), [wire.Ok()])
            priority = fixture.server.priority
            self.assertIsInstance(priority, records.QueuePriority)
            self.assertEqual(service.read_state(fixture.state_dir).priority, priority)
            request = wire.PrioritySetRequest("retired", "present", "other words", "director")
            envelope = cast(dict[str, object], json.loads(wire.encode_request(request)))
            data = cast(dict[str, object], envelope["data"])
            data["target"] = {"kind": "OneRun", "run": codec.snapshot_value(wire.RunIdentity(41, "900")),
                              "step": "nextest"}
            for name in ("priority_set", "priority_run_set"):
                with self.subTest(name=name):
                    envelope["type"] = name
                    peer = fixture.connect(json.dumps(envelope) + "\n")
                    self.assertEqual(peer.replies(), [wire.Refused("priority applies to a unit")])
                    reply, = fixture.connect(wire.ViewRequest()).replies()
                    assert isinstance(reply, wire.ViewReply)
                    self.assertEqual(reply.reading.explanation.priority, priority)
                    self.assertEqual(service.read_state(fixture.state_dir).priority, priority)

    def test_waiter_keeps_source_basis_scope_and_typed_width_in_state_and_views(self) -> None:
        for source, runs in (("measured", 7), ("buildlog", 12), ("pooled", 31), ("fallback", 0)):
            with self.subTest(source=source), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request(width=records.RequestedTestWidth(8))
                assert isinstance(request.profile, records.StepProfile)
                request = replace(request, argv=("cargo", "nextest", "run", "-E",
                    "package(hana) & (test(one) | test(two))", "--test-threads", "8"),
                    profile=replace(request.profile, scope="filtered", repository=records.RepositoryIdentity(
                        "https://example.org/hana", "hana"), peak=records.ExpectedPeak(12 * GIB, source, runs)))
                _ = fixture.server.handle_request(request)
                row = fixture.server.waiters[request.episode].row
                self.assertEqual((row.get("need_source"), row.get("need_runs"), row["need"]), (source, runs, 12 * GIB))
                self.assertEqual((row.get("test_scope"), row.get("package"), row.get("filters")), ("filtered", "hana", ["one", "two"]))
                self.assertEqual(row.get("estimated_at"), fixture.clock.now())
                fixture.restart()
                _ = fixture.server.handle_request(request)
                saved = fixture.server.waiters[request.episode].row
                self.assertEqual(saved, row)
                reply = fixture.server.handle_request(wire.ExplainRequest())[0]
                assert isinstance(reply, wire.ExplainReply)
                head = reply.explanation.head
                assert isinstance(head, records.HeadExplanation)
                assert isinstance(head.need_source, records.RecordedNeed)
                self.assertEqual((head.need_source.source, head.need_source.runs, head.need_source.basis),
                    (source, runs, gate.read_need_basis(row.get("need_basis"))))
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request(step="clippy", width=records.NoTestWidth())
            _ = fixture.server.handle_request(request)
            stored_request = fixture.server.waiters[request.episode].request
            assert isinstance(stored_request, wire.AdmitRequest)
            self.assertIsInstance(stored_request.test_width, records.NoTestWidth)


    def test_priority_clear_and_replacement_replay_each_transition_once_after_a_crash(self) -> None:
        for action in ("clear", "replace"):
            with self.subTest(action=action), AdmissionFixture() as fixture:
                fixture.free(0)
                fixture.hold_starts()
                request = fixture.request()
                _ = fixture.server.handle_request(request)
                _ = self.enterContext(mock.patch.object(gate, "priority_labels", return_value={
                    request.identity.session_id: UnitLabels("worker", unit="old")}))
                self.assertEqual(fixture.completed_queue_change(
                    wire.PrioritySetRequest("initial", "old", "initial", "director")), [wire.Ok()])
                mutation = (wire.PriorityClearRequest("change", "director", why="clear words") if action == "clear" else
                            wire.PrioritySetRequest("change", "new", "replacement", "director"))
                if action == "replace":
                    _ = self.enterContext(mock.patch.object(gate, "priority_labels", return_value={
                        request.identity.session_id: UnitLabels("worker", unit="new")}))
                with mock.patch.object(buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged:
                    fixture.fault.stage = "after_state_write"
                    with self.assertRaises(InjectedCrash):
                        _ = fixture.completed_queue_change(mutation)
                    logged.assert_not_called()
                    fixture.restart()
                    self.assertEqual(fixture.completed_queue_change(mutation), [wire.Ok()])
                    fixture.restart()
                    self.assertEqual(fixture.completed_queue_change(mutation), [wire.Ok()])
                logged.assert_called_once()
                self.assertEqual(logged.call_args.kwargs["transition_id"], mutation.request_id)
                if action == "clear":
                    self.assertIsInstance(fixture.server.priority, records.NoQueuePriority)
                else:
                    priority = fixture.server.priority
                    assert not isinstance(priority, records.NoQueuePriority)
                    self.assertEqual([entry["unit"] for entry in priority.entries], ["old", "new"])

    def test_unowned_scope_memory_refreshes_without_a_waiter_or_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            scope = fixture.root / "builds.slice/unowned.scope"
            scope.mkdir(parents=True)
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "memory.current").write_text(str(GIB))
            _ = (scope / "memory.stat").write_text("inactive_file 0\n")
            _ = fixture.server.run_pass()
            first = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(first, wire.ViewReply)
            measured = first.reading.view.running[0].measured
            assert isinstance(measured, records.UnownedSliceMemory)
            self.assertEqual(measured.bytes, GIB)
            _ = (scope / "memory.current").write_text(str(17 * GIB))
            _ = (scope / "memory.stat").write_text("inactive_file 0\n")
            fixture.clock.advance(records.read_memory_config().running_pass_seconds)
            fixture.server.run_once(0)
            updated = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(updated, wire.ViewReply)
            self.assertFalse(fixture.server.waiters)
            self.assertFalse(fixture.server.reservations)
            measured = updated.reading.view.running[0].measured
            assert isinstance(measured, records.UnownedSliceMemory)
            self.assertEqual(measured.bytes, 17 * GIB)

    def test_finalization_crash_after_row_removal_expires_the_orphan_replay(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant)
            def crash_after_removal(stage: str) -> None:
                if stage != "after_state_write":
                    return
                saved = wire.parse_record(fixture.state_text(), service.ServiceState)
                if not saved.reservations and request.episode in saved.ledger:
                    raise InjectedCrash("reservation removed before replay end")
            with mock.patch.object(fixture.server, "fault", side_effect=crash_after_removal):
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
            self.assertEqual(len(fixture.history()), 1)
            fixture.restart()
            self.assertFalse(fixture.server.reservations)
            fixture.clock.advance(2 * records.read_memory_config().replay_retention_seconds)
            fixture.server.persist()
            self.assertNotIn(request.episode, fixture.server.ledger)
            fixture.restart()
            self.assertNotIn(request.episode, fixture.server.ledger)
            self.assertEqual(len(fixture.history()), 1)


    def test_priority_replay_after_a_crash_keeps_one_transition_and_the_same_order(self) -> None:
        from scripts.message.unit_labels import UnitLabels
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            first, second = fixture.request(), fixture.request()
            labels = {first.identity.session_id: UnitLabels("first", unit="other"),
                      second.identity.session_id: UnitLabels("second", unit="queue")}
            with mock.patch.object(gate, "priority_labels", return_value=labels), \
                 mock.patch.object(buildlog_record, "queue_priority", wraps=buildlog_record.queue_priority) as logged:
                _ = fixture.server.handle_request(first)
                _ = fixture.server.handle_request(second)
                mutation = wire.PrioritySetRequest("priority-once", "queue", "urgent", "director")
                fixture.fault.stage = "after_state_write"
                with self.assertRaises(InjectedCrash):
                    _ = fixture.completed_queue_change(mutation)
                logged.assert_not_called()
                fixture.restart()
                self.assertEqual(fixture.completed_queue_change(mutation), [wire.Ok()])
                for request in (first, second):
                    _ = fixture.server.handle_request(request)
                fixture.restart()
                for request in (first, second):
                    _ = fixture.server.handle_request(request)
                self.assertEqual(fixture.completed_queue_change(mutation), [wire.Ok()])
            logged.assert_called_once()
            self.assertEqual(logged.call_args.kwargs["transition_id"], mutation.request_id)
            viewed = fixture.server.handle_request(wire.ViewRequest())[0]
            assert isinstance(viewed, wire.ViewReply)
            self.assertEqual([row["pid"] for _, row in viewed.reading.ordered],
                             [second.owner.pid, first.owner.pid])
            self.assertEqual([records.waiter_priority(row) for _, row in viewed.reading.ordered],
                             [records.PriorityUnit("queue"), records.NormalOrder()])
            priority = viewed.reading.explanation.priority
            assert not isinstance(priority, records.NoQueuePriority)
            self.assertEqual(priority.entries[0]["unit"], "queue")

    def test_token_replay_after_a_crash_restores_the_place_and_records_one_gap(self) -> None:
        with AdmissionFixture() as fixture:
            first = fixture.request()
            first = replace(first, sidecar=str(fixture.scope(first.owner)))
            grant = only_admitted(fixture.server.handle_request(first))
            fixture.scoped_peak(grant)
            before, = fixture.server.places.values()
            mutation = wire.TokenWaitRequest("token-once", grant.reservation)
            fixture.fault.stage = "after_state_write"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(mutation)
            fixture.restart()
            self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
            self.assertFalse(fixture.server.reservations)
            place, = fixture.server.places.values()
            self.assertEqual((place.owner, place.since, place.priority_since),
                             (before.owner, before.since, before.priority_since))
            self.assertIsInstance(place.token_left_at, float)
            self.assertEqual([row["reservation_id"] for row in fixture.history()], [grant.reservation])
            fixture.clock.advance(1)
            fixture.free(0)
            fixture.hold_starts()
            second = replace(first, episode="after-token", token_back_at=fixture.clock.now())
            _ = fixture.server.handle_request(second)
            _ = fixture.server.handle_request(second)
            row = fixture.server.waiters[second.episode].row
            self.assertEqual(row["since"], first.since)
            self.assertEqual(row.get("token_left_at"), place.token_left_at)
            fixture.restart()
            _ = fixture.server.handle_request(second)
            with (fixture.state_dir / "queue_times.jsonl").open("rb") as stream:
                times = list(gate.queue_times_backward(stream))
            gaps = [row for row in times if row.get("kind") == "token_gap"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["episode"], second.episode)
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            _ = fixture.server.run_pass()
            later = fixture.server.ledger[second.episode].reply
            assert isinstance(later, wire.Admitted)
            self.assertNotEqual(later.reservation, grant.reservation)

    def test_new_failed_build_peak_expires_matching_waiters_without_losing_their_place(self) -> None:
        with AdmissionFixture() as fixture:
            argv = ("cargo", "build", "--release", "--bin", "fixture")
            repository = records.RepositoryIdentity("https://example.org/fixture", "fixture")
            shape = build_shape(list(argv))
            profile = records.StepProfile("fixture", "build", str(fixture.root),
                records.ExpectedPeak(12 * GIB, "shape", 9,
                    records.ShapeRankedPeak(9, 10, records.NoFloor(), records.ReleaseHeadroom(records.read_memory_config(report=False).release_headroom), records.UnderBudget())),
                repository, "full", fixture.clock.now())
            owner_request = fixture.request(need=12 * GIB, step="build")
            assert isinstance(owner_request.profile, records.StepProfile)
            profile = replace(profile, build_history_generation=owner_request.profile.build_history_generation)
            owner_request = replace(owner_request, argv=argv, profile=profile,
                sidecar=str(fixture.scope(owner_request.owner)))
            grant = only_admitted(fixture.server.handle_request(owner_request))
            peak = int(15.76 * GIB)
            fixture.scoped_peak(grant, peak)
            fixture.allow_next_start()
            blocker = fixture.request(need=GIB // 8, step="check",
                owner=fixture.proc.spawn(used=GIB // 8))
            blocker_grant = only_admitted(fixture.server.handle_request(blocker))
            fixture.free(0)
            fixture.hold_starts()
            waiter = replace(fixture.request(need=12 * GIB, step="build",
                since=fixture.clock.now() - 900), argv=argv, profile=profile)
            _ = fixture.server.handle_request(waiter)
            before = dict(fixture.server.waiters[waiter.episode].row)
            fixture.proc.kill(owner_request.owner)
            scope = Path(Path(owner_request.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            _ = fixture.server.run_pass()
            row, = fixture.history()
            self.assertEqual((row["outcome"], row["need"], row["shape"], row["anon_peak_bytes"]),
                ("owner_gone", 12 * GIB, shape.key, peak))
            waiting = fixture.server.waiters[waiter.episode]
            held = waiting.last_reply
            assert isinstance(held, wire.Wait)
            self.assertIsInstance((held.reason.cause if isinstance(held.reason, records.HeadAdmissionStatus) else held.reason), records.EstimateExpired)
            for key in ("since", "priority_since", "need", "need_basis"):
                self.assertEqual(waiting.row.get(key), before.get(key))
            self.assertNotIn(owner_request.episode, fixture.server.reservations)
            self.assertNotIn(waiter.episode, fixture.server.reservations)
            fixture.clock.advance(1)
            fixture.free(60 * GIB)
            fixture.free(18 * GIB)
            with test_hang_guard.LauncherHangGuard(self.id()).protect(), fixture.serving(), \
                    mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(step_estimator, "git_identity", return_value=(repository, str(fixture.root))):
                refreshed = client.step_request(list(argv), str(fixture.root), waiter.owner,
                    waiter.since, waiter.sidecar, waiter.episode)
            self.assertEqual((refreshed.owner, refreshed.episode, refreshed.since),
                (waiter.owner, waiter.episode, waiter.since))
            self.assertIsInstance(refreshed.profile, records.StepProfile)
            assert isinstance(refreshed.profile, records.StepProfile)
            self.assertEqual(refreshed.profile.build_history_generation,
                records.BuildHistoryGeneration(fixture.server.build_history_generation))
            self.assertGreater(refreshed.profile.peak.bytes, peak)
            self.assertEqual(fixture.server.reservations[blocker.episode].reservation_id,
                blocker_grant.reservation)
            next_attempt = replace(waiter, profile=refreshed.profile)
            reply, = fixture.server.handle_request(next_attempt)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertNotIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
            self.assertGreater(reply.snapshot.peak.bytes, 15.76 * GIB)
            self.assertEqual(reply.snapshot.decision.state, "hold")
            self.assertEqual(reply.snapshot.peak, next_attempt.profile.peak)
            self.assertNotIn(waiter.episode, fixture.server.reservations)
            fixture.free(60 * GIB)
            if next_attempt.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            admitted = only_admitted(fixture.server.handle_request(next_attempt))
            self.assertEqual(admitted.need, next_attempt.profile.peak.bytes)
            self.assertEqual(fixture.server.reservations[waiter.episode].need, next_attempt.profile.peak.bytes)

    def build_history_request(self, fixture: AdmissionFixture, *, since: float = 900) -> wire.AdmitRequest:
        request = fixture.request(need=12 * GIB, step="build", since=since)
        assert isinstance(request.profile, records.StepProfile)
        profile = replace(request.profile, worktree=str(fixture.root),
            repository=records.RepositoryIdentity("https://example.org/fixture", "fixture"),
            peak=records.ExpectedPeak(12 * GIB, "shape", 9,
                records.ShapeRankedPeak(9, 10, records.NoFloor(), records.ReleaseHeadroom(records.read_memory_config(report=False).release_headroom), records.UnderBudget())))
        return replace(request, argv=("cargo", "build", "--release", "--bin", "fixture"), profile=profile)

    def assert_unregistered_build_is_expired(self, *, restart: bool = False,
            corrected_clock: bool = False, longer_head: bool = False) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = 1000
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.clock.epoch = 1050
            stale = self.build_history_request(fixture)
            self.assertNotIn(stale.episode, fixture.server.waiters)
            self.assertFalse(any(place.owner == stale.owner for place in fixture.server.places.values()))
            assert isinstance(stale.profile, records.StepProfile)
            generation = stale.profile.build_history_generation
            assert isinstance(generation, records.BuildHistoryGeneration)
            fixture.clock.epoch = 1040 if corrected_clock else 1100
            _ = fixture.server.handle_request(wire.ReleaseRequest("published-before-connect", grant.reservation, 137))
            fixture.clock.advance(0.001)
            fixture.server.persist()
            if restart:
                fixture.restart()
            if longer_head:
                old_head = records.read_memory_config().policy.timing.estimate_refresh_seconds
                fixture.clock.epoch = stale.profile.estimated_at + old_head + 2
                fixture.configure(estimate_refresh_seconds=old_head * 2)
                self.assertLess(fixture.clock.now() - stale.profile.estimated_at,
                    records.read_memory_config().policy.timing.estimate_refresh_seconds)
            if corrected_clock:
                self.assertLess(fixture.clock.now(), stale.profile.estimated_at)
            history, = fixture.history()
            self.assertEqual(history["anon_peak_bytes"], 16 * GIB)
            self.assertGreater(service.read_state(fixture.state_dir).build_history_generation, generation.value)
            self.assertFalse(fixture.server.waiters)
            fixture.free(18 * GIB)
            reply, = fixture.server.handle_request(stale)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
            self.assertNotIn(stale.episode, fixture.server.reservations)
            self.assertEqual(fixture.server.waiters[stale.episode].row["since"], stale.since)
            self.assertLess(stale.profile.peak.bytes + reply.snapshot.decision.reserve, 18 * GIB)

    def test_unregistered_build_estimate_expires_after_publication_and_persistence(self) -> None:
        self.assert_unregistered_build_is_expired()

    def test_unregistered_build_estimate_expires_after_publication_and_restart(self) -> None:
        self.assert_unregistered_build_is_expired(restart=True)

    def test_clock_correction_cannot_revive_a_build_estimate_after_publication(self) -> None:
        self.assert_unregistered_build_is_expired(corrected_clock=True)

    def test_larger_head_seconds_cannot_revive_a_build_estimate_after_publication(self) -> None:
        self.assert_unregistered_build_is_expired(longer_head=True)

    def test_build_admit_and_launch_require_a_current_history_generation(self) -> None:
        for launch in (False, True):
            for missing in (False, True):
                with self.subTest(launch=launch, missing=missing), AdmissionFixture() as fixture:
                    stale = self.build_history_request(fixture)
                    assert isinstance(stale.profile, records.StepProfile)
                    if missing:
                        profile = replace(stale.profile, build_history_generation=records.NoBuildHistoryGeneration())
                        stale = replace(stale, profile=profile)
                    else:
                        owner = self.build_history_request(fixture)
                        owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
                        grant = only_admitted(fixture.server.handle_request(owner))
                        fixture.scoped_peak(grant, 16 * GIB)
                        _ = fixture.server.handle_request(wire.ReleaseRequest("advance-before-launch", grant.reservation, 137))
                    fixture.free(18 * GIB)
                    request: wire.AdmitRequest | wire.LaunchRequest = stale
                    if launch:
                        request = wire.LaunchRequest(stale.episode, stale.owner, stale.since, stale.identity, stale.profile)
                    reply, = fixture.server.handle_request(request)
                    self.assertIsInstance(reply, wire.Wait)
                    assert isinstance(reply, wire.Wait)
                    self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
                    self.assertNotIn(stale.episode, fixture.server.reservations)
                    self.assertNotIn(stale.episode, fixture.server.ledger)

    def test_current_generation_refresh_preserves_owner_episode_and_queue_place(self) -> None:
        with AdmissionFixture() as fixture:
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            stale = self.build_history_request(fixture)
            _ = fixture.server.handle_request(wire.ReleaseRequest("peak-before-refresh", grant.reservation, 137))
            fixture.free(18 * GIB)
            reply, = fixture.server.handle_request(stale)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
            before = dict(fixture.server.waiters[stale.episode].row)
            with test_hang_guard.LauncherHangGuard(self.id()).protect(), fixture.serving(), \
                    mock.patch.object(client, "CLOCK", fixture.clock), \
                    mock.patch.object(step_estimator, "git_identity", return_value=(stale.profile.repository, str(fixture.root))):
                refreshed = client.step_request(list(stale.argv), stale.cwd, stale.owner,
                    stale.since, stale.sidecar, stale.episode)
            self.assertEqual((refreshed.owner, refreshed.episode, refreshed.since),
                (stale.owner, stale.episode, stale.since))
            self.assertGreater(refreshed.profile.peak.bytes, stale.profile.peak.bytes)
            fixture.free(60 * GIB)
            if refreshed.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            granted = only_admitted(fixture.server.handle_request(refreshed))
            self.assertEqual(granted.need, refreshed.profile.peak.bytes)
            record = fixture.server.reservations[stale.episode]
            self.assertEqual(record.owner, stale.owner)
            for key in ("since", "priority_since"):
                self.assertEqual(record.queue_waiter.get(key), before.get(key))

    def test_retry_of_an_existing_history_row_advances_the_generation(self) -> None:
        with AdmissionFixture() as fixture:
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            release = wire.ReleaseRequest("retry-published-row", grant.reservation, 137)
            with mock.patch.object(service.StateIntents, "published", side_effect=OSError("generation save failed")):
                with self.assertRaisesRegex(OSError, "generation save failed"):
                    _ = fixture.server.handle_request(release)
            history, = fixture.history()
            self.assertEqual(history["reservation_id"], grant.reservation)
            stale = self.build_history_request(fixture)
            before = service.read_state(fixture.state_dir).build_history_generation
            fixture.restart()
            self.assertGreater(service.read_state(fixture.state_dir).build_history_generation, before)
            self.assertEqual(len(fixture.history()), 1)
            fixture.free(18 * GIB)
            reply, = fixture.server.handle_request(stale)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
            self.assertNotIn(stale.episode, fixture.server.reservations)

    def test_delayed_build_refresh_cannot_clear_a_later_failed_peak(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = 1000
            owner = self.build_history_request(fixture)
            owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
            profile = owner.profile
            grant = only_admitted(fixture.server.handle_request(owner))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.free(0)
            fixture.hold_starts()
            age = records.read_memory_config().policy.timing.estimate_refresh_seconds + 1
            waiter = replace(self.build_history_request(fixture),
                profile=replace(profile, estimated_at=1000 - age))
            first, = fixture.server.handle_request(waiter)
            assert isinstance(first, wire.Wait)
            self.assertIsInstance((first.reason.cause if isinstance(first.reason, records.HeadAdmissionStatus) else first.reason), records.EstimateExpired)
            before = dict(fixture.server.waiters[waiter.episode].row)
            delayed = replace(waiter, profile=replace(profile, estimated_at=1005))
            fixture.clock.epoch = 1010
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                "finished-after-refresh-read", grant.reservation, 137)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual(row["anon_peak_bytes"], 16 * GIB)
            fixture.free(18 * GIB)
            fixture.clock.epoch = 1011
            reply, = fixture.server.handle_request(delayed)
            self.assertIsInstance(reply, wire.Wait)
            assert isinstance(reply, wire.Wait)
            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
            self.assertNotIn(waiter.episode, fixture.server.reservations)
            self.assertEqual(fixture.server.waiters[waiter.episode].row["since"], before["since"])
            self.assertLess(12 * GIB + reply.snapshot.decision.reserve, 18 * GIB)

    def test_pending_failed_peak_expires_estimates_before_and_after_restart_publication(self) -> None:
        for append_fails in (False, True):
            with self.subTest(append_fails=append_fails), AdmissionFixture() as fixture:
                fixture.clock.epoch = 1000
                owner = self.build_history_request(fixture)
                owner = replace(owner, sidecar=str(fixture.scope(owner.owner)))
                grant = only_admitted(fixture.server.handle_request(owner))
                fixture.scoped_peak(grant, 16 * GIB)
                fixture.clock.epoch = 1010
                fixture.fault.stage = "after_finalization_intent"
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("pending-failed-peak", grant.reservation, 137))
                saved = service.read_state(fixture.state_dir).reservations[owner.episode]
                intent = saved.finalization
                assert not isinstance(intent, service.NoFinalizationIntent)
                self.assertEqual(datetime.fromisoformat(intent["ended_at"]).timestamp(), 1010)
                self.assertEqual(fixture.history(), [])

                fixture.clock.epoch = 1050
                stale = self.build_history_request(fixture)
                assert isinstance(stale.profile, records.StepProfile)
                self.assertEqual(stale.profile.build_history_generation, fixture.build_history_generation())
                fixture.free(18 * GIB)
                with self.subTest(boundary="pending finalization"), \
                        mock.patch.object(store, "append_locked", side_effect=OSError("history unavailable")):
                    reply, = fixture.server.handle_request(stale)
                    self.assertIsInstance(reply, wire.Wait)
                    assert isinstance(reply, wire.Wait)
                    self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
                    self.assertNotIn(stale.episode, fixture.server.reservations,
                        "a pending failed peak must prevent admission with the stale need")
                    self.assertEqual(fixture.server.waiters[stale.episode].row["since"], stale.since)
                self.assertEqual(fixture.history(), [])

                if append_fails:
                    fixture.clock.epoch = 1075
                    self.assertEqual(fixture.proc.owner_start(owner.owner.pid),
                        service.LiveOwnerIdentity(owner.owner.start_time))
                    with mock.patch.object(store, "append_locked",
                            side_effect=OSError("history append failed")) as append:
                        fixture.restart()
                        with self.subTest(boundary="failed append after restart"):
                            reply, = fixture.server.handle_request(stale)
                            self.assertIsInstance(reply, wire.Wait)
                            assert isinstance(reply, wire.Wait)
                            self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
                            self.assertNotIn(stale.episode, fixture.server.reservations,
                                "a failed history append must prevent stale admission")
                            retained = service.read_state(fixture.state_dir).reservations[owner.episode]
                            self.assertEqual(retained.finalization, intent)
                            self.assertEqual(fixture.history(), [])
                            self.assertGreaterEqual(append.call_count, 2,
                                "restart and admission must retry the pending append")

                fixture.clock.epoch = 1100
                if append_fails:
                    _ = fixture.server.run_pass()
                    self.assertEqual(fixture.proc.owner_start(owner.owner.pid),
                        service.LiveOwnerIdentity(owner.owner.start_time))
                else:
                    fixture.restart()
                history, = fixture.history()
                self.assertEqual((history["reservation_id"], history["anon_peak_bytes"]),
                    (grant.reservation, 16 * GIB))
                self.assertNotIn(owner.episode, fixture.server.reservations)
                with self.subTest(boundary="history published after restart"):
                    reply, = fixture.server.handle_request(stale)
                    self.assertIsInstance(reply, wire.Wait)
                    assert isinstance(reply, wire.Wait)
                    self.assertIsInstance((reply.reason.cause if isinstance(reply.reason, records.HeadAdmissionStatus) else reply.reason), records.EstimateExpired)
                    self.assertNotIn(stale.episode, fixture.server.reservations,
                        "publication must invalidate estimates made after the intent and before the append")
                    self.assertEqual(fixture.server.waiters[stale.episode].row["since"], stale.since)
                    self.assertLess(stale.profile.peak.bytes + reply.snapshot.decision.reserve, 18 * GIB)

    def test_rollback_export_finalizes_an_ended_reservation_before_removing_state(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("unfinished-export", grant.reservation, 137))
            fixture.proc.kill(request.owner)
            scope = Path(Path(request.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            fixture.server.close()
            self.assertEqual(fixture.history(), [])
            finalize = gate.finalize_reservation
            def record_before_retirement(path: Path, row: records.Reservation, outcome: step_estimator.FinalOutcome,
                    now: datetime, status: int | step_estimator.OutcomeNotRecorded = gate.OUTCOME_NOT_RECORDED, *,
                    intents: gate.FinalizationIntents, end_kind: gate.StepEnd | gate.TokenReturn
                    ) -> gate.HistoryRecorded | gate.NoHistoryRecorded:
                self.assertTrue((fixture.state_dir / "state.json").exists())
                return finalize(path, row, outcome, now, status, intents=intents, end_kind=end_kind)
            with mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(gate, "finalize_reservation", side_effect=record_before_retirement) as finished:
                service.export_priority(fixture.state_dir)
            self.assertEqual(finished.call_count, 1)
            row, = fixture.history()
            self.assertEqual((row["reservation_id"], row["anon_peak_bytes"], row["status"]),
                (grant.reservation, 16 * GIB, 137))
            self.assertFalse((fixture.state_dir / "state.json").exists())

    def test_rollback_export_keeps_state_when_pending_finalization_fails(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant, 16 * GIB)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("failed-export", grant.reservation, 137))
            fixture.proc.kill(request.owner)
            scope = Path(Path(request.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            fixture.server.close()
            with mock.patch.object(service, "SystemProc", return_value=fixture.proc), \
                 mock.patch.object(gate, "finalize_reservation", side_effect=OSError("history append failed")):
                with self.assertRaisesRegex(ValueError, grant.reservation):
                    service.export_priority(fixture.state_dir)
            saved = service.read_state(fixture.state_dir)
            self.assertIn(request.episode, saved.reservations)
            self.assertIsInstance(saved.reservations[request.episode].finalization, dict)
            self.assertEqual(fixture.history(), [])


    def test_service_drain_ignores_unknown_state_fields(self) -> None:
        from scripts.build_queue.admission_test_support import TestProcesses
        state = service.ServiceState({}, {}, {}, records.ReadyToStart())
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            proc = TestProcesses(directory / "proc")
            record = wire.parse_record(wire.encode_record(state), dict[str, object])
            record["unknown"] = True
            _ = (directory / "state.json").write_text(wire.encode_record(record))
            self.assertEqual(service.drain_check(
                directory, service=True, include_gate_callers=False, proc=proc), 0)
            self.assertEqual(service.read_state(directory), state)

    def service_drain_with_process(self, script: str, *, default_folder: bool) -> tuple[int, str]:
        from scripts.build_queue.admission_test_support import TestProcesses
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "admission"
            directory.mkdir()
            state = service.ServiceState({}, {}, {}, records.ReadyToStart())
            _ = (directory / "state.json").write_text(wire.encode_record(state))
            proc = TestProcesses(root / "proc")
            try:
                owner = proc.spawn()
                _ = (proc.path / str(owner.pid) / "cmdline").write_bytes(
                    f"python3\0/private/{script}\0".encode())
                argv = ["admission_service", "drain-check", "--service", "--state-dir", str(directory)]
                output = io.StringIO()
                with mock.patch.object(store, "root", return_value=root if default_folder else root / "other"), \
                     mock.patch.object(service, "SystemProc", return_value=proc), \
                     mock.patch.object(sys, "argv", argv), mock.patch("sys.stdout", output):
                    status = service.main()
                return status, output.getvalue()
            finally:
                proc.close()

    def test_serve_without_state_dir_uses_the_buildlog_admission_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server = mock.MagicMock(replace_in_place=False)
            with mock.patch.object(store, "root", return_value=root), \
                 mock.patch.object(service, "AdmissionServer", return_value=server) as made, \
                 mock.patch.object(memory_trace, "read_policy", return_value=TRACE_POLICY), \
                 mock.patch.object(sys, "argv", ["admission_service", "serve"]):
                self.assertEqual(service.main(), 0)
            self.assertEqual(made.call_args.args[0], root / "admission")

    def test_service_drain_other_folder_ignores_verify_process(self) -> None:
        status, output = self.service_drain_with_process("verify.sh", default_folder=False)
        self.assertEqual(status, 0)
        self.assertEqual(output, "drained\n")

    def test_service_drain_other_folder_counts_admission_client(self) -> None:
        status, output = self.service_drain_with_process("admission_client.py", default_folder=False)
        self.assertEqual(status, 1)
        self.assertIn("clients: 1:", output)
        self.assertIn("waiters: 0\nreservations: 0\n", output)

    def test_service_drain_other_folder_counts_queue_place(self) -> None:
        status, output = self.service_drain_with_process("queue_place.py", default_folder=False)
        self.assertEqual(status, 1)
        self.assertIn("clients: 1:", output)

    def test_service_drain_default_folder_counts_verify_process(self) -> None:
        status, output = self.service_drain_with_process("verify.sh", default_folder=True)
        self.assertEqual(status, 1)
        self.assertIn("clients: 1:", output)
        self.assertIn("waiters: 0\nreservations: 0\n", output)

    def test_drain_barriers_count_live_waiters_reservations_and_client_processes(self) -> None:
        import json
        for rollback in (False, True):
            for kind in ("waiter", "reservation", "client", "empty"):
                with self.subTest(rollback=rollback, kind=kind), AdmissionFixture() as fixture:
                    request = fixture.request()
                    if kind == "waiter":
                        if rollback:
                            fixture.free(0)
                            fixture.hold_starts()
                            _ = fixture.server.handle_request(request)
                        else:
                            _ = (fixture.state_dir / "old.waiting").write_text(json.dumps({
                                "pid": request.owner.pid, "start_time": request.owner.start_time,
                                "need": GIB, "since": fixture.clock.now()}))
                    elif kind == "reservation":
                        if rollback:
                            _ = fixture.server.handle_request(request)
                        else:
                            _ = (fixture.state_dir / "old.reservation").write_text(json.dumps({
                                "pid": request.owner.pid, "start_time": request.owner.start_time,
                                "need": GIB, "repo": "fixture", "step": "check", "worktree": "tree",
                                "admitted_at": store.utc_iso(fixture.clock.now()),
                                "sidecar": "", "test_threads": None, "call_id": None}))
                    elif kind == "client":
                        script = "admission_client.py" if rollback else "verify.sh"
                        _ = (fixture.proc.path / str(request.owner.pid) / "cmdline").write_bytes(
                            f"python3\0/private/{script}\0".encode())
                    before = fixture.state_text()
                    output = io.StringIO()
                    with mock.patch("sys.stdout", output):
                        status = service.drain_check(fixture.state_dir, service=rollback,
                                                     include_gate_callers=True, proc=fixture.proc)
                    self.assertEqual(status, 0 if kind == "empty" else 1)
                    if kind == "empty":
                        self.assertEqual(output.getvalue(), "drained\n")
                    else:
                        label = {"waiter": "waiters", "reservation": "reservations", "client": "clients"}[kind]
                        self.assertIn(f"{label}: 1", output.getvalue())
                    self.assertEqual(fixture.state_text(), before)


    def phase_grant(self, fixture: AdmissionFixture
                    ) -> tuple[wire.AdmitRequest, wire.Admitted, Path, records.TestPhaseNeed]:
        need = records.TestPhaseNeed(2 * GIB, 10, 8, 8, "same_width", 1.2)
        request = fixture.request(need=11 * GIB)
        request = replace(request, sidecar=str(fixture.scope(request.owner)))
        grant = only_admitted(fixture.server.handle_request(request))
        record = replace(fixture.server.reservations[request.episode], test_phase_need=need)
        fixture.server.reservations[request.episode] = record
        fixture.server.persist()
        fixture.scoped_peak(grant, 11 * GIB)
        phase = gate.test_phase_path(service.reservation_row(record))
        assert isinstance(phase, Path)
        fields: dict[str, object] = {"test_phase_peak_bytes": 3 * GIB,
            "test_phase_started_at": fixture.clock.now() - 30, "test_threads": 8,
            "call_id": "call-1", "test_scope": "full", "repo_identity": "fixture",
            "package": "", "filters": []}
        _ = phase.write_text(wire.encode_record(fields))
        return request, grant, phase, need


    def test_saved_test_phase_margin_and_evidence_survive_finalization_crashes(self) -> None:
        for stage in ("after_finalization_intent", "after_history_append"):
            with self.subTest(stage=stage), AdmissionFixture() as fixture:
                request, grant, phase, need = self.phase_grant(fixture)
                _ = fixture.server.run_pass()
                evidence: test_phase.TestPhaseChargeUse = {"bytes": 2 * GIB,
                    "selected_at": fixture.clock.now(), "reservation_id": grant.reservation,
                    "need": gate.test_phase_need_record(need)}
                fixture.server.reservations[request.episode] = replace(
                    fixture.server.reservations[request.episode], charge_use=evidence)
                fixture.server.persist()
                write_memory_config(fixture.root / "build_admission.conf", estimate_refresh_seconds="900")
                inputs = fixture.server.last_inputs
                assert isinstance(inputs, records.QueueInputs)
                self.assertEqual(len(inputs.running), 2)
                memory, = (step for step in inputs.running if gate.running_has_reservation(step))
                self.assertEqual(memory.promise, 11 * GIB)
                fixture.fault.stage = stage
                with self.assertRaises(InjectedCrash):
                    _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 137))
                phase.unlink()
                fixture.restart()
                _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
                row, = fixture.history()
                self.assertEqual((row["outcome"], row["test_phase_peak_bytes"]), ("killed", 3 * GIB))
                self.assertEqual(gate.read_test_phase_need(row["test_phase_need"]), need)
                self.assertEqual(row["test_phase_charge_use"], evidence)
                self.assertFalse(fixture.server.reservations)

    def test_ramp_owner_exit_records_the_peak_without_inventing_charge_evidence(self) -> None:
        with AdmissionFixture() as fixture:
            request, grant, phase, need = self.phase_grant(fixture)
            _ = fixture.server.run_pass()
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual(len(inputs.running), 2)
            memory, = (step for step in inputs.running if gate.running_has_reservation(step))
            self.assertEqual(memory.promise, 11 * GIB)
            self.assertIsInstance(fixture.server.reservations[request.episode].charge_use, test_phase.NoTestPhaseChargeUse)
            fixture.proc.kill(request.owner)
            record = fixture.server.reservations[request.episode]
            scope = Path(Path(record.sidecar).read_text())
            _ = (scope / "cgroup.procs").write_text("")
            _ = (scope / "cgroup.events").write_text("populated 0\n")
            fixture.clock.advance(1)
            fixture.server.run_once(0)
            row, = fixture.history()
            self.assertEqual((row["outcome"], row["test_phase_peak_bytes"]), ("owner_gone", 3 * GIB))
            self.assertEqual(gate.read_test_phase_need(row["test_phase_need"]), need)
            self.assertNotIn("test_phase_charge_use", row)
            self.assertTrue(phase.exists())
            self.assertFalse(fixture.server.reservations)
            self.assertFalse(Path(grant.max_path).exists())

    def test_old_sampler_history_keeps_the_need_without_test_phase_window_fields(self) -> None:
        with AdmissionFixture() as fixture:
            request, grant, phase, need = self.phase_grant(fixture)
            phase.unlink()
            _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
            row, = fixture.history()
            self.assertEqual(gate.read_test_phase_need(row["test_phase_need"]), need)
            for field in ("test_phase_peak_bytes", "test_phase_started_at", "test_phase_charge_use"):
                self.assertNotIn(field, row)
            self.assertNotIn(request.episode, fixture.server.reservations)


    def test_priority_orders_new_workers_and_drops_rank_from_persisted_places(self) -> None:
        from scripts.message.unit_labels import UnitLabels
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            older = fixture.request(need=12 * GIB, since=fixture.clock.now() - 600)
            chosen = fixture.request(need=12 * GIB, since=fixture.clock.now() - 60)
            newest = fixture.request(need=12 * GIB, since=fixture.clock.now() - 30)
            labels = {older.identity.session_id: UnitLabels("older", unit="other"),
                chosen.identity.session_id: UnitLabels("chosen", unit="chosen"),
                newest.identity.session_id: UnitLabels("copy", unit="chosen")}
            with mock.patch.object(gate, "priority_labels", return_value=labels):
                for request in (older, chosen):
                    _ = fixture.server.handle_request(request)
                self.assertEqual(fixture.completed_queue_change(wire.PrioritySetRequest("priority", "chosen", "user work", "director")), [wire.Ok()])
                _ = fixture.server.handle_request(newest)
                view = fixture.server.handle_request(wire.ViewRequest())[0]
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                    [chosen.owner.pid, newest.owner.pid, older.owner.pid])
                state = service.read_state(fixture.state_dir)
                before = {key: (place.since, place.priority_since)
                    for key, place in state.places.items()}
                for waiting in fixture.server.waiters.values():
                    self.assertNotIn("priority_rank", records.stored_waiter(waiting.row))
                self.assertEqual(fixture.server.handle_request(wire.PriorityClearRequest("clear", "director", why="clear words")), [wire.Ok()])
                view = fixture.server.handle_request(wire.ViewRequest())[0]
                assert isinstance(view, wire.ViewReply)
                self.assertEqual([step.pid for step in view.reading.view.waiting],
                    [older.owner.pid, chosen.owner.pid, newest.owner.pid])
                self.assertEqual({key: (place.since, place.priority_since)
                    for key, place in fixture.server.places.items()}, before)


    def test_estimate_expiry_keeps_recovery_place_and_basis_until_refresh(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            policy = fixture.server.last_policy
            assert isinstance(policy, records.AdmissionPolicy)
            request = fixture.request(age=policy.timing.estimate_refresh_seconds - 1)
            _ = fixture.server.handle_request(request)
            place = dict(fixture.server.waiters[request.episode].row)
            fixture.server.spacing = records.NextStartAt(fixture.clock.now() + 30)
            recovery = fixture.server.spacing
            fixture.clock.advance(2)
            _ = fixture.server.run_pass()
            held = fixture.server.waiters[request.episode].last_reply
            assert isinstance(held, wire.Wait)
            self.assertIsInstance((held.reason.cause if isinstance(held.reason, records.HeadAdmissionStatus) else held.reason), records.EstimateExpired)
            saved = fixture.server.waiters[request.episode].row
            for key in ("since", "priority_since", "need", "estimated_at", "need_source", "need_basis"):
                self.assertEqual(saved.get(key), place.get(key))
            self.assertEqual(fixture.server.spacing, recovery)
            self.assertFalse(fixture.server.reservations)
            refreshed = replace(request, profile=replace(request.profile,
                peak=records.ExpectedPeak(12 * GIB, "measured", 9), estimated_at=fixture.clock.now()))
            _ = fixture.server.handle_request(refreshed)
            saved = fixture.server.waiters[request.episode].row
            self.assertEqual((saved["since"], saved["need"], saved.get("need_runs")), (request.since, 12 * GIB, 9))
            self.assertNotIsInstance(fixture.server.waiters[request.episode].last_reply, service.NoWaitReply)

    def test_policy_changes_reclassify_new_passes_without_changing_an_earlier_reply(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(need=12 * GIB)
            grant = only_admitted(fixture.server.handle_request(request))
            record = fixture.server.reservations[request.episode]
            fixture.hold_starts()
            younger = fixture.request(need=10 * GIB)
            _ = fixture.server.handle_request(younger)
            old, = fixture.server.handle_request(wire.ExplainRequest())
            assert isinstance(old, wire.ExplainReply)
            self.assertIsInstance(old.explanation.host, records.HostAvailable)
            assert isinstance(old.explanation.host, records.HostAvailable)
            self.assertIsInstance(old.explanation.host.pressure, records.MemoryPressure)
            old_release = gate.smallest_head_release(old.explanation)
            fixture.configure(full_pressure_limit=100)
            _ = fixture.server.run_pass()
            current, = fixture.server.handle_request(wire.ExplainRequest())
            assert isinstance(current, wire.ExplainReply)
            assert isinstance(current.explanation.host, records.HostAvailable)
            self.assertIsInstance(current.explanation.host.pressure, records.NoPressureHold)
            self.assertEqual(old.explanation.policy.pressure.full_avg10_limit, 5)
            self.assertEqual(gate.smallest_head_release(old.explanation), old_release)
            self.assertEqual(fixture.server.reservations[request.episode].reservation_id, grant.reservation)
            self.assertEqual(record.need, 12 * GIB)


    def test_filtered_estimate_is_reused_and_release_keeps_repository_and_selection(self) -> None:
        with AdmissionFixture() as fixture:
            argv = ["cargo", "nextest", "run", "--test-threads", "16", "-E", "package(hana) & test(native_rate)"]
            repository = records.RepositoryIdentity("https://example.org/hana", "hana")
            peak = records.ExpectedPeak(2 * GIB, "measured", 5)
            policy = records.read_memory_config().policy
            from datetime import UTC, datetime
            with mock.patch.object(step_estimator, "git_identity", return_value=(repository, "clone")) as identity, \
                 mock.patch.object(step_estimator, "expected_peak", return_value=peak) as history:
                first = step_estimator.step_estimate(argv, str(fixture.root), records.InferFromArgv(),
                    datetime.fromtimestamp(fixture.clock.now(), UTC), fixture.state_dir.parent, policy, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
                second = step_estimator.step_estimate(argv, str(fixture.root), first.profile,
                    datetime.fromtimestamp(fixture.clock.now() + 1, UTC), fixture.state_dir.parent, policy, first_peak_bytes=admission_configuration_fixture().program_first_peaks["cargo"])
            self.assertEqual((identity.call_count, history.call_count), (1, 1))
            self.assertEqual(history.call_args.kwargs["package"], "hana")
            self.assertEqual(history.call_args.kwargs["filters"], ("native_rate",))
            self.assertEqual(second.profile.peak, first.profile.peak)
            request = fixture.request(need=2 * GIB, width=records.RequestedTestWidth(16))
            request = replace(request, argv=tuple(argv), profile=second.profile,
                sidecar=str(fixture.scope(request.owner)))
            fixture.free(0)
            fixture.hold_starts()
            held = fixture.server.handle_request(request)[0]
            self.assertIsInstance(held, wire.Wait)
            fixture.free(40 * GIB)
            _ = fixture.server.run_pass()
            grant = fixture.server.ledger[request.episode].reply
            assert isinstance(grant, wire.Admitted)
            fixture.scoped_peak(grant, GIB)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("filtered", grant.reservation, 0)), [wire.Ok()])
            row, = fixture.history()
            self.assertEqual((row["repo_identity"], row["test_scope"]), (repository.key, "filtered"))
            self.assertEqual((row["package"], row["filters"], row["test_threads"]), ("hana", ["native_rate"], 16))


    def test_one_process_snapshot_covers_fresh_mature_and_reused_owners(self) -> None:
        with AdmissionFixture() as fixture:
            fresh = fixture.request(need=4 * GIB, owner=fixture.proc.spawn(GIB))
            fresh = replace(fresh, sidecar=str(fixture.scope(fresh.owner)))
            _ = only_admitted(fixture.server.handle_request(fresh))
            fixture.clock.advance(120)
            mature = fresh
            fresh = fixture.request(need=4 * GIB, owner=fixture.proc.spawn(GIB // 2))
            if fresh.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            fresh = replace(fresh, sidecar=str(fixture.scope(fresh.owner)))
            _ = only_admitted(fixture.server.handle_request(fresh))
            second_scope = Path(fresh.sidecar).read_text().strip()
            _ = (Path(second_scope) / "memory.current").write_text(str(GIB // 2))
            _ = (Path(second_scope) / "memory.stat").write_text(f"anon {GIB // 2}\nshmem 0\ninactive_file 0\n")
            reused = fixture.request(need=GIB)
            if reused.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            old = only_admitted(fixture.server.handle_request(reused))
            fixture.proc.fixture_owners[reused.owner.pid] = "reused-start"
            stat_path = fixture.proc.path / str(reused.owner.pid) / "stat"
            _ = stat_path.write_text(stat_path.read_text().replace(reused.owner.start_time, "reused-start"))
            with mock.patch.object(fixture.proc, "processes", wraps=fixture.proc.processes) as tables:
                result = fixture.server.run_pass()
            tables.assert_called_once_with()
            assert isinstance(result, gate.PassResult)
            self.assertEqual((result.policy.totals.promised, result.policy.totals.measured), (13 * GIB // 2, 3 * GIB // 2))
            self.assertEqual({record.owner.pid for record in fixture.server.reservations.values()},
                {fresh.owner.pid, mature.owner.pid})
            self.assertFalse(Path(old.max_path).exists())


    def test_state_write_interruption_keeps_the_saved_width_for_restart_and_retry(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(width=records.RequestedTestWidth(8))
            grant = only_admitted(fixture.server.handle_request(request))
            before = fixture.state_text()
            mutation = wire.WidthRequest("new-width", grant.reservation, 2)
            replace_file = os.replace
            def stop(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
                if Path(destination) == fixture.state_dir / "state.json":
                    raise OSError("state replace stopped")
                replace_file(source, destination)
            with mock.patch.object(os, "replace", side_effect=stop):
                with self.assertRaisesRegex(OSError, "state replace stopped"):
                    _ = fixture.server.handle_request(mutation)
            self.assertEqual(fixture.state_text(), before)
            fixture.restart()
            self.assertEqual(fixture.server.reservations[request.episode].test_width, records.RequestedTestWidth(8))
            self.assertEqual(fixture.server.handle_request(mutation), [wire.Ok()])
            fixture.restart()
            self.assertEqual(fixture.server.reservations[request.episode].test_width, records.RequestedTestWidth(2))


class VerifyRunSlotTests(unittest.TestCase):

    def configure(self, fixture: AdmissionFixture, maximum: int) -> None:
        fixture.configure(slot_gap_seconds=60)
        config = fixture.root / "build_admission.conf"
        _ = config.write_text(config.read_text() +
            f"\nmax_runs.{conf.host_key_name(fixture.server.host)}.fixture={maximum}\nmax_runs.exempt_unit=natemerge\n")

    def verify_step(self, fixture: AdmissionFixture, *, unit: str = "", episode: str = "",
                    owner: wire.Owner | None = None) -> wire.PlacementRequest:
        from scripts.build_queue.test_placement import local_candidate, placement_request

        request = placement_request(fixture)
        if owner is None:
            owner = wire.Owner(request.owner.pid, request.owner.start_time)
        return replace(request, episode=episode or request.episode,
            owner=placement.SubmittingOwner(fixture.server.host, owner.pid, owner.start_time),
            run=wire.RunIdentity(owner.pid, owner.start_time),
            identity=replace(request.identity, unit=unit, verify=records.VerifyCall("final", "verify final", 1, 2)),
            constraints=replace(request.constraints, candidates=(local_candidate(fixture),)))

    def grant(self, fixture: AdmissionFixture, request: wire.PlacementRequest) -> wire.LocalPlacementGranted:
        fixture.allow_next_start()
        reply, = fixture.connect(request).replies()
        self.assertIsInstance(reply, wire.LocalPlacementGranted)
        assert isinstance(reply, wire.LocalPlacementGranted)
        return reply

    def test_either_named_machine_can_take_the_verify_run(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate

        with PlacementFixture() as fixture:
            self.configure(fixture, 1)
            first = self.verify_step(fixture)
            fixture.release(self.grant(fixture, first).grant)
            fixture.allow_next_start()
            peer = self.verify_step(fixture)
            mac, = placement_request(fixture).constraints.candidates
            _ = publish_candidate(fixture, replace(peer, constraints=replace(peer.constraints, candidates=(mac,))))
            peer = replace(peer, constraints=replace(peer.constraints, candidates=(*peer.constraints.candidates, mac)))
            reply, = fixture.connect(peer).replies()
            self.assertIsInstance(reply, wire.Assignment)
            assert isinstance(reply, wire.Assignment)
            self.assertEqual(reply.assignment.destination, mac.destination)
            self.assertNotIn(peer.episode, fixture.server.reservations)
            self.assertEqual(list(fixture.server.verify_run_slots.values()),
                             [service.VerifyRunSlot("fixture", wire.Owner(first.owner.pid, first.owner.start_time))])

        with PlacementFixture() as fixture:
            self.configure(fixture, 1)
            peer = self.verify_step(fixture)
            mac, = placement_request(fixture).constraints.candidates
            _ = publish_candidate(fixture, replace(peer, constraints=replace(peer.constraints, candidates=(mac,))),
                                  placement.TemporaryExclusion("busy"))
            peer = replace(peer, constraints=replace(peer.constraints, candidates=(mac, *peer.constraints.candidates)))
            local = self.grant(fixture, peer)
            self.assertEqual(local.assignment.destination.target, "natedev")
            slot, = fixture.server.verify_run_slots.values()
            self.assertEqual(slot.owner, wire.Owner(peer.owner.pid, peer.owner.start_time))
            self.assertEqual(slot.repository, "fixture")
            self.assertEqual(slot.use, service.PendingVerifyGrants(frozenset({local.grant.reservation})))

    def test_returned_local_grant_releases_only_an_unused_run_slot(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate

        for ending, started in (("retry", False), ("cancel", False), ("retry", True), ("cancel", True)):
            with self.subTest(ending=ending, started=started), PlacementFixture() as fixture:
                self.configure(fixture, 1)
                request = self.verify_step(fixture)
                owner = wire.Owner(request.owner.pid, request.owner.start_time)
                if started:
                    prior = self.verify_step(fixture, owner=owner, episode="prior-step")
                    fixture.release(self.grant(fixture, prior).grant)
                local, = request.constraints.candidates
                mac, = placement_request(fixture).constraints.candidates
                request = replace(request, constraints=replace(request.constraints, candidates=(local, mac)))
                granted = self.grant(fixture, request)
                fixture.restart()
                if ending == "retry":
                    fixture.clock.advance(1)
                    _ = publish_candidate(fixture, request, placement.TemporaryExclusion("blocked"))
                    path = fixture.root / "grant.json"
                    client.write_assignment(path, granted)
                    with fixture.serving():
                        _ = client.finish_step(path, 10, "local hold changed before execution")
                else:
                    cancelled = wire.CancelPlacement("return-grant", request.episode, request.owner, "token wait ended")
                    self.assertEqual(fixture.connect(cancelled).replies(), [wire.Ok()])
                self.assertNotIn(request.episode, fixture.server.reservations)
                self.assertNotIn(granted.grant.reservation, fixture.server.slots)
                self.assertEqual(bool(fixture.server.verify_run_slots), started)
                if ending == "retry":
                    fixture.clock.advance(1)
                    _ = publish_candidate(fixture, replace(request, constraints=replace(request.constraints,
                        candidates=(mac,))))
                    reply, = fixture.connect(request).replies()
                    assert isinstance(reply, wire.Assignment), reply
                    self.assertEqual(reply.assignment.destination, mac.destination)
                    self.assertEqual(bool(fixture.server.verify_run_slots), started)

    def test_scoped_verify_grant_only_keeps_its_slot_if_the_guard_allows_the_step(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, publish_candidate
        from scripts.buildlog.cgroup_test_support import write_cgroup
        from scripts.lint.test_invoke_scope import scope_command

        scripts = Path(__file__).resolve().parents[1]
        verify = (scripts / "direct/verify.sh").read_text()
        guard = verify.split("STEP_OWNER_PY='\n", 1)[1].split("\n'\n", 1)[0]
        scope_script = scope_command()
        for taken in (False, True):
            with self.subTest(taken=taken), PlacementFixture() as fixture:
                self.configure(fixture, 1)
                scope = fixture.root / "scope"
                write_cgroup(scope)
                sidecar = fixture.root / "peak.cgroup"
                marker = sidecar.with_suffix("")
                lock = fixture.root / "locks/cargo.d"
                lock.mkdir(parents=True)
                _ = (lock / "holder_pid").write_text("caller" if taken else "other")
                lost = fixture.root / "token-lost"
                request = self.verify_step(fixture)
                assert isinstance(request.local, wire.LocalAdmissionContext)
                request = replace(request, local=replace(request.local, sidecar=str(sidecar)))
                grant = self.grant(fixture, request)
                result = subprocess.run(
                    ["/bin/sh", "-c", 'exec 3>&2; exec /bin/sh -c "$@"', "scope-test",
                     scope_script.replace('"/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"', f'"{scope}"'),
                     str(marker), sys.executable, "-c", guard, str(lock), "caller", "cat", str(sidecar)],
                    env={**os.environ, "BUILDLOG_DEFER_SCOPE_SIDECAR": "1",
                         "VERIFY_PROCESS_GROUP_LIB": str(scripts / "lib"), "VERIFY_TOKEN_LOST_FILE": str(lost)},
                    capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(result.returncode, 0 if taken else 125, result.stderr)
                self.assertEqual(result.stdout, f"{scope}\n" if taken else "")
                self.assertEqual(lost.exists(), not taken)
                path = fixture.root / "grant.json"
                client.write_assignment(path, grant)
                fixture.clock.advance(1)
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion("blocked"))
                with fixture.serving():
                    _ = client.finish_step(path, result.returncode,
                        "execution token ownership changed" if lost.exists() else "")
                self.assertNotIn(request.episode, fixture.server.reservations)
                self.assertEqual(bool(fixture.server.verify_run_slots), taken)
                self.assertEqual(grant.grant.reservation in fixture.server.slots, taken)

    def test_cancel_local_grant_reports_a_sidecar_read_failure_and_can_retry(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture

        with PlacementFixture() as fixture:
            self.configure(fixture, 1)
            request = self.verify_step(fixture)
            sidecar = fixture.root / "unreadable.cgroup"
            assert isinstance(request.local, wire.LocalAdmissionContext)
            request = replace(request, local=replace(request.local, sidecar=str(sidecar)))
            grant = self.grant(fixture, request)
            cancel = wire.CancelPlacement("cancel-grant", request.episode, request.owner, "token wait ended")
            sidecar.mkdir()
            reply, = fixture.server.handle_request(cancel)
            self.assertIsInstance(reply, wire.Refused)
            self.assertIn(request.episode, fixture.server.reservations)
            self.assertIn(request.episode, fixture.server.current_assignments)
            slot, = fixture.server.verify_run_slots.values()
            self.assertEqual(slot.use, service.PendingVerifyGrants(frozenset({grant.grant.reservation})))

            sidecar.rmdir()
            self.assertEqual(fixture.connect(cancel).replies(), [wire.Ok()])
            self.assertNotIn(request.episode, fixture.server.reservations)
            self.assertNotIn(request.episode, fixture.server.current_assignments)
            self.assertFalse(fixture.server.verify_run_slots)

    def test_memory_wait_and_mac_assignment_take_no_run_slot(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate

        with PlacementFixture() as fixture:
            self.configure(fixture, 1)
            held = self.verify_step(fixture)
            fixture.free(0)
            fixture.hold_starts()
            reply, = fixture.connect(held).replies()
            assert isinstance(reply, wire.PlacementWait)
            self.assertFalse(fixture.server.verify_run_slots)
            # An older client can still send the former request while it changes versions.
            owner = wire.Owner(held.owner.pid, held.owner.start_time)
            legacy = wire.VerifyRunSlotRequest(owner, "fixture", "", placement_episode=held.episode)
            self.assertEqual(fixture.server.handle_request(legacy), [wire.Ok()])
            self.assertFalse(fixture.server.verify_run_slots)

            mac, = placement_request(fixture).constraints.candidates
            peer = self.verify_step(fixture)
            peer = replace(peer, constraints=replace(peer.constraints, candidates=(*peer.constraints.candidates, mac)))
            _ = publish_candidate(fixture, replace(peer, constraints=replace(peer.constraints, candidates=(mac,))))
            reply, = fixture.connect(peer).replies()
            assert isinstance(reply, wire.Assignment)
            self.assertEqual(reply.assignment.destination, mac.destination)
            self.assertIn(held.episode, fixture.server.waiters)
            self.assertFalse(fixture.server.verify_run_slots)

    def test_local_verify_waits_at_the_limit_and_takes_the_released_slot(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture

        with PlacementFixture() as fixture:
            self.configure(fixture, 1)
            first = self.verify_step(fixture)
            fixture.release(self.grant(fixture, first).grant)
            fixture.allow_next_start()
            peer = self.verify_step(fixture)
            reply, = fixture.connect(peer).replies()
            self.assertIsInstance(reply, wire.PlacementWait)
            assert isinstance(reply, wire.PlacementWait) and isinstance(reply.wait, wire.Wait)
            reason = records.RunLimitWait(1, 1, "fixture", fixture.server.host)
            cause = reply.wait.reason
            self.assertEqual(cause.cause if isinstance(cause, records.HeadAdmissionStatus) else cause, reason)
            reading, = fixture.connect(wire.ViewRequest()).replies()
            assert isinstance(reading, wire.ViewReply)
            step, = reading.reading.view.waiting
            self.assertEqual((step.pid, step.folder, step.verify), (peer.owner.pid, peer.identity.folder, peer.identity.verify))
            self.assertIn(f"1 of 1 fixture runs on {fixture.server.host}", reply.wait.text)

            fixture.end_owner(wire.Owner(first.owner.pid, first.owner.start_time))
            granted = self.grant(fixture, peer)
            self.assertEqual(list(fixture.server.verify_run_slots.values()),
                             [service.VerifyRunSlot("fixture", wire.Owner(peer.owner.pid, peer.owner.start_time),
                                service.PendingVerifyGrants(frozenset({granted.grant.reservation})))])

    def test_verify_grants_share_one_run_slot_until_exit_including_after_restart(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture

        with PlacementFixture() as fixture:
            self.configure(fixture, 1)
            first = self.verify_step(fixture)
            owner = wire.Owner(first.owner.pid, first.owner.start_time)
            fixture.release(self.grant(fixture, first).grant)
            slot = service.VerifyRunSlot("fixture", owner)
            self.assertEqual(list(fixture.server.verify_run_slots.values()), [slot])
            fixture.restart()
            self.assertEqual(list(fixture.server.verify_run_slots.values()), [slot])
            self.assertIn(service.OwnerWatch(owner), fixture.server.watchers.values())

            fixture.allow_next_start()
            peer = self.verify_step(fixture)
            self.assertIsInstance(fixture.connect(peer).replies()[0], wire.PlacementWait)
            fixture.clock.advance(1)
            second = self.verify_step(fixture, owner=owner, episode="verify-second")
            second = replace(second, identity=replace(second.identity, verify=records.VerifyCall("final", "verify final", 2, 2)))
            fixture.release(self.grant(fixture, second).grant)
            self.assertEqual(list(fixture.server.verify_run_slots.values()), [slot])
            fixture.end_owner(owner)
            self.assertFalse(fixture.server.verify_run_slots)
            _ = self.grant(fixture, peer)

    def test_restart_releases_a_slot_when_the_verify_owner_died_during_the_outage(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture

        with PlacementFixture() as fixture:
            self.configure(fixture, 1)
            first = self.verify_step(fixture)
            owner = wire.Owner(first.owner.pid, first.owner.start_time)
            fixture.release(self.grant(fixture, first).grant)
            self.assertEqual(list(fixture.server.verify_run_slots.values()), [service.VerifyRunSlot("fixture", owner)])
            fixture.server.close()
            fixture.proc.kill(owner)
            fixture.restart()
            self.assertFalse(fixture.server.verify_run_slots)
            self.assertNotIn(service.OwnerWatch(owner), fixture.server.watchers.values())
            _ = self.grant(fixture, self.verify_step(fixture))

    def test_restart_keeps_only_legacy_slots_with_a_local_grant(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture

        with PlacementFixture() as fixture:
            self.configure(fixture, 1)
            local, waiting = self.verify_step(fixture), self.verify_step(fixture)
            fixture.release(self.grant(fixture, local).grant)
            owners = (wire.Owner(local.owner.pid, local.owner.start_time),
                      wire.Owner(waiting.owner.pid, waiting.owner.start_time))
            granted, preflight = (service.VerifyRunSlot("fixture", owner) for owner in owners)
            fixture.server.close()
            state = wire.parse_record(fixture.state_text(), service.ServiceState)
            state = replace(state, verify_run_slots={slot.run_key: slot for slot in (granted, preflight)})
            _ = (fixture.state_dir / "state.json").write_text(wire.encode_record(state))
            fixture.restart()
            self.assertEqual(list(fixture.server.verify_run_slots.values()), [granted])

    def test_exempt_verify_runs_locally_without_taking_a_slot(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture

        with PlacementFixture() as fixture:
            self.configure(fixture, 0)
            exempt = self.verify_step(fixture, unit="natemerge")
            # Exercise an older client before its first placement request.
            legacy = wire.VerifyRunSlotRequest(wire.Owner(exempt.owner.pid, exempt.owner.start_time),
                                               "fixture", "", unit="natemerge")
            self.assertEqual(fixture.server.handle_request(legacy), [wire.Ok()])
            _ = self.grant(fixture, exempt)
            self.assertFalse(fixture.server.verify_run_slots)


class RunPlaceTests(unittest.TestCase):
    def reading(self, fixture: AdmissionFixture) -> records.QueueRead:
        reply, = fixture.server.handle_request(wire.ViewRequest())
        assert isinstance(reply, wire.ViewReply)
        return reply.reading

    def test_phase_ten_client_reconnect_after_restart_keeps_place_and_replays_one_grant(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            request = fixture.request()
            envelope = wire.parse_record(wire.encode_request(request), dict[str, object])
            data = cast(dict[str, object], envelope["data"])
            _ = data.pop("run", None)
            legacy = wire.encode_record(envelope) + "\n"
            peer = fixture.connect(legacy)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            peer.close()
            fixture.restart()
            fixture.clock.advance(1)
            other = fixture.request()
            _ = fixture.server.handle_request(other)
            peer = fixture.connect(legacy)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            self.assertEqual(fixture.queue_pids(), [request.owner.pid, other.owner.pid])
            self.assertEqual(fixture.server.waiters[request.episode].request, request)
            self.assertFalse(service.read_state(fixture.state_dir).run_places)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            granted = only_admitted(peer.replies())
            peer.close()
            fixture.restart()
            replay = fixture.connect(legacy)
            self.assertEqual(only_admitted(replay.replies()), granted)
            self.assertEqual(len([record for record in fixture.server.reservations.values()
                                  if record.owner == request.owner]), 1)

    def test_transfer_keeps_order_and_replays_one_grant_after_disconnect(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            older = fixture.request(age=10, need=40 * GIB)
            younger = fixture.request(need=40 * GIB)
            original = fixture.connect(older)
            self.assertIsInstance(original.replies()[0], wire.Wait)
            younger_peer = fixture.connect(younger)
            self.assertIsInstance(younger_peer.replies()[0], wire.Wait)
            self.assertEqual(fixture.queue_pids(), [older.owner.pid, younger.owner.pid])
            replacement = fixture.connect(older)
            self.assertIsInstance(replacement.replies()[0], wire.Wait)
            original.close()
            _ = replacement.replies()
            self.assertEqual(fixture.queue_pids(), [older.owner.pid, younger.owner.pid])
            self.assertFalse(fixture.server.reservations)

            server = fixture.server
            send = server._send  # pyright: ignore[reportPrivateUsage]
            disconnect = server._disconnect  # pyright: ignore[reportPrivateUsage]
            dropped: list[wire.Admitted] = []

            def disconnect_after_save(connection: service.Connection, reply: wire.Reply) -> None:
                if connection.request == older and isinstance(reply, wire.Admitted):
                    saved = service.read_state(fixture.state_dir)
                    self.assertEqual(saved.ledger[older.episode].reply, reply)
                    self.assertEqual(saved.reservations[older.episode].reservation_id, reply.reservation)
                    dropped.append(reply)
                    disconnect(connection)
                else:
                    send(connection, reply)

            fixture.free(50 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            with mock.patch.object(server, "_send", side_effect=disconnect_after_save):
                _ = fixture.run_pass()
            grant, = dropped
            self.assertEqual(replacement.replies(), [])
            self.assertEqual(fixture.queue_pids(), [younger.owner.pid])
            for _ in range(2):
                replay = fixture.connect(older)
                self.assertEqual(only_admitted(replay.replies()), grant)
                self.assertEqual(fixture.queue_pids(), [younger.owner.pid])
                self.assertEqual(set(fixture.server.reservations), {older.episode})
                self.assertEqual(fixture.server.reservations[older.episode].reservation_id, grant.reservation)

    def test_run_order_never_moves_an_earlier_step_behind_its_own_arrival(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.free(0)
            fixture.hold_starts()
            peer = fixture.request(since=first.since - 10)
            _ = fixture.server.handle_request(peer)
            earlier = fixture.request(run=run, since=first.since - 20)
            _ = fixture.server.handle_request(earlier)
            self.assertEqual(fixture.queue_pids(), [earlier.owner.pid, peer.owner.pid])
            row, = (row for _, row in self.reading(fixture).ordered if row["pid"] == earlier.owner.pid)
            self.assertEqual(row["since"], earlier.since)
            assert "order_since" in row
            self.assertEqual(row["order_since"], earlier.since)

    def test_later_child_keeps_run_order_and_run_exit_restores_step_arrival(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(10)
            peers = [fixture.request()]
            _ = fixture.server.handle_request(peers[0])
            fixture.clock.advance(10)
            peers.append(fixture.request())
            _ = fixture.server.handle_request(peers[1])
            fixture.clock.advance(10)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, *(peer.owner.pid for peer in peers)])
            row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == second.owner.pid)
            self.assertEqual(row["since"], second.since)
            assert "order_since" in row
            self.assertEqual(row["order_since"], first.since)
            place = next(place for place in service.read_state(fixture.state_dir).places.values()
                         if place.owner == second.owner)
            self.assertEqual(place.since, second.since)
            self.assertNotEqual(first.owner, second.owner)
            fixture.end_run(run)
            self.assertEqual(fixture.queue_pids(), [*(peer.owner.pid for peer in peers), second.owner.pid])
            self.assertEqual(fixture.server.waiters[second.episode].row["since"], second.since)
            state = service.read_state(fixture.state_dir)
            self.assertFalse(state.run_places)
            restored = next(place for place in state.places.values() if place.owner == second.owner)
            self.assertEqual(restored.since, second.since)
            row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == second.owner.pid)
            assert "order_since" in row
            self.assertEqual(row["order_since"], second.since)
            self.assertFalse(fixture.server.reservations)

    def test_doc_keeps_its_run_rank_without_a_memory_hold_or_repository_slot(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            grant = fixture.finish_step(first)
            self.assertIsInstance(fixture.server.slots[grant.reservation], records.BetweenStepsSlot)
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(1)
            peer = fixture.request()
            _ = fixture.server.handle_request(peer)
            fixture.clock.advance(1)
            doc = fixture.request(step="doc", run=run)
            doc = replace(doc, identity=replace(doc.identity, verify=records.VerifyCall("doc", "verify doc", 1, 2)))
            self.assertIsInstance(fixture.server.handle_request(doc)[0], wire.Wait)
            self.assertEqual(fixture.queue_pids(), [doc.owner.pid, peer.owner.pid])
            self.assertIsInstance(fixture.server.slots[grant.reservation], records.BetweenStepsSlot)
            self.assertIsInstance(fixture.server.waiters[doc.episode].continuation_claim, service.NoContinuationClaim)

            fixture.free(60 * GIB)
            write_pressure_reading(fixture.root / "pressure")
            fixture.allow_next_start()
            _ = fixture.run_pass()
            doc_grant = fixture.server.reservations[doc.episode].reservation_id
            self.assertFalse(fixture.server.verify_run_slots)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("doc", doc_grant, 0)), [wire.Ok()])
            self.assertNotIn(doc_grant, fixture.server.slots)

    def test_mac_busy_mend_keeps_its_rank_for_the_retry(self) -> None:
        from scripts.build_queue.test_placement import (
            PlacementFixture, adopt_assignment, assignment_for, consume_permission, local_candidate,
            placement_request, publish_candidate,
        )

        with PlacementFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(1)
            peer = fixture.request()
            _ = fixture.server.handle_request(peer)
            fixture.clock.advance(1)
            mend = placement_request(fixture)
            assert isinstance(mend.profile, records.StepProfile)
            mend = replace(mend, run=run, profile=replace(mend.profile, step="mend"),
                identity=replace(mend.identity, step="mend"), constraints=replace(mend.constraints,
                    command=("cargo", "mend"), candidates=(*mend.constraints.candidates, local_candidate(fixture))))
            _ = publish_candidate(fixture, mend)
            remote = assignment_for(fixture, mend)
            self.assertEqual(remote.destination.provider, "mac")
            saved_place = next(place for place in fixture.server.places.values() if place.episode == mend.episode)
            self.assertTrue(saved_place.move_id)
            claim = adopt_assignment(fixture, mend, remote)
            self.assertEqual(fixture.server.handle_request(consume_permission("mend-start", remote, claim)),
                             [wire.FirstAuthorization(remote, claim)])

            key = fixture.server.host_evidence_key(mend, remote.destination)
            evidence = fixture.provider_evidence[key]
            assert isinstance(evidence, placement.FreshHostEvidence)
            execution = execution_identity.ProcessExecution(remote.destination.host, evidence.generation, 700001, "start")
            observed = replace(evidence, executions=((claim, execution_identity.ObservedExecution(execution)),))
            fixture.provider_evidence[key] = observed
            fixture.server.publish_host_evidence(observed)
            _ = fixture.run_pass()
            self.assertNotIn(mend.owner.pid, fixture.queue_pids())

            fixture.clock.advance(1)
            _ = publish_candidate(fixture, mend, placement.TemporaryExclusion("busy"))
            refused, = fixture.server.handle_request(wire.ExecutionReport("mend-busy", remote, claim,
                execution_identity.ExecutionNotStarted(), placement.PrelaunchRefused("busy"),
                placement.DispatchPathSettled()))
            assert isinstance(refused, wire.DispatchStatus)
            self.assertEqual(refused.state, placement.ClosedAssignment("busy"))
            waiting, = fixture.server.handle_request(mend)
            self.assertIsInstance(waiting, wire.PlacementWait)
            self.assertEqual(fixture.queue_pids(), [mend.owner.pid, peer.owner.pid])
            returned = next(place for place in fixture.server.places.values() if place.episode == mend.episode)
            self.assertEqual(returned.move_id, saved_place.move_id)
            row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == mend.owner.pid)
            self.assertEqual(row.get("order_since"), first.since)

            fixture.clock.advance(1)
            _ = publish_candidate(fixture, mend)
            retry = assignment_for(fixture, mend)
            self.assertEqual(retry.destination, remote.destination)
            self.assertNotEqual(retry.attempt, remote.attempt)
            self.assertEqual(retry.episode, remote.episode)

    def test_restart_between_steps_keeps_the_run_watch_without_a_memory_charge(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            fixture.clock.advance(2)
            state = service.read_state(fixture.state_dir)
            self.assertEqual(list(state.run_places.values()), [wire.RunPlace(run, first.since, first.owner)])
            self.assertFalse(state.reservations)
            self.assertFalse(state.places)
            fixture.restart()
            fixture.free(0)
            fixture.hold_starts()
            other = fixture.request()
            _ = fixture.server.handle_request(other)
            fixture.clock.advance(2)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [second.owner.pid, other.owner.pid])
            self.assertEqual(fixture.server.waiters[second.episode].row["since"], second.since)
            row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == second.owner.pid)
            assert "order_since" in row
            self.assertEqual(row["order_since"], first.since)
            fixture.end_run(run)
            self.assertEqual(fixture.queue_pids(), [other.owner.pid, second.owner.pid])
            self.assertFalse(service.read_state(fixture.state_dir).run_places)

    def test_returning_run_sorts_by_its_first_wait_start(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, step="gate")
            _ = fixture.finish_step(first, end_owner=True)
            fixture.allow_next_start()
            fixture.restart()
            saved, = service.read_state(fixture.state_dir).run_places.values()
            fixture.hold_starts()
            peers = [fixture.request(step="gate", owner=fixture.fixture_owner(pid, "4000"))
                     for pid in (700001, 700002)]
            for peer in peers:
                self.assertIsInstance(fixture.server.handle_request(peer)[0], wire.Wait)
            returning = fixture.request(run=run, step="gate")
            self.assertIsInstance(fixture.server.handle_request(returning)[0], wire.Wait)
            self.assertEqual(fixture.queue_pids(), [returning.owner.pid, *(peer.owner.pid for peer in peers)])
            row, = (row for _, row in self.reading(fixture).ordered if row["pid"] == returning.owner.pid)
            self.assertEqual(row["since"], returning.since)
            self.assertEqual(row.get("order_since"), first.since)
            self.assertTrue(saved.move_id)
            self.assertEqual(saved.repository, "fixture")
            self.assertNotEqual(row.get("move_id"), saved.move_id)

    def test_tied_waiter_keeps_the_first_step_at_the_head(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, step="gate", owner=fixture.fixture_owner(700001, "4000"))
            _ = fixture.finish_step(first)
            fixture.allow_next_start()
            fixture.restart()
            fixture.hold_starts()
            peer = fixture.request(step="gate", since=first.since,
                                   owner=fixture.fixture_owner(700002, "4000"))
            self.assertIsInstance(fixture.server.handle_request(peer)[0], wire.Wait)
            returning = fixture.request(run=run, step="gate", owner=fixture.fixture_owner(700003, "4000"))
            self.assertIsInstance(fixture.server.handle_request(returning)[0], wire.Wait)
            self.assertGreater(returning.since, peer.since)
            rows = [row for _, row in self.reading(fixture).ordered]
            self.assertEqual([row.get("order_since") for row in rows], [first.since, first.since])
            self.assertEqual([row["pid"] for row in rows], [returning.owner.pid, peer.owner.pid])
            write_pressure_reading(fixture.root / "pressure")
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {returning.episode})

    def test_replacement_gate_saves_its_move_id_with_the_inherited_arrival(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            old = fixture.request(run=fixture.run_owner(), step="gate")
            old = replace(old, identity=replace(old.identity, unit="queue", job=StepJob("test-affected")))
            self.assertIsInstance(fixture.server.handle_request(old)[0], wire.Wait)
            fixture.clock.advance(10)
            run = fixture.run_owner()
            new = fixture.request(run=run, step="gate")
            new = replace(new, identity=replace(new.identity, unit=old.identity.unit, job=old.identity.job))
            peer = fixture.connect(new)
            self.assertIsInstance(peer.replies()[0], wire.Wait)
            row = fixture.server.waiters[new.episode].row
            move_id = row.get("move_id", "")
            self.assertTrue(move_id)
            fixture.free(60 * GIB)
            fixture.allow_next_start()
            _ = fixture.server.run_pass()
            _ = only_admitted(peer.replies())
            saved = service.read_state(fixture.state_dir).run_places[records.run_owner(run.pid, run.start_time)]
            self.assertEqual((saved.since, saved.move_id, saved.repository), (old.since, move_id, "fixture"))

    def test_returning_gate_uses_its_current_units_priority(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, step="gate")
            first = replace(first, identity=replace(first.identity, unit="former"))
            _ = fixture.finish_step(first)
            saved, = service.read_state(fixture.state_dir).run_places.values()
            self.assertEqual(saved.unit, "former")
            fixture.hold_starts()
            priority = ("former", "peer", "current")
            fixture.server.priority = records.QueuePriority(tuple(
                {"unit": unit, "why": "fixture", "set_at": fixture.clock.now(), "set_by": "fixture"}
                for unit in priority))
            peer = fixture.request(step="gate")
            peer = replace(peer, identity=replace(peer.identity, unit="peer"))
            returning = fixture.request(run=run, step="gate", need=first.profile.peak.bytes * 2)
            returning = replace(returning, identity=replace(returning.identity, unit="current"))
            for request in (peer, returning):
                self.assertIsInstance(fixture.server.handle_request(request)[0], wire.Wait)
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            self.assertEqual([records.waiter_priority(row) for _, row in gate.ranked_queue_waiters(inputs)],
                             [records.PriorityUnit("peer", 1), records.PriorityUnit("current", 2)])
            fixture.allow_next_start()
            write_pressure_reading(fixture.root / "pressure")
            _ = fixture.run_pass()
            self.assertEqual(set(fixture.server.reservations), {peer.episode})

    def test_younger_run_logs_once_when_it_starts_in_a_gate_gap(self) -> None:
        with AdmissionFixture() as fixture:
            first_run, second_run = fixture.run_owner(), fixture.run_owner()
            first = fixture.request(run=first_run, step="gate")
            first_grant = only_admitted(fixture.server.handle_request(first))
            fixture.allow_next_start()
            second = fixture.request(run=second_run, step="gate")
            second_grant = only_admitted(fixture.server.handle_request(second))
            fixture.release(first_grant)
            fixture.release(second_grant)
            fixture.allow_next_start()
            self.assertEqual(len(fixture.server.slots), 2)
            younger_run = fixture.run_owner()
            younger = fixture.request(run=younger_run, step="gate")
            with redirect_stderr(io.StringIO()) as journal:
                grant = only_admitted(fixture.server.handle_request(younger))
                self.assertEqual(only_admitted(fixture.server.handle_request(younger)), grant)
                _ = fixture.run_pass()
            lines = [line for line in journal.getvalue().splitlines() if "started in the gate gap" in line]
            expected = (f"memory admission: run {younger_run.pid}:{younger_run.start_time} "
                        f"started in the gate gap of {first_run.pid}:{first_run.start_time} "
                        f"for fixture on {fixture.server.host}")
            self.assertEqual(lines, [expected])

    def test_old_run_place_loads_with_empty_move_id_and_repository(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            _ = fixture.finish_step(fixture.request(run=run), end_owner=True)
            saved = cast(dict[str, object], json.loads(fixture.state_text()))
            places = cast(dict[str, dict[str, object]], saved["run_places"])
            place, = places.values()
            _ = place.pop("move_id", None)
            _ = place.pop("repository", None)
            _ = (fixture.state_dir / "state.json").write_text(json.dumps(saved))
            restored, = service.read_state(fixture.state_dir).run_places.values()
            self.assertEqual(restored.move_id, "")
            self.assertEqual(restored.repository, "")

    def test_main_892cb2d74f_loads_run_place_with_new_fields(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            _ = fixture.finish_step(fixture.request(run=run, step="gate"), end_owner=True)
            place, = service.read_state(fixture.state_dir).run_places.values()
            self.assertTrue(place.move_id)
            self.assertEqual(place.repository, "fixture")
            baseline = fixture.root / "baseline"
            baseline.mkdir()
            for name in ("admission_protocol.py", "admission_wire.py", "admission_service.py", "snapshot_codec.py"):
                source = subprocess.check_output(
                    ("git", "-C", str(repository), "show", f"892cb2d74f:scripts/build_queue/{name}"),
                    timeout=5)
                _ = (baseline / name).write_bytes(source)
            check = """
import sys
from pathlib import Path
import scripts.build_queue
scripts.build_queue.__path__ = [sys.argv[1], *scripts.build_queue.__path__]
from scripts.build_queue import admission_service, admission_protocol
place, = admission_service.read_state(Path(sys.argv[2])).run_places.values()
assert type(place) is admission_protocol.RunPlace
assert place.run.pid == int(sys.argv[3])
assert not hasattr(place, 'move_id')
assert not hasattr(place, 'repository')
"""
            result = subprocess.run((sys.executable, "-c", check, str(baseline),
                                     str(fixture.state_dir), str(run.pid)),
                                    capture_output=True, text=True, check=False, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_exit_between_steps_removes_the_idle_watch_and_saved_place(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            self.assertTrue(service.read_state(fixture.state_dir).run_places)
            fixture.end_run(run)
            self.assertFalse(service.read_state(fixture.state_dir).run_places)
            self.assertFalse(fixture.server.reservations)
            fixture.clock.advance(1)
            fixture.free(0)
            fixture.hold_starts()
            stale = fixture.request(run=run)
            _ = fixture.server.handle_request(stale)
            row, = (row for _, row in self.reading(fixture).ordered)
            assert "order_since" in row
            self.assertEqual(row["order_since"], stale.since)

    def test_child_episodes_and_reservations_stay_separate_under_one_run(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            fixture.free(0)
            fixture.hold_starts()
            first = fixture.request(run=run)
            _ = fixture.server.handle_request(first)
            fixture.clock.advance(1)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            self.assertEqual(fixture.queue_pids(), [first.owner.pid, second.owner.pid])
            for _, row in self.reading(fixture).ordered:
                assert "order_since" in row
                self.assertEqual(row["order_since"], first.since)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            one = only_admitted([fixture.server.ledger[first.episode].reply])
            fixture.allow_next_start()
            _ = fixture.server.run_pass()
            two = only_admitted([fixture.server.ledger[second.episode].reply])
            self.assertNotEqual(first.episode, second.episode)
            self.assertNotEqual(one.reservation, two.reservation)
            self.assertEqual(set(fixture.server.reservations), {first.episode, second.episode})
            for request, granted in ((first, one), (second, two)):
                record = fixture.server.reservations[request.episode]
                self.assertEqual(record.owner, request.owner)
                self.assertEqual(record.queue_waiter["since"], request.since)
                replay = only_admitted(fixture.server.handle_request(request))
                self.assertEqual(replay, granted)
            self.assertEqual(len(service.read_state(fixture.state_dir).run_places), 1)
            _ = fixture.server.handle_request(wire.ReleaseRequest("release-one", one.reservation, 0))
            self.assertEqual(set(fixture.server.reservations), {second.episode})
            _ = fixture.server.handle_request(wire.ReleaseRequest("release-two", two.reservation, 0))
            self.assertFalse(fixture.server.reservations)
            self.assertEqual(len(service.read_state(fixture.state_dir).run_places), 1)

    def test_reconnect_and_token_reacquire_use_the_child_arrival_key(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.clock.advance(10)
            second = fixture.request(run=run)
            two = only_admitted(fixture.server.handle_request(second))
            _ = fixture.server.handle_request(wire.TokenWaitRequest("token-gap", two.reservation))
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(11)
            again = replace(second, episode="child-after-token", token_back_at=fixture.clock.now())
            _ = fixture.server.handle_request(again)
            fixture.restart()
            _ = fixture.server.handle_request(again)
            _ = fixture.server.handle_request(again)
            places = [place for place in service.read_state(fixture.state_dir).places.values()
                      if place.owner == second.owner]
            self.assertEqual(len(places), 1)
            self.assertEqual(places[0].since, second.since)
            row, = (row for _, row in self.reading(fixture).ordered)
            self.assertEqual(row["since"], second.since)
            assert "order_since" in row
            self.assertEqual(row["order_since"], first.since)
            with (fixture.state_dir / "queue_times.jsonl").open("rb") as stream:
                gaps = [row for row in gate.queue_times_backward(stream) if row.get("kind") == "token_gap"]
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0]["episode"], again.episode)
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            later = only_admitted([fixture.server.ledger[again.episode].reply])
            self.assertNotEqual(later.reservation, two.reservation)

    def test_reused_run_pid_and_no_run_keep_each_steps_own_order(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first)
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(1)
            ordinary = fixture.request()
            _ = fixture.server.handle_request(ordinary)
            requests: list[wire.AdmitRequest] = []
            for identity in (wire.RunIdentity(run.pid, str(int(run.start_time) + 1)), wire.NoRun()):
                fixture.clock.advance(1)
                request = fixture.request(run=identity)
                requests.append(request)
                _ = fixture.server.handle_request(request)
                row = next(row for _, row in self.reading(fixture).ordered if row["pid"] == request.owner.pid)
                assert "order_since" in row
                self.assertEqual(row["order_since"], request.since)
            self.assertEqual(fixture.queue_pids(), [ordinary.owner.pid, *(request.owner.pid for request in requests)])
            self.assertEqual(list(service.read_state(fixture.state_dir).run_places.values()),
                             [wire.RunPlace(run, first.since, first.owner)])

    def test_restart_drops_a_run_that_ended_while_the_service_was_stopped(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.server.handle_request(first)
            fixture.clock.advance(1)
            peer = fixture.request()
            _ = fixture.server.handle_request(peer)
            fixture.clock.advance(1)
            second = fixture.request(run=run)
            _ = fixture.server.handle_request(second)
            fixture.server.close()
            fixture.proc.kill(wire.Owner(run.pid, run.start_time))
            fixture.server = fixture.new_server()
            for request in (second, peer, first):
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.queue_pids(), [first.owner.pid, peer.owner.pid, second.owner.pid])
            self.assertFalse(service.read_state(fixture.state_dir).run_places)

    def test_run_types_round_trip_in_admit_and_service_state(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            run = fixture.run_owner()
            request = fixture.request(run=run)
            for identity in (run, wire.NoRun()):
                with self.subTest(identity=identity):
                    message = replace(request, run=identity)
                    self.assertEqual(wire.parse_request(wire.encode_request(message)), message)
            _ = fixture.server.handle_request(request)
            state = service.read_state(fixture.state_dir)
            self.assertEqual(list(state.run_places.values()), [wire.RunPlace(run, request.since, request.owner)])
            self.assertEqual(wire.parse_record(wire.encode_record(state), service.ServiceState), state)
            for value, schema in ((run, wire.RunIdentity), (wire.NoRun(), wire.NoRun),
                                  (wire.RunPlace(run, request.since, request.owner), wire.RunPlace)):
                self.assertEqual(codec.parse_snapshot_value(codec.snapshot_value(value), schema), value)
            fixture.restart()
            self.assertEqual(service.read_state(fixture.state_dir), state)

    def test_phase_ten_state_upgrade_preserves_pending_work_priority_and_replay(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request(step="build")
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            grant = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(grant)
            fixture.fault.stage = "after_finalization_intent"
            with self.assertRaises(InjectedCrash):
                _ = fixture.server.handle_request(wire.ReleaseRequest("pending-release", grant.reservation, 137))
            saved = service.read_state(fixture.state_dir)
            self.assertNotIsInstance(saved.reservations[request.episode].finalization, service.NoFinalizationIntent)
            priority = records.QueuePriority(({"unit": "queue", "why": "the user", "set_at": fixture.clock.now(),
                "set_by": "director"},))
            expected = replace(saved, priority=priority, build_history_generation=7)
            old = wire.parse_record(wire.encode_record(expected), dict[str, object])
            del old["run_places"]
            path = fixture.state_dir / "state.json"
            _ = path.write_text(wire.encode_record(old))
            before = path.read_text()
            restored = service.read_state(fixture.state_dir)
            self.assertEqual(restored, expected)
            self.assertEqual(path.read_text(), before)
            self.assertEqual(restored.ledger[request.episode].reply, grant)
            self.assertEqual(restored.reservations[request.episode].ending, saved.reservations[request.episode].ending)

    def test_display_age_and_aws_wait_threshold_use_child_arrival_not_run_order(self) -> None:
        from scripts.build_queue import queue_lines

        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run, since=fixture.clock.now() - 600)
            _ = fixture.finish_step(first)
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(11)
            second = fixture.request(run=run, step="check")
            second = replace(second, profile=replace(second.profile,
                repository=records.RepositoryIdentity("github.com/team/project", "project")))
            _ = fixture.server.handle_request(second)
            reading = self.reading(fixture)
            self.assertEqual(reading.view.waiting[0].since, second.since)
            self.assertEqual(queue_lines.age(reading.view.waiting[0].since, reading.explanation.now), 0)


class AdmissionConfigurationTests(unittest.TestCase):
    def test_admission_fixture_leaves_an_inherited_config_folder_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inherited = Path(temporary) / "inherited-conf"
            with mock.patch.dict(os.environ, {"CONF_DIR": str(inherited)}), AdmissionFixture() as fixture:
                self.assertEqual(os.environ["CONF_DIR"], str(fixture.root))
                self.assertTrue((fixture.root / "build_folders.conf").is_file())
                self.assertFalse(inherited.exists())

    def test_invalid_remote_start_hold_is_refused_during_configuration_read(self) -> None:
        for value in (None, "invalid", "86401"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_memory_config(root / "build_admission.conf", remote_start_hold_seconds=value)
                with AdmissionFixture(root=root) as fixture:
                    reply, = fixture.connect(wire.ViewRequest()).replies()
                    self.assertIsInstance(reply, wire.Refused)
                    assert isinstance(reply, wire.Refused)
                    self.assertIn("remote_start_hold_seconds", reply.reason)

    def test_slow_requests_log_wall_cpu_and_kind_but_fast_requests_do_not(self) -> None:
        for limit in (0.25, 0.75):
            with self.subTest(limit=limit), AdmissionFixture() as fixture:
                fixture.configure(request_slow_seconds=limit)
                fixture.server.run_once(0)
                durations = iter((0.5, 0.1))

                def measured(request: wire.Request, connection: service.Connection | service.NoConnection) -> list[wire.Reply]:
                    del request, connection
                    fixture.clock.advance(next(durations))
                    return [wire.BuildHistoryReply(0)]

                output = io.StringIO()
                with mock.patch.object(fixture.server, "_handle_request", side_effect=measured), \
                        mock.patch.object(time, "thread_time", side_effect=(2.0, 2.125, 3.0)), \
                        redirect_stderr(output):
                    for _ in range(2):
                        self.assertEqual(fixture.server.handle_request(wire.BuildHistoryRequest()), [wire.BuildHistoryReply(0)])
                self.assertEqual(output.getvalue(), (
                    "admission request BuildHistoryRequest: wall=0.500s cpu=0.125s\n" if limit == 0.25 else ""))

    def test_bad_request_timing_limit_logs_once_and_keeps_requests_and_cleanup_available(self) -> None:
        with AdmissionFixture() as fixture:
            grant = only_admitted(fixture.server.handle_request(fixture.request()))
            write_memory_config(fixture.root / "build_admission.conf", request_slow_seconds="invalid")
            output = io.StringIO()
            with mock.patch.object(records, "_warned_configuration_diagnostics", set[records.ConfigurationDiagnostic]()), \
                    redirect_stderr(output):
                for _ in range(2):
                    self.assertIsInstance(fixture.connect(wire.BuildHistoryRequest()).replies()[0], wire.BuildHistoryReply)
                fixture.release(grant)
            self.assertEqual(output.getvalue().count("request_slow_seconds"), 1)
            self.assertFalse(fixture.server.reservations)


    def test_ledger_grants_restore_replay_and_release_without_usable_configuration(self) -> None:
        with AdmissionFixture() as fixture:
            old_request = fixture.request(need=4 * GIB)
            old_grant = only_admitted(fixture.server.handle_request(old_request))
            saved_request = fixture.request(need=4 * GIB)
            if saved_request.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            saved_grant = only_admitted(fixture.server.handle_request(saved_request))
            saved_grant = replace(saved_grant, cleanup_timing=wire.CleanupTiming(17, 3))
            fixture.server.ledger[saved_request.episode] = replace(
                fixture.server.ledger[saved_request.episode], reply=saved_grant)
            self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("absent", "absent", 0)), [wire.Ok()])
            fixture.server.close()
            write_memory_config(fixture.root / "build_admission.conf", estimate_refresh_seconds="invalid")
            fixture.server = fixture.new_server()
            expected = old_grant
            if old_request.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            self.assertEqual(only_admitted(fixture.server.handle_request(old_request)), expected)
            if saved_request.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            self.assertEqual(only_admitted(fixture.server.handle_request(saved_request)), saved_grant)
            self.assertEqual(fixture.server.ledger["absent"].reply, wire.Ok())
            with mock.patch.object(fixture.server, "_log_request_timing"), \
                    mock.patch.object(conf, "read", side_effect=AssertionError("release read config")):
                for grant in (expected, saved_grant):
                    self.assertEqual(fixture.server.handle_request(
                        wire.ReleaseRequest(f"release-{grant.reservation}", grant.reservation, 0)), [wire.Ok()])
            self.assertFalse(service.read_state(fixture.state_dir).reservations)

    def test_watch_errors_keep_cleanup_available_and_retry_configuration_recovery(self) -> None:
        for operation in ("is_dir", "add_watch"):
            with self.subTest(operation=operation), AdmissionFixture() as fixture:
                grant = only_admitted(fixture.server.handle_request(fixture.request()))
                folder = fixture.root / "watched-config"
                config = folder / "build_admission.conf"
                write_memory_config(config, estimate_refresh_seconds="invalid")
                with mock.patch.dict(os.environ, {"CONF_DIR": str(folder)}):
                    fixture.restart()
                    config.unlink()
                    folder.rmdir()
                    is_dir = Path.is_dir
                    ancestors = {folder, *folder.parents}

                    def inaccessible(path: Path) -> bool:
                        if path in ancestors:
                            raise OSError(errno.EACCES, "watch folder unavailable")
                        return is_dir(path)

                    patch = (mock.patch.object(Path, "is_dir", inaccessible) if operation == "is_dir" else
                             mock.patch.object(inotify, "add_watch", side_effect=OSError(errno.EACCES, "watch denied")))
                    with patch:
                        fixture.server.run_once(0)
                        self.assertFalse(fixture.server.stopped)
                        self.assertEqual(fixture.server.config_watch, -1)
                        self.assertTrue(math.isfinite(fixture.server.next_pass))
                        self.assertEqual(fixture.connect(wire.ReleaseRequest("release", grant.reservation, 0)).replies(),
                                         [wire.Ok()])
                        self.assertFalse(fixture.server.reservations)
                    write_memory_config(config)
                    fixture.clock.advance(wire.PROTOCOL_CLEANUP_TIMING.reconnect_seconds)
                    fixture.server.run_once(0)
                    self.assertIsInstance(fixture.server.last_policy, records.AdmissionPolicy)
                    self.assertIsInstance(fixture.connect(wire.ViewRequest()).replies()[0], wire.ViewReply)
                    fixture.allow_next_start()
                    request = fixture.request()
                    self.assertEqual(only_admitted(fixture.server.handle_request(request)).need, request.profile.peak.bytes)

    def test_persist_prunes_dead_and_reused_callers_but_keeps_live_callers_without_reservations(self) -> None:
        with AdmissionFixture() as fixture:
            dead, reused, live = (fixture.request() for _ in range(3))
            for request in (dead, reused, live):
                _ = fixture.finish_step(request)
            fixture.proc.kill(dead.owner)
            owner_start = fixture.proc.owner_start

            def current_start(pid: int) -> service.LiveOwnerIdentity | service.OwnerProcessGone:
                return service.LiveOwnerIdentity(reused.owner.start_time + "1") if pid == reused.owner.pid else owner_start(pid)

            write_memory_config(fixture.root / "build_admission.conf", estimate_refresh_seconds="invalid")
            _ = fixture.server.run_pass()
            with mock.patch.object(fixture.proc, "owner_start", side_effect=current_start):
                fixture.server.persist()
            self.assertFalse(fixture.server.reservations)
            self.assertEqual(service.read_state(fixture.state_dir).caller_versions,
                             {f"{live.owner.pid}-{live.owner.start_time}": live.code_version})

    def test_release_keeps_unrelated_running_evidence_and_defers_admission_until_the_pass(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.compiler(GIB)
            scope = fixture.root / "builds.slice/cache.scope"
            scope.mkdir(parents=True)
            _ = (scope / "memory.current").write_text(str(GIB))
            _ = (scope / "memory.stat").write_text("inactive_file 0\n")
            _ = (scope / "cgroup.procs").write_text("")
            first = fixture.request(need=4 * GIB)
            grant = only_admitted(fixture.server.handle_request(first))
            second = fixture.request(need=4 * GIB)
            if second.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            _ = only_admitted(fixture.server.handle_request(second))
            waiting = fixture.request(need=14 * GIB)
            self.assertIsInstance(fixture.server.handle_request(waiting)[0], wire.Wait)
            before = fixture.server.last_inputs
            assert isinstance(before, records.QueueInputs)
            other_running = tuple(step for step in before.running if step.session_id != first.identity.session_id)
            with mock.patch.object(gate, "admission_pass", side_effect=AssertionError("release ran admission")):
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0)), [wire.Ok()])
            after = fixture.server.last_inputs
            assert isinstance(after, records.QueueInputs)
            self.assertEqual(after.running, other_running)
            self.assertEqual({row["pid"] for row in after.reservations}, {second.owner.pid})
            self.assertNotIn(waiting.episode, fixture.server.reservations)
            fixture.allow_next_start()
            with mock.patch.object(gate, "admission_pass", wraps=gate.admission_pass) as passed:
                self.assertIsInstance(fixture.server.handle_request(wire.ViewRequest())[0], wire.ViewReply)
            passed.assert_not_called()
            self.assertNotIn(waiting.episode, fixture.server.reservations)
            fixture.server.run_once(0)
            self.assertIn(waiting.episode, fixture.server.reservations)

    def test_drain_ignores_current_socket_waiters_and_counts_older_caller_code(self) -> None:
        scripts = Path(__file__).resolve().parents[1]
        for name, expected in (("gate.py", True), ("cloud_gate.py", False), ("gate_box.py", False)):
            with self.subTest(name=name):
                self.assertEqual(name in service.ADMISSION_CALLERS, expected)
        for relative in ("direct/verify.sh", "lint/invoke.sh", "production/merge_gate.py", "production/gate.py", "build_queue/admission_client.py",
                         "build_queue/queue_place.py", "mac_test/offload.py", "mac_test/copy_watch.py"):
            with self.subTest(script=relative), AdmissionFixture() as fixture:
                caller = fixture.proc.spawn()
                script = fixture.root / Path(relative).name
                current = (scripts / relative).read_bytes()
                _ = script.write_bytes(current)
                _ = (fixture.proc.path / str(caller.pid) / "cmdline").write_bytes(
                    f"python3\0{script}\0".encode())
                request = replace(fixture.request(owner=caller), code_version=wire.admission_code_version())
                grant = only_admitted(fixture.server.handle_request(request))
                _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
                for older in (False, True):
                    with self.subTest(older=older), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                        _ = script.write_bytes(current + b"\n# older caller snapshot\n" if older else current)
                        status = service.drain_check(fixture.state_dir, service=True,
                                                     include_gate_callers=True, proc=fixture.proc)
                    self.assertEqual(status, 1 if older else 0)
                    if older:
                        self.assertIn(f"clients: 1: {caller.pid}", output.getvalue())
                    else:
                        self.assertEqual(output.getvalue(), "drained\n")

    def test_drain_counts_a_caller_with_an_older_imported_admission_module(self) -> None:
        with AdmissionFixture() as fixture:
            scripts = fixture.root / "scripts"
            source = Path(__file__).resolve().parents[1]
            for relative in admission_code.ADMISSION_CODE_PATHS:
                target = scripts / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _ = target.write_bytes((source / relative).read_bytes())
            caller = fixture.proc.spawn()
            script = scripts / "direct/verify.sh"
            _ = (fixture.proc.path / str(caller.pid) / "cmdline").write_bytes(
                f"bash\0{script}\0".encode())
            loaded_version = wire.admission_code_version(scripts)
            request = replace(fixture.request(owner=caller), code_version=loaded_version)
            grant = only_admitted(fixture.server.handle_request(request))
            _ = fixture.server.handle_request(wire.ReleaseRequest("release", grant.reservation, 0))
            with mock.patch.object(service, "__file__", str(scripts / "build_queue/admission_service.py")):
                with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(service.drain_check(fixture.state_dir, service=True,
                        include_gate_callers=True, proc=fixture.proc), 0)
                self.assertEqual(output.getvalue(), "drained\n")
                imported = scripts / "lint/memory_admit.py"
                _ = imported.write_bytes(imported.read_bytes() + b"\n# Changed admission code.\n")
                with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(service.drain_check(fixture.state_dir, service=True,
                        include_gate_callers=True, proc=fixture.proc), 1)
                self.assertIn(f"clients: 1: {caller.pid}", output.getvalue())

    def test_drain_counts_a_caller_without_a_recorded_admission_version(self) -> None:
        with AdmissionFixture() as fixture:
            caller = fixture.proc.spawn()
            script = Path(__file__).resolve().parents[1] / "direct/verify.sh"
            _ = (fixture.proc.path / str(caller.pid) / "cmdline").write_bytes(
                f"bash\0{script}\0".encode())
            with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(service.drain_check(fixture.state_dir, service=True,
                    include_gate_callers=True, proc=fixture.proc), 1)
            self.assertIn(f"clients: 1: {caller.pid}", output.getvalue())

    def test_unusable_configuration_bounds_requests_and_keeps_the_last_usable_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "build_admission.conf"
            write_memory_config(config, request_max_bytes="invalid")
            with AdmissionFixture(root=root) as fixture:
                oversized = fixture.connect("x" * (wire.CLEANUP_REQUEST_MAX_BYTES + 1))
                refused, = oversized.replies()
                self.assertIsInstance(refused, wire.Refused)
                self.assertIn(str(config), wire.encode_reply(refused))
                self.assertIn("request_max_bytes", wire.encode_reply(refused))
                self.assertFalse(fixture.server.reservations)
                maximum_cleanup = wire.ReleaseRequest("\0" * wire.CLEANUP_STRING_MAX,
                    "\0" * wire.CLEANUP_STRING_MAX, wire.CLEANUP_STATUS_MIN,
                    code_version="\0" * wire.CLEANUP_STRING_MAX)
                self.assertEqual(fixture.connect(maximum_cleanup).replies(), [wire.Ok()])
                write_memory_config(config, request_max_bytes="512")
                fixture.server.run_once(0)
                request = fixture.request()
                grant = only_admitted(fixture.server.handle_request(request))
                write_memory_config(config, request_max_bytes="invalid")
                fixture.server.run_once(0)
                large = replace(request, argv=("cargo", "x" * 1024))
                refused, = fixture.connect(large).replies()
                self.assertIsInstance(refused, wire.Refused)
                self.assertIn("exceeds", wire.encode_reply(refused))
                cleanup = wire.ReleaseRequest("r" * wire.CLEANUP_STRING_MAX, grant.reservation, 0)
                self.assertGreater(len(wire.encode_request(cleanup).encode()), 512)
                self.assertEqual(fixture.connect(cleanup).replies(), [wire.Ok()])
                self.assertFalse(fixture.server.reservations)

    def test_missing_config_folder_rearms_its_watch_before_the_file_arrives(self) -> None:
        with AdmissionFixture() as fixture:
            folder = fixture.root / "new-config"
            config = folder / "build_admission.conf"
            with mock.patch.dict(os.environ, {"CONF_DIR": str(folder)}):
                fixture.restart()
                request = fixture.request()
                refused, = fixture.server.handle_request(request)
                self.assertIsInstance(refused, wire.Refused)
                self.assertIn(str(config), wire.encode_reply(refused))
                folder.mkdir()
                fixture.server.run_once(0)
                self.assertFalse(fixture.server.reservations)
                write_memory_config(config)
                fixture.server.run_once(0)
                view, = fixture.server.handle_request(wire.ViewRequest())
                self.assertIsInstance(view, wire.ViewReply)
                grant = only_admitted(fixture.server.handle_request(request))
                self.assertEqual(grant.need, request.profile.peak.bytes)
                self.assertEqual(len(fixture.server.reservations), 1)

    def test_config_permission_repair_recovers_without_an_inode_or_mtime_change(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            before = config.stat()
            config.chmod(0)
            try:
                fixture.server.run_once(0)
                request = fixture.request()
                refused, = fixture.server.handle_request(request)
                self.assertIsInstance(refused, wire.Refused)
                self.assertIn(str(config), wire.encode_reply(refused))
                self.assertFalse(fixture.server.reservations)
            finally:
                config.chmod(stat.S_IMODE(before.st_mode))
            self.assertEqual((config.stat().st_ino, config.stat().st_mtime_ns),
                             (before.st_ino, before.st_mtime_ns))
            fixture.server.run_once(0)
            view, = fixture.server.handle_request(wire.ViewRequest())
            self.assertIsInstance(view, wire.ViewReply)
            grant = only_admitted(fixture.server.handle_request(request))
            self.assertEqual(grant.need, request.profile.peak.bytes)
            self.assertEqual(len(fixture.server.reservations), 1)

    def test_configuration_reads_supply_a_complete_service_pass(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            _ = fixture.server.handle_request(fixture.request())
            with mock.patch.object(conf, "read", wraps=conf.read) as reader:
                _ = fixture.server.run_pass()
            # The pass reads cloud settings, memory settings, and the request timing limit.
            self.assertEqual(reader.call_args_list, [mock.call("build_admission")] * 3)

    def test_unusable_config_restart_replays_one_existing_reservation(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            grant = only_admitted(fixture.server.handle_request(request))
            write_memory_config(fixture.root / "build_admission.conf", estimate_refresh_seconds="invalid")
            _ = fixture.server.run_pass()
            fixture.restart()
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    if request.episode not in fixture.server.reservations:
                        fixture.allow_next_start()
                    self.assertEqual(only_admitted(fixture.server.handle_request(request)), grant)
                    self.assertEqual(set(fixture.server.reservations), {request.episode})
                    record = fixture.server.reservations[request.episode]
                    self.assertEqual(record.owner, request.owner)
                    self.assertEqual(record.queue_waiter["since"], request.since)
            fixture.end_owner(request.owner)
            self.assertFalse(fixture.server.reservations)
            self.assertFalse(service.read_state(fixture.state_dir).reservations)

    def test_unusable_startup_serves_refusal_and_recovers_on_the_next_pass(self) -> None:
        for bad in (None, "invalid"):
            with self.subTest(value=bad), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = root / "build_admission.conf"
                write_memory_config(config, estimate_refresh_seconds=bad)
                with AdmissionFixture(root=root) as fixture:
                    self.assertTrue(fixture.socket_path.is_socket())
                    request = fixture.request()
                    replies = fixture.server.handle_request(request)
                    self.assertFalse(any(isinstance(reply, wire.Admitted) for reply in replies))
                    self.assertFalse(fixture.server.reservations)
                    reply, = fixture.server.handle_request(wire.ViewRequest())
                    encoded = wire.encode_reply(reply)
                    self.assertIn(str(config), encoded)
                    self.assertIn("estimate_refresh_seconds", encoded)
                    self.assertEqual(wire.parse_reply(encoded), reply)
                    socket_reply, = fixture.connect(wire.ViewRequest()).replies()
                    self.assertEqual(socket_reply, reply)
                    self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("absent", "absent", 0)), [wire.Ok()])
                    write_memory_config(config, estimate_refresh_seconds="600")
                    _ = fixture.server.run_pass()
                    granted = only_admitted(fixture.server.handle_request(request))
                    self.assertEqual(granted.need, request.profile.peak.bytes)
                    self.assertEqual(len(fixture.server.reservations), 1)

    def test_bad_live_key_stops_grants_and_repair_keeps_the_waiter_identity(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.hold_starts()
            request = fixture.request()
            _ = fixture.server.handle_request(request)
            place, = service.read_state(fixture.state_dir).places.values()
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, estimate_refresh_seconds="invalid")
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            self.assertFalse(fixture.server.reservations)
            saved_place, = service.read_state(fixture.state_dir).places.values()
            self.assertEqual(saved_place, place)
            reply, = fixture.server.handle_request(wire.ViewRequest())
            self.assertIn("estimate_refresh_seconds", wire.encode_reply(reply))
            write_memory_config(config, estimate_refresh_seconds="900")
            _ = fixture.server.run_pass()
            granted = only_admitted(fixture.server.handle_request(request))
            self.assertEqual(granted.need, request.profile.peak.bytes)
            self.assertEqual(fixture.server.reservations[request.episode].owner, request.owner)
            self.assertEqual(fixture.server.reservations[request.episode].queue_waiter["since"], request.since)
            configuration = fixture.server.last_usable_configuration
            assert isinstance(configuration, records.MemoryConfig)
            self.assertEqual(configuration.policy.timing.estimate_refresh_seconds, 900)

    def test_committed_recovery_warns_once_and_grants_with_the_recovered_value(self) -> None:
        with AdmissionFixture() as fixture:
            config = fixture.root / "build_admission.conf"
            write_memory_config(config, estimate_refresh_seconds="321")
            commit_config_fixture(config)
            write_memory_config(config, estimate_refresh_seconds="invalid")
            request = fixture.request()
            with mock.patch.object(records, "_warned_configuration_diagnostics", set[records.ConfigurationDiagnostic]()), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as errors:
                granted = only_admitted(fixture.server.handle_request(request))
                _ = fixture.server.run_pass()
            policy = fixture.server.last_policy
            assert isinstance(policy, records.AdmissionPolicy)
            self.assertEqual(policy.timing.estimate_refresh_seconds, 321)
            self.assertEqual(granted.need, request.profile.peak.bytes)
            self.assertEqual(errors.getvalue(), "")
            diagnostic, = fixture.server.last_diagnostics
            self.assertIn(str(config), diagnostic.diagnostic)
            self.assertIn("estimate_refresh_seconds='invalid'", diagnostic.diagnostic)

    def test_release_finishes_history_and_persists_without_configuration(self) -> None:
        with AdmissionFixture() as fixture:
            request = fixture.request()
            request = replace(request, sidecar=str(fixture.scope(request.owner)))
            granted = only_admitted(fixture.server.handle_request(request))
            fixture.scoped_peak(granted, 7 * GIB)
            progress.publish_counter(Path(request.sidecar), progress.ProgressCounter("build:1", 50, 100))
            _ = fixture.run_pass()
            self.assertTrue(fixture.server.progress_history.merged)
            write_memory_config(fixture.root / "build_admission.conf", replay_retention_seconds="invalid",
                                peak_history_share_percent="70")
            with mock.patch.object(fixture.server, "_log_request_timing"), \
                    mock.patch.object(conf, "read", side_effect=AssertionError("release read config")):
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest("release-bad-config", granted.reservation, 137)), [wire.Ok()])
                fixture.server.persist()
            self.assertNotIn(request.episode, fixture.server.reservations)
            self.assertFalse(service.read_state(fixture.state_dir).reservations)
            row, = fixture.history()
            self.assertEqual(row["anon_peak_bytes"], 7 * GIB)
            self.assertEqual(row["status"], 137)

    def test_restart_and_run_exit_preserve_step_arrivals_during_config_refusal(self) -> None:
        with AdmissionFixture() as fixture:
            run = fixture.run_owner()
            first = fixture.request(run=run)
            _ = fixture.finish_step(first, end_owner=True)
            fixture.free(0)
            fixture.hold_starts()
            fixture.clock.advance(1)
            peer = fixture.request()
            _ = fixture.server.handle_request(peer)
            fixture.clock.advance(11)
            child = fixture.request(run=run)
            _ = fixture.server.handle_request(child)
            config = fixture.root / "build_admission.conf"
            self.assertEqual(fixture.queue_pids(), [child.owner.pid, peer.owner.pid])
            saved = service.read_state(fixture.state_dir)
            valid_configuration = config.read_text()
            write_memory_config(config, estimate_refresh_seconds="invalid")
            fixture.restart()
            restored = service.read_state(fixture.state_dir)
            self.assertEqual(restored.places, saved.places)
            self.assertEqual(restored.run_places, saved.run_places)
            _ = fixture.server.handle_request(child)
            _ = fixture.server.handle_request(peer)
            self.assertEqual(fixture.server.waiters[child.episode].row["since"], child.since)
            self.assertEqual(fixture.server.waiters[peer.episode].row["since"], peer.since)
            fixture.end_run(run)
            self.assertFalse(service.read_state(fixture.state_dir).run_places)
            _ = config.write_text(valid_configuration)
            _ = fixture.server.run_pass()
            self.assertEqual(fixture.queue_pids(), [peer.owner.pid, child.owner.pid])
            fixture.free(60 * GIB)
            _ = fixture.server.run_pass()
            fixture.allow_next_start()
            granted = only_admitted(fixture.server.handle_request(child))
            fixture.restart()
            if child.episode not in fixture.server.reservations:
                fixture.allow_next_start()
            self.assertEqual(only_admitted(fixture.server.handle_request(child)), granted)
            self.assertEqual(len([record for record in fixture.server.reservations.values() if record.owner == child.owner]), 1)


class CiJobStartPlaceTests(unittest.TestCase):
    def ci_request(self, fixture: AdmissionFixture, job_start: records.CiJobStart) -> wire.ServiceEstimateRequest:
        return replace(fixture.service_request(), job_start=job_start)

    def test_a_ci_step_whose_job_started_before_an_older_step_ranks_first(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            now = fixture.clock.now()
            older = fixture.request(since=now - 600)
            ci = self.ci_request(fixture, records.JobStart(now - 900))
            for request in (older, ci):
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.queue_pids(), [ci.owner.pid, older.owner.pid])
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            rows = {row["pid"]: row for _, row in inputs.ordered}
            self.assertEqual(rows[ci.owner.pid].get("order_since"), now - 900)
            self.assertEqual(rows[ci.owner.pid]["since"], ci.since)

    def test_a_ci_step_with_no_job_start_keeps_its_arrival_place(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            now = fixture.clock.now()
            older = fixture.request(since=now - 600)
            ci = self.ci_request(fixture, records.NO_JOB_START)
            envelope = wire.parse_record(wire.encode_request(ci), dict[str, object])
            data = cast(dict[str, object], envelope["data"])
            self.assertEqual(data.pop("job_start"), {"kind": "NoJobStart"})
            legacy = wire.parse_request(wire.encode_record(envelope))
            assert isinstance(legacy, wire.ServiceEstimateRequest)
            self.assertEqual(legacy.job_start, records.NO_JOB_START)
            for request in (older, legacy):
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.queue_pids(), [older.owner.pid, ci.owner.pid])
            self.assertNotIn("ci_job_start", fixture.server.waiters[ci.episode].row)

    def test_two_steps_of_one_ci_job_both_rank_at_the_job_start(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            started = fixture.clock.now() - 1200
            first = self.ci_request(fixture, records.JobStart(started))
            fixture.clock.advance(300)
            between = fixture.request()
            fixture.clock.advance(300)
            second = self.ci_request(fixture, records.JobStart(started))
            for request in (between, second, first):
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.queue_pids(), [first.owner.pid, second.owner.pid, between.owner.pid])
            inputs = fixture.server.last_inputs
            assert isinstance(inputs, records.QueueInputs)
            rows = {row["pid"]: row for _, row in inputs.ordered}
            for request in (first, second):
                self.assertEqual(rows[request.owner.pid].get("order_since"), started)
                self.assertEqual(rows[request.owner.pid]["since"], request.since)
            self.assertEqual(rows[between.owner.pid].get("order_since"), between.since)

    def test_a_restart_keeps_the_job_start_place(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            now = fixture.clock.now()
            older = fixture.request(since=now - 600)
            ci = self.ci_request(fixture, records.JobStart(now - 900))
            for request in (older, ci):
                _ = fixture.server.handle_request(request)
            fixture.server.persist()
            saved = [place for place in service.read_state(fixture.state_dir).places.values() if place.owner == ci.owner]
            self.assertEqual([place.ci_job_start for place in saved], [records.JobStart(now - 900)])
            fixture.restart()
            ranked = fixture.server._ranked_waiters()  # pyright: ignore[reportPrivateUsage]
            self.assertEqual([row["pid"] for _, row in ranked], [ci.owner.pid, older.owner.pid])
            self.assertEqual(ranked[0][1].get("order_since"), now - 900)
            fixture.clock.advance(1)
            for request in (older, ci):
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.queue_pids(), [ci.owner.pid, older.owner.pid])

    def test_a_waiting_request_from_an_older_front_ranks_at_its_job_start_after_a_reload(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            now = fixture.clock.now()
            older = fixture.request(since=now - 600)
            ci = self.ci_request(fixture, records.NO_JOB_START)
            for request in (older, ci):
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.queue_pids(), [older.owner.pid, ci.owner.pid])
            fixture.server.persist()
            # The new service code reads the job start from the runner process of the request that waits.
            fixture.job_starts[ci.owner.pid] = records.JobStart(now - 900)
            fixture.restart()
            fixture.clock.advance(1)
            for request in (older, ci):
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.queue_pids(), [ci.owner.pid, older.owner.pid])
            self.assertEqual(fixture.server.waiters[ci.episode].row.get("ci_job_start"), records.JobStart(now - 900))
            self.assertEqual(fixture.server.waiters[ci.episode].row["since"], ci.since)

    def test_a_recovered_job_start_stays_when_a_later_read_finds_none(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            now = fixture.clock.now()
            older = fixture.request(since=now - 600)
            ci = self.ci_request(fixture, records.NO_JOB_START)
            fixture.job_starts[ci.owner.pid] = records.JobStart(now - 900)
            for request in (older, ci):
                _ = fixture.server.handle_request(request)
            fixture.server.persist()
            # A later read fails, and the older front sends NoJobStart again after a reload.
            fixture.job_starts.clear()
            fixture.restart()
            fixture.clock.advance(1)
            for request in (older, ci):
                _ = fixture.server.handle_request(request)
            self.assertEqual(fixture.queue_pids(), [ci.owner.pid, older.owner.pid])
            self.assertEqual(fixture.server.waiters[ci.episode].row.get("ci_job_start"), records.JobStart(now - 900))

    def test_the_service_reads_no_job_start_for_a_request_with_no_runner(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.free(0)
            fixture.hold_starts()
            local = replace(fixture.service_request(runner=""), job_start=records.NO_JOB_START)
            fixture.job_starts[local.owner.pid] = records.JobStart(fixture.clock.now() - 900)
            _ = fixture.server.handle_request(local)
            self.assertNotIn("ci_job_start", fixture.server.waiters[local.episode].row)


@dataclass(frozen=True)
class ScopedMemoryTrace:
    reader: service.OperatingSystemReader
    request: wire.AdmitRequest
    grant: wire.Admitted
    sidecar: Path
    scope: Path
    log: Path
    store: memory_trace.TraceStore


class MemoryTraceCaptureTests(unittest.TestCase):
    def start_trace_reader(self, fixture: AdmissionFixture,
                           policy: memory_trace.MemoryTracePolicy) -> service.OperatingSystemReader:
        server = fixture.server
        _ = server.run_pass()
        server.trace_policy = policy
        with mock.patch.object(threading.Thread, "start"):
            server.start_reader()
        reader = server.system_reader
        assert isinstance(reader, service.OperatingSystemReader)

        def close_server() -> None:
            with mock.patch.object(reader.thread, "join"):
                server.close()

        _ = fixture.callback(close_server)
        return reader

    def grant(self, fixture: AdmissionFixture, *, repository: str = "hana",
              step: str = "nextest", scoped: bool = True, need: int = 8 * GIB,
              argv: tuple[str, ...] = ("cargo", "nextest", "run", "--test-threads", "8"),
              policy: memory_trace.MemoryTracePolicy = TRACE_POLICY) -> ScopedMemoryTrace:
        request = fixture.request(step=step, need=need, width=records.RequestedTestWidth(8))
        sidecar = fixture.root / f"sample-{request.owner.pid}.cgroup"
        request = replace(request, sidecar=str(sidecar) if scoped else "",
                          argv=argv,
                          profile=replace(request.profile, repo=repository,
                              repository=records.RepositoryIdentity(f"git-root:{repository}", repository)))
        reader = self.start_trace_reader(fixture, policy)
        grant = only_admitted(fixture.server.handle_request(request))
        if scoped:
            _ = fixture.scope(request.owner, current=8 * GIB, anonymous=2 * GIB, swap=3 * GIB)
            stamp = round(fixture.clock.now() * 1_000_000_000)
            os.utime(sidecar, ns=(stamp, stamp))
        scope = fixture.root / f"scope-{request.owner.pid}"
        log = progress.progress_path(sidecar, "log")
        return ScopedMemoryTrace(reader, request, grant, sidecar, scope, log,
                                 memory_trace.TraceStore(fixture.state_dir / "memory_traces", policy))

    @staticmethod
    def trace_worker(run: ScopedMemoryTrace) -> memory_trace.TraceWorker:
        worker = run.reader.traces
        assert isinstance(worker, memory_trace.TraceWorker)
        return worker

    def drive(self, fixture: AdmissionFixture, run: ScopedMemoryTrace, finish_at: float,
              events: tuple[tuple[float, Callable[[], None]], ...]) -> None:
        reader = run.reader
        worker = self.trace_worker(run)
        start = fixture.clock.monotonic()
        end_at = start + finish_at
        scheduled = iter((start + elapsed, action) for elapsed, action in events)
        next_event = next(scheduled, None)
        waits = 0

        class Idle(Exception):
            pass

        def dispatch() -> None:
            with mock.patch.object(worker._ready, "wait", side_effect=Idle):  # pyright: ignore[reportPrivateUsage]
                try:
                    while worker._step():  # pyright: ignore[reportPrivateUsage]
                        pass
                except Idle:
                    pass

        def delay(seconds: float) -> bool:
            nonlocal next_event, waits
            full_at = fixture.clock.monotonic() + seconds
            while True:
                waits += 1
                self.assertLess(waits, 60)
                now = fixture.clock.monotonic()
                while next_event is not None and next_event[0] <= now:
                    _, action = next_event
                    next_event = next(scheduled, None)
                    action()
                if now >= end_at:
                    reader.stopped.set()
                    return False
                if reader.requested.is_set():
                    return True
                if now >= full_at:
                    return False
                dispatch()
                destination = min(full_at, worker.capture.deadline,
                                  next_event[0] if next_event is not None else end_at, end_at)
                fixture.clock.advance(max(0, destination - fixture.clock.monotonic()))

        worker.capture.recover()
        try:
            with mock.patch.object(reader.requested, "wait", side_effect=delay):
                reader.thread.run()
            dispatch()
        finally:
            worker.capture.close()

    @staticmethod
    def append(path: Path, content: str) -> None:
        with path.open("a") as output:
            _ = output.write(content)

    def sealed(self, run: ScopedMemoryTrace) -> memory_trace.MemoryTrace:
        path, = (run.store.root / "finished").glob(f"*/*-{run.grant.reservation}.jsonl")
        trace = memory_trace.read_trace(path, run.store.policy)
        assert isinstance(trace, memory_trace.MemoryTrace)
        self.assertIsInstance(trace.state, memory_trace.TraceEnd)
        return trace

    def test_non_nextest_scope_records_memory_until_release_without_a_log(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture, repository="fixture", step="lint", argv=("verify.sh", "lint", "fixture"))

            def increase_memory() -> None:
                write_scope(run.scope, members=(run.request.owner.pid,), current=8 * GIB, anonymous=5 * GIB)

            def release() -> None:
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "lint-release", run.grant.reservation, 0)), [wire.Ok()])

            with mock.patch.object(progress, "CaptureReader", side_effect=AssertionError("lint opened a progress log")):
                self.drive(fixture, run, 61, ((28, increase_memory), (60.5, release)))
            trace = self.sealed(run)
            shape = memory_trace.GeneralTraceShape(fixture.server.host, "git-root:fixture", "lint", run.request.argv)
            self.assertEqual(trace.header.general, shape)
            self.assertEqual(trace.header.shape_key, memory_trace.shape_key(shape))
            self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], list(range(0, 60001, 2000)))
            self.assertEqual([sample.program_bytes for sample in trace.compile_samples], [2 * GIB] * 14 + [5 * GIB] * 17)
            self.assertEqual(trace.test_samples, ())
            assert isinstance(trace.state, memory_trace.TraceEnd)
            self.assertEqual(trace.state.coverage, memory_trace.WholeRun())
            path, = (run.store.root / "finished").glob(f"*/*-{run.grant.reservation}.jsonl")
            print(f"General lint trace, 31 samples over 60 seconds: {path.stat().st_size} bytes")

    def test_placed_gate_records_one_trace_for_modules_in_separate_scopes(self) -> None:
        from scripts.build_queue.test_placement import PlacementFixture, local_candidate, placement_request

        for step in ("gate", "test-affected"):
            with self.subTest(step=step), PlacementFixture() as fixture:
                request = placement_request(fixture)
                assert isinstance(request.local, wire.LocalAdmissionContext)
                sidecar = fixture.root / "placement.cgroup"
                argv = (step,)
                repo = "/fixture/.claude"
                request = replace(request, profile=replace(request.profile, repo=repo, step=step),
                                  constraints=replace(request.constraints, command=argv,
                                                      candidates=(local_candidate(fixture),)),
                                  local=replace(request.local, argv=argv, sidecar=str(sidecar),
                                                trace_sampling=wire.ProcessTreeTraceSampling()))
                reader = self.start_trace_reader(fixture, TRACE_POLICY)
                reply, = fixture.connect(request).replies()
                assert isinstance(reply, wire.LocalPlacementGranted), reply
                record = fixture.server.reservations[request.episode]
                self.assertEqual(record.sidecar, str(sidecar))
                self.assertIsInstance(record.place, wire.LocalPlace)
                self.assertEqual(record.trace_sampling, wire.ProcessTreeTraceSampling())
                child_request = fixture.request(owner=wire.Owner(request.owner.pid, request.owner.start_time), step=step)
                trace_store = memory_trace.TraceStore(fixture.state_dir / "memory_traces", TRACE_POLICY)
                run = ScopedMemoryTrace(reader, child_request, reply.grant, sidecar,
                                        fixture.root / "unused-scope", sidecar.with_suffix(".log"), trace_store)

                def start_modules() -> None:
                    for module, used in enumerate((2 * GIB, 3 * GIB), start=1):
                        child = fixture.proc.spawn()
                        write_process(fixture.proc.path, child.pid, used, parent=request.owner.pid, start=child.start_time)
                        scope = fixture.root / f"module-{module}.scope"
                        write_scope(scope, members=(child.pid,), current=used, anonymous=used)
                    outsider = fixture.proc.spawn()
                    write_process(fixture.proc.path, outsider.pid, 4 * GIB, start=outsider.start_time)

                self.drive(fixture, run, 5, ((1, start_modules), (4.5, lambda: fixture.release(run.grant))))
                self.assertFalse(sidecar.exists())
                trace = self.sealed(run)
                self.assertEqual(trace.header.general, memory_trace.GeneralTraceShape(
                    fixture.server.host, request.profile.worktree, step, argv))
                self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [0, 2000, 4000])
                self.assertEqual([sample.program_bytes for sample in trace.compile_samples], [GIB, 6 * GIB, 6 * GIB])
                self.assertEqual(trace.test_samples, ())
                assert isinstance(trace.state, memory_trace.TraceEnd)
                self.assertEqual(trace.state.coverage, memory_trace.WholeRun())

    def test_queue_place_gate_without_trace_sampling_uses_the_published_scope(self) -> None:
        from scripts.build_queue import queue_place

        with AdmissionFixture() as fixture:
            write_memory_config(fixture.root / "build_admission.conf", **{"program_first_peak_gib.gate": "4"})
            owner = fixture.proc.spawn()
            reader = self.start_trace_reader(fixture, TRACE_POLICY)
            trace_store = memory_trace.TraceStore(fixture.state_dir / "memory_traces", TRACE_POLICY)
            scope = fixture.root / "gate.scope"

            def admit(request: queue_place.RefreshedRequest, _output: client.WaitOutput, *,
                      before_check: Callable[[], None]) -> wire.Admitted:
                before_check()
                submitted = request.refresh()
                self.assertEqual(submitted.argv, ("gate",))
                self.assertEqual(submitted.trace_sampling, wire.ScopeTraceSampling())
                encoded = wire.encode_request(submitted)
                envelope = cast(dict[str, object], json.loads(encoded))
                data = cast(dict[str, object], envelope["data"])
                self.assertNotIn("trace_sampling", data)
                return only_admitted(fixture.connect(encoded).replies())

            def start(place: queue_place.QueuePlace, _argv: Sequence[str], *,
                      pass_fds: tuple[int, ...]) -> object:
                self.assertEqual(pass_fds, ())
                self.assertIsInstance(place.reservation, queue_place.HeldReservation)
                assert isinstance(place.reservation, queue_place.HeldReservation)
                grant = place.reservation.grant
                record, = fixture.server.reservations.values()
                self.assertEqual(record.trace_sampling, wire.ScopeTraceSampling())
                child = fixture.proc.spawn()
                write_scope(scope, members=(child.pid,), current=4 * GIB, anonymous=4 * GIB)
                _ = place.sidecar.write_text(str(scope))
                stamp = round(fixture.clock.now() * 1_000_000_000)
                os.utime(place.sidecar, ns=(stamp, stamp))
                run = ScopedMemoryTrace(reader, fixture.request(owner=owner, step="gate"), grant,
                                        place.sidecar, scope, place.sidecar.with_suffix(".log"), trace_store)
                self.drive(fixture, run, 3, ((2.5, lambda: fixture.release(grant)),))
                self.assertEqual([sample.program_bytes for sample in self.sealed(run).compile_samples],
                                 [4 * GIB, 4 * GIB])
                return mock.Mock(wait=lambda: 0)

            with (mock.patch.object(os, "getpid", return_value=owner.pid),
                  mock.patch.object(records, "process_start_time", return_value=owner.start_time),
                  mock.patch.object(queue_place, "request_place", side_effect=admit),
                  mock.patch.object(queue_place, "release_place", return_value=wire.Ok()),
                  mock.patch.object(queue_place.QueuePlace, "popen", new=start)):
                self.assertEqual(queue_place.main(["--", "gate"]), 0)

    def test_failed_process_table_read_is_shared_for_one_trace_round(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture, repository="fixture", step="lint")
            worker = self.trace_worker(run)
            scoped, = worker._commands  # pyright: ignore[reportPrivateUsage]
            assert isinstance(scoped, memory_trace.TraceGrant)
            capture = worker.capture
            capture.process(scoped)
            with (redirect_stderr(io.StringIO()) as errors,
                  mock.patch.object(gate, "process_table", side_effect=OSError("fixture process read failed")) as read):
                capture.sample_due(lambda _identifier: float("inf"), lambda _release: None)
                read.assert_not_called()
                for identifier in ("tree-one", "tree-two"):
                    capture.process(replace(scoped, reservation_id=identifier,
                                           trace_sampling=wire.ProcessTreeTraceSampling()))
                fixture.clock.advance(TRACE_POLICY.sample_seconds)
                capture.sample_due(lambda _identifier: float("inf"), lambda _release: None)
                read.assert_called_once_with(fixture.proc.path)
                self.assertIn("bind tree-one", errors.getvalue())
                self.assertIn("bind tree-two", errors.getvalue())
                fixture.clock.advance(TRACE_POLICY.sample_seconds)
                capture.sample_due(lambda _identifier: float("inf"), lambda _release: None)
                self.assertEqual(read.call_count, 2)
            capture.close()

    def test_general_trace_binds_a_scope_published_after_the_first_sample(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture, repository="fixture", step="lint")
            run.sidecar.unlink()
            published_at = fixture.clock.now() + 3

            def publish_scope() -> None:
                _ = run.sidecar.write_text(str(run.scope))
                stamp = round(fixture.clock.now() * 1_000_000_000)
                os.utime(run.sidecar, ns=(stamp, stamp))

            self.drive(fixture, run, 7, ((3, publish_scope), (6.5, lambda: fixture.release(run.grant))))
            trace = self.sealed(run)
            self.assertEqual(trace.header.scope_started_at_ms, round(published_at * 1000))
            self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [1000, 3000])
            self.assertIsNotNone(trace.header.general)

    def test_zero_need_scoped_steps_record_only_general_traces(self) -> None:
        for repository, step in (("fixture", "lint"), ("hana", "nextest")):
            with self.subTest(step=step), AdmissionFixture() as fixture:
                run = self.grant(fixture, repository=repository, step=step, need=0)
                self.drive(fixture, run, 3, ((2.5, lambda: fixture.release(run.grant)),))
                if step == "lint":
                    trace = self.sealed(run)
                    self.assertEqual(trace.header.compile_reserve_bytes, 0)
                    self.assertIsNotNone(trace.header.general)
                    self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [0, 2000])
                else:
                    self.assertEqual(list(run.store.root.glob("*/*/*.jsonl")), [])
                    self.assertIsInstance(run.store.read_live(run.grant.reservation), memory_trace.TraceUnavailable)

    def test_full_read_publishes_while_release_seal_is_blocked(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            worker = self.trace_worker(run)
            created, sealing, resume, published = (threading.Event() for _ in range(4))
            start = memory_trace.TraceStore.start
            seal = memory_trace.TraceWriter.seal
            write_event = os.eventfd_write

            def remember(store: memory_trace.TraceStore, header: memory_trace.TraceHeader) -> memory_trace.TraceWriter:
                writer = start(store, header)
                created.set()
                return writer

            def slow_seal(writer: memory_trace.TraceWriter, end: memory_trace.TraceEnd) -> memory_trace.TraceEnd:
                sealing.set()
                self.assertTrue(resume.wait(timeout=5))
                return seal(writer, end)

            def notify(fd: int, value: int) -> None:
                write_event(fd, value)
                if fd == fixture.server.reading_events:
                    published.set()

            with (mock.patch.object(memory_trace.TraceStore, "start", autospec=True, side_effect=remember),
                  mock.patch.object(memory_trace.TraceWriter, "seal", autospec=True, side_effect=slow_seal),
                  mock.patch.object(os, "eventfd_write", side_effect=notify)):
                worker.thread.start()
                try:
                    self.assertTrue(created.wait(timeout=3))
                    self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                        "blocked-seal", run.grant.reservation, 0)), [wire.Ok()])
                    self.assertTrue(sealing.wait(timeout=3))
                    initial = run.reader.latest
                    run.reader.thread.start()
                    run.reader.request_read()
                    self.assertTrue(published.wait(timeout=3))
                    self.assertIsNot(run.reader.latest, initial)
                finally:
                    resume.set()
                    if run.reader.thread.ident is None:
                        worker.close()
                    else:
                        run.reader.close()

    def test_recovery_error_keeps_the_worker_and_the_admission_reader_running(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            worker = self.trace_worker(run)
            published, captured = threading.Event(), threading.Event()
            initial = run.reader.latest
            start = memory_trace.TraceStore.start

            def notify(fd: int, _value: int) -> None:
                if fd == fixture.server.reading_events:
                    published.set()

            def remember(store: memory_trace.TraceStore, header: memory_trace.TraceHeader) -> memory_trace.TraceWriter:
                writer = start(store, header)
                captured.set()
                return writer

            with (mock.patch.object(memory_trace.TraceStore, "recover_abandoned", side_effect=OverflowError("recovery")),
                  mock.patch.object(memory_trace.TraceStore, "start", autospec=True, side_effect=remember),
                  mock.patch.object(os, "eventfd_write", side_effect=notify)):
                worker.thread.start()
                run.reader.thread.start()
                try:
                    run.reader.request_read()
                    self.assertTrue(published.wait(timeout=3))
                    run.reader.raise_failure()
                    self.assertIsNot(run.reader.latest, initial)
                    self.assertTrue(captured.wait(timeout=3))
                    self.assertTrue(worker.thread.is_alive())
                finally:
                    run.reader.close()

    def test_narrow_ticks_leave_the_full_cadence_and_admission_result_intact(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            server = fixture.server
            record = server.reservations[run.request.episode]
            need = records.TestPhaseNeed(2 * GIB, 1, 8, 8, "same_width", 1.5)
            server.reservations[run.request.episode] = replace(record, test_phase_need=need)
            run.reader.update(replace(run.reader.plan, config=replace(run.reader.plan.config, running_pass_seconds=4),
                                      reservations=(server.reservations[run.request.episode],)))
            initial_result = server.last_result
            initial_epoch = fixture.clock.now()
            initial_monotonic = fixture.clock.monotonic()
            full_times: list[float] = []
            _ = run.log.write_text("\r\x1b[32m Compiling hana v0.1.0\x1b[0m\rBuilding [=>] 8/9\r")
            _ = (run.scope / "memory.stat").write_text(
                f"anon {2 * GIB}\nshmem {GIB}\ninactive_file {4 * GIB}\nfile {5 * GIB}\n")

            def start_tests() -> None:
                self.assertIs(server.last_result, initial_result)
                self.append(run.log, "    Nextest run ID edd0\n    Starting 20 tests\n")

            def publish_phase() -> None:
                phase = Path(record.sidecar).with_suffix(".test-phase.json")
                _ = phase.write_text(json.dumps({"test_phase_peak_bytes": 3 * GIB,
                    "test_phase_started_at": initial_epoch + 3.5, "repo_identity": record.repo_identity,
                    "test_scope": "full", "package": "", "filters": [], "call_id": "", "test_threads": 8,
                    "reservation_id": record.reservation_id}))

            def release() -> None:
                self.append(run.log, "    Summary [   2.97s] 20 tests run: 19 passed, 1 failed")
                self.assertEqual(server.handle_request(wire.ReleaseRequest("trace-release", run.grant.reservation, 100)),
                                 [wire.Ok()])
                run.log.unlink(missing_ok=True)
                run.sidecar.unlink(missing_ok=True)

            read_system = service.read_system

            def timed_full_read(plan: service.SystemReadingPlan, readings: service.Readings,
                                proc: service.Proc, clock: service.Clock) -> service.SystemEvidence:
                full_times.append(clock.monotonic() - initial_monotonic)
                return read_system(plan, readings, proc, clock)

            read_scope = gate.slice_working_set
            scope_reads: list[Path] = []

            def timed_scope_read(directory: Path) -> records.MeasuredMemory:
                scope_reads.append(directory.resolve())
                return read_scope(directory)

            with mock.patch.object(service, "read_system", side_effect=timed_full_read), \
                 mock.patch.object(gate, "slice_working_set", side_effect=timed_scope_read), \
                 mock.patch.object(gate, "admission_pass", side_effect=AssertionError("trace tick ran admission")):
                self.drive(fixture, run, 7.5, ((4, start_tests), (6, publish_phase), (7, release)))
            self.assertEqual(full_times, [0, 4])
            self.assertEqual(scope_reads.count(run.scope), 6)
            self.assertEqual(os.eventfd_read(server.reading_events), 2)
            self.assertIs(server.last_result, initial_result)
            trace = self.sealed(run)
            self.assertEqual(trace.compile_samples, tuple(
                memory_trace.CompileSample(elapsed, 3 * GIB, memory_trace.CargoProgress(8, 9), "hana")
                for elapsed in (0, 2000)))
            self.assertEqual(trace.test_start, memory_trace.TestStarted(4000, memory_trace.TestReserveUnavailable()))
            self.assertEqual(trace.test_samples, tuple(memory_trace.TestSample(elapsed, 3 * GIB)
                                                      for elapsed in (4000, 6000)))
            self.assertEqual(trace.state, memory_trace.TraceEnd(round((initial_epoch + 7) * 1000),
                                                               7000, 100, memory_trace.WholeRun()))
            shape = memory_trace.HanaTraceShape(server.host, record.repo_identity, run.request.argv,
                                                records.RequestedTestWidth(8))
            self.assertEqual(trace.header.shape_key, memory_trace.shape_key(shape))
            self.assertEqual(trace.header.compile_reserve_bytes, run.grant.need)

    def test_first_sample_reads_the_scope_that_exists_when_the_worker_binds(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            record = fixture.server.reservations[run.request.episode]
            run.reader.update(replace(run.reader.plan, reservations=(record,)))
            run.reader.request_read()
            read_system = service.read_system

            def read_then_replace_scope(plan: service.SystemReadingPlan, readings: service.Readings,
                                        proc: service.Proc, clock: service.Clock) -> service.SystemEvidence:
                evidence = read_system(plan, readings, proc, clock)
                _ = run.scope.rename(fixture.root / "old-scope")
                write_scope(run.scope, members=(run.request.owner.pid,), current=9 * GIB, anonymous=9 * GIB)
                return evidence

            with mock.patch.object(service, "read_system", side_effect=read_then_replace_scope):
                self.drive(fixture, run, 1, ())
            trace = run.store.read_live(run.grant.reservation)
            assert isinstance(trace, memory_trace.MemoryTrace)
            self.assertEqual([sample.program_bytes for sample in trace.compile_samples], [9 * GIB])

    def test_trace_keeps_only_the_first_test_reserve_from_an_admission_pass(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            server = fixture.server
            record = server.reservations[run.request.episode]
            need = records.TestPhaseNeed(2 * GIB, 1, 8, 8, "same_width", 1.5)
            updated = replace(record, test_phase_need=need)
            server.reservations[run.request.episode] = updated
            run.reader.update(replace(run.reader.plan, reservations=(updated,)))
            began = fixture.clock.now()

            def write_phase(peak: int) -> None:
                _ = Path(record.sidecar).with_suffix(".test-phase.json").write_text(json.dumps({
                    "test_phase_peak_bytes": peak * GIB, "test_phase_started_at": began + 2,
                    "repo_identity": record.repo_identity, "test_scope": "full", "package": "",
                    "filters": [], "call_id": "", "test_threads": 8, "reservation_id": record.reservation_id,
                }))

            def before_decision() -> None:
                trace = run.store.read_live(run.grant.reservation)
                assert isinstance(trace, memory_trace.MemoryTrace)
                self.assertEqual(trace.test_start, memory_trace.TestStarted(2000, memory_trace.TestReserveUnavailable()))
                write_phase(6)
                run.reader.request_read()

            def decide(need_bytes: int) -> None:
                _ = server.run_pass()
                published = server.published
                assert isinstance(published, service.PublishedReplies)
                running = published.view.reading.explanation.running
                entry = next(entry for entry in running if entry.step.pid == run.request.owner.pid)
                self.assertEqual(entry.promise, need_bytes)
                for _ in range(run.store.policy.max_pending_commands + 1):
                    run.reader.record_trace_reserves(published.view.reading.explanation)

            def later_read() -> None:
                write_phase(8)
                run.reader.request_read()

            self.drive(fixture, run, 7, ((2, lambda: write_phase(2)), (3, before_decision),
                (3.5, lambda: decide(9 * GIB)), (4.5, later_read), (5, lambda: decide(12 * GIB))))
            trace = run.store.read_live(run.grant.reservation)
            assert isinstance(trace, memory_trace.MemoryTrace)
            self.assertEqual(trace.test_start, memory_trace.TestStarted(2000, 9 * GIB))

    def test_trace_worker_reports_queue_saturation(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture, policy=replace(TRACE_POLICY, max_pending_commands=2))
            worker = self.trace_worker(run)
            record = fixture.server.reservations[run.request.episode]
            shape = memory_trace.HanaTraceShape(fixture.server.host, record.repo_identity, run.request.argv,
                                                records.RequestedTestWidth(8))
            worker.submit(memory_trace.TraceGrant("excess", shape, service.reservation_row(record),
                                                  fixture.clock.now(), fixture.clock.monotonic()))

            def release() -> None:
                for _ in range(3):
                    worker.release(run.grant.reservation, fixture.clock.now(), 0)
                command = worker._commands[-1]  # pyright: ignore[reportPrivateUsage]
                assert isinstance(command, memory_trace.TraceRelease)
                self.assertIsInstance(command.test_reserve, memory_trace.TestReserveUnavailable)

            error_output = io.StringIO()
            with mock.patch.object(sys, "stderr", error_output):
                self.drive(fixture, run, 2, ((1, release),))
            self.assertIn("command queue is full", error_output.getvalue())
            self.assertEqual([sample.elapsed_ms for sample in self.sealed(run).compile_samples], [0])

    def test_general_trace_commands_leave_capacity_for_hana(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture, repository="fixture", step="lint")
            worker = self.trace_worker(run)
            commands = worker._commands  # pyright: ignore[reportPrivateUsage]
            general, = commands
            assert isinstance(general, memory_trace.TraceGrant)
            worker.submit(replace(general, reservation_id="general-2"))
            worker.submit(replace(general, reservation_id="general-3"))
            worker.submit(replace(general, reservation_id="general-4"))
            worker.release("general-2", fixture.clock.now(), 0)
            worker.submit(replace(general, reservation_id="general-5"))
            hana = memory_trace.HanaTraceShape("natedev", "hana", ("cargo", "nextest"), records.RequestedTestWidth(8))
            worker.submit(replace(general, reservation_id="hana", shape=hana))
            self.assertEqual([entry.reservation_id for entry in commands if isinstance(entry, memory_trace.TraceGrant)],
                             [run.grant.reservation, "general-2", "hana"])

    def test_general_trace_share_at_64_command_slots(self) -> None:
        for share, expected in ((50, 16), (25, 8)):
            with self.subTest(share=share), AdmissionFixture() as fixture:
                policy = replace(TRACE_POLICY, max_pending_commands=64, general_trace_share_percent=share)
                run = self.grant(fixture, repository="fixture", step="lint", policy=policy)
                worker = self.trace_worker(run)
                commands = worker._commands  # pyright: ignore[reportPrivateUsage]
                general, = commands
                assert isinstance(general, memory_trace.TraceGrant)
                for number in range(1, 33):
                    worker.submit(replace(general, reservation_id=f"general-{number}"))
                self.assertEqual(len(commands), expected)

    def test_full_queue_keeps_the_first_test_reserve(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture, policy=replace(TRACE_POLICY, max_pending_commands=2))
            worker = self.trace_worker(run)
            identifier = run.grant.reservation
            worker.submit({identifier: 9 * GIB})

            def later_reserve() -> None:
                self.append(run.log, "    Starting 1 test\n")
                worker.submit({identifier: 12 * GIB})

            self.drive(fixture, run, 3, ((2, later_reserve),))
            live = run.store.read_live(identifier)
            assert isinstance(live, memory_trace.MemoryTrace)
            self.assertEqual(live.test_start, memory_trace.TestStarted(2000, 9 * GIB))

    def test_expired_scope_releases_its_queue_capacity(self) -> None:
        with AdmissionFixture() as fixture:
            policy = replace(TRACE_POLICY, max_pending_commands=2, scope_loss_grace_seconds=1)
            run = self.grant(fixture, policy=policy)
            record = fixture.server.reservations[run.request.episode]
            shape = memory_trace.HanaTraceShape(fixture.server.host, record.repo_identity, run.request.argv,
                                                records.RequestedTestWidth(8))

            def remove_scope() -> None:
                _ = run.scope.rename(fixture.root / "old-scope")

            def submit_next() -> None:
                write_scope(run.scope, members=(run.request.owner.pid,), current=2 * GIB, anonymous=2 * GIB)
                stamp = round(fixture.clock.now() * 1_000_000_000)
                os.utime(run.sidecar, ns=(stamp, stamp))
                self.trace_worker(run).submit(memory_trace.TraceGrant(
                    "replacement", shape, service.reservation_row(record), fixture.clock.now(), fixture.clock.monotonic()))

            self.drive(fixture, run, 5, ((2, remove_scope), (4, submit_next)))
            _ = self.sealed(run)
            self.assertIsInstance(run.store.read_live("replacement"), memory_trace.MemoryTrace)

    def test_scope_expiry_seals_the_reserve_accepted_during_its_tick(self) -> None:
        for queued_release in (False, True):
            with self.subTest(queued_release=queued_release), AdmissionFixture() as fixture:
                run = self.grant(fixture, policy=replace(TRACE_POLICY, scope_loss_grace_seconds=1))
                worker = self.trace_worker(run)
                sample_due = worker.capture.sample_due
                expiry = fixture.clock.monotonic() + 5

                def submit_reserve(release_time: Callable[[str], float],
                                   claim_expiry: Callable[[memory_trace.TraceRelease], memory_trace.TraceRelease | None]
                                   ) -> None:
                    if fixture.clock.monotonic() == expiry:
                        worker.submit({run.grant.reservation: 9 * GIB})
                        if queued_release:
                            worker.release(run.grant.reservation, fixture.clock.now(), 0)
                    sample_due(release_time, claim_expiry)

                def remove_scope() -> None:
                    _ = run.scope.rename(fixture.root / "old-scope")

                with mock.patch.object(worker.capture, "sample_due", side_effect=submit_reserve):
                    self.drive(fixture, run, 6, ((2, lambda: self.append(run.log, "    Starting 1 test\n")),
                                                 (4, remove_scope)))
                trace = self.sealed(run)
                self.assertEqual(trace.test_start, memory_trace.TestStarted(2000, 9 * GIB))
                end = trace.state
                assert isinstance(end, memory_trace.TraceEnd)
                self.assertEqual(end.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.SCOPE_LOST))

    def test_worker_shutdown_uses_the_policy_timeout(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            worker = self.trace_worker(run)
            worker.thread.start()
            with mock.patch.object(worker.thread, "join", wraps=worker.thread.join) as join:
                worker.close()
            join.assert_called_once_with(timeout=run.store.policy.shutdown_timeout_seconds)

    def test_worker_shutdown_retries_a_pending_seal(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            worker = self.trace_worker(run)
            live_path = run.store.root / "live" / f"{run.grant.reservation}.jsonl"
            move = Path.replace
            attempts: list[Path] = []

            def fail_first_move(path: Path, destination: Path) -> Path:
                if path == live_path:
                    attempts.append(destination)
                    if len(attempts) == 1:
                        raise OSError("first trace move")
                return move(path, destination)

            def release() -> None:
                self.append(run.log, "    Summary [  1.0s] 1 test run: 1 passed\n")
                worker.release(run.grant.reservation, fixture.clock.now(), 0)

            def shutdown() -> None:
                self.assertEqual(len(attempts), 1)
                with mock.patch.object(worker.capture, "recover"):
                    worker.thread.start()
                    worker.close()
                self.assertFalse(worker.thread.is_alive())

            with mock.patch.object(Path, "replace", autospec=True, side_effect=fail_first_move):
                self.drive(fixture, run, 3.5, ((2, lambda: self.append(run.log, "    Starting 1 test\n")),
                                               (3, release), (3.5, shutdown)))
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0], attempts[1])
            end = self.sealed(run).state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual((end.elapsed_ms, end.coverage), (3000, memory_trace.WholeRun()))

    def test_release_seals_the_minimum_wire_status(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)

            def release() -> None:
                self.append(run.log, "    Summary [  1.0s] 1 test run: 1 passed\n")
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "minimum-status", run.grant.reservation, status_wire.CLEANUP_STATUS_MIN)), [wire.Ok()])

            self.drive(fixture, run, 4, ((2, lambda: self.append(run.log, "    Starting 1 test\n")), (3, release)))
            end = self.sealed(run).state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual(end.exit_status, status_wire.CLEANUP_STATUS_MIN)
            self.assertEqual(end.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.NEXTTEST_STATUS))

    def test_release_drops_a_sample_read_after_the_release_time(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            _ = run.log.write_text("Compiling hana v0.1.0\rBuilding [=>] 8/9\r")
            read_scope = gate.slice_working_set
            start = fixture.clock.monotonic()
            release_time = 4.01
            record = fixture.server.reservations[run.request.episode]

            def read(directory: Path) -> records.MeasuredMemory:
                if directory.resolve() == run.scope and fixture.clock.monotonic() == start + 4:
                    fixture.clock.advance(release_time - 4)
                    self.append(run.log, "    Summary [  2.01s] 1 test run: 1 passed")
                    ending = service.ReleaseStatus("sample-after-release", 0)
                    run.reader.release_trace(record, ending, fixture.clock.now())
                    fixture.clock.advance(8 - release_time)
                return read_scope(directory)

            with mock.patch.object(gate, "slice_working_set", side_effect=read):
                self.drive(fixture, run, 9, ((2, lambda: self.append(run.log, "    Starting 1 test\n")),))
            trace = self.sealed(run)
            self.assertEqual([sample.elapsed_ms for sample in trace.test_samples], [2000])
            end = trace.state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual((end.elapsed_ms, end.exit_status, end.coverage), (4010, 0, memory_trace.WholeRun()))

    def test_summary_after_the_memory_read_keeps_the_test_sample(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            _ = run.log.write_text("Building [=>] 9/10\r")
            read = gate.slice_working_set
            start = fixture.clock.monotonic()

            def read_then_finish(directory: Path) -> records.MeasuredMemory:
                memory = read(directory)
                if directory.resolve() == run.scope and fixture.clock.monotonic() == start + 4:
                    self.append(run.log, "    Summary [  1.0s] 1 test run: 1 passed\n")
                return memory

            def release() -> None:
                _ = run.scope.rename(fixture.root / "old-scope")
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "summary-after-read", run.grant.reservation, 0)), [wire.Ok()])

            with mock.patch.object(gate, "slice_working_set", side_effect=read_then_finish):
                self.drive(fixture, run, 5, ((3, lambda: self.append(run.log, "    Starting 1 test\n")), (4.01, release)))
            trace = self.sealed(run)
            self.assertEqual(trace.test_samples, (memory_trace.TestSample(4000, 2 * GIB),))
            end = trace.state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual(end.coverage, memory_trace.WholeRun())

    def test_sample_append_failure_keeps_the_prefix_for_release(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            append = memory_trace.TraceWriter.append

            def fail_second_sample(writer: memory_trace.TraceWriter, observation: memory_trace.TraceObservation
                                   ) -> memory_trace.TraceRecording | memory_trace.TraceLimit:
                if isinstance(observation, memory_trace.CompileSample) and observation.elapsed_ms == 2000:
                    raise PermissionError("sample append")
                return append(writer, observation)

            def release() -> None:
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "failed-sample-append", run.grant.reservation, 0)), [wire.Ok()])

            with mock.patch.object(memory_trace.TraceWriter, "append", autospec=True, side_effect=fail_second_sample):
                self.drive(fixture, run, 4, ((3, release),))
            trace = self.sealed(run)
            self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [0])
            self.assertFalse((run.store.root / "live" / f"{run.grant.reservation}.jsonl").exists())
            end = trace.state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual(end.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.RECORDING_ERROR))

    def test_reserve_append_failure_keeps_the_prefix_for_release(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            _ = run.log.write_text("Building [=>] 9/10\r")
            writer_append = memory_trace.TraceWriter.append

            def append(writer: memory_trace.TraceWriter, observation: memory_trace.TraceObservation
                       ) -> memory_trace.TraceRecording | memory_trace.TraceLimit:
                if isinstance(observation, memory_trace.TestReserveObserved):
                    raise OSError("reserve append")
                return writer_append(writer, observation)

            def release() -> None:
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "reserve-append-failure", run.grant.reservation, 0)), [wire.Ok()])

            def reserve() -> None:
                self.trace_worker(run).submit({run.grant.reservation: 3 * GIB})

            with mock.patch.object(memory_trace.TraceWriter, "append", autospec=True, side_effect=append):
                self.drive(fixture, run, 4, ((2, lambda: self.append(run.log, "    Starting 1 test\n")),
                                             (2.5, reserve), (3, release)))
            trace = self.sealed(run)
            self.assertEqual(trace.test_samples, (memory_trace.TestSample(2000, 2 * GIB),))
            end = trace.state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual(end.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.RECORDING_ERROR))

    def test_final_log_error_keeps_the_prefix_for_release(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            _ = run.log.write_text("Building [=>] 9/10\r")

            def release() -> None:
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "final-log-error", run.grant.reservation, 0)), [wire.Ok()])

            with mock.patch.object(progress.CaptureReader, "trace_tail", side_effect=PermissionError("final log")):
                self.drive(fixture, run, 4, ((3, release),))
            trace = self.sealed(run)
            self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [0, 2000])
            end = trace.state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual(end.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.RECORDING_ERROR))

    def test_release_reads_the_summary_after_a_full_log_chunk(self) -> None:
        for budget, coverage in ((131072, memory_trace.WholeRun()),
                                 (65536, memory_trace.IncompleteRun(memory_trace.IncompleteReason.NO_NEXTTEST_COMPLETION))):
            with self.subTest(budget=budget), AdmissionFixture() as fixture:
                run = self.grant(fixture, policy=replace(TRACE_POLICY, final_replay_bytes=budget))
                _ = run.log.write_text("Building [=>] 9/10\r")

                def release() -> None:
                    self.append(run.log, "test output\n" * 6000 + "    Summary [  1.0s] 1 test run: 1 passed")
                    run.log.unlink()
                    self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                        "final-log-replay", run.grant.reservation, 0)), [wire.Ok()])

                self.drive(fixture, run, 4, ((2, lambda: self.append(run.log, "    Starting 1 test\n")), (3, release)))
                end = self.sealed(run).state
                assert isinstance(end, memory_trace.TraceEnd)
                self.assertEqual(end.coverage, coverage)

    def test_release_with_invalid_wall_time_seals_the_prefix(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            worker = self.trace_worker(run)
            self.drive(fixture, run, 4, ((3, lambda: worker.release(run.grant.reservation, float("inf"), 0)),))
            trace = self.sealed(run)
            end = trace.state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual(end.ended_at_ms, trace.header.scope_started_at_ms + 3000)
            self.assertEqual(end.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.RECORDING_ERROR))

    def test_final_append_that_never_recovers_seals_the_prefix(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture, policy=replace(TRACE_POLICY, seal_retry_count=1))
            _ = run.log.write_text("Building [=>] 9/10\r")
            writer_append = memory_trace.TraceWriter.append
            attempts = 0

            def fail_phase(writer: memory_trace.TraceWriter, observation: memory_trace.TraceObservation
                           ) -> memory_trace.TraceRecording | memory_trace.TraceLimit:
                nonlocal attempts
                if isinstance(observation, memory_trace.TestStarted):
                    attempts += 1
                    raise OSError("final phase write")
                return writer_append(writer, observation)

            def release() -> None:
                self.append(run.log, "    Starting 1 test\n")
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "failed-final-phase", run.grant.reservation, 0)), [wire.Ok()])

            with mock.patch.object(memory_trace.TraceWriter, "append", autospec=True, side_effect=fail_phase):
                self.drive(fixture, run, 5, ((1.5, release),))
            self.assertEqual(attempts, 2)
            trace = self.sealed(run)
            self.assertIsInstance(trace.test_start, memory_trace.TestNotStarted)
            self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [0])
            end = trace.state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual(end.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.RECORDING_ERROR))

    def test_missing_process_identity_does_not_hide_other_terminal_evidence(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            worker = self.trace_worker(run)
            worker.capture.boot_id = "fixture"
            owner = run.request.owner.pid
            other = owner + 1
            _ = (run.scope / "cgroup.procs").write_text(f"{owner}\n{other}\n")

            def start_time(pid: int) -> str:
                if pid == owner:
                    raise PermissionError("process identity")
                self.assertEqual(pid, other)
                return "17"

            worker.capture.process_start = start_time
            with mock.patch.object(progress.CaptureReader, "trace_terminal", return_value=progress.NoTraceCapture()) as terminal:
                self.drive(fixture, run, 1, ())
            terminal.assert_called_once_with({other: "17"}, "fixture")
            trace = run.store.read_live(run.grant.reservation)
            assert isinstance(trace, memory_trace.MemoryTrace)
            self.assertEqual([sample.program_bytes for sample in trace.compile_samples], [2 * GIB])

    def test_phase_sidecar_must_match_the_reservation_and_sample_time(self) -> None:
        for fault in ("stale", "future", "other reservation"):
            with self.subTest(fault=fault), AdmissionFixture() as fixture:
                run = self.grant(fixture)
                record = fixture.server.reservations[run.request.episode]
                began = fixture.clock.now()
                when = {"stale": began - 1, "future": began + 4, "other reservation": began + 1}[fault]
                phase: dict[str, object] = {
                    "test_phase_peak_bytes": 3 * GIB, "test_phase_started_at": when,
                    "repo_identity": record.repo_identity, "test_scope": "full", "package": "",
                    "filters": [], "call_id": "", "test_threads": 8,
                    "reservation_id": "other" if fault == "other reservation" else run.grant.reservation,
                }
                _ = run.sidecar.with_suffix(".test-phase.json").write_text(json.dumps(phase))
                self.drive(fixture, run, 3, ())
                trace = run.store.read_live(run.grant.reservation)
                assert isinstance(trace, memory_trace.MemoryTrace)
                self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [0, 2000])
                self.assertIsInstance(trace.test_start, memory_trace.TestNotStarted)

    def assert_phase_does_not_start_tests(self, *, phase_offset: float, include_reservation: bool = True,
                                         scope_offset: float = 0.9996, modification_offset: float | None = None) -> None:
        with AdmissionFixture() as fixture:
            second = int(fixture.clock.now())
            fixture.clock.advance(scope_offset)
            run = self.grant(fixture)
            phase = buildlog_record.test_phase_context(wire.encode_record(run.grant.sampling_context), list(run.request.argv))
            if not include_reservation:
                del phase["reservation_id"]
            phase.update(test_phase_peak_bytes=3 * GIB, test_phase_started_at=second + phase_offset)
            path = run.sidecar.with_suffix(".test-phase.json")
            _ = path.write_text(json.dumps(phase))
            if modification_offset is not None:
                stamp = round((second + modification_offset) * 1_000_000_000)
                os.utime(path, ns=(stamp, stamp))

            self.drive(fixture, run, 1, ())
            trace = run.store.read_live(run.grant.reservation)
            assert isinstance(trace, memory_trace.MemoryTrace)
            self.assertIsInstance(trace.test_start, memory_trace.TestNotStarted)
            self.assertEqual(trace.test_samples, ())
            self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [0])

    def test_precise_phase_start_before_scope_creation_is_stale(self) -> None:
        self.assert_phase_does_not_start_tests(phase_offset=0.5)

    def test_phase_file_written_before_scope_creation_is_stale(self) -> None:
        self.assert_phase_does_not_start_tests(phase_offset=0, modification_offset=0.5)

    def test_phase_start_after_observation_is_rejected_before_rounding(self) -> None:
        self.assert_phase_does_not_start_tests(phase_offset=0.0004, scope_offset=0)

    def test_phase_without_a_reservation_id_is_ignored(self) -> None:
        self.assert_phase_does_not_start_tests(phase_offset=0, include_reservation=False)

    def test_whole_second_phase_start_matches_scope_created_in_the_same_second(self) -> None:
        with AdmissionFixture() as fixture:
            fixture.clock.advance(0.9996)
            run = self.grant(fixture)
            phase = buildlog_record.test_phase_context(wire.encode_record(run.grant.sampling_context), list(run.request.argv))
            self.assertEqual(phase["reservation_id"], run.grant.reservation)
            phase.update(test_phase_peak_bytes=3 * GIB, test_phase_started_at=int(fixture.clock.now()))
            _ = run.sidecar.with_suffix(".test-phase.json").write_text(json.dumps(phase))

            self.drive(fixture, run, 1, ())
            trace = run.store.read_live(run.grant.reservation)
            assert isinstance(trace, memory_trace.MemoryTrace)
            self.assertEqual(trace.test_start, memory_trace.TestStarted(0, memory_trace.TestReserveUnavailable()))
            self.assertEqual(trace.test_samples, (memory_trace.TestSample(0, 2 * GIB),))
            self.assertEqual(trace.compile_samples, ())

    def test_warm_run_records_test_memory_from_a_marker_written_before_bind(self) -> None:
        for terminal in (False, True):
            with self.subTest(terminal=terminal), AdmissionFixture() as fixture:
                run = self.grant(fixture)
                path = run.log
                if terminal:
                    capture = self.trace_worker(run).capture
                    capture.capture_root = fixture.root / "captures"
                    capture.boot_id = "fixture"
                    owner = run.request.owner
                    capture.process_start = fixture.proc.starts.get
                    account = capture.capture_root / "1000"
                    registration = account / f"state/pids/{owner.pid}.run"
                    registration.parent.mkdir(parents=True)
                    _ = registration.write_bytes(
                        f"cargo-tile-v3\0run\0fixture\0{owner.start_time}\0cargo.log\0folder\0".encode())
                    path = account / "cargo.log"
                fixture.clock.advance(1)
                _ = path.write_text("    Starting 1 test\n")
                stamp = round(fixture.clock.now() * 1_000_000_000)
                os.utime(path, ns=(stamp, stamp))
                fixture.clock.advance(1)

                self.drive(fixture, run, 1, ())
                trace = run.store.read_live(run.grant.reservation)
                assert isinstance(trace, memory_trace.MemoryTrace)
                self.assertEqual(trace.compile_samples, ())
                self.assertEqual(trace.test_samples, (memory_trace.TestSample(2000, 2 * GIB),))

    def test_progress_log_ignores_phase_markers_from_before_scope_creation(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            _ = run.log.write_text("Compiling hana v0.1.0\nBuilding [=>] 8/9\r    Starting 1 test\n" +
                                   "    Summary [  1.0s] 1 test run: 1 passed\n    Starting 1 test")
            stamp = round((fixture.clock.now() - 1) * 1_000_000_000)
            os.utime(run.log, ns=(stamp, stamp))

            self.drive(fixture, run, 5, ((2, lambda: self.append(run.log, "\n")),
                                         (4, lambda: self.append(run.log, "    Starting 2 tests\n"))))
            trace = run.store.read_live(run.grant.reservation)
            assert isinstance(trace, memory_trace.MemoryTrace)
            self.assertEqual(trace.test_start, memory_trace.TestStarted(4000, memory_trace.TestReserveUnavailable()))
            self.assertEqual(trace.test_samples, (memory_trace.TestSample(4000, 2 * GIB),))
            self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [0, 2000])
            self.assertEqual(trace.compile_samples[0].last_compiling, "hana")

    def test_restarted_progress_log_reads_new_phase_frames_from_the_beginning(self) -> None:
        for replace_log in (False, True):
            with self.subTest(replace_log=replace_log), AdmissionFixture() as fixture:
                run = self.grant(fixture)
                _ = run.log.write_text("output\n" * 100)

                def restart_log() -> None:
                    if replace_log:
                        _ = run.log.rename(fixture.root / "old.log")
                    suffix = "output\n" * 100 if replace_log else ""
                    _ = run.log.write_text("    Starting 1 test\n" + suffix)

                self.drive(fixture, run, 3, ((2, restart_log),))
                trace = run.store.read_live(run.grant.reservation)
                assert isinstance(trace, memory_trace.MemoryTrace)
                self.assertEqual(trace.test_start, memory_trace.TestStarted(2000, memory_trace.TestReserveUnavailable()))
                self.assertEqual(trace.test_samples, (memory_trace.TestSample(2000, 2 * GIB),))
                self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [0])

    def test_replaced_path_during_read_cannot_supply_memory_from_another_scope(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            read = gate.slice_working_set
            at = fixture.clock.monotonic() + 2

            def swap_then_read(directory: Path) -> records.MeasuredMemory:
                if directory.resolve() != run.scope or fixture.clock.monotonic() != at:
                    return read(directory)
                original = run.scope.rename(fixture.root / "old-scope")
                write_scope(run.scope, members=(run.request.owner.pid,), current=9 * GIB, anonymous=9 * GIB)
                try:
                    return read(directory)
                finally:
                    _ = run.scope.rename(fixture.root / "new-scope")
                    _ = original.rename(run.scope)

            with mock.patch.object(gate, "slice_working_set", side_effect=swap_then_read):
                self.drive(fixture, run, 3, ())
            trace = run.store.read_live(run.grant.reservation)
            assert isinstance(trace, memory_trace.MemoryTrace)
            self.assertEqual([sample.program_bytes for sample in trace.compile_samples], [2 * GIB, 2 * GIB])

    def test_trace_sample_reads_the_sidecar_once(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            run.reader.requested.clear()
            open_path = Path.open
            opened: list[float] = []
            start = fixture.clock.monotonic()

            def count_reads(path: Path, mode: str = "r", buffering: int = -1, encoding: str | None = None,
                            errors: str | None = None, newline: str | None = None):
                if path == run.sidecar:
                    opened.append(fixture.clock.monotonic() - start)
                return open_path(path, mode, buffering, encoding, errors, newline)

            with mock.patch.object(Path, "open", autospec=True, side_effect=count_reads):
                self.drive(fixture, run, 3, ())
            self.assertEqual(opened, [0, 2])

    def test_binding_uses_the_age_of_the_sidecar_it_opens(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            stat_path = Path.stat
            fresh = run.sidecar.stat()
            old = round((fixture.clock.now() - 1) * 1_000_000_000)
            os.utime(run.sidecar, ns=(old, old))

            def stale_snapshot(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
                if path == run.sidecar:
                    return fresh
                return stat_path(path, follow_symlinks=follow_symlinks)

            with mock.patch.object(Path, "stat", autospec=True, side_effect=stale_snapshot):
                self.drive(fixture, run, 3, ())
            self.assertIsInstance(run.store.read_live(run.grant.reservation), memory_trace.TraceUnavailable)

    def test_failed_scope_read_preserves_the_trace_for_release(self) -> None:
        for disappears in (False, True):
            with self.subTest(disappears=disappears), AdmissionFixture() as fixture:
                run = self.grant(fixture)
                _ = run.log.write_text("Compiling hana v0.1.0\rBuilding [=>] 8/9\r")
                read_scope = gate.slice_working_set
                start = fixture.clock.monotonic()

                def read(directory: Path) -> records.MeasuredMemory:
                    if directory.resolve() == run.scope and fixture.clock.monotonic() == start + 4:
                        if disappears:
                            _ = run.scope.rename(fixture.root / "old-scope")
                            raise OSError(errno.ENODEV, "scope disappeared")
                        raise PermissionError(errno.EACCES, "memory.stat")
                    return read_scope(directory)

                def release() -> None:
                    self.append(run.log, "    Summary [  2.01s] 1 test run: 1 passed\n")
                    self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                        "scope-read-failure", run.grant.reservation, 0)), [wire.Ok()])

                with mock.patch.object(gate, "slice_working_set", side_effect=read):
                    self.drive(fixture, run, 5, (
                        (2, lambda: self.append(run.log, "    Starting 1 test\n")), (4.01, release)))
                trace = self.sealed(run)
                self.assertEqual([sample.elapsed_ms for sample in trace.test_samples], [2000])
                end = trace.state
                assert isinstance(end, memory_trace.TraceEnd)
                self.assertEqual((end.exit_status, end.coverage), (0, memory_trace.WholeRun()))

    def test_delayed_tick_uses_scope_loss_time_for_the_release_grace(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture, policy=replace(TRACE_POLICY, scope_loss_grace_seconds=1))
            _ = run.log.write_text("Compiling hana v0.1.0\rBuilding [=>] 8/9\r")
            sample_due = memory_trace.TraceCapture.sample_due
            start = fixture.clock.monotonic()

            def delayed(capture: memory_trace.TraceCapture, release_time: Callable[[str], float],
                        claim_expiry: Callable[[memory_trace.TraceRelease], memory_trace.TraceRelease | None]) -> None:
                if fixture.clock.monotonic() == start + 4:
                    fixture.clock.advance(1.5)
                    self.append(run.log, "    Summary [  3.51s] 1 test run: 1 passed\n")
                    _ = run.scope.rename(fixture.root / "old-scope")
                    run.log.unlink()
                sample_due(capture, release_time, claim_expiry)

            def release() -> None:
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "delayed-tick", run.grant.reservation, 0)), [wire.Ok()])

            with mock.patch.object(memory_trace.TraceCapture, "sample_due", autospec=True, side_effect=delayed):
                self.drive(fixture, run, 6, (
                    (2, lambda: self.append(run.log, "    Starting 1 test\n")), (5.51, release)))
            end = self.sealed(run).state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual((end.elapsed_ms, end.exit_status, end.coverage), (5510, 0, memory_trace.WholeRun()))

    def test_narrow_sample_uses_the_time_of_the_scope_read(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            read_scope = gate.slice_working_set
            start = fixture.clock.monotonic()

            def read(directory: Path) -> records.MeasuredMemory:
                if directory.resolve() == run.scope and fixture.clock.monotonic() == start + 2:
                    fixture.clock.advance(0.5)
                return read_scope(directory)

            with mock.patch.object(gate, "slice_working_set", side_effect=read):
                self.drive(fixture, run, 3, ())
            live = run.store.read_live(run.grant.reservation)
            assert isinstance(live, memory_trace.MemoryTrace)
            self.assertEqual([sample.elapsed_ms for sample in live.compile_samples], [0, 2500])

    def test_release_after_scope_loss_can_seal_a_complete_run(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            _ = run.log.write_text("Compiling hana v0.1.0\rBuilding [=>] 8/9\r")

            def remove_scope() -> None:
                self.append(run.log, "    Summary [   3.99s] 1 test run: 1 passed\n")
                _ = run.scope.rename(fixture.root / "old-scope")
                run.log.unlink()
                run.sidecar.unlink()

            def release() -> None:
                live = run.store.read_live(run.grant.reservation)
                assert isinstance(live, memory_trace.MemoryTrace)
                self.assertEqual([sample.elapsed_ms for sample in live.test_samples], [2000, 4000])
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "late-release", run.grant.reservation, 0)), [wire.Ok()])

            self.drive(fixture, run, 7, (
                (2, lambda: self.append(run.log, "    Starting 1 test\n")), (6, remove_scope), (6.01, release)))
            end = self.sealed(run).state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual((end.elapsed_ms, end.exit_status, end.coverage), (6010, 0, memory_trace.WholeRun()))

    def test_release_retries_a_failed_trace_move(self) -> None:
        with AdmissionFixture() as fixture:
            policy = replace(TRACE_POLICY, sample_seconds=3)
            run = self.grant(fixture, policy=policy)
            live_path = run.store.root / "live" / f"{run.grant.reservation}.jsonl"
            move = Path.replace
            attempts: list[tuple[float, Path]] = []
            start = fixture.clock.monotonic()

            def move_once(path: Path, target: Path) -> Path:
                if path == live_path:
                    attempts.append((fixture.clock.monotonic() - start, target))
                    if len(attempts) == 1:
                        raise OSError("first trace move")
                return move(path, target)

            def release() -> None:
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "retry-seal", run.grant.reservation, 0)), [wire.Ok()])

            def check_pending() -> None:
                trace = run.store.read_live(run.grant.reservation)
                assert isinstance(trace, memory_trace.MemoryTrace)
                self.assertIsInstance(trace.state, memory_trace.TraceEnd)
                self.assertEqual(len(attempts), 1)

            due = 4 + policy.sample_seconds
            with mock.patch.object(Path, "replace", autospec=True, side_effect=move_once):
                self.drive(fixture, run, due + 1, ((4, release), (due - 0.5, check_pending)))
            self.assertEqual([at for at, _ in attempts], [4, due])
            self.assertEqual(attempts[0][1], attempts[1][1])
            self.assertFalse(live_path.exists())
            end = self.sealed(run).state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual((end.elapsed_ms, end.exit_status), (4000, 0))

    def test_release_retries_when_the_end_record_cannot_open_the_trace(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            live_path = run.store.root / "live" / f"{run.grant.reservation}.jsonl"
            moved_path = live_path.with_suffix(".held")

            def release() -> None:
                _ = run.scope.rename(fixture.root / "old-scope")
                _ = live_path.rename(moved_path)
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "open-failure", run.grant.reservation, 0)), [wire.Ok()])

            def restore() -> None:
                _ = moved_path.rename(live_path)
                trace = run.store.read_live(run.grant.reservation)
                assert isinstance(trace, memory_trace.MemoryTrace)
                self.assertIsInstance(trace.state, memory_trace.TraceRecording)

            self.drive(fixture, run, 4 + TRACE_POLICY.sample_seconds, ((3, release), (3.5, restore)))
            trace = self.sealed(run)
            end = trace.state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual((end.elapsed_ms, end.exit_status), (3000, 0))
            self.assertEqual([sample.elapsed_ms for sample in trace.compile_samples], [0, 2000])

    def test_release_retries_the_final_phase_record_and_seal(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            _ = run.log.write_text("Building [=>] 9/10\r")
            append = memory_trace.TraceWriter.append
            phase_attempts: list[memory_trace.TestStarted] = []
            move = Path.replace
            move_attempts: list[Path] = []
            live_path = run.store.root / "live" / f"{run.grant.reservation}.jsonl"

            def append_once(writer: memory_trace.TraceWriter, observation: memory_trace.TraceObservation
                            ) -> memory_trace.TraceRecording | memory_trace.TraceLimit:
                if isinstance(observation, memory_trace.TestStarted):
                    phase_attempts.append(observation)
                    if len(phase_attempts) == 1:
                        raise OSError("first final phase write")
                return append(writer, observation)

            def move_once(path: Path, target: Path) -> Path:
                if path == live_path:
                    move_attempts.append(target)
                    if len(move_attempts) == 1:
                        raise OSError("first trace move")
                return move(path, target)

            def release() -> None:
                self.append(run.log, "    Starting 1 test\n    Summary [   0.02s] 1 test run: 1 passed")
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "retry-final-phase", run.grant.reservation, 0)), [wire.Ok()])

            with (mock.patch.object(memory_trace.TraceWriter, "append", autospec=True, side_effect=append_once),
                  mock.patch.object(Path, "replace", autospec=True, side_effect=move_once)):
                self.drive(fixture, run, 2 + 2 * run.store.policy.sample_seconds, ((1.5, release),))
            trace = self.sealed(run)
            self.assertEqual(trace.test_start, memory_trace.TestStarted(1500, memory_trace.TestReserveUnavailable()))
            self.assertEqual(trace.test_samples, ())
            self.assertEqual(phase_attempts, [trace.test_start, trace.test_start])
            self.assertEqual(len(move_attempts), 2)
            self.assertEqual(move_attempts[0], move_attempts[1])
            end = trace.state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual(end.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.MISSING_TEST))

    def test_failed_trace_move_stops_at_the_policy_retry_count(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            live_path = run.store.root / "live" / f"{run.grant.reservation}.jsonl"
            move = Path.replace
            attempts: list[tuple[Path, bytes]] = []

            def deny_trace_move(path: Path, target: Path) -> Path:
                if path == live_path:
                    attempts.append((target, path.read_bytes()))
                    raise OSError("trace move failed")
                return move(path, target)

            def release() -> None:
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "retry-limit", run.grant.reservation, 0)), [wire.Ok()])

            with mock.patch.object(Path, "replace", autospec=True, side_effect=deny_trace_move):
                self.drive(fixture, run, 4 + TRACE_POLICY.sample_seconds * TRACE_POLICY.seal_retry_count,
                           ((3, release),))
            self.assertEqual(len(attempts), 1 + TRACE_POLICY.seal_retry_count)
            destination, original = attempts[-1]
            self.assertEqual(live_path.read_bytes(), original)
            self.assertEqual(destination.read_bytes(), original)
            end = self.sealed(run).state
            assert isinstance(end, memory_trace.TraceEnd)
            self.assertEqual(end.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.NO_NEXTTEST_COMPLETION))

    def test_replaced_scope_directory_seals_without_sampling_the_new_scope(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)

            def replace_scope() -> None:
                _ = run.scope.rename(fixture.root / "old-scope")
                write_scope(run.scope, members=(run.request.owner.pid,), current=9 * GIB, anonymous=9 * GIB)

            def check_pending() -> None:
                live = run.store.read_live(run.grant.reservation)
                assert isinstance(live, memory_trace.MemoryTrace)
                self.assertIsInstance(live.state, memory_trace.TraceRecording)

            grace = TRACE_POLICY.scope_loss_grace_seconds
            finish_at = 3 + grace
            self.drive(fixture, run, finish_at, ((2, replace_scope), (2 + grace - 0.5, check_pending)))
            trace = self.sealed(run)
            self.assertEqual([sample.program_bytes for sample in trace.compile_samples], [2 * GIB])
            self.assertEqual(trace.state, memory_trace.TraceEnd(
                round((fixture.clock.now() - finish_at + 2) * 1000), 2000, memory_trace.ExitStatusUnavailable(),
                memory_trace.IncompleteRun(memory_trace.IncompleteReason.SCOPE_LOST)))

    def test_tests_that_finish_between_ticks_seal_without_a_test_sample(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            _ = run.log.write_text("Building [=>] 9/10\r")

            def completed() -> None:
                self.append(run.log, "    Nextest run ID edd0\n    Starting 1 test\n"
                            + "    Summary [   0.02s] 1 test run: 1 passed\n")

            def release() -> None:
                self.assertEqual(fixture.server.handle_request(wire.ReleaseRequest(
                    "short-release", run.grant.reservation, 0)), [wire.Ok()])

            self.drive(fixture, run, 3.5, ((2, completed), (3, release)))
            trace = self.sealed(run)
            self.assertEqual(trace.test_start, memory_trace.TestStarted(2000, memory_trace.TestReserveUnavailable()))
            self.assertEqual(trace.test_samples, ())
            assert isinstance(trace.state, memory_trace.TraceEnd)
            self.assertEqual(trace.state.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.MISSING_TEST))

    def test_owner_exit_seals_an_incomplete_trace_after_the_scope_drains(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)

            def owner_exit() -> None:
                fixture.proc.kill(run.request.owner)
                _ = (run.scope / "cgroup.procs").write_text("")
                _ = (run.scope / "cgroup.events").write_text("populated 0\n")
                run.reader.request_read()

            def finalize_owner() -> None:
                fixture.server.run_once(0)
                self.assertNotIn(run.request.episode, fixture.server.reservations)

            self.drive(fixture, run, 4, ((2, owner_exit), (3, finalize_owner)))
            trace = self.sealed(run)
            assert isinstance(trace.state, memory_trace.TraceEnd)
            self.assertEqual(trace.state.exit_status, memory_trace.ExitStatusUnavailable())
            self.assertEqual(trace.state.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.SCOPE_LOST))

    def test_a_restart_seals_the_readable_prefix_as_incomplete(self) -> None:
        with AdmissionFixture() as fixture:
            run = self.grant(fixture)
            self.drive(fixture, run, 1, ())
            live = run.store.read_live(run.grant.reservation)
            assert isinstance(live, memory_trace.MemoryTrace)
            partial = run.store.root / "live" / f"{run.grant.reservation}.jsonl"
            self.append(partial, '["t",')
            original = partial.read_bytes()
            with mock.patch.object(threading.Thread, "start"):
                restarted = service.OperatingSystemReader(run.reader.plan, run.reader.latest,
                    fixture.readings, fixture.proc, fixture.clock, fixture.server.reading_events,
                    traces=memory_trace.TraceCapture(run.store, fixture.root, "", fixture.clock))
            updated = replace(run, reader=restarted)
            self.drive(fixture, updated, 1, ())
            trace = self.sealed(run)
            self.assertEqual(trace.compile_samples, live.compile_samples)
            self.assertEqual(partial.read_bytes(), original)
            assert isinstance(trace.state, memory_trace.TraceEnd)
            self.assertEqual(trace.state.coverage, memory_trace.IncompleteRun(memory_trace.IncompleteReason.RESTART))
            self.assertIsInstance(run.store.newest_whole_run(trace.header.shape_key), memory_trace.NoMatchingWholeRun)

    def test_other_scoped_steps_record_general_traces_and_unscoped_steps_record_none(self) -> None:
        for repository, step, scoped in (("other", "nextest", True), ("hana", "check", True), ("hana", "nextest", False)):
            with self.subTest(repository=repository, step=step, scoped=scoped), AdmissionFixture() as fixture:
                run = self.grant(fixture, repository=repository, step=step, scoped=scoped)
                self.drive(fixture, run, 3, ())
                files = list(run.store.root.rglob("*.jsonl"))
                if scoped:
                    self.assertEqual(files, [run.store.root / "live" / f"{run.grant.reservation}.jsonl"])
                    trace = run.store.read_live(run.grant.reservation)
                    assert isinstance(trace, memory_trace.MemoryTrace)
                    self.assertEqual(trace.header.general, memory_trace.GeneralTraceShape(
                        fixture.server.host, f"git-root:{repository}", step, run.request.argv))
                    self.assertEqual(trace.test_samples, ())
                else:
                    self.assertEqual(files, [])

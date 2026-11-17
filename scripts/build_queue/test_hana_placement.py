"""Check boot failure settlement and queue read invalidation through private sockets."""
from __future__ import annotations

from dataclasses import replace
from collections.abc import Callable
from typing import cast
import unittest
from unittest import mock

from scripts.build_queue import admission_protocol as wire, admission_records as records, admission_service as service, placement
from scripts.build_queue.test_placement import PlacementFixture, placement_request, publish_candidate, assignment_for, adopt_assignment, local_candidate
from scripts.aws_target import hana_box, hana_execution, hana_placement
from scripts.build_targets import target_config


def hana_target(fixture: PlacementFixture) -> target_config.SshTarget:
    settings = fixture.root / 'hana.conf'
    _ = settings.write_text('execution_transport=hana-control\nhost=fixture-hana\n# whole: 1..\n'
        + 'aws_timeout_s=7\n# whole: 1..\nstep_memory_gib=24\n')
    return target_config.SshTarget('fixture-hana', 'linux', settings)


class HanaQueueTests(unittest.TestCase):
    def test_failed_boot_reports_terminal_outcome_before_capacity_and_fallback_release(self) -> None:
        with PlacementFixture() as fixture:
            target = hana_target(fixture)
            request = placement_request(fixture)
            remote = replace(request.constraints.candidates[0], destination=replace(
                request.constraints.candidates[0].destination, provider='ssh', target=target.name, host='fixture-hana'))
            request = replace(request, prerequisite=placement.TokenNotRequired(),
                constraints=replace(request.constraints, candidates=(remote, local_candidate(fixture))))
            with mock.patch.object(target_config, 'read_targets', return_value=(target,)):
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                reference = adopt_assignment(fixture, request, assignment)
                report = wire.ExecutionReport('boot-failed', assignment, reference, placement.ExecutionNotStarted(),
                    placement.PrelaunchRefused('boot failed'), placement.DispatchPathPending('start recovery'))
                reply = fixture.connect(report).replies()[-1]
                self.assertIsInstance(reply, wire.DispatchStatus)
                key = placement.attempt_key(assignment.episode, assignment.attempt)
                pending = fixture.server.assignments[key]
                self.assertIsInstance(pending.state, placement.CurrentAssignment)
                self.assertIsInstance(pending.terminal_report, placement.TerminalAttemptReport)
                self.assertIsInstance(pending.retry, placement.RetryAt)
                deadline = pending.retry
                self.assertEqual(fixture.server.current_assignments[request.episode], key)
                self.assertNotIsInstance(fixture.connect(request).replies()[-1], wire.LocalPlacementGranted)
                fixture.clock.advance(10)
                _ = publish_candidate(fixture, request, placement.TemporaryExclusion('boot failed'))
                _ = fixture.connect(replace(report, request_id='boot-settled', path=placement.DispatchPathSettled())).replies()
                settled = fixture.server.assignments[key]
                self.assertIsInstance(settled.state, placement.ClosedAssignment)
                self.assertEqual(settled.retry, deadline)
                self.assertNotEqual(fixture.server.current_assignments.get(request.episode), key)
                fallback = fixture.connect(request).replies()[-1]
                self.assertIsInstance(fallback, wire.LocalPlacementGranted)
                assert isinstance(fallback, wire.LocalPlacementGranted)
                self.assertNotEqual(fallback.assignment.attempt, assignment.attempt)
                self.assertEqual(fallback.assignment.episode, assignment.episode)

    def test_dead_coordinator_releases_an_assignment_that_never_bound(self) -> None:
        with PlacementFixture() as fixture:
            target = hana_target(fixture)
            controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                target.settings, hana_box.ProviderNotInstalled())
            request = placement_request(fixture)
            candidate = request.constraints.candidates[0]
            request = replace(request, constraints=replace(request.constraints, candidates=(replace(candidate,
                destination=replace(candidate.destination, provider='ssh', target=target.name)),)))
            with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                    mock.patch.object(hana_placement, 'controller_for', return_value=controller)):
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                fixture.end_owner(wire.Owner(assignment.owner.pid, assignment.owner.start_time))
                _ = fixture.run_pass()
                saved = fixture.server.assignments[placement.attempt_key(assignment.episode, assignment.attempt)]
                self.assertIsInstance(saved.state, placement.ClosedAssignment)
                self.assertNotIn(request.episode, fixture.server.current_assignments)
                self.assertFalse(hana_execution.execution_path(controller.directory,
                    placement.assigned_claim_id(assignment.episode, assignment.attempt)).exists())

    def test_dead_coordinator_keeps_a_bound_assignment_with_missing_or_unreadable_journal(self) -> None:
        for journal in ('missing', 'unreadable'):
            with self.subTest(journal=journal), PlacementFixture() as fixture:
                target = hana_target(fixture)
                controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                    target.settings, hana_box.ProviderNotInstalled())
                request = placement_request(fixture)
                candidate = request.constraints.candidates[0]
                request = replace(request, constraints=replace(request.constraints, candidates=(replace(candidate,
                    destination=replace(candidate.destination, provider='ssh', target=target.name)),)))
                with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                        mock.patch.object(hana_placement, 'controller_for', return_value=controller)):
                    _ = publish_candidate(fixture, request)
                    assignment = assignment_for(fixture, request)
                    execution = placement.assigned_claim_id(assignment.episode, assignment.attempt)
                    controller.bind_execution(execution)
                    if journal == 'unreadable':
                        _ = hana_execution.execution_path(controller.directory, execution).write_text('{')
                    fixture.end_owner(wire.Owner(assignment.owner.pid, assignment.owner.start_time))
                    _ = fixture.run_pass()
                    key = placement.attempt_key(assignment.episode, assignment.attempt)
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                    self.assertEqual(fixture.server.current_assignments[request.episode], key)

    def test_memory_eligibility_is_per_request_with_shared_host_evidence_in_both_orders(self) -> None:
        for order in ((1, 40), (40, 1)):
            with self.subTest(order=order), PlacementFixture() as fixture:
                target = hana_target(fixture)
                request = placement_request(fixture)
                candidate = request.constraints.candidates[0]
                request = replace(request, constraints=replace(request.constraints, candidates=(replace(candidate,
                    destination=replace(candidate.destination, provider='ssh', target=target.name)),)))
                with mock.patch.object(target_config, 'read_targets', return_value=(target,)):
                    shared = publish_candidate(fixture, request)
                    for gib in order:
                        sized = replace(request, episode=f'{request.episode}-{gib}', profile=replace(request.profile,
                            peak=records.ExpectedPeak(gib * 1024 ** 3, 'fallback', 0)))
                        self.assertEqual(fixture.server.host_evidence_key(sized,
                            request.constraints.candidates[0].destination), shared.key)
                        reply = fixture.connect(sized).replies()[-1]
                        if gib == 1:
                            self.assertIsInstance(reply, wire.Assignment)
                        else:
                            self.assertIsInstance(reply, wire.PlacementWait)
                            assert isinstance(reply, wire.PlacementWait)
                            self.assertEqual(reply.eligibility[0].eligibility, placement.TemporaryExclusion('memory'))
                            self.assertNotIn(sized.episode, fixture.server.current_assignments)
                        self.assertEqual(fixture.server.host_evidence(shared.key), shared)

    def test_hana_timeout_uses_the_aws_bound_without_ssh_keys(self) -> None:
        with PlacementFixture() as fixture:
            target = hana_target(fixture)
            request = placement_request(fixture)
            destination = replace(request.constraints.candidates[0].destination, provider='ssh', target=target.name)
            key = placement.HostEvidenceKey(destination, request.constraints.session, ('cargo', 'check'), 'configuration')
            with mock.patch.object(target_config, 'read_targets', return_value=(target,)):
                timeout = cast(Callable[[placement.HostEvidenceKey], float], getattr(fixture.server, '_host_probe_timeout'))
                self.assertEqual(timeout(key), 7)

    def test_hana_read_bypasses_ssh_policy_and_process_probes(self) -> None:
        with PlacementFixture() as fixture:
            settings = fixture.root / 'hana.conf'
            _ = settings.write_text('execution_transport=hana-control\nhost=fixture-hana\n')
            target = target_config.SshTarget('fixture-hana', 'linux', settings)
            request = placement_request(fixture)
            destination = replace(request.constraints.candidates[0].destination, provider='ssh', target=target.name)
            key = placement.HostEvidenceKey(destination, request.constraints.session, ('cargo', 'check'), 'configuration')
            fixture.server.host_requests[key] = request
            answer = placement.FreshHostEvidence(key, fixture.clock.now(), 'boot', placement.AllowedHost())
            with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                    mock.patch.object(hana_placement, 'read_evidence', return_value=answer) as read):
                self.assertEqual(fixture.server.read_provider_evidence(key), answer)
                read.assert_called_once_with(key, request)

    def test_worker_evidence_marks_the_snapshot_stale_on_the_loop_thread(self) -> None:
        with PlacementFixture() as fixture:
            request = placement_request(fixture)
            _ = publish_candidate(fixture, request)
            _ = assignment_for(fixture, request)
            key = fixture.server.host_evidence_key(request, request.constraints.candidates[0].destination)
            worker = service.HostEvidenceWorker(key, fixture.server.host_probe, fixture.clock,
                fixture.server.reading_events, 1)
            self.addCleanup(worker.close)
            reader = service.HostEvidenceReader(key, worker, 1)
            fixture.server.host_readers[key] = reader
            updated = placement.FreshHostEvidence(key, fixture.clock.now(), 'new-boot', placement.TemporaryExclusion('starting'))
            reader.publish(updated)
            fixture.server.pass_stale = False
            refresh = cast(Callable[[], None], getattr(fixture.server, '_refresh_host_observations'))
            refresh()
            self.assertTrue(fixture.server.pass_stale)
            self.assertEqual(fixture.server.host_observations[key], updated)

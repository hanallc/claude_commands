"""Check boot failure settlement and queue read invalidation through private sockets."""
from __future__ import annotations

import base64
from dataclasses import replace
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stderr
from importlib import import_module
from io import StringIO
import json
import os
from pathlib import Path
import threading
import time
from typing import cast, override
import unittest
from unittest import mock

from scripts.build_queue import execution_identity
from scripts.build_queue import admission_client, admission_protocol as wire, admission_records as records, admission_service as service, placement
from scripts.build_queue.test_placement import (
    PlacementFixture, ManualEvidenceWorker, placement_request, publish_candidate, assignment_for, adopt_assignment,
    consume_permission, coordinator_holder, hana_policy_request, local_candidate,
)
from scripts.build_queue.admission_test_support import only_admitted
from scripts.aws_target import hana_box, hana_execution, hana_placement, hana_placement_adapter, hana_protocol, hana_result, ledger, test_hana_execution
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.ledger_test_support import install_cost_settings, install_hana_spending_settings, text_setting
from scripts.aws_target.test_hana_box import FakeProvider, IDENTITY, record_lifecycle_image
from scripts.aws_target.controller_lock import control_lock
from scripts.build_targets import target_config
from scripts.buildlog import store
from scripts.lib import atomic_file, conf, process_start
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import claim_state, eligibility, mac_test, offload


def hana_target(fixture: PlacementFixture) -> target_config.SshTarget:
    settings = fixture.root / 'hana.conf'
    _ = settings.write_text('execution_transport=hana-control\nhost=fixture-hana\n# whole: 1..\n'
        + 'aws_timeout_s=7\n# whole: 1..\nstep_memory_gib=24\n')
    return target_config.SshTarget('fixture-hana', 'linux', settings)


def hana_fallback_request(fixture: PlacementFixture, *, include_mac: bool = False) -> wire.PlacementRequest:
    target = hana_target(fixture)
    _ = write_config_fixture(fixture.root / 'mac_test.conf', 'host=fixture-mac\n')
    _ = write_config_fixture(fixture.root / 'build_targets.conf',
        f'targets={target.name},mac,natedev\nkind.{target.name}=ssh\nplatform.{target.name}=linux\n'
        + f'settings.{target.name}={target.settings.name}\nkind.mac=ssh\nplatform.mac=macos\nsettings.mac=mac_test.conf\nenabled.mac=on\n'
        + 'kind.natedev=local\n')
    request = placement_request(fixture)
    mac = request.constraints.candidates[0]
    remote = replace(mac, destination=replace(mac.destination, provider='ssh', target=target.name, host='fixture-hana'))
    local = local_candidate(fixture)
    candidates = (remote, mac, local) if include_mac else (remote, local)
    return replace(request, prerequisite=placement.TokenNotRequired(),
        constraints=replace(request.constraints, candidates=candidates))


class HanaQueueTests(unittest.TestCase):
    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        # Load the target helpers before a test replaces the configuration reader.
        _ = import_module('scripts.build_targets.build_targets')

    def test_consume_reads_the_claim_immediately_after_warm_or_cold_adoption(self) -> None:
        for warm, lower_memory in ((False, False), (True, False), (True, True)):
            with self.subTest(warm=warm, lower_memory=lower_memory), PlacementFixture() as fixture:
                self.check_immediate_consume(fixture, warm=warm, lower_memory=lower_memory)

    def check_immediate_consume(self, fixture: PlacementFixture, *, warm: bool, lower_memory: bool) -> None:
        request, transport = hana_policy_request(fixture)
        install_hana_spending_settings(transport.settings)
        destination = request.constraints.candidates[0].destination
        scope = destination.scope
        assert isinstance(scope, claim_state.RepositoryScope)
        for name, value in (('host', transport.host), ('execution_transport', 'hana-control'),
                            ('repository_identity', scope.key), ('kinds', 'check,lint')):
            text_setting(transport.settings, name, value)
        with transport.settings.open('a') as settings_file:
            _ = settings_file.write('# whole: 1..\nmax_steps=1\n# whole: 1..5\npreview_max_age_s=2\n')
        request = replace(request, prerequisite=placement.TokenNotRequired(),
            profile=replace(request.profile, peak=records.ExpectedPeak(2 * 1024 ** 3, 'fallback', 0)))
        directory = fixture.root / 'hana-state'
        clock = TestClock(int(fixture.clock.now()))
        provider = FakeProvider(directory, clock)
        settings = fixture.root / 'aws_target.conf'
        install_cost_settings(settings)
        controller = hana_box.HanaController(directory, settings, transport.settings, provider, clock=clock)
        book = ledger.CostLedger(directory)
        book.record_inventory(ledger.ResourceInventory(clock.now, (), 'private inventory', True))
        book.observe(ledger.InstanceObservation(clock.now, 'terminated', 'private account inventory'))
        controller.register(IDENTITY)
        _ = record_lifecycle_image(directory, transport.settings)
        _ = controller.tick()

        def tick(seconds: tuple[int, ...]) -> None:
            for advance in seconds:
                clock.advance(advance)
                fixture.clock.advance(advance)
                _ = controller.tick()

        if warm:
            self.assertIsInstance(controller.request('warm-up'), hana_box.ClaimCommitted)
            tick((1, 1, 1, 1))
            self.assertIsInstance(controller.release('warm-up'), hana_placement.HanaClaimReleased)

        # Keep the first worker reading in the cache until after launch permission.
        fixture.configure(waiting_pass_seconds=60)
        published = threading.Event()
        probe = cast(Callable[[service.HostEvidenceWorker, placement.HostEvidenceKey, object], bool],
            getattr(service.HostEvidenceWorker, '_probe'))

        def notify(worker: service.HostEvidenceWorker, key: placement.HostEvidenceKey, subscription: object) -> bool:
            running = probe(worker, key, subscription)
            if isinstance(worker.evidence(key), placement.FreshHostEvidence):
                published.set()
            return running

        with (mock.patch.object(hana_placement_adapter, 'controller_for', return_value=controller),
                mock.patch.object(eligibility, 'repository_identity',
                    return_value=eligibility.KnownRepository(scope.name, scope.key)),
                mock.patch.dict(os.environ, {'BUILD_TARGETS_STATE_DIR': str(fixture.root / 'targets')}),
                mock.patch.object(service.HostEvidenceWorker, '_probe', notify)):
            fixture.server.host_probe = fixture.read_host_evidence
            _ = fixture.run_pass()
            _ = fixture.connect(request).replies()
            self.assertTrue(published.wait(TEST_HANG_GUARD_SECS))
            assignment = assignment_for(fixture, request)
            holder = coordinator_holder(assignment)
            self.assertIsInstance(hana_placement.claim_assigned(assignment, holder, controller), hana_placement.HanaClaimPending)
            tick((1, 1) if warm else (1, 1, 1, 1))
            adopted = hana_placement.claim_assigned(assignment, holder, controller)
            assert isinstance(adopted, hana_placement.HanaClaimAdopted), adopted
            reference = adopted.reserved.claim.reference
            if lower_memory:
                settings_text = transport.settings.read_text()
                self.assertIn('step_memory_gib=24', settings_text)
                _ = transport.settings.write_text(settings_text.replace('step_memory_gib=24', 'step_memory_gib=1'))
            reply, = fixture.connect(consume_permission('consume', assignment, reference)).replies()
            if lower_memory:
                assert isinstance(reply, wire.DispatchStatus), reply
                self.assertEqual(reply.reason, 'Hana admission refused: memory')
                self.assertIsInstance(reply.permission, placement.PermissionAvailable)
            else:
                self.assertEqual(reply, wire.FirstAuthorization(assignment, reference))

    def test_refusal_keeps_the_same_episode_off_hana(self) -> None:
        for cause, claim_adopted in ((placement.HANA_IMAGE_STALE, True), (hana_box.HANA_CLAIM_BUSY, False)):
            with self.subTest(cause=cause), PlacementFixture() as fixture:
                request = hana_fallback_request(fixture)
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = (adopt_assignment(fixture, request, assignment) if claim_adopted
                         else placement.assigned_claim_reference(assignment))
                _ = fixture.connect(wire.ExecutionReport('admission-refusal', assignment, claim,
                    execution_identity.ExecutionNotStarted(), execution_identity.HanaCommandAdmissionRefused(cause),
                    placement.DispatchPathSettled())).replies()
                key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                closed = fixture.server.assignments[key]
                self.assertEqual(closed.retry, placement.DoNotRetry(cause))
                assert isinstance(closed.terminal_report, placement.TerminalAttemptReport)
                self.assertEqual(closed.terminal_report.outcome, execution_identity.HanaCommandAdmissionRefused(cause))
                _ = publish_candidate(fixture, request)
                fallback = fixture.connect(request).replies()[-1]
                assert isinstance(fallback, wire.LocalPlacementGranted)
                self.assertEqual(fallback.assignment.episode, assignment.episode)
                self.assertFalse(fallback.hana_image_stale)

    def test_routed_fallback_prints_the_stale_hana_image_once(self) -> None:
        with PlacementFixture() as fixture:
            request = hana_fallback_request(fixture)
            _ = publish_candidate(fixture, request, placement.FixedExclusion(placement.HANA_IMAGE_STALE))
            reply = fixture.connect(request).replies()[-1]
            assert isinstance(reply, wire.LocalPlacementGranted)
            with (mock.patch.dict(os.environ, {'BUILDLOG_HOLD_WAIT_FILE': str(fixture.root / 'seconds')}),
                    redirect_stderr(StringIO()) as output):
                client = admission_client.WaitOutput()
                client.placement_notices(reply, placement_events=True)
                client.placement_notices(reply, placement_events=True)
                admission_client.WaitOutput().placement_notices(reply, placement_events=True)
            self.assertEqual(output.getvalue(), placement.HANA_IMAGE_STALE + '\n')

    def test_failed_boot_reports_terminal_outcome_before_capacity_and_fallback_release(self) -> None:
        with PlacementFixture() as fixture:
            request = hana_fallback_request(fixture)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            reference = adopt_assignment(fixture, request, assignment)
            report = wire.ExecutionReport('boot-failed', assignment, reference, execution_identity.ExecutionNotStarted(),
                placement.PrelaunchRefused('boot failed'), placement.DispatchPathPending('start recovery'))
            reply = fixture.connect(report).replies()[-1]
            self.assertIsInstance(reply, wire.DispatchStatus)
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
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

    def test_command_admission_refusal_excludes_hana_from_the_same_build(self) -> None:
        for next_target, stale_absence, stale_image in (
                ('natedev', False, False), ('mac', False, False), ('natedev', True, False), ('natedev', False, True)):
            with (self.subTest(next_target=next_target, stale_absence=stale_absence, stale_image=stale_image),
                  PlacementFixture() as fixture, mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker)):
                request = hana_fallback_request(fixture, include_mac=next_target == 'mac')
                remote = request.constraints.candidates[0]
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                claim = adopt_assignment(fixture, request, assignment)
                adopted = placement.AdoptedProviderClaim(claim, coordinator_holder(assignment),
                    assignment.session, assignment.destination.host, 'host-generation-a')
                with mock.patch.object(hana_placement_adapter, 'read_assigned_adoption', return_value=adopted):
                    self.assertIsInstance(fixture.connect(consume_permission('consume', assignment, claim)).replies()[-1],
                                          wire.FirstAuthorization)
                if next_target == 'mac':
                    mac = request.constraints.candidates[1]
                    _ = publish_candidate(fixture, replace(request, constraints=replace(request.constraints, candidates=(mac,))))
                other = replace(placement_request(fixture), prerequisite=placement.TokenNotRequired(),
                    constraints=replace(request.constraints, candidates=(remote,)))
                if stale_absence:
                    _ = publish_candidate(fixture, other)
                    fixture.clock.advance(1)
                cause = placement.HANA_IMAGE_STALE if stale_image else 'The data mount and a fresh permit are required'
                if stale_image:
                    provider = placement.AdoptedProviderClaim(claim, placement.ClaimHolderIdentity(
                        assignment.owner.host, assignment.owner.pid, assignment.owner.start_time),
                        assignment.session, assignment.destination.host, 'host-generation-a')
                    _ = publish_candidate(fixture, other, claims=(provider,))
                report = wire.ExecutionReport('admission-refused', assignment, claim,
                    execution_identity.ExecutionNotStarted(), execution_identity.HanaCommandAdmissionRefused(cause),
                    placement.DispatchPathSettled())
                _ = fixture.connect(report).replies()
                key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                saved = fixture.server.assignments[key]
                self.assertEqual(saved.retry, placement.DoNotRetry(cause))
                self.assertEqual(saved.state, placement.ClosedAssignment(cause))
                fallback = fixture.connect(request).replies()[-1]
                self.assertIsInstance(fallback, wire.Assignment if next_target == 'mac' else wire.LocalPlacementGranted)
                assert isinstance(fallback, (wire.Assignment, wire.LocalPlacementGranted))
                self.assertEqual(fallback.assignment.destination.target, next_target)
                self.assertEqual(fallback.assignment.episode, assignment.episode)
                self.assertNotEqual(fallback.assignment.attempt, assignment.attempt)
                if stale_absence or stale_image:
                    self.assertIsInstance(fixture.connect(other).replies()[-1], wire.PlacementWait)
                    _ = publish_candidate(fixture, other)
                    self.assertEqual(assignment_for(fixture, other).destination, remote.destination)

    def test_dead_coordinator_releases_an_assignment_that_never_bound(self) -> None:
        with PlacementFixture() as fixture, mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker):
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
                worker = fixture.server.host_targets[('ssh', target.name)]
                assert isinstance(worker, ManualEvidenceWorker)
                _ = worker.probe_next()
                _ = fixture.run_pass()
                saved = fixture.server.assignments[execution_identity.attempt_key(assignment.episode, assignment.attempt)]
                self.assertIsInstance(saved.state, placement.ClosedAssignment)
                self.assertNotIn(request.episode, fixture.server.current_assignments)
                self.assertFalse(hana_execution.execution_path(controller.directory,
                    execution_identity.assigned_claim_id(assignment.episode, assignment.attempt)).exists())

    def test_dead_coordinator_keeps_a_bound_assignment_with_missing_or_unreadable_journal(self) -> None:
        for journal in ('missing', 'unreadable'):
            with (self.subTest(journal=journal), PlacementFixture() as fixture,
                    mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker)):
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
                    execution = execution_identity.assigned_claim_id(assignment.episode, assignment.attempt)
                    controller.bind_execution(execution)
                    if journal == 'unreadable':
                        _ = hana_execution.execution_path(controller.directory, execution).write_text('{')
                    fixture.end_owner(wire.Owner(assignment.owner.pid, assignment.owner.start_time))
                    _ = fixture.run_pass()
                    worker = fixture.server.host_targets[('ssh', target.name)]
                    assert isinstance(worker, ManualEvidenceWorker)
                    _ = worker.probe_next()
                    _ = fixture.run_pass()
                    key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                    self.assertEqual(fixture.server.current_assignments[request.episode], key)

    def test_dead_holder_replay_requires_exact_proof_and_reports_once(self) -> None:
        for proof in ('legacy refusal', 'no reply', 'other boot', 'claim from other boot',
                      'restart after release reports once', 'sender settled without a saved observation',
                      'sender final write failed without a saved observation',
                      'pending conflicting report', 'accepted completion with changed source', 'failed release', 'interrupted journal write'):
            with self.subTest(proof=proof):
                self.check_dead_holder_replay(proof)

    def check_dead_holder_replay(self, proof: str) -> None:
        with PlacementFixture() as fixture, mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker):
            target = hana_target(fixture)
            controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                target.settings, hana_box.ProviderNotInstalled())
            controller.register(IDENTITY)
            original = placement_request(fixture)
            scope = claim_state.RepositoryScope('hana', 'fixture-repository', 1)
            destination = placement.PlacementDestination('ssh', target.name, 'fixture-hana', scope, 1)
            request = replace(original, prerequisite=placement.TokenNotRequired(),
                constraints=replace(original.constraints, command=('cargo', 'check'), command_kind='check',
                    repository=str(fixture.root), candidates=(placement.PlacementCandidate(destination),)))
            _ = fixture.enter_context(mock.patch.object(target_config, 'read_targets', return_value=(target,)))
            _ = fixture.enter_context(mock.patch.object(hana_placement, 'controller_for', return_value=controller))
            _ = cast(object, fixture.enter_context(mock.patch.dict(os.environ,
                {'BUILD_TARGETS_STATE_DIR': str(fixture.root / 'targets')})))

            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            reference = placement.assigned_claim_reference(assignment)
            holder = coordinator_holder(assignment)
            adopted = placement.AdoptedProviderClaim(reference, holder, assignment.session, destination.host, 'host-generation-a')
            _ = publish_candidate(fixture, request, claims=(adopted,))
            with mock.patch.object(hana_placement_adapter, 'read_assigned_adoption', return_value=adopted):
                authorized, = fixture.connect(consume_permission('consume', assignment, reference)).replies()
            self.assertEqual(authorized, wire.FirstAuthorization(assignment, reference))

            paths = claim_state.state_paths(target.name)
            path = mac_test.claim_path(paths, scope)
            path.parent.mkdir(parents=True)
            claim: claim_state.RemoteJobClaim = {
                'claim_id': reference.claim_id, 'episode': reference.episode, 'attempt': reference.attempt,
                'pid': holder.pid, 'proc_start': holder.start_time, 'what': 'Hana judgment', 'worktree': str(fixture.root),
                'since': '2024-01-01T00:00:00+00:00', 'submitting_session': claim_state.session_id(assignment.session),
                'repo_name': scope.name, 'repo_key': scope.key, 'slot': scope.slot, 'remote_host': destination.host,
            }
            controller.bind_execution(reference.claim_id)
            execution_fixture = test_hana_execution.HanaExecutionTests()
            template = execution_fixture.request
            source = assignment.source_key
            assert isinstance(source, execution_identity.SourceKeyKnown)
            attempt = replace(template.key, episode=reference.episode, attempt=reference.attempt, claim_id=reference.claim_id)
            dispatch = replace(template, key=attempt, source_key=source, link=replace(template.link, episode=attempt.episode))
            claim_generation = (replace(attempt.generation, boot_id='other-boot')
                                if proof == 'claim from other boot' else attempt.generation)
            claim['instance_generation'] = hana_placement.generation_text(claim_generation)
            _ = path.write_text(json.dumps(claim))
            observed_attempt = (replace(attempt, generation=replace(attempt.generation, boot_id='other-boot'))
                                if proof == 'other boot' else attempt)
            reason = 'The data mount and a fresh permit are required'
            accepted_completion = proof == 'accepted completion with changed source'
            evidence = execution_identity.ExecutionEvidenceUnavailable(
                'The command ended' if accepted_completion else reason, fixture.clock.now())
            observation = hana_protocol.HanaCommandObservation(observed_attempt, dispatch.slot, True,
                accepted_completion, True, True, fixture.clock.now(), evidence, False)
            journal: dict[str, object] = {'version': 1, 'request': hana_protocol.request_fields(dispatch), 'phase': 'fenced',
                'publication_attempted': True, 'command_settled': False, 'evidence': hana_protocol.encode_evidence(evidence)}
            sender_write_failed = proof == 'sender final write failed without a saved observation'
            sender_fenced = proof == 'sender settled without a saved observation' or sender_write_failed
            if proof != 'no reply' and not sender_fenced:
                journal['observation'] = cast(object, json.loads(hana_protocol.encode_observation(observation)))
            if accepted_completion:
                result = replace(execution_fixture.command_result(), key=attempt, source_key=source.key, host=destination.host)
                journal['accepted_result'] = base64.b64encode(hana_result.encode_result(result)).decode('ascii')
            journal_path = hana_execution.execution_path(controller.directory, reference.claim_id)
            original_journal = json.dumps(journal)
            _ = journal_path.write_text(original_journal)

            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            if proof == 'pending conflicting report':
                _ = fixture.connect(wire.ExecutionReport('pending', assignment, reference, evidence,
                    placement.AttemptDeclined('lost'), placement.DispatchPathPending('claim retained'))).replies()
            pending = fixture.server.assignments[key].terminal_report
            if proof == 'pending conflicting report':
                assert isinstance(pending, placement.TerminalAttemptReport)
                self.assertEqual(pending.outcome, placement.AttemptDeclined('lost'))
            write_atomic = atomic_file.write_text_atomic

            def interrupted_write(path: Path, content: str, *, new_file_mode: int | None = None) -> None:
                if path == journal_path and '"phase": "settled"' in content:
                    raise OSError('The final journal write was interrupted')
                write_atomic(path, content, new_file_mode=new_file_mode)

            with (mock.patch.object(hana_placement_adapter, 'release_assigned',
                                    wraps=hana_placement_adapter.release_assigned) as release,
                    mock.patch.object(offload, 'read_tree_key', return_value=offload.KnownTreeKey('changed')) as source_read):
                sender_reason = 'The sender fenced the command'
                if sender_fenced:
                    objects = test_hana_execution.FakeExecutionObjects(dispatch, accepted=True)
                    with (mock.patch.object(atomic_file, 'write_text_atomic', side_effect=interrupted_write)
                          if sender_write_failed else nullcontext()):
                        settled = hana_execution.settle_execution(controller.directory, dispatch, objects,
                            release_claim=lambda: hana_placement_adapter.release_assigned(assignment, holder, controller),
                            reason=sender_reason, fence_window=hana_execution.FenceAcknowledgementWindow(
                                time.monotonic() + TEST_HANG_GUARD_SECS, 1))
                    if sender_write_failed:
                        self.assertIsInstance(settled, hana_execution.HanaSettlementPending)
                        self.assertIn('"phase": "fenced"', journal_path.read_text())
                        self.assertTrue(hana_execution.execution_command_settled(controller.directory, reference.claim_id))
                        self.assertTrue(hana_execution.claim_dispatch_pending(controller.directory, reference))
                        self.assertEqual(fixture.server.current_assignments[assignment.episode], key)
                        release.assert_called_once_with(assignment, holder, controller)
                    else:
                        assert isinstance(settled, hana_execution.HanaSettled)
                        self.assertEqual(settled.outcome, placement.AttemptDeclined(sender_reason))
                        self.assertFalse(hana_execution.claim_dispatch_pending(controller.directory, reference))
                    self.assertEqual(len(objects.envelopes), 1)
                    self.assertIsInstance(objects.envelopes[0], hana_protocol.HanaFenceEnvelope)
                    self.assertNotIn('"observation"', journal_path.read_text())
                    self.assertFalse(path.exists())
                fixture.end_owner(wire.Owner(holder.pid, holder.start_time))
                if proof == 'failed release':
                    release.return_value = hana_placement.HanaClaimReleaseRefused('The Hana lifecycle operation observe is pending')
                    self.probe_dead_holder(fixture, target.name)
                    _ = fixture.run_pass()
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                    self.assertEqual(fixture.server.current_assignments[assignment.episode], key)
                    self.assertTrue(path.exists())
                    self.assertIn('"phase": "fenced"', journal_path.read_text())
                    self.assertTrue(hana_execution.execution_command_settled(controller.directory, reference.claim_id))
                    source_read.assert_not_called()
                    release.assert_called_once_with(assignment, holder, controller)
                    return
                if proof == 'interrupted journal write':
                    with mock.patch.object(atomic_file, 'write_text_atomic', side_effect=interrupted_write):
                        self.probe_dead_holder(fixture, target.name)
                    _ = fixture.run_pass()
                    self.assertEqual(fixture.server.current_assignments[assignment.episode], key)
                    self.assertFalse(path.exists())
                    self.assertIn('"phase": "fenced"', journal_path.read_text())
                    self.assertTrue(hana_execution.execution_command_settled(controller.directory, reference.claim_id))
                    release.assert_called_once_with(assignment, holder, controller)
                self.probe_dead_holder(fixture, target.name)
                if proof == 'restart after release reports once':
                    self.assertFalse(path.exists())
                    self.assertFalse(hana_execution.claim_dispatch_pending(controller.directory, reference))
                    self.assertEqual(fixture.server.current_assignments[assignment.episode], key)
                    self.assertIsInstance(fixture.server.assignments[key].terminal_report, placement.NoTerminalReport)
                    release.assert_called_once_with(assignment, holder, controller)
                    fixture.restart()
                    self.probe_dead_holder(fixture, target.name)
                handle_report = cast(Callable[[wire.ExecutionReport], list[wire.Reply]],
                    getattr(fixture.server, '_handle_placement'))
                applied = fixture.enter_context(mock.patch.object(fixture.server, '_handle_placement', wraps=handle_report))
                _ = fixture.run_pass()
                saved = fixture.server.assignments[key]
                if proof in ('no reply', 'other boot', 'claim from other boot'):
                    self.assertIsInstance(saved.state, placement.CurrentAssignment)
                    self.assertIsInstance(saved.terminal_report, placement.NoTerminalReport)
                    self.assertEqual(fixture.server.current_assignments[assignment.episode], key)
                    self.assertTrue(path.exists())
                    self.assertEqual(journal_path.read_text(), original_journal)
                    release.assert_not_called()
                    applied.assert_not_called()
                    return
                self.assertNotIn(assignment.episode, fixture.server.current_assignments)
                self.assertFalse(path.exists())
                self.assertFalse(hana_execution.claim_dispatch_pending(controller.directory, reference))
                report = saved.terminal_report
                assert isinstance(report, placement.TerminalAttemptReport)
                self.assertIsInstance(report.path, placement.DispatchPathSettled)
                if proof == 'pending conflicting report':
                    assert isinstance(pending, placement.TerminalAttemptReport)
                    self.assertEqual(saved.state, placement.ClosedAssignment('lost'))
                    self.assertEqual(report, replace(pending, path=placement.DispatchPathSettled()))
                elif accepted_completion:
                    self.assertIsInstance(saved.state, placement.ClosedAssignment)
                    self.assertEqual(report.outcome, placement.AttemptCompleted(0))
                    source_read.assert_not_called()
                elif sender_fenced:
                    self.assertEqual(saved.state, placement.ClosedAssignment(sender_reason))
                    self.assertEqual(report.outcome, placement.AttemptDeclined(sender_reason))
                    source_read.assert_not_called()
                else:
                    self.assertEqual(saved.state, placement.ClosedAssignment(reason))
                    self.assertEqual(saved.retry, placement.DoNotRetry(reason))
                    self.assertEqual(report.outcome, execution_identity.HanaCommandAdmissionRefused(reason))
                    self.assertIsInstance(fixture.server.execution_evidence[key], execution_identity.ExecutionNotStarted)
                _ = fixture.run_pass()
                self.assertIs(fixture.server.assignments[key], saved)
                applied.assert_called_once_with(wire.ExecutionReport('hana-recovery:' + reference.claim_id,
                    assignment, reference, fixture.server.execution_evidence[key], report.outcome, placement.DispatchPathSettled()))
                release.assert_called_once_with(assignment, holder, controller)

    def probe_dead_holder(self, fixture: PlacementFixture, target: str) -> None:
        _ = fixture.run_pass()
        worker = fixture.server.host_targets[('ssh', target)]
        assert isinstance(worker, ManualEvidenceWorker)
        _ = worker.probe_next()

    def test_controller_lock_does_not_block_a_queue_pass_or_release_reply(self) -> None:
        with PlacementFixture() as fixture:
            target = hana_target(fixture)
            controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                target.settings, hana_box.ProviderNotInstalled())
            request = placement_request(fixture)
            candidate = request.constraints.candidates[0]
            request = replace(request, constraints=replace(request.constraints, candidates=(replace(candidate,
                destination=replace(candidate.destination, provider='ssh', target=target.name)),)))
            locked, finish, reading = threading.Event(), threading.Event(), threading.Event()
            execution_bound = controller.execution_bound

            def hold_controller_lock() -> None:
                with control_lock(controller.directory):
                    locked.set()
                    if not finish.wait(TEST_HANG_GUARD_SECS):
                        raise AssertionError('The fixture did not release the controller lock')

            def read_bound(execution: str) -> bool:
                reading.set()
                return execution_bound(execution)

            with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                    mock.patch.object(hana_placement, 'controller_for', return_value=controller),
                    mock.patch.object(controller, 'execution_bound', side_effect=read_bound),
                    ThreadPoolExecutor(max_workers=2) as workers):
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                grant = only_admitted(fixture.connect(fixture.request()).replies())
                lock_owner = workers.submit(hold_controller_lock)
                try:
                    self.assertTrue(locked.wait(1))
                    fixture.proc.kill(wire.Owner(assignment.owner.pid, assignment.owner.start_time))
                    queue_pass = workers.submit(fixture.run_pass)
                    _ = queue_pass.result(timeout=1)
                    self.assertTrue(reading.wait(1))
                    began = time.monotonic()
                    reply = fixture.connect(wire.ReleaseRequest('release-with-controller-lock', grant.reservation, 0)).replies()
                    self.assertEqual(reply, [wire.Ok()])
                    self.assertLess(time.monotonic() - began, 1)
                    self.assertFalse(lock_owner.done())
                    key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                finally:
                    finish.set()
                _ = lock_owner.result(timeout=TEST_HANG_GUARD_SECS)
                worker = fixture.server.host_targets[('ssh', target.name)]
                worker.close()
                worker.thread.join(timeout=1)
                self.assertFalse(worker.thread.is_alive())

    def test_cancel_before_permission_use_settles_and_frees_capacity_with_a_live_coordinator(self) -> None:
        with PlacementFixture() as fixture, mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker):
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
                execution = execution_identity.assigned_claim_id(assignment.episode, assignment.attempt)
                controller.bind_execution(execution)
                reference = placement.ProviderClaimReference('ssh', target.name, assignment.destination.scope,
                    execution, assignment.episode, assignment.attempt)
                _ = fixture.connect(wire.CancelPlacement('cancel-before-consume', request.episode, request.owner, 'cancelled')).replies()
                key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                pending = fixture.server.assignments[key]
                self.assertIsInstance(pending.permission, placement.PermissionAvailable)
                self.assertIsInstance(pending.terminal_report, placement.TerminalAttemptReport)
                assert isinstance(pending.terminal_report, placement.TerminalAttemptReport)
                self.assertIsInstance(pending.terminal_report.path, placement.DispatchPathPending)
                self.assertIsInstance(pending.terminal_report.outcome, placement.AttemptCancelled)
                report = wire.ExecutionReport('cancel-settled', assignment, reference, execution_identity.ExecutionNotStarted(),
                    placement.AttemptCancelled('cancelled'), placement.DispatchPathSettled())
                for invalid in (replace(report, claim=replace(reference, attempt='another-attempt')),
                        replace(report, outcome=placement.AttemptCompleted(0)),
                        replace(report, outcome=placement.AttemptCancelled('another-cancel'))):
                    _ = fixture.connect(invalid).replies()
                    self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
                _ = fixture.connect(wire.ClaimFailure('refusal-after-cancel', assignment, 'disabled')).replies()
                self.assertEqual(fixture.server.assignments[key].terminal_report, pending.terminal_report)
                _ = fixture.connect(report).replies()
                self.assertIsInstance(fixture.server.assignments[key].state, placement.ClosedAssignment)
                self.assertNotIn(request.episode, fixture.server.current_assignments)
                self.assertEqual(fixture.proc.owner_start(request.owner.pid), service.LiveOwnerIdentity(request.owner.start_time))
                next_request = placement_request(fixture)
                next_request = replace(next_request, constraints=replace(next_request.constraints,
                    candidates=request.constraints.candidates))
                _ = publish_candidate(fixture, next_request)
                self.assertEqual(assignment_for(fixture, next_request).destination, assignment.destination)

    def test_cancelled_final_refusal_releases_the_claim_and_admission_after_the_runner_exits(self) -> None:
        cause = 'IgnoredInput: remove the ignored path'
        for outcome in (placement.PrelaunchRefused(cause), execution_identity.HanaCommandAdmissionRefused(cause)):
            with self.subTest(outcome=outcome):
                self.assert_cancelled_final_refusal(outcome)

    def assert_cancelled_final_refusal(self, outcome: placement.PrelaunchRefused |
                                      execution_identity.HanaCommandAdmissionRefused) -> None:
        with PlacementFixture() as fixture, mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker):
            target = hana_target(fixture)
            install_hana_spending_settings(target.settings)
            repo_key = 'https://github.com/natepiano/hana'
            with target.settings.open('a') as settings:
                _ = settings.write(f'execution_transport=hana-control\nrepository_identity={repo_key}\nkinds=check,lint\n'
                                   + '# whole: 1..\nmax_steps=1\n')
            original = placement_request(fixture)
            assert isinstance(original.profile, records.StepProfile)
            destination = placement.PlacementDestination('ssh', target.name, 'fixture-hana',
                claim_state.RepositoryScope('hana', repo_key, 1), 1)
            request = replace(original, profile=replace(original.profile, step='check'),
                prerequisite=placement.TokenNotRequired(),
                constraints=replace(original.constraints, command=('cargo', 'check'), command_kind='check',
                    candidates=(placement.PlacementCandidate(destination),)))
            clock = TestClock()
            directory = fixture.root / 'hana-state'
            provider = FakeProvider(directory, clock)
            controller = hana_box.HanaController(directory, fixture.root / 'aws_target.conf',
                target.settings, provider, clock=clock)
            book = ledger.CostLedger(directory)
            book.record_inventory(ledger.ResourceInventory(clock.now, (), 'private inventory', True))
            book.observe(ledger.InstanceObservation(clock.now, 'terminated', 'private account inventory'))
            controller.register(IDENTITY)
            _ = record_lifecycle_image(directory, target.settings)

            _ = fixture.enter_context(mock.patch.object(target_config, 'read_targets', return_value=(target,)))
            _ = fixture.enter_context(mock.patch.object(hana_placement, 'controller_for', return_value=controller))
            _ = fixture.enter_context(mock.patch.object(hana_placement_adapter, 'controller_for', return_value=controller))
            _ = fixture.enter_context(mock.patch.object(eligibility, 'repository_identity',
                return_value=eligibility.KnownRepository('hana', repo_key)))
            _ = cast(object, fixture.enter_context(mock.patch.dict(os.environ, {'BUILD_TARGETS_STATE_DIR': str(fixture.root / 'targets')})))
            transport = hana_placement.HanaControlTransport(target.settings, destination.host)
            workload = conf.read(target.settings)
            fixture.server.host_probe = lambda key: hana_placement.read_evidence(
                key, request, transport=transport, workload=workload)
            _ = publish_candidate(fixture, request)
            assignment = assignment_for(fixture, request)
            reference = placement.assigned_claim_reference(assignment)
            start = process_start.process_start(os.getpid())
            assert isinstance(start, process_start.Started)
            holder = placement.ClaimHolderIdentity(store.host_name(), os.getpid(), start.start)
            self.assertIsInstance(hana_placement.claim_assigned(assignment, holder, controller), hana_placement.HanaClaimPending)
            for _step in range(4):
                clock.advance(1)
                _ = controller.tick()
            self.assertIsInstance(hana_placement.claim_assigned(assignment, holder, controller), hana_placement.HanaClaimAdopted)
            paths = claim_state.state_paths(target.name)

            with mock.patch.object(controller, 'release', return_value=hana_placement.HanaClaimReleaseRefused('The Hana claim is busy')):
                pending = hana_execution.settle_unpublished_claim(controller.directory, reference,
                    release_claim=lambda: hana_placement.release_assigned(assignment, holder, controller),
                    reason=outcome if isinstance(outcome, execution_identity.HanaCommandAdmissionRefused) else outcome.reason)
            assert isinstance(pending, hana_execution.HanaSettlementPending)
            report = wire.ExecutionReport('source-refused', assignment, reference, execution_identity.ExecutionNotStarted(),
                outcome, placement.DispatchPathPending(pending.reason))
            _ = fixture.connect(report).replies()
            key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
            recorded = fixture.server.assignments[key]
            assert isinstance(recorded.terminal_report, placement.TerminalAttemptReport)
            self.assertEqual(recorded.terminal_report.outcome, outcome)
            if isinstance(outcome, execution_identity.HanaCommandAdmissionRefused):
                self.assertEqual(recorded.retry, placement.DoNotRetry(outcome.reason))
            _ = fixture.connect(wire.CancelPlacement('verify-ended', request.episode, request.owner, 'verify ended')).replies()
            self.assertIsInstance(fixture.server.assignments[key].state, placement.CurrentAssignment)
            self.assertEqual(len(claim_state.read_claims(paths)), 1)

            _ = fixture.run_pass()
            worker = fixture.server.host_targets[('ssh', target.name)]
            assert isinstance(worker, ManualEvidenceWorker)
            _ = worker.probe_next()
            self.assertTrue(hana_execution.claim_dispatch_pending(controller.directory, reference))

            with mock.patch.object(process_start, 'process_start', return_value=process_start.Gone()):
                _ = worker.probe_next()
            _ = fixture.run_pass()

            self.assertFalse(hana_execution.claim_dispatch_pending(controller.directory, reference))
            self.assertEqual(claim_state.read_claims(paths), ())
            snapshot = controller.inspect()
            assert isinstance(snapshot, hana_box.HanaLifecycleEvidence)
            self.assertEqual(snapshot.claims, ())
            self.assertEqual(fixture.server.assignments[key].state, placement.ClosedAssignment('cancelled'))
            self.assertNotIn(request.episode, fixture.server.current_assignments)
            self.assertNotIn(request.episode, fixture.server.waiters)
            self.assertNotIn(request.episode, fixture.caller_reservations())
            self.assertEqual(fixture.proc.owner_start(request.owner.pid), service.LiveOwnerIdentity(request.owner.start_time))

    def test_unbound_refusal_reaches_fallback_after_worker_evidence_with_a_live_coordinator(self) -> None:
        for reason in ('disabled', 'busy'):
            with self.subTest(reason=reason):
                with PlacementFixture() as fixture, mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker):
                    target = hana_target(fixture)
                    controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                        target.settings, hana_box.ProviderNotInstalled())
                    request = placement_request(fixture)
                    candidate = request.constraints.candidates[0]
                    remote = replace(candidate, destination=replace(candidate.destination, provider='ssh', target=target.name))
                    request = replace(request, prerequisite=placement.TokenNotRequired(),
                        constraints=replace(request.constraints, candidates=(remote, local_candidate(fixture))))
                    with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                            mock.patch.object(hana_placement, 'controller_for', return_value=controller)):
                        _ = publish_candidate(fixture, request)
                        assignment = assignment_for(fixture, request)
                        failure = wire.ClaimFailure('refused-before-bind', assignment, reason)
                        _ = fixture.server.handle_request(failure)
                        key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                        pending = fixture.server.assignments[key]
                        self.assertIsInstance(pending.state, placement.CurrentAssignment)
                        self.assertIsInstance(pending.terminal_report, placement.TerminalAttemptReport)
                        fixture.clock.advance(10)
                        _ = fixture.server.handle_request(failure)
                        self.assertEqual(fixture.server.assignments[key].retry, pending.retry)
                        _ = publish_candidate(fixture, request, placement.TemporaryExclusion(reason))
                        _ = fixture.run_pass()
                        worker = fixture.server.host_targets[('ssh', target.name)]
                        assert isinstance(worker, ManualEvidenceWorker)
                        _ = worker.probe_next()
                        _ = fixture.run_pass()
                        self.assertIsInstance(fixture.server.assignments[key].state, placement.ClosedAssignment)
                        self.assertIsInstance(fixture.connect(request).replies()[-1], wire.LocalPlacementGranted)
                        self.assertEqual(fixture.proc.owner_start(request.owner.pid), service.LiveOwnerIdentity(request.owner.start_time))

    def test_prelaunch_refusal_with_an_earlier_claim_settles_only_without_a_bound_dispatch(self) -> None:
        for bound in (False, True):
            with (self.subTest(bound=bound), PlacementFixture() as fixture,
                    mock.patch.object(service, 'HostEvidenceWorker', ManualEvidenceWorker)):
                target = hana_target(fixture)
                controller = hana_box.HanaController(fixture.root / 'hana-state', fixture.root / 'account.conf',
                    target.settings, hana_box.ProviderNotInstalled())
                request = placement_request(fixture)
                candidate = request.constraints.candidates[0]
                remote = replace(candidate, destination=replace(candidate.destination, provider='ssh', target=target.name))
                request = replace(request, prerequisite=placement.TokenNotRequired(),
                    constraints=replace(request.constraints, candidates=(remote, local_candidate(fixture))))
                with (mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                        mock.patch.object(hana_placement, 'controller_for', return_value=controller)):
                    _ = publish_candidate(fixture, request)
                    assignment = assignment_for(fixture, request)
                    if bound:
                        controller.bind_execution(execution_identity.assigned_claim_id(assignment.episode, assignment.attempt))
                    retained = placement.ProviderClaimReference('ssh', target.name, assignment.destination.scope,
                        'retained-earlier-claim', assignment.episode, 'earlier-attempt')
                    _ = fixture.server.handle_request(wire.ClaimFailure('retained-before-bind', assignment, 'busy', retained))
                    key = execution_identity.attempt_key(assignment.episode, assignment.attempt)
                    pending = fixture.server.assignments[key]
                    self.assertIsInstance(pending.state, placement.CurrentAssignment)
                    self.assertIsInstance(pending.permission, placement.PermissionAvailable)
                    self.assertEqual(pending.provider, retained)
                    self.assertIsInstance(pending.terminal_report, placement.TerminalAttemptReport)
                    assert isinstance(pending.terminal_report, placement.TerminalAttemptReport)
                    self.assertIsInstance(pending.terminal_report.path, placement.DispatchPathPending)
                    _ = publish_candidate(fixture, request, placement.TemporaryExclusion('busy'))
                    _ = fixture.run_pass()
                    worker = fixture.server.host_targets[('ssh', target.name)]
                    assert isinstance(worker, ManualEvidenceWorker)
                    _ = worker.probe_next()
                    _ = fixture.run_pass()
                    saved = fixture.server.assignments[key]
                    self.assertEqual(saved.provider, retained)
                    self.assertEqual(saved.retry, pending.retry)
                    self.assertEqual(fixture.proc.owner_start(request.owner.pid), service.LiveOwnerIdentity(request.owner.start_time))
                    if bound:
                        self.assertIsInstance(saved.state, placement.CurrentAssignment)
                        self.assertEqual(fixture.server.current_assignments[request.episode], key)
                    else:
                        self.assertIsInstance(saved.state, placement.ClosedAssignment)
                        self.assertNotEqual(fixture.server.current_assignments.get(request.episode), key)
                        self.assertIsInstance(fixture.connect(request).replies()[-1], wire.LocalPlacementGranted)

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
                read.assert_called_once_with(key, request,
                    transport=hana_placement.HanaControlTransport(settings, 'fixture-hana'),
                    workload=conf.read(settings, load=conf.read_bytes))

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


class HanaGpuQueueTests(unittest.TestCase):
    def test_pinned_route_reports_settled_refusal_without_local_fallback(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO
        import os
        from pathlib import Path
        import subprocess

        from scripts.aws_target import gpu_size_trial
        from scripts.build_targets import build_targets
        from scripts.buildlog import treekey
        from scripts.copies import copies
        from scripts.direct.runner_result import UsableRunnerResult, read_result
        from scripts.direct.step_selection import Pinned

        with PlacementFixture() as fixture:
            settings = fixture.root / 'cloud_gate.conf'
            repository = 'https://github.com/fixture/hana'
            _ = settings.write_text('execution_transport=hana-gpu\n'
                + f'hana_gpu_repository={repository}\nhana_gpu_instance_types=g6.2xlarge\n'
                + 'hana_gpu_selections=hana_mimesis_tools:shader_variants\n# range: 1..60\napi_timeout_s=30\n')
            _ = (fixture.root / 'build_targets.conf').write_text(
                'targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n'
                + 'pinned_targets=hana-gpu\nkind.hana-gpu=ssh\nplatform.hana-gpu=linux\n'
                + 'settings.hana-gpu=cloud_gate.conf\nenabled.hana-gpu=on\n')
            account = target_config.ssh_target_state_directory('aws')
            account.mkdir(parents=True)
            _ = (account / 'gate-deployment.json').write_text('{"gpu_instance_types":["g6.2xlarge"]}')
            pid = os.getpid()
            start = process_start.process_start(pid)
            assert isinstance(start, process_start.Started)
            fixture.proc.register(wire.Owner(pid, start.start))
            initial = placement_request(fixture)
            package = 'hana_mimesis_tools'
            command = ('cargo', 'nextest', 'run', '--no-fail-fast', '--workspace', '--test', 'shader_variants',
                       '-E', 'package(hana_mimesis_tools)')
            source = execution_identity.SourceKeyKnown('source-a')
            reason = 'GPU result could not be collected'
            result_path = fixture.root / 'verify-result'

            with (mock.patch.object(eligibility, 'repository_identity',
                    return_value=eligibility.KnownRepository('hana', repository)),
                    mock.patch.object(treekey, 'tree_key', return_value=source.key),
                    mock.patch.object(copies, 'capture_source', return_value='captured')):
                constraints = build_targets.fixed_placement_constraints(
                    fixture.root, package, 'test', command, initial.constraints.session, source,
                    selection=Pinned('hana-gpu'))
                request = replace(initial, owner=placement.SubmittingOwner(fixture.server.host, pid, start.start),
                    run=wire.RunIdentity(pid, start.start), constraints=constraints, route=target_config.PinnedTarget('hana-gpu'))
                self.assertEqual(constraints.selection, placement.PinnedHost('hana-gpu'))
                candidate, = constraints.candidates
                self.assertEqual(candidate.eligibility, placement.AllowedHost())
                _ = publish_candidate(fixture, request)
                assignment = assignment_for(fixture, request)
                self.assertEqual(assignment.destination.target, 'hana-gpu')
                key = fixture.server.host_evidence_key(request, assignment.destination)

                def publish_evidence(evidence: placement.HostEvidence) -> None:
                    fixture.provider_evidence[key] = evidence
                    fixture.server.publish_host_evidence(evidence)

                def fresh_evidence() -> placement.FreshHostEvidence:
                    evidence = fixture.read_host_evidence(key)
                    assert isinstance(evidence, placement.FreshHostEvidence)
                    return evidence

                evidence_expired = False

                def consume(message: wire.ConsumePermission) -> wire.FirstAuthorization | wire.DispatchStatus:
                    nonlocal evidence_expired
                    if not evidence_expired:
                        evidence_expired = True
                        assert isinstance(message.holder, placement.ClaimHolderIdentity)
                        ineligible = replace(fresh_evidence(), eligibility=placement.FixedExclusion('fixture policy'))
                        self.assertIn(placement.AdoptedProviderClaim(message.claim, message.holder,
                            assignment.session, assignment.destination.host, ineligible.generation), ineligible.claims)
                        publish_evidence(ineligible)
                        denied = fixture.connect(replace(message, request_id='gpu-ineligible')).replies()[-1]
                        assert isinstance(denied, wire.DispatchStatus)
                        self.assertIsInstance(denied.permission, placement.PermissionAvailable)
                        self.assertEqual(denied.reason, 'The Hana GPU step is no longer eligible')

                        stale = placement.StaleHostEvidence(key, fixture.clock.now(), 'provider evidence expired')
                        publish_evidence(stale)
                        refused = fixture.connect(message).replies()[-1]
                        assert isinstance(refused, wire.DispatchStatus)
                        self.assertIsInstance(refused.permission, placement.PermissionAvailable)
                        self.assertEqual(refused.reason, 'adoption evidence is unavailable or stale')
                        return refused
                    publish_evidence(fresh_evidence())
                    answer = fixture.connect(message).replies()[-1]
                    self.assertIsInstance(answer, wire.FirstAuthorization)
                    assert isinstance(answer, wire.FirstAuthorization)
                    return answer

                def decline(_repo: Path, _commit: str, _request: gpu_size_trial.HanaGpuNextestRequest, *,
                            launch: gpu_size_trial.AssignedHanaGpuLaunch) -> gpu_size_trial.HanaGpuNextestResult:
                    launch.before_service()
                    gpu_size_trial.confirm_assigned_gpu_service(launch.trial)
                    folder = gpu_size_trial.trial_root() / launch.trial
                    receipt = gpu_size_trial.cleanup_receipt(folder)
                    receipt.parent.mkdir(parents=True)
                    _ = receipt.write_text('{"trial":"' + launch.trial + '"}')
                    raise ValueError(reason)

                def report(message: wire.ExecutionReport) -> wire.DispatchStatus:
                    self.assertEqual(message.outcome, placement.AttemptDeclined(reason))
                    self.assertEqual(message.path, placement.DispatchPathSettled())
                    self.assertFalse(hana_placement_adapter.gpu_claim_pending('hana-gpu', message.claim.claim_id)
                                     if isinstance(message.claim, placement.ProviderClaimReference) else True)
                    publish_evidence(fresh_evidence())
                    answer = fixture.connect(message).replies()[-1]
                    assert isinstance(answer, wire.DispatchStatus)
                    return answer

                with (mock.patch.object(admission_client, 'consume_permission', new=consume),
                      mock.patch.object(admission_client, 'report_execution', new=report),
                      mock.patch.object(gpu_size_trial, 'run_nextest', new=decline),
                      redirect_stderr(StringIO())):
                    status = hana_placement.run_gpu_assignment(assignment, fixture.root, package, command, result_path)
                self.assertEqual(status, gpu_size_trial.NEXTEST_INFRASTRUCTURE_EXIT)
                result = read_result(result_path, status, False)
                assert isinstance(result, UsableRunnerResult)
                self.assertEqual((result.word, result.reason), ('declined', reason))
                reply = fixture.connect(request).replies()[-1]
                assert isinstance(reply, wire.DispatchStatus)
                self.assertEqual(reply.state, placement.ClosedAssignment(reason))

            shell = Path(__file__).resolve().parents[1] / 'direct/verify.sh'
            script = shell.read_text()
            start_at = script.index('pinned_target_refuse()')
            stop_at = script.index('\nmac_only_refuse()', start_at)
            pinned_refusal = script[start_at:stop_at] + (
                'note_event() { :; }\nPINNED_TARGET=hana-gpu\npinned_target_refuse "$1"')
            refused = subprocess.run(['bash', '-c', pinned_refusal, 'verify.sh', result.reason],
                                     capture_output=True, text=True, timeout=5)
            self.assertEqual((refused.returncode, refused.stderr), (2, f'verify.sh: target hana-gpu refused: {reason}\n'))

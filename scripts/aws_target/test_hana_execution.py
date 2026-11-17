"""Check assigned Hana dispatch, fencing, and recovery without provider calls."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from typing import cast, final, override

from scripts.aws_target import hana_box as box, hana_execution as execution, hana_placement as hana, hana_result as result
from scripts.aws_target import hana_source, hana_transport
from scripts.aws_target.hana_guest import WorkPermit
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_protocol as wire, placement
from scripts.mac_test.claim_state import RepositoryScope


@dataclass
class FakeExecutionObjects:
    request: execution.HanaExecutionRequest
    envelopes: list[execution.HanaExecutionEnvelope] = field(default_factory=list)
    accepted: bool = False
    fenced: bool = False
    terminated: bool = False
    lease_free: bool = False
    delay_acceptance: bool = False
    lose_publication: bool = False
    fail_fence: bool = False
    fail_observation: bool = False
    release_lease: bool = True
    missing_basis: bool = False
    source_updated: bool = False

    def execution_access(self, key: result.HanaAttemptKey) -> execution.HanaExecutionAccess:
        assert key == self.request.key
        return execution.HanaExecutionAccess('https://bucket.s3.us-east-2.amazonaws.com/observation',
                                             'https://bucket.s3.us-east-2.amazonaws.com/result', 2000)

    def accept(self) -> bool:
        if self.fenced:
            return False
        self.accepted = True
        return True

    def publish_execution(self, envelope: execution.HanaExecutionEnvelope) -> None:
        self.envelopes.append(envelope)
        if isinstance(envelope, execution.HanaCommandEnvelope):
            if not self.delay_acceptance:
                _ = self.accept()
            if self.lose_publication:
                raise RuntimeError('The publication reply was lost')
        elif isinstance(envelope, execution.HanaSourceUpdateEnvelope):
            self.source_updated = True
        else:
            if self.fail_fence:
                raise RuntimeError('The fence publication failed')
            self.fenced = True
            self.terminated = True
            self.lease_free = self.release_lease

    def observe_execution(self, key: result.HanaAttemptKey) -> execution.HanaCommandObservation:
        assert key == self.request.key
        if self.fail_observation:
            raise RuntimeError('The observation failed')
        evidence = (placement.ObservedExecution(placement.ProcessExecution('fixture-host',
            hana.generation_text(key.generation), 123, 'fixture-start')) if self.accepted else placement.ExecutionNotStarted())
        if self.missing_basis:
            evidence = placement.ExecutionEvidenceUnavailable('MissingBasis: ' + json.dumps({
                'repository': self.request.manifest.repository, 'commits': self.request.manifest.basis_commits}), 1000)
        return execution.HanaCommandObservation(key, self.request.slot, self.accepted, self.fenced,
            self.terminated, self.lease_free, 1000, evidence, self.source_updated)

    def execution_result(self, key: result.HanaAttemptKey) -> bytes:
        assert key == self.request.key
        raise RuntimeError('The fake result requires explicit test input')


@final
class HanaExecutionTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.root = Path()
        self.key = result.HanaAttemptKey('fixture-episode', 'fixture-attempt',
            placement.assigned_claim_id('fixture-episode', 'fixture-attempt'), hana.HanaHostGeneration('deployment', 'start', 'boot'))
        self.source_key = placement.SourceKeyKnown('verify-source')
        scope = RepositoryScope('hana', 'github.com/natepiano/hana', 1)
        self.assignment = placement.PlacementAssignment(self.key.episode, self.key.attempt,
            placement.PlacementDestination('ssh', 'configured-target', 'fixture-host', scope, 1),
            self.source_key, placement.SubmittingOwner('local', 10, 'coordinator-start'), NoOwningSession(), 900)
        self.reference = placement.ProviderClaimReference('ssh', self.assignment.destination.target, scope,
            self.key.claim_id, self.key.episode, self.key.attempt)
        self.holder = placement.ClaimHolderIdentity('local', 11, 'holder-start')
        self.adopted = hana.HanaClaimAdopted(placement.ReservedHost(self.assignment,
            placement.AdoptedProviderClaim(self.reference, self.holder, self.assignment.session,
                                          'fixture-host', hana.generation_text(self.key.generation))), self.key.generation)
        self.manifest = hana_source.SourceManifest(1, 'hana-source', 'a' * 40, 'b' * 64,
            'https://bucket.s3.us-east-2.amazonaws.com/source', (), ('cargo', 'check'), (), (), 'c' * 64)
        self.request = execution.HanaExecutionRequest(self.key, self.manifest, 1, self.source_key,
            self.manifest.argv, result.LogicalStepLink('call', self.key.episode))
        self.objects = FakeExecutionObjects(self.request)
        self.permission = box.CommandPermission(self.key.claim_id, box.VerifiedBoot('boot', 'start'), 3, 1800)
        self.authorizations = 0
        self.releases = 0

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def consume(self, request: wire.ConsumePermission) -> wire.FirstAuthorization:
        self.authorizations += 1
        self.assertEqual(request.assignment, self.assignment)
        self.assertEqual(request.claim, self.reference)
        self.assertEqual(request.holder, self.holder)
        self.assertTrue(execution.execution_path(self.root, self.key.claim_id).exists())
        return wire.FirstAuthorization(request.assignment, request.claim)

    def dispatch(self, *, consume: Callable[[wire.ConsumePermission], wire.FirstAuthorization | wire.DispatchStatus] | None = None
                 ) -> execution.HanaDispatchState:
        return execution.dispatch_execution(self.root, self.request, self.adopted, self.objects,
            consume=self.consume if consume is None else consume, current_permission=lambda: self.permission,
            current_source_key=lambda: self.source_key)

    def release(self) -> bool:
        self.releases += 1
        self.assertTrue(execution.execution_command_settled(self.root, self.key.claim_id))
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
        return True

    def settle(self, *, cancelled: bool = False) -> execution.HanaSettlement:
        return execution.settle_execution(self.root, self.request, self.objects, release_claim=self.release,
                                           reason='The remote attempt ended', cancelled=cancelled)

    def command_result(self, status: int = 0) -> result.HanaCommandResult:
        return result.HanaCommandResult(self.key, self.source_key.key, self.manifest.commit, self.manifest.toolchain,
            'fixture-host', self.request.argv, 1, status, b'compiler output', b'diagnostics', 1000, 1010, 10,
            result.PeakUnavailable('The fake has no memory measurement'))

    def test_codecs_preserve_identity_permission_and_scoped_access(self) -> None:
        envelope = execution.HanaCommandEnvelope(self.request,
            WorkPermit(1800, '3', 'boot'), self.objects.execution_access(self.key))
        self.assertEqual(execution.decode_request(execution.encode_request(self.request)), self.request)
        self.assertEqual(execution.decode_envelope(execution.encode_envelope(envelope)), envelope)
        observation = self.objects.observe_execution(self.key)
        self.assertEqual(execution.decode_observation(execution.encode_observation(observation)), observation)
        self.assertNotIn(b'observation_url', execution.encode_request(self.request))

    def test_codecs_reject_wrong_claim_source_command_and_boolean_slot(self) -> None:
        for request in (replace(self.request, key=replace(self.key, claim_id='wrong')),
                        replace(self.request, argv=('cargo', 'clippy')), replace(self.request, slot=True)):
            with self.subTest(request=request), self.assertRaises(ValueError):
                _ = execution.encode_request(request)
        with self.assertRaises(ValueError):
            _ = execution.decode_envelope(b'x' * (execution.CONTROL_MAX_BYTES + 1))

    def test_dispatch_consumes_once_and_never_republishes_after_lost_acknowledgement(self) -> None:
        self.objects.lose_publication = True
        self.assertIsInstance(self.dispatch(), execution.DispatchSent)
        self.assertIsInstance(self.dispatch(), execution.DispatchSent)
        self.assertEqual(self.authorizations, 1)
        self.assertEqual(len(self.objects.envelopes), 1)
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
        self.assertIsInstance(execution.read_claim_execution(self.root, self.reference), placement.ExecutionEvidenceUnavailable)
        self.assertIsInstance(execution.observe_execution(self.root, self.request, self.objects), placement.ObservedExecution)
        self.assertIsInstance(self.settle(), execution.HanaSettled)
        self.assertFalse(execution.claim_dispatch_pending(self.root, self.reference))

    def test_lost_authorization_starts_zero_commands_and_closes_only_the_original_dispatch(self) -> None:
        def lost(request: wire.ConsumePermission) -> wire.FirstAuthorization:
            _ = self.consume(request)
            raise wire.AdmissionUnavailable('The first authorization reply was lost')
        _ = self.dispatch(consume=lost)
        _ = self.dispatch()
        self.assertEqual(self.authorizations, 1)
        self.assertEqual(self.objects.envelopes, [])
        settled = self.settle()
        self.assertIsInstance(settled, execution.HanaSettled)
        assert isinstance(settled, execution.HanaSettled)
        self.assertIsInstance(settled.outcome, placement.PrelaunchRefused)

    def test_status_only_permission_never_launches(self) -> None:
        status = wire.DispatchStatus(self.assignment, placement.CurrentAssignment(), placement.PermissionConsumed(1000))
        _ = self.dispatch(consume=lambda request: status)
        self.assertEqual(self.objects.envelopes, [])
        self.assertIsInstance(self.settle(), execution.HanaSettled)

    def test_a_fence_acknowledgement_blocks_delayed_command_acceptance(self) -> None:
        self.objects.delay_acceptance = True
        _ = self.dispatch()
        self.assertIsInstance(self.settle(), execution.HanaSettled)
        self.assertFalse(self.objects.accept())
        self.assertFalse(self.objects.accepted)

    def test_failed_fence_missing_observation_or_held_lease_retains_cleanup(self) -> None:
        _ = self.dispatch()
        self.objects.fail_fence = True
        self.assertIsInstance(self.settle(), execution.HanaSettlementPending)
        self.objects.fail_fence = False
        self.objects.fail_observation = True
        self.assertIsInstance(self.settle(), execution.HanaSettlementPending)
        self.objects.fail_observation = False
        self.objects.release_lease = False
        self.assertIsInstance(self.settle(), execution.HanaSettlementPending)
        self.assertEqual(self.releases, 0)
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
        self.objects.release_lease = True
        self.assertIsInstance(self.settle(), execution.HanaSettled)

    def test_cancellation_and_restart_keep_one_settlement(self) -> None:
        _ = self.dispatch()
        first = self.settle(cancelled=True)
        self.assertIsInstance(first, execution.HanaSettled)
        assert isinstance(first, execution.HanaSettled)
        self.assertIsInstance(first.outcome, placement.AttemptCancelled)
        self.assertEqual(self.settle(), first)
        self.assertEqual(self.releases, 1)

    def test_nonzero_compiler_result_is_terminal_and_replay_is_idempotent(self) -> None:
        _ = self.dispatch()
        content = result.encode_result(self.command_result(1))
        accepted = execution.accept_result(self.root, self.request, content, 'fixture-host', lambda: self.source_key)
        self.assertIsInstance(accepted, result.AcceptedHanaResult)
        self.assertEqual(execution.accept_result(self.root, self.request, content, 'fixture-host', lambda: self.source_key), accepted)
        settled = self.settle()
        self.assertIsInstance(settled, execution.HanaSettled)
        assert isinstance(settled, execution.HanaSettled)
        self.assertEqual(settled.outcome, placement.AttemptCompleted(1))
        self.assertEqual(execution.accept_result(self.root, self.request, content, 'fixture-host', lambda: self.source_key), accepted)

    def test_edited_replaced_and_fenced_results_never_become_accepted(self) -> None:
        _ = self.dispatch()
        for remote, live in ((self.command_result(), placement.SourceKeyKnown('edited')),
                             (replace(self.command_result(), key=replace(self.key, attempt='replacement')), self.source_key),
                             (replace(self.command_result(), argv=('cargo', 'clippy')), self.source_key)):
            with self.subTest(remote=remote, live=live):
                decision = execution.accept_result(self.root, self.request, result.encode_result(remote), 'fixture-host', lambda: live)
                self.assertIsInstance(decision, result.RejectedHanaResult)
        _ = self.settle()
        decision = execution.accept_result(self.root, self.request, result.encode_result(self.command_result()),
                                           'fixture-host', lambda: self.source_key)
        self.assertIsInstance(decision, result.RejectedHanaResult)

    def test_missing_corrupt_and_mismatched_journals_never_prove_departure(self) -> None:
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
        _ = self.dispatch()
        self.assertTrue(execution.claim_dispatch_pending(self.root, replace(self.reference, attempt='other')))
        _ = execution.execution_path(self.root, self.key.claim_id).write_text('{')
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
        self.assertIsInstance(execution.read_claim_execution(self.root, self.reference), placement.ExecutionEvidenceUnavailable)

    def test_source_capture_rechecks_live_source_before_permission(self) -> None:
        sender = cast(hana_transport.SourceSender, mock.create_autospec(hana_transport.SourceSender, instance=True))
        reads = iter((self.source_key, placement.SourceKeyKnown('edited')))
        with mock.patch.object(sender, 'prepare', return_value=self.manifest):
            prepared = execution.prepare_execution(self.adopted, sender, self.root, self.request.argv, self.request.link,
                current_source_key=lambda: next(reads), environment={})
        self.assertIsInstance(prepared, execution.ExecutionPreparationRefused)
        self.assertEqual(self.authorizations, 0)
        self.assertEqual(self.objects.envelopes, [])

    def test_source_change_after_authorization_prevents_publication(self) -> None:
        def changed(request: wire.ConsumePermission) -> wire.FirstAuthorization:
            authorized = self.consume(request)
            self.source_key = placement.SourceKeyKnown('edited')
            return authorized
        _ = self.dispatch(consume=changed)
        self.assertEqual(self.objects.envelopes, [])
        self.assertIsInstance(self.settle(), execution.HanaSettled)

    def test_an_unpublished_refusal_needs_no_boot_identity_or_provider_call(self) -> None:
        settled = execution.settle_unpublished_claim(self.root, self.reference, release_claim=self.release,
                                                    reason='The source capture failed')
        self.assertIsInstance(settled, execution.HanaSettled)
        self.assertFalse(execution.claim_dispatch_pending(self.root, self.reference))
        self.assertEqual(self.objects.envelopes, [])

    def test_refusal_cannot_replace_an_existing_executable_dispatch(self) -> None:
        _ = self.dispatch()
        refused = execution.settle_unpublished_claim(self.root, self.reference, release_claim=self.release,
                                                    reason='The holder ended')
        self.assertIsInstance(refused, execution.HanaSettlementPending)
        self.assertEqual(self.releases, 0)

    def test_settlement_during_authorization_prevents_a_delayed_publication(self) -> None:
        def settled_during_authorization(request: wire.ConsumePermission) -> wire.FirstAuthorization:
            authorized = self.consume(request)
            self.assertIsInstance(self.settle(), execution.HanaSettled)
            return authorized
        state = self.dispatch(consume=settled_during_authorization)
        self.assertIsInstance(state, execution.DispatchSettled)
        self.assertEqual(self.objects.envelopes, [])

    def test_transfer_charge_replay_keeps_measured_counts_without_new_dollar_totals(self) -> None:
        from scripts.aws_target.ledger import CostLedger

        _ = self.dispatch()
        _ = self.settle()
        book = CostLedger(self.root)
        with book.transaction():
            pass
        execution.record_attempt_charge(self.root, self.request,
            [{'operation': 'hana-control-put', 'sent_bytes': 200, 'received_bytes': 100, 'complete': True}], 1000, 1010)
        execution.record_attempt_charge(self.root, self.request, [], 1000, 1100)
        charges = book.read_snapshot().hana_attempts
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges[0].uploaded_bytes, 200)
        self.assertEqual(charges[0].downloaded_bytes, 100)
        self.assertEqual(charges[0].requests, 1)

    def test_complete_source_update_keeps_capture_and_never_repeats_command_authority(self) -> None:
        self.manifest = replace(self.manifest, basis_commits=('d' * 40,))
        self.request = replace(self.request, manifest=self.manifest)
        self.objects.request = self.request
        self.objects.missing_basis = True
        self.objects.terminated = True
        self.objects.lease_free = True
        _ = self.dispatch()
        _ = execution.observe_execution(self.root, self.request, self.objects)
        complete = replace(self.manifest, basis_commits=(), bundle_sha256='e' * 64)
        self.assertTrue(execution.publish_source_update(self.root, self.request, complete, self.objects))
        self.assertFalse(execution.publish_source_update(self.root, self.request, complete, self.objects))
        update = self.objects.envelopes[-1]
        self.assertIsInstance(update, execution.HanaSourceUpdateEnvelope)
        self.assertEqual(execution.decode_envelope(execution.encode_envelope(update)), update)
        self.assertEqual(sum(isinstance(envelope, execution.HanaCommandEnvelope) for envelope in self.objects.envelopes), 1)
        self.assertEqual(self.authorizations, 1)
        self.assertEqual(complete.commit, self.manifest.commit)
        _ = execution.observe_execution(self.root, self.request, self.objects)
        observation = execution.read_dispatch_observation(self.root, self.request)
        self.assertIsInstance(observation, execution.HanaCommandObservation)
        assert isinstance(observation, execution.HanaCommandObservation)
        self.assertTrue(observation.source_updated)
        self.assertIsInstance(self.settle(), execution.HanaSettled)

    def test_source_update_cannot_change_the_captured_commit_or_command(self) -> None:
        _ = self.dispatch()
        for manifest in (replace(self.manifest, commit='e' * 40), replace(self.manifest, argv=('cargo', 'clippy')),
                         replace(self.manifest, basis_commits=('d' * 40,))):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                _ = execution.publish_source_update(self.root, self.request, manifest, self.objects)
        self.assertEqual(len(self.objects.envelopes), 1)


if __name__ == '__main__':
    _ = unittest.main()

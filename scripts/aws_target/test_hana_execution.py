"""Check assigned Hana dispatch, fencing, and recovery without provider calls."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
from typing import cast, final, override

from scripts.build_queue import execution_identity
from scripts.aws_target import hana_protocol
from scripts.aws_target import hana_box as box, hana_execution as execution, hana_placement as hana, hana_result as result
from scripts.aws_target import hana_transport
from scripts.aws_target.hana_guest import CommandAdmissionRefusalCode, WorkPermit
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_protocol as wire, placement
from scripts.mac_test.claim_state import RepositoryScope
from scripts.aws_target.ledger import CostLedger


@dataclass
class FakeExecutionObjects:
    request: hana_protocol.HanaExecutionRequest
    envelopes: list[hana_protocol.HanaExecutionEnvelope] = field(default_factory=list)
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

    def boot_control_deadline(self, key: result.HanaAttemptKey) -> int:
        assert key == self.request.key
        return 10**10

    def execution_access(self, key: result.HanaAttemptKey) -> hana_protocol.HanaExecutionAccess:
        assert key == self.request.key
        return hana_protocol.HanaExecutionAccess('https://bucket.s3.us-east-2.amazonaws.com/observation',
                                             'https://bucket.s3.us-east-2.amazonaws.com/result', 2000)

    def accept(self) -> bool:
        if self.fenced:
            return False
        self.accepted = True
        return True

    def publish_execution(self, content: bytes) -> None:
        envelope = hana_protocol.decode_envelope(content)
        self.envelopes.append(envelope)
        if isinstance(envelope, hana_protocol.HanaCommandEnvelope):
            if not self.delay_acceptance:
                _ = self.accept()
            if self.lose_publication:
                raise RuntimeError('The publication reply was lost')
        elif isinstance(envelope, hana_protocol.HanaSourceUpdateEnvelope):
            self.source_updated = True
        else:
            if self.fail_fence:
                raise RuntimeError('The fence PUT failed')
            self.fenced = True
            self.terminated = True
            self.lease_free = self.release_lease

    def observe_execution(self, key: result.HanaAttemptKey, *,
                          timeout_s: float | None = None) -> hana_protocol.HanaCommandObservation:
        assert key == self.request.key
        assert timeout_s is None or timeout_s > 0
        if self.fail_observation:
            raise RuntimeError('The observation failed')
        evidence = (execution_identity.ObservedExecution(execution_identity.ProcessExecution('fixture-host',
            hana.generation_text(key.generation), 123, 'fixture-start')) if self.accepted else execution_identity.ExecutionNotStarted())
        if self.missing_basis:
            evidence = execution_identity.ExecutionEvidenceUnavailable('MissingBasis: ' + json.dumps({
                'repository': self.request.manifest.repository, 'commits': self.request.manifest.basis_commits}), 1000)
        return hana_protocol.HanaCommandObservation(key, self.request.slot, self.accepted, self.fenced,
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
            execution_identity.assigned_claim_id('fixture-episode', 'fixture-attempt'), hana.HanaHostGeneration('deployment', 'start', 'boot'))
        self.source_key = execution_identity.SourceKeyKnown('verify-source')
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
        self.manifest = hana_protocol.SourceManifest(1, 'hana-source', 'a' * 40, 'b' * 64,
            'https://bucket.s3.us-east-2.amazonaws.com/source', (), ('cargo', 'check'), (), (), 'c' * 64,
            runtime_abi=hana_protocol.RUNTIME_ABI)
        self.request = hana_protocol.HanaExecutionRequest(self.key, self.manifest, 1, self.source_key,
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

    def send_dispatch(self, *, consume: Callable[[wire.ConsumePermission], wire.FirstAuthorization | wire.DispatchStatus],
                      upload_source: Callable[[], None]
                      ) -> execution.HanaDispatchState | execution_identity.HanaCommandAdmissionRefused:
        return execution.dispatch_execution(self.root, self.request, self.adopted, self.objects,
            upload_source=upload_source, consume=consume, current_permission=lambda: self.permission,
            current_source_key=lambda: self.source_key)

    def dispatch(self, *, consume: Callable[[wire.ConsumePermission], wire.FirstAuthorization] | None = None,
                 upload_source: Callable[[], None] | None = None
                 ) -> execution.HanaDispatchState:
        state = self.send_dispatch(consume=self.consume if consume is None else consume,
                                   upload_source=(lambda: None) if upload_source is None else upload_source)
        assert not isinstance(state, execution_identity.HanaCommandAdmissionRefused)
        return state

    def release(self) -> hana.HanaClaimRelease:
        self.releases += 1
        self.assertTrue(execution.execution_command_settled(self.root, self.key.claim_id))
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
        return hana.HanaClaimReleased()

    def settle(self, *, cancelled: bool = False, reason: str = 'The remote attempt ended') -> execution.HanaSettlement:
        current = 1000.0

        def advance(seconds: float) -> None:
            nonlocal current
            current += seconds

        window = execution.FenceAcknowledgementWindow(1001, 1)
        with (mock.patch.object(time, 'monotonic', side_effect=lambda: current),
              mock.patch.object(time, 'sleep', side_effect=advance)):
            return execution.settle_execution(self.root, self.request, self.objects, release_claim=self.release,
                reason=reason, fence_window=window, cancelled=cancelled)

    def command_result(self, status: int = 0) -> result.HanaCommandResult:
        return result.HanaCommandResult(self.key, self.source_key.key, self.manifest.commit, self.manifest.toolchain,
            'fixture-host', self.request.argv, 1, status, b'compiler output', b'diagnostics', 1000, 1010, 10,
            result.PeakUnavailable('The fake has no memory measurement'))

    def test_codecs_preserve_identity_permission_and_scoped_access(self) -> None:
        envelope = hana_protocol.HanaCommandEnvelope(self.request,
            WorkPermit(1800, '3', 'boot'), self.objects.execution_access(self.key))
        self.assertEqual(hana_protocol.decode_request(hana_protocol.encode_request(self.request)), self.request)
        self.assertEqual(hana_protocol.decode_envelope(hana_protocol.encode_envelope(envelope)), envelope)
        observation = self.objects.observe_execution(self.key)
        self.assertEqual(hana_protocol.decode_observation(hana_protocol.encode_observation(observation)), observation)
        self.assertNotIn(b'observation_url', hana_protocol.encode_request(self.request))
        no_code = None
        rows = (
            (CommandAdmissionRefusalCode.ADMISSION_DISABLED, 'Guest policy refused the command', True),
            (CommandAdmissionRefusalCode.DATA_NOT_MOUNTED, 'The Hana data directory is not mounted', True),
            (CommandAdmissionRefusalCode.PERMIT_REJECTED, 'The Hana permit is not fresh: the wall-clock expiry has passed', True),
            (CommandAdmissionRefusalCode.PRE_COMMAND_FAILED, 'The Hana runtime ABI 2 differs from image ABI 1', True),
            (no_code, 'The data mount and a fresh permit are required', True),
            (no_code, 'The data mount and a fresh permit are required during setup', False),
            (no_code, 'Hana command admission is disabled on the guest', False),
            (no_code, 'The Hana data directory is not mounted', False),
            (no_code, 'The Hana permit is not fresh: unrelated diagnostic', False),
        )
        for code, reason, refused in rows:
            with self.subTest(code=code, reason=reason):
                reply = (hana_protocol.ConfirmedCommandAdmissionRefusal(
                    observation.key, observation.slot, observation.fenced, observation.observed_at, reason, code)
                    if code is not None else replace(observation, accepted=True, terminated=True, lease_free=True,
                        evidence=execution_identity.ExecutionEvidenceUnavailable(reason, 1000)))
                decoded = hana_protocol.decode_observation(hana_protocol.encode_observation(reply))
                self.assertEqual(decoded, reply)
                expected = (execution_identity.HanaCommandAdmissionRefused(reason) if refused
                            else hana_protocol.NoCommandAdmissionRefusal())
                self.assertEqual(hana_protocol.command_admission_refusal(decoded), expected)
        invalid = hana_protocol.encode_observation(replace(observation, evidence=execution_identity.ExecutionEvidenceUnavailable('refused', 1000)))
        with self.assertRaises(ValueError):
            _ = hana_protocol.decode_observation(invalid.replace(
                b'"kind":"unavailable"', b'"kind":"unavailable","refusal_code":"invalid"'))

        confirmed = hana_protocol.ConfirmedCommandAdmissionRefusal(
            observation.key, observation.slot, False, observation.observed_at, 'The payload could not start',
            CommandAdmissionRefusalCode.PRE_COMMAND_FAILED)
        raw = hana_protocol.encode_observation(confirmed)
        for field in (b'accepted', b'terminated', b'lease_free'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'confirmed departure'):
                _ = hana_protocol.decode_observation(raw.replace(b'"' + field + b'":true', b'"' + field + b'":false'))

    def test_codecs_reject_wrong_claim_source_command_and_boolean_slot(self) -> None:
        for request in (replace(self.request, key=replace(self.key, claim_id='wrong')),
                        replace(self.request, argv=('cargo', 'clippy')), replace(self.request, slot=True)):
            with self.subTest(request=request), self.assertRaises(ValueError):
                _ = hana_protocol.encode_request(request)
        with self.assertRaises(ValueError):
            _ = hana_protocol.decode_envelope(b'x' * (hana_protocol.CONTROL_MAX_BYTES + 1))

    def test_dispatch_consumes_once_and_never_republishes_after_lost_acknowledgement(self) -> None:
        self.objects.lose_publication = True
        self.assertIsInstance(self.dispatch(), execution.DispatchSent)
        self.assertIsInstance(self.dispatch(), execution.DispatchSent)
        self.assertEqual(self.authorizations, 1)
        self.assertEqual(len(self.objects.envelopes), 1)
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
        self.assertIsInstance(execution.read_claim_execution(self.root, self.reference), execution_identity.ExecutionEvidenceUnavailable)
        self.assertIsInstance(execution.observe_execution(self.root, self.request, self.objects), execution_identity.ObservedExecution)
        self.assertIsInstance(self.settle(), execution.HanaSettled)
        self.assertFalse(execution.claim_dispatch_pending(self.root, self.reference))

    def test_execution_journal_keeps_the_guest_result_timing(self) -> None:
        self.assertIsInstance(self.dispatch(), execution.DispatchSent)
        timing: hana_protocol.HanaCommandTiming = {
            'unit_exited_at': 1011, 'unit_exit_observed_at': 1011.25,
            'lease_free_at': 1011.5, 'upload_started_at': 1012,
        }
        observation = replace(self.objects.observe_execution(self.key), terminated=True, lease_free=True,
                              observed_at=1013, timing=timing)
        reply = hana_protocol.decode_observation(hana_protocol.encode_observation(observation))
        with mock.patch.object(self.objects, 'observe_execution', return_value=reply):
            self.assertEqual(execution.observe_execution(self.root, self.request, self.objects), observation.evidence)
        path = execution.execution_path(self.root, self.key.claim_id)
        row = result.object_fields(cast(object, json.loads(path.read_text())))
        self.assertEqual(result.object_fields(row['observation'])['timing'], timing)
        self.assertEqual(execution.read_dispatch_observation(self.root, self.request), observation)

    def test_lost_authorization_starts_zero_commands_and_closes_only_the_original_dispatch(self) -> None:
        def lost(request: wire.ConsumePermission) -> wire.FirstAuthorization:
            _ = self.consume(request)
            raise wire.AdmissionUnavailable('The first authorization reply was lost')
        self.assertEqual(self.send_dispatch(consume=lost, upload_source=lambda: None),
            execution_identity.HanaCommandAdmissionRefused('Hana authorization failed: The first authorization reply was lost'))
        self.assertEqual(self.send_dispatch(consume=self.consume, upload_source=lambda: None),
            execution_identity.HanaCommandAdmissionRefused('The saved Hana dispatch ended before a publication attempt'))
        self.assertEqual(self.authorizations, 1)
        self.assertEqual(self.objects.envelopes, [])
        settled = self.settle()
        self.assertIsInstance(settled, execution.HanaSettled)
        assert isinstance(settled, execution.HanaSettled)
        self.assertIsInstance(settled.outcome, placement.PrelaunchRefused)

    def test_prepublication_refusal_never_launches(self) -> None:
        root = self.root
        journal_failure = 'The Hana dispatch journal is unavailable: fixture journal write failed'
        cases = (
            ('status only', 'The admission service returned status without a first authorization', 1, 0),
            ('consume refused', 'adoption evidence is unavailable: the Hana claim record is missing', 1, 0),
            ('authorization mismatch', 'The Hana authorization differs from the adopted assignment or claim', 1, 0),
            ('changed source', 'The live source key no longer matches before Hana authorization', 0, 0),
            ('unconfirmed holder', 'The Hana claim has no confirmed holder', 0, 0),
            ('permit mismatch', 'The Hana command permission differs from the adopted claim', 1, 0),
            ('source changed during upload', 'The live source key no longer matches after Hana upload', 1, 1),
            ('prepared write', journal_failure, 0, 0),
            ('sent write', journal_failure, 0, 0),
            ('publication mark', journal_failure, 1, 1),
        )
        save = cast(Callable[[Path, str, dict[str, object]], None], getattr(execution, '_save_journal'))
        for case, reason, consumes, uploads in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=root) as folder:
                self.root = Path(folder)
                self.source_key = self.request.source_key
                adopted = self.adopted
                permission = self.permission
                authorization: wire.DispatchStatus | wire.FirstAuthorization = wire.FirstAuthorization(self.assignment, self.reference)
                changed_source = execution_identity.SourceKeyKnown('edited')
                upload = mock.Mock()
                if case == 'status only':
                    authorization = wire.DispatchStatus(self.assignment, placement.CurrentAssignment(), placement.PermissionConsumed(1000))
                elif case == 'consume refused':
                    authorization = wire.DispatchStatus(self.assignment, placement.CurrentAssignment(),
                        placement.PermissionAvailable(), reason)
                elif case == 'authorization mismatch':
                    authorization = wire.FirstAuthorization(self.assignment, replace(self.reference, claim_id='another-claim'))
                elif case == 'changed source':
                    self.source_key = changed_source
                elif case == 'unconfirmed holder':
                    claim = replace(adopted.reserved.claim, holder=placement.ClaimHolderUnconfirmed('unknown'))
                    adopted = replace(adopted, reserved=replace(adopted.reserved, claim=claim))
                elif case == 'permit mismatch':
                    permission = replace(permission, execution='another-claim')
                elif case == 'source changed during upload':
                    def change_source() -> None:
                        self.source_key = changed_source
                    upload.side_effect = change_source
                consume = mock.Mock(return_value=authorization)

                def save_with_failure(directory: Path, execution_id: str, row: dict[str, object]) -> None:
                    stage = 'publication mark' if row['publication_attempted'] else f"{row['phase']} write"
                    if case == stage:
                        raise OSError('fixture journal write failed')
                    save(directory, execution_id, row)

                with mock.patch.object(execution, '_save_journal', side_effect=save_with_failure):
                    self.assertEqual(execution.dispatch_execution(self.root, self.request, adopted, self.objects,
                        upload_source=upload, consume=consume, current_permission=lambda: permission,
                        current_source_key=lambda: self.source_key), execution_identity.HanaCommandAdmissionRefused(reason))
                self.assertEqual(consume.call_count, consumes)
                if uploads == 0:
                    upload.assert_not_called()
                else:
                    upload.assert_called_once()
                self.assertEqual(self.objects.envelopes, [])
                if case == 'prepared write':
                    self.assertFalse(execution.execution_path(self.root, self.key.claim_id).exists())
                else:
                    self.assertIsInstance(self.settle(), execution.HanaSettled)

    def test_dispatch_uses_the_permit_renewed_during_source_upload(self) -> None:
        renewed = replace(self.permission, generation=4, expires=1900)

        def upload() -> None:
            self.assertEqual(self.authorizations, 1)
            self.permission = renewed

        self.assertIsInstance(self.dispatch(upload_source=upload), execution.DispatchSent)
        envelope, = self.objects.envelopes
        assert isinstance(envelope, hana_protocol.HanaCommandEnvelope)
        self.assertEqual(envelope.permission, WorkPermit(renewed.expires, str(renewed.generation), renewed.boot.boot_id))

    def test_a_fence_acknowledgement_blocks_delayed_command_acceptance(self) -> None:
        self.objects.delay_acceptance = True
        _ = self.dispatch()
        self.assertIsInstance(self.settle(), execution.HanaSettled)
        self.assertFalse(self.objects.accept())
        self.assertFalse(self.objects.accepted)

    def test_fence_settlement_saves_the_upload_start_time_in_the_execution_journal(self) -> None:
        _ = self.dispatch()
        observe = self.objects.observe_execution
        initial = replace(observe(self.key), timing={'unit_exited_at': 997})
        with mock.patch.object(self.objects, 'observe_execution', return_value=initial):
            _ = execution.observe_execution(self.root, self.request, self.objects)
        timing: hana_protocol.HanaCommandTiming = {'upload_started_at': 998}

        def observe_fence(key: result.HanaAttemptKey, *,
                          timeout_s: float | None = None) -> hana_protocol.HanaCommandObservation:
            observation = observe(key, timeout_s=timeout_s)
            self.assertTrue(observation.fenced)
            return replace(observation, timing=timing)

        with mock.patch.object(self.objects, 'observe_execution', side_effect=observe_fence):
            self.assertIsInstance(self.settle(), execution.HanaSettled)
        saved = execution.read_dispatch_observation(self.root, self.request)
        assert isinstance(saved, hana_protocol.HanaCommandObservation)
        self.assertTrue(saved.fenced)
        self.assertEqual(saved.timing, {'unit_exited_at': 997, 'upload_started_at': 998})

    def test_failed_fence_missing_observation_or_held_lease_retains_cleanup(self) -> None:
        _ = self.dispatch()
        self.objects.fail_fence = True
        self.assertIsInstance(self.settle(), execution.HanaSettlementPending)
        observation = replace(self.objects.observe_execution(self.key), terminated=True, lease_free=True,
                              evidence=execution_identity.ExecutionEvidenceUnavailable(hana_protocol.COMMAND_RESULT_PENDING, 1000))
        with mock.patch.object(self.objects, 'observe_execution', return_value=observation):
            _ = execution.observe_execution(self.root, self.request, self.objects)
        self.assertIsInstance(self.settle(), execution.HanaSettlementPending)
        self.objects.fail_fence = False
        self.objects.fail_observation = True
        self.assertIsInstance(self.settle(), execution.HanaSettlementPending)
        self.objects.fail_observation = False
        self.objects.release_lease = False
        current = 1000.0
        timeouts: list[float | None] = []
        observe = self.objects.observe_execution

        def observe_held_lease(key: result.HanaAttemptKey, *,
                               timeout_s: float | None = None) -> hana_protocol.HanaCommandObservation:
            timeouts.append(timeout_s)
            return observe(key, timeout_s=timeout_s)

        def advance(seconds: float) -> None:
            nonlocal current
            current += seconds

        with (mock.patch.object(time, 'monotonic', side_effect=lambda: current),
              mock.patch.object(time, 'sleep', side_effect=advance),
              mock.patch.object(self.objects, 'observe_execution', side_effect=observe_held_lease)):
            window = execution.FenceAcknowledgementWindow(1002.5, 1)
            self.assertIsInstance(execution.settle_execution(self.root, self.request, self.objects,
                release_claim=self.release, reason='The remote attempt ended', fence_window=window),
                execution.HanaSettlementPending)
        self.assertEqual(timeouts, [2.5, 1.5, 0.5])
        self.assertEqual(current, window.deadline)
        self.assertEqual(self.releases, 0)
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
        self.objects.release_lease = True
        self.assertIsInstance(self.settle(), execution.HanaSettled)
        self.assertEqual(self.releases, 1)

        with self.subTest('Guest admission refusal before claim release'):
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=self.root)))
            self.objects = FakeExecutionObjects(self.request, fail_fence=True)
            _ = self.dispatch()
            reason = 'The data mount and a fresh permit are required'
            observation = replace(self.objects.observe_execution(self.request.key), terminated=True, lease_free=True,
                                  evidence=execution_identity.ExecutionEvidenceUnavailable(reason, 1000))
            with mock.patch.object(self.objects, 'observe_execution', return_value=observation):
                _ = execution.observe_execution(self.root, self.request, self.objects)
            settled = execution.HanaAdmissionRefusalSettled(execution_identity.HanaCommandAdmissionRefused(reason),
                                                            execution_identity.ExecutionNotStarted())
            self.assertEqual(self.settle(), settled)
            self.assertEqual(self.settle(), settled)
            self.assertEqual(self.releases, 2)

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
        self.assertEqual(execution.accept_result(self.root, self.request, content, 'fixture-host',
            lambda: execution_identity.SourceKeyKnown('edited')), accepted)

    def test_edited_replaced_and_fenced_results_never_become_accepted(self) -> None:
        _ = self.dispatch()
        replaced = replace(self.command_result(), key=replace(self.key, attempt='replacement'))
        for remote, live in ((self.command_result(), execution_identity.SourceKeyKnown('edited')),
                             (replace(self.command_result(), argv=('cargo', 'clippy')), self.source_key)):
            with self.subTest(remote=remote, live=live):
                decision = execution.accept_result(self.root, self.request, result.encode_result(remote), 'fixture-host', lambda: live)
                self.assertIsInstance(decision, result.RejectedHanaResult)
        with self.subTest('Replaced result with a pending guest result'):
            observation = replace(self.objects.observe_execution(self.key), terminated=True, lease_free=True,
                                  evidence=execution_identity.ExecutionEvidenceUnavailable(hana_protocol.COMMAND_RESULT_PENDING, 1000))
            with mock.patch.object(self.objects, 'observe_execution', return_value=observation):
                _ = execution.observe_execution(self.root, self.request, self.objects)
            rejected = execution.accept_result(self.root, self.request, result.encode_result(replaced),
                                               'fixture-host', lambda: self.source_key)
            assert isinstance(rejected, result.RejectedHanaResult)
            settled = self.settle(reason=rejected.reason)
            assert isinstance(settled, execution.HanaGuestEndedWithoutResult)
            self.assertEqual(settled.outcome, placement.AttemptDeclined('The Hana attempt or host generation changed'))
        decision = execution.accept_result(self.root, self.request, result.encode_result(self.command_result()),
                                           'fixture-host', lambda: self.source_key)
        self.assertIsInstance(decision, result.RejectedHanaResult)

    def test_missing_corrupt_and_mismatched_journals_never_prove_departure(self) -> None:
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
        _ = self.dispatch()
        self.assertTrue(execution.claim_dispatch_pending(self.root, replace(self.reference, attempt='other')))
        _ = execution.execution_path(self.root, self.key.claim_id).write_text('{')
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
        self.assertIsInstance(execution.read_claim_execution(self.root, self.reference), execution_identity.ExecutionEvidenceUnavailable)

    def test_source_capture_rechecks_live_source_before_permission(self) -> None:
        sender = cast(hana_transport.SourceSender, mock.create_autospec(hana_transport.SourceSender, instance=True))
        reads = iter((self.source_key, execution_identity.SourceKeyKnown('edited')))
        with ExitStack() as staging, mock.patch.object(sender, 'prepare', return_value=(self.manifest, lambda: None)):
            prepared = execution.prepare_execution(self.adopted, sender, self.root, self.request.argv, self.request.link,
                current_source_key=lambda: next(reads), environment={}, staging=staging)
        self.assertIsInstance(prepared, execution.ExecutionPreparationRefused)
        self.assertEqual(self.authorizations, 0)
        self.assertEqual(self.objects.envelopes, [])

    def test_source_change_after_authorization_prevents_publication(self) -> None:
        def changed(request: wire.ConsumePermission) -> wire.FirstAuthorization:
            authorized = self.consume(request)
            self.source_key = execution_identity.SourceKeyKnown('edited')
            return authorized
        upload = mock.Mock()
        self.assertEqual(self.send_dispatch(consume=changed, upload_source=upload),
            execution_identity.HanaCommandAdmissionRefused('The live source key no longer matches before Hana upload'))
        upload.assert_not_called()
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

    def test_refusal_retries_claim_release_until_it_succeeds(self) -> None:
        reason = 'The payload could not start'
        outcome = execution_identity.HanaCommandAdmissionRefused(reason)
        expected = execution.HanaAdmissionRefusalSettled(outcome, execution_identity.ExecutionNotStarted())
        root = self.root
        for published in (False, True):
            with self.subTest(published=published), tempfile.TemporaryDirectory(dir=root) as folder:
                self.root = Path(folder)
                releases: list[bool] = []

                def release() -> hana.HanaClaimRelease:
                    released = bool(releases)
                    releases.append(released)
                    return hana.HanaClaimReleased() if released else hana.HanaClaimReleaseRefused('The Hana claim is busy')

                if published:
                    _ = self.dispatch()
                    observation = hana_protocol.ConfirmedCommandAdmissionRefusal(
                        self.key, self.request.slot, False, 1000, reason, CommandAdmissionRefusalCode.PRE_COMMAND_FAILED)
                    with mock.patch.object(self.objects, 'observe_execution', return_value=observation):
                        _ = execution.observe_execution(self.root, self.request, self.objects)

                def settle() -> execution.HanaSettlement:
                    if published:
                        return execution.settle_execution(self.root, self.request, self.objects, release_claim=release,
                            reason='The result is unavailable', fence_window=execution.FenceAcknowledgementWindow(0, 1))
                    return execution.settle_unpublished_claim(self.root, self.reference,
                                                              release_claim=release, reason=outcome)

                self.assertIsInstance(settle(), execution.HanaSettlementPending)
                self.assertEqual(releases, [False])
                self.assertTrue(execution.execution_command_settled(self.root, self.key.claim_id))
                self.assertTrue(execution.claim_dispatch_pending(self.root, self.reference))
                self.assertEqual(settle(), expected)
                self.assertFalse(execution.claim_dispatch_pending(self.root, self.reference))
                self.assertEqual(settle(), expected)
                self.assertEqual(releases, [False, True])

    def test_settlement_during_authorization_prevents_a_delayed_publication(self) -> None:
        def settled_during_authorization(request: wire.ConsumePermission) -> wire.FirstAuthorization:
            authorized = self.consume(request)
            self.assertIsInstance(self.settle(), execution.HanaSettled)
            return authorized
        state = self.dispatch(consume=settled_during_authorization)
        self.assertIsInstance(state, execution.DispatchSettled)
        self.assertEqual(self.objects.envelopes, [])

    def test_transfer_charge_replay_keeps_measured_counts_without_new_dollar_totals(self) -> None:
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
        upload = mock.Mock()
        self.assertTrue(execution.publish_source_update(self.root, self.request, complete, self.objects, upload_source=upload))
        self.assertFalse(execution.publish_source_update(self.root, self.request, complete, self.objects, upload_source=upload))
        upload.assert_called_once()
        update = self.objects.envelopes[-1]
        self.assertIsInstance(update, hana_protocol.HanaSourceUpdateEnvelope)
        self.assertEqual(hana_protocol.decode_envelope(hana_protocol.encode_envelope(update)), update)
        self.assertEqual(sum(isinstance(envelope, hana_protocol.HanaCommandEnvelope) for envelope in self.objects.envelopes), 1)
        self.assertEqual(self.authorizations, 1)
        self.assertEqual(complete.commit, self.manifest.commit)
        _ = execution.observe_execution(self.root, self.request, self.objects)
        observation = execution.read_dispatch_observation(self.root, self.request)
        self.assertIsInstance(observation, hana_protocol.HanaCommandObservation)
        assert isinstance(observation, hana_protocol.HanaCommandObservation)
        self.assertTrue(observation.source_updated)
        self.assertIsInstance(self.settle(), execution.HanaSettled)

    def test_source_update_cannot_change_the_captured_commit_or_command(self) -> None:
        _ = self.dispatch()
        for manifest in (replace(self.manifest, commit='e' * 40), replace(self.manifest, argv=('cargo', 'clippy')),
                         replace(self.manifest, runtime_abi=self.manifest.runtime_abi + 1),
                         replace(self.manifest, basis_commits=('d' * 40,))):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                _ = execution.publish_source_update(self.root, self.request, manifest, self.objects, upload_source=lambda: None)
        self.assertEqual(len(self.objects.envelopes), 1)


if __name__ == '__main__':
    _ = unittest.main()

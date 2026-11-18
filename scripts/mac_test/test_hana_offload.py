"""Exercise Hana offload without SSH, AWS, live holds, or a local execution grant."""
from __future__ import annotations

from contextlib import ExitStack, nullcontext, redirect_stdout
from dataclasses import replace
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from typing import cast, final, override
import unittest
from unittest import mock

from scripts.aws_target import hana_box as box, hana_execution as execution, hana_placement as hana
from scripts.aws_target import hana_result, hana_transport, test_hana_execution as fixtures
from scripts.aws_target.hana_guest import CommandAdmissionRefusalCode
from scripts.build_queue import admission_client, admission_protocol as wire, admission_service, placement
from scripts.build_queue import test_placement as queue_fixtures
from scripts.build_targets import target_config
from scripts.buildlog import record
from scripts.direct import runner_result
from scripts.lib import process_start
from scripts.lint.memory_test_support import config_environment
from scripts.mac_test import eligibility, offload


@final
class HanaOffloadTests(unittest.TestCase):
    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.fixture = fixtures.HanaExecutionTests()
        self.root = Path()
        self.transport = hana.HanaControlTransport(Path(), 'fixture-host')
        self.request = offload.OffloadRequest(Path(), 'hana', 'call', offload.NO_RESULT_FILE, (), '', '',
            verb='check', kind='check', target='configured-target',
            placement=offload.AssignedOffloadRequest(self.fixture.assignment))
        self.lifecycle = box.HanaController(Path(), Path(), Path(), box.ProviderNotInstalled())
        self.controller = offload.ProcessController()
        self.output = BytesIO()
        self.error = BytesIO()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, config_environment(self.root))))
        self.transport = hana.HanaControlTransport(self.root / 'hana.conf', 'fixture-host')
        _ = self.transport.settings.write_text("""\
execution_transport=hana-control
host=fixture-host
enabled=on
repository_identity=https://github.com/natepiano/hana
kinds=check
# whole: 1..
max_steps=1
# whole: 1..
free_floor_gib=30
# whole: 1..
step_memory_gib=24
# whole: 1..
aws_timeout_s=7
# whole: 1..
control_url_seconds=3600
# whole: 1..
control_poll_seconds=1
# whole: 1..
result_transfer_timeout_s=83
# whole: 1..
source_upload_timeout_s=77
# whole: 1..
source_download_timeout_s=120
# whole: 1..
source_git_timeout_s=60
# whole: 1..
source_url_seconds=3600
# whole: 1..
source_max_bytes=1073741824
source_allowed_environment=RUSTFLAGS
""")
        self.request = replace(self.request, repo_root=self.root)
        self.lifecycle = box.HanaController(self.root, self.root / 'account.conf', self.transport.settings,
            cast(box.HanaProvider, cast(object, self.fixture.objects)))
        self.fixture.objects.terminated = True
        self.fixture.objects.lease_free = True

    def context(self) -> ExitStack:
        stack = ExitStack()
        _ = stack.enter_context(mock.patch.object(hana, 'validate_assignment', return_value=self.transport))
        _ = stack.enter_context(mock.patch.object(hana, 'controller_for', return_value=self.lifecycle))
        _ = stack.enter_context(mock.patch.object(hana, 'claim_assigned', return_value=self.fixture.adopted))
        _ = stack.enter_context(mock.patch.object(hana, 'release_assigned', return_value=True))
        snapshot = box.HanaLifecycleEvidence(box.DeploymentIdentity('deployment', 'instance', 'spot', 'us-east-2'),
            box.Lifecycle.READY, box.VerifiedBoot('boot', 'start'), box.NoOperation(),
            box.DeliveredPermit(3, 1800, self.fixture.key.claim_id),
            (box.WorkClaim(self.fixture.key.claim_id, 'fixture-token', 900, active=True),), 1000)
        _ = stack.enter_context(mock.patch.object(self.lifecycle, 'inspect', return_value=snapshot))
        _ = stack.enter_context(mock.patch.object(self.lifecycle, 'permission', return_value=self.fixture.permission))
        _ = stack.enter_context(mock.patch.object(process_start, 'process_start', return_value=process_start.Started('holder-start')))
        _ = stack.enter_context(mock.patch.object(hana_transport.SourceSender, 'prepare', return_value=self.fixture.manifest))
        _ = stack.enter_context(mock.patch.object(offload, 'hana_source_key', return_value=self.fixture.source_key))
        _ = stack.enter_context(mock.patch('scripts.build_queue.admission_client.consume_permission',
            return_value=wire.FirstAuthorization(self.fixture.assignment, self.fixture.reference)))
        _ = stack.enter_context(mock.patch.object(execution, 'record_attempt_charge'))
        _ = stack.enter_context(mock.patch.object(self.fixture.objects, 'execution_result',
            return_value=hana_result.encode_result(self.fixture.command_result())))
        _ = stack.enter_context(mock.patch.object(sys, 'stdout', buffer=self.output))
        _ = stack.enter_context(mock.patch.object(sys, 'stderr', buffer=self.error))
        target = target_config.SshTarget('configured-target', 'linux', self.transport.settings)
        _ = stack.enter_context(mock.patch.object(eligibility, 'configured_ssh_target', return_value=target))
        return stack

    def run_claimed(self) -> offload.MacOffloadResult:
        return offload.claimed_run(self.controller, self.transport, self.request,
                                  eligibility.KnownRepository('hana', 'github.com/natepiano/hana'), time.monotonic())

    def test_accepted_compiler_result_uses_the_existing_result_adapter_after_settlement(self) -> None:
        with self.context(), mock.patch.object(record, 'publish_hana_result') as publish:
            outcome = self.run_claimed()
        self.assertIsInstance(outcome, offload.PassedOnTarget)
        publish.assert_called_once()
        self.assertEqual(self.output.getvalue(), b'compiler output')
        self.assertIsInstance(self.controller.pending, offload.SettledDispatchOperations)
        self.assertEqual(self.controller.attempt_evidence.cleanup, 'confirmed')

    def test_stale_image_before_publication_reports_the_box_fault(self) -> None:
        with (self.context(), mock.patch.object(self.lifecycle, 'permission',
                    return_value=box.ClaimRefused(placement.HANA_IMAGE_STALE)),
                mock.patch.object(hana, 'release_assigned', return_value=False) as release,
                mock.patch.object(admission_client, 'report_execution') as report):
            outcome = self.run_claimed()
            offload.report_placement_result(self.controller, self.request, outcome)
        release.assert_not_called()
        report.assert_called_once()
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertEqual(sent.outcome, placement.HanaCommandAdmissionRefused(placement.HANA_IMAGE_STALE))
        self.assertIsInstance(sent.path, placement.DispatchPathSettled)
        self.assertIsInstance(sent.evidence, placement.ExecutionNotStarted)
        self.assertTrue(execution.claim_admission_refused(self.root, self.fixture.reference))
        assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
        self.assertEqual(outcome.reason, placement.HANA_IMAGE_STALE)
        settled = self.controller.hana_settlement
        assert isinstance(settled, execution.HanaSettled)
        self.assertEqual(settled.outcome, placement.HanaCommandAdmissionRefused(placement.HANA_IMAGE_STALE))
        self.assertEqual(self.fixture.objects.envelopes, [])
        result_path = self.root / 'result'
        with (mock.patch.dict(os.environ, {'BUILDLOG_PINNED_TARGET': self.request.target}),
                mock.patch.object(eligibility, 'configured_ssh_target', return_value=target_config.SshTarget(
                    self.request.target, 'linux', self.transport.settings)),
                mock.patch('sys.stdout', new_callable=StringIO) as output):
            self.assertEqual(offload.write_result(outcome, offload.ResultFile(result_path), target=self.request.target), 75)
        self.assertEqual(output.getvalue(), '')
        self.assertIn(f'reason={placement.HANA_IMAGE_STALE}\n', result_path.read_text())

    def test_nonzero_compiler_status_remains_a_terminal_failure(self) -> None:
        with self.context(), mock.patch.object(self.fixture.objects, 'execution_result',
                return_value=hana_result.encode_result(self.fixture.command_result(1))), \
                mock.patch.object(record, 'publish_hana_result') as publish:
            outcome = self.run_claimed()
        self.assertIsInstance(outcome, offload.FailedOnTarget)
        assert isinstance(outcome, offload.FailedOnTarget)
        self.assertEqual(outcome.status, 1)
        publish.assert_called_once()

    def assert_result_transfer_loss(self, outcome: offload.MacOffloadResult, reason: str) -> None:
        self.assertIsInstance(outcome, offload.LostMacRun)
        assert isinstance(outcome, offload.LostMacRun)
        self.assertEqual(outcome.detail, reason)
        self.assertEqual(self.output.getvalue(), b'')
        self.assertEqual(self.error.getvalue(), b'')
        report = self.root / 'transfer-loss.json'
        with redirect_stdout(StringIO()) as printed:
            status = offload.write_result(outcome, offload.ResultFile(report), verb='check', target=self.request.target)
        self.assertEqual(status, 75)
        self.assertIn('outcome=lost', report.read_text())
        self.assertIn(reason, printed.getvalue())

    def test_result_download_timeout_reports_lost_with_the_transfer_limit(self) -> None:
        reason = 'HanaResultDownloadTimedOut: The result download exceeded 83 s (result_transfer_timeout_s)'
        with (self.context(), mock.patch.object(self.fixture.objects, 'execution_result',
                side_effect=hana_result.HanaResultDownloadTimedOut(83)),
                mock.patch.object(record, 'publish_hana_result') as publish,
                mock.patch.object(record, 'publish_hana_loss') as loss):
            outcome = self.run_claimed()
            self.assert_result_transfer_loss(outcome, reason)
        publish.assert_not_called()
        loss.assert_called_once()

    def test_result_download_reserves_time_for_fence_settlement(self) -> None:
        reason = ('HanaResultDownloadWindowExpired: The control window cannot fit a result download of 83 s '
                  + 'and 113 s for settlement')
        monotonic = time.monotonic()
        boot_deadline = int(time.time()) + 250
        observation = replace(self.fixture.objects.observe_execution(self.fixture.key), accepted=True,
            evidence=placement.ExecutionEvidenceUnavailable('The command result is pending', 1000))
        fence_reads: list[float] = []

        def advance(seconds: float) -> None:
            nonlocal monotonic
            monotonic += seconds

        with (self.context(), mock.patch.object(self.fixture.objects, 'boot_control_deadline', return_value=boot_deadline),
                mock.patch.object(time, 'monotonic', side_effect=lambda: monotonic),
                mock.patch.object(time, 'sleep', side_effect=advance),
                mock.patch.object(self.fixture.objects, 'execution_result') as download,
                mock.patch.object(record, 'publish_hana_loss') as loss):
            observe = self.fixture.objects.observe_execution

            def observe_near_deadline(key: hana_result.HanaAttemptKey, *,
                                      timeout_s: float | None = None) -> execution.HanaCommandObservation:
                if not self.fixture.objects.fenced:
                    advance(80)
                    return observation
                self.assertIsNotNone(timeout_s)
                fence_reads.append(monotonic)
                if len(fence_reads) == 1:
                    return observation
                if len(fence_reads) == 2:
                    raise TimeoutError('The fence acknowledgement read timed out')
                return observe(key, timeout_s=timeout_s)

            with mock.patch.object(self.fixture.objects, 'observe_execution', side_effect=observe_near_deadline):
                outcome = self.run_claimed()
            self.assert_result_transfer_loss(outcome, reason)
        download.assert_not_called()
        loss.assert_called_once()
        self.assertEqual([read_at - fence_reads[0] for read_at in fence_reads], [0, 1, 2])
        self.assertEqual([type(value) for value in self.fixture.objects.envelopes],
                         [execution.HanaCommandEnvelope, execution.HanaFenceEnvelope])

    def test_result_upload_timeout_reports_without_downloading_a_result(self) -> None:
        self.assert_upload_loss('HanaResultUploadTimedOut: The result upload exceeded 83 s (result_transfer_timeout_s)')

    def test_result_upload_window_refusal_reports_without_downloading_a_result(self) -> None:
        self.assert_upload_loss('HanaResultUploadFailed: The signed access window cannot fit a result upload of 83 s and settlement')

    def assert_upload_loss(self, reason: str) -> None:
        objects = self.fixture.objects
        observation = replace(objects.observe_execution(self.fixture.key), accepted=True,
            evidence=placement.ExecutionEvidenceUnavailable(reason, 1000))
        observe = objects.observe_execution

        def upload_failed(key: hana_result.HanaAttemptKey, *,
                          timeout_s: float | None = None) -> execution.HanaCommandObservation:
            return observe(key, timeout_s=timeout_s) if objects.fenced else observation

        with (self.context(), mock.patch.object(objects, 'observe_execution', side_effect=upload_failed),
                mock.patch.object(objects, 'execution_result') as download,
                mock.patch.object(record, 'publish_hana_result') as publish,
                mock.patch.object(record, 'publish_hana_loss') as loss):
            outcome = self.run_claimed()
            self.assert_result_transfer_loss(outcome, reason)
        download.assert_not_called()
        publish.assert_not_called()
        loss.assert_called_once()

    def test_source_change_during_settlement_reports_loss_instead_of_completion(self) -> None:
        from scripts.build_queue import admission_client

        with self.context(), mock.patch.object(offload, 'hana_source_key', return_value=self.fixture.source_key) as source, \
                mock.patch.object(record, 'publish_hana_result') as publish, \
                mock.patch.object(record, 'publish_hana_loss') as loss, \
                mock.patch.object(admission_client, 'report_execution') as report:
            def release(*_: object) -> bool:
                source.return_value = placement.SourceKeyKnown('changed-source')
                return True

            with mock.patch.object(hana, 'release_assigned', side_effect=release):
                outcome = self.run_claimed()
            offload.report_placement_result(self.controller, self.request, outcome)
            replay = execution.replay_accepted_result(self.root, self.fixture.request, self.transport.host,
                                                       lambda: self.fixture.source_key)
        self.assertIsInstance(outcome, offload.LostMacRun)
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertIsInstance(sent.outcome, placement.AttemptDeclined)
        self.assertIsInstance(sent.path, placement.DispatchPathSettled)
        self.assertIsInstance(replay, hana_result.RejectedHanaResult)
        row = hana_result.object_fields(cast(object, json.loads(
            execution.execution_path(self.root, self.fixture.key.claim_id).read_text())))
        self.assertEqual(hana_result.object_fields(row['outcome'])['kind'], 'declined')
        publish.assert_not_called()
        loss.assert_called_once()

    def test_replaced_result_creates_no_final_row_or_verify_cache(self) -> None:
        stale = replace(self.fixture.command_result(), key=replace(self.fixture.key, attempt='replaced'))
        placement_path = self.root / 'assignment.json'
        objects = self.fixture.objects
        observation = replace(objects.observe_execution(self.fixture.key), accepted=True,
            evidence=placement.ExecutionEvidenceUnavailable(execution.COMMAND_RESULT_PENDING, 1000))
        with self.context(), mock.patch.object(self.fixture.objects, 'execution_result', return_value=hana_result.encode_result(stale)), \
                mock.patch.object(objects, 'observe_execution', return_value=observation), \
                mock.patch.object(record, 'publish_hana_result') as publish, \
                mock.patch.object(record, 'publish_hana_loss') as loss, \
                mock.patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': str(placement_path)}):
            outcome = self.run_claimed()
            result_file = self.root / 'verify-result.json'
            status = offload.write_result(outcome, offload.ResultFile(result_file), offload.NO_PRIOR_MAC_DECLINE,
                'check', self.request.target, self.controller.attempt_evidence)
        self.assertIsInstance(outcome, offload.LostMacRun)
        self.assertEqual(status, 75)
        decoded = runner_result.read_result(result_file, status, selected_is_mac=False)
        assert isinstance(decoded, runner_result.UsableRunnerResult)
        self.assertEqual((decoded.word, decoded.reason), ('lost', 'The Hana attempt or host generation changed'))
        publish.assert_not_called()
        loss.assert_called_once()
        link = hana_result.object_fields(cast(object, json.loads(Path(str(placement_path) + '.hana-link').read_text())))
        self.assertEqual(link['link'], {'call_id': 'call', 'episode': self.fixture.key.episode})

    def test_fenced_attempt_creates_no_final_row_or_verify_cache(self) -> None:
        with self.context(), mock.patch.object(record, 'publish_hana_result') as publish, \
                mock.patch.object(record, 'publish_hana_loss'):
            _ = execution.dispatch_execution(self.root, self.fixture.request, self.fixture.adopted, self.fixture.objects,
                consume=lambda request: wire.FirstAuthorization(self.fixture.assignment, self.fixture.reference),
                current_permission=lambda: self.fixture.permission,
                current_source_key=lambda: self.fixture.source_key)
            _ = execution.settle_execution(self.root, self.fixture.request, self.fixture.objects,
                release_claim=lambda: True, reason='The original owner cancelled the attempt',
                fence_window=execution.FenceAcknowledgementWindow(time.monotonic() + 113, 1))
            outcome = self.run_claimed()
            result_file = self.root / 'fenced-result.json'
            status = offload.write_result(outcome, offload.ResultFile(result_file), offload.NO_PRIOR_MAC_DECLINE,
                'check', self.request.target, self.controller.attempt_evidence)
        self.assertEqual(status, 75)
        self.assertIn('outcome=lost', result_file.read_text())
        publish.assert_not_called()

    def test_uncertain_cleanup_reports_pending_even_after_a_compiler_result(self) -> None:
        from scripts.build_queue import admission_client

        self.controller.pending = offload.PendingHanaOperation()
        self.controller.attempt_evidence.provider = self.fixture.reference
        with mock.patch.object(admission_client, 'report_execution') as report:
            offload.report_placement_result(self.controller, self.request, offload.PassedOnTarget(1))
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertIsInstance(sent.outcome, placement.AttemptOutcomeUnavailable)
        self.assertIsInstance(sent.path, placement.DispatchPathPending)

    def test_cancellation_reports_cancelled_after_exact_settlement(self) -> None:
        from scripts.build_queue import admission_client

        self.controller.hana_settlement = execution.HanaSettled(placement.AttemptCancelled('The user cancelled'),
                                                               placement.ExecutionNotStarted())
        self.controller.attempt_evidence.provider = self.fixture.reference
        with mock.patch.object(admission_client, 'report_execution') as report:
            offload.report_placement_result(self.controller, self.request, offload.LostMacRun(1))
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertIsInstance(sent.outcome, placement.AttemptCancelled)

    def test_disabled_and_repair_requests_never_reach_claims_or_ssh(self) -> None:
        repository = eligibility.KnownRepository('hana', 'github.com/natepiano/hana')
        with mock.patch.object(eligibility, 'repository_identity', return_value=repository), \
                mock.patch.object(hana, 'fixed_policy', return_value=hana.HanaStepRefused('off')), \
                mock.patch.object(hana, 'claim_assigned') as claim, \
                mock.patch.object(offload, 'ssh_prefix') as ssh:
            outcome = offload.run_offload(self.controller, self.transport, self.request, time.monotonic())
        self.assertIsInstance(outcome, offload.DeclinedMacRun)
        claim.assert_not_called()
        ssh.assert_not_called()

    def test_policy_reads_the_worktree_path(self) -> None:
        repository = eligibility.KnownRepository('hana', 'github.com/natepiano/hana')
        with mock.patch.object(eligibility, 'repository_identity', return_value=repository), \
                mock.patch.object(hana, 'fixed_policy', return_value=hana.HanaStepRefused('off')) as policy:
            _ = offload.run_offload(self.controller, self.transport, self.request, time.monotonic())
        self.assertEqual(policy.call_args.args[1], str(self.request.repo_root))

    def test_disabled_unbound_assignment_reports_claim_failure_without_retaining_dispatch(self) -> None:
        from scripts.build_queue import admission_client

        repository = eligibility.KnownRepository('hana', 'github.com/natepiano/hana')
        with mock.patch.object(eligibility, 'repository_identity', return_value=repository), \
                mock.patch.object(hana, 'fixed_policy', return_value=hana.HanaStepRefused('off')), \
                mock.patch.object(hana, 'claim_assigned') as claim, \
                mock.patch.object(admission_client, 'claim_failure') as report:
            outcome = offload.run_offload(self.controller, self.transport, self.request, time.monotonic())
            offload.report_placement_result(self.controller, self.request, outcome)
        self.assertIsInstance(outcome, offload.DeclinedMacRun)
        claim.assert_not_called()
        report.assert_called_once()
        sent = cast(wire.ClaimFailure, report.call_args.args[0])
        self.assertEqual(sent.assignment, self.fixture.assignment)
        self.assertFalse(self.lifecycle.execution_bound(self.fixture.key.claim_id))
        self.assertFalse(execution.execution_path(self.root, self.fixture.key.claim_id).exists())

    def test_stopping_a_local_child_keeps_the_remote_operation_pending(self) -> None:
        self.controller.pending = offload.PendingHanaOperation()
        self.controller.stop()
        self.assertIsInstance(self.controller.pending, offload.PendingHanaOperation)
        self.assertFalse(self.controller.defer_claim_interrupt(signal.SIGTERM))

    def test_startup_deadline_reports_prelaunch_refusal_while_cleanup_stays_pending(self) -> None:
        from scripts.build_queue import admission_client

        pending = hana.HanaClaimPending(self.fixture.reference,
            box.WorkClaim(self.fixture.key.claim_id, 'claim-token', 900), 0)
        with self.context(), mock.patch.object(hana, 'claim_assigned', return_value=pending), \
                mock.patch.object(hana, 'release_assigned', return_value=False), \
                mock.patch.object(admission_client, 'report_execution') as report:
            outcome = self.run_claimed()
            offload.report_placement_result(self.controller, self.request, outcome)
        self.assertIsInstance(outcome, offload.DeclinedMacRun)
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertIsInstance(sent.outcome, placement.PrelaunchRefused)
        self.assertIsInstance(sent.path, placement.DispatchPathPending)
        self.assertIsInstance(sent.evidence, placement.ExecutionNotStarted)
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.fixture.reference))
        self.assertEqual(self.fixture.objects.envelopes, [])

    def test_failed_fence_prevents_result_publication_after_compiler_completion(self) -> None:
        self.fixture.objects.fail_fence = True
        monotonic = time.monotonic()

        def advance(seconds: float) -> None:
            nonlocal monotonic
            monotonic += seconds

        with (self.context(), mock.patch.object(record, 'publish_hana_result') as publish,
              mock.patch.object(time, 'monotonic', side_effect=lambda: monotonic),
              mock.patch.object(time, 'sleep', side_effect=advance) as pause):
            outcome = self.run_claimed()
        self.assertIsInstance(outcome, offload.LostMacRun)
        self.assertIsInstance(self.controller.pending, offload.PendingHanaOperation)
        pause.assert_called()
        publish.assert_not_called()

    def test_terminal_guest_refusal_supplies_one_cause_to_the_pinned_runner(self) -> None:
        root = self.root
        for refusal_in_fence_ack in (False, True):
            with self.subTest(refusal_in_fence_ack=refusal_in_fence_ack), tempfile.TemporaryDirectory(dir=root) as folder:
                self.root = Path(folder)
                self.fixture = fixtures.HanaExecutionTests()
                self.controller = offload.ProcessController()
                self.lifecycle = box.HanaController(self.root, self.root / 'account.conf', self.transport.settings,
                    cast(box.HanaProvider, cast(object, self.fixture.objects)))
                self.assert_terminal_guest_refusal(refusal_in_fence_ack=refusal_in_fence_ack)

    def assert_terminal_guest_refusal(self, *, refusal_in_fence_ack: bool) -> None:
        from scripts.copies import copies

        queue = self.enterContext(queue_fixtures.PlacementFixture())
        _ = self.enterContext(mock.patch.object(admission_service, 'HostEvidenceWorker', queue_fixtures.ManualEvidenceWorker))
        target = target_config.SshTarget(self.request.target, 'linux', self.transport.settings)
        _ = self.enterContext(mock.patch.object(target_config, 'read_targets', return_value=(target,)))
        submitted = queue_fixtures.placement_request(queue)
        submitted = replace(submitted, constraints=replace(submitted.constraints, repository=str(self.root),
            command=('cargo', 'check'), command_kind='check', source_key=self.fixture.source_key,
            candidates=(placement.PlacementCandidate(self.fixture.assignment.destination),)))
        _ = queue_fixtures.publish_candidate(queue, submitted)
        assigned = queue_fixtures.assignment_for(queue, submitted)
        claim = placement.assigned_claim_reference(assigned)
        provider_claim = placement.AdoptedProviderClaim(claim, queue_fixtures.coordinator_holder(assigned),
            assigned.session, assigned.destination.host, hana.generation_text(self.fixture.adopted.generation))
        key = queue.server.host_evidence_key(submitted, assigned.destination)
        evidence = placement.FreshHostEvidence(key, queue.clock.now(), provider_claim.generation,
                                               placement.AllowedHost(), (provider_claim,))
        queue.provider_evidence[key] = evidence
        queue.server.publish_host_evidence(evidence)
        authorization, = queue.connect(queue_fixtures.consume_permission('consume', assigned, claim)).replies()
        self.assertEqual(authorization, wire.FirstAuthorization(assigned, claim))
        self.fixture.assignment, self.fixture.reference = assigned, claim
        self.fixture.adopted = replace(self.fixture.adopted, reserved=placement.ReservedHost(assigned, provider_claim))
        self.fixture.key = replace(self.fixture.key, episode=assigned.episode, attempt=assigned.attempt, claim_id=claim.claim_id)
        self.fixture.request = replace(self.fixture.request, key=self.fixture.key,
                                       link=replace(self.fixture.request.link, episode=assigned.episode))
        self.fixture.permission = replace(self.fixture.permission, execution=claim.claim_id)

        reason = 'Hana command admission is disabled on the guest'
        objects = self.fixture.objects
        objects.request = self.fixture.request
        objects.fail_fence = not refusal_in_fence_ack
        observation = replace(objects.observe_execution(self.fixture.key), accepted=True, terminated=True, lease_free=True,
                              evidence=placement.ExecutionEvidenceUnavailable(reason, 1000),
                              refusal_code=CommandAdmissionRefusalCode.ADMISSION_DISABLED)
        monotonic = time.monotonic()
        boot_deadline = int(time.time()) + 200
        publish = objects.publish_execution

        def publish_near_deadline(envelope: execution.HanaExecutionEnvelope) -> None:
            nonlocal monotonic
            publish(envelope)
            if refusal_in_fence_ack and isinstance(envelope, execution.HanaCommandEnvelope):
                monotonic += 90

        def observe_refusal(key: hana_result.HanaAttemptKey, *,
                            timeout_s: float | None = None) -> execution.HanaCommandObservation:
            self.assertEqual(key, observation.key)
            self.assertEqual(objects.fenced, refusal_in_fence_ack)
            self.assertEqual(timeout_s is not None, refusal_in_fence_ack)
            return replace(observation, fenced=objects.fenced)

        result = offload.ResultFile(self.root / 'result')
        assignment_file = self.root / 'assignment'
        _ = assignment_file.write_text(wire.encode_reply(wire.Assignment(assigned)))
        argv = ['offload.py', 'run', '--repo-root', str(self.root), '--verb', 'check', '--kind', 'check',
                '--call-id', 'call', '--target', target.name, '--assignment-file', str(assignment_file), '--result', str(result.path)]
        send_report = admission_client.report_execution

        def report_while_claimed(request: wire.ExecutionReport) -> wire.DispatchStatus:
            snapshot = self.lifecycle.inspect()
            assert isinstance(snapshot, box.HanaLifecycleEvidence)
            self.assertEqual([item.execution for item in snapshot.claims], [claim.claim_id])
            self.assertTrue(execution.claim_admission_refused(self.root, claim))
            return send_report(request)

        with (self.context(),
              queue.serving(),
              mock.patch.object(sys, 'argv', argv),
              mock.patch.object(offload, 'ProcessController', return_value=self.controller),
              mock.patch.object(offload, 'interrupt_handlers', return_value=nullcontext()),
              mock.patch.object(offload, 'restore_ignored_interrupt_default'),
              mock.patch.object(copies, 'source_owner', return_value='fixture'),
              mock.patch.object(eligibility, 'repository_identity',
                                return_value=eligibility.KnownRepository('hana', 'https://github.com/natepiano/hana')),
              mock.patch.object(time, 'monotonic', side_effect=lambda: monotonic),
              mock.patch.object(objects, 'boot_control_deadline', return_value=boot_deadline),
              mock.patch.object(objects, 'publish_execution', side_effect=publish_near_deadline),
              mock.patch.object(objects, 'observe_execution', side_effect=observe_refusal) as observe,
              mock.patch.object(objects, 'execution_result') as fetch,
              mock.patch.object(hana, 'release_assigned', return_value=False) as release,
              mock.patch.object(record, 'publish_hana_loss') as loss,
              mock.patch.object(record, 'ssh_attempt'),
              mock.patch.object(admission_client, 'report_execution', side_effect=report_while_claimed) as report,
              mock.patch.dict(os.environ, {'BUILDLOG_PINNED_TARGET': target.name}),
              redirect_stdout(StringIO()) as text):
            status = offload.main()
        self.assertEqual(text.getvalue(), '')
        self.assertEqual(status, 75)
        decoded = runner_result.read_result(result.path, status, selected_is_mac=False)
        assert isinstance(decoded, runner_result.UsableRunnerResult)
        self.assertEqual((decoded.word, decoded.reason), ('hana_command_admission_refused', reason))
        self.assertIsInstance(self.controller.pending, offload.PendingHanaOperation)
        self.assertTrue(execution.execution_command_settled(self.root, self.fixture.key.claim_id))
        self.assertTrue(execution.claim_admission_refused(self.root, self.fixture.reference))
        self.assertFalse(execution.claim_dispatch_pending(self.root, self.fixture.reference))
        release.assert_not_called()
        fetch.assert_not_called()
        report.assert_called_once()
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertEqual(sent.outcome, placement.HanaCommandAdmissionRefused(reason))
        self.assertIsInstance(sent.evidence, placement.ExecutionNotStarted)
        self.assertIsInstance(sent.path, placement.DispatchPathSettled)
        saved = queue.server.assignments[placement.attempt_key(assigned.episode, assigned.attempt)]
        self.assertEqual(saved.state, placement.ClosedAssignment(reason))
        self.assertEqual(saved.retry, placement.DoNotRetry(reason))
        envelopes: list[type[execution.HanaExecutionEnvelope]] = [execution.HanaCommandEnvelope]
        if refusal_in_fence_ack:
            envelopes.append(execution.HanaFenceEnvelope)
        self.assertEqual([type(envelope) for envelope in objects.envelopes], envelopes)
        observe.assert_called_once()
        loss.assert_called_once()

    def test_repair_flags_and_mend_profiles_never_claim_the_hana_target(self) -> None:
        repository = eligibility.KnownRepository('hana', 'github.com/natepiano/hana')
        policy = hana.EnabledHanaRoute(repository.key, ('check', 'lint'), 1, 30)
        for request in (replace(self.request, verb='clippy', kind='lint', nextest_words=('--fix',)),
                        replace(self.request, verb='mend-check', kind='lint'),
                        replace(self.request, verb='nextest', kind='test')):
            with self.subTest(request=request), mock.patch.object(eligibility, 'repository_identity', return_value=repository), \
                    mock.patch.object(hana, 'fixed_policy', return_value=policy), \
                    mock.patch.object(hana, 'claim_assigned') as claim:
                outcome = offload.run_offload(self.controller, self.transport, request, time.monotonic())
            self.assertIsInstance(outcome, offload.DeclinedMacRun)
            claim.assert_not_called()

    def test_missing_basis_repairs_the_captured_commit_without_another_command(self) -> None:
        from scripts.aws_target.hana_source import MissingBasis

        self.fixture.manifest = replace(self.fixture.manifest, basis_commits=('d' * 40,))
        self.fixture.request = replace(self.fixture.request, manifest=self.fixture.manifest)
        self.fixture.objects.request = self.fixture.request
        self.fixture.objects.missing_basis = True
        complete = replace(self.fixture.manifest, basis_commits=(), bundle_sha256='e' * 64)
        with self.context(), mock.patch.object(hana_transport.SourceSender, 'retry_complete', return_value=complete) as retry, \
                mock.patch.object(hana_transport.SourceSender, 'acknowledge') as acknowledge, \
                mock.patch.object(record, 'publish_hana_result') as publish, \
                mock.patch.object(record, 'publish_hana_loss') as loss, mock.patch.object(time, 'sleep'):
            outcome = self.run_claimed()
        retry.assert_called_once_with(self.root, self.fixture.manifest,
            MissingBasis(self.fixture.manifest.repository, self.fixture.manifest.basis_commits))
        acknowledge.assert_called_once()
        publish.assert_not_called()
        loss.assert_called_once()
        self.assertIsInstance(outcome, offload.LostMacRun)
        self.assertEqual(sum(isinstance(envelope, execution.HanaCommandEnvelope)
                             for envelope in self.fixture.objects.envelopes), 1)
        self.assertEqual(sum(isinstance(envelope, execution.HanaSourceUpdateEnvelope)
                             for envelope in self.fixture.objects.envelopes), 1)


if __name__ == '__main__':
    _ = unittest.main()

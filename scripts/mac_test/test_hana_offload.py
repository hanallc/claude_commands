"""Exercise Hana offload without SSH, AWS, live holds, or a local execution grant."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import cast, final, override
import unittest
from unittest import mock

from scripts.aws_target import hana_box as box, hana_execution as execution, hana_placement as hana
from scripts.aws_target import hana_result, hana_source, hana_transport, test_hana_execution as fixtures
from scripts.build_hold import override_guard
from scripts.build_queue import admission_protocol as wire, placement
from scripts.build_targets import target_config
from scripts.buildlog import record, treekey
from scripts.copies import copies
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
        _ = self.transport.settings.write_text('''\
# whole: 1..
aws_timeout_s=10
# whole: 1..
source_upload_timeout_s=120
# whole: 1..
source_url_seconds=300
# whole: 1..
source_max_bytes=4096
# whole: 1..
source_download_timeout_s=10
# whole: 1..
source_git_timeout_s=10
source_allowed_environment=RUST_BACKTRACE
# whole: 1..
control_url_seconds=10
# whole: 1..
control_poll_seconds=1
''')
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
            box.DeliveredPermit(3, 1800, self.fixture.key.claim_id), (), 1000)
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

    def test_nonzero_compiler_status_remains_a_terminal_failure(self) -> None:
        with self.context(), mock.patch.object(self.fixture.objects, 'execution_result',
                return_value=hana_result.encode_result(self.fixture.command_result(1))), \
                mock.patch.object(record, 'publish_hana_result') as publish:
            outcome = self.run_claimed()
        self.assertIsInstance(outcome, offload.FailedOnTarget)
        assert isinstance(outcome, offload.FailedOnTarget)
        self.assertEqual(outcome.status, 1)
        publish.assert_called_once()

    def test_source_refusal_releases_the_claim_and_ends_the_build(self) -> None:
        from scripts.build_queue import admission_client

        cause = 'IgnoredInput: remove the ignored path'
        assigned = replace(self.request, result=offload.ResultFile(self.root / 'result'))
        repository = eligibility.KnownRepository('hana', 'github.com/natepiano/hana')

        def run(runner: offload.ProcessController, transport: hana.HanaControlTransport,
                placed: offload.OffloadRequest, started: float) -> offload.MacOffloadResult:
            return offload.hana_claimed_run(runner, transport, placed, repository, started)

        for request in (assigned, replace(assigned, placement=offload.DirectOffloadRequest())):
            with self.subTest(request=request.placement):
                output = StringIO()
                controller = offload.ProcessController()
                with self.context(), ExitStack() as patches:
                    _ = patches.enter_context(mock.patch.object(sys, 'stdout', output))
                    _ = patches.enter_context(mock.patch.object(sys, 'argv',
                        ['offload', 'run', '--repo-root', str(self.root), '--call-id', 'call']))
                    _ = patches.enter_context(mock.patch.object(override_guard, 'refuse_overrides'))
                    _ = patches.enter_context(mock.patch.object(offload, 'request_from_arguments', return_value=request))
                    _ = patches.enter_context(mock.patch.object(offload, 'ProcessController', return_value=controller))
                    _ = patches.enter_context(mock.patch.object(hana, 'read_execution_transport', return_value=self.transport))
                    _ = patches.enter_context(mock.patch.object(offload, 'run_offload', side_effect=run))
                    submit = patches.enter_context(mock.patch.object(offload, 'submit_direct_placement', side_effect=[assigned]))
                    claim = patches.enter_context(mock.patch.object(hana, 'claim_assigned', return_value=self.fixture.adopted))
                    capture = patches.enter_context(mock.patch.object(hana_transport.SourceSender, 'prepare',
                        side_effect=hana_source.SourceRefused(cause)))
                    release = patches.enter_context(mock.patch.object(hana, 'release_assigned', return_value=True))
                    report = patches.enter_context(mock.patch.object(admission_client, 'report_execution'))
                    _ = patches.enter_context(mock.patch.object(admission_client, 'cancel_placement'))
                    _ = patches.enter_context(mock.patch.object(offload, 'persist_attempt'))
                    status = offload.main()
                self.assertEqual(status, 1)
                self.assertEqual(submit.call_count, int(isinstance(request.placement, offload.DirectOffloadRequest)))
                claim.assert_called_once()
                capture.assert_called_once()
                release.assert_called_once()
                report.assert_called_once()
                sent = cast(wire.ExecutionReport, report.call_args.args[0])
                self.assertEqual(sent.outcome, placement.PrelaunchRefused(cause))
                self.assertIsInstance(sent.path, placement.DispatchPathSettled)
                self.assertIsInstance(sent.evidence, placement.ExecutionNotStarted)
                self.assertFalse(execution.claim_dispatch_pending(self.root, self.fixture.reference))
                self.assertEqual(self.fixture.objects.envelopes, [])
                self.assertEqual(output.getvalue().splitlines(), [
                    'mac_test: The build did not run on configured-target. '
                    + 'Fix the Hana source refusal (IgnoredInput: remove the ignored path) and run the build again.'])
                self.assertIn('outcome=prelaunch_failed', (self.root / 'result').read_text())

    def test_source_change_inside_capture_returns_a_usable_retry_result(self) -> None:
        def changed_capture(*_args: object, **_options: object) -> str:
            return copies.capture_source(self.root)

        report = self.root / 'result'
        with (self.context(), mock.patch.object(sys, 'stdout', StringIO()),
                mock.patch.object(copies, 'source_commit', return_value='head'),
                mock.patch.object(treekey, 'tree_key', return_value='source'),
                mock.patch.object(copies, 'staged_tree', side_effect=['index', 'tree', 'changed']),
                mock.patch.object(hana_transport.SourceSender, 'prepare', side_effect=changed_capture)):
            outcome = self.run_claimed()
            status = offload.write_result(outcome, offload.ResultFile(report), target=self.request.target)
        self.assertEqual(status, 75)
        self.assertIsInstance(outcome, offload.DeclinedMacRun)
        result = runner_result.read_result(report, status, False)
        assert isinstance(result, runner_result.UsableRunnerResult)
        self.assertEqual((result.word, result.reason), ('declined', 'source_changed'))
        self.assertEqual(self.controller.attempt_evidence.cleanup, 'confirmed')
        self.assertEqual(self.fixture.objects.envelopes, [])

    def test_killed_capture_git_ends_the_attempt_with_a_runner_fault(self) -> None:
        limits = hana_source.SourceLimits(1024, 2, 2, ())

        def killed_git(*_args: object, **_options: object) -> str:
            return hana_source.git(self.root, limits, 'bundle', 'create', 'source.bundle')

        output = StringIO()
        report = self.root / 'result'
        killed = subprocess.CompletedProcess(['git'], -signal.SIGKILL, '', '')
        with (self.context(), mock.patch.object(sys, 'stdout', output),
                mock.patch.object(hana_transport.SourceSender, 'prepare', side_effect=killed_git),
                mock.patch.object(subprocess, 'run', return_value=killed)):
            outcome = self.run_claimed()
            status = offload.write_result(outcome, offload.ResultFile(report), target=self.request.target)
        self.assertEqual(status, 75)
        self.assertIn('reason=runner\n', report.read_text())
        self.assertIn('The Hana source capture failed', output.getvalue())
        self.assertIn('SIGKILL', output.getvalue())
        self.assertEqual(self.controller.attempt_evidence.cleanup, 'confirmed')
        self.assertEqual(self.fixture.objects.envelopes, [])

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
        with self.context(), mock.patch.object(self.fixture.objects, 'execution_result', return_value=hana_result.encode_result(stale)), \
                mock.patch.object(record, 'publish_hana_result') as publish, \
                mock.patch.object(record, 'publish_hana_loss') as loss, \
                mock.patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': str(placement_path)}):
            outcome = self.run_claimed()
            result_file = self.root / 'verify-result.json'
            status = offload.write_result(outcome, offload.ResultFile(result_file), offload.NO_PRIOR_MAC_DECLINE,
                'check', self.request.target, self.controller.attempt_evidence)
        self.assertIsInstance(outcome, offload.LostMacRun)
        self.assertEqual(status, 75)
        self.assertIn('outcome=lost', result_file.read_text())
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
                release_claim=lambda: True, reason='The original owner cancelled the attempt')
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
        with self.context(), mock.patch.object(record, 'publish_hana_result') as publish:
            outcome = self.run_claimed()
        self.assertIsInstance(outcome, offload.LostMacRun)
        self.assertIsInstance(self.controller.pending, offload.PendingHanaOperation)
        publish.assert_not_called()

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

    def test_failed_git_in_source_update_settles_the_attempt_as_lost(self) -> None:
        self.fixture.manifest = replace(self.fixture.manifest, basis_commits=('d' * 40,))
        self.fixture.request = replace(self.fixture.request, manifest=self.fixture.manifest)
        self.fixture.objects.request = self.fixture.request
        self.fixture.objects.missing_basis = True
        error = subprocess.CalledProcessError(-signal.SIGKILL, ['git', 'bundle', 'create'])
        with (self.context(), mock.patch.object(hana_transport.SourceSender, 'retry_complete', side_effect=error),
                mock.patch.object(record, 'publish_hana_loss') as loss):
            outcome = self.run_claimed()
        assert isinstance(outcome, offload.LostMacRun)
        self.assertIn('The Hana source update publication is unresolved', outcome.detail)
        self.assertIn('SIGKILL', outcome.detail)
        self.assertEqual(self.controller.attempt_evidence.cleanup, 'confirmed')
        loss.assert_called_once()


if __name__ == '__main__':
    _ = unittest.main()

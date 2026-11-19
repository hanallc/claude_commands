"""Exercise Hana offload without SSH, AWS, live holds, or a local execution grant."""
from __future__ import annotations

from contextlib import ExitStack, nullcontext, redirect_stdout
from dataclasses import replace
from functools import partial
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

from scripts.aws_target import admin_secrets, deploy, hana_box as box, hana_execution as execution, hana_placement as hana
from scripts.aws_target import hana_placement_adapter, hana_protocol, hana_result, hana_source, hana_transport, test_hana_execution as fixtures
from scripts.aws_target.controller_lock import HanaCommandAccess, hana_command_lock
from scripts.aws_target.hana_guest import CommandAdmissionRefusalCode
from scripts.aws_target.ledger_test_support import install_hana_spending_settings, numeric_settings
from scripts.build_hold import override_guard
from scripts.build_queue import execution_identity
from scripts.build_queue import admission_client, admission_protocol as wire, admission_service, placement
from scripts.build_queue import test_placement as queue_fixtures
from scripts.build_targets import target_config
from scripts.buildlog import record, treekey
from scripts.copies import copies
from scripts.direct import runner_result
from scripts.lib import conf, process_start
from scripts.lint.memory_test_support import config_environment
from scripts.mac_test import eligibility, offload


RELEASE_BLOCKED = hana.HanaClaimReleaseRefused('The Hana lifecycle operation observe is pending')


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
source_allowed_ignored=target/
source_forbidden_paths=*.key
""")
        self.request = replace(self.request, repo_root=self.root)
        self.lifecycle = box.HanaController(self.root, self.root / 'account.conf', self.transport.settings,
            cast(box.HanaProvider, cast(object, self.fixture.objects)))
        self.fixture.objects.terminated = True
        self.fixture.objects.lease_free = True

    def context(self, *, real_source: bool = False) -> ExitStack:
        stack = ExitStack()
        _ = stack.enter_context(mock.patch.object(hana, 'validate_assignment', return_value=self.transport))
        _ = stack.enter_context(mock.patch.object(hana, 'controller_for', return_value=self.lifecycle))
        _ = stack.enter_context(mock.patch.object(hana, 'claim_assigned', return_value=self.fixture.adopted))
        _ = stack.enter_context(mock.patch.object(hana, 'release_assigned', return_value=hana.HanaClaimReleased()))
        snapshot = box.HanaLifecycleEvidence(box.DeploymentIdentity('deployment', 'instance', 'spot', 'us-east-2'),
            box.Lifecycle.READY, box.VerifiedBoot('boot', 'start'), box.NoOperation(),
            box.DeliveredPermit(3, 1800, self.fixture.key.claim_id),
            (box.WorkClaim(self.fixture.key.claim_id, 'fixture-token', 900, active=True),), 1000)
        _ = stack.enter_context(mock.patch.object(self.lifecycle, 'inspect', return_value=snapshot))
        _ = stack.enter_context(mock.patch.object(self.lifecycle, 'permission', return_value=self.fixture.permission))
        _ = stack.enter_context(mock.patch.object(process_start, 'process_start', return_value=process_start.Started('holder-start')))
        if not real_source:
            _ = stack.enter_context(mock.patch.object(hana_transport, 'check_inputs'))
            _ = stack.enter_context(mock.patch.object(hana_transport.SourceSender, 'prepare',
                                                     return_value=(self.fixture.manifest, lambda: None)))
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
        root = self.root
        for compiler_status, problem, fault in (
            (0, '', ''), (0, 'release', ''), (7, 'release', ''), (7, 'fence', ''),
            (0, '', 'the Hana result'), (7, 'fence', 'the Hana result'), (7, '', 'the Hana attempt charge'),
            (7, 'release', 'the placement report'), (0, '', 'the attempt'),
        ):
            with (self.subTest(compiler_status=compiler_status, problem=problem, fault=fault),
                  tempfile.TemporaryDirectory(dir=root) as folder):
                self.root = Path(folder)
                self.controller = offload.ProcessController()
                self.fixture.objects = fixtures.FakeExecutionObjects(self.fixture.request, terminated=True, lease_free=True,
                                                                     fail_fence=problem == 'fence')
                self.lifecycle = box.HanaController(self.root, self.root / 'account.conf', self.transport.settings,
                    cast(box.HanaProvider, cast(object, self.fixture.objects)))
                self.output, self.error = BytesIO(), BytesIO()
                self.check_accepted_compiler_result(compiler_status, problem=problem, fault=fault)

    def check_accepted_compiler_result(self, compiler_status: int, *, problem: str, fault: str) -> None:
        completed = self.fixture.command_result(compiler_status)
        result = offload.ResultFile(self.root / 'runner.result')
        request = replace(self.request, result=result)
        argv = ['offload', 'run', '--repo-root', str(self.root), '--call-id', 'call']
        release_result = RELEASE_BLOCKED if problem == 'release' else hana.HanaClaimReleased()
        clock = 1000.0
        writes: list[str] = []

        def advance(seconds: float) -> None:
            nonlocal clock
            clock += seconds

        def assert_delivered() -> None:
            self.assertEqual(self.output.getvalue(), completed.stdout)
            self.assertEqual(self.error.getvalue(), completed.stderr)
            decoded = runner_result.read_result(result.path, compiler_status, selected_is_mac=False)
            assert isinstance(decoded, runner_result.UsableRunnerResult), decoded
            self.assertEqual((decoded.word, decoded.reason), ('passed' if compiler_status == 0 else 'failed', ''))

        def write_record(name: str, *_arguments: object) -> None:
            assert_delivered()
            writes.append(name)
            if name == fault:
                raise OSError('The fixture record write failed')

        def run(*_arguments: object) -> offload.MacOffloadResult:
            return self.run_claimed()

        with (self.context(),
              mock.patch.object(self.fixture.objects, 'execution_result', return_value=hana_result.encode_result(completed)),
              mock.patch.object(hana, 'release_assigned', return_value=release_result) as release,
              mock.patch.object(record, 'publish_hana_result', side_effect=partial(write_record, 'the Hana result')),
              mock.patch.object(record, 'publish_hana_loss') as loss,
              mock.patch.object(admission_client, 'report_execution',
                                side_effect=partial(write_record, 'the placement report')) as report,
              mock.patch.object(execution, 'record_attempt_charge', side_effect=partial(write_record, 'the Hana attempt charge')),
              mock.patch.object(time, 'monotonic', side_effect=lambda: clock),
              mock.patch.object(time, 'sleep', side_effect=advance),
              mock.patch.object(sys, 'argv', argv),
              mock.patch.object(sys.stderr, 'write') as stderr,
              mock.patch.object(offload, 'request_from_arguments', return_value=request),
              mock.patch.object(offload, 'run_offload', side_effect=run),
              mock.patch.object(offload, 'ProcessController', return_value=self.controller),
              mock.patch.object(offload, 'interrupt_handlers', return_value=nullcontext()),
              mock.patch.object(offload, 'restore_ignored_interrupt_default'),
              mock.patch.object(offload, 'persist_attempt', side_effect=partial(write_record, 'the attempt')),
              mock.patch.object(hana, 'read_execution_transport', return_value=self.transport),
              mock.patch.object(override_guard, 'refuse_overrides')):
            status = offload.main()
        self.assertEqual(status, compiler_status)
        assert_delivered()
        self.assertEqual(release.call_count, int(problem != 'fence'))
        self.assertEqual(writes, ['the Hana result', 'the Hana attempt charge', 'the placement report', 'the attempt'])
        loss.assert_not_called()
        report.assert_called_once()
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertEqual(sent.outcome, placement.AttemptCompleted(compiler_status))
        cleanup_pending = problem in ('fence', 'release')
        messages: list[str] = []
        if cleanup_pending:
            reason = (RELEASE_BLOCKED.reason if problem == 'release'
                      else 'The Hana fence, command termination, or lease release remains unconfirmed')
            self.assertEqual(sent.path, placement.DispatchPathPending(reason))
            messages.append(f'offload: Hana cleanup pending. {reason}')
        else:
            self.assertEqual(sent.path, placement.DispatchPathSettled())
        if fault:
            messages.append(f'offload: Cannot record {fault} (OSError).')
        self.assertEqual(stderr.call_args_list, [call for message in messages for call in (mock.call(message), mock.call('\n'))])
        self.assertIsInstance(self.controller.pending,
                              offload.PendingHanaOperation if cleanup_pending else offload.SettledDispatchOperations)
        evidence = self.controller.attempt_evidence
        self.assertEqual(evidence.completion, 'confirmed')
        self.assertEqual(evidence.cleanup, 'unconfirmed' if cleanup_pending else 'confirmed')
        self.assertEqual(execution.execution_command_settled(self.root, self.fixture.key.claim_id), problem != 'fence')
        self.assertEqual(execution.claim_dispatch_pending(self.root, self.fixture.reference), cleanup_pending)

    def finish_claimed(self, outcome: offload.MacOffloadResult,
                       result: offload.ResultDestination = offload.NO_RESULT_FILE) -> int:
        with (mock.patch.object(offload, 'report_placement_result'), mock.patch.object(offload, 'persist_attempt')):
            return offload.finish_offload(self.controller, replace(self.request, result=result), outcome, time.monotonic())

    def test_accepted_result_releases_the_claim_during_account_prepare(self) -> None:
        install_hana_spending_settings(self.transport.settings)
        numeric_settings(self.transport.settings, {'source_upload_timeout_s': '5', 'source_url_seconds': '60'})
        self.lifecycle.register(box.DeploymentIdentity('deployment', 'instance', 'spot', 'us-east-2'),
                                state=box.Lifecycle.READY)
        with mock.patch.object(self.lifecycle, 'image_is_current', return_value=True):
            self.assertIsInstance(self.lifecycle.request(self.fixture.key.claim_id, assigned=True), box.ClaimCommitted)
        account = deploy.Deployment(self.lifecycle.settings_path, self.root, self.root / 'output')
        inspect = self.lifecycle.inspect
        released: list[hana.HanaClaimRelease] = []

        def release_while_preparing(assignment: placement.PlacementAssignment, holder: placement.ClaimHolderIdentity,
                                    lifecycle: box.HanaController) -> hana.HanaClaimRelease:
            def finish_claim(*_arguments: object) -> deploy.PreparedDeployment:
                with mock.patch.object(lifecycle, 'inspect', wraps=inspect):
                    self.assertIsInstance(lifecycle.inspect(), box.HanaResourcesHeld)
                    released.append(hana_placement_adapter.release_assigned(assignment, holder, lifecycle))
                return deploy.PreparedDeployment(1000, 'fixture', {})

            with mock.patch.object(account, '_write_preparation', side_effect=finish_claim):
                _ = account.prepare()
            return released[-1]

        target = target_config.SshTarget('configured-target', 'linux', self.transport.settings)
        with (self.context(),
                mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                mock.patch.object(hana_placement_adapter, 'read_execution_transport', return_value=self.transport),
                mock.patch.dict(os.environ, {'BUILD_TARGETS_STATE_DIR': str(self.root / 'targets')}),
                mock.patch.object(admin_secrets, 'refuse_plain_admin'),
                mock.patch.object(deploy, 'read_deployment_settings'),
                mock.patch.object(deploy, 'read_budget_notification_address'),
                mock.patch.object(account, '_parameters'),
                mock.patch.object(account, '_guard'),
                mock.patch.object(hana, 'release_assigned', side_effect=release_while_preparing),
                mock.patch.object(record, 'publish_hana_result') as publish):
            outcome = self.run_claimed()
            self.assertEqual(self.finish_claimed(outcome), 0)

        self.assertIsInstance(outcome, offload.PassedOnTarget)
        self.assertEqual(released, [hana.HanaClaimReleased()])
        self.assertIsInstance(self.lifecycle.claim_status(self.fixture.key.claim_id), box.ClaimRefused)
        publish.assert_called_once()

    def pinned_verify(self) -> subprocess.CompletedProcess[str]:
        scripts = Path(__file__).resolve().parents[1]
        verify = (scripts / 'direct' / 'verify.sh').read_text()
        functions: list[str] = []
        for name in ('pinned_target_refuse', 'route_test'):
            header = f'{name}() {{'
            body = verify.split(f'\n{header}', 1)[1].split('\n}\n', 1)[0]
            functions.append(header + body + '\n}\n')
        result = self.root / 'runner.result'
        target = target_config.SshTarget('hana-aws', 'linux', self.transport.settings)
        with (self.context(),
                mock.patch.object(hana_placement_adapter, 'validate_assignment', return_value=self.transport),
                mock.patch.object(hana, 'claim_assigned', wraps=hana_placement_adapter.claim_assigned),
                mock.patch.object(target_config, 'read_targets', return_value=(target,)),
                mock.patch.dict(os.environ, {'BUILD_TARGETS_STATE_DIR': str(self.root / 'targets')})):
            outcome = self.run_claimed()
            status = offload.write_result(outcome, offload.ResultFile(result), verb='check', target=target.name)
        stubs = r"""
PINNED_TARGET=hana-aws
CMD=lint
MAC_ONLY=0
STEP_TARGET_TABLE=''
PLACEMENT_PROVIDER=ssh
HOLD_WAIT_DIR=.
DOC_REPO_TOP=.
release_token() { :; }
check_routing_hold() { :; }
placement_once() { :; }
collect_routing_wait() { :; }
remote_step_kind() { printf 'check'; }
verify_helper() { :; }
note_event() { :; }
wait_for_placement() { exit 78; }
try_reserved_target() {
    local -a fields
    mapfile -t fields < <("${VERIFY_FIXTURE[@]}" 0)
    LAST_TARGET_OUTCOME=${fields[0]}
    LAST_TARGET_REASON=${fields[1]}
}
route_test
"""
        # The shell uses the real runner decoder and the pinned refusal branch.
        command = 'VERIFY_FIXTURE=("$@")\n' + ''.join(functions) + stubs
        return subprocess.run(['bash', '-c', command, 'verify.sh', sys.executable,
            str(scripts / 'direct' / 'runner_result.py'), str(result), str(status)],
            cwd=self.root, check=False, capture_output=True, text=True, timeout=5)

    def establish_pinned_hana_assignment(self) -> None:
        install_hana_spending_settings(self.transport.settings)
        assignment = replace(self.fixture.assignment, destination=replace(self.fixture.assignment.destination,
            target='hana-aws'))
        self.request = replace(self.request, target='hana-aws', placement=offload.AssignedOffloadRequest(assignment))
        self.lifecycle.register(box.DeploymentIdentity('deployment', 'instance', 'spot', 'us-east-2'))

    def test_pinned_verify_refuses_when_another_claim_holds_hana(self) -> None:
        self.establish_pinned_hana_assignment()
        with mock.patch.object(self.lifecycle, 'image_is_current', return_value=True):
            self.assertIsInstance(self.lifecycle.request('other-claim'), box.ClaimCommitted)
            result = self.pinned_verify()

        self.assertEqual((result.returncode, result.stderr), (2, 'verify.sh: target hana-aws refused: Hana is busy\n'))

    def test_pinned_verify_refuses_the_same_way_during_setup(self) -> None:
        self.establish_pinned_hana_assignment()
        with hana_command_lock(self.lifecycle.directory) as access:
            self.assertIs(access, HanaCommandAccess.ACQUIRED)
            result = self.pinned_verify()
        self.assertEqual((result.returncode, result.stderr), (2, 'verify.sh: target hana-aws refused: Hana is busy\n'))

    def test_setup_hold_refuses_without_a_provider_claim(self) -> None:
        with self.context(), mock.patch.object(hana, 'claim_assigned', return_value=hana.HanaClaimResourcesHeld()), \
                mock.patch.object(execution, 'settle_unpublished_claim') as settle, \
                mock.patch.object(admission_client, 'report_execution') as report, \
                mock.patch.object(admission_client, 'claim_failure') as claim_failure:
            outcome = self.run_claimed()
            offload.report_placement_result(self.controller, self.request, outcome)
        assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
        self.assertEqual(outcome.reason, box.HANA_CLAIM_BUSY)
        self.assertIsInstance(self.controller.pending, offload.SettledDispatchOperations)
        self.assertIsInstance(self.controller.attempt_evidence.provider, placement.NoProviderClaim)
        settle.assert_not_called()
        claim_failure.assert_not_called()
        report.assert_called_once()
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertEqual(sent.claim, self.fixture.reference)
        self.assertEqual(sent.outcome, execution_identity.HanaCommandAdmissionRefused(box.HANA_CLAIM_BUSY))
        self.assertIsInstance(sent.evidence, execution_identity.ExecutionNotStarted)
        self.assertIsInstance(sent.path, placement.DispatchPathSettled)

    def test_prelaunch_refusal_reports_the_fault_without_publishing_a_command(self) -> None:
        root = self.root
        oversized = 'SourceUploadBound: archive exceeds its size bound'
        for case, release_succeeds in (('stale image', True), ('stale image', False),
                                       ('oversized archive', True), ('oversized archive', False),
                                       ('consume refusal', True), ('consume refusal', False),
                                       ('pending renewal', False)):
            with (self.subTest(case=case, release_succeeds=release_succeeds), tempfile.TemporaryDirectory(dir=root) as folder):
                self.root = Path(folder)
                self.controller = offload.ProcessController()
                self.lifecycle = box.HanaController(self.root, self.root / 'account.conf', self.transport.settings,
                    cast(box.HanaProvider, cast(object, self.fixture.objects)))
                detail = oversized if case == 'oversized archive' else ''
                reason = 'The Hana payload could not be prepared: ' + detail if detail else placement.HANA_IMAGE_STALE
                if case == 'pending renewal':
                    reason = 'The claim, boot identity, or delivered permit is stale'
                elif case == 'consume refusal':
                    reason = 'the exact claim is not adopted'
                self.assert_prelaunch_refusal(reason, oversized=detail, release_succeeds=release_succeeds,
                                              renewal_pending=case == 'pending renewal', consume_refused=case == 'consume refusal')

    def assert_prelaunch_refusal(self, reason: str, *, oversized: str, release_succeeds: bool,
                                 renewal_pending: bool, consume_refused: bool) -> None:
        uploaded = False

        def upload() -> None:
            nonlocal uploaded
            uploaded = True

        def permission(*_args: object) -> box.CommandPermission | box.ClaimRefused:
            return box.ClaimRefused(reason) if uploaded else self.fixture.permission

        with (self.context() as context,
              mock.patch.object(hana, 'release_assigned', return_value=hana.HanaClaimReleased() if release_succeeds else RELEASE_BLOCKED) as release,
              mock.patch.object(admission_client, 'report_execution') as report):
            if oversized:
                _ = context.enter_context(mock.patch.object(hana_transport.SourceSender, 'prepare',
                    side_effect=hana_protocol.SourceRefused(oversized)))
            elif consume_refused:
                _ = context.enter_context(mock.patch.object(admission_client, 'consume_permission',
                    return_value=wire.DispatchStatus(self.fixture.assignment, placement.CurrentAssignment(),
                                                    placement.PermissionAvailable(), reason)))
            elif renewal_pending:
                _ = context.enter_context(mock.patch.object(hana_transport.SourceSender, 'prepare',
                    return_value=(self.fixture.manifest, upload)))
                _ = context.enter_context(mock.patch.object(self.lifecycle, 'permission', side_effect=permission))
            else:
                _ = context.enter_context(mock.patch.object(self.lifecycle, 'permission',
                    return_value=box.ClaimRefused(reason)))
            outcome = self.run_claimed()
            offload.report_placement_result(self.controller, self.request, outcome)
        release.assert_called_once()
        report.assert_called_once()
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertEqual(sent.outcome, execution_identity.HanaCommandAdmissionRefused(reason))
        assert isinstance(sent.outcome, execution_identity.HanaCommandAdmissionRefused)
        self.assertIsInstance(sent.path, placement.DispatchPathSettled if release_succeeds else placement.DispatchPathPending)
        self.assertIsInstance(sent.evidence, execution_identity.ExecutionNotStarted)
        self.assertEqual(execution.claim_admission_refused(self.root, self.fixture.reference), release_succeeds)
        self.assertEqual(execution.claim_dispatch_pending(self.root, self.fixture.reference), not release_succeeds)
        assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
        self.assertEqual(outcome.reason, reason)
        if release_succeeds:
            settled = self.controller.hana_settlement
            assert isinstance(settled, execution.HanaSettled)
            self.assertEqual(settled.outcome, execution_identity.HanaCommandAdmissionRefused(reason))
        else:
            self.assertIsInstance(self.controller.hana_settlement, offload.NoHanaSettlement)
        self.assertEqual(self.fixture.objects.envelopes, [])
        self.assertEqual(placement.retry_decision(self.fixture.assignment.destination, sent.outcome, 1000),
                         placement.DoNotRetry(reason))
        result_path = self.root / 'result'
        with (mock.patch.dict(os.environ, {'BUILDLOG_PINNED_TARGET': self.request.target}),
                mock.patch.object(eligibility, 'configured_ssh_target', return_value=target_config.SshTarget(
                    self.request.target, 'linux', self.transport.settings)),
                mock.patch('sys.stdout', new_callable=StringIO) as output):
            self.assertEqual(offload.write_result(outcome, offload.ResultFile(result_path), target=self.request.target), 75)
        self.assertEqual(output.getvalue(), '')
        self.assertIn(f'reason={reason}\n', result_path.read_text())
        if renewal_pending:
            self.assertTrue(uploaded)
            released = execution.settle_execution(self.root, self.fixture.request, self.fixture.objects,
                release_claim=hana.HanaClaimReleased, reason='Replay the command settlement',
                fence_window=execution.FenceAcknowledgementWindow(0, 1))
            self.assertEqual(released, execution.HanaAdmissionRefusalSettled(
                execution_identity.HanaCommandAdmissionRefused(reason), execution_identity.ExecutionNotStarted()))
            self.assertFalse(execution.claim_dispatch_pending(self.root, self.fixture.reference))

    def test_invalid_control_documents_refuse_before_the_archive_or_command_upload(self) -> None:
        root = self.root
        for case in ('request size', 'request binding', 'envelope reserve', 'envelope size', 'envelope access'):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=root) as folder:
                self.root = Path(folder)
                self.controller = offload.ProcessController()
                self.lifecycle = box.HanaController(self.root, self.root / 'account.conf', self.transport.settings,
                    cast(box.HanaProvider, cast(object, self.fixture.objects)))
                self.assert_control_refusal(case)

    def assert_control_refusal(self, case: str) -> None:
        manifest = self.fixture.manifest
        original = self.request
        access = self.fixture.objects.execution_access(self.fixture.key)
        bound = 'The Hana control document exceeds its protocol bound'
        if case in ('request size', 'envelope reserve'):
            word = 'x' * (32769 if case == 'request size' else 30000)
            self.request = replace(self.request, nextest_words=(word,))
            manifest = replace(manifest, argv=('cargo', 'check', word))
            self.assertLess(len(manifest.encode()), hana_protocol.CONTROL_MAX_BYTES)
            if case == 'envelope reserve':
                _ = hana_protocol.encode_request(replace(self.fixture.request, manifest=manifest, argv=manifest.argv))
            cause = bound
        elif case == 'request binding':
            manifest = replace(manifest, argv=('cargo', 'clippy'))
            cause = 'The Hana source, command, or attempt binding differs'
        elif case == 'envelope size':
            access = replace(access, result_url=access.result_url + 'x' * hana_protocol.CONTROL_MAX_BYTES)
            cause = bound
        else:
            access = replace(access, result_url='invalid')
            cause = 'The Hana publication URL is invalid'
        upload = mock.Mock()
        with (self.context(),
              mock.patch.object(hana_transport.SourceSender, 'prepare', return_value=(manifest, upload)),
              mock.patch.object(self.fixture.objects, 'execution_access', return_value=access),
              mock.patch.object(self.lifecycle, 'permission', return_value=self.fixture.permission) as permission,
              mock.patch.object(admission_client, 'consume_permission',
                                return_value=wire.FirstAuthorization(self.fixture.assignment, self.fixture.reference)) as consume,
              mock.patch.object(execution, 'settle_execution', wraps=execution.settle_execution) as settle_dispatch,
              mock.patch.object(hana, 'release_assigned', return_value=hana.HanaClaimReleased()) as release,
              mock.patch.object(self.fixture.objects, 'observe_execution') as observe):
            outcome = self.run_claimed()
        self.request = original
        assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
        self.assertIn(cause, outcome.reason)
        self.assertTrue(execution.claim_admission_refused(self.root, self.fixture.reference))
        self.assertEqual(self.fixture.objects.envelopes, [])
        upload.assert_not_called()
        if case in ('envelope size', 'envelope access'):
            consume.assert_called_once()
            permission.assert_called_once()
            settle_dispatch.assert_called_once()
        else:
            consume.assert_not_called()
            permission.assert_not_called()
            settle_dispatch.assert_not_called()
        release.assert_called_once()
        observe.assert_not_called()

    def test_stale_image_at_claim_reports_a_settled_refusal(self) -> None:
        cause = placement.HANA_IMAGE_STALE
        with (self.context(), mock.patch.object(hana, 'claim_assigned', return_value=hana.HanaClaimRefused(cause)),
                mock.patch.object(hana, 'release_assigned', return_value=hana.HanaClaimReleased()) as release,
                mock.patch.object(admission_client, 'report_execution') as report):
            outcome = self.run_claimed()
            offload.report_placement_result(self.controller, self.request, outcome)
        assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
        self.assertEqual(outcome.reason, cause)
        report.assert_called_once()
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertEqual(sent.claim, self.fixture.reference)
        self.assertEqual(sent.outcome, execution_identity.HanaCommandAdmissionRefused(cause))
        self.assertIsInstance(sent.evidence, execution_identity.ExecutionNotStarted)
        self.assertIsInstance(sent.path, placement.DispatchPathSettled)
        self.assertTrue(execution.claim_admission_refused(self.root, self.fixture.reference))
        release.assert_called_once()
        self.assertEqual(self.fixture.objects.envelopes, [])

    def test_claim_refusal_replay_requires_release_of_the_previous_claim(self) -> None:
        refusal = execution_identity.HanaCommandAdmissionRefused(placement.HANA_IMAGE_STALE)
        claim = hana.HanaClaimRefused('The Hana attempt already has a settled command path')
        root = self.root
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), tempfile.TemporaryDirectory(dir=root) as folder:
                self.root = Path(folder)
                self.controller = offload.ProcessController()
                self.lifecycle = box.HanaController(self.root, self.root / 'account.conf', self.transport.settings,
                    cast(box.HanaProvider, cast(object, self.fixture.objects)))
                release_result = hana.HanaClaimReleased() if cancelled else RELEASE_BLOCKED
                saved = execution.settle_unpublished_claim(self.root, self.fixture.reference,
                    release_claim=lambda: release_result, reason=refusal, cancelled=cancelled)
                self.assertIsInstance(saved, execution.HanaSettled if cancelled else execution.HanaSettlementPending)
                self.assertIsInstance(self.controller.attempt_evidence.provider, placement.NoProviderClaim)
                with (self.context(), mock.patch.object(hana, 'claim_assigned', return_value=claim),
                      mock.patch.object(hana, 'release_assigned', return_value=RELEASE_BLOCKED) as release):
                    outcome = self.run_claimed()
                if cancelled:
                    assert isinstance(outcome, offload.DeclinedMacRun)
                    self.assertEqual((outcome.reason, outcome.detail), ('state', claim.reason))
                    release.assert_not_called()
                else:
                    assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
                    self.assertEqual(outcome.reason, refusal.reason)
                    release.assert_called_once()
                    self.assertIsInstance(self.controller.pending, offload.PendingHanaOperation)
                self.assertEqual(execution.claim_dispatch_pending(self.root, self.fixture.reference), not cancelled)
                self.assertFalse(execution.claim_admission_refused(self.root, self.fixture.reference))

    def test_nonzero_compiler_status_remains_a_terminal_failure(self) -> None:
        with self.context(), mock.patch.object(self.fixture.objects, 'execution_result',
                return_value=hana_result.encode_result(self.fixture.command_result(1))), \
                mock.patch.object(record, 'publish_hana_result') as publish:
            outcome = self.run_claimed()
            self.assertEqual(self.finish_claimed(outcome), 1)
        self.assertIsInstance(outcome, offload.FailedOnTarget)
        assert isinstance(outcome, offload.FailedOnTarget)
        self.assertEqual(outcome.status, 1)
        publish.assert_called_once()

    def test_source_refusal_releases_the_claim_for_fallback(self) -> None:
        cause = 'IgnoredInput: remove the ignored path'
        reason = 'The Hana payload could not be prepared: ' + cause
        assigned = replace(self.request, result=offload.ResultFile(self.root / 'result'))
        repository = eligibility.KnownRepository('hana', 'github.com/natepiano/hana')

        def run(runner: offload.ProcessController, transport: hana.HanaControlTransport,
                placed: offload.OffloadRequest, started: float) -> offload.MacOffloadResult:
            return offload.claimed_run(runner, transport, placed, repository, started)

        for request in (assigned, replace(assigned, placement=offload.DirectOffloadRequest())):
            with self.subTest(request=request.placement):
                output = StringIO()
                controller = offload.ProcessController()
                directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir=self.root)))
                with self.context(), ExitStack() as patches:
                    _ = patches.enter_context(mock.patch.object(self.lifecycle, 'directory', directory))
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
                        side_effect=hana_protocol.SourceRefused(cause)))
                    release = patches.enter_context(mock.patch.object(hana, 'release_assigned', return_value=hana.HanaClaimReleased()))
                    report = patches.enter_context(mock.patch.object(admission_client, 'report_execution'))
                    _ = patches.enter_context(mock.patch.object(admission_client, 'cancel_placement'))
                    _ = patches.enter_context(mock.patch.object(offload, 'persist_attempt'))
                    status = offload.main()
                self.assertEqual(status, 75)
                self.assertEqual(submit.call_count, int(isinstance(request.placement, offload.DirectOffloadRequest)))
                claim.assert_called_once()
                capture.assert_called_once()
                release.assert_called_once()
                report.assert_called_once()
                sent = cast(wire.ExecutionReport, report.call_args.args[0])
                self.assertEqual(sent.outcome, execution_identity.HanaCommandAdmissionRefused(reason))
                self.assertIsInstance(sent.path, placement.DispatchPathSettled)
                self.assertIsInstance(sent.evidence, execution_identity.ExecutionNotStarted)
                self.assertFalse(execution.claim_dispatch_pending(directory, self.fixture.reference))
                self.assertEqual(self.fixture.objects.envelopes, [])
                self.assertEqual(output.getvalue().splitlines(), [
                    f'mac_test: The Hana command did not start on configured-target. {reason}'])
                result = runner_result.read_result(self.root / 'result', status, False)
                assert isinstance(result, runner_result.UsableRunnerResult)
                self.assertEqual((result.word, result.reason), ('hana_command_admission_refused', reason))

    def checkout_with_ignored_path(self) -> Path:
        checkout = self.root / 'checkout'
        checkout.mkdir()
        _ = copies.git(checkout, 'init', '--quiet')
        _ = (checkout / '.gitignore').write_text('refused.rs\n')
        return checkout

    def test_source_refusal_reports_fallback_without_requesting_a_claim(self) -> None:
        checkout = self.checkout_with_ignored_path()
        _ = (checkout / 'refused.rs').write_text('ignored build input\n')
        with self.assertRaisesRegex(hana_protocol.SourceRefused, 'IgnoredInput') as refusal:
            hana_transport.check_inputs(checkout, conf.read(self.transport.settings))
        reason = 'The Hana payload could not be prepared: ' + str(refusal.exception)
        assigned = replace(self.request, repo_root=checkout, result=offload.ResultFile(self.root / 'result'))
        repository = eligibility.KnownRepository('hana', 'github.com/natepiano/hana')

        def run(runner: offload.ProcessController, transport: hana.HanaControlTransport,
                placed: offload.OffloadRequest, started: float) -> offload.MacOffloadResult:
            return offload.claimed_run(runner, transport, placed, repository, started)

        for request in (assigned, replace(assigned, placement=offload.DirectOffloadRequest())):
            with self.subTest(request=request.placement):
                output = StringIO()
                controller = offload.ProcessController()
                directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir=self.root)))
                with self.context(real_source=True), ExitStack() as patches:
                    _ = patches.enter_context(mock.patch.object(self.lifecycle, 'directory', directory))
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
                    capture = patches.enter_context(mock.patch.object(hana_transport.SourceSender, 'prepare'))
                    release = patches.enter_context(mock.patch.object(hana, 'release_assigned', return_value=hana.HanaClaimReleased()))
                    start_box = patches.enter_context(mock.patch.object(self.lifecycle, 'request'))
                    upload = patches.enter_context(mock.patch.object(hana_transport.AwsSourceObjects, 'put_if_absent'))
                    report = patches.enter_context(mock.patch.object(admission_client, 'report_execution'))
                    _ = patches.enter_context(mock.patch.object(admission_client, 'cancel_placement'))
                    _ = patches.enter_context(mock.patch.object(offload, 'persist_attempt'))
                    status = offload.main()
                self.assertEqual(status, 75)
                self.assertEqual(submit.call_count, int(isinstance(request.placement, offload.DirectOffloadRequest)))
                claim.assert_not_called()
                capture.assert_not_called()
                release.assert_not_called()
                start_box.assert_not_called()
                upload.assert_not_called()
                report.assert_called_once()
                sent = cast(wire.ExecutionReport, report.call_args.args[0])
                self.assertEqual(sent.outcome, execution_identity.HanaCommandAdmissionRefused(reason))
                self.assertIsInstance(sent.path, placement.DispatchPathSettled)
                self.assertIsInstance(sent.evidence, execution_identity.ExecutionNotStarted)
                self.assertFalse(self.lifecycle.execution_bound(self.fixture.key.claim_id))
                self.assertFalse(execution.execution_path(directory, self.fixture.key.claim_id).exists())
                self.assertEqual(self.fixture.objects.envelopes, [])
                self.assertEqual(output.getvalue().splitlines(), [
                    f'mac_test: The Hana command did not start on configured-target. {reason}'])
                result = runner_result.read_result(self.root / 'result', status, False)
                assert isinstance(result, runner_result.UsableRunnerResult)
                self.assertEqual((result.word, result.reason), ('hana_command_admission_refused', reason))

    def test_ignored_file_added_during_claim_is_refused_after_adoption(self) -> None:
        checkout = self.checkout_with_ignored_path()
        self.request = replace(self.request, repo_root=checkout)

        def adopt(*_args: object, **_options: object) -> hana.HanaClaimAdopted:
            _ = (checkout / 'refused.rs').write_text('ignored build input\n')
            return self.fixture.adopted

        with (self.context(real_source=True),
              mock.patch.object(hana, 'claim_assigned', side_effect=adopt) as claim,
              mock.patch.object(hana, 'release_assigned', return_value=hana.HanaClaimReleased()) as release,
              mock.patch.object(hana_transport.AwsSourceObjects, 'put_if_absent') as upload):
            outcome = self.run_claimed()

        assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
        self.assertTrue(outcome.reason.startswith('The Hana payload could not be prepared: IgnoredInput:'))
        claim.assert_called_once()
        release.assert_called_once()
        upload.assert_not_called()
        self.assertEqual(self.fixture.objects.envelopes, [])

    def test_unreadable_lifecycle_preserves_claim_refusal(self) -> None:
        checkout = self.checkout_with_ignored_path()
        _ = (checkout / 'refused.rs').write_text('ignored build input\n')
        self.request = replace(self.request, repo_root=checkout)
        self.lifecycle.path.parent.mkdir()
        _ = self.lifecycle.path.write_text('{')

        with (self.context(real_source=True),
              mock.patch.object(hana, 'claim_assigned', return_value=hana.HanaClaimResourcesHeld()) as claim,
              mock.patch.object(hana, 'release_assigned') as release):
            outcome = self.run_claimed()

        assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
        self.assertEqual(outcome.reason, box.HANA_CLAIM_BUSY)
        claim.assert_called_once()
        release.assert_not_called()
        self.assertFalse(execution.execution_path(self.root, self.fixture.key.claim_id).exists())

    def test_source_change_inside_capture_returns_a_usable_refusal(self) -> None:
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
        self.assertIsInstance(outcome, offload.HanaCommandAdmissionRefused)
        result = runner_result.read_result(report, status, False)
        assert isinstance(result, runner_result.UsableRunnerResult)
        self.assertEqual((result.word, result.reason),
                         ('hana_command_admission_refused', 'SourceChanged: inputs changed during source capture'))
        self.assertEqual(self.controller.attempt_evidence.cleanup, 'confirmed')
        self.assertEqual(self.fixture.objects.envelopes, [])

    def test_killed_capture_git_ends_the_attempt_with_a_runner_fault(self) -> None:
        limits = hana_protocol.SourceLimits(2, ())

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
        self.assertIn('outcome=hana_command_admission_refused\n', report.read_text())
        self.assertIn('The Hana source capture failed', output.getvalue())
        self.assertIn('SIGKILL', output.getvalue())
        self.assertEqual(self.controller.attempt_evidence.cleanup, 'confirmed')
        self.assertEqual(self.fixture.objects.envelopes, [])

    def assert_result_transfer_loss(self, outcome: offload.MacOffloadResult, reason: str) -> None:
        self.assertIsInstance(outcome, offload.LostMacRun)
        assert isinstance(outcome, offload.LostMacRun)
        self.assertEqual(outcome.detail, reason)
        self.assertEqual(self.output.getvalue(), b'')
        self.assertEqual(self.error.getvalue(), b'')
        report = self.root / 'transfer-loss.json'
        with redirect_stdout(StringIO()) as printed:
            status = self.finish_claimed(outcome, offload.ResultFile(report))
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
            evidence=execution_identity.ExecutionEvidenceUnavailable('The command result is pending', 1000))
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
                                      timeout_s: float | None = None) -> hana_protocol.HanaCommandObservation:
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
                         [hana_protocol.HanaCommandEnvelope, hana_protocol.HanaFenceEnvelope])

    def test_result_upload_timeout_reports_without_downloading_a_result(self) -> None:
        self.assert_upload_loss('HanaResultUploadTimedOut: The result upload exceeded 83 s (result_transfer_timeout_s)')

    def test_result_upload_window_refusal_reports_without_downloading_a_result(self) -> None:
        self.assert_upload_loss('HanaResultUploadFailed: The signed access window cannot fit a result upload of 83 s and settlement')

    def assert_upload_loss(self, reason: str) -> None:
        objects = self.fixture.objects
        observation = replace(objects.observe_execution(self.fixture.key), accepted=True,
            evidence=execution_identity.ExecutionEvidenceUnavailable(reason, 1000))
        observe = objects.observe_execution

        def upload_failed(key: hana_result.HanaAttemptKey, *,
                          timeout_s: float | None = None) -> hana_protocol.HanaCommandObservation:
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

    def test_source_change_during_settlement_preserves_the_accepted_result(self) -> None:
        from scripts.build_queue import admission_client

        with self.context(), mock.patch.object(offload, 'hana_source_key', return_value=self.fixture.source_key) as source, \
                mock.patch.object(record, 'publish_hana_result') as publish, \
                mock.patch.object(record, 'publish_hana_loss') as loss, \
                mock.patch.object(admission_client, 'report_execution') as report, \
                mock.patch.object(offload, 'persist_attempt'):
            def release(*_: object) -> hana.HanaClaimRelease:
                source.return_value = execution_identity.SourceKeyKnown('changed-source')
                return hana.HanaClaimReleased()

            with mock.patch.object(hana, 'release_assigned', side_effect=release):
                outcome = self.run_claimed()
            self.assertEqual(offload.finish_offload(self.controller, self.request, outcome, time.monotonic()), 0)
            replay = execution.replay_accepted_result(self.root, self.fixture.request)
        self.assertIsInstance(outcome, offload.PassedOnTarget)
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertEqual(sent.outcome, placement.AttemptCompleted(0))
        self.assertIsInstance(sent.path, placement.DispatchPathSettled)
        self.assertIsInstance(replay, hana_result.AcceptedHanaResult)
        row = hana_result.object_fields(cast(object, json.loads(
            execution.execution_path(self.root, self.fixture.key.claim_id).read_text())))
        self.assertEqual(hana_result.object_fields(row['outcome'])['kind'], 'completed')
        publish.assert_called_once()
        loss.assert_not_called()

    def test_replaced_result_creates_no_final_row_or_verify_cache(self) -> None:
        stale = replace(self.fixture.command_result(), key=replace(self.fixture.key, attempt='replaced'))
        placement_path = self.root / 'assignment.json'
        objects = self.fixture.objects
        observation = replace(objects.observe_execution(self.fixture.key), accepted=True,
            evidence=execution_identity.ExecutionEvidenceUnavailable(hana_protocol.COMMAND_RESULT_PENDING, 1000))
        with self.context(), mock.patch.object(self.fixture.objects, 'execution_result', return_value=hana_result.encode_result(stale)), \
                mock.patch.object(objects, 'observe_execution', return_value=observation), \
                mock.patch.object(record, 'publish_hana_result') as publish, \
                mock.patch.object(record, 'publish_hana_loss') as loss, \
                mock.patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': str(placement_path)}):
            outcome = self.run_claimed()
            result_file = self.root / 'verify-result.json'
            status = self.finish_claimed(outcome, offload.ResultFile(result_file))
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
                upload_source=lambda: None,
                consume=lambda request: wire.FirstAuthorization(self.fixture.assignment, self.fixture.reference),
                current_permission=lambda: self.fixture.permission,
                current_source_key=lambda: self.fixture.source_key)
            _ = execution.settle_execution(self.root, self.fixture.request, self.fixture.objects,
                release_claim=hana.HanaClaimReleased, reason='The original owner cancelled the attempt',
                fence_window=execution.FenceAcknowledgementWindow(time.monotonic() + 113, 1))
            outcome = self.run_claimed()
            result_file = self.root / 'fenced-result.json'
            status = self.finish_claimed(outcome, offload.ResultFile(result_file))
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
                                                               execution_identity.ExecutionNotStarted())
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
                mock.patch.object(hana, 'release_assigned', return_value=RELEASE_BLOCKED), \
                mock.patch.object(admission_client, 'report_execution') as report:
            outcome = self.run_claimed()
            offload.report_placement_result(self.controller, self.request, outcome)
        assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertEqual(sent.outcome, execution_identity.HanaCommandAdmissionRefused(outcome.reason))
        self.assertIsInstance(sent.path, placement.DispatchPathPending)
        self.assertIsInstance(sent.evidence, execution_identity.ExecutionNotStarted)
        self.assertTrue(execution.claim_dispatch_pending(self.root, self.fixture.reference))
        self.assertEqual(self.fixture.objects.envelopes, [])

    def test_failed_fence_keeps_a_command_without_an_accepted_result_pending(self) -> None:
        self.fixture.objects.fail_fence = True
        monotonic = time.monotonic()

        def advance(seconds: float) -> None:
            nonlocal monotonic
            monotonic += seconds

        mismatched = replace(self.fixture.command_result(), argv=('cargo', 'clippy'))
        with (self.context(), mock.patch.object(record, 'publish_hana_result') as publish,
              mock.patch.object(self.fixture.objects, 'execution_result', return_value=hana_result.encode_result(mismatched)),
              mock.patch.object(time, 'monotonic', side_effect=lambda: monotonic),
              mock.patch.object(time, 'sleep', side_effect=advance) as pause):
            outcome = self.run_claimed()
        self.assertIsInstance(outcome, offload.LostMacRun)
        self.assertIsInstance(self.controller.pending, offload.PendingHanaOperation)
        pause.assert_called()
        publish.assert_not_called()

    def test_terminal_guest_refusal_supplies_one_cause_to_the_pinned_runner(self) -> None:
        root = self.root
        disabled = 'Hana command admission is disabled on the guest'
        installed_abi = hana_protocol.RUNTIME_ABI
        requested_abi = installed_abi + 1
        abi_reason = f'The Hana runtime ABI {requested_abi} differs from the image ABI {installed_abi}'
        for code, reason, abi, refusal_in_fence_ack, release_succeeds in (
            (CommandAdmissionRefusalCode.ADMISSION_DISABLED, disabled, installed_abi, False, True),
            (CommandAdmissionRefusalCode.ADMISSION_DISABLED, disabled, installed_abi, True, True),
            (CommandAdmissionRefusalCode.ADMISSION_DISABLED, disabled, installed_abi, False, False),
            (CommandAdmissionRefusalCode.ADMISSION_DISABLED, disabled, installed_abi, True, False),
            (CommandAdmissionRefusalCode.PRE_COMMAND_FAILED, abi_reason, requested_abi, False, True),
            (CommandAdmissionRefusalCode.PRE_COMMAND_FAILED, abi_reason, requested_abi, False, False),
        ):
            with (self.subTest(code=code, refusal_in_fence_ack=refusal_in_fence_ack, release_succeeds=release_succeeds),
                  tempfile.TemporaryDirectory(dir=root) as folder):
                self.root = Path(folder)
                self.fixture = fixtures.HanaExecutionTests()
                self.fixture.manifest = replace(self.fixture.manifest, runtime_abi=abi)
                self.fixture.request = replace(self.fixture.request, manifest=self.fixture.manifest)
                self.controller = offload.ProcessController()
                self.lifecycle = box.HanaController(self.root, self.root / 'account.conf', self.transport.settings,
                    cast(box.HanaProvider, cast(object, self.fixture.objects)))
                self.assert_terminal_guest_refusal(code, reason, refusal_in_fence_ack=refusal_in_fence_ack,
                                                   release_succeeds=release_succeeds)

    def assert_terminal_guest_refusal(self, code: CommandAdmissionRefusalCode, reason: str, *,
                                      refusal_in_fence_ack: bool, release_succeeds: bool) -> None:
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
        holder = queue_fixtures.coordinator_holder(assigned)
        generation = self.fixture.adopted.generation
        now = int(self.lifecycle.clock())
        work_claim = box.WorkClaim(claim.claim_id, 'fixture-token', now, active=True)
        snapshot = box.HanaLifecycleEvidence(
            box.DeploymentIdentity(generation.deployment, 'fixture-instance', 'fixture-spot', 'us-east-2'),
            box.Lifecycle.READY, box.VerifiedBoot(generation.boot_id, generation.start_token), box.NoOperation(),
            box.DeliveredPermit(3, now + 1800, claim.claim_id), (work_claim,), now)
        with (mock.patch.dict(os.environ, {'BUILD_TARGETS_STATE_DIR': str(self.root / 'targets')}),
              mock.patch.object(hana_placement_adapter, 'validate_assignment', return_value=self.transport),
              mock.patch.object(hana_placement_adapter, 'controller_for', return_value=self.lifecycle),
              mock.patch.object(self.lifecycle, 'request', return_value=box.ClaimCommitted(work_claim)),
              mock.patch.object(self.lifecycle, 'read_saved_lifecycle', return_value=snapshot)):
            adopted = hana_placement_adapter.claim_assigned(assigned, holder, self.lifecycle)
            assert isinstance(adopted, hana.HanaClaimAdopted), adopted
            authorization, = queue.connect(queue_fixtures.consume_permission('consume', assigned, claim)).replies()
        self.assertEqual(authorization, wire.FirstAuthorization(assigned, claim))
        self.fixture.assignment, self.fixture.reference = assigned, claim
        self.fixture.adopted = adopted
        self.fixture.key = replace(self.fixture.key, episode=assigned.episode, attempt=assigned.attempt, claim_id=claim.claim_id)
        self.fixture.request = replace(self.fixture.request, key=self.fixture.key,
                                       link=replace(self.fixture.request.link, episode=assigned.episode))
        self.fixture.permission = replace(self.fixture.permission, execution=claim.claim_id)

        objects = self.fixture.objects
        objects.request = self.fixture.request
        objects.fail_fence = not refusal_in_fence_ack
        observation = hana_protocol.ConfirmedCommandAdmissionRefusal(
            self.fixture.key, self.fixture.request.slot, False, 1000, reason, code)
        monotonic = time.monotonic()
        boot_deadline = int(time.time()) + 200
        publish = objects.publish_execution

        def publish_near_deadline(content: bytes) -> None:
            nonlocal monotonic
            publish(content)
            envelope = hana_protocol.decode_envelope(content)
            if refusal_in_fence_ack and isinstance(envelope, hana_protocol.HanaCommandEnvelope):
                monotonic += 90

        def observe_refusal(key: hana_result.HanaAttemptKey, *,
                            timeout_s: float | None = None) -> hana_protocol.ConfirmedCommandAdmissionRefusal:
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

        def release_claim(assignment: placement.PlacementAssignment, _holder: placement.ClaimHolderIdentity,
                          lifecycle: box.HanaController) -> hana.HanaClaimRelease:
            self.assertEqual(assignment, assigned)
            self.assertIs(lifecycle, self.lifecycle)
            self.assertTrue(execution.execution_command_settled(self.root, claim.claim_id))
            return hana.HanaClaimReleased() if release_succeeds else RELEASE_BLOCKED

        def report_after_release(request: wire.ExecutionReport) -> wire.DispatchStatus:
            release.assert_called_once()
            self.assertEqual(execution.claim_admission_refused(self.root, claim), release_succeeds)
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
              mock.patch.object(hana, 'release_assigned', side_effect=release_claim) as release,
              mock.patch.object(record, 'publish_hana_loss') as loss,
              mock.patch.object(record, 'ssh_attempt'),
              mock.patch.object(admission_client, 'report_execution', side_effect=report_after_release) as report,
              mock.patch.dict(os.environ, {'BUILDLOG_PINNED_TARGET': target.name}),
              redirect_stdout(StringIO()) as text):
            status = offload.main()
        self.assertEqual(text.getvalue(), '')
        self.assertEqual(status, 75)
        decoded = runner_result.read_result(result.path, status, selected_is_mac=False)
        assert isinstance(decoded, runner_result.UsableRunnerResult)
        self.assertEqual((decoded.word, decoded.reason), ('hana_command_admission_refused', reason))
        self.assertIsInstance(self.controller.pending,
                              offload.SettledDispatchOperations if release_succeeds else offload.PendingHanaOperation)
        self.assertTrue(execution.execution_command_settled(self.root, self.fixture.key.claim_id))
        self.assertEqual(execution.claim_admission_refused(self.root, self.fixture.reference), release_succeeds)
        self.assertEqual(execution.claim_dispatch_pending(self.root, self.fixture.reference), not release_succeeds)
        release.assert_called_once()
        fetch.assert_not_called()
        report.assert_called_once()
        sent = cast(wire.ExecutionReport, report.call_args.args[0])
        self.assertEqual(sent.outcome, execution_identity.HanaCommandAdmissionRefused(reason))
        self.assertIsInstance(sent.evidence, execution_identity.ExecutionNotStarted)
        path = (placement.DispatchPathSettled() if release_succeeds
                else placement.DispatchPathPending(RELEASE_BLOCKED.reason))
        self.assertEqual(sent.path, path)
        cleanup = 'confirmed' if release_succeeds else 'unconfirmed'
        self.assertIn(f'cleanup={cleanup}', result.path.read_text().splitlines())
        saved = queue.server.assignments[execution_identity.attempt_key(assigned.episode, assigned.attempt)]
        self.assertEqual(saved.state, placement.ClosedAssignment(reason) if release_succeeds else placement.CurrentAssignment())
        self.assertEqual(saved.retry, placement.DoNotRetry(reason))
        envelopes: list[type[hana_protocol.HanaExecutionEnvelope]] = [hana_protocol.HanaCommandEnvelope]
        if refusal_in_fence_ack:
            envelopes.append(hana_protocol.HanaFenceEnvelope)
        self.assertEqual([type(envelope) for envelope in objects.envelopes], envelopes)
        observe.assert_called_once()
        if release_succeeds:
            loss.assert_called_once()
        else:
            loss.assert_not_called()

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
        root = self.root
        for case in ('repaired', 'sender refusal', 'publication refused', 'guest refusal'):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=root) as folder:
                self.root = Path(folder)
                self.request = replace(self.request, repo_root=self.root)
                self.fixture = fixtures.HanaExecutionTests()
                self.controller = offload.ProcessController()
                self.lifecycle = box.HanaController(self.root, self.root / 'account.conf', self.transport.settings,
                    cast(box.HanaProvider, cast(object, self.fixture.objects)))
                self.assert_source_repair(case)

    def assert_source_repair(self, case: str) -> None:
        from scripts.aws_target.hana_source import MissingBasis

        self.fixture.manifest = replace(self.fixture.manifest, basis_commits=('d' * 40,))
        self.fixture.request = replace(self.fixture.request, manifest=self.fixture.manifest)
        objects = self.fixture.objects
        objects.request = self.fixture.request
        objects.missing_basis = True
        objects.terminated = True
        objects.lease_free = True
        complete = replace(self.fixture.manifest, basis_commits=(), bundle_sha256='e' * 64)
        cause = 'The payload exceeds its size bound'
        observe = objects.observe_execution

        def observe_repair(key: hana_result.HanaAttemptKey, *,
                           timeout_s: float | None = None) -> hana_protocol.HanaExecutionObservation:
            observation = observe(key, timeout_s=timeout_s)
            if case == 'guest refusal' and observation.source_updated:
                return hana_protocol.ConfirmedCommandAdmissionRefusal(
                    key, observation.slot, observation.fenced, observation.observed_at, cause,
                    CommandAdmissionRefusalCode.PRE_COMMAND_FAILED)
            return observation

        with (self.context() as context,
              mock.patch.object(hana_transport.SourceSender, 'retry_complete', return_value=(complete, lambda: None),
                  side_effect=hana_protocol.SourceRefused(cause) if case == 'sender refusal' else None) as retry,
              mock.patch.object(hana_transport.SourceSender, 'acknowledge') as acknowledge,
              mock.patch.object(objects, 'observe_execution', side_effect=observe_repair) as observed,
              mock.patch.object(objects, 'execution_result') as result,
              mock.patch.object(record, 'publish_hana_result') as publish,
              mock.patch.object(record, 'publish_hana_loss') as loss,
              mock.patch.object(time, 'sleep') as sleep):
            if case == 'publication refused':
                _ = context.enter_context(mock.patch.object(execution, 'publish_source_update', return_value=False))
            outcome = self.run_claimed()
            self.assertEqual(self.finish_claimed(outcome), 75)
        retry.assert_called_once_with(self.root, self.fixture.manifest,
            MissingBasis(self.fixture.manifest.repository, self.fixture.manifest.basis_commits), staging=mock.ANY)
        result.assert_not_called()
        publish.assert_not_called()
        loss.assert_called_once()
        if case == 'repaired':
            acknowledge.assert_called_once()
            self.assertIsInstance(outcome, offload.LostMacRun)
        else:
            acknowledge.assert_not_called()
            reason = 'The Hana full-payload repair failed: ' + cause if case == 'sender refusal' else cause
            if case == 'publication refused':
                reason = 'The Hana full-payload repair failed: The Hana source update was not published'
                observed.assert_called_once()
                sleep.assert_not_called()
            assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
            self.assertEqual(outcome.reason, reason)
            self.assertFalse(execution.claim_dispatch_pending(self.root, self.fixture.reference))
            self.assertTrue(execution.claim_admission_refused(self.root, self.fixture.reference))
            self.assertNotIn(hana_protocol.HanaFenceEnvelope, [type(envelope) for envelope in objects.envelopes])
        self.assertEqual(sum(isinstance(envelope, hana_protocol.HanaCommandEnvelope)
                             for envelope in objects.envelopes), 1)
        updates = sum(isinstance(envelope, hana_protocol.HanaSourceUpdateEnvelope) for envelope in objects.envelopes)
        self.assertEqual(updates, 1 if case in ('repaired', 'guest refusal') else 0)

    def test_failed_git_in_source_update_settles_the_attempt_as_refused(self) -> None:
        self.fixture.manifest = replace(self.fixture.manifest, basis_commits=('d' * 40,))
        self.fixture.request = replace(self.fixture.request, manifest=self.fixture.manifest)
        self.fixture.objects.request = self.fixture.request
        self.fixture.objects.missing_basis = True
        error = subprocess.CalledProcessError(-signal.SIGKILL, ['git', 'bundle', 'create'])
        with (self.context(), mock.patch.object(hana_transport.SourceSender, 'retry_complete', side_effect=error),
                mock.patch.object(record, 'publish_hana_loss') as loss):
            outcome = self.run_claimed()
            self.assertEqual(self.finish_claimed(outcome), 75)
        assert isinstance(outcome, offload.HanaCommandAdmissionRefused)
        self.assertIn('The Hana source update publication is unresolved', outcome.reason)
        self.assertIn('SIGKILL', outcome.reason)
        self.assertEqual(self.controller.attempt_evidence.cleanup, 'confirmed')
        loss.assert_called_once()


if __name__ == '__main__':
    _ = unittest.main()

"""Use scoped scaler calls and signed objects for the kept Hana lifecycle."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, cast

from scripts.aws_target import admin_secrets, hana_box as box, hana_guest, ledger, remote, s3_presign
from scripts.aws_target.aws_cli import AwsCli, AwsExecutor, AwsUnavailable, execute_command
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic

if TYPE_CHECKING:
    from scripts.aws_target.hana_execution import HanaExecutionAccess, HanaExecutionEnvelope, HanaCommandObservation
    from scripts.aws_target.hana_result import HanaAttemptKey

# Use this mapping for provider calls and policy coverage tests.
PROVIDER_OPERATIONS = {
    'describe-instances': 'ec2:DescribeInstances',
    'describe-spot-instance-requests': 'ec2:DescribeSpotInstanceRequests',
    'describe-instance-attribute': 'ec2:DescribeInstanceAttribute',
    'modify-instance-attribute': 'ec2:ModifyInstanceAttribute',
    'start-instances': 'ec2:StartInstances',
    'stop-instances': 'ec2:StopInstances',
    # Control messages and digest source bundles use these scoped operations.
    'put-object': 's3:PutObject',
    'get-object': 's3:GetObject',
}


BOOTSTRAP_OBJECTS: tuple[tuple[str, dict[str, object]], ...] = (
    ('/permit.json', {'version': 1}),
    ('/command.json', {'version': 1, 'action': 'idle'}),
    ('/fence.json', {'version': 1, 'action': 'idle'}),
)


class ProviderPending(RuntimeError):
    """The provider has no settled response for the saved operation."""


class AwsHanaProvider:
    def __init__(self, api: AwsCli, directory: Path, bucket: str, url_seconds: int,
                 *, clock: Callable[[], float] = time.time):
        self.api: AwsCli = api
        self.directory: Path = directory / 'hana' / 'provider'
        self.bucket: str = bucket
        self.url_seconds: int = url_seconds
        self.clock: Callable[[], float] = clock

    @classmethod
    def from_settings(cls, account_path: Path, directory: Path, workload_path: Path,
                      *, execute: AwsExecutor = execute_command) -> AwsHanaProvider:
        account, workload = conf.read(account_path), conf.read(workload_path)
        timeout, seconds = workload.whole('aws_timeout_s'), workload.whole('control_url_seconds')
        if not 0 < timeout < workload.whole('controller_tick_timeout_s') or not 0 < seconds <= 604800:
            raise ValueError('Hana provider time bounds are invalid')
        # Ignore inherited admin credentials and use the installed scaler profile.
        environment = {key: value for key, value in os.environ.items() if not key.startswith('AWS_')}
        environment.update(AWS_SHARED_CREDENTIALS_FILE=str(admin_secrets.credentials_file()),
            AWS_CONFIG_FILE=os.devnull, AWS_EC2_METADATA_DISABLED='true')

        def scaler(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                   check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            _ = env
            return execute(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=environment)

        path = directory / 'hana' / 'setup.json'
        saved = remote.read_record(path) if path.exists() else {}
        return cls(AwsCli(account.text('credentials_profile'), workload.text('region'), timeout, scaler), directory,
            str(saved.get('bucket', '')), seconds)

    def _call(self, operation: str, *arguments: str) -> dict[str, object]:
        action = PROVIDER_OPERATIONS[operation]
        try:
            return self.api.call(operation, *arguments, service='s3api' if action.startswith('s3:') else 'ec2')
        except (OSError, ValueError, AwsUnavailable, subprocess.SubprocessError) as error:
            message = 'Hana provider call failed: ' + action
            if isinstance(error, AwsUnavailable):
                code = re.match(
                    r'\s*(?:aws: \[ERROR\]: )?An error occurred '
                    + r'\(([A-Z][a-z][A-Za-z0-9]*(?:\.[A-Z][a-z][A-Za-z0-9]*)*)\)'
                    + r' when calling the [A-Z][A-Za-z0-9]* operation'
                    + r'(?: \(reached max retries: [0-9]+\))?(?::|\Z)', str(error))
                if code:
                    message += f' ({code[1]})'
            raise ProviderPending(message) from None

    def _object(self, key: str, document: dict[str, object] | None = None) -> dict[str, object]:
        if not self.bucket:
            raise ProviderPending('Hana setup has no control bucket')
        with tempfile.TemporaryDirectory(prefix='hana-object-', dir=self.directory) as folder:
            path = Path(folder) / 'object.json'
            if document is not None:
                _ = path.write_text(json.dumps(document))
                _ = self._call('put-object', '--bucket', self.bucket, '--key', key, '--body', str(path))
                return document
            _ = self._call('get-object', '--bucket', self.bucket, '--key', key, str(path))
            return remote.read_record(path)

    def _paths(self, operation: box.PendingOperation) -> tuple[Path, Path]:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', operation.token):
            raise ValueError('The Hana operation token is invalid')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.directory / (operation.token + '.json'), self.directory / 'mutation.lock'

    def _execution_prefix(self, key: HanaAttemptKey) -> str:
        import hashlib

        generation = key.generation
        if not all(re.fullmatch(r'[A-Za-z0-9_-]+', value) for value in (generation.deployment, generation.start_token)):
            raise ValueError('The Hana execution generation is invalid')
        digest = hashlib.sha256(key.claim_id.encode()).hexdigest()
        return 'control/' + generation.deployment + '/' + generation.start_token + '/executions/' + digest

    def execution_access(self, key: HanaAttemptKey) -> HanaExecutionAccess:
        """Grant bounded root publication access for this exact execution."""
        from scripts.aws_target.hana_execution import HanaExecutionAccess

        prefix = self._execution_prefix(key)
        credentials = self.api.call('export-credentials', '--format', 'process', service='configure')
        signing = s3_presign.SigningKey(ledger.text(credentials, 'AccessKeyId'), ledger.text(credentials, 'SecretAccessKey'),
                                     str(credentials.get('SessionToken', '')))
        now = self.clock()
        def signed(suffix: str) -> str:
            return s3_presign.presign('PUT', self.bucket + '.s3.' + self.api.region + '.amazonaws.com',
                prefix + suffix, self.api.region, signing, datetime.fromtimestamp(now, UTC), self.url_seconds)
        return HanaExecutionAccess(signed('/observation.json'), signed('/result.json'), now + self.url_seconds)

    def publish_execution(self, envelope: HanaExecutionEnvelope) -> None:
        """Publish one bounded command or an idempotent exact-attempt fence."""
        from scripts.aws_target.hana_execution import HanaCommandEnvelope, HanaFenceEnvelope, encode_envelope

        content = encode_envelope(envelope)
        key = envelope.request.key if isinstance(envelope, HanaCommandEnvelope) else envelope.key
        _ = self._execution_prefix(key)
        prefix = 'control/' + key.generation.deployment + '/' + key.generation.start_token
        suffix = '/fence.json' if isinstance(envelope, HanaFenceEnvelope) else '/command.json'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _ = self._object(prefix + suffix, ledger.mapping(cast(object, json.loads(content))))

    def _execution_bytes(self, key: HanaAttemptKey, suffix: str, maximum: int) -> bytes:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix='hana-execution-', dir=self.directory) as folder:
            path = Path(folder) / 'object.json'
            _ = self._call('get-object', '--bucket', self.bucket, '--key', self._execution_prefix(key) + suffix,
                           '--range', f'bytes=0-{maximum}', str(path))
            if path.stat().st_size > maximum:
                raise ProviderPending('The Hana execution object exceeds its protocol bound')
            return path.read_bytes()

    def observe_execution(self, key: HanaAttemptKey) -> HanaCommandObservation:
        """Read execution evidence without repeating command publication."""
        from scripts.aws_target.hana_execution import CONTROL_MAX_BYTES, decode_observation

        return decode_observation(self._execution_bytes(key, '/observation.json', CONTROL_MAX_BYTES))

    def execution_result(self, key: HanaAttemptKey) -> bytes:
        """Read the bounded result after the guest publishes its terminal observation."""
        from scripts.aws_target.hana_result import RESULT_MAX_BYTES

        return self._execution_bytes(key, '/result.json', RESULT_MAX_BYTES)

    def _prefix(self, identity: box.DeploymentIdentity, start: box.DemandStart) -> str:
        if not all(re.fullmatch(r'[A-Za-z0-9_-]+', name) for name in (identity.deployment, start.token)):
            raise ValueError('The Hana control identity is invalid')
        return 'control/' + identity.deployment + '/' + start.token

    def _report(self, identity: box.DeploymentIdentity, start: box.DemandStart | box.NoDemandStart) -> dict[str, object]:
        if isinstance(start, box.NoDemandStart):
            return {}
        return self._object(self._prefix(identity, start) + '/report.json')

    def _observation(self, identity: box.DeploymentIdentity, start: box.DemandStart | box.NoDemandStart) -> box.BoxObservation:
        reply = self._call('describe-instances', '--instance-ids', identity.instance)
        instances = [row for group in ledger.rows(reply, 'Reservations') for row in ledger.rows(group, 'Instances')]
        if len(instances) != 1 or instances[0].get('InstanceId') != identity.instance:
            raise ProviderPending('Hana instance identity differs')
        instance = instances[0]
        tags = {ledger.text(tag, 'Key'): ledger.text(tag, 'Value') for tag in ledger.rows(instance, 'Tags')}
        if tags.get('HanaDeployment') != identity.deployment or tags.get('Workload') != 'hana':
            raise ProviderPending('Hana instance ownership differs')
        state = ledger.text(ledger.mapping(instance['State']), 'Name')
        if state not in ('pending', 'running', 'stopping', 'stopped', 'shutting-down', 'terminated'):
            raise ProviderPending('Hana instance state is unknown')
        requests = ledger.rows(self._call('describe-spot-instance-requests', '--spot-instance-request-ids', identity.spot_request), 'SpotInstanceRequests')
        if (len(requests) != 1 or requests[0].get('InstanceId') != identity.instance
                or requests[0].get('SpotInstanceRequestId') != identity.spot_request
                or requests[0].get('Type') != 'persistent' or requests[0].get('InstanceInterruptionBehavior') != 'stop'):
            raise ProviderPending('Hana Spot request ownership or behavior differs')
        reason = ledger.text(ledger.mapping(requests[0]['Status']), 'Code')
        cause = box.StopCause.INTERRUPTION if reason in ('instance-stopped-by-price',
            'instance-stopped-no-capacity', 'instance-stopped-by-experiment') else box.StopCause.UNKNOWN
        boot: box.VerifiedBoot | box.BootUnverified = box.BootUnverified()
        if state == 'running' and isinstance(start, box.DemandStart):
            try:
                report = self._report(identity, start)
                if (report.get('deployment') == identity.deployment and report.get('start_token') == start.token
                        and report.get('boot_id') and ledger.instant(report, 'at') >= start.at):
                    boot = box.VerifiedBoot(ledger.text(report, 'boot_id'), start.token)
            except ProviderPending:
                pass
        return box.BoxObservation(identity, int(self.clock()), cast(ledger.InstanceState, state), boot, cause)

    def _bootstrap(self, identity: box.DeploymentIdentity, start: box.DemandStart) -> dict[str, object]:
        # URLs exist only in private runtime records and EC2 user data.
        credentials = self.api.call('export-credentials', '--format', 'process', service='configure')
        signing = s3_presign.SigningKey(ledger.text(credentials, 'AccessKeyId'), ledger.text(credentials, 'SecretAccessKey'),
            str(credentials.get('SessionToken', '')))
        prefix = self._prefix(identity, start)
        def url(method: str, suffix: str) -> str:
            return s3_presign.presign(method, self.bucket + '.s3.' + self.api.region + '.amazonaws.com',
                prefix + suffix, self.api.region, signing, datetime.fromtimestamp(self.clock(), UTC), self.url_seconds)
        return {'version': 1, 'deployment': identity.deployment, 'start_token': start.token, 'start_at': start.at,
            'expires': int(self.clock()) + self.url_seconds, 'control_url': url('GET', '/permit.json'),
            'report_url': url('PUT', '/report.json'), 'command_url': url('GET', '/command.json'),
            'fence_url': url('GET', '/fence.json')}

    def _write_bootstrap(self, identity: box.DeploymentIdentity, start: box.DemandStart,
                         path: Path, saved: dict[str, object]) -> None:
        prefix = self._prefix(identity, start)
        completed = saved.get('bootstrap_puts', 0)
        if type(completed) is not int or not 0 <= completed <= len(BOOTSTRAP_OBJECTS):
            raise ValueError('The saved Hana bootstrap progress is invalid')
        descriptor = ledger.mapping(saved['descriptor'])
        for index, (suffix, document) in enumerate(BOOTSTRAP_OBJECTS[completed:], start=completed + 1):
            _ = self._object(prefix + suffix, document)
            # Save each write so the next tick can continue after a timeout.
            saved['bootstrap_puts'] = index
            self._save_mutation(path, saved)
        encoded = base64.b64encode(json.dumps(descriptor, sort_keys=True).encode()).decode()
        _ = self._call('modify-instance-attribute', '--instance-id', identity.instance,
            '--attribute', 'userData', '--value', encoded)

    def _save_mutation(self, path: Path, saved: dict[str, object]) -> None:
        write_text_atomic(path, json.dumps(saved, sort_keys=True) + '\n', new_file_mode=0o600)

    def step(self, identity: box.DeploymentIdentity, start: box.DemandStart | box.NoDemandStart,
             operation: box.PendingOperation) -> box.ProviderReply:
        path, lock_path = self._paths(operation)
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if operation.action == box.Action.OBSERVE:
                    return self._observation(identity, start)
                saved: dict[str, object] = remote.read_record(path) if path.exists() else {}
                if saved.get('cancelled') is True:
                    raise ProviderPending('The Hana ' + operation.action.value + ' operation is cancelled')
                if saved.get('accepted') is True:
                    return self._accepted(identity, start, operation)
                if saved:
                    recovered = self._recover(identity, start, operation, path, saved)
                    if isinstance(recovered, box.MutationUnresolved):
                        if (operation.action != box.Action.BOOTSTRAP or recovered.observed.state != 'stopped'
                                or not isinstance(start, box.DemandStart)):
                            raise ProviderPending('The Hana ' + operation.action.value + ' operation remains unresolved')
                        self._write_bootstrap(identity, start, path, saved)
                    elif operation.action != box.Action.BOOTSTRAP:
                        return self._accepted(identity, start, operation)
                else:
                    saved = {'version': 1, 'token': operation.token, 'action': operation.action.value,
                        'deployment': identity.deployment, 'dispatched': True}
                    if operation.action == box.Action.BOOTSTRAP:
                        if not isinstance(start, box.DemandStart):
                            raise ValueError('Hana bootstrap needs a demand start')
                        saved['descriptor'] = self._bootstrap(identity, start)
                        saved['bootstrap_puts'] = 0
                    self._save_mutation(path, saved)
                    self._dispatch(identity, start, operation, path, saved)
                saved['accepted'] = True
                self._save_mutation(path, saved)
                return self._accepted(identity, start, operation)
            except (OSError, AwsUnavailable, subprocess.SubprocessError):
                raise ProviderPending('The Hana ' + operation.action.value + ' response is unavailable') from None
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _dispatch(self, identity: box.DeploymentIdentity, start: box.DemandStart | box.NoDemandStart,
                  operation: box.PendingOperation, path: Path, saved: dict[str, object]) -> None:
        if operation.action == box.Action.BOOTSTRAP:
            assert isinstance(start, box.DemandStart)
            self._write_bootstrap(identity, start, path, saved)
        elif operation.action == box.Action.START:
            _ = self._call('start-instances', '--instance-ids', identity.instance)
        elif operation.action == box.Action.STOP:
            _ = self._call('stop-instances', '--instance-ids', identity.instance)
        elif operation.action == box.Action.PUBLISH:
            if not isinstance(start, box.DemandStart) or not isinstance(operation.permit, hana_guest.WorkPermit):
                raise ValueError('Hana publication needs a boot permit')
            _ = self._object(self._prefix(identity, start) + '/permit.json', {
                'token': operation.token, 'deployment': identity.deployment, 'start_token': start.token,
                'permit': asdict(operation.permit), 'expected_generation': operation.expected_generation,
                'execution': operation.execution})

    def _accepted(self, identity: box.DeploymentIdentity, start: box.DemandStart | box.NoDemandStart,
                  operation: box.PendingOperation) -> box.ProviderReply:
        if operation.action != box.Action.PUBLISH:
            return box.OperationAccepted(operation.token)
        report = self._report(identity, start)
        permit = operation.permit
        if (not isinstance(start, box.DemandStart) or not isinstance(permit, hana_guest.WorkPermit)
                or report.get('deployment') != identity.deployment or report.get('start_token') != start.token
                or report.get('token') != operation.token or report.get('boot_id') != permit.boot_id
                or report.get('generation') != operation.generation or report.get('execution') != operation.execution):
            raise ProviderPending('Hana permit acknowledgement is pending')
        return box.PermitAccepted(operation.token, identity, box.VerifiedBoot(permit.boot_id, start.token),
            operation.generation, operation.execution)

    def recover(self, identity: box.DeploymentIdentity, start: box.DemandStart | box.NoDemandStart,
                operation: box.PendingOperation, *, abandon_start: bool = False) -> box.MutationRecovery:
        if abandon_start and operation.action not in (box.Action.BOOTSTRAP, box.Action.START):
            raise ValueError('The Hana operation cannot be cancelled as a start')
        path, lock_path = self._paths(operation)
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return box.MutationUnresolved(operation.token, self._observation(identity, start))
            try:
                saved = remote.read_record(path) if path.exists() else {}
                if abandon_start:
                    read_started_at = self.clock()
                    observed = self._observation(identity, start)
                    if saved.get('cancelled') is not True:
                        saved = {**saved, 'version': 1, 'token': operation.token,
                            'cancelled': True, 'cancelled_at': float(self.clock())}
                        self._save_mutation(path, saved)
                    if operation.action == box.Action.START and observed.state == 'stopped':
                        if saved.get('accepted') is True:
                            return box.StartMayBeRunning(operation.token, observed)
                        if saved.get('dispatched') is True:
                            return self._recover_fenced_start(path, operation, saved, observed, read_started_at)
                    return box.MutationSettled(operation.token, observed)
                return self._recover(identity, start, operation, path, saved)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _recover_fenced_start(self, path: Path, operation: box.PendingOperation, saved: dict[str, object],
                              observed: box.BoxObservation, read_started_at: float) -> box.MutationRecovery:
        fenced_at = saved.get('cancelled_at')
        # Start a new wait if the saved time lacks full precision.
        if type(fenced_at) is not float:
            self._save_mutation(path, {**saved, 'cancelled_at': float(self.clock())})
        elif read_started_at >= fenced_at + max(1, self.api.timeout_s):
            return box.MutationSettled(operation.token, observed)
        return box.MutationUnresolved(operation.token, observed)

    def _recover(self, identity: box.DeploymentIdentity, start: box.DemandStart | box.NoDemandStart,
                 operation: box.PendingOperation, path: Path, saved: dict[str, object]) -> box.MutationRecovery:
        if not saved:
            self._save_mutation(path, {'version': 1, 'token': operation.token, 'cancelled': True})
        read_started_at = self.clock()
        observed = self._observation(identity, box.NoDemandStart() if operation.action == box.Action.BOOTSTRAP else start)
        if (operation.action == box.Action.START and observed.state == 'stopped'
                and saved.get('dispatched') is True and saved.get('accepted') is not True
                and saved.get('cancelled') is True):
            return self._recover_fenced_start(path, operation, saved, observed, read_started_at)
        settled = not saved or saved.get('accepted') is True or saved.get('cancelled') is True
        if saved and saved.get('accepted') is not True and saved.get('cancelled') is not True:
            if operation.action == box.Action.START:
                settled = observed.state in ('pending', 'running', 'stopping')
            elif operation.action == box.Action.STOP:
                settled = observed.state in ('stopped', 'stopping', 'terminated')
            elif (operation.action == box.Action.BOOTSTRAP
                    and saved.get('bootstrap_puts', len(BOOTSTRAP_OBJECTS)) == len(BOOTSTRAP_OBJECTS)):
                actual = self._call('describe-instance-attribute', '--instance-id', identity.instance, '--attribute', 'userData')
                encoded = ledger.mapping(actual.get('UserData', {})).get('Value', '')
                if not isinstance(encoded, str):
                    raise ProviderPending('The Hana bootstrap user data is unreadable')
                expected = base64.b64encode(json.dumps(saved['descriptor'], sort_keys=True).encode()).decode()
                settled = encoded == expected
            elif operation.action == box.Action.PUBLISH:
                settled = isinstance(operation.permit, hana_guest.WorkPermit) and self.clock() >= operation.permit.expires_at
        if operation.action == box.Action.PUBLISH and isinstance(operation.permit, hana_guest.WorkPermit):
            # An accepted object can still grant permission until its deadline.
            settled = self.clock() >= operation.permit.expires_at
        return box.MutationSettled(operation.token, observed) if settled else box.MutationUnresolved(operation.token, observed)

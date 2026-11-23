"""Run AWS CLI calls with one bound and one failure type."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
import re
import subprocess
from typing import Protocol, cast, final, get_args

from scripts.aws_target import ledger


class AwsExecutor(Protocol):
    def __call__(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                 timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]: ...


def execute_command(arguments: Sequence[str], *, capture_output: bool, text: bool,
                    timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    if arguments and arguments[0] == 'aws':
        arguments = [*arguments, '--cli-error-format', 'legacy']
    return subprocess.run(arguments, capture_output=capture_output, text=text, timeout=timeout, check=check, env=env)


class AwsUnavailable(RuntimeError):
    pass


# AWS gives these codes when it refuses the key itself, not one action.
KEY_REFUSED_CODES = frozenset(('AuthFailure', 'InvalidClientTokenId', 'UnrecognizedClientException',
                               'SignatureDoesNotMatch', 'ExpiredToken', 'ExpiredTokenException',
                               'InvalidAccessKeyId'))


def aws_error_code(error: AwsUnavailable, *, first_attempt_of: str = '', allow_status: bool = False,
                   operation: str = '') -> str:
    """Read the AWS error code from the CLI header.

    A first attempt requires a named service code, a matching operation, and zero retries.
    """
    code_pattern = r'[A-Z][a-z][A-Za-z0-9]*(?:\.[A-Z][a-z][A-Za-z0-9]*)*'
    if allow_status and not first_attempt_of:
        code_pattern += r'|[1-5][0-9]{2}'
    match = re.match(
        r'\s*(?:aws: \[ERROR\]: )?An error occurred '
        + rf'\((?P<code>{code_pattern})\)'
        + r' when calling the (?P<operation>[A-Z][A-Za-z0-9]*) operation'
        + r'(?: \(reached max retries: (?P<retries>[0-9]+)\))?(?::|\Z)', str(error))
    if not match or (operation and match['operation'] != operation):
        return ''
    if first_attempt_of and (match['operation'] != first_attempt_of or match['retries'] not in (None, '0')):
        return ''
    return match['code']


@dataclass(frozen=True)
class InstanceEvidence:
    instance_id: str
    generation: str
    state: ledger.InstanceState
    observed_at: int


@final
class AwsCli:
    def __init__(self, profile: str, region: str, timeout_s: float,
                 execute: AwsExecutor = execute_command) -> None:
        self.profile = profile
        self.region = region
        self.timeout_s = timeout_s
        self.execute = execute

    def in_region(self, region: str) -> AwsCli:
        """Keep the executor and credentials when selecting a region."""
        return self if region == self.region else AwsCli(self.profile, region, self.timeout_s, self.execute)

    def _run(self, service: str, operation: str, arguments: Sequence[str], timeout_s: float) -> str:
        result = self.execute(['aws', '--profile', self.profile, '--region', self.region,
                               service, operation, *arguments, '--output', 'json'], capture_output=True, text=True,
                              timeout=timeout_s, check=False)
        if result.returncode:
            raise AwsUnavailable(result.stderr.strip() or f'{operation}: failed')
        return result.stdout

    def call(self, operation: str, *arguments: str, service: str = 'ec2',
             timeout_s: float | None = None) -> dict[str, object]:
        stdout = self._run(service, operation, arguments, self.timeout_s if timeout_s is None else timeout_s)
        if not stdout.strip() or service == 'cloudformation' and operation == 'deploy':
            return {}
        raw = cast(object, json.loads(stdout))
        if not isinstance(raw, dict):
            raise AwsUnavailable(f'{operation}: invalid reply')
        return cast(dict[str, object], raw)

    def ec2_dry_run(self, operation: str, *arguments: str, timeout_s: float | None = None) -> None:
        """Require AWS to confirm permission with DryRunOperation."""
        try:
            _ = self._run('ec2', operation, (*arguments, '--dry-run'), self.timeout_s if timeout_s is None else timeout_s)
        except AwsUnavailable as error:
            if aws_error_code(error) == 'DryRunOperation':
                return
            raise
        raise AwsUnavailable(f'{operation}: expected a dry-run response')

    def observe(self, instance_id: str, now: int) -> InstanceEvidence:
        row = self.call('describe-instances', '--instance-ids', instance_id)
        try:
            reservations = cast(list[dict[str, object]], row['Reservations'])
            instances = [instance for reservation in reservations
                         for instance in cast(list[dict[str, object]], reservation['Instances'])]
            if len(instances) != 1 or instances[0].get('InstanceId') != instance_id:
                raise AwsUnavailable('instance identity does not match')
            instance = instances[0]
            state = cast(dict[str, object], instance['State']).get('Name')
            generation = instance.get('LaunchTime')
            if state not in get_args(ledger.InstanceState) or state == 'unknown' or not isinstance(generation, str) or not generation:
                raise AwsUnavailable('instance lifecycle evidence is incomplete')
            return InstanceEvidence(instance_id, f'{instance_id}:{generation}', cast(ledger.InstanceState, state), now)
        except (KeyError, TypeError, AttributeError, IndexError) as error:
            raise AwsUnavailable('instance lifecycle reply is malformed') from error

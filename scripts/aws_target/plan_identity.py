"""Create the IAM identity that can check AWS deployment plans."""

from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import cast, final
from urllib.parse import unquote

from scripts.aws_target import admin_secrets, ledger
from scripts.aws_target.aws_cli import AwsUnavailable, aws_error_code
from scripts.aws_target.config import gate_regions
from scripts.aws_target.controller_lock import control_lock
from scripts.lib import conf
from scripts.lib.atomic_file import prepared_replacement, sync_folder


NAME = 'aws-build-plan'
IAM_PATH = '/aws-build/'
OWNER_TAG = {'Key': 'ManagedBy', 'Value': 'aws-build-plan-identity'}
CHANGE_SET_PREFIX = 'aws-build-plan-'
CREATE_COMMAND = 'deploy.py plan-identity --approve-resources'
DOWN_COMMAND = 'deploy.py down --workload plan-identity --approve-destroy'

READ_ACTIONS = (
    'budgets:ViewBudget',
    'ce:GetCostAndUsage',
    'cloudformation:ListStacks',
    'ec2:DescribeAvailabilityZones',
    'ec2:DescribeImages',
    'ec2:DescribeInstanceTypeOfferings',
    'ec2:DescribeInstances',
    'ec2:DescribeRouteTables',
    'ec2:DescribeSpotPriceHistory',
    'ec2:DescribeSubnets',
    'ec2:DescribeVolumes',
    'ec2:DescribeVpcs',
    'servicequotas:GetServiceQuota',
    'servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota',
    'sts:GetCallerIdentity',
)
STACK_READS = (
    'cloudformation:DescribeChangeSet',
    'cloudformation:DescribeStackEvents',
    'cloudformation:DescribeStackResource',
    'cloudformation:DescribeStackResources',
    'cloudformation:DescribeStacks',
    'cloudformation:GetTemplate',
)
IAM_READS = ('iam:GetUser', 'iam:SimulatePrincipalPolicy')
BUCKET_READS = ('s3:ListBucket',)
CHANGE_SET_ACTIONS = ('cloudformation:CreateChangeSet', 'cloudformation:DeleteChangeSet')
DENIAL_PROBES = ('cloudformation:ExecuteChangeSet', 'cloudformation:DeleteStack',
                 'iam:PassRole', 'ec2:RunInstances', 'ec2:DescribeInstanceAttribute',
                 'ec2:DescribeSpotInstanceRequests', 's3:PutObject')
PROOF_RETRY_DELAYS = (0, 1, 2, 4, 8)
PROPAGATION_ERRORS = frozenset((
    'AccessDenied', 'AccessDeniedException', 'InvalidClientTokenId', 'UnauthorizedOperation',
    'UnrecognizedClientException',
))


class PlanIdentityRefused(ValueError):
    """The plan identity operation cannot continue."""


class PlanAgentCold(PlanIdentityRefused):
    """The GPG agent cannot decrypt without a passphrase."""


@final
class PlanAwsRefused(PlanIdentityRefused):
    def __init__(self, message: str, code: str = '') -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, repr=False)
class PlanAccount:
    partition: str
    account: str

    @classmethod
    def from_identity(cls, reply: dict[str, object]) -> PlanAccount:
        arn = ledger.text(reply, 'Arn')
        account = ledger.text(reply, 'Account')
        match = re.fullmatch(r'arn:(aws(?:-[a-z0-9-]+)?):(?:iam|sts)::([0-9]{12}):[^\s]+', arn)
        if match is None or match[2] != account:
            raise PlanIdentityRefused('AWS returned an invalid account identity.')
        return cls(match[1], account)

    def iam(self, name: str) -> str:
        return f'arn:{self.partition}:iam::{self.account}:{name}'

    @property
    def user_arn(self) -> str:
        return self.iam(f'user{IAM_PATH}{NAME}')

    @property
    def policy_arn(self) -> str:
        return self.iam(f'policy{IAM_PATH}{NAME}')


@dataclass(frozen=True, order=True)
class PlanStack:
    region: str
    name: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r'[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+', self.region):
            raise PlanIdentityRefused('The Amazon region for a plan stack is invalid.')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,127}', self.name):
            raise PlanIdentityRefused('A plan stack name is invalid.')


def _configured_stacks(configuration: conf.ConfigRead, home: str) -> tuple[PlanStack, ...]:
    gates = gate_regions(home)
    hana = conf.read_or_repository('hana_aws')
    name = configuration.text('stack_name')
    return tuple(sorted({
        PlanStack(home, name),
        *(PlanStack(region, f'{name}-gates-{region}') for region in gates[1:]),
        PlanStack(hana.text('region'), hana.text('stack_name')),
    }))


def policy_document(owner: PlanAccount, stacks: tuple[PlanStack, ...]) -> dict[str, object]:
    """Grant the plan calls and deny all other actions."""
    stack_arns = [f'arn:{owner.partition}:cloudformation:{stack.region}:{owner.account}:stack/{stack.name}/*'
                  for stack in stacks]
    change_set_name = {'cloudformation:ChangeSetName': CHANGE_SET_PREFIX + '*'}
    return {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'ReadSetupInputs', 'Effect': 'Allow', 'Action': list(READ_ACTIONS), 'Resource': '*'},
        {'Sid': 'ReadWorkloadStacks', 'Effect': 'Allow', 'Action': list(STACK_READS), 'Resource': stack_arns},
        {'Sid': 'ReadAndSimulateUsers', 'Effect': 'Allow', 'Action': list(IAM_READS),
         'Resource': owner.iam('user/*')},
        {'Sid': 'ReadBucketMetadata', 'Effect': 'Allow', 'Action': list(BUCKET_READS),
         'Resource': f'arn:{owner.partition}:s3:::*',
         'Condition': {'StringEquals': {'s3:ResourceAccount': owner.account}}},
        {'Sid': 'ManagePlanChangeSets', 'Effect': 'Allow', 'Action': list(CHANGE_SET_ACTIONS),
         'Resource': stack_arns,
         'Condition': {'StringLike': change_set_name,
                       'Null': {'cloudformation:RoleArn': 'true', 'cloudformation:ImportResourceTypes': 'true',
                                'cloudformation:TemplateUrl': 'true'}}},
        {'Sid': 'DenyOtherActions', 'Effect': 'Deny', 'Resource': '*',
         'NotAction': [*READ_ACTIONS, *STACK_READS, *IAM_READS, *BUCKET_READS, *CHANGE_SET_ACTIONS]},
    ]}


def credential_path() -> Path:
    return Path.home() / '.config/aws-build/plan.gpg'


@dataclass(frozen=True, repr=False)
class PlanCredentials:
    access_key_id: str
    secret_access_key: str
    owner: PlanAccount

    def environment(self) -> dict[str, str]:
        return _aws_environment(os.environ, self.access_key_id, self.secret_access_key)

    def encode(self) -> bytes:
        return json.dumps({'version': 1, 'AccessKeyId': self.access_key_id, 'SecretAccessKey': self.secret_access_key,
                           'Account': self.owner.account, 'Arn': self.owner.user_arn}).encode()

    @classmethod
    def decode(cls, content: bytes) -> PlanCredentials:
        try:
            row = ledger.mapping(cast(object, json.loads(content)))
            owner = PlanAccount.from_identity(row)
            if row.get('version') != 1 or row.get('Arn') != owner.user_arn:
                raise ValueError
            return cls(ledger.text(row, 'AccessKeyId'), ledger.text(row, 'SecretAccessKey'), owner)
        except ValueError:
            raise PlanIdentityRefused('The encrypted AWS plan credential has invalid fields.') from None


def _aws_environment(environment: Mapping[str, str], key: str, secret: str) -> dict[str, str]:
    clean = {name: value for name, value in environment.items() if not name.startswith('AWS_')}
    clean.update(AWS_ACCESS_KEY_ID=key, AWS_SECRET_ACCESS_KEY=secret, AWS_EC2_METADATA_DISABLED='true',
                 AWS_CONFIG_FILE='/dev/null', AWS_SHARED_CREDENTIALS_FILE='/dev/null', AWS_MAX_ATTEMPTS='1',
                 AWS_CLI_AUTO_PROMPT='off', AWS_IGNORE_CONFIGURED_ENDPOINT_URLS='true', AWS_PAGER='')
    return clean


def _gpg(*arguments: str, content: bytes = b'', interactive: bool = False) -> subprocess.CompletedProcess[bytes]:
    environment = dict(os.environ)
    mode = ['--batch', '--pinentry-mode', 'error']
    if interactive:
        mode = ['--pinentry-mode', 'ask']
        descriptor = admin_secrets.terminal()
        try:
            environment['GPG_TTY'] = os.ttyname(descriptor)
        finally:
            os.close(descriptor)
    try:
        return subprocess.run(
            ['gpg', '--no-options', '--no-tty', '--quiet', *mode, *arguments],
            input=content, capture_output=True, timeout=120 if interactive else 10, check=False, env=environment)
    except (OSError, subprocess.SubprocessError):
        raise PlanIdentityRefused('GPG could not complete the AWS plan credential operation.') from None


def _decrypt(content: bytes, *, interactive: bool = False) -> bytes:
    result = _gpg('--status-fd', '2', '--decrypt', content=content, interactive=interactive)
    if result.returncode:
        errors = (match[1] for match in re.finditer(
            rb'^\[GNUPG:\] ERROR pkdecrypt_failed ([0-9]+)$', result.stderr, re.M))
        if not interactive and any(int(code) & 0xffff == 85 for code in errors):  # GPG_ERR_NO_PIN_ENTRY
            raise PlanAgentCold('The AWS plan key is locked. Run github-warmup in a terminal on natedev.')
        raise PlanIdentityRefused('GPG could not decrypt the AWS plan credential.')
    return result.stdout


def _encryption_recipient() -> str:
    token = Path.home() / '.config/gh/token.gpg'
    result = _gpg('--list-only', '--status-fd', '1', '--decrypt', str(token))
    keys = [match[1] for match in re.finditer(rb'^\[GNUPG:\] ENC_TO ([0-9A-F]{16}) ', result.stdout, re.M)]
    if result.returncode or len(keys) != 1 or keys[0] == b'0000000000000000':
        raise PlanIdentityRefused('GPG could not identify the one recipient of the encrypted GitHub token.')
    return keys[0].decode('ascii') + '!'


def _encrypt(recipient: str, content: bytes) -> bytes:
    result = _gpg('--trust-model', 'always', '--no-encrypt-to', '--recipient', recipient,
                  '--output', '-', '--encrypt', content=content)
    if result.returncode or not result.stdout:
        raise PlanIdentityRefused('GPG could not encrypt the AWS plan credential.')
    return result.stdout


def _read_encrypted(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise OSError
            content = stream.read(65537)
            if not content or len(content) > 65536:
                raise OSError
            return content
    except FileNotFoundError:
        raise PlanIdentityRefused(f'The AWS plan key is missing. On natedev, run {CREATE_COMMAND}.') from None
    except OSError:
        raise PlanIdentityRefused('Require a private mode 0600 file at ~/.config/aws-build/plan.gpg.') from None


def _stored_credentials(path: Path) -> PlanCredentials:
    return PlanCredentials.decode(_decrypt(_read_encrypted(path)))


def plan_credentials() -> PlanCredentials:
    """Read the plan key through the warm GPG agent."""
    return _stored_credentials(credential_path())


@dataclass(frozen=True)
class PlanKeyToCreate:
    recipient: str


def _key_for_setup(path: Path) -> PlanCredentials | PlanKeyToCreate:
    try:
        try:
            _ = path.lstat()
        except FileNotFoundError:
            pass
        else:
            return _stored_credentials(path)
    except (OSError, PlanIdentityRefused):
        raise PlanIdentityRefused(
            'Cannot read the AWS plan key at ~/.config/aws-build/plan.gpg. '
            + 'Run github-warmup in a terminal on natedev. '
            + 'If the file is damaged, delete it and run the command again.') from None

    with _step('check encryption to the GitHub token key'):
        recipient = _encryption_recipient()
        sample = b'AWS plan encryption check'
        if _decrypt(_encrypt(recipient, sample)) != sample:
            raise PlanIdentityRefused('GPG did not confirm the AWS plan encryption key.')
    return PlanKeyToCreate(recipient)


@final
class IdentityApi:
    """Pass JSON request fields as an argument and keep credentials in the environment."""

    def __init__(self, region: str, timeout: float, environment: dict[str, str]) -> None:
        self.region = region
        self.timeout = timeout
        self.environment = environment

    def call(self, service: str, operation: str, **parameters: object) -> dict[str, object]:
        try:
            result = subprocess.run(
                ['aws', '--region', self.region, service, operation, '--output', 'json', '--cli-error-format', 'legacy',
                 '--cli-input-json', json.dumps(parameters)],
                capture_output=True, timeout=self.timeout, check=False, env=self.environment)
        except OSError:
            raise PlanAwsRefused(f'The aws command did not start for {operation}.') from None
        except subprocess.TimeoutExpired:
            raise PlanAwsRefused(f'The aws command timed out for {operation}.') from None
        if result.returncode == 252:
            raise PlanAwsRefused(f'The aws command refused its arguments for {operation}.')
        if result.returncode:
            code = aws_error_code(AwsUnavailable(result.stderr.decode(errors='replace')))
            detail = f' ({code})' if code else ''
            raise PlanAwsRefused(f'AWS did not confirm {operation}.{detail}', code)
        try:
            return ledger.mapping(cast(object, json.loads(result.stdout))) if result.stdout.strip() else {}
        except ValueError:
            raise PlanIdentityRefused(f'AWS returned an invalid reply for {operation}.') from None


@contextmanager
def _step(description: str) -> Generator[None]:
    print('plan-identity: ' + description, flush=True)
    try:
        yield
    except (OSError, ValueError, subprocess.SubprocessError):
        print('plan-identity: failed', flush=True)
        raise
    print('plan-identity: passed', flush=True)


def _lookup(api: IdentityApi, kind: str, arn: str, **parameters: str) -> dict[str, object]:
    try:
        reply = api.call('iam', 'get-' + kind.lower(), **parameters)
    except PlanAwsRefused as error:
        if error.code == 'NoSuchEntity':
            return {}
        raise
    record = ledger.mapping(reply.get(kind))
    tags = {'Tags': record.get('Tags', [])}
    if record.get('Arn') != arn or OWNER_TAG not in ledger.rows(tags, 'Tags'):
        raise PlanIdentityRefused(f'The {kind.lower()} name already belongs to another IAM resource.')
    return record


def _access_key_ids(api: IdentityApi) -> tuple[str, ...]:
    reply = api.call('iam', 'list-access-keys', UserName=NAME)
    return tuple(ledger.text(row, 'AccessKeyId') for row in ledger.rows(reply, 'AccessKeyMetadata'))


def _remove_iam(api: IdentityApi, operation: str, **parameters: str) -> None:
    try:
        _ = api.call('iam', operation, **parameters)
    except PlanAwsRefused as error:
        if error.code != 'NoSuchEntity':
            raise


def _retry_proof[Result](read: Callable[[], Result]) -> Result:
    for delay in PROOF_RETRY_DELAYS:
        if delay:
            time.sleep(delay)
        try:
            return read()
        except PlanAwsRefused as error:
            if error.code not in PROPAGATION_ERRORS or delay == PROOF_RETRY_DELAYS[-1]:
                raise
    raise AssertionError('Proof requires at least one attempt.')


def _require_missing_plan_key(path: Path) -> None:
    try:
        _ = path.lstat()
    except FileNotFoundError:
        return
    raise PlanIdentityRefused('The local plan key appeared during setup. Run the command again.')


def _revoke_setup_key(api: IdentityApi, path: Path, key: str) -> None:
    _require_missing_plan_key(path)
    _remove_iam(api, 'delete-access-key', UserName=NAME, AccessKeyId=key)


def _revoke_failed_creation(api: IdentityApi, path: Path, known_key: str) -> bool:
    """Return whether cleanup found a key to revoke."""
    # The new key can be absent from the list during IAM propagation.
    keys = {known_key} if known_key else set[str]()
    try:
        for delay in PROOF_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            keys.update(_retry_proof(lambda: _access_key_ids(api)))
            if keys:
                break
    finally:
        for key in sorted(keys):
            _revoke_setup_key(api, path, key)
    return bool(keys)


def _prove(api: IdentityApi, owner: PlanAccount) -> None:
    with _step('check the stored key with STS'):
        reply = _retry_proof(lambda: api.call('sts', 'get-caller-identity'))
        if reply.get('Arn') != owner.user_arn or reply.get('Account') != owner.account:
            raise PlanIdentityRefused('STS did not confirm the AWS plan user.')

    with _step('read availability zones with the stored key'):
        reply = _retry_proof(lambda: api.call('ec2', 'describe-availability-zones'))
        if not ledger.rows(reply, 'AvailabilityZones'):
            raise PlanIdentityRefused('AWS did not return any availability zones.')

    with _step('simulate denied actions with the stored key'):
        reply = _retry_proof(lambda: api.call('iam', 'simulate-principal-policy',
                                            PolicySourceArn=owner.user_arn, ActionNames=DENIAL_PROBES))
        results = ledger.rows(reply, 'EvaluationResults')
        decisions = {ledger.text(row, 'EvalActionName'): row.get('EvalDecision') for row in results}
        complete = not reply.get('IsTruncated') and len(results) == len(DENIAL_PROBES)
        for action in DENIAL_PROBES:
            passed = complete and decisions.get(action) == 'explicitDeny'
            print(f'plan-identity: {action} explicit deny {"passed" if passed else "failed"}', flush=True)
        if not complete or decisions != dict.fromkeys(DENIAL_PROBES, 'explicitDeny'):
            raise PlanIdentityRefused('The AWS plan policy did not prove every required explicit denial.')


def _admin_api(settings: Path, region: str, timeout: float) -> IdentityApi:
    environment = admin_secrets.AdminSecretSession(settings).environment()
    return IdentityApi(region, timeout, _aws_environment(
        environment, environment['AWS_ACCESS_KEY_ID'], environment['AWS_SECRET_ACCESS_KEY']))


def _create_key(api: IdentityApi, owner: PlanAccount, recipient: str, path: Path) -> None:
    key = ''
    published = False
    try:
        with _step('create one access key'):
            row = ledger.mapping(api.call('iam', 'create-access-key', UserName=NAME).get('AccessKey'))
            key = ledger.text(row, 'AccessKeyId')
        with _step('encrypt the new access key'):
            credential = PlanCredentials(key, ledger.text(row, 'SecretAccessKey'), owner)
            ciphertext = _encrypt(recipient, credential.encode())
        with prepared_replacement(path, ciphertext, mode=0o600) as temporary:
            with _step('decrypt the stored key through GPG'):
                stored = _stored_credentials(temporary)
            _prove(IdentityApi(api.region, api.timeout, stored.environment()), owner)
            with _step('save the verified key at ~/.config/aws-build/plan.gpg'):
                _require_missing_plan_key(path)
                prepared_file = temporary.stat()
                try:
                    # This rename commits setup. Setup calls DeleteAccessKey only while plan.gpg is absent.
                    # The approved down --workload plan-identity --approve-destroy command is the exception.
                    os.replace(temporary, path)
                    published = True
                except (OSError, KeyboardInterrupt):
                    try:
                        published = os.path.samestat(prepared_file, path.lstat())
                    except FileNotFoundError:
                        pass
                    raise
        sync_folder(path.parent)
    except (Exception, KeyboardInterrupt):
        if published:
            print('plan-identity: warning: the key is published. Setup could not confirm local cleanup or directory sync.',
                  file=sys.stderr, flush=True)
            return
        with _step('revoke all keys from the failed setup'):
            try:
                found_key = _revoke_failed_creation(api, path, key)
            except (OSError, ValueError):
                raise PlanIdentityRefused(
                    f'AWS could not confirm key cleanup. Run {CREATE_COMMAND} again on natedev.') from None
            if not found_key:
                raise PlanIdentityRefused(
                    f'An IAM key can still appear. Run {CREATE_COMMAND} again on natedev to remove it.') from None
        raise


def _remove_old_policy_versions(api: IdentityApi, owner: PlanAccount) -> None:
    reply = api.call('iam', 'list-policy-versions', PolicyArn=owner.policy_arn)
    for version in ledger.rows(reply, 'Versions'):
        if version.get('IsDefaultVersion') is False:
            _remove_iam(api, 'delete-policy-version', PolicyArn=owner.policy_arn,
                        VersionId=ledger.text(version, 'VersionId'))


def _configure_policy(api: IdentityApi, owner: PlanAccount, stacks: tuple[PlanStack, ...],
                      policy: dict[str, object]) -> None:
    document = policy_document(owner, stacks)
    if not policy:
        with _step('create the managed plan policy'):
            _ = api.call('iam', 'create-policy', PolicyName=NAME, Path=IAM_PATH, PolicyDocument=json.dumps(document),
                         Tags=[OWNER_TAG])
        return

    with _step('check the default plan policy version'):
        reply = api.call('iam', 'get-policy-version', PolicyArn=owner.policy_arn,
                         VersionId=ledger.text(policy, 'DefaultVersionId'))
        saved = ledger.mapping(reply.get('PolicyVersion')).get('Document')
        if isinstance(saved, str):
            try:
                saved = cast(object, json.loads(unquote(saved)))
            except ValueError:
                raise PlanIdentityRefused('AWS returned an invalid plan policy.') from None
    if saved == document:
        return
    print(f'plan-identity: the policy differs from the current settings. {CREATE_COMMAND} updates it.', flush=True)
    with _step('set a new default plan policy version'):
        _remove_old_policy_versions(api, owner)
        _ = api.call('iam', 'create-policy-version', PolicyArn=owner.policy_arn,
                     PolicyDocument=json.dumps(document), SetAsDefault=True)


def _setup(api: IdentityApi, owner: PlanAccount, stacks: tuple[PlanStack, ...],
           local_key: PlanCredentials | PlanKeyToCreate, path: Path) -> None:
    with _step('check for existing IAM resources'):
        policy = _lookup(api, 'Policy', owner.policy_arn, PolicyArn=owner.policy_arn)
        user = _lookup(api, 'User', owner.user_arn, UserName=NAME)
        keys = _access_key_ids(api) if user else ()
    if isinstance(local_key, PlanCredentials):
        if keys != (local_key.access_key_id,):
            raise PlanIdentityRefused(f'The local key differs from the sole IAM key. Run {DOWN_COMMAND} before setup.')
        if not policy:
            raise PlanIdentityRefused(f'The managed plan policy is missing. Run {DOWN_COMMAND} before setup.')
        _configure_policy(api, owner, stacks, policy)
        _prove(IdentityApi(api.region, api.timeout, local_key.environment()), owner)
        return

    if keys:
        with _step('revoke IAM keys because the local file is missing'):
            for key in keys:
                _revoke_setup_key(api, path, key)
    _configure_policy(api, owner, stacks, policy)
    if not user:
        with _step('create the plan user'):
            _ = api.call('iam', 'create-user', UserName=NAME, Path=IAM_PATH, Tags=[OWNER_TAG])
    with _step('attach the managed policy to the plan user'):
        _ = api.call('iam', 'attach-user-policy', UserName=NAME, PolicyArn=owner.policy_arn)
    _create_key(api, owner, local_key.recipient, path)


def command_create(settings: Path, *, approve_resources: bool) -> None:
    if not approve_resources:
        print('Plan: create an IAM user, attach one managed policy, and save one encrypted access key.')
        print('The key can read setup inputs, applied service quotas, and quota request history.')
        print('The key can create and delete plan change sets.')
        print('The policy denies change set execution.')
        print(f'On natedev, run {CREATE_COMMAND}.')
        return

    configuration = conf.read(settings)
    region, timeout = configuration.text('region'), configuration.number('aws_timeout_s')
    stacks = _configured_stacks(configuration, region)
    path = credential_path()
    ready = False
    try:
        with control_lock(path.parent):
            for stack in stacks:
                print(f'plan-identity: allow stack {stack.name} in {stack.region}', flush=True)
            print('plan-identity: allow reads of applied service quotas and quota request history', flush=True)
            local_key = _key_for_setup(path)
            with _step('confirm the administrator account'):
                api = _admin_api(settings, region, timeout)
                owner = PlanAccount.from_identity(api.call('sts', 'get-caller-identity'))
                if isinstance(local_key, PlanCredentials) and local_key.owner != owner:
                    raise PlanIdentityRefused('The local plan key belongs to a different AWS account.')
            _setup(api, owner, stacks, local_key, path)
            ready = True
    except (OSError, KeyboardInterrupt):
        if not ready:
            raise
        print('plan-identity: warning: could not confirm release of the setup lock.', file=sys.stderr, flush=True)
    print('plan-identity: ready', flush=True)


def _remove(api: IdentityApi, owner: PlanAccount) -> None:
    with _step('check IAM resource ownership'):
        policy = _lookup(api, 'Policy', owner.policy_arn, PolicyArn=owner.policy_arn)
        user = _lookup(api, 'User', owner.user_arn, UserName=NAME)
    if user:
        with _step('delete the plan user access keys'):
            for key in _access_key_ids(api):
                _remove_iam(api, 'delete-access-key', UserName=NAME, AccessKeyId=key)
        if policy:
            with _step('detach the managed plan policy'):
                _remove_iam(api, 'detach-user-policy', UserName=NAME, PolicyArn=owner.policy_arn)
        with _step('delete the plan user'):
            _remove_iam(api, 'delete-user', UserName=NAME)
    if policy:
        with _step('delete the managed plan policy'):
            _remove_old_policy_versions(api, owner)
            _remove_iam(api, 'delete-policy', PolicyArn=owner.policy_arn)


def command_down(settings: Path, *, approve_destroy: bool) -> None:
    if not approve_destroy:
        print('Plan: revoke the plan access key and remove the IAM user, policy, and local encrypted file.')
        print(f'On natedev, run {DOWN_COMMAND}.')
        return

    configuration = conf.read(settings)
    region, timeout = configuration.text('region'), configuration.number('aws_timeout_s')
    path = credential_path()
    with control_lock(path.parent):
        with _step('confirm the administrator account'):
            api = _admin_api(settings, region, timeout)
            owner = PlanAccount.from_identity(api.call('sts', 'get-caller-identity'))
        remove_local = True
        if os.path.lexists(path):
            try:
                credential = PlanCredentials.decode(_decrypt(_read_encrypted(path), interactive=True))
            except PlanIdentityRefused:
                remove_local = False
            else:
                if credential.owner != owner:
                    raise PlanIdentityRefused('The local plan key belongs to a different AWS account.')
        _remove(api, owner)
        if not remove_local:
            print('plan-identity: kept the local encrypted file because its account could not be confirmed.', flush=True)
            return
        with _step('remove the local encrypted plan key'):
            path.unlink(missing_ok=True)
            sync_folder(path.parent)


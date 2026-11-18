"""Check plan identity provisioning across the AWS and GPG command boundaries."""

from collections.abc import Generator, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from typing import cast, final, override
from urllib.parse import quote
import unittest
from unittest import mock

from scripts.aws_target import admin_secrets, deploy, ledger, plan_identity
from scripts.aws_target.controller_lock import control_lock


OWNER = plan_identity.PlanAccount('aws', '000000000000')
KEY = 'AKIAFAKEFAKEFAKE'
SECRET = 'fixture-plan-secret'
TAG = {'Key': 'ManagedBy', 'Value': 'aws-build-plan-identity'}
PROOF_CALLS = ['get-caller-identity', 'describe-availability-zones', 'simulate-principal-policy']
POLICY_READS = ['get-caller-identity', 'get-policy', 'get-user', 'list-access-keys', 'get-policy-version']
STACKS = (
    plan_identity.PlanStack('us-east-1', 'fixture-hana'),
    plan_identity.PlanStack('us-east-2', 'fixture-build'),
    plan_identity.PlanStack('us-west-2', 'fixture-build-gates-us-west-2'),
)


@final
class Commands:
    def __init__(self, plan_file: Path) -> None:
        self.plan_file = plan_file
        self.key_deletion_file_states: list[bool] = []
        self.operations: list[str] = []
        self.arguments: list[tuple[str, ...]] = []
        self.payloads: dict[str, dict[str, object]] = {}
        self.ciphertexts: dict[bytes, bytes] = {}
        self.versions: dict[str, str] = {}
        self.default_version = ''
        self.version_number = 0
        self.user = False
        self.key = False
        self.key_creation = 'complete'
        self.empty_key_lists = 0
        self.cold = False
        self.decrypt_timeout = False
        self.fail_key_encryption = False
        self.fail_denial = False

    def execute(self, arguments: Sequence[str], *, input: bytes, capture_output: bool,
                timeout: float, check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        assert capture_output and timeout > 0 and not check
        self.arguments.append(tuple(arguments))
        if arguments[0] == 'gpg':
            return self.gpg(arguments, input)
        assert arguments[0] == 'aws' and arguments[-1] == 'file:///dev/stdin'
        assert env['AWS_CONFIG_FILE'] == env['AWS_SHARED_CREDENTIALS_FILE'] == '/dev/null'
        assert not {'AWS_SESSION_TOKEN', 'AWS_PROFILE', 'AWS_ENDPOINT_URL'} & env.keys()
        operation = arguments[4]
        self.operations.append(operation)
        if operation == 'delete-access-key':
            self.key_deletion_file_states.append(os.path.lexists(self.plan_file))
        parameters = ledger.mapping(cast(object, json.loads(input)))
        self.payloads[operation] = parameters
        key = env['AWS_ACCESS_KEY_ID']
        assert key in ('fixture-admin', KEY)
        if operation == 'get-caller-identity':
            arn = OWNER.user_arn if key == KEY else OWNER.iam('user/fixture-admin')
            reply: object = {'Arn': arn, 'Account': OWNER.account}
        elif key == KEY:
            reply = self.proof(operation, parameters)
        else:
            return self.admin(arguments, operation, parameters)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply).encode(), b'')

    def gpg(self, arguments: Sequence[str], content: bytes) -> subprocess.CompletedProcess[bytes]:
        if '--list-only' in arguments:
            return subprocess.CompletedProcess(arguments, 0, b'[GNUPG:] ENC_TO AAAAAAAAAAAAAAAA 1 0\n', b'')
        if '--encrypt' in arguments:
            assert 'AAAAAAAAAAAAAAAA!' in arguments
            if self.fail_key_encryption and SECRET.encode() in content:
                return subprocess.CompletedProcess(arguments, 2, b'', SECRET.encode())
            ciphertext = b'encrypted-fixture-' + str(len(self.ciphertexts)).encode()
            self.ciphertexts[ciphertext] = content
            return subprocess.CompletedProcess(arguments, 0, ciphertext, b'')
        assert '--decrypt' in arguments
        if self.cold:
            assert '--batch' in arguments and 'error' in arguments
            return subprocess.CompletedProcess(arguments, 2, b'', b'[GNUPG:] ERROR pkdecrypt_failed 67108949\n')
        if content not in self.ciphertexts:
            return subprocess.CompletedProcess(arguments, 2, b'', b'[GNUPG:] NODATA 1\n')
        plaintext = self.ciphertexts[content]
        if self.decrypt_timeout and SECRET.encode() in plaintext:
            raise subprocess.TimeoutExpired(arguments, 10)
        return subprocess.CompletedProcess(arguments, 0, plaintext, b'')

    def proof(self, operation: str, parameters: dict[str, object]) -> object:
        if operation == 'describe-availability-zones':
            return {'AvailabilityZones': [{'ZoneName': 'us-east-2a'}]}
        assert operation == 'simulate-principal-policy'
        assert parameters['PolicySourceArn'] == OWNER.user_arn
        assert parameters['ActionNames'] == ['cloudformation:ExecuteChangeSet', 'cloudformation:DeleteStack',
                                              'iam:PassRole', 'ec2:RunInstances', 'ec2:DescribeInstanceAttribute',
                                              'ec2:DescribeSpotInstanceRequests', 's3:PutObject']
        return {'EvaluationResults': [
            {'EvalActionName': action,
             'EvalDecision': 'allowed' if action == 'cloudformation:DeleteStack' and self.fail_denial else 'explicitDeny'}
            for action in plan_identity.DENIAL_PROBES]}

    def admin(self, arguments: Sequence[str], operation: str,
              parameters: dict[str, object]) -> subprocess.CompletedProcess[bytes]:
        reply: object = {}
        if operation == 'get-policy' and self.versions:
            reply = {'Policy': {'Arn': OWNER.policy_arn, 'Tags': [TAG], 'DefaultVersionId': self.default_version}}
        elif operation == 'get-user' and self.user:
            reply = {'User': {'Arn': OWNER.user_arn, 'Tags': [TAG]}}
        elif operation in ('get-policy', 'get-user'):
            label = 'GetPolicy' if operation == 'get-policy' else 'GetUser'
            return subprocess.CompletedProcess(arguments, 1, b'',
                f'An error occurred (NoSuchEntity) when calling the {label} operation: {KEY} {SECRET}'.encode())
        elif operation == 'create-policy':
            assert parameters['Tags'] == [TAG] and not self.versions
            self.add_policy_version(parameters)
        elif operation == 'get-policy-version':
            assert parameters['PolicyArn'] == OWNER.policy_arn and parameters['VersionId'] == self.default_version
            reply = {'PolicyVersion': {'Document': quote(self.versions[self.default_version], safe='')}}
        elif operation == 'list-policy-versions':
            assert parameters['PolicyArn'] == OWNER.policy_arn
            reply = {'Versions': [{'VersionId': version, 'IsDefaultVersion': version == self.default_version}
                                  for version in self.versions]}
        elif operation == 'create-policy-version':
            assert parameters['PolicyArn'] == OWNER.policy_arn and parameters['SetAsDefault'] is True
            assert self.versions and len(self.versions) < 5
            self.add_policy_version(parameters)
        elif operation == 'delete-policy-version':
            version = parameters['VersionId']
            assert isinstance(version, str) and version != self.default_version
            assert parameters['PolicyArn'] == OWNER.policy_arn
            del self.versions[version]
        elif operation == 'create-user':
            assert parameters['Tags'] == [TAG] and not self.user
            self.user = True
        elif operation == 'attach-user-policy':
            assert self.versions and self.user and parameters['PolicyArn'] == OWNER.policy_arn
        elif operation == 'create-access-key':
            assert self.user and not self.key
            self.key = True
            if self.key_creation == 'timeout':
                raise subprocess.TimeoutExpired(arguments, 15, stderr=SECRET.encode())
            fields = {'SecretAccessKey': SECRET}
            if self.key_creation != 'missing id':
                fields['AccessKeyId'] = KEY
            reply = {'AccessKey': fields}
        elif operation == 'list-access-keys':
            visible = self.key and not self.empty_key_lists
            if self.key and self.empty_key_lists:
                self.empty_key_lists -= 1
            reply = {'AccessKeyMetadata': [{'AccessKeyId': KEY}] if visible else []}
        elif operation == 'delete-access-key':
            assert parameters['AccessKeyId'] == KEY and self.key
            self.key = False
        elif operation == 'detach-user-policy':
            assert self.versions and self.user and parameters['PolicyArn'] == OWNER.policy_arn
        elif operation == 'delete-user':
            assert not self.key
            self.user = False
        elif operation == 'delete-policy':
            assert not self.user
            assert tuple(self.versions) == (self.default_version,)
            self.versions.clear()
        else:
            raise AssertionError(operation)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply).encode(), b'')

    def add_policy_version(self, parameters: dict[str, object]) -> None:
        document = parameters['PolicyDocument']
        assert isinstance(document, str)
        self.version_number += 1
        self.default_version = f'v{self.version_number}'
        self.versions[self.default_version] = document


@final
class PlanIdentityTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        settings = home / 'aws_target.conf'
        _ = settings.write_text('region=us-east-2\nstack_name=fixture-build\n# range: 0.001..\naws_timeout_s=15\n')
        _ = (home / 'cloud_gate.conf').write_text('regions=us-east-2 us-west-2\n')
        _ = (home / 'hana_aws.conf').write_text('region=us-east-1\nstack_name=fixture-hana\n')
        self.settings: Path = settings  # pyright: ignore[reportUninitializedInstanceVariable]
        self.path: Path = home / '.config/aws-build/plan.gpg'  # pyright: ignore[reportUninitializedInstanceVariable]
        self.commands: Commands = Commands(self.path)  # pyright: ignore[reportUninitializedInstanceVariable]
        self.output: StringIO = self.enterContext(redirect_stdout(StringIO()))  # pyright: ignore[reportUninitializedInstanceVariable]
        self.errors: StringIO = self.enterContext(redirect_stderr(StringIO()))  # pyright: ignore[reportUninitializedInstanceVariable]
        _ = self.enterContext(mock.patch.object(Path, 'home', return_value=home))
        _ = self.enterContext(mock.patch.object(subprocess, 'run', new=self.commands.execute))
        _ = self.enterContext(mock.patch.object(admin_secrets.AdminSecretSession, 'environment', return_value={
            'AWS_ACCESS_KEY_ID': 'fixture-admin', 'AWS_SECRET_ACCESS_KEY': 'fixture-admin-secret',
            'AWS_PROFILE': 'unused-profile', 'AWS_ENDPOINT_URL': 'invalid-endpoint',
        }))
        _ = self.enterContext(mock.patch.object(admin_secrets, 'terminal', side_effect=lambda: os.open('/dev/null', os.O_RDONLY)))
        _ = self.enterContext(mock.patch.object(os, 'ttyname', return_value='fixture-tty'))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            'AWS_PROFILE': 'fixture-profile', 'AWS_ENDPOINT_URL': 'invalid-endpoint', 'AWS_SESSION_TOKEN': 'fixture-token',
            'CONF_DIR': str(home),
        })))

    def run_deploy(self, *arguments: str) -> int:
        argv = ['deploy.py', *arguments, '--settings', str(self.settings)]
        with mock.patch.object(sys, 'argv', argv):
            return deploy.main()

    def test_policy_document_limits_change_sets_and_denies_every_other_action(self) -> None:
        reads = [
            'budgets:ViewBudget', 'ce:GetCostAndUsage', 'cloudformation:ListStacks',
            'ec2:DescribeAvailabilityZones', 'ec2:DescribeImages', 'ec2:DescribeInstanceTypeOfferings',
            'ec2:DescribeInstances', 'ec2:DescribeRouteTables', 'ec2:DescribeSpotPriceHistory',
            'ec2:DescribeSubnets', 'ec2:DescribeVolumes', 'ec2:DescribeVpcs', 'servicequotas:GetServiceQuota',
            'servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota', 'sts:GetCallerIdentity',
        ]
        stack_reads = [
            'cloudformation:DescribeChangeSet',
            'cloudformation:DescribeStackEvents', 'cloudformation:DescribeStackResource',
            'cloudformation:DescribeStackResources', 'cloudformation:DescribeStacks', 'cloudformation:GetTemplate',
        ]
        iam_reads = ['iam:GetUser', 'iam:SimulatePrincipalPolicy']
        s3_reads = ['s3:ListBucket']
        stacks = [
            'arn:aws:cloudformation:us-east-1:000000000000:stack/fixture-hana/*',
            'arn:aws:cloudformation:us-east-2:000000000000:stack/fixture-build/*',
            'arn:aws:cloudformation:us-west-2:000000000000:stack/fixture-build-gates-us-west-2/*',
        ]
        writes = ['cloudformation:CreateChangeSet', 'cloudformation:DeleteChangeSet']
        expected = {'Version': '2012-10-17', 'Statement': [
            {'Sid': 'ReadSetupInputs', 'Effect': 'Allow', 'Action': reads, 'Resource': '*'},
            {'Sid': 'ReadWorkloadStacks', 'Effect': 'Allow', 'Action': stack_reads, 'Resource': stacks},
            {'Sid': 'ReadAndSimulateUsers', 'Effect': 'Allow', 'Action': iam_reads,
             'Resource': 'arn:aws:iam::000000000000:user/*'},
            {'Sid': 'ReadBucketMetadata', 'Effect': 'Allow', 'Action': s3_reads, 'Resource': 'arn:aws:s3:::*',
             'Condition': {'StringEquals': {'s3:ResourceAccount': '000000000000'}}},
            {'Sid': 'ManagePlanChangeSets', 'Effect': 'Allow', 'Action': writes,
             'Resource': stacks, 'Condition': {'StringLike': {'cloudformation:ChangeSetName': 'aws-build-plan-*'},
                 'Null': {'cloudformation:RoleArn': 'true', 'cloudformation:ImportResourceTypes': 'true',
                          'cloudformation:TemplateUrl': 'true'}}},
            {'Sid': 'DenyOtherActions', 'Effect': 'Deny', 'Resource': '*', 'NotAction': [
                *reads, *stack_reads, *iam_reads, *s3_reads, *writes]},
        ]}
        self.assertEqual(plan_identity.policy_document(OWNER, STACKS), expected)
        denied = (*plan_identity.DENIAL_PROBES, 's3:GetObject', 's3:GetObjectVersion', 'iam:CreateAccessKey',
                  'ec2:TerminateInstances', 'cloudformation:UpdateStack', 'lambda:InvokeFunction',
                  'iam:ListAccessKeys', 's3:GetBucketLocation')
        allowed = {*reads, *stack_reads, *iam_reads, *s3_reads, *writes}
        self.assertFalse(allowed.intersection(denied))

    def test_dry_runs_do_not_read_configuration_or_create_anything(self) -> None:
        self.settings.unlink()
        self.assertEqual(self.run_deploy('plan-identity'), 0)
        self.assertEqual(self.run_deploy('down', '--workload', 'plan-identity'), 0)
        self.assertEqual(self.commands.arguments, [])
        self.assertFalse(self.path.parent.exists())
        self.assertIn('plan-identity --approve-resources', self.output.getvalue())
        self.assertIn('plan-identity --approve-destroy', self.output.getvalue())
        self.assertIn('read setup inputs, applied service quotas, and quota request history', self.output.getvalue())

    def test_setup_refuses_a_wildcard_in_the_configured_stack(self) -> None:
        _ = self.settings.with_name('hana_aws.conf').write_text('region=us-east-1\nstack_name=fixture-hana*\n')
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 1)
        self.assertIn('A plan stack name is invalid.', self.errors.getvalue())
        self.assertEqual(self.commands.arguments, [])

    def test_create_encrypts_one_key_and_repeat_keeps_the_matching_key(self) -> None:
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0, self.errors.getvalue())
        self.assertEqual(self.commands.operations, [
            'get-caller-identity', 'get-policy', 'get-user', 'create-policy', 'create-user',
            'attach-user-policy', 'create-access-key', *PROOF_CALLS])
        policy = self.commands.payloads['create-policy']['PolicyDocument']
        assert isinstance(policy, str)
        self.assertEqual(cast(object, json.loads(policy)), plan_identity.policy_document(OWNER, STACKS))
        for stack in STACKS:
            self.assertIn(f'allow stack {stack.name} in {stack.region}', self.output.getvalue())
        self.assertIn('allow reads of applied service quotas and quota request history', self.output.getvalue())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn(SECRET.encode(), self.path.read_bytes())
        self.assertEqual(plan_identity.plan_credentials().access_key_id, KEY)
        original = self.path.read_bytes()
        self.commands.operations.clear()
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0)
        self.assertEqual(self.commands.operations, [*POLICY_READS, *PROOF_CALLS])
        self.assertTrue(self.commands.key)
        self.assertEqual(self.path.read_bytes(), original)
        for action in plan_identity.DENIAL_PROBES:
            self.assertIn(f'{action} explicit deny passed', self.output.getvalue())
        self.assert_no_credentials()

    def test_changed_config_updates_the_policy_without_replacing_the_key(self) -> None:
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0, self.errors.getvalue())
        original = self.path.read_bytes()
        for region in ('us-west-1', 'eu-west-1'):
            with self.subTest(region=region):
                _ = self.settings.with_name('cloud_gate.conf').write_text(f'regions=us-east-2 {region}\n')
                previous_default = self.commands.default_version
                old_versions = set(self.commands.versions) - {previous_default}
                self.commands.operations.clear()
                self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0, self.errors.getvalue())
                self.assertEqual(self.commands.operations, [
                    *POLICY_READS, 'list-policy-versions', *(['delete-policy-version'] * len(old_versions)),
                    'create-policy-version', *PROOF_CALLS])
                stacks = (*STACKS[:2], plan_identity.PlanStack(region, f'fixture-build-gates-{region}'))
                document = self.commands.versions[self.commands.default_version]
                self.assertEqual(cast(object, json.loads(document)), plan_identity.policy_document(OWNER, tuple(sorted(stacks))))
                self.assertEqual(self.path.read_bytes(), original)
                self.assertEqual(set(self.commands.versions), {previous_default, self.commands.default_version})
        self.assertIn(f'{plan_identity.CREATE_COMMAND} updates it.', self.output.getvalue())
        self.commands.operations.clear()
        self.assertEqual(self.run_deploy('down', '--workload', 'plan-identity', '--approve-destroy'), 0)
        self.assertEqual(self.commands.operations[-3:], ['list-policy-versions', 'delete-policy-version', 'delete-policy'])
        self.assert_no_credentials()

    def test_encryption_failure_revokes_the_key_and_keeps_output_private(self) -> None:
        self.commands.fail_key_encryption = True
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 1)
        self.assertEqual(self.commands.operations[-3:], ['create-access-key', 'list-access-keys', 'delete-access-key'])
        self.assertFalse(self.commands.key)
        self.assertFalse(self.path.exists())
        self.assert_no_credentials()

    def test_directory_sync_failure_keeps_the_published_key_ready(self) -> None:
        operations_at_sync: list[str] = []

        def fail_sync(folder: Path) -> None:
            self.assertEqual(folder, self.path.parent)
            self.assertTrue(self.path.is_file())
            operations_at_sync.extend(self.commands.operations)
            raise OSError('fixture directory sync failure')

        with mock.patch.object(plan_identity, 'sync_folder', new=fail_sync):
            self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0)
        self.assertEqual(operations_at_sync[-3:], PROOF_CALLS)
        self.assertEqual(self.commands.operations, operations_at_sync)
        self.assertTrue(self.commands.key)
        self.assertEqual(plan_identity.plan_credentials().access_key_id, KEY)
        self.assertIn('warning:', self.errors.getvalue())
        self.assertIn('directory sync', self.errors.getvalue())
        self.assertTrue(self.output.getvalue().endswith('plan-identity: ready\n'))
        self.assert_no_credentials()

    def test_interrupt_after_rename_keeps_the_published_key_ready(self) -> None:
        replace = os.replace

        def interrupt_after_rename(source: Path, destination: Path) -> None:
            replace(source, destination)
            raise KeyboardInterrupt

        with mock.patch.object(os, 'replace', new=interrupt_after_rename):
            self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0)
        self.assertEqual(self.commands.key_deletion_file_states, [])
        self.assertTrue(self.commands.key)
        self.assertEqual(plan_identity.plan_credentials().access_key_id, KEY)
        self.assertIn('warning:', self.errors.getvalue())
        self.assertTrue(self.output.getvalue().endswith('plan-identity: ready\n'))

    def test_setup_lock_release_failure_keeps_the_published_key_ready(self) -> None:
        @contextmanager
        def fail_after_release(directory: Path) -> Generator[None]:
            with control_lock(directory):
                yield
            raise OSError('fixture lock release failure')

        with mock.patch.object(plan_identity, 'control_lock', new=fail_after_release):
            self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0)
        self.assertEqual(self.commands.key_deletion_file_states, [])
        self.assertTrue(self.commands.key)
        self.assertEqual(plan_identity.plan_credentials().access_key_id, KEY)
        self.assertIn('could not confirm release of the setup lock', self.errors.getvalue())
        self.assertTrue(self.output.getvalue().endswith('plan-identity: ready\n'))

    def test_setup_failures_never_revoke_a_key_while_the_file_exists(self) -> None:
        replace = os.replace
        for starting_key in ('none', 'orphaned', 'matching', 'policy changed'):
            def run(failure: tuple[str, str]) -> list[tuple[str, str]]:
                self.path.unlink(missing_ok=True)
                commands = Commands(self.path)
                observed: list[tuple[str, str]] = []

                def inject(step: str, point: str) -> None:
                    observed.append((step, point))
                    if (step, point) == failure:
                        raise OSError('fixture setup failure')

                @contextmanager
                def step(description: str) -> Generator[None]:
                    inject(description, 'before')
                    yield
                    inject(description, 'after')

                def rename(source: Path, destination: Path) -> None:
                    inject('rename the verified key', 'before')
                    replace(source, destination)

                with mock.patch.object(subprocess, 'run', new=commands.execute):
                    if starting_key != 'none':
                        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0)
                    if starting_key == 'orphaned':
                        self.path.unlink()
                    if starting_key == 'policy changed':
                        commands.versions[commands.default_version] = '{}'
                    with (mock.patch.object(plan_identity, '_step', new=step),
                          mock.patch.object(os, 'replace', new=rename), mock.patch.object(time, 'sleep')):
                        status = self.run_deploy('plan-identity', '--approve-resources')

                self.assertNotIn(True, commands.key_deletion_file_states)
                if failure == ('', ''):
                    self.assertEqual(status, 0)
                    self.assertTrue(observed)
                else:
                    self.assertIn(failure, observed)
                    published = failure == ('save the verified key at ~/.config/aws-build/plan.gpg', 'after')
                    self.assertEqual(status, 0 if published else 1)
                    has_file = published or starting_key in ('matching', 'policy changed')
                    self.assertEqual(self.path.exists(), has_file)
                    if has_file:
                        self.assertTrue(commands.key)
                return observed

            for failure in run(('', '')):
                with self.subTest(starting_key=starting_key, failure=failure):
                    _ = run(failure)

    def assert_key_creation_recovers(self, failure: str) -> None:
        expected_lists = self.commands.empty_key_lists + 1
        self.commands.key_creation = failure
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 1)
        self.assertEqual(self.commands.operations[-expected_lists - 2:],
                         ['create-access-key', *(['list-access-keys'] * expected_lists), 'delete-access-key'])
        self.assertFalse(self.commands.key)
        self.assertFalse(self.path.exists())
        self.commands.operations.clear()
        self.commands.key_creation = 'complete'
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0, self.errors.getvalue())
        self.assertEqual(self.commands.operations, [
            'get-caller-identity', 'get-policy', 'get-user', 'list-access-keys', 'get-policy-version',
            'attach-user-policy', 'create-access-key', *PROOF_CALLS])
        self.assertTrue(self.commands.key)
        self.assertEqual(plan_identity.plan_credentials().access_key_id, KEY)
        self.assert_no_credentials()

    def test_key_creation_timeout_revokes_the_key_and_allows_retry(self) -> None:
        self.commands.empty_key_lists = 1
        with mock.patch.object(time, 'sleep', return_value=None):
            self.assert_key_creation_recovers('timeout')

    def test_key_reply_without_an_id_revokes_the_key_and_allows_retry(self) -> None:
        self.assert_key_creation_recovers('missing id')

    def test_late_key_is_revoked_on_the_next_approved_run(self) -> None:
        self.commands.key_creation = 'timeout'
        self.commands.empty_key_lists = len(plan_identity.PROOF_RETRY_DELAYS)
        with mock.patch.object(time, 'sleep', return_value=None):
            self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 1)
        self.assertIn('An IAM key can still appear.', self.errors.getvalue())
        self.assertIn(f'Run {plan_identity.CREATE_COMMAND} again on natedev to remove it.', self.errors.getvalue())
        self.assertTrue(self.commands.key)
        self.assertFalse(self.path.exists())
        self.assertNotIn('delete-access-key', self.commands.operations)
        self.assertTrue(self.output.getvalue().endswith('plan-identity: failed\n'))
        self.commands.key_creation = 'complete'
        self.commands.operations.clear()
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0, self.errors.getvalue())
        self.assertEqual(self.commands.operations, [
            'get-caller-identity', 'get-policy', 'get-user', 'list-access-keys', 'delete-access-key',
            'get-policy-version', 'attach-user-policy', 'create-access-key', *PROOF_CALLS])
        self.assertEqual(plan_identity.plan_credentials().access_key_id, KEY)
        self.assert_no_credentials()

    def test_setup_refuses_a_local_key_that_differs_from_the_iam_key(self) -> None:
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0, self.errors.getvalue())
        original = self.path.read_bytes()
        self.commands.ciphertexts[original] = plan_identity.PlanCredentials('other-key', SECRET, OWNER).encode()
        self.commands.operations.clear()
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 1)
        self.assertIn('The local key differs from the sole IAM key.', self.errors.getvalue())
        self.assertEqual(self.commands.operations, POLICY_READS[:-1])
        self.assertTrue(self.commands.key)
        self.assertEqual(self.path.read_bytes(), original)
        self.assert_no_credentials()

    def assert_setup_preserves_unreadable_key(self) -> None:
        original = self.path.read_bytes()
        self.commands.operations.clear()
        self.commands.arguments.clear()
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 1)
        self.assertIn('Run github-warmup in a terminal on natedev.', self.errors.getvalue())
        self.assertIn('If the file is damaged, delete it and run the command again.', self.errors.getvalue())
        self.assertEqual(self.commands.operations, [])
        self.assertEqual(self.commands.arguments, [(
            'gpg', '--no-options', '--no-tty', '--quiet', '--batch', '--pinentry-mode', 'error',
            '--status-fd', '2', '--decrypt')])
        self.assertTrue(self.commands.key)
        self.assertEqual(self.path.read_bytes(), original)
        self.assert_no_credentials()

    def test_setup_refuses_damaged_ciphertext(self) -> None:
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0, self.errors.getvalue())
        _ = self.path.write_bytes(b'invalid ciphertext')
        self.assert_setup_preserves_unreadable_key()

    def test_setup_refuses_a_cold_agent_with_a_local_key(self) -> None:
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0, self.errors.getvalue())
        self.commands.cold = True
        self.assert_setup_preserves_unreadable_key()

    def test_setup_refuses_a_timeout_while_decrypting_the_local_key(self) -> None:
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0, self.errors.getvalue())
        self.commands.decrypt_timeout = True
        self.assert_setup_preserves_unreadable_key()

    def test_setup_refuses_a_cold_agent_without_a_local_key(self) -> None:
        self.commands.cold = True
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 1)
        self.assertIn('Run github-warmup in a terminal on natedev.', self.errors.getvalue())
        self.assertEqual(self.commands.operations, [])
        self.assertFalse(self.path.exists())
        self.assert_no_credentials()

    def test_simulation_that_allows_stack_deletion_fails_the_proof(self) -> None:
        self.commands.fail_denial = True
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 1)
        self.assertIn('cloudformation:DeleteStack explicit deny failed', self.output.getvalue())
        self.assertFalse(self.commands.key)
        self.assertFalse(self.path.exists())
        self.assert_no_credentials()

    def test_down_deletes_the_key_before_the_user_and_policy(self) -> None:
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0)
        self.commands.operations.clear()
        self.assertEqual(self.run_deploy('down', '--workload', 'plan-identity', '--approve-destroy'), 0)
        self.assertEqual(self.commands.operations, [
            'get-caller-identity', 'get-policy', 'get-user', 'list-access-keys', 'delete-access-key',
            'detach-user-policy', 'delete-user', 'list-policy-versions', 'delete-policy'])
        self.assertFalse(self.path.exists())
        self.assert_no_credentials()

    def test_reader_fails_with_a_named_cold_agent_refusal(self) -> None:
        self.path.parent.mkdir(parents=True)
        _ = self.path.write_bytes(b'encrypted-fixture')
        self.path.chmod(0o600)
        self.commands.cold = True
        with self.assertRaisesRegex(plan_identity.PlanAgentCold, 'github-warmup'):
            _ = plan_identity.plan_credentials()
        self.assertEqual(self.commands.operations, [])
        self.assert_no_credentials()

    def test_down_revokes_the_key_if_the_local_file_is_unreadable(self) -> None:
        self.assertEqual(self.run_deploy('plan-identity', '--approve-resources'), 0)
        _ = self.path.write_bytes(b'invalid ciphertext')
        self.assertEqual(self.run_deploy('down', '--workload', 'plan-identity', '--approve-destroy'), 0)
        self.assertFalse(self.commands.key)
        self.assertFalse(self.commands.user)
        self.assertFalse(self.commands.versions)
        self.assertTrue(self.path.is_file())
        self.assertIn('kept the local encrypted file', self.output.getvalue())
        self.assert_no_credentials()

    def assert_no_credentials(self) -> None:
        output = self.output.getvalue() + self.errors.getvalue() + repr(self.commands.arguments)
        for value in (KEY, SECRET, OWNER.account, OWNER.user_arn, 'fixture-admin-secret'):
            self.assertNotIn(value, output)


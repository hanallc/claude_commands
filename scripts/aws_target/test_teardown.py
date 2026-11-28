"""Test gate bucket removal with a local AWS transport."""
from collections.abc import Sequence
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from unittest import mock
from typing import cast

from scripts.aws_target import admin_secrets, ledger, teardown
from scripts.aws_target.aws_cli import AwsCli, AwsUnavailable
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.lib import conf


FIXTURE_ACCOUNT = '0' * 12


class GateBucketRemovalTests(CostFixture):
    def check_bucket_removal(self, *, resume: bool = False) -> None:
        home = conf.read(self.settings).text('region')
        deployment = 'fixture'
        bucket = 'cloud-claude-gate-' + deployment
        contents = {'requests/fixture.json', 'source/fixture.pack'}
        calls: list[str] = []
        emptied: list[set[str]] = []
        status = 'DELETE_IN_PROGRESS' if resume else 'CREATE_COMPLETE'

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            nonlocal status
            operation = arguments[6]
            calls.append(operation)
            if arguments[5] == 's3api':
                self.assertEqual(arguments[arguments.index('--bucket') + 1], bucket)
                self.assertEqual(arguments[arguments.index('--expected-bucket-owner') + 1], FIXTURE_ACCOUNT)
            reply: dict[str, object] = {}
            if operation == 'describe-stacks':
                stack: dict[str, object] = {'StackName': 'fixture', 'StackId': 'fixture', 'StackStatus': status}
                if status != 'DELETE_IN_PROGRESS':
                    stack['Outputs'] = [{'OutputKey': 'GateBucket', 'OutputValue': bucket}]
                reply = {'Stacks': [stack]}
                if status == 'DELETE_IN_PROGRESS':
                    status = 'DELETE_FAILED'
            elif operation == 'describe-stack-events':
                self.assertEqual(status, 'DELETE_FAILED')
                reply = {'StackEvents': [{'LogicalResourceId': 'GateBucket', 'ResourceStatus': 'DELETE_FAILED',
                    'ResourceStatusReason': 'The bucket you tried to delete is not empty'}]}
            elif operation == 'get-bucket-tagging':
                reply = {'TagSet': [{'Key': 'GateDeployment', 'Value': deployment}]}
            elif operation == 'list-objects-v2':
                reply = {'Contents': [{'Key': key} for key in sorted(contents)]}
            elif operation == 'delete-objects':
                payload = ledger.mapping(cast(object, json.loads(arguments[arguments.index('--delete') + 1])))
                keys = {ledger.text(row, 'Key') for row in ledger.rows(payload, 'Objects')}
                self.assertEqual(keys, contents)
                emptied.append(keys)
                contents.clear()
            elif operation == 'delete-stack':
                self.assertFalse(contents)
                if status == 'CREATE_COMPLETE':
                    contents.add('source/late.pack')
                    status = 'DELETE_FAILED'
                else:
                    status = 'DELETE_COMPLETE'
            else:
                raise AssertionError(arguments)
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        api = AwsCli('fixture', home, 1, execute=execute)
        admin = cast(admin_secrets.AdminSecretSession, mock.create_autospec(admin_secrets.AdminSecretSession, instance=True))
        removal = teardown.TargetRemoval(self.settings, self.state, Path(), admin, api, execute)
        removal.identity = replace(removal.identity, stack_id='fixture', deployment_id=deployment, account_id=FIXTURE_ACCOUNT)
        self.assertTrue(removal.foundation())
        self.assertFalse(contents)
        self.assertEqual(calls.count('delete-stack'), 1 if resume else 2)
        self.assertEqual(calls.count('describe-stack-events'), 1)
        self.assertEqual(calls.count('get-bucket-tagging'), 1 if resume else 2)
        expected = [{'requests/fixture.json', 'source/fixture.pack'}]
        if not resume:
            expected.append({'source/late.pack'})
        self.assertEqual(emptied, expected)

    def test_down_empties_the_home_gate_bucket_and_retries_after_a_late_write(self) -> None:
        self.check_bucket_removal()

    def test_down_reads_fresh_bucket_output_after_a_resumed_delete_fails(self) -> None:
        self.check_bucket_removal(resume=True)

    def test_down_refuses_a_gate_bucket_for_another_deployment(self) -> None:
        stack: dict[str, object] = {'Outputs': [{'OutputKey': 'GateBucket', 'OutputValue': 'cloud-claude-gate-old'}]}
        with self.assertRaisesRegex(ValueError, 'GateBucket differs from the saved deployment'):
            _ = teardown.verified_gate_bucket(stack, 'current')

    def test_gate_bucket_drain_refuses_a_replacement_tag_or_owner(self) -> None:
        for owner_matches in (True, False):
            with self.subTest(owner_matches=owner_matches):
                def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
                    self.assertEqual(arguments[5:11], ['s3api', 'get-bucket-tagging', '--bucket',
                        'cloud-claude-gate-current', '--expected-bucket-owner', FIXTURE_ACCOUNT])
                    if not owner_matches:
                        return subprocess.CompletedProcess(arguments, 1, '', 'AccessDenied')
                    return subprocess.CompletedProcess(arguments, 0,
                        json.dumps({'TagSet': [{'Key': 'GateDeployment', 'Value': 'old'}]}), '')

                api = AwsCli('fixture', conf.read(self.settings).text('region'), 1, execute=execute)
                expected_type = ValueError if owner_matches else AwsUnavailable
                expected_reason = 'live gate bucket tag differs' if owner_matches else 'AccessDenied'
                with self.assertRaisesRegex(expected_type, expected_reason):
                    teardown.empty_gate_bucket(api, 'cloud-claude-gate-current',
                                               deployment='current', account_id=FIXTURE_ACCOUNT)

    def test_gate_bucket_removal_uses_at_most_500_keys_per_batch(self) -> None:
        region = conf.read(self.settings).text('region')
        deployment = 'a' * 32
        bucket = 'cloud-claude-gate-' + deployment
        contents = [f'source/fixture-{index:04d}.pack' for index in range(1001)]
        sizes: list[int] = []

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            self.assertEqual(arguments[arguments.index('--expected-bucket-owner') + 1], FIXTURE_ACCOUNT)
            if 'get-bucket-tagging' in arguments:
                return subprocess.CompletedProcess(arguments, 0,
                    json.dumps({'TagSet': [{'Key': 'GateDeployment', 'Value': deployment}]}), '')
            if 'list-objects-v2' in arguments:
                self.assertIn('--no-paginate', arguments)
                limit = int(arguments[arguments.index('--max-keys') + 1])
                self.assertEqual(limit, 500)
                reply = {'Contents': [{'Key': key} for key in contents[:limit]]}
                return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
            self.assertIn('delete-objects', arguments)
            payload = arguments[arguments.index('--delete') + 1]
            keys = ledger.rows(ledger.mapping(cast(object, json.loads(payload))), 'Objects')
            sizes.append(len(keys))
            self.assertEqual([ledger.text(key, 'Key') for key in keys], contents[:len(keys)])
            del contents[:len(keys)]
            return subprocess.CompletedProcess(arguments, 0, '{}', '')

        teardown.empty_gate_bucket(AwsCli('fixture', region, 1, execute=execute), bucket,
                                   deployment=deployment, account_id=FIXTURE_ACCOUNT)
        self.assertEqual(sizes, [500, 500, 1])
        self.assertFalse(contents)

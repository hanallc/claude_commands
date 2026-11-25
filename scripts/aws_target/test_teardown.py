"""Test source bucket removal with a local AWS transport."""
from collections.abc import Sequence
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from unittest import mock
from typing import cast

from scripts.aws_target import admin_secrets, gate_source, ledger, teardown
from scripts.aws_target.aws_cli import AwsCli
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.lib import conf


class SourceBucketRemovalTests(CostFixture):
    def test_down_rejects_a_gate_record_for_another_deployment_before_deletion(self) -> None:
        home = conf.read(self.settings).text('region')
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps({'deployment_id': 'old', 'regions': [
            {'region': home}, {'region': 'us-west-2', 'stack': 'old-stack'},
        ]}))
        execute = mock.Mock(side_effect=AssertionError('The stale record must not authorize an AWS call'))
        api = AwsCli('fixture', home, 1, execute=execute)
        admin = cast(admin_secrets.AdminSecretSession, mock.create_autospec(admin_secrets.AdminSecretSession, instance=True))
        removal = teardown.TargetRemoval(self.settings, self.state, Path(), admin, api, execute)
        removal.identity = replace(removal.identity, deployment_id='current')
        with self.assertRaisesRegex(ValueError, 'gate record differs from the verified deployment'):
            _ = removal.regional_gates()
        execute.assert_not_called()

    def check_bucket_removal(self, *, home_only: bool, late_write: bool = False) -> None:
        home = conf.read(self.settings).text('region')
        west = 'us-west-2'
        deployment = 'a' * 32
        self.configure_gate(f'regions={home} {west}')
        names = {home: 'fixture-home', west: 'fixture-west'}
        buckets = {region: gate_source.bucket_name(deployment, region) for region in names}
        regions = [{'region': home, 'source_bucket': buckets[home]}]
        if home_only:
            _ = (self.state / 'regional-stacks.json').write_text(json.dumps({west: names[west]}))
        else:
            regions.append({'region': west, 'source_bucket': buckets[west], 'stack': names[west]})
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps({'deployment_id': deployment, 'regions': regions}))
        contents = {region: {'base/current.json', 'base/generation/base.bundle', 'delta/generation/delta.bundle'}
                    for region in names}
        deleted: list[str] = []
        failed: set[str] = set()
        operations: list[tuple[str, str]] = []

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            region = arguments[arguments.index('--region') + 1]
            operation = arguments[6]
            operations.append((region, operation))
            reply: dict[str, object] = {}
            if operation == 'describe-instances':
                reply = {'Reservations': []}
            elif operation == 'describe-stacks':
                if region in deleted:
                    return subprocess.CompletedProcess(arguments, 1, '', 'ValidationError: Stack does not exist')
                reply = {'Stacks': [{'StackName': names[region], 'StackId': names[region], 'Outputs': [
                    {'OutputKey': 'GateSourceBucket', 'OutputValue': buckets[region]},
                ], 'StackStatus': 'DELETE_FAILED' if region in failed else 'CREATE_COMPLETE'}]}
            elif operation == 'describe-stack-events':
                self.assertIn(region, failed)
                reply = {'StackEvents': [
                    {'LogicalResourceId': 'GateSourceBucket', 'ResourceStatus': 'DELETE_FAILED',
                     'ResourceStatusReason': 'The bucket you tried to delete is not empty'},
                ]}
            elif operation == 'list-objects-v2':
                self.assertEqual(arguments[arguments.index('--bucket') + 1], buckets[region])
                if contents[region]:
                    reply = {'Contents': [{'Key': key} for key in sorted(contents[region])]}
            elif operation == 'delete-objects':
                self.assertEqual(arguments[arguments.index('--bucket') + 1], buckets[region])
                contents[region].clear()
            elif operation == 'delete-stack':
                self.assertFalse(contents[region])
                if late_write and region not in failed:
                    contents[region].add('delta/generation/late.bundle')
                    failed.add(region)
                else:
                    failed.discard(region)
                    deleted.append(region)
            elif operation == 'wait':
                if region in failed:
                    return subprocess.CompletedProcess(arguments, 255, '', 'Waiter reached DELETE_FAILED')
            else:
                raise AssertionError(arguments)
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        api = AwsCli('fixture', home, 1, execute=execute)
        admin = cast(admin_secrets.AdminSecretSession, mock.create_autospec(admin_secrets.AdminSecretSession, instance=True))
        removal = teardown.TargetRemoval(self.settings, self.state, Path(), admin, api, execute)
        removal.identity = replace(removal.identity, stack_id=names[home], deployment_id=deployment)
        self.assertTrue(removal.regional_gates())
        self.assertTrue(removal.foundation())
        self.assertEqual(deleted, [west, home])
        tries = 2 if late_write else 1
        self.assertEqual([(region, operation) for region, operation in operations
                          if operation in ('delete-objects', 'delete-stack')],
            ([(west, 'delete-objects'), (west, 'delete-stack')] * tries
             + [(home, 'delete-objects'), (home, 'delete-stack')] * tries))

    def test_down_empties_the_home_and_regional_source_buckets_before_stack_deletion(self) -> None:
        self.check_bucket_removal(home_only=False)

    def test_down_empties_the_saved_regional_stack_after_a_home_only_manifest(self) -> None:
        self.check_bucket_removal(home_only=True)

    def test_down_retries_home_and_regional_stack_deletion_after_a_late_source_write(self) -> None:
        self.check_bucket_removal(home_only=False, late_write=True)

    def test_source_bucket_removal_uses_at_most_500_keys_per_batch(self) -> None:
        region = conf.read(self.settings).text('region')
        deployment = 'a' * 32
        bucket = gate_source.bucket_name(deployment, region)
        stack: dict[str, object] = {'Outputs': [{'OutputKey': 'GateSourceBucket', 'OutputValue': bucket}]}
        contents = [f'delta/g{index:020d}-{"a" * 32}/{"b" * 64}.bundle' for index in range(1001)]
        sizes: list[int] = []

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
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

        teardown.empty_gate_source_bucket(AwsCli('fixture', region, 1, execute=execute), stack, deployment)
        self.assertEqual(sizes, [500, 500, 1])
        self.assertFalse(contents)

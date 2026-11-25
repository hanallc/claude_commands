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

    def check_bucket_removal(self, *, home_only: bool, late_write: bool = False, resume_delete: bool = False,
                             request_bucket: bool = False) -> None:
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
        contents = {bucket: {'base/current.json', 'base/generation/base.bundle', 'delta/generation/delta.bundle'}
                    for bucket in buckets.values()}
        requests = f'cloud-claude-gate-{deployment}'
        if request_bucket:
            contents[requests] = {'requests/fixture.json', 'artifacts/fixture/log'}
        deleted: list[str] = []
        failed: dict[str, str] = {}
        operations: list[tuple[str, str]] = []
        emptied: list[str] = []
        resuming = resume_delete

        def execute(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            nonlocal resuming
            region = arguments[arguments.index('--region') + 1]
            operation = arguments[6]
            operations.append((region, operation))
            reply: dict[str, object] = {}
            if operation == 'describe-instances':
                reply = {'Reservations': []}
            elif operation == 'describe-stacks':
                if region in deleted:
                    return subprocess.CompletedProcess(arguments, 1, '', 'ValidationError: Stack does not exist')
                stack: dict[str, object] = {'StackName': names[region], 'StackId': names[region]}
                if region == west and resuming:
                    stack['StackStatus'] = 'DELETE_IN_PROGRESS'
                else:
                    outputs = [{'OutputKey': 'GateSourceBucket', 'OutputValue': buckets[region]}]
                    if region == home and request_bucket:
                        outputs.append({'OutputKey': 'GateBucket', 'OutputValue': requests})
                    stack.update(Outputs=outputs, StackStatus='DELETE_FAILED' if region in failed else 'CREATE_COMPLETE')
                reply = {'Stacks': [stack]}
            elif operation == 'describe-stack-events':
                self.assertIn(region, failed)
                reply = {'StackEvents': [
                    {'LogicalResourceId': failed[region], 'ResourceStatus': 'DELETE_FAILED',
                     'ResourceStatusReason': 'The bucket you tried to delete is not empty'},
                ]}
            elif operation == 'list-objects-v2':
                bucket = arguments[arguments.index('--bucket') + 1]
                self.assertIn(bucket, (buckets[region], requests) if region == home and request_bucket else (buckets[region],))
                if contents[bucket]:
                    reply = {'Contents': [{'Key': key} for key in sorted(contents[bucket])]}
            elif operation == 'delete-objects':
                bucket = arguments[arguments.index('--bucket') + 1]
                contents[bucket].clear()
                emptied.append(bucket)
            elif operation == 'delete-stack':
                self.assertFalse(contents[buckets[region]])
                if region == home and request_bucket:
                    self.assertFalse(contents[requests])
                if late_write and region not in failed:
                    if region == home and request_bucket:
                        contents[requests].add('requests/late.json')
                        failed[region] = 'GateBucket'
                    else:
                        contents[buckets[region]].add('delta/generation/late.bundle')
                        failed[region] = 'GateSourceBucket'
                else:
                    _ = failed.pop(region, None)
                    deleted.append(region)
            elif operation == 'wait':
                if region == west and resuming:
                    resuming = False
                    failed[region] = 'GateSourceBucket'
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
        west_tries = 2 if late_write and not resume_delete else 1
        home_tries = 2 if late_write else 1
        self.assertEqual([region for region, operation in operations if operation == 'delete-stack'],
                         [west] * west_tries + [home] * home_tries)
        home_buckets = [buckets[home], requests, requests] if request_bucket else [buckets[home]] * home_tries
        self.assertEqual(emptied, [buckets[west]] * west_tries + home_buckets)

    def test_down_empties_the_home_and_regional_source_buckets_before_stack_deletion(self) -> None:
        self.check_bucket_removal(home_only=False)

    def test_down_empties_the_saved_regional_stack_after_a_home_only_manifest(self) -> None:
        self.check_bucket_removal(home_only=True)

    def test_down_retries_home_and_regional_stack_deletion_after_a_late_source_write(self) -> None:
        self.check_bucket_removal(home_only=False, late_write=True)

    def test_down_reads_fresh_outputs_after_a_resumed_delete_fails(self) -> None:
        self.check_bucket_removal(home_only=False, resume_delete=True)

    def test_down_empties_the_request_bucket_and_retries_after_a_late_request(self) -> None:
        self.check_bucket_removal(home_only=False, late_write=True, request_bucket=True)

    def test_source_bucket_removal_uses_at_most_500_keys_per_batch(self) -> None:
        region = conf.read(self.settings).text('region')
        deployment = 'a' * 32
        bucket = gate_source.bucket_name(deployment, region)
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

        teardown.empty_gate_bucket(AwsCli('fixture', region, 1, execute=execute), bucket)
        self.assertEqual(sizes, [500, 500, 1])
        self.assertFalse(contents)

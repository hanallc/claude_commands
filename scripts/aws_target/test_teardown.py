"""Test source bucket removal with a local AWS transport."""
from collections.abc import Sequence
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from unittest import mock
from typing import cast

from scripts.aws_target import admin_secrets, gate_source, teardown
from scripts.aws_target.aws_cli import AwsCli
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.lib import conf


class SourceBucketRemovalTests(CostFixture):
    def test_down_empties_the_home_and_regional_source_buckets_before_stack_deletion(self) -> None:
        home = conf.read(self.settings).text('region')
        west = 'us-west-2'
        deployment = 'a' * 32
        self.configure_gate(f'regions={home} {west}')
        names = {home: 'fixture-home', west: 'fixture-west'}
        buckets = {region: gate_source.bucket_name(deployment, region) for region in names}
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps({'deployment_id': deployment, 'regions': [
            {'region': home, 'source_bucket': buckets[home]},
            {'region': west, 'source_bucket': buckets[west], 'stack': names[west]},
        ]}))
        contents = {region: {'base/current.json', 'base/generation/base.bundle', 'delta/generation/delta.bundle'}
                    for region in names}
        deleted: list[str] = []
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
                ]}]}
            elif operation == 'list-objects-v2':
                self.assertEqual(arguments[arguments.index('--bucket') + 1], buckets[region])
                if contents[region]:
                    reply = {'Contents': [{'Key': key} for key in sorted(contents[region])]}
            elif operation == 'delete-objects':
                self.assertEqual(arguments[arguments.index('--bucket') + 1], buckets[region])
                contents[region].clear()
            elif operation == 'delete-stack':
                self.assertFalse(contents[region])
                deleted.append(region)
            elif operation != 'wait':
                raise AssertionError(arguments)
            return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

        api = AwsCli('fixture', home, 1, execute=execute)
        admin = cast(admin_secrets.AdminSecretSession, mock.create_autospec(admin_secrets.AdminSecretSession, instance=True))
        removal = teardown.TargetRemoval(self.settings, self.state, Path(), admin, api, execute)
        removal.identity = replace(removal.identity, stack_id=names[home], deployment_id=deployment)
        self.assertTrue(removal.regional_gates())
        self.assertTrue(removal.foundation())
        self.assertEqual(deleted, [west, home])
        self.assertEqual([(region, operation) for region, operation in operations
                          if operation in ('delete-objects', 'delete-stack')],
                         [(west, 'delete-objects'), (west, 'delete-stack'),
                          (home, 'delete-objects'), (home, 'delete-stack')])

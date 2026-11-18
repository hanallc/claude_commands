"""Check conditional source uploads and the deployment's S3 source policy."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import json
import os
import subprocess
import tempfile
from typing import cast
import unittest
from unittest import mock

from scripts.aws_target import hana_provider, hana_transport
from scripts.aws_target.aws_cli import AwsCli
from scripts.lib import conf
from scripts.lib.build_slots_test_support import private_slot_config


class HanaTransportPolicyTests(unittest.TestCase):
    def test_source_upload_signing_and_policy_use_scoped_provider_operations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = private_slot_config(root, repository='hana')
            with mock.patch.dict(os.environ, private.environment):
                configuration = conf.read(Path(private.environment['CONF_DIR']) / 'hana_aws.conf')
                calls: list[tuple[str, float]] = []
                objects: dict[str, bytes] = {}
                def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                            check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    _ = capture_output, text, check, env
                    operation = arguments[6]
                    calls.append((operation, timeout))
                    reply: dict[str, str] = {}
                    if operation == 'put-object':
                        key = arguments[arguments.index('--key') + 1]
                        self.assertEqual(arguments[arguments.index('--if-none-match') + 1], '*')
                        if key in objects:
                            return subprocess.CompletedProcess(arguments, 1, '', 'An error occurred (PreconditionFailed)')
                        objects[key] = Path(arguments[arguments.index('--body') + 1]).read_bytes()
                    elif operation == 'export-credentials':
                        reply = {'AccessKeyId': 'fixture-access', 'SecretAccessKey': 'fixture-secret'}
                    else:
                        self.fail('unexpected source AWS operation')
                    return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')
                provider = hana_provider.AwsHanaProvider(AwsCli('fixture-profile', 'us-east-2', 999, execute),
                    root, 'fixture-bucket', 1)
                transport = hana_transport.AwsSourceObjects(provider, configuration, 'fixture-deployment', clock=lambda: 1)
                payload = root / 'bundle'
                _ = payload.write_bytes(b'fixture bundle')
                digest = 'a' * 64
                transport.put_if_absent(digest, payload)
                _ = payload.write_bytes(b'changed fixture bundle')
                transport.put_if_absent(digest, payload)
                self.assertEqual(objects['source/fixture-deployment/' + digest], b'fixture bundle')
                url = transport.signed_get(digest)
                self.assertIn('/source/fixture-deployment/' + digest, url)
                self.assertTrue(all(timeout == configuration.whole('aws_timeout_s') for _, timeout in calls))
                actions = {hana_provider.PROVIDER_OPERATIONS[operation] for operation, _ in calls
                           if operation != 'export-credentials'}
                actions.add(hana_provider.PROVIDER_OPERATIONS['get-object'])
                template_path = Path(__file__).resolve().parents[2] / 'docs/plans/builds-aws/hana-cloudformation.json'
                template = cast(dict[str, object], json.loads(template_path.read_text()))
                resources = cast(dict[str, dict[str, object]], template['Resources'])
                properties = cast(dict[str, object], resources['ScalerPolicy']['Properties'])
                policy = cast(dict[str, object], properties['PolicyDocument'])
                statements = cast(list[dict[str, object]], policy['Statement'])
                source_rows = [row for row in statements if '/source/' in json.dumps(row.get('Resource'))]
                self.assertEqual(len(source_rows), 1)
                self.assertEqual(set(cast(list[str], source_rows[0]['Action'])), actions)
                self.assertEqual(source_rows[0]['Resource'], {'Fn::Sub': '${ControlBucket.Arn}/source/${DeploymentId}/*'})
                self.assertNotIn('s3:ListBucket', actions)

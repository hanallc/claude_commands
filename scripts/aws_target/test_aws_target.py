"""Test AWS CLI error codes and the prices that the AWS page shows."""
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
import json
import subprocess
import unittest

from scripts.aws_target import aws_target, ledger, spot_table
from scripts.aws_target.aws_cli import AwsUnavailable, aws_error_code
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.lib import conf
from scripts.lint.memory_test_support import write_config_fixture


class AwsErrorCodeTests(unittest.TestCase):
    def test_aws_error_code_reads_named_service_code(self) -> None:
        error = AwsUnavailable('aws: [ERROR]: An error occurred (InvalidInstance.NotFound) '
                               + 'when calling the DescribeInstances operation: fixture')
        self.assertEqual(aws_error_code(error), 'InvalidInstance.NotFound')

    def test_aws_error_code_accepts_http_status_only_when_enabled(self) -> None:
        for status, expected in (('99', ''), ('100', '100'), ('404', '404'), ('599', '599'), ('600', '')):
            with self.subTest(status=status):
                error = AwsUnavailable(f'An error occurred ({status}) when calling the GetObject operation: fixture')
                self.assertEqual(aws_error_code(error), '')
                self.assertEqual(aws_error_code(error, allow_status=True), expected)
                self.assertEqual(aws_error_code(error, allow_status=True, first_attempt_of='GetObject'), '')

    def test_aws_error_code_requires_the_expected_operation(self) -> None:
        for code in ('AccessDenied', '403'):
            with self.subTest(code=code):
                error = AwsUnavailable(f'An error occurred ({code}) when calling the GetObject operation: fixture')
                self.assertEqual(aws_error_code(error, allow_status=True, operation='GetObject'), code)
                self.assertEqual(aws_error_code(error, allow_status=True, operation='ListObjectsV2'), '')

    def test_aws_error_code_requires_zero_retries_for_first_attempt(self) -> None:
        for retries, expected in (('', 'UnauthorizedOperation'), (' (reached max retries: 0)', 'UnauthorizedOperation'),
                                  (' (reached max retries: 1)', '')):
            with self.subTest(retries=retries):
                error = AwsUnavailable('An error occurred (UnauthorizedOperation) when calling the '
                                       + f'RunInstances operation{retries}: fixture')
                self.assertEqual(aws_error_code(error, operation='RunInstances'), 'UnauthorizedOperation')
                self.assertEqual(aws_error_code(error, first_attempt_of='RunInstances'), expected)
                self.assertEqual(aws_error_code(error, first_attempt_of='ExecuteChangeSet'), '')


class HanaGpuSnapshotTests(CostFixture):
    def test_snapshot_reads_and_displays_hana_gpu_prices(self) -> None:
        _ = write_config_fixture(conf.path('cloud_gate'), 'spot_candidate_regions=us-east-1\n'
                                  + 'gpu_instance_types=g6.2xlarge\n'
                                  + 'hana_gpu_instance_types=g4dn.2xlarge g6.2xlarge')
        now = 1768478400
        reads: list[str] = []

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            del capture_output, text, timeout, check, env
            if 'describe-spot-price-history' in arguments:
                sizes = arguments[arguments.index('--instance-types') + 1:arguments.index('--product-descriptions')]
                reads.extend(sizes)
                return subprocess.CompletedProcess(arguments, 0, json.dumps({'SpotPriceHistory': [
                    {'InstanceType': size, 'AvailabilityZone': 'us-east-1a', 'SpotPrice': '0.52',
                     'Timestamp': datetime.fromtimestamp(now, UTC).isoformat()} for size in sizes]}), '')
            self.assertIn('get-service-quota', arguments)
            return subprocess.CompletedProcess(arguments, 0, '{"Quota":{"Value":8}}', '')

        status = aws_target.snapshot(self.settings, self.state, execute=execute, clock=lambda: now)
        self.assertEqual(status, 0)
        self.assertEqual(reads.count('g6.2xlarge'), 1)
        self.assertEqual(reads.count('g4dn.2xlarge'), 1)
        prices = ledger.read_spot_prices(self.state)
        self.assertEqual({price.usd_per_hour for price in prices if price.instance_type == 'g4dn.2xlarge'}, {Decimal('.52')})
        _, gpu = spot_table.spot_tables(self.state, now, settings_path=self.settings).prices
        self.assertEqual(gpu.rows[0][gpu.headers.index('g4dn.2xlarge')], '0.5200')

"""Test the prices that the hourly snapshot supplies to the AWS page."""
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
import json
import subprocess

from scripts.aws_target import aws_target, ledger, spot_table
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.lib import conf
from scripts.lint.memory_test_support import write_config_fixture


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

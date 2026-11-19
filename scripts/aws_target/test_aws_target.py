"""Test AWS CLI error codes and the prices that the AWS page shows."""
from collections.abc import Sequence
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
from typing import override
import unittest
from unittest import mock

from scripts.aws_target import aws_target, ledger, run_cost, spot_table
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


class SpotPlacementTests(CostFixture):
    now: int = 1768478400
    regions: tuple[str, ...] = tuple(f'fixture-region-{index}' for index in range(19))
    types: tuple[str, ...] = ('c7a.4xlarge', 'c7a.8xlarge', 'm7a.4xlarge', 'm7a.8xlarge')

    def __init__(self, methodName: str = 'runTest') -> None:
        super().__init__(methodName)
        self.credential_file: Path = Path()
        self.calls: list[tuple[str, ...]] = []
        self.refuse_zonal: bool = False

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = write_config_fixture(conf.path('cloud_gate'), 'regions=' + ' '.join(self.regions) + '\ninstance_types='
                                 + ' '.join(self.types))
        self.credential_file = self.root / '.config/aws-build/credentials'
        self.credential_file.parent.mkdir(parents=True)
        self.credential_file.touch()

    def execute(self, arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        del capture_output, text, timeout, check
        assert env is not None
        self.assertEqual(env['AWS_SHARED_CREDENTIALS_FILE'], str(self.credential_file))
        self.assertEqual(env['AWS_MAX_ATTEMPTS'], '1')
        self.assertNotIn('AWS_ACCESS_KEY_ID', env)
        self.assertIn('get-spot-placement-scores', arguments)
        self.assertEqual(arguments[arguments.index('--target-capacity-unit-type') + 1], 'units')
        self.calls.append(tuple(arguments))
        zonal = '--single-availability-zone' in arguments
        region = arguments[arguments.index('--region-names') + 1]
        if zonal and self.refuse_zonal and region == self.regions[0]:
            return subprocess.CompletedProcess(arguments, 255, '', 'An error occurred (MaxConfigLimitExceededException) '
                                               + 'when calling the GetSpotPlacementScores operation: fixture')
        score: dict[str, object] = {'Region': region, 'Score': 7}
        if zonal:
            score['AvailabilityZoneId'] = 'fixture-az1'
        return subprocess.CompletedProcess(arguments, 0, json.dumps({'SpotPlacementScores': [score]}), '')

    def collect_at(self, now: int) -> None:
        with mock.patch.dict(os.environ, {'AWS_ACCESS_KEY_ID': 'fixture-other-key', 'AWS_MAX_ATTEMPTS': '5'}):
            aws_target.placement_snapshot(self.settings, self.state, execute=self.execute, clock=lambda: now)

    def test_collection_prioritizes_shapes_and_adds_one_accepted_shape_per_hour(self) -> None:
        regional, zonal = ledger.SpotPlacementScope.REGION, ledger.SpotPlacementScope.ZONE
        pair, all_types = ledger.SpotPlacementTypeSet.GATE_32_VCPU, ledger.SpotPlacementTypeSet.ALL_GATE_TYPES
        gate_32_vcpu = ('c7a.8xlarge', 'm7a.8xlarge')
        shapes = (
            (regional, pair, 8, gate_32_vcpu),
            (regional, all_types, 8, self.types),
            (zonal, pair, 8, gate_32_vcpu),
            (zonal, all_types, 8, self.types),
            (regional, all_types, 20, self.types),
        )
        expected = tuple(ledger.SpotPlacementRequest(scope, type_set, boxes, types, batch)
                         for scope, type_set, boxes, types in shapes for batch in (self.regions[:10], self.regions[10:]))
        self.assertEqual(aws_target.spot_placement_requests(conf.read_or_repository('cloud_gate')), expected)
        request_fields = [(request.boxes, request.scope is zonal, request.instance_types, request.regions)
                          for request in expected]
        for hour in range(168):
            with self.subTest(hour=hour):
                self.calls.clear()
                self.collect_at(self.now + 3600 * hour)
                facts: list[tuple[int, bool, tuple[str, ...], tuple[str, ...]]] = []
                for call in self.calls:
                    types = call[call.index('--instance-types') + 1:call.index('--target-capacity')]
                    regions = call[call.index('--region-names') + 1:call.index('--output')]
                    facts.append((int(call[call.index('--target-capacity') + 1]),
                                  '--single-availability-zone' in call, types, regions))
                self.assertEqual(facts, request_fields[:4 + 2 * hour])

    def test_configuration_limit_counts_recent_failed_bodies_and_leaves_batches_together(self) -> None:
        first = self.now - 24 * 3600
        requests = aws_target.spot_placement_requests(conf.read_or_repository('cloud_gate'))
        history = tuple(ledger.SpotPlacementSample(request, self.now - 3600, ()) for request in requests[:4])
        failures = (ledger.SpotPlacementFailure(replace(requests[0], boxes=1), first + 2, 'refused'),
                    *(ledger.SpotPlacementFailure(replace(requests[0], boxes=boxes), first + 3600 + boxes, 'refused')
                      for boxes in range(2, 7)))
        ledger.write_spot_placement_scores(self.state, (*history, *failures), self.now)

        self.collect_at(self.now + 1)
        self.assertEqual(len(self.calls), 4)

        # One expired body leaves one slot. The next shape needs two slots.
        self.calls.clear()
        self.collect_at(self.now + 3601)
        self.assertEqual(len(self.calls), 4)

        self.calls.clear()
        self.collect_at(self.now + 7200)
        self.assertEqual(len(self.calls), 6)
        self.assertTrue(all('--single-availability-zone' in call for call in self.calls[-2:]))

    def test_refusals_pause_growth_until_the_next_hour_and_keep_partial_scores(self) -> None:
        self.collect_at(self.now)
        self.refuse_zonal = True
        self.collect_at(self.now + 3600)
        self.collect_at(self.now + 3601)
        self.assertEqual(len(self.calls), 10)
        *samples, failed, second = ledger.read_spot_placement_scores(self.state)
        self.assertEqual(len(samples), 8)
        self.assertTrue(all(isinstance(row, ledger.SpotPlacementSample) and len(row.scores) == 1 for row in samples))
        assert isinstance(failed, ledger.SpotPlacementFailure)
        self.assertEqual(failed.reason, 'MaxConfigLimitExceededException')
        self.assertEqual(failed.request.scope, ledger.SpotPlacementScope.ZONE)
        self.assertEqual(failed.request.regions, self.regions[:10])
        assert isinstance(second, ledger.SpotPlacementSample)
        self.assertEqual(second.request.regions, self.regions[10:])
        self.assertEqual(len(second.scores), 1)
        self.collect_at(self.now + 7200)
        self.assertEqual(self.calls[-2:], self.calls[-8:-6])
        self.assertEqual(len(self.calls), 16)
        self.refuse_zonal = False
        self.collect_at(self.now + 10800)
        self.assertEqual(len(self.calls), 22)
        recovered = ledger.read_spot_placement_scores(self.state)[-2]
        assert isinstance(recovered, ledger.SpotPlacementSample)
        self.assertEqual(recovered.scores, (ledger.SpotPlacementScore(
            ledger.SpotPlacementZone(self.regions[0], 'fixture-az1'), 7),))
        self.collect_at(self.now + 14400)
        self.assertEqual(len(self.calls), 30)

    def test_collection_replaces_unreadable_history(self) -> None:
        _ = (self.state / ledger.SPOT_PLACEMENT_FILE).write_text('{')
        errors = StringIO()
        with redirect_stderr(errors):
            self.collect_at(self.now)
        self.assertEqual(errors.getvalue().splitlines(), ['aws_target: Spot placement history unreadable: JSONDecodeError'])
        self.assertEqual(len(self.calls), 4)
        saved = ledger.read_spot_placement_scores(self.state)
        self.assertEqual(len(saved), 4)
        self.assertTrue(all(isinstance(row, ledger.SpotPlacementSample) for row in saved))

    def test_hourly_collects_placement_when_the_hours_gap_file_is_unreadable(self) -> None:
        _ = (self.state / 'hours-gap-notices.json').write_text('{')
        collect = partial(aws_target.placement_snapshot, execute=self.execute, clock=lambda: self.now)
        with (mock.patch.object(aws_target, 'snapshot', return_value=0) as prices,
              mock.patch.object(run_cost, 'day_hours_checks', return_value={}),
              mock.patch.object(aws_target, 'placement_snapshot', side_effect=collect),
              redirect_stderr(StringIO()) as errors):
            self.assertEqual(aws_target.hourly(self.settings, self.state), 1)
        prices.assert_called_once_with(self.settings, self.state)
        self.assertIn('hours-gap check failed: JSONDecodeError', errors.getvalue())
        self.assertEqual(len(self.calls), 4)

    def test_hourly_checks_hours_and_placement_when_the_price_snapshot_raises(self) -> None:
        with (mock.patch.object(aws_target, 'snapshot', side_effect=OSError('price fixture')),
              mock.patch.object(aws_target, 'hours_gap_notices', return_value=2) as hours,
              mock.patch.object(aws_target, 'placement_snapshot', side_effect=OSError('placement fixture')) as placement,
              redirect_stderr(StringIO()) as errors):
            self.assertEqual(aws_target.hourly(self.settings, self.state), 2)
        hours.assert_called_once_with(self.settings, self.state)
        placement.assert_called_once_with(self.settings, self.state)
        self.assertIn('price snapshot failed: OSError', errors.getvalue())
        self.assertIn('placement snapshot failed: OSError', errors.getvalue())

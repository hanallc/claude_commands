"""Test hourly Spot snapshots with private files and fake AWS reads."""
from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO
import fcntl
import json
import shutil
import os
import subprocess
import sys
from typing import cast, override
import unittest
from unittest import mock

from scripts.review_links.test_page_frame import assert_status_states
from scripts.aws_target import admin_secrets, aws_page, aws_target, deploy, ledger, run_cost, spend, spot_table
from scripts.aws_target import config as aws_config
from scripts.aws_target import plan_identity, quota_requests
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.ledger_test_support import CostFixture, text_setting
from scripts.buildlog import aws_report
from scripts.lib import atomic_file, conf
from scripts.message import send_budget
from scripts.notify import push_texts
from scripts.review_links import console_theme, review_link


def table_texts(tables: spot_table.SpotTables) -> tuple[str, ...]:
    """Every snapshot line, then every title, header and cell of the tables, for text checks."""
    every = (*tables.prices, *tables.quotas, *(table for costs in tables.runs
             for table in (*costs.tables, costs.gate_prices, costs.refused, costs.launched)), tables.hours)
    return (*(text for _region, text in tables.snapshot_rows),
            *(text for table in every for text in (table.title, *table.headers, *(cell for row in table.rows for cell in row))))


class ControlTests(CostFixture):
    clock: TestClock = TestClock()
    calls: list[tuple[str, ...]] = []
    regions: tuple[str, ...] = ()
    sizes: tuple[str, ...] = ()
    standard: tuple[str, ...] = ()
    compare: tuple[str, ...] = ()
    gpu: tuple[str, ...] = ()
    failure: str = ''
    failure_reason: str = ''
    regional_failure_reasons: dict[str, str] = {}
    omitted: str = ''
    quota: int = 0
    notices: list[tuple[str, push_texts.RenderedPush, float]] = []
    notice_accepted: bool = True

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        self.calls = []
        self.failure = ''
        self.notices = []
        self.regional_failure_reasons = {}
        self.notice_accepted = True
        self.failure_reason = 'AccessDenied: account 123456789012'
        self.omitted = ''
        values_path = self.root / 'cloud_gate.conf'
        self.configure_gate("""spot_candidate_regions=us-east-1 us-west-1 us-east-1
instance_types=c7a.4xlarge m7a.2xlarge
price_compare_instance_types=m7a.8xlarge c7a.4xlarge
gpu_instance_types=g6.2xlarge g4dn.xlarge
hana_gpu_instance_types=g6.2xlarge g4dn.2xlarge
gate_wait_usd_per_minute=0
region_transfer_usd={"us-west-1":0.00015}
""")
        values = conf.read(values_path)
        self.regions = tuple(dict.fromkeys(values.words('spot_candidate_regions')))
        self.standard = values.words('instance_types')
        self.compare = tuple(size for size in values.words('price_compare_instance_types') if size not in self.standard)
        self.gpu = ('g6.2xlarge', 'g4dn.xlarge', 'g4dn.2xlarge')
        self.sizes = (*self.standard, *self.compare, *self.gpu)
        self.quota = deploy.largest_gate_vcpus(self.sizes) * 3 + 1
        self.enterContext(mock.patch.dict(os.environ, {
            'AWS_ACCESS_KEY_ID': 'fixture-admin-id', 'AWS_SECRET_ACCESS_KEY': 'fixture-admin-secret',
            'AWS_SESSION_TOKEN': 'fixture-token', 'AWS_CONFIG_FILE': '/fixture/admin-config',
            'AWS_PROFILE': 'fixture-admin', 'AWS_DEFAULT_PROFILE': 'fixture-admin',
            'AWS_SHARED_CREDENTIALS_FILE': str(self.root / 'private-scaler'),
        }))
        _ = self.enterContext(mock.patch.object(plan_identity, 'plan_credentials',
                                                side_effect=plan_identity.PlanIdentityRefused('The AWS plan key is missing.')))

    def execute(self, arguments: Sequence[str], *, capture_output: bool, text: bool,
                timeout: float, check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        self.assertTrue(capture_output and text)
        self.assertFalse(check)
        configuration = conf.read(self.settings)
        self.assertEqual(timeout, configuration.number('aws_timeout_s'))
        self.assertEqual(arguments[arguments.index('--profile') + 1], configuration.text('credentials_profile'))
        self.assertIsNotNone(env)
        environment = cast(dict[str, str], env)
        self.assertEqual(environment['AWS_SHARED_CREDENTIALS_FILE'], str(self.root / 'private-scaler'))
        self.assertEqual(environment['AWS_CONFIG_FILE'], os.devnull)
        for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE', 'AWS_DEFAULT_PROFILE'):
            self.assertNotIn(key, environment)
        region = arguments[arguments.index('--region') + 1]
        self.calls.append(tuple(arguments))
        operation = arguments[6]
        self.assertIn(operation, ('describe-spot-price-history', 'get-service-quota'))
        self.assertNotIn('--no-paginate', arguments)
        self.assertNotIn('--max-items', arguments)
        if region == self.regions[0] and self.failure == 'timeout':
            raise subprocess.TimeoutExpired('fixture price read', timeout)
        if self.failure == 'all' or region == self.regions[0] and (self.failure == operation or
                self.failure == 'gpu-quota' and operation == 'get-service-quota' and 'L-3819A6DF' in arguments):
            return subprocess.CompletedProcess(arguments, 1, '', self.regional_failure_reasons.get(region, self.failure_reason))
        reply: dict[str, object]
        if operation == 'get-service-quota':
            code = arguments[arguments.index('--quota-code') + 1]
            self.assertIn(code, ('L-34B43A08', 'L-3819A6DF'))
            reply = {'Quota': {'Value': self.quota if code == 'L-34B43A08' else self.quota - 1}}
        else:
            self.assertEqual(tuple(arguments[arguments.index('--instance-types') + 1:arguments.index('--product-descriptions')]), self.sizes)
            stamp = datetime.fromtimestamp(self.clock.now, UTC).isoformat()
            rows = [dict(InstanceType=size, AvailabilityZone=region + zone, SpotPrice=price, Timestamp=stamp)
                    for size in self.sizes if size != self.omitted for zone, price in (('a', '.25'), ('b', '.3'))]
            rows.append(dict(InstanceType=self.sizes[0], AvailabilityZone=region + 'a', SpotPrice='.9',
                             Timestamp=datetime.fromtimestamp(self.clock.now - 1, UTC).isoformat()))
            reply = {'SpotPriceHistory': rows}
            if region == self.regions[0] and self.failure == 'pagination':
                reply['NextToken'] = 'fixture-page'
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

    def notify(self, key: str, text: push_texts.RenderedPush, timeout: float) -> bool:
        saved = aws_target.read_snapshot(self.state)
        self.assertTrue(aws_target.snapshot_failures(saved))
        self.assertTrue(ledger.mapping(saved['failure_streak'])['notice_claimed'])
        self.notices.append((key, text, timeout))
        return self.notice_accepted

    def snapshot(self) -> int:
        return aws_target.snapshot(self.settings, self.state, execute=self.execute, clock=self.clock, notify=self.notify)

    def deploy_gate_zones(self) -> None:
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps({'regions': [
            {'region': region, 'subnets': [{'id': 'subnet-' + region, 'zone': region + 'a'}]} for region in self.regions
        ]}))

    def test_snapshot_saves_each_quota_request_history_with_the_plan_identity(self) -> None:
        calls: list[tuple[str, str]] = []
        credential = mock.Mock(spec=plan_identity.PlanCredentials, environment=mock.Mock(
            return_value={'AWS_FIXTURE_IDENTITY': 'plan'}))
        expected_timeout = conf.read(self.settings).number('aws_timeout_s')

        def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                    check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if 'list-requested-service-quota-change-history-by-quota' not in arguments:
                return self.execute(arguments, capture_output=capture_output, text=text, timeout=timeout,
                                    check=check, env=env)
            self.assertEqual(env, {'AWS_FIXTURE_IDENTITY': 'plan'})
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, expected_timeout)
            self.assertNotIn('--profile', arguments)
            self.assertNotIn('--no-paginate', arguments)
            region = arguments[arguments.index('--region') + 1]
            code = arguments[arguments.index('--quota-code') + 1]
            calls.append((region, code))
            if code == 'L-34B43A08' and region == self.regions[0]:
                history = [{'Status': 'APPROVED', 'DesiredValue': 16, 'Created': '2026-01-01T03:00:00Z',
                            'LastUpdated': '2026-01-04T05:00:00Z'},
                           {'Status': 'CASE_OPENED', 'DesiredValue': 32, 'Created': '2026-01-02T03:00:00Z',
                            'LastUpdated': '2026-01-03T04:00:00Z'}]
            elif code == 'L-34B43A08':
                history = [{'Status': 'arn:aws:iam::123456789012:user/private', 'DesiredValue': 64,
                            'LastUpdated': '2026-01-03T05:00:00+01:00'}]
            elif region == self.regions[0]:
                history = []
            else:
                return subprocess.CompletedProcess(arguments, 1, '',
                    'An error occurred (AccessDeniedException) when calling the '
                    + 'ListRequestedServiceQuotaChangeHistoryByQuota operation: Private error detail')
            return subprocess.CompletedProcess(arguments, 0, json.dumps({'RequestedQuotas': history}), '')

        with mock.patch.object(plan_identity, 'plan_credentials', return_value=credential) as load:
            status = aws_target.snapshot(self.settings, self.state, execute=execute, clock=self.clock,
                                         notify=self.notify)
        load.assert_called_once_with()
        self.assertCountEqual(calls, [(region, code) for region in self.regions for code in ('L-34B43A08', 'L-3819A6DF')])
        document = aws_target.read_snapshot(self.state)
        histories = ledger.rows(document, 'quota_requests')
        standard = {'state': 'requested', 'status': 'CASE_OPENED', 'requested_vcpus': '32',
                    'created_at': '2026-01-02T03:00:00+00:00', 'updated_at': '2026-01-03T04:00:00+00:00'}
        self.assertEqual(histories, (
            {'region': self.regions[0], 'standard': standard, 'gpu': {'state': 'no_request'}},
            {'region': self.regions[1], 'standard': {'state': 'requested', 'status': 'unknown',
                'requested_vcpus': '64', 'updated_at': '2026-01-03T04:00:00+00:00'},
             'gpu': {'state': 'read_failed', 'reason': 'AccessDeniedException'}},
        ))
        self.assertEqual(status, 0)
        self.assertEqual(len(ledger.read_spot_prices(self.state)), len(self.regions) * len(self.sizes) * 2)
        self.assertEqual(len(ledger.rows(document, 'regions')), len(self.regions))
        self.assertEqual(aws_target.snapshot_failures(document), ())
        self.assertEqual(self.notices, [])
        first, second = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings).quota_requests
        self.assertEqual(first.standard, quota_requests.QuotaRequest(quota_requests.QuotaRequestStatus.CASE_OPENED,
            Decimal(32), datetime(2026, 1, 2, 3, tzinfo=UTC), datetime(2026, 1, 3, 4, tzinfo=UTC)))
        self.assertEqual(first.gpu, quota_requests.NoQuotaRequest())
        self.assertEqual(second.standard, quota_requests.QuotaRequest(quota_requests.QuotaRequestStatus.UNKNOWN,
            Decimal(64), quota_requests.QuotaRequestTimeUnavailable(), datetime(2026, 1, 3, 4, tzinfo=UTC)))
        self.assertEqual(second.gpu, quota_requests.QuotaRequestReadFailed('AccessDeniedException'))

    def test_snapshot_publishes_prices_when_a_history_worker_cannot_start(self) -> None:
        credential = mock.Mock(spec=plan_identity.PlanCredentials)
        with (mock.patch.object(plan_identity, 'plan_credentials', return_value=credential),
              mock.patch.object(ThreadPoolExecutor, '_adjust_thread_count',
                                side_effect=RuntimeError('Private thread startup detail'))):
            self.assertEqual(self.snapshot(), 0)
        document = aws_target.read_snapshot(self.state)
        self.assertEqual(len(ledger.rows(document, 'regions')), len(self.regions))
        self.assertEqual(len(ledger.rows(document, 'prices')), len(self.regions) * len(self.sizes) * 2)
        self.assertEqual(ledger.rows(document, 'quota_requests'), tuple(
            {'region': region, 'standard': {'state': 'read_failed', 'reason': 'QuotaHistoryUnavailable'},
             'gpu': {'state': 'read_failed', 'reason': 'QuotaHistoryUnavailable'}} for region in self.regions))

    def test_snapshot_validates_quota_request_values_before_publication(self) -> None:
        credential = mock.Mock(spec=plan_identity.PlanCredentials, environment=mock.Mock(return_value={}))
        rejected = {'state': 'read_failed', 'reason': 'QuotaHistoryUnavailable'}
        requested = {'state': 'requested', 'status': 'PENDING', 'requested_vcpus': '128'}
        rows: tuple[tuple[str, object, dict[str, str]], ...] = (
            ('DesiredValue', '000000000128', rejected),
            ('DesiredValue', True, rejected),
            ('DesiredValue', 128.5, rejected),
            ('DesiredValue', 0, rejected),
            ('DesiredValue', -1, rejected),
            ('DesiredValue', 10_000_000_001, rejected),
            ('DesiredValue', 10_000_000_000, {**requested, 'requested_vcpus': '10000000000'}),
            ('DesiredValue', 128.0, requested),
            ('Created', '000000000128', rejected),
            ('LastUpdated', '000000000128', rejected),
        )
        for field, value, expected in rows:
            with self.subTest(field=field, value=value):
                def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                            check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    if 'list-requested-service-quota-change-history-by-quota' not in arguments:
                        return self.execute(arguments, capture_output=capture_output, text=text,
                                            timeout=timeout, check=check, env=env)
                    history = [{'Status': 'PENDING', 'DesiredValue': 128, field: value}]
                    return subprocess.CompletedProcess(arguments, 0, json.dumps({'RequestedQuotas': history}), '')

                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
                with mock.patch.object(plan_identity, 'plan_credentials', return_value=credential):
                    self.assertEqual(aws_target.snapshot(self.settings, self.state, execute=execute,
                                                         clock=self.clock, notify=self.notify), 0)
                document = aws_target.read_snapshot(self.state)
                self.assertEqual(ledger.rows(document, 'quota_requests'), tuple(
                    {'region': region, 'standard': expected, 'gpu': expected} for region in self.regions))

    def test_snapshot_does_not_retry_prices_for_history_errors(self) -> None:
        credential = mock.Mock(spec=plan_identity.PlanCredentials, environment=mock.Mock(return_value={}))
        failures = (
            subprocess.TimeoutExpired('private command detail', 1),
            RuntimeError('Private executor detail'),
            'An error occurred (PrivateAccount123456789012) when calling the '
            + 'ListRequestedServiceQuotaChangeHistoryByQuota operation: Private error detail',
        )
        for failure in failures:
            with self.subTest(failure=failure):
                def execute(arguments: Sequence[str], *, capture_output: bool, text: bool, timeout: float,
                            check: bool, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
                    if 'list-requested-service-quota-change-history-by-quota' not in arguments:
                        return self.execute(arguments, capture_output=capture_output, text=text,
                                            timeout=timeout, check=check, env=env)
                    if isinstance(failure, str):
                        return subprocess.CompletedProcess(arguments, 1, '', failure)
                    raise failure

                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
                with mock.patch.object(plan_identity, 'plan_credentials', return_value=credential):
                    self.assertEqual(aws_target.snapshot(self.settings, self.state, execute=execute,
                                                         clock=self.clock, notify=self.notify), 0)
                document = aws_target.read_snapshot(self.state)
                self.assertEqual(len(ledger.rows(document, 'regions')), len(self.regions))
                self.assertTrue(all(row.read_at == self.clock.now for row in ledger.read_spot_prices(self.state)))
                self.assertEqual(aws_target.snapshot_failures(document), ())
                self.assertEqual(ledger.rows(document, 'quota_requests'), tuple(
                    {'region': region, 'standard': {'state': 'read_failed', 'reason': 'QuotaHistoryUnavailable'},
                     'gpu': {'state': 'read_failed', 'reason': 'QuotaHistoryUnavailable'}} for region in self.regions))

    def test_snapshot_history_shares_one_deadline_across_quota_calls(self) -> None:
        credential = mock.Mock(spec=plan_identity.PlanCredentials, environment=mock.Mock(return_value={}))
        clock = TestClock()
        timeout_seen: list[float] = []

        def execute(arguments: Sequence[str], *, timeout: float, **_options: object) -> subprocess.CompletedProcess[str]:
            timeout_seen.append(timeout)
            clock.advance(1 if len(timeout_seen) == 1 else 9)
            return subprocess.CompletedProcess(arguments, 0, '{"RequestedQuotas": []}', '')

        with (mock.patch.object(plan_identity, 'plan_credentials', return_value=credential),
              mock.patch.object(aws_target, 'monotonic', clock),
              mock.patch.object(aws_target, 'ThreadPoolExecutor', return_value=ThreadPoolExecutor(max_workers=1))):
            first, second = aws_target.read_snapshot_quota_requests(self.regions, 10, execute)
        self.assertEqual(timeout_seen, [10, 9])
        self.assertEqual(first.standard, quota_requests.NoQuotaRequest())
        missing = quota_requests.QuotaRequestsNotMeasured('The quota request history deadline expired.')
        self.assertEqual(first.gpu, missing)
        self.assertEqual(second, quota_requests.RegionalQuotaRequests(self.regions[1], missing, missing))

    def test_snapshot_keeps_prices_when_the_plan_identity_cannot_load(self) -> None:
        for error in (plan_identity.PlanAgentCold('The AWS plan key is locked.'),
                      plan_identity.PlanIdentityRefused('The AWS plan key is missing.')):
            with self.subTest(error=error), mock.patch.object(plan_identity, 'plan_credentials', side_effect=error) as load:
                self.calls.clear()
                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
                self.assertEqual(self.snapshot(), 0)
                load.assert_called_once_with()
                self.assertEqual(len(self.calls), len(self.regions) * 3)
                self.assertEqual(len(ledger.read_spot_prices(self.state)), len(self.regions) * len(self.sizes) * 2)
                document = aws_target.read_snapshot(self.state)
                self.assertEqual(len(ledger.rows(document, 'regions')), len(self.regions))
                missing = quota_requests.QuotaRequestsNotMeasured(str(error))
                self.assertEqual(spot_table.spot_tables(self.state, int(self.clock.now),
                    settings_path=self.settings).quota_requests, tuple(
                        quota_requests.RegionalQuotaRequests(region, missing, missing) for region in self.regions))

    def test_spot_table_marks_legacy_snapshots_as_not_measured(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        document = aws_target.read_snapshot(self.state)
        del document['quota_requests']
        _ = (self.state / 'spot-prices.json').write_text(json.dumps(document))
        tables = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings)
        missing = quota_requests.QuotaRequestsNotMeasured('The snapshot has no quota request history for this region.')
        self.assertEqual(tables.quota_requests, tuple(
            quota_requests.RegionalQuotaRequests(region, missing, missing) for region in self.regions))
        self.assertEqual(tables.quotas[0].rows[0][1], f'{self.quota} vCPUs')

    def test_snapshot_writes_prices_and_quotas_with_one_atomic_write_and_limits_repeats(self) -> None:
        self.assertFalse((self.state / 'gate-deployment.json').exists())
        with mock.patch.object(aws_target, 'write_text_atomic', wraps=atomic_file.write_text_atomic) as write:
            self.assertEqual(self.snapshot(), 0)
        write.assert_called_once()
        self.assertEqual(write.call_args.args[0], self.state / 'spot-prices.json')
        prices = ledger.read_spot_prices(self.state)
        self.assertEqual(len(prices), len(self.regions) * len(self.sizes) * 2)
        self.assertTrue(all(row.read_at == self.clock.now for row in prices))
        self.assertEqual({row.usd_per_hour for row in prices}, {Decimal('.25'), Decimal('.3')})
        readings = ledger.rows(aws_target.read_snapshot(self.state), 'regions')
        self.assertEqual([row['region'] for row in readings], list(self.regions))
        for row in readings:
            self.assertEqual(row['standard_vcpus'], str(self.quota))
            self.assertEqual(row['gpu_vcpus'], str(self.quota - 1))
            self.assertEqual(row['read_at'], self.clock.now)
        count = len(self.regions) * 3
        self.assertEqual(len(self.calls), count)
        interval = conf.read(self.settings).whole('price_refresh_seconds')
        self.clock.advance(interval - 1)
        self.assertEqual(self.snapshot(), 0)
        self.assertEqual(len(self.calls), count)
        self.clock.advance(1)
        self.assertEqual(self.snapshot(), 0)
        self.assertEqual(len(self.calls), 2 * count)

    def test_forced_snapshot_reads_inside_the_refresh_interval_and_keeps_the_scheduled_read(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        count = len(self.calls)
        self.clock.advance(1)
        self.assertEqual(aws_target.snapshot(self.settings, self.state, execute=self.execute, clock=self.clock,
                                             notify=self.notify, force=True), 0)
        self.assertEqual(len(self.calls), 2 * count)
        readings = ledger.rows(aws_target.read_snapshot(self.state), 'regions')
        self.assertTrue(all(row['read_at'] == self.clock.now for row in readings))
        # The forced read does not move the interval: the next scheduled read runs one interval after the last one.
        self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds') - 1)
        self.assertEqual(self.snapshot(), 0)
        self.assertEqual(len(self.calls), 3 * count)

    def test_regional_failure_preserves_its_values_and_time_but_refreshes_other_regions(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        original = aws_target.read_snapshot(self.state)
        for failure in ('timeout', 'describe-spot-price-history', 'get-service-quota', 'gpu-quota', 'pagination'):
            with self.subTest(failure=failure):
                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
                self.failure = failure
                with redirect_stderr(StringIO()) as errors:
                    self.assertEqual(self.snapshot(), 1)
                self.assertNotIn('123456789012', errors.getvalue())
                document = aws_target.read_snapshot(self.state)
                for field in ('prices', 'regions'):
                    old = [row for row in ledger.rows(original, field) if row['region'] == self.regions[0]]
                    current = [row for row in ledger.rows(document, field) if row['region'] == self.regions[0]]
                    self.assertEqual(current, old)
                    good = [row for row in ledger.rows(document, field) if row['region'] == self.regions[1]]
                    self.assertTrue(all(row['read_at'] == self.clock.now for row in good))

    def test_snapshot_errors_name_the_failed_read_and_redact_the_reason(self) -> None:
        self.failure_reason = ('AccessDenied: caller arn:aws:iam::123456789012:user/fixture '
                               'cannot read account 123456789012')
        for failure, read_name in (('describe-spot-price-history', 'price'),
                                   ('get-service-quota', 'Standard quota'), ('gpu-quota', 'G and VT quota')):
            with self.subTest(read_name=read_name):
                self.failure = failure
                with redirect_stderr(StringIO()) as errors:
                    self.assertEqual(self.snapshot(), 1)
                self.assertEqual(errors.getvalue(),
                    f'aws_target: snapshot {self.regions[0]} {read_name} unavailable: AwsUnavailable: '
                    + 'AccessDenied: caller [redacted] cannot read account [redacted]\n')
                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))

    def test_snapshot_errors_redact_access_keys_and_resource_ids(self) -> None:
        identifiers = ('AKIAIOSFODNN7EXAMPLE', 'ASIAIOSFODNN7EXAMPLE', 'i-0123456789abcdef0',
                       'subnet-0123456789abcdef0', 'ami-0123456789abcdef0',
                       'vol-0123456789abcdef0', 'snap-0123456789abcdef0')
        self.failure = 'describe-spot-price-history'
        self.failure_reason = 'AccessDenied: ' + ' '.join(identifiers)
        with redirect_stderr(StringIO()) as errors:
            self.assertEqual(self.snapshot(), 1)
        self.assertEqual(errors.getvalue(),
            f'aws_target: snapshot {self.regions[0]} price unavailable: AwsUnavailable: AccessDenied: '
            + ' '.join('[redacted]' for _ in identifiers) + '\n')

    def test_snapshot_errors_limit_the_reason_after_redaction(self) -> None:
        self.replace('snapshot_error_reason_max_chars', '40')
        self.failure = 'describe-spot-price-history'
        self.failure_reason = 'arn:aws:iam::123456789012:user/fixture ' + 'denied ' * 100
        with redirect_stderr(StringIO()) as errors:
            self.assertEqual(self.snapshot(), 1)
        reason = ('[redacted] ' + 'denied ' * 100)[:40]
        self.assertEqual(errors.getvalue(),
            f'aws_target: snapshot {self.regions[0]} price unavailable: AwsUnavailable: {reason}\n')

    def test_snapshot_errors_redact_credential_fields_and_wrapped_identifiers(self) -> None:
        self.failure = 'describe-spot-price-history'
        for message, expected in (
                ('X-Amz-Security-Token: fixture-session-token', 'X-Amz-Security-Token: [redacted]'),
                ('AWS_SECRET_ACCESS_KEY=fixture-secret', 'AWS_SECRET_ACCESS_KEY=[redacted]'),
                ('AKIAIOSFODNN\n7EXAMPLE', '[redacted]'),
                ('123456\n789012', '[redacted]'),
                ('arn:aws:iam::123456\n789012:user/fixture', '[redacted]')):
            with self.subTest(message=message):
                self.failure_reason = 'AccessDenied: ' + message
                with redirect_stderr(StringIO()) as errors:
                    self.assertEqual(self.snapshot(), 1)
                self.assertEqual(errors.getvalue(),
                    f'aws_target: snapshot {self.regions[0]} price unavailable: AwsUnavailable: AccessDenied: {expected}\n')
                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))

    def test_snapshot_errors_discard_request_dumps(self) -> None:
        self.failure = 'describe-spot-price-history'
        for dump in (
                'Request dump: {"message":"Unable to process fixture-private-body"}',
                'Response dump: fixture-private-body', 'Headers: fixture-private-body',
                'Authorization: fixture-private-body', 'Host: fixture-private-body',
                'Content-Type: application/json', 'X-Amz-Security-Token: fixture-session-token',
                '{"message":"Unable to process fixture-private-body"}',
                '["Unable to process fixture-private-body"]', '<error>fixture-private-body</error>'):
            with self.subTest(dump=dump):
                self.failure_reason = 'AccessDenied: fixture read denied\n' + dump
                with redirect_stderr(StringIO()) as errors:
                    self.assertEqual(self.snapshot(), 1)
                self.assertEqual(errors.getvalue(),
                    f'aws_target: snapshot {self.regions[0]} price unavailable: AwsUnavailable: AccessDenied: fixture read denied\n')
                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))

    def test_snapshot_errors_discard_all_lines_after_a_dump_marker(self) -> None:
        self.failure = 'get-service-quota'
        for prefix in ('', 'fixture failure\n'):
            with self.subTest(prefix=prefix):
                self.failure_reason = prefix + 'Request dump:\nUnable to locate credentials in fixture-private-body'
                with redirect_stderr(StringIO()) as errors:
                    self.assertEqual(self.snapshot(), 1)
                expected = prefix.strip()
                self.assertEqual(errors.getvalue(),
                    f'aws_target: snapshot {self.regions[0]} Standard quota unavailable: AwsUnavailable: {expected}\n')
                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))

    def test_snapshot_errors_select_the_error_after_cli_usage(self) -> None:
        self.failure = 'get-service-quota'
        for error_line in (
                'aws: error: argument command: Invalid choice: service-quotas',
                'aws: [ERROR]: argument command: Invalid choice: service-quotas',
                'aws: eRrOr: fixture CLI failure', 'aws: [error]: fixture CLI failure',
                'Invalid choice: service-quotas',
                'Parameter validation failed: fixture parameter',
                'An error occurred (AccessDenied) when calling GetServiceQuota: fixture refusal',
                'Unable to locate credentials',
                'Could not connect to the endpoint URL: fixture-endpoint',
                'AccessDenied: fixture read denied', 'ExpiredToken: fixture token expired',
                'UnauthorizedOperation: fixture operation denied',
                'fixture failure without a recognized error prefix'):
            with self.subTest(error_line=error_line):
                prefix = ('\n  \n' if error_line.startswith('fixture failure') else
                          '\nusage: aws [options]\nfixture note: Unable to process fixture-private-body\n')
                self.failure_reason = prefix + error_line + '\nRequest dump: fixture-private-body'
                with redirect_stderr(StringIO()) as errors:
                    self.assertEqual(self.snapshot(), 1)
                self.assertEqual(errors.getvalue(),
                    f'aws_target: snapshot {self.regions[0]} Standard quota unavailable: AwsUnavailable: {error_line}\n')
                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))

    def test_held_lock_skips_the_snapshot_without_calls(self) -> None:
        with (self.state / 'spot-prices.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.snapshot(), 0)
            self.assertEqual(self.calls, [])
            self.assertFalse((self.state / 'spot-prices.json').exists())
        self.assertEqual(self.snapshot(), 0)

    def test_table_reads_only_local_files_and_shows_minimum_prices_ages_and_quota_fit(self) -> None:
        self.omitted = self.gpu[-1]
        self.assertEqual(self.snapshot(), 0)
        old = self.clock.now
        self.clock.advance(conf.read(self.settings).whole('price_max_age_seconds') + 1)
        before = {path: path.read_bytes() for path in self.state.iterdir()}
        with (mock.patch.object(subprocess, 'run', side_effect=AssertionError('No external command')),
              mock.patch.object(admin_secrets.AdminSecretSession, 'get', side_effect=AssertionError('No administrator key'))):
            tables = spot_table.spot_tables(self.state, int(self.clock.now))
        cells = {cell for table in tables.prices for row in table.rows for cell in row[1:]}
        self.assertTrue(any(cell.startswith('0.2500 [old ') for cell in cells))
        self.assertFalse(any('0.3000' in cell for cell in cells))
        self.assertEqual(tables.snapshot_rows, (('', f'Snapshot read {int(self.clock.now - old) // 60} min ago (stale)'),))
        self.assertEqual([row[0] for row in tables.prices[0].rows], list(self.regions))
        standard, gpu = tables.quotas
        self.assertEqual((standard.headers[1], gpu.headers[1]), ('Standard quota', 'G and VT quota'))
        for size in self.sizes:
            vcpus = deploy.largest_gate_vcpus((size,))
            quotas, quota = (gpu, self.quota - 1) if size in self.gpu else (standard, self.quota)
            column = quotas.headers.index(f'{size} ({vcpus} vCPUs)')
            self.assertIn(column, quotas.right)
            # The quotas come from the same read as the prices, so only the prices show the age.
            for row in quotas.rows:
                self.assertEqual(row[column], f'{quota // vcpus} boxes')
        # A stale region names its note in an empty cell, not the sign of a type it does not sell.
        prices = tables.prices[1]
        column = prices.headers.index(self.omitted)
        self.assertTrue(all('old' in row[column] for row in prices.rows))
        self.assertEqual(prices.not_sold, frozenset())
        self.assertEqual(before, {path: path.read_bytes() for path in self.state.iterdir()})

    def test_table_ranks_cost_per_run_with_region_times_and_marks_other_region_times(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        self.deploy_gate_zones()
        fast, slow = self.standard
        home, other = self.regions
        events: list[dict[str, object]] = []
        for attempt, size, region, box_s in (('fast', fast, home, 360), ('slow', slow, home, 720)):
            row: dict[str, object] = {'attempt': attempt, 'instance_type': size, 'mode': 'merge-gate', 'region': region}
            events.extend(({**row, 'time': self.clock.now - 3600 - box_s, 'result': 'launched'},
                           {**row, 'time': self.clock.now - 3600, 'result': 'finished', 'gate_s': box_s - 60}))
        events.append({'attempt': 'fast', 'instance_type': fast, 'mode': 'merge-gate', 'region': home, 'result': 'refused',
                       'time': self.clock.now - 3600 - 400, 'error_code': 'InsufficientInstanceCapacity'})
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in events))
        merge_gates = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings).runs[0]
        tables = {table.title: table for table in merge_gates.tables}

        # At 0.25 USD/h, 6 minutes cost 0.0250 USD and 12 minutes cost 0.0500 USD.
        # The other region adds the configured transfer charge of 0.00015 USD before rounding.
        # One refused request came before one launch of the fast type at home; the other region has no requests.
        # No bill covers the runs yet; the other region has no runs of its own.
        self.assertEqual(tables[fast].rows, (
            (home, '0.2500', '6.0', '1', '0.0250', 'bill due', '1 bill due', '0'),
            (other, '0.2500', '6.0', '1 elsewhere', '0.0252', 'no runs', 'no runs', '0')))
        self.assertEqual([row[:5] for row in tables[slow].rows], [(home, '0.2500', '12.0', '1', '0.0500'),
                                                                 (other, '0.2500', '12.0', '1 elsewhere', '0.0502')])
        for size in self.compare:
            self.assertEqual([row[2:] for row in tables[size].rows], [('—', '0', '—', 'no runs', 'no runs', '0')]
                             * len(self.regions))
        # Every column after the region holds a number.
        self.assertEqual(tables[fast].headers[-1], 'paid runs')
        self.assertIn('merge gate (minutes)', tables[fast].headers)
        self.assertEqual(tables[fast].right, frozenset(range(1, len(tables[fast].headers))))
        self.assertEqual(merge_gates.summary_of(merge_gates.cheapest),
                         f'Cheapest merge gate: {fast} in {home}, 0.0250 USD (estimate)')
        # Refused requests and launched boxes have their own tables: one row per type, one column per region.
        self.assertEqual(merge_gates.refused.rows[0], (fast, '1', '0'))
        self.assertEqual(merge_gates.launched.rows[0], (fast, '1', '0'))

    def test_launch_history_readers_accept_missing_and_unreadable_gate_jobs(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        now = int(self.clock.now)
        end = datetime.fromtimestamp(now, UTC)
        start = end - timedelta(hours=1)
        region = self.regions[0]
        size = self.standard[0]
        attempt: dict[str, object] = {
            'attempt': 'fixture', 'instance_type': size, 'mode': 'merge-gate', 'region': region}
        launched = {**attempt, 'result': 'launched', 'time': now - 600}
        finished = {**attempt, 'result': 'finished', 'time': now - 240, 'gate_s': 300}
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(now - 900,
            (ledger.PricedResource('fixture-fixed', Decimal(0)),), 'fixture'))
        gate_job = {'kind': 'StepJob', 'step': 'test-all', 'unit': '', 'branch': '', 'modules': 0}
        job_fields: tuple[dict[str, object], ...] = ({}, {'gate_job': gate_job},
            {'gate_job': {'kind': 'GateJobNotRecorded'}}, {'gate_job': {'kind': 'FutureGateJob'}},
            {'gate_job': {**gate_job, 'modules': []}})
        for job_field in job_fields:
            with self.subTest(job_field=job_field):
                rows = ({**launched, **job_field}, {**finished, **job_field})
                _ = book.launch_history_path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                self.assertEqual(book.launches(), rows)
                self.assertEqual(ledger.recent_gate_launches(self.state, start.timestamp()), rows[::-1])
                runs = run_cost.gate_runs(book, start, end, region).runs
                self.assertEqual([(run.instance_type, run.box_s) for run in runs], [(size, Decimal(360))])
                self.assertEqual(sum(day.runs for day in book.spend_days(aws_config.read_settings(self.settings), now)), 1)
                report = aws_report.gate_type_report(self.state, end.date().isoformat(), 1)
                self.assertTrue(any(line.startswith(f'| {size} |') for line in report.tables))
                page = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings)
                payload = page.read_text().split('<script id="aws-prices-data" type="application/json">', 1)[1]
                data = cast(aws_page.PageData, json.loads(payload.split('</script>', 1)[0]))
                table = next(table for table in data['runs'][0]['tables'] if table.get('title') == size)
                self.assertEqual(table['rows'][0][:3], [region, '6.0', '1'])

    def test_paid_run_cell_counts_the_runs_whose_bill_is_still_due(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        billed_size, due_size = self.standard
        home = self.regions[0]
        today = datetime.fromtimestamp(self.clock.now, UTC).date()
        midnight = int(datetime(today.year, today.month, today.day, tzinfo=UTC).timestamp())
        events: list[dict[str, object]] = []
        # The billed type runs yesterday and today; the other type runs only today. Only yesterday has a bill.
        for attempt, size, finished in (('old', billed_size, midnight - 3600), ('new', billed_size, midnight + 60),
                                        ('due', due_size, midnight + 60)):
            row: dict[str, object] = {'attempt': attempt, 'instance_type': size, 'mode': 'merge-gate', 'region': home}
            events.extend(({**row, 'time': finished - 360, 'result': 'launched'},
                           {**row, 'time': finished, 'result': 'finished', 'gate_s': 300}))
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in events))
        yesterday = midnight - 86400
        bill = ledger.ChargeRead((today - timedelta(days=1)).isoformat(), midnight + 3600, yesterday, midnight,
                                 Decimal(1), Decimal(0), Decimal(0), (ledger.UsageTypeCost(
                                     ledger.REGION_USAGE_PREFIXES[home][-1] + '-SpotUsage:' + billed_size,
                                     Decimal('0.05'), Decimal('0.1'), 'Hrs'),))
        with mock.patch.object(ledger.CostLedger, 'read_bills', return_value=(bill,)):
            tables = spot_table.spot_tables(self.state, max(int(self.clock.now), midnight + 120), settings_path=self.settings)
        cells = {table.title: table.rows[0][6] for table in tables.runs[0].tables}
        self.assertEqual(cells[billed_size], '0.0500 (1 bill due)')
        self.assertEqual(cells[due_size], '1 bill due')

    def test_each_scope_counts_only_its_run_kinds(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        self.deploy_gate_zones()
        size = self.standard[0]
        home = self.regions[0]
        events: list[dict[str, object]] = []
        for attempt, mode, box_s in (('gate', 'merge-gate', 360), ('targeted', 'targeted-test', 120)):
            row: dict[str, object] = {'attempt': attempt, 'instance_type': size, 'mode': mode, 'region': home}
            events.extend(({**row, 'time': self.clock.now - 3600 - box_s, 'result': 'launched'},
                           {**row, 'time': self.clock.now - 3600, 'result': 'finished', 'gate_s': box_s - 60}))
        refusal: dict[str, object] = {'instance_type': size, 'region': home, 'result': 'refused',
                                      'time': self.clock.now - 7200, 'error_code': 'InsufficientInstanceCapacity'}
        events.extend(({**refusal, 'attempt': 'gate', 'mode': 'merge-gate'},
                       *({**refusal, 'attempt': 'targeted', 'mode': 'targeted-test'} for _ in range(2)),
                       # A request from before launch rows recorded a mode counts only when every kind counts.
                       {**refusal, 'attempt': 'old'}, {**refusal, 'attempt': 'old', 'result': 'launched'}))
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in events))
        tables = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings)
        self.assertEqual([costs.scope.name for costs in tables.runs], ['merge-gate', 'targeted-test', 'both'])
        self.assertEqual([(costs.refused.rows[0][:2], costs.launched.rows[0][:2]) for costs in tables.runs],
                         [((size, count), (size, launched)) for count, launched in (('1', '1'), ('2', '1'), ('4', '3'))])
        end = datetime.fromtimestamp(self.clock.now, UTC)
        evidence = run_cost.run_evidence(events, end - timedelta(days=conf.read(self.settings).whole('run_cost_days')), end, home)
        # The fixture lowest zone price is 0.25 USD/h: 6 minutes cost 0.0250, 2 minutes 0.0083, and the 4 minute median 0.0167.
        for costs, run_time, usd in zip(tables.runs, (('6.0', '1'), ('2.0', '1'), ('4.0', '2')), ('0.0250', '0.0083', '0.0167')):
            with self.subTest(scope=costs.scope.name):
                table = next(table for table in costs.tables if table.title == size)
                self.assertEqual(table.headers[2:4], (f'{costs.scope.noun} (minutes)', 'runs'))
                self.assertEqual(table.rows[0][2:5], (*run_time, usd))
                self.assertEqual(f'{evidence.expected_usd(Decimal("0.25"), size, home, costs.scope.modes, Decimal(0), Decimal(0), unmeasured_extra_s=Decimal(0)):.4f}', usd)
                self.assertEqual(costs.summary_of(costs.cheapest), f'Cheapest {costs.scope.noun}: {size} in {home}, {usd} USD (estimate)')
                # The Gate columns show the same estimate for Standard types.
                standard = costs.gate_prices
                self.assertEqual(standard.headers, ('region', *self.standard, *self.compare))
                self.assertEqual((standard.rows[0][1], f'{standard.values[0][1]:.4f}'), (usd, usd))

    def test_a_type_without_runs_borrows_the_times_of_its_vcpu_count(self) -> None:
        home = self.regions[0]
        events: list[dict[str, object]] = []
        for attempt, size, box_s in (('eight', 'c7a.8xlarge', 240), ('four', 'c7a.4xlarge', 420)):
            row: dict[str, object] = {'attempt': attempt, 'instance_type': size, 'mode': 'merge-gate', 'region': home}
            events.extend(({**row, 'time': self.clock.now - 3600 - box_s, 'result': 'launched'},
                           {**row, 'time': self.clock.now - 3600, 'result': 'finished', 'gate_s': box_s - 60}))
        end = datetime.fromtimestamp(self.clock.now, UTC)
        evidence = run_cost.run_evidence(events, end - timedelta(days=7), end, home)
        # Not the median of all types (330 s): the same vCPU count runs at the same speed.
        own, elsewhere, same = run_cost.BoxTimeSource.OWN, run_cost.BoxTimeSource.OTHER_REGIONS, run_cost.BoxTimeSource.SAME_VCPUS
        for size, region, times, source in (('c7a.8xlarge', home, [240], own), ('c7a.8xlarge', 'elsewhere', [240], elsewhere),
                                            ('m7a.8xlarge', home, [240], same), ('m7a.4xlarge', home, [420], same),
                                            ('m7a.2xlarge', home, [], same), ('unknown', home, [], same)):
            with self.subTest(size=size, region=region):
                self.assertEqual(evidence.box_seconds(size, region, ('merge-gate',)), ([Decimal(time) for time in times], source))

    def test_page_holds_the_tables_as_data_and_its_link_needs_the_page(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        folder = self.root / 'page'
        with (mock.patch.object(aws_page, 'output_folder', return_value=folder),
              mock.patch.object(review_link, 'review_root', return_value=self.root),
              mock.patch.object(review_link, 'link_host', return_value='127.0.0.1')):
            with self.assertRaises(FileNotFoundError):
                _ = aws_page.page_url()
            page = aws_page.publish(self.state, int(self.clock.now), folder, settings_path=self.settings)
            self.assertEqual(page, folder / aws_page.PAGE_NAME)
            self.assertTrue(aws_page.page_url().endswith('/page/' + aws_page.PAGE_NAME))
        self.assertEqual(page.stat().st_mode & 0o777, 0o644)
        source = page.read_text()
        self.assertNotIn('__AWS_PRICES_DATA__', source)
        self.assertIn(console_theme.CSS, source)
        payload = source.split('<script id="aws-prices-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        data = cast(aws_page.PageData, json.loads(payload))
        tables = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings)
        expected = aws_page.page_data(tables, data["spend"], int(self.clock.now))
        self.assertGreater(data["render_seconds"], 0)
        self.assertEqual(data, {**expected, "render_seconds": data["render_seconds"]})
        self.assertEqual([(entry['scope'], entry['label']) for entry in data['runs']],
                         [('merge-gate', 'Merge gates'), ('targeted-test', 'Targeted runs'), ('both', 'Both')])
        self.assertEqual([table['right'] for table in data['prices']],
                         [list(range(1, len(types) + 1)) for types in ((*self.standard, *self.compare), self.gpu)])
        # The fixture lowest zone price is 0.25 USD/h for every type and region; the region column has no number.
        prices = data['prices'][0]
        self.assertEqual([row[0] for row in prices['rows']], list(self.regions))
        self.assertEqual(prices['values'], [[None, *(0.25 for _ in prices['headers'][1:])] for _ in self.regions])
        for table in (*data['prices'], *data['quotas']):
            self.assertNotIn('title', table)
        # The Spot and Gate columns in Prices and quotas show these values.
        run_headers = data['runs'][0]['tables'][0]['headers']
        self.assertNotIn(spot_table.SPOT_HEADER, run_headers)
        self.assertNotIn(spot_table.ESTIMATE_HEADER, run_headers)
        self.assertEqual(data['runs'][0]['tables'][0]['right'], list(range(1, len(run_headers))))
        for entry in data['runs']:
            self.assertEqual([table['headers'] for table in entry['prices']], [['region', *self.standard, *self.compare]])
        self.assertEqual([table['not_sold'] for table in data['prices']], [[], []])
        self.assertEqual([table['region_axis'] for table in (*data['prices'], *data['quotas'])], ['rows'] * 4)
        self.assertEqual(data['regions'], sorted(self.regions, key=aws_page.region_filter_key))
        self.assertEqual([line['text'] for line in data['snapshot']], [text for _region, text in tables.snapshot_rows])
        summaries = [entry['estimates'][0]['summary'] if entry['estimates'] else entry['no_estimate'] for entry in data['runs']]
        self.assertEqual(summaries, [costs.summary_of(costs.cheapest) for costs in tables.runs])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_page_status_uses_the_real_aws_loader(self) -> None:
        _ = self.snapshot()
        folder = self.root / 'page'
        page = aws_page.publish(self.state, int(self.clock.now), folder, settings_path=self.settings)
        source = page.read_text()
        assert_status_states(self, source, 0)

    def test_region_filter_sorts_each_group_alphabetically_in_the_group_order(self) -> None:
        regions = ('us-west-2', 'sa-east-1', 'me-south-1', 'ap-south-1', 'eu-west-1', 'ca-central-1',
                   'us-east-2', 'ap-northeast-1', 'eu-central-1', 'af-south-1')
        self.assertEqual(sorted(regions, key=aws_page.region_filter_key),
                         ['us-east-2', 'us-west-2', 'eu-central-1', 'eu-west-1', 'ap-northeast-1', 'ap-south-1',
                          'ca-central-1', 'sa-east-1', 'af-south-1', 'me-south-1'])

    def test_spend_card_shows_today_yesterday_and_the_month(self) -> None:
        def day(name: str, source: str, gate: str, runs: int, provisional: bool = False) -> ledger.DailySpend:
            return ledger.DailySpend(name, source, (ledger.SpendGroupCost(ledger.SPEND_GROUPS[0], Decimal(gate)),
                                                    ledger.SpendGroupCost('storage', Decimal('1'))), runs, provisional_bill=provisional)
        now = int(datetime(2026, 1, 15, 12, tzinfo=UTC).timestamp())
        read = spend.Spend((day('2026-01-15', 'estimated from the newest bill', '1', 5),
                            day('2026-01-14', 'AWS bill', '3', 7, True), day('2026-01-13', 'AWS bill', '1', 2)),
                           Decimal(35), Decimal(400))
        data = aws_page.spend_data(read, now, 2)
        self.assertEqual(data['unavailable'], '')
        self.assertEqual([(tile['label'], tile['usd']) for tile in data['tiles']],
                         [('Today', '$2.00'), ('Yesterday', '$4.00'), ('Month to date', '$8.00')])
        self.assertEqual(data['tiles'][0]['note'], 'UTC 2026-01-15, estimate of the $35.00 daily limit. 5 gate runs.')
        self.assertEqual(data['tiles'][1]['note'], 'UTC 2026-01-14, provisional bill of the $35.00 daily limit. 7 gate runs.')
        self.assertEqual(data['tiles'][2]['note'], 'UTC 2026-01, of the $400.00 monthly limit.')
        # The page and the buildlog report share one month calculation.
        self.assertEqual(data['forecast'], 'Month forecast: $57.00 at $3.00 a day, the average of the last 2 billed days. '
                         + '32-CPU gate compute is 67% of the spend on those days.')
        no_bill = aws_page.spend_data(spend.Spend(read.days[:1], Decimal(35), Decimal(400)), now, 2)
        self.assertEqual([tile['usd'] for tile in no_bill['tiles']], ['$2.00', '—', '—'])
        self.assertEqual(no_bill['forecast'], '')
        gone = aws_page.spend_data(aws_config.MeasurementUnavailable('ledger unreadable'), now, 2)
        self.assertEqual(gone, {'unavailable': 'AWS spend unavailable: ledger unreadable.', 'tiles': [], 'forecast': ''})

    def test_page_data_cannot_end_its_script_element(self) -> None:
        folder = self.root / 'page'
        cell = 'a</script><b>'
        tables = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings)
        standard, *other_prices = tables.prices
        first, *other_rows = standard.rows
        prices = (replace(standard, rows=((first[0], cell, *first[2:]), *other_rows)), *other_prices)
        with mock.patch.object(spot_table, 'spot_tables', return_value=replace(tables, prices=prices)):
            source = aws_page.publish(self.state, int(self.clock.now), folder).read_text()
        payload = source.split('<script id="aws-prices-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        self.assertEqual(cast(aws_page.PageData, json.loads(payload))['prices'][0]['rows'][0][1], cell)

    def test_paid_cells_name_why_no_bill_counts_and_the_cheapest_is_a_placement_type(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        self.deploy_gate_zones()
        size, = self.compare
        home = self.regions[0]
        events: list[dict[str, object]] = []
        # The ledger contains a short targeted run of a type outside the deployment.
        for attempt, kind, mode, box_s in (('a', self.standard[0], 'merge-gate', 360), ('b', size, 'targeted-test', 60)):
            row: dict[str, object] = {'attempt': attempt, 'instance_type': kind, 'mode': mode, 'region': home}
            events.extend(({**row, 'time': self.clock.now - 3600 - box_s, 'result': 'launched'},
                           {**row, 'time': self.clock.now - 3600, 'result': 'finished', 'gate_s': box_s - 60}))
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in events))
        tables = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings)
        paid = {costs.scope.name: next(table for table in costs.tables if table.title == size).rows[0][5:] for costs in tables.runs}
        self.assertEqual(paid, {'merge-gate': ('bill due', 'no runs', '0'),
                                'targeted-test': ('bill due', '1 bill due', '0'),
                                'both': ('bill due', '1 bill due', '0')})
        self.assertEqual([(costs.cheapest.size if costs.cheapest else None) for costs in tables.runs],
                         [self.standard[0], None, self.standard[0]])

    def test_prices_split_gpu_types_and_carry_each_price_for_the_color_scale(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        path = self.state / 'spot-prices.json'
        document = ledger.mapping(cast(object, json.loads(path.read_text())))
        cheap = self.regions[-1], self.standard[0]
        for row in ledger.rows(document, 'prices'):
            if (row['region'], row['instance_type']) == cheap:
                row['usd_per_hour'] = '0.2000'
        _ = path.write_text(json.dumps(document))
        standard, gpu = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings).prices
        self.assertEqual((standard.headers[1:], gpu.headers[1:]), ((*self.standard, *self.compare), self.gpu))
        self.assertEqual([row[0] for row in standard.rows], list(self.regions))
        self.assertEqual([row[1] for row in standard.values], [*(Decimal('0.25') for _ in self.regions[:-1]), Decimal('0.2000')])
        self.assertEqual({row[0] for row in standard.values}, {None})

    def test_snapshot_line_is_one_line_only_when_every_region_agrees(self) -> None:
        reads = tuple(spot_table.RegionRead(region, 3364, False, None, 0) for region in ('a', 'b'))
        tables = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings)
        self.assertEqual(replace(tables, regions=reads).snapshot_rows, (('', 'Snapshot read 56 min ago'),))
        differ = (reads[0], spot_table.RegionRead('b', 7200, True, None, 0))
        self.assertEqual(replace(tables, regions=differ).snapshot_rows,
                         (('a', 'a: read 56 min ago'), ('b', 'b: read 120 min ago (stale)')))

    def test_absent_snapshot_prints_unknown_values_without_creating_files(self) -> None:
        texts = table_texts(spot_table.spot_tables(self.state, int(self.clock.now)))
        self.assertIn('Snapshot no snapshot was ever taken', texts)
        self.assertIn('no snapshot', texts)
        self.assertNotIn('-', texts)
        self.assertFalse(any('0.0000' in text for text in texts))
        self.assertEqual(list(self.state.iterdir()), [])

    def test_tick_reads_only_account_charges(self) -> None:
        controller = aws_target.AccountRefresh(self.settings, self.state, execute=self.execute, clock=self.clock)
        with mock.patch.object(controller, 'refresh_charges') as charges:
            controller.tick()
        charges.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_snapshot_cli_uses_the_aws_state_directory(self) -> None:
        with (mock.patch.object(aws_target, 'ssh_target_state_directory', return_value=self.state),
              mock.patch.object(aws_target, 'snapshot', return_value=1) as read,
              mock.patch.object(sys, 'argv', ['aws_target.py', 'snapshot', '--settings', str(self.settings)])):
            self.assertEqual(aws_target.main(), 1)
        read.assert_called_once_with(self.settings, self.state)

    def test_scaler_policy_allows_spot_reads_in_all_regions(self) -> None:
        document = ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_text())))
        resources = ledger.mapping(document['Resources'])
        policy = ledger.mapping(ledger.mapping(resources['GateGpuTrialScalerPolicy'])['Properties'])
        statements = ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement')
        self.assertEqual(self.snapshot(), 0)
        # Derive IAM actions from every API request in the snapshot path.
        actions = {arguments[5].replace('-', '') + ':' + ''.join(
            word.capitalize() for word in arguments[6].split('-')) for arguments in self.calls}
        for action in sorted(actions | {'ec2:GetSpotPlacementScores'}):
            with self.subTest(action=action):
                self.assertTrue(any(action in cast(list[str], row['Action']) and row['Resource'] == '*'
                                    and row['Effect'] == 'Allow' and 'Condition' not in row for row in statements))

    def test_scaler_policy_reads_quota_request_history_for_only_the_spot_quotas(self) -> None:
        document = ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_text())))
        resources = ledger.mapping(document['Resources'])
        policy = ledger.mapping(ledger.mapping(resources['GateGpuTrialScalerPolicy'])['Properties'])
        statements = [row for row in ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement')
                      if 'servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota' in cast(list[str], row['Action'])]
        self.assertEqual(len(statements), 1)
        statement, = statements
        self.assertEqual((statement['Effect'], statement['Action']),
                         ('Allow', ['servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota']))
        codes = sorted({code for _regions, code, _desired, _label in deploy.quota_targets('us-east-2')})
        self.assertEqual(statement['Resource'], [
            {'Fn::Sub': 'arn:${AWS::Partition}:servicequotas:*:${AWS::AccountId}:ec2/' + code} for code in codes])

    def test_initial_failure_saves_each_error_and_the_table_names_its_time(self) -> None:
        self.failure = 'describe-spot-price-history'
        self.failure_reason = 'UnauthorizedOperation: fixture price read denied'
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        failure, = aws_target.snapshot_failures(aws_target.read_snapshot(self.state))
        self.assertEqual((failure.region, failure.at, failure.read, failure.reason),
                         (self.regions[0], int(self.clock.now), 'price', self.failure_reason))
        before = {path: path.read_bytes() for path in self.state.iterdir()}
        texts = table_texts(spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings))
        self.assertIn('read failed', texts)
        self.assertIn(datetime.fromtimestamp(self.clock.now, UTC).isoformat(), '\n'.join(texts))
        self.assertIn(self.failure_reason, '\n'.join(texts))
        self.assertNotIn('-', texts)
        self.assertEqual(before, {path: path.read_bytes() for path in self.state.iterdir()})

    def test_every_failed_region_has_a_record_but_the_streak_sends_one_notice(self) -> None:
        self.failure = 'all'
        self.failure_reason = 'UnauthorizedOperation: fixture request denied'
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        failures = aws_target.snapshot_failures(aws_target.read_snapshot(self.state))
        self.assertEqual(tuple(row.region for row in failures), self.regions)
        self.assertEqual(len(self.notices), 1)
        texts = table_texts(spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings))
        for region in self.regions:
            self.assertIn('\n' + region + ': last read failed at', '\n' + '\n'.join(texts))
            self.assertIn(region, self.notices[0][1].body)
        self.assertNotIn('-', texts)

    def test_failure_with_cached_prices_shows_the_error_and_the_last_good_age(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        interval = conf.read(self.settings).whole('price_refresh_seconds')
        self.clock.advance(interval)
        self.failure = 'get-service-quota'
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        texts = table_texts(spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings))
        self.assertIn('0.2500 [read failed]', texts)
        self.assertIn(f'last good read {interval // 60} min ago', '\n'.join(texts))
        self.assertIn('Standard quota: AwsUnavailable:', '\n'.join(texts))

    def test_failure_notice_sends_once_per_streak_and_success_resets_it(self) -> None:
        self.failure = 'describe-spot-price-history'
        self.failure_reason = 'UnauthorizedOperation: fixture price read denied'
        interval = conf.read(self.settings).whole('price_refresh_seconds')
        for _ in range(3):
            with redirect_stderr(StringIO()):
                self.assertEqual(self.snapshot(), 1)
            self.clock.advance(interval)
        self.assertEqual(len(self.notices), 1)
        key, text, timeout = self.notices[0]
        self.assertIn(f'AWS refused the {self.regions[0]} price read.', text.body)
        self.assertEqual(text.action, push_texts.render('aws.snapshot_failed', failures='').action)
        self.assertEqual(timeout, conf.read(self.settings).number('snapshot_notification_timeout_s'))
        self.failure = ''
        self.assertEqual(self.snapshot(), 0)
        self.assertNotIn('failure_streak', aws_target.read_snapshot(self.state))
        self.assertEqual(aws_target.snapshot_failures(aws_target.read_snapshot(self.state)), ())
        self.clock.advance(interval)
        self.failure = 'describe-spot-price-history'
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        self.assertEqual(len(self.notices), 2)
        self.assertNotEqual(self.notices[-1][0], key)

    def test_crash_after_notice_delivery_does_not_repeat_the_streak_notice(self) -> None:
        self.failure = 'describe-spot-price-history'

        def deliver_then_crash(key: str, text: push_texts.RenderedPush, timeout: float) -> bool:
            _ = self.notify(key, text, timeout)
            raise RuntimeError('fixture crash after delivery')

        with redirect_stderr(StringIO()), self.assertRaisesRegex(RuntimeError, 'fixture crash after delivery'):
            _ = aws_target.snapshot(self.settings, self.state, execute=self.execute, clock=self.clock,
                                    notify=deliver_then_crash)
        self.assertEqual(len(self.notices), 1)
        self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        self.assertEqual(len(self.notices), 1)

    def test_previous_notified_field_still_suppresses_the_streak_notice(self) -> None:
        self.failure = 'describe-spot-price-history'
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        path = self.state / 'spot-prices.json'
        document = aws_target.read_snapshot(self.state)
        streak = ledger.mapping(document['failure_streak'])
        document['failure_streak'] = {'since': streak['since'], 'notified': True}
        _ = path.write_text(json.dumps(document))
        self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        self.assertEqual(len(self.notices), 1)
        self.assertTrue(ledger.mapping(aws_target.read_snapshot(self.state)['failure_streak'])['notice_claimed'])

    def test_failed_notice_retries_without_changing_its_key(self) -> None:
        self.failure = 'describe-spot-price-history'
        self.notice_accepted = False
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
        self.notice_accepted = True
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        self.assertEqual(len(self.notices), 2)
        self.assertEqual(self.notices[0][0], self.notices[1][0])
        self.assertEqual(self.notices[1][1].action, push_texts.render('aws.snapshot_failed', failures='').action)
        self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        self.assertEqual(len(self.notices), 2)

    def test_other_failures_preserve_the_streak_without_claiming_a_notice(self) -> None:
        interval = conf.read(self.settings).whole('price_refresh_seconds')
        since = int(self.clock.now)
        for failure, reason in (
                ('timeout', ''),
                ('describe-spot-price-history', 'Could not connect to the endpoint URL: fixture-endpoint'),
                ('describe-spot-price-history', 'ThrottlingException: fixture rate limit'),
                ('describe-spot-price-history', 'InvalidClientTokenId: fixture key rejected'),
                ('describe-spot-price-history', 'ExpiredToken: fixture token expired'),
                ('describe-spot-price-history', 'AuthFailure: fixture authentication failed'),
                ('describe-spot-price-history', 'Unable to locate credentials'),
                ('describe-spot-price-history', 'fixture unknown failure')):
            with self.subTest(failure=failure, reason=reason):
                self.failure, self.failure_reason = failure, reason
                with redirect_stderr(StringIO()):
                    self.assertEqual(self.snapshot(), 1)
                saved = aws_target.read_snapshot(self.state)
                self.assertEqual(saved['failure_streak'], {'since': since, 'notice_claimed': False})
                self.assertTrue(aws_target.snapshot_failures(saved))
                self.assertEqual(self.notices, [])
                self.assertIn('read failed', table_texts(spot_table.spot_tables(self.state, int(self.clock.now),
                                                                               settings_path=self.settings)))
                self.clock.advance(interval)
        self.failure_reason = 'AccessDenied: fixture read denied'
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        self.assertEqual(len(self.notices), 1)
        self.assertEqual(self.notices[0][0], f'aws-spot-snapshot:{since}')
        self.clock.advance(interval)
        self.failure = 'timeout'
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        self.clock.advance(interval)
        self.failure = 'describe-spot-price-history'
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        self.assertEqual(len(self.notices), 1)

    def test_mixed_failures_alert_only_for_the_permission_error(self) -> None:
        self.failure = 'all'
        self.failure_reason = 'ThrottlingException: fixture rate limit'
        self.regional_failure_reasons[self.regions[1]] = 'AccessDenied: fixture read denied'
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        self.assertEqual(len(aws_target.snapshot_failures(aws_target.read_snapshot(self.state))), len(self.regions))
        self.assertEqual(len(self.notices), 1)
        self.assertNotIn(self.regions[0], self.notices[0][1].body)
        self.assertIn(self.regions[1], self.notices[0][1].body)
        self.assertNotIn('ThrottlingException', self.notices[0][1].body)

    def test_permission_error_forms_send_a_notice(self) -> None:
        for reason in ('AccessDenied: fixture refusal', 'AccessDeniedException: fixture refusal',
                       'UnauthorizedOperation: fixture refusal', 'The caller is not authorized to read prices.'):
            with self.subTest(reason=reason):
                self.failure = 'describe-spot-price-history'
                self.failure_reason = reason
                with redirect_stderr(StringIO()):
                    self.assertEqual(self.snapshot(), 1)
                self.assertEqual(len(self.notices), 1)
                self.failure = ''
                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
                self.assertEqual(self.snapshot(), 0)
                self.clock.advance(conf.read(self.settings).whole('price_refresh_seconds'))
                self.notices.clear()

    def test_notice_uses_the_registered_user_template_and_a_bounded_command(self) -> None:
        timeout = conf.read(self.settings).number('snapshot_notification_timeout_s')
        push = aws_target.snapshot_failure_push((aws_target.SnapshotFailure(
            self.regions[0], int(self.clock.now), 'price', 'AwsUnavailable', 'UnauthorizedOperation: fixture price read denied'),))
        with mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            self.assertTrue(aws_target.snapshot_failure_notice('fixture-streak', push, timeout))
        arguments = cast(list[str], run.call_args.args[0])
        self.assertEqual(arguments[arguments.index('--to') + 1], 'user')
        self.assertEqual(arguments[arguments.index('--text-id') + 1], 'aws.snapshot_failed')
        self.assertEqual(arguments[arguments.index('--key') + 1], 'fixture-streak')
        self.assertEqual(arguments, push_texts.user_send_argv(push, key='fixture-streak'))
        self.assertEqual(push.action, push_texts.render('aws.snapshot_failed', failures='').action)
        self.assertEqual(set(push.values), {'failures'})
        self.assertIn(f'AWS refused the {self.regions[0]} price read.', push.body)
        self.assertEqual(run.call_args.kwargs['timeout'], timeout)
        with mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture notice', timeout)):
            self.assertFalse(aws_target.snapshot_failure_notice('fixture-streak', push, timeout))

    def test_notice_names_each_aws_error_without_copying_the_cli_error_line(self) -> None:
        reason = ('An error occurred (UnauthorizedOperation) when calling the DescribeSpotPriceHistory operation: '
                  + 'fixture price read denied ' * conf.read(self.settings).whole('snapshot_error_reason_max_chars'))
        failures = tuple(aws_target.SnapshotFailure(region, int(self.clock.now), 'price', 'AwsUnavailable', reason)
                         for region in self.regions)
        push = aws_target.snapshot_failure_push(failures)
        for region in self.regions:
            self.assertIn(f'AWS refused the {region} price read.', push.body)
        self.assertNotIn('DescribeSpotPriceHistory', push.body)
        self.assertNotIn(reason, push.body)

    def test_saved_failure_has_the_same_bounded_redacted_reason_as_the_log(self) -> None:
        self.failure = 'describe-spot-price-history'
        self.failure_reason = 'AccessDenied: fixture refusal ' * conf.read(self.settings).whole('snapshot_error_reason_max_chars')
        with redirect_stderr(StringIO()):
            self.assertEqual(self.snapshot(), 1)
        failure, = aws_target.snapshot_failures(aws_target.read_snapshot(self.state))
        self.assertEqual(len(failure.reason), conf.read(self.settings).whole('snapshot_error_reason_max_chars'))
        self.assertIn('AccessDenied', failure.reason)
        self.assertIn(f'AWS refused the {self.regions[0]} price read.', self.notices[0][1].body)

    def test_fresh_reply_without_an_offer_names_the_missing_price(self) -> None:
        self.omitted = self.gpu[-1]
        self.assertEqual(self.snapshot(), 0)
        gpu = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings).prices[1]
        column = gpu.headers.index(self.omitted)
        self.assertEqual({row[column] for row in gpu.rows}, {spot_table.NOT_SOLD})
        self.assertEqual(gpu.not_sold, frozenset((row, column) for row in range(len(gpu.rows))))



class HanaBilledHoursTests(CostFixture):
    def test_hana_billed_hours_reads_only_the_day_and_configured_spot_types(self) -> None:
        configuration = self.root / 'hana_aws.conf'
        text_setting(configuration, 'instance_type', 'm7a.4xlarge')
        text_setting(configuration, 'fallback_instance_types', 'c7a.4xlarge')
        text_setting(configuration, 'region', 'us-east-2')
        day = date(2026, 1, 15)
        start = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
        directory = self.state / 'charges'
        directory.mkdir()
        path = directory / (day.isoformat() + '.json')
        self.assertIsNone(run_cost.hana_billed_hours(self.state, day))

        ignored = (
            ledger.UsageTypeCost('USE1-SpotUsage:m7a.4xlarge', Decimal(0), Decimal(4), 'Hrs'),
            ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal(0), Decimal(2), 'Hrs'),
            ledger.UsageTypeCost('USE2-BoxUsage:m7a.4xlarge', Decimal(0), Decimal(3), 'Hrs'),
        )
        primary = ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', Decimal(0), Decimal('0.490279'), 'Hrs')
        fallback = ledger.UsageTypeCost('USE2-SpotUsage:c7a.4xlarge', Decimal(0), Decimal('0.25'), 'Hrs')
        bill = ledger.ChargeRead(day.isoformat(), start + 86400, start, start + 86400,
                                 Decimal(0), Decimal(0), Decimal(0))
        for usage, expected in (
            ((*ignored, primary), Decimal('0.490279')),
            ((*ignored, primary, fallback), Decimal('0.740279')),
            (ignored, Decimal('0.0')),
        ):
            with self.subTest(hours=expected):
                _ = path.write_text(json.dumps(ledger.charge_record(replace(bill, usage_types=usage))))
                self.assertEqual(run_cost.hana_billed_hours(self.state, day), expected)

        next_day = replace(bill, at=start + 2 * 86400, period_start=start + 86400,
                           period_end=start + 2 * 86400, usage_types=(primary,))
        _ = path.write_text(json.dumps(ledger.charge_record(next_day)))
        self.assertIsNone(run_cost.hana_billed_hours(self.state, day))

        damaged = ledger.charge_record(bill)
        damaged['at'] = 'invalid'
        _ = path.write_text(json.dumps(damaged))
        self.assertIsNone(run_cost.hana_billed_hours(self.state, day))


class HoursCheckTests(CostFixture):
    """Billed Spot hours against the cost ledger's box hours, per type, region and UTC day."""
    clock: TestClock = TestClock()
    home: str = ''
    other: str = ''
    size: str = ''
    builder: str = ''
    limit: Decimal = Decimal(0)
    observations: list[ledger.InstanceObservation] = []
    reservations: list[ledger.GateReservation] = []
    exposures: list[ledger.HanaComputeExposure] = []
    bills: list[ledger.ChargeRead] = []
    notice_commands: list[tuple[str, ...]] = []

    @override
    def setUp(self) -> None:
        super().setUp()
        self.configure('region=us-east-1\ninstance_type=m7a.4xlarge\n'
                       + '# whole: 1..\nrun_cost_days=7\n# whole: 1..\nprice_max_age_seconds=7200\n'
                       + '# range: 0.001..\nsnapshot_notification_timeout_s=5\n'
                       + '# range: 0..1\nbilled_hours_gap_max_ratio=0.15\n')
        self.configure_gate(
                                 'instance_types=c7a.4xlarge\nprice_compare_instance_types=m7a.8xlarge\n'
                                 + 'gpu_instance_types=g4dn.xlarge\nhana_gpu_instance_types=g6.2xlarge\n'
                                 + 'spot_candidate_regions=us-east-1 us-east-2\n'
                                 + '# range: 0..100\ngate_wait_usd_per_minute=0\n'
                                 + 'unmeasured_region_extra_seconds={}\n# whole: 1..60\nlifetime_minutes=20\n')
        self.clock = TestClock()
        configuration = conf.read(self.settings)
        self.home = configuration.text('region')
        self.other = 'us-east-2'
        self.size = conf.read_or_repository('cloud_gate').words('instance_types')[0]
        workload = self.root / 'hana_aws.conf'
        text_setting(workload, 'region', self.other)
        text_setting(workload, 'instance_type', 'm7a.2xlarge')
        text_setting(workload, 'fallback_instance_types', f'{self.size},m6a.4xlarge')
        self.builder = configuration.text('instance_type')
        self.observations = []
        self.limit = conf.decimal(configuration.text('billed_hours_gap_max_ratio'))
        self.reservations, self.bills, self.notice_commands = [], [], []
        self.exposures = []

        def evidence(_book: ledger.CostLedger) -> ledger.LedgerEvidence:
            return ledger.LedgerEvidence(observations=list(self.observations), gate_reservations=list(self.reservations),
                                         hana_compute=list(self.exposures))

        def bills(_book: ledger.CostLedger) -> tuple[ledger.ChargeRead, ...]:
            return tuple(self.bills)

        _ = self.enterContext(mock.patch.object(ledger.CostLedger, 'read_snapshot', evidence))
        _ = self.enterContext(mock.patch.object(ledger.CostLedger, 'read_bills', bills))

    def day(self, back: int) -> datetime:
        """Midnight UTC, back days before the clock's day."""
        today = datetime.fromtimestamp(self.clock.now, UTC).date()
        return datetime(today.year, today.month, today.day, tzinfo=UTC) - timedelta(days=back)

    def gate(self, size: str, region: str, start: datetime, seconds: int,
             state: ledger.GateTerminated | ledger.GateAbsent | ledger.GateRunning | None = None) -> None:
        at = int(start.timestamp())
        self.reservations.append(ledger.GateReservation(f'gate-{len(self.reservations)}', at, at + seconds,
            Decimal(1), Decimal(0), state or ledger.GateTerminated(at + seconds, 'i-fixture'),
            instance_type=size, region='' if region == self.home else region))

    def bill(self, day: datetime, hours: dict[tuple[str, str], Decimal]) -> None:
        start = int(day.timestamp())
        self.bills.append(ledger.ChargeRead(day.date().isoformat(), start + 90000, start, start + 86400,
            Decimal(1), Decimal(0), Decimal(0), tuple(ledger.UsageTypeCost(
                ledger.REGION_USAGE_PREFIXES[region][-1] + '-SpotUsage:' + size, Decimal(1), quantity, 'Hrs')
                for (size, region), quantity in hours.items())))

    def checks(self) -> dict[run_cost.DayKey, run_cost.HoursCheck]:
        end = datetime.fromtimestamp(self.clock.now, UTC)
        days = conf.read(self.settings).whole('run_cost_days')
        scope = aws_target.hours_check_scope(conf.read_or_repository('cloud_gate'))
        scope = replace(scope, sizes=(*scope.sizes, self.builder))
        return run_cost.day_hours_checks(ledger.CostLedger(self.state), end - timedelta(days=days), end, self.home,
                                         self.builder, scope, self.limit)

    def notice_run(self, *, code: int = 0, stdout: str = 'SENT: fixture\n', sender_times_out: bool = False) -> int:
        def run(arguments: Sequence[str], *, capture_output: bool, text: bool, check: bool,
                timeout: float) -> subprocess.CompletedProcess[str]:
            self.assertEqual((capture_output, text, check, timeout), (True, True, False, send_budget.seconds(5)))
            self.assertGreater(timeout, 5)
            self.notice_commands.append(tuple(arguments))
            if sender_times_out:
                self.assertFalse((self.state / 'hours-gap-notices.json').exists())
                raise subprocess.TimeoutExpired(arguments, timeout)
            return subprocess.CompletedProcess(arguments, code, stdout, '')

        with (redirect_stderr(StringIO()), mock.patch.object(subprocess, 'run', run)):
            return aws_target.hours_gap_notices(self.settings, self.state, clock=self.clock)

    def test_box_hours_split_at_utc_midnight_and_count_only_confirmed_gates(self) -> None:
        two, one = self.day(2), self.day(1)
        # Half an hour before and after midnight, then one more hour; an open, an absent and another type do not count.
        self.gate(self.size, self.home, one - timedelta(minutes=30), 3600)
        self.gate(self.size, self.home, one + timedelta(hours=2), 3600)
        self.gate(self.size, self.home, one + timedelta(hours=4), 3600, ledger.GateRunning('i-fixture'))
        self.gate(self.size, self.home, one + timedelta(hours=6), 3600, ledger.GateAbsent(int(one.timestamp())))
        self.gate(self.size + 'x', self.home, one, 3600)
        boxes = run_cost.box_hours(ledger.CostLedger(self.state), two, self.day(0), self.home, self.builder)
        self.assertEqual(boxes, {(self.size, self.home, two.date().isoformat()): Decimal('0.5'),
                                 (self.size, self.home, one.date().isoformat()): Decimal('1.5'),
                                 (self.size + 'x', self.home, one.date().isoformat()): Decimal(1)})

    def test_day_and_window_checks_compare_bill_hours_with_box_hours(self) -> None:
        two, one = self.day(2), self.day(1)
        self.gate(self.size, self.home, two, 3600)
        self.gate(self.size, self.home, one, 3600)
        self.gate(self.size, self.other, one, 1800)
        within, above = Decimal(1) + self.limit / 2, Decimal(1) + self.limit * 2
        self.bill(two, {(self.size, self.home): within})
        # The second day bills the home region above the limit and the other region not at all.
        self.bill(one, {(self.size, self.home): above})
        self.gate(self.size, self.home, self.day(0), 600)
        checks = self.checks()
        self.assertEqual(checks[self.size, self.home, two.date().isoformat()],
                         run_cost.HoursCheck(within, Decimal(1), self.limit / 2, False))
        self.assertEqual(checks[self.size, self.home, one.date().isoformat()],
                         run_cost.HoursCheck(above, Decimal(1), self.limit * 2, True))
        self.assertEqual(checks[self.size, self.other, one.date().isoformat()],
                         run_cost.HoursCheck(Decimal(0), Decimal('0.5'), Decimal(1), True))
        # Today has no bill read yet, and the window ends at today's midnight like the bill split.
        self.assertNotIn((self.size, self.home, self.day(0).date().isoformat()), checks)
        end = datetime.fromtimestamp(self.clock.now, UTC)
        window = run_cost.hours_checks(ledger.CostLedger(self.state), end - timedelta(days=3), end, self.home,
                                       self.builder, run_cost.SpotHoursScope((self.size,), {}), self.limit)
        self.assertEqual(window[self.size, self.home],
                         run_cost.hours_check(within + above, Decimal(2), self.limit))
        self.assertEqual(window[self.size, self.other], run_cost.HoursCheck(Decimal(0), Decimal('0.5'), Decimal(1), True))

    def test_builder_running_time_counts_as_box_hours_of_the_builder_type(self) -> None:
        one = self.day(1)
        def observe(hours: float, state: ledger.InstanceState) -> None:
            self.observations.append(ledger.InstanceObservation(int((one + timedelta(hours=hours)).timestamp()), state, 'fixture'))
        # The builder runs 13:00-13:30, stops, then runs 14:00-22:00 with a repeated running read; it is stopped after.
        for hours, state in ((12, 'stopped'), (13, 'running'), (13.5, 'stopping'), (13.6, 'stopped'),
                             (14, 'running'), (18, 'running'), (22, 'stopped')):
            observe(hours, cast(ledger.InstanceState, state))
        self.gate(self.builder, self.home, one + timedelta(hours=1), 1800)
        self.bill(one, {(self.builder, self.home): Decimal('9')})
        check = self.checks()[self.builder, self.home, one.date().isoformat()]
        self.assertEqual(check.box_hours, Decimal('9'))
        self.assertFalse(check.hours_gap_over)
        # A builder still running at the window end counts until the window end.
        observe(23, 'running')
        boxes = run_cost.box_hours(ledger.CostLedger(self.state), one, self.day(0), self.home, self.builder)
        self.assertEqual(boxes[self.builder, self.home, one.date().isoformat()], Decimal('10'))

    def test_billed_hours_without_a_gate_box_are_over_and_unread_days_have_no_bill(self) -> None:
        one = self.day(1)
        self.bill(one, {(self.size, self.home): Decimal(1)})
        self.gate(self.size, self.other, self.day(2), 3600)
        checks = self.checks()
        self.assertEqual(checks[self.size, self.home, one.date().isoformat()],
                         run_cost.HoursCheck(Decimal(1), Decimal(0), None, True))
        self.assertEqual(checks[self.size, self.other, self.day(2).date().isoformat()],
                         run_cost.HoursCheck(None, Decimal(1), None, False))

    def test_hana_exposure_sets_the_upper_bound(self) -> None:
        day = self.day(1)
        at = int(day.timestamp())
        stop = at + 11232
        self.exposures.append(ledger.HanaComputeExposure(at, stop, 1, Decimal('.25'),
            ledger.HanaExposureClosed(stop), self.size, region=self.other))
        for billed, over in (('0.49', False), ('0', False), ('3.588', False), ('4.0', True)):
            with self.subTest(billed=billed):
                self.bills.clear()
                self.bill(day, {(self.size, self.other): Decimal(billed)} if billed != '0' else {})
                check = self.checks()[self.size, self.other, day.date().isoformat()]
                self.assertEqual((check.billed_hours, check.box_hours, check.hana_reserved_hours, check.hours_gap_over),
                                 (Decimal(billed), Decimal(0), Decimal('3.12'), over))

        self.gate(self.size, self.other, day, 3600)
        self.bills.clear()
        self.bill(day, {(self.size, self.other): Decimal('.84')})
        check = self.checks()[self.size, self.other, day.date().isoformat()]
        self.assertTrue(check.hours_gap_over)
        self.assertEqual(check.hours_gap_ratio, Decimal('.16'))

    def test_gate_only_hours_keep_both_limits(self) -> None:
        day = self.day(1)
        self.gate(self.size, self.home, day, 3600)
        for billed, over in (('0.84', True), ('0.85', False), ('1.15', False), ('1.16', True)):
            with self.subTest(billed=billed):
                self.bills.clear()
                self.bill(day, {(self.size, self.home): Decimal(billed)})
                check = self.checks()[self.size, self.home, day.date().isoformat()]
                self.assertEqual(check.hours_gap_over, over)
                self.assertEqual(check.hours_gap_ratio, abs(Decimal(billed) - 1))

    def test_gate_only_check_preserves_ratio_rounding(self) -> None:
        day = self.day(1)
        self.gate(self.size, self.home, day, 1)
        self.bill(day, {(self.size, self.home): Decimal('0.0003194444444444444444444444445')})
        check = self.checks()[self.size, self.home, day.date().isoformat()]
        self.assertTrue(check.hours_gap_over)

    def test_hana_midnight_split_and_open_exposure_past_its_permit(self) -> None:
        two, one = self.day(2), self.day(1)
        midnight = int(one.timestamp())
        self.exposures.extend((
            ledger.HanaComputeExposure(midnight - 900, midnight + 900, 1, Decimal('.25'),
                ledger.HanaExposureClosed(midnight + 900), self.size, region=self.other),
            ledger.HanaComputeExposure(midnight + 22 * 3600, midnight + 22 * 3600 + 60, 2, Decimal('.25'),
                instance_type=self.size, region=self.other),
        ))
        checks = self.checks()
        self.assertEqual(checks[self.size, self.other, two.date().isoformat()].hana_reserved_hours, Decimal('.25'))
        self.assertEqual(checks[self.size, self.other, one.date().isoformat()].hana_reserved_hours, Decimal('2.25'))

    def test_hana_types_share_regional_hours_and_count_the_first_row_per_generation(self) -> None:
        day = self.day(1)
        at = int(day.timestamp())
        primary, fallback, gate_only = 'm7a.2xlarge', 'm6a.4xlarge', 'm7a.8xlarge'
        first = ledger.HanaComputeExposure(at, at + 3600, 1, Decimal('.25'),
            ledger.HanaExposureClosed(at + 3600), primary, region=self.other)
        self.exposures.extend((first, replace(first, state=ledger.HanaExposureClosed(at + 7200)),
                               replace(first, generation=2, region=self.home),
                               replace(first, generation=3, instance_type='')))
        self.bill(day, {(primary, self.other): Decimal(1), (fallback, self.other): Decimal(1),
                        (fallback, self.home): Decimal(1), (gate_only, self.other): Decimal(1)})
        checks = self.checks()
        for size in (primary, fallback, self.size):
            check = checks[size, self.other, day.date().isoformat()]
            self.assertEqual(check.hana_reserved_hours, Decimal(2))
            self.assertFalse(check.hours_gap_over)
        self.assertTrue(checks[fallback, self.home, day.date().isoformat()].hours_gap_over)
        self.assertTrue(checks[gate_only, self.other, day.date().isoformat()].hours_gap_over)

    def test_fallback_hana_type_outside_the_gate_list_sends_a_notice_with_reserved_hours(self) -> None:
        fallback = 'm6a.4xlarge'
        text_setting(self.root / 'hana_aws.conf', 'mount_timeout_seconds', 'invalid')
        self.assertNotIn(fallback, aws_target.gate_sizes(conf.read(self.root / 'cloud_gate.conf')))
        day = self.day(1)
        at = int(day.timestamp())
        self.exposures.append(ledger.HanaComputeExposure(at, at + 3600, 1, Decimal('.25'),
            ledger.HanaExposureClosed(at + 3600), fallback, region=self.other))
        self.bill(day, {(fallback, self.other): Decimal(2)})
        self.assertEqual(self.notice_run(), 0)
        command, = self.notice_commands
        self.assertIn('The ledger also reserves 1.00 h of Hana box time in that region for that day.', command[-1])
        claims = ledger.mapping(cast(object, json.loads((self.state / 'hours-gap-notices.json').read_text())))
        self.assertEqual(claims['claimed'], [f'aws-hours-gap:{fallback}:{self.other}:{day.date().isoformat()}'])
        table = spot_table.spot_tables(self.state, self.clock.now, settings_path=self.settings).hours
        row = next(row for row in table.rows if row[0] == fallback)
        self.assertEqual(row[table.headers.index(self.other)], '2.0/0.0 + 1.00 reserved (100%) over')

    def test_unusable_hana_size_setting_preserves_gate_notices_and_table(self) -> None:
        text_setting(self.root / 'hana_aws.conf', 'fallback_instance_types', 'm6a.4xlarge,')
        day = self.day(1)
        at = int(day.timestamp())
        self.gate(self.size, self.other, day, 3600)
        self.exposures.append(ledger.HanaComputeExposure(at, at + 3600, 1, Decimal('.25'),
            ledger.HanaExposureClosed(at + 3600), self.size, region=self.other))
        self.bill(day, {(self.size, self.other): Decimal(2)})
        self.assertEqual(self.notice_run(), 0)
        command, = self.notice_commands
        self.assertIn('The gap is 100%.', command[-1])
        self.assertNotIn('Hana box time', command[-1])
        table = spot_table.spot_tables(self.state, self.clock.now, settings_path=self.settings).hours
        row, = table.rows
        self.assertEqual(row[table.headers.index(self.other)], '2.0/1.0 (100%) over')

    def test_gap_sends_one_staff_message_and_no_user_push(self) -> None:
        one = self.day(1)
        self.gate(self.size, self.home, one, 3600)
        self.gate(self.size, self.other, one, 3600)
        self.bill(one, {(self.size, self.home): Decimal(1) + self.limit * 2, (self.size, self.other): Decimal(1)})
        self.assertEqual(self.notice_run(), 0)
        day = one.date().isoformat()
        self.assertEqual(len(self.notice_commands), 1)
        self.assertEqual(self.notice_commands[0][2:], (
            '--to', 'staff:a898cb38-dbac-4751-b72b-d0b14ab9c38d', '--from', 'AWS hours check',
            '--timeout', '5', '--text',
            'AWS Spot hours differ from the cost ledger.\n'
            + f'On {day} UTC, AWS billed 1.30 h of {self.size} Spot in {self.home}.\n'
            + 'The cost ledger records 1.00 h for that size, region, and day.\n'
            + 'The gap is 30%. The limit is 15%.\n'
            + 'File an investigation for this gap and add it to the list.',
        ))
        self.assertTrue(self.notice_commands[0][1].endswith('/message/send.py'))
        self.assertEqual(self.notice_run(), 0)
        self.assertEqual(len(self.notice_commands), 1)

    def test_spot_table_shows_billed_over_box_hours_with_the_gap(self) -> None:
        one, two = self.day(1), self.day(2)
        gpu = conf.read_or_repository('cloud_gate').words('gpu_instance_types')[0]
        self.gate(self.size, self.home, one, 3600)
        self.gate(self.size, self.other, one, 1800)
        self.gate(gpu, self.home, two, 3600)
        self.bill(one, {(self.size, self.home): Decimal(1) + self.limit * 2})
        tables = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings)
        columns = tables.hours.headers
        cells = {row[0]: dict(zip(columns[1:], row[1:])) for row in tables.hours.rows}
        self.assertEqual(set(cells), {self.size, gpu})
        self.assertEqual(cells[self.size][self.home], f'{Decimal(1) + self.limit * 2:.1f}/1.0 ({self.limit * 2:.0%}) over')
        # The other region has no Spot hours on the billed day.
        self.assertEqual(cells[self.size][self.other], '0.0/0.5 (100%) over')
        self.assertEqual(cells[gpu][self.home], 'not billed')
        self.assertIn(f'above {self.limit:.0%}', tables.hours_note)
        self.assertEqual(tables.hours.title, 'Billed hours / box hours')

    def test_spot_table_keeps_gate_hours_and_shows_the_hana_allowance(self) -> None:
        day = self.day(1)
        at = int(day.timestamp())
        self.gate(self.size, self.other, day, 3600)
        self.exposures.append(ledger.HanaComputeExposure(at, at + 7200, 1, Decimal('.25'),
            ledger.HanaExposureClosed(at + 7200), self.size, region=self.other))
        self.bill(day, {(self.size, self.other): Decimal(3)})
        table = spot_table.spot_tables(self.state, self.clock.now, settings_path=self.settings).hours
        row = next(row for row in table.rows if row[0] == self.size)
        self.assertEqual(row[table.headers.index(self.other)], '3.0/1.0 + 2.00 reserved (0%)')

    def test_failed_notice_stays_pending_and_old_days_leave_the_claims(self) -> None:
        one = self.day(1)
        self.gate(self.size, self.home, one, 3600)
        self.bill(one, {(self.size, self.home): Decimal(1) + self.limit * 2})
        self.assertEqual(self.notice_run(code=3, stdout='FAILED: fixture\n'), 1)
        claims = self.state / 'hours-gap-notices.json'
        self.assertEqual(cast(dict[str, list[str]], json.loads(claims.read_text()))['claimed'], [])
        self.assertEqual(self.notice_run(), 0)
        self.assertEqual(len(self.notice_commands), 2)
        self.assertEqual(self.notice_commands[0], self.notice_commands[1])
        self.assertEqual(len(cast(dict[str, list[str]], json.loads(claims.read_text()))['claimed']), 1)
        # After the window passes the day, its claim is dropped.
        self.clock.advance((conf.read(self.settings).whole('run_cost_days') + 1) * 86400)
        self.assertEqual(self.notice_run(), 0)
        self.assertEqual(cast(dict[str, list[str]], json.loads(claims.read_text()))['claimed'], [])

    def test_queued_notice_saves_the_claim(self) -> None:
        one = self.day(1)
        self.bill(one, {(self.size, self.home): Decimal(1)})
        self.assertEqual(self.notice_run(code=1, stdout='QUEUED: fixture\n'), 0)
        claims = self.state / 'hours-gap-notices.json'
        self.assertEqual(cast(dict[str, list[str]], json.loads(claims.read_text()))['claimed'],
                         [f'aws-hours-gap:{self.size}:{self.home}:{one.date().isoformat()}'])
        self.assertEqual(self.notice_run(), 0)
        self.assertEqual(len(self.notice_commands), 1)

    def test_timed_out_notice_never_saves_the_claim(self) -> None:
        one = self.day(1)
        self.bill(one, {(self.size, self.home): Decimal(1)})
        self.assertEqual(self.notice_run(sender_times_out=True), 1)
        claims = self.state / 'hours-gap-notices.json'
        self.assertEqual(cast(dict[str, list[str]], json.loads(claims.read_text()))['claimed'], [])
        self.assertEqual(self.notice_run(), 0)
        self.assertEqual(len(self.notice_commands), 2)

    def test_hourly_runs_the_snapshot_and_the_hours_check_and_keeps_the_worse_status(self) -> None:
        # buildlog hourly calls aws_target.hourly; a failed snapshot must not skip the hours check.
        for snapshot_status, check_status in ((1, 0), (0, 1), (0, 0)):
            with (mock.patch.object(aws_target, 'snapshot', return_value=snapshot_status) as read,
                  mock.patch.object(aws_target, 'hours_gap_notices', return_value=check_status) as check):
                self.assertEqual(aws_target.hourly(self.settings, self.state), max(snapshot_status, check_status))
            read.assert_called_once_with(self.settings, self.state)
            check.assert_called_once_with(self.settings, self.state)


if __name__ == '__main__':
    _ = unittest.main()

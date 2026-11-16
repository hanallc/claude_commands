"""Test hourly Spot snapshots with private files and fake AWS reads."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import cast, override
import unittest
from unittest import mock

from scripts.aws_target import admin_secrets, aws_page, aws_target, deploy, ledger, run_cost, spot_table
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.ledger_test_support import CostFixture, text_setting
from scripts.lib import atomic_file, conf
from scripts.notify import push_texts
from scripts.review_links import console_theme, review_link


def table_texts(tables: spot_table.SpotTables) -> tuple[str, ...]:
    """Every snapshot line, then every title, header and cell of the tables, for text checks."""
    every = (*tables.prices, *tables.quotas, *(table for costs in tables.runs
             for table in (*costs.tables, *costs.prices, costs.refused, costs.launched)), tables.hours)
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
        source = Path(__file__).resolve().parents[2] / 'config/cloud_gate.conf'
        values_path = self.root / 'cloud_gate.conf'
        _ = values_path.write_bytes(source.read_bytes())
        # Use different regions and types to detect dependencies on shipped values.
        text_setting(values_path, 'spot_candidate_regions', 'us-east-1 us-west-1 us-east-1')
        text_setting(values_path, 'instance_types', 'c7a.4xlarge m7a.2xlarge')
        text_setting(values_path, 'price_compare_instance_types', 'm7a.8xlarge c7a.4xlarge')
        text_setting(values_path, 'gpu_instance_types', 'g6.2xlarge g4dn.xlarge')
        values = conf.read(values_path)
        self.regions = tuple(dict.fromkeys(values.words('spot_candidate_regions')))
        self.standard = values.words('instance_types')
        self.compare = tuple(size for size in values.words('price_compare_instance_types') if size not in self.standard)
        self.gpu = values.words('gpu_instance_types')
        self.sizes = (*self.standard, *self.compare, *self.gpu)
        self.quota = deploy.largest_gate_vcpus(self.sizes) * 3 + 1
        self.enterContext(mock.patch.dict(os.environ, {
            'AWS_ACCESS_KEY_ID': 'fixture-admin-id', 'AWS_SECRET_ACCESS_KEY': 'fixture-admin-secret',
            'AWS_SESSION_TOKEN': 'fixture-token', 'AWS_CONFIG_FILE': '/fixture/admin-config',
            'AWS_PROFILE': 'fixture-admin', 'AWS_DEFAULT_PROFILE': 'fixture-admin',
            'AWS_SHARED_CREDENTIALS_FILE': str(self.root / 'private-scaler'),
        }))

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

        # The fixture lowest zone price is 0.25 USD/h, so 6 minutes cost 0.0250 and 12 minutes 0.0500.
        # One refused request came before one launch of the fast type at home; the other region has no requests.
        # No bill covers the runs yet; the other region has no runs of its own.
        self.assertEqual(tables[fast].rows, (
            (home, '0.2500', '6.0', '1', '0.0250', 'bill due', '1 bill due', '0'),
            (other, '0.2500', '6.0', '1 elsewhere', '0.0250', 'no runs', 'no runs', '0')))
        self.assertEqual([row[:5] for row in tables[slow].rows], [(home, '0.2500', '12.0', '1', '0.0500'),
                                                                 (other, '0.2500', '12.0', '1 elsewhere', '0.0500')])
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
                # The Cost per gate view shows the same estimate; a GPU type without runs has none.
                standard, gpu = costs.prices
                self.assertEqual((standard.title, standard.headers[1]), (f'Standard cost per {costs.scope.noun} (USD; estimate)', size))
                self.assertEqual((standard.rows[0][1], f'{standard.values[0][1]:.4f}'), (usd, usd))
                self.assertEqual({cell for row in gpu.rows for cell in row[1:]}, {spot_table.NOT_SOLD})
                self.assertEqual({value for row in gpu.values for value in row}, {None})

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
        self.assertEqual(data, aws_page.page_data(tables, int(self.clock.now)))
        self.assertEqual([(entry['scope'], entry['label']) for entry in data['runs']],
                         [('merge-gate', 'Merge gates'), ('targeted-test', 'Targeted runs'), ('both', 'Both')])
        self.assertEqual([table['right'] for table in data['prices']],
                         [list(range(1, len(types) + 1)) for types in ((*self.standard, *self.compare), self.gpu)])
        # The fixture lowest zone price is 0.25 USD/h for every type and region; the region column has no number.
        prices = data['prices'][0]
        self.assertEqual([row[0] for row in prices['rows']], list(self.regions))
        self.assertEqual(prices['values'], [[None, *(0.25 for _ in prices['headers'][1:])] for _ in self.regions])
        # The run tables leave out the Spot price and the estimate, which the Cost per gate view shows.
        run_headers = data['runs'][0]['tables'][0]['headers']
        self.assertNotIn(spot_table.SPOT_HEADER, run_headers)
        self.assertNotIn(spot_table.ESTIMATE_HEADER, run_headers)
        self.assertEqual(data['runs'][0]['tables'][0]['right'], list(range(1, len(run_headers))))
        self.assertEqual([table['title'] for table in data['runs'][0]['prices']],
                         ['Standard cost per merge gate (USD; estimate)', 'GPU cost per merge gate (USD; estimate)'])
        self.assertEqual([table['not_sold'] for table in data['prices']], [[], []])
        self.assertEqual([table['region_axis'] for table in (*data['prices'], *data['quotas'])], ['rows'] * 4)
        self.assertEqual(data['regions'], sorted(self.regions, key=aws_page.region_filter_key))
        self.assertEqual([line['text'] for line in data['snapshot']], [text for _region, text in tables.snapshot_rows])
        summaries = [entry['estimates'][0]['summary'] if entry['estimates'] else entry['no_estimate'] for entry in data['runs']]
        self.assertEqual(summaries, [costs.summary_of(costs.cheapest) for costs in tables.runs])

    def test_region_filter_sorts_each_group_alphabetically_in_the_group_order(self) -> None:
        regions = ('us-west-2', 'sa-east-1', 'me-south-1', 'ap-south-1', 'eu-west-1', 'ca-central-1',
                   'us-east-2', 'ap-northeast-1', 'eu-central-1', 'af-south-1')
        self.assertEqual(sorted(regions, key=aws_page.region_filter_key),
                         ['us-east-2', 'us-west-2', 'eu-central-1', 'eu-west-1', 'ap-northeast-1', 'ap-south-1',
                          'ca-central-1', 'sa-east-1', 'af-south-1', 'me-south-1'])

    def test_page_data_cannot_end_its_script_element(self) -> None:
        folder = self.root / 'page'
        cell = 'a</script><b>'
        tables = spot_table.spot_tables(self.state, int(self.clock.now), settings_path=self.settings)
        prices = (replace(tables.prices[0], title=cell), *tables.prices[1:])
        with mock.patch.object(spot_table, 'spot_tables', return_value=replace(tables, prices=prices)):
            source = aws_page.publish(self.state, int(self.clock.now), folder).read_text()
        payload = source.split('<script id="aws-prices-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        self.assertEqual(cast(aws_page.PageData, json.loads(payload))['prices'][0]['title'], cell)

    def test_paid_cells_name_why_no_bill_counts_and_the_cheapest_is_a_placement_type(self) -> None:
        self.assertEqual(self.snapshot(), 0)
        size, = self.compare
        home = self.regions[0]
        events: list[dict[str, object]] = []
        # A short targeted run of a type that placement never picks: the cheapest estimate, but not a candidate.
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

    def test_scaler_policy_allows_snapshot_reads_in_all_regions(self) -> None:
        document = ledger.mapping(cast(object, json.loads(deploy.TEMPLATE.read_text())))
        resources = ledger.mapping(document['Resources'])
        policy = ledger.mapping(ledger.mapping(resources['GateGpuTrialScalerPolicy'])['Properties'])
        statements = ledger.rows(ledger.mapping(policy['PolicyDocument']), 'Statement')
        self.assertEqual(self.snapshot(), 0)
        # Derive IAM actions from every API request in the snapshot path.
        actions = {arguments[5].replace('-', '') + ':' + ''.join(
            word.capitalize() for word in arguments[6].split('-')) for arguments in self.calls}
        for action in actions:
            self.assertTrue(any(action in cast(list[str], row['Action']) and row['Resource'] == '*'
                                and row['Effect'] == 'Allow' and 'Condition' not in row for row in statements))

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
    bills: list[ledger.ChargeRead] = []
    notices: list[tuple[str, push_texts.RenderedPush]] = []
    notice_accepted: bool = True

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        configuration = conf.read(self.settings)
        self.home = configuration.text('region')
        self.other = next(region for region in ledger.REGION_USAGE_PREFIXES if region != self.home)
        self.size = conf.read_or_repository('cloud_gate').words('instance_types')[0]
        self.builder = configuration.text('instance_type')
        self.observations = []
        self.limit = conf.decimal(configuration.text('billed_hours_gap_max_ratio'))
        self.reservations, self.bills, self.notices = [], [], []
        self.notice_accepted = True

        def evidence(_book: ledger.CostLedger) -> ledger.LedgerEvidence:
            return ledger.LedgerEvidence(observations=list(self.observations), gate_reservations=list(self.reservations))

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
        return run_cost.day_hours_checks(ledger.CostLedger(self.state), end - timedelta(days=days), end, self.home,
                                         self.builder, (self.size, self.builder), self.limit)

    def notify(self, key: str, push: push_texts.RenderedPush, _timeout: float) -> bool:
        self.notices.append((key, push))
        return self.notice_accepted

    def notice_run(self) -> int:
        with redirect_stderr(StringIO()):
            return aws_target.hours_gap_notices(self.settings, self.state, clock=self.clock, notify=self.notify)

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
                                       self.builder, (self.size,), self.limit)
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

    def test_gap_sends_one_notice_per_type_region_and_day(self) -> None:
        one = self.day(1)
        self.gate(self.size, self.home, one, 3600)
        self.gate(self.size, self.other, one, 3600)
        self.bill(one, {(self.size, self.home): Decimal(1) + self.limit * 2, (self.size, self.other): Decimal(1)})
        self.assertEqual(self.notice_run(), 0)
        key = f'aws-hours-gap:{self.size}:{self.home}:{one.date().isoformat()}'
        self.assertEqual([sent for sent, _push in self.notices], [key])
        push = self.notices[0][1]
        self.assertEqual(push.id, 'aws.hours_gap')
        self.assertIn(f'AWS billed {Decimal(1) + self.limit * 2:.2f} h of {self.size} Spot in {self.home}', push.body)
        self.assertIn(f'The gap is {self.limit * 2:.0%}, above the {self.limit:.0%} limit.', push.body)
        # The next hourly run sends nothing again for the same day.
        self.assertEqual(self.notice_run(), 0)
        self.assertEqual(len(self.notices), 1)

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

    def test_failed_notice_stays_pending_and_old_days_leave_the_claims(self) -> None:
        one = self.day(1)
        self.gate(self.size, self.home, one, 3600)
        self.bill(one, {(self.size, self.home): Decimal(1) + self.limit * 2})
        self.notice_accepted = False
        self.assertEqual(self.notice_run(), 1)
        self.notice_accepted = True
        self.assertEqual(self.notice_run(), 0)
        self.assertEqual(len(self.notices), 2)
        claims = self.state / 'hours-gap-notices.json'
        self.assertEqual(len(cast(dict[str, list[str]], json.loads(claims.read_text()))['claimed']), 1)
        # After the window passes the day, its claim is dropped.
        self.clock.advance((conf.read(self.settings).whole('run_cost_days') + 1) * 86400)
        self.assertEqual(self.notice_run(), 0)
        self.assertEqual(cast(dict[str, list[str]], json.loads(claims.read_text()))['claimed'], [])

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

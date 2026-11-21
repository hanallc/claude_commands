"""Check the published AWS table and quota case instructions."""

from collections.abc import Mapping
from contextlib import redirect_stderr
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import unescape
from io import StringIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import cast, override
import unittest
from unittest import mock

from scripts.aws_target import (
    aws_page, cloud_gate, console_help, ledger, ledger_test_support, quota_requests, run_cost, spend, spot_table,
)
from scripts.aws_target import config as aws_config
from scripts.aws_target.aws_cli import AwsCli
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED
from scripts.build_targets import target_config
from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.review_links import page_frame
from scripts.review_links.test_accounts_page import firefox_probe_results


class AwsPageQuotaHelpTests(ledger_test_support.CostFixture):
    def test_quota_help_is_closed_and_contains_the_ordered_steps_and_case_link(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        with ledger.CostLedger(self.state).transaction():
            pass
        page = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings)
        section = page.read_text().split('<section class="card" id="prices-quotas">', 1)[1].split('</section>', 1)[0]
        details = re.search(r'<details id="quota-help">(.*?)</details>\s*$', section, flags=re.S)
        assert details is not None
        block = details[1]
        self.assertIn('<summary>How to open the quota cases in AWS</summary>', block)
        self.assertRegex(block, r'<ol><li>.*</li></ol>')
        steps = tuple(match[1] for match in re.finditer(r'<li>(.*?)</li>', block))
        self.assertEqual(tuple(unescape(re.sub(r'<[^>]+>', '', step)) for step in steps), console_help.QUOTA_CASE_STEPS)
        url = console_help.CASE_LIST_URL
        self.assertIn(f'<a href="{url}">{url}</a>', steps[2])
        self.assertEqual(block.count('<a href='), 1)
        self.assertEqual(re.findall(r'<p>(.*?)</p>', unescape(block)), [console_help.WRONG_ACCOUNT])

    def test_quota_help_uses_the_shared_words(self) -> None:
        with (mock.patch.object(console_help, 'QUOTA_CASE_STEPS', ('Open the test console.',)),
              mock.patch.object(console_help, 'WRONG_ACCOUNT', 'Choose the test account.')):
            self.assertEqual(aws_page.quota_help(), '<ol><li>Open the test console.</li></ol>'
                             + '\n<p>Choose the test account.</p>')

    def test_quota_request_uses_plain_labels_when_values_are_missing(self) -> None:
        applied = aws_page.AppliedQuotaUnavailable.NOT_MEASURED
        for status, expected in (
            ('APPROVED', {'request': 'not measured', 'detail': 'closed, quota unknown'}),
            ('IN_PROGRESS', {'request': 'open'}),
            ('FIXTURE_UNKNOWN', {'request': 'not measured', 'detail': 'status unknown'}),
        ):
            with self.subTest(status=status):
                request = quota_requests.quota_request(status, Decimal(32), None, None)
                self.assertEqual(aws_page.quota_request_columns(request, applied, 0),
                                 {'wanted': '32', 'age': 'age unknown', **expected})
        reading = quota_requests.QuotaRequestReadFailed('fixture private diagnostic')
        self.assertEqual(aws_page.quota_request_columns(reading, applied, 0),
                         {'wanted': '—', 'request': 'not measured', 'age': '—', 'detail': 'request read failed'})

    def test_future_quota_request_dates_have_unknown_age(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        future = '2026-01-22T12:00:00Z'
        past = '2026-01-14T12:00:00Z'
        for created, updated in ((future, past), (None, future)):
            with self.subTest(created=created, updated=updated):
                request = quota_requests.quota_request('PENDING', Decimal(32), created, updated)
                self.assertEqual(aws_page.quota_request_age(request, now), 'age unknown')


def configure_page_costs(fixture: ledger_test_support.CostFixture) -> None:
    fixture.costs({'spend_pace_days': '3', 'run_cost_days': '5', 'billed_hours_gap_max_ratio': '0.2'})
    fixture.configure('hours_table_open_row_limit=6')
    fixture.configure_gate("""
regions=us-east-1
spot_candidate_regions=us-east-1
instance_types=m7a.8xlarge
price_compare_instance_types=c7a.8xlarge
gpu_instance_types=g6.2xlarge
hana_gpu_instance_types=g6.2xlarge
gate_wait_usd_per_minute=0
unmeasured_region_extra_seconds={}
# whole: 1..
lifetime_minutes=30
# whole: 1..
gpu_spot_quota_request_vcpus=32
""")


def save_published_bill(book: ledger.CostLedger, bill: ledger.ChargeRead,
                        reads: tuple[tuple[int, Decimal], ...] = ()) -> None:
    with book.transaction() as state:
        charges = book.directory / 'charges'
        charges.mkdir(exist_ok=True)
        record = ledger.charge_record(bill)
        if reads:
            record['amount_reads'] = [{'at': at, 'actual_usd': str(amount)} for at, amount in reads]
        _ = (charges / (bill.day + '.json')).write_text(json.dumps(record))
        state.reconciliations = [row for row in state.reconciliations if row.period_start != bill.period_start]
        state.reconciliations.append(ledger.CostReconciliation(
            bill.at, bill.period_start, bill.period_end, bill.actual_usd, 'Cost Explorer', bill.query_usd))
        state.charge_refresh_at = max(state.charge_refresh_at, bill.at)


class PageCostFixture(ledger_test_support.CostFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        configure_page_costs(self)
        with ledger.CostLedger(self.state).transaction():
            pass


class AwsGpuQuotaSummaryTests(PageCostFixture):
    now: int = ledger_test_support.instant('2026-01-15T12:00:00Z')

    def quota_summary(self, applied: dict[str, str], history: dict[str, dict[str, str]]) -> aws_page.GpuQuotaSummary:
        _ = (self.state / 'spot-prices.json').write_text(json.dumps({
            'prices': [],
            'regions': [{'region': region, 'read_at': self.now, 'standard_vcpus': '64', 'gpu_vcpus': vcpus}
                        for region, vcpus in applied.items()],
            'quota_requests': [{'region': region, 'standard': {'state': 'no_request'}, 'gpu': reading}
                               for region, reading in history.items()],
        }))
        tables = spot_table.spot_tables(self.state, self.now, settings_path=self.settings)
        return aws_page.quota_history_data(tables, self.now)['gpu_summary']

    def test_applied_quota_counts_without_request_history(self) -> None:
        for history in (
            {},
            {'us-east-1': {'state': 'no_request'}},
            {'us-east-1': {'state': 'not_measured', 'reason': 'fixture'}},
            {'us-east-1': {'state': 'read_failed', 'reason': 'fixture'}},
        ):
            with self.subTest(history=history):
                self.assertEqual(self.quota_summary({'us-east-1': '32'}, history), {
                    'target_vcpus': 32,
                    'regions_by_outcome': {'granted': ['us-east-1'], 'open': [], 'closed, not granted': []},
                })

    def test_summary_counts_regions_in_the_quota_table(self) -> None:
        config = self.root / 'cloud_gate.conf'
        ledger_test_support.text_setting(config, 'regions', 'us-east-1 us-east-2')
        ledger_test_support.text_setting(config, 'spot_candidate_regions', 'us-east-1 eu-north-1')
        request = {'state': 'requested', 'status': 'PENDING', 'requested_vcpus': '32'}
        self.assertEqual(self.quota_summary(
            {'us-east-1': '32', 'eu-north-1': '8'},
            {'us-east-1': request, 'eu-north-1': request},
        ), {'target_vcpus': 32,
            'regions_by_outcome': {'granted': ['us-east-1'], 'open': ['eu-north-1'], 'closed, not granted': []}})


class AwsPageLaunchStateTests(PageCostFixture):
    now: int = ledger_test_support.instant('2026-01-15T12:00:00Z')

    def published_launch_state(self, *events: Mapping[str, object],
                               default_attempt_ids: bool = True) -> dict[str, object]:
        rows = ({'instance_type': 'm7a.8xlarge', 'region': 'us-east-1', 'mode': 'gate',
                 **({'attempt': f'fixture-{index}'} if default_attempt_ids else {}),
                 **event} for index, event in enumerate(events))
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
        page = aws_page.publish(self.state, self.now, self.root / 'page', settings_path=self.settings)
        source = page.read_text()
        self.assertNotIn('private diagnostic', source)
        payload = source.split('<script id="aws-prices-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        return ledger.mapping(ledger.mapping(cast(object, json.loads(payload)))['launch_state'])

    def test_account_refusals_count_from_the_last_good_launch(self) -> None:
        normal_codes = ('SpotMaxPriceTooLow', 'VcpuLimitExceeded', 'MaxSpotInstanceCountExceeded',
                        'InsufficientSpotCapacity', 'InsufficientInstanceCapacity')
        state = self.published_launch_state(
            {'time': self.now - 600, 'result': 'refused', 'error_code': 'Blocked'},
            {'time': self.now - 500, 'result': 'launched'},
            {'time': self.now - 240.25, 'result': 'refused', 'error_code': 'ExpiredToken'},
            {'time': self.now - 180, 'result': 'refused', 'error_code': 'Blocked', 'reason': 'private diagnostic'},
            *({'time': self.now - 120 + index, 'result': 'refused', 'error_code': code}
              for index, code in enumerate(normal_codes)),
            {'time': self.now - 10, 'result': 'finished'},
        )
        self.assertEqual(state, {'kind': 'refused', 'code': 'Blocked', 'reason': 'The account is blocked.',
                                 'first_at': self.now - 240.25, 'count': 2, 'last_good_at': self.now - 500,
                                 'latest_attempt_at': self.now - 116})

    def test_a_good_launch_clears_the_refusal_run(self) -> None:
        state = self.published_launch_state(
            {'time': self.now - 180, 'result': 'refused', 'error_code': 'InvalidClientTokenId'},
            {'time': self.now - 120, 'result': 'launched'},
            {'time': self.now - 60, 'result': 'refused', 'error_code': 'InsufficientSpotCapacity'},
        )
        self.assertEqual(state, {'kind': 'working', 'last_good_at': self.now - 120,
                                 'latest_attempt_at': self.now - 60})

    def test_other_refusals_replace_the_working_state_without_publishing_diagnostics(self) -> None:
        for code, shown in (({}, 'unknown code'), ({'error_code': 'fixture-private-id'}, 'unknown code'),
                            ({'error_code': 'UnauthorizedOperation'}, 'UnauthorizedOperation')):
            with self.subTest(code=code):
                state = self.published_launch_state(
                    {'time': self.now - 120, 'result': 'launched'},
                    {'time': self.now - 60, 'result': 'refused', 'reason': 'private diagnostic', **code})
                self.assertEqual((state['kind'], state['code']), ('refused', shown))
                self.assertEqual(state['reason'], 'The launch could not proceed.')
                self.assertNotIn('fixture-private-id', json.dumps(state))

    def test_prelaunch_credential_fallback_counts_and_local_fallback_does_not(self) -> None:
        state = self.published_launch_state(
            {'time': self.now - 120, 'result': 'launched'},
            {'time': self.now - 90, 'result': 'fallback', 'error_code': 'ExpiredToken', 'reason': 'private diagnostic'},
            {'time': self.now - 60, 'result': 'fallback', 'error_code': 'Spot'},
        )
        self.assertEqual((state['kind'], state['code'], state['count']), ('refused', 'ExpiredToken', 1))

    def test_fallback_after_launch_does_not_report_a_refusal(self) -> None:
        launched_at = self.now - 120
        state = self.published_launch_state(
            {'attempt': 'gate-a', 'time': launched_at, 'result': 'launched'},
            {'attempt': 'gate-a', 'time': self.now - 60, 'result': 'fallback', 'error_code': 'AccessDenied',
             'reason': 'GetObject failed: private diagnostic'},
        )
        self.assertEqual(state, {'kind': 'working', 'last_good_at': launched_at, 'latest_attempt_at': launched_at})

    def test_fallback_after_refusal_counts_only_the_gate_attempt(self) -> None:
        refused_at = self.now - 90
        refusal = {'attempt': 'gate-b', 'time': refused_at, 'result': 'refused',
                   'error_code': 'ServiceUnavailable'}
        fallback = {**refusal, 'time': self.now - 60, 'result': 'fallback'}
        for worker_rows in ((refusal,), (refusal, {**refusal, 'time': refused_at + 1})):
            with self.subTest(worker_rows=len(worker_rows)):
                state = self.published_launch_state(*worker_rows, fallback)
                self.assertEqual((state['kind'], state['code'], state['count']), ('refused', 'ServiceUnavailable', 1))
                self.assertEqual(state['latest_attempt_at'], worker_rows[-1]['time'])

    def test_fallback_without_attempt_id_keeps_the_good_launch(self) -> None:
        launched_at = self.now - 120
        for identity in ({}, {'attempt': ''}):
            with self.subTest(identity=identity):
                state = self.published_launch_state(
                    {'time': launched_at, 'result': 'launched', **identity},
                    {'time': self.now - 60, 'result': 'fallback', 'error_code': 'AccessDenied', **identity},
                    default_attempt_ids=False,
                )
                self.assertEqual(state, {'kind': 'working', 'last_good_at': launched_at,
                                         'latest_attempt_at': launched_at})

    def test_fallback_without_attempt_id_does_not_count_the_refusal_twice(self) -> None:
        refused_at = self.now - 120
        for identity in ({}, {'attempt': ''}):
            with self.subTest(identity=identity):
                refusal = {'time': refused_at, 'result': 'refused', 'error_code': 'ServiceUnavailable', **identity}
                state = self.published_launch_state(
                    refusal,
                    {**refusal, 'time': self.now - 60, 'result': 'fallback'},
                    default_attempt_ids=False,
                )
                self.assertEqual((state['kind'], state['count'], state['latest_attempt_at']),
                                 ('refused', 1, refused_at))

    def test_state_without_a_good_launch(self) -> None:
        self.assertEqual(self.published_launch_state(), {'kind': 'no-evidence'})
        state = self.published_launch_state(
            {'time': self.now - 60, 'result': 'refused', 'error_code': 'AuthFailure'})
        self.assertEqual(state, {'kind': 'refused', 'code': 'AuthFailure',
                                 'reason': 'AWS could not authenticate the launch credentials.',
                                 'first_at': self.now - 60, 'count': 1, 'latest_attempt_at': self.now - 60})

    def test_publisher_resolves_one_file_for_spend_and_tables(self) -> None:
        account = self.root / 'account' / 'aws_target.conf'
        account.parent.mkdir()
        _ = account.write_bytes(self.settings.read_bytes())
        ledger_test_support.numeric_settings(account, {'daily_limit_usd': '7', 'run_cost_days': '11'})
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(now, (), 'fixture', builder_removed=True))
        with mock.patch.object(aws_config, 'account_settings_path', return_value=account) as resolve:
            page = aws_page.publish(self.state, now, self.root / 'page').read_text()
        resolve.assert_called_once_with()
        self.assertIn('of the $7.00 daily limit', page)
        self.assertIn('11 days', page)

    def test_publisher_settings_have_fixed_fixture_values(self) -> None:
        settings = conf.read(self.settings)
        self.assertEqual((settings.whole('spend_pace_days'), settings.whole('run_cost_days'),
                          settings.number('billed_hours_gap_max_ratio'), settings.whole('hours_table_open_row_limit')),
                         (3, 5, 0.2, 6))
        self.assertEqual(conf.read('cloud_gate').words('spot_candidate_regions'), ('us-east-1',))


class AwsPageSettledAmountsTests(PageCostFixture):
    def test_spend_shows_the_settled_summary_and_changed_amounts(self) -> None:
        self.text_setting('accounting_zone', 'America/Los_Angeles')
        checked = ledger_test_support.instant('2026-11-19T00:15:00Z')
        start = ledger_test_support.instant('2026-11-04T00:00:00Z')
        end = start + 9 * 86400
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (), 'fixture', builder_removed=True))
        settled = Decimal('7.73')
        unsettled = ledger.ChargeRead('2026-11-13', checked, end, end + 86400, settled, Decimal(0), Decimal(0))
        save_published_bill(book, unsettled)

        def settled_notes() -> list[str]:
            page = aws_page.publish(self.state, checked, self.root / 'page', settings_path=self.settings)
            payload = page.read_text().split('<script id="aws-prices-data" type="application/json">', 1)[1]
            data = cast(aws_page.PageData, json.loads(payload.split('</script>', 1)[0]))
            return data['settled_notes']

        self.assertEqual(settled_notes(), [])
        reads = ledger_test_support.charge_readings(start, end, settled, Decimal(0))
        history = ((checked - 2 * 86400, settled), (checked - 86400, settled))
        for bill in reads:
            save_published_bill(book, replace(bill, at=checked - 86400), history)
        self.assertEqual(settled_notes(), [
            'Settled AWS amounts: 9 of 9 did not change after they settled (checked Nov 19).',
        ])

        revised = Decimal('7.81')
        latest = replace(reads[-1], at=checked, actual_usd=revised)
        save_published_bill(book, latest, (*history, (checked, revised)))
        self.assertEqual(settled_notes(), [
            'Settled AWS amounts: 8 of 9 did not change after they settled (checked Nov 19).',
            '2026-11-12 changed from USD 7.73 to USD 7.81 (+0.08).',
        ])


class AwsPageSpendReferenceTests(ledger_test_support.SpendReferenceFixture):
    """Record the existing spend and publication behavior for Phase 1."""

    midnight: int = ledger_test_support.instant('2026-01-15T00:00:00Z')
    book: ledger.CostLedger  # pyright: ignore[reportUninitializedInstanceVariable]
    folder: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        configure_page_costs(self)
        self.costs({'fallback_usd_per_hour': '1', 'limit_reserve_usd': '0',
                    'cost_explorer_interval_seconds': '60'})
        self.book = ledger.CostLedger(self.state)
        self.book.record_inventory(ledger.ResourceInventory(self.midnight - 86400, (), 'fixture inventory',
                                                            builder_removed=True))
        self.book.observe(ledger.InstanceObservation(self.midnight - 86400, 'terminated', 'fixture observation'))
        _ = self.enterContext(mock.patch.object(ledger, 'send_settled_changes', return_value=True))
        self.folder = self.root / 'page'

    def refresh(self, now: int, amount: str) -> None:
        start = self.midnight - 86400
        usd = Decimal(amount)
        reading = ledger.ChargeRead('2026-01-14', now, start, self.midnight, usd, Decimal(0), Decimal(0),
                                    (ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', usd, usd, 'Hrs'),), False)
        result = self.book.refresh_charges(self.settings, now, lambda _start, _end: (reading,))
        self.assertIsInstance(result, ledger.ChargeRead)
        assert isinstance(result, ledger.ChargeRead)
        self.assertEqual(result.actual_usd, Decimal(amount))

    def publish(self, now: int) -> aws_page.PageData:
        path = aws_page.publish(self.state, now, self.folder, settings_path=self.settings)
        return self.published_data(path.read_text())

    @staticmethod
    def published_data(source: str) -> aws_page.PageData:
        payload = source.split('<script id="aws-prices-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        return cast(aws_page.PageData, json.loads(payload))

    def assert_spend_tiles(self, data: aws_page.PageData, today: str, yesterday: str, month: str) -> None:
        self.assertEqual(data['spend']['unavailable'], '')
        self.assertEqual([tile['usd'] for tile in data['spend']['tiles']], [today, yesterday, month])

    def assert_month_admission(self, now: int, amount: Decimal) -> None:
        with self.decision_book(self.book) as live:
            request = ledger.GateReservation('fixture-month-check', now, now + 3600, Decimal(0), Decimal(0),
                                             instance_type='m7a.8xlarge', region='us-east-1')
            self.replace('monthly_limit_usd', str(amount))
            self.assertEqual(live.reserve_gate(self.settings, request), ledger.GateCapReached('monthly_limit'))
            self.replace('monthly_limit_usd', str(amount.next_plus()))
            self.assertIsInstance(live.reserve_gate(self.settings, request), ledger.GateReserved)

    def assert_month_guard(self, now: int, amount: Decimal) -> None:
        with self.decision_book(self.book) as live:
            self.replace('monthly_limit_usd', str(amount))
            refused = live.guard(self.settings, 'admission', now, lambda _event: True, clock=lambda: now)
            self.assertIsInstance(refused, ledger.SpendingRefused)
            assert isinstance(refused, ledger.SpendingRefused)
            self.assertEqual(refused.reason, 'monthly_limit')
            self.replace('monthly_limit_usd', str(amount.next_plus()))
            allowed = live.guard(self.settings, 'admission', now, lambda _event: True, clock=lambda: now)
            self.assertIsInstance(allowed, ledger.SpendingAllowed)

    def test_row_57_formats_the_month_after_summing_the_bills(self) -> None:
        checked = self.midnight + 7200
        for offset in (1, 2):
            start = self.midnight - 86400 * offset
            day = datetime.fromtimestamp(start, UTC).date().isoformat()
            usd = Decimal('1.004')
            reading = ledger.ChargeRead(day, checked, start, start + 86400, usd, Decimal(0), Decimal(0),
                                        (ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', usd),), False)
            save_published_bill(self.book, reading, ((reading.period_end + 1, usd), (checked, usd)))
        actual = spend.read_spend(self.settings, self.state, checked, '2026-01-15')
        assert isinstance(actual, spend.Spend)
        self.assertEqual([(row.day, row.usd) for row in actual.days if row.source == 'AWS bill'],
                         [('2026-01-14', Decimal('1.004')), ('2026-01-13', Decimal('1.004'))])
        published = self.publish(checked)
        self.assert_spend_tiles(published, '$0.00', '$1.00', '$2.01')
        self.assertEqual(published['settled_notes'], ['Settled AWS amounts: 2 of 2 did not change after they settled (checked Jan 15).'])
        self.assert_month_guard(checked, Decimal('2.008'))
        self.assert_month_admission(checked, Decimal('2.008'))

    def test_row_64_page_and_guard_use_their_evaluation_times(self) -> None:
        self.refresh(self.midnight, '2')
        self.book.observe(ledger.InstanceObservation(self.midnight, 'running', 'fixture builder'))
        earlier = self.publish(self.midnight + 3600)
        self.assert_spend_tiles(earlier, '$1.00', '$2.00', '$3.00')
        self.assert_month_guard(self.midnight + 3600, Decimal(3))
        self.assert_month_admission(self.midnight + 3600, Decimal(3))
        now = self.midnight + 7200
        decision = self.book.guard(self.settings, 'admission', now, lambda _event: True, clock=lambda: 0)
        assert isinstance(decision, ledger.SpendingAllowed)
        costs = decision.evidence.costs
        assert isinstance(costs, ledger.CostEstimate)
        self.assertEqual((costs.at, costs.daily.usd, costs.monthly.usd), (now, Decimal(2), Decimal(4)))
        self.assert_spend_tiles(self.publish(now), '$2.00', '$2.00', '$4.00')
        self.assert_month_guard(now, Decimal(4))
        self.assert_month_admission(now, Decimal(4))

    def test_row_65_page_and_guard_can_use_different_saved_bills(self) -> None:
        self.refresh(self.midnight, '2')
        self.assert_spend_tiles(self.publish(self.midnight), '$0.00', '$2.00', '$2.00')
        self.assert_month_guard(self.midnight, Decimal(2))
        self.assert_month_admission(self.midnight, Decimal(2))
        path = self.folder / aws_page.PAGE_NAME
        earlier = path.read_bytes()
        now = self.midnight + 60
        self.refresh(now, '5')
        decision = self.book.guard(self.settings, 'admission', now, lambda _event: True, clock=lambda: 0)
        assert isinstance(decision, ledger.SpendingAllowed)
        costs = decision.evidence.costs
        assert isinstance(costs, ledger.CostEstimate)
        self.assertEqual((costs.daily.usd, costs.monthly.usd), (Decimal(0), Decimal(5)))
        self.assertEqual(path.read_bytes(), earlier)
        self.assert_month_admission(now, Decimal(5))
        self.assert_spend_tiles(self.publish(now), '$0.00', '$5.00', '$5.00')
        self.assert_month_guard(now, Decimal(5))

    def test_row_n03_page_keeps_the_prior_bill_until_reconciliation(self) -> None:
        self.refresh(self.midnight, '2')
        self.assert_spend_tiles(self.publish(self.midnight), '$0.00', '$2.00', '$2.00')
        path = self.folder / aws_page.PAGE_NAME
        earlier = path.read_bytes()
        now = self.midnight + 60
        self.assert_month_guard(self.midnight, Decimal(2))
        self.assert_month_admission(self.midnight, Decimal(2))
        interrupted: list[Decimal] = []
        reconcile = self.book._reconcile_charge  # pyright: ignore[reportPrivateUsage]

        def interrupt_refresh(state: ledger.LedgerEvidence, bill: ledger.ChargeRead) -> None:
            if bill.actual_usd == 2:
                reconcile(state, bill)
                return
            self.assertEqual(bill.actual_usd, Decimal(5))
            saved, = self.book.read_snapshot().reconciliations
            self.assertEqual(saved.usd, Decimal(2))
            self.assert_spend_tiles(self.publish(now), '$0.00', '$2.00', '$2.00')
            self.assertEqual(path.read_bytes(), earlier)
            interrupted.append(bill.actual_usd)
            raise OSError('fixture interrupted refresh')

        with mock.patch.object(self.book, '_reconcile_charge', side_effect=interrupt_refresh):
            with self.assertRaisesRegex(OSError, 'fixture interrupted refresh'):
                self.refresh(now, '5')
        self.assertEqual(interrupted, [Decimal(5)])
        self.assert_spend_tiles(self.publish(now), '$0.00', '$2.00', '$2.00')
        self.assertEqual(path.read_bytes(), earlier)

        self.refresh(now, '5')
        saved, = self.book.read_snapshot().reconciliations
        self.assertEqual(saved.usd, Decimal(5))
        self.assert_spend_tiles(self.publish(now), '$0.00', '$5.00', '$5.00')
        self.assert_month_guard(now, Decimal(5))
        self.assert_month_admission(now, Decimal(5))

    def test_interrupted_sub_cent_refresh_keeps_the_earlier_page(self) -> None:
        self.refresh(self.midnight, '5')
        self.assert_spend_tiles(self.publish(self.midnight), '$0.00', '$5.00', '$5.00')
        path = self.folder / aws_page.PAGE_NAME
        earlier = path.read_bytes()
        now = self.midnight + 60
        reconcile = self.book._reconcile_charge  # pyright: ignore[reportPrivateUsage]

        def interrupt_refresh(state: ledger.LedgerEvidence, bill: ledger.ChargeRead) -> None:
            if bill.actual_usd == Decimal('.006'):
                raise OSError('fixture interrupted sub-cent refresh')
            reconcile(state, bill)

        with mock.patch.object(self.book, '_reconcile_charge', side_effect=interrupt_refresh):
            with self.assertRaisesRegex(OSError, 'fixture interrupted sub-cent refresh'):
                self.refresh(now, '.006')
        saved, = self.book.read_snapshot().reconciliations
        self.assertEqual(saved.usd, Decimal(5))
        bill_path = self.state / 'charges' / '2026-01-14.json'
        self.assertEqual(ledger.read_charge(bill_path).actual_usd, Decimal('.006'))

        self.assert_spend_tiles(self.publish(now), '$0.00', '$5.00', '$5.00')
        self.assertEqual(path.read_bytes(), earlier)

        self.refresh(now, '.006')
        self.assert_spend_tiles(self.publish(now), '$0.00', '$0.00', '$0.00')

    def test_changes_to_deployment_or_placement_keep_the_earlier_page(self) -> None:
        _ = self.publish(self.midnight)
        path = self.folder / aws_page.PAGE_NAME
        earlier = path.read_bytes()
        now = self.midnight + 60
        renderer = aws_page.render_report_page
        sources = ((ledger.GATE_DEPLOYMENT_FILE, '{"regions": []}'),
                   (ledger.SPOT_PLACEMENT_FILE, '{"version": 1, "readings": []}'))
        for name, initial in sources:
            with self.subTest(name=name):
                source = self.state / name
                _ = source.write_text(initial)

                def change_source(directory: Path, report: spend.UtcSpendReport, started: float) -> str:
                    _ = source.write_text(source.read_text() + '\n')
                    with (mock.patch.object(run_cost, 'read_deployed_gate_variants', side_effect=AssertionError),
                          mock.patch.object(ledger, 'read_spot_placement_scores', side_effect=AssertionError)):
                        return renderer(directory, report, started)

                with mock.patch.object(aws_page, 'render_report_page', side_effect=change_source) as render:
                    with mock.patch.object(aws_page, 'write_text_atomic', wraps=write_text_atomic) as publish:
                        _ = self.publish(now)
                    publish.assert_not_called()
                self.assertEqual(render.call_count, 3)
                self.assertEqual(path.read_bytes(), earlier)

    def test_render_uses_captured_configuration_recovery(self) -> None:
        def git(*args: str) -> None:
            _ = subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture',
                                '-c', 'user.email=fixture@example.invalid', *args],
                               capture_output=True, check=True, timeout=TEST_HANG_GUARD_SECS)

        git('init', '-q')
        self.replace('run_cost_days', '7')
        git('add', self.settings.name)
        git('commit', '-qm', 'Save fixture settings')
        settings = self.settings.read_text().replace('run_cost_days=7\n', '')
        _ = self.settings.write_text(settings)
        report = spend.read_utc_spend(self.state, self.midnight, settings_path=self.settings,
                                      purpose=ledger.SpendCapturePurpose.PAGE_PUBLICATION)
        assert isinstance(report, spend.UtcSpendReport)

        with (mock.patch.object(conf, '_git', side_effect=AssertionError('configuration Git read during render')),
              mock.patch.object(conf, 'read_or_repository', side_effect=AssertionError('settings read during render')),
              mock.patch.object(Path, 'read_text', side_effect=AssertionError('file read during render'))):
            page = aws_page.render_report_page(self.state, report, 0)
        self.assertIn('last 7 days', page)
        with ledger.verify_spend_revision(self.state, report.evidence) as verified:
            self.assertIsInstance(verified, ledger.SpendRevisionVerified)

        git('commit', '--allow-empty', '-qm', 'Change fixture configuration revision')
        with ledger.verify_spend_revision(self.state, report.evidence) as verified:
            self.assertEqual(verified, ledger.SpendEvidenceUnavailable('spend sources changed before publication'))

    def test_row_n04_page_retries_when_a_launch_changes_the_revision(self) -> None:
        self.refresh(self.midnight, '2')
        now = self.midnight + 3600
        proposal = ledger.GateReservation('fixture-zero', now, now + 3600, Decimal(0), Decimal(0),
                                          instance_type='m7a.8xlarge', region='us-east-1')
        self.assertIsInstance(self.book.reserve_gate(self.settings, proposal), ledger.GateReserved)
        earlier = self.publish(now)
        self.assert_spend_tiles(earlier, '$0.00', '$2.00', '$2.00')
        self.assertEqual(earlier['launch_state'], {'kind': 'no-evidence'})
        path = self.folder / aws_page.PAGE_NAME
        prior_bytes = path.read_bytes()
        quota_help = aws_page.quota_help
        rendered: list[bytes] = []

        def append_after_capture() -> str:
            rendered.append(path.read_bytes())
            if len(rendered) == 1:
                cloud_gate.append_launch(self.state, {'attempt': proposal.attempt, 'time': now,
                    'result': 'launched', 'instance_type': proposal.instance_type, 'region': proposal.region,
                    'mode': 'merge-gate'}, gate_job=GATE_JOB_NOT_RECORDED)
            return quota_help()

        with mock.patch.object(aws_page, 'quota_help', side_effect=append_after_capture):
            following = self.publish(now + 1)
        self.assertEqual(rendered, [prior_bytes, prior_bytes])
        self.assert_spend_tiles(following, '$0.00', '$2.00', '$2.00')
        self.assertEqual(following['launch_state'], {'kind': 'working', 'last_good_at': now, 'latest_attempt_at': now})
        self.assertTrue(following['spend']['tiles'][0]['note'].endswith('1 gate runs.'))

        priced = replace(proposal, attempt='fixture-priced', region='us-east-2')
        self.assertIsInstance(self.book.reserve_gate(self.settings, priced), ledger.GateReserved)
        self.assertEqual(self.book.accrued_cost(self.settings, now, window='day').limit_count, Decimal(0))
        self.assert_month_guard(now, Decimal(2))
        self.assert_month_admission(now, Decimal(2))
        cloud_gate.append_launch(self.state, {'attempt': priced.attempt, 'time': now, 'result': 'launched',
            'instance_type': 'm7a.4xlarge', 'region': 'us-east-2', 'mode': 'merge-gate'},
            gate_job=GATE_JOB_NOT_RECORDED)
        self.assertEqual(tuple(self.book.accrued_cost(self.settings, now, window=window).limit_count
                               for window in ('day', 'month')), (Decimal(1), Decimal(3)))
        self.assert_month_guard(now, Decimal(3))
        self.assert_month_admission(now, Decimal(3))
        self.costs({'daily_limit_usd': '1'})
        self.assertEqual(self.book.reserve_gate(self.settings, replace(proposal, attempt='fixture-cap-probe')),
                         ledger.GateCapReached('daily_limit'))

    def test_row_n14_earlier_publisher_keeps_the_newer_page(self) -> None:
        for seconds in (0, 60):
            with self.subTest(newer_evaluation=seconds != 0):
                now = self.midnight + seconds * 10 + 120
                self.refresh(now - 60, '2')
                self.assert_spend_tiles(self.publish(now - 60), '$0.00', '$2.00', '$2.00')
                path = self.folder / aws_page.PAGE_NAME
                quota_help = aws_page.quota_help
                newer_pages: list[bytes] = []
                rendering_earlier = True

                def publish_newer_first() -> str:
                    nonlocal rendering_earlier
                    if rendering_earlier:
                        rendering_earlier = False
                        self.refresh(now, '5')
                        newer = self.publish(now + seconds)
                        self.assert_spend_tiles(newer, '$0.00', '$5.00', '$5.00')
                        self.assertEqual(newer['read_at'], now + seconds)
                        newer_pages.append(path.read_bytes())
                    return quota_help()

                with mock.patch.object(aws_page, 'quota_help', side_effect=publish_newer_first):
                    visible = self.publish(now)
                self.assertEqual(newer_pages, [path.read_bytes()])
                self.assertEqual(visible['read_at'], now + seconds)
                self.assert_spend_tiles(visible, '$0.00', '$5.00', '$5.00')
                self.assert_month_guard(now + seconds, Decimal(5))
                self.assert_month_admission(now + seconds, Decimal(5))

    def test_shows_a_month_amount_without_an_accepted_bill(self) -> None:
        now = self.midnight + 10800
        self.book.observe(ledger.InstanceObservation(self.midnight, 'running', 'fixture builder'))
        self.book.observe(ledger.InstanceObservation(now, 'stopped', 'fixture builder'))
        published = self.publish(now)
        tiles = published['spend']['tiles']
        self.assertEqual((tiles[0]['usd'], tiles[2]['usd']), ('$3.00', '$3.00'))
        self.assertIn('estimate', tiles[0]['note'])
        self.assertEqual(published['spend']['forecast'], '')

    def test_final_verification_holds_the_ledger_lock_through_replacement(self) -> None:
        self.refresh(self.midnight, '2')
        _ = self.publish(self.midnight)
        path = self.folder / aws_page.PAGE_NAME
        earlier = path.read_bytes()
        at = self.midnight + 60
        writes: list[int] = []

        def try_second_publication(destination: Path, source: str, *, new_file_mode: int | None = None) -> None:
            writes.append(new_file_mode or 0)
            if len(writes) == 1:
                with mock.patch.object(ledger, 'capture_spend_evidence', wraps=ledger.capture_spend_evidence) as capture:
                    _ = self.publish(at + 1)
                self.assertEqual(capture.call_count, 3)
                self.assertEqual(path.read_bytes(), earlier)
            write_text_atomic(destination, source, new_file_mode=new_file_mode)

        with mock.patch.object(aws_page, 'write_text_atomic', side_effect=try_second_publication):
            published = self.publish(at)
        self.assertEqual(writes, [0o644])
        self.assertEqual(published['read_at'], at)

    def test_repeated_source_changes_leave_the_page_bytes_intact(self) -> None:
        _ = self.publish(self.midnight)
        path = self.folder / aws_page.PAGE_NAME
        earlier = path.read_bytes()
        quota_help = aws_page.quota_help
        renders: list[int] = []
        at = self.midnight + 60

        def change_source() -> str:
            self.book.observe(ledger.InstanceObservation(at - len(renders), 'stopped', 'fixture'))
            renders.append(at)
            return quota_help()

        with mock.patch.object(aws_page, 'quota_help', side_effect=change_source):
            _ = self.publish(at)
        self.assertEqual(len(renders), 3)
        self.assertEqual(path.read_bytes(), earlier)

    def publish_configured(self, directory: Path) -> int:
        with (mock.patch.object(target_config, 'ssh_target_state_directory', return_value=directory),
              mock.patch.object(aws_page, 'output_folder', return_value=self.folder),
              mock.patch.object(time, 'time', return_value=self.midnight)):
            return aws_page.publish_configured()

    def test_configured_publisher_accepts_an_empty_state_directory(self) -> None:
        empty = self.root / 'empty-state'
        empty.mkdir()
        self.assertEqual(self.publish_configured(empty), 0)
        data = self.published_data((self.folder / aws_page.PAGE_NAME).read_text())
        self.assert_spend_tiles(data, '—', '—', '—')
        self.assertEqual(data['hours'], {'kind': 'unavailable', 'reason': 'Our records are unreadable.'})
        self.assertEqual(list(empty.iterdir()), [])

    def test_configured_publisher_defers_for_a_ledger_without_its_lock(self) -> None:
        unlocked = self.root / 'unlocked-state'
        unlocked.mkdir()
        ledger_path = ledger.CostLedger(unlocked).path
        _ = ledger_path.write_bytes(self.book.path.read_bytes())
        with redirect_stderr(StringIO()) as errors:
            self.assertEqual(self.publish_configured(unlocked), 1)
        self.assertIn('account evidence exists without its lock', errors.getvalue())
        self.assertFalse(self.folder.exists())
        self.assertEqual(list(unlocked.iterdir()), [ledger_path])

    def test_configured_publisher_accepts_a_price_snapshot_without_a_ledger(self) -> None:
        prices = self.root / 'price-state'
        prices.mkdir()
        _ = (prices / 'spot-prices.lock').write_text(str(self.midnight))
        ledger.write_spot_prices(prices, (
            ledger.RegionalSpotPrice('us-east-1', 'us-east-1a', 'm7a.8xlarge', Decimal('.25'), self.midnight),
        ))
        self.assertEqual(self.publish_configured(prices), 0)
        data = self.published_data((self.folder / aws_page.PAGE_NAME).read_text())
        self.assert_spend_tiles(data, '—', '—', '—')
        standard = data['prices'][0]
        self.assertEqual(standard['values'][0][standard['headers'].index('m7a.8xlarge')], .25)
        self.assertEqual(sorted(path.name for path in prices.iterdir()), [ledger.SPOT_PRICE_FILE, 'spot-prices.lock'])

    def test_page_folder_lock_serializes_publication_without_a_ledger(self) -> None:
        empty = self.root / 'empty-state'
        empty.mkdir()
        self.assertEqual(self.publish_configured(empty), 0)
        path = self.folder / aws_page.PAGE_NAME
        earlier = path.read_bytes()
        at = self.midnight + 1

        def try_second_publication(destination: Path, source: str, *, new_file_mode: int | None = None) -> None:
            with mock.patch.object(aws_page, 'write_text_atomic') as second_write:
                _ = aws_page.publish(empty, at + 1, self.folder, settings_path=self.settings)
            second_write.assert_not_called()
            self.assertEqual(path.read_bytes(), earlier)
            write_text_atomic(destination, source, new_file_mode=new_file_mode)

        with mock.patch.object(aws_page, 'write_text_atomic', side_effect=try_second_publication):
            _ = aws_page.publish(empty, at, self.folder, settings_path=self.settings)
        self.assertEqual(self.published_data(path.read_text())['read_at'], at)
        self.assertEqual(list(empty.iterdir()), [])

    def test_page_recaptures_when_the_first_ledger_appears_during_rendering(self) -> None:
        book = ledger.CostLedger(self.root / 'new-ledger')
        quota_help = aws_page.quota_help

        def write_first_ledger() -> str:
            if not book.path.exists():
                book.record_inventory(ledger.ResourceInventory(
                    self.midnight - 3600, (), 'fixture inventory', builder_removed=True))
            return quota_help()

        with mock.patch.object(aws_page, 'quota_help', side_effect=write_first_ledger) as rendered:
            self.assertEqual(self.publish_configured(book.directory), 0)
        self.assertEqual(rendered.call_count, 2)
        data = self.published_data((self.folder / aws_page.PAGE_NAME).read_text())
        self.assertEqual(data['spend']['tiles'][1]['usd'], '$0.00')


@unittest.skipUnless(shutil.which('firefox'), 'Firefox is required for browser checks')
class AwsHoursColorTests(unittest.TestCase):
    def test_hours_color_marks_rounded_differences_and_the_filtered_total(self) -> None:
        limit = Decimal('.20')
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        ledger_test_support.install_aws_fixture_settings(root)
        settings = root / 'aws_target.conf'
        ledger_test_support.override_fixture_settings(settings, f"""
billed_hours_gap_max_ratio={limit}
hours_table_open_row_limit=5
""")
        _ = (root / 'console.conf').write_text("""\
# range: 0..
status.round_trip_limit_seconds=1
# range: 0..
status.render_limit_seconds=1
# range: 0..
status.reading_delay_seconds=1
# range: 0..
status.aws.age_limit_seconds=3900
""")
        state = root / 'state'
        with ledger.CostLedger(state).transaction():
            pass
        readings = (
            ('us-east-1', '2.0', '1.0', 'difference', '+1.0'),
            ('us-east-2', '1.0', '2.0', 'difference', '-1.0'),
            ('eu-west-1', '1.0', '1.0', 'no-difference', '0.0'),
            ('us-west-1', '1.049', '1.0', 'no-difference', '0.0'),
            ('us-west-2', '1.2', '1.0', 'difference', '+0.2'),
        )
        checks = {('c7a.4xlarge', region, '2026-01-14'): run_cost.hours_check(Decimal(billed), Decimal(box), limit)
                  for region, billed, box, _kind, _text in readings}
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        with (mock.patch.dict(os.environ, CONF_DIR=str(root), AWS_TARGET_CONFIG=str(settings), USER_ZONE='UTC'),
              mock.patch.object(run_cost, 'day_hours_from', return_value=checks)):
            page = aws_page.publish(state, now, root / 'page', settings_path=settings).read_text()
        payload = page.split('<script id="aws-prices-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        hours = cast(aws_page.PageData, json.loads(payload))['hours']
        assert hours['kind'] == 'comparison'
        self.assertEqual({row['region']: row['difference_kind'] for row in hours['rows']},
                         {region: kind for region, _billed, _box, kind, _text in readings})

        expected = json.dumps([(region, kind, value) for region, _billed, _box, kind, value in readings])
        probe = '<script>\n(async () => {\nconst expected = ' + expected + r''';
const failures = [];
const realFetch = window.fetch;
const section = document.getElementById("hours");
const check = (condition, message) => { if (!condition) throw new Error(message); };
const total = () => section.querySelector("tfoot tr").cells[3];
const expectedColor = value => {
  const reference = document.body.appendChild(document.createElement("span"));
  reference.style.color = value;
  const color = getComputedStyle(reference).color;
  reference.remove();
  return color;
};
try {
  for (const theme of ["dark", "light"]) {
    document.documentElement.dataset.theme = theme;
    const yellow = expectedColor("var(--warning)");
    const blue = expectedColor("color-mix(in srgb, var(--accent) 80%, var(--ink))");
    check(yellow !== blue, `${theme}: both colors are distinct`);
    for (const [region, kind, text] of expected) {
      const row = [...section.querySelectorAll("tbody tr")].find(row => row.cells[1].textContent === region);
      const cell = row.cells[4];
      const color = kind === "difference" ? yellow : blue;
      check(cell.classList.contains(`hours-${kind}`) && cell.textContent === text, `${theme}: ${region} class and value`);
      check(getComputedStyle(cell).color === color, `${theme}: ${region} computed color`);
    }
    const legend = section.querySelector(".hours-legend");
    check(legend.textContent === "Yellow: AWS and our records differ. Blue: they agree.", `${theme}: legend text`);
    check(getComputedStyle(legend.children[0]).color === yellow && getComputedStyle(legend.children[1]).color === blue,
          `${theme}: legend colors`);
    check(total().classList.contains("hours-difference") && total().textContent === "+0.2"
          && getComputedStyle(total()).color === yellow, `${theme}: total differs`);
    const extra = document.querySelector("#regions [data-region='us-west-2']");
    extra.click();
    check(total().classList.contains("hours-no-difference") && total().textContent === "0.0"
          && getComputedStyle(total()).color === blue, `${theme}: the filtered total agrees`);
    extra.click();
  }
  const tableValues = () => [...section.querySelectorAll("table tr")].map(row =>
    [...row.cells].map(cell => cell.textContent));
  const before = JSON.stringify(tableValues());
  const olderData = document.getElementById("aws-prices-data").cloneNode(true);
  const olderPayload = JSON.parse(olderData.textContent);
  delete olderPayload.hours.difference_kinds;
  for (const row of olderPayload.hours.rows) delete row.difference_kind;
  olderData.textContent = JSON.stringify(olderPayload);
  const gateData = document.getElementById("aws-gate-time-data");
  window.fetch = async () => ({ok: true, text: async () => olderData.outerHTML + gateData.outerHTML});
  try {
    await loadAws();
  } finally {
    window.fetch = realFetch;
  }
  check(JSON.stringify(tableValues()) === before, "The older refresh keeps every table value");
  check(!section.querySelector(".hours-difference, .hours-no-difference, .hours-legend"),
        "The older refresh uses the default text color");
} catch (error) {
  failures.push(String(error));
}
await realFetch("/probe", {method: "POST", body: JSON.stringify(failures)});
})();
</script>'''
        self.assertEqual(firefox_probe_results(page.replace('</body>', probe + '</body>', 1).encode()), [[]])


@unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for the AWS table checks")
class AwsCombinedTableTests(PageCostFixture):
    def fixture_page(self, *, blocked: bool = False,
                     quota_history: tuple[dict[str, object], ...] = ()) -> Path:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        regions = ('us-east-1', 'us-east-2', 'eu-west-1')
        standard = ('c7a.4xlarge', 'm7a.8xlarge')
        gpu = 'g6.2xlarge'
        config = self.root / 'cloud_gate.conf'
        self.configure_gate('max_gate_minutes=60\n')
        for key, value in (
            ('spot_candidate_regions', ' '.join(reversed(regions))), ('instance_types', ' '.join(standard)),
            ('price_compare_instance_types', standard[1]), ('gpu_instance_types', gpu),
            ('hana_gpu_instance_types', gpu),
        ):
            ledger_test_support.text_setting(config, key, value)
        prices = [
            {'region': region, 'instance_type': size, 'zone': region + 'a', 'usd_per_hour': usd, 'read_at': now - 240}
            for region, row in zip(regions, (('0.1', '0.6', '0.7'), ('0.2', '0.5', '0.8'), ('0.3', '', '0.9')), strict=True)
            for size, usd in zip((*standard, gpu), row, strict=True) if usd
        ]
        _ = (self.state / 'gate-deployment.json').write_text(json.dumps({'regions': [
            {'region': region, 'subnets': [{'id': 'subnet-' + region, 'zone': region + 'a'}]} for region in regions
        ]}))
        readings = [
            {'region': region, 'read_at': now - age, 'standard_vcpus': '137', 'gpu_vcpus': '8' if quota_history and region != 'us-east-2' else '32'}
            for region, age in zip(regions, (240, 7201, 240), strict=True)
        ]
        failures = [{'region': regions[0], 'at': now - 120, 'read': 'price', 'error': 'FixtureError'}]
        _ = (self.state / 'spot-prices.json').write_text(json.dumps({
            'prices': prices, 'regions': readings, 'failures': failures,
            **({'quota_requests': quota_history} if quota_history else {}),
        }))
        events: list[dict[str, object]] = []
        for mode, times in (('merge-gate', (3600, 360)), ('targeted-test', (600, 1800))):
            for index, (size, seconds) in enumerate(zip(standard, times, strict=True)):
                run = {'attempt': f'{mode}-{index}', 'instance_type': size, 'mode': mode, 'region': regions[0]}
                events.extend(({**run, 'time': now - 60 - seconds, 'result': 'launched'},
                               {**run, 'time': now - 60, 'result': 'finished', 'gate_s': seconds - 60}))
        if blocked:
            events.append({'attempt': 'blocked-fixture', 'instance_type': standard[0], 'mode': 'gate',
                           'region': regions[0], 'time': now - 30, 'result': 'refused', 'error_code': 'Blocked'})
        _ = (self.state / 'cloud-gate-launches.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in events))
        placement_regions = (regions[0], 'us-west-1')
        request = ledger.SpotPlacementRequest(ledger.SpotPlacementScope.REGION,
            ledger.SpotPlacementTypeSet.ALL_GATE_TYPES, 8, standard, placement_regions)
        ledger.write_spot_placement_scores(self.state, (ledger.SpotPlacementSample(request, now,
            (ledger.SpotPlacementScore(ledger.SpotPlacementRegion(regions[0]), 9),)),), now)
        return aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings)

    def run_browser_checks(self, query: str, checks: str, *, blocked: bool = False,
                           quota_history: tuple[dict[str, object], ...] = ()) -> None:
        page = self.fixture_page(blocked=blocked, quota_history=quota_history)
        probe = """<script>
(async () => {
  const result = document.createElement("p");
  result.id = "table-checks";
  document.body.appendChild(result);
  function check(condition, description) { if (!condition) throw new Error(description); }
  try {
""" + checks + """
    result.textContent = "passed";
  } catch (error) {
    result.textContent = String(error);
  }
})();
</script>"""
        _ = page.write_text(page.read_text().replace('</body>', probe + '</body>', 1))
        result = subprocess.run([
            'google-chrome', '--headless', '--no-sandbox', '--disable-gpu', '--disable-background-networking',
            '--no-first-run', '--host-resolver-rules=MAP * ~NOTFOUND', f'--user-data-dir={self.root / "chrome"}',
            '--window-size=760,1000',
            '--dump-dom', page.as_uri() + query,
        ], capture_output=True, text=True, timeout=TEST_HANG_GUARD_SECS, check=True)
        marker = re.search(r'<p id="table-checks">(.*?)</p>', result.stdout)
        assert marker is not None, 'The browser must finish the table checks.'
        self.assertEqual(unescape(marker[1]), 'passed')

    def test_disabled_cloud_gates_replace_the_launch_status(self) -> None:
        self.configure_gate('enabled=off')
        self.run_browser_checks('', """
    const block = document.getElementById("launch-state");
    check(block.dataset.kind === "off", "Disabled cloud gate state");
    check(block.innerText === "Cloud gates are off (cloud_gate.conf enabled=off). Gates run on natedev.",
          "The disabled state explains where gates run");
""", blocked=True)

    def test_invalid_cloud_gate_switch_keeps_the_other_page_blocks(self) -> None:
        self.configure_gate('enabled=invalid')
        at = ledger_test_support.instant('2026-01-15T11:59:00Z')
        with ledger.CostLedger(self.state).transaction() as evidence:
            evidence.observations.append(ledger.InstanceObservation(at, 'stopped', 'fixture'))
            evidence.inventories.append(ledger.ResourceInventory(at, (), 'fixture', builder_removed=True))
        self.run_browser_checks('?show=quota', """
    const block = document.getElementById("launch-state");
    check(block.innerText === "cloud_gate.conf enabled is not valid. Gates run on natedev.",
          "The invalid switch explains where gates run");
    check(document.querySelectorAll("#spend-tiles .tile").length === 3, "Spend still renders");
    check(document.querySelector("#price-table tbody tr"), "Quotas still render");
    const hours = document.getElementById("hours");
    check(hours.querySelector("h2").textContent === "Did AWS bill more machine hours than we used?", "Hours still render");
    check(hours.querySelector("p").textContent === "No AWS bill is available yet for the last 5 days.",
          "The answer explains that no bill is available");
    check(!hours.querySelector("table, details"), "No table or disclosure without a bill");
""", blocked=True)

    def test_billed_hours_filters_sizes_by_activity_in_the_selected_regions(self) -> None:
        start = ledger_test_support.instant('2026-01-14T00:00:00Z')
        end = start + 86400
        with ledger.CostLedger(self.state).transaction() as evidence:
            evidence.hana_compute.append(ledger.HanaComputeExposure(
                start, start + 3600, 1, Decimal('.25'), ledger.HanaExposureClosed(start + 3600),
                'm7a.2xlarge', region='us-east-2'))
        charges = self.state / 'charges'
        charges.mkdir()
        charge = ledger.ChargeRead(
            '2026-01-14', end, start, end, Decimal(1), Decimal(0), Decimal(0),
            (ledger.UsageTypeCost('USE1-SpotUsage:c7a.4xlarge', Decimal('.75'), Decimal(1), 'Hrs'),
             ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal('.25'), Decimal('.01'), 'Hrs')))
        save_published_bill(ledger.CostLedger(self.state), charge)
        self.run_browser_checks('', """
    const sizes = () => [...document.querySelectorAll("#hours tbody tr")].map(row => row.cells[0].textContent);
    check(sizes().includes("c7a.4xlarge") && sizes().includes("m7a.8xlarge"), "Both active sizes show");
    const east = document.querySelector("#regions [data-region='us-east-1']");
    document.querySelector("#regions .all:last-child").click();
    document.querySelector("#regions [data-region='us-east-2']").click();
    check(sizes().join() === "m7a.8xlarge", "The hidden region's size row is absent");
    const cells = [...document.querySelector("#hours tbody tr").cells];
    check(cells.slice(1, 5).map(cell => cell.textContent).join("|") === "us-east-2|0.0|0.0|0.0",
          "The displayed values give a difference of zero");
    check(document.querySelector("#hours p").textContent
          === "No. AWS billed the same hours as our records (0.0). "
          + "1 row is more than 20% above its recorded hours plus the hours reserved for Hana.",
          "The answer follows the displayed totals and the limit uses the full values");
    east.click();
    check(sizes().includes("c7a.4xlarge"), "The row returns when its region is selected");
""")

    def test_refused_bill_notice_lists_only_estimates_shown_on_the_page(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        start = ledger_test_support.instant('2026-01-13T00:00:00Z')
        book = ledger.CostLedger(self.state)
        book.record_inventory(ledger.ResourceInventory(start, (), 'fixture', builder_removed=True))
        book.observe(ledger.InstanceObservation(start, 'terminated', 'fixture'))
        charges = self.state / 'charges'
        charges.mkdir()
        bill = ledger.ChargeRead('2026-01-13', now, start, start + 86400, Decimal(4), Decimal(0), Decimal(0))
        save_published_bill(book, bill)
        _ = (charges / 'alias.json').write_text(json.dumps(ledger.charge_record(bill)))
        self.run_browser_checks('', """
    check(document.querySelector("#bill-refusals").hidden, "The valid bill still supplies the day");
""")
        mismatched = ledger.ChargeRead('2026-01-14', now, start + 86400, start + 2 * 86400,
                                       Decimal(5), Decimal(0), Decimal(0))
        mismatched_path = charges / '2026-01-15.json'
        _ = mismatched_path.write_text(json.dumps(ledger.charge_record(mismatched)))
        self.run_browser_checks('', """
    const summary = document.querySelector("#bill-refusal-summary");
    check(summary.textContent === "1 AWS bill record refused; those days show our estimate.", "One record");
    check(document.querySelector("dl[role=tooltip] dt").textContent === "2026-01-14, 2026-01-15",
          "The refusal lists both UTC dates");
""")
        mismatched_path.unlink()
        _ = (charges / '2026-01-14.json').write_text('{broken')
        self.run_browser_checks('', """
    const summary = document.querySelector("#bill-refusal-summary");
    check(summary.textContent === "1 AWS bill record refused; that day shows our estimate.", "Singular notice");
    check(document.querySelector("dl[role=tooltip] dt").textContent === "2026-01-14", "Refused UTC day");
""")
        _ = (charges / '2026-01-15.json').write_text('{broken')
        self.run_browser_checks('', """
    const summary = document.querySelector("#bill-refusal-summary");
    const tip = document.querySelector("#bill-refusal-tip");
    check(summary.textContent === "2 AWS bill records refused; those days show our estimate.", "Plural notice");
    check(!summary.parentElement.hidden && getComputedStyle(tip).display === "none", "Notice with closed tooltip");
    summary.focus();
    check(getComputedStyle(tip).display === "grid", "Focus opens the tooltip");
    check([...tip.querySelectorAll("dt")].map(row => row.textContent).join() === "2026-01-14,2026-01-15",
          "Only shown estimates appear");
    check([...tip.querySelectorAll("dd")].every(row =>
          row.textContent === "invalid bill format" && getComputedStyle(row).textAlign === "right"),
          "The tooltip lists right-aligned reasons");
    summary.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
    check(getComputedStyle(tip).display === "none", "Escape closes the tooltip");
""")

    def test_launch_state_sits_above_spend_and_updates_after_a_good_launch(self) -> None:
        self.run_browser_checks('', """
    const block = document.getElementById("launch-state");
    const message = () => block.querySelector("[role='status']").textContent;
    check(block.nextElementSibling.id === "spend", "Launch state sits above spend");
    check(block.dataset.kind === "refused", "Refusal style");
    check(!block.innerText.includes("Cloud gates are off"), "The notice is absent when gates are on");
    check(message().includes("AWS refuses launches (Blocked). The account is blocked."),
          "Refusal includes the code and plain reason");
    const {first_at, last_good_at, latest_attempt_at} = data.launch_state;
    check(message().includes(`First refusal: ${launchTime.format(first_at * 1000)}.`)
          && message().includes(`1 refusal since the last good launch (${launchTime.format(last_good_at * 1000)}).`),
          "Refusal times and count");
    check(Number.isFinite(latest_attempt_at)
          && message().includes(`Latest attempt: ${launchTime.format(latest_attempt_at * 1000)}.`),
          "Latest attempt appears in the refused state");
    document.querySelector("#regions .all:last-child").click();
    check(block.dataset.kind === "refused", "Region filters leave launch state intact");
    const next = JSON.stringify({...data, launch_state: {kind: "working", last_good_at: first_at + 1,
      latest_attempt_at: first_at + 61}});
    const gateTime = document.getElementById("aws-gate-time-data").outerHTML;
    window.fetch = async () => ({ok: true, text: async () =>
      `<script type="application/json" id="aws-prices-data">${next}</scr` + "ipt>" + gateTime});
    await loadAws();
    check(block.dataset.kind === "working", "Refresh clears refusal style");
    check(message().includes(`AWS launches work. Last good launch: ${launchTime.format((first_at + 1) * 1000)}.`)
          && message().includes(`Latest attempt: ${launchTime.format((first_at + 61) * 1000)}.`),
          "Refresh shows the last good launch and latest attempt");
""", blocked=True)

    def test_default_table_marks_and_shared_filters(self) -> None:
        self.run_browser_checks('?prices=cost', """
    check(scope === null && scopeCosts().scope === "gate", "The default scope shows gate prices");
    const table = () => document.querySelector("#price-table table");
    const click = selector => document.querySelector(selector).click();
    const spot = () => table().tBodies[0].rows[0].cells[5];
    const gate = () => table().tBodies[0].rows[0].cells[6];
    const params = () => new URLSearchParams(location.search);
    const placement = document.querySelector("#placement-scores");
    const placementRows = () => [...placement.querySelectorAll("tbody tr")];
    check(placement.previousElementSibling.id === "prices-quotas", "Placement follows Prices and quotas");
    check(placementRows()[0].cells[3].textContent === "9 / 9 / 1", "Placement sample renders");
    check(placementRows().some(row => row.cells[0].textContent === "us-west-1")
          && table().tBodies[0].rows.length === 3, "A region with only scores does not get a price row");
    check(table().tHead.rows.length === 2, "Default grouped headers");
    check(table().tHead.rows[0].cells[5].textContent === "c7a.4xlarge (16 vCPUs)", "Type vCPU count");
    check(table().tHead.rows[1].cells[0].textContent === "Spot USD/h", "Spot unit");
    check(table().tHead.rows[1].cells[1].textContent === "Gate USD", "Gate unit");
    check(table().tHead.rows[1].cells[2].textContent === "Fit boxes", "Fit unit");
    const first = table().tBodies[0].rows[0];
    check(first.cells[1].textContent === "137" && first.cells[7].textContent === "8", "Quota and Fit numbers");
    check([...table().querySelectorAll(".quota-request")].map(cell => cell.textContent).join(",")
          === "not measured,not measured,not measured", "Legacy snapshots have no request history");
    check(document.querySelector("#quota-request-note").textContent === "The snapshot has no quota request history.",
          "One note for the legacy table");
    check(first.cells[1].className === "n edge region-quota" && first.cells[7].className === "n", "No quota marks");
    check(spot().textContent === "0.1000" && spot().classList.contains("cheapest"), "Lowest Spot price");
    check(first.querySelector(".spot-status").textContent === "read failed", "Visible failed read");
    check(table().tBodies[0].rows[1].querySelector(".spot-status").textContent === "old", "Visible old read");
    check(first.cells[8].classList.contains("dearest"), "Highest Spot price");
    check(table().tBodies[0].rows[1].cells[9].classList.contains("cheapest"), "Lowest Gate cost in another cell");
    check(table().tBodies[0].rows[2].cells[6].classList.contains("dearest"), "Highest Gate cost");
    check(table().tBodies[0].rows[2].cells[5].classList.contains("end"), "Column box");
    check(table().tBodies[0].rows[2].cells[8].title === "Not sold here", "Unavailable type icon");
    const missingGate = table().tBodies[0].rows[2].cells[9];
    check(missingGate.title === "no current gate price"
          && missingGate.getAttribute("aria-label") === missingGate.title, "Unavailable Gate price note");
    const merge = gate().textContent;
    click("#runs-chips [data-scope='test-affected']");
    check(gate().textContent !== merge, "Runs chips change the Gate values");
    check(document.querySelector("#filter [data-scope='test-affected']").getAttribute("aria-pressed") === "true",
          "Runs chips update Cost per run");
    click("#regions [data-region='us-east-1']");
    check(table().tBodies[0].rows.length === 2 && spot().classList.contains("cheapest"), "Marks use the filtered regions");
    check(!placementRows().some(row => row.cells[0].textContent === "us-east-1"), "The region filter applies to placement scores");
    check(gate().classList.contains("cheapest") && table().tBodies[0].rows[0].cells[9].classList.contains("dearest"),
          "Gate marks use the filtered regions");
    click("#show [data-measure='cost']");
    check(document.querySelector("#runs-label").hidden && document.querySelector("#runs-chips").hidden, "Hide Runs");
    check(params().get("show") === "spot,quota" && params().get("runs") === "test-affected", "Show and Runs URL");
    click("#show [data-measure='spot']");
    check(table().tHead.rows.length === 1 && table().tBodies[0].rows[0].cells.length === 7, "Fit alone");
    click("#show [data-measure='quota']");
    check(table().tBodies[0].rows[0].cells.length === 5 && params().get("show") === "", "Quota remains with all chips off");
    click("#show [data-measure='cost']");
    const gateOnly = table().tBodies[0].rows[0].cells;
    check(gateOnly[5].classList.contains("cheapest") && gateOnly[6].classList.contains("dearest"), "Gate marks with Gate alone");
    click("#show [data-measure='spot']");
    document.head.appendChild(element("style", "#price-table table { min-width: 1000px; }"));
    const scroller = () => document.querySelector("#price-table .scroll");
    check(innerWidth === 760 && scroller().scrollWidth > scroller().clientWidth, "Narrow scroll container");
    scroller().scrollLeft = 150;
    check(scroller().scrollLeft === 150, "Set scroll position before refresh");
    const settled = document.getElementById("settled-notes");
    check(settled.children.length === 0, "No settled notes on the first render");
    const notes = ["Settled AWS amounts: 8 of 9 did not change after they settled (checked Nov 19).",
                   "2026-11-12 changed from USD 7.73 to USD 7.81 (+0.08)."];
    const prices = JSON.parse(document.getElementById("aws-prices-data").textContent);
    let refreshed = JSON.stringify({...prices, render_seconds: 12, settled_notes: notes});
    const gateTime = document.getElementById("aws-gate-time-data").textContent;
    window.fetch = async () => ({ok: true, text: async () =>
      `<script type="application/json" id="aws-prices-data">${refreshed}</scr` + "ipt>"
      + `<script type="application/json" id="aws-gate-time-data">${gateTime}</scr` + "ipt>"});
    const status = await loadAws();
    check(status.renderSeconds === 12 && scroller().scrollLeft === 150, "Refresh keeps scroll position");
    check(settled.innerHTML === notes.map(note => '<p class="note">' + note + '</p>').join(""),
          "Refresh renders the settled notes in Spend");
    refreshed = JSON.stringify({...data, settled_notes: []});
    await loadAws();
    check(settled.children.length === 0, "Refresh removes obsolete settled notes");
    check(params().get("show") === "spot,cost" && params().get("types") === "standard"
          && params().get("runs") === "test-affected", "Refresh keeps the filter URL");
    const pressed = id => [...document.querySelectorAll(`#${id} [aria-pressed='true']`)].map(chip => chip.textContent).join(",");
    check(pressed("show") === "Spot price,Cost per gate" && pressed("types") === "Standard" && pressed("runs-chips") === "Test affected runs",
          "Refresh keeps the selected chips");
    for (const [values, indices, color] of [[[0.1, 0.1, 0.3], [0, 1], "low"], [[0.1, 0.3, 0.3], [1, 2], "high"]]) {
      const {colors} = scaleCells({values: values.map(value => [null, value])}, {rows: [0, 1, 2], columns: [1]});
      check(indices.every(index => colors.get(`${index},1`) === `var(--scale-${color})`), `Tied ${color} endpoint`);
    }
""")

    def test_old_merge_gate_url_selects_the_gate_scope(self) -> None:
        self.run_browser_checks('?prices=cost&runs=merge-gate', """
    check(scope === "gate" && scopeCosts().scope === "gate", "The old URL selects the gate scope");
""")

    def test_cost_switch_uses_the_configured_gate_name_without_standard_types(self) -> None:
        ledger_test_support.text_setting(self.root / 'build_steps.conf', 'names.gate', 'fixture gate')
        read_words = conf.ConfigRead.words

        def words(settings: conf.ConfigRead, key: str) -> tuple[str, ...]:
            if key in ('instance_types', 'price_compare_instance_types'):
                return ()
            return read_words(settings, key)

        with mock.patch.object(conf.ConfigRead, 'words', new=words):
            self.run_browser_checks('?prices=cost', """
    check(scopeCosts().tables.length === 0, "The gate scope has no tables");
    check(document.querySelector("#show [data-measure='cost']").textContent === "Cost per fixture gate",
          "The switch uses the configured gate name");
""")

    def test_quota_cells_and_gpu_summary_use_the_latest_request(self) -> None:
        gpu = {'state': 'requested', 'requested_vcpus': '32', 'created_at': '2026-01-11T10:00:00Z',
               'updated_at': '2026-01-15T10:00:00Z'}
        not_measured = {'state': 'not_measured',
                        'reason': 'The AWS plan key is locked. Run github-warmup in a terminal on natedev.'}
        self.run_browser_checks('?types=gpu&show=quota', """
    const summary = document.querySelector("#gpu-quota-summary");
    const cells = region => [...document.querySelectorAll("#price-table tbody tr")]
      .find(row => row.cells[0].textContent === region).cells;
    const request = region => [...cells(region)].slice(1, 5).map(cell => cell.textContent).join("|");
    const headers = [...document.querySelectorAll("#price-table thead th")].slice(2, 5);
    check(headers.map(cell => cell.textContent).join("|") === "wanted|request|request age", "Request headings");
    check(summary?.textContent === "G and VT quota (32 vCPUs): 1 granted · 1 open · 1 closed, not granted",
          "GPU progress across the three regions");
    check(request("us-east-2") === "32|32|granted|4 days", "The applied quota meets an open request");
    check(request("us-east-1") === "8|32|closed, not granted|4 days", "The applied quota is below the approved request");
    check(request("eu-west-1") === "8|32|open|2 hours", "Age uses the update time without a creation time");
    check([1, 2, 4].every(index => getComputedStyle(cells("us-east-2")[index]).textAlign === "right"),
          "Quota, wanted, and request age align right");
    const grantRegion = document.querySelector("#regions [data-region='us-east-2']");
    grantRegion.click();
    check(summary.textContent === "G and VT quota (32 vCPUs): 0 granted · 1 open · 1 closed, not granted",
          "The summary counts only the visible rows");
    grantRegion.click();
    document.querySelector("#types [data-types='standard']").click();
    check(request("us-east-2") === "137|—|not measured|—" && request("us-east-1") === "137|—|not measured|—",
          "Standard has its own request history");
    check(request("eu-west-1") === "137|—|—|—", "Empty history remains distinct");
    check(document.querySelector("#quota-request-note").textContent
          === "The plan identity is locked. Run github-warmup on natedev.", "One note for the locked identity");
    check(document.querySelectorAll("#price-table td").length === 21, "Three added columns for each region");
""", quota_history=(
            {'region': 'us-east-2', 'standard': not_measured, 'gpu': {**gpu, 'status': 'CASE_OPENED'}},
            {'region': 'us-east-1', 'standard': not_measured, 'gpu': {**gpu, 'status': 'APPROVED'}},
            {'region': 'eu-west-1', 'standard': {'state': 'no_request'},
             'gpu': {**gpu, 'status': 'PENDING', 'created_at': None}},
        ))

    def test_summary_omits_unknown_counts_before_the_first_request_history(self) -> None:
        self.run_browser_checks('?types=gpu&show=quota', """
    const summary = document.querySelector("#gpu-quota-summary");
    const prefix = "G and VT quota (32 vCPUs): ";
    check(summary.textContent === prefix + "3 granted · request history not measured yet",
          "The summary keeps known grants and omits unknown counts");
    const requests = [...document.querySelectorAll("#price-table .quota-request")];
    check(requests.length === 3 && requests.every(cell => cell.textContent === "not measured"),
          "All three request cells show that history is missing");
    const note = document.querySelector("#quota-request-note");
    check(!note.hidden && note.textContent === "The snapshot has no quota request history.", "One history note");
    document.querySelector("#regions .all:last-child").click();
    check(summary.textContent === prefix + "request history not measured yet",
          "The summary omits the grant count when no grant is visible");
""")

    def test_missing_history_note_requires_every_visible_row_to_lack_history(self) -> None:
        self.run_browser_checks('?types=gpu&show=quota', """
    const note = document.querySelector("#quota-request-note");
    const requests = [...document.querySelectorAll("#price-table .quota-request")];
    check(requests.some(cell => cell.textContent === "not measured"), "A missing history cell says not measured");
    check(requests.some(cell => cell.textContent === "—"), "An empty history has a record");
    check(note.hidden && note.textContent === "", "Mixed history has no whole-snapshot note");
    document.querySelector("#regions [data-region='us-east-1']").click();
    check(!note.hidden && note.textContent === "The snapshot has no quota request history.",
          "The note appears when all visible rows lack history");
""", quota_history=(
            {'region': 'us-east-1', 'standard': {'state': 'no_request'}, 'gpu': {'state': 'no_request'}},
        ))

    def test_url_selects_gpu_and_omits_gate(self) -> None:
        self.run_browser_checks('?show=spot,cost,quota&types=gpu&runs=both', """
    const table = document.querySelector("#price-table table");
    check(table.tHead.rows[0].cells[1].textContent === "G and VT quota vCPUs", "GPU quota");
    check(table.tHead.rows[0].cells[5].textContent === "g6.2xlarge (8 vCPUs)", "GPU type");
    check([...table.tHead.rows[1].cells].map(cell => cell.textContent).join(",") === "Spot USD/h,Fit boxes", "GPU omits Gate");
    check(table.tBodies[0].rows[0].cells[6].textContent === "4", "GPU Fit uses the GPU quota and vCPUs");
    check(document.querySelector("#runs-chips [data-scope='both']").getAttribute("aria-pressed") === "true", "Runs URL");
    document.querySelector("#show [data-measure='spot']").click();
    document.querySelector("#show [data-measure='quota']").click();
    const gateNote = document.querySelector("#gpu-gate-note");
    check(!gateNote.hidden && gateNote.textContent === "GPU types have no gate runs.", "GPU Gate-only note");
    document.querySelector("#types [data-types='standard']").click();
    check(new URLSearchParams(location.search).get("types") === "standard", "Types URL");
""")

    def test_hours_show_the_sorted_table_and_answer_from_the_totals(self) -> None:
        limit = Decimal('.15')
        self.costs({'billed_hours_gap_max_ratio': str(limit)})
        checks = {
            ('c7a.4xlarge', 'us-east-1', '2026-01-14'): run_cost.hours_check(Decimal('1.5'), Decimal('1'), limit),
            ('c7a.4xlarge', 'us-east-2', '2026-01-12'): run_cost.hours_check(
                Decimal('1.5'), Decimal('1'), limit, hana_reserved_hours=Decimal('.5')),
            ('m7a.8xlarge', 'us-west-1', '2026-01-14'): run_cost.hours_check(
                Decimal('0'), Decimal('2.1'), limit, hana_reserved_hours=Decimal('1')),
            ('m7a.8xlarge', 'us-west-2', '2026-01-14'): run_cost.hours_check(Decimal('.2'), Decimal('1'), limit),
            ('c7a.4xlarge', 'eu-west-1', '2026-01-14'): run_cost.hours_check(
                Decimal('5.7'), Decimal('2'), limit, hana_reserved_hours=Decimal('1')),
        }
        with mock.patch.object(run_cost, 'day_hours_from', return_value=checks):
            self.run_browser_checks('', """
    const section = document.getElementById("hours");
    const table = () => section.querySelector("table");
    const rows = () => [...table().tBodies[0].rows];
    const answer = () => section.querySelector("p").textContent;
    const cells = row => [...row.cells].map(cell => cell.textContent).join("|");
    const toggleRegion = region => document.querySelector(`#regions [data-region='${region}']`).click();
    const noHigher = "No row is more than 15% above its recorded hours plus the hours reserved for Hana.";
    const oneHigher = "1 row is more than 15% above its recorded hours plus the hours reserved for Hana.";
    const twoLower = "2 rows are more than 15% below their recorded hours.";
    check(answer() === "Yes. AWS billed 1.8 hours more than our records (8.9 against 7.1). "
          + "2 rows are more than 15% above their recorded hours plus the hours reserved for Hana. " + twoLower,
          "The answer uses the totals and reports both limits");
    check(!section.querySelector("details") && table().getBoundingClientRect().height > 0, "The table is visible");
    const header = cell => [...cell.children].map(part => part.textContent).join(" ");
    check([...table().tHead.rows[0].cells].map(header).join("|") === "Instance type|Region|AWS billed (hours)|"
          + "Our records (hours)|AWS minus our records (hours)|Reserved for Hana (hours)", "Hours headings and units");
    check(section.querySelector(".answer + .note").textContent.includes("Jan 12")
          && section.querySelector(".answer + .note").textContent.includes("14, 2026 (UTC)"),
          "The date range covers the bills used by the displayed rows");
    check(section.querySelector(".hours-legend").nextElementSibling.textContent
          === "A positive difference means AWS billed more than our records.", "The note explains the sign");
    check(table().tHead.rows[0].cells[5].title ===
          "The check adds these hours reserved for Hana in this region to our recorded hours when it checks for a higher bill.",
          "Hana reserved has a tooltip");
    check(rows().map(cells).join("\\n") === [
      "c7a.4xlarge|eu-west-1|5.7|2.0|+3.7|1.0",
      "c7a.4xlarge|us-east-1|1.5|1.0|+0.5|—",
      "c7a.4xlarge|us-east-2|1.5|1.0|+0.5|0.5",
      "m7a.8xlarge|us-west-2|0.2|1.0|-0.8|—",
      "m7a.8xlarge|us-west-1|0.0|2.1|-2.1|—"
    ].join("\\n"), "The rows sort by descending difference");
    check(cells(table().tFoot.rows[0]) === "Total|8.9|7.1|+1.8|—", "The total row leaves the Hana allowance blank");
    check([...table().querySelectorAll(".n")].every(cell => getComputedStyle(cell).textAlign === "right"),
          "Numeric headings, data and totals align right");
    toggleRegion("eu-west-1");
    check(answer() === "No. AWS billed 1.9 hours less than our records (3.2 against 5.1). " + oneHigher + " " + twoLower,
          "A row above the limit can occur with a lower total");
    toggleRegion("us-east-1");
    toggleRegion("us-west-1");
    toggleRegion("us-west-2");
    check(answer() === "Yes. AWS billed 0.5 hours more than our records (1.5 against 1.0). " + noHigher,
          "An in-limit higher bill explains the configured limit");
    check(section.querySelector(".answer + .note").textContent === "Bills compared Jan 12, 2026 (UTC).",
          "The date range follows the region filter");
    check(rows().length === 1 && rows()[0].cells[5].textContent === "0.5", "The in-limit row used Hana reserved");
    toggleRegion("us-east-2");
    toggleRegion("us-west-1");
    check(answer() === "No. AWS billed 2.1 hours less than our records (0.0 against 2.1). " + noHigher
          + " 1 row is more than 15% below its recorded hours.", "One row below the limit");
    check(rows().length === 1 && cells(rows()[0]) === "m7a.8xlarge|us-west-1|0.0|2.1|-2.1",
          "The lower bill has a negative difference and no Hana column");
""")

    def test_hours_keep_a_disclosure_only_above_the_configured_row_limit(self) -> None:
        self.configure('hours_table_open_row_limit=3')
        checks = {
            ('c7a.4xlarge', region, '2026-01-14'): run_cost.hours_check(Decimal(2), Decimal(1), Decimal('.2'))
            for region in ('us-east-1', 'us-east-2', 'us-west-1', 'ca-central-1')
        }
        with mock.patch.object(run_cost, 'day_hours_from', return_value=checks):
            self.run_browser_checks('', """
    const section = document.getElementById("hours");
    const details = section.querySelector("details");
    check(!details.open && details.querySelector("summary").textContent === "All rows (4)", "Long table starts closed");
    details.querySelector("summary").click();
    check(details.open && details.querySelectorAll("tbody tr").length === 4, "The disclosure shows all four rows");
    document.querySelector("#regions [data-region='ca-central-1']").click();
    check(!section.querySelector("details") && section.querySelectorAll("tbody tr").length === 3,
          "The table opens at the configured limit");
""")

    def test_hours_sort_by_full_difference_and_total_the_displayed_values(self) -> None:
        limit = Decimal('.15')
        self.costs({'billed_hours_gap_max_ratio': str(limit)})
        checks = {
            ('c7a.4xlarge', 'us-east-1', '2026-01-14'): run_cost.hours_check(Decimal('1.16'), Decimal('1.00'), limit),
            ('c7a.4xlarge', 'us-east-2', '2026-01-14'): run_cost.hours_check(Decimal('1.24'), Decimal('1.06'), limit),
        }
        with mock.patch.object(run_cost, 'day_hours_from', return_value=checks):
            self.run_browser_checks('', """
    const section = document.getElementById("hours");
    const answer = () => section.querySelector("p").textContent;
    const rows = () => [...section.querySelectorAll(":scope > .scroll tbody tr")];
    const amounts = row => [...row.cells].slice(2, 5).map(cell => cell.textContent).join("|");
    check(rows().map(amounts).join(";") === "1.2|1.1|+0.1;1.2|1.0|+0.2",
          "Rows sort by the difference before it rounds");
    const totals = [...section.querySelector("tfoot tr").cells].map(cell => cell.textContent).join("|");
    check(totals === "Total|2.4|2.1|+0.3", "The totals sum the displayed values");
    check(answer().startsWith("Yes. AWS billed 0.3 hours more than our records (2.4 against 2.1)."),
          "The answer agrees with the displayed totals");
    document.querySelector("#regions [data-region='us-east-2']").click();
    check(answer().startsWith("Yes. AWS billed 0.2 hours more than our records (1.2 against 1.0)."),
          "The filtered row still matches the answer");
""")

    def test_hours_answer_matches_the_displayed_totals_when_three_differences_round_to_zero(self) -> None:
        limit = Decimal('.04')
        self.costs({'billed_hours_gap_max_ratio': str(limit)})
        checks = {
            ('c7a.4xlarge', region, '2026-01-14'): run_cost.hours_check(Decimal('1.049'), Decimal(1), limit)
            for region in ('us-east-1', 'us-east-2', 'eu-west-1')
        }
        with mock.patch.object(run_cost, 'day_hours_from', return_value=checks):
            self.run_browser_checks('', """
    const section = document.getElementById("hours");
    const totals = [...section.querySelector("tfoot tr").cells].map(cell => cell.textContent).join("|");
    check(totals === "Total|3.0|3.0|0.0", "Three differences of 0.049 round to zero in the table");
    check(section.querySelector(".answer").textContent === "No. AWS billed the same hours as our records (3.0). "
          + "3 rows are more than 4% above their recorded hours plus the hours reserved for Hana.",
          "The summary uses 3.0 against 3.0 and the limits use full precision");
""")

    def test_hours_exclude_unbilled_rows_and_keep_their_regions_in_the_filter(self) -> None:
        limit = Decimal('.2')
        checks = {
            ('c7a.4xlarge', 'us-east-2', '2026-01-12'): run_cost.hours_check(Decimal(1), Decimal(1), limit),
            ('c7a.4xlarge', 'us-west-2', '2026-01-14'): run_cost.hours_check(None, Decimal('1.25'), limit),
            ('c7a.4xlarge', 'ca-west-1', '2026-01-14'): run_cost.hours_check(
                None, Decimal(0), limit, hana_reserved_hours=Decimal('.5')),
        }
        with mock.patch.object(run_cost, 'day_hours_from', return_value=checks):
            self.run_browser_checks('', """
    const section = document.getElementById("hours");
    const rows = [...section.querySelectorAll("tbody tr")];
    check(rows.length === 1 && rows[0].cells[1].textContent === "us-east-2", "The table excludes unbilled rows");
    const totals = [...section.querySelector("tfoot tr").cells].map(cell => cell.textContent).join("|");
    check(totals === "Total|1.0|1.0|0.0", "The totals exclude unbilled rows");
    check(section.querySelector(".answer + .note").textContent === "Bills compared Jan 12, 2026 (UTC).",
          "The range excludes dates without a compared row");
    check(document.querySelector("#regions [data-region='us-west-2']"), "The filter includes the unbilled region");
    check(document.querySelector("#regions [data-region='ca-west-1']"),
          "The filter includes a region with a check and no displayed row");
""")


class AwsPagePlacementScoresTests(PageCostFixture):
    def test_page_shows_latest_median_count_and_missing_or_stale_scores(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        region, missing_region = 'us-east-2', 'eu-north-1'
        location = ledger.SpotPlacementRegion(region)
        zone = ledger.SpotPlacementZone(region, 'use2-az1')
        sizes = ('c7a.8xlarge', 'm7a.8xlarge')
        request = ledger.SpotPlacementRequest(ledger.SpotPlacementScope.REGION,
            ledger.SpotPlacementTypeSet.GATE_32_VCPU, 8, sizes, (region, missing_region))
        all_types = replace(request, type_set=ledger.SpotPlacementTypeSet.ALL_GATE_TYPES,
                            instance_types=(*sizes, 'c7a.4xlarge', 'm7a.4xlarge'))
        zonal = replace(request, scope=ledger.SpotPlacementScope.ZONE)
        failed = ledger.SpotPlacementFailure(replace(all_types, boxes=20), now, 'MaxConfigLimitExceededException')
        readings = (
            ledger.SpotPlacementSample(replace(all_types, boxes=4, instance_types=('c7a.4xlarge',)), now, ()),
            ledger.SpotPlacementSample(replace(request, boxes=20, regions=('retired-region',)), now, ()),
            ledger.SpotPlacementSample(replace(request, instance_types=sizes[::-1]), now - 3600,
                (ledger.SpotPlacementScore(location, 6),)),
            ledger.SpotPlacementSample(request, now, (ledger.SpotPlacementScore(location, 9),)),
            ledger.SpotPlacementSample(all_types, now - 3600, (ledger.SpotPlacementScore(location, 10),)),
            ledger.SpotPlacementSample(all_types, now, ()),
            ledger.SpotPlacementSample(zonal, now - 2 * 3600, (ledger.SpotPlacementScore(zone, 5),)),
            failed,
        )
        ledger.write_spot_placement_scores(self.state, readings, now)
        source = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings).read_text()
        payload = source.split('<script id="aws-prices-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        data = cast(aws_page.PageData, json.loads(payload))
        scores = data['placement_scores']
        self.assertEqual(scores['title'], 'Spot placement scores')
        self.assertEqual(scores['region_axis'], 'rows')
        self.assertEqual(scores['headers'], ['Region', 'Location', '32 vCPU (8 boxes)', 'All types (8 boxes)',
                                             'All types (20 boxes)'])
        rows = {tuple(row[:2]): row[2:] for row in scores['rows']}
        refused = 'Missing (MaxConfigLimitExceededException)'
        self.assertEqual(rows[region, 'Region'], ['9 / 7.5 / 2', 'Stale: 10 / 10 / 1', refused])
        self.assertEqual(rows[missing_region, 'Region'], ['Missing', 'Missing', refused])
        self.assertEqual(rows[region, 'use2-az1'], ['Stale: 5 / 5 / 1', 'Missing', 'Not sampled'])
        self.assertNotIn(('retired-region', 'Region'), rows)
        self.assertIn('<section class="card" id="placement-scores"></section>', source)
        self.assertIn('data.placement_scores', source)
        self.assertIn('AWS scores a request with fewer than three instance types lower. '
                      + 'Compare the 32-vCPU column with recorded launch results.', source)


class AwsPageGateTimePriceTests(PageCostFixture):
    def test_page_compares_the_lowest_run_cost_for_each_size_in_each_region(self) -> None:
        ledger_test_support.override_fixture_settings(self.root / 'build_steps.conf', 'names.gate=fixture gate')
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        east, west, north = 'us-east-2', 'us-west-2', 'eu-north-1'
        candidate_only = 'eu-south-2'
        undeployed_type = 'c7i.4xlarge'
        unmeasured_type = 'c7a.2xlarge'
        slow_type = 'm7a.4xlarge'
        deployed_types = ('c7a.4xlarge', 'c7a.8xlarge', slow_type, 'm7a.8xlarge', unmeasured_type)
        self.text_setting('region', east)
        config = self.root / 'cloud_gate.conf'
        ledger_test_support.override_fixture_settings(config,
            f'regions={east} {west} {north}\nspot_candidate_regions={west} {east} {candidate_only}\n'
            + f'instance_types={" ".join((*deployed_types, undeployed_type))}\n'
            + 'gate_wait_usd_per_minute=0.02\nmax_gate_minutes=12\n'
            + f'region_transfer_usd={json.dumps({east: 0.30, west: 0.08})}\n'
            + f'region_extra_gate_seconds={json.dumps({west: 30})}\n')
        deployment = self.state / 'gate-deployment.json'
        deployed_regions = [
            {'region': east, 'subnets': [{'id': 'subnet-east-a', 'zone': east + 'a'},
                                         {'id': 'subnet-east-c', 'zone': east + 'c'}]},
            {'region': west, 'subnets': [{'id': 'subnet-west', 'zone': west + 'a'}]},
        ]
        _ = deployment.write_text(json.dumps({'instance_types': deployed_types, 'regions': deployed_regions}))
        history: list[dict[str, object]] = []
        prices: list[ledger.RegionalSpotPrice] = []
        for size, rate, seconds in (('c7a.4xlarge', '0.6', 600), (slow_type, '0.6', 780),
                                    ('c7a.8xlarge', '2.4', 360), ('m7a.8xlarge', '1.8', 300)):
            base = {'attempt': size, 'instance_type': size, 'region': east, 'mode': 'gate'}
            history.extend(({**base, 'result': 'launched', 'time': now - 1000},
                            {**base, 'result': 'finished', 'time': now - 1000 + seconds, 'gate_s': seconds - 60}))
            prices.append(ledger.RegionalSpotPrice(east, east + 'a', size, Decimal(rate), now))
        prices.extend((
            ledger.RegionalSpotPrice(east, east + 'b', 'c7a.4xlarge', Decimal('0.006'), now),
            ledger.RegionalSpotPrice(east, east + 'c', 'c7a.4xlarge', Decimal('0.72'), now),
            ledger.RegionalSpotPrice(east, east + 'a', undeployed_type, Decimal('0.012'), now),
            ledger.RegionalSpotPrice(east, east + 'a', unmeasured_type, Decimal('0.1'), now),
            ledger.RegionalSpotPrice(east, east + 'b', 'm7a.4xlarge', Decimal('0.01'), now - 99999),
            ledger.RegionalSpotPrice(east, east + 'b', 'c7a.8xlarge', Decimal('0.01'), now),
            ledger.RegionalSpotPrice(east, east + 'b', 'm7a.8xlarge', Decimal('2'), now),
            ledger.RegionalSpotPrice(west, west + 'a', 'c7a.4xlarge', Decimal('0.6'), now),
            ledger.RegionalSpotPrice(west, west + 'a', 'm7a.8xlarge', Decimal('0.01'), now - 99999),
            ledger.RegionalSpotPrice(north, north + 'a', 'c7a.4xlarge', Decimal('0.01'), now),
        ))
        ledger.write_spot_prices(self.state, prices)
        history_path = self.state / 'cloud-gate-launches.jsonl'
        _ = history_path.write_text(
            ''.join(json.dumps(row) + '\n' for row in sorted(history, key=lambda row: int(str(row['time'])))))
        def publish() -> str:
            return aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings).read_text()

        def payload(source: str, name: str) -> object:
            raw = source.split(f'<script id="{name}" type="application/json">', 1)[1].split('</script>', 1)[0]
            return cast(object, json.loads(raw))

        source = publish()
        data = ledger.mapping(payload(source, 'aws-gate-time-data'))
        table = cast(aws_page.TableData, data['table'])
        self.assertEqual(table['title'], '32-vCPU fixture gate break-even')
        self.assertEqual(table['region_axis'], 'rows')
        self.assertEqual(data['regions'], [east, west, north, candidate_only])
        self.assertEqual([row[0] for row in table['rows']], [east, west, north])
        self.assertEqual(table['rows'][0], [east, 'c7a.4xlarge', '0.6000', '10.0', '0.1000',
                                           'm7a.8xlarge', '1.8000', '5.0', '0.1500', '0.0100 or more'])
        self.assertEqual(table['rows'][1], [west, 'c7a.4xlarge', '0.6000', '10.0', '0.1900',
                                           *(['—'] * 4), 'no current price'])
        self.assertEqual(table['rows'][2], [north, *(['—'] * 8), 'no current price'])

        page = cast(aws_page.PageData, payload(source, 'aws-prices-data'))
        gates = next(costs for costs in page['runs'] if costs['scope'] == 'gate')
        self.assertEqual(gates['label'], 'Fixture gate runs')
        self.assertNotRegex(source.lower(), r'\bmerge (?:gate|check|test)\b')
        gate_prices = gates['prices'][0]
        size = 'c7a.4xlarge'
        column = gate_prices['headers'].index(size)
        prices_by_region = {row[0]: row[column] for row in gate_prices['rows']}
        end = datetime.fromtimestamp(now, UTC)
        days = conf.read(self.settings).whole('run_cost_days')
        evidence = run_cost.run_evidence(history, end - timedelta(days=days), end, east)
        pricing = run_cost.GateRunPricing.from_config(conf.read(config), evidence, east, ('gate',))
        for row in table['rows'][:2]:
            region = row[0]
            with self.subTest(region=region):
                self.assertEqual(prices_by_region[region], row[4])
                self.assertEqual(prices_by_region[region], f'{pricing.expected_usd(Decimal("0.6"), size, region):.4f}')
                self.assertIn({'region': region, 'summary': f'Cheapest fixture gate: {size} in {region}, {row[4]} USD (estimate)'},
                              gates['estimates'])
        spot_prices = page['prices'][0]
        for region, instance_type, spot, gate in (
            (east, size, 0.006, '0.1000'),
            (east, undeployed_type, 0.012, 'no current gate price'),
            (west, 'm7a.8xlarge', 0.01, 'no current gate price'),
        ):
            with self.subTest(region=region, instance_type=instance_type):
                index = next(index for index, row in enumerate(spot_prices['rows']) if row[0] == region)
                self.assertEqual(spot_prices['values'][index][spot_prices['headers'].index(instance_type)], spot)
                self.assertEqual(gate_prices['rows'][index][gate_prices['headers'].index(instance_type)], gate)
        for region, instance_type in ((east, undeployed_type), (west, 'm7a.8xlarge')):
            self.assertFalse(any(entry['region'] == region and instance_type in entry['summary']
                                 for entry in gates['estimates']))
        self.assertLess(source.index('id="placement-scores"'), source.index('id="gate-time-price"'))
        self.assertIn('fill(document.getElementById("gate-time-price"), data.gateTimePrice,', source)

        east_index = next(index for index, row in enumerate(gate_prices['rows']) if row[0] == east)
        with self.subTest(gate='13 minutes above the 12-minute limit'):
            slow_column = gate_prices['headers'].index(slow_type)
            self.assertEqual(gate_prices['rows'][east_index][slow_column], 'too slow')
            self.assertIsNone(gate_prices['values'][east_index][slow_column])
            self.assertFalse(any(slow_type in entry['summary'] for entry in gates['estimates']))
        with self.subTest(gate='current price with no run history'):
            empty_column = gate_prices['headers'].index(unmeasured_type)
            self.assertEqual(gate_prices['rows'][east_index][empty_column], 'no runs')
            self.assertIsNone(gate_prices['values'][east_index][empty_column])
            self.assertNotIn([east_index, empty_column], gate_prices['not_sold'])

        with self.subTest(prices='snapshot changes between reads'):
            updated_prices = tuple(replace(price, usd_per_hour=price.usd_per_hour * Decimal('1.5'))
                                   for price in prices)
            with mock.patch.object(ledger, 'spot_prices_from', side_effect=(tuple(prices), updated_prices)) as read:
                updated_source = publish()
            updated_page = cast(aws_page.PageData, payload(updated_source, 'aws-prices-data'))
            updated_gate = updated_page['runs'][0]['prices'][0]['rows'][east_index][column]
            updated_data = ledger.mapping(payload(updated_source, 'aws-gate-time-data'))
            updated_card = cast(aws_page.TableData, updated_data['table'])
            self.assertEqual(updated_gate, '0.1000')
            self.assertEqual(updated_card['rows'][0][4], updated_gate)
            self.assertEqual(read.call_count, 1)

        values, policy = conf.read(config), conf.read(self.settings)
        with self.subTest(deployment='valid types with a null entry'):
            _ = deployment.write_text(json.dumps({'instance_types': [None, *deployed_types], 'regions': deployed_regions}))
            comparisons = run_cost.gate_comparisons(self.state, now, values, policy)
            small = comparisons[0].small
            self.assertIsInstance(small, run_cost.GateRunEstimate)
            assert isinstance(small, run_cost.GateRunEstimate)
            self.assertEqual(small.instance_type, size)
            self.assertEqual(small.usd_per_run, Decimal('0.1'))
            gates = spot_table.spot_tables(self.state, now, settings_path=self.settings).runs[0]
            self.assertEqual(next(row[column] for row in gates.gate_prices.rows if row[0] == east), '0.1000')
        _ = deployment.write_text(json.dumps({'instance_types': deployed_types, 'regions': deployed_regions}))

        with self.subTest(history='launch before the cutoff and finish inside it'):
            cutoff = now - days * 86400
            crossing = {'attempt': 'cutoff', 'instance_type': size, 'region': east, 'mode': 'gate'}
            recent = [{**row, 'time': now - 460, 'gate_s': 480}
                      if row['attempt'] == slow_type and row['result'] == 'finished' else row for row in history]
            ordered = sorted(recent, key=lambda row: int(str(row['time'])))
            _ = history_path.write_text(
                json.dumps({**crossing, 'result': 'launched', 'time': cutoff - 120}) + '\n'
                + json.dumps({**crossing, 'result': 'finished', 'time': cutoff + 180, 'gate_s': 240}) + '\n'
                + ''.join(json.dumps(row) + '\n' for row in ordered))
            api = AwsCli('fixture', east, 1, execute=mock.Mock(side_effect=AssertionError('Unexpected AWS call')))
            worker = cloud_gate.GateWorker(api, ledger.CostLedger(self.state), self.settings, self.root / 'sidecar',
                                           clock=lambda: now)
            variants = run_cost.read_deployed_gate_variants(self.state, deployed_types, east)
            ranked = worker.rank_variants([row for row in variants if row.region == east
                                           and row.instance_type in (size, slow_type)], 'gate')
            self.assertEqual(ranked[0].instance_type, slow_type)
            gates = spot_table.spot_tables(self.state, now, settings_path=self.settings).runs[0]
            east_prices = next(row for row in gates.gate_prices.rows if row[0] == east)
            self.assertEqual(east_prices[column], '0.1000')
            self.assertEqual(east_prices[gates.gate_prices.headers.index(slow_type)], '0.0900')
            cheapest = next(entry for entry in gates.estimates if entry.region == east)
            self.assertEqual((cheapest.size, cheapest.usd), (ranked[0].instance_type, Decimal('0.09')))
            small = run_cost.gate_comparisons(self.state, now, values, policy)[0].small
            self.assertIsInstance(small, run_cost.GateRunEstimate)
            assert isinstance(small, run_cost.GateRunEstimate)
            self.assertEqual((small.instance_type, small.usd_per_run), (cheapest.size, cheapest.usd))
            recorded = next(table for table in gates.tables if table.title == size)
            self.assertEqual(next(row[2:4] for row in recorded.rows if row[0] == east), ('7.5', '2'))

        deployment.unlink()
        unpriced = run_cost.NoPricedGateVariant()
        expected = tuple(run_cost.GateTimeComparison(region, unpriced, unpriced) for region in (east, west, north))
        for label in ('missing', 'malformed'):
            with self.subTest(deployment=label):
                if label == 'malformed':
                    _ = deployment.write_text('{"regions":[{"region":"us-east-2","subnets":"invalid"}]}')
                tables = spot_table.spot_tables(self.state, now, settings_path=self.settings)
                self.assertFalse(tables.runs[0].estimates)
                self.assertTrue(all(cell == 'no current gate price' for row in tables.runs[0].gate_prices.rows
                                    for cell in row[1:]))
                comparisons = run_cost.gate_comparisons(self.state, now, conf.read(config), conf.read(self.settings))
                self.assertEqual(comparisons, expected)

    def test_page_refresh_redraws_the_break_even_table_and_region_filter(self) -> None:
        now = ledger_test_support.instant('2026-01-15T12:00:00Z')
        before = spot_table.Table('Break-even', ('Region', 'USD/min'), frozenset({1}),
                                  (('us-east-2', '0.0100'),), 'rows')
        after = replace(before, rows=(('us-east-2', '0.0900'), ('test-new-region', 'no current price')))
        with mock.patch.object(aws_page, 'gate_time_price_table', side_effect=(before, after)):
            first = aws_page.publish(self.state, now, self.root / 'page', settings_path=self.settings).read_text()
            second = aws_page.publish(self.state, now + 1, self.root / 'page', settings_path=self.settings).read_text()
        script = first.rsplit('<script>', 1)[1].split('</script>', 1)[0].replace(page_frame.REFRESH_SCRIPT, '')
        fixture = r'''
const assert = require("node:assert/strict");
const ids = new Map();
class Element {
  children = [];
  dataset = {};
  classList = {add() {}, toggle() {}};
  style = {};
  textContent = "";
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this.children = children; }
  createTHead() { return this.appendChild(new Element()); }
  createTBody() { return this.appendChild(new Element()); }
  insertRow() { return this.appendChild(new Element()); }
  querySelector(selector) { return selector === "p" ? this.children[0] ?? null : null; }
  setAttribute() {}
  addEventListener() {}
}
const launchState = new Element();
launchState.append(new Element());
ids.set("launch-state", launchState);
function parse(html) {
  return {getElementById(id) {
    const start = '<script id="' + id + '" type="application/json">';
    return {textContent: html.split(start)[1].split("</script>")[0]};
  }};
}
const document = {
  getElementById(id) {
    if (id.endsWith("-data")) return parse(first).getElementById(id);
    if (!ids.has(id)) ids.set(id, new Element());
    return ids.get(id);
  },
  createElement: () => new Element(),
  createElementNS: () => new Element(),
  querySelectorAll: () => [],
};
const location = {pathname: "/", search: ""};
const fetch = async () => ({ok: true, text: async () => second});
class DOMParser { parseFromString(html) { return parse(html); } }
function addEventListener() {}
let reload;
function createPageRefresh(load) { reload = load; }
function words(node) {
  return typeof node === "string" ? node : [node.textContent, ...node.children.map(words)].join(" ");
}
'''
        check = r'''
(async () => {
  const table = ids.get("gate-time-price");
  assert.match(words(table), /0\.0100/);
  const result = await reload();
  assert.equal(result.readAt, data.read_at);
  assert.doesNotMatch(words(table), /0\.0100/);
  assert.match(words(table), /0\.0900/);
  assert.match(words(table), /test-new-region/);
  assert.match(words(ids.get("regions")), /test-new-region/);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
        source = f'const first = {json.dumps(first)};\nconst second = {json.dumps(second)};\n' + fixture + script + check
        result = subprocess.run([node, '-'], input=source, capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

"""Record the three spending views before they share an amount."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
from typing import override
from unittest import mock

from scripts.aws_target import aws_page, aws_target, config, ledger, spend
from scripts.aws_target.aws_cli import AwsCli
from scripts.aws_target.ledger_test_support import SpendReferenceFixture, instant


HOUR = 3600
DAY = 24 * HOUR
JAN1 = instant('2026-01-01T00:00:00Z')
JAN2 = JAN1 + DAY
JAN3 = JAN2 + DAY
NOON = JAN2 + 12 * HOUR
FEB1 = instant('2026-02-01T00:00:00Z')
ZERO = Decimal(0)
ABSENT = '—'
MALFORMED = 'Expecting property name enclosed in double quotes: line 1 column 2 (char 1)'
PENDING_GATE = ledger.GateLaunchPending()


# Day and month in USD for page, guard, and admission. A dash means no amount.
# An older day records its contribution to the month.
type SpendReference = tuple[tuple[str, str], tuple[str, str], tuple[str, str]]
REFERENCE: dict[str, SpendReference] = {
    '26': (('2', '2'), ('3', '3'), ('2', '2')),
    '27': (('2', '2'), ('5', '5'), ('2', '2')),
    '28': (('2', '2'), ('2', '2'), ('2', '2')),
    '29': (('2', '2'), ('2', '2'), ('2', '2')),
    '30': (('—', '7'), ('3', '10'), ('6', '13')),
    '31 low': (('1.5', '3.5'), ('3', '5'), ('1.5', '3.5')),
    '31 high': (('6', '8'), ('3', '5'), ('6', '8')),
    '32': (('3.449', '5.449'), ('3', '5'), ('3.449', '5.449')),
    '33': (
        ('0.01666666666666666666666666667', '1.016666666666666666666666667'),
        ('0.008333333333333333333333333333', '1.008333333333333333333333333'),
        ('0.01666666666666666666666666667', '1.016666666666666666666666667'),
    ),
    '34': (('2.483', '4.483'), ('1', '3'), ('2.483', '4.483')),
    '35': (('2', '4'), ('3', '92'), ('2', '4')),
    '36': (('2', '4'), ('0', '2'), ('2', '4')),
    '37': (('2', '4'), ('2', '4'), ('2', '32')),
    '38': (('2', '3'), ('7', '9'), ('5', '7')),
    '39': (('2', '4'), ('7', '9'), ('7', '9')),
    '40': (('2', '4'), ('5', '7'), ('2', '4')),
    '41': (('0', '3'), ('0.75', '3.75'), ('0.75', '3')),
    '42': (('3', '—'), ('3', '3'), ('3', '3')),
    '43': (('0.5', '2.5'), ('1', '3'), ('1', '3')),
    '44': (('—', '—'), ('—', '—'), ('2', '2')),
    '45 high': (('2', '4'), ('1', '3'), ('1', '3')),
    '45 low': (('1', '3'), ('2', '4'), ('2', '4')),
    '46': (('0.5', '2.5'), ('1', '3'), ('1', '3')),
    '47': (('0', '2'), ('1', '3'), ('1', '3')),
    '48': (('0', '2'), ('1', '3'), ('1', '3')),
    '49': (('0', '2'), ('5', '7'), ('0', '2')),
    '50': (('0', '2'), ('3', '5'), ('3', '5')),
    '51': (('0.5', '—'), ('1', '28'), ('1', '28')),
    '52': (('0', '0'), ('1', '28'), ('0', '0')),
    '53': (('0', '—'), ('1', '28'), ('—', '—')),
    '54': (('2.4', '2.4'), ('4', '4'), ('1.9', '1.9')),
    '55': (('2.4', '2.4'), ('1', '2.4'), ('1.9', '2.4')),
    '56': (('3', '—'), ('3', '3'), ('0.006', '0.006')),
    '57': (('1.004', '2.008'), ('1.004', '2.008'), ('1.004', '2.008')),
    '58 name': (('3', '—'), ('3', '3'), ('2', '2')),
    '58 period': (('3', '—'), ('3', '3'), ('2', '2')),
    '59': (('3', '—'), ('3', '3'), ('2', '2')),
    '60 duplicate': (('3', '—'), ('3', '3'), ('2', '2')),
    '60 history': (('3', '—'), ('3', '3'), ('2', '2')),
    '61': (('5', '5'), ('5', '5'), ('5', '5')),
    '62 stale': (('2', '4'), ('instance state evidence is stale', 'instance state evidence is stale'), ('2', '4')),
    '62 missing': (('0', '2'), ('instance state evidence is missing', 'instance state evidence is missing'), ('0', '2')),
    '63': (('0', '2'), ('cost evidence is from the future', 'cost evidence is from the future'), ('0', '2')),
    '64 early': (('1', '3'), ('1', '3'), ('1', '3')),
    '64 later': (('2', '4'), ('2', '4'), ('2', '4')),
    '65 early': (('2', '2'), ('2', '2'), ('2', '2')),
    '65 later': (('5', '5'), ('5', '5'), ('5', '5')),
    '66': (('2', '—'), ('2', '2'), ('2', '2')),
    '67': (('1', '3'), ('1', '3'), ('1', '3')),
    '73': (('2', '2'), ('3', '3'), ('2', '2')),
    '74': (('0.5', '2.5'), ('1', '3'), ('1', '3')),
    '75': (('0.5', '2.5'), ('1', '3'), ('1', '3')),
    '76': (('0', '—'), ('0', '0'), ('0', '0')),
    '78': (('0', '—'), ('0', '0'), ('0', '0')),
    '78 without trial': (('0.01666666666666666666666666667', '—'), ('2', '2'), ('2', '2')),
    '79': (('5', '5'), ('5', '5'), ('5', '5')),
    '85 absent': (('0', '2'), ('0', '2'), ('0', '2')),
    '85 refused': (('0', '2'), ('0', '2'), ('0', '2')),
    '85 dispatched': (('2', '4'), ('1', '3'), ('1', '3')),
    '85 expired': (('2', '4'), ('5', '7'), ('5', '7')),
    '86': (('2', '4'), ('2', '4'), ('2', '4')),
    '86 zero marker': (('0', '4'), ('0', '4'), ('2', '6')),
    '87': (('11', '12'), ('11', '12'), ('11', '12')),
    '87 tax carry': (('1', '12'), ('1', '12'), ('1', '12')),
    '88 zero': (('3', '—'), ('3', '3'), ('3', '3')),
    '88 negative': (('3', '—'), ('3', '3'), ('3', '3')),
    '88 legacy': (('3', '—'), ('3', '3'), ('3', '3')),
    '89': (('3', '—'), (MALFORMED, MALFORMED), (MALFORMED, MALFORMED)),
    '90 before': (('0', '2'), ('0', '4'), ('0', '2')),
    '90 after': (('0', '2'), ('0', '2'), ('0', '2')),
    '91 spring': (('3', '—'), ('22', '22'), ('22', '22')),
    '91 fall': (('4', '—'), ('24', '24'), ('24', '24')),
    'N01': (('3', '—'), ('3', '3'), ('8', '8')),
    'N02 initial': (('5', '5'), ('5', '5'), ('5', '5')),
    'N02 revised': (('2', '2'), ('5', '5'), ('2', '2')),
    'N05': (('—', '2'), ('1', '3'), ('2', '4')),
    'N06': (('2', '4'), ('1', '3'), ('2', '4')),
    'N08': (('—', '12'), ('0', '12'), ('5', '17')),
    'N09': (('2', '4'), ('3', '92'), ('2', '4')),
    'N10': (('2', '4'), ('3', '5'), ('5', '7')),
    'N11 initial': (('5', '5'), ('5', '5'), ('5', '5')),
    'N11 higher': (('7', '7'), ('7', '7'), ('7', '7')),
    'N11 lower first': (('3', '3'), ('7', '7'), ('3', '3')),
    'N11 lower second': (('3', '3'), ('3', '3'), ('3', '3')),
    'N11 returned': (('5', '5'), ('7', '7'), ('5', '5')),
    'N12': (('0', '—'), (MALFORMED, MALFORMED), (MALFORMED, MALFORMED)),
    'N13': (
        ('0', '—'),
        ('12.00000000000000000000000001', '12.00000000000000000000000001'),
        ('12.00000000000000000000000001', '12.00000000000000000000000001'),
    ),
}


def utc_day(at: int) -> str:
    return datetime.fromtimestamp(at, UTC).date().isoformat()


def exact_amount(value: Decimal) -> str:
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


class ObservedCap(Decimal):
    """Record the exact amount on the left of the admission cap comparison."""

    def __new__(cls, limit: Decimal) -> ObservedCap:
        return super().__new__(cls, limit)

    def __init__(self, limit: Decimal) -> None:
        self.comparisons: list[Decimal] = []

    @override
    def __le__(self, amount: object) -> bool:
        assert isinstance(amount, Decimal)
        self.comparisons.append(amount)
        return Decimal(self) <= amount


@dataclass(frozen=True)
class AdmissionPeriod:
    start: int
    end: int
    usd: Decimal


@dataclass(frozen=True)
class AdmissionCounts:
    day: tuple[AdmissionPeriod, ...]
    month: tuple[AdmissionPeriod, ...]
    reason: str
    intervals: tuple[ledger.ChargedInterval, ...]
    complete_days: tuple[ledger.CompleteDayAmount, ...]
    gates: tuple[ledger.GateReservation, ...]

    def contribution(self, start: int, end: int) -> Decimal:
        return (ledger.period_cost(self.intervals, start, end)
                + ledger.complete_day_adjustment(self.complete_days, self.intervals, self.gates, start, end))


def admission_counts(book: ledger.CostLedger, settings: config.SpendingSettings, now: int) -> AdmissionCounts:
    """Observe the amounts and windows used by the existing account cap."""
    state = book.read_snapshot()
    costs = ledger.costs_from_evidence(state, settings, now)
    daily_cap = ObservedCap(settings.daily_limit_usd)
    monthly_cap = ObservedCap(settings.monthly_limit_usd)
    bounds: list[tuple[int, int]] = []
    intervals: tuple[ledger.ChargedInterval, ...] = ()
    complete: tuple[ledger.CompleteDayAmount, ...] = ()
    gates: tuple[ledger.GateReservation, ...] = ()
    compare_day = ledger.complete_day_adjustment

    def record_bounds(bills: Sequence[ledger.CompleteDayAmount], charges: Sequence[ledger.ChargedInterval],
                      reserves: Sequence[ledger.GateReservation], start: int, end: int) -> Decimal:
        nonlocal intervals, complete, gates
        bounds.append((start, end))
        intervals, complete, gates = tuple(charges), tuple(bills), tuple(reserves)
        return compare_day(bills, charges, reserves, start, end)

    with mock.patch.object(ledger, 'complete_day_adjustment', side_effect=record_bounds):
        reason = book.reservation_cap(state, replace(settings, daily_limit_usd=daily_cap,
                                                   monthly_limit_usd=monthly_cap), now, costs)
    daily = daily_cap.comparisons
    monthly = monthly_cap.comparisons
    periods = tuple(AdmissionPeriod(start, end, usd)
                    for (start, end), usd in zip(bounds, (*daily, *monthly), strict=True))
    return AdmissionCounts(periods[:len(daily)], periods[len(daily):], reason, intervals, complete, gates)


def spot_bill(usd: str, hours: str, *, region_prefix: str = 'USE1',
              size: str = 'm7a.4xlarge') -> ledger.UsageTypeCost:
    return ledger.UsageTypeCost(f'{region_prefix}-SpotUsage:{size}', Decimal(usd), Decimal(hours), 'Hrs')


def storage_bill(usd: str) -> ledger.UsageTypeCost:
    return ledger.UsageTypeCost('USE1-EBS:VolumeUsage.gp3', Decimal(usd))


@dataclass(frozen=True)
class GateAttemptStatus:
    state: ledger.GateLaunchPending | ledger.GateRunning | ledger.GateAbsent | ledger.GateTerminated
    expires_at: int


def saved_gate_attempts(book: ledger.CostLedger) -> dict[str, GateAttemptStatus]:
    return {row.attempt: GateAttemptStatus(row.state, row.expires) for row in book.read_snapshot().gate_reservations}


class AccountSpendParityTests(SpendReferenceFixture):
    book: ledger.CostLedger = ledger.CostLedger(Path())
    now: int = 0
    original_settings: str = ''

    @override
    def setUp(self) -> None:
        super().setUp()
        self.original_settings = self.settings.read_text()

    def scene(self, key: str, *, now: int = NOON, start: int = JAN1, fixed: str = '0', observe: bool = True) -> None:
        self.now = now
        self.book = ledger.CostLedger(self.state / key)
        _ = self.settings.write_text(self.original_settings)
        self.costs({'daily_limit_usd': '10000', 'monthly_limit_usd': '10000',
                    'limit_reserve_usd': '0', 'observation_max_age_seconds': '2678400',
                    'cost_explorer_interval_seconds': '3600', 'cost_explorer_lookback_days': '4',
                    'cost_explorer_watch_days': '4'})
        with self.book.transaction() as evidence:
            evidence.inventories.append(ledger.ResourceInventory(
                start, (ledger.PricedResource('fixture-storage', Decimal(fixed)),), 'fixture inventory',
                builder_removed=True))
            if observe:
                evidence.observations.append(ledger.InstanceObservation(start, 'stopped', 'fixture'))

    def observe(self, at: int, state: ledger.InstanceState) -> None:
        self.book.observe(ledger.InstanceObservation(at, state, 'fixture'))

    def run_builder(self, start: int, end: int) -> None:
        self.observe(start, 'running')
        self.observe(end, 'stopped')

    def write_bill(self, start: int, usd: str, *usage: ledger.UsageTypeCost, at: int = 0, name: str = '',
                   settled: bool = True, provisional: bool = False, account_bill: bool = True,
                   period_start_shift: int = 0, earlier: tuple[tuple[int, str], ...] = (),
                   invalid_history: bool = False, duplicate: bool = False) -> None:
        end = start + DAY
        read_at = at or min(self.now, end + HOUR)
        bill = ledger.ChargeRead(utc_day(start), read_at, start + period_start_shift, end, Decimal(usd),
                                 ZERO, ZERO, usage, provisional, account_bill)
        record = ledger.charge_record(bill)
        if settled and not earlier:
            earlier = ((read_at - HOUR, usd),)
        if earlier or invalid_history:
            record['amount_reads'] = [] if invalid_history else [
                {'at': moment, 'actual_usd': money} for moment, money in (*earlier, (read_at, usd))]
        path = self.book.directory / 'charges' / (name or f'{bill.day}.json')
        path.parent.mkdir(exist_ok=True)
        document = json.dumps(record)
        _ = path.write_text(('{"actual_usd":"9",' + document[1:]) if duplicate else document)

    def gate(self, start: int, expires: int, rate: str, allowance: str = '0', *,
             outcome: ledger.GateLaunchPending | ledger.GateRunning | ledger.GateAbsent | ledger.GateTerminated
             = PENDING_GATE) -> ledger.GateReservation:
        with self.book.transaction() as evidence:
            row = ledger.GateReservation(f'fixture-{len(evidence.gate_reservations)}', start, expires, Decimal(rate),
                                         Decimal(allowance), outcome, region='us-east-2')
            evidence.gate_reservations.append(row)
        return row

    def storage(self, at: int) -> None:
        with self.book.transaction() as evidence:
            evidence.hana_storage.append(ledger.HanaStorageExposure(at, Decimal(30)))

    def page(self) -> spend.Spend:
        page = spend.read_spend(self.settings, self.book.directory, self.now, utc_day(self.now))
        self.assertIsInstance(page, spend.Spend, page)
        assert isinstance(page, spend.Spend)
        return page

    def guard(self) -> ledger.SpendingDecision:
        return self.book.guard(self.settings, 'admission', self.now, lambda _notice: True, clock=lambda: self.now)

    def project(self) -> AdmissionCounts:
        return admission_counts(self.book, config.read_settings(self.settings), self.now)

    def proposal(self) -> ledger.GateReservation:
        return ledger.GateReservation('fixture-proposal', self.now, self.now + 60, ZERO, ZERO, region='us-east-2')

    def record(self, key: str, *, page_day: str = '', cap_day: str = '', cap_month: str = '') -> None:
        with self.subTest(row=key):
            page = self.page()
            utc = datetime.fromtimestamp(self.now, UTC).date()
            page_key = page_day or utc.isoformat()
            monthly = spend.month_spend(page.days, date.fromisoformat(page_key), 7)
            page_values = (next((exact_amount(row.usd) for row in page.days if row.day == page_key), ABSENT),
                           exact_amount(monthly.to_date) if monthly is not None else ABSENT)
            settings = config.read_settings(self.settings)
            local = datetime.fromtimestamp(self.now, settings.accounting_zone).date()
            day_key = cap_day or local.isoformat()
            month_key = cap_month or day_key[:7]
            day_bounds = ledger.period_bounds(date.fromisoformat(day_key), settings.accounting_zone)
            month_day = date.fromisoformat(month_key + '-01')
            next_month = (month_day + timedelta(days=31)).replace(day=1)
            month_bounds = (ledger.period_bounds(month_day, settings.accounting_zone)[0],
                            ledger.period_bounds(next_month, settings.accounting_zone)[0])
            guard_day = self.book.accrued_cost(self.settings, self.now, window='day')
            guard_month = self.book.accrued_cost(self.settings, self.now, window='month')
            if isinstance(guard_day.limit_count, ledger.CostEvidenceUnavailable):
                guard_values = (guard_day.limit_count.reason, guard_day.limit_count.reason)
            else:
                assert isinstance(guard_month.limit_count, Decimal)
                guard_values = (
                    exact_amount(guard_day.limit_count) if day_key == local.isoformat() else
                    next((exact_amount(row.cost) for row in guard_month.days if row.day.isoformat() == day_key), ABSENT),
                    exact_amount(guard_month.limit_count) if month_key == local.isoformat()[:7] else ABSENT,
                )
            with self.decision_book(self.book) as live:
                self.assert_guard_decisions(live, guard_day.limit_count, guard_month.limit_count)
            try:
                projection = self.project()
            except (OSError, ValueError, ArithmeticError) as error:
                admission_values = (str(error), str(error))
                with self.decision_book(self.book) as live:
                    request = self.proposal()
                    before = saved_gate_attempts(live)
                    with self.assertRaises(type(error)) as refusal:
                        _ = live.reserve_gate(self.settings, request)
                    self.assertEqual(str(refusal.exception), str(error))
                    self.assert_gate_attempts_after_refusal(live, request.attempt, before)
            else:
                day = next((exact_amount(row.usd) for row in projection.day
                            if (row.start, row.end) == day_bounds), ABSENT)
                month = next((exact_amount(row.usd) for row in projection.month
                              if (row.start, row.end) == month_bounds), ABSENT)
                if day == ABSENT and day_key < local.isoformat() and month != ABSENT:
                    day = exact_amount(projection.contribution(*day_bounds))
                admission_values = (day, month)
                with self.decision_book(self.book) as live:
                    self.assert_gate_decisions(live, projection)
            self.assert_baseline(key, (page_values, guard_values, admission_values))

    def assert_guard_decisions(self, book: ledger.CostLedger, day: Decimal | ledger.CostEvidenceUnavailable,
                               month: Decimal | ledger.CostEvidenceUnavailable) -> None:
        def guard() -> ledger.SpendingDecision:
            return book.guard(self.settings, 'admission', self.now, lambda _notice: True, clock=lambda: self.now)

        if isinstance(day, ledger.CostEvidenceUnavailable):
            decision = guard()
            self.assertIsInstance(decision, ledger.SpendingUnavailable)
            assert isinstance(decision, ledger.SpendingUnavailable)
            self.assertEqual(decision.reason, day.reason)
            return
        assert isinstance(month, Decimal)
        self.costs({'daily_limit_usd': '10000', 'monthly_limit_usd': '10000'})
        for period, amount in (('daily', day), ('monthly', month)):
            cap = f'{period}_limit_usd'
            self.replace(cap, str(amount))
            decision = guard()
            self.assertIsInstance(decision, ledger.SpendingRefused)
            assert isinstance(decision, ledger.SpendingRefused)
            self.assertEqual(decision.reason, f'{period}_limit')
            self.replace(cap, str(amount.next_plus()))
            self.assertIsInstance(guard(), ledger.SpendingAllowed)
            self.replace(cap, '10000')

    def assert_gate_decisions(self, book: ledger.CostLedger, projection: AdmissionCounts) -> None:
        request = replace(self.proposal(), attempt='fixture-cap-probe')
        before = saved_gate_attempts(book)
        self.costs({'daily_limit_usd': '10000', 'monthly_limit_usd': '10000'})
        for period, periods in (('daily', projection.day), ('monthly', projection.month)):
            if periods:
                amount = max(row.usd for row in periods)
                cap = f'{period}_limit_usd'
                self.replace(cap, str(amount))
                self.assertEqual(book.reserve_gate(self.settings, request), ledger.GateCapReached(f'{period}_limit'))
                self.assert_gate_attempts_after_refusal(book, request.attempt, before)
                self.replace(cap, '10000')
        for period, periods in (('daily', projection.day), ('monthly', projection.month)):
            if periods:
                self.replace(f'{period}_limit_usd', str(max(row.usd for row in periods).next_plus()))
        self.assertIsInstance(book.reserve_gate(self.settings, request), ledger.GateReserved)

    def assert_gate_attempts_after_refusal(self, book: ledger.CostLedger, refused_attempt: str,
                                           before: dict[str, GateAttemptStatus]) -> None:
        after = saved_gate_attempts(book)
        self.assertNotIn(refused_attempt, after)
        self.assertEqual(after, before)

    def assert_baseline(self, key: str, measured: SpendReference) -> None:
        self.assertEqual(measured, REFERENCE[key], 'Page, guard, and admission amounts')

    def test_bill_settlement_and_floors(self) -> None:
        for row in ('26', '27', '28', '29', '73'):
            now = JAN3 + HOUR if row == '29' else NOON
            self.scene(row, now=now)
            self.run_builder(JAN2 - 3 * HOUR, JAN2)
            if row == '29':
                self.replace('cost_explorer_lookback_days', '1')
            if row == '27':
                with self.book.transaction() as state:
                    state.reconciliations.append(ledger.CostReconciliation(
                        JAN2 + HOUR, JAN1, JAN2, Decimal(5), 'fixture floor',
                        established_days=(ledger.EstablishedDayCost(utc_day(JAN1), Decimal(5)),)))
                self.write_bill(JAN1, '2', spot_bill('2', '2'), at=JAN2 + 2 * HOUR, settled=False,
                                earlier=((JAN2, '5'), (JAN2 + HOUR, '5')))
            else:
                self.write_bill(JAN1, '2', spot_bill('2', '2'), settled=row == '28', provisional=row in ('28', '29'))
            self.record(row, page_day=utc_day(JAN1), cap_day=utc_day(JAN1))
            watch, = self.book.read_charge_watch(config.read_settings(self.settings), self.now)
            if row == '28':
                self.assertEqual(self.page().days[-1].provisional_bill, True)
                self.assertEqual(watch.settlement, ledger.SettledChargeUnchanged(Decimal(2)))
            elif row == '27':
                self.assertEqual(watch.settlement, ledger.SettledChargeChanged(Decimal(5), Decimal(2)))
            else:
                self.assertIsInstance(watch.settlement, ledger.ChargeNotSettled)
            if row == '73':
                self.replace('monthly_limit_usd', '2.50')
                refused = self.guard()
                assert isinstance(refused, ledger.SpendingRefused)
                self.assertEqual(refused.reason, 'monthly_limit')
                self.assertIsInstance(self.book.reserve_gate(self.settings, self.proposal()), ledger.GateReserved)

    def test_rates_carry_and_minimums(self) -> None:
        self.scene('30', now=JAN3 + DAY + 12 * HOUR)
        self.run_builder(JAN2 + HOUR, JAN2 + 4 * HOUR)
        self.write_bill(JAN1, '2', spot_bill('2', '1'))
        self.write_bill(JAN3, '5', spot_bill('5', '2.5'))
        self.record('30', page_day=utc_day(JAN2), cap_day=utc_day(JAN2))

        for row, seconds, bill, usage in (
            ('31 low', 3 * HOUR, '2', spot_bill('2', '4')),
            ('31 high', 3 * HOUR, '2', spot_bill('2', '1')),
            ('32', 3 * HOUR, '2', storage_bill('2')),
            ('33', 30, '1', spot_bill('1', '1')),
            ('34', HOUR, '2', ledger.UsageTypeCost('USE1-DataTransfer-Out-Bytes', Decimal(2))),
            ('N06', HOUR, '2', spot_bill('2', '1', region_prefix='USE2')),
        ):
            self.scene(row)
            self.run_builder(self.now - seconds, self.now)
            self.write_bill(JAN1, bill, usage)
            self.record(row)

        self.scene('36')
        self.write_bill(JAN1, '2')
        self.record('36')

        first = JAN1 + 14 * DAY
        self.scene('37', now=first + DAY + 12 * HOUR, start=first)
        self.write_bill(first, '2', storage_bill('2'))
        self.record('37')

        self.scene('N05', now=JAN3 + 12 * HOUR)
        self.run_builder(JAN1 + HOUR, JAN1 + 2 * HOUR)
        self.write_bill(JAN2, '2', spot_bill('2', '1'))
        self.record('N05', page_day=utc_day(JAN1), cap_day=utc_day(JAN1))

        self.scene('N08', now=JAN3 + DAY + 60)
        self.write_bill(JAN1, '2', storage_bill('2'))
        self.write_bill(JAN3, '5', storage_bill('5'), settled=False)
        self.replace('cost_explorer_unchanged_reads', '1')
        self.record('N08', page_day=utc_day(JAN2), cap_day=utc_day(JAN2))

        self.scene('N09', fixed='93')
        self.book.record_inventory(ledger.ResourceInventory(
            JAN2, (ledger.PricedResource('fixture-storage', Decimal(186)),), 'fixture inventory', True))
        self.book.record_inventory(ledger.ResourceInventory(
            self.now, (ledger.PricedResource('fixture-storage', Decimal(93)),), 'fixture inventory', True))
        self.write_bill(JAN1, '2', storage_bill('2'))
        self.record('N09')

        self.scene('N10')
        self.write_bill(JAN1, '2', ledger.UsageTypeCost('USE1-TimedStorage-ByteHrs', Decimal(2)))
        with self.book.transaction() as state:
            state.hana_storage.append(ledger.HanaStorageExposure(
                JAN1 + 12 * HOUR, Decimal(180), ledger.HanaExposureClosed(self.now)))
        self.record('N10')

    def test_row_35_guard_counts_monthly_fixed_reserves(self) -> None:
        self.scene('35', now=JAN2 + 20 * HOUR, fixed='93')
        self.write_bill(JAN1, '2', storage_bill('2'))
        self.replace('monthly_limit_usd', '10')
        decision = self.guard()
        self.assertIsInstance(decision, ledger.SpendingRefused)
        assert isinstance(decision, ledger.SpendingRefused)
        self.assertEqual(decision.reason, 'monthly_limit')
        self.replace('monthly_limit_usd', '10000')
        self.record('35')

    def test_gate_compute_allowance_and_future_periods(self) -> None:
        self.scene('38', now=JAN2 + HOUR)
        self.write_bill(JAN1, '2', spot_bill('2', '2'), settled=False)
        self.replace('cost_explorer_unchanged_reads', '1')
        _ = self.gate(JAN2 - 2 * HOUR, JAN2 + 2 * HOUR, '1', '3', outcome=ledger.GateRunning('fixture'))
        self.record('38', page_day=utc_day(JAN1), cap_day=utc_day(JAN1))

        for row in ('39', '40'):
            self.scene(row)
            self.write_bill(JAN1, '2', spot_bill('2', '2'))
            status = ledger.GateRunning('fixture') if row == '39' else ledger.GateTerminated(self.now, 'fixture')
            _ = self.gate(self.now - 2 * HOUR, self.now + 2 * HOUR, '1', '3', outcome=status)
            self.record(row)

        self.scene('41', now=JAN3)
        self.write_bill(JAN1, '2', spot_bill('2', '2'))
        self.replace('cost_explorer_unchanged_reads', '1')
        _ = self.gate(self.now - 15, self.now + 45, '60', outcome=ledger.GateRunning('fixture'))
        self.record('41')

        self.scene('42')
        _ = self.gate(self.now, self.now + HOUR, '0', '3')
        self.record('42')

        self.scene('43-44', now=FEB1 - HOUR // 2)
        self.write_bill(FEB1 - 2 * DAY, '2', spot_bill('2', '2'))
        _ = self.gate(FEB1 - HOUR, FEB1 + 2 * HOUR, '1', outcome=ledger.GateRunning('fixture'))
        self.record('43')
        self.record('44', page_day=utc_day(FEB1), cap_day=utc_day(FEB1))
        future_day, = [period for period in self.project().day if period.start == FEB1]
        self.assertEqual(future_day, AdmissionPeriod(FEB1, FEB1 + DAY, Decimal(2)))

    def test_row_44_crossing_proposal_refuses_at_the_future_daily_cap(self) -> None:
        self.scene('44 crossing', now=FEB1 - HOUR // 2)
        self.write_bill(FEB1 - 2 * DAY, '2', spot_bill('2', '2'))
        request = replace(self.proposal(), expires=FEB1 + 2 * HOUR, usd_per_hour=Decimal(1))
        self.replace('daily_limit_usd', '2')
        self.assertEqual(self.book.reserve_gate(self.settings, request), ledger.GateCapReached('daily_limit'))
        self.assertEqual(self.book.read_snapshot().gate_reservations, [])
        self.replace('daily_limit_usd', str(Decimal(2).next_plus()))
        self.assertIsInstance(self.book.reserve_gate(self.settings, request), ledger.GateReserved)

    def test_hana_saved_exposures_and_account_proposals(self) -> None:
        for row, bill_rate, saved_rate in (('45 high', '2', '1'), ('45 low', '1', '2')):
            self.scene(row)
            self.write_bill(JAN1, '2', spot_bill('2', '1' if bill_rate == '2' else '2', region_prefix='USE2'))
            with self.book.transaction() as state:
                state.hana_compute.append(ledger.HanaComputeExposure(
                    self.now - HOUR, self.now, 1, Decimal(saved_rate), ledger.HanaExposureClosed(self.now),
                    'm7a.4xlarge', region='us-east-2'))
            self.record(row)

        for row in ('46', '47', '74', '75'):
            self.scene(row)
            self.write_bill(JAN1, '2', spot_bill('2', '2'))
            self.storage(JAN2 - 1 if row == '47' else JAN2)
            self.record(row)

        self.scene('76', now=JAN2)
        self.assertEqual(self.project().day, ())
        self.assertEqual(self.book.accrued_cost(self.settings, self.now, window='day').limit_count, ZERO)
        request = self.proposal()
        self.assertIsInstance(self.book.reserve_gate(self.settings, request), ledger.GateReserved)
        self.record('76')

        for row in ('85 absent', '85 refused', '85 dispatched', '85 expired'):
            self.scene(row)
            self.write_bill(JAN1, '2', spot_bill('2', '1', region_prefix='USE2'))
            if row == '85 absent':
                _ = self.gate(self.now - 2 * HOUR, self.now - HOUR, '1', '3', outcome=ledger.GateAbsent(self.now))
            elif row == '85 expired':
                _ = self.gate(self.now - 2 * HOUR, self.now - HOUR, '1', '3', outcome=ledger.GateRunning('fixture'))
            else:
                with self.book.transaction() as state:
                    state.hana_compute.append(ledger.HanaComputeExposure(
                        self.now - HOUR, self.now, 1, Decimal(1), ledger.HanaExposureClosed(self.now),
                        'm7a.4xlarge', region='us-east-2'))
                journal = self.book.directory / 'hana' / 'provider'
                journal.mkdir(parents=True)
                dispatch: dict[str, object] = {'action': 'start', 'generation': 1, 'dispatched': True}
                if row == '85 refused':
                    dispatch['start_refusal_code'] = 'InsufficientInstanceCapacity'
                _ = (journal / 'start-fixture.json').write_text(json.dumps(dispatch))
            self.record(row)

    def test_fees_floors_and_fixed_cost_reserves(self) -> None:
        self.scene('48', start=JAN2)
        self.costs({'cost_explorer_read_usd': '1', 'cost_explorer_interval_seconds': str(DAY),
                    'cost_explorer_unchanged_reads': '1'})
        self.write_bill(JAN1, '2', spot_bill('2', '2'), settled=False)
        self.record('48')

        self.scene('49')
        self.write_bill(JAN1, '2', spot_bill('2', '2'))
        with self.book.transaction() as state:
            state.reconciliations.append(ledger.CostReconciliation(
                self.now, JAN2, self.now, Decimal(5), 'fixture floor'))
        self.record('49')

        self.scene('50')
        self.write_bill(JAN1, '2', spot_bill('2', '2'))
        self.replace('limit_reserve_usd', '3')
        self.record('50')

        for row, now, start in (('51', FEB1 + 12 * HOUR, FEB1),
                                ('52', FEB1, FEB1 - DAY), ('53', FEB1, FEB1)):
            self.scene(row, now=now, start=start, fixed='28')
            if row == '52':
                self.replace('cost_explorer_unchanged_reads', '1')
                self.write_bill(FEB1 - DAY, '2', spot_bill('2', '2'), settled=False)
            self.record(row)

    def test_local_windows_and_daylight_saving(self) -> None:
        jan15 = JAN1 + 14 * DAY
        for row, bill_day, now, hours in (('54', FEB1, FEB1 + DAY + 2 * HOUR, 4),
                                         ('55', jan15, jan15 + DAY + HOUR, 1)):
            self.scene(row, now=now, start=bill_day)
            self.text_setting('accounting_zone', 'America/New_York')
            self.run_builder(bill_day + 5 * HOUR, bill_day + (5 + hours) * HOUR)
            self.write_bill(bill_day, '2.40', spot_bill('2.40', '2.40'))
            self.record(row, page_day=utc_day(bill_day))

        for row, start, now, duration in (
            ('91 spring', instant('2026-03-08T00:00:00-05:00'), instant('2026-03-08T23:00:00-04:00'), 23),
            ('91 fall', instant('2026-11-01T00:00:00-04:00'), instant('2026-11-01T23:00:00-05:00'), 25),
        ):
            self.scene(row, now=now, start=start, observe=False)
            self.text_setting('accounting_zone', 'America/New_York')
            self.run_builder(start, now)
            self.record(row)
            period, = self.project().day
            self.assertEqual((period.start, period.end), (start, start + duration * HOUR))

    def admission_bill_fixture(self, row: str) -> None:
        self.scene(row)
        if row != 'N12':
            self.run_builder(JAN2 - 3 * HOUR, JAN2)
        if row in ('89', 'N12'):
            path = self.book.directory / 'charges' / f'{utc_day(JAN1)}.json'
            path.parent.mkdir()
            _ = path.write_text('{')
            return
        usd = '8' if row == 'N01' else '.006' if row == '56' else '2'
        self.write_bill(JAN1, usd, spot_bill(usd, '3'), name='alias.json' if row in ('N01', '58 name') else '',
                        at=self.now + HOUR if row == '59' else 0,
                        period_start_shift=1 if row == '58 period' else 0,
                        duplicate=row == '60 duplicate', invalid_history=row == '60 history')

    def test_committed_admission_bill_references(self) -> None:
        # Run this test unchanged at 64286e52d0 and at the dependency tip.
        for row, expected in (('N01', '8'), ('56', '.006'), ('58 name', '2'), ('58 period', '2'),
                              ('59', '2'), ('60 duplicate', '2'), ('60 history', '2'),
                              ('89', MALFORMED), ('N12', MALFORMED)):
            with self.subTest(row=row):
                self.admission_bill_fixture(row)
                request = self.proposal()
                before = saved_gate_attempts(self.book)
                if expected == MALFORMED:
                    with self.assertRaises(json.JSONDecodeError) as refusal:
                        _ = self.book.reserve_gate(self.settings, request)
                    self.assertEqual(str(refusal.exception), MALFORMED)
                    self.assert_gate_attempts_after_refusal(self.book, request.attempt, before)
                    continue
                monthly, = self.project().month
                self.assertEqual(monthly, AdmissionPeriod(JAN1, FEB1, Decimal(expected)))
                self.replace('monthly_limit_usd', '4' if row == 'N01' else expected)
                self.assertEqual(self.book.reserve_gate(self.settings, request), ledger.GateCapReached('monthly_limit'))
                self.assert_gate_attempts_after_refusal(self.book, request.attempt, before)
                self.replace('monthly_limit_usd', str(Decimal(expected) + Decimal('.001')))
                self.assertIsInstance(self.book.reserve_gate(self.settings, request), ledger.GateReserved)

    def test_invalid_bills_and_competing_records(self) -> None:
        for row, rejection in (
            ('56', ledger.BillRecordRejection.BELOW_ONE_CENT),
            ('58 name', ledger.BillRecordRejection.UTC_DAY_MISMATCH),
            ('58 period', ledger.BillRecordRejection.UTC_DAY_MISMATCH),
            ('59', ledger.BillRecordRejection.FUTURE_READ),
            ('60 duplicate', ledger.BillRecordRejection.DUPLICATE_FIELDS),
            ('60 history', ledger.BillRecordRejection.INVALID_HISTORY),
            ('89', ledger.BillRecordRejection.MALFORMED),
            ('N01', ledger.BillRecordRejection.UTC_DAY_MISMATCH),
            ('N12', ledger.BillRecordRejection.MALFORMED),
        ):
            self.admission_bill_fixture(row)
            self.record(row, page_day=utc_day(JAN1), cap_day=utc_day(JAN1))
            self.assertEqual(tuple(item.reason for item in self.book.refused_bill_records(self.now)), (rejection,))
            if row in ('89', 'N12'):
                guard = self.guard()
                assert isinstance(guard, ledger.SpendingUnavailable)
                self.assertEqual(guard.reason, MALFORMED)

        self.scene('57', now=JAN3 + 12 * HOUR)
        for day in (JAN1, JAN2):
            self.write_bill(day, '1.004', spot_bill('1.004', '1'))
        self.record('57', page_day=utc_day(JAN2), cap_day=utc_day(JAN2))
        data = aws_page.spend_data(self.page(), self.now, 7)
        self.assertEqual([tile['usd'] for tile in data['tiles'][1:]], ['$1.00', '$2.01'])

        self.scene('61')
        self.write_bill(JAN1, '5', spot_bill('5', '5'))
        self.write_bill(JAN1, '2', spot_bill('2', '2'), at=JAN2 + 2 * HOUR, name='zz-competing.json')
        self.record('61', page_day=utc_day(JAN1), cap_day=utc_day(JAN1))

        for row, usd, account in (('88 zero', '0', True), ('88 negative', '-2', True),
                                   ('88 legacy', '2', False)):
            self.scene(row)
            self.run_builder(JAN2 - 3 * HOUR, JAN2)
            self.write_bill(JAN1, usd, account_bill=account)
            self.record(row, page_day=utc_day(JAN1), cap_day=utc_day(JAN1))

    def test_evidence_and_evaluation_time(self) -> None:
        for row, observed in (('62 stale', True), ('62 missing', False)):
            self.scene(row, start=NOON - 2 * HOUR, observe=observed)
            self.replace('observation_max_age_seconds', str(HOUR))
            self.write_bill(JAN1, '2', spot_bill('2', '1'))
            self.record(row)
            guard = self.guard()
            assert isinstance(guard, ledger.SpendingUnavailable)
            self.assertEqual(guard.reason, 'instance state evidence is stale' if observed
                             else 'instance state evidence is missing')

        self.scene('63')
        self.write_bill(JAN1, '2', spot_bill('2', '2'))
        self.book.reconcile(ledger.CostReconciliation(JAN3, JAN2, JAN3, Decimal(5), 'fixture future floor'))
        self.record('63')
        guard = self.guard()
        assert isinstance(guard, ledger.SpendingUnavailable)
        self.assertEqual(guard.reason, 'cost evidence is from the future')

        self.scene('64', now=JAN2 + HOUR)
        self.write_bill(JAN1, '2', spot_bill('2', '2'), settled=False)
        self.replace('cost_explorer_unchanged_reads', '1')
        self.observe(JAN2, 'running')
        self.record('64 early')
        self.now += HOUR
        self.record('64 later')

        self.scene('65')
        self.write_bill(JAN1, '2', spot_bill('2', '2'))
        self.record('65 early', page_day=utc_day(JAN1), cap_day=utc_day(JAN1))
        self.write_bill(JAN1, '5', spot_bill('5', '5'), at=JAN2 + 3 * HOUR)
        self.record('65 later', page_day=utc_day(JAN1), cap_day=utc_day(JAN1))

        self.scene('66')
        self.replace('fallback_usd_per_hour', '2')
        self.run_builder(self.now - HOUR, self.now)
        other = self.root / 'report-settings.conf'
        _ = other.write_text(self.original_settings)
        self.record('66')
        self.assertEqual(spend.read_spend(other, self.book.directory, self.now, utc_day(self.now)), self.page())

        jan15 = JAN1 + 14 * DAY
        self.scene('67', now=jan15 + DAY + 12 * HOUR)
        self.write_bill(jan15, '2', spot_bill('2', '2'))
        self.run_builder(self.now - HOUR, self.now)
        self.record('67')
        month = spend.month_spend(self.page().days, date.fromisoformat(utc_day(self.now)), 7)
        assert month is not None
        self.assertEqual((month.to_date, month.forecast), (Decimal(3), Decimal(34)))

    def test_gate_trial_cap_and_saved_attempt_order(self) -> None:
        self.scene('78')
        self.record('78')
        request = replace(self.proposal(), expires=self.now + 2 * HOUR, usd_per_hour=Decimal(1),
                          mode='gpu-size-trial', trial=ledger.GateTrialBudget('fixture-trial', Decimal(1)))
        with mock.patch.object(self.book, 'reservation_cap', side_effect=AssertionError('account cap ran first')):
            self.assertEqual(self.book.reserve_gate(self.settings, request), ledger.GateCapReached('gpu_size_trial_limit_usd'))
        self.assertEqual(self.book.read_snapshot().gate_reservations, [])
        self.assertIsInstance(self.book.reserve_gate(self.settings, replace(request, trial=ledger.NoGateTrial())),
                              ledger.GateReserved)
        self.record('78 without trial')

        self.scene('79')
        request = self.proposal()
        saved = self.book.reserve_gate(self.settings, request)
        assert isinstance(saved, ledger.GateReserved)
        self.write_bill(JAN1, '5', spot_bill('5', '5'))
        self.record('79', page_day=utc_day(JAN1), cap_day=utc_day(JAN1))
        self.replace('monthly_limit_usd', '5')
        with mock.patch.object(self.book, 'reservation_cap', side_effect=AssertionError('retry ran the account cap')):
            self.assertEqual(self.book.reserve_gate(self.settings, request), saved)
        self.assertEqual(self.book.reserve_gate(self.settings, replace(request, attempt='fixture-new')),
                         ledger.GateCapReached('monthly_limit'))

    def test_closed_carry_and_bill_query(self) -> None:
        for row in ('86', '86 zero marker'):
            self.scene(row, now=NOON if row == '86' else JAN3 + 12 * HOUR)
            self.write_bill(JAN1, '2', storage_bill('2'))
            self.run_builder(JAN1 + HOUR, JAN2 - HOUR)
            if row == '86 zero marker':
                self.write_bill(JAN2, '0')
                self.assertEqual(tuple(item.reason for item in self.book.refused_bill_records(self.now)),
                                 (ledger.BillRecordRejection.BELOW_ONE_CENT,))
            self.record(row, page_day=utc_day(JAN2), cap_day=utc_day(JAN2))

        self.scene('87')
        groups = [{'Keys': [name], 'Metrics': {'UnblendedCost': {'Amount': amount, 'Unit': 'USD'},
                                             'UsageQuantity': {'Amount': '1', 'Unit': 'Hrs'}}}
                  for name, amount in (('USE1-SpotUsage:m7a.4xlarge', '10'), ('Tax', '1'))]
        api = AwsCli('fixture', 'us-east-1', 1)
        result = {'ResultsByTime': [{'TimePeriod': {'Start': utc_day(JAN1), 'End': utc_day(JAN2)},
                                     'Estimated': False, 'Groups': groups}]}
        with mock.patch.object(api, 'call', return_value=result) as call:
            bill, = aws_target.fetch_account_charges(api, JAN1, JAN2, self.now, ZERO)
            call.assert_called_once_with(
                'get-cost-and-usage', '--time-period', 'Start=2026-01-01,End=2026-01-02',
                '--granularity', 'DAILY', '--metrics', 'UnblendedCost', 'UsageQuantity', '--group-by',
                'Type=DIMENSION,Key=USAGE_TYPE', '--filter',
                json.dumps({'Not': {'Dimensions': {'Key': 'RECORD_TYPE', 'Values': ['Credit', 'Refund']}}}),
                '--no-paginate', service='ce')
        self.assertEqual(bill.actual_usd, Decimal(11))
        self.write_bill(JAN1, '11', *bill.usage_types)
        self.record('87', page_day=utc_day(JAN1), cap_day=utc_day(JAN1))
        self.record('87 tax carry')

    def test_saved_query_across_lookback_rollover(self) -> None:
        self.scene('90', now=JAN3 - 1)
        self.costs({'cost_explorer_lookback_days': '1', 'cost_explorer_interval_seconds': str(DAY)})
        self.run_builder(JAN2 - 4 * HOUR, JAN2)
        self.write_bill(JAN1, '2', spot_bill('2', '2'), at=NOON, settled=False)
        _ = self.gate(JAN3 - 60, JAN3 + 60, '0', outcome=ledger.GateRunning('fixture'))
        self.record('90 before')
        self.now = JAN3
        self.record('90 after')

        def unexpected_query(_start: int, _end: int) -> tuple[ledger.ChargeRead, ...]:
            self.fail('The saved query is still inside the wait interval')

        read = self.book.refresh_charges(self.settings, self.now, unexpected_query)
        assert isinstance(read, ledger.ChargeRead)
        self.assertEqual((read.day, read.at, read.actual_usd), (utc_day(JAN1), NOON, Decimal(2)))

    def test_guard_floor_survives_a_restart_and_lower_bill(self) -> None:
        self.scene('N02')
        self.run_builder(JAN2 - 3 * HOUR, JAN2)
        self.write_bill(JAN1, '5', spot_bill('5', '5'))
        self.book.reconcile(ledger.CostReconciliation(
            JAN2 + HOUR, JAN1, JAN2, Decimal(5), 'fixture floor'))
        self.assertIsInstance(self.guard(), ledger.SpendingAllowed)
        self.record('N02 initial', page_day=utc_day(JAN1), cap_day=utc_day(JAN1))
        self.replace('monthly_limit_usd', '4')
        initial = self.guard()
        assert isinstance(initial, ledger.SpendingRefused)
        self.assertEqual(initial.reason, 'monthly_limit')
        self.write_bill(JAN1, '2', spot_bill('2', '2'), at=JAN2 + 2 * HOUR, settled=False,
                        earlier=((JAN2, '5'), (JAN2 + HOUR, '5')))
        command = """
import sys
from pathlib import Path
from scripts.aws_target import ledger

book = ledger.CostLedger(Path(sys.argv[1]))
reading, = book.read_snapshot().reconciliations
floor, = reading.established_days
decision = book.guard(Path(sys.argv[2]), 'admission', int(sys.argv[3]),
                      lambda _notice: True, clock=lambda: int(sys.argv[3]))
assert isinstance(decision, ledger.SpendingRefused)
print(floor.day, floor.usd, decision.reason)
"""
        child = subprocess.run([sys.executable, '-c', command, str(self.book.directory), str(self.settings), str(self.now)],
                               cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, check=True,
                               timeout=30)
        self.assertEqual(child.stdout.strip(), '2026-01-01 5 monthly_limit')
        self.book = ledger.CostLedger(self.book.directory)
        self.record('N02 revised', page_day=utc_day(JAN1), cap_day=utc_day(JAN1))
        reading, = self.book.read_snapshot().reconciliations
        self.assertEqual(reading.established_days, (ledger.EstablishedDayCost('2026-01-01', Decimal(5)),))

    def test_spot_revisions_keep_the_floor_and_watch_separate(self) -> None:
        self.scene('N11')
        self.run_builder(JAN2 - 4 * HOUR, JAN2)
        series: tuple[tuple[int, str], ...] = ()
        for stage, hour, usd in (('initial', 1, '5'), ('higher', 2, '7'), ('lower first', 3, '3'),
                                 ('lower second', 4, '3'), ('returned', 5, '5')):
            if not series:
                series = ((JAN2, usd),)
            at = JAN2 + hour * HOUR
            self.write_bill(JAN1, usd, spot_bill(usd, usd), at=at, earlier=series)
            series = (*series, (at, usd))
            if stage == 'higher':
                self.book.reconcile(ledger.CostReconciliation(at, JAN1, JAN2, Decimal(7), 'fixture floor'))
                self.assertIsInstance(self.guard(), ledger.SpendingAllowed)
            self.record(f'N11 {stage}', page_day=utc_day(JAN1), cap_day=utc_day(JAN1))
            watch, = self.book.read_charge_watch(config.read_settings(self.settings), self.now)
            expected = (ledger.SettledChargeUnchanged(Decimal(5)) if usd == '5'
                        else ledger.SettledChargeChanged(Decimal(5), Decimal(usd)))
            self.assertEqual(watch.settlement, expected)
        reading, = self.book.read_snapshot().reconciliations
        self.assertEqual(reading.established_days, (ledger.EstablishedDayCost('2026-01-01', Decimal(7)),))

    def test_account_limit_operators_at_full_precision(self) -> None:
        self.scene('N13', start=JAN2)
        self.costs({'cost_explorer_read_usd': '12.00000000000000000000000001',
                    'cost_explorer_interval_seconds': str(DAY)})
        self.record('N13')
        exact = Decimal('12.00000000000000000000000001')
        for period in ('daily', 'monthly'):
            with self.subTest(period=period):
                key = f'{period}_limit_usd'
                self.replace(key, str(exact))
                decision = self.guard()
                assert isinstance(decision, ledger.SpendingRefused)
                self.assertEqual(decision.reason, f'{period}_limit')
                self.assertEqual(self.book.reserve_gate(self.settings, self.proposal()),
                                 ledger.GateCapReached(f'{period}_limit'))
                self.assertEqual(self.book.read_snapshot().gate_reservations, [])
                self.replace(key, str(exact.next_plus()))
                self.assertIsInstance(self.guard(), ledger.SpendingAllowed)
                # The reference comparison runs at the next representable cap.
                self.assertEqual(self.project().reason, '')
                self.replace(key, '10000')

"""Link attempts to physical lifetimes without charging renewals or retries twice."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import override

from scripts.aws_target import config, hana_result, ledger
from scripts.aws_target.ledger_test_support import CostFixture, instant
from scripts.aws_target.test_hana_result import attempt_key
from scripts.buildlog import aws_report


class HanaAccountingTests(CostFixture):
    start: int = instant('2026-01-15T00:00:00+00:00')
    book: ledger.CostLedger = ledger.CostLedger(Path())

    @override
    def setUp(self) -> None:
        super().setUp()
        self.book = ledger.CostLedger(self.state)

    def evidence(self) -> ledger.LedgerEvidence:
        return ledger.LedgerEvidence(inventories=[ledger.ResourceInventory(self.start, (), 'fixture', builder_removed=True)],
            hana_compute=[ledger.HanaComputeExposure(self.start, self.start + 600, 2,
            Decimal('.2'), ledger.HanaExposureClosed(self.start + 3600), 'm7a.2xlarge', region='us-east-1')],
            hana_storage=[ledger.HanaStorageExposure(self.start, Decimal('30'))])

    def test_attempt_links_survive_restart_and_add_no_compute(self) -> None:
        first = hana_result.HanaAttemptCharge(attempt_key(), hana_result.LogicalStepLink('call', 'episode'),
            1000, 2000, 4, float(self.start), float(self.start + 10), ('hana-compute:2',))
        self.book.record_hana_attempt(first)
        ledger.CostLedger(self.state).record_hana_attempt(first)
        fallback = replace(first, key=attempt_key('replacement'), uploaded_bytes=0, downloaded_bytes=0)
        self.book.record_hana_attempt(fallback)
        state = self.book.read_snapshot()
        self.assertEqual(state.hana_attempts, [first, fallback])
        self.assertEqual(state.hana_compute, [])
        self.assertEqual(ledger.hana_intervals(state, self.start + 3600, 60), ())
        with self.assertRaises(ledger.LedgerUnavailable):
            self.book.record_hana_attempt(replace(first, requests=99))
        self.assertEqual(self.book.read_snapshot().hana_attempts, [first, fallback])

    def test_open_lifetime_charges_idle_after_command_and_expiry_until_stop(self) -> None:
        state = self.evidence()
        state.hana_compute = [replace(state.hana_compute[0], state=ledger.HanaExposureOpen())]
        later = self.start + 7200
        open_cost = ledger.hana_intervals(state, later, 60)
        self.assertEqual(sum((row.usd for row in open_cost if row.kind == 'instance'), Decimal(0)), Decimal('.4'))
        state.hana_compute = self.evidence().hana_compute
        stopped_cost = ledger.hana_intervals(state, later, 60)
        self.assertEqual(sum((row.usd for row in stopped_cost if row.kind == 'instance'), Decimal(0)), Decimal('.2'))
        self.assertEqual(max(row.end for row in stopped_cost if row.kind == 'fixed'), later)

    def bill(self, rate: str) -> ledger.ChargeRead:
        return ledger.ChargeRead('2026-01-15', self.start + 86400, self.start, self.start + 86400,
            Decimal(rate) + 1, Decimal(0), Decimal(0), (
                ledger.UsageTypeCost('SpotUsage:m7a.2xlarge', Decimal(rate), Decimal(1), 'Hrs'),
                ledger.UsageTypeCost('EBS:VolumeUsage.gp3', Decimal(1))), False)

    def test_delayed_and_revised_bills_replace_estimates_without_double_storage(self) -> None:
        state = self.evidence()
        settings = config.read_settings(self.settings)
        now = self.start + 2 * 86400
        estimated = next(row for row in ledger.gate_spend_days(state, settings, (), now) if row.day == '2026-01-15')
        groups = {row.group: row.usd for row in estimated.groups}
        self.assertEqual(groups['kept-machine compute'], Decimal('.2'))
        self.assertEqual(groups['storage'], Decimal(1))
        for rate in ('.1', '.15'):
            with self.subTest(rate=rate):
                days = ledger.gate_spend_days(state, settings, (self.bill(rate),), now)
                billed = next(row for row in days if row.day == '2026-01-15')
                self.assertEqual(billed.usd, Decimal(rate) + 1)
                self.assertEqual(billed.source, 'AWS bill')
                groups = {row.group: row.usd for row in billed.groups}
                self.assertEqual(groups['kept-machine compute'], Decimal(rate))
                self.assertEqual(groups['storage'], Decimal(1))
                later = next(row for row in days if row.day == '2026-01-16')
                self.assertEqual({row.group: row.usd for row in later.groups}['storage'], Decimal(1))
                lines = '\n'.join(aws_report.daily_spend_lines([billed], Decimal(20), billed.day))
                self.assertIn('Kept-machine compute', lines)
                self.assertIn(f'${Decimal(rate):.2f}', lines)

    def test_earlier_bill_does_not_hide_new_storage(self) -> None:
        state = self.evidence()
        state.hana_compute = []
        previous = replace(self.bill('.1'), day='2026-01-14', period_start=self.start - 86400,
            period_end=self.start, at=self.start, actual_usd=Decimal('.1'),
            usage_types=(ledger.UsageTypeCost('SpotUsage:m7a.2xlarge', Decimal('.1'), Decimal(1), 'Hrs'),))
        days = ledger.gate_spend_days(state, config.read_settings(self.settings), (previous,), self.start + 86400)
        today = next(row for row in days if row.day == '2026-01-15')
        self.assertEqual({row.group: row.usd for row in today.groups}['storage'], Decimal(1))
        self.assertEqual(sum((row.usd for row in today.intervals if row.source == 'Hana storage'), Decimal(0)), Decimal(1))

    def test_regional_bill_rows_allocate_each_lifetime_once_in_its_region(self) -> None:
        for region in ('us-east-1', 'us-east-2'):
            with self.subTest(region=region):
                state = self.evidence()
                state.hana_compute = [replace(state.hana_compute[0], region=region)]
                bill = replace(self.bill('.2'), actual_usd=Decimal('.6'), usage_types=(
                    ledger.UsageTypeCost('SpotUsage:m7a.2xlarge', Decimal('.1'), Decimal('.5'), 'Hrs'),
                    ledger.UsageTypeCost('USE1-SpotUsage:m7a.2xlarge', Decimal('.1'), Decimal('.5'), 'Hrs'),
                    ledger.UsageTypeCost('USE2-SpotUsage:m7a.2xlarge', Decimal('.4'), Decimal(1), 'Hrs')))
                now = self.start + 86400
                billed = next(row for row in ledger.gate_spend_days(state, config.read_settings(self.settings),
                    (bill,), now) if row.day == '2026-01-15')
                groups = {row.group: row.usd for row in billed.groups}
                kept = Decimal('.2') if region == 'us-east-1' else Decimal('.4')
                self.assertEqual(groups['kept-machine compute'], kept)
                self.assertEqual(groups['other'], bill.actual_usd - kept)
                self.assertEqual(billed.usd, bill.actual_usd)
                with self.book.transaction() as stored:
                    stored.hana_compute = state.hana_compute
                self.assertEqual(self.book.read_snapshot().hana_compute, state.hana_compute)

    def test_two_links_share_one_physical_lifetime_and_storage_until_deletion(self) -> None:
        state = self.evidence()
        before = ledger.hana_intervals(state, self.start + 7200, 60)
        for attempt in ('remote', 'fallback'):
            state.hana_attempts.append(hana_result.HanaAttemptCharge(attempt_key(attempt),
                hana_result.LogicalStepLink('call', 'episode'), 100, 200, 2,
                float(self.start + 100), float(self.start + 200), ('hana-compute:2',)))
        self.assertEqual(ledger.hana_intervals(state, self.start + 7200, 60), before)
        state.hana_storage = [replace(state.hana_storage[0], state=ledger.HanaExposureClosed(self.start + 7200))]
        self.assertEqual(max(row.end for row in ledger.hana_intervals(state, self.start + 9000, 60)
                             if row.kind == 'fixed'), self.start + 7200)

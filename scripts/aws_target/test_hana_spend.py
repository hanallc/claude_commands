"""Check Hana permits against shared account exposure with private files."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.aws_target import config, hana_spend, ledger, spend
from scripts.aws_target.ledger_test_support import (
    SpendReferenceFixture, git_only, install_hana_spending_settings, instant, numeric_settings, text_setting,
)
from scripts.lib import conf


class HanaSpendTests(SpendReferenceFixture):
    book: ledger.CostLedger  # pyright: ignore[reportUninitializedInstanceVariable]
    controller: hana_spend.HanaSpending[hana_spend.PermitSettings]  # pyright: ignore[reportUninitializedInstanceVariable]
    workload: hana_spend.PermitSettings  # pyright: ignore[reportUninitializedInstanceVariable]
    start: int = instant('2026-01-15T00:00:00+00:00')

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(subprocess, 'run', side_effect=git_only))
        private = self.root / 'hana_aws.conf'
        install_hana_spending_settings(private)
        self.workload = hana_spend.read_settings(conf.read(private))
        self.costs({'daily_limit_usd': '35', 'limit_reserve_usd': '0'})
        self.book = ledger.CostLedger(self.state)
        self.book.record_inventory(ledger.ResourceInventory(self.start, (), 'private inventory', builder_removed=True))
        self.book.observe(ledger.InstanceObservation(self.start, 'terminated', 'private account inventory'))
        self.controller = hana_spend.HanaSpending(self.book, self.settings, self.workload)

    def issue(self, now: int, *, generation: int = 0) -> hana_spend.PermitIssued | hana_spend.PermitRefused:
        return self.controller.issue(now, now + self.workload.permit_max_seconds, 'private-boot', expected_generation=generation)

    def issued(self, now: int, *, generation: int = 0) -> hana_spend.PermitIssued:
        result = self.issue(now, generation=generation)
        self.assertIsInstance(result, hana_spend.PermitIssued)
        assert isinstance(result, hana_spend.PermitIssued)
        return result

    def assert_account_projection(self, candidate: ledger.LedgerEvidence, now: int,
                                  day: Decimal, month: Decimal) -> None:
        decision = self.book.placement_spending_preview(candidate, self.settings, now)
        costs, policy = decision.evidence.costs, decision.evidence.policy
        assert isinstance(costs, ledger.CostEstimate) and isinstance(policy, config.SpendingSettings)
        self.assertEqual((costs.daily.usd + decision.evidence.reserved_daily_fixed_usd + policy.limit_reserve_usd,
                          costs.monthly.usd + decision.evidence.reserved_monthly_fixed_usd + policy.limit_reserve_usd),
                         (day, month))
        # Compare each admission count at equality and at the next Decimal value.
        policy = replace(policy, daily_limit_usd=day.next_plus(), monthly_limit_usd=month.next_plus())
        for settings, reason in ((policy, ''), (replace(policy, daily_limit_usd=day), 'daily_limit'),
                                 (replace(policy, monthly_limit_usd=month), 'monthly_limit')):
            self.assertEqual(self.book.reservation_cap(candidate, settings, now, costs), reason)

    def test_row_n01_both_hana_checks_refuse_the_wrong_name_bill(self) -> None:
        prior = self.start - 86400
        with self.book.transaction() as state:
            state.inventories = [ledger.ResourceInventory(prior, (), 'fixture inventory', builder_removed=True)]
            state.observations = [
                ledger.InstanceObservation(prior, 'terminated', 'fixture observation'),
                ledger.InstanceObservation(self.start - 10800, 'running', 'fixture observation'),
                ledger.InstanceObservation(self.start, 'terminated', 'fixture observation'),
            ]
        amount = Decimal(8)
        bill = ledger.ChargeRead('2026-01-14', self.start, prior, self.start, amount, Decimal(0), Decimal(0),
            (ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', amount, Decimal(3), 'Hrs'),), False)
        charges = self.state / 'charges'
        charges.mkdir()
        _ = (charges / 'alias.json').write_text(json.dumps(ledger.charge_record(bill)))
        self.costs({'fallback_usd_per_hour': '1', 'monthly_limit_usd': '4'})
        guard = self.book.guard(self.settings, 'admission', self.start, lambda _notice: True, clock=lambda: self.start)
        self.assertIsInstance(guard, ledger.SpendingAllowed)
        self.assertEqual(self.book.accrued_cost(self.settings, self.start, window='month').limit_count, Decimal(3))
        self.controller.settings = replace(self.workload, spot_max_usd_per_hour=Decimal(0),
                                           gp3_usd_per_gib_month=Decimal(0))
        expiry = self.start + self.workload.permit_max_seconds
        refusal = hana_spend.PermitRefused('monthly_limit')
        with self.subTest(path='preview'):
            self.assertEqual(self.controller.preview_start(self.start, expiry), refusal)
        with self.subTest(path='commit'):
            self.assertEqual(self.controller.reserve_start(self.start, expiry), refusal)
        self.assertEqual(self.book.read_snapshot().hana_compute, [])
        self.assertEqual(self.book.read_snapshot().hana_storage, [])
        self.replace('monthly_limit_usd', str(amount.next_plus()))
        self.assertEqual(self.controller.preview_start(self.start, expiry), hana_spend.StartAffordable(expiry))
        self.assertIsInstance(self.controller.reserve_start(self.start, expiry), hana_spend.StartReserved)

    def test_row_73_hana_refuses_when_a_zero_cost_gate_fits(self) -> None:
        start = self.start - 86400
        bill = ledger.ChargeRead('2026-01-14', self.start, start, self.start, Decimal(2), Decimal(0), Decimal(0),
                                 (ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', Decimal(2)),), False)
        with self.book.transaction() as state:
            state.gate_reservations.append(ledger.GateReservation('fixture-completed', start, start + 10800,
                Decimal(1), Decimal(0), ledger.GateTerminated(start + 10800, 'i-fixture'),
                instance_type='m7a.2xlarge', region='us-east-2'))
        _ = self.book.refresh_charges(self.settings, self.start, lambda _start, _end: (bill,))
        self.costs({'monthly_limit_usd': '2.50'})
        self.assertEqual(self.book.accrued_cost(self.settings, self.start, window='month').limit_count, Decimal(3))
        gate = ledger.GateReservation('fixture-zero', self.start, self.start + 3600, Decimal(0), Decimal(0),
                                      instance_type='m7a.8xlarge', region='us-east-2')
        self.assertIsInstance(self.book.reserve_gate(self.settings, gate), ledger.GateReserved)
        self.controller.settings = replace(self.workload, spot_max_usd_per_hour=Decimal(0),
                                           gp3_usd_per_gib_month=Decimal(0))
        expires = self.start + self.workload.permit_max_seconds
        self.assertEqual(self.controller.preview_start(self.start, expires), hana_spend.PermitRefused('monthly_limit'))
        self.assertEqual(self.controller.reserve_start(self.start, expires), hana_spend.PermitRefused('monthly_limit'))
        self.assertEqual(self.book.read_snapshot().hana_compute, [])
        self.assertEqual(self.book.read_snapshot().hana_storage, [])

    def test_rows_74_and_75_keep_storage_reserve_and_hana_budget_operator(self) -> None:
        amount = Decimal(2)
        bill = ledger.ChargeRead('2026-01-14', self.start, self.start - 86400, self.start, amount,
            Decimal(0), Decimal(0), (ledger.UsageTypeCost('USE2-SpotUsage:m7a.4xlarge', amount),), False)
        _ = self.book.refresh_charges(self.settings, self.start, lambda _start, _end: (bill,))
        storage = ledger.HanaStorageExposure(self.start, Decimal(30))
        with self.book.transaction() as state:
            state.hana_storage.append(storage)
        now = self.start + 43200
        page = spend.read_spend(self.settings, self.state, now, '2026-01-15')
        assert isinstance(page, spend.Spend)
        self.assertEqual(tuple((row.day, row.usd) for row in page.days),
                         (('2026-01-15', Decimal('.50')), ('2026-01-14', Decimal(2))))
        self.assert_account_projection(self.book.read_snapshot(), now, Decimal(1), Decimal(3))
        expiry = now + self.workload.permit_max_seconds
        self.controller.settings = replace(self.workload, daily_budget_usd=Decimal('.75'),
                                           spot_max_usd_per_hour=Decimal(0))
        refusal = hana_spend.PermitRefused('hana_daily_budget_usd')
        self.assertEqual(self.controller.preview_start(now, expiry), refusal)
        self.assertEqual(self.controller.reserve_start(now, expiry), refusal)
        self.assertEqual(self.controller.reserve_storage(now), refusal)

        self.controller.settings = replace(self.controller.settings, daily_budget_usd=Decimal(1))
        self.assertEqual(self.controller.preview_start(now, expiry), hana_spend.StartAffordable(expiry))
        self.assertEqual(self.controller.reserve_storage(now), hana_spend.StorageReserved(storage))
        self.costs({'daily_limit_usd': '1', 'monthly_limit_usd': '1'})
        refusal = hana_spend.PermitRefused('daily_limit')
        self.assertEqual(self.controller.preview_start(now, expiry), refusal)
        self.assertEqual(self.controller.reserve_start(now, expiry), refusal)
        self.assertEqual(self.controller.reserve_storage(now), refusal)
        self.assertEqual(self.book.read_snapshot().hana_storage, [storage])
        self.assertEqual(self.book.read_snapshot().hana_compute, [])

    def test_row_76_both_hana_checks_apply_the_proposal_once(self) -> None:
        self.controller.settings = replace(self.workload, root_gib=20, data_gib=10, gp3_usd_per_gib_month=Decimal(1),
                                           spot_max_usd_per_hour=Decimal(1), permit_max_seconds=3600)
        expires = self.start + 3600
        self.assertEqual(tuple(self.book.accrued_cost(self.settings, self.start, window=window).limit_count
                               for window in ('day', 'month')), (Decimal(0), Decimal(0)))
        for period in ('daily', 'monthly'):
            with self.subTest(period=period):
                cap = f'{period}_limit_usd'
                self.replace(cap, '2')
                refusal = hana_spend.PermitRefused(f'{period}_limit')
                self.assertEqual(self.controller.preview_start(self.start, expires), refusal)
                self.assertEqual(self.controller.reserve_start(self.start, expires), refusal)
                self.replace(cap, str(Decimal(2).next_plus()))
        captured: list[ledger.LedgerEvidence] = []
        preview = self.book.placement_spending_preview

        def capture_preview(state: ledger.LedgerEvidence, settings: Path, now: int) -> ledger.SpendingDecision:
            captured.append(state)
            return preview(state, settings, now)

        with mock.patch.object(self.book, 'placement_spending_preview', side_effect=capture_preview):
            self.assertEqual(self.controller.preview_start(self.start, expires), hana_spend.StartAffordable(expires))
        self.assertEqual(self.book.read_snapshot().hana_compute, [])
        self.assertEqual(self.book.read_snapshot().hana_storage, [])
        candidate, = captured
        self.assert_account_projection(candidate, self.start, Decimal(2), Decimal(2))
        reserved = self.controller.reserve_start(self.start, expires)
        assert isinstance(reserved, hana_spend.StartReserved)
        saved = self.book.read_snapshot()
        self.assertEqual(saved.hana_compute, [reserved.exposure])
        self.assertEqual(saved.hana_compute, candidate.hana_compute)
        self.assertEqual(saved.hana_storage, candidate.hana_storage)
        self.assert_account_projection(saved, self.start, Decimal(2), Decimal(2))

    def test_row_77_saved_affordability_can_outlast_the_live_cap(self) -> None:
        now = self.start + 36000
        self.book.observe(ledger.InstanceObservation(now, 'running', 'fixture observation'))
        with self.book.transaction() as state:
            state.hana_storage.append(ledger.HanaStorageExposure(self.start, Decimal(15)))
        workload_path = self.root / 'hana_aws.conf'
        numeric_settings(workload_path, {'spot_max_usd_per_hour': '1'})
        with workload_path.open('a') as handle:
            _ = handle.write('# whole: 1..5\npreview_max_age_s=5\n')
        private = conf.read(workload_path, load=conf.read_bytes)
        lifecycle = self.state / 'hana' / 'lifecycle.json'
        controller = hana_spend.HanaSpending(self.book, self.settings, hana_spend.read_settings(private))
        refusal = hana_spend.PermitRefused('daily_limit')
        for delay, rate, reuse in ((2, '1800', True), (30, '120', False)):
            with self.subTest(delay=delay):
                later = now + delay
                self.costs({'daily_limit_usd': '1.50', 'monthly_limit_usd': '1.50', 'fallback_usd_per_hour': rate,
                            'spot_max_usd_per_hour': rate})
                first_states: list[ledger.LedgerEvidence] = []
                preview = self.book.placement_spending_preview

                def save_preview(state: ledger.LedgerEvidence, settings: Path, at: int) -> ledger.SpendingDecision:
                    first_states.append(state)
                    return preview(state, settings, at)

                times = iter((float(now), float(later)))
                ticks = iter((0.0, float(delay)))
                clock = SimpleNamespace(time=lambda: next(times), monotonic=lambda: next(ticks))
                with (mock.patch.object(hana_spend, 'time', clock),
                      mock.patch.object(hana_spend, '_REPOSITORY_GATE_CONFIG', self.root / 'cloud_gate.conf'),
                      mock.patch.object(self.book, 'placement_spending_preview', side_effect=save_preview)):
                    first = hana_spend.preview_placement_start(self.book, self.settings, private, lifecycle)
                    second = hana_spend.preview_placement_start(self.book, self.settings, private, lifecycle)
                self.assertEqual(first, hana_spend.StartAffordable(now + controller.settings.permit_max_seconds))
                self.assertEqual(len(first_states), 1 if reuse else 2)
                self.assertEqual(second, first if reuse else refusal)
                self.assert_account_projection(first_states[0], now, Decimal(1), Decimal(1))
                with self.decision_book(self.book) as guard_book:
                    first_guard = guard_book.guard(self.settings, 'admission', now, lambda _notice: True,
                                                   clock=lambda: now)
                    self.assertIsInstance(first_guard, ledger.SpendingAllowed)
                    later_guard = guard_book.guard(self.settings, 'admission', later, lambda _notice: True,
                                                   clock=lambda: later)
                    self.assertIsInstance(later_guard, ledger.SpendingRefused)
                    assert isinstance(later_guard, ledger.SpendingRefused)
                    self.assertEqual(later_guard.reason, 'daily_limit')
                checked_states: list[ledger.LedgerEvidence] = []
                live = self.book.spending_decision

                def save_live(state: ledger.LedgerEvidence, settings: Path, at: int) -> ledger.SpendingDecision:
                    checked_states.append(state)
                    return live(state, settings, at)

                with mock.patch.object(self.book, 'spending_decision', side_effect=save_live):
                    self.assertEqual(controller.reserve_start(later, later + controller.settings.permit_max_seconds), refusal)
                candidate, = checked_states
                self.assert_account_projection(candidate, later, Decimal(2), Decimal(2))
                self.assertEqual(self.book.read_snapshot().hana_compute, [])
                for at, compute in ((now, Decimal(0)), (later, Decimal(1))):
                    page = spend.read_spend(self.settings, self.state, at, '2026-01-15')
                    assert isinstance(page, spend.Spend)
                    self.assertEqual(page.days[0].usd, compute + Decimal(15 * (at - self.start)) / Decimal(30 * 86400))
                    self.assertIsNone(spend.month_spend(page.days, datetime.fromtimestamp(at, UTC).date(), 3))

    def test_row_n07_renewal_replaces_the_old_rate_and_extends_the_deadline(self) -> None:
        for day, old_day, old_month, old_total, new_total in (
            ('2026-01-16', '2026-01-15', '2026-01', 3, 8),
            ('2026-02-01', '2026-01-31', '2026-01', 2, 6),
        ):
            with self.subTest(day=day):
                self.book = ledger.CostLedger(self.root / day)
                self.controller = hana_spend.HanaSpending(self.book, self.settings, replace(
                    self.workload, spot_max_usd_per_hour=Decimal(2), gp3_usd_per_gib_month=Decimal(0),
                    daily_budget_usd=Decimal(20), permit_max_seconds=7200))
                midnight = instant(f'{day}T00:00:00Z')
                start, now, expiry = midnight - 3600, midnight + 3600, midnight + 10800
                old = ledger.HanaComputeExposure(start, midnight + 7200, 1, Decimal(1),
                    instance_type=self.workload.instance_type, region=self.workload.region, observed_active=True)
                with self.book.transaction() as state:
                    state.inventories.append(ledger.ResourceInventory(start, (), 'fixture inventory', builder_removed=True))
                    state.observations.append(ledger.InstanceObservation(start, 'terminated', 'fixture account inventory'))
                    state.hana_compute.append(old)
                saved = self.book.read_snapshot()
                self.assert_account_projection(saved, now, Decimal(2), Decimal(old_total))
                page = spend.read_spend(self.settings, self.book.directory, now, day)
                assert isinstance(page, spend.Spend)
                self.assertEqual((page.days[0].day, page.days[0].usd), (day, Decimal(1)))
                self.assertIsNone(spend.month_spend(page.days, datetime.fromtimestamp(now, UTC).date(), 3))
                self.assertEqual(self.controller.preview_start(now, expiry),
                                 hana_spend.PermitRefused('Confirm the previous stop before another start'))
                old_daily, old_monthly = ledger.spending_periods(
                    ledger.hana_intervals(saved, now, 0, reserve=True), ZoneInfo('UTC'))
                self.assertEqual(old_daily, {old_day: Decimal(1), day: Decimal(2)})
                expected_old_months = {old_month: Decimal(3)} if day.startswith(old_month) else {
                    old_month: Decimal(1), day[:7]: Decimal(2)}
                self.assertEqual(old_monthly, expected_old_months)
                self.assertEqual(self.controller.issue(now, old.expires, 'fixture-boot', expected_generation=1),
                                 hana_spend.PermitRefused('The permit expired or its deadline did not increase'))
                self.costs({'daily_limit_usd': '6'})
                self.assertEqual(self.controller.issue(now, expiry, 'fixture-boot', expected_generation=1),
                                 hana_spend.PermitRefused('daily_limit'))
                self.assertEqual(self.book.read_snapshot().hana_compute, [old])
                self.costs({'daily_limit_usd': str(Decimal(6).next_plus()), 'monthly_limit_usd': str(new_total)})
                self.assertEqual(self.controller.issue(now, expiry, 'fixture-boot', expected_generation=1),
                                 hana_spend.PermitRefused('monthly_limit'))
                self.assertEqual(self.book.read_snapshot().hana_compute, [old])
                self.costs({'monthly_limit_usd': str(Decimal(new_total).next_plus())})
                renewed = self.controller.issue(now, expiry, 'fixture-boot', expected_generation=1)
                assert isinstance(renewed, hana_spend.PermitIssued)
                self.assertEqual((renewed.exposure.at, renewed.exposure.expires, renewed.exposure.usd_per_hour),
                                 (start, expiry, Decimal(2)))
                candidate = self.book.read_snapshot()
                self.assertEqual(candidate.hana_compute, [renewed.exposure])
                daily, monthly = ledger.spending_periods(
                    ledger.hana_intervals(candidate, now, 0, reserve=True), ZoneInfo('UTC'))
                self.assertEqual(daily, {old_day: Decimal(2), day: Decimal(6)})
                expected_months = {old_month: Decimal(8)} if day.startswith(old_month) else {
                    old_month: Decimal(2), day[:7]: Decimal(6)}
                self.assertEqual(monthly, expected_months)
                self.assert_account_projection(candidate, now, Decimal(6), Decimal(new_total))
                self.costs({'daily_limit_usd': '35', 'monthly_limit_usd': '400'})

    def test_row_n13_hana_preview_and_commit_refuse_exact_account_equality(self) -> None:
        amount = Decimal('12.00000000000000000000000001')
        self.costs({'limit_reserve_usd': str(amount)})
        self.controller.settings = replace(self.workload, spot_max_usd_per_hour=Decimal(0),
                                           gp3_usd_per_gib_month=Decimal(0))
        expiry = self.start + self.workload.permit_max_seconds
        self.assertEqual(tuple(self.book.accrued_cost(self.settings, self.start, window=window).limit_count
                               for window in ('day', 'month')), (amount, amount))
        self.costs({'daily_limit_usd': str(amount.next_plus()), 'monthly_limit_usd': str(amount.next_plus())})
        self.assertEqual(self.controller.preview_start(self.start, expiry), hana_spend.StartAffordable(expiry))
        for period in ('daily', 'monthly'):
            with self.subTest(period=period):
                self.costs({'daily_limit_usd': '35', 'monthly_limit_usd': '100', f'{period}_limit_usd': str(amount)})
                expected = hana_spend.PermitRefused(f'{period}_limit')
                self.assertEqual(self.controller.preview_start(self.start, expiry), expected)
                self.assertEqual(self.controller.reserve_start(self.start, expiry), expected)
                self.assertEqual(self.book.read_snapshot().hana_compute, [])
                self.assertEqual(self.book.read_snapshot().hana_storage, [])
        self.costs({'daily_limit_usd': str(amount.next_plus()), 'monthly_limit_usd': str(amount.next_plus())})
        self.assertIsInstance(self.controller.reserve_start(self.start, expiry), hana_spend.StartReserved)

    def test_start_reserves_compute_and_storage_before_returning_permit(self) -> None:
        result = self.issued(self.start)
        state = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(state.hana_compute, [result.exposure])
        self.assertEqual(result.permit.generation, str(result.exposure.generation))
        self.assertEqual(result.exposure.usd_per_hour, self.workload.spot_max_usd_per_hour)
        self.assertEqual(result.exposure.expires, self.start + self.workload.permit_max_seconds)
        self.assertEqual(state.hana_storage[0].usd_per_month, self.workload.storage_usd_per_month)

    def test_fallback_types_reject_empty_items_and_duplicates(self) -> None:
        configuration = self.root / 'hana_aws.conf'
        for value in ('c7a.4xlarge,', ',m7i.4xlarge', 'c7a.4xlarge,c7a.4xlarge',
                      f'c7a.4xlarge,{self.workload.instance_type}'):
            with self.subTest(value=value):
                text_setting(configuration, 'fallback_instance_types', value)
                with self.assertRaisesRegex(ValueError, 'distinct types and no empty items'):
                    _ = hana_spend.read_settings(conf.read(configuration))

    def test_hana_budget_refuses_start_and_saves_no_exposure(self) -> None:
        self.controller.settings = replace(self.workload, daily_budget_usd=self.workload.storage_usd_per_month / 60)
        self.assertEqual(self.issue(self.start), hana_spend.PermitRefused('hana_daily_budget_usd'))
        self.assertEqual(self.book.read_snapshot().hana_compute, [])
        self.assertEqual(self.book.read_snapshot().hana_storage, [])

    def test_gate_spend_and_hana_share_the_unchanged_account_limit(self) -> None:
        account = config.read_settings(self.settings)
        self.assertEqual(account.daily_limit_usd, Decimal('35'))
        storage = self.workload.storage_usd_per_month / 30
        gate = ledger.GateReservation('private-gate', self.start, self.start + 3600,
            account.daily_limit_usd - storage / 2, Decimal(0), instance_type=account.instance_type, region=self.workload.region)
        self.assertIsInstance(self.book.reserve_gate(self.settings, gate), ledger.GateReserved)
        self.assertEqual(self.issue(self.start), hana_spend.PermitRefused('daily_limit'))
        self.assertEqual(self.book.read_snapshot().hana_compute, [])
        self.assertEqual(config.read_settings(self.settings).daily_limit_usd, account.daily_limit_usd)

    def test_check_start_refuses_after_a_gate_launch_appears_during_pricing(self) -> None:
        self.costs({'daily_limit_usd': '5', 'limit_reserve_usd': '0'})
        day = self.start - 86400
        bill = ledger.ChargeRead(datetime.fromtimestamp(day, UTC).date().isoformat(), self.start,
            day, self.start, Decimal(10), Decimal(0), Decimal(0),
            (ledger.UsageTypeCost('USE2-SpotUsage:m7a.8xlarge', Decimal(10), Decimal(1), 'Hrs'),))
        charges = self.state / 'charges'
        charges.mkdir()
        _ = (charges / f'{bill.day}.json').write_text(json.dumps(ledger.charge_record(bill)))
        gate = ledger.GateReservation('private-gate', self.start, self.start + 3600,
            Decimal(1), Decimal(0), instance_type='m7a.4xlarge', region='us-east-2')
        self.assertIsInstance(self.book.reserve_gate(self.settings, gate), ledger.GateReserved)
        generation = self.issued(self.start).exposure.generation
        self.assertIsInstance(self.controller.check_start(self.start, generation), hana_spend.StartReserved)
        history = self.state / 'cloud-gate-launches.jsonl'
        launches = self.book.launches

        def read_launches() -> tuple[dict[str, object], ...]:
            rows = launches()
            if not rows:
                _ = history.write_text(json.dumps({'attempt': gate.attempt, 'time': self.start,
                    'instance_type': 'm7a.8xlarge', 'result': 'launched'}) + '\n')
            return rows

        with mock.patch.object(self.book, 'launches', side_effect=read_launches) as pricing_reads:
            self.assertEqual(self.controller.check_start(self.start, generation), hana_spend.PermitRefused('daily_limit'))
        self.assertEqual(pricing_reads.call_count, 2)

    def test_renewal_extends_exposure_then_refusal_preserves_the_old_deadline(self) -> None:
        first = self.issued(self.start)
        renewed = self.issued(self.start + 1, generation=first.exposure.generation)
        self.assertGreater(renewed.exposure.generation, first.exposure.generation)
        self.assertEqual(renewed.exposure.at, first.exposure.at)
        self.assertGreater(renewed.exposure.expires, first.exposure.expires)
        self.controller.settings = replace(self.workload, daily_budget_usd=self.workload.storage_usd_per_month / 60)
        result = self.issue(self.start + 2, generation=renewed.exposure.generation)
        self.assertEqual(result, hana_spend.PermitRefused('hana_daily_budget_usd'))
        self.assertEqual(self.book.read_snapshot().hana_compute, [renewed.exposure])

    def test_renewal_retains_the_original_machine_type_and_region(self) -> None:
        first = self.issued(self.start)
        self.controller.settings = replace(self.workload, region='us-west-2', instance_type='fixture-other-type')
        renewed = self.issued(self.start + 1, generation=first.exposure.generation)
        self.assertEqual(first.exposure.region, self.workload.region)
        self.assertEqual(renewed.exposure.region, first.exposure.region)
        self.assertEqual(renewed.exposure.instance_type, first.exposure.instance_type)

    def test_observed_hana_keeps_its_exposure_after_renewal_and_refusal(self) -> None:
        first = self.issued(self.start)
        self.controller.reconcile(self.start, 'running', generation=first.exposure.generation)
        renewed = self.issued(self.start + 1, generation=first.exposure.generation)
        journal = self.state / 'hana' / 'provider'
        journal.mkdir(parents=True)
        _ = (journal / 'renewed-start.json').write_text(json.dumps({
            'action': 'start', 'generation': renewed.exposure.generation, 'dispatched': True,
            'start_refusal_code': 'Blocked',
        }))
        stop = self.start + 120
        self.controller.reconcile(stop, 'stopped', generation=renewed.exposure.generation)
        state = self.book.read_snapshot()
        interval, = (row for row in ledger.hana_intervals(state, stop, 60) if row.kind == 'instance')
        self.assertEqual((interval.start, interval.end), (self.start, stop))

    def test_unconfirmed_stop_retains_full_exposure_after_expiry(self) -> None:
        issued = self.issued(self.start)
        now = issued.exposure.expires + 100
        self.controller.reconcile(now, 'unknown', generation=issued.exposure.generation)
        state = self.book.read_snapshot()
        self.assertIsInstance(state.hana_compute[0].state, ledger.HanaExposureOpen)
        intervals = ledger.hana_intervals(state, now, 0, reserve=True)
        compute = sum((row.usd for row in intervals if row.kind == 'instance'), Decimal(0))
        self.assertEqual(compute, self.workload.spot_max_usd_per_hour * Decimal(now - self.start) / 3600)
        self.assertIsInstance(self.issue(now), hana_spend.PermitRefused)

    def test_confirmed_stop_releases_compute_but_keeps_storage(self) -> None:
        issued = self.issued(self.start)
        now = self.start + 10
        self.controller.reconcile(now, 'stopped', generation=issued.exposure.generation)
        state = self.book.read_snapshot()
        self.assertEqual(state.hana_compute[0].state, ledger.HanaExposureClosed(now))
        self.assertLess(now, issued.exposure.expires)
        self.assertIsInstance(state.hana_storage[0].state, ledger.HanaExposureOpen)
        later = now + 86400
        rows = ledger.hana_intervals(state, later, 0, reserve=True)
        self.assertEqual(max(row.end for row in rows if row.kind == 'instance'), now)
        self.assertGreater(max(row.end for row in rows if row.kind == 'fixed'), later)

    def test_automatic_restart_without_a_caller_reserves_guard_exposure(self) -> None:
        first = self.issued(self.start)
        self.controller.reconcile(self.start + 10, 'stopped', generation=first.exposure.generation)
        observer = hana_spend.HanaSpending(ledger.CostLedger(self.state), self.settings, replace(self.workload, enabled=False))
        observer.reconcile(self.start + 20, 'running', generation=first.exposure.generation)
        generation = self.book.read_snapshot().hana_compute[-1].generation
        observer.reconcile(self.start + 21, 'running', generation=generation)
        state = self.book.read_snapshot()
        self.assertEqual(len(state.hana_compute), 2)
        restarted = state.hana_compute[-1]
        self.assertGreater(restarted.generation, first.exposure.generation)
        self.assertEqual(restarted.expires, self.start + 20 + self.workload.guard_grace_seconds)
        self.assertIsInstance(restarted.state, ledger.HanaExposureOpen)
        self.assertEqual(len(state.hana_storage), 1)
        observer.reconcile(self.start + 22, 'stopped', generation=restarted.generation)
        self.assertEqual(self.book.read_snapshot().hana_compute[-1].state, ledger.HanaExposureClosed(self.start + 22))

    def test_old_generation_cannot_renew_or_replace_a_new_generation(self) -> None:
        first = self.issued(self.start)
        renewed = self.issued(self.start + 1, generation=first.exposure.generation)
        self.assertEqual(self.issue(self.start + 2, generation=first.exposure.generation),
                         hana_spend.PermitRefused('The permit generation is old'))
        self.assertEqual(self.book.read_snapshot().hana_compute, [renewed.exposure])

    def test_old_records_read_without_inventing_hana_values(self) -> None:
        document = ledger.mapping(cast(object, json.loads(self.book.path.read_text())))
        del document['hana_compute']
        del document['hana_storage']
        _ = self.book.path.write_text(json.dumps(document))
        state = self.book.read_snapshot()
        self.assertEqual(state.hana_compute, [])
        self.assertEqual(state.hana_storage, [])
        _ = self.issued(self.start)
        document = ledger.mapping(cast(object, json.loads(self.book.path.read_text())))
        records = ledger.rows(document, 'hana_compute')
        del records[0]['usd_per_hour']
        _ = self.book.path.write_text(json.dumps(document))
        with self.assertRaises(ledger.LedgerUnavailable):
            _ = self.book.read_snapshot()

    def test_gates_also_count_hana_reservations(self) -> None:
        _ = self.issued(self.start)
        account = config.read_settings(self.settings)
        proposed = ledger.GateReservation('private-gate', self.start, self.start + 3600,
            account.daily_limit_usd - self.workload.storage_usd_per_month / 60, Decimal(0),
            instance_type=account.instance_type, region=self.workload.region)
        self.assertEqual(self.book.reserve_gate(self.settings, proposed), ledger.GateCapReached('daily_limit'))

    def test_account_guard_counts_hana_exposure(self) -> None:
        _ = self.issued(self.start)
        self.costs({'daily_limit_usd': str(self.workload.storage_usd_per_month / 60)})
        result = self.book.guard(self.settings, 'admission', self.start, lambda _event: True)
        self.assertIsInstance(result, ledger.SpendingRefused)

    def test_concurrent_starts_share_one_reservation(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(self.issue, (self.start, self.start)))
        self.assertEqual(sum(isinstance(row, hana_spend.PermitIssued) for row in results), 1)
        self.assertEqual(len(self.book.read_snapshot().hana_compute), 1)

    def test_storage_reservation_precedes_provisioning_and_survives_termination(self) -> None:
        result = self.controller.reserve_storage(self.start)
        self.assertIsInstance(result, hana_spend.StorageReserved)
        first = self.issued(self.start)
        self.controller.reconcile(self.start + 10, 'terminated', generation=first.exposure.generation)
        self.assertIsInstance(self.book.read_snapshot().hana_storage[0].state, ledger.HanaExposureOpen)
        self.controller.confirm_storage_deleted(self.start + 11)
        self.assertEqual(self.book.read_snapshot().hana_storage[0].state, ledger.HanaExposureClosed(self.start + 11))

    def test_invalid_deadline_disabled_and_expired_renewal_refuse(self) -> None:
        self.assertIsInstance(self.controller.issue(self.start, self.start + self.workload.permit_max_seconds + 1, 'boot'),
                              hana_spend.PermitRefused)
        first = self.issued(self.start)
        self.assertIsInstance(self.issue(first.exposure.expires, generation=first.exposure.generation), hana_spend.PermitRefused)
        self.controller.settings = replace(self.workload, enabled=False)
        self.assertEqual(self.issue(self.start + 1), hana_spend.PermitRefused('Hana is disabled'))

    def test_account_policy_failure_refuses_without_new_exposure(self) -> None:
        self.replace('daily_limit_usd', 'broken')
        self.assertIsInstance(self.issue(self.start), hana_spend.PermitRefused)
        self.assertEqual(self.book.read_snapshot().hana_compute, [])

    def test_midnight_reserves_each_day_and_storage_once(self) -> None:
        now = self.start + 86400 - self.workload.permit_max_seconds // 2
        _ = self.issued(now)
        rows = ledger.hana_intervals(self.book.read_snapshot(), now, 0, reserve=True)
        daily, _monthly = ledger.spending_periods(rows, ZoneInfo('UTC'))
        self.assertEqual(len(daily), 2)
        self.assertTrue(all(usd > 0 for usd in daily.values()))
        storage = sum((row.usd for row in rows if row.kind == 'fixed'), Decimal(0))
        seconds = 86400 + self.workload.permit_max_seconds // 2
        self.assertEqual(storage, self.workload.storage_usd_per_month * Decimal(seconds) / Decimal(30 * 86400))

    def test_newest_account_bill_can_refuse_a_start(self) -> None:
        account = config.read_settings(self.settings)
        amount = account.daily_limit_usd - self.workload.storage_usd_per_month / 60
        prior = self.start - 86400
        reading = ledger.ChargeRead('2026-01-14', self.start, prior, self.start, amount, Decimal(0), Decimal(0),
            (ledger.UsageTypeCost('USE2-EBS:VolumeUsage.gp3', amount),), False)
        _ = self.book.refresh_charges(self.settings, self.start, lambda _start, _end: (reading,))
        self.assertEqual(self.issue(self.start), hana_spend.PermitRefused('daily_limit'))
        self.assertEqual(self.book.read_snapshot().hana_compute, [])

    def test_account_refuses_a_renewal_without_changing_its_deadline(self) -> None:
        first = self.issued(self.start)
        self.costs({'daily_limit_usd': str(self.workload.storage_usd_per_month / 60)})
        self.assertEqual(self.issue(self.start + 1, generation=first.exposure.generation),
                         hana_spend.PermitRefused('daily_limit'))
        self.assertEqual(self.book.read_snapshot().hana_compute, [first.exposure])

    def test_report_estimate_includes_accrued_hana_cost(self) -> None:
        _ = self.issued(self.start)
        now = self.start + self.workload.permit_max_seconds
        estimate = ledger.costs_from_evidence(self.book.read_snapshot(), config.read_settings(self.settings), now)
        expected = (self.workload.spot_max_usd_per_hour * Decimal(now - self.start) / 3600
                    + self.workload.storage_usd_per_month * Decimal(now - self.start) / Decimal(30 * 86400))
        self.assertEqual(estimate.daily.usd, expected)

    def test_new_start_after_stop_uses_a_later_generation(self) -> None:
        first = self.issued(self.start)
        self.controller.reconcile(self.start + 10, 'stopped', generation=first.exposure.generation)
        second = self.issued(self.start + 20)
        self.assertGreater(second.exposure.generation, first.exposure.generation)
        self.assertEqual(len(self.book.read_snapshot().hana_storage), 1)

    def test_old_generation_stop_cannot_release_a_budget_refused_renewal(self) -> None:
        first = self.issued(self.start)
        renewed = self.issued(self.start + 10, generation=first.exposure.generation)
        budget = (self.workload.storage_usd_per_month / 30 + self.workload.spot_max_usd_per_hour
                  * Decimal(self.workload.permit_max_seconds + 15) / 3600)
        self.controller.settings = replace(self.workload, daily_budget_usd=budget)
        refused = self.issue(self.start + 20, generation=renewed.exposure.generation)
        self.assertEqual(refused, hana_spend.PermitRefused('hana_daily_budget_usd'))
        before = self.book.read_snapshot()
        self.controller.reconcile(self.start + 5, 'stopped', generation=first.exposure.generation)
        self.assertEqual(self.book.read_snapshot(), before)
        self.assertEqual(self.issue(self.start + 20, generation=renewed.exposure.generation), refused)
        self.controller.reconcile(self.start + 30, 'terminated', generation=first.exposure.generation)
        self.assertEqual(self.book.read_snapshot(), before)

    def test_later_running_observation_fences_delayed_stop_after_controller_restart(self) -> None:
        first = self.issued(self.start)
        self.controller.reconcile(self.start + 30, 'running', generation=first.exposure.generation)
        before = self.book.read_snapshot()
        self.assertEqual(before.hana_lifecycle_fence, ledger.HanaLifecycleFence(self.start + 30, first.exposure.generation))
        restarted = hana_spend.HanaSpending(ledger.CostLedger(self.state), self.settings, self.workload)
        restarted.reconcile(self.start + 20, 'stopped', generation=first.exposure.generation)
        self.assertEqual(self.book.read_snapshot(), before)
        restarted.reconcile(self.start + 40, 'stopped', generation=first.exposure.generation)
        self.assertEqual(self.book.read_snapshot().hana_compute[0].state, ledger.HanaExposureClosed(self.start + 40))
        stopped = self.book.read_snapshot()
        restarted.reconcile(self.start + 35, 'running', generation=first.exposure.generation)
        self.assertEqual(self.book.read_snapshot(), stopped)

    def test_stop_before_current_permit_issue_time_keeps_exposure_open(self) -> None:
        first = self.issued(self.start)
        renewed = self.issued(self.start + 10, generation=first.exposure.generation)
        before = self.book.read_snapshot()
        self.controller.reconcile(self.start + 5, 'stopped', generation=renewed.exposure.generation)
        self.assertEqual(self.book.read_snapshot(), before)
        self.controller.reconcile(self.start + 10, 'stopped', generation=renewed.exposure.generation)
        self.assertEqual(self.book.read_snapshot(), before)

    def test_saved_storage_rechecks_account_caps_before_provisioning(self) -> None:
        reserved = self.controller.reserve_storage(self.start)
        self.assertIsInstance(reserved, hana_spend.StorageReserved)
        self.costs({'daily_limit_usd': str(self.workload.storage_usd_per_month / 60)})
        shared = self.book.guard(self.settings, 'start', self.start + 1, lambda _event: True)
        self.assertIsInstance(shared, ledger.SpendingRefused)
        assert isinstance(shared, ledger.SpendingRefused)
        restarted = hana_spend.HanaSpending(ledger.CostLedger(self.state), self.settings, self.workload)
        result = restarted.reserve_storage(self.start + 1)
        self.assertEqual(result, hana_spend.PermitRefused(shared.reason))
        self.assertEqual(result, restarted.issue(self.start + 1, self.start + 1 + self.workload.permit_max_seconds, 'boot'))
        self.assertEqual(len(self.book.read_snapshot().hana_storage), 1)
        self.assertEqual(self.book.read_snapshot().hana_compute, [])

    def test_saved_storage_rechecks_hana_budget_before_provisioning(self) -> None:
        reserved = self.controller.reserve_storage(self.start)
        self.assertIsInstance(reserved, hana_spend.StorageReserved)
        self.controller.settings = replace(self.workload, daily_budget_usd=self.workload.storage_usd_per_month / 60)
        self.assertEqual(self.controller.reserve_storage(self.start + 1), hana_spend.PermitRefused('hana_daily_budget_usd'))
        self.controller.settings = self.workload
        self.assertEqual(self.controller.reserve_storage(self.start + 2), reserved)

    def test_missing_lifecycle_fence_stays_absent_until_a_fresh_observation(self) -> None:
        first = self.issued(self.start)
        document = ledger.mapping(cast(object, json.loads(self.book.path.read_text())))
        del document['hana_lifecycle_fence']
        _ = self.book.path.write_text(json.dumps(document))
        before = self.book.read_snapshot()
        self.assertIsInstance(before.hana_lifecycle_fence, ledger.HanaLifecycleUnfenced)
        self.controller.reconcile(self.start + 1, 'stopped', generation=first.exposure.generation)
        self.assertEqual(self.book.read_snapshot().hana_compute, before.hana_compute)
        self.assertEqual(self.book.read_snapshot().hana_lifecycle_fence,
                         ledger.HanaLifecycleFence(self.start + 1, first.exposure.generation))
        self.controller.reconcile(self.start + 2, 'running', generation=first.exposure.generation)
        self.controller.reconcile(self.start + 3, 'stopped', generation=first.exposure.generation)
        self.assertEqual(self.book.read_snapshot().hana_compute[0].state, ledger.HanaExposureClosed(self.start + 3))

    def test_termination_without_running_observation_establishes_fence_then_allows_restart(self) -> None:
        first = self.issued(self.start)
        document = ledger.mapping(cast(object, json.loads(self.book.path.read_text())))
        del document['hana_lifecycle_fence']
        _ = self.book.path.write_text(json.dumps(document))
        self.controller.reconcile(self.start + 10, 'terminated', generation=first.exposure.generation)
        state = self.book.read_snapshot()
        self.assertIsInstance(state.hana_compute[0].state, ledger.HanaExposureOpen)
        self.assertEqual(state.hana_lifecycle_fence, ledger.HanaLifecycleFence(self.start + 10, first.exposure.generation))
        self.assertIsInstance(self.issue(self.start + 11), hana_spend.PermitRefused)
        restarted = hana_spend.HanaSpending(ledger.CostLedger(self.state), self.settings, self.workload)
        restarted.reconcile(self.start + 9, 'terminated', generation=first.exposure.generation)
        restarted.reconcile(self.start + 10, 'terminated', generation=first.exposure.generation)
        restarted.reconcile(self.start + 20, 'terminated', generation=first.exposure.generation - 1)
        self.assertEqual(self.book.read_snapshot(), state)
        restarted.reconcile(self.start + 12, 'terminated', generation=first.exposure.generation)
        self.assertEqual(self.book.read_snapshot().hana_compute[0].state, ledger.HanaExposureClosed(self.start + 12))
        second = self.issued(self.start + 13)
        self.assertGreater(second.exposure.generation, first.exposure.generation)

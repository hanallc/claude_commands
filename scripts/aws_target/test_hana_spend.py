"""Check Hana permits against shared account exposure with private files."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import json
import subprocess
from typing import cast, override
from unittest import mock
from zoneinfo import ZoneInfo

from scripts.aws_target import config, hana_spend, ledger
from scripts.aws_target.ledger_test_support import CostFixture, git_only, install_hana_spending_settings, instant, text_setting
from scripts.lib import conf


class HanaSpendTests(CostFixture):
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

    def test_start_reserves_compute_and_storage_before_returning_permit(self) -> None:
        result = self.issued(self.start)
        state = ledger.CostLedger(self.state).read_snapshot()
        self.assertEqual(state.hana_compute, [result.exposure])
        self.assertEqual(result.permit.generation, str(result.exposure.generation))
        self.assertEqual(result.exposure.usd_per_hour, self.workload.spot_max_usd_per_hour)
        self.assertEqual(result.exposure.expires, self.start + self.workload.permit_max_seconds)
        self.assertEqual(state.hana_storage[0].usd_per_month, self.workload.storage_usd_per_month)

    def test_demand_start_accrues_only_after_acceptance(self) -> None:
        reserved = self.controller.reserve_start(self.start, self.start + self.workload.permit_max_seconds)
        assert isinstance(reserved, hana_spend.StartReserved)
        now = self.start + 60
        pending = self.book.read_snapshot()
        self.assertFalse(any(row.kind == 'instance' for row in ledger.hana_intervals(pending, now, 60)))
        self.assertTrue(any(row.kind == 'instance' for row in ledger.hana_intervals(pending, now, 60, reserve=True)))
        self.controller.reconcile(now, 'running', generation=reserved.exposure.generation)
        self.assertTrue(any(row.kind == 'instance' for row in ledger.hana_intervals(self.book.read_snapshot(), now, 60)))

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

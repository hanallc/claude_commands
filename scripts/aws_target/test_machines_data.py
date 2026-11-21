"""Check the Machines projection with private launch, cost, and price records."""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
from typing import override
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate, ledger, machines_data, run_cost
from scripts.aws_target.aws_cli import AwsCli
from scripts.build_queue import snapshot_codec
from scripts.build_queue.admission_jobs import StepJob
from scripts.lib import build_steps, conf

SMALL = ledger.GateRateCandidate('c7a.4xlarge', 'fixture-home')
FAST = ledger.GateRateCandidate('c7a.8xlarge', 'fixture-fast')
FASTER = ledger.GateRateCandidate('m7a.8xlarge', 'fixture-faster')
WEEK_START = int(datetime(2026, 11, 13, tzinfo=UTC).timestamp())
NOW = int(datetime(2026, 11, 20, 12, tzinfo=UTC).timestamp())
READ_AT = NOW - 3600


class MachinesDataTests(unittest.TestCase):
    directory: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    account_path: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    configuration: machines_data.MachinesConfiguration  # pyright: ignore[reportUninitializedInstanceVariable]
    book: ledger.CostLedger  # pyright: ignore[reportUninitializedInstanceVariable]
    spot: tuple[ledger.RegionalSpotPrice, ...]  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        page_path = self.directory / 'aws_page.conf'
        _ = page_path.write_text("""# whole: 1..
machines.window_days=7
# whole: 1..
machines.small_sample_checks=2
# range: 1..10
machines.ratio_max=1.75
# range: 0.01..1
machines.ratio_step=0.25
""")
        gate_path = self.directory / 'cloud_gate.conf'
        _ = gate_path.write_text("""instance_types=c7a.4xlarge c7a.8xlarge m7a.8xlarge
# whole: 1..
spot_quota_request_vcpus=32
# whole: 1..
max_gate_minutes=20
# whole: 1..
lifetime_minutes=20
# whole: 0..
capacity_cooldown_seconds=60
remote_region_transfer_usd=0
region_transfer_usd={}
gate_wait_usd_per_minute=0
region_extra_gate_seconds={}
unmeasured_region_extra_seconds={}
# range: 1..10
faster_box_max_cost_ratio=1.50
""")
        self.account_path = self.directory / 'aws_target.conf'
        _ = self.account_path.write_text('region=fixture-home\n# whole: 1..\nprice_max_age_seconds=7200\n')
        _ = (self.directory / 'build_steps.conf').write_text("""[names]
names.merge-gate=fixture gate
names.targeted-test=fixture targeted
names.test-affected=fixture affected
names.integrate=fixture integration
names.test-modules=fixture named
names.test-all=fixture all
""")
        with mock.patch.dict('os.environ', {'CONF_DIR': str(self.directory)}):
            names = build_steps.read_step_names()
        self.configuration = machines_data.MachinesConfiguration(
            conf.read(page_path), conf.read(gate_path), conf.read(self.account_path), names)
        self.write_deployment(SMALL.region, FAST.region, FASTER.region)
        self.book = ledger.CostLedger(self.directory)
        self.spot = tuple(ledger.RegionalSpotPrice(machine.region, 'zone-a', machine.instance_type, Decimal(price), READ_AT)
                          for machine, price in ((SMALL, '0.36'), (FAST, '0.60'), (FASTER, '1.08')))
        self.write_prices()
        rows: list[dict[str, object]] = []
        with self.book.transaction() as state:
            for index, (machine, offset, seconds, mode) in enumerate((
                    (SMALL, 0, 600, 'merge-gate'), (FAST, 100, 450, 'merge-gate'),
                    (FASTER, 900, 300, 'merge-gate'), (SMALL, 1500, 600, 'targeted-test'),
                    (SMALL, 2500, 600, 'test-affected'))):
                at = WEEK_START + offset
                finish = at + seconds
                attempt = f'fixture-{index}'
                state.gate_reservations.append(ledger.GateReservation(
                    attempt, at, finish + 1000, Decimal(1), Decimal(0),
                    state=ledger.GateTerminated(finish, f'i-fixture-{index}'),
                    instance_type=machine.instance_type, region=machine.region, mode=mode))
                row: dict[str, object] = {'attempt': attempt, 'mode': mode,
                    'region': machine.region, 'instance_type': machine.instance_type}
                rows.extend(({**row, 'time': at, 'result': 'launched'},
                             {**row, 'time': finish, 'result': 'finished', 'gate_s': seconds - 10}))
        rows.append({'attempt': 'fixture-refusal', 'mode': 'merge-gate', 'region': FAST.region,
                     'instance_type': FAST.instance_type, 'time': WEEK_START + 90, 'result': 'refused',
                     'error_code': 'InsufficientInstanceCapacity'})
        _ = self.book.launch_history_path.write_text(''.join(json.dumps(row) + '\n'
            for row in sorted(rows, key=lambda row: float(str(row['time'])))))

    def write_deployment(self, *deployed_regions: str) -> None:
        regions = [{'region': region, 'subnets': [{'id': 'subnet-fixture', 'zone': 'zone-a'}]}
                   for region in deployed_regions]
        _ = (self.directory / 'gate-deployment.json').write_text(json.dumps(
            {'regions': regions, 'instance_types': [machine.instance_type for machine in (SMALL, FAST, FASTER)]}))

    def write_prices(self, *, closed: bool = False) -> None:
        prices = [{'instance_type': row.instance_type, 'region': row.region, 'zone': row.zone,
                   'usd_per_hour': str(row.usd_per_hour), 'read_at': row.read_at} for row in self.spot]
        regions = [{'region': machine.region, 'standard_vcpus': '64', 'gpu_vcpus': '0', 'read_at': READ_AT}
                   for machine in (SMALL, FASTER)]
        failures = [{'region': row.region, 'at': READ_AT + 1, 'read': 'price', 'error': 'AuthFailure',
                     'reason': 'The fixture credential is unavailable'} for row in self.spot] if closed else []
        _ = (self.directory / 'spot-prices.json').write_text(json.dumps(
            {'prices': prices, 'regions': regions, 'failures': failures}))

    def read(self) -> machines_data.MachinesPageData:
        return machines_data.read_machines_data(self.directory, NOW, self.configuration)

    def merge(self) -> machines_data.MeasuredWorkload:
        workload = self.read().workloads[0]
        assert isinstance(workload, machines_data.MeasuredWorkload)
        return workload

    def test_fixture_week_uses_ledger_time_quota_and_refusals(self) -> None:
        result = self.read()
        self.assertEqual(result.window_start.timestamp(), WEEK_START)
        self.assertEqual(result.window_end.day, 20)
        self.assertEqual(result.price_read, machines_data.SavedSpotPriceRead(READ_AT, False))
        merge = result.workloads[0]
        assert isinstance(merge, machines_data.MeasuredWorkload)
        self.assertEqual((merge.name, merge.checks, merge.checks_per_month), ('fixture gate', 3, Decimal(13)))
        self.assertEqual(merge.concurrent_starts, ((1, 2), (2, 1)))
        self.assertEqual(merge.live_ratio, Decimal('1.50'))
        fast = next(row for row in merge.choices if row.machine == FAST)
        self.assertEqual((fast.median_seconds, fast.usd_per_hour, fast.usd_per_check),
                         (Decimal(450), Decimal('.60'), Decimal('.075')))
        self.assertEqual((fast.checks, fast.launched, fast.capacity_refusals), (1, 1, 1))
        self.assertTrue(fast.small_sample)
        self.assertTrue(fast.quota_assumed)
        self.assertAlmostEqual(fast.free_share, Decimal(1) / 3)
        self.assertTrue(any('32 vCPUs' in assumption for assumption in merge.assumptions))
        self.assertIn('The measured run rate, overlap, and share of Spot refusals repeat for fixture gate.',
                      merge.assumptions)

    def test_ratios_add_each_choice_at_its_first_slider_value(self) -> None:
        off, first, second = self.merge().steps
        self.assertEqual((off.first_ratio, off.last_ratio, off.added, off.extra_usd_per_month),
                         (Decimal('1.00'), Decimal('1.00'), (), Decimal(0)))
        self.assertIsInstance(off.usd_per_hour_saved, machines_data.NoHoursSaved)
        self.assertEqual((first.first_ratio, first.last_ratio, first.added),
                         (Decimal('1.25'), Decimal('1.25'), (FAST,)))
        self.assertEqual((second.first_ratio, second.last_ratio, second.added),
                         (Decimal('1.50'), Decimal('1.75'), (FASTER,)))
        self.assertEqual(tuple(point.ceiling_extra_usd_per_month for point in second.limits),
                         (Decimal('.39'), Decimal('.585')))
        self.assertAlmostEqual(first.extra_usd_per_month, Decimal('.065'))
        self.assertAlmostEqual(first.hours_saved_per_month, Decimal(13 * 150) / 3 / 3600)
        self.assertEqual(tuple(row.machine for row in second.shares), (FAST, FASTER, SMALL))

    def test_one_size_workloads_report_the_recorded_count(self) -> None:
        workloads = {workload.mode: workload for workload in self.read().workloads}
        for mode, name in (('targeted-test', 'fixture targeted'), ('test-affected', 'fixture affected')):
            workload = workloads[mode]
            assert isinstance(workload, machines_data.WorkloadWithoutComparison)
            self.assertEqual(workload.name, name)
            self.assertEqual(workload.sentence,
                             f'The records for {name} hold 1 completed run on 1 machine size in this period.')

    def test_total_uses_every_measured_job_and_every_step_start(self) -> None:
        data = self.read()
        gate = data.workloads[0]
        assert isinstance(gate, machines_data.MeasuredWorkload)
        zero, last = Decimal(0), Decimal('1.75')
        limits = tuple(machines_data.MachineLimit(limit.ratio, (limit.ratio - 1) * Decimal('.40'))
                       for step in gate.steps for limit in step.limits)
        extra, hours, live = Decimal('.25'), Decimal('.50'), Decimal('1.76')
        targeted = replace(gate, name='fixture targeted', mode='targeted-test', live_ratio=live,
            live_extra_usd_per_month=extra, steps=(
                replace(gate.steps[0], last_ratio=Decimal('1.50'), limits=limits[:-1]),
                machines_data.MachineStep(last, last, (FAST,), (), extra, hours, extra / hours, limits[-1:]),
            ))
        gate = replace(gate, live_ratio=live)
        total = replace(data, workloads=(gate, targeted, data.workloads[2])).total
        assert isinstance(total, machines_data.AllJobsProjection)
        self.assertEqual(tuple((step.first_ratio, step.last_ratio) for step in total.steps),
                         tuple((limit.ratio, limit.ratio) for limit in limits))
        self.assertEqual(total.steps[2].added, (machines_data.JobMachineAddition(gate.name, FASTER),))
        self.assertEqual(total.steps[3].added, (machines_data.JobMachineAddition(targeted.name, FAST),))
        self.assertIsInstance(total.steps[0].usd_per_hour_saved, machines_data.NoHoursSaved)
        for index, (gate_step, job_extra, job_hours) in enumerate((
                (gate.steps[1], zero, zero), (gate.steps[2], zero, zero), (gate.steps[2], extra, hours)), start=1):
            with self.subTest(ratio=limits[index].ratio):
                step = total.steps[index]
                expected_extra = gate_step.extra_usd_per_month + job_extra
                expected_hours = gate_step.hours_saved_per_month + job_hours
                gate_limit = next(limit for limit in gate_step.limits if limit.ratio == limits[index].ratio)
                self.assertEqual(step.extra_usd_per_month, expected_extra)
                self.assertEqual(step.hours_saved_per_month, expected_hours)
                self.assertEqual(step.usd_per_hour_saved, expected_extra / expected_hours)
                self.assertEqual(step.limits[0].ceiling_extra_usd_per_month,
                                 gate_limit.ceiling_extra_usd_per_month + limits[index].ceiling_extra_usd_per_month)
        self.assertEqual(total.live_ratio, live)
        self.assertEqual(total.live_extra_usd_per_month, gate.live_extra_usd_per_month + extra)

    def test_one_measured_job_gives_the_same_total_as_that_job(self) -> None:
        data = self.read()
        gate = data.workloads[0]
        assert isinstance(gate, machines_data.MeasuredWorkload)
        total = data.total
        assert isinstance(total, machines_data.AllJobsProjection)
        for step, gate_step in zip(total.steps, gate.steps, strict=True):
            self.assertEqual((step.first_ratio, step.last_ratio, step.limits),
                             (gate_step.first_ratio, gate_step.last_ratio, gate_step.limits))
            self.assertEqual((step.extra_usd_per_month, step.hours_saved_per_month, step.usd_per_hour_saved),
                             (gate_step.extra_usd_per_month, gate_step.hours_saved_per_month, gate_step.usd_per_hour_saved))
        self.assertEqual(total.live_extra_usd_per_month, gate.live_extra_usd_per_month)

    def test_recorded_steps_take_precedence_over_modes(self) -> None:
        jobs = {'fixture-1': StepJob('integrate'), 'fixture-2': StepJob('test-modules', modules=1),
                'fixture-3': StepJob('test-all')}
        rows = self.book.launches()
        for row in rows:
            if (attempt := str(row['attempt'])) in jobs:
                row['gate_job'] = snapshot_codec.snapshot_value(jobs[attempt])
            elif attempt == 'fixture-0':
                row['mode'] = 'gate'
        _ = self.book.launch_history_path.write_text(''.join(json.dumps(row) + '\n' for row in rows))

        workloads = {workload.mode: workload for workload in self.read().workloads}
        for mode, name, machine, cost in (
                ('merge-gate', 'fixture gate', SMALL, '.06'), ('integrate', 'fixture integration', FAST, '.075'),
                ('test-modules', 'fixture named', FASTER, '.09'), ('test-all', 'fixture all', SMALL, '.06')):
            with self.subTest(mode=mode):
                workload = workloads[mode]
                self.assertEqual(workload.name, name)
                choice, = (choice for choice in workload.choices if choice.checks or choice.launched)
                self.assertEqual((choice.machine, choice.checks, choice.launched, choice.usd_per_check),
                                 (machine, 1, 1, Decimal(cost)))
        targeted = workloads['targeted-test']
        assert isinstance(targeted, machines_data.WorkloadWithoutComparison)
        self.assertEqual(targeted.sentence, 'The records for fixture targeted hold 0 completed runs in this period.')

    def test_the_gate_without_two_sizes_has_no_comparison(self) -> None:
        path = self.book.launch_history_path
        rows = [row for row in path.read_text().splitlines()
                if FAST.region not in row and FASTER.region not in row]
        _ = path.write_text('\n'.join(rows) + '\n')
        merge = self.read().workloads[0]
        self.assertIsInstance(merge, machines_data.WorkloadWithoutComparison)
        self.assertEqual(merge.sentence, 'The records for fixture gate hold 1 completed run on 1 machine size in this period.')

    def test_live_cost_uses_the_live_ratio_between_slider_values(self) -> None:
        path = self.configuration.gate.source
        _ = path.write_text(path.read_text().replace('faster_box_max_cost_ratio=1.50', 'faster_box_max_cost_ratio=1.26'))
        self.configuration = replace(self.configuration, gate=conf.read(path))
        merge = self.merge()
        self.assertEqual(merge.live_ratio, Decimal('1.26'))
        self.assertEqual(merge.live_extra_usd_per_month, merge.steps[1].extra_usd_per_month)
        self.assertGreater(merge.live_extra_usd_per_month, 0)

    def test_removed_deployment_region_stays_measured_and_leaves_the_projection(self) -> None:
        self.write_deployment(SMALL.region, FASTER.region)
        merge = self.merge()
        fast = next(choice for choice in merge.choices if choice.machine == FAST)
        self.assertEqual(fast.checks, 1)
        self.assertFalse(fast.in_deployment)
        self.assertEqual((merge.steps[0].last_ratio, merge.steps[0].added), (Decimal('1.25'), ()))
        self.assertEqual(merge.steps[1].added, (FASTER,))

    def test_cost_per_check_distinguishes_missing_measurements_and_prices(self) -> None:
        unmeasured = ledger.GateRateCandidate('c7a.4xlarge', FASTER.region)
        self.spot = tuple(row for row in self.spot if row.region != FAST.region) + (
            ledger.RegionalSpotPrice(unmeasured.region, 'zone-a', unmeasured.instance_type, Decimal('.40'), READ_AT),)
        self.write_prices()
        with self.book.launch_history_path.open('a') as stream:
            _ = stream.write(json.dumps({'attempt': 'fixture-unmeasured', 'mode': 'merge-gate',
                'region': unmeasured.region, 'instance_type': unmeasured.instance_type,
                'time': WEEK_START + 3500, 'result': 'launched'}) + '\n')
        choices = {row.machine: row for row in self.merge().choices}
        self.assertEqual(choices[unmeasured].usd_per_hour, Decimal('.40'))
        self.assertIsInstance(choices[unmeasured].usd_per_check, machines_data.NoCompletedChecks)
        self.assertEqual(choices[FAST].checks, 1)
        self.assertIsInstance(choices[FAST].usd_per_check, run_cost.NoPricedGateVariant)

    def test_closed_aws_marks_the_last_saved_price_read(self) -> None:
        self.write_prices(closed=True)
        self.assertEqual(self.read().price_read, machines_data.SavedSpotPriceRead(READ_AT, True))

    def test_projection_and_live_worker_use_the_same_rule(self) -> None:
        merge = self.merge()
        choices = tuple(run_cost.GateLaunchVariant(row.region, row.instance_type, '', 'zone-a') for row in self.spot)
        work = run_cost.RunEvidence(
            {(machine.instance_type, machine.region, 'merge-gate'): [Decimal(seconds)]
             for machine, seconds in ((SMALL, 600), (FAST, 450), (FASTER, 300))},
            {}, {}, {}, {}, Decimal(0))
        ranking = cloud_gate.GateRankingInput(run_cost.PlacementRunHistory((), work),
                                             run_cost.GateVariantEligibility(choices, {}))

        def refuse_aws(arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError(f'Unexpected AWS command: {arguments}')

        worker = cloud_gate.GateWorker(AwsCli('fixture', SMALL.region, 1, execute=refuse_aws),
            self.book, self.account_path, self.directory / 'reservation', clock=lambda: NOW)
        with redirect_stdout(StringIO()):
            ordered = worker.rank_variants(choices, 'merge-gate', self.spot, configuration=self.configuration.gate,
                                           ranking=ranking)
        self.assertEqual(tuple(ledger.GateRateCandidate(row.instance_type, row.region) for row in ordered),
                         tuple(row.machine for row in merge.steps[-1].shares))


if __name__ == '__main__':
    _ = unittest.main()

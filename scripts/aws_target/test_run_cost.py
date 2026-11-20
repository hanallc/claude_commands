"""Test the gate cost estimate and the price of saved time."""

from dataclasses import replace
from decimal import Decimal

from scripts.aws_target import ledger_test_support, run_cost
from scripts.lib import conf
from scripts.lint.memory_test_support import write_config_fixture


class GateRunPricingTests(ledger_test_support.CostFixture):
    def test_ranked_cost_includes_transfer_and_wait(self) -> None:
        config = self.root / 'cloud_gate.conf'
        _ = write_config_fixture(config,
            'remote_region_transfer_usd=0.01\n'
            + 'region_transfer_usd={"us-west-2":0.03}\n'
            + 'gate_wait_usd_per_minute=0.06\n'
            + 'region_extra_gate_seconds={"us-west-2":60}\n'
            + 'unmeasured_region_extra_seconds={"us-east-2":120}\n')
        size, home, west, mode = 'c7a.4xlarge', 'us-east-2', 'us-west-2', 'gate'
        seconds = [Decimal(600)]
        evidence = run_cost.RunEvidence({(size, west, mode): seconds}, {(size, mode): seconds}, {mode: seconds},
                                        {(size, west, mode): 2}, {(size, west, mode): 1}, Decimal(30))
        pricing = run_cost.GateRunPricing.from_config(conf.read(config), evidence, home, (mode,))
        price = Decimal('0.6')
        self.assertEqual(pricing.expected_usd(price, size, west), Decimal('0.25'))
        self.assertEqual(pricing.expected_usd(price, size, home), Decimal('0.12'))
        self.assertEqual(pricing.estimate(price, size, home),
                         run_cost.GateRunEstimate(size, price, Decimal(10), Decimal('0.12')))

    def test_break_even_uses_measured_box_times(self) -> None:
        small = run_cost.GateRunEstimate('c7a.4xlarge', Decimal('0.6'), Decimal(10), Decimal('0.10'))
        large = run_cost.GateRunEstimate('c7a.8xlarge', Decimal('1.8'), Decimal(5), Decimal('0.15'))
        outcomes = (
            (large, run_cost.BreakEvenRate(Decimal('0.01'), True)),
            (replace(large, usd_per_run=Decimal('0.10')), run_cost.BreakEvenCondition.ALWAYS),
            (replace(large, median_minutes=Decimal(10)), run_cost.BreakEvenCondition.NEVER),
            (replace(large, median_minutes=run_cost.NoBoxTimeMeasurement()),
             run_cost.BreakEvenCondition.NO_MEASUREMENT),
            (replace(large, median_minutes=Decimal(15), usd_per_run=Decimal('0.05')),
             run_cost.BreakEvenRate(Decimal('0.01'), False)),
            (run_cost.NoPricedGateVariant(), run_cost.BreakEvenCondition.NO_CURRENT_PRICE),
        )
        for estimate, expected in outcomes:
            with self.subTest(estimate=estimate):
                self.assertEqual(run_cost.gate_break_even(small, estimate), expected)
        self.assertEqual(run_cost.gate_break_even(replace(small, median_minutes=run_cost.NoBoxTimeMeasurement()), large),
                         run_cost.BreakEvenCondition.NO_MEASUREMENT)

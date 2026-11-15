"""Test the daily Spot price history that the hourly snapshot keeps."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
from typing import override
from unittest import mock

from scripts.aws_target import aws_target, ledger, price_days
from scripts.aws_target.control_test_support import TestClock
from scripts.aws_target.ledger_test_support import CostFixture
from scripts.lib import conf

DAY = 86_400


def price(size: str, region: str, zone: str, usd: str, now: int) -> ledger.RegionalSpotPrice:
    return ledger.RegionalSpotPrice(region, region + zone, size, Decimal(usd), now)


class PriceDayTests(CostFixture):
    clock: TestClock = TestClock()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.clock = TestClock()
        source = Path(__file__).resolve().parents[2] / 'config/cloud_gate.conf'
        _ = (self.root / 'cloud_gate.conf').write_bytes(source.read_bytes())
        self.enterContext(mock.patch.dict(os.environ, {
            'AWS_SHARED_CREDENTIALS_FILE': str(self.root / 'private-scaler'),
        }))

    def keep(self) -> int:
        return conf.read(self.settings).whole('spot_price_days_keep')

    def execute(self, arguments: Sequence[str], **_options: object) -> subprocess.CompletedProcess[str]:
        region = arguments[arguments.index('--region') + 1]
        reply: dict[str, object]
        if 'get-service-quota' in arguments:
            reply = {'Quota': {'Value': 1000}}
        else:
            sizes = arguments[arguments.index('--instance-types') + 1:arguments.index('--product-descriptions')]
            stamp = datetime.fromtimestamp(self.clock.now, UTC).isoformat()
            reply = {'SpotPriceHistory': [
                dict(InstanceType=size, AvailabilityZone=region + zone, SpotPrice=usd, Timestamp=stamp)
                for size in sizes for zone, usd in (('a', '.25'), ('b', '.3'))]}
        return subprocess.CompletedProcess(arguments, 0, json.dumps(reply), '')

    def test_reads_of_one_day_keep_the_mean_of_the_lowest_zone_price(self) -> None:
        now = self.clock.now
        price_days.record(self.state, [price('c7a.4xlarge', 'us-east-2', 'a', '.3', now),
                                       price('c7a.4xlarge', 'us-east-2', 'b', '.2', now)], now, self.keep())
        price_days.record(self.state, [price('c7a.4xlarge', 'us-east-2', 'a', '.4', now + 3600)], now + 3600, self.keep())
        day = datetime.fromtimestamp(now, UTC).date().isoformat()
        entry = price_days.read_days(self.state)['c7a.4xlarge', 'us-east-2', day]
        self.assertEqual(entry.reads, 2)
        self.assertEqual(entry.mean_usd_per_hour, Decimal('.3'))

    def test_each_type_region_and_day_has_its_own_entry(self) -> None:
        now = self.clock.now
        price_days.record(self.state, [price('c7a.4xlarge', 'us-east-2', 'a', '.3', now),
                                       price('c7a.4xlarge', 'us-west-2', 'a', '.4', now),
                                       price('m7a.4xlarge', 'us-east-2', 'a', '.5', now)], now, self.keep())
        price_days.record(self.state, [price('c7a.4xlarge', 'us-east-2', 'a', '.6', now + DAY)], now + DAY, self.keep())
        days = price_days.read_days(self.state)
        self.assertEqual(len(days), 4)
        self.assertTrue(all(entry.reads == 1 for entry in days.values()))

    def test_days_older_than_the_kept_days_are_dropped(self) -> None:
        keep = self.keep()
        first = self.clock.now
        price_days.record(self.state, [price('c7a.4xlarge', 'us-east-2', 'a', '.3', first)], first, keep)
        last = first + (keep - 1) * DAY
        price_days.record(self.state, [price('c7a.4xlarge', 'us-east-2', 'a', '.3', last)], last, keep)
        self.assertEqual(len(price_days.read_days(self.state)), 2)
        price_days.record(self.state, [price('c7a.4xlarge', 'us-east-2', 'a', '.3', last + DAY)], last + DAY, keep)
        kept = {day for _size, _region, day in price_days.read_days(self.state)}
        self.assertNotIn(datetime.fromtimestamp(first, UTC).date().isoformat(), kept)
        self.assertEqual(len(kept), 2)

    def test_no_file_means_no_days_and_a_day_without_reads_is_refused(self) -> None:
        self.assertEqual(price_days.read_days(self.state), {})
        _ = (self.state / price_days.FILE_NAME).write_text(json.dumps({'days': [
            {'instance_type': 'c7a.4xlarge', 'region': 'us-east-2', 'day': '2026-01-15', 'usd_sum': '0', 'reads': '0'}]}))
        with self.assertRaises(ValueError):
            _ = price_days.read_days(self.state)

    def test_the_hourly_snapshot_records_the_lowest_zone_price_of_each_type_and_region(self) -> None:
        self.assertEqual(aws_target.snapshot(self.settings, self.state, execute=self.execute, clock=self.clock), 0)
        days = price_days.read_days(self.state)
        snapshot = ledger.read_spot_prices(self.state)
        self.assertEqual(set(days), {(row.instance_type, row.region, '2026-01-15') for row in snapshot})
        self.assertTrue(all(entry.mean_usd_per_hour == Decimal('.25') and entry.reads == 1 for entry in days.values()))

    def test_a_corrupt_history_fails_the_snapshot_but_keeps_the_prices(self) -> None:
        _ = (self.state / price_days.FILE_NAME).write_text('not json')
        self.assertEqual(aws_target.snapshot(self.settings, self.state, execute=self.execute, clock=self.clock), 1)
        self.assertTrue(ledger.read_spot_prices(self.state))

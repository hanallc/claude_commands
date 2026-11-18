"""Measure finished cloud gate runs and their billed cost by machine type and kind."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
import json
from pathlib import Path
from statistics import median
from typing import cast

from scripts.aws_target import ledger
from scripts.aws_target.ledger import gate_vcpus as gate_vcpus
from scripts.aws_target.measurement import number
from scripts.lib import conf

KINDS = (('merge-gate', 'merge gates'), ('targeted-test', 'targeted tests'))
EVERY_KIND = tuple(mode for mode, _label in KINDS)
# The mode of a launch request written before launch rows recorded a mode.
NO_MODE = ''


@dataclass(frozen=True)
class GateLaunchVariant:
    region: str
    instance_type: str
    subnet_id: str
    zone: str


@dataclass(frozen=True)
class DeployedGateVariants:
    variants: tuple[GateLaunchVariant, ...]
    unsupported_types: tuple[str, ...]


def deployed_gate_variants(deployment: dict[str, object], types: tuple[str, ...]) -> DeployedGateVariants:
    """List each configured type that the stack allows in each deployed subnet."""
    allowed = cast(list[str], deployment['instance_types']) if 'instance_types' in deployment else types
    unsupported = tuple(size for size in types if size not in allowed)
    sizes = tuple(size for size in types if size in allowed)
    if not sizes:
        return DeployedGateVariants((), unsupported)
    variants = tuple(
        GateLaunchVariant(ledger.text(regional, 'region'), size, ledger.text(subnet, 'id'), ledger.text(subnet, 'zone'))
        for regional in ledger.rows(deployment, 'regions') for size in sizes for subnet in ledger.rows(regional, 'subnets')
    )
    return DeployedGateVariants(variants, unsupported)


@dataclass(frozen=True)
class RunScope:
    """The run kinds that one cost table counts."""
    name: str
    # The filter label on the AWS page.
    label: str
    # One run of this scope, as the table headers and sentences name it.
    noun: str
    modes: tuple[str, ...]


SCOPES = (RunScope('merge-gate', 'Merge gates', 'merge gate', ('merge-gate',)),
          RunScope('targeted-test', 'Targeted runs', 'targeted test', ('targeted-test',)),
          RunScope('both', 'Both', 'run', EVERY_KIND))

# AWS had no Spot box for this type and zone at this price.
ZONE_REFUSALS = frozenset({'InsufficientInstanceCapacity', 'InsufficientSpotCapacity', 'SpotMaxPriceTooLow'})
# The region has no Spot quota room.
QUOTA_REFUSALS = frozenset({'MaxSpotInstanceCountExceeded', 'VcpuLimitExceeded'})


@dataclass(frozen=True)
class FinishedGateRun:
    instance_type: str
    # Rows written before launches recorded a region ran in the home region.
    region: str
    mode: str
    day: str
    test_s: Decimal
    # From the launch row to the finished row: boot, test and the box stop.
    box_s: Decimal


@dataclass(frozen=True)
class GateRuns:
    runs: list[FinishedGateRun]
    omitted: int
    fallbacks: list[tuple[datetime, dict[str, object]]]


def gate_runs(book: ledger.CostLedger, start: datetime, end: datetime, home: str) -> GateRuns:
    """Each finished run in [start, end) with its launch, and the fallbacks in the same window."""
    return runs_from_rows(book.launches(), start, end, home)


def runs_from_rows(rows: Sequence[dict[str, object]], start: datetime, end: datetime, home: str) -> GateRuns:
    launched = {str(row.get('attempt', '')): row for row in rows if row.get('result') == 'launched' and 'mode' in row}
    runs: list[FinishedGateRun] = []
    fallbacks: list[tuple[datetime, dict[str, object]]] = []
    omitted = 0
    for row in rows:
        at = datetime.fromtimestamp(float(str(row['time'])), UTC)
        if not start <= at < end:
            continue
        if row.get('result') == 'fallback':
            fallbacks.append((at, row))
        if 'mode' not in row:
            omitted += 1
        elif row.get('result') == 'finished':
            launch = launched.get(str(row.get('attempt', '')))
            size, mode = row.get('instance_type'), row.get('mode')
            # Placement reads this too, so a row it cannot use is counted, not raised.
            if launch is None or not isinstance(size, str) or not size or not isinstance(mode, str):
                omitted += 1
                continue
            region = str(row.get('region') or launch.get('region') or home)
            runs.append(FinishedGateRun(size, region, mode, at.date().isoformat(), number(row['gate_s']),
                                        number(row['time']) - number(launch['time'])))
    return GateRuns(runs, omitted, fallbacks)


BilledKey = tuple[str, str, str, str]


def billed_readings(book: ledger.CostLedger, start: datetime, end: datetime) -> list[ledger.ChargeRead]:
    """The bill reads of the UTC days in [start, end) that have a bill."""
    return [reading for reading in book.read_bills()
            if start.date().isoformat() <= reading.day < end.date().isoformat() and ledger.is_billed_day(reading)]


def spot_bills(book: ledger.CostLedger, start: datetime, end: datetime) -> dict[tuple[str, str, str], tuple[Decimal, Decimal]]:
    """The billed Spot USD and box hours for each type, region and day in [start, end)."""
    return bills_from_readings(billed_readings(book, start, end))


def bills_from_readings(readings: Sequence[ledger.ChargeRead]) -> dict[tuple[str, str, str], tuple[Decimal, Decimal]]:
    bills: dict[tuple[str, str, str], tuple[Decimal, Decimal]] = {}
    for reading in readings:
        for usage in reading.usage_types:
            size = usage.usage_type.partition('SpotUsage:')[2]
            region = ledger.usage_region(usage.usage_type, 'SpotUsage:')
            if size and region is not None:
                usd, hours = bills.get((size, region, reading.day), (Decimal(0), Decimal(0)))
                bills[size, region, reading.day] = usd + usage.usd, hours + usage.quantity
    return bills


def paid_per_hour(bills: dict[tuple[str, str, str], tuple[Decimal, Decimal]]) -> dict[tuple[str, str], Decimal]:
    """The billed Spot USD per box hour of each type and region."""
    totals: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for (size, region, _day), (usd, hours) in bills.items():
        paid, used = totals.get((size, region), (Decimal(0), Decimal(0)))
        totals[size, region] = paid + usd, used + hours
    return {key: usd / hours for key, (usd, hours) in totals.items() if hours > 0}


def billed_allocations(bills: dict[tuple[str, str, str], tuple[Decimal, Decimal]],
                       runs: list[FinishedGateRun]) -> dict[BilledKey, Decimal]:
    """Split each billed day's Spot cost of a type in a region between its run kinds by box time."""
    groups: dict[BilledKey, Decimal] = defaultdict(Decimal)
    for run in runs:
        groups[run.instance_type, run.region, run.day, run.mode] += run.box_s
    allocations: dict[BilledKey, Decimal] = {}
    for (size, region, at, mode), seconds in groups.items():
        if (size, region, at) in bills:
            total = sum((groups.get((size, region, at, kind), Decimal(0)) for kind, _label in KINDS), Decimal(0))
            allocations[size, region, at, mode] = bills[size, region, at][0] * seconds / total
    return allocations


def billed_runs(runs: list[FinishedGateRun], allocations: dict[BilledKey, Decimal]) -> list[FinishedGateRun]:
    return [run for run in runs if (run.instance_type, run.region, run.day, run.mode) in allocations]


def billed_cost_per_run(runs: list[FinishedGateRun], allocations: dict[BilledKey, Decimal]) -> Decimal:
    groups = {(run.instance_type, run.region, run.day, run.mode) for run in runs}
    return sum((allocations[key] for key in groups), Decimal(0)) / len(runs)


DayKey = tuple[str, str, str]


@dataclass(frozen=True)
class HoursCheck:
    """Billed Spot hours against the recorded box hours of one type and region, on the days with a bill."""
    # None when no day has a bill read; box_hours then counts every day.
    billed_hours: Decimal | None
    box_hours: Decimal
    # abs(billed - box) / box; None without a bill or without box hours.
    hours_gap_ratio: Decimal | None
    # The ratio is above the limit, or AWS billed hours with no recorded box.
    hours_gap_over: bool


def hours_check(billed: Decimal | None, box: Decimal, max_ratio: Decimal) -> HoursCheck:
    ratio = abs(billed - box) / box if billed is not None and box > 0 else None
    over = billed is not None and (ratio > max_ratio if ratio is not None else billed > 0)
    return HoursCheck(billed, box, ratio, over)


def box_hours(book: ledger.CostLedger, start: datetime, end: datetime, home: str, builder: str) -> dict[DayKey, Decimal]:
    """The recorded box hours of each type, region and UTC day in [start, end).

    A gate counts from its reservation to its confirmed end; open and absent gates do not count.
    The builder box of type builder in the home region counts from each running observation to the next other state."""
    first, last = start.date().isoformat(), end.date().isoformat()
    hours: dict[DayKey, Decimal] = defaultdict(Decimal)
    def add(size: str, region: str, left: int, right: int) -> None:
        while left < right:
            day = datetime.fromtimestamp(left, UTC).date()
            stop = min(right, int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp()) + 86400)
            if first <= day.isoformat() < last:
                hours[size, region, day.isoformat()] += Decimal(stop - left) / 3600
            left = stop
    snapshot = book.read_snapshot()
    for reservation in snapshot.gate_reservations:
        if isinstance(reservation.state, ledger.GateTerminated) and reservation.instance_type:
            add(reservation.instance_type, reservation.region or home, reservation.at, reservation.state.at)
    # A builder day bills Spot hours of the builder type, so its running time is box time too.
    running: int | None = None
    for observation in sorted(snapshot.observations, key=lambda row: row.at):
        if observation.state == 'running' and running is None:
            running = observation.at
        elif observation.state != 'running' and running is not None:
            add(builder, home, running, observation.at)
            running = None
    if running is not None:
        add(builder, home, running, int(end.timestamp()))
    return dict(hours)


def day_hours_checks(book: ledger.CostLedger, start: datetime, end: datetime, home: str, builder: str,
                     sizes: Collection[str], max_ratio: Decimal) -> dict[DayKey, HoursCheck]:
    """The hours check of each type in sizes, region and UTC day in [start, end).

    A day with a bill read and no Spot hours for the type bills 0 hours; a day without a read has no bill."""
    readings = billed_readings(book, start, end)
    read_days = {reading.day for reading in readings}
    bills = bills_from_readings(readings)
    boxes = box_hours(book, start, end, home, builder)
    keys = {key for key in boxes.keys() | bills.keys() if key[0] in sizes}
    return {key: hours_check(bills[key][1] if key in bills else Decimal(0) if key[2] in read_days else None,
                             boxes.get(key, Decimal(0)), max_ratio) for key in keys}


def hours_checks(book: ledger.CostLedger, start: datetime, end: datetime, home: str, builder: str,
                 sizes: Collection[str], max_ratio: Decimal) -> dict[tuple[str, str], HoursCheck]:
    """The hours check of each type in sizes and region, summed over the days in [start, end) with a bill."""
    billed: dict[tuple[str, str], list[HoursCheck]] = defaultdict(list)
    unbilled: dict[tuple[str, str], list[HoursCheck]] = defaultdict(list)
    for (size, region, _day), check in day_hours_checks(book, start, end, home, builder, sizes, max_ratio).items():
        (unbilled if check.billed_hours is None else billed)[size, region].append(check)
    checks: dict[tuple[str, str], HoursCheck] = {}
    for key in billed.keys() | unbilled.keys():
        if key in billed:
            checks[key] = hours_check(sum((day.billed_hours or Decimal(0) for day in billed[key]), Decimal(0)),
                                      sum((day.box_hours for day in billed[key]), Decimal(0)), max_ratio)
        else:
            checks[key] = HoursCheck(None, sum((day.box_hours for day in unbilled[key]), Decimal(0)), None, False)
    return checks


def median_run_time(seconds: list[Decimal]) -> str:
    if not seconds:
        return '—'
    minutes, remainder = divmod(int(median(seconds)), 60)
    return f'{minutes}m {remainder}s'


class BoxTimeSource(Enum):
    """Where the box times of a type and region come from."""
    OWN = ''
    OTHER_REGIONS = ' elsewhere'
    SAME_VCPUS = ' same vCPUs'


@dataclass(frozen=True)
class RunEvidence:
    """Box times and refusals by type and region, for the expected cost of one run."""
    by_region: dict[tuple[str, str, str], list[Decimal]]
    by_type: dict[tuple[str, str], list[Decimal]]
    by_mode: dict[str, list[Decimal]]
    # Requests by type, region and mode; NO_MODE holds requests without a mode.
    refused: dict[tuple[str, str, str], int]
    launched: dict[tuple[str, str, str], int]
    # Median seconds from a refused launch request to the next request of the same attempt.
    refusal_wait_s: Decimal

    def box_seconds(self, size: str, region: str, modes: tuple[str, ...]) -> tuple[list[Decimal], BoxTimeSource]:
        """Own region box times, else the type's times in all regions, else times of types with its vCPU count."""
        own = [time for mode in modes for time in self.by_region.get((size, region, mode), [])]
        if own:
            return own, BoxTimeSource.OWN
        typed = [time for mode in modes for time in self.by_type.get((size, mode), [])]
        if typed:
            return typed, BoxTimeSource.OTHER_REGIONS
        # A type without runs is priced by the runs of a type with the same vCPU count, not by the median of all types.
        vcpus = gate_vcpus(size)
        return [time for (other, mode), times in self.by_type.items()
                if vcpus and mode in modes and gate_vcpus(other) == vcpus for time in times], BoxTimeSource.SAME_VCPUS

    def median_box_seconds(self, size: str, region: str, modes: tuple[str, ...]) -> Decimal | NoBoxTimeMeasurement:
        seconds, _source = self.box_seconds(size, region, modes)
        return median(seconds) if seconds else NoBoxTimeMeasurement()

    def refusals_per_launch(self, size: str, region: str, modes: tuple[str, ...]) -> tuple[Decimal | None, int, int]:
        """Refused requests for each launched box, with the counts it comes from.

        Requests without a mode count only when the modes cover every kind.
        """
        counted = (*modes, NO_MODE) if set(EVERY_KIND) <= set(modes) else modes
        refused = sum(self.refused.get((size, region, mode), 0) for mode in counted)
        launched = sum(self.launched.get((size, region, mode), 0) for mode in counted)
        if not refused + launched:
            return None, 0, 0
        return Decimal(refused) / max(launched, 1), refused, launched

    def expected_usd(self, price: Decimal, size: str, region: str, modes: tuple[str, ...], wait_usd_per_minute: Decimal,
                     default_seconds: Decimal, *, unmeasured_extra_s: Decimal) -> Decimal:
        """Spot price for the median box time, plus the wait cost of the expected refusals first.

        A region without its own runs borrows the other regions' box time and adds unmeasured_extra_s for its transfers."""
        seconds, source = self.box_seconds(size, region, modes)
        seconds = seconds or [time for mode in modes for time in self.by_mode.get(mode, [])]
        box = ((Decimal(median(seconds)) if seconds else default_seconds)
               + (Decimal(0) if source is BoxTimeSource.OWN else unmeasured_extra_s))
        # Capacity refusals do not depend on the run kind, so every request counts.
        refusals = self.refusals_per_launch(size, region, EVERY_KIND)[0] or Decimal(0)
        return price * box / 3600 + refusals * self.refusal_wait_s * wait_usd_per_minute / 60


@dataclass(frozen=True)
class NoBoxTimeMeasurement:
    """The run evidence has no box time for this choice."""


@dataclass(frozen=True)
class GateRunEstimate:
    instance_type: str
    spot_usd_per_hour: Decimal
    median_minutes: Decimal | NoBoxTimeMeasurement
    usd_per_run: Decimal


@dataclass(frozen=True)
class NoPricedGateVariant:
    """The snapshot has no current price for this vCPU count in the region."""


@dataclass(frozen=True)
class GateTimeComparison:
    region: str
    small: GateRunEstimate | NoPricedGateVariant
    large: GateRunEstimate | NoPricedGateVariant


class BreakEvenCondition(Enum):
    ALWAYS = 'always'
    NEVER = 'never'
    NO_MEASUREMENT = 'no measurement'
    NO_CURRENT_PRICE = 'no current price'


@dataclass(frozen=True)
class BreakEvenRate:
    usd_per_minute: Decimal
    faster_32_vcpu: bool


def gate_break_even(
    small: GateRunEstimate | NoPricedGateVariant,
    large: GateRunEstimate | NoPricedGateVariant,
) -> BreakEvenRate | BreakEvenCondition:
    """Find the time price at which the two estimates have the same total cost."""
    if isinstance(small, NoPricedGateVariant) or isinstance(large, NoPricedGateVariant):
        return BreakEvenCondition.NO_CURRENT_PRICE
    if isinstance(small.median_minutes, NoBoxTimeMeasurement) or isinstance(large.median_minutes, NoBoxTimeMeasurement):
        return BreakEvenCondition.NO_MEASUREMENT
    minutes_saved = small.median_minutes - large.median_minutes
    extra_usd = large.usd_per_run - small.usd_per_run
    if minutes_saved >= 0 and extra_usd <= 0:
        return BreakEvenCondition.ALWAYS
    if minutes_saved <= 0 and extra_usd > 0:
        return BreakEvenCondition.NEVER
    return BreakEvenRate((extra_usd / minutes_saved).copy_abs(), minutes_saved > 0)


@dataclass(frozen=True)
class GateRunPricing:
    """The policy and run evidence that rank priced gate variants."""
    evidence: RunEvidence
    home: str
    modes: tuple[str, ...]
    default_seconds: Decimal
    remote_transfer_usd: Decimal
    region_transfer_usd: dict[str, Decimal]
    wait_usd_per_minute: Decimal
    region_extra_seconds: dict[str, Decimal]
    unmeasured_extra_seconds: dict[str, Decimal]

    @classmethod
    def from_config(cls, values: conf.ConfigRead, evidence: RunEvidence, home: str,
                    modes: tuple[str, ...]) -> GateRunPricing:
        return cls(evidence, home, modes,
                   Decimal(values.whole('lifetime_minutes') * 60),
                   conf.decimal(values.text('remote_region_transfer_usd')),
                   region_numbers(values, 'region_transfer_usd'),
                   conf.decimal(values.text('gate_wait_usd_per_minute')),
                   region_numbers(values, 'region_extra_gate_seconds'),
                   region_numbers(values, 'unmeasured_region_extra_seconds'))

    def expected_usd(self, price: Decimal, size: str, region: str) -> Decimal:
        box_and_refusals = self.evidence.expected_usd(
            price, size, region, self.modes, self.wait_usd_per_minute, self.default_seconds,
            unmeasured_extra_s=self.unmeasured_extra_seconds.get(region, Decimal(0)),
        )
        transfer = self.region_transfer_usd.get(region, self.remote_transfer_usd) if region != self.home else Decimal(0)
        wait = self.region_extra_seconds.get(region, Decimal(0)) * self.wait_usd_per_minute / 60
        return box_and_refusals + transfer + wait

    def estimate(self, price: Decimal, size: str, region: str) -> GateRunEstimate:
        seconds = self.evidence.median_box_seconds(size, region, self.modes)
        minutes = seconds / 60 if isinstance(seconds, Decimal) else seconds
        return GateRunEstimate(size, price, minutes, self.expected_usd(price, size, region))


def current_zone_prices(directory: Path, now: float, max_age: int) -> dict[tuple[str, str, str], Decimal]:
    """Use the latest current Spot price for each region, type, and zone."""
    try:
        return {(row.region, row.instance_type, row.zone): row.usd_per_hour
                for row in sorted(ledger.read_spot_prices(directory), key=lambda row: row.read_at)
                if 0 <= now - row.read_at <= max_age}
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError):
        return {}


def gate_launch_history(directory: Path, now: float, window_seconds: int) -> tuple[dict[str, object], ...]:
    """Read launch rows in the window through the current time."""
    try:
        history = ledger.recent_gate_launches(directory, now - window_seconds)
    except OSError:
        return ()
    return tuple(row for row in history if float(str(row['time'])) <= now)


@dataclass(frozen=True)
class GateVariantEligibility:
    variants: tuple[GateLaunchVariant, ...]
    prices: dict[GateLaunchVariant, Decimal]
    slow_box_seconds: dict[tuple[str, str], Decimal]


def gate_variant_eligibility(
    deployed: Sequence[GateLaunchVariant], directory: Path, evidence: RunEvidence, mode: str,
    *, now: float, max_age: int, max_gate_minutes: int, live_prices: Sequence[ledger.RegionalSpotPrice] = (),
) -> GateVariantEligibility:
    """Filter deployed variants by median time and match prices by region, type, and zone."""
    zone_prices = current_zone_prices(directory, now, max_age)
    zone_prices.update({(row.region, row.instance_type, row.zone): row.usd_per_hour for row in live_prices})
    sizes = dict.fromkeys((variant.instance_type, variant.region) for variant in deployed)
    slow = {(size, region): seconds for size, region in sizes
            if isinstance(seconds := evidence.median_box_seconds(size, region, (mode,)), Decimal)
            and seconds > max_gate_minutes * 60}
    variants = tuple(variant for variant in deployed if (variant.instance_type, variant.region) not in slow)
    prices = {variant: zone_prices[key] for variant in variants
              if (key := (variant.region, variant.instance_type, variant.zone)) in zone_prices}
    return GateVariantEligibility(variants, prices, slow)


def merge_gate_comparisons(directory: Path, now: int, values: conf.ConfigRead,
                           policy: conf.ConfigRead) -> tuple[GateTimeComparison, ...]:
    """Compare the cheapest priced 16-vCPU and 32-vCPU merge gates in each region."""
    regions = tuple(dict.fromkeys(values.words('regions')))
    home = policy.text('region')
    missing = NoPricedGateVariant()
    try:
        deployment = ledger.mapping(cast(object, json.loads((directory / 'gate-deployment.json').read_text())))
        if 'regions' not in deployment:
            deployment = {**deployment, 'regions': [{**deployment, 'region': deployment.get('region', home)}]}
        deployed = deployed_gate_variants(deployment, values.words('instance_types'))
    except (OSError, ValueError, TypeError):
        return tuple(GateTimeComparison(region, missing, missing) for region in regions)

    end = datetime.fromtimestamp(now, UTC)
    days = policy.whole('run_cost_days')
    max_age = policy.whole('price_max_age_seconds')
    cooldown = values.whole('capacity_cooldown_seconds')
    rows = gate_launch_history(directory, now, max(max_age, cooldown, days * 86400))
    evidence = run_evidence(rows, end - timedelta(days=days), end, home)
    eligibility = gate_variant_eligibility(deployed.variants, directory, evidence, 'merge-gate',
                                           now=now, max_age=max_age, max_gate_minutes=values.whole('max_gate_minutes'))
    if not eligibility.prices:
        return tuple(GateTimeComparison(region, missing, missing) for region in regions)
    pricing = GateRunPricing.from_config(values, evidence, home, ('merge-gate',))
    estimates: dict[tuple[str, int], list[GateRunEstimate]] = defaultdict(list)
    for variant, price in eligibility.prices.items():
        vcpus = gate_vcpus(variant.instance_type)
        if vcpus in (16, 32) and variant.region in regions:
            estimates[variant.region, vcpus].append(pricing.estimate(price, variant.instance_type, variant.region))
    return tuple(GateTimeComparison(
        region, min(estimates[region, 16], key=lambda row: row.usd_per_run, default=missing),
        min(estimates[region, 32], key=lambda row: row.usd_per_run, default=missing),
    ) for region in regions)


def region_numbers(values: conf.ConfigRead, key: str) -> dict[str, Decimal]:
    """Read a JSON map from region names to nonnegative numbers; a missing region has none."""
    numbers = {region: conf.decimal(str(value))
               for region, value in ledger.mapping(cast(object, json.loads(values.text(key)))).items()}
    if any(not region or value < 0 for region, value in numbers.items()):
        raise ValueError(f'{key} needs region names and nonnegative numbers')
    return numbers


def run_evidence(rows: Sequence[dict[str, object]], start: datetime, end: datetime, home: str) -> RunEvidence:
    by_region: dict[tuple[str, str, str], list[Decimal]] = defaultdict(list)
    by_type: dict[tuple[str, str], list[Decimal]] = defaultdict(list)
    by_mode: dict[str, list[Decimal]] = defaultdict(list)
    for run in runs_from_rows(rows, start, end, home).runs:
        by_region[run.instance_type, run.region, run.mode].append(run.box_s)
        by_type[run.instance_type, run.mode].append(run.box_s)
        by_mode[run.mode].append(run.box_s)
    refused: dict[tuple[str, str, str], int] = defaultdict(int)
    launched: dict[tuple[str, str, str], int] = defaultdict(int)
    attempts: dict[str, list[tuple[Decimal, str]]] = defaultdict(list)
    low, high = Decimal(str(start.timestamp())), Decimal(str(end.timestamp()))
    for row in rows:
        at = number(row['time'])
        if not low <= at < high:
            continue
        result = row.get('result')
        attempts[str(row.get('attempt', ''))].append((at, str(result)))
        key = str(row.get('instance_type', '')), str(row.get('region') or home), str(row.get('mode', NO_MODE))
        if result == 'launched':
            launched[key] += 1
        elif result == 'refused' and row.get('error_code') in ZONE_REFUSALS | QUOTA_REFUSALS:
            refused[key] += 1
    waits: list[Decimal] = []
    for events in attempts.values():
        ordered = sorted(events)
        waits.extend(after[0] - before[0] for before, after in zip(ordered, ordered[1:]) if before[1] == 'refused')
    return RunEvidence(dict(by_region), dict(by_type), dict(by_mode), dict(refused), dict(launched),
                       Decimal(median(waits)) if waits else Decimal(0))

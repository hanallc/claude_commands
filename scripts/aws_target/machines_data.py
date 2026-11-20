"""Read local evidence for the AWS Machines page without calling AWS."""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
import heapq
from pathlib import Path
import re
from statistics import median
from typing import Literal, cast

from scripts.aws_target import cloud_gate, ledger, price_days, run_cost
from scripts.aws_target.aws_target import read_snapshot, snapshot_failures
from scripts.lib import build_steps, conf


_WEEKS_PER_YEAR = 52
_MONTHS_PER_YEAR = 12
_HOURS_IN_SECONDS = 3600
_DAYS_PER_WEEK = 7
_RULE_OFF = Decimal(1)
_WORKLOADS = ('merge-gate', 'targeted-test', 'test-affected')
_SAVED_PRICE_AUTH_FAILURE = re.compile(
    r'\b(?:AuthFailure|ExpiredToken(?:Exception)?|InvalidClientTokenId|UnrecognizedClientException)\b'
    + r'|unable to locate credentials', re.IGNORECASE)


@dataclass(frozen=True)
class MachinesConfiguration:
    page: conf.ConfigRead
    gate: conf.ConfigRead
    account: conf.ConfigRead
    names: build_steps.StepNames


@dataclass(frozen=True)
class SavedSpotPriceRead:
    read_at: int
    aws_closed: bool


@dataclass(frozen=True)
class NoSavedSpotPriceRead:
    """The snapshot has no successful quote with a read time."""


@dataclass(frozen=True)
class NoHoursSaved:
    """The projection has no time saving to divide the cost by."""


@dataclass(frozen=True)
class NoCompletedChecks:
    """The choice has no completed checks in the measurement window."""


@dataclass(frozen=True)
class MachineChoice:
    machine: ledger.GateRateCandidate
    vcpus: int
    checks: int
    median_seconds: Decimal | run_cost.NoBoxTimeMeasurement
    usd_per_hour: Decimal | run_cost.NoPricedGateVariant
    usd_per_check: Decimal | run_cost.NoPricedGateVariant | NoCompletedChecks
    in_deployment: bool
    capacity_refusals: int
    launched: int
    small_sample: bool
    quota_vcpus: Decimal
    quota_assumed: bool
    quota_fit_share: Decimal
    spot_granted_share: Decimal
    free_share: Decimal


@dataclass(frozen=True)
class MachineRunShare:
    machine: ledger.GateRateCandidate
    share: Decimal


@dataclass(frozen=True)
class MachineLimit:
    ratio: Decimal
    ceiling_extra_usd_per_month: Decimal


@dataclass(frozen=True)
class MachineStep:
    first_ratio: Decimal
    last_ratio: Decimal
    added: tuple[ledger.GateRateCandidate, ...]
    shares: tuple[MachineRunShare, ...]
    extra_usd_per_month: Decimal
    hours_saved_per_month: Decimal
    usd_per_hour_saved: Decimal | NoHoursSaved
    limits: tuple[MachineLimit, ...]


@dataclass(frozen=True)
class MeasuredWorkload:
    name: str
    mode: str
    choices: tuple[MachineChoice, ...]
    reference: ledger.GateRateCandidate
    checks: int
    checks_per_month: Decimal
    reference_usd_per_month: Decimal
    concurrent_starts: tuple[tuple[int, int], ...]
    live_ratio: Decimal
    live_extra_usd_per_month: Decimal
    steps: tuple[MachineStep, ...]
    assumptions: tuple[str, ...]

    @property
    def sentence(self) -> str:
        sizes = len({choice.vcpus for choice in self.choices if choice.checks})
        return _measurement_sentence(self.name, self.checks, sizes)


@dataclass(frozen=True)
class WorkloadWithoutComparison:
    name: str
    mode: str
    sentence: str
    choices: tuple[MachineChoice, ...] = ()


@dataclass(frozen=True)
class JobMachineAddition:
    job_name: str
    machine: ledger.GateRateCandidate


@dataclass(frozen=True)
class AllJobsStep:
    first_ratio: Decimal
    last_ratio: Decimal
    added: tuple[JobMachineAddition, ...]
    extra_usd_per_month: Decimal
    hours_saved_per_month: Decimal
    usd_per_hour_saved: Decimal | NoHoursSaved
    limits: tuple[MachineLimit, ...]


@dataclass(frozen=True)
class AllJobsProjection:
    live_ratio: Decimal
    live_extra_usd_per_month: Decimal
    steps: tuple[AllJobsStep, ...]
    assumption: str


@dataclass(frozen=True)
class NoMeasuredWorkloads:
    """No job has a machine comparison."""


@dataclass(frozen=True)
class MachinesPageData:
    window_start: datetime
    window_end: datetime
    ratio_max: Decimal
    ratio_step: Decimal
    price_read: SavedSpotPriceRead | NoSavedSpotPriceRead
    workloads: tuple[MeasuredWorkload | WorkloadWithoutComparison, ...]

    @property
    def total(self) -> AllJobsProjection | NoMeasuredWorkloads:
        """Sum the measured jobs at each slider value."""
        measured = tuple(job for job in self.workloads if isinstance(job, MeasuredWorkload))
        if not measured:
            return NoMeasuredWorkloads()
        return _sum_jobs(measured)


def _sum_jobs(jobs: Sequence[MeasuredWorkload]) -> AllJobsProjection:
    starts: dict[Decimal, list[tuple[int, str, MachineStep]]] = defaultdict(list)
    ceilings: dict[Decimal, Decimal] = defaultdict(Decimal)
    for index, job in enumerate(jobs):
        for step in job.steps:
            starts[step.first_ratio].append((index, job.name, step))
            for limit in step.limits:
                ceilings[limit.ratio] += limit.ceiling_extra_usd_per_month
    ratios = sorted(ceilings)
    boundaries = [bisect_left(ratios, start) for start in sorted(starts)]
    active: dict[int, MachineStep] = {}
    steps: list[AllJobsStep] = []
    for begin, end in zip(boundaries, (*boundaries[1:], len(ratios)), strict=True):
        changes = starts[ratios[begin]]
        active.update((index, step) for index, _, step in changes)
        added = tuple(JobMachineAddition(name, machine) for _, name, step in changes for machine in step.added)
        limits = tuple(MachineLimit(ratio, ceilings[ratio]) for ratio in ratios[begin:end])
        extra = sum((step.extra_usd_per_month for step in active.values()), Decimal(0))
        hours = sum((step.hours_saved_per_month for step in active.values()), Decimal(0))
        steps.append(AllJobsStep(limits[0].ratio, limits[-1].ratio, added, extra, hours,
                                 extra / hours if hours else NoHoursSaved(), limits))
    live_extra = sum((job.live_extra_usd_per_month for job in jobs), Decimal(0))
    return AllJobsProjection(jobs[0].live_ratio, live_extra, tuple(steps),
        'Each job uses the full quota in its estimate, so the total hours saved are an upper estimate.')


@dataclass(frozen=True)
class _GateInterval:
    machine: ledger.GateRateCandidate
    start: int
    end: int
    finished_at: datetime


@dataclass(frozen=True)
class _SavedPrices:
    read: SavedSpotPriceRead | NoSavedSpotPriceRead
    hourly: dict[ledger.GateRateCandidate, Decimal]
    quotas: dict[str, ledger.SpotQuota]


def _workload(row: dict[str, object]) -> str:
    job = row.get('gate_job')
    if isinstance(job, dict):
        step = cast(dict[str, object], job).get('step')
        if isinstance(step, str) and step:
            return step
    mode = row.get('mode')
    if mode == 'gate':
        return 'merge-gate'
    return mode if isinstance(mode, str) and mode in _WORKLOADS else ''


def _saved_prices(directory: Path, now: int, days: int) -> _SavedPrices:
    latest: dict[tuple[ledger.GateRateCandidate, str], ledger.RegionalSpotPrice] = {}
    for row in sorted(ledger.read_spot_prices(directory), key=lambda price: price.read_at):
        if row.read_at <= now:
            latest[ledger.GateRateCandidate(row.instance_type, row.region), row.zone] = row
    read_at = max((row.read_at for row in latest.values()), default=-1)
    read: SavedSpotPriceRead | NoSavedSpotPriceRead = NoSavedSpotPriceRead()
    hourly: dict[ledger.GateRateCandidate, Decimal] = {}
    if read_at >= 0:
        region_reads: dict[str, int] = {}
        for (machine, _zone), row in latest.items():
            region_reads[row.region] = max(region_reads.get(row.region, row.read_at), row.read_at)
            hourly[machine] = min(hourly.get(machine, row.usd_per_hour), row.usd_per_hour)
        failed = {failure.region for failure in snapshot_failures(read_snapshot(directory))
                  if failure.read == 'price' and region_reads.get(failure.region, now) < failure.at <= now
                  and _SAVED_PRICE_AUTH_FAILURE.search(failure.error + ' ' + failure.reason)}
        read = SavedSpotPriceRead(read_at, region_reads.keys() <= failed)
        last_day = datetime.fromtimestamp(read_at, UTC).date()
        first_day = last_day - timedelta(days=days - 1)
        means: dict[ledger.GateRateCandidate, list[Decimal]] = defaultdict(list)
        for entry in price_days.read_days(directory).values():
            if first_day.isoformat() <= entry.day <= last_day.isoformat():
                means[ledger.GateRateCandidate(entry.instance_type, entry.region)].append(entry.mean_usd_per_hour)
        hourly.update({machine: median(prices) for machine, prices in means.items()})
    quotas = {row.region: row for row in ledger.read_spot_quotas(directory) if row.read_at <= now}
    return _SavedPrices(read, hourly, quotas)


def _intervals(rows: Sequence[dict[str, object]], reservations: dict[str, ledger.GateReservation], home: str,
               start: datetime, end: datetime) -> tuple[_GateInterval, ...]:
    intervals: dict[str, _GateInterval] = {}
    for row in rows:
        if row.get('result') != 'finished':
            continue
        finished_at = datetime.fromtimestamp(float(str(row['time'])), UTC)
        if not start <= finished_at < end:
            continue
        attempt = str(row.get('attempt', ''))
        reservation = reservations.get(attempt)
        if reservation is None or not isinstance(reservation.state, ledger.GateTerminated):
            continue
        size = reservation.instance_type or str(row.get('instance_type') or '')
        if ledger.gate_vcpus(size) and reservation.state.at > reservation.at:
            region = reservation.region or str(row.get('region') or home)
            _ = intervals.setdefault(attempt, _GateInterval(ledger.GateRateCandidate(size, region), reservation.at,
                                                            reservation.state.at, finished_at))
    return tuple(intervals.values())


def _concurrent_starts(intervals: Sequence[_GateInterval]) -> tuple[tuple[int, int], ...]:
    ends: list[int] = []
    counts: Counter[int] = Counter()
    for interval in sorted(intervals, key=lambda row: (row.start, row.end)):
        while ends and ends[0] <= interval.start:
            _ = heapq.heappop(ends)
        heapq.heappush(ends, interval.end)
        counts[len(ends)] += 1
    return tuple(sorted(counts.items()))


def _requests(rows: Sequence[dict[str, object]], home: str, start: datetime,
              end: datetime) -> tuple[Counter[ledger.GateRateCandidate], Counter[ledger.GateRateCandidate]]:
    launched: Counter[ledger.GateRateCandidate] = Counter()
    refused: Counter[ledger.GateRateCandidate] = Counter()
    low, high = start.timestamp(), end.timestamp()
    for row in rows:
        if not low <= float(str(row['time'])) < high:
            continue
        size = str(row.get('instance_type') or '')
        if not ledger.gate_vcpus(size):
            continue
        machine = ledger.GateRateCandidate(size, str(row.get('region') or home))
        if row.get('result') == 'launched':
            launched[machine] += 1
        elif row.get('result') == 'refused' and row.get('error_code') in run_cost.ZONE_REFUSALS:
            refused[machine] += 1
    return launched, refused


def _steps(rows: Sequence[cloud_gate.GateRankedChoice], types: tuple[str, ...], ratios: Sequence[Decimal],
           reference: cloud_gate.GateRankedChoice, choices: dict[ledger.GateRateCandidate, MachineChoice],
           monthly_checks: Decimal) -> tuple[MachineStep, ...]:
    assert isinstance(reference.estimate, run_cost.GatePlacementEstimate)
    base = reference.estimate.aws_spend_usd
    reference_machine = ledger.GateRateCandidate(reference.variant.instance_type, reference.variant.region)
    allowed_before: set[ledger.GateRateCandidate] = set()
    steps: list[MachineStep] = []
    for ratio in ratios:
        allowed: set[ledger.GateRateCandidate] = set()
        shares: list[MachineRunShare] = []
        extra = saved = Decimal(0)
        left = Decimal(1)
        for row in cloud_gate.rank_gate_choices(rows, types, ratio).choices:
            if not isinstance(row.decision, cloud_gate.FasterWithinRatio):
                continue
            assert isinstance(row.estimate, run_cost.GatePlacementEstimate)
            machine = ledger.GateRateCandidate(row.variant.instance_type, row.variant.region)
            allowed.add(machine)
            share = left * choices[machine].free_share
            shares.append(MachineRunShare(machine, share))
            left -= share
            extra += share * (row.estimate.aws_spend_usd - base)
            saved += share * row.decision.saved_seconds
        limit = MachineLimit(ratio, (ratio - _RULE_OFF) * base * monthly_checks)
        if steps and allowed == allowed_before:
            previous = steps[-1]
            steps[-1] = MachineStep(previous.first_ratio, ratio, previous.added, previous.shares,
                previous.extra_usd_per_month, previous.hours_saved_per_month, previous.usd_per_hour_saved,
                (*previous.limits, limit))
            continue
        added = tuple(row.machine for row in shares if row.machine not in allowed_before)
        shares.append(MachineRunShare(reference_machine, left))
        monthly_extra = extra * monthly_checks
        monthly_saved = saved * monthly_checks / _HOURS_IN_SECONDS
        steps.append(MachineStep(ratio, ratio, added, tuple(shares), monthly_extra, monthly_saved,
            monthly_extra / monthly_saved if monthly_saved else NoHoursSaved(), (limit,)))
        allowed_before = allowed
    return tuple(steps)


def _measurement_sentence(name: str, runs: int, sizes: int) -> str:
    run_word = 'run' if runs == 1 else 'runs'
    size_word = 'size' if sizes == 1 else 'sizes'
    return f'The records for {name} hold {runs} completed {run_word} on {sizes} machine {size_word} in this period.'


def _project_workload(name: str, mode: str, rows: Sequence[dict[str, object]], intervals: Sequence[_GateInterval],
                      prices: _SavedPrices, start: datetime, end: datetime, configuration: MachinesConfiguration,
                      ratios: Sequence[Decimal], deployed: Mapping[ledger.GateRateCandidate, run_cost.GateLaunchVariant]
                      ) -> MeasuredWorkload | WorkloadWithoutComparison:
    checks = len(intervals)
    if not checks:
        return WorkloadWithoutComparison(name, mode, f'The records for {name} hold 0 completed runs in this period.')
    home = configuration.account.text('region')
    gate = configuration.gate
    types = gate.words('instance_types')
    run_mode = 'targeted-test' if mode == 'test-affected' else mode
    concurrency = _concurrent_starts(intervals)
    quota_assumption = Decimal(gate.whole('spot_quota_request_vcpus'))
    small_sample_checks = configuration.page.whole('machines.small_sample_checks')
    samples: dict[ledger.GateRateCandidate, list[Decimal]] = defaultdict(list)
    runs: list[run_cost.FinishedGateRun] = []
    for interval in intervals:
        size, region = interval.machine.instance_type, interval.machine.region
        seconds = Decimal(interval.end - interval.start)
        samples[interval.machine].append(seconds)
        runs.append(run_cost.FinishedGateRun(size, region, run_mode, interval.finished_at.date().isoformat(),
                                            Decimal(0), seconds))
    evidence = run_cost.evidence_from_runs(runs, rows, start, end, home)
    pricing = run_cost.GateRunPricing.from_config(gate, evidence, home, (mode,))
    launched, refused = _requests(rows, home, start, end)
    max_seconds = gate.whole('max_gate_minutes') * 60
    choices: dict[ledger.GateRateCandidate, MachineChoice] = {}
    ranked_rows: list[cloud_gate.GateRankedChoice] = []
    assumptions = [
        'A month has 52 weeks divided by 12.',
        f'The measured run rate, overlap, and share of Spot refusals repeat for {name}.',
        f'The estimate gives {name} the full quota. Other jobs can reduce its available quota.',
        'Each successful launch and each capacity refusal counts as one Spot request.',
        'Prices use the median of saved daily means, or the lowest saved zone price when daily means are absent.',
        f'The projection uses deployed choices with measurements within the time limit for {name}.',
        'The projection includes configured transfer cost.',
    ]
    for machine in sorted(samples.keys() | launched.keys() | refused.keys(),
                          key=lambda item: (item.instance_type, item.region)):
        size, region = machine.instance_type, machine.region
        cpus = ledger.gate_vcpus(size)
        quota = prices.quotas.get(region)
        recorded_quota = quota.limit(ledger.spot_quota_class(size)) if quota is not None else ledger.NoSpotQuota()
        assumed = not isinstance(recorded_quota, Decimal)
        quota_vcpus = recorded_quota if isinstance(recorded_quota, Decimal) else quota_assumption
        if assumed:
            assumption = f'The records have no quota for {region}. The estimate uses {quota_vcpus} vCPUs.'
            if assumption not in assumptions:
                assumptions.append(assumption)
        fits = Decimal(sum(count for occupancy, count in concurrency if occupancy <= quota_vcpus // cpus)) / checks
        requests = launched[machine] + refused[machine]
        granted = Decimal(launched[machine]) / requests if requests else Decimal(0)
        if not requests:
            assumptions.append(f'The records have no Spot requests for {size} in {region}. The estimate uses 0% availability.')
        hourly = prices.hourly.get(machine, run_cost.NoPricedGateVariant())
        times = samples.get(machine, [])
        typical = median(times) if times else run_cost.NoBoxTimeMeasurement()
        variant = deployed.get(machine)
        cost: Decimal | run_cost.NoPricedGateVariant | NoCompletedChecks = NoCompletedChecks()
        if isinstance(typical, Decimal):
            estimate = (pricing.placement_estimate(hourly, size, region)
                        if isinstance(hourly, Decimal) else hourly)
            cost = estimate.aws_spend_usd if isinstance(estimate, run_cost.GatePlacementEstimate) else estimate
            if variant is not None and typical <= max_seconds:
                ranked_rows.append(cloud_gate.GateRankedChoice(variant, len(times), typical, estimate,
                    cloud_gate.NoComparableSpendRatio(), cloud_gate.GateCapacity(False, False, False),
                    cloud_gate.DefaultOrder()))
        choices[machine] = MachineChoice(machine, cpus, len(times), typical, hourly, cost, variant is not None,
            refused[machine], launched[machine], len(times) < small_sample_checks, quota_vcpus, assumed,
            fits, granted, fits * granted)
    measured_sizes = {choice.vcpus for choice in choices.values() if choice.checks}
    if len(measured_sizes) == 1:
        return WorkloadWithoutComparison(name, mode, _measurement_sentence(name, checks, 1), tuple(choices.values()))
    if not ranked_rows:
        return WorkloadWithoutComparison(name, mode,
            f'No deployed choice has a measurement for {name} within the time limit.', tuple(choices.values()))
    ranking = cloud_gate.rank_gate_choices(ranked_rows, types, _RULE_OFF)
    reference = ranking.reference
    if not isinstance(reference, cloud_gate.GateRankedChoice):
        return WorkloadWithoutComparison(name, mode,
            f'No eligible measured choice for {name} has a saved price.', tuple(choices.values()))
    assert isinstance(reference.estimate, run_cost.GatePlacementEstimate)
    monthly_checks = Decimal(checks * _DAYS_PER_WEEK * _WEEKS_PER_YEAR) / (_MONTHS_PER_YEAR * (end - start).days)
    steps = _steps(ranked_rows, types, ratios, reference, choices, monthly_checks)
    live_ratio = gate.value('faster_box_max_cost_ratio', conf.decimal)
    live_extra = next((step.extra_usd_per_month for step in steps if any(
        limit.ratio == live_ratio for limit in step.limits)), None)
    if live_extra is None:
        live_extra = _steps(ranked_rows, types, (live_ratio,), reference, choices, monthly_checks)[0].extra_usd_per_month
    return MeasuredWorkload(name, mode, tuple(choices.values()),
        ledger.GateRateCandidate(reference.variant.instance_type, reference.variant.region), checks,
        monthly_checks, reference.estimate.aws_spend_usd * monthly_checks, concurrency,
        live_ratio, live_extra, steps, tuple(assumptions))


def read_machines_data(directory: Path, now: int,
                       configuration: MachinesConfiguration | Literal['live_settings'] = 'live_settings') -> MachinesPageData:
    """Read the last complete UTC days and project every slider value."""
    if configuration == 'live_settings':
        configuration = MachinesConfiguration(conf.read_or_repository('aws_page'), cloud_gate.settings(),
                                               conf.read_or_repository('aws_target'), build_steps.read_step_names())
    page = configuration.page
    days = page.whole('machines.window_days')
    ratio_max = page.value('machines.ratio_max', conf.decimal)
    ratio_step = page.value('machines.ratio_step', conf.decimal)
    intervals, remainder = divmod(ratio_max - _RULE_OFF, ratio_step)
    if remainder:
        raise conf.Unusable('machines.ratio_max must lie on a slider step from 1.00')
    ratios = tuple(_RULE_OFF + step * ratio_step for step in range(int(intervals) + 1))
    end = datetime.combine(datetime.fromtimestamp(now, UTC).date(), time.min, UTC)
    start = end - timedelta(days=days)
    prices = _saved_prices(directory, now, days)
    try:
        reservations = {row.attempt: row for row in ledger.CostLedger(directory).read_snapshot().gate_reservations}
    except ledger.LedgerUnavailable:
        names = configuration.names
        return MachinesPageData(start, end, ratio_max, ratio_step, prices.read, tuple(
            WorkloadWithoutComparison(names.shown(mode), mode, f'The cost ledger for {names.shown(mode)} is unavailable.')
            for mode in _WORKLOADS))
    rows_by_workload: dict[str, list[dict[str, object]]] = defaultdict(list)
    window_end = end.timestamp()
    for row in ledger.recent_gate_launches(directory, start.timestamp()):
        if float(str(row['time'])) < window_end and (mode := _workload(row)):
            rows_by_workload[mode].append(row)
    home = configuration.account.text('region')
    deployed: dict[ledger.GateRateCandidate, run_cost.GateLaunchVariant] = {}
    for variant in run_cost.read_deployed_gate_variants(directory, configuration.gate.words('instance_types'), home):
        _ = deployed.setdefault(ledger.GateRateCandidate(variant.instance_type, variant.region), variant)
    workloads: list[MeasuredWorkload | WorkloadWithoutComparison] = []
    for mode in dict.fromkeys((*_WORKLOADS, *sorted(rows_by_workload))):
        rows = rows_by_workload[mode]
        workloads.append(_project_workload(configuration.names.shown(mode), mode, rows,
            _intervals(rows, reservations, home, start, end), prices, start, end, configuration, ratios, deployed))
    return MachinesPageData(start, end, ratio_max, ratio_step, prices.read, tuple(workloads))

"""Project saved AWS costs into the Builds report."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import ceil
import re
from typing import Literal, TypedDict
from zoneinfo import ZoneInfo

from scripts.aws_target import config, ledger, run_cost
from scripts.aws_target.aws_cli import AwsUnavailable, aws_error_code
from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.build_queue.snapshot_codec import parse_snapshot_value
from scripts.build_targets.target_config import ssh_target_state_directory
from scripts.lib import conf
from scripts.lib.user_zone import user_zone_name
from scripts.lib.build_steps import RAW_ID, read_step_names
from scripts.production import unit_names


class AmountPerRun(TypedDict):
    runs: int
    cost_usd: str
    per_run_usd: str


class JobCost(AmountPerRun):
    job: str | None


class HanaCost(AmountPerRun):
    idle_usd: str


class UnitCost(TypedDict):
    unit: str | None
    runs: int
    cost_usd: str


class RunCost(TypedDict):
    id: str
    start: int
    kind: Literal["gate", "hana"]
    job: str | None
    unit: str | None
    region: str
    zone: str | None
    machine: str
    mode: Literal["Spot", "On-Demand"]
    seconds: int
    cost_usd: str


class Day(TypedDict):
    day: str
    state: Literal["estimate", "billed", "final", "none"]
    amount_usd: str | None
    runs: int
    jobs: list[JobCost]
    hana: HanaCost | None
    units: list[UnitCost]
    unrecorded_compute_usd: str
    no_run_usd: str
    runs_list: list[RunCost]


class Limits(TypedDict):
    day_usd: str
    month_usd: str
    reserve_usd: str


class KeptItem(TypedDict):
    name: str
    month_usd: str


class KeptCosts(TypedDict):
    month_usd: str
    items: list[KeptItem]


class AwsCosts(TypedDict):
    zone: str
    read_at: int | None
    next_read_at: int | None
    read_error: str | None
    price_day: str | None
    records_start: str | None
    today: str
    limits: Limits
    days: list[Day]
    kept: KeptCosts | None


AWS_IDENTIFIER = re.compile(r"[0-9]{12}|(?:ami|i|sg|vpc|subnet|vol|snap|eni|eipalloc|lt)-[0-9a-f]{8,}",
                            re.IGNORECASE | re.ASCII)
LEGACY_JOB_WORDS = re.compile(r"\bgate\b|\btargeted test\b", re.IGNORECASE)
SPEND_READ_ERRORS = (OSError, ValueError, KeyError, TypeError, ArithmeticError)
BILL_ZONE = ZoneInfo("UTC")
CENT = Decimal("0.01")
REGIONAL_ZONE = re.compile(r"([a-z]{2}(?:-[a-z0-9]+)+-[0-9]+)[a-z]")
CARRIED_BILL_COSTS = "newest bill non-compute"
COST_READ = "GetCostAndUsage"
KEY_REFUSALS = frozenset({"UnrecognizedClientException", "InvalidClientTokenId", "InvalidAccessKeyId",
                          "ExpiredToken", "ExpiredTokenException", "SignatureDoesNotMatch"})


def empty_costs(instant: int) -> AwsCosts:
    return {"zone": user_zone_name(), "read_at": None, "next_read_at": None, "read_error": None, "price_day": None,
            "records_start": None, "today": bill_day(instant),
            "limits": {"day_usd": "0.00", "month_usd": "0.00", "reserve_usd": "0.00"}, "days": [], "kept": None}


def bill_day(instant: int) -> str:
    return datetime.fromtimestamp(instant, UTC).date().isoformat()


def usd(amount: Decimal) -> str:
    return f"{amount.quantize(CENT) + Decimal(0):.2f}"


def read_error(rows: Sequence[dict[str, object]], since: int) -> str | None:
    """Name why the newest AWS cost read after `since` failed.

    A run that starts reads the AWS costs first. When that read fails, the launch history keeps the AWS error.
    """
    newest_at, code = since, ""
    for row in rows:
        try:
            at = float(str(row["time"]))
        except SPEND_READ_ERRORS:
            continue
        refused = aws_error_code(AwsUnavailable(str(row.get("reason", ""))), operation=COST_READ)
        if refused and at > newest_at:
            newest_at, code = at, refused
    if not code:
        return None
    return "AWS refuses the key" if code in KEY_REFUSALS else f"AWS refuses the read ({code})"


def recorded_unit(value: object) -> str:
    """Accept a unit name free of private identifiers."""
    if (isinstance(value, str) and unit_names.NAME.fullmatch(value)
            and not RAW_ID.search(value) and not AWS_IDENTIFIER.search(value)):
        return value
    return ""


def job_names() -> dict[str, str]:
    names = read_step_names()
    steps = (key.removeprefix("names.") for key in names.settings if key.startswith("names."))
    shown = ((step, " ".join(names.shown(step).split())) for step in steps)
    return {step: name for step, name in shown if name and not LEGACY_JOB_WORDS.search(name)}


def recorded_job(value: object) -> StepJob | None:
    """Read a saved job with the queue decoder."""
    if value is None:
        return None
    try:
        job = parse_snapshot_value(value, StepJob | GateJobNotRecorded)
    except (ValueError, OverflowError):
        return None
    return job if isinstance(job, StepJob) else None


@dataclass(frozen=True)
class Run:
    attempt: str
    start: int
    job: str | None
    unit: str | None
    zone: str | None
    box: run_cost.FinishedGateRun

    @property
    def bill_key(self) -> run_cost.BilledKey:
        return self.box.instance_type, self.box.region, self.box.day, self.box.mode


def runs_by_day(state: ledger.LedgerEvidence, rows: Sequence[dict[str, object]], instant: int,
                names: dict[str, str], home: str) -> dict[str, list[Run]]:
    """Join launches to box lifetimes and split box time at UTC midnight."""
    launched: dict[str, dict[str, object]] = {}
    finished: dict[str, int] = {}
    for row in rows:
        attempt = row.get("attempt")
        if not isinstance(attempt, str) or row.get("result") not in ("launched", "finished"):
            continue
        try:
            at = int(float(str(row["time"])))
        except SPEND_READ_ERRORS:
            continue
        if at < 0:
            continue
        if row["result"] == "launched":
            _ = launched.setdefault(attempt, row)
        else:
            finished[attempt] = at

    reservations = {row.attempt: row for row in state.gate_reservations}
    days: dict[str, list[Run]] = defaultdict(list)
    for attempt in launched.keys() | reservations.keys():
        launch = launched.get(attempt, {})
        reservation = reservations.get(attempt)
        if (not launch and (reservation is None
                or not isinstance(reservation.state, (ledger.GateRunning, ledger.GateTerminated)))):
            continue
        if reservation is not None and isinstance(reservation.state, ledger.GateAbsent):
            continue
        start = int(float(str(launch["time"]))) if launch else reservation.at if reservation is not None else instant
        if start > instant:
            continue
        end = finished.get(attempt, instant)
        if reservation is not None and isinstance(reservation.state, ledger.GateTerminated):
            end = reservation.state.at
        elif reservation is None and attempt not in finished:
            end = start
        end = max(start, min(end, instant))
        size = str(launch.get("instance_type") or (reservation.instance_type if reservation is not None else ""))
        zone = str(launch.get("availability_zone") or "") or None
        zone_region = REGIONAL_ZONE.fullmatch(zone) if zone is not None else None
        region = str(launch.get("region") or (reservation.region if reservation is not None else "")
                     or (zone_region[1] if zone_region is not None else home))
        mode = str(launch.get("mode") or (reservation.mode if reservation is not None else ""))
        identity = recorded_job(launch.get("gate_job"))
        job = names.get(identity.step) if identity is not None else None
        unit = recorded_unit(launch.get("unit")) or recorded_unit(identity.unit if identity else None) or None
        first = date.fromisoformat(bill_day(start))
        last = date.fromisoformat(bill_day(max(start, end - 1)))
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            left, right = ledger.period_bounds(day, BILL_ZONE)
            seconds = max(0, min(end, right) - max(start, left))
            days[day.isoformat()].append(Run(attempt, start, job, unit, zone, run_cost.FinishedGateRun(
                size, region, mode, day.isoformat(), Decimal(0), Decimal(seconds))))
    return days


def fixed_costs(state: ledger.LedgerEvidence, instant: int) -> KeptCosts | None:
    """Read the current monthly rates from the resource records."""
    inventory = max((row for row in state.inventories if row.at <= instant),
                    key=lambda row: row.at, default=None)
    items: list[KeptItem] = []
    storage = next((row for row in state.hana_storage if row.at <= instant
                    and isinstance(row.state, ledger.HanaExposureOpen)), None)
    if storage is not None:
        items.append({"name": "Hana data disk", "month_usd": usd(storage.usd_per_month)})
    if inventory is not None:
        other = Decimal(0)
        for resource in inventory.resources:
            if resource.name == "gate-shared":
                items.append({"name": "shared storage for AWS runs", "month_usd": usd(resource.usd_per_month)})
            else:
                other += resource.usd_per_month
        if other:
            items.append({"name": "Other kept resources", "month_usd": usd(other)})
    return {"month_usd": usd(sum((Decimal(item["month_usd"]) for item in items), Decimal(0))),
            "items": items} if inventory is not None or items else None


def daily_charge_totals(days: Sequence[ledger.DailySpend], zone: ZoneInfo) -> dict[str, Decimal]:
    """Use the guard's charge intervals and keep each UTC bill day intact."""
    return {day.day: sum(ledger.spending_periods(day.intervals, zone)[0].values(), Decimal(0))
            for day in days if day.intervals}


type HanaMachine = tuple[str, str]
type HanaCompute = dict[HanaMachine, list[tuple[str, ledger.ChargedInterval]]]


def billed_run_costs(bill: ledger.ChargeRead, runs: Sequence[Run],
                     hana_spot: dict[HanaMachine, Decimal]) -> tuple[dict[str, Decimal], dict[HanaMachine, Decimal]]:
    prices = run_cost.bills_from_readings((bill,))
    # Set aside the kept machine's share of the billed Spot cost.
    hana_cost: dict[HanaMachine, Decimal] = {}
    for (size, region, day), (amount, hours) in prices.items():
        machine = (size, region)
        if hours > 0 and amount > 0 and machine in hana_spot:
            kept = min(amount, hana_spot[machine])
            prices[size, region, day] = amount - kept, hours
            hana_cost[machine] = kept
    allocations = run_cost.billed_allocations(prices, [run.box for run in runs])
    seconds: dict[run_cost.BilledKey, Decimal] = defaultdict(Decimal)
    for run in runs:
        seconds[run.bill_key] += run.box.box_s
    costs = {run.attempt: allocations[run.bill_key] * run.box.box_s / seconds[run.bill_key]
             for run in runs if run.bill_key in allocations}
    return costs, hana_cost


def hana_compute_by_day(state: ledger.LedgerEvidence, settings: config.SpendingSettings,
                        readings: Sequence[ledger.ChargeRead], instant: int) -> dict[str, HanaCompute]:
    result: dict[str, HanaCompute] = defaultdict(lambda: defaultdict(list))
    for exposure in state.hana_compute:
        machine = (exposure.instance_type, exposure.region)
        evidence = replace(state, hana_compute=[exposure], hana_storage=[])
        reference = f"hana_compute:{exposure.generation}"
        for charge in ledger.hana_intervals(evidence, instant, settings.box_minimum_seconds, readings=readings):
            result[bill_day(charge.start)][machine].append((reference, charge))
    return result


@dataclass(frozen=True)
class HanaRun:
    attempt: str
    start: int
    seconds: int
    machine: str
    region: str
    begin: Decimal
    end: Decimal
    references: tuple[str, ...]


def hana_runs_by_day(state: ledger.LedgerEvidence, instant: int) -> dict[str, list[HanaRun]]:
    """Split the measured time at midnight and keep each attempt's identity."""
    days: dict[str, list[HanaRun]] = defaultdict(list)
    for attempt in state.hana_attempts:
        if attempt.finished_at > instant:
            continue
        started_at = Decimal(str(attempt.started_at))
        finished_at = Decimal(str(attempt.finished_at))
        start = int(started_at)
        references = tuple(ref for ref in attempt.ledger_references if ref.startswith("hana_compute:"))
        exposure = max((row for row in state.hana_compute if row.at <= attempt.started_at
                        and (not references or f"hana_compute:{row.generation}" in references)
                        and (not isinstance(row.state, ledger.HanaExposureClosed) or row.state.at > attempt.started_at)),
                       key=lambda row: row.at, default=None)
        first = date.fromisoformat(bill_day(start))
        last = date.fromisoformat(bill_day(max(start, ceil(finished_at) - 1)))
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            left, right = ledger.period_bounds(day, BILL_ZONE)
            begin, end = max(started_at, Decimal(left)), min(finished_at, Decimal(right))
            seconds = int(end - started_at) - int(begin - started_at)
            days[day.isoformat()].append(HanaRun(attempt.key.attempt, start, seconds,
                exposure.instance_type if exposure else "", exposure.region if exposure else "", begin, end, references))
    return days


def hana_attempt_costs(runs: Sequence[HanaRun], compute: HanaCompute, priced: dict[HanaMachine, Decimal],
                       paid: dict[HanaMachine, Decimal]) -> dict[str, Decimal]:
    """Share the cost of covered time among the attempts that used it."""
    costs: dict[str, Decimal] = defaultdict(Decimal)
    for machine, amount in paid.items():
        if not amount:
            continue
        fraction = amount / priced[machine]
        for reference, charge in compute[machine]:
            if charge.start == charge.end:
                continue
            overlaps: list[tuple[Decimal, Decimal, str]] = []
            for run in runs:
                if run.references and reference not in run.references:
                    continue
                begin, end = max(run.begin, Decimal(charge.start)), min(run.end, Decimal(charge.end))
                if begin < end:
                    overlaps.append((begin, end, run.attempt))
            if not overlaps:
                continue
            covered = used = Decimal(0)
            previous_end = Decimal(charge.start)
            for begin, end, _ in sorted(overlaps):
                covered += max(Decimal(0), end - max(begin, previous_end))
                used += end - begin
                previous_end = max(previous_end, end)
            cost = charge.usd * fraction * covered / Decimal(charge.end - charge.start)
            for begin, end, attempt in overlaps:
                costs[attempt] += cost * (end - begin) / used
    return costs


def per_run(cost: Decimal, count: int) -> AmountPerRun:
    return {"runs": count, "cost_usd": usd(cost), "per_run_usd": usd(cost / count if count else Decimal(0))}


def day_row(day: str, amount: Decimal | None, compute: Decimal, bill: ledger.ChargeRead | None, runs: list[Run],
            costs: dict[str, Decimal], names: dict[str, str], hana_cost: Decimal,
            hana_runs: Sequence[HanaRun], hana_costs: dict[str, Decimal]) -> Day:
    by_job: dict[str | None, list[Decimal]] = defaultdict(list)
    by_unit: dict[str | None, list[Decimal]] = defaultdict(list)
    details: list[RunCost] = []
    total = previous = Decimal(0)
    for run in sorted(runs, key=lambda row: (row.start, row.attempt)):
        total += costs.get(run.attempt, Decimal(0))
        rounded = total.quantize(CENT)
        cost = rounded - previous
        previous = rounded
        by_job[run.job].append(cost)
        by_unit[run.unit].append(cost)
        details.append({"id": run.attempt, "start": run.start, "kind": "gate", "job": run.job, "unit": run.unit,
                        "region": run.box.region, "zone": run.zone, "machine": run.box.instance_type, "mode": "Spot",
                        "seconds": int(run.box.box_s), "cost_usd": usd(cost)})
    has_hana = bool(hana_cost or hana_runs)
    rounded_total = (total + hana_cost).quantize(CENT)
    hana_total = rounded_total - previous
    for run in sorted(hana_runs, key=lambda row: (row.start, row.attempt)):
        total += hana_costs.get(run.attempt, Decimal(0))
        rounded = min(rounded_total, total.quantize(CENT))
        cost = rounded - previous
        previous = rounded
        by_unit[None].append(cost)
        details.append({"id": run.attempt, "start": run.start, "kind": "hana", "job": None, "unit": None,
                        "region": run.region, "zone": None, "machine": run.machine, "mode": "Spot",
                        "seconds": run.seconds, "cost_usd": usd(cost)})
    idle = rounded_total - previous
    hana: HanaCost | None = None
    if has_hana:
        hana = {**per_run(hana_total - idle, len(hana_runs)), "cost_usd": usd(hana_total), "idle_usd": usd(idle)}
    if idle:
        _ = by_unit[None]
    jobs: list[JobCost] = [{"job": job, **per_run(sum(by_job[job], Decimal(0)), len(by_job[job]))}
                          for job in dict.fromkeys((*names.values(), None)) if job in by_job]
    units: list[UnitCost] = [
        {"unit": unit, "runs": len(by_unit[unit]),
         "cost_usd": usd(sum(by_unit[unit], Decimal(0)) + (idle if unit is None else 0))}
        for unit in sorted(by_unit, key=lambda name: (name is None, name or ""))]
    rounded_compute = compute.quantize(CENT)
    unrecorded_compute = rounded_compute - rounded_total
    no_run = (amount.quantize(CENT) if amount is not None else Decimal(0)) - rounded_compute
    state: Literal["estimate", "billed", "final", "none"]
    if bill is not None:
        state = "billed" if bill.provisional else "final"
    else:
        state = "estimate" if amount is not None else "none"
    return {"day": day, "state": state, "amount_usd": usd(amount) if amount is not None else None,
            "runs": len(details), "jobs": jobs, "hana": hana, "units": units,
            "unrecorded_compute_usd": usd(unrecorded_compute), "no_run_usd": usd(no_run),
            "runs_list": sorted(details, key=lambda row: row["start"])}


def day_rows(state: ledger.LedgerEvidence, days: Sequence[ledger.DailySpend], readings: Sequence[ledger.ChargeRead],
             launches: Sequence[dict[str, object]], settings: config.SpendingSettings | None,
             names: dict[str, str], home: str, instant: int) -> list[Day]:
    bills = {row.day: row for row in readings if row.account_bill and row.period_end <= instant}
    zone = settings.accounting_zone if settings is not None else BILL_ZONE
    amounts = daily_charge_totals(days, zone)
    spends = {row.day: row for row in days}
    runs = runs_by_day(state, launches, instant, names, home)
    hana_compute = hana_compute_by_day(state, settings, readings, instant) if settings is not None else {}
    hana_runs = hana_runs_by_day(state, instant)
    first_bill = min(bills, default=bill_day(instant))
    newest_bill = max((bill for bill in bills.values() if ledger.is_billed_day(bill)),
                      key=lambda bill: bill.day, default=None)
    carried_compute = sum((row.usd for row in newest_bill.usage_types if "BoxUsage:" in row.usage_type),
                          Decimal(0)) if newest_bill is not None else Decimal(0)
    recorded_days = bills.keys() | runs.keys() | hana_runs.keys()
    recorded_days.update(day for day, spend in spends.items() if day in amounts and
                         (day >= first_bill or any(row.source != CARRIED_BILL_COSTS for row in spend.intervals)))
    if not recorded_days:
        return []

    current = datetime.fromtimestamp(instant, UTC).date()
    first = date.fromisoformat(min(recorded_days))
    result: list[Day] = []
    for offset in range((current - first).days + 1):
        day = (current - timedelta(days=offset)).isoformat()
        spend = spends.get(day)
        bill = bills.get(day)
        active = runs.get(day, [])
        amount = amounts.get(day) if day in recorded_days else None
        compute = hana_compute.get(day, {})
        hana_spot = {machine: sum((charge.usd for _, charge in intervals), Decimal(0))
                     for machine, intervals in compute.items()}
        hana_paid = hana_spot if spend is not None else {}
        costs: dict[str, Decimal] = defaultdict(Decimal)
        compute_total = Decimal(0)
        if bill is not None:
            amount = bill.actual_usd
            costs, hana_paid = billed_run_costs(bill, active, hana_spot)
            compute_total = sum((row.usd for row in bill.usage_types
                                 if "SpotUsage:" in row.usage_type or "BoxUsage:" in row.usage_type), Decimal(0))
        elif spend is not None:
            for charge in spend.intervals:
                if charge.kind == "instance":
                    compute_total += charge.usd
                if charge.source == CARRIED_BILL_COSTS:
                    compute_total += carried_compute
                if charge.source.startswith("gate:"):
                    costs[charge.source.removeprefix("gate:")] += charge.usd
        attempts = hana_runs.get(day, [])
        hana_costs = hana_attempt_costs(attempts, compute, hana_spot, hana_paid)
        result.append(day_row(day, amount, compute_total, bill, active, costs, names, sum(hana_paid.values(), Decimal(0)),
                              attempts, hana_costs))
    return result


def read_limits(configuration: conf.ConfigRead) -> Limits:
    def limit(key: str) -> str:
        value = configuration.origin(key, config.money)
        return usd(value.value) if isinstance(value, conf.UsableLiveValue) else "0.00"

    return {"day_usd": limit("daily_limit_usd"), "month_usd": limit("monthly_limit_usd"),
            "reserve_usd": limit("limit_reserve_usd")}


def read_saved_costs(instant: int) -> AwsCosts:
    """Build the cost feed from local records during a report refresh."""
    result = empty_costs(instant)
    book = ledger.CostLedger(ssh_target_state_directory("aws"))
    configuration = conf.read(config.account_settings_path())
    result["limits"] = read_limits(configuration)
    try:
        settings = config.read_settings(configuration)
    except SPEND_READ_ERRORS:
        settings = None
    try:
        readings = book.read_bills()
    except SPEND_READ_ERRORS:
        readings = ()
    try:
        launches = book.launches()
    except SPEND_READ_ERRORS:
        launches = ()
    try:
        state = book.read_snapshot()
    except SPEND_READ_ERRORS:
        state = ledger.LedgerEvidence()
    result["read_at"] = state.charge_refresh_at or None
    if state.charge_refresh_at and settings is not None:
        result["next_read_at"] = state.charge_refresh_at + settings.cost_explorer_interval_seconds
    result["read_error"] = read_error(launches, state.charge_refresh_at)
    result["kept"] = fixed_costs(state, instant)
    try:
        names = job_names()
    except SPEND_READ_ERRORS:
        names = {}
    try:
        home = configuration.text("region")
    except SPEND_READ_ERRORS:
        home = ""

    days: tuple[ledger.DailySpend, ...] = ()
    if settings is not None and state.inventories:
        try:
            state = book.priced_snapshot(instant)
            days = ledger.gate_spend_days(state, settings, readings, instant)
        except SPEND_READ_ERRORS:
            pass
    result["days"] = day_rows(state, days, readings, launches, settings, names, home, instant)
    result["records_start"] = result["days"][-1]["day"] if result["days"] else None
    result["price_day"] = next((day["day"] for day in result["days"] if day["state"] in ("billed", "final")), None)
    return result

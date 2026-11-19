"""Project saved AWS costs into the public Builds report."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
import re
from typing import TypedDict
from zoneinfo import ZoneInfo

from scripts.aws_target import config, ledger, run_cost
from scripts.build_targets.target_config import ssh_target_state_directory
from scripts.lib import conf
from scripts.lib.build_steps import RAW_ID
from scripts.production import unit_names


class BilledCharge(TypedDict):
    amount: str
    provisional: bool


class CostAgainstLimit(TypedDict):
    amount: str | None
    limit: str | None


class UnitGateCosts(TypedDict):
    unit: str
    merge_gates: str | None
    targeted_tests: str | None


class GateCosts(TypedDict):
    merge_gates: str | None
    targeted_tests: str | None
    per_merge_gate: str | None
    units: list[UnitGateCosts]


class FixedCosts(TypedDict):
    gate_image: str
    other: str
    total: str


class AwsCosts(TypedDict):
    accounting_zone: str | None
    yesterday: BilledCharge | None
    today: CostAgainstLimit
    month: CostAgainstLimit
    gates: GateCosts | None
    fixed: FixedCosts | None


NO_COSTS: AwsCosts = {"accounting_zone": None, "yesterday": None, "today": {"amount": None, "limit": None},
                     "month": {"amount": None, "limit": None}, "gates": None, "fixed": None}
AWS_IDENTIFIER = re.compile(r"[0-9]{12}|(?:ami|i|sg|vpc|subnet|vol|snap|eni|eipalloc|lt)-[0-9a-f]{8,}",
                            re.IGNORECASE | re.ASCII)
SPEND_READ_ERRORS = (OSError, ValueError, KeyError, TypeError, ArithmeticError)


def usd(amount: Decimal) -> str:
    return f"${amount:.2f}"


def recorded_unit(value: object) -> str:
    """Accept a unit name with no private identifier."""
    if (isinstance(value, str) and unit_names.NAME.fullmatch(value)
            and not RAW_ID.search(value) and not AWS_IDENTIFIER.search(value)):
        return value
    return ""


def gate_costs(book: ledger.CostLedger, bills: list[ledger.ChargeRead], start: datetime,
               now: datetime) -> GateCosts | None:
    """Divide the billed Spot cost by recorded box time in each bill group."""
    if not bills:
        return None
    prices = run_cost.bills_from_readings(bills)
    rows = book.launches()
    units: dict[str, str] = {}
    for row in rows:
        attempt = row.get("attempt")
        unit = recorded_unit(row.get("unit"))
        if isinstance(attempt, str) and unit:
            units[attempt] = unit
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        attempt = row.get("attempt")
        unit = units.get(attempt, "") if isinstance(attempt, str) else ""
        grouped[unit].append(row)
    home = conf.read_or_repository("aws_target").text("region")
    runs = [(unit, run) for unit, records in grouped.items()
            for run in run_cost.runs_from_rows(records, start, now, home).runs if run.box_s > 0]
    bill_start = datetime.fromtimestamp(min(bill.period_start for bill in bills), UTC)
    # Keep the whole UTC bill day in the allocation when the accounting month splits it.
    billed_runs = [run for _, run in runs]
    if bill_start < start:
        billed_runs.extend(run for run in run_cost.runs_from_rows(rows, bill_start, start, home).runs if run.box_s > 0)
    allocations = run_cost.billed_allocations(prices, billed_runs)
    seconds: dict[run_cost.BilledKey, Decimal] = defaultdict(Decimal)
    for run in billed_runs:
        seconds[run.instance_type, run.region, run.day, run.mode] += run.box_s

    totals: dict[str, Decimal] = defaultdict(Decimal)
    per_unit: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    merge_count = 0
    for unit, run in runs:
        key = (run.instance_type, run.region, run.day, run.mode)
        if key not in allocations or run.mode not in run_cost.EVERY_KIND:
            continue
        cost = allocations[key] * run.box_s / seconds[key]
        totals[run.mode] += cost
        per_unit[unit][run.mode] += cost
        merge_count += int(run.mode == "merge-gate")
    if not totals:
        return None

    def amount(costs: dict[str, Decimal], kind: str) -> str | None:
        return usd(costs[kind]) if kind in costs else None

    unit_rows: list[UnitGateCosts] = [
        {"unit": unit or "Unit not recorded", "merge_gates": amount(costs, "merge-gate"),
         "targeted_tests": amount(costs, "targeted-test")}
        for unit, costs in sorted(per_unit.items(), key=lambda pair: (-sum(pair[1].values()), pair[0]))]
    return {"merge_gates": amount(totals, "merge-gate"), "targeted_tests": amount(totals, "targeted-test"),
            "per_merge_gate": usd(totals["merge-gate"] / merge_count) if merge_count else None, "units": unit_rows}


def fixed_costs(book: ledger.CostLedger, instant: int) -> FixedCosts | None:
    """Show monthly rates from the latest saved inventory."""
    inventory = max((row for row in book.read_snapshot().inventories if row.at <= instant),
                    key=lambda row: row.at, default=None)
    if inventory is None:
        return None
    total = sum((resource.usd_per_month for resource in inventory.resources), Decimal(0))
    image = sum((resource.usd_per_month for resource in inventory.resources if resource.name == "gate-image"), Decimal(0))
    return {"gate_image": usd(image), "other": usd(total - image), "total": usd(total)}


def daily_charge_totals(book: ledger.CostLedger, settings: config.SpendingSettings | config.MeasurementUnavailable,
                        readings: tuple[ledger.ChargeRead, ...], bills: dict[str, ledger.ChargeRead], instant: int,
                        zone: ZoneInfo) -> dict[str, Decimal]:
    if isinstance(settings, config.SpendingSettings):
        try:
            days = ledger.gate_spend_days(book.priced_snapshot(instant), settings, readings, instant)
            intervals = tuple(charge for day in days for charge in day.intervals)
            return ledger.spending_periods(intervals, settings.accounting_zone)[0]
        except SPEND_READ_ERRORS:
            pass
    # A saved bill still shows spend when the ledger cannot price the current period.
    billed = tuple(ledger.ChargedInterval(bill.period_start, bill.period_end, bill.actual_usd,
                                        "AWS bill", "reconciliation") for bill in bills.values())
    return ledger.spending_periods(billed, zone)[0]


def read_saved_costs(instant: int) -> AwsCosts:
    """Read each saved cost source independently of the queue and the index."""
    book = ledger.CostLedger(ssh_target_state_directory("aws"))
    daily_limit = monthly_limit = None
    configuration = conf.read(config.account_settings_path())
    try:
        settings = config.read_settings(configuration)
        accounting_zone = settings.accounting_zone
        daily_limit, monthly_limit = usd(settings.daily_limit_usd), usd(settings.monthly_limit_usd)
    except SPEND_READ_ERRORS as error:
        settings = config.MeasurementUnavailable(str(error))
        try:
            current_zone = configuration.origin("accounting_zone", ZoneInfo)
            accounting_zone = current_zone.value if isinstance(current_zone, conf.UsableLiveValue) else None
        except (KeyError, ValueError):
            accounting_zone = None
    zone = accounting_zone or ZoneInfo("UTC")
    now = datetime.fromtimestamp(instant, zone)
    today = now.date()
    first_of_month = today.replace(day=1)
    month_start, _ = ledger.period_bounds(first_of_month, zone)

    try:
        readings = book.read_bills()
    except SPEND_READ_ERRORS:
        readings = ()
    bills: dict[str, ledger.ChargeRead] = {}
    for row in readings:
        if row.account_bill and row.period_end <= instant and (row.day not in bills or row.at >= bills[row.day].at):
            bills[row.day] = row
    bill = bills.get(ledger.charge_period_day(instant).isoformat())
    billed: BilledCharge | None = ({"amount": usd(bill.actual_usd), "provisional": bill.provisional}
                                   if bill is not None else None)

    by_day = daily_charge_totals(book, settings, readings, bills, instant, zone)
    today_key = today.isoformat()
    month_start_key = first_of_month.isoformat()
    day_amount = by_day.get(today_key)
    month_amounts = [amount for day, amount in by_day.items() if month_start_key <= day <= today_key]
    month_amount = sum(month_amounts, Decimal(0)) if month_amounts else None
    try:
        gates = gate_costs(book, [row for row in bills.values() if row.period_end > month_start],
                          datetime.fromtimestamp(month_start, zone), now)
    except SPEND_READ_ERRORS:
        gates = None
    try:
        fixed = fixed_costs(book, instant)
    except SPEND_READ_ERRORS:
        fixed = None
    return {"accounting_zone": str(accounting_zone) if accounting_zone is not None else None, "yesterday": billed,
            "today": {"amount": usd(day_amount) if day_amount is not None else None, "limit": daily_limit},
            "month": {"amount": usd(month_amount) if month_amount is not None else None, "limit": monthly_limit},
            "gates": gates, "fixed": fixed}

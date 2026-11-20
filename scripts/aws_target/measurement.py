"""Read local AWS measurements and reconcile scheduled charge replies."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.aws_target import ledger
from scripts.aws_target.ledger import ChargeRead as ChargeRead, read_charge as read_charge, charge_record as charge_record
from scripts.aws_target.config import MeasurementUnavailable as MeasurementUnavailable, SpendingSettings, read_settings
from scripts.lib.atomic_file import create_text_atomic
from scripts.lib import conf
from scripts.mac_test import mac_test


@dataclass(frozen=True)
class AwsStatusMeasurement:
    permission: ledger.AdmissionPermission
    costs: ledger.CostEstimate | ledger.CostEvidenceUnavailable
    events: tuple[ledger.LimitEvent, ...]
    monthly_limit_usd: Decimal | MeasurementUnavailable


@dataclass(frozen=True)
class AwsMeasurement(AwsStatusMeasurement):
    charge_reads: tuple[ChargeRead, ...]
    go_live_at: int | MeasurementUnavailable


@dataclass(frozen=True)
class MeasurementInputs:
    state: ledger.LedgerEvidence
    settings: SpendingSettings | MeasurementUnavailable


def read_inputs(configuration: Path | conf.ConfigRead | ledger.SpendEvidenceAt, directory: Path) -> MeasurementInputs:
    if isinstance(configuration, ledger.SpendEvidenceAt):
        return MeasurementInputs(configuration.state, configuration.settings)
    book = ledger.CostLedger(directory)
    try:
        state = book.read_snapshot()
    except ledger.LedgerUnavailable:
        if book.path.exists():
            raise
        state = ledger.LedgerEvidence()
    try:
        settings: SpendingSettings | MeasurementUnavailable = read_settings(configuration)
    except (OSError, ValueError, ArithmeticError) as error:
        settings = MeasurementUnavailable(str(error))
    return MeasurementInputs(state, settings)


def record_go_live(directory: Path, now: int) -> None:
    """Keep the first permitted running observation as the comparison boundary."""
    path = directory / 'go-live.json'
    if not path.exists():
        _ = create_text_atomic(path, json.dumps({'at': now}) + '\n')


def read_status_measurement(configuration: Path | conf.ConfigRead | ledger.SpendEvidenceAt,
                            directory: Path, now: int, *,
                            inputs: MeasurementInputs | None = None) -> AwsStatusMeasurement | MeasurementUnavailable:
    try:
        source = read_inputs(configuration, directory) if inputs is None else inputs
        state, settings = source.state, source.settings
        permission = ledger.decode_permission(state.decision)
        try:
            if isinstance(settings, MeasurementUnavailable):
                raise ValueError(settings.reason)
            costs: ledger.CostEstimate | ledger.CostEvidenceUnavailable = ledger.costs_from_evidence(state, settings, now)
        except (OSError, ValueError, ArithmeticError) as error:
            costs = ledger.CostEvidenceUnavailable(str(error))
            permission = ledger.PermissionUnavailable(permission.at, str(error), permission.evidence)
        cap = (MeasurementUnavailable(settings.reason) if isinstance(settings, MeasurementUnavailable)
               else settings.monthly_limit_usd)
        return AwsStatusMeasurement(permission, costs, tuple(state.events), cap)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        return MeasurementUnavailable(str(error))


def read_measurement(configuration: Path | conf.ConfigRead | ledger.SpendEvidenceAt,
                     directory: Path, now: int, *,
                     inputs: MeasurementInputs | None = None) -> AwsMeasurement | MeasurementUnavailable:
    status = read_status_measurement(configuration, directory, now, inputs=inputs)
    if isinstance(status, MeasurementUnavailable):
        return status
    try:
        go_live_row = mac_test.read_json_object(directory / 'go-live.json')
        go_live = ledger.instant(go_live_row, 'at') if go_live_row is not None else MeasurementUnavailable('go-live time is not recorded')
        readings = (configuration.validated_bills if isinstance(configuration, ledger.SpendEvidenceAt)
                    else ledger.CostLedger(directory).validated_bills(now))
        return AwsMeasurement(status.permission, status.costs, status.events,
                              status.monthly_limit_usd, readings, go_live)
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        return MeasurementUnavailable(str(error))


def charge_period_day(_settings: SpendingSettings, now: int) -> date:
    return ledger.charge_period_day(now)


def read_charges(settings_path: Path, directory: Path, now: int,
                 fetch: Callable[[int, int], Sequence[ChargeRead]]) -> ChargeRead | MeasurementUnavailable:
    """Read charges through the ledger's schedule and recovery lock."""
    return ledger.CostLedger(directory).refresh_charges(settings_path, now, fetch)


def number(value: object) -> Decimal:
    if isinstance(value, int | float | str) and not isinstance(value, bool):
        return conf.decimal(str(value))
    raise ValueError('expected a finite number')


@dataclass(frozen=True)
class DailyUsage:
    uptime_s: int
    usd: Decimal


def accounting_day_bounds(day: date, now: int, zone: ZoneInfo) -> tuple[int, int]:
    """Return accounting day bounds up to the report time."""
    start, end = ledger.period_bounds(day, zone)
    return start, min(now, end)


def daily_usage(settings_path: Path, directory: Path, day: str, now: int) -> DailyUsage | MeasurementUnavailable:
    book = ledger.CostLedger(directory)
    try:
        inputs = read_inputs(settings_path, directory)
        return daily_usage_from(inputs, book, day, now, book.refused_bill_records(now))
    except (OSError, ValueError, ArithmeticError) as error:
        return MeasurementUnavailable(str(error))


def daily_usage_from(source: MeasurementInputs, book: ledger.CostLedger, day: str, now: int,
                     refused: Sequence[ledger.RefusedBillRecord]) -> DailyUsage | MeasurementUnavailable:
    try:
        if isinstance(source.settings, MeasurementUnavailable):
            return source.settings
        accounting_day = date.fromisoformat(day)
        start, end = accounting_day_bounds(accounting_day, now, source.settings.accounting_zone)
        if end < start:
            return MeasurementUnavailable('report day is in the future')
        costs = ledger.costs_from_evidence(source.state, source.settings, end, window=accounting_day)
        uptime = sum(row.confirmed_uptime_s for row in costs.intervals)
        usd = ledger.period_cost(costs.intervals, start, end)
        first_utc_day = datetime.fromtimestamp(start, UTC).date()
        last_utc_day = datetime.fromtimestamp(max(start, end - 1), UTC).date()
        estimate_days = {day.isoformat(): day for record in refused for day in record.days
                         if first_utc_day <= day <= last_utc_day}
        use_spend_days = bool(source.state.gate_reservations or book.validated_bills(now))
        if use_spend_days or estimate_days:
            spends = book.spend_days(source.settings, now, estimate_days=tuple(estimate_days.values()))
            if use_spend_days:
                usd = ledger.period_cost((charge for spend in spends for charge in spend.intervals), start, end)
            else:
                # A local accounting day can overlap two UTC days.
                for spend in spends:
                    if (estimate_day := estimate_days.get(spend.day)) is None:
                        continue
                    left, right = ledger.period_bounds(estimate_day, ZoneInfo('UTC'))
                    left, right = max(left, start), min(right, end)
                    usd += (ledger.period_cost(spend.intervals, left, right)
                            - ledger.period_cost(costs.intervals, left, right))
        return DailyUsage(uptime, usd)
    except (OSError, ValueError, ArithmeticError) as error:
        return MeasurementUnavailable(str(error))

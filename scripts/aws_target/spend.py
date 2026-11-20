"""The AWS spend per day and per month from the local cost ledger, without an AWS call.

The buildlog report and the AWS page both read the spend here, so both show the same numbers.
"""
from __future__ import annotations

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from scripts.aws_target import config as aws_config, ledger, measurement


@dataclass(frozen=True)
class Spend:
    # Include bills and shown estimates in reverse date order.
    days: tuple[ledger.DailySpend, ...]
    daily_limit: Decimal
    monthly_limit: Decimal
    refused_bills: tuple[ledger.RefusedBillRecord, ...] = ()


NO_SPEND = Spend((), Decimal(0), Decimal(0))


@dataclass(frozen=True)
class MonthSpend:
    to_date: Decimal
    forecast: Decimal
    # The average spend of the latest billed days.
    pace: Decimal
    billed_days: int
    # The 32-CPU gate compute share of the spend on those billed days.
    gate_share: Decimal


def spend_from(inputs: measurement.MeasurementInputs, snapshot: measurement.AwsMeasurement | aws_config.MeasurementUnavailable,
               book: ledger.CostLedger, instant: int, last_day: str,
               refused: tuple[ledger.RefusedBillRecord, ...]) -> Spend | aws_config.MeasurementUnavailable:
    """The shown spend days from measurement inputs that the caller already read."""
    if isinstance(snapshot, aws_config.MeasurementUnavailable):
        return snapshot
    if isinstance(snapshot.costs, ledger.CostEvidenceUnavailable):
        return aws_config.MeasurementUnavailable(snapshot.costs.reason)
    if isinstance(inputs.settings, aws_config.MeasurementUnavailable):
        return inputs.settings
    refused_days = {day.isoformat(): day for record in refused for day in record.days
                    if day.isoformat() <= last_day}
    spends = book.spend_days(inputs.settings, instant, estimate_days=tuple(refused_days.values()))
    newest_bill_day = max((spend.day for spend in spends if spend.source == 'AWS bill'), default='')

    def shown(spend: ledger.DailySpend) -> bool:
        if spend.source == 'AWS bill' or newest_bill_day < spend.day <= last_day:
            return True
        return spend.day in refused_days and any(
            charge.usd > 0 and charge.kind != 'explorer' and charge.source != 'newest bill non-compute'
            for charge in spend.intervals)

    days = sorted((spend for spend in spends if shown(spend)),
                  key=lambda spend: spend.day, reverse=True)
    return Spend(tuple(days), inputs.settings.daily_limit_usd, inputs.settings.monthly_limit_usd, refused)


def read_spend(settings_path: Path, directory: Path, instant: int, last_day: str) -> Spend | aws_config.MeasurementUnavailable:
    """Read the ledger and return the shown spend days through the last day."""
    try:
        book = ledger.CostLedger(directory)
        inputs = measurement.read_inputs(settings_path, directory)
        snapshot = measurement.read_measurement(settings_path, directory, instant, inputs=inputs)
        return spend_from(inputs, snapshot, book, instant, last_day, book.refused_bill_records(instant))
    except (OSError, ValueError, ArithmeticError) as error:
        return aws_config.MeasurementUnavailable(str(error))


def month_spend(days: Sequence[ledger.DailySpend], today: date, pace_days: int) -> MonthSpend | None:
    """Month to date, and the month forecast from the average of the latest billed days."""
    month = [spend for spend in days if spend.day.startswith(f'{today:%Y-%m}')]
    billed = sorted((spend for spend in days if spend.source == 'AWS bill'), key=lambda spend: spend.day)[-pace_days:]
    billed_usd = sum((spend.usd for spend in billed), Decimal(0))
    if not month or not billed or billed_usd <= 0:
        return None
    pace = billed_usd / len(billed)
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    forecast = sum((spend.usd for spend in month if spend.day < today.isoformat()), Decimal(0)) + pace * days_left
    gates = sum((group.usd for spend in billed for group in spend.groups if group.group == ledger.SPEND_GROUPS[0]), Decimal(0))
    return MonthSpend(sum((spend.usd for spend in month), Decimal(0)), forecast, pace, len(billed), gates / billed_usd)


REPORT_ATTEMPTS = 3


@dataclass(frozen=True)
class UtcSpendReport:
    evidence: ledger.SpendEvidenceAt
    account: ledger.UtcAccountSpend | ledger.SpendEvidenceUnavailable


def read_utc_spend(directory: Path, instant: int, *, settings_path: Path | None = None,
                   purpose: ledger.SpendCapturePurpose = ledger.SpendCapturePurpose.REPORT
                   ) -> UtcSpendReport | ledger.SpendEvidenceUnavailable:
    """Read and evaluate one revision for both reports."""
    evidence = ledger.capture_spend_evidence(directory, instant, settings_path=settings_path, purpose=purpose)
    if isinstance(evidence, ledger.SpendEvidenceUnavailable):
        return evidence
    return UtcSpendReport(evidence, ledger.utc_account_spend(evidence))


def utc_day_label(day: ledger.UtcDaySpend) -> str:
    if isinstance(day.source, ledger.SettledWatchAmount):
        return 'accepted bill (AWS provisional)' if day.source.bill.provisional else 'accepted bill'
    if isinstance(day.source, ledger.UnsettledAwsFloor):
        return 'unsettled AWS amount so far'
    return 'estimate'


def utc_month_to_date(account: ledger.UtcAccountSpend, today: date) -> ledger.UtcMonthSpend:
    month_start = today.replace(day=1)
    return ledger.UtcMonthSpend(month_start, tuple(day for day in account.days if month_start <= day.day <= today))


@dataclass(frozen=True)
class UtcMonthForecast:
    forecast: Decimal
    pace: Decimal
    accepted_days: int
    gate_share: Decimal

    def display_line(self) -> str:
        days = 'day' if self.accepted_days == 1 else 'days'
        return (f'Month forecast: ${self.forecast:.2f} at ${self.pace:.2f} a day. '
                f'The pace uses {self.accepted_days} accepted bill {days}.')


@dataclass(frozen=True)
class NoAcceptedBillPace:
    """No accepted bills supply a pace for the forecast."""


def utc_month_forecast(account: ledger.UtcAccountSpend, today: date, pace_days: int
                      ) -> UtcMonthForecast | NoAcceptedBillPace:
    accepted = sorted((day for day in account.days if isinstance(day, ledger.UtcDaySpend)
                       and isinstance(day.source, ledger.SettledWatchAmount) and day.day <= today),
                      key=lambda day: day.day)[-pace_days:]
    billed_usd = sum((day.usd for day in accepted), Decimal(0))
    if billed_usd <= 0:
        return NoAcceptedBillPace()
    pace = billed_usd / len(accepted)
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    forecast = utc_month_to_date(account, today - date.resolution).usd if today.day > 1 else Decimal(0)
    gates = sum((group.usd for day in accepted for group in day.groups if group.group == ledger.SPEND_GROUPS[0]), Decimal(0))
    return UtcMonthForecast(forecast + pace * days_left, pace, len(accepted), gates / billed_usd)

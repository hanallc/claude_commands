"""The AWS spend per day and per month from the local cost ledger, without an AWS call.

The /aws_spend report and the AWS page both read the spend here, so both show the same numbers.
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
    # Every billed day, then each estimate day after the newest bill through the last day; newest first.
    days: tuple[ledger.DailySpend, ...]
    daily_limit: Decimal
    monthly_limit: Decimal


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
               directory: Path, instant: int, last_day: str) -> Spend | aws_config.MeasurementUnavailable:
    """The shown spend days from measurement inputs that the caller already read."""
    if isinstance(snapshot, aws_config.MeasurementUnavailable):
        return snapshot
    if isinstance(snapshot.costs, ledger.CostEvidenceUnavailable):
        return aws_config.MeasurementUnavailable(snapshot.costs.reason)
    if isinstance(inputs.settings, aws_config.MeasurementUnavailable):
        return inputs.settings
    spends = ledger.CostLedger(directory).spend_days(inputs.settings, instant)
    newest_bill_day = max((spend.day for spend in spends if spend.source == 'AWS bill'), default='')
    days = sorted((spend for spend in spends if spend.source == 'AWS bill' or newest_bill_day < spend.day <= last_day),
                  key=lambda spend: spend.day, reverse=True)
    return Spend(tuple(days), inputs.settings.daily_limit_usd, inputs.settings.monthly_limit_usd)


def read_spend(settings_path: Path, directory: Path, instant: int, last_day: str) -> Spend | aws_config.MeasurementUnavailable:
    """Read the ledger and return the shown spend days through the last day."""
    try:
        inputs = measurement.read_inputs(aws_config.account_settings_path(), directory)
        snapshot = measurement.read_measurement(settings_path, directory, instant, inputs=inputs)
        return spend_from(inputs, snapshot, directory, instant, last_day)
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

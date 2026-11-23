"""Read and expand the List's RFC 5545 repeat rules."""
from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from itertools import islice


class Frequency(StrEnum):
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


class Weekday(StrEnum):
    MONDAY = "MO"
    TUESDAY = "TU"
    WEDNESDAY = "WE"
    THURSDAY = "TH"
    FRIDAY = "FR"
    SATURDAY = "SA"
    SUNDAY = "SU"

    @property
    def day_number(self) -> int:
        return _WEEKDAYS.index(self)


_WEEKDAYS = tuple(Weekday)
_ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", -1: "last"}


class StartDay(StrEnum):
    SAME = "use the start date"


@dataclass(frozen=True)
class WeeklyDays:
    days: tuple[Weekday, ...]


@dataclass(frozen=True)
class MonthDay:
    day: int


@dataclass(frozen=True)
class MonthWeekday:
    ordinal: int
    weekday: Weekday


class RepeatUntilLimit(StrEnum):
    REACHED = "end at the series limit"


@dataclass(frozen=True)
class RunCount:
    count: int


@dataclass(frozen=True)
class RunUntil:
    utc: datetime


class NoNextRun(StrEnum):
    REMAINING = "no run remains"


def start_instant(start: datetime) -> datetime:
    if start.utcoffset() is None:
        raise ValueError("a schedule start must have a time zone")
    try:
        instant = start.astimezone(UTC)
        local = instant.astimezone(start.tzinfo)
    except OverflowError:
        raise ValueError("the start is outside the supported date range in UTC") from None
    if local.replace(tzinfo=None) != start.replace(tzinfo=None):
        raise ValueError("the start does not exist in its time zone")
    return instant


@dataclass(frozen=True)
class RepeatRule:
    frequency: Frequency
    interval: int
    days: StartDay | WeeklyDays | MonthDay | MonthWeekday
    end: RepeatUntilLimit | RunCount | RunUntil

    def __post_init__(self) -> None:
        if isinstance(self.days, WeeklyDays) and self.frequency is not Frequency.WEEKLY:
            raise ValueError("weekday lists require a weekly repeat")
        if isinstance(self.days, (MonthDay, MonthWeekday)) and self.frequency is not Frequency.MONTHLY:
            raise ValueError("monthly days require a monthly repeat")

    @property
    def rrule(self) -> str:
        fields = [f"FREQ={self.frequency}"]
        if self.interval != 1:
            fields.append(f"INTERVAL={self.interval}")
        if isinstance(self.days, WeeklyDays):
            fields.append("BYDAY=" + ",".join(self.days.days))
        elif isinstance(self.days, MonthDay):
            fields.append(f"BYMONTHDAY={self.days.day}")
        elif isinstance(self.days, MonthWeekday):
            fields.append(f"BYDAY={self.days.ordinal}{self.days.weekday}")
        if isinstance(self.end, RunCount):
            fields.append(f"COUNT={self.end.count}")
        elif isinstance(self.end, RunUntil):
            stamp = self.end.utc.replace(tzinfo=None).isoformat(timespec="seconds")
            fields.append("UNTIL=" + stamp.replace("-", "").replace(":", "") + "Z")
        return ";".join(fields)

    def _weekly_days(self, start: date) -> tuple[Weekday, ...]:
        return self.days.days if isinstance(self.days, WeeklyDays) else (_WEEKDAYS[start.weekday()],)

    def _month_day(self, start: date, year: int, month: int) -> int:
        if isinstance(self.days, MonthWeekday):
            first, last = calendar.monthrange(year, month)
            weekday = self.days.weekday.day_number
            if self.days.ordinal == -1:
                return last - (first + last - 1 - weekday) % 7
            return 1 + (weekday - first) % 7 + (self.days.ordinal - 1) * 7
        return self.days.day if isinstance(self.days, MonthDay) else start.day

    def validate_start(self, start: datetime) -> None:
        """Refuse a start that cannot be the first occurrence."""
        instant = start_instant(start)
        day = start.date()
        if self.frequency is Frequency.WEEKLY and _WEEKDAYS[day.weekday()] not in self._weekly_days(day):
            raise ValueError("the start must fall on one of the BYDAY weekdays")
        if self.frequency is Frequency.MONTHLY and day.day != self._month_day(day, day.year, day.month):
            raise ValueError("the start must match the monthly repeat day")
        if isinstance(self.end, RunUntil) and instant > self.end.utc:
            raise ValueError("UNTIL must be on or after the start")

    def _dates(self, start: date) -> Iterator[date]:
        first = start.toordinal()
        last = date.max.toordinal()
        match self.frequency:
            case Frequency.DAILY:
                for day in range(first, last + 1, self.interval):
                    yield date.fromordinal(day)
            case Frequency.WEEKLY:
                weekdays = self._weekly_days(start)
                for week in range(first - start.weekday(), last + 1, 7 * self.interval):
                    for weekday in weekdays:
                        if first <= (day := week + weekday.day_number) <= last:
                            yield date.fromordinal(day)
            case Frequency.MONTHLY:
                for index in range(start.year * 12 + start.month - 1, 10000 * 12, self.interval):
                    year, month_zero = divmod(index, 12)
                    month = month_zero + 1
                    day = self._month_day(start, year, month)
                    if day <= calendar.monthrange(year, month)[1]:
                        yield date(year, month, day)
            case Frequency.YEARLY:
                for year in range(start.year, 10000, self.interval):
                    if start.day <= calendar.monthrange(year, start.month)[1]:
                        yield date(year, start.month, start.day)

    def runs(self, start: datetime, limit: int) -> Iterator[datetime]:
        """Yield up to the series limit, including the start."""
        self.validate_start(start)
        count = min(limit, self.end.count) if isinstance(self.end, RunCount) else limit

        def valid_runs() -> Iterator[datetime]:
            for day in self._dates(start.date()):
                run = start if day == start.date() else datetime.combine(day, start.timetz().replace(fold=0))
                try:
                    instant = run.astimezone(UTC)
                    local = instant.astimezone(start.tzinfo)
                except OverflowError:
                    break
                if isinstance(self.end, RunUntil) and instant > self.end.utc:
                    break
                if local.replace(tzinfo=None) == run.replace(tzinfo=None):
                    yield run

        return islice(valid_runs(), max(0, count))

    def next_after(self, start: datetime, after: datetime, limit: int) -> datetime | NoNextRun:
        """Find the first run strictly after a time."""
        if after.utcoffset() is None:
            raise ValueError("the comparison time must have a time zone")
        try:
            instant = after.astimezone(UTC)
        except OverflowError:
            raise ValueError("the comparison time is outside the supported date range in UTC") from None
        return next((run for run in self.runs(start, limit) if run.astimezone(UTC) > instant), NoNextRun.REMAINING)

    def describe(self, start: datetime) -> str:
        singular, plural = {
            Frequency.DAILY: ("Daily", "days"), Frequency.WEEKLY: ("Weekly", "weeks"),
            Frequency.MONTHLY: ("Monthly", "months"), Frequency.YEARLY: ("Annually", "years"),
        }[self.frequency]
        text = singular if self.interval == 1 else f"Every {self.interval} {plural}"
        match self.frequency:
            case Frequency.DAILY:
                pass
            case Frequency.WEEKLY:
                weekdays = self._weekly_days(start.date())
                if self.interval == 1 and weekdays == _WEEKDAYS[:5]:
                    text = "Every weekday (Monday to Friday)"
                else:
                    names = [day.name.title() for day in weekdays]
                    text += " on " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])
            case Frequency.MONTHLY:
                if isinstance(self.days, MonthWeekday):
                    text += f" on the {_ORDINALS[self.days.ordinal]} {self.days.weekday.name.title()}"
                else:
                    text += f" on day {self._month_day(start.date(), start.year, start.month)}"
            case Frequency.YEARLY:
                text += f" on {calendar.month_name[start.month]} {start.day}"
        if isinstance(self.end, RunCount):
            text += f" for {self.end.count} {'run' if self.end.count == 1 else 'runs'}"
        elif isinstance(self.end, RunUntil):
            text += f" until {self.end.utc:%Y-%m-%d %H:%M:%S} UTC"
        return text


def _positive(text: str, field: str) -> int:
    if not re.fullmatch(r"[0-9]+", text) or len(text) > 10 or not (0 < (value := int(text)) <= 2**31 - 1):
        raise ValueError(f"{field} must be a positive RFC 5545 integer")
    return value


def parse(text: str) -> RepeatRule:
    """Refuse rule parts outside the List's repeat menu."""
    fields: dict[str, str] = {}
    for part in text.upper().removeprefix("RRULE:").split(";"):
        name, separator, value = part.partition("=")
        if not separator or name not in {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL"}:
            raise ValueError(f"unsupported repeat rule part: {part}")
        if name in fields or not value:
            raise ValueError(f"{name} must have one value")
        fields[name] = value
    try:
        frequency = Frequency(fields.get("FREQ", ""))
    except ValueError:
        raise ValueError("FREQ must be DAILY, WEEKLY, MONTHLY, or YEARLY") from None
    interval = _positive(fields.get("INTERVAL", "1"), "INTERVAL")
    days: StartDay | WeeklyDays | MonthDay | MonthWeekday = StartDay.SAME
    if "BYMONTHDAY" in fields:
        if frequency is not Frequency.MONTHLY or "BYDAY" in fields:
            raise ValueError("BYMONTHDAY is for monthly repeats without BYDAY")
        day = _positive(fields["BYMONTHDAY"], "BYMONTHDAY")
        if day > 31:
            raise ValueError("BYMONTHDAY must be from 1 to 31")
        days = MonthDay(day)
    if "BYDAY" in fields:
        if frequency is Frequency.WEEKLY:
            weekdays = fields["BYDAY"].split(",")
            if len(set(weekdays)) != len(weekdays) or any(day not in Weekday for day in weekdays):
                raise ValueError("weekly BYDAY needs distinct weekdays such as MO,TH")
            days = WeeklyDays(tuple(day for day in _WEEKDAYS if day in weekdays))
        elif frequency is Frequency.MONTHLY:
            match = re.fullmatch(r"(\+?[1-4]|-1)(MO|TU|WE|TH|FR|SA|SU)", fields["BYDAY"])
            if match is None:
                raise ValueError("monthly BYDAY needs a weekday with ordinal 1 to 4 or -1")
            days = MonthWeekday(int(match[1]), Weekday(match[2]))
        else:
            raise ValueError("BYDAY is for weekly or monthly repeats")
    end: RepeatUntilLimit | RunCount | RunUntil = RepeatUntilLimit.REACHED
    if "COUNT" in fields:
        if "UNTIL" in fields:
            raise ValueError("use COUNT or UNTIL")
        end = RunCount(_positive(fields["COUNT"], "COUNT"))
    if "UNTIL" in fields:
        try:
            if re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", fields["UNTIL"]) is None:
                raise ValueError
            end = RunUntil(datetime.strptime(fields["UNTIL"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC))
        except ValueError:
            raise ValueError("UNTIL must be a UTC time such as 20261231T170000Z") from None
    return RepeatRule(frequency, interval, days, end)

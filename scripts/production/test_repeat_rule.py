"""Check the dates and words of List repeat rules."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from itertools import islice
from zoneinfo import ZoneInfo

from scripts.production import repeat_rule


class RepeatRuleTests(unittest.TestCase):
    def test_menu_and_custom_rules(self) -> None:
        cases = (
            ("2026-11-30", "FREQ=DAILY", "Daily", ("2026-12-01", "2026-12-02", "2026-12-03")),
            ("2026-11-30", "FREQ=WEEKLY;BYDAY=MO", "Weekly on Monday", ("2026-12-07", "2026-12-14", "2026-12-21")),
            ("2026-11-03", "FREQ=MONTHLY;BYDAY=1TU", "Monthly on the first Tuesday",
             ("2026-12-01", "2027-01-05", "2027-02-02")),
            ("2026-11-10", "FREQ=MONTHLY;BYDAY=2TU", "Monthly on the second Tuesday",
             ("2026-12-08", "2027-01-12", "2027-02-09")),
            ("2026-11-17", "FREQ=MONTHLY;BYDAY=3TU", "Monthly on the third Tuesday",
             ("2026-12-15", "2027-01-19", "2027-02-16")),
            ("2026-11-24", "FREQ=MONTHLY;BYDAY=4TU", "Monthly on the fourth Tuesday",
             ("2026-12-22", "2027-01-26", "2027-02-23")),
            ("2026-11-30", "FREQ=MONTHLY;BYDAY=-1MO", "Monthly on the last Monday",
             ("2026-12-28", "2027-01-25", "2027-02-22")),
            ("2026-11-30", "FREQ=YEARLY", "Annually on November 30", ("2027-11-30", "2028-11-30", "2029-11-30")),
            ("2026-11-27", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "Every weekday (Monday to Friday)",
             ("2026-11-30", "2026-12-01", "2026-12-02")),
            ("2026-11-30", "FREQ=DAILY;INTERVAL=2", "Every 2 days", ("2026-12-02", "2026-12-04", "2026-12-06")),
            ("2026-11-30", "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH", "Every 2 weeks on Monday and Thursday",
             ("2026-12-03", "2026-12-14", "2026-12-17")),
            ("2027-01-31", "FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=31", "Every 2 months on day 31",
             ("2027-03-31", "2027-05-31", "2027-07-31")),
            ("2027-01-31", "FREQ=MONTHLY;BYMONTHDAY=31", "Monthly on day 31", ("2027-03-31", "2027-05-31", "2027-07-31")),
            ("2026-11-27", "FREQ=MONTHLY;INTERVAL=2;BYDAY=-1FR", "Every 2 months on the last Friday",
             ("2027-01-29", "2027-03-26", "2027-05-28")),
            ("2024-02-29", "FREQ=YEARLY;INTERVAL=2", "Every 2 years on February 29",
             ("2028-02-29", "2032-02-29", "2036-02-29")),
            ("2026-11-30", "FREQ=DAILY;COUNT=3", "Daily for 3 runs", ("2026-12-01", "2026-12-02")),
            ("2026-11-30", "FREQ=DAILY;UNTIL=20261202T170000Z", "Daily until 2026-12-02 17:00:00 UTC",
             ("2026-12-01", "2026-12-02")),
            ("2026-03-06", "FREQ=DAILY", "Daily", ("2026-03-07", "2026-03-08", "2026-03-09")),
        )
        zone = ZoneInfo("America/Los_Angeles")
        for day, text, description, expected in cases:
            with self.subTest(start=day, text=text):
                start = datetime.fromisoformat(day + "T09:00").replace(tzinfo=zone)
                rule = repeat_rule.parse(text)
                dates = (*islice(rule.runs(start, 730), 4),)
                self.assertEqual(tuple(run.strftime("%Y-%m-%d %H:%M") for run in dates),
                                 tuple(date + " 09:00" for date in (day, *expected)))
                if day == "2026-03-06":
                    self.assertEqual(tuple(run.strftime("%z") for run in dates), ("-0800", "-0800", "-0700", "-0700"))
                self.assertEqual(rule.next_after(start, start, 730), dates[1])
                self.assertEqual(rule.describe(start), description)
                if len(expected) < 3:
                    self.assertIs(rule.next_after(start, dates[-1], 730), repeat_rule.NoNextRun.REMAINING)

        start = datetime.fromisoformat("9999-12-30T23:00-05:00")
        for text, limit, expected, error in (
            ("FREQ=DAILY", 730, ("9999-12-30 23:00",), ""),
            ("FREQ=DAILY;COUNT=2", 730, (), "COUNT crosses the supported date limit"),
            ("FREQ=DAILY", sys.maxsize + 1, ("9999-12-30 23:00",), ""),
        ):
            with self.subTest(text=text, limit=limit):
                rule = repeat_rule.parse(text)
                if error:
                    with self.assertRaisesRegex(ValueError, error):
                        _ = tuple(rule.runs(start, limit))
                else:
                    dates = tuple(rule.runs(start, limit))
                    self.assertEqual(tuple(run.strftime("%Y-%m-%d %H:%M") for run in dates), expected)
                    self.assertEqual(rule.describe(start), "Daily")
                    self.assertIs(rule.next_after(start, dates[-1], limit), repeat_rule.NoNextRun.REMAINING)

    def test_rule_refuses_parts_outside_the_repeat_menu(self) -> None:
        for text, error in (
            ("FREQ=HOURLY", "FREQ must be"),
            ("FREQ=DAILY;INTERVAL=0", "INTERVAL must be"),
            ("FREQ=WEEKLY;BYDAY=MO,MO", "distinct weekdays"),
            ("FREQ=MONTHLY;BYDAY=5MO", "ordinal 1 to 4 or -1"),
            ("FREQ=MONTHLY;BYMONTHDAY=32", "from 1 to 31"),
            ("FREQ=MONTHLY;BYMONTHDAY=2;BYDAY=2TU", "without BYDAY"),
            ("FREQ=DAILY;WKST=SU", "unsupported repeat rule part"),
            ("FREQ=YEARLY;BYMONTH=11", "unsupported repeat rule part"),
            ("FREQ=DAILY;UNTIL=20261202T170000", "UTC time"),
            ("FREQ=DAILY;COUNT=3;UNTIL=20261202T170000Z", "use COUNT or UNTIL"),
            (repeat_rule.WeeklyDays((repeat_rule.Weekday.MONDAY,)), "require a weekly repeat"),
        ):
            with self.subTest(rule=text), self.assertRaisesRegex(ValueError, error):
                if isinstance(text, str):
                    _ = repeat_rule.parse(text)
                else:
                    _ = repeat_rule.RepeatRule(repeat_rule.Frequency.DAILY, 1, text, repeat_rule.RepeatUntilLimit.REACHED)

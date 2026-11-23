"""The Builds page uses recorded owner names and elapsed time."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.build_queue.queue_lines import age, step_text


class StepAgeTests(unittest.TestCase):
    def test_minutes_floor_elapsed_time_and_never_show_a_negative_wait(self) -> None:
        for elapsed, expected in ((-1, 0), (0, 0), (59.9, 0), (60, 1), (125, 2)):
            with self.subTest(elapsed=elapsed):
                self.assertEqual(age(1000 - elapsed, 1000), expected)


class StepNameTests(unittest.TestCase):
    def test_step_text_checks_each_unit_before_the_staff_owner(self) -> None:
        from scripts.build_queue import admission_records as records
        from scripts.lib.build_steps import StepNames
        from scripts.message.unit_labels import UnitLabels

        step = records.WaitingStep(123, "fixture", "caller", "check", "/private/widget",
                                   0, 1, records.ReadyForAdmission(), unit="unregistered session", project="widget")
        names = StepNames({"names.check": "check"})
        for staff_unit, expected in (("production-panes", "panes: check"), ("$UNIT", "production: check")):
            with self.subTest(staff_unit=staff_unit):
                labels = {"fixture": UnitLabels("caller", "production", staff_unit)}
                self.assertEqual(step_text(step, names, labels=labels), expected)

    def test_step_text_checks_the_configured_step_and_owner_parts(self) -> None:
        from scripts.build_queue import admission_records as records
        from scripts.lib.build_steps import StepNames

        step = records.WaitingStep(123, "", "", "check", "", 0, 1, records.ReadyForAdmission())
        for owner, shown, expected in (
            ("panes", "type check", "panes: type check"),
            ("panes", "—", "panes"),
            ("—", "type check", "unregistered session: type check"),
            ("—", "—", "unregistered session"),
        ):
            with self.subTest(owner=owner, shown=shown):
                self.assertEqual(step_text(replace(step, unit=owner), StepNames({"names.check": shown})), expected)

    def test_staff_owners_replace_caller_names_in_step_text(self) -> None:
        from scripts.build_queue import admission_records as records
        from scripts.lib.build_steps import StepNames
        from scripts.message import unit_labels
        from scripts.production.staff_test_support import fixture_member, isolated_staff_file

        with tempfile.TemporaryDirectory() as directory, isolated_staff_file(Path(directory) / "staff.json", {}):
            runner = fixture_member("showrunner", name="tool-based-ui")
            unit = fixture_member("unit", name="trunk", parent=runner.id)
            solo = fixture_member("producer", name="solo")
            with mock.patch.object(unit_labels, "newest_sessions", return_value=[]):
                labels = unit_labels.unit_labels_for([unit.current_session, solo.current_session], {})
            names = StepNames({"names.check": "check", "prose.check": "type check"})
            waiter = records.WaitingStep(123, unit.current_session, "caller", "check", "/private/tree",
                                         0, 1, records.ReadyForAdmission(), project="tool-based-ui-trunk")
            running = records.RunningStep(unit.current_session, "caller", "check", "/private/tree", 1,
                                           records.PromisedMemory(1), pid=123, project="tool-based-ui-trunk")
            for step in (waiter, running):
                for session, saved_unit, expected in ((unit.current_session, "", "trunk"),
                        (solo.current_session, "", "solo"),
                        ("unrecorded", "", "caller"),
                        ("unrecorded", "saved-unit", "saved-unit"),
                        (unit.current_session, "saved-unit", "saved-unit")):
                    with self.subTest(step=type(step).__name__, session=session, unit=saved_unit):
                        row = replace(step, session_id=session, unit=saved_unit)
                        self.assertEqual(step_text(row, names, labels=labels), expected + ": check")

    def test_the_wait_line_and_the_row_pick_the_same_owner(self) -> None:
        from scripts.build_queue import admission_records as records
        from scripts.build_queue.queue_lines import blocking_step_name
        from scripts.build_queue.snapshot_codec import wait_line_name
        from scripts.lib.build_steps import StepNames
        from scripts.message.unit_labels import UnitLabels

        step = records.WaitingStep(123, "fixture", "caller", "check", "/private/widget",
                                   0, 1, records.ReadyForAdmission(), unit="production-panes")
        labels = {"fixture": UnitLabels("caller", "production", "production-panes")}
        names = StepNames({"names.check": "check"})
        self.assertEqual(blocking_step_name(step, labels=labels), step_text(step, names, labels=labels))
        self.assertEqual(blocking_step_name(step, labels=labels), "panes: check")
        for unit, caller, folder, fallback, expected in (
            ("panes", "caller", "/private/widget", "head", "panes: check"),
            ("", "caller", "/private/widget", "head", "caller: check"),
            ("", "", "/private/widget", "head", "widget: check"),
            ("", "", "", "head", "head: check"),
            ("", "", "", "", "unregistered session: check"),
        ):
            with self.subTest(unit=unit, caller=caller, folder=folder, fallback=fallback):
                self.assertEqual(wait_line_name(unit, caller, folder, "check", fallback_name=fallback), expected)


if __name__ == "__main__":
    _ = unittest.main()

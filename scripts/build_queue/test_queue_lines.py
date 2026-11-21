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
    def test_recorded_units_replace_repository_names_in_step_text_and_prose(self) -> None:
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
            waiter = records.WaitingStep(123, unit.current_session, "trunk", "check", "/private/tree",
                                         0, 1, records.ReadyForAdmission(), project="tool-based-ui-trunk")
            running = records.RunningStep(unit.current_session, "trunk", "check", "/private/tree", 1,
                                           records.PromisedMemory(1), pid=123, project="tool-based-ui-trunk")
            for step in (waiter, running):
                for session, saved_unit, expected in ((unit.current_session, "", "trunk"),
                        (solo.current_session, "", "tool-based-ui-trunk"),
                        ("unrecorded", "", "tool-based-ui-trunk"),
                        ("unrecorded", "saved-unit", "saved-unit"),
                        (unit.current_session, "saved-unit", "saved-unit")):
                    with self.subTest(step=type(step).__name__, session=session, unit=saved_unit):
                        row = replace(step, session_id=session, unit=saved_unit)
                        self.assertEqual(step_text(row, names, labels=labels), expected + " check")


if __name__ == "__main__":
    _ = unittest.main()

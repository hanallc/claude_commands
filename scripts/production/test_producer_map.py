"""The producer map finds each producer in lookup order and sends a checked row through /tune."""
from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.lib import conf, tune
from scripts.production import producer_map, producer_map_rows, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member

HEADER = "# Map a showrunner or unit to the producer it reports to.\n"
UNSET = staff.StaffRow(staff.StaffId(""), "producer", "", staff.StaffRoot(), current_session=staff.StaffId(""))


class ProducerMapTests(StaffTestCase):
    config: Path = Path()
    dwight: staff.StaffRow = UNSET
    bob: staff.StaffRow = UNSET
    spacetime: staff.StaffRow = UNSET
    models: staff.StaffRow = UNSET
    child: staff.StaffRow = UNSET
    inner: staff.StaffRow = UNSET

    @override
    def setUp(self) -> None:
        super().setUp()
        folder = self.root / "config"
        folder.mkdir()
        self.config = folder / "producer_map.conf"
        _ = self.config.write_text(HEADER, encoding="utf-8")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"CONF_DIR": str(folder)})))
        self.dwight = fixture_member("producer", name="dwight")
        self.bob = fixture_member("producer", name="bob")
        self.spacetime = fixture_member("showrunner", name="spacetime")
        self.models = fixture_member("unit", name="models", parent=self.spacetime.id)
        self.child = fixture_member("showrunner", name="child", parent=self.spacetime.id)
        self.inner = fixture_member("unit", name="inner", parent=self.child.id)

    def rows(self, *pairs: tuple[staff.StaffRow, staff.StaffId]) -> None:
        _ = self.config.write_text(HEADER + "".join(f"{producer_map_rows.name_comment(member.id, producer)}\n"
                                                    + f"{member.id}={producer}\n"
                                                    for member, producer in pairs), encoding="utf-8")

    def producer(self, member: staff.StaffRow) -> staff.StaffRow | producer_map.NoProducer:
        return producer_map.producer_for(member.id, staff.snapshot())

    def test_a_producer_is_its_own_producer(self) -> None:
        self.assertEqual(self.producer(self.dwight), self.dwight)

    def test_a_showrunner_row_names_its_producer(self) -> None:
        self.rows((self.spacetime, self.dwight.id))
        self.assertEqual(self.producer(self.spacetime), self.dwight)

    def test_a_unit_with_no_row_takes_its_showrunner_producer(self) -> None:
        self.rows((self.spacetime, self.dwight.id))
        self.assertEqual(self.producer(self.models), self.dwight)

    def test_a_unit_row_overrides_its_showrunner_row(self) -> None:
        self.rows((self.spacetime, self.dwight.id), (self.models, self.bob.id))
        self.assertEqual(self.producer(self.models), self.bob)
        self.assertEqual(self.producer(self.spacetime), self.dwight)

    def test_a_child_showrunner_with_no_row_takes_its_parent_producer(self) -> None:
        self.rows((self.spacetime, self.dwight.id))
        self.assertEqual(self.producer(self.child), self.dwight)
        self.assertEqual(self.producer(self.inner), self.dwight)
        self.rows((self.spacetime, self.dwight.id), (self.child, self.bob.id))
        self.assertEqual(self.producer(self.inner), self.bob)

    def test_no_row_gives_no_producer(self) -> None:
        self.assertIs(self.producer(self.models), producer_map.NO_PRODUCER)

    def test_a_retired_or_missing_producer_counts_as_no_producer(self) -> None:
        self.rows((self.spacetime, staff.StaffId("00000000-0000-4000-8000-000000000000")))
        self.assertIs(self.producer(self.models), producer_map.NO_PRODUCER)
        self.rows((self.spacetime, self.bob.id))
        _ = staff.set_producer_retirement(self.bob.id, staff.RetiredProducer("claude:sonnet:high"))
        self.assertIs(self.producer(self.spacetime), producer_map.NO_PRODUCER)
        self.assertIs(self.producer(self.bob), producer_map.NO_PRODUCER)

    def test_a_row_names_an_active_showrunner_or_unit_and_an_active_producer(self) -> None:
        self.assertEqual(producer_map_rows.checked_comment(self.models.id, self.bob.id), "# models -> bob")
        self.assertEqual(producer_map_rows.checked_comment(self.spacetime.id, self.dwight.id), "# spacetime -> dwight")
        for key, value in ((self.dwight.id, self.bob.id), (self.models.id, self.spacetime.id),
                           ("models", self.bob.id), (self.models.id, "bob")):
            with self.subTest(key=key, value=value), self.assertRaises(conf.Unusable):
                _ = producer_map_rows.checked_comment(key, value)
        unknown = "00000000-0000-4000-8000-000000000000"
        self.assertEqual(producer_map_rows.name_comment(self.models.id, unknown), f"# models -> {unknown}")

    def test_a_unit_maps_through_its_own_showrunner(self) -> None:
        members = staff.snapshot()
        self.assertEqual(producer_map.map_member(self.models, members), self.spacetime)
        self.assertEqual(producer_map.map_member(self.inner, members), self.child)
        self.assertEqual(producer_map.map_member(self.child, members), self.child)
        self.assertEqual(producer_map.map_member(self.spacetime, members), self.spacetime)

    def test_cli_set_sends_the_row_and_nate_words_to_tune(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(tune, "main", return_value=0) as change:
            self.assertEqual(producer_map.main(["lookup", "spacetime"]), 3)
            self.assertEqual(producer_map.main(["set", "spacetime", "Dwight", "--user-words", "dwight runs it"]), 0)
            self.rows((self.spacetime, self.dwight.id))
            self.assertEqual(producer_map.main(["lookup", "spacetime/models"]), 0)
            self.assertEqual(producer_map.main(["set", "spacetime", "nobody", "--user-words", "nobody"]), 2)
        change.assert_called_once_with([producer_map.STEM, self.spacetime.id, self.dwight.id,
                                        "--why", "spacetime reports to producer dwight",
                                        "--user-words", "dwight runs it"])
        self.assertEqual(output.getvalue().splitlines(), [
            "spacetime: no producer", f"models -> dwight ({self.dwight.id})"])

    def test_set_passes_when_tune_keeps_the_row_live_but_main_lacks_it(self) -> None:
        def live_only(_arguments: list[str]) -> int:
            self.rows((self.spacetime, self.dwight.id))
            return 2

        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(tune, "main", side_effect=live_only):
            self.assertEqual(producer_map.main(["set", "spacetime", "dwight", "--user-words", "dwight"]), 0)
        self.assertEqual(output.getvalue(), "spacetime -> dwight is live; main gets the row later\n")

    def test_set_returns_the_tune_code_when_the_row_is_not_live(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(tune, "main", return_value=2):
            self.assertEqual(producer_map.main(["set", "spacetime", "dwight", "--user-words", "dwight"]), 2)
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    _ = unittest.main()

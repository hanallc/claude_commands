"""The producer map finds each producer in lookup order and sends a checked row through /tune."""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, override
from unittest import mock

from scripts.lib import conf, tune, waiting_rows
from scripts.production import producer_map, producer_map_rows, staff
from scripts.production.staff_test_support import StaffTestCase, fixture_member

HEADER = "# The producer of a showrunner or of a unit.\n"
SEEDED_ROWS = "".join(f"{row}\n" for row in (
    "51505394-82db-43d3-a528-136f4ee38d7a = a898cb38-dbac-4751-b72b-d0b14ab9c38d  # builds = bob",
    "439f9cfc-4aef-41ce-931a-c7abac39327e = 920b0a64-6eee-479e-8c07-e03e4dcd4ef6  # hana = dwight",
    "96b016bf-87cf-4a19-af7a-77425852c2e2 = 01a19d07-c162-7b43-bb48-3ee6c913b3f0  # natedev = brain",
    "20b0427d-945b-4c40-bb24-a6bec83dd648 = 01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a  # sdlc = morty",
    "b6374931-fe3a-4f6d-96b7-ad20415a8346 = 920b0a64-6eee-479e-8c07-e03e4dcd4ef6  # spacetime = dwight",
    "450ac99b-caed-49ba-8c6f-29016df421ab = 01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a  # cables = morty",
    "49511209-add0-40d2-8c53-bca664aaa852 = 01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a  # demo = morty",
    "e404d92e-8cc6-4714-952f-528c8de5cb7c = 01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a  # postmaster = morty",
    "e46c8ba2-9fde-4b71-b1ee-c1d87ad1f850 = 01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a  # efficiency = morty",
))
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
        _ = self.config.write_text(HEADER + "".join(
            f"{member.id} = {producer}  {producer_map_rows.name_comment(member.id, producer)}\n"
            for member, producer in pairs), encoding="utf-8")

    def producer(self, member: staff.StaffRow) -> staff.StaffRow | producer_map.NoProducer:
        found = producer_map.producer_for(member.id, staff.snapshot())
        return found.producer if isinstance(found, producer_map.MappedProducer) else found

    def set_by(self, member: staff.StaffRow) -> staff.StaffRow:
        found = producer_map.producer_for(member.id, staff.snapshot())
        assert isinstance(found, producer_map.MappedProducer)
        return found.set_by

    def set_rows(self, *arguments: str) -> tuple[list[list[str]], str]:
        """Run set with a fake tune that writes the row, and return the tune calls and the output."""
        calls: list[list[str]] = []

        def save(arguments: list[str]) -> int:
            calls.append(arguments)
            text = self.config.read_text(encoding="utf-8")
            _ = self.config.write_text(waiting_rows.named_row(text, arguments[1], arguments[2], "# fixture"),
                                       encoding="utf-8")
            print("live now: tune detail")
            return 0

        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(tune, "main", side_effect=save):
            self.assertEqual(producer_map.main(["set", *arguments]), 0)
        return calls, output.getvalue()

    def test_a_producer_is_its_own_producer(self) -> None:
        self.assertEqual(self.producer(self.dwight), self.dwight)
        self.assertEqual(self.set_by(self.dwight), self.dwight)

    def test_a_showrunner_row_names_its_producer(self) -> None:
        self.rows((self.spacetime, self.dwight.id))
        self.assertEqual(self.producer(self.spacetime), self.dwight)
        self.assertEqual(self.set_by(self.spacetime), self.spacetime)

    def test_a_showrunner_row_covers_a_unit_with_no_row(self) -> None:
        self.rows((self.spacetime, self.dwight.id))
        self.assertEqual(self.producer(self.models), self.dwight)
        self.assertEqual(self.set_by(self.models), self.spacetime)

    def test_a_unit_row_wins_over_its_showrunner_row(self) -> None:
        self.rows((self.spacetime, self.dwight.id), (self.models, self.bob.id))
        self.assertEqual(self.producer(self.models), self.bob)
        self.assertEqual(self.set_by(self.models), self.models)
        self.assertEqual(self.producer(self.spacetime), self.dwight)

    def test_a_child_showrunner_with_no_row_takes_its_parent_producer(self) -> None:
        self.rows((self.spacetime, self.dwight.id))
        self.assertEqual(self.producer(self.child), self.dwight)
        self.assertEqual(self.producer(self.inner), self.dwight)
        self.rows((self.spacetime, self.dwight.id), (self.child, self.bob.id))
        self.assertEqual(self.producer(self.inner), self.bob)
        self.assertEqual(self.set_by(self.inner), self.child)

    def test_no_row_gives_no_producer(self) -> None:
        self.assertIs(self.producer(self.models), producer_map.NO_ROW)

    def test_a_retired_or_missing_producer_makes_a_stale_row(self) -> None:
        missing = "00000000-0000-4000-8000-000000000000"
        self.rows((self.spacetime, staff.StaffId(missing)))
        self.assertEqual(self.producer(self.models), producer_map.StaleRow(self.spacetime, missing))
        self.rows((self.spacetime, self.dwight.id), (self.models, self.bob.id))
        bob = staff.set_producer_retirement(self.bob.id, staff.RetiredProducer("claude:sonnet:high"))
        self.assertEqual(self.producer(self.models), producer_map.StaleRow(self.models, bob.name))
        self.assertIs(self.producer(bob), producer_map.NO_ROW)

    def test_an_answer_replaces_the_stale_row_that_wins(self) -> None:
        self.rows((self.spacetime, self.dwight.id), (self.models, self.bob.id))
        _ = staff.set_producer_retirement(self.bob.id, staff.RetiredProducer("claude:sonnet:high"))
        calls, output = self.set_rows("models", "dwight", "--user-words", "dwight")
        self.assertEqual([call[1] for call in calls], [self.models.id])
        self.assertEqual(output, "Saved: models = dwight.\n")
        self.assertEqual(self.set_by(self.models), self.models)

    def test_a_row_names_an_active_showrunner_or_unit_and_an_active_producer(self) -> None:
        self.assertEqual(producer_map_rows.checked_comment(self.models.id, self.bob.id), "# models = bob")
        self.assertEqual(producer_map_rows.checked_comment(self.spacetime.id, self.dwight.id), "# spacetime = dwight")
        for key, value in ((self.dwight.id, self.bob.id), (self.models.id, self.spacetime.id),
                           ("models", self.bob.id), (self.models.id, "bob")):
            with self.subTest(key=key, value=value), self.assertRaises(conf.Unusable):
                _ = producer_map_rows.checked_comment(key, value)
        unknown = "00000000-0000-4000-8000-000000000000"
        self.assertEqual(producer_map_rows.name_comment(self.models.id, unknown), f"# models = {unknown}")

    def test_a_unit_names_its_own_showrunner(self) -> None:
        members = staff.snapshot()
        self.assertEqual(producer_map.showrunner_of(self.models, members), self.spacetime)
        self.assertEqual(producer_map.showrunner_of(self.inner, members), self.child)

    def test_lookup_prints_the_producer_and_its_source(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(producer_map.main(["lookup", "spacetime"]), 3)
            self.rows((self.spacetime, self.dwight.id))
            self.assertEqual(producer_map.main(["lookup", "spacetime/models"]), 0)
        self.assertEqual(output.getvalue().splitlines(), [
            "spacetime: no producer", f"models -> dwight ({self.dwight.id}), set by spacetime"])

    def test_an_answer_for_a_showrunner_covers_its_units(self) -> None:
        calls, output = self.set_rows("spacetime", "Dwight", "--user-words", "dwight")
        self.assertEqual(calls, [[producer_map.STEM, self.spacetime.id, self.dwight.id,
                                  "--why", "spacetime reports to producer dwight", "--user-words", "dwight"]])
        self.assertEqual(output, "Saved: spacetime = dwight. Its units use dwight too.\n")
        self.assertEqual(self.producer(self.models), self.dwight)

    def test_a_saved_showrunner_row_leaves_a_unit_with_its_own_row(self) -> None:
        charts = fixture_member("unit", name="charts", parent=self.spacetime.id)
        self.rows((self.models, self.bob.id))
        _, output = self.set_rows("spacetime", "dwight", "--user-words", "dwight")
        self.assertEqual(output, "Saved: spacetime = dwight. Its units with no row use dwight too.\n")
        self.rows((self.models, self.bob.id))
        _, output = self.set_rows("charts", "dwight", "--user-words", "dwight")
        self.assertEqual(output, "Saved: spacetime = dwight, for each of its units with no row.\n")
        self.assertEqual((self.producer(self.models), self.producer(charts)), (self.bob, self.dwight))

    def test_lookup_names_a_stale_row(self) -> None:
        missing = "00000000-0000-4000-8000-000000000000"
        self.rows((self.spacetime, staff.StaffId(missing)))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(producer_map.main(["lookup", "spacetime/models"]), 3)
        self.assertEqual(output.getvalue(), f"The row of spacetime names {missing}, who is not active.\n")

    def test_an_answer_for_a_unit_saves_its_showrunner_row(self) -> None:
        calls, output = self.set_rows("models", "dwight", "--user-words", "dwight")
        self.assertEqual([call[1] for call in calls], [self.spacetime.id])
        self.assertEqual(output, "Saved: spacetime = dwight, for each of its units.\n")
        self.assertEqual(self.set_by(self.models), self.spacetime)

    def test_an_answer_for_one_unit_only_saves_the_unit_row(self) -> None:
        calls, output = self.set_rows("models", "dwight", "--unit-only", "--user-words", "dwight for models only")
        self.assertEqual([call[1] for call in calls], [self.models.id])
        self.assertEqual(output, "Saved: models = dwight.\n")
        self.assertEqual(self.set_by(self.models), self.models)
        self.assertIs(self.producer(self.spacetime), producer_map.NO_ROW)

    def test_unit_only_needs_a_unit_and_an_unknown_producer_is_refused(self) -> None:
        with mock.patch.object(tune, "main", return_value=0) as change:
            self.assertEqual(producer_map.main(["set", "spacetime", "dwight", "--unit-only", "--user-words", "x"]), 2)
            self.assertEqual(producer_map.main(["set", "spacetime", "nobody", "--user-words", "nobody"]), 2)
        change.assert_not_called()

    def test_set_passes_when_tune_keeps_the_row_live_but_main_lacks_it(self) -> None:
        def live_only(_arguments: list[str]) -> int:
            self.rows((self.spacetime, self.dwight.id))
            return 2

        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(tune, "main", side_effect=live_only):
            self.assertEqual(producer_map.main(["set", "spacetime", "dwight", "--user-words", "dwight"]), 0)
        self.assertEqual(output.getvalue(), "Saved: spacetime = dwight. Its units use dwight too.\n")

    def test_set_returns_the_tune_code_when_the_row_is_not_live(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(tune, "main", return_value=2):
            self.assertEqual(producer_map.main(["set", "spacetime", "dwight", "--user-words", "dwight"]), 2)
        self.assertEqual(output.getvalue(), "")

    def test_set_needs_the_own_row_when_a_parent_row_gives_the_same_producer(self) -> None:
        self.rows((self.spacetime, self.dwight.id))
        output = io.StringIO()
        with redirect_stdout(output), mock.patch.object(tune, "main", return_value=2):
            self.assertEqual(producer_map.main(["set", self.models.id, "dwight", "--unit-only",
                                                "--user-words", "dwight for models only"]), 2)
        self.assertEqual(output.getvalue(), "")


def seeded(identity: str, kind: staff.Kind, name: str, parent: staff.StaffRow | None = None) -> staff.StaffRow:
    reports: staff.ReportsTo | staff.StaffRoot = staff.ReportsTo(parent.id) if parent is not None else staff.StaffRoot()
    return staff.StaffRow(staff.StaffId(identity), kind, name, reports, current_session=staff.StaffId(identity))


class SeededRowsTests(unittest.TestCase):
    """A copy of nate's nine first rows gives each member the producer he named."""

    def test_the_nine_rows_give_the_named_producers(self) -> None:
        bob = seeded("a898cb38-dbac-4751-b72b-d0b14ab9c38d", "producer", "bob")
        dwight = seeded("920b0a64-6eee-479e-8c07-e03e4dcd4ef6", "producer", "dwight")
        brain = seeded("01a19d07-c162-7b43-bb48-3ee6c913b3f0", "producer", "brain")
        morty = seeded("01a18dbb-7e8f-7641-b3dd-bb2071ab9d7a", "producer", "morty")
        hana = seeded("439f9cfc-4aef-41ce-931a-c7abac39327e", "showrunner", "hana")
        natedev = seeded("96b016bf-87cf-4a19-af7a-77425852c2e2", "showrunner", "natedev")
        builds = seeded("51505394-82db-43d3-a528-136f4ee38d7a", "showrunner", "builds", natedev)
        sdlc = seeded("20b0427d-945b-4c40-bb24-a6bec83dd648", "showrunner", "sdlc", natedev)
        spacetime = seeded("b6374931-fe3a-4f6d-96b7-ad20415a8346", "showrunner", "spacetime", hana)
        cargo_liner = seeded("4dbdb78e-a83b-4248-b8f9-ff278dcbc60c", "showrunner", "cargo-liner")
        units = {
            "cables": seeded("450ac99b-caed-49ba-8c6f-29016df421ab", "unit", "cables", hana),
            "demo": seeded("49511209-add0-40d2-8c53-bca664aaa852", "unit", "demo", hana),
            "postmaster": seeded("e404d92e-8cc6-4714-952f-528c8de5cb7c", "unit", "postmaster", natedev),
            "efficiency": seeded("e46c8ba2-9fde-4b71-b1ee-c1d87ad1f850", "unit", "efficiency", natedev),
            "fps": seeded("10000000-0000-4000-8000-000000000001", "unit", "fps", spacetime),
            "aws": seeded("10000000-0000-4000-8000-000000000002", "unit", "aws", builds),
            "phases": seeded("10000000-0000-4000-8000-000000000003", "unit", "phases", sdlc),
            "errors": seeded("10000000-0000-4000-8000-000000000004", "unit", "errors", hana),
            "accounts": seeded("10000000-0000-4000-8000-000000000005", "unit", "accounts", natedev),
            "tile": seeded("10000000-0000-4000-8000-000000000006", "unit", "tile", cargo_liner),
        }
        members = staff.StaffSnapshot((bob, dwight, brain, morty, hana, natedev, builds, sdlc, spacetime, cargo_liner,
                                       *units.values()))
        expected = {
            builds: (bob, builds), hana: (dwight, hana), natedev: (brain, natedev), sdlc: (morty, sdlc),
            spacetime: (dwight, spacetime), units["cables"]: (morty, units["cables"]),
            units["demo"]: (morty, units["demo"]), units["postmaster"]: (morty, units["postmaster"]),
            units["efficiency"]: (morty, units["efficiency"]), units["fps"]: (dwight, spacetime),
            units["aws"]: (bob, builds), units["phases"]: (morty, sdlc), units["errors"]: (dwight, hana),
            units["accounts"]: (brain, natedev),
        }
        # A fixture copy, so a later tuned row in the repository file never changes this test.
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {"CONF_DIR": folder}):
            _ = (Path(folder) / "producer_map.conf").write_text(SEEDED_ROWS, encoding="utf-8")
            for member, (producer, set_by) in expected.items():
                with self.subTest(member=member.name):
                    self.assertEqual(producer_map.producer_for(member.id, members),
                                     producer_map.MappedProducer(producer, set_by))
            for member in (cargo_liner, units["tile"]):
                with self.subTest(member=member.name):
                    self.assertIs(producer_map.producer_for(member.id, members), producer_map.NO_ROW)


if __name__ == "__main__":
    _ = unittest.main()

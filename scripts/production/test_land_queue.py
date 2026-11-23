"""Tests for the line of checkpoint lands."""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.production import land_queue, unit_phases
from zoneinfo import ZoneInfo


class LandQueueTests(unittest.TestCase):
    root: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"GATE_STATE_DIR": str(self.root)}))

    def run_line(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = land_queue.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def batch(self, name: str, *lines: str) -> str:
        path = self.root / f"{name}.txt"
        _ = path.write_text("".join(f"{line}\n" for line in lines))
        return str(path)

    def test_the_runner_takes_each_item_once_in_line_order(self) -> None:
        self.assertIsInstance(land_queue.places(), land_queue.NoLine)
        first = self.batch("first", "unit-board land-line " + "a" * 40 + " --started __STARTED__", "",
                           "phases 5 " + "b" * 40 + " --shrink")
        self.assertEqual(self.run_line("add", "sdlc first", "--batch", first)[0], 0)
        self.assertEqual(self.run_line("add", "builds batch590", "--commit", "c" * 40)[0], 0)
        self.assertEqual(self.run_line("add", "sdlc urgent", "--commit", "d" * 9, "--first")[0], 0)
        self.assertEqual(self.run_line("add", "sdlc middle", "--commit", "e" * 40, "--before", "builds batch590")[0], 0)
        self.assertEqual(self.run_line("list")[1],
                         "1. sdlc urgent: ddddddddd\n2. sdlc first: unit-board land-line aaaaaaaaa, phases 5 bbbbbbbbb\n"
                         + "3. sdlc middle: eeeeeeeee\n4. builds batch590: ccccccccc\n")
        places = land_queue.places()
        assert not isinstance(places, land_queue.NoLine)
        self.assertEqual(land_queue.place_of(places, "b" * 40), land_queue.Place(2, "sdlc first", "phases 5"))
        self.assertEqual(land_queue.place_of(places, "d" * 40), land_queue.Place(1, "sdlc urgent", "sdlc urgent"))
        self.assertIsNone(land_queue.place_of(places, "f" * 40))
        self.assertEqual(self.run_line("move", "builds batch590", "--first")[0], 0)
        self.assertEqual(self.run_line("remove", "sdlc middle")[0], 0)
        taken = [self.run_line("next")[1] for _ in range(4)]
        self.assertEqual(taken, ["builds batch590\n", "sdlc urgent\n", f"sdlc first {Path(first).resolve()}\n", ""])
        self.assertEqual(self.run_line("list")[1], "the land line is empty\n")
        self.assertEqual(land_queue.places(), {})

    def test_next_prints_the_batch_path_stored_for_the_label(self) -> None:
        batch = self.batch("setupwait2", "aws hana-setup-wait " + "a" * 40)
        self.assertEqual(self.run_line("add", "builds setupwait3", "--batch", os.path.relpath(batch)), (0, "", ""))
        self.assertEqual(self.run_line("next"), (0, f"builds setupwait3 {Path(batch).resolve()}\n", ""))

    def test_next_prints_only_the_label_for_an_item_with_only_commits(self) -> None:
        self.assertEqual(self.run_line("add", "builds hashes", "--commit", "a" * 40), (0, "", ""))
        self.assertEqual(self.run_line("next"), (0, "builds hashes\n", ""))

    def test_add_refuses_a_batch_path_with_white_space(self) -> None:
        batch = self.batch("setup wait", "aws hana-setup-wait " + "a" * 40)
        status, output, error = self.run_line("add", "builds setupwait3", "--batch", batch)
        self.assertEqual(status, 1)
        self.assertEqual(output, "")
        self.assertIn("white space", error)
        self.assertIsInstance(land_queue.read_items(), land_queue.NoLine)

    def test_a_bad_request_changes_nothing(self) -> None:
        self.assertEqual(self.run_line("add", "sdlc one", "--commit", "a" * 40)[0], 0)
        empty = self.batch("empty", "no hash here", "unit phase not-a-hash")
        refusals = [("add", "sdlc one", "--commit", "b" * 40), ("add", "sdlc two"), ("add", "sdlc two", "--batch", empty),
                    ("add", "sdlc two", "--commit", "xyz"), ("add", "sdlc two", "--commit", "b" * 40, "--before", "none"),
                    ("move", "none", "--first"), ("remove", "none")]
        for argv in refusals:
            status, _, error = self.run_line(*argv)
            self.assertEqual(status, 1, argv)
            self.assertTrue(error.startswith("land_queue: "), argv)
        for label in ("builds", "builds setup wait", "", "builds  setupwait3", "builds\tsetupwait3"):
            with self.subTest(label=label):
                status, output, error = self.run_line("add", label, "--commit", "b" * 40)
                self.assertEqual((status, output), (1, ""))
                self.assertIn("two words separated by one space", error)
        self.assertEqual(self.run_line("list")[1], "1. sdlc one: aaaaaaaaa\n")

    def test_next_keeps_an_item_with_invalid_fields_first(self) -> None:
        batch = self.batch("setupwait2", "aws hana-setup-wait " + "a" * 40)
        spaced_batch = self.batch("setup wait", "aws hana-setup-wait " + "a" * 40)
        label_error = "two words separated by one space"
        cases = (("builds", batch, label_error),
                 ("builds setup wait", batch, label_error),
                 ("", batch, label_error),
                 ("builds setupwait3", spaced_batch, "must have no white space"))
        for label, stored_batch, reason in cases:
            with self.subTest(label=label, batch=stored_batch):
                with land_queue.edited() as items:
                    items[:] = [{"label": label, "batch": stored_batch, "commits": [], "added": ""},
                                {"label": "builds second", "batch": "", "commits": ["b" * 40], "added": ""}]
                before = land_queue.line_path().read_bytes()
                status, output, error = self.run_line("next")
                self.assertEqual((status, output), (1, ""))
                self.assertIn(reason, error)
                self.assertEqual(land_queue.line_path().read_bytes(), before)

    def test_carried_queue_work_names_its_recorded_production(self) -> None:
        batch = self.batch("carrier", "queue bf4-limits " + "a" * 40)
        for label, expected in (("builds batch638", "builds bf4-limits"),
                                ("other batch638", "other bf4-limits"),
                                ("queue bf4-limits", "bf4-limits"),
                                ("batch638", "bf4-limits"), ("", "bf4-limits")):
            with self.subTest(label=label):
                with land_queue.edited() as items:
                    items[:] = [{"label": label, "batch": batch, "commits": [], "added": ""}]
                places = land_queue.places()
                assert not isinstance(places, land_queue.NoLine)
                place = places["a" * 40]
                self.assertEqual(place.owner, "queue bf4-limits")
                state = land_queue.land_state(land_queue.LandView({}, places, {}, frozenset(), {"b" * 40: place}), "b" * 40)
                self.assertEqual(unit_phases.land_text(state, now=0, zone=ZoneInfo("UTC")),
                                 f"merges with {expected}; 1st in the merge line")

    def test_an_unreadable_line_has_no_places(self) -> None:
        _ = land_queue.line_path().write_text("not json")
        self.assertEqual(land_queue.places(), {})
        _ = land_queue.line_path().write_text('[{"batch": ""}]')
        self.assertEqual(land_queue.places(), {})
        self.assertEqual(self.run_line("next")[0], 1)


if __name__ == "__main__":
    _ = unittest.main()

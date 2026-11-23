from __future__ import annotations

from scripts.lint.memory_test_support import config_environment, write_config_fixture

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Literal, cast, override
from unittest import mock

from scripts.disks import measure, slots
from scripts.disks.measure import DisksData, Folder
from scripts.lib import build_folders, conf

GIB = 1 << 30
DAY = 86400
NOW = 100 * DAY


def folder(name: str, size: int, children: list[Folder] | None = None) -> Folder:
    listed = children or []
    return {"name": name, "size": size, "other": size - sum(child["size"] for child in listed), "children": listed}


DATA: DisksData = {"render_seconds": 0.01, "measured_at": NOW, "keep_bytes": GIB // 4, "drives": [
    {"mount": "/", "tree": folder("/", 900 * GIB, [folder("home/natepiano", 800 * GIB, [folder("slot", 3 * GIB)])])},
    {"mount": "/build", "tree": folder("/build", 500 * GIB, [
        folder("cargo", 200 * GIB, [folder("hana-slot1", 70 * GIB), folder("hana-slot2/debug", 40 * GIB)]),
        folder("slot", 2 * GIB, [folder("hana-slot1", GIB)])])},
    {"mount": "/data", "error": "du failed"},
]}


class TreeSizeTests(unittest.TestCase):
    def test_a_path_takes_the_size_of_its_folder_on_the_deepest_drive(self) -> None:
        cases = {"/build/cargo/hana-slot1": 70 * GIB, "/build/cargo": 200 * GIB, "/home/natepiano/slot": 3 * GIB,
                 "/build/cargo/hana-slot2": 40 * GIB, "/build/cargo/hana-slot2/debug": 40 * GIB,
                 "/build/cargo/hana-slot3": 0, "/data/slot": None, "/build": 500 * GIB}
        self.assertEqual({path: measure.tree_size(DATA, PurePath(path)) for path in cases}, cases)
        self.assertIsNone(measure.tree_size(None, PurePath("/build")))


class ReadSlotsTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = config_environment(folder)
        _ = write_config_fixture(folder / "build_steps.conf",
            "names.integrate=integrate\nowned.default={unit} {step}\nowned.integrate={unit} {step}\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, environment)))

    def test_lease_names_keep_staff_references_separate_from_work_text(self) -> None:
        from scripts.lib.build_steps import StepNames

        names = StepNames({"names.build": "helper", "owned.default": "{unit} {step}"})
        for step, expected in (("build", "helper"), ("", "helper")):
            with self.subTest(step=step):
                use = slots.slot_use({"unit": "unit-id", "showrunner": "showrunner-id",
                                      "work": step, "purpose": "helper"}, names, lambda owner: "builder")
                self.assertEqual(use["name"], "builder " + expected)
                self.assertEqual(use.get("name_parts"), [{"kind": "staff", "text": "builder"},
                    {"kind": "text", "text": " " + expected}])

    def test_copies_group_by_unit_and_report_their_own_use(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "build_folders.conf"
            _ = settings.write_text("# whole: 1..\nfolders.unused_days=2\n")
            for unit, number, days in (("one", 1, 0), ("one", 2, 3), ("two", 1, 1)):
                path = root / "hana" / unit / str(number)
                path.mkdir(parents=True)
                used = NOW - days * DAY
                stamp = path / build_folders.COPY_USE_STAMP
                stamp.touch()
                os.utime(stamp, (used, used))
                lease = {"showrunner": "builds", "unit": unit, "work": "integrate",
                         "purpose": "integrate", "last_use": datetime.fromtimestamp(used, timezone.utc).isoformat()}
                _ = build_folders.lease_path(path).write_text(json.dumps(lease))

            def state(path: Path) -> Literal["held", "free"]:
                return "held" if path.parent.name == "one" and path.name == "1" else "free"

            report = slots.read_slots(root, conf.read(settings), "fixture", None, NOW, state,
                                      owners=lambda owner: owner)
        self.assertEqual(report["root"], str(root))
        first, second = report["groups"]
        self.assertEqual((first["repository"], second["repository"]), ("hana/one", "hana/two"))
        self.assertIsNone(first["maximum"])
        held, expired = first["slots"]
        self.assertEqual(held["used_by"], {"name": "one integrate", "showrunner": "builds", "name_parts": [
            {"kind": "staff", "text": "one"}, {"kind": "text", "text": " integrate"}]})
        self.assertEqual((held.get("key"), expired.get("key"), second["slots"][0].get("key")),
                         ("hana/one/1", "hana/one/2", "hana/two/1"))
        self.assertTrue(expired["needs_review"])
        self.assertFalse(held["needs_review"])
        self.assertFalse(second["slots"][0]["needs_review"])


if __name__ == "__main__":
    _ = unittest.main()

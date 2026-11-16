from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Literal
from unittest import mock

from scripts.disks import measure, slots
from scripts.disks.measure import DisksData, Folder
from scripts.lib import build_slots, conf

GIB = 1 << 30
DAY = 86400
NOW = 100 * DAY


def folder(name: str, size: int, children: list[Folder] | None = None) -> Folder:
    listed = children or []
    return {"name": name, "size": size, "other": size - sum(child["size"] for child in listed), "children": listed}


DATA: DisksData = {"measured_at": NOW, "keep_bytes": GIB // 4, "drives": [
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
    def test_slots_group_by_repository_with_size_use_and_the_nightly_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings_file = Path(directory) / "build_folders.conf"
            lines = ("slots.unused_days=7", "slots.host.default_max=2", "slots.host.hana.max=8")
            _ = settings_file.write_text("".join(f"# whole: 1..\n{line}\n" for line in lines))
            root = Path(directory) / "slot"
            root.mkdir()
            for name, purpose, days in (("hana-slot1", "fps", 0), ("hana-slot2", "verify", 9), ("hana-slot10", "", 2),
                                        ("organon-slot1", "gate", 8)):
                (root / name).mkdir()
                lease = {"showrunner": "builds", "unit": "report", "purpose": purpose,
                         "last_use": datetime.fromtimestamp(NOW - days * DAY, timezone.utc).isoformat()}
                _ = build_slots.lease_path(root / name).write_text(json.dumps(lease))
            (root / "notes").mkdir()

            def state(path: Path) -> Literal["held", "free"]:
                return "held" if path.name in {"hana-slot1", "organon-slot1"} else "free"

            report = slots.read_slots(root, Path("/build/cargo"), conf.read(settings_file), "host", DATA, NOW, state)
        self.assertEqual(report["root"], str(root))
        self.assertEqual([(group["repository"], group["maximum"], [row["name"] for row in group["slots"]])
                          for group in report["groups"]],
                         [("hana", 8, ["hana-slot1", "hana-slot2", "hana-slot10"]), ("organon", 2, ["organon-slot1"])])
        first, second, tenth = report["groups"][0]["slots"]
        self.assertEqual((first["size"], first["last_used"], first["used_by"], first["deleted_tonight"]),
                         (70 * GIB, NOW, {"purpose": "fps", "unit": "report", "showrunner": "builds"}, False))
        self.assertEqual((second["size"], second["used_by"], second["deleted_tonight"]), (40 * GIB, None, True))
        self.assertEqual((tenth["size"], tenth["last_used"], tenth["deleted_tonight"]), (0, NOW - 2 * DAY, False))
        self.assertFalse(report["groups"][1]["slots"][0]["deleted_tonight"])

    def test_no_slot_root_in_the_config_gives_no_report(self) -> None:
        class Settings:
            def values(self) -> dict[str, str]:
                return {"roots.slot_cargo": "/build/cargo", "slots.unused_days": "7"}

        with mock.patch.object(conf, "read_or_repository", return_value=Settings()):
            self.assertIsNone(slots.slot_report(NOW))


if __name__ == "__main__":
    _ = unittest.main()

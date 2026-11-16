from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast

from scripts.disks import disks_page, measure
from scripts.lib import file_lock

GIB = 1 << 30
KEEP = 256 << 20
DU_OUTPUT = "\n".join((
    f"{40 * GIB}\t/fixture/home/natepiano/rust/target",
    f"{50 * GIB}\t/fixture/home/natepiano/rust",
    f"{2 * GIB}\t/fixture/home/natepiano/.cache",
    f"{60 * GIB}\t/fixture/home/natepiano",
    f"{60 * GIB + 100}\t/fixture/home",
    f"{5 * GIB}\t/fixture/nix/store",
    f"{5 * GIB + 10}\t/fixture/nix",
    f"{70 * GIB}\t/fixture",
)) + "\n"


def settings(folder: Path, *drives: str) -> measure.MeasureSettings:
    return measure.MeasureSettings(drives, KEEP, "2G", 30, folder)


def no_floors(mounts: Sequence[str]) -> dict[int, int | None]:
    return {os.stat(mount).st_dev: 300 * GIB for mount in mounts}


class TreeTests(unittest.TestCase):
    def tree(self) -> measure.Folder:
        return measure.folder_tree("/fixture", measure.parse_du(DU_OUTPUT), KEEP)

    def test_each_folder_hangs_from_its_nearest_listed_ancestor_by_size(self) -> None:
        root = self.tree()
        self.assertEqual((root["name"], root["size"]), ("/fixture", 70 * GIB))
        self.assertEqual([child["name"] for child in root["children"]], ["home/natepiano", "nix/store"])
        natepiano = root["children"][0]
        self.assertEqual([child["name"] for child in natepiano["children"]], ["rust", ".cache"])
        self.assertEqual([child["name"] for child in natepiano["children"][0]["children"]], ["target"])

    def test_other_is_the_size_less_the_listed_children(self) -> None:
        root = self.tree()
        self.assertEqual(root["other"], 70 * GIB - (60 * GIB + 100) - (5 * GIB + 10))
        natepiano = root["children"][0]
        self.assertEqual(natepiano["other"], 100 + 60 * GIB - 52 * GIB)
        self.assertEqual(natepiano["children"][0]["other"], 10 * GIB)

    def test_a_single_child_joins_its_parent_only_while_the_parent_files_are_below_the_limit(self) -> None:
        root = self.tree()
        home = root["children"][0]
        self.assertEqual((home["name"], home["size"]), ("home/natepiano", 60 * GIB + 100))
        rust = home["children"][0]
        self.assertEqual(rust["name"], "rust", "rust keeps 10 GiB of its own files, so target stays its child")

    def test_the_root_never_joins_its_only_child(self) -> None:
        root = measure.folder_tree("/fixture", {"/fixture": 3 * GIB, "/fixture/data": 3 * GIB}, KEEP)
        self.assertEqual([child["name"] for child in root["children"]], ["data"])

    def test_du_command_runs_at_idle_priority_under_the_memory_cap(self) -> None:
        command = measure.du_command("/build", settings(Path("/unused"), "/build"))
        self.assertEqual(command[:7], ["systemd-run", "--user", "--scope", "--quiet", "-p", "MemoryMax=2G", "--"])
        self.assertEqual(command[7:], ["nice", "-n", "19", "ionice", "-c3", "du", "-x", "-B1",
                                       f"--threshold={KEEP}", "/build"])


class MeasureTests(unittest.TestCase):
    def temporary(self) -> Path:
        """A page folder for one test; the measure's printed result stays out of the test output."""
        _ = self.enterContext(redirect_stdout(io.StringIO()))
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def data(self, folder: Path) -> measure.DisksData:
        return cast(measure.DisksData, json.loads((folder / disks_page.DATA_FILE).read_text()))

    def test_a_measure_writes_each_drive_with_capacity_floor_and_tree(self) -> None:
        folder = self.temporary()

        def runner(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
            self.assertEqual(timeout, 30)
            return subprocess.CompletedProcess(argv, 1, f"{KEEP * 2}\t{argv[-1]}\n", "du: cannot read 'x'")

        self.assertEqual(measure.measure(settings(folder, str(folder)), runner, no_floors), 0)
        drive = self.data(folder)["drives"][0]
        self.assertEqual(drive.get("floor"), 300 * GIB)
        self.assertGreater(drive.get("total", 0), 0)
        self.assertEqual(drive.get("tree", {}).get("size"), KEEP * 2)
        self.assertNotIn("error", drive)

    def test_a_timeout_records_an_error_and_the_next_drive_still_runs(self) -> None:
        folder = self.temporary()
        calls: list[str] = []

        def runner(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
            calls.append(argv[-1])
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(list(argv), timeout)
            return subprocess.CompletedProcess(argv, 0, f"{KEEP}\t{argv[-1]}\n", "")

        self.assertEqual(measure.measure(settings(folder, str(folder), str(folder)), runner, no_floors), 0)
        first, second = self.data(folder)["drives"]
        self.assertEqual(first.get("error"), "the measure stopped after 30 seconds")
        self.assertNotIn("error", second)

    def test_a_failed_du_records_its_last_error_line(self) -> None:
        folder = self.temporary()

        def runner(argv: Sequence[str], _timeout: int) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 2, "", "first\nMemoryMax: bad value")

        _ = measure.measure(settings(folder, str(folder)), runner, no_floors)
        self.assertEqual(self.data(folder)["drives"][0].get("error"), "du exited 2: MemoryMax: bad value")

    def test_a_second_measure_exits_while_one_holds_the_lock(self) -> None:
        folder = self.temporary()

        def runner(argv: Sequence[str], _timeout: int) -> subprocess.CompletedProcess[str]:
            self.fail(f"a busy lock ran {argv}")

        with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as held:
            self.assertIs(held, file_lock.LockResult.ACQUIRED)
            self.assertEqual(measure.measure(settings(folder, str(folder)), runner, no_floors),
                             measure.LOCK_BUSY_EXIT)
        self.assertFalse((folder / disks_page.DATA_FILE).exists())


if __name__ == "__main__":
    _ = unittest.main()

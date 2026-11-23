from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Sequence
from contextlib import redirect_stdout
from pathlib import Path, PurePath
from typing import cast
from unittest import mock

from scripts.disks import disks_page, measure
from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic

GIB = 1 << 30
KEEP = 256 << 20


def dust_folder(name: str, size: int, *children: measure.DustFolder) -> measure.DustFolder:
    return {"name": name, "size": f"{size}B", "children": list(children)}


DUST_OUTPUT = json.dumps(dust_folder("/fixture/", 70 * GIB,
    dust_folder("/fixture/home", 60 * GIB + 100,
        dust_folder("/fixture/home/natepiano", 60 * GIB,
            dust_folder("/fixture/home/natepiano/rust", 50 * GIB,
                dust_folder("/fixture/home/natepiano/rust/target", 40 * GIB)),
            dust_folder("/fixture/home/natepiano/.cache", 2 * GIB))),
    dust_folder("/fixture/nix", 5 * GIB + 10,
        dust_folder("/fixture/nix/store", 5 * GIB))))


def settings(folder: Path, *drives: str) -> measure.MeasureSettings:
    return measure.MeasureSettings(drives, KEEP, "2G", 30, folder, 7, 128)


def no_floors(mounts: Sequence[str]) -> dict[int, int | None]:
    return {os.stat(mount).st_dev: 300 * GIB for mount in mounts}


class TreeTests(unittest.TestCase):
    def tree(self) -> measure.Folder:
        return measure.folder_tree("/fixture", measure.parse_dust(DUST_OUTPUT), KEEP)

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

    def test_dust_command_runs_at_idle_priority_under_the_memory_cap(self) -> None:
        config = Path(measure.__file__).with_name("dust.toml")
        for name in ("", "builds-detached-r1.slice"):
            with self.subTest(slice=name), mock.patch.dict(os.environ, {"DETACHED_RUN_SLICE": name}):
                command = measure.dust_command("/build", settings(Path("/unused"), "/build"))
                slice_args = [f"--slice={name}"] if name else []
                self.assertEqual(command, ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                    *slice_args, "-p", "MemoryMax=2G", "--", "nice", "-n", "19", "dust", "-x", "-D", "-j", "-P",
                    "-o", "b", "-z", str(KEEP - 1), "-d", "7", "-n", "128", "--config", str(config), "/build"])
        self.assertTrue(config.is_file())


class MountTests(unittest.TestCase):
    def test_a_path_under_a_mount_of_a_subfolder_is_found_under_its_whole_file_system(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            table = Path(directory) / "mountinfo"
            _ = table.write_text("\n".join((
                "1 0 259:2 / / rw - ext4 /dev/root rw",
                "2 1 254:0 / /build rw - ext4 /dev/mapper/build rw",
                "3 1 254:0 /session /session rw - ext4 /dev/mapper/build rw",
                "4 1 254:0 /my\\040disk /space\\040here rw - ext4 /dev/mapper/build rw",
                "short line")) + "\n")
            mounts = measure.read_mounts(table)
            self.assertEqual(measure.read_mounts(Path(directory) / "absent"), ())
        self.assertEqual(len(mounts), 4)
        cases = {"/session/fps/run": "/build/session/fps/run", "/session": "/build/session", "/build/cargo": "/build/cargo",
                 "/home/natepiano": "/home/natepiano", "/space here/x": "/build/my disk/x"}
        self.assertEqual({path: str(measure.drive_path(PurePath(path), mounts)) for path in cases}, cases)
        # With no mount of the whole file system, the path stays as it is.
        self.assertEqual(measure.drive_path(PurePath("/session/fps"), mounts[2:3]), PurePath("/session/fps"))
        self.assertEqual(measure.drive_path(PurePath("/session/fps"), ()), PurePath("/session/fps"))


class MeasureTests(unittest.TestCase):
    def temporary(self) -> Path:
        """A page folder for one test; the measure's printed result stays out of the test output."""
        _ = self.enterContext(redirect_stdout(io.StringIO()))
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def data(self, folder: Path) -> measure.DisksData:
        return cast(measure.DisksData, json.loads((folder / disks_page.DATA_FILE).read_text()))

    def test_a_failed_walk_runs_in_a_scope_that_collects(self) -> None:
        folder = self.temporary()

        def runner(argv: Sequence[str], _timeout: int) -> subprocess.CompletedProcess[str]:
            self.assertIn("--collect", argv[:argv.index("--")])
            return subprocess.CompletedProcess(argv, 1, "", "walk failed")

        self.assertEqual(measure.measure(settings(folder, str(folder)), runner, no_floors), 0)
        self.assertEqual(self.data(folder)["drives"][0].get("error"), "dust exited 1: walk failed")

    def test_a_measure_publishes_the_next_generation_before_removing_the_active_file(self) -> None:
        folder = self.temporary()
        active = folder / disks_page.ACTIVE_FILE
        write = write_text_atomic

        def publish(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
            write(path, text, new_file_mode=new_file_mode)
            if path.name == disks_page.DATA_FILE:
                self.assertEqual(int(active.read_text()), self.data(folder).get("generation"))
                with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as lock:
                    self.assertIs(lock, file_lock.LockResult.BUSY)
                with file_lock.exclusive(folder / disks_page.COORD_FILE, wait=False) as coordination:
                    self.assertIs(coordination, file_lock.LockResult.BUSY)

        def runner(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
            self.assertEqual(timeout, 30)
            self.assertEqual(int(active.read_text()), expected)
            with file_lock.exclusive(folder / disks_page.COORD_FILE, wait=False) as coordination:
                self.assertIs(coordination, file_lock.LockResult.ACQUIRED)
            return subprocess.CompletedProcess(argv, 0, json.dumps(dust_folder(argv[-1], KEEP)), "")

        read_generation = disks_page.published_generation

        def generation_under_locks(path: Path) -> int:
            for name in (disks_page.LOCK_FILE, disks_page.COORD_FILE):
                with file_lock.exclusive(path / name, wait=False) as held:
                    self.assertIs(held, file_lock.LockResult.BUSY)
            return read_generation(path)

        for previous, expected in ((None, 1), ({"measured_at": 900}, 1), ({"generation": 7}, 8)):
            with self.subTest(previous=previous):
                if previous is not None:
                    write(folder / disks_page.DATA_FILE, json.dumps(previous))
                with mock.patch.object(measure, "write_text_atomic", side_effect=publish), \
                        mock.patch.object(disks_page, "published_generation", side_effect=generation_under_locks):
                    self.assertEqual(measure.measure(settings(folder, str(folder)), runner, no_floors), 0)
                self.assertEqual(self.data(folder).get("generation"), expected)
                self.assertFalse(active.exists())

    def test_a_failed_measure_removes_the_active_file_and_keeps_the_published_generation(self) -> None:
        folder = self.temporary()
        published = folder / disks_page.DATA_FILE
        _ = published.write_text('{"generation":7}\n')

        def fail(mounts: Sequence[str]) -> dict[int, int | None]:
            self.assertEqual(tuple(mounts), (str(folder),))
            self.assertEqual((folder / disks_page.ACTIVE_FILE).read_text(), "8\n")
            raise OSError("the measure failed")

        with self.assertRaisesRegex(OSError, "the measure failed"):
            _ = measure.measure(settings(folder, str(folder)), floors=fail)
        self.assertFalse((folder / disks_page.ACTIVE_FILE).exists())
        self.assertEqual(self.data(folder).get("generation"), 7)
        with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as released:
            self.assertIs(released, file_lock.LockResult.ACQUIRED)

    def test_an_inherited_reservation_keeps_its_generation(self) -> None:
        folder = self.temporary()
        lock = file_lock.take_exclusive(folder / disks_page.LOCK_FILE)
        assert isinstance(lock, file_lock.ExclusiveLock)
        _ = (folder / disks_page.ACTIVE_FILE).write_text("8\n")
        _ = (folder / disks_page.DATA_FILE).write_text('{"generation":99}')

        def runner(argv: Sequence[str], _timeout: int) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(dust_folder(argv[-1], KEEP)), "")

        with mock.patch.object(disks_page, "published_generation", side_effect=AssertionError("generation selected again")):
            self.assertEqual(measure.measure(settings(folder, str(folder)), runner, no_floors,
                                             reservation=measure.MeasureReservation(8, lock)), 0)
        self.assertEqual(self.data(folder).get("generation"), 8)
        self.assertFalse((folder / disks_page.ACTIVE_FILE).exists())
        with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as released:
            self.assertIs(released, file_lock.LockResult.ACQUIRED)

    def test_a_failed_reserved_measure_publishes_nothing_and_releases_the_lock(self) -> None:
        folder = self.temporary()
        lock = file_lock.take_exclusive(folder / disks_page.LOCK_FILE)
        assert isinstance(lock, file_lock.ExclusiveLock)
        _ = (folder / disks_page.ACTIVE_FILE).write_text("1\n")

        def fail(_mounts: Sequence[str]) -> dict[int, int | None]:
            raise OSError("the measure failed")

        with self.assertRaisesRegex(OSError, "the measure failed"):
            _ = measure.measure(settings(folder, str(folder)), floors=fail,
                                reservation=measure.MeasureReservation(1, lock))
        self.assertFalse((folder / disks_page.ACTIVE_FILE).exists())
        self.assertFalse((folder / disks_page.DATA_FILE).exists())
        with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as released:
            self.assertIs(released, file_lock.LockResult.ACQUIRED)

    def test_measured_at_records_the_end_of_the_drive_measures(self) -> None:
        folder = self.temporary()
        clock = [1000.0]

        def runner(argv: Sequence[str], _timeout: int) -> subprocess.CompletedProcess[str]:
            clock[0] = 1056.0
            return subprocess.CompletedProcess(argv, 0, json.dumps(dust_folder(argv[-1], KEEP)), "")

        with mock.patch("scripts.disks.measure.time.time", side_effect=lambda: clock[0]):
            self.assertEqual(measure.measure(settings(folder, str(folder)), runner, no_floors), 0)
        self.assertGreaterEqual(self.data(folder)["measured_at"], 1056.0)
        self.assertGreater(self.data(folder)["render_seconds"], 0)

    def test_a_publish_failure_removes_the_active_file_and_releases_the_lock(self) -> None:
        folder = self.temporary()
        write = write_text_atomic

        def publish(path: Path, text: str, *, new_file_mode: int | None = None) -> None:
            if path.name == disks_page.DATA_FILE:
                raise OSError("cannot publish")
            write(path, text, new_file_mode=new_file_mode)

        def runner(argv: Sequence[str], _timeout: int) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(dust_folder(argv[-1], KEEP)), "")

        with mock.patch.object(measure, "write_text_atomic", side_effect=publish), \
                self.assertRaisesRegex(OSError, "cannot publish"):
            _ = measure.measure(settings(folder, str(folder)), runner, no_floors)
        self.assertFalse((folder / disks_page.ACTIVE_FILE).exists())
        self.assertFalse((folder / disks_page.DATA_FILE).exists())
        with file_lock.exclusive(folder / disks_page.LOCK_FILE, wait=False) as released:
            self.assertIs(released, file_lock.LockResult.ACQUIRED)

    def test_the_command_adopts_the_reserved_descriptor_and_generation(self) -> None:
        folder = self.temporary()
        lock = file_lock.take_exclusive(folder / disks_page.LOCK_FILE)
        assert isinstance(lock, file_lock.ExclusiveLock)
        _ = (folder / disks_page.ACTIVE_FILE).write_text("8\n")
        _ = (folder / disks_page.DATA_FILE).write_text('{"generation":7}')
        with mock.patch.object(measure, "read_settings", return_value=settings(folder, str(folder))), \
                mock.patch.object(measure, "measure", return_value=0) as run:
            try:
                self.assertEqual(measure.main(["--generation", "8", "--lock-fd", str(lock.descriptor)]), 0)
                run.assert_called_once_with(settings(folder, str(folder)), reservation=measure.MeasureReservation(8, lock))
            finally:
                lock.release()

    def test_a_measure_writes_each_drive_with_capacity_floor_and_tree(self) -> None:
        folder = self.temporary()

        def runner(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
            self.assertEqual(timeout, 30)
            return subprocess.CompletedProcess(argv, 0, json.dumps(dust_folder(argv[-1], KEEP * 2)),
                                               "Did not have permissions for all directories")

        self.assertEqual(measure.measure(settings(folder, str(folder)), runner, no_floors), 0)
        self.assertGreater(self.data(folder)["render_seconds"], 0)
        drive = self.data(folder)["drives"][0]
        self.assertEqual(drive.get("floor"), 300 * GIB)
        self.assertGreater(drive.get("total", 0), 0)
        self.assertEqual(drive.get("tree", {}).get("size"), KEEP * 2)
        self.assertNotIn("error", drive)

    def test_a_timeout_records_an_error_and_the_next_drive_still_runs(self) -> None:
        folder = self.temporary()
        second_mount = folder / "second"
        second_mount.mkdir()

        def runner(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
            if argv[-1] == str(folder):
                raise subprocess.TimeoutExpired(list(argv), timeout)
            return subprocess.CompletedProcess(argv, 0, json.dumps(dust_folder(argv[-1], KEEP)), "")

        self.assertEqual(measure.measure(settings(folder, str(folder), str(second_mount)), runner, no_floors), 0)
        first, second = self.data(folder)["drives"]
        self.assertEqual(first.get("error"), "the measure stopped after 30 seconds")
        self.assertNotIn("error", second)

    def test_a_failed_dust_records_its_last_error_line(self) -> None:
        folder = self.temporary()
        for code, output, error, expected in (
            (2, "", "first\nMemoryMax: bad value", "dust exited 2: MemoryMax: bad value"),
            (1, "", "first\nNo such file or directory: /missing", "dust exited 1: No such file or directory: /missing"),
            (0, "", "", "dust exited 0: no output"),
            (2, json.dumps(dust_folder(str(folder), KEEP)), "walk failed", "dust exited 2: walk failed"),
        ):
            with self.subTest(code=code, error=error):
                def runner(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
                    self.assertEqual(timeout, 30)
                    return subprocess.CompletedProcess(argv, code, output, error)

                _ = measure.measure(settings(folder, str(folder)), runner, no_floors)
                self.assertEqual(self.data(folder)["drives"][0].get("error"), expected)

    def test_both_drives_start_before_either_walk_ends_and_keep_the_config_order(self) -> None:
        folder = self.temporary()
        second_mount = folder / "second"
        second_mount.mkdir()
        barrier = threading.Barrier(2)

        def runner(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
            _ = barrier.wait(timeout=min(timeout, 5))
            size = KEEP if argv[-1] == str(folder) else KEEP * 2
            return subprocess.CompletedProcess(argv, 0, json.dumps(dust_folder(argv[-1], size)), "")

        self.assertEqual(measure.measure(settings(folder, str(folder), str(second_mount)), runner, no_floors), 0)
        drives = self.data(folder)["drives"]
        self.assertEqual([drive["mount"] for drive in drives], [str(folder), str(second_mount)])
        self.assertEqual([drive.get("tree", {}).get("size") for drive in drives], [KEEP, KEEP * 2])

    def test_a_start_failure_records_why_the_measure_did_not_start(self) -> None:
        folder = self.temporary()

        def runner(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError(f"dust absent: {argv[-1]} ({timeout} seconds)")

        _ = measure.measure(settings(folder, str(folder)), runner, no_floors)
        self.assertEqual(self.data(folder)["drives"][0].get("error"),
                         f"the measure did not start: dust absent: {folder} (30 seconds)")

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

"""Prove that removal keeps mounted filesystems."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
from typing import cast
import unittest
from unittest import mock

from scripts.lib import build_folder_lock, build_folders, removal_guard
from scripts.lib.removal_bwrap_test_support import BuildMount, LINUX_CHECKOUT, RemovalSandbox, run_mac_fixture_command
from scripts.lib.test_build_folders_bwrap import ClaimCase, ToolFolderRemovalFixture, check_refusal, read_guard
from scripts.lint.test_removal_bwrap import PROBE_MOUNT


RUN_MOVED_PARENT = """
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from scripts.lib.test_build_folders_bwrap_mounts import remove_after_parent_moves

remove_after_parent_moves(Path(sys.argv[2]))
"""


PROBE_MAC_MOUNT = """
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from scripts.lib import removal_guard

mount = Path(sys.argv[2])
assert removal_guard.identity(mount.stat()) in removal_guard.read().mounts.points, mount
probe = mount / "write-probe"
probe.touch(exist_ok=False)
probe.unlink()
"""


def remove_after_parent_moves(candidate: Path) -> None:
    """Keep the original parent open when its path changes."""
    guard = read_guard(candidate)
    parent = candidate.parent
    moved = parent.with_name("moved")
    with build_folder_lock.folder_parent(candidate, parent, create=False) as directory:
        _ = parent.rename(moved)
        parent.mkdir()
        check_refusal(guard, moved / candidate.name, removal_guard.RefusalReason.MOUNT)
        assert not build_folders.remove_build_folder(candidate, dir_fd=directory, repair_access=True)


@unittest.skipUnless(sys.platform in ("linux", "darwin"), "requires Linux or macOS")
class ToolFolderMountRemovalTests(ToolFolderRemovalFixture):
    def disk_image(self, *arguments: str, allow_skip: bool = False) -> str:
        command = ["/usr/bin/hdiutil", *arguments]
        try:
            result = run_mac_fixture_command(command, timeout=20)
        except (FileNotFoundError, PermissionError) as error:
            if allow_skip:
                self.skipTest(f"hdiutil {arguments[0]} is unavailable: {error}")
            raise
        detail = f"{shlex.join(command)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        if allow_skip and result.returncode and any(reason in (result.stderr or result.stdout).lower() for reason in (
                "permission denied", "operation not permitted", "not authorized")):
            self.skipTest(f"hdiutil {arguments[0]} cannot use the fixture: {detail}")
        self.assertEqual(result.returncode, 0, detail)
        return result.stdout

    def attached_images(self) -> list[dict[str, object]]:
        reply = cast(dict[str, list[dict[str, object]]], plistlib.loads(self.disk_image("info", "-plist").encode()))
        return reply["images"]

    def current_mount(self, mount: Path, moved_mount: Path | None) -> Path | None:
        # The mount table can still show the original path after a rename.
        return next((path for path in (moved_mount, mount) if path is not None and path.is_mount()), None)

    def cleanup_command(self, attempts: list[str], *arguments: str) -> bool:
        command = list(arguments)
        try:
            result = run_mac_fixture_command(command, timeout=20)
        except AssertionError as error:
            attempts.append(str(error))
            return False
        except OSError as error:
            attempts.append(f"{shlex.join(command)}\ncommand did not start: {error}")
            return False
        attempts.append("\n".join([shlex.join(command), f"exit status: {result.returncode}",
                                   "stdout:", result.stdout, "stderr:", result.stderr]))
        return result.returncode == 0

    def detach_image_device(self, image: Path, root: Path, attempts: list[str]) -> None:
        for attached in self.attached_images():
            if attached.get("image-path") != str(image):
                continue
            entities = cast(list[dict[str, str]], attached["system-entities"])
            points = (Path(point) for entry in entities if (point := entry.get("mount-point")))
            for point in points:
                target = point.resolve()
                if not point.is_absolute() or target == root or not target.is_relative_to(root):
                    return
            devices = {device for entry in entities if (device := entry.get("dev-entry"))
                       and re.fullmatch(r"/dev/disk[0-9]+", device)}
            if len(devices) == 1:
                _ = self.cleanup_command(attempts, "/usr/bin/hdiutil", "detach", "-force", devices.pop())
            return

    def detach_image(self, image: Path, mount: Path, moved_mount: Path | None,
                     images: list[dict[str, object]], attempts: list[str]) -> None:
        root = image.parent.resolve()
        expected_points = {path.resolve() for path in (mount, moved_mount) if path is not None}
        for attached in images:
            if attached.get("image-path") != str(image):
                continue
            entities = cast(list[dict[str, str]], attached["system-entities"])
            points = {Path(point).resolve() for entry in entities if (point := entry.get("mount-point"))
                      and Path(point).is_absolute()}
            if not points & expected_points or any(
                    point == root or not point.is_relative_to(root) for point in points):
                continue
            current = self.current_mount(mount, moved_mount)
            if current is None:
                return
            target = current.resolve()
            if target == root or not target.is_relative_to(root):
                return
            if not self.cleanup_command(attempts, "/usr/bin/hdiutil", "detach", "-force", str(target)):
                if target.resolve() == target and target.is_mount():
                    _ = self.cleanup_command(attempts, "/sbin/umount", "-f", str(target))
                self.detach_image_device(image, root, attempts)
            return

    def assert_no_images(self, image: Path, mount: Path, moved_mount: Path | None,
                         images: list[dict[str, object]], attempts: list[str]) -> None:
        root = image.parent.parent
        remaining: list[str] = []
        for attached in images:
            image_path = attached.get("image-path")
            if not isinstance(image_path, str) or not Path(image_path).is_relative_to(root):
                continue
            if image_path == str(image) and (current := self.current_mount(mount, moved_mount)):
                remaining.append(str(current))
                continue
            entities = cast(list[dict[str, str]], attached["system-entities"])
            paths = [entry["mount-point"] for entry in entities if "mount-point" in entry]
            remaining.extend(paths or [image_path])
        self.assertFalse(remaining, "\n".join([f"disk images still attached: {', '.join(remaining)}", *attempts]))

    def cleanup_image(self, image: Path, mount: Path, moved_mount: Path | None) -> None:
        attempts: list[str] = []
        try:
            self.detach_image(image, mount, moved_mount, self.attached_images(), attempts)
        except AssertionError as error:
            attempts.append(str(error))
        try:
            remaining = self.attached_images()
            if any(entry.get("image-path") == str(image) for entry in remaining):
                try:
                    self.detach_image(image, mount, moved_mount, remaining, attempts)
                except AssertionError as error:
                    attempts.append(str(error))
                remaining = self.attached_images()
        except AssertionError as error:
            raise AssertionError("\n".join([str(error), *attempts])) from error
        self.assert_no_images(image, mount, moved_mount, remaining, attempts)

    @contextmanager
    def attach_image(self, image: Path, mount: Path, moved_mount: Path | None = None) -> Generator[None]:
        with ExitStack() as cleanup:
            _ = cleanup.callback(self.cleanup_image, image, mount, moved_mount)
            _ = self.disk_image("attach", "-nobrowse", "-mountpoint", str(mount),
                                "-owners", "off", "-plist", str(image), allow_skip=True)
            self.assertTrue(mount.is_mount(), f"hdiutil did not mount {mount}")
            yield

    @contextmanager
    def mount_canary(self, sandbox: RemovalSandbox, candidate: Path, *, moved: Path | None = None) -> Generator[Path]:
        mount = candidate / "unit"
        local = sandbox.host_path(mount)
        local.mkdir()
        with ExitStack() as cleanup:
            if sys.platform == "darwin":
                image = sandbox.fixture / "removal.dmg"
                _ = self.disk_image("create", "-size", "16m", "-fs", "HFS+", "-layout", "NONE",
                                    "-type", "UDIF", str(image), allow_skip=True)
                moved_mount = sandbox.host_path(moved / "unit") if moved is not None else None
                cleanup.enter_context(self.attach_image(image, local, moved_mount))
                keep = local / "output"
                probe = (PROBE_MAC_MOUNT, str(sandbox.checkout), str(mount))
            else:
                keep = sandbox.bind_source / "output"
                probe = (PROBE_MOUNT, str(mount))
            self.canary(keep)
            result = self.run_python(sandbox, candidate, *probe)
            self.assert_child_ok(result)
            yield keep

    def test_interrupted_image_attach_detaches_only_the_private_image(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        fixture = root / "fixture"
        image = fixture / "removal.dmg"
        mount = fixture / "mount"
        other = {"image-path": str(root.parent / "other.dmg"), "system-entities": [{"dev-entry": "/dev/disk8"}]}
        attached = {"image-path": str(image), "system-entities": [
            {"dev-entry": "/dev/disk9"},
            {"dev-entry": "/dev/disk9s1", "mount-point": str(mount)},
        ]}
        info = plistlib.dumps({"images": [other, attached]}).decode()
        clean = plistlib.dumps({"images": [other]}).decode()
        with (mock.patch.object(Path, "is_mount", return_value=True),
              mock.patch.object(subprocess, "run", side_effect=[
                  KeyboardInterrupt(),
                  subprocess.CompletedProcess([], 0, info, ""),
                  subprocess.CompletedProcess([], 0, "", ""),
                  subprocess.CompletedProcess([], 0, clean, ""),
              ]) as run):
            with self.assertRaises(KeyboardInterrupt), self.attach_image(image, mount):
                self.fail("the interrupted attach returned")
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["/usr/bin/hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), "-owners", "off", "-plist", str(image)],
            ["/usr/bin/hdiutil", "info", "-plist"],
            ["/usr/bin/hdiutil", "detach", "-force", str(mount)],
            ["/usr/bin/hdiutil", "info", "-plist"],
        ])

    def test_detaches_at_the_moved_mount_when_info_has_the_old_path(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        mount = image.parent / "unit/proof/unit"
        moved = image.parent / "moved/proof/unit"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [{"mount-point": str(mount)}]},
        ]}).decode()
        with (mock.patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, info, ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]) as run, mock.patch.object(Path, "is_mount", autospec=True, return_value=True) as is_mount):
            self.detach_image(image, mount, moved, self.attached_images(), [])
        is_mount.assert_called_once_with(moved)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/hdiutil", "detach", "-force", str(moved)])

    def test_reports_a_disk_image_that_stays_attached(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        mount = image.parent / "unit/proof/unit"
        moved = image.parent / "moved/proof/unit"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [
                {"dev-entry": "/dev/disk9"},
                {"mount-point": str(mount)},
            ]},
        ]}).decode()
        commands = [
            (["/usr/bin/hdiutil", "detach", "-force", str(moved)], 1, "path output", "path error"),
            (["/sbin/umount", "-f", str(moved)], 2, "unmount output", "unmount error"),
            (["/usr/bin/hdiutil", "detach", "-force", "/dev/disk9"], 3, "device output", "device error"),
        ]
        query = subprocess.CompletedProcess([], 0, info, "")
        failures = [subprocess.CompletedProcess(*command) for command in commands]
        with (mock.patch.object(subprocess, "run", side_effect=[
                  query, *failures[:2], query, failures[2], query,
                  *failures[:2], query, failures[2], query,
              ]),
              mock.patch.object(Path, "is_mount", return_value=True),
              self.assertRaises(AssertionError) as failure):
            self.cleanup_image(image, mount, moved)
        detail = str(failure.exception)
        self.assertIn(f"disk images still attached: {moved}", detail)
        for command, status, stdout, stderr in commands:
            attempt = f"{shlex.join(command)}\nexit status: {status}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            self.assertEqual(detail.count(attempt), 2, detail)

    def test_failed_path_detach_uses_the_moved_path_and_a_fresh_device_match(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        mount = image.parent / "unit/proof/unit"
        moved = image.parent / "moved/proof/unit"
        initial: list[dict[str, object]] = [{"image-path": str(image), "system-entities": [
            {"dev-entry": "/dev/disk8"},
            {"mount-point": str(mount)},
        ]}]
        other = {"image-path": str(image.with_suffix(".other")), "system-entities": [{"dev-entry": "/dev/disk8"}]}
        matched = {"image-path": str(image), "system-entities": [{"dev-entry": "/dev/disk9"}]}
        outside = {"image-path": str(image), "system-entities": [
            {"dev-entry": "/dev/disk9", "mount-point": str(image.parent.parent / "outside")},
        ]}
        for fresh, device in (([other, matched], "/dev/disk9"), ([other], None), ([outside], None)):
            with self.subTest(fresh=fresh):
                reply = plistlib.dumps({"images": fresh}).decode()
                results = [
                    subprocess.CompletedProcess([], 1, "", "no such disk"),
                    subprocess.CompletedProcess([], 0, "", ""),
                    subprocess.CompletedProcess([], 0, reply, ""),
                ]
                commands = [
                    ["/usr/bin/hdiutil", "detach", "-force", str(moved)],
                    ["/sbin/umount", "-f", str(moved)],
                    ["/usr/bin/hdiutil", "info", "-plist"],
                ]
                if device is not None:
                    results.append(subprocess.CompletedProcess([], 0, "", ""))
                    commands.append(["/usr/bin/hdiutil", "detach", "-force", device])
                with (mock.patch.object(subprocess, "run", side_effect=results) as run,
                      mock.patch.object(Path, "is_mount", autospec=True, return_value=True) as is_mount):
                    self.detach_image(image, mount, moved, initial, [])
                self.assertEqual([call.args[0] for call in run.call_args_list], commands)
                self.assertEqual(is_mount.call_args_list, [mock.call(moved), mock.call(moved)])

    def test_final_query_recovers_an_image_after_an_info_timeout(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        mount = image.parent / "unit"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [
                {"dev-entry": "/dev/disk9", "mount-point": str(mount)},
            ]},
        ]}).decode()
        query = ["/usr/bin/hdiutil", "info", "-plist"]
        with (mock.patch.object(Path, "is_mount", return_value=True),
              mock.patch.object(subprocess, "run", side_effect=[
                  subprocess.TimeoutExpired(query, 20),
                  subprocess.CompletedProcess([], 0, info, ""),
                  subprocess.CompletedProcess([], 0, "", ""),
                  subprocess.CompletedProcess([], 0, plistlib.dumps({"images": []}).decode(), ""),
              ]) as run):
            self.cleanup_image(image, mount, None)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            query, query, ["/usr/bin/hdiutil", "detach", "-force", str(mount)], query,
        ])

    def test_fixture_detach_requires_a_mount_inside_the_fixture(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        for entity, mount in (
            ({"dev-entry": "/dev/disk9"}, image.parent / "unit"),
            ({"mount-point": str(image.parent.parent / "outside")}, image.parent.parent / "outside"),
        ):
            images: list[dict[str, object]] = [{"image-path": str(image), "system-entities": [entity]}]
            with (self.subTest(entity=entity), mock.patch.object(Path, "is_mount", return_value=True),
                  mock.patch.object(subprocess, "run") as run):
                self.detach_image(image, mount, None, images, [])
            run.assert_not_called()

    def test_mac_fixture_timeout_reports_partial_output(self) -> None:
        command = ["/usr/bin/hdiutil", "info", "-plist"]
        with (mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(
                  command, 20, output=b"partial output", stderr=b"partial error")),
              self.assertRaises(AssertionError) as failure):
            _ = run_mac_fixture_command(command, timeout=20)
        self.assertIn("timed out after 20 s: /usr/bin/hdiutil info -plist", str(failure.exception))
        self.assertIn("stdout:\npartial output", str(failure.exception))
        self.assertIn("stderr:\npartial error", str(failure.exception))

    def test_does_not_follow_a_bind_mount_inside_the_leftover(self) -> None:
        sandbox = replace(self.sandbox(), mount=BuildMount.BIND_UNIT)
        candidate = self.candidate(sandbox)
        with self.mount_canary(sandbox, candidate) as keep:
            result = self.claim(sandbox, candidate, ClaimCase.NESTED_MOUNT)
            self.assert_claim(result, "proof-2")
            self.assert_canary(keep)
            self.assert_canary(sandbox.host_path(candidate / "old" / "output"))

    def test_keeps_a_bind_mount_after_the_removal_parent_moves(self) -> None:
        sandbox = replace(self.sandbox(), mount=BuildMount.BIND_UNIT)
        candidate = self.candidate(sandbox)
        moved = candidate.parent.with_name("moved") / candidate.name
        with self.mount_canary(sandbox, candidate, moved=moved) as keep:
            checkout = sandbox.checkout if sys.platform == "darwin" else LINUX_CHECKOUT
            result = self.run_python(sandbox, candidate, RUN_MOVED_PARENT, str(checkout), str(candidate))
            self.assert_child_ok(result)
            self.assert_canary(sandbox.host_path(moved / "old" / "output"))
            self.assert_canary(sandbox.host_path(moved / "unit" / "output") if sys.platform == "darwin" else keep)


if __name__ == "__main__":
    _ = unittest.main()

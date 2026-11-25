"""Prove that removal keeps mounted filesystems."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager, redirect_stderr
from dataclasses import dataclass, replace
from io import StringIO
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import tempfile
import time
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

MOVED_PARENT_DETACH_RETRY_S = 10


@dataclass
class MovedParentCleanup:
    directory: tempfile.TemporaryDirectory[str]
    deferred: bool = False

    def cleanup(self) -> None:
        if not self.deferred:
            self.directory.cleanup()


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
    def disk_image(self, *arguments: str, allow_skip: bool = False, attempts: list[str] | None = None) -> str:
        command = ["/usr/bin/hdiutil", *arguments]
        try:
            result = run_mac_fixture_command(command, timeout=20)
        except (FileNotFoundError, PermissionError) as error:
            if allow_skip:
                self.skipTest(f"hdiutil {arguments[0]} is unavailable: {error}")
            raise
        detail = self.command_detail(command, result)
        if attempts is not None:
            attempts.append(detail)
        if allow_skip and result.returncode and any(reason in (result.stderr or result.stdout).lower() for reason in (
                "permission denied", "operation not permitted", "not authorized")):
            self.skipTest(f"hdiutil {arguments[0]} cannot use the fixture: {detail}")
        self.assertEqual(result.returncode, 0, detail)
        return result.stdout

    def command_detail(self, command: list[str], result: subprocess.CompletedProcess[str]) -> str:
        return "\n".join([shlex.join(command), f"exit status: {result.returncode}",
                          "stdout:", result.stdout, "stderr:", result.stderr])

    def cleanup_command(self, attempts: list[str], *arguments: str,
                        timeout: int = 20) -> subprocess.CompletedProcess[str] | None:
        command = list(arguments)
        try:
            result = run_mac_fixture_command(command, timeout=timeout)
        except AssertionError as error:
            attempts.append(str(error))
            return None
        except OSError as error:
            attempts.append(f"{shlex.join(command)}\ncommand did not start: {error}")
            return None
        attempts.append(self.command_detail(command, result))
        return result

    def attached_images(self, attempts: list[str], *, deadline: float | None = None) -> list[dict[str, object]]:
        for _ in range(2):
            timeout = 20 if deadline is None else int(deadline - time.monotonic())
            if timeout < 1:
                break
            result = self.cleanup_command(attempts, "/usr/bin/hdiutil", "info", "-plist", timeout=timeout)
            if result is not None and result.returncode == 0:
                try:
                    reply = cast(dict[str, list[dict[str, object]]], plistlib.loads(result.stdout.encode()))
                    return reply["images"]
                except (KeyError, ValueError, plistlib.InvalidFileException) as error:
                    attempts.append(f"Invalid image list: {error}")
        self.fail("\n".join(["Cannot read the attached disk images.", *attempts]))

    def image_entities(self, reply: dict[str, object]) -> list[dict[str, str]]:
        return cast(list[dict[str, str]], reply.get("system-entities", []))

    def device_images(self, images: list[dict[str, object]], device: str) -> list[dict[str, object]]:
        return [entry for entry in images
                if any(entity.get("dev-entry") == device for entity in self.image_entities(entry))]

    def live_children(self, snapshot: str, test_pid: int) -> dict[int, str]:
        by_parent: dict[int, list[tuple[int, str, str]]] = {}
        for line in snapshot.splitlines():
            fields = line.split(maxsplit=3)
            if len(fields) == 4 and all(value.isdecimal() for value in fields[:2]):
                pid, parent = map(int, fields[:2])
                if pid > 0:
                    by_parent.setdefault(parent, []).append((pid, fields[2], fields[3]))

        pending = [test_pid]
        live: dict[int, str] = {}
        for parent in pending:
            for pid, state, command in by_parent.get(parent, []):
                if state.startswith("Z") or pid in pending:
                    continue
                pending.append(pid)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    pass
                live[pid] = command
        return live

    def busy_mount_diagnostics(self, mounts: list[Path], attempts: list[str]) -> None:
        attempts.append("Open files (visible to this user only; root-owned processes can be missing):")
        if not mounts:
            attempts.append("No verified mount path for lsof or mdutil.")
        for mount in mounts:
            _ = self.cleanup_command(attempts, "/usr/sbin/lsof", "-n", "+f", "--", str(mount))

        self.busy_child_diagnostics(attempts)
        for mount in mounts:
            _ = self.cleanup_command(attempts, "/usr/bin/mdutil", "-s", str(mount))

    def busy_child_diagnostics(self, attempts: list[str]) -> None:
        test_pid = os.getpid()
        attempts.append(f"Live children of test PID {test_pid} (visible to this user only; root-owned processes can be missing):")
        process_attempts: list[str] = []
        snapshot = self.cleanup_command(process_attempts, "/bin/ps", "-axo", "pid=,ppid=,state=,comm=")
        if snapshot is None or snapshot.returncode != 0:
            attempts.extend(process_attempts)
            return

        children = self.live_children(snapshot.stdout, test_pid)
        if not children:
            attempts.append("No live children.")
            return
        pids = ",".join(map(str, children))
        result = self.cleanup_command(attempts, "/usr/sbin/lsof", "-n", "-a", "-p", pids, "-d", "cwd", "-Fpn")
        directories: dict[int, str] = {}
        if result is not None:
            pid = 0
            for field in result.stdout.splitlines():
                if field.startswith("p"):
                    pid = int(field[1:]) if field[1:].isdecimal() else 0
                elif field.startswith("n/") and pid in children:
                    directories[pid] = field[1:]
        for pid, command in children.items():
            attempts.append(f"PID {pid} {command} cwd: {directories.get(pid, 'unavailable')}")

    def verified_mounts(self, image: Path, device: str, owners: list[dict[str, object]], attempts: list[str]) -> list[Path]:
        if len(owners) != 1 or owners[0].get("image-path") != str(image):
            self.fail("\n".join([f"{device} no longer belongs to {image}.", *attempts]))
        root = image.parent.resolve()
        mounts: list[Path] = []
        for entity in self.image_entities(owners[0]):
            if point := entity.get("mount-point"):
                mount = Path(point)
                target = mount.resolve()
                if not mount.is_absolute() or target == root or not target.is_relative_to(root):
                    self.fail("\n".join([f"{device} has a mount outside the fixture: {mount}", *attempts]))
                mounts.append(target)
        return mounts

    def detach_is_busy(self, result: subprocess.CompletedProcess[str] | None) -> bool:
        return result is not None and result.returncode != 0 and any(
            "resource busy" in stream.lower() for stream in (result.stdout, result.stderr))

    def retry_moved_parent_detach(self, image: Path, device: str, attempts: list[str],
                                  result: subprocess.CompletedProcess[str] | None,
                                  ) -> tuple[subprocess.CompletedProcess[str] | None, int, float]:
        started = time.monotonic()
        deadline = started + MOVED_PARENT_DETACH_RETRY_S
        attempt = 1
        while self.detach_is_busy(result):
            remaining = deadline - time.monotonic()
            # Allow one second each for the identity check and the detach.
            if remaining < 2:
                if remaining > 0:
                    time.sleep(remaining)
                break
            owners = self.device_images(self.attached_images(attempts, deadline=deadline), device)
            if not owners:
                break
            _ = self.verified_mounts(image, device, owners, attempts)
            timeout = int(deadline - time.monotonic())
            if timeout < 1:
                continue
            attempt += 1
            result = self.cleanup_command(attempts, "/usr/bin/hdiutil", "detach", "-force", device, timeout=timeout)
            if self.detach_is_busy(result) and (delay := min(1, deadline - time.monotonic())) > 0:
                time.sleep(delay)
        return result, attempt, time.monotonic() - started

    def cleanup_image(self, image: Path, device: str | None, attempts: list[str], *,
                      moved_parent: MovedParentCleanup | None = None) -> None:
        remaining = self.attached_images(attempts)
        deferred_image = None
        attempt = 1
        elapsed = 0.0
        if device is not None and (owners := self.device_images(remaining, device)):
            mounts = self.verified_mounts(image, device, owners, attempts)
            result = self.cleanup_command(attempts, "/usr/bin/hdiutil", "detach", "-force", device)
            if moved_parent is not None and self.detach_is_busy(result):
                result, attempt, elapsed = self.retry_moved_parent_detach(image, device, attempts, result)
            busy = self.detach_is_busy(result)
            if busy:
                self.busy_mount_diagnostics(mounts, attempts)
            remaining = self.attached_images(attempts)
            if owners := self.device_images(remaining, device):
                if moved_parent is None or not busy or elapsed < MOVED_PARENT_DETACH_RETRY_S:
                    self.fail("\n".join([f"Disk device still attached: {device} for {image}", *attempts]))
                _ = self.verified_mounts(image, device, owners, attempts)
                deferred_image = owners[0]
            elif result is None or result.returncode != 0:
                self.fail("\n".join([f"Detach failed for {device}.", *attempts]))
        fixture_images = [name for entry in remaining if entry is not deferred_image
                          and isinstance(name := entry.get("image-path"), str)
                          and Path(name).is_relative_to(image.parent.parent)]
        if fixture_images:
            self.fail("\n".join([f"Disk images still attached: {', '.join(fixture_images)}", *attempts]))
        if deferred_image is not None and moved_parent is not None:
            moved_parent.deferred = True
            print("\n".join([
                "Mac: the image detach after the parent moves stays busy under a holder that this user cannot see; "
                + "the gate cleanup detaches it",
                *attempts,
            ]), file=sys.stderr, flush=True)
        elif attempt > 1:
            print(f"Mac: detach attempt {attempt} released {device} after {elapsed:.2f} s.",
                  file=sys.stderr, flush=True)

    @contextmanager
    def attach_image(self, image: Path, mount: Path, *,
                     moved_parent: MovedParentCleanup | None = None) -> Generator[None]:
        device: str | None = None
        attempts: list[str] = []
        interrupt: BaseException | None = None
        completed = False
        try:
            reply = self.disk_image("attach", "-nobrowse", "-mountpoint", str(mount),
                                    "-owners", "off", "-plist", str(image), allow_skip=True, attempts=attempts)
            entities = self.image_entities(cast(dict[str, object], plistlib.loads(reply.encode())))
            devices = {name for entry in entities if (name := entry.get("dev-entry"))
                       and re.fullmatch(r"/dev/disk[0-9]+", name)}
            self.assertEqual(len(devices), 1, f"hdiutil did not report one whole disk for {image}: {reply}")
            device = devices.pop()
            self.assertTrue(mount.is_mount(), f"hdiutil did not mount {mount}")
            yield
            completed = True
        except BaseException as error:
            if not isinstance(error, Exception):
                interrupt = error
            raise
        finally:
            try:
                self.cleanup_image(image, device, attempts, moved_parent=moved_parent if completed else None)
            except BaseException as cleanup_error:
                if interrupt is None:
                    raise
                interrupt.add_note(f"Disk image cleanup failed: {cleanup_error}")

    @contextmanager
    def mount_canary(self, sandbox: RemovalSandbox, candidate: Path, *,
                     moved_parent: MovedParentCleanup | None = None) -> Generator[Path]:
        mount = candidate / "unit"
        local = sandbox.host_path(mount)
        local.mkdir()
        with ExitStack() as cleanup:
            if sys.platform == "darwin":
                image = sandbox.fixture / "removal.dmg"
                _ = self.disk_image("create", "-size", "16m", "-fs", "HFS+", "-layout", "NONE",
                                    "-type", "UDIF", str(image), allow_skip=True)
                cleanup.enter_context(self.attach_image(image, local, moved_parent=moved_parent))
                keep = local / "output"
                probe = (PROBE_MAC_MOUNT, str(sandbox.checkout), str(mount))
            else:
                keep = sandbox.bind_source / "output"
                probe = (PROBE_MOUNT, str(mount))
            self.canary(keep)
            result = self.run_python(sandbox, candidate, *probe)
            self.assert_child_ok(result)
            yield keep

    def test_attach_interrupt_keeps_the_cleanup_diagnostics(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        image = root / "fixture/removal.dmg"
        mount = image.parent / "mount"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [{"dev-entry": "/dev/disk9", "mount-point": str(mount)}]},
        ]}).decode()
        for interrupt in (KeyboardInterrupt(), SystemExit(130)):
            with self.subTest(interrupt=type(interrupt)):
                with (mock.patch.object(subprocess, "run", side_effect=[
                    interrupt,
                    subprocess.CompletedProcess([], 0, info, "info diagnostic"),
                ]) as run, self.assertRaises(type(interrupt)) as failure):
                    with self.attach_image(image, mount):
                        self.fail("the interrupted attach returned")
                self.assertIs(failure.exception, interrupt)
                self.assertEqual(len(interrupt.__notes__), 1)
                note = interrupt.__notes__[0]
                self.assertIn(f"Disk images still attached: {image}", note)
                self.assertIn(f"/usr/bin/hdiutil info -plist\nexit status: 0\nstdout:\n{info}\nstderr:\ninfo diagnostic", note)
                self.assertEqual([call.args[0] for call in run.call_args_list], [
                    ["/usr/bin/hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), "-owners", "off", "-plist", str(image)],
                    ["/usr/bin/hdiutil", "info", "-plist"],
                ])

    def test_detaches_the_device_from_attach_after_the_mount_moves(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        image = root / "removal.dmg"
        mount = root / "original/unit"
        mount.mkdir(parents=True)
        entities = [
            {"dev-entry": "/dev/disk9s1", "mount-point": str(mount)},
            {"dev-entry": "/dev/disk9"},
        ]
        attach = plistlib.dumps({"system-entities": entities}).decode()
        info = plistlib.dumps({"images": [{"image-path": str(image), "system-entities": entities}]}).decode()
        clean = plistlib.dumps({"images": []}).decode()
        with (mock.patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, attach, ""),
            subprocess.CompletedProcess([], 0, info, ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, clean, ""),
        ]) as run, mock.patch.object(Path, "is_mount", autospec=True, return_value=True) as is_mount):
            with self.attach_image(image, mount):
                moved = mount.parent.rename(root / "moved") / mount.name
        self.assertTrue(moved.is_dir())
        is_mount.assert_called_once_with(mount)
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["/usr/bin/hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), "-owners", "off", "-plist", str(image)],
            ["/usr/bin/hdiutil", "info", "-plist"],
            ["/usr/bin/hdiutil", "detach", "-force", "/dev/disk9"],
            ["/usr/bin/hdiutil", "info", "-plist"],
        ])

    def test_reports_a_disk_device_that_stays_attached(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        device = "/dev/disk9"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [{"dev-entry": device}]},
        ]}).decode()
        query = ["/usr/bin/hdiutil", "info", "-plist"]
        detach = ["/usr/bin/hdiutil", "detach", "-force", device]
        for status in (0, 3):
            with (self.subTest(status=status), mock.patch.object(subprocess, "run", side_effect=[
                subprocess.CompletedProcess(query, 0, info, "before"),
                subprocess.CompletedProcess(detach, status, "device output", "device error"),
                subprocess.CompletedProcess(query, 0, info, "after"),
            ]) as run, self.assertRaises(AssertionError) as failure):
                self.cleanup_image(image, device, [])
            detail = str(failure.exception)
            self.assertIn(f"Disk device still attached: {device} for {image}", detail)
            self.assertIn(f"{shlex.join(detach)}\nexit status: {status}\nstdout:\ndevice output\nstderr:\ndevice error", detail)
            for stderr in ("before", "after"):
                self.assertIn(f"{shlex.join(query)}\nexit status: 0\nstdout:\n{info}\nstderr:\n{stderr}", detail)
            self.assertEqual([call.args[0] for call in run.call_args_list], [query, detach, query])

    def test_busy_detach_reports_mount_holders_and_live_child_cwds(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        mount = image.parent / "moved/proof/unit"
        device = "/dev/disk9"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [{"dev-entry": device, "mount-point": str(mount)}]},
        ]}).decode()
        snapshot = "\n".join([
            "102 101 S sandbox child",
            "101 100 S sandbox-exec",
            "103 100 Z zombie child",
            "104 100 R /bin/ps",
            "105 1 S unrelated process",
            "106 100 S helper",
        ])
        mount_output = f"Python 102 4r DIR {mount}"
        cwd_output = f"p101\nn{image.parent}\np102\nn{mount}\n"
        with (mock.patch.object(os, "getpid", return_value=100),
              mock.patch.object(os, "kill", side_effect=[None, ProcessLookupError(), None, PermissionError()]) as kill,
              mock.patch.object(subprocess, "run", side_effect=[
                  subprocess.CompletedProcess([], 0, info, ""),
                  subprocess.CompletedProcess([], 16, "", "Resource busy"),
                  subprocess.CompletedProcess([], 0, mount_output, "mount diagnostic"),
                  subprocess.CompletedProcess([], 0, snapshot, ""),
                  subprocess.CompletedProcess([], 1, cwd_output, "cwd diagnostic"),
                  subprocess.CompletedProcess([], 0, "Indexing enabled.", "Spotlight diagnostic"),
                  subprocess.CompletedProcess([], 0, info, ""),
              ]) as run, self.assertRaises(AssertionError) as failure):
            self.cleanup_image(image, device, [])
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["/usr/bin/hdiutil", "info", "-plist"],
            ["/usr/bin/hdiutil", "detach", "-force", device],
            ["/usr/sbin/lsof", "-n", "+f", "--", str(mount)],
            ["/bin/ps", "-axo", "pid=,ppid=,state=,comm="],
            ["/usr/sbin/lsof", "-n", "-a", "-p", "101,106,102", "-d", "cwd", "-Fpn"],
            ["/usr/bin/mdutil", "-s", str(mount)],
            ["/usr/bin/hdiutil", "info", "-plist"],
        ])
        self.assertTrue(all(call.kwargs["timeout"] == 20 for call in run.call_args_list))
        self.assertEqual(kill.call_args_list, [mock.call(pid, 0) for pid in (101, 104, 106, 102)])
        detail = str(failure.exception)
        self.assertIn(f"Disk device still attached: {device} for {image}", detail)
        scope = "visible to this user only; root-owned processes can be missing"
        self.assertIn(f"Open files ({scope}):\n/usr/sbin/lsof", detail)
        self.assertIn(f"Live children of test PID 100 ({scope}):\n/usr/sbin/lsof", detail)
        self.assertIn(f"stdout:\n{mount_output}\nstderr:\nmount diagnostic", detail)
        self.assertIn(f"stdout:\n{cwd_output}\nstderr:\ncwd diagnostic", detail)
        self.assertIn(f"/usr/bin/mdutil -s {mount}\nexit status: 0\nstdout:\nIndexing enabled.\nstderr:\nSpotlight diagnostic", detail)
        self.assertIn(f"PID 101 sandbox-exec cwd: {image.parent}", detail)
        self.assertIn(f"PID 102 sandbox child cwd: {mount}", detail)
        self.assertIn("PID 106 helper cwd: unavailable", detail)
        for excluded in ("PID 103", "PID 104", "PID 105"):
            self.assertNotIn(excluded, detail)

    def test_reports_partial_mount_diagnostics_after_lsof_timeout(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        mount = image.parent / "moved/proof/unit"
        device = "/dev/disk9"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [{"dev-entry": device, "mount-point": str(mount)}]},
        ]}).decode()
        command = ["/usr/sbin/lsof", "-n", "+f", "--", str(mount)]
        with (mock.patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, info, ""),
            subprocess.CompletedProcess([], 16, "", "Resource busy"),
            subprocess.TimeoutExpired(command, 20, output=b"partial lsof output", stderr=b"partial lsof error"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 1, "", "Unknown indexing state."),
            subprocess.CompletedProcess([], 0, info, ""),
        ]) as run, self.assertRaises(AssertionError) as failure):
            self.cleanup_image(image, device, [])
        detail = str(failure.exception)
        self.assertIn(f"timed out after 20 s: {shlex.join(command)}", detail)
        self.assertIn("stdout:\npartial lsof output\nstderr:\npartial lsof error", detail)
        self.assertIn("No live children.", detail)
        self.assertIn(f"/usr/bin/mdutil -s {mount}\nexit status: 1\nstdout:\n\nstderr:\nUnknown indexing state.", detail)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/hdiutil", "info", "-plist"])

    def test_reports_a_failed_detach_after_the_device_disappears(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        device = "/dev/disk9"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [{"dev-entry": device}]},
        ]}).decode()
        clean = plistlib.dumps({"images": []}).decode()
        with (mock.patch.object(subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, info, ""),
            subprocess.CompletedProcess([], 2, "", "detach error"),
            subprocess.CompletedProcess([], 0, clean, ""),
        ]), self.assertRaisesRegex(AssertionError, "Detach failed for /dev/disk9") as failure):
            self.cleanup_image(image, device, [])
        self.assertIn("exit status: 2\nstdout:\n\nstderr:\ndetach error", str(failure.exception))

    def test_moved_parent_retry_reports_the_attempt_that_released_the_device(self) -> None:
        directory = tempfile.TemporaryDirectory()
        _ = self.enterContext(directory)
        moved_parent = MovedParentCleanup(directory)
        image = Path(directory.name).resolve() / "fixture/removal.dmg"
        mount = image.parent / "moved/unit"
        device = "/dev/disk9"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [{"dev-entry": device, "mount-point": str(mount)}]},
        ]}).decode()
        clean = plistlib.dumps({"images": []}).decode()
        query = ["/usr/bin/hdiutil", "info", "-plist"]
        detach = ["/usr/bin/hdiutil", "detach", "-force", device]
        stderr = StringIO()
        attempts: list[str] = []
        with (mock.patch.object(time, "monotonic", side_effect=[100.0, 100.0, 100.0, 100.5, 101.0]),
              mock.patch.object(subprocess, "run", side_effect=[
                  subprocess.CompletedProcess(query, 0, info, ""),
                  subprocess.CompletedProcess(detach, 16, "", "Resource busy"),
                  subprocess.CompletedProcess(query, 0, info, ""),
                  subprocess.CompletedProcess(detach, 0, "", ""),
                  subprocess.CompletedProcess(query, 0, clean, ""),
              ]) as run, redirect_stderr(stderr)):
            self.cleanup_image(image, device, attempts, moved_parent=moved_parent)
        self.assertEqual([call.args[0] for call in run.call_args_list], [query, detach, query, detach, query])
        self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [20, 20, 10, 9, 20])
        self.assertEqual(stderr.getvalue(), f"Mac: detach attempt 2 released {device} after 1.00 s.\n")
        self.assertIn("Resource busy", attempts[1])
        self.assertIn("exit status: 0", attempts[3])
        moved_parent.cleanup()
        self.assertFalse(Path(directory.name).exists())

    def test_moved_parent_retry_leaves_a_busy_device_and_fixture_for_the_gate(self) -> None:
        directory = tempfile.TemporaryDirectory(delete=False)
        self.addCleanup(directory.cleanup)
        moved_parent = MovedParentCleanup(directory)
        image = Path(directory.name).resolve() / "fixture/removal.dmg"
        mount = image.parent / "moved/unit"
        mount.mkdir(parents=True)
        device = "/dev/disk9"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [{"dev-entry": device, "mount-point": str(mount)}]},
        ]}).decode()
        query = ["/usr/bin/hdiutil", "info", "-plist"]
        detach = ["/usr/bin/hdiutil", "detach", "-force", device]
        attached = subprocess.CompletedProcess(query, 0, info, "")
        busy = subprocess.CompletedProcess(detach, 16, "", "Resource busy")
        stderr = StringIO()
        now = 100.0

        def monotonic() -> float:
            return now

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        with (mock.patch.object(time, "monotonic", side_effect=monotonic),
              mock.patch.object(time, "sleep", side_effect=advance),
              mock.patch.object(subprocess, "run", side_effect=[*([attached, busy] * 10), attached]) as run,
              mock.patch.object(self, "busy_mount_diagnostics") as diagnostics, redirect_stderr(stderr)):
            self.cleanup_image(image, device, [], moved_parent=moved_parent)
        diagnostics.assert_called_once()
        self.assertEqual(now, 100 + MOVED_PARENT_DETACH_RETRY_S)
        self.assertEqual([call.args[0] for call in run.call_args_list], [*([query, detach] * 10), query])
        self.assertTrue(stderr.getvalue().startswith(
            "Mac: the image detach after the parent moves stays busy under a holder that this user cannot see; "
            + "the gate cleanup detaches it\n"
        ))
        self.assertEqual(stderr.getvalue().count(f"{shlex.join(detach)}\nexit status: 16"), 10)
        moved_parent.cleanup()
        self.assertTrue(mount.is_dir())

    def test_moved_parent_retry_refuses_a_device_with_a_different_image(self) -> None:
        directory = tempfile.TemporaryDirectory()
        _ = self.enterContext(directory)
        moved_parent = MovedParentCleanup(directory)
        image = Path(directory.name).resolve() / "fixture/removal.dmg"
        device = "/dev/disk9"
        original, changed = (plistlib.dumps({"images": [
            {"image-path": str(source), "system-entities": [{"dev-entry": device}]},
        ]}).decode() for source in (image, image.with_name("other.dmg")))
        query = ["/usr/bin/hdiutil", "info", "-plist"]
        detach = ["/usr/bin/hdiutil", "detach", "-force", device]
        with (mock.patch.object(time, "monotonic", return_value=100.0),
              mock.patch.object(subprocess, "run", side_effect=[
                  subprocess.CompletedProcess(query, 0, original, ""),
                  subprocess.CompletedProcess(detach, 16, "", "Resource busy"),
                  subprocess.CompletedProcess(query, 0, changed, ""),
              ]) as run, self.assertRaisesRegex(AssertionError, "no longer belongs")):
            self.cleanup_image(image, device, [], moved_parent=moved_parent)
        self.assertEqual([call.args[0] for call in run.call_args_list], [query, detach, query])
        self.assertFalse(moved_parent.deferred)

    def test_detach_requires_the_saved_device_and_the_same_private_image(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        device = "/dev/disk9"
        cases = (
            (image.parent.parent.parent / "other.dmg", {"dev-entry": device}, "no longer belongs"),
            (image, {"dev-entry": "/dev/disk8"}, "Disk images still attached"),
            (image, {"dev-entry": device, "mount-point": str(image.parent.parent / "outside")}, "outside the fixture"),
        )
        for image_path, entity, reason in cases:
            with self.subTest(image_path=image_path, entity=entity):
                info = plistlib.dumps({"images": [{"image-path": str(image_path), "system-entities": [entity]}]}).decode()
                with (mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, info, "")) as run,
                      self.assertRaisesRegex(AssertionError, reason)):
                    self.cleanup_image(image, device, [])
                self.assertEqual([call.args[0] for call in run.call_args_list], [["/usr/bin/hdiutil", "info", "-plist"]])

    def test_detaches_the_saved_device_after_an_info_timeout(self) -> None:
        image = Path("/private/tmp/test-root/fixture/removal.dmg")
        device = "/dev/disk9"
        info = plistlib.dumps({"images": [
            {"image-path": str(image), "system-entities": [{"dev-entry": device}]},
        ]}).decode()
        query = ["/usr/bin/hdiutil", "info", "-plist"]
        with mock.patch.object(subprocess, "run", side_effect=[
            subprocess.TimeoutExpired(query, 20),
            subprocess.CompletedProcess([], 0, info, ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, plistlib.dumps({"images": []}).decode(), ""),
        ]) as run:
            self.cleanup_image(image, device, [])
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            query, query, ["/usr/bin/hdiutil", "detach", "-force", device], query,
        ])

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
        moved_parent = None
        if sys.platform == "darwin":
            directory = tempfile.TemporaryDirectory(prefix="tool-removal-", delete=False)
            moved_parent = MovedParentCleanup(directory)
            self.addCleanup(moved_parent.cleanup)
            sandbox = RemovalSandbox(Path(directory.name).resolve() / "fixture", mount=BuildMount.BIND_UNIT)
            sandbox.configure()
        else:
            sandbox = replace(self.sandbox(), mount=BuildMount.BIND_UNIT)
        candidate = self.candidate(sandbox)
        moved = candidate.parent.with_name("moved") / candidate.name
        with self.mount_canary(sandbox, candidate, moved_parent=moved_parent) as keep:
            checkout = sandbox.checkout if sys.platform == "darwin" else LINUX_CHECKOUT
            result = self.run_python(sandbox, candidate, RUN_MOVED_PARENT, str(checkout), str(candidate))
            self.assert_child_ok(result)
            self.assert_canary(sandbox.host_path(moved / "old" / "output"))
            self.assert_canary(sandbox.host_path(moved / "unit" / "output") if sys.platform == "darwin" else keep)


if __name__ == "__main__":
    _ = unittest.main()

"""Prove the removal boundary of a tool folder claim."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from enum import Enum
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
from typing import cast
import unittest
from unittest import mock

from scripts.lib import build_folder_lock, build_folders, removal_guard
from scripts.lib.removal_bwrap_test_support import BuildMount, LINUX_CHECKOUT, RemovalSandbox
from scripts.lint.test_removal_bwrap import CANARY, PROBE_MOUNT, PROBE_OUTSIDE


class ClaimCase(Enum):
    LEFTOVER = "leftover"
    UNREADABLE_LEASE = "unreadable-lease"
    LIVE_OWNER = "live-owner"
    FOREIGN_OWNER = "foreign-owner"
    HELD_LOCK = "held-lock"
    STAGED_REPLACEMENT = "staged-replacement"
    OWNER_REPLACEMENT = "owner-replacement"


RUN_CLAIM = """
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from scripts.lib.test_build_folders_bwrap import ClaimCase, run_claim

run_claim(Path(sys.argv[2]), ClaimCase(sys.argv[3]))
"""


RUN_MOVED_PARENT = """
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
from scripts.lib.test_build_folders_bwrap import remove_after_parent_moves

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
    parent = candidate.parent
    with build_folder_lock.folder_parent(candidate, parent, create=False) as directory:
        _ = parent.rename(parent.with_name("moved"))
        parent.mkdir()
        assert not build_folders.remove_build_folder(candidate, dir_fd=directory, repair_access=True)


def _check_missing_pid(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"fixture process {pid} still exists")


def _lease_owner(case: ClaimCase) -> int:
    if case is ClaimCase.LIVE_OWNER:
        return os.getpid()
    if case is ClaimCase.FOREIGN_OWNER:
        if sys.platform == "linux":
            assert Path("/proc/1").stat().st_uid != os.geteuid()
        try:
            os.kill(1, 0)
        except PermissionError:
            return 1
        raise AssertionError("the claim process can signal the foreign lease owner")
    child = subprocess.run([sys.executable, "-B", "-c", "import os; print(os.getpid())"],
                           check=True, capture_output=True, text=True, timeout=10)
    pid = int(child.stdout)
    _check_missing_pid(pid)
    return pid


def _replace_checked_tree(stack: ExitStack, candidate: Path) -> None:
    remove = build_folders.CheckedRemovalTree.remove
    original = candidate.stat()

    def replace_then_remove(tree: build_folders.CheckedRemovalTree, *, repair_access: bool = False) -> None:
        if os.path.samestat(tree.info, original):
            _ = tree.path.rename(tree.path.with_name("original-tree"))
            tree.path.mkdir()
            _ = (tree.path / "replacement").write_text(CANARY)
        remove(tree, repair_access=repair_access)

    _ = stack.enter_context(mock.patch.object(build_folders.CheckedRemovalTree, "remove", replace_then_remove))


def _claim_after_other_owner(candidate: Path) -> None:
    """Let the first claim return to its live owner before the second locks the name."""
    acquire = cast(Callable[..., build_folders.FolderRemovalLease], getattr(build_folders, "_hold_free_lease"))
    command = ["tool", "--tool", candidate.name, "--parent", str(candidate.parent), "--owner-pid", str(os.getpid())]
    claimed = False

    def acquire_after_claim(path: Path, directory: int, stack: ExitStack,
                            **options: object) -> build_folders.FolderRemovalLease:
        nonlocal claimed
        if path == candidate and not claimed:
            claimed = True
            with redirect_stdout(StringIO()) as first:
                assert build_folders.main(command) == 0
            assert first.getvalue().strip() == str(candidate)
            _ = (candidate / "live").write_text(CANARY)
        return acquire(path, directory, stack, **options)

    with mock.patch.object(build_folders, "_hold_free_lease", acquire_after_claim):
        with redirect_stdout(StringIO()) as second:
            assert build_folders.main(command) == 0
    assert claimed
    print(Path(second.getvalue().strip()).name)


def run_claim(candidate: Path, case: ClaimCase) -> None:
    """Run the real claim with the selected fixture state."""
    if sys.platform == "darwin":
        assert removal_guard.read().root == Path.home() / "build"
    lease = build_folders.lease_path(candidate)
    _ = lease.write_text(json.dumps({"pid": _lease_owner(case)}))
    if case is ClaimCase.OWNER_REPLACEMENT:
        _claim_after_other_owner(candidate)
        return
    with ExitStack() as fixture:
        if case is ClaimCase.UNREADABLE_LEASE:
            lease.chmod(0)
            _ = fixture.callback(lease.chmod, 0o600)
            try:
                _ = lease.read_text()
            except PermissionError:
                pass
            else:
                raise AssertionError("the fixture lease is readable")
        if case is ClaimCase.HELD_LOCK:
            lock = fixture.enter_context(candidate.with_suffix(".lock").open("r+"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if case is ClaimCase.STAGED_REPLACEMENT:
            _replace_checked_tree(fixture, candidate)
        try:
            with build_folders.tool_folder(candidate.name, candidate.parent) as claimed:
                print(claimed.name)
                assert not list(claimed.iterdir()), f"{claimed} contains output from the previous claim"
        except build_folders.RemovalTreeChanged:
            if case is not ClaimCase.STAGED_REPLACEMENT:
                raise
            print("replacement refused")


@unittest.skipUnless(sys.platform in ("linux", "darwin"), "requires Linux or macOS")
class ToolFolderRemovalTests(unittest.TestCase):
    def sandbox(self) -> RemovalSandbox:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="tool-removal-"))).resolve()
        sandbox = RemovalSandbox(root / "fixture")
        sandbox.configure()
        return sandbox

    def canary(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(CANARY)

    def assert_canary(self, path: Path) -> None:
        self.assertTrue(path.is_file(), f"the claim removed {path}")
        self.assertEqual(path.read_text(), CANARY)

    def candidate(self, sandbox: RemovalSandbox) -> Path:
        candidate = sandbox.build / "proof"
        local = sandbox.host_path(candidate)
        self.canary(local / "old" / "output")
        _ = local.with_suffix(".lock").touch()
        return candidate

    def claim(self, sandbox: RemovalSandbox, candidate: Path,
              case: ClaimCase = ClaimCase.LEFTOVER) -> subprocess.CompletedProcess[str]:
        checkout = sandbox.checkout if sys.platform == "darwin" else LINUX_CHECKOUT
        return sandbox.python("-c", RUN_CLAIM, str(checkout), str(candidate), case.value)

    def assert_claim(self, result: subprocess.CompletedProcess[str], name: str) -> None:
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), name)

    def test_removes_only_the_leftover(self) -> None:
        sandbox = self.sandbox()
        outside = sandbox.outside.read_bytes()
        probe = sandbox.python("-c", PROBE_OUTSIDE, str(sandbox.outside))
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        candidate = self.candidate(sandbox)
        local = sandbox.host_path(candidate)
        keep = local.parent / "keep"
        sibling = local.parent / "other-tool" / "output"
        for path in (keep, sibling):
            self.canary(path)
        sibling_lock = sibling.parent.with_suffix(".lock")
        sibling_lock.touch()
        parent = local.parent.stat()
        self.assert_claim(self.claim(sandbox, candidate), candidate.name)
        self.assertFalse(local.exists())
        self.assertFalse(local.with_suffix(".lock").exists())
        self.assertFalse(build_folders.lease_path(local).exists())
        self.assertTrue(os.path.samestat(parent, local.parent.stat()))
        self.assert_canary(keep)
        self.assert_canary(sibling)
        self.assertTrue(sibling_lock.is_file())
        self.assertEqual(sandbox.outside.read_bytes(), outside)

    def test_keeps_a_symlink_at_the_tool_name(self) -> None:
        sandbox = self.sandbox()
        candidate = sandbox.build / "proof"
        local = sandbox.host_path(candidate)
        target = sandbox.build / "other-tool"
        keep = sandbox.host_path(target / "output")
        self.canary(keep)
        local.symlink_to(target, target_is_directory=True)
        self.assert_claim(self.claim(sandbox, candidate), "proof-2")
        self.assertEqual(local.readlink(), target)
        self.assert_canary(keep)

    def test_does_not_follow_a_symlink_inside_the_leftover(self) -> None:
        sandbox = self.sandbox()
        candidate = self.candidate(sandbox)
        local = sandbox.host_path(candidate)
        target = sandbox.build / "other-tool"
        keep = sandbox.host_path(target / "output")
        self.canary(keep)
        (local / "link").symlink_to(target, target_is_directory=True)
        self.assert_claim(self.claim(sandbox, candidate), candidate.name)
        self.assertFalse(local.exists())
        self.assert_canary(keep)

    def disk_image(self, *arguments: str) -> str:
        try:
            result = subprocess.run(["/usr/bin/hdiutil", *arguments], stdin=subprocess.DEVNULL,
                                    text=True, capture_output=True, timeout=20, check=False)
        except (FileNotFoundError, PermissionError) as error:
            self.skipTest(f"hdiutil {arguments[0]} is unavailable: {error}")
        detail = (result.stderr or result.stdout).strip()
        if result.returncode and any(reason in detail.lower() for reason in (
                "permission denied", "operation not permitted", "not authorized")):
            self.skipTest(f"hdiutil {arguments[0]} cannot use the fixture: {detail}")
        self.assertEqual(result.returncode, 0, detail)
        return result.stdout

    def detach_image(self, device: str) -> None:
        result = subprocess.run(["/usr/bin/hdiutil", "detach", "-force", device], stdin=subprocess.DEVNULL,
                                text=True, capture_output=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mount_canary(self, sandbox: RemovalSandbox, candidate: Path) -> Path:
        mount = candidate / "unit"
        local = sandbox.host_path(mount)
        local.mkdir()
        if sys.platform == "darwin":
            image = sandbox.fixture / "removal.dmg"
            _ = self.disk_image("create", "-size", "16m", "-fs", "HFS+", "-layout", "NONE", "-format", "UDRW", str(image))
            try:
                attached = self.disk_image("attach", "-nobrowse", "-mountpoint", str(local), "-owners", "off", "-plist", str(image))
                reply = cast(dict[str, list[dict[str, str]]], plistlib.loads(attached.encode()))
                [device] = [entry["dev-entry"] for entry in reply["system-entities"] if entry.get("mount-point") == str(local)]
            except BaseException:
                if local.is_mount():
                    self.detach_image(str(local))
                raise
            self.addCleanup(self.detach_image, device)
            self.assertTrue(local.is_mount(), f"hdiutil did not mount {local}")
            keep = local / "output"
            probe = (PROBE_MAC_MOUNT, str(sandbox.checkout), str(mount))
        else:
            keep = sandbox.bind_source / "output"
            probe = (PROBE_MOUNT, str(mount))
        self.canary(keep)
        result = sandbox.python("-c", *probe)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return keep

    def test_does_not_follow_a_bind_mount_inside_the_leftover(self) -> None:
        sandbox = replace(self.sandbox(), mount=BuildMount.BIND_UNIT)
        candidate = self.candidate(sandbox)
        keep = self.mount_canary(sandbox, candidate)
        result = self.claim(sandbox, candidate)
        self.assert_canary(keep)
        self.assert_canary(sandbox.host_path(candidate / "old" / "output"))
        self.assert_claim(result, "proof-2")

    def test_keeps_a_bind_mount_after_the_removal_parent_moves(self) -> None:
        sandbox = replace(self.sandbox(), mount=BuildMount.BIND_UNIT)
        with mock.patch.object(RemovalSandbox, "build", sandbox.build / "removal-parent"):
            candidate = self.candidate(sandbox)
            keep = self.mount_canary(sandbox, candidate)
            checkout = sandbox.checkout if sys.platform == "darwin" else LINUX_CHECKOUT
            result = sandbox.python("-c", RUN_MOVED_PARENT, str(checkout), str(candidate))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        moved = candidate.parent.with_name("moved") / candidate.name
        self.assert_canary(sandbox.host_path(moved / "old" / "output"))
        self.assert_canary(sandbox.host_path(moved / "unit" / "output") if sys.platform == "darwin" else keep)

    def test_second_claim_keeps_the_first_claim_live_owner(self) -> None:
        sandbox = self.sandbox()
        candidate = self.candidate(sandbox)
        result = self.claim(sandbox, candidate, ClaimCase.OWNER_REPLACEMENT)
        self.assert_canary(sandbox.host_path(candidate / "live"))
        self.assert_claim(result, "proof-2")

    def test_keeps_a_replacement_at_the_checked_staging_name(self) -> None:
        sandbox = self.sandbox()
        candidate = self.candidate(sandbox)
        result = self.claim(sandbox, candidate, ClaimCase.STAGED_REPLACEMENT)
        parent = sandbox.host_path(candidate.parent)
        self.assert_canary(parent / "original-tree" / "old" / "output")
        staged = [path for path in parent.glob(".removing-*") if path.is_dir()]
        self.assertEqual(len(staged), 1, result.stdout + result.stderr)
        [replacement] = staged
        self.assert_canary(replacement / "replacement")
        self.assert_claim(result, "replacement refused")

    def assert_kept(self, case: ClaimCase) -> None:
        sandbox = self.sandbox()
        candidate = self.candidate(sandbox)
        result = self.claim(sandbox, candidate, case)
        self.assert_canary(sandbox.host_path(candidate / "old" / "output"))
        self.assert_claim(result, "proof-2")

    def test_keeps_a_folder_with_an_unreadable_lease(self) -> None:
        self.assert_kept(ClaimCase.UNREADABLE_LEASE)

    def test_keeps_a_folder_with_a_live_lease_owner(self) -> None:
        self.assert_kept(ClaimCase.LIVE_OWNER)

    def test_keeps_a_folder_with_a_foreign_lease_owner(self) -> None:
        sandbox = self.sandbox()
        candidate = self.candidate(sandbox)
        if sys.platform == "darwin":
            with self.assertRaises(PermissionError):
                os.kill(1, 0)
            self.assert_claim(self.claim(sandbox, candidate, ClaimCase.FOREIGN_OWNER), "proof-2")
        else:
            self.assert_claim(self.claim_with_host_pids(sandbox, sandbox.host_path(candidate)), "proof-2")
        self.assert_canary(sandbox.host_path(candidate / "old" / "output"))

    def claim_with_host_pids(self, sandbox: RemovalSandbox, candidate: Path) -> subprocess.CompletedProcess[str]:
        bubblewrap = shutil.which("bwrap")
        self.assertIsNotNone(bubblewrap)
        assert bubblewrap is not None
        # Keep host PIDs visible. Only the fixture has a writable bind.
        return subprocess.run([
            bubblewrap, "--unshare-user", "--unshare-net", "--unshare-ipc", "--unshare-uts", "--die-with-parent",
            "--ro-bind", "/", "/", "--bind", str(sandbox.fixture), str(sandbox.fixture), "--clearenv",
            "--hostname", "natedev", "--chdir", str(sandbox.fixture),
            "--setenv", "HOME", str(sandbox.host_path(sandbox.home)),
            "--setenv", "CONF_DIR", str(sandbox.host_path(sandbox.home / "config")),
            "--setenv", "CLAUDE_DISABLE_DATACLASS_CODE_CACHE", "1",
            str(Path(sys.executable).resolve()), "-B", "-c", RUN_CLAIM, str(sandbox.checkout), str(candidate),
            ClaimCase.FOREIGN_OWNER.value,
        ], check=False, capture_output=True, text=True, timeout=90)

    def test_keeps_a_folder_with_a_held_lock(self) -> None:
        self.assert_kept(ClaimCase.HELD_LOCK)

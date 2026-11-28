"""Prove the removal boundary of a tool folder claim."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, redirect_stdout
from enum import Enum
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import cast
import unittest
from unittest import mock

from scripts.lib import build_folders, removal_guard
from scripts.lib.build_copies_test_support import write_private_build_roots
from scripts.lib.process_start import Gone, Started, Unreadable, proc_table_start
from scripts.lib.removal_bwrap_test_support import BuildMount, LINUX_CHECKOUT, RemovalSandbox, mac_profile
from scripts.lint.test_removal_bwrap import CANARY, PROBE_OUTSIDE


class ClaimCase(Enum):
    LEFTOVER = "leftover"
    SYMLINK = "symlink"
    NESTED_MOUNT = "nested-mount"
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

run_claim(Path(sys.argv[2]), ClaimCase(sys.argv[3]), *map(int, sys.argv[4:]))
"""


def read_guard(candidate: Path) -> removal_guard.RemovalGuard:
    names = removal_guard.KnownNames(frozenset(("proof",)), frozenset(("unit", "moved")))
    return removal_guard.read(build_top=candidate.parents[2], names=names)


def check_refusal(guard: removal_guard.RemovalGuard, path: Path,
                  reason: removal_guard.RefusalReason) -> None:
    try:
        with guard.check(path):
            raise AssertionError(f"the guard approved {path}")
    except removal_guard.Refused as error:
        assert error.reason is reason, error


def check_guard(candidate: Path, case: ClaimCase) -> None:
    guard = read_guard(candidate)
    if case is ClaimCase.SYMLINK:
        check_refusal(guard, candidate, removal_guard.RefusalReason.UNVERIFIED_PATH)
        return
    if case is ClaimCase.NESTED_MOUNT:
        check_refusal(guard, candidate, removal_guard.RefusalReason.MOUNT)
        return
    with guard.check(candidate) as checked:
        checked.recheck()
    if case is ClaimCase.LEFTOVER:
        # Keep the original tree for the tool claim.
        peer = candidate.with_name("guard-proof")
        _ = shutil.copytree(candidate, peer, symlinks=True)
        with guard.check(peer) as checked:
            removal_guard.remove(checked)
        assert not peer.exists()


def _check_missing_pid(pid: int) -> None:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    raise AssertionError(f"fixture process {pid} still exists")


def _lease_owner(case: ClaimCase, foreign_pid: int) -> int:
    if case is ClaimCase.LIVE_OWNER:
        return os.getpid()
    if case is ClaimCase.FOREIGN_OWNER:
        assert foreign_pid > 0
        if sys.platform == "linux":
            assert (Path("/proc") / str(foreign_pid)).stat().st_uid != os.geteuid()
        try:
            os.kill(foreign_pid, 0)
        except PermissionError:
            return foreign_pid
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


def run_claim(candidate: Path, case: ClaimCase, foreign_pid: int = 0) -> None:
    """Run the real claim with the selected fixture state."""
    if case is ClaimCase.NESTED_MOUNT:
        assert not build_folders.may_remove_folder_tree(candidate), f"the mount table allowed removal of {candidate}"
    check_guard(candidate, case)
    lease = build_folders.lease_path(candidate)
    _ = lease.write_text(json.dumps({"pid": _lease_owner(case, foreign_pid)}))
    if case is ClaimCase.OWNER_REPLACEMENT:
        _claim_after_other_owner(candidate)
        return
    probes: list[tuple[int, int, type[BaseException] | None]] = []
    kill = os.kill

    def record_kill(pid: int, signal_number: int, /) -> None:
        try:
            kill(pid, signal_number)
        except BaseException as error:
            probes.append((pid, signal_number, type(error)))
            raise
        probes.append((pid, signal_number, None))

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
        if case is ClaimCase.FOREIGN_OWNER:
            _ = fixture.enter_context(mock.patch.object(os, "kill", record_kill))
        try:
            with build_folders.tool_folder(candidate.name, candidate.parent) as claimed:
                print(claimed.name)
                assert not list(claimed.iterdir()), f"{claimed} contains output from the previous claim"
        except build_folders.RemovalTreeChanged:
            if case is not ClaimCase.STAGED_REPLACEMENT:
                raise
            print("replacement refused")
    if case is ClaimCase.FOREIGN_OWNER:
        outcomes = [(number, outcome) for pid, number, outcome in probes if pid == foreign_pid]
        assert any(number == 0 for number, _ in outcomes), f"the claim did not probe foreign lease owner PID {foreign_pid}"
        assert all(outcome is PermissionError for _, outcome in outcomes), (
            f"the claim expected PermissionError for foreign lease owner PID {foreign_pid}, got {outcomes}"
        )


class ToolFolderRemovalFixture(unittest.TestCase):
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
        candidate = sandbox.build / "proof" / "unit" / "proof"
        local = sandbox.host_path(candidate)
        self.canary(local / "old" / "output")
        _ = local.with_suffix(".lock").touch()
        return candidate

    def claim(self, sandbox: RemovalSandbox, candidate: Path,
              case: ClaimCase = ClaimCase.LEFTOVER) -> subprocess.CompletedProcess[str]:
        checkout = sandbox.checkout if sys.platform == "darwin" else LINUX_CHECKOUT
        return self.run_python(sandbox, candidate, RUN_CLAIM, str(checkout), str(candidate), case.value)

    def run_python(self, sandbox: RemovalSandbox, candidate: Path,
                   *arguments: str) -> subprocess.CompletedProcess[str]:
        if sys.platform == "linux" and sandbox.mount is BuildMount.BIND_UNIT:
            with mock.patch.object(RemovalSandbox, "build", candidate.parent):
                return sandbox.python("-c", *arguments)
        return sandbox.python("-c", *arguments)

    def assert_child_ok(self, result: subprocess.CompletedProcess[str]) -> None:
        if result.returncode == 0:
            return
        if result.returncode < 0:
            number = -result.returncode
            try:
                reason = signal.Signals(number).name
            except ValueError:
                reason = f"signal {number}"
        else:
            reason = f"exit {result.returncode}"
        command = shlex.join(cast(list[str], result.args))
        self.fail(f"{reason}: {command}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    def assert_claim(self, result: subprocess.CompletedProcess[str], name: str) -> None:
        self.assert_child_ok(result)
        self.assertEqual(result.stdout.strip(), name)


@unittest.skipUnless(sys.platform in ("linux", "darwin"), "requires Linux or macOS")
class ToolFolderRemovalTests(ToolFolderRemovalFixture):
    def test_removes_only_the_leftover(self) -> None:
        sandbox = self.sandbox()
        outside = sandbox.outside.read_bytes()
        probe = sandbox.python("-c", PROBE_OUTSIDE, str(sandbox.outside))
        self.assert_child_ok(probe)
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

    def test_mac_profile_limits_writes_and_protects_the_test_root(self) -> None:
        self.assertEqual(mac_profile(Path("/private/tmp/tool removal/fixture")), (
            "(version 1)\n"
            "(allow default)\n"
            "(deny network*)\n"
            "(deny file-write*)\n"
            '(allow file-write* (subpath "/private/tmp/tool removal/fixture") (literal "/dev/null"))\n'
            '(deny file-read-data (subpath "/private/tmp/tool removal"))\n'
            '(allow file-read-data (subpath "/private/tmp/tool removal/fixture"))\n'
        ))

    def test_keeps_a_symlink_at_the_tool_name(self) -> None:
        sandbox = self.sandbox()
        candidate = sandbox.build / "proof" / "unit" / "proof"
        local = sandbox.host_path(candidate)
        local.parent.mkdir(parents=True)
        target = sandbox.build / "other-tool"
        keep = sandbox.host_path(target / "output")
        self.canary(keep)
        local.symlink_to(target, target_is_directory=True)
        self.assert_claim(self.claim(sandbox, candidate, ClaimCase.SYMLINK), "proof-2")
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

    def test_second_claim_keeps_the_first_claim_live_owner(self) -> None:
        sandbox = self.sandbox()
        candidate = self.candidate(sandbox)
        result = self.claim(sandbox, candidate, ClaimCase.OWNER_REPLACEMENT)
        self.assert_claim(result, "proof-2")
        self.assert_canary(sandbox.host_path(candidate / "live"))

    def test_keeps_a_replacement_at_the_checked_staging_name(self) -> None:
        sandbox = self.sandbox()
        candidate = self.candidate(sandbox)
        result = self.claim(sandbox, candidate, ClaimCase.STAGED_REPLACEMENT)
        self.assert_claim(result, "replacement refused")
        parent = sandbox.host_path(candidate.parent)
        self.assert_canary(parent / "original-tree" / "old" / "output")
        staged = [path for path in parent.glob(".removing-*") if path.is_dir()]
        self.assertEqual(len(staged), 1, result.stdout + result.stderr)
        [replacement] = staged
        self.assert_canary(replacement / "replacement")

    def assert_kept(self, case: ClaimCase) -> None:
        sandbox = self.sandbox()
        candidate = self.candidate(sandbox)
        result = self.claim(sandbox, candidate, case)
        self.assert_claim(result, "proof-2")
        self.assert_canary(sandbox.host_path(candidate / "old" / "output"))

    def test_keeps_a_folder_with_an_unreadable_lease(self) -> None:
        self.assert_kept(ClaimCase.UNREADABLE_LEASE)

    def test_keeps_a_folder_with_a_live_lease_owner(self) -> None:
        self.assert_kept(ClaimCase.LIVE_OWNER)

    def foreign_lease_identity(self, process: Path) -> tuple[str, int]:
        pid = int(process.name)
        identity = proc_table_start(pid, process.parent)
        if isinstance(identity, Gone):
            self.fail(f"Foreign lease owner PID {pid} disappeared")
        if isinstance(identity, Unreadable):
            self.fail(f"Cannot read the start time for foreign lease owner PID {pid}: {identity.reason}")
        if not isinstance(identity, Started):
            self.fail(f"Foreign lease owner PID {pid} exited")
        try:
            owner = process.stat().st_uid
        except OSError as error:
            self.fail(f"Cannot read the UID for foreign lease owner PID {pid}: {error}")
        return identity.start, owner

    def test_keeps_a_folder_with_a_foreign_lease_owner(self) -> None:
        if sys.platform == "darwin":
            sandbox = self.sandbox()
            candidate = self.candidate(sandbox)
            with self.assertRaises(PermissionError):
                os.kill(1, 0)
            result = self.run_python(sandbox, candidate, RUN_CLAIM, str(sandbox.checkout), str(candidate),
                                     ClaimCase.FOREIGN_OWNER.value, "1")
            self.assert_claim(result, "proof-2")
            self.assert_canary(sandbox.host_path(candidate / "old" / "output"))
            return

        uid = os.geteuid()
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                owner = process.stat().st_uid
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if owner != uid:
                break
        else:
            self.skipTest("not run: the guest sandbox shows no process of another owner")

        foreign_pid = int(process.name)
        sandbox = self.sandbox()
        candidate = sandbox.host_path(self.candidate(sandbox))
        start, before_owner = self.foreign_lease_identity(process)
        self.assertEqual(before_owner, owner, f"Foreign lease owner PID {foreign_pid} changed UID before the claim")
        result = self.claim_with_host_pids(sandbox, candidate, foreign_pid)
        after_start, after_owner = self.foreign_lease_identity(process)
        self.assertEqual(after_start, start, f"Foreign lease owner PID {foreign_pid} changed start time during the claim")
        self.assertEqual(after_owner, before_owner, f"Foreign lease owner PID {foreign_pid} changed UID during the claim")
        self.assert_canary(candidate / "old" / "output")
        self.assert_claim(result, "proof-2")

    @unittest.skipIf(sys.platform == "darwin", "requires the Linux process table")
    def test_foreign_lease_owner_case_skips_when_no_foreign_process_is_visible(self) -> None:
        processes = [Path("/proc/self"), Path("/proc") / str(os.getpid())]
        with (mock.patch.object(Path, "iterdir", return_value=iter(processes)),
              mock.patch.object(self, "sandbox", side_effect=AssertionError("the claim started")),
              self.assertRaises(unittest.SkipTest) as skipped):
            self.test_keeps_a_folder_with_a_foreign_lease_owner()
        self.assertEqual(str(skipped.exception), "not run: the guest sandbox shows no process of another owner")

    def claim_with_host_pids(self, sandbox: RemovalSandbox, candidate: Path,
                             foreign_pid: int) -> subprocess.CompletedProcess[str]:
        bubblewrap = shutil.which("bwrap")
        self.assertIsNotNone(bubblewrap)
        assert bubblewrap is not None
        templates = " ".join(str(candidate.parents[2].joinpath(*form.value)) for form in removal_guard.Form)
        config = sandbox.host_path(sandbox.home / "config" / "build_folders.conf")
        write_private_build_roots(config, sandbox.fixture, "natedev")
        key = "removal.path_templates.natedev"
        source, replaced = re.subn(rf"(?m)^{re.escape(key)}=.*$", f"{key}={templates}", config.read_text())
        self.assertEqual(replaced, 1)
        _ = config.write_text(source)
        # Keep host PIDs visible. Only the fixture has a writable bind.
        return subprocess.run([
            bubblewrap, "--unshare-user", "--unshare-net", "--unshare-ipc", "--unshare-uts", "--die-with-parent",
            "--ro-bind", "/", "/", "--bind", str(sandbox.fixture), str(sandbox.fixture), "--clearenv",
            "--hostname", "natedev", "--chdir", str(sandbox.fixture),
            "--setenv", "HOME", str(sandbox.host_path(sandbox.home)),
            "--setenv", "CONF_DIR", str(sandbox.host_path(sandbox.home / "config")),
            "--setenv", "CLAUDE_DISABLE_DATACLASS_CODE_CACHE", "1",
            str(Path(sys.executable).resolve()), "-B", "-c", RUN_CLAIM, str(sandbox.checkout), str(candidate),
            ClaimCase.FOREIGN_OWNER.value, str(foreign_pid),
        ], check=False, capture_output=True, text=True, timeout=90)

    def test_keeps_a_folder_with_a_held_lock(self) -> None:
        self.assert_kept(ClaimCase.HELD_LOCK)


if __name__ == "__main__":
    _ = unittest.main()

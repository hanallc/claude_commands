"""Prove the manual removal boundary in the platform sandbox."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.lib import removal_guard
from scripts.lib.removal_bwrap_test_support import LINUX_CHECKOUT, BuildMount, RemovalSandbox

CANARY = "keep this content\n"
BUILD = "scripts/lint/sweep.py"
PATH_TAIL = r"""[^\s'"<>()\[\]{},;:]*"""
PROBE_OUTSIDE = """
import errno
from pathlib import Path
import sys

if sys.platform == "darwin":
    try:
        Path(sys.argv[1]).read_bytes()
    except OSError as error:
        assert error.errno in (errno.EPERM, errno.EACCES), error
    else:
        raise AssertionError("the sandbox allowed a read outside the fixture")

try:
    Path(sys.argv[1]).unlink()
except OSError as error:
    if sys.platform == "darwin":
        assert error.errno in (errno.EPERM, errno.EACCES), error
    else:
        assert error.errno in (errno.ENOENT, errno.EROFS, errno.EPERM, errno.EACCES), error
else:
    raise AssertionError("the sandbox allowed removal outside the fixture")
"""
PROBE_MOUNT = """
from pathlib import Path
import sys

mounts = Path("/proc/self/mountinfo").read_text().splitlines()
assert any(line.split()[4] == sys.argv[1] for line in mounts), sys.argv[1]
"""
CLEANUP = "scripts/disks/cleanup_old_folders.py"
PARENT_CLEANUP_WITH_PROCESS_FIXTURE = """
import json
from pathlib import Path
import subprocess
import sys

checkout, proc, *arguments = sys.argv[1:]
sys.path.insert(0, checkout)
from scripts.disks import cleanup_old_folders as cleanup

def fake_ssh(args):
    assert args[:4] == ("ssh", "-o", "BatchMode=yes", "mac"), args
    assert args[-1].endswith(" --json"), args
    assert (Path.home() / "rust/hanadocs/payload").read_text() == "keep this content\\n"
    mac = cleanup.HostList(cleanup.Machine.MAC, ()).record()
    return subprocess.CompletedProcess(args, 0, json.dumps(mac), "")

cleanup.PROC_ROOT = proc
raise SystemExit(cleanup.run_cli(arguments, run_ssh=fake_ssh))
"""


class RemovalProofTests(unittest.TestCase):
    def sandbox(self) -> RemovalSandbox:
        parent = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="removal-proof-"))).resolve()
        sandbox = RemovalSandbox(parent / "fixture")
        sandbox.configure()
        outside = sandbox.outside.read_bytes()
        result = sandbox.python("-c", PROBE_OUTSIDE, str(sandbox.outside))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(sandbox.outside.read_bytes(), outside)
        return sandbox

    def canary(self, sandbox: RemovalSandbox, path: Path) -> Path:
        local = sandbox.host_path(path)
        local.parent.mkdir(parents=True, exist_ok=True)
        _ = local.write_text(CANARY)
        return path

    def assert_canaries(self, sandbox: RemovalSandbox, paths: tuple[Path, ...]) -> None:
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(sandbox.host_path(path).read_text(), CANARY)

    def tool(self, sandbox: RemovalSandbox, script: str, *args: str,
             status: int = 0) -> subprocess.CompletedProcess[str]:
        result = sandbox.script(script, *args)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, status, output)
        paths = re.compile(rf"(?<![\w/])(?:{re.escape(str(sandbox.fixture))}|/(?!/)){PATH_TAIL}")
        for match in paths.finditer(output):
            path = Path(match[0])
            self.assertTrue(sandbox.contains(path), f"{path} is outside the fixture:\n{output}")
        return result

    def read_plan(self, sandbox: RemovalSandbox, result: subprocess.CompletedProcess[str],
                  expected: Path) -> tuple[Path, bytes, removal_guard.ListedEntry]:
        plans = [Path(line.removeprefix("removal list: ")) for line in result.stdout.splitlines()
                 if line.startswith("removal list: ")]
        self.assertEqual(len(plans), 1, result.stdout + result.stderr)
        plan = plans[0]
        self.assertEqual(plan.parent, sandbox.home / removal_guard.LIST_DIRECTORY)
        data = sandbox.host_path(plan).read_bytes()
        rows = [removal_guard.ListedEntry.parse(line) for line in data.decode().splitlines()]
        self.assertEqual([row.path for row in rows], [expected])
        [row] = rows
        for path in (row.path, row.anchor):
            self.assertTrue(sandbox.contains(path), f"{path} is outside the fixture")
        if isinstance(row.evidence, Path):
            self.assertTrue(sandbox.contains(row.evidence), f"{row.evidence} is outside the fixture")
        return plan, data, row

    def remove(self, sandbox: RemovalSandbox, plan: Path, digest: str, *,
               status: int = 0) -> subprocess.CompletedProcess[str]:
        return self.tool(sandbox, "scripts/lib/removal_guard.py", "remove", "--list", str(plan),
                         "--sha256", digest, status=status)

    def prove(self, sandbox: RemovalSandbox, script: str, args: tuple[str, ...], *,
              manual: tuple[Path, ...] = ()) -> None:
        candidate = sandbox.build / "proof/unit/short"
        keep = tuple(self.canary(sandbox, path) for path in (
            sandbox.build / "session/keep", sandbox.build / "claude/scratch/keep",
            sandbox.home / "top-level", sandbox.home / "rust/keep", sandbox.home / "worktrees/keep",
            sandbox.home / ".claude/keep", sandbox.home / "nixos/keep",
        ))
        payload = self.canary(sandbox, candidate / "output")
        preview = self.tool(sandbox, script, *args, "--dry-run")
        self.assertNotIn("removal list:", preview.stdout)
        self.assertFalse(sandbox.host_path(sandbox.home / removal_guard.LIST_DIRECTORY).exists())
        self.assert_canaries(sandbox, (*keep, payload))

        plan, approved, row = self.read_plan(sandbox, self.tool(sandbox, script, *args),
                                              manual[0] if manual else candidate)
        if manual:
            [entry] = manual
            self.assertEqual(row.fingerprint, "")
            refused = self.remove(sandbox, plan, hashlib.sha256(approved).hexdigest())
            self.assertIn(f"kept {str(entry)!r}", refused.stdout)
            self.assertEqual(sandbox.host_path(entry / "keep").read_text(), CANARY)
            plan, approved, row = self.read_plan(
                sandbox, self.tool(sandbox, BUILD, "--target-dir", str(candidate)), candidate)

        self.assert_canaries(sandbox, (*keep, payload))
        self.assertEqual(row.anchor, candidate)
        self.assertTrue(row.fingerprint)
        self.assertIs(row.evidence, removal_guard.UnknownMaker.NO_EVIDENCE)
        digest = hashlib.sha256(approved).hexdigest()
        edited = plan.with_name("edited.jsonl")
        forbidden = keep[0].parent
        stamp = removal_guard.fingerprint(sandbox.host_path(forbidden))
        changed = replace(row, path=forbidden, anchor=forbidden, fingerprint=stamp).line() + "\n"
        _ = sandbox.host_path(edited).write_text(changed)
        invalid = self.remove(sandbox, edited, digest, status=1)
        self.assertIn("SHA-256 does not match", invalid.stderr)
        refused = self.remove(sandbox, edited, hashlib.sha256(changed.encode()).hexdigest())
        self.assertEqual(refused.stdout.splitlines(), [
            f"kept {str(forbidden)!r}: {removal_guard.RefusalReason.OUTSIDE_ROOT.value}",
        ])
        self.assert_canaries(sandbox, (*keep, payload))
        removed = self.remove(sandbox, plan, digest)
        self.assertIn(f"removed {str(candidate)!r}", removed.stdout)
        self.assertFalse(sandbox.host_path(candidate).exists())
        self.assert_canaries(sandbox, keep)

    def test_temporary_sweep_lists_for_manual_review(self) -> None:
        sandbox = self.sandbox()
        candidate = sandbox.temporary / "verify-hold.999999.abcdefgh"
        payload = self.canary(sandbox, candidate / "keep")
        old = 1_700_000_000
        for path in (payload, candidate):
            os.utime(sandbox.host_path(path), (old, old))
        self.prove(sandbox, "scripts/lint/tmp_sweep.py", (), manual=(candidate,))

    def test_build_sweep_removes_only_the_reviewed_build_form(self) -> None:
        sandbox = self.sandbox()
        self.prove(sandbox, BUILD, ("--target-dir", str(sandbox.build / "proof/unit/short")))

    def test_cleanup_removes_only_the_approved_exact_folder(self) -> None:
        sandbox = self.sandbox()
        candidate = sandbox.home / "old-output"
        payload = self.canary(sandbox, candidate / "payload")
        bare = sandbox.home / "old-archive" / "saved.git"
        model = sandbox.build / "proof" / "unit" / "short"
        worktree = sandbox.home / "old-worktree"
        hanadocs = sandbox.home / "rust" / "hanadocs"
        keep = tuple(self.canary(sandbox, path) for path in (
            sandbox.home / "old-output-next/payload", hanadocs / "payload", model / "payload", worktree / ".git",
            bare / "HEAD", bare / "objects" / "object", bare / "refs" / "head",
        ))
        paths = ", ".join(map(str, (candidate, hanadocs, model, model.parent.parent, worktree, bare.parent)))
        _ = sandbox.host_path(sandbox.home / "config/disks.conf").write_text(
            f"[cleanup]\ncleanup.dead.natedev={paths}\ncleanup.dead.mac={paths}\n"
            + f"cleanup.list_only.natedev={sandbox.build / 'sccache'}\ncleanup.list_only.mac=~/build/slot\n"
            + "cleanup.list_only_reason.natedev=shared tool cache\ncleanup.list_only_reason.mac=bob's hand step\n")

        proc = sandbox.home / "proc"
        if sys.platform == "linux":
            sandbox.host_path(proc / "self").mkdir(parents=True)
            _ = sandbox.host_path(proc / "self/status").write_text("CapPrm: 0000000000000000\n")
            cli = sandbox.script(CLEANUP, "--host-list")
            self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
            self.assertIn(f"{str(candidate)!r}", cli.stdout)

        def run_parent(*arguments: str) -> subprocess.CompletedProcess[str]:
            return sandbox.python("-c", PARENT_CLEANUP_WITH_PROCESS_FIXTURE, str(LINUX_CHECKOUT),
                                  str(proc), *arguments)

        if sys.platform == "linux":
            pid = proc / "42"
            sandbox.host_path(pid / "fd").mkdir(parents=True)
            _ = sandbox.host_path(pid / "status").write_text(
                f"Uid: {os.geteuid()} {os.geteuid()}\nState: S\nThreads: 1\n")
            _ = sandbox.host_path(pid / "environ").write_bytes(b"")
            sandbox.host_path(pid / "cwd").symlink_to(candidate)
            busy = run_parent()
            self.assertEqual(busy.returncode, 0, busy.stdout + busy.stderr)
            self.assertIn(f"{str(candidate)!r}", busy.stdout)
            self.assertIn("pid 42 works in it", busy.stdout)
            self.assertNotIn("would remove", busy.stdout)
            self.assertIn("mac total: 0 bytes", busy.stdout)
            shutil.rmtree(sandbox.host_path(pid))

        def run_cleanup(*arguments: str) -> subprocess.CompletedProcess[str]:
            if sys.platform == "linux":
                return run_parent("--host-list", *arguments)
            return sandbox.script(CLEANUP, "--host-list", *arguments)

        preview = run_cleanup()
        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertIn(f"{str(candidate)!r}", preview.stdout)
        self.assertEqual(preview.stdout.count("would remove"), 1, preview.stdout)
        for git_work in (worktree, bare.parent):
            self.assertRegex(preview.stdout, rf"{str(git_work)!r}\t[0-9]+\tholds Git work: the owner saves it,")
        self.assertIn(f"{model.parent.parent.as_posix()!r}\tunknown\tcontains a model folder", preview.stdout)
        self.assert_canaries(sandbox, (payload, *keep))
        approval = preview.stdout.splitlines()[-1].removeprefix("Digest: ")
        refused = run_cleanup("--go", "0" * 64)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("list digest changed", refused.stderr)
        self.assert_canaries(sandbox, (payload, *keep))
        removed = run_cleanup("--go", approval)
        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertEqual(removed.stdout.splitlines(), [f"removed {str(candidate)!r}"])
        self.assertFalse(sandbox.host_path(candidate).exists())
        self.assert_canaries(sandbox, keep)

    @unittest.skipUnless(sys.platform == "linux", "requires a Linux bind mount")
    def test_removal_refuses_a_bind_mount_above_the_reviewed_folder(self) -> None:
        sandbox = self.sandbox()
        candidate = sandbox.build / "proof/unit/short"
        payload = self.canary(sandbox, candidate / "output")
        plan, data, row = self.read_plan(sandbox, self.tool(sandbox, BUILD, "--target-dir", str(candidate)), candidate)
        self.assertTrue(row.fingerprint)

        outside = sandbox.bind_source / "short" / "output"
        outside.parent.mkdir(parents=True)
        _ = outside.write_text(CANARY)
        mounted = replace(sandbox, mount=BuildMount.BIND_UNIT)
        probe = mounted.python("-c", PROBE_MOUNT, str(candidate.parent))
        self.assertEqual(probe.returncode, 0, probe.stdout + probe.stderr)
        result = self.remove(mounted, plan, hashlib.sha256(data).hexdigest())
        self.assertIn(f"kept {str(candidate)!r}: {removal_guard.RefusalReason.MOUNT.value}", result.stdout)
        self.assertEqual(outside.read_text(), CANARY)
        self.assert_canaries(sandbox, (payload,))

    def test_host_path_refuses_parent_steps_and_links_outside_the_fixture(self) -> None:
        sandbox = self.sandbox()
        with self.assertRaises(AssertionError):
            _ = sandbox.host_path(sandbox.home / ".." / "escape")
        outside = sandbox.outside if sys.platform == "darwin" else Path("/outside")
        with self.assertRaises(AssertionError):
            _ = sandbox.host_path(outside)
        link = sandbox.home / "link"
        sandbox.host_path(link).symlink_to(sandbox.outside)
        with self.assertRaises(AssertionError):
            _ = sandbox.host_path(link)

"""Prove the manual removal boundary in the platform sandbox."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from scripts.lib import removal_guard
from scripts.lib.removal_bwrap_test_support import BuildMount, RemovalSandbox

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

#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""The Mac fix pass returns its changed files only to an unchanged worktree."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.lint.memory_test_support import config_environment
from scripts.mac_test import eligibility, fix_return, offload
from scripts.mac_test.offload_test_commands import write_shell
from scripts.lib import test_hang_guard

HOST = "fixture-host"
REPOSITORY = eligibility.KnownRepository("claude")

# Run the real rsync with the remote side mapped into a local folder.
FAKE_RSYNC = r'''
printf '%s\n' "$*" >> "$FIX_TEST_DIR/rsync-calls"
words=()
while (( $# )); do
    case $1 in
        -e) shift ;;
        fixture-host:/*) words+=("$FIX_TEST_REMOTE${1#fixture-host:}") ;;
        fixture-host:*) words+=("$FIX_TEST_REMOTE/${1#fixture-host:}") ;;
        *) words+=("$1") ;;
    esac
    shift
done
exec "$FIX_TEST_RSYNC" "${words[@]}"
'''


def git(root: Path, *words: str) -> None:
    _ = subprocess.run(["git", "-C", str(root), *words], check=True, capture_output=True)


class ReturnFixTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.worktree: Path = Path()
        self.mirror: Path = Path()

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        real_rsync = shutil.which("rsync")
        if real_rsync is None:
            self.skipTest("rsync is not installed")
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        write_shell(bin_directory / "rsync", FAKE_RSYNC)
        remote = self.root / "remote"
        _ = cast(object, self.enterContext(patch.dict(os.environ, {
            **config_environment(self.root / "configuration"),
            "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
            "FIX_TEST_DIR": str(self.root), "FIX_TEST_REMOTE": str(remote), "FIX_TEST_RSYNC": real_rsync,
        })))
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        git(self.worktree, "init", "-q")
        _ = (self.worktree / ".gitignore").write_text("out.log\ntarget/\n")
        _ = (self.worktree / "a.rs").write_text("fn a() {}\n")
        _ = (self.worktree / "b.rs").write_text("fn b() {}\n")
        _ = (self.worktree / "d.rs").write_text("fn d() {}\n")
        git(self.worktree, "add", "-A")
        git(self.worktree, "-c", "user.name=fixture", "-c", "user.email=fixture@example.com",
            "commit", "-q", "-m", "fixture")
        (self.worktree / "target").mkdir()
        _ = (self.worktree / "target/build.txt").write_text("natedev build\n")
        mirror_text = offload.remote_mirror(REPOSITORY, "mac", 1)
        self.mirror = remote / mirror_text.lstrip("/") if mirror_text.startswith("/") else remote / mirror_text
        self.mirror.parent.mkdir(parents=True)
        _ = shutil.copytree(self.worktree, self.mirror, ignore=shutil.ignore_patterns(".git", "target"))

    def fix_the_mirror(self) -> None:
        """Make the edits a Mac fix pass and a Mac build make in the mirror."""
        _ = (self.mirror / "a.rs").write_text("pub(crate) fn a() {}\n")
        _ = (self.mirror / "c.rs").write_text("fn c() {}\n")
        (self.mirror / "b.rs").unlink()
        (self.mirror / "d.rs").chmod(0o755)
        _ = (self.mirror / "out.log").write_text("mac log\n")
        _ = (self.mirror / ".mac-test-mirror").write_text("mark\n")
        (self.mirror / "target").mkdir()
        _ = (self.mirror / "target/build.txt").write_text("mac build\n")

    def return_fix(self, sent: offload.TreeKey) -> fix_return.FixResult:
        config = eligibility.OffloadConfig(
            enabled=True, connection=eligibility.SshConnectionSettings(HOST, 6, 10), probe_timeout_s=1,
            unreachable_backoff_s=300, free_floor_gib=1, default_target_budget_gib="24", values={},
        )
        request = offload.OffloadRequest(
            self.worktree, "", "fix-call", offload.NO_RESULT_FILE, (), owner="verify",
            holder_session="fixture-session", verb="mend-fix", target="mac")
        return offload.return_fix(offload.ProcessController(), config, request, REPOSITORY, sent)

    def test_mac_fix_returns_the_patch(self) -> None:
        sent = offload.read_tree_key(self.worktree)
        self.fix_the_mirror()
        result = self.return_fix(sent)
        self.assertEqual(result, fix_return.FixApplied(("a.rs", "c.rs"), ("b.rs",)))
        self.assertEqual((self.worktree / "a.rs").read_text(), "pub(crate) fn a() {}\n")
        self.assertEqual((self.worktree / "c.rs").read_text(), "fn c() {}\n")
        self.assertFalse((self.worktree / "b.rs").exists())
        # A mode change alone, an ignored file, the mirror mark and the build output stay out.
        self.assertEqual((self.worktree / "d.rs").stat().st_mode & 0o777, 0o644)
        self.assertFalse((self.worktree / "out.log").exists())
        self.assertFalse((self.worktree / ".mac-test-mirror").exists())
        self.assertEqual((self.worktree / "target/build.txt").read_text(), "natedev build\n")
        self.assertEqual(sorted(path.name for path in self.worktree.glob(".*mac-fix-*")), [])
        calls = (self.root / "rsync-calls").read_text()
        self.assertIn("--files-from", calls)

    def test_changed_worktree_refuses_the_patch(self) -> None:
        sent = offload.read_tree_key(self.worktree)
        self.fix_the_mirror()
        _ = (self.worktree / "a.rs").write_text("fn a() { edited }\n")
        result = self.return_fix(sent)
        self.assertEqual(result, fix_return.FixRefused("the worktree changed after the copy to the Mac"))
        self.assertEqual((self.worktree / "a.rs").read_text(), "fn a() { edited }\n")
        self.assertFalse((self.worktree / "c.rs").exists())
        self.assertTrue((self.worktree / "b.rs").exists())

    def test_unknown_sent_key_refuses_the_patch(self) -> None:
        self.fix_the_mirror()
        result = self.return_fix(offload.UNAVAILABLE_TREE_KEY)
        self.assertEqual(result, fix_return.FixRefused("the copy sent no tree key"))
        self.assertEqual((self.worktree / "a.rs").read_text(), "fn a() {}\n")

    def test_unchanged_mirror_writes_nothing(self) -> None:
        result = self.return_fix(offload.read_tree_key(self.worktree))
        self.assertEqual(result, fix_return.FixNone())
        self.assertNotIn("--files-from", (self.root / "rsync-calls").read_text())


FIX_RETURN = Path(fix_return.__file__)
VERIFY = FIX_RETURN.parents[1] / "direct/verify.sh"

# Apply a fix in a child that exits with no cleanup, as SIGKILL does, at one point of the return.
STOPPED_CHILD = r'''
import os, sys
from pathlib import Path
from scripts.mac_test import fix_return
worktree, staging, stop_at = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
stage, replace = fix_return.stage_file, os.replace
staged: list[Path] = []
renamed: list[str] = []

def stage_then_stop(source, temporary, destination):
    staged.append(source)
    if stop_at == "staging" and len(staged) == 2:
        os._exit(137)
    stage(source, temporary, destination)

def rename_then_stop(source, destination):
    replace(source, destination)
    if fix_return.TEMPORARY_MARK in str(source):
        renamed.append(str(destination))
        if stop_at == "renames" and len(renamed) == 1:
            os._exit(137)

fix_return.stage_file = stage_then_stop
fix_return.os.replace = rename_then_stop
changes = fix_return.MirrorChanges(("a.rs", "src/b.rs", "c.rs"), ("gone.rs",))
print(fix_return.apply_fix(worktree, staging, changes, lambda: True))
'''

# The start of verify.sh: finish_mac_fix with verify_helper running the program in a process.
VERIFY_START = r'''
VERIFY_START_DIR=$1
verify_helper() { "$VERIFY_PYTHON" "$@"; }
'''


def verify_start_function() -> str:
    body = VERIFY.read_text().split("\nfinish_mac_fix() {", 1)[1].split("\n}\n", 1)[0]
    return "finish_mac_fix() {" + body + "\n}\nfinish_mac_fix\n"


class RollForwardTests(unittest.TestCase):
    """A stop at any point of the return is completed by the next verify.sh run."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.worktree: Path = Path()
        self.staging: Path = Path()

    @override
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.worktree = root / "worktree"
        self.staging = root / "staging"
        (self.worktree / "src").mkdir(parents=True)
        (self.staging / "src").mkdir(parents=True)
        for name in ("a.rs", "src/b.rs", "c.rs"):
            _ = (self.worktree / name).write_text(f"old {name}\n")
            _ = (self.staging / name).write_text(f"fixed {name}\n")
        _ = (self.worktree / "gone.rs").write_text("removed by the fix\n")
        git(self.worktree, "init", "-q")
        git(self.worktree, "add", "-A")
        git(self.worktree, "-c", "user.name=fixture", "-c", "user.email=fixture@example.com",
            "commit", "-q", "-m", "fixture")

    def stop_return(self, stop_at: str) -> None:
        stopped = subprocess.run(
            [sys.executable, "-c", STOPPED_CHILD, str(self.worktree), str(self.staging), stop_at],
            capture_output=True, text=True, check=False, timeout=test_hang_guard.TEST_HANG_GUARD_SECS,
            env={**os.environ, "PYTHONPATH": str(FIX_RETURN.parents[2])},
        )
        self.assertEqual(stopped.returncode, 137, stopped.stdout + stopped.stderr)

    def next_verify_start(self) -> str:
        started = subprocess.run(
            ["bash", "-c", VERIFY_START + verify_start_function(), "verify", str(VERIFY.parent)],
            cwd=self.worktree / "src", capture_output=True, text=True, check=False,
            timeout=test_hang_guard.TEST_HANG_GUARD_SECS, env={**os.environ, "VERIFY_PYTHON": sys.executable},
        )
        self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
        return started.stdout

    def files(self) -> dict[str, str]:
        return {str(path.relative_to(self.worktree)): path.read_text()
                for path in sorted(self.worktree.rglob("*")) if path.is_file() and ".git" not in path.parts}

    def record(self) -> Path:
        return fix_return.git_folder_path(self.worktree, fix_return.RECORD_NAME)

    def test_stop_while_staging_leaves_the_worktree_unchanged(self) -> None:
        self.stop_return("staging")
        self.assertEqual(fix_return.read_record(self.record())["state"], "staging")
        self.assertEqual(len([name for name in self.files() if fix_return.TEMPORARY_MARK in name]), 1)
        output = self.next_verify_start()
        self.assertIn("the worktree is unchanged", output)
        self.assertEqual(self.files(), {"a.rs": "old a.rs\n", "c.rs": "old c.rs\n",
                                        "gone.rs": "removed by the fix\n", "src/b.rs": "old src/b.rs\n"})
        self.assertFalse(self.record().exists())

    def test_stop_between_renames_is_finished_by_the_next_run(self) -> None:
        self.stop_return("renames")
        self.assertEqual(fix_return.read_record(self.record())["state"], "commit")
        self.assertEqual(self.files()["a.rs"], "fixed a.rs\n")
        self.assertEqual(self.files()["src/b.rs"], "old src/b.rs\n")
        output = self.next_verify_start()
        self.assertIn("finished a stopped Mac fix: 3 file(s) written, 1 removed", output)
        self.assertEqual(self.files(), {"a.rs": "fixed a.rs\n", "c.rs": "fixed c.rs\n", "src/b.rs": "fixed src/b.rs\n"})
        self.assertFalse(self.record().exists())

    def test_whole_return_leaves_no_record(self) -> None:
        changes = fix_return.MirrorChanges(("a.rs", "src/b.rs", "c.rs"), ("gone.rs",))
        result = fix_return.apply_fix(self.worktree, self.staging, changes, lambda: True)
        self.assertEqual(result, fix_return.FixApplied(changes.changed, changes.removed))
        self.assertEqual(self.files(), {"a.rs": "fixed a.rs\n", "c.rs": "fixed c.rs\n", "src/b.rs": "fixed src/b.rs\n"})
        self.assertFalse(self.record().exists())
        self.assertEqual(self.next_verify_start(), "")


class ParseItemizedTests(unittest.TestCase):
    def test_counts_content_and_new_files_only(self) -> None:
        listing = "\n".join([
            ">f.st...... a.rs",
            ">f+++++++++ c.rs",
            ".f...p..... d.rs",
            ">fc........ e.rs",
            "cd+++++++++ src/",
            "*deleting   b.rs",
            "*deleting   old/",
            "Number of files: 4",
        ])
        self.assertEqual(fix_return.parse_itemized(listing),
                         fix_return.MirrorChanges(("a.rs", "c.rs", "e.rs"), ("b.rs",)))

    def test_link_change_fails(self) -> None:
        result = fix_return.parse_itemized("cL+++++++++ link.rs -> a.rs")
        self.assertIsInstance(result, fix_return.FixFailed)


class ApplyFixTests(unittest.TestCase):
    def test_path_outside_the_worktree_fails(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staging = root / "staging"
        staging.mkdir()
        worktree = root / "worktree"
        worktree.mkdir()
        result = fix_return.apply_fix(worktree, staging, fix_return.MirrorChanges(("../escape.rs",), ()), lambda: True)
        self.assertIsInstance(result, fix_return.FixFailed)
        self.assertFalse((root / "escape.rs").exists())


if __name__ == "__main__":
    _ = unittest.main()

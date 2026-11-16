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
from typing import Literal, cast, override
from unittest.mock import patch

from scripts.lib import berth_board
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


def commit_all(root: Path) -> None:
    git(root, "add", "-A")
    git(root, "-c", "user.name=fixture", "-c", "user.email=fixture@example.com", "commit", "-q", "-m", "fixture")


def reservation(reservation_id: str, root: Path, branch: str, *scopes: str,
                status: Literal["blocking", "clear"] = "blocking", lifecycle: str = "active",
                protected_tip: str = "") -> berth_board.Reservation:
    return berth_board.Reservation(reservation_id, "lint", root, branch, scopes, lifecycle, status, protected_tip)


def head(root: Path) -> str:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


UNCONFIGURED = berth_board.Board((), configured=False)


def cargo_workspace_search(manifest: Path) -> list[Path]:
    """The Cargo.toml files that cargo reads above a package to find its workspace.

    Cargo starts above the package folder and stops at a folder that ends in
    target/package. The test models the rule, because tests must not run real cargo.
    """
    found: list[Path] = []
    for folder in manifest.parent.parents:
        if folder.parts[-2:] == ("target", "package"):
            break
        if (folder / "Cargo.toml").exists():
            found.append(folder / "Cargo.toml")
    return found


def unconfigured(_: Path) -> berth_board.Board:
    return UNCONFIGURED


class ReturnFixTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.worktree: Path = Path()
        self.mirror: Path = Path()
        self.board: berth_board.Board = UNCONFIGURED

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
        self.board = UNCONFIGURED
        _ = cast(object, self.enterContext(patch.object(fix_return, "read_board", self.read_board)))
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        git(self.worktree, "init", "-q")
        _ = (self.worktree / ".gitignore").write_text("out.log\ntarget/\n")
        _ = (self.worktree / "a.rs").write_text("fn a() {}\n")
        _ = (self.worktree / "b.rs").write_text("fn b() {}\n")
        _ = (self.worktree / "d.rs").write_text("fn d() {}\n")
        commit_all(self.worktree)
        (self.worktree / "target").mkdir()
        _ = (self.worktree / "target/build.txt").write_text("natedev build\n")
        mirror_text = offload.remote_mirror(REPOSITORY, "mac", 1)
        self.mirror = remote / mirror_text.lstrip("/") if mirror_text.startswith("/") else remote / mirror_text
        self.mirror.parent.mkdir(parents=True)
        _ = shutil.copytree(self.worktree, self.mirror, ignore=shutil.ignore_patterns(".git", "target"))

    def read_board(self, _: Path) -> berth_board.Board:
        return self.board

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
        self.assertEqual(result, fix_return.FixRefused("the worktree changed after the copy"))
        self.assertEqual((self.worktree / "a.rs").read_text(), "fn a() { edited }\n")
        self.assertFalse((self.worktree / "c.rs").exists())
        self.assertTrue((self.worktree / "b.rs").exists())

    def test_unknown_sent_key_refuses_the_patch(self) -> None:
        self.fix_the_mirror()
        result = self.return_fix(offload.UNAVAILABLE_TREE_KEY)
        self.assertEqual(result, fix_return.FixRefused("the copy sent no tree key"))
        self.assertEqual((self.worktree / "a.rs").read_text(), "fn a() {}\n")

    def test_mac_fix_writes_held_paths_only(self) -> None:
        self.board = berth_board.Board((
            reservation("own-1", self.worktree, "mine", "file:a.rs"),
            reservation("other-2", self.root / "other", "theirs", "file:c.rs"),
        ))
        sent = offload.read_tree_key(self.worktree)
        self.fix_the_mirror()
        result = self.return_fix(sent)
        self.assertEqual(result, fix_return.FixApplied(("a.rs",), (), (
            fix_return.Withheld("c.rs", "other-2 (theirs)"), fix_return.Withheld("b.rs", ""))))
        self.assertEqual(fix_return.fix_word(result), "withheld")
        self.assertEqual((self.worktree / "a.rs").read_text(), "pub(crate) fn a() {}\n")
        self.assertFalse((self.worktree / "c.rs").exists())
        self.assertEqual((self.worktree / "b.rs").read_text(), "fn b() {}\n")

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
unconfigured = fix_return.berth_board.Board((), configured=False)
print(fix_return.apply_fix(worktree, staging, changes, lambda: True, lambda _: unconfigured))
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


class HoldFilterTests(unittest.TestCase):
    ROOT: Path = Path("/fixture/worktree")
    CHANGES: fix_return.MirrorChanges = fix_return.MirrorChanges(
        ("src/held.rs", "src/foreign.rs", "src/free.rs", "src/clear.rs"), ("src/gone.rs",))

    def hold(self, *entries: berth_board.Reservation) -> tuple[fix_return.MirrorChanges, tuple[fix_return.Withheld, ...]]:
        return fix_return.hold_filter(self.ROOT, self.CHANGES, berth_board.Board(entries))

    def test_own_reservation_keeps_and_others_are_withheld(self) -> None:
        kept, withheld = self.hold(
            reservation("own-1", self.ROOT, "mine", "tree:src/held.rs", "file:src/gone.rs"),
            reservation("other-2", Path("/fixture/other"), "theirs", "file:src/foreign.rs"),
            reservation("other-3", Path("/fixture/other"), "theirs", "file:src/clear.rs", status="clear"),
        )
        self.assertEqual(kept, fix_return.MirrorChanges(("src/held.rs",), ("src/gone.rs",)))
        self.assertEqual(withheld, (fix_return.Withheld("src/foreign.rs", "other-2 (theirs)"),
                                    fix_return.Withheld("src/free.rs", ""), fix_return.Withheld("src/clear.rs", "")))

    def test_foreign_reservation_wins_over_an_own_tree(self) -> None:
        kept, withheld = self.hold(
            reservation("own-1", self.ROOT, "mine", "tree:src"),
            reservation("other-2", Path("/fixture/other"), "theirs", "file:src/foreign.rs"),
        )
        self.assertEqual(kept, fix_return.MirrorChanges(
            ("src/held.rs", "src/free.rs", "src/clear.rs"), ("src/gone.rs",)))
        self.assertEqual(withheld, (fix_return.Withheld("src/foreign.rs", "other-2 (theirs)"),))

    def test_outstanding_reservation_whose_tip_is_in_head_history_does_not_withhold(self) -> None:
        # A later phase branch holds hook.rs; an earlier phase's outstanding reservation also lists it.
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        git(root, "init", "-q")
        _ = (root / "hook.rs").write_text("earlier phase\n")
        commit_all(root)
        merged = head(root)
        git(root, "checkout", "-q", "-b", "side")
        _ = (root / "hook.rs").write_text("side branch\n")
        commit_all(root)
        unmerged = head(root)
        git(root, "checkout", "-q", "-")
        _ = (root / "hook.rs").write_text("later phase\n")
        commit_all(root)
        changes = fix_return.MirrorChanges(("hook.rs",), ())
        own = reservation("own-66", root, "tile-phase-66", "file:hook.rs")

        def hold(tip: str, lifecycle: str = "outstanding") -> tuple[fix_return.MirrorChanges, tuple[fix_return.Withheld, ...]]:
            earlier = reservation("other-43j", Path("/fixture/other"), "tile-phase-43j", "file:hook.rs",
                                  lifecycle=lifecycle, protected_tip=tip)
            return fix_return.hold_filter(root, changes, berth_board.Board((own, earlier)))

        self.assertEqual(hold(merged), (changes, ()))
        withheld = (fix_return.Withheld("hook.rs", "other-43j (tile-phase-43j)"),)
        for tip, lifecycle in ((unmerged, "outstanding"), ("0" * 40, "outstanding"), (merged, "active")):
            with self.subTest(tip=tip, lifecycle=lifecycle):
                self.assertEqual(hold(tip, lifecycle), (fix_return.MirrorChanges((), ()), withheld))

    def test_unconfigured_board_keeps_every_path(self) -> None:
        self.assertEqual(fix_return.hold_filter(self.ROOT, self.CHANGES, UNCONFIGURED), (self.CHANGES, ()))

    def test_withheld_lines_name_the_holder_and_the_check(self) -> None:
        result = fix_return.FixApplied(("src/held.rs",), (), (
            fix_return.Withheld("src/foreign.rs", "other-2 (theirs)"),
            fix_return.Withheld("src/free.rs", ""), fix_return.Withheld("src/also.rs", "")))
        self.assertEqual(fix_return.withheld_lines(result), [
            "not written: src/foreign.rs is held by reservation other-2 (theirs)",
            "not written: no reservation holds src/free.rs, src/also.rs; "
            + "run `cargo-berth check src/free.rs src/also.rs`, then run lint again",
        ])
        self.assertFalse(fix_return.fix_passed(result))
        self.assertEqual(fix_return.fix_word(result), "withheld")

    def test_foreign_only_withheld_passes(self) -> None:
        result = fix_return.FixApplied((), (), (fix_return.Withheld("src/foreign.rs", "other-2 (theirs)"),))
        self.assertTrue(fix_return.fix_passed(result))
        self.assertEqual(fix_return.fix_word(result), "applied")


class LocalCopyTests(unittest.TestCase):
    """The local fix pass edits a private copy, and only held paths come back."""

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.worktree: Path = Path()
        self.target: Path = Path()
        self.metadata: fix_return.CargoMetadata = fix_return.CargoMetadata(workspace_root="", target_directory="")
        self.board: berth_board.Board | berth_board.BoardUnavailable = UNCONFIGURED

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.worktree = self.root / "worktree"
        source = self.worktree / "ws/src"
        source.mkdir(parents=True)
        git(self.worktree, "init", "-q")
        _ = (self.worktree / ".gitignore").write_text("target/\nout.log\n")
        _ = (self.worktree / "ws/Cargo.toml").write_text("[workspace]\n")
        for name in ("held", "foreign", "free", "gone"):
            _ = (source / f"{name}.rs").write_text(f"fn {name}() {{}}\n")
        commit_all(self.worktree)
        self.target = self.worktree / "target"
        self.metadata = fix_return.CargoMetadata(
            workspace_root=str(self.worktree / "ws"), target_directory=str(self.target))
        self.board = berth_board.Board((
            reservation("own-1", self.worktree, "mine", "file:ws/src/held.rs", "file:ws/src/gone.rs",
                        "file:ws/src/new.rs"),
            reservation("other-2", self.root / "other", "theirs", "file:ws/src/foreign.rs"),
        ))

    def read(self, _: Path) -> berth_board.Board | berth_board.BoardUnavailable:
        return self.board

    def copy(self) -> fix_return.CopyMade:
        made = fix_return.make_copy(self.worktree, self.metadata, os.getpid(), self.read)
        assert isinstance(made, fix_return.CopyMade), made
        return made

    def text(self, relative: str) -> str:
        return (self.worktree / relative).read_text()

    def test_held_fixes_are_written_and_others_withheld(self) -> None:
        made = self.copy()
        self.assertEqual(made.folder, self.target / "package" / fix_return.COPY_NAME)
        self.assertEqual(made.manifest, made.tree / "ws/Cargo.toml")
        self.assertEqual(made.target, self.target)
        for name in ("held", "foreign", "free"):
            _ = (made.tree / f"ws/src/{name}.rs").write_text(f"pub(crate) fn {name}() {{}}\n")
        (made.tree / "ws/src/gone.rs").unlink()
        _ = (made.tree / "ws/src/new.rs").write_text("fn new() {}\n")
        _ = (made.tree / "out.log").write_text("log\n")
        result = fix_return.return_copy(self.worktree, made.folder, self.read)
        self.assertEqual(result, fix_return.FixApplied(("ws/src/held.rs", "ws/src/new.rs"), ("ws/src/gone.rs",), (
            fix_return.Withheld("ws/src/foreign.rs", "other-2 (theirs)"), fix_return.Withheld("ws/src/free.rs", ""))))
        self.assertFalse(fix_return.fix_passed(result))
        self.assertEqual(self.text("ws/src/held.rs"), "pub(crate) fn held() {}\n")
        self.assertEqual(self.text("ws/src/new.rs"), "fn new() {}\n")
        self.assertFalse((self.worktree / "ws/src/gone.rs").exists())
        self.assertEqual(self.text("ws/src/foreign.rs"), "fn foreign() {}\n")
        self.assertEqual(self.text("ws/src/free.rs"), "fn free() {}\n")
        self.assertFalse((self.worktree / "out.log").exists())
        self.assertFalse(made.folder.exists())

    def test_foreign_only_fix_passes_and_writes_nothing(self) -> None:
        made = self.copy()
        _ = (made.tree / "ws/src/foreign.rs").write_text("pub(crate) fn foreign() {}\n")
        result = fix_return.return_copy(self.worktree, made.folder, self.read)
        self.assertEqual(result, fix_return.FixApplied((), (), (
            fix_return.Withheld("ws/src/foreign.rs", "other-2 (theirs)"),)))
        self.assertTrue(fix_return.fix_passed(result))
        self.assertEqual(self.text("ws/src/foreign.rs"), "fn foreign() {}\n")

    def test_unchanged_copy_is_none(self) -> None:
        made = self.copy()
        self.assertEqual(fix_return.return_copy(self.worktree, made.folder, self.read), fix_return.FixNone())
        self.assertFalse(made.folder.exists())

    def test_changed_worktree_refuses_and_deletes_the_copy(self) -> None:
        made = self.copy()
        _ = (made.tree / "ws/src/held.rs").write_text("pub(crate) fn held() {}\n")
        _ = (self.worktree / "ws/src/held.rs").write_text("fn held() { edited }\n")
        result = fix_return.return_copy(self.worktree, made.folder, self.read)
        self.assertEqual(result, fix_return.FixRefused("the worktree changed during the lint fix pass"))
        self.assertEqual(self.text("ws/src/held.rs"), "fn held() { edited }\n")
        self.assertFalse(made.folder.exists())

    def test_unconfigured_repository_fixes_in_place(self) -> None:
        self.board = UNCONFIGURED
        made = fix_return.make_copy(self.worktree, self.metadata, os.getpid(), self.read)
        self.assertEqual(made, fix_return.InPlace())
        self.assertFalse(self.target.exists())

    def test_repository_with_no_berth_ledger_fixes_in_place(self) -> None:
        missing = berth_board.BoardUnavailable("the cargo-berth ledger is not initialized", ledger_missing=True)
        with (patch("scripts.mac_test.fix_return.shutil.which", return_value="/bin/cargo-berth"),
              patch.object(berth_board, "read_board", return_value=missing)):
            self.assertEqual(fix_return.read_board(self.worktree), UNCONFIGURED)
            made = fix_return.make_copy(self.worktree, self.metadata, os.getpid())
        self.assertEqual(made, fix_return.InPlace())
        self.assertFalse(self.target.exists())

    def test_unavailable_board_fails(self) -> None:
        self.board = berth_board.BoardUnavailable("ledger unreadable")
        made = fix_return.make_copy(self.worktree, self.metadata, os.getpid(), self.read)
        self.assertEqual(made, fix_return.FixFailed("cargo-berth board: ledger unreadable"))

    def test_workspace_outside_the_repository_fails(self) -> None:
        self.metadata = fix_return.CargoMetadata(workspace_root=str(self.root), target_directory=str(self.target))
        made = fix_return.make_copy(self.worktree, self.metadata, os.getpid(), self.read)
        self.assertIsInstance(made, fix_return.FixFailed)

    def stale_copy(self, owner: int) -> Path:
        folder = fix_return.copy_parent(self.target) / fix_return.COPY_NAME
        folder.mkdir(parents=True)
        _ = (folder / fix_return.OWNER_NAME).write_text(str(owner))
        _ = (folder / "stale").write_text("left\n")
        return folder

    def test_live_owner_keeps_its_copy(self) -> None:
        live = subprocess.Popen(["sleep", "60"])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        folder = self.stale_copy(live.pid)
        made = self.copy()
        self.assertEqual(made.folder, fix_return.copy_parent(self.target) / f"{fix_return.COPY_NAME}-{os.getpid()}")
        self.assertTrue((folder / "stale").exists())
        _ = fix_return.return_copy(self.worktree, made.folder, self.read)
        self.assertFalse(made.folder.exists())

    def test_stopped_owner_copy_is_replaced(self) -> None:
        stopped = subprocess.Popen(["true"])
        _ = stopped.wait()
        folder = self.stale_copy(stopped.pid)
        made = self.copy()
        self.assertEqual(made.folder, folder)
        self.assertFalse((folder / "stale").exists())

    def test_member_the_copy_excludes_never_finds_the_worktree_workspace(self) -> None:
        clay = self.worktree / "ws/vendor/clay"
        (clay / "src").mkdir(parents=True)
        _ = (self.worktree / "ws/Cargo.toml").write_text('[workspace]\nexclude = ["vendor/clay"]\n')
        _ = (clay / "Cargo.toml").write_text('[package]\nname = "clay"\n')
        commit_all(self.worktree)
        for target in (self.worktree / "ws/target", self.worktree / "ws/out"):
            with self.subTest(target=target.name):
                self.metadata = fix_return.CargoMetadata(
                    workspace_root=str(self.worktree / "ws"), target_directory=str(target))
                made = self.copy()
                self.addCleanup(shutil.rmtree, made.folder, True)
                outside = [manifest for manifest in cargo_workspace_search(made.tree / "ws/vendor/clay/Cargo.toml")
                           if not manifest.is_relative_to(made.tree)]
                self.assertEqual(outside, [])


class ApplyFixTests(unittest.TestCase):
    def test_path_outside_the_worktree_fails(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        staging = root / "staging"
        staging.mkdir()
        worktree = root / "worktree"
        worktree.mkdir()
        result = fix_return.apply_fix(worktree, staging, fix_return.MirrorChanges(("../escape.rs",), ()),
                                      lambda: True, unconfigured)
        self.assertIsInstance(result, fix_return.FixFailed)
        self.assertFalse((root / "escape.rs").exists())


if __name__ == "__main__":
    _ = unittest.main()

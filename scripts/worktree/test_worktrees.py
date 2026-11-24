"""Test worktree creation and removal through the public commands."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import errno
from functools import partial
import fcntl
import hashlib
from itertools import product
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
from typing import Callable, ClassVar, NamedTuple, TypedDict, cast, override
import unittest
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch
from uuid import uuid4


from scripts.lib.helper_tree_test_support import COPY_HELPERS, copy_helpers

from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.buildlog.store import host_name
from scripts.agents import remote
from scripts.build_queue import mac_run
from scripts.herdr import herdr, pane_launch
from scripts.lib import build_folders, build_slots, cargo_output, conf, git_exclude, git_files, scratch
from scripts.message import sessions
from scripts.lint import folder_audit, sweep, tmp_sweep
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.mac_test import mac_test
from scripts.production import staff
from scripts.worktree import worktree_links, worktree_records, worktrees
from scripts.herdr.missing_binary import MISSING_HERDR_BIN


CHECKOUT = Path(__file__).resolve().parents[2]
MAKE_WORKTREE = CHECKOUT / "scripts/worktree/make_worktree.sh"
WORKTREES = CHECKOUT / "scripts/worktree/worktrees.py"
CARGO_FOLDER = CHECKOUT / "scripts/build_hold/cargo_folder.py"
NO_PROBE_FDS: frozenset[int] = frozenset()
ALL_WORKTREES = worktrees.MigrationScope.ALL

BIND_MOVES = """
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import sys
from unittest import TestCase
from unittest.mock import patch
from scripts.lib import git_files
from scripts.lint import tmp_sweep
from scripts.worktree import worktree_records, worktrees

repo, mounted, workspace, build, proc = map(Path, sys.argv[1:])
tmp_sweep.PROC_DIR = str(proc)
check = TestCase()
for name in ("fresh", "resume"):
    old = mounted / name
    new = workspace / "origin/fixture-unit" / name
    worktree_records.git(repo, "worktree", "add", "-b", name, str(old))
    entry = git_files.at(old).git_dir
    target = old / "target"
    target.mkdir()
    (target / "artifact").write_text("warm")
    target_inode = target.stat().st_ino
    check.assertEqual(old.stat().st_dev, workspace.stat().st_dev)
    if name == "resume":
        with (patch.object(worktrees, "_worktree_copy_required", return_value=False),
              check.assertRaisesRegex(worktree_records.Refused, "Invalid cross-device link")):
            worktrees.move_worktree(old, "origin", "fixture-unit", name)
        planned = worktrees.read_move(worktrees.move_file(old))
        check.assertEqual(planned.stage, worktrees.MoveStage.PLANNED)
        check.assertEqual(planned.copy_tag, "")

    notices = StringIO()
    with redirect_stderr(notices):
        check.assertEqual(worktrees.move_worktree(old, worktrees.MoveRepository.FROM_ORIGIN, "fixture-unit", name), new)
    journal = worktrees.read_move(worktrees.move_file(old))
    check.assertTrue(journal.copy_tag)
    check.assertEqual(journal.stage, worktrees.MoveStage.FINISHED)
    check.assertEqual(old.readlink(), new)
    check.assertEqual(git_files.at(new).git_dir, entry)
    check.assertEqual(worktree_records.git(new, "branch", "--show-current").stdout.strip(), name)
    check.assertEqual((new / "code.txt").read_text(), "base\\n")
    output = build / "origin/fixture-unit" / name
    check.assertEqual((new / "target").readlink(), output)
    check.assertEqual(list(output.iterdir()), [])
    backup_target = journal.backup / "target"
    check.assertEqual(backup_target.stat().st_ino, target_inode)
    check.assertEqual((backup_target / "artifact").read_text(), "warm")
    check.assertIn(f"kept for a manual removal list: {journal.backup} (worktrees,", notices.getvalue())
    check.assertIn(f"kept for a manual removal list: {backup_target} (worktrees, the checkout's earlier Cargo target)",
                   notices.getvalue())
"""

MIGRATE_WORKTREES = """
import sys
root, proc = sys.argv[1:3]
del sys.argv[1:3]
sys.path.insert(0, root)
from scripts.lint import tmp_sweep
from scripts.worktree import worktrees
tmp_sweep.PROC_DIR = proc
raise SystemExit(worktrees.main())
"""

BIND_WORKTREE_USE = """
import subprocess
import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from scripts.lint import tmp_sweep
from scripts.worktree import worktree_records, worktrees

old, proc, cwd, expected = map(Path, sys.argv[1:5])
child_code, *child_args = sys.argv[5:]
tmp_sweep.PROC_DIR = str(proc)
check = TestCase()
with subprocess.Popen([sys.executable, "-c", child_code, *child_args], cwd=cwd,
                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as child:
    link = child.stdout.readline().strip()
    check.assertTrue(link)
    process = Path("/proc") / str(child.pid)
    (proc / str(child.pid)).symlink_to(process, target_is_directory=True)
    check.assertEqual((process / link).readlink(), expected)
    with (patch.object(worktrees, "_worktree_copy_required", return_value=True),
          check.assertRaisesRegex(worktree_records.Refused, f"pids {child.pid}")):
        worktrees.move_worktree(old, "origin", "fixture-unit", "demo")
check.assertTrue(old.is_dir() and not old.is_symlink())
"""

REQUEST_COPY_WITH_UNIT_LOCK = """
from contextlib import nullcontext
from pathlib import Path
import sys
from scripts.lib import build_slots
from scripts.worktree import worktrees

source = Path(sys.argv[1])
rename = worktrees.UnitFolderRename("origin", "demo", "renamed", source, source.with_name("renamed"))
with rename.hold_copy_allocations() if "--same-process" in sys.argv else nullcontext():
    try:
        with build_slots.take_copy(source, "HEAD", showrunner="demo", unit="demo",
                                   work="rename", purpose="fixture"):
            raise AssertionError("the copy bypassed the unit lock")
    except BlockingIOError as error:
        print(error)
"""

MAKE_LEGACY_FIXTURE = """
from pathlib import Path
import sys
from scripts.lib.build_folders import NewWorktreeBranch
from scripts.worktree import worktree_records, worktrees

source = Path.cwd()
owner, branch = worktrees.caller_branch(source, source, worktrees.staff_record())
worktrees.create_checkout(Path(sys.argv[1]), source, NewWorktreeBranch(sys.argv[2], "HEAD"),
                          worktree_records.UnassignedOutput(), owner=owner, merge_branch=branch)
"""

END_LANDED = """
import json
from pathlib import Path
import sys
from scripts.worktree.worktrees import Ended, end_landed

results = end_landed(Path(sys.argv[1]), sys.argv[2])
print("RESULT=" + json.dumps([
    {"kind": type(result).__name__, "path": str(result.path),
     "detail": result.branch if isinstance(result, Ended) else result.reason}
    for result in results
]))
"""


class MadeWorktree(TypedDict):
    version: int
    path: str
    branch: str
    repo: str
    base: str
    merge_branch: str
    owner: str
    made_at: str


class RepositoryState(NamedTuple):
    worktrees: str
    branches: str
    origin_branches: str
    records: dict[str, bytes]
    folders: tuple[str, ...]


class EndResult(TypedDict):
    kind: str
    path: str
    detail: str


class WorktreeLifecycleTests(unittest.TestCase):
    helper_tree: ClassVar[Path]
    root: Path = Path()
    home: Path = Path()
    origin: Path = Path()
    repository: Path = Path()
    merge_tree: Path = Path()
    trees: Path = Path()
    state: Path = Path()
    proc: Path = Path()
    environment: dict[str, str] = {}
    base: str = ""

    @classmethod
    @override
    def setUpClass(cls) -> None:
        home = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="worktree-helpers-"))).resolve()
        _ = copy_helpers(home, *COPY_HELPERS, "scripts/worktree", "config")
        cls.helper_tree = home / ".claude"

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="worktree-lifecycle-"))).resolve()
        self.proc = self.root / "proc"
        (self.proc / "self").mkdir(parents=True)
        _ = (self.proc / "self" / "status").write_text("CapPrm:\t0000000000000000\n")
        _ = self.enterContext(patch.object(tmp_sweep, "PROC_DIR", str(self.proc)))
        self.home = self.root / "home"
        self.home.mkdir()
        _ = shutil.copytree(self.helper_tree, self.home / ".claude", symlinks=True)
        self.origin = self.root / "origin.git"
        self.repository = self.root / "clone"
        self.merge_tree = self.root / "integration"
        self.trees = self.root / "trees"
        self.trees.mkdir()
        self.state = self.root / "worktrees-state"
        config = self.root / "staff.conf"
        _ = config.write_text("# range: 0.001..\nOPERATION_LOCK_SECONDS=5\n"
                              + "# range: 0.001..\nREMOTE_SECONDS=5\n")
        staff_file = self.root / "staff.json"
        self.environment = {
            **config_environment(self.root / "settings"),
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"), "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "WORKTREES_STATE_DIR": str(self.state),
            "STAFF_CONFIG": str(config), "STAFF_FILE": str(staff_file),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            **interpreter_cache_environment(self.root / "cache"), "PYTHONPATH": str(CHECKOUT / "scripts/lib/python_startup"),
            "HERDR_BIN": MISSING_HERDR_BIN,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
            "GIT_AUTHOR_NAME": "Worktree fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Worktree fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        _ = self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.origin))
        _ = self.git(self.root, "clone", str(self.origin), str(self.repository))
        _ = (self.repository / "code.txt").write_text("base\n")
        document = self.repository / "docs/production.md"
        document.parent.mkdir()
        _ = document.write_text("# Fixture production\n")
        _ = (self.repository / ".unit.toml").write_text('plan = "standby"\nresponsibilities = []\n')
        _ = self.git(self.repository, "add", ".")
        _ = self.git(self.repository, "commit", "-m", "Create the fixture")
        self.base = self.git(self.repository, "rev-parse", "HEAD")
        _ = self.git(self.repository, "push", "-u", "origin", "main")
        _ = self.git(self.repository, "remote", "set-head", "origin", "main")
        _ = self.git(self.repository, "worktree", "add", str(self.merge_tree), "-b", "integration")
        _ = self.git(self.merge_tree, "push", "-u", "origin", "integration")
        _ = self.git(self.repository, "checkout", "-b", "unit-home")
        machine = socket.gethostname().split(".", 1)[0]
        runner_id, unit_id = str(uuid4()), str(uuid4())
        rows: list[dict[str, object]] = [
            {"id": runner_id, "current_session": runner_id, "kind": "showrunner",
             "name": "fixture-production", "parent": None,
             "doc": {"machine": machine, "path": str(self.merge_tree / "docs/production.md"), "state": "live"}},
            {"id": unit_id, "current_session": unit_id, "kind": "unit",
             "name": "fixture-unit", "parent": runner_id,
             "worktree": {"machine": machine, "path": str(self.repository), "state": "present"}},
        ]
        _ = staff_file.write_text(json.dumps({"schema": 2, "rows": rows}))

    def command(self, folder: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(arguments, cwd=folder, env=self.environment,
                              capture_output=True, text=True, check=False, timeout=20)

    def git(self, folder: Path, *arguments: str) -> str:
        result = self.command(folder, ["git", *arguments])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def migration_command(self, *arguments: Path | str) -> list[str]:
        return [sys.executable, "-B", "-c", MIGRATE_WORKTREES, str(CHECKOUT), str(self.proc),
                "migrate", *map(str, arguments)]

    def register_process(self, pid: int) -> None:
        (self.proc / str(pid)).symlink_to(Path("/proc") / str(pid), target_is_directory=True)

    def finish_landed(self, *, reviewed: tuple[Path, ...] = ()) -> list[EndResult]:
        python = self.home / ".claude/scripts/lib/py"
        result = self.command(self.repository, [str(python), "-c", END_LANDED,
                                               str(self.repository), "integration"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path in reviewed:
            self.assertIn(f"kept for a manual removal list: {path}", result.stderr)
        payload = next(line.removeprefix("RESULT=") for line in result.stdout.splitlines()
                       if line.startswith("RESULT="))
        return cast(list[EndResult], json.loads(payload))

    def cleanup_result(self, tree: Path) -> subprocess.CompletedProcess[str]:
        python = self.home / ".claude/scripts/lib/py"
        return self.command(self.repository, [str(python), str(WORKTREES), "cleanup", str(tree)])

    def make(self, tree: Path, branch: str) -> None:
        result = self.legacy_fixture(tree, branch)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def legacy_fixture(self, tree: Path, branch: str) -> subprocess.CompletedProcess[str]:
        return self.command(self.repository, [sys.executable, "-c", MAKE_LEGACY_FIXTURE, str(tree), branch])

    def layout_fixture(self, mode: str = "enforce") -> None:
        config = Path(self.environment["CONF_DIR"])
        build = self.root / "build"
        scratch_root = self.root / "scratch"
        session_root = self.root / "session-root"
        _ = write_config_fixture(config / "build_folders.conf",
            f"roots.workspace={self.trees}\nroots.build={build}\nroots.build.{host_name()}={build}\n"
            + f"roots.scratch={scratch_root}\nroots.scratch.{host_name()}={scratch_root}\n"
            + f"roots.session={session_root}\nroots.session.{host_name()}={session_root}\n"
            + f"folders.check={mode}\n# range: 0..1\nlocks.probe_wait_seconds=0.01\n"
            + "# range: 0.001..1\nlocks.probe_retry_seconds=0.005\n")
        copies = config / "copies.conf"
        _ = copies.write_text("# range: 0.001..\ngit_timeout_seconds=5\n")
        self.environment["COPIES_CONFIG"] = str(copies)

    def make_layout(self, unit: str, name: str) -> Path:
        source = self.home / "rust/origin"
        source.parent.mkdir(exist_ok=True)
        if not source.exists():
            _ = self.git(self.root, "clone", str(self.repository), str(source))
            _ = self.git(source, "remote", "set-url", "origin", str(self.origin))
        made = self.command(self.repository, ["bash", str(MAKE_WORKTREE), "origin", unit, name])
        self.assertEqual(made.returncode, 0, made.stderr)
        return Path(made.stdout.strip())

    def test_move_command_reads_origin_and_resumes_with_the_warm_target(self) -> None:
        self.layout_fixture()
        old = self.trees / "legacy"
        self.make(old, "legacy")
        target = old / "target"
        target.mkdir()
        _ = (target / "artifact").write_text("warm")
        inode = target.stat().st_ino
        checkout = git_files.at(old)
        new = self.trees / "origin/fixture-unit/cli"
        output = self.root / "build/origin/fixture-unit/cli"
        output.parent.mkdir(parents=True)
        command = [sys.executable, str(WORKTREES), "move", str(old), "fixture-unit", "cli"]
        with output.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            blocked = self.command(self.root, command)
        self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
        self.assertIn("the build folder lock is held", blocked.stderr)
        self.assertFalse(output.exists())
        self.assertEqual((new / "target").stat().st_ino, inode)
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.LINKED)
        for _attempt in range(2):
            moved = self.command(self.root, command)
            self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
            self.assertEqual(moved.stdout, f"{new}\n")
        self.assertEqual(old.readlink(), new)
        self.assertEqual(git_files.at(new).git_dir, checkout.git_dir)
        self.assertEqual((new / "target").readlink(), output)
        self.assertEqual(output.stat().st_ino, inode)
        self.assertEqual((output / "artifact").read_text(), "warm")
        self.assertEqual(list(new.parent.glob(".saved-target-*")), [])

    def test_move_across_a_bind_mount_starts_or_resumes_a_copy(self) -> None:
        unshare, mount, shell = shutil.which("unshare"), shutil.which("mount"), shutil.which("sh")
        if sys.platform != "linux" or not unshare or not mount or not shell:
            self.skipTest("Linux bind mounts require unshare and mount")
        self.layout_fixture()
        source, mounted = self.root / "source", self.root / "mount"
        source.mkdir()
        mounted.mkdir()
        python = CHECKOUT / "scripts/lib/py"
        result = self.command(self.root, [
            unshare, "--map-root-user", "--mount", "--propagation", "private",
            shell, "-c", '"$1" --bind "$2" "$3" || exit 77; shift 3; exec "$@"', "bind-fixture",
            mount, str(source), str(mounted), str(python), "-c", BIND_MOVES,
            str(self.repository), str(mounted), str(self.trees), str(self.root / "build"), str(self.proc),
        ])
        if result.returncode == 77 or "unshare failed: Operation not permitted" in result.stderr:
            self.skipTest("the kernel does not permit a private bind mount")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_move_refuses_a_numbered_build_copy_before_reading_its_journal(self) -> None:
        self.layout_fixture()
        old = self.add_linked_tree(self.repository, self.root / "build/hana/startup/1", "numbered-copy")
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(ValueError, "cannot move numbered build copy"):
                _ = worktrees.move_worktree(old, "origin", "fixture-unit", "demo")
            self.assertFalse(worktrees.move_folder().exists())
            journal = worktrees.move_file(old)
            journal.parent.mkdir(parents=True)
            _ = journal.write_text("unreadable move record")
            with self.assertRaisesRegex(ValueError, "cannot move numbered build copy"):
                _ = worktrees.move_worktree(old, "origin", "fixture-unit", "demo")
        self.assertFalse(old.is_symlink())
        self.assertEqual(journal.read_text(), "unreadable move record")

    def test_move_writes_the_checkout_record_before_deferring_the_output(self) -> None:
        self.layout_fixture()
        config = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        with config.open("a") as handle:
            _ = handle.write("repo_word.origin=claude\n")
        legacy = self.root / "build/claude/scratch"
        legacy.mkdir(parents=True)
        old = self.trees / "legacy"
        self.make(old, "legacy")
        new = self.trees / "claude/fixture-unit/demo"
        with patch.dict(os.environ, self.environment, clear=True), redirect_stdout(StringIO()):
            prior = worktree_records.read_record(worktree_records.record_file(old))
            self.assertEqual(worktrees.move_worktree(old, "claude", "fixture-unit", "demo"), new)
            old.unlink()
            self.assertFalse(worktree_records.record_file(old).exists())
            record = worktree_records.read_record(worktree_records.record_file(new))
            self.assertEqual(record, replace(prior, path=str(new), layout=worktree_records.UnassignedOutput()))
            self.assertFalse(os.path.lexists(new / "target"))
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.LINKED)
            legacy.rmdir()
            self.assertEqual(worktrees.move_worktree(old, "claude", "fixture-unit", "demo"), new)
            layout = worktrees.output_record("claude", "fixture-unit", "demo")
            self.assertEqual(worktrees.worktree_output(new), layout)
            self.assertEqual((new / "target").readlink(), layout.output)

    def test_record_branch_repairs_an_empty_branch(self) -> None:
        tree = self.trees / "legacy"
        self.make(tree, "legacy")
        _ = self.git(tree, "tag", "legacy")
        with patch.dict(os.environ, self.environment, clear=True):
            prior = worktree_records.read_record(worktree_records.record_file(tree))
            worktrees.write_record(replace(prior, branch=""))
        result = self.command(self.root, [sys.executable, str(WORKTREES), "record-branch", str(tree)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "branch '' -> 'legacy'\n")
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(worktree_records.checkout_record(tree), replace(prior, branch="legacy"))

    def test_record_branch_refuses_an_unrecorded_or_detached_checkout(self) -> None:
        unrecorded = self.command(self.root, [sys.executable, str(WORKTREES), "record-branch", str(self.repository)])
        self.assertEqual(unrecorded.returncode, 1, unrecorded.stdout + unrecorded.stderr)
        self.assertIn("no worktree record for this checkout", unrecorded.stderr)
        tree = self.trees / "legacy"
        self.make(tree, "legacy")
        _ = self.git(tree, "switch", "--detach")
        with patch.dict(os.environ, self.environment, clear=True):
            record = worktree_records.record_file(tree)
            before = record.read_bytes()
        result = self.command(self.root, [sys.executable, str(WORKTREES), "record-branch", str(tree)])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("the checkout has no branch (detached HEAD)", result.stderr)
        self.assertEqual(record.read_bytes(), before)

    def test_record_branch_uses_the_checkout_git_directory(self) -> None:
        tree = self.trees / "legacy"
        self.make(tree, "legacy")
        foreign = self.root / "foreign"
        _ = self.git(self.root, "clone", str(self.origin), str(foreign))
        command = [sys.executable, str(WORKTREES), "record-branch", str(tree)]
        with patch.dict(os.environ, self.environment, clear=True):
            prior = worktree_records.read_record(worktree_records.record_file(tree))
            worktrees.write_record(replace(prior, branch=""))
            with worktrees.move_lock(), self.assertRaises(worktrees.WorktreeMoveLockHeld):
                worktrees.sync_worktree_branch(tree, "legacy")
            with self.assertRaisesRegex(ValueError, "expected branch 'main', found 'legacy'"):
                worktrees.sync_worktree_branch(tree, "main")
            with patch.dict(self.environment, {"GIT_DIR": str(foreign / ".git")}):
                result = self.command(self.root, command)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "branch '' -> 'legacy'\n")
            self.assertEqual(worktree_records.checkout_record(tree), prior)

            mismatch = replace(prior, branch="", repo=str(foreign))
            worktrees.write_record(mismatch)
            result = self.command(self.root, command)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("checkout does not belong to the recorded repository", result.stderr)
            self.assertEqual(worktree_records.checkout_record(tree), mismatch)

    def test_move_publishes_the_checkout_record_before_the_linked_stage(self) -> None:
        self.layout_fixture()
        old = self.trees / "legacy"
        self.make(old, "legacy")
        tip = self.commit(old, "code.txt", "landed\n")
        _ = self.git(self.repository, "push", "origin", "legacy:main")
        new = self.trees / "origin/fixture-unit/demo"
        write_move = worktrees.write_move

        def stop_after_linked(journal: worktrees.MoveJournal) -> None:
            write_move(journal)
            if journal.stage is worktrees.MoveStage.LINKED:
                raise InterruptedError("stop after LINKED")

        with patch.dict(os.environ, self.environment, clear=True):
            prior = worktree_records.read_record(worktree_records.record_file(old))
            prior = replace(prior, merge_branch="main")
            worktrees.write_record(prior)
            with (patch.object(worktrees, "write_move", stop_after_linked),
                  self.assertRaisesRegex(InterruptedError, "stop after LINKED")):
                _ = worktrees.move_worktree(old, "origin", "fixture-unit", "demo")
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.LINKED)
            old.unlink()
            self.assertFalse(worktree_records.record_file(old).exists())
            self.assertEqual(worktree_records.checkout_record(new), replace(prior, path=str(new)))
            result, = worktrees.end_landed(self.repository, "main")
            self.assertIsInstance(result, worktrees.Kept)
            self.assertEqual(result.path, new)
            self.assertEqual(self.git(new, "rev-parse", "refs/heads/legacy"), tip)
            self.assertEqual(worktrees.move_worktree(old, "origin", "fixture-unit", "demo"), new)

    def test_migration_recovers_the_branch_of_a_managed_move(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        new = self.trees / "origin/fixture-unit/source"
        prior = worktrees.new_record(old, "", main, self.git(old, "rev-parse", "HEAD"), "", "")
        command = self.migration_command("--all")
        with patch.dict(os.environ, self.environment, clear=True):
            worktrees.write_record(prior)
            with (patch.object(worktrees, "_publish_move_source", side_effect=InterruptedError("before relocation")),
                  self.assertRaisesRegex(InterruptedError, "before relocation")):
                _ = worktrees.move_worktree(old, "origin", "fixture-unit", "source")
            journal_path = worktrees.move_file(old)
            planned = worktrees.read_move(journal_path)
            self.assertTrue(planned.require_branch)
            kept = self.command(self.root, command)
            self.assertEqual(kept.returncode, 0, kept.stdout + kept.stderr)
            self.assertIn(f"keep {old}: resume the pending managed move", kept.stdout)
            self.assertEqual(worktrees.read_move(journal_path), planned)
            self.assertFalse(new.exists())

            with (patch.object(worktrees, "_relocate_worktree_records", side_effect=InterruptedError("before record")),
                  self.assertRaisesRegex(InterruptedError, "before record")):
                _ = worktrees.move_worktree(old, "origin", "fixture-unit", "source")
            planned = worktrees.read_move(journal_path)
            self.assertEqual(planned.stage, worktrees.MoveStage.PLANNED)
            self.assertIsNone(worktree_records.checkout_record(new))
        self.assertEqual(old.readlink(), new)

        _ = self.git(new, "switch", "--detach")
        refused = self.command(self.root, command)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertIn("the checkout has no branch (detached HEAD)", refused.stderr)
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(worktrees.read_move(journal_path), planned)
            self.assertEqual(worktree_records.checkout_record(old), prior)
            self.assertIsNone(worktree_records.checkout_record(new))

        _ = self.git(new, "switch", "source")
        result = self.command(self.root, command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"resume {old} -> {new}", result.stdout)
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(worktrees.read_move(journal_path).stage, worktrees.MoveStage.FINISHED)
            self.assertIsNone(worktree_records.checkout_record(old))
            self.assertEqual(worktree_records.checkout_record(new), replace(
                prior, path=str(new), branch="source", layout=worktrees.output_record("origin", "fixture-unit", "source")))

    def test_migration_continues_after_a_detached_managed_move(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        independent = self.add_linked_tree(main, self.trees / "independent", "independent")
        new = self.trees / "origin/fixture-unit/source"
        independent_destination = new.with_name("independent")
        names = self.root / "list"
        _ = names.write_text(f"{independent} fixture-unit independent\n")
        with patch.dict(os.environ, self.environment, clear=True):
            with (patch.object(worktrees, "_relocate_worktree_records", side_effect=InterruptedError("before record")),
                  self.assertRaisesRegex(InterruptedError, "before record")):
                _ = worktrees.move_worktree(old, "origin", "fixture-unit", "source")
            journal = worktrees.move_file(old)
            before = journal.read_bytes()
        _ = self.git(new, "switch", "--detach")

        result = self.command(self.root, self.migration_command("--all", "--list", names))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"{new}: the checkout has no branch (detached HEAD)", result.stderr)
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(independent.readlink(), independent_destination)
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertIsNone(worktree_records.checkout_record(new))
            self.assertEqual(worktree_records.read_record(worktree_records.record_file(independent_destination)).branch, "independent")

    def test_cleanup_keeps_a_branch_registered_at_a_different_worktree_path(self) -> None:
        old, branch = self.trees / "legacy", "legacy"
        self.make(old, branch)
        tip = self.commit(old, "code.txt", "landed\n")
        self.publish(old, branch)
        _ = self.git(self.repository, "push", "origin", f"{branch}:main")
        new = self.trees / "new"
        _ = self.git(self.repository, "worktree", "move", str(old), str(new))
        self.assertFalse(os.path.lexists(old))

        with patch.dict(os.environ, self.environment, clear=True):
            record = worktree_records.read_record(worktree_records.record_file(old))
            worktrees.write_record(replace(record, merge_branch="main"))
            result, = worktrees.end_landed(self.repository, "main")
            assert isinstance(result, worktrees.Kept), result
            self.assertIn(str(new), result.reason)
            self.assertTrue(worktree_records.record_file(old).is_file())
        self.assertEqual(self.git(new, "rev-parse", f"refs/heads/{branch}"), tip)
        self.assertEqual(self.git(self.repository, "ls-remote", "--heads", "origin", branch),
                         f"{tip}\trefs/heads/{branch}")

    def test_move_retries_an_unverified_copy_stage_without_removing_it(self) -> None:
        self.layout_fixture()
        old = self.trees / "legacy"
        self.make(old, "legacy")
        _ = self.git(old, "switch", "--detach")
        new = self.trees / "origin/fixture-unit/demo"
        notices = StringIO()
        with (patch.dict(os.environ, self.environment, clear=True), redirect_stderr(notices),
              patch.object(worktrees, "_worktree_copy_required", return_value=True)):
            move = worktrees.WorktreeMove(old, new, "origin", "fixture-unit", "demo", worktrees.MoveOutput.LIVE_UNIT)
            partial = worktrees._open_move(move)  # pyright: ignore[reportPrivateUsage]
            partial.staging.mkdir(parents=True)
            _ = (partial.staging / "code.txt").write_text("partial copy\n")
            self.assertEqual(worktrees.move_worktree(old, "origin", "fixture-unit", "demo"), new)
            completed = worktrees.read_move(worktrees.move_file(old))
        self.assertNotEqual(completed.copy_tag, partial.copy_tag)
        self.assertEqual(completed.stage, worktrees.MoveStage.FINISHED)
        self.assertFalse(completed.require_branch)
        self.assertEqual(git_files.current_branch(new), "")
        self.assertTrue(partial.source.matches(completed.backup))
        self.assertEqual((new / "code.txt").read_text(), "base\n")
        self.assertEqual((partial.staging / "code.txt").read_text(), "partial copy\n")
        self.assertIn(f"{partial.staging} (worktrees, a prior move left an unverified stage)", notices.getvalue())

    def test_copy_move_repairs_a_relative_git_pointer(self) -> None:
        self.layout_fixture()
        old = self.trees / "legacy"
        _ = self.git(self.repository, "worktree", "add", "--relative-paths", "-b", "legacy", str(old))
        original = git_files.at(old)
        pointer = Path((old / ".git").read_text().strip().removeprefix("gitdir: "))
        self.assertFalse(pointer.is_absolute())
        new = self.trees / "origin/fixture-unit/demo"
        self.assertFalse((new / pointer).resolve().exists())
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees, "_worktree_copy_required", return_value=True)):
            self.assertEqual(worktrees.move_worktree(old, "origin", "fixture-unit", "demo"), new)
            completed = worktrees.read_move(worktrees.move_file(old))
        self.assertEqual(git_files.at(new).git_dir, original.git_dir)
        self.assertEqual(self.git(new, "branch", "--show-current"), "legacy")
        self.assertIn(f"worktree {new}", self.git(self.repository, "worktree", "list", "--porcelain"))
        self.assertEqual((completed.backup / ".git").read_text(), f"gitdir: {pointer}\n")

    def test_copy_move_keeps_tracked_target_files_and_leaves_artifacts_in_backup(self) -> None:
        self.layout_fixture()
        old = self.trees / "legacy"
        self.make(old, "legacy")
        target = old / "target"
        (target / "nested").mkdir(parents=True)
        _ = (target / "nested/.gitkeep").write_text("tracked\n")
        literal = "target/nested/[keep]\\hint"
        _ = (old / literal).write_text("literal file name\n")
        _ = self.git(old, "add", "-f", "target/nested/.gitkeep", f":(literal){literal}")
        _ = self.git(old, "commit", "-m", "Track a placeholder")
        _ = (target / "nested/.gitkeep").write_text("local edit\n")
        _ = (target / "nested/artifact").write_text("warm\n")
        _ = (target / "untracked").symlink_to("nested")
        new = self.trees / "origin/fixture-unit/demo"
        notices = StringIO()
        with (patch.dict(os.environ, self.environment, clear=True), redirect_stderr(notices),
              patch.object(worktrees, "_worktree_copy_required", return_value=True)):
            self.assertEqual(worktrees.move_worktree(old, "origin", "fixture-unit", "demo"), new)
            completed = worktrees.read_move(worktrees.move_file(old))
        self.assertEqual(completed.stage, worktrees.MoveStage.FINISHED)
        self.assertFalse((new / "target").is_symlink())
        self.assertEqual((new / "target/nested/.gitkeep").read_text(), "local edit\n")
        self.assertEqual((new / literal).read_text(), "literal file name\n")
        self.assertFalse((new / "target/nested/artifact").exists())
        self.assertFalse(os.path.lexists(new / "target/untracked"))
        self.assertEqual((completed.backup / "target/nested/artifact").read_text(), "warm\n")
        self.assertIn(f"{completed.backup / 'target'} (worktrees, the checkout's earlier Cargo target)",
                      notices.getvalue())
        self.assertIn(f"{new / 'target'} (worktrees, Git tracks the checkout's target)", notices.getvalue())
        with patch.dict(os.environ, self.environment, clear=True):
            record = worktree_records.read_record(worktree_records.record_file(new))
            self.assertIsInstance(record.layout, worktree_records.UnassignedOutput)

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_direct_copy_move_refuses_a_source_in_use_before_copying(self) -> None:
        self.layout_fixture()
        old = self.trees / "legacy"
        self.make(old, "legacy")
        nested = old / "nested"
        nested.mkdir()
        new = self.trees / "origin/fixture-unit/demo"
        copy = worktrees._copy_worktree_source  # pyright: ignore[reportPrivateUsage]
        with patch.dict(os.environ, self.environment, clear=True):
            with patch.object(worktrees, "_worktree_copy_required", return_value=False):
                move = worktrees.WorktreeMove(old, new, "origin", "fixture-unit", "demo", worktrees.MoveOutput.LIVE_UNIT)
                _ = worktrees._open_move(move)  # pyright: ignore[reportPrivateUsage]
            with (patch.object(worktrees, "_worktree_copy_required", return_value=True),
                  patch.object(worktrees, "_copy_worktree_source", wraps=copy) as copy_source):
                for lock in (old.with_name(old.name + ".lock"), scratch.busy_path(old)):
                    with self.subTest(lock=lock), lock.open("wb") as handle:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        with self.assertRaisesRegex(worktree_records.Refused, f"{re.escape(str(lock))} held"):
                            _ = worktrees.move_worktree(old, "origin", "fixture-unit", "demo")

                with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                      cwd=nested, stdin=subprocess.PIPE, env=self.environment) as child:
                    self.register_process(child.pid)
                    with self.assertRaisesRegex(worktree_records.Refused, f"pids {child.pid}"):
                        _ = worktrees.move_worktree(old, "origin", "fixture-unit", "demo")
                copy_source.assert_not_called()
                self.assertTrue(old.is_dir() and not old.is_symlink())
                self.assertFalse(os.path.lexists(new))
                self.assertEqual(worktrees.move_worktree(old, "origin", "fixture-unit", "demo"), new)

    def test_direct_copy_move_finds_a_working_directory_through_a_bind_mount(self) -> None:
        self._check_process_use_through_bind_mount(holds_file=False)

    def test_direct_copy_move_finds_an_open_file_through_a_bind_mount(self) -> None:
        self._check_process_use_through_bind_mount(holds_file=True)

    def _check_process_use_through_bind_mount(self, *, holds_file: bool) -> None:
        unshare, mount, shell = shutil.which("unshare"), shutil.which("mount"), shutil.which("sh")
        if sys.platform != "linux" or not unshare or not mount or not shell:
            self.skipTest("Linux bind mounts require unshare and mount")
        self.layout_fixture()
        old = self.trees / "legacy"
        self.make(old, "legacy")
        (old / "nested").mkdir()
        _ = (old / "nested/work").write_text("work")
        alias = self.root / "bind alias"
        alias.mkdir()
        nested = alias / "legacy/nested"
        if holds_file:
            expected = nested / "work"
            child_code = "import os, sys; fd = os.open(sys.argv[1], os.O_RDWR); print(f'fd/{fd}', flush=True); sys.stdin.read()"
            cwd, child_args = self.root, [str(expected)]
        else:
            expected = nested
            child_code = "import sys; print('cwd', flush=True); sys.stdin.read()"
            cwd, child_args = nested, []
        python = CHECKOUT / "scripts/lib/py"
        result = self.command(self.root, [
            unshare, "--map-root-user", "--mount", "--propagation", "private",
            shell, "-c", '"$1" --bind "$2" "$3" || exit 77; shift 3; exec "$@"', "bind-fixture",
            mount, str(self.trees), str(alias), str(python), "-c", BIND_WORKTREE_USE,
            str(old), str(self.proc), str(cwd), str(expected), child_code, *child_args,
        ])
        if result.returncode == 77 or "unshare failed: Operation not permitted" in result.stderr:
            self.skipTest("the kernel does not permit a private bind mount")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_copy_move_preserves_a_destination_created_before_publication(self) -> None:
        self.layout_fixture()
        old = self.trees / "legacy"
        self.make(old, "legacy")
        new = self.trees / "origin/fixture-unit/demo"
        rename = Path.rename
        occupants: list[worktrees.FolderIdentity] = []

        def create_competing_directory(path: Path, target: Path) -> Path:
            result = rename(path, target)
            if path == old:
                new.mkdir()
                occupants.append(worktrees.FolderIdentity.read(new))
            return result

        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees, "_worktree_copy_required", return_value=True)):
            with (patch.object(Path, "rename", create_competing_directory),
                  self.assertRaisesRegex(FileExistsError, f"{re.escape(str(new))}: worktree destination already exists")):
                _ = worktrees.move_worktree(old, "origin", "fixture-unit", "demo")
            occupant, = occupants
            self.assertTrue(occupant.matches(new))
            self.assertEqual(list(new.iterdir()), [])
            journal = worktrees.read_move(worktrees.move_file(old))
            self.assertEqual(journal.stage, worktrees.MoveStage.PLANNED)
            self.assertTrue(journal.source.matches(journal.backup))
            self.assertTrue(journal.destination.matches(journal.staging))
            new.rmdir()
            self.assertEqual(worktrees.move_worktree(old, "origin", "fixture-unit", "demo"), new)
            self.assertTrue(journal.destination.matches(new))
            self.assertEqual(old.readlink(), new)

    def test_move_reuses_an_empty_output_and_reports_a_target_at_an_occupied_output(self) -> None:
        self.layout_fixture()
        for name, occupied in (("empty", False), ("occupied", True)):
            with self.subTest(name=name):
                old = self.trees / name
                self.make(old, name)
                target = old / "target"
                target.mkdir()
                _ = (target / "artifact").write_text("warm")
                inode = target.stat().st_ino
                output = self.root / "build/origin/fixture-unit" / name
                output.mkdir(parents=True)
                if occupied:
                    _ = (output / "other").write_text("keep")
                notices = StringIO()
                with patch.dict(os.environ, self.environment, clear=True), redirect_stderr(notices):
                    moved = worktrees.move_worktree(old, "origin", "fixture-unit", name)
                self.assertEqual((moved / "target").readlink(), output)
                if occupied:
                    saved, = moved.parent.glob(".saved-target-*")
                    self.assertEqual((saved / "target").stat().st_ino, inode)
                    self.assertEqual((saved / "target/artifact").read_text(), "warm")
                    self.assertEqual((output / "other").read_text(), "keep")
                    self.assertFalse((output / "artifact").exists())
                    self.assertIn(f"kept for a manual removal list: {saved} (worktrees, the checkout's earlier Cargo target)",
                                  notices.getvalue())
                else:
                    self.assertEqual(output.stat().st_ino, inode)
                    self.assertEqual((output / "artifact").read_text(), "warm")
                    self.assertEqual(list(moved.parent.glob(".saved-target-*")), [])

    def test_move_saves_the_target_when_its_output_rename_crosses_devices(self) -> None:
        self.layout_fixture()
        old = self.trees / "legacy"
        self.make(old, "legacy")
        target = old / "target"
        target.mkdir()
        _ = (target / "artifact").write_text("warm")
        inode = target.stat().st_ino
        output = self.root / "build/origin/fixture-unit/demo"
        notices = StringIO()
        rename = os.rename

        def rename_with_cross_device_target(source: Path, destination: str | Path, *,
                                           src_dir_fd: int | None = None, dst_dir_fd: int | None = None) -> None:
            if dst_dir_fd is not None and source.name == "target":
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            rename(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with (patch.dict(os.environ, self.environment, clear=True), redirect_stderr(notices),
              patch.object(os, "rename", side_effect=rename_with_cross_device_target)):
            new = worktrees.move_worktree(old, "origin", "fixture-unit", "demo")
        saved, = new.parent.glob(".saved-target-*")
        self.assertEqual((saved / "target").stat().st_ino, inode)
        self.assertEqual((saved / "target/artifact").read_text(), "warm")
        self.assertEqual((new / "target").readlink(), output)
        self.assertEqual(list(output.iterdir()), [])
        self.assertIn(f"kept for a manual removal list: {saved} (worktrees, the checkout's earlier Cargo target)",
                      notices.getvalue())

    def test_move_preserves_the_old_target_when_the_output_is_a_symlink(self) -> None:
        self.layout_fixture()
        old = self.trees / "legacy"
        self.make(old, "legacy")
        target = old / "target"
        target.mkdir()
        _ = (target / "artifact").write_text("warm")
        endpoint = self.root / "other-output"
        endpoint.mkdir()
        output = self.root / "build/origin/fixture-unit/demo"
        output.parent.mkdir(parents=True)
        output.symlink_to(endpoint, target_is_directory=True)
        new = self.trees / "origin/fixture-unit/demo"
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(ValueError, "invalid worktree output"):
                _ = worktrees.move_worktree(old, "origin", "fixture-unit", "demo")
            with self.assertRaisesRegex(ValueError, "expected a real output folder"):
                worktrees.ensure_target_link(new, output)
        self.assertEqual((new / "target/artifact").read_text(), "warm")
        self.assertEqual(output.readlink(), endpoint)
        self.assertEqual(list(endpoint.iterdir()), [])

    def test_move_reuses_git_entry_and_finishes_after_transcript_conflict(self) -> None:
        self.layout_fixture()
        old = self.trees / "fixture-unit-old"
        self.make(old, "fixture-branch")
        checkout = git_files.at(old)
        berth = checkout.git_dir / "cargo-berth-worktree-id"
        _ = berth.write_text("fixture-id\n")
        _ = (old / "untracked.txt").write_text("preserved\n")
        new = self.trees / "origin/fixture-unit/demo"
        projects = self.home / ".claude/projects"
        other_projects = self.home / ".claude2/projects"
        old_project = re.sub(r"[^A-Za-z0-9]", "-", str(old))
        new_project = re.sub(r"[^A-Za-z0-9]", "-", str(new))
        for root in (projects, other_projects):
            (root / old_project).mkdir(parents=True)
            _ = (root / old_project / "conversation.jsonl").write_text("history\n")
        (projects / new_project).mkdir()
        with patch.dict(os.environ, self.environment, clear=True):
            snapshot = staff.snapshot()
            row = next(row for row in snapshot.rows if row.kind == "unit")
            _ = staff.set_worktree(row.id, staff.PresentWorktree(host_name(), old), expected=row.worktree)
            lock = checkout.git_dir / "locked"
            reason = f"staff:{row.id}"
            _ = self.git(old, "worktree", "lock", "--reason", reason, str(old))
            with self.assertRaisesRegex(ValueError, "existing transcript folder"):
                _ = worktrees.move_worktree(old, "origin", "fixture-unit", "demo")
            self.assertTrue(worktrees.move_file(old).is_file())
            self.assertEqual(old.readlink(), new)
            self.assertEqual(lock.read_text().strip(), reason)
            self.assertEqual(staff.by_id(row.id), replace(row, worktree=staff.PresentWorktree(host_name(), old)))
            (projects / new_project).rmdir()
            publish = staff.set_worktree

            def publish_with_lock(identity: staff.StaffId, location: staff.PresentWorktree | staff.RemovedWorktree,
                                  *, expected: staff.PresentWorktree | staff.RemovedWorktree
                                  | staff.WorktreeUnfilled) -> staff.StaffRow:
                self.assertEqual(lock.read_text().strip(), reason)
                return publish(identity, location, expected=expected)

            with patch.object(staff, "set_worktree", publish_with_lock):
                self.assertEqual(worktrees.move_worktree(old, "origin", "fixture-unit", "demo"), new)
            self.assertEqual(lock.read_text().strip(), reason)
            self.assertEqual(worktrees.move_worktree(old, "origin", "fixture-unit", "demo"), new)
            self.assertEqual(git_files.at(new).git_dir, checkout.git_dir)
            self.assertEqual(berth.read_text(), "fixture-id\n")
            self.assertEqual((new / "untracked.txt").read_text(), "preserved\n")
            self.assertEqual(self.git(new, "branch", "--show-current"), "fixture-branch")
            expected = staff.PresentWorktree(host_name(), new)
            member = staff.snapshot().unit_by_worktree(host_name(), old)
            assert member is not None
            self.assertEqual(member.worktree, expected)
            self.assertEqual(staff.snapshot().by_worktree(host_name(), old / "subfolder"), member)
            self.assertFalse(self.record_path(old).exists())
            layout = worktrees.output_record("origin", "fixture-unit", "demo")
            self.assertEqual(worktrees.worktree_output(new), layout)
            self.assertEqual((new / "target").readlink(), layout.output)
            for root in (projects, other_projects):
                self.assertEqual((root / new_project / "conversation.jsonl").read_text(), "history\n")

            start = worktree_links.SessionStart("old-session", 102, (old / "subfolder",))
            current = worktree_links.SessionStart("new-session", 103, (new,))
            for evidence in (worktree_links.SessionStarts((start, current), ()),
                             worktree_links.SessionStarts((current,), ("registry unreadable",))):
                with patch.object(worktree_links, "live_session_starts", return_value=evidence):
                    first, = worktrees.old_worktree_links(remove_unused=True)
                self.assertEqual(first.sessions, (start,) if start in evidence.sessions else ())
                self.assertFalse(first.removed)
                self.assertTrue(old.is_symlink())
            with patch.object(worktree_links, "live_session_starts",
                              return_value=worktree_links.SessionStarts((current,), ())):
                final, = worktrees.old_worktree_links(remove_unused=True)
            self.assertTrue(final.removed)
            self.assertFalse(old.is_symlink())

    def test_claude_moves_defer_output_until_a_retry_or_migration(self) -> None:
        self.layout_fixture()
        config = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        with config.open("a") as handle:
            _ = handle.write("repo_word.claude_commands=claude\n")
        origin = self.root / "claude_commands.git"
        _ = self.git(self.root, "clone", "--bare", str(self.origin), str(origin))
        main = self.home / ".claude"
        _ = self.git(main, "init", "--initial-branch=main")
        _ = self.git(main, "remote", "add", "origin", str(origin))
        _ = self.git(main, "fetch", "origin")
        _ = self.git(main, "checkout", "-B", "main", "--track", "origin/main")
        _ = self.git(main, "remote", "set-head", "origin", "main")
        legacy = self.root / "build/claude/scratch"
        legacy.mkdir(parents=True)
        destinations = self.trees / "claude/fixture-unit"
        names = ("retry", "migrate")
        with patch.dict(os.environ, self.environment, clear=True):
            for name in names:
                old = self.add_linked_tree(main, self.trees / name, f"fixture-unit-{name}")
                new = destinations / name
                output = StringIO()
                with redirect_stdout(output):
                    self.assertEqual(worktrees.move_worktree(old, "claude", "fixture-unit", name), new)
                self.assertEqual(output.getvalue().strip(), f"defer output {new}: archive the old build root first")
                self.assertEqual(old.readlink(), new)
                self.assertEqual(self.git(new, "branch", "--show-current"), f"fixture-unit-{name}")
                self.assertFalse(os.path.lexists(new / "target"))
                self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.LINKED)

            old = self.trees / "retry"
            new = destinations / "retry"
            self.assertIsInstance(worktrees.cleanup(new), worktrees.CleanupReady)
            removal = StringIO()
            with redirect_stderr(removal):
                worktrees.remove_git_checkout(new, main)
            self.assertIn(f"kept for a manual removal list: {new}", removal.getvalue())
            make = [sys.executable, str(WORKTREES), "make", "claude", "fixture-unit"]
            occupied = self.command(new, [*make, "retry"])
            self.assertEqual(occupied.returncode, 1, occupied.stdout + occupied.stderr)
            self.assertIn("editable repository destination is occupied", occupied.stderr)
            made = self.command(new, [*make, "another"])
            self.assertEqual(made.returncode, 0, made.stdout + made.stderr)
            self.assertTrue((destinations / "another/target").is_symlink())

            legacy.rmdir()
            self.assertEqual(worktrees.move_worktree(old, "claude", "fixture-unit", "retry"), new)
            self.assertEqual((new / "target").readlink(), self.root / "build/claude/fixture-unit/retry")
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.FINISHED)
            self.assertFalse(os.path.lexists(destinations / "migrate/target"))
        empty = self.command(self.root, self.migration_command("--list", os.devnull))
        self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
        self.assertNotIn("resume ", empty.stdout)
        self.assertFalse(os.path.lexists(destinations / "migrate/target"))
        list_file = self.root / "moves"
        _ = list_file.write_text(f"{self.trees / 'migrate'} fixture-unit migrate\n")
        applied = self.command(self.root, self.migration_command("--list", list_file))
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertIn(f"resume {self.trees / 'migrate'} -> {destinations / 'migrate'}", applied.stdout)
        with patch.dict(os.environ, self.environment, clear=True):
            for name in names:
                new = destinations / name
                layout = worktrees.output_record("claude", "fixture-unit", name)
                self.assertEqual((new / "target").readlink(), layout.output)
                self.assertEqual(worktrees.worktree_output(new), layout)
                journal = worktrees.read_move(worktrees.move_file(self.trees / name))
                self.assertEqual(journal.stage, worktrees.MoveStage.FINISHED)

    def add_linked_tree(self, repository: Path, path: Path, branch: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = self.git(repository, "worktree", "add", "-b", branch, str(path))
        return path

    def migration_main(self) -> Path:
        main = self.home / "rust/origin"
        main.parent.mkdir(parents=True, exist_ok=True)
        _ = self.git(self.root, "clone", str(self.origin), str(main))
        return main

    def migration_unit(self, name: str, worktree: staff.PresentWorktree | staff.RemovedWorktree
                       | staff.WorktreeUnfilled = staff.WORKTREE_UNFILLED) -> None:
        path = Path(self.environment["STAFF_FILE"])
        snapshot = staff.StaffSnapshot.decode(path.read_text())
        parent = next(row.id for row in snapshot.rows if row.kind == "showrunner")
        identity = staff.StaffId(str(uuid4()))
        unit = staff.StaffRow(identity, "unit", name, staff.ReportsTo(parent), current_session=identity,
                              worktree=worktree, unit_type=staff.UnitDetails("direct"))
        _ = path.write_text(replace(snapshot, rows=(*snapshot.rows, unit)).encode())

    def test_migration_limits_the_dry_run_and_apply_to_listed_worktrees(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        listed = self.add_linked_tree(main, self.trees / "listed", "listed")
        unlisted = self.add_linked_tree(main, self.trees / "origin-fixture-unit-unlisted", "unlisted")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{listed} fixture-unit chosen\n")
        new = self.trees / "origin/fixture-unit/chosen"
        command = self.migration_command("--list", list_file)

        missing_selection = self.command(self.root, self.migration_command("--dry-run"))
        self.assertEqual(missing_selection.returncode, 2, missing_selection.stdout + missing_selection.stderr)
        self.assertIn("specify --list FILE, --all or --repo WORD", missing_selection.stderr)
        for flags in (["--dry-run"], []):
            with self.subTest(flags=flags):
                result = self.command(self.root, [*command, *flags])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                moves = [line for line in result.stdout.splitlines() if line.startswith("move ")]
                self.assertEqual(moves, [f"move {listed} -> {new} | fixture-unit | chosen | list | live-unit"])
                self.assertNotIn(str(unlisted), result.stdout)
                self.assertFalse(unlisted.is_symlink())
                self.assertFalse(os.path.lexists(main / "target"))
                self.assertFalse((self.trees / "origin/fixture-unit/unlisted").exists())
        self.assertEqual(listed.readlink(), new)
        self.assertIn(f"worktree {unlisted}", self.git(main, "worktree", "list", "--porcelain"))

    def other_main(self) -> Path:
        """Make the main checkout of a second repository, `other`."""
        other_origin = self.root / "other.git"
        _ = self.git(self.root, "init", "--bare", "--initial-branch=main", str(other_origin))
        other = self.home / "rust/other"
        _ = self.git(self.root, "clone", str(other_origin), str(other))
        _ = self.git(other, "commit", "--allow-empty", "-m", "Other fixture")
        return other

    def real_target(self, checkout: Path) -> Path:
        """Give the checkout a real target folder with an artifact, and give its Cargo lock."""
        profile = checkout / "target/debug"
        profile.mkdir(parents=True)
        _ = (profile / "artifact").write_text("warm")
        lock = profile / ".cargo-lock"
        lock.touch()
        return lock

    def moved_worktree_with_real_target(self) -> tuple[Path, Path, Path]:
        """Move a worktree, then put a real target where its link was; give the checkout, its output, and its Cargo lock."""
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{old} fixture-unit source\n")
        moved = self.command(self.root, self.migration_command("--list", list_file))
        self.assertEqual(moved.returncode, 0, moved.stdout + moved.stderr)
        new = self.trees / "origin/fixture-unit/source"
        (new / "target").unlink()
        return new, self.root / "build/origin/fixture-unit/source", self.real_target(new)

    @staticmethod
    def rename_then(target: Path, action: Callable[[], object]) -> Callable[..., None]:
        """Give an os.rename that runs the action after it renames the target."""
        rename = os.rename

        def renamed(source: str | os.PathLike[str], destination: str | os.PathLike[str], *,
                    src_dir_fd: int | None = None, dst_dir_fd: int | None = None) -> None:
            rename(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            if Path(source) == target:
                _ = action()

        return renamed

    def real_main_target(self) -> tuple[Path, Path]:
        """Make the main checkout with a real target folder and give the checkout and its Cargo lock."""
        main = self.migration_main()
        return main, self.real_target(main)

    def test_migration_repo_selects_one_repository(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        tree = self.add_linked_tree(main, self.trees / "origin-fixture-unit-repair", "repair")
        other = self.other_main()
        other_tree = self.add_linked_tree(other, self.trees / "other-fixture-unit-probe", "probe")
        command = self.migration_command("--repo", "origin")
        for flags in (["--dry-run"], []):
            with self.subTest(flags=flags):
                result = self.command(self.root, [*command, *flags])
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"main output {main} -> {self.root}/build/origin/main", result.stdout)
                self.assertIn(f"move {tree} -> {self.trees}/origin/fixture-unit/repair |", result.stdout)
                self.assertNotIn(str(other), result.stdout)
                self.assertNotIn(str(other_tree), result.stdout)
        self.assertEqual((main / "target").readlink(), self.root / "build/origin/main")
        self.assertEqual(tree.readlink(), self.trees / "origin/fixture-unit/repair")
        self.assertFalse(os.path.lexists(other / "target"))
        self.assertFalse(other_tree.is_symlink())

    def test_migration_refuses_repo_with_all(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        result = self.command(self.root, self.migration_command("--repo", "origin", "--all", "--dry-run"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("not allowed with argument", result.stderr)
        self.assertFalse(os.path.lexists(main / "target"))

    def mac_migration(self, repo: str, *, dry_run: bool,
                      claim_error: Exception | None = None) -> tuple[int, str, list[str], list[str]]:
        """Run the natedev form with a fake claim layer and a fake ssh call; give the status, output, events, and commands."""
        events: list[str] = []
        commands: list[str] = []
        claims: list[tuple[object, ...]] = []

        @contextmanager
        def fake_claim(*arguments: object) -> Generator[None]:
            claims.append(arguments)
            if claim_error is not None:
                raise claim_error
            events.append("claim")
            try:
                yield
            finally:
                events.append("release")

        def fake_ssh(command: str) -> int:
            events.append("ssh")
            commands.append(command)
            return 0

        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(mac_run, "repository_claim", fake_claim),
              patch.object(worktrees, "_run_on_mac", fake_ssh),
              redirect_stdout(output := StringIO()), redirect_stderr(output)):
            status = worktrees.migrate_on_mac(repo, dry_run=dry_run)
        if claims:
            main = self.home / "rust/origin"
            self.assertEqual(claims, [(main, "mac", f"migrate --repo {repo}", mac_test.MacClaimKind.MIRROR)])
        return status, output.getvalue(), events, commands

    def test_mac_host_migration_runs_no_ssh_when_the_claim_is_busy(self) -> None:
        self.layout_fixture()
        _ = self.migration_main()
        busy = mac_run.MacRunRefused("mac is busy with native Mac run")
        status, output, events, commands = self.mac_migration("origin", dry_run=False, claim_error=busy)
        self.assertEqual(status, 1)
        self.assertEqual(output, "kept: origin: mac is busy with native Mac run\n")
        self.assertEqual((events, commands), ([], []))

    def test_mac_host_dry_run_takes_no_claim_and_passes_the_dry_run(self) -> None:
        self.layout_fixture()
        status, output, events, commands = self.mac_migration("origin", dry_run=True,
                                                              claim_error=AssertionError("claimed"))
        self.assertEqual(status, 0, output)
        self.assertEqual(events, ["ssh"])
        self.assertEqual(commands, [f"{worktrees._MAC_MIGRATE} origin --dry-run"])  # pyright: ignore[reportPrivateUsage]

    def test_mac_host_migration_refuses_a_repo_with_no_natedev_checkout(self) -> None:
        self.layout_fixture()
        status, output, events, commands = self.mac_migration("missing", dry_run=False)
        self.assertEqual(status, 2)
        self.assertRegex(output, r"\Amigrate --repo missing: [^\n]*no main checkout for 'missing'\n\Z")
        self.assertEqual((events, commands), ([], []))

    def test_migration_refuses_an_unknown_repo_word_in_one_line(self) -> None:
        self.layout_fixture()
        result = self.command(self.root, self.migration_command("--repo", "missing"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertRegex(result.stderr, r"\Amigrate --repo missing: [^\n]*no main checkout for 'missing'\n\Z")

    def test_mac_host_needs_repo_and_natedev(self) -> None:
        self.layout_fixture()
        for arguments in (["--host", "mac", "--all"], ["--host", "mac", "--repo", "origin", "--list", os.devnull]):
            with self.subTest(arguments=arguments):
                result = self.command(self.root, self.migration_command(*arguments))
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("--host needs --repo WORD and takes no --list", result.stderr)
        with (patch.object(sys, "argv", ["worktrees.py", "migrate", "--repo", "origin", "--host", "mac"]),
              patch.object(remote, "local_machine", return_value="mac"), redirect_stderr(errors := StringIO()),
              self.assertRaises(SystemExit) as refused):
            _ = worktrees.main()
        self.assertEqual(refused.exception.code, 2)
        self.assertIn("--host runs on natedev only", errors.getvalue())

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_keeps_a_main_checkout_whose_target_a_process_uses(self) -> None:
        self.layout_fixture()
        main, _lock = self.real_main_target()
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                              cwd=main / "target/debug", stdin=subprocess.PIPE, env=self.environment) as child:
            self.register_process(child.pid)
            result = self.command(self.root, self.migration_command("--repo", "origin"))
            _ = child.communicate(b"")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"keep {main}: target in use (pids {child.pid})", result.stdout)
        self.assertNotIn(f"main output {main} ->", result.stdout)
        self.assertEqual((main / "target/debug/artifact").read_text(), "warm")
        self.assertFalse((main / "target").is_symlink())

    def test_migration_keeps_a_main_checkout_with_a_held_cargo_lock(self) -> None:
        self.layout_fixture()
        main, lock = self.real_main_target()
        with lock.open("rb") as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            for flags in (["--dry-run"], []):
                with self.subTest(flags=flags):
                    result = self.command(self.root, [*self.migration_command("--all"), *flags])
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"keep {main}: kept: {lock} held", result.stdout)
                    self.assertNotIn(f"main output {main} ->", result.stdout)
        self.assertEqual((main / "target/debug/artifact").read_text(), "warm")
        self.assertFalse((main / "target").is_symlink())

    def test_migration_keeps_a_main_checkout_with_a_held_cargo_lock_at_any_depth(self) -> None:
        self.layout_fixture()
        main, _lock = self.real_main_target()
        profile = main / "target/x86_64-unknown-linux-gnu/debug"
        profile.mkdir(parents=True)
        lock = profile / ".cargo-lock"
        lock.touch()
        with lock.open("rb") as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.command(self.root, self.migration_command("--repo", "origin"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"keep {main}: kept: {lock} held", result.stdout)
        self.assertFalse((main / "target").is_symlink())

    def test_migration_moves_a_free_main_target_into_the_main_output(self) -> None:
        self.layout_fixture()
        main, _lock = self.real_main_target()
        result = self.command(self.root, self.migration_command("--repo", "origin"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.root / "build/origin/main"
        self.assertIn(f"main output {main} -> {output}", result.stdout)
        self.assertEqual((main / "target").readlink(), output)
        self.assertEqual((output / "debug/artifact").read_text(), "warm")
        with (output / "debug/.cargo-lock").open("rb") as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_migration_asks_for_a_rerun_when_a_new_main_target_appears_after_the_move(self) -> None:
        self.layout_fixture()
        main, _lock = self.real_main_target()
        target = main / "target"
        output = self.root / "build/origin/main"
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(os, "rename", self.rename_then(target, target.mkdir)),
              redirect_stdout(output_text := StringIO())):
            status = worktrees.migrate_worktrees(None, scope=worktrees.MigrationScope.REPOSITORY,
                                                 repo="origin", dry_run=False)
        self.assertEqual(status, 1, output_text.getvalue())
        self.assertIn(f"keep {main}: rerun: a new {target} appeared after the move; the earlier target is in {output}",
                      output_text.getvalue())
        self.assertNotIn(f"main output {main} ->", output_text.getvalue())
        self.assertEqual((output / "debug/artifact").read_text(), "warm")
        self.assertFalse(target.is_symlink())
        self.assertEqual(list(target.iterdir()), [])

    def test_migration_links_the_real_target_of_an_already_moved_worktree(self) -> None:
        self.layout_fixture()
        new, output, lock = self.moved_worktree_with_real_target()
        command = self.migration_command("--repo", "origin")
        with lock.open("rb") as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = self.command(self.root, command)
        self.assertEqual(held.returncode, 1, held.stdout + held.stderr)
        self.assertIn(f"keep {new}: kept: {lock} held", held.stdout)
        self.assertFalse((new / "target").is_symlink())
        result = self.command(self.root, command)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"output {new} -> {output}", result.stdout)
        self.assertNotIn(f"keep {new}", result.stdout)
        self.assertEqual((new / "target").readlink(), output)
        self.assertEqual((output / "debug/artifact").read_text(), "warm")

    def test_migration_all_links_the_real_target_of_an_already_moved_worktree(self) -> None:
        self.layout_fixture()
        new, output, _lock = self.moved_worktree_with_real_target()
        result = self.command(self.root, self.migration_command("--all"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"output {new} -> {output}", result.stdout)
        self.assertEqual((new / "target").readlink(), output)
        self.assertEqual((output / "debug/artifact").read_text(), "warm")

    def test_migration_keeps_a_checkout_whose_new_target_appears_after_the_save_and_goes_on(self) -> None:
        self.layout_fixture()
        main, _lock = self.real_main_target()
        other = self.other_main()
        _ = self.real_target(other)
        output = self.root / "build/origin/main"
        output.mkdir(parents=True)
        _ = (output / "earlier").write_text("kept")
        target = main / "target"
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(os, "rename", self.rename_then(target, target.mkdir)),
              redirect_stdout(text := StringIO()), redirect_stderr(StringIO())):
            status = worktrees.migrate_worktrees(None, scope=ALL_WORKTREES, dry_run=False)
        self.assertEqual(status, 0, text.getvalue())
        saved, = (self.home / "rust").glob(".saved-target-origin-*/target")
        self.assertIn(f"keep {main}: rerun: a new {target} appeared after the move; the earlier target is in {saved}",
                      text.getvalue())
        self.assertEqual((saved / "debug/artifact").read_text(), "warm")
        self.assertEqual((output / "earlier").read_text(), "kept")
        self.assertFalse(target.is_symlink())
        self.assertIn(f"main output {other} -> {self.root}/build/other/main", text.getvalue())
        self.assertEqual((other / "target").readlink(), self.root / "build/other/main")
        self.assertEqual((self.root / "build/other/main/debug/artifact").read_text(), "warm")
        with (patch.dict(os.environ, self.environment, clear=True),
              redirect_stdout(StringIO()), redirect_stderr(rerun := StringIO())):
            _ = worktrees.migrate_worktrees(None, scope=ALL_WORKTREES, dry_run=False)
        self.assertIn(f"kept for a manual removal list: {saved.parent} (worktrees, the checkout's earlier Cargo target)",
                      rerun.getvalue())

    def test_migration_links_the_target_when_a_stop_comes_between_the_move_and_the_link(self) -> None:
        self.layout_fixture()
        main, _lock = self.real_main_target()
        target = main / "target"
        output = self.root / "build/origin/main"
        stop = partial(signal.pthread_kill, threading.get_ident(), signal.SIGINT)
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(os, "rename", self.rename_then(target, stop)),
              redirect_stdout(StringIO()), self.assertRaises(KeyboardInterrupt)):
            _ = worktrees.migrate_worktrees(None, scope=worktrees.MigrationScope.REPOSITORY,
                                            repo="origin", dry_run=False)
        self.assertEqual(target.readlink(), output)
        self.assertEqual((output / "debug/artifact").read_text(), "warm")

    def test_migration_links_the_target_when_a_stop_comes_between_the_save_and_the_link(self) -> None:
        self.layout_fixture()
        main, _lock = self.real_main_target()
        target = main / "target"
        output = self.root / "build/origin/main"
        output.mkdir(parents=True)
        _ = (output / "earlier").write_text("kept")
        stop = partial(signal.pthread_kill, threading.get_ident(), signal.SIGINT)
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(os, "rename", self.rename_then(target, stop)),
              redirect_stdout(StringIO()), redirect_stderr(StringIO()), self.assertRaises(KeyboardInterrupt)):
            _ = worktrees.migrate_worktrees(None, scope=worktrees.MigrationScope.REPOSITORY,
                                            repo="origin", dry_run=False)
        self.assertEqual(target.readlink(), output)
        saved, = main.parent.glob(".saved-target-origin-*/target")
        self.assertEqual((saved / "debug/artifact").read_text(), "warm")

    def test_migration_keeps_a_checkout_whose_target_appears_after_the_check(self) -> None:
        self.layout_fixture()
        new, output, _lock = self.moved_worktree_with_real_target()
        shutil.rmtree(new / "target")
        link = worktrees.ensure_target_link

        def appear_then_link(checkout: Path, linked: Path, *, save_existing: bool) -> None:
            (checkout / "target").mkdir()
            link(checkout, linked, save_existing=save_existing)

        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees, "ensure_target_link", appear_then_link),
              redirect_stdout(text := StringIO())):
            status = worktrees.migrate_worktrees(None, scope=worktrees.MigrationScope.REPOSITORY,
                                                 repo="origin", dry_run=False)
        self.assertEqual(status, 1, text.getvalue())
        self.assertIn(f"keep {new}: kept: {new / 'target'}: expected a link to {output}", text.getvalue())
        self.assertFalse((new / "target").is_symlink())
        self.assertTrue((new / "target").is_dir())

    def test_migration_moves_the_target_back_when_the_link_fails(self) -> None:
        self.layout_fixture()
        main, _lock = self.real_main_target()
        other = self.other_main()
        _ = self.real_target(other)
        target = main / "target"
        link = Path.symlink_to

        def refuse_origin(path: Path, to: str | os.PathLike[str], target_is_directory: bool = False) -> None:
            if path == target:
                raise PermissionError(errno.EACCES, "fixture refusal", str(path))
            link(path, to, target_is_directory)

        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(Path, "symlink_to", refuse_origin),
              redirect_stdout(text := StringIO())):
            status = worktrees.migrate_worktrees(None, scope=ALL_WORKTREES, dry_run=False)
        self.assertEqual(status, 0, text.getvalue())
        self.assertIn(f"keep {main}: kept: [Errno {errno.EACCES}] fixture refusal: '{target}'", text.getvalue())
        self.assertEqual((target / "debug/artifact").read_text(), "warm")
        self.assertFalse(target.is_symlink())
        self.assertEqual((other / "target").readlink(), self.root / "build/other/main")

    def test_failed_link_moves_the_output_back_to_the_previous_target(self) -> None:
        self.layout_fixture()
        checkout = self.migration_main()
        previous = self.root / "backup/target"
        (previous / "debug").mkdir(parents=True)
        _ = (previous / "debug/artifact").write_text("warm")
        output = self.root / "build/origin/main"
        refusal = PermissionError(errno.EACCES, "fixture refusal")
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(Path, "symlink_to", side_effect=refusal), self.assertRaises(PermissionError)):
            worktrees._link_with_previous_target(checkout, output, previous)  # pyright: ignore[reportPrivateUsage]
        self.assertEqual((previous / "debug/artifact").read_text(), "warm")
        self.assertFalse(output.exists())
        self.assertFalse(os.path.lexists(checkout / "target"))

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_scan_skips_its_own_cargo_lock_descriptors(self) -> None:
        self.layout_fixture()
        main, _lock = self.real_main_target()
        self.register_process(os.getpid())
        with patch.dict(os.environ, self.environment, clear=True), redirect_stdout(text := StringIO()):
            status = worktrees.migrate_worktrees(None, scope=worktrees.MigrationScope.REPOSITORY,
                                                 repo="origin", dry_run=False)
        self.assertEqual(status, 0, text.getvalue())
        self.assertIn(f"main output {main} -> {self.root}/build/origin/main", text.getvalue())
        self.assertEqual((main / "target").readlink(), self.root / "build/origin/main")

    def test_migration_repo_keeps_the_text_of_an_error_that_is_not_an_unknown_word(self) -> None:
        self.layout_fixture()
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees, "repository_selection", side_effect=ValueError("fixture journal")),
              self.assertRaisesRegex(ValueError, "fixture journal")):
            _ = worktrees.migrate_worktrees(None, scope=worktrees.MigrationScope.REPOSITORY,
                                            repo="origin", dry_run=False)

    def test_migration_repo_counts_a_deferred_output_as_no_work(self) -> None:
        self.layout_fixture()
        new, _output, _lock = self.moved_worktree_with_real_target()
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees.MigrationPublication, "places_output", return_value=False),
              redirect_stdout(text := StringIO())):
            status = worktrees.migrate_worktrees(None, scope=worktrees.MigrationScope.REPOSITORY,
                                                 repo="origin", dry_run=False)
        self.assertEqual(status, 0, text.getvalue())
        self.assertIn(f"keep {new}: already moved; defer output: archive the old build root first", text.getvalue())

    def test_migration_reports_an_unnamed_saved_target_once_per_run(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        _ = self.other_main()
        unnamed = main.parent / ".saved-target-ab_d1234"
        (unnamed / "target").mkdir(parents=True)
        result = self.command(self.root, self.migration_command("--all"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = f"kept for a manual removal list: {unnamed} (worktrees, the checkout's earlier Cargo target)"
        self.assertEqual(result.stderr.count(line), 1, result.stderr)

    def test_migration_reports_the_saved_target_of_a_main_checkout_with_a_deferred_output(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        saved = main.parent / f".saved-target-{main.name}-abcd1234"
        (saved / "target").mkdir(parents=True)
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees.MigrationPublication, "places_output", return_value=False),
              redirect_stdout(text := StringIO()), redirect_stderr(errors := StringIO())):
            status = worktrees.migrate_worktrees(None, scope=worktrees.MigrationScope.REPOSITORY,
                                                 repo="origin", dry_run=False)
        self.assertEqual(status, 0, text.getvalue() + errors.getvalue())
        self.assertIn(f"defer main output {main} ->", text.getvalue())
        self.assertIn(f"kept for a manual removal list: {saved} (worktrees, the checkout's earlier Cargo target)",
                      errors.getvalue())

    def test_migration_reports_the_saved_target_of_a_linked_moved_worktree(self) -> None:
        self.layout_fixture()
        new, output, _lock = self.moved_worktree_with_real_target()
        _ = (output / "earlier").write_text("kept")
        command = self.migration_command("--repo", "origin")
        saved = self.command(self.root, command)
        self.assertEqual(saved.returncode, 0, saved.stdout + saved.stderr)
        self.assertEqual((new / "target").readlink(), output)
        folder, = new.parent.glob(f".saved-target-{new.name}-*")
        rerun = self.command(self.root, command)
        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
        self.assertIn(f"kept for a manual removal list: {folder} (worktrees, the checkout's earlier Cargo target)",
                      rerun.stderr)

    def test_migration_links_a_moved_worktree_whose_target_is_missing(self) -> None:
        self.layout_fixture()
        new, output, _lock = self.moved_worktree_with_real_target()
        shutil.rmtree(new / "target")
        result = self.command(self.root, self.migration_command("--repo", "origin"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"output {new} -> {output}", result.stdout)
        self.assertNotIn(f"keep {new}", result.stdout)
        self.assertEqual((new / "target").readlink(), output)

    def test_migration_keeps_a_checkout_whose_build_folder_lock_is_held_and_goes_on(self) -> None:
        self.layout_fixture()
        main, _lock = self.real_main_target()
        other = self.other_main()
        _ = self.real_target(other)
        output = self.root / "build/origin/main"
        output.parent.mkdir(parents=True)
        with output.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.command(self.root, self.migration_command("--all"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"keep {main}: kept: {output}: the build folder lock is held. Rerun when idle", result.stdout)
        self.assertEqual((main / "target/debug/artifact").read_text(), "warm")
        self.assertFalse((main / "target").is_symlink())
        self.assertIn(f"main output {other} -> {self.root}/build/other/main", result.stdout)
        self.assertEqual((other / "target").readlink(), self.root / "build/other/main")

    def test_run_ssh_waits_for_ssh_to_end_on_sigterm(self) -> None:
        marker = self.root / "ended"
        # The fake ssh sends SIGTERM to the caller, sleeps briefly, and then records whether it leads its own session.
        child = ("import os, pathlib, signal, sys, time; os.kill(os.getppid(), signal.SIGTERM); time.sleep(0.2); "
                 + "pathlib.Path(sys.argv[1]).write_text(str(os.getsid(0) == os.getpid()))")
        runner = ("import sys\nfrom scripts.worktree import worktrees\n"
                  + "worktrees._run_ssh((sys.executable, '-c', sys.argv[1], sys.argv[2]))\nprint('after ssh')\n")
        result = subprocess.run([sys.executable, "-c", runner, child, str(marker)], cwd=CHECKOUT,
                                capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(result.returncode, -signal.SIGTERM, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(), "True")
        self.assertNotIn("after ssh", result.stdout)

    def test_migration_refuses_rows_for_numbered_hana_copies(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        list_file = self.root / "list"
        copies = tuple(
            self.add_linked_tree(main, self.root / "build/hana" / unit / "1", unit)
            for unit in ("hana-merges", "startup")
        )
        for path, flags in product(copies, ([], ["--dry-run"], ["--all", "--dry-run"])):
            with self.subTest(path=path, flags=flags):
                _ = list_file.write_text(f"{path} fixture-unit selected\n")
                result = self.command(self.root, self.migration_command("--list", list_file, *flags))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"{list_file}:1: cannot migrate numbered build copy {path}", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertFalse((self.state / "moves").exists())
        full = self.command(self.root, self.migration_command("--all"))
        self.assertEqual(full.returncode, 0, full.stdout + full.stderr)
        for path in copies:
            self.assertIn(f"keep {path}: a numbered build copy; the build folder model owns it", full.stdout)
            self.assertNotIn(f"move {path} ->", full.stdout)
            self.assertFalse(path.is_symlink())
            self.assertIn(f"worktree {path}", self.git(main, "worktree", "list", "--porcelain"))

    def test_migration_uses_the_staff_unit_name_for_list_rows_and_fallback(self) -> None:
        self.layout_fixture()
        config = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        with config.open("a") as handle:
            _ = handle.write("repo_word.origin=claude\n")
        main = self.migration_main()
        self.migration_unit("unit-board", staff.PresentWorktree(host_name(), main))
        listed = self.add_linked_tree(main, self.trees / "claude-natedev-unit-board-home", "home")
        by_folder = self.add_linked_tree(main, self.trees / "claude-natedev-unit-board-cablesfault", "cablesfault")
        by_subject = self.add_linked_tree(main, self.trees / "claude-legacy-note", "note")
        _ = self.git(by_subject, "commit", "--allow-empty", "-m", "checkpoint(claude-natedev-unit-board): save work")
        list_file = self.root / "list"
        for unit, flags in (("natedev-unit-board", []), ("claude-unit-board", ["--all", "--dry-run"]),
                            ("fixture-production/unit-board", ["--dry-run"])):
            with self.subTest(unit=unit):
                _ = list_file.write_text(f"{listed} {unit} home\n")
                result = self.command(self.root, self.migration_command("--list", list_file, *flags))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"{unit} is not a staff unit; use unit-board", result.stderr)
                self.assertFalse(listed.is_symlink())
                self.assertFalse((self.state / "moves").exists())

        _ = list_file.write_text(f"{listed} unit-board home\n")
        result = self.command(self.root, self.migration_command("--all", "--list", list_file, "--dry-run"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        unit_folder = self.trees / "claude/unit-board"
        for old, short, source in ((listed, "home", "list"), (by_folder, "cablesfault", "folder name"),
                                   (by_subject, "legacy-note", "commit subject")):
            self.assertIn(f"move {old} -> {unit_folder / short} | unit-board | {short} | {source}", result.stdout)
        self.assertNotIn(f" -> {self.trees}/claude/natedev-unit-board/", result.stdout)

    def test_migration_refuses_a_different_valid_staff_unit_for_an_owned_checkout(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        owned = self.add_linked_tree(main, self.trees / "owned", "owned")
        recorded = self.add_linked_tree(main, self.trees / "recorded", "recorded")
        self.migration_unit("owner", staff.PresentWorktree(host_name(), owned))
        with patch.dict(os.environ, self.environment, clear=True):
            worktrees.write_record(worktrees.new_record(recorded, "recorded", main, self.base, "owner", ""))
        list_file = self.root / "list"
        for old in (owned, recorded):
            with self.subTest(old=old):
                _ = list_file.write_text(f"{old} fixture-unit chosen\n")
                result = self.command(self.root, self.migration_command("--list", list_file, "--dry-run"))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"{list_file}:1: {old} belongs to staff unit owner; use owner", result.stderr)
                _ = list_file.write_text(f"{old} owner chosen\n")
                result = self.command(self.root, self.migration_command("--list", list_file, "--dry-run"))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"move {old} -> {self.trees}/origin/owner/chosen", result.stdout)

    def test_migration_dry_run_reads_without_writing_then_applies_the_same_plan(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        named = self.add_linked_tree(main, self.trees / "named source", "branch-list")
        inferred = self.add_linked_tree(main, self.trees / "origin-fixture-unit-repair", "branch-name")
        retired = self.add_linked_tree(main, self.trees / "legacy-readme", "branch-subject")
        self.migration_unit("former-unit", staff.RemovedWorktree(host_name(), self.trees / "former-checkout"))
        _ = self.git(retired, "commit", "--allow-empty", "-m", "checkpoint(former-unit): save work")
        unknown = self.add_linked_tree(main, self.trees / "orphan", "branch-unknown")
        _ = self.git(unknown, "commit", "--allow-empty", "-m", "checkpoint(unlisted-unit): save work")
        by_record = self.add_linked_tree(main, self.trees / "legacy-proof", "saved-branch")
        by_branch = self.add_linked_tree(main, self.trees / "fixture-unit-45", "fixture-unit-phase-45")
        digits = self.add_linked_tree(main, self.trees / "fixture-unit-47", "47")
        empty = self.add_linked_tree(main, self.trees / "origin-fixture-unit", "fixture-unit")
        repeated = self.add_linked_tree(main, self.trees / "fixture-unit-natedev-tool-based-ui-patch", "one-patch")
        duplicate = self.add_linked_tree(main, self.trees / "fixture-unit-mac-patch", "other-patch")
        with patch.dict(os.environ, self.environment, clear=True):
            recorded = worktrees.new_record(by_record, "saved-branch", main, self.base, "fixture-unit", "")
            worktrees.write_record(recorded)
        made_before = self.record_path(by_record).read_bytes()
        missing = self.add_linked_tree(main, self.trees / "vanished", "branch-missing")
        shutil.rmtree(missing)
        catalyst = self.add_linked_tree(main, self.home / "rust/hana_catalyst", "protected-catalyst")
        user_tree = self.add_linked_tree(main, self.home / "worktrees/nate_style-message-clarity", "user-branch")
        maker = self.add_linked_tree(main, self.root / "b44doc-123", "maker-branch")
        session_tree = self.add_linked_tree(main, self.root / "session-root/fixture-unit-probe", "session-probe")
        scratch_tree = self.add_linked_tree(main, self.root / "scratch/fixture-unit-probe", "scratch-probe")
        conflict = self.add_linked_tree(main, self.trees / "conflict", "branch-conflict")
        occupied = self.trees / "origin/fixture-unit/taken"
        occupied.mkdir(parents=True)
        list_file = self.root / "list"
        _ = list_file.write_text(f"{shlex.quote(str(named))} fixture-unit chosen\n{conflict} fixture-unit taken\n")
        check = self.migration_command("--all", "--list", list_file)
        registered = self.git(main, "worktree", "list", "--porcelain")
        staff_file = Path(self.environment["STAFF_FILE"])
        staff_before = staff_file.read_bytes()
        log = self.root / "conf-reads"
        self.environment["CONF_READ_LOG"] = str(log)
        dry = self.command(self.root, [*check, "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn(f"move {named} -> {self.trees}/origin/fixture-unit/chosen | fixture-unit | chosen | list", dry.stdout)
        self.assertIn(f"move {inferred} -> {self.trees}/origin/fixture-unit/repair |", dry.stdout)
        self.assertIn(f"move {retired} -> {self.trees}/origin/former-unit/legacy-readme |", dry.stdout)
        self.assertIn("commit subject | retired-unit", dry.stdout)
        self.assertIn(f"review {missing}", dry.stdout)
        self.assertIn(f"keep {main}: main checkout", dry.stdout)
        self.assertIn(f"keep {unknown}: unit unknown", dry.stdout)
        self.assertIn(f"move {by_record} -> {self.trees}/origin/fixture-unit/legacy-proof |", dry.stdout)
        self.assertIn("legacy-proof | staff row | live-unit", dry.stdout)
        self.assertIn(f"move {by_branch} -> {self.trees}/origin/fixture-unit/phase-45 |", dry.stdout)
        self.assertIn(f"move {digits} -> {self.trees}/origin/fixture-unit/p47 |", dry.stdout)
        self.assertIn(f"move {empty} -> {self.trees}/origin/fixture-unit/worktree |", dry.stdout)
        for shared in (repeated, duplicate):
            self.assertIn(f"keep {shared}: short name shared by multiple worktrees", dry.stdout)
            self.assertNotIn(f"move {shared} ->", dry.stdout)
        self.assertIn(f"keep {catalyst}: showrunner checkout awaits question 4", dry.stdout)
        self.assertIn(f"keep {user_tree}: the user's worktree", dry.stdout)
        for protected in (maker, session_tree, scratch_tree):
            self.assertIn(f"keep {protected}: the maker owns this worktree", dry.stdout)
        self.assertIn(f"keep {conflict}: short name occupied", dry.stdout)
        self.assertEqual(self.git(main, "worktree", "list", "--porcelain"), registered)
        self.assertEqual(staff_file.read_bytes(), staff_before)
        self.assertFalse((self.state / "moves").exists())
        self.assertEqual(self.record_path(by_record).read_bytes(), made_before)
        self.assertFalse((self.root / "build").exists())
        self.assertFalse(log.exists())
        applied = self.command(self.root, check)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn(str(missing), self.git(main, "worktree", "list", "--porcelain"))
        self.assertEqual((main / "target").readlink(), self.root / "build/origin/main")
        self.assertEqual((named / "target").readlink(), self.root / "build/origin/fixture-unit/chosen")
        self.assertEqual(self.git(inferred, "branch", "--show-current"), "branch-name")
        self.assertFalse((retired / "target").is_symlink())
        self.assertFalse((self.root / "build/origin/former-unit").exists())

    def test_migration_keeps_build_folders_and_moves_legacy_scratch(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        with patch.dict(os.environ, self.environment, clear=True):
            copy = build_folders.copy_path("origin", "fixture-unit", 12)
            ci = build_folders.ci_path("origin", 3)
            output = build_folders.main_output_path("origin")
        _ = self.add_linked_tree(main, copy, "numbered-copy")
        _ = self.add_linked_tree(main, ci, "ci-runner")
        _ = self.add_linked_tree(main, output, "main-output")
        scratch = self.add_linked_tree(main, self.root / "build/claude/scratch/12", "legacy-scratch")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{ci} fixture-unit ci\n{output} fixture-unit output\n{scratch} fixture-unit legacy\n")

        result = self.command(self.root, [*self.migration_command("--all", "--list", list_file), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"keep {copy}: a numbered build copy; the build folder model owns it", result.stdout)
        self.assertIn(f"keep {ci}: a CI runner folder; the build folder model owns it", result.stdout)
        self.assertIn(f"keep {output}: a main output; the build folder model owns it", result.stdout)
        for path in (copy, ci, output):
            self.assertNotIn(f"move {path} ->", result.stdout)
        self.assertIn(f"move {scratch} -> {self.trees}/origin/fixture-unit/legacy |", result.stdout)

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_retries_a_worktree_after_its_process_exits(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        busy = self.add_linked_tree(main, self.trees / "busy", "busy")
        idle = self.add_linked_tree(main, self.trees / "idle", "idle")
        nested = busy / "nested"
        nested.mkdir()
        list_file = self.root / "list"
        _ = list_file.write_text(f"{busy} fixture-unit busy\n{idle} fixture-unit idle\n")
        command = self.migration_command("--all", "--list", list_file)

        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                              cwd=nested, stdin=subprocess.PIPE, env=self.environment) as child:
            self.register_process(child.pid)
            reason = f"keep {busy}: in use (pids {child.pid})"
            dry = self.command(self.root, [*command, "--dry-run"])
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn(reason, dry.stdout)
            self.assertEqual(dry.stdout.splitlines()[-1], "would move 1, kept in use 1, kept 1")
            first = self.command(self.root, command)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn(reason, first.stdout)
            self.assertEqual(first.stdout.splitlines()[-1], "moved 1, kept in use 1, kept 1")
            self.assertFalse(busy.is_symlink())
            self.assertTrue(idle.is_symlink())
            with patch.dict(os.environ, self.environment, clear=True):
                self.assertFalse(worktrees.move_file(busy).exists())
                self.assertEqual(worktrees.read_move(worktrees.move_file(idle)).stage, worktrees.MoveStage.FINISHED)

        second = self.command(self.root, command)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.splitlines()[-1], "moved 1, kept in use 0, kept 2")
        self.assertEqual(busy.readlink(), self.trees / "origin/fixture-unit/busy")
        self.assertIn(f"keep {idle.readlink()}: already moved", second.stdout)

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_keeps_a_held_busy_claim(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        busy = self.add_linked_tree(main, self.trees / "busy", "busy")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{busy} fixture-unit busy\n")
        command = self.migration_command("--all", "--list", list_file)
        busy_mark = scratch.busy_path(busy)
        with busy_mark.open("wb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            for flags, verb in ((["--dry-run"], "would move"), ([], "moved")):
                with self.subTest(verb=verb):
                    result = self.command(self.root, [*command, *flags])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"keep {busy}: kept: {busy_mark} held", result.stdout)
                    self.assertEqual(result.stdout.splitlines()[-1], f"{verb} 0, kept in use 1, kept 1")
        self.assertFalse(busy.is_symlink())
        self.assertFalse((self.trees / "origin/fixture-unit/busy").exists())

    def test_migration_keeps_a_held_removal_lock_in_both_modes(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{old} fixture-unit source\n")
        command = self.migration_command("--all", "--list", list_file)
        sidecar = old.with_name(old.name + ".lock")
        with sidecar.open("wb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            dry = self.command(self.root, [*command, "--dry-run"])
            applied = self.command(self.root, command)
        for result in (dry, applied):
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"keep {old}: kept: {sidecar} held", result.stdout)
            self.assertNotIn(f"move {old} ->", result.stdout)
        self.assertEqual(dry.stdout.splitlines()[:-1], applied.stdout.splitlines()[:-1])
        self.assertFalse(old.is_symlink())

    def test_migration_reports_a_held_move_lock_in_both_modes(self) -> None:
        self.layout_fixture()
        with patch.dict(os.environ, self.environment, clear=True):
            lock = worktrees.move_folder() / "move.lock"
        lock.parent.mkdir(parents=True)
        command = self.migration_command("--all")
        with lock.open("wb") as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            dry = self.command(self.root, [*command, "--dry-run"])
            applied = self.command(self.root, command)
        for result in (dry, applied):
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout.strip(), f"kept: {lock} held")

    def test_migration_keeps_a_source_when_its_publication_lock_is_held(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{old} fixture-unit source\n")
        command = self.migration_command("--all", "--list", list_file)
        staff_file = Path(self.environment["STAFF_FILE"])
        with patch.dict(os.environ, self.environment, clear=True):
            row = staff.snapshot().unit_by_worktree(host_name(), self.repository)
            assert row is not None
            previous = staff.PresentWorktree(host_name(), self.repository)
            _ = staff.set_worktree(row.id, replace(previous, path=old), expected=previous)
        for lock in (staff_file.with_suffix(".json.lock"), git_exclude.exclude_lock_path(main / ".git")):
            with self.subTest(lock=lock), lock.open("wb") as descriptor:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                dry = self.command(self.root, [*command, "--dry-run"])
                applied = self.command(self.root, command)
                for result in (dry, applied):
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"keep {old}: kept: {lock} held", result.stdout)
                    self.assertNotIn(f"move {old} ->", result.stdout)
                self.assertEqual(dry.stdout.splitlines()[:-1], applied.stdout.splitlines()[:-1])
                self.assertFalse(old.is_symlink())

    def test_migration_checks_the_owner_lock_before_a_remote_move(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        new = self.trees / "origin/fixture-unit/source"
        list_file = self.root / "list"
        _ = list_file.write_text(f"{old} fixture-unit source\n")
        staff_file = Path(self.environment["STAFF_FILE"])
        lock = staff_file.with_suffix(".json.lock")
        with patch.dict(os.environ, self.environment, clear=True):
            previous = staff.PresentWorktree(host_name(), self.repository)
            row = staff.snapshot().unit_by_worktree(previous.machine, previous.path)
            assert row is not None
            _ = staff.set_worktree(row.id, staff.PresentWorktree("mac", old), expected=previous)

        commands: list[str] = []

        def run_owner(arguments: list[str]) -> object:
            commands.append(arguments[0])
            result = self.command(CHECKOUT, [sys.executable, "-m", "scripts.production.staff", *arguments, "--json"])
            self.assertEqual(result.returncode, 0, result.stderr)
            return cast(object, json.loads(result.stdout))

        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(remote, "local_machine", return_value="mac"),
              patch.object(staff, "is_owner", return_value=False),
              patch.object(staff, "_remote", side_effect=run_owner)):
            with lock.open("wb") as descriptor:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                for dry_run in (True, False):
                    with redirect_stdout(output := StringIO()):
                        self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=dry_run), 0)
                    self.assertIn(f"keep {old}: kept: {lock} held", output.getvalue())
                    self.assertNotIn(f"move {old} ->", output.getvalue())
                self.assertEqual(commands, ["show", "worktree-write-lock-status"] * 2)
                self.assertFalse(old.is_symlink())
            with redirect_stdout(StringIO()):
                self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 0)
        self.assertEqual(old.readlink(), new)
        self.assertIn("set-worktree", commands)

    def test_migration_keeps_a_planned_numbered_copy(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        with patch.dict(os.environ, self.environment, clear=True):
            copy = build_folders.copy_path("origin", "fixture-unit", 12)
            _ = self.add_linked_tree(main, copy, "numbered-copy")
            new = self.trees / "origin/fixture-unit/p12"
            move = worktrees.WorktreeMove(copy, new, "origin", "fixture-unit", "p12", worktrees.MoveOutput.LIVE_UNIT)
            journal = worktrees._open_move(move)  # pyright: ignore[reportPrivateUsage]
            record = worktrees.move_file(copy)
        command = self.migration_command("--all")
        for flags in (["--dry-run"], []):
            with self.subTest(flags=flags):
                result = self.command(self.root, [*command, *flags])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"keep {copy}: a numbered build copy; the build folder model owns it", result.stdout)
                self.assertNotIn(f"resume {copy} ->", result.stdout)
                if flags:
                    with patch.dict(os.environ, self.environment, clear=True):
                        self.assertEqual(worktrees.read_move(record), journal)
        self.assertFalse(copy.is_symlink())
        self.assertFalse(new.exists())
        self.assertFalse(record.exists())

    def test_migration_keeps_a_planned_source_that_becomes_a_showrunner_checkout(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        new = self.trees / "origin/fixture-unit/source"
        move = worktrees.WorktreeMove(old, new, "origin", "fixture-unit", "source", worktrees.MoveOutput.LIVE_UNIT)
        with patch.dict(os.environ, self.environment, clear=True):
            journal = worktrees._open_move(move)  # pyright: ignore[reportPrivateUsage]
            record = worktrees.move_file(old)
        doc = self.merge_tree / "docs/production.md"
        _ = doc.write_text(f"- **Showrunner checkout:** `{old}`\n")

        command = self.migration_command("--all")
        for flags in (["--dry-run"], []):
            result = self.command(self.root, [*command, *flags])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"keep {old}: showrunner checkout awaits question 4", result.stdout)
            self.assertFalse(old.is_symlink())
            self.assertFalse(new.exists())
            if flags:
                with patch.dict(os.environ, self.environment, clear=True):
                    self.assertEqual(worktrees.read_move(record), journal)
        self.assertFalse(record.exists())

    def test_migration_dry_run_and_apply_replan_the_same_pending_source(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        stale = self.trees / "origin/fixture-unit/stale"
        new = stale.with_name("chosen")
        stale.parent.mkdir(parents=True)
        list_file = self.root / "list"
        _ = list_file.write_text(f"{old} fixture-unit chosen\n")
        move = worktrees.WorktreeMove(old, stale, "origin", "fixture-unit", "stale", worktrees.MoveOutput.LIVE_UNIT)
        with patch.dict(os.environ, self.environment, clear=True):
            journal = replace(worktrees._open_move(move), copy_tag=uuid4().hex)  # pyright: ignore[reportPrivateUsage]
            _ = shutil.copytree(old, journal.staging, symlinks=True)
            worktrees.write_move(replace(journal, destination=worktrees.FolderIdentity.read(journal.staging)))
            record = worktrees.move_file(old)
        before = record.read_bytes()
        command = self.migration_command("--list", list_file)
        dry = self.command(self.root, [*command, "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(record.read_bytes(), before)
        self.assertFalse(old.is_symlink())

        applied = self.command(self.root, command)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        rows = [line for line in dry.stdout.splitlines() if line.startswith("move ")]
        self.assertEqual(rows, [line for line in applied.stdout.splitlines() if line.startswith("move ")])
        self.assertEqual(len(rows), 1)
        self.assertIn(f"move {old} -> {new} |", rows[0])
        self.assertIn(f"kept for a manual removal list: {journal.staging}", applied.stderr)
        self.assertTrue(journal.staging.is_dir())
        self.assertEqual(old.readlink(), new)
        self.assertFalse(stale.exists())

    def test_migration_keeps_unverified_moves_and_previously_published_copies(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        missing = self.add_linked_tree(main, self.trees / "missing", "missing")
        new = self.trees / "origin/fixture-unit/source"
        new.parent.mkdir(parents=True)
        move = worktrees.WorktreeMove(old, new, "origin", "fixture-unit", "source", worktrees.MoveOutput.LIVE_UNIT)
        with patch.dict(os.environ, self.environment, clear=True):
            journal = replace(worktrees._open_move(move), copy_tag=uuid4().hex)  # pyright: ignore[reportPrivateUsage]
            _ = shutil.copytree(old, new, symlinks=True)
            worktrees.write_move(replace(journal, destination=worktrees.FolderIdentity.read(new)))
            unverified = replace(move, old=missing, new=new.with_name("missing"), short_name="missing")
            _ = worktrees._open_move(unverified)  # pyright: ignore[reportPrivateUsage]
            _ = missing.rename(missing.with_name("displaced"))
            records = {path: path.read_bytes() for path in worktrees.move_folder().glob("*.json")}
        command = self.migration_command("--all")
        for flags in (["--dry-run"], []):
            result = self.command(self.root, [*command, *flags])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"keep {old}: review the previously published copy at {new} before retry", result.stdout)
            self.assertIn(f"keep {missing}: cannot verify the source identity in the saved move", result.stdout)
        self.assertFalse(old.is_symlink())
        self.assertTrue(new.is_dir())
        for path, contents in records.items():
            self.assertEqual(path.read_bytes(), contents)

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_checks_for_new_use_before_renaming_and_replans_when_free(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        new = self.trees / "origin/fixture-unit/source"
        list_file = self.root / "list"
        _ = list_file.write_text(f"{old} fixture-unit source\n")
        open_move = worktrees._open_move  # pyright: ignore[reportPrivateUsage]
        with patch.dict(os.environ, self.environment, clear=True), ExitStack() as children:
            pids: list[int] = []

            def open_then_start_process(move: worktrees.WorktreeMove, *,
                                        clone_members: tuple[Path, ...] = ()) -> worktrees.MoveJournal:
                journal = open_move(move, clone_members=clone_members)
                child = children.enter_context(subprocess.Popen(
                    [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], cwd=old, stdin=subprocess.PIPE))
                self.register_process(child.pid)
                pids.append(child.pid)
                return journal

            with patch.object(worktrees, "_open_move", open_then_start_process), redirect_stdout(first := StringIO()):
                self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 0)
            pid, = pids
            keep = f"keep {old}: in use (pids {pid})"
            self.assertIn(keep, first.getvalue())
            self.assertFalse(new.exists())
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.PLANNED)
            for dry_run in (True, False):
                with redirect_stdout(held := StringIO()):
                    self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=dry_run), 0)
                self.assertIn(keep, held.getvalue())
                self.assertFalse(new.exists())

        with patch.dict(os.environ, self.environment, clear=True), redirect_stdout(resumed := StringIO()):
            self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 0)
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.FINISHED)
        self.assertIn(f"move {old} -> {new}", resumed.getvalue())
        self.assertEqual(resumed.getvalue().splitlines()[-1], "moved 1, kept in use 0, kept 1")
        self.assertEqual(old.readlink(), new)

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_ignores_only_the_idle_shell_of_a_stopped_pane(self) -> None:
        self.check_idle_pane_migration(("child", "non-herdr", "live", "open-file", "mapped-file", "root", "not-ended"))

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_keeps_a_pane_shell_that_execs_during_the_herdr_query(self) -> None:
        self.check_idle_pane_migration(("shell-exec",))

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_keeps_a_pid_that_is_not_the_pane_shell(self) -> None:
        self.check_idle_pane_migration(("other-shell", "unknown-shell"))

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_keeps_a_reused_pid(self) -> None:
        self.check_idle_pane_migration(("reused-pid",))

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_keeps_shells_with_missing_path_data(self) -> None:
        self.check_idle_pane_migration(("missing-root", "missing-fd", "missing-maps"))

    def check_idle_pane_migration(self, kept: tuple[str, ...]) -> None:
        self.layout_fixture()
        main = self.migration_main()
        cases = ("stopped", *kept)
        trees = {name: self.add_linked_tree(main, self.trees / name, name) for name in cases}
        list_file = self.root / "list"
        _ = list_file.write_text("".join(f"{old} fixture-unit {name}\n" for name, old in trees.items()))
        record_dir = self.root / "panes"
        record_dir.mkdir()
        registry = self.root / "sessions"
        registry.mkdir()
        self.environment.update(HERDR_PANE_RECORD_DIR=str(record_dir), NOTIFIER_SESSIONS_DIR=str(registry),
                                HERDR_PROC=str(registry))
        snapshot = staff.StaffSnapshot.decode(Path(self.environment["STAFF_FILE"]).read_text())
        owner = next(row for row in snapshot.rows if row.kind == "unit")
        live_id = staff.StaffId(str(uuid4()))
        live_owner = replace(owner, id=live_id, current_session=live_id, name="live-unit", worktree=staff.WORKTREE_UNFILLED)
        snapshot = replace(snapshot, rows=(*snapshot.rows, live_owner))
        _ = Path(self.environment["STAFF_FILE"]).write_text(snapshot.encode())
        live_pid = os.getpid()
        _ = (registry / f"{live_pid}.json").write_text(json.dumps({
            "pid": live_pid, "sessionId": live_id, "name": live_owner.name, "messagingSocketPath": "",
            "procStart": sessions.process_start(live_pid), "updatedAt": 1,
        }))
        entries: dict[str, pane_launch.PaneLaunch] = {}
        parent_processes = [(100, 1, b"herdr\0--session\0agents\0server\0"), (101, 1, b"python3\0")]
        if "child" in cases:
            parent_processes.append((999, 200 + cases.index("child"), b"python3\0"))
        for pid, ppid, argv in parent_processes:
            parent = self.proc / str(pid)
            parent.mkdir()
            _ = (parent / "status").write_text(f"State:\tS\nUid:\t{os.geteuid()}\t{os.geteuid()}\nThreads:\t1\n")
            _ = (parent / "stat").write_text(f"{pid} (fixture) S {ppid}\n")
            _ = (parent / "cmdline").write_bytes(argv)
        responses: dict[str, dict[str, object]] = {}
        for pid, (name, old) in enumerate(trees.items(), 200):
            pane = f"w1:p{pid}"
            launch = pane_launch.launch(pane, live_id if name == "live" else owner.current_session,
                                        str(old), {}, ["claude"])
            launch["ended"] = name != "not-ended"
            entries[pane] = pane_launch.retained_launch(launch, snapshot)
            process = self.proc / str(pid)
            process.mkdir()
            parent = 101 if name == "non-herdr" else 100
            _ = (process / "stat").write_text(f"{pid} (zsh) S {parent} {'0 ' * 17}{pid}\n")
            _ = (process / "comm").write_text("zsh\n")
            _ = (process / "status").write_text(f"State:\tS\nUid:\t{os.geteuid()}\t{os.geteuid()}\nThreads:\t1\n")
            _ = (process / "environ").write_bytes(f"HERDR_PANE_ID={pane}\0".encode())
            (process / "cwd").symlink_to(old)
            (process / "exe").symlink_to("/usr/bin/zsh")
            if name != "missing-root":
                (process / "root").symlink_to(old if name == "root" else "/")
            if name != "missing-fd":
                (process / "fd").mkdir()
            if name == "open-file":
                (process / "fd/7").symlink_to(old / "code.txt")
            mapped = f"00-01 r--p 0 00:1 1 {old / 'code.txt'}\n" if name == "mapped-file" else ""
            if name != "missing-maps":
                _ = (process / "maps").write_text(mapped)
            info = {} if name == "unknown-shell" else {"shell_pid": pid + 100 if name == "other-shell" else pid}
            responses[f"--session agents pane process-info --pane {pane}"] = {
                "code": 0, "stdout": json.dumps({"result": {"process_info": info}}),
            }
        _ = (record_dir / "agents.json").write_text(json.dumps(entries))
        response_file = self.root / "herdr-responses.json"
        _ = response_file.write_text(json.dumps(responses))
        self.environment.update(HERDR_BIN=str(CHECKOUT / "scripts/herdr/fake_herdr.py"),
                                FAKE_HERDR_RESPONSES=str(response_file), FAKE_HERDR_LOG=str(self.root / "herdr.log"))

        if "shell-exec" in cases:
            exec_pid = 200 + cases.index("shell-exec")
            process = self.proc / str(exec_pid)
            old = trees["shell-exec"]
            query = herdr.Herdr.result

            def exec_during_query(server: herdr.Herdr, *args: str) -> dict[str, object]:
                result = query(server, *args)
                if args == ("pane", "process-info", "--pane", f"w1:p{exec_pid}"):
                    _ = (process / "comm").write_text("python3\n")
                    (process / "exe").unlink()
                    (process / "exe").symlink_to("/usr/bin/python3")
                    (process / "fd/7").symlink_to(old / "code.txt")
                return result

            with (patch.dict(os.environ, self.environment, clear=True), redirect_stdout(report := StringIO()),
                  patch.object(herdr.Herdr, "result", exec_during_query)):
                self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=True), 0)
            self.assertIn(f"keep {old}: in use (pids {exec_pid})", report.getvalue())
            return

        if "reused-pid" in cases:
            scan = tmp_sweep.process_paths
            reused = 200 + cases.index("reused-pid")
            process_stat = self.proc / str(reused) / "stat"
            start = reused

            def reuse_pid(proc: str, prefix: str | tuple[str, ...], *,
                          require_readable_user_processes: bool = False,
                          ignored_self_fds: frozenset[int] = NO_PROBE_FDS,
                          ) -> tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable:
                nonlocal start
                result = scan(proc, prefix, require_readable_user_processes=require_readable_user_processes,
                              ignored_self_fds=ignored_self_fds)
                start += 1
                _ = process_stat.write_text(f"{reused} (zsh) S 100 {'0 ' * 17}{start}\n")
                return result

            with (patch.dict(os.environ, self.environment, clear=True), redirect_stdout(report := StringIO()),
                  patch.object(tmp_sweep, "process_paths", side_effect=reuse_pid)):
                self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=True), 0)
            self.assertIn(f"keep {trees['reused-pid']}: in use (pids {reused})", report.getvalue())
            return

        new = self.trees / "origin/fixture-unit/stopped"
        for flags in (["--dry-run"], []):
            result = self.command(self.root, self.migration_command("--list", list_file, *flags))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"move {trees['stopped']} -> {new}", result.stdout)
            for pid, name in enumerate(cases[1:], 201):
                with self.subTest(name=name, flags=flags):
                    self.assertIn(f"keep {trees[name]}: in use (pids {pid})", result.stdout)
                    self.assertFalse(trees[name].is_symlink())
        self.assertEqual(trees["stopped"].readlink(), new)
        self.assertEqual(self.git(new, "branch", "--show-current"), "stopped")

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_keeps_every_candidate_for_an_unreadable_same_user_process(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        first = self.add_linked_tree(main, self.trees / "first", "first")
        second = self.add_linked_tree(main, self.trees / "second", "second")
        for pid, uid in ((100, os.geteuid() + 1), (101, os.geteuid())):
            process = self.proc / str(pid)
            process.mkdir()
            _ = (process / "status").write_text(
                f"State:\tS\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nCapPrm:\t0000000000000000\nThreads:\t1\n")
            (process / "cwd").symlink_to(self.root)
            (process / "maps").mkdir()
        list_file = self.root / "list"
        _ = list_file.write_text(f"{first} fixture-unit first\n{second} fixture-unit second\n")
        result = self.command(self.root, self.migration_command("--all", "--list", list_file))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "moved 0, kept in use 2, kept 1")
        for path in (first, second):
            self.assertIn(f"keep {path}: in use: unreadable process 101", result.stdout)
            self.assertFalse(path.is_symlink())

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_prints_each_kernel_hidden_process_once_per_run(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        first = self.add_linked_tree(main, self.trees / "first", "first")
        second = self.add_linked_tree(main, self.trees / "second", "second")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{first} fixture-unit first\n{second} fixture-unit second\n")
        uses = tmp_sweep.ProcessUsesKnown([], kernel_hidden=(tmp_sweep.KernelHiddenProcess(17, "fixture-monitor"),))
        line = "not checked (hidden by the kernel): 17 fixture-monitor"
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees, "_migration_processes", return_value=uses) as scan):
            with redirect_stdout(dry := StringIO()):
                self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=True), 0)
            with redirect_stdout(applied := StringIO()):
                self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 0)
        self.assertEqual(scan.call_count, 4)
        self.assertEqual(dry.getvalue().splitlines().count(line), 1)
        self.assertEqual(applied.getvalue().splitlines().count(line), 1)
        self.assertEqual(applied.getvalue().splitlines()[-1], "moved 2, kept in use 0, kept 1")

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_holds_claim_and_lease_during_scan_and_rename(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.trees / "source", "source")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{old} fixture-unit source\n")
        marks = (scratch.busy_path(old), old.with_name(old.name + ".lock"))
        for mark in marks:
            mark.touch()
        scan = worktrees._migration_processes  # pyright: ignore[reportPrivateUsage]
        git = worktree_records.git
        events: list[str] = []

        def assert_held(event: str) -> None:
            for mark in marks:
                with mark.open("rb") as descriptor, self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            events.append(event)

        def scan_under_lock(paths: tuple[Path, ...], *, ignored_fds: frozenset[int] = NO_PROBE_FDS
                            ) -> tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable:
            if ignored_fds:
                assert_held("scan")
            return scan(paths, ignored_fds=ignored_fds)

        def git_under_lock(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            if arguments[:2] == ("worktree", "move"):
                assert_held("rename")
            return git(repo, *arguments, check=check)

        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees, "_migration_processes", scan_under_lock),
              patch.object(worktrees, "git", git_under_lock), redirect_stdout(StringIO())):
            self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 0)
        self.assertEqual(events, ["scan", "rename"])
        for mark in marks:
            with mark.open("rb") as descriptor:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_migration_keeps_a_checkout_at_the_build_unit_folder(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.root / "build/origin/fixture-unit", "source")
        new = self.trees / "origin/fixture-unit/source"
        list_file = self.root / "list"
        _ = list_file.write_text(f"{old} fixture-unit source\n")
        command = self.migration_command("--list", list_file)
        for flags in (["--dry-run"], []):
            result = self.command(self.root, [*command, *flags])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"keep {old}: a build unit folder; the build folder model owns it", result.stdout)
            self.assertNotIn(f"move {old} ->", result.stdout)
        self.assertFalse(old.is_symlink())
        self.assertFalse(new.exists())
        self.assertIn(f"worktree {old}", self.git(main, "worktree", "list", "--porcelain"))

    def test_migration_checks_three_build_unit_paths_with_no_config_read(self) -> None:
        self.layout_fixture()
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(conf, "read_or_repository", wraps=conf.read_or_repository) as config):
            folders = worktrees.MigrationFolders.configured()
            reads = config.call_count
            for name in ("unit-one", "unit-two", "unit-three"):
                path = folders.build_root / "origin" / name
                reason = worktrees._migration_build_keep(path, folders)  # pyright: ignore[reportPrivateUsage]
                self.assertEqual(reason, "a build unit folder; the build folder model owns it")
        self.assertEqual(config.call_count, reads)

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_discards_the_staged_copy_when_the_source_changes(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.root / "build/claude/scratch/source", "source")
        new = self.trees / "origin/fixture-unit/source"
        list_file = self.root / "list"
        _ = list_file.write_text(f"{old} fixture-unit source\n")
        scans: list[Path] = []
        journals: list[worktrees.MoveJournal] = []
        scan = worktrees._migration_processes  # pyright: ignore[reportPrivateUsage]
        open_move = worktrees._open_move  # pyright: ignore[reportPrivateUsage]

        def open_copy(move: worktrees.WorktreeMove, *, clone_members: tuple[Path, ...] = ()) -> worktrees.MoveJournal:
            journal = replace(open_move(move, clone_members=clone_members), copy_tag=uuid4().hex)
            worktrees.write_move(journal)
            journals.append(journal)
            return journal

        with patch.dict(os.environ, self.environment, clear=True):
            def change_after_copy(paths: tuple[Path, ...], *, ignored_fds: frozenset[int] = NO_PROBE_FDS
                                  ) -> tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable:
                if ignored_fds:
                    scans.append(paths[0])
                    if len(scans) == 2:
                        self.assertTrue(journals[0].staging.is_dir())
                        _ = (old / "code.txt").write_text("changed\n")
                return scan(paths, ignored_fds=ignored_fds)

            with (patch.object(worktrees, "_open_move", open_copy),
                  patch.object(worktrees, "_migration_processes", change_after_copy), redirect_stdout(kept := StringIO())):
                self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 1)
            self.assertIn(f"keep {old}: in use: changed during copy", kept.getvalue())
            self.assertEqual((old / "code.txt").read_text(), "changed\n")
            self.assertFalse(old.is_symlink())
            self.assertFalse(new.exists())
            self.assertFalse(journals[0].staging.exists())
            with redirect_stdout(StringIO()):
                self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 0)
        self.assertEqual((new / "code.txt").read_text(), "changed\n")

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_keeps_a_pending_clone_when_a_linked_checkout_is_in_use(self) -> None:
        self.layout_fixture()
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        clone = scratch_root / "clone"
        _ = self.git(self.root, "clone", str(self.origin), str(clone))
        member = self.add_linked_tree(clone, self.trees / "member", "member")
        new = self.trees / "origin/fixture-unit/clone"
        with patch.dict(os.environ, self.environment, clear=True):
            move = worktrees.WorktreeMove(clone, new, "origin", "fixture-unit", "clone", worktrees.MoveOutput.LIVE_UNIT)
            _ = worktrees._open_move(move, clone_members=(member,))  # pyright: ignore[reportPrivateUsage]
        list_file = self.root / "list"
        _ = list_file.write_text(f"{clone} fixture-unit clone\n{member} fixture-unit member\n")
        command = self.migration_command("--all", "--list", list_file)
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                              cwd=member, stdin=subprocess.PIPE, env=self.environment) as child:
            self.register_process(child.pid)
            for flags, status in ((["--dry-run"], 0), ([], 1)):
                result = self.command(self.root, [*command, *flags])
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertIn(f"keep {clone}: {clone}: {member} stays in use", result.stdout)
                self.assertIn(f"keep {member}: in use (pids {child.pid})", result.stdout)
                self.assertFalse(new.exists())
        self.assertFalse(clone.is_symlink())

    def test_migration_keeps_a_clone_with_a_nested_member_added_after_the_plan(self) -> None:
        self.layout_fixture()
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        clone = scratch_root / "clone"
        _ = self.git(self.root, "clone", str(self.origin), str(clone))
        new = self.trees / "origin/fixture-unit/clone"
        list_file = self.root / "list"
        _ = list_file.write_text(f"{clone} fixture-unit clone\n")
        member = clone / "member"
        scan = worktrees._migration_processes  # pyright: ignore[reportPrivateUsage]

        def add_member_at_final_scan(paths: tuple[Path, ...], *, ignored_fds: frozenset[int] = NO_PROBE_FDS
                                     ) -> tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable:
            if ignored_fds:
                _ = self.add_linked_tree(clone, member, "member")
            return scan(paths, ignored_fds=ignored_fds)

        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees, "_migration_processes", add_member_at_final_scan),
              redirect_stdout(kept := StringIO())):
            self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 1)
        self.assertIn(f"keep {clone}: linked checkouts changed since the plan", kept.getvalue())
        self.assertFalse(clone.is_symlink())
        self.assertFalse(new.exists())
        self.assertEqual(git_files.at(member).common_dir, clone / ".git")

    def test_migration_keeps_a_clone_when_a_finished_member_changes(self) -> None:
        self.layout_fixture()
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        unit = "fixture-unit"
        list_file = self.root / "list"
        foreign_store = git_files.at(self.repository).common_dir
        changes = ("replacement-before-planning", "git-change-before-planning", "git-change-after-planning")
        for change in changes:
            with self.subTest(change=change):
                clone = scratch_root / change
                _ = self.git(self.root, "clone", str(self.origin), str(clone))
                member_name = f"{change}-member"
                old = self.add_linked_tree(clone, scratch_root / member_name, "member")
                clone_new = self.trees / "origin" / unit / change
                with patch.dict(os.environ, self.environment, clear=True):
                    member = worktrees.move_worktree(old, "origin", unit, member_name)
                    journal = worktrees.read_move(worktrees.move_file(old))
                self.assertEqual(journal.stage, worktrees.MoveStage.FINISHED)
                _ = list_file.write_text(f"{clone} {unit} {change}\n{member} {unit} {member_name}-moved\n")
                command = self.migration_command("--all", "--list", list_file)
                registration = git_files.at(member).git_dir / "gitdir"
                registration_before = registration.read_bytes()

                if change != "git-change-after-planning":
                    if change == "replacement-before-planning":
                        original = self.root / "original-member"
                        _ = member.rename(original)
                        _ = self.add_linked_tree(self.repository, member, "replacement")
                        self.assertTrue(journal.destination.matches(original))
                        self.assertFalse(journal.destination.matches(member))
                        reason = "the finished move destination has changed"
                    else:
                        _ = (member / ".git").write_text(f"gitdir: {foreign_store}\n")
                        self.assertTrue(journal.destination.matches(member))
                        reason = f"linked worktree {member} now uses a different Git store"
                    dry = self.command(self.root, [*command, "--dry-run"])
                    applied = self.command(self.root, command)
                    self.assertEqual(dry.returncode, 0, dry.stderr)
                    self.assertEqual(applied.returncode, 1, applied.stdout + applied.stderr)
                    self.assertEqual(dry.stdout.splitlines()[:-1], applied.stdout.splitlines()[:-1])
                    self.assertIn(f"keep {member}: {reason}", dry.stdout)
                    self.assertIn(f"keep {clone}: {clone}: linked worktree {member} stays", dry.stdout)
                    self.assertNotIn(f"move {member} ->", dry.stdout)
                    self.assertNotIn(f"move {clone} ->", dry.stdout)
                else:
                    dry = self.command(self.root, [*command, "--dry-run"])
                    self.assertEqual(dry.returncode, 0, dry.stderr)
                    self.assertIn(f"move {clone} -> {clone_new} |", dry.stdout)
                    self.assertIn(f"keep {member}: already moved", dry.stdout)

                    def change_store_at_final_scan(
                        _paths: tuple[Path, ...], *, ignored_fds: frozenset[int] = NO_PROBE_FDS,
                    ) -> tmp_sweep.ProcessUsesKnown:
                        if ignored_fds:
                            _ = (member / ".git").write_text(f"gitdir: {foreign_store}\n")
                        return tmp_sweep.ProcessUsesKnown([])

                    with (patch.dict(os.environ, self.environment, clear=True),
                          patch.object(worktrees, "_migration_processes", change_store_at_final_scan),
                          redirect_stdout(kept := StringIO())):
                        self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 1)
                    self.assertIn(f"keep {clone}: linked worktree {member} now uses a different Git store",
                                  kept.getvalue())
                    self.assertTrue(journal.destination.matches(member))

                self.assertEqual(git_files.at(member).common_dir, foreign_store)
                self.assertEqual(registration.read_bytes(), registration_before)
                self.assertTrue(clone.is_dir() and not clone.is_symlink())
                self.assertFalse(clone_new.exists())

    def test_migration_moves_a_clone_with_a_missing_finished_member(self) -> None:
        self.layout_fixture()
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        clone = scratch_root / "clone"
        _ = self.git(self.root, "clone", str(self.origin), str(clone))
        old = self.add_linked_tree(clone, scratch_root / "member", "member")
        with patch.dict(os.environ, self.environment, clear=True):
            member = worktrees.move_worktree(old, "origin", "fixture-unit", "member")
            journal = worktrees.read_move(worktrees.move_file(old))
        self.assertEqual(journal.stage, worktrees.MoveStage.FINISHED)
        checkout = git_files.at(member)
        registration = checkout.git_dir / "gitdir"
        registration_before = registration.read_bytes()
        shutil.rmtree(member)
        clone_new = member.with_name("clone")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{clone} fixture-unit clone\n")
        command = self.migration_command("--all", "--list", list_file)

        dry = self.command(self.root, [*command, "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(registration.read_bytes(), registration_before)
        self.assertFalse(clone_new.exists())
        applied = self.command(self.root, command)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(dry.stdout.splitlines()[:-1], applied.stdout.splitlines()[:-1])
        self.assertIn(f"review {member}: missing registration path", applied.stdout)
        self.assertIn(f"move {clone} -> {clone_new} |", applied.stdout)
        self.assertNotIn("the finished move destination has changed", applied.stdout)
        self.assertEqual(clone.readlink(), clone_new)
        self.assertFalse(member.exists())
        self.assertIn(f"worktree {member}", self.git(clone_new, "worktree", "list", "--porcelain"))
        relocated_registration = clone_new / ".git" / registration.relative_to(checkout.common_dir)
        self.assertEqual(relocated_registration.read_bytes(), registration_before)

    def test_migration_keeps_a_clone_when_git_reads_fail(self) -> None:
        self.layout_fixture()
        config = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        is_directory = Path.is_dir
        read_registrations = worktrees.linked_trees
        read_head = git_files.head
        run_git = worktree_records.git
        failures = ("Git directory", "registration", "HEAD", "origin", "HEAD and origin", "subject")
        for has_journal_keep, failure in product((False, True), failures):
            with (self.subTest(has_journal_keep=has_journal_keep, failure=failure),
                  tempfile.TemporaryDirectory(dir=self.root) as folder):
                root = Path(folder)
                build = root / "build"
                _ = write_config_fixture(config, f"roots.workspace={root}/trees\nroots.build={build}\n"
                                         + f"roots.build.{host_name()}={build}\n")
                scratch_root = build / "claude/scratch"
                scratch_root.mkdir(parents=True)
                damaged = scratch_root / "damaged"
                healthy = scratch_root / "healthy"
                for clone in (damaged, healthy):
                    _ = self.git(self.root, "clone", str(self.origin), str(clone))
                member = self.add_linked_tree(damaged, root / "member", "member")
                missing = self.add_linked_tree(damaged, root / "missing", "missing")
                shutil.rmtree(missing)
                registration = git_files.at(member).git_dir / "gitdir"
                registration_before = registration.read_bytes()
                listed = root / "list"
                subject_checkout = member if has_journal_keep else damaged
                paths = {damaged: "damaged", healthy: "healthy", member: "member", missing: "missing"}
                if failure == "subject":
                    del paths[subject_checkout]
                _ = listed.write_text("".join(f"{path} fixture-unit {name}\n" for path, name in paths.items()))
                destination = root / "trees/origin/fixture-unit"
                journal_reason = "cannot verify the source identity in the saved move"
                journals = worktrees.MigrationJournalView((), {damaged: journal_reason} if has_journal_keep else {})
                origin_error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "origin read failed")

                def is_dir(path: Path) -> bool:
                    if path == damaged / ".git" and failure == "Git directory":
                        raise PermissionError("Git directory read failed")
                    return is_directory(path)

                def registrations(repo: Path) -> tuple[worktrees.RegisteredTree, ...]:
                    if repo == damaged and failure == "registration":
                        raise worktree_records.Refused(1, "registration read failed")
                    return read_registrations(repo)

                def head(checkout: git_files.Checkout) -> git_files.OnBranch | git_files.DetachedHead:
                    if checkout.toplevel == damaged and failure in ("HEAD", "HEAD and origin"):
                        raise OSError("HEAD read failed")
                    return read_head(checkout)

                def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
                    if (repo == damaged and failure in ("origin", "HEAD and origin")
                            and arguments == ("remote", "get-url", "origin")):
                        raise origin_error
                    if repo == subject_checkout and failure == "subject" and arguments[:1] == ("log",):
                        raise worktree_records.Refused(1, "subject read failed")
                    return run_git(repo, *arguments, check=check)

                environment = self.environment | {"WORKTREES_STATE_DIR": str(root / "state")}
                with (patch.dict(os.environ, environment, clear=True),
                      patch.object(worktrees, "_migration_journals", return_value=journals),
                      patch.object(Path, "is_dir", is_dir),
                      patch.object(worktrees, "linked_trees", side_effect=registrations),
                      patch.object(git_files, "head", side_effect=head),
                      patch.object(worktrees, "git", side_effect=git)):
                    with redirect_stdout(dry := StringIO()):
                        self.assertEqual(worktrees.migrate_worktrees(listed, scope=ALL_WORKTREES, dry_run=True), 0)
                    self.assertFalse(destination.exists())
                    with redirect_stdout(applied := StringIO()), redirect_stderr(StringIO()):
                        self.assertEqual(worktrees.migrate_worktrees(listed, scope=ALL_WORKTREES, dry_run=False), 1)

                self.assertEqual(dry.getvalue().splitlines()[:-1], applied.getvalue().splitlines()[:-1])
                read_reasons = {
                    "Git directory": "cannot read the clone's Git metadata (Git directory read failed)",
                    "registration": "cannot read the clone's Git metadata (registration read failed)",
                    "HEAD": "cannot read the clone's Git metadata (HEAD read failed)",
                    "origin": f"cannot read the clone's origin ({origin_error})",
                    "HEAD and origin": "cannot read the clone's Git metadata (HEAD read failed)",
                    "subject": "cannot read the Git commit subject (subject read failed)",
                }
                reason = journal_reason if has_journal_keep else read_reasons[failure]
                self.assertIn(f"keep {damaged}: {reason}", applied.getvalue().splitlines())
                self.assertIn(f"move {healthy} -> {destination / 'healthy'} |", applied.getvalue())
                if failure in ("Git directory", "registration"):
                    for unknown in (member, missing):
                        self.assertNotIn(f"review {unknown}:", applied.getvalue())
                else:
                    self.assertIn(f"review {missing}: missing registration path", applied.getvalue().splitlines())
                if failure == "HEAD" or (failure == "subject" and not has_journal_keep):
                    self.assertIn(f"keep {member}: {damaged}: the clone and every linked worktree need move names",
                                  applied.getvalue())
                elif failure in ("origin", "HEAD and origin"):
                    self.assertIn(f"keep {member}: the repository has no usable origin name", applied.getvalue())
                elif failure == "subject":
                    self.assertIn(f"keep {member}: {read_reasons['subject']}", applied.getvalue())
                self.assertEqual(healthy.readlink(), destination / "healthy")
                self.assertTrue(damaged.is_dir() and not damaged.is_symlink())
                self.assertTrue(member.is_dir() and not member.is_symlink())
                self.assertFalse((destination / "damaged").exists())
                self.assertFalse((destination / "member").exists())
                self.assertEqual(registration.read_bytes(), registration_before)

    def test_migration_checks_registered_members_before_journal_history(self) -> None:
        self.layout_fixture()
        config = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        foreign_store = git_files.at(self.repository).common_dir
        cases = product(("valid", "foreign", "missing"), (False, True), ("move.old", "source_key"))
        for state, history_first, source_field in cases:
            with (self.subTest(state=state, history_first=history_first, source_field=source_field),
                  tempfile.TemporaryDirectory(dir=self.root) as folder):
                root = Path(folder)
                build = root / "build"
                _ = write_config_fixture(config, f"roots.workspace={root}/trees\nroots.build={build}\n"
                                         + f"roots.build.{host_name()}={build}\n")
                scratch_root = build / "claude/scratch"
                scratch_root.mkdir(parents=True)
                clone = scratch_root / "clone"
                _ = self.git(self.root, "clone", str(self.origin), str(clone))
                old = self.add_linked_tree(clone, scratch_root / "member", "member")
                environment = self.environment | {"WORKTREES_STATE_DIR": str(root / "state")}
                with patch.dict(os.environ, environment, clear=True):
                    member = worktrees.move_worktree(old, "origin", "fixture-unit", "member")
                    current = worktrees.read_move(worktrees.move_file(old))
                self.assertEqual(current.stage, worktrees.MoveStage.FINISHED)
                other_source = root / "earlier-source"
                move_old, record_source = (member, other_source) if source_field == "move.old" else (other_source, member)
                earlier_identity = worktrees.FolderIdentity.read(clone)
                history = replace(current, source=earlier_identity, destination=earlier_identity,
                                  move=replace(current.move, old=move_old, new=member.with_name("historical")),
                                  record_source=record_source)
                self.assertNotEqual(current.destination, history.destination)
                journals = (history, current) if history_first else (current, history)

                checkout = git_files.at(member)
                registration = checkout.git_dir / "gitdir"
                registration_before = registration.read_bytes()
                if state == "foreign":
                    _ = (member / ".git").write_text(f"gitdir: {foreign_store}\n")
                    self.assertTrue(current.destination.matches(member))
                elif state == "missing":
                    shutil.rmtree(member)
                clone_new = member.with_name("clone")
                list_file = root / "list"
                _ = list_file.write_text(f"{clone} fixture-unit clone\n{member} fixture-unit moved-member\n")
                with (patch.dict(os.environ, environment, clear=True),
                      patch.object(worktrees, "_migration_journals",
                                   return_value=worktrees.MigrationJournalView(journals, {}))):
                    with redirect_stdout(dry := StringIO()):
                        self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=True), 0)
                    self.assertFalse(clone_new.exists())
                    self.assertEqual(registration.read_bytes(), registration_before)
                    with redirect_stdout(applied := StringIO()), redirect_stderr(StringIO()):
                        self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), int(state == "foreign"))

                decisions = [line for line in dry.getvalue().splitlines() if line.startswith(("move ", "keep ", "review "))]
                self.assertEqual(decisions, [line for line in applied.getvalue().splitlines()
                                             if line.startswith(("move ", "keep ", "review "))])
                if state == "foreign":
                    self.assertIn(f"keep {member}: linked worktree {member} now uses a different Git store", decisions)
                    self.assertIn(f"keep {clone}: {clone}: linked worktree {member} stays", applied.getvalue())
                    self.assertFalse(clone_new.exists())
                    self.assertEqual(registration.read_bytes(), registration_before)
                else:
                    self.assertIn(f"move {clone} -> {clone_new} |", applied.getvalue())
                    self.assertEqual(clone.readlink(), clone_new)
                    relocated_registration = clone_new / ".git" / registration.relative_to(checkout.common_dir)
                    self.assertEqual(relocated_registration.read_bytes(), registration_before)
                    if state == "missing":
                        self.assertIn(f"review {member}: missing registration path", decisions)
                        self.assertFalse(member.exists())
                    else:
                        self.assertIn(f"keep {member}: already moved", decisions)
                        self.assertEqual(git_files.at(member).common_dir, clone_new / ".git")

    @unittest.skipUnless(sys.platform == "linux", "requires the synthetic /proc scan")
    def test_migration_keeps_a_clone_when_a_member_destination_lock_is_held(self) -> None:
        self.layout_fixture()
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        clone = scratch_root / "clone"
        _ = self.git(self.root, "clone", str(self.origin), str(clone))
        member = self.add_linked_tree(clone, scratch_root / "member", "member")
        new = self.trees / "origin/fixture-unit/member"
        clone_new = new.with_name("clone")
        new.parent.mkdir(parents=True)
        list_file = self.root / "list"
        _ = list_file.write_text(f"{clone} fixture-unit clone\n{member} fixture-unit member\n")
        command = self.migration_command("--all", "--list", list_file)
        lock = new.with_name(new.name + ".lock")
        with lock.open("wb") as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            dry = self.command(self.root, [*command, "--dry-run"])
            applied = self.command(self.root, command)
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(applied.returncode, 1, applied.stdout + applied.stderr)
            self.assertEqual(dry.stdout.splitlines()[:-1], applied.stdout.splitlines()[:-1])
            self.assertIn(f"keep {member}: kept: {lock} held", dry.stdout)
            self.assertIn(f"keep {clone}: {clone}: {member} stays in use", dry.stdout)
            self.assertNotIn(f"move {member} ->", dry.stdout)
            self.assertNotIn(f"move {clone} ->", dry.stdout)
            self.assertTrue(clone.is_dir() and not clone.is_symlink())
            self.assertTrue(member.is_dir() and not member.is_symlink())
            self.assertFalse(new.exists())
            self.assertFalse(clone_new.exists())

        applied = self.command(self.root, command)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(member.readlink(), new)
        self.assertEqual(clone.readlink(), clone_new)
        self.assertEqual(git_files.at(new).common_dir, clone_new / ".git")

    def test_migration_publishes_a_staged_copy_after_the_source_reaches_backup(self) -> None:
        self.layout_fixture()
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        old = scratch_root / "clone"
        _ = self.git(self.root, "clone", str(self.origin), str(old))
        new = self.trees / "origin/fixture-unit/clone"
        new.parent.mkdir(parents=True)
        move = worktrees.WorktreeMove(old, new, "origin", "fixture-unit", "clone", worktrees.MoveOutput.LIVE_UNIT)
        with patch.dict(os.environ, self.environment, clear=True):
            journal = replace(worktrees._open_move(move), copy_tag=uuid4().hex)  # pyright: ignore[reportPrivateUsage]
            _ = shutil.copytree(old, journal.staging, symlinks=True)
            worktrees.write_move(replace(journal, destination=worktrees.FolderIdentity.read(journal.staging)))
            _ = old.rename(journal.backup)
        doc = self.merge_tree / "docs/production.md"
        _ = doc.write_text(f"- **Showrunner checkout:** `{old}`\n")

        result = self.command(self.root, self.migration_command("--all"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(old.readlink(), new)
        self.assertTrue(journal.backup.is_dir())
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.FINISHED)

    def test_full_migration_names_the_scratch_backup_for_a_blocked_move(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.root / "build/claude/scratch/legacy", "legacy")
        new = self.trees / "origin/fixture-unit/legacy"
        new.parent.mkdir(parents=True)
        move = worktrees.WorktreeMove(old, new, "origin", "fixture-unit", "legacy", worktrees.MoveOutput.LIVE_UNIT)
        with patch.dict(os.environ, self.environment, clear=True):
            journal = replace(worktrees._open_move(move), copy_tag=uuid4().hex)  # pyright: ignore[reportPrivateUsage]
            _ = shutil.copytree(old, journal.staging, symlinks=True)
            worktrees.write_move(replace(journal, destination=worktrees.FolderIdentity.read(journal.staging)))
            _ = old.rename(journal.backup)
        new.mkdir()

        command = self.migration_command("--all")
        for blocking in (self.migration_command("--repo", "origin"), command):
            with self.subTest(blocking=blocking):
                blocked = self.command(self.root, blocking)
                self.assertEqual(blocked.returncode, 1, blocked.stdout + blocked.stderr)
                self.assertIn(str(journal.backup), blocked.stderr)
                self.assertTrue(journal.source.matches(journal.backup))

        new.rmdir()
        applied = self.command(self.root, command)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(old.readlink(), new)
        self.assertTrue(journal.source.matches(journal.backup))

    def test_migration_models_a_linked_registration_repair_in_both_modes(self) -> None:
        self.layout_fixture()
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        clone = scratch_root / "clone"
        _ = self.git(self.root, "clone", str(self.origin), str(clone))
        old = self.add_linked_tree(clone, scratch_root / "member", "member")
        new = self.trees / "origin/fixture-unit/member"
        new.parent.mkdir(parents=True)
        clone_new = new.with_name("clone")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{clone} fixture-unit clone\n{old} fixture-unit member\n")
        move = worktrees.WorktreeMove(old, new, "origin", "fixture-unit", "member", worktrees.MoveOutput.LIVE_UNIT)
        gitdir = git_files.at(old).git_dir / "gitdir"
        back_pointer = gitdir.read_bytes()
        with patch.dict(os.environ, self.environment, clear=True):
            _ = worktrees._open_move(move)  # pyright: ignore[reportPrivateUsage]
            _ = old.rename(new)

        command = self.migration_command("--all", "--list", list_file)
        dry = self.command(self.root, [*command, "--dry-run"])
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(gitdir.read_bytes(), back_pointer)
        self.assertFalse(os.path.lexists(old))

        applied = self.command(self.root, command)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(dry.stdout.splitlines()[:-1], applied.stdout.splitlines()[:-1])
        self.assertIn(f"move {clone} -> {clone_new} |", dry.stdout)
        self.assertIn(f"keep {new}: already moved", dry.stdout)
        self.assertEqual(old.readlink(), new)
        self.assertEqual(clone.readlink(), clone_new)
        self.assertEqual(git_files.at(new).common_dir, clone_new / ".git")

    def stage_linked_migration_member(self) -> tuple[Path, Path, worktrees.MoveJournal]:
        self.layout_fixture()
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        clone = scratch_root / "clone"
        _ = self.git(self.root, "clone", str(self.origin), str(clone))
        old = self.add_linked_tree(clone, scratch_root / "member", "member")
        new = self.trees / "origin/fixture-unit/member"
        new.parent.mkdir(parents=True)
        list_file = self.root / "list"
        _ = list_file.write_text(f"{clone} fixture-unit clone\n{old} fixture-unit member\n")
        move = worktrees.WorktreeMove(old, new, "origin", "fixture-unit", "member", worktrees.MoveOutput.LIVE_UNIT)
        with patch.dict(os.environ, self.environment, clear=True):
            journal = replace(worktrees._open_move(move), copy_tag=uuid4().hex)  # pyright: ignore[reportPrivateUsage]
            _ = shutil.copytree(old, journal.staging, symlinks=True)
            journal = replace(journal, destination=worktrees.FolderIdentity.read(journal.staging))
            worktrees.write_move(journal)
            _ = old.rename(journal.backup)
        return clone, list_file, journal

    def test_migration_uses_the_unfinished_journal_in_both_orders(self) -> None:
        clone, list_file, journal = self.stage_linked_migration_member()
        old, new = journal.move.old, journal.move.new
        history = replace(journal, move=replace(journal.move, old=old.with_name("historical-member")),
                          destination=journal.source, stage=worktrees.MoveStage.FINISHED)
        self.assertNotEqual(history.destination, journal.destination)
        decisions: list[list[str]] = []
        with patch.dict(os.environ, self.environment, clear=True):
            for journals in ((journal, history), (history, journal)):
                with (patch.object(worktrees, "_migration_journals",
                                   return_value=worktrees.MigrationJournalView(journals, {})),
                      redirect_stdout(output := StringIO())):
                    self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=True), 0)
                decisions.append(output.getvalue().splitlines()[:-1])
                self.assertIn(f"resume {old} -> {new} |", output.getvalue())
                self.assertIn(f"move {clone} -> {new.with_name('clone')} |", output.getvalue())
                self.assertTrue(journal.destination.matches(journal.staging))
                self.assertFalse(new.exists())
            self.assertEqual(decisions[0], decisions[1])

            duplicate = replace(history, stage=worktrees.MoveStage.PLANNED)
            with (patch.object(worktrees, "_migration_journals",
                               return_value=worktrees.MigrationJournalView((journal, duplicate), {})),
                  self.assertRaisesRegex(ValueError, "multiple unfinished move journals")):
                _ = worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=True)

            with (patch.object(worktrees, "_migration_journals",
                               return_value=worktrees.MigrationJournalView((journal, history), {})),
                  redirect_stdout(output := StringIO())):
                self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 0)
            self.assertEqual(output.getvalue().splitlines()[:-1], decisions[0])
            self.assertTrue(journal.destination.matches(new))

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_keeps_a_clone_with_a_process_in_a_staged_linked_checkout(self) -> None:
        clone, list_file, journal = self.stage_linked_migration_member()
        old, new = journal.move.old, journal.move.new
        command = self.migration_command("--all", "--list", list_file)
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                              cwd=journal.staging, stdin=subprocess.PIPE, env=self.environment) as child:
            self.register_process(child.pid)
            dry = self.command(self.root, [*command, "--dry-run"])
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertTrue(journal.staging.is_dir())
            applied = self.command(self.root, command)
            self.assertEqual(applied.returncode, 1, applied.stderr)
        for result in (dry, applied):
            self.assertIn(f"keep {clone}: {clone}: {new} stays in use", result.stdout)
            self.assertNotIn(f"move {clone} ->", result.stdout)
        self.assertEqual(dry.stdout.splitlines()[:-1], applied.stdout.splitlines()[:-1])
        self.assertFalse(clone.is_symlink())
        self.assertEqual(old.readlink(), new)

    def check_staged_member_lock(self, *, use_removal_lease: bool) -> None:
        clone, list_file, journal = self.stage_linked_migration_member()
        stage, new = journal.staging, journal.move.new
        lock = stage.with_name(stage.name + ".lock") if use_removal_lease else scratch.busy_path(stage)
        with patch.dict(os.environ, self.environment, clear=True):
            journal_path = worktrees.move_file(journal.source_key)
        journal_before = journal_path.read_bytes()
        registration = git_files.at(stage).git_dir / "gitdir"
        git_before = registration.read_bytes()
        command = self.migration_command("--all", "--list", list_file)

        def move_and_keep_rows(result: subprocess.CompletedProcess[str]) -> list[str]:
            return [line for line in result.stdout.splitlines() if line.startswith(("move ", "keep "))]

        with lock.open("wb") as descriptor:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            dry = self.command(self.root, [*command, "--dry-run"])
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(journal_path.read_bytes(), journal_before)
            self.assertEqual(registration.read_bytes(), git_before)
            self.assertTrue(journal.destination.matches(stage))
            self.assertFalse(new.exists())
            applied = self.command(self.root, command)
            self.assertEqual(applied.returncode, 1, applied.stdout + applied.stderr)
            self.assertEqual(move_and_keep_rows(dry), move_and_keep_rows(applied))
            for result in (dry, applied):
                self.assertIn(f"keep {clone}: {clone}: {new} stays in use (kept: {lock} held)", result.stdout)
                self.assertNotIn(f"move {clone} ->", result.stdout)
            self.assertFalse(clone.is_symlink())

        dry = self.command(self.root, [*command, "--dry-run"])
        applied = self.command(self.root, command)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(move_and_keep_rows(dry), move_and_keep_rows(applied))
        clone_new = new.with_name("clone")
        self.assertIn(f"move {clone} -> {clone_new} |", applied.stdout)
        self.assertEqual(clone.readlink(), clone_new)
        self.assertEqual(git_files.at(new).common_dir, clone_new / ".git")

    @unittest.skipUnless(sys.platform == "linux", "requires the synthetic /proc scan")
    def test_migration_keeps_a_clone_with_a_held_busy_claim_in_a_staged_linked_checkout(self) -> None:
        self.check_staged_member_lock(use_removal_lease=False)

    @unittest.skipUnless(sys.platform == "linux", "requires the synthetic /proc scan")
    def test_migration_keeps_a_clone_with_a_held_removal_lease_in_a_staged_linked_checkout(self) -> None:
        self.check_staged_member_lock(use_removal_lease=True)

    def test_migration_keeps_a_clone_when_its_staged_member_cannot_be_verified(self) -> None:
        clone, list_file, journal = self.stage_linked_migration_member()
        _ = journal.staging.rename(journal.staging.with_name("unrecorded-location"))
        command = self.migration_command("--all", "--list", list_file)
        dry = self.command(self.root, [*command, "--dry-run"])
        applied = self.command(self.root, command)
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(applied.returncode, 1, applied.stdout + applied.stderr)
        self.assertEqual(dry.stdout.splitlines()[:-1], applied.stdout.splitlines()[:-1])
        for result in (dry, applied):
            self.assertIn(f"keep {clone}:", result.stdout)
            self.assertIn(f"cannot verify the saved checkout at {journal.staging}", result.stdout)
            self.assertNotIn("resume ", result.stdout)
            self.assertNotIn(f"move {clone} ->", result.stdout)
        self.assertFalse(journal.move.new.exists())

    def test_migration_recovers_a_renamed_clone_with_a_nested_member(self) -> None:
        self.layout_fixture()
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        old = scratch_root / "clone"
        _ = self.git(self.root, "clone", str(self.origin), str(old))
        member = self.add_linked_tree(old, old / "member", "member")
        new = self.trees / "origin/fixture-unit/clone"
        new.parent.mkdir(parents=True)
        move = worktrees.WorktreeMove(old, new, "origin", "fixture-unit", "clone", worktrees.MoveOutput.LIVE_UNIT)
        with patch.dict(os.environ, self.environment, clear=True):
            _ = worktrees._open_move(move, clone_members=(member,))  # pyright: ignore[reportPrivateUsage]
            worktrees.write_record(worktrees.new_record(member, "member", old, self.base, "", ""))
            _ = old.rename(new)

        result = self.command(self.root, self.migration_command("--all"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(old.readlink(), new)
        relocated = new / "member"
        self.assertEqual(git_files.at(relocated).common_dir, new / ".git")
        self.assertIn(f"worktree {relocated}", self.git(new, "worktree", "list", "--porcelain"))
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.FINISHED)
            record = worktree_records.read_record(worktree_records.record_file(relocated))
            self.assertEqual(record.path, str(relocated))
            self.assertEqual(record.repo, str(new))
            self.assertFalse(worktree_records.record_file(member).exists())

    def test_migration_moves_a_detached_clone_with_a_detached_member(self) -> None:
        self.layout_fixture()
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        clone = scratch_root / "clone"
        _ = self.git(self.root, "clone", str(self.origin), str(clone))
        member = self.add_linked_tree(clone, clone / "member", "member")
        for tree in (clone, member):
            _ = self.git(tree, "switch", "--detach")
        new = self.trees / "origin/fixture-unit/clone"
        relocated = new.with_name("member")
        list_file = self.root / "list"
        _ = list_file.write_text(f"{clone} fixture-unit clone\n{member} fixture-unit member\n")
        with patch.dict(os.environ, self.environment, clear=True):
            prior = worktrees.new_record(member, "member", clone, self.base, "", "")
            worktrees.write_record(prior)
        result = self.command(self.root, self.migration_command("--all", "--list", list_file))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(clone.readlink(), new)
        self.assertEqual(member.readlink(), relocated)
        self.assertEqual(git_files.at(relocated).common_dir, new / ".git")
        for tree in (new, relocated):
            self.assertEqual(git_files.current_branch(tree), "")
        with patch.dict(os.environ, self.environment, clear=True):
            layout = worktrees.output_record("origin", "fixture-unit", "member")
            self.assertEqual(worktree_records.checkout_record(relocated),
                             replace(prior, path=str(relocated), repo=str(new), layout=layout))
            self.assertIsNone(worktree_records.checkout_record(member))

    def test_migration_moves_scratch_clone_and_repairs_its_linked_worktrees(self) -> None:
        self.layout_fixture()
        self.migration_unit("lfo")
        config = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        with config.open("a") as handle:
            _ = handle.write("repo_word.origin=claude\n")
        scratch_root = self.root / "build/claude/scratch"
        scratch_root.mkdir(parents=True)
        archived = scratch_root / "crprof-8a8cbf"
        _ = self.git(scratch_root, "init", str(archived))
        _ = (archived / "notes").write_text("retain with the old build root")
        clone = scratch_root / "lfo-reach-row-88b16c"
        _ = self.git(self.root, "clone", str(self.origin), str(clone))
        first = self.add_linked_tree(clone, scratch_root / "lfo-float-delay", "float")
        second = self.add_linked_tree(clone, scratch_root / "lfo-open-close", "open")
        for tree in (first, second):
            _ = (git_files.at(tree).git_dir / "cargo-berth-worktree-id").write_text(tree.name)
        list_file = self.root / "list"
        _ = list_file.write_text(f"{clone} lfo reach\n{first} lfo float\n{second} lfo open\n")
        new = self.trees / "claude/lfo"
        open_move = worktrees._open_move   # pyright: ignore[reportPrivateUsage]

        def copy_move(move: worktrees.WorktreeMove, *, clone_members: tuple[Path, ...] = ()) -> worktrees.MoveJournal:
            journal = replace(open_move(move, clone_members=clone_members), copy_tag=uuid4().hex)
            worktrees.write_move(journal)
            return journal

        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees, "_open_move", copy_move),
              redirect_stdout(dry_run := StringIO())):
            self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=True), 0)
            self.assertIn(f"keep {archived}: no origin; moves with the build root to old-claude", dry_run.getvalue())
            self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 0)
            self.assertEqual(clone.readlink(), new / "reach")
            self.assertFalse((new / "reach/target").is_symlink())
            self.assertTrue(any(scratch_root.glob(".worktree-move-*")))
            for old, short in ((first, "float"), (second, "open")):
                self.assertEqual(old.readlink(), new / short)
                repaired = git_files.at(new / short)
                self.assertEqual(repaired.common_dir, new / "reach/.git")
                self.assertEqual((repaired.git_dir / "cargo-berth-worktree-id").read_text(), old.name)
            archive = self.root / "build/old-claude"
            _ = scratch_root.parent.rename(archive)
            self.assertEqual((archive / "scratch" / archived.name / "notes").read_text(),
                             "retain with the old build root")
            self.assertEqual(worktrees.migrate_worktrees(list_file, scope=ALL_WORKTREES, dry_run=False), 0)
            for short in ("reach", "float", "open"):
                self.assertEqual((new / short / "target").readlink(), self.root / "build/claude/lfo" / short)

    def test_make_uses_the_exact_main_checkout_for_the_repo_word(self) -> None:
        self.layout_fixture()
        config = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        with config.open("a") as handle:
            _ = handle.write("repo_word.claude_commands=claude\n")
        main = self.migration_main()
        sibling = main.with_name("origin-version")
        _ = self.git(self.root, "clone", str(self.origin), str(sibling))
        claude = self.home / ".claude"
        _ = self.git(claude, "init", "-b", "main")
        _ = self.git(claude, "commit", "--allow-empty", "-m", "Fixture main")
        _ = self.git(claude, "remote", "add", "origin", "ssh://fixture/claude_commands.git")
        for word, reason in (("missing", "no main checkout"), (sibling.name, "differs from origin"),
                             ("claude_commands", "no main checkout")):
            result = self.command(self.root, [sys.executable, str(WORKTREES), "make", word, "demo", "probe"])
            self.assertEqual(result.returncode, 1)
            self.assertIn(reason, result.stderr)
        for repo, source in (("origin", main), ("claude", self.home / ".claude")):
            result = self.command(self.root, [sys.executable, str(WORKTREES), "make", repo, "demo", "probe"])
            self.assertEqual(result.returncode, 0, result.stderr)
            target = self.trees / repo / "demo/probe"
            self.assertEqual(Path(result.stdout.strip()), target)
            self.assertEqual(git_files.at(target).common_dir, git_files.at(source).common_dir)
        migration = self.command(self.root, [*self.migration_command("--all"), "--dry-run"])
        self.assertEqual(migration.returncode, 0, migration.stderr)
        self.assertIn(f"main output {main} -> {self.root}/build/origin/main", migration.stdout)
        self.assertIn(f"keep {sibling}: keeps its current output; {main} is the main checkout for origin",
                      migration.stdout)
        self.assertNotIn(f"main output {sibling} ->", migration.stdout)

    def test_migration_uses_origin_alias_and_strips_legacy_names(self) -> None:
        self.layout_fixture()
        self.migration_unit("statusline-role")
        self.migration_unit("former-foreign")
        config = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        with config.open("a") as handle:
            _ = handle.write("repo_word.claude_commands=claude\n")
        main = self.migration_main()
        _ = self.git(self.repository, "remote", "set-url", "origin", "git@fixture:owner/claude_commands.git")
        _ = self.git(main, "remote", "set-url", "origin", "git@fixture:owner/claude_commands.git")
        names = (
            ("claude_commands-Nates-MacBook-Pro-2023-fixture-unit", "init/claude-fixture-unit", "worktree"),
            ("claude-natedev-fixture-unit-aws", "fixture-unit-other", "aws"),
            ("claude-fixture-unit-25", "init/claude_commands-Nates-MacBook-Pro-2023-fixture-unit-phase-25", "phase-25"),
            ("claude-old-fixture-unit-review", "old-fixture-unit-review", "review"),
        )
        for old, branch, _ in names:
            _ = self.add_linked_tree(main, self.trees / old, branch)
        production = self.add_linked_tree(main, self.trees / "claude-fixture-unit-showrunner", "integration-copy")
        doc = self.merge_tree / "docs/production.md"
        _ = doc.write_text(f"- **Showrunner checkout:** `{production}`\n- **Formerly:** `old-fixture-unit`\n")
        harness = self.add_linked_tree(main, main / ".claude/worktrees/fixture-unit", "harness")
        gate = self.add_linked_tree(main, self.root / "build/claude/scratch/gate-fixture-unit", "gate")
        repo_unit = self.add_linked_tree(main, self.trees / "origin-other", "repo-unit")
        _ = self.git(repo_unit, "commit", "--allow-empty", "-m", "checkpoint(claude): save work")
        status = self.add_linked_tree(main, self.trees / "claude-legacy-status", "statusline-role")
        _ = self.git(status, "commit", "--allow-empty", "-m", "checkpoint(claude-old-fixture-unit-statusline-role): save")
        foreign = main.with_name("foreign")
        _ = self.git(self.root, "clone", str(self.origin), str(foreign))
        _ = self.git(foreign, "remote", "set-url", "origin", "git@fixture:owner/foreign.git")
        unknown = self.add_linked_tree(foreign, self.trees / "foreign-fixture-unit-probe", "probe")
        _ = self.git(unknown, "commit", "--allow-empty", "-m", "checkpoint(old-fixture-unit-fixture-unit): save")
        fallback = self.add_linked_tree(foreign, self.trees / "foreign-fixture-unit-example", "example")
        _ = self.git(fallback, "commit", "--allow-empty", "-m", "shrink(old-fixture-unit-former-foreign): save")
        wrapped = self.add_linked_tree(main, self.trees / "wrapped-fixture-unit", "wrapped")
        archived = self.home / "rust/hanadocs/archive/vault/wrapped/wrapped-production.md"
        archived.parent.mkdir(parents=True)
        _ = archived.write_text(f"- **Showrunner checkout:** `{wrapped}`\n")
        result = self.command(self.root, [*self.migration_command("--all"), "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        for old, _, short in names:
            self.assertIn(f"move {self.trees / old} -> {self.trees}/claude/fixture-unit/{short} |", result.stdout)
        self.assertIn(f"keep {production}: showrunner checkout awaits question 4", result.stdout)
        self.assertIn(f"keep {harness}: Claude Code owns this worktree", result.stdout)
        self.assertIn(f"keep {gate}: the maker owns this worktree", result.stdout)
        self.assertIn(f"keep {wrapped}: showrunner checkout awaits question 4; archived production wrapped",
                      result.stdout)
        self.assertIn(f"keep {repo_unit}: unit unknown", result.stdout)
        self.assertIn(f"move {status} -> {self.trees}/claude/statusline-role/legacy-status | statusline-role | legacy-status "
                      + "| commit subject | retired-unit", result.stdout)
        self.assertIn(f"keep {unknown}: unit unknown", result.stdout)
        self.assertIn(f"move {fallback} -> {self.trees}/foreign/former-foreign/fixture-unit-example | former-foreign "
                      + "| fixture-unit-example | commit subject | retired-unit", result.stdout)
        self.assertNotIn(f"{self.trees}/claude_commands/", result.stdout)

    def test_migration_keeps_claude_code_worktrees_through_parent_links(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        aliases = (
            (self.trees / ".claude/worktrees", self.trees / "plain", "registered"),
            (self.trees / "alias", self.root / "protected/.claude/worktrees", "resolved"),
        )
        listed: dict[Path, worktrees.ListedWorktree] = {}
        for registered_parent, physical_parent, name in aliases:
            physical = self.add_linked_tree(main, physical_parent / name, name)
            registered_parent.parent.mkdir(parents=True, exist_ok=True)
            registered_parent.symlink_to(physical_parent, target_is_directory=True)
            registered = registered_parent / name
            _ = (git_files.at(physical).git_dir / "gitdir").write_text(f"{registered}/.git\n")
            listed[registered] = worktrees.ListedWorktree(worktrees.StaffUnitName("fixture-unit"), name)
            self.assertEqual(registered.resolve(strict=True), physical)
            self.assertIn(f"worktree {registered}", self.git(main, "worktree", "list", "--porcelain"))
        with patch.dict(os.environ, self.environment, clear=True):
            plan = worktrees.plan_worktree_migration(
                worktrees.MigrationSelection(ALL_WORKTREES, listed), worktrees.staff_record(),
                worktrees.MigrationFolders.configured(),
                worktrees.MigrationJournalView((), {}), worktrees.MigrationLocations({}))
        self.assertEqual(plan.moves, ())
        for registered in listed:
            self.assertEqual(plan.skipped[registered], "Claude Code owns this worktree")

    def test_layout_make_records_names_and_lists_output_with_the_source(self) -> None:
        self.layout_fixture()
        tree = self.make_layout("demo", "probe")
        self.assertEqual(tree, self.trees / "origin/demo/probe")
        self.assertEqual(self.git(tree, "branch", "--show-current"), "demo-probe")
        output = self.root / "build/origin/demo/probe"
        self.assertEqual((tree / "target").readlink(), output)
        saved = cast(dict[str, object], json.loads(self.record_path(tree).read_text()))
        self.assertEqual((saved["unit"], saved["short_name"], saved["output"]), ("demo", "probe", str(output)))
        self.assertEqual((saved["owner"], saved["merge_branch"]), ("fixture-unit", "integration"))
        _ = (output / "artifact").write_text("output")
        with output.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            busy = self.command(self.home / "rust/origin", [sys.executable, str(WORKTREES), "remove", str(tree)])
            self.assertEqual(busy.returncode, 0, busy.stderr)
            self.assertTrue(tree.is_dir())
            self.assertTrue(output.is_dir())
        gone = self.command(self.home / "rust/origin", [sys.executable, str(WORKTREES), "remove", str(tree)])
        self.assertEqual(gone.returncode, 0, gone.stderr)
        self.assertTrue(tree.is_dir())
        self.assertEqual((output / "artifact").read_text(), "output")
        for path in (tree, output):
            self.assertIn(f"kept for a manual removal list: {path}", gone.stderr)

    def test_a_try_folder_is_a_named_worktree_and_removal_keeps_unsaved_work(self) -> None:
        self.layout_fixture()
        tree = self.make_layout("demo", "try-C12")
        self.assertEqual(tree, self.trees / "origin/demo/try-C12")
        self.assertEqual(self.git(tree, "branch", "--show-current"), "demo-try-C12")
        self.assertEqual((tree / "target").readlink(), self.root / "build/origin/demo/try-C12")
        other = self.make_layout("other", "try-C12")
        _ = (other / "notes.txt").write_text("work")
        python = self.home / ".claude/scripts/lib/py"
        code = "from scripts.worktree.worktrees import remove_named; print(remove_named('demo', 'try-C12'))"
        removed = self.command(self.repository, [str(python), "-c", code])
        self.assertEqual((removed.returncode, removed.stdout), (0, "[]\n"), removed.stderr)
        self.assertIn(f"kept for a manual removal list: {tree} (worktrees, the checkout is selected for removal)",
                      removed.stderr)
        self.assertNotIn(str(other), removed.stderr)
        code = "from scripts.worktree.worktrees import remove_named; print(*remove_named('other', 'try-C12'))"
        kept = self.command(self.repository, [str(python), "-c", code])
        self.assertEqual(kept.returncode, 0, kept.stderr)
        self.assertTrue(kept.stdout.startswith(f"{other} kept: dirty worktree: ?? notes.txt. Save the work, then run: "))
        self.assertTrue(kept.stdout.endswith(f"worktrees.py remove {other}\n"))
        self.assertNotIn("kept for a manual removal list", kept.stderr)

    def test_make_carries_direnv_permission_only_for_the_allowed_source_file(self) -> None:
        self.layout_fixture()
        source = self.home / "rust/origin"
        source.parent.mkdir()
        _ = self.git(self.root, "clone", str(self.repository), str(source))
        _ = self.git(source, "remote", "set-url", "origin", str(self.origin))
        source_rc = source / ".envrc"
        original = b"export FIXTURE=tracked\n"
        _ = source_rc.write_bytes(original)
        target_rc = source / "fixture.envrc"
        _ = target_rc.write_bytes(original)
        _ = self.git(source, "add", source_rc.name, target_rc.name)
        _ = self.git(source, "commit", "-m", "Add the fixture environment")

        log = self.root / "direnv.log"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        direnv = bin_dir / "direnv"
        link_target = self.root / "nested"
        link_target.mkdir()
        (self.repository / "bin-link").symlink_to(link_target, target_is_directory=True)
        _ = direnv.write_text(rf"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
with Path(os.environ["DIRENV_TEST_LOG"]).open("a") as log:
    log.write(json.dumps([os.getcwd(), *args]) + "\n")
if args == ["status", "--json"]:
    path = os.environ.get("DIRENV_TEST_PATH", str(Path.cwd() / ".envrc"))
    print(json.dumps({{"state": {{"foundRC": {{"path": path, "allowed": int(os.environ["DIRENV_TEST_STATUS"])}}}}}}))
    raise SystemExit(0)
if len(args) == 2 and args[0] == "allow":
    if os.environ["DIRENV_TEST_CHANGE_ON_ALLOW"] == "1":
        (Path(args[1]) / ".envrc").write_text("export FIXTURE=changed_during_allow\n")
    raise SystemExit(int(os.environ["DIRENV_TEST_ALLOW_EXIT"]))
if len(args) == 2 and args[0] == "deny":
    raise SystemExit(0)
raise SystemExit(2)
""")
        direnv.chmod(0o755)
        original_path = self.environment["PATH"]
        self.environment["DIRENV_TEST_LOG"] = str(log)

        for name, expected_commands, warning in (
            ("equal", ["status", "allow"], False),
            ("relative-path", ["status", "allow"], False),
            ("symlink-path", ["status", "allow"], False),
            ("different", [], True),
            ("unallowed", ["status"], True),
            ("other-file", ["status"], True),
            ("allow-failed", ["status", "allow"], True),
            ("changed-during-allow", ["status", "allow", "deny"], True),
            ("source-symlink", [], True),
            ("checkout-symlink", [], True),
            ("absent", [], False),
        ):
            with self.subTest(name=name):
                match name:
                    case "relative-path":
                        fake_path = os.path.relpath(bin_dir, self.repository)
                    case "symlink-path":
                        fake_path = "bin-link/../bin"
                    case _:
                        fake_path = str(bin_dir)
                self.environment["PATH"] = fake_path + os.pathsep + original_path
                self.environment["DIRENV_TEST_STATUS"] = "1" if name == "unallowed" else "0"
                self.environment["DIRENV_TEST_ALLOW_EXIT"] = "1" if name == "allow-failed" else "0"
                self.environment["DIRENV_TEST_CHANGE_ON_ALLOW"] = "1" if name == "changed-during-allow" else "0"
                found = source.parent / ".envrc" if name == "other-file" else source_rc
                self.environment["DIRENV_TEST_PATH"] = str(found)
                if name == "absent":
                    _ = self.git(source, "checkout", "--detach", self.base)
                elif name in {"source-symlink", "checkout-symlink"}:
                    source_rc.unlink()
                    source_rc.symlink_to(target_rc.name)
                    if name == "checkout-symlink":
                        _ = self.git(source, "commit", "-am", "Link the fixture environment")
                        source_rc.unlink()
                        _ = source_rc.write_bytes(original)
                else:
                    _ = source_rc.write_bytes(b"export FIXTURE=modified\n" if name == "different" else original)
                _ = log.write_text("")
                command = [sys.executable, "-B", str(WORKTREES), "make", "origin", "demo", name]
                result = self.command(self.repository, command)
                if name in {"source-symlink", "checkout-symlink"}:
                    _ = self.git(source, "restore", source_rc.name)
                tree = self.trees / "origin/demo" / name
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(tree))
                notice = f"make: .envrc not allowed. Run direnv allow {shlex.quote(str(tree))}"
                notices = [line for line in result.stderr.splitlines() if line.startswith("make: .envrc")]
                self.assertEqual(notices, [notice] if warning else [])
                calls = {"status": [str(source), "status", "--json"],
                         "allow": [str(self.repository), "allow", str(tree)],
                         "deny": [str(self.repository), "deny", str(tree)]}
                self.assertEqual([json.loads(line) for line in log.read_text().splitlines()],
                                 [calls[command] for command in expected_commands])
                if name == "equal":
                    _ = log.write_text("")
                    repeated = self.command(self.repository, command)
                    self.assertEqual(repeated.returncode, 0, repeated.stderr)
                    self.assertEqual(repeated.stdout.strip(), str(tree))
                    self.assertEqual(log.read_text(), "")

    def test_make_assigns_output_to_a_legacy_checkout_on_its_recorded_branch(self) -> None:
        self.layout_fixture()
        tree = self.make_layout("demo", "probe")
        path = self.record_path(tree)
        saved = worktree_records.read_record(path)
        _ = self.git(tree, "branch", "-m", "legacy-branch")
        legacy = replace(saved, branch="legacy-branch", layout=worktree_records.UnassignedOutput())
        (tree / "target").unlink()
        with patch.dict(os.environ, self.environment, clear=True):
            worktrees.write_record(legacy)

        self.assertEqual(self.make_layout("demo", "probe"), tree)

        self.assertEqual(worktree_records.read_record(path), replace(legacy, layout=saved.layout))
        self.assertEqual(self.git(tree, "branch", "--show-current"), "legacy-branch")
        self.assertEqual((tree / "target").readlink(), self.root / "build/origin/demo/probe")

    def test_make_refreshes_a_migrated_owner_and_empty_merge_branch(self) -> None:
        self.layout_fixture()
        tree = self.make_layout("demo", "probe")
        path = self.record_path(tree)
        current = worktree_records.read_record(path)
        with patch.dict(os.environ, self.environment, clear=True):
            worktrees.write_record(replace(current, owner=str(uuid4()), merge_branch=""))

        self.assertEqual(self.make_layout("demo", "probe"), tree)

        self.assertEqual(worktree_records.read_record(path), current)

    def test_make_refreshes_caller_fields_and_output_from_another_directory(self) -> None:
        self.layout_fixture()
        tree = self.make_layout("demo", "probe")
        path = self.record_path(tree)
        original = worktree_records.read_record(path)
        assert isinstance(original.layout, worktree_records.RecordedWorktreeOutput)
        old_output = self.root / "old-output/origin/demo/probe"
        old_output.mkdir(parents=True)
        _ = (old_output / "artifact").write_text("old output")
        target = tree / "target"
        target.unlink()
        target.symlink_to(old_output, target_is_directory=True)
        with patch.dict(os.environ, self.environment, clear=True):
            worktrees.write_record(replace(original, layout=replace(original.layout, output=old_output)))
        source = self.home / "rust/origin"
        _ = self.git(source, "remote", "set-head", "origin", "main")

        result = self.command(self.root, ["bash", str(MAKE_WORKTREE), "origin", "demo", "probe"])

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), tree)
        self.assertEqual(worktree_records.read_record(path), replace(original, owner="", merge_branch="main"))
        self.assertEqual(target.readlink(), original.layout.output)
        self.assertEqual((old_output / "artifact").read_text(), "old output")

    def test_make_reuses_the_empty_directory_after_git_add_stops(self) -> None:
        self.layout_fixture()
        tree = self.trees / "origin/demo/probe"
        source = worktrees.FrozenWorktreeSource(self.repository, self.base)
        run_command = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

        def stop_before_add(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
            if command[3:5] == ["worktree", "add"]:
                self.assertTrue(tree.is_dir())
                self.assertEqual(list(tree.iterdir()), [])
                raise OSError("stopped before Git registered the checkout")
            return run_command(command, **options)

        with patch.dict(os.environ, self.environment, clear=True):
            with (patch.object(subprocess, "run", side_effect=stop_before_add),
                  self.assertRaisesRegex(OSError, "stopped before Git registered")):
                _ = worktrees.make("origin", "demo", "probe", source)
            record_path = worktree_records.record_file(tree)
            pending = worktree_records.read_record(record_path)
            self.assertIs(pending.stage, worktree_records.MakeStage.MAKING)
            inode = tree.stat().st_ino
            foreign = tree / ".foreign"
            _ = foreign.write_text("keep this file")
            with self.assertRaisesRegex(FileExistsError, re.escape(str(tree))):
                _ = worktrees.make("origin", "demo", "probe", source)
            self.assertEqual(foreign.read_text(), "keep this file")
            self.assertEqual(worktree_records.read_record(record_path), pending)
            foreign.unlink()
            self.assertEqual(worktrees.make("origin", "demo", "probe", source), tree)
            finished = worktree_records.read_record(record_path)
            self.assertEqual(finished, replace(pending, stage=worktree_records.MakeStage.DONE))
            self.assertEqual(worktrees.make("origin", "demo", "probe", source), tree)
            self.assertEqual(worktree_records.read_record(record_path), finished)
            self.assertEqual(tree.stat().st_ino, inode)
        self.assertEqual(self.git(tree, "branch", "--show-current"), "demo-probe")
        self.assertEqual((tree / "target").readlink(), self.root / "build/origin/demo/probe")

    def test_remove_keeps_the_checkout_and_output_when_mounts_cannot_be_verified(self) -> None:
        self.layout_fixture()
        tree = self.make_layout("demo", "probe")
        output = (tree / "target").readlink()
        artifact = output / "artifact"
        _ = artifact.write_text("keep")
        with patch.dict(os.environ, self.environment, clear=True):
            with patch.object(build_folders, "may_remove_folder_tree", return_value=False) as check:
                worktrees.remove_git_checkout(tree, self.home / "rust/origin")
                check.assert_not_called()
        self.assertTrue(tree.is_dir())
        self.assertTrue(artifact.is_file())

    def test_remove_reports_output_before_listing_a_missing_checkout(self) -> None:
        self.layout_fixture()
        tree = self.make_layout("demo", "probe")
        output = (tree / "target").readlink()
        _ = tree.rename(tree.with_name("unavailable"))
        notices = StringIO()
        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(worktrees, "worktree_branches", side_effect=worktree_records.Refused(1, "Git listing failed")),
              redirect_stderr(notices),
              self.assertRaisesRegex(worktree_records.Refused, "Git listing failed")):
            worktrees.remove_git_checkout(tree, self.home / "rust/origin")
        self.assertEqual(notices.getvalue(),
                         f"kept for a manual removal list: {output} (worktrees, the checkout is selected for removal)\n")

    def test_retire_lists_the_unit_folders_from_each_repo(self) -> None:
        self.layout_fixture()
        root = self.root / "build"
        output = root / "origin/demo/probe"
        retained = [root / "origin/other/probe", root / "origin/ci/1", root / "origin/main"]
        for path in (output, *retained):
            path.mkdir(parents=True)
        _ = self.git(self.repository, "remote", "set-url", "origin", str(self.root / "other.git"))
        with patch.dict(os.environ, self.environment, clear=True):
            with build_slots.take_copy(self.repository, self.base, showrunner="fixture", unit="demo",
                                       work="retire", purpose="fixture") as copy:
                self.assertEqual(copy.path, root / "other/demo/1")
                git_directory = git_files.at(copy.path).git_dir
        result = self.command(self.repository, [sys.executable, str(WORKTREES), "retire", "demo"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.is_dir())
        self.assertIn(f"kept for a manual removal list: {output.parent}", result.stderr)
        self.assertTrue(copy.path.is_dir())
        self.assertTrue(git_directory.is_dir())
        self.assertTrue((self.repository / ".git").is_dir())
        self.assertTrue(all(path.is_dir() for path in retained))

    def test_legacy_cargo_cleanup_keeps_a_mount_under_the_output_lock(self) -> None:
        self.layout_fixture()
        cache = self.root / "legacy" / "cache"
        cache.mkdir(parents=True)
        roots = cargo_output.CargoOutputRoots(self.repository, self.repository / "target", cache)
        saved = scratch.SavedCargoOutput(roots, ())
        index = sweep.CargoOutputIndex((roots,))
        lock_path = cache.with_name(cache.name + ".lock")
        lock_path.touch()

        def has_no_mount(path: Path) -> bool:
            self.assertEqual(path, cache)
            with lock_path.open("rb") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False

        with (patch.dict(os.environ, self.environment, clear=True),
              patch.object(scratch, "save_cargo_outputs", return_value=(saved,)),
              patch.object(sweep, "saved_cargo_output_roots", return_value=index),
              patch.object(folder_audit, "process_uses", return_value=tmp_sweep.ProcessUsesKnown([])),
              patch.object(build_folders, "may_remove_folder_tree", side_effect=has_no_mount)):
            with self.assertRaisesRegex(OSError, "mounted or could not be verified"):
                folder_audit.clean_checkout_cargo_output(self.repository)
        self.assertTrue(cache.is_dir())

    def test_rename_lock_refuses_copy_allocation_before_the_scan(self) -> None:
        self.layout_fixture()
        request = [sys.executable, "-B", "-c", REQUEST_COPY_WITH_UNIT_LOCK, str(self.repository)]
        result = subprocess.run([*request, "--same-process"],
                                cwd=CHECKOUT, env=self.environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Rerun when idle", result.stdout)
        self.assertFalse((self.root / "build/origin/demo").exists())

    def test_rename_lock_refuses_a_copy_request_from_another_process(self) -> None:
        self.layout_fixture()
        source = self.trees / "origin/demo"
        move = worktrees.UnitFolderRename("origin", "demo", "renamed", source, source.with_name("renamed"))
        with patch.dict(os.environ, self.environment, clear=True), move.hold_copy_allocations():
            result = subprocess.run([sys.executable, "-B", "-c", REQUEST_COPY_WITH_UNIT_LOCK, str(self.repository)],
                                    cwd=CHECKOUT, env=self.environment, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Rerun when idle", result.stdout)
        self.assertFalse((self.root / "build/origin/demo").exists())

    def test_rename_keeps_a_nonempty_old_output_parent(self) -> None:
        self.layout_fixture()
        source = self.trees / "origin/demo"
        move = worktrees.UnitFolderRename("origin", "demo", "renamed", source, source.with_name("renamed"))
        with patch.dict(os.environ, self.environment, clear=True), move.hold_copy_allocations():
            move.old_build.mkdir(parents=True)
            leftover = move.old_build / "unexpected"
            with move.hold_outputs():
                leftover.touch()
            self.assertTrue(leftover.is_file())

    def test_retire_reports_and_keeps_an_unverified_copy(self) -> None:
        self.layout_fixture()
        path = self.root / "build/origin/demo/1"
        path.mkdir(parents=True)
        result = self.command(self.repository, [sys.executable, str(WORKTREES), "retire", "demo"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"kept for a manual removal list: {path.parent}", result.stderr)
        self.assertTrue(path.is_dir())

    def test_cargo_repairs_the_target_and_refuses_an_unassigned_checkout(self) -> None:
        self.layout_fixture()
        _ = self.commit(self.repository, "Cargo.toml", '[workspace]\nmembers = []\n')
        tree = self.make_layout("demo", "probe")
        target = tree / "target"
        output = target.readlink()
        for mode, expected in (("report", 0), ("enforce", 1)):
            with self.subTest(mode=mode):
                self.layout_fixture(mode=mode)
                target.unlink()
                repaired = self.command(tree, [sys.executable, str(CARGO_FOLDER), "check"])
                self.assertEqual(repaired.returncode, 0, repaired.stderr)
                self.assertEqual(target.readlink(), output)
                self.assertEqual((output / build_folders.COPY_USE_STAMP).read_text(), f"{tree}\n")
                target.unlink()
                target.mkdir()
                saved = target / "saved"
                _ = saved.write_text("keep this file")
                kept = self.command(tree, [sys.executable, str(CARGO_FOLDER), "check"])
                self.assertEqual(kept.returncode, expected, kept.stderr)
                self.assertIn("retain the existing path", kept.stderr)
                self.assertEqual(saved.read_text(), "keep this file")
                saved.unlink()
                target.rmdir()
                target.symlink_to(output)
        escaped = self.command(tree, [sys.executable, str(CARGO_FOLDER), "check", "--target-dir", str(self.root)])
        self.assertEqual(escaped.returncode, 1, escaped.stderr)
        refused = self.command(self.repository, [sys.executable, str(CARGO_FOLDER), "check"])
        self.assertEqual(refused.returncode, 1, refused.stderr)
        self.assertIn("worktrees.py make", refused.stderr)
        self.layout_fixture(mode="report")
        reported = self.command(self.repository, [sys.executable, str(CARGO_FOLDER), "check"])
        self.assertEqual(reported.returncode, 0, reported.stderr)
        self.assertIn("worktrees.py make", reported.stderr)

    def test_cargo_accepts_main_output_and_copy_output(self) -> None:
        self.layout_fixture()
        manifest = '[workspace]\nmembers = []\n'
        output = self.root / "build/origin/main"
        main = self.migration_main()
        _ = self.commit(main, "Cargo.toml", manifest)
        for mode in ("report", "enforce"):
            with self.subTest(mode=mode):
                self.layout_fixture(mode=mode)
                (main / "target").unlink(missing_ok=True)
                result = self.command(main, [sys.executable, str(CARGO_FOLDER), "check"])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((main / "target").readlink(), output)
                self.assertEqual((output / build_folders.COPY_USE_STAMP).read_text(), f"{main}\n")
        copy = self.root / "build/origin/demo/1"
        copy.mkdir(parents=True)
        _ = self.git(copy, "init")
        _ = (copy / "Cargo.toml").write_text(manifest)
        copied = self.command(copy, [sys.executable, str(CARGO_FOLDER), "check"])
        self.assertEqual(copied.returncode, 0, copied.stderr)
        (copy / "target").symlink_to(output, target_is_directory=True)
        escaped = self.command(copy, [sys.executable, str(CARGO_FOLDER), "check"])
        self.assertEqual(escaped.returncode, 1, escaped.stderr)

    def test_full_cargo_clean_lists_linked_outputs_and_keeps_the_links(self) -> None:
        self.layout_fixture(mode="report")
        _ = self.commit(self.repository, "Cargo.toml", '[workspace]\nmembers = []\n')
        tree = self.make_layout("demo", "probe")
        main = self.home / "rust/origin"
        main_output = self.root / "build/origin/main"
        main_output.mkdir()
        (main / "target").symlink_to(main_output)
        for checkout in (tree, main):
            target = checkout / "target"
            output = target.readlink()
            artifact = output / "artifact"
            _ = artifact.write_text("output")
            outside = self.root / "outside"
            _ = outside.write_text("retain")
            (output / "link").symlink_to(outside)
            args = [sys.executable, str(CARGO_FOLDER), "--clean-output", "clean"]
            with output.with_name(output.name + ".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                busy = self.command(checkout, args)
                self.assertEqual(busy.returncode, 0, busy.stderr)
                self.assertEqual(artifact.read_text(), "output")
            clean = self.command(checkout, args)
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertEqual(target.readlink(), output)
            self.assertEqual(artifact.read_text(), "output")
            self.assertTrue((output / "link").is_symlink())
            self.assertIn(f"kept for a manual removal list: {output}", clean.stderr)
            self.assertEqual(outside.read_text(), "retain")
        copy = self.root / "build/origin/demo/1"
        copy.mkdir()
        _ = self.git(copy, "init")
        _ = (copy / "Cargo.toml").write_text('[workspace]\nmembers = []\n')
        delegated = self.command(copy, [sys.executable, str(CARGO_FOLDER), "--clean-output", "clean"])
        self.assertEqual(delegated.returncode, 3, delegated.stderr)

    def commit(self, tree: Path, path: str, contents: str) -> str:
        destination = tree / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_text(contents)
        _ = self.git(tree, "add", path)
        _ = self.git(tree, "commit", "-m", f"Change {path}")
        return self.git(tree, "rev-parse", "HEAD")

    def publish(self, tree: Path, branch: str) -> None:
        _ = self.git(tree, "push", "-u", "origin", branch)
        _ = self.git(tree, "config", "--local", f"branch.{branch}.merge", f"refs/heads/{branch}")

    def land(self, branch: str) -> None:
        _ = self.git(self.merge_tree, "merge", "--no-ff", branch, "-m", "Merge the checkpoint")
        self.publish(self.merge_tree, "integration")

    def record_path(self, tree: Path) -> Path:
        digest = hashlib.sha256(str(tree.absolute()).encode()).hexdigest()
        return self.state / "made" / f"{digest}.json"

    def record(self, tree: Path) -> MadeWorktree:
        return cast(MadeWorktree, json.loads(self.record_path(tree).read_text()))

    def repository_state(self) -> RepositoryState:
        return RepositoryState(
            self.git(self.repository, "worktree", "list", "--porcelain"),
            self.git(self.repository, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
            self.git(self.origin, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads"),
            {path.name: path.read_bytes() for path in (self.state / "made").glob("*.json")},
            tuple(sorted(path.name for path in self.trees.iterdir())),
        )

    def assert_ended(self, tree: Path, branch: str) -> None:
        state = self.repository_state()
        self.assertNotIn(f"worktree {tree}\n", state.worktrees + "\n")
        self.assertNotIn(f"refs/heads/{branch}", [line.split()[0] for line in state.branches.splitlines()])
        self.assertNotIn(f"refs/heads/{branch}", [line.split()[0] for line in state.origin_branches.splitlines()])
        self.assertNotIn(self.record_path(tree).name, state.records)
        self.assertNotIn(tree.name, state.folders)
        self.assertFalse(tree.with_name(tree.name + ".lease.json").exists())
        self.assertFalse(tree.with_name(tree.name + ".lock").exists())
        config = self.command(self.repository, ["git", "config", "--local", "--get", f"branch.{branch}.merge"])
        self.assertEqual(config.returncode, 1, config.stdout + config.stderr)
        self.assertEqual(config.stdout, "")

    def test_make_records_the_staff_owner_and_merge_branch(self) -> None:
        for branch in ("recorded", "record-unwritable"):
            with self.subTest(branch=branch):
                tree = self.trees / branch
                if branch == "record-unwritable":
                    blocked_state = self.root / "blocked-state"
                    _ = blocked_state.write_text("retain this file\n")
                    self.environment["WORKTREES_STATE_DIR"] = str(blocked_state)
                    retained = self.repository_state()

                    result = self.legacy_fixture(tree, branch)

                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(self.repository_state(), retained)
                    self.assertEqual(blocked_state.read_text(), "retain this file\n")
                else:
                    before = datetime.now(UTC)
                    self.make(tree, branch)
                    after = datetime.now(UTC)
                    record = self.record(tree)
                    self.assertEqual({key: value for key, value in record.items() if key != "made_at"}, {
                        "version": 1, "path": str(tree), "branch": branch, "repo": str(self.repository),
                        "base": self.base, "merge_branch": "integration", "owner": "fixture-unit",
                        "stage": "done",
                    })
                    made_at = datetime.fromisoformat(record["made_at"])
                    self.assertGreaterEqual(made_at, before.replace(microsecond=0))
                    self.assertLessEqual(made_at, after)
                    self.assertEqual((tree / "code.txt").read_text(), "base\n")
                    self.assertEqual(self.git(tree, "branch", "--show-current"), branch)
                    self.assertEqual(build_folders.build_folder_state(tree), "free")
                    lease = cast(dict[str, object], json.loads(tree.with_name(tree.name + ".lease.json").read_text()))
                    self.assertEqual(lease["purpose"], tree.name)

    def test_merged_checkpoint_and_plan_shrink_end_all_recorded_items(self) -> None:
        for branch in ("checkpoint", "ancestor-with-earlier-target-date", "without-branch-config"):
            with self.subTest(branch=branch):
                if branch != "checkpoint":
                    self.setUp()
                tree = self.trees / branch
                self.make(tree, branch)
                commit_dates: dict[str, str] = {} if branch == "checkpoint" else {
                    "GIT_COMMITTER_DATE": "2030-01-02T00:00:00+00:00",
                    "GIT_AUTHOR_DATE": "2030-01-02T00:00:00+00:00"}
                with patch.dict(self.environment, commit_dates):
                    _ = self.commit(tree, "code.txt", "checkpoint\n")
                self.publish(tree, branch)
                if branch == "checkpoint":
                    self.land(branch)
                    _ = self.commit(tree, "docs/plans/phase.md", "# Completed phase\n")
                    self.publish(tree, branch)
                else:
                    _ = self.git(self.merge_tree, "merge", "--ff-only", branch)
                    with patch.dict(self.environment, {
                            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
                            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00"}):
                        _ = self.commit(self.merge_tree, "code.txt", "later target work\n")
                    self.publish(self.merge_tree, "integration")

                if branch == "without-branch-config":
                    _ = self.git(self.repository, "config", "--local", "--remove-section", f"branch.{branch}")
                cleanup = self.cleanup_result(tree)
                self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
                self.assertEqual(cleanup.stdout.splitlines(), ["cleanup: all work is on the target branch"])
                results = self.finish_landed()

                self.assertEqual(results, [{"kind": "Kept", "path": str(tree), "detail": "the checkout landed and requires manual review"}])
                if branch == "checkpoint":
                    self.assertEqual((self.merge_tree / "code.txt").read_text(), "checkpoint\n")
                else:
                    self.assertEqual((self.merge_tree / "code.txt").read_text(), "later target work\n")
                self.assertTrue(tree.is_dir())
                self.assertTrue(self.record_path(tree).is_file())

    def test_cherry_picked_content_ends_under_a_different_commit_hash(self) -> None:
        tree, branch = self.trees / "cherry", "cherry"
        self.make(tree, branch)
        checkpoint = self.commit(tree, "code.txt", "cherry-picked\n")
        self.publish(tree, branch)
        _ = self.git(self.merge_tree, "cherry-pick", "-x", checkpoint)
        self.publish(self.merge_tree, "integration")

        results = self.finish_landed()

        self.assertEqual(results, [{"kind": "Kept", "path": str(tree), "detail": "the checkout landed and requires manual review"}])
        self.assertEqual((self.merge_tree / "code.txt").read_text(), "cherry-picked\n")
        self.assertTrue(tree.is_dir())
        self.assertTrue(self.record_path(tree).is_file())

    def test_allow_existing_reuses_unrecorded_checkouts_without_adoption(self) -> None:
        self.layout_fixture()
        for owner, tree, merge_branch in (
            ("style", self.trees / "origin/style/worktree", ""),
            ("restored", self.root / "legacy-checkout", "integration"),
        ):
            with self.subTest(owner=owner):
                branch = f"{owner}-worktree"
                _ = self.git(self.repository, "worktree", "add", "-b", branch, str(tree), self.base)
                head = self.commit(tree, "code.txt", f"{owner} work\n")
                with patch.dict(os.environ, self.environment, clear=True):
                    layout = worktrees.unit_checkout_output(tree, "origin", owner)
                    reused = worktrees.create_checkout(
                        tree, self.merge_tree, build_folders.ExistingWorktreeBranch(branch), layout,
                        owner=owner, merge_branch=merge_branch, allow_existing=True)
                    self.assertIsNone(worktree_records.checkout_record(tree))
                self.assertEqual(reused, tree)
                self.assertEqual(self.git(tree, "rev-parse", "HEAD"), head)
                self.assertFalse((tree / "target").is_symlink())

    def test_landed_worktree_lists_its_assigned_output(self) -> None:
        self.layout_fixture()
        tree, branch = self.trees / "origin/demo/probe", "demo-probe"
        with patch.dict(os.environ, self.environment, clear=True):
            layout = worktrees.output_record("origin", "demo", "probe")
            _ = worktrees.create_checkout(tree, self.repository, build_folders.NewWorktreeBranch(branch, "HEAD"),
                                          layout, owner="demo", merge_branch="integration")
        _ = (layout.output / "artifact").write_text("build output")
        _ = self.commit(tree, "code.txt", "landed\n")
        self.publish(tree, branch)
        self.land(branch)
        self.assertEqual(self.finish_landed(reviewed=(layout.output, tree)), [{"kind": "Kept", "path": str(tree), "detail": "the checkout landed and requires manual review"}])
        self.assertEqual((layout.output / "artifact").read_text(), "build output")
        self.assertTrue(tree.is_dir())
        self.assertTrue(self.record_path(tree).is_file())

    def test_landed_worktree_keeps_an_existing_removal_mark(self) -> None:
        tree, branch = self.trees / "recorded-output", "recorded-output"
        self.make(tree, branch)
        _ = self.commit(tree, "code.txt", "landed\n")
        self.publish(tree, branch)
        self.land(branch)
        mark = scratch.cargo_removal_mark(tree)
        scratch.atomic_record(mark, [])
        for _attempt in range(2):
            self.assertEqual(self.finish_landed(reviewed=(tree,)),
                             [{"kind": "Kept", "path": str(tree), "detail": "the checkout landed and requires manual review"}])
            self.assertTrue(mark.is_file())
            self.assertTrue(tree.is_dir())

    def test_end_landed_keeps_a_record_and_branch_for_another_merge_branch(self) -> None:
        tree, branch = self.trees / "other-production", "other-production"
        self.make(tree, branch)
        path = self.record_path(tree)
        saved = replace(worktree_records.read_record(path), merge_branch="other-integration")
        with patch.dict(os.environ, self.environment, clear=True):
            worktrees.write_record(saved)
        tip = self.commit(tree, "code.txt", "checkpoint\n")
        self.publish(tree, branch)
        self.land(branch)
        _ = self.git(self.repository, "worktree", "remove", str(tree))

        self.assertEqual(self.finish_landed(), [])

        self.assertEqual(worktree_records.read_record(path), saved)
        for repository in (self.repository, self.origin):
            self.assertEqual(self.git(repository, "rev-parse", f"refs/heads/{branch}"), tip)

    def landed_cargo_worktree(self) -> tuple[Path, Path]:
        tree, branch = self.trees / "legacy-output", "legacy-output"
        self.make(tree, branch)
        _ = self.commit(tree, "Cargo.toml", "[workspace]\n")
        self.publish(tree, branch)
        self.land(branch)
        cargo_root = self.root / "external"
        cargo_root.mkdir()
        build = cargo_root / tree.name
        mirror = self.root / "mirror"
        parent = mirror.joinpath(*tree.parent.parts[1:])
        parent.parent.mkdir(parents=True)
        parent.symlink_to(cargo_root, target_is_directory=True)
        mapping = {"workspace_root": str(tree), "target_directory": str(tree / "target"),
                   "build_directory": str(build)}
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        cargo = bin_dir / "cargo"
        overridden = {**mapping, "build_directory": str(cargo_root / "override")}
        _ = cargo.write_text("#!/bin/sh\ncase \"$*\" in\n"
                             + "  *build.build-dir=*) printf '%s\\n' " + shlex.quote(json.dumps(overridden)) + " ;;\n"
                             + "  *) printf '%s\\n' " + shlex.quote(json.dumps(mapping)) + " ;;\nesac\n")
        cargo.chmod(0o755)
        state = self.root / "sweep-state"
        state.mkdir()
        _ = (state / "cargo-output-roots.json").write_text(json.dumps([mapping]))
        self.environment.update({"PATH": str(bin_dir) + os.pathsep + self.environment["PATH"],
                                 "LINT_SWEEP_STATE_DIR": str(state)})
        config = Path(self.environment["CONF_DIR"]) / "build_folders.conf"
        _ = write_config_fixture(config, f"roots.build={self.root / 'build'}\nroots.build.natedev={self.root / 'build'}\n"
            + f"roots.workspace={self.root / 'worktrees'}\n")
        return tree, build

    def test_landed_legacy_worktree_reports_only_its_checkout(self) -> None:
        # The old Cargo mirror keys are gone, so its output is no longer found or listed.
        tree, build = self.landed_cargo_worktree()
        file = build / "debug/deps/libportable-1234567890abcdef.rlib"
        file.parent.mkdir(parents=True)
        _ = file.write_text("build output")
        self.assertEqual(self.finish_landed(reviewed=(tree,)),
                         [{"kind": "Kept", "path": str(tree), "detail": "the checkout landed and requires manual review"}])
        self.assertEqual(file.read_text(), "build output")
        self.assertTrue(tree.is_dir())
        self.assertFalse(scratch.cargo_removal_mark(tree).exists())

    def test_worktree_removal_keeps_unassigned_outputs(self) -> None:
        tree, default = self.landed_cargo_worktree()
        override = default.with_name("override")
        mark = scratch.cargo_removal_mark(tree)
        outside = self.root / "downloads"
        for directory in (default, override, outside):
            directory.mkdir()
            _ = (directory / "contents").write_text(directory.name)
        no_peers: list[str] = []
        scratch.atomic_record(mark, [
            {"workspace_root": str(tree), "target_directory": str(tree / "target"),
             "build_directory": str(directory), "other_checkouts": no_peers}
            for directory in (override, outside)
        ])

        self.assertEqual(self.finish_landed(), [{"kind": "Kept", "path": str(tree), "detail": "the checkout landed and requires manual review"}])
        self.assertEqual((default / "contents").read_text(), default.name)
        self.assertEqual((override / "contents").read_text(), "override")
        self.assertEqual((outside / "contents").read_text(), "downloads")
        self.assertTrue(mark.exists())
        self.assertTrue(tree.is_dir())

    def test_unlanded_commit_is_reported_and_preserves_all_items(self) -> None:
        for branch in ("unlanded", "origin-ahead", "origin-ahead-missing-locally"):
            with self.subTest(branch=branch):
                if branch != "unlanded":
                    self.setUp()
                tree = self.trees / branch
                self.make(tree, branch)
                checkpoint = self.commit(tree, "code.txt", "unlanded work\n")
                self.publish(tree, branch)
                if branch != "unlanded":
                    self.land(branch)
                    clone = self.root / branch
                    _ = self.git(self.root, "clone", "--branch", branch, str(self.origin), str(clone))
                    checkpoint = self.commit(clone, "origin.txt", "unlanded origin work\n")
                    self.publish(clone, branch)
                    if branch == "origin-ahead":
                        _ = self.git(self.repository, "fetch", "origin", branch)
                    else:
                        absent = self.command(self.repository, ["git", "cat-file", "-e", f"{checkpoint}^{{commit}}"])
                        self.assertNotEqual(absent.returncode, 0)
                before = self.repository_state()

                results = self.finish_landed()

                self.assertEqual([(row["kind"], row["path"]) for row in results], [("Kept", str(tree))])
                self.assertIn(checkpoint, results[0]["detail"])
                self.assertEqual(self.repository_state(), before)
                self.assertEqual((tree / "code.txt").read_text(), "unlanded work\n")
                cleanup = self.cleanup_result(tree)
                self.assertEqual(cleanup.returncode, 1, cleanup.stdout + cleanup.stderr)
                self.assertEqual(self.repository_state(), before)
                if branch != "unlanded":
                    self.assertEqual(results[0]["detail"], f"origin has commits that the local branch lacks: {checkpoint}")
                    self.assertEqual(cleanup.stdout.strip(), results[0]["detail"])
                else:
                    self.assertIn(checkpoint, cleanup.stdout)

    def test_cleanup_without_a_record_uses_the_origin_default_branch(self) -> None:
        for state in ("landed", "unlanded", "missing-default"):
            with self.subTest(state=state):
                if state != "landed":
                    self.setUp()
                tree, branch = self.trees / "unrecorded", "unrecorded"
                self.make(tree, branch)
                checkpoint = self.commit(tree, "code.txt", "unrecorded work\n")
                self.publish(tree, branch)
                self.record_path(tree).unlink()
                if state == "landed":
                    _ = self.git(self.repository, "push", "origin", f"{branch}:main")
                elif state == "missing-default":
                    _ = self.git(self.repository, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
                    _ = self.git(self.origin, "symbolic-ref", "HEAD", "refs/heads/missing-default")
                before = self.repository_state()

                result = self.cleanup_result(tree)

                expected = {"landed": 0, "unlanded": 1, "missing-default": 2}[state]
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual(self.repository_state(), before)
                if state == "landed":
                    self.assertEqual(result.stdout.splitlines(), ["cleanup: all work is on the target branch"])
                elif state == "unlanded":
                    self.assertIn(checkpoint, result.stdout)
                else:
                    self.assertIn("origin default branch is missing", result.stderr)

    def test_dirty_and_staff_held_trees_preserve_all_items(self) -> None:
        for reason in ("dirty", "staff-held", "switched", "detached", "no-commit", "target-merge-only"):
            with self.subTest(reason=reason):
                tree = self.trees / reason
                self.make(tree, reason)
                if reason not in ("no-commit", "target-merge-only"):
                    _ = self.commit(tree, f"{reason}.txt", "landed work\n")
                    self.publish(tree, reason)
                    self.land(reason)
                if reason == "dirty":
                    _ = (tree / "code.txt").write_text("uncommitted work\n")
                elif reason == "staff-held":
                    staff_file = Path(self.environment["STAFF_FILE"])
                    record = cast(dict[str, object], json.loads(staff_file.read_text()))
                    rows = cast(list[dict[str, object]], record["rows"])
                    holder_id = str(uuid4())
                    rows.append({"id": holder_id, "current_session": holder_id, "kind": "unit",
                                 "name": "held-unit", "parent": rows[0]["id"],
                                 "worktree": {"machine": socket.gethostname().split(".", 1)[0],
                                              "path": str(tree), "state": "present"}})
                    _ = staff_file.write_text(json.dumps(record))
                elif reason == "switched":
                    _ = self.git(tree, "checkout", "-b", "replacement-branch")
                    _ = self.commit(tree, f"{reason}-work.txt", "unlanded replacement work\n")
                elif reason == "detached":
                    _ = self.git(tree, "checkout", "--detach")
                    _ = self.commit(tree, f"{reason}-work.txt", "unlanded replacement work\n")
                elif reason == "target-merge-only":
                    _ = self.commit(self.merge_tree, "target.txt", "new target work\n")
                    self.publish(self.merge_tree, "integration")
                    _ = self.git(tree, "fetch", "origin", "integration")
                    _ = self.git(tree, "merge", "--no-ff", "origin/integration", "-m", "Merge the target")
                before = self.repository_state()
                tip = self.git(tree, "rev-parse", "HEAD")

                results = self.finish_landed()

                selected = next(row for row in results if row["path"] == str(tree))
                self.assertEqual(selected["kind"], "Kept")
                self.assertTrue(selected["detail"], results)
                self.assertEqual(self.repository_state(), before)
                self.assertEqual(self.git(tree, "rev-parse", "HEAD"), tip)
                if reason == "no-commit":
                    self.assertEqual(selected["detail"], "no commit of its own")
                elif reason == "target-merge-only":
                    self.assertEqual(selected["detail"], "only merges of the target outside it")
                else:
                    self.assertEqual((tree / f"{reason}.txt").read_text(), "landed work\n")
                if reason == "dirty":
                    self.assertEqual((tree / "code.txt").read_text(), "uncommitted work\n")
                    cleanup = self.cleanup_result(tree)
                    self.assertEqual(cleanup.returncode, 1, cleanup.stdout + cleanup.stderr)
                    self.assertIn("dirty worktree", cleanup.stdout)
                    self.assertEqual(self.repository_state(), before)
                elif reason in ("switched", "detached"):
                    self.assertEqual((tree / f"{reason}-work.txt").read_text(), "unlanded replacement work\n")

    def test_landed_cleanup_waits_for_manual_removal_before_finishing_git_records(self) -> None:
        tree, branch = self.trees / "partial", "partial"
        self.make(tree, branch)
        _ = self.commit(tree, "code.txt", "landed work\n")
        self.publish(tree, branch)
        self.land(branch)
        record = self.record_path(tree).read_bytes()
        before = self.repository_state()
        self.assertEqual(self.finish_landed(reviewed=(tree,)),
                         [{"kind": "Kept", "path": str(tree),
                           "detail": "the checkout landed and requires manual review"}])
        self.assertEqual(self.record_path(tree).read_bytes(), record)
        self.assertEqual(self.repository_state(), before)

        _ = self.git(self.repository, "worktree", "remove", str(tree))
        self.assertEqual(self.finish_landed(), [{"kind": "Ended", "path": str(tree), "detail": branch}])
        self.assert_ended(tree, branch)


if __name__ == "__main__":
    _ = unittest.main()

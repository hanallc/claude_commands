"""Test worktree creation and removal through the public commands."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Callable, NamedTuple, TypedDict, cast, override
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch
from uuid import uuid4


from scripts.lib.helper_tree_test_support import COPY_HELPERS, copy_helpers

from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.buildlog.store import host_name
from scripts.lib import build_folders, build_slots, cargo_output, git_files, scratch
from scripts.lib.build_folders_test_support import workspace_folder_settings
from scripts.agents.test_support import MACHINE_SETTINGS
from scripts.lib.scratch_test_support import write_scratch_settings
from scripts.lint.config_test_support import SWEEP_SCAN_SETTINGS
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.lint import folder_audit, sweep, tmp_sweep
from scripts.production import staff
from scripts.worktree import worktree_links, worktrees


CHECKOUT = Path(__file__).resolve().parents[2]
MAKE_WORKTREE = CHECKOUT / "scripts/worktree/make_worktree.sh"
WORKTREES = CHECKOUT / "scripts/worktree/worktrees.py"
CARGO_FOLDER = CHECKOUT / "scripts/build_hold/cargo_folder.py"

MAKE_LEGACY_FIXTURE = """
from pathlib import Path
import sys
from scripts.lib.build_folders import NewWorktreeBranch
from scripts.worktree import worktrees

source = Path.cwd()
owner, branch = worktrees.caller_branch(source, source, worktrees.staff_record())
worktrees.create_checkout(Path(sys.argv[1]), source, NewWorktreeBranch(sys.argv[2], "HEAD"),
                          worktrees.UnassignedOutput(), owner=owner, merge_branch=branch)
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
    maxDiff: int | None = None
    root: Path = Path()
    home: Path = Path()
    origin: Path = Path()
    repository: Path = Path()
    merge_tree: Path = Path()
    trees: Path = Path()
    state: Path = Path()
    environment: dict[str, str] = {}
    base: str = ""

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="worktree-lifecycle-"))).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        _ = copy_helpers(self.home, *COPY_HELPERS, "scripts/worktree")
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
        write_scratch_settings(self.root / "settings", self.root)
        self.environment = {
            **config_environment(self.root / "settings", fixtures={
                "build_folders.conf": workspace_folder_settings(self.root),
                "machines.conf": MACHINE_SETTINGS,
                "lint.conf": SWEEP_SCAN_SETTINGS,
            }),
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"), "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "WORKTREES_STATE_DIR": str(self.state),
            "STAFF_CONFIG": str(config), "STAFF_FILE": str(staff_file),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            **interpreter_cache_environment(self.root / "cache"), "PYTHONPATH": str(CHECKOUT / "scripts/lib/python_startup"),
            "HERDR_BIN": "/nonexistent/herdr",
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
        _ = write_config_fixture(config / "build_folders.conf",
            f"roots.workspace={self.trees}\nroots.build={build}\nroots.build.{host_name()}={build}\n"
            + f"roots.scratch={scratch_root}\nroots.scratch.{host_name()}={scratch_root}\n"
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

    def add_linked_tree(self, repository: Path, path: Path, branch: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = self.git(repository, "worktree", "add", "-b", branch, str(path))
        return path

    def migration_main(self) -> Path:
        main = self.home / "rust/origin"
        main.parent.mkdir(parents=True, exist_ok=True)
        _ = self.git(self.root, "clone", str(self.origin), str(main))
        return main

    def test_migration_dry_run_reads_without_writing_then_applies_the_same_plan(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        named = self.add_linked_tree(main, self.trees / "named source", "branch-list")
        inferred = self.add_linked_tree(main, self.trees / "origin-fixture-unit-repair", "branch-name")
        retired = self.add_linked_tree(main, self.trees / "legacy-readme", "branch-subject")
        _ = self.git(retired, "commit", "--allow-empty", "-m", "checkpoint(former-unit): save work")
        unknown = self.add_linked_tree(main, self.trees / "orphan", "branch-unknown")
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
        conflict = self.add_linked_tree(main, self.trees / "conflict", "branch-conflict")
        occupied = self.trees / "origin/fixture-unit/taken"
        occupied.mkdir(parents=True)
        list_file = self.root / "list"
        _ = list_file.write_text(f"{shlex.quote(str(named))} fixture-unit chosen\n{conflict} fixture-unit taken\n")
        check = [sys.executable, "-B", str(WORKTREES), "migrate", "--list", str(list_file)]
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
        self.assertIn(f"keep {maker}: the maker owns this worktree", dry.stdout)
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

    def test_migration_moves_scratch_clone_and_repairs_its_linked_worktrees(self) -> None:
        self.layout_fixture()
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
            self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=True), 0)
            self.assertIn(f"keep {archived}: no origin; moves with the build root to old-claude", dry_run.getvalue())
            self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 0)
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
            self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 0)
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
        migration = self.command(self.root, [sys.executable, "-B", str(WORKTREES), "migrate",
                                             "--list", os.devnull, "--dry-run"])
        self.assertEqual(migration.returncode, 0, migration.stderr)
        self.assertIn(f"main output {main} -> {self.root}/build/origin/main", migration.stdout)
        self.assertIn(f"keep {sibling}: keeps its current output; {main} is the main checkout for origin",
                      migration.stdout)
        self.assertNotIn(f"main output {sibling} ->", migration.stdout)

    def test_migration_uses_origin_alias_and_strips_legacy_names(self) -> None:
        self.layout_fixture()
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
        status = self.add_linked_tree(main, self.trees / "claude-statusline-role", "statusline-role")
        _ = self.git(status, "commit", "--allow-empty", "-m", "checkpoint(claude-old-fixture-unit-statusline-role): save")
        foreign = main.with_name("foreign")
        _ = self.git(self.root, "clone", str(self.origin), str(foreign))
        _ = self.git(foreign, "remote", "set-url", "origin", "git@fixture:owner/foreign.git")
        unknown = self.add_linked_tree(foreign, self.trees / "foreign-fixture-unit-probe", "probe")
        _ = self.git(unknown, "commit", "--allow-empty", "-m", "checkpoint(old-fixture-unit-fixture-unit): save")
        fallback = self.add_linked_tree(foreign, self.trees / "foreign-fixture-unit-example", "example")
        _ = self.git(fallback, "commit", "--allow-empty", "-m", "shrink(old-fixture-unit-other): save")
        wrapped = self.add_linked_tree(main, self.trees / "wrapped-fixture-unit", "wrapped")
        archived = self.home / "rust/hanadocs/archive/vault/wrapped/wrapped-production.md"
        archived.parent.mkdir(parents=True)
        _ = archived.write_text(f"- **Showrunner checkout:** `{wrapped}`\n")
        result = self.command(self.root, [sys.executable, "-B", str(WORKTREES), "migrate", "--list", os.devnull,
                                          "--dry-run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        for old, _, short in names:
            self.assertIn(f"move {self.trees / old} -> {self.trees}/claude/fixture-unit/{short} |", result.stdout)
        self.assertIn(f"keep {production}: showrunner checkout awaits question 4", result.stdout)
        self.assertIn(f"keep {harness}: Claude Code owns this worktree", result.stdout)
        self.assertIn(f"keep {gate}: the maker owns this worktree", result.stdout)
        self.assertIn(f"keep {wrapped}: showrunner checkout awaits question 4; archived production wrapped",
                      result.stdout)
        self.assertIn(f"keep {repo_unit}: unit unknown", result.stdout)
        self.assertIn(f"move {status} -> {self.trees}/claude/statusline-role/worktree | statusline-role | worktree "
                      + "| commit subject | retired-unit", result.stdout)
        self.assertIn(f"keep {unknown}: unit unknown", result.stdout)
        self.assertIn(f"move {fallback} -> {self.trees}/foreign/other/fixture-unit-example | other "
                      + "| fixture-unit-example | commit subject | retired-unit", result.stdout)
        self.assertNotIn(f"{self.trees}/claude_commands/", result.stdout)

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

    def test_make_assigns_output_to_a_legacy_checkout_on_its_recorded_branch(self) -> None:
        self.layout_fixture()
        tree = self.make_layout("demo", "probe")
        path = self.record_path(tree)
        saved = worktrees.read_record(path)
        _ = self.git(tree, "branch", "-m", "legacy-branch")
        legacy = replace(saved, branch="legacy-branch", layout=worktrees.UnassignedOutput())
        (tree / "target").unlink()
        with patch.dict(os.environ, self.environment, clear=True):
            worktrees.write_record(legacy)

        self.assertEqual(self.make_layout("demo", "probe"), tree)

        self.assertEqual(worktrees.read_record(path), replace(legacy, layout=saved.layout))
        self.assertEqual(self.git(tree, "branch", "--show-current"), "legacy-branch")
        self.assertEqual((tree / "target").readlink(), self.root / "build/origin/demo/probe")

    def test_make_refreshes_a_migrated_owner_and_empty_merge_branch(self) -> None:
        self.layout_fixture()
        tree = self.make_layout("demo", "probe")
        path = self.record_path(tree)
        current = worktrees.read_record(path)
        with patch.dict(os.environ, self.environment, clear=True):
            worktrees.write_record(replace(current, owner=str(uuid4()), merge_branch=""))

        self.assertEqual(self.make_layout("demo", "probe"), tree)

        self.assertEqual(worktrees.read_record(path), current)

    def test_make_refreshes_caller_fields_and_output_from_another_directory(self) -> None:
        self.layout_fixture()
        tree = self.make_layout("demo", "probe")
        path = self.record_path(tree)
        original = worktrees.read_record(path)
        assert isinstance(original.layout, worktrees.RecordedWorktreeOutput)
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
        self.assertEqual(worktrees.read_record(path), replace(original, owner="", merge_branch="main"))
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
            record_path = worktrees.record_file(tree)
            pending = worktrees.read_record(record_path)
            self.assertIs(pending.stage, worktrees.MakeStage.MAKING)
            inode = tree.stat().st_ino
            foreign = tree / ".foreign"
            _ = foreign.write_text("keep this file")
            with self.assertRaisesRegex(FileExistsError, re.escape(str(tree))):
                _ = worktrees.make("origin", "demo", "probe", source)
            self.assertEqual(foreign.read_text(), "keep this file")
            self.assertEqual(worktrees.read_record(record_path), pending)
            foreign.unlink()
            self.assertEqual(worktrees.make("origin", "demo", "probe", source), tree)
            finished = worktrees.read_record(record_path)
            self.assertEqual(finished, replace(pending, stage=worktrees.MakeStage.DONE))
            self.assertEqual(worktrees.make("origin", "demo", "probe", source), tree)
            self.assertEqual(worktrees.read_record(record_path), finished)
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
              patch.object(worktrees, "worktree_branches", side_effect=worktrees.Refused(1, "Git listing failed")),
              redirect_stderr(notices),
              self.assertRaisesRegex(worktrees.Refused, "Git listing failed")):
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
        source = self.trees / "origin/demo"
        move = worktrees.UnitFolderRename("origin", "demo", "renamed", source, source.with_name("renamed"))
        with patch.dict(os.environ, self.environment, clear=True), move.hold_copy_allocations():
            with self.assertRaisesRegex(BlockingIOError, "Rerun when idle"):
                with build_slots.take_copy(self.repository, "HEAD", showrunner="demo", unit="demo",
                                           work="rename", purpose="fixture"):
                    self.fail("the copy must wait for the rename")
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
        saved = replace(worktrees.read_record(path), merge_branch="other-integration")
        with patch.dict(os.environ, self.environment, clear=True):
            worktrees.write_record(saved)
        tip = self.commit(tree, "code.txt", "checkpoint\n")
        self.publish(tree, branch)
        self.land(branch)
        _ = self.git(self.repository, "worktree", "remove", str(tree))

        self.assertEqual(self.finish_landed(), [])

        self.assertEqual(worktrees.read_record(path), saved)
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
        host = host_name()
        _ = write_config_fixture(config, f"roots.slot_cargo.{host}={cargo_root}\nroots.cargo_paths.{host}={mirror}\n"
            + f"roots.build={self.root / 'build'}\nroots.build.natedev={self.root / 'build'}\n"
            + f"roots.workspace={self.root / 'worktrees'}\n")
        return tree, build

    def test_landed_worktree_lists_its_external_cargo_output(self) -> None:
        tree, build = self.landed_cargo_worktree()
        file = build / "debug/deps/libportable-1234567890abcdef.rlib"
        file.parent.mkdir(parents=True)
        _ = file.write_text("build output")
        self.assertEqual(self.finish_landed(reviewed=(build, tree)),
                         [{"kind": "Kept", "path": str(tree), "detail": "the checkout landed and requires manual review"}])
        self.assertEqual(file.read_text(), "build output")
        self.assertFalse(scratch.cargo_removal_mark(tree).exists())

    def test_landed_worktree_reports_an_empty_external_output(self) -> None:
        tree, build = self.landed_cargo_worktree()
        build.mkdir()
        self.assertEqual(self.finish_landed(reviewed=(build, tree)),
                         [{"kind": "Kept", "path": str(tree), "detail": "the checkout landed and requires manual review"}])
        self.assertTrue(build.is_dir())
        self.assertTrue(tree.is_dir())

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

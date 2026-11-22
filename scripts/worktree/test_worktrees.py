"""Test worktree creation and removal through the public commands."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import fcntl
import hashlib
from itertools import product
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
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch
from uuid import uuid4


from scripts.lib.helper_tree_test_support import COPY_HELPERS, copy_helpers

from scripts.lib.git_test_support import fixture_git_environment
from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.buildlog.store import host_name
from scripts.agents import remote
from scripts.lib import build_folders, build_slots, cargo_output, conf, git_exclude, git_files, scratch
from scripts.lint import folder_audit, sweep, tmp_sweep
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.production import staff
from scripts.production.production_names import CARGO_LINER_PRODUCTION
from scripts.production.test_production_doc import enrolled_document
from scripts.worktree import worktree_links, worktrees


CHECKOUT = Path(__file__).resolve().parents[2]
MAKE_WORKTREE = CHECKOUT / "scripts/worktree/make_worktree.sh"
WORKTREES = CHECKOUT / "scripts/worktree/worktrees.py"
CARGO_FOLDER = CHECKOUT / "scripts/build_hold/cargo_folder.py"
NO_PROBE_FDS: frozenset[int] = frozenset()

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

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="worktree-lifecycle-"))).resolve()
        self.proc = self.root / "proc"
        (self.proc / "self").mkdir(parents=True)
        _ = (self.proc / "self" / "status").write_text("CapPrm:\t0000000000000000\n")
        _ = self.enterContext(patch.object(tmp_sweep, "PROC_DIR", str(self.proc)))
        self.home = self.root / "home"
        self.home.mkdir()
        _ = copy_helpers(self.home, *COPY_HELPERS, "scripts/worktree", "config")
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
            **fixture_git_environment(self.root),
            **config_environment(self.root / "settings"),
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"), "PATH": os.environ.get("PATH", os.defpath),
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "WORKTREES_STATE_DIR": str(self.state),
            "STAFF_CONFIG": str(config), "STAFF_FILE": str(staff_file),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            **interpreter_cache_environment(self.root / "cache"), "PYTHONPATH": str(CHECKOUT / "scripts/lib/python_startup"),
            "HERDR_BIN": "/nonexistent/herdr",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
            "GIT_AUTHOR_NAME": "Worktree fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Worktree fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        for key in ("CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR"):
            _ = self.environment.pop(key, None)
        _ = self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.origin))
        _ = self.git(self.root, "clone", str(self.origin), str(self.repository))
        _ = (self.repository / "code.txt").write_text("base\n")
        document = self.repository / "docs/production.md"
        document.parent.mkdir()
        _ = document.write_text("# Fixture production\n- **Merge branch:** integration\n")
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

    def migration_command(self, list_file: Path | str) -> list[str]:
        return [sys.executable, "-B", "-c", MIGRATE_WORKTREES, str(CHECKOUT), str(self.proc),
                "migrate", "--list", str(list_file)]

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

    def test_cleanup_and_end_landed_keep_an_enrolled_integration_and_its_refs(self) -> None:
        _ = self.git(self.repository, "switch", "main")
        doc = self.repository / "docs/fixture-production-live.md"
        enrolled_document(doc, self.repository, self.merge_tree, "integration")
        staff_file = Path(self.environment["STAFF_FILE"])
        fields = cast(dict[str, object], json.loads(staff_file.read_text()))
        runner = cast(list[dict[str, object]], fields["rows"])[0]
        cast(dict[str, object], runner["doc"])["path"] = str(doc)
        _ = staff_file.write_text(json.dumps(fields))
        _ = self.commit(self.merge_tree, "landed.txt", "landed\n")
        self.publish(self.merge_tree, "integration")
        record = self.record_path(self.merge_tree)
        record.parent.mkdir(parents=True)
        _ = record.write_text(json.dumps({
            "version": 1, "path": str(self.merge_tree), "repo": str(self.repository),
            "branch": "integration", "base": self.base, "merge_branch": "integration",
            "owner": "fixture-production", "made_at": datetime.now(UTC).isoformat(),
        }))
        refs = self.git(self.repository, "for-each-ref", "--format=%(refname) %(objectname)")
        remote = self.git(self.origin, "for-each-ref", "--format=%(refname) %(objectname)")
        cleanup = self.cleanup_result(self.merge_tree)
        self.assertEqual(cleanup.returncode, 1)
        self.assertIn("protected production", cleanup.stdout)
        kept, = self.finish_landed()
        self.assertEqual(kept["kind"], "Kept")
        self.assertIn("protected production", kept["detail"])
        self.assertTrue(record.is_file())
        self.assertEqual((self.merge_tree / "landed.txt").read_text(), "landed\n")
        self.assertEqual(self.git(self.repository, "for-each-ref", "--format=%(refname) %(objectname)"), refs)
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname) %(objectname)"), remote)

    def test_cargo_liner_record_update_reverses_only_the_original_main_targets(self) -> None:
        state = self.state / "made"
        state.mkdir(parents=True)
        other_repo = self.root / "other"
        _ = self.git(self.root, "init", "--initial-branch=main", str(other_repo))
        staff_file = Path(self.environment["STAFF_FILE"])
        fields = cast(dict[str, object], json.loads(staff_file.read_text()))
        runner = cast(list[dict[str, object]], fields["rows"])[0]
        runner["name"] = CARGO_LINER_PRODUCTION
        document = Path(cast(str, cast(dict[str, object], runner["doc"])["path"]))
        _ = document.write_text(f"- **Repository:** {self.repository}\n")
        _ = staff_file.write_text(json.dumps(fields))
        target_branch = f"{CARGO_LINER_PRODUCTION}-merges-integration"
        for name, repository, target in (("cargo", self.repository, "main"),
                                         ("integration", self.repository, target_branch),
                                         ("other", other_repo, "main")):
            path = self.trees / name
            _ = self.record_path(path).write_text(json.dumps({
                "version": 1, "path": str(path), "repo": str(repository),
                "branch": name, "base": self.base, "merge_branch": target,
                "owner": name, "made_at": datetime.now(UTC).isoformat(),
            }))
        receipt = self.root / "retarget.json"
        python = self.home / ".claude/scripts/lib/py"
        arguments = [str(python), str(WORKTREES), "cargo-liner-target"]
        for repository, switches, target, count in (
                (other_repo, [], "main", 0),
                (self.repository, [], target_branch, 1), (self.repository, [], target_branch, 0),
                (self.repository, ["--reverse"], "main", 1), (self.repository, ["--reverse"], "main", 0)):
            with self.subTest(repository=repository, reverse=bool(switches), count=count):
                result = self.command(self.repository, arguments + [str(repository), target_branch, str(receipt)] + switches)
                if repository == other_repo:
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("expected the cargo-liner production repository", result.stderr)
                    self.assertFalse(receipt.exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"updated {count}", result.stdout)
                for name, expected in (("cargo", target), ("integration", target_branch), ("other", "main")):
                    record = self.record(self.trees / name)
                    self.assertEqual(record["merge_branch"], expected)

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
        session_tree = self.add_linked_tree(main, self.root / "session-root/fixture-unit-probe", "session-probe")
        scratch_tree = self.add_linked_tree(main, self.root / "scratch/fixture-unit-probe", "scratch-probe")
        conflict = self.add_linked_tree(main, self.trees / "conflict", "branch-conflict")
        occupied = self.trees / "origin/fixture-unit/taken"
        occupied.mkdir(parents=True)
        list_file = self.root / "list"
        _ = list_file.write_text(f"{shlex.quote(str(named))} fixture-unit chosen\n{conflict} fixture-unit taken\n")
        check = self.migration_command(list_file)
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
        _ = list_file.write_text(f"{copy} fixture-unit copy\n{ci} fixture-unit ci\n"
                                 + f"{output} fixture-unit output\n{scratch} fixture-unit legacy\n")

        result = self.command(self.root, [*self.migration_command(list_file), "--dry-run"])
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
        command = self.migration_command(list_file)

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
        command = self.migration_command(list_file)
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
        command = self.migration_command(list_file)
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
        command = self.migration_command(os.devnull)
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
        command = self.migration_command(list_file)
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
                        self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=dry_run), 0)
                    self.assertIn(f"keep {old}: kept: {lock} held", output.getvalue())
                    self.assertNotIn(f"move {old} ->", output.getvalue())
                self.assertEqual(commands, ["show", "worktree-write-lock-status"] * 2)
                self.assertFalse(old.is_symlink())
            with redirect_stdout(StringIO()):
                self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 0)
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
        command = self.migration_command(os.devnull)
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

        command = self.migration_command(os.devnull)
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
        command = self.migration_command(list_file)
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
        command = self.migration_command(os.devnull)
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
                self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 0)
            pid, = pids
            keep = f"keep {old}: in use (pids {pid})"
            self.assertIn(keep, first.getvalue())
            self.assertFalse(new.exists())
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.PLANNED)
            for dry_run in (True, False):
                with redirect_stdout(held := StringIO()):
                    self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=dry_run), 0)
                self.assertIn(keep, held.getvalue())
                self.assertFalse(new.exists())

        with patch.dict(os.environ, self.environment, clear=True), redirect_stdout(resumed := StringIO()):
            self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 0)
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.FINISHED)
        self.assertIn(f"move {old} -> {new}", resumed.getvalue())
        self.assertEqual(resumed.getvalue().splitlines()[-1], "moved 1, kept in use 0, kept 1")
        self.assertEqual(old.readlink(), new)

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
        result = self.command(self.root, self.migration_command(list_file))
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
                self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=True), 0)
            with redirect_stdout(applied := StringIO()):
                self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 0)
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
        git = worktrees.git
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
            self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 0)
        self.assertEqual(events, ["scan", "rename"])
        for mark in marks:
            with mark.open("rb") as descriptor:
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_migration_keeps_a_checkout_at_the_build_unit_folder(self) -> None:
        self.layout_fixture()
        main = self.migration_main()
        old = self.add_linked_tree(main, self.root / "build/origin/fixture-unit", "source")
        new = self.trees / "origin/moved-unit/source"
        list_file = self.root / "list"
        _ = list_file.write_text(f"{old} moved-unit source\n")
        command = self.migration_command(list_file)
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
                self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 1)
            self.assertIn(f"keep {old}: in use: changed during copy", kept.getvalue())
            self.assertEqual((old / "code.txt").read_text(), "changed\n")
            self.assertFalse(old.is_symlink())
            self.assertFalse(new.exists())
            self.assertFalse(journals[0].staging.exists())
            with redirect_stdout(StringIO()):
                self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 0)
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
        command = self.migration_command(list_file)
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
            self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 1)
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
                command = self.migration_command(list_file)
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
                        self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 1)
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
        command = self.migration_command(list_file)

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
        run_git = worktrees.git
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
                        raise worktrees.Refused(1, "registration read failed")
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
                        raise worktrees.Refused(1, "subject read failed")
                    return run_git(repo, *arguments, check=check)

                environment = self.environment | {"WORKTREES_STATE_DIR": str(root / "state")}
                with (patch.dict(os.environ, environment, clear=True),
                      patch.object(worktrees, "_migration_journals", return_value=journals),
                      patch.object(Path, "is_dir", is_dir),
                      patch.object(worktrees, "linked_trees", side_effect=registrations),
                      patch.object(git_files, "head", side_effect=head),
                      patch.object(worktrees, "git", side_effect=git)):
                    with redirect_stdout(dry := StringIO()):
                        self.assertEqual(worktrees.migrate_worktrees(listed, dry_run=True), 0)
                    self.assertFalse(destination.exists())
                    with redirect_stdout(applied := StringIO()), redirect_stderr(StringIO()):
                        self.assertEqual(worktrees.migrate_worktrees(listed, dry_run=False), 1)

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
                        self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=True), 0)
                    self.assertFalse(clone_new.exists())
                    self.assertEqual(registration.read_bytes(), registration_before)
                    with redirect_stdout(applied := StringIO()), redirect_stderr(StringIO()):
                        self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), int(state == "foreign"))

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
        command = self.migration_command(list_file)
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

        result = self.command(self.root, self.migration_command(os.devnull))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(old.readlink(), new)
        self.assertTrue(journal.backup.is_dir())
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.FINISHED)

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

        command = self.migration_command(list_file)
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
                    self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=True), 0)
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
                _ = worktrees.migrate_worktrees(list_file, dry_run=True)

            with (patch.object(worktrees, "_migration_journals",
                               return_value=worktrees.MigrationJournalView((journal, history), {})),
                  redirect_stdout(output := StringIO())):
                self.assertEqual(worktrees.migrate_worktrees(list_file, dry_run=False), 0)
            self.assertEqual(output.getvalue().splitlines()[:-1], decisions[0])
            self.assertTrue(journal.destination.matches(new))

    @unittest.skipUnless(sys.platform == "linux", "requires /proc")
    def test_migration_keeps_a_clone_with_a_process_in_a_staged_linked_checkout(self) -> None:
        clone, list_file, journal = self.stage_linked_migration_member()
        old, new = journal.move.old, journal.move.new
        command = self.migration_command(list_file)
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
        command = self.migration_command(list_file)

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
        command = self.migration_command(list_file)
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

        result = self.command(self.root, self.migration_command(os.devnull))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(old.readlink(), new)
        relocated = new / "member"
        self.assertEqual(git_files.at(relocated).common_dir, new / ".git")
        self.assertIn(f"worktree {relocated}", self.git(new, "worktree", "list", "--porcelain"))
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(worktrees.read_move(worktrees.move_file(old)).stage, worktrees.MoveStage.FINISHED)
            record = worktrees.read_record(worktrees.record_file(relocated))
            self.assertEqual(record.path, str(relocated))
            self.assertEqual(record.repo, str(new))
            self.assertFalse(worktrees.record_file(member).exists())

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
        migration = self.command(self.root, [*self.migration_command(os.devnull), "--dry-run"])
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
        result = self.command(self.root, [*self.migration_command(os.devnull), "--dry-run"])
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
            listed[registered] = worktrees.ListedWorktree("fixture-unit", name)
            self.assertEqual(registered.resolve(strict=True), physical)
            self.assertIn(f"worktree {registered}", self.git(main, "worktree", "list", "--porcelain"))
        with patch.dict(os.environ, self.environment, clear=True):
            plan = worktrees.plan_worktree_migration(
                listed, worktrees.staff_record(), worktrees.MigrationFolders.configured(),
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

    def test_remove_lists_a_missing_checkout_before_reporting_output(self) -> None:
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
        self.assertEqual(notices.getvalue(), "")
        self.assertTrue(output.is_dir())

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
        for branch in ("checkpoint", "ancestor-with-earlier-target-date", "without-branch-config",
                       "protected-origin", "protected-origin-ahead", "protected-origin-unknown-commit"):
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
                protected = branch.startswith("protected-origin")
                if branch in ("protected-origin-ahead", "protected-origin-unknown-commit"):
                    writer = self.root / "writer"
                    _ = self.git(self.root, "clone", "--branch", branch, str(self.origin), str(writer))
                    advance = self.commit(writer, "remote-only.txt", "Only on origin.\n")
                    self.publish(writer, branch)
                    if branch == "protected-origin-ahead":
                        _ = self.git(self.repository, "fetch", "origin", branch)
                    else:
                        missing = self.command(self.repository, ["git", "cat-file", "-e", f"{advance}^{{commit}}"])
                        self.assertNotEqual(missing.returncode, 0)
                before = self.git(self.origin, "rev-parse", f"refs/heads/{branch}")
                if protected:
                    document = self.root / "other-production.md"
                    _ = document.write_text(f"- **Merge branch:** {branch}\n")
                    with patch.dict(os.environ, self.environment, clear=True):
                        identity = staff.StaffId(str(uuid4()))
                        _ = staff.ensure("showrunner", name="other", runtime_id=identity,
                                         doc=staff.LiveDoc(socket.gethostname().split(".", 1)[0], document))
                if branch != "protected-origin-unknown-commit":
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
                self.assertEqual(self.git(self.origin, "rev-parse", f"refs/heads/{branch}"), before)

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

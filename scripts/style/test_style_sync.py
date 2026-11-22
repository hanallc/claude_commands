"""Sync notices are best effort; lint records describe the exact requested tree."""

from __future__ import annotations

import fcntl
import io
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from types import ModuleType
from typing import ClassVar, TypedDict, cast, final, override
from unittest import mock

from scripts.copies import copies
from scripts.copies.copies_test_support import copies_environment
from scripts.direct import test_verify_lint_scope as verify_fixture
from scripts.lib import build_folders, scratch
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.lib.process_group import send_process_group
from scripts.lib.process_supervisor import ManagedProcess
from scripts.lib.process_supervisor import launch as launch_process
from scripts.lib.py_floor_test_support import interpreter_cache_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import send_budget, session_send
from scripts.message.test_send_budget import write_configs
from scripts.production import showrunners, staff
from scripts.production.cli_fixture import invoke_cli
from scripts.production.staff_test_support import fixture_unit_location, recorded_showrunner
from scripts.style import style_ledger, style_plan, style_rules, style_sync
from scripts.style.add_style import NoStyleProduction, standing_style_production
from scripts.style.ledger_test_support import style_staff_fixture, style_unit_id, ticket_owner_id, write_style_units
from scripts.style.style_child_test_support import unqueued_style_children
from scripts.style.style_config import StyleUnitAssignment
from scripts.style.style_plan_test_support import run_style_command
from scripts.style.style_reader_test_support import oracle_reader


def setUpModule() -> None:
    _ = unittest.enterModuleContext(oracle_reader())


INITIAL_FILES = {
    "Cargo.toml": '[package]\nname="sample"\nversion="0.1.0"\n',
    "src/lib.rs": "fn value() {}\n",
}
REAL_RUN = subprocess.run


class RecordedCall(TypedDict):
    arguments: list[str]


RECORDED_LINT = '''style-lint: mend start
+ env RUSTC_WRAPPER= cargo mend --all-targets --workspace --fail-on-warn
warning: import module instead of item
 --> src/lib.rs:1:1
  |
1 | use crate::values::Value;
  | ^^^^^^^^^^^^^^^^^^^^^^^^^
  = help: for further information visit https://github.com/natepiano/cargo-liner
style-lint: rustfmt start
+ cargo +nightly fmt --all --check
Diff in src/lib.rs:2:
style-lint: clippy start
+ cargo clippy --workspace --all-targets --all-features -- -D warnings
warning: prefer clearer names
 --> src/lib.rs:3:1
warning: prefer a type
 --> src/lib.rs:5:1
'''

CLEAN_LINT = ''.join(f"style-lint: {tool} start\nstyle-lint: {tool} passed\n"
                     for tool in ("mend", "rustfmt", "clippy"))

TEST_CHILD_STOP_SECONDS = 0.5

# Runs the lint in its own process, so a signal takes the real path through style_sync.
LINT_DRIVER = f'''import sys
from pathlib import Path
from scripts.style import style_sync
style_sync.VERIFY = Path(sys.argv[1])
style_sync.LINT_TEMP_PARENT = Path(sys.argv[2])
style_sync.CHILD_STOP_SECONDS = {TEST_CHILD_STOP_SECONDS}
style_sync.gate_owner = lambda repo: "sample-style"
'''


def process_state(pid: int) -> str:
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return state.stdout.strip() if state.returncode == 0 else ""


def running(pid: int) -> bool:
    state = process_state(pid)
    return bool(state) and not state.startswith("Z")


def wait_until(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def group_running(group: int) -> bool:
    # Earlier tests can enable adoption. Reap exited children before probing their group.
    while True:
        try:
            pid, _ = os.waitpid(-group, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break
    try:
        send_process_group(group, 0)
    except ProcessLookupError:
        return False
    return True


def kill_group(group: int) -> None:
    with suppress(ProcessLookupError):
        send_process_group(group, signal.SIGKILL)


def ended_pid() -> int:
    process = subprocess.Popen(["true"])
    _ = process.wait()
    return process.pid


@final
class StyleSyncTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    style: Path = Path()
    doc: Path = Path()
    tip: str = ""
    lint_temp: Path = Path()
    resolve_owner = staticmethod(style_sync.gate_owner)
    initial_repository: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.initial_repository = Path(cls.enterClassContext(
            tempfile.TemporaryDirectory(prefix="style-sync-seed-"))) / "sample"
        cls.initial_repository.mkdir()
        for relative, content in INITIAL_FILES.items():
            path = cls.initial_repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(content)
        for arguments in (
            ["init", "-q", "--initial-branch=main"],
            ["config", "user.name", "Sync tests"],
            ["config", "user.email", "sync@example.invalid"],
            ["add", "--all"],
            ["commit", "-qm", "Initial"],
        ):
            _ = subprocess.run(["git", "-C", str(cls.initial_repository), *arguments],
                               capture_output=True, text=True, check=True)

    @override
    def setUp(self) -> None:
        _ = self.enterContext(fake_cargo_output_roots(children=True))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="style-sync-test-")))
        self.repo = self.root / "sample"
        self.style = self.root / "sample-style"
        self.repo.mkdir()
        for relative, content in INITIAL_FILES.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            _ = path.write_text(content)
        _ = shutil.copytree(self.initial_repository / ".git", self.repo / ".git")
        _ = self.git("remote", "add", "origin", str(self.root / "unused-origin/sample"))
        self.tip = self.git("rev-parse", "HEAD")
        _ = self.git("update-ref", "refs/remotes/origin/main", self.tip)
        self.doc = self.style / "docs/sample-style-production.md"
        rule_root = self.root / "nate_style"
        (rule_root / "rust").mkdir(parents=True)
        for tool in ("mend", "clippy", "rustfmt"):
            _ = (rule_root / "rust" / f"{tool}.md").write_text(f"---\ntier: A\nmechanism: {tool}\n---\n# {tool}\n")
        environment = copies_environment(self.root)
        _ = write_config_fixture(Path(environment["CONF_DIR"]) / "build_folders.conf",
            "patterns.legacy-unique={purpose}{suffix}\n"
            + f"roots.build={self.root / 'build'}\nroots.build.natedev={self.root / 'build'}\n"
            + f"roots.workspace={self.root / 'worktrees'}\n")
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {
            **environment,
            "HOME": str(self.root / "gate-home"),
            **interpreter_cache_environment(self.root / "cache"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(rule_root),
            "STYLE_LEDGER_STATE_DIR": str(self.root / "state"),
            "SCRATCH_HOLDS": str(self.root / "scratch-holds.json"),
            "SCRATCH_ROOT": str(self.root / "scratch"),
            "SCRATCH_COPIES_ROOT": str(self.root / "copies"),
            "SCRATCH_VISIBLE_ROOT": str(self.root / "repro"),
            # A child's fresh gate copy goes in the test folder, so a killed child leaves nothing outside it.
            "TMPDIR": str(self.root),
        })))
        _ = os.environ.pop("CLAUDE_PYTHON", None)
        _ = self.enterContext(style_staff_fixture(self.root, self.repo))
        owner = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "sample-style")
        assert isinstance(owner.doc, staff.LiveDoc)
        _ = owner.doc.path.write_text("# Ticket owners\n")
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", rule_root))
        sender = self.root / "send.py"
        _ = sender.write_text(
            "import json,sys\nfrom pathlib import Path\n"
            + f"Path({str(self.root / 'sent.json')!r}).write_text(json.dumps({{'arguments':sys.argv[1:]}}))\n"
            + "print('SENT: fixture')\nraise SystemExit(0)\n",
        )
        address = self.root / "address.py"
        _ = address.write_text("print('session:fake-style')\n")
        _ = self.enterContext(mock.patch.object(session_send, "SEND_SCRIPT", sender))
        _ = self.enterContext(mock.patch.object(style_sync, "SHOWRUNNERS", address))
        _ = self.enterContext(mock.patch.object(showrunners, "own_name", return_value=""))
        self.lint_temp = self.root / "tmp"
        self.lint_temp.mkdir()
        _ = self.enterContext(mock.patch.object(style_sync, "LINT_TEMP_PARENT", self.lint_temp))
        _ = self.enterContext(mock.patch.object(style_sync, "gate_owner", return_value="sample-style"))
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=self.run_command))

    def run_command(self, command: list[str], *, capture_output: bool = False, text: bool = False,
                    check: bool = False, cwd: Path | str | None = None, env: dict[str, str] | None = None,
                    stdin: int | None = None, stdout: int | None = None, stderr: int | None = None,
                    encoding: str | None = None, errors: str | None = None,
                    input: str | bytes | None = None, timeout: float | None = None,
                    pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess[str]:
        """Run synchronous fixture commands and preserve the required child processes."""
        if pass_fds or isinstance(input, bytes):
            return REAL_RUN(command, capture_output=capture_output, text=text, check=check, cwd=cwd, env=env,
                            stdin=stdin, stdout=stdout, stderr=stderr, encoding=encoding, errors=errors,
                            input=input, timeout=timeout, pass_fds=pass_fds)
        script = Path(command[1]) if len(command) > 1 else Path()
        if (len(command) > 1 and command[0] in {str(style_sync.PYTHON), sys.executable}
                and script.parent == self.root and script.name in {"address.py", "send.py", "roster.py"}):
            module = ModuleType("style_sync_fixture_cli")
            module.__file__ = str(script)

            def main() -> int:
                exec(compile(script.read_text(), str(script), "exec"), {**vars(module), "__name__": "__main__"})
                return 0
        else:
            return run_style_command(self.root, self, command, capture_output=capture_output, text=text,
                                     check=check, cwd=cwd, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                                     encoding=encoding, errors=errors, input=input, timeout=timeout)
        result = invoke_cli(module, main, command[2:], dict(os.environ) if env is None else env,
                            Path.cwd() if cwd is None else Path(cwd), combined_output=stderr == subprocess.STDOUT)
        result.args = command
        if not capture_output:
            if stdout != subprocess.DEVNULL:
                print(result.stdout, end="")
            if stderr not in {subprocess.DEVNULL, subprocess.STDOUT}:
                print(result.stderr, end="", file=sys.stderr)
        if check:
            result.check_returncode()
        return result

    def git(self, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *arguments],
                              capture_output=True, text=True, check=True).stdout.strip()

    def production(self, branch: str = "main") -> None:
        _ = self.git("worktree", "add", "-q", "--detach", str(self.style))
        self.doc.parent.mkdir(parents=True)
        _ = self.doc.write_text(f"# Style\n- **Kind:** style\n- **Branch point:** {branch}\n" +
                                f"- **Merge branch:** style/main\n- **Showrunner checkout:** {self.style}\n")
        write_style_units(self.doc)
        owner = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "sample-style")
        assert isinstance(owner.doc, staff.LiveDoc)
        owner.doc.path.unlink()
        _ = recorded_showrunner(owner.name, self.doc)

    def direct_production(self) -> None:
        self.doc = self.repo / ".git/style-production.md"
        _ = self.doc.write_text(
            f"# Production\n- **Kind:** product\n- **Merge branch:** main\n- **Showrunner checkout:** {self.repo}\n")
        assignments = (StyleUnitAssignment("names", ("names", "values")),
                       StyleUnitAssignment("types", ("types", "modules")))
        write_style_units(self.doc, assignments)
        owner = recorded_showrunner("sample-style", self.doc)
        for assignment in assignments:
            worktree = self.root / f"direct-{assignment.unit}"
            _ = self.git("worktree", "add", "-qb", f"unit-{assignment.unit}", str(worktree))
            _ = fixture_unit_location(owner, assignment.unit, worktree)

    def notice(self, repo: Path | None = None) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_sync.main(["notify", "--repo-dir", str(repo or self.repo),
                                    "--branch", "refs/heads/main", "--tip", self.tip])
        return code, output.getvalue()

    def checkpoint_for_wake(self, *, on_style_branch: bool = True) -> None:
        rule = "global:rust/read-modules.md"
        _ = (self.root / "nate_style/rust/read-modules.md").write_text(
            "---\ntier: C\nfamily: modules\ninput_scope: file\n---\n# Modules\nReview module names.\n"
        )
        _ = self.git("branch", "style/main")
        if not on_style_branch:
            _ = (self.repo / "src/lib.rs").write_text("fn changed() {}\n")
            _ = self.git("commit", "-qam", "Unit checkpoint")
        checkpoint = self.git("rev-parse", "HEAD")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "style/main")
            ledger.ticket_add("3001", ticket_owner_id("modules"), "read", "modules", ".",
                              ["src/lib.rs"], [rule])
            ledger.ticket_assign("3001", "main")
            blob = ledger.pinned_rule_texts("3001")[0].blob
            ledger.record(rule, "src/lib.rs", checkpoint, blob, "clean", ticket="3001", worker="test")
            ledger.ticket_checkpoint("3001", checkpoint)

    def waiting_units(self, *units: str) -> None:
        with style_ledger.open_ledger(self.repo) as ledger:
            for unit in units:
                ledger.wait_for_sync(ticket_owner_id(unit))

    def remaining_waiters(self) -> list[str]:
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            rows = cast(list[tuple[str]], connection.execute("SELECT unit FROM sync_waits ORDER BY unit").fetchall())
        return [unit for (unit,) in rows]

    def wake_roster(self, units: tuple[str, ...] = ("names", "types")) -> None:
        entries: list[dict[str, str]] = [
            {"role": "unit director", "production": "sample-style", "unit": unit,
             "name": unit, "address": "session:fake-" + unit}
            for unit in units
        ]
        entries.extend([
            {"role": "unit director", "production": "another-style", "unit": "names",
             "name": "names", "address": "session:wrong-production"},
            {"role": "worker", "production": "sample-style", "unit": "names",
             "name": "names-impl", "address": "seat:wrong-worker"},
        ])
        roster = self.root / "roster.py"
        _ = roster.write_text(
            "import sys\nfrom pathlib import Path\n"
            + f"Path({str(self.root / 'roster-args')!r}).write_text(' '.join(sys.argv[1:]))\n"
            + f"print({json.dumps({'entries': entries, 'problems': []})!r})\n",
        )
        _ = self.enterContext(mock.patch.object(style_sync, "ROSTER", roster, create=True))

    def wake_sender(self, failed_unit: str = "") -> None:
        self.wake_roster()
        _ = (self.root / "send.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            + f"with Path({str(self.root / 'wakes.jsonl')!r}).open('a') as output:\n"
            + "    output.write(json.dumps(sys.argv[1:])+'\\n')\n"
            + f"if sys.argv[sys.argv.index('--to')+1] == {('session:fake-' + failed_unit)!r}:\n"
            + "    print('FAILED: unavailable'); raise SystemExit(3)\n",
        )

    def recorded_wakes(self) -> list[list[str]]:
        path = self.root / "wakes.jsonl"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def merge_for_wake(self) -> tuple[int, str, str]:
        # Keep the CLI's success/refusal routing real; replace only its process boundary.
        def wake_in_process(repo: Path, ticket: str) -> None:
            self.assertEqual(style_sync.main(["wake-waiters", "--repo", str(repo), "--ticket", ticket]), 0)

        output, errors = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(style_ledger, "_wake_sync_waiters", wake_in_process, create=True), mock.patch.object(
            sys, "argv", ["style_ledger.py", "ticket", "merge", "--repo", str(self.repo), "--id", "3001"],
        ), redirect_stdout(output), redirect_stderr(errors):
            try:
                style_ledger.main()
            except SystemExit as error:
                code = cast(int, error.code)
        return code, output.getvalue(), errors.getvalue()

    def test_own_handoff_syncs_the_merged_tip_without_lint(self) -> None:
        _ = self.git("checkout", "-qb", "style/main")
        _ = (self.repo / "src/lib.rs").write_text("fn corrected() {}\n")
        _ = self.git("commit", "-qam", "Style checkpoint")
        _ = self.git("checkout", "-q", "main")
        _ = self.git("merge", "--no-ff", "-qm", "Integrate style", "style/main")
        tip = self.git("rev-parse", "HEAD")
        self.assertNotEqual(tip, self.git("rev-parse", "style/main"))
        with mock.patch.object(style_sync, "lint") as lint, redirect_stdout(io.StringIO()):
            for branch in ("style/main", "refs/heads/style/main"):
                self.assertEqual(style_sync.main([
                    "own-handoff", "--repo", str(self.repo), "--tip", tip, "--style-branch", branch,
                ]), 0)
            lint.assert_not_called()
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(ledger.synced_commit(), tip)
            self.assertEqual(ledger.tip_seen(tip), "own hand-off")
            ledger.tip_done(tip)
            self.assertEqual(ledger.tip_seen(tip), "own hand-off")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM lint_runs").fetchone(), (0,))
        seen = subprocess.run([
            str(style_sync.PYTHON), str(Path(style_ledger.__file__)), "tip", "seen", "--repo", str(self.repo), "--tip", tip,
        ], capture_output=True, text=True, check=True)
        self.assertEqual(seen.stdout.strip(), "seen (own hand-off: synced with no lint run)")

    def test_own_handoff_leaves_a_different_tree_unprocessed(self) -> None:
        _ = self.git("branch", "style/main")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "style/main")
        _ = (self.repo / "src/lib.rs").write_text("fn changed() {}\n")
        _ = self.git("commit", "-qam", "Other change")
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(style_sync.main([
            "own-handoff", "--repo", str(self.repo), "--tip", tip, "--style-branch", "style/main",
        ]), 1)
        with style_ledger.open_ledger(self.repo) as ledger:
            self.assertEqual(ledger.synced_commit(), self.tip)
            self.assertIsInstance(ledger.tip_seen(tip), style_ledger.UnseenTip)

    def test_direct_own_tree_comparison_uses_the_unit_branch_and_records_main(self) -> None:
        self.direct_production()
        _ = self.enterContext(unqueued_style_children())
        worktree = self.root / "direct-names"
        rule = "global:rust/names.md"
        _ = (self.root / "nate_style/rust/names.md").write_text("---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Names\n")
        run = self.root / "run"

        def assign(ticket: str, source: str) -> None:
            with style_ledger.open_ledger(worktree) as ledger:
                ledger.ticket_add(ticket, ticket_owner_id("names"), "read", "names", ".", [source], [rule])
                ledger.ticket_assign(ticket, "unit-names")
                _ = ledger.ticket_base(ticket)
                head = style_sync.git(worktree, "rev-parse", "HEAD")
                blob = ledger.pinned_rule_texts(ticket)[0].blob
                ledger.record(rule, source, head, blob, "clean", ticket=ticket, worker="test")

        for case, interrupted in (("ordinary merge", False), ("stopped publication", True)):
            with self.subTest(case=case), mock.patch.object(
                    style_plan, "run_verification_step", return_value=subprocess.CompletedProcess([], 0)):
                with style_ledger.open_ledger(self.repo) as ledger:
                    ledger.set_tip(self.tip, "main")
                if interrupted:
                    assign("3001", "src/lib.rs")
                    with mock.patch.object(style_ledger.Ledger, "finish_publication",
                                           side_effect=RuntimeError("publication stopped after checkpoint record")):
                        with self.assertRaisesRegex(RuntimeError, "publication stopped after checkpoint record"):
                            _ = style_plan.publish_checkpoint(worktree, "3001", run, "1", "First checkpoint\n")
                    with style_ledger.open_ledger(worktree) as ledger:
                        self.assertIsInstance(ledger.pending_publication("refs/heads/unit-names"), style_ledger.PendingPublication)
                        self.assertEqual(ledger.ticket_get("3001")["state"], "checkpointed")
                    self.assertEqual(style_sync.git(worktree, "status", "--porcelain"), "")
                _ = (self.repo / "src/lib.rs").write_text(f"fn changed_{int(interrupted)}() {{}}\n")
                if interrupted:
                    _ = (self.repo / "src/later.rs").write_text("fn later() {}\n")
                    _ = self.git("add", "src/later.rs")
                _ = self.git("commit", "-qam", "Change main")
                tip = self.git("rev-parse", "HEAD")
                arguments = ["own-handoff", "--repo", str(self.repo), "--tip", tip,
                             "--style-branch", "main", "--unit", "names"]
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(style_sync.main(arguments), 0)
                with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
                    self.assertEqual(ledger.synced_commit(), tip)
                    self.assertEqual(connection.execute("SELECT style_branch FROM sync").fetchone(), ("refs/heads/main",))
                if interrupted:
                    self.assertEqual(style_sync.git(worktree, "merge-base", "HEAD", tip), tip)
                    _ = self.git("merge", "--no-edit", "unit-names")
                    with style_ledger.open_ledger(worktree) as ledger:
                        self.assertIsInstance(ledger.pending_publication("refs/heads/unit-names"), style_ledger.NoPendingPublication)
                        _ = ledger.ticket_merge("3001")
                    assign("3002", "src/later.rs")
                    checkpoint = style_plan.publish_checkpoint(worktree, "3002", run, "2", "Later checkpoint\n")
                    self.assertEqual(style_sync.git(worktree, "rev-parse", "HEAD"), checkpoint)
                    with style_ledger.open_ledger(worktree) as ledger:
                        self.assertEqual(ledger.ticket_get("3002")["checkpoint"], checkpoint)

    def test_processed_tip_does_not_skip_a_second_direct_units_main_merge(self) -> None:
        self.direct_production()
        _ = (self.repo / "src/lib.rs").write_text("fn changed() {}\n")
        _ = self.git("commit", "-qam", "Change main")
        tip = self.git("rev-parse", "HEAD")
        _ = style_sync.git(self.root / "direct-names", "merge", "--no-ff", "-qm", "Sync main", "main")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(tip, "main")
            ledger.tip_done(tip)
        arguments = ["own-handoff", "--repo", str(self.repo), "--tip", tip,
                     "--style-branch", "main", "--unit", "types"]
        self.assertEqual(self.git("rev-parse", "unit-types"), self.tip)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(style_sync.main(arguments), 0)
        self.assertNotEqual(self.git("rev-parse", "unit-types"), self.tip)
        self.assertEqual(self.git("merge-base", "unit-types", tip), tip)

    def test_direct_merge_refuses_assigned_or_dirty_work_and_repeats_the_open_ticket_proof(self) -> None:
        self.direct_production()
        worktree = self.root / "direct-names"
        _ = (self.root / "nate_style/rust/names.md").write_text("---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Names\n")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "main")
            rule = "global:rust/names.md"
            ledger.ticket_add("3001", ticket_owner_id("names"), "read", "names", ".", ["src/lib.rs"], [rule])
            ledger.ticket_assign("3001", "unit-names")
            _ = (self.repo / "main.txt").write_text("Main change.\n")
            _ = self.git("add", "main.txt")
            _ = self.git("commit", "-qm", "Change main")
            tip = self.git("rev-parse", "HEAD")
            arguments = ["own-handoff", "--repo", str(self.repo), "--tip", tip,
                         "--style-branch", "main", "--unit", "names"]
            self.assertEqual(style_sync.main(arguments), 4)
            self.assertEqual(self.git("rev-parse", "unit-names"), self.tip)
            blob = ledger.pinned_rule_texts("3001")[0].blob
            ledger.record(rule, "src/lib.rs", self.tip, blob, "clean", ticket="3001", worker="test")
            ledger.ticket_checkpoint("3001", self.tip)
            partial = worktree / "partial.rs"
            _ = partial.write_text("fn partial(\n")
            self.assertEqual(style_sync.main(arguments), 4)
            self.assertEqual(self.git("rev-parse", "unit-names"), self.tip)
            partial.unlink()
            pending = style_ledger.publication_path(worktree, "pending-sync")
            _ = pending.write_text(tip + "\n")
            original_proof = ledger.check_ticket_pipeline
            def prove(ticket: str) -> None:
                self.assertEqual(style_sync.git(worktree, "merge-base", "HEAD", tip), tip)
                with style_ledger.publication_path(worktree, "publication.lock").open("a") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                original_proof(ticket)
            with mock.patch.object(style_ledger.Ledger, "check_ticket_pipeline", side_effect=prove) as proof, redirect_stdout(io.StringIO()):
                self.assertEqual(style_sync.main(arguments), 0)
            proof.assert_called_once_with("3001")
            self.assertFalse(pending.exists())

    def test_direct_startup_reconciles_main_without_parent_lookup_or_handoff_replay(self) -> None:
        self.direct_production()
        with (mock.patch.object(style_sync, "runner_value") as lookup,
              mock.patch.object(style_sync, "replay_handoffs") as replay,
              redirect_stdout(io.StringIO())):
            self.assertEqual(style_sync.main([
                "resume", "--repo", str(self.repo), "--production", "names",
                "--branch-point", "unused", "--style-branch", "unused",
            ]), 0)
        lookup.assert_not_called()
        replay.assert_not_called()
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(ledger.synced_commit(), self.tip)
            self.assertEqual(connection.execute("SELECT branch_point FROM reconciliation").fetchone(), ("main",))
            self.assertEqual(connection.execute("SELECT style_branch FROM sync").fetchone(), ("refs/heads/main",))

    def test_send_handoff_uses_the_existing_path_without_route_discovery(self) -> None:
        self.direct_production()
        with (mock.patch.object(style_sync, "runner_value", return_value="") as lookup,
              mock.patch.object(style_sync, "sync_productions", side_effect=AssertionError("no route discovery")),
              redirect_stdout(io.StringIO())):
            self.assertEqual(style_sync.main([
                "send-handoff", "--repo", str(self.repo), "--production", "names", "--checkpoint", self.tip,
            ]), 0)
        lookup.assert_called_once_with("parent", "names")
        self.assertEqual(self.handoff_rows()[0][:3], (self.tip, "pending", ""))

    def test_unavailable_wait_owner_does_not_block_a_later_healthy_wait(self) -> None:
        self.production()
        self.waiting_units("names")
        self.wake_sender()
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection, connection:
            _ = connection.execute("INSERT INTO sync_waits (unit,since,token) VALUES ('missing','2000-01-01','bad-wait')")
        output = io.StringIO()
        with redirect_stdout(output):
            style_sync.wake_waiters(self.repo, "14")
        self.assertIn("style_ledger: wake missing:", output.getvalue())
        self.assertEqual(self.remaining_waiters(), ["missing"])
        self.assertEqual([arguments[1] for arguments in self.recorded_wakes()], ["session:fake-names"])

    def test_ticket_merge_wakes_two_waiters_once_and_clears_their_rows(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names", "types")
        self.wake_sender()
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(self.recorded_wakes(), [
            ["--to", "session:fake-" + unit, "--from", "sample-style", "--text",
             "ticket 3001 merged; refill.", "--timeout", "10"]
            for unit in ("names", "types")
        ])
        self.assertEqual(self.remaining_waiters(), [])
        self.assertEqual((self.root / "roster-args").read_text(), "--json")
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(len(self.recorded_wakes()), 2)

    def test_a_send_is_given_send_pys_whole_run_before_it_counts_as_timed_out(self) -> None:
        """The caller must allow delivery and relay cleanup before its timeout."""
        from scripts.lib import conf
        kill_grace = conf.read("send").number("kill_grace")
        self.assertGreater(send_budget.seconds(style_sync.SEND_SECONDS), style_sync.SEND_SECONDS + kill_grace)

    def test_handoff_budget_tracks_a_raised_cleanup_grace_without_reload(self) -> None:
        config = self.root / "send-budget-config"
        write_configs(config)
        _ = write_config_fixture(config / "postmaster.conf", "claim_grace_seconds=2.0\n")
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config)}):
            budgets: list[float] = []
            for grace in (10, 80):
                _ = write_config_fixture(config / "send.conf", f"kill_grace={grace}\n")
                with (mock.patch.object(style_sync, "runner_value", return_value="session:fixture"),
                      mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture")) as sender):
                    self.assertEqual(style_sync.handoff_notice("style", "parent", "checkpoint"), "sent")
                budgets.append(cast(float, sender.call_args.kwargs["timeout"]))
            self.assertEqual(budgets, [150, 290])
            self.assertGreater(budgets[1], style_sync.SEND_SECONDS + 80)

    def test_handoff_budget_tracks_raised_postmaster_wait_and_claim_grace_without_reload(self) -> None:
        config = self.root / "postmaster-budget-config"
        write_configs(config)
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config)}):
            budgets: list[float] = []
            for wait, grace in ((3, 2), (120, 40)):
                _ = write_config_fixture(config / "postmaster.conf",
                                         f"wait_seconds={wait}\nclaim_grace_seconds={grace}\n")
                with (mock.patch.object(style_sync, "runner_value", return_value="session:fixture"),
                      mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "SENT: fixture")) as sender):
                    self.assertEqual(style_sync.handoff_notice("style", "parent", "checkpoint"), "sent")
                budgets.append(cast(float, sender.call_args.kwargs["timeout"]))
            self.assertEqual(budgets[1] - budgets[0], 155)
            self.assertGreater(budgets[1], 120 + 40 + style_sync.SEND_SECONDS)

    def test_an_older_in_flight_wake_cannot_erase_the_units_new_wait(self) -> None:
        self.production()
        self.waiting_units("names")
        self.wake_sender()
        with style_ledger.open_ledger(self.repo) as ledger:
            first = ledger.sync_waits()[0]
        interleaved = False

        def refill_during_send(arguments: list[str], **options: object) -> object:
            nonlocal interleaved
            if (len(arguments) > 1 and arguments[1] == str(session_send.SEND_SCRIPT)
                    and options.get("timeout") == send_budget.seconds(style_sync.SEND_SECONDS) and not interleaved):
                interleaved = True
                style_sync.wake_waiters(self.repo, "3002")
                with style_ledger.open_ledger(self.repo) as ledger:
                    self.assertEqual(ledger.sync_waits(), ())
                    # Equal timestamps ensure that only a wait identity protects the new row.
                    with mock.patch.object(style_ledger, "_now", return_value=first.since):
                        ledger.wait_for_sync(ticket_owner_id("names"))
            return cast(object, mock.DEFAULT)

        with mock.patch.object(subprocess, "run", wraps=subprocess.run, side_effect=refill_during_send):
            style_sync.wake_waiters(self.repo, "3001")
        self.assertTrue(interleaved)
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])
        with style_ledger.open_ledger(self.repo) as ledger:
            second = ledger.sync_waits()[0]
        self.assertEqual((second.unit, second.since), (first.unit, first.since))
        self.assertNotEqual(second.token, first.token)
        self.assertEqual([call[5] for call in self.recorded_wakes()], [
            "ticket 3002 merged; refill.",
            "ticket 3001 merged; refill.",
        ])

    def test_failed_wake_keeps_only_that_waiter_and_an_already_merged_ticket_retries_it(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names", "types")
        self.wake_sender("names")
        self.assertEqual(self.merge_for_wake(), (0, f"style_ledger: wake {style_unit_id('names')}: send.py: stdout: FAILED: unavailable\n", ""))
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            self.assertEqual(connection.execute("SELECT state FROM tickets WHERE id='3001'").fetchone(), ("merged",))
        self.wake_sender()
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(self.remaining_waiters(), [])
        self.assertEqual([arguments[1] for arguments in self.recorded_wakes()], ["session:fake-names", "session:fake-types", "session:fake-names"])

    def test_merge_without_waiters_or_a_standing_production_sends_nothing(self) -> None:
        self.checkpoint_for_wake()
        self.waiting_units()
        self.production()
        self.wake_sender()
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.doc.unlink()
        self.waiting_units("names")
        self.assertEqual(self.merge_for_wake(), (0, "", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])

    def test_configured_direct_route_wakes_waiters_without_a_style_showrunner(self) -> None:
        self.direct_production()
        self.waiting_units("names", "types")
        self.wake_sender()
        self.assertIsInstance(standing_style_production(self.repo), NoStyleProduction)
        style_sync.wake_waiters(self.repo, "3001")
        self.assertEqual([arguments[1] for arguments in self.recorded_wakes()],
                         ["session:fake-names", "session:fake-types"])
        self.assertEqual(self.remaining_waiters(), [])

    def test_main_move_notifies_only_configured_direct_units(self) -> None:
        self.direct_production()
        self.wake_roster(("names", "types", "modules"))
        self.handoff_sender("SENT: fixture", 0)
        with mock.patch.object(style_sync, "runner_value") as lookup:
            self.assertEqual(self.notice(), (0, ""))
        lookup.assert_not_called()
        notices = self.handoff_notices()
        self.assertEqual([notice[1] for notice in notices], ["session:fake-names", "session:fake-types"])
        self.assertEqual([notice[notice.index("--key") + 1] for notice in notices],
                         ["style-sync:names", "style-sync:types"])
        self.assertTrue(all(f"branch point main moved to {self.tip[:7]}; sync." in notice
                            for notice in notices))
        style_sync.notify(self.repo, "another", self.tip)
        self.assertEqual(self.handoff_notices(), notices)

    def test_director_without_an_address_does_not_block_other_waiters(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names", "types")
        self.wake_sender()
        roster = self.root / "roster.py"
        _ = roster.write_text(roster.read_text().replace("session:fake-names", ""))
        self.assertEqual(self.merge_for_wake(), (0, f"style_ledger: wake {style_unit_id('names')}: no live director\n", ""))
        self.assertEqual(self.recorded_wakes(), [[
            "--to", "session:fake-types", "--from", "sample-style", "--text",
            "ticket 3001 merged; refill.", "--timeout", "10",
        ]])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])

    def test_merge_without_a_matching_director_keeps_the_wait_and_reports_why(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names")
        self.wake_sender()
        self.wake_roster(())
        self.assertEqual(self.merge_for_wake(), (0, f"style_ledger: wake {style_unit_id('names')}: no live director\n", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])

    def test_ambiguous_or_unreadable_roster_does_not_send_or_forget_the_wait(self) -> None:
        self.production()
        self.checkpoint_for_wake()
        self.waiting_units("names")
        self.wake_sender()
        self.wake_roster(("names", "names"))
        self.assertEqual(self.merge_for_wake(), (0, f"style_ledger: wake {style_unit_id('names')}: 2 directors\n", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])
        _ = (self.root / "roster.py").write_text("print('roster unavailable'); raise SystemExit(3)\n")
        self.assertEqual(self.merge_for_wake(), (0, f"style_ledger: wake {style_unit_id('names')}: roster: roster.py exited 3\n", ""))
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])

    def test_refused_checkpoint_merge_does_not_wake_or_clear_waiters(self) -> None:
        self.production()
        self.checkpoint_for_wake(on_style_branch=False)
        self.waiting_units("names")
        self.wake_sender()
        code, output, errors = self.merge_for_wake()
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("is not on refs/heads/style/main", errors)
        self.assertEqual(self.recorded_wakes(), [])
        self.assertEqual(self.remaining_waiters(), [style_unit_id("names")])

    def test_no_style_production_and_another_branch_point_are_silent(self) -> None:
        for retired in (False, True):
            with self.subTest(retired=retired):
                if retired:
                    doc = self.repo / "retired-production.md"
                    _ = doc.write_text(
                        f"# Production\n- **Kind:** product\n- **Merge branch:** main\n"
                        + f"- **Showrunner checkout:** {self.repo}\n")
                    write_style_units(doc, (StyleUnitAssignment(
                        "sample (retired 2026-11-09)", ("names", "values", "types", "modules"), 0),))
                    _ = recorded_showrunner("sample-parent", doc)
                with mock.patch.object(style_sync, "_waiting_directors",
                                       side_effect=AssertionError("no roster read")) as roster:
                    self.assertIsInstance(style_sync.direct_route(self.repo), style_sync.NoDirectRoute)
                    self.assertEqual(self.notice(), (0, ""))
                roster.assert_not_called()
        self.production("init/catalyst")
        self.assertEqual(self.notice(), (0, ""))
        self.assertFalse((self.root / "sent.json").exists())

    def test_found_production_sends_the_keyed_sync_message_to_its_current_address(self) -> None:
        self.production()
        for retired in (False, True):
            with self.subTest(retired=retired):
                if retired:
                    write_style_units(self.doc, (StyleUnitAssignment(
                        "sample (retired 2026-11-09)", ("names", "values", "types", "modules"), 0),))
                (self.root / "sent.json").unlink(missing_ok=True)
                with mock.patch("scripts.style.style_sync.subprocess.run", wraps=subprocess.run) as calls:
                    self.assertEqual(self.notice(), (0, ""))
                call = cast(RecordedCall, json.loads((self.root / "sent.json").read_text()))
                arguments = call["arguments"]
                self.assertEqual(arguments, [
                    "--to", "session:fake-style", "--from", "style_sync", "--text",
                    f"branch point main moved to {self.tip[:7]}; sync.",
                    "--key", "style-sync:sample-style", "--timeout", "10",
                ])
                self.assertLess(int(arguments[-1]), cast(int, calls.call_args_list[-1].kwargs["timeout"]))

    def test_linked_worktree_notification_finds_the_same_production(self) -> None:
        self.production()
        alias = self.root / "former-style"
        alias.symlink_to(self.style, target_is_directory=True)
        _ = recorded_showrunner("sample-style", alias / self.doc.relative_to(self.style))
        unit = self.root / "unit"
        _ = self.git("worktree", "add", "-qb", "unit", str(unit))
        self.assertEqual(self.notice(unit), (0, ""))
        call = cast(RecordedCall, json.loads((self.root / "sent.json").read_text()))
        self.assertIn("style-sync:sample-style", call["arguments"])

    def test_send_failure_reports_once_and_does_not_fail_the_merge(self) -> None:
        self.production()
        _ = (self.root / "send.py").write_text("print('FAILED: unavailable')\nraise SystemExit(3)\n")
        code, output = self.notice()
        self.assertEqual(code, 0)
        self.assertEqual(output, "style_sync: notify: send.py: stdout: FAILED: unavailable\n")

    def test_notify_requires_a_confirmed_success_outcome(self) -> None:
        self.production()
        cases = ((0, "SENT: fixture", True), (0, "SKIPPED: repeated", True),
                 (0, "KEPT: silence", True), (0, "REPLACED: revision", True),
                 (1, "QUEUED: offline", False), (3, "FAILED: unavailable", False),
                 (2, "REFUSED: invalid", False), (4, "HELD: decision", False), (5, "PENDING: active", False), (6, "RETRY: active", False),
                 (0, "", False), (0, "QUEUED: inconsistent", False),
                 (1, "SENT: inconsistent", False), (0, "FAILED: inconsistent", False))
        for code, text, accepted in cases:
            with self.subTest(code=code, text=text):
                _ = (self.root / "send.py").write_text(f"print({text!r})\nraise SystemExit({code})\n")
                result, output = self.notice()
                self.assertEqual(result, 0)
                if accepted:
                    self.assertEqual(output, "")
                else:
                    self.assertIn("style_sync: notify: send.py:", output)

    def test_notify_lookup_refusal_prints_one_prefix_and_keeps_the_merge_successful(self) -> None:
        with mock.patch.object(style_sync, "sync_productions",
                               side_effect=ValueError("style_sync: invalid production")):
            self.assertEqual(self.notice(), (0, "style_sync: notify: invalid production\n"))
        self.assertFalse((self.root / "sent.json").exists())

    def test_recorded_workspace_output_is_counted_by_tool_and_by_file(self) -> None:
        records = {record.tool: record for record in style_sync.lint_records(RECORDED_LINT, self.repo)}
        self.assertEqual({tool: record.diagnostics for tool, record in records.items()},
                         {"mend": {"src/lib.rs": 1}, "rustfmt": {"src/lib.rs": 1}, "clippy": {"src/lib.rs": 2}})
        self.assertTrue(all(not record.passed for record in records.values()))
        with self.assertRaises(TypeError):
            cast(dict[str, int], records["mend"].diagnostics)["src/lib.rs"] = 99
        self.assertEqual(records["mend"].diagnostics["src/lib.rs"], 1)
        incomplete = style_sync.lint_records(CLEAN_LINT.removesuffix("style-lint: clippy passed\n"), self.repo)
        self.assertFalse(next(record.passed for record in incomplete if record.tool == "clippy"))
        self.assertTrue(all(not record.passed for record in style_sync.lint_records("checks skipped\n", self.repo)))

    def test_workspace_verify_covers_all_members_and_reports_completed_tools(self) -> None:
        fixture = verify_fixture.VerifyLintScopeTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        result = fixture.verify("lint", "--no-cache")
        self.assertEqual(fixture.calls("+nightly"), [
            ["fmt", "--version"], ["fmt", "--all"],
        ])
        self.assertEqual(fixture.calls("clippy"), [
            ["--workspace", "--all-targets", "--all-features", "--keep-going", "--", "-D", "warnings"],
        ])
        self.assertEqual(fixture.calls("mend"), [["--all-targets", "--workspace", "--fix"]])
        self.assertTrue(all(record.passed for record in style_sync.lint_records(result.stdout, fixture.root)))

    def test_workspace_check_reports_without_fixing_and_continues_after_a_tool_fails(self) -> None:
        fixture = verify_fixture.VerifyLintScopeTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        cargo = fixture.root / "bin/cargo"
        fixture.write_executable(cargo, verify_fixture.CARGO_STUB + '''
if arguments[:1] == ["mend"]:
    if "--fix" in arguments:
        Path("src/lib.rs").write_text("rewritten by mend")
    print("warning: import module instead of item\\n --> src/lib.rs:1:1")
    raise SystemExit(1)
if arguments[:2] == ["+nightly", "fmt"] and "--version" not in arguments:
    if "--check" not in arguments:
        Path("src/lib.rs").write_text("rewritten by rustfmt")
    print("Diff in src/lib.rs:2:")
    raise SystemExit(1)
''')
        source = fixture.root / "src/lib.rs"
        _ = source.write_text("pub fn value()->u8{1}\n")
        before = source.read_bytes()
        result = fixture.invoke_verify("lint", "--check", "--no-cache", cwd=fixture.root)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(fixture.calls("mend"), [["--all-targets", "--workspace", "--fail-on-warn"]])
        self.assertEqual(fixture.calls("+nightly"), [["fmt", "--version"], ["fmt", "--all", "--check"]])
        self.assertEqual(fixture.calls("clippy"), [[
            "--workspace", "--all-targets", "--all-features", "--", "-D", "warnings",
        ]])
        records = {record.tool: record for record in style_sync.lint_records(result.stdout, fixture.root)}
        self.assertEqual(records["mend"].diagnostics, {"src/lib.rs": 1})
        self.assertEqual(records["rustfmt"].diagnostics, {"src/lib.rs": 1})
        self.assertFalse(records["mend"].passed)
        self.assertFalse(records["rustfmt"].passed)
        self.assertTrue(records["clippy"].passed)

    def test_lint_runs_workspace_gate_on_requested_tip_and_records_each_tool(self) -> None:
        verify = self.root / "verify.sh"
        _ = verify.write_text(
            '#!/bin/bash\n[[ "$*" == "lint --check --no-cache" ]] || exit 2\n'
            + f'git rev-parse HEAD > "{self.root / "checked-tip"}"\n'
            + "cat <<'OUTPUT'\n" + RECORDED_LINT + "OUTPUT\n",
        )
        # The style checkout can contain fixes that are not part of the requested synced tip.
        _ = (self.repo / "src/lib.rs").write_text("fn style_fix() {}\n")
        _ = self.git("commit", "-qam", "Style changes after tip")
        original_worktrees = self.git("worktree", "list", "--porcelain")
        with mock.patch.object(style_sync, "VERIFY", verify), redirect_stdout(io.StringIO()):
            self.assertEqual(style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo),
                                              "--tip", self.tip]), 1)
        self.assertEqual((self.root / "checked-tip").read_text().strip(), self.tip)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), self.worktrees_with_copy(original_worktrees))
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            rows = cast(list[tuple[str, str, int, str]], connection.execute(
                "SELECT tool,tip,passed,diagnostics_json FROM lint_runs ORDER BY tool",
            ).fetchall())
        self.assertEqual([(tool, tip, passed) for tool, tip, passed, _ in rows],
                         [(tool, self.tip, 0) for tool in ("clippy", "mend", "rustfmt")])
        self.assertEqual([cast(dict[str, int], json.loads(diagnostics)) for _, _, _, diagnostics in rows],
                         [{"src/lib.rs": 2}, {"src/lib.rs": 1}, {"src/lib.rs": 1}])

    def test_clean_check_mode_records_all_tools_passing_and_returns_zero(self) -> None:
        verify = self.root / "verify.sh"
        snapshot = self.standing_target().parent
        metadata = self.lint_temp / f"{style_sync.lint_prefix()}*"
        _ = verify.write_text(f"#!/bin/bash\ncp {metadata}/owner.json {self.root / 'owner-seen'}\n"
                              + f'test "$PWD" = "{snapshot}" || exit 3\n'
                              + f'test "$CARGO_BUILD_BUILD_DIR" = "{snapshot / "target"}" || exit 4\n'
                              + f'test "$CARGO_TARGET_DIR" = "{snapshot / "target"}" || exit 5\n'
                              + f"cp {metadata}/{style_sync.VERIFIER_FILE} {self.root / 'verifier-seen'}\n"
                              + f'ps -o pgid= -p "$$" > {self.root / "verify-group"}\ncat <<\'OUTPUT\'\n'
                              + CLEAN_LINT + "OUTPUT\n")
        with mock.patch.object(style_sync, "VERIFY", verify), redirect_stdout(io.StringIO()):
            self.assertEqual(style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo),
                                              "--tip", self.tip]), 0)
        self.assertEqual(cast(object, json.loads((self.root / "owner-seen").read_text())), {
            "owner": os.getpid(), "repo": self.git("rev-parse", "--path-format=absolute", "--git-common-dir"),
        })
        self.assertEqual((self.root / "verifier-seen").read_text(), (self.root / "verify-group").read_text().strip())
        self.assertTrue((snapshot / "target" / scratch.USE_STAMP).is_file())
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            rows = cast(list[tuple[str, int, str]], connection.execute(
                "SELECT tool,passed,diagnostics_json FROM lint_runs ORDER BY tool",
            ).fetchall())
        self.assertEqual(rows, [(tool, 1, "{}") for tool in ("clippy", "mend", "rustfmt")])

    def test_lint_rewrites_cannot_be_recorded_as_successful_coverage_of_the_original_tip(self) -> None:
        verify = self.root / "verify.sh"
        _ = verify.write_text("#!/bin/bash\necho 'fn rewritten() {}' > src/lib.rs\ncat <<'OUTPUT'\n"
                              + RECORDED_LINT + "OUTPUT\n")
        with mock.patch.object(style_sync, "VERIFY", verify), redirect_stdout(io.StringIO()):
            self.assertEqual(style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo),
                                              "--tip", self.tip]), 1)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            rows = cast(list[tuple[int]], connection.execute("SELECT passed FROM lint_runs").fetchall())
        self.assertEqual(rows, [(0,), (0,), (0,)])
        self.assertEqual((self.repo / "src/lib.rs").read_text(), "fn value() {}\n")

    def clean_verify(self, before: str = "") -> Path:
        verify = self.root / "verify.sh"
        _ = verify.write_text(f"#!/bin/bash\nmkdir -p target\n{before}cat <<'OUTPUT'\n" + CLEAN_LINT + "OUTPUT\n")
        return verify

    def run_lint(self, before: str = "") -> tuple[int, str]:
        output = io.StringIO()
        with mock.patch.object(style_sync, "VERIFY", self.clean_verify(before)), redirect_stdout(output), \
                redirect_stderr(output):
            code = style_sync.main(["lint", "--repo", str(self.repo), "--checkout", str(self.repo), "--tip", self.tip])
        return code, output.getvalue()

    @contextmanager
    def git_stub(self, body: str) -> Generator[None]:
        real_git = shutil.which("git")
        assert real_git is not None
        stub = self.root / "bin/git"
        stub.parent.mkdir(exist_ok=True)
        _ = stub.write_text("#!/bin/bash\n" + body + f'exec {shlex.quote(real_git)} "$@"\n')
        stub.chmod(0o755)
        with mock.patch.dict(os.environ, PATH=f"{stub.parent}{os.pathsep}{os.environ['PATH']}"):
            yield

    def stale_dir(self, name: str, owner: int | None, *, registered: bool, verifier_group: int = 0) -> Path:
        directory = self.lint_temp / f"{style_sync.lint_prefix()}{name}"
        directory.mkdir()
        if owner is not None:
            common_dir = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
            style_sync.OwnerRecord(owner, common_dir).write(directory)
        if verifier_group:
            _ = (directory / style_sync.VERIFIER_FILE).write_text(str(verifier_group))
        if registered:
            _ = self.git("worktree", "add", "--detach", str(directory / "checkout"), self.tip)
        return directory

    def worktrees_with_copy(self, original: str) -> str:
        copy = self.standing_target().parent
        return original + f"\n\nworktree {copy}\nHEAD {self.tip}\ndetached"

    def test_absent_checkout_keeps_recovery_marks_when_cargo_refuses_cleanup(self) -> None:
        directory = self.stale_dir("cargo-recovery", 0, registered=True)
        snapshot = directory / "checkout"
        task_mark, cargo_mark = scratch.removal_mark(snapshot), scratch.cargo_removal_mark(snapshot)
        scratch.atomic_record(cargo_mark, [])
        with mock.patch.object(scratch, "finish_removal", side_effect=OSError("stop after source deletion")):
            with redirect_stdout(io.StringIO()):
                self.assertFalse(style_sync.remove_lint_dir(self.repo, directory))
        self.assertFalse(snapshot.exists())
        self.assertTrue(task_mark.is_file())
        self.assertTrue(cargo_mark.is_file())
        with (mock.patch.object(scratch, "clean_cargo_outputs", side_effect=scratch.CargoOutputKept("Cargo refuses recovery")),
              redirect_stdout(io.StringIO()) as output):
            self.assertFalse(style_sync.remove_lint_dir(self.repo, directory))
        self.assertIn("Cargo refuses recovery", output.getvalue())
        self.assertTrue(task_mark.is_file())
        self.assertTrue(cargo_mark.is_file())
        self.assertTrue((directory / style_sync.OWNER_FILE).is_file())
        self.assertTrue(style_sync.remove_lint_dir(self.repo, directory))
        self.assertFalse(directory.exists())

    def standing_target(self) -> Path:
        return build_folders.copy_path("sample", "sample-style", 1) / "target"

    def test_standing_target_survives_task_end_and_reuses_artifacts(self) -> None:
        target = self.standing_target()
        code, output = self.run_lint('printf previous > target/artifact\n')
        self.assertEqual(code, 0, output)
        self.assertEqual(scratch.end("run:sample-style"), [])
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "artifact").read_text(), "previous")
        code, output = self.run_lint('[[ $(cat target/artifact) == previous ]] || exit 2\n')
        self.assertEqual(code, 0, output)
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_lint_keeps_intermediates_with_the_standing_warm_target(self) -> None:
        target = self.standing_target()
        code, output = self.run_lint(
            '[[ "$CARGO_BUILD_BUILD_DIR" == "$(realpath target)" ]] || exit 2\n'
            + 'printf warm > "$CARGO_BUILD_BUILD_DIR/intermediate"\n')
        self.assertEqual(code, 0, output)
        self.assertEqual((target / "intermediate").read_text(), "warm")
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_lint_keeps_its_own_warm_target_after_metadata_cleanup(self) -> None:
        target = self.standing_target()
        code, output = self.run_lint('[[ -d target && ! -L target ]] || exit 2\nprintf warm > target/artifact\n')
        self.assertEqual(code, 0, output)
        self.assertEqual((target / "artifact").read_text(), "warm")
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertNotIn(str(target), scratch.holds())
        # The copy overrides the caller's target and reuses the artifacts.
        with mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": "inherited-value"}):
            code, output = self.run_lint(
                '[[ -d target && ! -L target && $(cat target/artifact) == warm ]] || exit 2\n'
                + '[[ $CARGO_TARGET_DIR == "$(realpath target)" ]] || exit 3\n'
            )
        self.assertEqual(code, 0, output)
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertEqual(scratch.end("run:sample-style"), [])
        self.assertTrue(target.exists())
        self.assertEqual(scratch.holds(), {})

    def test_a_missing_or_swept_target_runs_cold(self) -> None:
        target = self.standing_target()
        for state in ("missing", "swept"):
            with self.subTest(state=state):
                self.assertFalse(target.exists())
                code, output = self.run_lint(
                    '[[ -d target && ! -L target && ! -e target/artifact ]] || exit 2\nprintf fresh > target/artifact\n'
                )
                self.assertEqual(code, 0, output)
                self.assertEqual((target / "artifact").read_text(), "fresh")
                self.assertEqual(list(self.lint_temp.iterdir()), [])
                shutil.rmtree(target)

    def test_lint_without_a_production_runs_in_the_configured_role_pool(self) -> None:
        observed = self.root / "fallback-checkout"
        with (mock.patch.object(style_sync, "gate_owner", self.resolve_owner),
              mock.patch.object(style_sync, "standing_style_production", return_value=NoStyleProduction(self.repo, "sample-style")),
              mock.patch.object(staff, "caller_identity", return_value="unregistered-caller"),
              mock.patch.object(showrunners, "own_name", return_value=""),
              mock.patch.object(copies, "fallback_owner", return_value="configured-style")):
            code, output = self.run_lint(f"pwd -P > {observed}\n")
        self.assertEqual(code, 0, output)
        checkout = Path(observed.read_text().strip())
        self.assertEqual(checkout, build_folders.copy_path("sample", "configured-style", 1))
        self.assertTrue(checkout.exists())
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertTrue((checkout / "target").is_dir())
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_lint_takes_second_checkout_when_first_is_busy(self) -> None:
        observed = self.root / "checked-directory"
        with copies.standing("sample-style", self.repo, copies.CommittedSource(self.tip)) as first:
            probe = """import fcntl
from pathlib import Path
tree = Path.cwd()
with tree.with_name(tree.name + '.lock').open('r+') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise SystemExit('lint released its checkout while verifying')
"""
            code, output = self.run_lint(
                f"pwd -P > {observed}\n{shlex.quote(str(Path(__file__).resolve().parents[1] / "lib/py"))} -c {shlex.quote(probe)} || exit $?\n")
            self.assertEqual(code, 0, output)
            second = Path(observed.read_text().strip())
            self.assertNotEqual(first, second)
            self.assertEqual(second, build_folders.copy_path("sample", "sample-style", 2))
            self.assertTrue((second / "target").is_dir())
            self.assertTrue(second.exists())
            self.assertEqual(build_folders.build_folder_state(second), "free")
            self.assertTrue((first / ".git").is_file())
            with second.with_name(second.name + ".lock").open("r+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(lock, fcntl.LOCK_UN)
        with copies.standing("sample-style", self.repo, copies.CommittedSource(self.tip)) as reused:
            self.assertEqual(reused, first)
            self.assertEqual((reused / "target").resolve(), self.standing_target())
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_redirecting_the_target_cannot_grant_successful_lint_coverage(self) -> None:
        outside = self.root / "outside-target"
        outside.mkdir()
        code, output = self.run_lint(f'rm -rf target\nln -s {outside} target\n')
        self.assertEqual(code, 1, output)
        self.assertIn("lint rewrote the checked tree", output)
        with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
            self.assertEqual(connection.execute("SELECT passed FROM lint_runs").fetchall(), [(0,), (0,), (0,)])

    def running_group(self) -> int:
        group = self.enterContext(launch_process(["sleep", "60"]))
        return group.pid

    @contextmanager
    def lint_driver(self, verify: Path, *, setup: str = "", keeper_setup: str = "",
                    capture_stdout: bool = False) -> Generator[ManagedProcess[str]]:
        if keeper_setup:
            setup += f'''
import subprocess

launch = subprocess.Popen

def launch_keeper_fixture(command, **options):
    if command[1:3] == ["-m", "scripts.style.lint_keeper"]:
        command = [command[0], "-c", {keeper_setup!r}, *command[3:]]
    return launch(command, **options)

subprocess.Popen = launch_keeper_fixture
'''
        driver = self.root / "lint_driver.py"
        _ = driver.write_text(LINT_DRIVER + setup + "raise SystemExit(style_sync.main(sys.argv[3:]))\n")
        python = Path(style_sync.__file__).resolve().parents[1] / "lib" / "py"
        # Keep the guardian open until the test checks the keeper's cleanup.
        process = self.enterContext(launch_process(
            [str(python), str(driver), str(verify), str(self.lint_temp), "lint", "--repo", str(self.repo),
             "--checkout", str(self.repo), "--tip", self.tip],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True,
        ))
        try:
            yield process
        finally:
            if process.poll() is None:
                process.kill()
                _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def wait_for_lint_marker(self, process: ManagedProcess[str], marker: Path) -> None:
        self.assertTrue(wait_until(lambda: marker.exists() or process.poll() is not None, TEST_HANG_GUARD_SECS),
                        f"Lint did not reach {marker.name}")
        if not marker.exists():
            _, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.fail(f"Lint ended before {marker.name}: {stderr}")

    def assert_lint_slot_and_folder_released(self) -> None:
        self.assertTrue(wait_until(lambda: not list(self.lint_temp.iterdir()), TEST_HANG_GUARD_SECS),
                        list(self.lint_temp.iterdir()))
        slot = self.standing_target().parent
        self.assertTrue(wait_until(lambda: build_folders.build_folder_state(slot) == "free", TEST_HANG_GUARD_SECS),
                        f"Lint still holds the slot at {slot}")

    def assert_stopped_lint_releases_resources(self, original_worktrees: str) -> None:
        self.assert_lint_slot_and_folder_released()
        self.assertEqual(self.git("worktree", "list", "--porcelain"), self.worktrees_with_copy(original_worktrees))

    def test_lint_returns_the_verifier_result_and_removes_its_background_child(self) -> None:
        marker = shlex.quote(str(self.root / "background-verifier-child"))
        verify = self.clean_verify(
            "sleep 300 &\n"
            + f'printf "%s %s\\n" "$!" "$(ps -o pgid= -p "$!")" > {marker}.part\n'
            + f"mv {marker}.part {marker}\n"
            + "printf '%s\\n' 'verifier output before exit'\nprintf '%s\\n' 'verifier error before exit' >&2\nexit 1\n"
        )
        with self.lint_driver(verify, capture_stdout=True) as caller:
            pids = self.root / "background-verifier-child"
            self.wait_for_lint_marker(caller, pids)
            child, group = (int(pid) for pid in pids.read_text().split())
            self.addCleanup(kill_group, group)
            # Start the bound after the verifier starts. The checkout can take longer.
            stdout, stderr = caller.communicate(timeout=15)
        self.assertEqual(caller.returncode, 1, stderr)
        self.assertIn("verifier output before exit\n", stdout)
        self.assertIn("verifier error before exit\n", stdout)
        self.assertFalse(running(child))
        self.assertFalse(group_running(group))
        self.assert_lint_slot_and_folder_released()

    def test_stopping_lint_during_slot_checkout_or_final_status_stops_git_and_releases_resources(self) -> None:
        slot = self.standing_target().parent
        for step, command in (("checkout", "checkout --force --detach"), ("status", "status --porcelain --")):
            with self.subTest(step=step):
                marker = self.root / f"blocked-git-{step}"
                quoted_marker = shlex.quote(str(marker))
                block = (
                    f'if [[ "$1" == -C && "$2" == {shlex.quote(str(slot))} && " $* " == *" {command} "* ]]; then\n'
                    + f'  printf "%s\\n" "$$" > {quoted_marker}.part\n  mv {quoted_marker}.part {quoted_marker}\n'
                    + f"  exec sleep {2 * TEST_HANG_GUARD_SECS}\nfi\n"
                )
                with self.git_stub(block), self.lint_driver(self.clean_verify()) as caller:
                    self.wait_for_lint_marker(caller, marker)
                    git_pid = int(marker.read_text())
                    try:
                        self.assertTrue(running(git_pid))
                        self.assertEqual(build_folders.build_folder_state(slot), "held")
                        self.assertTrue(any(path.is_dir() for path in self.lint_temp.iterdir()))
                        group = os.getpgid(git_pid)
                        caller.terminate()
                        _ = caller.wait(timeout=TEST_HANG_GUARD_SECS)
                        self.assertTrue(wait_until(lambda: not running(git_pid) and not group_running(group),
                                                   TEST_HANG_GUARD_SECS),
                                        f"The Git group still runs after the lint caller stopped during {step}")
                        self.assert_lint_slot_and_folder_released()
                    finally:
                        if running(git_pid):
                            with suppress(ProcessLookupError):
                                os.kill(git_pid, signal.SIGKILL)

    def test_stop_signals_remove_the_lint_dir_release_the_slot_and_end_verifier_processes(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        cases = ((signal.SIGTERM, False), (signal.SIGHUP, False), (signal.SIGINT, False),
                 (signal.SIGTERM, True), (signal.SIGKILL, False))
        for signum, pause_grandchild in cases:
            with self.subTest(signal=signum.name, pause_grandchild=pause_grandchild):
                pids = self.root / f"pids-{signum.name}-{pause_grandchild}"
                pause = 'kill -STOP "$!" || exit 1\n' if pause_grandchild else ""
                verify = self.clean_verify("sleep 300 &\n" + pause
                                           + f'echo "$$ $!" > {pids}.part && mv {pids}.part {pids}\nwait\n')
                with self.lint_driver(verify) as process:
                    self.wait_for_lint_marker(process, pids)
                    child, grandchild = (int(pid) for pid in pids.read_text().split())
                    self.addCleanup(kill_group, os.getpgid(child))
                    self.assertEqual(build_folders.build_folder_state(self.standing_target().parent), "held")
                    process.send_signal(signum)
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
                self.assert_stopped_lint_releases_resources(original_worktrees)
                self.assertTrue(wait_until(lambda: not running(child) and not running(grandchild), TEST_HANG_GUARD_SECS))

    def test_a_stopped_verifier_is_killed_after_the_term_grace_period(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        verifier_pid = self.root / "verifier-pid"
        signals = self.root / "verifier-signals"
        keeper_setup = f'''
import os
import sys
import time
from contextlib import suppress
from pathlib import Path
from scripts.lib import process_group
from scripts.style import lint_keeper

send = process_group.send_process_group

def record_verifier_signal(group, signum):
    verifier_pid = Path({str(verifier_pid)!r})
    with suppress(FileNotFoundError, ProcessLookupError):
        if group == os.getpgid(int(verifier_pid.read_text())):
            with Path({str(signals)!r}).open("a") as trace:
                print(int(signum), time.monotonic(), file=trace)
    send(group, signum)

process_group.send_process_group = record_verifier_signal
raise SystemExit(lint_keeper.main(sys.argv[1:]))
'''
        verify = self.clean_verify(f'echo "$$" > {verifier_pid}.part && mv {verifier_pid}.part {verifier_pid}\n'
                                  + "sleep 300 &\nwait\n")
        with self.lint_driver(verify, keeper_setup=keeper_setup) as process:
            self.wait_for_lint_marker(process, verifier_pid)
            verifier = int(verifier_pid.read_text())
            group = os.getpgid(verifier)
            self.addCleanup(kill_group, group)
            os.kill(verifier, signal.SIGSTOP)
            self.assertTrue(wait_until(lambda: process_state(verifier).startswith("T"), TEST_HANG_GUARD_SECS))
            process.terminate()
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assert_stopped_lint_releases_resources(original_worktrees)
        self.assertTrue(wait_until(lambda: not group_running(group), TEST_HANG_GUARD_SECS))
        term, killed = (line.split() for line in signals.read_text().splitlines())
        self.assertEqual(int(term[0]), signal.SIGTERM)
        self.assertEqual(int(killed[0]), signal.SIGKILL)
        self.assertGreaterEqual(float(killed[1]) - float(term[1]), TEST_CHILD_STOP_SECONDS)

    def test_stopping_the_caller_inside_verifier_launch_ends_the_whole_group(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        started = self.root / "verify-started"
        launched = self.root / "verify-group"
        verify = self.clean_verify(f"sleep 300 &\ntouch {started}\nwait\n")
        keeper_setup = f'''
import os
import select
import subprocess
import sys
from pathlib import Path
from scripts.style import lint_keeper

launch = subprocess.Popen

def pause_inside_verifier_launch(command, *arguments, **options):
    child = launch(command, *arguments, **options)
    if {str(verify)!r} in command:
        group = Path({str(launched)!r})
        partial = group.with_suffix(".part")
        partial.write_text(str(os.getpgid(child.pid)))
        partial.replace(group)
        select.select([], [], [], {2 * TEST_HANG_GUARD_SECS})
        raise RuntimeError("The fixture expected the caller to stop during launch")
    return child

subprocess.Popen = pause_inside_verifier_launch
raise SystemExit(lint_keeper.main(sys.argv[1:]))
'''
        with self.lint_driver(verify, keeper_setup=keeper_setup) as process:
            self.wait_for_lint_marker(process, launched)
            group = int(launched.read_text())
            self.addCleanup(kill_group, group)
            self.wait_for_lint_marker(process, started)
            self.assertTrue(group_running(group))
            process.terminate()
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assert_stopped_lint_releases_resources(original_worktrees)
        self.assertTrue(wait_until(lambda: not group_running(group), TEST_HANG_GUARD_SECS))

    def test_a_stop_signal_right_after_the_dir_is_made_still_removes_it(self) -> None:
        allocated = self.root / "lint-allocated"
        keeper_setup = f'''
import sys
from contextlib import contextmanager
from pathlib import Path
from scripts.lib import build_folders
from scripts.style import lint_keeper

temporary_build_folder = build_folders.temporary_build_folder

@contextmanager
def pause_after_allocation(request):
    with temporary_build_folder(request) as directory:
        Path({str(allocated)!r}).touch()
        sys.stdin.read()
        yield directory

build_folders.temporary_build_folder = pause_after_allocation
raise SystemExit(lint_keeper.main(sys.argv[1:]))
'''
        with self.lint_driver(self.clean_verify(), keeper_setup=keeper_setup) as process:
            self.wait_for_lint_marker(process, allocated)
            [directory] = [path for path in self.lint_temp.iterdir() if path.is_dir()]
            self.assertFalse((directory / style_sync.OWNER_FILE).exists())
            self.assertFalse((directory / "checkout").is_symlink())
            self.assertEqual(build_folders.build_folder_state(directory), "held")
            process.terminate()
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assert_lint_slot_and_folder_released()

    def test_lint_keeper_holds_the_directory_lease_before_verification(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        ready = self.root / "lint-ready"
        setup = f'''
def pause_before_verify(session):
    if session.read() != "ready\\n":
        raise RuntimeError("Lint keeper did not prepare the directory")
    Path({str(ready)!r}).touch()
    sys.stdin.read()
    raise RuntimeError("The fixture expected a stop before verification")

style_sync.verify_lint = pause_before_verify
'''
        with self.lint_driver(self.clean_verify(), setup=setup) as process:
            self.wait_for_lint_marker(process, ready)
            [directory] = [path for path in self.lint_temp.iterdir() if path.is_dir()]
            self.assertTrue(directory.with_name(directory.name + ".lock").is_file())
            lease_path = directory.with_name(directory.name + ".lease.json")
            lease = cast(dict[str, object], json.loads(lease_path.read_text()))
            self.assertEqual(set(lease), {"showrunner", "unit", "work", "purpose", "pid", "start_time"})
            self.assertIsInstance(lease["start_time"], str)
            self.assertTrue(running(cast(int, lease["pid"])))
            self.assertEqual(build_folders.build_folder_state(directory), "held")
            self.assertEqual(style_sync.OwnerRecord.read(directory).owner, process.pid)
            self.assertTrue((directory / "checkout").resolve().is_relative_to(self.root.resolve()))
            self.assertFalse((directory / style_sync.VERIFIER_FILE).exists())
            process.terminate()
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assert_stopped_lint_releases_resources(original_worktrees)

    def test_stopping_the_caller_during_cleanup_still_releases_the_slot_and_removes_the_dir(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        entered = self.root / "lint-cleanup"
        keeper_setup = f'''
import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from scripts.lib import build_folders
from scripts.style import lint_keeper

temporary_build_folder = build_folders.temporary_build_folder

@contextmanager
def stop_during_cleanup(request):
    with temporary_build_folder(request) as directory:
        try:
            yield directory
        finally:
            caller = int(sys.argv[1])
            if os.getppid() != caller:
                raise RuntimeError("Lint caller ended before cleanup")
            os.kill(caller, signal.SIGTERM)
            Path({str(entered)!r}).touch()

build_folders.temporary_build_folder = stop_during_cleanup
raise SystemExit(lint_keeper.main(sys.argv[1:]))
'''
        with self.lint_driver(self.clean_verify(), keeper_setup=keeper_setup) as process:
            self.wait_for_lint_marker(process, entered)
            _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
        self.assert_stopped_lint_releases_resources(original_worktrees)

    def test_standing_checkout_remains_registered_in_the_source(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        observed = self.root / "unregistered-checkout"
        code, output = self.run_lint(f"pwd -P > {observed}\n[[ -f .git ]] || exit 2\n")
        self.assertEqual(code, 0, output)
        checkout = Path(observed.read_text().strip())
        self.assertTrue(checkout.exists())
        self.assertEqual(build_folders.build_folder_state(checkout), "free")
        self.assertTrue(self.standing_target().is_dir())
        self.assertTrue((self.standing_target().parent / ".git").is_file())
        self.assertEqual(self.git("worktree", "list", "--porcelain"), self.worktrees_with_copy(original_worktrees))
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_sweep_removes_only_dead_runs_dirs_with_their_registrations(self) -> None:
        dead = ended_pid()
        live = self.stale_dir("live", os.getpid(), registered=True)
        verifier_running = self.stale_dir("verifier-running", dead, registered=True,
                                          verifier_group=self.running_group())
        unowned = self.stale_dir("unowned", None, registered=True)
        unreadable = self.stale_dir("unreadable", None, registered=False)
        _ = (unreadable / style_sync.OWNER_FILE).write_text("not a record\n")
        kept_worktrees = self.git("worktree", "list", "--porcelain")
        registered = self.stale_dir("registered", dead, registered=True, verifier_group=ended_pid())
        checkout_gone = self.stale_dir("checkout-gone", dead, registered=False)
        # A killed `worktree add`: its registration is still locked and the checkout has no `.git` yet.
        killed_add = self.stale_dir("killed-add", dead, registered=True)
        _ = self.git("worktree", "lock", "--reason", "initializing", str(killed_add / "checkout"))
        (killed_add / "checkout/.git").unlink()
        output = io.StringIO()
        with redirect_stdout(output):
            style_sync.sweep_stale_lint_dirs()
        self.assertEqual(output.getvalue().splitlines(), [
            f"style_sync: removed stale lint checkout {directory} (owner {dead} not running)"
            for directory in (checkout_gone, killed_add, registered)
        ])
        self.assertEqual(sorted(self.lint_temp.iterdir()), [live, unowned, unreadable, verifier_running])
        self.assertEqual(self.git("worktree", "list", "--porcelain"), kept_worktrees)

    def test_sweep_retries_a_query_that_still_holds_its_lock(self) -> None:
        directory = self.stale_dir("query", ended_pid(), registered=True)
        lock = directory.with_name(directory.name + ".lock")
        lock.touch()
        lease = directory.with_name(directory.name + ".lease.json")
        _ = lease.write_text("record")
        reached, release = threading.Event(), threading.Event()
        flock = fcntl.flock

        def pause_query(descriptor: int, mode: int) -> None:
            try:
                flock(descriptor, mode)
            except BlockingIOError:
                release.set()
                raise
            if threading.current_thread().name.startswith("probe") and mode == fcntl.LOCK_EX | fcntl.LOCK_NB:
                reached.set()
                if not release.wait(3):
                    raise AssertionError("the sweep did not meet the query")

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="probe") as workers:
            with mock.patch("fcntl.flock", side_effect=pause_query):
                query = workers.submit(build_folders.build_folder_state, directory)
                try:
                    self.assertTrue(reached.wait(3))
                    with redirect_stdout(io.StringIO()):
                        style_sync.sweep_stale_lint_dirs()
                finally:
                    release.set()
                self.assertEqual(query.result(timeout=3), "free")
        self.assertFalse(directory.exists())
        self.assertFalse(lock.exists())
        self.assertFalse(lease.exists())
        self.assertNotIn(str(directory / "checkout"), self.git("worktree", "list", "--porcelain"))

    def test_sweep_removes_free_folder_metadata_and_keeps_held_folder_metadata(self) -> None:
        for held in (False, True):
            with self.subTest(held=held):
                directory = self.stale_dir(f"lock-{held}", ended_pid(), registered=True)
                lock = directory.with_name(directory.name + ".lock")
                lease = directory.with_name(directory.name + ".lease.json")
                _ = lease.write_text(json.dumps({"pid": ended_pid(), "purpose": "sweep-test"}))
                with lock.open("w") as descriptor:
                    if held:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    entries = set(self.lint_temp.iterdir())
                    files = {entry: entry.read_bytes() for entry in (lock, lease, directory / style_sync.OWNER_FILE)}
                    original_worktrees = self.git("worktree", "list", "--porcelain")
                    with redirect_stdout(io.StringIO()):
                        style_sync.sweep_stale_lint_dirs()
                    if held:
                        self.assertEqual(set(self.lint_temp.iterdir()), entries)
                        self.assertTrue((directory / "checkout").is_dir())
                        self.assertEqual({entry: entry.read_bytes() for entry in files}, files)
                        self.assertEqual(self.git("worktree", "list", "--porcelain"), original_worktrees)
                        self.assertEqual(build_folders.build_folder_state(directory), "held")
                    else:
                        self.assertFalse(directory.exists())
                        self.assertFalse(lock.exists())
                        self.assertFalse(lease.exists())
                        self.assertNotIn(str(directory / "checkout"), self.git("worktree", "list", "--porcelain"))
                        self.assertEqual(build_folders.build_folder_state(directory), "free")

    def test_a_sweep_failure_prints_one_line_and_the_lint_run_continues(self) -> None:
        stuck = self.stale_dir("stuck", ended_pid(), registered=False)
        (stuck / "locked").mkdir()
        _ = (stuck / "locked/file").write_text("kept\n")
        (stuck / "locked").chmod(0o500)
        self.addCleanup((stuck / "locked").chmod, 0o700)
        code, output = self.run_lint()
        self.assertEqual(code, 0, output)
        sweep_lines = [line for line in output.splitlines() if line.startswith("style_sync: sweep ")]
        self.assertEqual(len(sweep_lines), 1, output)
        self.assertTrue(sweep_lines[0].startswith(f"style_sync: sweep {stuck}: [Errno 13] Permission denied"))
        self.assertTrue(stuck.exists())

    def test_a_git_launch_failure_in_the_sweep_keeps_the_record_and_the_lint_run_continues(self) -> None:
        stale = self.stale_dir("git-missing", None, registered=False)
        stale_repo = self.root / "stale-repo/.git"
        style_sync.OwnerRecord(ended_pid(), stale_repo).write(stale)
        real_git = style_sync.git

        def git_missing_for_stale_repo(repo: Path, *arguments: str) -> str:
            if repo == stale_repo:
                raise FileNotFoundError(2, "No such file or directory", "git")
            return real_git(repo, *arguments)

        with mock.patch.object(style_sync, "git", git_missing_for_stale_repo):
            code, output = self.run_lint()
        self.assertEqual(code, 0, output)
        self.assertIn(f"style_sync: kept {stale} for the next lint run; worktree registration: "
                      + "[Errno 2] No such file or directory: 'git'", output.splitlines())
        self.assertEqual(style_sync.OwnerRecord.read(stale).repo, stale_repo)

    def test_lint_folder_cleanup_needs_no_git_registration_read(self) -> None:
        original_worktrees = self.git("worktree", "list", "--porcelain")
        verifier_ran = self.root / "verify-ran"
        refused = self.root / "refused-git-registration-read"
        refuse = (f'if [[ -e {shlex.quote(str(verifier_ran))} && " $* " == *" worktree list "* ]]; then\n'
                  + f"  touch {shlex.quote(str(refused))}\n  exit 75\nfi\n")
        with self.git_stub(refuse), self.lint_driver(self.clean_verify(f"touch {shlex.quote(str(verifier_ran))}\n")) as process:
            _, stderr = process.communicate(timeout=TEST_HANG_GUARD_SECS)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertTrue(verifier_ran.exists())
        self.assertFalse(refused.exists())
        self.assertEqual(list(self.lint_temp.iterdir()), [])
        self.assertEqual(self.git("worktree", "list", "--porcelain"), self.worktrees_with_copy(original_worktrees))
        self.assertTrue((self.standing_target().parent / ".git").is_file())
        self.assertTrue(self.standing_target().is_dir())

    def test_lint_folder_is_removed_when_verify_raises(self) -> None:
        with mock.patch.object(style_sync, "verify_lint", side_effect=RuntimeError("verify failed")):
            with self.assertRaisesRegex(RuntimeError, "verify failed"):
                _ = style_sync.lint(self.repo, self.repo, self.tip)
        self.assertEqual(list(self.lint_temp.iterdir()), [])

    def test_a_verifier_wrapper_whose_owner_is_gone_exits_before_verify_starts(self) -> None:
        directory = self.root / "wrapper"
        directory.mkdir()
        started = self.root / "verify-started"
        verify = self.clean_verify(f"touch {started}\n")
        with launch_process(style_sync.verifier_command(directory, ended_pid(), verify=verify),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE) as ended:
            _ = ended.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertNotEqual(ended.returncode, 0)
        self.assertTrue((directory / style_sync.VERIFIER_FILE).exists())
        self.assertFalse(started.exists())
        with launch_process(style_sync.verifier_command(directory, os.getpid(), verify=verify),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE) as running_owner:
            _, stderr = running_owner.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(running_owner.returncode, 0, stderr)
        self.assertTrue(started.exists())

    def test_the_sweep_never_follows_a_checkout_link_or_a_lint_dir_link(self) -> None:
        dead = ended_pid()
        outside = self.root / "outside-checkout"
        outside.mkdir()
        _ = (outside / "kept").write_text("kept\n")
        linked = self.stale_dir("linked-checkout", dead, registered=False)
        (linked / "checkout").symlink_to(outside)
        planted_target = self.root / "outside-lint-dir"
        planted_target.mkdir()
        common_dir = Path(self.git("rev-parse", "--path-format=absolute", "--git-common-dir"))
        style_sync.OwnerRecord(dead, common_dir).write(planted_target)
        _ = (planted_target / "kept").write_text("kept\n")
        planted = self.lint_temp / f"{style_sync.lint_prefix()}planted"
        planted.symlink_to(planted_target)
        with redirect_stdout(io.StringIO()):
            style_sync.sweep_stale_lint_dirs()
        self.assertEqual((outside / "kept").read_text(), "kept\n")
        self.assertEqual((planted_target / "kept").read_text(), "kept\n")
        self.assertTrue((planted_target / style_sync.OWNER_FILE).exists())
        self.assertEqual(list(self.lint_temp.iterdir()), [planted])

    def handoff_checkpoint(self) -> str:
        _ = self.git("switch", "-qc", "style/main")
        _ = (self.repo / "src/lib.rs").write_text("fn corrected() {}\n")
        _ = self.git("commit", "-qam", "Style checkpoint")
        checkpoint = self.git("rev-parse", "HEAD")
        _ = self.git("switch", "-q", "main")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.set_tip(self.tip, "style/main")
        _ = (self.root / "address.py").write_text(
            "import sys\nprint('parent' if sys.argv[1] == 'parent' else 'session:fake-parent')\n",
        )
        _ = (self.root / "send.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            + f"with Path({str(self.root / 'notices.jsonl')!r}).open('a') as out:\n"
            + " out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            + "print('SENT: fixture')\n",
        )
        return checkpoint

    def handoff_sender(self, outcome: str, code: int) -> None:
        _ = (self.root / "send.py").write_text(
            "import json,sys\nfrom pathlib import Path\n"
            + f"with Path({str(self.root / 'notices.jsonl')!r}).open('a') as out:\n"
            + " out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            + f"print({outcome!r})\nraise SystemExit({code})\n",
        )

    def send_handoff(self, checkpoint: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_sync.main(["send-handoff", "--repo", str(self.repo), "--production", "sample-style",
                                    "--checkpoint", checkpoint])
        return code, output.getvalue()

    def handoff_rows(self) -> list[tuple[str, str, str, str, str]]:
        with style_ledger.open_ledger(self.repo) as ledger, closing(sqlite3.connect(ledger.database)) as connection:
            return cast(list[tuple[str, str, str, str, str]], connection.execute(
                "SELECT checkpoint,state,sent_to,created_at,updated_at FROM handoffs ORDER BY created_at,checkpoint",
            ).fetchall())

    def handoff_notices(self) -> list[list[str]]:
        path = self.root / "notices.jsonl"
        return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def resume_handoffs(self) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_sync.main(["resume", "--repo", str(self.repo), "--production", "sample-style",
                                    "--branch-point", "main", "--style-branch", "style/main"])
        return code, output.getvalue()

    def test_resume_reconciles_before_notice_and_sends_pending_handoff(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
            # This second row gives reconcile a visible transition before the notice.
            ledger.handoff_add(self.tip)
        output = io.StringIO()
        real_run = subprocess.run
        def observe_send(arguments: list[str], **options: object) -> object:
            if str(self.root / "send.py") in arguments:
                self.assertIn(f"hand-off {self.tip}: pending -> merged", output.getvalue())
                with closing(sqlite3.connect(self.root / "state/sample/ledger.sqlite")) as connection:
                    self.assertEqual(connection.execute("SELECT branch_point FROM reconciliation").fetchone(), ("main",))
            self.assertTrue(options.get("capture_output"))
            return cast(object, mock.DEFAULT)
        with mock.patch("scripts.style.style_sync.subprocess.run", wraps=real_run, side_effect=observe_send), redirect_stdout(output):
            code = style_sync.main(["resume", "--repo", str(self.repo), "--production", "sample-style",
                                    "--branch-point", "main", "--style-branch", "style/main"])
        self.assertEqual(code, 0)
        rows = {row[0]: row for row in self.handoff_rows()}
        self.assertEqual(rows[checkpoint][1:3], ("sent", "parent"))
        notices = self.handoff_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn(f"hand-off {checkpoint}", notices[0])
        self.assertIn("session:fake-parent", notices[0])

    def test_resume_replays_same_parent_without_touching_the_row(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
            ledger.handoff_send(checkpoint, "parent")
        before = self.handoff_rows()
        self.assertEqual(self.resume_handoffs()[0], 0)
        self.assertEqual(self.handoff_rows(), before)
        self.assertEqual(len(self.handoff_notices()), 1)

    def test_resume_reroutes_departed_parent_and_keeps_pending_age(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
            ledger.handoff_send(checkpoint, "departed")
        before = self.handoff_rows()[0]
        self.assertEqual(self.resume_handoffs()[0], 0)
        after = self.handoff_rows()[0]
        self.assertEqual(after[:4], (checkpoint, "sent", "parent", before[3]))
        self.assertNotEqual(after[4], before[4])
        self.assertEqual(len(self.handoff_notices()), 1)

    def test_resume_as_root_leaves_pending_until_branch_point_contains_it(self) -> None:
        checkpoint = self.handoff_checkpoint()
        _ = (self.root / "address.py").write_text("print('')\n")
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
        code, output = self.resume_handoffs()
        self.assertEqual(code, 0)
        self.assertIn(f"root: merge {checkpoint[:7]} with --root-self", output)
        self.assertEqual(self.handoff_rows()[0][1], "pending")
        self.assertEqual(self.handoff_notices(), [])
        _ = self.git("merge", "--no-ff", "-qm", "Integrate root handoff", "style/main")
        _ = self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        code, output = self.resume_handoffs()
        self.assertEqual(code, 0)
        self.assertIn(f"hand-off {checkpoint}: pending -> merged", output)
        self.assertEqual(self.handoff_rows()[0][1], "merged")
        self.assertEqual(self.handoff_notices(), [])

    def test_failed_resume_notice_leaves_pending_and_sent_destinations_unchanged(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
        _ = (self.root / "send.py").write_text("print('FAILED: unavailable')\nraise SystemExit(3)\n")
        for destination in ("", "departed"):
            if destination:
                with style_ledger.open_ledger(self.repo) as ledger:
                    ledger.handoff_send(checkpoint, destination)
            before = self.handoff_rows()
            code, output = self.resume_handoffs()
            self.assertEqual(code, 1)
            self.assertEqual(len(output.splitlines()), 1)
            self.assertIn("FAILED: unavailable", output)
            self.assertEqual(self.handoff_rows(), before)

    def test_acknowledgment_requires_resume_to_establish_branch_point(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
            ledger.handoff_send(checkpoint, "parent")
        _ = self.git("merge", "--no-ff", "-qm", "Integrate handoff", "style/main")
        merge = self.git("rev-parse", "HEAD")
        _ = self.git("update-ref", "refs/remotes/origin/main", merge)
        def acknowledge() -> subprocess.CompletedProcess[str]:
            return subprocess.run([str(style_sync.PYTHON), str(Path(style_ledger.__file__)), "handoff", "merged",
                                   "--repo", str(self.repo), "--checkpoint", checkpoint, "--merge-commit", merge],
                                  capture_output=True, text=True, check=False)
        refused = acknowledge()
        self.assertEqual(refused.returncode, 2)
        self.assertIn("reconcile", refused.stderr)
        self.assertEqual(self.resume_handoffs()[0], 0)
        accepted = acknowledge()
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(self.handoff_rows()[0][1], "merged")

    def test_send_handoff_sends_one_notice_and_records_the_current_parent(self) -> None:
        checkpoint = self.handoff_checkpoint()
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_sync.main(["send-handoff", "--repo", str(self.repo), "--production", "sample-style",
                                    "--checkpoint", checkpoint])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))
        notices = self.handoff_notices()
        self.assertEqual(len(notices), 1)
        self.assertIn("session:fake-parent", notices[0])
        self.assertIn(f"hand-off {checkpoint}", notices[0])

    def test_send_handoff_as_root_sends_nothing_and_leaves_the_row_pending(self) -> None:
        checkpoint = self.handoff_checkpoint()
        _ = (self.root / "address.py").write_text("print('')\n")
        output = io.StringIO()
        with redirect_stdout(output):
            code = style_sync.main(["send-handoff", "--repo", str(self.repo), "--production", "sample-style",
                                    "--checkpoint", checkpoint])
        self.assertEqual(code, 0)
        self.assertEqual(output.getvalue().strip(), f"root: merge {checkpoint[:7]} with --root-self")
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "pending", ""))
        self.assertEqual(self.handoff_notices(), [])

    def test_queued_send_handoff_is_sent_to_the_parent(self) -> None:
        """send.py queued a hand-off and the row stayed pending, so its merge was refused (cargo-liner, 2026-10-27)."""
        checkpoint = self.handoff_checkpoint()
        self.handoff_sender("checking recipient\nQUEUED: relay timed out", 1)
        self.assertEqual(self.send_handoff(checkpoint), (0, f"hand-off {checkpoint[:7]}: queued for parent\n"))
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))
        self.assertEqual(len(self.handoff_notices()), 1)

    def test_kept_handoff_notice_records_acceptance_and_reports_queued(self) -> None:
        checkpoint = self.handoff_checkpoint()
        self.handoff_sender("KEPT: until silence ends", 0)
        self.assertEqual(self.send_handoff(checkpoint), (0, f"hand-off {checkpoint[:7]}: queued for parent\n"))
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))
        self.assertEqual(len(self.handoff_notices()), 1)

    def test_handoff_without_a_confirmed_send_result_stays_pending(self) -> None:
        checkpoint = self.handoff_checkpoint()
        for output, code in (("", 0), ("unrelated output", 0), ("QUEUED: offline", 0),
                             ("SENT: apparent delivery", 1), ("FAILED: unavailable\nQUEUED: quoted result", 1),
                             ("REFUSED: action required", 2), ("HELD: review", 4), ("PENDING: active", 5)):
            with self.subTest(output=output, code=code):
                self.handoff_sender(output, code)
                result, diagnostic = self.send_handoff(checkpoint)
                self.assertEqual(result, 1)
                self.assertIn("style_sync: hand-off: send.py:", diagnostic)
                self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "pending", ""))

    def test_skipped_and_replaced_handoff_notices_record_parent_acceptance(self) -> None:
        checkpoint = self.handoff_checkpoint()
        for output in ("SKIPPED: acknowledged", "REPLACED: newer notice"):
            with self.subTest(output=output):
                self.handoff_sender(output, 0)
                self.assertEqual(self.send_handoff(checkpoint), (0, ""))
                self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))

    def test_undelivered_send_handoff_stays_pending(self) -> None:
        checkpoint = self.handoff_checkpoint()
        for outcome, code in (("FAILED: unavailable", 3), ("relay error", 1)):
            with self.subTest(outcome=outcome):
                self.handoff_sender(outcome, code)
                self.assertEqual(self.send_handoff(checkpoint), (1, f"style_sync: hand-off: send.py: stdout: {outcome}\n"))
                self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "pending", ""))

    def test_resume_records_a_queued_notice_as_sent(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with style_ledger.open_ledger(self.repo) as ledger:
            ledger.handoff_add(checkpoint)
        self.handoff_sender("QUEUED: relay timed out", 1)
        code, output = self.resume_handoffs()
        self.assertEqual(code, 0, output)
        self.assertIn(f"hand-off {checkpoint[:7]}: queued for parent\n", output)
        self.assertEqual(self.handoff_rows()[0][:3], (checkpoint, "sent", "parent"))

    def test_send_handoff_interrupted_after_notice_is_replayed_on_resume(self) -> None:
        checkpoint = self.handoff_checkpoint()
        with mock.patch.object(style_ledger.Ledger, "handoff_send", side_effect=RuntimeError("interruption")):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                _ = style_sync.main(["send-handoff", "--repo", str(self.repo), "--production", "sample-style",
                                     "--checkpoint", checkpoint])
        self.assertEqual(len(self.handoff_notices()), 1)
        self.assertEqual(self.handoff_rows()[0][1], "pending")
        self.assertEqual(self.resume_handoffs()[0], 0)
        self.assertEqual(len(self.handoff_notices()), 2)
        self.assertEqual(self.handoff_notices()[0], self.handoff_notices()[1])
        self.assertEqual(self.handoff_rows()[0][1:3], ("sent", "parent"))

    def test_resume_sends_multiple_pending_handoffs_oldest_first(self) -> None:
        first = self.handoff_checkpoint()
        _ = self.git("switch", "-q", "style/main")
        _ = (self.repo / "src/lib.rs").write_text("fn another_correction() {}\n")
        _ = self.git("commit", "-qam", "Next style checkpoint")
        second = self.git("rev-parse", "HEAD")
        _ = self.git("switch", "-q", "main")
        with style_ledger.open_ledger(self.repo) as ledger:
            with mock.patch.object(style_ledger, "_now", return_value="2026-01-02T00:00:00+00:00"):
                ledger.handoff_add(second)
            with mock.patch.object(style_ledger, "_now", return_value="2026-01-01T00:00:00+00:00"):
                ledger.handoff_add(first)
        self.assertEqual(self.resume_handoffs()[0], 0)
        texts = [notice[notice.index("--text") + 1] for notice in self.handoff_notices()]
        self.assertEqual(texts, [f"hand-off {checkpoint}" for checkpoint in (first, second)])
        self.assertEqual({row[1:3] for row in self.handoff_rows()}, {("sent", "parent")})


if __name__ == "__main__":
    _ = unittest.main()

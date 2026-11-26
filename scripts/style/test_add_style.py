"""Bootstrap a style production in disposable repositories without starting sessions."""

from __future__ import annotations

from scripts.lib.test_models_registry import write_registry

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from typing import Protocol, TypedDict, cast, final, override
from unittest import mock

from scripts.style import add_style, style_config
from scripts.style.ledger_test_support import write_style_units
from scripts.production import add_unit
from scripts.style import rules
from scripts.style import style_ledger
from scripts.style import unit_plans
from scripts.style import style_rules
from scripts.production.production_doc import production_field, read_production
from scripts.accounts.accounts import NamedAccount
from scripts.herdr import herdr
from scripts.production import change_review, production_doc, staff
from scripts.production.cli_fixture import invoke_cli
from scripts.production.add_showrunner_test_support import write_guard_proof
from scripts.production.production_names import GUARD_READY_FILE, LIVE_DOCUMENT_SUFFIX, MERGE_CHECKOUT_FIELD
from scripts.lib.atomic_file import create_text_atomic
from scripts.lib.git_test_support import fixture_git_environment
from scripts.production.staff_test_support import fixture_runtime_id, isolated_staff_file, recorded_showrunner

from scripts.style.style_reader_test_support import oracle_reader
from scripts.worktree import worktree_records, worktrees


def setUpModule() -> None:
    _ = unittest.enterModuleContext(oracle_reader())

SCRIPT = Path(add_style.__file__)
STUB = '''import json
import os
import sys
from pathlib import Path

from scripts.production.production_doc import read_new_production, read_production
from scripts.production.staff_test_support import recorded_showrunner, showrunner_relationship

arguments = sys.argv[1:]
name = Path(sys.argv[0]).name
with Path(os.environ["TEST_EVENTS"]).open("a") as events:
    events.write(json.dumps({"command": name, "arguments": arguments}) + "\\n")
state = Path(os.environ["SHOWRUNNER_STATE_DIR"])
if name == "style_sync.py":
    raise SystemExit(0)
elif name == "launch.py":
    production_name = arguments[arguments.index("--production") + 1]
    production = (read_new_production(Path(arguments[arguments.index("--doc") + 1]), production_name)
                  if "--doc" in arguments else read_production(production_name))
    doc = production.doc
    if "--doc" in arguments and os.environ.get("TEST_REQUIRE_ADMISSION"):
        from scripts.production import production_admission, production_doc
        production_doc.GUARD_REPOSITORY = Path.cwd()
        production_admission.require_new_document(production)
    if os.environ.get("TEST_BOOTSTRAP_AT_LAUNCH"):
        Path(os.environ["TEST_BOOTSTRAP_AT_LAUNCH"]).write_text(json.dumps({
            "production": doc.read_text(),
            "plans": {path.name: path.read_text() for path in (production.checkout / "docs/style-production/units").glob("*.md")},
        }))
    destination = next(line for line in doc.read_text().splitlines() if line.startswith("- **Proposals:**"))
    with Path(os.environ["TEST_LAUNCH_DESTINATIONS"]).open("a") as destinations:
        destinations.write(destination + "\\n")
    if os.environ.get("TEST_FAIL_LAUNCH") == "1":
        raise SystemExit(2)
    name = production.name
    owner = recorded_showrunner(name, doc)
    if os.environ.get("TEST_OPEN_SESSION_EXIT"):
        (Path(os.environ["TEST_LIVE_SESSIONS"]) / name).touch()
        raise SystemExit(int(os.environ["TEST_OPEN_SESSION_EXIT"]))
    timer = Path(os.environ["NOTIFIER_STATE_DIR"]) / ("showrunner-" + owner.id) / "conf"
    timer.parent.mkdir(parents=True, exist_ok=True)
    timer.write_text("TARGET=session:" + owner.id + "\\nCHECK=zsh /x/production_check.sh " + owner.id + "\\n")
elif arguments[0] == "detach":
    (state / arguments[1] / "parent").unlink()
    showrunner_relationship(arguments[1])
elif arguments[0] == "attach":
    parent = state / arguments[1] / "parent"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(arguments[2] + "\\n")
    showrunner_relationship(arguments[1], arguments[2])
else:
    raise SystemExit(2)
'''
BOOTSTRAP = '''import os
import sys
from pathlib import Path
from unittest import mock

from scripts.production import production_doc
from scripts.style import add_style

production_doc.GUARD_REPOSITORY = Path.cwd()
real_run_script = add_style._run_script
def run_script(script, *arguments):
    if script.name == "style_sync.py":
        script = Path(os.environ["TEST_STYLE_SYNC"])
    return real_run_script(script, *arguments)

with mock.patch.object(add_style, "_run_script", side_effect=run_script), mock.patch.object(
    add_style, "herdr_tab_open", side_effect=lambda name, ending:
    (Path(os.environ["TEST_LIVE_SESSIONS"]) / name).exists()
):
    raise SystemExit(add_style.main(sys.argv[2:]))
'''


class ScriptRunner(Protocol):
    def __call__(self, script: Path, *arguments: str) -> None: ...


class ScriptCall(TypedDict):
    command: str
    arguments: list[str]


class BootstrapAtLaunch(TypedDict):
    production: str
    plans: dict[str, str]


@final
class AddStyleTests(unittest.TestCase):
    root: Path = Path()
    repo: Path = Path()
    checkout: Path = Path()
    parent: Path = Path()
    doc: Path = Path()
    initial_tip: str = ""
    merge_name = "fixture-merges"
    style_branch = "fixture-merges-integration-sample-style"
    env: dict[str, str] = {}

    repository_seed: Path = Path()

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.repository_seed = Path(cls.enterClassContext(
            tempfile.TemporaryDirectory(prefix="add-style-seed-"),
        )) / "sample"
        fixture = cls(methodName="runTest")
        fixture.repo = cls.repository_seed
        fixture.env = {**fixture_git_environment(fixture.repo.parent), "GIT_CONFIG_SYSTEM": os.devnull}
        fixture.prepare_repository()
        cls.initial_tip = fixture.initial_tip

    def prepare_repository(self) -> None:
        self.repo.mkdir()
        _ = self.git("init", "-q", "--initial-branch=main")
        _ = self.git("config", "user.name", "Style bootstrap tests")
        _ = self.git("config", "user.email", "style-tests@example.invalid")
        _ = (self.repo / "Cargo.toml").write_text('[package]\nname = "sample"\nversion = "0.1.0"\nedition = "2021"\n')
        (self.repo / "src").mkdir()
        _ = (self.repo / "src/lib.rs").write_text("pub fn value() -> u32 { 1 }\n")
        local = self.repo / "docs/style/local.md"
        local.parent.mkdir(parents=True)
        _ = local.write_text("---\ntier: C\nfamily: names\ninput_scope: file\n---\n# Local rule\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-q", "-m", "Create sample repository")
        self.initial_tip = self.git("rev-parse", "HEAD")

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="add-style-")))
        self.repo = self.root / "sample"
        home = self.root / "home"
        self.checkout = home / "worktrees" / self.repo.name / self.merge_name / "integration-sample-style"
        self.doc = self.repo / "docs" / f"sample-style-production{LIVE_DOCUMENT_SUFFIX}"
        _ = shutil.copytree(self.repository_seed, self.repo)
        config = home / ".claude/config"
        config.mkdir(parents=True)
        _ = (config / "build_folders.conf").write_text(
            f"roots.workspace={home / 'worktrees'}\nroots.build={self.root / 'build'}\n"
            + "# one of: report, enforce\nfolders.check=report\n"
            + "# range: 0.001..1\nlocks.probe_wait_seconds=0.01\n"
            + "# range: 0.001..1\nlocks.probe_retry_seconds=0.005\n")
        copies_config = config / "copies.conf"
        _ = copies_config.write_text("# range: 0.001..\ngit_timeout_seconds=10\n")
        rule_root = self.root / "nate_style"
        (rule_root / "rust").mkdir(parents=True)
        for family in add_style.FAMILIES:
            _ = (rule_root / "rust" / f"{family}.md").write_text(
                f"---\ntier: C\nfamily: {family}\ninput_scope: file\n---\n# {family}\n",
            )
        for name in ("launch.py", "showrunners.py", "style_sync.py"):
            _ = (self.root / name).write_text(STUB)
        (self.root / "live-sessions").mkdir()
        self.env = {
            **fixture_git_environment(self.root),
            "GIT_CONFIG_SYSTEM": os.devnull,
            "HOME": str(home),
            "COPIES_CONFIG": str(copies_config),
            "WORKTREES_STATE_DIR": str(self.root / "worktree-records"),
            "STYLE_HISTORY_NATE_STYLE_DIR": str(rule_root),
            "STYLE_LEDGER_STATE_DIR": str(self.root / "ledger"),
            "SHOWRUNNER_STATE_DIR": str(self.root / "showrunners"),
            "NOTIFIER_STATE_DIR": str(self.root / "notifier"),
            "ADD_STYLE_LAUNCHER": str(self.root / "launch.py"),
            "ADD_STYLE_SHOWRUNNERS": str(self.root / "showrunners.py"),
            "TEST_EVENTS": str(self.root / "events.jsonl"),
            "TEST_LAUNCH_DESTINATIONS": str(self.root / "launch-destinations.txt"),
            "TEST_PARENT_SLUG": "parent",
            "TEST_LIVE_SESSIONS": str(self.root / "live-sessions"),
            "TEST_STYLE_SYNC": str(self.root / "style_sync.py"),
            "ROSTER_RUN_ROOT": str(self.root / "roster"),
            "UNIT_RUN_ROOT": str(self.root / "units"),
            "SCRATCH_ROOT": str(self.root / "scratch"),
        }
        _ = self.git("remote", "add", "origin", str(self.repo.with_suffix(".git")))
        _ = self.enterContext(isolated_staff_file(self.root / "staff/staff.json", self.env))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.env, clear=True)))
        _ = self.enterContext(mock.patch.object(style_rules, "NATE_STYLE_DIR", rule_root))
        self.parent = self.parent_doc("parent")
        write_guard_proof(self.root, Path(self.env["SHOWRUNNER_STATE_DIR"]), self.repo)
        self.checkout.parent.mkdir(parents=True)
        _ = self.git("worktree", "add", "-qb", self.style_branch, str(self.checkout), "main")

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments], check=True, text=True, capture_output=True, env=self.env,
        ).stdout.strip()

    def parent_doc(self, name: str, branch: str = "main", *, proposals: bool = True) -> Path:
        path = self.root / f"{name}-production.md"
        fields = [
            f"# Production — {name}", "", "## Production Context", "",
            f"- **Repository:** `{self.repo}`",
            f"- **Merge branch:** `{branch}`",
            f"- **Showrunner checkout:** `{self.repo}`",
            f"- **Log:** docs/{name}-log.md",
            "- **Merge tests:** `sample`, `support` — packages tested on every merge",
        ]
        if proposals:
            fields.append(f"- **Proposals:** `{self.root / (name + '-issues')}`")
        _ = path.write_text("\n".join([*fields, ""]))
        owner = recorded_showrunner(name, path)
        if proposals:
            staging = self.root / name / "staging"
            staging.mkdir(parents=True)
            _ = staff.ensure("unit", name=self.merge_name, parent=staff.ReportsTo(owner.id),
                             worktree=staff.PresentWorktree("natedev", staging), unit_type="merge",
                             runtime_id=fixture_runtime_id(f"{name}/{self.merge_name}"))
        timer = self.root / "notifier" / f"showrunner-{owner.id}" / "conf"
        timer.parent.mkdir(parents=True, exist_ok=True)
        _ = timer.write_text(f"TARGET=session:{owner.id}\nCHECK=zsh /x/production_check.sh {owner.id}\n")
        return path

    def _timer_conf(self) -> Path:
        owner = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "sample-style")
        return self.root / "notifier" / f"showrunner-{owner.id}" / "conf"

    def parent_document(self) -> Path:
        record = staff.snapshot()
        child = next(row for row in record.rows if row.name == "sample-style")
        assert isinstance(child.parent, staff.ReportsTo)
        parent = record.parent_of(child)
        assert parent is not None and isinstance(parent.doc, staff.LiveDoc)
        return parent.doc.path

    def bootstrap_arguments(self, arguments: tuple[str, ...], parent: Path | None) -> list[str]:
        production = next(row.name for row in staff.snapshot().find(kind="showrunner")
                          if isinstance(row.doc, staff.LiveDoc) and row.doc.path == (parent or self.parent))
        return ["--production", production, "--model", "test-model", "--effort", "high", *arguments]

    def run_bootstrap(self, *arguments: str, parent: Path | None = None, success: bool = True) -> subprocess.CompletedProcess[str]:
        argv = self.bootstrap_arguments(arguments, parent)
        real_run_script = cast(ScriptRunner, getattr(add_style, "_run_script"))

        def run_script(script: Path, *script_arguments: str) -> None:
            if script.name == "style_sync.py":
                script = Path(self.env["TEST_STYLE_SYNC"])
            real_run_script(script, *script_arguments)

        def live_session_open(name: str, ending: object) -> bool:
            del ending
            return (Path(self.env["TEST_LIVE_SESSIONS"]) / name).exists()

        with (
            mock.patch.object(production_doc, "GUARD_REPOSITORY", self.repo),
            mock.patch.object(add_style, "LAUNCHER", Path(self.env["ADD_STYLE_LAUNCHER"])),
            mock.patch.object(add_style, "SHOWRUNNERS", Path(self.env["ADD_STYLE_SHOWRUNNERS"])),
            mock.patch.object(add_style, "_run_script", run_script),
            mock.patch.object(add_style, "herdr_tab_open", live_session_open),
        ):
            result = invoke_cli(add_style, lambda: add_style.main(argv), argv, self.env, self.repo,
                                production_state=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def run_bootstrap_process(self, *arguments: str, parent: Path | None = None, success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(Path(__file__).resolve().parents[1] / "lib/py"), "-c", BOOTSTRAP, str(SCRIPT), *self.bootstrap_arguments(arguments, parent)],
            env=self.env, cwd=self.repo, capture_output=True, text=True, check=False,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def calls(self) -> list[ScriptCall]:
        path = self.root / "events.jsonl"
        return [cast(ScriptCall, json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []

    def resume_call(self) -> ScriptCall:
        return {"command": "style_sync.py", "arguments": [
            "resume", "--repo", str(self.checkout), "--production", "sample-style",
            "--branch-point", "main", "--style-branch", self.style_branch,
        ]}

    def sync(self) -> tuple[str, str, str]:
        with closing(sqlite3.connect(self.root / "ledger/sample/ledger.sqlite")) as connection:
            return cast(tuple[str, str, str], connection.execute("SELECT tip,style_branch,synced_at FROM sync").fetchone())

    def add_workspace_packages(self) -> None:
        manifest = self.repo / "Cargo.toml"
        _ = manifest.write_text(manifest.read_text() + '\n[workspace]\nmembers = ["crates/handler", "crates/port"]\n')
        for directory, package in (("handler", "cargo-handler"), ("port", "cargo-port")):
            member = self.repo / "crates" / directory
            (member / "src").mkdir(parents=True)
            _ = (member / "Cargo.toml").write_text(f'[package]\nname = "{package}"\nversion = "0.1.0"\n')
            _ = (member / "src/lib.rs").write_text("pub fn member() {}\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-q", "-m", "Add workspace packages")

    def interrupted_bootstrap_publish(self, path: Path) -> None:
        real_replace = os.replace

        def interrupted_replace(source: str | Path, destination: str | Path, *,
                                src_dir_fd: int | None = None, dst_dir_fd: int | None = None) -> None:
            if Path(destination) == path:
                raise OSError("stopped before publish")
            real_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with mock.patch.object(os, "replace", interrupted_replace):
            stopped = self.run_bootstrap(success=False)
        self.assertEqual(stopped.returncode, 2, stopped.stderr)
        self.assertIn("stopped before publish", stopped.stderr)
        self.assertFalse(path.exists())
        finished = self.run_bootstrap()
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(read_production("sample-style").merge_branch, self.style_branch)
        self.assertEqual(len(list((self.checkout / add_style.PLAN_DIRECTORY).glob("*.md"))), 4)
        self.assertIn("## Phases", (self.checkout / add_style.PLAN_DIRECTORY / "sample-style-names-unit.md").read_text())
        self.assertEqual(sum(call["command"] == "launch.py" for call in self.calls()), 1)

    def test_interrupted_first_production_doc_publish_reruns_bootstrap(self) -> None:
        self.interrupted_bootstrap_publish(self.doc)

    def test_interrupted_first_unit_plan_publish_reruns_bootstrap(self) -> None:
        self.interrupted_bootstrap_publish(self.checkout / add_style.PLAN_DIRECTORY / "sample-style-names-unit.md")

    def test_pilot_units_and_package_scopes_are_ready_before_the_showrunner_launches(self) -> None:
        self.add_workspace_packages()
        snapshot = self.root / "bootstrap-at-launch.json"
        self.env["TEST_BOOTSTRAP_AT_LAUNCH"] = str(snapshot)

        _ = self.run_bootstrap_process("--pilot", "modules", "--pilot", "names=cargo-handler,cargo-port",
                               "--pilot", "types=cargo-port")

        launched = cast(BootstrapAtLaunch, json.loads(snapshot.read_text()))
        self.assertEqual(launched["production"], self.doc.read_text())
        config = style_config.read_config(self.doc)
        self.assertEqual(config.assignments, tuple(style_config.StyleUnitAssignment(
            f"sample-style-{family}", (family,), 1) for family in add_style.FAMILIES))
        self.assertEqual(production_field(launched["production"].splitlines(), "Pilot units"),
                         "sample-style-names, sample-style-types, sample-style-modules")
        self.assertEqual(set(launched["plans"]), {f"sample-style-{family}-unit.md" for family in add_style.FAMILIES})
        for unit, scope in (("names", "cargo-handler, cargo-port"), ("types", "cargo-port")):
            lines = launched["plans"][f"sample-style-{unit}-unit.md"].splitlines()
            scope_index = lines.index(f"- **Pilot scope:** {scope}")
            self.assertLess(lines.index("## Delegation Context"), scope_index)
            self.assertTrue(lines[scope_index - 1].startswith("  - `"))
            self.assertLess(scope_index, lines.index("## Phases"))
        for unit in ("values", "modules"):
            self.assertNotIn("**Pilot scope:**", launched["plans"][f"sample-style-{unit}-unit.md"])
        self.assertEqual(unit_plans.parse_pilot_scope(launched["plans"]["sample-style-names-unit.md"], self.repo),
                         unit_plans.PilotCrates(frozenset({"crates/handler", "crates/port"})))
        self.assertEqual(unit_plans.parse_pilot_scope(launched["plans"]["sample-style-types-unit.md"], self.repo),
                         unit_plans.PilotCrates(frozenset({"crates/port"})))
        self.assertEqual(unit_plans.parse_pilot_scope(launched["plans"]["sample-style-modules-unit.md"], self.repo),
                         unit_plans.AllCrates())

    def test_pilot_is_refused_for_an_existing_production_without_changing_it(self) -> None:
        _ = self.run_bootstrap()
        paths = (self.doc, *(self.checkout / add_style.PLAN_DIRECTORY).glob("*.md"))
        before = {path: path.read_bytes() for path in paths}, self.calls(), self.sync()
        for extra in ((), ("--check",)):
            result = self.run_bootstrap("--pilot", "names=sample", *extra, success=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr.strip(), "add_style: --pilot applies only while creating sample-style")
            self.assertEqual(({path: path.read_bytes() for path in paths}, self.calls(), self.sync()), before)

    def test_invalid_pilot_units_packages_and_duplicate_units_refuse_before_writes(self) -> None:
        self.add_workspace_packages()
        for arguments, bad in (
            (("--pilot", "unknown"), "unknown"),
            (("--pilot", "names=absent"), "absent"),
            (("--pilot", "names=crates/handler"), "crates/handler"),
            (("--pilot", "names", "--pilot", "names=sample"), "names"),
            (("--pilot", "names="), "''"),
        ):
            for extra in ((), ("--check",)):
                with self.subTest(arguments=arguments, extra=extra):
                    result = self.run_bootstrap(*arguments, *extra, success=False)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(bad, result.stderr)
                    self.assertFalse((self.checkout / add_style.PLAN_DIRECTORY).exists())
                    self.assertFalse((self.root / "ledger").exists())
                    self.assertEqual(self.calls(), [])

    def test_interrupted_pilot_changes_only_unlaunched_units_and_plan_scopes(self) -> None:
        self.add_workspace_packages()
        self.env["TEST_FAIL_LAUNCH"] = "1"
        self.assertEqual(self.run_bootstrap("--pilot", "names=cargo-handler", "--pilot", "types=cargo-handler",
                                           "--pilot", "values=sample", success=False).returncode, 2)
        launched = self.root / "sample-style-names"
        launched.mkdir()
        names_plan = self.checkout / add_style.PLAN_DIRECTORY / "sample-style-names-unit.md"
        _ = names_plan.write_text(names_plan.read_text() + "\n### Phase 1 — launched record\n")
        names_before = names_plan.read_bytes(), names_plan.stat().st_mtime_ns
        types_plan = self.checkout / add_style.PLAN_DIRECTORY / "sample-style-types-unit.md"
        _ = types_plan.write_text(types_plan.read_text() + "\nUnlaunched notes retained.\n")
        log = self.repo / "docs/sample-style-log.md"
        _ = log.write_text("Interrupted bootstrap notes.\n")
        sync, excluded = self.sync(), (self.repo / ".git/info/exclude").read_bytes()
        del self.env["TEST_FAIL_LAUNCH"]

        _ = self.run_bootstrap("--pilot", "types=cargo-port", "--pilot", "modules")

        self.assertEqual((names_plan.read_bytes(), names_plan.stat().st_mtime_ns), names_before)
        self.assertEqual(production_field(self.doc.read_text().splitlines(), "Pilot units"),
                         "sample-style-names, sample-style-types, sample-style-modules")
        self.assertIn("- **Pilot scope:** cargo-port\n", types_plan.read_text())
        self.assertNotIn("**Pilot scope:** cargo-handler", types_plan.read_text())
        self.assertIn("Unlaunched notes retained.", types_plan.read_text())
        self.assertNotIn("**Pilot scope:**", (self.checkout / add_style.PLAN_DIRECTORY / "sample-style-values-unit.md").read_text())
        self.assertEqual(log.read_text(), "Interrupted bootstrap notes.\n")
        self.assertEqual((self.sync(), (self.repo / ".git/info/exclude").read_bytes()), (sync, excluded))

    def test_check_lists_pilot_units_and_scopes_in_steps_four_and_five_without_writes(self) -> None:
        self.add_workspace_packages()
        result = self.run_bootstrap("--check", "--pilot", "modules", "--pilot", "names=cargo-handler",
                                    "--pilot", "types=cargo-port")
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 7)
        for unit in ("sample-style-modules", "sample-style-names", "sample-style-types"):
            self.assertIn(unit, lines[3])
        self.assertIn("sample-style-modules: all crates", lines[4])
        self.assertIn("sample-style-names: cargo-handler", lines[4])
        self.assertIn("sample-style-types: cargo-port", lines[4])
        self.assertNotIn("values", lines[3] + lines[4])
        self.assertFalse((self.checkout / add_style.PLAN_DIRECTORY).exists())
        self.assertFalse((self.root / "ledger").exists())
        self.assertEqual(self.calls(), [])

    def test_pilot_family_old_id_and_full_id_produce_the_same_check(self) -> None:
        checks = [self.run_bootstrap("--check", "--pilot", f"{name}=sample").stdout
                  for name in ("names", "names", "sample-style-names")]
        self.assertEqual(checks[1:], [checks[0], checks[0]])
        self.assertIn("sample-style-names: sample", checks[0])
        self.assertFalse((self.checkout / add_style.PLAN_DIRECTORY).exists())
        self.assertFalse((self.root / "ledger").exists())

    def test_pilot_aliases_cannot_select_the_same_family_twice(self) -> None:
        result = self.run_bootstrap("--check", "--pilot", "names=sample",
                                    "--pilot", "sample-style-names=sample", success=False)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.checkout / add_style.PLAN_DIRECTORY).exists())
        self.assertEqual(self.calls(), [])

    def test_lookup_from_main_and_linked_worktree_finds_the_same_standing_production(self) -> None:
        _ = self.run_bootstrap()
        previous = self.doc
        self.doc = previous.with_name("tool-based-ui-production.md")
        _ = previous.rename(self.doc)
        owner = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "sample-style")
        _ = staff.set_doc(owner.id, staff.LiveDoc("natedev", self.doc))
        main = add_style.standing_style_production(self.repo)
        linked = add_style.standing_style_production(self.checkout)
        self.assertEqual(main, linked)
        self.assertIsInstance(main, add_style.StandingStyleProduction)
        if isinstance(main, add_style.StandingStyleProduction):
            self.assertEqual((main.repository, main.name, main.checkout, main.doc, main.branch_point),
                             (self.repo, "sample-style", self.checkout, self.doc, "main"))

    def test_check_uses_the_staff_name_and_keeps_document_paths(self) -> None:
        _ = self.run_bootstrap()
        self._timer_conf().unlink()
        owner = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "sample-style")
        _ = staff.rename(owner.id, "hana")
        output = self.run_bootstrap("--check").stdout
        self.assertIn("find standing hana under parent", output)
        self.assertIn(f"existing production doc {self.doc}", output)
        self.assertIn("docs/sample-style-log.md", output)


    def test_lookup_without_a_production_leaves_directories_branches_and_excludes_unchanged(self) -> None:
        before = (sorted(self.root.iterdir()), self.git("branch", "--list"),
                  (self.repo / ".git/info/exclude").read_bytes())
        self.assertEqual(add_style.standing_style_production(self.repo),
                         add_style.NoStyleProduction(self.repo, "sample-style"))
        self.assertEqual((sorted(self.root.iterdir()), self.git("branch", "--list"),
                          (self.repo / ".git/info/exclude").read_bytes()), before)

    def test_bootstrap_creates_excluded_production_plans_and_pins_style_branch(self) -> None:
        self.checkout.chmod(0o700)
        layout = worktrees.output_record("sample", self.merge_name, self.checkout.name)
        record = worktrees.new_record(self.checkout, self.style_branch, self.repo,
                                      self.initial_tip, "sample-style", "")
        worktrees.write_record(replace(record, layout=layout, stage=worktrees.MakeStage.MAKING))
        self.assertFalse(any(row.name == "sample-style" for row in staff.snapshot().find(kind="showrunner")))
        proof = Path(self.env["SHOWRUNNER_STATE_DIR"]) / GUARD_READY_FILE
        saved = proof.read_bytes()
        proof.unlink()
        with self.subTest(guard_proof=False):
            result = self.run_bootstrap_process(success=False)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn(GUARD_READY_FILE, result.stderr)
            self.assertFalse(self.doc.exists())
            self.assertEqual(self.calls(), [])
        _ = proof.write_bytes(saved)
        _ = self.run_bootstrap_process()

        self.assertEqual(stat.S_IMODE(self.checkout.stat().st_mode), 0o700)
        self.assertEqual(self.git("rev-parse", self.style_branch), self.initial_tip)
        self.assertIn(str(self.checkout), self.git("worktree", "list", "--porcelain"))
        self.assertEqual((self.checkout / "target").readlink(), self.root / "build" / "sample" / self.merge_name / self.checkout.name)
        production = read_production("sample-style")
        self.assertEqual((production.name, production.merge_branch, production.checkout), ("sample-style", self.style_branch, self.checkout))
        lines = self.doc.read_text().splitlines()
        for field, value in {
            "Kind": "style", "Repository": str(self.repo), "Branch point": "main",
            MERGE_CHECKOUT_FIELD: str(self.repo),
            "Proposals": str(self.root / "parent-issues"),
            "Log": "docs/sample-style-log.md", "Updates": "on demand", "Push": "git",
        }.items():
            self.assertEqual(production_field(lines, field), value)
        self.assertIn("- **Merge tests:** `sample`, `support` — packages tested on every merge", lines)
        for family in add_style.FAMILIES:
            plan = self.checkout / f"docs/style-production/units/sample-style-{family}-unit.md"
            text = plan.read_text()
            self.assertNotIn("**Production:", text)
            self.assertIn("> **Standing:** style_plan.py", text)
            self.assertNotIn("**Pilot scope:**", text)
            self.assertIn(f"global:rust/{family}.md", text)
            self.assertIn("~/.claude/docs/style_production.md", text)
            self.assertIn(f"--production sample-style sample-style-{family} --unit-type style", text)
            self.assertNotIn("### Phase", text)
            for verb in ("check", "test", "lint"):
                self.assertIn(f"verify.sh {verb} [<package>]", text)
            self.assertIn(str(plan), self.git("-C", str(self.checkout), "check-ignore", "--", str(plan)))
        self.assertEqual(self.git("check-ignore", "--", str(self.doc), str(self.repo / "docs/sample-style-log.md")).splitlines(),
                         [str(self.doc), str(self.repo / "docs/sample-style-log.md")])
        excluded = (self.repo / ".git/info/exclude").read_text()
        self.assertIn("/docs/style-production/\n", excluded)
        self.assertNotIn("/docs/style/\n", excluded)
        self.assertEqual(self.git("ls-files", "--", str(self.doc), "docs/style-production/"), "")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.git("-C", str(self.checkout), "status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(self.sync()[:2], (self.initial_tip, f"refs/heads/{self.style_branch}"))
        self.assertEqual(self.parent_document(), self.parent)
        self.assertEqual(self.calls(), [{
            "command": "launch.py",
            "arguments": ["--production", "sample-style", "--doc", str(self.doc),
                          "--model", "test-model", "--effort", "high"],
        }, {"command": "showrunners.py", "arguments": ["attach", "sample-style", "parent"]}])

    def test_launch_with_an_existing_staff_row_uses_its_doc_without_a_doc_argument(self) -> None:
        _ = self.git("commit", "--allow-empty", "-qm", "Advance the parent branch")
        _ = self.run_bootstrap()
        self.assertEqual(self.git("rev-parse", self.style_branch), self.initial_tip)
        self.assertNotEqual(self.git("rev-parse", "main"), self.initial_tip)
        self._timer_conf().unlink()
        _ = self.run_bootstrap()

        launches = [call for call in self.calls() if call["command"] == "launch.py"]
        self.assertEqual(launches[-1]["arguments"],
                         ["--production", "sample-style", "--model", "test-model", "--effort", "high"])
        self.assertEqual(read_production("sample-style").doc, self.doc)

    def test_standing_plans_do_not_become_local_rules_or_hide_real_rules(self) -> None:
        _ = self.run_bootstrap()

        catalog = rules.load_rules(self.checkout)
        self.assertEqual({rule.rule_id for rule in catalog if rule.origin == "local"}, {"local:docs/style/local.md"})
        listing = subprocess.run(
            [str(Path(__file__).resolve().parents[1] / "lib/py"), str(SCRIPT.with_name("rules.py")), "list", "--repo", str(self.checkout), "--json"],
            check=True, capture_output=True, text=True, env=self.env,
        )
        rows = cast(list[rules.RuleListing], json.loads(listing.stdout))
        self.assertEqual({row["rule_id"] for row in rows}, {rule.rule_id for rule in catalog})
        with style_ledger.open_ledger(self.checkout) as ledger:
            ledger.set_tip(self.initial_tip, self.style_branch)
            self.assertIn("local:docs/style/local.md", {row.rule_id for row in ledger.rule_counts(style_ledger.WholeRepository())})

    def test_create_refreshes_an_older_ledger_before_pinning_its_style_branch(self) -> None:
        _ = (self.repo / "src/new.rs").write_text("pub fn new_input() {}\n")
        _ = self.git("add", "--all")
        _ = self.git("commit", "-qm", "Advance branch point before creating style production")
        branch_tip = self.git("rev-parse", "main")
        _ = self.git("-C", str(self.checkout), "merge", "--ff-only", branch_tip)
        for previous_branch in ("", "style/previous"):
            with self.subTest(previous_branch=previous_branch):
                with style_ledger.open_ledger(self.repo) as ledger:
                    if previous_branch:
                        ledger.set_tip(self.initial_tip, previous_branch)
                    else:
                        ledger.set_tip(self.initial_tip)
                _ = self.run_bootstrap()
                self.assertEqual(self.sync()[:2], (branch_tip, f"refs/heads/{self.style_branch}"))
                with style_ledger.open_ledger(self.checkout) as ledger:
                    self.assertIn("src/new.rs", {cell.path for cell in ledger.cells()})
                self._timer_conf().unlink()

    def test_create_retry_with_the_exact_sync_pair_does_not_refresh_its_timestamp(self) -> None:
        self.env["TEST_FAIL_LAUNCH"] = "1"
        failed = self.run_bootstrap(success=False)
        self.assertEqual(failed.returncode, 2)
        before = self.sync()
        self.assertEqual(before[:2], (self.initial_tip, f"refs/heads/{self.style_branch}"))
        del self.env["TEST_FAIL_LAUNCH"]

        _ = self.run_bootstrap()

        self.assertEqual(self.sync(), before)

    def test_rerun_keeps_plans_ledger_and_excludes_and_does_not_launch_twice(self) -> None:
        _ = self.run_bootstrap()
        plan = self.checkout / "docs/style-production/units/sample-style-names-unit.md"
        _ = plan.write_text(plan.read_text() + "\n### Phase 1 — already assigned\n")
        paths = (plan, self.doc, self.repo / ".git/info/exclude")
        before = {path: (path.read_text(), path.stat().st_mtime_ns) for path in paths}
        sync = self.sync()
        calls = self.calls()

        _ = self.run_bootstrap()

        self.assertEqual({path: (path.read_text(), path.stat().st_mtime_ns) for path in paths}, before)
        self.assertEqual(self.sync(), sync)
        self.assertEqual(self.calls(), [*calls, self.resume_call()])

    def test_existing_production_reattaches_updates_destination_and_runs_resume(self) -> None:
        _ = self.run_bootstrap()
        with style_ledger.open_ledger(self.checkout) as ledger:
            ledger.handoff_add(self.initial_tip)
        _ = self.git("commit", "--allow-empty", "-q", "-m", "Another checkpoint")
        second = self.git("rev-parse", "HEAD")
        with style_ledger.open_ledger(self.checkout) as ledger:
            ledger.handoff_add(second)
            ledger.handoff_send(second, "parent")
        caller = self.parent_doc("next")
        original_sync = self.sync()
        original_plans = {path: path.read_text() for path in (self.checkout / add_style.PLAN_DIRECTORY).glob("*.md")}

        result = self.run_bootstrap(parent=caller)

        self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls()[-1], self.resume_call())
        with style_ledger.open_ledger(self.checkout) as ledger:
            self.assertEqual({row["checkpoint"]: row["state"] for row in ledger.pending_handoffs()},
                             {self.initial_tip: "pending", second: "sent"})
        fields = self.doc.read_text().splitlines()
        self.assertEqual(self.parent_document(), caller)
        self.assertEqual(production_field(fields, "Proposals"), str(self.root / "next-issues"))
        self.assertEqual(self.calls()[2:], [
            {"command": "showrunners.py", "arguments": ["detach", "sample-style"]},
            {"command": "showrunners.py", "arguments": ["attach", "sample-style", "next"]},
            self.resume_call(),
        ])
        self.assertEqual((self.root / "showrunners/sample-style/parent").read_text().strip(), "next")
        self.assertEqual(self.sync(), original_sync)
        self.assertEqual({path: path.read_text() for path in original_plans}, original_plans)

    def test_change_review_credits_the_parent_after_creation_and_reattach(self) -> None:
        _ = self.run_bootstrap()
        with self.subTest(destination="new production"):
            self.assertEqual(change_review.credited(self.doc, staff.snapshot()), self.parent)
        lines = [line for line in self.doc.read_text().splitlines()
                 if not line.startswith("- **Parent production:**")]
        _ = self.doc.write_text("\n".join([*lines, f"- **Parent production:** {self.parent}", ""]))
        caller = self.parent_doc("next")
        _ = self.run_bootstrap(parent=caller)
        with self.subTest(destination="new parent"):
            self.assertEqual(change_review.credited(self.doc, staff.snapshot()), caller)

    def test_missing_proposals_refuses_before_writes_in_check_and_apply(self) -> None:
        parent = self.parent_doc("missing", proposals=False)
        before = self.git("show-ref"), self.git("worktree", "list", "--porcelain"), (self.repo / ".git/info/exclude").read_text()
        for arguments in ((), ("--check",)):
            with self.subTest(arguments=arguments):
                result = self.run_bootstrap(*arguments, parent=parent, success=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Proposals", result.stderr)
                self.assertEqual(len(result.stderr.splitlines()), 1)
                self.assertFalse((self.checkout / add_style.PLAN_DIRECTORY).exists())
                self.assertFalse((self.root / "ledger").exists())
                self.assertEqual(self.calls(), [])
                self.assertEqual((self.git("show-ref"), self.git("worktree", "list", "--porcelain"), (self.repo / ".git/info/exclude").read_text()), before)

    def test_check_prints_seven_steps_without_creating_state_or_artifacts(self) -> None:
        before = self.git("show-ref"), (self.repo / ".git/info/exclude").read_text()

        result = self.run_bootstrap("--check")

        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 7)
        self.assertEqual([line.split(".", 1)[0] for line in lines], [str(number) for number in range(1, 8)])
        for expected in (self.style_branch, "docs/style-production/units", "info/exclude", "add_showrunner.py"):
            self.assertIn(expected, result.stdout)
        self.assertFalse((self.checkout / add_style.PLAN_DIRECTORY).exists())
        self.assertFalse((self.root / "ledger").exists())
        self.assertEqual(self.calls(), [])
        self.assertEqual((self.git("show-ref"), (self.repo / ".git/info/exclude").read_text()), before)

    def test_check_existing_production_describes_attachment_and_preserves_all_state(self) -> None:
        _ = self.run_bootstrap()
        _ = self.git("commit", "--allow-empty", "-q", "-m", "Advance parent beyond synced tip")
        caller = self.parent_doc("next")
        paths = (self.doc, self.repo / ".git/info/exclude", *(self.checkout / add_style.PLAN_DIRECTORY).glob("*.md"))
        before = {path: (path.read_text(), path.stat().st_mtime_ns) for path in paths}
        refs, worktrees, sync, calls = self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.sync(), self.calls()
        parent = (self.root / "showrunners/sample-style/parent").read_text()

        result = self.run_bootstrap("--check", parent=caller)

        self.assertEqual(result.stdout.splitlines(), [
            f"1. read {caller}; repository {self.repo}; Proposals {self.root / 'next-issues'}",
            "2. attach standing sample-style under next; branch point main",
            *(f"{number}. skipped: sample-style already exists" for number in range(3, 7)),
            "7. update Proposals, run style_sync.py resume; synced tip unchanged",
        ])
        self.assertEqual({path: (path.read_text(), path.stat().st_mtime_ns) for path in paths}, before)
        self.assertEqual((self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.sync(), self.calls()),
                         (refs, worktrees, sync, calls))
        self.assertEqual((self.root / "showrunners/sample-style/parent").read_text(), parent)

    def test_unready_live_session_requires_timer_before_retry_can_attach_without_relaunching(self) -> None:
        self.env["TEST_OPEN_SESSION_EXIT"] = "3"

        result = self.run_bootstrap(success=False)

        guidance = (
            "look at it with herdr session attach agents, workspace sample-style, tab sample-style; "
            "once it has its update timer, rerun add_style, which attaches it; "
            "if it never gets one, end that session and rerun"
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("add_style: sample-style was left open by add_showrunner.py (exit 3); " + guidance, result.stderr)
        self.assertNotIn("rerun add_style to continue", result.stderr)
        self.assertTrue((self.root / "live-sessions/sample-style").exists())
        calls, sync = self.calls(), self.sync()

        retry = self.run_bootstrap(success=False)

        self.assertEqual(retry.returncode, 2, retry.stderr)
        self.assertIn(guidance, retry.stderr)
        self.assertEqual((self.calls(), self.sync()), (calls, sync))
        timer = self._timer_conf()
        timer.parent.mkdir(parents=True)
        owner = next(row for row in staff.snapshot().find(kind="showrunner") if row.name == "sample-style")
        _ = timer.write_text(f"TARGET=session:{owner.id}\nCHECK=zsh /x/production_check.sh {owner.id}\n")

        _ = self.run_bootstrap()

        self.assertEqual(self.calls(), [*calls, {
            "command": "showrunners.py", "arguments": ["attach", "sample-style", "parent"],
        }, self.resume_call()])
        self.assertEqual((self.root / "showrunners/sample-style/parent").read_text().strip(), "parent")
        self.assertEqual(self.sync(), sync)

    def test_bootstrapped_standing_plan_passes_real_unit_preflight(self) -> None:
        _ = self.run_bootstrap()
        plan = add_style.PLAN_DIRECTORY / "sample-style-names-unit.md"
        request = add_unit.RequestedUnitLaunch(
            production=read_production("sample-style"), requested_name="sample-style-names", unit="sample-style-names",
            branch="sample-style-names", worktree=self.root / "sample-style-names",
            plan=add_unit.PlanGiven(plan), port=add_unit.OmittedCell(), session=add_unit.NewSession(), timeout=1.0,
        )
        before = self.git("status", "--porcelain", "--untracked-files=all"), self.git("show-ref"), self.doc.read_text()
        agent_config = self.root / "models.conf"
        _ = write_registry(agent_config, {'assignments': {'production': 'claude'}, 'production.claude': {'director': 'opus:xhigh'}, 'claude.agents': {'opus': 'low,medium,high,xhigh'}})
        catalog_sync = self.root / "catalog-synced"
        catalog_sync.touch()

        with (
            mock.patch.dict(os.environ, {
                "MODELS_CONFIG_FILE": str(agent_config), "CODEX_CATALOG_SYNC_STATE_FILE": str(catalog_sync),
                "CODEX_CONFIG_FILE": str(self.root / "codex.toml"), "CODEX_MODELS_CACHE_FILE": str(self.root / "models.json"),
            }),
            mock.patch.object(herdr, "claude_trust", return_value=herdr.Trusted(
                self.repo, self.root / "account/.claude.json", self.repo)),
            mock.patch.object(add_unit, "remote_head", return_value=""),
        ):
            ready = add_unit.preflight(request, NamedAccount("test", self.root / "account"))
            for unit_type in ("direct", "style"):
                with self.subTest(unit_type=unit_type):
                    selected = add_unit.preflight(replace(request, unit_type=unit_type),
                                                  NamedAccount("test", self.root / "account"))
                    self.assertEqual(add_unit.prompt_for(selected.launch), f"/unit:{unit_type} {plan}")
                    command = SCRIPT.parents[2] / f"commands/unit/{unit_type}.md"
                    self.assertTrue(command.is_file())

        self.assertEqual(ready.launch.plan_path, plan)
        self.assertEqual(ready.launch.identity.unit, "sample-style-names")
        self.assertEqual((self.git("status", "--porcelain", "--untracked-files=all"), self.git("show-ref"), self.doc.read_text()), before)
        self.assertFalse(request.worktree.exists())

    def test_second_branch_point_is_refused_without_creating_or_reattaching_anything(self) -> None:
        _ = self.run_bootstrap()
        _ = self.git("branch", "init/catalyst")
        parent = self.parent_doc("other", "init/catalyst")
        before = self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.doc.read_text(), self.sync(), self.calls()
        for arguments in ((), ("--check",)):
            with self.subTest(arguments=arguments):
                result = self.run_bootstrap(*arguments, parent=parent, success=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr.strip(), "add_style: sample already has style production sample-style on branch point main; one branch point per repository")
                self.assertEqual((self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.doc.read_text(), self.sync(), self.calls()), before)

    def test_retry_before_registration_refuses_a_second_branch_point(self) -> None:
        self.env["TEST_FAIL_LAUNCH"] = "1"
        stopped = self.run_bootstrap(success=False)
        self.assertIn("launch.py exited 2", stopped.stderr)
        self.assertFalse(any(row.name == "sample-style" for row in staff.snapshot().find(kind="showrunner")))
        del self.env["TEST_FAIL_LAUNCH"]
        _ = self.git("branch", "other")
        parent = self.parent_doc("next", "other")
        before = self.doc.read_bytes(), self.git("show-ref"), self.calls()
        for arguments in (("--check",), ()):
            with self.subTest(arguments=arguments):
                result = self.run_bootstrap(*arguments, parent=parent, success=False)
                self.assertEqual(result.returncode, 2)
                self.assertIn("sample-style on branch point main", result.stderr)
                self.assertEqual((self.doc.read_bytes(), self.git("show-ref"), self.calls()), before)

    def test_slash_branch_point_uses_main_checkout_location_even_when_parent_runs_in_a_worktree(self) -> None:
        _ = self.git("branch", "init/catalyst")
        product_checkout = self.root / "product-checkout"
        _ = self.git("worktree", "add", "-q", str(product_checkout), "init/catalyst")
        parent = self.parent_doc("nested", "init/catalyst")
        _ = parent.write_text(parent.read_text().replace(str(self.repo), str(product_checkout)))

        _ = self.run_bootstrap(parent=parent)

        self.assertEqual(self.git("rev-parse", self.style_branch), self.initial_tip)
        self.assertEqual(production_field(self.doc.read_text().splitlines(), "Repository"), str(self.repo))
        self.assertEqual(self.sync()[:2], (self.initial_tip, f"refs/heads/{self.style_branch}"))
        self.assertFalse((self.root / "product-checkout-style").exists())

    def test_retry_after_failed_launch_preserves_open_plan_and_keeps_ledger_at_style_branch_commit(self) -> None:
        self.env["TEST_FAIL_LAUNCH"] = "1"
        refused = self.run_bootstrap(success=False)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("launch.py exited 2", refused.stderr)
        plan = self.checkout / "docs/style-production/units/sample-style-values-unit.md"
        _ = plan.write_text(plan.read_text() + "\n### Phase 1 — resumed work\n")
        original_plan = plan.read_text()
        _ = (self.repo / "src/new.rs").write_text("pub fn added_after_launch() {}\n")
        _ = self.git("add", "src/new.rs")
        _ = self.git("commit", "-q", "-m", "Advance branch point before retry")
        branch_tip = self.git("rev-parse", "main")
        del self.env["TEST_FAIL_LAUNCH"]

        _ = self.run_bootstrap()

        self.assertEqual(plan.read_text(), original_plan)
        style_tip = self.git("rev-parse", f"refs/heads/{self.style_branch}")
        self.assertEqual(style_tip, self.initial_tip)
        self.assertNotEqual(style_tip, branch_tip)
        self.assertEqual(self.sync()[:2], (style_tip, f"refs/heads/{self.style_branch}"))
        with style_ledger.open_ledger(self.checkout) as ledger:
            self.assertNotIn("src/new.rs", {cell.path for cell in ledger.cells()})
        self.assertEqual([call["command"] for call in self.calls()], ["launch.py", "launch.py", "showrunners.py"])
        self.assertEqual(self.git("worktree", "list", "--porcelain").count(f"worktree {self.checkout}"), 1)

    def test_bootstrap_writes_one_plan_for_a_combined_configured_unit(self) -> None:
        self.env["TEST_FAIL_LAUNCH"] = "1"
        self.assertEqual(self.run_bootstrap(success=False).returncode, 2)
        folder = self.checkout / add_style.PLAN_DIRECTORY
        for plan in folder.glob("*.md"):
            plan.unlink()
        write_style_units(self.doc, (style_config.StyleUnitAssignment("cargo-liner-style", add_style.FAMILIES),))
        self.env["TEST_FAIL_LAUNCH"] = "0"

        _ = self.run_bootstrap()

        plan, = folder.glob("*.md")
        self.assertEqual(plan.name, "cargo-liner-style-unit.md")
        for family in add_style.FAMILIES:
            self.assertIn(f"global:rust/{family}.md", plan.read_text())
        self.assertIn("# cargo-liner-style", plan.read_text())

    def test_retry_finishes_a_registered_style_checkout(self) -> None:
        layout = worktrees.output_record("sample", self.merge_name, self.checkout.name)
        pending = replace(worktrees.new_record(self.checkout, self.style_branch, self.repo,
                                               self.initial_tip, "sample-style", ""),
                          layout=layout, stage=worktrees.MakeStage.MAKING)
        worktrees.write_record(pending)
        with mock.patch.object(worktrees, "ensure_target_link", side_effect=OSError("output setup stopped")):
            stopped = self.run_bootstrap(success=False)
        self.assertEqual(stopped.returncode, 2, stopped.stdout + stopped.stderr)
        self.assertIn("output setup stopped", stopped.stderr)
        inode = self.checkout.stat().st_ino
        with mock.patch.dict(os.environ, self.env):
            record_path = worktree_records.record_file(self.checkout)
            pending = worktree_records.read_record(record_path)
        self.assertIs(pending.stage, worktree_records.MakeStage.MAKING)
        self.assertFalse((self.checkout / "target").is_symlink())

        _ = self.run_bootstrap()

        self.assertEqual(worktree_records.read_record(record_path), replace(pending, stage=worktree_records.MakeStage.DONE))
        self.assertEqual(self.checkout.stat().st_ino, inode)
        self.assertIsInstance(pending.layout, worktree_records.RecordedWorktreeOutput)
        assert isinstance(pending.layout, worktree_records.RecordedWorktreeOutput)
        self.assertEqual((self.checkout / "target").readlink(), pending.layout.output)

    def test_retry_completes_worktree_left_before_production_doc_was_written(self) -> None:
        _ = self.run_bootstrap()

        self.assertTrue(self.doc.exists())
        self.assertEqual(len(tuple((self.checkout / add_style.PLAN_DIRECTORY).glob("*.md"))), 4)
        self.assertEqual(self.sync()[:2], (self.initial_tip, f"refs/heads/{self.style_branch}"))
        self.assertEqual(len(self.calls()), 2)

    def test_retry_without_a_doc_refuses_the_wrong_branch_point_for_a_new_checkout(self) -> None:
        layout = worktrees.output_record("sample", self.merge_name, self.checkout.name)
        record = worktrees.new_record(self.checkout, self.style_branch, self.repo,
                                      self.initial_tip, "sample-style", "")
        worktrees.write_record(replace(record, layout=layout, stage=worktrees.MakeStage.MAKING))
        with mock.patch.object(worktrees, "ensure_target_link", side_effect=OSError("output setup stopped")):
            stopped = self.run_bootstrap(success=False)
        self.assertIn("output setup stopped", stopped.stderr)
        self.assertFalse(self.doc.exists())
        self.assertTrue((self.checkout / ".git").is_file())
        _ = self.git("branch", "other")
        parent = self.parent_doc("next", "other")
        before = self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.calls()

        refused = self.run_bootstrap(parent=parent, success=False)

        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("sample-style on branch point main", refused.stderr)
        self.assertEqual((self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.calls()), before)
        self.assertFalse(self.doc.exists())
        _ = self.run_bootstrap()
        self.assertEqual(production_field(self.doc.read_text().splitlines(), "Branch point"), "main")

    def test_retry_without_a_doc_refuses_the_wrong_branch_point_for_a_legacy_checkout(self) -> None:
        _ = self.git("worktree", "remove", str(self.checkout))
        legacy_checkout = self.checkout.parent.with_name("sample-style") / "production"
        _ = self.git("worktree", "add", "-q", "-b", "style/main", str(legacy_checkout), self.initial_tip)
        _ = self.git("branch", "other")
        parent = self.parent_doc("next", "other")
        before = self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.calls()

        refused = self.run_bootstrap(parent=parent, success=False)

        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn("sample-style on branch point main", refused.stderr)
        self.assertEqual((self.git("show-ref"), self.git("worktree", "list", "--porcelain"), self.calls()), before)
        self.assertFalse(self.checkout.exists())
        self.assertFalse(self.doc.exists())
        self.assertFalse((legacy_checkout / "docs/sample-style-production.md").exists())

    def test_retry_keeps_the_previous_checkout_and_branch(self) -> None:
        _ = self.git("worktree", "remove", str(self.checkout))
        legacy_checkout = self.checkout.parent.with_name("sample-style") / "production"
        _ = self.git("worktree", "add", "-q", "-b", "style/main", str(legacy_checkout), self.initial_tip)

        _ = self.run_bootstrap()

        production = read_production("sample-style")
        self.assertEqual((production.checkout, production.merge_branch), (legacy_checkout, "style/main"))
        self.assertFalse(self.checkout.exists())

    def legacy_bootstrap(self) -> tuple[Path, Path, Path]:
        self.env["TEST_REQUIRE_ADMISSION"] = "1"
        _ = self.git("worktree", "remove", str(self.checkout))
        checkout = self.checkout.parent.with_name("sample-style") / "production"
        _ = self.git("worktree", "add", "-qb", "style/main", str(checkout), self.initial_tip)
        doc = checkout / "docs" / "sample-style-production.md"
        log = checkout / "docs" / "custom-style-log.md"
        plan = checkout / add_style.PLAN_DIRECTORY / "sample-style-custom-unit.md"
        plan.parent.mkdir(parents=True)
        _ = plan.write_text("# Saved plan\n\nKeep this scope.\n")
        _ = log.write_text("# Saved log\n\nKeep this entry.\n")
        _ = doc.write_text("\n".join([
            "# Legacy style production", "", "## Production Context", "",
            f"- **Repository:** {self.repo}", "- **Kind:** style", "- **Branch point:** main",
            "- **Merge branch:** style/main", f"- **Showrunner checkout:** {checkout}",
            "- **Log:** docs/custom-style-log.md", f"- **Proposals:** {self.root / 'parent-issues'}",
            "- **Pilot units:** sample-style-custom", "- **Custom field:** Keep this field.", "",
            "## Style units", "", "| Unit | Families | Open tickets |", "| --- | --- | --- |",
            "| sample-style-custom | names, values, types, modules | 2 |", "",
            "## Custom notes", "", "Keep this note.", "",
        ]))
        return doc, log, plan

    def test_legacy_bootstrap_enrolls_and_retries_without_losing_saved_files(self) -> None:
        legacy, log, plan = self.legacy_bootstrap()
        checkout = legacy.parent.parent
        live_log = self.repo / log.relative_to(checkout)
        before = {path: path.read_bytes() for path in (legacy, log, plan)}
        registered = self.git("worktree", "list", "--porcelain")
        self.env["TEST_FAIL_LAUNCH"] = "1"

        failed = self.run_bootstrap(success=False)

        self.assertIn("launch.py exited 2", failed.stderr)
        enrolled = production_doc.read_new_production(self.doc, "sample-style")
        self.assertEqual((enrolled.merge_checkout, enrolled.checkout, enrolled.merge_branch, enrolled.log),
                         (self.repo, checkout, "style/main", live_log))
        self.assertEqual(live_log.read_bytes(), before[log])
        self.assertEqual(self.doc.read_text(), legacy.read_text().replace(
            "- **Showrunner checkout:**", f"- **{MERGE_CHECKOUT_FIELD}:** {self.repo}\n- **Showrunner checkout:**"))
        self.assertEqual(style_config.read_config(self.doc).assignments,
                         (style_config.StyleUnitAssignment("sample-style-custom", add_style.FAMILIES, 2),))
        self.assertFalse(any(row.name == "sample-style" for row in staff.snapshot().find(kind="showrunner")))

        with self.doc.open("a") as document:
            _ = document.write("Keep this enrolled edit.\n")
        with live_log.open("a") as log_file:
            _ = log_file.write("Keep this enrolled log entry.\n")
        published = self.doc.read_bytes(), live_log.read_bytes()
        failed_again = self.run_bootstrap(success=False)
        self.assertIn("launch.py exited 2", failed_again.stderr)
        self.assertEqual((self.doc.read_bytes(), live_log.read_bytes()), published)
        del self.env["TEST_FAIL_LAUNCH"]

        _ = self.run_bootstrap()

        self.assertEqual(read_production("sample-style").doc, self.doc)
        self.assertEqual((self.doc.read_bytes(), live_log.read_bytes()), published)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(list(plan.parent.glob("*.md")), [plan])
        self.assertEqual(self.git("worktree", "list", "--porcelain"), registered)
        self.assertFalse(self.checkout.exists())
        launches = [call for call in self.calls() if call["command"] == "launch.py"]
        self.assertEqual(len(launches), 3)
        for launch in launches:
            self.assertIn(str(self.doc), launch["arguments"])

    def test_legacy_bootstrap_refuses_a_conflicting_destination_before_writes(self) -> None:
        legacy, log, plan = self.legacy_bootstrap()
        live_log = self.repo / log.relative_to(legacy.parent.parent)
        tracked = (legacy, log, plan, self.repo / ".git/info/exclude")
        before = {path: path.read_bytes() for path in tracked}
        refs = self.git("show-ref")
        for conflict in (self.doc, live_log):
            with self.subTest(conflict=conflict):
                _ = conflict.write_text("Keep this existing file.\n")
                try:
                    refused = self.run_bootstrap(success=False)
                    self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
                    self.assertIn(str(conflict), refused.stderr)
                    self.assertEqual(conflict.read_text(), "Keep this existing file.\n")
                    other = live_log if conflict == self.doc else self.doc
                    self.assertFalse(other.exists())
                    self.assertEqual({path: path.read_bytes() for path in tracked}, before)
                    self.assertEqual(self.git("show-ref"), refs)
                    self.assertEqual(self.calls(), [])
                finally:
                    conflict.unlink()

    def check_legacy_repository_conversion(self, replacement: str) -> None:
        legacy, _, _ = self.legacy_bootstrap()
        text = legacy.read_text().replace(f"- **Repository:** {self.repo}\n", replacement)
        _ = legacy.write_text(text)

        _ = self.run_bootstrap()

        enrolled = read_production("sample-style")
        self.assertEqual(enrolled.doc, self.doc)
        self.assertEqual(production_field(self.doc.read_text().splitlines(), "Repository"), str(self.repo))
        self.assertEqual(legacy.read_text(), text)

    def test_legacy_bootstrap_adds_an_absolute_repository_when_absent(self) -> None:
        self.check_legacy_repository_conversion("")

    def test_legacy_bootstrap_expands_the_repository_for_admission(self) -> None:
        relative = os.path.relpath(self.repo, self.env["HOME"])
        self.check_legacy_repository_conversion(f"- **Repository:** ~/{relative}\n")

    def test_legacy_bootstrap_refuses_a_destination_log_when_the_legacy_log_is_missing(self) -> None:
        legacy, log, plan = self.legacy_bootstrap()
        log.unlink()
        live_log = self.repo / log.relative_to(legacy.parent.parent)
        _ = live_log.write_text("Keep this unrelated log.\n")
        tracked = (legacy, live_log, plan, self.repo / ".git/info/exclude", self.repo / ".git/config")
        before = {path: path.read_bytes() for path in tracked}
        refs = self.git("show-ref")

        refused = self.run_bootstrap(success=False)

        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(f"{live_log}: conflicts with the missing legacy style log", refused.stderr)
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)
        self.assertEqual(self.git("show-ref"), refs)
        self.assertFalse(log.exists())
        self.assertFalse(self.doc.exists())
        self.assertEqual(self.calls(), [])

    def test_legacy_bootstrap_without_a_log_retries_after_interrupted_publication(self) -> None:
        legacy, log, plan = self.legacy_bootstrap()
        log.unlink()
        live_log = self.repo / log.relative_to(legacy.parent.parent)
        tracked = (legacy, plan)
        before = {path: path.read_bytes() for path in tracked}

        def interrupted_create(path: Path, text: str) -> bool:
            if path == self.doc:
                raise OSError("stopped before document publication")
            return create_text_atomic(path, text)

        with mock.patch.object(add_style, "create_text_atomic", interrupted_create):
            stopped = self.run_bootstrap(success=False)

        self.assertEqual(stopped.returncode, 2, stopped.stderr)
        self.assertIn("stopped before document publication", stopped.stderr)
        self.assertFalse(self.doc.exists())
        self.assertFalse(live_log.exists())
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)
        self.assertEqual(self.calls(), [])

        _ = self.run_bootstrap()

        enrolled = read_production("sample-style")
        self.assertEqual((enrolled.doc, enrolled.log), (self.doc, live_log))
        self.assertFalse(live_log.exists())
        self.assertFalse(log.exists())
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)

    def publish_legacy_bootstrap_before_launch_failure(self) -> tuple[Path, Path, Path]:
        legacy, log, plan = self.legacy_bootstrap()
        self.env["TEST_FAIL_LAUNCH"] = "1"
        stopped = self.run_bootstrap(success=False)
        del self.env["TEST_FAIL_LAUNCH"]
        self.assertEqual(stopped.returncode, 2, stopped.stderr)
        self.assertIn("launch.py exited 2", stopped.stderr)
        self.assertEqual(len(Path(self.env["TEST_LAUNCH_DESTINATIONS"]).read_text().splitlines()), 1)
        return legacy, self.repo / log.relative_to(legacy.parent.parent), plan

    def check_enrolled_retry_after_legacy_document_change(self, field: str) -> None:
        legacy, live_log, plan = self.publish_legacy_bootstrap_before_launch_failure()
        if field:
            _ = legacy.write_text("".join(line for line in legacy.read_text().splitlines(keepends=True)
                                          if not line.startswith(f"- **{field}:**")))
        else:
            legacy.unlink()
        tracked = (self.doc, live_log, plan)
        before = {path: path.read_bytes() for path in tracked}

        _ = self.run_bootstrap()

        enrolled = read_production("sample-style")
        self.assertEqual((enrolled.doc, enrolled.checkout, enrolled.merge_branch),
                         (self.doc, legacy.parent.parent, "style/main"))
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)
        self.assertEqual(legacy.exists(), bool(field))
        self.assertEqual(len(Path(self.env["TEST_LAUNCH_DESTINATIONS"]).read_text().splitlines()), 2)

    def test_enrolled_bootstrap_retries_after_the_legacy_log_field_is_removed(self) -> None:
        self.check_enrolled_retry_after_legacy_document_change("Log")

    def test_enrolled_bootstrap_retries_after_the_legacy_merge_branch_is_removed(self) -> None:
        self.check_enrolled_retry_after_legacy_document_change("Merge branch")

    def test_enrolled_bootstrap_retries_after_the_legacy_document_is_deleted(self) -> None:
        self.check_enrolled_retry_after_legacy_document_change("")

    def check_enrolled_repository_refusal(self, replacement: str, diagnostic: str) -> None:
        legacy, live_log, plan = self.publish_legacy_bootstrap_before_launch_failure()
        _ = legacy.write_text(legacy.read_text().replace("- **Merge branch:** style/main\n", ""))
        _ = self.doc.write_text(self.doc.read_text().replace(f"- **Repository:** {self.repo}\n", replacement))
        tracked = (legacy, self.doc, live_log, plan, self.repo / ".git/info/exclude", self.repo / ".git/config")
        before = {path: path.read_bytes() for path in tracked}
        calls = self.calls()

        refused = self.run_bootstrap(success=False)

        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertIn(f"{self.doc}: {diagnostic}", refused.stderr)
        self.assertEqual({path: path.read_bytes() for path in tracked}, before)
        self.assertEqual(self.calls(), calls)

    def test_enrolled_bootstrap_refuses_a_missing_repository_before_writes(self) -> None:
        self.check_enrolled_repository_refusal("", "production doc lacks Repository")

    def test_enrolled_bootstrap_refuses_an_abbreviated_repository_before_writes(self) -> None:
        relative = os.path.relpath(self.repo, self.env["HOME"])
        self.check_enrolled_repository_refusal(f"- **Repository:** ~/{relative}\n",
                                               "Repository must match the absolute main checkout path")

    def test_registered_legacy_bootstrap_keeps_its_document_without_a_timer(self) -> None:
        legacy, log, _ = self.legacy_bootstrap()
        _ = recorded_showrunner("sample-style", legacy)
        _ = self.run_bootstrap()

        self.assertFalse(self.doc.exists())
        self.assertFalse((self.repo / log.relative_to(legacy.parent.parent)).exists())
        self.assertEqual(read_production("sample-style").doc, legacy)
        launch = next(call for call in self.calls() if call["command"] == "launch.py")
        self.assertNotIn("--doc", launch["arguments"])

    def test_interrupted_bootstrap_rewrites_destination_for_the_next_parent_only(self) -> None:
        self.env["TEST_FAIL_LAUNCH"] = "1"
        failed = self.run_bootstrap(success=False)
        self.assertEqual(failed.returncode, 2)
        plans = {path: path.read_bytes() for path in (self.checkout / add_style.PLAN_DIRECTORY).glob("*.md")}
        sync, excludes = self.sync(), (self.repo / ".git/info/exclude").read_bytes()
        caller = self.parent_doc("next")
        self.env["TEST_FAIL_LAUNCH"] = "0"
        self.env["TEST_PARENT_SLUG"] = "next"
        _ = self.run_bootstrap(parent=caller)
        fields = self.doc.read_text().splitlines()
        self.assertEqual(self.parent_document(), caller)
        self.assertEqual(production_field(fields, "Proposals"), str(self.root / "next-issues"))
        self.assertEqual({path: path.read_bytes() for path in plans}, plans)
        self.assertEqual((self.sync(), (self.repo / ".git/info/exclude").read_bytes()), (sync, excludes))
        launches = [call for call in self.calls() if call["command"] == "launch.py"]
        self.assertEqual(len(launches), 2)
        self.assertIn(str(self.doc), launches[-1]["arguments"])
        destinations = (self.root / "launch-destinations.txt").read_text().splitlines()
        self.assertEqual(len(destinations), 2)
        self.assertEqual(production_field([destinations[0]], "Proposals"), str(self.root / "parent-issues"))
        self.assertEqual(production_field([destinations[1]], "Proposals"), str(self.root / "next-issues"))
        self.assertNotIn(self.resume_call(), self.calls())

    def test_check_interrupted_bootstrap_describes_destination_repair_without_writes(self) -> None:
        self.env["TEST_FAIL_LAUNCH"] = "1"
        self.assertEqual(self.run_bootstrap(success=False).returncode, 2)
        caller = self.parent_doc("next")
        paths = (self.doc, self.repo / ".git/info/exclude", *(self.checkout / add_style.PLAN_DIRECTORY).glob("*.md"))
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        calls, sync = self.calls(), self.sync()
        result = self.run_bootstrap("--check", parent=caller)
        self.assertEqual(result.stdout.splitlines()[3],
                         f"4. set Proposals in existing production doc {self.doc}")
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}, before)
        self.assertEqual((self.calls(), self.sync()), (calls, sync))
        self.assertEqual(production_field(self.doc.read_text().splitlines(), "Proposals"), str(self.root / "parent-issues"))


if __name__ == "__main__":
    _ = unittest.main()

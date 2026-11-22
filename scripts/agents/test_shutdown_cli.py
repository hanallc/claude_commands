"""Test the agents CLI with private staff rows and temporary repositories."""
from __future__ import annotations

import fcntl
import io
import json
import math
import multiprocessing
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Generator, Iterable, Mapping, MutableMapping
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from functools import cached_property
from enum import Enum
from multiprocessing.connection import Connection
from pathlib import Path
from typing import ClassVar, cast, final, override
from unittest import mock

from scripts.accounts import accounts
from scripts.lib.state_home_test_support import isolate_state_home
from scripts.herdr import herdr_types
from scripts.agents import set_model, status_entry, test_stop_resume, worker_lifecycle
from scripts.direct import progress_history
from scripts.copies import copies
from scripts.copies import remote_test_support
from scripts.copies.workspace_test_support import registered_workspace
from scripts.copies.copies_test_support import fake_remote_removal, publish_mirror
from scripts.copies.remote_test_support import local_remote_transport
from scripts.herdr import herdr, pane_launch, worker_panes
from scripts.herdr.herdr import HerdrPlace
from scripts.herdr.test_worker_panes import PaneWorld
from scripts.lib import (
    atomic_file, build_folder_lock, build_folders, build_slots, build_slots_test_support, conf, git_files, process_group, process_start,
    socket_test_support, unit_runs,
)
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.lib.conf import UseConfiguredValue
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.unit_runs_test_support import bind_private_run_roots
from scripts.message import session_records, sessions, session_send
from scripts.message.sessions import SessionRecord
from scripts.production import (
    unit_lookup, unit_move, staff, staff_test_support, unit_config, add_unit, completion_receipt, worker,
    rename_unit_test_support,
)
from scripts.production.production_doc import Production, read_production
from scripts.production.staff import StaffId
from scripts.production.staff_test_support import (
    foreign_hostname,
    fixture_member,
    fixture_unit_saved_name,
    fixture_worker,
    SharedStaffOwner, StaffTestCase,
    fixture_runtime_id,
    CHECKOUT,
    unit_membership,
)
from scripts.agents import shutdown, remote, stop_resume, restart, pane_record as staff_pane_record, pane_record, producer_transfer
from scripts.agents.stop_results import StopResult
from scripts.worktree import worktrees

from scripts.production import rename_test_support as support
from scripts.production.rename_test_support import (
    FAIL_GIT_MOVE_INDEX, NEW, OLD, ProductionRenameFixture, RenameDisplays, stop_at_staff_write,
)
from scripts.production.rename_rerun_test_support import file_versions, final_files, saved_fixture
from scripts.production.cli_fixture import invoke_cli
from scripts.production.rename_unit_test_support import FolderRenameFixture, FolderSessionFixture, UnitRenameFixture

class AgentsUsageTests(unittest.TestCase):
    def test_resume_fixture_planner_admits_the_first_conversation_and_retains_the_live_conversation(self) -> None:
        with (mock.patch.object(test_stop_resume, "write_config_fixture", write_pinned_fixture_setting),
              mock.patch.object(test_stop_resume, "write_machine_config", shutdown_fixture_configuration),
              test_stop_resume.producer_first_fixture("claude") as world):
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    code, output, errors = self.invoke(["resume", "fixture-planner"])
                    self.assertEqual(code, 0, errors + output)
                    self.assertIn(world.identity, output)
            row = staff.initial_snapshot().rows[0]
            self.assertEqual((row.name, row.kind, row.current_session), ("fixture-planner", "producer", world.identity))
            self.assertEqual(len(world.arguments), 1)
            self.assertEqual(world.openings, [herdr.NoOpeningPrompt()])

    def test_status_excludes_a_retired_unit_whose_process_still_runs(self) -> None:
        from scripts.agents.test_inventory import InventoryTests, TARGET_LOGIN, UNIT_ID, DESKTOP_ID
        from scripts.whoami.account import Account

        fixture = InventoryTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        row = staff.snapshot().by_current_session(UNIT_ID)
        assert row is not None and isinstance(row.worktree, staff.PresentWorktree)
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(row.worktree.machine, row.worktree.path),
                               expected=row.worktree)
        with mock.patch.object(status_entry, "_selected_account", return_value=Account("claude", TARGET_LOGIN, "claude2")):
            code, output, errors = self.invoke(["status", "--here", "--json"])
            self.assertEqual(code, 0, errors)
            report = cast(dict[str, object], json.loads(output))
            observed = cast(list[dict[str, object]], report["sessions"])
            self.assertNotIn(UNIT_ID, {item["session_id"] for item in observed})
            self.assertIn(DESKTOP_ID, {item["session_id"] for item in observed})
            code, output, errors = self.invoke(["status", "--here"])
            self.assertEqual(code, 0, errors)
            self.assertNotRegex(output, rf"(?m)^  \S+ {re.escape(row.name)} · ")
        self.assertIn(UNIT_ID, {item["sessionId"] for item in sessions.live_sessions()})

    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            try:
                status = shutdown.main(arguments)
            except SystemExit as error:
                self.assertIsInstance(error.code, int)
                status = int(str(error.code))
        return status, output.getvalue(), errors.getvalue()

    def test_no_arguments_print_the_help_usage_and_exit_zero(self) -> None:
        empty = self.invoke([])
        self.assertEqual(empty[0], 0)
        self.assertEqual(empty, self.invoke(["help"]))
        self.assertTrue(empty[1].startswith("usage: agents"))
        self.assertEqual(empty[2], "")

    def test_help_lists_only_public_commands_with_their_arguments(self) -> None:
        status, output, errors = self.invoke(["help"])
        self.assertEqual(status, 0)
        self.assertEqual(errors, "")
        self.assertEqual([line.split()[1] for line in output.splitlines()[1:]],
                         ["help", "status", "pause", "exit", "pause", "resume", "resume", "resume", "retire", "rename", "move",
                          "restart", "stop", "remove-stray", "transfer", "add-producer", "set-model"])
        self.assertIn("resume NAME", output)
        self.assertIn("resume PRODUCER --resend-opening-prompt", output)
        self.assertIn("transfer NAME [--model MODEL --effort EFFORT]", output)
        self.assertIn("add-producer NAME [--model MODEL] [--effort EFFORT] [--job TEXT]", output)
        self.assertIn("set-model ROLE MODEL [EFFORT]", output)
        self.assertIn("retire NAME [--to PRODUCER]", output)
        self.assertIn("stop SESSION", output)
        self.assertIn("remove-stray NAME SESSION", output)
        self.assertIn("[ACCOUNT]", output)
        self.assertIn("--dry-run", output)
        for command in ("records", "held-sessions", "remote-control"):
            self.assertNotIn(command, output)

    def test_transfer_gives_the_name_and_settings_to_the_producer_transfer(self) -> None:
        with mock.patch.object(producer_transfer, "transfer", return_value=0) as transfer:
            self.assertEqual(self.invoke(["transfer", "fixture_keeper", "--model", "opus", "--effort", "high"])[0], 0)
            self.assertEqual(self.invoke(["transfer", "fixture_organizer"])[0], 0)
        self.assertEqual(transfer.call_args_list, [mock.call("fixture_keeper", model="opus", effort="high"),
                                                   mock.call("fixture_organizer", model="", effort="")])
        with mock.patch.object(producer_transfer, "transfer", side_effect=ValueError("fixture_keeper: no producer row")):
            code, output, errors = self.invoke(["transfer", "fixture_keeper"])
        self.assertEqual((code, output), (2, ""))
        self.assertIn("fixture_keeper: no producer row", errors)

    def test_help_option_prints_the_same_public_usage(self) -> None:
        option = self.invoke(["--help"])
        self.assertEqual(option[0], 0)
        self.assertTrue(option[1].startswith("usage: agents"))
        self.assertEqual(option, self.invoke(["help"]))

        root = Path(__file__).resolve().parents[2]
        status_help = subprocess.run(
            [str(root / "scripts/lib/py"), "-c", """
import runpy
import sys

sys.modules["scripts.agents.producer_lifecycle"] = None
sys.argv = [sys.argv[1], "status", "--help"]
runpy.run_path(sys.argv[0], run_name="__main__")
""", str(root / "scripts/agents/shutdown.py")],
            cwd=root, capture_output=True, text=True, check=False, timeout=30,
        )
        code, output, errors = self.invoke(["status", "--help"])
        self.assertEqual((status_help.returncode, status_help.stdout, status_help.stderr),
                         (code, output, errors))
        self.assertEqual(code, 0)
        self.assertIn("positional arguments:\n  account", output)

        with (mock.patch.object(set_model, "COMMAND", "select-model"),
              mock.patch.object(set_model, "ARGUMENTS", "TARGET MODEL")):
            for arguments in (["--help"], ["status", "--help"]):
                with self.subTest(arguments=arguments):
                    code, output, errors = self.invoke(arguments)
                    self.assertEqual((code, errors), (0, ""))
                    self.assertIn("agents select-model TARGET MODEL", output)

    def test_unknown_command_prints_usage_and_exits_two(self) -> None:
        status, output, errors = self.invoke(["unknown-command"])
        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        usage = self.invoke(["help"])[1]
        self.assertTrue(usage.startswith("usage: agents"))
        self.assertIn(usage, errors)


DIRECTOR = "11111111-1111-1111-1111-111111111111"

RUN_COMMAND = shutdown.retirement_run

SEAT = "22222222-2222-2222-2222-222222222222"

STOP_SESSION = stop_resume.stop

SHUTDOWN_FIXTURE_SETTINGS = {
    "staff": """\
# range: 0.001..
OPERATION_LOCK_SECONDS=30
# range: 0.001..
REMOTE_SECONDS=20
PRODUCER_WORKSPACE=producers
PRODUCER_FOLDER_ROOT=~/rust/hanadocs
""",
    "shutdown": """\
# whole: 1..300
STOP_GRACE_SECONDS=2
# whole: 1..300
KILL_GRACE_SECONDS=5
# whole: 1..300
MOVE_STOP_MARGIN_SECONDS=3
# range: 5e-324..
START_SECONDS=20
# range: 5e-324..
OPENING_PROMPT_SECONDS=90
# range: 5e-324..
STAY_SECONDS=10
# range: 0..300
OWN_PANE_SHELL_SECONDS=30
# range: 0..3600
RESUME_CHECK_SECONDS=180
# range: 5e-324..300
RESUME_CHECK_INTERVAL_SECONDS=5
""",
    "production": """\
# range: 5e-324..
RETIRE_POLL_SECONDS=0.2
# range: 5e-324..
RETIRE_TERM_WAIT_SECONDS=10
# range: 5e-324..
RETIRE_COMMAND_TIMEOUT_SECONDS=900
# range: 5e-324..
RENAME_LIVE_WAIT_SECONDS=180
# range: 5e-324..
RENAME_RUN_STOP_WAIT_SECONDS=120
# range: 5e-324..
RENAME_POLL_SECONDS=1
# range: 5e-324..
RENAME_COMMAND_TIMEOUT_SECONDS=900
# whole: 1..
unit_port_first=15720
# whole: 1..
unit_port_last=15789
# whole: 1..
smoke_port=15790
# whole: 1..
mac_run_port=15710
""",
    "direct": """\
# range: 0.1..0.9
WORKER_SPLIT_RATIO=0.667
WORKER_FIRST_TURN_TEXT=Reply with ready. Do not use tools.
# range: 1..600
WORKER_FIRST_TURN_SECONDS=120
# range: 1..1800
WORKER_COMPACT_SECONDS=300
# range: 0.01..600
WORKER_PUBLISH_LOCK_SECONDS=15
# range: 1..600
PANE_AGENT_START_SECONDS=90
# whole: 1..
UNIT_COMPACT_ABOVE_TOKENS=200000
""",
    "build_folders": """\
# whole: 1..
names.suffix_length=8
# whole: 1..64
names.repository_key_length=8
names.suffix_characters=abcdefghijklmnopqrstuvwxyz0123456789_
roots.system=/tmp
roots.temporary=${TMPDIR}/builds
roots.gate=/tmp
roots.session=~/session
roots.workspace=~/worktrees
roots.scratch=~/scratch
roots.copies=~/copies
roots.build=~/build
# one of: report, enforce
folders.check=report
patterns.temporary={repository}/{owner}-{purpose}-{suffix}
patterns.tool={purpose}{suffix}
patterns.legacy-unique={purpose}{suffix}
patterns.legacy-stable={purpose}
patterns.editable={repository}-{owner}/{purpose}
patterns.named={showrunner}-{unit}/{work}-{purpose}
patterns.gate={purpose}-{suffix}
patterns.scratch={purpose}-{suffix}
patterns.target={purpose}
# range: 0.001..1
locks.probe_retry_seconds=0.005
# range: 0.001..60
locks.probe_wait_seconds=0.001
# whole: 0..1
seed.enabled=0
# whole: 1..
seed.max_gib=24
# whole: 0..
seed.min_free_gib=350
prefixes.copies-index=copies-index-
""",
    "copies": """\
remote_registry=~/remote-mirrors
wait_seconds=0.01
# range: 5e-324..
git_timeout_seconds=10
# range: 5e-324..
guard_timeout_seconds=10
guard_choice_limit=32
owner.verify=verify
""",
    "machines": """\
aliases=natedev mac
# one of: linux, darwin
natedev.platform=linux
natedev.hosts=natedev
# one of: linux, darwin
mac.platform=darwin
mac.hosts=Nates-MacBook-Pro-2023
""",
    "scratch": """\
visible_root=~/rust/repro
holds_file=~/.local/state/scratch/holds.json
# whole: 1..
default_life_seconds=86400
# whole: 1..
repo_depth=3
# whole: 1..
suffix_length=6
repo_skip=target node_modules .git
# whole: 1..
record_lock_attempts=3
# whole: 1..
removal_remote_timeout_seconds=30
""",
    "codex_daemon": """\
# range: 0.001..
timeout_seconds=5.0
# range: 0.001..
queue_timeout_seconds=30
agents_slice=app-agents.slice
""",
    "postmaster": """\
# range: 0.001..
wait_seconds=3.0
# range: 0.001..
claim_grace_seconds=15.0
# range: 0.001..
heartbeat_max_age_seconds=2.0
# range: 0.001..
poll_seconds=0.05
""",
    "send": """\
# range: 0.001..
waits_review_seconds=30
# range: 0.001..
delivery_timeout=40
# range: 0.001..
kill_grace=10
# range: 0.001..
user_cancel_timeout=30
# range: 0.001..
check_timeout=20
# range: 0.001..
caller_margin_seconds=15
# range: 0.001..
remote_transport_seconds=30
""",
    "build_steps": """\
names.test-affected=test affected
names.gate=gate
names.integrate=integrate
names.test-all=test all
names.test-modules=test named
prose.gate=the gate
prose.build=release build
owned.default={unit} {step}
owned.showrunner-default=showrunner {step}
owned.ci-default=CI {step}
""",
    "showrunners": """\
# range: 0..100
threshold_percent=2
# range: 0..
repeat_minutes=30
# range: 0..
stall_minutes=5
""",
    "script_prompts": """\
# whole: 1..
max_records=4096
""",
}


def shutdown_fixture_configuration(root: Path) -> dict[str, str]:
    """Supply fixed settings to the shared repository fixtures."""
    root.mkdir(parents=True, exist_ok=True)
    for stem, content in SHUTDOWN_FIXTURE_SETTINGS.items():
        destination = root / f"{stem}.conf"
        if not destination.exists():
            if stem == "copies":
                content = content.replace("~/remote-mirrors", str(root / "remote-mirrors"))
            _ = destination.write_text(content, encoding="utf-8")
    return {"CONF_DIR": str(root), "COPIES_CONFIG": str(root / "copies.conf"),
            "BUILD_TARGETS_CONFIG": str(root / "build_targets.conf")}


def retirement_copies_environment(root: Path) -> dict[str, str]:
    private = build_slots_test_support.private_slot_config(root, repository="fixture", default_maximum=2)
    return {**private.environment, "HOME": str(root), "SCRATCH_COPIES_ROOT": str(root / "copies")}


def write_pinned_fixture_setting(path: Path, overrides: str = "") -> int:
    """Write only settings that the shutdown fixtures use."""
    content = SHUTDOWN_FIXTURE_SETTINGS.get(path.stem, "")
    if not content:
        if overrides:
            raise AssertionError(f"Unexpected fixture settings for {path}")
        return 0
    lines = content.splitlines()
    for assignment in overrides.splitlines():
        key, _, _ = assignment.partition("=")
        index = next(index for index, line in enumerate(lines) if line.startswith(key + "="))
        lines[index] = assignment
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fixture_git_environment() -> dict[str, str]:
    """Use private Git settings and ignore the caller's Git repository."""
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TEMPLATE_DIR": "",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "maintenance.auto",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "commit.gpgsign",
        "GIT_CONFIG_VALUE_1": "false",
    }


def setUpModule() -> None:
    unittest.enterModuleContext(foreign_hostname())
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/New_York"}))
    isolate_state_home()
    root = Path(unittest.enterModuleContext(tempfile.TemporaryDirectory(prefix="shutdown-config-")))
    environment = {**fixture_git_environment(), **shutdown_fixture_configuration(root)}
    ShutdownStaffFixture.owner_server = SharedStaffOwner(root)
    unittest.addModuleCleanup(ShutdownStaffFixture.owner_server.close)
    unittest.enterModuleContext(mock.patch.dict(os.environ, environment, clear=True))
    _ = unittest.enterModuleContext(mock.patch.object(staff_test_support, "write_config_fixture", write_pinned_fixture_setting))
    _ = unittest.enterModuleContext(mock.patch.object(build_slots_test_support, "config_environment", shutdown_fixture_configuration))
    _ = unittest.enterModuleContext(mock.patch.object(rename_unit_test_support, "config_environment", shutdown_fixture_configuration))
    _ = unittest.enterModuleContext(mock.patch.object(remote_test_support, "copies_environment", retirement_copies_environment))
    _ = unittest.enterModuleContext(mock.patch.object(socket_test_support, "config_environment", shutdown_fixture_configuration))
    _ = unittest.enterModuleContext(mock.patch.object(PaneWorld, "setup", setup_pinned_panes))

def git(where: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(where), *arguments], check=True, capture_output=True,
                          text=True, env=fixture_git_environment()).stdout.strip()

def session(session_id: str, name: str, pid: int) -> SessionRecord:
    return SessionRecord(pid=pid, sessionId=session_id, name=name, messagingSocketPath="/nonexistent.sock",
                         updatedAt=0, tmux="", formerNames=[])

@final
class MoveTests(unittest.TestCase):
    def test_help_teaches_only_numbered_claude_targets(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as result:
            _ = shutdown.main(["move", "--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("<claudeN>", output.getvalue())
        self.assertNotIn("default", output.getvalue())

    def test_session_delegates_once_with_the_reason_and_preserves_the_result(self) -> None:
        with mock.patch.object(accounts, "main", return_value=1) as move:
            code = shutdown.main(["move", "TEST-nightly-review", "claude 2", "--why", "weekly limit"])
        self.assertEqual(code, 1)
        move.assert_called_once_with(["move", "TEST-nightly-review", "claude2", "--why", "weekly limit"])

    def test_group_and_preview_delegate_to_the_same_accounts_command(self) -> None:
        for group in ("units", "showrunners"):
            for preview in (False, True):
                with self.subTest(group=group, preview=preview), mock.patch.object(
                        accounts, "main", return_value=0) as move:
                    argv = ["move", group, "claude1"] + (["--dry-run"] if preview else [])
                    self.assertEqual(shutdown.main(argv), 0)
                move.assert_called_once_with(argv)

    def test_session_id_preview_keeps_the_reason_output_and_error(self) -> None:
        identity = "11111111-2222-4333-8444-555555555555"

        def move(arguments: list[str]) -> int:
            self.assertEqual(arguments, ["move", identity, "claude2", "--dry-run", "--why", "TEST reason"])
            print("TEST move report")
            print("TEST move anomaly", file=sys.stderr)
            return 2

        with (mock.patch.object(accounts, "main", side_effect=move) as delegated,
              redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors):
            code = shutdown.main(["move", identity, "claude 2", "--dry-run", "--why", "TEST reason"])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "TEST move report\n")
        self.assertEqual(errors.getvalue(), "TEST move anomaly\n")
        self.assertEqual(delegated.call_count, 1)


SETUP_PANES = PaneWorld.setup


@contextmanager
def pinned_fixture_settings(*stems: str) -> Generator[None]:
    """Give a shared fixture pinned text before it opens any config source."""
    sources = {CHECKOUT / "config" / f"{stem}.conf": SHUTDOWN_FIXTURE_SETTINGS[stem] for stem in stems}
    read_text = Path.read_text

    def read(path: Path, encoding: str | None = None, errors: str | None = None,
             newline: str | None = None) -> str:
        if path in sources:
            return sources[path]
        return read_text(path, encoding=encoding, errors=errors, newline=newline)

    with mock.patch.object(Path, "read_text", read):
        yield


def setup_pinned_panes(world: PaneWorld, test: StaffTestCase) -> None:
    with pinned_fixture_settings("codex_daemon", "script_prompts"):
        SETUP_PANES(world, test)


class ShutdownStaffFixture(StaffTestCase):
    owner_server: ClassVar[SharedStaffOwner]

    @cached_property
    @override
    def shared_owner(self) -> SharedStaffOwner:
        return self.owner_server


class ShutdownRepositoryFixture(unittest.TestCase):
    repository_root: ClassVar[Path]
    repository_snapshot: ClassVar[Path]
    snapshot_available: ClassVar[bool] = False
    root: Path = Path()
    checkout: Path = Path()
    worktree: Path = Path()
    doc: Path = Path()

    @classmethod
    @override
    def setUpClass(cls) -> None:
        folder = cls.enterClassContext(tempfile.TemporaryDirectory(prefix="shutdown-repository-"))
        cls.repository_root = Path(folder).resolve() / "case"
        cls.repository_snapshot = cls.repository_root.with_name("snapshot")
        cls.repository_root.mkdir()
        builder = cls()
        builder.root = cls.repository_root
        with mock.patch.dict(os.environ, {"HOME": str(builder.root)}):
            builder.build_repository()
        _ = shutil.copytree(cls.repository_root, cls.repository_snapshot)
        shutil.rmtree(cls.repository_root)
        cls.snapshot_available = True
        cls.addClassCleanup(setattr, cls, "snapshot_available", False)

    def bind_repository(self) -> None:
        self.checkout = self.root / "checkout"
        self.worktree = self.root / "alpha-wt"
        self.doc = self.checkout / "alpha-production.md"

    def restore_repository(self) -> None:
        cls = type(self)
        if cls.snapshot_available:
            cls.snapshot_available = False
            self.addCleanup(setattr, cls, "snapshot_available", True)
            self.root = cls.repository_root
            self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
            _ = shutil.copytree(cls.repository_snapshot, self.root)
            self.bind_repository()
        else:
            # Nested fixtures need separate roots to preserve the outer repository.
            self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
            with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
                self.build_repository()

    def build_repository(self) -> None:
        raise NotImplementedError


class RetireUnitTests(ShutdownRepositoryFixture):
    @cached_property
    def pane_world(self) -> PaneWorld:
        return PaneWorld()

    def test_retirement_retains_the_identity_and_excludes_it_from_active_views(self) -> None:
        from scripts.production import census, unit_phases, live_units
        from scripts.production import session_staff

        row = staff.by_id(self.unit_staff_id)
        assert row is not None
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        record = staff.snapshot()
        self.assertEqual(record.by_id(row.id), replace(row,
            worktree=staff.RemovedWorktree(remote.local_machine(), self.worktree, staff.ParkedBranch("alpha-branch"))))
        self.assertNotIn(row.id, {item.id for item in staff.active(record, "unit")})
        self.assertEqual(session_staff.production_members(record)["alpha"], ())
        observed = session_staff.group_labeled_sessions((), record=record, sources=session_staff.MemberSources(()))
        listing = live_units.units_in("alpha", observed)
        rendered = unit_phases.table(listing, {}, snapshot=record, census_read=census.Census((), (), ()))
        self.assertEqual(rendered.splitlines(), [
            "working 0 · waiting to merge 0 · waiting on other work 0 · idle 0 · 0 units",
        ])


        for foreign, placement in ((False, "retained unit shells"), (True, "foreign row"),
                                   (False, "director gone"), (False, "worker in another tab")):
            with self.subTest(tab=placement):
                case = RetireUnitTests()
                case.setUp()
                try:
                    owner = staff.by_id(case.unit_staff_id)
                    assert owner is not None
                    case.pane_world.session = "alpha"
                    worker = staff.snapshot().find(kind="worker", parent=owner.id)[0]
                    case.pane_world.record(worker, "w1:p2")
                    if placement != "director gone":
                        case.pane_world.add(owner, "w1:p1", live=False)
                    if placement == "worker in another tab":
                        case.pane_world.add(worker, "w1:p4", live=False, tab="w1:t2")
                    sibling = (fixture_member("unit", name="other", parent=owner.parent.id)
                               if foreign and isinstance(owner.parent, staff.ReportsTo) else owner)
                    if placement in ("retained unit shells", "foreign row"):
                        case.pane_world.add(sibling, "w1:p3", live=False)
                        case.pane_world.record(sibling, "w1:p3")
                    code, output = case.retire()
                    case.assertEqual(code, 0, output)
                    panes = {pane["pane_id"] for pane in case.pane_world.panes() if pane.get("tab_id") == "w1:t1"}
                    case.assertEqual(panes, {"w1:p3"} if foreign else set())
                    case.assertNotIn("w1:p4", case.pane_world.locations)
                    case.assertIsNotNone(staff.by_id(worker.id))
                finally:
                    case.doCleanups()

    def test_retirement_continues_after_a_member_stop_raises(self) -> None:
        def stop(identity: str, *, deadline: float = math.inf) -> stop_resume.Stopped | stop_resume.NotRunning:
            if identity == DIRECTOR:
                raise OSError("stop read failed")
            return self.stop_session(identity, deadline=deadline)

        with (mock.patch.object(stop_resume, "stop", side_effect=stop) as stops,
              mock.patch.object(shutdown, "retirement_refusals", return_value=[]),
              mock.patch.object(shutdown, "retirement_remove", return_value=[]) as remove):
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(stops.call_count, 2)
        self.assertEqual(output.count("stop read failed"), 1)
        remove.assert_called_once()

    def test_retire_keeps_stopped_worker_rows(self) -> None:
        row = staff.snapshot().find(kind="worker", parent=self.unit_staff_id)[0]
        states: list[worker_panes.WorkerPane] = [worker_panes.OpenWorker(row, "outside-pane", "same-terminal", herdr.AgentStatus.WORKING)]
        self.marked = None
        self.claude = [item for item in self.claude if item["sessionId"] != DIRECTOR]
        def observe(_parent: staff.StaffId, _record: staff.StaffSnapshot) -> list[worker_panes.WorkerPane]:
            return list(states)
        def stop(member: staff.StaffRow, client: herdr.Herdr, *, deadline: float) -> bool:
            del client
            self.assertEqual(member, row)
            self.assertGreater(deadline, time.monotonic())
            states[0] = worker_panes.StoppedWorker(row, "outside-pane", "same-terminal")
            return True
        with (mock.patch.object(worker_panes, "worker_run", return_value=self.run_folder),
              mock.patch.object(worker_panes, "observe", side_effect=observe),
              mock.patch.object(restart, "stop_row", side_effect=stop) as stops):
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(stops.call_count, 1)
        self.assertEqual(staff.by_id(row.id), row)
        unit = staff.by_id(self.unit_staff_id)
        assert unit is not None
        self.assertIsInstance(unit.worktree, staff.RemovedWorktree)
        self.assertEqual(states, [worker_panes.StoppedWorker(row, "outside-pane", "same-terminal")])

    def test_retirement_uses_the_staff_worktree(self) -> None:
        plan = self.worktree / "docs/plans/alpha.md"
        plan.parent.mkdir(parents=True, exist_ok=True)
        _ = plan.write_text("# Plan\n")
        _ = git(self.worktree, "add", str(plan))
        _ = git(self.worktree, "commit", "-m", "Retain the unit plan")
        _ = git(self.worktree, "push", "origin", "alpha-branch")
        _ = git(self.checkout, "merge", "--no-ff", "-m", "Merge the unit plan", "alpha-branch")
        director = self.root / "director-folder"
        _ = git(self.worktree, "checkout", "--detach")
        _ = git(self.checkout, "worktree", "add", str(director), "alpha-branch")
        _ = unit_config.write(director, unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
                                                             unit_config.AppPort(1), ("scripts",)))
        _ = staff.set_worktree(self.unit_staff_id, staff.PresentWorktree(remote.local_machine(), director), expected=staff.PresentWorktree(remote.local_machine(), self.worktree))
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertTrue(director.is_dir())
        self.assertTrue(self.worktree.exists())
        self.assertIn(f"kept for a manual removal list: {director}", output)
        self.assertNotIn(f"kept for a manual removal list: {self.worktree}", output)
        row = staff.snapshot().by_id(self.unit_staff_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertIsInstance(row.worktree, staff.RemovedWorktree)


    staff: StaffTestCase = StaffTestCase()
    root: Path = Path()
    checkout: Path = Path()
    worktree: Path = Path()
    doc: Path = Path()
    run_folder: Path = Path()
    calls: list[list[str]] = []
    claude: list[SessionRecord] = []
    marked: unit_lookup.LabeledSession | None = None
    config: Path = Path()
    unit_staff_id: StaffId = StaffId("")

    def limits(self, *, poll: str = "0.2", wait: str = "10", command: str = "900") -> Path:
        path = self.root / "production.conf"
        _ = path.write_text(f"# range: 5e-324..\nRETIRE_POLL_SECONDS={poll}\n# range: 5e-324..\nRETIRE_TERM_WAIT_SECONDS={wait}\n"
                            + f"# range: 5e-324..\nRETIRE_COMMAND_TIMEOUT_SECONDS={command}\n"
                            + "# whole: 40..\nunits_table_columns=170\n"
                            + "# whole: 40..\nunits_table_floor_columns=120\n"
                            + "# whole: 1..4\nunits_table_cell_lines=4\n"
                            + "# whole: 2..\nunits_table_break_columns=30\n")
        return path


    def test_retire_removes_only_the_units_idle_trees_and_workspaces(self) -> None:
        with copies.standing(str(self.unit_staff_id), self.checkout, copies.CommittedSource("HEAD")) as tree:
            cache = (tree / "target").resolve().parent
            with copies.standing("other-unit", self.checkout, copies.CommittedSource("HEAD")) as other:
                other_cache = (other / "target").resolve().parent
            self.assertNotEqual(tree, other)
        with registered_workspace(
            str(self.unit_staff_id), self.checkout, "HEAD", name="worker", branch="worker", task="gate:retire-worker",
        ) as workspace:
            pass
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertTrue(tree.exists())
        self.assertEqual(build_folders.build_folder_state(tree), "free")
        self.assertFalse(workspace.exists())
        self.assertTrue(other.exists())
        self.assertEqual(build_folders.build_folder_state(other), "free")
        self.assertTrue(cache.exists())
        self.assertEqual(build_folders.build_folder_state(cache), "free")
        self.assertTrue(other_cache.is_dir())

    def test_retire_keeps_a_same_named_units_copy_in_another_production(self) -> None:
        from scripts.production.staff_test_support import fixture_member

        parent = fixture_member("showrunner", name="other-production")
        other = fixture_member("unit", name="alpha", parent=parent.id)
        with copies.standing(str(self.unit_staff_id), self.checkout, copies.CommittedSource("HEAD")) as mine:
            mine_cache = (mine / "target").resolve().parent
            with copies.standing(str(other.id), self.checkout, copies.CommittedSource("HEAD")) as theirs:
                their_cache = (theirs / "target").resolve().parent
                their_commit = git(theirs, "rev-parse", "HEAD")
            self.assertNotEqual(mine, theirs)
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertTrue(mine.exists())
        self.assertEqual(build_folders.build_folder_state(mine), "free")
        self.assertTrue(theirs.exists())
        self.assertEqual(build_folders.build_folder_state(theirs), "free")
        self.assertTrue(mine_cache.exists())
        self.assertEqual(build_folders.build_folder_state(mine_cache), "free")
        self.assertTrue(their_cache.is_dir())
        self.assertEqual(git(their_cache, "rev-parse", "HEAD"), their_commit)
        self.assertEqual(their_commit, git(self.checkout, "rev-parse", "HEAD"))

    def test_retire_without_a_build_cache_has_no_standing_tree_to_remove(self) -> None:
        _ = git(self.worktree, "rm", "Cargo.toml")
        _ = git(self.worktree, "commit", "-qm", "remove the root build manifest")
        _ = git(self.worktree, "push", "-q", "origin", "alpha-branch")
        _ = git(self.checkout, "merge", "-q", "--no-ff", "-m", "Merge manifest removal", "alpha-branch")
        with copies.standing(str(self.unit_staff_id), self.worktree, copies.CommittedSource("HEAD")) as tree:
            self.assertTrue(tree.is_relative_to(Path(tempfile.gettempdir())))
            self.assert_slot_held(tree)
            self.assertEqual(git(tree, "rev-parse", "HEAD"), git(self.worktree, "rev-parse", "HEAD"))
        self.assertTrue(tree.exists())
        self.assertEqual(build_folders.build_folder_state(tree), "free")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(build_folders.build_folder_state(tree), "free")

    def assert_slot_held(self, path: Path) -> None:
        """Read the lock after the fixture acquires its lease."""
        with mock.patch.object(build_folders, "_probe_timing",
                               return_value=build_folder_lock.ProbeTiming(0, 0.001)):
            self.assertEqual(build_folders.build_folder_state(path), "held")

    def test_retire_keeps_a_busy_tree_and_still_retires_the_unit(self) -> None:
        with copies.standing(str(self.unit_staff_id), self.checkout, copies.CommittedSource("HEAD")) as tree:
            code, output = self.retire()
            self.assertEqual(code, 0, output)
            self.assertNotIn("busy", output)
            self.assert_slot_held(tree)
            self.assertTrue(tree.exists())
        self.assertTrue(self.worktree.is_dir())

    def test_retirement_retry_removes_a_tree_kept_while_busy(self) -> None:
        with copies.standing(str(self.unit_staff_id), self.checkout, copies.CommittedSource("HEAD")) as tree:
            code, output = self.retire()
            self.assertEqual(code, 0, output)
            self.assertNotIn("busy", output)
            self.assert_slot_held(tree)
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertIn("unchanged", output)
        self.assertTrue(tree.exists())
        self.assertEqual(build_folders.build_folder_state(tree), "free")

    def park_without_worktree(self) -> None:
        row = staff.by_id(self.unit_staff_id)
        assert row is not None
        _ = git(self.checkout, "worktree", "remove", "--force", str(self.worktree))
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree,
            staff.ParkedBranch("alpha-branch")), expected=row.worktree)

    def test_recovery_recreates_the_checkout_after_manual_removal(self) -> None:
        base = git(self.checkout, "rev-parse", "main")
        record = worktrees.new_record(self.worktree, "alpha-branch", self.checkout, base, "alpha", "main")
        record_path = worktrees.record_file(self.worktree)
        worktrees.write_record(record)
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertTrue(self.worktree.is_dir())
        _ = git(self.checkout, "worktree", "remove", "--force", str(self.worktree))
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(git(self.checkout, "branch", "--list", "alpha-branch"), "")
        self.assertEqual(worktrees.read_record(record_path), record)

        unit = shutdown.recovery_find_unit("alpha", str(self.unit_staff_id))
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        self.assertEqual(shutdown.recovery_refusals(unit), [])
        with redirect_stdout(io.StringIO()):
            shutdown.recovery_make_worktree(unit)

        self.assertEqual(git(self.worktree, "branch", "--show-current"), record.branch)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), base)
        layout = worktrees.output_record("origin", "alpha", "worktree")
        self.assertEqual(worktrees.read_record(record_path), replace(record, layout=layout))
        self.assertEqual((self.worktree / "target").readlink(), layout.output)

    def test_retirement_refuses_a_rebase_in_the_main_checkout(self) -> None:
        branch = "refs/heads/alpha-branch"
        tip = git(self.checkout, "rev-parse", branch)
        _ = git(self.checkout, "checkout", "--detach", tip)
        state = Path(git(self.checkout, "rev-parse", "--absolute-git-dir")) / "rebase-merge"
        state.mkdir()
        write_text_atomic(state / "head-name", branch + "\n")
        code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn(f"branch alpha-branch is rebasing in {self.checkout}", output)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(git(self.root / "origin.git", "rev-parse", branch), tip)
        self.assertEqual(self.calls, [])

    def test_retirement_retry_refuses_a_rebase_after_the_unit_checkout_is_absent(self) -> None:
        self.park_without_worktree()
        branch = "refs/heads/alpha-branch"
        tip = git(self.checkout, "rev-parse", branch)
        other = self.root / "rebasing-worktree"
        _ = git(self.checkout, "worktree", "add", "--detach", str(other), tip)
        state = Path(git(other, "rev-parse", "--absolute-git-dir")) / "rebase-apply"
        state.mkdir()
        write_text_atomic(state / "head-name", branch + "\n")
        code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn(f"branch alpha-branch is rebasing in {other}", output)
        self.assertEqual(git(self.root / "origin.git", "rev-parse", branch), tip)
        self.assertEqual(self.calls, [])

    def test_retirement_ignores_a_stale_linked_checkout_on_another_branch(self) -> None:
        stale = self.root / "stale"
        _ = git(self.checkout, "worktree", "add", "-b", "other-branch", str(stale), "main")
        (stale / ".git").unlink()
        listed = git(self.checkout, "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {stale}\n", listed)
        self.assertIn("branch refs/heads/other-branch", listed)
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertTrue(stale.is_dir())

    def test_retirement_retry_preserves_the_remote_branch_if_local_deletion_fails(self) -> None:
        self.park_without_worktree()
        branch = "refs/heads/alpha-branch"
        tip = git(self.checkout, "rev-parse", branch)
        _ = git(self.checkout, "checkout", "alpha-branch")
        code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("cannot delete branch 'alpha-branch' used by worktree", output)
        self.assertEqual(git(self.checkout, "rev-parse", branch), tip)
        self.assertEqual(git(self.root / "origin.git", "rev-parse", branch), tip)

    def test_retirement_refuses_a_failed_worktree_scan_before_inspecting_git_files(self) -> None:
        def fail_scan(argv: list[str], cwd: Path | None = None, *,
                      settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT
                      ) -> subprocess.CompletedProcess[str]:
            if argv[3:] == ["worktree", "list", "--porcelain", "-z"]:
                self.assertIsInstance(settings, conf.ConfigRead)
                return subprocess.CompletedProcess(argv, 128, f"worktree {self.root}\0\0", "scan failed")
            return self.fake_run(argv, cwd=cwd, settings=settings)

        with mock.patch.object(shutdown, "retirement_run", side_effect=fail_scan):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("retire: cannot list Git worktrees: scan failed", output)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(self.calls, [])


    def test_one_retirement_shares_settings_through_validation_and_cleanup(self) -> None:
        with mock.patch.object(conf, "read", wraps=conf.read) as reads:
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual([call.args[0] for call in reads.call_args_list if call.args[0] == "production"],
                         ["production"])
        self.assertTrue(self.worktree.is_dir())

    def test_external_command_reads_current_settings_for_the_named_request(self) -> None:
        with mock.patch.object(conf, "read", wraps=conf.read) as reads:
            result = RUN_COMMAND([sys.executable, "-c", "print('fixture command')"],
                                 settings=UseConfiguredValue.CURRENT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "fixture command\n")
        reads.assert_called_once_with("production")

    def test_external_termination_reads_current_settings_for_the_named_request(self) -> None:
        with (mock.patch.object(conf, "read", wraps=conf.read) as reads,
              mock.patch.object(os, "kill") as send,
              mock.patch.object(shutdown, "retirement_alive", return_value=False)):
            left = shutdown.retirement_terminate([1234], {1234: "fixture"}, settings=UseConfiguredValue.CURRENT)
        self.assertEqual(left, [])
        send.assert_called_once_with(1234, signal.SIGTERM)
        reads.assert_called_once_with("production")

    def test_sigterm_wait_reads_the_retirement_settings_once(self) -> None:
        with (mock.patch.object(conf, "read", wraps=conf.read) as reads,
              mock.patch.object(os, "kill") as send,
              mock.patch.object(shutdown, "retirement_alive", return_value=False)):
            self.assertEqual(shutdown.retirement_terminate([1234], {1234: "fixture"}), [])
        send.assert_called_once_with(1234, signal.SIGTERM)
        reads.assert_called_once_with("production")

    def test_retirement_commands_use_only_the_remaining_operation_budget(self) -> None:
        path = self.limits(command="900")
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}),
              mock.patch.object(time, "monotonic", return_value=100.0),
              worker_lifecycle.operation_deadline(deadline=100.25),
              mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run):
            _ = RUN_COMMAND(["fixture-command"])
            self.assertEqual(run.call_args.kwargs["timeout"], 0.25)
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}),
              mock.patch.object(time, "monotonic", return_value=100.0),
              worker_lifecycle.operation_deadline(deadline=99.0),
              mock.patch.object(subprocess, "run") as run):
            with self.assertRaises(worker_lifecycle.OperationDeadlineExpired):
                _ = RUN_COMMAND(["fixture-command"])
            run.assert_not_called()

    def test_retirement_sigterm_wait_stops_at_the_shared_operation_deadline(self) -> None:
        path = self.limits(poll="1", wait="10")
        now = 100.0
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            nonlocal now
            sleeps.append(seconds)
            now += seconds

        with (mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}),
              mock.patch.object(time, "monotonic", side_effect=lambda: now),
              mock.patch.object(time, "sleep", side_effect=sleep),
              mock.patch.object(os, "kill") as send,
              mock.patch.object(shutdown, "retirement_alive", return_value=True),
              worker_lifecycle.operation_deadline(deadline=100.25)):
            with self.assertRaises(worker_lifecycle.OperationDeadlineExpired):
                _ = shutdown.retirement_terminate([1234], {1234: "fixture"})
        send.assert_called_once_with(1234, signal.SIGTERM)
        self.assertEqual(sleeps, [0.25])

    def test_changed_command_timeout_is_read_at_each_use_without_reloading_the_module(self) -> None:
        path = self.limits(command="17")
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}), \
                mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            _ = RUN_COMMAND(["fixture-command"])
            self.assertEqual(run.call_args.kwargs["timeout"], 17)
            _ = self.limits(command="23")
            _ = RUN_COMMAND(["fixture-command"])
            self.assertEqual(run.call_args.kwargs["timeout"], 23)

    def test_changed_poll_and_term_wait_control_real_sigterm_loop_and_cap_the_last_sleep(self) -> None:
        path = self.limits()
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}):
            for poll, wait in ((0.3, 0.8), (0.7, 1.2)):
                with self.subTest(poll=poll, wait=wait):
                    _ = self.limits(poll=str(poll), wait=str(wait))
                    with (mock.patch.object(os, "kill") as send_signal,
                          mock.patch.object(shutdown, "retirement_alive", return_value=True),
                          mock.patch.object(time, "monotonic", side_effect=[0, 0, wait - 0.1, wait]),
                          mock.patch.object(time, "sleep") as sleep):
                        left = shutdown.retirement_terminate([1234], {1234: "fixture"})
                    send_signal.assert_called_once_with(1234, signal.SIGTERM)
                    self.assertEqual(len(sleep.call_args_list), 2)
                    self.assertEqual(sleep.call_args_list[0].args, (poll,))
                    self.assertAlmostEqual(cast(float, sleep.call_args_list[1].args[0]), 0.1)
                    self.assertEqual(left, ["fixture: still runs after SIGTERM"])

    def test_an_explicit_shorter_wait_caps_the_configured_term_wait(self) -> None:
        path = self.limits(poll="1", wait="10")
        with (mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}),
              mock.patch.object(os, "kill"),
              mock.patch.object(shutdown, "retirement_alive", return_value=True),
              mock.patch.object(time, "monotonic", side_effect=[0, 0, 0.1]),
              mock.patch.object(time, "sleep") as sleep):
            _ = shutdown.retirement_terminate([1234], {1234: "fixture"}, wait=0.1)
        sleep.assert_called_once_with(0.1)


    def test_a_missing_retirement_config_file_refuses_with_its_path_and_key(self) -> None:
        path = self.root / "absent" / "production.conf"
        with mock.patch.dict(os.environ, {"CONF_DIR": str(Path(path).parent)}), self.assertRaises(conf.Unusable) as error:
            _ = RUN_COMMAND(["fixture-command"])
        self.assertIn(str(path), str(error.exception))
        self.assertIn("RETIRE_COMMAND_TIMEOUT_SECONDS", str(error.exception))

    @override
    def build_repository(self) -> None:
        origin = self.root / "origin.git"
        _ = git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.checkout = self.root / "checkout"
        _ = git(self.root, "clone", "-q", str(origin), str(self.checkout))
        for setting in (("user.email", "test@example.invalid"), ("user.name", "test"), ("commit.gpgsign", "false")):
            _ = git(self.checkout, "config", *setting)
        _ = (self.checkout / "Cargo.toml").write_text("[workspace]\n")
        self.worktree = self.root / "alpha-wt"
        self.doc = self.checkout / "alpha-production.md"
        _ = self.doc.write_text(
            "# Production\n\n- **Merge branch:** main\n"
            + f"- **Showrunner checkout:** {self.checkout}\n- **Log:** log.md\n",
            encoding="utf-8")
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "production")
        _ = git(self.checkout, "worktree", "add", "-q", "-b", "alpha-branch", str(self.worktree))
        (self.worktree / "docs/plans").mkdir(parents=True, exist_ok=True)
        _ = (self.worktree / "work.txt").write_text("done\n", encoding="utf-8")
        _ = git(self.worktree, "add", "work.txt")
        _ = git(self.worktree, "commit", "-q", "-m", "checkpoint(alpha): the work")
        _ = git(self.checkout, "push", "-q", "origin", "main", "alpha-branch")
        _ = git(self.checkout, "merge", "-q", "--no-ff", "-m", "Merge alpha", "alpha-branch")
        exclude = self.checkout / ".git/info/exclude"
        exclude.parent.mkdir(exist_ok=True)
        _ = exclude.write_text("/.unit.toml\n", encoding="utf-8")
        _ = (self.worktree / ".unit.toml").write_text(
            'plan = "docs/plans/alpha.md"\nport = 1\nresponsibilities = ["scripts"]\n', encoding="utf-8")

    @override
    def setUp(self) -> None:
        _ = self.enterContext(fake_cargo_output_roots(children=True))
        self.staff = ShutdownStaffFixture()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        self.restore_repository()
        config = shutdown_fixture_configuration(self.root)
        _ = self.limits()
        environment = retirement_copies_environment(self.root)
        _ = (Path(environment["CONF_DIR"]) / "build_folders.conf").replace(self.root / "build_folders.conf")
        self.enterContext(mock.patch.dict(os.environ, {**config, **environment, "CONF_DIR": str(self.root)}))
        runs = self.root / "runs"
        self.run_folder = runs / "run-1"
        self.run_folder.mkdir(parents=True)
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, {"UNIT_RUN_DIR": str(self.run_folder)})))
        holds = self.root / "holds"
        holds.mkdir()
        _ = (holds / DIRECTOR).write_text("{}", encoding="utf-8")

        self.calls = []
        self.claude = [session(DIRECTOR, "alpha", 4101), session(SEAT, "alpha-seat", 4102)]
        _ = self.staff.record_production("alpha", self.doc)
        parent = fixture_member("showrunner", name="alpha")
        self.unit_staff_id = fixture_member("unit", name="alpha", parent=parent.id, runtime_id=DIRECTOR,
                         worktree=staff.PresentWorktree(remote.local_machine(), self.worktree)).id
        member = fixture_member("worker", name="seat", parent=self.unit_staff_id, runtime_id=SEAT)
        atomic_file.write_text_atomic(self.run_folder / "progress_history_state.json",
                                     json.dumps({"owner_staff_id": str(self.unit_staff_id)}))
        self.pane_world.add(member, "w1:p2")
        self.addCleanup(self.pane_world.process_directory.cleanup)
        production = read_production("alpha")

        def production_of(_production_name: str, *, record: staff.StaffSnapshot) -> Production:
            del record
            return production

        def nothing_inside(_worktree: Path) -> list[int]:
            return []

        def no_reservations(_worktree: Path) -> tuple[str, ...]:
            return ()

        def session_processes(session_id: str) -> sessions.SessionProcesses:
            return sessions.SessionProcesses(tuple(item["pid"] for item in self.claude if item["sessionId"] == session_id))

        def session_liveness(session_id: str) -> sessions.LiveSessionProcess | sessions.SessionNotLive:
            owner = next((item for item in self.claude if item["sessionId"] == session_id), None)
            if owner:
                return sessions.LiveSessionProcess(owner["pid"])
            marked = unit_lookup.labeled_session("alpha", "alpha")
            if marked and isinstance(marked.claude, unit_lookup.LiveClaude) and marked.claude.session_id == session_id:
                return sessions.LiveSessionProcess(marked.claude.pid)
            return sessions.SessionNotLive()

        self.enterContext(mock.patch.dict(os.environ, {"RETIRE_UNIT_RUN_ROOT": str(runs),
                                                       "BUILD_HOLD_DIR": str(holds),
                                                       "DESIGN_ROLES_STATE": str(self.root / "design-roles"),
                                                       "DESIGN_OWNERSHIP_STATE": str(self.root / "design-ownership")}))
        _ = self.enterContext(mock.patch.object(shutdown, "MAC_CLAIMS", self.root / "claims"))
        _ = self.enterContext(mock.patch.object(shutdown, "retirement_production_of", production_of))
        _ = self.enterContext(mock.patch.object(shutdown, "retirement_run", self.fake_run))
        _ = self.enterContext(mock.patch.object(stop_resume, "stop", self.stop_session))
        _ = self.enterContext(mock.patch.object(unit_move, "processes_inside", nothing_inside))
        _ = self.enterContext(mock.patch.object(shutdown, "retirement_reservations", no_reservations))
        _ = self.enterContext(mock.patch.object(sessions, "session_processes", session_processes))
        _ = self.enterContext(mock.patch.object(sessions, "session_liveness", session_liveness))
        self.marked = unit_lookup.LabeledSession(
            "alpha", unit_lookup.LiveClaude("alpha", DIRECTOR, "/nonexistent.sock", 4101), HerdrPlace("alpha", "w1:p1"))

        def labeled_session(_production_name: str, _name: str) -> unit_lookup.LabeledSession | None:
            return self.marked

        self.config = self.root / "config"
        self.enterContext(mock.patch.dict(os.environ, {"HERDR_PANE_RECORD_DIR": str(self.root / "panes")}))
        _ = self.enterContext(mock.patch.object(pane_record, "RECORD_DIR", self.root / "panes"))
        _ = self.enterContext(mock.patch.object(staff_pane_record, "RECORD_DIR", self.root / "panes"))
        pane_record.write("alpha", pane_record.launch("w1:p1", DIRECTOR, str(self.worktree),
            {"CLAUDE_CONFIG_DIR": str(self.config), ('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha"}, ["claude", "-n", "alpha"]))
        def process_info(*_arguments: str, deadline: float = math.inf) -> dict[str, object]:
            del deadline
            # Existing cases launch Claude with exec: it replaces the pane's shell.
            pid = self.marked.claude.pid if self.marked and isinstance(self.marked.claude, unit_lookup.LiveClaude) else 4101
            return {"process_info": {"shell_pid": pid}}

        _ = self.enterContext(mock.patch.object(unit_lookup, "labeled_session", labeled_session))
        _ = self.enterContext(mock.patch.object(herdr.Herdr, "result", side_effect=process_info))
        pane_patches = [
            cast(object, self.enterContext(mock.patch.object(worker_panes, "_client", return_value=self.pane_world))),
            cast(object, self.enterContext(mock.patch.object(herdr, "Herdr", return_value=self.pane_world))),
            cast(object, self.enterContext(mock.patch.object(herdr, "native_process", side_effect=self.pane_world.native_process))),
            cast(object, self.enterContext(mock.patch.object(herdr, "_startup_processes", side_effect=self.pane_world.descendants))),
        ]
        self.addCleanup(pane_patches.clear)
        _ = self.enterContext(mock.patch.object(sessions, "config_dirs", lambda: [self.config]))
        _ = self.enterContext(mock.patch.object(session_records, "config_dirs", lambda: [self.config]))
        self.transcript(self.worktree)

    def transcript(self, cwd: Path) -> None:
        folder = self.config / "projects" / unit_move.project_folder(str(cwd))
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / f"{DIRECTOR}.jsonl").write_text(json.dumps({"entrypoint": "cli", "cwd": str(cwd)}) + "\n")

    def fake_run(self, argv: list[str], cwd: Path | None = None, *,
                 settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> subprocess.CompletedProcess[str]:
        """Git runs for real; every other command is recorded and answered."""
        del settings
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        call = [Path(argv[1]).name, *argv[2:]] if argv[0] == sys.executable else argv
        self.calls.append(call)
        if call[:2] == ["build_hold.py", "release"]:
            self.assertEqual(Path(argv[1]), Path(shutdown.__file__).resolve().parents[1] / "build_hold/build_hold.py")
            (Path(os.environ["BUILD_HOLD_DIR"]) / call[call.index("--holder") + 1]).unlink(missing_ok=True)
        stdout = ""
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    def stop_session(self, identity: str, *, deadline: float = math.inf) -> stop_resume.Stopped | stop_resume.NotRunning:
        del deadline
        self.calls.append(["stop_resume.stop", identity])
        owner = next((item for item in self.claude if item["sessionId"] == identity), None)
        self.claude = [item for item in self.claude if item["sessionId"] != identity]
        for pane, (pid, family, arguments) in tuple(self.pane_world.foreground.items()):
            del family, arguments
            if self.pane_world.processes[pid].identity.id == identity:
                del self.pane_world.foreground[pane]
        if self.marked and isinstance(self.marked.claude, unit_lookup.LiveClaude) and self.marked.claude.session_id == identity:
            self.marked = None
        return stop_resume.Stopped(identity, owner["pid"]) if owner else stop_resume.NotRunning(identity)

    def retire(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = shutdown.main(["retire", str(self.unit_staff_id), *arguments])
        return code, output.getvalue()

    def remote_call(self, args: list[str], stdin: str = "", limit: remote.RemoteCallLimit = remote.STANDARD_TIME_LIMIT) -> tuple[int, str]:
        del limit
        output = io.StringIO()
        receiver = "mac"
        row = staff.by_id(self.unit_staff_id)
        if row is not None and not isinstance(row.worktree, staff.WorktreeUnfilled) and row.worktree.machine != "mac":
            receiver = "nates-macbook-pro.example.net"
        with (mock.patch.object(sys, "stdin", io.StringIO(stdin)),
              mock.patch.object(shutdown, "local_machine", return_value=receiver),
              mock.patch.object(remote, "local_machine", return_value=receiver),
              mock.patch.object(shutdown, "other_machine", return_value="natedev"),
              mock.patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "remote-shutdown")}),
              redirect_stdout(output)):
            code = shutdown.main(args)
        return code, output.getvalue()

    def move_to_other_machine(self, machine: str = "mac") -> staff.StaffRow:
        row = staff.by_id(self.unit_staff_id)
        assert row is not None
        row = staff.set_worktree(row.id, staff.PresentWorktree(machine, self.worktree), expected=row.worktree)
        _ = self.enterContext(mock.patch.object(shutdown, "run_remote", side_effect=self.remote_call))
        return row

    def stops(self) -> list[list[str]]:
        return self.calls.copy()

    def unit_worktree(self) -> staff.PresentWorktree | staff.RemovedWorktree | staff.WorktreeUnfilled:
        row = staff.by_id(self.unit_staff_id)
        assert row is not None
        return row.worktree


    def test_retirement_without_saved_work_still_records_the_staff_identity(self) -> None:
        for step, machine in (("complete", "natedev"), ("staff", "natedev"),
                              ("retention", "natedev"), ("retention", "mac")):
            with self.subTest(step=step, machine=machine):
                case = RetireUnitTests()
                case.setUp()
                try:
                    unit = next(row for row in staff.find(kind="unit") if row.name == "alpha")
                    if machine == "mac":
                        unit = staff.set_worktree(unit.id, staff.PresentWorktree(machine, case.worktree), expected=unit.worktree)
                        _ = case.enterContext(case.staff.machine("mac"))
                        _ = case.enterContext(mock.patch.object(shutdown, "local_machine", return_value="mac"))
                        self.assertFalse(staff.is_owner())
                    retain, publish = shutdown.retirement_remove, staff.set_worktree

                    def publish_retirement(identity: staff.StaffId, value: staff.PresentWorktree | staff.RemovedWorktree, *,
                                           expected: staff.PresentWorktree | staff.RemovedWorktree | staff.WorktreeUnfilled) -> staff.StaffRow:
                        result = publish(identity, value, expected=expected)
                        if step == "staff" and isinstance(value, staff.RemovedWorktree):
                            raise SystemExit(91)
                        return result

                    def retain_worktree(selected: shutdown.RetiringUnit, *,
                                        settings: conf.ConfigRead) -> list[str]:
                        failures = retain(selected, settings=settings)
                        if step == "retention" and not failures:
                            raise SystemExit(91)
                        return failures

                    if step != "complete":
                        with (mock.patch.object(staff, "set_worktree", side_effect=publish_retirement),
                              mock.patch.object(shutdown, "retirement_remove", side_effect=retain_worktree),
                              self.assertRaises(SystemExit) as stopped):
                            _ = case.retire()
                        self.assertEqual(stopped.exception.code, 91)
                    code, output = case.retire()
                    self.assertEqual(code, 0, output)
                    self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree(
                        remote.local_machine(), case.worktree, staff.ParkedBranch("alpha-branch"))))
                    self.assertIn(str(case.worktree), git(case.checkout, "worktree", "list", "--porcelain"))
                    self.assertIn("alpha-branch", git(case.checkout, "branch", "--list", "alpha-branch"))
                    published = git(case.checkout, "ls-remote", "--heads", "origin", "alpha-branch")
                    self.assertIn("refs/heads/alpha-branch", published)
                    self.assertEqual(git(case.checkout, "ls-remote", "origin", f"refs/retired/{unit.id}"), "")
                finally:
                    case.doCleanups()

    def test_a_finished_berth_reservation_is_released_on_replay(self) -> None:
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        for stdout, stderr in (("already has a final disposition", ""),
                               ("", "already has a final disposition")):
            with self.subTest(stdout=stdout, stderr=stderr):
                output = io.StringIO()
                with (mock.patch.object(shutdown, "retirement_reservations", return_value=("reservation-1",)),
                      mock.patch.object(shutdown, "retirement_run", return_value=subprocess.CompletedProcess(
                          ["cargo-berth"], 5, stdout, stderr)), redirect_stdout(output)):
                    shutdown.retirement_release_reservations(unit, settings=conf.read("production"))
                self.assertEqual(output.getvalue(), "")
        output = io.StringIO()
        with (mock.patch.object(shutdown, "retirement_reservations", return_value=("reservation-1",)),
              mock.patch.object(shutdown, "retirement_run", return_value=subprocess.CompletedProcess(
                  ["cargo-berth"], 5, "", "reservation could not be released")), redirect_stdout(output)):
            shutdown.retirement_release_reservations(unit, settings=conf.read("production"))
        self.assertIn("reservation could not be released", output.getvalue())


    def orphan_child(self, session_id: str) -> subprocess.Popen[bytes]:
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                 stdin=subprocess.PIPE, cwd=self.worktree, start_new_session=True,
                                 env={**os.environ, "CLAUDE_CODE_SESSION_ID": session_id})
        def cleanup() -> None:
            if child.stdin is not None:
                child.stdin.close()
            _ = child.wait(timeout=TEST_HANG_GUARD_SECS)
        self.addCleanup(cleanup)
        return child

    def test_dead_sessions_orphan_group_is_signalled_after_identity_checks(self) -> None:
        child = self.orphan_child(DIRECTOR)
        self.claude = []
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        settings = conf.read("production")
        for platform, ps_status in (("linux", 0), ("darwin", 0), ("darwin", 1)):
            with self.subTest(platform=platform, ps_status=ps_status), ExitStack() as reads:
                if platform == "darwin":
                    _ = reads.enter_context(mock.patch.object(sys, "platform", platform))
                    _ = reads.enter_context(mock.patch.object(Path, "read_bytes", side_effect=FileNotFoundError(2, "No such file or directory", "/proc/TEST/environ")))
                    _ = reads.enter_context(mock.patch.object(Path, "read_text", side_effect=FileNotFoundError(2, "No such file or directory", "/proc/TEST/stat")))
                    _ = reads.enter_context(mock.patch.object(pane_record, "process_parent", return_value=123))
                    _ = reads.enter_context(mock.patch.object(herdr, "darwin_process_environment", return_value={"CLAUDE_CODE_SESSION_ID": DIRECTOR}))
                    _ = reads.enter_context(mock.patch.object(shutdown, "retirement_proc_start", return_value="TEST-start"))
                    _ = reads.enter_context(mock.patch.object(shutdown, "retirement_run", return_value=subprocess.CompletedProcess(
                        ["ps"], ps_status, f"{child.pid}\n", "TEST-incomplete process list" if ps_status else "")))
                    _ = reads.enter_context(mock.patch.object(unit_move, "processes_inside", return_value=[child.pid]))
                    _ = reads.enter_context(mock.patch.object(process_start, "process_start", return_value=process_start.Started("TEST-start")))
                with (mock.patch.object(process_group, "send_process_group") as send,
                      mock.patch.object(shutdown, "retirement_alive", return_value=False)):
                    left = shutdown.retirement_stop_orphan(unit, child.pid, DIRECTOR, settings=settings)
                if ps_status:
                    send.assert_not_called()
                    self.assertEqual(len(left), 1, left)
                    self.assertIn(f"unowned process {child.pid}", left[0])
                    self.assertIn("ps -axo pid=: TEST-incomplete process list", left[0])
                else:
                    self.assertEqual(left, [])
                    send.assert_called_once_with(child.pid, signal.SIGTERM)
                self.assertIsNone(child.poll(), "the test patches the signal; the child still runs")

    def test_unowned_or_live_sessions_process_is_named_and_never_signalled(self) -> None:
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        settings = conf.read("production")
        for platform in ("linux", "darwin"):
            for session_id, live_owner in ((SEAT, False), (DIRECTOR, True)):
                with self.subTest(platform=platform, session_id=session_id, live_owner=live_owner), ExitStack() as reads:
                    child = self.orphan_child(session_id)
                    self.claude = [session(DIRECTOR, "alpha", 4101)] if live_owner else []
                    if platform == "darwin":
                        _ = reads.enter_context(mock.patch.object(sys, "platform", platform))
                        _ = reads.enter_context(mock.patch.object(Path, "read_bytes", side_effect=FileNotFoundError(2, "No such file or directory", "/proc/TEST/environ")))
                        _ = reads.enter_context(mock.patch.object(Path, "read_text", side_effect=FileNotFoundError(2, "No such file or directory", "/proc/TEST/stat")))
                        _ = reads.enter_context(mock.patch.object(pane_record, "process_parent", return_value=123))
                        _ = reads.enter_context(mock.patch.object(herdr, "darwin_process_environment", return_value={"CLAUDE_CODE_SESSION_ID": session_id}))
                        _ = reads.enter_context(mock.patch.object(shutdown, "retirement_proc_start", return_value="TEST-start"))
                        _ = reads.enter_context(mock.patch.object(shutdown, "retirement_run", return_value=subprocess.CompletedProcess(
                            ["ps"], 0, "TEST-shell\n", "")))
                    with mock.patch.object(process_group, "send_process_group") as send:
                        left = shutdown.retirement_stop_orphan(unit, child.pid, DIRECTOR, settings=settings)
                    send.assert_not_called()
                    self.assertEqual(len(left), 1, left)
                    for detail in (f"unowned process {child.pid}, parent ", "command ", "start ", str(self.worktree)):
                        self.assertIn(detail, left[0])
                    if platform == "darwin":
                        self.assertNotIn("Errno 2", left[0])
                        self.assertIn("parent 123, command TEST-shell, start TEST-start", left[0])
                        self.assertEqual(shutdown.retirement_process_session(child.pid), session_id)
                        self.assertEqual(shutdown.retirement_command_name(child.pid), "TEST-shell")

        rows = [("mac", True, "w1:p1", True, False), ("mac", False, "w1:p1", True, False),
                ("mac", True, "w1:p3", True, False), (remote.local_machine(), False, "w1:p1", True, False)]
        rows.extend((machine, at_shell, "w1:p1", inside_worktree, removal_fails)
                    for machine in (remote.local_machine(), "mac")
                    for at_shell, inside_worktree, removal_fails in ((True, False, False),
                                                                    (False, False, False), (True, True, True)))
        for machine, at_shell, pane, inside_worktree, removal_fails in rows:
            with self.subTest(machine=machine, at_shell=at_shell, pane=pane,
                              inside_worktree=inside_worktree, removal_fails=removal_fails):
                case = RetireUnitTests()
                case.setUp()
                try:
                    if machine == remote.local_machine():
                        row = staff.by_id(case.unit_staff_id)
                        assert row is not None
                    else:
                        row = case.move_to_other_machine()
                    case.pane_world.add(row, "w1:p1", live=False)
                    if pane != "w1:p1":
                        case.pane_world.add(row, pane, live=False, label="TEST-visitor")
                    shell = pane_record.pane_shell_pid(HerdrPlace("agents", pane))
                    succeeds = not removal_fails and (not inside_worktree or at_shell and pane == "w1:p1")
                    closes = succeeds and at_shell and pane == "w1:p1"
                    if not at_shell:
                        case.pane_world.activate("TEST-visitor", "w1:p1", "claude")
                    def inside(worktree: Path) -> list[int]:
                        case.assertEqual(worktree, case.worktree)
                        return [shell] if inside_worktree and pane in case.pane_world.locations else []
                    close = case.pane_world.close_retained_pane
                    def close_last(identity: str, *, terminal: str = "") -> bool:
                        case.assertEqual(remote.local_machine(), machine)
                        case.assertTrue(case.worktree.is_dir())
                        case.assertIsInstance(case.unit_worktree(), staff.RemovedWorktree)
                        return close(identity, terminal=terminal)
                    remove = shutdown.retirement_remove
                    def remove_worktree(unit: shutdown.RetiringUnit, *, settings: conf.ConfigRead) -> list[str]:
                        return ["TEST-removal failed"] if removal_fails else remove(unit, settings=settings)
                    with (mock.patch.object(unit_move, "processes_inside", side_effect=inside),
                          mock.patch.object(shutdown, "retirement_process_session", return_value=""),
                          mock.patch.object(shutdown, "retirement_proc_start", return_value="TEST-start"),
                          mock.patch.object(shutdown, "retirement_remove", side_effect=remove_worktree),
                          mock.patch.object(case.pane_world, "close_retained_pane", side_effect=close_last),
                          mock.patch.object(process_group, "send_process_group") as send):
                        code, output = case.retire()
                        if succeeds:
                            rerun_code, rerun_output = case.retire()
                            case.assertEqual(rerun_code, 0, rerun_output)
                            case.assertNotIn("pane close failed", rerun_output)
                            if closes:
                                case.assertNotIn("anomaly:", rerun_output)
                    send.assert_not_called()
                    case.assertEqual(case.pane_world.signals, [])
                    case.assertEqual(code, 0 if succeeds else 1, output)
                    case.assertEqual(case.pane_world.closed, [("w1:p1", "terminal-w1:p1")] if closes else [])
                    if succeeds:
                        if closes:
                            case.assertNotIn("anomaly:", output)
                        else:
                            case.assertEqual(output.count("anomaly:"), 1, output)
                            case.assertIn("alpha:w1:p1: foreground work keeps the pane open", output)
                            case.assertIn("w1:p1", case.pane_world.locations)
                        case.assertTrue(case.worktree.is_dir())
                        case.assertIsInstance(case.unit_worktree(), staff.RemovedWorktree)
                    else:
                        if removal_fails:
                            case.assertIn("TEST-removal failed", output)
                        else:
                            case.assertIn(f"unowned process {shell}", output)
                            case.assertIn("refused: live holds or processes remain", output)
                        case.assertTrue(case.worktree.is_dir())
                        case.assertEqual(staff.by_id(row.id), row)
                finally:
                    case.doCleanups()

        for machine, journal_retry in ((remote.local_machine(), False), ("mac", False), ("mac", True)):
            with self.subTest(machine=machine, journal_retry=journal_retry):
                case = RetireUnitTests()
                case.setUp()
                try:
                    if machine == remote.local_machine():
                        row = staff.by_id(case.unit_staff_id)
                        assert row is not None
                    else:
                        row = case.move_to_other_machine()
                    case.pane_world.add(row, "w1:p1", live=False)
                    assert isinstance(row.parent, staff.ReportsTo)
                    other = fixture_member("unit", name="TEST-other", parent=row.parent.id,
                                           runtime_id=fixture_runtime_id("TEST-other"))
                    observed = pane_record.launch("w1:p1", other.current_session, str(case.root),
                                                 {"CLAUDE_CONFIG_DIR": str(case.config)}, ["claude", "-n", other.name])
                    replacement = pane_launch.retained_launch(observed, staff.snapshot())
                    remove = shutdown.retirement_remove
                    def replace_after_cleanup(unit: shutdown.RetiringUnit, *, settings: conf.ConfigRead) -> list[str]:
                        result = remove(unit, settings=settings)
                        case.assertEqual(result, [])
                        case.pane_world.locations["w1:p1"]["terminal_id"] = "TEST-new-terminal"
                        case.pane_world.locations["w1:p1"]["label"] = other.name
                        pane_record.write("alpha", observed)
                        return result
                    with (mock.patch.object(shutdown, "retirement_remove", side_effect=replace_after_cleanup),
                          mock.patch.object(process_group, "send_process_group") as send):
                        if journal_retry:
                            unit = shutdown.retirement_find_unit("alpha", str(row.id))
                            result = shutdown.unit_remote_command("retire", row, staff.snapshot(),
                                                                  merge_branch=unit.production.merge_branch,
                                                                  zone=unit.production.zone.key)
                            case.assertEqual(result["code"], 0, result["output"])
                            _ = staff.set_worktree(row.id, staff.RemovedWorktree(machine, case.worktree), expected=row.worktree)
                            journal = case.root / "remote-shutdown/retire-worktrees" / f"{row.id}.json"
                            case.assertTrue(journal.exists())
                        code, output = case.retire()
                    send.assert_not_called()
                    case.assertEqual(code, 0, output)
                    case.assertEqual(case.pane_world.closed, [])
                    case.assertEqual(pane_record.read("alpha").get("w1:p1"), replacement)
                    case.assertEqual(case.pane_world.locations["w1:p1"]["terminal_id"], "TEST-new-terminal")
                    case.assertEqual(output.count("anomaly:"), 1, output)
                    case.assertIn("alpha:w1:p1: pane close failed", output)
                    case.assertIn("terminal identity changed", output)
                    case.assertTrue(case.worktree.is_dir())
                    case.assertIsInstance(case.unit_worktree(), staff.RemovedWorktree)
                finally:
                    case.doCleanups()

    def test_orphan_start_time_change_before_signal_refuses_without_sending(self) -> None:
        child = self.orphan_child(DIRECTOR)
        self.claude = []
        unit = shutdown.retirement_find_unit("alpha", "alpha")
        birth = shutdown.retirement_proc_start(child.pid)
        with (mock.patch.object(shutdown, "retirement_proc_start", side_effect=[birth, birth, birth, "reused"]),
              mock.patch.object(process_group, "send_process_group") as send):
            left = shutdown.retirement_stop_orphan(unit, child.pid, DIRECTOR, settings=conf.read("production"))
        send.assert_not_called()
        self.assertTrue(any("identity changed before signal" in line for line in left), left)


    def test_refuses_uncommitted_work_and_an_unmerged_checkpoint_and_changes_nothing(self) -> None:
        for machine in (remote.local_machine(), "mac"):
            with self.subTest(machine=machine):
                case = self if machine == remote.local_machine() else RetireUnitTests()
                if case is not self:
                    case.setUp()
                    self.addCleanup(case.doCleanups)
                    _ = case.move_to_other_machine()
                _ = (case.worktree / "late.txt").write_text("late\n", encoding="utf-8")
                _ = git(case.worktree, "add", "late.txt")
                _ = git(case.worktree, "commit", "-q", "-m", "checkpoint(alpha): the late work")
                _ = (case.worktree / "draft.txt").write_text("draft\n", encoding="utf-8")
                before = case.doc.read_text(encoding="utf-8")

                code, output = case.retire()

                case.assertEqual(code, 1)
                case.assertIn(f"refused: uncommitted work in {case.worktree}: draft.txt", output)
                case.assertEqual(case.calls, [])
                case.assertTrue(case.worktree.is_dir())
                case.assertEqual(case.doc.read_text(encoding="utf-8"), before)

                case.assertIn("unmerged commits on alpha-branch", output)
                case.assertIsInstance(case.unit_worktree(), staff.PresentWorktree)

        for unmerged, tail_match, fetch_tags in ((False, False, False), (True, False, False),
                                                (True, True, False), (False, False, True)):
            with self.subTest(origin_merge_branch=True, unmerged=unmerged, tail_match=tail_match, fetch_tags=fetch_tags):
                case = RetireUnitTests()
                case.setUp()
                try:
                    if not tail_match:
                        _ = git(case.checkout, "push", "-q", "origin", "main")
                    _ = git(case.checkout, "branch", "-m", "main", "local-checkout")
                    _ = git(case.checkout, "update-ref", "-d", "refs/remotes/origin/main")
                    if unmerged:
                        write_text_atomic(case.worktree / "late.txt", "late\n")
                        _ = git(case.worktree, "add", "late.txt")
                        _ = git(case.worktree, "commit", "-q", "-m", "checkpoint(alpha): the late work")
                    commit = git(case.worktree, "rev-parse", "HEAD")
                    if tail_match:
                        _ = git(case.checkout, "push", "-q", "origin", f"{commit}:refs/heads/a/main")
                    if fetch_tags:
                        _ = git(case.checkout, "config", "remote.origin.tagOpt", "--tags")
                        _ = git(case.checkout, "push", "-q", "origin", f"{commit}:refs/tags/TEST-origin-tag")
                        case.assertEqual(git(case.checkout, "for-each-ref", "refs/tags/TEST-origin-tag"), "")
                    unit = shutdown.retirement_find_unit("alpha", "alpha")
                    refusals = shutdown.retirement_refusals(unit, settings=conf.read("production"))
                    case.assertEqual(refusals, [f"unmerged commits on alpha-branch: {commit}"] if unmerged else [])
                    case.assertEqual(git(case.checkout, "branch", "--list", "main"), "")
                    case.assertEqual(git(case.checkout, "for-each-ref", "refs/remotes/origin/main"), "")
                    if fetch_tags:
                        case.assertEqual(git(case.checkout, "for-each-ref", "refs/tags/TEST-origin-tag"), "")
                    case.assertEqual(case.calls, [])
                    case.assertTrue(case.worktree.is_dir())
                finally:
                    case.doCleanups()

        with self.subTest(branch_with_line_separator=True):
            case = RetireUnitTests()
            case.setUp()
            try:
                branch = "work\u2028in-progress"
                _ = git(case.worktree, "branch", "-m", branch)
                _ = git(case.worktree, "commit", "--allow-empty", "-qm", "Unmerged checkpoint")
                tip = git(case.worktree, "rev-parse", "HEAD")
                status, output = case.retire()
                self.assertEqual(status, 1, output)
                self.assertIn(f"unmerged commits on {branch}: {tip}", output)
                self.assertEqual(git(case.checkout, "rev-parse", f"refs/heads/{branch}"), tip)
                self.assertTrue(case.worktree.is_dir())
            finally:
                case.doCleanups()

        for moving_branch in ("alpha-branch", "main"):
            with self.subTest(moving_branch=moving_branch):
                case = RetireUnitTests()
                case.setUp()
                try:
                    ref = f"refs/heads/{moving_branch}"
                    unit_ref = "refs/heads/alpha-branch"
                    unit_tip = git(case.checkout, "rev-parse", unit_ref)
                    tip = git(case.checkout, "rev-parse", ref)
                    if moving_branch == "main":
                        moved = git(case.checkout, "rev-parse", "refs/remotes/origin/main")
                        unmerged = unit_tip
                    else:
                        moved = git(case.checkout, "commit-tree", f"{tip}^{{tree}}", "-p", tip, "-m", "Late checkpoint")
                        unmerged = moved
                    unit_queries = 0

                    def move_during_read(argv: list[str], cwd: Path | None = None, *,
                                         settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT
                    ) -> subprocess.CompletedProcess[str]:
                        nonlocal unit_queries
                        if argv[3:4] == ["ls-remote"] and unit_ref in argv[4:]:
                            unit_queries += 1
                            if unit_queries == 2:
                                _ = git(case.checkout, "update-ref", ref, moved, tip)
                        return case.fake_run(argv, cwd=cwd, settings=settings)

                    with mock.patch.object(shutdown, "retirement_run", side_effect=move_during_read):
                        status, output = case.retire()
                    self.assertEqual(status, 1, output)
                    self.assertIn(f"unmerged commits on alpha-branch: {unmerged}", output)
                    self.assertEqual(git(case.checkout, "rev-parse", ref), moved)
                    self.assertIn(unit_tip, git(case.checkout, "ls-remote", "origin", unit_ref))
                    self.assertTrue(case.worktree.is_dir())
                    status, output = case.retire()
                    self.assertEqual(status, 1, output)
                    self.assertIn(f"unmerged commits on alpha-branch: {unmerged}", output)
                finally:
                    case.doCleanups()

        for disappearing in ("alpha-branch", "main"):
            with self.subTest(disappearing=disappearing):
                case = RetireUnitTests()
                case.setUp()
                try:
                    origin = case.root / "origin.git"
                    branch = f"refs/heads/{disappearing}"
                    tip = git(case.checkout, "rev-parse", "refs/heads/main")
                    _ = git(case.checkout, "push", "-q", "origin",
                            "main", f"{tip}:{branch}" if disappearing != "main" else "alpha-branch",
                            f"{tip}:refs/heads/retained")
                    removed = False

                    def remove_after_advertisement(argv: list[str], cwd: Path | None = None, *,
                                                   settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT
                    ) -> subprocess.CompletedProcess[str]:
                        nonlocal removed
                        result = case.fake_run(argv, cwd=cwd, settings=settings)
                        if argv[3:4] == ["ls-remote"] and branch in argv[4:] and not removed:
                            _ = git(origin, "update-ref", "-d", branch, tip)
                            removed = True
                        return result

                    with mock.patch.object(shutdown, "retirement_run", side_effect=remove_after_advertisement):
                        status, output = case.retire()
                    self.assertEqual(status, 1, output)
                    self.assertIn(f"couldn't find remote ref {branch}", output)
                    self.assertEqual(git(origin, "rev-parse", "refs/heads/retained"), tip)
                    self.assertTrue(case.worktree.is_dir())
                finally:
                    case.doCleanups()

        with self.subTest(other_worktree_rebases_branch=True):
            case = RetireUnitTests()
            case.setUp()
            try:
                branch = "refs/heads/alpha-branch"
                tip = git(case.checkout, "rev-parse", branch)
                other = case.root / "rebasing-worktree"
                _ = git(case.checkout, "worktree", "add", "--detach", str(other), tip)
                state = Path(git(other, "rev-parse", "--absolute-git-dir")) / "rebase-merge"
                state.mkdir()
                write_text_atomic(state / "head-name", branch + "\n")
                write_text_atomic(state / "orig-head", tip + "\n")
                write_text_atomic(state / "onto", git(case.checkout, "rev-parse", "main") + "\n")
                status, output = case.retire()
                self.assertEqual(status, 1, output)
                self.assertIn(str(other), output)
                self.assertEqual(git(case.checkout, "rev-parse", branch), tip)
                self.assertTrue(state.is_dir())
            finally:
                case.doCleanups()

        with self.subTest(suffix_only_advertisement=True):
            case = RetireUnitTests()
            case.setUp()
            try:
                origin = case.root / "origin.git"
                branch = "refs/heads/alpha-branch"
                suffix = f"refs/heads/archive/{branch}"
                tip = git(origin, "rev-parse", branch)
                _ = git(origin, "update-ref", suffix, tip)
                _ = git(origin, "update-ref", "-d", branch, tip)
                status, output = case.retire()
                self.assertEqual(status, 1, output)
                self.assertIn(f"couldn't find remote ref {branch}", output)
                self.assertEqual(git(origin, "rev-parse", suffix), tip)
                self.assertTrue(case.worktree.is_dir())
            finally:
                case.doCleanups()

        with self.subTest(fixture_ignores_inherited_git_directory=True):
            directory = git(self.worktree, "rev-parse", "--absolute-git-dir")
            with mock.patch.dict(os.environ, {"GIT_DIR": str(self.root / "origin.git")}):
                self.assertEqual(git(self.worktree, "rev-parse", "--absolute-git-dir"), directory)

    def test_the_recorders_project_started_line_is_not_uncommitted_work(self) -> None:
        plan = self.worktree / "work.txt"
        recorded = plan.read_text(encoding="utf-8") + "- **Project started:** 2026-10-18T22:00:00-04:00\n"
        _ = plan.write_text(recorded, encoding="utf-8")

        unit = shutdown.retirement_find_unit("alpha", "alpha")
        self.assertEqual(shutdown.retirement_refusals(unit, settings=conf.read("production")), [])
        # A line a person wrote beside it is still uncommitted work.
        _ = plan.write_text(recorded + "a note\n", encoding="utf-8")
        code, output = self.retire()
        self.assertEqual(code, 1)
        self.assertIn(f"refused: uncommitted work in {self.worktree}: work.txt", output)


    def test_retires_a_unit_with_a_seat_a_worker_and_a_reviewer(self) -> None:
        for machine in (remote.local_machine(), "mac", "NaTeS-MacBook-Pro.LOCAL"):
            with self.subTest(machine=machine):
                case = self if machine == remote.local_machine() else RetireUnitTests()
                if case is not self:
                    case.setUp()
                    self.addCleanup(case.doCleanups)
                    _ = case.move_to_other_machine(machine)
                code, output = case.retire()

                case.assertEqual(code, 0, output)
                case.assertEqual(case.stops(), [
                    ["stop_resume.stop", DIRECTOR],
                    ["stop_resume.stop", SEAT],
                    ["build_hold.py", "release", "--holder", DIRECTOR],
                ])
                case.assertIn("alpha retired", output)
                case.assertEqual(case.unit_worktree(), staff.RemovedWorktree(machine, case.worktree, staff.ParkedBranch("alpha-branch")))
                row = staff.by_id(case.unit_staff_id)
                assert row is not None
                case.assertEqual(row.current_session, DIRECTOR)
                case.assertEqual(git(case.checkout, "status", "--porcelain", "--untracked-files=no"), "")
                case.assertTrue(case.worktree.is_dir())
                case.assertIn("alpha-branch", git(case.checkout, "branch", "--list", "alpha-branch"))
                case.assertIn("refs/heads/alpha-branch", git(case.checkout, "ls-remote", "--heads", "origin", "alpha-branch"))


    def test_remote_retirement_retries_failed_steps_without_retiring_the_staff_row(self) -> None:
        for failure in ("stop", "remove", "branch", "origin", "ssh", "answer", "hold", "copy", "journal", "journal removal"):
            with self.subTest(failure=failure):
                case = RetireUnitTests()
                case.setUp()
                try:
                    row = case.move_to_other_machine()
                    remove = shutdown.retirement_remove
                    def blocked_remove(unit: shutdown.RetiringUnit, *, settings: conf.ConfigRead) -> list[str]:
                        case.assertEqual(remote.local_machine(), "mac")
                        if failure == "remove":
                            return ["fixture removal failed"]
                        if failure == "origin":
                            _ = git(case.checkout, "worktree", "remove", "--force", str(case.worktree))
                            _ = git(case.checkout, "branch", "-D", "alpha-branch")
                            return ["fixture branch deletion failed"]
                        result = remove(unit, settings=settings)
                        return ["fixture completion failed"] if failure == "branch" else result
                    def stop(identity: str, *, deadline: float = math.inf) -> StopResult:
                        case.assertEqual(remote.local_machine(), "mac")
                        if failure == "stop":
                            return stop_resume.StopFailed(identity, "fixture stop failed")
                        return case.stop_session(identity, deadline=deadline)
                    with (mock.patch.object(shutdown, "retirement_remove", side_effect=blocked_remove),
                          mock.patch.object(stop_resume, "stop", side_effect=stop),
                          mock.patch.object(shutdown, "run_remote", side_effect=case.remote_call) as ssh,
                          mock.patch.object(copies, "remove_trees", wraps=copies.remove_trees) as trees,
                          mock.patch.object(shutdown, "retirement_holds", wraps=shutdown.retirement_holds) as holds):
                        if failure == "copy":
                            trees.side_effect = copies.CopyError("TEST copy removal failed")
                        if failure in ("ssh", "answer"):
                            ssh.side_effect = None
                            ssh.return_value = (255, "") if failure == "ssh" else (0, "unreadable answer")
                        if failure == "hold":
                            holds.side_effect = None
                            holds.return_value = (DIRECTOR,)
                        if failure == "journal":
                            _ = git(case.checkout, "worktree", "remove", "--force", str(case.worktree))
                            code, output = case.retire()
                            case.assertEqual(code, 0, output)
                            case.assertEqual(output.count("anomaly:"), 1, output)
                            case.assertIn("branch is unknown", output)
                            case.assertEqual(case.unit_worktree(), staff.RemovedWorktree("mac", case.worktree))
                            case.assertEqual(case.stops()[:2], [["stop_resume.stop", DIRECTOR], ["stop_resume.stop", SEAT]])
                            case.assertNotEqual(git(case.checkout, "ls-remote", "--heads", "origin", "alpha-branch"), "")
                            continue
                        if failure == "journal removal":
                            unlink = Path.unlink
                            journal = case.root / "remote-shutdown/retire-worktrees" / f"{row.id}.json"
                            def refuse_journal(path: Path, missing_ok: bool = False) -> None:
                                if path == journal:
                                    case.assertIsInstance(case.unit_worktree(), staff.RemovedWorktree)
                                    raise PermissionError("fixture journal removal failed")
                                unlink(path, missing_ok=missing_ok)
                            with mock.patch.object(Path, "unlink", refuse_journal):
                                code, output = case.retire()
                            case.assertEqual(code, 0, output)
                            case.assertEqual(output.count("anomaly:"), 1, output)
                            case.assertIn("fixture journal removal failed", output)
                            case.assertTrue(journal.exists())
                            before = case.stops()
                            case.assertEqual(case.retire(), (0, "unchanged\n"))
                            case.assertEqual(case.stops(), before)
                            case.assertFalse(journal.exists())
                            continue
                        code, output = case.retire()
                    case.assertNotEqual(code, 0, output)
                    if failure == "copy":
                        case.assertIn("anomaly: TEST copy removal failed", output)
                    case.assertNotIn("alpha retired", output)
                    case.assertEqual(staff.by_id(row.id), row)
                    case.assertIsInstance(unit_config.read_parked(row.id, cleanup=False), unit_config.ConfigMissing)
                    case.assertEqual(case.worktree.exists(), failure != "origin")
                    journal = case.root / "remote-shutdown/retire-worktrees" / f"{row.id}.json"
                    if failure == "origin":
                        commit = git(case.checkout, "commit-tree", "main^{tree}", "-p", "main", "-m", "unmerged remote work")
                        _ = git(case.checkout, "push", "-q", "origin", f"{commit}:refs/heads/alpha-branch")
                        code, output = case.retire()
                        case.assertEqual(code, 1, output)
                        case.assertIn("refused: unmerged commits on alpha-branch", output)
                        case.assertEqual(staff.by_id(row.id), row)
                        case.assertTrue(journal.exists())
                        case.assertIn(commit, git(case.checkout, "ls-remote", "--heads", "origin", "alpha-branch"))
                        _ = git(case.checkout, "merge", "-q", "--ff-only", commit)
                    code, output = case.retire()
                    case.assertEqual(code, 0, output)
                    case.assertEqual(case.worktree.is_dir(), failure != "origin")
                    case.assertEqual(case.unit_worktree(), staff.RemovedWorktree("mac", case.worktree, staff.ParkedBranch("alpha-branch")))
                    case.assertFalse(journal.exists())
                    before = case.stops()
                    case.assertEqual(case.retire(), (0, "unchanged\n"))
                    case.assertEqual(case.stops(), before)
                finally:
                    case.doCleanups()

    def outside_director(self) -> Path:
        self.claude = []
        old = self.config / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        old.unlink()
        folder = self.root / "original-checkout"
        self.transcript(folder)
        return folder

    def test_retirement_keeps_the_transcript_in_its_original_folder(self) -> None:
        folder = self.outside_director()
        transcript = self.config / "projects" / unit_move.project_folder(str(folder)) / f"{DIRECTOR}.jsonl"
        old = self.root / "old-checkout"
        _ = transcript.write_text("\n".join(json.dumps({"cwd": str(cwd)})
                                            for cwd in (old, old, folder, folder)) + "\n")
        before = transcript.read_bytes(), transcript.stat().st_mtime_ns
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        row = staff.by_id(self.unit_staff_id)
        assert row is not None
        self.assertEqual(row.current_session, DIRECTOR)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual((transcript.read_bytes(), transcript.stat().st_mtime_ns), before)


    def test_retirement_keeps_a_worktree_with_only_the_project_started_line(self) -> None:
        plan = self.worktree / "work.txt"
        _ = plan.write_text(plan.read_text() + "- **Project started:** 2026-10-18T22:00:00-04:00\n")
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(self.unit_worktree(), staff.RemovedWorktree(remote.local_machine(), self.worktree, staff.ParkedBranch("alpha-branch")))

    def test_retirement_retains_the_worktree_lock_and_lease(self) -> None:
        for held in (False, True):
            with self.subTest(held=held):
                case = RetireUnitTests()
                case.setUp()
                try:
                    lease = case.worktree.with_name(case.worktree.name + ".lease.json")
                    lock = case.worktree.with_name(case.worktree.name + ".lock")
                    _ = lease.write_text("{}\n")
                    lock.touch()
                    with lock.open("r+") as descriptor:
                        if held:
                            fcntl.flock(descriptor, fcntl.LOCK_EX)
                        code, output = case.retire()

                    self.assertEqual(code, 0, output)
                    self.assertTrue(case.worktree.is_dir())
                    self.assertIsInstance(case.unit_worktree(), staff.RemovedWorktree)
                    self.assertEqual(lease.read_text(), "{}\n")
                    self.assertTrue(lock.is_file())
                    self.assertIn(f"kept for a manual removal list: {case.worktree}", output)
                finally:
                    case.doCleanups()

    def test_retirement_removes_the_gate_copy_owned_by_the_unit_worktree(self) -> None:
        owner = copies.source_owner(self.worktree)
        with copies.standing(owner, self.worktree, copies.CommittedSource("HEAD")) as checkout:
            self.assertTrue(checkout.exists())
        code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertTrue(checkout.exists())
        self.assertEqual(build_folders.build_folder_state(checkout), "free")

    def test_retirement_removes_only_its_remote_mirrors(self) -> None:
        mine = copies.RemoteMirror(str(self.unit_staff_id), copies.SelectedRepository("hana"), "mac", "mirror/alpha", "alpha-session")
        other = copies.RemoteMirror("beta-unit", copies.SelectedRepository("hana"), "mac", "mirror/beta", "beta-session")
        with fake_remote_removal((mine, other)) as remove:
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        remove.assert_called_once_with(mine)
        self.assertEqual(copies.owned_remote_mirrors("beta-unit"), [other])

    def test_retirement_refuses_a_busy_remote_mirror_before_worktree_removal(self) -> None:
        mirror = copies.RemoteMirror(str(self.unit_staff_id), copies.SelectedRepository("hana"), "mac", "mirror/alpha", "alpha-session")
        copies.register_remote(mirror)
        with copies.remote_lease(mirror):
            code, output = self.retire()
        self.assertEqual(code, 1, output)
        self.assertIn("alpha-session", output)
        self.assertIn("mirror/alpha", output)
        self.assertTrue(self.worktree.exists())
        self.assertTrue(copies.remote_record_path(mirror).exists())

    def test_retirement_removes_an_idle_remote_mirror_through_the_remote_command(self) -> None:
        mirror = copies.RemoteMirror(str(self.unit_staff_id), copies.SelectedRepository("hana"), "mac",
                                     str(self.root / "mirrors/alpha"), "alpha-session")
        with local_remote_transport(self.root):
            self.assertEqual(publish_mirror(mirror), 0)
            code, output = self.retire()
        self.assertEqual(code, 0, output)
        self.assertFalse(Path(mirror.path).exists())
        self.assertFalse(copies.remote_record_path(mirror).exists())

@final
class ResumeUnitTests(ShutdownRepositoryFixture):
    def test_retired_resume_passes_the_selected_account_to_the_launcher(self) -> None:
        code, output = self.resume("--to", "claude3")
        self.assertEqual(code, 0, output)
        command = self.calls[-1]
        self.assertIn("--account", command)
        self.assertEqual(command[command.index("--account") + 1], "claude3")

    def test_resume_of_a_retired_unit_requires_the_retired_option_before_dispatch(self) -> None:
        row = unit_membership("alpha", "alpha")
        before = self.staff.staff_file.read_bytes(), self.parked_facts().read_bytes()
        for name in (row.name, str(row.id), row.current_session):
            with self.subTest(name=name), mock.patch.object(stop_resume, "resume") as start:
                output = io.StringIO()
                with redirect_stderr(output):
                    code = shutdown.main(["resume", name])
                self.assertEqual(code, 2)
                self.assertIn(f"agents resume {row.name} --retired", output.getvalue())
                start.assert_not_called()
        self.assertEqual((self.staff.staff_file.read_bytes(), self.parked_facts().read_bytes()), before)
        self.assertEqual(self.calls, [])

    def test_retired_resume_refuses_the_old_name_before_restoring_the_unit(self) -> None:
        row = unit_membership("alpha", "alpha")
        legacy = fixture_unit_saved_name(row, "alpha-unit")
        parked = unit_config.parked_path(row.id)
        before = self.staff.staff_state(), parked.read_bytes()
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as error:
            code = shutdown.main(["resume", legacy.name, "--retired"])
        self.assertEqual((code, output.getvalue(), error.getvalue()),
                         (2, "", "StaffRefused: unit name 'alpha-unit' must not end in -unit\n"))
        self.assertEqual((self.staff.staff_state(), parked.read_bytes()), before)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.calls, [])

    def test_retired_resume_accepts_an_underscore_in_the_unit_name(self) -> None:
        row = unit_membership("alpha", "alpha")
        renamed = staff.rename(row.id, "alpha_unit")
        record = staff.initial_snapshot()
        unit = shutdown.recovery_find_unit("alpha", renamed.name, record=record)
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        unit = replace(unit, branch="alpha-alpha-unit")
        with redirect_stdout(io.StringIO()) as output:
            code = shutdown.recovery_resume(unit, record)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("alpha_unit resumed", output.getvalue())
        self.assertEqual(git(self.worktree, "branch", "--show-current"), unit.branch)

    def test_resume_restores_retained_workers(self) -> None:
        from scripts.production import worker

        with mock.patch.object(worker, "restore", return_value=worker.ExitCode.OK) as restore:
            code, output = self.resume()
        self.assertEqual(code, 0, output)
        restore.assert_called_once()

    def test_uncertain_worker_restore_keeps_the_director_resume_complete(self) -> None:
        row = unit_membership("alpha", "alpha")
        with mock.patch.object(worker, "restore", side_effect=[
                worker.ExitCode.UNCERTAIN, worker.ExitCode.OK]) as restore:
            code, output = self.resume()
            self.assertEqual(code, 8, output)
            self.assertIn("alpha resumed", output)
            self.assertFalse(unit_config.parked_path(row.id).exists())
            unit = shutdown.recovery_find_unit("alpha", "alpha")
            self.assertIsInstance(unit, shutdown.NoRecoveryRequired)
            receipt = completion_receipt.read_receipt(
                shutdown.recovery_resume_record(unit.production, unit.name).with_suffix(".done"), staff.snapshot())
            self.assertIsInstance(receipt, completion_receipt.OwnedReceipt)
            assert isinstance(receipt, completion_receipt.OwnedReceipt)
            self.assertEqual(receipt.owner.id, row.id)
            self.assertEqual(receipt.fields["session_id"], row.current_session)
            code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertEqual(output, "alpha already resumed\n")
        self.assertEqual(restore.call_args_list, [mock.call(row.id), mock.call(row.id)])
        self.assertEqual(len(self.calls), 1)


    def test_resume_restores_the_staff_location(self) -> None:
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertTrue((self.worktree / ".git").is_file())
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        row = unit_membership("alpha", "alpha")
        self.assertEqual(row.worktree, staff.PresentWorktree(remote.local_machine(), self.worktree))
        self.assertIsInstance(shutdown.recovery_find_unit("alpha", "alpha"), shutdown.NoRecoveryRequired)


    staff: StaffTestCase = StaffTestCase()
    root: Path = Path()
    checkout: Path = Path()
    worktree: Path = Path()
    doc: Path = Path()
    configs: list[Path] = []
    calls: list[list[str]] = []
    launch_code: int = 0

    def test_resume_command_uses_the_row_name_and_current_native_session(self) -> None:
        row = unit_membership("alpha", "alpha")
        self.assertNotEqual(row.id, row.current_session)
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        self.assertEqual(unit.branch, row.name)
        command = shutdown.recovery_launch_command(replace(unit, branch="parked-branch"))
        self.assertEqual(command[command.index("--production") + 2], row.name)
        self.assertEqual(command[command.index("--resume") + 1], row.current_session)
        self.assertNotIn("STAFF_ID", " ".join(command))

    def test_recovery_refuses_an_empty_session_id(self) -> None:
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        with self.assertRaisesRegex(shutdown.RecoveryRefused, "^alpha: recovery requires a session id$"):
            _ = replace(unit, session_id=" ")

    def test_recovery_requires_each_retirement_fact(self) -> None:
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        for fact in ("branch", "session_id", "plan"):
            fields = dict(vars(unit))
            del fields[fact]
            with self.subTest(fact=fact), self.assertRaises(TypeError):
                _ = cast(Callable[..., object], shutdown.RetiredUnitRecovery)(**fields)

    def test_neither_config_refuses_with_the_backfill_instruction_before_any_change(self) -> None:
        row = unit_membership("alpha", "alpha")
        row = staff.update_current_session(row.current_session, staff.StaffId(DIRECTOR))
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree), expected=row.worktree)
        unit_config.discard_parked(row.id)
        before = self.doc.read_bytes(), self.staff.staff_file.read_bytes(), git(self.checkout, "rev-parse", "HEAD")
        for flags in ((),):
            with self.subTest(flags=flags):
                code, output = self.resume(*flags)
                self.assertEqual(code, 1, output)
                self.assertEqual(output, f"alpha: no .unit.toml in {self.worktree} or "
                                 + f"{unit_config.parked_path(row.id)}; ask the showrunner\n")
                self.assertFalse(self.worktree.exists())
                self.assertEqual(unit_config.place(row.id, self.worktree), unit_config.Nowhere())
                self.assertEqual(self.calls, [])
                self.assertEqual((self.doc.read_bytes(), self.staff.staff_file.read_bytes(),
                                  git(self.checkout, "rev-parse", "HEAD")), before)


    @override
    def build_repository(self) -> None:
        origin = self.root / "origin.git"
        _ = git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.checkout = self.root / "checkout"
        _ = git(self.root, "clone", "-q", str(origin), str(self.checkout))
        for setting in (("user.email", "test@example.invalid"), ("user.name", "test"), ("commit.gpgsign", "false")):
            _ = git(self.checkout, "config", *setting)
        self.worktree = self.root / "alpha-wt"
        self.doc = self.checkout / "alpha-production.md"
        _ = self.doc.write_text("# Production\n\n- **Merge branch:** main\n"
                                + f"- **Showrunner checkout:** {self.checkout}\n- **Log:** log.md\n", encoding="utf-8")
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "production")
        _ = git(self.checkout, "push", "-q", "origin", "main")

    @override
    def setUp(self) -> None:
        self.staff = ShutdownStaffFixture()
        self.staff.setUp()
        self.addCleanup(self.staff.doCleanups)
        self.restore_repository()
        self.enterContext(mock.patch.dict(os.environ, shutdown_fixture_configuration(self.root)))
        _ = self.enterContext(mock.patch.object(pane_record, "RECORD_DIR", self.root / "pane-launches"))
        self.configs = [self.root / "claude1", self.root / "claude2"]
        self.transcript(self.configs[0])

        self.calls = []
        self.launch_code = 0
        _ = self.staff.record_production("alpha", self.doc)
        production = read_production("alpha")

        def production_of(_production_name: str, *, record: staff.StaffSnapshot) -> Production:
            del record
            return production

        def not_marked(_production_name: str, _name: str) -> unit_lookup.LabeledSession | None:
            # The real pane writer retains a fixed owner; the live process resumes
            # that owner's current conversation, which can differ after /clear.
            owner = staff.by_current_session(DIRECTOR)
            entries = [entry for entry in pane_record.launches()
                       if owner is not None and entry.get("staff_id") == owner.id]
            return (unit_lookup.LabeledSession("alpha", unit_lookup.LiveClaude("alpha", DIRECTOR, "fixture", 123),
                                          HerdrPlace("agents", entries[-1]["pane_id"]))
                    if entries else None)

        _ = self.enterContext(mock.patch.object(shutdown, "retirement_production_of", production_of))
        _ = self.enterContext(mock.patch.object(shutdown, "retirement_run", self.fake_run))
        _ = self.enterContext(mock.patch.object(unit_lookup, "labeled_session", not_marked))
        _ = self.enterContext(mock.patch.object(sessions, "config_dirs", lambda: self.configs))
        _ = self.enterContext(mock.patch.object(session_records, "config_dirs", lambda: self.configs))
        _ = cast(MutableMapping[str, str], self.enterContext(
            mock.patch.dict(os.environ, {"SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
                                       "HERDR_PANE_RECORD_DIR": str(self.root / "pane-launches")})))
        _ = self.staff.record_production("alpha", self.doc)
        row = unit_membership("alpha", "alpha")
        row = staff.update_current_session(row.current_session, staff.StaffId(DIRECTOR))
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree, staff.ParkedBranch(row.name)), expected=row.worktree)
        self.retained_config()

    def retained_config(self, *, plan: str = "docs/plans/alpha.md") -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        unit_config.discard_parked(row.id)
        _ = unit_config.write_parked(row.id, unit_config.UnitConfig(unit_config.PlanFile(Path(plan)),
            unit_config.AppPort(1), ("scripts",),
            unit_config.RetiredSession("2026-10-20", row.current_session)))

    def transcript(self, config: Path, cwd: Path | None = None) -> None:
        cwd = self.worktree if cwd is None else cwd
        folder = config / "projects" / unit_move.project_folder(str(cwd))
        folder.mkdir(parents=True, exist_ok=True)
        _ = (folder / f"{DIRECTOR}.jsonl").write_text(json.dumps({"entrypoint": "cli", "cwd": str(cwd)}) + "\n", encoding="utf-8")

    def fake_run(self, argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Git runs for real; add_unit.py is recorded and answered."""
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        self.calls.append([Path(argv[1]).name, *argv[2:]])
        if self.launch_code == 0:
            pane_record.write("agents", pane_record.launch("w1:p1", DIRECTOR, str(self.worktree),
                {}, ["claude", "--resume", DIRECTOR]))
        return subprocess.CompletedProcess(argv, self.launch_code, "", "launch failed" if self.launch_code else "")

    def resume(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = shutdown.main(["resume", "alpha", "--retired", *arguments])
        return code, output.getvalue()

    def assert_resumed(self, code: int, output: str) -> None:
        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls[-1], ["add_unit.py", "--production", "alpha", "alpha", "--resume", DIRECTOR,
                                          "--cwd", str(self.worktree), "--join-recovery-locks",
                                          "--plan", "docs/plans/alpha.md"])
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), git(self.checkout, "rev-parse", "main"))
        self.assertIn("alpha resumed", output)

    def test_retired_resume_rerun(self) -> None:
        _ = self.staff.record_production("alpha", self.doc)
        row = unit_membership("alpha", "alpha")
        row = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree, staff.ParkedBranch("alpha")), expected=row.worktree)
        saved = unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
            unit_config.AppPort(1), ("scripts",),
            unit_config.RetiredSession("2026-10-20", DIRECTOR))
        _ = unit_config.write_parked(row.id, saved)
        parked = unit_config.parked_path(row.id)
        retained = parked.read_bytes(), parked.stat().st_mtime_ns
        original = staff.restore_unit
        for after in (False, True):
            def interrupted(identity: staff.StaffId,
                            worktree: staff.PresentWorktree) -> staff.StaffRow:
                if after:
                    _ = original(identity, worktree)
                raise RuntimeError("stopped at staff step")
            with mock.patch.object(staff, "restore_unit", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "stopped at staff step"):
                    _ = self.resume()
            self.assertTrue(self.worktree.is_dir())
            self.assertEqual(unit_config.read(self.worktree),
                             unit_config.UnitConfig(saved.plan, saved.port, saved.responsibilities))
            self.assertEqual((parked.read_bytes(), parked.stat().st_mtime_ns), retained)
            self.assertEqual(self.calls, [])
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        rows = staff.find(kind="unit")
        self.assertEqual([row.name for row in rows], ["alpha"])
        data, document = self.staff.staff_file.read_bytes(), self.doc.read_bytes()
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertIn("already resumed", output)
        self.assertEqual((self.staff.staff_file.read_bytes(), self.doc.read_bytes()), (data, document))
        self.assertEqual(len(self.calls), 1)

    def test_retire_then_resume_reuses_the_staff_identity_at_the_add_unit_boundary(self) -> None:
        retirement = RetireUnitTests()
        self.addCleanup(retirement.doCleanups)
        retirement.setUp()
        _ = self.enterContext(mock.patch.object(sessions, "config_dirs", lambda: [retirement.config]))
        _ = self.enterContext(mock.patch.object(session_records, "config_dirs", lambda: [retirement.config]))
        unit = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        tip = git(retirement.worktree, "rev-parse", "HEAD")
        code, output = retirement.retire()
        self.assertEqual(code, 0, output)
        self.assertEqual(staff.by_id(unit.id), replace(unit, worktree=staff.RemovedWorktree(remote.local_machine(), retirement.worktree, staff.ParkedBranch("alpha-branch"))))
        restore = self.enterContext(mock.patch.object(
            worker, "restore", return_value=worker.ExitCode.OK))
        launched_ids: list[staff.StaffId] = []
        commands: list[list[str]] = []

        def launch(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            if len(argv) > 1 and Path(argv[1]).name == "add_unit.py":
                commands.append(argv)
                production = read_production(argv[argv.index("--production") + 1])
                row = add_unit.ensure_staff_unit(production, argv[4])
                launched_ids.append(row.id)
                retirement.marked = unit_lookup.LabeledSession("alpha",
                    unit_lookup.LiveClaude("alpha", DIRECTOR, "fixture", 123), HerdrPlace("agents", "w1:p1"))
                pane_record.write("agents", pane_record.launch("w1:p1", DIRECTOR, str(retirement.worktree),
                    {}, ["claude", "--resume", DIRECTOR]))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return retirement.fake_run(argv, cwd)

        resumed = io.StringIO()
        with mock.patch.object(shutdown, "retirement_run", side_effect=launch), redirect_stdout(resumed):
            code = shutdown.main(["resume", "alpha", "--retired"])
        self.assertEqual(code, 0, resumed.getvalue())
        restore.assert_called_once_with(unit.id)
        self.assertEqual(launched_ids, [unit.id])
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][commands[0].index("--resume") + 1], DIRECTOR)
        expected = replace(unit, home=staff.SessionHome(Path.home() / ".claude"))
        self.assertEqual(staff.by_id(unit.id), expected)
        assert isinstance(unit.parent, staff.ReportsTo)
        self.assertEqual(staff.find(kind="unit", parent=unit.parent.id), [expected])
        self.assertEqual(git(retirement.worktree, "rev-parse", "HEAD"), tip)
        before = (retirement.staff.staff_file.read_bytes(), retirement.doc.read_bytes(),
                  git(retirement.checkout, "for-each-ref"))
        with mock.patch.object(shutdown, "retirement_run", side_effect=launch), redirect_stdout(io.StringIO()):
            code = shutdown.main(["resume", "alpha", "--retired"])
        self.assertEqual(code, 0)
        self.assertEqual(restore.call_args_list, [mock.call(unit.id), mock.call(unit.id)])
        self.assertEqual(len(commands), 1)
        self.assertEqual((retirement.staff.staff_file.read_bytes(), retirement.doc.read_bytes(),
                          git(retirement.checkout, "for-each-ref")), before)


    def parked_facts(self) -> Path:
        return unit_config.parked_path(next(row for row in staff.find(kind="unit") if row.name == "alpha").id)


    def test_successful_launch_keeps_parked_facts_until_its_staff_pane_is_proven(self) -> None:
        row = staff.find(kind="unit")[0]
        saved = unit_config.parked_path(row.id).read_bytes()
        document = self.doc.read_bytes()
        with mock.patch.object(shutdown, "recovery_resumed_session_proven", return_value=False):
            code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn("resumed native session identity is not proven", output)
        self.assertEqual(unit_config.parked_path(row.id).read_bytes(), saved)
        self.assertEqual(self.doc.read_bytes(), document)
        self.assertEqual(len(self.calls), 1)
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(unit_config.parked_path(row.id).exists())

    def test_resume_proof_requires_the_exact_pane_owner_and_current_conversation(self) -> None:
        row = staff.find(kind="unit")[0]
        pane_record.write("agents", pane_record.launch("w1:p1", DIRECTOR, str(self.worktree),
            {}, ["claude", "--resume", DIRECTOR]))
        before = (self.staff.staff_file.read_bytes(), pane_record.record_path("agents").read_bytes(),
                  unit_config.parked_path(row.id).read_bytes())
        with self.subTest(evidence="fixed owner for the live current conversation"):
            self.assertTrue(shutdown.recovery_resumed_session_proven("alpha", "alpha", DIRECTOR, row))
        current = unit_lookup.LabeledSession("alpha",
            unit_lookup.LiveClaude("alpha", DIRECTOR, "fixture", 123), HerdrPlace("agents", "w1:p1"))
        other_session = fixture_runtime_id("unrelated-resume-conversation")
        for marked, expected_session in (
            (replace(current, place=HerdrPlace("agents", "w1:other")), DIRECTOR),
            (replace(current, claude=unit_lookup.LiveClaude("alpha", other_session, "fixture", 123)), DIRECTOR),
            (current, row.id),
        ):
            with self.subTest(marked=marked, expected_session=expected_session), mock.patch.object(
                    unit_lookup, "labeled_session", return_value=marked):
                self.assertFalse(shutdown.recovery_resumed_session_proven("alpha", "alpha", expected_session, row))
        self.assertEqual((self.staff.staff_file.read_bytes(), pane_record.record_path("agents").read_bytes(),
                          unit_config.parked_path(row.id).read_bytes()), before)

    def test_retired_unit_receipt_cannot_complete_a_new_incarnation(self) -> None:
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        unit = staff.find(kind="unit")[0]
        self.staff.forget_rows(unit.id)
        replacement = fixture_member("unit", name=unit.name, parent=unit.parent,
                                     runtime_id=fixture_runtime_id("replacement-unit-backend"))
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertNotIn("already resumed", output)
        self.assertEqual(staff.by_id(replacement.id), replacement)
        self.assertEqual(len(self.calls), 1)

    def test_resume_child_joins_the_operation_until_the_parked_facts_are_discarded(self) -> None:
        source = self.staff.production("alpha", "alpha")
        source.unlink()
        source.symlink_to(self.doc)
        _ = self.staff.record_production("alpha", source)
        row = unit_membership("alpha", "alpha")
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), self.worktree), expected=row.worktree)
        wrapper = self.root / "resume-launch.py"
        # Run the add_unit entry point with a fixture pane and the real lock checks.
        write_text_atomic(wrapper, "\n".join([
            "import os, subprocess, sys", "from unittest.mock import patch",
            "from datetime import datetime, timezone", "from pathlib import Path",
            f'with open(Path({str(CHECKOUT)!r}) / "scripts/lib/package_root.py") as package_root: exec(package_root.read(), {{"__file__": package_root.name}})',
            "from scripts.production import add_unit", "from scripts.herdr import herdr", "from scripts.accounts import accounts",
            "from scripts.agents import pane_record, remote",
            "def preflight(request, selected, *, record):",
            "    launch = add_unit.UnitLaunch(production=request.production, branch=request.branch, worktree=request.session.cwd,",
            "        plan=request.plan, port=request.port, session=request.session, timeout=request.timeout,",
            "        identity=add_unit.UnitIdentity(request.unit, request.requested_name))",
            "    return add_unit.ReadyToLaunch(launch, add_unit.NoUnitRow(), add_unit.DirectorAgent('sonnet', add_unit.DefaultEffort()))",
            "def publish(ready, *, record):",
            "    launch = ready.launch",
            "    return add_unit.ensure_staff_unit(launch.production, launch.unit_id,",
            "                                      runtime_id=launch.session.session_id, record=record)",
            "def launch(*args, **kwargs):",
            "    competing = [arg for arg in sys.argv[1:] if arg != '--join-recovery-locks']",
            "    competing[competing.index('--resume')] = '--attach'",
            "    env = {key: val for key, val in os.environ.items() if not key.startswith('STAFF_ROW_FD_')}",
            "    result = subprocess.run([sys.executable, __file__, *competing], text=True, capture_output=True,",
            "        env=env, pass_fds=(int(env['STAFF_OPERATION_FD']),), timeout=10, check=False)",
            "    assert result.returncode == 2 and 'another operation holds this row' in result.stderr, result.stderr",
            f"    pane_record.write('agents', pane_record.launch('w1:p1', {DIRECTOR!r}, {str(self.worktree)!r}, {{}}, ['claude']))",
            "    return herdr.PromptSubmitted(datetime.now(timezone.utc))",
            "with (patch.object(accounts, 'check', return_value=[]),",
            f"      patch.object(remote, 'local_machine', return_value={remote.local_machine()!r}),",
            "      patch.object(add_unit, 'account_for_launch', return_value=accounts.NamedAccount('claude3', Path.home() / '.claude3')),",
            "      patch.object(add_unit, 'model_for_resume', return_value=''),",
            "      patch.object(add_unit, 'autocompact_for_resume', return_value=''),",
            "      patch.object(add_unit, 'preflight', side_effect=preflight),",
            "      patch.object(add_unit, 'publish_unit', side_effect=publish),",
            "      patch.object(add_unit, 'launched_unit', return_value=add_unit.NoLabeledSession()),",
            "      patch.object(add_unit, 'prompt_for', return_value='Fixture opening.'),",
            "      patch.object(add_unit, 'launch_session', side_effect=launch),",
            "      patch.object(add_unit, 'wait_for_remote_control'), patch.object(add_unit, 'record'),",
            "      patch.object(add_unit, 'attach_hint', return_value='fixture pane')):",
            "    raise SystemExit(add_unit.main(sys.argv[1:]))", "",
        ]))
        context = multiprocessing.get_context("fork")
        first, second = context.Pipe()
        parent = cast(Connection, cast(object, first))
        child = cast(Connection, cast(object, second))
        original_discard = unit_config.discard_parked
        def finish(unit: staff.StaffId) -> None:
            child.send((staff.snapshot().find(kind="unit")[0].id, bool(staff.operation_fds())))
            _ = cast(object, child.recv())
            original_discard(unit)
        def run() -> None:
            parent.close()
            try:
                with mock.patch.object(shutdown, "retirement_run", RUN_COMMAND), mock.patch.object(
                        shutdown, "ADD_UNIT", wrapper), mock.patch.object(unit_config, "discard_parked", side_effect=finish):
                    child.send(self.resume())
            finally:
                child.close()
        process = context.Process(target=run)
        writer: subprocess.Popen[str] | None = None
        try:
            with self.staff.lock_notice(self.staff.staff_file.parent / "operation.lock") as listener:
                process.start()
                child.close()
                self.assertTrue(parent.poll(TEST_HANG_GUARD_SECS), "nested add_unit did not join resume's lock")
                identity, lock_held = cast(tuple[str, bool], parent.recv())
                self.assertIsInstance(identity, str, f"resume exited before discard: {(identity, lock_held)!r}")
                self.assertIsInstance(lock_held, bool)
                staff_id = staff.StaffId(identity)
                self.assertTrue(lock_held, "resume released its operation lock before discarding the parked facts")
                self.assertTrue(unit_config.parked_path(staff_id).exists())
                writer = self.staff.start_script(CHECKOUT / "scripts/production/staff.py", "rename", identity, "--name", "alpha")
                self.staff.assert_operation_blocks(writer, listener)
                parent.send("finish")
                self.assertTrue(parent.poll(TEST_HANG_GUARD_SECS), "resume did not discard the parked facts")
                code, output = cast(tuple[int, str], parent.recv())
                self.assertEqual(code, 0, output)
                process.join(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.exitcode, 0)
                output, errors = writer.communicate(timeout=TEST_HANG_GUARD_SECS)
            self.assertEqual(writer.returncode, 0, output + errors)
            self.assertFalse(unit_config.parked_path(staff_id).exists())
            row = staff.by_id(staff_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual((row.kind, row.name), ("unit", "alpha"))
            self.assertEqual(staff.find(kind="unit"), [row])
        finally:
            parent.close()
            child.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=TEST_HANG_GUARD_SECS)
            process.close()
            if writer is not None and writer.returncode is None:
                writer.kill()
                _ = writer.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_resumes_the_recorded_session_in_a_worktree_made_from_the_merge_branch(self) -> None:
        self.assert_resumed(*self.resume())
        self.assertEqual(build_folders.build_folder_state(self.worktree), "free")

    def test_recovery_worktree_uses_an_existing_branch_at_its_saved_tip(self) -> None:
        _ = git(self.checkout, "branch", "alpha", "main")
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        with redirect_stdout(io.StringIO()) as output:
            shutdown.recovery_make_worktree(unit)
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), git(self.checkout, "rev-parse", "alpha"))
        self.assertIn(f"made: worktree {self.worktree} on alpha", output.getvalue())
        self.assertEqual(build_folders.build_folder_state(self.worktree), "free")

    def test_recovery_retries_after_a_checkout_hook_fails(self) -> None:
        hook = self.checkout / ".git/hooks/post-checkout"
        hook.parent.mkdir(exist_ok=True)
        _ = hook.write_text("#!/bin/sh\nprintf 'checkout failed' >&2\nexit 1\n")
        hook.chmod(0o755)
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn("checkout failed", output)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        self.assertEqual(self.calls, [])
        hook.unlink()
        self.assert_resumed(*self.resume())
        self.assertEqual(build_folders.build_folder_state(self.worktree), "free")

    def test_retired_as_built_without_a_header_is_passed_to_the_resume_launch(self) -> None:
        plan = "docs/as-built/alpha.md"
        path = self.checkout / plan
        path.parent.mkdir(parents=True)
        _ = path.write_text("# Alpha as built\n\nThe plan is complete.\n", encoding="utf-8")
        self.retained_config(plan=plan)
        _ = git(self.checkout, "add", "-A")
        _ = git(self.checkout, "commit", "-q", "-m", "condensed plan")

        code, output = self.resume()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.calls, [["add_unit.py", "--production", "alpha", "alpha",
                                      "--resume", DIRECTOR, "--cwd", str(self.worktree), "--join-recovery-locks",
                                      "--plan", plan]])

    def test_a_transcript_under_another_account_resumes(self) -> None:
        # The units account moved since the retire: the transcript is under the other config dir.
        self.configs.reverse()
        self.assert_resumed(*self.resume())

    def test_a_failed_launch_keeps_the_parked_facts_and_a_rerun_resumes(self) -> None:
        document = self.doc.read_bytes()
        head = git(self.checkout, "rev-parse", "HEAD")
        self.launch_code = 1
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn("refused: add_unit.py --resume: launch failed", output)
        self.assertTrue(unit_config.parked_path(staff.find(kind="unit")[0].id).exists())
        self.assertEqual(self.doc.read_bytes(), document)
        self.assertEqual(git(self.checkout, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.worktree, "branch", "--show-current"), "alpha")
        self.assertEqual(len(self.calls), 1)
        worktree_head = git(self.worktree, "rev-parse", "HEAD")
        self.launch_code = 0
        code, output = self.resume()
        self.assert_resumed(code, output)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), worktree_head)
        self.assertNotIn("made: worktree", output)

    def test_transcript_and_branch_refusals_change_nothing(self) -> None:
        self.configs = [self.root / "claude3"]
        _ = git(self.checkout, "branch", "alpha", "main")
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn(f"refused: alpha: no account has the transcript of session {DIRECTOR}", output)
        self.assertIn("refused: branch alpha still exists; retire did not finish", output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.checkout, "log", "-1", "--format=%s"), "production")


    def test_missing_session_refuses_launch_when_no_evidence_exists(self) -> None:
        self.configs = [self.root / "empty-config"]
        unit_config.discard_parked(staff.find(kind="unit")[0].id)
        before = self.doc.read_bytes()
        for flags in ((),):
            with self.subTest(flags=flags):
                code, output = self.resume(*flags)
                self.assertEqual(code, 1, output)
                self.assertIn("; ask the showrunner", output)
                self.assertNotIn("would resume:", output)
                self.assertNotIn("resumed:", output)
                self.assertEqual(self.calls, [])
                self.assertFalse(self.worktree.exists())
                self.assertEqual(self.doc.read_bytes(), before)

    def test_parked_session_survives_newer_worker_transcripts(self) -> None:
        older = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / "older.jsonl"
        _ = older.write_text(json.dumps({"entrypoint": "cli", "cwd": str(self.worktree)}) + "\n")
        os.utime(older, (1, 1))
        worker = older.with_name("worker.jsonl")
        _ = worker.write_text(json.dumps({"entrypoint": "sdk-py", "cwd": str(self.worktree)}) + "\n")
        self.assert_resumed(*self.resume())

    def test_parked_session_survives_stale_retained_pane_launches(self) -> None:
        for pane, identity, at in (("w1:p1", "older", "2026-10-27T23:00:00+00:00"),
                                    ("w1:p2", DIRECTOR, "2026-10-28T23:00:00+00:00")):
            launch = pane_record.launch(pane, identity, str(self.worktree),
                {('' + 'SHOWRUNNER_' + 'UNIT_ID'): "alpha", "CLAUDE_CONFIG_DIR": str(self.configs[0])}, ["--resume", identity])
            launch["written_at"] = at
            pane_record.write("agents", launch)
        current_backend = unit_lookup.labeled_session

        def observed_backend(production_name: str, name: str) -> unit_lookup.LabeledSession | None:
            # Retained pane files are historical evidence. Only the fake launch
            # establishes a live backend for this test.
            return current_backend(production_name, name) if self.calls else None

        with mock.patch.object(unit_lookup, "labeled_session", side_effect=observed_backend):
            self.assert_resumed(*self.resume())

    def external_transcript(self) -> Path:
        for transcript in (self.configs[0] / "projects").glob(f"*/{DIRECTOR}.jsonl"):
            transcript.unlink()
        folder = self.root / "original-checkout"
        folder.mkdir()
        self.transcript(self.configs[0], folder)
        return folder

    def test_resume_finds_the_session_transcript_outside_the_staff_worktree(self) -> None:
        _ = self.external_transcript()
        code, output = self.resume()
        self.assertEqual(code, 0, output)
        self.assertIn(["--cwd", str(self.worktree)], [call[index:index + 2] for call in self.calls for index in range(len(call) - 1)])
        self.assertTrue(self.worktree.is_dir())

    def test_renamed_transcript_keeps_the_staff_resume_folder(self) -> None:
        transcript = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        old = self.root / "alpha-old-wt"
        _ = transcript.write_text("\n".join(json.dumps({"cwd": str(cwd)})
                                            for cwd in (old, old, self.worktree, self.worktree)) + "\n")
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        self.assertEqual(unit.worktree, self.worktree)
        self.assertNotIn(str(old), shutdown.recovery_launch_command(unit))
        self.assert_resumed(*self.resume())
        self.assertEqual(len(self.calls), 1)

    def test_resume_ignores_the_transcript_folder_and_recorded_cwd(self) -> None:
        transcript = self.configs[0] / "projects" / unit_move.project_folder(str(self.worktree)) / f"{DIRECTOR}.jsonl"
        _ = transcript.write_text(json.dumps({"cwd": str(self.root / "alpha-old-wt")}) + "\n")
        before = transcript.read_bytes(), transcript.stat().st_mtime_ns
        self.assert_resumed(*self.resume())
        self.assertEqual((transcript.read_bytes(), transcript.stat().st_mtime_ns), before)

    def test_duplicate_transcript_locations_do_not_change_the_staff_resume_folder(self) -> None:
        _ = self.external_transcript()
        self.transcript(self.configs[1], self.root / "another-checkout")
        self.assert_resumed(*self.resume())

    def test_missing_session_transcript_keeps_the_retirement_record(self) -> None:
        for config in self.configs:
            for transcript in (config / "projects").glob(f"*/{DIRECTOR}.jsonl"):
                transcript.unlink()
        before = self.doc.read_bytes()
        code, output = self.resume()
        self.assertEqual(code, 1, output)
        self.assertIn(f"no account has the transcript of session {DIRECTOR}", output)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.doc.read_bytes(), before)

    def test_resume_uses_a_changed_staff_folder_without_rewriting_retirement_facts(self) -> None:
        row = unit_membership("alpha", "alpha")
        parked = unit_config.parked_path(row.id)
        before = parked.read_bytes(), parked.stat().st_mtime_ns
        destination = self.root / "changed-staff-folder"
        _ = staff.set_worktree(row.id, staff.RemovedWorktree(remote.local_machine(), destination), expected=row.worktree)
        unit = shutdown.recovery_find_unit("alpha", "alpha")
        assert isinstance(unit, shutdown.RetiredUnitRecovery)
        command = shutdown.recovery_launch_command(unit)
        self.assertEqual(command[command.index("--cwd") + 1], str(destination))
        self.assertTrue(shutdown.recovery_has_transcript(unit))
        self.assertEqual((parked.read_bytes(), parked.stat().st_mtime_ns), before)

    def test_shared_projects_symlink_is_one_transcript_location(self) -> None:
        self.configs[1].mkdir()
        (self.configs[1] / "projects").symlink_to(self.configs[0] / "projects", target_is_directory=True)
        self.assert_resumed(*self.resume())


class WorkerRenameTests(StaffTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(shutdown, "rename_send_notice"))

    def test_duplicate_owner_name_keeps_staff_and_stopped_worker_panes_unchanged(self) -> None:
        from scripts.herdr.test_worker_panes import PaneWorld

        world = PaneWorld()
        world.setup(self)
        runner = fixture_member("showrunner", name="TEST-runner")
        owner = fixture_member("unit", name="TEST-old", parent=runner.id)
        _ = fixture_member("unit", name="TEST-taken", parent=runner.id)
        child = fixture_worker("review", owner.id)
        world.add(owner, "w1:p0")
        world.add(child, "w1:p1", live=False, label="TEST-old-review")
        world.record(child, "w1:p1")
        before = self.staff_file.read_bytes()

        def retitle(pane: str, title: str, *, tab: bool = True) -> bool:
            del tab
            world.locations[pane]["label"] = title
            return True

        with mock.patch.object(world, "retitle", side_effect=retitle):
            arguments = ["rename", str(owner.id), "TEST-taken"]
            refused = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments, dict(os.environ), CHECKOUT)
            self.assertNotEqual(refused.returncode, 0, refused.stderr)
            self.assertEqual(self.staff_file.read_bytes(), before)
            self.assertEqual(world.locations["w1:p1"]["label"], "TEST-old-review")
            arguments = ["rename", str(owner.id), "TEST-free"]
            result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments, dict(os.environ), CHECKOUT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(world.locations["w1:p1"]["label"], "TEST-free-review")
        self.assertIsInstance(worker_panes.observe(owner.id, staff.snapshot())[0], worker_panes.StoppedWorker)

    def test_owner_rename_keeps_workers_findable_closable_and_dispatchable(self) -> None:
        from contextlib import chdir
        from scripts.herdr.test_worker_panes import WorkerPaneTests

        for kind in ("unit", "showrunner"):
            with self.subTest(kind=kind):
                fixture = WorkerPaneTests()
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                write_text_atomic(fixture.root / "conf/production.conf",
                                  "# range: 0.01..\nRENAME_COMMAND_TIMEOUT_SECONDS=30\n")
                owner = (staff.rename(fixture.parent.id, "TEST-old") if kind == "unit" else
                         fixture_member("showrunner", name="TEST-old", parent=fixture.parent.parent))
                fixture.parent_id = owner.id
                if kind == "showrunner":
                    fixture.world.add(owner, "w2:p0", workspace="w2", tab="w2:t2")
                _ = fixture.enterContext(chdir(fixture.root))
                _ = subprocess.run(["git", "init", "-q", "-b", "TEST-checkout", str(fixture.root)],
                                   check=True, capture_output=True, text=True)
                started = fixture.start("impl", "impl")
                self.assertIsInstance(started, worker_panes.OpenWorker)
                assert isinstance(started, worker_panes.OpenWorker)
                stopped = fixture.worker("review")
                location = fixture.world.locations[started.pane]
                fixture.world.add(stopped, "w1:p9", live=False, label="TEST-old-review",
                                  workspace=str(location["workspace_id"]), tab=str(location["tab_id"]))
                fixture.world.record(stopped, "w1:p9")
                old_name = fixture.world.agent_names[-1]
                agents = {old_name: started.pane}
                original_run = fixture.world.run

                def run(*arguments: str, deadline: float = math.inf) -> str:
                    if arguments[:2] == ("agent", "rename"):
                        pane, name = arguments[2:]
                        previous = next(key for key, value in agents.items() if value == pane)
                        del agents[previous]
                        agents[name] = pane
                        fixture.world.commands.append(arguments)
                        return ""
                    return original_run(*arguments, deadline=deadline)

                failed_panes = {"w1:p9"}

                def retitle(pane: str, title: str, *, tab: bool = True) -> bool:
                    del tab
                    if pane in failed_panes:
                        failed_panes.remove(pane)
                        raise herdr_types.HerdrFailure("fixture retitle failed")
                    changed = fixture.world.locations[pane]["label"] != title
                    fixture.world.locations[pane]["label"] = title
                    return changed

                arguments = ["rename", str(owner.id), "TEST-new"]
                with (mock.patch.object(fixture.world, "run", side_effect=run),
                      mock.patch.object(fixture.world, "retitle", side_effect=retitle),
                      mock.patch.object(fixture.world, "save_layout")):
                    failed = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                        dict(os.environ), CHECKOUT)
                    self.assertIn("fixture retitle failed", failed.stderr)
                    self.assertEqual(staff.by_id(owner.id), owner)
                    result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                        dict(os.environ), CHECKOUT)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(agents, {"TEST-new-impl": started.pane})
                self.assertEqual(fixture.world.locations[started.pane]["label"], "TEST-new-impl")
                self.assertNotIn(old_name, [pane["label"] for pane in fixture.world.locations.values()])
                self.assertNotIn("impl", [pane["label"] for pane in fixture.world.locations.values()])
                self.assertEqual(fixture.world.locations["w1:p9"]["label"], "TEST-new-review")
                self.assertIsInstance(fixture.observation(stopped), worker_panes.StoppedWorker)
                self.assertIsInstance(fixture.observation(started.row), worker_panes.OpenWorker)
                fixture.completed_dispatch(started.row, "impl")
                closed = worker_panes.close(owner.id, staff.WorkerName.parse("impl"))
                self.assertIsInstance(closed, worker_panes.Closed)
                self.assertNotIn(started.pane, fixture.world.locations)
                resumed = fixture.start("impl", "impl")
                self.assertIsInstance(resumed, worker_panes.OpenWorker)
                assert isinstance(resumed, worker_panes.OpenWorker)
                self.assertEqual(resumed.row.id, started.row.id)
                self.assertEqual(fixture.world.agent_names[-1], "TEST-new-impl")
                self.assertEqual(fixture.world.locations[resumed.pane]["label"], "TEST-new-impl")
                self.assertNotIn(old_name, [pane["label"] for pane in fixture.world.locations.values()])

        class QualifiedWorkerCase(Enum):
            OWNER_SUFFIX = "a prefix ends with the owner name"
            OWNER_PREFIX = "the owner name is the prefix"
            UNDECIDED = "the prefix does not end with the owner name"
            DASHED_ROLE = "the bare role contains a dash"
            NUMBERED_DASHED_ROLE = "the bare role contains a dash and digits"
            UNDECIDED_DASHED_ROLE = "an undecided prefix keeps the dashed role"
            SHELL_NO_TWIN = "the renamed row retains its shell pane"
            PROCESS_UNKNOWN = "the shell process evidence is temporarily unreadable"
            TERMINAL_UNKNOWN = "the shell terminal evidence is temporarily incomplete"
            RETIRE_REFUSED = "a live native process prevents retirement"
            CLOSED_CLASH = "the old row has no pane or active dispatch"
            MISLEADING_LABEL = "another native owner has the old worker display name"
            ACTIVE_CLASH = "the old row has an active dispatch"
            OPEN_CLASH = "the old row has an open pane"
            SHELL_CLASH = "the old row retains its shell pane"

        for owner_kind in ("unit", "showrunner"):
            for scenario in QualifiedWorkerCase:
                with self.subTest(owner_kind=owner_kind, migration=scenario):
                    fixture = WorkerPaneTests()
                    fixture.setUp()
                    try:
                        _ = fixture.enterContext(mock.patch.object(unit_runs, "ASTRA_RUN_ROOT", fixture.root / "astra"))
                        owner = (staff.rename(fixture.parent.id, "TEST-handler") if owner_kind == "unit" else
                                 fixture_member("showrunner", name="TEST-handler", parent=fixture.parent.parent))
                        fixture.parent_id = owner.id
                        if owner_kind == "showrunner":
                            fixture.world.add(owner, "w2:p0", workspace="w2", tab="w2:t2")
                        bare = ("astra-designer" if scenario in (QualifiedWorkerCase.DASHED_ROLE,
                                QualifiedWorkerCase.UNDECIDED_DASHED_ROLE) else
                                "astra-reviewer1481574" if scenario is QualifiedWorkerCase.NUMBERED_DASHED_ROLE else "review")
                        prefix = ("TEST-handler" if scenario is QualifiedWorkerCase.OWNER_PREFIX else
                                  "TEST-handler-enhancements" if scenario in (QualifiedWorkerCase.UNDECIDED,
                                      QualifiedWorkerCase.UNDECIDED_DASHED_ROLE) else
                                  "cargo-liner-TEST-handler")
                        qualified = fixture.worker(prefix + "-" + bare)
                        twins = (QualifiedWorkerCase.CLOSED_CLASH, QualifiedWorkerCase.MISLEADING_LABEL, QualifiedWorkerCase.ACTIVE_CLASH,
                                 QualifiedWorkerCase.OPEN_CLASH, QualifiedWorkerCase.SHELL_CLASH,
                                 QualifiedWorkerCase.RETIRE_REFUSED)
                        bare_twins = [fixture.worker("review")] if scenario in twins else []
                        later = ([fixture.worker(prefix + "-impl")] if scenario is QualifiedWorkerCase.RETIRE_REFUSED else [])
                        if scenario is QualifiedWorkerCase.RETIRE_REFUSED:
                            live = session(qualified.current_session, qualified.name, os.getpid())
                            live["procStart"] = sessions.process_start(os.getpid()) or ""
                            write_text_atomic(sessions.sessions_dir() / f"{os.getpid()}.json", json.dumps(live))
                        dispatches: list[str] = []
                        if scenario is QualifiedWorkerCase.ACTIVE_CLASH:
                            dispatch_run = fixture.run_state()
                            prompt = fixture.root / "TEST-prompt.txt"
                            write_text_atomic(prompt, "Review the TEST change.")
                            dispatches.append(progress_history.begin_dispatch(dispatch_run, slot=qualified.name, phase="earlier", role="review",
                                worker_name=qualified.name, worker_id=qualified.id, launcher_command=["assign.sh"],
                                prompt=prompt, kind="review", lens=""))
                        recovery_cases = (QualifiedWorkerCase.PROCESS_UNKNOWN, QualifiedWorkerCase.TERMINAL_UNKNOWN)
                        shell_cases = (QualifiedWorkerCase.SHELL_NO_TWIN, *recovery_cases)
                        if scenario in (QualifiedWorkerCase.OPEN_CLASH, QualifiedWorkerCase.SHELL_CLASH, *shell_cases):
                            fixture.world.add(qualified, "w1:p9", live=scenario is QualifiedWorkerCase.OPEN_CLASH,
                                              workspace="w2" if owner_kind == "showrunner" and scenario in shell_cases else "w1",
                                              tab="w2:t2" if owner_kind == "showrunner" and scenario in shell_cases else "w1:t1")
                        if scenario is QualifiedWorkerCase.PROCESS_UNKNOWN:
                            fixture.world.unknown["w1:p9"] = "TEST process evidence unavailable"
                        if scenario is QualifiedWorkerCase.TERMINAL_UNKNOWN:
                            fixture.world.locations["w1:p9"]["terminal_id"] = ""
                        if scenario is QualifiedWorkerCase.MISLEADING_LABEL:
                            foreign = fixture_member("showrunner", name="foreign-native-owner")
                            fixture.world.add(foreign, "w1:foreign", label=qualified.name)
                        before = fixture.staff_file.read_bytes()
                        arguments = ["rename", str(owner.id), owner.name]
                        interruptions = (["before label", "after label"]
                                         if scenario is QualifiedWorkerCase.SHELL_NO_TWIN else [])
                        def retitle(pane: str, title: str, *, tab: bool = True) -> bool:
                            del tab
                            if pane == "w1:p9" and interruptions == ["before label", "after label"]:
                                del interruptions[0]
                                raise herdr_types.HerdrFailure("TEST interrupted before label")
                            changed = fixture.world.locations[pane]["label"] != title
                            fixture.world.locations[pane]["label"] = title
                            if pane == "w1:p9" and interruptions:
                                del interruptions[0]
                                raise herdr_types.HerdrFailure("TEST interrupted after label")
                            return changed
                        server_read, server_command = fixture.world.result, fixture.world.run
                        workspace_labels = {"w1": "unit", "w2": "other"}
                        def migration_read(*arguments: str, deadline: float = math.inf) -> dict[str, object]:
                            result = server_read(*arguments, deadline=deadline)
                            if arguments[:2] == ("workspace", "list"):
                                for space in cast(list[dict[str, object]], result["workspaces"]):
                                    space["label"] = workspace_labels[str(space["workspace_id"])]
                            return result
                        def migration_command(*arguments: str, deadline: float = math.inf) -> str:
                            if arguments[:2] == ("workspace", "rename"):
                                workspace_labels[arguments[2]] = arguments[3]
                            return server_command(*arguments, deadline=deadline)
                        with (mock.patch.object(fixture.world, "retitle", side_effect=retitle),
                              mock.patch.object(fixture.world, "result", side_effect=migration_read),
                              mock.patch.object(fixture.world, "run", side_effect=migration_command),
                              mock.patch.object(fixture.world, "save_layout"),
                              mock.patch.object(worker, "end", wraps=worker.end) as end):
                            if scenario is QualifiedWorkerCase.SHELL_NO_TWIN:
                                for point in ("before label", "after label"):
                                    interrupted = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                                             dict(os.environ), CHECKOUT)
                                    self.assertNotEqual(interrupted.returncode, 0, interrupted.stderr)
                                    self.assertIn("TEST interrupted " + point, interrupted.stderr)
                                    self.assertEqual(fixture.staff_file.read_bytes(), before)
                            result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                                dict(os.environ), CHECKOUT)
                            self.assertEqual(result.returncode, 0, result.stderr)
                            if scenario in recovery_cases:
                                self.assertEqual(fixture.staff_file.read_bytes(), before)
                                self.assertEqual(fixture.world.locations["w1:p9"]["label"], qualified.name)
                                reason = ("TEST process evidence unavailable" if scenario is QualifiedWorkerCase.PROCESS_UNKNOWN
                                          else "recovery terminal identity is missing or duplicated")
                                self.assertEqual([line for line in result.stderr.splitlines()
                                                  if line.startswith(qualified.name + " (")], [
                                    f"{qualified.name} ({qualified.id}): kept; {reason}"])
                                fixture.world.unknown.clear()
                                fixture.world.locations["w1:p9"]["terminal_id"] = "terminal-w1:p9"
                                result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                                    dict(os.environ), CHECKOUT)
                                self.assertEqual(result.returncode, 0, result.stderr)
                            unqualified = scenario in (QualifiedWorkerCase.UNDECIDED, QualifiedWorkerCase.UNDECIDED_DASHED_ROLE)
                            unchanged_count = len(bare_twins) + int(unqualified)
                            worker_names = "worker name" if unchanged_count == 1 else "worker names"
                            summary = (f"{unchanged_count} {worker_names} unchanged: no qualified prefix for {owner.name}\n"
                                       if unchanged_count else "")
                            self.assertTrue(result.stderr.endswith(summary), result.stderr)
                            migration_errors = result.stderr.removesuffix(summary) if summary else result.stderr
                            if scenario in (QualifiedWorkerCase.UNDECIDED, QualifiedWorkerCase.UNDECIDED_DASHED_ROLE):
                                self.assertEqual(fixture.staff_file.read_bytes(), before)
                                self.assertEqual(result.stderr, summary)
                            elif scenario is QualifiedWorkerCase.RETIRE_REFUSED:
                                self.assertEqual(staff.by_id(qualified.id), qualified)
                                self.assertEqual(staff.by_id(bare_twins[0].id), bare_twins[0])
                                self.assertEqual(staff.by_id(later[0].id), replace(later[0], name="impl"))
                                self.assertEqual(migration_errors.splitlines(), [
                                    f"{qualified.name} ({qualified.id}) and {bare_twins[0].name} ({bare_twins[0].id}): kept; "
                                    + f"{qualified.id}: native session is live (pid {os.getpid()}); stop it before removing its row"])
                                end.assert_called_once()
                            elif scenario in (QualifiedWorkerCase.CLOSED_CLASH, QualifiedWorkerCase.MISLEADING_LABEL):
                                self.assertEqual(staff.snapshot().find(kind="worker", parent=owner.id), bare_twins)
                                end.assert_called_once()
                                self.assertEqual(migration_errors, "")
                            elif scenario in (QualifiedWorkerCase.ACTIVE_CLASH, QualifiedWorkerCase.OPEN_CLASH,
                                              QualifiedWorkerCase.SHELL_CLASH):
                                self.assertEqual(fixture.staff_file.read_bytes(), before)
                                reason = (f"active dispatch {dispatches[0]}" if scenario is QualifiedWorkerCase.ACTIVE_CLASH else
                                          "the old pane is not closed")
                                twin = bare_twins[0]
                                self.assertEqual(migration_errors.splitlines(), [
                                    f"{qualified.name} ({qualified.id}) and {twin.name} ({twin.id}): kept; {reason}"])
                                end.assert_not_called()
                            else:
                                self.assertEqual(staff.by_id(qualified.id), replace(qualified, name=bare))
                                self.assertEqual(migration_errors, "")
                                if scenario in shell_cases:
                                    current = staff.by_id(qualified.id)
                                    assert current is not None
                                    self.assertEqual(fixture.world.locations["w1:p9"]["label"], owner.name + "-" + bare)
                                    observed = worker_panes.observe_worker(owner.id, current, staff.snapshot())
                                    self.assertIsInstance(observed, worker_panes.StoppedWorker)
                                    assert isinstance(observed, worker_panes.StoppedWorker)
                                    self.assertEqual(observed.pane, "w1:p9")
                            retained = fixture.staff_file.read_bytes()
                            panes = {pane: dict(value) for pane, value in fixture.world.locations.items()}
                            again = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                               dict(os.environ), CHECKOUT)
                            self.assertEqual(again.returncode, 0, again.stderr)
                            self.assertEqual(fixture.staff_file.read_bytes(), retained)
                            self.assertEqual(fixture.world.locations, panes)
                            remaining = staff.snapshot().find(kind="worker", parent=owner.id)
                            current = staff.by_id(qualified.id)
                            still_qualified = current is not None and current.name == qualified.name and not unqualified
                            unchanged_count = len(remaining) - int(still_qualified)
                            worker_names = "worker name" if unchanged_count == 1 else "worker names"
                            repeated_summary = (f"{unchanged_count} {worker_names} unchanged: no qualified prefix for {owner.name}\n"
                                                if unchanged_count else "")
                            self.assertEqual(again.stderr, migration_errors + repeated_summary)
                            self.assertEqual(again.stdout, "unchanged\n")
                            self.assertEqual(fixture.world.stops, [])
                            self.assertEqual(fixture.world.closed, [])
                    finally:
                        fixture.doCleanups()

    def test_owner_self_rename_summarizes_bare_and_foreign_worker_names(self) -> None:
        from scripts.herdr.test_worker_panes import WorkerPaneTests

        for kind in ("unit", "showrunner"):
            with self.subTest(kind=kind):
                fixture = WorkerPaneTests()
                fixture.setUp()
                try:
                    owner = (staff.rename(fixture.parent.id, "TEST-style") if kind == "unit" else
                             fixture_member("showrunner", name="TEST-style", parent=fixture.parent.parent))
                    fixture.parent_id = owner.id
                    names = ("review", "review2", "style-ticket-1493", "held-check-3", "foreign-impl")
                    rows = [fixture.worker(name) for name in names]
                    qualified = fixture.worker("project-TEST-style-test")
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        changed = shutdown.rename_qualified_workers(owner)
                    self.assertTrue(changed)
                    self.assertEqual(errors.getvalue(), "5 worker names unchanged: no qualified prefix for TEST-style\n")
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual([staff.by_id(row.id) for row in rows], rows)
                    self.assertEqual(staff.by_id(qualified.id), replace(qualified, name="test"))
                    before = fixture.staff_file.read_bytes()
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        changed = shutdown.rename_qualified_workers(owner)
                    self.assertFalse(changed)
                    self.assertEqual(errors.getvalue(), "6 worker names unchanged: no qualified prefix for TEST-style\n")
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(fixture.staff_file.read_bytes(), before)
                finally:
                    fixture.doCleanups()

    def test_owner_self_rename_does_not_use_process_scoped_former_names(self) -> None:
        from scripts.herdr.test_worker_panes import WorkerPaneTests

        fixture = WorkerPaneTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        owner = staff.rename(fixture.parent.id, "TEST-widget")
        old_worker = fixture.worker("widget-enhancements-impl")
        live = session(str(owner.current_session), owner.name, os.getpid())
        live["formerNames"] = ["widget-enhancements"]
        live["procStart"] = sessions.process_start(os.getpid()) or ""
        path = sessions.sessions_dir() / f"{os.getpid()}.json"
        write_text_atomic(path, json.dumps(live))
        arguments = ["rename", str(owner.id), owner.name]
        for registered in (True, False):
            with self.subTest(registered=registered):
                if not registered:
                    path.unlink()
                before = fixture.staff_file.read_bytes()
                result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                    dict(os.environ), CHECKOUT)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "1 worker name unchanged: no qualified prefix for TEST-widget\n")
                self.assertEqual(staff.by_id(old_worker.id), old_worker)
                self.assertEqual(fixture.staff_file.read_bytes(), before)

    def test_worker_id_rename_keeps_the_full_pane_label_and_shared_tab(self) -> None:

        owner = fixture_member("showrunner", name="TEST-owner")
        unit = fixture_member("unit", name="TEST-member", parent=owner.id)
        for kind in ("claude", "codex"):
            with self.subTest(kind=kind):
                backend = (staff.ClaudeBackend() if kind == "claude"
                           else staff.CodexBackend(self.root / "codex"))
                row = fixture_worker(f"TEST-{kind}-worker", unit.id, backend=backend)
                name = f"TEST-{kind}-renamed"
                displays = RenameDisplays(row, backend=kind)
                arguments = ["rename", str(row.id), name]
                with displays.installed():
                    result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                        dict(os.environ), CHECKOUT)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(staff.by_id(row.id), replace(row, name=name))
                title = f"{unit.name}-{name}"
                self.assertEqual(displays.commands, [("pane", "rename", "probe-pane", title)])
                self.assertEqual((displays.pane, displays.tab, displays.workspace), (title, "previous", "previous"))

    def test_unavailable_worker_pane_keeps_the_staff_rename_and_reports_one_anomaly(self) -> None:

        owner = fixture_member("showrunner", name="TEST-owner")
        unit = fixture_member("unit", name="TEST-member", parent=owner.id)
        row = fixture_worker("TEST-worker", unit.id)
        arguments = ["rename", str(row.id), "TEST-renamed"]
        with mock.patch.object(herdr.Herdr, "retitle_row", side_effect=herdr_types.HerdrFailure("pane unavailable")):
            result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments, dict(os.environ), CHECKOUT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(staff.by_id(row.id), replace(row, name="TEST-renamed"))
        self.assertEqual(result.stderr.splitlines(), ["anomaly: worker display: pane unavailable"])


@final
class ProductionRenameTests(support.ProductionRenameFixture):
    def test_same_name_keeps_a_pane_with_another_native_session(self) -> None:
        row = self.production_row()
        displays = support.RenameDisplays(row)
        other = herdr.NativeAgentProcess(herdr.EnvironmentThread(fixture_runtime_id("another-live-session")), "probe-generation", {})
        with (displays.installed(), mock.patch.object(herdr, "native_process", return_value=other)):
            result = self.run_command(row.name, row.name)
        self.assert_success(result)
        self.assertEqual(displays.commands, [])
        self.assertEqual((displays.pane, displays.tab, displays.workspace), ("previous",) * 3)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("anomaly:", result.stderr)
        self.assertEqual(staff.by_id(row.id), row)

    def test_same_name_reports_an_unavailable_display_and_succeeds(self) -> None:
        row = self.production_row()
        displays = support.RenameDisplays(row)
        with (displays.installed(), mock.patch.object(herdr.Herdr, "panes",
                side_effect=herdr_types.HerdrFailure("display unavailable"))):
            result = self.run_command(row.name, row.name)
        self.assert_success(result)
        self.assertEqual(result.stderr.strip(), "anomaly: set_displays: display unavailable")
        self.assertEqual(staff.by_id(row.id), row)

    def test_same_name_repairs_displays_without_an_update_timer(self) -> None:
        row = self.production_row()
        (self.notifier / f"showrunner-{row.name}" / "conf").unlink()
        displays = support.RenameDisplays(row)
        with displays.installed():
            result = self.run_command(row.name, row.name)
        self.assert_success(result)
        self.assertEqual((displays.pane, displays.tab, displays.workspace), (row.name,) * 3)
        self.assertEqual(staff.by_id(row.id), row)

    def test_same_name_repairs_the_current_session_and_keeps_an_old_session(self) -> None:
        row = self.production_row()
        current = staff.native_id(fixture_runtime_id("showrunner-session-after-clear"))
        row = staff.update_current_session(row.current_session, current)
        record = sessions.SessionRecord(sessionId=current, name="previous", pid=998041,
            procStart="probe-generation", updatedAt=1, formerNames=[], tmux="", entrypoint="cli", messagingSocketPath="")
        path = self.root / "state/sessions/998041.json"
        old_path = self.root / "state/sessions/998042.json"
        self.write(path, json.dumps(record))
        self.write(old_path, json.dumps({**record, "sessionId": row.id, "pid": 998042}))
        before = old_path.read_bytes(), old_path.stat().st_mtime_ns
        displays = support.RenameDisplays(row)
        displays.pane = displays.tab = displays.workspace = row.name
        with (displays.installed(), mock.patch.object(sessions, "running_session", return_value=True),
              mock.patch.object(session_records, "running_session", return_value=True)):
            self.assert_success(self.run_command(row.name, row.name))
            self.assertEqual(json.loads(path.read_text())["name"], row.name)
            settled = path.read_bytes(), path.stat().st_mtime_ns
            rerun = self.run_command(row.name, row.name)
        self.assert_success(rerun)
        self.assertEqual(rerun.stdout.strip(), "unchanged")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), settled)
        self.assertEqual((old_path.read_bytes(), old_path.stat().st_mtime_ns), before)
        self.assertEqual(staff.by_id(row.id), row)

    def test_same_name_repairs_each_display_and_keeps_the_current_row(self) -> None:
        row = self.production_row()
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("showrunner-after-clear")))
        for field in ("pane", "tab", "workspace"):
            with self.subTest(field=field):
                displays = support.RenameDisplays(row)
                displays.pane = displays.tab = displays.workspace = row.name
                setattr(displays, field, "previous")
                before = self.snapshot()
                with displays.installed():
                    result = self.run_command(row.name, row.name)
                    self.assert_success(result)
                    self.assertNotIn("unchanged", result.stdout)
                    self.assertEqual((displays.pane, displays.tab, displays.workspace), (row.name,) * 3)
                    self.assertEqual(staff.by_id(row.id), row)
                    self.assertEqual(self.snapshot(), before)
                    commands = list(displays.commands)
                    rerun = self.run_command(row.name, row.name)
                self.assert_success(rerun)
                self.assertEqual(rerun.stdout.strip(), "unchanged")
                self.assertEqual(displays.commands, commands)
                self.assertEqual(self.snapshot(), before)

    def test_document_rename_updates_a_wrapped_location_without_reactivating_its_row(self) -> None:
        row = self.production_row()
        _ = staff.set_doc(row.id, staff.WrappedDoc("natedev", self.doc))
        self.assert_success(self.run_command(OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW))
        retained = staff.by_id(row.id)
        assert retained is not None
        self.assertEqual((retained.id, retained.name, retained.parent, retained.doc),
                         (row.id, NEW, row.parent, staff.WrappedDoc("natedev", self.new_doc)))
        before = self.snapshot()
        self.assert_success(self.run_command(OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.snapshot(), before)

    def test_retry_after_doc_move_updates_the_live_doc_on_the_same_staff_id(self) -> None:
        with saved_fixture(self.root) as restore:
            for legacy, collision in ((False, ""), (True, ""),
                                      (False, "source tracked"), (False, "source removed")):
                with self.subTest(legacy=legacy, collision=collision):
                    restore()
                    if collision:
                        self.write(self.new_doc, "An unrelated production document.\n")
                        _ = self.git("add", "--", str(self.new_doc))
                        _ = self.git("commit", "-m", "Keep a separate document")
                        self.doc.unlink()
                        if collision == "source removed":
                            _ = self.git("rm", "--cached", "--", str(self.doc))
                        before = self.snapshot()
                        refused = self.run_command(OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW)
                        self.assertNotEqual(refused.returncode, 0, refused.stdout + refused.stderr)
                        self.assertIn("destination already exists", refused.stderr)
                        self.assertEqual(self.snapshot(), before)
                        continue
                    legacy_files: dict[Path, bytes] = {}
                    if legacy:
                        owner = self.production_row()
                        for name in ("rename.json", "rename_unit.json"):
                            path = self.state / owner.id / name
                            self.write(path, '{"done": false, "step": 7, "old": "obsolete"}\n')
                            legacy_files[path] = path.read_bytes()
                    row = self.production_row()
                    _ = staff.set_doc(row.id, staff.LiveDoc("natedev", self.doc))
                    with mock.patch.object(staff, "set_doc", side_effect=OSError("stopped after doc move")):
                        interrupted = self.run_command(OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW)
                    self.assertIn("anomaly: document_move: stopped after doc move", interrupted.stderr)
                    self.assertFalse(self.doc.exists())
                    self.assertTrue(self.new_doc.is_file())
                    retained = staff.by_id(row.id)
                    assert retained is not None
                    self.assertEqual(retained.doc, staff.LiveDoc("natedev", self.doc))
                    self.assert_success(self.run_command(OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW))
                    renamed = staff.by_id(row.id)
                    assert renamed is not None
                    self.assertEqual(renamed.doc, staff.LiveDoc("natedev", self.new_doc))
                    before = self.snapshot()
                    self.assert_success(self.run_command(OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW))
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual({path: path.read_bytes() for path in legacy_files}, legacy_files)

    def production_row(self) -> staff.StaffRow:
        return next(row for row in staff.find(kind="showrunner") if row.name == OLD)

    def test_bare_showrunner_name_renames_the_showrunner_and_keeps_its_id(self) -> None:
        row = self.production_row()
        row = staff.rename(row.id, "tools")
        self.assert_success(self.run_command("tools", NEW))
        self.assertEqual(staff.by_id(row.id), replace(row, name=NEW))
        self.assertTrue(self.doc.is_file())
        self.assertFalse(self.new_doc.exists())

    def test_taken_staff_name_refuses_before_sources_change(self) -> None:
        _ = fixture_member("showrunner", name=NEW)
        before = self.snapshot()
        failed = self.run_rename()
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertIn("StaffRefused", failed.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_missing_staff_member_refuses_before_sources_change(self) -> None:
        row = self.production_row()
        self.forget_rows(row.id, *(unit.id for unit in staff.snapshot().find(kind="unit", parent=row.id)))
        before = self.snapshot()
        failed = self.run_rename()
        self.assertEqual(failed.returncode, 2, failed.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_document_move_reruns_after_git_move_loses_its_index_update(self) -> None:
        row = self.production_row()
        failed = self.run_command(OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        self.assertFalse(self.doc.exists())
        self.assertTrue(self.new_doc.exists())
        self.assert_success(self.run_command(OLD, NEW, "--to-doc", str(self.new_doc), "--to-branch", NEW))
        production_docs = [path for path in self.git("ls-files").splitlines()
                           if path.endswith("-production.md")]
        self.assertEqual(production_docs, ["docs/plans/new-production.md"])
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.doc, staff.LiveDoc("natedev", self.new_doc))

    def test_document_move_recovery_keeps_partial_staging(self) -> None:
        source = self.doc.relative_to(self.checkout).as_posix()
        destination = self.new_doc.relative_to(self.checkout).as_posix()
        staged = self.doc.read_bytes() + b"\nThe staged document edit.\n"
        working = staged + b"\nThe unstaged document edit.\n"
        _ = self.doc.write_bytes(staged)
        _ = self.git("add", "--", source)
        staged_blob = self.git("rev-parse", f":{source}")
        _ = self.doc.write_bytes(working)

        failed = self.run_command(OLD, NEW, "--to-doc", str(self.new_doc),
                                  "--to-branch", NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        self.assertEqual(self.new_doc.read_bytes(), working)
        self.assertEqual(self.git("rev-parse", f":{source}"), staged_blob)

        self.assert_success(self.run_command(OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.git("rev-parse", f":{destination}"), staged_blob)
        self.assertEqual(self.new_doc.read_bytes(), working)
        self.assertEqual(self.git("ls-files", "--", source), "")

    def test_document_move_reruns_after_the_index_source_is_removed(self) -> None:
        row = self.production_row()
        failed = self.run_command(OLD, NEW, "--to-doc", str(self.new_doc),
                                  "--to-branch", NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        git = unit_move.git

        def interrupted(checkout: Path, *arguments: str) -> str:
            result = git(checkout, *arguments)
            if arguments[0] == "rm":
                raise OSError("stopped after index removal")
            return result

        with mock.patch.object(unit_move, "git", side_effect=interrupted):
            interrupted_move = self.run_command(OLD, NEW,
                "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertIn("stopped after index removal", interrupted_move.stderr)
        self.assertEqual(self.git("ls-files", "--", "docs/plans/old-production.md", "docs/plans/new-production.md"),
                         "docs/plans/new-production.md")
        self.assert_success(self.run_command(OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.git("ls-files", "--", "docs/plans/old-production.md", "docs/plans/new-production.md"),
                         "docs/plans/new-production.md")
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.doc, staff.LiveDoc("natedev", self.new_doc))
        before = self.file_versions()
        self.assert_success(self.run_command(OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.file_versions(), before)

    def test_document_move_keeps_the_blob_indexed_before_source_removal(self) -> None:
        source = self.doc.relative_to(self.checkout).as_posix()
        destination = self.new_doc.relative_to(self.checkout).as_posix()
        staged = self.doc.read_bytes() + b"\nThe staged document edit.\n"
        working = staged + b"\nThe unstaged document edit.\n"
        _ = self.doc.write_bytes(staged)
        self.doc.chmod(0o755)
        _ = self.git("add", "--", source)
        source_entry = self.git("ls-files", "-s", "--", source).split()[:3]
        _ = self.doc.write_bytes(working)
        failed = self.run_command(OLD, NEW, "--to-doc", str(self.new_doc),
                                  "--to-branch", NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        git = unit_move.git

        def interrupted(checkout: Path, *arguments: str) -> str:
            if arguments[0] == "rm":
                raise OSError("stopped before source index removal")
            return git(checkout, *arguments)

        with mock.patch.object(unit_move, "git", side_effect=interrupted):
            interrupted_move = self.run_command(OLD, NEW,
                "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertIn("stopped before source index removal", interrupted_move.stderr)
        self.assertEqual(self.git("ls-files", "-s", "--", destination).split()[:3], source_entry)
        self.assertEqual(self.git("ls-files", "-s", "--", source).split()[:3], source_entry)
        self.assertEqual(self.new_doc.read_bytes(), working)
        self.assert_success(self.run_command(OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.git("ls-files", "-s", "--", destination).split()[:3], source_entry)
        self.assertEqual(self.new_doc.read_bytes(), working)
        self.assertEqual(self.git("ls-files", "--", source), "")


    def test_invalid_optional_move_name_leaves_no_journal_and_allows_a_corrected_request(self) -> None:
        row = self.production_row()
        before = self.file_versions()
        failed = self.run_command(OLD, "../escape",
                                  "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertFalse((self.state / row.id / "rename.json").exists())
        self.assertEqual(self.file_versions(), before)
        self.assert_success(self.run_command(OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertTrue(self.new_doc.exists())

    def test_document_edits_after_journaling_are_kept_and_the_move_finishes(self) -> None:
        row = self.production_row()
        legacy = self.state / row.id / "rename.json"
        self.write(legacy, '{"done": true}\n')
        legacy_bytes = legacy.read_bytes()
        with mock.patch.object(staff, "rename", side_effect=OSError("stopped after journal")):
            interrupted = self.run_command(OLD, NEW,
                "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertNotEqual(interrupted.returncode, 0)
        self.assertTrue((self.state / row.id / "rename.json").exists())
        content = self.doc.read_bytes() + b"\nA new document edit.\n"
        _ = self.doc.write_bytes(content)
        result = self.run_command(OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assert_success(result)
        self.assertTrue(self.new_doc.exists(), result.stderr)
        self.assertEqual(self.new_doc.read_bytes(), content)
        self.assertFalse(self.doc.exists())
        self.assertEqual(legacy.read_bytes(), legacy_bytes)
        self.assertEqual(result.stderr, "")
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.doc, staff.LiveDoc("natedev", self.new_doc))
        self.assertEqual(self.git("branch", "--show-current"), NEW)
        before = self.file_versions()
        self.assert_success(self.run_command(OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.file_versions(), before)

    def test_unit_reference_cannot_move_an_existing_showrunners_document(self) -> None:
        row = self.production_row()
        unit = fixture_member("unit", name="requested", parent=row.id)
        before = self.file_versions()
        result = self.run_command(unit.id, row.name,
            "--to-doc", str(self.new_doc), "--to-branch", NEW)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not a showrunner", result.stderr)
        self.assertEqual(self.file_versions(), before)
        self.assertEqual(staff.by_id(row.id), row)

    def file_versions(self) -> dict[Path, tuple[bytes, int]]:
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file() and path.name != "herdr.jsonl"}

    def test_rename_changes_the_row_session_tab_and_workspace_without_a_move(self) -> None:
        row = self.production_row()
        record = sessions.SessionRecord(sessionId=row.current_session, name=OLD, pid=998041,
            procStart="probe-generation", updatedAt=1, formerNames=[], tmux="", entrypoint="cli", messagingSocketPath="")
        path = self.root / "state/sessions/998041.json"
        self.write(path, json.dumps(record))
        document = self.doc.read_bytes(), self.doc.stat().st_mtime_ns
        branch = self.git("branch", "--show-current")
        tip = self.git("rev-parse", "HEAD")
        displays = support.RenameDisplays(row)
        displays.pane = displays.tab = displays.workspace = OLD
        with (displays.installed(), mock.patch.object(sessions, "running_session", return_value=True),
              mock.patch.object(session_records, "running_session", return_value=True)):
            self.assert_success(self.run_rename())
        self.assertEqual(staff.by_id(row.id), replace(row, name=NEW))
        self.assertEqual((displays.pane, displays.tab, displays.workspace), (NEW,) * 3)
        self.assertEqual(json.loads(path.read_text())["name"], NEW)
        self.assertEqual((self.doc.read_bytes(), self.doc.stat().st_mtime_ns), document)
        self.assertFalse(self.new_doc.exists())
        self.assertEqual((self.git("branch", "--show-current"), self.git("rev-parse", "HEAD")), (branch, tip))

    def test_same_name_is_unchanged_when_every_name_agrees(self) -> None:
        row = self.production_row()
        record = sessions.SessionRecord(sessionId=row.current_session, name=row.name, pid=998041,
            procStart="probe-generation", updatedAt=1, formerNames=[], tmux="", entrypoint="cli", messagingSocketPath="")
        self.write(self.root / "state/sessions/998041.json", json.dumps(record))
        displays = support.RenameDisplays(row)
        displays.pane = displays.tab = displays.workspace = row.name
        before = self.file_versions()
        with (displays.installed(), mock.patch.object(sessions, "running_session", return_value=True),
              mock.patch.object(session_records, "running_session", return_value=True)):
            result = self.run_command(row.name, row.name)
        self.assert_success(result)
        self.assertEqual(result.stdout.strip(), "unchanged")
        self.assertEqual(displays.commands, [])
        self.assertEqual(self.file_versions(), before)

    def test_to_doc_sets_the_row_after_the_file_move_and_keeps_the_document_text(self) -> None:
        row = self.production_row()
        content = self.doc.read_bytes()
        set_doc = staff.set_doc

        def publish(identity: staff.StaffId, doc: staff.LiveDoc | staff.WrappedDoc) -> staff.StaffRow:
            self.assertEqual(identity, row.id)
            self.assertFalse(self.doc.exists())
            self.assertEqual(self.new_doc.read_bytes(), content)
            current = staff.by_id(identity)
            assert current is not None
            self.assertEqual(current.doc, row.doc)
            return set_doc(identity, doc)

        with mock.patch.object(staff, "set_doc", side_effect=publish) as published:
            self.assert_success(self.run_command(OLD, NEW,
                "--to-doc", str(self.new_doc), "--to-branch", NEW))
        published.assert_called_once_with(row.id, staff.LiveDoc("natedev", self.new_doc))
        self.assertEqual(staff.by_id(row.id), replace(row, name=NEW, doc=staff.LiveDoc("natedev", self.new_doc)))
        self.assertEqual(self.git("branch", "--show-current"), NEW)
        before = self.file_versions()
        self.assert_success(self.run_command(OLD, NEW,
            "--to-doc", str(self.new_doc), "--to-branch", NEW))
        self.assertEqual(self.file_versions(), before)


    def test_finished_rename_rerun_changes_no_bytes_or_times(self) -> None:
        row = self.production_row()
        displays = support.RenameDisplays(row)
        displays.pane = displays.tab = displays.workspace = OLD
        with displays.installed():
            self.assert_success(self.run_rename())
            before = self.file_versions()
            commands = list(displays.commands)
            result = self.run_rename()
        self.assert_success(result)
        self.assertEqual(result.stdout.strip(), "unchanged")
        self.assertEqual(self.file_versions(), before)
        self.assertEqual(displays.commands, commands)


    def test_rename_preserves_staff_stores_and_parent_relationships(self) -> None:
        row = self.production_row()
        child = fixture_member("showrunner", name="child", parent=row.id)
        listed = self.root / "lists/change-review - old.md"
        rows = ["| # | Change | Merged | How to check | Production |",
                "| --- | --- | --- | --- | --- |",
                f"| C1 | own | m | try | {row.id} |",
                f"| C2 | child's | m | try | {child.id} |",
                f"| C3 | root's | m | try | {row.id} |"]
        self.write(listed, "\n".join(rows) + "\n")
        pins = self.home / ".local/state/next"
        question = {"current": f"{row.id}: first",
                    "ended": {f"{row.id}: earlier": "2026-10-27T01:00:00"},
                    "moved": ["oldish: kept"]}
        pin = pins / f"question-{row.id}.json"
        aggregate = pins / f"next-{row.id}-all-below.json"
        self.write(pin, json.dumps(question))
        self.write(aggregate, json.dumps({"current": "C1", "source": "change-old"}))
        bevy = self.home / ".local/state/bevy-upgrade"
        watch = bevy / f"{row.id}.json"
        lock = bevy / f"{row.id}.lock"
        job = self.notifier / f"bevy-upgrade-{row.id}/conf"
        self.write(watch, json.dumps({"staff_id": row.id, "entries": 3}))
        self.write(lock, "")
        self.write(job, f"EVERY=240\nRUN=py bevy_upgrade.py --staff-id {row.id} check --scheduled\n"
                       + f"FROM=bevy-upgrade-{row.id}\n")
        run = self.root / "runs/current/progress_history_state.json"
        self.write(run, json.dumps({"owner_staff_id": row.id}))
        fixed = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in (listed, pin, aggregate, watch, lock, job, run)}
        displays = support.RenameDisplays(row)
        with displays.installed():
            self.assert_success(self.run_rename())
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in fixed}, fixed)
        self.assertEqual(json.loads(pin.read_text()), question)
        self.assertEqual(json.loads(aggregate.read_text()), {"current": "C1", "source": "change-old"})
        for content in rows[2:]:
            self.assertIn(content, listed.read_text())
        self.assertEqual(json.loads(watch.read_text()), {"staff_id": row.id, "entries": 3})
        self.assertEqual(lock.read_text(), "")
        self.assertIn(f"--staff-id {row.id} check", job.read_text())
        self.assertIn(f"FROM=bevy-upgrade-{row.id}\n", job.read_text())
        self.assertEqual(json.loads(run.read_text()), {"owner_staff_id": row.id})
        self.assertEqual(staff.by_id(row.id), replace(row, name=NEW))
        self.assertEqual(staff.by_id(child.id), child)
        self.assertEqual(child.parent, staff.ReportsTo(row.id))

    def test_invalid_staff_name_refuses_without_writes(self) -> None:
        before = self.snapshot()
        result = self.run_rename("../escape")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("../escape", result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)


    def test_document_move_ignores_an_incomplete_legacy_staged_blob(self) -> None:
        row = self.production_row()
        failed = self.run_command(OLD, NEW, "--to-doc", str(self.new_doc),
                                  "--to-branch", NEW, wrapper=FAIL_GIT_MOVE_INDEX)
        self.assertIn("simulated git mv index failure", failed.stderr)
        journal = self.state / row.id / "rename.json"
        with saved_fixture(self.root) as restore:
            for blob in ({}, {"mode": 100644, "object_id": "blob"},
                         {"mode": "100644", "object_id": 7},
                         {"mode": "", "object_id": ""}, None):
                with self.subTest(blob=blob):
                    restore()
                    self.write(journal, json.dumps({"staged_blob": blob}))
                    legacy = journal.read_bytes()
                    self.assert_success(self.run_command(OLD, NEW,
                        "--to-doc", str(self.new_doc), "--to-branch", NEW))
                    self.assertEqual(journal.read_bytes(), legacy)
                    self.assertEqual(staff.by_id(row.id), replace(row, name=NEW,
                        doc=staff.LiveDoc("natedev", self.new_doc)))
                    before = self.file_versions()
                    self.assert_success(self.run_command(OLD, NEW,
                        "--to-doc", str(self.new_doc), "--to-branch", NEW))
                    self.assertEqual(self.file_versions(), before)

    def test_document_move_plan_is_proved_before_the_row_changes(self) -> None:
        row = self.production_row()
        rename = staff.rename
        plan = shutdown.rename_plan
        document = self.doc.read_bytes()
        requests: list[shutdown.RenamePlan] = []

        def read_plan(old: str, new: str, *, to_doc: str = "", to_branch: str = "") -> shutdown.RenamePlan:
            request = plan(old, new, to_doc=to_doc, to_branch=to_branch)
            requests.append(request)
            return request

        def rename_row(identity: staff.StaffId, name: str) -> staff.StaffRow:
            self.assertEqual(len(requests), 1)
            request = requests[0]
            self.assertEqual(request.member.id, row.id)
            self.assertIsInstance(request.move, shutdown.DocBranchMove)
            assert isinstance(request.move, shutdown.DocBranchMove)
            self.assertEqual(request.move.source, self.doc)
            self.assertEqual(request.move.destination, self.new_doc)
            self.assertEqual(request.move.branch, NEW)
            self.assertFalse((self.state / row.id / "rename.json").exists())
            self.assertEqual(staff.by_id(row.id), row)
            self.assertEqual(self.doc.read_bytes(), document)
            self.assertFalse(self.new_doc.exists())
            self.assertEqual(self.git("branch", "--show-current"), OLD)
            return rename(identity, name)

        with (mock.patch.object(shutdown, "rename_plan", side_effect=read_plan),
              mock.patch.object(staff, "rename", side_effect=rename_row) as renamed):
            self.assert_success(self.run_command(OLD, NEW,
                "--to-doc", str(self.new_doc), "--to-branch", NEW))
        renamed.assert_called_once_with(row.id, NEW)


@final
class ProductionRenameBranchTests(support.ProductionRenameFixture):
    def test_a_merge_branch_not_named_for_the_production_is_kept(self) -> None:
        _ = self.git("branch", "-m", "merge-main")
        _ = self.git("branch", OLD)
        _ = self.git("push", "origin", "merge-main")
        tip = self.git("rev-parse", "HEAD")
        document = self.doc.read_bytes(), self.doc.stat().st_mtime_ns
        self.assert_success(self.run_rename())
        self.assertEqual(self.git("branch", "--show-current"), "merge-main")
        self.assertEqual(self.git("for-each-ref", "--format=%(refname)", f"refs/heads/{NEW}"), "")
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{NEW}"), "")
        self.assertEqual(self.git("rev-parse", "HEAD"), tip)
        self.assertEqual(self.git("rev-parse", "merge-main"), self.git("rev-parse", "origin/merge-main"))
        self.assertEqual((self.doc.read_bytes(), self.doc.stat().st_mtime_ns), document)
        self.assertFalse(self.new_doc.exists())
        self.assertEqual(self.git("rev-parse", OLD), tip)
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/old"), f"{tip}\trefs/heads/old")


@final
class ProductionRenameRerunTests(ProductionRenameFixture):
    def test_rename_rerun(self) -> None:
        self.env.update(GIT_AUTHOR_DATE="2026-10-29T12:00:00Z", GIT_COMMITTER_DATE="2026-10-29T12:00:00Z")
        with saved_fixture(self.root) as restore:
            self.assert_success(self.run_rename())
            expected = self.outcome()
            for after in (False, True):
                with self.subTest(after=after):
                    restore()

                    with stop_at_staff_write("rename", after=after):
                        failed = self.run_rename()
                    self.assertNotEqual(failed.returncode, 0)
                    self.assertIn("stopped at staff step", failed.stderr)
                    self.assert_success(self.run_rename())
                    self.assertEqual(self.outcome(), expected)
                    finished = self.run_rename()
                    self.assert_success(finished)
                    self.assertIn("unchanged", finished.stdout)
                    self.assertEqual(self.outcome(), expected)

    def outcome(self) -> tuple[dict[str, bytes], str, str, str]:
        return (final_files(self.root), self.git("show-ref"), self.git("status", "--porcelain"),
                self.git("--git-dir", str(self.origin), "show-ref"))


@final
class UnitRenameTests(UnitRenameFixture):
    send_rename_notice = staticmethod(shutdown.rename_send_notice)

    @override
    def setUp(self) -> None:
        super().setUp()
        bind_private_run_roots(self, self.root)
        _ = self.enterContext(mock.patch.object(shutdown, "rename_send_notice"))

    def test_unit_name_alias_migrates_workers_without_a_folder_or_branch_move(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        folder = self.rust / "claude-demo-alpha"
        worker = fixture_worker("demo-alpha-review", row.id)
        backend = FolderSessionFixture(row, live=True)
        with (backend.installed(), RenameDisplays(row).installed(),
              mock.patch.object(herdr.Herdr, "agents", return_value=[])):
            result = self.invoke(["rename", "alpha", "alpha-unit"])
        self.assert_ok(result)
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(staff.by_id(worker.id), replace(worker, name="review"))
        self.assertIsNone(unit_move.pending_rename(row.id))
        self.assertTrue(folder.is_dir())
        self.assertFalse(folder.with_name("claude-demo-alpha-unit").exists())
        self.assertEqual(self.git("branch", "--show-current", cwd=folder), "demo-alpha")
        self.assertEqual((backend.starts, backend.stops), ([], []))

    def test_rename_refuses_the_reserved_suffix_in_the_plan_and_apply(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        path = f"demo/{row.name}-unit"
        before = file_versions(self.root)
        self.assertEqual(shutdown.rename_plan(path, row.name).member, row)
        with self.subTest(stage="plan"):
            with self.assertRaisesRegex(staff.StaffRefused, "^unit name 'x-unit' must not end in -unit$"):
                _ = shutdown.rename_plan(path, "x-unit")
            self.assertEqual(file_versions(self.root), before)
        with self.subTest(stage="apply"):
            result = self.rename(old=f"{row.name}-unit", new="x-unit")
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (2, "", "StaffRefused: unit name 'x-unit' must not end in -unit\n"))
            self.assertEqual(file_versions(self.root), before)

    def test_explicit_branch_change_keeps_the_staff_row(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        folder = self.rust / "claude-demo-alpha"
        branch = "cargo-liner-handler"
        result = self.invoke(["rename", "alpha", "alpha", "--to-branch", branch])
        self.assert_ok(result)
        self.assertEqual(self.git("branch", "--show-current", cwd=folder), branch)
        self.assertEqual(staff.by_id(row.id), row)

    def test_unit_rename_refuses_a_reserved_name_before_changing_files_or_displays(self) -> None:
        unit = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        producer = fixture_member("producer", name="fixture-producer")
        plan = staff.ProducerRename("1" * 32, producer, replace(producer, name="gamma"))
        with mock.patch.object(staff, "producer_creation_registry",
                               return_value=staff.ProducerCreationRegistryStatus.ABSENT):
            staff.reserve_producer_rename(plan)
        try:
            before = file_versions(self.root)
            with mock.patch.object(worker_panes, "rename_workers") as displays:
                result = self.rename()
            self.assertEqual((result.returncode, result.stdout), (2, ""))
            self.assertIn("finish the producer rename to gamma", result.stderr)
            displays.assert_not_called()
            self.assertEqual(file_versions(self.root), before)
            self.assertEqual(staff.by_id(unit.id), unit)
            self.assertIsNone(unit_move.pending_rename(unit.id))
        finally:
            staff.release_producer_rename(plan.operation)

    def test_rename_replaces_the_old_suffix_when_it_also_matches_the_new_name(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        row = staff.rename(row.id, "demo-alpha")
        folder = self.rust / "claude-demo-alpha"
        destination = self.rust / "claude-alpha"
        _ = self.git("branch", "-m", "topic-demo-alpha", cwd=folder)

        with FolderSessionFixture(row, live=False).installed(), RenameDisplays(row).installed():
            result = self.invoke(["rename", str(row.id), "alpha"])

        self.assert_ok(result)
        self.assertEqual(result.stderr, "")
        self.assertFalse(folder.exists())
        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "topic-alpha")
        self.assertEqual(staff.by_id(row.id), replace(row, name="alpha",
            worktree=staff.PresentWorktree("natedev", destination)))

    def test_review_push_uses_an_explicit_refspec_for_the_move_and_retry(self) -> None:
        from scripts.production import unit_move

        folder = self.rust / "claude-demo-alpha"
        branch = "cargo-liner-handler"
        with saved_fixture(self.root) as restore:
            for recovering in (False, True):
                with self.subTest(recovering=recovering):
                    restore()
                    _ = self.git("config", "remote.origin.push", f"refs/heads/{branch}:refs/heads/unwanted")
                    if recovering:
                        _ = self.git("branch", "-m", branch, cwd=folder)
                    git = unit_move.git
                    pushes: list[tuple[str, ...]] = []

                    def record(checkout: Path, *arguments: str) -> str:
                        if arguments[:1] == ("push",):
                            pushes.append(arguments)
                        return git(checkout, *arguments)

                    with mock.patch.object(unit_move, "git", side_effect=record):
                        result = self.invoke(["rename", "alpha", "alpha", "--to-branch", branch])
                    self.assert_ok(result)
                    self.assertEqual(pushes, [("push", "--set-upstream", "origin", f"{branch}:refs/heads/{branch}")])
                    self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", branch),
                                     self.git("rev-parse", "HEAD", cwd=folder))
                    self.assertEqual(self.git("--git-dir", str(self.origin), "for-each-ref", "refs/heads/unwanted"), "")

    def test_name_change_pushes_the_branch_with_its_new_suffix(self) -> None:
        folder = self.rust / "claude-demo-alpha"
        tip = self.git("rev-parse", "HEAD", cwd=folder)
        self.assert_ok(self.rename())
        destination = self.rust / "claude-demo-gamma"
        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "demo-gamma")
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "demo-gamma"), tip)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=destination), "origin/demo-gamma")

    def test_to_branch_keeps_the_unit_name_folder_and_live_session(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        folder = self.rust / "claude-demo-alpha"
        tip = self.git("rev-parse", "HEAD", cwd=folder)
        self.write(self.home / f".claude/projects/fixture/{row.current_session}.jsonl", "{}\n")
        session = FolderSessionFixture(row, live=True)
        with session.installed(), RenameDisplays(row).installed():
            self.assert_ok(self.invoke(["rename", "alpha", "alpha", "--to-branch", "cargo-liner-handler"]))
        self.assertEqual(staff.by_id(row.id), row)
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.git("branch", "--show-current", cwd=folder), "cargo-liner-handler")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=folder), tip)
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "cargo-liner-handler"), tip)
        self.assertEqual(session.stops, [])
        self.assertEqual(session.starts, [])
        self.assertTrue(session.live)

    def test_to_branch_with_a_name_change_moves_the_folder_suffix_and_preserves_work(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        folder = self.rust / "claude-demo-alpha"
        destination = self.rust / "claude-demo-gamma"
        tip = self.git("rev-parse", "HEAD", cwd=folder)
        self.write(folder / "draft.md", "Keep the draft.\n")
        self.assert_ok(self.invoke(["rename", "alpha", "gamma", "--to-branch", "cargo-liner-handler"]))
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual((renamed.name, renamed.worktree), ("gamma", staff.PresentWorktree("natedev", destination)))
        self.assertFalse(folder.exists())
        self.assertEqual((destination / "draft.md").read_text(), "Keep the draft.\n")
        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "cargo-liner-handler")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=destination), tip)
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "cargo-liner-handler"), tip)


    def test_to_branch_refuses_an_existing_target_for_units_and_showrunners(self) -> None:
        for arguments in (["alpha", "alpha"], ["demo", "demo", "--to-doc", str(self.doc)]):
            with self.subTest(arguments=arguments):
                before = file_versions(self.root)
                result = self.invoke(["rename", *arguments, "--to-branch", "demo-mul-add"])
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("branch already exists: demo-mul-add", result.stderr)
                self.assertEqual(file_versions(self.root), before)

    def test_to_branch_and_suffix_rename_reruns_finish_an_interrupted_push(self) -> None:
        from scripts.production import unit_move

        with saved_fixture(self.root) as restore:
            for name, branch, options in (("alpha", "cargo-liner-handler", ["--to-branch", "cargo-liner-handler"]),
                                          ("gamma", "cargo-liner-handler", ["--to-branch", "cargo-liner-handler"]),
                                          ("gamma", "demo-gamma", [])):
                with self.subTest(name=name, branch=branch):
                    restore()
                    row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
                    displays = RenameDisplays(row)
                    arguments = ["rename", "alpha", name, *options]
                    git = unit_move.git

                    def interrupted(checkout: Path, *arguments: str) -> str:
                        if arguments[:1] == ("push",):
                            raise OSError("The push stopped.")
                        return git(checkout, *arguments)

                    with displays.installed():
                        with mock.patch.object(unit_move, "git", side_effect=interrupted):
                            failed = self.invoke(arguments)
                        self.assertNotEqual(failed.returncode, 0)
                        self.assertIn("The push stopped.", failed.stderr)
                        destination = self.rust / f"claude-demo-{name}"
                        self.assertEqual(self.git("branch", "--show-current", cwd=destination), branch)
                        self.assert_ok(self.invoke(arguments))
                        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", branch),
                                         self.git("rev-parse", "HEAD", cwd=destination))
                        before = file_versions(self.root)
                        rerun = self.invoke(arguments)
                        self.assert_ok(rerun)
                        self.assertEqual(rerun.stdout.strip(), "unchanged")
                        self.assertEqual(file_versions(self.root), before)

    def test_to_branch_does_not_allow_a_unit_document_move(self) -> None:
        before = file_versions(self.root)
        result = self.invoke(["rename", "alpha", "alpha", "--to-doc", str(self.doc)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--to-doc is only for showrunners", result.stderr)
        self.assertEqual(file_versions(self.root), before)

    def test_bare_unit_name_renames_the_unit_folder_and_branch(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        with RenameDisplays(row).installed():
            self.assert_ok(self.invoke(["rename", "alpha", "gamma"]))
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.name, "gamma")
        self.assertEqual(renamed.worktree, staff.PresentWorktree("natedev", self.rust / "claude-demo-gamma"))
        self.assertEqual(self.git("branch", "--show-current", cwd=self.rust / "claude-demo-gamma"), "demo-gamma")

    def test_rename_waits_for_a_named_output_and_keeps_a_held_numbered_copy(self) -> None:
        self.assert_managed_unit_rename(hold_numbered_copy=True)

    def test_idle_rename_moves_the_worktree_and_existing_output(self) -> None:
        self.assert_managed_unit_rename(hold_numbered_copy=False)

    def assert_managed_unit_rename(self, *, hold_numbered_copy: bool) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        source_root, build_root = self.home / "worktrees", self.root / "build"
        self.write(Path(self.env["CONF_DIR"]) / "build_folders.conf",
                   f"roots.workspace={source_root}\nroots.build={build_root}\nroots.scratch={self.root / 'scratch'}\n"
                   + "# range: 0..1\nlocks.probe_wait_seconds=0\n"
                   + "# range: 0.001..1\nlocks.probe_retry_seconds=0.01\n")
        self.env["WORKTREES_STATE_DIR"] = str(self.root / "worktree-records")
        source = source_root / "origin/alpha/director"
        source.parent.mkdir(parents=True)
        _ = self.git("worktree", "move", str(self.rust / "claude-demo-alpha"), str(source))
        _ = self.git("branch", "-m", "alpha-director", cwd=source)
        other = source.with_name("review")
        _ = self.git("worktree", "add", "-b", "alpha-review", str(other))
        output = build_root / "origin/alpha"
        self.write(output / "director/artifact", "director")
        self.write(output / "review/artifact", "review")
        self.env["COPIES_CONFIG"] = str(Path(self.env["CONF_DIR"]) / "copies.conf")
        with mock.patch.dict(os.environ, self.env, clear=True):
            with build_slots.take_copy(self.checkout, "HEAD", showrunner="demo", unit="alpha",
                                       work="rename", purpose="fixture") as copy:
                self.assertEqual(copy.path, output / "1")
                copy_git = git_files.at(copy.path).git_dir
                self.write(copy.path / "target/artifact", "numbered copy")
        copy_lock = output / "1.lock"
        for checkout in (source, other):
            (checkout / "target").symlink_to(output / checkout.name)
        alias = source_root / "alpha-legacy"
        internal = source.with_name("legacy")
        internal.symlink_to(source, target_is_directory=True)
        alias.symlink_to(internal, target_is_directory=True)
        with mock.patch.dict(os.environ, self.env):
            record = worktrees.new_record(source, "alpha-director", worktrees.main_checkout(source), "base",
                                          "alpha", "demo")
            worktrees.write_record(replace(record, layout=worktrees.output_record("origin", "alpha", "director")))
            identity = worktrees.FolderIdentity.read(source)
            for former, target, stage in ((internal, source, worktrees.MoveStage.PLANNED),
                                          (alias, internal, worktrees.MoveStage.FINISHED)):
                moved = worktrees.WorktreeMove(former, target, "origin", "alpha", "director",
                                               worktrees.MoveOutput.LIVE_UNIT)
                worktrees.write_move(worktrees.MoveJournal(
                    moved, identity, identity, "", git_files.at(source).git_dir, (), stage))
        row = staff.set_worktree(row.id, staff.PresentWorktree("natedev", source), expected=row.worktree)
        destination = source_root / "origin/gamma/director"
        new_output = build_root / "origin/gamma"
        finish_rename = unit_move.finish_rename

        def finish_with_lock(current: staff.StaffRow, pending: unit_move.PendingRename) -> bool:
            journal = unit_move.rename_move_path(current.id)
            self.assertTrue(journal.is_file())
            finished = finish_rename(current, pending)
            self.assertTrue(finished)
            self.assertFalse(journal.exists())
            with self.assertRaises(BlockingIOError), build_folders.unit_allocation_lock(output, exclusive=False):
                self.fail("Copy allocation acquired the old unit lock during rename.")
            return finished

        with (RenameDisplays(row).installed(),
              mock.patch.object(unit_move, "finish_rename", side_effect=finish_with_lock) as finish):
            if hold_numbered_copy:
                with (output / "director.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    blocked = self.invoke(["rename", "alpha", "gamma"])
                    self.assertEqual(blocked.returncode, 1)
                    self.assertIn("Rerun when idle", blocked.stderr)
                    self.assertTrue(source.is_dir())
                    self.assertFalse(new_output.exists())
                    self.assertTrue((copy.path / "target/artifact").is_file())
            with copy_lock.open("r+") as lock:
                if hold_numbered_copy:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = self.invoke(["rename", "alpha", "gamma"])
            self.assert_ok(result)
            self.assertIn(f"kept for a manual removal list: {copy.path}", result.stderr)
            finish.assert_called_once()
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual(renamed.worktree, staff.PresentWorktree("natedev", destination))
        self.assertEqual(list(source.parent.iterdir()), [])
        self.assertEqual((copy.path / "target/artifact").read_text(), "numbered copy")
        self.assertTrue(copy_lock.is_file())
        self.assertTrue((output / "1.lease.json").is_file())
        self.assertFalse((output / "director").exists())
        self.assertFalse((output / "review").exists())
        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "gamma-director")
        moved_review = destination.with_name("review")
        self.assertEqual(self.git("branch", "--show-current", cwd=moved_review), "alpha-review")
        for checkout in (destination, moved_review):
            self.assertEqual((checkout / "target").readlink(), new_output / checkout.name)
            self.assertEqual((checkout / "target/artifact").read_text(), checkout.name)
        new_internal = destination.with_name(internal.name)
        self.assertEqual(alias.readlink(), new_internal)
        self.assertEqual(new_internal.readlink(), destination)
        self.assertEqual(alias.resolve(), destination)
        self.assertFalse((new_output / "1").exists())
        self.assertFalse((new_output / copy_lock.name).exists())
        self.assertEqual(git_files.at(copy.path).git_dir, copy_git)
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(worktrees.read_record(worktrees.record_file(destination)).branch, "gamma-director")
            journal = worktrees.read_move(worktrees.move_file(alias))
            self.assertEqual((journal.move.new, journal.move.unit), (new_internal, "gamma"))
            self.assertFalse(worktrees.move_file(internal).exists())
            journal = worktrees.read_move(worktrees.move_file(new_internal))
            self.assertEqual((journal.move.old, journal.move.new), (new_internal, destination))
            self.assertEqual(journal.stage, worktrees.MoveStage.PLANNED)
            self.assertEqual(worktrees.worktree_output(destination),
                             worktrees.output_record("origin", "gamma", "director"))
            self.assertFalse(worktrees.record_file(source).exists())

    def test_bare_unit_rename_rerun_keeps_the_callers_unit_and_leaves_the_foreign_unit_unchanged(self) -> None:
        parent = next(row for row in staff.find(kind="showrunner") if row.name == "demo")
        own = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        foreign_parent = fixture_member("showrunner", name="foreign")
        foreign = fixture_member("unit", name="alpha", parent=foreign_parent.id)
        self.env["CLAUDE_CODE_SESSION_ID"] = parent.current_session
        with RenameDisplays(own).installed():
            self.assert_ok(self.invoke(["rename", "alpha", "gamma"]))
            before = final_files(self.root)
            rerun = self.invoke(["rename", "alpha", "gamma"])
            self.assert_ok(rerun)
            self.assertEqual(rerun.stdout.strip(), "unchanged")
            self.assertEqual(final_files(self.root), before)
        renamed = staff.by_id(own.id)
        assert renamed is not None
        self.assertEqual(renamed.name, "gamma")
        self.assertEqual(staff.by_id(foreign.id), foreign)

    def test_bare_unit_name_with_two_matches_outside_the_caller_requires_a_path(self) -> None:
        caller = next(row for row in staff.find(kind="showrunner") if row.name == "demo")
        for name in ("first", "second"):
            parent = fixture_member("showrunner", name=name)
            _ = fixture_member("unit", name="duplicate", parent=parent.id)
        self.env["CLAUDE_CODE_SESSION_ID"] = caller.current_session
        before = final_files(self.root)
        result = self.invoke(["rename", "duplicate", "renamed"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate: several rows match; use showrunner/name or the staff id", result.stderr)
        self.assertEqual(final_files(self.root), before)

    def test_same_name_repairs_displays_with_an_unfilled_worktree(self) -> None:
        parent = next(row for row in staff.find(kind="showrunner") if row.name == "demo")
        row = fixture_member("unit", name="unfilled", parent=parent.id)
        displays = RenameDisplays(row)
        with displays.installed():
            result = self.rename(row.name, row.name)
        self.assert_ok(result)
        self.assertEqual((displays.pane, displays.tab), (row.name,) * 2)
        self.assertEqual(staff.by_id(row.id), row)

    def test_same_name_repairs_the_current_session_name_without_moving(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("unit-session-after-clear")))
        record = sessions.SessionRecord(sessionId=row.current_session, name="previous", pid=998041,
            procStart="probe-generation", updatedAt=1, formerNames=[], tmux="", entrypoint="cli", messagingSocketPath="")
        path = self.root / "state/sessions/998041.json"
        self.write(path, json.dumps(record))
        displays = RenameDisplays(row)
        displays.pane = displays.tab = row.name
        before = final_files(self.root)
        with (displays.installed(), mock.patch.object(sessions, "running_session", return_value=True),
              mock.patch.object(session_records, "running_session", return_value=True)):
            self.assert_ok(self.rename(row.name, row.name))
            self.assertEqual(json.loads(path.read_text())["name"], row.name)
            settled = path.read_bytes(), path.stat().st_mtime_ns
            rerun = self.rename(row.name, row.name)
        self.assert_ok(rerun)
        self.assertEqual(rerun.stdout.strip(), "unchanged")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), settled)
        self.assertEqual(staff.by_id(row.id), row)
        self.assertEqual(final_files(self.root), before)

    def test_same_name_repairs_each_display_without_moving_the_worktree(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        row = staff.update_current_session(row.current_session, staff.native_id(fixture_runtime_id("unit-after-clear")))
        for field in ("pane", "tab"):
            with self.subTest(field=field):
                displays = RenameDisplays(row)
                displays.pane = displays.tab = row.name
                setattr(displays, field, "previous")
                before = final_files(self.root)
                with displays.installed():
                    result = self.rename(row.name, row.name)
                    self.assert_ok(result)
                    self.assertNotIn("unchanged", result.stdout)
                    self.assertEqual((displays.pane, displays.tab), (row.name, row.name))
                    self.assertEqual(staff.by_id(row.id), row)
                    self.assertEqual(final_files(self.root), before)
                    commands = list(displays.commands)
                    rerun = self.rename(row.name, row.name)
                self.assert_ok(rerun)
                self.assertEqual(rerun.stdout.strip(), "unchanged")
                self.assertEqual(displays.commands, commands)
                self.assertEqual(final_files(self.root), before)

    def test_rename_continues_when_the_removed_worktree_is_absent(self) -> None:
        with saved_fixture(self.root) as restore:
            for branch in (staff.ParkedBranch("demo-alpha"), staff.BranchUnrecorded()):
                with self.subTest(branch=branch), ExitStack() as scope:
                    _ = scope.callback(restore)
                    _ = scope.enter_context(mock.patch.object(herdr.Herdr, "panes", return_value=[]))
                    row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
                    source = self.rust / "claude-demo-alpha"
                    _ = self.git("worktree", "remove", str(source))
                    _ = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", source, branch), expected=row.worktree)
                    result = self.rename()
                    self.assert_ok(result)
                    destination = self.rust / "claude-demo-gamma"
                    renamed = staff.by_id(row.id)
                    assert renamed is not None
                    self.assertEqual((renamed.name, renamed.worktree),
                                     ("gamma", staff.RemovedWorktree("natedev", destination, branch)))
                    self.assertFalse(destination.exists())
                    self.assertIn(f"anomaly: folder is absent: {destination}", result.stderr.splitlines())
                    self.assertIsNone(unit_move.pending_rename(row.id))
                    before = final_files(self.root)
                    retry = self.rename()
                    self.assert_ok(retry)
                    self.assertEqual(retry.stdout.strip(), "unchanged")
                    self.assertEqual(retry.stderr, "")
                    self.assertEqual(final_files(self.root), before)
                    result = self.invoke(["rename", str(row.id), "omega"])
                    self.assert_ok(result)
                    destination = self.rust / "claude-demo-omega"
                    renamed = staff.by_id(row.id)
                    assert renamed is not None
                    self.assertEqual((renamed.name, renamed.worktree),
                                     ("omega", staff.RemovedWorktree("natedev", destination, branch)))
                    self.assertFalse(destination.exists())
                    self.assertIsNone(unit_move.pending_rename(row.id))
                    moved = self.rust / "moved-omega"
                    arguments = ["move-folder", "--unit", str(row.id), "--to", str(moved)]
                    result = invoke_cli(unit_move, lambda: unit_move.main(arguments), arguments, self.env, self.checkout)
                    self.assert_ok(result)
                    self.assertEqual(staff.by_id(row.id), replace(renamed,
                        worktree=staff.RemovedWorktree("natedev", moved, branch)))
                    self.assertFalse(moved.exists())
                    self.assertIsNone(unit_move.pending_rename(row.id))

    def test_worktree_rename_updates_a_removed_location_without_reactivating_its_row(self) -> None:
        with saved_fixture(self.root) as restore:
            for branch in (staff.BranchUnrecorded(), staff.ParkedBranch("demo-alpha")):
                with self.subTest(branch=branch):
                    restore()
                    row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
                    source, destination = self.rust / "claude-demo-alpha", self.rust / "claude-demo-gamma"
                    _ = staff.set_worktree(row.id, staff.RemovedWorktree("natedev", source, branch), expected=row.worktree)
                    self.assert_ok(self.rename())
                    retained = staff.by_id(row.id)
                    assert retained is not None
                    self.assertEqual((retained.id, retained.name, retained.parent, retained.worktree),
                                     (row.id, "gamma", row.parent, staff.RemovedWorktree(
                                         "natedev", destination, staff.ParkedBranch("demo-gamma"))))
                    before = final_files(self.root)
                    self.assert_ok(self.rename())
                    self.assertEqual(final_files(self.root), before)

    def test_a_retry_after_git_move_sets_the_new_staff_path_and_keeps_config_bytes_and_identity(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        source = self.rust / "claude-demo-alpha"
        destination = self.rust / "claude-demo-gamma"
        _ = staff.set_worktree(row.id, staff.PresentWorktree("natedev", source), expected=row.worktree)
        config = unit_config.UnitConfig(unit_config.PlanFile(Path("docs/plans/alpha.md")),
                                        unit_config.AppPort(8123), ("nightly",))
        _ = unit_config.write(source, config)
        data = (source / ".unit.toml").read_bytes()
        with mock.patch.object(staff, "set_worktree", side_effect=SystemExit(91)):
            interrupted = self.rename()
        self.assertNotEqual(interrupted.returncode, 0, interrupted.stdout + interrupted.stderr)
        self.assertFalse(source.exists())
        self.assertTrue(destination.is_dir())
        retained = staff.by_id(row.id)
        assert retained is not None
        self.assertEqual(retained.worktree, staff.PresentWorktree("natedev", source))
        self.assert_ok(self.rename())
        renamed = staff.by_id(row.id)
        assert renamed is not None
        self.assertEqual((renamed.name, renamed.parent, renamed.worktree),
                         ("gamma", row.parent, staff.PresentWorktree("natedev", destination)))
        self.assertEqual((destination / ".unit.toml").read_bytes(), data)
        self.assertEqual(self.git("check-ignore", ".unit.toml", cwd=destination), ".unit.toml")
        before = final_files(self.root)
        self.assert_ok(self.rename())
        self.assertEqual(final_files(self.root), before)

    def test_retry_after_staff_publication_keeps_the_settled_worktree(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        destination = staff.PresentWorktree("natedev", self.rust / "claude-demo-gamma")
        publish = staff.set_worktree

        def interrupted(identity: staff.StaffId, value: staff.PresentWorktree | staff.RemovedWorktree, *,
                        expected: staff.PresentWorktree | staff.RemovedWorktree | staff.WorktreeUnfilled) -> staff.StaffRow:
            self.assertEqual(expected, row.worktree)
            _ = publish(identity, value, expected=expected)
            raise SystemExit(91)

        with mock.patch.object(staff, "set_worktree", side_effect=interrupted):
            stopped = self.rename()
        self.assertNotEqual(stopped.returncode, 0, stopped.stderr)
        current = staff.by_id(row.id)
        assert current is not None
        self.assertEqual(current.worktree, destination)
        self.assert_ok(self.rename())
        current = staff.by_id(row.id)
        assert current is not None
        self.assertEqual(current.worktree, destination)
        before = final_files(self.root)
        self.assert_ok(self.rename())
        self.assertEqual(final_files(self.root), before)

    def test_rename_unit_preserves_staff_and_worker_identity(self) -> None:
        row = next(row for row in staff.snapshot().find(kind="unit") if row.name == "alpha")
        worker = fixture_member("worker", name="impl1", parent=row.id)
        identity = row.id
        with saved_fixture(self.root) as restore:
            for running, followup in ((False, ""), (True, ""), (True, "pending folder move"),
                                      (True, "custom branch pending folder move"),
                                      (True, "custom branch staff id retry"),
                                      (True, "custom branch lost recovery"),
                                      (True, "long former name pending folder move"),
                                      (False, "old notice before staff rename"),
                                      (False, "new notice before staff rename"),
                                      (False, "rename again with pending notice"),
                                      (False, "first notice timeout"),
                                      (False, "document rename after promotion")):
                with self.subTest(running=running, followup=followup), ExitStack() as scope:
                    restore()
                    current = staff.by_id(identity)
                    assert current is not None
                    row = current
                    folder = self.rust / "claude-demo-alpha"
                    destination = self.rust / "claude-demo-gamma"
                    original_branch = "demo-alpha"
                    final_branch = "demo-gamma"
                    if followup == "long former name pending folder move":
                        original = folder
                        folder = original.with_name("claude-demo-alpha-foo")
                        _ = self.git("worktree", "move", str(original), str(folder))
                        row = staff.set_worktree(row.id, staff.PresentWorktree("natedev", folder), expected=row.worktree)
                        row = staff.rename(row.id, "alpha-foo")
                        original_branch = "demo-alpha-foo"
                        _ = self.git("branch", "-m", original_branch, cwd=folder)
                    if followup in {"custom branch pending folder move", "custom branch staff id retry",
                                    "custom branch lost recovery"}:
                        original_branch = final_branch = "feature"
                        _ = self.git("branch", "-m", original_branch, cwd=folder)
                    if followup.endswith("pending folder move"):
                        staging = "staging-foo" if row.name == "alpha-foo" else "staging-alpha"
                        moved = folder.with_name(staging)
                        _ = self.git("worktree", "move", str(folder), str(moved))
                        row = staff.set_worktree(row.id, staff.PresentWorktree("natedev", moved), expected=row.worktree)
                        folder = moved
                        destination = folder.with_name("staging-foo" if row.name == "alpha-foo" else "staging-gamma")
                    if followup.endswith("notice before staff rename"):
                        fields: dict[str, object] = {"key": "private-notice", "pending": [str(row.id), str(worker.id)]}
                        if followup.startswith("new"):
                            fields["attempted"] = False
                        else:
                            fields.update(name="gamma", old_name=row.name)
                        self.write(self.state / row.id / "rename-notices.json", json.dumps(fields))
                    live = {row.id, worker.id} if running else set[staff.StaffId]()
                    stopped: list[staff.StaffId] = []
                    started: list[staff.StaffId] = []
                    messages: list[tuple[str, str, str]] = []
                    attempted: list[str] = []
                    original_run = cast(Callable[..., subprocess.CompletedProcess[str]], subprocess.run)

                    def observe(members: Iterable[staff.StaffRow], record: staff.StaffSnapshot) -> list[staff.StaffRow]:
                        del record
                        return [member for member in members if member.id in live]

                    def stop(member: staff.StaffRow, client: herdr.Herdr) -> bool:
                        del client
                        stopped.append(member.id)
                        live.discard(member.id)
                        return True

                    def start(member: staff.StaffRow, record: staff.StaffSnapshot, client: herdr.Herdr) -> str:
                        del record, client
                        started.append(member.id)
                        live.add(member.id)
                        return "probe-pane"

                    def capture_transport(command: list[str], *arguments: object, **options: object) -> subprocess.CompletedProcess[str]:
                        if command[:2] == session_send.send_argv():
                            recipient = command[command.index("--to") + 1]
                            attempted.append(recipient)
                            if followup in {"first notice timeout", "rename again with pending notice"} and len(attempted) == 1:
                                raise subprocess.TimeoutExpired(command, 0.1)
                            text = options.get("input")
                            self.assertIsInstance(text, str)
                            messages.append((command[command.index("--to") + 1], str(text),
                                             command[command.index("--from") + 1]))
                            return subprocess.CompletedProcess(command, 0, "SENT: private fixture\n", "")
                        return original_run(command, *arguments, **options)

                    _ = scope.enter_context(mock.patch.object(restart, "live_members",
                        side_effect=observe))
                    _ = scope.enter_context(mock.patch.object(restart, "stop_row", side_effect=stop))
                    _ = scope.enter_context(mock.patch.object(restart, "start_row", side_effect=start))
                    _ = scope.enter_context(mock.patch.object(shutdown, "rename_send_notice", self.send_rename_notice))
                    _ = scope.enter_context(mock.patch.object(subprocess, "run", side_effect=capture_transport))
                    displays = RenameDisplays(row)
                    _ = scope.enter_context(displays.installed())
                    _ = scope.enter_context(mock.patch.object(herdr.Herdr, "agents", return_value=[]))
                    commands: list[subprocess.Popen[bytes]] = []
                    if running:
                        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                                                   cwd=folder, stdin=subprocess.PIPE)
                        _ = scope.callback(process.communicate, timeout=TEST_HANG_GUARD_SECS)
                        commands.append(process)
                    reference = (str(row.id) if followup in {"custom branch staff id retry", "custom branch lost recovery"}
                                 else row.name)
                    result = self.invoke(["rename", reference, "gamma"])
                    self.assert_ok(result)
                    if running:
                        self.assertEqual(stopped, [])
                        self.assertEqual(started, [])
                        self.assertIsNone(commands[0].poll())
                        self.assertEqual({message[0] for message in messages}, {str(row.id), str(worker.id)})
                        self.assertEqual(len(messages), 2)
                        self.assertTrue(all("gamma" in message[1] for message in messages), messages)
                        self.assertTrue(all(message[2] == "agents rename" for message in messages), messages)
                        self.assertIn("folder and branch", result.stdout)
                        self.assertEqual(staff.by_id(row.id), replace(row, name="gamma"))
                        self.assertEqual(self.git("branch", "--show-current", cwd=folder), original_branch)
                        self.assertEqual((displays.pane, displays.tab), ("gamma", "gamma"))
                        _ = commands[0].communicate(timeout=TEST_HANG_GUARD_SECS)
                        if followup == "custom branch staff id retry":
                            retry = self.invoke(["rename", str(row.id), "gamma"])
                            self.assert_ok(retry)
                            self.assertEqual(retry.stdout.splitlines()[-1], "done")
                            self.assertEqual((stopped, started), ([], []))
                        elif followup == "custom branch lost recovery":
                            path = unit_move.rename_move_path(row.id)
                            saved = unit_move.json_object(unit_move.read_json(path))
                            fields = dict(saved)
                            del fields["source_branch"]
                            unit_move.write_json(path, fields)
                            retry = self.invoke(["rename", str(row.id), "gamma"])
                            self.assertEqual(retry.returncode, 1)
                            self.assertIn("the pending rename record is invalid", retry.stderr)
                            self.assertIn(str(path), retry.stderr)
                            self.assertNotIn("unchanged", retry.stdout)
                            self.assertEqual(staff.by_id(row.id), replace(row, name="gamma"))
                            self.assertTrue(folder.is_dir())
                            self.assertFalse(destination.exists())
                            self.assertEqual(self.git("branch", "--show-current", cwd=folder), "feature")
                            unit_move.write_json(path, saved)
                            self.assert_ok(self.invoke(["rename", str(row.id), "gamma"]))
                            self.assertEqual((stopped, started), ([], []))
                        elif followup.endswith("pending folder move"):
                            reference = row.name if followup != "pending folder move" else str(row.id)
                            self.assert_ok(self.invoke(["rename", reference, "gamma"]))
                        else:
                            self.assert_ok(self.rename())
                        self.assertEqual(len(messages), 2)
                    self.assertEqual(staff.snapshot().by_id(row.id), staff.StaffRow(
                        row.id, "unit", "gamma", row.parent, current_session=row.id,
                        worktree=staff.PresentWorktree("natedev", destination), doc=row.doc, unit_type=staff.UnitDetails("direct")))
                    self.assertEqual(staff.snapshot().by_id(worker.id), worker)
                    self.assertEqual(self.git("branch", "--show-current", cwd=destination), final_branch)
                    if followup == "rename again with pending notice":
                        self.assertEqual([message[0] for message in messages], [str(worker.id)])
                        self.assert_ok(self.invoke(["rename", str(row.id), "omega"]))
                        self.assertEqual({message[0] for message in messages if "omega" in message[1]},
                                         {str(row.id), str(worker.id)})
                        self.assertEqual(len(messages), 3)
                        settled = self.invoke(["rename", str(row.id), "omega"])
                        self.assertEqual((settled.returncode, settled.stdout, settled.stderr),
                                         (0, "unchanged\n", "1 worker name unchanged: no qualified prefix for omega\n"))
                        self.assertEqual(len(messages), 3)
                        continue
                    if followup == "first notice timeout":
                        self.assertEqual([message[0] for message in messages], [str(worker.id)])
                        self.assertIn("anomaly:", result.stderr)
                        self.assertIn(str(row.id), result.stderr)
                        self.assertEqual(len(result.stderr.splitlines()), 1)
                        self.assert_ok(self.rename())
                        self.assertEqual([message[0] for message in messages], [str(worker.id), str(row.id)])
                    rerun = self.rename()
                    self.assertEqual((rerun.returncode, rerun.stdout, rerun.stderr),
                                     (0, "unchanged\n", "1 worker name unchanged: no qualified prefix for gamma\n"))
                    if followup == "document rename after promotion":
                        self.assertIsInstance(row.parent, staff.ReportsTo)
                        assert isinstance(row.parent, staff.ReportsTo)
                        document = destination / "docs/plans/alpha.md"
                        content = document.read_bytes()
                        promoted = staff.promote_unit(row.id, name="gamma", parent=row.parent.id,
                            doc=staff.LiveDoc("natedev", document))
                        new_document = document.with_name("omega.md")
                        moved = self.invoke(["rename", str(promoted.id), "omega", "--to-doc", str(new_document),
                                             "--to-branch", "promoted-omega"])
                        self.assert_ok(moved)
                        self.assertEqual(staff.by_id(row.id), replace(promoted, name="omega",
                            doc=staff.LiveDoc("natedev", new_document)))
                        self.assertEqual(new_document.read_bytes(), content)
                        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "promoted-omega")

    def test_uncommitted_work_between_phases_also_keeps_the_tip(self) -> None:
        worktree = self.rust / "claude-demo-alpha"
        tip = self.git("rev-parse", "HEAD", cwd=worktree)
        self.write(worktree / "draft.md", "a new file\n")
        self.assert_ok(self.rename())
        new = self.rust / "claude-demo-gamma"
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=new), tip)
        self.assertEqual((new / "draft.md").read_text(), "a new file\n")

    def test_a_mid_phase_rename_keeps_the_commits_and_the_uncommitted_work(self) -> None:
        source = self.rust / "claude-demo-alpha"
        self.write(source / "work.md", "phase work\n")
        _ = self.git("add", "work.md", cwd=source)
        _ = self.git("commit", "-m", "checkpoint: phase 1", cwd=source)
        tip = self.git("rev-parse", "HEAD", cwd=source)
        self.write(source / "work.md", "phase work\nmore, not committed\n")
        self.write(source / "draft.md", "a new file\n")
        plan = (source / "docs/plans/alpha.md").read_bytes()
        self.assert_ok(self.rename())
        destination = self.rust / "claude-demo-gamma"
        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "demo-gamma")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=destination), tip)
        self.assertEqual((destination / "work.md").read_text(), "phase work\nmore, not committed\n")
        self.assertEqual((destination / "draft.md").read_text(), "a new file\n")
        self.assertEqual((destination / "docs/plans/alpha.md").read_bytes(), plan)


    def test_review_refuses_a_branch_that_differs_from_the_pending_move(self) -> None:
        from scripts.production import unit_move

        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        folder = self.rust / "claude-demo-alpha"
        with saved_fixture(self.root) as restore:
            for pending_name in ("", "alpha"):
                with self.subTest(pending_name=pending_name):
                    restore()
                    _ = unit_move.prepare_rename(row, folder, "pending-branch", pending_name or row.name)
                    if not pending_name:
                        path = unit_move.rename_move_path(row.id)
                        fields = unit_move.json_object(unit_move.read_json(path))
                        fields["name"] = ""
                        unit_move.write_json(path, fields)
                    before = file_versions(self.root)
                    result = self.invoke(["rename", "alpha", "alpha", "--to-branch", "cargo-liner-handler"])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("finish the pending rename to alpha" if pending_name else
                                  "the pending rename record is invalid", result.stderr)
                    self.assertEqual(file_versions(self.root), before)

    def test_to_branch_keeps_the_unit_name_and_folder_and_resumes_its_session(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        folder = self.rust / "claude-demo-alpha"
        tip = self.git("rev-parse", "HEAD", cwd=folder)
        self.write(self.home / f".claude/projects/fixture/{row.current_session}.jsonl", "{}\n")
        session = FolderSessionFixture(row, live=True)
        with session.installed(), RenameDisplays(row).installed():
            self.assert_ok(self.invoke(["rename", "alpha", "alpha", "--to-branch", "cargo-liner-handler"]))
        self.assertEqual(staff.by_id(row.id), row)
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.git("branch", "--show-current", cwd=folder), "cargo-liner-handler")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=folder), tip)
        self.assertEqual(self.git("--git-dir", str(self.origin), "rev-parse", "cargo-liner-handler"), tip)
        self.assertEqual(session.stops, [])
        self.assertEqual([resumed.id for resumed in session.starts], [])


class ShutdownFolderRenameFixture(FolderRenameFixture):
    @override
    def setUp(self) -> None:
        with pinned_fixture_settings("production"):
            super().setUp()


@final
class FolderRenameTests(ShutdownFolderRenameFixture):
    old_name: str = "port"
    new_name: str = "x"

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(shutdown, "rename_send_notice"))

    def test_rename_finishes_a_pending_folder_move_before_renaming_its_folder_and_branch(self) -> None:
        from dataclasses import replace
        from scripts.production import unit_move

        moved = self.source.with_name("staging-port")
        destination = moved.with_name("staging-x")
        real_git, publish, rename = unit_move.git, staff.set_worktree, staff.rename
        with saved_fixture(self.root) as restore:
            for point in ("complete", "folder move", "folder publication", "staff rename", "rename move", "rename branch", "rename publication"):
                with self.subTest(point=point):
                    restore()
                    displays = RenameDisplays(self.owner)
                    displays.pane = displays.tab = self.owner.name

                    interrupted = False

                    def stop_after(step: str) -> None:
                        nonlocal interrupted
                        if step == point and not interrupted:
                            interrupted = True
                            raise SystemExit(91)

                    def git(folder: Path, *arguments: str) -> str:
                        result = real_git(folder, *arguments)
                        if arguments[:2] == ("worktree", "move"):
                            stop_after("folder move" if folder == self.source else "rename move")
                        elif arguments[:2] == ("branch", "-m"):
                            stop_after("rename branch")
                        return result

                    def publish_worktree(identity: staff.StaffId,
                                         value: staff.PresentWorktree | staff.RemovedWorktree, *,
                                         expected: staff.PresentWorktree | staff.RemovedWorktree |
                                         staff.WorktreeUnfilled) -> staff.StaffRow:
                        result = publish(identity, value, expected=expected)
                        stop_after("folder publication" if value.path == moved else "rename publication")
                        return result

                    def rename_row(identity: staff.StaffId, name: str) -> staff.StaffRow:
                        result = rename(identity, name)
                        stop_after("staff rename")
                        return result

                    with FolderSessionFixture(self.owner, live=False).installed(), displays.installed():
                        with (mock.patch.object(unit_move, "git", side_effect=git),
                              mock.patch.object(staff, "set_worktree", side_effect=publish_worktree),
                              mock.patch.object(staff, "rename", side_effect=rename_row)):
                            try:
                                _ = unit_move.move_folder(self.owner.id, moved)
                            except SystemExit as error:
                                self.assertEqual(error.code, 91)
                                _ = unit_move.move_folder(self.owner.id, moved)
                            result = self.command()
                        if point not in ("complete", "folder move", "folder publication"):
                            self.assertEqual(result.returncode, 91, result.stdout + result.stderr)
                            result = self.command()
                        self.assertEqual((result.returncode, result.stderr), (0, ""))
                        self.assertEqual(result.stdout.splitlines()[-1], "done")
                        self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="x",
                            worktree=staff.PresentWorktree("natedev", destination)))
                        self.assertEqual(self.git("branch", "--show-current", cwd=destination), "cargo-liner-x")
                        self.assertEqual((destination / "kept.txt").read_text(), "Keep this work.\n")
                        self.assertEqual((displays.pane, displays.tab), ("x", "x"))
                        before = file_versions(self.root)
                        commands = list(displays.commands)
                        rerun = self.command()
                        self.assertEqual((rerun.returncode, rerun.stdout, rerun.stderr), (0, "unchanged\n", ""))
                        self.assertEqual(file_versions(self.root), before)
                        self.assertEqual(displays.commands, commands)

    def test_same_name_with_unit_suffix_changes_nothing(self) -> None:
        from scripts.production import unit_move
        from scripts.agents import shutdown

        displays = RenameDisplays(self.owner)
        displays.pane = displays.tab = self.owner.name
        labels = displays.pane, displays.tab, displays.workspace
        before = file_versions(self.root)
        parent = staff.snapshot().parent_of(self.owner)
        assert parent is not None
        arguments = ["rename", f"{parent.name}/port-unit", "port"]
        with (FolderSessionFixture(self.owner, live=False).installed(), displays.installed(),
              mock.patch.object(unit_move, "move_folder", wraps=unit_move.move_folder) as move):
            result = invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments,
                                dict(os.environ), self.checkout)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "unchanged\n", ""))
        move.assert_not_called()
        self.assertEqual(file_versions(self.root), before)
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.source), "cargo-liner-port")
        self.assertEqual((displays.pane, displays.tab, displays.workspace), labels)
        self.assertEqual(displays.commands, [])

    def test_finished_pending_rename_reports_done_then_unchanged(self) -> None:
        from dataclasses import replace

        renamed = staff.rename(self.owner.id, "x")
        _ = self.git("worktree", "move", str(self.source), str(self.destination))
        _ = self.git("branch", "-m", "cargo-liner-x", cwd=self.destination)
        renamed = staff.set_worktree(renamed.id, staff.PresentWorktree("natedev", self.destination),
                                    expected=renamed.worktree)
        displays = RenameDisplays(renamed)
        displays.pane = displays.tab = renamed.name
        with FolderSessionFixture(renamed, live=False).installed(), displays.installed():
            result = self.command()
            self.assertEqual((result.returncode, result.stderr), (0, ""))
            self.assertEqual(result.stdout.splitlines()[-1], "unchanged")
            self.assertEqual(staff.by_id(renamed.id), replace(self.owner, name="x",
                worktree=staff.PresentWorktree("natedev", self.destination)))
            self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-x")
            before = file_versions(self.root)
            rerun = self.command()
        self.assertEqual((rerun.returncode, rerun.stdout, rerun.stderr), (0, "unchanged\n", ""))
        self.assertEqual(file_versions(self.root), before)

    def test_displays_change_before_the_folder_and_branch(self) -> None:
        from scripts.production import unit_move

        displays = RenameDisplays(self.owner)
        events: list[str] = []
        real_git = unit_move.git

        def display(*arguments: str, **options: object) -> str:
            result = displays.run(*arguments, **options)
            events.append(arguments[0])
            return result

        def git(folder: Path, *arguments: str) -> str:
            if arguments[:2] == ("worktree", "move"):
                events.append("folder")
            elif arguments[:2] == ("branch", "-m"):
                events.append("branch")
            return real_git(folder, *arguments)

        with (FolderSessionFixture(self.owner, live=False).installed(), displays.installed(),
              mock.patch.object(herdr.Herdr, "run", side_effect=display),
              mock.patch.object(unit_move, "git", side_effect=git)):
            result = self.command()
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(events, ["pane", "tab", "folder", "branch"])
        self.assertEqual((displays.pane, displays.tab), ("x", "x"))
        self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-x")
        self.assertEqual((self.destination / "kept.txt").read_text(), "Keep this work.\n")

    def test_same_name_writes_nothing_and_moves_nothing(self) -> None:
        from scripts.production import unit_move
        from scripts.agents import shutdown

        before = file_versions(self.root)
        with (mock.patch.object(shutdown, "rename_repair_displays", return_value=False),
              mock.patch.object(unit_move, "move_folder") as move):
            result = self.command(new="port")
        self.assertEqual(result.returncode, 0, result.stderr)
        move.assert_not_called()
        self.assertEqual(file_versions(self.root), before)
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertEqual(self.git("branch", "--show-current", cwd=self.source), "cargo-liner-port")

    def test_new_name_preserves_the_folder_parent_and_branch_prefix(self) -> None:
        from dataclasses import replace
        from scripts.agents import shutdown

        with (FolderSessionFixture(self.owner, live=False).installed(),
              mock.patch.object(shutdown, "rename_repair_displays", return_value=False)):
            result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.source.exists())
        self.assertEqual(self.destination, self.home / "rust/cargo-liner-x")
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(self.git("branch", "--show-current", cwd=self.destination), "cargo-liner-x")
        self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="x",
            worktree=staff.PresentWorktree("natedev", self.destination)))
        self.assertFalse((self.home / "worktrees/x").exists())

    def test_unmatched_folder_stays_and_reports_one_anomaly_while_branch_moves(self) -> None:
        from dataclasses import replace
        from scripts.agents import shutdown

        original = self.source
        self.source = original.with_name("unmatched")
        _ = self.git("worktree", "move", str(original), str(self.source))
        _ = staff.set_worktree(self.owner.id, staff.PresentWorktree("natedev", self.source),
                              expected=staff.PresentWorktree("natedev", original))
        with (FolderSessionFixture(self.owner, live=False).installed(),
              mock.patch.object(shutdown, "rename_repair_displays", return_value=False)):
            result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertTrue(result.stderr.startswith("anomaly:"), result.stderr)
        self.assertTrue(self.source.is_dir())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.git("branch", "--show-current", cwd=self.source), "cargo-liner-x")
        self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, name="x"))


@final
class StartupRenameTests(ShutdownFolderRenameFixture):
    old_name: str = "startup"

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(mock.patch.object(shutdown, "rename_send_notice"))
        _ = self.enterContext(mock.patch.object(shutdown, "rename_repair_displays", return_value=False))
        _ = fixture_unit_saved_name(self.owner, "startup-unit")

    def restore_startup_name(self) -> subprocess.CompletedProcess[str]:
        arguments = ["rename", str(self.owner.id), "startup"]
        return invoke_cli(shutdown, lambda: shutdown.main(arguments), arguments, dict(os.environ), self.checkout)

    def test_restore_startup_name_keeps_the_matching_folder_and_branch_quiet(self) -> None:
        result = self.restore_startup_name()

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "done\n", ""))
        self.assertEqual(staff.by_id(self.owner.id), self.owner)
        self.assertTrue(self.source.is_dir())
        self.assertEqual(self.git("branch", "--show-current", cwd=self.source), "cargo-liner-startup")
        self.assertIsNone(unit_move.pending_rename(self.owner.id))

    def test_restore_startup_name_reports_only_the_unmatched_folder(self) -> None:
        folder = self.source.with_name("unmatched")
        _ = self.git("worktree", "move", str(self.source), str(folder))
        location = staff.PresentWorktree(remote.local_machine(), folder)
        _ = staff.set_worktree(self.owner.id, location, expected=self.owner.worktree)

        result = self.restore_startup_name()

        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, "done\n", f"anomaly: folder {folder} does not end with -startup-unit\n"))
        self.assertEqual(staff.by_id(self.owner.id), replace(self.owner, worktree=location))
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.git("branch", "--show-current", cwd=folder), "cargo-liner-startup")
        self.assertIsNone(unit_move.pending_rename(self.owner.id))


class UnitRenameSessionsTests(UnitRenameFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        bind_private_run_roots(self, self.root)
        _ = self.enterContext(mock.patch.object(shutdown, "rename_send_notice"))

    def test_recordless_rename_keeps_the_staff_session_without_a_launch(self) -> None:
        row = next(row for row in staff.find(kind="unit") if row.name == "alpha")
        panes: list[herdr.PaneIdentity] = [
            {"pane_id": "w1:p1", "workspace_id": "w1", "terminal_id": "t1",
             "label": "alpha", "session_id": row.current_session},
            {"pane_id": "w1:p2", "workspace_id": "w1", "terminal_id": "t2",
             "label": "other", "session_id": "unrelated-session"},
        ]
        def native_process(pane: str, kind: str, timeout: float, *,
                           pane_row: Mapping[str, object] | herdr.PaneNotRead = herdr.PANE_NOT_READ) -> herdr.NativeAgentProcess:
            del timeout, pane_row
            session = row.current_session if pane == "w1:p1" else "unrelated-session"
            identity = herdr.PaneSessionReport(session) if kind == "codex" else herdr.EnvironmentThread(session)
            return herdr.NativeAgentProcess(identity, "fixture-generation", {})

        with (mock.patch.object(herdr.Herdr, "panes", return_value=panes),
              mock.patch.object(herdr.Herdr, "native_pane_process", side_effect=native_process),
              mock.patch.object(herdr.Herdr, "retitle", return_value=True) as displays,
              mock.patch.object(herdr.Herdr, "start_agent") as launches):
            self.assert_ok(self.rename())
        displays.assert_called_once_with("w1:p1", "gamma")
        launches.assert_not_called()
        self.assertEqual(staff.by_id(row.id), replace(
            row, name="gamma", worktree=staff.PresentWorktree("natedev", self.rust / "claude-demo-gamma")))

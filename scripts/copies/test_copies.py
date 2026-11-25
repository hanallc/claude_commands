"""Real-Git acceptance tests for the standing gate checkout contract."""
from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr
import fcntl
import io
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from typing import ClassVar, TextIO, cast, override
from unittest import mock

from scripts.buildlog.store import host_name
from scripts.copies import copies
from scripts.copies.workspace_test_support import registered_workspace
from scripts.build_queue import admission_client, first_step, queue_wait
from scripts.lib.build_steps import GATE_RESULT_UNKNOWN_STATUS, MAC_LINUX_PRELAUNCH_REFUSED_STATUS
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.copies.test_remote_copies import expected_remote_lease_columns
from scripts.accounts import accounts
from scripts.lib import build_folders, copy_record, nix_env, process_start, scratch, session_folder
from scripts.lib.build_copies_test_support import private_copy_config
from scripts.lint import folder_audit, tmp_sweep
from scripts.lint.memory_test_support import ADMISSION_CONFIG_FIXTURE, write_config_fixture
from textwrap import dedent
from scripts.mac_test import floor_claims, offload
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.lib.cargo_output import CargoOutputRoots
from scripts.message import sessions
from scripts.production.cli_fixture import ImmutableGitSeed, invoke_cli
from scripts.lib import removal_guard
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, fixture_member, fixture_worker


SSH_SOURCE_TRUST = copies.SshSourceTrust(trusted_caller=offload.trusted_linux_source, trusted_root=nix_env.trusted_root,
                                        registered_worktree=nix_env.registered_worktree)



def launch_copy_fixture(launch: Callable[[], subprocess.CompletedProcess[str]]) -> subprocess.CompletedProcess[str]:
    return launch()


class ImportTests(unittest.TestCase):
    def test_import_loads_no_modules_from_unit_labels(self) -> None:
        program = dedent("""
            import sys
            import scripts.copies.copies

            forbidden = {
                "scripts.message.unit_labels",
                "scripts.production.showrunners",
                "scripts.production.session_staff",
                "scripts.lib.unit_runs",
            }
            unwanted = sorted(forbidden.intersection(sys.modules))
            if unwanted:
                raise SystemExit("Unexpected imports: " + ", ".join(unwanted))
        """)
        result = subprocess.run(
            [sys.executable, "-c", program], cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class RunCopyTests(unittest.TestCase):
    def test_gate_cleanup_error_keeps_the_gate_status(self) -> None:
        command = ["python3", "scripts/production/gate.py", "--tests-only"]
        tree = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for status in (MAC_LINUX_PRELAUNCH_REFUSED_STATUS, GATE_RESULT_UNKNOWN_STATUS):
            with self.subTest(status=status), redirect_stderr(io.StringIO()) as errors:
                run_gate = mock.Mock(return_value=subprocess.CompletedProcess(command, status))
                admitted = mock.Mock(spec=first_step.FirstStepAdmission, run_gate=run_gate)

                @contextmanager
                def command_copy() -> Generator[copies.AdmittedCommandCopy]:
                    yield copies.AdmittedCommandCopy(tree, admitted)
                    raise copies.CopyError("cannot release gate placement")

                with (mock.patch.object(copies, "caller_owner", return_value="fixture"),
                      mock.patch.object(first_step, "gate_targets", return_value=("mac-linux",)),
                      mock.patch.object(copies, "_command_copy", return_value=command_copy())):
                    result = copies.main(["run", "--repo", str(tree), "--commit", "HEAD", "--", *command])
                self.assertEqual(result, status)
                run_gate.assert_called_once()
                self.assertEqual(errors.getvalue(), "Gate cleanup failed. cannot release gate placement\n")


class StandingTests(unittest.TestCase):
    source_seed: ClassVar[ImmutableGitSeed]
    base: Path = Path()
    source: Path = Path()
    gates: Path = Path()
    configuration: Path = Path()
    settings: dict[str, str] = {}
    head: str = ""
    published: str = ""
    owner_id: str = ""

    @classmethod
    @override
    def setUpClass(cls) -> None:
        root = Path(tempfile.mkdtemp(prefix="copies-source-seed-")).resolve()
        cls.addClassCleanup(shutil.rmtree, root)
        home = root / "home"
        home.mkdir()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_") and key != "UNIT_RUN_DIR"}
        environment.update({
            "HOME": str(home), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Gate Tests",
            "GIT_AUTHOR_EMAIL": "gate-tests@example.invalid", "GIT_COMMITTER_NAME": "Gate Tests",
            "GIT_COMMITTER_EMAIL": "gate-tests@example.invalid",
        })
        with mock.patch.dict(os.environ, environment, clear=True):
            cls.source_seed = ImmutableGitSeed.create(
                root, "main", "Gate Tests", "gate-tests@example.invalid",
                {"Cargo.toml": "[workspace]\n", "tracked.txt": "published\n", "stable.txt": "unchanged\n",
                 ".gitignore": "target/\nignored.txt\n"},
            )
            builder = cls()
            _ = builder.git(cls.source_seed.origin, "remote", "add", "origin", "https://example.invalid/checkout.git")
            cls.published = builder.git(cls.source_seed.checkout, "rev-parse", "HEAD")
            _ = (cls.source_seed.checkout / "tracked.txt").write_text("source HEAD\n")
            _ = builder.git(cls.source_seed.checkout, "commit", "-qam", "unpushed source commit")
            cls.head = builder.git(cls.source_seed.checkout, "rev-parse", "HEAD")

    @override
    def setUp(self) -> None:
        _ = self.enterContext(fake_cargo_output_roots(children=True))
        self.base = Path(tempfile.mkdtemp(prefix="copies-fixture-")).resolve()
        self.addCleanup(shutil.rmtree, self.base)
        temporary = self.base / "temporary"
        temporary.mkdir()
        _ = self.enterContext(mock.patch.object(tempfile, "tempdir", str(temporary)))
        copy_configuration = private_copy_config(self.base)
        configuration = Path(copy_configuration.environment["CONF_DIR"])
        home = self.base / "home"
        home.mkdir()
        _ = write_config_fixture(copy_configuration.settings, """\
locks.probe_wait_seconds=0.25
locks.probe_retry_seconds=1
names.repository_key_length=8
names.suffix_characters=abcdefghijklmnopqrstuvwxyz0123456789_
names.suffix_length=8
patterns.legacy-stable={purpose}
patterns.legacy-unique={purpose}{suffix}
patterns.scratch={purpose}-{suffix}
patterns.standing={repository}/{owner}-gate{suffix}
patterns.target={purpose}
patterns.tool={purpose}{suffix}
prefixes.admit-sock=admit-sock-
prefixes.copies-index=copies-index-
roots.system=/tmp
roots.temporary=${TMPDIR}/builds
""" + f"roots.scratch={self.base / 'scratch'}\nroots.scratch.{host_name()}={self.base / 'scratch'}\n")
        _ = write_config_fixture(configuration / "scratch.conf", f"""\
default_life_seconds=86400
record_lock_attempts=3
repo_depth=3
repo_skip=target node_modules .git
suffix_length=6
""")
        _ = write_config_fixture(configuration / "nix_env.conf", """\
command_timeout_seconds=600
trusted_home_folders=rust worktrees
trusted_home_prefix=.claude
""")
        _ = write_config_fixture(configuration / "lint.conf", f"""\
folder_state_dir={home / '.local/state/folder-sweep'}
sweep_max_tree_depth=3
""")
        _ = write_config_fixture(configuration / "build_steps.conf", "names.integrate=integrate\n")
        _ = (configuration / "build_admission.conf").write_text("""\
# whole: 1..
cloud_gate_slots=16
# whole: 1..
gpu_test_slots=2
# whole: 0..100
agent_swap_share_percent=20
# whole: 0..50
peak_history_share_percent=10
# whole: 1..
peak_hold_seconds.merge-gate=180
# whole: 1..
peak_hold_seconds.targeted-test=180
# range: 5e-324..1.6742321987285425e299
program_first_peak_gib.targeted-test=4
# range: 0..
progress_profile_sweep_seconds=0.05
# whole: 1..86400
remote_start_hold_seconds=1200
# range: 5e-324..
source_check_seconds=1
""" + ADMISSION_CONFIG_FIXTURE.read_text())
        self.configuration = self.base / "copies.conf"
        self.settings = {"owner.verify": "verify",
                         "remote_registry": str(self.base / "remote-mirrors"),
                         "git_timeout_seconds": "10",
                         "guard_timeout_seconds": "10", "guard_choice_limit": "32"}
        self.write_config(self.settings)
        # The test's gates lease their own Cargo cache.
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GIT_", "COPIES_STEP_")) and key not in (
                           "UNIT_RUN_DIR", "CARGO_BUILD_BUILD_DIR", "CARGO_TARGET_DIR", "COPIES_RUN_PID",
                           "BASH_ENV", "STAFF_CONFIG", admission_client.RESERVED_STEP_ENV)}
        environment.update({
            **copy_configuration.environment,
            "HOME": str(home),
            "USER_ZONE": "UTC",
            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Gate Tests",
            "GIT_AUTHOR_EMAIL": "gate-tests@example.invalid", "GIT_COMMITTER_NAME": "Gate Tests",
            "GIT_COMMITTER_EMAIL": "gate-tests@example.invalid",
            "SCRATCH_ROOT": str(self.base / "scratch"),
            "SCRATCH_HOLDS": str(self.base / "holds.json"),
            "SCRATCH_COPIES_ROOT": str(self.base / "workspaces"),
            "COPIES_CONFIG": str(self.configuration),
            # A child's fresh copy goes in the test folder, so a killed child leaves nothing outside it.
            "TMPDIR": str(temporary),
        })
        self.enterContext(mock.patch.dict(os.environ, environment, clear=True))
        self.enterContext(isolated_staff_file(self.base / "staff.json", {}))
        parent = fixture_member("showrunner", name="builds")
        owner = fixture_member("unit", name="copy-owner", parent=parent.id)
        self.owner_id = str(owner.id)
        os.environ["CODEX_THREAD_ID"] = ""
        os.environ["CLAUDE_CODE_SESSION_ID"] = owner.current_session
        self.source = self.base / "checkout"
        self.source_seed.copy_to(self.source, self.base / "origin.git")
        self.gates = self.base / "build/origin/copy-owner"

    def lease_machine(self) -> str:
        from scripts.buildlog.store import host_name

        return host_name()

    def write_config(self, settings: Mapping[str, str]) -> None:
        lines = ["[copies]"]
        for key, value in settings.items():
            if key == "git_timeout_seconds":
                lines.append("# range: 5e-324..")
            lines.append(f"{key}={value}")
        _ = self.configuration.write_text("\n".join(lines) + "\n")

    def git(self, path: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(path), *arguments], capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return invoke_cli(copies, lambda: copies.main(list(arguments)), list(arguments),
                          dict(os.environ), self.base)

    def run_cli(self, *options: str, command: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        return self.cli("run", "--repo", str(self.source),
                        "--commit", "HEAD", *options, "--", *(command or ("true",)))

    def test_run_keeps_a_numbered_copy_with_the_childs_reason(self) -> None:
        reason = "checkout exit is unconfirmed"
        result = self.run_cli(command=(
            sys.executable, "-c",
            "import os, pathlib, sys; pathlib.Path(os.environ['COPIES_KEEP_FILE']).write_text(sys.argv[1])",
            reason,
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        first = self.gates / "1"
        self.assertEqual(build_folders.build_folder_state(first), "kept")
        self.assertEqual(first.with_name("1.keep").read_text(), reason)
        self.assertIs(build_folders.folder_lease_fields(first)["released"], False)
        listing = self.cli("list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        row = next(line.split("\t") for line in listing.stdout.splitlines() if str(first) in line)
        self.assertEqual(row[7], "kept")
        with build_folders.folder_removal_lease(first) as lease:
            self.assertIsInstance(lease, build_folders.RefusedRemovalLease)
        location = self.base / "command-location"
        next_run = self.run_cli(command=(
            sys.executable, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(str(pathlib.Path.cwd()))",
            str(location),
        ))
        self.assertEqual(next_run.returncode, 0, next_run.stderr)
        self.assertEqual(Path(location.read_text()), self.gates / "2")
        self.assertTrue(first.is_dir())

    def test_run_reuses_a_numbered_copy_when_the_child_leaves_no_reason(self) -> None:
        result = self.run_cli(command=(
            sys.executable, "-c",
            "import os, pathlib; path = pathlib.Path(os.environ['COPIES_KEEP_FILE']); "
            + "assert path.is_absolute() and not path.exists(); path.touch(); raise SystemExit(7)",
        ))
        self.assertEqual(result.returncode, 7, result.stderr)
        first = self.gates / "1"
        self.assertEqual(build_folders.build_folder_state(first), "free")
        self.assertFalse(first.with_name("1.keep").exists())
        self.assertIs(build_folders.folder_lease_fields(first)["released"], True)
        location = self.base / "command-location"
        next_run = self.run_cli(command=(
            sys.executable, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(str(pathlib.Path.cwd()))",
            str(location),
        ))
        self.assertEqual(next_run.returncode, 0, next_run.stderr)
        self.assertEqual(Path(location.read_text()), first)

    def test_claude_and_codex_workers_use_their_unit_owner(self) -> None:
        unit = next(row for row in staff.local_snapshot().rows if row.kind == "unit")
        worker = fixture_worker("builds-impl", unit.id)
        for variable in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
            with self.subTest(variable=variable), mock.patch.dict(os.environ, {
                "CLAUDE_CODE_SESSION_ID": "", "CODEX_THREAD_ID": "", variable: worker.current_session,
            }):
                if variable == "CODEX_THREAD_ID":
                    self.assertEqual(copies.caller_owner(), str(unit.id))
                    continue
                location = self.base / "command-location"
                result = self.run_cli(command=(
                    sys.executable, "-c",
                    "import json,os,sys; from pathlib import Path; "
                    + "Path(sys.argv[1]).write_text(json.dumps([str(Path.cwd()), os.environ['COPIES_STEP_OWNER']]))",
                    str(location),
                ))
                self.assertEqual(result.returncode, 0, result.stderr)
                folder, owner = cast(list[str], json.loads(location.read_text()))
                self.assertEqual(owner, str(unit.id))
                self.assertEqual(Path(folder), self.gates / "1")
                self.assertEqual(build_folders.build_folder_state(Path(folder)), "free")
                with copies.standing(owner, self.source, copies.CommittedSource(self.head)) as tree:
                    self.assertEqual(self.lease(tree)["unit"], self.gates.name)
                removed = self.cli("remove")
                self.assertEqual(removed.returncode, 0, removed.stderr)
                self.assertTrue(tree.exists())

        # The user, 11-16: a test or lint run showed as a gate of the base commit's subject.
        for step, merging, purpose in (("integrate", False, "integrate"), ("lint", True, "lint"), ("run", False, "run")):
            with self.subTest(step=step, merging=merging):
                revision = self.git(self.source, "rev-parse", "HEAD")
                _ = self.git(self.source, "commit", "--allow-empty", "-qm", "Later source work")
                merge = copies.MergeRevision(self.git(self.source, "rev-parse", "HEAD")) if merging else copies.NO_MERGE
                with copies.standing(self.owner_id, self.source, copies.CommittedSource(revision), merge=merge, step=step) as tree:
                    lease = self.lease(tree)
                    self.assertEqual(lease["unit"], tree.parent.name)
                    self.assertEqual((lease["work"], lease["purpose"]), (step, purpose))

    def test_a_renamed_unit_uses_its_current_name(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as original:
            _ = (original / "target/artifact").write_text("warm")
        _ = staff.rename(staff.StaffId(self.owner_id), "renamed")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as renamed:
            self.assertEqual(renamed, original.parent.with_name("renamed") / "1")
            self.assertEqual((original / "target/artifact").read_text(), "warm")

    def test_distinct_units_use_their_own_first_copy(self) -> None:
        parent = fixture_member("showrunner", name="another-production")
        other = fixture_member("unit", name="other-owner", parent=parent.id)
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as first:
            with copies.standing(str(other.id), self.source, copies.CommittedSource(self.head)) as second:
                self.assertEqual(first, self.gates / "1")
                self.assertEqual(second, self.gates.with_name("other-owner") / "1")
        with copies.standing(str(other.id), self.source, copies.CommittedSource(self.head)) as reused:
            self.assertEqual(reused, second)
            self.assertEqual(self.git(reused, "rev-parse", "HEAD"), self.head)

    def test_a_role_keeps_its_copy_when_a_staff_row_changes_name(self) -> None:
        with copies.standing("toolchain-trial", self.source, copies.CommittedSource(self.head)) as tree:
            _ = (tree / "target/artifact").write_text("role cache")
        _ = staff.rename(staff.StaffId(self.owner_id), "renamed")
        copies.remove_trees(self.owner_id)
        with copies.standing("toolchain-trial", self.source, copies.CommittedSource(self.head)) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(reused, tree)
            self.assertEqual((reused / "target/artifact").read_text(), "role cache")
        copies.remove_trees("toolchain-trial")
        self.assertTrue(tree.exists())

    def test_missing_staff_row_refuses_run_and_remove_in_one_line(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "absent", "CODEX_THREAD_ID": ""}):
            for arguments in (("run", "--repo", str(self.source), "--commit", "HEAD", "--", "true"), ("remove",)):
                with self.subTest(arguments=arguments):
                    result = self.cli(*arguments)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stderr, "copies: calling session has no staff row or unit owner\n")
                    self.assertFalse(self.gates.exists())

    def test_free_copy_does_not_resolve_the_queue_name(self) -> None:
        command = ("bash", "-c", "bash /fixture/verify.sh check hana")
        with mock.patch.object(queue_wait, "command_queue_name", wraps=queue_wait.command_queue_name) as resolve:
            with copies.command_tree(self.owner_id, self.source, copies.CommittedSource(self.head), command) as tree:
                self.assertTrue(tree.is_dir())
        resolve.assert_not_called()

    def test_copy_uses_the_requested_tree_if_head_moves_after_lookup(self) -> None:
        incoming = self.incoming()
        requested_tree = self.git(self.source, "rev-parse", f"{incoming}^{{tree}}")
        real_git = copies.git

        def advance_head() -> None:
            _ = self.git(self.source, "update-ref", "HEAD", incoming, self.head)

        def git(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
            if repo == self.source and arguments[-2:] == ("HEAD^{commit}", "HEAD^{tree}"):
                # Move HEAD between Git's two revision arguments.
                result = real_git(repo, *arguments[:-1], data=data)
                advance_head()
                return result + real_git(repo, *arguments[:-2], "HEAD^{tree}")
            result = real_git(repo, *arguments, data=data)
            if repo == self.source and arguments[:1] == ("log",):
                advance_head()
            return result

        output = self.base / "copy-tree"
        args = copies.CopyArguments()
        args.tree = requested_tree
        args.command = ["sh", "-eu", "-c", 'git rev-parse HEAD^{tree} > "$1"', "copy-command", str(output)]
        with mock.patch.object(copies, "git", side_effect=git):
            self.assertEqual(copies.run_copy_step(self.source, args), 0)
        self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), incoming)
        self.assertEqual(output.read_text().strip(), requested_tree)

    def test_copy_accepts_a_source_that_configures_utf16_log_output(self) -> None:
        _ = self.git(self.source, "config", "i18n.logOutputEncoding", "UTF-16")
        tree = self.git(self.source, "rev-parse", "HEAD^{tree}")
        output = self.base / "copy-head"
        result = self.cli("hold", "--repo", str(self.source), "--tree", tree,
                          "--", "sh", "-eu", "-c",
                          'git rev-parse HEAD > "$1"', "copy-command", str(output))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text().strip(), self.head)

    def lease(self, tree: Path) -> dict[str, object]:
        return cast(dict[str, object], json.loads(tree.with_name(tree.name + ".lease.json").read_text()))

    def snapshot(self, path: Path) -> dict[str, bytes]:
        return {str(entry.relative_to(path)): entry.read_bytes()
                for entry in path.rglob("*") if entry.is_file()}

    def incoming(self) -> str:
        _ = self.git(self.source, "switch", "-qc", "incoming")
        _ = (self.source / "incoming.txt").write_text("merged content\n")
        _ = self.git(self.source, "add", "incoming.txt")
        _ = self.git(self.source, "commit", "-qm", "incoming feature")
        commit = self.git(self.source, "rev-parse", "HEAD")
        _ = self.git(self.source, "switch", "-q", "main")
        return commit

    def python_run(self, program: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-c", program, *arguments],
                              capture_output=True, text=True, timeout=10)

    def fresh_base(self) -> str:
        _ = self.git(self.source, "rm", "Cargo.toml")
        _ = self.git(self.source, "commit", "-qm", "remove the root build manifest")
        return self.git(self.source, "rev-parse", "HEAD")

    def test_fresh_gate_holds_the_commit_and_lock_then_removes_its_task_folder(self) -> None:
        base = self.fresh_base()
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(base)) as tree:
            self.assertEqual(tree, self.gates / '1')
            self.assertEqual(self.git(tree, 'rev-parse', 'HEAD'), base)
            self.assertEqual(build_folders.build_folder_state(tree), 'held')
            self.assertEqual(list(scratch.registered_copy_paths()), [])
            self.assertEqual(self.git(self.source, 'branch', '--show-current'), 'main')
        self.assertTrue(tree.exists())
        self.assertEqual(build_folders.build_folder_state(tree), 'free')

    def test_a_staff_name_with_a_dot_gets_a_valid_fresh_folder(self) -> None:
        base = self.fresh_base()
        _ = staff.rename(staff.StaffId(self.owner_id), 'unit.test')
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(base)) as tree:
            self.assertEqual(tree.name, '1')
            self.assertEqual(self.git(tree, 'rev-parse', 'HEAD'), base)
            self.assertEqual(self.lease(tree)['unit'], tree.parent.name)
            listing = self.cli('list')
            self.assertIn('unit.test', listing.stdout)
        self.assertEqual(build_folders.build_folder_state(tree), 'free')

    def test_fresh_gate_removes_its_folder_after_a_caller_exception(self) -> None:
        base = self.fresh_base()
        failure = ValueError("the gate failed")
        folders: list[Path] = []
        with self.assertRaises(ValueError) as raised:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(base)) as tree:
                folders.append(tree)
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertTrue(folders[0].exists())
        self.assertEqual(build_folders.build_folder_state(folders[0]), "free")

    def test_fresh_gate_removes_its_folder_after_a_merge_conflict(self) -> None:
        _ = self.fresh_base()
        _ = self.git(self.source, "switch", "-qc", "incoming")
        _ = (self.source / "tracked.txt").write_text("incoming conflict\n")
        _ = self.git(self.source, "commit", "-qam", "incoming conflict")
        _ = self.git(self.source, "switch", "-q", "main")
        _ = (self.source / "tracked.txt").write_text("base conflict\n")
        _ = self.git(self.source, "commit", "-qam", "base conflict")
        base = self.git(self.source, "rev-parse", "HEAD")
        with self.assertRaises(copies.PreparationError):
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(base),
                                 merge=copies.MergeRevision("incoming")):
                self.fail("A conflicting merge must not enter the caller")
        self.assertEqual(list((self.base / "scratch").rglob("checkout")), [])

    def test_fresh_gates_of_one_owner_overlap_without_reusing_a_tree(self) -> None:
        base = self.fresh_base()
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(base)) as first:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(base)) as second:
                self.assertNotEqual(first, second)
                self.assertTrue(first.exists())
                self.assertTrue(second.exists())
                self.assertTrue(second.is_relative_to(self.gates))
            self.assertTrue(second.exists())
            self.assertTrue(first.exists())
        self.assertTrue(first.exists())

    def test_overlapping_gates_build_in_their_own_leased_caches(self) -> None:
        previous = os.environ.get('CARGO_BUILD_BUILD_DIR')
        with copies.standing(self.owner_id, self.source, copies.CommittedSource('HEAD')) as first:
            first_output = Path(os.environ['CARGO_BUILD_BUILD_DIR'])
            with copies.standing(self.owner_id, self.source, copies.CommittedSource('HEAD')) as second:
                second_output = Path(os.environ['CARGO_BUILD_BUILD_DIR'])
                self.assertNotEqual(first_output, second_output)
                self.assertEqual(first_output, first / "target")
                self.assertEqual(second_output, second / "target")
                for tree in (first, second):
                    self.assertFalse((tree / 'target').is_symlink())
                    self.assertEqual(self.git(tree, 'status', '--porcelain'), '')
                    self.assertEqual(build_folders.build_folder_state(tree), 'held')
            self.assertEqual(os.environ['CARGO_BUILD_BUILD_DIR'], str(first_output))
        self.assertEqual(os.environ.get('CARGO_BUILD_BUILD_DIR'), previous)
        self.assertEqual(build_folders.build_folder_state(first), 'free')
        self.assertEqual(build_folders.build_folder_state(second), 'free')
        # A command inside a build copy inherits that copy's cache; a gate it opens builds in its own copy's cache.
        outer = str(self.base / "cargo" / "outer-copy1")
        with mock.patch.dict(os.environ, {"CARGO_BUILD_BUILD_DIR": outer}):
            with copies.standing(self.owner_id, self.source, copies.CommittedSource('HEAD')) as first:
                self.assertEqual(os.environ['CARGO_BUILD_BUILD_DIR'], str(first / "target"))
            self.assertEqual(os.environ['CARGO_BUILD_BUILD_DIR'], outer)
        for output in (str(self.base / "explicit-output"), "relative-output"):
            with self.subTest(output=output), mock.patch.dict(os.environ, {"CARGO_BUILD_BUILD_DIR": output}):
                with copies.standing(self.owner_id, self.source, copies.CommittedSource('HEAD')) as first:
                    self.assertEqual(os.environ['CARGO_BUILD_BUILD_DIR'], str(first / "target"))
                    with copies.standing(self.owner_id, self.source, copies.CommittedSource('HEAD')) as second:
                        self.assertEqual(os.environ['CARGO_BUILD_BUILD_DIR'], str(second / "target"))
                    self.assertEqual(os.environ['CARGO_BUILD_BUILD_DIR'], str(first / "target"))

    def test_rescued_workspace_retry_removes_unchanged_source_and_keeps_new_edits(self) -> None:
        for changed in (False, True):
            with self.subTest(changed=changed):
                name = f"rescued-retry-{changed}"
                task = f"gate:{name}"
                tree = copies.workspace_path(self.owner_id, self.source, name)
                manifest = self.base / f"{name}.json"
                _ = manifest.write_text(json.dumps({"version": 2, "canonical": str(self.source), "base": self.head,
                    "crates": {"saved": {"checkout": str(tree), "stage": "abandoned"}}}))
                created: dict[Path, os.stat_result] = {}
                with registered_workspace(self.owner_id, self.source, self.head, name=name, branch="worker",
                                      task=task, run_manifest=manifest, on_create=created.__setitem__):
                    _ = (tree / "tracked.txt").write_text("saved dirty workspace\n")
                with (mock.patch.object(copies, "rescue_workspace", wraps=copies.rescue_workspace) as rescue,
                      mock.patch.object(scratch, "remove", side_effect=OSError("stop after intent"))):
                    with self.assertRaisesRegex(copies.CopyError, "stop after intent"):
                        copies.end_workspaces(task, [tree], created=created)
                    rescue.assert_called_once()
                self.assertTrue(scratch.removal_mark(tree).is_file())
                if changed:
                    _ = (tree / "tracked.txt").write_text("new edit after rescue\n")
                with mock.patch.object(copies, "rescue_workspace", side_effect=AssertionError("duplicate rescue")):
                    if changed:
                        with self.assertRaisesRegex((copies.CopyError, scratch.ScratchError), "uncommitted changes"):
                            copies.end_workspaces(task, [tree], created=created)
                    else:
                        copies.end_workspaces(task, [tree], created=created)
                if changed:
                    self.assertEqual((tree / "tracked.txt").read_text(), "new edit after rescue\n")
                    self.assertTrue(scratch.removal_mark(tree).is_file())
                else:
                    self.assertFalse(tree.exists())
                    self.assertFalse(scratch.removal_mark(tree).exists())

    def test_workspace_cleanup_exports_clean_branch_commits_before_deletion(self) -> None:
        run_git = copies.git

        def refuse_export(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
            if arguments and arguments[0] == "fetch":
                raise copies.CopyError("stash rescue export refused")
            return run_git(repo, *arguments, data=data)

        for source_gone, export_failure in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(source_gone=source_gone, export_failure=export_failure):
                source = self.base / f"rescue-source-{source_gone}-{export_failure}"
                _ = self.git(self.base, "clone", "-q", str(self.source), str(source))
                for key in ("user.name", "user.email"):
                    _ = self.git(source, "config", key, self.git(self.source, "config", key))
                name = f"rescue-{source_gone}-{export_failure}"
                task = f"gate:{name}"
                tree = copies.workspace_path(self.owner_id, source, name)
                manifest = self.base / f"{name}.json"
                _ = manifest.write_text(json.dumps({"version": 2, "canonical": str(source), "base": self.head,
                    "crates": {"saved": {"checkout": str(tree), "stage": "abandoned"}}}))
                created: dict[Path, os.stat_result] = {}
                with registered_workspace(self.owner_id, source, self.head, name=name, branch="worker",
                                      task=task, run_manifest=manifest, on_create=created.__setitem__):
                    _ = (tree / "tracked.txt").write_text("unpublished clean commit\n")
                    _ = self.git(tree, "commit", "-qam", "workspace commit")
                    tip = self.git(tree, "rev-parse", "HEAD")
                    _ = self.git(tree, "branch", "saved-work")
                    _ = self.git(tree, "reset", "--hard", self.head)
                    stash_contents = (f"first stashed edit {name}", f"second stashed edit {name}")
                    for content in stash_contents:
                        _ = (tree / "tracked.txt").write_text(content + "\n")
                        _ = self.git(tree, "stash", "push", "-qm", content)
                    stashes = self.git(tree, "reflog", "show", "--format=%H", "refs/stash").splitlines()
                    self.assertEqual(len(stashes), 2)
                    for stash in stashes:
                        self.assertNotEqual(subprocess.run(["git", "-C", str(source), "cat-file", "-e", stash],
                            check=False, capture_output=True).returncode, 0)
                    _ = self.git(tree, "repack", "-a", "-d")
                    (tree / ".git/objects/info/alternates").unlink()
                if source_gone:
                    _ = self.git(tree, "remote", "set-url", "origin", "https://example.invalid/gone.git")
                    shutil.rmtree(source)
                if export_failure:
                    with mock.patch.object(copies, "git", side_effect=refuse_export):
                        with self.assertRaisesRegex(copies.CopyError, "stash rescue export refused"):
                            copies.end_workspaces(task, [tree], created=created)
                    self.assertTrue(tree.exists())
                    self.assertEqual(self.git(tree, "reflog", "show", "--format=%H", "refs/stash").splitlines(), stashes)
                copies.end_workspaces(task, [tree], created=created)
                self.assertFalse(tree.exists())
                root = copy_record.copies_root()
                destination = (root.with_name(root.name + "-rescues.git") if source_gone
                               else copies.copy_source(source))
                refs = self.git(destination, "for-each-ref", "--format=%(refname)", "refs/rescue/").splitlines()
                self.assertTrue(refs)
                self.assertTrue(any(subprocess.run(["git", "-C", str(destination), "merge-base", "--is-ancestor",
                    tip, ref], check=False).returncode == 0 for ref in refs))
                self.assertEqual(self.git(destination, "show", tip + ":tracked.txt"), "unpublished clean commit")
                for stash, content in zip(stashes, reversed(stash_contents), strict=True):
                    self.assertTrue(any(subprocess.run(["git", "-C", str(destination), "merge-base", "--is-ancestor",
                        stash, ref], check=False).returncode == 0 for ref in refs))
                    self.assertEqual(self.git(destination, "show", stash + ":tracked.txt"), content)

    def test_nested_manifest_and_uncommitted_root_manifest_keep_a_fresh_gate(self) -> None:
        _ = self.fresh_base()
        nested = self.source / "scripts/proof_crate"
        nested.mkdir(parents=True)
        _ = (nested / "Cargo.toml").write_text("[workspace]\n")
        _ = self.git(self.source, "add", ".")
        _ = self.git(self.source, "commit", "-qm", "add a nested manifest")
        base = self.git(self.source, "rev-parse", "HEAD")
        _ = (self.source / "Cargo.toml").write_text("[workspace]\n")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(base)) as tree:
            self.assertTrue(tree.is_relative_to(self.gates))
            self.assertTrue((tree / "scripts/proof_crate/Cargo.toml").exists())
            self.assertFalse((tree / "Cargo.toml").exists())
        self.assertTrue(tree.exists())

    def test_committed_root_manifest_reuses_output_despite_working_file_removal(self) -> None:
        (self.source / "Cargo.toml").unlink()
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            output = tree / "target/compiled"
            _ = output.write_text("retained output")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(reused, tree)
            self.assertEqual(output.read_text(), "retained output")
            self.assertEqual((reused / "Cargo.toml").read_text(), "[workspace]\n")

    def test_fresh_gate_applies_working_changes_before_a_merge(self) -> None:
        base = self.fresh_base()
        incoming = self.incoming()
        _ = (self.source / "untracked.txt").write_text("working content\n")
        with copies.standing(self.owner_id, self.source, copies.WorktreeSource(base, self.source),
                             merge=copies.MergeRevision("incoming")) as tree:
            self.assertTrue(tree.is_relative_to(self.gates))
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^2"), incoming)
            self.assertEqual((tree / "incoming.txt").read_text(), "merged content\n")
            self.assertEqual((tree / "untracked.txt").read_text(), "working content\n")
        self.assertTrue(tree.exists())

    def test_fresh_gate_removal_failure_preserves_the_caller_exception(self) -> None:
        base = self.fresh_base()
        failure = ValueError('the gate failed')
        tree = self.gates / '1'
        with self.assertRaises(ValueError) as raised:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(base)) as held:
                self.assertEqual(held, tree)
                _ = (held / 'target/proof').write_text('keep the output')
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual((tree / 'target/proof').read_text(), 'keep the output')
        self.assertEqual(build_folders.build_folder_state(tree), 'free')

    def test_clone_cli_creates_a_task_owned_checkout_without_a_root_busy_mark(self) -> None:
        arguments = ("clone", str(self.source), "--task", "gate:camera")
        blocked_registry = self.base / "unwritable-registry"
        blocked_registry.touch()
        self.write_config({**self.settings, "remote_registry": str(blocked_registry)})
        with self.subTest(holder="unwritable registry"):
            result = self.cli(*arguments, "--name", "camera")
            self.assertEqual(result.returncode, 0, result.stderr)
        self.write_config(self.settings)
        checkout = Path(result.stdout.strip())
        self.assertTrue(checkout.is_relative_to(self.base / "scratch"))
        self.assertEqual((checkout / "tracked.txt").read_text(), "source HEAD\n")
        self.assertFalse((checkout / scratch.BUSY).exists())
        record = scratch.task_of(checkout)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.task, "gate:camera")
        self.assertIs(record.fields["test_hana_clone"], True)
        self.assertEqual(record.fields["source_path"], str(self.source))
        self.assertEqual(record.fields["source_commit"], self.git(checkout, "rev-parse", "HEAD"))
        self.assertEqual([entry.path for entry in scratch.end("gate:camera")], [checkout])
        self.assertTrue(checkout.is_dir())
        self.assertEqual((self.source / "tracked.txt").read_text(), "source HEAD\n")

        for holder in ("gone", "implicit owner", "live", "registration exited", "another owner",
                       "another host", "reused", "unreadable", "malformed start", "replaced clone",
                       "protected checkout", "wrong clone path", "invalid repository", "same owner replacement",
                       "unusual start", "session command", "lease after setup", "implicit lease after setup"):
            with self.subTest(holder=holder), tempfile.TemporaryDirectory(prefix="lease case ",
                                                                         dir=checkout.parent) as directory, \
                    mock.patch.dict(os.environ, {copy_record.ROOT_ENV: directory}), ExitStack() as scope:
                root = Path(directory)
                self.write_config({**self.settings, "remote_registry": str(root / "mirrors")})
                owner = "another-owner" if holder == "another owner" else self.owner_id
                saved_session = "--flag-like" if holder == "session command" else "fixture"
                parent = root
                setup: list[Path] = []
                if holder == "session command":
                    session_root = root / "sessions"
                    parent = session_root / "clone maker"
                    parent.mkdir(parents=True)
                    _ = cast(object, scope.enter_context(mock.patch.dict(os.environ, {session_folder.ROOT_ENV: str(session_root)})))
                if holder in ("lease after setup", "implicit lease after setup"):
                    owner_options = ("--owner", owner) if holder == "lease after setup" else ()
                    result = self.cli(*arguments, "--name", "previous", "--repository", "origin",
                                      "--session", saved_session, *owner_options)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    completed = Path(result.stdout.strip())
                    self.assertEqual((completed / "tracked.txt").read_text(), "source HEAD\n")
                    setup.append(completed)
                with self.clone_lease_fixture(parent, owner, saved_session, setup, second=holder == "gone") as (maker, existing):
                    assert maker.stdin is not None
                    if holder not in ("live", "registration exited"):
                        maker.stdin.close()
                        self.assertEqual(maker.wait(timeout=TEST_HANG_GUARD_SECS), 0)
                    tree = existing[0]
                    if holder in ("registration exited", "protected checkout", "wrong clone path", "malformed start",
                                  "invalid repository", "another host", "same owner replacement"):
                        # The registrar runs separately from the folder lease holder.
                        registration = subprocess.run([
                            sys.executable, str(Path(copies.__file__).resolve()), "register-remote",
                            "--host", "localhost", "--path", str(tree), "--owner", owner,
                            "--repository", "origin", "--session", "fixture",
                        ], text=True, capture_output=True, check=False, timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual(registration.returncode, 0, registration.stderr)
                    if holder in ("reused", "unreadable", "malformed start", "unusual start"):
                        fields = copies.clone_lease_fields(tree)
                        if holder == "unreadable":
                            fields["pid"] = "unknown"
                        elif holder == "malformed start":
                            fields.update(pid=os.getpid(), process_start="bad identity")
                        elif holder == "unusual start":
                            current = process_start.process_start(os.getpid())
                            assert isinstance(current, process_start.Started)
                            fields.update(pid=os.getpid(), process_start=(
                                "0" + current.start if os.uname().sysname == "Linux" else "Sun Feb 31 22:00:00 2020"))
                        else:
                            fields.update(pid=os.getpid(), process_start=(
                                "0" if os.uname().sysname == "Linux" else "Wed Nov  4 22:00:00 2020"))
                        _ = build_folders.lease_path(tree).write_text(json.dumps(fields))
                    elif holder in ("another host", "invalid repository"):
                        path = copies.remote_record_path(copies.RemoteMirror(owner, copies.SelectedRepository("origin"),
                                                                           "localhost", str(tree), saved_session))
                        fields = cast(dict[str, object], json.loads(path.read_text()))
                        if holder == "another host":
                            fields["host"] = "other-host"
                        else:
                            del fields["repo"]
                        _ = path.write_text(json.dumps(fields))
                    elif holder == "replaced clone":
                        copies.mark_mirror(tree, "another-owner", "origin", "fixture")
                    elif holder == "same owner replacement":
                        copies.mark_mirror(tree, owner, "origin", "replacement")
                        copies.register_scratch_clone(tree, copies.CloneSource.UNAPPROVED,
                                                     owner=owner, repository="origin", session="replacement")
                    elif holder == "wrong clone path":
                        _ = scratch.update_record(tree, lambda current: {
                            **(current.fields if isinstance(current, copy_record.Registered) else {}),
                            "clone_path": str(root / "different clone"),
                        })

                    if holder in ("gone", "implicit owner", "reused", "session command", "lease after setup",
                                  "implicit lease after setup"):
                        before = sorted(root.rglob("*"))
                        contents = self.snapshot(root)
                        owner_options = () if holder in ("implicit owner", "implicit lease after setup") else ("--owner", self.owner_id)
                        result = self.cli(*arguments, "--name", "camera", *owner_options)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertEqual(result.stdout, "")
                        lines = result.stderr.splitlines()
                        self.assertEqual(lines[1:1 + len(existing)], [str(path) for path in existing])
                        self.assertEqual(lines[1 + len(existing)], "Run one removal command per path.")
                        commands = [shlex.split(line) for line in lines[2 + len(existing):]]
                        session_options = ([f"--session={saved_session}"] if holder == "session command"
                                           else ["--session", saved_session])
                        self.assertEqual(commands, [[
                            "~/.claude/scripts/lib/py", "~/.claude/scripts/copies/copies.py", "mirror-remove",
                            "--path", str(path), "--owner", self.owner_id, "--repository", "origin",
                            *session_options, "--host", "localhost",
                        ] for path in existing])
                        for path, command in zip(existing, commands, strict=True):
                            removal = copies.parse_copy_arguments(command[2:])
                            self.assertEqual((removal.action, removal.path, removal.owner, removal.repository,
                                              removal.session, removal.host),
                                             ("mirror-remove", str(path), self.owner_id, "origin", saved_session, "localhost"))
                        self.assertEqual(sorted(root.rglob("*")), before)
                        self.assertEqual(self.snapshot(root), contents)
                    else:
                        # The name check runs after the guard and before Git creates another clone.
                        options = [*arguments, "--name", "invalid/name"]
                        result = invoke_cli(copies, lambda: copies.main(options), options, dict(os.environ),
                                            tree if holder == "protected checkout" else self.base)
                        self.assertIn("a name is letters, digits, - and _", result.stderr)

    @contextmanager
    def clone_lease_fixture(self, root: Path, owner: str, session: str, setup: list[Path], *, second: bool
                            ) -> Generator[tuple[subprocess.Popen[str], list[Path]]]:
        """Hold real folder leases until the fixture closes the maker's input."""
        program = """
import json
from contextlib import ExitStack
from pathlib import Path
import sys
import time
from scripts.copies import copies
from scripts.lib import build_folders, scratch

root, owner, session, *names = sys.argv[1:]
with ExitStack() as leases:
    paths = []
    for name in names:
        if Path(name).is_absolute():
            checkout = Path(name)
            spec = build_folders.BuildFolderSpec(kind="legacy-stable", parent=checkout.parent,
                                                suffix_length=0, purpose=checkout.name, owner=owner)
            path = leases.enter_context(build_folders.leased_build_folder(spec))
        else:
            spec = build_folders.BuildFolderSpec(kind="legacy-unique", parent=Path(root),
                                                suffix_length=0, suffix=copies.clone_owner_prefix(owner) + name, owner=owner)
            path = leases.enter_context(build_folders.leased_build_folder(spec))
            scratch.write_record(path, "gate:previous", time.time())
            copies.register_scratch_clone(path, copies.CloneSource.UNAPPROVED, owner=owner,
                                         repository="origin", session=session)
            copies.mark_mirror(path, owner, "origin", session)
            copies.record_clone_setup_lease(path)
        paths.append(str(path))
    print(json.dumps(paths), flush=True)
    sys.stdin.read(1)
"""
        names = tuple(map(str, setup)) if setup else ("old-clone-1", "old-clone-2") if second else ("old-clone",)
        maker: subprocess.Popen[str] = subprocess.Popen(
            [sys.executable, "-c", program, str(root), owner, session, *names],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        with maker:
            assert maker.stdout is not None and maker.stdin is not None
            output = cast(TextIO, maker.stdout)
            self.assertTrue(select.select([output], [], [], TEST_HANG_GUARD_SECS)[0],
                            "The maker did not report the leases.")
            line = output.readline()
            self.assertTrue(line, "The maker exited before it reported the leases.")
            paths = [Path(path) for path in cast(list[str], json.loads(line))]
            try:
                yield maker, paths
            finally:
                if not maker.stdin.closed:
                    maker.stdin.close()
                    self.assertEqual(maker.wait(timeout=TEST_HANG_GUARD_SECS), 0)

    def test_task_end_during_clone_setup_keeps_completion_fields(self) -> None:
        task = "gate:clone-setup"
        ended = time.time()
        initialize = copies.initialize_git

        def end_during_setup(path: Path, source: Path) -> None:
            self.assertEqual([entry.path for entry in scratch.end(task, now=ended)], [path])
            self.assertTrue(path.is_dir())
            initialize(path, source)

        with mock.patch.object(copies, "initialize_git", side_effect=end_during_setup):
            checkout = copies.clone(self.source, task)
        record = scratch.task_of(checkout)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.fields.get("ended"), ended)
        self.assertEqual(record.fields.get("ended_task"), task)
        self.assertIs(record.fields["test_hana_clone"], True)
        self.assertEqual(record.fields["source_path"], str(self.source))
        self.assertEqual(record.fields["source_commit"], self.head)

    def test_ssh_copy_preserves_the_validated_caller_anchor(self) -> None:
        trusted = Path.home() / "rust"
        trusted.mkdir()
        config = Path(os.environ["CONF_DIR"]) / "nix_env.conf"
        _ = write_config_fixture(config, "trusted_home_folders=rust\n")
        binaries = self.base / "bin"
        binaries.mkdir()
        direnv = binaries / "direnv"
        _ = direnv.write_text("#!/bin/sh\nexit 1\n")
        direnv.chmod(0o755)
        fixture_path = str(binaries) + os.pathsep + os.environ["PATH"]

        plain = trusted / "plain"
        _ = self.git(self.source, "clone", "-q", str(self.source), str(plain))
        repository = trusted / "repository"
        _ = self.git(plain, "clone", "-q", str(plain), str(repository))
        for checkout in (repository, self.source):
            _ = (checkout / ".envrc").write_text("export FIXTURE_ENVRC=1\n")
            _ = self.git(checkout, "add", ".envrc")
            _ = self.git(checkout, "commit", "-qm", "Add the approved envrc")
        approved_commit = self.git(repository, "rev-parse", "HEAD")

        outside = self.base / "linked-outside"
        _ = self.git(repository, "worktree", "add", "-q", "--detach", str(outside))
        inside = trusted / "linked-inside"
        _ = self.git(self.source, "worktree", "add", "-q", "--detach", str(inside))
        task_clone = copies.clone(repository, "gate:ssh-envrc")
        _ = self.git(task_clone, "commit", "--allow-empty", "-qm", "Advance the task clone")
        self.assertNotEqual(self.git(task_clone, "rev-parse", "HEAD"), approved_commit)
        readable = copies.clone(plain, "gate:ssh-readable")
        invalid = copies.clone(plain, "gate:ssh-invalid-anchor")
        _ = scratch.update_record(invalid, lambda record: {
            **(record.fields if isinstance(record, copy_record.Registered) else {}),
            "generated_source": True, "source_commit": "f" * len(self.head),
        })
        for caller in (readable, invalid):
            record_path = copy_record.record_path(caller)
            assert record_path is not None
            self.assertIsInstance(copy_record.read_record(record_path), copy_record.Registered)

        cases = (
            ("trusted-envrc", repository, True, str(repository), approved_commit),
            ("linked-outside", outside, True, str(repository), approved_commit),
            ("linked-inside", inside, True, str(inside), self.git(inside, "rev-parse", "HEAD")),
            ("task-clone", task_clone, True, str(repository), approved_commit),
            ("trusted-no-envrc", plain, True, str(plain), self.head),
            ("untrusted-readable-record", readable, False, None, None),
            ("approved-with-invalid-anchor", invalid, True, None, None),
        )
        for name, caller, caller_approved, anchor_path, anchor_commit in cases:
            with self.subTest(caller=name), mock.patch.dict(os.environ, {"PATH": fixture_path}):
                self.assertEqual(offload.trusted_linux_source(caller), caller_approved)
                with copies.requested_source(caller, copies.CommittedSource("HEAD"), copies.NO_MERGE) as source:
                    _ = (source / "tracked.txt").write_text("the admitted change\n")
                    tree = copies.staged_tree(source, include_worktree=True)
                    with copies.remote_step_source(source, tree, caller, SSH_SOURCE_TRUST) as checkout:
                        self.assertEqual((checkout / "tracked.txt").read_text(), "the admitted change\n")
                        approved = anchor_path is not None
                        self.assertEqual(offload.trusted_linux_source(checkout), approved,
                                         "SSH copy approval differs from its validated anchor")
                        record = copy_record.read_record(checkout / copy_record.RECORD)
                        assert isinstance(record, copy_record.Registered)
                        self.assertEqual(record.fields.get("source_path"), anchor_path)
                        self.assertEqual(record.fields.get("source_commit"), anchor_commit)
                        self.assertEqual("generated_source" in record.fields, approved)

    def test_ssh_copy_checks_envrc_against_the_prepared_merge(self) -> None:
        trusted = Path.home() / "rust"
        trusted.mkdir()
        _ = write_config_fixture(Path(os.environ["CONF_DIR"]) / "nix_env.conf", "trusted_home_folders=rust\n")
        binaries = self.base / "bin"
        binaries.mkdir()
        direnv = binaries / "direnv"
        _ = direnv.write_text("#!/bin/sh\nexit 1\n")
        direnv.chmod(0o755)
        fixture_path = str(binaries) + os.pathsep + os.environ["PATH"]
        initial = "export FIRST=0\n# Keep the two edits separate.\n\n# More settings.\n\nexport LAST=0\n"
        incoming_envrc = initial.replace("LAST=0", "LAST=1")

        for name in ("incoming-envrc", "three-way-envrc", "stale-origin-envrc", "task-clone-envrc",
                     "worktree-envrc", "staged-envrc", "worktree-with-committed-envrc"):
            with self.subTest(merge=name), mock.patch.dict(os.environ, {"PATH": fixture_path}):
                repository = trusted / name
                _ = self.git(self.source, "clone", "-q", str(self.source), str(repository))
                _ = (repository / ".envrc").write_text(initial)
                _ = self.git(repository, "add", ".envrc")
                _ = self.git(repository, "commit", "-qm", "Add the envrc")
                base = self.git(repository, "rev-parse", "HEAD")
                stale = name == "stale-origin-envrc"
                publisher = repository
                if stale:
                    upstream = self.base / "upstream.git"
                    publisher = self.base / "publisher"
                    _ = self.git(repository, "clone", "-q", "--bare", str(repository), str(upstream))
                    _ = self.git(repository, "remote", "set-url", "origin", str(upstream))
                    _ = self.git(repository, "clone", "-q", str(upstream), str(publisher))
                    _ = self.git(repository, "update-ref", "refs/remotes/origin/incoming", base)

                _ = self.git(publisher, "switch", "-qc", "incoming")
                staged = name == "staged-envrc"
                worktree = name == "worktree-with-committed-envrc"
                edited = name == "worktree-envrc" or staged
                expected = incoming_envrc
                if edited:
                    _ = (publisher / "merged.txt").write_text("merge without an envrc change\n")
                    _ = self.git(publisher, "add", "merged.txt")
                    expected = initial.replace("LAST=0", "LAST=2")
                else:
                    _ = (publisher / ".envrc").write_text(incoming_envrc)
                    _ = self.git(publisher, "add", ".envrc")
                _ = self.git(publisher, "commit", "-qm", "Prepare the incoming change")
                incoming = self.git(publisher, "rev-parse", "HEAD")
                _ = self.git(publisher, "switch", "-q", "main")
                revision = "incoming"
                if stale:
                    _ = self.git(publisher, "push", "-q", "origin", "incoming")
                    with self.assertRaises(copies.GitExit):
                        _ = copies.commit_at(repository, incoming)
                    revision = "origin/incoming"
                if name == "three-way-envrc":
                    local_envrc = initial.replace("FIRST=0", "FIRST=1")
                    _ = (repository / ".envrc").write_text(local_envrc)
                    _ = self.git(repository, "commit", "-qam", "Change the first setting")
                    expected = incoming_envrc.replace("FIRST=0", "FIRST=1")

                caller = repository
                if name == "task-clone-envrc":
                    caller = copies.clone(repository, "gate:ssh-merge-envrc")
                    _ = self.git(caller, "commit", "--allow-empty", "-qm", "Advance the task clone")
                    _ = self.git(caller, "update-ref", "refs/heads/incoming", incoming)
                policy: copies.SourcePolicy = copies.CommittedSource("HEAD")
                merge: copies.MergePolicy = copies.MergeRevision(revision)
                if edited:
                    _ = (caller / ".envrc").write_text(expected)
                    if staged:
                        _ = self.git(caller, "add", ".envrc")
                        policy = copies.StagedSource(copies.staged_tree(caller))
                        merge = copies.NO_MERGE
                    else:
                        policy = copies.WorktreeSource("HEAD", caller)
                if worktree:
                    _ = (caller / "tracked.txt").write_text("uncommitted worktree bytes\n")
                    policy = copies.WorktreeSource("HEAD", caller)
                self.assertTrue(offload.trusted_linux_source(caller))

                before: dict[Path, tuple[str, bytes]] = {}
                for watched in dict.fromkeys((caller, repository)):
                    fetch_head = Path(self.git(watched, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD"))
                    _ = fetch_head.write_bytes(b"keep the existing FETCH_HEAD\n")
                    before[fetch_head] = self.git(watched, "for-each-ref"), fetch_head.read_bytes()
                with copies.requested_source(caller, policy, merge) as source:
                    prepared = self.git(source, "rev-parse", "HEAD")
                    if not staged:
                        self.assertEqual(self.git(source, "rev-parse", "HEAD^2"), incoming)
                    self.assertEqual((source / ".envrc").read_text(), expected)
                    _ = (source / "tracked.txt").write_text("change after snapshot preparation\n")
                    tree = copies.staged_tree(source, include_worktree=True)
                    with copies.remote_step_source(source, tree, caller, SSH_SOURCE_TRUST) as checkout:
                        self.assertEqual((checkout / ".envrc").read_text(), expected)
                        self.assertEqual(offload.trusted_linux_source(checkout), not edited,
                                         "SSH envrc approval differs from the prepared merge")
                        record = copy_record.read_record(checkout / copy_record.RECORD)
                        assert isinstance(record, copy_record.Registered)
                        self.assertIs(record.fields.get("generated_source"), True)
                        if edited:
                            self.assertNotIn("source_commit", record.fields)
                            self.assertNotIn("source_path", record.fields)
                        else:
                            self.assertEqual(record.fields["source_path"], str(repository))
                            reference = record.fields["source_commit"]
                            assert isinstance(reference, str)
                            self.assertEqual(copies.commit_at(repository, reference), reference)
                            if worktree:
                                self.assertNotEqual(reference, prepared)
                                self.assertEqual(self.git(repository, "show", f"{reference}:tracked.txt"),
                                                 self.git(repository, "show", "HEAD:tracked.txt"))
                                self.assertEqual(self.git(repository, "rev-parse", f"{reference}^2"), incoming)
                            else:
                                self.assertEqual(reference, prepared)
                for watched in dict.fromkeys((caller, repository)):
                    fetch_head = Path(self.git(watched, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD"))
                    self.assertEqual((self.git(watched, "for-each-ref"), fetch_head.read_bytes()), before[fetch_head])

    def test_clone_git_child_keeps_both_destination_locks_after_parent_death(self) -> None:
        task = "gate:orphan-clone"
        child = """import os, sys
ready, release, *locks = map(int, sys.argv[2:])
os.write(ready, os.fsencode(sys.argv[1]) + b'\\n')
os.read(release, 1)
for lock in locks:
    os.close(lock)
"""
        maker = """import subprocess, sys
from pathlib import Path
from unittest.mock import patch
from scripts.copies import copies

source, task, child, ready, release = sys.argv[1:]
run = subprocess.run
ready, release = int(ready), int(release)

def pause_clone(arguments, **options):
    if arguments[0] != 'git' or arguments[5:8] != ['-C', source, 'clone']:
        return run(arguments, **options)
    locks = options['pass_fds']
    options['pass_fds'] = (*locks, ready, release)
    destination = Path(arguments[-1]).parent
    return run([sys.executable, '-c', child, str(destination), str(ready), str(release), *map(str, locks)], **options)

with patch.object(subprocess, 'run', pause_clone):
    copies.clone(Path(source), task, 'orphan')
"""
        with ExitStack() as pipes:
            ready_read, ready_write = os.pipe()
            ready = pipes.enter_context(os.fdopen(ready_read, "r", encoding="utf-8"))
            ready_writer = pipes.enter_context(os.fdopen(ready_write, "wb"))
            release_read, release_write = os.pipe()
            release_reader = pipes.enter_context(os.fdopen(release_read, "rb"))
            release = pipes.enter_context(os.fdopen(release_write, "wb"))
            process = pipes.enter_context(subprocess.Popen(
                [sys.executable, "-c", maker, str(self.source), task, child, str(ready_write), str(release_read)],
                env={**os.environ, session_folder.ROOT_ENV: str(self.base / "session")},
                pass_fds=(ready_write, release_read), stderr=subprocess.PIPE))
            ready_writer.close()
            release_reader.close()
            try:
                self.assertTrue(select.select([ready], [], [], TEST_HANG_GUARD_SECS)[0], "Git did not start")
                line = ready.readline().strip()
                if not line:
                    _, error = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                    self.fail(f"The clone maker exited before Git started: {error.decode(errors='replace')}")
                checkout = Path(line)
                self.assertTrue(checkout.is_relative_to(self.base / "scratch"))
                process.kill()
                _ = process.wait(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(build_folders.build_folder_state(checkout), "held")
                with scratch.busy_path(checkout).open("rb") as other, self.assertRaises(BlockingIOError):
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = scratch.remove_task_copy(checkout, task, time.time())
                self.assertEqual(result, scratch.KeptFolder(checkout, scratch.BUSY_REASON))
                self.assertTrue(checkout.is_dir())
                release.close()
                self.assertTrue(select.select([ready], [], [], TEST_HANG_GUARD_SECS)[0], "Git did not exit")
                self.assertEqual(ready.read(), "")
                self.assertTrue(scratch.end_copy(checkout, task, time.time()))
                self.assertFalse(checkout.exists())
            finally:
                release.close()
                if process.poll() is None:
                    process.kill()
                    _ = process.wait(timeout=TEST_HANG_GUARD_SECS)

    def test_removal_retry_claims_a_missing_busy_mark_and_refuses_a_linked_checkout(self) -> None:
        task = "gate:retry-source"
        clone = copies.clone(self.source, task, "source")
        with mock.patch.object(scratch, "remove", side_effect=OSError("stop after removal intent")):
            with self.assertRaisesRegex(OSError, "stop after removal intent"):
                _ = scratch.remove_task_copy(clone, task, time.time())
        self.assertTrue(scratch.removal_mark(clone).is_file())
        busy = scratch.busy_path(clone)
        busy.unlink()
        destination = self.base / "late-worktree"
        request = build_folders.LinkedEditableRepository(destination, clone,
                                                         build_folders.NewWorktreeBranch("late", self.head))
        with self.assertRaisesRegex(BlockingIOError, "source removal has already started"):
            _ = build_folders.create_build_folder(request)
        self.assertFalse(destination.exists())
        self.assertEqual(self.git(clone, "branch", "--list", "late"), "")

        work = clone / "new-work" / scratch.BUSY
        work.parent.mkdir()
        _ = work.write_text("Keep this file")
        kept = scratch.remove_task_copy(clone, task, time.time())
        self.assertIsInstance(kept, scratch.KeptFolder)
        self.assertEqual(work.read_text(), "Keep this file")
        work.unlink()
        work.parent.rmdir()
        busy.unlink(missing_ok=True)

        remove = scratch.remove

        original = clone.stat()

        def check_retry(path: Path) -> None:
            self.assertTrue(os.path.samestat(path.stat(), original))
            with (path / busy.relative_to(clone)).open("rb") as other, self.assertRaises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                _ = build_folders.create_build_folder(request)
            remove(path)

        with mock.patch.object(scratch, "remove", side_effect=check_retry):
            self.assertTrue(scratch.end_copy(clone, task, time.time()))
        self.assertFalse(scratch.removal_mark(clone).exists())
        self.assertFalse(destination.exists())

    def clone_trust_files(self, trusted_homes: tuple[int, ...] = (0, 1, 2)) -> tuple[Path, ...]:
        home = Path.home()
        (home / ".claude").mkdir(exist_ok=True)
        paths = [home / ".claude.json"]
        for number in (1, 2):
            account = home / f".claude{number}"
            account.mkdir(exist_ok=True)
            paths.append(account / ".claude.json")
        for index, path in enumerate(paths):
            _ = path.write_text(json.dumps({"keep": [1, 2], "projects": {
                str(self.source.resolve()): {"hasTrustDialogAccepted": index in trusted_homes, "keep": "source"},
                "/other": {"hasTrustDialogAccepted": False, "keep": [index, {"nested": True}]},
            }}))
            path.chmod(0o640)
        return tuple(paths)

    def trust_projects(self, path: Path) -> dict[str, dict[str, object]]:
        return cast(dict[str, dict[str, object]], accounts.read_json_object(path)["projects"])

    def assert_clone_trust(self, checkout: Path, paths: tuple[Path, ...]) -> None:
        key = str(checkout.resolve())
        record = scratch.task_of(checkout)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.fields["claude_trust_folder"], key)
        for path in paths:
            self.assertEqual(self.trust_projects(path)[key], {"hasTrustDialogAccepted": True})

    def test_clone_inherits_trust_in_every_home_and_preserves_other_values(self) -> None:
        paths = self.clone_trust_files()
        before = {path: accounts.read_json_object(path) for path in paths}
        checkout = copies.clone(self.source, "gate:trusted")
        self.assert_clone_trust(checkout, paths)
        for path in paths:
            after = accounts.read_json_object(path)
            projects = self.trust_projects(path)
            del projects[str(checkout.resolve())]
            self.assertEqual(projects, before[path]["projects"])
            self.assertEqual(after["keep"], before[path]["keep"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_clone_trusted_in_only_one_home_gains_trust_in_every_home(self) -> None:
        for index in range(3):
            with self.subTest(home=index):
                paths = self.clone_trust_files((index,))
                checkout = copies.clone(self.source, f"gate:trusted-{index}")
                self.assert_clone_trust(checkout, paths)

    def test_clone_of_an_untrusted_source_changes_no_account_file(self) -> None:
        paths = self.clone_trust_files(())
        before = {path: path.read_bytes() for path in paths}
        checkout = copies.clone(self.source, "gate:untrusted")
        record = scratch.task_of(checkout)
        assert isinstance(record, copy_record.Registered)
        self.assertNotIn("claude_trust_folder", record.fields)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_clone_uses_the_main_checkout_trust_for_a_linked_source(self) -> None:
        paths = self.clone_trust_files((2,))
        linked = self.base / "linked-source"
        _ = self.git(self.source, "worktree", "add", "--detach", str(linked), "HEAD")
        checkout = copies.clone(linked, "gate:linked-trust")
        self.assert_clone_trust(checkout, paths)

    def test_clone_skips_a_home_without_a_trust_file(self) -> None:
        paths = self.clone_trust_files((0,))
        paths[2].unlink()
        errors = io.StringIO()
        with redirect_stderr(errors):
            checkout = copies.clone(self.source, "gate:missing-trust")
        self.assert_clone_trust(checkout, paths[:2])
        self.assertFalse(paths[2].exists())
        self.assertEqual(list(paths[2].parent.iterdir()), [])
        self.assertEqual(errors.getvalue(), "")

    def test_clone_reports_one_bad_home_and_updates_other_homes(self) -> None:
        paths = self.clone_trust_files((1,))
        _ = paths[0].write_text("{bad JSON")
        errors = io.StringIO()
        with redirect_stderr(errors):
            checkout = copies.clone(self.source, "gate:bad-trust")
        self.assert_clone_trust(checkout, paths[1:])
        self.assertEqual(paths[0].read_text(), "{bad JSON")
        self.assertEqual(len(errors.getvalue().splitlines()), 1)
        self.assertTrue(errors.getvalue().startswith("accounts: anomaly:"))

    def add_clone_project_settings(self, checkout: Path, paths: tuple[Path, ...]) -> None:
        key = str(checkout.resolve())
        for path in paths:
            document = accounts.read_json_object(path)
            projects = cast(dict[str, dict[str, object]], document["projects"])
            projects[key].update({"keep": ["clone setting"], "lastCost": 1.5, "exampleFiles": ["tracked.txt"]})
            projects[key + "-neighbor"] = {"keep": "neighbor"}
            projects[key + "/child"] = {"hasTrustDialogAccepted": True}
            _ = path.write_text(json.dumps(document))

    def assert_clone_project_removed(self, checkout: Path, paths: tuple[Path, ...]) -> None:
        key = str(checkout.resolve())
        for path in paths:
            projects = self.trust_projects(path)
            self.assertNotIn(key, projects)
            self.assertEqual(projects[key + "-neighbor"], {"keep": "neighbor"})
            self.assertEqual(projects[key + "/child"], {"hasTrustDialogAccepted": True})
            self.assertIn("/other", projects)
            self.assertIn(str(self.source.resolve()), projects)

    def test_mirror_receiver_names_the_check_that_refused_it(self) -> None:
        tree = self.base / "mirrors" / "hana"
        tree.mkdir(parents=True)
        _ = (tree / floor_claims.MIRROR_SESSION).write_text("reader\n")
        receive = ("mirror-receive", "--path", str(tree), "--owner", "verify",
                   "--repository", "hana", "--session", "writer", "--", sys.executable, "-c", "pass")
        with floor_claims.mirror_lock_path(tree).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            refused = self.cli(*receive)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("busy; held by session reader (mirror lease)", refused.stderr)

        processes = copies.MirrorProcessScan(tmp_sweep.ProcessUsesUnavailable(), frozenset())
        with mock.patch.object(copies, "scan_mirror_processes", return_value=processes):
            refused = self.cli(*receive)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("busy; held by session reader (mirror process check)", refused.stderr)

    def test_copies_removal_clears_clone_trust_before_deleting_the_folder(self) -> None:
        paths = self.clone_trust_files()
        checkout = copies.clone(self.source, "gate:remove-trust")
        self.add_clone_project_settings(checkout, paths)
        copies.mark_mirror(checkout, "clone-owner", "hana", "fixture")
        remove = scratch.remove

        def delete(folder: Path) -> None:
            self.assertTrue(scratch.removal_mark(checkout).exists())
            self.assert_clone_project_removed(checkout, paths)
            remove(folder)

        processes = copies.MirrorProcessScan(tmp_sweep.ProcessUsesKnown([]), frozenset())
        with (mock.patch.object(copies, "scan_mirror_processes", return_value=processes),
              mock.patch.object(copies, "mirror_metadata_locks",
                                return_value=nullcontext(floor_claims.ClaimSnapshot(frozenset()))),
              mock.patch.object(scratch, "remove", side_effect=delete)):
            result = self.cli("mirror-remove", "--path", str(checkout), "--owner", "clone-owner",
                              "--repository", "hana", "--session", "fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(checkout.exists())
        self.assert_clone_project_removed(checkout, paths)

    def test_mirror_retry_keeps_authority_until_absent_source_cargo_recovery_finishes(self) -> None:
        checkout = copies.clone(self.source, "gate:mirror-recovery")
        copies.mark_mirror(checkout, "clone-owner", "hana", "fixture")
        authority = floor_claims.removal_authority_path(checkout)
        cargo_mark, task_mark = scratch.cargo_removal_mark(checkout), scratch.removal_mark(checkout)
        scratch.atomic_record(cargo_mark, [])
        processes = copies.MirrorProcessScan(tmp_sweep.ProcessUsesKnown([]), frozenset())
        with (mock.patch.object(copies, "scan_mirror_processes", return_value=processes),
              mock.patch.object(copies, "mirror_metadata_locks",
                                return_value=nullcontext(floor_claims.ClaimSnapshot(frozenset())))):
            with mock.patch.object(scratch, "finish_removal", side_effect=OSError("stop after source deletion")):
                first = self.cli("mirror-remove", "--path", str(checkout), "--owner", "clone-owner",
                                 "--repository", "hana", "--session", "fixture")
            self.assertNotEqual(first.returncode, 0)
            self.assertFalse(checkout.exists())
            self.assertTrue(authority.is_file())
            with mock.patch.object(scratch, "clean_cargo_outputs", side_effect=scratch.CargoOutputKept("Cargo refuses recovery")):
                refused = self.cli("mirror-remove", "--path", str(checkout), "--owner", "clone-owner",
                                   "--repository", "hana", "--session", "fixture")
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("Cargo refuses recovery", refused.stderr)
            self.assertTrue(authority.is_file())
            self.assertTrue(task_mark.is_file())
            self.assertTrue(cargo_mark.is_file())
            finished = self.cli("mirror-remove", "--path", str(checkout), "--owner", "clone-owner",
                                "--repository", "hana", "--session", "fixture")
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertFalse(authority.exists())
        self.assertFalse(task_mark.exists())
        self.assertFalse(cargo_mark.exists())

    def test_temporary_review_keeps_clone_trust(self) -> None:
        paths = self.clone_trust_files()
        checkout = copies.clone(self.source, "gate:sweep-trust")
        self.add_clone_project_settings(checkout, paths)
        before = tuple(path.read_bytes() for path in paths)
        entry = tmp_sweep.RemovableEntry(str(checkout), str(checkout.parent), time.time(), 1)
        result = tmp_sweep.Sweep()
        plan = removal_guard.RemovalList(removal_guard.Tool.TEMPORARY)
        self.assertFalse(tmp_sweep.remove_entry(entry, result, False, plan=plan))
        self.assertEqual(list(plan.rows), [checkout])
        self.assertTrue(checkout.is_dir())
        self.assertEqual(result.kept.get("manual review"), [str(checkout)])
        self.assertEqual(tuple(path.read_bytes() for path in paths), before)

    def test_folder_audit_clears_clone_trust(self) -> None:
        paths = self.clone_trust_files()
        checkout = copies.clone(self.source, "gate:audit-trust")
        self.add_clone_project_settings(checkout, paths)
        self.assertEqual(folder_audit.remove(checkout, time.time(), time.time()), folder_audit.REMOVED)
        self.assertFalse(checkout.exists())
        self.assert_clone_project_removed(checkout, paths)

    def test_task_end_clears_clone_trust(self) -> None:
        paths = self.clone_trust_files()
        checkout = copies.clone(self.source, "gate:end-trust")
        self.add_clone_project_settings(checkout, paths)
        self.assertTrue(scratch.end_copy(checkout, "gate:end-trust", time.time()))
        self.assert_clone_project_removed(checkout, paths)

    def test_mirror_removal_checks_for_user_changes(self) -> None:
        for state in ("untracked", "tracked"):
            with self.subTest(state=state):
                task = f"gate:mirror-work-{state}"
                checkout = copies.clone(self.source, task)
                copies.mark_mirror(checkout, "clone-owner", "hana", "fixture")
                self.assertEqual(self.git(checkout, "status", "--porcelain"), "")
                for name in floor_claims.MIRROR_METADATA:
                    self.assertTrue((checkout / name).is_file())
                work = checkout / ("new-work.txt" if state == "untracked" else "tracked.txt")
                original = work.read_text() if work.exists() else ""
                _ = work.write_text("user work in a registered mirror")
                result = scratch.remove_task_copy(checkout, task, time.time())
                self.assertIsInstance(result, scratch.KeptFolder)
                assert isinstance(result, scratch.KeptFolder)
                self.assertEqual(work.read_text(), "user work in a registered mirror")
                self.assertIn("uncommitted changes", result.reason)
                if state == "untracked":
                    work.unlink()
                else:
                    _ = work.write_text(original)
                self.assertTrue(scratch.end_copy(checkout, task, time.time()))
                self.assertFalse(checkout.exists())

    def test_old_mirror_cleanup_checks_user_work_without_metadata_exclusions(self) -> None:
        for state in ("clean", "untracked", "tracked", "tracked-metadata", "invalid-record", "wrong-session"):
            with self.subTest(state=state):
                task = f"gate:old-mirror-{state}"
                checkout = copies.clone(self.source, task)
                copies.mark_mirror(checkout, "clone-owner", "hana", "fixture")
                exclude = checkout / ".git/info/exclude"
                patterns = {f"/{name}" for name in floor_claims.MIRROR_METADATA}
                _ = exclude.write_text("\n".join(line for line in exclude.read_text().splitlines()
                                               if line not in patterns) + "\n")
                status = self.git(checkout, "status", "--porcelain").splitlines()
                self.assertEqual(set(status), {f"?? {name}" for name in floor_claims.MIRROR_METADATA})
                work = checkout / "new-work.txt"
                if state == "tracked":
                    work = checkout / "tracked.txt"
                elif state in ("tracked-metadata", "invalid-record"):
                    work = checkout / floor_claims.MIRROR_RECORD
                elif state == "wrong-session":
                    work = checkout / floor_claims.MIRROR_SESSION
                if state == "tracked-metadata":
                    _ = self.git(checkout, "add", "--", work.name)
                    _ = self.git(checkout, "commit", "-qm", "Track the mirror record")
                    fields = cast(dict[str, object], json.loads(work.read_text()))
                    _ = work.write_text(json.dumps({**fields, "user_note": "keep this work"}))
                elif state != "clean":
                    _ = work.write_text("keep this work")
                result = scratch.remove_task_copy(checkout, task, time.time())
                if state == "clean":
                    self.assertIsInstance(result, scratch.RemovedCopy)
                    self.assertFalse(checkout.exists())
                else:
                    self.assertIsInstance(result, scratch.KeptFolder)
                    assert isinstance(result, scratch.KeptFolder)
                    self.assertTrue(checkout.is_dir())
                    self.assertIn("uncommitted changes", result.reason)
                    self.assertIn("keep this work", work.read_text())
                    self.assertFalse(scratch.removal_mark(checkout).exists())

    def test_removal_keeps_the_clone_when_a_read_only_account_denies_trust_removal(self) -> None:
        paths = self.clone_trust_files()
        checkout = copies.clone(self.source, "gate:denied-trust")
        _ = self.enterContext(fake_cargo_output_roots(CargoOutputRoots(checkout, checkout / "target", checkout / "target")))
        blocked = paths[1]
        blocked.chmod(0o444)
        blocked.parent.chmod(0o555)
        try:
            with self.assertRaisesRegex(scratch.ScratchError, "PermissionError") as failure:
                _ = scratch.remove_task_copy(checkout, "gate:denied-trust", time.time())
            self.assertTrue(checkout.is_dir())
            self.assertFalse(scratch.removal_mark(checkout).exists())
            self.assertIn(str(blocked), str(failure.exception))
            self.assertIn(str(checkout.resolve()), self.trust_projects(blocked))
        finally:
            blocked.parent.chmod(0o755)
            blocked.chmod(0o640)
        self.assertTrue(scratch.end_copy(checkout, "gate:denied-trust", time.time()))
        self.assertFalse(checkout.exists())
        self.assertFalse(scratch.removal_mark(checkout).exists())
        for path in paths:
            self.assertNotIn(str(checkout.resolve()), self.trust_projects(path))

    def test_removal_rechecks_unsaved_work_after_a_denied_trust_write(self) -> None:
        paths = self.clone_trust_files()
        task = "gate:retry-unsaved"
        checkout = copies.clone(self.source, task)
        _ = self.enterContext(fake_cargo_output_roots(CargoOutputRoots(checkout, checkout / "target", checkout / "target")))
        blocked = paths[1]
        blocked.chmod(0o444)
        blocked.parent.chmod(0o555)
        work = checkout / "new-work.txt"
        try:
            with self.assertRaisesRegex(scratch.ScratchError, "PermissionError"):
                _ = scratch.remove_task_copy(checkout, task, time.time())
            self.assertTrue(checkout.is_dir())
            record = scratch.task_of(checkout)
            assert isinstance(record, copy_record.Registered)
            self.assertEqual(record.fields["claude_trust_folder"], str(checkout.resolve()))
            _ = work.write_text("work written after the failed trust update")
        finally:
            blocked.parent.chmod(0o755)
            blocked.chmod(0o640)
        result = scratch.remove_task_copy(checkout, task, time.time())
        self.assertIsInstance(result, scratch.KeptFolder)
        assert isinstance(result, scratch.KeptFolder)
        self.assertEqual(work.read_text(), "work written after the failed trust update")
        self.assertIn("uncommitted changes", result.reason)
        self.assertFalse(scratch.removal_mark(checkout).exists())
        self.assertIn(str(checkout.resolve()), self.trust_projects(blocked))
        work.unlink()
        self.assertTrue(scratch.end_copy(checkout, task, time.time()))
        self.assertFalse(checkout.exists())
        for path in paths:
            self.assertNotIn(str(checkout.resolve()), self.trust_projects(path))

    def test_removal_rechecks_unsaved_work_after_cargo_review_fails(self) -> None:
        task = "gate:retry-cargo-cleanup"
        checkout = copies.clone(self.source, task)
        cache = self.base / "external-cargo-output"
        cache.mkdir()
        _ = (cache / "output").write_text("saved build output")
        roots = CargoOutputRoots(checkout, checkout / "target", cache)
        _ = self.enterContext(fake_cargo_output_roots(roots))
        with mock.patch.object(scratch, "cargo_checkout_peers", return_value=()):
            with (mock.patch.object(removal_guard.RemovalList, "publish", side_effect=OSError("Cargo review failed")),
                  self.assertRaisesRegex(OSError, "Cargo review failed")):
                _ = scratch.remove_task_copy(checkout, task, time.time())
            self.assertTrue(checkout.is_dir())
            self.assertTrue(scratch.cargo_removal_mark(checkout).exists())
            work = checkout / "new-work.txt"
            _ = work.write_text("work written after Cargo review failed")

            result = scratch.remove_task_copy(checkout, task, time.time())
            self.assertIsInstance(result, scratch.KeptFolder)
            assert isinstance(result, scratch.KeptFolder)
            self.assertEqual(work.read_text(), "work written after Cargo review failed")
            self.assertIn("uncommitted changes", result.reason)
            self.assertFalse(scratch.removal_mark(checkout).exists())
            self.assertTrue(scratch.cargo_removal_mark(checkout).exists())
            self.assertEqual((cache / "output").read_text(), "saved build output")

            work.unlink()
            self.assertTrue(scratch.end_copy(checkout, task, time.time()))
        self.assertFalse(checkout.exists())
        self.assertEqual((cache / "output").read_text(), "saved build output")
        [plan] = (Path.home() / removal_guard.LIST_DIRECTORY).glob("*.jsonl")
        [line] = plan.read_text().splitlines()
        row = removal_guard.ListedEntry.parse(line)
        self.assertEqual((row.tool, row.path, row.fingerprint), (removal_guard.Tool.CARGO, cache, ""))
        self.assertFalse(scratch.removal_mark(checkout).exists())
        self.assertFalse(scratch.cargo_removal_mark(checkout).exists())

    def test_removal_retries_with_only_the_external_mark_and_changes_no_account_file(self) -> None:
        paths = self.clone_trust_files()
        checkout = copies.clone(self.source, "gate:retry-trust")
        self.add_clone_project_settings(checkout, paths)

        def stopped(folder: Path) -> None:
            (folder / copy_record.RECORD).unlink()
            raise OSError("stopped removal")

        with mock.patch.object(scratch, "remove", side_effect=stopped):
            with self.assertRaisesRegex(OSError, "stopped removal"):
                scratch.remove_marked(checkout)
        self.assertTrue(scratch.removal_mark(checkout).exists())
        self.assert_clone_project_removed(checkout, paths)
        before = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}
        scratch.remove_marked(checkout)
        self.assertFalse(checkout.exists())
        self.assertFalse(scratch.removal_mark(checkout).exists())
        self.assertEqual({path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}, before)

    def test_clone_records_the_trust_key_before_writing_account_files(self) -> None:
        paths = self.clone_trust_files()
        before = {path: path.read_bytes() for path in paths}
        with mock.patch.object(accounts, "set_claude_folder_trust", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                _ = copies.clone(self.source, "gate:stop-before-trust")
        folder = next(folder.path for folder in scratch.folders() if folder.task == "gate:stop-before-trust")
        record = scratch.task_of(folder)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.fields["claude_trust_folder"], str(folder.resolve()))
        self.assertTrue(scratch.end_copy(folder, "gate:stop-before-trust", time.time()))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_removal_without_a_recorded_trust_key_keeps_account_trust(self) -> None:
        paths = self.clone_trust_files(())
        checkout = copies.clone(self.source, "gate:no-trust-field")
        self.assertIsInstance(accounts.set_claude_folder_trust(checkout, trusted=True), accounts.FolderTrustComplete)
        before = {path: path.read_bytes() for path in paths}
        self.assertTrue(scratch.end_copy(checkout, "gate:no-trust-field", time.time()))
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_clone_trust_key_survives_task_transfer_and_renewal(self) -> None:
        paths = self.clone_trust_files()
        checkout = copies.clone(self.source, "gate:original")
        scratch.hand_over(checkout, "next-unit", "gate:transferred")
        scratch.renew(checkout)
        self.assert_clone_trust(checkout, paths)
        record = scratch.task_of(checkout)
        assert isinstance(record, copy_record.Registered)
        fields = scratch.fields_for_task(record.fields, "gate:another")
        self.assertEqual(fields["claude_trust_folder"], str(checkout.resolve()))

    def shared_source_commit(self) -> tuple[Path, str]:
        source = self.base / "shared-source"
        _ = self.git(self.base, "clone", "--shared", "-q", str(self.source), str(source))
        for key in ("user.name", "user.email"):
            _ = self.git(source, "config", key, self.git(self.source, "config", key))
        _ = self.git(source, "checkout", "-q", "--detach")
        _ = (source / "tracked.txt").write_text("shared source commit\n")
        _ = self.git(source, "commit", "-qam", "commit only in shared source")
        return source, self.git(source, "rev-parse", "HEAD")

    def test_clone_keeps_a_commit_that_exists_only_in_its_shared_source(self) -> None:
        source, commit = self.shared_source_commit()
        checkout = copies.clone(source, task="gate:shared-source")
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), commit)
        self.assertEqual((checkout / "tracked.txt").read_text(), "shared source commit\n")
        record = scratch.task_of(checkout)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.fields["source_commit"], commit)

    def test_workspace_selects_a_detached_commit_that_exists_only_in_its_shared_source(self) -> None:
        source, commit = self.shared_source_commit()
        with registered_workspace(self.owner_id, source, commit, name="detached", branch="worker") as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), commit)
            self.assertEqual((tree / "tracked.txt").read_text(), "shared source commit\n")
            record = scratch.task_of(tree)
            assert isinstance(record, copy_record.Registered)
            self.assertEqual(record.fields["source_commit"], commit)

    def test_clone_cli_with_one_until_value_ends_only_the_selected_clone(self) -> None:
        checkouts: list[Path] = []
        records: list[copy_record.Registered] = []
        for name in ("first", "second"):
            result = self.cli("clone", str(self.source), "--until", "2099-01-01 09:30", "--name", name)
            self.assertEqual(result.returncode, 0, result.stderr)
            checkout = Path(result.stdout.strip())
            record = scratch.task_of(checkout)
            assert isinstance(record, copy_record.Registered)
            checkouts.append(checkout)
            records.append(record)
        self.assertNotEqual(records[0].task, records[1].task)

        self.assertEqual([entry.path for entry in scratch.end(records[0].task)], [checkouts[0]])
        self.assertTrue(checkouts[0].is_dir())
        self.assertEqual((checkouts[1] / "tracked.txt").read_text(), "source HEAD\n")
        self.assertEqual(self.git(checkouts[1], "rev-parse", "HEAD"), self.head)
        self.assertEqual(scratch.task_of(checkouts[1]), records[1])

    def symlink_scratch_root(self) -> Path:
        physical = self.base / "physical-scratch"
        physical.mkdir()
        alias = self.base / "scratch-alias"
        alias.symlink_to(physical, target_is_directory=True)
        os.environ["SCRATCH_ROOT"] = str(alias)
        return alias

    def test_clone_through_symlink_scratch_root(self) -> None:
        alias = self.symlink_scratch_root()
        result = self.cli("clone", str(self.source), "--task", "gate:symlink", "--name", "camera")
        self.assertEqual(result.returncode, 0, result.stderr)
        checkout = Path(result.stdout.strip())
        self.assertTrue(checkout.is_relative_to(alias))
        self.assertNotEqual(checkout, checkout.resolve())
        self.assertEqual(self.git(checkout, "rev-parse", "--absolute-git-dir"),
                         str((checkout / ".git").resolve()))
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), self.head)
        self.assertEqual((checkout / "tracked.txt").read_text(), "source HEAD\n")
        self.assertEqual([entry.path for entry in scratch.end("gate:symlink")], [checkout])
        self.assertTrue(checkout.is_dir())

    def test_symlink_scratch_clone_refuses_foreign_git_directory(self) -> None:
        _ = self.symlink_scratch_root()
        checkout = copies.clone(self.source, task="gate:foreign")
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.source / ".git")}):
            with self.assertRaisesRegex(copies.CopyError, "Git directory is .*expected"):
                copies.initialize_git(checkout, self.source)
        self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), self.head)

    def test_symlink_scratch_clone_refuses_symlink_git_directory(self) -> None:
        _ = self.symlink_scratch_root()
        checkout = copies.clone(self.source, task="gate:git-link")
        metadata = checkout / ".git"
        saved = checkout / "saved-git"
        _ = metadata.rename(saved)
        metadata.symlink_to(saved, target_is_directory=True)
        with self.assertRaisesRegex(copies.CopyError, "must own its .git directory"):
            copies.initialize_git(checkout, self.source)

    def test_apply_changes_through_symlink_scratch_root(self) -> None:
        _ = self.symlink_scratch_root()
        checkout = copies.clone(self.source, task="gate:apply-link")
        _ = (self.source / "untracked.txt").write_text("new file\n")
        _ = copies.apply_changes(checkout, self.source, self.head)
        self.assertEqual((checkout / "untracked.txt").read_text(), "new file\n")

    def test_borrower_order_resolves_stored_git_directory(self) -> None:
        alias = self.symlink_scratch_root()
        with registered_workspace(self.owner_id, self.source, self.head, name="a-parent", branch="parent") as parent:
            pass
        with registered_workspace(self.owner_id, parent, self.head, name="z-child", branch="child") as child:
            pass
        link = alias / "parent"
        link.symlink_to(parent, target_is_directory=True)
        _ = scratch.update_record(child, lambda record: {
            **(record.fields if isinstance(record, copy_record.Registered) else {}),
            "borrows_from": str(link / ".git"),
        })
        self.assertEqual([tree for tree, _ in copies.owned_workspaces(self.owner_id)], [child, parent])

    def test_first_use_owns_git_and_never_changes_source_branch(self) -> None:
        _ = self.git(self.source, "status", "--porcelain")
        before = {key: value for key, value in self.snapshot(self.source).items() if not key.startswith(".git/")}
        source_index = (self.source / ".git/index").read_bytes()
        with copies.standing(self.owner_id, self.source, copies.CommittedSource("HEAD")) as tree:
            self.assertEqual(tree, self.gates / "1")
            self.assertTrue(tree.is_relative_to(self.gates))
            self.assertFalse((tree / "target").is_symlink())
            self.assertTrue((tree / ".git").is_file())
            self.assertEqual(self.git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir"), str(self.source / ".git"))
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
            self.assertEqual(self.git(tree, "branch", "--show-current"), "")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
        self.assertEqual({key: value for key, value in self.snapshot(self.source).items() if not key.startswith(".git/")}, before)
        self.assertEqual((self.source / ".git/index").read_bytes(), source_index)
        self.assertEqual(self.git(self.source, "rev-parse", "main"), self.head)

    def test_copies_use_upstream_url_and_tracking_refs_instead_of_local_branches(self) -> None:
        _ = self.git(self.source, "branch", "builds", self.head)
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/builds", self.published)
        origin = self.git(self.source, "remote", "get-url", "origin")
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(registered_workspace(self.owner_id, self.source, self.head,
                                                         name="upstream", branch="worker")),
                     copies.clone(self.source, task="gate:upstream", word="upstream")]
            for tree in trees:
                with self.subTest(tree=tree):
                    if tree == trees[0]:
                        self.assertEqual(self.git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                                         str(self.source / ".git"))
                        self.assertEqual(self.git(tree, "remote", "get-url", "origin"),
                                         self.git(self.source, "remote", "get-url", "origin"))
                        self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.published)
                        self.assertEqual(self.git(tree, "branch", "--show-current"), "")
                        continue
                    self.assertEqual(self.git(tree, "remote", "get-url", "origin"), origin)
                    self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.published)
                    self.assertNotEqual(self.git(tree, "rev-parse", "origin/builds"),
                                        self.git(self.source, "rev-parse", "builds"))
                    self.assertEqual(self.git(tree, "remote", "get-url", "source"), str(self.source))
                    self.assertEqual(self.git(tree, "config", "--get-all", "remote.source.fetch"),
                                     "+refs/heads/*:refs/remotes/source/*")
                    self.assertEqual(self.git(tree, "rev-parse", "source/builds"), self.head)
                    self.assertIsNone(scratch.repo_problem(tree))
                    self.assertEqual((tree / ".git/objects/info/alternates").read_text().strip(),
                                     str(self.source / ".git/objects"))

    def test_existing_copies_heal_local_origin_and_prune_local_branch_refs(self) -> None:
        _ = self.git(self.source, "branch", "builds", self.head)
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/builds", self.published)
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as standing:
            pass
        with registered_workspace(self.owner_id, self.source, self.head, name="heal", branch="worker") as workspace:
            pass
        for tree in (workspace,):
            _ = self.git(tree, "remote", "set-url", "origin", str(self.source))
            _ = self.git(tree, "update-ref", "refs/remotes/origin/builds", self.head)
            _ = self.git(tree, "update-ref", "refs/remotes/origin/local-only", self.head)
            _ = self.git(tree, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/builds")
            _ = self.git(tree, "remote", "remove", "source")
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(registered_workspace(self.owner_id, self.source, self.head,
                                                         name="heal", branch="worker"))]
            self.assertEqual(trees[0], standing)
            self.assertEqual(trees[0], standing)
            self.assertEqual(trees[1], workspace)
            for tree in trees:
                with self.subTest(tree=tree):
                    if tree == trees[0]:
                        self.assertEqual(self.git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                                         str(self.source / ".git"))
                        self.assertEqual(self.git(tree, "remote", "get-url", "origin"),
                                         self.git(self.source, "remote", "get-url", "origin"))
                        self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.published)
                        self.assertEqual(self.git(tree, "branch", "--show-current"), "")
                        continue
                    self.assertEqual(self.git(tree, "remote", "get-url", "origin"),
                                     self.git(self.source, "remote", "get-url", "origin"))
                    self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.published)
                    self.assertEqual(self.git(tree, "for-each-ref", "--format=%(refname) %(objectname)",
                                              "refs/remotes/origin"),
                                     self.git(self.source, "for-each-ref", "--format=%(refname) %(objectname)",
                                              "refs/remotes/origin"))
                    self.assertEqual(self.git(tree, "remote", "get-url", "source"), str(self.source))
                    self.assertEqual(self.git(tree, "rev-parse", "source/builds"), self.head)
                    self.assertIsNone(scratch.repo_problem(tree))
        for tree in (workspace,):
            self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.published)

    def test_origin_head_is_copied_only_when_the_source_records_it(self) -> None:
        _ = self.git(self.source, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(self.git(tree, "rev-parse", "origin/HEAD"), self.published)
        _ = self.git(self.source, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(self.git(tree, "for-each-ref", "--format=%(refname)",
                                      "refs/remotes/origin/HEAD"), "")

    def test_end_of_use_refreshes_source_branches_for_the_copy_sweep(self) -> None:
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(registered_workspace(self.owner_id, self.source, self.head,
                                                         name="saved", branch="worker"))]
            incoming = self.incoming()
        self.assertTrue(trees[0].exists())
        for tree in trees[1:]:
            with self.subTest(tree=tree):
                self.assertEqual(self.git(tree, "rev-parse", "source/incoming"), incoming)
                self.assertIsNone(scratch.repo_problem(tree))

    def test_source_without_origin_keeps_local_url_and_branch_tracking(self) -> None:
        _ = self.git(self.source, "remote", "remove", "origin")
        _ = self.git(self.source, "branch", "builds", self.head)
        with registered_workspace(self.owner_id, self.source, self.head, name="no-origin", branch="no-origin") as tree:
            self.assertEqual(self.git(tree, "remote", "get-url", "origin"), str(self.source))
            self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.head)
            self.assertEqual(self.git(tree, "remote", "get-url", "source"), str(self.source))
            self.assertEqual(self.git(tree, "rev-parse", "source/builds"), self.head)

    def test_warm_copies_refresh_moved_and_deleted_refs_without_a_new_head(self) -> None:
        _ = self.git(self.source, "branch", "retired", self.head)
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/retired", self.published)
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as standing:
            pass
        with registered_workspace("verify-unit", self.source, self.head, name="refs", branch="worker") as workspace:
            pass
        _ = self.git(self.source, "branch", "-D", "retired")
        _ = self.git(self.source, "update-ref", "-d", "refs/remotes/origin/retired")
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/main", self.head)
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing("verify-unit", self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(registered_workspace("verify-unit", self.source, self.head,
                                                         name="refs", branch="worker"))]
            self.assertEqual(trees[0], standing)
            self.assertEqual(trees[0], standing)
            self.assertEqual(trees[1], workspace)
            for tree in trees:
                with self.subTest(tree=tree):
                    self.assertEqual(self.git(tree, "rev-parse", "origin/main"), self.head)
                    self.assertEqual(self.git(tree, "for-each-ref", "--format=%(refname)",
                                              "refs/remotes/source/retired", "refs/remotes/origin/retired"), "")
                    self.assertEqual((tree / "tracked.txt").read_text(), "source HEAD\n")

    def test_warm_copies_remove_tracking_refs_after_source_objects_are_pruned(self) -> None:
        retired = self.incoming()
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as standing:
            pass
        with registered_workspace("verify-unit", self.source, self.head, name="pruned", branch="worker") as workspace:
            pass
        _ = self.git(self.source, "branch", "-D", "incoming")
        _ = self.git(self.source, "reflog", "expire", "--expire=now", "--all")
        _ = self.git(self.source, "gc", "--prune=now")
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing("verify-unit", self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(registered_workspace("verify-unit", self.source, self.head,
                                                         name="pruned", branch="worker"))]
            self.assertEqual(trees[0], standing)
            self.assertEqual(trees[0], standing)
            self.assertEqual(trees[1], workspace)
            for tree in trees[1:]:
                with self.subTest(tree=tree):
                    fetched = subprocess.run(["git", "-C", str(tree), "fetch", "--quiet", "source"],
                                             capture_output=True, text=True, timeout=10)
                    self.assertEqual(fetched.returncode, 0, fetched.stderr)
                    listed = subprocess.run(["git", "-C", str(tree), "for-each-ref",
                                             "--format=%(refname) %(objectname)"],
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(listed.returncode, 0, listed.stderr)
                    self.assertEqual(listed.stderr, "")
                    self.assertNotIn("refs/remotes/source/incoming", listed.stdout)
                    self.assertNotIn(retired, listed.stdout)
                    self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
                    self.assertEqual((tree / "tracked.txt").read_text(), "source HEAD\n")

    def test_new_commit_reuses_tree_keeps_unchanged_mtime_and_target(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            stable = tree / "stable.txt"
            os.utime(stable, ns=(1_500_000_000_000_000_000, 1_500_000_000_000_000_000))
            # Keep this an unchanged tracked file after arranging its sentinel timestamp.
            _ = self.git(tree, "update-index", "--refresh")
            unchanged_mtime = stable.stat().st_mtime_ns
            cache = tree / "target/debug/compiled"
            cache.parent.mkdir(parents=True, exist_ok=True)
            _ = cache.write_bytes(b"warm build output")
            _ = (tree / "leftover.txt").write_text("remove untracked work\n")
        _ = (self.source / "tracked.txt").write_text("new commit\n")
        _ = self.git(self.source, "commit", "-qam", "next source commit")
        next_commit = self.git(self.source, "rev-parse", "HEAD")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource("HEAD")) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(reused, tree)
            self.assertEqual(self.git(reused, "rev-parse", "HEAD"), next_commit)
            self.assertEqual((reused / "stable.txt").stat().st_mtime_ns, unchanged_mtime)
            self.assertEqual(cache.read_bytes(), b"warm build output")
            self.assertFalse((tree / "leftover.txt").exists())
            self.assertEqual(self.git(reused, "status", "--porcelain"), "")
        self.assertIn(scratch.CACHE_TAG_SIGNATURE, (tree / "target/CACHEDIR.TAG").read_text())
        self.assertTrue((tree / "target" / scratch.USE_STAMP).is_file())

    def test_held_first_tree_sends_second_caller_to_second_tree(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as first:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as second:
                self.assertEqual(second, first.with_name('2'))
                self.assertEqual(build_folders.build_folder_state(first), 'held')
                self.assertEqual(build_folders.build_folder_state(second), 'held')
            self.assertEqual(build_folders.build_folder_state(second), 'free')

    def test_configured_root_contains_only_the_two_standing_checkouts(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as first:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as second:
                self.assertEqual(first, self.gates / '1')
                self.assertEqual(second, self.gates / '2')
                self.assertEqual(sorted(path.name for path in self.gates.iterdir() if path.is_dir()),
                                 ['1', '2'])
                for tree in (first, second):
                    self.assertTrue((tree / '.git').is_file())
                    self.assertTrue((tree / 'tracked.txt').is_file())
                    self.assertEqual(self.lease(tree)['unit'], tree.parent.name)

    def test_missing_or_invalid_required_configuration_refuses_before_clone(self) -> None:
        variants = [
            {**self.settings, key: value}
            for key, value in (("remote_registry", ""),
                               ("git_timeout_seconds", "0"), ("git_timeout_seconds", "nan"),
                               ("guard_timeout_seconds", "inf"))
        ]
        variants.extend({key: value for key, value in self.settings.items() if key != missing}
                        for missing in ("remote_registry", "git_timeout_seconds", "guard_timeout_seconds"))
        for settings in variants:
            with self.subTest(settings=settings):
                self.write_config(settings)
                result = self.run_cli()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(self.gates.exists())
        for legacy in ("absent", "empty", "relative"):
            with self.subTest(legacy_root=legacy):
                settings = {key: value for key, value in self.settings.items() if key != "root"}
                if legacy != "absent":
                    settings["root"] = "" if legacy == "empty" else "unused-relative-root"
                self.write_config(settings)
                result = self.run_cli()
                self.assertEqual(result.returncode, 0, result.stderr)
                with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
                    self.assertEqual(tree, self.gates / "1")

    def test_copy_roots_use_build_folders(self) -> None:
        scratch_root = self.base / "configured-scratch"
        workspace_root = self.base / "configured-workspaces"
        configuration = Path(os.environ["CONF_DIR"]) / "build_folders.conf"
        _ = configuration.write_text(f"roots.scratch={scratch_root}\nroots.workspace={workspace_root}\n", encoding="utf-8")
        environment = {key: value for key, value in os.environ.items()
                       if key not in (copy_record.ROOT_ENV, copy_record.COPIES_ENV)}
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(copies.load_config().remote_registry, self.base / "remote-mirrors")
            self.assertEqual(copy_record.root(), scratch_root)
            self.assertEqual(copy_record.copies_root(), workspace_root)

    def test_linked_source_uses_canonical_repository_folder_name(self) -> None:
        linked = self.base / "linked-worktree"
        _ = self.git(self.source, "worktree", "add", "--detach", str(linked), self.published)
        with copies.standing(self.owner_id, linked, copies.CommittedSource("HEAD")) as tree:
            self.assertEqual(tree, self.gates / "1")
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.published)
            self.assertTrue((tree / ".git").is_file())

    def test_main_worktree_and_local_clones_share_the_upstream_identity(self) -> None:
        linked = self.base / "worktree"
        _ = self.git(self.source, "worktree", "add", "--detach", str(linked), self.head)
        checkout = self.base / "clone/checkout"
        reload = self.base / "clone/reload"
        for path, origin in ((checkout, self.source), (reload, checkout)):
            _ = self.git(self.base, "clone", "-q", str(origin), str(path))
        _ = self.git(self.source, "remote", "set-url", "origin", "git@example.invalid:team/project.git")
        for source in (self.source, linked, checkout, reload):
            with self.subTest(source=source):
                self.assertEqual(copies.repository_name(source), "project")
        _ = self.git(reload, "remote", "set-url", "origin", checkout.as_uri())
        self.assertEqual(copies.repository_name(reload), "project")
        _ = self.git(self.source, "remote", "set-url", "origin", "work:team/project.git")
        _ = self.git(self.source, "remote", "set-url", "--add", "origin", "ssh://backup.invalid/project.git")
        _ = self.git(self.source, "config", "url.ssh://fallback.invalid/.insteadOf", "work:")
        _ = self.git(self.source, "config", "url.ssh://preferred.invalid/team/.insteadOf", "work:team/")
        resolved = self.git(self.source, "remote", "get-url", "origin")
        self.assertEqual(resolved, "ssh://preferred.invalid/team/project.git")
        self.assertEqual(copies.origin_url(self.source), resolved)

    def test_repository_without_origin_uses_main_checkout_name(self) -> None:
        _ = self.git(self.source, "remote", "remove", "origin")
        linked = self.base / "worktree"
        _ = self.git(self.source, "worktree", "add", "--detach", str(linked), self.head)
        bare = self.base / "bare.git"
        _ = self.git(self.base, "clone", "-q", "--bare", str(self.source), str(bare))
        _ = self.git(bare, "remote", "remove", "origin")
        bare_linked = self.base / "bare-worktree"
        _ = self.git(bare, "worktree", "add", "--detach", str(bare_linked), self.head)
        for source, main in ((self.source, self.source), (linked, self.source),
                             (bare, bare), (bare_linked, bare)):
            with self.subTest(source=source):
                self.assertEqual(copies.main_checkout(source), main)
                self.assertEqual(copies.repository_name(source), main.name)

    def test_relative_origin_uses_the_main_checkout_from_each_worktree(self) -> None:
        _ = self.git(self.source, "remote", "set-url", "origin", "../origin.git")
        upstream = self.source.parent / "origin.git"
        _ = self.git(upstream, "remote", "set-url", "origin", "git@example.invalid:team/widget.git")
        linked = self.base / "worktrees" / "unit"
        _ = self.git(self.source, "worktree", "add", "--detach", str(linked), self.head)
        for source in (self.source, linked):
            with self.subTest(source=source), copies.standing(self.owner_id, source, copies.CommittedSource("HEAD")) as tree:
                self.assertEqual(tree.name, "1")
                self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)

    def test_separate_git_directory_uses_the_main_checkout_origin(self) -> None:
        main = self.base / "widget"
        metadata = self.base / "state" / "widget-index"
        metadata.parent.mkdir()
        _ = self.git(self.base, "clone", "-q", "--separate-git-dir", str(metadata), str(self.source), str(main))
        _ = self.git(main, "remote", "set-url", "origin", "https://example.invalid/widget.git")
        self.assertEqual(copies.repository_name(main), "widget")
        # Linked worktrees need the main path recorded when Git metadata lives elsewhere.
        _ = self.git(main, "config", "core.worktree", str(main))
        linked = self.base / "separate-worktree"
        _ = self.git(main, "worktree", "add", "--detach", str(linked), "HEAD")
        for source in (main, linked):
            with self.subTest(source=source), copies.standing(self.owner_id, source, copies.CommittedSource("HEAD")) as tree:
                self.assertEqual(copies.main_checkout(source), main)
                self.assertEqual(tree.name, "1")
                self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)

    def test_gate_borrows_the_last_store_and_keeps_source_commits_after_clones_are_deleted(self) -> None:
        first, second = self.base / "first", self.base / "second"
        for checkout, source in ((first, self.source), (second, first)):
            _ = self.git(self.base, "clone", "--shared", "-q", str(source), str(checkout))
        # Git resolves a relative alternate from the object store that contains it.
        _ = (second / ".git/objects/info/alternates").write_text(
            os.path.relpath(first / ".git/objects", second / ".git/objects") + "\n")
        _ = (second / "only-in-source.txt").write_text("source-only object\n")
        _ = self.git(second, "add", "only-in-source.txt")
        _ = self.git(second, "commit", "-qm", "source-only commit")
        commit = self.git(second, "rev-parse", "HEAD")
        linked = self.base / "linked"
        _ = self.git(second, "worktree", "add", "--detach", str(linked), commit)
        for source in (second, linked):
            with self.subTest(source=source), copies.standing(self.owner_id, source, copies.CommittedSource(commit)) as tree:
                self.assertEqual(self.git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir"),
                                 str(self.source / ".git"))
                self.assertEqual(self.git(tree, "rev-parse", "HEAD"), commit)
                saved = copies.clone(tree, task="gate:retained-proof")
        shutil.rmtree(first)
        shutil.rmtree(second)
        self.assertEqual(self.git(saved, "rev-list", "--objects", "--all", "--quiet"), "")
        self.assertEqual(self.git(saved, "show", "HEAD:only-in-source.txt"), "source-only object")

    def test_gate_copies_a_registered_scratch_store_without_alternates(self) -> None:
        source = copies.clone(self.source, task="gate:independent")
        _ = self.git(source, "repack", "-a", "-d")
        (source / ".git/objects/info/alternates").unlink()
        with copies.standing(self.owner_id, source, copies.CommittedSource("HEAD")) as tree:
            self.assertFalse((tree / ".git/objects/info/alternates").exists())
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
            saved = copies.clone(tree, task="gate:retained-proof")
        shutil.rmtree(source)
        self.assertEqual(self.git(saved, "rev-list", "--objects", "--all", "--quiet"), "")
        self.assertEqual(self.git(saved, "show", "HEAD:tracked.txt"), "source HEAD")

    def test_gate_copies_a_registered_store_with_multiple_alternates_without_borrowing(self) -> None:
        source = copies.clone(self.source, task="gate:multiple-alternates")
        _ = (source / ".git/objects/info/alternates").write_text(
            str(self.source / ".git/objects") + "\n" + str(self.base / "origin.git/objects") + "\n")
        with copies.standing(self.owner_id, source, copies.CommittedSource("HEAD")) as tree:
            self.assertFalse((tree / ".git/objects/info/alternates").exists())
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
            saved = copies.clone(tree, task="gate:retained-proof")
        shutil.rmtree(source)
        self.assertEqual(self.git(saved, "rev-list", "--objects", "--all", "--quiet"), "")
        self.assertEqual(self.git(saved, "show", "HEAD:tracked.txt"), "source HEAD")

    def test_local_origin_cycle_refuses_a_gate(self) -> None:
        other = self.base / "other-clone"
        _ = self.git(self.base, "clone", "-q", str(self.source), str(other))
        _ = self.git(self.source, "remote", "set-url", "origin", str(other))
        with self.assertRaisesRegex(copies.CopyError, "local origins form a cycle"):
            with copies.standing(self.owner_id, self.source, copies.CommittedSource("HEAD")):
                self.fail("a local origin cycle started a gate")

    def test_repository_removal_keeps_workspace_identity_after_source_deletion(self) -> None:
        _ = self.git(self.source, "remote", "set-url", "origin", "git@example.invalid:team/widget.git")
        selector = self.base / "selector"
        _ = self.git(self.base, "clone", "-q", str(self.source), str(selector))
        _ = self.git(selector, "remote", "set-url", "origin", "git@example.invalid:team/widget.git")
        with registered_workspace(self.owner_id, self.source, "HEAD", name="worker", branch="worker") as tree:
            pass
        _ = self.git(tree, "repack", "-a", "-d")
        (tree / ".git/objects/info/alternates").unlink()
        shutil.rmtree(self.source)
        result = self.cli("remove", "--repo", str(selector))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(tree.exists())

    def test_remove_reports_a_filesystem_failure_as_a_copy_error(self) -> None:
        with registered_workspace(self.owner_id, self.source, 'HEAD', name='removal', branch='removal') as tree:
            pass
        with mock.patch.object(scratch, 'remove', side_effect=OSError('removal interrupted')):
            with self.assertRaisesRegex(copies.CopyError, 'removal interrupted'):
                copies.remove_trees(self.owner_id)
        self.assertTrue(tree.exists())
        self.assertIsInstance(scratch.pending_removal(tree), copy_record.Registered)
        copies.remove_trees(self.owner_id)
        self.assertFalse(tree.exists())
        self.assertIsInstance(scratch.pending_removal(tree), copy_record.NotRegistered)

    def test_new_editable_workspace_points_to_the_worktree_tool(self) -> None:
        destination = copies.workspace_path(self.owner_id, self.source, "new-editor")
        for present in (False, True):
            with self.subTest(present=present):
                if present:
                    destination.mkdir(parents=True)
                with (self.assertRaisesRegex(copies.CopyError, "worktrees.py make <repo> <unit> <short-name>"),
                      copies.workspace(self.owner_id, self.source, self.head, name="new-editor", branch="editor")):
                    self.fail("an unregistered workspace reached the caller")
                self.assertEqual(destination.exists(), present)
                if present:
                    self.assertEqual(list(destination.iterdir()), [])

    def test_workspace_records_ownership_and_preserves_edits_on_resume(self) -> None:
        name = "crate-run-package"
        branch = "style-crate/run/package"
        with registered_workspace(self.owner_id, self.source, self.head, name=name, branch=branch) as tree:
            record = scratch.task_of(tree)
            self.assertIsInstance(record, copy_record.Registered)
            assert isinstance(record, copy_record.Registered)
            self.assertEqual(record.fields["owner_unit"], self.owner_id)
            self.assertEqual(record.fields["source_commit"], self.head)
            self.assertEqual(record.fields["borrows_from"], str(self.source / ".git"))
            self.assertEqual(self.git(tree, "rev-parse", "--absolute-git-dir"), str(tree / ".git"))
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
            _ = (tree / "tracked.txt").write_text("pending recovery\n")
            _ = self.git(tree, "add", "tracked.txt")
            _ = (tree / "untracked.txt").write_text("pending file\n")
            with scratch.busy_locks(tree, create=False) as idle:
                self.assertFalse(idle)
            with self.assertRaisesRegex(copies.CopyError, "busy"):
                with registered_workspace(self.owner_id, self.source, self.head, name=name, branch=branch):
                    self.fail("a concurrent caller entered the workspace")
        with copies.workspace(self.owner_id, self.source, self.head, name=name, branch=branch) as resumed:
            self.assertEqual(resumed, tree)
            self.assertEqual((resumed / "tracked.txt").read_text(), "pending recovery\n")
            self.assertEqual(self.git(resumed, "diff", "--cached", "--name-only"), "tracked.txt")
            self.assertTrue((resumed / "untracked.txt").is_file())
        self.assertEqual(self.git(self.source, "rev-parse", "HEAD"), self.head)

    def test_workspace_retries_each_initialization_boundary_without_resetting_finished_edits(self) -> None:
        for boundary in ("registration", "clone", "checkout", "config"):
            with self.subTest(boundary=boundary):
                tree = copies.workspace_path(self.owner_id, self.source, boundary)
                original_git = copies.git
                original_register = scratch.register_copy
                interrupted = False
                def register(path: Path, **fields: str) -> None:
                    nonlocal interrupted
                    original_register(path, **fields)
                    if boundary == "registration" and not interrupted:
                        interrupted = True
                        raise RuntimeError("interrupted init")
                def git(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
                    nonlocal interrupted
                    result = original_git(repo, *arguments, data=data)
                    match = ((boundary == "clone" and "clone" in arguments)
                             or (boundary == "checkout" and "checkout" in arguments)
                             or (boundary == "config" and repo == tree and len(arguments) == 3
                                 and arguments[:2] == ("config", "user.email")))
                    if match and not interrupted:
                        interrupted = True
                        raise RuntimeError("interrupted init")
                    return result
                with mock.patch.object(scratch, "register_copy", side_effect=register), mock.patch.object(copies, "git", side_effect=git):
                    with self.assertRaisesRegex(RuntimeError, "interrupted init"):
                        with registered_workspace(self.owner_id, self.source, self.head, name=boundary, branch="worker"):
                            self.fail("initialization reached the caller")
                record = scratch.task_of(tree)
                assert isinstance(record, copy_record.Registered)
                self.assertEqual(record.fields["copy_commit"], "")
                self.assertEqual(copies.workspace_repository(record), copies.repository_name(self.source))
                with registered_workspace(self.owner_id, self.source, self.head, name=boundary, branch="worker") as resumed:
                    self.assertEqual(self.git(resumed, "rev-parse", "HEAD"), self.head)
                    _ = (resumed / "tracked.txt").write_text("finished workspace edit\n")
                with registered_workspace(self.owner_id, self.source, self.head, name=boundary, branch="worker") as resumed:
                    self.assertEqual((resumed / "tracked.txt").read_text(), "finished workspace edit\n")

    def test_workspace_reads_identity_after_the_checkout(self) -> None:
        real_git = copies.git

        def git(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
            result = real_git(repo, *arguments, data=data)
            if arguments[:1] == ("checkout",):
                _ = self.git(self.source, "config", "user.name", "Checkout Hook")
                _ = self.git(self.source, "config", "user.email", "hook@example.invalid")
            return result

        with mock.patch.object(copies, "git", side_effect=git):
            with registered_workspace(self.owner_id, self.source, self.head, name="identity", branch="worker") as tree:
                self.assertEqual(self.git(tree, "config", "user.name"), "Checkout Hook")
                self.assertEqual(self.git(tree, "config", "user.email"), "hook@example.invalid")

    def test_legacy_workspace_keeps_commits_index_journal_and_the_checkout_at_task_end(self) -> None:
        tree = self.base / "old-run/worker"
        tree.mkdir(parents=True)
        copies.initialize_git(tree, self.source)
        _ = self.git(tree, "checkout", "-q", "-B", "worker", self.head)
        _ = (tree / "stable.txt").write_text("legacy commit\n")
        _ = self.git(tree, "commit", "-qam", "legacy worker commit")
        tip = self.git(tree, "rev-parse", "HEAD")
        _ = (tree / "tracked.txt").write_text("legacy index\n")
        _ = self.git(tree, "add", "tracked.txt")
        journal = tree / ".git/style-batch-intent.json"
        _ = journal.write_text("journal\n")
        _ = (tree / "pending.txt").write_text("pending\n")
        with registered_workspace(self.owner_id, self.source, self.head, name="legacy", branch="worker",
                              task="gate:legacy-run", path=tree) as resumed:
            self.assertEqual(resumed, tree)
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), tip)
            self.assertEqual(self.git(tree, "diff", "--cached", "--name-only"), "tracked.txt")
            self.assertEqual(journal.read_text(), "journal\n")
            self.assertEqual((tree / "pending.txt").read_text(), "pending\n")
        with redirect_stderr(io.StringIO()) as errors:
            copies.end_workspaces("gate:legacy-run", [tree])
        self.assertGreater(scratch.holds()[str(tree)]["ended"], 0)
        self.assertIn(f"kept for a manual removal list: {tree} (scratch, task gate:legacy-run ended)", errors.getvalue())
        self.assertEqual(self.git(tree, "rev-parse", "HEAD"), tip)
        self.assertEqual(self.git(tree, "diff", "--cached", "--name-only"), "tracked.txt")
        self.assertEqual(journal.read_text(), "journal\n")
        self.assertEqual((tree / "pending.txt").read_text(), "pending\n")

    def test_legacy_workspace_refuses_matching_relative_urls_for_different_repositories(self) -> None:
        tree = self.base / "old-run/worker"
        tree.mkdir(parents=True)
        copies.initialize_git(tree, self.source)
        _ = self.git(tree, "checkout", "-q", "-B", "worker", self.head)
        _ = self.git(self.base, "clone", "-q", "--bare", str(self.source), str(tree.parent / "origin.git"))
        for repo in (self.source, tree):
            _ = self.git(repo, "remote", "set-url", "origin", "../origin.git")

        with self.assertRaisesRegex(copies.CopyError, "legacy workspace source differs"):
            with registered_workspace(self.owner_id, self.source, self.head, name="legacy", branch="worker", path=tree):
                self.fail("the unrelated workspace reached the caller")

        self.assertIsInstance(scratch.task_of(tree), copy_record.NotRegistered)
        self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.git(tree, "remote", "get-url", "origin"), "../origin.git")

    def test_workspace_uses_the_existing_copy_sweep(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.published,
                              name="sweep-package", branch="worker") as tree:
            self.assertIn(tree, list(scratch.copy_paths()))
            record = scratch.task_of(tree)
            assert isinstance(record, copy_record.Registered)
            self.assertFalse(scratch.end_copy(tree, record.task, time.time()))
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        self.assertTrue(scratch.end_copy(tree, record.task, time.time()))
        self.assertFalse(tree.exists())

        with self.subTest(ended_workspace="unlinked native copy with unsaved work"):
            with registered_workspace(self.owner_id, self.source, self.published,
                                  name="unsaved-sweep-package", branch="worker", task="gate:unlinked-run") as unsaved:
                _ = (unsaved / "pending.txt").write_text("keep unsaved work\n")
            record = scratch.task_of(unsaved)
            assert isinstance(record, copy_record.Registered)
            self.assertNotIn("crate_run", record.fields)
            copies.end_workspaces(record.task, [unsaved])
            self.assertTrue(unsaved.exists())
            ended = scratch.task_of(unsaved)
            assert isinstance(ended, copy_record.Registered)
            self.assertEqual(ended.fields["ended_task"], record.task)
            self.assertIsInstance(scratch.lifetime(unsaved), scratch.Ended)
            self.assertEqual((unsaved / "pending.txt").read_text(), "keep unsaved work\n")

    def test_workspace_refuses_another_base_or_branch_without_resetting(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.head, name="reserved", branch="worker") as tree:
            _ = (tree / "tracked.txt").write_text("keep these edits\n")
        for revision, branch in ((self.published, "worker"), (self.head, "other")):
            with self.subTest(revision=revision, branch=branch):
                with self.assertRaises(copies.CopyError):
                    with registered_workspace(self.owner_id, self.source, revision, name="reserved", branch=branch):
                        self.fail("a different request entered the workspace")
                self.assertEqual((tree / "tracked.txt").read_text(), "keep these edits\n")

    def test_proof_reuses_its_held_standing_checkout(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            with copies.standing(self.owner_id, tree, copies.CommittedSource(self.head)) as proof:
                self.assertEqual(proof, tree)
                with build_folders.collector_lease(proof, wait=False) as idle:
                    self.assertFalse(idle)
            self.assertEqual([path for path in self.gates.iterdir() if path.is_dir()], [tree])

    def test_worker_proof_uses_the_source_repository_gate_pool(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as frozen:
            with registered_workspace(self.owner_id, self.source, self.head, name="package", branch="worker") as worker:
                with copies.standing(self.owner_id, worker, copies.CommittedSource(self.head)) as proof:
                    self.assertEqual(proof, frozen.with_name("2"))

    def test_apply_uncommitted_captures_staged_unstaged_and_untracked(self) -> None:
        _ = (self.source / "tracked.txt").write_text("staged edit\n")
        _ = self.git(self.source, "add", "tracked.txt")
        _ = (self.source / "stable.txt").write_text("unstaged edit\n")
        _ = (self.source / "untracked.bin").write_bytes(b"binary\x00payload")
        _ = (self.source / "ignored.txt").write_text("ignored\n")
        _ = self.git(self.source, "status", "--porcelain")
        before = {key: value for key, value in self.snapshot(self.source).items() if not key.startswith(".git/")}
        source_index = (self.source / ".git/index").read_bytes()
        with copies.standing(self.owner_id, self.source, copies.WorktreeSource(self.head, self.source)) as tree:
            self.assertEqual((tree / "tracked.txt").read_text(), "staged edit\n")
            self.assertEqual((tree / "stable.txt").read_text(), "unstaged edit\n")
            self.assertEqual((tree / "untracked.bin").read_bytes(), b"binary\x00payload")
            self.assertFalse((tree / "ignored.txt").exists())
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^"), self.head)
            self.assertEqual(self.git(tree, "log", "-1", "--format=%s"),
                             f"gate: uncommitted {self.source} at {self.head[:7]}")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
        self.assertEqual({key: value for key, value in self.snapshot(self.source).items() if not key.startswith(".git/")}, before)
        self.assertEqual((self.source / ".git/index").read_bytes(), source_index)

    def test_apply_changes_refuses_an_untracked_nested_repository(self) -> None:
        nested = self.source / "nested"
        nested.mkdir()
        _ = self.git(nested, "init", "--quiet")
        _ = (nested / "file").write_text("nested source\n")
        _ = self.git(nested, "add", ".")
        _ = self.git(nested, "commit", "-qm", "nested source")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            with self.assertRaisesRegex(copies.PreparationError, "NestedRepository: nested"):
                _ = copies.apply_changes(tree, self.source, self.head)
            self.assertFalse((tree / "nested").exists())

    def test_apply_changes_error_names_untracked_files(self) -> None:
        _ = (self.source / "untracked.txt").write_text("new source\n")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            with mock.patch.object(copies, "staged_tree", side_effect=copies.PreparationError("fixture failure")):
                with self.assertRaisesRegex(copies.PreparationError, "files: untracked.txt; fixture failure"):
                    _ = copies.apply_changes(tree, self.source, self.head)

    def test_private_capture_timeout_exits_three_and_names_changed_files(self) -> None:
        _ = (self.source / "tracked.txt").write_text("changed source\n")
        _ = (self.source / "untracked.txt").write_text("new source\n")
        real_run = cast(Callable[..., object], subprocess.run)
        for step in ("add", "write-tree"):
            with self.subTest(step=step):
                def timeout_capture(arguments: list[str], **options: object) -> object:
                    if arguments[:3] == ["git", "-C", str(self.source)] and arguments[3] == step:
                        raise subprocess.TimeoutExpired(arguments, float(self.settings["git_timeout_seconds"]))
                    return real_run(arguments, **options)
                with mock.patch("scripts.copies.copies.subprocess.run", side_effect=timeout_capture):
                    result = self.run_cli("--apply-uncommitted", str(self.source))
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn("tracked.txt", result.stderr)
                self.assertIn("untracked.txt", result.stderr)
                arguments = "add --all -- ." if step == "add" else "write-tree"
                self.assertIn(f"git {arguments} in {self.source}: timed out after 10 seconds", result.stderr)

    def test_merge_fetches_source_ref_and_creates_a_merge_commit(self) -> None:
        incoming = self.incoming()
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head), merge=copies.MergeRevision("incoming")) as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^1"), self.head)
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^2"), incoming)
            self.assertEqual((tree / "incoming.txt").read_text(), "merged content\n")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")

    def test_apply_precedes_merge(self) -> None:
        incoming = self.incoming()
        _ = (self.source / "untracked.txt").write_text("applied before merge\n")
        with copies.standing(self.owner_id, self.source, copies.WorktreeSource(self.head, self.source), merge=copies.MergeRevision("incoming")) as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^2"), incoming)
            self.assertTrue(self.git(tree, "log", "-1", "--format=%s", "HEAD^1").startswith("gate: uncommitted "))
            self.assertEqual((tree / "untracked.txt").read_text(), "applied before merge\n")

    def test_merge_conflict_exits_three_names_file_and_next_use_resets(self) -> None:
        _ = self.git(self.source, "switch", "-qc", "conflict")
        _ = (self.source / "tracked.txt").write_text("incoming conflict\n")
        _ = self.git(self.source, "commit", "-qam", "conflicting change")
        _ = self.git(self.source, "switch", "-q", "main")
        _ = (self.source / "tracked.txt").write_text("uncommitted conflict\n")
        result = self.run_cli("--apply-uncommitted", str(self.source), "--merge", "conflict")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("tracked.txt", result.stdout + result.stderr)
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(tree, self.gates / "1")
            self.assertEqual((tree / "tracked.txt").read_text(), "source HEAD\n")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
            self.assertFalse((tree / ".git/MERGE_HEAD").exists())

    def test_failed_apply_exits_three_releases_lock_and_resets(self) -> None:
        _ = (self.source / "tracked.txt").write_text("patch based on source HEAD\n")
        result = self.run_cli("--commit", self.published, "--apply-uncommitted", str(self.source))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("tracked.txt", result.stdout + result.stderr)
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(tree, self.gates / "1")
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")

    def test_direct_cargo_uses_the_numbered_copy_and_its_lock(self) -> None:
        cargo = self.base / "cargo"
        _ = cargo.write_text(f"#!{sys.executable}\n" + dedent("""\
            import os
            import sys
            from pathlib import Path

            assert sys.argv[1] == "test"
            assert Path(os.environ["CARGO_TARGET_DIR"]) == Path.cwd() / "target"
            os.fstat(int(os.environ["NUMBERED_COPY_LOCK_FD"]))
            Path(sys.argv[2]).write_text(str(Path.cwd()))
            """))
        cargo.chmod(0o700)
        evidence = self.base / "cargo-cwd"
        result = self.run_cli(command=(str(cargo), "test", str(evidence)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(evidence.read_text().strip()).resolve(), self.gates / "1")

    def test_run_publishes_identical_bytes_after_a_repeat_build(self) -> None:
        program = dedent("""\
            import os
            import sys
            from pathlib import Path

            path = Path(os.environ["CARGO_TARGET_DIR"]) / "debug/artifact"
            content = b"same artifact"
            if sys.argv[1] == "repeat":
                assert path.read_bytes() == content
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            """)
        cargo = self.base / "cargo"
        _ = cargo.write_text('#!/bin/sh\nexec "$@"\n')
        cargo.chmod(0o700)
        for build in ("first", "repeat"):
            with self.subTest(build=build):
                result = self.run_cli("--publish", "target/debug/artifact",
                                      command=(str(cargo), sys.executable, "-c", program, build))
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = [line for line in result.stdout.splitlines() if line.startswith("published: ")]
                self.assertEqual(len(lines), 1, result.stderr)
                path, commit = lines[0].removeprefix("published: ").rsplit(" ", 1)
                self.assertEqual(commit, self.head)
                self.assertEqual(Path(path).read_bytes(), b"same artifact")

    def commit_gate_script(self, program: str) -> str:
        script = self.source / "scripts/production/gate.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        _ = script.write_text(dedent(program))
        _ = self.git(self.source, "add", str(script))
        _ = self.git(self.source, "commit", "-qm", "Install the gate fixture")
        return self.git(self.source, "rev-parse", "HEAD")

    def commit_gate_validator(self) -> str:
        scripts = Path(__file__).resolve().parents[1]
        package = self.source / "scripts/__init__.py"
        package.parent.mkdir(exist_ok=True)
        _ = package.write_text(f"__path__.append({str(scripts)!r})\n")
        _ = self.git(self.source, "add", str(package))
        return self.commit_gate_script((scripts / "production/gate.py").read_text())

    def test_run_refuses_a_named_gate_without_a_test_mode_before_admission(self) -> None:
        _ = self.commit_gate_validator()
        with mock.patch.object(first_step, "admitted_run") as admit:
            result = self.run_cli(command=("./scripts/lib/py", "scripts/production/gate.py", "--module", "x"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "copies: --module needs --tests-only or --affected\n")
        admit.assert_not_called()

    def test_run_reports_the_gate_refusal_for_an_invalid_nightly_owner(self) -> None:
        from scripts.production import gate

        _ = self.commit_gate_validator()
        arguments = ("--nightly", "--merge-branch", "builds", "--unit", "Bad!")
        with (mock.patch.object(gate, "checked_gate_target", return_value=gate.NO_GATE_ASSIGNMENT),
              redirect_stderr(io.StringIO()) as direct_errors):
            self.assertEqual(gate.main(arguments), 1)
        command = (sys.executable, "scripts/production/gate.py", *arguments)
        with mock.patch.object(first_step, "admitted_run",
                               side_effect=first_step.FirstStepRefused("Fixture reached admission")) as admit:
            result = self.run_cli(command=command)
        admit.assert_not_called()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, direct_errors.getvalue())
        self.assertEqual(result.stderr, "REFUSED: 'Bad!': owner must match [a-z0-9][a-z0-9_-]*\n")

    def test_run_checks_the_absolute_merge_gate_parser_against_the_snapshot(self) -> None:
        _ = self.commit_gate_script("""\
            def check_prepared_gate_source(arguments, parser, source):
                parser.error("The snapshot parser ran")
            """)
        launcher = Path(__file__).resolve().parents[1] / "production/merge_gate.py"
        arguments = ("--checkout", "{tree}", "--tests-only", "--module", "copies/test_copies.py")
        with mock.patch.object(first_step, "admitted_run",
                               side_effect=first_step.FirstStepRefused("Fixture reached admission")) as admit:
            refused = self.run_cli(command=(sys.executable, str(launcher), *arguments))
            admit.assert_not_called()
            self.assertEqual(refused.returncode, 2)
            self.assertIn("--module 'copies/test_copies.py' does not name a test module", refused.stderr)

            snapshot = self.run_cli(command=(sys.executable, "{tree}/scripts/production/gate.py", *arguments))
            admit.assert_not_called()
            self.assertEqual(snapshot.returncode, 2)
            self.assertEqual(snapshot.stderr, "copies: The snapshot parser ran\n")

            module = self.source / "scripts/copies/test_copies.py"
            module.parent.mkdir()
            module.touch()
            _ = self.git(self.source, "add", str(module))
            _ = self.git(self.source, "commit", "-qm", "Add the named module to the snapshot")
            result = self.run_cli(command=(sys.executable, str(launcher), *arguments))
            admit.assert_called_once()
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "copies: Fixture reached admission\n")

    def test_run_refuses_a_module_with_the_scripts_prefix_before_admission(self) -> None:
        _ = self.commit_gate_validator()
        with mock.patch.object(first_step, "admitted_run") as admit:
            result = self.run_cli(command=("./scripts/lib/py", "scripts/production/gate.py",
                                           "--tests-only", "--module", "scripts/lib/test_fixture.py"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("--module 'scripts/lib/test_fixture.py' needs a path under scripts/", result.stderr)
        self.assertIn("lib/test_x.py", result.stderr)
        admit.assert_not_called()

    def test_run_refuses_a_module_absent_from_the_requested_commit_before_admission(self) -> None:
        revision = self.commit_gate_validator()
        test = self.source / "scripts/lib/test_fixture.py"
        test.parent.mkdir(parents=True)
        test.touch()
        _ = self.git(self.source, "add", str(test))
        _ = self.git(self.source, "commit", "-qm", "Add the test module")
        with mock.patch.object(first_step, "admitted_run") as admit:
            result = self.run_cli("--commit", revision, command=(
                "./scripts/lib/py", "scripts/production/gate.py", "--tests-only", "--module", "lib/test_fixture.py"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("--module 'lib/test_fixture.py' does not name a test module", result.stderr)
        admit.assert_not_called()

    def test_run_checks_both_gate_script_spellings_with_a_dot_segment(self) -> None:
        _ = self.commit_gate_validator()
        arguments = ("--tests-only", "--module", "lib/test_missing.py")
        for script in ("./scripts/production/gate.py", "{tree}/./scripts/production/gate.py"):
            with (self.subTest(script=script),
                  mock.patch.object(first_step, "admitted_run",
                                    side_effect=first_step.FirstStepRefused("Fixture reached admission")) as admit):
                result = self.run_cli(command=(sys.executable, script, *arguments))
                admit.assert_not_called()
                self.assertEqual(result.returncode, 2)
                self.assertIn("--module 'lib/test_missing.py' does not name a test module", result.stderr)

    def test_run_admits_a_module_from_the_requested_commit_once(self) -> None:
        _ = self.commit_gate_validator()
        test = self.source / "scripts/lib/test_fixture.py"
        test.parent.mkdir(parents=True)
        test.touch()
        _ = self.git(self.source, "add", str(test))
        _ = self.git(self.source, "commit", "-qm", "Add the test module")
        revision = self.git(self.source, "rev-parse", "HEAD")
        _ = self.git(self.source, "rm", str(test))
        _ = self.git(self.source, "commit", "-qm", "Remove the test module")
        with mock.patch.object(first_step, "admitted_run",
                               side_effect=first_step.FirstStepRefused("Fixture reached admission")) as admit:
            result = self.run_cli("--commit", revision, command=("./scripts/lib/py", "scripts/production/gate.py",
                "--affected", "--module", "lib/test_fixture.py", "--repo", "{tree}"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "copies: Fixture reached admission\n")
        admit.assert_called_once()

    def test_run_refuses_an_inner_repo_outside_the_requested_snapshot(self) -> None:
        revision = self.commit_gate_validator()
        test = self.source / "scripts/lib/test_fixture.py"
        test.parent.mkdir()
        test.touch()
        with mock.patch.object(first_step, "admitted_run") as admit:
            result = self.run_cli("--commit", revision, command=(sys.executable, "scripts/production/gate.py",
                "--tests-only", "--module", "lib/test_fixture.py", "--repo", str(self.source)))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("The gate --repo must stay in the prepared source.", result.stderr)
        admit.assert_not_called()

    def test_run_refuses_a_repo_path_that_only_matches_the_validation_snapshot(self) -> None:
        _ = self.commit_gate_validator()
        test = self.source / "scripts/lib/test_fixture.py"
        test.parent.mkdir()
        test.touch()
        _ = self.git(self.source, "add", str(test))
        _ = self.git(self.source, "commit", "-qm", "Add the test module")
        for repo in ("../source", "./{tree}"):
            with self.subTest(repo=repo), mock.patch.object(first_step, "admitted_run") as admit:
                result = self.run_cli(command=(sys.executable, "scripts/production/gate.py",
                    "--tests-only", "--module", "lib/test_fixture.py", f"--checkout={repo}"))
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("Use --repo '{tree}'.", result.stderr)
                admit.assert_not_called()

    def test_run_launches_alternate_gate_script_paths_without_early_validation(self) -> None:
        revision = self.commit_gate_script("""\
            from pathlib import Path
            import sys

            def check_prepared_gate_source(arguments, parser, source):
                raise AssertionError("An alternate script path reached validation")

            assert sys.argv[1:] == ["--targeted", str(Path.cwd())]
            """)
        for script in (str(self.source / "scripts/production/gate.py"),
                       "{tree}/scripts/production/../production/gate.py", "scripts/production/../production/gate.py"):
            with self.subTest(script=script):
                run_gate = mock.Mock(side_effect=launch_copy_fixture)
                admitted = mock.Mock(spec=first_step.FirstStepAdmission, run_gate=run_gate)
                with mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)) as admit:
                    result = self.run_cli("--commit", revision,
                                          command=(sys.executable, script, "--targeted", "{tree}"))
                self.assertEqual(result.returncode, 0, result.stderr)
                admit.assert_called_once()
                run_gate.assert_called_once()

    def test_run_checks_only_the_requested_module_paths(self) -> None:
        _ = self.commit_gate_validator()
        named = self.source / "scripts/lib/test_fixture.py"
        named.parent.mkdir()
        named.touch()
        unrelated = self.source / "scripts/test_unrelated.py"
        unrelated.touch()
        _ = self.git(self.source, "add", str(named), str(unrelated))
        _ = self.git(self.source, "commit", "-qm", "Add the fixture modules")
        with mock.patch.object(first_step, "admitted_run",
                               side_effect=first_step.FirstStepRefused("Fixture reached admission")) as admit:
            result = self.run_cli(command=(sys.executable, "scripts/production/gate.py",
                "--tests-only", "--module", "lib/test_fixture.py"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "copies: Fixture reached admission\n")
        admit.assert_called_once()

    def test_run_uses_the_gate_parser_from_the_requested_revision(self) -> None:
        revision = self.commit_gate_script("""\
            def check_prepared_gate_source(arguments, parser, source):
                parser.add_argument("--selected-option", action="store_true", required=True)
                parser.parse_args(arguments)
            """)
        _ = self.commit_gate_validator()
        command = (sys.executable, "scripts/production/gate.py")
        with mock.patch.object(first_step, "admitted_run",
                               side_effect=first_step.FirstStepRefused("Fixture reached admission")) as admit:
            refused = self.run_cli("--commit", revision, command=command)
            self.assertEqual(refused.returncode, 2)
            self.assertIn("the following arguments are required: --selected-option", refused.stderr)
            admit.assert_not_called()
            result = self.run_cli("--commit", revision, command=(*command, "--selected-option"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr, "copies: Fixture reached admission\n")
        admit.assert_called_once()

    def test_run_preserves_a_legacy_gate_without_a_validator(self) -> None:
        revision = self.commit_gate_script("""\
            import argparse

            assert __name__ == "__main__"
            parser = argparse.ArgumentParser()
            parser.add_argument("--targeted", action="store_true", required=True)
            parser.parse_args()
            """)
        _ = self.commit_gate_validator()
        run_gate = mock.Mock(side_effect=launch_copy_fixture)
        admitted = mock.Mock(spec=first_step.FirstStepAdmission, run_gate=run_gate)
        with mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)) as admit:
            result = self.run_cli("--commit", revision,
                                  command=(sys.executable, "scripts/production/gate.py", "--targeted"))
        self.assertEqual(result.returncode, 0, result.stderr)
        admit.assert_called_once()
        run_gate.assert_called_once()

    def test_run_does_not_validate_an_unrelated_gate_script_or_module(self) -> None:
        _ = self.commit_gate_validator()
        vendor_script = "vendor/tool/scripts/production/gate.py"
        for name in ("tools/gate.py", vendor_script):
            script = self.source / name
            script.parent.mkdir(parents=True)
            _ = script.write_text("import sys\nassert sys.argv[1:] == ['--report']\n")
            _ = self.git(self.source, "add", str(script))
        _ = self.git(self.source, "commit", "-qm", "Add the report commands")
        for program in (("tools/gate.py",), ("-m", "tools.gate"), (vendor_script,)):
            with self.subTest(program=program):
                run_gate = mock.Mock(side_effect=launch_copy_fixture)
                admitted = mock.Mock(spec=first_step.FirstStepAdmission, run_gate=run_gate)
                with mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)):
                    result = self.run_cli(command=(sys.executable, *program, "--report"))
                self.assertEqual(result.returncode, 0, result.stderr)
                run_gate.assert_called_once()

    def test_run_passes_qualified_gate_module_arguments_to_the_child(self) -> None:
        _ = self.commit_gate_script("""\
            import sys

            def check_prepared_gate_source(arguments, parser, source):
                raise AssertionError("A module invocation reached validation")

            assert sys.argv[1:] == ["--tests-only", "--module", "x"]
            """)
        run_gate = mock.Mock(side_effect=launch_copy_fixture)
        admitted = mock.Mock(spec=first_step.FirstStepAdmission, run_gate=run_gate)
        command = (sys.executable, "-m", "scripts.production.gate")
        with mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)) as admit:
            refused = self.run_cli(command=(*command, "--module", "x"))
            self.assertEqual(refused.returncode, 2)
            self.assertEqual(refused.stderr, "copies: --module needs --tests-only or --affected\n")
            admit.assert_not_called()
            result = self.run_cli(command=(*command, "--tests-only", "--module", "x"))
        self.assertEqual(result.returncode, 0, result.stderr)
        admit.assert_called_once()
        run_gate.assert_called_once()

    def test_run_launches_the_qualified_gate_without_a_prepared_handoff(self) -> None:
        revision = self.commit_gate_script(f"""\
            import os
            from pathlib import Path
            import sys

            def parse(arguments, parser):
                parser.add_argument("--repo")
                return parser.parse_args(arguments)

            def check_prepared_gate_source(arguments, parser, source):
                parse(arguments, parser)

            if __name__ == "__main__":
                import argparse

                root = Path.cwd()
                assert Path(__file__).parents[2] == root
                assert parse(sys.argv[1:], argparse.ArgumentParser()).repo == str(root)
                assert {copies.PREPARED_GATE_FD!r} not in os.environ
            """)
        _ = self.commit_gate_script("raise SystemExit(9)\n")
        run_gate = mock.Mock(side_effect=launch_copy_fixture)
        admitted = mock.Mock(spec=first_step.FirstStepAdmission, run_gate=run_gate)
        with mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)):
            result = self.run_cli("--commit", revision,
                command=(sys.executable, "-m", "scripts.production.gate", "--repo", "{tree}"))
        self.assertEqual(result.returncode, 0, result.stderr)
        run_gate.assert_called_once()

    def test_run_prints_gate_help_without_admission(self) -> None:
        _ = self.commit_gate_validator()
        with mock.patch.object(first_step, "admitted_run") as admit:
            result = self.run_cli(command=(sys.executable, "scripts/production/gate.py", "--help"))
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage: gate.py", result.stdout)
        admit.assert_not_called()

    def test_run_uses_tree_cwd_substitution_and_command_exit_status(self) -> None:
        output = self.base / "command-result.json"
        evidence = self.base / "build-evidence.json"
        program = "\n".join([
            "import json,os,subprocess,sys; from pathlib import Path",
            "Path(sys.argv[1]).write_text(json.dumps([os.getcwd(),sys.argv[2]]))",
            "for relative, content, mode in [('target/debug/x', b'binary\\x00x', 0o751), "
            + "('target/release/y', b'binary\\x00y', 0o705)]:",
            "    path = Path(relative)",
            "    path.parent.mkdir(parents=True, exist_ok=True)",
            "    path.write_bytes(content)",
            "    path.chmod(mode)",
            "Path('target/debug/broken').mkdir(exist_ok=True)",
            "Path(sys.argv[3]).write_text(json.dumps(["
            + "subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(), "
            + "str(Path('target').resolve())]))",
            "sys.exit(int(sys.argv[4]))",
        ])
        verifier = self.base / "verify.sh"
        _ = verifier.write_text(dedent("""\
            python=$1 script=$2
            shift 2
            tree=$(git write-tree)
            exec "$python" "$script" hold --repo "$COPIES_STEP_SOURCE" --tree "$tree" \
                -- "$python" -c "$@"
            """))
        copy_write = program.replace("Path(relative)", "Path(os.environ['CARGO_TARGET_DIR']) / Path(relative).relative_to('target')").replace(
            "Path('target/debug/broken')", "(Path(os.environ['CARGO_TARGET_DIR']) / 'debug/broken')")
        cases = (
            ("plain", (), 7, False),
            ("published", ("target/debug/x", "target/release/y"), 0, False),
            ("command-failed", ("target/debug/x", "target/release/y"), 7, False),
            ("applied", ("target/debug/x",), 0, True),
            ("partly-built", ("target/debug/x", "target/debug/missing"), 7, False),
            ("not-built", ("target/debug/missing",), 0, False),
            ("not-built-command-failed", ("target/debug/missing",), 7, False),
            ("copy-failed", ("target/debug/x", "target/debug/broken"), 0, False),
            ("copy-and-command-failed", ("target/debug/x", "target/debug/broken"), 7, False),
            ("retained-copy-file", ("target/debug/x",), 0, False),
            ("touched-copy-file", ("target/debug/x",), 0, False),
            ("explicit-target-dir", ("target/debug/x",), 0, False),
            ("two-changed-target-dirs", ("target/debug/x",), 0, False),
            ("metadata-only", ("target/debug/x",), 0, False),
            ("copy-symlink-loop", ("target/debug/loop", "report.txt"), 0, False),
            ("replaced-copy-symlink-loop", ("target/debug/loop",), 0, False),
        )
        for label, requested, command_exit, apply in cases:
            with self.subTest(case=label):
                previous_target = self.gates / "1/target"
                if previous_target.is_dir():
                    shutil.rmtree(previous_target)
                options = [part for path in requested for part in ("--publish", path)]
                if apply:
                    _ = (self.source / "untracked.txt").write_text("gate publication\n")
                    options.extend(("--apply-uncommitted", str(self.source)))
                folders_before = {folder.path for folder in scratch.folders()}
                arguments = (str(output), "checkout={tree}/tracked.txt", str(evidence), str(command_exit))
                if label in ("retained-copy-file", "touched-copy-file", "metadata-only", "copy-symlink-loop",
                             "replaced-copy-symlink-loop"):
                    with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as copy:
                        old = copy / "target/debug/x"
                        old.parent.mkdir(parents=True, exist_ok=True)
                        _ = old.write_bytes(b"old binary")
                        if label in ("copy-symlink-loop", "replaced-copy-symlink-loop"):
                            loop = old.with_name("loop")
                            loop.unlink(missing_ok=True)
                            loop.symlink_to(loop.name)
                    touch = "from pathlib import Path\nPath('target/debug/x').touch()\n"
                    copy_command = copy_write
                    if label == "touched-copy-file":
                        copy_command = touch + copy_write
                    elif label == "metadata-only":
                        copy_command = touch
                    elif label == "copy-symlink-loop":
                        copy_command = "from pathlib import Path\nPath('report.txt').write_text('step report\\n')"
                    elif label == "replaced-copy-symlink-loop":
                        copy_command = dedent("""
                            from pathlib import Path
                            path = Path("target/debug/loop")
                            path.unlink()
                            path.write_bytes(b"replacement")
                            """)
                    result = self.run_cli(*options, command=(
                        "sh", "-eu", str(verifier), sys.executable,
                        str(Path(copies.__file__).resolve()), copy_command, *arguments,
                    ))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    published = [Path(line.removeprefix("published: ").rsplit(" ", 1)[0])
                                 for line in result.stdout.splitlines() if line.startswith("published: ")]
                    if label == "metadata-only":
                        self.assertEqual(published, [])
                    elif label == "copy-symlink-loop":
                        self.assertEqual([path.name for path in published], ["report.txt"], result.stderr)
                        self.assertEqual(published[0].read_text(), "step report\n")
                    elif label == "replaced-copy-symlink-loop":
                        self.assertEqual([path.name for path in published], ["loop"], result.stderr)
                        self.assertEqual(published[0].read_bytes(), b"replacement")
                    else:
                        self.assertEqual(len(published), 1, result.stderr)
                        self.assertEqual(published[0].read_bytes(), b"binary\x00x")
                    expected = b"binary\x00x" if label in ("retained-copy-file", "touched-copy-file") else b"old binary"
                    self.assertEqual(old.read_bytes(), expected)
                    continue
                if label in ("explicit-target-dir", "two-changed-target-dirs"):
                    inherited = self.base / label
                    stale = inherited / "debug/x"
                    stale.parent.mkdir(parents=True)
                    _ = stale.write_bytes(b"stale inherited binary")
                    target_program = program.replace(
                        "Path(relative)", "Path(sys.argv[-1]) / Path(relative).relative_to('target')",
                    ).replace("Path('target/debug/broken')", "(Path(sys.argv[-1]) / 'debug/broken')")
                    target_directory = "target"
                    if label == "two-changed-target-dirs":
                        target_directory = "target/override"
                        target_program = ("import os; from pathlib import Path\n"
                            + "path = Path(os.environ['CARGO_TARGET_DIR'], 'debug/x')\n"
                            + "path.parent.mkdir(parents=True, exist_ok=True)\n"
                            + "path.write_bytes(b'new default binary')\n"
                            + target_program)
                    with mock.patch.dict(os.environ, {"CARGO_TARGET_DIR": str(inherited)}):
                        result = self.run_cli(*options, command=(
                            sys.executable, "-c", target_program, *arguments, "--target-dir", target_directory,
                        ))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    published = [Path(line.removeprefix("published: ").rsplit(" ", 1)[0])
                                 for line in result.stdout.splitlines() if line.startswith("published: ")]
                    self.assertEqual(len(published), 1, result.stderr)
                    self.assertEqual(published[0].read_bytes(), b"binary\x00x")
                    self.assertEqual(stale.read_bytes(), b"stale inherited binary")
                    continue
                result = self.run_cli(*options, command=(sys.executable, "-c", program, *arguments))
                self.assertEqual(result.returncode, command_exit, result.stderr)
                row = cast(list[str], json.loads(output.read_text()))
                checkout = Path(row[0])
                self.assertEqual(row, [str(checkout), f"checkout={checkout}/tracked.txt"])
                self.assertEqual(checkout, self.gates / "1")
                self.assertEqual(build_folders.build_folder_state(checkout), "free")
                commit, target = cast(list[str], json.loads(evidence.read_text()))
                self.assertEqual(Path(target), checkout / "target")
                if apply:
                    self.assertNotEqual(commit, self.head)
                else:
                    self.assertEqual(commit, self.head)
                lines = [line for line in result.stdout.splitlines() if line.startswith("published: ")]
                folders_after = {folder.path for folder in scratch.folders()}
                if "target/debug/broken" in requested:
                    self.assertIn("copies: publish:", result.stderr)
                    self.assertIn("Is a directory", result.stderr)
                    self.assertEqual(lines, [], result.stdout)
                    self.assertEqual(folders_after, folders_before)
                    continue
                missing = [path for path in requested if path.endswith("/missing")]
                self.assertEqual(result.stderr.splitlines(),
                                 [f"copies: publish: {path} not built" for path in missing])
                built = [path for path in requested if path not in missing]
                self.assertEqual(len(lines), len(built), result.stdout)
                if not built:
                    self.assertEqual(folders_after, folders_before)
                    continue
                published = [Path(line.removeprefix("published: ").rsplit(" ", 1)[0]) for line in lines]
                publication = published[0].parent
                self.assertEqual(folders_after - folders_before, {publication})
                self.assertTrue(publication.is_relative_to(self.base / "scratch"))
                self.assertFalse(publication.is_relative_to(checkout))
                self.assertEqual([path.parent for path in published], [publication] * len(built))
                self.assertEqual([path.name for path in published], [Path(path).name for path in built])
                self.assertEqual([line.rsplit(" ", 1)[1] for line in lines], [commit] * len(built))
                record = scratch.task_of(publication)
                assert isinstance(record, copy_record.Registered)
                self.assertTrue(record.fields["throwaway"])
                self.assertTrue(record.task.startswith("until:"))
                manifest = cast(dict[str, object], json.loads((publication / "manifest.json").read_text()))
                self.assertEqual(manifest, {
                    "repository": self.source.name, "commit": commit, "command_exit": command_exit,
                    "files": [path.name for path in published],
                })
                files = {"target/debug/x": (b"binary\x00x", 0o751), "target/release/y": (b"binary\x00y", 0o705)}
                expected = [files[relative] for relative in built]
                copies.remove_trees(self.owner_id)
                for path, (content, mode) in zip(published, expected, strict=True):
                    self.assertTrue(path.is_absolute())
                    self.assertFalse(path.is_symlink())
                    self.assertTrue(stat.S_ISREG(path.stat().st_mode))
                    self.assertEqual(path.read_bytes(), content)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

    def test_list_reports_tree_owner_repo_commit_busy_pid_and_target_size(self) -> None:
        mirror = copies.RemoteMirror(self.owner_id, copies.SelectedRepository(self.source.name),
                                     'remote-host', 'mirror/mixed-records', 'former-holder')
        for layout in ('local-only', 'mixed'):
            with self.subTest(layout=layout):
                if layout == 'mixed':
                    copies.register_remote(mirror)
                with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
                    result = self.cli('list')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    line = next(line for line in result.stdout.splitlines() if str(tree) in line)
                    for value in ('copy-owner', 'origin', self.head[:7], 'busy', str(os.getpid())):
                        self.assertIn(value, line)
                    self.assertIn(f'\t{host_name()}\t', line)
                result = self.cli('list')
                self.assertEqual(result.returncode, 0, result.stderr)
                line = next(line for line in result.stdout.splitlines() if str(tree) in line)
                self.assertIn('idle', line)
                self.assertRegex(line, r'\s\d+(?:\.\d+)?[KMGTPE]?B?\s*$')
                lines = result.stdout.splitlines()
                columns = lines[0].split('	')
                self.assertEqual(columns, ['number', 'owner', 'repository', 'host', 'path', 'session',
                                           'commit', 'state', 'holder_pid', 'target'])
                rows = [dict(zip(columns, row.split('	'), strict=True)) for row in lines[1:]]
                self.assertEqual([row['number'] for row in rows], ['1'] if layout == 'local-only' else ['1', '2'])
                if layout == 'mixed':
                    remote = next(row for row in rows if row['path'] == mirror.path)
                    self.assertEqual((remote['owner'], remote['repository'], remote['host']),
                                     ('copy-owner', self.source.name, 'remote-host'))
                    self.assertEqual((remote['state'], remote['session'], remote['holder_pid']),
                                     expected_remote_lease_columns('idle', '-', '-'))

    def test_list_owner_display_preserves_gate_folder_names(self) -> None:
        for staff_read in ('readable', 'unreadable'):
            with self.subTest(staff=staff_read), ExitStack() as contexts:
                with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
                    _ = staff.rename(staff.StaffId(self.owner_id), 'renamed')
                    if staff_read == 'unreadable':
                        _ = cast(object, contexts.enter_context(mock.patch.dict(os.environ, {'STAFF_FILE': str(self.base)})))
                    before = self.snapshot(tree)
                    result = self.cli('list')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(tree.name, '1')
                    self.assertEqual(self.snapshot(tree), before)
                    self.assertIn(f"\t{tree.parent.name}\t", result.stdout)

    def test_remove_refuses_busy_tree_then_deletes_both_idle_trees_and_repeats(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as first:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as second:
                result = self.cli('remove', '--repo', str(self.source))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertTrue(first.is_dir())
                self.assertTrue(second.is_dir())
                self.assertEqual(build_folders.build_folder_state(first), 'held')
                self.assertEqual(build_folders.build_folder_state(second), 'held')
        result = self.cli('remove', '--repo', str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(self.cli('remove', '--repo', str(self.source)).returncode, 0)

    def test_owner_removal_deletes_a_gate_while_its_workspace_is_live(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as gate:
            pass
        with registered_workspace(self.owner_id, self.source, self.head, name="live", branch="worker") as tree:
            with self.assertRaises(copies.CopyError) as refusal:
                copies.remove_trees(self.owner_id)
            self.assertIn(str(tree), str(refusal.exception))
            self.assertEqual(len(str(refusal.exception).splitlines()), 1)
            self.assertTrue(gate.exists())
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
            record = scratch.task_of(tree)
            assert isinstance(record, copy_record.Registered)
            self.assertNotIn("ended_task", record.fields)

    def idle_gate_source(self) -> Path:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as gate:
            return gate

    def test_owner_removal_preserves_a_gate_and_its_busy_workspace_borrower(self) -> None:
        gate = self.idle_gate_source()
        with registered_workspace(self.owner_id, gate, self.head, name='borrower', branch='worker') as tree:
            with self.assertRaises(copies.CopyError) as refusal:
                copies.remove_trees(self.owner_id)
            self.assertIn(str(tree), str(refusal.exception))
            self.assertTrue(gate.exists())
            self.assertEqual(self.git(tree, 'rev-parse', 'HEAD'), self.head)
            record = scratch.task_of(tree)
            assert isinstance(record, copy_record.Registered)
            self.assertNotIn('ended_task', record.fields)

    def test_owner_removal_deletes_an_idle_workspace_before_its_gate_source(self) -> None:
        gate = self.idle_gate_source()
        with registered_workspace(self.owner_id, gate, self.head, name="borrower", branch="worker") as tree:
            pass
        copies.remove_trees(self.owner_id)
        self.assertFalse(tree.exists())
        self.assertTrue(gate.exists())

    def test_owner_removal_preserves_a_gate_borrowed_by_another_owners_workspace(self) -> None:
        gate = self.idle_gate_source()
        with registered_workspace('other-unit', gate, self.head, name='borrower', branch='worker') as tree:
            pass
        copies.remove_trees(self.owner_id)
        self.assertTrue(gate.exists())
        self.assertEqual(self.git(tree, 'rev-parse', 'HEAD'), self.head)
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        self.assertNotIn('ended_task', record.fields)

    def test_owner_removal_reads_relative_alternates_despite_a_damaged_workspace_record(self) -> None:
        gate = self.idle_gate_source()
        with registered_workspace(self.owner_id, gate, self.head, name='borrower', branch='worker') as tree:
            pass
        objects = tree / '.git/objects'
        common = Path(self.git(gate, 'rev-parse', '--path-format=absolute', '--git-common-dir'))
        _ = (objects / 'info/alternates').write_text(os.path.relpath(common / 'objects', objects) + '\n')
        record = tree / copy_record.RECORD
        _ = record.write_text('{invalid')
        with self.assertRaises(copies.CopyError) as refusal:
            copies.remove_trees(self.owner_id)
        self.assertIn(str(record), str(refusal.exception))
        self.assertTrue(gate.exists())
        self.assertEqual(self.git(tree, 'rev-parse', 'HEAD'), self.head)

    def test_owner_removal_checks_the_borrowed_path_of_a_gate_tombstone(self) -> None:
        gate = self.idle_gate_source()
        with registered_workspace('other-unit', gate, self.head, name='borrower', branch='worker') as tree:
            pass
        legacy = self.base / 'old-gate.removing'
        legacy.mkdir(parents=True)
        _ = (legacy / 'source.txt').write_text('legacy work')
        copies.remove_trees(self.owner_id)
        self.assertTrue(gate.exists())
        self.assertEqual((legacy / 'source.txt').read_text(), 'legacy work')
        self.assertEqual(self.git(tree, 'rev-parse', 'HEAD'), self.head)

    def test_owner_removal_deletes_an_idle_repository_while_another_gate_is_busy(self) -> None:
        other = self.base / 'other-repository'
        _ = self.git(self.base, 'clone', '-q', str(self.source), str(other))
        _ = self.git(other, 'remote', 'set-url', 'origin', 'https://example.invalid/other.git')
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as busy:
            with copies.standing(self.owner_id, other, copies.CommittedSource('HEAD')) as idle:
                pass
            copies.remove_trees(self.owner_id)
            self.assertTrue(idle.exists())
            self.assertEqual(build_folders.build_folder_state(idle), 'free')
            self.assertEqual(build_folders.build_folder_state(busy), 'held')
        copies.remove_trees(self.owner_id)
        self.assertTrue(busy.exists())

    def test_remove_cli_reports_a_kept_workspace_and_a_removed_gate(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as gate:
            pass
        with registered_workspace(self.owner_id, self.source, self.head, name="live", branch="worker") as tree:
            result = self.cli("remove")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(str(tree), result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertTrue(gate.exists())
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)

    def test_owner_removal_reports_each_workspace_with_unsaved_work_on_a_separate_line(self) -> None:
        trees: list[Path] = []
        for name in ("first", "second"):
            with registered_workspace(self.owner_id, self.source, self.head, name=name, branch="worker") as tree:
                _ = (tree / "tracked.txt").write_text(f"unsaved {name}\n")
                trees.append(tree)
        with self.assertRaises(copies.CopyError) as refusal:
            copies.remove_trees(self.owner_id)
        failures = str(refusal.exception).splitlines()
        self.assertEqual(len(failures), 2, str(refusal.exception))
        for tree, failure in zip(trees, failures, strict=True):
            self.assertIn(str(tree), failure)
            self.assertIn("kept", failure)
            self.assertEqual((tree / "tracked.txt").read_text(), f"unsaved {tree.name}\n")
        refs = self.git(self.source, "for-each-ref", "--format=%(refname)", "refs/rescue/").splitlines()
        self.assertEqual(len(refs), 2)
        self.assertEqual({self.git(self.source, "show", ref + ":tracked.txt") for ref in refs},
                         {"unsaved first", "unsaved second"})

    def test_owner_removal_covers_repositories_and_editable_copies(self) -> None:
        other = ImmutableGitSeed.create(self.base / "other", "main", "Gate Tests",
                                        "gate-tests@example.invalid", {"Cargo.toml": "[workspace]\n", "other.txt": "content"}).checkout
        other = other.rename(other.with_name("other-repository"))
        _ = self.git(other, "remote", "set-url", "origin", "https://example.invalid/other.git")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as first:
            pass
        with copies.standing(self.owner_id, other, copies.CommittedSource("HEAD")) as second:
            pass
        with registered_workspace(self.owner_id, self.source, self.head,
                              name="worker", branch="worker") as worker:
            pass
        legacy = self.base / "legacy-run/worker"
        legacy.mkdir(parents=True)
        copies.initialize_git(legacy, self.source)
        _ = self.git(legacy, "checkout", "-q", "-B", "legacy", self.head)
        with registered_workspace(self.owner_id, self.source, self.head, name="legacy", branch="legacy",
                              task="gate:legacy-run", path=legacy):
            pass
        with copies.standing("other-unit", self.source, copies.CommittedSource(self.head)) as kept:
            pass
        narrowed = self.cli("remove", "--repo", str(other))
        self.assertEqual(narrowed.returncode, 0, narrowed.stderr)
        self.assertTrue(second.exists())
        self.assertTrue(first.exists())
        result = self.cli("remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(first.exists())
        self.assertFalse(worker.exists())
        self.assertFalse(legacy.exists())
        self.assertNotIn(str(legacy), scratch.holds())
        self.assertTrue(kept.exists())
        copies.remove_trees(self.owner_id)

    def test_owner_removal_retries_busy_and_interrupted_trees_without_a_source(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.head, name='removal-retry', branch='removal-retry') as tree:
            result = self.cli('remove')
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('busy', result.stderr)
            self.assertTrue(tree.exists())
        with mock.patch.object(scratch, 'remove', side_effect=OSError('interrupted owner removal')):
            result = self.cli('remove')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIsInstance(scratch.pending_removal(tree), copy_record.Registered)
        _ = self.git(tree, "repack", "-a", "-d")
        (tree / ".git/objects/info/alternates").unlink()
        shutil.rmtree(self.source)
        result = self.cli('remove')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(tree.exists())
        self.assertEqual(self.cli('remove').returncode, 0)

    def test_owner_removal_finishes_a_deleted_editable_copy(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.head,
                              name="deleted-worker", branch="worker") as tree:
            pass
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        _ = scratch.hold(tree, record.task, "keep the worker during its run")
        delete = scratch.remove

        def interrupt(path: Path) -> None:
            delete(path)
            raise OSError("interrupted after folder deletion")

        with mock.patch.object(scratch, "remove", side_effect=interrupt):
            result = self.cli("remove")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(tree.exists())
        self.assertTrue(scratch.removal_mark(tree).exists())
        result = self.cli("remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(scratch.removal_mark(tree).exists())
        self.assertNotIn(str(tree), scratch.holds())
        self.assertEqual(self.cli("remove").returncode, 0)

    def test_busy_editable_copy_identifies_its_holder(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.head,
                              name="busy-worker", branch="worker") as tree:
            with mock.patch.object(sessions, "running_sessions", return_value=[]):
                result = self.cli("remove")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(str(tree), result.stderr)
            self.assertIn(f"pid {os.getpid()}", result.stderr)

    def test_repository_removal_skips_a_workspace_with_a_deleted_source(self) -> None:
        other = ImmutableGitSeed.create(self.base / "other", "main", "Gate Tests",
                                        "gate-tests@example.invalid", {"Cargo.toml": "[workspace]\n", "other.txt": "content"}).checkout
        other = other.rename(other.with_name("unrelated-repository"))
        with registered_workspace(self.owner_id, other, "HEAD", name="other-worker", branch="worker") as kept:
            pass
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            pass
        shutil.rmtree(other)
        result = self.cli("remove", "--repo", str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(tree.exists())
        self.assertTrue(kept.exists())

    def test_explicit_repository_scopes_select_only_the_requested_repository(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            pass
        copies.remove_trees(self.owner_id, copies.SelectedRepository(self.source.name))
        self.assertTrue(tree.exists())
        copies.remove_trees(self.owner_id, copies.AllRepositories())

    def test_owner_removal_requires_a_repository_scope_in_the_python_api(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            pass
        for value in ("", str(self.source), self.source):
            with self.subTest(value=value), self.assertRaisesRegex(copies.CopyError, "repository scope"):
                copies.remove_trees(self.owner_id, cast(copies.RepositoryScope, cast(object, value)))
            self.assertTrue(tree.exists())
        copies.remove_trees(self.owner_id, copies.AllRepositories())
        self.assertTrue(tree.exists())

    def test_owner_removal_allows_an_ordinary_copy_with_a_crate_name(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.head, name="crate-notes", branch="notes") as tree:
            pass
        copies.remove_trees(self.owner_id)
        self.assertFalse(tree.exists())
        for stage in ("integrated", "abandoned"):
            with self.subTest(crate_stage=stage):
                name = "crate-" + stage
                terminal = copies.workspace_path(self.owner_id, self.source, name)
                active = copies.workspace_path(self.owner_id, self.source, name + "-active")
                manifest_path = self.base / (name + ".json")
                crates = {"terminal": {"checkout": str(terminal), "stage": stage},
                          "active": {"checkout": str(active), "stage": "selected"}}
                manifest = {"version": 2, "canonical": str(self.source), "base": self.head, "crates": crates}
                _ = manifest_path.write_text(json.dumps(manifest))
                with registered_workspace(self.owner_id, self.source, self.head, name=name,
                                      branch="worker", run_manifest=manifest_path):
                    pass
                with registered_workspace(self.owner_id, self.source, self.head, name=name + "-active",
                                      branch="worker", run_manifest=manifest_path):
                    pass
                with self.assertRaisesRegex(copies.CopyError, "not terminal") as refused:
                    copies.remove_trees(self.owner_id)
                self.assertIn(str(active), str(refused.exception))
                self.assertFalse(terminal.exists())
                self.assertTrue(active.exists())
                crates["active"]["stage"] = "abandoned"
                _ = manifest_path.write_text(json.dumps(manifest))
                copies.remove_trees(self.owner_id)
                self.assertFalse(active.exists())

    def test_owner_removal_preserves_a_crate_copy_without_terminal_evidence(self) -> None:
        name = "crate-recovery"
        tree = copies.workspace_path(self.owner_id, self.source, name)
        manifest_path = self.base / "crate-recovery.json"
        manifest = json.dumps({
            "version": 2, "canonical": str(self.source), "base": self.head,
            "crates": {"sample": {"checkout": str(tree), "stage": "selected"}},
        })
        _ = manifest_path.write_text(manifest)
        with registered_workspace(self.owner_id, self.source, self.head, name=name, branch="worker",
                                  run_manifest=manifest_path) as worker:
            self.assertEqual(worker, tree)
        _ = scratch.hold(tree, f"gate:{name}", copies.LEGACY_WORKER_HOLD_REASON)
        scratch.hand_over(tree, self.owner_id, "gate:changed-task")
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)

        with self.subTest(evidence="blank run link"):
            _ = scratch.update_record(tree, lambda _: {**record.fields, "crate_run": ""})
            with self.assertRaisesRegex(copies.CopyError, "repair crate run link"):
                copies.remove_trees(self.owner_id)
            self.assertTrue(tree.is_dir())
            self.assertEqual(manifest_path.read_text(), manifest)
            self.assertIn(str(tree), scratch.holds())
        _ = scratch.update_record(tree, lambda _: record.fields.copy())

        for evidence in ("missing", "corrupt"):
            with self.subTest(evidence=evidence):
                if evidence == "missing":
                    manifest_path.unlink()
                else:
                    _ = manifest_path.write_text("{invalid")
                with self.assertRaisesRegex(copies.CopyError, "repair crate run") as refused:
                    copies.remove_trees(self.owner_id)
                self.assertIn(str(manifest_path), str(refused.exception))
                self.assertTrue(tree.is_dir())
            _ = manifest_path.write_text(manifest)

        with mock.patch.object(scratch, "remove", side_effect=OSError("interrupted removal")):
            with self.assertRaisesRegex(OSError, "interrupted removal"):
                scratch.remove_marked(tree)
        marker = scratch.removal_mark(tree)
        saved = marker.read_bytes()
        for missing in (tree / ".git", tree):
            with self.subTest(missing=missing.name):
                shutil.rmtree(missing)
                with self.assertRaisesRegex(copies.CopyError, "not terminal") as refused:
                    copies.remove_trees(self.owner_id)
                self.assertIn(str(manifest_path), str(refused.exception))
                self.assertEqual(marker.read_bytes(), saved)
                self.assertEqual(manifest_path.read_text(), manifest)
                self.assertEqual(tree.exists(), missing != tree)

    def test_owner_removal_preserves_a_source_with_an_adopted_borrower(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.head, name="parent", branch="worker") as parent:
            pass
        child = self.base / "adopted/borrower"
        child.mkdir(parents=True)
        copies.initialize_git(child, parent)
        _ = self.git(child, "checkout", "-q", "-B", "borrower", self.head)
        with registered_workspace("other-unit", parent, self.head, name="borrower", branch="borrower", path=child):
            pass
        with self.assertRaisesRegex(copies.CopyError, "borrowed") as refusal:
            copies.remove_trees(self.owner_id)
        self.assertIn(str(child), str(refusal.exception))
        self.assertTrue(parent.exists())
        self.assertEqual(self.git(child, "cat-file", "-p", self.head), self.git(parent, "cat-file", "-p", self.head))
        for pending in (child, parent):
            with self.subTest(pending=pending):
                with mock.patch.object(scratch, "remove", side_effect=RuntimeError("interrupted removal")):
                    with self.assertRaisesRegex(RuntimeError, "interrupted removal"):
                        scratch.remove_marked(pending)
                with self.assertRaisesRegex(copies.CopyError, "borrowed"):
                    copies.remove_trees(self.owner_id)
                self.assertTrue(parent.exists())
                self.assertEqual(self.git(child, "rev-parse", "HEAD"), self.head)
        copies.remove_trees("other-unit")
        copies.remove_trees(self.owner_id)
        self.assertFalse(child.exists())
        self.assertFalse(parent.exists())

    def test_owner_removal_matches_transferred_names_without_constructing_unsafe_paths(self) -> None:
        for owner in ("Verify", "unit_name"):
            with self.subTest(owner=owner):
                with registered_workspace(self.owner_id, self.source, self.head, name=owner, branch="worker") as tree:
                    pass
                record = scratch.task_of(tree)
                assert isinstance(record, copy_record.Registered)
                scratch.hand_over(tree, owner, record.task)
                copies.remove_trees(owner)
                self.assertFalse(tree.exists())
        for owner in ("../escape", str(self.base / "outside")):
            with self.subTest(owner=owner), mock.patch.object(copies, "workspace_path", side_effect=AssertionError("unsafe path")):
                copies.remove_trees(owner)

    def test_owner_removal_finds_adopted_copies_with_a_changed_hold_reason(self) -> None:
        tree = self.base / "adopted/worker"
        tree.mkdir(parents=True)
        copies.initialize_git(tree, self.source)
        _ = self.git(tree, "checkout", "-q", "-B", "worker", self.head)
        with registered_workspace(self.owner_id, self.source, self.head, name="adopted", branch="worker", path=tree):
            pass
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        scratch.hand_over(tree, "Next_unit", record.task)
        _ = scratch.hold(tree, record.task, "a different hold reason")
        copies.remove_trees("Next_unit")
        self.assertFalse(tree.exists())
        self.assertNotIn(str(tree), scratch.holds())

    def test_owner_transfer_after_selection_preserves_the_new_owners_copy(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.head, name="transfer", branch="worker") as tree:
            pass
        select_workspaces = copies.owned_workspaces

        def transfer(owner: str, repo: copies.RepositoryScope) -> list[tuple[Path, copy_record.Registered]]:
            selected = select_workspaces(owner, repo)
            scratch.hand_over(tree, "next-unit", selected[0][1].task)
            return selected

        with mock.patch.object(copies, "owned_workspaces", side_effect=transfer):
            with self.assertRaisesRegex(copies.CopyError, "changed"):
                copies.remove_trees(self.owner_id)
        self.assertTrue(tree.exists())
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        self.assertEqual(record.fields["owner_unit"], "next-unit")
        self.assertNotIn("ended_task", record.fields)

    def test_owner_removal_deletes_borrowers_before_their_sources(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.head, name="a-parent", branch="worker") as parent:
            pass
        with registered_workspace(self.owner_id, parent, self.head, name="z-child", branch="worker") as child:
            pass
        copies.remove_trees(self.owner_id)
        self.assertFalse(child.exists())
        self.assertFalse(parent.exists())

    def test_owner_removal_preserves_an_external_borrower_and_does_not_end_a_busy_copy(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.head, name="a-source", branch="worker") as parent:
            with self.assertRaisesRegex(copies.CopyError, "busy"):
                copies.remove_trees(self.owner_id)
            record = scratch.task_of(parent)
            assert isinstance(record, copy_record.Registered)
            self.assertNotIn("ended_task", record.fields)
        with registered_workspace("other-unit", parent, self.head, name="borrower", branch="worker") as child:
            pass
        with self.assertRaisesRegex(copies.CopyError, "borrowed"):
            copies.remove_trees(self.owner_id)
        self.assertTrue(parent.exists())
        self.assertTrue(child.exists())

    def test_owner_removal_refuses_a_damaged_record_until_repaired(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as gate:
            pass
        with registered_workspace(self.owner_id, self.source, self.head, name="damaged", branch="worker") as tree:
            pass
        path = tree / copy_record.RECORD
        record = path.read_text()
        _ = path.write_text("{invalid")
        with self.assertRaisesRegex(copies.CopyError, "repair record") as refusal:
            copies.remove_trees(self.owner_id)
        self.assertIn(str(path), str(refusal.exception))
        self.assertTrue(tree.exists())
        self.assertTrue(gate.exists())
        _ = path.write_text(record)
        copies.remove_trees(self.owner_id)
        self.assertFalse(tree.exists())

    def test_invalid_owner_refuses_without_creating_a_tree(self) -> None:
        for owner in ("../escape", "Verify", "unit/name", "-unit", ""):
            with self.subTest(owner=owner):
                with self.assertRaises(copies.CopyError), copies.standing(owner, self.source, copies.CommittedSource("HEAD")):
                    self.fail("invalid owner started a tree")
                self.assertFalse(self.gates.exists())

    def test_underscore_owner_succeeds_in_a_numbered_copy(self) -> None:
        member = fixture_member("unit", name="unit_name",
            parent=next(row.id for row in staff.local_snapshot().rows if row.kind == "showrunner"))
        result_file = self.base / "command-owner"
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": member.current_session, "CODEX_THREAD_ID": ""}):
            result = self.run_cli(command=(sys.executable, "-c",
                "import json,os,sys; from pathlib import Path; " +
                "Path(sys.argv[1]).write_text(json.dumps([os.environ['COPIES_STEP_OWNER'], os.getcwd()]))",
                str(result_file)))
        self.assertEqual(result.returncode, 0, result.stderr)
        owner, checkout = cast(list[str], json.loads(result_file.read_text()))
        self.assertEqual(owner, str(member.id))
        self.assertEqual(Path(checkout), self.gates.with_name("unit_name") / "1")
        self.assertEqual(build_folders.build_folder_state(Path(checkout)), "free")
        with copies.standing(owner, self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(tree, self.gates.with_name("unit_name") / "1")
        self.assertFalse(self.gates.exists())

    def test_record_is_local_metadata_and_repeated_use_preserves_tree(self) -> None:
        with copies.standing(self.owner_id, str(self.source), copies.CommittedSource(self.head)) as tree:
            stable = (tree / 'stable.txt').stat()
            record = self.lease(tree)
            self.assertEqual(record['unit'], self.gates.name)
            self.assertEqual(record['repository'], 'origin')
            self.assertEqual(record['commit'], self.head)
            self.assertEqual(record['pid'], os.getpid())
            self.assertIsInstance(record['last_use'], str)
            self.assertEqual(self.git(tree, 'status', '--porcelain'), '')
            self.assertFalse((tree / '.gate.json').exists())
            self.assertTrue(tree.with_name(tree.name + '.lease.json').is_file())
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual((reused / 'stable.txt').stat().st_mtime_ns, stable.st_mtime_ns)
            self.assertEqual(self.git(reused, 'status', '--porcelain'), '')
        self.assertEqual(list((self.base / 'workspaces').glob('**/.scratch.json')), [])

    def test_interrupted_first_record_publication_preserves_the_unregistered_folder(self) -> None:
        program = dedent("""
        import os, sys
        from pathlib import Path
        from scripts.copies import copies
        from scripts.lib import build_folders
        replace = build_folders.os.replace
        def interrupt(source, destination, **options):
            if str(destination).endswith('.lease.json'):
                os._exit(19)
            return replace(source, destination, **options)
        build_folders.os.replace = interrupt
        with copies.standing(copies.caller_owner(), Path(sys.argv[1]), copies.CommittedSource('HEAD')):
            pass
        """)
        interrupted = self.python_run(program, str(self.source))
        self.assertEqual(interrupted.returncode, 19, interrupted.stderr)
        tree = self.gates / '1'
        self.assertFalse(tree.with_name(tree.name + '.lease.json').exists())
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as recovered:
            self.assertEqual(recovered, tree.with_name("2"))
            self.assertEqual(self.git(recovered, "rev-parse", "HEAD"), self.head)
            self.assertTrue(self.lease(recovered)["ready"])
        self.assertTrue(tree.is_dir())
        self.assertFalse(tree.with_name(tree.name + ".lease.json").exists())

    def test_interrupted_remove_finishes_even_after_record_is_deleted(self) -> None:
        with registered_workspace(self.owner_id, self.source, self.head, name='deleted-record', branch='deleted-record') as tree:
            pass
        def interrupt(path: Path) -> None:
            (path / copy_record.RECORD).unlink()
            raise OSError('interrupted directory removal')
        with mock.patch.object(scratch, 'remove', side_effect=interrupt):
            result = self.cli('remove', '--repo', str(self.source))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIsInstance(scratch.pending_removal(tree), copy_record.Registered)
        result = self.cli('remove', '--repo', str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(tree.exists())

    def test_relocated_source_keeps_the_old_copy_and_uses_a_new_copy(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            cache = tree / 'target/compiled'
            _ = cache.write_text('warm')
        relocated = self.base / 'relocated' / self.source.name
        relocated.parent.mkdir()
        _ = self.source.rename(relocated)
        _ = self.git(relocated, 'worktree', 'repair', str(tree))
        with copies.standing(self.owner_id, relocated, copies.CommittedSource(self.head)) as reused:
            self.assertEqual(reused, tree.with_name("2"))
            self.assertEqual(cache.read_text(), "warm")
            self.assertFalse((reused / "target/compiled").exists())
            self.assertEqual(self.git(reused, "rev-parse", "--path-format=absolute", "--git-common-dir"), str(relocated / ".git"))
            self.assertEqual(self.git(reused, 'remote', 'get-url', 'origin'), self.git(relocated, 'remote', 'get-url', 'origin'))

    def test_replacement_source_keeps_the_copy_with_missing_git_metadata(self) -> None:
        origin = self.git(self.source, 'remote', 'get-url', 'origin')
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            cache = tree / 'target/compiled'
            _ = cache.write_text('warm')
        shutil.rmtree(self.source)
        _ = self.git(self.base, 'clone', '-q', '--branch', 'main', origin, str(self.source))
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), self.published)
        with copies.standing(self.owner_id, self.source, copies.CommittedSource("HEAD")) as fresh:
            self.assertEqual(fresh, tree.with_name("2"))
            self.assertEqual(self.git(fresh, "rev-parse", "HEAD"), self.published)
            self.assertFalse((fresh / "target/compiled").exists())
        self.assertEqual(cache.read_text(), 'warm')
        self.assertTrue(tree.exists())
        self.assertEqual(build_folders.build_folder_state(tree), 'free')

    def test_caller_oserror_passes_through_without_conversion(self) -> None:
        failure = OSError("caller failed writing its own output")
        with self.assertRaises(OSError) as raised:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)):
                raise failure
        self.assertIs(raised.exception, failure)

    def test_post_use_failure_has_distinct_error_and_preserves_caller_failure(self) -> None:
        failure = ValueError('workspace verdict failed')
        tree = copies.workspace_path(self.owner_id, self.source, 'refresh')
        with mock.patch.object(copies, 'mark_target', side_effect=OSError('refresh failed')):
            with self.assertRaises(copies.PostUseError) as raised:
                with registered_workspace(self.owner_id, self.source, self.head, name='refresh', branch='refresh') as held:
                    self.assertEqual(held, tree)
                    raise failure
        self.assertIs(raised.exception.caller_error, failure)
        with scratch.busy_locks(tree, create=False) as idle:
            self.assertTrue(idle)
        self.assertEqual(build_folders.build_folder_state(tree), 'free')

        copy = self.gates / "1"
        for verdict in ("green", "red"):
            with self.subTest(verdict=verdict):
                with ExitStack() as patches, self.assertRaises(copies.PostUseError) as raised:
                    with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as held:
                        self.assertEqual(held, copy)
                        _ = patches.enter_context(mock.patch("scripts.lib.build_folders._write_lease",
                            side_effect=OSError("lease release failed")))
                        if verdict == "red":
                            raise failure
                self.assertIs(raised.exception.caller_error, failure if verdict == "red" else None)
                self.assertIn("lease release failed", str(raised.exception))
                self.assertTrue(copy.exists())
                self.assertEqual(build_folders.build_folder_state(copy), "free")

    def test_initial_record_names_requested_commit_before_git_initialization(self) -> None:
        real_git = shutil.which("git")
        assert real_git is not None
        binaries = self.base / "bin"
        binaries.mkdir()
        marker = self.base / "lease-before-checkout"
        fake = binaries / "git"
        _ = fake.write_text(dedent(f"""\
            #!/bin/sh
            if [ "$1" = -C ] && [ "$3" = checkout ]; then
                {shlex.quote(sys.executable)} - "$2" <<'PYTHON' || exit "$?"
            import json, os, pathlib, sys
            path = pathlib.Path(sys.argv[1])
            lease = json.loads(path.with_name(path.name + '.lease.json').read_text())
            assert lease['commit'] == os.environ['COPY_TEST_COMMIT']
            pathlib.Path(os.environ['COPY_TEST_MARKER']).write_text(lease['commit'])
            PYTHON
            fi
            exec {shlex.quote(real_git)} "$@"
            """))
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                "COPY_TEST_MARKER": str(marker), "COPY_TEST_COMMIT": self.head}):
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
                self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(marker.read_text(), self.head)
        git = copies.git
        partial = self.base / "partial-clone"
        partial.mkdir()

        def failed_clone(repo: Path, *arguments: str, data: bytes | None = None) -> bytes:
            if arguments[:1] == ("clone",):
                stage = Path(arguments[-1])
                self.assertEqual(stage.parent, partial)
                _ = (stage / "partial").write_text("unfinished clone")
                raise copies.CopyError("clone failed")
            return git(repo, *arguments, data=data)

        with mock.patch.object(copies, "git", side_effect=failed_clone), \
             self.assertRaisesRegex(copies.CopyError, "clone failed"):
            copies.initialize_git(partial, self.source)
        self.assertEqual(list(partial.iterdir()), [])

    def test_claim_state_distinguishes_owned_and_busy_checkout(self) -> None:
        tree = self.gates / "1"
        with copies.claim_tree(tree, create=True) as first:
            self.assertIs(first, copies.ClaimState.ACQUIRED)
            with copies.claim_tree(tree, create=False) as second:
                self.assertIs(second, copies.ClaimState.BUSY)
        with copies.claim_tree(tree, create=False) as released:
            self.assertIs(released, copies.ClaimState.ACQUIRED)

    def test_applied_untracked_files_have_current_mtime_and_preserve_mode(self) -> None:
        source = self.source / "untracked-build-input"
        _ = source.write_text("new source contents")
        source.chmod(0o755)
        os.utime(source, (1, 1))
        with copies.standing(self.owner_id, self.source, copies.WorktreeSource(self.head, self.source)) as tree:
            applied = tree / source.name
            self.assertEqual(applied.read_text(), "new source contents")
            self.assertGreater(applied.stat().st_mtime_ns, source.stat().st_mtime_ns)
            self.assertEqual(applied.stat().st_mode & 0o777, 0o755)

    def test_each_git_step_uses_runtime_timeout_and_next_use_resets(self) -> None:
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        assert real_git is not None
        wrappers = self.base / "bin"
        wrappers.mkdir()
        wrapper = wrappers / "git"
        fifo = self.base / "git-timeout"
        os.mkfifo(fifo)
        _ = wrapper.write_text(dedent(f"""\
            #!/bin/sh
            for arg do
                if [ "$arg" = "$GATE_TEST_BLOCK" ]; then
                    read -r line < {shlex.quote(str(fifo))}
                fi
            done
            exec {shlex.quote(real_git)} "$@"
            """))
        wrapper.chmod(0o755)
        for step in ("clone", "fetch", "checkout", "clean", "apply", "merge"):
            with self.subTest(step=step):
                self.write_config({**self.settings, "git_timeout_seconds": "0.1"})
                with mock.patch.dict(os.environ, {"PATH": str(wrappers) + os.pathsep + os.environ["PATH"],
                                                  "GATE_TEST_BLOCK": step}):
                    with self.assertRaisesRegex(copies.CopyError, f"git {step}.*timed out after 0.1"):
                        _ = copies.git(self.source, step)
        self.write_config(self.settings)
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)

    def test_copy_preparation_errors_name_the_git_command_and_stderr_before_use(self) -> None:
        real_git = shutil.which("git")
        assert real_git is not None
        wrappers = self.base / "bin"
        wrappers.mkdir()
        wrapper = wrappers / "git"
        _ = wrapper.write_text(dedent(f"""\
            #!/bin/sh
            for arg do
                if [ "$arg" = "$COPY_TEST_FAIL" ]; then
                    printf '%s\\n' 'fixture copy Git failure' >&2
                    exit 47
                fi
            done
            exec {shlex.quote(real_git)} "$@"
            """))
        wrapper.chmod(0o755)
        for step in ("add", "checkout", "clean"):
            with self.subTest(step=step):
                with mock.patch.dict(os.environ, {"PATH": str(wrappers) + os.pathsep + os.environ["PATH"],
                                                  "COPY_TEST_FAIL": step}):
                    with self.assertRaises(copies.CopyError) as refused:
                        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)):
                            self.fail("a copy preparation failure reached the caller")
                self.assertIn(step, str(refused.exception))
                self.assertIn("fixture copy Git failure", str(refused.exception))
                with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as recovered:
                    self.assertEqual(self.git(recovered, "rev-parse", "HEAD"), self.head)

    def test_merge_origin_ref_fetches_upstream_even_when_source_tracking_is_stale(self) -> None:
        incoming = self.incoming()
        _ = self.git(self.source, "push", "-q", "origin", "incoming")
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/incoming", self.published)
        before = self.git(self.source, "rev-parse", "origin/incoming")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head), merge=copies.MergeRevision("origin/incoming")) as tree:
            self.assertEqual(self.git(tree, "rev-parse", "HEAD^2"), incoming)
            self.assertEqual((tree / "incoming.txt").read_text(), "merged content\n")
        self.assertEqual(self.git(self.source, "rev-parse", "origin/incoming"), before)

    def test_merge_relative_origin_fetches_upstream_main_from_source_directory(self) -> None:
        incoming = self.incoming()
        _ = self.git(self.source, "push", "-q", "origin", "incoming:main")
        origin = self.git(self.source, "remote", "get-url", "origin")
        relative = os.path.relpath(origin, self.source)
        _ = self.git(self.source, "remote", "set-url", "origin", relative)
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/main", self.published)

        result = self.python_run(
            "import sys; from scripts.copies import copies; sys.exit(copies.main(sys.argv[1:]))",
            "run", "--repo", str(self.source), "--commit", "HEAD",
            "--merge", "origin/main", "--", "sh", "-c", "git rev-parse HEAD^2; cat incoming.txt")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [incoming, "merged content"])
        self.assertEqual(list((self.base / "temporary").iterdir()), [])
        self.assertEqual(self.git(self.source, "rev-parse", "origin/main"), self.published)
        self.assertEqual(self.git(self.source, "remote", "get-url", "origin"), relative)

    def test_command_keeps_busy_lock_after_wrapper_is_killed(self) -> None:
        command = ("import os,sys; output=open('/proc/'+str(os.getppid())+'/fd/1','w'); "
                   "print(os.getpid(), os.getcwd(), file=output, flush=True); sys.stdin.read(1)")
        wrapper = """import pathlib,sys
from scripts.copies import copies
from scripts.production import checkpoint_gate
with copies.standing(copies.caller_owner(), pathlib.Path(sys.argv[1]), copies.CommittedSource('HEAD')) as tree:
    checkpoint_gate.run_test_command([sys.executable, '-c', sys.argv[2]], tree)
"""
        arguments = [sys.executable, "-c", wrapper, str(self.source), command]
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIsNotNone(process.stdout)
            output = cast(TextIO, process.stdout)
            self.assertTrue(select.select([output], [], [], 10)[0], "command did not start")
            pid, checkout = output.readline().strip().split(" ", 1)
            child = int(pid)
            # The wrapper cannot remove its copy after a kill; the test folder removes it.
            self.assertTrue(Path(checkout).is_relative_to(self.gates), checkout)
            process.kill()
            _ = process.wait(timeout=10)
            os.kill(child, 0)
            first = self.gates / "1"
            self.assertEqual(build_folders.build_folder_state(first), "held",
                             "wrapper death released a surviving command's checkout")
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as second:
                self.assertEqual(second, first.with_name("2"))
        finally:
            if process.stdin is not None:
                _ = process.stdin.write("x")
                process.stdin.close()
                process.stdin = None
            if process.poll() is None:
                process.kill()
            _ = process.communicate(timeout=10)


if __name__ == "__main__":
    _ = unittest.main()

"""Real-Git acceptance tests for the standing gate checkout contract."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Generator, Mapping
from contextvars import copy_context
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from threading import Event
import unittest
from typing import ClassVar, TextIO, cast, override
from unittest import mock

from scripts.buildlog.store import host_name
from scripts.copies import copies
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue import admission_protocol as wire
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.copies.test_remote_copies import expected_remote_lease_columns
from scripts.accounts import accounts
from scripts.lib import build_folders, copy_record, scratch
from scripts.lib.build_slots_test_support import private_slot_config
from scripts.lint import folder_audit, tmp_sweep
from scripts.lint.memory_test_support import write_config_fixture
from textwrap import dedent
from scripts.mac_test import floor_claims
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.message import sessions
from scripts.production.cli_fixture import ImmutableGitSeed, invoke_cli
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, fixture_member, fixture_worker


class WaitNotice(io.StringIO):
    """Signal when the third caller has announced its blocking acquisition."""

    def __init__(self) -> None:
        super().__init__()
        self.noticed: Event = Event()

    @override
    def write(self, text: str) -> int:
        count = super().write(text)
        if "wait" in self.getvalue().lower():
            self.noticed.set()
        return count


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
        root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="copies-source-seed-"))).resolve()
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
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        temporary = self.base / "temporary"
        temporary.mkdir()
        _ = self.enterContext(mock.patch.object(tempfile, "tempdir", str(temporary)))
        slot_configuration = private_slot_config(self.base, repository="origin", maximum=2, default_maximum=2)
        home = self.base / "home"
        home.mkdir()
        self.configuration = self.base / "copies.conf"
        self.settings = {"root": str(self.base / "configured-gates"),
                         "remote_registry": str(self.base / "remote-mirrors"),
                         "wait_seconds": "0.01", "git_timeout_seconds": "10",
                         "guard_timeout_seconds": "10"}
        self.write_config(self.settings)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("GIT_") and key != "UNIT_RUN_DIR"}
        environment.update({
            **slot_configuration.environment,
            "HOME": str(home), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
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
        owner = fixture_member("unit", name="verify-unit", parent=parent.id)
        self.owner_id = str(owner.id)
        os.environ["CODEX_THREAD_ID"] = ""
        os.environ["CLAUDE_CODE_SESSION_ID"] = owner.current_session
        self.source = self.base / "checkout"
        self.source_seed.copy_to(self.source, self.base / "origin.git")
        _ = shutil.copyfile(self.source_seed.checkout.parent / "git-config", self.base / "git-config")
        self.gates = slot_configuration.slots

    @contextmanager
    def queue_service(self) -> Generator[AdmissionFixture]:
        original = dict(os.environ)
        with AdmissionFixture() as fixture:
            fixture.clock.epoch = time.time()
            with mock.patch.dict(os.environ, {**original, "ADMISSION_SOCKET": str(fixture.socket_path)}), fixture.serving():
                yield fixture

    def lease_machine(self) -> str:
        from scripts.buildlog.store import host_name

        return host_name()

    def write_config(self, settings: Mapping[str, str]) -> None:
        _ = self.configuration.write_text("[copies]\n" + "".join(f"{key}={value}\n" for key, value in settings.items()))

    def git(self, path: Path, *arguments: str) -> str:
        return subprocess.run(["git", "-C", str(path), *arguments], capture_output=True,
                              text=True, check=True, timeout=10).stdout.strip()

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return invoke_cli(copies, lambda: copies.main(list(arguments)), list(arguments),
                          dict(os.environ), self.base)

    def run_cli(self, *options: str, command: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        return self.cli("run", "--repo", str(self.source),
                        "--commit", "HEAD", *options, "--", *(command or (sys.executable, "-c", "pass")))

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
                    "import sys; from pathlib import Path; Path(sys.argv[1]).write_text(str(Path.cwd()))",
                    str(location),
                ))
                self.assertEqual(result.returncode, 0, result.stderr)
                tree = Path(location.read_text())
                self.assertTrue(tree.exists())
                tree = self.gates / "origin-slot1"
                self.assertEqual(self.lease(tree)["unit"], self.owner_id)
                removed = self.cli("remove")
                self.assertEqual(removed.returncode, 0, removed.stderr)
                self.assertTrue(tree.exists())

    def test_a_renamed_unit_reuses_and_removes_its_copy_without_changing_the_folder(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            _ = (tree / "target/artifact").write_text("warm")
        before = self.snapshot(tree)
        _ = staff.rename(staff.StaffId(self.owner_id), "renamed")
        listing = io.StringIO()
        with redirect_stdout(listing):
            copies.list_trees()
        self.assertIn("\trenamed\t", listing.getvalue())
        self.assertEqual(self.snapshot(tree), before)
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(reused, tree)
            self.assertEqual((reused / "target/artifact").read_text(), "warm")
        copies.remove_trees(self.owner_id)
        self.assertTrue(tree.exists())

    def test_same_named_units_have_separate_folders_and_removal(self) -> None:
        parent = fixture_member("showrunner", name="another-production")
        other = fixture_member("unit", name="verify-unit", parent=parent.id)
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as first:
            with copies.standing(str(other.id), self.source, copies.CommittedSource(self.head)) as second:
                self.assertNotEqual(first, second)
                self.assertEqual(first.name, "origin-slot1")
                self.assertEqual(second.name, "origin-slot2")
        copies.remove_trees(self.owner_id)
        self.assertTrue(first.exists())
        with copies.standing(str(other.id), self.source, copies.CommittedSource(self.head)) as reused:
            self.assertNotEqual(reused, second)
            self.assertEqual(reused, first)
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

    def test_expired_slot_wait_stops_the_cli_before_it_starts_the_command(self) -> None:
        _ = write_config_fixture(self.base / "config/build_folders.conf",
            f"# whole: 1..\nslots.{self.lease_machine()}.origin.max=1\n")
        marker = self.base / "not-started"
        with self.queue_service() as fixture:
            def expire_after_wait(reply: wire.Reply) -> wire.Reply:
                if isinstance(reply, wire.RepositorySlotWaiting):
                    fixture.clock.advance(60)
                return reply

            with mock.patch.object(fixture.server, "reply_filter", side_effect=expire_after_wait):
                with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)):
                    result = self.run_cli(command=(sys.executable, "-c",
                        "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(marker)))
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("wait ended for origin", result.stderr)
        self.assertIn("slots.", result.stderr)
        self.assertFalse(marker.exists())

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
            self.assertEqual(tree, self.gates / 'origin-slot1')
            self.assertEqual(self.git(tree, 'rev-parse', 'HEAD'), base)
            self.assertEqual(build_folders.build_folder_state(tree), 'held')
            self.assertFalse(Path(self.settings['root']).exists())
            self.assertEqual(list(scratch.registered_copy_paths()), [])
            self.assertEqual(self.git(self.source, 'branch', '--show-current'), 'main')
        self.assertTrue(tree.exists())
        self.assertEqual(build_folders.build_folder_state(tree), 'free')

    def test_a_staff_name_with_a_dot_gets_a_valid_fresh_folder(self) -> None:
        base = self.fresh_base()
        _ = staff.rename(staff.StaffId(self.owner_id), 'unit.test')
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(base)) as tree:
            self.assertEqual(tree.name, 'origin-slot1')
            self.assertEqual(self.git(tree, 'rev-parse', 'HEAD'), base)
            self.assertEqual(self.lease(tree)['unit'], self.owner_id)
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
        self.assertFalse(Path(self.settings["root"]).exists())

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
                self.assertEqual(first_output, self.base / 'cargo' / first.name)
                self.assertEqual(second_output, self.base / 'cargo' / second.name)
                for tree in (first, second):
                    self.assertFalse((tree / 'target').is_symlink())
                    self.assertEqual(self.git(tree, 'status', '--porcelain'), '')
                    self.assertEqual(build_folders.build_folder_state(tree), 'held')
            self.assertEqual(os.environ['CARGO_BUILD_BUILD_DIR'], str(first_output))
        self.assertEqual(os.environ.get('CARGO_BUILD_BUILD_DIR'), previous)
        self.assertEqual(build_folders.build_folder_state(first), 'free')
        self.assertEqual(build_folders.build_folder_state(second), 'free')
        for output in (str(self.base / "explicit-output"), "relative-output"):
            with self.subTest(output=output), mock.patch.dict(os.environ, {"CARGO_BUILD_BUILD_DIR": output}):
                with copies.standing(self.owner_id, self.source, copies.CommittedSource('HEAD')) as first:
                    self.assertEqual(os.environ['CARGO_BUILD_BUILD_DIR'], output)
                    with copies.standing(self.owner_id, self.source, copies.CommittedSource('HEAD')):
                        self.assertEqual(os.environ['CARGO_BUILD_BUILD_DIR'], output)
                    self.assertEqual(os.environ['CARGO_BUILD_BUILD_DIR'], output)

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
                tree = copies.workspace_path(self.owner_id, source, name)
                manifest = self.base / f"{name}.json"
                _ = manifest.write_text(json.dumps({"version": 2, "canonical": str(source), "base": self.head,
                    "crates": {"saved": {"checkout": str(tree), "stage": "abandoned"}}}))
                with copies.workspace(self.owner_id, source, self.head, name=name, branch="worker",
                                      task=f"gate:{name}", run_manifest=manifest):
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
                            copies.end_workspaces(f"gate:{name}", [tree])
                    self.assertTrue(tree.exists())
                    self.assertEqual(self.git(tree, "reflog", "show", "--format=%H", "refs/stash").splitlines(), stashes)
                copies.end_workspaces(f"gate:{name}", [tree])
                self.assertFalse(tree.exists())
                root = copy_record.copies_root()
                destination = (root.with_name(root.name + "-rescues.git") if source_gone
                               else copies.slot_source(source))
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
        tree = self.gates / 'origin-slot1'
        with self.assertRaises(ValueError) as raised:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(base)) as held:
                self.assertEqual(held, tree)
                _ = (held / 'target/proof').write_text('keep the output')
                raise failure
        self.assertIs(raised.exception, failure)
        self.assertEqual((tree / 'target/proof').read_text(), 'keep the output')
        self.assertEqual(build_folders.build_folder_state(tree), 'free')

    def test_clone_cli_creates_a_task_owned_checkout_without_a_root_busy_mark(self) -> None:
        result = self.cli("clone", str(self.source), "--task", "gate:camera", "--name", "camera")
        self.assertEqual(result.returncode, 0, result.stderr)
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
        self.assertEqual(scratch.end("gate:camera"), [checkout])
        self.assertFalse(checkout.exists())
        self.assertEqual((self.source / "tracked.txt").read_text(), "source HEAD\n")

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

    def test_copies_removal_clears_clone_trust_before_deleting_the_folder(self) -> None:
        paths = self.clone_trust_files()
        checkout = copies.clone(self.source, "gate:remove-trust")
        self.add_clone_project_settings(checkout, paths)
        copies.mark_mirror(checkout, "clone-owner", "hana", "fixture")
        remove = scratch.remove

        def delete(folder: Path) -> None:
            self.assertTrue(scratch.removal_mark(folder).exists())
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

    def test_temporary_sweep_clears_clone_trust(self) -> None:
        paths = self.clone_trust_files()
        checkout = copies.clone(self.source, "gate:sweep-trust")
        self.add_clone_project_settings(checkout, paths)
        entry = tmp_sweep.RemovableEntry(str(checkout), str(checkout.parent), time.time(), 1)
        self.assertTrue(tmp_sweep.remove_entry(entry, tmp_sweep.Sweep(), False))
        self.assertFalse(checkout.exists())
        self.assert_clone_project_removed(checkout, paths)

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
        self.assertEqual(scratch.end("gate:end-trust"), [checkout])
        self.assert_clone_project_removed(checkout, paths)

    def test_removal_retries_with_only_the_external_mark_and_changes_no_account_file(self) -> None:
        paths = self.clone_trust_files()
        checkout = copies.clone(self.source, "gate:retry-trust")
        self.add_clone_project_settings(checkout, paths)

        def stopped(folder: Path) -> None:
            (folder / copy_record.RECORD).unlink()
            raise OSError("stopped removal")

        with self.assertRaisesRegex(OSError, "stopped removal"):
            scratch.remove_marked(checkout, stopped)
        self.assertTrue(scratch.removal_mark(checkout).exists())
        self.assert_clone_project_removed(checkout, paths)
        before = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in paths}
        scratch.remove_marked(checkout, scratch.remove)
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
        self.assertEqual(scratch.end("gate:stop-before-trust"), [folder])
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_removal_without_a_recorded_trust_key_keeps_account_trust(self) -> None:
        paths = self.clone_trust_files(())
        checkout = copies.clone(self.source, "gate:no-trust-field")
        accounts.set_claude_folder_trust(checkout, trusted=True)
        before = {path: path.read_bytes() for path in paths}
        self.assertEqual(scratch.end("gate:no-trust-field"), [checkout])
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
        with copies.workspace(self.owner_id, source, commit, name="detached", branch="worker") as tree:
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

        self.assertEqual(scratch.end(records[0].task), [checkouts[0]])
        self.assertFalse(checkouts[0].exists())
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
        self.assertEqual(scratch.end("gate:symlink"), [checkout])
        self.assertFalse(checkout.exists())

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
        copies.apply_changes(checkout, self.source, self.head)
        self.assertEqual((checkout / "untracked.txt").read_text(), "new file\n")

    def test_borrower_order_resolves_stored_git_directory(self) -> None:
        alias = self.symlink_scratch_root()
        with copies.workspace(self.owner_id, self.source, self.head, name="a-parent", branch="parent") as parent:
            pass
        with copies.workspace(self.owner_id, parent, self.head, name="z-child", branch="child") as child:
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
            self.assertEqual(tree, self.gates / "origin-slot1")
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
                     stack.enter_context(copies.workspace(self.owner_id, self.source, self.head,
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
        with copies.workspace(self.owner_id, self.source, self.head, name="heal", branch="worker") as workspace:
            pass
        for tree in (workspace,):
            _ = self.git(tree, "remote", "set-url", "origin", str(self.source))
            _ = self.git(tree, "update-ref", "refs/remotes/origin/builds", self.head)
            _ = self.git(tree, "update-ref", "refs/remotes/origin/local-only", self.head)
            _ = self.git(tree, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/builds")
            _ = self.git(tree, "remote", "remove", "source")
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(copies.workspace(self.owner_id, self.source, self.head,
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
                     stack.enter_context(copies.workspace(self.owner_id, self.source, self.head,
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
        with copies.workspace(self.owner_id, self.source, self.head, name="no-origin", branch="no-origin") as tree:
            self.assertEqual(self.git(tree, "remote", "get-url", "origin"), str(self.source))
            self.assertEqual(self.git(tree, "rev-parse", "origin/builds"), self.head)
            self.assertEqual(self.git(tree, "remote", "get-url", "source"), str(self.source))
            self.assertEqual(self.git(tree, "rev-parse", "source/builds"), self.head)

    def test_warm_copies_refresh_moved_and_deleted_refs_without_a_new_head(self) -> None:
        _ = self.git(self.source, "branch", "retired", self.head)
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/retired", self.published)
        with copies.standing("verify-unit", self.source, copies.CommittedSource(self.head)) as standing:
            pass
        with copies.workspace("verify-unit", self.source, self.head, name="refs", branch="worker") as workspace:
            pass
        _ = self.git(self.source, "branch", "-D", "retired")
        _ = self.git(self.source, "update-ref", "-d", "refs/remotes/origin/retired")
        _ = self.git(self.source, "update-ref", "refs/remotes/origin/main", self.head)
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing("verify-unit", self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(copies.workspace("verify-unit", self.source, self.head,
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
        with copies.workspace("verify-unit", self.source, self.head, name="pruned", branch="worker") as workspace:
            pass
        _ = self.git(self.source, "branch", "-D", "incoming")
        _ = self.git(self.source, "reflog", "expire", "--expire=now", "--all")
        _ = self.git(self.source, "gc", "--prune=now")
        with ExitStack() as stack:
            trees = [stack.enter_context(copies.standing("verify-unit", self.source, copies.CommittedSource(self.head))),
                     stack.enter_context(copies.workspace("verify-unit", self.source, self.head,
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
                self.assertEqual(second, first.with_name('origin-slot2'))
                self.assertEqual(build_folders.build_folder_state(first), 'held')
                self.assertEqual(build_folders.build_folder_state(second), 'held')
            self.assertEqual(build_folders.build_folder_state(second), 'free')

    def test_third_caller_waits_for_first_without_creating_a_third_tree(self) -> None:
        notice = WaitNotice()
        def third_use() -> Path:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
                return tree
        with self.queue_service(), ThreadPoolExecutor(max_workers=1) as executor:
            holder_context = copy_context()
            with ExitStack() as primary:
                first = holder_context.run(primary.enter_context, copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)))
                with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)):
                    with redirect_stderr(notice):
                        third = executor.submit(third_use)
                        self.assertTrue(notice.noticed.wait(TEST_HANG_GUARD_SECS))
                        self.assertFalse(third.done())
                        self.assertIn('origin slot (2 of 2', notice.getvalue())
                    holder_context.run(primary.close)
                    self.assertEqual(third.result(timeout=TEST_HANG_GUARD_SECS), first)
        self.assertEqual(len([line for line in notice.getvalue().splitlines() if line.strip()]), 1)
        self.assertEqual(sorted(path.name for path in self.gates.iterdir() if path.is_dir()),
                         ['origin-slot1', 'origin-slot2'])

    def test_waiting_caller_uses_second_tree_when_it_is_released_first(self) -> None:
        notice = WaitNotice()
        def third_use() -> Path:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
                return tree
        with self.queue_service(), ThreadPoolExecutor(max_workers=1) as executor:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)):
                with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as second:
                    with redirect_stderr(notice):
                        third = executor.submit(third_use)
                        self.assertTrue(notice.noticed.wait(TEST_HANG_GUARD_SECS))
                        self.assertFalse(third.done())
                self.assertEqual(third.result(timeout=TEST_HANG_GUARD_SECS), second)

    def test_configured_root_contains_only_the_two_standing_checkouts(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as first:
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as second:
                self.assertEqual(first, self.gates / 'origin-slot1')
                self.assertEqual(second, self.gates / 'origin-slot2')
                self.assertEqual(sorted(path.name for path in self.gates.iterdir() if path.is_dir()),
                                 ['origin-slot1', 'origin-slot2'])
                for tree in (first, second):
                    self.assertTrue((tree / '.git').is_file())
                    self.assertTrue((tree / 'tracked.txt').is_file())
                    self.assertEqual(self.lease(tree)['unit'], self.owner_id)
        self.assertFalse(Path(self.settings['root']).exists())

    def test_missing_or_invalid_required_configuration_refuses_before_clone(self) -> None:
        variants = [
            {**self.settings, key: value}
            for key, value in (("remote_registry", ""), ("wait_seconds", "bad"), ("wait_seconds", "-1"),
                               ("git_timeout_seconds", "0"), ("git_timeout_seconds", "nan"),
                               ("guard_timeout_seconds", "inf"))
        ]
        variants.extend({key: value for key, value in self.settings.items() if key != missing}
                        for missing in self.settings if missing != "root")
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
                self.assertTrue((self.gates / "origin-slot1").is_dir())

    def test_linked_source_uses_canonical_repository_folder_name(self) -> None:
        linked = self.base / "linked-worktree"
        _ = self.git(self.source, "worktree", "add", "--detach", str(linked), self.published)
        with copies.standing(self.owner_id, linked, copies.CommittedSource("HEAD")) as tree:
            self.assertEqual(tree, self.gates / "origin-slot1")
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
                self.assertEqual(tree.name, "origin-slot1")
                self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)

    def test_separate_git_directory_uses_the_main_checkout_without_origin(self) -> None:
        main = self.base / "widget"
        metadata = self.base / "state" / "widget-index"
        metadata.parent.mkdir()
        _ = self.git(self.base, "clone", "-q", "--separate-git-dir", str(metadata), str(self.source), str(main))
        _ = self.git(main, "remote", "remove", "origin")
        self.assertEqual(copies.repository_name(main), "widget")
        # Linked worktrees need the main path recorded when Git metadata lives elsewhere.
        _ = self.git(main, "config", "core.worktree", str(main))
        linked = self.base / "separate-worktree"
        _ = self.git(main, "worktree", "add", "--detach", str(linked), "HEAD")
        for source in (main, linked):
            with self.subTest(source=source), copies.standing(self.owner_id, source, copies.CommittedSource("HEAD")) as tree:
                self.assertEqual(copies.main_checkout(source), main)
                self.assertEqual(tree.name, "widget-slot1")
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
        with copies.workspace(self.owner_id, self.source, "HEAD", name="worker", branch="worker") as tree:
            pass
        _ = self.git(tree, "repack", "-a", "-d")
        (tree / ".git/objects/info/alternates").unlink()
        shutil.rmtree(self.source)
        result = self.cli("remove", "--repo", str(selector))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(tree.exists())

    def test_remove_reports_a_filesystem_failure_as_a_copy_error(self) -> None:
        with copies.workspace(self.owner_id, self.source, 'HEAD', name='removal', branch='removal') as tree:
            pass
        with mock.patch.object(scratch, 'remove', side_effect=OSError('removal interrupted')):
            with self.assertRaisesRegex(copies.CopyError, 'removal interrupted'):
                copies.remove_trees(self.owner_id)
        self.assertTrue(tree.exists())
        self.assertIsInstance(scratch.pending_removal(tree), copy_record.Registered)
        copies.remove_trees(self.owner_id)
        self.assertFalse(tree.exists())
        self.assertIsInstance(scratch.pending_removal(tree), copy_record.NotRegistered)

    def test_workspace_records_ownership_and_preserves_edits_on_resume(self) -> None:
        name = "crate-run-package"
        branch = "style-crate/run/package"
        with copies.workspace(self.owner_id, self.source, self.head, name=name, branch=branch) as tree:
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
                with copies.workspace(self.owner_id, self.source, self.head, name=name, branch=branch):
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
                        with copies.workspace(self.owner_id, self.source, self.head, name=boundary, branch="worker"):
                            self.fail("initialization reached the caller")
                record = scratch.task_of(tree)
                assert isinstance(record, copy_record.Registered)
                self.assertEqual(record.fields["copy_commit"], "")
                self.assertEqual(copies.workspace_repository(record), copies.repository_name(self.source))
                with copies.workspace(self.owner_id, self.source, self.head, name=boundary, branch="worker") as resumed:
                    self.assertEqual(self.git(resumed, "rev-parse", "HEAD"), self.head)
                    _ = (resumed / "tracked.txt").write_text("finished workspace edit\n")
                with copies.workspace(self.owner_id, self.source, self.head, name=boundary, branch="worker") as resumed:
                    self.assertEqual((resumed / "tracked.txt").read_text(), "finished workspace edit\n")

    def test_legacy_workspace_keeps_commits_index_and_journal_and_ends_through_holds(self) -> None:
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
        with copies.workspace(self.owner_id, self.source, self.head, name="legacy", branch="worker",
                              task="gate:legacy-run", path=tree) as resumed:
            self.assertEqual(resumed, tree)
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), tip)
            self.assertEqual(self.git(tree, "diff", "--cached", "--name-only"), "tracked.txt")
            self.assertEqual(journal.read_text(), "journal\n")
            self.assertEqual((tree / "pending.txt").read_text(), "pending\n")
        _ = self.git(self.source, "fetch", "-q", str(tree), "worker:refs/heads/legacy")
        _ = self.git(tree, "reset", "--hard", self.head)
        (tree / "pending.txt").unlink()
        with copies.workspace(self.owner_id, self.source, self.head, name="legacy", branch="worker",
                              task="gate:legacy-run", path=tree):
            pass
        copies.end_workspaces("gate:legacy-run", [tree])
        self.assertNotIn(str(tree), scratch.holds())
        self.assertFalse(tree.exists())

    def test_legacy_workspace_refuses_matching_relative_urls_for_different_repositories(self) -> None:
        tree = self.base / "old-run/worker"
        tree.mkdir(parents=True)
        copies.initialize_git(tree, self.source)
        _ = self.git(tree, "checkout", "-q", "-B", "worker", self.head)
        _ = self.git(self.base, "clone", "-q", "--bare", str(self.source), str(tree.parent / "origin.git"))
        for repo in (self.source, tree):
            _ = self.git(repo, "remote", "set-url", "origin", "../origin.git")

        with self.assertRaisesRegex(copies.CopyError, "legacy workspace source differs"):
            with copies.workspace(self.owner_id, self.source, self.head, name="legacy", branch="worker", path=tree):
                self.fail("the unrelated workspace reached the caller")

        self.assertIsInstance(scratch.task_of(tree), copy_record.NotRegistered)
        self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self.git(tree, "remote", "get-url", "origin"), "../origin.git")

    def test_workspace_uses_the_existing_copy_sweep(self) -> None:
        with copies.workspace(self.owner_id, self.source, self.published,
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
            with copies.workspace(self.owner_id, self.source, self.published,
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
        with copies.workspace(self.owner_id, self.source, self.head, name="reserved", branch="worker") as tree:
            _ = (tree / "tracked.txt").write_text("keep these edits\n")
        for revision, branch in ((self.published, "worker"), (self.head, "other")):
            with self.subTest(revision=revision, branch=branch):
                with self.assertRaises(copies.CopyError):
                    with copies.workspace(self.owner_id, self.source, revision, name="reserved", branch=branch):
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
            with copies.workspace(self.owner_id, self.source, self.head, name="package", branch="worker") as worker:
                with copies.standing(self.owner_id, worker, copies.CommittedSource(self.head)) as proof:
                    self.assertEqual(proof, frozen.with_name("origin-slot2"))

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
                copies.apply_changes(tree, self.source, self.head)
            self.assertFalse((tree / "nested").exists())

    def test_apply_changes_error_names_untracked_files(self) -> None:
        _ = (self.source / "untracked.txt").write_text("new source\n")
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            with mock.patch.object(copies, "staged_tree", side_effect=copies.PreparationError("fixture failure")):
                with self.assertRaisesRegex(copies.PreparationError, "files: untracked.txt; fixture failure"):
                    copies.apply_changes(tree, self.source, self.head)

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
            self.assertEqual(tree, self.gates / "origin-slot1")
            self.assertEqual((tree / "tracked.txt").read_text(), "source HEAD\n")
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")
            self.assertFalse((tree / ".git/MERGE_HEAD").exists())

    def test_failed_apply_exits_three_releases_lock_and_resets(self) -> None:
        _ = (self.source / "tracked.txt").write_text("patch based on source HEAD\n")
        result = self.run_cli("--commit", self.published, "--apply-uncommitted", str(self.source))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("tracked.txt", result.stdout + result.stderr)
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            self.assertEqual(tree, self.gates / "origin-slot1")
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)
            self.assertEqual(self.git(tree, "status", "--porcelain"), "")

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
        )
        for label, requested, command_exit, apply in cases:
            with self.subTest(case=label):
                options = [part for path in requested for part in ("--publish", path)]
                if apply:
                    _ = (self.source / "untracked.txt").write_text("gate publication\n")
                    options.extend(("--apply-uncommitted", str(self.source)))
                folders_before = {folder.path for folder in scratch.folders()}
                result = self.run_cli(*options, command=(
                    sys.executable, "-c", program, str(output), "checkout={tree}/tracked.txt",
                    str(evidence), str(command_exit),
                ))
                self.assertEqual(result.returncode, command_exit, result.stderr)
                row = cast(list[str], json.loads(output.read_text()))
                checkout = Path(row[0])
                self.assertEqual(row, [str(checkout), f"checkout={checkout}/tracked.txt"])
                self.assertTrue(checkout.is_relative_to(self.gates))
                self.assertTrue(checkout.exists())
                tree = self.gates / "origin-slot1"
                with scratch.busy_locks(tree, create=False) as idle:
                    self.assertTrue(idle)
                commit, target = cast(list[str], json.loads(evidence.read_text()))
                self.assertEqual(Path(target).resolve(), tree / "target")
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
                expected = [(path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                            for relative in built for path in (tree / relative,)]
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
                    for value in ('verify-unit', 'origin', self.head[:7], 'busy', str(os.getpid())):
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
                                     ('verify-unit', self.source.name, 'remote-host'))
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
                    self.assertEqual(tree.name, 'origin-slot1')
                    self.assertEqual(self.snapshot(tree), before)
                    self.assertIn('renamed' if staff_read == 'readable' else 'unavailable', result.stdout)

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
        with copies.workspace(self.owner_id, self.source, self.head, name="live", branch="worker") as tree:
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
        with copies.workspace(self.owner_id, gate, self.head, name='borrower', branch='worker') as tree:
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
        with copies.workspace(self.owner_id, gate, self.head, name="borrower", branch="worker") as tree:
            pass
        copies.remove_trees(self.owner_id)
        self.assertFalse(tree.exists())
        self.assertTrue(gate.exists())

    def test_owner_removal_preserves_a_gate_borrowed_by_another_owners_workspace(self) -> None:
        gate = self.idle_gate_source()
        with copies.workspace('other-unit', gate, self.head, name='borrower', branch='worker') as tree:
            pass
        copies.remove_trees(self.owner_id)
        self.assertTrue(gate.exists())
        self.assertEqual(self.git(tree, 'rev-parse', 'HEAD'), self.head)
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        self.assertNotIn('ended_task', record.fields)

    def test_owner_removal_reads_relative_alternates_despite_a_damaged_workspace_record(self) -> None:
        gate = self.idle_gate_source()
        with copies.workspace(self.owner_id, gate, self.head, name='borrower', branch='worker') as tree:
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
        with copies.workspace('other-unit', gate, self.head, name='borrower', branch='worker') as tree:
            pass
        legacy = Path(self.settings['root']) / 'old-gate.removing'
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
        with copies.workspace(self.owner_id, self.source, self.head, name="live", branch="worker") as tree:
            result = self.cli("remove")
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(str(tree), result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertTrue(gate.exists())
            self.assertEqual(self.git(tree, "rev-parse", "HEAD"), self.head)

    def test_owner_removal_reports_each_workspace_with_unsaved_work_on_a_separate_line(self) -> None:
        trees: list[Path] = []
        for name in ("first", "second"):
            with copies.workspace(self.owner_id, self.source, self.head, name=name, branch="worker") as tree:
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
        with copies.workspace(self.owner_id, self.source, self.head,
                              name="worker", branch="worker") as worker:
            pass
        legacy = self.base / "legacy-run/worker"
        legacy.mkdir(parents=True)
        copies.initialize_git(legacy, self.source)
        _ = self.git(legacy, "checkout", "-q", "-B", "legacy", self.head)
        with copies.workspace(self.owner_id, self.source, self.head, name="legacy", branch="legacy",
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
        with copies.workspace(self.owner_id, self.source, self.head, name='removal-retry', branch='removal-retry') as tree:
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
        with copies.workspace(self.owner_id, self.source, self.head,
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
        with copies.workspace(self.owner_id, self.source, self.head,
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
        with copies.workspace(self.owner_id, other, "HEAD", name="other-worker", branch="worker") as kept:
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
        with copies.workspace(self.owner_id, self.source, self.head, name="crate-notes", branch="notes") as tree:
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
                with copies.workspace(self.owner_id, self.source, self.head, name=name,
                                      branch="worker", run_manifest=manifest_path):
                    pass
                with copies.workspace(self.owner_id, self.source, self.head, name=name + "-active",
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

    def test_owner_removal_preserves_a_source_with_an_adopted_borrower(self) -> None:
        with copies.workspace(self.owner_id, self.source, self.head, name="parent", branch="worker") as parent:
            pass
        child = self.base / "adopted/borrower"
        child.mkdir(parents=True)
        copies.initialize_git(child, parent)
        _ = self.git(child, "checkout", "-q", "-B", "borrower", self.head)
        with copies.workspace("other-unit", parent, self.head, name="borrower", branch="borrower", path=child):
            pass
        with self.assertRaisesRegex(copies.CopyError, "borrowed") as refusal:
            copies.remove_trees(self.owner_id)
        self.assertIn(str(child), str(refusal.exception))
        self.assertTrue(parent.exists())
        self.assertEqual(self.git(child, "cat-file", "-p", self.head), self.git(parent, "cat-file", "-p", self.head))
        for pending in (child, parent):
            with self.subTest(pending=pending):
                with self.assertRaisesRegex(RuntimeError, "interrupted removal"):
                    scratch.remove_marked(pending, mock.Mock(side_effect=RuntimeError("interrupted removal")))
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
                with copies.workspace(self.owner_id, self.source, self.head, name=owner, branch="worker") as tree:
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
        with copies.workspace(self.owner_id, self.source, self.head, name="adopted", branch="worker", path=tree):
            pass
        record = scratch.task_of(tree)
        assert isinstance(record, copy_record.Registered)
        scratch.hand_over(tree, "Next_unit", record.task)
        _ = scratch.hold(tree, record.task, "a different hold reason")
        copies.remove_trees("Next_unit")
        self.assertFalse(tree.exists())
        self.assertNotIn(str(tree), scratch.holds())

    def test_owner_transfer_after_selection_preserves_the_new_owners_copy(self) -> None:
        with copies.workspace(self.owner_id, self.source, self.head, name="transfer", branch="worker") as tree:
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
        with copies.workspace(self.owner_id, self.source, self.head, name="a-parent", branch="worker") as parent:
            pass
        with copies.workspace(self.owner_id, parent, self.head, name="z-child", branch="worker") as child:
            pass
        copies.remove_trees(self.owner_id)
        self.assertFalse(child.exists())
        self.assertFalse(parent.exists())

    def test_owner_removal_preserves_an_external_borrower_and_does_not_end_a_busy_copy(self) -> None:
        with copies.workspace(self.owner_id, self.source, self.head, name="a-source", branch="worker") as parent:
            with self.assertRaisesRegex(copies.CopyError, "busy"):
                copies.remove_trees(self.owner_id)
            record = scratch.task_of(parent)
            assert isinstance(record, copy_record.Registered)
            self.assertNotIn("ended_task", record.fields)
        with copies.workspace("other-unit", parent, self.head, name="borrower", branch="worker") as child:
            pass
        with self.assertRaisesRegex(copies.CopyError, "borrowed"):
            copies.remove_trees(self.owner_id)
        self.assertTrue(parent.exists())
        self.assertTrue(child.exists())

    def test_owner_removal_refuses_a_damaged_record_until_repaired(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as gate:
            pass
        with copies.workspace(self.owner_id, self.source, self.head, name="damaged", branch="worker") as tree:
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

    def test_underscore_owner_succeeds_and_creates_the_checkout(self) -> None:
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": fixture_member("unit", name="unit_name",
                parent=next(row.id for row in staff.local_snapshot().rows if row.kind == "showrunner")).current_session,
                                          "CODEX_THREAD_ID": ""}):
            result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.gates / "origin-slot1").exists())

    def test_record_is_local_metadata_and_repeated_use_preserves_tree(self) -> None:
        with copies.standing(self.owner_id, str(self.source), copies.CommittedSource(self.head)) as tree:
            stable = (tree / 'stable.txt').stat()
            record = self.lease(tree)
            self.assertEqual(record['unit'], self.owner_id)
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

    def test_interrupted_first_record_publication_recovers_on_next_use(self) -> None:
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
        tree = self.gates / 'origin-slot1'
        self.assertFalse(tree.with_name(tree.name + '.lease.json').exists())
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as recovered:
            self.assertEqual(recovered, tree)
            self.assertEqual(self.git(recovered, 'rev-parse', 'HEAD'), self.head)
            self.assertTrue(self.lease(recovered)['ready'])
        self.assertEqual(build_folders.build_folder_state(tree), 'free')

    def test_interrupted_remove_finishes_even_after_record_is_deleted(self) -> None:
        with copies.workspace(self.owner_id, self.source, self.head, name='deleted-record', branch='deleted-record') as tree:
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

    def test_relocated_source_rebuilds_git_metadata_and_keeps_target(self) -> None:
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            cache = tree / 'target/compiled'
            _ = cache.write_text('warm')
        relocated = self.base / 'relocated' / self.source.name
        relocated.parent.mkdir()
        _ = self.source.rename(relocated)
        _ = self.git(relocated, 'worktree', 'repair', str(tree))
        with copies.standing(self.owner_id, relocated, copies.CommittedSource(self.head)) as reused:
            self.assertEqual(reused, tree)
            self.assertEqual(cache.read_text(), 'warm')
            self.assertEqual(self.git(reused, 'rev-parse', '--path-format=absolute', '--git-common-dir'), str(relocated / '.git'))
            self.assertEqual(self.git(reused, 'remote', 'get-url', 'origin'), self.git(relocated, 'remote', 'get-url', 'origin'))

    def test_deleted_borrowed_objects_are_recovered_from_replacement_source(self) -> None:
        origin = self.git(self.source, 'remote', 'get-url', 'origin')
        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as tree:
            cache = tree / 'target/compiled'
            _ = cache.write_text('warm')
        shutil.rmtree(self.source)
        _ = self.git(self.base, 'clone', '-q', '--branch', 'main', origin, str(self.source))
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), self.published)
        with self.assertRaises(ValueError):
            with copies.standing(self.owner_id, self.source, copies.CommittedSource('HEAD')):
                self.fail('a slot with missing Git metadata must not reset its source')
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
                with copies.workspace(self.owner_id, self.source, self.head, name='refresh', branch='refresh') as held:
                    self.assertEqual(held, tree)
                    raise failure
        self.assertIs(raised.exception.caller_error, failure)
        with scratch.busy_locks(tree, create=False) as idle:
            self.assertTrue(idle)
        self.assertEqual(build_folders.build_folder_state(tree), 'free')

        slot = self.gates / "origin-slot1"
        for verdict in ("green", "red"):
            with self.subTest(verdict=verdict):
                with ExitStack() as patches, self.assertRaises(copies.PostUseError) as raised:
                    with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as held:
                        self.assertEqual(held, slot)
                        _ = patches.enter_context(mock.patch("scripts.lib.build_folders._write_lease",
                            side_effect=OSError("lease release failed")))
                        if verdict == "red":
                            raise failure
                self.assertIs(raised.exception.caller_error, failure if verdict == "red" else None)
                self.assertIn("lease release failed", str(raised.exception))
                self.assertTrue(slot.exists())
                self.assertEqual(build_folders.build_folder_state(slot), "free")

    def test_initial_record_names_requested_commit_before_git_initialization(self) -> None:
        real_git = shutil.which("git")
        assert real_git is not None
        binaries = self.base / "bin"
        binaries.mkdir()
        marker = self.base / "lease-before-checkout"
        fake = binaries / "git"
        _ = fake.write_text(f"#!{sys.executable}\n" + """import json, os, pathlib, sys
if sys.argv[1] == '-C' and sys.argv[3] == 'checkout':
    path = pathlib.Path(sys.argv[2])
    lease = json.loads(path.with_name(path.name + '.lease.json').read_text())
    assert lease['commit'] == os.environ['COPY_TEST_COMMIT']
    pathlib.Path(os.environ['COPY_TEST_MARKER']).write_text(lease['commit'])
os.execv(os.environ['COPY_TEST_GIT'], [os.environ['COPY_TEST_GIT'], *sys.argv[1:]])
""")
        fake.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                "COPY_TEST_GIT": real_git, "COPY_TEST_MARKER": str(marker), "COPY_TEST_COMMIT": self.head}):
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
        tree = self.gates / "origin-slot1"
        with copies.claim_tree(tree, create=True) as first:
            self.assertIs(first, copies.ClaimState.ACQUIRED)
            with copies.claim_tree(tree, create=False) as second:
                self.assertIs(second, copies.ClaimState.BUSY)
        with copies.claim_tree(tree, create=False) as released:
            self.assertIs(released, copies.ClaimState.ACQUIRED)

    def test_waiter_wakes_on_first_release_without_fast_polling(self) -> None:
        _ = self.fresh_base()
        notice = WaitNotice()
        def waiting_use(source: Path, revision: str) -> Path:
            with copies.standing(self.owner_id, source, copies.CommittedSource(revision)) as tree:
                return tree
        settings = self.base / 'config/build_folders.conf'
        _ = write_config_fixture(settings, f"# whole: 1..\nslots.{self.lease_machine()}.origin.max=1\n")
        new_revision = self.incoming()
        with self.queue_service(), ThreadPoolExecutor(max_workers=1) as executor:
            holder_context = copy_context()
            with ExitStack() as primary:
                first = holder_context.run(primary.enter_context, copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)))
                with ExitStack() as proof_scope:
                    identical = holder_context.run(proof_scope.enter_context,
                        copies.standing(self.owner_id, first, copies.CommittedSource(self.head)))
                    self.assertEqual(identical, first)
                    holder_context.run(proof_scope.close)
                with redirect_stderr(notice):
                    future = executor.submit(waiting_use, first, new_revision)
                    self.assertTrue(notice.noticed.wait(TEST_HANG_GUARD_SECS))
                    self.assertFalse(future.done())
                self.assertEqual(self.git(first, 'rev-parse', 'HEAD'), self.head)
                holder_context.run(primary.close)
                self.assertEqual(future.result(timeout=TEST_HANG_GUARD_SECS), first)
                self.assertEqual(self.git(first, 'rev-parse', 'HEAD'), new_revision)

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
        wrappers = self.base / "bin"
        wrappers.mkdir()
        wrapper = wrappers / "git"
        _ = wrapper.write_text(f"#!{sys.executable}\n"
                               + "import os,sys,time\n"
                               + "if os.environ['GATE_TEST_BLOCK'] in sys.argv[1:]: time.sleep(10)\n"
                               + f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n")
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

    def test_slot_preparation_errors_name_the_git_command_and_stderr_before_use(self) -> None:
        real_git = shutil.which("git")
        assert real_git is not None
        wrappers = self.base / "bin"
        wrappers.mkdir()
        wrapper = wrappers / "git"
        _ = wrapper.write_text(f"#!{sys.executable}\n"
            + "import os, sys\n"
            + "if os.environ['COPY_TEST_FAIL'] in sys.argv[1:]:\n"
            + " sys.stderr.write('fixture slot Git failure\\n')\n"
            + " sys.exit(47)\n"
            + f"os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o755)
        for step in ("add", "checkout", "clean"):
            with self.subTest(step=step):
                with mock.patch.dict(os.environ, {"PATH": str(wrappers) + os.pathsep + os.environ["PATH"],
                                                  "COPY_TEST_FAIL": step}):
                    with self.assertRaises(copies.CopyError) as refused:
                        with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)):
                            self.fail("a slot preparation failure reached the caller")
                self.assertIn(step, str(refused.exception))
                self.assertIn("fixture slot Git failure", str(refused.exception))
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
        for launcher in ("cli", "checkpoint"):
            with self.subTest(launcher=launcher):
                self.check_surviving_command_lock(command, wrapper, launcher)

    def check_surviving_command_lock(self, command: str, wrapper: str, launcher: str) -> None:
        arguments = ([sys.executable, str(Path(copies.__file__)), "run", "--repo", str(self.source),
                      "--commit", "HEAD", "--", sys.executable, "-c", command] if launcher == "cli"
                     else [sys.executable, "-c", wrapper, str(self.source), command])
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
            first = self.gates / "origin-slot1"
            self.assertEqual(build_folders.build_folder_state(first), "held",
                             "wrapper death released a surviving command's checkout")
            with copies.standing(self.owner_id, self.source, copies.CommittedSource(self.head)) as second:
                self.assertEqual(second, first.with_name("origin-slot2"))
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

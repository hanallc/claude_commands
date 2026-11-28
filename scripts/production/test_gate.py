"""Gate checkpoint contracts."""

from __future__ import annotations

from scripts.build_queue import admission_client, admission_records, admission_wire, execution_identity, first_step, placement, queue_place
from scripts.build_queue.admission_jobs import StepJob
from scripts.build_targets.gate_targets import caller_gate_targets

from scripts.agents import remote
from scripts.build_hold import build_hold
from scripts.herdr.missing_binary import MISSING_HERDR_BIN

import unittest
import fcntl

import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
from textwrap import dedent
import time
from collections.abc import Generator, Sequence
from typing import cast, final, override
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from unittest import mock
from scripts.build_targets import mac_linux
from scripts.buildlog import treekey
from scripts.buildlog.store import host_name
from scripts.direct.wait_for_test_state import wait_until
from scripts.copies import copies
from scripts.lib import build_folders, conf, scratch, testing_folders
from scripts.lib.build_copies import CopyQueuePosition
from scripts.lib.build_steps import (
    GATE_PRELAUNCH_REFUSED_STATUS, GATE_UNSETTLED_LAUNCH_STATUS, MAC_LINUX_PRELAUNCH_REFUSED_STATUS, gate_job_for_command,
)
from scripts.lib.git_test_support import fixture_git_environment
from scripts.lib.process_start import Gone
from scripts.lib.socket_test_support import short_socket_address_examples
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import change_review, gate, promotion, staff
from scripts.production.gate_modules import GateRefused, discover_test_modules
from scripts.production.test_change_review import ProducerFixture
from scripts.production.staff_test_support import (
    build_fixture_sessions, fixture_member, fixture_unit_location, isolated_source_readers, isolated_staff_file, source_environment,
)
from scripts.production.gate_test_support import FIXTURE_FOLDERS, MergeGateFixture, SUITE_MODULE, gate_suites
from scripts.production.test_gate_default_placement import placed


class GateFolderGuardTests(unittest.TestCase):
    def test_gate_test_folders_are_outside_git_working_trees(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        build = root / "build"
        guest_build = root / "guest/build"
        scratch = root / "scratch"
        scratch.mkdir()
        copy = build / "claude/verify/1"
        worktree = root / "worktree"
        worktree_output = build / "claude/verify/deadbeef"
        main = root / "main"
        main_output = build / "claude/main"
        unmodeled = root / "checkout"
        guest = guest_build / "claude/verify/2"
        for checkout in (copy, worktree, main, unmodeled, guest):
            checkout.mkdir(parents=True)
            _ = subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        for checkout, output in ((worktree, worktree_output), (main, main_output)):
            output.mkdir(parents=True)
            (checkout / "target").symlink_to(output, target_is_directory=True)
        guest_home = guest / "target/mac-linux/home"
        guest_home.mkdir(parents=True)
        cases = (
            (copy, build, root, scratch, build / "test-runs/claude/verify/1"),
            (worktree, build, root, scratch, build / "test-runs/claude/verify/deadbeef"),
            (main, build, root, scratch, build / "test-runs/claude/main"),
            (unmodeled, build, root, scratch, scratch / "gate-test-runs"),
            (guest, guest_build, guest_home, guest_home.parent, guest_build / "test-runs/claude/verify/2"),
        )
        for checkout, build_root, home, temporary, expected in cases:
            with (self.subTest(checkout=checkout), build_folders.use_build_root(build_root),
                  mock.patch.dict(os.environ, {"HOME": str(home), "TMPDIR": str(temporary),
                                               build_folders.TEST_RUN_FOLDER_ENV: ""})):
                parent = gate.gate_suite_parent(checkout, gate.CHECKOUT_TEST_RUNS)
                self.assertEqual(parent, expected)
                request = build_folders.GateSuiteFolder("g-", build_folders.ExplicitBuildParent(parent))
                with build_folders.temporary_build_folder(request) as folder:
                    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=folder,
                                            capture_output=True, text=True, check=False)
                    self.assertEqual(inside.returncode, 128, inside.stdout + inside.stderr)
                    self.assertIn("not a git repository", inside.stderr)
        with (build_folders.use_build_root(build),
              mock.patch.dict(os.environ, {"TMPDIR": str(unmodeled), build_folders.TEST_RUN_FOLDER_ENV: ""})):
            with self.assertRaisesRegex(GateRefused, "inside a Git checkout"):
                _ = gate.gate_suite_parent(unmodeled, gate.CHECKOUT_TEST_RUNS)
        self.assertFalse((unmodeled / "gate-test-runs").exists())

    def test_direct_cleanup_preserves_an_inherited_gate_folder_and_record(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        build = root / "build"
        cases = (
            (root / "unmodeled", root / "gate-test-runs", root / "test-runs"),
            (build / "claude/verify/1", build / "test-runs/claude/verify/1",
             build / "claude/verify/1/target/test-runs"),
        )
        for checkout, gate_parent, direct_parent in cases:
            with (self.subTest(checkout=checkout), build_folders.use_build_root(build),
                  mock.patch.dict(os.environ, {"TMPDIR": str(root), testing_folders.RUN_FOLDER_ENV: ""})):
                checkout.mkdir(parents=True)
                parent = gate.gate_suite_parent(checkout, gate.CHECKOUT_TEST_RUNS)
                request = build_folders.GateSuiteFolder("g-", build_folders.ExplicitBuildParent(parent))
                with (gate.private_gate_recovery(checkout, parent) as recovery,
                      build_folders.temporary_build_folder(request, keep_on_exit=lambda: True) as retained):
                    recovery.folders.append(str(retained))
                record = recovery.path.read_bytes()
                self.assertEqual(build_folders.build_folder_state(retained), "free")

                direct_parent.mkdir(parents=True, exist_ok=True)
                stale_request = build_folders.UniqueBuildFolder("r", build_folders.ExplicitBuildParent(direct_parent))
                with build_folders.temporary_build_folder(stale_request, keep_on_exit=lambda: True) as stale:
                    pass

                with testing_folders.test_run_folder(parent) as outer:
                    self.assertEqual(os.environ["TMPDIR"], str(outer))
                    self.assertEqual(os.environ[testing_folders.RUN_FOLDER_ENV], str(outer))
                    direct = testing_folders.prepare_test_runs_parent(checkout)
                    self.assertTrue(retained.is_dir())
                    self.assertEqual(recovery.path.read_bytes(), record)
                    self.assertFalse(stale.exists())
                    self.assertEqual(direct, direct_parent)
                    self.assertEqual(parent, gate_parent)
                    self.assertEqual(gate.gate_suite_parent(checkout, gate.CHECKOUT_TEST_RUNS), gate_parent)
                with testing_folders.test_run_folder(direct):
                    self.assertEqual(gate.gate_suite_parent(checkout, gate.CHECKOUT_TEST_RUNS), gate_parent)

    def test_gate_module_folders_leave_room_for_unix_sockets(self) -> None:
        config = Path(__file__).resolve().parents[2] / "config"
        guest_copy = Path("/home/guest/build/claude/01234567-89ab-4def-8123-456789abcdef/1")
        guest_home = guest_copy / "target/mac-linux/home"
        cases = (
            (guest_home, Path("/home/guest/build/test-runs/claude/01234567-89ab-4def-8123-456789abcdef/1")),
            (Path("/home/gate-trial"), Path("/tmp/gate-test-runs")),
        )
        with mock.patch.dict(os.environ, {"CONF_DIR": str(config), "HOME": "/home/original", "TMPDIR": "/tmp"}):
            expected = short_socket_address_examples()
            for home, parent in cases:
                with mock.patch.dict(os.environ, {"HOME": str(home)}):
                    self.assertEqual(short_socket_address_examples(), expected)
                    for module in (parent / "g-abcdefgh/m0001", parent / "nested/t-abcdefgh/m0001"):
                        with (self.subTest(home=home, module=module),
                              mock.patch.dict(os.environ, {"TMPDIR": str(module)})):
                            self.assertEqual(short_socket_address_examples(), expected)
            longest = max(len(os.fsencode(address)) + 1 for address in expected)
            self.assertLess(longest, 104)

    def test_only_legacy_move_native_mac_and_guest_isolation_name_the_old_roots(self) -> None:
        # Split the search strings so this test cannot match its own declarations.
        memory = "/dev/" + "shm"
        gate_key = "roots." + "gate"
        mac_key = gate_key + ".nates-macbook-pro-2023"
        root_reader = "configured_" + "gate_root"
        # List 542 tip B removes these lines.
        move_worker = {
            ("config/build_folders.conf", f"{gate_key}={memory}"),
            ("scripts/lib/build_folders.py", f"def {root_reader}() -> Path:"),
            ("scripts/lib/build_folders.py", f'return Path(_settings().text("{gate_key}"))'),
            ("scripts/disks/cleanup_old_folders.py", f"gate = build_folders.{root_reader}()"),
        }
        # The L365 follow-on removes these lines.
        native_mac = {
            ("config/build_folders.conf", f"{mac_key}=/private/tmp"),
            ("scripts/mac_test/mac_gate.py", f'root = conf.read_or_repository("build_folders").text("{mac_key}")'),
        }
        # Keep these lines for sandbox isolation. They do not select a test folder.
        guest_isolation = {
            ("scripts/build_targets/mac_linux.py",
             f"'--proc', '/proc', '--bind', str(tmp), '/tmp', '--tmpfs', '{memory}', '--', *command]"),
            ("scripts/build_targets/test_mac_linux.py",
             f"'--proc', '/proc', '--bind', os.environ['TMPDIR'], '/tmp', '--tmpfs', '{memory}', '--']"),
            ("scripts/build_targets/test_mac_linux.py",
             f"assert os.stat('{memory}').st_dev != shm_device, 'The suite {memory} must have its own file system.'"),
            ("scripts/build_targets/test_mac_linux.py",
             f"Path('{memory}/suite-output').write_text('shared memory output')"),
            ("scripts/build_targets/test_mac_linux.py", f"str(os.stat('{memory}').st_dev),"),
        }
        # Keep this mount for the removal sandbox.
        removal_isolation = {
            ("scripts/lib/removal_bwrap_test_support.py",
             f'LINUX_PRIVATE_PATHS = tuple(Path(name) for name in ("/home", "/tmp", "/build", "/session", "{memory}", "/var/tmp"))'),
        }
        # List 542 tip B removes these lines.
        move_fixtures = {
            ("scripts/disks/test_cleanup_old_folders.py",
             r'f"roots.build={self.build}\n' + gate_key
             + r'={self.gate}\n# range: 0.001..1\nlocks.probe_retry_seconds=0.005\n")'),
        }
        allowed = move_worker | native_mac | guest_isolation | removal_isolation | move_fixtures
        checkout = Path(__file__).resolve().parents[2]
        excluded = {".git", "__pycache__", "docs", "node_modules", "target", "test-runs", "typecheck-venv"}
        source_suffixes = {".bash", ".conf", ".js", ".json", ".mjs", ".nix", ".py", ".rs", ".sh", ".toml",
                           ".ts", ".yaml", ".yml", ".zsh"}
        found: list[tuple[str, str]] = []
        for directory, names, files in os.walk(checkout):
            names[:] = [name for name in names if name not in excluded]
            for name in files:
                path = Path(directory) / name
                relative = path.relative_to(checkout)
                is_source = (path.suffix in source_suffixes or relative.parts[0] == "config"
                             or not path.suffix and os.access(path, os.X_OK))
                if path.is_symlink() or not is_source:
                    continue
                for line in path.read_text(errors="replace").splitlines():
                    if memory in line or gate_key in line or root_reader in line:
                        found.append((relative.as_posix(), line.strip()))
        self.assertCountEqual(found, allowed)


class GateModuleOwnerTests(unittest.TestCase):
    def test_a_module_with_no_owner_names_the_gate_module(self) -> None:
        for inherited, owner in (({}, {"CLAUDE_SESSION_NAME": gate.GATE_MODULE_OWNER}),
                                 ({"BUILDLOG_OWNER_STAFF_ID": "impl-id"}, {"BUILDLOG_OWNER_STAFF_ID": "impl-id"})):
            with self.subTest(inherited=inherited), tempfile.TemporaryDirectory() as temporary:
                environment = gate.test_module_environment(Path(temporary), {"PATH": os.defpath, **inherited})
                self.assertEqual({name: environment[name] for name in admission_records.OWNER_VARIABLES
                                  if environment.get(name)}, owner)

    def test_gate_child_does_not_inherit_placement_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shim = root / "scripts" / "lib" / "py"
            shim.parent.mkdir(parents=True)
            shim.touch()
            environment = {"PATH": os.defpath, "BUILDLOG_PLACEMENT_FILE": "/fixture/placement",
                           "BUILDLOG_PLACEMENT_EPISODE": "fixture-episode",
                           "BUILDLOG_PLACEMENT_SIDECAR": "/fixture/placement.cgroup"}
            with mock.patch.object(subprocess, "Popen") as start:
                job = gate.start_gate_job("hooks", Path("test_fixture.py"), (), root, environment, root / "module.log")
            job.output.close()
            child = cast(dict[str, str], start.call_args.kwargs["env"])
        self.assertEqual(child["PATH"], os.defpath)
        self.assertFalse(child.keys() & (environment.keys() - {"PATH"}))


GATE_EVENTS = ".git/gate-events"


def record_gate_event(source: Path, event: str) -> None:
    with (source / GATE_EVENTS).open("a") as log:
        _ = log.write(f"{event}\n")


def run_affected_gate_fixture(status: int | None, detail: str, *, folder_cleanup_error: str) -> int:
    """Run the gate in the child that receives the handoff."""
    source = Path.cwd()
    path = source / ".git" / "assignment.json"
    assert os.environ["BUILDLOG_PLACEMENT_FILE"] == str(path)
    assignment = admission_wire.read_assignment(path).assignment
    identity = build_hold.ProcessIdentity(assignment.owner.pid, assignment.owner.start_time)
    reply = admission_wire.PlacementStatusReply(admission_wire.KnownPlacementStatus(
        assignment, placement.CurrentAssignment(), placement.NoProviderClaim()))

    @contextmanager
    def run_folder(_parent: Path) -> Generator[Path]:
        try:
            yield source
        finally:
            if folder_cleanup_error:
                raise PermissionError(folder_cleanup_error)

    def run(args: gate.GateArguments, mode: gate.SuiteMode, target: gate.GateTarget) -> int:
        record_gate_event(source, "guest")
        assert args.prepared_copy
        assert mode is gate.SuiteMode.TARGETED
        if status is None:
            return 0
        assert target == gate.MacLinuxAssignment(assignment)
        if status == GATE_PRELAUNCH_REFUSED_STATUS:
            raise gate.MacLinuxTookNoRun(detail)
        raise gate.GateSettlementPending(detail)

    with (mock.patch.object(build_hold, "caller_processes", return_value=frozenset({identity})),
          mock.patch.object(gate, "gate_job_from_process",
                            return_value=gate_job_for_command(("gate.py", *sys.argv[1:]))),
          mock.patch.object(admission_client, "placement_status", return_value=reply),
          mock.patch.object(gate, "test_run_folder", side_effect=run_folder),
          mock.patch.object(gate, "run_tests_only", side_effect=run) as suite):
        code = gate.main()
    if status is None:
        suite.assert_not_called()
    else:
        suite.assert_called_once()
    record_gate_event(source, "child finished")
    return code


class DirectGatePlacementTests(unittest.TestCase):
    def checkout(self, folder: str) -> Path:
        source = Path(folder).resolve()
        _ = subprocess.run(["git", "init", "-q", str(source)], check=True)
        _ = subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
                            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false",
                            "commit", "-qm", "Fixture", "--allow-empty"], check=True)
        return source

    def admission(self, source: Path, command: tuple[str, ...], *, target: str = "mac-linux"
                  ) -> first_step.FirstStepAdmission:
        key = treekey.tree_key(str(source))
        start = admission_records.process_start_time(os.getpid())
        assert key is not None and start is not None
        owner = placement.SubmittingOwner(host_name(), os.getpid(), start)
        assignment = replace(placed(target).assignment, owner=owner,
                             source_key=execution_identity.SourceKeyKnown(key))
        grant = admission_wire.Assignment(assignment)
        position = CopyQueuePosition(assignment.episode, assignment.since)
        profile = admission_records.ProgramProfile("gate", str(source), str(source),
            admission_records.HistoryPeakSizing(1, admission_records.FirstProgramPeak(1), 0))
        constraints = placement.PlacementConstraints(assignment.source_key, command, str(source), "", assignment.session,
            (placement.PlacementCandidate(assignment.destination),))
        request = admission_wire.PlacementRequest(position.episode, assignment.owner, position.since, profile, constraints)
        path = source / ".git" / "assignment.json"
        admission_client.write_assignment(path, grant)
        return first_step.FirstStepAdmission(position, first_step.OwnedGatePlacement(grant, request), path)

    def placement_status(self, admitted: first_step.FirstStepAdmission) -> admission_wire.PlacementStatusReply:
        assignment = admission_wire.read_assignment(admitted.assignment_file).assignment
        status = admission_wire.KnownPlacementStatus(assignment, placement.CurrentAssignment(), placement.NoProviderClaim())
        return admission_wire.PlacementStatusReply(status)

    def run_affected_gate(self, source: Path, arguments: tuple[str, ...], *, status: int | None = None,
                          detail: str = "", folder_cleanup_error: str = "") -> int:
        script = "scripts/production/gate.py"
        (source / script).parent.mkdir(parents=True, exist_ok=True)
        # Keep the fixture launcher out of the placement source key.
        _ = (source / ".git/info/exclude").write_text(f"/{script}\n")
        _ = (source / script).write_text(dedent(f"""\
            from scripts.production.test_gate import run_affected_gate_fixture

            raise SystemExit(run_affected_gate_fixture(
                {status!r}, {detail!r}, folder_cleanup_error={folder_cleanup_error!r}))
            """))
        events = source / GATE_EVENTS
        events.unlink(missing_ok=True)
        command = [sys.executable, "-B", script, *arguments]
        repository = Path(__file__).resolve().parents[2]
        with (mock.patch.dict(os.environ, {"PYTHONPATH": str(repository)}),
              mock.patch.object(copies, "caller_owner", return_value="fixture"),
              mock.patch.object(copies, "requested_source", return_value=nullcontext(source)),
              mock.patch.object(copies, "standing", return_value=nullcontext(source)),
              tempfile.TemporaryFile(mode="w+") as diagnostic, redirect_stderr(diagnostic)):
            saved_stderr = os.dup(2)
            try:
                _ = os.dup2(diagnostic.fileno(), 2)
                code = copies.main(["run", "--repo", str(source), "--commit", "HEAD", "--", *command])
            finally:
                _ = os.dup2(saved_stderr, 2)
                os.close(saved_stderr)
            _ = diagnostic.seek(0)
            errors = diagnostic.read()
        _ = sys.stderr.write(errors)
        self.assertIn("child finished", events.read_text().splitlines() if events.exists() else [], errors)
        return code

    def test_direct_gate_requests_settlement_without_replacement(self) -> None:
        for flag, mode in (("--tests-only", gate.SuiteMode.GATE), ("--affected", gate.SuiteMode.TARGETED)):
            for blocked in (False, True):
                with self.subTest(flag=flag, blocked=blocked), tempfile.TemporaryDirectory() as folder:
                    source = self.checkout(folder)
                    arguments = (flag, "--repo", str(source))
                    admitted = self.admission(source, ("gate.py", *arguments))
                    original = admission_wire.read_assignment(admitted.assignment_file).assignment
                    @contextmanager
                    def admit(_command: Sequence[str], checkout: Path, *,
                              targets: placement.GateTargets,
                              place_unnamed_gate: bool = True) -> Generator[first_step.FirstStepAdmission]:
                        self.assertEqual(checkout, source)
                        self.assertEqual(targets.names, ("mac-linux",))
                        self.assertTrue(place_unnamed_gate)
                        record_gate_event(source, "admit")
                        try:
                            yield admitted
                        finally:
                            record_gate_event(source, "close")

                    def run(_args: gate.GateArguments, selected_mode: gate.SuiteMode, target: gate.GateTarget) -> int:
                        self.assertIs(selected_mode, mode)
                        self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], str(admitted.assignment_file))
                        self.assertEqual(target, gate.MacLinuxAssignment(original))
                        record_gate_event(source, "guest")
                        raise gate.GateSettlementPending("fixture guest did not confirm its end")

                    def settle(request: admission_wire.SettlementRequest) -> None:
                        self.assertEqual(request.assignment, original)
                        record_gate_event(source, "settle")
                        if blocked:
                            raise admission_wire.AdmissionUnavailable("fixture settlement failure")

                    with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
                          mock.patch.object(first_step, "gate_targets",
                              return_value=caller_gate_targets(("mac-linux",))),
                          mock.patch.object(first_step, "admitted_run", side_effect=admit),
                          mock.patch.object(admission_client, "placement_status", return_value=self.placement_status(admitted)),
                          mock.patch.object(admission_client, "request_settlement", side_effect=settle),
                          mock.patch.object(queue_place, "wait_for_placement") as again,
                          mock.patch.object(gate, "run_tests_only", side_effect=run),
                          redirect_stderr(io.StringIO()) as errors):
                        if flag == "--affected":
                            status = self.run_affected_gate(source, arguments, status=GATE_UNSETTLED_LAUNCH_STATUS,
                                detail="fixture guest did not confirm its end",
                                folder_cleanup_error="fixture test folder cleanup failed" if blocked else "")
                        else:
                            status = gate.main(arguments)
                        self.assertEqual(status, GATE_UNSETTLED_LAUNCH_STATUS, errors.getvalue())
                    events = (source / GATE_EVENTS).read_text().splitlines()
                    expected = ["admit", "guest"]
                    if flag == "--affected":
                        expected.append("child finished")
                    self.assertEqual(events, [*expected, "settle", "close"])
                    again.assert_not_called()
                    self.assertIn("fixture guest did not confirm its end", errors.getvalue())
                    if blocked:
                        if flag == "--affected":
                            self.assertIn("Gate placement blocked during launch settlement. fixture settlement failure",
                                          errors.getvalue())
                            self.assertIn("Gate cleanup failed. fixture test folder cleanup failed", errors.getvalue())
                        else:
                            self.assertIn("Cannot request launch settlement. fixture settlement failure", errors.getvalue())

    def test_unproven_guest_end_requests_settlement_through_run_gate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = self.checkout(folder)
            arguments = ("--tests-only", "--repo", str(source))
            admitted = self.admission(source, ("gate.py", *arguments), target="mac-linux")
            assignment = admission_wire.read_assignment(admitted.assignment_file).assignment
            armed = mac_linux.ArmedGuestAttempt("fixture-arm", "f" * 32, "boot-id", 10, "user", "/fixture")
            failure = mac_linux.GuestEndUnproven("fixture guest end remains unproven", armed)
            with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
                  mock.patch.object(first_step, "gate_targets",
                      return_value=caller_gate_targets(("mac-linux",))),
                  mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)),
                  mock.patch.object(admission_client, "placement_status", return_value=self.placement_status(admitted)),
                  mock.patch.object(conf, "read", return_value=mock.Mock(value=mock.Mock(return_value=True))),
                  mock.patch.object(gate, "read_gate_checkout", return_value=("fixture-tree", set())),
                  mock.patch.object(gate, "gate_recovery", return_value=nullcontext()),
                  mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
                  mock.patch.object(copies, "source_owner", return_value=""),
                  mock.patch.object(mac_linux, "route", side_effect=failure),
                  mock.patch.object(admission_client, "request_settlement",
                                    side_effect=admission_wire.AdmissionUnavailable("fixture settlement failure")) as settle,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(gate.main(arguments), GATE_UNSETTLED_LAUNCH_STATUS)
            settle.assert_called_once_with(admission_wire.SettlementRequest(assignment))
            self.assertIn(str(failure), errors.getvalue())
            self.assertIn("Cannot request launch settlement. fixture settlement failure", errors.getvalue())

    def test_direct_tests_only_keeps_exit_79_after_retry_fails(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = self.checkout(folder)
            arguments = ("--tests-only", "--repo", str(source))
            admitted = self.admission(source, ("gate.py", *arguments), target="mac-linux")
            with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
                  mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)),
                  mock.patch.object(gate, "run_tests_only", return_value=GATE_PRELAUNCH_REFUSED_STATUS) as run,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(gate.main(arguments), GATE_PRELAUNCH_REFUSED_STATUS, errors.getvalue())
            run.assert_called_once()

    def test_gate_recovery_keeps_placement_status_when_finish_fails(self) -> None:
        checkpoint = ["--merge-branch", "main", "--unit-branch", "fixture", "--checkpoint", "tip",
                      "--previous-checkpoint", "base", "--unit", "fixture", "--phase", "1"]
        for arguments in (checkpoint, ["--tests-only"]):
            for status, refusal in (
                (GATE_PRELAUNCH_REFUSED_STATUS, gate.MacLinuxTookNoRun("fixture guest took no run")),
                (GATE_UNSETTLED_LAUNCH_STATUS, gate.GateSettlementPending("fixture guest end is unproven")),
            ):
                with (self.subTest(arguments=arguments, status=status), tempfile.TemporaryDirectory() as directory,
                      mock.patch.object(gate, "current_gate_placement", return_value=placed("mac-linux")),
                      mock.patch.object(gate, "gate_state_dir", return_value=Path(directory)),
                      mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
                      mock.patch.object(copies, "source_owner", return_value=""),
                      mock.patch.object(gate, "gate_production", return_value=Path(directory)),
                      mock.patch.object(gate, "merge_checkpoint", side_effect=refusal),
                      mock.patch.object(gate, "run_gate_suites", side_effect=refusal),
                      mock.patch.object(gate.GateRecovery, "finish",
                                        side_effect=PermissionError("fixture recovery cleanup failed")) as finish,
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(gate.main(arguments), status, errors.getvalue())
                    finish.assert_called_once()
                    self.assertIn(str(refusal), errors.getvalue())
                    self.assertIn("fixture recovery cleanup failed", errors.getvalue())

    def test_direct_affected_gate_keeps_placement_status_through_folder_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = self.checkout(folder)
            arguments = ("--affected", "--repo", str(source))
            for status, failed_cleanup in ((GATE_PRELAUNCH_REFUSED_STATUS, ""),
                                           (GATE_PRELAUNCH_REFUSED_STATUS, "folder"),
                                           (GATE_UNSETTLED_LAUNCH_STATUS, "folder"),
                                           (GATE_PRELAUNCH_REFUSED_STATUS, "admission")):
                with self.subTest(status=status, failed_cleanup=failed_cleanup):
                    admitted = self.admission(source, ("gate.py", *arguments))
                    owned = admitted.grant
                    assert isinstance(owned, first_step.OwnedGatePlacement)
                    cleanup_error = (PermissionError("fixture admission cleanup failed")
                                     if failed_cleanup == "admission" else None)
                    detail = ("fixture guest took no run" if status == GATE_PRELAUNCH_REFUSED_STATUS
                              else "fixture guest end remains unproven")
                    with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
                          mock.patch.object(first_step, "gate_targets",
                              return_value=caller_gate_targets(("mac-linux",))),
                          mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(source / ".git")),
                          mock.patch.object(first_step, "_gate_request", return_value=owned.request),
                          mock.patch.object(first_step, "_placement_grant", return_value=owned.grant),
                          mock.patch.object(admission_client, "cancel_placement", side_effect=cleanup_error) as cancel,
                          mock.patch.object(admission_client, "request_settlement",
                                            side_effect=admission_wire.AdmissionUnavailable("fixture settlement failure")),
                          redirect_stderr(io.StringIO()) as errors):
                        result = self.run_affected_gate(source, arguments, status=status, detail=detail,
                            folder_cleanup_error="fixture folder cleanup failed" if failed_cleanup == "folder" else "")
                    self.assertEqual(result, status, errors.getvalue())
                    cancel.assert_called_once()
                    self.assertIn(detail, errors.getvalue())
                    if failed_cleanup:
                        self.assertIn(f"fixture {failed_cleanup} cleanup failed", errors.getvalue())

    def test_named_gate_refuses_an_inherited_assignment_for_a_different_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = self.checkout(folder)
            test = source / "scripts/lib/test_fixture.py"
            test.parent.mkdir(parents=True)
            test.touch()
            path = source / ".git" / "assignment.json"
            key = treekey.tree_key(str(source))
            assert key is not None
            owner = placement.SubmittingOwner(host_name(), os.getpid(), "fixture-start")
            assignment = replace(placed("natedev").assignment, owner=owner,
                                 source_key=execution_identity.SourceKeyKnown(key))
            identity = build_hold.ProcessIdentity(owner.pid, owner.start_time)
            grant = admission_wire.Assignment(assignment)
            admission_client.write_assignment(path, grant)
            inherited = first_step.FirstStepAdmission(CopyQueuePosition(assignment.episode, assignment.since),
                                                       first_step.InheritedGatePlacement(grant), path)
            _ = (source / "edit.txt").write_text("Changed since placement.\n")
            response = admission_wire.PlacementStatusReply(admission_wire.KnownPlacementStatus(
                assignment, placement.CurrentAssignment(), placement.NoProviderClaim()))
            for flag in ("--tests-only", "--affected"):
                with self.subTest(flag=flag):
                    arguments = (flag, "--module", "lib/test_fixture.py", "--repo", str(source / ".." / source.name))
                    with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"], "BUILDLOG_PLACEMENT_FILE": str(path),
                                                       "BUILDLOG_PLACEMENT_EPISODE": assignment.episode}, clear=True),
                          mock.patch.object(build_hold, "caller_processes", return_value=frozenset({identity})),
                          mock.patch.object(admission_client, "placement_status", return_value=response),
                          mock.patch.object(gate, "test_run_folder", return_value=nullcontext()),
                          mock.patch.object(gate, "run_tests_only", return_value=0) as run,
                          redirect_stderr(io.StringIO()) as errors):
                        if flag == "--affected":
                            with (mock.patch.object(first_step, "gate_targets",
                                                   return_value=caller_gate_targets(("natedev",))),
                                  mock.patch.object(first_step, "admitted_run", return_value=nullcontext(inherited))):
                                result = self.run_affected_gate(source, arguments)
                        else:
                            result = gate.main(arguments)
                        self.assertEqual(result, 1)
                    self.assertIn("source does not match", errors.getvalue())
                    run.assert_not_called()

    def test_direct_gate_refuses_a_missing_module_before_admission(self) -> None:
        with (tempfile.TemporaryDirectory() as folder,
              mock.patch.dict(os.environ, {}, clear=True),
              mock.patch.object(first_step, "admitted_run") as admit,
              mock.patch.object(gate, "run_tests_only") as run,
              redirect_stderr(io.StringIO()) as errors):
            status = gate.main(["--tests-only", "--repo", folder, "--module", "lib/test_missing.py"])
        self.assertEqual(status, 1)
        self.assertIn("--module 'lib/test_missing.py' does not name a test module", errors.getvalue())
        admit.assert_not_called()
        run.assert_not_called()

    def test_direct_gate_accepts_a_symlinked_module_that_discovery_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "checkout"
            shared = base / "sibling/test_shared.py"
            shared.parent.mkdir()
            shared.touch()
            module = "lib/test_shared.py"
            link = source / "scripts" / module
            link.parent.mkdir(parents=True)
            link.symlink_to(shared)
            self.assertEqual(discover_test_modules(source), [("lib", link)])
            with (mock.patch.dict(os.environ, {}, clear=True),
                  mock.patch.object(gate, "checked_gate_target", return_value="natedev"),
                  mock.patch.object(gate, "run_tests_only", return_value=0) as run):
                self.assertEqual(gate.main(["--tests-only", "--repo", str(source), "--module", module]), 0)
            args, mode, target = cast(tuple[gate.GateArguments, gate.SuiteMode, gate.GateTarget], run.call_args.args)
            self.assertEqual((args.modules, mode, target), ([module], gate.SuiteMode.GATE, "natedev"))

    def test_direct_gate_refuses_an_assignment_for_a_different_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = self.checkout(folder)
            arguments = ("--tests-only", "--repo", str(source / ".." / source.name))
            admitted = self.admission(source, ("gate.py", *arguments))
            _ = (source / "edit.txt").write_text("Changed since placement.\n")
            with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
                  mock.patch.object(first_step, "gate_targets",
                      return_value=caller_gate_targets(("mac-linux",))),
                  mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)) as open_admission,
                  mock.patch.object(admission_client, "placement_status", return_value=self.placement_status(admitted)),
                  mock.patch.object(treekey, "tree_key", wraps=treekey.tree_key) as key,
                  mock.patch.object(gate, "run_tests_only", return_value=0) as run,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(gate.main(arguments), 1)
            open_admission.assert_called_once_with(("gate.py", *arguments), source,
                                                   targets=caller_gate_targets(("mac-linux",)))
            key.assert_called_once_with(str(source))
            self.assertIn("source does not match", errors.getvalue())
            run.assert_not_called()


@final
class MergeGateCheckpointTests(MergeGateFixture):
    @override
    def bind_fixture(self, root: Path) -> None:
        super().bind_fixture(root)
        self.environment = {
            **fixture_git_environment(root.parent),
            **{key: value for key, value in self.environment.items() if not key.startswith("GIT_")},
            "GIT_CONFIG_SYSTEM": os.devnull,
            "SHOWRUNNER_STATE_DIR": str(Path(self.environment["HOME"]) / ".local/state/showrunner"),
        }

    @override
    def setUp(self) -> None:
        super().setUp()
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", self.environment))
        _ = fixture_member("showrunner", name="unrelated")
        _ = fixture_member("showrunner", name="quiet", doc=staff.LiveDoc(remote.local_machine(), self.quiet_production))
        environment = source_environment(self.root / "staff-sources")
        environment["NOTIFIER_STATE_DIR"] = self.environment["NOTIFIER_STATE_DIR"]
        self.environment.update(environment)
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, environment)))
        _ = self.enterContext(isolated_source_readers())

    def review_producer(self, doc: Path) -> staff.StaffId:
        """Give the checkpoint unit a private staff record and a `.unit.toml` whose plan names its checkpoints."""
        self.write(self.unit / ".unit.toml", 'plan = "docs/plans/fixture.md"\nresponsibilities = []\n')
        exclude = Path(self.git(self.unit, "rev-parse", "--git-path", "info/exclude"))
        exclude = exclude if exclude.is_absolute() else self.unit / exclude
        self.write(exclude, (exclude.read_text() if exclude.exists() else "") + ".unit.toml\n")
        runner = fixture_member("showrunner", name="fixture-showrunner", runtime_id=staff.caller_identity(),
                                doc=staff.LiveDoc(remote.local_machine(), doc))
        self.environment.update(build_fixture_sessions(runner.current_session))
        self.environment["CLAUDE_CODE_SESSION_ID"] = runner.current_session
        unit = fixture_unit_location(runner, "fixture", self.unit)
        return unit.id

    def install_rollback_prepare_hook(self, body: str) -> Path:
        hook = self.checkout / ".git/hooks/reference-transaction"
        marker = self.checkout / ".git/rollback-prepare"
        self.write(hook, f'''#!{sys.executable}
import sys
from pathlib import Path

rows = [line.split() for line in sys.stdin]
merge = next((old for old, new, ref in rows
              if new == {self.base!r} and ref in ("HEAD", "refs/heads/integration")), "")
if sys.argv[1] != "prepared" or not merge:
    raise SystemExit(0)
Path({str(marker)!r}).write_text(merge)
{body}
''')
        hook.chmod(0o755)
        return marker

    def test_checkout_holds_a_shared_copy_lock_through_suites_and_releases_it_on_error(self) -> None:
        copies = self.root / "copies"
        checkout = copies / "show-maker" / "21-gate"
        checkout.parent.mkdir(parents=True)
        _ = self.git(self.checkout, "clone", str(self.checkout), str(checkout))
        self.environment["SCRATCH_COPIES_ROOT"] = str(copies)
        with mock.patch.dict(os.environ, self.environment):
            commit = self.git(checkout, "rev-parse", "HEAD")
            scratch.register_copy(checkout, task="gate:fixture", owner_unit="fixture",
                                  source_path=str(self.checkout), source_commit=commit,
                                  copy_commit=commit, target_mode="none", seed_source="", borrows_from="")
        observed: list[Path] = []

        def suites(repo: Path, *_arguments: object, **_keywords: object) -> gate.GateRun:
            self.assertEqual(repo, checkout)
            with scratch.busy_path(repo).open("a") as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(contender, fcntl.LOCK_SH | fcntl.LOCK_NB)
            observed.append(repo)
            raise GateRefused("injected suite stop")

        with mock.patch.object(gate, "run_suites", side_effect=suites):
            result = self.run_gate("--tests-only", "--checkout", str(checkout))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("injected suite stop", result.stderr)
        self.assertEqual(observed, [checkout])
        excludes = (checkout / ".git/info/exclude").read_text().splitlines()
        self.assertEqual(excludes.count("/.claude-busy"), 1)
        self.assertEqual(excludes.count("/.scratch.json"), 1)
        with scratch.busy_path(checkout).open("a") as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_checkpoint_names_its_unit_before_admission_and_suites(self) -> None:
        observed: list[str] = []

        def suites(_repo: Path, *_arguments: object, **_keywords: object) -> gate.GateRun:
            observed.append(os.environ[admission_records.BUILDLOG_UNIT])
            return gate.GateRun((), 0, 0, tests_reused=True)

        @contextmanager
        def admit(command: Sequence[str], source: Path, *, targets: placement.GateTargets) -> Generator[object]:
            self.assertEqual(command[0], 'gate.py')
            self.assertEqual(source, self.checkout)
            self.assertEqual(targets.names, ("natedev",))
            self.assertEqual(os.environ[admission_records.BUILDLOG_UNIT], "fixture")
            self.assertEqual(self.git(source, 'rev-parse', 'HEAD^2'), self.checkpoint)
            yield mock.Mock(spec=first_step.FirstStepAdmission, grant=first_step.InheritedGatePlacement(assignment))

        assignment = placed('natedev')
        with (mock.patch.object(gate, "run_suites", side_effect=suites),
              mock.patch.object(first_step, "gate_targets",
                  return_value=caller_gate_targets(("natedev",))),
              mock.patch.object(first_step, 'admitted_run', side_effect=admit) as admitted,
              mock.patch.object(gate, 'current_gate_placement', side_effect=[gate.NO_GATE_ASSIGNMENT, assignment])):
            result = self.run_gate()
        admitted.assert_called_once()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(observed, ["fixture"])

    def test_the_merge_gate_and_a_tests_only_run_name_an_owner_with_no_session(self) -> None:
        # A gate under systemd, or with no session, still names who started its queue requests.
        observed: list[dict[str, str]] = []

        def suites(_repo: Path, *_arguments: object, **_keywords: object) -> gate.GateRun:
            observed.append({name: os.environ[name] for name in admission_records.OWNER_VARIABLES
                             if os.environ.get(name)})
            return gate.GateRun((), 0, 0, tests_reused=True)

        self.environment.update(dict.fromkeys(admission_records.OWNER_VARIABLES, ""))
        with mock.patch.dict(os.environ, dict.fromkeys(admission_records.OWNER_VARIABLES, "")), \
             mock.patch.object(gate, "run_suites", side_effect=suites), \
             mock.patch.object(admission_records, "family_owner",
                               return_value=admission_records.OwnerNotGiven("systemd · run-p1-i2.service")):
            merge = self.run_gate()
            tests_only = self.run_gate("--tests-only")
            self.assertFalse(admission_records.owner_given(os.environ))
        self.assertEqual(merge.returncode, 0, merge.stdout + merge.stderr)
        self.assertEqual(tests_only.returncode, 0, tests_only.stdout + tests_only.stderr)
        self.assertEqual(observed, [{"CLAUDE_SESSION_NAME": "fixture"}, {"CLAUDE_SESSION_NAME": "verify"}])

    def test_a_tests_only_run_with_a_given_owner_does_not_read_the_copy_record(self) -> None:
        # A bad copy record never refuses a run whose caller gave an owner.
        observed: list[dict[str, str]] = []

        def suites(_repo: Path, *_arguments: object, **_keywords: object) -> gate.GateRun:
            observed.append({name: os.environ[name] for name in admission_records.OWNER_VARIABLES
                             if os.environ.get(name)})
            return gate.GateRun((), 0, 0, tests_reused=True)

        given = {**dict.fromkeys(admission_records.OWNER_VARIABLES, ""), "CLAUDE_SESSION_NAME": "nightly"}
        self.environment.update(given)
        with mock.patch.dict(os.environ, given), mock.patch.object(gate, "run_suites", side_effect=suites), \
             mock.patch.object(copies, "source_owner", side_effect=copies.CopyError("bad copy record")):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(observed, [{"CLAUDE_SESSION_NAME": "nightly"}])

    def test_a_tests_only_run_from_a_terminal_keeps_the_terminal_owner(self) -> None:
        # The terminal family names the owner, so the copy record does not replace it.
        observed: list[dict[str, str]] = []

        def suites(_repo: Path, *_arguments: object, **_keywords: object) -> gate.GateRun:
            observed.append({name: os.environ[name] for name in admission_records.OWNER_VARIABLES
                             if os.environ.get(name)})
            return gate.GateRun((), 0, 0, tests_reused=True)

        unset: dict[str, str] = dict.fromkeys(admission_records.OWNER_VARIABLES, "")
        self.environment.update(unset)
        with mock.patch.dict(os.environ, unset), mock.patch.object(gate, "run_suites", side_effect=suites), \
             mock.patch.object(admission_records, "family_owner", return_value=admission_records.TERMINAL_OWNER), \
             mock.patch.object(copies, "source_owner", return_value="verify"):
            result = self.run_gate("--tests-only")
            owner = admission_records.request_owner("")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(observed, [{}])
        assert isinstance(owner, admission_records.QueueCaller)
        self.assertEqual(owner.name, "terminal")

    def test_checkpoint_restores_the_unit_after_success_and_failure(self) -> None:
        checkpoint = gate.merge_checkpoint
        for previous in (None, "previous"):
            for outcome in ("pass", "red", "exception"):
                with self.subTest(previous=previous, outcome=outcome):
                    if previous is None:
                        _ = self.environment.pop(admission_records.BUILDLOG_UNIT, None)
                    else:
                        self.environment[admission_records.BUILDLOG_UNIT] = previous
                    restored: list[str | None] = []

                    def suites(_repo: Path, *_arguments: object, **_keywords: object) -> gate.GateRun:
                        self.assertEqual(os.environ.get(admission_records.BUILDLOG_UNIT), "fixture")
                        if outcome == "exception":
                            raise GateRefused("injected suite failure")
                        return gate.GateRun(
                            (gate.GateJobResult("hooks", "test_first.py", int(outcome == "red"), 0, "fixture"),),
                            0, 0, tests_reused=True)

                    def merge(arguments: gate.GateArguments, target: str | gate.NoGateAssignment) -> int:
                        self.assertEqual(arguments.gate_job, StepJob("integrate", unit="fixture", branch="integration"))
                        try:
                            return checkpoint(arguments, target)
                        finally:
                            restored.append(os.environ.get(admission_records.BUILDLOG_UNIT))

                    with mock.patch.object(gate, "run_suites", side_effect=suites), \
                         mock.patch.object(gate, "merge_checkpoint", side_effect=merge):
                        result = self.run_gate()
                    self.assertEqual(result.returncode == 0, outcome == "pass", result.stdout + result.stderr)
                    self.assertEqual(restored, [previous])

    def test_rollback_and_recovery_keep_a_commit_made_during_the_suites(self) -> None:
        for change in ("commit", "amend"):
            state = self.state / change
            with self.subTest(change=change), mock.patch.dict(self.environment, GATE_STATE_DIR=str(state)):
                checkout = self.root / change
                _ = self.git(self.root, "clone", "-q", "--branch", "integration", str(self.origin), str(checkout))
                file = checkout / "concurrent.txt"
                changed_head = ""

                def suites(repo: Path, *_arguments: object, **_keywords: object) -> gate.GateRun:
                    nonlocal changed_head
                    self.assertEqual(repo, checkout)
                    self.write(file, "concurrent change\n")
                    _ = self.git(repo, "add", file.name)
                    commit_args = ("--amend", "--no-edit") if change == "amend" else ("-m", "concurrent change")
                    _ = self.git(repo, "commit", *commit_args)
                    changed_head = self.git(repo, "rev-parse", "HEAD")
                    return gate.GateRun((), 0, 0, tests_reused=True)

                with mock.patch.object(gate, "run_gate_suites", side_effect=suites):
                    result = self.run_gate("--repo", str(checkout))
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), changed_head)
                self.assertIn(f"rollback kept HEAD {changed_head}", result.stderr)
                self.assertEqual(file.read_text(), "concurrent change\n")

                with mock.patch.dict(os.environ, self.environment, clear=True), \
                        mock.patch.object(gate, "process_start", return_value=Gone()):
                    with self.assertRaisesRegex(GateRefused, f"rollback kept HEAD {changed_head}"):
                        directory = gate.gate_run_directory(checkout)
                        gate.recover_gate_directory(directory, own_directory=directory)
                self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), changed_head)
                self.assertEqual(file.read_text(), "concurrent change\n")
                self.assert_not_pushed()

    def test_rollback_keeps_a_commit_made_before_rollback_starts(self) -> None:
        file = self.checkout / "concurrent.txt"
        changed_head = ""

        def fail_after_commit(repo: Path, *_arguments: object, **_keywords: object) -> gate.GateRun:
            nonlocal changed_head
            self.write(file, "concurrent change\n")
            changed_head = self.commit(repo, "concurrent change")
            raise GateRefused("fixture suite failure")

        with mock.patch.object(gate, "run_gate_suites", side_effect=fail_after_commit):
            result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), changed_head)
        self.assertIn(changed_head, result.stderr)
        self.assertEqual(file.read_text(), "concurrent change\n")
        self.assertEqual((self.checkout / "unit.txt").read_text(), "checkpoint\n")
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

        with mock.patch.dict(os.environ, self.environment, clear=True), \
                mock.patch.object(gate, "process_start", return_value=Gone()):
            with self.assertRaisesRegex(GateRefused, f"rollback kept HEAD {changed_head}"):
                directory = gate.gate_run_directory(self.checkout)
                gate.recover_gate_directory(directory, own_directory=directory)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), changed_head)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assert_not_pushed()

    def test_rollback_restores_the_index_before_a_competing_commit_can_land(self) -> None:
        hook = self.checkout / ".git/hooks/reference-transaction"
        attempts = self.checkout / ".git/rollback-commit-attempts"
        self.write(hook, f'''#!/bin/sh
matches=$(awk '$2 == "{self.base}" && ($3 == "HEAD" || $3 == "refs/heads/integration") {{ print $3 }}')
case "$1" in
prepared|committed)
    if [ -n "$matches" ]; then
        git commit --allow-empty -qm 'concurrent change' >/dev/null 2>&1
        printf '%s %s\\n' "$1" "$?" >> .git/rollback-commit-attempts
    fi
    ;;
esac
exit 0
''')
        hook.chmod(0o755)

        with mock.patch.object(gate, "run_gate_suites", side_effect=GateRefused("fixture suite failure")):
            result = self.run_gate()

        self.assertEqual(attempts.read_text().splitlines(), ["prepared 128", "committed 0"])
        self.assertEqual(self.git(self.checkout, "ls-tree", "HEAD", "--", "unit.txt"), "",
                         "The competing commit contains the rejected checkpoint.")
        self.assertEqual(self.git(self.checkout, "ls-files", "--", "unit.txt"), "")
        self.assertFalse((self.checkout / "unit.txt").exists())
        self.assertEqual(self.git(self.checkout, "log", "-1", "--format=%s"), "concurrent change")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD^"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("merge undone, not pushed", result.stderr)
        self.assert_not_pushed()

    def test_rollback_completes_when_the_prepare_hook_writes_large_diagnostics(self) -> None:
        marker = self.install_rollback_prepare_hook('sys.stderr.write("x" * (256 * 1024) + "\\n")')
        result = self.run_gate(own_process=True, preamble='''\
def fail_suites(*args, **kwargs):
    raise gate.GateRefused("fixture suite failure")

gate.run_gate_suites = fail_suites
''')
        self.assertTrue(marker.is_file(), result.stderr[-512:])
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assertEqual(result.returncode, 1, result.stderr[-512:])
        self.assertIn("fixture suite failure; merge undone, not pushed", result.stderr[-512:])
        self.assert_not_pushed()

    def test_rollback_refusal_includes_the_prepare_hook_diagnostic(self) -> None:
        message = "fixture prepare hook refused rollback"
        marker = self.install_rollback_prepare_hook(f"print({message!r}, file=sys.stderr)\nraise SystemExit(1)")
        with mock.patch.object(gate, "run_gate_suites", side_effect=GateRefused("fixture suite failure")):
            result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue(marker.is_file(), result.stderr)
        self.assertIn(message, result.stderr)
        self.assertIn("cannot prepare the rollback ref transaction", result.stderr)
        merged = marker.read_text()
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [merged, self.base, self.checkpoint])
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assert_not_pushed()

    def test_recovery_finishes_rollback_on_either_side_of_the_ref_commit(self) -> None:
        hold = gate.hold_rollback_head
        for when in ("before", "after"):
            state = self.state / when
            with self.subTest(when=when), mock.patch.dict(self.environment, GATE_STATE_DIR=str(state)):
                checkout = self.root / f"rollback-{when}"
                _ = self.git(self.root, "clone", "-q", "--branch", "integration", str(self.origin), str(checkout))
                merged = ""
                injected = False

                def fail_suites(repo: Path, *_arguments: object, **_keywords: object) -> gate.GateRun:
                    nonlocal merged
                    merged = self.git(repo, "rev-parse", "HEAD")
                    raise GateRefused("fixture suite failure")

                @contextmanager
                def interrupt_ref_commit(repo: Path, baseline: str, head: str) -> Generator[None]:
                    nonlocal injected
                    with hold(repo, baseline, head):
                        yield
                        if when == "before":
                            injected = True
                            raise KeyboardInterrupt("fixture stop before the ref commit")
                    injected = True
                    raise KeyboardInterrupt("fixture stop after the ref commit")

                with mock.patch.object(gate, "run_gate_suites", side_effect=fail_suites), \
                        mock.patch.object(gate, "hold_rollback_head", side_effect=interrupt_ref_commit):
                    result = self.run_gate("--repo", str(checkout))
                self.assertTrue(injected)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f"fixture stop {when} the ref commit", result.stderr)
                self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), merged if when == "before" else self.base)
                self.assertEqual(self.git(checkout, "ls-files", "--", "unit.txt"), "")
                self.assertFalse((checkout / "unit.txt").exists())

                with mock.patch.dict(os.environ, self.environment, clear=True), \
                        mock.patch.object(gate, "process_start", return_value=Gone()):
                    directory = gate.gate_run_directory(checkout)
                    self.assertEqual(len(list(directory.glob("*.json"))), 1)
                    gate.recover_gate_directory(directory, own_directory=directory)
                    self.assertEqual(list(directory.glob("*.json")), [])
                self.assertEqual(self.git(checkout, "rev-parse", "HEAD"), self.base)
                self.assertEqual(self.git(checkout, "status", "--porcelain"), "")
                self.assert_not_pushed()

    def test_checkpoint_uses_default_sigterm_for_recovery(self) -> None:
        def run(_args: gate.GateArguments, _target: gate.GateTarget) -> int:
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)
            return 0

        with (mock.patch.object(gate, "checked_gate_target", return_value=gate.NO_GATE_ASSIGNMENT),
              mock.patch.object(gate, "run_gate_command", side_effect=run)):
            result = gate.main(["--repo", str(self.checkout), "--merge-branch", "integration",
                                "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint,
                                "--previous-checkpoint", self.base, "--unit", "fixture", "--phase", "1"])
        self.assertEqual(result, 0)

    def test_unsettled_checkpoint_keeps_exit_80_and_the_baseline_when_rollback_fails(self) -> None:
        pending = gate.GateSettlementPending("fixture launch remains unproven")
        with (mock.patch.object(gate, "current_gate_placement", return_value=placed("mac-linux")),
              mock.patch.object(gate, "run_gate_suites", side_effect=pending) as run,
              mock.patch.object(gate, "rollback_merge", side_effect=OSError("fixture rollback failed")) as rollback):
            result = self.run_gate()
        run.assert_called_once()
        merged = self.git(self.checkout, "rev-parse", "HEAD")
        rollback.assert_called_once_with(self.checkout, self.base, checkpoint=self.checkpoint,
                                         recorded_merge=merged, recovery=mock.ANY)
        self.assertEqual(result.returncode, GATE_UNSETTLED_LAUNCH_STATUS, result.stdout + result.stderr)
        self.assertIn(str(pending), result.stderr)
        self.assertIn("fixture rollback failed", result.stderr)
        with mock.patch.dict(os.environ, self.environment):
            records = list(gate.gate_run_directory(self.checkout).glob("*.json"))
        self.assertEqual(len(records), 1)
        saved = cast(dict[str, object], json.loads(records[0].read_text()))
        self.assertEqual(saved["rollback"], self.base)
        self.assert_not_pushed()

    def test_unsettled_checkpoint_keeps_exit_80_when_admission_cleanup_fails(self) -> None:
        pending = gate.GateSettlementPending("fixture launch remains unproven")
        profile = admission_records.ProgramProfile("gate", str(self.checkout), str(self.checkout),
            admission_records.HistoryPeakSizing(1, admission_records.FirstProgramPeak(1), 0))
        grant = placed("mac-linux")
        assignment = grant.assignment
        constraints = placement.PlacementConstraints(assignment.source_key, ("gate.py",), str(self.checkout),
            "", assignment.session, (placement.PlacementCandidate(assignment.destination),))
        request = admission_wire.PlacementRequest(assignment.episode, assignment.owner, assignment.since,
                                                 profile, constraints)

        @contextmanager
        def admit() -> Generator[first_step.FirstStepAdmission]:
            try:
                position = CopyQueuePosition(assignment.episode, assignment.since)
                yield first_step.FirstStepAdmission(position, first_step.OwnedGatePlacement(grant, request),
                                                   self.root / "assignment.json")
            finally:
                raise OSError("fixture admission cleanup failed")

        with (mock.patch.object(gate, "current_gate_placement", side_effect=[gate.NO_GATE_ASSIGNMENT, grant]),
              mock.patch.object(first_step, "gate_targets",
                  return_value=caller_gate_targets(("mac-linux",))),
              mock.patch.object(first_step, "admitted_run", return_value=admit()),
              mock.patch.object(admission_client, "request_settlement"),
              mock.patch.object(gate, "run_gate_suites", side_effect=pending) as run):
            result = self.run_gate()
        self.assertEqual(result.returncode, GATE_UNSETTLED_LAUNCH_STATUS, result.stdout + result.stderr)
        self.assertIn(str(pending), result.stderr)
        self.assertIn("fixture admission cleanup failed", result.stderr)
        run.assert_called_once()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()

    def test_checkpoint_retries_its_merge_after_a_stop_before_push(self) -> None:
        with mock.patch.object(promotion, "push_integration", side_effect=GateRefused("injected stop before push")):
            stopped = self.run_gate()
        self.assertNotEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("injected stop before push", stopped.stderr)
        merged = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertNotEqual(merged, self.base)
        self.assert_not_pushed()
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), merged)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), merged)
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [merged, self.base, self.checkpoint])

    def test_checkpoint_retry_refuses_a_remote_rewound_below_its_base(self) -> None:
        self.write(self.checkout / "published.txt", "published code\n")
        published = self.commit(self.checkout, "published non-doc change")
        _ = self.git(self.checkout, "push", "origin", "integration")
        with mock.patch.object(promotion, "push_integration", side_effect=GateRefused("injected stop before push")):
            stopped = self.run_gate()
        self.assertNotEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertIn("injected stop before push", stopped.stderr)
        merged = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [merged, published, self.checkpoint])
        _ = self.git(self.origin, "update-ref", "refs/heads/integration", self.base, published)

        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("merge HEAD differs from origin merge branch", result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), merged)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), self.base)

    def test_checkpoint_retry_refuses_a_recorded_merge_for_another_checkpoint(self) -> None:
        with mock.patch.object(promotion, "push_integration", side_effect=GateRefused("injected push failure")):
            stopped = self.run_gate()
        self.assertIn("injected push failure", stopped.stderr)
        merged = self.git(self.checkout, "rev-parse", "HEAD")
        self.write(self.unit / "next.txt", "next checkpoint\n")
        checkpoint = self.commit(self.unit, "next checkpoint")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        result = self.run_gate(checkpoint=checkpoint)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), merged)
        self.assert_not_pushed()

    @gate_suites("folder_from_the_checkout")
    def test_a_folder_without_a_registry_entry_runs_and_is_reported(self) -> None:
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_all_modules_started()
        self.assertRegex(result.stdout, r"(?m)^folder_from_the_checkout: OK \d+\.\d+s$")
        self.assertEqual(self.pass_records()[0]["suites"], ["folder_from_the_checkout"])

    def test_module_folders_are_removed_after_success_and_preparation_error(self) -> None:
        prepare = gate.test_module_environment
        for fails in (False, True):
            with self.subTest(raises=fails):
                folders: list[Path] = []

                def prepare_module(root: Path, environment: dict[str, str]) -> dict[str, str]:
                    folders.append(root)
                    self.assertTrue(root.is_dir())
                    if fails:
                        raise RuntimeError("module preparation failed")
                    prepared = prepare(root, {**environment, "HERDR_BIN": "/host/herdr"})
                    self.assertEqual(prepared["HERDR_BIN"], MISSING_HERDR_BIN)
                    return prepared

                with mock.patch.object(gate, "test_module_environment", side_effect=prepare_module):
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, "module preparation failed"):
                            _ = self.run_gate("--tests-only", "--full")
                    else:
                        result = self.run_gate("--tests-only", "--full")
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(folders)
                self.assertTrue(all(not folder.exists() for folder in folders), folders)

    def test_finished_module_keeps_a_held_child_folder(self) -> None:
        prepare = gate.test_module_environment
        children: list[Path] = []

        def hold_child(root: Path, environment: dict[str, str]) -> dict[str, str]:
            child = root / "active-child"
            child.mkdir()
            lock = self.enterContext((root / "active-child.lock").open("wb"))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            children.append(child)
            return prepare(root, environment)

        with mock.patch.object(gate, "test_module_environment", side_effect=hold_child):
            result = self.run_gate("--tests-only", "--full")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(children)
        self.assertTrue(all(child.is_dir() for child in children), children)

    def test_nested_modules_run_and_retry_from_their_own_folder_under_the_top_suite(self) -> None:
        nested = self.checkout / "scripts" / "hooks" / "tests" / "test_first.py"
        self.write(nested, SUITE_MODULE)
        _ = self.commit(self.checkout, "nested test fixture")
        self.write(self.state / "fail-first-hooks", "fail")
        result = self.run_gate("--tests-only", "--full")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        starts = [(row["suite"], row["module"]) for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual(starts, [("hooks", "test_first.py"), ("hooks", "tests/test_first.py")] * 2)
        self.assertIn("Rerun alone: hooks/tests/test_first.py passed", result.stdout)
        self.assertIn("Rerun alone: hooks/test_first.py passed", result.stdout)
        self.assertRegex(result.stdout, r"(?m)^hooks: OK \d+\.\d+s$")
        self.assertNotRegex(result.stdout, r"(?m)^tests:")
        launches = [call for call in self.calls() if call["tool"] == "suite-python"]
        runner = str(self.checkout / "scripts/test_times/module_run.py")
        self.assertCountEqual([call["args"] for call in launches],
                              [[runner, str(folder), nested.name]
                               for folder in (nested.parent, nested.parent.parent)] * 2)
        self.assertTrue(all(call["cwd"] == str(self.checkout) for call in launches))
        timings = gate.read_measures(self.state / "runner" / "module_seconds.json")
        self.assertEqual(set(timings), {"hooks/test_first.py", "hooks/tests/test_first.py"})
        self.assertEqual(self.pass_records()[0]["suites"], ["hooks"])

    def test_gate_ignores_the_inherited_module_folder_for_placement(self) -> None:
        temporary = self.fixture_root.parent
        parent = temporary / "gate-test-runs"
        existing = parent / "inherited-module"
        existing.mkdir(parents=True)
        for inherited in (temporary / "missing-module", existing, self.root):
            with (self.subTest(inherited=inherited),
                  mock.patch.dict(self.environment, {gate.GATE_MODULE_TEMP_DIR: str(inherited)}),
                  mock.patch.object(gate, "gate_temporary", wraps=gate.gate_temporary) as allocate):
                self.clear_gate_events()
                result = self.run_gate("--tests-only", "--module", "hooks/test_first.py")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                allocate.assert_called_once_with(mock.ANY, "g", parent)
                self.assert_all_modules_started()
        self.assertTrue(existing.is_dir())

    @gate_suites("hooks", "message")
    def test_gate_and_affected_modules_use_the_checkout_test_runs(self) -> None:
        prepare = gate.test_module_environment
        output = self.root / "build/fixture/verify/deadbeef"
        output.mkdir(parents=True)
        (self.checkout / "target").symlink_to(output, target_is_directory=True)
        expected = self.root / "build/test-runs/fixture/verify/deadbeef"
        gate_root = self.fixture_root.parent
        inherited_run = gate_root / "test-runs/test-run-abcdefgh"
        inherited_run.mkdir(parents=True)
        deep_tmpdir = gate_root / "gate-invocation-abcdefghijklmnopqrstuvwxyz"
        deep_tmpdir.mkdir()
        for flag in ("--tests-only", "--affected"):
            for inherited in (gate_root, inherited_run, deep_tmpdir):
                with self.subTest(mode=flag, inherited=inherited):
                    self.clear_gate_events()
                    job_folders: list[Path] = []

                    def prepare_module(root: Path, environment: dict[str, str]) -> dict[str, str]:
                        job_folders.append(root)
                        return prepare(root, environment)

                    environment = {"TMPDIR": str(inherited), "BUILDLOG_PREPARED_COPY": str(self.checkout)}
                    with (mock.patch.dict(self.environment, environment),
                          mock.patch.object(copies, "take_prepared_gate_handoff", return_value=flag == "--affected"),
                          mock.patch.object(gate, "test_module_environment", side_effect=prepare_module)):
                        result = self.run_gate(flag, "--module", "hooks/test_first.py", "--module", "message/test_first.py")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assert_all_modules_started()
                    self.assertCountEqual([root.name for root in job_folders], ["m0001", "m0002"])
                    for root in job_folders:
                        self.assertEqual(root.parent.parent, expected)
                        self.assertRegex(root.parent.name, r"^[gt]-[a-z0-9_]{8}$")
                        self.assertFalse(root.exists())

    def test_folders_without_test_modules_and_cached_files_are_not_suites(self) -> None:
        scripts = self.checkout / "scripts"
        self.write(scripts / "empty" / "helper.py", "raise AssertionError('helper was run')\n")
        self.write(scripts / "cached" / "__pycache__" / "test_cached.py",
                   "raise AssertionError('cached test was run')\n")
        (scripts / "empty" / "test_directory.py").mkdir()
        _ = self.commit(self.checkout, "non-test folder fixture")
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_all_modules_started()
        self.assertNotRegex(result.stdout, r"(?m)^(?:empty|cached|__pycache__):")
        self.assertEqual(self.pass_records()[0]["suites"], ["hooks"])

    @gate_suites()
    def test_an_empty_test_list_is_refused_before_any_job_runs(self) -> None:
        result = self.run_gate("--tests-only")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the test list is empty", result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.module_events(), [])

    def test_dirty_merge_checkout_is_refused_without_destroying_changes(self) -> None:
        self.write(self.checkout / "subject.txt", "uncommitted\n")
        self.assert_refused_before_gate(self.run_gate())
        self.assertEqual((self.checkout / "subject.txt").read_text(), "uncommitted\n")
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)

    def test_untracked_showrunner_log_does_not_block_the_gate(self) -> None:
        self.write(self.checkout / "docs" / "plans" / "build-followups-log.md", "log\n")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.checkout / "docs" / "plans" / "build-followups-log.md").read_text(), "log\n")

    def test_merge_refuses_type_check_options_before_any_suite_runs(self) -> None:
        result = self.run_gate("--type-dirs", "scripts")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unrecognized arguments: --type-dirs scripts", result.stderr)
        self.assertEqual(self.module_events(), [])
        self.assert_not_pushed()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)

    def test_merge_checkout_ahead_of_origin_is_refused(self) -> None:
        self.write(self.checkout / "local.txt", "local\n")
        local = self.commit(self.checkout, "unpublished merge work")
        self.assert_refused_before_gate(self.run_gate())
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), local)

    def test_a_local_production_doc_commit_goes_out_with_the_merge(self) -> None:
        # agents retire, add_unit and promote_showrunner leave their doc commit to the next merge push.
        self.write(self.checkout / "local.txt", "retired\n")
        local = self.commit(self.checkout, "production(fixture): retire alpha")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Local doc commit goes out with this merge: {local[:12]}", result.stdout)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), head)
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [head, local, self.checkpoint])

    def test_a_red_gate_keeps_the_local_production_doc_commit(self) -> None:
        self.write(self.checkout / "local.txt", "retired\n")
        local = self.commit(self.checkout, "production(fixture): retire alpha")
        self.write(self.state / "modify-tracked", "modify")
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), local)
        self.assert_not_pushed()

    def test_a_mac_linux_guest_that_took_no_run_undoes_the_merge(self) -> None:
        cause = "the guest did not arm the run: fixture SSH refused the connection"
        with mock.patch.object(gate, "run_gate_suites",
                               side_effect=gate.MacLinuxTookNoRun(f"Mac Linux guest took no run: {cause}")):
            result = self.run_gate()
        self.assertEqual(result.returncode, MAC_LINUX_PRELAUNCH_REFUSED_STATUS, result.stdout + result.stderr)
        self.assertEqual(result.stderr.splitlines()[-2:],
                         ["Merge undone. The merge was not pushed.", f"Mac Linux guest took no run: {cause}"])
        self.assertNotIn("REFUSED", result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assert_not_pushed()

    def test_a_mac_linux_guest_that_took_no_run_keeps_its_status_when_rollback_fails(self) -> None:
        refusal = "Mac Linux guest took no run: fixture SSH refused the connection"
        with (mock.patch.object(gate, "run_gate_suites", side_effect=gate.MacLinuxTookNoRun(refusal)),
              mock.patch.object(gate, "rollback_merge", side_effect=OSError("fixture rollback failed")) as rollback):
            result = self.run_gate()
        merged = self.git(self.checkout, "rev-parse", "HEAD")
        rollback.assert_called_once_with(self.checkout, self.base, checkpoint=self.checkpoint,
                                         recorded_merge=merged, recovery=mock.ANY)
        self.assertEqual(result.returncode, MAC_LINUX_PRELAUNCH_REFUSED_STATUS, result.stdout + result.stderr)
        self.assertIn(refusal, result.stderr)
        self.assertIn("fixture rollback failed", result.stderr)
        self.assertNotIn("merge undone", result.stderr)
        self.assert_not_pushed()

    def test_a_mac_linux_guest_that_took_no_run_keeps_the_recovery_record_when_save_fails(self) -> None:
        save = gate.GateRecovery.save

        def fail_cleanup_save(recovery: gate.GateRecovery) -> None:
            if recovery.rollback_head and not recovery.rollback:
                raise OSError("fixture recovery save failed")
            save(recovery)

        refusal = gate.MacLinuxTookNoRun("Mac Linux guest took no run: fixture SSH refused the connection")
        with (mock.patch.object(gate, "run_gate_suites", side_effect=refusal),
              mock.patch.object(gate.GateRecovery, "save", new=fail_cleanup_save)):
            result = self.run_gate()
        self.assertEqual(result.returncode, MAC_LINUX_PRELAUNCH_REFUSED_STATUS, result.stdout + result.stderr)
        self.assertIn("fixture recovery save failed", result.stderr)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        with mock.patch.dict(os.environ, self.environment):
            records = list(gate.gate_run_directory(self.checkout).glob("*.json"))
        self.assertEqual(len(records), 1, "The gate must keep the recovery record.")
        saved = cast(dict[str, object], json.loads(records[0].read_text()))
        self.assertEqual(saved["rollback"], self.base)

    def test_merge_checkout_behind_origin_is_refused(self) -> None:
        _ = self.git(self.unit, "push", "origin", "HEAD:integration")
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), self.checkpoint)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.base)
        self.assertEqual(self.calls(), [])

    def test_wrong_checkout_branch_is_refused(self) -> None:
        _ = self.git(self.checkout, "checkout", "-b", "wrong-branch")
        self.assert_refused_before_gate(self.run_gate())

    def test_checkpoint_not_on_remote_unit_branch_is_refused(self) -> None:
        self.write(self.unit / "unpublished.txt", "unpublished\n")
        unpublished = self.commit(self.unit, "unpublished checkpoint")
        self.assert_refused_before_gate(self.run_gate(checkpoint=unpublished))

    def test_missing_checkpoint_is_refused(self) -> None:
        self.assert_refused_before_gate(self.run_gate(checkpoint="does-not-exist"))

    def test_missing_previous_checkpoint_is_refused(self) -> None:
        self.assert_refused_before_gate(self.run_gate(previous="does-not-exist"))

    def test_previous_checkpoint_must_be_an_ancestor(self) -> None:
        self.write(self.unit / "later.txt", "later\n")
        later = self.commit(self.unit, "later checkpoint")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        self.assert_refused_before_gate(self.run_gate(previous=later))

    def test_conflict_aborts_merge_and_names_conflicting_files(self) -> None:
        self.write(self.unit / "subject.txt", "unit version\n")
        self.checkpoint = self.commit(self.unit, "unit conflict")
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        self.write(self.checkout / "subject.txt", "merge version\n")
        self.base = self.commit(self.checkout, "merge conflict")
        _ = self.git(self.checkout, "push", "origin", "integration")
        result = self.run_gate(previous=self.git(self.unit, "rev-parse", "HEAD~1"))
        self.assert_refused_before_gate(result)
        self.assertIn("subject.txt", result.stdout + result.stderr)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        self.assertFalse((self.checkout / ".git" / "MERGE_HEAD").exists())

    def test_a_checkpoint_without_a_change_review_line_is_refused_before_the_merge(self) -> None:
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades")
        result = self.run_gate(checkpoint=checkpoint)
        self.assert_refused_before_gate(result)
        self.assertIn("checkpoint(fixture): grid fades: no Change-Review line", result.stderr)
        self.assertIn(change_review.HELD_NEXT_STEP, result.stderr)

    def test_a_visible_checkpoint_without_an_image_is_refused_before_the_merge(self) -> None:
        line = "Change-Review: The grid fades | Open Hana | The grid appears"
        self.write(self.unit / "visible.txt", line)
        with mock.patch.dict(self.environment, {"GIT_COMMITTER_DATE": "2026-11-16T00:00:00-05:00",
                                               "GIT_AUTHOR_DATE": "2026-10-15T12:00:00-04:00"}):
            checkpoint = self.commit(self.unit, "checkpoint(fixture): grid fades\n\n" + line)
        _ = self.git(self.unit, "push", "origin", "unit-phase")
        result = self.run_gate(checkpoint=checkpoint)
        self.assert_refused_before_gate(result)
        self.assertIn(checkpoint[:8] + " checkpoint(fixture): grid fades:", result.stderr)
        self.assertIn("has no image", result.stderr)
        self.assertIn("change_review.py image --image <png>", result.stderr)
        self.assertIn(line, result.stderr)

    def test_a_green_merge_adds_one_row_per_visible_change_to_the_named_list(self) -> None:
        review, doc = self.review_list()
        original = review.read_bytes()
        producer = self.review_producer(doc)
        member = staff.snapshot().by_id(producer)
        assert member is not None and isinstance(member.parent, staff.ReportsTo)
        production = member.parent.id
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\n"
                                            + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away | image 012345abcdef\n"
                                            + "Change-Review: none — the gate script")
        # The row carries today in the user's zone, as the gate reads it; a run across midnight may take either day.
        with mock.patch.dict(os.environ, self.environment, clear=True):
            before = change_review.today()
            result = self.run_gate("--promote", "--production", "fixture-showrunner", checkpoint=checkpoint)
            days = {before.isoformat(), change_review.today().isoformat()}
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        merged = self.git(self.checkout, "rev-parse", "--short=8", "HEAD")
        written = review.read_text(encoding="utf-8").splitlines()[-1]
        rows = {f"| C8 | The grid fades gently | {day} · {merged} | not yet | Run `hana` in a natedev terminal. You see: The grid lines fade out farther away | {production} | {producer} | 012345abcdef |" for day in days}
        self.assertRegex(written, r"^\| C8 \| [^|]+ \| \d{4}-\d\d-\d\d \d\d:\d\d · ")
        self.assertIn(re.sub(r" \d\d:\d\d · ", " · ", written, count=1), rows)
        self.assertIn(f"Change review: {written}", result.stdout)
        self.assertNotIn("drift", result.stderr)
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")
        for explicit_checkout in (True, False):
            with self.subTest(promote_only=True, explicit_checkout=explicit_checkout):
                _ = review.write_bytes(original)
                promoted = self.run_promote_only("--production", "fixture-showrunner",
                                                 explicit_checkout=explicit_checkout)
                self.assertEqual(promoted.returncode, 0, promoted.stdout + promoted.stderr)
                promoted_rows = change_review.pending(review)
                self.assertEqual([row["Change"] for row in promoted_rows], ["old", "The grid fades gently"])
                self.assertEqual(promoted_rows[-1]["Producer"], producer)
                self.assertEqual(promoted_rows[-1]["Production"], production)

    def test_a_change_row_uses_the_user_date_when_utc_has_the_next_date(self) -> None:
        review, doc = self.review_list()
        _ = self.review_producer(doc)
        checkpoint = self.checkpoint_commit("checkpoint(fixture): date\n\n"
                                            + "Change-Review: The date follows the user zone | Read | The local date | image 012345abcdef")
        instant = datetime(2026, 11, 4, 1, tzinfo=UTC)

        def now(zone: tzinfo | None = None) -> datetime:
            return instant.astimezone(zone) if zone is not None else instant.replace(tzinfo=None)

        self.environment["USER_ZONE"] = "America/New_York"
        with mock.patch.object(change_review, "datetime", mock.Mock(wraps=datetime, now=now)):
            result = self.run_gate("--promote", "--production", "fixture-showrunner", checkpoint=checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(change_review.pending(review)[-1]["Merged"].split(" · ")[0], "2026-11-03 20:00")

    def test_without_production_the_calling_showrunners_list_takes_the_rows(self) -> None:
        review, doc = self.review_list()
        producer = self.review_producer(doc)
        runner = staff.by_id(producer)
        assert runner is not None and isinstance(runner.parent, staff.ReportsTo)
        owner = staff.by_id(runner.parent.id)
        assert owner is not None
        self.environment["CODEX_THREAD_ID"] = owner.current_session
        timer = Path(self.environment["NOTIFIER_STATE_DIR"]) / f"showrunner-{runner.parent.id}"
        timer.mkdir(parents=True)
        self.write(timer / "conf", f"TARGET=session:{runner.parent.id}\n"
                   + f"CHECK=bash /scripts/production/production_check.sh {runner.parent.id}\n")
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\n"
                                            + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away | image 012345abcdef\n"
                                            + "Change-Review: The sky dims | Run `hana` in a natedev terminal | The sky turns darker | image 012345abcdef")
        result = self.run_gate("--promote", checkpoint=checkpoint, callers_production=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"Change review: 2 row(s) added to {review}: C8–C9", result.stdout)
        self.assertEqual([line.split(" | ")[1] for line in review.read_text(encoding="utf-8").splitlines()[-2:]],
                         ["The grid fades gently", "The sky dims"])
        self.assertEqual([row[change_review.PRODUCER] for row in change_review.pending(review)
                          if row["#"] in {"C8", "C9"}], [producer, producer])

    def test_a_gate_that_finds_no_production_is_refused_before_the_merge(self) -> None:
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\n"
                                            + "Change-Review: The grid fades gently | Run `hana` in a natedev terminal | The grid lines fade out farther away | image 012345abcdef")
        result = self.run_gate(checkpoint=checkpoint, callers_production=True)
        self.assert_refused_before_gate(result)
        self.assertIn("pass --production <name>: the gate adds this merge's change-review rows to its list",
                      result.stderr)

    def test_a_production_without_a_list_still_merges_a_checkpoint_with_its_line(self) -> None:
        doc = self.root / "production.md"
        self.write(doc, "# p\n\n- **Merge branch:** `integration`\n")
        _ = self.review_producer(doc)
        checkpoint = self.checkpoint_commit("checkpoint(fixture): grid fades\n\nChange-Review: none — the gate script")
        result = self.run_gate("--promote", "--production", "fixture-showrunner", checkpoint=checkpoint)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Change review: the production doc has no Change review line; no rows written",
                      result.stdout)


    @gate_suites(*FIXTURE_FOLDERS)
    def test_green_gate_pushes_no_ff_merge_with_subject_body_and_all_modules(self) -> None:
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        head = self.git(self.checkout, "rev-parse", "HEAD")
        self.assertEqual(self.git(self.origin, "rev-parse", "integration"), head)
        notifications = [call["args"] for call in self.calls() if call["tool"] == "style_sync.py"]
        self.assertEqual(notifications, [["notify", "--repo-dir", str(self.checkout), "--branch", "integration", "--tip", head]])
        self.assertEqual((self.state / "notified-tip").read_text(), head)
        self.assertEqual(self.git(self.checkout, "rev-list", "--parents", "-n", "1", "HEAD").split(),
                         [head, self.base, self.checkpoint])
        message = self.git(self.checkout, "log", "-1", "--format=%B")
        self.assertIn(f"Merge fixture phase-2 ({self.checkpoint}) into integration", message)
        self.assertIn("Fixture checkpoint gate.", message)
        starts = [row for row in self.module_events() if row["event"] == "start"]
        self.assertCountEqual([(row["suite"], row["module"]) for row in starts],
                              [(suite, "test_first.py") for suite in self.suites] + ([("lib", "test_second.py")] if "lib" in self.suites else []))
        self.assertEqual(len({row["pid"] for row in starts}), self.module_count)
        self.assertEqual(len({row["tmpdir"] for row in starts}), self.module_count)
        self.assertTrue(all(not Path(row["tmpdir"]).exists() for row in starts))
        self.assertEqual([call for call in self.calls() if call["tool"] == "basedpyright"], [])
        for suite in self.suites:
            self.assertRegex(result.stdout, rf"(?m)^{suite}: OK \d+\.\d+s$")
        peak = re.search(r"peak memory: ([\d.]+) GiB", result.stdout)
        self.assertIsNotNone(peak, result.stdout)
        assert peak is not None
        self.assertGreater(float(peak.group(1)), 0)
        self.assertRegex(result.stdout, r"(?m)^Largest module peak: [\d.]+ GiB \(sampled memory\); module: (?:\w+/test_\w+\.py|\(not sampled\))$")
        launches = [call for call in self.calls() if call["tool"] == "suite-python"]
        self.assertEqual(len(launches), self.module_count)
        runner = str(self.checkout / "scripts/test_times/module_run.py")
        expected = [[runner, str(self.checkout / "scripts" / suite), "test_first.py"] for suite in self.suites]
        if "lib" in self.suites:
            expected.append([runner, str(self.checkout / "scripts/lib"), "test_second.py"])
        self.assertCountEqual([call["args"] for call in launches], expected)
        for call in launches:
            self.assertEqual(call["cwd"], str(self.checkout))
        self.assertEqual(self.git(self.origin, "rev-parse", "main"), self.base)
        self.assertFalse(any(call["tool"] == "ssh" for call in self.calls()))

    def test_rejected_push_never_notifies_style(self) -> None:
        self.write(self.origin / "hooks/pre-receive", "#!/bin/sh\nexit 1\n")
        (self.origin / "hooks/pre-receive").chmod(0o755)
        result = self.run_gate()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_not_pushed()
        self.assertFalse(any(call["tool"] == "style_sync.py" for call in self.calls()))

    def test_style_notification_failure_keeps_the_green_merge_successful(self) -> None:
        self.write(self.state / "notify-fails", "fail")
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("style sync: exit 1", result.stdout)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "integration"))

    def test_notification_value_error_keeps_the_completed_merge_successful(self) -> None:
        original_notify = gate.notify_style

        def malformed_notification(repo: Path, branch: str, tip: str) -> None:
            with mock.patch("scripts.production.gate.subprocess.run",
                            side_effect=ValueError("invalid production")):
                original_notify(repo, branch, tip)

        arguments = ["--repo", str(self.checkout), "--merge-branch", "integration",
                     "--unit-branch", "unit-phase", "--checkpoint", self.checkpoint,
                     "--previous-checkpoint", self.base, "--unit", "fixture", "--phase", "phase-2",
                     "--body", "Fixture checkpoint gate.",
                     "--main-repo", str(self.main),
                     "--ssh", str(self.bin / "ssh"), "--production", "quiet"]
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.environment, clear=True), mock.patch.object(
            gate, "notify_style", side_effect=malformed_notification,
        ), redirect_stdout(output):
            code = gate.main(arguments)
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("style sync: invalid production", output.getvalue())
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), self.git(self.origin, "rev-parse", "integration"))


@final
class GateRecoveryTests(MergeGateFixture):
    def test_recovery_skips_record_removed_during_owner_check(self) -> None:
        directory = self.root / "recovery"
        directory.mkdir()
        pool = self.root / "tokens"
        os.mkfifo(pool)
        descriptor = os.open(pool, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, descriptor)
        status = os.fstat(descriptor)
        token = gate.HeldGateToken(str(pool), status.st_dev, status.st_ino, b"t".hex())
        recovery = gate.GateRecovery(directory / "owner.json", str(self.checkout), 5_000_001, "owner",
                                     tokens=[token])
        recovery.save()

        def finish_owner(_pid: int) -> Gone:
            _ = os.write(descriptor, b"t")
            recovery.finish()
            return Gone()

        with (mock.patch.object(gate, "process_start", side_effect=finish_owner) as read_start,
              mock.patch.dict(os.environ, self.environment)):
            gate.recover_gate_directory(directory, own_directory=directory)

        read_start.assert_called_once_with(recovery.pid)
        self.assertFalse(recovery.path.exists())
        self.assertEqual(os.read(descriptor, 2), b"t")

    def test_tests_only_sigterm_closes_the_active_record(self) -> None:
        self.write(self.state / "hold-module", "hold")
        ready = self.state / "module-held"
        with mock.patch.dict(os.environ, self.environment):
            records = gate.gate_run_directory(self.checkout)
        command = [*self.gate_command(), "--tests-only", "--repo", str(self.checkout),
                   "--module", "hooks/test_first.py"]
        with subprocess.Popen(command, cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            try:
                wait_until(lambda: ready.exists() or process.poll() is not None, self.state, processes=(process,))
                self.assertTrue(ready.is_file())
                self.assertEqual(len(list(records.glob(f"{process.pid}-*.json"))), 1)
                process.send_signal(signal.SIGTERM)
                output, errors = process.communicate(timeout=TEST_HANG_GUARD_SECS)
                self.assertEqual(process.returncode, -signal.SIGTERM, output + errors)
                self.assertEqual(list(records.glob("*.json")), [])
            finally:
                if process.poll() is None:
                    process.kill()
                    _ = process.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_recovery_reports_every_refusal_in_its_own_directory(self) -> None:
        directory = self.root / "recovery"
        directory.mkdir()
        unreadable = GateRefused("cannot recover gate 5000001: start is unreadable")
        rollback = gate.RollbackRecoveryRefused("gate recovery kept the rollback record")
        refusals = {directory / "a.json": unreadable, directory / "b.json": rollback}
        for path in refusals:
            self.write(path, "{}")

        def refuse(path: Path) -> None:
            raise refusals[path]

        with (mock.patch.object(gate, "recover_gate_record", side_effect=refuse) as recover,
              self.assertRaises(GateRefused) as refused):
            gate.recover_gate_directory(directory, own_directory=directory)
        self.assertEqual(recover.call_count, 2)
        self.assertIs(type(refused.exception), GateRefused)
        self.assertEqual(str(refused.exception), f"{unreadable}\n{rollback}")

    def test_tests_only_recovers_unfinished_record_and_keeps_finished_record(self) -> None:
        with mock.patch.dict(os.environ, self.environment):
            directory = gate.gate_run_directory(self.checkout)
        directory.mkdir(parents=True)
        unfinished = gate.GateRecovery(directory / "unfinished.json", str(self.checkout), 5_000_001, "dead-owner")
        unfinished.save()
        folder = self.root / "retained"
        canary = folder / "canary"
        self.write(canary, "fixture data")
        finished = gate.GateRecovery(directory / "finished.json", str(self.checkout), 5_000_002, "dead-owner",
                                     folders=[str(folder)], finished=True)
        finished.save()
        saved = finished.path.read_text()

        result = self.run_gate("--tests-only", "--module", "hooks/test_first.py")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list(directory.glob("*.json")), [finished.path])
        self.assertEqual(finished.path.read_text(), saved)
        self.assertEqual(canary.read_text(), "fixture data")
        self.assertNotIn(str(folder), result.stderr)


class LiveConfigTripwireTests(unittest.TestCase):
    def test_live_config_violation_uses_the_nonrerunnable_tripwire(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        folders = self.enterContext(ExitStack())
        spec = build_folders.BuildFolderSpec(kind="tool", parent=root, purpose="job", suffix_length=0)
        job_root = folders.enter_context(build_folders.temporary_build_folder(spec))
        marker = job_root / "live-config-writes"
        _ = marker.write_text("/fixture/config/console.conf\n")
        log = job_root / "output.log"
        process = cast(subprocess.Popen[str], mock.Mock(spec=subprocess.Popen))
        job = gate.RunningGateJob("hooks", Path("test_fixture.py"), process, time.monotonic(),
                                  time.time(), log.open("w+"), log, "test_fixture.py", folder_cleanup=folders)
        self.addCleanup(job.output.close)
        with mock.patch.object(gate, "kill_job_group"):
            result = gate.finish_gate_job(job, 0)
        self.assertTrue(result.tripwire_tripped)
        self.assertEqual(result.returncode, 1)
        self.assertIn("a test attempted to write live configuration at /fixture/config/console.conf", result.output)
        self.assertFalse(job_root.exists())


if __name__ == "__main__":
    _ = unittest.main()


class MergeGateProducerTests(ProducerFixture):
    @override
    def setUp(self) -> None:
        super().setUp()
        environment = source_environment(self.root / "staff-sources")
        _ = self.enterContext(cast(AbstractContextManager[object], mock.patch.dict(os.environ, environment)))
        _ = self.enterContext(isolated_source_readers())

    def test_record_changes_writes_the_checkpoint_units_staff_id(self) -> None:
        merged = self.merge()
        gate.record_changes(self.repo, self.listing, self.doc, "main")
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)
        assert isinstance(self.unit.parent, staff.ReportsTo)
        self.assertEqual(change_review.pending(self.listing)[0]["Production"], self.unit.parent.id)

        with self.subTest(producer="recorded before the unit leaves staff"):
            before = self.listing.read_bytes()
            _ = self.listing.unlink()
            record = staff.StaffSnapshot(tuple(row for row in staff.snapshot().rows if row.id != self.unit.id))
            with mock.patch.object(staff, "snapshot", return_value=record):
                gate.record_changes(self.repo, self.listing, self.doc, "main",
                    known_producer=change_review.KnownMergeProducer(merged, self.unit.id))
            self.assertEqual(change_review.pending(self.listing)[0]["Producer"], self.unit.id)
            self.assertEqual(self.listing.read_bytes(), before)

    def test_record_changes_keeps_an_unknown_producer_empty_and_prints_one_named_note(self) -> None:
        record = staff.StaffSnapshot(tuple(row for row in staff.snapshot().rows if row.id != self.unit.id))
        output = io.StringIO()
        with (mock.patch.object(staff, "snapshot", return_value=record), redirect_stdout(output)):
            _ = self.merge()
            gate.record_changes(self.repo, self.listing, self.doc,
                                      "main")
        self.assertEqual(change_review.pending(self.listing)[0]["Producer"], "")
        notes = [line for line in output.getvalue().splitlines() if line.startswith("change review: no staff row")]
        self.assertEqual(len(notes), 1)
        self.assertIn("old-branch", notes[0])

"""Gate checkpoint contracts."""

from __future__ import annotations

from scripts.build_queue import admission_client, admission_records, admission_wire, execution_identity, first_step, placement, queue_place
from scripts.build_queue.admission_jobs import StepJob

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
import tempfile
import time
from typing import cast, final, override
from contextlib import AbstractContextManager, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from collections.abc import Generator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, tzinfo
from unittest import mock
from scripts.build_targets import mac_linux
from scripts.buildlog import treekey
from scripts.buildlog.store import host_name
from scripts.direct.wait_for_test_state import wait_until
from scripts.copies import copies
from scripts.lib import conf, scratch
from scripts.lib.build_copies import CopyQueuePosition
from scripts.lib.build_steps import GATE_PRELAUNCH_REFUSED_STATUS, GATE_UNSETTLED_LAUNCH_STATUS, MAC_LINUX_PRELAUNCH_REFUSED_STATUS
from scripts.lib.process_start import Gone
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.production import change_review, gate, staff
from scripts.production.gate_modules import GateRefused
from scripts.production.test_change_review import ProducerFixture
from scripts.production.staff_test_support import (
    build_fixture_sessions, fixture_member, fixture_unit_location, isolated_source_readers, isolated_staff_file, source_environment,
)
from scripts.production.gate_test_support import FIXTURE_FOLDERS, MergeGateFixture, SUITE_MODULE, gate_suites
from scripts.production.test_gate_default_placement import placed


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

    def test_direct_gate_uses_the_replacement_grant_after_settlement(self) -> None:
        for flag, mode in (("--tests-only", gate.SuiteMode.GATE), ("--affected", gate.SuiteMode.TARGETED)):
            for blocked in (False, True):
                with self.subTest(flag=flag, blocked=blocked), tempfile.TemporaryDirectory() as folder:
                    source = self.checkout(folder)
                    arguments = (flag, "--repo", str(source))
                    admitted = self.admission(source, ("gate.py", *arguments))
                    original = admission_wire.read_assignment(admitted.assignment_file).assignment
                    events: list[str] = []

                    @contextmanager
                    def admit(_command: Sequence[str], checkout: Path, *,
                              targets: tuple[str, ...]) -> Generator[first_step.FirstStepAdmission]:
                        self.assertEqual(checkout, source)
                        self.assertEqual(targets, ("mac-linux",))
                        events.append("admit")
                        try:
                            yield admitted
                        finally:
                            events.append("close")

                    def run(_args: gate.GateArguments, selected_mode: gate.SuiteMode, target: gate.GateTarget) -> int:
                        self.assertIs(selected_mode, mode)
                        self.assertEqual(os.environ["BUILDLOG_PLACEMENT_FILE"], str(admitted.assignment_file))
                        self.assertEqual(target, gate.MacLinuxAssignment(original))
                        events.append("guest")
                        raise gate.GateSettlementPending("fixture guest did not confirm its end")

                    def settle(request: admission_wire.SettlementRequest) -> None:
                        self.assertEqual(request.assignment, original)
                        events.append("settle")
                        if blocked:
                            raise admission_wire.AdmissionUnavailable("fixture settlement failure")

                    @contextmanager
                    def test_folder() -> Generator[None]:
                        try:
                            yield
                        finally:
                            if blocked:
                                raise OSError("fixture test folder cleanup failed")

                    with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
                          mock.patch.object(first_step, "gate_targets", return_value=("mac-linux",)),
                          mock.patch.object(first_step, "admitted_run", side_effect=admit),
                          mock.patch.object(admission_client, "placement_status", return_value=self.placement_status(admitted)),
                          mock.patch.object(admission_client, "request_settlement", side_effect=settle),
                          mock.patch.object(queue_place, "wait_for_placement") as again,
                          mock.patch.object(gate, "test_run_folder", side_effect=test_folder),
                          mock.patch.object(gate, "run_tests_only", side_effect=run),
                          redirect_stderr(io.StringIO()) as errors):
                        self.assertEqual(gate.main(arguments), GATE_UNSETTLED_LAUNCH_STATUS, errors.getvalue())
                    self.assertEqual(events, ["admit", "guest", "settle", "close"])
                    again.assert_not_called()
                    self.assertIn("fixture guest did not confirm its end", errors.getvalue())
                    if blocked:
                        self.assertIn("Cannot request launch settlement. fixture settlement failure", errors.getvalue())
                        if flag == "--affected":
                            self.assertIn("Gate cleanup failed. fixture test folder cleanup failed", errors.getvalue())

    def test_unproven_guest_end_requests_settlement_through_run_gate(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = self.checkout(folder)
            arguments = ("--tests-only", "--repo", str(source))
            admitted = self.admission(source, ("gate.py", *arguments), target="mac-linux")
            assignment = admission_wire.read_assignment(admitted.assignment_file).assignment
            armed = mac_linux.ArmedGuestAttempt("fixture-arm", "f" * 32, "boot-id", 10, "user", "/fixture")
            failure = mac_linux.GuestEndUnproven("fixture guest end remains unproven", armed)
            with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
                  mock.patch.object(first_step, "gate_targets", return_value=("mac-linux",)),
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

                    @contextmanager
                    def admit() -> Generator[first_step.FirstStepAdmission]:
                        try:
                            yield admitted
                        finally:
                            if failed_cleanup == "admission":
                                raise PermissionError("fixture admission cleanup failed")

                    @contextmanager
                    def run_folder() -> Generator[Path]:
                        try:
                            yield source
                        finally:
                            if failed_cleanup == "folder":
                                raise PermissionError("fixture folder cleanup failed")

                    outcome = (gate.MacLinuxTookNoRun("fixture guest took no run") if status == GATE_PRELAUNCH_REFUSED_STATUS
                               else gate.GateSettlementPending("fixture guest end remains unproven"))
                    with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
                          mock.patch.object(first_step, "gate_targets", return_value=("mac-linux",)),
                          mock.patch.object(first_step, "admitted_run", return_value=admit()),
                          mock.patch.object(admission_client, "placement_status", return_value=self.placement_status(admitted)),
                          mock.patch.object(gate, "test_run_folder", side_effect=run_folder),
                          mock.patch.object(gate, "run_tests_only", side_effect=outcome) as run,
                          mock.patch.object(admission_client, "request_settlement",
                                            side_effect=admission_wire.AdmissionUnavailable("fixture settlement failure")),
                          redirect_stderr(io.StringIO()) as errors):
                        self.assertEqual(gate.main(arguments), status, errors.getvalue())
                        run.assert_called_once()
                        if failed_cleanup:
                            self.assertIn(f"fixture {failed_cleanup} cleanup failed", errors.getvalue())

    def test_named_gate_refuses_an_inherited_assignment_for_a_different_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = self.checkout(folder)
            path = source / ".git" / "assignment.json"
            key = treekey.tree_key(str(source))
            assert key is not None
            owner = placement.SubmittingOwner(host_name(), os.getpid(), "fixture-start")
            assignment = replace(placed("natedev").assignment, owner=owner,
                                 source_key=execution_identity.SourceKeyKnown(key))
            identity = build_hold.ProcessIdentity(owner.pid, owner.start_time)
            admission_client.write_assignment(path, admission_wire.Assignment(assignment))
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
                        self.assertEqual(gate.main(arguments), 1)
                    self.assertIn("source does not match", errors.getvalue())
                    run.assert_not_called()

    def test_direct_gate_refuses_an_assignment_for_a_different_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = self.checkout(folder)
            arguments = ("--tests-only", "--repo", str(source / ".." / source.name))
            admitted = self.admission(source, ("gate.py", *arguments))
            _ = (source / "edit.txt").write_text("Changed since placement.\n")
            with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True),
                  mock.patch.object(first_step, "gate_targets", return_value=("mac-linux",)),
                  mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)) as open_admission,
                  mock.patch.object(admission_client, "placement_status", return_value=self.placement_status(admitted)),
                  mock.patch.object(treekey, "tree_key", wraps=treekey.tree_key) as key,
                  mock.patch.object(gate, "run_tests_only", return_value=0) as run,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(gate.main(arguments), 1)
            open_admission.assert_called_once_with(("gate.py", *arguments), source, targets=("mac-linux",))
            key.assert_called_once_with(str(source))
            self.assertIn("source does not match", errors.getvalue())
            run.assert_not_called()


@final
class MergeGateCheckpointTests(MergeGateFixture):
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
        def admit(command: Sequence[str], source: Path, *, targets: tuple[str, ...]) -> Generator[object]:
            self.assertEqual(command[0], 'gate.py')
            self.assertEqual(source, self.checkout)
            self.assertEqual(targets, ("natedev",))
            self.assertEqual(os.environ[admission_records.BUILDLOG_UNIT], "fixture")
            self.assertEqual(self.git(source, 'rev-parse', 'HEAD^2'), self.checkpoint)
            yield mock.Mock(spec=first_step.FirstStepAdmission, grant=first_step.InheritedGatePlacement(assignment))

        assignment = placed('natedev')
        with (mock.patch.object(gate, "run_suites", side_effect=suites),
              mock.patch.object(first_step, "gate_targets", return_value=("natedev",)),
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
        rollback.assert_called_once_with(self.checkout, self.base)
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
              mock.patch.object(first_step, "gate_targets", return_value=("mac-linux",)),
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
        checked = gate.checked_git

        def stop_at_push(repo: Path, *arguments: str) -> str:
            if arguments == ("push", "origin", "integration"):
                raise GateRefused("injected stop before push")
            return checked(repo, *arguments)

        with mock.patch.object(gate, "checked_git", side_effect=stop_at_push):
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
        checked = gate.checked_git

        def stop_at_push(repo: Path, *arguments: str) -> str:
            if arguments == ("push", "origin", "integration"):
                raise GateRefused("injected stop before push")
            return checked(repo, *arguments)

        with mock.patch.object(gate, "checked_git", side_effect=stop_at_push):
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
        checked = gate.checked_git

        def stop_at_push(repo: Path, *arguments: str) -> str:
            if arguments == ("push", "origin", "integration"):
                raise GateRefused("injected push failure")
            return checked(repo, *arguments)

        with mock.patch.object(gate, "checked_git", side_effect=stop_at_push):
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

    def test_gate_uses_configured_root_when_module_folder_is_missing(self) -> None:
        gate_root = self.fixture_root.parent
        missing = gate_root / "missing-module"
        with (mock.patch.dict(self.environment, {gate.GATE_MODULE_TEMP_DIR: str(missing)}),
              mock.patch.object(gate, "gate_temporary", wraps=gate.gate_temporary) as allocate):
            result = self.run_gate("--tests-only", "--module", "hooks/test_first.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        allocate.assert_called_once_with(mock.ANY, "g", gate_root)
        self.assert_all_modules_started()

    def test_nested_gate_uses_the_inherited_module_folder_only_under_the_gate_root(self) -> None:
        gate_root = self.fixture_root.parent
        module = gate_root / "inherited-module"
        module.mkdir()
        outside = gate_root.parent / f"{gate_root.name}-outside"
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        for inherited, parent in ((module, module.resolve()), (outside, gate_root)):
            with self.subTest(inherited=inherited.name):
                with (mock.patch.dict(self.environment, {gate.GATE_MODULE_TEMP_DIR: str(inherited)}),
                      mock.patch.object(gate, "gate_temporary", wraps=gate.gate_temporary) as allocate):
                    result = self.run_gate("--tests-only", "--module", "hooks/test_first.py")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                allocate.assert_called_once_with(mock.ANY, "g", parent)

    @gate_suites("hooks", "message")
    def test_gate_and_affected_module_folders_leave_room_for_unix_sockets(self) -> None:
        prepare = gate.test_module_environment
        gate_root = self.fixture_root.parent
        legacy_run = Path("test-runs/test-run-abcdefgh")
        inherited_run = gate_root / legacy_run
        inherited_run.mkdir(parents=True)
        deep_tmpdir = gate_root / "gate-invocation-abcdefghijklmnopqrstuvwxyz"
        deep_tmpdir.mkdir()
        sockets = (Path(".com.google.Chrome.123456/SingletonSocket"),
                   Path("style-socket-abcdefgh/outside.sock"))
        for flag in ("--tests-only", "--affected"):
            for inherited in (gate_root, inherited_run, deep_tmpdir):
                with self.subTest(mode=flag, inherited=inherited):
                    job_folders: list[Path] = []

                    def prepare_module(root: Path, environment: dict[str, str]) -> dict[str, str]:
                        job_folders.append(root)
                        return prepare(root, environment)

                    with (mock.patch.dict(self.environment, {"TMPDIR": str(inherited)}),
                          mock.patch.object(gate, "test_module_environment", side_effect=prepare_module)):
                        result = self.run_gate(flag, "--module", "hooks/test_first.py", "--module", "message/test_first.py")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertCountEqual([root.name for root in job_folders], ["m0001", "m0002"])
                    for root in job_folders:
                        self.assertEqual(root.parent.parent, gate_root)
                        relative = root.relative_to(gate_root)
                        for platform_root, limit in ((Path("/dev/shm"), 108), (Path("/private/tmp"), 104)):
                            for suffix in sockets:
                                socket_path = platform_root / relative / legacy_run / suffix
                                self.assertLessEqual(len(os.fsencode(socket_path)) + 1, limit, str(socket_path))

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
        rollback.assert_called_once_with(self.checkout, self.base)
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
        self.write(doc, "# p\n")
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
        with mock.patch.dict(os.environ, self.environment), mock.patch.object(
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
        job_root = root / "job"
        job_root.mkdir()
        marker = job_root / "live-config-writes"
        _ = marker.write_text("/fixture/config/console.conf\n")
        log = job_root / "output.log"
        process = cast(subprocess.Popen[str], mock.Mock(spec=subprocess.Popen))
        job = gate.RunningGateJob("hooks", Path("test_fixture.py"), process, time.monotonic(),
                                  time.time(), log.open("w+"), log, "test_fixture.py")
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

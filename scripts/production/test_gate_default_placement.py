"""Route gate commands through the queue assignment."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import chdir, contextmanager, nullcontext, redirect_stderr
from io import StringIO
import os
from pathlib import Path
import shlex
import signal
import subprocess
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_client, admission_wire as wire, execution_identity, first_step, placement
from scripts.build_queue.admission_client import OuterReservation
from scripts.build_targets import mac_linux
from scripts.copies import copies
from scripts.lib import build_folders, conf, scratch
from scripts.lib.build_steps import GATE_RESULT_UNKNOWN_STATUS, MAC_LINUX_PRELAUNCH_REFUSED_STATUS
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.mac_test.claim_state import WHOLE_MAC
from scripts.production import gate
from scripts.production.gate_routes import GateResultUnknown, ModuleCoverage


def placed(target: str) -> wire.Assignment:
    provider = 'scaler' if target == 'aws' else 'ssh' if target == 'mac-linux' else 'local'
    return wire.Assignment(placement.PlacementAssignment('episode', 'attempt',
        placement.PlacementDestination(provider, target, 'fixture-host', WHOLE_MAC),
        execution_identity.SourceKeyKnown('fixture-source'), placement.SubmittingOwner('fixture-host', 1, '1'),
        NoOwningSession(), 1.0))


class GateDefaultPlacementTests(unittest.TestCase):
    def test_gate_step_preserves_incomplete_child_status_when_cancellation_fails(self) -> None:
        for status in (MAC_LINUX_PRELAUNCH_REFUSED_STATUS, GATE_RESULT_UNKNOWN_STATUS):
            with self.subTest(status=status), TemporaryDirectory() as folder:
                source = Path(folder)
                command = ["gate.py", "--tests-only"]
                request = mock.Mock(spec=wire.PlacementRequest, local=wire.LocalAdmissionUnavailable("fixture"))
                with (mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(source)),
                      mock.patch.object(first_step, "_gate_request", return_value=request),
                      mock.patch.object(first_step, "_placement_grant", return_value=placed("mac-linux")),
                      mock.patch.object(admission_client, "cancel_placement", side_effect=wire.AdmissionUnavailable(
                          "fixture queue unavailable")) as cancel,
                      mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(command, status)),
                      redirect_stderr(StringIO()) as errors):
                    result = copies.main(["gate-step", "--repo", folder, "--targets", "mac-linux", "--", *command])
                self.assertEqual(result, status)
                self.assertEqual(errors.getvalue(), "Gate cleanup failed. fixture queue unavailable\n")
                cancel.assert_called_once()

    def test_tests_only_sigterm_releases_the_placement_before_termination(self) -> None:
        arguments = ["--tests-only"]
        events: list[str] = []

        @contextmanager
        def admit(*_args: object, **_options: object) -> Generator[object]:
            events.append('admit')
            try:
                yield mock.Mock(spec=first_step.FirstStepAdmission,
                                grant=mock.Mock(spec=first_step.GatePlacement))
            finally:
                events.append('release')

        def interrupt(*_args: object) -> int:
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            events.append('interrupt')
            handler(signal.SIGTERM, None)
            raise AssertionError('SIGTERM did not interrupt the gate')

        def terminate(number: int) -> None:
            self.assertEqual(number, signal.SIGTERM)
            self.assertEqual(signal.getsignal(number), signal.SIG_DFL)
            events.append('terminate')

        with (mock.patch.dict(os.environ, clear=True),
              mock.patch.object(first_step, 'gate_targets', return_value=('natedev', 'mac-linux')),
              mock.patch.object(first_step, 'admitted_run', side_effect=admit),
              mock.patch.object(gate, 'current_gate_placement',
                                side_effect=[gate.NO_GATE_ASSIGNMENT, placed('natedev')]),
              mock.patch.object(gate, 'run_tests_only', side_effect=interrupt),
              mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
              mock.patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              mock.patch.object(signal, 'raise_signal', side_effect=terminate),
              self.assertRaises(SystemExit) as exited):
            _ = gate.main(arguments)
        self.assertEqual(exited.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(events, ['admit', 'interrupt', 'release', 'terminate'])

    def test_default_modes_enter_one_admission_for_the_whole_command(self) -> None:
        checkpoint = ['--merge-branch', 'main', '--unit-branch', 'fixture', '--checkpoint', 'tip',
                      '--previous-checkpoint', 'base', '--unit', 'fixture', '--phase', '1']
        for arguments in (checkpoint, ['--tests-only']):
            with self.subTest(arguments=arguments):
                calls: list[str] = []

                @contextmanager
                def admit(command: Sequence[str], source: Path, *, targets: tuple[str, ...]) -> Generator[object]:
                    self.assertEqual(list(command), ['gate.py', *arguments])
                    self.assertEqual(source, Path.cwd())
                    self.assertEqual(targets, ('natedev', 'mac-linux'))
                    calls.append('admit')
                    try:
                        yield mock.Mock(spec=first_step.FirstStepAdmission,
                                        grant=mock.Mock(spec=first_step.GatePlacement))
                    finally:
                        calls.append('release')

                def run(*_arguments: object) -> int:
                    calls.append('run')
                    return 0

                def merge(args: gate.GateArguments, target: gate.GateTarget) -> int:
                    with gate.gate_suite_target(args, Path.cwd(), target):
                        return run()

                with (mock.patch.dict(os.environ, {'BUILDLOG_GATE_TARGETS': 'natedev,mac-linux,aws'}, clear=True),
                      mock.patch.object(first_step, 'admitted_run', side_effect=admit),
                      mock.patch.object(gate, 'current_gate_placement',
                                        side_effect=[gate.NO_GATE_ASSIGNMENT, placed('natedev')]),
                      mock.patch.object(gate, 'run_tests_only', side_effect=run),
                      mock.patch.object(gate, 'merge_checkpoint', side_effect=merge),
                      mock.patch.object(gate, 'test_run_folder', return_value=nullcontext()),
                      mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
                      mock.patch.object(gate, 'gate_recovery', return_value=nullcontext())):
                    self.assertEqual(gate.main(arguments), 0)
                self.assertEqual(calls, ['admit', 'run', 'release'])

    def test_direct_affected_gate_prints_the_copy_command_and_stops(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "unit's worktree"
            repo.mkdir()
            environment = fixture_git_environment(root)
            _ = subprocess.run(["git", "init", "-q", str(repo)], env=environment, check=True,
                               capture_output=True, timeout=TEST_HANG_GUARD_SECS)
            commit = commit_repository(repo, "Unit fixture", allow_empty=True, env=environment)
            environment["BUILDLOG_PREPARED_COPY"] = str(repo)
            _ = environment.pop(copies.PREPARED_GATE_FD, None)
            for module in ((), ("--module", "fixture/test_ok.py", f"--checkout={repo}")):
                arguments = ["--affected", *module]
                with (self.subTest(arguments=arguments), chdir(repo),
                      mock.patch.dict(os.environ, environment, clear=True),
                      mock.patch.object(gate, "gate_start_target") as place,
                      mock.patch.object(gate, "run_gate_command") as run,
                      redirect_stderr(StringIO()) as errors):
                    self.assertEqual(gate.main(arguments), 1)
                command, = errors.getvalue().splitlines()
                self.assertEqual(shlex.split(command), [
                    "python3", "~/.claude/scripts/copies/copies.py", "run", "--repo", str(repo),
                    "--commit", commit, "--", "./scripts/lib/py", "scripts/production/gate.py",
                    *arguments, *(["--repo", "{tree}"] if module else [])])
                place.assert_not_called()
                run.assert_not_called()

    def test_printed_affected_copy_command_runs_with_the_prepared_handoff(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, tree = root / "unit", root / "numbered copy"
            arguments = ["--affected", "--checkout", str(source), "--full"]
            commit = "a" * 40
            with (mock.patch.dict(os.environ, clear=True), mock.patch.object(gate, "checked_git", return_value=commit),
                  redirect_stderr(StringIO()) as errors):
                self.assertEqual(gate.main(arguments), 1)
            copy_command = shlex.split(errors.getvalue())[2:]

            def launch(words: Sequence[str], *, cwd: Path, pass_fds: tuple[int, ...],
                       env: dict[str, str], text: bool) -> subprocess.CompletedProcess[str]:
                self.assertEqual(cwd, tree)
                self.assertEqual(list(words), ["./scripts/lib/py", "scripts/production/gate.py", *arguments,
                                               "--repo", str(tree)])
                self.assertTrue(text)
                reader, = pass_fds
                self.assertEqual(env[copies.PREPARED_GATE_FD], str(reader))
                inherited = {**env, copies.PREPARED_GATE_FD: str(os.dup(reader))}
                with mock.patch.dict(os.environ, inherited, clear=True):
                    status = gate.main(words[2:])
                return subprocess.CompletedProcess(words, status)

            with (mock.patch.dict(os.environ, clear=True),
                  mock.patch.object(first_step, "gate_targets", return_value=("aws",)),
                  mock.patch.object(first_step, "admitted_run") as admit,
                  mock.patch.object(copies, "caller_owner", return_value="fixture"),
                  mock.patch.object(copies, "_command_copy", return_value=nullcontext(copies.DirectCommandCopy(tree))),
                  mock.patch.object(subprocess, "run", side_effect=launch),
                  mock.patch.object(gate, "current_gate_placement", return_value=placed("aws")),
                  mock.patch.object(gate, "test_run_folder", return_value=nullcontext()),
                  mock.patch.object(gate, "run_tests_only", return_value=0) as run):
                self.assertEqual(copies.main(copy_command), 0)
            admit.assert_not_called()
            run.assert_called_once()
            args, mode, target = cast(tuple[gate.GateArguments, gate.SuiteMode, gate.GateTarget], run.call_args.args)
            self.assertTrue(args.prepared_copy)
            self.assertEqual((args.repo, mode, target), (str(tree), gate.SuiteMode.TARGETED, "aws"))

    def test_copied_gate_reuses_its_aws_assignment(self) -> None:
        assignment = placed('aws')
        with TemporaryDirectory() as directory:
            tree = Path(directory)
            command = ["./scripts/lib/py", "scripts/production/gate.py", "--tests-only"]

            def launch(arguments: Sequence[str], *, cwd: Path, pass_fds: tuple[int, ...],
                       env: dict[str, str], text: bool) -> subprocess.CompletedProcess[str]:
                self.assertEqual(arguments, command)
                self.assertEqual(cwd, tree)
                self.assertTrue(text)
                reader, = pass_fds
                self.assertEqual(env[copies.PREPARED_GATE_FD], str(reader))
                # The parent keeps its descriptor during the child run.
                inherited = {**env, copies.PREPARED_GATE_FD: str(os.dup(reader))}
                with mock.patch.dict(os.environ, inherited, clear=True):
                    gate_arguments = ["--tests-only", "--repo", directory]
                    status = gate.main(gate_arguments)
                    self.assertEqual(gate.main(gate_arguments), 1)
                return subprocess.CompletedProcess(arguments, status)

            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "aws"}, clear=True),
                  mock.patch.object(copies, "caller_owner", return_value="fixture"),
                  mock.patch("scripts.copies.copies._command_copy",
                             return_value=nullcontext(copies.DirectCommandCopy(tree))),
                  mock.patch.object(subprocess, "run", side_effect=launch),
                  mock.patch.object(gate, 'current_gate_placement', return_value=assignment),
                  mock.patch.object(first_step, 'admitted_run') as admit,
                  mock.patch.object(gate, 'run_tests_only', return_value=0) as run,
                  redirect_stderr(StringIO()) as errors):
                args = copies.CopyArguments(repo=directory)
                self.assertEqual(copies.run_copy(args, command, copies.CommittedSource("HEAD"), copies.NO_MERGE), 0)
            self.assertIn("A direct gate cannot take an AWS assignment", errors.getvalue())
        admit.assert_not_called()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[-1], 'aws')

    def test_a_gate_inside_a_copy_shell_requires_direct_placement(self) -> None:
        with TemporaryDirectory() as directory:
            tree = Path(directory)
            command = ["sh", "-c", "gate.py --tests-only"]

            def launch(arguments: Sequence[str], *, cwd: Path, pass_fds: tuple[int, ...],
                       env: dict[str, str], text: bool) -> subprocess.CompletedProcess[str]:
                self.assertEqual(arguments, command)
                self.assertEqual(cwd, tree)
                self.assertTrue(text)
                self.assertEqual(pass_fds, ())
                with mock.patch.dict(os.environ, env, clear=True):
                    status = gate.main(["--tests-only", "--repo", directory])
                return subprocess.CompletedProcess(arguments, status)

            with (mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "aws"}, clear=True),
                  mock.patch.object(copies, "caller_owner", return_value="fixture"),
                  mock.patch("scripts.copies.copies._command_copy",
                             return_value=nullcontext(copies.DirectCommandCopy(tree))),
                  mock.patch.object(subprocess, "run", side_effect=launch),
                  mock.patch.object(gate, "run_tests_only", return_value=0) as run,
                  mock.patch.object(first_step, "admitted_run") as admit,
                  redirect_stderr(StringIO()) as errors):
                args = copies.CopyArguments(repo=directory)
                self.assertEqual(copies.run_copy(args, command, copies.CommittedSource("HEAD"), copies.NO_MERGE), 1)
            self.assertIn("Direct gates require natedev or mac-linux", errors.getvalue())
            run.assert_not_called()
            admit.assert_not_called()

    def test_each_assignment_runs_its_route(self) -> None:
        result = gate.GateRun((), 1, 0, selection_certified=True)
        for target in ('natedev', 'mac-linux'):
            assignment = placed(target)
            with (self.subTest(target=target),
                  mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
                  mock.patch.object(conf, 'read', return_value=mock.Mock(value=mock.Mock(return_value=True))),
                  mock.patch.object(gate, 'run_suites', return_value=result) as local,
                  mock.patch.object(mac_linux, 'route', return_value=result) as guest,
                  mock.patch.object(cloud_gate, 'route', return_value=result) as aws):
                self.assertIs(gate.run_gate_suites(Path.cwd(), target=gate.placement_target(assignment)), result)
                self.assertEqual([local.call_count, guest.call_count, aws.call_count],
                                 [int(target == name) for name in ('natedev', 'mac-linux', 'aws')])

    def test_direct_gate_refuses_aws_as_its_only_target(self) -> None:
        with (mock.patch.dict(os.environ, {'BUILDLOG_GATE_TARGETS': 'aws'}, clear=True),
              mock.patch.object(first_step, 'admitted_run') as place,
              mock.patch.object(gate, 'run_tests_only') as run,
              redirect_stderr(StringIO()) as errors):
            self.assertEqual(gate.main(['--tests-only']), 1)
        self.assertIn('Direct gates require natedev or mac-linux', errors.getvalue())
        place.assert_not_called()
        run.assert_not_called()

    def test_direct_gate_refuses_an_aws_assignment(self) -> None:
        aws = placed('aws')
        for assigned_at_start in (True, False):
            assignments = (aws,) if assigned_at_start else (gate.NO_GATE_ASSIGNMENT, aws)
            admitted = mock.Mock(spec=first_step.FirstStepAdmission,
                                 grant=mock.Mock(spec=first_step.GatePlacement))
            with (self.subTest(assigned_at_start=assigned_at_start),
                  mock.patch.dict(os.environ, {'BUILDLOG_GATE_TARGETS': 'natedev'}, clear=True),
                  mock.patch.object(gate, 'current_gate_placement', side_effect=assignments),
                  mock.patch.object(first_step, 'admitted_run', return_value=nullcontext(admitted)),
                  mock.patch.object(gate, 'run_tests_only') as run,
                  redirect_stderr(StringIO()) as errors):
                self.assertEqual(gate.main(['--tests-only']), 1)
            self.assertIn('A direct gate cannot take an AWS assignment', errors.getvalue())
            run.assert_not_called()

    def test_a_standalone_named_module_stays_local_and_a_nested_module_keeps_its_parent(self) -> None:
        arguments = ['--tests-only', '--module', 'fixture/test_ok.py']
        parent = mock.Mock(spec=OuterReservation)
        for nested in (True, False):
            environment = {"BUILDLOG_GATE_TARGETS": "mac-linux"} if nested else {}
            with (self.subTest(nested=nested), mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch.object(gate, 'outer_reservation', return_value=parent if nested else None),
                  mock.patch.object(gate, 'current_gate_placement', return_value=gate.NO_GATE_ASSIGNMENT),
                  mock.patch.object(first_step, 'gate_targets', return_value=()) as targets,
                  mock.patch.object(first_step, 'admitted_run') as admit,
                  mock.patch.object(gate, 'run_tests_only', return_value=0) as run):
                self.assertEqual(gate.main(arguments), 0)
            admit.assert_not_called()
            self.assertEqual(run.call_args.args[-1], 'natedev')
            if nested:
                targets.assert_not_called()

    def test_named_module_selection_uses_the_exact_tree_and_has_no_pass_certificate(self) -> None:
        checked, run = self.run_named_guest()
        self.assertEqual(run.call_args.kwargs['selected'], gate.TreeSelection(('scripts/fixture/test_ok.py',), 'tree'))
        self.assertFalse(checked.selection_certified)
        self.assertIs(checked.coverage, ModuleCoverage.SUBSET)
        self.assertTrue(checked.passed)

    def test_named_guest_modules_use_the_configured_run_limit(self) -> None:
        _checked, run = self.run_named_guest()
        run.assert_called_once()
        self.assertIsNone(cast(object, run.call_args.kwargs["module_time_limit_s"]))

    def run_named_guest(self) -> tuple[gate.GateRun, mock.Mock]:
        assignment = placed("mac-linux")
        result = gate.GateRun((gate.GateJobResult("fixture", "test_ok.py", 0, 1, ""),), 1, 0,
                              selection_certified=True)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "config"
            _ = write_config_fixture(settings / "build_targets.conf", "enabled.mac-linux=on\n")
            with (mock.patch.dict(os.environ, config_environment(settings)),
                  mock.patch.object(cloud_gate, "working_tree", return_value=("tree", set())),
                  mock.patch.object(gate, "checked_git", return_value="fixture-commit"),
                  mock.patch.object(mac_linux, "route", return_value=result) as run):
                checked = gate.run_gate_suites(root, mode=gate.SuiteMode.TARGETED, module_time_limit_s=30,
                    target=gate.placement_target(assignment), named=("fixture/test_ok.py",))
        return checked, run

    def test_nightly_types_follow_tool_availability(self) -> None:
        result = gate.GateRun((), 1, 0, selection_certified=True)
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'scripts').mkdir()
            for target, tools in (('natedev', False), ('mac-linux', True), ('mac-linux', False)):
                assignment = placed(target)
                with (self.subTest(target=target, tools=tools),
                      mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
                      mock.patch.object(gate, 'checked_git', return_value='fixture-commit'),
                      mock.patch.object(conf, 'read', return_value=mock.Mock(value=mock.Mock(return_value=True))),
                      mock.patch.object(gate, 'guest_has_type_tools', return_value=tools),
                      mock.patch.object(mac_linux, 'route', return_value=result) as guest,
                      mock.patch.object(cloud_gate, 'route', return_value=result) as aws,
                      mock.patch.object(gate, 'run_suites', return_value=result) as local):
                    run = gate.run_gate_suites(repo, ('scripts',), full=True, nightly_types=True,
                                               target=gate.placement_target(assignment))
                self.assertTrue(run.passed)
                self.assertEqual(local.call_count, int(target != 'mac-linux' or not tools))
                if local.called:
                    self.assertEqual(local.call_args.args[1], ('scripts',))
                    if target != 'natedev':
                        self.assertEqual(local.call_args.kwargs['selected'], gate.SelectedModules(()))
                if target == 'mac-linux':
                    self.assertEqual(guest.call_args.kwargs['type_dirs'], ('scripts',) if tools else ())
                aws.assert_not_called()

    def test_nightly_places_the_fetched_checkout_before_running_suites(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            clone = repo / 'nightly'
            clone.mkdir()
            command = ('gate.py', '--nightly', '--merge-branch', 'main', '--unit', 'fixture')
            args = gate.GateArguments(repo=str(repo), merge_branch='main', unit='fixture', command=command)
            assignment = placed('mac-linux')
            admitted = mock.Mock(spec=first_step.FirstStepAdmission, grant=mock.Mock(spec=first_step.GatePlacement))
            with (mock.patch.object(gate, 'nightly_git'), mock.patch.object(gate, 'checked_git', return_value='tree'),
                  mock.patch.object(gate, 'nightly_owner', return_value='fixture'),
                  mock.patch.object(copies, 'standing', return_value=nullcontext(clone)),
                  mock.patch.object(gate, 'gate_state_dir', return_value=repo),
                  mock.patch.object(gate, 'push_nightly_result') as publish,
                  mock.patch.object(first_step, 'gate_targets', return_value=('mac-linux', 'natedev')),
                  mock.patch.object(first_step, 'admitted_run', return_value=nullcontext(admitted)) as admit,
                  mock.patch.object(gate, 'current_gate_placement', return_value=assignment),
                  mock.patch.object(gate, 'run_gate_suites', return_value=gate.GateRun((), 0, 0, tests_reused=True)) as run):
                self.assertEqual(gate.nightly(args, gate.NO_GATE_ASSIGNMENT), 0)
            admit.assert_called_once_with(command, clone, targets=('mac-linux', 'natedev'))
            self.assertEqual(run.call_args.args[0], clone)
            self.assertEqual(run.call_args.kwargs['target'], gate.MacLinuxAssignment(assignment.assignment))
            publish.assert_called_once()

    def test_prepared_guest_no_run_ends_with_79_without_replacement(self) -> None:
        self.check_prepared_guest_refusal(mac_linux.GuestNotStarted('fixture no run'), 79)

    def test_prepared_guest_unknown_result_ends_with_80_without_replacement(self) -> None:
        self.check_prepared_guest_refusal(GateResultUnknown('fixture unknown result'), 80)

    def check_prepared_guest_refusal(self, outcome: mac_linux.GuestNotStarted | GateResultUnknown, status: int) -> None:
        assignment = placed('mac-linux')

        @contextmanager
        def recovery(_repo: Path) -> Generator[None]:
            try:
                yield
            finally:
                raise OSError('fixture recovery cleanup failed')

        with TemporaryDirectory() as directory:
            repo = Path(directory).resolve()
            with (mock.patch.dict(os.environ, {'BUILDLOG_PREPARED_COPY': str(repo),
                                              'BUILDLOG_PLACEMENT_FILE': str(repo / 'assignment')}),
                  mock.patch.object(copies, 'take_prepared_gate_handoff', return_value=True),
                  mock.patch.object(first_step, 'admitted_run') as place,
                  mock.patch.object(gate, 'current_gate_placement', return_value=assignment),
                  mock.patch.object(gate, 'test_run_folder', return_value=nullcontext()),
                  mock.patch.object(gate, 'gate_recovery', side_effect=recovery),
                  mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
                  mock.patch.object(copies, 'source_owner', return_value=''),
                  mock.patch.object(conf, 'read', return_value=mock.Mock(value=mock.Mock(return_value=True))),
                  mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
                  mock.patch.object(mac_linux, 'route') as guest,
                  mock.patch.object(gate, 'run_suites') as local,
                  mock.patch.object(cloud_gate, 'route') as aws,
                  redirect_stderr(StringIO()) as errors):
                if isinstance(outcome, GateResultUnknown):
                    guest.side_effect = outcome
                else:
                    guest.return_value = outcome
                arguments = ['--affected', '--full', '--repo', str(repo)]
                self.assertEqual(gate.main(arguments), status)
            place.assert_not_called()
            self.assertIn('fixture recovery cleanup failed', errors.getvalue())
        guest.assert_called_once()
        local.assert_not_called()
        aws.assert_not_called()

    def test_promote_only_skips_placement(self) -> None:
        with (mock.patch.dict(os.environ, clear=True),
              mock.patch.object(first_step, 'admitted_run') as admit,
              mock.patch.object(gate, 'run_gate_command', return_value=0) as run):
            self.assertEqual(gate.main(['--promote-only', '--merge-branch', 'main']), 0)
        self.assertEqual(run.call_args.args[-1], 'natedev')
        admit.assert_not_called()

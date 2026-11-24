"""Route gate commands through the queue assignment."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager, nullcontext, redirect_stderr
import io
import os
from pathlib import Path
import signal
import subprocess
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_client, admission_wire as wire, execution_identity, first_step, placement, queue_place
from scripts.build_queue.admission_client import OuterReservation
from scripts.build_queue.queue_fixture import write_memory_config
from scripts.build_targets import mac_linux
from scripts.buildlog import treekey
from scripts.copies import copies
from scripts.lib import build_folders, conf, scratch
from scripts.lib.build_slots import SlotQueuePosition
from scripts.lib.build_steps import CLAIM_FAILURE_SUFFIX, GATE_PRELAUNCH_REFUSED_STATUS, GATE_UNSETTLED_LAUNCH_STATUS
from scripts.mac_test.claim_state import WHOLE_MAC
from scripts.production import gate
from scripts.production.gate_routes import GateResultUnknown, ModuleCoverage


def placed(target: str) -> wire.Assignment:
    provider = 'scaler' if target == 'aws' else 'ssh' if target == 'mac-linux' else 'local'
    return wire.Assignment(placement.PlacementAssignment('episode', 'attempt',
        placement.PlacementDestination(provider, target, 'fixture-host', WHOLE_MAC),
        execution_identity.SourceKeyKnown('fixture-source'), placement.SubmittingOwner('fixture-host', 1, '1'),
        NoOwningSession(), 1.0))


def inherited_admission(grant: wire.Assignment) -> first_step.FirstStepAdmission:
    assignment = grant.assignment
    return first_step.FirstStepAdmission(SlotQueuePosition(assignment.episode, assignment.since),
        first_step.InheritedGatePlacement(grant), Path('/fixture/assignment.json'))


class GateDefaultPlacementTests(unittest.TestCase):
    def test_sigterm_releases_each_direct_placement_before_termination(self) -> None:
        merge = ['--merge-branch', 'main', '--unit-branch', 'fixture', '--checkpoint', 'tip',
                 '--previous-checkpoint', 'base', '--unit', 'fixture', '--phase', '1']
        nightly = ['--nightly', '--merge-branch', 'main', '--unit', 'fixture']
        for arguments in (['--affected'], ['--tests-only'], nightly, merge):
            with self.subTest(arguments=arguments):
                events: list[str] = []

                @contextmanager
                def admit(*_args: object, **_options: object) -> Generator[first_step.FirstStepAdmission]:
                    events.append('admit')
                    try:
                        yield inherited_admission(placed('natedev'))
                    finally:
                        events.append('release')

                def interrupt(*_args: object) -> int:
                    handler = signal.getsignal(signal.SIGTERM)
                    assert callable(handler)
                    events.append('interrupt')
                    handler(signal.SIGTERM, None)
                    raise AssertionError('SIGTERM did not interrupt the gate')

                def interrupt_placed(args: gate.GateArguments, target: gate.GateTarget) -> int:
                    with gate.gate_suite_target(args, Path.cwd(), target):
                        return interrupt()

                def terminate(number: int) -> None:
                    self.assertEqual(number, signal.SIGTERM)
                    self.assertEqual(signal.getsignal(number), signal.SIG_DFL)
                    events.append('terminate')

                with (mock.patch.dict(os.environ, clear=True),
                      mock.patch.object(first_step, 'gate_targets', return_value=('natedev',)),
                      mock.patch.object(first_step, 'admitted_run', side_effect=admit),
                      mock.patch.object(treekey, 'tree_key', return_value='fixture-source'),
                      mock.patch.object(gate, 'current_gate_placement',
                                        side_effect=[gate.NO_GATE_ASSIGNMENT, placed('natedev')]),
                      mock.patch.object(gate, 'run_tests_only', side_effect=interrupt),
                      mock.patch.object(gate, 'merge_checkpoint', side_effect=interrupt_placed),
                      mock.patch.object(gate, 'nightly', side_effect=interrupt_placed),
                      mock.patch.object(gate, '_nightly_unit', return_value='fixture'),
                      mock.patch.object(gate, '_sweep_nightly_build_folders'),
                      mock.patch.object(gate, 'test_run_folder', return_value=nullcontext()),
                      mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
                      mock.patch.object(gate, 'gate_recovery', return_value=nullcontext()),
                      mock.patch.object(signal, 'raise_signal', side_effect=terminate),
                      self.assertRaises(SystemExit) as exited):
                    _ = gate.main(arguments)
                self.assertEqual(exited.exception.code, 128 + signal.SIGTERM)
                self.assertEqual(events, ['admit', 'interrupt', 'release', 'terminate'])

    def test_gate_step_preserves_incomplete_child_status_when_cancellation_fails(self) -> None:
        for status in (GATE_PRELAUNCH_REFUSED_STATUS, GATE_UNSETTLED_LAUNCH_STATUS):
            with self.subTest(status=status), TemporaryDirectory() as folder:
                source = Path(folder)
                command = ["gate.py", "--tests-only"]
                request = mock.Mock(spec=wire.PlacementRequest, local=wire.LocalAdmissionUnavailable("fixture"))
                with (mock.patch.dict(os.environ, clear=True),
                      mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(source)),
                      mock.patch.object(first_step, "_gate_request", return_value=request),
                      mock.patch.object(first_step, "_placement_grant", return_value=placed("mac-linux")),
                      mock.patch.object(first_step, "assignment_matches_request", return_value=True),
                      mock.patch.object(admission_client, "request_settlement"),
                      mock.patch.object(admission_client, "cancel_placement", side_effect=wire.AdmissionUnavailable(
                          "fixture queue unavailable")) as cancel,
                      mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(command, status)),
                      redirect_stderr(io.StringIO()) as errors):
                    result = copies.main(["gate-step", "--repo", folder, "--targets", "mac-linux", "--", *command])
                self.assertEqual(result, status)
                self.assertIn("Gate cleanup failed. fixture queue unavailable\n", errors.getvalue())
                cancel.assert_called_once()

    def test_gate_step_hands_off_its_aws_assignment(self) -> None:
        with TemporaryDirectory() as folder:
            source = Path(folder)
            command = ["gate.py", "--tests-only", "--repo", folder]
            assignment = placed("aws")
            admitted = inherited_admission(assignment)

            def launch(arguments: Sequence[str], *, cwd: Path, pass_fds: tuple[int, ...], env: dict[str, str],
                       text: bool, check: bool) -> subprocess.CompletedProcess[str]:
                self.assertEqual((arguments, cwd, text, check), (command, source, True, False))
                reader, = pass_fds
                inherited = {**env, copies.PREPARED_GATE_FD: str(os.dup(reader))}
                with mock.patch.dict(os.environ, inherited, clear=True):
                    status = gate.main(command[1:])
                return subprocess.CompletedProcess(arguments, status)

            with (mock.patch.dict(os.environ, clear=True),
                  mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)),
                  mock.patch.object(gate, "current_gate_placement", return_value=assignment),
                  mock.patch.object(gate, "run_tests_only", return_value=0) as run,
                  mock.patch.object(subprocess, "run", side_effect=launch)):
                args = copies.CopyArguments(repo=folder, targets="aws")
                self.assertEqual(copies.run_gate_step(args, command), 0)
            self.assertEqual(run.call_args.args[-1], gate.AwsGateAssignment(assignment.assignment))

    def test_default_modes_enter_one_admission_for_the_whole_command(self) -> None:
        checkpoint = ['--merge-branch', 'main', '--unit-branch', 'fixture', '--checkpoint', 'tip',
                      '--previous-checkpoint', 'base', '--unit', 'fixture', '--phase', '1']
        for arguments in (checkpoint, ['--affected'], ['--tests-only']):
            with self.subTest(arguments=arguments):
                calls: list[str] = []

                @contextmanager
                def admit(command: Sequence[str], source: Path, *, targets: tuple[str, ...]) -> Generator[first_step.FirstStepAdmission]:
                    self.assertEqual(list(command), ['gate.py', *arguments])
                    self.assertEqual(source, Path.cwd())
                    self.assertEqual(targets, ('natedev', 'mac-linux'))
                    calls.append('admit')
                    try:
                        yield inherited_admission(placed('natedev'))
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
                      mock.patch.object(treekey, 'tree_key', return_value='fixture-source'),
                      mock.patch.object(gate, 'current_gate_placement',
                                        side_effect=[gate.NO_GATE_ASSIGNMENT, placed('natedev')]),
                      mock.patch.object(gate, 'run_tests_only', side_effect=run),
                      mock.patch.object(gate, 'merge_checkpoint', side_effect=merge),
                      mock.patch.object(gate, 'test_run_folder', return_value=nullcontext()),
                      mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
                      mock.patch.object(gate, 'gate_recovery', return_value=nullcontext())):
                    self.assertEqual(gate.main(arguments), 0)
                self.assertEqual(calls, ['admit', 'run', 'release'])

    def test_default_gate_uses_the_real_placement_request_builder(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            write_memory_config(repo / "build_admission.conf", **{"program_first_peak_gib.gate": "4"})
            _ = (repo / "build_targets.conf").write_text(
                "targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n")

            def place(request: wire.PlacementRequest, _reason: str) -> wire.Assignment:
                destination, = (item.destination for item in request.constraints.candidates)
                self.assertEqual(destination.target, "natedev")
                self.assertEqual(destination.provider, "local")
                self.assertEqual(request.constraints.command, ("gate.py", "--tests-only"))
                return wire.Assignment(placement.PlacementAssignment(request.episode, "attempt", destination,
                    request.constraints.source_key, request.owner, request.constraints.session, request.since))

            def run(_args: gate.GateArguments, _mode: gate.SuiteMode, target: gate.GateTarget) -> int:
                self.assertEqual(target, "natedev")
                assignment = wire.read_assignment(repo / "assignment.json").assignment
                self.assertEqual(os.environ["BUILDLOG_PLACEMENT_EPISODE"], assignment.episode)
                return 0

            def status(_episode: str, _attempt: str) -> wire.PlacementStatusReply:
                assignment = wire.read_assignment(repo / "assignment.json").assignment
                return wire.PlacementStatusReply(wire.KnownPlacementStatus(
                    assignment, placement.CurrentAssignment(), placement.NoProviderClaim()))

            with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"], "CONF_DIR": directory, "HOME": directory,
                                               "CLAUDE_SESSION_NAME": "gate-fixture"}, clear=True),
                  mock.patch.object(build_folders, "repo_word", return_value="claude"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(repo)),
                  mock.patch.object(treekey, "tree_key", return_value="fixture-source"),
                  mock.patch.object(admission_client, "placement_status", side_effect=status),
                  mock.patch.object(queue_place, "wait_for_placement", side_effect=place) as request,
                  mock.patch.object(admission_client, "cancel_placement") as close,
                  mock.patch.object(gate, "run_tests_only", side_effect=run)):
                self.assertEqual(gate.main(["--tests-only"]), 0)
            request.assert_called_once()
            close.assert_called_once()

    def test_aws_gate_exit_records_only_the_confirmed_prelaunch_decline(self) -> None:
        assignment = placed("aws")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "assignment.json"
            failure_path = path.with_name(path.name + CLAIM_FAILURE_SUFFIX)
            for error, status in ((gate.AwsGateLaunchPending("launch unknown"), GATE_UNSETTLED_LAUNCH_STATUS),
                                  (gate.AwsGateTookNoRun("capacity"), GATE_PRELAUNCH_REFUSED_STATUS)):
                with (self.subTest(status=status),
                      mock.patch.dict(os.environ, {"BUILDLOG_PLACEMENT_FILE": str(path), "BUILDLOG_PREPARED_COPY": str(Path.cwd())}),
                      mock.patch.object(gate, "current_gate_placement", return_value=assignment),
                      mock.patch.object(copies, "take_prepared_gate_handoff", return_value=True),
                      mock.patch.object(gate, "gate_recovery", return_value=nullcontext()),
                      mock.patch.object(scratch, "copy_lock", return_value=nullcontext()),
                      mock.patch.object(copies, "source_owner", return_value=""),
                      mock.patch.object(cloud_gate, "working_tree", return_value=("tree", set())),
                      mock.patch.object(cloud_gate, "route", side_effect=error),
                      mock.patch.object(gate, "run_suites") as local):
                    self.assertEqual(gate.main(["--tests-only"]), status)
                local.assert_not_called()
                if status == GATE_UNSETTLED_LAUNCH_STATUS:
                    self.assertFalse(failure_path.exists())
                else:
                    failure = wire.parse_request(failure_path.read_bytes())
                    assert isinstance(failure, wire.ClaimFailure), failure
                    self.assertEqual(failure.assignment, assignment.assignment)
                    self.assertEqual(failure.claim, placement.assigned_claim_reference(assignment.assignment))
                    self.assertEqual(failure.reason, "capacity")

    def test_wrapped_gate_reuses_the_placement_file(self) -> None:
        assignment = placed('mac-linux')
        with (mock.patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': '/fixture/assignment.json'}),
              mock.patch.object(gate, 'current_gate_placement', return_value=assignment),
              mock.patch.object(first_step, 'admitted_run') as admit,
              mock.patch.object(gate, 'run_tests_only', return_value=0) as run):
            self.assertEqual(gate.main(['--tests-only']), 0)
        admit.assert_not_called()
        self.assertEqual(run.call_args.args[-1], gate.MacLinuxAssignment(assignment.assignment))

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
                  redirect_stderr(io.StringIO()) as errors):
                args = copies.CopyArguments(repo=directory)
                self.assertEqual(copies.run_copy(args, command, copies.CommittedSource("HEAD"), copies.NO_MERGE), 0)
            self.assertIn("A direct gate cannot take an AWS assignment", errors.getvalue())
        admit.assert_not_called()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[-1], gate.AwsGateAssignment(assignment.assignment))

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
                  redirect_stderr(io.StringIO()) as errors):
                args = copies.CopyArguments(repo=directory)
                self.assertEqual(copies.run_copy(args, command, copies.CommittedSource("HEAD"), copies.NO_MERGE), 1)
            self.assertIn("Direct gates require natedev or mac-linux", errors.getvalue())
            run.assert_not_called()
            admit.assert_not_called()

    def test_each_assignment_runs_its_route(self) -> None:
        result = gate.GateRun((), 1, 0, selection_certified=True)
        for target in ('natedev', 'mac-linux', 'aws'):
            assignment = placed(target)
            with (self.subTest(target=target),
                  mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
                  mock.patch.object(gate, 'checked_git', return_value='fixture-commit'),
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
              redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(gate.main(['--tests-only']), 1)
        self.assertIn('Direct gates require natedev or mac-linux', errors.getvalue())
        place.assert_not_called()
        run.assert_not_called()

    def test_direct_gate_refuses_an_aws_assignment(self) -> None:
        with (mock.patch.dict(os.environ, clear=True),
              mock.patch.object(gate, 'current_gate_placement', return_value=placed('aws')),
              mock.patch.object(gate, 'run_tests_only') as run, redirect_stderr(io.StringIO()) as errors):
            self.assertEqual(gate.main(['--tests-only']), 1)
        self.assertIn('A direct gate cannot take an AWS assignment', errors.getvalue())
        run.assert_not_called()

    def test_a_standalone_named_module_stays_local_and_a_nested_module_keeps_its_parent(self) -> None:
        arguments = ['--tests-only', '--module', 'fixture/test_ok.py']
        parent = mock.Mock(spec=OuterReservation)
        for nested in (True, False):
            with (self.subTest(nested=nested), mock.patch.dict(os.environ, clear=True),
                  mock.patch.object(gate, 'outer_reservation', return_value=parent if nested else None),
                  mock.patch.object(gate, 'current_gate_placement', return_value=gate.NO_GATE_ASSIGNMENT),
                  mock.patch.object(first_step, 'gate_targets', return_value=()),
                  mock.patch.object(first_step, 'admitted_run') as admit,
                  mock.patch.object(gate, 'run_tests_only', return_value=0) as run):
                self.assertEqual(gate.main(arguments), 0)
            admit.assert_not_called()
            self.assertEqual(run.call_args.args[-1], 'natedev')

    def test_aws_failure_exits_nonzero_without_local_tests(self) -> None:
        assignment = placed('aws')
        with (mock.patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': '/fixture/assignment.json', 'BUILDLOG_PREPARED_COPY': str(Path.cwd())}),
              mock.patch.object(gate, 'current_gate_placement', return_value=assignment),
              mock.patch.object(copies, 'take_prepared_gate_handoff', return_value=True),
              mock.patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
              mock.patch.object(copies, 'source_owner', return_value=''),
              mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
              mock.patch.object(cloud_gate, 'route', return_value='AWS fixture failed') as aws,
              mock.patch.object(gate, 'run_suites') as local):
            self.assertEqual(gate.main(['--tests-only']), 1)
        aws.assert_called_once()
        local.assert_not_called()

    def test_a_standalone_named_module_uses_placement_and_a_nested_module_keeps_its_parent(self) -> None:
        arguments = ['--tests-only', '--module', 'fixture/test_ok.py']
        parent = mock.Mock(spec=OuterReservation)
        for nested in (True, False):
            with (self.subTest(nested=nested), mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "natedev"}, clear=True),
                  mock.patch.object(treekey, 'tree_key', return_value='fixture-source'),
                  mock.patch.object(gate, 'outer_reservation', return_value=parent if nested else None),
                  mock.patch.object(gate, 'current_gate_placement', side_effect=[gate.NO_GATE_ASSIGNMENT, placed('natedev')]),
                  mock.patch.object(first_step, 'admitted_run', return_value=nullcontext(
                      inherited_admission(placed('natedev')))) as admit,
                  mock.patch.object(gate, 'run_tests_only', return_value=0) as run):
                self.assertEqual(gate.main(arguments), 0)
            self.assertEqual(admit.call_count, int(not nested))
            target = cast(gate.GateTarget, run.call_args.args[-1])
            self.assertEqual(target, 'natedev')

    def test_standalone_named_gate_keeps_exit_80_when_cancel_fails(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            write_memory_config(repo / 'build_admission.conf', **{'program_first_peak_gib.gate': '4'})
            _ = (repo / 'build_targets.conf').write_text(
                'targets=natedev\nkind.natedev=local\nplatform.natedev=linux\n')

            def place(request: wire.PlacementRequest, _reason: str) -> wire.Assignment:
                destination, = (item.destination for item in request.constraints.candidates)
                return wire.Assignment(placement.PlacementAssignment(request.episode, 'attempt', destination,
                    request.constraints.source_key, request.owner, request.constraints.session, request.since))

            arguments = ['--tests-only', '--module', 'fixture/test_ok.py']
            with (mock.patch.dict(os.environ, {'PATH': os.environ['PATH'], 'CONF_DIR': directory, 'HOME': directory,
                                               'CLAUDE_SESSION_NAME': 'gate-fixture', 'BUILDLOG_GATE_TARGETS': 'natedev'}, clear=True),
                  mock.patch.object(build_folders, 'repo_word', return_value='claude'),
                  mock.patch.object(build_folders, 'tool_folder', return_value=nullcontext(repo)),
                  mock.patch.object(treekey, 'tree_key', return_value='fixture-source'),
                  mock.patch.object(gate, 'outer_reservation', return_value=None),
                  mock.patch.object(queue_place, 'wait_for_placement', side_effect=place),
                  mock.patch.object(admission_client, 'cancel_placement',
                                    side_effect=wire.AdmissionUnavailable('The queue is unavailable')) as cancel,
                  mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
                  mock.patch.object(gate, 'gate_recovery', return_value=nullcontext()),
                  mock.patch.object(copies, 'source_owner', return_value=''),
                  mock.patch.object(gate, 'run_gate_suites', side_effect=GateResultUnknown('fixture')) as run):
                self.assertEqual(gate.main(arguments), GATE_UNSETTLED_LAUNCH_STATUS)
            run.assert_called_once()
            cancel.assert_called_once()

    def test_named_module_selection_uses_the_exact_tree_and_has_no_pass_certificate(self) -> None:
        for name, route in (('aws', cloud_gate), ('mac-linux', mac_linux)):
            assignment = placed(name)
            result = gate.GateRun((gate.GateJobResult('fixture', 'test_ok.py', 0, 1, ''),), 1, 0,
                                  selection_certified=True)
            with (self.subTest(name=name), mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
                  mock.patch.object(gate, 'checked_git', return_value='fixture-commit'),
                  mock.patch.object(conf, 'read', return_value=mock.Mock(value=mock.Mock(return_value=True))),
                  mock.patch.object(route, 'route', return_value=result) as run):
                checked = gate.run_gate_suites(Path.cwd(), mode=gate.SuiteMode.TARGETED,
                    target=gate.placement_target(assignment), named=('fixture/test_ok.py',))
            self.assertEqual(run.call_args.kwargs['selected'], gate.TreeSelection(('scripts/fixture/test_ok.py',), 'tree'))
            self.assertFalse(checked.selection_certified)
            self.assertIs(checked.coverage, ModuleCoverage.SUBSET)
            self.assertTrue(checked.passed)

    def test_nightly_types_follow_tool_availability(self) -> None:
        result = gate.GateRun((), 1, 0, selection_certified=True)
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / 'scripts').mkdir()
            for target, tools in (('natedev', False), ('mac-linux', True), ('mac-linux', False), ('aws', False)):
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
                self.assertEqual(aws.call_count, int(target == 'aws'))

    def test_nightly_places_the_fetched_checkout_before_running_suites(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            clone = repo / 'nightly'
            clone.mkdir()
            command = ('gate.py', '--nightly', '--merge-branch', 'main', '--unit', 'fixture')
            args = gate.GateArguments(repo=str(repo), merge_branch='main', unit='fixture', command=command)
            assignment = placed('mac-linux')
            admitted = inherited_admission(assignment)
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

    def test_nightly_keeps_placement_status_through_cleanup(self) -> None:
        with TemporaryDirectory() as directory:
            repo = Path(directory)
            arguments = ['--nightly', '--merge-branch', 'main', '--unit', 'fixture', '--repo', directory]
            refused = gate.MacLinuxTookNoRun('fixture guest took no run')
            pending = gate.GateSettlementPending('fixture guest end remains unproven')
            cases = ((refused, GATE_PRELAUNCH_REFUSED_STATUS, ''),
                     (refused, GATE_PRELAUNCH_REFUSED_STATUS, 'copy'),
                     (pending, GATE_UNSETTLED_LAUNCH_STATUS, 'lock'))

            @contextmanager
            def scope(name: str, failed: str) -> Generator[Path]:
                try:
                    yield repo
                finally:
                    if name == failed:
                        raise OSError(f'fixture {name} cleanup failed')

            for error, status, failed in cases:
                with (self.subTest(status=status, failed=failed),
                      mock.patch.object(gate, 'checked_gate_target', return_value=gate.placement_target(placed('mac-linux'))),
                      mock.patch.object(gate, 'nightly_git'), mock.patch.object(gate, 'checked_git', return_value='tree'),
                      mock.patch.object(gate, 'nightly_owner', return_value='fixture'),
                      mock.patch.object(copies, 'standing', return_value=scope('copy', failed)),
                      mock.patch.object(scratch, 'copy_lock', return_value=scope('lock', failed)),
                      mock.patch.object(gate, '_sweep_nightly_build_folders'),
                      mock.patch.object(gate, 'gate_state_dir', return_value=repo),
                      mock.patch.object(gate, 'nightly_red_lock', side_effect=AssertionError('unexpected nightly red')),
                      mock.patch.object(gate, 'push_nightly_result') as publish,
                      mock.patch.object(gate, 'run_gate_suites', side_effect=error),
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(gate.main(arguments), status, errors.getvalue())
                    self.assertIn(str(error), errors.getvalue())
                    if failed:
                        self.assertIn(f'fixture {failed} cleanup failed', errors.getvalue())
                    publish.assert_called_once()
                    published = cast(GateResultUnknown | gate.MacLinuxTookNoRun, publish.call_args.args[1])
                    self.assertEqual(published.status, status)
                    self.assertIn(str(error), str(published))
                    self.assertFalse((repo / 'full-required').exists())

    def test_direct_guest_no_run_ends_with_79_without_replacement(self) -> None:
        self.check_direct_guest_refusal(mac_linux.GuestNotStarted('fixture no run'), 79)

    def test_direct_guest_unknown_result_ends_with_80_without_replacement(self) -> None:
        self.check_direct_guest_refusal(GateResultUnknown('fixture unknown result'), 80)

    def check_direct_guest_refusal(self, outcome: mac_linux.GuestNotStarted | GateResultUnknown, status: int) -> None:
        assignment = placed('mac-linux')
        targets = ('mac-linux', 'natedev')
        admitted = inherited_admission(assignment)

        @contextmanager
        def recovery(_repo: Path) -> Generator[None]:
            try:
                yield
            finally:
                raise OSError('fixture recovery cleanup failed')

        with TemporaryDirectory() as directory:
            repo = Path(directory)
            admitted.assignment_file = repo / 'assignment'
            with (mock.patch.dict(os.environ, clear=True),
                  mock.patch.object(first_step, 'gate_targets', return_value=targets),
                  mock.patch.object(first_step, 'admitted_run', return_value=nullcontext(admitted)) as place,
                  mock.patch.object(gate, 'current_gate_placement', return_value=gate.NO_GATE_ASSIGNMENT),
                  mock.patch.object(treekey, 'tree_key', return_value='fixture-source'),
                  mock.patch.object(gate, 'test_run_folder', return_value=nullcontext()),
                  mock.patch.object(gate, 'gate_recovery', side_effect=recovery),
                  mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
                  mock.patch.object(copies, 'source_owner', return_value=''),
                  mock.patch.object(conf, 'read', return_value=mock.Mock(value=mock.Mock(return_value=True))),
                  mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
                  mock.patch.object(mac_linux, 'route') as guest,
                  mock.patch.object(gate, 'run_suites') as local,
                  mock.patch.object(cloud_gate, 'route') as aws,
                  redirect_stderr(io.StringIO()) as errors):
                if isinstance(outcome, GateResultUnknown):
                    guest.side_effect = outcome
                else:
                    guest.return_value = outcome
                arguments = ['--affected', '--full', '--repo', directory]
                self.assertEqual(gate.main(arguments), status)
            place.assert_called_once_with(('gate.py', *arguments), repo, targets=targets)
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

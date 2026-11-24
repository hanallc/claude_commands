"""Route gate commands through the queue assignment."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager, nullcontext, redirect_stderr
from io import StringIO
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_wire as wire, execution_identity, first_step, placement
from scripts.build_queue.admission_client import OuterReservation
from scripts.build_targets import mac_linux
from scripts.copies import copies
from scripts.lib import conf, scratch
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
              mock.patch.object(first_step, 'gate_targets', return_value=('natedev', 'mac-linux', 'aws')),
              mock.patch.object(first_step, 'admitted_run', side_effect=admit),
              mock.patch.object(gate, 'current_gate_placement',
                                side_effect=[gate.NO_GATE_ASSIGNMENT, placed('aws')]),
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
        for arguments in (checkpoint, ['--affected'], ['--tests-only']):
            with self.subTest(arguments=arguments):
                calls: list[str] = []

                @contextmanager
                def admit(command: Sequence[str], source: Path, *, targets: tuple[str, ...]) -> Generator[object]:
                    self.assertEqual(list(command), ['gate.py', *arguments])
                    self.assertEqual(source, Path.cwd())
                    self.assertEqual(targets, ('natedev', 'mac-linux', 'aws'))
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

                with (mock.patch.dict(os.environ, clear=True),
                      mock.patch.object(first_step, 'gate_targets', return_value=('natedev', 'mac-linux', 'aws')),
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

    def test_wrapped_gate_reuses_the_placement_file(self) -> None:
        assignment = placed('aws')
        with (mock.patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': '/fixture/assignment.json'}),
              mock.patch.object(gate, 'current_gate_placement', return_value=assignment),
              mock.patch.object(first_step, 'admitted_run') as admit,
              mock.patch.object(gate, 'run_tests_only', return_value=0) as run):
            self.assertEqual(gate.main(['--tests-only']), 0)
        admit.assert_not_called()
        self.assertEqual(run.call_args.args[-1], gate.AwsGateAssignment(assignment.assignment))

    def test_each_assignment_runs_its_route(self) -> None:
        result = gate.GateRun((), 1, 0, selection_certified=True)
        for target in ('natedev', 'mac-linux', 'aws'):
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

    def test_aws_failure_exits_nonzero_without_local_tests(self) -> None:
        assignment = placed('aws')
        with (mock.patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': '/fixture/assignment.json'}),
              mock.patch.object(gate, 'current_gate_placement', return_value=assignment),
              mock.patch.object(gate, 'gate_recovery', return_value=nullcontext()),
              mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
              mock.patch.object(copies, 'source_owner', return_value=''),
              mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
              mock.patch.object(cloud_gate, 'route', return_value='AWS fixture failed') as aws,
              mock.patch.object(gate, 'run_suites') as local):
            self.assertEqual(gate.main(['--tests-only']), 1)
        aws.assert_called_once()
        local.assert_not_called()

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

    def test_named_module_selection_uses_the_exact_tree_and_has_no_pass_certificate(self) -> None:
        for name, route in (('aws', cloud_gate), ('mac-linux', mac_linux)):
            assignment = placed(name)
            result = gate.GateRun((gate.GateJobResult('fixture', 'test_ok.py', 0, 1, ''),), 1, 0,
                                  selection_certified=True)
            with (self.subTest(name=name), mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
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
            admitted = mock.Mock(spec=first_step.FirstStepAdmission, grant=mock.Mock(spec=first_step.GatePlacement))
            with (mock.patch.object(gate, 'nightly_git'), mock.patch.object(gate, 'checked_git', return_value='tree'),
                  mock.patch.object(gate, 'nightly_owner', return_value='fixture'),
                  mock.patch.object(copies, 'standing', return_value=nullcontext(clone)),
                  mock.patch.object(gate, 'gate_state_dir', return_value=repo),
                  mock.patch.object(gate, 'push_nightly_result') as publish,
                  mock.patch.object(first_step, 'gate_targets', return_value=('aws',)),
                  mock.patch.object(first_step, 'admitted_run', return_value=nullcontext(admitted)) as admit,
                  mock.patch.object(gate, 'current_gate_placement', return_value=assignment),
                  mock.patch.object(gate, 'run_gate_suites', return_value=gate.GateRun((), 0, 0, tests_reused=True)) as run):
                self.assertEqual(gate.nightly(args, gate.NO_GATE_ASSIGNMENT), 0)
            admit.assert_called_once_with(command, clone, targets=('aws',))
            self.assertEqual(run.call_args.args[0], clone)
            self.assertEqual(run.call_args.kwargs['target'], gate.MacLinuxAssignment(assignment.assignment))
            publish.assert_called_once()

    def test_direct_guest_no_run_ends_with_79_without_replacement(self) -> None:
        self.check_direct_guest_refusal(mac_linux.GuestNotStarted('fixture no run'), 79)

    def test_direct_guest_unknown_result_ends_with_80_without_replacement(self) -> None:
        self.check_direct_guest_refusal(GateResultUnknown('fixture unknown result'), 80)

    def check_direct_guest_refusal(self, outcome: mac_linux.GuestNotStarted | GateResultUnknown, status: int) -> None:
        assignment = placed('mac-linux')
        targets = ('mac-linux', 'natedev', 'aws')
        admitted = mock.Mock(spec=first_step.FirstStepAdmission, grant=mock.Mock(spec=first_step.GatePlacement))

        @contextmanager
        def recovery(_repo: Path) -> Generator[None]:
            try:
                yield
            finally:
                raise OSError('fixture recovery cleanup failed')

        with TemporaryDirectory() as directory:
            repo = Path(directory)
            with (mock.patch.dict(os.environ, {'BUILDLOG_PLACEMENT_FILE': str(repo / 'assignment')}),
                  mock.patch.object(first_step, 'gate_targets', return_value=targets),
                  mock.patch.object(first_step, 'admitted_run', return_value=nullcontext(admitted)) as place,
                  mock.patch.object(gate, 'current_gate_placement', side_effect=[gate.NO_GATE_ASSIGNMENT, assignment]),
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

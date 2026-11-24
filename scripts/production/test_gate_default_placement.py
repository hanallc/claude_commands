"""Route gate commands through the queue assignment."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager, nullcontext, redirect_stderr
from io import StringIO
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_client, admission_wire as wire, execution_identity, first_step, placement
from scripts.build_queue.admission_client import OuterReservation
from scripts.build_targets import mac_linux
from scripts.copies import copies
from scripts.lib import build_folders, conf, scratch
from scripts.lint.memory_test_support import write_config_fixture
from scripts.lib.build_steps import GATE_RESULT_UNKNOWN_STATUS, MAC_LINUX_PRELAUNCH_REFUSED_STATUS
from scripts.mac_test.claim_state import WHOLE_MAC
from scripts.production import gate
from scripts.production.gate_routes import GateResultUnknown, GateTookNoRun, ModuleCoverage


def placed(target: str) -> wire.Assignment:
    provider = 'scaler' if target == 'claude-aws' else 'ssh' if target == 'mac-linux' else 'local'
    return wire.Assignment(placement.PlacementAssignment('episode', 'attempt',
        placement.PlacementDestination(provider, target, 'fixture-host', WHOLE_MAC),
        execution_identity.SourceKeyKnown('fixture-source'), placement.SubmittingOwner('fixture-host', 1, '1'),
        NoOwningSession(), 1.0))


def scaler_registry(root: Path) -> dict[str, str]:
    _ = write_config_fixture(root / "build_targets.conf",
        "targets=natedev,mac-linux,claude-aws\nkind.claude-aws=scaler\n"
        + "settings.claude-aws=cloud_gate.conf\nenabled.claude-aws=on\n")
    return {"CONF_DIR": str(root)}


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
        for arguments in (checkpoint, ['--affected'], ['--tests-only']):
            with self.subTest(arguments=arguments), TemporaryDirectory() as config:
                calls: list[str] = []

                @contextmanager
                def admit(command: Sequence[str], source: Path, *, targets: tuple[str, ...]) -> Generator[object]:
                    self.assertEqual(list(command), ['scripts/production/gate.py', *arguments])
                    self.assertEqual(source, Path.cwd())
                    self.assertEqual(targets, ('natedev', 'mac-linux', 'claude-aws'))
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

                with (mock.patch.dict(os.environ, {**scaler_registry(Path(config)),
                                            'BUILDLOG_GATE_TARGETS': 'natedev,mac-linux,claude-aws'}, clear=True),
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

    def run_copy_fixture_gate(self, tree: Path, script: str, program: str, *, targets: str = "claude-aws") -> int:
        path = tree / script
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(dedent(program))
        repository = Path(__file__).resolve().parents[2]
        with (mock.patch.dict(os.environ, {**scaler_registry(tree), "BUILDLOG_GATE_TARGETS": "claude-aws", "PYTHONPATH": str(repository)},
                              clear=True),
              mock.patch.object(copies, "caller_owner", return_value="fixture"),
              mock.patch("scripts.copies.copies._command_copy",
                         return_value=nullcontext(copies.DirectCommandCopy(tree)))):
            return copies.main(["run", "--repo", str(tree), "--commit", "HEAD", "--targets", targets, "--",
                                sys.executable, script, "--tests-only"])

    def test_copied_gate_reuses_its_aws_assignment(self) -> None:
        with TemporaryDirectory() as directory:
            status = self.run_copy_fixture_gate(Path(directory), "scripts/production/gate.py", """\
                from contextlib import nullcontext, redirect_stderr
                from dataclasses import replace
                from io import StringIO
                import os
                from pathlib import Path
                from unittest import mock

                from scripts.aws_target import cloud_gate
                from scripts.build_queue import admission_client, admission_records, admission_wire as wire, first_step, placement
                from scripts.production import gate
                from scripts.production.test_gate_default_placement import placed

                pid = os.getpid()
                start = admission_records.process_start_time(pid)
                assert start is not None
                owner = placement.SubmittingOwner(gate.host_name(), pid, start)
                saved = replace(placed("claude-aws").assignment, owner=owner)
                path = Path.cwd() / "assignment.json"
                wire.write_assignment(path, wire.Assignment(saved))
                os.environ["BUILDLOG_PLACEMENT_FILE"] = str(path)
                os.environ["BUILDLOG_PLACEMENT_EPISODE"] = saved.episode
                status = wire.PlacementStatusReply(wire.KnownPlacementStatus(
                    saved, placement.CurrentAssignment(), placement.NoProviderClaim()))
                result = gate.GateRun((), 1, 0, tests_reused=True)
                with (mock.patch.object(admission_client, "placement_status", return_value=status) as check,
                      mock.patch.object(first_step, "admitted_run") as admit,
                      mock.patch.object(gate.scratch, "copy_lock", return_value=nullcontext()),
                      mock.patch.object(gate, "gate_recovery", return_value=nullcontext()),
                      mock.patch.object(gate.copies, "source_owner", return_value=""),
                      mock.patch.object(gate, "run_suites") as local,
                      mock.patch.object(cloud_gate, "working_tree", return_value=("tree", set())),
                      mock.patch.object(cloud_gate, "route", return_value=result) as aws,
                      redirect_stderr(StringIO()) as errors):
                    assert gate.main(["--tests-only"]) == 0, errors.getvalue()
                check.assert_called_once_with(saved.episode, saved.attempt)
                admit.assert_not_called()
                local.assert_not_called()
                aws.assert_called_once()
                assert aws.call_args.kwargs["assignment"] == saved
                """)
        self.assertEqual(status, 0)

    def test_copied_gate_without_an_assignment_runs_locally(self) -> None:
        with TemporaryDirectory() as directory:
            status = self.run_copy_fixture_gate(Path(directory), "scripts/production/gate.py", """\
                from contextlib import nullcontext
                from unittest import mock

                from scripts.aws_target import cloud_gate
                from scripts.build_queue import first_step
                from scripts.production import gate

                result = gate.GateRun((), 1, 0, tests_reused=True)
                with (mock.patch.object(first_step, "admitted_run") as admit,
                      mock.patch.object(gate.scratch, "copy_lock", return_value=nullcontext()),
                      mock.patch.object(gate, "gate_recovery", return_value=nullcontext()),
                      mock.patch.object(gate.copies, "source_owner", return_value=""),
                      mock.patch.object(gate, "run_suites", return_value=result) as local,
                      mock.patch.object(cloud_gate, "working_tree", return_value=("tree", set())),
                      mock.patch.object(cloud_gate, "route") as aws):
                    assert gate.main(["--tests-only"]) == 0
                admit.assert_not_called()
                aws.assert_not_called()
                local.assert_called_once()
                """)
        self.assertEqual(status, 0)

    def test_a_child_of_the_copied_gate_cannot_take_the_handoff(self) -> None:
        with TemporaryDirectory() as directory:
            status = self.run_copy_fixture_gate(Path(directory), "scripts/production/gate.py", """\
                import os
                import subprocess
                import sys
                from pathlib import Path

                from scripts.copies import copies

                fd = int(os.environ[copies.PREPARED_GATE_FD])
                repository = Path(copies.__file__).resolve().parents[2]
                check = (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "from scripts.copies import copies; assert not copies.take_prepared_gate_handoff()"
                )
                subprocess.run([sys.executable, "-c", check, str(repository)], pass_fds=(fd,), check=True)
                assert not copies.take_prepared_gate_handoff()
                """)
        self.assertEqual(status, 0)

    def test_a_gate_script_outside_the_production_path_gets_no_handoff(self) -> None:
        for symlink in (False, True):
            with self.subTest(symlink=symlink), TemporaryDirectory() as directory:
                tree = Path(directory) / "copy"
                tree.mkdir()
                script = Path(directory) / "gate.py"
                if symlink:
                    canonical = tree / "scripts/production/gate.py"
                    canonical.parent.mkdir(parents=True)
                    script = tree / "gate-alias.py"
                    script.symlink_to(canonical)
                status = self.run_copy_fixture_gate(tree, str(script), """\
                    import os

                    from scripts.copies import copies

                    assert copies.PREPARED_GATE_FD not in os.environ
                    """, targets="natedev,claude-aws")
                self.assertEqual(status, 0)

    def test_a_gate_inside_a_copy_shell_requires_direct_placement(self) -> None:
        assignment = placed('claude-aws')
        admitted = mock.Mock(spec=first_step.FirstStepAdmission,
                             grant=mock.Mock(spec=first_step.GatePlacement))
        with TemporaryDirectory() as directory:
            tree = Path(directory)
            command = ["sh", "-c", "gate.py --tests-only"]

            def launch(arguments: Sequence[str], *, cwd: Path, pass_fds: tuple[int, ...],
                       env: dict[str, str], text: bool) -> subprocess.CompletedProcess[str]:
                self.assertEqual(arguments, command)
                self.assertEqual(cwd, tree)
                self.assertTrue(text)
                self.assertEqual(pass_fds, ())
                self.assertNotIn(copies.PREPARED_GATE_FD, env)
                with mock.patch.dict(os.environ, env, clear=True):
                    status = gate.main(["--tests-only", "--repo", directory])
                return subprocess.CompletedProcess(arguments, status)

            with (mock.patch.dict(os.environ, {**scaler_registry(tree), "BUILDLOG_GATE_TARGETS": "claude-aws"}, clear=True),
                  mock.patch.object(copies, "caller_owner", return_value="fixture"),
                  mock.patch("scripts.copies.copies._command_copy",
                             return_value=nullcontext(copies.DirectCommandCopy(tree))),
                  mock.patch.object(subprocess, "run", side_effect=launch),
                  mock.patch.object(gate, 'current_gate_placement', side_effect=[gate.NO_GATE_ASSIGNMENT, assignment]),
                  mock.patch.object(gate, 'run_tests_only', return_value=0) as run,
                  mock.patch.object(first_step, 'admitted_run', return_value=nullcontext(admitted)) as admit):
                args = copies.CopyArguments(repo=directory)
                self.assertEqual(copies.run_copy(args, command, copies.CommittedSource("HEAD"), copies.NO_MERGE), 0)
        admit.assert_called_once_with(("scripts/production/gate.py", "--tests-only", "--repo", directory),
                                      tree, targets=("claude-aws",))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[-1], gate.AwsGateAssignment(assignment.assignment))

    def test_isolated_bootstrap_preserves_the_gate_script_and_job(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        with TemporaryDirectory() as directory:
            tree = Path(directory)
            script = tree / "scripts/production/gate.py"
            script.parent.mkdir(parents=True)
            _ = script.write_text(dedent("""\
                from pathlib import Path
                from unittest import mock

                from scripts.build_queue.admission_jobs import StepJob
                from scripts.production import gate

                def gate_command(args, _target):
                    assert args.command == (str(Path(__file__).absolute()), "--tests-only")
                    assert args.gate_job == StepJob("test-all")
                    return 0

                with (mock.patch.object(gate, "run_gate_command", gate_command),
                      mock.patch.object(gate, "current_gate_placement", return_value=gate.NO_GATE_ASSIGNMENT)):
                    raise SystemExit(gate.main())
                """))
            result = subprocess.run([str(repository / "scripts/lib/py"), "--isolated", str(script), "--tests-only"],
                                    cwd=tree, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_assignment_runs_its_route(self) -> None:
        result = gate.GateRun((), 1, 0, selection_certified=True)
        for target in ('natedev', 'mac-linux', 'claude-aws'):
            assignment = placed(target)
            with (self.subTest(target=target),
                  mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
                  mock.patch.object(conf, 'read', return_value=mock.Mock(value=mock.Mock(return_value=True))),
                  mock.patch.object(gate, 'run_suites', return_value=result) as local,
                  mock.patch.object(mac_linux, 'route', return_value=result) as guest,
                  mock.patch.object(cloud_gate, 'route', return_value=result) as aws):
                self.assertIs(gate.run_gate_suites(Path.cwd(), target=gate.placement_target(assignment)), result)
                self.assertEqual([local.call_count, guest.call_count, aws.call_count],
                                 [int(target == name) for name in ('natedev', 'mac-linux', 'claude-aws')])

    def test_without_a_queue_assignment_the_gate_uses_natedev(self) -> None:
        result = gate.GateRun((), 1, 0, selection_certified=True)
        with (mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
              mock.patch.object(cloud_gate, 'route') as aws,
              mock.patch.object(cloud_gate, 'run') as legacy_aws,
              mock.patch.object(gate, 'run_suites', return_value=result) as local):
            self.assertIs(gate.run_gate_suites(Path.cwd(), target=gate.NO_GATE_ASSIGNMENT), result)
        local.assert_called_once()
        aws.assert_not_called()
        legacy_aws.assert_not_called()

    def test_direct_aws_ends_after_one_placement_with_no_test_verdict(self) -> None:
        assignment = placed('claude-aws')
        admitted = mock.Mock(spec=first_step.FirstStepAdmission,
                             grant=mock.Mock(spec=first_step.GatePlacement))
        for outcome in (GateTookNoRun('fixture prelaunch refusal'), GateResultUnknown('fixture lost result')):
            with (self.subTest(status=outcome.status), TemporaryDirectory() as directory,
                  mock.patch.dict(os.environ, {**scaler_registry(Path(directory)),
                                                'BUILDLOG_GATE_TARGETS': 'claude-aws'}, clear=True),
                  mock.patch.object(gate, 'current_gate_placement', side_effect=[gate.NO_GATE_ASSIGNMENT, assignment]),
                  mock.patch.object(first_step, 'admitted_run', return_value=nullcontext(admitted)) as place,
                  mock.patch.object(gate, 'test_run_folder', return_value=nullcontext()),
                  mock.patch.object(gate, 'gate_recovery', return_value=nullcontext()),
                  mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
                  mock.patch.object(copies, 'source_owner', return_value=''),
                  mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
                  mock.patch.object(cloud_gate, 'route', side_effect=outcome) as aws,
                  mock.patch.object(gate, 'run_suites') as local,
                  mock.patch.object(mac_linux, 'route') as guest,
                  redirect_stderr(StringIO()) as errors):
                arguments = ('--affected', '--full', '--repo', directory)
                self.assertEqual(gate.main(arguments), outcome.status)
                self.assertIn(str(outcome), errors.getvalue())
                place.assert_called_once_with(('scripts/production/gate.py', *arguments), Path(directory), targets=('claude-aws',))
                aws.assert_called_once()
                local.assert_not_called()
                guest.assert_not_called()

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
        assignment = placed('mac-linux')
        result = gate.GateRun((gate.GateJobResult('fixture', 'test_ok.py', 0, 1, ''),), 1, 0,
                              selection_certified=True)
        with (mock.patch.object(cloud_gate, 'working_tree', return_value=('tree', set())),
              mock.patch.object(conf, 'read', return_value=mock.Mock(value=mock.Mock(return_value=True))),
              mock.patch.object(mac_linux, 'route', return_value=result) as run):
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

    def test_direct_guest_no_run_ends_with_79_without_replacement(self) -> None:
        self.check_direct_guest_refusal(mac_linux.GuestNotStarted('fixture no run'), 79)

    def test_direct_guest_unknown_result_ends_with_80_without_replacement(self) -> None:
        self.check_direct_guest_refusal(GateResultUnknown('fixture unknown result'), 80)

    def check_direct_guest_refusal(self, outcome: mac_linux.GuestNotStarted | GateResultUnknown, status: int) -> None:
        assignment = placed('mac-linux')
        targets = ('mac-linux', 'natedev')
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
            place.assert_called_once_with(('scripts/production/gate.py', *arguments), repo, targets=targets)
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

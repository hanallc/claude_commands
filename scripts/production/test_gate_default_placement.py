"""Route gate commands through the queue assignment."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import chdir, contextmanager, nullcontext, redirect_stderr
from dataclasses import replace
import io
import os
from pathlib import Path
import runpy
import shlex
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
from time import monotonic
from typing import cast
import unittest
from unittest import mock

from scripts.aws_target import cloud_gate
from scripts.build_hold.owning_session import NoOwningSession
from scripts.build_queue import admission_client, admission_wire as wire, execution_identity, first_step, placement, queue_place
from scripts.build_queue import admission_service as service
from scripts.build_queue.admission_client import OuterReservation
from scripts.build_queue.admission_test_support import AdmissionFixture
from scripts.build_queue.queue_fixture import write_memory_config
from scripts.build_targets import mac_linux
from scripts.buildlog import treekey
from scripts.copies import copies
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib import build_folders, conf, scratch
from scripts.lib.build_copies import CopyQueuePosition
from scripts.lib.build_steps import CLAIM_FAILURE_SUFFIX, GATE_PRELAUNCH_REFUSED_STATUS, GATE_UNSETTLED_LAUNCH_STATUS
from scripts.lib.git_test_support import commit_repository, fixture_git_environment
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint.memory_test_support import config_environment, write_config_fixture
from scripts.mac_test.claim_state import WHOLE_MAC
from scripts.production import gate
from scripts.production.gate_routes import GateResultUnknown, ModuleCoverage


REPOSITORY = Path(__file__).resolve().parents[2]


DIRECT_GATE_ARGUMENTS = (
    ('--tests-only',),
    ('--nightly', '--merge-branch', 'main', '--unit', 'fixture'),
    ('--merge-branch', 'main', '--unit-branch', 'fixture', '--checkpoint', 'tip',
     '--previous-checkpoint', 'base', '--unit', 'fixture', '--phase', '1'),
)


def placed(target: str) -> wire.Assignment:
    provider = 'scaler' if target in ('aws', 'claude-aws') else 'ssh' if target == 'mac-linux' else 'local'
    return wire.Assignment(placement.PlacementAssignment('episode', 'attempt',
        placement.PlacementDestination(provider, target, 'fixture-host', WHOLE_MAC),
        execution_identity.SourceKeyKnown('fixture-source'), placement.SubmittingOwner('fixture-host', 1, '1'),
        NoOwningSession(), 1.0))


def scaler_registry(root: Path) -> dict[str, str]:
    _ = write_config_fixture(root / "build_targets.conf",
        "targets=natedev,mac-linux,claude-aws\nkind.claude-aws=scaler\n"
        + "settings.claude-aws=cloud_gate.conf\nenabled.claude-aws=on\n")
    return {"CONF_DIR": str(root)}


def inherited_admission(grant: wire.Assignment) -> first_step.FirstStepAdmission:
    assignment = grant.assignment
    return first_step.FirstStepAdmission(CopyQueuePosition(assignment.episode, assignment.since),
        first_step.InheritedGatePlacement(grant), Path('/fixture/assignment.json'))


def run_aws_handoff_fixture(arguments: list[str], mode: gate.SuiteMode) -> None:
    """Check the prepared gate in the process that receives the pipe."""
    assert sys.argv[1:] == arguments
    assignment = placed("aws")
    with (mock.patch.object(gate, "current_gate_placement", return_value=assignment),
          mock.patch.object(gate, "test_run_folder", return_value=nullcontext()),
          mock.patch.object(first_step, "admitted_run") as admit,
          mock.patch.object(gate, "run_tests_only", return_value=0) as run):
        if Path(sys.argv[0]).name == "merge_gate.py":
            with unittest.TestCase().assertRaises(SystemExit) as status:
                _ = runpy.run_path(str(REPOSITORY / "scripts/production/merge_gate.py"), run_name="__main__")
            assert status.exception.code == 0
        else:
            assert gate.main() == 0
    admit.assert_not_called()
    run.assert_called_once()
    args, actual_mode, target = cast(tuple[gate.GateArguments, gate.SuiteMode, gate.GateTarget], run.call_args.args)
    assert args.prepared_copy
    assert (args.repo, actual_mode, target) == (str(Path.cwd()), mode, gate.AwsGateAssignment(assignment.assignment))


def run_held_direct_gate(ready: Path, arguments: list[str]) -> int:
    """Enter the direct placement and stop before the build or guest launch."""
    def hold(*_args: object) -> int:
        ready.touch()
        _ = sys.stdin.read()
        raise AssertionError("The parent did not terminate the gate")

    def hold_placed(args: gate.GateArguments, target: gate.GateTarget) -> int:
        with gate.gate_suite_target(args, Path(args.repo), target):
            return hold()

    # The tests-only route still has a legacy SIGTERM handler. Test owner exit without it.
    with (mock.patch.object(gate, 'unwind_gate_on_sigterm', nullcontext),
          mock.patch.object(gate, 'run_tests_only', side_effect=hold),
          mock.patch.object(gate, 'gate_production', return_value=ready.parent),
          mock.patch.object(gate, 'merge_checkpoint', side_effect=hold_placed),
          mock.patch.object(gate, 'nightly', side_effect=hold_placed),
          mock.patch.object(gate, '_nightly_unit', return_value='fixture'),
          mock.patch.object(gate, 'test_run_folder', return_value=nullcontext()),
          mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
          mock.patch.object(gate, 'gate_recovery', return_value=nullcontext()),
          mock.patch.object(build_folders, 'tool_folder', return_value=nullcontext(ready.parent))):
        return gate.main(arguments)


class GateDefaultPlacementTests(unittest.TestCase):
    def test_sigterm_releases_each_direct_placement_after_owner_exit(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        # Allow eight minimum pass gaps. The idle pass interval is five seconds.
        release_limit = 2.0
        cases = [(arguments, 'natedev') for arguments in DIRECT_GATE_ARGUMENTS]
        cases.append((('--tests-only',), 'mac-linux'))
        for arguments, target in cases:
            with self.subTest(arguments=arguments, target=target), AdmissionFixture() as fixture:
                def available(key: placement.HostEvidenceKey) -> placement.FreshHostEvidence:
                    return placement.FreshHostEvidence(key, fixture.clock.now(),
                        key.configuration_generation, placement.AllowedHost())

                fixture.server.host_probe = available
                ready = fixture.root / 'gate-ready'
                source = fixture.root / 'checkout'
                source.mkdir()
                environment = fixture_git_environment(fixture.root)
                environment['BUILDLOG_GATE_TARGETS'] = target
                for name in ('BUILDLOG_PLACEMENT_FILE', 'BUILDLOG_PLACEMENT_EPISODE', 'BUILDLOG_PREPARED_COPY'):
                    _ = environment.pop(name, None)
                _ = subprocess.run(['git', 'init', '-q'], cwd=source, env=environment, check=True,
                                   capture_output=True, timeout=TEST_HANG_GUARD_SECS)
                _ = subprocess.run(['git', 'remote', 'add', 'origin', str(fixture.root / 'claude_commands.git')],
                                   cwd=source, env=environment, check=True, capture_output=True,
                                   timeout=TEST_HANG_GUARD_SECS)
                _ = commit_repository(source, 'Fixture checkout', allow_empty=True, env=environment)
                program = dedent('''\
                    from pathlib import Path
                    import sys
                    from scripts.production.test_gate_default_placement import run_held_direct_gate

                    raise SystemExit(run_held_direct_gate(Path(sys.argv[1]), sys.argv[2:]))
                    ''')
                command = [sys.executable, '-B', '-c', program, str(ready), *arguments, '--repo', str(source)]
                with (mock.patch.object(fixture.server, '_prune_places',
                                        wraps=getattr(fixture.server, '_prune_places')) as prune,
                      fixture.serving(), subprocess.Popen(command, cwd=repository, env=environment,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child):
                    try:
                        wait_until(lambda: ready.exists() or child.poll() is not None, fixture.root,
                                   processes=(child,))
                        if not ready.exists():
                            output, errors = child.communicate(timeout=TEST_HANG_GUARD_SECS)
                            self.fail(output + errors)
                        assignment = wire.read_assignment(fixture.root / 'assignment.json').assignment
                        episode = assignment.episode
                        attempt = execution_identity.attempt_key(episode, assignment.attempt)
                        owner = wire.Owner(child.pid, assignment.owner.start_time)
                        watch = service.OwnerWatch(owner)
                        self.assertEqual(assignment.owner.pid, child.pid)
                        self.assertEqual(assignment.destination.target, target)
                        with fixture.loop_turn():
                            self.assertEqual(fixture.server.current_assignments[episode], attempt)
                            self.assertIn(watch, fixture.server.watchers.values())
                            self.assertEqual(episode in fixture.server.reservations, target == 'natedev')
                            permission = fixture.server.assignments[attempt].permission
                            expected = placement.PermissionAvailable if target == 'mac-linux' else placement.PermissionConsumed
                            self.assertIsInstance(permission, expected)

                        def released() -> bool:
                            state = service.read_state(fixture.state_dir)
                            return (episode not in state.current_assignments
                                    and episode not in state.reservations
                                    and isinstance(state.assignments[attempt].state, placement.ClosedAssignment))

                        started = monotonic()
                        child.send_signal(signal.SIGTERM)
                        wait_until(released, fixture.state_dir, timeout=release_limit)
                        elapsed = monotonic() - started
                        self.assertLess(elapsed, release_limit)
                        print(f'{target} {arguments[0]} SIGTERM to release: {elapsed:.3f}s', flush=True)
                        output, errors = child.communicate(timeout=TEST_HANG_GUARD_SECS)
                        self.assertEqual(child.returncode, -signal.SIGTERM, output + errors)
                        with fixture.loop_turn():
                            prune.assert_any_call(watch)
                            self.assertNotIn(watch, fixture.server.watchers.values())
                    finally:
                        if child.poll() is None:
                            child.kill()
                            _ = child.communicate(timeout=TEST_HANG_GUARD_SECS)

    def test_incomplete_gate_releases_each_direct_placement(self) -> None:
        for arguments in DIRECT_GATE_ARGUMENTS:
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
                    events.append('interrupt')
                    raise GateResultUnknown('The fixture gate stopped')

                def interrupt_placed(args: gate.GateArguments, target: gate.GateTarget) -> int:
                    with gate.gate_suite_target(args, Path.cwd(), target):
                        return interrupt()

                with (mock.patch.dict(os.environ, clear=True),
                      mock.patch.object(first_step, 'gate_targets', return_value=('natedev',)),
                      mock.patch.object(first_step, 'admitted_run', side_effect=admit),
                      mock.patch.object(treekey, 'tree_key', return_value='fixture-source'),
                      mock.patch.object(gate, 'current_gate_placement',
                                        side_effect=[gate.NO_GATE_ASSIGNMENT, placed('natedev')]),
                      mock.patch.object(gate, 'run_tests_only', side_effect=interrupt),
                      mock.patch.object(gate, 'gate_production', return_value=Path.cwd()),
                      mock.patch.object(gate, 'merge_checkpoint', side_effect=interrupt_placed),
                      mock.patch.object(gate, 'nightly', side_effect=interrupt_placed),
                      mock.patch.object(gate, '_nightly_unit', return_value='fixture'),
                      mock.patch.object(gate, '_sweep_nightly_build_folders'),
                      mock.patch.object(gate, 'test_run_folder', return_value=nullcontext()),
                      mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
                      mock.patch.object(gate, 'gate_recovery', return_value=nullcontext()),
                      mock.patch.object(gate, 'unwind_gate_on_sigterm', nullcontext),
                      redirect_stderr(io.StringIO())):
                    self.assertEqual(gate.main(arguments), GATE_UNSETTLED_LAUNCH_STATUS)
                    self.assertEqual(events, ['admit', 'interrupt', 'release'])

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
                      mock.patch.object(admission_client, "cancel_placement", side_effect=wire.AdmissionUnavailable(
                          "fixture queue unavailable")) as cancel,
                      mock.patch.object(copies, "run_prepared_gate",
                                        return_value=subprocess.CompletedProcess(command, status)),
                      redirect_stderr(io.StringIO()) as errors):
                    result = copies.main(["gate-step", "--repo", folder, "--targets", "mac-linux", "--", *command])
                self.assertEqual(result, status)
                self.assertEqual(errors.getvalue(), "Gate cleanup failed. fixture queue unavailable\n")
                cancel.assert_called_once()

    def test_gate_step_stops_after_a_mac_linux_claim_failure(self) -> None:
        with TemporaryDirectory() as folder:
            source = Path(folder)
            command = ["scripts/production/gate.py", "--tests-only"]
            request = mock.Mock(spec=wire.PlacementRequest, local=wire.LocalAdmissionUnavailable("fixture"))
            original = placed("mac-linux")
            replacement = wire.Assignment(replace(placed("natedev").assignment, attempt="replacement"))
            failure = wire.ClaimFailure("refusal", original.assignment, placement.MAC_LINUX_NO_ANSWER)
            settled = wire.DispatchStatus(original.assignment, placement.ClosedAssignment(failure.reason),
                                          placement.PermissionAvailable(), retry=placement.RetryAt(0))

            def launch(arguments: Sequence[str], *, cwd: Path, descriptors: tuple[int, ...],
                       environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
                self.assertEqual((arguments, cwd, descriptors), (command, source, ()))
                self.assertEqual(environment["BUILDLOG_PREPARED_COPY"], str(source))
                assignment_file = Path(environment["BUILDLOG_PLACEMENT_FILE"])
                if wire.read_assignment(assignment_file) == original:
                    report = assignment_file.with_name(assignment_file.name + CLAIM_FAILURE_SUFFIX)
                    _ = report.write_text(wire.encode_request(failure))
                    return subprocess.CompletedProcess(command, GATE_PRELAUNCH_REFUSED_STATUS)
                return subprocess.CompletedProcess(command, 0)

            with (mock.patch.dict(os.environ, clear=True),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(source)),
                  mock.patch.object(first_step, "_gate_request", return_value=request),
                  mock.patch.object(first_step, "_placement_grant", side_effect=(original, replacement)) as place,
                  mock.patch.object(first_step, "assignment_matches_request", return_value=True),
                  mock.patch.object(admission_client, "claim_failure", return_value=settled) as claim,
                  mock.patch.object(admission_client, "cancel_placement", return_value=wire.Ok()) as cancel,
                  mock.patch.object(copies, "run_prepared_gate", side_effect=launch) as run):
                result = copies.main(["gate-step", "--repo", folder, "--targets", "mac-linux,natedev", "--", *command])
            self.assertEqual(result, GATE_PRELAUNCH_REFUSED_STATUS)
            place.assert_called_once_with(request)
            claim.assert_not_called()
            run.assert_called_once()
            cancel.assert_called_once()

    def test_gate_step_hands_off_its_aws_assignment(self) -> None:
        self.check_gate_step_aws_handoff("gate.py")

    def test_gate_step_hands_off_its_aws_assignment_to_merge_gate(self) -> None:
        self.check_gate_step_aws_handoff("merge_gate.py")

    def check_gate_step_aws_handoff(self, entrypoint: str) -> None:
        with TemporaryDirectory() as folder:
            source = Path(folder).resolve()
            script = Path("scripts/production") / entrypoint
            arguments = ["--tests-only", "--repo", str(source)]
            command = [sys.executable, "-B", str(script), *arguments]
            (source / script).parent.mkdir(parents=True)
            _ = (source / script).write_text(dedent(f"""\
                from scripts.production.gate import SuiteMode
                from scripts.production.test_gate_default_placement import run_aws_handoff_fixture

                run_aws_handoff_fixture({arguments!r}, SuiteMode.GATE)
                """))
            assignment = placed("aws")
            admitted = inherited_admission(assignment)
            with (mock.patch.dict(os.environ, {"PYTHONPATH": str(REPOSITORY)}, clear=True),
                  mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)) as admit):
                self.assertEqual(copies.main(["gate-step", "--repo", str(source), "--targets", "aws", "--", *command]), 0)
            admit.assert_called_once_with(command, source, targets=("aws",))

    def test_gate_step_runs_an_affected_named_module_on_natedev(self) -> None:
        with TemporaryDirectory() as folder:
            source = Path(folder).resolve()
            module = "fixture/test_ok.py"
            script = Path("scripts/production/gate.py")
            arguments = ["--affected", "--module", module, "--repo"]
            command = [sys.executable, "-B", str(script), *arguments, "{tree}"]
            expanded = [*arguments, str(source)]
            test = source / "scripts" / module
            test.parent.mkdir(parents=True)
            test.touch()
            assignment = placed("natedev")
            (source / script).parent.mkdir(parents=True)
            _ = (source / "scripts/__init__.py").write_text('')
            _ = (source / script).write_text(dedent(f"""\
                from contextlib import nullcontext
                from pathlib import Path
                import sys
                from unittest import mock

                def check_prepared_gate_source(arguments, parser, source):
                    parser.add_argument("--affected", action="store_true", required=True)
                    parser.add_argument("--module", required=True)
                    parser.add_argument("--repo", required=True)
                    args = parser.parse_args(arguments)
                    assert args.repo == str(source)
                    assert (source / "scripts" / args.module).is_file()
                    (source / "validated").write_text(args.repo)

                if __name__ == "__main__":
                    from scripts.production import gate
                    from scripts.production.test_gate_default_placement import placed

                    assert sys.argv[1:] == {expanded!r}
                    assert (Path.cwd() / "validated").read_text() == sys.argv[-1]
                    outcome = gate.GateRun((gate.GateJobResult("fixture", "test_ok.py", 0, 1, ""),), 1, 0)
                    with (mock.patch.object(gate, "current_gate_placement", return_value=placed("natedev")),
                          mock.patch.object(gate, "test_run_folder", return_value=nullcontext()),
                          mock.patch.object(gate.scratch, "copy_lock", return_value=nullcontext()),
                          mock.patch.object(gate, "gate_recovery", return_value=nullcontext()),
                          mock.patch.object(gate.copies, "source_owner", return_value=""),
                          mock.patch.object(gate, "run_named_modules", return_value=outcome) as run):
                        assert gate.main() == 0
                    run.assert_called_once_with(Path.cwd(), [{module!r}], gate.SuiteMode.TARGETED)
                """))
            with (mock.patch.dict(os.environ, {"PYTHONPATH": str(REPOSITORY)}, clear=True),
                  mock.patch.object(first_step, "admitted_run", return_value=nullcontext(
                      inherited_admission(assignment))) as admit):
                self.assertEqual(copies.main(["gate-step", "--repo", str(source), "--targets", "natedev", "--", *command]), 0)
            admit.assert_called_once_with([*command[:-1], str(source)], source, targets=("natedev",))

    def test_gate_step_expands_the_checkout_path_once(self) -> None:
        with TemporaryDirectory() as folder:
            source = Path(folder).resolve() / "{tree}"
            script = "scripts/production/gate.py"
            module = "fixture/test_ok.py"
            test = source / "scripts" / module
            test.parent.mkdir(parents=True)
            test.touch()
            package = source / "scripts/__init__.py"
            _ = package.write_text(f"__path__.append({str(REPOSITORY / 'scripts')!r})\n")
            (source / script).parent.mkdir()
            _ = (source / script).write_bytes((REPOSITORY / script).read_bytes())
            command = [sys.executable, "-B", script, "--affected", "--module", module, "--repo"]
            expanded = [*command, str(source)]
            grant = placed("natedev")
            admitted = inherited_admission(grant)
            with (mock.patch.dict(os.environ, {}, clear=True),
                  mock.patch.object(first_step, "admitted_run", return_value=nullcontext(admitted)) as admit,
                  mock.patch.object(copies, "run_prepared_gate",
                                    return_value=subprocess.CompletedProcess(expanded, 0)) as launch):
                result = copies.main(["gate-step", "--repo", str(source), "--targets", "natedev",
                                      "--", *command, "{tree}"])
            self.assertEqual(result, 0)
            admit.assert_called_once_with(expanded, source, targets=("natedev",))
            launch.assert_called_once_with(expanded, cwd=source, descriptors=(), environment={
                "BUILDLOG_PREPARED_COPY": str(source),
                "BUILDLOG_PLACEMENT_FILE": str(admitted.assignment_file),
                "BUILDLOG_PLACEMENT_EPISODE": grant.assignment.episode,
            })

    def test_default_modes_enter_one_admission_for_the_whole_command(self) -> None:
        checkpoint = ['--merge-branch', 'main', '--unit-branch', 'fixture', '--checkpoint', 'tip',
                      '--previous-checkpoint', 'base', '--unit', 'fixture', '--phase', '1']
        for arguments in (checkpoint, ['--tests-only'], ['--affected']):
            with self.subTest(arguments=arguments):
                calls: list[str] = []
                prepared = arguments == ['--affected']
                environment = {'BUILDLOG_GATE_TARGETS': 'natedev,mac-linux,aws'}
                expected_targets = ('natedev', 'mac-linux')
                if prepared:
                    environment['BUILDLOG_PREPARED_COPY'] = str(Path.cwd())

                @contextmanager
                def admit(command: Sequence[str], source: Path, *, targets: tuple[str, ...]) -> Generator[first_step.FirstStepAdmission]:
                    self.assertEqual(list(command), ['gate.py', *arguments])
                    self.assertEqual(source, Path.cwd())
                    self.assertEqual(targets, expected_targets)
                    calls.append('admit')
                    try:
                        yield inherited_admission(placed('natedev'))
                    finally:
                        calls.append('release')

                def run(_args: gate.GateArguments, _mode: gate.SuiteMode, target: gate.GateTarget) -> int:
                    self.assertEqual(target, 'natedev')
                    calls.append('run')
                    return 0

                def merge(args: gate.GateArguments, target: gate.GateTarget) -> int:
                    with gate.gate_suite_target(args, Path.cwd(), target) as assigned:
                        return run(args, gate.SuiteMode.GATE, assigned)

                with (mock.patch.dict(os.environ, environment, clear=True),
                      mock.patch.object(copies, 'take_prepared_gate_handoff', return_value=prepared),
                      mock.patch.object(first_step, 'admitted_run', side_effect=admit),
                      mock.patch.object(treekey, 'tree_key', return_value='fixture-source'),
                      mock.patch.object(gate, 'current_gate_placement',
                                        side_effect=[gate.NO_GATE_ASSIGNMENT, placed('natedev')]),
                      mock.patch.object(gate, 'run_tests_only', side_effect=run),
                      mock.patch.object(gate, 'gate_production', return_value=Path.cwd()),
                      mock.patch.object(gate, 'merge_checkpoint', side_effect=merge),
                      mock.patch.object(gate, 'test_run_folder', return_value=nullcontext()),
                      mock.patch.object(scratch, 'copy_lock', return_value=nullcontext()),
                      mock.patch.object(gate, 'gate_recovery', return_value=nullcontext())):
                    self.assertEqual(gate.main(arguments), 0)
                self.assertEqual(calls, ['run'] if prepared else ['admit', 'run', 'release'])

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

            state: placement.AssignmentState = placement.CurrentAssignment()

            def status(_episode: str, _attempt: str) -> wire.PlacementStatusReply:
                assignment = wire.read_assignment(repo / "assignment.json").assignment
                return wire.PlacementStatusReply(wire.KnownPlacementStatus(
                    assignment, state, placement.NoProviderClaim()))

            with (mock.patch.dict(os.environ, {"PATH": os.environ["PATH"], "CONF_DIR": directory, "HOME": directory,
                                               "CLAUDE_SESSION_NAME": "gate-fixture"}, clear=True),
                  mock.patch.object(build_folders, "repo_word", return_value="claude"),
                  mock.patch.object(build_folders, "tool_folder", return_value=nullcontext(repo)),
                  mock.patch.object(treekey, "tree_key", return_value="fixture-source"),
                  mock.patch.object(admission_client, "placement_status", side_effect=status) as query,
                  mock.patch.object(queue_place, "wait_for_placement", side_effect=place) as request,
                  mock.patch.object(admission_client, "cancel_placement") as close,
                  mock.patch.object(gate, "run_tests_only", side_effect=run) as suites,
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(gate.main(["--tests-only"]), 0)
                request.assert_called_once()
                query.assert_called_once()
                close.assert_called_once()
                suites.assert_called_once()

                state = placement.ClosedAssignment("fixture closed")
                self.assertEqual(gate.main(["--tests-only"]), 1)
            self.assertEqual(errors.getvalue(), "REFUSED: The gate placement attempt is stale: fixture closed\n")
            self.assertEqual(request.call_count, 2)
            self.assertEqual(query.call_count, 2)
            self.assertEqual(close.call_count, 2)
            suites.assert_called_once()

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

    def test_direct_affected_gate_prints_the_copy_command_and_stops(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "unit's worktree"
            nested = repo / "scripts" / "example"
            nested.mkdir(parents=True)
            environment = fixture_git_environment(root)
            _ = subprocess.run(["git", "init", "-q", str(repo)], env=environment, check=True,
                               capture_output=True, timeout=TEST_HANG_GUARD_SECS)
            commit = commit_repository(repo, "Unit fixture", allow_empty=True, env=environment)
            environment["BUILDLOG_PREPARED_COPY"] = str(repo)
            _ = environment.pop(copies.PREPARED_GATE_FD, None)
            for cwd, repo_options in (
                (repo, ()),
                (nested, ()),
                (repo, ("--repo", str(nested))),
                (repo, (f"--repo={nested}",)),
                (repo, ("--checkout", str(nested))),
                (repo, (f"--checkout={nested}",)),
                (repo, ("--repo", str(repo), f"--checkout={nested}")),
            ):
                module_options = ["--module", "fixture/test_ok.py"] if repo_options else []
                arguments = ["--affected", *repo_options, *module_options]
                with (self.subTest(cwd=cwd, arguments=arguments), chdir(cwd),
                      mock.patch.dict(os.environ, environment, clear=True),
                      mock.patch.object(gate, "checked_gate_target") as place,
                      mock.patch.object(gate, "run_gate_command") as run,
                      redirect_stderr(io.StringIO()) as errors):
                    self.assertEqual(gate.main(arguments), 1)
                command, = errors.getvalue().splitlines()
                self.assertEqual(shlex.split(command), [
                    "~/.claude/scripts/lib/py", "~/.claude/scripts/copies/copies.py", "run", "--repo", str(repo),
                    "--commit", commit, "--", "./scripts/lib/py", "scripts/production/gate.py",
                    "--affected", *module_options, "--repo", "{tree}"])
                place.assert_not_called()
                run.assert_not_called()

    def test_printed_affected_copy_command_runs_with_the_prepared_handoff(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source, tree = root / "unit", root / "numbered copy"
            arguments = ["--affected", "--checkout", str(source), "--full"]
            commit = "a" * 40
            with (mock.patch.dict(os.environ, clear=True),
                  mock.patch.object(gate, "checked_git", side_effect=(str(source), commit)),
                  redirect_stderr(io.StringIO()) as errors):
                self.assertEqual(gate.main(arguments), 1)
            copy_command = shlex.split(errors.getvalue())[2:]

            interpreter = tree / "scripts/lib/py"
            interpreter.parent.mkdir(parents=True)
            interpreter.symlink_to(sys.executable)
            script = tree / "scripts/production/gate.py"
            script.parent.mkdir()
            expected = ["--affected", "--full", "--repo", str(tree)]
            _ = script.write_text(dedent(f"""\
                from scripts.lib.python_startup.sitecustomize import select_checkout

                select_checkout([{str(REPOSITORY / "scripts/production/gate.py")!r}])

                from scripts.production.gate import SuiteMode
                from scripts.production.test_gate_default_placement import run_aws_handoff_fixture

                run_aws_handoff_fixture({expected!r}, SuiteMode.TARGETED)
                """))
            with (mock.patch.dict(os.environ, {"PYTHONPATH": str(REPOSITORY)}, clear=True),
                  mock.patch.object(first_step, "gate_targets", return_value=("aws",)),
                  mock.patch.object(first_step, "admitted_run") as admit,
                  mock.patch.object(copies, "caller_owner", return_value="fixture"),
                  mock.patch.object(copies, "_command_copy", return_value=nullcontext(copies.DirectCommandCopy(tree)))):
                self.assertEqual(copies.main(copy_command), 0)
            admit.assert_not_called()

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
                from scripts.buildlog import treekey
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
                      mock.patch.object(treekey, "tree_key", return_value="fixture-source") as source,
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
                source.assert_called_once_with(str(Path.cwd()))
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
        repo = Path(self.enterContext(TemporaryDirectory())).resolve()
        test = repo / 'scripts/fixture/test_ok.py'
        test.parent.mkdir(parents=True)
        test.touch()
        arguments = ['--tests-only', '--module', 'fixture/test_ok.py', '--repo', str(repo)]
        parent = mock.Mock(spec=OuterReservation)
        assignment = placed('mac-linux')
        for nested in (True, False):
            with (self.subTest(nested=nested), mock.patch.dict(os.environ, {"BUILDLOG_GATE_TARGETS": "mac-linux"}, clear=True),
                  mock.patch.object(treekey, 'tree_key', return_value='fixture-source'),
                  mock.patch.object(gate, 'outer_reservation', return_value=parent if nested else None),
                  mock.patch.object(gate, 'current_gate_placement', side_effect=[gate.NO_GATE_ASSIGNMENT, assignment]),
                  mock.patch.object(first_step, 'gate_targets', wraps=first_step.gate_targets) as targets,
                  mock.patch.object(first_step, 'admitted_run', return_value=nullcontext(
                      inherited_admission(assignment))) as admit,
                  mock.patch.object(gate, 'run_tests_only', return_value=0) as run):
                self.assertEqual(gate.main(arguments), 0)
            self.assertEqual(admit.call_count, int(not nested))
            target = cast(gate.GateTarget, run.call_args.args[-1])
            self.assertEqual(target, 'natedev' if nested else gate.MacLinuxAssignment(assignment.assignment))
            if nested:
                targets.assert_not_called()

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

            def status(_episode: str, _attempt: str) -> wire.PlacementStatusReply:
                assignment = wire.read_assignment(repo / 'assignment.json').assignment
                return wire.PlacementStatusReply(wire.KnownPlacementStatus(
                    assignment, placement.CurrentAssignment(), placement.NoProviderClaim()))

            test = repo / 'scripts/fixture/test_ok.py'
            test.parent.mkdir(parents=True)
            test.touch()
            arguments = ['--tests-only', '--module', 'fixture/test_ok.py', '--repo', str(repo)]
            with (mock.patch.dict(os.environ, {'PATH': os.environ['PATH'], 'CONF_DIR': directory, 'HOME': directory,
                                               'CLAUDE_SESSION_NAME': 'gate-fixture', 'BUILDLOG_GATE_TARGETS': 'natedev'}, clear=True),
                  mock.patch.object(build_folders, 'repo_word', return_value='claude'),
                  mock.patch.object(build_folders, 'tool_folder', return_value=nullcontext(repo)),
                  mock.patch.object(treekey, 'tree_key', return_value='fixture-source'),
                  mock.patch.object(gate, 'outer_reservation', return_value=None),
                  mock.patch.object(queue_place, 'wait_for_placement', side_effect=place),
                  mock.patch.object(admission_client, 'placement_status', side_effect=status),
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

    def test_prepared_guest_no_run_ends_with_79_without_replacement(self) -> None:
        self.check_prepared_guest_refusal(mac_linux.GuestNotStarted('fixture no run'), GATE_PRELAUNCH_REFUSED_STATUS)

    def test_prepared_guest_unknown_result_ends_with_80_without_replacement(self) -> None:
        self.check_prepared_guest_refusal(GateResultUnknown('fixture unknown result'), GATE_UNSETTLED_LAUNCH_STATUS)

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
            environment = {'BUILDLOG_PREPARED_COPY': str(repo), 'BUILDLOG_PLACEMENT_FILE': str(repo / 'assignment')}
            with (mock.patch.dict(os.environ, environment, clear=True),
                  mock.patch.object(copies, 'take_prepared_gate_handoff', return_value=True),
                  mock.patch.object(first_step, 'admitted_run') as place,
                  mock.patch.object(gate, 'current_gate_placement', return_value=assignment),
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

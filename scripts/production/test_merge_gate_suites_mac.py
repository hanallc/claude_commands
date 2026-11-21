"""Gate placement through a private service and a fake Mac runner."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager, redirect_stdout
from dataclasses import replace
from enum import Enum
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
from typing import cast, final, override
from unittest import mock
from uuid import uuid4

from scripts.aws_target import cloud_gate, gate_box, ledger
from scripts.build_queue import admission_client as client, admission_protocol as wire, admission_service, queue_place
from scripts.build_queue import admission_wire as status_wire, execution_identity, first_step, placement
from scripts.build_queue.admission_test_support import CallerProviderControls, caller_provider_controls
from scripts.build_queue.queue_fixture import write_process
from scripts.build_queue.test_placement import local_candidate, placement_request
from scripts.direct.wait_for_test_state import wait_until
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.checkout_test_support import write_stdlib_tool
from scripts.lib.process_start import Started, process_start
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lint import memory_admit
from scripts.lint.memory_test_support import write_config_fixture
from scripts.mac_test import claim_state, eligibility, mac_gate, mac_queue_run, mac_test, offload
from scripts.production import mac_gate_process, merge_gate, targeted_test
from scripts.production.gate_modules import GateRefused
from scripts.production.gate_routes import (
    MacGateCompleted, MacGatePrelaunchRefused, MacGateUnresolved, ModuleCoverage, SuiteMode, TreeSelection,
)
from scripts.production.merge_gate_suites_test_support import GateSuitesFixture
from scripts.production.merge_gate_test_support import gate_suites


HOOKS = "scripts/hooks/test_first.py"
LIB_FIRST = "scripts/lib/test_first.py"
LIB_SECOND = "scripts/lib/test_second.py"
MESSAGE = "scripts/message/test_first.py"


class MacTransportCase(Enum):
    HELD = "held"
    MALFORMED = "malformed"
    COMPLETE = "complete"
    CANCEL_COMPLETION = "cancel-completion"
    UNAVAILABLE_OWNER = "unavailable-owner"
    STALLED_CLEANUP = "stalled-cleanup"
    CLAIM_PUBLICATION = "claim-publication"
    MANUAL_BEFORE_PLACEMENT = "manual-before-placement"
    HOLD_MANUAL_TESTS = "hold-manual-tests"


class ClaimWriteBarrier(Enum):
    BEFORE = "before"
    AFTER = "after"


def run_claim_at_write_barrier() -> int:
    """Pause the real claim subprocess at its record write."""
    state = Path(os.environ["MERGE_GATE_TEST_STATE"])
    barrier = ClaimWriteBarrier(os.environ["MAC_GATE_CLAIM_BARRIER"])
    claim_id = sys.argv[sys.argv.index("--claim-id") + 1]
    attempt = sys.argv[sys.argv.index("--attempt") + 1]
    write = mac_test.write_json

    def pause() -> None:
        write_text_atomic(state / "claim-at-write", f"{attempt} {os.getpid()}")
        wait_until((state / "allow-claim-write").exists, state)

    def write_claim(path: Path, record: mac_test.RunRecord | dict[str, object]) -> None:
        if record.get("claim_id") != claim_id:
            write(path, record)
            return
        if barrier is ClaimWriteBarrier.BEFORE:
            pause()
        write(path, record)
        write_text_atomic(state / "claim-written", claim_id)
        if barrier is ClaimWriteBarrier.AFTER:
            pause()

    with mock.patch.object(mac_test, "write_json", side_effect=write_claim):
        return mac_test.main()


def run_queue_with_fixture_transport() -> int:
    """Run the assigned queue with local substitutes for remote operations."""
    state = Path(os.environ["MERGE_GATE_TEST_STATE"])
    write_text_atomic(state / "assigned-queue-pid", str(os.getpid()))
    run = mac_queue_run.run_assigned

    def with_transport(args: mac_queue_run.Arguments, encoded: str) -> int:
        return run_assignment_with_fixture_transport(run, args, encoded, state)

    start = client.placement_status

    def confirm_owner(episode: str, attempt: str) -> status_wire.PlacementStatusReply:
        if MacTransportCase(os.environ["MAC_GATE_TRANSPORT_CASE"]) is MacTransportCase.UNAVAILABLE_OWNER:
            args = sys.argv[1:]
            assignment = mac_queue_run.read_assignment(args[args.index("--gate-assignment") + 1])
            paths = mac_gate.assigned_gate_paths(assignment.attempt)
            assert paths.journal.is_file() and not paths.request_root.exists() and not paths.directory.exists()
            write_text_atomic(state / "owner-unavailable", attempt)
            raise wire.AdmissionUnavailable("the fixture admission service is unavailable")
        return start(episode, attempt)

    if MacTransportCase(os.environ["MAC_GATE_TRANSPORT_CASE"]) is MacTransportCase.MANUAL_BEFORE_PLACEMENT:
        def hold_placement(command: list[str], caller: mac_queue_run.QueueCaller) -> tuple[int, str]:
            episode = command[command.index("--episode") + 1]
            write_text_atomic(state / "manual-before-placement", json.dumps([episode, sys.argv[-2]]))
            caller.ready(timeout=-1)
            raise AssertionError("the manual runner reached placement")

        with mock.patch.object(mac_queue_run, "capture_command", side_effect=hold_placement):
            return mac_queue_run.main()

    with (mock.patch.object(mac_queue_run, "run_assigned", side_effect=with_transport),
          mock.patch.object(client, "placement_status", side_effect=confirm_owner)):
        return mac_queue_run.main()


def run_assignment_with_fixture_transport(run: Callable[[mac_queue_run.Arguments, str], int],
                                          args: mac_queue_run.Arguments, encoded: str, state: Path) -> int:
    case = MacTransportCase(os.environ["MAC_GATE_TRANSPORT_CASE"])
    folder = Path(args.argv[-2])
    assignment = mac_queue_run.read_assignment(encoded)
    proc = Path(os.environ["BUILDLOG_PROC"])
    start = process_start(os.getpid())
    assert isinstance(start, Started)
    assert os.getppid() == assignment.owner.pid
    write_process(proc, assignment.owner.pid, start=assignment.owner.start_time)
    write_process(proc, os.getpid(), parent=assignment.owner.pid, start=start.start)
    reference = placement.assigned_claim_reference(assignment)
    identity = claim_state.ClaimIdentity(reference.claim_id, reference.episode, reference.attempt)

    callback = mac_queue_run.run_callback

    def lifecycle(_script: str, phase: str, argv: Path, arguments: list[str],
                  environment: dict[str, str], **options: object) -> int:
        assert environment["MAC_QUEUE_HOST"] == assignment.destination.host
        if phase == "prepare":
            _ = argv.write_text(json.dumps(["true"]))
            _ = argv.with_suffix(".json.close").write_text("true")
        else:
            assert phase == "cleanup"
            if case is MacTransportCase.STALLED_CLEANUP:
                timeout = options["timeout"]
                assert isinstance(timeout, float)
                return callback(str(state / "stalled-cleanup.py"), phase, argv, arguments, environment,
                                timeout=timeout)
            write_text_atomic(state / "assigned-closed", assignment.attempt)
        return 0

    def stream(_command: list[str], remote: mac_queue_run.RemoteRun, _ssh: ExitStack,
               emit: Callable[[str], None] = print, *, caller: mac_queue_run.QueueCaller) -> int:
        remote.state = mac_queue_run.RemoteState.LIVE
        if case in (MacTransportCase.HELD, MacTransportCase.HOLD_MANUAL_TESTS):
            write_text_atomic(state / "assigned-ready", str(folder))
            if case is MacTransportCase.HELD:
                caller.ready(timeout=-1)
            else:
                wait_until((state / "allow-manual-finish").is_file, state)
        write_text_atomic(state / "assigned-streamed", assignment.attempt)
        if case is MacTransportCase.MALFORMED:
            emit(mac_gate.RESULT_PREFIX + "{\n")
        else:
            request = cloud_gate.read(folder / "request.json")
            output = (state / "assigned-output").read_text()
            modules = tuple(Path(path) for path in cast(list[str], request["modules"]))
            jobs = tuple(merge_gate.GateJobResult(path.parent.name, path.name, 0, 1, output) for path in modules)
            row: dict[str, object] = {key: ledger.text(request, key) for key in ("attempt", "tree", "commit")}
            row["gate"] = gate_box.encode_run(merge_gate.GateRun(jobs, 1, 0))
            emit(mac_gate.RESULT_PREFIX + json.dumps(row) + "\n")
        remote.state = mac_queue_run.RemoteState.EXITED
        return 0

    def settled(_config: object, host: str, _script: str, target: str, **_options: object) -> offload.LeftoverObservation:
        assert (host, target) == (assignment.destination.host, reference.target)
        return offload.LeftoverObservation(
            offload.LeftoversEnded(), "dispatch_start_settled=" + offload.pending_start_key(identity) + "\n")

    report = client.report_execution

    def interrupt_before_report(message: wire.ExecutionReport) -> wire.DispatchStatus:
        if case is MacTransportCase.CANCEL_COMPLETION and isinstance(message.outcome, placement.AttemptCompleted):
            assert offload.read_assigned_mac_claim(assignment) == offload.MacClaimSettled()
            write_text_atomic(state / "assigned-before-report", assignment.attempt)
            os.kill(os.getpid(), signal.SIGKILL)
            raise AssertionError("the runner did not stop before the report")
        return report(message)

    claim_command = offload.claim_arguments

    def claim_at_write_barrier(request: offload.OffloadRequest, settings: eligibility.OffloadConfig,
                               slots: tuple[int, ...], *, fresh_only: bool) -> list[str]:
        command = claim_command(request, settings, slots, fresh_only=fresh_only)
        if case is MacTransportCase.CLAIM_PUBLICATION:
            command[1] = str(state / "claim-at-write.py")
        return command

    with (mock.patch.object(mac_queue_run, "run_callback", side_effect=lifecycle),
          mock.patch.object(mac_queue_run, "stream_remote", side_effect=stream),
          mock.patch.object(offload, "read_leftovers", side_effect=settled),
          mock.patch.object(client, "report_execution", side_effect=interrupt_before_report),
          mock.patch.object(offload, "claim_arguments", side_effect=claim_at_write_barrier)):
        return run(args, encoded)


@final
class MacGateSuitesTests(GateSuitesFixture):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.output = io.StringIO()
        self.requests: list[wire.PlacementRequest] = []
        self.mac_runs: list[tuple[placement.PlacementAssignment, TreeSelection]] = []
        self.mac_outcome: MacGateCompleted | MacGatePrelaunchRefused | MacGateUnresolved = MacGateCompleted(
            merge_gate.GateRun((), 0, 0))
        self.before_mac: Callable[[], None] = lambda: None
        self.cloud_route = mock.Mock(return_value="fixture cloud unavailable")

    @property
    def controls(self) -> CallerProviderControls:
        return caller_provider_controls(self, self.admission)

    @override
    def setUp(self) -> None:
        super().setUp()
        with (self.checkout / ".git/info/exclude").open("a") as exclusions:
            _ = exclusions.write(f"\n/{mac_gate.MODULE_LIST}\n")
        self.exclude_modules()
        self.controls.set_host("mac", placement.AllowedHost())
        grant = first_step.placement_grant

        def place(request: wire.PlacementRequest) -> wire.Assignment | wire.LocalPlacementGranted:
            self.requests.append(request)
            return grant(request)

        _ = cast(object, self.enterContext(mock.patch.object(first_step, "placement_grant", side_effect=place)))
        _ = cast(object, self.enterContext(mock.patch.object(mac_gate_process, "load_mac_gate_route", return_value=self)))
        _ = cast(object, self.enterContext(mock.patch.object(cloud_gate, "route", self.cloud_route)))
        _ = cast(object, self.enterContext(mock.patch.dict(os.environ, self.environment)))
        _ = self.enterContext(redirect_stdout(self.output))

    def exclude_modules(self, *paths: str) -> None:
        self.write(self.checkout / mac_gate.MODULE_LIST,
                   mac_gate.MODULE_LIST_HEADER + "".join(path + "\n" for path in paths))

    read_modules = staticmethod(mac_gate.read_modules)

    def run_assigned(self, repo: Path, *, assignment: placement.PlacementAssignment, mode: SuiteMode,
                     full: bool, module_time_limit_s: float, selected: TreeSelection,
                     ) -> MacGateCompleted | MacGatePrelaunchRefused | MacGateUnresolved:
        self.assertEqual(repo, self.checkout)
        self.assertEqual(assignment.destination.target, "mac")
        self.assertEqual(assignment.source_key, execution_identity.SourceKeyKnown(selected.tree))
        self.assertEqual(self.requests[-1].profile.step, mode.value)
        self.assertEqual(module_time_limit_s, merge_gate.DEFAULT_MODULE_TIME_LIMIT_S)
        self.assertFalse(full)
        self.before_mac()
        self.mac_runs.append((assignment, selected))
        reference = placement.assigned_claim_reference(assignment)
        if isinstance(self.mac_outcome, MacGatePrelaunchRefused):
            status = client.report_execution(wire.ExecutionReport(uuid4().hex, assignment, reference,
                execution_identity.ExecutionNotStarted(), placement.PrelaunchRefused(self.mac_outcome.reason),
                placement.DispatchPathSettled()))
            self.assertEqual(status.state, placement.ClosedAssignment(self.mac_outcome.reason))
            return self.mac_outcome

        holder = placement.ClaimHolderIdentity(assignment.owner.host, assignment.owner.pid, assignment.owner.start_time)

        def confirm_adoption(received: placement.PlacementAssignment,
                             owner: placement.ClaimHolderIdentity) -> offload.MacClaimAdopted:
            self.assertEqual((received, owner), (assignment, holder))
            return offload.MacClaimAdopted()

        with (mock.patch.object(offload, "confirm_assigned_mac_adoption", side_effect=confirm_adoption),
              mock.patch.object(mac_test, "free_now", return_value="busy")):
            authorized = client.consume_permission(wire.ConsumePermission(uuid4().hex, assignment, reference, holder))
        self.assertIsInstance(authorized, wire.FirstAuthorization)
        outcome = self.mac_outcome
        status = 0 if isinstance(outcome, MacGateCompleted) and outcome.run.passed else 1
        reply = client.report_execution(wire.ExecutionReport(uuid4().hex, assignment, reference,
            execution_identity.ExecutionEvidenceUnavailable("fixture command ended", self.admission.clock.now()),
            placement.AttemptCompleted(status), placement.DispatchPathSettled()))
        self.assertEqual(reply.state, placement.ClosedAssignment("completed"))
        return outcome

    def pass_on_mac(self, *paths: str, status: int = 0) -> None:
        jobs = tuple(merge_gate.GateJobResult(Path(path).parent.name, Path(path).name, status, 1, "")
                     for path in paths)
        self.mac_outcome = MacGateCompleted(merge_gate.GateRun(jobs, 1, 0))

    def assert_request(self, target: str) -> wire.PlacementRequest:
        request, = self.requests
        self.assertEqual(request.constraints.package, "")
        self.assertEqual(request.constraints.command_kind, "run")
        self.assertEqual(tuple(item.destination.target for item in request.constraints.candidates), ("mac", "natedev"))
        with self.admission.loop_turn():
            selected, = [attempt.assignment.destination.target for attempt in self.admission.server.assignments.values()
                         if attempt.assignment.episode == request.episode]
        self.assertEqual(selected, target)
        return request

    def assert_local_modules(self, *names: str) -> None:
        self.assertCountEqual([row["module"] for row in self.module_events() if row["event"] == "start"], names)

    def assert_types_once(self) -> None:
        self.assertEqual(len([call for call in self.calls() if call["tool"] == "basedpyright"]), 1)

    @contextmanager
    def assigned_queue(self, case: MacTransportCase, *, output: str = "") -> Generator[tuple[str, str]]:
        shim = self.state / "assigned-queue.py"
        self.write(shim, "from scripts.production.test_merge_gate_suites_mac import run_queue_with_fixture_transport\n"
                   + "raise SystemExit(run_queue_with_fixture_transport())\n")
        command = (str(Path(__file__).resolve().parents[1] / "lib/py"), str(shim))
        self.write(self.state / "assigned-output", output)
        if case is MacTransportCase.STALLED_CLEANUP:
            write_stdlib_tool(self.state / "stalled-cleanup.py", "import signal\n"
                              + "from pathlib import Path\n"
                              + "Path(__file__).with_name('cleanup-entered').touch()\n"
                              + "signal.pause()\n")
            self.configure_gate("program_stop_seconds=1\n")
            _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "mac_gate.conf",
                                     "terminate_grace_seconds=1\n")
        tools = self.state / "assigned-tools"
        tools.mkdir()
        write_stdlib_tool(tools / "systemd-run", "import os\nimport sys\n"
                          + 'if "--no-block" in sys.argv:\n    raise SystemExit(0)\n'
                          + f"os.execv({str(self.bin / 'systemd-run')!r}, sys.argv)\n")
        environment = {
            "MAC_GATE_TRANSPORT_CASE": case.value, "TMPDIR": str(self.state),
            "PATH": str(tools) + os.pathsep + self.environment["PATH"],
        }
        self.controls.set_host("natedev", placement.TemporaryExclusion("blocked"))
        with (mock.patch.dict(self.environment, environment), mock.patch.dict(os.environ, environment),
              mock.patch.object(mac_gate_process, "load_mac_gate_route", return_value=mac_gate),
              mock.patch.object(mac_gate, "QUEUE_COMMAND", command)):
            yield command

    @contextmanager
    def assigned_gate_process(self, *, before_run: str = "", case: MacTransportCase = MacTransportCase.HELD,
                              launcher: tuple[str, ...] = ()) -> Generator[tuple[subprocess.Popen[bytes], Path]]:
        with self.assigned_queue(case) as queue:
            preamble = (
                "from scripts.aws_target import cloud_gate\n"
                + "from scripts.mac_test import mac_gate\n"
                + "cloud_gate.route = lambda *_args, **_kwargs: 'fixture cloud unavailable'\n"
                + f"mac_gate.QUEUE_COMMAND = {queue!r}\n"
                + before_run
            )
            command = [*launcher, *self.gate_command(preamble=preamble),
                       "--repo", str(self.checkout), "--tests-only"]
            log = self.state / "assigned-gate.log"
            with log.open("w") as output, subprocess.Popen(
                command, env=self.environment, cwd=self.root, stdin=subprocess.DEVNULL,
                stdout=output, stderr=output, start_new_session=True,
            ) as process:
                try:
                    yield process, log
                finally:
                    if process.poll() is None:
                        process.kill()
                    (self.state / "assigned-finish").touch()

    def assert_signal_cleans_assigned_run(self, number: signal.Signals, status: int) -> None:
        launcher = tuple(shlex.split(os.environ.get("MAC_GATE_FIXTURE_SIGNAL_LAUNCHER", "")))
        with self.assigned_gate_process(launcher=launcher) as (process, log):
            ready = self.state / "assigned-ready"
            wait_until(lambda: ready.exists() or process.poll() is not None, self.state, processes=(process,))
            self.assertTrue(ready.exists(), log.read_text())
            folder = Path(ready.read_text())
            grant = client.read_assignment(folder / "assignment.json")
            assert isinstance(grant, wire.Assignment)
            assignment = grant.assignment
            claim = offload.read_assigned_mac_claim(assignment)
            assert isinstance(claim, offload.AssignedMacClaim), claim
            self.assertEqual(assignment.owner.pid, process.pid)
            process.send_signal(number)
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), status, log.read_text())
            self.assert_process_stopped(claim.record["pid"])
            self.assertEqual(offload.read_assigned_mac_claim(assignment), offload.MacClaimSettled())
            self.assertEqual((self.state / "assigned-closed").read_text(), assignment.attempt)
            if number == signal.SIGTERM:
                self.assertFalse(folder.exists(), str(folder))
        self.assert_local_modules()

    @gate_suites("hooks")
    def test_sigint_during_assigned_mac_run_releases_the_claim_and_fails_the_gate(self) -> None:
        self.assert_signal_cleans_assigned_run(signal.SIGINT, 1)

    @gate_suites("hooks")
    def test_sigterm_reaches_the_assigned_mac_claim_and_removes_the_request(self) -> None:
        self.assert_signal_cleans_assigned_run(signal.SIGTERM, -signal.SIGTERM)

    @gate_suites("hooks")
    def test_malformed_assigned_mac_result_fails_the_gate(self) -> None:
        with self.assigned_queue(MacTransportCase.MALFORMED):
            result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Expecting property name", result.stderr)
        _ = self.assert_request("mac")
        self.assert_local_modules()

    @gate_suites("hooks")
    def test_large_assigned_mac_result_reaches_the_gate_whole(self) -> None:
        payload = "fixture module output\n" * (1 << 16)
        with self.assigned_queue(MacTransportCase.COMPLETE, output=payload):
            run = merge_gate.run_gate_suites(self.checkout)
        self.assertTrue(run.passed, self.output.getvalue())
        job, = run.jobs
        self.assertEqual((job.suite, job.module), ("hooks", "test_first.py"))
        self.assertEqual(job.output, payload)
        _ = self.assert_request("mac")
        self.assert_local_modules()

    def state_after_assigned_runner_exit(self) -> placement.AssignmentState:
        submitted, = self.requests
        with self.admission.loop_turn():
            server = self.admission.server
            key, = (key for key, saved in server.assignments.items()
                    if saved.assignment.episode == submitted.episode)
            saved = server.assignments[key]
            holder = saved.holder
            assert isinstance(holder, placement.ClaimHolderIdentity), holder
            self.assert_process_stopped(holder.pid)
            self.assertEqual(offload.read_assigned_mac_claim(saved.assignment), offload.MacClaimSettled())
            self.controls.advance(1)
            for reader in tuple(server.host_readers.values()):
                if reader.key.destination == saved.assignment.destination:
                    server.publish_host_evidence(self.controls.read(reader.key))
            _ = self.admission.run_pass()
            return server.assignments[key].state

    @gate_suites("hooks")
    def test_assignment_stays_completed_after_runner_exit_and_claim_release(self) -> None:
        decode = cloud_gate.decode_requested_run

        def settle_after_runner_exit(row: dict[str, object], request: dict[str, object]) -> merge_gate.GateRun:
            self.assertEqual(self.state_after_assigned_runner_exit(), placement.ClosedAssignment("completed"))
            return decode(row, request)

        with (self.assigned_queue(MacTransportCase.COMPLETE),
              mock.patch.object(cloud_gate, "decode_requested_run", side_effect=settle_after_runner_exit) as decode_result):
            run = merge_gate.run_gate_suites(self.checkout)

        decode_result.assert_called_once()
        self.assertTrue(run.passed, self.output.getvalue())
        self.assert_local_modules()

    @gate_suites("hooks")
    def test_cancel_between_claim_release_and_report_preserves_completion(self) -> None:
        with self.assigned_queue(MacTransportCase.CANCEL_COMPLETION):
            with self.assertRaises(GateRefused):
                _ = merge_gate.run_gate_suites(self.checkout)
        self.assertTrue((self.state / "assigned-before-report").exists(), self.output.getvalue())
        self.assertEqual(self.state_after_assigned_runner_exit(), placement.ClosedAssignment("completed"))
        self.assert_local_modules()

    @gate_suites("hooks")
    def test_sigterm_during_request_preparation_removes_the_request(self) -> None:
        hold_preparation = (
            "import os\n"
            "from pathlib import Path\n"
            "from scripts.direct.wait_for_test_state import wait_for_file\n"
            "from scripts.lib.atomic_file import write_text_atomic\n"
            "state = Path(os.environ['MERGE_GATE_TEST_STATE'])\n"
            "def prepare(_repo, folder, *_args, **_options):\n"
            "    write_text_atomic(state / 'assigned-preparing', str(folder))\n"
            "    wait_for_file(state / 'assigned-finish')\n"
            "    raise AssertionError('the gate did not stop during preparation')\n"
            "cloud_gate.prepare_request = prepare\n"
        )
        with self.assigned_gate_process(before_run=hold_preparation) as (process, log):
            marker = self.state / "assigned-preparing"
            wait_until(lambda: marker.exists() or process.poll() is not None, self.state, processes=(process,))
            self.assertTrue(marker.exists(), log.read_text())
            folder = Path(marker.read_text())
            self.assertTrue(folder.is_dir())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), -signal.SIGTERM, log.read_text())
            owner = self.state / "assigned-queue-pid"
            if owner.exists():
                self.assert_process_stopped(int(owner.read_text()))
            self.assertFalse(folder.parent.exists(), str(folder.parent))
        self.assert_local_modules()

    def recorded_assignment(self, attempt: str) -> placement.PlacementAssignment:
        with self.admission.loop_turn():
            saved, = (saved for saved in self.admission.server.assignments.values()
                      if saved.assignment.attempt == attempt)
            return saved.assignment

    @contextmanager
    def recover_fixture_mac(self, allowed: Path) -> Generator[None]:
        """Provide cleanup evidence to the admission worker after the barrier opens."""
        def cleanup(_script: str, phase: str, _argv: Path, arguments: list[str],
                    environment: dict[str, str], **_options: object) -> int:
            self.assertEqual(phase, "cleanup-ended")
            folder = Path(arguments[0])
            self.assertTrue(folder.is_dir())
            self.assertTrue(environment["MAC_QUEUE_HOST"])
            (self.state / "recovery-entered").touch()
            return int(not allowed.exists())

        def settle(_config: object, _host: str, _script: str, target: str, **_options: object) -> offload.LeftoverObservation:
            claims = claim_state.read_claims(claim_state.state_paths(target))
            claim, = claims
            identity = claim_state.claim_identity(claim.record)
            pending = offload.pending_start_path(target, identity)
            return offload.LeftoverObservation(offload.LeftoversEnded(),
                                                "dispatch_start_settled=" + pending.read_text().strip() + "\n")

        with (mock.patch.object(mac_queue_run, "run_callback", side_effect=cleanup),
              mock.patch.object(offload, "read_leftovers", side_effect=settle)):
            yield

    @gate_suites("hooks")
    def test_owner_start_failure_creates_no_gate_resources(self) -> None:
        with self.assigned_queue(MacTransportCase.UNAVAILABLE_OWNER):
            with self.assertRaisesRegex(GateRefused, "fixture admission service is unavailable"):
                _ = merge_gate.run_gate_suites(self.checkout)
        attempt = (self.state / "owner-unavailable").read_text()
        assignment = self.recorded_assignment(attempt)
        paths = mac_gate.assigned_gate_paths(assignment.attempt)
        self.assertFalse(paths.request_root.exists())
        self.assertFalse(paths.directory.exists())
        self.assertEqual(offload.read_assigned_mac_claim(assignment), offload.MacClaimSettled())

    @gate_suites("hooks")
    def test_admission_restart_recovers_a_dead_runner_after_an_empty_claim_watch(self) -> None:
        allowed = self.state / "allow-recovery"
        allowed.touch()
        paths = claim_state.state_paths("mac")
        self.assertFalse(mac_test.claim_watch_pass(paths, mac_test.read_config()))
        with self.recover_fixture_mac(allowed), self.assigned_gate_process() as (gate_process, log):
            marker = self.state / "assigned-ready"
            wait_until(lambda: marker.exists() or gate_process.poll() is not None, self.state,
                       processes=(gate_process,))
            self.assertTrue(marker.exists(), log.read_text())
            request = Path(marker.read_text())
            grant = client.read_assignment(request / "assignment.json")
            assert isinstance(grant, wire.Assignment)
            assignment = grant.assignment
            claim = offload.read_assigned_mac_claim(assignment)
            assert isinstance(claim, offload.AssignedMacClaim), claim
            with self.admission.loop_turn():
                _ = self.admission.run_pass()
                self.assertFalse((self.state / "recovery-entered").exists())
                self.assertTrue(mac_gate.assigned_gate_paths(assignment.attempt).journal.exists())
                os.kill(claim.record["pid"], signal.SIGKILL)
                self.assert_process_stopped(claim.record["pid"])
                provider = self.admission.server.host_probe
                self.admission.restart()
                self.admission.server.host_probe = provider
            self.assertEqual(gate_process.wait(timeout=TEST_HANG_GUARD_SECS), 1, log.read_text())
            wait_until(lambda: not request.parent.exists(), request.parent.parent)
            self.assertTrue((self.state / "recovery-entered").exists())
            self.assertEqual(offload.read_assigned_mac_claim(assignment), offload.MacClaimSettled())
            status = client.placement_status(assignment.episode, assignment.attempt).status
            assert isinstance(status, status_wire.KnownPlacementStatus)
            self.assertEqual(status.state, placement.ClosedAssignment("cancelled"))
        self.assert_local_modules()

    @gate_suites("hooks")
    def test_admission_recovers_claim_subprocess_paused_before_write(self) -> None:
        self.assert_admission_recovers_claim_subprocess(ClaimWriteBarrier.BEFORE)

    @gate_suites("hooks")
    def test_admission_recovers_claim_subprocess_paused_after_write(self) -> None:
        self.assert_admission_recovers_claim_subprocess(ClaimWriteBarrier.AFTER)

    def assert_admission_recovers_claim_subprocess(self, barrier: ClaimWriteBarrier) -> None:
        self.write(self.state / "claim-at-write.py",
                   "from scripts.production.test_merge_gate_suites_mac import run_claim_at_write_barrier\n"
                   + "raise SystemExit(run_claim_at_write_barrier())\n")
        marker = self.state / "claim-at-write"
        release = self.state / "allow-claim-write"
        checked = mac_gate.queue_runs_directory() / "claim-creator-checked"
        queue_creators_ended = mac_queue_run.queue_creators_ended
        callback = mac_queue_run.run_callback

        def check_creators(owner: mac_queue_run.SavedGateOwner, *, allowed: tuple[int, ...] = ()) -> bool:
            result = queue_creators_ended(owner, allowed=allowed)
            if not result:
                checked.touch()
            return result

        with (mock.patch.dict(self.environment, {"MAC_GATE_CLAIM_BARRIER": barrier.value}),
              mock.patch.object(mac_queue_run, "queue_creators_ended", side_effect=check_creators),
              mock.patch.object(mac_queue_run, "run_callback", wraps=callback) as recovered_callback,
              mock.patch.object(mac_queue_run, "save_assigned_outcome", wraps=mac_queue_run.save_assigned_outcome)
              as recovered_report,
              self.assigned_gate_process(case=MacTransportCase.CLAIM_PUBLICATION) as (gate_process, log)):
            try:
                wait_until(lambda: marker.exists() or gate_process.poll() is not None, self.state,
                           processes=(gate_process,))
                self.assertTrue(marker.exists(), log.read_text())
                attempt, creator_pid = marker.read_text().split()
                creator = int(creator_pid)
                assignment = self.recorded_assignment(attempt)
                paths = mac_gate.assigned_gate_paths(assignment.attempt)
                run = mac_queue_run.read_registered_mac_run(assignment)
                assert isinstance(run, mac_queue_run.RegisteredMacRun)
                claim = offload.read_assigned_mac_claim(assignment)
                if barrier is ClaimWriteBarrier.AFTER:
                    assert isinstance(claim, offload.AssignedMacClaim), claim
                    self.assertTrue(claim.held_by(assignment, run.holder))
                else:
                    self.assertEqual(claim, offload.MacClaimSettled())
                self.assertIs(run.saved.get("lifecycle_started"), False)
                os.kill(run.holder.pid, signal.SIGKILL)
                self.assert_process_stopped(run.holder.pid)
                with self.admission.loop_turn():
                    provider = self.admission.server.host_probe
                    self.admission.restart()
                    self.admission.server.host_probe = provider
                wait_until(lambda: checked.exists() or not paths.journal.exists(), paths.journal.parent,
                           watch_writes=True)
                self.assertIsInstance(process_start(creator), Started)
                self.assertTrue(paths.journal.exists(), "Recovery discarded the plan while claim creation was possible")
                self.assertEqual(offload.read_assigned_mac_claim(assignment), claim)
            finally:
                release.touch()
                if marker.exists():
                    self.assert_process_stopped(int(marker.read_text().split()[1]))
            self.assertEqual(gate_process.wait(timeout=TEST_HANG_GUARD_SECS), 1, log.read_text())
            wait_until(lambda: not paths.journal.exists(), paths.journal.parent)
            self.assertFalse(paths.request_root.exists())
            reference = placement.assigned_claim_reference(assignment)
            self.assertEqual((self.state / "claim-written").read_text(), reference.claim_id)
            self.assertEqual(offload.read_assigned_mac_claim(assignment), offload.MacClaimSettled())
            status = client.placement_status(assignment.episode, assignment.attempt).status
            assert isinstance(status, status_wire.KnownPlacementStatus)
            self.assertEqual(status.state, placement.ClosedAssignment("cancelled"))
            recovered_callback.assert_not_called()
            recovered_report.assert_called_once()
            self.assertEqual(recovered_report.call_args.args[-1], execution_identity.ExecutionNotStarted())
        self.assert_local_modules()

    @gate_suites("hooks")
    def test_admission_restart_recovers_manual_gate_before_placement(self) -> None:
        manual = (
            "from scripts.production import merge_gate as gate\n"
            + "result = mac_gate.run_remote(Path(sys.argv[2]), mode=gate.SuiteMode.MERGE_GATE, full=True, "
            + "module_time_limit_s=30, selected=gate.ALL_MODULES, "
            + "cancellation=mac_gate.USE_CALLER_PROCESS_LIFETIME)\n"
            + "print(result)\n"
            + "raise SystemExit(1 if isinstance(result, str) else 0)\n"
        )
        marker = self.state / "manual-before-placement"
        with self.assigned_gate_process(before_run=manual,
                                        case=MacTransportCase.MANUAL_BEFORE_PLACEMENT) as (process, log):
            wait_until(lambda: marker.exists() or process.poll() is not None, self.state, processes=(process,))
            self.assertTrue(marker.exists(), log.read_text())
            episode, request_path = cast(list[str], json.loads(marker.read_text()))
            request = Path(request_path)
            runs = mac_gate.queue_runs_directory()
            journal = runs / f"{episode}.json"
            self.assertTrue(request.is_dir())
            registered = journal.is_file()
            with self.admission.loop_turn():
                self.assertFalse(self.admission.server.assignments)
                runner = int((self.state / "assigned-queue-pid").read_text())
                os.kill(runner, signal.SIGKILL)
                self.assert_process_stopped(runner)
                provider = self.admission.server.host_probe
                self.admission.restart()
                self.admission.server.host_probe = provider
            self.assertEqual(process.wait(timeout=TEST_HANG_GUARD_SECS), 1, log.read_text())
            self.assertTrue(registered, "The manual request had no recovery owner when the runner died")
            wait_until(lambda: not journal.exists(), runs)
            self.assertFalse(request.parent.exists())
            self.assertFalse((runs / episode).exists())
            self.assertFalse((Path.home() / mac_queue_run.RECEIPTS / f"{episode}.json").exists())

    @gate_suites("hooks")
    def test_stalled_cleanup_preserves_exact_state_and_recovers_without_launching(self) -> None:
        allowed = self.state / "allow-recovery"
        route = mac_gate.run_assigned
        outcomes: list[MacGateCompleted | MacGatePrelaunchRefused | MacGateUnresolved] = []

        def capture(repo: Path, *, assignment: placement.PlacementAssignment, mode: SuiteMode, full: bool,
                    module_time_limit_s: float, selected: TreeSelection
                    ) -> MacGateCompleted | MacGatePrelaunchRefused | MacGateUnresolved:
            result = route(repo, assignment=assignment, mode=mode, full=full,
                           module_time_limit_s=module_time_limit_s, selected=selected)
            outcomes.append(result)
            return result

        with (self.recover_fixture_mac(allowed), self.assigned_queue(MacTransportCase.STALLED_CLEANUP),
              mock.patch.object(mac_gate, "run_assigned", side_effect=capture)):
            with self.assertRaisesRegex(GateRefused, "timed out"):
                _ = merge_gate.run_gate_suites(self.checkout)
            self.assertIsInstance(outcomes[0], MacGateUnresolved)
            self.assertTrue((self.state / "cleanup-entered").exists())
            attempt = (self.state / "assigned-streamed").read_text()
            assignment = self.recorded_assignment(attempt)
            paths = mac_gate.assigned_gate_paths(assignment.attempt)
            self.assertTrue(paths.journal.exists())
            self.assertTrue(paths.request.is_dir())
            claim = offload.read_assigned_mac_claim(assignment)
            assert isinstance(claim, offload.AssignedMacClaim), claim
            identity = claim_state.claim_identity(claim.record)
            self.assertTrue(offload.pending_start_path(assignment.destination.target, identity).exists())
            allowed.touch()
            wait_until(lambda: not paths.journal.exists(), paths.journal.parent)
            status = client.placement_status(assignment.episode, assignment.attempt).status
            assert isinstance(status, status_wire.KnownPlacementStatus)
            self.assertEqual(status.state, placement.ClosedAssignment("completed"))
            self.assertEqual(offload.read_assigned_mac_claim(assignment), offload.MacClaimSettled())
        self.assert_local_modules()

    @gate_suites("lib")
    def test_gate_with_exclusions_reuses_an_existing_full_pass(self) -> None:
        self.exclude_modules(LIB_FIRST)
        local = merge_gate.run_suites(self.checkout)
        self.assertTrue(local.passed, self.output.getvalue())
        record, = self.pass_records()
        self.assertEqual(record["kind"], "full")
        self.clear_gate_events()

        run = merge_gate.run_gate_suites(self.checkout)

        self.assertTrue(run.tests_reused, self.output.getvalue())
        self.assertIs(run.coverage, ModuleCoverage.COMPLETE)
        self.assertEqual(self.pass_records(), [record])
        self.assertEqual(self.requests, [])
        self.assert_local_modules()

    @gate_suites("hooks", "message")
    def test_full_gate_does_not_reuse_a_one_module_split(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "merge_gate.conf", "scan_roots=\n")
        self.assertTrue(merge_gate.run_suites(self.checkout).passed, self.output.getvalue())
        baseline, = self.pass_records()
        self.clear_gate_events()
        self.exclude_modules(MESSAGE)
        module = self.checkout / HOOKS
        self.write(module, module.read_text() + "\n# Change the hooks test.\n")
        _ = self.commit(self.checkout, "Change one test module")
        self.pass_on_mac(HOOKS)
        self.controls.set_host("natedev", placement.TemporaryExclusion("blocked"))
        split = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(split.passed, self.output.getvalue())
        self.assertIs(split.coverage, ModuleCoverage.SUBSET)
        _ = self.assert_request("mac")
        self.assertEqual(self.mac_runs[0][1].paths, (HOOKS,))
        self.assertEqual(self.pass_records(), [baseline])
        self.assert_local_modules()

        self.exclude_modules(HOOKS, MESSAGE)
        self.controls.set_host("natedev", placement.AllowedHost())
        full = merge_gate.run_gate_suites(self.checkout)

        self.assertTrue(full.passed, self.output.getvalue())
        self.assertFalse(full.tests_reused)
        self.assert_all_modules_started()
        _, record = self.pass_records()
        self.assertEqual((record["kind"], record["tree"]), ("full", self.mac_runs[0][1].tree))

    @gate_suites("lib")
    def test_merge_gate_cli_submits_one_request_after_aws_declines(self) -> None:
        self.exclude_modules(LIB_FIRST)
        self.pass_on_mac(LIB_SECOND)
        self.controls.set_host("natedev", placement.TemporaryExclusion("blocked"))

        def check_aws() -> None:
            self.cloud_route.assert_called_once()
            self.assertIs(cast(object, self.cloud_route.call_args.kwargs["selected"]), merge_gate.ALL_MODULES)
            self.assertIs(cast(object, self.cloud_route.call_args.kwargs["mode"]), SuiteMode.MERGE_GATE)
            self.controls.set_host("natedev", placement.AllowedHost())

        self.before_mac = check_aws
        result = self.run_gate("--tests-only")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        _ = self.assert_request("mac")
        self.assertEqual(self.mac_runs[0][1].paths, (LIB_SECOND,))
        self.assert_local_modules("test_first.py")
        self.assert_gate_released()

    @gate_suites("lib")
    def test_targeted_cli_uses_the_mac_ahead_of_an_older_natedev_only_request(self) -> None:
        self.pass_on_mac(LIB_FIRST, LIB_SECOND)
        self.controls.set_host("natedev", placement.TemporaryExclusion("blocked"))
        submitted = placement_request(self.admission, since=self.admission.clock.now() - 10)
        older = replace(submitted, constraints=replace(submitted.constraints,
                        candidates=(local_candidate(self.admission),)))
        self.assertIsInstance(client.request_placement(older), wire.PlacementWait)

        def check_order() -> None:
            with self.admission.loop_turn():
                self.assertIn(older.episode, self.admission.server.waiters)

        self.before_mac = check_order
        try:
            status = targeted_test.main(["--repo", str(self.checkout)])
        finally:
            _ = client.cancel_placement(wire.CancelPlacement(uuid4().hex, older.episode, older.owner, "fixture ended"))
        self.assertEqual(status, 0, self.output.getvalue())
        request = self.assert_request("mac")
        self.assertGreater(request.since, older.since)
        self.assert_local_modules()
        self.cloud_route.assert_not_called()

    @gate_suites("lib")
    def test_merge_gate_uses_natedev_for_mac_hold_and_backoff(self) -> None:
        config = Path(self.environment["CONF_DIR"])
        for reason in ("blocked", "backoff"):
            with self.subTest(reason=reason):
                self.requests.clear()
                self.clear_gate_events()
                self.exclude_modules(LIB_FIRST)
                self.controls.set_host("mac", placement.TemporaryExclusion(reason))
                _ = write_config_fixture(config / "build_admission.conf", "program_first_peak_gib.merge-gate=0.5")
                run = merge_gate.run_gate_suites(self.checkout, ("scripts",), full=True)
                self.assertTrue(run.passed, self.output.getvalue())
                _ = self.assert_request("natedev")
                self.assertEqual(self.mac_runs, [])
                self.assert_all_modules_started()
                self.assert_types_once()
                self.assert_gate_released()

    @gate_suites("lib")
    def test_local_gate_keeps_a_populated_scope_after_cleanup_expires(self) -> None:
        self.controls.set_host("mac", placement.TemporaryExclusion("blocked"))
        scope = self.state / "scope"
        scope.mkdir()
        self.write(scope / "cgroup.procs", f"{os.getpid()}\n")

        def local(*_args: object, admitted: queue_place.QueuePlace, **_kwargs: object) -> merge_gate.GateRun:
            self.write(admitted.sidecar, str(scope))
            return merge_gate.GateRun((), 0, 0)

        with (mock.patch.object(merge_gate, "run_suites", side_effect=local),
              mock.patch.object(queue_place.QueuePlace, "finish", side_effect=TimeoutError("cleanup expired")),
              self.assertRaisesRegex(TimeoutError, "cleanup expired")):
            _ = merge_gate.run_gate_suites(self.checkout, full=True)
        request = self.assert_request("natedev")
        self.assertIsInstance(request.local, wire.LocalAdmissionContext)
        assert isinstance(request.local, wire.LocalAdmissionContext)
        with self.admission.loop_turn():
            record = self.admission.server.reservations[request.episode]
            self.assertEqual(memory_admit.reservation_lifetime(admission_service.reservation_row(record), False),
                             memory_admit.PopulatedStepScope(scope), "The gate discarded a populated scope")

    @gate_suites("lib")
    def test_local_gate_replays_the_saved_result_after_unconfirmed_release(self) -> None:
        self.controls.set_host("mac", placement.TemporaryExclusion("blocked"))

        def local(*_args: object, admitted: queue_place.QueuePlace, **_kwargs: object) -> merge_gate.GateRun:
            admitted.observe(1 << 30, 1 << 30)
            job = merge_gate.GateJobResult("lib", "test_first.py", 1, 1, "")
            return merge_gate.GateRun((job,), 1, 0)

        with (mock.patch.object(merge_gate, "run_suites", side_effect=local),
              mock.patch.object(client, "release", side_effect=wire.AdmissionUnavailable("release unavailable")),
              self.assertRaisesRegex(GateRefused, "release unavailable")):
            _ = merge_gate.run_gate_suites(self.checkout, full=True)
        request = self.assert_request("natedev")
        assert isinstance(request.local, wire.LocalAdmissionContext)
        assignment = Path(request.local.sidecar).with_suffix(".assignment.json")
        self.assertTrue(assignment.is_file(), "The gate discarded its unconfirmed assignment")

        with self.admission.loop_turn():
            self.admission.proc.fixture_owners[request.owner.pid] = "ended"
            self.admission.restart()
            _ = self.admission.run_pass()
            saved, = self.admission.server.assignments.values()
            self.assertIsInstance(saved.terminal_report, placement.TerminalAttemptReport)
            assert isinstance(saved.terminal_report, placement.TerminalAttemptReport)
            self.assertEqual(saved.terminal_report.outcome, placement.AttemptCompleted(1))
            self.assertEqual(saved.state, placement.ClosedAssignment("completed"))
            self.assertFalse(assignment.exists())
            self.assert_gate_released()
        row, = self.gate_history()
        self.assertEqual(row["status"], 1)

    @gate_suites("lib")
    def test_local_placement_bounds_gate_job_reaping_and_retains_the_group(self) -> None:
        self.controls.set_host("mac", placement.TemporaryExclusion("blocked"))
        config = Path(self.environment["CONF_DIR"]) / "build_admission.conf"
        _ = write_config_fixture(config, "program_stop_seconds=0.2\nprogram_sample_seconds=0.02\n")
        wait4 = os.wait4
        child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"],
                                 start_new_session=True, text=True)
        def stalled(pid: int, flags: int) -> tuple[int, int, object]:
            if pid == child.pid:
                self.assertEqual(flags, os.WNOHANG, "The placed gate used a blocking reap")
                return 0, 0, None
            return wait4(pid, flags)

        def local(*_args: object, **_kwargs: object) -> merge_gate.GateRun:
            with merge_gate.gate_recovery(self.checkout) as journal, (self.state / "held-job").open("w+") as output:
                journal.add_group(child.pid)
                self.assertIn(child.pid, journal.groups)
                job = merge_gate.RunningGateJob("lib", self.checkout / LIB_FIRST, child, 0, 0,
                                               output, Path(output.name), LIB_FIRST)
                try:
                    with mock.patch.object(os, "wait4", side_effect=stalled):
                        merge_gate.stop_gate_jobs((job,))
                finally:
                    self.assertIn(child.pid, journal.groups)
            raise AssertionError("The placed gate returned without confirmation")

        try:
            with (mock.patch.object(merge_gate, "run_suites", side_effect=local),
                  self.assertRaisesRegex(TimeoutError, "deadline passed")):
                _ = merge_gate.run_gate_suites(self.checkout, full=True)
            request = self.assert_request("natedev")
            with self.admission.loop_turn():
                self.assertIn(request.episode, self.admission.server.reservations)
        finally:
            if child.poll() is None:
                child.kill()
            _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    @gate_suites("lib")
    def test_mac_winner_runs_only_eligible_modules_and_local_types_run_once(self) -> None:
        self.exclude_modules(LIB_FIRST)
        self.pass_on_mac(LIB_SECOND)
        self.controls.set_host("natedev", placement.TemporaryExclusion("blocked"))

        def check_before_mac() -> None:
            self.assert_local_modules()
            self.controls.set_host("natedev", placement.AllowedHost())

        self.before_mac = check_before_mac
        run = merge_gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(run.passed, self.output.getvalue())
        _ = self.assert_request("mac")
        self.assertEqual(self.mac_runs[0][1].paths, (LIB_SECOND,))
        self.assert_local_modules("test_first.py")
        self.assert_types_once()
        self.assertEqual(self.pass_records(), [])
        self.assert_gate_released()

    @gate_suites("lib")
    def test_red_or_missing_mac_result_does_not_launch_the_assigned_modules_on_natedev(self) -> None:
        self.controls.set_host("natedev", placement.TemporaryExclusion("blocked"))
        for missing in (False, True):
            with self.subTest(missing=missing):
                self.requests.clear()
                self.pass_on_mac(LIB_FIRST, LIB_SECOND, status=1)
                if missing:
                    self.mac_outcome = MacGateUnresolved("The fixture lost the result after launch")
                status = targeted_test.main(["--repo", str(self.checkout)])
                self.assertEqual(status, 1, self.output.getvalue())
                _ = self.assert_request("mac")
                self.assert_local_modules()
                self.assert_gate_released()

    @gate_suites("lib")
    def test_settled_prelaunch_refusal_keeps_the_episode_and_records_a_full_local_pass(self) -> None:
        self.mac_outcome = MacGatePrelaunchRefused("fixture Mac hold")
        self.controls.set_host("natedev", placement.TemporaryExclusion("blocked"))

        def free_natedev() -> None:
            self.controls.set_host("natedev", placement.AllowedHost())

        self.before_mac = free_natedev
        run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(run.passed, self.output.getvalue())
        first, second = self.requests
        self.assertIs(first, second)
        self.assertEqual(self.mac_runs[0][0].episode, first.episode)
        self.assert_all_modules_started()
        record, = self.pass_records()
        self.assertEqual(record["kind"], "full")
        self.clear_gate_events()
        reused = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(reused.tests_reused, self.output.getvalue())
        self.assertEqual(self.requests, [first, second])
        self.assert_local_modules()

    @gate_suites("hooks")
    def test_missing_mac_route_preserves_the_cloud_fallback(self) -> None:
        with mock.patch.object(mac_gate_process, "load_mac_gate_route",
                               side_effect=ModuleNotFoundError(name=mac_gate_process.MAC_GATE_MODULE)):
            run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(run.passed, self.output.getvalue())
        tree, _ = cloud_gate.working_tree(self.checkout)
        self.assertEqual(cast(object, self.cloud_route.call_args.kwargs["selected"]), TreeSelection((HOOKS,), tree))
        self.assertEqual(self.requests, [])

    @gate_suites("hooks")
    def test_missing_exclude_file_preserves_the_cloud_route(self) -> None:
        excluded = self.checkout / mac_gate.MODULE_LIST
        excluded.unlink()
        hooks = merge_gate.GateJobResult("hooks", "test_first.py", 0, 1, "")
        self.cloud_route.return_value = merge_gate.GateRun((hooks,), 1, 0)
        is_file = Path.is_file

        def avoid_probe(path: Path, *, follow_symlinks: bool = True) -> bool:
            self.assertNotEqual(path, excluded)
            return is_file(path, follow_symlinks=follow_symlinks)

        with mock.patch.object(Path, "is_file", avoid_probe):
            run = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertEqual(run.jobs, (hooks,))
        self.cloud_route.assert_called_once()
        self.assertEqual(self.requests, [])

    @gate_suites("direct", "hooks", "message")
    def test_targeted_gate_places_only_affected_modules_and_runs_types_once(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "merge_gate.conf", "scan_roots=\n")
        baseline = merge_gate.run_suites(self.checkout)
        self.assertTrue(baseline.passed, self.output.getvalue())
        passes = self.pass_records()
        self.clear_gate_events()
        for path in (HOOKS, MESSAGE):
            module = self.checkout / path
            self.write(module, module.read_text() + "\n# Change the module.\n")
        _ = self.commit(self.checkout, "Change two test modules")
        self.exclude_modules(MESSAGE)
        self.pass_on_mac(HOOKS)
        self.controls.set_host("natedev", placement.TemporaryExclusion("blocked"))

        def free_natedev() -> None:
            self.controls.set_host("natedev", placement.AllowedHost())

        self.before_mac = free_natedev
        run = merge_gate.run_gate_suites(self.checkout, ("scripts",), mode=SuiteMode.TARGETED_TEST)
        self.assertTrue(run.passed, self.output.getvalue())
        _ = self.assert_request("mac")
        self.assertEqual(self.mac_runs[0][1].paths, (HOOKS,))
        self.assertEqual([row["suite"] for row in self.module_events() if row["event"] == "start"], ["message"])
        self.assert_types_once()
        self.assertEqual(self.pass_records(), passes)

    @gate_suites("hooks", "message")
    def test_all_excluded_affected_modules_reject_a_checkout_edit_during_the_run(self) -> None:
        _ = write_config_fixture(Path(self.environment["CONF_DIR"]) / "merge_gate.conf", "scan_roots=\n")
        baseline = merge_gate.run_suites(self.checkout)
        self.assertTrue(baseline.passed, self.output.getvalue())
        self.clear_gate_events()
        selected = self.checkout / HOOKS
        self.write(selected, selected.read_text() + f"""
class CheckoutEdit(unittest.TestCase):
    def test_edit_the_checkout(self):
        with Path({MESSAGE!r}).open("a") as output:
            output.write("\\n# The checkout changed during the test.\\n")
""")
        _ = self.commit(self.checkout, "Add a test that edits the checkout")
        self.exclude_modules(HOOKS)
        with self.assertRaisesRegex(GateRefused, "The checkout changed during the gate"):
            _ = merge_gate.run_gate_suites(self.checkout, mode=SuiteMode.TARGETED_TEST)
        self.assertEqual(self.requests, [])
        self.assertEqual([row["suite"] for row in self.module_events() if row["event"] == "start"], ["hooks"])

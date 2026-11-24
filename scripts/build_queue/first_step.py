"""Hold first-step admission while a caller prepares its repository slot."""
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue import execution_identity, placement, queue_place
from scripts.build_targets import target_config
from scripts.buildlog import store, treekey
from scripts.direct.step_selection import Pinned
from scripts.lib import build_folders, conf
from scripts.lib.build_slots import SlotQueuePosition
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, StepJob
from scripts.build_queue.admission_wire import KnownPlacementStatus, read_assignment
from scripts.lib.build_steps import (
    CLAIM_FAILURE_SUFFIX, GATE_PRELAUNCH_REFUSED_STATUS, GATE_SCRIPTS, GATE_UNSETTLED_LAUNCH_STATUS,
    OtherCommand, VerifyCommand, command_job, command_program, gate_job_from_process, parse_command_program,
    read_step_names, verify_command,
)
from scripts.mac_test import claim_state


_GATE_PASS_STATUS = 0


class FirstStepRefused(RuntimeError):
    """The command did not receive permission to take a slot."""

    def __init__(self, reason: str, exit_code: int = 2) -> None:
        super().__init__(reason)
        self.exit_code: int = exit_code


class PlacementExhausted(FirstStepRefused):
    """The queue has no remaining candidate for this request."""


@dataclass(frozen=True)
class HandedOff:
    """The command now owns the next admission of this run."""


def _gate_configuration() -> records.MemoryConfig:
    return records.read_memory_config(report=False)


@dataclass(frozen=True)
class OwnedGatePlacement:
    """This caller owns placement, retry, and cleanup."""

    grant: wire.LocalPlacementGranted | wire.Assignment
    request: wire.PlacementRequest
    configuration: records.MemoryConfig = field(default_factory=_gate_configuration)


@dataclass(frozen=True)
class InheritedGatePlacement:
    """The live wrapper owns retry and cleanup."""

    grant: wire.LocalPlacementGranted | wire.Assignment


GatePlacement = OwnedGatePlacement | InheritedGatePlacement


def is_gate_placement_exit(status: int) -> bool:
    return status in (GATE_PRELAUNCH_REFUSED_STATUS, GATE_UNSETTLED_LAUNCH_STATUS)


def close_gate_preserving_status(cleanup: Callable[[], None]) -> None:
    """Log cleanup errors after a gate placement exit."""
    try:
        cleanup()
    except Exception as error:
        print(f'Gate cleanup failed. {error}', file=sys.stderr, flush=True)


@dataclass
class FirstStepAdmission:
    """Keep the preparation grant until the command can start."""

    queue_position: SlotQueuePosition
    grant: wire.Admitted | wire.LocalPlacementGranted | wire.Assignment | GatePlacement | HandedOff
    assignment_file: Path
    _guest_end_report_failed: bool = field(default=False, init=False)
    preserve_exit_status: bool = field(default=False, init=False)

    def handoff(self, tree: Path) -> None:
        """End preparation or pass the assignment to the command."""
        os.environ['BUILDLOG_PREPARED_COPY'] = str(tree)
        grant = self.grant
        if isinstance(grant, GatePlacement):
            self._hand_gate(grant)
            return
        if isinstance(grant, wire.Admitted):
            answer = queue_place.release_place(grant.reservation, 0, grant.cleanup_timing, 'handed_off')
            if isinstance(answer, wire.Refused):
                raise FirstStepRefused(answer.reason)
        elif isinstance(grant, wire.LocalPlacementGranted):
            _ = client.finish_step(self.assignment_file, 0, ending='handed_off')
        elif isinstance(grant, wire.Assignment):
            destination = grant.assignment.destination
            os.environ['BUILDLOG_FIRST_STEP_ASSIGNMENT'] = str(self.assignment_file)
            os.environ['BUILDLOG_FIRST_STEP_SINCE'] = str(self.queue_position.since)
            os.environ['BUILDLOG_FIRST_STEP_ROUTE'] = '\t'.join((destination.provider, destination.target,
                destination.host, str(destination.slot), grant.assignment.episode, grant.assignment.attempt))
            return
        self.grant = HandedOff()

    def _hand_gate(self, grant: GatePlacement) -> None:
        # A local grant keeps its reservation, so the queue reads the assignment as current when the gate checks it.
        # The gate uses the reservation for its suites. The cancel returns an unused reservation.
        os.environ['BUILDLOG_PLACEMENT_FILE'] = str(self.assignment_file)
        os.environ['BUILDLOG_PLACEMENT_EPISODE'] = grant.grant.assignment.episode
        if isinstance(grant, OwnedGatePlacement) and isinstance(grant.request.local, wire.LocalAdmissionContext):
            os.environ['BUILDLOG_PLACEMENT_SIDECAR'] = grant.request.local.sidecar

    def place_again(self, request: wire.PlacementRequest) -> None:
        """Place the gate again with its original request and pass the new assignment to the next run."""
        grant = self.grant
        if not isinstance(grant, OwnedGatePlacement) or grant.request != request:
            raise wire.AdmissionUnavailable('only the original gate request can be placed again')
        self.grant = replace(grant, grant=_placement_grant(request))
        client.write_assignment(self.assignment_file, self.grant.grant)
        self._hand_gate(self.grant)

    def run_gate(self, run: Callable[[], subprocess.CompletedProcess[str]]) -> subprocess.CompletedProcess[str]:
        """Run the gate and retry only when the queue permits a new attempt."""
        grant = self.grant
        result = (run_gate_with_placement(run, self.assignment_file, grant.request, self.place_again)
                  if isinstance(grant, OwnedGatePlacement) else run())
        self.preserve_exit_status = is_gate_placement_exit(result.returncode)
        self.report_gate_end(result.returncode)
        return result

    def report_gate_end(self, status: int) -> None:
        """Report a pass from the guest gate child."""
        grant = self.grant
        if not isinstance(grant, OwnedGatePlacement) or status != _GATE_PASS_STATUS:
            return
        try:
            assignment = read_assignment(self.assignment_file).assignment
            if assignment.destination.provider != 'ssh' or assignment.destination.target != 'mac-linux':
                return
            if not assignment_matches_request(assignment, grant.request):
                raise wire.AdmissionUnavailable('the saved assignment does not match the gate request')
            config = grant.configuration
            timing = client.OwnerEndTiming(config.query_timeout_seconds, config.reconnect_seconds,
                                           config.unreachable_limit_seconds)
            client.report_owner_end(wire.OwnerEnd(uuid4().hex, assignment, grant.request.owner, status), timing)
        except (OSError, ValueError, wire.AdmissionUnavailable) as error:
            if isinstance(error, TimeoutError):
                # The queue checks the assignment when the owner exits.
                self.grant = HandedOff()
            self._guest_end_report_failed = True
            with suppress(OSError, ValueError):
                reason = ' '.join(str(error).split())
                print(f'Mac Linux gate owner end report failed: {reason}', file=sys.stderr, flush=True)
        else:
            self.grant = HandedOff()

    def close(self) -> None:
        """Release preparation after an error before command startup."""
        grant = self.grant
        if isinstance(grant, wire.Admitted):
            _ = queue_place.release_place(grant.reservation, 1, grant.cleanup_timing)
        elif isinstance(grant, wire.LocalPlacementGranted):
            _ = client.finish_step(self.assignment_file, 1, 'slot preparation failed')
        elif isinstance(grant, (wire.Assignment, OwnedGatePlacement)):
            assignment = grant.grant.assignment if isinstance(grant, OwnedGatePlacement) else grant.assignment
            reason = 'the gate caller ended' if isinstance(grant, OwnedGatePlacement) else 'slot preparation failed'
            configuration = grant.configuration if isinstance(grant, OwnedGatePlacement) else client.READ_CONFIGURATION_AT_USE
            try:
                _ = client.cancel_placement(wire.CancelPlacement(uuid4().hex, assignment.episode,
                    assignment.owner, reason), configuration=configuration)
            except wire.AdmissionUnavailable as error:
                if error.reason != wire.PLACEMENT_REQUEST_MISSING_REASON and not self._guest_end_report_failed:
                    raise
        self.grant = HandedOff()


def _verify_request(command: VerifyCommand, source: Path, folder: Path, owner: wire.Owner,
                    position: SlotQueuePosition) -> wire.PlacementRequest:
    request_file = folder / 'request.json'
    environment = {**os.environ, 'UNIT_DETACHED_RUN': '1',
        'BUILDLOG_FIRST_STEP_REQUEST': str(request_file), 'BUILDLOG_FIRST_STEP_PID': str(owner.pid),
        'BUILDLOG_FIRST_STEP_EPISODE': position.episode, 'BUILDLOG_FIRST_STEP_SINCE': str(position.since),
        'BUILDLOG_MEM_ADMIT_SCRIPT': str(Path(__file__).with_name('admission_client.py'))}
    script = Path(__file__).resolve().parents[1] / 'direct/verify.sh'
    arguments = [word.replace('{tree}', str(source)) for word in command.arguments]
    result = subprocess.run(['bash', str(script), *arguments], cwd=source, env=environment)
    if result.returncode:
        raise FirstStepRefused('the first verify step could not prepare its admission request', result.returncode)
    if not request_file.is_file():
        raise FirstStepRefused('verify did not produce a first-step admission request')
    request = wire.parse_request(request_file.read_text())
    if not isinstance(request, wire.PlacementRequest):
        raise FirstStepRefused('verify produced an invalid first-step admission request')
    return request


def _placement_grant(request: wire.PlacementRequest) -> wire.LocalPlacementGranted | wire.Assignment:
    answer = queue_place.wait_for_placement(request, 'the caller left before first-step admission')
    if isinstance(answer, client.LeftQueue):
        raise FirstStepRefused('the caller left before first-step admission', 75)
    if isinstance(answer, wire.DispatchStatus):
        reason = answer.reason or 'the first placement ended before admission'
        if isinstance(answer.assignment, placement.UnknownAssignment) and isinstance(answer.retry, placement.DoNotRetry):
            raise PlacementExhausted(reason)
        raise FirstStepRefused(reason)
    return answer


def _program_request(command: Sequence[str], source: Path, owner: wire.Owner,
                     position: SlotQueuePosition, run: wire.RunIdentity) -> queue_place.RefreshedRequest:
    program = parse_command_program(command)
    script, script_arguments = ('', ()) if isinstance(program, OtherCommand) else program
    affected = script == 'targeted_test.py' or '--affected' in script_arguments
    name = 'test-affected' if affected else 'gate'
    resolved_job = command_job(command)
    job = resolved_job if isinstance(resolved_job, StepJob) else GATE_JOB_NOT_RECORDED
    what = read_step_names().shown(job.step) if isinstance(job, StepJob) else ""
    config = records.read_memory_config()
    first = config.program_first_peaks.get(name)
    if first is None:
        raise FirstStepRefused(f'the first memory peak for {name} is not configured')
    profile = records.ProgramProfile(name, str(Path.home() / '.claude'), str(source),
        records.HistoryPeakSizing(first, records.FirstProgramPeak(first), 0.0))
    arguments = [name]
    for index, word in enumerate(script_arguments):
        if word in ('--type-dirs', '--type-dir'):
            arguments.append('--type-dirs')
            for directory in script_arguments[index + 1:]:
                if directory.startswith('-'):
                    break
                arguments.append(directory)
            break
        if word.startswith(('--type-dirs=', '--type-dir=')):
            arguments.extend(('--type-dirs', word.partition('=')[2]))
            break
    def refresh() -> wire.AdmitRequest:
        request = client.step_request(arguments, str(source), owner, position.since, '', position.episode, profile, run=run)
        return replace(request, identity=replace(request.identity, job=job, what=what))
    return queue_place.RefreshedRequest(refresh)


def _program_grant(command: Sequence[str], source: Path, owner: wire.Owner,
                   position: SlotQueuePosition, run: wire.RunIdentity) -> wire.Admitted:
    program = _program_request(command, source, owner, position, run)
    unit = ''
    def refresh() -> wire.AdmitRequest:
        nonlocal unit
        request = program.refresh()
        unit = request.identity.unit
        return request
    output = client.WaitOutput()
    answer = queue_place.request_place(queue_place.RefreshedRequest(refresh), output)
    if isinstance(answer, wire.Refused):
        raise FirstStepRefused(client.replaced_line(answer.reason, unit) or answer.reason)
    if not isinstance(answer, wire.Admitted):
        raise FirstStepRefused('the caller left before first-step admission', 75)
    output.finished()
    return answer


def run_gate_with_placement(
    run: Callable[[], subprocess.CompletedProcess[str]],
    assignment_file: Path,
    request: wire.PlacementRequest,
    place_again: Callable[[wire.PlacementRequest], None],
) -> subprocess.CompletedProcess[str]:
    """Retry a gate within the scope that owns its original placement request.

    The placement callback installs the new assignment before the next run.
    """
    while True:
        result = run()
        if not is_gate_placement_exit(result.returncode):
            return result
        try:
            previous = read_assignment(assignment_file).assignment
            if not assignment_matches_request(previous, request):
                raise wire.AdmissionUnavailable('the gate assignment does not match the request')
            destination = previous.destination
            mac_linux = destination.provider == 'ssh' and destination.target == 'mac-linux'
            retry_notice = ''
            if result.returncode == GATE_PRELAUNCH_REFUSED_STATUS:
                refusal = read_gate_claim_failure(assignment_file, request)
                settled = client.claim_failure(refusal)
                waiting_for_settlement = isinstance(settled.state, placement.CurrentAssignment) and (
                    destination.provider == 'scaler' or
                    (mac_linux and isinstance(settled.permission, placement.PermissionConsumed)))
                if (settled.assignment != refusal.assignment
                        or not isinstance(settled.permission, (placement.PermissionAvailable, placement.PermissionConsumed))
                        or not isinstance(settled.retry, placement.RetryAt)
                        or (settled.state != placement.ClosedAssignment(refusal.reason) and not waiting_for_settlement)):
                    raise wire.AdmissionUnavailable('the queue did not permit a retry for the gate attempt')
                reason = 'did not answer' if refusal.reason == placement.MAC_LINUX_NO_ANSWER else refusal.reason
                retry_notice = (f'placement: retry — {destination.host} ({destination.target}) {reason}. '
                                + 'Received a replacement assignment.')
            else:
                client.request_settlement(wire.SettlementRequest(previous))
                if mac_linux:
                    # A retry also requires a report that the guest took no run.
                    try:
                        refusal = read_gate_claim_failure(assignment_file, request)
                    except FileNotFoundError:
                        return result
                    if refusal.reason != placement.MAC_LINUX_NO_ANSWER:
                        return result
                waiting_for_settlement = True
            if mac_linux and waiting_for_settlement:
                print(f'waiting for Mac Linux to confirm the end of attempt {previous.attempt}', flush=True)
            place_again(request)
            replacement = read_assignment(assignment_file).assignment
            if (not assignment_matches_request(replacement, request)
                    or replacement.attempt == previous.attempt):
                raise wire.AdmissionUnavailable('the queue did not provide a replacement assignment')
            if retry_notice:
                print(retry_notice, flush=True)
        except Exception as error:
            if result.returncode == GATE_UNSETTLED_LAUNCH_STATUS:
                detail = f'Gate placement blocked during launch settlement. {error}\n'
                stderr = f'{result.stderr.rstrip()}\n{detail}' if result.stderr else detail
                return subprocess.CompletedProcess(request.constraints.command, result.returncode, result.stdout, stderr)
            if not isinstance(error, (OSError, ValueError, wire.AdmissionUnavailable, FirstStepRefused)):
                raise
            message = ('Gate placement blocked' if isinstance(error, PlacementExhausted)
                       else f'Gate placement blocked. {error}')
            return subprocess.CompletedProcess(request.constraints.command, GATE_PRELAUNCH_REFUSED_STATUS, '',
                                               message + '\n')


def assignment_matches_request(assignment: placement.PlacementAssignment, request: wire.PlacementRequest) -> bool:
    destination = assignment.destination
    return (assignment.episode == request.episode and assignment.owner == request.owner
            and assignment.since == request.since and assignment.source_key == request.constraints.source_key
            and assignment.session == request.constraints.session
            and any(candidate.destination.target == destination.target
                    and candidate.destination.provider == destination.provider
                    for candidate in request.constraints.candidates))


def read_gate_claim_failure(assignment_file: Path, request: wire.PlacementRequest) -> wire.ClaimFailure:
    assignment = read_assignment(assignment_file).assignment
    report_file = assignment_file.with_name(assignment_file.name + CLAIM_FAILURE_SUFFIX)
    refusal = wire.parse_request(report_file.read_bytes())
    if (not isinstance(refusal, wire.ClaimFailure) or not refusal.request_id or not refusal.reason
            or refusal.assignment != assignment
            or not assignment_matches_request(assignment, request)):
        raise wire.AdmissionUnavailable('the gate report does not match the assignment')
    return refusal


def canonical_gate_command(command: Sequence[str], source: Path) -> bool:
    """Check for a direct call to the checkout's gate script."""
    program = parse_command_program(command, keep_path=True)
    if isinstance(program, OtherCommand) or Path(command[0]).name in {"bash", "sh"}:
        return False
    root = source.resolve()
    path = root / program[0]
    canonical = root / "scripts/production/gate.py"
    return path == canonical and path.resolve() == canonical


def gate_targets(command: Sequence[str], source: Path, requested: str = "") -> tuple[str, ...]:
    """Read the caller's gate targets or the source repository's default."""
    if command_program(command) not in GATE_SCRIPTS:
        return ()
    from scripts.build_targets.gate_targets import NoEnabledTarget, eligible_gate_targets, uses_parent_reservation

    arguments = tuple(command)
    if uses_parent_reservation(arguments):
        return ()
    names = requested or os.environ.get('BUILDLOG_GATE_TARGETS', '')
    if names:
        return tuple(dict.fromkeys(name for value in names.split(',') if (name := value.strip())))

    selected = eligible_gate_targets(source, arguments)
    if isinstance(selected, NoEnabledTarget):
        raise FirstStepRefused(selected.reason)
    return selected


def _gate_request(command: Sequence[str], source: Path, targets: tuple[str, ...],
                  owner: wire.Owner, position: SlotQueuePosition, run: wire.RunIdentity,
                  sidecar: Path) -> wire.PlacementRequest:
    from scripts.build_targets.gate_targets import natedev_only_flag, scaler_candidate

    arguments = tuple(command)
    flag = natedev_only_flag(arguments)
    if flag is not None:
        if "natedev" not in targets:
            raise FirstStepRefused(f"gate.py {flag} runs only on natedev")
        targets = ("natedev",)
    local = replace(_program_request(command, source, owner, position, run).refresh(), sidecar=str(sidecar))
    tree_key = treekey.tree_key(str(source))
    source_key = (execution_identity.SourceKeyKnown(tree_key) if tree_key
                  else execution_identity.SourceKeyUnavailable('source key is unavailable'))
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    # Keep the registry order when more than one named target can start.
    registry = conf.read("build_targets")
    configured = target_config.read_registered_targets(purpose=target_config.RegistryRead.PLACEMENT,
                                                        configuration=registry)
    ordered_names = dict.fromkeys((*(target.name for target in configured if target.name in targets), *targets))
    builder = client.placement_constraint_builder()
    scalers = {target.name: target for target in configured if isinstance(target, target_config.ScalerTarget)}
    candidates: list[placement.PlacementCandidate] = []
    for name in ordered_names:
        if name in scalers:
            named = (scaler_candidate(registry, scalers[name]),)
        else:
            fixed = builder(source, '', 'final', arguments, session, source_key, target_config.ANY_ELIGIBLE_TARGET,
                Pinned(name), writes_tree=False)
            named = tuple(candidate for candidate in fixed.candidates if candidate.destination.target == name)
        if not named:
            raise FirstStepRefused(f'gate target {name} is not configured')
        candidates.extend(named)
    constraints = placement.PlacementConstraints(source_key, arguments, str(source), '', session,
        tuple(candidates), command_kind='final')
    route = target_config.PinnedTarget(targets[0]) if len(targets) == 1 else target_config.ANY_ELIGIBLE_TARGET
    return wire.PlacementRequest(position.episode, placement.SubmittingOwner(store.host_name(), owner.pid, owner.start_time),
        position.since, local.profile, constraints, identity=local.identity, run=local.run, route=route,
        local=queue_place.local_context(local))


def assignment_matches_source(assignment: placement.PlacementAssignment, source: Path) -> bool:
    """Check the current checkout against the assignment source key."""
    key = treekey.tree_key(str(source))
    return key is not None and assignment.source_key == execution_identity.SourceKeyKnown(key)


def checked_gate_assignment(command: Sequence[str], source: Path, file: Path, targets: tuple[str, ...]
                            ) -> wire.Assignment | wire.LocalPlacementGranted:
    """Check that a live wrapper owns the current assignment for this gate."""
    from scripts.build_hold import build_hold

    grant = read_assignment(file)
    assignment = grant.assignment
    owner = assignment.owner
    pid = os.getpid()
    if assignment.episode != os.environ.get('BUILDLOG_PLACEMENT_EPISODE'):
        raise FirstStepRefused('the placement episode does not match the assignment file')
    caller = build_hold.ProcessIdentity(owner.pid, owner.start_time)
    if owner.host != store.host_name() or caller not in build_hold.caller_processes(build_hold.PROC_ROOT, pid):
        raise FirstStepRefused('the placement owner is not this process or a live ancestor')
    if owner.pid != pid and command_job(command) != gate_job_from_process(
            'gate', build_hold.PROC_ROOT / str(owner.pid), start_time=owner.start_time):
        raise FirstStepRefused('the placement owner has a different gate job')
    if targets and assignment.destination.target not in targets:
        raise FirstStepRefused('the placement target is outside the caller target list')
    if not assignment_matches_source(assignment, source):
        raise FirstStepRefused('the requested source does not match the placement source')
    status = client.placement_status(assignment.episode, assignment.attempt).status
    if (not isinstance(status, KnownPlacementStatus) or status.assignment != assignment
            or not isinstance(status.state, placement.CurrentAssignment)):
        raise FirstStepRefused('the placement assignment is no longer current')
    return grant


@contextmanager
def admitted_run(command: Sequence[str], source: Path, *,
                 targets: tuple[str, ...] = (), place_unnamed_gate: bool = True) -> Generator[FirstStepAdmission]:
    """Admit the first step before any caller takes or prepares a slot."""
    gate = command_program(command) in GATE_SCRIPTS
    if targets and not gate:
        raise FirstStepRefused('named gate targets require a gate command')
    inherited = os.environ.get('BUILDLOG_PLACEMENT_FILE') if gate else None
    if inherited is not None:
        try:
            file = Path(inherited)
            grant = checked_gate_assignment(command, source, file, targets)
        except (OSError, ValueError, wire.AdmissionUnavailable) as error:
            raise FirstStepRefused(str(error)) from error
        assignment = grant.assignment
        yield FirstStepAdmission(SlotQueuePosition(assignment.episode, assignment.since), InheritedGatePlacement(grant), file)
        return
    gate_selection = targets
    if gate and place_unnamed_gate and not gate_selection:
        from scripts.build_targets.gate_targets import is_claude_gate_source, natedev_only_flag

        gate_selection = gate_targets(command, source)
        if not gate_selection and natedev_only_flag(tuple(command)) is not None and is_claude_gate_source(source):
            gate_selection = ('natedev',)
    start = records.process_start_time(os.getpid())
    if start is None:
        raise FirstStepRefused('the command has no live process identity')
    owner = wire.Owner(os.getpid(), start)
    run = wire.RunIdentity(owner.pid, owner.start_time)
    position = SlotQueuePosition(uuid4().hex, time.time())
    keys = ('BUILDLOG_RUN', 'BUILDLOG_FIRST_STEP_ASSIGNMENT', 'BUILDLOG_FIRST_STEP_ROUTE',
            'BUILDLOG_FIRST_STEP_SINCE', 'BUILDLOG_PLACEMENT_FILE', 'BUILDLOG_PLACEMENT_EPISODE',
            'BUILDLOG_PLACEMENT_SIDECAR',
            'BUILDLOG_PREPARED_COPY', 'UNIT_DETACHED_RUN')
    previous = {key: os.environ.get(key) for key in keys}
    os.environ['BUILDLOG_RUN'] = f'{owner.pid}:{owner.start_time}'
    os.environ['UNIT_DETACHED_RUN'] = '1'
    for key in ('BUILDLOG_FIRST_STEP_ASSIGNMENT', 'BUILDLOG_FIRST_STEP_ROUTE',
                'BUILDLOG_FIRST_STEP_SINCE', 'BUILDLOG_PREPARED_COPY'):
        _ = os.environ.pop(key, None)
    if gate:
        _ = os.environ.pop('BUILDLOG_PLACEMENT_FILE', None)
        _ = os.environ.pop('BUILDLOG_PLACEMENT_EPISODE', None)
        _ = os.environ.pop('BUILDLOG_PLACEMENT_SIDECAR', None)
    try:
        with ExitStack() as cleanup:
            folder = cleanup.enter_context(build_folders.tool_folder('copies-admission'))
            assignment_file = folder / 'assignment.json'
            verify = verify_command(command)
            grant: wire.Admitted | wire.LocalPlacementGranted | wire.Assignment | GatePlacement
            if isinstance(verify, VerifyCommand):
                request = _verify_request(verify, source, folder, owner, position)
                grant = _placement_grant(request)
            elif gate_selection:
                gate_request = _gate_request(command, source, gate_selection, owner, position, run,
                                             folder / 'placement.cgroup')
                configuration = _gate_configuration()
                grant = OwnedGatePlacement(_placement_grant(gate_request), gate_request, configuration)
            else:
                grant = _program_grant(command, source, owner, position, run)
            admitted = FirstStepAdmission(position, grant, assignment_file)
            try:
                if isinstance(grant, OwnedGatePlacement):
                    client.write_assignment(assignment_file, grant.grant)
                elif isinstance(grant, (wire.LocalPlacementGranted, wire.Assignment)):
                    client.write_assignment(assignment_file, grant)
                yield admitted
            finally:
                if admitted.preserve_exit_status:
                    close_gate_preserving_status(admitted.close)
                    close_gate_preserving_status(cleanup.close)
                else:
                    admitted.close()
    except wire.AdmissionUnavailable as error:
        raise FirstStepRefused(str(error)) from error
    finally:
        for key, value in previous.items():
            if value is None:
                _ = os.environ.pop(key, None)
            else:
                os.environ[key] = value

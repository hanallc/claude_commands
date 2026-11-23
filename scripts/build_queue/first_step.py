"""Hold first-step admission while a caller prepares its repository slot."""
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue import execution_identity, placement, queue_place
from scripts.build_queue.placement import MAC_LINUX_NO_ANSWER
from scripts.build_targets import target_config
from scripts.buildlog import store, treekey
from scripts.direct.step_selection import Pinned
from scripts.lib import build_folders
from scripts.lib.build_slots import SlotQueuePosition
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, StepJob
from scripts.build_queue.admission_wire import read_assignment
from scripts.lib.build_steps import (
    CLAIM_FAILURE_SUFFIX, GATE_SCRIPTS, MAC_LINUX_PRELAUNCH_REFUSED_STATUS, OtherCommand,
    VerifyCommand, command_job, command_program, parse_command_program, read_step_names, verify_command,
)
from scripts.mac_test import claim_state


class FirstStepRefused(RuntimeError):
    """The command did not receive permission to take a slot."""

    def __init__(self, reason: str, exit_code: int = 2) -> None:
        super().__init__(reason)
        self.exit_code: int = exit_code


@dataclass(frozen=True)
class HandedOff:
    """The command now owns the next admission of this run."""


@dataclass(frozen=True)
class GatePlacement:
    """The gate uses this assignment until its caller ends."""

    grant: wire.LocalPlacementGranted | wire.Assignment
    request: wire.PlacementRequest


@dataclass
class FirstStepAdmission:
    """Keep the preparation grant until the command can start."""

    queue_position: SlotQueuePosition
    grant: wire.Admitted | wire.LocalPlacementGranted | wire.Assignment | GatePlacement | HandedOff
    assignment_file: Path

    def handoff(self, tree: Path) -> None:
        """End preparation or pass the assignment to the command."""
        os.environ['BUILDLOG_PREPARED_COPY'] = str(tree)
        grant = self.grant
        if isinstance(grant, GatePlacement):
            self._hand_gate(grant)
            return
        if isinstance(grant, wire.Admitted):
            answer = queue_place.release_place(grant.reservation, 0, grant.cleanup_timing)
            if isinstance(answer, wire.Refused):
                raise FirstStepRefused(answer.reason)
        elif isinstance(grant, wire.LocalPlacementGranted):
            _ = client.finish_step(self.assignment_file, 0)
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
        # The gate ends that reservation after the check, before it takes its own (gate.gate_start_target).
        # close() cancels the placement, and the cancel returns a reservation that the gate did not end.
        os.environ['BUILDLOG_PLACEMENT_FILE'] = str(self.assignment_file)
        os.environ['BUILDLOG_PLACEMENT_EPISODE'] = grant.grant.assignment.episode

    def place_again(self, request: wire.PlacementRequest) -> None:
        """Place the gate again with its original request and pass the new assignment to the next run."""
        grant = self.grant
        if not isinstance(grant, GatePlacement) or grant.request != request:
            raise wire.AdmissionUnavailable('only the original gate request can be placed again')
        self.grant = GatePlacement(_placement_grant(request), request)
        client.write_assignment(self.assignment_file, self.grant.grant)
        self._hand_gate(self.grant)

    def run_gate(self, run: Callable[[], subprocess.CompletedProcess[str]]) -> subprocess.CompletedProcess[str]:
        """Run the gate; place it again while its Mac Linux guest takes no run."""
        grant = self.grant
        if not isinstance(grant, GatePlacement):
            return run()
        return run_gate_with_placement(run, self.assignment_file, grant.request, self.place_again)

    def close(self) -> None:
        """Release preparation after an error before command startup."""
        grant = self.grant
        if isinstance(grant, wire.Admitted):
            _ = queue_place.release_place(grant.reservation, 1, grant.cleanup_timing)
        elif isinstance(grant, wire.LocalPlacementGranted):
            _ = client.finish_step(self.assignment_file, 1, 'slot preparation failed')
        elif isinstance(grant, (wire.Assignment, GatePlacement)):
            assignment = grant.grant.assignment if isinstance(grant, GatePlacement) else grant.assignment
            reason = 'the gate caller ended' if isinstance(grant, GatePlacement) else 'slot preparation failed'
            try:
                _ = client.cancel_placement(wire.CancelPlacement(uuid4().hex, assignment.episode,
                    assignment.owner, reason))
            except wire.AdmissionUnavailable as error:
                if error.reason != wire.PLACEMENT_REQUEST_MISSING_REASON:
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
        raise FirstStepRefused(answer.reason or 'the first placement ended before admission')
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
    request = _program_request(command, source, owner, position, run)
    output = client.WaitOutput()
    answer = queue_place.request_place(request, output)
    if isinstance(answer, wire.Refused):
        raise FirstStepRefused(answer.reason)
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
        if result.returncode != MAC_LINUX_PRELAUNCH_REFUSED_STATUS:
            return result
        try:
            refusal = read_gate_claim_failure(assignment_file, request)
            settled = client.claim_failure(refusal)
            # The guest gate can consume the launch permission before it takes no run.
            if (settled.assignment != refusal.assignment
                    or settled.state != placement.ClosedAssignment(refusal.reason)
                    or not isinstance(settled.permission, (placement.PermissionAvailable, placement.PermissionConsumed))
                    or not isinstance(settled.retry, placement.RetryAt)):
                raise wire.AdmissionUnavailable('the queue has not settled the Mac guest attempt')
            destination = refusal.assignment.destination
            print(f'placement: retry — {destination.host} ({destination.target}) did not answer. '
                  + 'Requesting placement again.', flush=True)
            place_again(request)
            replacement = read_assignment(assignment_file).assignment
            if (not assignment_matches_request(replacement, request)
                    or replacement.attempt == refusal.assignment.attempt):
                raise wire.AdmissionUnavailable('the queue did not provide a replacement assignment')
        except (OSError, ValueError, wire.AdmissionUnavailable, FirstStepRefused) as error:
            return subprocess.CompletedProcess(request.constraints.command, result.returncode, '',
                                               f'Gate placement blocked. {error}\n')


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
    if (not isinstance(refusal, wire.ClaimFailure) or not refusal.request_id
            or refusal.assignment != assignment or refusal.reason != MAC_LINUX_NO_ANSWER
            or assignment.destination.provider != 'ssh' or assignment.destination.target != 'mac-linux'
            or not isinstance(refusal.claim, placement.NoProviderClaim)
            or not assignment_matches_request(assignment, request)):
        raise wire.AdmissionUnavailable('the gate report does not match the Mac guest assignment')
    return refusal


def gate_targets(command: Sequence[str], source: Path, requested: str = "") -> tuple[str, ...]:
    """Read the caller's gate targets or the source repository's default."""
    if command_program(command) not in GATE_SCRIPTS:
        return ()
    names = requested or os.environ.get('BUILDLOG_GATE_TARGETS', '')
    if names:
        return tuple(dict.fromkeys(name for value in names.split(',') if (name := value.strip())))
    from scripts.build_targets.gate_targets import NoEnabledTarget, eligible_gate_targets

    selected = eligible_gate_targets(source, tuple(command))
    if isinstance(selected, NoEnabledTarget):
        raise FirstStepRefused(selected.reason)
    return selected


def _gate_request(command: Sequence[str], source: Path, targets: tuple[str, ...],
                  owner: wire.Owner, position: SlotQueuePosition, run: wire.RunIdentity) -> wire.PlacementRequest:
    local = _program_request(command, source, owner, position, run).refresh()
    tree_key = treekey.tree_key(str(source))
    source_key = (execution_identity.SourceKeyKnown(tree_key) if tree_key
                  else execution_identity.SourceKeyUnavailable('source key is unavailable'))
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    # Keep the registry order when more than one named target can start.
    configured = target_config.read_targets(purpose=target_config.RegistryRead.PLACEMENT)
    ordered_names = dict.fromkeys((*(target.name for target in configured if target.name in targets), *targets))
    builder = client.placement_constraint_builder()
    arguments = tuple(command)
    candidates: list[placement.PlacementCandidate] = []
    for name in ordered_names:
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


@contextmanager
def admitted_run(command: Sequence[str], source: Path, *,
                 targets: tuple[str, ...] = ()) -> Generator[FirstStepAdmission]:
    """Admit the first step before any caller takes or prepares a slot."""
    gate = command_program(command) in GATE_SCRIPTS
    if targets and not gate:
        raise FirstStepRefused('named gate targets require a gate command')
    start = records.process_start_time(os.getpid())
    if start is None:
        raise FirstStepRefused('the command has no live process identity')
    owner = wire.Owner(os.getpid(), start)
    run = wire.RunIdentity(owner.pid, owner.start_time)
    position = SlotQueuePosition(uuid4().hex, time.time())
    keys = ('BUILDLOG_RUN', 'BUILDLOG_FIRST_STEP_ASSIGNMENT', 'BUILDLOG_FIRST_STEP_ROUTE',
            'BUILDLOG_FIRST_STEP_SINCE', 'BUILDLOG_PLACEMENT_FILE', 'BUILDLOG_PLACEMENT_EPISODE',
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
    try:
        with build_folders.tool_folder('copies-admission') as folder:
            assignment_file = folder / 'assignment.json'
            verify = verify_command(command)
            grant: wire.Admitted | wire.LocalPlacementGranted | wire.Assignment | GatePlacement
            if isinstance(verify, VerifyCommand):
                request = _verify_request(verify, source, folder, owner, position)
                grant = _placement_grant(request)
            elif targets:
                gate_request = _gate_request(command, source, targets, owner, position, run)
                grant = GatePlacement(_placement_grant(gate_request), gate_request)
            else:
                grant = _program_grant(command, source, owner, position, run)
            admitted = FirstStepAdmission(position, grant, assignment_file)
            try:
                if isinstance(grant, GatePlacement):
                    client.write_assignment(assignment_file, grant.grant)
                elif isinstance(grant, (wire.LocalPlacementGranted, wire.Assignment)):
                    client.write_assignment(assignment_file, grant)
                yield admitted
            finally:
                admitted.close()
    except wire.AdmissionUnavailable as error:
        raise FirstStepRefused(str(error)) from error
    finally:
        for key, value in previous.items():
            if value is None:
                _ = os.environ.pop(key, None)
            else:
                os.environ[key] = value

"""Hold first-step admission while a caller prepares its repository slot."""
from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from importlib import import_module
import os
from pathlib import Path
import subprocess
import time
from typing import Protocol, cast
from uuid import uuid4

from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue import execution_identity, placement, queue_place
from scripts.build_targets import target_config
from scripts.buildlog import store, treekey
from scripts.direct.step_selection import Pinned
from scripts.lib import build_folders
from scripts.lib.build_slots import SlotQueuePosition
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, StepJob
from scripts.lib.build_steps import (
    GATE_SCRIPTS, OtherCommand, VerifyCommand, command_job, command_program, parse_command_program, verify_command,
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
            if isinstance(grant.grant, wire.LocalPlacementGranted):
                # The gate takes its own memory reservation when it starts.
                local = grant.grant.grant
                answer = queue_place.release_place(local.reservation, 0, local.cleanup_timing)
                if isinstance(answer, wire.Refused):
                    raise FirstStepRefused(answer.reason)
            os.environ['BUILDLOG_PLACEMENT_FILE'] = str(self.assignment_file)
            os.environ['BUILDLOG_PLACEMENT_EPISODE'] = grant.grant.assignment.episode
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


class AdmissionEvents(Protocol):
    """Wait for changes to the local admission files."""

    def arm(self) -> None: ...
    def wait(self, seconds: float) -> None: ...


def _placement_grant(request: wire.PlacementRequest) -> wire.LocalPlacementGranted | wire.Assignment:
    output = client.WaitOutput()
    directory = store.root() / 'admission'
    waiter = cast(Callable[..., AbstractContextManager[AdmissionEvents]],
        getattr(import_module('scripts.direct.local_release_wait'), 'local_release_waiter'))
    try:
        with client.placement_acquisition(), waiter(directory, directory,
                wake_files=(directory / 'state.json', directory / 'queue.json')) as events:
            while True:
                events.arm()
                answer = client.acquire_placement(request)
                if isinstance(answer, client.LeftQueue):
                    raise FirstStepRefused('the caller left before first-step admission', 75)
                output.placement_notices(answer, placement_events=False)
                if isinstance(answer, (wire.Assignment, wire.LocalPlacementGranted)):
                    output.finished(left=isinstance(answer, wire.Assignment))
                    return answer
                if isinstance(answer, wire.DispatchStatus):
                    raise FirstStepRefused(answer.reason or 'the first placement ended before admission')
                if isinstance(answer.wait, wire.Wait):
                    output(answer.wait)
                    local = request.local
                    cause = answer.wait.reason
                    if isinstance(cause, records.HeadAdmissionStatus):
                        cause = cause.cause
                    if isinstance(local, wire.LocalAdmissionContext) and isinstance(cause, records.EstimateExpired):
                        profile = (request.profile if isinstance(request.profile, records.ProgramProfile)
                                   else records.INFER_FROM_ARGV)
                        refreshed = client.step_request(list(local.argv), local.cwd,
                            wire.Owner(request.owner.pid, request.owner.start_time), request.since,
                            local.sidecar, request.episode, profile, run=request.run)
                        request = replace(request, profile=refreshed.profile,
                            local=wire.LocalAdmissionContext(refreshed.argv, refreshed.cwd, refreshed.sidecar,
                                refreshed.call, refreshed.test_width, refreshed.fresh_test, refreshed.token_back_at))
                elif isinstance(answer.wait, wire.PlacementResourceWait):
                    output.resource_wait(answer.wait)
                events.wait(records.read_memory_config().waiting_pass_seconds)
    except BaseException:
        _ = client.cancel_placement(wire.CancelPlacement(uuid4().hex, request.episode, request.owner,
            'the caller left before first-step admission'))
        raise


def _program_request(command: Sequence[str], source: Path, owner: wire.Owner,
                     position: SlotQueuePosition, run: wire.RunIdentity) -> queue_place.RefreshedRequest:
    program = parse_command_program(command)
    script, script_arguments = ('', ()) if isinstance(program, OtherCommand) else program
    affected = script == 'targeted_test.py' or '--affected' in script_arguments
    name = 'test-affected' if affected else 'gate'
    resolved_job = command_job(command)
    job = resolved_job if isinstance(resolved_job, StepJob) else GATE_JOB_NOT_RECORDED
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
        return replace(request, identity=replace(request.identity, job=job))
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


def gate_targets(command: Sequence[str], requested: str = "") -> tuple[str, ...]:
    """Read the targets for a gate from the caller."""
    if command_program(command) not in GATE_SCRIPTS:
        return ()
    names = requested or os.environ.get('BUILDLOG_GATE_TARGETS', '')
    return tuple(dict.fromkeys(name for value in names.split(',') if (name := value.strip())))


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
    builder = cast(client.PlacementConstraintBuilder,
        getattr(import_module('scripts.build_targets.build_targets'), 'fixed_placement_constraints'))
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
        local=wire.LocalAdmissionContext(local.argv, local.cwd, local.sidecar, local.call, local.test_width,
            local.fresh_test, local.token_back_at))


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
                grant = GatePlacement(_placement_grant(_gate_request(command, source, targets, owner, position, run)))
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

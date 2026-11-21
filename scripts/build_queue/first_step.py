"""Hold first-step admission while a caller prepares its repository slot."""
from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from scripts.build_queue import admission_client as client, admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue import execution_identity, placement, queue_place
from scripts.buildlog import store
from scripts.direct.local_release_wait import local_release_waiter
from scripts.lib import build_folders
from scripts.lib.build_slots import SlotQueuePosition
from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.lib.build_steps import VerifyCommand, command_job, command_program, verify_command


class FirstStepRefused(RuntimeError):
    """The command did not receive permission to take a slot."""

    def __init__(self, reason: str, exit_code: int = 2) -> None:
        super().__init__(reason)
        self.exit_code: int = exit_code


@dataclass(frozen=True)
class HandedOff:
    """The command now owns the next admission of this run."""


@dataclass(frozen=True)
class GatePlacementAfterCopy:
    """The gate places its tests after it identifies the tested tree."""


GATE_PLACEMENT_EPISODE = 'BUILDLOG_GATE_PLACEMENT_EPISODE'
GATE_PLACEMENT_SINCE = 'BUILDLOG_GATE_PLACEMENT_SINCE'


@dataclass
class FirstStepAdmission:
    """Keep the preparation grant until the command can start."""

    queue_position: SlotQueuePosition
    grant: wire.Admitted | wire.LocalPlacementGranted | wire.Assignment | GatePlacementAfterCopy | HandedOff
    assignment_file: Path

    def handoff(self, tree: Path) -> None:
        """End local preparation or pass the remote assignment to verify.sh."""
        os.environ['BUILDLOG_PREPARED_COPY'] = str(tree)
        grant = self.grant
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
        elif isinstance(grant, GatePlacementAfterCopy):
            os.environ[GATE_PLACEMENT_EPISODE] = self.queue_position.episode
            os.environ[GATE_PLACEMENT_SINCE] = str(self.queue_position.since)
        self.grant = HandedOff()

    def close(self) -> None:
        """Release preparation after an error before command startup."""
        grant = self.grant
        if isinstance(grant, wire.Admitted):
            _ = queue_place.release_place(grant.reservation, 1, grant.cleanup_timing)
        elif isinstance(grant, wire.LocalPlacementGranted):
            _ = client.finish_step(self.assignment_file, 1, 'slot preparation failed')
        elif isinstance(grant, wire.Assignment):
            try:
                _ = client.cancel_placement(wire.CancelPlacement(uuid4().hex, grant.assignment.episode,
                    grant.assignment.owner, 'slot preparation failed'))
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


def local_admission(request: wire.AdmitRequest) -> wire.LocalAdmissionContext:
    return wire.LocalAdmissionContext(request.argv, request.cwd, request.sidecar, request.call,
                                      request.test_width, request.fresh_test, request.token_back_at)


def placement_grant(request: wire.PlacementRequest) -> wire.LocalPlacementGranted | wire.Assignment:
    output = client.WaitOutput()
    directory = store.root() / 'admission'
    try:
        with client.placement_acquisition(), local_release_waiter(directory, directory,
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
                        request = replace(request, profile=refreshed.profile, local=local_admission(refreshed))
                elif isinstance(answer.wait, wire.PlacementResourceWait):
                    output.resource_wait(answer.wait)
                events.wait(records.read_memory_config().waiting_pass_seconds)
    except BaseException:
        with suppress(wire.AdmissionUnavailable, OSError):
            _ = client.cancel_placement(wire.CancelPlacement(uuid4().hex, request.episode, request.owner,
                'the caller left before first-step admission'))
        raise


def gate_placement_request(source: Path, tree: str, program: str, type_dirs: Sequence[str],
                           sidecar: Path, config: records.MemoryConfig, job: StepJob | GateJobNotRecorded
                           ) -> wire.PlacementRequest:
    """Submit one request for the final test tree."""
    from scripts.build_hold.owning_session import NoOwningSession, OwningSession, owning_session
    from scripts.build_targets.build_targets import fixed_placement_constraints

    pid = os.getpid()
    start = records.process_start_time(pid)
    if start is None:
        raise FirstStepRefused('the gate has no live process identity')
    episode = os.environ.pop(GATE_PLACEMENT_EPISODE, '') or uuid4().hex
    since = float(os.environ.pop(GATE_PLACEMENT_SINCE, '') or time.time())
    profile = records.ProgramProfile(program, str(Path.home() / '.claude'), str(source),
                                     queue_place.program_sizing(config, program))
    arguments = [program, '--type-dirs', *type_dirs] if type_dirs else [program]
    local = client.step_request(arguments, str(source), wire.Owner(pid, start), since, str(sidecar), episode, profile)
    session = owning_session()
    if isinstance(session, NoOwningSession) and local.identity.session_id:
        session = OwningSession(local.identity.session_id)
    constraints = fixed_placement_constraints(source, '', 'run', tuple(arguments), session,
                                              execution_identity.SourceKeyKnown(tree))
    constraints = replace(constraints, candidates=tuple(candidate for candidate in constraints.candidates
                          if candidate.destination.provider == 'local' or
                          (candidate.destination.provider == 'mac' and candidate.destination.target == 'mac')))
    return wire.PlacementRequest(episode, placement.SubmittingOwner(store.host_name(), pid, start),
        since, local.profile, constraints, identity=replace(local.identity, job=job), run=local.run,
        local=local_admission(local), prerequisite=placement.TokenNotRequired())


def _program_grant(command: Sequence[str], source: Path, owner: wire.Owner,
                   position: SlotQueuePosition, run: wire.RunIdentity) -> wire.Admitted:
    name = 'targeted-test' if command_program(command) == 'targeted_test.py' else 'merge-gate'
    resolved_job = command_job(command)
    job = resolved_job if isinstance(resolved_job, StepJob) else GATE_JOB_NOT_RECORDED
    config = records.read_memory_config()
    try:
        sizing = queue_place.program_sizing(config, name)
    except queue_place.QueuePlaceRefused as error:
        raise FirstStepRefused(str(error)) from error
    profile = records.ProgramProfile(name, str(Path.home() / '.claude'), str(source), sizing)
    arguments = [name]
    for index, word in enumerate(command):
        if word in ('--type-dirs', '--type-dir'):
            arguments.append('--type-dirs')
            for directory in command[index + 1:]:
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
    output = client.WaitOutput()
    answer = queue_place.request_place(queue_place.RefreshedRequest(refresh), output)
    if isinstance(answer, wire.Refused):
        raise FirstStepRefused(answer.reason)
    if not isinstance(answer, wire.Admitted):
        raise FirstStepRefused('the caller left before first-step admission', 75)
    output.finished()
    return answer


@contextmanager
def admitted_run(command: Sequence[str], source: Path) -> Generator[FirstStepAdmission]:
    """Admit the first step before any caller takes or prepares a slot."""
    start = records.process_start_time(os.getpid())
    if start is None:
        raise FirstStepRefused('the command has no live process identity')
    owner = wire.Owner(os.getpid(), start)
    run = wire.RunIdentity(owner.pid, owner.start_time)
    position = SlotQueuePosition(uuid4().hex, time.time())
    keys = ('BUILDLOG_RUN', 'BUILDLOG_FIRST_STEP_ASSIGNMENT', 'BUILDLOG_FIRST_STEP_ROUTE',
            'BUILDLOG_FIRST_STEP_SINCE', 'BUILDLOG_PREPARED_COPY', 'UNIT_DETACHED_RUN',
            GATE_PLACEMENT_EPISODE, GATE_PLACEMENT_SINCE)
    previous = {key: os.environ.get(key) for key in keys}
    os.environ['BUILDLOG_RUN'] = f'{owner.pid}:{owner.start_time}'
    os.environ['UNIT_DETACHED_RUN'] = '1'
    for key in ('BUILDLOG_FIRST_STEP_ASSIGNMENT', 'BUILDLOG_FIRST_STEP_ROUTE',
                'BUILDLOG_FIRST_STEP_SINCE', 'BUILDLOG_PREPARED_COPY', GATE_PLACEMENT_EPISODE, GATE_PLACEMENT_SINCE):
        _ = os.environ.pop(key, None)
    try:
        with build_folders.tool_folder('copies-admission') as folder:
            assignment_file = folder / 'assignment.json'
            verify = verify_command(command)
            grant: wire.Admitted | wire.LocalPlacementGranted | wire.Assignment | GatePlacementAfterCopy
            if isinstance(verify, VerifyCommand):
                request = _verify_request(verify, source, folder, owner, position)
                grant = placement_grant(request)
            else:
                job = command_job(command)
                if isinstance(job, StepJob) and job.step in ('test-affected', 'test-all', 'integrate'):
                    grant = GatePlacementAfterCopy()
                else:
                    grant = _program_grant(command, source, owner, position, run)
            admitted = FirstStepAdmission(position, grant, assignment_file)
            try:
                if isinstance(grant, (wire.LocalPlacementGranted, wire.Assignment)):
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

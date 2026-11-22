"""Hold a whole-run reservation and the claim on its Mac host."""
from collections.abc import Generator
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, replace
from io import StringIO
import os
from pathlib import Path
import time
from uuid import uuid4

from scripts.build_queue import admission_client, admission_records as records, admission_wire as wire, queue_place
from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.build_targets import target_config
from scripts.lib import cancellation
from scripts.mac_test import claim_state, mac_test
from scripts.production.gate_routes import SuiteMode


class WholeRunRefused(Exception):
    """The queue or Mac host refused the run."""


@dataclass
class WholeRun:
    """The caller publishes progress here and records its exit status."""

    sidecar: Path
    status: int = 1


@contextmanager
def _hold_mac(host: str, target: str, step: str, owner: wire.Owner) -> Generator[None]:
    try:
        paths = claim_state.state_paths(host)
    except target_config.ConfigError as error:
        raise WholeRunRefused(str(error)) from error
    if paths.platform != "macos":
        raise WholeRunRefused(f"{target} needs a Mac host")

    args = mac_test.CliArguments()
    args.pid, args.target, args.what = owner.pid, host, f"{target} {step}"
    # A relative worktree preserves the whole-Mac scope when a reader reloads the claim.
    args.worktree = target
    args.claim_id, args.episode, args.attempt = uuid4().hex, uuid4().hex, uuid4().hex
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    acquired = False
    try:
        while True:
            output = StringIO()
            try:
                with cancellation.defer_cleanup_signals(), claim_state.locked(paths.lock), redirect_stdout(output):
                    block = mac_test.read_claim_block(paths)
                    attempt = (mac_test.claim_once(args, paths, claim_state.WHOLE_MAC, session=session)
                               if isinstance(block, mac_test.NoMacBlock) else mac_test.blocked_claim(block))
                    acquired = isinstance(attempt, mac_test.ClaimFinished) and attempt.status == 0
            except (OSError, ValueError) as error:
                raise WholeRunRefused(str(error)) from error
            if acquired:
                break
            if isinstance(attempt, mac_test.ClaimFinished):
                raise WholeRunRefused(output.getvalue().strip() or f"{host} refused the whole-run claim")
            time.sleep(2)
        yield
    finally:
        if acquired:
            with cancellation.defer_cleanup_signals():
                identity = claim_state.ClaimIdentity(args.claim_id, args.episode, args.attempt)
                released = mac_test.release_exact_claims(paths, owner.pid, identity, "")
                if isinstance(released, mac_test.ClaimReleaseRefused):
                    raise RuntimeError(released.reason)
                if released.count != 1:
                    raise RuntimeError(f"{host} did not release the exact whole-run claim")


@contextmanager
def held(repo: Path, target: str, work: SuiteMode | StepJob, gate_job: StepJob | GateJobNotRecorded) -> Generator[WholeRun]:
    """Hold the target reservation until the caller releases its Mac claim."""
    try:
        selected = target_config.configured_ssh_target(target)
    except target_config.ConfigError as error:
        raise WholeRunRefused(str(error)) from error
    if not isinstance(selected.shared_host, target_config.SharedHost):
        raise WholeRunRefused(f"{target} has no host for a whole run")
    pid = os.getpid()
    start = records.process_start_time(pid)
    if start is None:
        raise WholeRunRefused("The whole-run owner no longer exists")
    owner, episode, since = wire.Owner(pid, start), uuid4().hex, time.time()
    step = work.step if isinstance(work, StepJob) else work.value
    job = work if isinstance(work, StepJob) else gate_job
    runner_repo = str(Path(__file__).resolve().parents[2])
    worktree = str(repo.resolve())

    with queue_place.queue_place_folder() as folder:
        run = WholeRun(folder / "reservation.cgroup")
        _ = run.sidecar.touch()

        def refresh() -> wire.AdmitRequest:
            profile = records.ProgramProfile(step, runner_repo, worktree,
                records.HistoryPeakSizing(0, records.FirstProgramPeak(0), time.time()))
            request = admission_client.step_request([step], str(repo), owner, since, str(run.sidecar), episode,
                profile, run=wire.RunIdentity(pid, start))
            return replace(request, place=wire.WholeRunPlace(target), identity=replace(request.identity, job=job))

        output = admission_client.WaitOutput()
        try:
            grant = queue_place.request_place(queue_place.RefreshedRequest(refresh), output)
        except (queue_place.QueuePlaceRefused, wire.AdmissionUnavailable, OSError, ValueError) as error:
            raise WholeRunRefused(str(error)) from error
        if isinstance(grant, wire.Refused):
            raise WholeRunRefused(grant.reason)
        if not isinstance(grant, wire.Admitted):
            raise WholeRunRefused("left the whole-run queue")
        try:
            output.finished()
            with _hold_mac(selected.shared_host.name, target, step, owner):
                yield run
        finally:
            reply = queue_place.release_place(grant.reservation, run.status, grant.cleanup_timing)
            if isinstance(reply, wire.Refused):
                raise RuntimeError(reply.reason)

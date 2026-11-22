"""Hold the repository claim for a mac-linux run."""
from collections.abc import Generator
from contextlib import contextmanager
import os
from pathlib import Path
from uuid import uuid4

from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.build_targets import mac_linux
from scripts.lib import cancellation
from scripts.mac_test import claim_state, mac_test
from scripts.production.gate_routes import SuiteMode


class MacRunRefused(Exception):
    """The Mac refused the repository claim before the run started."""


@contextmanager
def held(repo: Path, target: str, work: SuiteMode | StepJob,
         gate_job: StepJob | GateJobNotRecorded, *, attempt: str) -> Generator[None]:
    """Hold the Mac repository claim until the guest run ends."""
    if not attempt:
        raise MacRunRefused("The guest attempt is empty")
    try:
        paths = claim_state.state_paths(target)
    except (OSError, ValueError) as error:
        raise MacRunRefused(str(error)) from error
    if paths.platform != "macos":
        raise MacRunRefused(f"{target} is not a Mac claim target")
    worktree = str(repo.resolve())
    repository = claim_state.mac_repository_identity(worktree)
    if isinstance(repository, claim_state.WholeMacScope):
        raise MacRunRefused(f"{repo} has no repository for the Mac claim")
    repository = repository.claim_repository()
    if not isinstance(repository, claim_state.MacRepositoryIdentity):
        raise MacRunRefused(f"{repo} has no history key for the Mac claim")
    scope = claim_state.RepositoryScope(repository.mirror_name, repository.key)
    if isinstance(work, StepJob):
        step = work.step
    else:
        step = gate_job.step if isinstance(gate_job, StepJob) else work.value
    identity = claim_state.ClaimIdentity(mac_test.MAC_LINUX_CLAIM_ID_PREFIX + uuid4().hex,
                                         uuid4().hex, attempt)
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    args = mac_test.CliArguments()
    args.pid, args.target, args.worktree = os.getpid(), target, worktree
    args.what = f"mac-linux {step}"
    args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
    acquired = False
    try:
        try:
            with cancellation.defer_cleanup_signals(), claim_state.locked(paths.lock):
                result = mac_test.claim_once(args, paths, scope, session=session,
                                             mac_kind=mac_test.MacClaimKind.GUEST, mac_repository=repository)
                acquired = isinstance(result, mac_test.ClaimFinished) and result.status == 0
        except (OSError, ValueError) as error:
            raise MacRunRefused(str(error)) from error
        if isinstance(result, mac_test.ClaimBusy):
            raise MacRunRefused(f"{target} is busy with {result.holder['what']}")
        if isinstance(result, mac_test.ClaimHeldByCopy):
            raise MacRunRefused(f"{target} is busy with {result.holders[0]['what']}")
        if isinstance(result, mac_test.GuestClaimNeedsCleanup):
            detail = mac_test.cleanup_kept_guest_claim(paths, result.claim)
            raise MacRunRefused(f"{result.reason}: {detail}")
        if isinstance(result, mac_test.ClaimDenied):
            raise MacRunRefused(result.reason)
        if not acquired:
            raise MacRunRefused(f"{target} needs cleanup before the run can start")
        yield
    except mac_linux.GuestEndUnproven as error:
        if acquired:
            try:
                _keep(paths, args.pid, identity, error.armed)
            except (OSError, ValueError, RuntimeError) as keep_error:
                error.add_note(f"Mac claim retention failed: {keep_error}")
        raise
    except BaseException as error:
        if acquired:
            try:
                _release(paths, args.pid, identity)
            except BaseException as release_error:
                error.add_note(f"Mac claim release failed: {release_error}")
        raise
    else:
        _release(paths, args.pid, identity)


def _keep(paths: claim_state.StatePaths, pid: int, identity: claim_state.ClaimIdentity,
          armed: mac_linux.ArmedGuestAttempt) -> None:
    with cancellation.defer_cleanup_signals(), claim_state.locked(paths.lock):
        for path in claim_state.claim_files(paths):
            try:
                record = claim_state.read_run(path)
            except claim_state.StateReadError:
                continue
            if (isinstance(record, claim_state.NoRun) or "remote_host" in record or record["pid"] != pid
                    or claim_state.claim_identity(record) != identity):
                continue
            record["guest_armed"] = armed.to_record()
            mac_test.write_json(path, record)
            return
    raise RuntimeError("The exact Mac claim is missing")


def _release(paths: claim_state.StatePaths, pid: int, identity: claim_state.ClaimIdentity) -> None:
    with cancellation.defer_cleanup_signals():
        released = mac_test.release_exact_claims(paths, pid, identity, "")
        if isinstance(released, mac_test.ClaimReleaseRefused):
            raise RuntimeError(released.reason)
        if released.count != 1:
            raise RuntimeError(f"{paths.target} did not release the exact repository claim")

"""Hold the repository claim for a mac-linux run."""
from collections.abc import Generator
from contextlib import contextmanager
import os
from pathlib import Path
from uuid import uuid4

from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.lib import cancellation
from scripts.mac_test import claim_state, mac_test
from scripts.production.gate_routes import SuiteMode


class MacRunRefused(Exception):
    """The Mac refused the repository claim before the run started."""


class MacClaimBusy(MacRunRefused):
    """A live claim for the same repository holds the Mac."""

    def __init__(self, target: str, what: str) -> None:
        super().__init__(f"{target} is busy with {what}")
        self.what: str = what


@contextmanager
def held(repo: Path, target: str, work: SuiteMode | StepJob,
         gate_job: StepJob | GateJobNotRecorded) -> Generator[claim_state.ClaimIdentity]:
    """Hold the Mac repository claim until the guest run ends, and give its identity."""
    if isinstance(work, StepJob):
        step = work.step
    else:
        step = gate_job.step if isinstance(gate_job, StepJob) else work.value
    with repository_claim(repo, target, f"mac-linux {step}", mac_test.MacClaimKind.GUEST) as identity:
        yield identity


@contextmanager
def repository_claim(repo: Path, target: str, what: str,
                     kind: mac_test.MacClaimKind) -> Generator[claim_state.ClaimIdentity]:
    """Hold the Mac claim for the checkout's repository until the block ends, and give its identity."""
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
    # The holder releases its own claim; the prefix keeps the Mac activity watch from releasing it.
    identity = claim_state.ClaimIdentity(mac_test.MAC_LINUX_CLAIM_ID_PREFIX + uuid4().hex,
                                         uuid4().hex, uuid4().hex)
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    args = mac_test.CliArguments()
    args.pid, args.target, args.worktree = os.getpid(), target, worktree
    args.what = what
    args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
    acquired = False
    try:
        try:
            with cancellation.defer_cleanup_signals(), claim_state.locked(paths.lock):
                attempt = mac_test.claim_once(args, paths, scope, session=session,
                                              mac_kind=kind, mac_repository=repository)
                acquired = isinstance(attempt, mac_test.ClaimFinished) and attempt.status == 0
        except (OSError, ValueError) as error:
            raise MacRunRefused(str(error)) from error
        if isinstance(attempt, mac_test.ClaimBusy):
            raise MacClaimBusy(target, attempt.holder['what'])
        if isinstance(attempt, mac_test.ClaimHeldByCopy):
            raise MacClaimBusy(target, attempt.holders[0]['what'])
        if isinstance(attempt, mac_test.ClaimDenied):
            raise MacRunRefused(attempt.reason)
        if not acquired:
            raise MacRunRefused(f"{target} needs cleanup before the run can start")
        yield identity
    except BaseException as error:
        if acquired:
            try:
                _release(paths, args.pid, identity)
            except BaseException as release_error:
                error.add_note(f"Mac claim release failed: {release_error}")
        raise
    else:
        _release(paths, args.pid, identity)


def claim_held(target: str, claim_id: str, pid: int, start: str) -> bool:
    """Tell whether the process holds the exact repository claim.

    Each claim file is written whole, so the read takes no lock.
    """
    return any(claim.record["claim_id"] == claim_id and claim.record["pid"] == pid
               and claim.record["proc_start"] == start
               for claim in claim_state.read_claims(claim_state.state_paths(target)))


def _release(paths: claim_state.StatePaths, pid: int, identity: claim_state.ClaimIdentity) -> None:
    with cancellation.defer_cleanup_signals():
        released = mac_test.release_exact_claims(paths, pid, identity, "")
        if isinstance(released, mac_test.ClaimReleaseRefused):
            raise RuntimeError(released.reason)
        if released.count != 1:
            raise RuntimeError(f"{paths.target} did not release the exact repository claim")

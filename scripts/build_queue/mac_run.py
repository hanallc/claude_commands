"""Hold the repository claim for a mac-linux run."""
from collections.abc import Generator
from contextlib import contextmanager, redirect_stdout
from io import StringIO
import os
from pathlib import Path
from uuid import uuid4

from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.lib import cancellation
from scripts.mac_test import claim_state, mac_test
from scripts.production.gate_routes import SuiteMode


class MacRunRefused(Exception):
    """The Mac refused the repository claim before the run started."""


@contextmanager
def held(repo: Path, target: str, work: SuiteMode | StepJob,
         gate_job: StepJob | GateJobNotRecorded) -> Generator[None]:
    """Hold the Mac repository claim until the guest run ends."""
    try:
        paths = claim_state.state_paths(target)
    except (OSError, ValueError) as error:
        raise MacRunRefused(str(error)) from error
    if paths.platform != "macos":
        raise MacRunRefused(f"{target} is not a Mac claim target")
    worktree = str(repo.resolve())
    scope = claim_state.target_claim_scope(paths, worktree)
    if not isinstance(scope, claim_state.RepositoryScope):
        raise MacRunRefused(f"{worktree} has no repository claim scope")
    if isinstance(work, StepJob):
        step = work.step
    else:
        step = gate_job.step if isinstance(gate_job, StepJob) else work.value
    identity = claim_state.ClaimIdentity(mac_test.MAC_LINUX_CLAIM_ID_PREFIX + uuid4().hex,
                                         uuid4().hex, uuid4().hex)
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    args = mac_test.CliArguments()
    args.pid, args.target, args.worktree = os.getpid(), target, worktree
    args.what = f"mac-linux {step}"
    args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
    acquired = False
    try:
        output = StringIO()
        try:
            with cancellation.defer_cleanup_signals(), claim_state.locked(paths.lock), redirect_stdout(output):
                attempt = mac_test.claim_once(args, paths, scope, session=session)
                acquired = isinstance(attempt, mac_test.ClaimFinished) and attempt.status == 0
        except (OSError, ValueError) as error:
            raise MacRunRefused(str(error)) from error
        if isinstance(attempt, mac_test.ClaimBusy):
            raise MacRunRefused(f"{target} is busy with {attempt.holder['what']}")
        if isinstance(attempt, mac_test.ClaimHeldByCopy):
            raise MacRunRefused(f"{target} is busy with {attempt.holders[0]['what']}")
        if not acquired:
            raise MacRunRefused(output.getvalue().strip() or f"{target} refused the repository claim")
        yield
    finally:
        if acquired:
            with cancellation.defer_cleanup_signals():
                released = mac_test.release_exact_claims(paths, args.pid, identity, "")
                if isinstance(released, mac_test.ClaimReleaseRefused):
                    raise RuntimeError(released.reason)
                if released.count != 1:
                    raise RuntimeError(f"{target} did not release the exact repository claim")

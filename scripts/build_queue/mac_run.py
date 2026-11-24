"""Hold the repository claim for a mac-linux run."""
from collections.abc import Generator
from contextlib import contextmanager, suppress
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from scripts.build_queue.admission_jobs import GateJobNotRecorded, StepJob
from scripts.lib import conf
from scripts.mac_test import claim_state, mac_test
from scripts.production.gate_routes import SuiteMode


class MacRunRefused(Exception):
    """The Mac refused the repository claim before the run started."""


@contextmanager
def held(repo: Path, target: str, work: SuiteMode | StepJob,
         gate_job: StepJob | GateJobNotRecorded) -> Generator[claim_state.ClaimIdentity]:
    """Hold the Mac repository claim until the guest run ends, and give its identity."""
    try:
        paths = claim_state.state_paths(target)
        stop_seconds = conf.read_or_repository("build_admission").number("program_stop_seconds")
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
                                         uuid4().hex, uuid4().hex)
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    args = mac_test.CliArguments()
    args.pid, args.target, args.worktree = os.getpid(), target, worktree
    args.what = f"mac-linux {step}"
    args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
    claiming = False
    acquired = False
    try:
        try:
            with claim_state.locked(paths.lock, wait=False):
                claiming = True
                attempt = mac_test.claim_once(args, paths, scope, session=session,
                                              mac_kind=mac_test.MacClaimKind.GUEST, mac_repository=repository)
                acquired = isinstance(attempt, mac_test.ClaimFinished) and attempt.status == 0
                claiming = False
        except BlockingIOError as error:
            raise MacRunRefused(f"{target} claim state is busy") from error
        except (OSError, ValueError) as error:
            raise MacRunRefused(str(error)) from error
        if isinstance(attempt, mac_test.ClaimBusy):
            raise MacRunRefused(f"{target} is busy with {attempt.holder['what']}")
        if isinstance(attempt, mac_test.ClaimHeldByCopy):
            raise MacRunRefused(f"{target} is busy with {attempt.holders[0]['what']}")
        if isinstance(attempt, mac_test.ClaimDenied):
            raise MacRunRefused(attempt.reason)
        if not acquired:
            raise MacRunRefused(f"{target} needs cleanup before the run can start")
        yield identity
    except BaseException as error:
        try:
            if acquired or (claiming and _has_claim(paths, args.pid, identity)):
                _release(paths, args.pid, identity, stop_seconds)
        except BaseException as release_error:
            error.add_note(f"Mac claim release failed: {release_error}")
        raise
    else:
        _release(paths, args.pid, identity, stop_seconds)


def claim_held(target: str, claim_id: str, pid: int, start: str) -> bool:
    """Tell whether the process holds the exact repository claim.

    Each claim file is written whole, so the read takes no lock.
    """
    return any(claim.record["claim_id"] == claim_id and claim.record["pid"] == pid
               and claim.record["proc_start"] == start
               for claim in claim_state.read_claims(claim_state.state_paths(target)))


def _has_claim(paths: claim_state.StatePaths, pid: int, identity: claim_state.ClaimIdentity) -> bool:
    for path in claim_state.claim_files(paths):
        with suppress(claim_state.StateReadError):
            record = claim_state.read_run(path)
            if isinstance(record, dict) and record["pid"] == pid and claim_state.claim_identity(record) == identity:
                return True
    return False


def _release(paths: claim_state.StatePaths, pid: int, identity: claim_state.ClaimIdentity, stop_seconds: float) -> None:
    if not _has_claim(paths, pid, identity):
        raise RuntimeError(f"{paths.target} has no matching repository claim")
    deadline = time.monotonic() + stop_seconds
    entry = Path(mac_test.__file__).resolve()
    try:
        result = subprocess.run(
            [str(entry.parents[1] / "lib/py"), str(entry), "release", "--target", paths.target,
             "--pid", str(pid), "--claim-id", identity.claim_id, "--episode", identity.episode,
             "--attempt", identity.attempt, "--deadline-monotonic", str(deadline)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
            timeout=max(0, deadline - time.monotonic()), check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{paths.target} claim release deadline expired") from error
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip().removeprefix("cannot release: ") or
                           f"{paths.target} claim release exited with status {result.returncode}")
    if _has_claim(paths, pid, identity):
        raise RuntimeError(f"{paths.target} did not release the exact repository claim")

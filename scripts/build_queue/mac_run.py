"""Hold the repository claim for a mac-linux run."""
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryFile
import time
from uuid import uuid4

from scripts.build_hold import override_guard
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
    # The holder releases its own claim; the prefix keeps the Mac activity watch from releasing it.
    identity = claim_state.ClaimIdentity(mac_test.MAC_LINUX_CLAIM_ID_PREFIX + uuid4().hex,
                                         uuid4().hex, uuid4().hex)
    session = claim_state.submitting_session(claim_state.CALLING_SESSION)
    args = mac_test.CliArguments()
    args.pid, args.target, args.worktree = os.getpid(), target, worktree
    args.what = what
    args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
    claiming = False
    acquired = False
    try:
        try:
            with claim_state.locked(paths.lock, wait=False):
                claiming = True
                attempt = mac_test.claim_once(args, paths, scope, session=session,
                                              mac_kind=kind, mac_repository=repository)
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
            if acquired or claiming:
                _release(paths, args.pid, identity, stop_seconds, required=acquired)
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


def _release(paths: claim_state.StatePaths, pid: int, identity: claim_state.ClaimIdentity, stop_seconds: float,
             *, required: bool = True) -> None:
    deadline = time.monotonic() + stop_seconds
    entry = Path(__file__).resolve()
    python = entry.parents[1] / "lib/py"
    command = [str(python), str(entry), paths.target, str(pid),
               identity.claim_id, identity.episode, identity.attempt, str(deadline),
               "required" if required else "if-present"]
    try:
        with TemporaryFile() as output, TemporaryFile() as errors:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, stop_seconds)
            # Reserve time to reap the child within the same deadline.
            kill_at = deadline - min(0.05, remaining / 2)
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors)
            try:
                status = child.wait(timeout=max(0, kill_at - time.monotonic()))
            finally:
                if child.returncode is None:
                    child.kill()
                    with suppress(subprocess.TimeoutExpired):
                        _ = child.wait(timeout=max(0, deadline - time.monotonic()))
            if status:
                _ = errors.seek(0)
                _ = output.seek(0)
                diagnostic = errors.read().decode(errors="replace").strip() or output.read().decode(errors="replace").strip()
                raise RuntimeError(diagnostic.removeprefix("cannot release: ") or
                                   f"{paths.target} claim release exited with status {status}")
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{paths.target} claim release deadline expired") from error


def _release_command(argv: Sequence[str]) -> int:
    override_guard.refuse_overrides(Path(mac_test.__file__), "mac_test/mac_test.py")
    target, pid_word, claim_id, episode, attempt, deadline_word, requirement = argv
    paths = mac_test.state_paths(target)
    pid = int(pid_word)
    identity = claim_state.ClaimIdentity(claim_id, episode, attempt)
    if not _has_claim(paths, pid, identity):
        if requirement == "if-present":
            return 0
        print(f"{target} has no matching repository claim", file=sys.stderr)
        return 1
    args = mac_test.CliArguments()
    args.target, args.pid = target, pid
    args.claim_id, args.episode, args.attempt = identity.claim_id, identity.episode, identity.attempt
    args.deadline_monotonic = float(deadline_word)
    status = mac_test.command_release(args, paths)
    if status:
        return status
    if _has_claim(paths, pid, identity):
        print(f"{target} did not release the exact repository claim", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_release_command(sys.argv[1:]))

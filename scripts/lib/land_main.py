#!/usr/bin/env python3
"""Land a commit under the main push lock and run the repository's hook.

A repository can supply `.claude/config/post_push.sh`. Bash runs the hook from
that checkout's root with LANDED_SHA and LANDED_BRANCH exported.
Only the default branch runs the hook. The hook must not push that branch.
The hook must permit another run for the same LANDED_SHA.
A hook failure keeps the landed commit and reports a command for another run.
The git common directory stores the last completed hook in post-push-completed.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from scripts.lib import main_push_lock
from scripts.lib.atomic_file import write_text_atomic


HOOK_PATH = ".claude/config/post_push.sh"


class LandError(RuntimeError):
    """Git could not land the requested commit."""


class NotFastForward(LandError):
    """Origin has commits absent from the requested commit."""


@dataclass(frozen=True)
class Pushed:
    """This call pushed the commit to origin."""


@dataclass(frozen=True)
class AlreadyLanded:
    """Origin already has the requested commit as its tip."""


@dataclass(frozen=True)
class HookAbsent:
    """The checkout has no post-push hook."""


@dataclass(frozen=True)
class HookDone:
    """The hook completed and its record was saved."""


@dataclass(frozen=True)
class HookSkipped:
    """The branch is not the default, or the hook already completed."""


@dataclass(frozen=True)
class HookFailed:
    exit_code: int
    rerun_command: str


@dataclass(frozen=True)
class Landed:
    commit: str
    branch: str
    push: Pushed | AlreadyLanded
    hook: HookAbsent | HookDone | HookSkipped | HookFailed


@dataclass(frozen=True)
class CommandTimeLimit:
    seconds: float


@dataclass(frozen=True)
class NoTimeLimit:
    """Git retains its existing unlimited operation."""


UNLIMITED_GIT = NoTimeLimit()


def land(checkout: Path, commit: str, branch: str = "main", *,
         time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT,
         retry_source: Path | None = None) -> Landed:
    """Push only a fast-forward and run any pending default-branch hook."""
    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(["git", "-C", str(checkout), *arguments],
                                  capture_output=True, text=True, check=False,
                                  timeout=time_limit.seconds if isinstance(time_limit, CommandTimeLimit) else None)
        except subprocess.TimeoutExpired as error:
            details = "; ".join(output.decode(errors="replace").strip()
                                for output in (error.stderr, error.stdout) if output)
            raise LandError(f"timed out after {error.timeout:g} seconds; {details}") from error

    def checked(*arguments: str) -> str:
        result = git(*arguments)
        if result.returncode:
            raise LandError((result.stderr or result.stdout).strip() or f"git {arguments[0]}: exit {result.returncode}")
        return result.stdout.strip()

    with main_push_lock.main_push_lock(checkout):
        sha = checked("rev-parse", "--verify", f"{commit}^{{commit}}")
        _ = checked("fetch", "origin", branch)
        origin = checked("rev-parse", "FETCH_HEAD")
        pushed: Pushed | AlreadyLanded
        if origin == sha:
            pushed = AlreadyLanded()
        elif not git("merge-base", "--is-ancestor", sha, origin).returncode:
            return Landed(sha, branch, AlreadyLanded(), HookSkipped())
        else:
            if git("merge-base", "--is-ancestor", origin, sha).returncode:
                raise NotFastForward(f"origin/{branch} does not fast-forward to {sha}")
            _ = checked("push", "origin", f"{sha}:refs/heads/{branch}")
            pushed = Pushed()
        default = git("symbolic-ref", "refs/remotes/origin/HEAD")
        default_branch = default.stdout.strip().removeprefix("refs/remotes/origin/") if not default.returncode else "main"
        root = Path(checked("rev-parse", "--show-toplevel"))
        hook = root / HOOK_PATH
        if branch != default_branch:
            return Landed(sha, branch, pushed, HookSkipped())
        if not hook.is_file():
            return Landed(sha, branch, pushed, HookAbsent())
        record = Path(checked("rev-parse", "--path-format=absolute", "--git-common-dir")) / "post-push-completed"
        return Landed(sha, branch, pushed, run_hook(root, hook, record, sha, branch, retry_source or root))


def run_hook(root: Path, hook: Path, record: Path, sha: str, branch: str,
             source: Path) -> HookAbsent | HookDone | HookSkipped | HookFailed:
    if not hook.is_file():
        return HookAbsent()
    try:
        if record.read_text(encoding="utf-8").strip() == sha:
            return HookSkipped()
    except OSError:
        pass
    script = Path(__file__).resolve()
    rerun = (f"LANDED_SHA={shlex.quote(sha)} LANDED_BRANCH={shlex.quote(branch)} "
             f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} "
             f"--checkout {shlex.quote(str(source))} --retry-hook")
    try:
        digest = hashlib.sha256(hook.read_bytes()).hexdigest()
        rerun += f" --hook-digest {digest}"
        result = subprocess.run(["bash", str(hook)], cwd=root,
                                env={**os.environ, "LANDED_SHA": sha, "LANDED_BRANCH": branch}, check=False)
        if result.returncode:
            return HookFailed(result.returncode, rerun)
        write_text_atomic(record, sha + "\n")
    except OSError:
        return HookFailed(1, rerun)
    return HookDone()


def checked_git(source: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(source), *arguments], capture_output=True, text=True, check=False)
    if result.returncode:
        raise LandError((result.stderr or result.stdout).strip() or f"git {arguments[0]}: exit {result.returncode}")
    return result.stdout.strip()


@contextmanager
def exact_hook_checkout(source: Path, sha: str, *, hook_digest: str = "") -> Generator[Path]:
    """Supply the tracked hook and files from the recorded commit."""
    with tempfile.TemporaryDirectory(prefix="promotion-hook-") as folder:
        exact = Path(folder) / "checkout"
        _ = checked_git(source, "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", str(exact), sha)
        try:
            local_hook = source / HOOK_PATH
            exact_hook = exact / HOOK_PATH
            if not exact_hook.resolve().is_relative_to(exact.resolve()):
                raise LandError(f"{HOOK_PATH} resolves outside the approved checkout")
            if local_hook.is_file() and not exact_hook.exists() and not checked_git(source, "ls-files", "--", HOOK_PATH):
                contents = local_hook.read_bytes()
                if hook_digest and hashlib.sha256(contents).hexdigest() != hook_digest:
                    raise LandError(f"{HOOK_PATH} changed since the failed hook")
                exact_hook.parent.mkdir(parents=True, exist_ok=True)
                _ = exact_hook.write_bytes(contents)
            yield exact
        finally:
            _ = checked_git(source, "worktree", "remove", "--force", str(exact))


def retry_hook(source: Path, sha: str, branch: str, hook_digest: str) -> HookAbsent | HookDone | HookSkipped | HookFailed:
    with main_push_lock.main_push_lock(source):
        if checked_git(source, "rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}") != sha:
            raise LandError("hook retry requires the full published commit SHA")
        _ = checked_git(source, "fetch", "origin", f"refs/heads/{branch}")
        _ = checked_git(source, "merge-base", "--is-ancestor", sha, "FETCH_HEAD")
        record = Path(checked_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "post-push-completed"
        with exact_hook_checkout(source, sha, hook_digest=hook_digest) as exact:
            hook = exact / HOOK_PATH
            if not hook.is_file():
                raise LandError(f"{HOOK_PATH} is missing from the approved checkout")
            return run_hook(exact, hook, record, sha, branch, source)


def hook_report(hook: HookAbsent | HookDone | HookSkipped | HookFailed) -> str:
    """Describe the hook result in one report line."""
    if isinstance(hook, HookFailed):
        return f"post-push hook: failed (exit {hook.exit_code}); rerun: {hook.rerun_command}"
    state = "absent" if isinstance(hook, HookAbsent) else "done" if isinstance(hook, HookDone) else "skipped"
    return f"post-push hook: {state}"


class LandCommand(argparse.Namespace):
    checkout: Path = Path()
    commit: str = ""
    branch: str = "main"
    retry_hook: bool = False
    hook_digest: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--checkout", type=Path, required=True)
    _ = parser.add_argument("--commit")
    _ = parser.add_argument("--retry-hook", action="store_true")
    _ = parser.add_argument("--hook-digest", default="")
    _ = parser.add_argument("--branch", default="main")
    args = parser.parse_args(namespace=LandCommand())
    try:
        if args.retry_hook:
            hook = retry_hook(args.checkout.resolve(), os.environ.get("LANDED_SHA", ""),
                              os.environ.get("LANDED_BRANCH", "main"), args.hook_digest)
            print(hook_report(hook), file=sys.stderr if isinstance(hook, HookFailed) else sys.stdout)
            return hook.exit_code if isinstance(hook, HookFailed) else 0
        if not args.commit:
            parser.error("--commit is required to land a commit")
        result = land(args.checkout, args.commit, args.branch)
    except (LandError, OSError) as error:
        print(f"land_main: {error}", file=sys.stderr)
        return 1
    state = "pushed" if isinstance(result.push, Pushed) else "already landed"
    print(f"land_main: {state} {result.commit} on {result.branch}")
    print(hook_report(result.hook), file=sys.stderr if isinstance(result.hook, HookFailed) else sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

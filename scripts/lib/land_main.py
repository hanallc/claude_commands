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
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess
import sys

from scripts.lib import main_push_lock
from scripts.lib.atomic_file import write_text_atomic


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
         time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT) -> Landed:
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
        hook = root / ".claude/config/post_push.sh"
        if branch != default_branch:
            return Landed(sha, branch, pushed, HookSkipped())
        if not hook.is_file():
            return Landed(sha, branch, pushed, HookAbsent())
        record = Path(checked("rev-parse", "--path-format=absolute", "--git-common-dir")) / "post-push-completed"
        try:
            if record.read_text(encoding="utf-8").strip() == sha:
                return Landed(sha, branch, pushed, HookSkipped())
        except OSError:
            pass
        rerun = (f"cd {shlex.quote(str(root))} && LANDED_SHA={shlex.quote(sha)} "
                 + f"LANDED_BRANCH={shlex.quote(branch)} bash {shlex.quote(str(hook))}")
        try:
            result = subprocess.run(["bash", str(hook)], cwd=root,
                                    env={**os.environ, "LANDED_SHA": sha, "LANDED_BRANCH": branch}, check=False)
            if result.returncode:
                return Landed(sha, branch, pushed, HookFailed(result.returncode, rerun))
            write_text_atomic(record, sha + "\n")
        except OSError:
            return Landed(sha, branch, pushed, HookFailed(1, rerun))
        return Landed(sha, branch, pushed, HookDone())


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--checkout", type=Path, required=True)
    _ = parser.add_argument("--commit", required=True)
    _ = parser.add_argument("--branch", default="main")
    args = parser.parse_args(namespace=LandCommand())
    try:
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

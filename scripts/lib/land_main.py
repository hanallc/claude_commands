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
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
import hashlib
import os
from pathlib import Path
import re
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


class RemoteBranchMissing(LandError):
    """The remote does not advertise the requested branch."""


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
ALLOWED_GIT_SETTINGS = frozenset({"core.fsync"})


def _git_quote(value: str) -> str:
    """Quote a field for Git's GIT_CONFIG_PARAMETERS parser."""
    return "'" + value.replace("'", r"'\''").replace("!", r"'\!'") + "'"


def _git_config_environment(git_config: Sequence[str]) -> dict[str, str] | None:
    if not git_config:
        return None
    environment = os.environ.copy()
    parameters = [environment.get("GIT_CONFIG_PARAMETERS", "")]
    for setting in git_config:
        key, separator, value = setting.partition("=")
        if key.lower() not in ALLOWED_GIT_SETTINGS:
            allowed = ", ".join(sorted(ALLOWED_GIT_SETTINGS))
            raise LandError(f"Git setting {key!r} is forbidden. Allowed settings are {allowed}.")
        quoted_value = _git_quote(value) if separator else ""
        parameters.append(f"{_git_quote(key)}={quoted_value}")
    environment["GIT_CONFIG_PARAMETERS"] = " ".join(filter(None, parameters))
    return environment


def run_git(checkout: Path, *arguments: str,
            time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT,
            environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", "-C", str(checkout), *arguments],
                              capture_output=True, text=True, check=False, env=environment,
                              timeout=time_limit.seconds if isinstance(time_limit, CommandTimeLimit) else None)
    except subprocess.TimeoutExpired as error:
        details = "; ".join(output.decode(errors="replace").strip()
                            for output in (error.stderr, error.stdout) if output)
        raise LandError(f"timed out after {error.timeout:g} seconds; {details}") from error


def git_output(result: subprocess.CompletedProcess[str], operation: str) -> str:
    if result.returncode:
        raise LandError((result.stderr or result.stdout).strip() or f"git {operation}: exit {result.returncode}")
    return result.stdout


def checked_git(checkout: Path, *arguments: str,
                time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT,
                environment: dict[str, str] | None = None) -> str:
    return git_output(run_git(checkout, *arguments, time_limit=time_limit, environment=environment),
                      arguments[0]).strip()


def remote_branch_commit(checkout: Path, remote: str, branch: str, *,
                         time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT,
                         environment: dict[str, str] | None = None) -> str:
    """Return the exact commit that the remote advertises for the branch."""
    return remote_branch_tip(checkout, remote, branch, time_limit=time_limit, environment=environment).commit


class RemoteDefaultBranch(Enum):
    HEAD = "resolve the remote HEAD"


@dataclass(frozen=True)
class AdvertisedBranchTip:
    branch: str
    commit: str


def remote_branch_tip(checkout: Path, remote: str, branch: str | RemoteDefaultBranch, *,
                      time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT,
                      environment: dict[str, str] | None = None) -> AdvertisedBranchTip:
    """Resolve one advertised branch tip without updating local refs."""
    # The snapshot protects against fetches in this checkout.
    # Another clone can still publish a different commit after ls-remote.
    default = isinstance(branch, RemoteDefaultBranch)
    ref = "HEAD" if default else f"refs/heads/{branch}"
    query = run_git(checkout, "ls-remote", "--symref" if default else "--refs", "--", remote, ref,
                    time_limit=time_limit, environment=environment)
    targets: list[str] = []
    matching_shas: list[str] = []
    for row in git_output(query, "ls-remote").splitlines():
        value, _, advertised_ref = row.partition("\t")
        if not advertised_ref or "\t" in advertised_ref:
            raise LandError(f"{remote}: malformed ls-remote output for {ref}")
        if default and advertised_ref == ref and value.startswith("ref: refs/heads/"):
            targets.append(value.removeprefix("ref: refs/heads/"))
            continue
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            raise LandError(f"{remote}: malformed ls-remote output for {ref}")
        if advertised_ref == ref:
            matching_shas.append(value)
    if not matching_shas:
        raise RemoteBranchMissing(f"{remote} does not advertise {ref}")
    if len(matching_shas) != 1:
        raise LandError(f"{remote}: expected one full commit SHA for {ref}")
    if isinstance(branch, RemoteDefaultBranch):
        if len(targets) != 1 or not targets[0]:
            raise LandError(f"{remote}: expected one symbolic HEAD branch")
        branch = targets[0]
    sha = matching_shas[0]
    object_type = run_git(checkout, "--no-replace-objects", "cat-file", "-t", sha, time_limit=time_limit,
                          environment=environment)
    if object_type.returncode:
        _ = checked_git(checkout, "-c", "remote.origin.followRemoteHEAD=never", "fetch",
                        "--no-write-fetch-head", "--no-tags", "--no-prune", "--refmap=",
                        "--", remote, sha, time_limit=time_limit, environment=environment)
        object_type = run_git(checkout, "--no-replace-objects", "cat-file", "-t", sha, time_limit=time_limit,
                              environment=environment)
    if git_output(object_type, "cat-file").strip() != "commit":
        raise LandError(f"{remote}: {sha} for {ref} is not a commit")
    return AdvertisedBranchTip(branch, sha)


def land(checkout: Path, commit: str, branch: str = "main", *,
         time_limit: CommandTimeLimit | NoTimeLimit = UNLIMITED_GIT,
         retry_source: Path | None = None, git_config: Sequence[str] = ()) -> Landed:
    """Push only a fast-forward and run any pending default-branch hook."""
    environment = _git_config_environment(git_config)

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return run_git(checkout, *arguments, time_limit=time_limit, environment=environment)

    def checked(*arguments: str) -> str:
        return checked_git(checkout, *arguments, time_limit=time_limit, environment=environment)

    with main_push_lock.main_push_lock(checkout):
        sha = checked("rev-parse", "--verify", f"{commit}^{{commit}}")
        origin = remote_branch_commit(checkout, "origin", branch, time_limit=time_limit, environment=environment)
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
        return Landed(sha, branch, pushed, run_hook(root, hook, record, sha, branch, retry_source or root,
                                                    git_config=git_config))


def run_hook(root: Path, hook: Path, record: Path, sha: str, branch: str,
             source: Path, *, git_config: Sequence[str] = ()) -> HookAbsent | HookDone | HookSkipped | HookFailed:
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
    rerun += "".join(f" --git-config {shlex.quote(setting)}" for setting in git_config)
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


class CommitOrder(Enum):
    SAME_COMMIT = auto()
    REQUESTED_DESCENDANT = auto()
    REQUESTED_ANCESTOR = auto()


def commit_order(source: Path, current: str, requested: str, *,
                 environment: dict[str, str] | None = None) -> CommitOrder:
    """Compare exact commits in one repository. Refuse unrelated histories."""
    for sha in dict.fromkeys((current, requested)):
        resolved = checked_git(source, "rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}",
                               environment=environment)
        if resolved != sha:
            raise LandError(f"{source}: expected a full commit SHA, got {sha}")
    if current == requested:
        return CommitOrder.SAME_COMMIT
    for ancestor, descendant, order in ((current, requested, CommitOrder.REQUESTED_DESCENDANT),
                                        (requested, current, CommitOrder.REQUESTED_ANCESTOR)):
        result = run_git(source, "merge-base", "--is-ancestor", ancestor, descendant, environment=environment)
        if result.returncode == 0:
            return order
        if result.returncode != 1:
            detail = result.stderr.strip() or f"git merge-base exited {result.returncode}"
            raise LandError(f"{source}: {detail}")
    raise NotFastForward(f"{source}: commits {current} and {requested} have diverged")


@contextmanager
def exact_hook_checkout(source: Path, sha: str, *, hook_digest: str = "",
                        environment: dict[str, str] | None = None) -> Generator[Path]:
    """Supply the tracked hook and files from the recorded commit."""
    with tempfile.TemporaryDirectory(prefix="promotion-hook-") as folder:
        exact = Path(folder) / "checkout"
        _ = checked_git(source, "-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", str(exact), sha,
                        environment=environment)
        try:
            local_hook = source / HOOK_PATH
            exact_hook = exact / HOOK_PATH
            if not exact_hook.resolve().is_relative_to(exact.resolve()):
                raise LandError(f"{HOOK_PATH} resolves outside the approved checkout")
            if (local_hook.is_file() and not exact_hook.exists()
                    and not checked_git(source, "ls-files", "--", HOOK_PATH, environment=environment)):
                contents = local_hook.read_bytes()
                if hook_digest and hashlib.sha256(contents).hexdigest() != hook_digest:
                    raise LandError(f"{HOOK_PATH} changed since the failed hook")
                exact_hook.parent.mkdir(parents=True, exist_ok=True)
                _ = exact_hook.write_bytes(contents)
            yield exact
        finally:
            _ = checked_git(source, "worktree", "remove", "--force", str(exact), environment=environment)


def retry_hook(source: Path, sha: str, branch: str, hook_digest: str, *,
               git_config: Sequence[str] = ()) -> HookAbsent | HookDone | HookSkipped | HookFailed:
    environment = _git_config_environment(git_config)
    with main_push_lock.main_push_lock(source):
        published = remote_branch_commit(source, "origin", branch, environment=environment)
        order = commit_order(source, published, sha, environment=environment)
        if order == CommitOrder.REQUESTED_ANCESTOR:
            raise LandError(f"origin/{branch} has a newer publication {published}; hook retry for {sha} refused")
        if order == CommitOrder.REQUESTED_DESCENDANT:
            raise LandError(f"origin/{branch} has not published {sha}; hook retry refused")
        record = Path(checked_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir",
                                  environment=environment)) / "post-push-completed"
        try:
            completed = record.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            pass
        except UnicodeError as error:
            raise LandError(f"{record}: {error}") from error
        else:
            order = commit_order(source, completed, sha, environment=environment)
            if order == CommitOrder.SAME_COMMIT:
                return HookSkipped()
            if order == CommitOrder.REQUESTED_ANCESTOR:
                raise LandError(f"the hook completion record has a newer commit {completed}; hook retry for {sha} refused")
        with exact_hook_checkout(source, sha, hook_digest=hook_digest, environment=environment) as exact:
            hook = exact / HOOK_PATH
            if not hook.is_file():
                raise LandError(f"{HOOK_PATH} is missing from the approved checkout")
            return run_hook(exact, hook, record, sha, branch, source, git_config=git_config)


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
    git_config: list[str] = []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--checkout", type=Path, required=True)
    _ = parser.add_argument("--commit")
    _ = parser.add_argument("--retry-hook", action="store_true")
    _ = parser.add_argument("--hook-digest", default="")
    _ = parser.add_argument("--branch", default="main")
    _ = parser.add_argument("--git-config", action="append", default=[])
    args = parser.parse_args(namespace=LandCommand())
    try:
        if args.retry_hook:
            hook = retry_hook(args.checkout.resolve(), os.environ.get("LANDED_SHA", ""),
                              os.environ.get("LANDED_BRANCH", "main"), args.hook_digest, git_config=args.git_config)
            print(hook_report(hook), file=sys.stderr if isinstance(hook, HookFailed) else sys.stdout)
            return hook.exit_code if isinstance(hook, HookFailed) else 0
        if not args.commit:
            parser.error("--commit is required to land a commit")
        result = land(args.checkout, args.commit, args.branch, git_config=args.git_config)
    except (LandError, OSError) as error:
        print(f"land_main: {error}", file=sys.stderr)
        return 1
    state = "pushed" if isinstance(result.push, Pushed) else "already landed"
    print(f"land_main: {state} {result.commit} on {result.branch}")
    print(hook_report(result.hook), file=sys.stderr if isinstance(result.hook, HookFailed) else sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

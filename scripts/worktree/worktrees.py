"""Record worktrees and end them during the production merge."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import time
import sys
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import uuid4

from scripts.lib.process_failure import failure_detail
from scripts.lib.atomic_file import create_text_atomic, write_text_atomic
from scripts.lib import build_folders, git_files, origin_deletion, scratch
from scripts.production import production_doc, session_staff, staff
from scripts.agents import remote
from scripts.production.production_names import CARGO_LINER_PRODUCTION


@dataclass(frozen=True)
class WorktreeRecord:
    version: int
    path: str
    branch: str
    repo: str
    base: str
    merge_branch: str
    owner: str
    made_at: str


@dataclass(frozen=True)
class Landed:
    """Every commit and changed path satisfies the landing rule."""


@dataclass(frozen=True)
class NotLanded:
    commits: tuple[str, ...]
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ended:
    path: Path
    branch: str


@dataclass(frozen=True)
class Kept:
    path: Path
    reason: str


@dataclass(frozen=True)
class OriginBranchAbsent:
    """Origin has no branch to delete."""


@dataclass(frozen=True)
class OriginBranchRetained:
    reason: str


@dataclass(frozen=True)
class CheckedOriginTip:
    """The origin tip is contained in the local branch or the target."""

    sha: str
    push_url: str


@dataclass(frozen=True)
class LocalBranchAbsent:
    """The local branch is already absent."""


@dataclass(frozen=True)
class CheckedLocalTip:
    sha: str


@dataclass(frozen=True)
class CleanupReady:
    local: CheckedLocalTip
    origin: OriginBranchAbsent | OriginBranchRetained | CheckedOriginTip


class Refused(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code: int = code


def git(repo: Path, *arguments: str, check: bool = True,
        input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), *arguments], input=input_text,
                            capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise Refused(1, failure_detail(result))
    return result


def state_folder() -> Path:
    return Path(os.environ.get("WORKTREES_STATE_DIR", str(Path.home() / ".local/state/worktrees"))) / "made"


def record_file(path: Path) -> Path:
    return state_folder() / (hashlib.sha256(str(path).encode()).hexdigest() + ".json")


def read_record(path: Path) -> WorktreeRecord:
    fields = cast(dict[str, object], json.loads(path.read_text()))
    return _worktree_record(fields)


def _worktree_record(fields: dict[str, object]) -> WorktreeRecord:
    return WorktreeRecord(
        version=cast(int, fields["version"]), path=cast(str, fields["path"]),
        branch=cast(str, fields["branch"]), repo=cast(str, fields["repo"]),
        base=cast(str, fields["base"]), merge_branch=cast(str, fields["merge_branch"]),
        owner=cast(str, fields["owner"]), made_at=cast(str, fields["made_at"]))


def write_record(record: WorktreeRecord) -> None:
    path = record_file(Path(record.path))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(asdict(record), indent=2) + "\n")


def cargo_liner_main(repository: Path) -> Path:
    """Require the main checkout of the local cargo-liner production."""
    owners = [row for row in production_doc.local_production_documents() if row.name == CARGO_LINER_PRODUCTION]
    if len(owners) != 1 or not isinstance(owners[0].doc, staff.LiveDoc):
        raise Refused(1, "cargo-liner must have one local live production document")
    document = owners[0].doc.path
    lines = document.read_text(encoding="utf-8").splitlines()
    declared = Path(production_doc.production_field(lines, "Repository")).expanduser().resolve(strict=True)
    main = git_files.at(declared)
    if (main.git_dir != main.common_dir or git_files.locate(document.parent).common_dir != main.common_dir
            or main_checkout(repository) != main.toplevel):
        raise Refused(1, f"{repository}: expected the cargo-liner production repository")
    return main.toplevel


def retarget_cargo_liner_records(repository: Path, integration_branch: str, receipt: Path,
                                *, reverse: bool = False) -> int:
    """Save the original records before retargeting them. Reverse only those edits."""
    expected = f"{CARGO_LINER_PRODUCTION}-merges-integration"
    if integration_branch != expected:
        raise Refused(1, f"the cargo-liner integration branch must be {expected}")
    main = cargo_liner_main(repository)
    if not reverse and not receipt.exists():
        originals: list[WorktreeRecord] = []
        for path in sorted(state_folder().glob("*.json")):
            record = read_record(path)
            if Path(record.repo).resolve() == main and record.merge_branch == "main":
                originals.append(record)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        _ = create_text_atomic(receipt, json.dumps({
            "version": 1, "repository": str(main), "integration_branch": integration_branch,
            "records": [asdict(record) for record in originals],
        }, indent=2) + "\n")
    saved = cast(dict[str, object], json.loads(receipt.read_text()))
    if saved["version"] != 1 or saved["repository"] != str(main) or saved["integration_branch"] != integration_branch:
        raise Refused(1, f"{receipt}: does not describe this cargo-liner move")
    original_records = tuple(_worktree_record(fields) for fields in cast(list[dict[str, object]], saved["records"]))
    if any(Path(record.repo).resolve() != main or record.merge_branch != "main" for record in original_records):
        raise Refused(1, f"{receipt}: contains a record outside the cargo-liner main target")
    changed = 0
    for original in original_records:
        updated = replace(original, merge_branch=integration_branch)
        before, after = (updated, original) if reverse else (original, updated)
        try:
            current = read_record(record_file(Path(original.path)))
        except FileNotFoundError:
            continue
        if current == before:
            write_record(after)
            changed += 1
    return changed


def staff_record() -> staff.StaffSnapshot:
    try:
        return staff.snapshot()
    except staff.StaffAbsent:
        return staff.StaffSnapshot(())


def caller_branch(repo: Path, caller: Path, snapshot: staff.StaffSnapshot) -> tuple[str, str]:
    resolver = session_staff.RowResolver(snapshot)
    member = resolver.member_by_folder(remote.local_machine(), caller)
    if isinstance(member, session_staff.MemberFound):
        branch = resolver.merge_branch(member.row)
        if isinstance(branch, session_staff.Branch):
            return member.row.name, branch.name
    # With no merge branch, no production merge ends the tree: it stays.
    head = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    return "", head.stdout.strip().removeprefix("origin/") if head.returncode == 0 else ""


def has_branch(repo: Path, branch: str) -> bool:
    result = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if result.returncode not in (0, 1):
        raise Refused(1, result.stderr.strip())
    return result.returncode == 0


def plan_path(path: str) -> bool:
    return path.startswith("docs/plans/")


def only_target_merges(record: WorktreeRecord) -> bool:
    repo = Path(record.repo)
    target = f"refs/remotes/origin/{record.merge_branch}"
    branch = f"refs/heads/{record.branch}"
    commits = git(repo, "rev-list", f"{target}..{branch}").stdout.splitlines()
    if not commits:
        return False
    for commit in commits:
        parents = git(repo, "rev-list", "--parents", "-n", "1", commit).stdout.split()[1:]
        if len(parents) < 2:
            return False
        for parent in parents[1:]:
            ancestor = git(repo, "merge-base", "--is-ancestor", parent, target, check=False)
            if ancestor.returncode == 0:
                break
            if ancestor.returncode != 1:
                raise Refused(1, ancestor.stderr.strip())
        else:
            return False
    return True


def landed(record: WorktreeRecord) -> Landed | NotLanded:
    repo = Path(record.repo)
    if not has_branch(repo, record.branch):
        return Landed()
    target = f"refs/remotes/origin/{record.merge_branch}"
    branch = f"refs/heads/{record.branch}"
    ancestor = git(repo, "merge-base", "--is-ancestor", branch, target, check=False)
    if ancestor.returncode == 0:
        return Landed()
    if ancestor.returncode != 1:
        raise Refused(1, ancestor.stderr.strip())
    equivalent = {line[2:] for line in git(repo, "cherry", target, branch).stdout.splitlines()
                  if line.startswith("- ")}
    remaining: list[str] = []
    for commit in git(repo, "rev-list", f"{target}..{branch}").stdout.splitlines():
        parents = git(repo, "rev-list", "--parents", "-n", "1", commit).stdout.split()
        if commit in equivalent or len(parents) > 2:
            continue
        paths = git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-z", commit).stdout
        if all(plan_path(path) for path in paths.split("\0") if path):
            continue
        remaining.append(commit)
    changed = git(repo, "diff", "--name-only", "--no-renames", "-z", record.base, branch).stdout
    last_commit = int(git(repo, "log", "-1", "--format=%ct", branch).stdout.strip())
    unmatched: list[str] = []
    for path in changed.split("\0"):
        if not path or plan_path(path):
            continue
        difference = git(repo, "diff", "--quiet", target, branch, "--", path, check=False)
        if difference.returncode == 0:
            continue
        if difference.returncode != 1:
            raise Refused(1, difference.stderr.strip())
        last_change = git(repo, "log", "-1", "--format=%ct", target, "--", path).stdout.strip()
        if not last_change or int(last_change) <= last_commit:
            unmatched.append(path)
    if remaining or unmatched:
        return NotLanded(tuple(remaining), tuple(unmatched))
    return Landed()


def worktree_branches(repo: Path) -> dict[Path, str]:
    trees: dict[Path, str] = {}
    path = repo
    for field in git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0"):
        if field.startswith("worktree "):
            path = Path(field.removeprefix("worktree ")).resolve()
        elif field.startswith("branch refs/heads/"):
            trees[path] = field.removeprefix("branch refs/heads/")
    return trees


def main_checkout(repo: Path) -> Path:
    first = git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0", 1)[0]
    return Path(first.removeprefix("worktree ")).resolve()


def new_record(path: Path, branch: str, repo: Path, base: str,
               owner: str, merge_branch: str) -> WorktreeRecord:
    return WorktreeRecord(1, str(path), branch, str(repo), base, merge_branch, owner,
                          datetime.now(timezone.utc).isoformat())


def local_branch_tip(repo: Path, branch: str) -> LocalBranchAbsent | CheckedLocalTip:
    if not has_branch(repo, branch):
        return LocalBranchAbsent()
    return CheckedLocalTip(git(repo, "rev-parse", "--verify", f"refs/heads/{branch}").stdout.strip())


def origin_push_urls(repo: Path) -> tuple[str, ...]:
    if "origin" not in git(repo, "remote").stdout.splitlines():
        return ()
    urls = tuple(git(repo, "remote", "get-url", "--push", "--all", "origin").stdout.splitlines())
    if not urls or any(not url for url in urls):
        raise Refused(1, "origin has no usable push destination")
    return urls


def single_push_url(urls: tuple[str, ...]) -> str:
    if len(urls) != 1:
        raise Refused(1, "origin deletion requires one push destination")
    return urls[0]


def origin_retention(ref: str) -> str:
    try:
        return production_doc.origin_ref_protection(ref)
    except production_doc.ProductionPathsError as error:
        return str(error)


def push_destination_heads(repo: Path, url: str, *patterns: str) -> dict[str, str]:
    if git(repo, "ls-remote", "--get-url", "--", url).stdout.rstrip("\n") != url:
        raise Refused(1, "Git rewrites the resolved push destination")
    heads = git(repo, "ls-remote", "--heads", "--", url, *patterns).stdout.splitlines()
    return {name: tip for tip, name in (line.split("\t", 1) for line in heads)}


def origin_branch_state(record: WorktreeRecord) -> OriginBranchAbsent | OriginBranchRetained | CheckedOriginTip | Kept:
    repo = Path(record.repo)
    ref = f"refs/heads/{record.branch}"
    if reason := origin_retention(ref):
        return OriginBranchRetained(reason)
    urls = origin_push_urls(repo)
    if not urls:
        return OriginBranchAbsent()
    url = single_push_url(urls)
    tip = push_destination_heads(repo, url, ref).get(ref)
    if tip is None:
        return OriginBranchAbsent()
    return check_origin_landing(record, CheckedOriginTip(tip, url))


def check_origin_landing(record: WorktreeRecord, origin: CheckedOriginTip) -> CheckedOriginTip | Kept:
    repo, tip = Path(record.repo), origin.sha
    reason = f"origin has commits that the local branch lacks: {tip}"
    commit = git(repo, "cat-file", "-e", f"{tip}^{{commit}}", check=False)
    if commit.returncode in (1, 128):
        return Kept(Path(record.path), reason)
    if commit.returncode:
        raise Refused(1, commit.stderr.strip())
    branch = f"refs/heads/{record.branch}"
    if not has_branch(repo, record.branch):
        branch = f"refs/remotes/origin/{record.merge_branch}"
        reason = f"origin has commits that the merge branch lacks: {tip}"
    ancestor = git(repo, "merge-base", "--is-ancestor", tip, branch, check=False)
    if ancestor.returncode == 1:
        return Kept(Path(record.path), reason)
    if ancestor.returncode:
        raise Refused(1, ancestor.stderr.strip())
    return origin


def check_local_cleanup(path: Path) -> tuple[CheckedLocalTip, WorktreeRecord] | Kept:
    head = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if head.returncode not in (0, 1):
        raise Refused(1, head.stderr.strip())
    branch = head.stdout.strip()
    try:
        production_doc.require_removable_checkout(path, path, branch)
    except production_doc.ProductionPathsError as error:
        return Kept(path, str(error))
    _ = git(path, "fetch", "--no-prune", "origin")
    dirty = git(path, "status", "--porcelain").stdout
    if dirty:
        return Kept(path, "dirty worktree: " + " ".join(dirty.splitlines()))
    if head.returncode == 1:
        return Kept(path, "HEAD is detached")
    repo = main_checkout(path)
    local = local_branch_tip(repo, branch)
    if isinstance(local, LocalBranchAbsent):
        return Kept(path, "the local branch is absent")
    try:
        record = read_record(record_file(path))
    except FileNotFoundError:
        default = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
        if default.returncode == 1:
            raise Refused(1, "origin default branch is missing")
        if default.returncode:
            raise Refused(1, default.stderr.strip())
        target = default.stdout.strip()
        base = git(repo, "merge-base", f"refs/heads/{branch}", target).stdout.strip()
        record = new_record(path, branch, repo, base, "", target.removeprefix("refs/remotes/origin/"))
    if Path(record.repo).resolve() != repo or Path(record.path).resolve() != path or record.branch != branch:
        return Kept(path, "the worktree does not match its record")
    result = landed(record)
    if isinstance(result, NotLanded):
        return Kept(path, "work has not landed: " + " ".join((*result.commits, *result.paths)))
    if local_branch_tip(repo, branch) != local:
        return Kept(path, "the local branch changed during the cleanup check")
    return local, record


def cleanup(path: Path) -> CleanupReady | Kept:
    local_check = check_local_cleanup(path)
    if isinstance(local_check, Kept):
        return local_check
    local, record = local_check
    origin = origin_branch_state(record)
    if isinstance(origin, Kept):
        return origin
    return CleanupReady(local, origin)


def release_worktree_pair(path: Path) -> None:
    try:
        result = build_folders.release_removed_folder(path)
    except (OSError, ValueError) as error:
        print(f"Worktree pair kept for {str(path)!r}: {str(error)!r}", file=sys.stderr)
        return
    if result is build_folders.FolderPairRelease.PARTIAL:
        print(f"Worktree pair partially released for {str(path)!r}", file=sys.stderr)
    elif result not in (build_folders.FolderPairRelease.RELEASED, build_folders.FolderPairRelease.PAIR_ABSENT):
        print(f"Worktree pair kept for {str(path)!r}: {result.value}", file=sys.stderr)


def recorded_worktree_change(path: Path, repo: Path, branch: str) -> str:
    if path.exists():
        if git_files.at(path).common_dir != git_files.at(repo).common_dir:
            return "the worktree belongs to a different repository"
        dirty = git(path, "status", "--porcelain", "--untracked-files=all").stdout
        if dirty:
            return f"dirty worktree: {dirty.rstrip()}"
        head = git(path, "symbolic-ref", "-q", "HEAD", check=False)
        expected = f"refs/heads/{branch}"
        if head.returncode or head.stdout.strip() != expected:
            return head.stderr.strip() or f"HEAD is {head.stdout.strip() or 'detached'}; expected {expected}"
    return ""


def end_record(record: WorktreeRecord, snapshot: staff.StaffSnapshot) -> Ended | Kept:
    path, repo = Path(record.path), Path(record.repo)
    try:
        production_doc.require_removable_checkout(path, repo, record.branch)
    except production_doc.ProductionPathsError as error:
        return Kept(path, str(error))
    resolver = session_staff.RowResolver(snapshot)
    member = resolver.member_by_folder(remote.local_machine(), path)
    if isinstance(member, session_staff.MemberFound):
        return Kept(path, f"staff member {member.row.name} holds this worktree")
    if reason := recorded_worktree_change(path, repo, record.branch):
        return Kept(path, reason)
    if any(peer != path and name == record.branch for peer, name in worktree_branches(repo).items()):
        return Kept(path, "a different worktree holds this branch")
    if has_branch(repo, record.branch) and not git(
            repo, "rev-list", "-n", "1", f"{record.base}..refs/heads/{record.branch}").stdout.strip():
        # A tree with no commit of its own can be in use before its first commit.
        return Kept(path, "no commit of its own")
    if has_branch(repo, record.branch) and only_target_merges(record):
        return Kept(path, "only merges of the target outside it")
    local = local_branch_tip(repo, record.branch)
    result = landed(record)
    if isinstance(result, NotLanded):
        detail = "\n".join((*result.commits, *result.paths))
        return Kept(path, f"work has not landed: {detail}")
    origin = origin_branch_state(record)
    if isinstance(origin, Kept):
        return origin
    cargo_mark = scratch.cargo_removal_mark(path.resolve())
    with ExitStack() as guards:
        try:
            _guard_cargo_cleanup(path, guards)
        except (OSError, ValueError) as error:
            return Kept(path, f"Cargo output kept: {error}")
        ref = f"refs/heads/{record.branch}"
        if local_branch_tip(repo, record.branch) != local:
            return Kept(path, "the local branch changed after the cleanup check; the worktree stays")
        if reason := recorded_worktree_change(path, repo, record.branch):
            return Kept(path, reason)
        if isinstance(origin, OriginBranchRetained):
            print(f"Kept origin ref {ref}: {origin.reason}")
        elif isinstance(origin, CheckedOriginTip):
            deletion = origin_deletion.delete(repo, "origin", ref, origin.sha, expected_push_url=origin.push_url)
            if isinstance(deletion, origin_deletion.Kept):
                if deletion.cause is origin_deletion.RetentionCause.GIT_FAILURE:
                    return Kept(path, f"origin branch deletion failed; the worktree stays: {deletion.reason}")
                print(f"Kept origin ref {ref}: {deletion.reason}")
        registered = path in worktree_branches(repo)
        if local_branch_tip(repo, record.branch) != local:
            return Kept(path, "the local branch changed after the cleanup check; the worktree stays")
        if not build_folders.remove_build_folder(
                path, lambda: git(repo, "worktree", "remove", str(path)) if path.exists() or registered else True):
            return Kept(path, "the build folder lock is held; the worktree stays")
        cargo_mark.unlink(missing_ok=True)
    if isinstance(local, CheckedLocalTip):
        deletion = git(repo, "update-ref", "-d", f"refs/heads/{record.branch}", local.sha, check=False)
        if deletion.returncode:
            return Kept(path, "the local branch changed after the cleanup check; the local branch stays: "
                        + failure_detail(deletion))
    section = f"branch.{record.branch}"
    config = subprocess.run(["git", "-C", str(repo), "config", "--local", "--remove-section", section],
                            capture_output=True, text=True, check=False, env={**os.environ, "LC_ALL": "C"})
    if config.returncode and not (config.returncode == 128
                                  and config.stderr == f"fatal: no such section: {section}\n"):
        raise Refused(1, failure_detail(config))
    record_file(path).unlink(missing_ok=True)
    return Ended(path, record.branch)


def _guard_cargo_cleanup(path: Path, guards: ExitStack) -> None:
    from scripts.buildlog.store import host_name
    from scripts.lib import cargo_seed, conf

    checkout = path.resolve()
    if path.exists() and sys.platform == "linux":
        try:
            guards.enter_context(cargo_seed.checkout_guard(checkout, cargo_seed.CheckoutAccess.REMOVE))
        except BlockingIOError as error:
            raise OSError(f"Cargo seed is in progress: {checkout}") from error

    settings = conf.read_or_repository("build_folders")
    host = host_name()
    cargo_root = cargo_seed.managed_cargo_root(settings, host).resolve()
    directory = cargo_seed.managed_build_directory(checkout, settings, host, cargo_root)
    if isinstance(directory, cargo_seed.UnmanagedBuildDirectory) or not directory.is_dir():
        return
    if sys.platform == "linux":
        limits = cargo_seed.seed_limits(settings, host)
        if isinstance(limits, cargo_seed.SeedLimits):
            try:
                guards.enter_context(cargo_seed.destination_guards((directory,), limits))
            except BlockingIOError as error:
                raise OSError(f"Cargo seed holds an output lock: {checkout}") from error
    target = checkout / "target"
    mark = scratch.cargo_removal_mark(checkout)
    scratch.atomic_record(mark, [{
        "workspace_root": str(checkout),
        "target_directory": str(target),
        "build_directory": str(directory),
        "other_checkouts": [str(peer) for peer in scratch.cargo_checkout_peers(checkout)],
        "shared_output_roots": [str(target.resolve())] if target.is_symlink() else [],
    }])
    scratch.clean_cargo_outputs(checkout, scratch.save_cargo_outputs(checkout))


def end_landed(repo: Path, merge_branch: str) -> list[Ended | Kept]:
    """End recorded work after a production push; retain each failed record."""
    folder = state_folder()
    try:
        records = sorted(folder.iterdir())
    except FileNotFoundError:
        return []
    except OSError as error:
        return [Kept(folder, str(error))]
    results: list[Ended | Kept] = []
    try:
        main_repo = main_checkout(repo)
        snapshot = staff_record()
    except (Refused, OSError, ValueError, staff.StaffRefused) as error:
        return [Kept(folder, str(error))]
    for path in records:
        if path.suffix != ".json":
            continue
        tree_path = path
        try:
            record = read_record(path)
            tree_path = Path(record.path)
            if Path(record.repo).resolve() != main_repo or record.merge_branch != merge_branch:
                continue
            results.append(end_record(record, snapshot))
        except (Refused, OSError, ValueError, KeyError, TypeError) as error:
            results.append(Kept(tree_path, str(error)))
    return results


def make(path: Path, branch: str, caller: Path, repo: Path, snapshot: staff.StaffSnapshot) -> None:
    owner, merge_branch = caller_branch(repo, caller, snapshot)
    base = git(caller, "rev-parse", "HEAD").stdout.strip()
    _ = build_folders.create_build_folder(build_folders.LinkedEditableRepository(
        path, caller, build_folders.NewWorktreeBranch(branch, base)))
    record = new_record(path, branch, repo, base, owner, merge_branch)
    try:
        write_record(record)
    except OSError:
        _ = build_folders.remove_build_folder(path, lambda: git(caller, "worktree", "remove", str(path)))
        _ = git(caller, "branch", "-D", branch)
        raise
    print(f"made worktree {path} on {branch}")



class DeletionLocalIdentity(TypedDict):
    worktree_path: str
    repository: str
    common_dir: str
    branch_ref: str
    local_commit: str


class DeletionApproval(TypedDict):
    local: DeletionLocalIdentity
    created_at: int
    expires_at: int


class WorktreeDeletionPreview(DeletionApproval):
    scope: Literal["worktree"]
    push_urls: list[str]
    remote_ref: str
    origin_action: Literal["keep", "absent", "delete"]
    origin_commit: str
    retention_reason: str
    has_uncommitted: bool
    cleanup: bool


class BranchOnlyDeletionPreview(DeletionApproval):
    scope: Literal["branch-only"]


DeletionPreview = WorktreeDeletionPreview | BranchOnlyDeletionPreview


def deletion_local_identity(path: Path, repository: Path, branch: str) -> DeletionLocalIdentity:
    path = path.resolve(strict=True)
    repository = main_checkout(repository)
    local = git_files.at(path)
    if local.common_dir != git_files.at(repository).common_dir:
        raise Refused(1, "the worktree belongs to a different repository")
    if path == repository or Path.cwd().resolve().is_relative_to(path):
        raise Refused(1, "cannot delete the main or current worktree")
    if branch == "main":
        raise Refused(1, "cannot delete main")
    if git_files.head(local) != git_files.OnBranch(branch) or worktree_branches(repository).get(path) != branch:
        raise Refused(1, f"the worktree branch does not match {branch}")
    ref = f"refs/heads/{branch}"
    tip = git(repository, "rev-parse", "--verify", ref).stdout.strip()
    return DeletionLocalIdentity(worktree_path=str(path), repository=str(repository),
                                 common_dir=str(local.common_dir), branch_ref=ref, local_commit=tip)


def branch_only_identity(path: Path, repository: Path, ref: str) -> DeletionLocalIdentity:
    try:
        _ = path.lstat()
    except FileNotFoundError:
        path = path.resolve()
    else:
        raise Refused(1, f"{path}: the worktree path still exists")
    if not ref.startswith("refs/heads/"):
        raise Refused(1, "give the full local ref under refs/heads/")
    repository = main_checkout(git_files.at(repository.resolve(strict=True)).toplevel)
    _ = git(repository, "check-ref-format", ref)
    branch = ref.removeprefix("refs/heads/")
    default = git(repository, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
    if branch == "main" or default.stdout.strip() == f"refs/remotes/origin/{branch}":
        raise Refused(1, f"{ref}: protected branch")
    production_doc.require_removable_checkout(path, repository, branch)
    if branch in worktree_branches(repository).values():
        raise Refused(1, f"{ref}: a registered worktree holds this branch")
    common = git_files.at(repository).common_dir
    reject_branch_in_use(common, branch)
    tip = git(repository, "show-ref", "--verify", "--hash", ref).stdout.strip()
    return DeletionLocalIdentity(worktree_path=str(path), repository=str(repository),
                                 common_dir=str(common), branch_ref=ref, local_commit=tip)


def reject_branch_in_use(common: Path, branch: str) -> None:
    """Check Git operations that detach HEAD while they hold a branch."""
    try:
        linked = tuple((common / "worktrees").iterdir())
    except FileNotFoundError:
        linked = ()
    for git_dir in (common, *(path for path in linked if path.is_dir())):
        for marker in ("rebase-merge/head-name", "rebase-apply/head-name", "BISECT_START"):
            try:
                saved_branch = (git_dir / marker).read_text().strip().removeprefix("refs/heads/")
            except FileNotFoundError:
                continue
            if saved_branch == branch:
                raise Refused(1, f"{branch}: a registered worktree holds this branch for a Git operation")


def preview_origin(repo: Path, urls: tuple[str, ...], ref: str) -> tuple[
    OriginBranchAbsent | OriginBranchRetained | CheckedOriginTip, tuple[str, ...]
]:
    if reason := origin_retention(ref):
        return OriginBranchRetained(reason), ()
    if not urls:
        return OriginBranchAbsent(), ()
    url = single_push_url(urls)
    refs = push_destination_heads(repo, url)
    other_tips = sorted({sha for name, sha in refs.items() if name != ref})
    objects = git(repo, "cat-file", "--batch-check", input_text="".join(f"{sha}\n" for sha in other_tips)).stdout
    local_heads = tuple(sha for sha, kind, *_ in (line.split() for line in objects.splitlines())
                        if kind == "commit")
    tip = refs.get(ref)
    if tip is None:
        return OriginBranchAbsent(), local_heads
    # Count loss from the exact commit that the deletion lease will use.
    if git(repo, "cat-file", "-e", f"{tip}^{{commit}}", check=False).returncode != 0:
        _ = git(repo, "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", "--", url, tip)
    return CheckedOriginTip(tip, url), local_heads


def deletion_preview(path: Path, directory: Path, ttl: int) -> dict[str, object]:
    path = path.resolve(strict=True)
    head = git_files.head(git_files.at(path))
    if not isinstance(head, git_files.OnBranch):
        raise Refused(1, "the worktree HEAD is detached")
    identity = deletion_local_identity(path, Path.cwd(), head.name)
    repo, ref = Path(identity["repository"]), identity["branch_ref"]
    production_doc.require_removable_checkout(path, repo, head.name)
    dirty = bool(git(path, "status", "--porcelain", "--untracked-files=all").stdout)
    try:
        local_check = check_local_cleanup(path)
    except (Refused, OSError, ValueError, KeyError, TypeError) as error:
        local_check = Kept(path, str(error))
    urls = origin_push_urls(repo)
    origin, other_heads = preview_origin(repo, urls, ref)
    retained = isinstance(origin, OriginBranchRetained)
    deletes = isinstance(origin, CheckedOriginTip)
    origin_tip = origin.sha if deletes else ""
    if urls:
        # Use known origin heads as exclusions. An unknown head cannot hide loss.
        exclusions = (*other_heads, origin_tip) if deletes else other_heads
        unpushed = int(git(repo, "rev-list", "--count", identity["local_commit"], "--not", *exclusions).stdout)
    else:
        unpushed = int(git(repo, "rev-list", "--count", identity["local_commit"], "--not",
                           f"--exclude={ref.removeprefix('refs/heads/')}", "--branches").stdout)
    remote_only = int(git(repo, "rev-list", "--count", origin_tip, "--not", *other_heads).stdout) if deletes else 0
    if deletion_local_identity(path, repo, head.name) != identity:
        raise Refused(1, "the local worktree changed during preview")
    cleanup_reason = "cleanup: all work is on the target branch"
    can_clean = not dirty and not isinstance(local_check, Kept) and local_check[0].sha == identity["local_commit"]
    if isinstance(local_check, Kept):
        cleanup_reason = local_check.reason
    elif deletes and isinstance(checked := check_origin_landing(local_check[1], origin), Kept):
        can_clean = False
        cleanup_reason = checked.reason
    now = int(time.time())
    record = WorktreeDeletionPreview(
        local=identity, scope="worktree", push_urls=list(urls), remote_ref=ref,
        origin_action="delete" if deletes else "keep" if retained else "absent",
        origin_commit=origin_tip, retention_reason=origin.reason if retained else "",
        has_uncommitted=dirty, cleanup=can_clean, created_at=now, expires_at=now + ttl,
    )
    return {
        "status": "success", "preview_id": save_deletion_preview(directory, record), "worktree_path": str(path),
        "target_branch": head.name, "has_uncommitted": dirty, "has_unpushed": unpushed > 0,
        "branch_ref": ref, "local_commit": identity["local_commit"],
        "unpushed_count": unpushed, "has_remote_branch": deletes, "keep_origin": retained,
        "origin_action": record["origin_action"], "origin_commit": origin_tip,
        "retention_reason": record["retention_reason"], "has_remote_only": remote_only > 0,
        "remote_only_count": remote_only, "cleanup": can_clean, "cleanup_reason": cleanup_reason,
    }


def branch_only_preview(path: Path, repo: Path, ref: str, directory: Path, ttl: int) -> dict[str, object]:
    identity = branch_only_identity(path, repo, ref)
    branch = ref.removeprefix("refs/heads/")
    local_only = int(git(Path(identity["repository"]), "rev-list", "--count", identity["local_commit"], "--not",
                         f"--exclude={branch}", "--branches").stdout)
    now = int(time.time())
    record = BranchOnlyDeletionPreview(
        local=identity, scope="branch-only", created_at=now, expires_at=now + ttl,
    )
    return {
        "status": "success", "preview_id": save_deletion_preview(directory, record),
        "worktree_path": identity["worktree_path"], "repository": identity["repository"],
        "branch_ref": ref, "local_commit": identity["local_commit"],
        "local_only_count": local_only,
    }


def save_deletion_preview(directory: Path, preview: DeletionPreview) -> str:
    preview_id = uuid4().hex
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not create_text_atomic(deletion_record(directory, preview_id, "preview"), json.dumps(preview)):
        raise Refused(1, "the preview identifier already exists")
    return preview_id


def deletion_record(directory: Path, preview_id: str, state: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", preview_id):
        raise Refused(1, "invalid deletion preview identifier")
    metadata = directory.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise Refused(1, "the deletion preview directory is not private to the current user")
    return directory / f"{preview_id}.{state}"


def read_deletion_preview(file: Path) -> DeletionPreview:
    try:
        decoded = cast(object, json.loads(file.read_text()))
    except FileNotFoundError as error:
        raise Refused(1, "the deletion preview is missing or already used; create a new preview") from error
    if not isinstance(decoded, dict):
        raise Refused(1, "invalid deletion preview")
    fields = cast(dict[str, object], decoded)
    if fields.get("scope") not in ("worktree", "branch-only"):
        raise Refused(1, "create and confirm a new deletion preview")
    preview = cast(DeletionPreview, cast(object, fields))
    if not preview["created_at"] <= int(time.time()) < preview["expires_at"]:
        raise Refused(1, "the deletion preview expired; create a new preview")
    return preview


def confirm_deletion(directory: Path, preview_id: str) -> None:
    file = deletion_record(directory, preview_id, "preview")
    preview = read_deletion_preview(file)
    if preview["scope"] == "worktree" and preview["has_uncommitted"]:
        raise Refused(1, "the preview shows uncommitted changes")
    _ = file.rename(deletion_record(directory, preview_id, "confirmed"))


def take_deletion_preview(directory: Path, preview_id: str, *, automatic: bool) -> DeletionPreview:
    file = deletion_record(directory, preview_id, "preview" if automatic else "confirmed")
    used = deletion_record(directory, preview_id, "used")
    try:
        _ = file.rename(used)
    except FileNotFoundError as error:
        raise Refused(1, "the deletion preview is missing or already used; create a new preview") from error
    try:
        return read_deletion_preview(used)
    finally:
        used.unlink()


def delete_branch_only(directory: Path, preview_id: str, path: Path, repo: Path, ref: str) -> None:
    preview = take_deletion_preview(directory, preview_id, automatic=False)
    if preview["scope"] != "branch-only":
        raise Refused(1, "create and confirm a branch-only preview")
    identity = branch_only_identity(path, repo, ref)
    if preview["local"] != identity:
        raise Refused(1, "the local branch identity changed after preview")
    _ = git(Path(identity["repository"]), "update-ref", "--no-deref", "-d", ref, identity["local_commit"])
    print(f"Deleted local ref {ref}.")


def consume_deletion(directory: Path, preview_id: str, path: Path, repo: Path, branch: str, *,
                     automatic: bool) -> tuple[CheckedLocalTip, OriginBranchAbsent | OriginBranchRetained | CheckedOriginTip]:
    preview = take_deletion_preview(directory, preview_id, automatic=automatic)
    if preview["scope"] == "branch-only":
        raise Refused(1, "use the branch-only deletion command")
    identity = deletion_local_identity(path, repo, branch)
    if preview["local"] != identity or preview["remote_ref"] != identity["branch_ref"]:
        raise Refused(1, "the local worktree identity changed after preview")
    if preview["has_uncommitted"] or git(path, "status", "--porcelain", "--untracked-files=all").stdout:
        raise Refused(1, "the worktree has uncommitted changes; create a new preview")
    local = CheckedLocalTip(identity["local_commit"])
    if automatic:
        if not preview["cleanup"]:
            raise Refused(1, "the preview requires confirmation")
        checked = check_local_cleanup(path.resolve())
        if isinstance(checked, Kept):
            raise Refused(1, checked.reason)
        if checked[0] != local:
            raise Refused(1, "the local branch changed after preview")
    action = preview["origin_action"]
    if action == "keep":
        return local, OriginBranchRetained(preview["retention_reason"])
    if action == "absent":
        return local, OriginBranchAbsent()
    if action != "delete":
        raise Refused(1, "invalid origin action in deletion preview")
    url = single_push_url(tuple(preview["push_urls"]))
    return local, CheckedOriginTip(preview["origin_commit"], url)


def print_retirement(local: CheckedLocalTip, origin: OriginBranchAbsent | OriginBranchRetained | CheckedOriginTip) -> None:
    print(local.sha)
    if isinstance(origin, CheckedOriginTip):
        print("delete", origin.sha, origin.push_url, sep="\n")
    elif isinstance(origin, OriginBranchRetained):
        print("keep", " ".join(origin.reason.splitlines()), sep="\n")
    else:
        print("absent")

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("make")
    _ = create.add_argument("path")
    _ = create.add_argument("branch")
    check_cleanup = commands.add_parser("cleanup")
    _ = check_cleanup.add_argument("path")
    _ = check_cleanup.add_argument("--checked-tips", action="store_true")
    protection = commands.add_parser("protect-deletion")
    _ = protection.add_argument("path")
    _ = protection.add_argument("branch")
    _ = protection.add_argument("repository")
    preview = commands.add_parser("deletion-preview")
    _ = preview.add_argument("path", type=Path)
    _ = preview.add_argument("directory", type=Path)
    _ = preview.add_argument("ttl", type=int)
    branch_preview = commands.add_parser("branch-only-preview")
    _ = branch_preview.add_argument("path", type=Path)
    _ = branch_preview.add_argument("repository", type=Path)
    _ = branch_preview.add_argument("ref")
    _ = branch_preview.add_argument("directory", type=Path)
    _ = branch_preview.add_argument("ttl", type=int)
    branch_delete = commands.add_parser("delete-branch-only")
    _ = branch_delete.add_argument("directory", type=Path)
    _ = branch_delete.add_argument("preview_id")
    _ = branch_delete.add_argument("path", type=Path)
    _ = branch_delete.add_argument("repository", type=Path)
    _ = branch_delete.add_argument("ref")
    confirm = commands.add_parser("confirm-deletion")
    _ = confirm.add_argument("directory", type=Path)
    _ = confirm.add_argument("preview_id")
    consume = commands.add_parser("consume-deletion")
    _ = consume.add_argument("directory", type=Path)
    _ = consume.add_argument("preview_id")
    _ = consume.add_argument("path", type=Path)
    _ = consume.add_argument("repository", type=Path)
    _ = consume.add_argument("branch")
    _ = consume.add_argument("--automatic", action="store_true")
    release = commands.add_parser("release-pair")
    _ = release.add_argument("path")
    retarget = commands.add_parser("cargo-liner-target")
    _ = retarget.add_argument("repository")
    _ = retarget.add_argument("integration_branch")
    _ = retarget.add_argument("receipt")
    _ = retarget.add_argument("--reverse", action="store_true")
    arguments = parser.parse_args()
    command = cast(str, arguments.command)
    if command in ("deletion-preview", "branch-only-preview", "confirm-deletion", "consume-deletion", "delete-branch-only"):
        try:
            directory = cast(Path, arguments.directory)
            if command == "deletion-preview":
                print(json.dumps(deletion_preview(cast(Path, arguments.path), directory, cast(int, arguments.ttl))))
            elif command == "branch-only-preview":
                print(json.dumps(branch_only_preview(cast(Path, arguments.path), cast(Path, arguments.repository),
                                                     cast(str, arguments.ref), directory, cast(int, arguments.ttl))))
            elif command == "delete-branch-only":
                delete_branch_only(directory, cast(str, arguments.preview_id), cast(Path, arguments.path),
                                   cast(Path, arguments.repository), cast(str, arguments.ref))
            elif command == "confirm-deletion":
                confirm_deletion(directory, cast(str, arguments.preview_id))
                print("Confirmation recorded.")
            else:
                local, origin = consume_deletion(directory, cast(str, arguments.preview_id),
                                                 cast(Path, arguments.path), cast(Path, arguments.repository),
                                                 cast(str, arguments.branch), automatic=cast(bool, arguments.automatic))
                print_retirement(local, origin)
        except (Refused, production_doc.ProductionPathsError, OSError, ValueError, KeyError, TypeError) as error:
            if command in ("deletion-preview", "branch-only-preview"):
                print(json.dumps({"status": "error", "message": str(error)}))
            else:
                print(str(error), file=sys.stderr)
            return 1
        return 0
    if cast(str, arguments.command) == "cargo-liner-target":
        try:
            count = retarget_cargo_liner_records(
                Path(cast(str, arguments.repository)).expanduser(), cast(str, arguments.integration_branch),
                Path(cast(str, arguments.receipt)).expanduser(), reverse=cast(bool, arguments.reverse))
        except (Refused, production_doc.Refusal, OSError, ValueError, KeyError, TypeError) as error:
            print(str(error), file=sys.stderr)
            return 1
        print(f"updated {count} cargo-liner worktree records")
        return 0
    if cast(str, arguments.command) == "protect-deletion":
        try:
            branch = cast(str, arguments.branch)
            production_doc.require_removable_checkout(
                Path(cast(str, arguments.path)), Path(cast(str, arguments.repository)), branch)
        except production_doc.ProductionPathsError as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    if cast(str, arguments.command) == "release-pair":
        release_worktree_pair(Path(cast(str, arguments.path)).absolute())
        return 0
    if cast(str, arguments.command) == "cleanup":
        try:
            result = cleanup(Path(cast(str, arguments.path)).expanduser().resolve())
        except (Refused, production_doc.ProductionPathsError, OSError, ValueError, KeyError, TypeError) as error:
            print(str(error), file=sys.stderr)
            return 2
        if isinstance(result, Kept):
            print(result.reason)
            return 1
        if cast(bool, arguments.checked_tips):
            print_retirement(result.local, result.origin)
        else:
            print("cleanup: all work is on the target branch")
        return 0
    try:
        caller = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        repo = main_checkout(caller)
        snapshot = staff_record()
        make(Path(cast(str, arguments.path)).expanduser().resolve(), cast(str, arguments.branch),
             caller, repo, snapshot)
    except Refused as error:
        print(str(error), file=sys.stderr)
        return error.code
    except (OSError, ValueError, KeyError, staff.StaffRefused) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

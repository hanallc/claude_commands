"""Record worktrees and end them during the production merge."""
from __future__ import annotations

import sys

if __name__ == "__main__" and "--dry-run" in sys.argv:
    sys.dont_write_bytecode = True

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from scripts.lib.process_failure import failure_detail
from scripts.lib import build_folder_lock, build_folders, conf, conf_reads, copy_record, git_exclude, git_files, session_folder
from scripts.lib.atomic_file import write_text_atomic
from scripts.worktree import worktree_links

if TYPE_CHECKING:
    from scripts.production import staff


@dataclass(frozen=True)
class UnassignedOutput:
    """The worktree has no recorded output in the build layout."""


@dataclass(frozen=True)
class RecordedWorktreeOutput:
    """The record gives the unit, short name, and output path."""

    unit: str
    short_name: str
    output: Path


class MakeStage(Enum):
    MAKING = "making"
    DONE = "done"


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
    layout: RecordedWorktreeOutput | UnassignedOutput = UnassignedOutput()
    stage: MakeStage = MakeStage.DONE


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
class CheckedOriginTip:
    """The origin tip is contained in the local branch or the target."""

    sha: str


@dataclass(frozen=True)
class LocalBranchAbsent:
    """The local branch is already absent."""


@dataclass(frozen=True)
class CheckedLocalTip:
    sha: str


@dataclass(frozen=True)
class CleanupReady:
    local: CheckedLocalTip
    origin: OriginBranchAbsent | CheckedOriginTip


class Refused(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code: int = code


def git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(repo), *arguments],
                            capture_output=True, text=True, check=False)
    if check and result.returncode:
        raise Refused(1, failure_detail(result))
    return result


def state_folder() -> Path:
    return Path(os.environ.get("WORKTREES_STATE_DIR", str(Path.home() / ".local/state/worktrees"))) / "made"


def record_file(path: Path) -> Path:
    return state_folder() / (hashlib.sha256(str(path).encode()).hexdigest() + ".json")


def read_record(path: Path) -> WorktreeRecord:
    try:
        decoded = cast(object, json.loads(path.read_text()))
    except ValueError as error:
        raise ValueError(f"{path}: invalid worktree record: {error}") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{path}: worktree record must be an object")
    fields = cast(dict[str, object], decoded)

    def text_field(name: str) -> str:
        value = fields.get(name)
        if not isinstance(value, str):
            raise ValueError(f"{path}: worktree record field {name!r} must be text")
        return value

    version = fields.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"{path}: worktree record field 'version' must be an integer")
    try:
        stage = MakeStage(fields.get("stage", MakeStage.DONE.value))
    except ValueError as error:
        raise ValueError(f"{path}: invalid worktree record field 'stage'") from error
    layout: RecordedWorktreeOutput | UnassignedOutput = UnassignedOutput()
    if {"unit", "short_name", "output"} & fields.keys():
        unit, short_name, output = fields.get("unit"), fields.get("short_name"), fields.get("output")
        if not isinstance(unit, str) or not isinstance(short_name, str) or not isinstance(output, str):
            raise ValueError(f"{path}: incomplete worktree output record")
        layout = RecordedWorktreeOutput(unit, short_name, Path(output))
    return WorktreeRecord(
        version=version, path=text_field("path"), branch=text_field("branch"), repo=text_field("repo"),
        base=text_field("base"), merge_branch=text_field("merge_branch"), owner=text_field("owner"),
        made_at=text_field("made_at"), layout=layout, stage=stage)


def write_record(record: WorktreeRecord) -> None:
    path = record_file(Path(record.path))
    fields = cast(dict[str, object], asdict(record))
    _ = fields.pop("layout")
    fields["stage"] = record.stage.value
    if isinstance(record.layout, RecordedWorktreeOutput):
        fields.update(unit=record.layout.unit, short_name=record.layout.short_name, output=str(record.layout.output))
    write_text_atomic(path, json.dumps(fields, indent=2) + "\n")


def enforce_build_folders() -> bool:
    """Read whether the migration permits refusal of legacy paths."""
    mode = conf.read_or_repository("build_folders").text("folders.check")
    if mode not in ("report", "enforce"):
        raise ValueError("folders.check must be report or enforce")
    return mode == "enforce"


def output_record(repo: str, unit: str, short_name: str) -> RecordedWorktreeOutput:
    """Assign the worktree output from its three names."""
    return RecordedWorktreeOutput(unit, short_name, build_folders.output_path(repo, unit, short_name))


UNIT_WORKTREE_NAME = "worktree"


def unit_checkout_output(path: Path, repo: str, unit: str) -> RecordedWorktreeOutput:
    """Use the checkout's short name or the default for a legacy path."""
    default = build_folders.worktree_path(repo, unit, UNIT_WORKTREE_NAME)
    record = checkout_record(path)
    if path.parent == default.parent:
        name = path.name
    elif record is not None and isinstance(record.layout, RecordedWorktreeOutput):
        name = record.layout.short_name
    else:
        name = UNIT_WORKTREE_NAME
    return output_record(repo, unit, name)


def checked_output(layout: RecordedWorktreeOutput) -> Path:
    """Confirm that the recorded output still names its own build folder."""
    output = layout.output
    form = build_folders.parse_form(output)
    if isinstance(form, build_folders.UnverifiedFolderPath):
        raise form.error
    if (not isinstance(form, build_folders.WorktreeOutput)
            or (form.unit, form.short_name) != (layout.unit, layout.short_name)
            or output != build_folders.output_path(form.repo, form.unit, form.short_name)):
        raise ValueError(f"{output}: invalid worktree output in the record")
    return output


def ensure_target_link(checkout: Path, output: Path, *, save_existing: bool = True) -> None:
    """Create the output and repair the checkout's target link."""
    ensure_output_folder(output)
    target = checkout / "target"
    if target.is_symlink() and target.readlink() == output:
        return
    git_exclude.ensure_excluded(checkout, "/target")
    if target.is_symlink():
        with tempfile.TemporaryDirectory(prefix=".target-link-", dir=checkout) as directory:
            replacement = Path(directory) / "target"
            replacement.symlink_to(output, target_is_directory=True)
            if not target.is_symlink():
                raise ValueError(f"{target}: changed during link repair")
            _ = replacement.replace(target)
        return
    if not target.exists():
        target.symlink_to(output, target_is_directory=True)
        return
    if not save_existing:
        raise ValueError(f"{target}: expected a link to {output}; retain the existing path")
    saved = Path(tempfile.mkdtemp(prefix=".saved-target-", dir=checkout.parent)) / "target"
    try:
        _ = target.rename(saved)
    except OSError:
        report_retained_folder(saved.parent, "target move failed")
        raise
    try:
        target.symlink_to(output, target_is_directory=True)
    except OSError:
        if not os.path.lexists(target):
            _ = saved.rename(target)
            report_retained_folder(saved.parent, "target link creation failed")
        else:
            print(f"worktrees: saved {target} in {saved}", file=sys.stderr)
        raise
    print(f"worktrees: saved {target} in {saved}", file=sys.stderr)


def ensure_output_folder(output: Path) -> None:
    """Create the output without following links below the build root."""
    with build_folder_lock.folder_parent(output, build_folders.build_root(), create=True) as parent:
        try:
            os.mkdir(output.name, dir_fd=parent)
        except FileExistsError:
            mode = os.stat(output.name, dir_fd=parent, follow_symlinks=False).st_mode
            if not stat.S_ISDIR(mode):
                raise ValueError(f"{output}: expected a real output folder") from None


def worktree_output(path: Path) -> RecordedWorktreeOutput | UnassignedOutput:
    """Read the output assignment for this exact checkout."""
    try:
        record = read_record(record_file(path))
    except FileNotFoundError:
        return UnassignedOutput()
    if Path(record.path) != path:
        raise ValueError(f"{path}: worktree record names a different path")
    if record.stage is MakeStage.MAKING:
        raise ValueError(f"{path}: worktree creation is incomplete")
    return record.layout


def report_retained_folder(path: Path, reason: str) -> None:
    print(f"kept for a manual removal list: {path} (worktrees, {reason})", file=sys.stderr)


def _review_rename_copies(folder: Path) -> None:
    if folder.is_dir():
        for number in sorted(build_folders.copy_numbers(folder)):
            report_retained_folder(folder / str(number), "the unit name changed")


def retire(unit: str) -> None:
    """Report the retired unit's output and copies for manual review."""
    root = build_folders.build_root()
    _ = build_folders.unit_folder("validation", unit)
    if not root.is_dir():
        return
    for repo in sorted(root.iterdir()):
        if (repo.name in build_folders.BUILD_ROOT_EXCEPTIONS
                or repo.name in ("cargo", "paths", "slot", "hana-ci")
                or repo.name.startswith("old-") or repo.is_symlink() or not repo.is_dir()):
            continue
        folder = build_folders.unit_folder(repo.name, unit)
        if folder.is_dir() and not folder.is_symlink():
            report_retained_folder(folder, "the unit retired")


def staff_record() -> staff.StaffSnapshot:
    from scripts.production import staff

    try:
        return staff.snapshot()
    except staff.StaffAbsent:
        return staff.StaffSnapshot(())


def caller_branch(repo: Path, caller: Path, snapshot: staff.StaffSnapshot) -> tuple[str, str]:
    from scripts.agents import remote
    from scripts.production import session_staff

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


def origin_branch_state(record: WorktreeRecord) -> OriginBranchAbsent | CheckedOriginTip | Kept:
    repo = Path(record.repo)
    remote_branch = git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{record.branch}").stdout
    if not remote_branch:
        return OriginBranchAbsent()
    tip = remote_branch.split()[0]
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
    return CheckedOriginTip(tip)


def cleanup(path: Path) -> CleanupReady | Kept:
    _ = git(path, "fetch", "--no-prune", "origin")
    dirty = git(path, "status", "--porcelain").stdout
    if dirty:
        return Kept(path, "dirty worktree: " + " ".join(dirty.splitlines()))
    head = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if head.returncode == 1:
        return Kept(path, "HEAD is detached")
    if head.returncode:
        raise Refused(1, head.stderr.strip())
    branch = head.stdout.strip()
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
    if record.stage is MakeStage.MAKING:
        return Kept(path, "worktree creation is incomplete")
    result = landed(record)
    if isinstance(result, NotLanded):
        return Kept(path, "work has not landed: " + " ".join((*result.commits, *result.paths)))
    origin = origin_branch_state(record)
    if isinstance(origin, Kept):
        return origin
    if local_branch_tip(repo, branch) != local:
        return Kept(path, "the local branch changed during the cleanup check")
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


def _legacy_output(checkout: Path) -> Path | UnassignedOutput:
    settings = conf.read_or_repository("build_folders").values("roots.slot_cargo", "roots.cargo_paths")
    cargo_root = settings.get("roots.slot_cargo")
    mirror_root = settings.get("roots.cargo_paths")
    if not cargo_root or not mirror_root:
        return UnassignedOutput()
    root = Path(os.path.expandvars(cargo_root)).expanduser().resolve()
    mirror = Path(os.path.expandvars(mirror_root)).expanduser()
    if not mirror.is_absolute() or not checkout.is_dir() or not checkout.name:
        return UnassignedOutput()
    entry = mirror.joinpath(*checkout.parts[1:])
    output = entry.parent.resolve() / entry.name
    if root not in output.parents or output.is_symlink() or not output.is_dir():
        return UnassignedOutput()
    return output


def clean_legacy_output(checkout: Path) -> None:
    """Report the output assigned by the former Cargo mirror."""
    output = _legacy_output(checkout)
    if isinstance(output, Path):
        report_retained_folder(output, "the legacy checkout ended")


def end_record(record: WorktreeRecord, snapshot: staff.StaffSnapshot) -> Ended | Kept:
    from scripts.agents import remote
    from scripts.production import session_staff

    path, repo = Path(record.path), Path(record.repo)
    if record.stage is MakeStage.MAKING:
        return Kept(path, "worktree creation is incomplete")
    resolver = session_staff.RowResolver(snapshot)
    member = resolver.member_by_folder(remote.local_machine(), path)
    if isinstance(member, session_staff.MemberFound):
        return Kept(path, f"staff member {member.row.name} holds this worktree")
    if path.exists():
        dirty = git(path, "status", "--porcelain").stdout
        if dirty:
            return Kept(path, f"dirty worktree: {dirty.rstrip()}")
        head = git(path, "symbolic-ref", "-q", "HEAD", check=False)
        expected = f"refs/heads/{record.branch}"
        if head.returncode or head.stdout.strip() != expected:
            reason = head.stderr.strip() or f"HEAD is {head.stdout.strip() or 'detached'}; expected {expected}"
            return Kept(path, reason)
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
    if local_branch_tip(repo, record.branch) != local:
        return Kept(path, "the local branch changed after the cleanup check")
    if os.path.lexists(path) or path in worktree_branches(repo):
        if isinstance(record.layout, RecordedWorktreeOutput):
            report_retained_folder(checked_output(record.layout), "the checkout landed")
        else:
            clean_legacy_output(path)
        reason = "the checkout landed and requires manual review"
        report_retained_folder(path, reason)
        return Kept(path, reason)
    if isinstance(origin, CheckedOriginTip):
        deletion = git(repo, "push", f"--force-with-lease=refs/heads/{record.branch}:{origin.sha}",
                       "origin", "--delete", record.branch, check=False)
        if deletion.returncode:
            return Kept(path, "origin branch deletion failed: " + failure_detail(deletion))
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
    release_worktree_pair(path)
    record_file(path).unlink(missing_ok=True)
    return Ended(path, record.branch)


def end_landed(repo: Path, merge_branch: str) -> list[Ended | Kept]:
    """End recorded work after a production push; retain each failed record."""
    from scripts.production import staff

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


def create_checkout(path: Path, source: Path,
                    branch: build_folders.NewWorktreeBranch | build_folders.ExistingWorktreeBranch,
                    layout: RecordedWorktreeOutput | UnassignedOutput, *,
                    owner: str = "", merge_branch: str = "", allow_empty: bool = False,
                    allow_existing: bool = False, fixed_base: bool = False,
                    use_recorded_branch: bool = False) -> Path:
    """Finish a recorded checkout or make one through the shared allocator."""
    path = path.absolute()
    repo = main_checkout(source)
    with move_lock():
        record = checkout_record(path)
        exists = os.path.lexists(path)
        if record is not None:
            if Path(record.repo).resolve() != repo or (
                    isinstance(record.layout, RecordedWorktreeOutput) and isinstance(layout, RecordedWorktreeOutput)
                    and (record.layout.unit, record.layout.short_name) != (layout.unit, layout.short_name)):
                raise FileExistsError(f"{path}: the saved worktree does not match this make")
            if use_recorded_branch and record.stage is MakeStage.DONE and isinstance(branch, build_folders.NewWorktreeBranch):
                branch = replace(branch, name=record.branch)
            if record.branch != branch.name:
                raise FileExistsError(f"{path}: the saved worktree branch does not match this make")
        registered = registered_checkout(path, repo, branch.name) if exists else False
        start = branch.start_point if isinstance(branch, build_folders.NewWorktreeBranch) else f"refs/heads/{branch.name}"
        if record is None:
            if registered and allow_existing:
                return path
            if exists and not (allow_empty and empty_checkout_path(path)):
                raise FileExistsError(f"{path}: editable repository destination is occupied")
            if isinstance(branch, build_folders.NewWorktreeBranch) and has_branch(repo, branch.name):
                raise FileExistsError(f"{path}: worktree branch {branch.name} already exists")
        elif record.stage is MakeStage.DONE and registered:
            updated = replace(record, repo=str(repo), owner=owner, merge_branch=merge_branch, layout=layout)
            if isinstance(layout, RecordedWorktreeOutput) and record.layout != layout:
                output = checked_output(layout)
                ensure_target_link(path, output)
            if updated != record:
                write_record(updated)
            return path
        if fixed_base and record is not None and record.base != checkout_base(source, start):
            raise FileExistsError(f"{path}: the saved worktree has a different base")
        if record is not None and record.stage is MakeStage.MAKING and record.layout != layout:
            raise FileExistsError(f"{path}: the saved make has a different output")
        if isinstance(layout, RecordedWorktreeOutput):
            _ = checked_output(layout)
        if record is None:
            base = checkout_base(source, start)
            record = replace(new_record(path, branch.name, repo, base, owner, merge_branch), layout=layout)
        elif record.stage is MakeStage.DONE:
            if exists:
                raise FileExistsError(f"{path}: editable repository destination is occupied")
            base = record.base if fixed_base else checkout_base(source, start)
            record = replace(record, base=base, repo=str(repo), owner=owner, merge_branch=merge_branch, layout=layout)
        if record.stage is MakeStage.DONE:
            record = replace(record, stage=MakeStage.MAKING)
            write_record(record)
        _finish_checkout(path, source, branch, replace(record, owner=owner, merge_branch=merge_branch),
                         registered=registered)
    return path


def checkout_base(source: Path, start: str) -> str:
    return git(source, "rev-parse", "--verify", "--end-of-options", f"{start}^{{commit}}").stdout.strip()


def empty_checkout_path(path: Path) -> bool:
    return not path.is_symlink() and path.is_dir() and not any(path.iterdir())


def registered_checkout(path: Path, repo: Path, branch: str) -> bool:
    if path.is_symlink() or path == repo or worktree_branches(repo).get(path.resolve()) != branch:
        return False
    try:
        return git_files.at(path).common_dir == git_files.at(repo).common_dir
    except git_files.GitFilesError:
        return False


def pending_checkout(path: Path, source: Path, branch: str) -> bool:
    """Identify an interrupted make before Git registers its checkout."""
    record = checkout_record(path)
    return (record is not None and record.stage is MakeStage.MAKING and record.branch == branch
            and record.repo == str(main_checkout(source))
            and (not os.path.lexists(path) or empty_checkout_path(path)))


def _finish_checkout(path: Path, source: Path,
                     branch: build_folders.NewWorktreeBranch | build_folders.ExistingWorktreeBranch,
                     record: WorktreeRecord, *, registered: bool) -> None:
    """Finish a saved make without replacing its checkout."""
    repo = Path(record.repo)
    try:
        if registered:
            if git(path, "rev-parse", "HEAD").stdout.strip() != record.base:
                raise ValueError(f"{path}: cannot verify the checkout from the interrupted make")
        else:
            tip = local_branch_tip(repo, record.branch)
            if isinstance(tip, CheckedLocalTip):
                if tip.sha != record.base:
                    raise ValueError(f"{path}: the saved worktree branch changed")
                branch = build_folders.ExistingWorktreeBranch(record.branch)
            elif isinstance(branch, build_folders.NewWorktreeBranch):
                branch = replace(branch, start_point=record.base)
            _ = build_folders.create_build_folder(
                build_folders.LinkedEditableRepository(path, source, branch, allow_empty=os.path.lexists(path)))
        layout = record.layout
        if isinstance(layout, RecordedWorktreeOutput):
            ensure_target_link(path, layout.output)
        write_record(replace(record, stage=MakeStage.DONE))
    except (OSError, ValueError, Refused):
        if os.path.lexists(path):
            report_retained_folder(path, "checkout setup failed")
        raise


def _main_checkout_path(repo: str) -> Path:
    alias_key = "repo_word.claude_commands"
    alias = conf.read_or_repository("build_folders").values(alias_key).get(alias_key)
    return Path.home() / (".claude" if repo == alias else f"rust/{repo}")


def main_checkout_for_word(repo: str) -> Path:
    """Verify the main checkout selected by the repo word."""
    source = _main_checkout_path(repo)
    if not (source / ".git").is_dir() or source.is_symlink() or main_checkout(source) != source.resolve():
        raise ValueError(f"{source}: no main checkout for {repo!r}")
    if build_folders.repo_word(source) != repo:
        raise ValueError(f"{source}: the repo word {repo!r} differs from origin")
    return source.resolve()


@dataclass(frozen=True)
class FrozenWorktreeSource:
    """Use a specific source and commit for a tool worktree."""

    checkout: Path
    commit: str


def make(repo: str, unit: str, short_name: str, snapshot: staff.StaffSnapshot | FrozenWorktreeSource) -> Path:
    """Make a named worktree from the selected source."""
    path = build_folders.worktree_path(repo, unit, short_name)
    if isinstance(snapshot, FrozenWorktreeSource):
        source = main_checkout(snapshot.checkout)
        if build_folders.repo_word(source) != repo:
            raise ValueError(f"{source}: the repo word {repo!r} differs from origin")
        owner, merge_branch, start = unit, "", snapshot.commit
    else:
        source = main_checkout_for_word(repo)
        owner, merge_branch = caller_branch(source, Path.cwd(), snapshot)
        start = "HEAD"
    return create_checkout(path, source, build_folders.NewWorktreeBranch(f"{unit}-{short_name}", start),
                           output_record(repo, unit, short_name), owner=owner, merge_branch=merge_branch,
                           fixed_base=isinstance(snapshot, FrozenWorktreeSource), use_recorded_branch=True)


def remove_git_checkout(path: Path, repository: Path, *, force: bool = False, retain_record: bool = False,
                        before_report: Callable[[], None] | None = None) -> None:
    """Report the checkout and its output for manual review.

    Call before_report after all checks and before the first notice.
    """
    _ = force
    if path.exists() and git_files.at(path).common_dir != git_files.locate(repository).common_dir:
        raise ValueError(f"{path}: worktree belongs to another repository")
    if path == main_checkout(repository):
        raise ValueError(f"{path}: cannot remove the main checkout")
    layout = worktree_output(path)
    reason = "the checkout is selected for removal"
    if isinstance(layout, RecordedWorktreeOutput):
        output = checked_output(layout)
        output_reason = reason
    else:
        output = _legacy_output(path)
        output_reason = "the legacy checkout ended"

    def report_output() -> None:
        if isinstance(output, Path):
            report_retained_folder(output, output_reason)

    if before_report is None:
        report_output()
    retain_checkout = os.path.lexists(path) or path in worktree_branches(repository)
    if before_report is not None and (isinstance(output, Path) or retain_checkout):
        before_report()
        report_output()
    if retain_checkout:
        report_retained_folder(path, reason)
    elif not retain_record:
        release_worktree_pair(path)
        record_file(path).unlink(missing_ok=True)


@dataclass(frozen=True)
class UnitFolderRename:
    repo: str
    old_unit: str
    new_unit: str
    source: Path
    destination: Path

    @property
    def old_build(self) -> Path:
        return build_folders.unit_folder(self.repo, self.old_unit)

    @property
    def new_build(self) -> Path:
        return build_folders.unit_folder(self.repo, self.new_unit)

    def check_available(self) -> None:
        for folder in (self.destination, self.new_build):
            if os.path.lexists(folder):
                raise FileExistsError(f"{folder}: the unit rename destination is occupied")

    def hold_copy_allocations(self) -> AbstractContextManager[None]:
        """Exclude copy allocation through the unit rename."""
        return build_folders.unit_allocation_lock(self.old_build, exclusive=True)

    @contextmanager
    def hold_outputs(self) -> Generator[None]:
        """Hold both names for each named output during the move."""
        old, new = self.old_build, self.new_build
        if old.is_symlink() or new.is_symlink():
            raise ValueError(f"{old}: expected real unit output folders")
        _review_rename_copies(old)
        names: set[str] = set()
        for folder in (old, new):
            if folder.is_symlink():
                raise ValueError(f"{folder}: expected a real unit folder")
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                name = build_folders.metadata_folder_name(entry.name)
                if name.isascii() and name.isdecimal():
                    continue
                form = build_folders.parse_form(folder / name)
                if isinstance(form, build_folders.UnverifiedFolderPath):
                    raise form.error
                if (not isinstance(form, build_folders.WorktreeOutput) or entry.is_symlink()
                        or (name == entry.name and not entry.is_dir())):
                    raise ValueError(f"{entry}: cannot verify the output for the unit rename")
                names.add(name)
        old_leases: list[build_folders.HeldFolderRemovalLease] = []
        with ExitStack() as locks:
            for folder in (old, new):
                for name in sorted(names):
                    path = folder / name
                    _ = locks.enter_context(build_folder_lock.folder_parent(
                        path, build_folders.build_root(), create=True))
                    lease = locks.enter_context(build_folders.folder_removal_lease(
                        path, root=build_folders.build_root(), create=True, wait=False))
                    if not isinstance(lease, build_folders.HeldFolderRemovalLease):
                        raise ValueError(f"{path}: the build folder lock is held. Rerun when idle")
                    if folder == old:
                        old_leases.append(lease)
            yield
            for lease in old_leases:
                if not os.path.lexists(lease.folder):
                    lease.unlink_sidecars(lease.sidecar_identities())
            if old.is_dir():
                report_retained_folder(old, "the unit name changed")


    def finish(self) -> None:
        """Move the remaining source folders and repair the new output paths."""
        if self.source.is_symlink() or self.destination.is_symlink():
            raise ValueError(f"{self.source}: expected real unit folders")
        with move_lock():
            self._move_build_folder()
            if self.source.is_dir():
                self.destination.mkdir(parents=True, exist_ok=True)
                for item in sorted(self.source.iterdir()):
                    target = self.destination / item.name
                    if os.path.lexists(target):
                        raise FileExistsError(f"{target}: the unit rename destination is occupied")
                    if (item / ".git").is_file() and not item.is_symlink():
                        entry = git_files.at(item)
                        _ = git(item, "worktree", "move", *_git_move_options(entry), str(item), str(target))
                    else:
                        _ = item.rename(target)
                report_retained_folder(self.source, "the unit name changed")
            self._move_aliases()
            if self.destination.is_dir():
                for item in sorted(self.destination.iterdir()):
                    if not item.is_symlink() and (item / ".git").is_file():
                        self._publish_checkout(self.source / item.name, item)

    def _move_build_folder(self) -> None:
        from scripts.lib.session_move import rename_exclusive

        old, new = self.old_build, self.new_build
        if not old.is_dir():
            return
        _review_rename_copies(old)
        if not new.is_dir():
            return
        root = build_folders.build_root()
        with (build_folder_lock.folder_parent(old / "output", root, create=False) as old_fd,
              build_folder_lock.folder_parent(new / "output", root, create=False) as new_fd):
            for path in sorted(old.iterdir()):
                if (build_folders.metadata_folder_name(path.name) != path.name
                        or path.name.isascii() and path.name.isdecimal()):
                    continue
                form = build_folders.parse_form(path)
                if isinstance(form, build_folders.UnverifiedFolderPath):
                    raise form.error
                if (not isinstance(form, build_folders.WorktreeOutput)
                        or not path.is_dir() or path.is_symlink()):
                    raise ValueError(f"{path}: cannot verify the output for the unit rename")
                destination = new / path.name
                try:
                    source_lock = build_folders.build_folder_lock_fd(path)
                    destination_lock = build_folders.build_folder_lock_fd(destination)
                except StopIteration:
                    raise ValueError(f"{path}: the output changed. Rerun when idle") from None
                if (not build_folder_lock.lock_current(path, old_fd, source_lock)
                        or not build_folder_lock.lock_current(destination, new_fd, destination_lock)):
                    raise ValueError(f"{path}: the output lock changed. Rerun when idle")
                if not build_folders.may_remove_folder_tree(path):
                    raise ValueError(f"{path}: a mount prevents the unit rename")
                rename_exclusive(old_fd, path.name, new_fd, path.name)

    def _renamed_path(self, path: Path) -> Path:
        if path.is_relative_to(self.source):
            return self.destination / path.relative_to(self.source)
        return path

    def _move_aliases(self) -> None:
        """Update both ends of each moved link and its journal key."""
        updates: dict[Path, MoveJournal] = {}
        links: dict[Path, tuple[Path, MoveJournal]] = {}
        for path in sorted(move_folder().glob("*.json")):
            journal = read_move(path)
            move = journal.move
            alias = self._renamed_path(move.old)
            destination = self._renamed_path(move.new)
            if journal.stage is MoveStage.PLANNED and not alias.is_symlink():
                continue
            members = tuple(self._renamed_path(member) for member in journal.clone_members)
            updated = replace(move, old=alias, new=destination,
                              unit=self.new_unit if destination != move.new else move.unit)
            if move == updated and members == journal.clone_members and journal.source_key == alias:
                continue
            update = replace(journal, move=updated, clone_members=members, record_source=MoveRecordKey.FROM_LINK_PATH)
            new_path = move_file(alias)
            if new_path != path and os.path.lexists(new_path) and read_move(new_path) != update:
                raise FileExistsError(f"{new_path}: the migration journal destination is occupied")
            updates[path] = update
            if alias.is_symlink() and (target := alias.readlink()) in (move.new, destination):
                links[alias] = target, update
            elif journal.stage is MoveStage.PLANNED:
                raise ValueError(f"{alias}: the migration link changed")
        for alias, (_, journal) in links.items():
            target = journal.move.new
            seen = {alias}
            while target in links:
                if target in seen:
                    raise ValueError(f"{alias}: the migration links form a cycle")
                seen.add(target)
                target = links[target][1].move.new
            if not journal.destination.matches(target.resolve()):
                raise ValueError(f"{target}: the migration destination changed")
        for path, journal in updates.items():
            alias = journal.move.old
            destination = journal.move.new
            if alias in links and (previous := links[alias][0]) != destination:
                with tempfile.TemporaryDirectory(prefix=".worktree-link-", dir=alias.parent) as directory:
                    link = Path(directory) / "worktree"
                    link.symlink_to(destination, target_is_directory=True)
                    if not alias.is_symlink() or alias.readlink() != previous:
                        raise ValueError(f"{alias}: the migration link changed")
                    _ = link.replace(alias)
            write_move(journal)
            if path != move_file(alias):
                path.unlink()

    def _publish_checkout(self, old: Path, new: Path) -> None:
        record = checkout_record(old) or checkout_record(new)
        target = new / "target"
        if ((record is not None and isinstance(record.layout, RecordedWorktreeOutput))
                or (target.is_symlink() and target.readlink() == self.old_build / new.name)):
            layout: RecordedWorktreeOutput | UnassignedOutput = output_record(self.repo, self.new_unit, new.name)
            ensure_target_link(new, checked_output(layout))
        else:
            layout = UnassignedOutput()
        worktree_links.link_transcripts(old, new)
        _ = _update_worktree_staff(old, new)
        if record is not None:
            head = git_files.head(git_files.at(new))
            owner = self.new_unit if record.owner == self.old_unit else record.owner
            branch = head.name if isinstance(head, git_files.OnBranch) else record.branch
            updated = replace(record, path=str(new), branch=branch, owner=owner, layout=layout)
            if updated != record:
                write_record(updated)
            record_file(old).unlink(missing_ok=True)


def sync_worktree_branch(checkout: Path) -> None:
    """Publish the checked out branch after a unit rename."""
    try:
        record = read_record(record_file(checkout))
    except FileNotFoundError:
        return
    head = git_files.head(git_files.at(checkout))
    if record.path != str(checkout) or not isinstance(head, git_files.OnBranch):
        raise ValueError(f"{checkout}: cannot verify the renamed worktree branch")
    if head.name != record.branch:
        write_record(replace(record, branch=head.name))


def unit_folder_rename(old: Path, new: Path) -> UnitFolderRename | None:
    """Read the unit folders from two paths in the current worktree layout."""
    root = Path(conf.read_or_repository("build_folders").text("roots.workspace")).expanduser().resolve()
    if not old.is_relative_to(root) or not new.is_relative_to(root):
        return None
    parts = old.relative_to(root).parts
    destination = new.relative_to(root).parts
    if len(parts) != 3 or len(destination) != 3:
        return None
    repo, old_unit, short_name = parts
    new_repo, new_unit, new_short = destination
    if repo != new_repo or short_name != new_short or old_unit == new_unit:
        return None
    _ = build_folders.worktree_path(repo, old_unit, short_name)
    _ = build_folders.worktree_path(repo, new_unit, short_name)
    return UnitFolderRename(repo, old_unit, new_unit, old.parent, new.parent)


def unit_rename_destination(folder: Path, old_unit: str, new_unit: str) -> Path | None:
    """Keep the short name when the unit owns a folder in the layout."""
    if folder.parent.name != old_unit or old_unit == new_unit:
        return None
    destination = folder.parent.with_name(new_unit) / folder.name
    rename = unit_folder_rename(folder, destination)
    return _move_destination(rename.repo, new_unit, folder.name) if rename is not None else None


class MoveStage(Enum):
    PLANNED = "planned"
    LINKED = "linked"
    FINISHED = "finished"


class MoveOutput(Enum):
    LIVE_UNIT = "live-unit"
    RETIRED_UNIT = "retired-unit"


@dataclass(frozen=True)
class FolderIdentity:
    device: int
    inode: int

    @classmethod
    def read(cls, folder: Path) -> FolderIdentity:
        status = folder.lstat()
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError(f"{folder}: expected a real folder")
        return cls(status.st_dev, status.st_ino)

    def matches(self, folder: Path) -> bool:
        try:
            return self == FolderIdentity.read(folder)
        except (FileNotFoundError, ValueError):
            return False


@dataclass(frozen=True)
class WorktreeMove:
    old: Path
    new: Path
    repo: str
    unit: str
    short_name: str
    output: MoveOutput


class MoveRecordKey(Enum):
    """The initial link path identifies the journal."""

    FROM_LINK_PATH = "from-link-path"


@dataclass(frozen=True)
class MoveJournal:
    move: WorktreeMove
    source: FolderIdentity
    destination: FolderIdentity
    copy_tag: str
    git_entry: Path
    clone_members: tuple[Path, ...]
    stage: MoveStage = MoveStage.PLANNED
    record_source: Path | MoveRecordKey = MoveRecordKey.FROM_LINK_PATH

    @property
    def source_key(self) -> Path:
        return self.record_source if isinstance(self.record_source, Path) else self.move.old

    @property
    def staging(self) -> Path:
        return self.move.new.with_name(f".worktree-move-{self.copy_tag}")

    @property
    def backup(self) -> Path:
        return self.move.old.with_name(f".worktree-move-{self.copy_tag}")


def move_folder() -> Path:
    return state_folder().parent / "moves"


def move_file(path: Path) -> Path:
    return move_folder() / record_file(path).name


@contextmanager
def move_lock() -> Generator[None]:
    move_folder().mkdir(parents=True, exist_ok=True)
    with (move_folder() / "move.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refused(1, "another worktree move is in progress") from error
        yield


def read_move(path: Path) -> MoveJournal:
    fields = cast(dict[str, object], json.loads(path.read_text()))
    move = WorktreeMove(
        Path(cast(str, fields["old"])), Path(cast(str, fields["new"])),
        cast(str, fields["repo"]), cast(str, fields["unit"]), cast(str, fields["short_name"]),
        MoveOutput(fields["output"]))
    record_source = fields.get("record_source")
    if record_source is not None and (not isinstance(record_source, str) or not Path(record_source).is_absolute()):
        raise ValueError(f"{path}: invalid move record source")
    journal = MoveJournal(
        move, FolderIdentity(*cast(tuple[int, int], fields["source"])),
        FolderIdentity(*cast(tuple[int, int], fields["destination"])), cast(str, fields["copy_tag"]),
        Path(cast(str, fields["git_entry"])), tuple(Path(item) for item in cast(list[str], fields["clone_members"])),
        MoveStage(fields["stage"]),
        Path(record_source) if isinstance(record_source, str) else MoveRecordKey.FROM_LINK_PATH)
    if path != move_file(journal.source_key) or not re.fullmatch(r"[0-9a-f]{32}|", journal.copy_tag):
        raise ValueError(f"{path}: invalid move record")
    return journal


def write_move(journal: MoveJournal) -> None:
    move = journal.move
    fields = {
        "old": str(move.old), "new": str(move.new), "repo": move.repo, "unit": move.unit,
        "short_name": move.short_name, "output": move.output.value, "copy_tag": journal.copy_tag,
        "source": (journal.source.device, journal.source.inode),
        "destination": (journal.destination.device, journal.destination.inode),
        "git_entry": str(journal.git_entry), "clone_members": list(map(str, journal.clone_members)),
        "stage": journal.stage.value,
    }
    if isinstance(journal.record_source, Path):
        fields["record_source"] = str(journal.record_source)
    write_text_atomic(move_file(journal.source_key), json.dumps(fields, indent=2) + "\n")


def _move_source(path: Path) -> Path:
    path = path.expanduser().absolute()
    return path.parent.resolve() / path.name


def _existing_move(path: Path) -> MoveJournal | None:
    try:
        return read_move(move_file(path))
    except FileNotFoundError:
        return None


def _move_destination(repo: str, unit: str, short_name: str) -> Path:
    destination = build_folders.worktree_path(repo, unit, short_name)
    for parent in (destination.parent, destination.parent.parent):
        if parent.is_symlink():
            raise ValueError(f"{parent}: a worktree parent must be a real folder")
    return destination.parent.resolve() / destination.name


def _scratch_root() -> Path:
    return build_folders.build_root().resolve() / "claude" / "scratch"


def _legacy_claude_present() -> bool:
    return _scratch_root().exists()


def _git_move_options(checkout: git_files.Checkout) -> tuple[str, ...]:
    if checkout.git_dir == checkout.common_dir:
        return ()
    try:
        reason = (checkout.git_dir / "locked").read_text().strip()
    except FileNotFoundError:
        return ()
    if not reason.startswith("staff:"):
        raise ValueError(f"{checkout.toplevel}: the Git worktree is locked: {reason}")
    return ("--force", "--force")


def _open_move(move: WorktreeMove, *, clone_members: tuple[Path, ...] = ()) -> MoveJournal:
    if move.new == move.old or move.new.is_relative_to(move.old) or move.old.is_relative_to(move.new):
        raise ValueError(f"{move.old}: invalid move destination {move.new}")
    if os.path.lexists(move.new):
        raise FileExistsError(f"{move.new}: worktree destination already exists")
    before = FolderIdentity.read(move.old)
    entry = git_files.at(move.old)
    _ = _git_move_options(entry)
    ancestor = next(folder for folder in move.new.parents if folder.is_dir())
    copy_required = before.device != ancestor.stat().st_dev
    if copy_required and move.old.parent != _scratch_root():
        raise ValueError(f"{move.old}: the worktree and its destination must use the same filesystem")
    journal = MoveJournal(move, before, before, uuid4().hex if copy_required else "",
                          entry.git_dir if entry.git_dir != entry.common_dir else Path(".git"), clone_members)
    write_move(journal)
    return journal


def _copy_across_filesystems(journal: MoveJournal) -> MoveJournal:
    old, new = journal.move.old, journal.move.new
    if not os.path.lexists(new):
        if not journal.source.matches(old):
            raise ValueError(f"{old}: source identity changed during the move")
        entry = git_files.at(old)
        _ = _git_move_options(entry)
        if entry.git_dir != (old / ".git" if journal.git_entry == Path(".git") else journal.git_entry):
            raise ValueError(f"{old}: the Git worktree entry changed")
        stage = journal.staging
        if not os.path.lexists(stage) or journal.destination == journal.source:
            if os.path.lexists(stage):
                report_retained_folder(stage, "a prior move left an unverified stage")
                raise ValueError(f"{stage}: review the prior move stage before retrying")
            if sys.platform == "linux":
                _ = subprocess.run(["cp", "-a", "--reflink=auto", "--", str(old), str(stage)], check=True)
            else:
                _ = shutil.copytree(old, stage, symlinks=True)
            journal = replace(journal, destination=FolderIdentity.read(stage))
            write_move(journal)
        if not journal.destination.matches(stage):
            raise ValueError(f"{stage}: staged worktree identity changed")
        _ = stage.rename(new)
    if not journal.destination.matches(new):
        raise ValueError(f"{new}: destination identity changed during the move")
    if journal.source.matches(old):
        if os.path.lexists(journal.backup):
            raise FileExistsError(f"{journal.backup}: move backup already exists")
        _ = old.rename(journal.backup)
    return journal


def _publish_move_source(journal: MoveJournal) -> MoveJournal:
    old, new = journal.move.old, journal.move.new
    new.parent.mkdir(parents=True, exist_ok=True)
    if journal.copy_tag:
        journal = _copy_across_filesystems(journal)
    elif not os.path.lexists(new):
        if not journal.source.matches(old):
            raise ValueError(f"{old}: source identity changed during the move")
        if journal.git_entry == Path(".git"):
            _ = old.rename(new)
        else:
            entry = git_files.at(old)
            if entry.git_dir != journal.git_entry:
                raise ValueError(f"{old}: the Git worktree entry changed")
            main = main_checkout(old)
            if any(tree.path == new for tree in linked_trees(main)):
                raise FileExistsError(f"{new}: Git already registers the move destination")
            _ = git(main, "worktree", "move", *_git_move_options(entry), str(old), str(new))
    if not journal.destination.matches(new):
        raise ValueError(f"{new}: destination identity changed during the move")
    _ = git(new, "worktree", "repair", *map(str, journal.clone_members))
    checkout = git_files.at(new)
    if checkout.git_dir != (new / ".git" if journal.git_entry == Path(".git") else journal.git_entry):
        raise ValueError(f"{new}: the move changed the Git worktree entry")
    for member in journal.clone_members:
        if git_files.at(member).common_dir != checkout.common_dir:
            raise ValueError(f"{member}: Git still points to the old clone store")
    if old.exists() and not old.is_symlink():
        raise FileExistsError(f"{old}: the former worktree path is occupied")
    try:
        old.symlink_to(new, target_is_directory=True)
    except FileExistsError:
        if not old.is_symlink() or old.resolve() != new:
            raise ValueError(f"{old}: the former path does not point to {new}") from None
    worktree_links.link_transcripts(old, new)
    _ = _update_worktree_staff(old, new)
    journal = replace(journal, stage=MoveStage.LINKED)
    write_move(journal)
    return journal


def _update_worktree_staff(old: Path, new: Path) -> str:
    from scripts.agents.remote import local_machine
    from scripts.production import staff

    machine = local_machine()
    owner = ""
    for row in staff_record().rows:
        current = row.worktree
        if not isinstance(current, (staff.PresentWorktree, staff.RemovedWorktree)) or current.machine != machine:
            continue
        if _move_source(current.path) == old:
            _ = staff.set_worktree(row.id, replace(current, path=new), expected=current)
        elif current.path != new:
            continue
        owner = str(row.id)
    return owner


def _finish_worktree_move(journal: MoveJournal) -> MoveJournal:
    if journal.stage is MoveStage.FINISHED:
        return journal
    if journal.stage is MoveStage.PLANNED:
        journal = _publish_move_source(journal)
    move = journal.move
    if not journal.destination.matches(move.new):
        raise ValueError(f"{move.new}: destination identity changed during the move")
    repo = main_checkout(move.new)
    if journal.copy_tag and journal.source.matches(journal.backup):
        report_retained_folder(journal.backup, "the worktree moved to a new filesystem")
    if move.output is MoveOutput.LIVE_UNIT:
        if move.repo == "claude" and _legacy_claude_present():
            print(f"defer output {move.new}: archive the old build root first")
            return journal
        layout: RecordedWorktreeOutput | UnassignedOutput = output_record(move.repo, move.unit, move.short_name)
        ensure_target_link(move.new, checked_output(layout))
    else:
        layout = UnassignedOutput()
        if (move.new / "target").is_symlink():
            git_exclude.require_untracked(move.new, "target")
            (move.new / "target").unlink()
    previous = checkout_record(move.old) or checkout_record(move.new)
    if previous is None:
        branch = git_files.head(git_files.at(move.new))
        head = branch.name if isinstance(branch, git_files.OnBranch) else ""
        previous = new_record(move.new, head, repo, git(move.new, "rev-parse", "HEAD").stdout.strip(),
                              _update_worktree_staff(move.old, move.new), "")
    write_record(replace(previous, path=str(move.new), repo=str(repo), layout=layout))
    for member in journal.clone_members:
        try:
            recorded = read_record(record_file(member))
        except FileNotFoundError:
            continue
        write_record(replace(recorded, repo=str(repo)))
    record_file(move.old).unlink(missing_ok=True)
    journal = replace(journal, stage=MoveStage.FINISHED)
    write_move(journal)
    return journal


def move_worktree(old: Path, repo: str, unit: str, short_name: str) -> Path:
    """Move a linked checkout and resume any interrupted publication."""
    old = _move_source(old)
    new = _move_destination(repo, unit, short_name)
    with move_lock():
        journal = _existing_move(old)
        if journal is not None:
            request = journal.move
            if (request.new, request.repo, request.unit, request.short_name) != (new, repo, unit, short_name):
                raise ValueError(f"{old}: this request differs from the saved move")
            if journal.stage is MoveStage.FINISHED:
                if not journal.destination.matches(new):
                    raise ValueError(f"{new}: the recorded destination has changed")
                return new
        if repo == "claude" and _legacy_claude_present():
            raise ValueError(f"{old}: archive the legacy build root before moving this worktree")
        if journal is None:
            checkout = git_files.at(old)
            if checkout.git_dir == checkout.common_dir:
                raise ValueError(f"{old}: expected a linked Git worktree")
            if build_folders.repo_word(main_checkout(old)) != repo:
                raise ValueError(f"{old}: the repo word differs from origin")
            journal = _open_move(WorktreeMove(old, new, repo, unit, short_name, MoveOutput.LIVE_UNIT))
        _ = _finish_worktree_move(journal)
    return new


@dataclass(frozen=True)
class OldWorktreeLink:
    old: Path
    new: Path
    sessions: tuple[worktree_links.SessionStart, ...]
    unreadable: tuple[str, ...]
    removed: bool


@dataclass(frozen=True)
class AllOldLinks:
    """Inspect every recorded old path."""


@dataclass(frozen=True)
class RestartedWorktrees:
    """Inspect old paths only for worktrees with a confirmed restart."""

    destinations: frozenset[Path]


ALL_OLD_LINKS = AllOldLinks()


def old_worktree_links(*, remove_unused: bool = False,
                       scope: AllOldLinks | RestartedWorktrees = ALL_OLD_LINKS) -> tuple[OldWorktreeLink, ...]:
    """List old links and remove only links with no possible live session user."""
    if remove_unused and move_folder().is_dir():
        with move_lock():
            return _old_worktree_links(scope, remove_unused=True)
    return _old_worktree_links(scope, remove_unused=False)


def _old_worktree_links(scope: AllOldLinks | RestartedWorktrees, *,
                        remove_unused: bool) -> tuple[OldWorktreeLink, ...]:
    journals = [read_move(path) for path in sorted(move_folder().glob("*.json"))]
    links = {journal.move.old: journal for journal in journals if journal.stage is not MoveStage.PLANNED
             and journal.move.old.is_symlink() and journal.move.old.readlink() == journal.move.new}
    selected = tuple(journal for journal in links.values()
                     if isinstance(scope, AllOldLinks) or journal.move.new.resolve() in scope.destinations)
    if not selected:
        return ()
    evidence = worktree_links.live_session_starts()
    result: list[OldWorktreeLink] = []
    for journal in selected:
        move = journal.move
        dependent_paths = {move.old}
        while True:
            incoming = {old for old, entry in links.items() if entry.move.new in dependent_paths}
            if incoming <= dependent_paths:
                break
            dependent_paths.update(incoming)
        using = tuple(session for session in evidence.sessions if any(
            path.is_relative_to(old) for path in session.paths for old in dependent_paths))
        removed = False
        if (remove_unused and journal.stage is MoveStage.FINISHED and not using and not evidence.unreadable
                and journal.destination.matches(move.new.resolve()) and dependent_paths == {move.old}):
            if move.old.is_symlink() and move.old.readlink() == move.new:
                move.old.unlink()
                removed = True
        result.append(OldWorktreeLink(move.old, move.new, using, evidence.unreadable, removed))
    return tuple(result)


@dataclass(frozen=True)
class RegisteredTree:
    store: Path
    path: Path
    branch: str


def linked_trees(repo: Path) -> tuple[RegisteredTree, ...]:
    """Read each linked entry, including detached and missing worktrees."""
    sections = git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0\0")
    entries: list[RegisteredTree] = []
    for section in sections[1:]:
        fields = section.split("\0")
        if fields[0].startswith("worktree "):
            path = Path(fields[0].removeprefix("worktree "))
            branch = next((field.removeprefix("branch refs/heads/") for field in fields[1:]
                           if field.startswith("branch refs/heads/")), "")
            entries.append(RegisteredTree(repo, path, branch))
    return tuple(entries)


@dataclass(frozen=True)
class ListedWorktree:
    unit: str
    short_name: str


def listed_worktrees(path: Path) -> dict[Path, ListedWorktree]:
    listed: dict[Path, ListedWorktree] = {}
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        fields = shlex.split(line, comments=True)
        if not fields:
            continue
        if len(fields) != 3:
            raise ValueError(f"{path}:{line_number}: expected a path, a unit, and a short name")
        old = _move_source(Path(fields[0]))
        if old in listed:
            raise ValueError(f"{path}:{line_number}: duplicate path {old}")
        listed[old] = ListedWorktree(fields[1], fields[2])
    return listed


def _without_name(path_name: str, word: str) -> str:
    return re.sub(rf"(?<![A-Za-z0-9]){re.escape(word)}(?![A-Za-z0-9])", "", path_name, flags=re.IGNORECASE)


def _subject_unit(path: Path) -> str:
    subject = git(path, "log", "-1", "--format=%s", "--grep=^checkpoint(", "--grep=^shrink(", "HEAD").stdout
    match = re.match(r"^(?:checkpoint|shrink)\(([^)]+)\)", subject)
    return match[1] if match else ""


def checkout_record(path: Path) -> WorktreeRecord | None:
    """Read the record for this checkout path if it exists."""
    try:
        value = read_record(record_file(path))
    except FileNotFoundError:
        return None
    if value.path != str(path):
        raise ValueError(f"{path}: worktree record names a different path")
    return value


@dataclass(frozen=True)
class UnitFact:
    unit: str
    source: str
    production: str = ""


def _member_unit(row: staff.StaffRow, snapshot: staff.StaffSnapshot, source: str) -> UnitFact:
    parent = snapshot.parent_of(row)
    return UnitFact(row.name, source, parent.name if parent is not None else "")


@dataclass(frozen=True)
class RepositoryNames:
    word: str
    origin: str


@dataclass(frozen=True)
class MigrationShortNames:
    machines: frozenset[str]
    productions: frozenset[str]
    checkouts: dict[str, set[str]]

    def unit_from_subject(self, slug: str, repo: RepositoryNames) -> str:
        return _strip_unit_prefixes(slug, self.productions | {repo.word, repo.origin})

    def for_tree(self, tree: RegisteredTree, repo: RepositoryNames, unit: str) -> str:
        checkout_words = self.checkouts.get(repo.word, set()) | {repo.word, repo.origin}
        if tree.store != tree.path:
            checkout_words.add(tree.store.name)

        words = sorted(checkout_words | self.productions | self.machines | {unit},
                       key=lambda token: (-len(token), token))

        def remove_words(value: str) -> str:
            value = value.removeprefix("init/")
            for word in words:
                value = _without_name(value, word)
            return re.sub(r"[-_/]{2,}|/", "-", value).strip("-_.")

        name = remove_words(tree.path.name)
        if not name:
            return UNIT_WORKTREE_NAME
        if name.isdecimal():
            name = remove_words(tree.branch) or name
            if name.isdecimal():
                return "p" + name
        return name


@dataclass(frozen=True)
class ShowrunnerCheckout:
    path: Path
    reason: str


def _strip_unit_prefixes(slug: str, prefixes: frozenset[str] | set[str]) -> str:
    words = sorted(filter(None, prefixes), key=lambda name: (-len(name), name))
    while True:
        for word in words:
            match = re.match(rf"{re.escape(word)}[-/]", slug, re.IGNORECASE)
            if match is not None:
                slug = slug[match.end():]
                break
        else:
            return slug


def _production_checkout(lines: list[str]) -> Path:
    from scripts.production import production_doc

    path = Path(production_doc.production_field(lines, "Showrunner checkout")).expanduser()
    if not path.is_absolute():
        raise production_doc.Refusal("Showrunner checkout must be an absolute path")
    return path.resolve()


def _archived_showrunner_checkouts(archive: Path) -> dict[Path, str]:
    from scripts.production import production_doc

    checkouts: dict[Path, str] = {}
    for doc in sorted(archive.rglob("*-production.md")):
        try:
            path = _production_checkout(doc.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError, production_doc.Refusal):
            continue
        name = doc.name.removesuffix("-production.md")
        checkouts[path] = f"showrunner checkout awaits question 4; archived production {name} at {doc}"
    return checkouts


def _migration_names(trees: list[RegisteredTree], repos: dict[Path, RepositoryNames], mains: tuple[Path, ...],
                     snapshot: staff.StaffSnapshot, machine: str
                     ) -> tuple[MigrationShortNames, dict[Path, str]]:
    from scripts.production import production_doc, staff

    machines = conf.read_or_repository("machines")
    aliases = machines.words("aliases")
    machine_words = {machine, *aliases, *(host for alias in aliases for host in machines.words(f"{alias}.hosts"))}
    # Tool-based UI was a Hana production before the current staff records.
    production_words = {"tool-based-ui"}
    checkout_words: dict[str, set[str]] = {}
    for root in mains:
        if (repo := repos.get(root)) is not None:
            checkout_words.setdefault(repo.word, set()).add(root.name)
    by_path = {tree.path: tree for tree in trees}
    showrunners = _archived_showrunner_checkouts(Path.home() / "rust/hanadocs/archive")
    for row in snapshot.rows:
        if row.kind != "showrunner":
            continue
        production_words.add(row.name)
        if not isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            continue
        if row.doc.machine != machine:
            continue
        doc = row.doc.path
        source = f"{row.name} staff row -> {doc}"
        recorded: ShowrunnerCheckout | None = None
        try:
            lines = doc.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            lines = []
        formerly = production_doc.optional_field(lines, "Formerly")
        if isinstance(formerly, production_doc.FieldPresent):
            name = production_doc.cell_value(formerly.text)
            if name:
                production_words.add(name)
        try:
            checkout = _production_checkout(lines)
        except production_doc.Refusal:
            pass
        else:
            recorded = ShowrunnerCheckout(checkout, f"{source} (Showrunner checkout)")
        if recorded is None:
            parent = next((path for path in doc.resolve().parents if path in by_path), None)
            if parent is not None:
                recorded = ShowrunnerCheckout(parent, f"{source} (document in checkout)")
        if recorded is not None:
            showrunners[recorded.path] = f"showrunner checkout awaits question 4; {recorded.reason}"
        if recorded is not None and (tree := by_path.get(recorded.path)) is not None:
            if (repo := repos.get(tree.store)) is not None:
                checkout_words.setdefault(repo.word, set()).add(tree.path.name)
            if tree.branch.startswith("init/"):
                production_words.add(tree.branch.split("/")[1])
    names = MigrationShortNames(frozenset(machine_words), frozenset(production_words), checkout_words)
    return names, showrunners


def _migration_maker_roots(scratch: Path) -> tuple[Path, ...]:
    # The base setting names the former scratch root, even when the host uses the build drive.
    settings = conf.read_or_repository("build_folders").settings
    former_scratch = copy_record.FORMER_ROOT
    if isinstance(settings, dict) and (setting := settings.get("roots.scratch")) is not None and setting.value:
        former_scratch = Path(setting.value).expanduser()
    roots = (Path("/tmp/claude/unit"), Path("/tmp/claude/delegate"), scratch.parents[1] / "slot",
             session_folder.configured_root().resolve(), Path.home() / ".local")
    former_scratch = former_scratch.resolve()
    return roots if former_scratch == scratch else (*roots, former_scratch)


def _migration_skip(path: Path, scratch: Path, maker_roots: tuple[Path, ...]) -> str:
    home = Path.home()
    if any(parts == (".claude", "worktrees") for parts in zip(path.parts, path.parts[1:])):
        return "Claude Code owns this worktree"
    if path == home / "worktrees/nate_style-message-clarity":
        return "the user's worktree"
    if path == scratch / "sessionmove-bbf204":
        return "queue moves the session-folder checkout before the switch"
    if (any(path.is_relative_to(root) for root in maker_roots)
            or (path.is_relative_to(scratch) and path != scratch
                and path.relative_to(scratch).parts[0].startswith("gate-"))
            or any(part.startswith("b44doc-") for part in path.parts)):
        return "the maker owns this worktree"
    return ""


@dataclass(frozen=True)
class PlannedWorktree:
    tree: RegisteredTree
    move: WorktreeMove
    source: str


@dataclass(frozen=True)
class MainOutputPlan:
    checkout: Path
    repo: str


@dataclass(frozen=True)
class WorktreeMigration:
    moves: tuple[PlannedWorktree, ...]
    missing: tuple[RegisteredTree, ...]
    main: tuple[MainOutputPlan, ...]
    clone_members: dict[Path, tuple[Path, ...]]
    skipped: dict[Path, str]


def _migration_main_checkouts(home: Path) -> tuple[Path, ...]:
    candidates = [home / ".claude", *(home / "rust").iterdir()] if (home / "rust").is_dir() else [home / ".claude"]
    return tuple(sorted(path.resolve() for path in candidates
                        if not path.is_symlink() and (path / ".git").is_dir()))


def _repository_names(checkout: Path, *, follow_local_origin: bool) -> RepositoryNames:
    """Find the upstream repository when a scratch clone uses a local origin."""
    current = checkout
    visited: set[Path] = set()
    while current not in visited:
        visited.add(current)
        origin = git(current, "remote", "get-url", "origin", check=False).stdout.strip()
        local = (current / origin).resolve()
        if not follow_local_origin or not origin or not os.path.lexists(local / ".git"):
            word = build_folders.repo_word(current)
            origin_word = origin.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
            return RepositoryNames(word, origin_word)
        current = main_checkout(local)
    raise build_folders.InvalidFolderForm(f"{checkout}: local Git origins form a loop")


_UNVERIFIED_SCRATCH = "no verified Git registration"
_ARCHIVED_SCRATCH = "no origin; moves with the build root to old-claude"


def _scratch_checkouts(scratch: Path) -> tuple[Path, ...]:
    if scratch.is_symlink():
        raise ValueError(f"{scratch}: expected a real scratch directory")
    if not scratch.is_dir():
        return ()
    return tuple(sorted(path for path in scratch.iterdir() if not path.is_symlink()
                        and not path.name.startswith(".worktree-move-") and os.path.lexists(path / ".git")))


def _live_migration_repos(units: list[staff.StaffRow], repos: dict[Path, RepositoryNames],
                          machine: str) -> dict[staff.StaffId, str]:
    from scripts.production import staff

    by_git_dir: dict[Path, str] = {}
    for path, repo in repos.items():
        try:
            by_git_dir[git_files.at(path).common_dir] = repo.word
        except git_files.GitFilesError:
            continue
    confirmed: dict[staff.StaffId, str] = {}
    for row in units:
        location = row.worktree
        if not isinstance(location, staff.PresentWorktree) or location.machine != machine:
            continue
        try:
            checkout = git_files.at(location.path)
            repo_word = by_git_dir.get(checkout.common_dir)
            confirmed[row.id] = repo_word if repo_word is not None else build_folders.repo_word(checkout.toplevel)
        except (git_files.GitFilesError, build_folders.InvalidFolderForm):
            continue
    return confirmed


def plan_worktree_migration(listed: dict[Path, ListedWorktree],
                            snapshot: staff.StaffSnapshot) -> WorktreeMigration:
    """Read Git, staff, and the supplied list without changing them."""
    from scripts.agents.remote import local_machine
    from scripts.production import staff

    machine = local_machine()
    scratch = _scratch_root()
    maker_roots = _migration_maker_roots(scratch)
    mains = _migration_main_checkouts(Path.home())
    clones: dict[Path, tuple[RegisteredTree, ...]] = {}
    skipped: dict[Path, str] = {path: "main checkout stays in place" for path in mains}
    trees: list[RegisteredTree] = []
    for path in _scratch_checkouts(scratch):
        reason = _migration_skip(path, scratch, maker_roots)
        if reason:
            skipped[path] = reason
        elif (path / ".git").is_dir():
            entries = linked_trees(path)
            if (path.name == "crprof-8a8cbf" and not entries
                    and git(path, "config", "--get", "remote.origin.url", check=False).returncode == 1):
                skipped[path] = _ARCHIVED_SCRATCH
                continue
            clones[path] = entries
            branch = git_files.head(git_files.at(path))
            trees.append(RegisteredTree(path, path, branch.name if isinstance(branch, git_files.OnBranch) else ""))
    for main in mains:
        trees.extend(linked_trees(main))
    for entries in clones.values():
        trees.extend(entries)

    repos: dict[Path, RepositoryNames] = {}
    main_plans: list[MainOutputPlan] = []
    for repo in (*mains, *clones):
        try:
            names = _repository_names(repo, follow_local_origin=repo in clones)
        except (build_folders.InvalidFolderForm, Refused) as error:
            skipped[repo] = str(error)
            continue
        repos[repo] = names
        if repo in mains:
            primary = _main_checkout_path(names.word)
            if not primary.is_symlink() and repo == primary.resolve():
                main_plans.append(MainOutputPlan(repo, names.word))
            else:
                skipped[repo] = f"keeps its current output; {primary} is the main checkout for {names.word}"

    for path in _scratch_checkouts(scratch):
        if path not in skipped and all(tree.path != path for tree in trees):
            skipped[path] = _UNVERIFIED_SCRATCH

    short_names, showrunners = _migration_names(trees, repos, mains, snapshot, machine)
    # Keep the known merge checkout if its staff document is unavailable.
    _ = showrunners.setdefault(Path.home() / "rust/hana_catalyst",
                               "showrunner checkout awaits question 4 (Hana merge checkout)")
    journals = [read_move(path) for path in sorted(move_folder().glob("*.json"))]
    placed = {journal.move.new for journal in journals}
    former = {path for journal in journals for path in (journal.move.old, journal.source_key)}
    stale = [tree for tree in trees if not os.path.lexists(tree.path) and tree.path not in former
             and tree.path not in showrunners
             and not _migration_skip(tree.path, scratch, maker_roots)]
    already_placed = {tree.path for tree in trees if tree.path in placed}
    for path in already_placed:
        skipped[path] = "already moved"
    candidates: list[RegisteredTree] = []
    for tree in trees:
        path = tree.path
        if path in already_placed or path in former or tree in stale:
            continue
        reason = showrunners.get(path) or _migration_skip(path, scratch, maker_roots)
        if reason:
            skipped[path] = reason
        elif path.is_symlink():
            skipped[path] = "Git names a symbolic link"
        elif tree.store not in repos:
            skipped[path] = "the repository has no usable origin name"
        else:
            try:
                _ = _git_move_options(git_files.at(path))
            except ValueError as error:
                skipped[path] = str(error)
            else:
                candidates.append(tree)

    units = [row for row in snapshot.rows if row.kind == "unit"]
    live_repos = _live_migration_repos(units, repos, machine)
    live_names = {row.name for row in units if isinstance(row.worktree, staff.PresentWorktree)}
    live = {(live_repos[row.id], row.name) for row in units if row.id in live_repos}
    retired = {row.name for row in units if isinstance(row.worktree, staff.RemovedWorktree)}
    known_by_repo: dict[str, dict[str, UnitFact]] = {repo.word: {} for repo in repos.values()}
    for row in units:
        fact = _member_unit(row, snapshot, "folder name")
        for repo, known in known_by_repo.items():
            if isinstance(row.worktree, staff.PresentWorktree) and live_repos.get(row.id) != repo:
                continue
            known[fact.unit] = fact
            if fact.production:
                _ = known.setdefault(f"{fact.production}-{fact.unit}", fact)
    for tree in candidates:
        supplied = listed.get(tree.path)
        if supplied is not None:
            known_by_repo[repos[tree.store].word][supplied.unit] = UnitFact(supplied.unit, "folder name")
    unit_words = {repo: sorted(known, key=lambda word: (-len(word), word))
                  for repo, known in known_by_repo.items()}
    moves: list[PlannedWorktree] = []
    destinations: dict[Path, Path] = {}
    collisions: set[Path] = set()
    for tree in candidates:
        path, repo = tree.path, repos[tree.store]
        supplied = listed.get(path)
        if supplied is not None:
            fact = UnitFact(supplied.unit, "list")
        else:
            row = snapshot.unit_by_worktree(machine, path)
            saved = checkout_record(path)
            if row is None and saved is not None and saved.owner:
                owner = snapshot.by_member_reference(saved.owner)
                if owner is None:
                    owner = next((member for member in units if member.name == saved.owner), None)
                if owner is not None:
                    row = owner if owner.kind == "unit" else snapshot.parent_of(owner)
            word = next((name for name in unit_words[repo.word] if _without_name(path.name, name) != path.name), "")
            if row is not None and row.kind == "unit":
                fact = _member_unit(row, snapshot, "staff row")
            elif word:
                fact = known_by_repo[repo.word][word]
            elif saved is not None and isinstance(saved.layout, RecordedWorktreeOutput):
                fact = UnitFact(saved.layout.unit, "worktree record")
            else:
                slug = _subject_unit(path)
                derived = short_names.unit_from_subject(slug, repo)
                if derived.casefold() in (repo.word.casefold(), repo.origin.casefold()):
                    derived = ""
                if derived in live_names and (repo.word, derived) not in live:
                    derived = ""
                fact = UnitFact(derived, "commit subject")
        unit = fact.unit
        if not unit:
            skipped[path] = "unit unknown"
            continue
        short_name = supplied.short_name if supplied is not None else short_names.for_tree(tree, repo, unit)
        if not short_name:
            skipped[path] = "the short name is empty"
            continue
        try:
            new = _move_destination(repo.word, unit, short_name)
        except ValueError as error:
            skipped[path] = str(error)
            continue
        if new == path:
            skipped[path] = "already at the destination"
            continue
        if os.path.lexists(new):
            skipped[path] = f"short name occupied at {new}"
            continue
        prior = destinations.setdefault(new, path)
        if prior != path:
            collisions.add(new)
        live_output = (repo.word, unit) in live or (
            supplied is not None and fact.unit not in retired and fact.unit not in live_names)
        output = MoveOutput.LIVE_UNIT if live_output else MoveOutput.RETIRED_UNIT
        move = WorktreeMove(path, new, repo.word, unit, short_name, output)
        moves.append(PlannedWorktree(tree, move, fact.source))

    selected = {plan.tree.path: plan for plan in moves if plan.move.new not in collisions}
    clone_members: dict[Path, tuple[Path, ...]] = {}
    for clone, members in clones.items():
        members = tuple(member for member in members if member not in stale)
        if clone not in selected or any(member.path not in selected and member.path not in already_placed
                                         for member in members):
            for path in (clone, *(member.path for member in members)):
                if path in selected:
                    del selected[path]
                    skipped[path] = f"{clone}: the clone and every linked worktree need move names"
            continue
        clone_members[clone] = tuple(selected[member.path].move.new if member.path in selected else member.path
                                     for member in members)
    for plan in moves:
        if plan.move.new in collisions:
            skipped[plan.tree.path] = f"short name shared by multiple worktrees at {plan.move.new}"
    for path in listed.keys() - {tree.path for tree in trees} - skipped.keys() - former:
        skipped[path] = "no registered worktree at this path"
    ordered = tuple(sorted(selected.values(), key=lambda plan: (plan.tree.path in clones, plan.tree.path)))
    return WorktreeMigration(ordered, tuple(stale), tuple(main_plans), clone_members, skipped)


def migrate_worktrees(list_file: Path, *, dry_run: bool) -> int:
    from scripts.production import staff

    listed = listed_worktrees(list_file)
    if dry_run:
        with conf_reads.record_reads(""):
            try:
                snapshot = staff.local_snapshot()
            except staff.StaffAbsent:
                snapshot = staff.StaffSnapshot(())
            return _apply_migration(listed, snapshot, dry_run=True)
    with move_lock():
        for path in sorted(move_folder().glob("*.json")):
            _ = _finish_worktree_move(read_move(path))
        return _apply_migration(listed, staff_record(), dry_run=False)


def _apply_migration(listed: dict[Path, ListedWorktree], snapshot: staff.StaffSnapshot, *, dry_run: bool) -> int:
    plan = plan_worktree_migration(listed, snapshot)
    for path, reason in sorted(plan.skipped.items()):
        print(f"keep {path}: {reason}")
    for tree in plan.missing:
        print(f"review {tree.path}: missing registration path")
    for move in plan.moves:
        layout = move.move
        names = f"{layout.unit} | {layout.short_name} | {move.source} | {layout.output.value}"
        print(f"move {layout.old} -> {layout.new} | {names}")
    legacy_claude = _legacy_claude_present()
    for main in plan.main:
        verb = "defer main output" if main.repo == "claude" and legacy_claude else "main output"
        print(f"{verb} {main.checkout} -> {build_folders.main_output_path(main.repo)}")
    if dry_run:
        for path in sorted(move_folder().glob("*.json")):
            journal = read_move(path)
            if journal.stage is not MoveStage.FINISHED:
                move = journal.move
                print(f"resume {move.old} -> {move.new} | {move.unit} | {move.short_name} | move record")
        return 0
    for tree in plan.missing:
        report_retained_folder(tree.store, f"Git still records the missing checkout {tree.path}")
    for planned in plan.moves:
        journal = _open_move(planned.move, clone_members=plan.clone_members.get(planned.tree.path, ()))
        _ = _finish_worktree_move(journal)
    for main in plan.main:
        if main.repo != "claude" or not legacy_claude:
            ensure_target_link(main.checkout, build_folders.main_output_path(main.repo))
    blocked_scratch = [path for path in _scratch_checkouts(_scratch_root())
                       if plan.skipped.get(path) not in (_UNVERIFIED_SCRATCH, _ARCHIVED_SCRATCH)]
    if blocked_scratch:
        print("keep the old build root until all remaining scratch checkouts have a confirmed location", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    from scripts.production import staff

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("make")
    _ = create.add_argument("repo")
    _ = create.add_argument("unit")
    _ = create.add_argument("short_name", metavar="short-name")
    remove = commands.add_parser("remove")
    _ = remove.add_argument("path", type=Path)
    _ = remove.add_argument("--force", action="store_true")
    retire_unit = commands.add_parser("retire")
    _ = retire_unit.add_argument("unit")
    migration = commands.add_parser("migrate")
    _ = migration.add_argument("--list", required=True, type=Path)
    _ = migration.add_argument("--dry-run", action="store_true")
    links = commands.add_parser("old-links")
    _ = links.add_argument("--remove-unused", action="store_true")
    check_cleanup = commands.add_parser("cleanup")
    _ = check_cleanup.add_argument("path")
    _ = check_cleanup.add_argument("--checked-tips", action="store_true")
    release = commands.add_parser("release-pair")
    _ = release.add_argument("path")
    arguments = parser.parse_args()
    if cast(str, arguments.command) == "release-pair":
        release_worktree_pair(Path(cast(str, arguments.path)).absolute())
        return 0
    if cast(str, arguments.command) == "cleanup":
        try:
            result = cleanup(Path(cast(str, arguments.path)).expanduser().resolve())
        except (Refused, OSError, ValueError, KeyError, TypeError) as error:
            print(str(error), file=sys.stderr)
            return 2
        if isinstance(result, Kept):
            print(result.reason)
            return 1
        if cast(bool, arguments.checked_tips):
            origin_tip = result.origin.sha if isinstance(result.origin, CheckedOriginTip) else ""
            print(f"{result.local.sha} {origin_tip}")
        else:
            print("cleanup: all work is on the target branch")
        return 0
    try:
        match cast(str, arguments.command):
            case "migrate":
                return migrate_worktrees(cast(Path, arguments.list), dry_run=cast(bool, arguments.dry_run))
            case "old-links":
                for link in old_worktree_links(remove_unused=cast(bool, arguments.remove_unused)):
                    users = ", ".join(session.identity for session in link.sessions) or "no known sessions"
                    status = "removed" if link.removed else "kept"
                    uncertainty = f"; unreadable: {', '.join(link.unreadable)}" if link.unreadable else ""
                    print(f"{status} {link.old} -> {link.new} | {users}{uncertainty}")
            case "remove":
                remove_git_checkout(cast(Path, arguments.path).expanduser().resolve(), Path.cwd(),
                                    force=cast(bool, arguments.force))
            case "retire":
                retire(cast(str, arguments.unit))
            case "make":
                path = make(cast(str, arguments.repo), cast(str, arguments.unit),
                            cast(str, arguments.short_name), staff_record())
                print(path)
            case _:
                raise AssertionError("unknown worktree command")
    except Refused as error:
        print(str(error), file=sys.stderr)
        return error.code
    except (OSError, ValueError, KeyError, staff.StaffRefused, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import errno
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
from contextlib import AbstractContextManager, ExitStack, contextmanager, redirect_stdout, suppress
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from functools import cached_property
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from scripts.lib.process_failure import failure_detail
from scripts.lib import build_folder_lock, build_folders, cancellation, conf, conf_reads, copy_record, file_lock, git_exclude, git_files, scratch, session_folder
from scripts.lib.atomic_file import write_text_atomic
from scripts.worktree import worktree_links

if TYPE_CHECKING:
    from scripts.lint import tmp_sweep
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
class Clean:
    """The check found no unsaved work in the checkout."""


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


def has_unit_word(owner: str, unit: str) -> bool:
    """Check if a completed record for this owner uses the unit word."""
    for path in state_folder().glob("*.json"):
        try:
            record = read_record(path)
        except (OSError, ValueError):
            continue
        if (record.owner == owner and record.stage is MakeStage.DONE
                and isinstance(record.layout, RecordedWorktreeOutput) and record.layout.unit == unit):
            return True
    return False


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


def _target_matches(target: Path, output: Path) -> bool:
    return target.is_symlink() and target.readlink() == output


def ensure_target_link(checkout: Path, output: Path, *, save_existing: bool = True) -> None:
    """Create the output and repair the checkout's target link."""
    target = checkout / "target"
    if save_existing and not target.is_symlink() and target.exists():
        _link_with_previous_target(checkout, output, target)
        return
    ensure_output_folder(output)
    if _target_matches(target, output):
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
    raise ValueError(f"{target}: expected a link to {output}; retain the existing path")


_EARLIER_CARGO_TARGET = "the checkout's earlier Cargo target"
_MAIN_STAYS = "main checkout stays in place"
_ALREADY_MOVED = "already moved"
_AT_DESTINATION = "already at the destination"
# Keeps that need no further work.
_SETTLED_KEEPS = frozenset((_MAIN_STAYS, _ALREADY_MOVED, _AT_DESTINATION))


class TargetReappeared(FileExistsError):
    """A new target appeared after the move, so the link failed; `kept` holds the earlier target."""

    def __init__(self, error: OSError, kept: Path) -> None:
        super().__init__(error.errno, error.strerror, cast(object, error.filename), None, cast(object, error.filename2))
        self.kept: Path = kept


def _saved_target_prefix(checkout: Path) -> str:
    return f".saved-target-{checkout.name}-"


def _report_saved_targets(checkout: Path) -> None:
    for saved in _saved_targets(checkout):
        report_retained_folder(saved, _EARLIER_CARGO_TARGET)


def _saved_targets(checkout: Path) -> tuple[Path, ...]:
    """Find the folders that hold an earlier target of this checkout, beside the checkout."""
    name = re.compile(re.escape(_saved_target_prefix(checkout)) + r"[a-z0-9_]{8}")
    with suppress(FileNotFoundError):
        return tuple(sorted(entry for entry in checkout.parent.iterdir() if name.fullmatch(entry.name)))
    return ()


def _link_with_previous_target(checkout: Path, output: Path, previous: Path) -> None:
    """Use or retain the previous target while holding the output lock."""
    target = checkout / "target"
    git_exclude.ensure_excluded(checkout, "/target")
    with build_folder_lock.folder_parent(output, build_folders.build_root(), create=True) as parent, ExitStack() as locks:
        try:
            descriptor = build_folders.build_folder_lock_fd(output)
        except StopIteration:
            descriptor = build_folder_lock.open_lock(output, parent, create=True)
            _ = locks.callback(os.close, descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError(f"{output}: the build folder lock is held. Rerun when idle") from error
        if not build_folder_lock.lock_current(output, parent, descriptor):
            raise ValueError(f"{output}: the build folder lock changed. Rerun when idle")
        _ensure_output_directory(output, parent)
        if stat.S_ISDIR(previous.lstat().st_mode):
            # A stop between the rename and the link leaves the checkout with no target.
            with cancellation.defer_cleanup_signals():
                try:
                    os.rename(previous, output.name, dst_dir_fd=parent)
                except OSError as error:
                    if error.errno not in (errno.EXDEV, errno.EEXIST, errno.ENOTEMPTY, errno.EBUSY):
                        raise
                else:
                    try:
                        target.symlink_to(output, target_is_directory=True)
                    except OSError as error:
                        if os.path.lexists(target):
                            raise TargetReappeared(error, output) from error
                        os.rename(output.name, target, src_dir_fd=parent)
                        raise
                    return
        if previous == target:
            _save_target_and_link(target, output)
        else:
            report_retained_folder(previous, _EARLIER_CARGO_TARGET)
            target.symlink_to(output, target_is_directory=True)


def _save_target_and_link(target: Path, output: Path) -> None:
    saved = Path(tempfile.mkdtemp(prefix=_saved_target_prefix(target.parent), dir=target.parent.parent)) / "target"
    with cancellation.defer_cleanup_signals():
        try:
            _ = target.rename(saved)
        except OSError:
            report_retained_folder(saved.parent, "target move failed")
            raise
        try:
            target.symlink_to(output, target_is_directory=True)
        except OSError as error:
            if not os.path.lexists(target):
                _ = saved.rename(target)
                report_retained_folder(saved.parent, "target link creation failed")
            else:
                report_retained_folder(saved.parent, _EARLIER_CARGO_TARGET)
                if isinstance(error, FileExistsError):
                    raise TargetReappeared(error, saved) from error
            raise
    report_retained_folder(saved.parent, _EARLIER_CARGO_TARGET)


def ensure_output_folder(output: Path) -> None:
    """Create the output without following links below the build root."""
    with build_folder_lock.folder_parent(output, build_folders.build_root(), create=True) as parent:
        _ensure_output_directory(output, parent)


def _ensure_output_directory(output: Path, parent: int) -> None:
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


def _registered_worktree_branches(repo: Path) -> Generator[tuple[Path, str]]:
    path = repo
    for field in git(repo, "worktree", "list", "--porcelain", "-z").stdout.split("\0"):
        if field.startswith("worktree "):
            path = Path(field.removeprefix("worktree "))
        elif field.startswith("branch refs/heads/"):
            yield path, field.removeprefix("branch refs/heads/")


def worktree_branches(repo: Path) -> dict[Path, str]:
    return {path.resolve(): branch for path, branch in _registered_worktree_branches(repo)}


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


def dirty_check(path: Path) -> Kept | Clean:
    """Keep a checkout with uncommitted changes, and name them on one line."""
    dirty = git(path, "status", "--porcelain").stdout
    return Kept(path, "dirty worktree: " + " ".join(dirty.splitlines())) if dirty else Clean()


def cleanup(path: Path) -> CleanupReady | Kept:
    _ = git(path, "fetch", "--no-prune", "origin")
    if isinstance(dirty := dirty_check(path), Kept):
        return dirty
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
        record = None
    if record is not None:
        if Path(record.repo).resolve() != repo or Path(record.path).resolve() != path or record.branch != branch:
            return Kept(path, "the worktree does not match its record")
        if record.stage is MakeStage.MAKING:
            return Kept(path, "worktree creation is incomplete")
    if record is None or not record.merge_branch:
        default = git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False)
        if default.returncode == 1:
            raise Refused(1, "origin default branch is missing")
        if default.returncode:
            raise Refused(1, default.stderr.strip())
        target = default.stdout.strip()
        base = git(repo, "merge-base", f"refs/heads/{branch}", target).stdout.strip()
        record = new_record(path, branch, repo, base, "", target.removeprefix("refs/remotes/origin/"))
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
        if isinstance(dirty := dirty_check(path), Kept):
            return dirty
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
    checkout_present = os.path.lexists(path)
    if not checkout_present:
        for registered, branch in _registered_worktree_branches(repo):
            if registered.resolve() == path:
                checkout_present = True
            elif branch == record.branch:
                return Kept(path, f"Git registers branch {branch} at {registered}")
    if checkout_present:
        if isinstance(record.layout, RecordedWorktreeOutput):
            report_retained_folder(checked_output(record.layout), "the checkout landed")
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
                    use_recorded_branch: bool = False, record_adoption: bool = False,
                    carry_envrc_permission: bool = False) -> Path:
    """Finish a recorded checkout or make one through the shared allocator.

    Unit admission opts in to setting up and recording an existing checkout.
    """
    path = path.absolute()
    repo = main_checkout(source)
    with move_lock():
        record = checkout_record(path)
        exists = os.path.lexists(path)
        if record is not None:
            if (exists and isinstance(record.layout, UnassignedOutput) and isinstance(layout, RecordedWorktreeOutput)
                    and _unfinished_move_at(path.resolve())):
                raise FileExistsError(f"{path}: editable repository destination is occupied")
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
                if record_adoption:
                    if not isinstance(layout, RecordedWorktreeOutput):
                        raise ValueError(f"{path}: adoption requires a worktree output assignment")
                    output = checked_output(layout)
                    base = checkout_base(path, "HEAD")
                    ensure_target_link(path, output)
                    record = new_record(path, branch.name, repo, base, owner, merge_branch)
                    write_record(replace(record, layout=layout))
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
    if carry_envrc_permission:
        _allow_copied_envrc(source, path)
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


def _allow_copied_envrc(source: Path, checkout: Path) -> None:
    """Allow the new checkout's .envrc when the source has the same allowed bytes."""
    new_rc = checkout / ".envrc"
    try:
        if not (new_rc.is_symlink() or new_rc.is_file()) or (direnv := shutil.which("direnv")) is None:
            return
        direnv = os.path.realpath(direnv)
        source_rc = source / ".envrc"
        if (
            not new_rc.is_symlink() and not source_rc.is_symlink() and source_rc.is_file()
            and (contents := new_rc.read_bytes()) == source_rc.read_bytes()
        ):
            status = subprocess.run([direnv, "status", "--json"], cwd=source,
                                    capture_output=True, check=True, timeout=5)
            match cast(object, json.loads(status.stdout)):
                case {"state": {"foundRC": {"path": str(found), "allowed": int(allowed)}}} if (
                    found == str(source_rc) and type(allowed) is int and allowed == 0
                    and not source_rc.is_symlink() and not new_rc.is_symlink()
                    and source_rc.read_bytes() == contents == new_rc.read_bytes()
                ):
                    _ = subprocess.run([direnv, "allow", str(checkout)],
                                       capture_output=True, check=True, timeout=5)
                    try:
                        if not new_rc.is_symlink() and new_rc.read_bytes() == contents:
                            return
                    except OSError:
                        pass
                    # A missing .envrc makes direnv search parent folders.
                    if new_rc.is_file():
                        _ = subprocess.run([direnv, "deny", str(checkout)],
                                           capture_output=True, check=True, timeout=5)
                case _:
                    pass
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    print(f"make: .envrc not allowed. Run direnv allow {shlex.quote(str(checkout))}", file=sys.stderr)


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
                           fixed_base=isinstance(snapshot, FrozenWorktreeSource), use_recorded_branch=True,
                           carry_envrc_permission=True)


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
    output = checked_output(layout) if isinstance(layout, RecordedWorktreeOutput) else UnassignedOutput()

    def report_output() -> None:
        if isinstance(output, Path):
            report_retained_folder(output, reason)

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


def unsaved_work(path: Path) -> Kept | Clean:
    """Keep a checkout with uncommitted changes or with commits that are on no remote."""
    dirty = dirty_check(path)
    if isinstance(dirty, Clean) and git(path, "rev-list", "-n1", "HEAD", "--not", "--remotes").stdout.strip():
        return Kept(path, "commits that are on no remote")
    return dirty


def remove_named(unit: str, short_name: str) -> list[str]:
    """Remove each recorded worktree of `unit` called `short_name`, and keep its branch.

    A worktree with unsaved work stays, and its line names the command to run after the work is saved.
    A worktree that an error stops stays, and its line names the first line of the error.
    """
    lines: list[str] = []
    for record_path in sorted(state_folder().glob("*.json")):
        try:
            record = read_record(record_path)
        except (OSError, ValueError):
            continue
        if not isinstance(record.layout, RecordedWorktreeOutput) or (
                record.layout.unit, record.layout.short_name) != (unit, short_name):
            continue
        path, repo = Path(record.path), Path(record.repo)
        try:
            state = unsaved_work(path) if path.exists() else Clean()
            if isinstance(state, Clean):
                remove_git_checkout(path, repo)
                continue
        except (Refused, OSError, ValueError) as error:
            first_line = str(error).partition("\n")[0]
            lines.append(f"{path} not removed: {first_line}")
            continue
        command = shlex.join(["env", f"--chdir={repo}", str(Path.home() / ".claude/scripts/lib/py"),
                              str(Path.home() / ".claude/scripts/worktree/worktrees.py"), "remove", str(path)])
        lines.append(f"{path} kept: {state.reason}. Save the work, then run: {command}")
    return lines


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


class MigrationInUse(Exception):
    """The migration must keep a source until its use ends."""


class MigrationPlanChanged(Exception):
    """The source no longer matches the migration plan."""


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


class WorktreeMoveLockHeld(Refused):
    def __init__(self, lock: Path) -> None:
        super().__init__(1, "another worktree move is in progress")
        self.lock: Path = lock


@contextmanager
def move_lock(*, create: bool = True) -> Generator[None]:
    path = move_folder() / "move.lock"
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = path.open("a" if create else "r")
    except FileNotFoundError:
        if create:
            raise
        yield
        return
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorktreeMoveLockHeld(path) from error
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


def moved_launch_folder(folder: Path) -> Path:
    """Use the verified destination for a saved folder inside a moved checkout."""
    if not folder.is_absolute() or ".." in folder.parts:
        return folder
    for former in (folder, *folder.parents):
        try:
            journal = _existing_move(former)
            if journal is None:
                continue
            current = journal.move.new
            if (journal.stage in {MoveStage.LINKED, MoveStage.FINISHED}
                    and current.is_absolute() and journal.destination.matches(current)
                    and (not os.path.lexists(former) or (former.is_symlink() and former.resolve() == current.resolve()))):
                return current / folder.relative_to(former)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError):
            return folder
        break
    return folder


def _unfinished_move_at(path: Path) -> bool:
    for file in move_folder().glob("*.json"):
        journal = read_move(file)
        if journal.stage is not MoveStage.FINISHED and journal.move.new == path:
            return True
    return False


def _move_destination(repo: str, unit: str, short_name: str) -> Path:
    destination = build_folders.worktree_path(repo, unit, short_name)
    for parent in (destination.parent, destination.parent.parent):
        if parent.is_symlink():
            raise ValueError(f"{parent}: a worktree parent must be a real folder")
    return destination.parent.resolve() / destination.name


_LEGACY_SCRATCH = Path("claude/scratch")


def _scratch_root() -> Path:
    return build_folders.build_root().resolve() / _LEGACY_SCRATCH


def _legacy_claude_present() -> bool:
    return _scratch_root().exists()


def _git_move_options(checkout: git_files.Checkout) -> tuple[str, ...]:
    if checkout.git_dir == checkout.common_dir:
        return ()
    return _linked_move_options(checkout.git_dir)


def _linked_move_options(entry: Path) -> tuple[str, ...]:
    lock = entry / "locked"
    try:
        reason = lock.read_text().strip()
    except FileNotFoundError:
        return ()
    if not reason.startswith("staff:"):
        raise ValueError(f"kept: {lock} held")
    return ("--force", "--force")


def _worktree_copy_required(source: Path, destination: Path) -> bool:
    """Check the mounts of the source and the nearest existing destination parent."""
    from scripts.lib.session_move import mount_identity

    ancestor = next(folder for folder in destination.parents if folder.is_dir())
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as directories:
        old = os.open(source, flags)
        _ = directories.callback(os.close, old)
        new = os.open(ancestor, flags)
        _ = directories.callback(os.close, new)
        return os.fstat(old).st_dev != os.fstat(new).st_dev or mount_identity(old) != mount_identity(new)


def _open_move(move: WorktreeMove, *, clone_members: tuple[Path, ...] = ()) -> MoveJournal:
    if move.new == move.old or move.new.is_relative_to(move.old) or move.old.is_relative_to(move.new):
        raise ValueError(f"{move.old}: invalid move destination {move.new}")
    if os.path.lexists(move.new):
        raise FileExistsError(f"{move.new}: worktree destination already exists")
    before = FolderIdentity.read(move.old)
    entry = git_files.at(move.old)
    _ = _git_move_options(entry)
    copy_tag = uuid4().hex if _worktree_copy_required(move.old, move.new) else ""
    journal = MoveJournal(move, before, before, copy_tag,
                          entry.git_dir if entry.git_dir != entry.common_dir else Path(".git"), clone_members)
    write_move(journal)
    return journal


type RenameGuard = Callable[[Path, MoveJournal], AbstractContextManager[Callable[[], None]]]


@contextmanager
def _unguarded_rename(_path: Path, _journal: MoveJournal) -> Generator[Callable[[], None]]:
    yield lambda: None


def _source_signature(source: Path) -> dict[Path, tuple[int, int, int]]:
    """Read the size and change times of each entry without following links."""
    def unreadable(error: OSError) -> None:
        raise error

    paths = (Path(top) / name for top, folders, files in os.walk(source, onerror=unreadable)
             for name in (*folders, *files))
    signature: dict[Path, tuple[int, int, int]] = {}
    try:
        for path in chain((source,), paths):
            status = path.lstat()
            signature[path] = (status.st_size, status.st_mtime_ns, status.st_ctime_ns)
    except OSError as error:
        raise MigrationInUse(f"source signature unavailable: {error}") from error
    return signature


def _discard_changed_stage(journal: MoveJournal) -> None:
    stage = journal.staging
    try:
        if not journal.destination.matches(stage):
            raise ValueError("the stage identity changed")
        shutil.rmtree(stage)
    except (OSError, ValueError):
        report_retained_folder(stage, "cannot discard the stage after the source changed")


def _is_real_target(target: Path) -> bool:
    return not target.is_symlink() and target.is_dir()


def _tracked_target_paths(checkout: Path) -> frozenset[Path]:
    result = subprocess.run(["git", "-C", str(checkout), "ls-files", "-z", "--", "target"],
                            capture_output=True, check=True)
    return frozenset(Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name)


def _copy_worktree_source(source: Path, stage: Path) -> None:
    """Copy the checkout and leave untracked Cargo artifacts at the source."""
    if not _is_real_target(source / "target"):
        if sys.platform == "linux":
            _ = subprocess.run(["cp", "-a", "--reflink=auto", "--", str(source), str(stage)], check=True)
        else:
            _ = shutil.copytree(source, stage, symlinks=True)
        return

    tracked = _tracked_target_paths(source)
    entries = tracked.union(parent for path in tracked for parent in path.parents if parent != Path("."))
    if sys.platform == "linux":
        def include_pattern(entry: Path) -> bytes:
            name = entry.as_posix()
            if any(character in name for character in "*?["):
                name = re.sub(r"([\\*?\[])", r"\\\1", name)
            return b"/" + os.fsencode(name) + b"\0"

        includes = b"".join(map(include_pattern, entries))
        _ = subprocess.run(["rsync", "-aHAXS", "--from0", "--include-from=-", "--exclude=/target/***", "--",
                            f"{source}/", str(stage)], input=includes, check=True)
        return

    def ignore(folder: str, names: list[str]) -> set[str]:
        relative = Path(folder).relative_to(source)
        return {name for name in names if (entry := relative / name).is_relative_to("target") and entry not in entries}

    _ = shutil.copytree(source, stage, symlinks=True, ignore=ignore)


def _rename_copy_directory(source: Path, destination: Path) -> None:
    from scripts.lib.session_move import open_directory, rename_exclusive

    with open_directory(destination.parent) as parent:
        try:
            rename_exclusive(parent, source.name, parent, destination.name)
        except FileExistsError:
            raise FileExistsError(f"{destination}: worktree destination already exists") from None


def _copy_across_filesystems(journal: MoveJournal, protect: RenameGuard) -> MoveJournal:
    old, new = journal.move.old, journal.move.new
    stage = journal.staging
    published = os.path.lexists(new)
    source_present = journal.source.matches(old)
    if not source_present and not journal.source.matches(journal.backup):
        raise ValueError(f"{old}: source identity changed during the move")
    if published and not journal.destination.matches(new):
        raise FileExistsError(f"{new}: worktree destination already exists")
    if source_present:
        entry = git_files.at(old)
        _ = _git_move_options(entry)
        if entry.git_dir != (old / ".git" if journal.git_entry == Path(".git") else journal.git_entry):
            raise ValueError(f"{old}: the Git worktree entry changed")
        if published:
            # An older journal can publish the copy before renaming the source.
            if os.path.lexists(stage):
                raise ValueError(f"{new}: cannot verify the prior move destination")
            with protect(new, journal) as final_check:
                final_check()
                if not journal.destination.matches(new):
                    raise ValueError(f"{new}: cannot verify the prior move destination")
                _rename_copy_directory(new, stage)
            published = False
        if os.path.lexists(stage):
            if journal.destination == journal.source or not journal.destination.matches(stage):
                reason = "a prior move left an unverified stage"
            else:
                reason = "the prior stage can differ from the current source"
            report_retained_folder(stage, reason)
            journal = replace(journal, destination=journal.source, copy_tag=uuid4().hex)
            write_move(journal)
            stage = journal.staging
        with protect(old, journal) as final_check:
            final_check()
            original = _source_signature(old)
            _copy_worktree_source(old, stage)
            journal = replace(journal, destination=FolderIdentity.read(stage))
            write_move(journal)
            if os.path.lexists(journal.backup):
                raise FileExistsError(f"{journal.backup}: move backup already exists")
            try:
                final_check()
            finally:
                try:
                    unchanged = _source_signature(old) == original
                except MigrationInUse:
                    unchanged = False
                if not unchanged:
                    _discard_changed_stage(journal)
                    raise MigrationInUse("changed during copy") from None
            if not journal.destination.matches(stage):
                raise MigrationInUse("cannot verify the staged copy")
            _check_planned_clone_members(journal)
            if os.path.lexists(new):
                raise FileExistsError(f"{new}: worktree destination already exists")
            _ = old.rename(journal.backup)
    prepared = new if published else stage
    if not journal.destination.matches(prepared):
        raise ValueError(f"{prepared}: prepared worktree identity changed")
    if not published:
        _rename_copy_directory(stage, new)
    return journal


def _relocated_clone_member(move: WorktreeMove, member: Path) -> Path:
    if member.is_relative_to(move.old):
        return move.new / member.relative_to(move.old)
    return member


def _publish_move_source(journal: MoveJournal, protect: RenameGuard) -> tuple[MoveJournal, WorktreeRecord]:
    old, new = journal.move.old, journal.move.new
    new.parent.mkdir(parents=True, exist_ok=True)
    destination_exists = os.path.lexists(new)
    if not journal.copy_tag and not destination_exists:
        if not journal.source.matches(old):
            raise ValueError(f"{old}: source identity changed during the move")
        if _worktree_copy_required(old, new):
            journal = replace(journal, copy_tag=uuid4().hex)
            write_move(journal)
    with ExitStack() as relocation:
        if journal.copy_tag:
            journal = _copy_across_filesystems(journal, protect)
        elif not destination_exists:
            if journal.git_entry == Path(".git"):
                final_check = relocation.enter_context(protect(old, journal))
                final_check()
                _check_planned_clone_members(journal)
                _ = old.rename(new)
            else:
                entry = git_files.at(old)
                if entry.git_dir != journal.git_entry:
                    raise ValueError(f"{old}: the Git worktree entry changed")
                main = main_checkout(old)
                if any(tree.path == new for tree in linked_trees(main)):
                    raise FileExistsError(f"{new}: Git already registers the move destination")
                options = _git_move_options(entry)
                final_check = relocation.enter_context(protect(old, journal))
                final_check()
                _ = git(main, "worktree", "move", *options, str(old), str(new))
        if not journal.destination.matches(new):
            raise ValueError(f"{new}: destination identity changed during the move")
        members = tuple(_relocated_clone_member(journal.move, member) for member in journal.clone_members)
        if journal.copy_tag and journal.git_entry != Path(".git"):
            # Start from the saved Git entry because the copied pointer can be relative.
            _ = git(journal.git_entry, "worktree", "repair", str(new))
        else:
            _ = git(new, "worktree", "repair", *map(str, members))
        checkout = git_files.at(new)
        if checkout.git_dir != (new / ".git" if journal.git_entry == Path(".git") else journal.git_entry):
            raise ValueError(f"{new}: the move changed the Git worktree entry")
        for member in members:
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
    record = _relocate_worktree_records(journal)
    journal = replace(journal, stage=MoveStage.LINKED)
    write_move(journal)
    return journal, record


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


def _relocate_worktree_records(journal: MoveJournal) -> WorktreeRecord:
    move = journal.move
    repo = main_checkout(move.new)
    at_destination = checkout_record(move.new)
    previous = checkout_record(move.old)
    if previous is not None:
        record = replace(previous, path=str(move.new), repo=str(repo), layout=UnassignedOutput())
    elif at_destination is not None:
        record = replace(at_destination, repo=str(repo))
    else:
        branch = git_files.head(git_files.at(move.new))
        head = branch.name if isinstance(branch, git_files.OnBranch) else ""
        record = new_record(move.new, head, repo, git(move.new, "rev-parse", "HEAD").stdout.strip(),
                            _update_worktree_staff(move.old, move.new), "")
    if record != at_destination:
        write_record(record)
    for member in journal.clone_members:
        relocated = _relocated_clone_member(move, member)
        recorded = checkout_record(member) or checkout_record(relocated)
        if recorded is None:
            continue
        write_record(replace(recorded, repo=str(repo), path=str(relocated)))
        if member != relocated:
            record_file(member).unlink(missing_ok=True)
    record_file(move.old).unlink(missing_ok=True)
    return record


def _finish_worktree_move(journal: MoveJournal, *, protect: RenameGuard) -> MoveJournal:
    if journal.stage is MoveStage.FINISHED:
        return journal
    move = journal.move
    if journal.stage is MoveStage.PLANNED:
        journal, record = _publish_move_source(journal, protect)
    elif journal.destination.matches(move.new):
        # An older journal can record LINKED before it moves the checkout record.
        record = _relocate_worktree_records(journal)
    else:
        raise ValueError(f"{move.new}: destination identity changed during the move")
    retained_copy = bool(journal.copy_tag) and journal.source.matches(journal.backup)
    if retained_copy:
        report_retained_folder(journal.backup, "the worktree moved to a new filesystem")
    if move.output is MoveOutput.LIVE_UNIT and move.repo == "claude" and _legacy_claude_present():
        print(f"defer output {move.new}: archive the old build root first")
        return journal

    backup_target = journal.backup / "target"
    target = move.new / "target"
    has_backup_target = retained_copy and _is_real_target(backup_target)
    tracked_target = bool(_tracked_target_paths(move.new))
    layout: RecordedWorktreeOutput | UnassignedOutput = UnassignedOutput()
    if tracked_target:
        if has_backup_target:
            report_retained_folder(backup_target, _EARLIER_CARGO_TARGET)
        if os.path.lexists(target):
            report_retained_folder(target, "Git tracks the checkout's target")
    elif move.output is MoveOutput.LIVE_UNIT:
        layout = output_record(move.repo, move.unit, move.short_name)
        output = checked_output(layout)
        if has_backup_target and not os.path.lexists(target):
            _link_with_previous_target(move.new, output, backup_target)
        else:
            ensure_target_link(move.new, output)
    else:
        if has_backup_target:
            report_retained_folder(backup_target, _EARLIER_CARGO_TARGET)
        if target.is_symlink():
            git_exclude.require_untracked(move.new, "target")
            target.unlink()
    if record.layout != layout:
        write_record(replace(record, layout=layout))
    journal = replace(journal, stage=MoveStage.FINISHED)
    write_move(journal)
    return journal


class MoveRepository(Enum):
    FROM_ORIGIN = "read the checkout's repository word"


def move_worktree(old: Path, repo: str | MoveRepository, unit: str, short_name: str) -> Path:
    """Move a linked checkout and resume any interrupted publication."""
    old = _move_source(old)
    if _is_numbered_build_copy(old, build_folders.build_root().resolve()):
        raise ValueError(f"{old}: cannot move numbered build copy")
    with move_lock():
        journal = _existing_move(old)
        if journal is None:
            checkout = git_files.at(old)
            if checkout.git_dir == checkout.common_dir:
                raise ValueError(f"{old}: expected a linked Git worktree")
            origin_repo = build_folders.repo_word(main_checkout(old))
            if isinstance(repo, str) and origin_repo != repo:
                raise ValueError(f"{old}: the repo word differs from origin")
            new = _move_destination(origin_repo, unit, short_name)
            journal = _open_move(WorktreeMove(old, new, origin_repo, unit, short_name, MoveOutput.LIVE_UNIT))
        else:
            request = journal.move
            repo = request.repo if isinstance(repo, MoveRepository) else repo
            new = _move_destination(repo, unit, short_name)
            if (request.new, request.repo, request.unit, request.short_name) != (new, repo, unit, short_name):
                raise ValueError(f"{old}: this request differs from the saved move")
            if journal.stage is MoveStage.FINISHED and not journal.destination.matches(new):
                raise ValueError(f"{new}: the recorded destination has changed")
        try:
            _ = _finish_worktree_move(journal, protect=_direct_move_guard)
        except (MigrationInUse, MigrationPlanChanged) as error:
            raise Refused(1, f"{old}: {error}") from error
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
class StaffUnitName:
    name: str


@dataclass(frozen=True)
class NoStaffUnitOwner:
    """Staff and the worktree record name no owning unit."""


def _staff_unit_owner(snapshot: staff.StaffSnapshot, machine: str, path: Path,
                      record: WorktreeRecord | None) -> StaffUnitName | NoStaffUnitOwner:
    row = snapshot.unit_by_worktree(machine, path)
    if row is not None:
        return StaffUnitName(row.name)
    if record is not None and record.owner:
        owner = snapshot.by_member_reference(record.owner)
        if owner is None:
            owner = next((row for row in snapshot.rows if row.kind == "unit" and row.name == record.owner), None)
        elif owner.kind != "unit":
            owner = snapshot.parent_of(owner)
        if owner is not None and owner.kind == "unit":
            return StaffUnitName(owner.name)
    return NoStaffUnitOwner()


@dataclass(frozen=True)
class UnrecognizedUnitName:
    entered: str


def _resolve_migration_unit(entered: str, staff_names: frozenset[str], *,
                            prefixes: frozenset[str]) -> StaffUnitName | UnrecognizedUnitName:
    if entered in staff_names:
        return StaffUnitName(entered)
    words = sorted(filter(None, prefixes), key=lambda name: (-len(name), name))
    name = entered
    while name not in staff_names:
        for word in words:
            match = re.match(rf"{re.escape(word)}[-/]", name, re.IGNORECASE)
            if match is not None:
                name = name[match.end():]
                break
        else:
            return UnrecognizedUnitName(entered)
    return StaffUnitName(name)


@dataclass(frozen=True)
class ListedWorktree:
    unit: StaffUnitName
    short_name: str


class MigrationScope(Enum):
    LISTED = "listed worktrees"
    ALL = "all worktrees"
    REPOSITORY = "one repository"


@dataclass(frozen=True)
class MigrationSelection:
    scope: MigrationScope
    listed: dict[Path, ListedWorktree]
    repo: str = ""
    members: frozenset[Path] = frozenset()

    def includes(self, path: Path) -> bool:
        match self.scope:
            case MigrationScope.ALL:
                return True
            case MigrationScope.LISTED:
                return path in self.listed
            case MigrationScope.REPOSITORY:
                return path in self.members

    def plans_main(self, repo: str) -> bool:
        return self.scope is MigrationScope.ALL or (self.scope is MigrationScope.REPOSITORY and repo == self.repo)

    def includes_journal(self, journal: MoveJournal) -> bool:
        return any(self.includes(path) for path in (journal.move.old, journal.move.new, journal.source_key))


def repository_selection(repo: str, listed: dict[Path, ListedWorktree],
                         folders: MigrationFolders) -> MigrationSelection:
    """Select the main checkout of one repository, its worktrees, its scratch clones, and its saved moves."""
    main = main_checkout_for_word(repo)
    members = {main, *(tree.path for tree in linked_trees(main))}
    for clone in _scratch_checkouts(folders.scratch):
        try:
            if _repository_names(clone, follow_local_origin=True).word == repo:
                members.update((clone, *(tree.path for tree in linked_trees(clone))))
        except (build_folders.InvalidFolderForm, Refused, git_files.GitFilesError, OSError, UnicodeError):
            continue
    for path in sorted(move_folder().glob("*.json")):
        journal = read_move(path)
        if journal.move.repo == repo:
            members.update((journal.move.old, journal.move.new, journal.source_key))
    return MigrationSelection(MigrationScope.REPOSITORY, listed, repo, frozenset(members))


def listed_worktrees(path: Path, folders: MigrationFolders, snapshot: staff.StaffSnapshot) -> dict[Path, ListedWorktree]:
    from scripts.agents.remote import local_machine

    machine = local_machine()
    staff_names = frozenset(row.name for row in snapshot.rows if row.kind == "unit")
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
        if _is_numbered_build_copy(old, folders.build_root):
            raise ValueError(f"{path}:{line_number}: cannot migrate numbered build copy {old}")
        saved = checkout_record(old)
        if saved is None and old.is_symlink():
            saved = checkout_record(old.resolve())
        owner = _staff_unit_owner(snapshot, machine, old, saved)
        unit = _resolve_migration_unit(fields[1], staff_names, prefixes=frozenset())
        if isinstance(unit, UnrecognizedUnitName):
            suggested = owner.name if isinstance(owner, StaffUnitName) else max(
                (name for name in staff_names if unit.entered.endswith((f"-{name}", f"/{name}"))),
                key=len, default="")
            action = f"use {suggested}" if suggested else "use the exact unit name from staff"
            raise ValueError(f"{path}:{line_number}: {unit.entered} is not a staff unit; {action}")
        if isinstance(owner, StaffUnitName) and unit != owner:
            raise ValueError(f"{path}:{line_number}: {old} belongs to staff unit {owner.name}; use {owner.name}")
        listed[old] = ListedWorktree(unit, fields[2])
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
class RepositoryNames:
    word: str
    origin: str


@dataclass(frozen=True)
class MigrationShortNames:
    machines: frozenset[str]
    productions: frozenset[str]
    checkouts: dict[str, set[str]]

    def unit_prefixes(self, repo: RepositoryNames) -> frozenset[str]:
        return self.machines | self.productions | {repo.word, repo.origin}

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


def _migration_maker_roots(scratch: Path, settings: conf.ConfigRead) -> tuple[Path, ...]:
    # The base setting names the former scratch root, even when the host uses the build drive.
    former_scratch = copy_record.FORMER_ROOT
    values = settings.settings
    if isinstance(values, dict) and (setting := values.get("roots.scratch")) is not None and setting.value:
        former_scratch = Path(setting.value).expanduser()
    roots = (Path("/tmp/claude/unit"), Path("/tmp/claude/delegate"), scratch.parents[1] / "slot",
             session_folder.configured_root().resolve(), Path.home() / ".local")
    former_scratch = former_scratch.resolve()
    return roots if former_scratch == scratch else (*roots, former_scratch)


@dataclass(frozen=True)
class MigrationFolders:
    build_root: Path
    maker_roots: tuple[Path, ...]

    @property
    def scratch(self) -> Path:
        return self.build_root / _LEGACY_SCRATCH

    @classmethod
    def configured(cls) -> MigrationFolders:
        settings = conf.read_or_repository("build_folders")
        root = build_folders.build_root(settings=settings).resolve()
        return cls(root, _migration_maker_roots(root / _LEGACY_SCRATCH, settings))


def _worktree_build_form(path: Path, build_root: Path) -> build_folders.ParsedFolder:
    # Numbered names in legacy scratch can name worktrees.
    if path.is_relative_to(build_root / _LEGACY_SCRATCH):
        return build_folders.OutsideFolderLayout()
    return build_folders.parse_resolved_form(path, build_root)


def _is_numbered_build_copy(path: Path, build_root: Path) -> bool:
    return (isinstance(_worktree_build_form(path, build_root), build_folders.BuildCopy)
            or isinstance(_worktree_build_form(path.resolve(), build_root), build_folders.BuildCopy))


_NUMBERED_BUILD_COPY = "a numbered build copy; the build folder model owns it"


def _migration_build_keep(path: Path, folders: MigrationFolders) -> str:
    try:
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            resolved = path.resolve()
    except (OSError, RuntimeError) as error:
        return f"cannot verify the checkout path ({error})"
    if folders.build_root.is_relative_to(resolved) or resolved.parent == folders.build_root:
        return "contains the build root or a build repository folder"
    form = _worktree_build_form(resolved, folders.build_root)
    if isinstance(form, build_folders.BuildCopy):
        return _NUMBERED_BUILD_COPY
    if isinstance(form, build_folders.CiOutput):
        return "a CI runner folder; the build folder model owns it"
    if isinstance(form, build_folders.MainOutput):
        return "a main output; the build folder model owns it"
    if resolved != folders.scratch and resolved.parent.parent == folders.build_root:
        try:
            _ = build_folders.unit_folder(resolved.parent.name, resolved.name, root=folders.build_root)
        except build_folders.InvalidFolderForm:
            pass
        else:
            return "a build unit folder; the build folder model owns it"
    return ""


def _migration_path_keep(path: Path, folders: MigrationFolders) -> str:
    if reason := _migration_build_keep(path, folders):
        return reason
    scratch_root = folders.scratch
    home = Path.home()
    if any(parts == (".claude", "worktrees") for parts in zip(path.parts, path.parts[1:])):
        return "Claude Code owns this worktree"
    if path == home / "worktrees/nate_style-message-clarity":
        return "the user's worktree"
    if path == scratch_root / "sessionmove-bbf204":
        return "queue moves the session-folder checkout before the switch"
    if (any(path.is_relative_to(root) for root in folders.maker_roots)
            or (path.is_relative_to(scratch_root) and path != scratch_root
                and path.relative_to(scratch_root).parts[0].startswith("gate-"))
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
    # Moved checkouts whose target is a real folder or missing, so it still needs the link to the recorded output.
    moved: tuple[Path, ...] = ()
    # Other moved checkouts that have a folder with an earlier target.
    saved: tuple[Path, ...] = ()


@dataclass(frozen=True)
class MigrationJournalView:
    relocated: tuple[MoveJournal, ...]
    kept: dict[Path, str]


class MigrationLocationUnverified(ValueError):
    """The member has no verified current checkout path."""


@dataclass
class MigrationLocations:
    """Resolve the current directory behind a logical member path."""

    journals_by_destination: dict[Path, MoveJournal]

    def record_publication(self, journal: MoveJournal) -> None:
        self.journals_by_destination[journal.move.new] = journal

    def current(self, member: Path) -> Path:
        journal = self.journals_by_destination.get(member)
        if journal is None:
            status = member.lstat()
            if stat.S_ISLNK(status.st_mode):
                raise MigrationLocationUnverified("Git names a symbolic link")
            if not stat.S_ISDIR(status.st_mode):
                raise MigrationLocationUnverified("Git names a path that is not a directory")
            return member.resolve(strict=True)

        checkout = journal.move.new
        try:
            if (journal.stage is MoveStage.PLANNED and journal.copy_tag
                    and not os.path.lexists(checkout) and journal.source.matches(journal.backup)):
                checkout = journal.staging
            if journal.destination.matches(checkout):
                return checkout.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise MigrationLocationUnverified(f"cannot verify the saved checkout at {checkout} ({error})") from error
        raise MigrationLocationUnverified(f"cannot verify the saved checkout at {checkout}")


def _migration_linked_trees(repo: Path, repairs: dict[Path, MoveJournal]) -> tuple[RegisteredTree, ...]:
    trees = linked_trees(repo)
    if not repairs:
        return trees
    entries = git_files.at(repo).common_dir / "worktrees"
    paths = {old: journal.move.new for old, journal in repairs.items() if journal.git_entry.parent == entries}
    return tuple(replace(tree, path=paths[tree.path]) if tree.path in paths else tree for tree in trees)


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
                          machine: str, relocated_repos: dict[Path, str]) -> dict[staff.StaffId, str]:
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
        if relocated_repo := relocated_repos.get(location.path):
            confirmed[row.id] = relocated_repo
            continue
        try:
            checkout = git_files.at(location.path)
            repo_word = by_git_dir.get(checkout.common_dir)
            confirmed[row.id] = repo_word if repo_word is not None else build_folders.repo_word(checkout.toplevel)
        except (git_files.GitFilesError, build_folders.InvalidFolderForm):
            continue
    return confirmed


def plan_worktree_migration(selection: MigrationSelection, snapshot: staff.StaffSnapshot,
                            folders: MigrationFolders, journals: MigrationJournalView,
                            locations: MigrationLocations) -> WorktreeMigration:
    """Read Git, staff, and the supplied list without changing them."""
    from scripts.agents.remote import local_machine
    from scripts.production import staff

    listed = selection.listed
    machine = local_machine()
    scratch_root = folders.scratch
    mains = _migration_main_checkouts(Path.home())
    repairs = {journal.move.old: journal for journal in journals.relocated
               if journal.stage is MoveStage.PLANNED and journal.git_entry != Path(".git")}
    repaired_entries = {journal.move.new: journal for journal in repairs.values()}
    clones: dict[Path, tuple[RegisteredTree, ...]] = {}
    skipped = {path: _MAIN_STAYS for path in mains} | journals.kept
    trees: list[RegisteredTree] = []
    for path in _scratch_checkouts(scratch_root):
        reason = _migration_path_keep(path, folders)
        if reason:
            skipped[path] = reason
            continue
        try:
            if not (path / ".git").is_dir():
                continue
            entries = _migration_linked_trees(path, repairs)
            if (path.name == "crprof-8a8cbf" and not entries
                    and git(path, "config", "--get", "remote.origin.url", check=False).returncode == 1):
                skipped[path] = _ARCHIVED_SCRATCH
                continue
            clones[path] = entries
            branch = git_files.head(git_files.at(path))
        except (Refused, git_files.GitFilesError, OSError, UnicodeError) as error:
            skipped[path] = journals.kept.get(path, f"cannot read the clone's Git metadata ({error})")
            continue
        trees.append(RegisteredTree(path, path, branch.name if isinstance(branch, git_files.OnBranch) else ""))
    for main in mains:
        trees.extend(_migration_linked_trees(main, repairs))
    for entries in clones.values():
        trees.extend(entries)

    repos: dict[Path, RepositoryNames] = {}
    main_plans: list[MainOutputPlan] = []
    for repo in (*mains, *clones):
        try:
            names = _repository_names(repo, follow_local_origin=repo in clones)
        except (build_folders.InvalidFolderForm, Refused, git_files.GitFilesError, OSError, UnicodeError) as error:
            if repo in mains:
                if not isinstance(error, (build_folders.InvalidFolderForm, Refused)):
                    raise
                skipped[repo] = str(error)
            else:
                _ = skipped.setdefault(repo, f"cannot read the clone's origin ({error})")
            continue
        repos[repo] = names
        if repo in mains:
            primary = _main_checkout_path(names.word)
            if not primary.is_symlink() and repo == primary.resolve():
                if selection.plans_main(names.word):
                    main_plans.append(MainOutputPlan(repo, names.word))
            else:
                skipped[repo] = f"keeps its current output; {primary} is the main checkout for {names.word}"

    for path in _scratch_checkouts(scratch_root):
        if path not in skipped and all(tree.path != path for tree in trees):
            skipped[path] = _UNVERIFIED_SCRATCH

    short_names, showrunners = _migration_names(trees, repos, mains, snapshot, machine)
    # Keep the known merge checkout if its staff document is unavailable.
    _ = showrunners.setdefault(Path.home() / "rust/hana_catalyst",
                               "showrunner checkout awaits question 4 (Hana merge checkout)")
    missing: list[RegisteredTree] = []
    validated: list[RegisteredTree] = []
    for tree in trees:
        path = tree.path
        if not selection.includes(path) and (tree.store not in clones or not selection.includes(tree.store)):
            continue
        if reason := showrunners.get(path):
            skipped[path] = reason
            continue
        try:
            physical = locations.current(path)
        except FileNotFoundError:
            if reason := _migration_path_keep(path, folders):
                skipped[path] = reason
            else:
                missing.append(tree)
            continue
        except MigrationLocationUnverified as error:
            skipped[path] = str(error)
            continue
        except (OSError, RuntimeError) as error:
            skipped[path] = f"cannot read the checkout path ({error})"
            continue
        if reason := (
            _migration_path_keep(path, folders)
            or _migration_path_keep(physical, folders)
        ):
            skipped[path] = reason
        elif tree.store not in repos:
            _ = skipped.setdefault(path, "the repository has no usable origin name")
        else:
            repair = repaired_entries.get(path)
            clone_member = tree.store in clones and path != tree.store
            try:
                if repair is None or clone_member:
                    checkout = git_files.at(physical)
                    if clone_member:
                        _check_clone_member_store(checkout, git_files.at(tree.store).common_dir)
                    if repair is None:
                        _ = _git_move_options(checkout)
                if repair is not None:
                    _ = _linked_move_options(repair.git_entry)
            except (OSError, ValueError, MigrationPlanChanged) as error:
                skipped[path] = str(error)
            else:
                validated.append(tree)

    placed_destinations = {journal.move.new for journal in journals.relocated
                           if journal.stage is not MoveStage.FINISHED}
    finished_destinations: dict[Path, list[FolderIdentity]] = {}
    for journal in journals.relocated:
        path = journal.move.new
        if journal.stage is MoveStage.FINISHED and path not in placed_destinations:
            finished_destinations.setdefault(path, []).append(journal.destination)
    registered_paths = {tree.path for tree in trees}
    for path in registered_paths & finished_destinations.keys():
        try:
            _ = path.lstat()
            if any(identity.matches(path) for identity in finished_destinations[path]):
                placed_destinations.add(path)
            else:
                skipped[path] = "the finished move destination has changed"
        except FileNotFoundError:
            continue
        except OSError as error:
            skipped[path] = f"cannot verify the finished move destination ({error})"
    skipped.update(journals.kept)
    stale = [tree for tree in missing if tree.path not in skipped]
    already_placed: set[Path] = set()
    candidates: list[RegisteredTree] = []
    for tree in validated:
        path = tree.path
        if path in skipped:
            continue
        if path in placed_destinations:
            already_placed.add(path)
            skipped[path] = _ALREADY_MOVED
        elif selection.includes(path):
            candidates.append(tree)

    units = [row for row in snapshot.rows if row.kind == "unit"]
    staff_names = frozenset(row.name for row in units)
    relocated_repos = {path: journal.move.repo for journal in journals.relocated
                       if journal.stage is not MoveStage.FINISHED
                       for path in (journal.move.old, journal.move.new, journal.source_key)}
    live_repos = _live_migration_repos(units, repos, machine, relocated_repos)
    live_names = {row.name for row in units if isinstance(row.worktree, staff.PresentWorktree)}
    live = {(live_repos[row.id], row.name) for row in units if row.id in live_repos}
    retired = {row.name for row in units if isinstance(row.worktree, staff.RemovedWorktree)}
    known_by_repo: dict[str, dict[str, str]] = {repo.word: {} for repo in repos.values()}
    for row in units:
        parent = snapshot.parent_of(row)
        for repo, known in known_by_repo.items():
            if isinstance(row.worktree, staff.PresentWorktree) and live_repos.get(row.id) != repo:
                continue
            known[row.name] = row.name
            if parent is not None:
                _ = known.setdefault(f"{parent.name}-{row.name}", row.name)
    for tree in candidates:
        supplied = listed.get(tree.path)
        if supplied is not None:
            known_by_repo[repos[tree.store].word][supplied.unit.name] = supplied.unit.name
    unit_words = {repo: sorted(known, key=lambda word: (-len(word), word))
                  for repo, known in known_by_repo.items()}
    moves: list[PlannedWorktree] = []
    destinations: dict[Path, Path] = {}
    collisions: set[Path] = set()
    for tree in candidates:
        path, repo = tree.path, repos[tree.store]
        supplied = listed.get(path)
        if supplied is not None:
            resolved, source = supplied.unit, "list"
        else:
            saved = checkout_record(path)
            owner = _staff_unit_owner(snapshot, machine, path, saved)
            word = next((name for name in unit_words[repo.word] if _without_name(path.name, name) != path.name), "")
            if isinstance(owner, StaffUnitName):
                name, source = owner.name, "staff row"
            elif word:
                name, source = known_by_repo[repo.word][word], "folder name"
            elif saved is not None and isinstance(saved.layout, RecordedWorktreeOutput):
                name, source = saved.layout.unit, "worktree record"
            else:
                name, source = "", ""
            prefixes = short_names.unit_prefixes(repo)
            resolved = _resolve_migration_unit(name, staff_names, prefixes=prefixes)
            if isinstance(resolved, UnrecognizedUnitName):
                try:
                    name = _subject_unit(path)
                except (Refused, git_files.GitFilesError, OSError, UnicodeError) as error:
                    if tree.store not in clones:
                        raise
                    skipped[path] = f"cannot read the Git commit subject ({error})"
                    continue
                resolved, source = _resolve_migration_unit(name, staff_names, prefixes=prefixes), "commit subject"
        if isinstance(resolved, UnrecognizedUnitName):
            skipped[path] = "unit unknown"
            continue
        unit = resolved.name
        if source == "commit subject" and (unit.casefold() in (repo.word.casefold(), repo.origin.casefold())
                                           or (unit in live_names and (repo.word, unit) not in live)):
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
            skipped[path] = _AT_DESTINATION
            continue
        if os.path.lexists(new):
            skipped[path] = f"short name occupied at {new}"
            continue
        prior = destinations.setdefault(new, path)
        if prior != path:
            collisions.add(new)
        live_output = (repo.word, unit) in live or (
            supplied is not None and unit not in retired and unit not in live_names)
        output = MoveOutput.LIVE_UNIT if live_output else MoveOutput.RETIRED_UNIT
        move = WorktreeMove(path, new, repo.word, unit, short_name, output)
        moves.append(PlannedWorktree(tree, move, source))

    selected = {plan.tree.path: plan for plan in moves if plan.move.new not in collisions}
    clone_members: dict[Path, tuple[Path, ...]] = {}
    for clone, members in clones.items():
        members = tuple(member for member in members if member not in stale)
        blocked = next((member.path for member in members
                        if member.path not in selected and member.path not in already_placed), None)
        if clone not in selected or blocked is not None:
            reason = f"{clone}: the clone and every linked worktree need move names"
            if blocked is not None and blocked in skipped:
                reason = f"{clone}: linked worktree {blocked} stays ({skipped[blocked]})"
            for path in (clone, *(member.path for member in members)):
                if path in selected:
                    del selected[path]
                    skipped[path] = reason
            continue
        clone_members[clone] = tuple(selected[member.path].move.new if member.path in selected else member.path
                                     for member in members)
    for plan in moves:
        if plan.move.new in collisions:
            skipped[plan.tree.path] = f"short name shared by multiple worktrees at {plan.move.new}"
    former = {path for journal in journals.relocated for path in (journal.move.old, journal.source_key)}
    for path in listed.keys() - registered_paths - skipped.keys() - former:
        skipped[path] = "no registered worktree at this path"
    ordered = tuple(sorted(selected.values(), key=lambda plan: (plan.tree.path in clones, plan.tree.path)))
    placed = sorted(path for path in already_placed if selection.includes(path))
    moved = tuple(path for path in placed
                  if _is_real_target(path / "target") or not os.path.lexists(path / "target"))
    return WorktreeMigration(ordered, tuple(tree for tree in stale if selection.includes(tree.path)),
                             tuple(main_plans), clone_members,
                             {path: reason for path, reason in skipped.items() if selection.includes(path)},
                             moved, tuple(path for path in placed if path not in moved and _saved_targets(path)))


_NO_PROBE_FDS: frozenset[int] = frozenset()


def _linux_checkout_spellings(paths: tuple[Path, ...]) -> dict[Path, tuple[str, ...]]:
    from scripts.lib.session_move import mount_identity, open_directory

    mounts = {mount.mount_id: mount for mount in build_folders.linux_mounts()}
    spellings: dict[Path, tuple[str, ...]] = {}
    for path in paths:
        with open_directory(path) as fd:
            mount = mounts[mount_identity(fd)]
        source = mount.root / path.relative_to(mount.point)
        aliases = {path}
        for other in mounts.values():
            if other.device != mount.device:
                continue
            if source.is_relative_to(other.root):
                aliases.add(other.point / source.relative_to(other.root))
            elif other.root.is_relative_to(source):
                # The bind mount exposes only part of the checkout.
                aliases.add(other.point)
        spellings[path] = tuple(map(str, sorted(aliases)))
    return spellings


def _migration_processes(paths: tuple[Path, ...], *, ignored_fds: frozenset[int] = _NO_PROBE_FDS
                         ) -> tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable:
    from scripts.lint import tmp_sweep

    if not paths:
        return tmp_sweep.ProcessUsesKnown([])
    if sys.platform == "linux":
        try:
            spellings = _linux_checkout_spellings(paths)
        except (OSError, ValueError, KeyError):
            return tmp_sweep.ProcessUsesIncomplete()
        prefixes = tuple(dict.fromkeys(chain.from_iterable(spellings.values())))
        uses = tmp_sweep.process_paths(tmp_sweep.PROC_DIR, prefixes, require_readable_user_processes=True,
                                      ignored_self_fds=ignored_fds)
    else:
        spellings = {path: (str(path),) for path in paths}
        uses = tmp_sweep.lsof_paths(tuple(map(str, paths)), require_complete=True, ignored_self_fds=ignored_fds,
                                   same_user_only=True)
    if isinstance(uses, tmp_sweep.ProcessUsesUnavailable):
        return uses
    paths_in_use: set[tuple[str, int, str]] = set()
    for name, pid, how in uses.paths:
        live_name = name.removesuffix(tmp_sweep.DELETED_SUFFIX)
        for path, aliases in spellings.items():
            if any(alias in (name, live_name) or name.startswith(alias + os.sep) for alias in aliases):
                paths_in_use.add((str(path), pid, how))
    return replace(uses, paths=sorted(paths_in_use))


def _stopped_pane_shell(proc: Path, pid: int, checkout: Path, scanned: tmp_sweep.CwdOnlyProcess) -> bool:
    """Check the stopped pane and confirm that its shell can stay in the checkout."""
    from scripts.herdr import herdr, herdr_types, pane_launch
    from scripts.lint import tmp_sweep
    from scripts.production import staff

    process = proc / str(pid)
    try:
        observed = tmp_sweep.linux_process_stat(process)
        if observed.state in {"Z", "X"} or observed.start_ticks != scanned.start_ticks:
            return False
        parent = observed.parent
        command = (proc / str(parent) / "cmdline").read_bytes().removesuffix(b"\0").split(b"\0")
        match [os.fsdecode(argument) for argument in command]:
            case [executable, "--session", server, "server"] if (
                Path(executable).name == "herdr" and re.fullmatch(r"[A-Za-z0-9_.-]+", server)
                and server not in {".", ".."}
            ):
                pass
            case _:
                return False
        marks = (process / "environ").read_bytes().split(b"\0")
        panes = [part.removeprefix(b"HERDR_PANE_ID=") for part in marks if part.startswith(b"HERDR_PANE_ID=")]
        if len(panes) != 1 or not panes[0]:
            return False
        pane = os.fsdecode(panes[0])
        directory = pane_launch.record_directory()
        # Read the atomic record without a publication lock that can write during a dry run.
        if os.path.lexists(directory / ".producer-publication"):
            return False
        entries = cast(object, json.loads((directory / f"{server}.json").read_text()))
        saved = cast(dict[str, object], entries).get(pane) if isinstance(entries, dict) else None
        if not isinstance(saved, dict):
            return False
        entry = cast(dict[str, object], saved)
        if entry.get("ended") is not True or entry.get("pane_id") != pane:
            return False
        owner = staff_record().by_member_reference(pane_launch.reference_id(entry))
        if owner is None or not isinstance(herdr.read_conversation(owner.current_session), herdr.StoppedConversation):
            return False
        info = herdr.Herdr(server).result("pane", "process-info", "--pane", pane).get("process_info")
        if not isinstance(info, dict):
            return False
        shell_pid = cast(dict[str, object], info).get("shell_pid")
        if type(shell_pid) is not int or shell_pid != pid:
            return False
        folders = _linux_checkout_spellings((checkout,))[checkout]
        for candidate in proc.iterdir():
            if not candidate.name.isdecimal():
                continue
            try:
                state = (candidate / "stat").read_text().rpartition(")")[2].split(maxsplit=2)
            except FileNotFoundError:
                if candidate.exists():
                    return False
                continue
            if int(state[1]) == pid:
                return False
        if not tmp_sweep.process_clear_beyond_cwd(process, pid, folders):
            return False
        if ((process / "comm").read_text().rstrip("\n") not in {"zsh", "bash", "sh"}
                or os.readlink(process / "exe") != scanned.executable):
            return False
        current = tmp_sweep.linux_process_stat(process)
        return (current.state not in {"Z", "X"} and current.parent == parent
                and current.start_ticks == scanned.start_ticks)
    except (OSError, ValueError, IndexError, KeyError, staff.StaffRefused, herdr_types.HerdrFailure,
            subprocess.SubprocessError):
        return False


def _migration_process_use(path: Path, uses: tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable, *,
                           ignore_stopped_pane_shells: bool) -> str:
    from scripts.lint import tmp_sweep

    if isinstance(uses, tmp_sweep.ProcessUsesUnavailable):
        return "process paths unavailable"
    if uses.unreadable_process_pids:
        return f"unreadable process {min(uses.unreadable_process_pids)}"
    by_pid: dict[int, set[str]] = {}
    for name, pid, how in uses.paths:
        if name == str(path):
            by_pid.setdefault(pid, set()).add(how)
    unchecked = set(uses.unreadable_cwd_pids) | {process.pid for process in uses.kernel_hidden}
    pids = [pid for pid, modes in by_pid.items()
            if (not ignore_stopped_pane_shells or sys.platform != "linux"
                or pid in unchecked or modes != {tmp_sweep.WORKS_IN}
                or pid not in uses.cwd_only_processes
                or not _stopped_pane_shell(Path(tmp_sweep.PROC_DIR), pid, path, uses.cwd_only_processes[pid]))]
    if pids:
        return "pids " + ", ".join(map(str, sorted(pids)))
    return ""


def _paths_overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def _migration_leases(folders: MigrationFolders, paths: tuple[Path, ...]) -> tuple[build_folders.HeldFolderLease, ...]:
    if not any(_paths_overlap(path, folders.build_root) for path in paths):
        return ()
    return tuple(lease for lease in build_folders.held_folder_leases()
                 if not lease.path.resolve().is_relative_to(folders.scratch))


def _migration_lease_use(path: Path, leases: tuple[build_folders.HeldFolderLease, ...]) -> str:
    for lease in leases:
        if _paths_overlap(path, lease.path.resolve()):
            if isinstance(lease, build_folders.KnownFolderLease):
                return f"kept: {lease.path.with_name(lease.path.name + '.lock')} held"
            return f"copy lease unverified at {lease.path}"
    return ""


def _check_clone_member_store(member: git_files.Checkout, common_dir: Path) -> None:
    if member.common_dir != common_dir:
        raise MigrationPlanChanged(f"linked worktree {member.toplevel} now uses a different Git store")


def _check_planned_clone_members(journal: MoveJournal) -> None:
    if journal.git_entry != Path(".git"):
        return
    current: set[Path] = set()
    try:
        for tree in linked_trees(journal.move.old):
            try:
                _ = tree.path.lstat()
            except FileNotFoundError:
                continue
            current.add(tree.path)
        if current != set(journal.clone_members):
            raise MigrationPlanChanged("linked checkouts changed since the plan")
        common_dir = git_files.at(journal.move.old).common_dir
        for member in sorted(current):
            _check_clone_member_store(git_files.at(member), common_dir)
    except (OSError, Refused, git_files.GitFilesError) as error:
        raise MigrationInUse(f"cannot verify linked worktrees: {error}") from error


def _take_migration_locks(source: Path, folders: MigrationFolders, locks: ExitStack,
                          marks: scratch.BusyMarkScope, leased: set[Path], fds: list[int], *, create: bool) -> None:
    covered = _paths_overlap(source, folders.build_root) and not source.is_relative_to(folders.scratch)
    copy = copy_record.numbered_copy_checkout(source) if covered else copy_record.OutsideNumberedCopy()
    lease_path = copy.resolve() if isinstance(copy, Path) else source
    if lease_path not in leased:
        leased.add(lease_path)
        lease = locks.enter_context(build_folders.folder_removal_lease(
            lease_path, children=covered and not isinstance(copy, Path), wait=False,
            create=create and isinstance(copy, Path), probe_fds=fds))
        if isinstance(lease, build_folders.RemovalLockHeld):
            raise MigrationInUse(f"kept: {lease.lock} held")
        if isinstance(lease, build_folders.RefusedRemovalLease):
            raise MigrationInUse(f"copy lease unavailable at {source}")
    try:
        idle = locks.enter_context(scratch.busy_locks(
            source, create=create, scope=marks, inherited_fds=fds, raise_if_held=True))
    except scratch.BusyMarkHeld as error:
        raise MigrationInUse(f"kept: {error.lock} held") from error
    if not idle:
        raise MigrationInUse(f"busy claim unavailable at {source}")


def _probe_migration_locks(source: Path, folders: MigrationFolders) -> str:
    try:
        with ExitStack() as locks:
            _take_migration_locks(source, folders, locks, scratch.BusyMarkScope(), set(), [], create=False)
    except MigrationInUse as error:
        return str(error)
    return ""


def _migration_lock_use(lock: Path) -> str:
    try:
        if file_lock.probe_existing_exclusive(lock) is file_lock.LockAvailability.HELD:
            return f"kept: {lock} held"
    except OSError as error:
        return f"cannot verify lock {lock} ({error})"
    return ""


def _migration_target_lock(checkout: Path, common_dir: Path, output: Path) -> str:
    if _target_matches(checkout / "target", output):
        return ""
    return _migration_lock_use(git_exclude.exclude_lock_path(common_dir))


@dataclass(frozen=True)
class MigrationPublication:
    staff_sources: frozenset[Path]
    legacy_claude: bool

    @classmethod
    def read(cls, snapshot: staff.StaffSnapshot, folders: MigrationFolders) -> MigrationPublication:
        from scripts.agents.remote import local_machine
        from scripts.production import staff

        machine = local_machine()
        sources = frozenset(_move_source(row.worktree.path) for row in snapshot.rows
                            if isinstance(row.worktree, (staff.PresentWorktree, staff.RemovedWorktree))
                            and row.worktree.machine == machine)
        return cls(sources, folders.scratch.exists())

    @cached_property
    def staff_lock_use(self) -> str:
        from scripts.production import staff

        status = staff.worktree_write_lock_status()
        if isinstance(status, staff.StaffWorktreeWriteLockHeld):
            return f"kept: {status.lock} held"
        return ""

    def lock_use(self, move: WorktreeMove, checkout: Path, common_dir: Path, *, update_staff: bool = True) -> str:
        if update_staff and move.old in self.staff_sources:
            if reason := self.staff_lock_use:
                return reason
        if move.output is MoveOutput.LIVE_UNIT and self.places_output(move.repo):
            output = build_folders.output_path(move.repo, move.unit, move.short_name)
            return _migration_target_lock(checkout, common_dir, output)
        return ""

    def places_output(self, repo: str) -> bool:
        return repo != "claude" or not self.legacy_claude


@contextmanager
def _hold_worktree_relocation(
    path: Path, journal: MoveJournal, locations: MigrationLocations, folders: MigrationFolders, *,
    ignore_stopped_pane_shells: bool = False,
    record_process_scan: Callable[[tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable], None] = lambda _: None,
) -> Generator[Callable[[], None]]:
    _check_planned_clone_members(journal)
    try:
        current = {member: locations.current(member) for member in dict.fromkeys((path, *journal.clone_members))}
    except (OSError, RuntimeError, MigrationLocationUnverified) as error:
        raise MigrationInUse(f"cannot verify move sources: {error}") from error
    paths = tuple(dict.fromkeys(current.values()))
    fds: list[int] = []
    with ExitStack() as locks:
        leases = _migration_leases(folders, paths)
        leased: set[Path] = set()
        marks = scratch.BusyMarkScope()
        for source in paths:
            if reason := _migration_build_keep(source, folders):
                raise MigrationPlanChanged(reason)
            _take_migration_locks(source, folders, locks, marks, leased, fds, create=True)
            if reason := _migration_lease_use(source, leases):
                raise MigrationInUse(reason)

        def final_check() -> None:
            # A process can enter the source between this scan and the rename.
            uses = _migration_processes(paths, ignored_fds=frozenset(fds))
            record_process_scan(uses)
            for member, source in current.items():
                if reason := _migration_process_use(source, uses, ignore_stopped_pane_shells=ignore_stopped_pane_shells):
                    if member != path and not reason.startswith("unreadable process "):
                        reason = f"linked worktree {member}: {reason}"
                    raise MigrationInUse(reason)

        yield final_check


def _direct_move_guard(path: Path, journal: MoveJournal) -> AbstractContextManager[Callable[[], None]]:
    if journal.copy_tag:
        return _hold_worktree_relocation(path, journal, MigrationLocations({}), MigrationFolders.configured())
    return _unguarded_rename(path, journal)


@dataclass
class MigrationProgress:
    kept: dict[Path, str]
    in_use: dict[Path, str]
    kernel_hidden: dict[int, str]
    moved: int = 0

    def record_process_scan(self, uses: tmp_sweep.ProcessUsesKnown | tmp_sweep.ProcessUsesUnavailable) -> None:
        from scripts.lint import tmp_sweep

        if isinstance(uses, tmp_sweep.ProcessUsesKnown):
            for process in uses.kernel_hidden:
                _ = self.kernel_hidden.setdefault(process.pid, process.comm)

    def print_keeps(self, *, dry_run: bool) -> None:
        for pid, comm in sorted(self.kernel_hidden.items()):
            print(f"not checked (hidden by the kernel): {pid} {comm}")
        reasons = self.kept.copy()
        for path, reason in self.in_use.items():
            if reason.startswith("kept: "):
                reasons[path] = reason
            elif reason.startswith("unreadable process ") or reason == "changed during copy":
                reasons[path] = f"in use: {reason}"
            else:
                reasons[path] = f"in use ({reason})"
        for path, reason in sorted(reasons.items()):
            print(f"keep {path}: {reason}")
        verb = "would move" if dry_run else "moved"
        print(f"{verb} {self.moved}, kept in use {len(self.in_use)}, kept {len(reasons) - len(self.in_use)}")


class MigrationJournalState(Enum):
    RELOCATED = "relocated"
    UNMOVED = "unmoved"
    PUBLISHED_COPY = "published copy"
    OCCUPIED_COPY_DESTINATION = "occupied copy destination"
    UNVERIFIED = "unverified"


def _migration_journal_state(journal: MoveJournal) -> MigrationJournalState:
    if journal.stage is MoveStage.FINISHED:
        return MigrationJournalState.RELOCATED
    if journal.stage is MoveStage.LINKED:
        if journal.destination.matches(journal.move.new):
            return MigrationJournalState.RELOCATED
        return MigrationJournalState.UNVERIFIED
    if journal.copy_tag and journal.source.matches(journal.backup):
        if os.path.lexists(journal.move.new) and not journal.destination.matches(journal.move.new):
            return MigrationJournalState.OCCUPIED_COPY_DESTINATION
        return MigrationJournalState.RELOCATED
    if journal.source.matches(journal.move.old):
        if journal.copy_tag and os.path.lexists(journal.move.new):
            if journal.destination.matches(journal.move.new):
                return MigrationJournalState.PUBLISHED_COPY
            return MigrationJournalState.OCCUPIED_COPY_DESTINATION
        return MigrationJournalState.UNMOVED
    if not journal.copy_tag and journal.source.matches(journal.move.new):
        return MigrationJournalState.RELOCATED
    return MigrationJournalState.UNVERIFIED


def _migration_journals(selection: MigrationSelection, folders: MigrationFolders, *, dry_run: bool) -> MigrationJournalView:
    relocated: list[MoveJournal] = []
    kept: dict[Path, str] = {}
    for path in sorted(move_folder().glob("*.json")):
        journal = read_move(path)
        move = journal.move
        try:
            state = _migration_journal_state(journal)
        except OSError as error:
            kept[move.old] = kept[move.new] = f"cannot verify the saved move ({error})"
            continue
        match state:
            case MigrationJournalState.RELOCATED:
                if isinstance(_worktree_build_form(move.old, folders.build_root), build_folders.BuildCopy):
                    kept[move.old] = kept[move.new] = _NUMBERED_BUILD_COPY
                else:
                    relocated.append(journal)
            case MigrationJournalState.UNMOVED:
                if not dry_run and selection.includes(move.old):
                    if journal.copy_tag and os.path.lexists(journal.staging):
                        report_retained_folder(journal.staging, "a discarded move left a staged copy")
                    path.unlink()
            case MigrationJournalState.PUBLISHED_COPY:
                kept[move.old] = f"review the previously published copy at {move.new} before retry"
                kept[move.new] = f"the saved copy of {move.old} needs review"
            case MigrationJournalState.OCCUPIED_COPY_DESTINATION:
                kept[move.old] = f"review the occupied copy destination at {move.new} before retry"
                kept[move.new] = f"the saved move from {move.old} names this path"
            case MigrationJournalState.UNVERIFIED:
                kept[move.old] = kept[move.new] = "cannot verify the source identity in the saved move"
    return MigrationJournalView(tuple(relocated), kept)


def _print_migration_resume(journal: MoveJournal) -> None:
    move = journal.move
    print(f"resume {move.old} -> {move.new} | {move.unit} | {move.short_name} | move record")


def _migration_recovery_locks(journals: MigrationJournalView, publication: MigrationPublication,
                              locations: MigrationLocations, selection: MigrationSelection) -> MigrationJournalView:
    kept = journals.kept.copy()
    for journal in journals.relocated:
        if journal.stage is MoveStage.FINISHED or not selection.includes_journal(journal):
            continue
        try:
            checkout = locations.current(journal.move.new)
        except (OSError, RuntimeError, MigrationLocationUnverified) as error:
            reason = str(error)
        else:
            common = checkout / ".git" if journal.git_entry == Path(".git") else journal.git_entry.parent.parent
            reason = publication.lock_use(journal.move, checkout, common, update_staff=journal.stage is MoveStage.PLANNED)
        if reason:
            kept[journal.move.old] = kept[journal.move.new] = reason
    return replace(journals, kept=kept)


def _migration_clone_keep(plan: WorktreeMigration, progress: MigrationProgress,
                          sources: dict[Path, Path], clone: Path) -> str:
    for member in plan.clone_members.get(clone, ()):
        if (source := sources.get(member)) is not None:
            if source in progress.in_use or source in progress.kept:
                return f"linked worktree {source} stays in place"
    return ""


def _scratch_backups_needing_recovery(scratch: Path, selection: MigrationSelection) -> Generator[Path]:
    for path in sorted(move_folder().glob("*.json")):
        journal = read_move(path)
        if not journal.copy_tag or journal.stage is MoveStage.FINISHED or not selection.includes_journal(journal):
            continue
        backup = journal.backup
        if not backup.is_relative_to(scratch) or not journal.source.matches(backup):
            continue
        if journal.stage is MoveStage.LINKED and journal.destination.matches(journal.move.new):
            continue
        yield backup


def migrate_worktrees(list_file: Path | None, *, dry_run: bool, scope: MigrationScope = MigrationScope.LISTED,
                      repo: str = "") -> int:
    try:
        with ExitStack() as locks:
            if dry_run:
                _ = locks.enter_context(conf_reads.record_reads(""))
            folders = MigrationFolders.configured()
            snapshot = staff_record()
            listed = listed_worktrees(list_file, folders, snapshot) if list_file is not None else {}
            if scope is MigrationScope.REPOSITORY:
                try:
                    selection = repository_selection(repo, listed, folders)
                except ValueError as error:
                    return _unknown_repo(repo, error)
            else:
                selection = MigrationSelection(scope, listed)
            _ = locks.enter_context(move_lock(create=not dry_run))
            try:
                status = _apply_migration(selection, snapshot, folders, dry_run=dry_run)
            finally:
                backups = (tuple(_scratch_backups_needing_recovery(folders.scratch, selection))
                           if not dry_run and scope is not MigrationScope.LISTED else ())
                for backup in backups:
                    print(f"{backup}: keep the old build root until the move can resume from this backup", file=sys.stderr)
            return 1 if backups else status
    except WorktreeMoveLockHeld as error:
        print(f"kept: {error.lock} held")
        return error.code


def _unknown_repo(repo: str, error: ValueError) -> int:
    print(f"migrate --repo {repo}: {error}", file=sys.stderr)
    return 2


# The Mac copy of this script runs the guarded switch for one repository.
_MAC_MIGRATE = "~/.claude/scripts/lib/py ~/.claude/scripts/worktree/worktrees.py migrate --repo"


def migrate_on_mac(repo: str, *, dry_run: bool) -> int:
    """Hold the repository's Mac claim on natedev while the Mac switches that repository's outputs."""
    from scripts.build_queue import mac_run
    from scripts.mac_test import mac_test

    command = f"{_MAC_MIGRATE} {shlex.quote(repo)}" + (" --dry-run" if dry_run else "")
    if dry_run:
        return _run_on_mac(command)
    try:
        checkout = main_checkout_for_word(repo)
    except ValueError as error:
        return _unknown_repo(repo, error)
    try:
        with mac_run.repository_claim(checkout, "mac", f"migrate --repo {repo}", mac_test.MacClaimKind.MIRROR):
            return _run_on_mac(command)
    except mac_run.MacRunRefused as error:
        print(f"kept: {repo}: {error}")
    return 1


def _run_on_mac(command: str) -> int:
    """Run the command on the Mac target over ssh with the guest keepalive options, and give its exit status."""
    from scripts.build_targets import mac_linux, target_config
    from scripts.mac_test import eligibility

    host = eligibility.read_connection_settings(target_config.configured_ssh_target("mac").settings).host
    return _run_ssh((*mac_linux.SSH, host, command))


def _run_ssh(command: tuple[str, ...]) -> int:
    """Run ssh with stops deferred until it ends, so the claim lasts while the Mac command runs."""
    # ssh inherits the deferred signals, so a stop at the terminal also waits for the Mac command.
    with cancellation.defer_cleanup_signals():
        return subprocess.run(command, stdin=subprocess.DEVNULL, check=False).returncode


def _hold_idle_target(target: Path, held: ExitStack) -> str:
    """Hold every Cargo lock in the target folder, or give the lock that is held."""
    from scripts.lint import sweep

    # A Cargo build folder holds no nested build folder, so the walk stops at one; its output folders are skipped.
    skipped = {*sweep.HASHED_DIRS, sweep.INCREMENTAL_DIR, sweep.DOC_DIR}
    trees: list[str] = []
    for folder, children, files in os.walk(target):
        if any(name in files for name in sweep.LOCK_NAMES):
            trees.append(folder)
            children.clear()
        else:
            children[:] = [child for child in children if child not in skipped]
    locks = sweep.lock_trees(sorted(trees))
    if isinstance(locks, sweep.TargetLockBusy):
        return f"kept: {locks.path} held"
    _ = held.callback(sweep.release, locks.held)
    return ""


def _switch_output(checkout: Path, output: Path, progress: MigrationProgress, *, dry_run: bool) -> bool:
    """Link the checkout's target to the output when no lock or process holds the target."""
    common = git_files.at(checkout).common_dir
    if reason := _migration_target_lock(checkout, common, output):
        progress.kept[checkout] = reason
        return False
    _report_saved_targets(checkout)
    target = checkout / "target"
    real = _is_real_target(target)
    with ExitStack() as held:
        if real:
            # The flocks come first: a cargo that waits on one keeps the lock file open, so the scan finds it.
            if reason := _hold_idle_target(target, held):
                progress.kept[checkout] = reason
                return False
            uses = _migration_processes((target,))
            progress.record_process_scan(uses)
            if reason := _migration_process_use(target, uses, ignore_stopped_pane_shells=False):
                progress.kept[checkout] = f"target in use ({reason})"
                return False
        # A target that appears after this check is not moved; the link refuses it and keeps the checkout.
        if not dry_run and (reason := _link_output(checkout, output, save_existing=real)):
            progress.kept[checkout] = reason
            return False
    return True


def _moved_output(checkout: Path, publication: MigrationPublication) -> Path | str:
    """Give the recorded output of a moved checkout, or the reason its target stays."""
    try:
        layout = worktree_output(checkout)
        if isinstance(layout, UnassignedOutput):
            return "already moved; the record names no output for the real target"
        output = checked_output(layout)
        form = build_folders.parse_form(output)
        if not isinstance(form, build_folders.WorktreeOutput):
            return f"already moved; {output} is not a worktree output"
        if _tracked_target_paths(checkout):
            return "already moved; Git tracks the checkout's target"
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return f"already moved; cannot read the recorded output ({error})"
    if not publication.places_output(form.repo):
        return "already moved; defer output: archive the old build root first"
    return output


def _link_output(checkout: Path, output: Path, *, save_existing: bool) -> str:
    """Move the target to the output and link it, or give the reason to keep the checkout."""
    try:
        ensure_target_link(checkout, output, save_existing=save_existing)
    except TargetReappeared as error:
        return f"rerun: a new {checkout / 'target'} appeared after the move; the earlier target is in {error.kept}"
    except (OSError, ValueError) as error:
        # The target is unchanged or back in place.
        # If the move back failed, the target is in the output, and a rerun links it.
        return f"kept: {error}"
    return ""


def _apply_migration(selection: MigrationSelection, snapshot: staff.StaffSnapshot,
                     folders: MigrationFolders, *, dry_run: bool) -> int:
    from scripts.lint import tmp_sweep

    publication = MigrationPublication.read(snapshot, folders)
    journals = _migration_journals(selection, folders, dry_run=dry_run)
    active_journals: dict[Path, MoveJournal] = {}
    for journal in journals.relocated:
        if journal.stage is MoveStage.FINISHED:
            continue
        destination = journal.move.new
        if destination in active_journals:
            raise ValueError(f"{destination}: multiple unfinished move journals")
        active_journals[destination] = journal
    locations = MigrationLocations(active_journals)
    journals = _migration_recovery_locks(journals, publication, locations, selection)
    progress = MigrationProgress({}, {}, {})

    def protect(path: Path, journal: MoveJournal) -> AbstractContextManager[Callable[[], None]]:
        return _hold_worktree_relocation(path, journal, locations, folders,
                                         ignore_stopped_pane_shells=not journal.copy_tag,
                                         record_process_scan=progress.record_process_scan)

    plan = plan_worktree_migration(selection, snapshot, folders, journals, locations)
    progress.kept.update(plan.skipped)
    sources = {move.move.new: move.move.old for move in plan.moves}
    destinations = {source: destination for destination, source in sources.items()}
    groups = {clone: (clone, *(sources.get(member, member) for member in members))
              for clone, members in plan.clone_members.items()}
    paths: dict[Path, Path] = {}
    busy_paths: dict[Path, str] = {}
    for source in dict.fromkeys(chain(sources.values(), chain.from_iterable(groups.values()))):
        try:
            paths[source] = locations.current(source)
        except (OSError, RuntimeError, MigrationLocationUnverified) as error:
            busy_paths[source] = f"cannot verify the checkout at {source} ({error})"
    lock_paths = {source: [path] for source, path in paths.items()}
    for member in chain.from_iterable(plan.clone_members.values()):
        source = sources.get(member, member)
        if source in lock_paths and member not in lock_paths[source]:
            lock_paths[source].append(member)
    leases = _migration_leases(folders, tuple(chain.from_iterable(lock_paths.values())))
    uses = _migration_processes(tuple(paths.values()))
    progress.record_process_scan(uses)
    idle_shell_candidates: set[str] = set()
    if isinstance(uses, tmp_sweep.ProcessUsesKnown):
        idle_shell_candidates = {name for name, pid, _ in uses.paths if pid in uses.cwd_only_processes}
    for source, path in paths.items():
        reason = ""
        for lock_path in lock_paths[source]:
            reason = _probe_migration_locks(lock_path, folders) or _migration_lease_use(lock_path, leases)
            if reason:
                break
        if not reason:
            rename = False
            if str(path) in idle_shell_candidates and source in destinations:
                with suppress(OSError, ValueError, StopIteration):
                    rename = not _worktree_copy_required(path, destinations[source])
            reason = _migration_process_use(path, uses, ignore_stopped_pane_shells=rename)
        if reason:
            busy_paths[source] = reason
    for planned in plan.moves:
        source = planned.move.old
        if source not in busy_paths:
            common = git_files.at(planned.tree.store).common_dir
            if reason := publication.lock_use(planned.move, paths[source], common):
                busy_paths[source] = reason
    for source in sources.values():
        if reason := busy_paths.get(source):
            progress.in_use[source] = reason
    for clone, group in groups.items():
        if busy := next((path for path in group if path in busy_paths), None):
            for path in group:
                if path in sources.values() and path not in progress.in_use:
                    progress.kept[path] = f"{clone}: {busy} stays in use ({busy_paths[busy]})"

    for journal in journals.relocated:
        if (journal.stage is not MoveStage.FINISHED and selection.includes_journal(journal)
                and journal.move.new not in journals.kept):
            if not dry_run:
                locations.record_publication(_finish_worktree_move(journal, protect=protect))
            _print_migration_resume(journal)
    for tree in plan.missing:
        print(f"review {tree.path}: missing registration path")
        if not dry_run:
            report_retained_folder(tree.store, f"Git still records the missing checkout {tree.path}")
    for planned in plan.moves:
        layout = planned.move
        source = layout.old
        if source in progress.kept or source in progress.in_use:
            continue
        if reason := _migration_clone_keep(plan, progress, sources, source):
            progress.kept[source] = reason
            continue
        if not dry_run:
            journal = _open_move(layout, clone_members=plan.clone_members.get(source, ()))
            try:
                completed = _finish_worktree_move(journal, protect=protect)
            except MigrationPlanChanged as error:
                progress.kept[source] = str(error)
                continue
            except MigrationInUse as error:
                progress.in_use[source] = str(error)
                continue
            locations.record_publication(completed)
        progress.moved += 1
        names = f"{layout.unit} | {layout.short_name} | {planned.source} | {layout.output.value}"
        print(f"move {layout.old} -> {layout.new} | {names}")
    for main in plan.main:
        output = build_folders.main_output_path(main.repo)
        if not publication.places_output(main.repo):
            print(f"defer main output {main.checkout} -> {output}")
            continue
        if _switch_output(main.checkout, output, progress, dry_run=dry_run):
            print(f"main output {main.checkout} -> {output}")
    for checkout in plan.moved:
        recorded = _moved_output(checkout, publication)
        if isinstance(recorded, str):
            # A missing target with no output to link stays as it is.
            if _is_real_target(checkout / "target"):
                progress.kept[checkout] = recorded
        elif _switch_output(checkout, recorded, progress, dry_run=dry_run):
            _ = progress.kept.pop(checkout, None)
            print(f"output {checkout} -> {recorded}")
    for checkout in plan.saved:
        _report_saved_targets(checkout)
    progress.print_keeps(dry_run=dry_run)
    if not dry_run and any(selection.includes(path)
                           and progress.kept.get(path) not in (_UNVERIFIED_SCRATCH, _ARCHIVED_SCRATCH)
                           for path in _scratch_checkouts(folders.scratch)):
        print("keep the old build root until all remaining scratch checkouts have a confirmed location", file=sys.stderr)
        return 1
    # A loop over repositories sees a kept checkout in the exit status.
    kept = progress.in_use.keys() | {path for path, reason in progress.kept.items() if reason not in _SETTLED_KEEPS}
    return 1 if selection.scope is MigrationScope.REPOSITORY and kept else 0


def main() -> int:
    from scripts.agents import remote
    from scripts.production import staff

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("make")
    _ = create.add_argument("repo")
    _ = create.add_argument("unit")
    _ = create.add_argument("short_name", metavar="short-name")
    move = commands.add_parser("move")
    _ = move.add_argument("path", type=Path)
    _ = move.add_argument("unit")
    _ = move.add_argument("short_name", metavar="short-name")
    remove = commands.add_parser("remove")
    _ = remove.add_argument("path", type=Path)
    _ = remove.add_argument("--force", action="store_true")
    retire_unit = commands.add_parser("retire")
    _ = retire_unit.add_argument("unit")
    migration = commands.add_parser("migrate")
    selection = migration.add_mutually_exclusive_group()
    _ = selection.add_argument("--all", dest="scope", action="store_const", const=MigrationScope.ALL,
                               default=MigrationScope.LISTED, help="select all worktrees")
    _ = selection.add_argument("--repo", help="select one repository by its repo word: its main checkout, worktrees, scratch clones and saved moves")
    _ = migration.add_argument("--host", choices=("mac",),
                               help="hold the Mac claim on natedev and run migrate --repo on the Mac")
    _ = migration.add_argument("--list", type=Path, help="select worktrees or supply names for --all or --repo")
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
                scope = cast(MigrationScope, arguments.scope)
                list_file = cast(Path | None, arguments.list)
                repo = cast(str | None, arguments.repo)
                if cast(str | None, arguments.host) is not None:
                    if repo is None or list_file is not None:
                        migration.error("--host needs --repo WORD and takes no --list")
                    if remote.local_machine() != staff.OWNER_MACHINE:
                        migration.error("--host runs on natedev only")
                    return migrate_on_mac(repo, dry_run=cast(bool, arguments.dry_run))
                if repo is not None:
                    scope = MigrationScope.REPOSITORY
                elif scope is MigrationScope.LISTED and list_file is None:
                    migration.error("specify --list FILE, --all or --repo WORD")
                return migrate_worktrees(list_file, scope=scope, repo=repo or "", dry_run=cast(bool, arguments.dry_run))
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
            case "move":
                with redirect_stdout(sys.stderr):
                    path = move_worktree(cast(Path, arguments.path), MoveRepository.FROM_ORIGIN,
                                         cast(str, arguments.unit), cast(str, arguments.short_name))
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

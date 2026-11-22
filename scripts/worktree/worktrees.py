"""Record worktrees and end them during the production merge."""
from __future__ import annotations

import os
import sys

if __name__ == "__main__" and "--dry-run" in sys.argv:
    sys.dont_write_bytecode = True
    _ = os.environ.pop("CONF_READ_LOG", None)

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable, Generator
import fcntl
import hashlib
import json
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal, TypedDict, cast
from uuid import uuid4

from scripts.lib import build_folder_lock, build_folders, conf, conf_reads, git_exclude, git_files, origin_deletion
from scripts.lib.atomic_file import create_text_atomic, write_text_atomic
from scripts.lib.process_failure import failure_detail
from scripts.production import production_doc, staff
from scripts.production.production_names import CARGO_LINER_PRODUCTION
from scripts.worktree import worktree_links


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
    try:
        decoded = cast(object, json.loads(path.read_text()))
    except ValueError as error:
        raise ValueError(f"{path}: invalid worktree record: {error}") from error
    if not isinstance(decoded, dict):
        raise ValueError(f"{path}: worktree record must be an object")
    return _worktree_record(cast(dict[str, object], decoded), path)


def _worktree_record(fields: dict[str, object], path: Path) -> WorktreeRecord:
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
    write_text_atomic(path, json.dumps(_record_fields(record), indent=2) + "\n")


def _record_fields(record: WorktreeRecord) -> dict[str, object]:
    fields = cast(dict[str, object], asdict(record))
    _ = fields.pop("layout")
    fields["stage"] = record.stage.value
    if isinstance(record.layout, RecordedWorktreeOutput):
        fields.update(unit=record.layout.unit, short_name=record.layout.short_name, output=str(record.layout.output))
    return fields


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
            "records": [_record_fields(record) for record in originals],
        }, indent=2) + "\n")
    saved = cast(dict[str, object], json.loads(receipt.read_text()))
    if saved["version"] != 1 or saved["repository"] != str(main) or saved["integration_branch"] != integration_branch:
        raise Refused(1, f"{receipt}: does not describe this cargo-liner move")
    original_records = tuple(_worktree_record(fields, receipt)
                             for fields in cast(list[dict[str, object]], saved["records"]))
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


def landed(record: WorktreeRecord, target: str) -> Landed | NotLanded:
    repo = Path(record.repo)
    if not has_branch(repo, record.branch):
        return Landed()
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


@dataclass(frozen=True)
class CleanupLandingTarget:
    branch: str
    commit: str


def origin_cleanup_tip(repo: Path, ref: str) -> CleanupLandingTarget:
    """Read the landing target and fetch its commit without changing refs."""
    result = git(repo, "ls-remote", "--exit-code", "--symref", "origin", ref, check=False)
    if result.returncode == 2:
        reason = "origin default branch is missing" if ref == "HEAD" else f"{ref}: origin branch is missing"
        raise Refused(1, reason)
    if result.returncode:
        raise Refused(1, failure_detail(result))
    target = ref
    tip = ""
    for line in result.stdout.splitlines():
        value, _, name = line.partition("\t")
        if name == ref:
            if value.startswith("ref: "):
                target = value.removeprefix("ref: ")
            else:
                tip = value
    if not target.startswith("refs/heads/") or not tip:
        raise Refused(1, f"{ref}: origin did not advertise a branch commit")
    if git(repo, "cat-file", "-e", f"{tip}^{{commit}}", check=False).returncode != 0:
        _ = git(repo, "-c", "remote.origin.followRemoteHEAD=never", "fetch", "--no-tags", "--no-prune",
                "--no-write-fetch-head", "--refmap=", "origin", tip)
    return CleanupLandingTarget(target.removeprefix("refs/heads/"), tip)


def check_local_cleanup(path: Path) -> tuple[CheckedLocalTip, WorktreeRecord] | Kept:
    head = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if head.returncode not in (0, 1):
        raise Refused(1, head.stderr.strip())
    branch = head.stdout.strip()
    try:
        production_doc.require_removable_checkout(path, path, branch)
    except production_doc.ProductionPathsError as error:
        return Kept(path, str(error))
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
        target = origin_cleanup_tip(repo, "HEAD")
        base = git(repo, "merge-base", f"refs/heads/{branch}", target.commit).stdout.strip()
        record = new_record(path, branch, repo, base, "", target.branch)
    else:
        if Path(record.repo).resolve() != repo or Path(record.path).resolve() != path or record.branch != branch:
            return Kept(path, "the worktree does not match its record")
        if record.stage is MakeStage.MAKING:
            return Kept(path, "worktree creation is incomplete")
        target = origin_cleanup_tip(repo, f"refs/heads/{record.merge_branch}")
    result = landed(record, target.commit)
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
    try:
        production_doc.require_removable_checkout(path, repo, record.branch)
    except production_doc.ProductionPathsError as error:
        return Kept(path, str(error))

    if record.stage is MakeStage.MAKING:
        return Kept(path, "worktree creation is incomplete")
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
    result = landed(record, f"refs/remotes/origin/{record.merge_branch}")
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
    ref = f"refs/heads/{record.branch}"
    if isinstance(origin, OriginBranchRetained):
        print(f"Kept origin ref {ref}: {origin.reason}")
    elif isinstance(origin, CheckedOriginTip):
        deletion = origin_deletion.delete(repo, "origin", ref, origin.sha, expected_push_url=origin.push_url)
        if isinstance(deletion, origin_deletion.Kept):
            if deletion.cause is origin_deletion.RetentionCause.GIT_FAILURE:
                return Kept(path, f"origin branch deletion failed: {deletion.reason}")
            print(f"Kept origin ref {ref}: {deletion.reason}")
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

    registered = worktree_branches(repository)
    try:
        production_doc.require_removable_checkout(path, repository, registered.get(path, ""))
    except production_doc.ProductionPathsError as error:
        raise Refused(1, str(error)) from error
    if before_report is None:
        report_output()
    retain_checkout = os.path.lexists(path) or path in registered
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


def _migration_skip(path: Path, scratch: Path) -> str:
    home = Path.home()
    if any(parts == (".claude", "worktrees") for parts in zip(path.parts, path.parts[1:])):
        return "Claude Code owns this worktree"
    if path == home / "worktrees/nate_style-message-clarity":
        return "the user's worktree"
    if path == scratch / "sessionmove-bbf204":
        return "queue moves the session-folder checkout before the switch"
    if (any(path.is_relative_to(root) for root in (
            Path("/tmp/claude/unit"), Path("/tmp/claude/scratch"), Path("/tmp/claude/delegate"),
            scratch.parents[1] / "slot", Path("/session"), home / ".local"))
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
    mains = _migration_main_checkouts(Path.home())
    clones: dict[Path, tuple[RegisteredTree, ...]] = {}
    skipped: dict[Path, str] = {path: "main checkout stays in place" for path in mains}
    trees: list[RegisteredTree] = []
    for path in _scratch_checkouts(scratch):
        reason = _migration_skip(path, scratch)
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
             and not _migration_skip(tree.path, scratch)]
    already_placed = {tree.path for tree in trees if tree.path in placed}
    for path in already_placed:
        skipped[path] = "already moved"
    candidates: list[RegisteredTree] = []
    for tree in trees:
        path = tree.path
        if path in already_placed or path in former or tree in stale:
            continue
        reason = showrunners.get(path) or _migration_skip(path, scratch)
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
    production_doc.require_removable_checkout(path, repo, ref.removeprefix("refs/heads/"))
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

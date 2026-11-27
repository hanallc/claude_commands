#!/usr/bin/env python3
"""Run the restartable git and document steps of a production lifecycle."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from functools import partial
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, cast


from scripts.production import ci_points, investigation, session_staff, staff, unit_config
from scripts.agents import pane_record, shutdown, stop_resume
from scripts.lib import conf, git_files, git_merge_tree, land_main, main_publish, main_push_lock, main_sync, origin_deletion
from scripts.herdr import herdr, worker_panes
from scripts.agents.remote import local_machine
from scripts.lib.git_exclude import ExcludeRefused, ensure_excluded, require_untracked_ignored

from scripts.production.store_owner import showrunner_id
from scripts.production import showrunners
from scripts.production import showrunner_tree
from scripts.production import tidy_wrapped
from scripts.production import unit_lookup
from scripts.production.production_doc import Production, Refusal, production_field, read_production, staged_notes_directory
from scripts.production.production_admission import (
    canonical_document_path, capture_rollout_inventory, complete_document_rebind, prepare_document_rebind,
    record_verified_move, require_admission, require_document_receipt,
    require_guard_ready as require_guard_ready,
    require_new_document as require_new_document,
)
from scripts.production.checkpoint_gate import Stop, report
from scripts.production.merge_checkpoint import (
    PromotionAlreadyAtTip, PromotionOriginPushed, PromotionNotPushed, VerifiedIntegrationTip,
    branch_is_landed, git, good, merged_checkpoints, promote_local_checkout, pull_mac_checkout,
)
from scripts.production import production_doc, promotion as publication
from scripts.production.production_names import (
    CARGO_LINER_PRODUCTION, LIVE_DOCUMENT_SUFFIX, MERGE_CHECKOUT_FIELD, MOVE_RECORD,
    MOVE_VERSION, NOTES_DIRECTORY,
)
from scripts.production.update_registration import (
    QuestionStaff, RegistrationFailure, judgment_state, status_line,
)

from scripts.lib.atomic_file import prepared_replacement, sync_folder, write_text_atomic
from scripts.lib.process_failure import failure_detail
from scripts.lib import berth_board
from scripts.worktree import worktree_records, worktrees
from scripts.worktree.worktrees import main_checkout, release_worktree_pair

PHASE_TABLE = Path(__file__).resolve().parent.parent / "direct" / "phase_table.py"
OFFSET_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
                         + r"(?:\.[0-9]+)?[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]")


class PlanLanded(NamedTuple):
    tip: str


class PlanPending(NamedTuple):
    pass


class ProductionPlanned(NamedTuple):
    pass


class ProductionRunning(NamedTuple):
    pass


class ProductionWrapped(NamedTuple):
    pass


class VerdictMissing(NamedTuple):
    pass


VERDICT_MISSING = VerdictMissing()


class CommitVerdict(NamedTuple):
    sha: str


class UnitRow(NamedTuple):
    name: str
    member: staff.StaffRow
    worktree: Path
    branch: str


class LifecycleStop(Exception):
    def __init__(self, step: str, state: str, detail: str) -> None:
        super().__init__(detail)
        self.step: str = step
        self.state: str = state
        self.detail: str = detail


def checked(checkout: Path, step: str, *args: str) -> str:
    return good(git(checkout, *args), step)


def command(step: str, cwd: Path, *args: str) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise LifecycleStop(step, "failed", f"{' '.join(args)}: {failure_detail(result)}")
    return result.stdout.strip()


def doc_lines(production: Production) -> list[str]:
    return production.doc.read_text(encoding="utf-8").splitlines()


@dataclass(frozen=True)
class MoveDestinations:
    live_doc: Path
    log: Path


@dataclass(frozen=True)
class MovePlan:
    main: Path
    integration: Path
    merge_branch: str
    destinations: MoveDestinations


@dataclass(frozen=True)
class MoveOpen:
    """The approved window can still grant a move."""


@dataclass(frozen=True)
class MoveClosed:
    closed_at: datetime


@dataclass(frozen=True)
class MoveApproval:
    production: str
    target_staff_id: staff.StaffId
    executor_staff_id: staff.StaffId
    approved_at: datetime
    window_start: datetime
    window_end: datetime
    destinations: MoveDestinations
    status: MoveOpen | MoveClosed

    def encode(self) -> str:
        status = ({"kind": "open"} if isinstance(self.status, MoveOpen)
                  else {"kind": "closed", "closed_at": self.status.closed_at.isoformat()})
        return json.dumps({
            "version": MOVE_VERSION, "production": self.production,
            "target_staff_id": self.target_staff_id, "executor_staff_id": self.executor_staff_id,
            "approved_at": self.approved_at.isoformat(),
            "window_start": self.window_start.isoformat(), "window_end": self.window_end.isoformat(),
            "destinations": {"live_doc": str(self.destinations.live_doc), "log": str(self.destinations.log)},
            "status": status,
        }, indent=2) + "\n"


@dataclass(frozen=True)
class NoMoveApproval:
    """The record has never been written."""


def offset_time(text: str) -> datetime:
    if not OFFSET_TIME.fullmatch(text):
        raise ValueError("use an RFC 3339 time with a numeric offset")
    return datetime.fromisoformat(text).astimezone(UTC)


def saved_move_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("the saved time must be UTC")
    normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
    if not normalized.endswith("+00:00"):
        raise ValueError("the saved time must be UTC")
    return offset_time(normalized)


def move_fields(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("the move record must contain an object")
    fields = cast(dict[str, object], value)
    if set(fields) != keys:
        raise ValueError("the move record has incorrect fields")
    return fields


def move_text(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("the move record has invalid text")
    return value


def move_destination(value: object) -> Path:
    text = move_text(value)
    destination = Path(text)
    if not destination.is_absolute() or str(destination.resolve()) != text:
        raise ValueError(f"{text}: the move destination must be canonical and absolute")
    return destination


def read_move_approval(path: Path) -> MoveApproval | NoMoveApproval:
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return NoMoveApproval()
    try:
        fields = move_fields(cast(object, json.loads(original)),
                             {"version", "production", "target_staff_id", "executor_staff_id",
                              "approved_at", "window_start", "window_end", "destinations", "status"})
        if type(fields["version"]) is not int or fields["version"] != MOVE_VERSION:
            raise ValueError("the move record version is unsupported")
        start = saved_move_time(fields["window_start"])
        end = saved_move_time(fields["window_end"])
        approved = saved_move_time(fields["approved_at"])
        if start >= end or approved >= end:
            raise ValueError("the move record has an invalid window")
        targets = move_fields(fields["destinations"], {"live_doc", "log"})
        destinations = MoveDestinations(move_destination(targets["live_doc"]), move_destination(targets["log"]))
        if destinations.live_doc == destinations.log:
            raise ValueError("the move destinations must be different")
        status: MoveOpen | MoveClosed
        if fields["status"] == {"kind": "open"}:
            status = MoveOpen()
        else:
            closed = move_fields(fields["status"], {"kind", "closed_at"})
            if closed["kind"] != "closed":
                raise ValueError("the move record has an invalid status")
            closed_at = saved_move_time(closed["closed_at"])
            if not max(start, approved) <= closed_at < end:
                raise ValueError("the move record has an invalid close time")
            status = MoveClosed(closed_at)
        return MoveApproval(move_text(fields["production"]), staff.native_id(move_text(fields["target_staff_id"])),
                            staff.native_id(move_text(fields["executor_staff_id"])), approved, start, end,
                            destinations, status)
    except (OSError, ValueError, OverflowError, staff.StaffRefused) as error:
        raise Refusal(f"{path}: {error}") from error


@contextmanager
def locked_move(owner: staff.StaffRow) -> Generator[Path]:
    path = showrunners.SHOWRUNNER_STATE_DIR / staff.native_id(owner.id) / MOVE_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def require_approval_terminal(action: str = "approve-move") -> None:
    if (staff.caller_identity() or os.environ.get("CLAUDECODE")
            or not all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))):
        raise Refusal(f"{action} requires a regular interactive terminal outside an agent session")
    pid = os.getpid()
    try:
        while pid > 1:
            arguments = pane_record.process_arguments(pid)
            if herdr.is_claude_process(arguments) or herdr.is_codex_process(arguments):
                raise Refusal(f"{action} cannot run from an agent process")
            parent = pane_record.process_parent(pid)
            if parent < 0 or parent == pid:
                raise ValueError("invalid process parent")
            pid = parent
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as error:
        raise Refusal(f"{action} cannot verify process ancestry: {error}") from error


def move_member(reference: str, record: staff.StaffSnapshot) -> staff.StaffRow:
    matches = [row for row in record.rows if reference in (row.id, row.name, record.qualified_name(row))]
    if len(matches) != 1:
        raise Refusal(f"{reference}: expected one staff row")
    return matches[0]


def move_caller(record: staff.StaffSnapshot) -> staff.StaffRow:
    identity = staff.caller_identity() or staff.runtime_id()
    row = record.by_current_session(identity)
    if row is None:
        raise Refusal("the caller has no current staff session")
    return row


def move_plan(production: Production, lines: list[str]) -> MovePlan:
    owner = production.owner
    if not isinstance(owner.doc, staff.LiveDoc) or owner.doc.machine != local_machine():
        raise Refusal(f"{production.name}: the live staff document must be on this machine")
    if staff.declares_merge_checkout(lines):
        raise Refusal(f"{production.name}: the live staff document is already enrolled")
    main = Path(production_field(lines, "Repository")).expanduser().resolve(strict=True)
    main_git = git_files.at(main)
    if main_git.git_dir != main_git.common_dir or git_files.head(main_git) != git_files.OnBranch("main"):
        raise Refusal(f"{main}: expected the main checkout on main")
    source = git_files.locate(production.doc.parent)
    if source.common_dir != main_git.common_dir:
        raise Refusal(f"{production.doc}: document belongs to a different repository")
    integration, branch = production.checkout, production.merge_branch
    if production.name == CARGO_LINER_PRODUCTION:
        merge_unit = staff.serving_merge_unit(owner.id)
        if (merge_unit is None or merge_unit.name != f"{production.name}-merges"
                or not isinstance(merge_unit.worktree, staff.PresentWorktree)
                or merge_unit.worktree.machine != local_machine()):
            raise Refusal(f"{production.name}: record its local merge unit staging worktree first")
        integration = (merge_unit.worktree.path.parent / "integration").resolve(strict=True)
        branch = f"{merge_unit.name}-integration"
    merge_git = git_files.at(integration)
    if (merge_git == main_git or merge_git.common_dir != main_git.common_dir
            or git_files.head(merge_git) != git_files.OnBranch(branch)):
        raise Refusal(f"{integration}: expected a separate worktree of {main} on {branch}")
    relative_doc = production.doc.relative_to(source.toplevel)
    relative_live = relative_doc.with_name(relative_doc.stem + LIVE_DOCUMENT_SUFFIX)
    relative_log = Path(production_field(lines, "Log"))
    live_doc, log = main / relative_live, main / relative_log
    for path in (live_doc, log):
        if path != path.resolve() or not path.is_relative_to(main) or path == main:
            raise Refusal(f"{path}: destination must be a canonical path under {main}")
    if live_doc == log:
        raise Refusal("the move destinations must be different")
    return MovePlan(main, integration, branch, MoveDestinations(live_doc, log))


def approve_move(production: Production, executor: staff.StaffRow, start: datetime, end: datetime,
                 lines: list[str]) -> None:
    if start >= end:
        raise Refusal("the move window start must be before its end")
    plan = move_plan(production, lines)
    with locked_move(production.owner) as path:
        at = datetime.now(UTC)
        if end <= at:
            raise Refusal("the move window has already ended")
        previous = read_move_approval(path)
        if isinstance(previous, MoveApproval) and isinstance(previous.status, MoveOpen) and at < previous.window_end:
            raise Refusal(f"{path}: an unexpired move approval is already open")
        approval = MoveApproval(production.name, staff.native_id(production.owner.id), staff.native_id(executor.id),
                                at, start, end, plan.destinations, MoveOpen())
        write_text_atomic(path, approval.encode(), new_file_mode=0o600)
        print(path.read_text(encoding="utf-8"), end="")


def require_move_window(approval: MoveApproval) -> datetime:
    at = datetime.now(UTC)
    if not isinstance(approval.status, MoveOpen) or not approval.window_start <= at < approval.window_end:
        raise Refusal("move-files requires an open approval within its window")
    return at


def migrated_document(production: Production, lines: list[str], plan: MovePlan) -> bytes:
    fields = {
        "Repository": f"- **Repository:** `{plan.main}`",
        "Merge branch": f"- **Merge branch:** `{plan.merge_branch}`",
        "Showrunner checkout": (f"- **Showrunner checkout:** `{plan.integration}`\n"
                                + f"- **{MERGE_CHECKOUT_FIELD}:** `{plan.main}`"),
    }
    result: list[str] = []
    merge_tests = False
    for line in lines:
        if line.startswith(("- **", "## ")):
            merge_tests = line.startswith("- **Merge tests:**")
        if merge_tests:
            line = line.replace(str(production.checkout), str(plan.integration))
            line = re.sub(r"showrunner checkout", "integration checkout", line, flags=re.IGNORECASE)
        result.append(next((value for field, value in fields.items() if line.startswith(f"- **{field}:**")), line))
    return ("\n".join(result) + "\n").encode("utf-8")


def copy_already_present(path: Path, data: bytes) -> bool:
    if path != path.resolve():
        raise Refusal(f"{path}: cannot copy through a symlink")
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        return False
    if existing != data:
        raise Refusal(f"{path}: the destination already has different bytes")
    return True


def write_move_copy(path: Path, data: bytes, mode: int) -> None:
    if copy_already_present(path, data):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with prepared_replacement(path, data, mode=mode) as temporary:
        try:
            os.link(temporary, path)
        except FileExistsError:
            if not copy_already_present(path, data):
                raise
        else:
            sync_folder(path.parent)


def move_files(production: Production, lines: list[str], record: staff.StaffSnapshot, *,
               reverse: bool = False) -> None:
    caller = move_caller(record)
    plan = move_plan(production, lines)
    targets = plan.destinations
    with locked_move(production.owner) as path:
        approval = read_move_approval(path)
        if isinstance(approval, NoMoveApproval):
            raise Refusal(f"{path}: move approval is missing")
        if (approval.production != production.name or approval.target_staff_id != production.owner.id
                or approval.executor_staff_id != caller.id or approval.destinations != targets):
            raise Refusal(f"{path}: approval must name this production, executor, and both destinations")
        receipt = path.with_name(f"worktrees-{approval.approved_at:%Y%m%dT%H%M%S%fZ}.json")
        if reverse:
            if production.name != CARGO_LINER_PRODUCTION:
                raise Refusal("only cargo-liner has worktree records to reverse")
            count = worktrees.retarget_cargo_liner_records(plan.main, plan.merge_branch, receipt, reverse=True)
            print(f"Restored {count} cargo-liner worktree records.")
            return
        _ = require_move_window(approval)
        require_admission(production)
        for destination in (targets.live_doc, targets.log):
            require_untracked_ignored(plan.main, destination.relative_to(plan.main).as_posix())
        document = migrated_document(production, lines, plan)
        log = production.log.read_bytes()
        same_log = targets.log.exists() and production.log.samefile(targets.log)
        _ = copy_already_present(targets.live_doc, document)
        if not same_log:
            _ = copy_already_present(targets.log, log)
        _ = require_move_window(approval)
        write_move_copy(targets.live_doc, document, production.doc.stat().st_mode & 0o777)
        if not same_log:
            _ = require_move_window(approval)
            write_move_copy(targets.log, log, production.log.stat().st_mode & 0o777)
        if (targets.live_doc.read_bytes() != document or doc_lines(production) != lines
                or targets.log.read_bytes() != production.log.read_bytes()):
            raise Refusal("the live document or LOG differs after the move")
        verified = production_doc.resolve_production_paths(targets.live_doc)
        if (not isinstance(verified, production_doc.EnrolledPaths) or verified.live_doc != targets.live_doc
                or verified.log != targets.log or verified.integration != plan.integration):
            raise Refusal("the moved document does not resolve to the approved destinations")
        if production.name == CARGO_LINER_PRODUCTION:
            _ = require_move_window(approval)
            _ = worktrees.retarget_cargo_liner_records(plan.main, plan.merge_branch, receipt)
        _ = require_move_window(approval)
        record_verified_move(production, targets.live_doc)
        closed = replace(approval, status=MoveClosed(require_move_window(approval)))
        write_text_atomic(path, closed.encode())
        print(f"Moved {targets.live_doc} and {targets.log}.")


def stage_notes(production: Production, lines: list[str], record: staff.StaffSnapshot) -> None:
    if move_caller(record).id != production.owner.id:
        raise Refusal("only the owning showrunner can stage its notes")
    plan = move_plan(production, lines)
    relative = Path(NOTES_DIRECTORY) / staff.native_id(production.owner.id)
    source = production.checkout / relative
    destination = plan.main / relative
    if source != source.resolve() or destination != destination.resolve():
        raise Refusal("notes must use canonical paths")
    ensure_excluded(plan.main, "/" + relative.as_posix())
    if not source.exists():
        return
    destination.mkdir(parents=True, exist_ok=True)
    staged = staged_notes_directory(production.doc)
    if staged is None or staged != destination:
        raise Refusal(f"{destination}: the production notes folder does not match")
    if source.samefile(staged):
        return
    copies: list[tuple[Path, bytes, int]] = []
    for entry in sorted(source.rglob("*")):
        if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
            raise Refusal(f"{entry}: the notes folder must contain regular files and directories")
        if entry.is_file():
            target = staged / entry.relative_to(source)
            require_untracked_ignored(plan.main, target.relative_to(plan.main).as_posix())
            data = entry.read_bytes()
            _ = copy_already_present(target, data)
            copies.append((target, data, entry.stat().st_mode & 0o777))
    for target, data, mode in copies:
        write_move_copy(target, data, mode)
    print(f"Staged {len(copies)} notes in {staged}.")


def doc_status(lines: list[str]) -> ProductionPlanned | ProductionRunning | ProductionWrapped:
    match = status_line(lines)
    if match is None:
        raise LifecycleStop("input", "failed", "production doc lacks a valid status")
    status = match.group(1)
    if status == "planned":
        return ProductionPlanned()
    if status == "running":
        return ProductionRunning()
    return ProductionWrapped()


def wrap_branch_path(row: staff.StaffRow) -> Path:
    assert isinstance(row.parent, staff.ReportsTo)
    return tidy_wrapped.wrap_unit_records(row.parent.id) / f"{row.id}.json"


def wrap_branch_facts(row: staff.StaffRow) -> dict[str, str]:
    assert not isinstance(row.worktree, staff.WorktreeUnfilled)
    return {"session": row.current_session, "worktree": str(row.worktree.path), "machine": row.worktree.machine}


def saved_wrap_branch(row: staff.StaffRow) -> str:
    path = wrap_branch_path(row)
    try:
        saved = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return ""
    if isinstance(saved, dict):
        fields = cast(dict[str, object], saved)
        branch = fields.get("branch")
        if isinstance(branch, str) and branch and fields == {**wrap_branch_facts(row), "branch": branch}:
            return branch
    raise ValueError("saved branch does not match the session and worktree")


class BranchMergeState(Enum):
    MERGED = "merged"
    ABSENT = "absent"
    UNMERGED = "unmerged"
    UNKNOWN = "unknown"


class RetirementBranch(NamedTuple):
    name: str
    state: BranchMergeState
    tips: shutdown.BranchTips
    reason: str = ""
    fetch_identity: str = ""


class RetirementBranches(NamedTuple):
    branches: tuple[RetirementBranch, ...]
    unreadable_records: tuple[str, ...]
    wrap_branch: str = ""

    def problems(self) -> tuple[str, ...]:
        return (*self.unreadable_records, *(branch.reason for branch in self.branches if branch.reason))


RETIREMENT_BRANCH_ERRORS = (OSError, ValueError, subprocess.SubprocessError, Stop)


def fetch_retirement_tip(run: git_merge_tree.GitRunner, fetch_url: str, ref: str, local_tip: str) -> str:
    result = run("ls-remote", "--exit-code", "--heads", "--", fetch_url, ref)
    if result.returncode == 2:
        return ""
    tip = good(result, "retire").split()[0]
    if tip != local_tip:
        _ = good(run("fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", "--", fetch_url, ref), "retire")
    return tip


def retirement_tips_landed(unit: shutdown.RetiringUnit, tips: shutdown.BranchTips, fetch_url: str,
                           *, settings: conf.ConfigRead) -> bool:
    remaining = {tip for tip in tips if tip}
    if not remaining:
        return True
    run = partial(shutdown.retirement_git, unit, settings=settings)
    target_ref = f"refs/heads/{unit.production.merge_branch}"
    unmerged: set[str] = set()
    errors: list[str] = []

    def compare(target: str) -> None:
        for tip in tuple(remaining):
            try:
                landed = branch_is_landed(unit.production.checkout, tip, target)
                if landed and good(run("rev-list", "--merges", "--max-count=1", tip, "--not", target), "retire"):
                    landed = git_merge_tree.leaves_target_unchanged(run, tip, target)
                if landed:
                    remaining.remove(tip)
                else:
                    unmerged.add(tip)
            except RETIREMENT_BRANCH_ERRORS as error:
                errors.append(str(error))

    local_target = ""
    try:
        local_target = shutdown.retirement_local_tip(unit, target_ref, settings=settings)
        if local_target:
            compare(local_target)
    except RETIREMENT_BRANCH_ERRORS as error:
        errors.append(str(error))
    if remaining and fetch_url:
        try:
            origin_target = fetch_retirement_tip(run, fetch_url, target_ref, local_target)
            if origin_target and origin_target != local_target:
                compare(origin_target)
        except RETIREMENT_BRANCH_ERRORS as error:
            errors.append(str(error))
    if remaining & unmerged:
        return False
    if remaining:
        reason = errors[0] if errors else f"merge branch {unit.production.merge_branch} is absent from origin and this clone"
        raise ValueError(reason)
    return True


def retirement_branch_state(unit: shutdown.RetiringUnit, settings: conf.ConfigRead) -> RetirementBranch:
    branch = unit.branch
    ref = f"refs/heads/{branch}"
    tips = shutdown.BranchTips("", "")
    fetch_identity = ""
    fetch_url = ""
    try:
        run = partial(shutdown.retirement_git, unit, settings=settings)
        if run("check-ref-format", ref).returncode:
            raise ValueError(f"{branch!r}: invalid recorded branch")
        errors: list[str] = []
        try:
            tips = tips._replace(local=shutdown.retirement_local_tip(unit, ref, settings=settings))
        except RETIREMENT_BRANCH_ERRORS as error:
            errors.append(str(error))
        try:
            url = shutdown.retirement_origin_url(unit.production.checkout, settings=settings)
            resolved = run("ls-remote", "--get-url", "--", url)
            if resolved.returncode or resolved.stdout.removesuffix("\n") != url:
                raise ValueError("Git rewrites the checked origin fetch URL")
            fetch_identity = shutdown.retirement_origin_identity(unit.production.checkout, url)
            fetch_url = url
            tips = tips._replace(origin=fetch_retirement_tip(run, fetch_url, ref, tips.local))
        except RETIREMENT_BRANCH_ERRORS as error:
            errors.append(str(error))
        if not retirement_tips_landed(unit, tips, fetch_url, settings=settings):
            return RetirementBranch(branch, BranchMergeState.UNMERGED, tips, f"unmerged commits on {branch}")
        if errors:
            raise ValueError(errors[0])
        if (rebasing := shutdown.retirement_rebase(unit, settings=settings)) is not None:
            raise ValueError(f"branch {branch} is rebasing in {rebasing}")
    except RETIREMENT_BRANCH_ERRORS as error:
        return RetirementBranch(branch, BranchMergeState.UNKNOWN, tips, f"{branch}: {error}")
    state = BranchMergeState.MERGED if any(tips) else BranchMergeState.ABSENT
    return RetirementBranch(branch, state, tips, fetch_identity=fetch_identity)


def retirement_branches(production: Production, row: staff.StaffRow, *, checkout_branch: str = "") -> RetirementBranches:
    """Read all branch names and check each branch in the production repository."""
    location = row.worktree
    if isinstance(location, staff.WorktreeUnfilled):
        return RetirementBranches((), ("worktree is unfilled",))
    wrap_branch = ""
    problems: list[str] = []
    try:
        wrap_branch = saved_wrap_branch(row)
    except (OSError, ValueError) as error:
        problems.append(str(error))
    names = [checkout_branch, wrap_branch]
    if isinstance(location, staff.RemovedWorktree) and isinstance(location.branch, staff.ParkedBranch):
        names.append(location.branch.value)
    try:
        saved = shutdown.saved_retirement(row) if location.machine == local_machine() else {}
        if saved:
            branch, checkout = str(saved["branch"]), Path(str(saved["checkout"]))
            if (not checkout.is_absolute()
                    or git_files.locate(checkout).common_dir != git_files.locate(production.checkout).common_dir):
                raise ValueError("the retirement journal names a different repository")
            if branch:
                names.append(branch)
    except (OSError, ValueError) as error:
        problems.append(str(error))

    unique = tuple(dict.fromkeys(name for name in names if name))
    if not unique:
        return RetirementBranches((), tuple(problems) or (f"{location.path}: no recorded branch",))
    try:
        settings = conf.read("production")
    except (OSError, ValueError) as error:
        branches = tuple(RetirementBranch(name, BranchMergeState.UNKNOWN, shutdown.BranchTips("", ""),
                                          f"{name}: {error}") for name in unique)
        return RetirementBranches(branches, tuple(problems), wrap_branch)
    branches = tuple(retirement_branch_state(
        shutdown.RetiringUnit(production, row.name, location.path, name, row), settings) for name in unique)
    return RetirementBranches(branches, tuple(problems), wrap_branch)


def require_deferred_branch(unit_fields: dict[str, object], parent_fields: dict[str, object]) -> None:
    """Require proof for every recorded branch before deferred retirement."""
    try:
        row = staff.StaffRow.from_json(unit_fields)
        parent = staff.StaffRow.from_json(parent_fields)
        evidence = retirement_branches(read_production(parent), row)
        if problems := evidence.problems():
            raise ValueError(". ".join(problems))
    except (Refusal, staff.StaffRefused) as error:
        raise ValueError(str(error)) from error


def units(runner: staff.StaffRow, record: staff.StaffSnapshot) -> tuple[UnitRow, ...]:
    resolver = session_staff.RowResolver(record)
    rows = record.find(kind="unit", parent=runner.id)
    permanent = [row.name for row in rows
                 if isinstance(config := resolver.unit_config(row), unit_config.UnitConfig)
                 and isinstance(config.plan, unit_config.PermanentPlan)]
    if permanent:
        raise LifecycleStop("wrap", "held", "; ".join(
            f"permanent child {name}: transfer it before wrapping" for name in permanent))
    parsed: list[UnitRow] = []
    for row in rows:
        location = row.worktree
        if isinstance(location, staff.WorktreeUnfilled):
            raise LifecycleStop("retire", "held", f"{row.name}: worktree is unfilled")
        if location.machine != local_machine():
            raise LifecycleStop("retire", "held", f"{row.name}: waiting on worktree host {location.machine}")
        branch = ""
        if os.path.lexists(location.path):
            found = resolver.unit_branch(row)
            if not isinstance(found, session_staff.Branch):
                raise LifecycleStop("retire", "held", f"{row.name}: {found.reason}")
            branch = found.name
        parsed.append(UnitRow(row.name, row, location.path, branch))
    return tuple(parsed)


def branch_state(production: Production) -> PlanLanded | PlanPending:
    result = git(production.checkout, "show-ref", "--verify", "--quiet",
                 f"refs/heads/{production.merge_branch}")
    if result.returncode:
        return PlanPending()
    return PlanLanded(checked(production.checkout, "checkout", "rev-parse", production.merge_branch))


def checkout_ready(production: Production) -> PlanLanded | PlanPending:
    state = branch_state(production)
    current = checked(production.checkout, "checkout", "branch", "--show-current")
    if isinstance(state, PlanLanded):
        ready = current == production.merge_branch
    else:
        ready = not checked(production.checkout, "checkout", "status", "--porcelain")
    if not ready:
        raise LifecycleStop("checkout", "held",
                            f"Run /showrunner:produce in a checkout on {production.merge_branch}.")
    report("checkout", "ok", f"{production.checkout} on {current or 'detached HEAD'}")
    return state


def move_session_state(production_name: str, scratch: Path) -> None:
    identity = showrunner_id(production_name)
    copied = showrunners.copy_session_state(identity, scratch)
    report("state-dir", "ok", f"{showrunners.session_state_dir(identity)}"
           + (f"; copied {', '.join(copied)} from {scratch}" if copied else ""))


def load(production: Production, lines: list[str], resume: bool, scratch: Path | None) -> None:
    require_admission(production)
    status = ProductionRunning() if resume else doc_status(lines)
    state = checkout_ready(production)
    report("load", "ok", "production doc read")
    if scratch is not None:
        move_session_state(production.name, scratch)
    if not isinstance(status, ProductionRunning):
        return
    owed: tuple[str, ...] = ()
    try:
        owed = judgment_state(showrunners.session_state_dir(production.owner.id) / "showrunner_state.json", "load").owed
    except FileNotFoundError:
        pass
    except (RegistrationFailure, OSError, ValueError, UnicodeError) as error:
        print(f"anomaly: {error}", file=sys.stderr)
    report("state", "ok", "; ".join(owed) if owed else "no next list")
    record = staff.snapshot()
    try:
        marked = unit_lookup.every_labeled_session(record=record)
    except OSError as error:
        raise LifecycleStop("session", "failed", str(error)) from error
    checkpoints = {entry.unit: entry for entry in merged_checkpoints(production, record=record)} if isinstance(state, PlanLanded) else {}
    for row in record.find(kind="unit", parent=production.owner.id):
        name = row.name
        entry = checkpoints.get(name)
        detail = f"phase {entry.phase} ({entry.commit[:7]})" if entry is not None else "no code checkpoint"
        report("last-merged", "ok", f"{name}: {detail}")
        match marked.checked_unit(production.name, name):
            case unit_lookup.ProvedLiveUnit(owner=unit_lookup.LabeledSession(place=place)):
                description = f"{place.session} live"
            case unit_lookup.ProvedLiveUnit():
                description = "live"
            case unit_lookup.ProvedAbsentUnit():
                description = "gone"
            case unit_lookup.UnknownUnit(reason=reason):
                description = f"unknown ({reason})"
        report("session", "ok", f"{name}: {description}")


def doc_path_in_checkout(production: Production, path: Path) -> str:
    try:
        return path.resolve().relative_to(production.checkout.resolve()).as_posix()
    except ValueError as error:
        raise LifecycleStop("open", "failed", f"{path} is outside {production.checkout}") from error


def open_production(production: Production, lines: list[str]) -> None:
    status = doc_status(lines)
    if isinstance(status, ProductionWrapped):
        raise LifecycleStop("open", "held", "production is already wrapped")
    require_admission(production)
    original = production.doc.read_bytes()
    if original.decode("utf-8").splitlines() != lines:
        raise LifecycleStop("doc", "held", f"{production.doc}: document changed before open")
    record = staff.snapshot()
    enrolled = production_doc.resolve_production_paths(production.doc)
    main = enrolled.main if isinstance(enrolled, production_doc.EnrolledPaths) else production.checkout
    if isinstance(enrolled, production_doc.EnrolledPaths):
        for path in (enrolled.live_doc, enrolled.log):
            require_untracked_ignored(main, path.relative_to(main).as_posix())
    rows = tuple(record.find(kind="unit", parent=production.owner.id))
    resolver = session_staff.RowResolver(record)
    state = checkout_ready(production)
    if isinstance(state, PlanPending):
        _ = checked(production.checkout, "branch", "switch", "-c", production.merge_branch)
        report("branch", "ok", f"created {production.merge_branch}")
    else:
        report("branch", "ok", f"already at {state.tip}")
    updated = list(lines)
    if isinstance(status, ProductionPlanned):
        updated = [line.replace("**Status: PRODUCTION — planned.**", "**Status: PRODUCTION — running.**")
                   for line in updated]
    if updated != lines:
        mode = production.doc.stat().st_mode & 0o777
        with prepared_replacement(production.doc, "\n".join(updated) + "\n", mode=mode) as temporary:
            if production.doc.read_bytes() != original or production.doc.stat().st_mode & 0o777 != mode:
                raise LifecycleStop("doc", "held", f"{production.doc}: document changed during open")
            _ = temporary.replace(production.doc)
            sync_folder(production.doc.parent)
    report("doc", "ok", "running")
    paths = [doc_path_in_checkout(production, production.doc)] if isinstance(enrolled, production_doc.LegacyProduction) else []
    for row in rows:
        name = row.name
        match resolver.unit_config(row):
            case unit_config.UnitConfig(plan=unit_config.PlanFile(path=relative)):
                plan = production.checkout / relative
                if plan.exists():
                    paths.append(doc_path_in_checkout(production, plan))
                else:
                    report("plan", "ok", f"{name}: no plan yet at {plan}; not committed")
            case unit_config.UnitConfig():
                pass
            case _:
                report("plan", "ok", f"{name}: no .unit.toml yet; no plan committed")
    subject = f"production({production.name}): plans for {len(rows)} units"
    subjects = checked(production.checkout, "commit", "log", "--format=%s", production.merge_branch)
    committed_paths = checked(production.checkout, "commit", "ls-tree", "-r", "--name-only", "HEAD", "--", *paths) if paths else ""
    if not paths or subject in subjects.splitlines() and set(paths).issubset(committed_paths.splitlines()):
        report("commit", "ok", "plans already committed")
    else:
        _ = checked(production.checkout, "commit", "add", "--", *paths)
        enrolled_paths = ["--", *paths] if isinstance(enrolled, production_doc.EnrolledPaths) else []
        pending = git(production.checkout, "diff", "--cached", "--quiet", *enrolled_paths)
        if pending.returncode == 1:
            options = ["--only"] if enrolled_paths else []
            _ = checked(production.checkout, "commit", "commit", *options, "-m", subject, *enrolled_paths)
            report("commit", "ok", "unit plans committed" if enrolled_paths else "production doc and unit plans committed")
        elif pending.returncode == 0:
            report("commit", "ok", "plans already committed")
        else:
            raise LifecycleStop("commit", "failed", pending.stderr.strip())
    tip = checked(production.checkout, "push", "rev-parse", production.merge_branch)
    remote = git(production.checkout, "ls-remote", "origin", f"refs/heads/{production.merge_branch}")
    if remote.returncode:
        raise LifecycleStop("push", "failed", remote.stderr.strip())
    if not remote.stdout.startswith(tip):
        if publication.is_main_branch(production.merge_branch) and isinstance(enrolled, production_doc.LegacyProduction):
            _ = checked(production.checkout, "push", "push", "-u", "origin", "main")
        else:
            push_integration_tip(production, tip, "push")
            branch = production.merge_branch.removeprefix("refs/heads/")
            _ = checked(production.checkout, "push", "branch", "--set-upstream-to", f"origin/{branch}", branch)
        report("push", "ok", f"{production.merge_branch} at {tip}")
    else:
        report("push", "ok", f"already at {tip}")
    log_name = production.log.relative_to(main).as_posix()
    try:
        if isinstance(enrolled, production_doc.LegacyProduction):
            ensure_excluded(main, log_name)
    except ExcludeRefused as error:
        raise LifecycleStop("log", "failed", str(error)) from error
    if not production.log.exists():
        production.log.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(production.log, f"# Production log — {production.name}\n")
    report("log", "ok", f"excluded and initialized {log_name}")


def ci_steps(production: Production, tip: str, no_ci: bool,
             ci_green: CommitVerdict | VerdictMissing,
             smoke_passed: CommitVerdict | VerdictMissing) -> None:
    if production.name == CARGO_LINER_PRODUCTION and staff.declares_merge_checkout(doc_lines(production)):
        if no_ci:
            raise LifecycleStop("ci", "held", "this production requires CI on the integration tip")
        state_dir = showrunners.session_state_dir(production.owner.id) / "waiting_state"
        try:
            ci_points.ci_collect(production, state_dir, no_ci)
            verdict = ci_points.ci_record(state_dir)
        except ci_points.PointFailure as error:
            raise LifecycleStop(error.step, "held", error.detail) from error
        if not isinstance(verdict, ci_points.CiRecord) or verdict.tip != tip:
            raise LifecycleStop("ci", "held", f"no collected CI result for integration tip {tip}")
        return
    if no_ci:
        for step in ("ci", "smoke-launch", "mac-run"):
            report(step, "ok", "skipped: no CI")
        return
    if isinstance(ci_green, VerdictMissing):
        raise LifecycleStop("ci", "held", "wait for a green CI point on the merge tip")
    ci_commit = git(production.checkout, "rev-parse", "--verify", f"{ci_green.sha}^{{commit}}")
    if ci_commit.returncode or ci_commit.stdout.strip() != tip:
        raise LifecycleStop("ci", "held", f"CI verdict {ci_green.sha} resolves to "
                            + f"{ci_commit.stdout.strip() or 'no commit'}, merge tip is {tip}")
    report("ci", "ok", "green CI point confirmed")
    if isinstance(smoke_passed, VerdictMissing):
        raise LifecycleStop("smoke-launch", "held", "record the live smoke verdict on this tip")
    smoke_commit = git(production.checkout, "rev-parse", "--verify", f"{smoke_passed.sha}^{{commit}}")
    if smoke_commit.returncode or smoke_commit.stdout.strip() != tip:
        raise LifecycleStop("smoke-launch", "held", f"smoke verdict {smoke_passed.sha} resolves to "
                            + f"{smoke_commit.stdout.strip() or 'no commit'}, merge tip is {tip}")
    report("smoke-launch", "ok", "live smoke verdict passed")
    report("mac-run", "ok", "Mac run is recorded with the CI point")


def push_integration_tip(production: Production, tip: str, step: str) -> None:
    try:
        publication.push_integration(production.checkout, tip, production.merge_branch)
    except Stop as error:
        raise LifecycleStop(step, error.state, error.detail) from error


def promote_main(production: Production, lines: list[str], no_ci: bool,
                 ci_green: CommitVerdict | VerdictMissing,
                 smoke_passed: CommitVerdict | VerdictMissing,
                 checked_tip: CommitVerdict | VerdictMissing = VERDICT_MISSING) -> str:
    state = checkout_ready(production)
    if isinstance(state, PlanPending):
        raise LifecycleStop("promote", "held", "merge branch does not exist")
    tip = state.tip
    if isinstance(checked_tip, VerdictMissing):
        ci_steps(production, tip, no_ci, ci_green, smoke_passed)
    elif checked_tip.sha != tip:
        raise LifecycleStop("promote", "held", f"checked tip {checked_tip.sha} differs from integration tip {tip}")
    with main_push_lock.main_push_lock(production.checkout):
        current = checked(production.checkout, "promote", "rev-parse", "HEAD")
        if current != tip:
            raise LifecycleStop("promote", "held", f"integration tip changed from {tip} to {current}")
        try:
            conflicts = main_sync.live_file_conflicts(main_checkout(production.checkout), tip)
        except production_doc.ProductionPathsError as error:
            raise LifecycleStop("push", "held", str(error)) from error
        if conflicts:
            for conflict in conflicts:
                report("push", "held", str(conflict))
            raise LifecycleStop("push", "held", "incoming tree conflicts with live production files")
        _ = checked(production.checkout, "main", "fetch", "origin", "main")
        if git(production.checkout, "merge-base", "--is-ancestor", "origin/main", tip).returncode:
            raise LifecycleStop("main", "held", f"origin/main has commits absent from {tip}")
        report("main", "ok", f"origin/main is behind {tip}")
        promotion = production_doc.promotion_from_doc(lines)
        approval: publication.PromotionApproval
        if no_ci:
            approval = publication.LifecycleNoCi(production.doc)
        elif isinstance(ci_green, CommitVerdict):
            approval = publication.LifecycleCi(production.doc, ci_green.sha)
        else:
            approval = publication.CollectedCi(production.doc)

        def publish_destinations(approved: publication.ApprovedPromotion) -> None:
            if isinstance(promotion, production_doc.NoPromotion) or not promotion.checkouts:
                landed = approved.land(production.checkout)
                detail = f"main pushed to {approved.sha}" if isinstance(landed.push, land_main.Pushed) else f"already at {approved.sha}"
                report("promote", "ok", detail)
                report("post-push", "ok", land_main.hook_report(landed.hook))
                return
            verified = VerifiedIntegrationTip(production.checkout, approved.sha)
            remote_ref = f"refs/remotes/origin/{production.merge_branch}"
            _ = checked(production.checkout, "promote", "fetch", "origin",
                        f"+refs/heads/{production.merge_branch}:{remote_ref}")
            for destination in promotion.checkouts:
                verified.require_current(production.merge_branch, production.checkout)
                if destination.resolve() == production.merge_checkout:
                    result = promote_local_checkout(destination, production.merge_branch, approved,
                                                    integration=production.checkout)
                else:
                    result = promote_local_checkout(destination, production.merge_branch, approved)
                if isinstance(result, PromotionNotPushed):
                    raise LifecycleStop("promote", "held", f"{destination} has main commits outside the merge branch")
                if isinstance(result, PromotionAlreadyAtTip):
                    detail = f"{destination}: already at {result.tip}"
                elif isinstance(result, PromotionOriginPushed):
                    detail = f"{destination}: pushed {result.tip}"
                else:
                    detail = f"{destination}: advanced to {result.tip}"
                report("promote", "ok", detail)
        try:
            if (publication.is_main_branch(production.merge_branch)
                    and isinstance(production.merge_checkout, production_doc.LegacyProduction)):
                published = publication.publish_legacy_main(production.checkout, tip, publish_destinations)
            else:
                published = publication.publish(approval, production.checkout, production.merge_branch, tip, publish_destinations)
        except land_main.LandError as error:
            raise LifecycleStop("push", "failed", str(error)) from error
        except Stop as error:
            raise LifecycleStop(error.step, error.state, error.detail) from error
    if isinstance(promotion, production_doc.PromoteTo) and isinstance(promotion.mac, production_doc.MacCheckout):
        source = promotion.checkouts[0] if promotion.checkouts else production.checkout
        outcome = pull_mac_checkout(promotion.mac.path, source, published)
        if isinstance(outcome, main_publish.MacPullPending):
            report("promote", "ok", f"mac unreachable, pull pending: {promotion.mac.path}")
        else:
            report("promote", "ok", f"mac {promotion.mac.path} main pulled")
    return published.sha


def close_out_items(lines: list[str]) -> tuple[str, ...]:
    if "## Close-out" not in lines:
        return ()
    start = lines.index("## Close-out") + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    return tuple(line[2:].strip() for line in lines[start:end] if line.startswith("- "))


def archived_doc(production: Production) -> Path | None:
    """Where a wrapped doc kept in a productions/ folder goes: archive/<repository>/<production_name>/ beside it."""
    if production.doc.parent.name != "productions":
        return None
    common = Path(checked(production.checkout, "wrap", "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return production.doc.parent.parent / "archive" / common.parent.name / production.name / production.doc.name


def wrap_production(name: str) -> Production:
    """Recover the recorded document after an interrupted archive move."""
    record = staff.snapshot()
    owner = showrunners.owner_for(name, record)
    if isinstance(owner.doc, staff.LiveDoc) and not owner.doc.path.exists():
        source = canonical_document_path(owner.doc.path)
        checkout = Path(checked(source.parent, "wrap", "rev-parse", "--show-toplevel"))
        renames = checked(checkout, "wrap", "diff", "--cached", "--name-status",
                          "--diff-filter=R", "--find-renames", "-z").split("\0")
        for before, after in zip(renames[1::3], renames[2::3]):
            if checkout / before == source:
                document = production_doc.read_new_production(checkout / after, owner.name, infer_legacy_branch=True)
                production = document.for_owner(owner)
                if archived_doc(replace(production, doc=source)) == production.doc:
                    return production
    if isinstance(owner.doc, (staff.LiveDoc, staff.WrappedDoc)):
        return production_doc.read_new_production(owner.doc.path, owner.name, infer_legacy_branch=True).for_owner(owner)
    return read_production(owner)


def record_wrap_merge_branch(production: Production) -> None:
    """Keep the resolved branch in the production record before removing worktrees."""
    before = production.doc.read_bytes()
    document = before.decode("utf-8")
    lines = document.splitlines()
    field = production_doc.optional_field(lines, "Merge branch")
    if isinstance(field, production_doc.FieldPresent) and (saved := production_doc.cell_value(field.text)):
        if saved != production.merge_branch:
            raise LifecycleStop("retire", "held", f"{production.doc}: Merge branch changed to {saved}")
        return
    head = git_files.head(git_files.at(production.checkout))
    if head != git_files.OnBranch(production.merge_branch):
        raise LifecycleStop("retire", "held", f"{production.checkout}: expected {production.merge_branch}")
    line = f"- **Merge branch:** `{production.merge_branch}`"
    if isinstance(field, production_doc.FieldPresent):
        updated = re.sub(r"(?m)^- \*\*Merge branch:\*\*[^\r\n]*", lambda _match: line, document, count=1)
    else:
        separator = "\r\n" if "\r\n" in document else "\n"
        checkout_field = re.search(r"(?m)^- \*\*Showrunner checkout:\*\*", document)
        if checkout_field is None:
            raise LifecycleStop("retire", "held", f"{production.doc}: Showrunner checkout is missing")
        position = checkout_field.start()
        updated = document[:position] + line + separator + document[position:]
    if production.doc.read_bytes() != before:
        raise LifecycleStop("retire", "held", f"{production.doc}: changed while recording Merge branch")
    write_text_atomic(production.doc, updated)
    report("retire", "ok", f"recorded Merge branch {production.merge_branch}")



def retire_reservations(worktree: Path) -> None:
    board = berth_board.read_board(worktree)
    if isinstance(board, berth_board.BoardUnavailable):
        report("retire", "skipped", f"board unavailable ({board.reason})")
        return
    for reservation in board.reservations():
        if reservation.holder_worktree_root.resolve() == worktree.resolve():
            _ = command("retire", worktree, "cargo-berth", "release", reservation.reservation_id, "--json")
            report("retire", "ok", f"released reservation {reservation.reservation_id} for {worktree}")


def refuse_running_children(production_name: str, record: staff.StaffSnapshot) -> None:
    """Hold a wrap for running product children; keep style children attached until retirement."""
    try:
        children = showrunner_tree.stored_children(production_name, record=record)
        running: list[str] = []
        discovered = showrunners.registered_showrunners()
        failures = [failure.reason for failure in discovered.lookup_failures if failure.production in children]
        if failures:
            raise OSError("; ".join(failures))
        for child in discovered:
            if child["name"] not in children or not child["socket"]:
                continue
            lines = Path(child["doc"]).read_text(encoding="utf-8").splitlines()
            try:
                is_style = production_field(lines, "Kind") == "style"
            except Refusal:
                is_style = False
            if not is_style:
                running.append(child["name"])
    except (OSError, UnicodeError) as error:
        raise LifecycleStop("children", "failed", str(error)) from error
    if running:
        raise LifecycleStop("children", "held", f"{', '.join(running)} still report{'s' if len(running) == 1 else ''} "
                            + f"to {production_name}; wrap each first, or detach it with `showrunners.py detach <child>`")


def release_path(production_name: str, *, record: staff.StaffSnapshot | None = None) -> Path:
    return showrunners.SHOWRUNNER_STATE_DIR / showrunner_id(production_name, record=record) / "wrap_release.json"


def save_release(production_name: str, released: showrunner_tree.WrapRelease, *,
                 record: staff.StaffSnapshot | None = None) -> None:
    path = release_path(production_name, record=record)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({
        "former_parent": released.former_parent.parent if isinstance(released.former_parent, showrunner_tree.ReportedTo) else None,
        "former_children": released.former_children, "told": released.told,
    }, indent=2, sort_keys=True) + "\n")


def capture_release(production_name: str, *, record: staff.StaffSnapshot | None = None) -> showrunner_tree.WrapRelease:
    path = release_path(production_name, record=record)
    if not path.exists():
        released = showrunner_tree.edges_to_release(production_name, record=record)
        save_release(production_name, released, record=record)
        return released
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a release object")
    fields = cast(dict[str, object], value)
    parent, children, told = (fields.get(key) for key in ("former_parent", "former_children", "told"))
    if (parent is not None and not isinstance(parent, str)) or not isinstance(children, list) or not isinstance(told, list):
        raise ValueError(f"{path}: invalid release capture")
    if not all(isinstance(item, str) for item in [*cast(list[object], children), *cast(list[object], told)]):
        raise ValueError(f"{path}: release staff ids must be strings")
    return showrunner_tree.WrapRelease(showrunner_tree.ReportedTo(staff.StaffId(parent)) if isinstance(parent, str) else showrunner_tree.Root(),
                                      tuple(staff.StaffId(item) for item in cast(list[str], children)),
                                      tuple(staff.StaffId(item) for item in cast(list[str], told)))


def release_edges(production_name: str, *, record: staff.StaffSnapshot | None = None) -> None:
    """Send captured wrap notices and save each completed delivery."""
    released = capture_release(production_name, record=record)
    recipients = (*((released.former_parent.parent,) if isinstance(released.former_parent, showrunner_tree.ReportedTo) else ()),
                  *released.former_children)
    messages: list[str] = []
    for target in recipients:
        if target not in released.told:
            name = staff.snapshot().rows_by_id[target].name
            parent = isinstance(released.former_parent, showrunner_tree.ReportedTo) and target == released.former_parent.parent
            text = (f"{production_name} wrapped; its recorded parent is retained for history." if parent else
                    f"{production_name} wrapped; {name} keeps its recorded parent for history.")
            told = showrunners.tell_others({name: text}, f"showrunner-tree:{production_name if parent else name}")
            if told.failed:
                raise LifecycleStop("tree", "failed", "; ".join(told.lines))
            released = replace(released, told=(*released.told, target))
            save_release(production_name, released, record=record)
            messages.extend(told.lines)
    current = staff.snapshot()
    names = {target: current.rows_by_id[target].name for target in recipients}
    above = f"retained parent {names[released.former_parent.parent]}" if isinstance(released.former_parent, showrunner_tree.ReportedTo) else ""
    below = f"retained children {', '.join(names[child] for child in released.former_children)}" if released.former_children else ""
    report("tree", "ok", "; ".join(part for part in (above, below, *messages) if part) or "no parent and no child showrunners")


class PreparedWrapUnit(NamedTuple):
    unit: UnitRow
    retirement: unit_config.RetiredSession


def require_removable_wrap_checkout(production: Production, unit: UnitRow, branch: str) -> None:
    try:
        production_doc.require_removable_checkout(unit.worktree, production.checkout, branch)
    except production_doc.ProductionPathsError as error:
        raise LifecycleStop("retire", "held", f"{unit.name}: {error}") from error


def require_wrap_branches(production: Production, unit: UnitRow, *, checkout_branch: str = "") -> RetirementBranches:
    evidence = retirement_branches(production, unit.member, checkout_branch=checkout_branch)
    if problems := evidence.problems():
        raise LifecycleStop("retire", "held", f"{unit.name}: {'. '.join(problems)}")
    return evidence


def checked_wrap_branch(production: Production, unit: UnitRow) -> tuple[RetirementBranch, shutdown.RetirementOrigin]:
    evidence = require_wrap_branches(production, unit, checkout_branch=unit.branch)
    branch = evidence.branches[0]
    if unit.branch and (not branch.tips.local or evidence.wrap_branch not in ("", unit.branch)):
        raise LifecycleStop("retire", "held", f"{unit.name}: the worktree branch conflicts with the recorded branch")
    require_removable_wrap_checkout(production, unit, branch.name)
    try:
        retiring = shutdown.RetiringUnit(production, unit.name, unit.worktree, branch.name, unit.member)
        origin = shutdown.retirement_origin(retiring, settings=conf.read("production"))
        if (not origin.push_url
                or shutdown.retirement_origin_identity(production.checkout, origin.push_url) != branch.fetch_identity):
            raise ValueError("the push destination does not match the checked fetch origin")
    except RETIREMENT_BRANCH_ERRORS as error:
        raise LifecycleStop("retire", "held", f"{unit.name}: {error}") from error
    return branch, origin


def prepare_wrap_units(production: Production, rows: tuple[UnitRow, ...]) -> tuple[PreparedWrapUnit, ...]:
    """Validate all retirement evidence before the first worktree is removed."""
    prepared: list[PreparedWrapUnit] = []
    for unit in rows:
        location = unit.member.worktree
        if isinstance(location, staff.WorktreeUnfilled) or location.machine != local_machine():
            raise LifecycleStop("retire", "held", f"{unit.name}: staff worktree is not on {local_machine()}")
        pair = unit_config.place(unit.member.id, unit.worktree, cleanup=False)
        if isinstance(pair, unit_config.Both) and not pair.same:
            raise LifecycleStop("retire", "held", f"{pair.worktree} and {pair.parked}: configurations differ; no file was changed")
        saved = unit_config.read_parked(unit.member.id, cleanup=False)
        current = unit_config.read(unit.worktree, cleanup=False)
        if isinstance(saved, unit_config.UnitConfig):
            if not isinstance(saved.retired, unit_config.RetiredSession):
                raise LifecycleStop("retire", "held", f"{unit.name}: parked retirement facts are incomplete")
            retirement = saved.retired
        elif isinstance(current, unit_config.ConfigMissing):
            raise LifecycleStop("retire", "held", current.reason)
        elif not unit.member.current_session:
            raise LifecycleStop("retire", "held", f"{unit.name}: the staff row has no director session for retirement")
        else:
            retirement = unit_config.RetiredSession(datetime.now(production.zone).date().isoformat(),
                                                   unit.member.current_session)
        if unit.worktree.exists():
            if isinstance(location, staff.RemovedWorktree) and not isinstance(location.branch, staff.ParkedBranch):
                raise LifecycleStop("retire", "held", f"{unit.name}: removed worktree path is occupied")
            require_untracked_ignored(unit.worktree, ".unit.toml")
            actual = checked(unit.worktree, "retire", "symbolic-ref", "--quiet", "--short", "HEAD")
            if actual != unit.branch:
                raise LifecycleStop("retire", "held", f"{unit.name}: worktree branch {actual} conflicts with {unit.branch}")
        prepared.append(PreparedWrapUnit(unit, retirement))
    return tuple(prepared)


def clean_wrap_unit(production: Production, item: PreparedWrapUnit, record: staff.StaffSnapshot) -> None:
    """Remove the unit's checkout and branches before the phase note archive."""
    unit = item.unit
    member = unit.member
    with staff.slot_operation(member.id, member.name):
        current = staff.authority_snapshot().by_id(member.id)
        if current is None or current.to_json() != member.to_json():
            raise LifecycleStop("retire", "held", f"{unit.name}: the unit changed during wrap")
        reasons = stop_resume.stop_unit_workers(member.id, record)
        if reasons:
            raise LifecycleStop("retire", "held", "\n".join(reasons))
        current_record = staff.authority_snapshot()
        workers = current_record.find(kind="worker", parent=member.id)
        remaining = worker_panes.observe(member.id, current_record) if workers else []
        if any(isinstance(state, (worker_panes.OpenWorker, worker_panes.UncertainWorker,
                                  worker_panes.ProcessUnknownWorker)) for state in remaining):
            raise LifecycleStop("retire", "held", f"{unit.name}: worker liveness is not clear")
        staff.require_stopped(member.id)
        path = wrap_branch_path(member)
        has_worktree = os.path.lexists(unit.worktree)
        if has_worktree != bool(unit.branch):
            raise LifecycleStop("retire", "held", f"{unit.name}: the worktree changed during wrap")
        if has_worktree and not worktree_records.registered_checkout(unit.worktree, production.checkout, unit.branch):
            raise LifecycleStop("retire", "held", f"{unit.name}: the Git worktree identity changed")
        branch, origin = checked_wrap_branch(production, unit)
        _ = unit_config.park(member.id, unit.worktree, item.retirement)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(path, json.dumps({**wrap_branch_facts(member), "branch": branch.name}) + "\n")
            sync_folder(path.parent.parent)
        if has_worktree:
            retire_reservations(unit.worktree)
            _ = checked(production.checkout, "retire", "worktree", "remove", str(unit.worktree))
            release_worktree_pair(unit.worktree)
            report("retire", "ok", f"removed {unit.name} worktree")
        retire_wrap_branch(production, branch, origin)


def finish_wrap_unit(production: Production, unit: UnitRow) -> bool:
    """Remove the staff row after the phase note archive."""
    member = unit.member
    with staff.slot_operation(member.id, member.name):
        record = staff.authority_snapshot()
        current = record.by_id(member.id)
        if current is None or current.to_json() != member.to_json() or os.path.lexists(unit.worktree):
            raise LifecycleStop("retire", "held", f"{unit.name}: the unit or its worktree changed during wrap")
        workers = record.find(kind="worker", parent=member.id)
        if workers:
            for worker in workers:
                print(f"kept worker {worker.id} {worker.name} under {unit.name}")
            staff.defer_retire((member.id,))
            return False
        _ = require_wrap_branches(production, unit)
        if os.path.lexists(unit.worktree):
            raise LifecycleStop("retire", "held", f"{unit.name}: the worktree returned during wrap")
        staff.retire_unit(member, expected_workers=())
        return True


def retire_wrap_branch(production: Production, checked_branch: RetirementBranch,
                       origin: shutdown.RetirementOrigin) -> None:
    branch = checked_branch.name
    ref = f"refs/heads/{branch}"
    checkout = production.checkout
    local_tip, origin_tip = checked_branch.tips
    if origin_tip and not origin.push_url:
        raise LifecycleStop("retire", "held", f"{branch}: no checked push destination")
    if local_tip:
        if branch in worktree_records.worktree_branches(checkout).values():
            raise LifecycleStop("retire", "held", f"{branch} is in use by a worktree")
        _ = checked(checkout, "retire", "update-ref", "-d", ref, local_tip)
        report("retire", "ok", f"deleted local {branch}")
    if origin_tip:
        result = origin_deletion.delete(checkout, "origin", ref, origin_tip, expected_push_url=origin.push_url)
        if isinstance(result, origin_deletion.Kept):
            if result.cause is origin_deletion.RetentionCause.GIT_FAILURE:
                raise LifecycleStop("retire", "failed", result.reason)
            report("retire", "kept", f"{ref} on origin: {' '.join(result.reason.splitlines())}")
        elif isinstance(result, origin_deletion.Deleted):
            report("retire", "ok", f"deleted origin/{branch}")


def tidy_state(production_name: str, document: Path, *, identities: QuestionStaff) -> None:
    """Close owned questions and delete state files. Keep the files when cleanup fails."""
    try:
        removed = tidy_wrapped.tidy(production_name, document, identities=identities)
    except (tidy_wrapped.TidyRefused, OSError) as error:
        report("tidy", "kept", f"{error}; tidy_wrapped.py removes them once the doc says wrapped")
        return
    names = ", ".join(str(path.relative_to(tidy_wrapped.STATE_DIR)) + ("/" if path.parent == tidy_wrapped.STATE_DIR else "")
                      for path in removed)
    report("tidy", "ok", f"removed {names}" if removed else "no state files")


def wrap(production: Production, lines: list[str], no_ci: bool,
         ci_green: CommitVerdict | VerdictMissing,
         smoke_passed: CommitVerdict | VerdictMissing, done: tuple[str, ...]) -> int:
    held = False
    status = doc_status(lines)
    enrolled = staff.declares_merge_checkout(lines)
    if isinstance(status, ProductionPlanned):
        raise LifecycleStop("wrap", "held", "production must be running")
    if enrolled:
        main = session_staff.production_session_directory(production.doc, production.checkout)
        for path in (production.doc, production.log):
            require_untracked_ignored(main, path.relative_to(main).as_posix())
    record = staff.snapshot()
    if isinstance(status, ProductionRunning):
        refuse_running_children(production.name, record)
    state = checkout_ready(production)
    if isinstance(state, PlanPending):
        raise LifecycleStop("wrap", "held", "merge branch does not exist")
    runner = production.owner
    rows = units(runner, record)
    for unit in rows:
        require_removable_wrap_checkout(production, unit, unit.branch)
    checkpoints = merged_checkpoints(production, record=record)
    if isinstance(status, ProductionRunning):
        for item in close_out_items(lines):
            if item not in done:
                raise LifecycleStop("close-out", "held", f'{item} is not recorded done; finish it or get the owner\'s OK, then pass --close-out-done "{item}"')
            report("close-out", "ok", f"{item} (recorded done)")
    require_document_receipt(runner.id)
    prepared = prepare_wrap_units(production, rows)
    tip = checked(production.checkout, "wrap", "rev-parse", "HEAD")
    commit_count = "0"
    if isinstance(status, ProductionRunning):
        ci_steps(production, tip, no_ci, ci_green, smoke_passed)
    for unit in rows:
        if unit.worktree.exists():
            worktree_status = checked(unit.worktree, "worktree", "status", "--short")
            if worktree_status:
                raise LifecycleStop("worktree", "failed", f"{unit.name} at {unit.worktree} is dirty: {worktree_status}")
        branch, _ = checked_wrap_branch(production, unit)
        if branch.state is BranchMergeState.ABSENT:
            report("worktree", "ok", f"{unit.name} already retired")
        else:
            report("worktree", "ok", f"{unit.name} clean and merged")
    if isinstance(status, ProductionRunning):
        current = checked(production.checkout, "wrap", "rev-parse", "HEAD")
        if current != tip:
            raise LifecycleStop("wrap", "held", f"integration tip changed from {tip} to {current}")
        if (publication.is_main_branch(production.merge_branch)
                and isinstance(production.merge_checkout, production_doc.LegacyProduction)):
            _ = checked(production.checkout, "push", "push", "origin", "main")
        else:
            push_integration_tip(production, tip, "push")
        report("push", "ok", f"{production.merge_branch} pushed")
        _ = checked(production.checkout, "main", "fetch", "origin", "main")
        commit_count = checked(production.checkout, "main", "rev-list", "--count", f"origin/main..{tip}")
        tip = promote_main(production, lines, no_ci, ci_green, smoke_passed, checked_tip=CommitVerdict(tip))
    if isinstance(status, ProductionRunning):
        _ = capture_release(production.name, record=record)
    record_wrap_merge_branch(production)
    for item in prepared:
        clean_wrap_unit(production, item, record)
    if isinstance(status, ProductionRunning):
        release_edges(production.name, record=record)
        archived = command(
            "phase-notes",
            production.checkout,
            sys.executable,
            str(PHASE_TABLE),
            "archive",
            "--production-doc",
            str(production.doc),
        )
        count = len(archived.splitlines())
        report(
            "phase-notes",
            "ok",
            f"archived {count} phase note{'' if count == 1 else 's'}"
            if count
            else "no phase notes to archive",
        )
    for unit in rows:
        held = not finish_wrap_unit(production, unit) or held
    if record.find(kind="worker", parent=runner.id):
        staff.defer_retire((runner.id,))
        held = True
    if isinstance(status, ProductionRunning):
        notifier = Path.home() / ".claude/scripts/message/notifier.sh"
        _ = command("notifier", production.checkout, str(notifier), "remove", showrunners.timer_name(production.name))
        report("notifier", "ok", "update timer removed")
        with investigation.recipient_locks(runner.id):
            original = production.doc.read_bytes()
            document = original.decode("utf-8")
            current_status = doc_status(document.splitlines())
            if isinstance(current_status, ProductionPlanned):
                raise LifecycleStop("wrap", "held", "production status changed to planned")
            if isinstance(current_status, ProductionRunning):
                updated = re.sub(r"(?m)^> \*\*Status: PRODUCTION — running\.\*\*",
                                 "> **Status: PRODUCTION — wrapped.**", document, count=1)
                mode = production.doc.stat().st_mode & 0o777
                with prepared_replacement(production.doc, updated.encode("utf-8"), mode=mode) as temporary:
                    if production.doc.stat().st_mode & 0o777 != mode or production.doc.read_bytes() != original:
                        raise LifecycleStop("wrap", "held", f"{production.doc}: document changed during wrap")
                    os.replace(temporary, production.doc)
                    sync_folder(production.doc.parent)
    doc_paths = [] if enrolled else [doc_path_in_checkout(production, production.doc)]
    archive = None if enrolled else archived_doc(production)
    final_doc = archive if archive is not None else production.doc
    binding = staff.WrappedDoc(local_machine(), final_doc)
    prepare_document_rebind(runner, binding)
    if archive is not None:
        archive.parent.mkdir(parents=True, exist_ok=True)
        doc_paths.append(doc_path_in_checkout(production, archive))
        _ = checked(production.checkout, "wrap", "mv", "--", *doc_paths)
        report("wrap", "ok", f"moved the doc out of productions/ to {doc_paths[1]}")
    elif doc_paths:
        renames = checked(production.checkout, "wrap", "diff", "--cached", "--name-status",
                          "--diff-filter=R", "--find-renames", "-z").split("\0")
        doc_paths[:0] = [source for source, destination in zip(renames[1::3], renames[2::3])
                        if destination == doc_paths[-1]]
    if not final_doc.is_file():
        raise LifecycleStop("staff", "held", f"wrapped production doc is missing: {final_doc}")
    wrapped_runner = complete_document_rebind(runner, binding)
    record = replace(record, rows=tuple(wrapped_runner if row.id == wrapped_runner.id else row for row in record.rows))
    if record.pending_retire:
        _ = staff.retire_pending()
    if doc_paths and git(production.checkout, "diff", "--quiet", "HEAD", "--", *doc_paths).returncode == 1:
        _ = checked(production.checkout, "wrap", "add", "--", doc_paths[-1])
        _ = checked(production.checkout, "wrap", "commit", "--only", "-m",
                    f"production({production.name}): wrapped", "--", *doc_paths)
        report("wrap", "ok", "production doc committed")
    elif doc_paths:
        report("wrap", "ok", "production doc already committed")
    current_tip = checked(production.checkout, "wrap", "rev-parse", "HEAD")
    remote_tip = git(production.checkout, "ls-remote", "origin", f"refs/heads/{production.merge_branch}")
    if remote_tip.returncode:
        raise LifecycleStop("wrap", "failed", remote_tip.stderr.strip())
    if not remote_tip.stdout.startswith(current_tip):
        if (publication.is_main_branch(production.merge_branch)
                and isinstance(production.merge_checkout, production_doc.LegacyProduction)):
            _ = checked(production.checkout, "wrap", "push", "origin", "main")
        else:
            push_integration_tip(production, current_tip, "wrap")
        report("wrap", "ok", "production doc pushed")
    else:
        report("wrap", "ok", "production doc already pushed")
    if isinstance(status, ProductionWrapped):
        subject = checked(production.checkout, "wrap", "log", "-1", "--format=%s")
        if subject == f"production({production.name}): wrapped":
            tip = checked(production.checkout, "wrap", "rev-parse", "HEAD^")
    print("| Area | Result |\n| --- | --- |")
    for unit in rows:
        merged = [entry for entry in checkpoints if entry.unit == unit.name]
        detail = (f"{unit.name}: {len(merged)} phases merged, last {merged[-1].commit[:7]}" if merged
                  else f"{unit.name}: no phases merged")
        print(f"| Units | {detail} |")
    print(f"| Merge branch | `{production.merge_branch}` at {current_tip[:7]}, pushed |")
    print(f"| CI | {'skipped: no CI' if no_ci else 'green'} |")
    print(f"| Close-out | {', '.join(close_out_items(lines)) or 'none'} |")
    main_result = (f"promoted to {tip[:7]} ({commit_count} commits)" if isinstance(status, ProductionRunning)
                   else f"promoted to {tip[:7]} by the earlier run")
    print(f"| Main | {main_result} |")
    print("| Next | close herdr sessions |")
    if not held:
        tidy_state(production.name, final_doc, identities=QuestionStaff.for_record(record))
    return 1 if held else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("action", choices=("load", "open", "promote-main", "wrap", "approve-move", "move-files",
                                               "stage-notes", "capture-admission-inventory"))
    _ = parser.add_argument("--production")
    _ = parser.add_argument("--staff-id", action="append", default=[],
                            help="capture-admission-inventory: repeat for each existing legacy production")
    _ = parser.add_argument("--reverse", action="store_true",
                            help="move-files: restore cargo-liner worktree records after restoring its legacy document")
    _ = parser.add_argument("--executor")
    _ = parser.add_argument("--window-start")
    _ = parser.add_argument("--window-end")
    _ = parser.add_argument("--no-ci", action="store_true")
    _ = parser.add_argument("--resume", action="store_true")
    _ = parser.add_argument("--scratch", type=Path, help="load: the scratchpad whose state moves to the state directory")
    # Accepted and unused: a showrunner started before its name was looked up still passes it.
    _ = parser.add_argument("--session")
    _ = parser.add_argument("--ci-green")
    _ = parser.add_argument("--smoke-passed")
    _ = parser.add_argument("--close-out-done", action="append", default=[])
    args = parser.parse_args()
    try:
        action = cast(str, args.action)
        name = cast(str | None, args.production)
        identities = cast(list[str], args.staff_id)
        executor = cast(str | None, args.executor)
        start = cast(str | None, args.window_start)
        end = cast(str | None, args.window_end)
        reverse = cast(bool, args.reverse)
        if action == "capture-admission-inventory":
            if name is not None or executor is not None or start is not None or end is not None or reverse:
                raise Refusal("capture-admission-inventory accepts only --staff-id")
            require_approval_terminal(action)
            print(capture_rollout_inventory(identities))
            return 0
        if identities:
            raise Refusal("only capture-admission-inventory accepts --staff-id")
        if name is None:
            parser.error("--production is required")
        if reverse and action != "move-files":
            raise Refusal("only move-files accepts --reverse")
        if action == "approve-move":
            require_approval_terminal()
            if executor is None or start is None or end is None:
                raise Refusal("approve-move requires --executor, --window-start and --window-end")
            record = staff.snapshot()
            owner = move_member(name, record)
            if owner.kind != "showrunner":
                raise Refusal(f"{name}: the target must be a showrunner")
            production = read_production(owner, record)
            approve_move(production, move_member(executor, record), offset_time(start), offset_time(end),
                         doc_lines(production))
            return 0
        if executor is not None or start is not None or end is not None:
            raise Refusal("only approve-move accepts the executor and window options")
        if action in ("move-files", "stage-notes"):
            record = staff.snapshot()
            production = read_production(name, record)
            lines = doc_lines(production)
            if action == "move-files":
                move_files(production, lines, record, reverse=reverse)
            else:
                stage_notes(production, lines, record)
            return 0
        no_ci = cast(bool, args.no_ci)
        ci_sha = cast(str | None, args.ci_green)
        smoke_sha = cast(str | None, args.smoke_passed)
        if no_ci and (ci_sha is not None or smoke_sha is not None):
            raise LifecycleStop("input", "failed", "--no-ci excludes the verdict flags")
        ci_green = CommitVerdict(ci_sha) if ci_sha is not None else VerdictMissing()
        smoke_passed = CommitVerdict(smoke_sha) if smoke_sha is not None else VerdictMissing()
        production = wrap_production(name) if action == "wrap" else read_production(name)
        lines = doc_lines(production)
        if action == "load":
            load(production, lines, cast(bool, args.resume), cast(Path | None, args.scratch))
        elif action == "open":
            open_production(production, lines)
        elif action == "promote-main":
            _ = promote_main(production, lines, no_ci, ci_green, smoke_passed)
        else:
            with staff.operation():
                return wrap(production, lines, no_ci, ci_green, smoke_passed,
                 tuple(cast(list[str], args.close_out_done)))
        return 0
    except LifecycleStop as error:
        report(error.step, error.state, error.detail.replace("\n", "; "))
    except Stop as error:
        report(error.step, error.state, error.detail.replace("\n", "; "))
    except (staff.StaffRefused, staff.StaffNotFound, Refusal, worktrees.Refused,
            OSError, ValueError, OverflowError) as error:
        report("input", "failed", str(error).replace("\n", "; "))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

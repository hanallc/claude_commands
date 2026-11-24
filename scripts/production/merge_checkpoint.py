#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Merge, test, push, promote, and record one production checkpoint.

A unit checkpoint commit without its Change-Review line is held before the merge. After the push,
each visible change becomes a row in the change-review list of the production's top-level showrunner
(`change_review.py`), with the doc stamped into its frontmatter; the first row creates a missing list, and the list commits itself when it sits in a git
work tree. A refused commit warns and never fails the merge. A recorded code checkpoint ends its
phase: the scratch folders of task `phase:<unit>/<phase>` (scripts/lib/scratch.py) are removed.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import bisect
import fcntl
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator, Iterable
from contextlib import ExitStack, contextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path, PurePath, PurePosixPath
from typing import NamedTuple, cast

from scripts.buildlog import record as build_record, store as build_store
from scripts.lib import build_folders, git_merge_tree
from scripts.lib.atomic_file import write_text_atomic
from scripts.production import change_review, checkpoint_gate, land_runs
from scripts.worktree import worktrees
from scripts.lib import land_main, mac_wake, main_publish, main_push_lock, scratch, waiting_rows
from scripts.production.showrunners import owner_for
from scripts.production.production_doc import Production, ProductionDocument, Refusal, cell_value, production_field, read_production

from scripts.direct import progress_history  # noqa: E402
from scripts.production import gate, production_doc, session_staff, staff, unit_config, unit_lookup, unit_names  # noqa: E402
from scripts.agents import remote  # noqa: E402
from scripts.lib.probe_session import PROBE_ENV  # noqa: E402
from scripts.message import sessions  # noqa: E402
from scripts.lib.process_failure import failure_detail  # noqa: E402
from scripts.lib import conf  # noqa: E402
from scripts.message import session_send  # noqa: E402

# /zone queues a change for the Mac while it is offline; a promotion's Mac pull sends it.
ZONE_SCRIPT = Path(__file__).resolve().parents[1] / "lib/zone.py"
PYTHON = str(Path(__file__).resolve().parents[1] / "lib/py")


class ValidateAndPush(NamedTuple):
    pass


class GitPush(NamedTuple):
    pass


class PushedTip(NamedTuple):
    tip: str


class NotYetPushed(NamedTuple):
    pass


class CheckpointOrigin(NamedTuple):
    branch: str
    already_merged: bool


class PromotionAlreadyAtTip(NamedTuple):
    tip: str


class PromotionAdvanced(NamedTuple):
    tip: str


class PromotionOriginPushed(NamedTuple):
    tip: str


class PromotionNotPushed(NamedTuple):
    tip: str
    already_at_tip: bool


class CodeCheckpoint(NamedTuple):
    pass


class ShrinkCommit(NamedTuple):
    pass


class AsBuiltCommit(NamedTuple):
    """A unit's closing documentation commit after its last phase merged."""


class ReviewTrial(NamedTuple):
    ux_findings: str
    code_findings: str
    review_minutes: str
    ux_check_minutes: str
    ux_repair_minutes: str


class NoReviewTrial(NamedTuple):
    pass


class Unit(NamedTuple):
    name: str
    plan: Path | unit_config.StandbyPlan | unit_config.PermanentPlan
    worktree: Path
    branch: str
    staff_id: staff.StaffId


class UnitUnavailable(NamedTuple):
    name: str
    reason: str


class StaffParentIsDestination(NamedTuple):
    pass


class MergePolicy(NamedTuple):
    push: ValidateAndPush | GitPush
    promotion: production_doc.NoPromotion | production_doc.PromoteTo
    merge_tests: tuple[tuple[str, ...], ...]
    flakes: frozenset[str]
    flaky_tests: frozenset[str]


class MergeRequest(NamedTuple):
    """The destination production and the staff parent that authorizes the merge."""

    production: Production
    unit: Unit
    phase: str
    commit: str
    kind: CodeCheckpoint | ShrinkCommit | AsBuiltCommit
    push: ValidateAndPush | GitPush
    promotion: production_doc.NoPromotion | production_doc.PromoteTo
    merge_tests: tuple[tuple[str, ...], ...]
    flakes: frozenset[str]
    flaky_tests: frozenset[str]
    units: tuple[Unit | UnitUnavailable, ...]
    scratch: Path
    trailer: tuple[str, ...]
    review_trial: ReviewTrial | NoReviewTrial
    delivers: str
    cancel_prior: bool
    started: str
    regime: str
    holds: int
    merge_defects: int
    excluded: str
    bump: bool = False
    staff_record: staff.StaffSnapshot | None = None
    staff_parent: Production | StaffParentIsDestination = StaffParentIsDestination()
    batch_argument_line: str = ""

    @property
    def parent_production(self) -> Production:
        return self.production if isinstance(self.staff_parent, StaffParentIsDestination) else self.staff_parent


def end_pushed_worktrees(production: ProductionDocument) -> None:
    for result in worktrees.end_landed(production.checkout, production.merge_branch):
        if isinstance(result, worktrees.Ended):
            checkpoint_gate.report("worktrees", "ended", f"{result.path}; branch {result.branch}")
        else:
            checkpoint_gate.report("worktrees", "kept", f"{result.path}: {result.reason}".replace("\n", "; "))


@contextmanager
def merge_lock(checkout: Path, name: str) -> Generator[None]:
    """Hold the lock for checkpoint integration and unit renames."""
    common = git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        raise OSError(f"{checkout}: no git directory for the merge lock: {common.stderr.strip()}")
    with (Path(common.stdout.strip()) / f"showrunner-merge-{name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"merge lock: waiting — another operation holds {name}'s checkpoint lock", flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(checkout), *args]
    if args[:1] == ("merge",):
        result = subprocess.run(command, capture_output=True, check=False)
        return subprocess.CompletedProcess(command, result.returncode,
                                           result.stdout.decode(errors="replace"),
                                           result.stderr.decode(errors="replace"))
    return subprocess.run(command, capture_output=True, text=True, check=False)


def good(result: subprocess.CompletedProcess[str], step: str) -> str:
    if result.returncode:
        raise checkpoint_gate.Stop(step, "failed", failure_detail(result),
                   "the showrunner will inspect the failed command")
    return result.stdout.strip()


def listed_names(lines: list[str], field: str) -> frozenset[str]:
    """A doc field's names: the backtick-quoted ones, else a comma list before " — "."""
    present = production_doc.optional_field(lines, field)
    if isinstance(present, production_doc.FieldAbsent):
        return frozenset()
    quoted = re.findall(r"`([^`]+)`", present.text)
    names = quoted if quoted else present.text.split(" — ", 1)[0].split(",")
    return frozenset(name.strip() for name in names if name.strip())


def unit_from_row(row: staff.StaffRow, resolver: session_staff.RowResolver) -> Unit | UnitUnavailable:
    """A local unit from its staff row, its branch and its `.unit.toml`."""
    name = row.name
    location = row.worktree
    if not isinstance(location, staff.PresentWorktree) or location.machine != remote.local_machine():
        return UnitUnavailable(name, "the worktree is not on this machine")
    branch = resolver.unit_branch(row)
    if not isinstance(branch, session_staff.Branch):
        return UnitUnavailable(name, branch.reason)
    try:
        config = resolver.unit_config(row)
    except unit_config.ConfigRefused as error:
        return UnitUnavailable(name, str(error))
    if isinstance(config, session_staff.NoUnitConfig):
        return UnitUnavailable(name, "no unit configuration")
    if not isinstance(config, unit_config.UnitConfig):
        return UnitUnavailable(name, config.reason)
    plan = config.plan.path if isinstance(config.plan, unit_config.PlanFile) else config.plan
    return Unit(name, plan, location.path, branch.name, row.id)


def parse_merge_tests(lines: list[str], checkout: Path) -> tuple[tuple[str, ...], ...]:
    """Each command the Merge tests line names, split and expanded, checked before any merge starts.

    The commands run with no shell, so `~` and `$VAR` are expanded here. A command must start with a
    program on PATH or an executable file in the showrunner checkout: a package name or a directory
    written there by mistake is refused by name.
    """
    field = production_doc.optional_field(lines, "Merge tests")
    if isinstance(field, production_doc.FieldAbsent):
        raise Refusal("production doc lacks Merge tests")
    first_comment = len(field.text)
    in_backticks = False
    for index, char in enumerate(field.text):
        if char == "`":
            in_backticks = not in_backticks
        elif char == "(" and not in_backticks:
            first_comment = index
            break
    commands: list[tuple[str, ...]] = []
    for entry in cast(list[str], re.findall(r"`([^`]+)`", field.text[:first_comment])):
        try:
            words = [os.path.expandvars(os.path.expanduser(word)) for word in shlex.split(entry)]
        except ValueError as error:
            raise Refusal(f"Merge tests entry `{entry}` does not split into words: {error}") from error
        if not words:
            raise Refusal(f"Merge tests entry `{entry}` is empty")
        for word in words:
            unset = re.search(r"\$\{?([A-Za-z_]\w*)", word)
            if unset:
                raise Refusal(f"Merge tests entry `{entry}` names ${unset[1]}, which is not set")
        program = words[0]
        located = checkout / program if "/" in program else shutil.which(program)
        if located is None or not (Path(located).is_file() and os.access(located, os.X_OK)):
            raise Refusal(f"Merge tests entry `{entry}` is not a command: {program} is neither on PATH "
                          + f"nor an executable file in {checkout}")
        commands.append(tuple(words))
    return tuple(commands)


def user_bump(args: argparse.Namespace) -> bool:
    bump = cast(bool, getattr(args, "bump", False))
    if bump and any((cast(bool, args.shrink), cast(bool, args.as_built),
                     cast(str | None, getattr(args, "from_child", None)))):
        raise Refusal("--bump applies only to a code checkpoint requested by the user")
    return bump


def worktree_in_repo(row: staff.StaffRow, main_checkout: Path) -> bool:
    location = row.worktree
    if not isinstance(location, staff.PresentWorktree) or location.machine != remote.local_machine():
        return False
    try:
        return worktrees.main_checkout(location.path) == main_checkout
    except worktrees.Refused:
        return False


def read_merge_policy(production: ProductionDocument) -> MergePolicy:
    lines = production.doc.read_text(encoding="utf-8").splitlines()
    push_field = production_doc.optional_field(lines, "Push")
    push_raw = cell_value(push_field.text) if isinstance(push_field, production_doc.FieldPresent) else "validate_and_push"
    if push_raw not in ("git", "validate_and_push"):
        raise Refusal(f"unknown Push mode: {push_raw}")
    push = GitPush() if push_raw == "git" else ValidateAndPush()
    promotion = production_doc.promotion_from_doc(lines)
    if isinstance(promotion, production_doc.PromoteTo):
        for checkout in promotion.checkouts:
            if not checkout.is_dir():
                raise Refusal(f"Promote checkout {checkout} does not exist; name an existing checkout, or none")
    return MergePolicy(push, promotion, parse_merge_tests(lines, production.checkout),
                       listed_names(lines, "Known flakes"), listed_names(lines, "Known flaky tests"))


def require_merge_branch(production: ProductionDocument) -> None:
    branch = good(git(production.checkout, "branch", "--show-current"), "input")
    if branch != production.merge_branch:
        raise Refusal(f"{production.checkout}: merge checkout is on {branch or 'detached HEAD'}, "
                      + f"expected {production.merge_branch}")


def request_from(args: argparse.Namespace) -> MergeRequest:
    bump = user_bump(args)
    record = staff.snapshot()
    production = read_production(cast(str, args.production), record)
    name = cast(str, args.unit)
    resolver = session_staff.RowResolver(record)
    rows = record.find(kind="unit", parent=production.owner.id)
    selected = next((row for row in rows if row.name == unit_names.name(name)), None)
    selected = selected if selected is not None else unit_lookup.unit_row(name, record)
    try:
        main_checkout = worktrees.main_checkout(production.checkout)
    except worktrees.Refused as error:
        raise Refusal(str(error)) from error
    parent_production: Production | StaffParentIsDestination = StaffParentIsDestination()
    if selected.parent != staff.ReportsTo(production.owner.id):
        parent = record.parent_of(selected)
        if parent is None or parent.kind != "showrunner" or not worktree_in_repo(selected, main_checkout):
            raise Refusal(f"unit {name}: staff parent is {parent.name if parent else 'unavailable'}; use that production")
        parent_production = read_production(parent)
        rows.extend(record.find(kind="unit", parent=parent.id))
    if isinstance(selected.worktree, staff.RemovedWorktree):
        raise Refusal(f"unit {name} is not a staff unit of this production")
    unit = unit_from_row(selected, resolver)
    if isinstance(unit, UnitUnavailable):
        raise Refusal(f"unit {name}: {unit.reason}")
    units = (unit, *(unit_from_row(row, resolver) for row in rows
                     if row.id != selected.id and worktree_in_repo(row, main_checkout)))
    policy = read_merge_policy(production)
    if cast(bool, args.cancel_prior) and isinstance(policy.push, GitPush):
        raise Refusal("--cancel-prior applies only to Push: validate_and_push")
    if cast(bool, args.shrink) and cast(bool, args.as_built):
        raise Refusal("--shrink and --as-built are separate checkpoints")
    kind: CodeCheckpoint | ShrinkCommit | AsBuiltCommit = (
        ShrinkCommit() if cast(bool, args.shrink) else AsBuiltCommit() if cast(bool, args.as_built) else CodeCheckpoint())
    trial = cast(str | None, args.review_trial)
    if isinstance(kind, CodeCheckpoint) and not trial:
        raise Refusal("code checkpoint requires --review-trial")
    review_trial: ReviewTrial | NoReviewTrial = review_numbers(trial) if trial else NoReviewTrial()
    delivers = cast(str | None, args.delivers)
    if isinstance(kind, CodeCheckpoint) and (not delivers or not delivers.strip()):
        raise Refusal("code checkpoint requires --delivers")
    if delivers and ("\n" in delivers or "\r" in delivers):
        raise Refusal("--delivers must be one line")
    if not isinstance(kind, CodeCheckpoint) and delivers:
        raise Refusal("--delivers applies only to a code checkpoint")
    scratch = Path(cast(str | None, args.scratch) or os.environ.get("MERGE_CHECKPOINT_SCRATCH")
                   or tempfile.gettempdir()).expanduser()
    return MergeRequest(production, unit, cast(str, args.phase), cast(str, args.hash),
                        kind, policy.push, policy.promotion,
                        policy.merge_tests, policy.flakes, policy.flaky_tests, units, scratch,
                        tuple(cast(list[str], args.trailer)), review_trial, delivers or "",
                        cast(bool, args.cancel_prior), cast(str, args.started),
                        cast(str, args.regime), cast(int, args.holds), cast(int, args.merge_defects),
                        cast(str | None, args.excluded) or "", bump, record, parent_production)


class MergedCheckpoint(NamedTuple):
    unit: str
    phase: str
    commit: str


def merged_checkpoints(production: Production, *, record: staff.StaffSnapshot | None = None) -> tuple[MergedCheckpoint, ...]:
    """Git proves which recorded unit checkpoints reached the merge branch."""
    record = record if record is not None else staff.snapshot()
    units = {row.worktree.path.resolve(): row.name for row in record.find(kind="unit", parent=production.owner.id)
             if not isinstance(row.worktree, staff.WorktreeUnfilled)}
    commits = good(git(production.checkout, "rev-list", "--reverse", "--topo-order",
                       production.merge_branch), "ancestry").splitlines()
    ancestors = set(commits)
    found: dict[str, MergedCheckpoint] = {}
    for event in progress_history.events_for_worktrees(tuple(units), frozenset({"checkpoint"})):
        commit, phase = event.get("checkpoint_commit"), event.get("phase_id")
        if isinstance(commit, str) and isinstance(phase, str) and commit in ancestors:
            found[commit] = MergedCheckpoint(units[Path(str(event["working_dir"]))], phase, commit)
    return tuple(found[commit] for commit in commits if commit in found)


def fetch_origin_unit_branch(checkout: Path, branch: str, *flags: str) -> bool:
    """Fetch the unit branch if it exists on origin."""
    fetched = git(checkout, "fetch", "--no-prune", *flags, "origin", branch)
    if fetched.returncode and git(checkout, "ls-remote", "--exit-code", "origin",
                                  f"refs/heads/{branch}").returncode == 2:
        return False
    _ = good(fetched, "ancestry")
    return True


def fetch_origin_heads(checkout: Path, *flags: str) -> str:
    """Fetch the origin heads and keep tracking refs that other worktrees use."""
    fetched = git(checkout, "fetch", "--no-prune", *flags, "origin", "+refs/heads/*:refs/remotes/origin/*")
    _ = good(fetched, "ancestry")
    return fetched.stdout


def on_origin(request: MergeRequest) -> str | None:
    """Find an origin branch holding the checkpoint, trying the unit branch first."""
    checkout = request.production.checkout
    if (fetch_origin_unit_branch(checkout, request.unit.branch)
            and git(checkout, "merge-base", "--is-ancestor", request.commit, "FETCH_HEAD").returncode == 0):
        return f"origin/{request.unit.branch}"
    fetched = fetch_origin_heads(checkout, "--porcelain", "--verbose")
    advertised = {fields[2]: fields[1] for line in fetched.splitlines()
                  if line[:1] in (" ", "+", "*", "=") and len(fields := line[1:].split()) == 3
                  and fields[2].startswith("refs/remotes/origin/")}
    refs = good(git(checkout, "for-each-ref", "--contains", request.commit,
                    "--format=%(refname) %(objectname) %(symref)", "refs/remotes/origin/"), "ancestry")
    for line in refs.splitlines():
        fields = line.split()
        if len(fields) == 2 and advertised.get(fields[0]) == fields[1]:
            return fields[0].removeprefix("refs/remotes/")
    return None


def merged(request: MergeRequest) -> bool:
    """Whether the production's merge branch already contains the checkpoint."""
    return git(request.production.checkout, "merge-base", "--is-ancestor", request.commit,
               request.production.merge_branch).returncode == 0


def check_closing_ancestry(request: MergeRequest) -> None:
    checkout = request.production.checkout
    run_git = partial(git, checkout)
    _ = good(run_git("fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"), "ancestry")
    main_tip = good(run_git("rev-parse", "--verify", "refs/remotes/origin/main^{commit}"), "ancestry")
    closing = good(run_git("rev-list", "--first-parent", "--no-merges", "-n", "1", request.commit), "ancestry")
    unmerged = good(run_git("rev-list", "--reverse", "--topo-order", "--no-merges",
                            "--no-commit-header", "--format=%H %P",
                            f"{request.production.merge_branch}..{request.commit}", f"^{main_tip}"), "ancestry")
    candidates = [line.split() for line in unmerged.splitlines()]
    pending = [row for row in candidates if row[0] != closing]
    if not pending:
        return
    commit = pending[0][0]
    try:
        target = git_merge_tree.prepare_target(run_git, main_tip)
        for commit, *parents in pending:
            if not parents or not git_merge_tree.leaves_target_unchanged(
                    run_git, commit, target, merge_base=parents[0]):
                break
        else:
            return
    except OSError as error:
        raise checkpoint_gate.Stop("ancestry", "failed", f"{commit} against origin/main ({main_tip}): {error}",
                                   "the showrunner will inspect the failed command") from error
    subject = good(run_git("show", "-s", "--format=%s", commit), "ancestry")
    kind = "shrink" if isinstance(request.kind, ShrinkCommit) else "as-built"
    raise checkpoint_gate.Stop("ancestry", "held", f"unmerged commit {commit[:12]}: {subject}",
                               f"wait for {commit[:12]} to merge, then resend the {kind}")


def ancestry(request: MergeRequest) -> CheckpointOrigin:
    origin_branch = on_origin(request)
    if origin_branch is None:
        raise checkpoint_gate.Stop("ancestry", "held", "commit is not on origin",
                   "push your branch to origin and resend the checkpoint")
    already = merged(request)
    if not isinstance(request.kind, CodeCheckpoint):
        check_closing_ancestry(request)
    checkpoint_gate.report("ancestry", "ok", f"checkpoint exists and is on {origin_branch}" +
           ("; already merged" if already else ""))
    return CheckpointOrigin(origin_branch.removeprefix("origin/"), already)


def branch_is_landed(checkout: Path, tip: str, target: str) -> bool:
    """Accept an ancestor or commits with equal patches on the target."""
    ancestor = git(checkout, "merge-base", "--is-ancestor", tip, target)
    if ancestor.returncode == 0:
        return True
    if ancestor.returncode != 1:
        _ = good(ancestor, "branch")
    patches = good(git(checkout, "cherry", target, tip), "branch")
    return all(line.startswith("- ") for line in patches.splitlines())


def branch_keep_reason(request: MergeRequest, branch: str) -> str:
    """Keep main, production merge branches, and branches with a present owner."""
    if branch == "main":
        return "main is long-lived"
    if branch == request.production.merge_branch:
        return "the production merge branch is long-lived"
    record = request.staff_record if request.staff_record is not None else staff.snapshot()
    resolver = session_staff.RowResolver(record)
    for row in record.rows:
        # A producer's worktree is its launch folder; it holds no branch of this repository.
        if row.kind != "producer" and isinstance(row.worktree, staff.PresentWorktree):
            owner_branch = resolver.unit_branch(row)
            if not isinstance(owner_branch, session_staff.Branch):
                return f"{row.name}: owner branch is unreadable"
            if owner_branch.name == branch:
                return f"{row.name} has a present worktree on this branch"
        if row.kind == "showrunner" and isinstance(row.doc, (staff.LiveDoc, staff.WrappedDoc)):
            doc = resolver.production_doc(row)
            if not isinstance(doc, session_staff.ProductionDoc):
                return f"{row.name}: {doc.reason}"
            if production_field(doc.text.splitlines(), "Merge branch") == branch:
                return f"{row.name} uses this production merge branch"
    return ""


def delete_landed_branch(request: MergeRequest, branch: str, tip: str) -> None:
    """Delete only this request's unowned origin branch after main receives the merge."""
    if isinstance(request.promotion, production_doc.NoPromotion):
        return
    try:
        if reason := branch_keep_reason(request, branch):
            checkpoint_gate.report("branch", "kept", f"origin/{branch}: {reason}")
            return
        checkout = request.production.checkout
        ref = f"refs/remotes/origin/{branch}"
        _ = good(git(checkout, "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main",
                     f"+refs/heads/{branch}:{ref}"), "branch")
        if git(checkout, "merge-base", "--is-ancestor", tip, "origin/main").returncode:
            checkpoint_gate.report("branch", "kept", f"origin/{branch}: this merge has not reached main")
            return
        branch_tip = good(git(checkout, "rev-parse", ref), "branch")
        if not branch_is_landed(checkout, branch_tip, "origin/main"):
            checkpoint_gate.report("branch", "kept", f"origin/{branch}: work has not reached main")
            return
        deleted = git(checkout, "push", f"--force-with-lease=refs/heads/{branch}:{branch_tip}",
                      "origin", "--delete", branch)
        if deleted.returncode:
            checkpoint_gate.report("branch", "kept",
                                   f"origin/{branch}: delete failed or branch moved; {failure_detail(deleted)}".replace("\n", "; "))
        else:
            checkpoint_gate.report("branch", "deleted", f"origin/{branch}")
    except (checkpoint_gate.Stop, OSError, ValueError, Refusal, staff.StaffRefused) as error:
        checkpoint_gate.report("branch", "kept", f"origin/{branch}: {error}".replace("\n", "; "))


def change_review_commits(request: MergeRequest) -> list[change_review.Commit]:
    """Check review lines and count selected commits with valid lines."""
    previous = request.production.merge_branch
    try:
        commits = change_review.unit_commits(request.production.checkout, request.commit, previous,
                                             request.production.merge_branch)
    except change_review.ChangeReviewError as error:
        raise checkpoint_gate.Stop("change review", "failed", str(error), "the showrunner will inspect the failed command") from error
    if refused := change_review.missing(commits):
        raise checkpoint_gate.Stop("change review", "held", "; ".join(refused), change_review.HELD_NEXT_STEP)
    selected = [commit for commit in change_review.review_commits(commits)
                if (lines := change_review.entries(commit.message))
                and not any(isinstance(line, change_review.Malformed) for line in lines)]
    checkpoint_gate.report("change review", "ok", f"{len(selected)} unit commit(s) carry their {change_review.TRAILER} lines")
    return commits


def checkpoint_step_record(checkout: Path, merge_head: str, step: str) -> Path:
    """Completion belongs to this merge, not to a session's disposable scratch."""
    path = good(git(checkout, "rev-parse", "--git-path", f"checkpoint-completed/{merge_head}/{step}"), step)
    return checkout / path


def record_publication_changes(production: ProductionDocument, *, report_unchanged: bool = True,
                               record: staff.StaffSnapshot | None = None,
                               review_docs: tuple[Path, ...] = (),
                               known_producer: change_review.KnownMergeProducer | change_review.UnknownProducer = change_review.UNKNOWN_PRODUCER) -> None:
    """Scan the destination branch after promotion and route rows by producer."""
    doc = next((path for source in review_docs or (production.doc,)
                if (path := change_review.list_path(source, record=record)) is not None), None)
    if doc is None:
        if report_unchanged:
            checkpoint_gate.report("change review", "ok", "the production doc has no Change review line; no rows written")
        return
    failure = ""
    warning = ""
    try:
        rows = change_review.list_ready_rows(production.checkout, production.doc, production.merge_branch, record=record,
                                             known_producer=known_producer)
    except change_review.ReviewListDurabilityUnconfirmed as error:
        rows = list(error.written_rows)
        warning = f"{error.list_path}: durability unconfirmed after replacement ({error})"
    except change_review.ReviewListWriteFailed as error:
        rows = list(error.written_rows)
        failure = f"{error.list_path} not written: {error}; retry after the repair"
    except (change_review.ChangeReviewError, OSError) as error:
        checkpoint_gate.report("change review", "failed", f"{doc} not written: {error}; retry after the repair")
        return
    rows_by_list: dict[Path, list[str]] = {}
    for row in rows:
        rows_by_list.setdefault(row.list_path, []).append(row.markdown)
    if not rows and not failure and not warning and report_unchanged:
        rows_by_list[doc] = []
    for path, written in rows_by_list.items():
        checkpoint_gate.report("change review", "ok",
                               f"{len(written)} row(s) added to {path}" + "".join(f"; {row}" for row in written))
    if warning:
        checkpoint_gate.report("change review", "warning", warning)
    if failure:
        checkpoint_gate.report("change review", "failed", failure)


def merge_paths(checkout: Path, base: str, tip: str) -> tuple[str, ...]:
    """The paths a merge of `tip` into `base` changes. A three-dot diff picks one merge base, so after a
    criss-cross merge it also lists paths `base` already holds; the merged tree does not. A conflicted
    merge still writes a tree, with the conflicted paths in it, and the conflicts step holds that merge."""
    tree = git(checkout, "merge-tree", "--write-tree", base, tip)
    if tree.returncode not in (0, 1):
        _ = good(tree, "scope")
    output = good(git(checkout, "diff", "--name-only", base, tree.stdout.split("\n", 1)[0].strip()), "scope")
    return tuple(line for line in output.splitlines() if line)


def changed_paths(request: MergeRequest, base: str = "") -> tuple[str, ...]:
    """The paths the checkpoint changes from `base`, by default the merge branch's tip."""
    return merge_paths(request.production.checkout, base or request.production.merge_branch, request.commit)


def _origin_merge_refs(request: MergeRequest) -> list[str]:
    return list(dict.fromkeys([
        f"origin/{request.production.merge_branch}",
        *change_review.reviewed_merge_refs(request.production.checkout, change_review.registered_productions),
    ]))


def _unit_paths(request: MergeRequest, paths: tuple[str, ...]) -> tuple[str, ...]:
    """Keep paths absent at an origin merge base or changed from every base that contains them."""
    if not paths:
        return ()
    own = set(paths)
    for ref in _origin_merge_refs(request):
        try:
            base = git(request.production.checkout, "merge-base", ref, request.commit)
            if base.returncode:
                continue
            changed = git(request.production.checkout, "diff", "--name-only", "--no-renames", "-z",
                          base.stdout.strip(), request.commit, "--", *paths)
            present = git(request.production.checkout, "ls-tree", "--name-only", "-r", "-z",
                          base.stdout.strip(), "--", *paths)
            if changed.returncode or present.returncode:
                continue
        except OSError:
            continue
        unchanged = set(present.stdout.split("\0")) - set(changed.stdout.split("\0"))
        own.difference_update(unchanged)
    return tuple(path for path in paths if path in own)


def checkpoint_plan(request: MergeRequest) -> Path:
    """Use a closing item's subject slug or retain the configured plan."""
    plan = request.unit.plan
    configured_path = (plan if isinstance(plan, Path) else
                       Path("standby" if isinstance(plan, unit_config.StandbyPlan) else "permanent"))
    if isinstance(request.kind, CodeCheckpoint):
        return configured_path
    subject = good(git(request.production.checkout, "show", "-s", "--format=%s", request.commit), "scope")
    pattern = (r"shrink\(([^()/\\]+)\): phase \S+ — .+" if isinstance(request.kind, ShrinkCommit)
               else r"docs\(([^()/\\]+)\): as-built")
    match = re.fullmatch(pattern, subject)
    if match and (isinstance(request.kind, AsBuiltCommit) or not isinstance(plan, Path)):
        return Path("docs/plans") / (match[1] + ".md")
    return configured_path.with_name(match[1] + ".md") if match else configured_path


def _shrink_leaves_main_unchanged(request: MergeRequest) -> bool:
    run_git = partial(git, request.production.checkout)
    _, *parents = good(run_git("rev-list", "--parents", "-n", "1", request.commit), "scope").split()
    if not parents:
        return False
    try:
        return git_merge_tree.leaves_target_unchanged(
            run_git, request.commit, "refs/remotes/origin/main", merge_base=parents[0])
    except OSError as error:
        raise checkpoint_gate.Stop("scope", "failed", f"{request.commit} against origin/main: {error}",
                                   "the showrunner will inspect the failed command") from error


def scope(request: MergeRequest, paths: tuple[str, ...]) -> tuple[str, ...]:
    staged = good(git(request.production.checkout, "diff", "--cached", "--name-only"), "scope")
    if staged:
        raise checkpoint_gate.Stop("scope", "held", f"staged paths in merge checkout: {staged.replace(chr(10), ', ')}",
                   "the showrunner will clear unrelated staged paths, then rerun the checkpoint")
    own_paths = _unit_paths(request, paths)
    imported = len(paths) - len(own_paths)
    paths = own_paths
    if isinstance(request.kind, ShrinkCommit):
        plan = checkpoint_plan(request).as_posix()
        next_items = str(Path(plan).with_name(Path(plan).stem + "-next.md"))
        unexpected = [path for path in paths if path not in (plan, next_items)]
        if unexpected or (plan not in paths and (paths or not _shrink_leaves_main_unchanged(request))):
            raise checkpoint_gate.Stop("scope", "held", f"shrink paths must be {plan} and optional {next_items}; got {', '.join(paths)}",
                       "commit only the plan doc and its next-items file, then resend the shrink")
    elif isinstance(request.kind, AsBuiltCommit):
        plan = checkpoint_plan(request).as_posix()
        unexpected = [path for path in paths if not path.endswith(".md")]
        if unexpected or plan not in paths:
            plan_exists = bool(good(git(request.production.checkout, "ls-tree", "--name-only",
                                        request.production.merge_branch, "--", plan), "scope"))
            if plan_exists or unexpected or any(PurePosixPath(path).parent != PurePosixPath("docs/as-built")
                                                for path in paths):
                rule = (f"an as-built commit carries only docs and must change or delete {plan}; " if plan_exists
                        else f"an as-built commit without {plan} on the merge branch allows only docs/as-built/*.md; ")
                raise checkpoint_gate.Stop("scope", "held", rule
                           + f"got {', '.join(paths)}",
                           "commit only the as-built docs" + (" and the plan's deletion" if plan_exists else "")
                           + ", then resend the as-built")
    checkpoint_gate.report("scope", "ok", f"{len(paths)} changed paths"
           + (f"; {imported} came from origin merge branches" if imported else ""))
    return own_paths


def conflicts(request: MergeRequest) -> None:
    result = git(request.production.checkout, "merge-tree", "--write-tree", "--name-only",
                 request.production.merge_branch, request.commit)
    if result.returncode:
        if result.returncode == 1:
            raise checkpoint_gate.Stop("conflicts", "held", "conflict: " + failure_detail(result),
                       "merge the merge branch on your unit branch, resolve conflicts, and send a new hash")
        raise checkpoint_gate.Stop("conflicts", "failed", result.stderr.strip() or f"merge-tree exit {result.returncode}",
                   "the showrunner will inspect the merge-tree failure")
    checkpoint_gate.report("conflicts", "ok", "merge tree is clean")


def other_units(request: MergeRequest, paths: tuple[str, ...]) -> None:
    if len(request.units) == 1:
        checkpoint_gate.report("other units", "ok", "no other units")
        return
    for unit in request.units:
        if unit == request.unit:
            continue
        if isinstance(unit, UnitUnavailable):
            checkpoint_gate.report("other units", "unavailable", f"{unit.name}: {unit.reason}")
            continue
        diff = git(request.production.checkout, "diff", "--name-only",
                   f"{request.production.merge_branch}...{unit.branch}")
        branch_paths: set[str] = set(diff.stdout.splitlines()) if diff.returncode == 0 else set()
        status = git(unit.worktree, "status", "--short")
        worktree_paths = {line[3:] for line in status.stdout.splitlines() if len(line) >= 4}
        overlap = sorted(set(paths) & (branch_paths | worktree_paths))
        checkpoint_gate.report("other units", "ok", f"{unit.name}: {', '.join(overlap) if overlap else 'no overlap'}")


def into(path: PurePath, branch: str, why: str, unblocks: str) -> None:
    print(f"into: {path} ({branch}) — {why}; unblocks: {unblocks}", flush=True)


def checkpoint_merge_target(commit: str, branch: str) -> str:
    return f" ({commit[:7]}) into {branch}"


def merge_subject(request: MergeRequest) -> str:
    kind = (" shrink" if isinstance(request.kind, ShrinkCommit) else " as-built" if isinstance(request.kind, AsBuiltCommit)
            else "")
    return (f"Merge {request.unit.name} phase {request.phase}{kind}"
            + checkpoint_merge_target(request.commit, request.production.merge_branch))


def merged_checkpoint(request: MergeRequest) -> str:
    return merge_evidence(request.production, request.commit)


@dataclass
class ResetBatchCheckout:
    production: ProductionDocument
    start_tip: str
    expected_tip: str
    published_tip: str
    record: BatchResumeRecord
    dropped_commit: str = ""

    def finish_merge(self, tip: str) -> None:
        self.expected_tip = tip
        self.record.store_merge_state(NO_BATCH_MERGE)


class KeepBatchCheckout(NamedTuple):
    pass


KEEP_BATCH_CHECKOUT = KeepBatchCheckout()


def merge(request: MergeRequest, *, batch_checkout: ResetBatchCheckout | KeepBatchCheckout = KEEP_BATCH_CHECKOUT) -> str:
    checkout = request.production.checkout
    staged = good(git(checkout, "diff", "--cached", "--name-only"), "merge")
    if staged:
        raise checkpoint_gate.Stop("merge", "held", f"staged paths in merge checkout: {staged.replace(chr(10), ', ')}",
                   "the showrunner will clear unrelated staged paths, then rerun the checkpoint")
    short = request.commit[:7]
    message = merge_subject(request) + "\n\n" + ("Phase plan shrink." if isinstance(request.kind, ShrinkCommit)
                                                  else "Closing as-built docs." if isinstance(request.kind, AsBuiltCommit)
                                                  else request.delivers)
    if request.trailer:
        message += "\n\n" + "\n".join(request.trailer)
    with build_folders.tool_folder("merge-message") as directory:
        message_path = Path(directory) / f"merge_{short}.msg"
        _ = message_path.write_text(message + "\n", encoding="utf-8")
        result = run_merge_command(checkout, batch_checkout, "merge", request.commit,
                                   "--no-ff", "-q", "-F", str(message_path))
    if result.returncode:
        raise checkpoint_gate.Stop("merge", "failed", result.stderr.strip() or "merge failed",
                   "the showrunner will inspect the merge failure")
    head = good(git(checkout, "rev-parse", "HEAD"), "merge")
    if isinstance(batch_checkout, ResetBatchCheckout):
        batch_checkout.finish_merge(head)
    change_review.record_merge_producer(change_review.KnownMergeProducer(head, request.unit.staff_id))
    checkpoint_gate.report("merge", "ok", f"merged as {head}")
    return head


def undo_unpushed_merge(production: ProductionDocument, merge_head: str, before_merge_tip: str = "") -> None:
    checkout = production.checkout
    head = good(git(checkout, "rev-parse", "HEAD"), "red")
    if head != merge_head:
        raise checkpoint_gate.Stop("red", "failed", f"HEAD moved from unpushed merge {merge_head}",
                   "the showrunner will inspect the merge branch")
    if git(checkout, "merge-base", "--is-ancestor", head,
           f"origin/{production.merge_branch}").returncode == 0:
        raise checkpoint_gate.Stop("red", "failed", "merge is already pushed; cannot reset it",
                   "the showrunner will inspect the merge branch")
    _ = good(git(checkout, "reset", "--keep", before_merge_tip or f"{merge_head}^1"), "red")


def tests(request: MergeRequest, paths: tuple[str, ...], merge_head: str) -> str:
    if not isinstance(request.kind, CodeCheckpoint):
        checkpoint_gate.report("test", "ok", f"{'shrink' if isinstance(request.kind, ShrinkCommit) else 'as-built'} has no package tests")
        return "no tests"
    checkout = request.production.checkout
    test_tip = good(git(checkout, "rev-parse", "HEAD"), "test")
    before_merge_tip = good(git(checkout, "rev-parse", f"{merge_head}^1"), "test")
    return checkpoint_tests(request.production, paths, request.commit, request.merge_tests,
                            request.flakes, request.scratch, test_tip, request.flaky_tests,
                            before_merge_tip=before_merge_tip, ledger=request.parent_production)


def checkpoint_tests(production: ProductionDocument, paths: tuple[str, ...], checkpoint: str,
                     merge_tests: tuple[tuple[str, ...], ...], flakes: frozenset[str], scratch: Path,
                     merge_head: str, flaky_tests: frozenset[str], *, before_merge_tip: str = "",
                     ledger: ProductionDocument | None = None) -> str:
    scratch.mkdir(parents=True, exist_ok=True)
    log = scratch / f"merge_{checkpoint[:7]}_test.log"
    _ = log.write_text("", encoding="utf-8")
    # A resumed merge resets to a pushed tip that holds the checkpoint, so that tip is no base to compare.
    has_base = bool(before_merge_tip) and git(production.checkout, "merge-base", "--is-ancestor",
                                                checkpoint, before_merge_tip).returncode == 1
    return run_checkpoint_tests(production, paths, merge_tests, flakes, flaky_tests, log, merge_head, before_merge_tip,
                                base_tip=before_merge_tip if has_base else "", ledger=ledger)


def run_checkpoint_tests(production: ProductionDocument, paths: tuple[str, ...],
                         merge_tests: tuple[tuple[str, ...], ...], flakes: frozenset[str], flaky_tests: frozenset[str],
                         log: Path, merge_head: str, before_merge_tip: str, *, base_tip: str,
                         ledger: ProductionDocument | None = None) -> str:
    """Append test output and undo an unpublished red checkpoint.

    With a base tip, a red is checked on that tip with main merged; the unit hears only of its own red.
    """
    try:
        return checkpoint_gate.run_checkpoint_test_steps(production, paths, merge_tests, flakes, flaky_tests, log,
                                                         ledger=ledger)
    except checkpoint_gate.RedCheckpointTests as error:
        undo_unpushed_merge(production, merge_head, before_merge_tip)
        if not base_tip:
            raise
        raise base_red_check(production, error, log, base_tip, paths, merge_tests) from error
    except checkpoint_gate.Stop as error:
        if error.step == "red" and error.state == "failed":
            undo_unpushed_merge(production, merge_head, before_merge_tip)
        raise


def base_red_check(production: ProductionDocument, red: checkpoint_gate.RedCheckpointTests, log: Path,
                   base_tip: str, paths: tuple[str, ...], merge_tests: tuple[tuple[str, ...], ...]) -> checkpoint_gate.Stop:
    """Run the red commands on the base tip with main merged, and return the stop that the red earns.

    Main merges as it did in the tested tree. A main conflict leaves the red as the unit's red.
    """
    checkout = production.checkout
    main = git(checkout, "rev-parse", "-q", "--verify", "refs/remotes/origin/main")
    try:
        if main.returncode == 0:
            merge_main_commit(production, main.stdout.strip())
        base = good(git(checkout, "rev-parse", "HEAD"), "red")
        split = checkpoint_gate.split_base_red(red, checkpoint_gate.test_steps(production, paths, merge_tests), log)
    except MainConflict:
        return red
    finally:
        _ = good(git(checkout, "reset", "--keep", base_tip), "red")
    if not split.base:
        return red
    base_labels = checkpoint_gate.red_labels(split.base)
    print(f"merge director: {base_labels} red on base {base} without the checkpoint; "
          + f"repair the base, then run the checkpoint again; test log: {log}", flush=True)
    if split.unit:
        return checkpoint_gate.unit_red(split.unit, log)
    return checkpoint_gate.Stop("red", "held", f"{base_labels} red on base {base}; log {log}",
                                "your checkpoint adds no red; the merge director repairs the base red "
                                + "and runs your checkpoint again, so send nothing")


def undo_failed_push(production: ProductionDocument, old_origin: str, before_merge_tip: str) -> bool:
    """Return whether Git restored the original tip."""
    checkout = production.checkout
    if git(checkout, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0:
        _ = good(git(checkout, "merge", "--abort"), "push")
    if old_origin and not push_origin_matches(production, old_origin):
        return False
    _ = good(git(checkout, "reset", "--keep", before_merge_tip), "push")
    return True


def push_origin_matches(production: ProductionDocument, *accepted_tips: str) -> bool:
    remote = good(git(production.checkout, "ls-remote", "origin", f"refs/heads/{production.merge_branch}"), "push")
    fields = remote.split(maxsplit=1)
    return bool(fields) and fields[0] in accepted_tips


def merge_main(production: ProductionDocument, *,
               batch_checkout: ResetBatchCheckout | KeepBatchCheckout = KEEP_BATCH_CHECKOUT) -> None:
    """Integrate main before the checkpoint selects and runs its tests."""
    checkout = production.checkout
    _ = good(git(checkout, "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"), "push")
    main = git(checkout, "rev-parse", "refs/remotes/origin/main")
    if main.returncode == 0:
        merge_main_commit(production, main.stdout.strip(), batch_checkout=batch_checkout)


class OpenMerge(NamedTuple):
    targets: tuple[str, ...]


class NoOpenMerge(NamedTuple):
    pass


MERGE_TARGET_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
OPEN_MERGE_ERROR = "You have not concluded your merge (MERGE_HEAD exists)."


def merge_head_path(checkout: Path) -> Path:
    return Path(good(git(checkout, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD"), "merge"))


def read_open_merge(path: Path) -> OpenMerge | NoOpenMerge:
    try:
        targets = path.read_text(encoding="ascii").split()
    except FileNotFoundError:
        return NoOpenMerge()
    except (OSError, UnicodeError):
        return OpenMerge(())
    return OpenMerge(tuple(targets))


def require_closed_batch_merge(checkout: Path, batch_checkout: ResetBatchCheckout | KeepBatchCheckout) -> None:
    if isinstance(batch_checkout, ResetBatchCheckout) and isinstance(
            read_open_merge(merge_head_path(checkout)), OpenMerge):
        raise checkpoint_gate.Stop("push", "held", OPEN_MERGE_ERROR,
                                   "the showrunner will inspect the merge branch")


def run_merge_command(checkout: Path, batch_checkout: ResetBatchCheckout | KeepBatchCheckout,
                      step: str, target: str, *options: str) -> subprocess.CompletedProcess[str]:
    args = ("merge", *options, target)
    if isinstance(batch_checkout, KeepBatchCheckout):
        return git(checkout, *args)
    merge_head = merge_head_path(checkout)
    if isinstance(read_open_merge(merge_head), OpenMerge):
        return subprocess.CompletedProcess(args, 1, "", OPEN_MERGE_ERROR)
    batch_checkout.record.store_merge_state(InFlightBatchMerge(target))
    result = git(checkout, *args)
    open_merge = read_open_merge(merge_head)
    if isinstance(open_merge, OpenMerge) and not result.returncode:
        raise checkpoint_gate.Stop(step, "failed", "git merge left MERGE_HEAD open",
                                   "the showrunner will inspect the merge options")
    return result


class MainConflict(checkpoint_gate.Stop):
    """A conflict between main and the unpublished merge tip."""

    def __init__(self, paths: tuple[str, ...], error: str) -> None:
        super().__init__("push", "held", f"origin/main conflicts: {', '.join(paths)}; {error}",
                         "merge origin/main into the merge branch, resolve the conflict, and run the push again")


def merge_main_commit(production: ProductionDocument, main_commit: str, *,
                      batch_checkout: ResetBatchCheckout | KeepBatchCheckout = KEEP_BATCH_CHECKOUT) -> None:
    """Integrate the given main commit without fetching another tip."""
    checkout = production.checkout
    if git(checkout, "merge-base", "--is-ancestor", main_commit, "HEAD").returncode == 0:
        require_closed_batch_merge(checkout, batch_checkout)
        return
    merged = run_merge_command(checkout, batch_checkout, "push", main_commit, "--no-ff", "-m",
                               "Merge origin/main into " + production.merge_branch)
    if merged.returncode:
        try:
            conflicts = git(checkout, "--no-optional-locks", "diff", "--name-only", "--diff-filter=U")
        except (OSError, ValueError) as error:
            if isinstance(batch_checkout, KeepBatchCheckout):
                raise
            raise checkpoint_gate.Stop("push", "failed", failure_detail(merged),
                                       "the showrunner will inspect the failed command") from error
        if isinstance(batch_checkout, KeepBatchCheckout):
            _ = good(conflicts, "push")
            _ = good(git(checkout, "merge", "--abort"), "push")
        if conflicts.returncode == 0 and conflicts.stdout.strip():
            raise MainConflict(tuple(conflicts.stdout.strip().splitlines()), failure_detail(merged))
        _ = good(merged, "push")
    if isinstance(batch_checkout, ResetBatchCheckout):
        batch_checkout.finish_merge(good(git(checkout, "rev-parse", "HEAD"), "test"))


@contextmanager
def push_rollback(production: ProductionDocument, before_merge_tip: str, *,
                  batch_checkout: ResetBatchCheckout | KeepBatchCheckout = KEEP_BATCH_CHECKOUT) -> Generator[None]:
    """Restore the original tip if preparation or publication fails before the push."""
    ref = f"refs/remotes/origin/{production.merge_branch}"
    old_origin = ""
    try:
        _ = good(git(production.checkout, "fetch", "origin", f"+refs/heads/{production.merge_branch}:{ref}"), "push")
        old_origin = good(git(production.checkout, "rev-parse", ref), "push")
        if isinstance(batch_checkout, ResetBatchCheckout):
            batch_checkout.published_tip = old_origin
        yield
    except (checkpoint_gate.Stop, OSError, ValueError) as error:
        if before_merge_tip and isinstance(batch_checkout, KeepBatchCheckout):
            try:
                rollback_completed = undo_failed_push(production, old_origin, before_merge_tip)
            except (checkpoint_gate.Stop, OSError, ValueError) as rollback:
                if isinstance(error, ValidationPushFailed):
                    detail = " ".join(str(rollback).splitlines())
                    raise ValidationPushFailed(f"{error.detail} — rollback failed: {detail}", frozenset()) from rollback
                raise
            if isinstance(error, ValidationPushFailed):
                error.rollback_completed = rollback_completed
        raise


def prepare_checkpoint(production: ProductionDocument, merge_head: str, *, published_tip: str = "",
                       batch_checkout: ResetBatchCheckout | KeepBatchCheckout = KEEP_BATCH_CHECKOUT) -> tuple[str, ...]:
    """Integrate main and select all paths that need checkpoint tests."""
    checkout = production.checkout
    before_merge_tip = good(git(checkout, "rev-parse", f"{merge_head}^1"), "test")
    with push_rollback(production, before_merge_tip, batch_checkout=batch_checkout):
        merge_main(production, batch_checkout=batch_checkout)
    output = good(git(checkout, "diff", "--name-only", published_tip or before_merge_tip, "HEAD"), "test")
    return tuple(output.splitlines())


def push_git(production: ProductionDocument, may_undo: bool) -> str:
    checkout = production.checkout
    before_merge_tip = good(git(checkout, "rev-parse", "HEAD^1"), "push") if may_undo else ""
    with push_rollback(production, before_merge_tip):
        tip = good(git(checkout, "rev-parse", "HEAD"), "push")
        _ = good(git(checkout, "push", "origin", production.merge_branch), "push")
    checkpoint_gate.report("push", "ok", f"{production.merge_branch} pushed at {tip}")
    end_pushed_worktrees(production)
    return tip


def settings_commits_only(checkout: Path, main_commit: str, tested_tip: str) -> bool:
    """Whether each main commit that the tested tip lacks is a settings commit.

    A settings commit has one parent, a settings subject (main_publish.SETTINGS_SUBJECTS),
    and changes only config/ paths.
    """
    listed = good(git(checkout, "log", "--format=%x01%P%x00%s", "--name-only", main_commit, f"^{tested_tip}"), "push")
    commits = [entry.strip("\n").split("\n") for entry in listed.split("\x01") if entry.strip()]
    for header, *paths in commits:
        parents, _, subject = header.partition("\0")
        changed = [path for path in paths if path]
        if (len(parents.split()) != 1 or not subject.startswith(main_publish.SETTINGS_SUBJECTS)
                or not changed or not all(path.startswith("config/") for path in changed)):
            return False
    return bool(commits)


@contextmanager
def tested_main_push(production: ProductionDocument, tested_tip: str,
                     retest: Callable[[tuple[str, ...], str], None], before_merge_tip: str,
                     *, follow_main: bool = True,
                     batch_checkout: ResetBatchCheckout | KeepBatchCheckout = KEEP_BATCH_CHECKOUT) -> Generator[str]:
    """Hold the main lock through retests, the push, and main promotion.

    A main move made only of settings commits merges with no retest (nate, 2026-11-14).
    """
    checkout = production.checkout
    with ExitStack() as publication_lock:
        with push_rollback(production, before_merge_tip, batch_checkout=batch_checkout):
            _ = publication_lock.enter_context(
                main_push_lock.main_push_lock(checkout, "merge_checkpoint.tested_main_push"))
            while follow_main:
                _ = good(git(checkout, "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"), "push")
                main_commit = good(git(checkout, "rev-parse", "refs/remotes/origin/main"), "push")
                if git(checkout, "merge-base", "--is-ancestor", main_commit, tested_tip).returncode == 0:
                    break
                settings_only = settings_commits_only(checkout, main_commit, tested_tip)
                merge_main_commit(production, main_commit, batch_checkout=batch_checkout)
                tip = good(git(checkout, "rev-parse", "HEAD"), "test")
                if isinstance(batch_checkout, ResetBatchCheckout):
                    batch_checkout.expected_tip = tip
                if settings_only:
                    checkpoint_gate.report("test", "ok", f"main moved only by settings commits; merged at {tip} with no retest")
                else:
                    paths = tuple(good(git(checkout, "diff", "--name-only", tested_tip, tip), "test").splitlines())
                    retest(paths, tip)
                tested_tip = tip
            require_closed_batch_merge(checkout, batch_checkout)
            if isinstance(batch_checkout, ResetBatchCheckout):
                require_drop_absent(batch_checkout, tested_tip)
        yield tested_tip


def refresh_pushed_tip(production: ProductionDocument, tip: str) -> str:
    """Test new main changes before an interrupted promotion publishes them."""
    lines = production.doc.read_text().splitlines()

    def retest(paths: tuple[str, ...], test_tip: str) -> None:
        _ = checkpoint_tests(production, paths, tip, parse_merge_tests(lines, production.checkout),
                             listed_names(lines, "Known flakes"),
                             Path(os.environ.get("MERGE_CHECKPOINT_SCRATCH") or tempfile.gettempdir()),
                             test_tip, listed_names(lines, "Known flaky tests"), before_merge_tip=tip)

    with tested_main_push(production, tip, retest, tip) as tested_tip:
        return push_git(production, False) if tested_tip != tip else tip


def pushed_state(production: ProductionDocument, checkpoint: str) -> PushedTip | NotYetPushed:
    checkout = production.checkout
    branch = production.merge_branch
    remote = f"refs/remotes/origin/{branch}"
    _ = good(git(checkout, "fetch", "origin", f"+refs/heads/{branch}:{remote}"), "push")
    if git(checkout, "merge-base", "--is-ancestor", checkpoint, remote).returncode:
        return NotYetPushed()
    return PushedTip(good(git(checkout, "rev-parse", remote), "push"))


class ValidationPushFailed(checkpoint_gate.Stop):
    """A failed validation push with the files named in its diagnostics."""

    def __init__(self, detail: str, paths: frozenset[Path]) -> None:
        super().__init__("push", "failed", detail, "the showrunner will inspect the failed command")
        self.paths: frozenset[Path] = paths
        self.rollback_completed: bool = False


def validation_push_failure(checkout: Path, log: Path, status: int,
                            output: Iterable[str]) -> ValidationPushFailed:
    """Read the diagnostics from the failed step of this attempt."""
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    step_banner = re.compile(r"=== STEP: (.+) ===")
    step = ""
    lines: list[str] = []
    for raw in output:
        line = ansi.sub("", raw).strip()
        if banner := step_banner.fullmatch(line):
            step = banner[1]
            lines.clear()
        else:
            lines.append(line)

    refusal = next((line for line in lines if line.startswith("!!! Cannot validate")), "")
    marker = re.compile(r"!!! VALIDATION FAILED at step: (.+) !!!")
    failure_marker = next((line for line in reversed(lines)
                           if (match := marker.fullmatch(line)) and (not step or match[1] == step)), "")
    mend_warning = bool(failure_marker) and step in ("cargo-mend", "cargo-mend (in-repo build)")
    file_error = re.compile(r"(.+?)(?::\d+){0,2}:\s+(?:-\s+)?(?:fatal )?error(?:\[\w+\])?:", re.IGNORECASE)
    error_header = re.compile(r"(?:remote: )?(?:fatal|(?:fatal )?error(?:\[\w+\])?):", re.IGNORECASE)
    primary_span = re.compile(r"(?:-->|┌─)\s+(.+):\d+:\d+$")
    missing_module = re.compile(r"Error writing files: failed to resolve mod `[^`]+`: (.+) does not exist")
    paths: set[Path] = set()
    first_error = ""
    span_header = ""
    for line in lines:
        is_error_header = error_header.match(line) is not None
        is_mend_warning = mend_warning and line.startswith("warning:")
        if location := missing_module.fullmatch(line):
            file = Path(os.path.normpath(checkout / location[1]))
            paths.add(file)
            if not first_error:
                display_path = file.relative_to(checkout) if file.is_relative_to(checkout) else file
                first_error = f"{line[:location.start(1)]}{display_path}{line[location.end(1):]}"
        elif is_error_header or line.startswith("Error writing files:") or line == "merge branch moved during validation":
            first_error = first_error or line
        elif not is_mend_warning and (location := file_error.match(line)):
            first_error = first_error or line
            paths.add(Path(os.path.normpath(checkout / location[1])))
        elif span_header and (location := primary_span.fullmatch(line)):
            first_error = first_error or span_header
            paths.add(Path(os.path.normpath(checkout / location[1])))
        span_header = line if is_error_header or is_mend_warning else ""
    if step.startswith("direct push") or refusal:
        paths.clear()
    cause = " ".join(part for part in (failure_marker or refusal, first_error) if part)
    cause = cause or f"no clear failure line (exit {status})"
    if len(cause) > 300:
        cause = cause[:297] + "..."
    return ValidationPushFailed(f"{cause} — log {log}", frozenset(paths))


def push_validate(production: ProductionDocument, fix_commit: str, may_undo: bool, log: Path,
                  cancel_prior: bool = False) -> str:
    script = checkpoint_gate.tool("validate_and_push.sh", Path.home() / ".claude/scripts/validate_and_push/validate_and_push.sh",
                  "MERGE_CHECKPOINT_VALIDATE_AND_PUSH")
    command = [script, "--quick", "--to", production.merge_branch,
               "--fix-commit", fix_commit]
    if cancel_prior:
        command.append("--cancel-prior")
    before_merge_tip = good(git(production.checkout, "rev-parse", "HEAD^1"), "push") if may_undo else ""
    with push_rollback(production, before_merge_tip):
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a+", encoding="utf-8", errors="replace") as output:
            _ = output.write("\n=== validate_and_push.sh ===\n")
            output.flush()
            attempt = output.tell()
            with subprocess.Popen(command, cwd=production.checkout,
                                  env=main_push_lock.held_environment(production.checkout),
                                  stdout=output, stderr=subprocess.STDOUT) as process:
                status = process.wait()
            if status:
                _ = output.seek(attempt)
                raise validation_push_failure(production.checkout, log, status, output)
    tip = good(git(production.checkout, "rev-parse", "HEAD"), "push")
    checkpoint_gate.report("push", "ok", f"validated and pushed {tip}")
    end_pushed_worktrees(production)
    return tip


def promote_local_checkout(checkout: Path, merge_branch: str, tip: str) -> (
        PromotionAlreadyAtTip | PromotionOriginPushed | PromotionAdvanced | PromotionNotPushed):
    """Fast-forward one main checkout and push any eligible unpushed tip."""
    with main_push_lock.main_push_lock(checkout):
        _ = good(git(checkout, "fetch", "origin", f"+refs/heads/{merge_branch}:refs/remotes/origin/{merge_branch}"), "promote")
        current_branch = good(git(checkout, "branch", "--show-current"), "promote")
        if current_branch != "main":
            raise checkpoint_gate.Stop("promote", "failed", f"{checkout} is on {current_branch}, expected main",
                       "switch the promotion checkout to main and rerun this checkpoint")
        current = good(git(checkout, "rev-parse", "main"), "promote")
        _ = good(git(checkout, "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"), "promote")
        if current == tip:
            unpushed = good(git(checkout, "rev-list", "refs/remotes/origin/main..main"), "promote")
            foreign = good(git(checkout, "rev-list", "main", "^refs/remotes/origin/main", f"^refs/remotes/origin/{merge_branch}"),
                           "promote")
            if foreign:
                return PromotionNotPushed(tip, True)
            try:
                landed = land_main.land(checkout, current)
            except land_main.LandError as error:
                raise checkpoint_gate.Stop("promote", "failed", str(error), "repair the landing and rerun this checkpoint") from error
            checkpoint_gate.report("post-push", "warning" if isinstance(landed.hook, land_main.HookFailed) else "ok",
                                   land_main.hook_report(landed.hook))
            if unpushed:
                return PromotionOriginPushed(tip)
            return PromotionAlreadyAtTip(tip)
        if conflict := gate.promotion_edit_conflict(checkout, tip, waiting_rows.WAITING_PATHS):
            raise checkpoint_gate.Stop("promote", "failed", conflict,
                       "clear the listed checkout paths and rerun this checkpoint")
        if refusal := gate.live_state_refusal(checkout, tip):
            raise checkpoint_gate.Stop("promote", "failed", refusal,
                       "run the conversion that the refusal names, then rerun this checkpoint")
        contained = not git(checkout, "merge-base", "--is-ancestor", tip, current).returncode
        if not contained and git(checkout, "merge-base", "--is-ancestor", current, tip).returncode:
            raise checkpoint_gate.Stop("promote", "failed", f"{checkout}: main does not fast-forward to {tip}",
                       "the showrunner will clear the listed checkout paths and rerun this checkpoint")
        if not contained:
            try:
                waiting_rows.move_to(checkout, tip)
            except (OSError, UnicodeError) as error:
                status = git(checkout, "status", "--short").stdout.strip()
                raise checkpoint_gate.Stop("promote", "failed", f"{checkout}: {status or ' '.join(str(error).split())}",
                           "the showrunner will clear the listed checkout paths and rerun this checkpoint") from error
        foreign = good(git(checkout, "rev-list", "main", "^refs/remotes/origin/main", f"^refs/remotes/origin/{merge_branch}"), "promote")
        if foreign:
            return PromotionNotPushed(tip, False)
        try:
            landed = land_main.land(checkout, "main")
        except land_main.LandError as error:
            raise checkpoint_gate.Stop("promote", "failed", str(error), "repair the landing and rerun this checkpoint") from error
        checkpoint_gate.report("post-push", "warning" if isinstance(landed.hook, land_main.HookFailed) else "ok",
                               land_main.hook_report(landed.hook))
        return PromotionAdvanced(tip)


def pull_mac_checkout(mac: PurePosixPath, source: Path) -> main_publish.MacPulled | main_publish.MacPullPending:
    settings_read = conf.read('merge_checkpoint')
    try:
        tries = settings_read.whole("MAC_PULL_TRIES")
        retry_seconds = settings_read.number("MAC_PULL_RETRY_SECONDS")
    except ValueError as error:
        raise checkpoint_gate.Stop("promote", "failed", str(error), "repair the Mac pull configuration and rerun this checkpoint") from error
    remote = main_publish.mac_pull_command(mac, source)
    attempt = 0
    while True:
        result = subprocess.run(["ssh", "mac", remote], capture_output=True, text=True, check=False)
        match = re.search(r"(?:^|\n)rc=(\d+)", result.stdout)
        if result.returncode == 0 and match is not None and int(match.group(1)) == 0:
            main_publish.clear_pull_only(mac)
            return main_publish.MacPulled()
        attempt += 1
        if attempt == tries:
            if result.returncode == 255 and match is None:
                main_publish.queue_pull_only(f"mac {mac}", main_publish.first_line(result),
                                             pulls=(main_publish.MacCheckoutPull(mac, source),))
                _ = mac_wake.start()
                return main_publish.MacPullPending()
            raise checkpoint_gate.Stop("promote", "failed", f"mac {mac}: {result.stdout.strip()} {result.stderr.strip()}",
                       "the showrunner will repair the Mac pull and rerun this checkpoint")
        time.sleep(retry_seconds)


def send_queued_zone(mac: PurePosixPath) -> None:
    """Send a /zone change queued while the Mac was offline: the pull just done carried its commit."""
    done = subprocess.run([PYTHON, str(ZONE_SCRIPT), "drain", "--pulled", str(mac)],
                          capture_output=True, text=True, check=False)
    for line in done.stdout.splitlines():
        checkpoint_gate.report("promote", "ok", line)


def promotion_label(request: MergeRequest | ChildHandoffRequest) -> str:
    return (f"{request.unit.name} phase {request.phase}" if isinstance(request, MergeRequest)
            else f"{request.child.name} style hand-off")


def promote(request: MergeRequest | ChildHandoffRequest, tip: str, *, local_only: bool = False) -> None:
    promote_targets(request.promotion, request.production.merge_branch, tip,
                    source=request.production.checkout, label=promotion_label(request), local_only=local_only)


def finish_promotion(request: MergeRequest | ChildHandoffRequest) -> None:
    finish_promotion_targets(request.promotion, source=request.production.checkout,
                             label=promotion_label(request))


def promote_targets(promotion: production_doc.NoPromotion | production_doc.PromoteTo,
                    branch: str, tip: str, *, source: Path, label: str, local_only: bool = False) -> None:
    """Install a pushed tip at the production's local and Mac destinations."""
    if isinstance(promotion, production_doc.NoPromotion):
        checkpoint_gate.report("promote", "ok", "no promotion configured")
        return
    for checkout in promotion.checkouts:
        result = promote_local_checkout(checkout, branch, tip)
        already_at = isinstance(result, (PromotionAlreadyAtTip, PromotionOriginPushed)) or (
            isinstance(result, PromotionNotPushed) and result.already_at_tip)
        detail = f"{checkout} {'already at' if already_at else 'main at'} {tip}"
        if isinstance(result, PromotionNotPushed):
            detail += "; main holds commits not on origin/" + branch + " and was not pushed"
        checkpoint_gate.report("promote", "ok", detail)
    if not local_only:
        finish_promotion_targets(promotion, source=source, label=label)


def finish_promotion_targets(promotion: production_doc.NoPromotion | production_doc.PromoteTo,
                             *, source: Path, label: str) -> None:
    """Announce local installs and pull the Mac after the main push lock is released."""
    if isinstance(promotion, production_doc.NoPromotion):
        return
    for checkout in promotion.checkouts:
        into(checkout, "main", f"sessions on {os.uname().nodename} run the installed commands from it",
             f"the live gate of {label}")
    if isinstance(promotion.mac, production_doc.MacCheckout):
        mac = promotion.mac.path
        source = promotion.checkouts[0] if promotion.checkouts else source
        outcome = pull_mac_checkout(mac, source)
        if isinstance(outcome, main_publish.MacPullPending):
            checkpoint_gate.report("promote", "ok", f"mac unreachable, pull pending: {mac}")
            return
        checkpoint_gate.report("promote", "ok", f"mac {mac} main pulled")
        send_queued_zone(mac)
        into(mac, "main", "sessions on mac run the installed commands from it",
             f"the live gate of {label}")


def review_numbers(line: str) -> ReviewTrial:
    pattern = (r"review trial: ux (\d+) findings, code (\d+) findings, review-worker minutes ([\d.]+), "
               r"ux check minutes ([\d.]+), ux repair minutes ([\d.]+)")
    match = re.fullmatch(pattern, line.strip())
    if match is None:
        raise Refusal("--review-trial must read: review trial: ux <N> findings, code <N> findings, "
                      + "review-worker minutes <M>, ux check minutes <U>, ux repair minutes <R>")
    return ReviewTrial(match[1], match[2], match[3], match[4], match[5])


def record(request: MergeRequest, merge_head: str, tests_green: str) -> None:
    recorded = (f"{request.unit.name} phase {request.phase} ({request.commit}) merged as {merge_head}; ")
    parent = request.parent_production
    if parent.log.exists() and recorded in parent.log.read_text(encoding="utf-8"):
        checkpoint_gate.report("record", "ok", "checkpoint already recorded")
        return
    detail = ("review ledger and production log updated" if isinstance(request.kind, CodeCheckpoint)
              else f"{'shrink' if isinstance(request.kind, ShrinkCommit) else 'as-built'} logged without review-ledger row")
    if isinstance(request.kind, CodeCheckpoint):
        numbers = request.review_trial
        if isinstance(numbers, NoReviewTrial):
            raise checkpoint_gate.Stop("record", "failed", "review trial absent", "send the notice's full review trial line")
        merged = good(git(request.production.checkout, "show", "-s", "--format=%cI", merge_head), "record")
        script = checkpoint_gate.tool("review_regime.py", Path.home() / ".claude/scripts/production/review_regime.py",
                      "MERGE_CHECKPOINT_REVIEW_REGIME")
        review_command = [PYTHON, script]
        command = [*review_command, "add", "--unit", request.unit.name, "--plan", checkpoint_plan(request).as_posix(),
                   "--phase", request.phase, "--checkpoint", request.commit,
                   "--regime", request.regime, "--started", request.started, "--merged", merged,
                   "--holds", str(request.holds), "--merge-defects", str(request.merge_defects),
                   "--ux-findings", numbers.ux_findings, "--code-findings", numbers.code_findings,
                   "--review-minutes", numbers.review_minutes, "--ux-check-minutes", numbers.ux_check_minutes,
                   "--ux-repair-minutes", numbers.ux_repair_minutes]
        if request.excluded:
            command.extend(("--excluded", request.excluded))
        result = subprocess.run(command, cwd=request.production.checkout, capture_output=True, text=True, check=False)
        if result.returncode:
            raise checkpoint_gate.Stop("record", "failed", failure_detail(result),
                       "the showrunner will repair the review record and rerun")
        if result.stdout.startswith("kept "):
            detail = result.stdout.strip() + "; " + detail
    parent.log.parent.mkdir(parents=True, exist_ok=True)
    with parent.log.open("a", encoding="utf-8") as log:
        suffix = "; promoted" if isinstance(request.promotion, production_doc.PromoteTo) else ""
        _ = log.write(f"- {datetime.now(parent.zone):%H:%M %Z}: {request.unit.name} phase {request.phase} "
                      + f"({request.commit}) merged as {merge_head}; "
                      + ("gate after push" if request.bump else f"{tests_green} green") + f"; pushed{suffix}\n")
    checkpoint_gate.report("record", "ok", detail)


def end_phase_scratch(request: MergeRequest) -> None:
    """End the merged phase's scratch task and report record failures."""
    if not isinstance(request.kind, CodeCheckpoint):
        return
    task = f"phase:{request.unit.name}/{request.phase}"
    try:
        _ = scratch.end(task)
    except (scratch.ScratchError, OSError) as error:
        checkpoint_gate.report("scratch", "warning", f"{task} not removed: {error}")
        return


def notify_style(checkout: Path, branch: str, tip: str) -> bool:
    try:
        script = checkpoint_gate.tool("style_sync.py", Path(__file__).resolve().parents[1] / "style/style_sync.py",
                      "MERGE_CHECKPOINT_STYLE_SYNC")
        _ = sys.stdout.flush()
        result = subprocess.run(
            [PYTHON, script, "notify", "--repo-dir", str(checkout), "--branch", branch, "--tip", tip],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if result.returncode or result.stdout.strip() or result.stderr.strip():
            detail = failure_detail(result)
            print("style sync: " + detail.replace("\n", "; "), flush=True)
        return result.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("style sync: " + str(error).replace("\n", "; "), flush=True)
        return False


@contextmanager
def _external_merge_wait(arguments: list[str]) -> Generator[None, None, None]:
    if os.environ.get(PROBE_ENV) == "1":
        yield
        return
    recorder = [PYTHON, str(Path(progress_history.__file__))]
    wait_id = ""
    try:
        started = subprocess.run([*recorder, *arguments], capture_output=True, text=True, check=False)
        wait_id = started.stdout.strip() if started.returncode == 0 else ""
        if not wait_id:
            checkpoint_gate.report("wait", "NotRecorded", started.stderr.strip() or "recorder returned no wait id")
    except OSError as error:
        checkpoint_gate.report("wait", "NotRecorded", str(error))
    try:
        yield
    finally:
        if wait_id:
            try:
                finished = subprocess.run([*recorder, "finish-external-wait", "--wait-id", wait_id],
                                          capture_output=True, text=True, check=False)
                if finished.returncode:
                    checkpoint_gate.report("wait", "NotRecorded", finished.stderr.strip() or "cannot close the gate wait")
            except OSError as error:
                checkpoint_gate.report("wait", "NotRecorded", str(error))


@contextmanager
def merge_gate_wait(worktree: Path, phase_id: str, unit: str, checkpoint: str) -> Generator[None, None, None]:
    """Resolve the full checkpoint in durable history, including finished runs."""
    with _external_merge_wait(["start-checkpoint-wait", "--commit", checkpoint,
        "--working-dir", str(worktree), "--phase-id", phase_id, "--unit", unit,
        "--reason", "checkpoint merge tests"]):
        yield


@contextmanager
def child_handoff_wait(production: str, checkpoint: str) -> Generator[None, None, None]:
    """A child branch checkpoint belongs to its production, with no phase."""
    owner = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID", "")
    with _external_merge_wait(["start-handoff-wait", "--production", production,
        "--commit", checkpoint, "--owner-session", owner, "--reason", "child production merge tests"]):
        yield


def record_pushed_merges(production: ProductionDocument, merge_head: str, source_checkpoint: str,
                         tip: str, *, phase_id: str, issue: str = "") -> None:
    """Receipts survive promotion failures and include origin/main integration merges."""
    repository = production_field(production.doc.read_text(encoding="utf-8").splitlines(), "Repository")
    project = Path(repository).expanduser().name
    merges = good(git(production.checkout, "rev-list", "--ancestry-path", "--merges", "--reverse",
                      f"{merge_head}..{tip}"), "receipt").splitlines()
    for commit in [merge_head, *merges]:
        if commit != merge_head and git(production.checkout, "merge-base", "--is-ancestor",
                                        f"{commit}^2", "origin/main").returncode != 0:
            break  # A retry can see later units' merges; their own call records them.
        source = source_checkpoint if commit == merge_head else good(
            git(production.checkout, "rev-parse", f"{commit}^2"), "receipt")
        stamp = float(good(git(production.checkout, "show", "-s", "--format=%ct", commit), "receipt"))
        _ = progress_history.write_merge_receipt(
            project=project, phase_id=phase_id, issue=issue, merge_commit=commit,
            source_checkpoint=source, target_branch=production.merge_branch, merged_at=stamp,
        )


def start_gate(request: MergeRequest, paths: tuple[str, ...], merge_head: str, tip: str) -> None:
    """Record and launch at most one gate for this tip, without delivering notices."""
    path = checkpoint_step_record(request.production.checkout, merge_head, "bump-gate")
    resume_delivery = False
    launch_failure = ""
    log = path.with_name("bump-gate.log")
    parent = request.parent_production
    record: checkpoint_gate.BumpGateRecord = {
        "tip": tip, "checkpoint": request.commit,
        "production_doc": str(parent.doc), "name": parent.name,
        "owner_id": str(parent.owner.id), "unit_id": str(request.unit.staff_id),
        "merge_branch": request.production.merge_branch, "checkout": str(request.production.checkout),
        "production_log": os.path.relpath(parent.log, request.production.checkout),
        "zone": str(parent.zone), "unit": request.unit.name,
        "delivered_to": [],
        "paths": list(paths), "merge_tests": [list(command) for command in request.merge_tests],
        "flakes": sorted(request.flakes), "flaky_tests": sorted(request.flaky_tests),
        "started": datetime.now(parent.zone).isoformat(), "pid": 0, "proc_start": "",
        "result": "pending", "detail": "", "log": str(log),
    }
    with checkpoint_gate.record_lock(path):
        if path.exists():
            previous = checkpoint_gate.read_record(path)
            if previous["tip"] == tip:
                if previous["result"] != "pending":
                    record = previous
                    resume_delivery = True
                else:
                    live = sessions.process_liveness(previous["pid"], previous["proc_start"])
                    if isinstance(live, sessions.LiveSessionProcess):
                        checkpoint_gate.report("bump gate", "ok", f"tip {tip} gate still runs as pid {previous['pid']}")
                        return
                    if isinstance(live, sessions.SessionLivenessUnknown):
                        checkpoint_gate.report("bump gate", "held", live.reason)
                        return
        if not resume_delivery:
            checkpoint_gate.save_record(path, record)
            with log.open("a", encoding="utf-8") as output:
                try:
                    process = subprocess.Popen(
                        [PYTHON, str(Path(__file__).with_name("bump_gate.py").resolve()), "--record", str(path)],
                        cwd=request.production.checkout, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                    )
                    record["pid"] = process.pid
                    record["proc_start"] = sessions.process_start(process.pid) or ""
                    checkpoint_gate.save_record(path, record)
                except OSError as error:
                    launch_failure = f"start failed: {error}"
                    record["result"] = "red"
                    record["detail"] = launch_failure
                    checkpoint_gate.save_record(path, record)
    if resume_delivery or launch_failure:
        if launch_failure:
            with Path(record["log"]).open("a", encoding="utf-8") as output:
                _ = output.write(launch_failure + "\n")
        checkpoint_gate.report("bump gate", "failed" if launch_failure else "ok",
               f"{launch_failure or 'tip ' + tip + ' already ' + record['result']}; log {record['log']}")
        return
    checkpoint_gate.report("bump gate", "ok", f"started pid {record['pid']} on pushed tip {tip}; log {record['log']}")


def deliver_gate_result(request: MergeRequest, merge_head: str, tip: str) -> None:
    """Retry completed notices only after the caller releases the main push lock."""
    path = checkpoint_step_record(request.production.checkout, merge_head, "bump-gate")
    if not path.exists():
        return
    record = checkpoint_gate.read_record(path)
    if record["tip"] != tip or record["result"] == "pending":
        return
    with Path(record["log"]).open("a", encoding="utf-8") as output, redirect_stdout(output):
        checkpoint_gate.send_result(path, record)


def resolved_checkpoint(request: MergeRequest) -> MergeRequest:
    """Resolve the checkpoint hash and retain its staff snapshot."""
    if request.staff_record is None:
        request = request._replace(staff_record=staff.snapshot())
    same_parent = isinstance(request.staff_parent, StaffParentIsDestination)
    checkout = request.production.checkout if same_parent else request.unit.worktree
    commit = f"{request.commit}^{{commit}}"
    checkpoint = git(checkout, "rev-parse", "--verify", commit)
    if checkpoint.returncode and same_parent:
        checkpoint = git(request.unit.worktree, "rev-parse", "--verify", commit)
    if checkpoint.returncode:
        raise checkpoint_gate.Stop("ancestry", "failed", f"commit {request.commit} is absent",
                                   "push the checkpoint and send its hash again")
    return request._replace(commit=checkpoint.stdout.strip())


class CheckedCheckpointMerge(NamedTuple):
    commits: list[change_review.Commit]
    paths: tuple[str, ...]
    own_paths: tuple[str, ...]
    merge_head: str


def merge_checked_checkpoint(
    request: MergeRequest, *, batch_checkout: ResetBatchCheckout | KeepBatchCheckout = KEEP_BATCH_CHECKOUT,
) -> CheckedCheckpointMerge:
    """Check an incoming checkpoint and make its own merge commit."""
    commits = change_review_commits(request)
    paths = changed_paths(request)
    own_paths = scope(request, paths)
    conflicts(request)
    other_units(request, paths)
    return CheckedCheckpointMerge(commits, paths, own_paths, merge(request, batch_checkout=batch_checkout))


def check_publication_sources(request: MergeRequest, commits: list[change_review.Commit],
                              test_tip: str, before_merge_tip: str, *,
                              batch_stop_owns_rollback: bool = False) -> None:
    """Check review ownership before publication and undo an unreadable source."""
    parent_doc = request.parent_production.doc
    if change_review.list_path(parent_doc, record=request.staff_record) is None:
        return
    try:
        _ = change_review.publication_changes(commits, parent_doc, record=request.staff_record,
                                               known_producer=request.unit.staff_id)
    except (change_review.ChangeReviewError, OSError) as error:
        if not batch_stop_owns_rollback:
            undo_unpushed_merge(request.production, test_tip, before_merge_tip)
        raise checkpoint_gate.Stop("change review", "failed", f"{error}: the staff record is read before the push",
                   "The staff record is unreadable; nothing was pushed; repair the staff record and send the checkpoint again.") from error


def publish_checkpoint(request: MergeRequest, log_folder: Path) -> str:
    """Publish the tested tip through the production's selected push command."""
    if isinstance(request.push, GitPush):
        return push_git(request.production, False)
    return push_validate(request.production,
                         f"ci({request.production.name}): format fixes after {request.unit.name} phase {request.phase}",
                         False, log_folder / f"merge_{request.commit[:7]}_push.log", request.cancel_prior)


def finish_checkpoint(request: MergeRequest, merge_head: str, tests_green: str) -> None:
    """Record one completed item and print its director's result."""
    promotion_done = checkpoint_step_record(request.production.checkout, merge_head, "promotion")
    record(request, merge_head, tests_green)
    end_phase_scratch(request)
    record_land(request, landed=True)
    print(f"send {request.unit.name}: phase {request.phase} merged and pushed as {merge_head}; continue with the next step")
    try:
        promotion_done.parent.mkdir(parents=True, exist_ok=True)
        promotion_done.touch()
    except OSError as error:
        checkpoint_gate.report("record", "warning", f"{promotion_done}: could not mark completion ({error})")


def land_result_error(request: MergeRequest, *, landed: bool, step: str, state: str, detail: str) -> str:
    try:
        land_runs.record_result(commit=request.commit, production=request.production.name, unit=request.unit.name,
                                phase=request.phase, landed=landed, step=step, state=state, detail=detail)
    except (OSError, UnicodeDecodeError) as error:
        return str(error)
    return ""


def record_land(request: MergeRequest, *, landed: bool, step: str = "", state: str = "", detail: str = "") -> None:
    """Add the land result for the units board."""
    if error := land_result_error(request, landed=landed, step=step, state=state, detail=detail):
        print(f"land result: not recorded — {error}", flush=True)


def report_checkpoint_stop(request: MergeRequest, error: checkpoint_gate.Stop) -> int:
    checkpoint_gate.report(error.step, error.state, error.detail.replace("\n", "; "))
    record_land(request, landed=False, step=error.step, state=error.state, detail=error.detail)
    print(f"send {request.unit.name}: {error.message}")
    return checkpoint_gate.TREE_CHANGED_STATUS if error.state == "cancelled" else 1


def report_validation_push_stop(error: ValidationPushFailed,
                                checkpoints_with_own_paths: tuple[tuple[MergeRequest, tuple[str, ...]], ...]) -> int:
    """Request a repair only when each reported file has one unit owner."""
    units_by_path: dict[Path, dict[staff.StaffId, tuple[str, str]]] = {path: {} for path in error.paths}
    for request, own_paths in checkpoints_with_own_paths:
        for path in own_paths:
            file = request.production.checkout / path
            if file in units_by_path:
                units_by_path[file][request.unit.staff_id] = (request.unit.name, path)
    files_by_unit: dict[str, list[str]] = {}
    if units_by_path and all(len(units) == 1 for units in units_by_path.values()):
        for units in units_by_path.values():
            unit, path = next(iter(units.values()))
            files_by_unit.setdefault(unit, []).append(path)
    faulty = sorted(files_by_unit)
    notices: list[BatchStopNotice] = []
    for request, _ in checkpoints_with_own_paths:
        unit = request.unit.name
        if files := files_by_unit.get(unit):
            message = f"repair the push failure in {', '.join(sorted(files))} and send a new checkpoint"
        elif faulty:
            change = f"{faulty[0]}'s change" if len(faulty) == 1 else f"changes from {', '.join(faulty)}"
            message = f"nothing was pushed; the push failed in {change}; send this checkpoint again after that repair"
        else:
            message = error.message
        notices.append(BatchStopNotice(request, error.detail, message))
    return finish_batch_stop(error, notices)


class StandaloneCheckpointPush(NamedTuple):
    pass


class ResumedBatchPush(NamedTuple):
    before_batch_tip: str
    own_paths_by_commit: dict[str, tuple[str, ...]]


STANDALONE_CHECKPOINT_PUSH = StandaloneCheckpointPush()


def run(request: MergeRequest, *, push_scope: StandaloneCheckpointPush | ResumedBatchPush = STANDALONE_CHECKPOINT_PUSH,
        batch_rows: tuple[MergeRequest, ...] = (),
        batch_checkout: ResetBatchCheckout | KeepBatchCheckout = KEEP_BATCH_CHECKOUT) -> int:
    own_paths: tuple[str, ...] = ()
    try:
        require_merge_branch(request.production)
        request = resolved_checkpoint(request)
        origin = ancestry(request)
        already = origin.already_merged
        tests_green = ("gate after push" if request.bump else
                       "merge tests" if isinstance(request.kind, CodeCheckpoint) else "no tests")
        pushed = pushed_state(request.production, request.commit) if already else NotYetPushed()
        if not already:
            commits, checkpoint_paths, own_paths, merge_head = merge_checked_checkpoint(request, batch_checkout=batch_checkout)
        else:
            merge_head = merged_checkpoint(request)
            # The merge branch already contains this checkpoint. Its merge
            # parents retain the original incoming commits even after later merges.
            commits = change_review.merge_commits(request.production.checkout, merge_head)
            # A run killed between its merge and its push left a merge nothing has tested.
            steps = ("scope", "conflicts", "other units", "merge")
            if isinstance(pushed, PushedTip) and not request.bump:
                steps += ("test", "red")
            for step in steps:
                checkpoint_gate.report(step, "ok", "checkpoint already merged; resuming promotion")
            checkpoint_paths = changed_paths(request, f"{merge_head}^1")
            if (isinstance(pushed, NotYetPushed) and isinstance(request.push, ValidateAndPush)
                    and isinstance(push_scope, StandaloneCheckpointPush)):
                own_paths = _unit_paths(request, checkpoint_paths)
        paths = checkpoint_paths
        before_merge_tip = good(git(request.production.checkout, "rev-parse", f"{merge_head}^1"), "merge")
        test_tip = (batch_checkout.expected_tip if isinstance(batch_checkout, ResetBatchCheckout)
                    else good(git(request.production.checkout, "rev-parse", "HEAD"), "test"))
        destination_tip = test_tip
        if isinstance(pushed, NotYetPushed):
            check_publication_sources(request, commits, test_tip, before_merge_tip,
                                      batch_stop_owns_rollback=isinstance(batch_checkout, ResetBatchCheckout))
            if isinstance(batch_checkout, ResetBatchCheckout):
                paths = prepare_checkpoint(request.production, merge_head, batch_checkout=batch_checkout)
            else:
                paths = prepare_checkpoint(request.production, merge_head)
        test_tip = (batch_checkout.expected_tip if isinstance(batch_checkout, ResetBatchCheckout)
                    else good(git(request.production.checkout, "rev-parse", "HEAD"), "test"))

        batch_log = request.scratch / f"merge_{request.commit[:7]}_test.log"
        batch_test_started = False

        def retest(paths: tuple[str, ...], test_tip: str) -> None:
            nonlocal tests_green, batch_test_started
            if request.bump:
                return
            try:
                if isinstance(request.kind, CodeCheckpoint):
                    with merge_gate_wait(request.unit.worktree, request.phase, request.unit.name, request.commit):
                        if isinstance(batch_checkout, ResetBatchCheckout):
                            if not batch_test_started:
                                batch_log.parent.mkdir(parents=True, exist_ok=True)
                                _ = batch_log.write_text("", encoding="utf-8")
                                batch_test_started = True
                            def paths_for_row(row: MergeRequest) -> tuple[str, ...]:
                                if row == request:
                                    return checkpoint_paths
                                base = f"{merged_checkpoint(row)}^1" if merged(row) else destination_tip
                                return changed_paths(row, base)

                            batch_checkout.expected_tip = test_tip
                            tests_green = run_batch_tests(request, paths, batch_rows, paths_for_row, batch_log,
                                                          batch_checkout)
                        elif isinstance(pushed, PushedTip):
                            tests_green = checkpoint_tests(request.production, paths, request.commit, request.merge_tests,
                                                           request.flakes, request.scratch, test_tip, request.flaky_tests,
                                                           before_merge_tip=pushed.tip, ledger=request.parent_production)
                        else:
                            tests_green = tests(request, paths, merge_head)
                else:
                    tests_green = tests(request, paths, merge_head)
            except (OSError, ValueError) as error:
                if isinstance(batch_checkout, KeepBatchCheckout):
                    undo_unpushed_merge(request.production, test_tip,
                                        pushed.tip if isinstance(pushed, PushedTip) else before_merge_tip)
                raise checkpoint_gate.Stop("red", "failed", str(error),
                           "repair the test logging failure and send a new checkpoint") from error

        if request.bump:
            checkpoint_gate.report("test", "ok", "user bump: the gate runs on the pushed tip in the background")
        elif isinstance(pushed, NotYetPushed):
            retest(paths, test_tip)
        promotion_done = checkpoint_step_record(request.production.checkout, merge_head, "promotion")
        tip = ""
        try:
            try:
                with ExitStack() as gate_lock:
                    if request.bump:
                        path = checkpoint_step_record(request.production.checkout, merge_head, "bump-gate")
                        _ = gate_lock.enter_context(checkpoint_gate.record_lock(path))
                    published_tip = pushed.tip if isinstance(pushed, PushedTip) else test_tip
                    rollback_tip = pushed.tip if isinstance(pushed, PushedTip) else before_merge_tip
                    with tested_main_push(request.production, published_tip, retest, rollback_tip,
                                          follow_main=not isinstance(pushed, PushedTip) or not promotion_done.exists(),
                                          batch_checkout=batch_checkout) as tested_tip:
                        batch_checkout = KEEP_BATCH_CHECKOUT
                        if isinstance(pushed, PushedTip):
                            tip = pushed.tip
                            if tested_tip != tip:
                                tip = push_git(request.production, False)
                            checkpoint_gate.report("push", "ok", f"{request.production.merge_branch} already pushed at {tip}")
                            end_pushed_worktrees(request.production)
                        else:
                            push_base = before_merge_tip if not already else ""
                            if isinstance(push_scope, ResumedBatchPush):
                                push_base = push_scope.before_batch_tip
                            with push_rollback(request.production, push_base):
                                tip = publish_checkpoint(request, request.scratch)
                        if request.bump:
                            paths = tuple(good(git(request.production.checkout, "diff", "--name-only",
                                                   before_merge_tip, tip), "test").splitlines())
                            start_gate(request, paths, merge_head, tip)
                        gate_lock.close()
                        promote(request, tip, local_only=True)
                        delete_landed_branch(request, origin.branch, tip)
            finally:
                if tip:
                    if request.bump:
                        deliver_gate_result(request, merge_head, tip)
                    into(request.production.checkout, request.production.merge_branch,
                         "the merge branch collects every unit's checkpoints", "the unit’s next checkpoint")
            finish_promotion(request)
        finally:
            if tip:
                record_pushed_merges(request.production, merge_head, request.commit, tip, phase_id=request.phase,
                                     issue=str(checkpoint_plan(request)))
                notify_published_style(request.production, merge_head, tip)
        record_publication_changes(request.production, record=request.staff_record,
                                   review_docs=(request.parent_production.doc,),
                                   known_producer=change_review.KnownMergeProducer(merge_head, request.unit.staff_id))
        finish_checkpoint(request, merge_head, tests_green)
        return 0
    except ValidationPushFailed as error:
        if isinstance(push_scope, ResumedBatchPush):
            raise
        return report_validation_push_stop(error, ((request, own_paths),))
    except checkpoint_gate.Stop as error:
        if isinstance(batch_checkout, ResetBatchCheckout):
            raise
        return report_checkpoint_stop(request, error)
    except (OSError, ValueError) as error:
        if isinstance(batch_checkout, ResetBatchCheckout):
            raise checkpoint_gate.Stop("run", "failed", str(error),
                                       "the showrunner will inspect the failed command") from error
        checkpoint_gate.report("run", "failed", str(error).replace("\n", "; "))
        record_land(request, landed=False, step="run", state="failed", detail=str(error))
        print(f"send {request.unit.name}: the showrunner will inspect the failed command")
        return 1


class MergedBatchItem(NamedTuple):
    """A checked merge and its test paths."""

    request: MergeRequest
    origin: CheckpointOrigin
    merge_head: str
    paths: tuple[str, ...]
    own_paths: tuple[str, ...]


class LegacyBatchMerge(NamedTuple):
    """An older batch record has no in-flight field."""


class NoBatchMerge(NamedTuple):
    pass


class InFlightBatchMerge(NamedTuple):
    target: str


NO_BATCH_MERGE = NoBatchMerge()


@dataclass
class BatchResumeRecord:
    path: Path
    checkpoints: tuple[MergeRequest, ...]
    unpublished_merges: dict[str, str]
    merge_state: LegacyBatchMerge | NoBatchMerge | InFlightBatchMerge = NO_BATCH_MERGE
    retirement_attempted: bool = False
    resuming: bool = False

    def checkpoint_rows(self) -> str:
        return "".join(f"{item.commit}\t{item.batch_argument_line}\n" for item in self.checkpoints)

    def store_merge_state(self, state: NoBatchMerge | InFlightBatchMerge) -> None:
        target = state.target if isinstance(state, InFlightBatchMerge) else "-"
        write_text_atomic(self.path, self.checkpoint_rows() + f"in-flight\t{target}\n")
        self.merge_state = state

    def retire(self) -> None:
        self.retirement_attempted = True
        try:
            self.path.unlink(missing_ok=True)
        except OSError as error:
            checkpoint_gate.report("batch", "warning", f"{self.path}: could not remove the batch record ({error})")


def reset_batch(rollback: ResetBatchCheckout, warnings: list[str]) -> None:
    """Undo every unpublished merge from the current batch attempt."""
    production = rollback.production
    checkout = production.checkout
    record = rollback.record
    state = record.merge_state
    head = good(git(checkout, "rev-parse", "HEAD"), "merge")
    if head != rollback.expected_tip:
        parents = good(git(checkout, "show", "-s", "--format=%P", head), "merge").split()
        if not isinstance(state, InFlightBatchMerge) or parents != [rollback.expected_tip, state.target]:
            raise checkpoint_gate.Stop("red", "failed", f"HEAD moved from {rollback.expected_tip}",
                                       "the showrunner will inspect the merge branch")
    open_merge = read_open_merge(merge_head_path(checkout))
    clear_merge = False
    if isinstance(open_merge, OpenMerge):
        owns_merge = isinstance(state, InFlightBatchMerge) and (
            not open_merge.targets or open_merge.targets == (state.target,))
        if not owns_merge and not isinstance(state, LegacyBatchMerge):
            detail = ", ".join(sorted(open_merge.targets)) or "MERGE_HEAD is empty or unreadable"
            raise checkpoint_gate.Stop("merge", "held", f"the batch left the operator merge open: {detail}",
                                       "the showrunner will inspect the merge branch")
        _ = good(git(checkout, "merge", "--abort"), "merge")
        clear_merge = True
    try:
        if clear_merge and good(git(checkout, "rev-parse", "HEAD"), "merge") == rollback.start_tip:
            return
        if head == rollback.start_tip:
            _ = good(git(checkout, "reset", "--keep", head), "red")
        elif not push_origin_matches(production, rollback.published_tip, rollback.start_tip):
            raise checkpoint_gate.Stop("push", "failed", "origin moved; cannot reset the batch",
                                       "the showrunner will inspect the merge branch")
        else:
            undo_unpushed_merge(production, head, rollback.start_tip)
        clear_merge = clear_merge or isinstance(state, InFlightBatchMerge)
    finally:
        if clear_merge:
            try:
                record.store_merge_state(NO_BATCH_MERGE)
            except (OSError, ValueError) as error:
                warnings.append(f"could not clear the in-flight batch merge: {error}")


class BatchStopNotice(NamedTuple):
    request: MergeRequest
    detail: str
    message: str


class UnknownRowCommands(NamedTuple):
    error: str


class BatchRed(checkpoint_gate.Stop):
    """The gate failure and the notice for each pending row."""

    def __init__(self, red: checkpoint_gate.RedCheckpointTests,
                 notices: tuple[BatchStopNotice, ...], rollback: ResetBatchCheckout, main_tip: str,
                 log: Path, paths: dict[str, tuple[str, ...]]) -> None:
        super().__init__(red.step, red.state, red.detail, red.message)
        self.notices: tuple[BatchStopNotice, ...] = notices
        self.red: tuple[checkpoint_gate.RedStep, ...] = red.red
        self.modules: frozenset[str] = frozenset(name for step in red.red for name in step.names)
        self.tested_tip: str = rollback.expected_tip
        self.main_tip: str = main_tip
        self.log: Path = log
        self.paths: dict[str, tuple[str, ...]] = paths
        self.rollback: ResetBatchCheckout = rollback


type CheckpointCommand = tuple[tuple[str, ...], Path]


def selected_commands(steps: Iterable[checkpoint_gate.CheckpointTestStep]) -> frozenset[CheckpointCommand]:
    return frozenset((tuple(step.command), step.folder) for step in steps)


def run_batch_tests(reference: MergeRequest, paths: tuple[str, ...],
                    requests: Iterable[MergeRequest], paths_for_row: Callable[[MergeRequest], tuple[str, ...]],
                    log: Path, rollback: ResetBatchCheckout) -> str:
    require_drop_absent(rollback, rollback.expected_tip)
    steps = checkpoint_gate.test_steps(reference.production, paths, reference.merge_tests)
    row_paths: dict[str, tuple[str, ...]] = {}
    row_commands: list[tuple[MergeRequest, frozenset[CheckpointCommand] | UnknownRowCommands]] = []
    for row in requests:
        commands: frozenset[CheckpointCommand] | UnknownRowCommands = frozenset()
        if isinstance(row.kind, CodeCheckpoint):
            try:
                row_paths[row.commit] = paths_for_row(row)
                commands = selected_commands(checkpoint_gate.test_steps(row.production, row_paths[row.commit], row.merge_tests))
            except Exception as error:
                commands = UnknownRowCommands(" ".join(str(error).splitlines()))
        row_commands.append((row, commands))
    main = git(reference.production.checkout, "rev-parse", "--verify", "refs/remotes/origin/main")
    main_tip = main.stdout.strip() if main.returncode == 0 else ""
    try:
        return checkpoint_gate.run_test_steps(reference.parent_production, steps, reference.flakes,
                                              reference.flaky_tests, log, retry_failed_steps=False)
    except checkpoint_gate.RedCheckpointTests as error:
        red_commands = selected_commands(red.step for red in error.red)
        notices: list[BatchStopNotice] = []
        for row, commands in row_commands:
            label = f"phase {row.phase} ({row.commit[:12]})"
            message = f"{error.detail}; resend this checkpoint after the repair"
            if isinstance(commands, UnknownRowCommands):
                match = f"changed paths unknown: {commands.error}"
            elif commands & red_commands:
                match = "this row's tests include a red step"
                message = error.message
            else:
                match = "this row's tests include no reported red step"
            notices.append(BatchStopNotice(row, f"{error.detail}; {match}", f"{label}: {match}; {message}"))
        raise BatchRed(error, tuple(notices), rollback, main_tip, log, row_paths) from error


def batch_ancestor(checkout: Path, commit: str, tip: str) -> bool:
    result = git(checkout, "merge-base", "--is-ancestor", commit, tip)
    if result.returncode == 1:
        return False
    _ = good(result, "batch")
    return True


def require_drop_absent(checkout: ResetBatchCheckout, tip: str) -> None:
    commit = checkout.dropped_commit
    if commit and batch_ancestor(checkout.production.checkout, commit, tip):
        raise checkpoint_gate.Stop("batch", "held", f"the survivor tip includes dropped checkpoint {commit}",
                                   "resolve the ancestor before you resend this checkpoint")


def run_batch_probe(red: BatchRed, commit: str = "") -> subprocess.CompletedProcess[str]:
    """Test selected modules in a private worktree of the pinned main commit."""
    checkout = red.rollback.production.checkout
    with tempfile.TemporaryDirectory(prefix="checkpoint-probe-") as directory:
        worktree = Path(directory) / "tree"
        _ = good(git(checkout, "worktree", "add", "--detach", str(worktree), red.main_tip), "batch")
        try:
            if commit:
                _ = good(git(worktree, "merge", "--no-ff", "--no-edit", commit), "batch")
            command = checkpoint_gate.batch_module_command(red.red[0], checkout, worktree)
            assert command is not None
            result = checkpoint_gate.run_test_command(command, worktree, raise_start_errors=True)
            with red.log.open("a", encoding="utf-8") as output:
                _ = output.write(f"\nProbe {commit or 'main'} on {red.main_tip}\n")
                _ = output.write(result.stdout + result.stderr)
                _ = output.write(f"\nPROBE_EXIT={result.returncode}\n")
            checkpoint_gate.stop_test_without_result(
                checkpoint_gate.CheckpointTestStep("batch module probe", command, worktree), result, red.log)
            return result
        finally:
            _ = good(git(checkout, "worktree", "remove", "--force", str(worktree)), "batch")


def probe_batch_rows(red: BatchRed) -> MergeRequest | None:
    """Find one unpublished row that reproduces all red modules on green main."""
    checkout = red.rollback.production.checkout
    rows = tuple(notice.request for notice in red.notices)
    if (len(rows) < 2 or len(red.red) != 1 or red.red[0].step.folder != checkout
            or checkpoint_gate.batch_module_command(red.red[0], checkout, checkout) is None
            or not red.main_tip or not batch_ancestor(checkout, red.main_tip, red.tested_tip)):
        return None
    candidates = [row for row in rows if batch_ancestor(checkout, row.commit, red.tested_tip)
                  and not batch_ancestor(checkout, row.commit, red.rollback.published_tip)]
    if not candidates:
        return None
    command = red.red[0].step.command
    base = run_batch_probe(red)
    if base.returncode or checkpoint_gate.gate_rerun_red(command, base.stdout + base.stderr):
        return None
    module_paths = red.modules | {f"scripts/{name}" for name in red.modules}
    candidates.sort(key=lambda row: -len(module_paths.intersection(red.paths.get(row.commit, ()))))
    owner = None
    for row in candidates:
        result = run_batch_probe(red, row.commit)
        output = result.stdout + result.stderr
        if checkpoint_gate.gate_rerun_red(command, output):
            return None
        if not result.returncode:
            continue
        if (result.returncode != 1 or owner is not None
                or not checkpoint_gate.batch_probe_reproduced(command, output, red.modules)):
            return None
        carried = (red.rollback.start_tip, red.main_tip, *(item.commit for item in rows if item is not row))
        if any(batch_ancestor(checkout, row.commit, tip) for tip in carried):
            checkpoint_gate.report("batch", "held", f"{row.commit} is an ancestor of the base or a survivor")
            return None
        owner = row
    return owner


def finish_batch_stop(stop: checkpoint_gate.Stop, notices: Iterable[BatchStopNotice],
                      rollback: ResetBatchCheckout | KeepBatchCheckout = KEEP_BATCH_CHECKOUT, *,
                      record_results: bool = True) -> int:
    """Attempt rollback, send all notices, and record the results."""
    rollback_warnings: list[str] = []
    if isinstance(rollback, ResetBatchCheckout):
        try:
            reset_batch(rollback, rollback_warnings)
        except (checkpoint_gate.Stop, OSError, ValueError) as error:
            rollback_warnings.insert(0, f"rollback failed: {error}")

    rows = tuple(notices)
    checkpoint_gate.report(stop.step, stop.state, stop.detail.replace("\n", "; "))
    for request, _, message in rows:
        print(f"send {request.unit.name}: {message}", flush=True)

    record_errors: list[str] = []
    if record_results:
        for request, detail, _ in rows:
            if error := land_result_error(request, landed=False, step=stop.step, state=stop.state, detail=detail):
                record_errors.append(f"{request.unit.name} phase {request.phase}: {error}")
    for warning in rollback_warnings:
        checkpoint_gate.report("batch", "warning", warning)
    for record_error in record_errors:
        print(f"land result: not recorded — {record_error}", flush=True)
    return checkpoint_gate.TREE_CHANGED_STATUS if stop.state == "cancelled" else 1


def batch_stop_notices(requests: tuple[MergeRequest, ...], stop: checkpoint_gate.Stop,
                       *, held_at: int | None = None) -> Iterable[BatchStopNotice]:
    if isinstance(stop, BatchRed):
        yield from stop.notices
        return
    for index, request in enumerate(requests):
        label = f"phase {request.phase} ({request.commit[:12]})"
        detail = stop.detail
        if held_at is not None and index > held_at:
            held = requests[held_at]
            detail = f"Not run because {held.unit.name} phase {held.phase} ({held.commit[:12]}) was held."
            message = detail
        elif isinstance(stop, MainConflict):
            message = f"{detail}; check your checkpoint against origin/main, resolve any conflict, and resend it"
        else:
            message = f"{detail}; {stop.message}"
        yield BatchStopNotice(request, detail, f"{label}: {' '.join(message.splitlines())}")


def run_batch_attempt(requests: tuple[MergeRequest, ...], log: Path,
                      batch_checkout: ResetBatchCheckout, *, unpublished: dict[str, str]) -> int:
    """Merge and test the batch once, then publish it or report the stop."""
    items: list[MergedBatchItem] = []
    publication_started = False
    tip = ""
    start_tip = batch_checkout.start_tip
    try:
        for request in requests:
            origin = ancestry(request)
            if merge_head := unpublished.get(request.commit):
                checkpoint_gate.report("batch", "resume", f"{request.unit.name} phase {request.phase} is already merged")
                commits = change_review.merge_commits(request.production.checkout, merge_head)
                paths = changed_paths(request, f"{merge_head}^1")
                own_paths = _unit_paths(request, paths)
            elif origin.already_merged:
                raise checkpoint_gate.Stop("ancestry", "held", "the batch item is already merged",
                                           "resume it alone with the single command")
            else:
                commits, paths, own_paths, merge_head = merge_checked_checkpoint(request, batch_checkout=batch_checkout)
            check_publication_sources(request, commits, merge_head, start_tip, batch_stop_owns_rollback=True)
            items.append(MergedBatchItem(request, origin, merge_head, paths, own_paths))

        first = items[0]
        shared = first.request
        paths_by_commit = {item.request.commit: item.paths for item in items}
        code_items = [item for item in items if isinstance(item.request.kind, CodeCheckpoint)]
        test_paths = {path for item in code_items for path in item.paths}
        non_code_paths = {path for item in items if not isinstance(item.request.kind, CodeCheckpoint) for path in item.paths}
        tests_green = "no tests"
        reference = code_items[0].request if code_items else shared

        def retest(paths: tuple[str, ...], test_tip: str) -> None:
            nonlocal tests_green
            test_paths.update(set(paths) - non_code_paths)
            if not code_items and not test_paths and not batch_checkout.dropped_commit:
                tests_green = tests(shared, (), first.merge_head)
                return
            with ExitStack() as waits:
                for item in code_items:
                    _ = waits.enter_context(merge_gate_wait(item.request.unit.worktree, item.request.phase,
                                                            item.request.unit.name, item.request.commit))
                batch_checkout.expected_tip = test_tip
                tests_green = run_batch_tests(reference, tuple(sorted(test_paths)), requests,
                                              lambda row: paths_by_commit[row.commit], log, batch_checkout)

        remote = f"refs/remotes/origin/{shared.production.merge_branch}"
        _ = good(git(shared.production.checkout, "fetch", "origin",
                     f"+refs/heads/{shared.production.merge_branch}:{remote}"), "test")
        batch_checkout.published_tip = good(git(shared.production.checkout, "rev-parse", remote), "test")
        paths = prepare_checkpoint(shared.production, first.merge_head, published_tip=batch_checkout.published_tip,
                                   batch_checkout=batch_checkout)
        test_tip = batch_checkout.expected_tip
        retest(paths, test_tip)
        try:
            try:
                with tested_main_push(shared.production, test_tip, retest, start_tip, batch_checkout=batch_checkout):
                    with push_rollback(shared.production, start_tip):
                        publication_started = True
                        tip = publish_checkpoint(shared, log.parent)
                    batch_checkout.record.retire()
                    promote(shared, tip, local_only=True)
                    for item in items:
                        delete_landed_branch(item.request, item.origin.branch, tip)
            finally:
                if tip:
                    into(shared.production.checkout, shared.production.merge_branch,
                         "the merge branch collects every unit's checkpoints", "the unit’s next checkpoint")
            finish_promotion(shared)
        finally:
            if tip:
                for item in items:
                    receipt_tip = tip if item == items[-1] else item.merge_head
                    record_pushed_merges(item.request.production, item.merge_head, item.request.commit, receipt_tip,
                                         phase_id=item.request.phase, issue=str(checkpoint_plan(item.request)))
                notify_published_style(shared.production, first.merge_head, tip)
        review_docs = tuple(dict.fromkeys(item.request.parent_production.doc for item in items))
        record_publication_changes(shared.production, record=shared.staff_record, review_docs=review_docs)
        for item in items:
            finish_checkpoint(item.request, item.merge_head,
                              tests_green if isinstance(item.request.kind, CodeCheckpoint) else "no tests")
        return 0
    except ValidationPushFailed as error:
        return report_validation_push_stop(error, tuple((item.request, item.own_paths) for item in items))
    except (OSError, ValueError, checkpoint_gate.Stop) as error:
        if (isinstance(error, BatchRed) and not publication_started
                and (unpublished or not batch_checkout.record.resuming) and not batch_checkout.dropped_commit):
            return drop_batch_row(error)
        stop = error if isinstance(error, checkpoint_gate.Stop) else checkpoint_gate.Stop(
            "run", "failed", str(error), "the showrunner will inspect the failed command")
        rollback = KEEP_BATCH_CHECKOUT if publication_started else batch_checkout
        held_at = len(items) if stop.state == "held" and len(items) < len(requests) else None
        return finish_batch_stop(stop, batch_stop_notices(requests, stop, held_at=held_at), rollback)


def checkpoint_completed(request: MergeRequest, upstream: str) -> bool:
    """Require both publication and the promotion marker for the local merge."""
    checkout = request.production.checkout
    try:
        if git(checkout, "merge-base", "--is-ancestor", request.commit, upstream).returncode or not merged(request):
            return False
        merge_head = merged_checkpoint(request)
        return (git(checkout, "merge-base", "--is-ancestor", merge_head, upstream).returncode == 0
                and checkpoint_step_record(checkout, merge_head, "promotion").exists())
    except (checkpoint_gate.Stop, OSError, ValueError):
        return False


def resume_merged_items(record: BatchResumeRecord) -> tuple[tuple[MergeRequest, ...], int]:
    """Resume each merged item through the single route, and return the items still to merge.

    A red test or main conflict returns every pending row.
    """
    remaining: list[MergeRequest] = []
    status = 0
    failed: dict[str, MergeRequest] = {}
    push_scope: StandaloneCheckpointPush | ResumedBatchPush = STANDALONE_CHECKPOINT_PUSH
    checkpoints = record.checkpoints
    unpublished = tuple(item for item in checkpoints if item.commit in record.unpublished_merges)
    production = record.checkpoints[0].production
    checkout = production.checkout
    upstream = f"refs/remotes/origin/{production.merge_branch}"
    if unpublished and isinstance(unpublished[0].push, ValidateAndPush):
        merge_parents = tuple((item, f"{merged_checkpoint(item)}^1") for item in unpublished)
        try:
            own_paths = {item.commit: _unit_paths(item, changed_paths(item, parent)) for item, parent in merge_parents}
        except (checkpoint_gate.Stop, OSError, ValueError):
            # Request inspection if Git cannot recover all ownership.
            own_paths = {}
        push_scope = ResumedBatchPush(merge_parents[0][1], own_paths)
    for index, request in enumerate(checkpoints):
        cause = failed.get(request.unit.staff_id)
        if cause is not None:
            label = f"{cause.unit.name} phase {cause.phase} ({cause.commit[:12]})"
            status = report_checkpoint_stop(request, checkpoint_gate.Stop(
                "ancestry", "failed", f"later item follows {label}, which failed",
                f"resolve {label}, then resend this checkpoint"))
            continue
        if not merged(request):
            remaining.append(request)
            continue
        record.resuming = True
        checkpoint_gate.report("batch", "resume", f"{request.unit.name} phase {request.phase} is already merged")
        pending_rows = tuple(row for row in (*remaining, *checkpoints[index:])
                             if not checkpoint_completed(row, upstream))
        head = good(git(checkout, "rev-parse", "HEAD"), "merge")
        start_tip = head
        if unpublished and git(checkout, "merge-base", "--is-ancestor", unpublished[0].commit, upstream).returncode == 1:
            start_tip = good(git(checkout, "rev-parse", f"{merged_checkpoint(unpublished[0])}^1"), "merge")
        published_tip = good(git(checkout, "rev-parse", upstream), "merge")
        rollback = ResetBatchCheckout(production, start_tip, head, published_tip, record)
        try:
            resumed = run(request, push_scope=push_scope, batch_rows=pending_rows,
                          batch_checkout=rollback if pending_rows else KEEP_BATCH_CHECKOUT)
        except ValidationPushFailed as error:
            assert isinstance(push_scope, ResumedBatchPush)
            pending = (*remaining, *(item for item in checkpoints[index:]
                                     if item in unpublished or not merged(item)))
            status = report_validation_push_stop(
                error, tuple((item, push_scope.own_paths_by_commit.get(item.commit, ())) for item in pending))
            if error.rollback_completed:
                record.retire()
            return (), status
        except checkpoint_gate.Stop as error:
            return (), finish_batch_stop(error, batch_stop_notices(pending_rows, error), rollback)
        if resumed:
            status = resumed
            failed[request.unit.staff_id] = request
    return tuple(remaining), status


def unpushed_checkpoint_merges(production: ProductionDocument, published: str) -> dict[str, str]:
    merges = good(git(production.checkout, "log", "--reverse", "--first-parent", "--merges",
                     "--format=%H%x09%P%x09%s", f"{published}..HEAD"), "batch")
    sources: dict[str, str] = {}
    for line in merges.splitlines():
        merge_head, parents, subject = line.split("\t", 2)
        source = parents.split()[1]
        target = checkpoint_merge_target(source, production.merge_branch)
        if subject.endswith(target) and re.fullmatch(rf"Merge {unit_names.NAME.pattern} phase .+",
                                                      subject.removesuffix(target)):
            sources[source] = merge_head
    return sources


def needs_tracking_ref_refresh(checkout: Path, revision: str, refreshed: set[str]) -> bool:
    """Check whether the revision uses a tracking ref that the batch has not fetched."""
    if not revision.startswith(("origin/", "remotes/origin/", "refs/remotes/origin/")):
        return False
    base = re.split(r"[~^:]|@\{", revision, maxsplit=1)[0]
    symbolic = git(checkout, "rev-parse", "--verify", "--quiet", "--symbolic-full-name", base)
    ref = symbolic.stdout.rstrip("\n")
    return symbolic.returncode == 0 and ref.startswith("refs/remotes/origin/") and ref not in refreshed


def resolve_batch_checkpoints(requests: tuple[MergeRequest, ...]) -> tuple[MergeRequest, ...]:
    checkout = requests[0].production.checkout
    refreshed = {f"refs/remotes/origin/{branch}" for branch in dict.fromkeys(request.unit.branch for request in requests)
                 if fetch_origin_unit_branch(checkout, branch, "--no-write-fetch-head")}
    resolved: dict[int, MergeRequest] = {}
    for index, request in enumerate(requests):
        if needs_tracking_ref_refresh(checkout, request.commit, refreshed):
            continue
        try:
            resolved[index] = resolved_checkpoint(request)
        except checkpoint_gate.Stop:
            continue
    if len(resolved) < len(requests):
        _ = fetch_origin_heads(checkout, "--no-write-fetch-head")
        for index, request in enumerate(requests):
            if index in resolved:
                continue
            try:
                resolved[index] = resolved_checkpoint(request)
            except checkpoint_gate.Stop as error:
                raise Refusal(f"{request.unit.name} phase {request.phase}: {error}") from error
    return tuple(resolved[index] for index in range(len(requests)))


@contextmanager
def batch_resume_record(requests: tuple[MergeRequest, ...], upstream: str) -> Generator[BatchResumeRecord]:
    """Keep the ordered arguments and commit hashes while the batch has unpushed merges."""
    production = requests[0].production
    checkout = production.checkout
    filename = f"checkpoint-batches/{production.merge_branch}"
    path = Path(good(git(checkout, "rev-parse", "--path-format=absolute", "--git-path", filename), "batch"))
    checkpoints = resolve_batch_checkpoints(requests)
    unpublished = unpushed_checkpoint_merges(production, upstream)
    record = BatchResumeRecord(path, checkpoints, unpublished)
    rows = record.checkpoint_rows()
    try:
        saved = path.read_bytes().decode("utf-8")
    except (FileNotFoundError, UnicodeError):
        saved = ""
    suffix = saved[len(rows):]
    target = suffix.removeprefix("in-flight\t").removesuffix("\n")
    has_merge_field = suffix == f"in-flight\t{target}\n" and (
        target == "-" or MERGE_TARGET_ID.fullmatch(target) is not None)
    legacy = all(entry.startswith("main\t") and MERGE_TARGET_ID.fullmatch(entry[5:]) for entry in suffix.splitlines())
    record_matches = saved.startswith(rows) and (has_merge_field or legacy)
    if unpublished and (not record_matches or unpublished.keys().isdisjoint(row.commit for row in checkpoints)):
        raise Refusal("unpushed merges do not have a matching batch record; "
                      + "reset the merge branch to its pushed tip, then start a fresh batch")
    if record_matches:
        record.resuming = True
        if legacy:
            record.merge_state = LegacyBatchMerge()
        elif target != "-":
            record.merge_state = InFlightBatchMerge(target)
        if not isinstance(record.merge_state, NoBatchMerge) and isinstance(
                read_open_merge(merge_head_path(checkout)), NoOpenMerge):
            record.store_merge_state(NO_BATCH_MERGE)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        record.store_merge_state(NO_BATCH_MERGE)
    try:
        yield record
    finally:
        if not record.retirement_attempted and not unpushed_checkpoint_merges(production, upstream):
            if isinstance(record.merge_state, NoBatchMerge) or isinstance(
                    read_open_merge(merge_head_path(checkout)), NoOpenMerge):
                record.retire()


def run_batch(requests: tuple[MergeRequest, ...]) -> int:
    cancel_prior = any(request.cancel_prior for request in requests)
    requests = tuple(request._replace(cancel_prior=cancel_prior) for request in requests)
    production = requests[0].production
    require_merge_branch(production)
    upstream = f"refs/remotes/origin/{production.merge_branch}"
    try:
        _ = good(git(production.checkout, "fetch", "--no-prune", "--no-write-fetch-head", "origin",
                     f"+refs/heads/{production.merge_branch}:{upstream}"), "batch")
    except (OSError, checkpoint_gate.Stop) as error:
        stop = checkpoint_gate.Stop(
            "batch", "failed", f"fetch failed: {error}", "send this checkpoint again when origin is available")
        return finish_batch_stop(stop, batch_stop_notices(requests, stop), record_results=False)
    with batch_resume_record(requests, upstream) as resume_record:
        checkpoints = resume_record.checkpoints
        unpublished = resume_record.unpublished_merges
        by_commit = {row.commit: row for row in checkpoints}
        if (0 < len(unpublished) < len(checkpoints) and unpublished.keys() <= by_commit.keys()
                and any(checkpoint_gate.runs_merge_gate(command) for command in checkpoints[0].merge_tests)
                and not any(batch_ancestor(production.checkout, row.commit, upstream) for row in checkpoints)):
            # Complete the gate batch before testing or publishing its rows.
            merged_rows = tuple(by_commit[commit] for commit in unpublished)
            remaining = tuple(row for row in checkpoints if row.commit not in unpublished)
            return run_pending_batch((*merged_rows, *remaining), resume_record, unpublished=unpublished)
        requests, resumed = resume_merged_items(resume_record)
        if not requests:
            return resumed
        return run_pending_batch(requests, resume_record) or resumed


def drop_batch_row(red: BatchRed) -> int:
    """Rebuild and gate the survivors after one row reproduces the red modules."""
    rows = tuple(notice.request for notice in red.notices)
    try:
        owner = probe_batch_rows(red)
    except (checkpoint_gate.Stop, OSError, ValueError) as error:
        if isinstance(error, checkpoint_gate.Stop) and error.state == "cancelled":
            return finish_batch_stop(error, batch_stop_notices(rows, error), red.rollback)
        checkpoint_gate.report("batch probe", "held", str(error))
        owner = None
    if owner is None:
        return finish_batch_stop(red, red.notices, red.rollback)
    try:
        kept = build_record.keep_log(red.log, build_store.host_name(), time.time(),
                                     f"batch-drop-{red.tested_tip}-{owner.commit}")
        kept_log = (build_store.root() / kept).resolve()
    except (OSError, ValueError) as error:
        checkpoint_gate.report("batch", "warning", f"could not keep the batch test log: {error}")
        return finish_batch_stop(red, red.notices, red.rollback)

    record = red.rollback.record
    warnings: list[str] = []
    try:
        reset_batch(red.rollback, warnings)
    except (checkpoint_gate.Stop, OSError, ValueError) as error:
        checkpoint_gate.report("batch", "held", f"could not prepare the survivor batch: {error}")
        return finish_batch_stop(red, red.notices)
    finally:
        for warning in warnings:
            checkpoint_gate.report("batch", "warning", warning)

    detail = f"red modules {', '.join(sorted(red.modules))} on {red.tested_tip}. Test log: {kept_log}"
    if error := land_result_error(owner, landed=False, step="red", state="dropped", detail=detail):
        checkpoint_gate.report("batch", "held", f"could not record the drop of {owner.unit.name} phase {owner.phase}: {error}")
        return finish_batch_stop(red, red.notices)
    checkpoint_gate.report("batch", "dropped", f"{owner.unit.name} phase {owner.phase} ({owner.commit[:12]})")
    print(f"send {owner.unit.name}: phase {owner.phase} ({owner.commit[:12]}): {detail}. "
          + "Repair the red modules and send a new checkpoint", flush=True)
    survivors = tuple(row for row in rows if row.commit != owner.commit)
    try:
        land_runs.update_landing([item.commit for item in survivors], [f"{item.unit.name} {item.phase}" for item in survivors])
    except (OSError, ValueError, TypeError) as error:
        checkpoint_gate.report("batch", "warning", f"could not update the running land: {error}")
    return run_pending_batch(survivors, record, dropped_commit=owner.commit)


def run_pending_batch(requests: tuple[MergeRequest, ...], resume_record: BatchResumeRecord, *,
                       dropped_commit: str = "", unpublished: dict[str, str] | None = None) -> int:
    production = requests[0].production
    head = good(git(production.checkout, "rev-parse", "HEAD"), "merge")
    start_tip = head
    if unpublished:
        first_merge = next(iter(unpublished.values()))
        start_tip = good(git(production.checkout, "rev-parse", f"{first_merge}^1"), "merge")
    published_tip = good(git(production.checkout, "rev-parse", f"origin/{production.merge_branch}"), "merge")
    rollback = ResetBatchCheckout(production, start_tip, head, published_tip, resume_record, dropped_commit)
    first = requests[0]
    log = first.scratch / f"merge_{first.commit[:7]}_test.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    _ = log.write_text("", encoding="utf-8")
    return run_batch_attempt(requests, log, rollback, unpublished=unpublished or {})


class NotMerged(NamedTuple):
    pass


class MergedNotPushed(NamedTuple):
    merge_head: str


class MergedAndPushed(NamedTuple):
    merge_head: str


class ChildHandoffRequest(NamedTuple):
    production: Production
    child: Production
    checkpoint: str
    push: ValidateAndPush | GitPush
    merge_tests: tuple[tuple[str, ...], ...]
    started: str
    delivery: NotMerged | MergedNotPushed | MergedAndPushed
    promotion: production_doc.NoPromotion | production_doc.PromoteTo
    root_self: bool
    flakes: frozenset[str]
    flaky_tests: frozenset[str]
    scratch: Path


def child_runner(action: str, *arguments: str) -> str:
    script = checkpoint_gate.tool("showrunners.py", Path(__file__).with_name("showrunners.py"), "MERGE_CHECKPOINT_SHOWRUNNERS")
    result = subprocess.run(
        [PYTHON, script, action, *arguments],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if result.returncode:
        raise Refusal(result.stderr.strip() or f"showrunners.py {action} exited {result.returncode}")
    return result.stdout.strip()


def child_request(args: argparse.Namespace) -> ChildHandoffRequest:
    production = read_production(cast(str, args.production))
    name = owner_for(cast(str, args.from_child)).name
    try:
        parent = child_runner("parent", name)
    except Refusal as error:
        raise Refusal(f"{name} is not attached under {production.name}") from error
    root_self = cast(bool, args.root_self)
    if root_self and parent:
        raise Refusal(f"{name} reports to {parent}; --root-self needs a root")
    if not root_self and parent != production.name:
        raise Refusal(f"{name} is not attached under {production.name}")
    child = read_production(name)
    lines = child.doc.read_text().splitlines()
    if production_field(lines, "Kind") != "style":
        raise Refusal(f"{name} is not a style production")
    branch_point = production_field(lines, "Branch point").removeprefix("refs/heads/")
    if branch_point != production.merge_branch.removeprefix("refs/heads/"):
        raise Refusal(f"{name} follows branch point {branch_point}, not {production.merge_branch}")
    checkout = production.checkout
    common = good(git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"), "input")
    if common != good(git(child.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"), "input"):
        raise Refusal(f"{name} is not in the parent's repository")
    checkpoint = good(git(checkout, "rev-parse", "--verify", f"{cast(str, args.checkpoint)}^{{commit}}"), "ancestry")
    if git(child.checkout, "merge-base", "--is-ancestor", checkpoint, child.merge_branch).returncode:
        raise Refusal(f"checkpoint {checkpoint[:7]} is not on {name}'s merge branch {child.merge_branch}")
    already = git(checkout, "merge-base", "--is-ancestor", checkpoint, production.merge_branch).returncode == 0
    policy = read_merge_policy(production)
    delivery: NotMerged | MergedNotPushed | MergedAndPushed = NotMerged()
    if already:
        merge_head = merge_evidence(production, checkpoint)
        pushed = pushed_state(production, checkpoint)
        delivery = MergedAndPushed(merge_head) if isinstance(pushed, PushedTip) else MergedNotPushed(merge_head)
    scratch = Path(os.environ.get("MERGE_CHECKPOINT_SCRATCH") or tempfile.gettempdir()).expanduser()
    return ChildHandoffRequest(production, child, checkpoint, policy.push, policy.merge_tests,
                               cast(str, args.started), delivery, policy.promotion,
                               root_self, policy.flakes, policy.flaky_tests, scratch)


def merge_evidence(production: ProductionDocument, checkpoint: str) -> str:
    checkout, branch = production.checkout, production.merge_branch
    commits = good(git(checkout, "rev-list", "--reverse", "--first-parent", branch), "merge").splitlines()
    index = bisect.bisect_left(commits, True, key=lambda commit:
                              git(checkout, "merge-base", "--is-ancestor", checkpoint, commit).returncode == 0)
    return commits[index] if index < len(commits) else good(git(checkout, "rev-parse", branch), "merge")


def record_handoff_merge(request: ChildHandoffRequest, merge_head: str) -> None:
    try:
        script = checkpoint_gate.tool("style_ledger.py", Path(__file__).resolve().parents[1] / "style/style_ledger.py",
                      "MERGE_CHECKPOINT_STYLE_LEDGER")
        _ = sys.stdout.flush()
        result = subprocess.run(
            [PYTHON, script, "handoff", "merged", "--repo", str(request.child.checkout),
             "--checkpoint", request.checkpoint, "--merge-commit", merge_head],
            capture_output=True, text=True, check=False, timeout=30,
        )
        if result.returncode:
            detail = failure_detail(result)
            print("hand-off ledger: " + detail.replace("\n", "; "), flush=True)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("hand-off ledger: " + str(error).replace("\n", "; "), flush=True)


def acknowledge_handoff(request: ChildHandoffRequest, merge_head: str) -> None:
    script = checkpoint_gate.tool("send.py", session_send.SEND_SCRIPT, "MERGE_CHECKPOINT_SEND")
    try:
        address = child_runner("address", request.child.name)
        if not address:
            raise Refusal(f"no address for {request.child.name}")
        result = subprocess.run(session_send.send_argv("--to", address, "--from", request.production.name,
            "--text", f"hand-off {request.checkpoint[:7]} merged as {merge_head[:7]}.",
            "--timeout", "10", send_script=Path(script), python_interpreter=PYTHON,
        ), capture_output=True, text=True, check=False, timeout=15)
        if result.returncode:
            raise Refusal(f"send.py: {failure_detail(result)}")
    except (Refusal, staff.StaffRefused, OSError, ValueError, subprocess.TimeoutExpired) as error:
        print("hand-off acknowledgment: " + " ".join(str(error).splitlines()), flush=True)


def finish_handoff(request: ChildHandoffRequest, merge_head: str) -> None:
    if request.root_self:
        print(f"root: run style_ledger.py reconcile to mark {request.checkpoint[:7]} merged", flush=True)
    else:
        record_handoff_merge(request, merge_head)
        acknowledge_handoff(request, merge_head)


def handoff_tests_pending_path(checkout: Path, checkpoint: str) -> Path:
    """Keep untested handoffs across cancelled runs, scratch cleanup, and new sessions."""
    path = good(git(checkout, "rev-parse", "--git-path", f"checkpoint-tests-pending/{checkpoint}"), "test")
    return checkout / path


def promote_handoff(request: ChildHandoffRequest, merge_head: str, tip: str) -> None:
    """Retry interrupted promotion; a push receipt is not a promotion receipt."""
    if isinstance(request.promotion, production_doc.NoPromotion):
        return
    done = checkpoint_step_record(request.production.checkout, merge_head, "handoff-promotion")
    if not done.exists():
        promote(request, tip, local_only=True)


def finish_handoff_promotion(request: ChildHandoffRequest, merge_head: str) -> None:
    done = checkpoint_step_record(request.production.checkout, merge_head, "handoff-promotion")
    if isinstance(request.promotion, production_doc.PromoteTo) and not done.exists():
        finish_promotion(request)
        done.parent.mkdir(parents=True, exist_ok=True)
        done.touch()


def notify_published_style(production: ProductionDocument, merge_head: str, tip: str) -> None:
    """Notify style once for each published tip, including failed promotions."""
    done = checkpoint_step_record(production.checkout, merge_head, "style-notice-" + tip)
    if done.exists():
        return
    if notify_style(production.checkout, production.merge_branch, tip):
        done.parent.mkdir(parents=True, exist_ok=True)
        done.touch()


def record_handoff_publication(request: ChildHandoffRequest, merge_head: str, tip: str) -> None:
    record_pushed_merges(request.production, merge_head, request.checkpoint, tip,
                         phase_id="", issue=f"{request.child.name} hand-off")
    notify_published_style(request.production, merge_head, tip)


def record_handoff_completion(request: ChildHandoffRequest, merge_head: str, tip: str) -> None:
    entry = f"{request.child.name} style hand-off ({request.checkpoint}) merged as {merge_head}; pushed at {tip}"
    log = request.production.log
    if log.exists() and entry in log.read_text(encoding="utf-8"):
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as output:
        _ = output.write(f"- {datetime.now(request.production.zone):%H:%M %Z}: {entry}\n")


def run_handoff(request: ChildHandoffRequest) -> int:
    checkout, branch, checkpoint = request.production.checkout, request.production.merge_branch, request.checkpoint
    delivery = request.delivery
    before_merge_tip = ""

    def retest(paths: tuple[str, ...], test_tip: str) -> None:
        try:
            with child_handoff_wait(request.child.name, checkpoint):
                _ = checkpoint_tests(request.production, paths, checkpoint, request.merge_tests,
                                     request.flakes, request.scratch, test_tip, request.flaky_tests,
                                     before_merge_tip=before_merge_tip)
        except (OSError, ValueError) as error:
            undo_unpushed_merge(request.production, test_tip, before_merge_tip)
            raise checkpoint_gate.Stop("red", "failed", str(error), "repair the test logging failure and send a new checkpoint") from error

    if isinstance(delivery, MergedAndPushed):
        print(f"hand-off {checkpoint[:7]} already merged as {delivery.merge_head[:7]}", flush=True)
        pushed = pushed_state(request.production, checkpoint)
        tip = pushed.tip if isinstance(pushed, PushedTip) else delivery.merge_head
        before_merge_tip = tip
        try:
            follow_main = isinstance(request.promotion, production_doc.PromoteTo) and not checkpoint_step_record(
                checkout, delivery.merge_head, "handoff-promotion").exists()
            with tested_main_push(request.production, tip, retest, tip, follow_main=follow_main) as tested_tip:
                if tested_tip != tip:
                    tip = push_git(request.production, False)
                promote_handoff(request, delivery.merge_head, tip)
            finish_handoff_promotion(request, delivery.merge_head)
        finally:
            record_handoff_publication(request, delivery.merge_head, tip)
        record_publication_changes(request.production, report_unchanged=False)
        record_handoff_completion(request, delivery.merge_head, tip)
        finish_handoff(request, delivery.merge_head)
        return 0
    current = good(git(checkout, "branch", "--show-current"), "merge")
    if current != branch:
        raise Refusal(f"merge checkout is on {current}, expected {branch}")
    staged = good(git(checkout, "diff", "--cached", "--name-only"), "merge")
    if staged:
        raise Refusal(f"staged paths in merge checkout: {staged.replace(chr(10), ', ')}")
    pending_tests = handoff_tests_pending_path(checkout, checkpoint)
    paths: tuple[str, ...] = ()
    if isinstance(delivery, NotMerged):
        paths = merge_paths(checkout, branch, checkpoint)
        _ = good(git(checkout, "merge-tree", "--write-tree", branch, checkpoint), "conflicts")
        subject = f"Merge {request.child.name} hand-off ({checkpoint[:7]}) into {branch}"
        pending_tests.parent.mkdir(parents=True, exist_ok=True)
        pending_tests.touch()
        _ = good(git(checkout, "merge", "--no-ff", "-m", subject, checkpoint), "merge")
        merge_head = good(git(checkout, "rev-parse", "HEAD"), "merge")
    else:
        merge_head = delivery.merge_head
        if pending_tests.exists():
            paths = merge_paths(checkout, f"{merge_head}^1", checkpoint)
    if isinstance(delivery, NotMerged) or pending_tests.exists():
        paths = prepare_checkpoint(request.production, merge_head)
        test_tip = good(git(checkout, "rev-parse", "HEAD"), "test")
        before_merge_tip = good(git(checkout, "rev-parse", f"{merge_head}^1"), "merge")
        retest(paths, test_tip)
        pending_tests.unlink()
    tip = ""
    try:
        test_tip = good(git(checkout, "rev-parse", "HEAD"), "test")
        before_merge_tip = good(git(checkout, "rev-parse", f"{merge_head}^1"), "push")
        with tested_main_push(request.production, test_tip, retest, before_merge_tip):
            with push_rollback(request.production, before_merge_tip if isinstance(delivery, NotMerged) else ""):
                if isinstance(request.push, GitPush):
                    tip = push_git(request.production, False)
                else:
                    tip = push_validate(request.production,
                                        f"ci({request.production.name}): format fixes after {request.child.name} style hand-off",
                                        False, request.scratch / f"merge_{checkpoint[:7]}_push.log")
            if isinstance(request.promotion, production_doc.NoPromotion):
                promote(request, tip)
            promote_handoff(request, merge_head, tip)
        finish_handoff_promotion(request, merge_head)
    finally:
        if tip:
            record_handoff_publication(request, merge_head, tip)
    record_publication_changes(request.production, report_unchanged=False)
    record_handoff_completion(request, merge_head, tip)
    finish_handoff(request, merge_head)
    print(f"hand-off {checkpoint[:7]} merged as {merge_head[:7]}", flush=True)
    return 0


def checkpoint_parser(*, batch_item: bool = False) -> argparse.ArgumentParser:
    """Define each item flag once for single commands and batch lines."""
    parser = argparse.ArgumentParser(description=__doc__)
    if not batch_item:
        _ = parser.add_argument("--production", required=True)
        _ = parser.add_argument("--batch", type=Path)
    _ = parser.add_argument("unit", nargs="?")
    _ = parser.add_argument("phase", nargs="?")
    _ = parser.add_argument("hash", nargs="?")
    _ = parser.add_argument("--from-child")
    _ = parser.add_argument("--checkpoint")
    _ = parser.add_argument("--root-self", action="store_true")
    _ = parser.add_argument("--shrink", action="store_true")
    _ = parser.add_argument("--as-built", action="store_true")
    _ = parser.add_argument("--trailer", action="append", default=[])
    _ = parser.add_argument("--review-trial")
    _ = parser.add_argument("--delivers")
    _ = parser.add_argument("--cancel-prior", action="store_true")
    _ = parser.add_argument("--bump", action="store_true",
                            help="only a user's bump: push a code checkpoint before its background gate")
    _ = parser.add_argument("--started", required=batch_item)
    _ = parser.add_argument("--regime", choices=("after", "trial"), default="after")
    _ = parser.add_argument("--holds", type=int, default=0)
    _ = parser.add_argument("--merge-defects", type=int, default=0)
    _ = parser.add_argument("--excluded")
    _ = parser.add_argument("--scratch")
    return parser


def require_unit_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if cast(bool, args.root_self) or cast(str | None, args.checkpoint):
        parser.error("--root-self and --checkpoint require --from-child")
    if not all((cast(str | None, args.unit), cast(str | None, args.phase), cast(str | None, args.hash))):
        parser.error("unit, phase and hash are required for a unit checkpoint")


def batch_requests(path: Path, production: str) -> tuple[MergeRequest, ...]:
    """Read all item arguments before any batch merge starts."""
    requests: list[MergeRequest] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        words = shlex.split(line)
        parser = checkpoint_parser(batch_item=True)
        args = parser.parse_args(words)
        if cast(bool, args.bump) or cast(str | None, args.from_child) is not None or cast(str | None, args.checkpoint) is not None:
            raise Refusal(f"{path}:{number}: --bump, --from-child and --checkpoint are not batch items")
        args.production = production
        require_unit_arguments(parser, args)
        requests.append(request_from(args)._replace(batch_argument_line=shlex.join(words)))
    if not requests:
        raise Refusal("the batch file has no items")
    if any(request.production.checkout != requests[0].production.checkout for request in requests):
        raise Refusal("batch items use different merge checkouts; submit a batch for each checkout")
    return tuple(requests)


def main() -> int:
    parser = checkpoint_parser()
    args = parser.parse_args()
    batch = cast(Path | None, args.batch)
    if batch is not None:
        if any((cast(str | None, args.unit), cast(str | None, args.phase), cast(str | None, args.hash))):
            parser.error("--batch is separate from positional item arguments")
        try:
            requests = batch_requests(batch, cast(str, args.production))
            production = requests[0].production
            owners = [f"{item.unit.name} {item.phase}" for item in requests]
            with merge_lock(production.checkout, production.name), land_runs.landing([item.commit for item in requests], owners):
                return run_batch(requests)
        except (Refusal, staff.StaffRefused, OSError, ValueError, checkpoint_gate.Stop) as error:
            checkpoint_gate.report("input", "failed", str(error))
            return 1
    if not cast(str | None, args.started):
        parser.error("the following arguments are required: --started")
    if cast(str | None, args.from_child):
        try:
            _ = user_bump(args)
            if not cast(str | None, args.checkpoint):
                raise Refusal("--from-child requires --checkpoint")
            if any((cast(str | None, args.unit), cast(str | None, args.phase), cast(str | None, args.hash),
                    cast(bool, args.shrink), cast(bool, args.as_built), cast(bool, args.bump), cast(str | None, args.review_trial),
                    cast(str | None, args.delivers), cast(bool, args.cancel_prior),
                    cast(list[str], args.trailer),
                    cast(str, args.regime) != "after", cast(int, args.holds), cast(int, args.merge_defects),
                    cast(str | None, args.excluded), cast(str | None, args.scratch))):
                raise Refusal("--from-child is separate from unit checkpoint arguments")
            handoff = child_request(args)
            with merge_lock(handoff.production.checkout, handoff.production.name):
                return run_handoff(handoff)
        except checkpoint_gate.Stop as error:
            if error.state in ("cancelled", "held"):
                checkpoint_gate.report(error.step, error.state, error.detail.replace("\n", "; "))
                print(error.message, flush=True)
                return checkpoint_gate.TREE_CHANGED_STATUS if error.state == "cancelled" else 1
            print("merge_checkpoint: " + " ".join(str(error).splitlines()), flush=True)
            return 1
        except (Refusal, staff.StaffRefused, OSError, ValueError, subprocess.TimeoutExpired) as error:
            print("merge_checkpoint: " + " ".join(str(error).splitlines()), flush=True)
            return 1
    require_unit_arguments(parser, args)
    try:
        request = request_from(args)
        owners = [f"{request.unit.name} {request.phase}"]
        with merge_lock(request.production.checkout, request.production.name), land_runs.landing([request.commit], owners):
            return run(request)
    except staff.StaffUnreadable as error:
        checkpoint_gate.report("change review", "failed", f"{error}: the staff record is read before the push")
        return 1
    except (Refusal, staff.StaffRefused, OSError, ValueError) as error:
        checkpoint_gate.report("input", "failed", str(error))
        print(f"send {cast(str, args.unit)}: correct the checkpoint input and resend it")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

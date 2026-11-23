#!/usr/bin/env python3
"""Find a production's stranded work and tell its showrunner once about each new row.

Two rules, each with the `stranded_after_hours` limit from `config/production.conf`:
1. An approved hash of a completed unit phase that is not on `origin/main`: the checkpoint commit, else the reviewed commit.
2. A unit tree with staged or changed tracked files, no commit or file change, and no live run.

`/showrunner:units` prints the rows. The agent-notes part `stranded` sends the new rows.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import NamedTuple, TypedDict, cast
from zoneinfo import ZoneInfo

from scripts.agents import detached_runs, remote
from scripts.build_queue.queue_snapshot import QueueSnapshot, read_queue_snapshot
from scripts.buildlog import store
from scripts.direct import pass_record, plan_history, step_record
from scripts.lib import conf, file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.showrunner_state import state_root
from scripts.lib.user_zone import short_time
from scripts.message import session_send
from scripts.production import board_reads, land_queue, showrunners, staff, unit_lookup, waits_on
from scripts.production.production_doc import Refusal, read_production
from scripts.worktree import finished_worktrees, worktrees

SENDER = "stranded"
SENT_FILE = "stranded_sent.json"
DROPPED_FILE = "stranded_dropped.json"
STATUS_WORKERS = 8
FULL_HASH = re.compile(r"[0-9a-f]{7,40}")
# A backticked hash, or the second hash of a backticked range; it has at least one letter.
REPLY_HASH = re.compile(r"`(?:[0-9a-f]{7,40}\.\.\.?)?((?=[0-9]*[a-f])[0-9a-f]{7,40})`")


class StrandedKind(Enum):
    APPROVED_HASH = "approved hash"
    TREE = "tree"


class RowCheck(Enum):
    """Whether the pass read every source that can clear the row. Only a complete row is sent."""

    COMPLETE = "every source read"
    LIVE_RUNS_UNREAD = "live runs unread"
    COMMIT_UNREAD = "commit not in a local repository"


class StrandedRow(NamedTuple):
    """One stranded item. The key stays the same while the item stays stranded."""

    unit: str
    kind: StrandedKind
    key: str
    text: str
    age: float
    check: RowCheck = RowCheck.COMPLETE


class TreeNotRecorded(Enum):
    NOT_RECORDED = "the event names no reviewed tree"


class Approval(NamedTuple):
    phase: str
    commit: str
    tree: Path | TreeNotRecorded
    at: float


class NoApproval(Enum):
    NO_APPROVAL = "the phase has no approved hash"


class NoCommit(Enum):
    NO_COMMIT = "the review names no commit"


class HomeNotLocal(Enum):
    NOT_LOCAL = "the home tree is not a folder on this machine"


class NoFolder(Enum):
    NO_FOLDER = "the run names no folder"


class NoRunUnit(Enum):
    NO_UNIT = "the run names no unit"


class NoRunOwner(Enum):
    NO_OWNER = "the run names no staff row"


class LiveRun(NamedTuple):
    """A live run: its folder, the unit name that the build queue records, and the staff row of a detached run."""

    folder: Path | NoFolder
    unit: str | NoRunUnit
    owner: staff.StaffId | NoRunOwner = NoRunOwner.NO_OWNER


class LiveRuns(NamedTuple):
    runs: tuple[LiveRun, ...]
    check: RowCheck


class LiveRunsNotRead(Enum):
    NOT_READ = "the live runs are not read yet"


class NoRepository(Enum):
    NO_REPOSITORY = "neither the phase tree nor the home tree is a readable repository with the commit"


class TreeChanges(NamedTuple):
    files: int
    changed_at: float


class NoTreeChanges(Enum):
    CLEAN = "the tree has no staged or changed tracked file"
    STATUS_UNREAD = "git status failed"


def limit_seconds() -> float:
    return conf.read("production").number("stranded_after_hours") * 3600


def age_text(seconds: float) -> str:
    return f"{int(seconds // 3600)} h"


def time_text(at: float, *, now: float, zone: ZoneInfo) -> str:
    moment = datetime.fromtimestamp(at, UTC)
    return f"{short_time(moment, now=now, zone=zone)} {moment.astimezone(zone).tzname() or zone.key}"


def reply_commit(text: str) -> str | NoCommit:
    """The last backticked hash before the end of the verdict line."""
    verdict = pass_record.VERDICT_WORD.search(text)
    if verdict is None:
        return NoCommit.NO_COMMIT
    end = text.find("\n", verdict.end())
    found = cast(list[str], REPLY_HASH.findall(text[:len(text) if end < 0 else end]))
    return found[-1] if found else NoCommit.NO_COMMIT


def is_review(event: dict[str, object]) -> bool:
    return (event.get("event_type") == "pass_finished" and event.get("pass_kind") == "review"
            and event.get("status") == "completed")


def reviewed_tree(event: dict[str, object]) -> Path | TreeNotRecorded:
    tree = pass_record.string(event.get("reviewed_tree"))
    return Path(tree) if tree.startswith("/") else TreeNotRecorded.NOT_RECORDED


def review_verdict(event: dict[str, object]) -> pass_record.ReviewVerdict:
    """The recorded verdict; an older event without one uses its reply file."""
    recorded = event.get("verdict")
    if isinstance(recorded, str):
        return next((verdict for verdict in pass_record.ReviewVerdict if verdict.value == recorded),
                    pass_record.ReviewVerdict.NO_VERDICT)
    return pass_record.file_verdict(pass_record.string(event.get("reply_file")))


def review_commit(event: dict[str, object]) -> str | NoCommit:
    """The recorded HEAD of the reviewed tree; an older event without one uses its reply text."""
    if "verdict" in event:
        commit = pass_record.string(event.get("reviewed_commit"))
        return commit if FULL_HASH.fullmatch(commit) else NoCommit.NO_COMMIT
    try:
        return reply_commit(Path(pass_record.string(event.get("reply_file"))).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return NoCommit.NO_COMMIT


def phase_approval(phase: str, history: list[dict[str, object]]) -> Approval | NoApproval:
    """The approved hash of one completed phase, when the newest completed review with a verdict approves.

    A review lens with no verdict word does not hide the verdicts of the other lenses.
    With a checkpoint, it is the last checkpoint commit, before or after that review.
    Without a checkpoint, it is the HEAD that the review approved.
    """
    reviews = [event for event in history
               if is_review(event) and review_verdict(event) is not pass_record.ReviewVerdict.NO_VERDICT]
    if not reviews or review_verdict(reviews[-1]) is not pass_record.ReviewVerdict.APPROVE:
        return NoApproval.NO_APPROVAL
    reviewed_at = pass_record.number(reviews[-1].get("timestamp_epoch"))
    checkpoints = [event for event in history if event.get("event_type") == "checkpoint"]
    if checkpoints:
        commit = pass_record.string(checkpoints[-1].get("checkpoint_commit"))
        if not commit:
            return NoApproval.NO_APPROVAL
        at = max(reviewed_at, pass_record.number(checkpoints[-1].get("timestamp_epoch"), reviewed_at))
        return Approval(phase, commit, reviewed_tree(reviews[-1]), at)
    commit = review_commit(reviews[-1])
    if isinstance(commit, NoCommit):
        return NoApproval.NO_APPROVAL
    return Approval(phase, commit, reviewed_tree(reviews[-1]), pass_record.number(reviews[-1].get("timestamp_epoch")))


def local_home(member: staff.StaffRow) -> Path | HomeNotLocal:
    home = member.worktree
    if isinstance(home, staff.PresentWorktree) and home.machine == remote.local_machine() and home.path.is_dir():
        return home.path
    return HomeNotLocal.NOT_LOCAL


def approval_evidence(reads: board_reads.BoardReads, approval: Approval,
                      member: staff.StaffRow) -> board_reads.RepositoryEvidence | NoRepository:
    """The evidence of the repository that holds the approved commit: the phase tree's, else the unit home's.

    A removed phase tree is not a checkout, so the unit home answers. The production checkout does not.
    """
    for folder in (approval.tree, local_home(member)):
        if isinstance(folder, Path):
            source = reads.source(folder)
            if isinstance(source, Path) and waits_on.commit_of(source, approval.commit):
                return reads.at(source)
    return NoRepository.NO_REPOSITORY


def approved_rows(members: Sequence[staff.StaffRow], reads: board_reads.BoardReads, *,
                  now: float, limit: float, zone: ZoneInfo) -> list[StrandedRow]:
    """Rule 1: approved hashes older than the limit that are not on main, for each run and phase.

    When no local repository holds the commit, a closed run gives no row.
    For an open run, a phase merge in the production checkout clears the approval.
    Else the row shows with `commit not in a local repository` and is not sent.
    A drop record of the unit and the commit removes the row.
    """
    found: dict[str, StrandedRow] = {}
    dropped = read_dropped()
    for member in members:
        for run in reads.history.get(member.id, []):
            phases = board_reads.completed_phase_histories(reads.run(run.path).events)
            for phase, history in sorted(phases.items()):
                approval = phase_approval(phase, history)
                if isinstance(approval, NoApproval) or now - approval.at < limit:
                    continue
                key = f"approved:{member.id}:{phase}:{approval.commit}"
                if key in found or is_dropped(dropped, member.id, approval.commit):
                    continue
                evidence = approval_evidence(reads, approval, member)
                if isinstance(evidence, NoRepository) and not (
                        isinstance(member.run, staff.OpenRun) and member.run.path.name == run.path.stem):
                    continue
                merges = (reads.at(reads.repository) if isinstance(evidence, NoRepository) else evidence).phase_merges
                merge = merges.get((member.name, phase))
                if merge is not None and merge.timestamp_epoch >= approval.at:
                    continue
                if isinstance(evidence, NoRepository):
                    check, state = RowCheck.COMMIT_UNREAD, RowCheck.COMMIT_UNREAD.value
                elif evidence.on_main(approval.commit):
                    continue
                else:
                    queued = land_queue.land_state(evidence.land, approval.commit)
                    check = RowCheck.COMPLETE
                    state = "not on main, " + ("in the merge line"
                                               if isinstance(queued, (land_queue.LandRunning, land_queue.InLine))
                                               else "not in the merge line")
                age = now - approval.at
                found[key] = StrandedRow(
                    member.name, StrandedKind.APPROVED_HASH, key,
                    f"{member.name} {phase} approved {approval.commit[:10]} {time_text(approval.at, now=now, zone=zone)}, "
                    + f"{state} ({age_text(age)})", age, check)
    return list(found.values())


def unit_trees(members: Sequence[staff.StaffRow], snapshot: staff.StaffSnapshot,
               reads: board_reads.BoardReads) -> dict[Path, staff.StaffRow]:
    """Each unit tree and its member: the home trees, and the linked trees that belong to one member.

    A home's parent folder holds unrecorded trees of the unit only when no other staff home is inside it.
    So a shared root such as `~/worktrees` gives no unit its unrecorded trees.
    A linked tree that is the home of another staff row is not a tree of the member.
    Each repository lists its trees once, and each tree's checkout record is read once.
    """
    staff_homes = {home for row in snapshot.rows if isinstance(home := local_home(row), Path)}
    by_name = {member.name: member for member in members}
    homes = {member.name: home for member in members if isinstance(home := local_home(member), Path)}
    owned = {home: by_name[name] for name, home in homes.items()}
    units_at: dict[Path, set[str]] = {}
    for name, home in homes.items():
        source = reads.source(home)
        if isinstance(source, Path):
            units_at.setdefault(source, set()).add(name)
    for source, names in units_at.items():
        folders = {homes[name].parent: name for name in names
                   if not any(other != homes[name] and other.is_relative_to(homes[name].parent) for other in staff_homes)}
        for tree in worktrees.linked_trees(source):
            if tree.path in staff_homes or tree.path in owned or not tree.path.is_dir():
                continue
            unit = finished_worktrees.tree_unit(tree.path, folders, snapshot)
            if isinstance(unit, str) and unit in names:
                owned[tree.path] = by_name[unit]
    return owned


def tree_changes(tree: Path) -> TreeChanges | NoTreeChanges:
    """Staged or changed tracked files and the newer of the last commit and the newest file change."""
    status = worktrees.git(tree, "--no-optional-locks", "status", "--porcelain=v1", "-z",
                           "--untracked-files=no", check=False)
    if status.returncode:
        return NoTreeChanges.STATUS_UNREAD
    entries = status.stdout.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) > 3:
            paths.append(entry[3:])
            # A rename or copy entry is followed by its source path.
            index += entry[0] in "RC"
    if not paths:
        return NoTreeChanges.CLEAN
    committed = worktrees.git(tree, "log", "-1", "--format=%ct", check=False).stdout.strip()
    changed_at = float(committed) if committed.isdecimal() else 0.0
    for path in paths:
        try:
            changed_at = max(changed_at, (tree / path).lstat().st_mtime)
        except OSError:
            continue
    return TreeChanges(len(paths), changed_at)


def queue_runs(now: float) -> tuple[LiveRun, ...] | LiveRunsNotRead:
    """The running and waiting steps of the build queue snapshot that the admission service writes at each pass.

    A missing snapshot, or one older than `silence_seconds` in `config/build_admission.conf`, is not read.
    `head_stall.current_stall` uses the same limit.
    """
    snapshot = read_queue_snapshot(store.root() / "admission")
    if (not isinstance(snapshot, QueueSnapshot)
            or now - snapshot.written_at > conf.read("build_admission").number("silence_seconds")):
        return LiveRunsNotRead.NOT_READ
    steps = [*((step.folder, step.unit) for step in snapshot.inputs.running),
             *((waiter.get("folder", ""), waiter.get("unit", "")) for _, waiter in snapshot.inputs.ordered)]
    return tuple(LiveRun(Path(folder) if folder.startswith("/") else NoFolder.NO_FOLDER, unit or NoRunUnit.NO_UNIT)
                 for folder, unit in steps)


def live_runs(snapshot: staff.StaffSnapshot, now: float) -> LiveRuns:
    """Verify step records, build queue steps and the detached runs of every unit, read once."""
    runs = [LiveRun(record.repo, NoRunUnit.NO_UNIT) for record in step_record.live_records()]
    check = RowCheck.COMPLETE
    queued = queue_runs(now)
    if isinstance(queued, LiveRunsNotRead):
        check = RowCheck.LIVE_RUNS_UNREAD
    else:
        runs.extend(queued)
    folders = {member.run.path: member.id for member in staff.active(snapshot, "unit")
               if isinstance(member.run, staff.OpenRun)}
    try:
        identities = frozenset(row.id for row in snapshot.rows)
        found = detached_runs.live_runs_in_folders({folder: identities for folder in folders})
        runs.extend(LiveRun(NoFolder.NO_FOLDER, NoRunUnit.NO_UNIT, folders[Path(run["session_dir"])])
                    for run in found if Path(run["session_dir"]) in folders)
    except OSError:
        check = RowCheck.LIVE_RUNS_UNREAD
    return LiveRuns(tuple(runs), check)


class LiveRunReader:
    """Read the live runs at the first use. One read serves each production in a pass."""

    def __init__(self, snapshot: staff.StaffSnapshot, now: float) -> None:
        self.snapshot: staff.StaffSnapshot = snapshot
        self.now: float = now
        self.found: LiveRuns | LiveRunsNotRead = LiveRunsNotRead.NOT_READ

    def read(self) -> LiveRuns:
        if isinstance(self.found, LiveRunsNotRead):
            self.found = live_runs(self.snapshot, self.now)
        return self.found


def tree_rows(members: Sequence[staff.StaffRow], snapshot: staff.StaffSnapshot, reads: board_reads.BoardReads,
              live_reader: LiveRunReader, *, now: float, limit: float) -> list[StrandedRow]:
    """Rule 2: unit trees with changed files older than the limit and no live run.

    When a live-run source is unreadable, the rows show with `live runs unread` and are not sent.
    """
    owned = unit_trees(members, snapshot, reads)
    # Each status is one git process; run them side by side.
    with ThreadPoolExecutor(max_workers=STATUS_WORKERS) as pool:
        statuses = list(pool.map(tree_changes, owned))
    old = [(member, tree, changes) for (tree, member), changes in zip(owned.items(), statuses, strict=True)
           if isinstance(changes, TreeChanges) and now - changes.changed_at >= limit]
    if not old:
        return []
    live = live_reader.read()

    def inside(folder: Path | NoFolder, tree: Path) -> bool:
        return isinstance(folder, Path) and folder.is_relative_to(tree)

    found: list[StrandedRow] = []
    for member, tree, changes in old:
        if any(inside(run.folder, tree) or run.owner == member.id
               or run.unit == member.name and not any(inside(run.folder, other) for other in owned)
               for run in live.runs):
            continue
        age = now - changes.changed_at
        files = f"{changes.files} changed file{'' if changes.files == 1 else 's'}"
        runs = "no live run" if live.check is RowCheck.COMPLETE else live.check.value
        found.append(StrandedRow(member.name, StrandedKind.TREE, f"tree:{tree}",
                                 f"{member.name} tree {tree.name}: {files}, no commit for {age_text(age)}, {runs}",
                                 age, live.check))
    return found


class PassReads:
    """The reads that one pass shares across productions: the unit run history and the live runs."""

    def __init__(self, snapshot: staff.StaffSnapshot, now: float) -> None:
        self.history: dict[staff.StaffId, list[board_reads.UnitRunStart]] = board_reads.unit_run_history(snapshot)
        self.live: LiveRunReader = LiveRunReader(snapshot, now)


def rows(production: str, now: float, reads: board_reads.BoardReads | PassReads | None = None) -> list[StrandedRow]:
    """Each stranded row of the production's units.

    The board passes its `BoardReads`. The send part passes one `PassReads` for all productions.
    """
    snapshot = plan_history.staff_record()
    document = read_production(production, record=snapshot)
    members = unit_lookup.doc_units(document.doc, snapshot)
    if isinstance(reads, board_reads.BoardReads):
        board, live = reads, LiveRunReader(snapshot, now)
    else:
        shared = PassReads(snapshot, now) if reads is None else reads
        board, live = board_reads.BoardReads(document.checkout, shared.history), shared.live
    limit = limit_seconds()
    return [*approved_rows(members, board, now=now, limit=limit, zone=document.zone),
            *tree_rows(members, snapshot, board, live, now=now, limit=limit)]


class DroppedRecord(TypedDict):
    """A hash dropped on purpose: the unit staff id, the full commit, who dropped it, why, and when (UTC epoch)."""

    unit_id: staff.StaffId
    commit: str
    by: str
    why: str
    at: float


class DropRefused(Exception):
    """The drop command cannot write a record."""


def dropped_path() -> Path:
    """One record file for all productions."""
    return state_root() / DROPPED_FILE


class NotARecord(Enum):
    NOT_A_RECORD = "the entry is not a drop record"


def dropped_record(entry: object) -> DroppedRecord | NotARecord:
    if not isinstance(entry, dict):
        return NotARecord.NOT_A_RECORD
    fields = cast(dict[str, object], entry)
    unit_id, commit, by, why = (fields.get(name) for name in ("unit_id", "commit", "by", "why"))
    at = fields.get("at")
    if (isinstance(unit_id, str) and isinstance(commit, str) and isinstance(by, str) and isinstance(why, str)
            and isinstance(at, int | float) and not isinstance(at, bool)):
        return DroppedRecord(unit_id=staff.StaffId(unit_id), commit=commit, by=by, why=why, at=float(at))
    return NotARecord.NOT_A_RECORD


def read_dropped() -> list[DroppedRecord]:
    """The drop records for rule 1; a missing or unreadable file has none, and a malformed entry is skipped."""
    try:
        value = cast(object, json.loads(dropped_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []
    return [record for entry in (cast(list[object], value) if isinstance(value, list) else [])
            if not isinstance(record := dropped_record(entry), NotARecord)]


def read_dropped_strictly(path: Path) -> list[DroppedRecord]:
    """The drop records for a new drop. A file that is not a list of valid records refuses the drop, so no record is lost."""
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise DropRefused(f"{path} is not readable JSON; repair it first: {error}") from error
    entries = cast(list[object], value) if isinstance(value, list) else []
    records = [record for entry in entries if not isinstance(record := dropped_record(entry), NotARecord)]
    if not isinstance(value, list) or len(records) != len(entries):
        raise DropRefused(f"{path} is not a list of drop records; repair it first")
    return records


def is_dropped(records: Sequence[DroppedRecord], unit_id: staff.StaffId, commit: str) -> bool:
    """A record names this unit and this commit. An approval from reply text can name an abbreviated hash."""
    return any(record["unit_id"] == unit_id and record["commit"].startswith(commit.lower()) for record in records)


def drop(unit: str, commit: str, *, by: str, why: str, now: float) -> DroppedRecord:
    """Record that a unit commit is dropped on purpose, so rule 1 does not show it again."""
    if not by.strip() or not why.strip():
        raise DropRefused("--by and --why must not be empty")
    if not FULL_HASH.fullmatch(commit.lower()):
        raise DropRefused(f"--commit {commit} is not a hash of 7 to 40 hex characters")
    members = [row for row in staff.active(staff.snapshot(), "unit") if row.name == unit]
    if len(members) != 1:
        raise DropRefused(f"no unit named {unit}" if not members else f"{len(members)} units named {unit}")
    home = local_home(members[0])
    if isinstance(home, HomeNotLocal):
        raise DropRefused(f"{unit}: {home.value}")
    resolved = worktrees.git(home, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}", check=False)
    if resolved.returncode or not FULL_HASH.fullmatch(full := resolved.stdout.strip()):
        raise DropRefused(f"{unit}: no commit {commit} in {home}")
    record = DroppedRecord(unit_id=members[0].id, commit=full, by=by.strip(), why=why.strip(), at=now)
    path = dropped_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(path.with_name(path.name + ".lock")):
        write_text_atomic(path, json.dumps([*read_dropped_strictly(path), record], indent=2) + "\n")
    return record


def sent_path(owner: staff.StaffRow) -> Path:
    return state_root() / owner.id / SENT_FILE


def read_sent(path: Path) -> frozenset[str]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return frozenset()
    return frozenset(key for key in cast(list[object], value) if isinstance(key, str)) if isinstance(value, list) else frozenset()


def send_notice(owner: staff.StaffRow, text: str) -> bool:
    """Send the text once; `send.py` skips the same text inside `units_anomaly_repeat_minutes`.

    So a pass that stops after a delivered send, before it writes the sent record, does not send the text again.
    """
    key = "stranded:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    repeat_minutes = conf.read("production").number("units_anomaly_repeat_minutes")
    done = subprocess.run(session_send.send_argv("--to", owner.name, "--from", SENDER, "--text", text, "--key", key,
                                                 "--repeat-minutes", f"{repeat_minutes:g}", send_script=showrunners.SEND),
                          capture_output=True, text=True, check=False)
    return done.returncode == 0


def notify(owner: staff.StaffRow, found: Sequence[StrandedRow], *,
           send: Callable[[staff.StaffRow, str], bool] = send_notice) -> bool:
    """Send the complete rows whose key is new in one message; keep only the keys of present rows.

    A row with unread live runs or an unread commit is not sent, and its key is not added. False on a failed send.
    """
    path = sent_path(owner)
    sent = read_sent(path)
    current = {row.key for row in found}
    kept = sent & current
    new = [row for row in found if row.key not in sent and row.check is RowCheck.COMPLETE]
    delivered = not new or send(owner, "\n".join(["Stranded work. Run /showrunner:units for the list.",
                                                 *(f"- {row.text}" for row in new)]))
    if new and delivered:
        kept |= {row.key for row in new}
    if kept != sent:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(sorted(kept)) + "\n")
    return delivered


def notify_all(now: float) -> list[str]:
    """Tell each production that has an update timer folder; return one line per production that failed.

    The unit run history and the live runs are read once for all productions.
    """
    failures: list[str] = []
    snapshot = staff.snapshot()
    with plan_history.history_snapshot(snapshot):
        shared = PassReads(snapshot, now)
        for instance in sorted(showrunners.NOTIFIER_STATE_DIR.glob("showrunner-*")):
            if not instance.is_dir():
                continue
            owner = snapshot.by_id(staff.StaffId(instance.name.removeprefix("showrunner-")))
            if owner is None or owner.kind != "showrunner":
                failures.append(f"{instance.name}: no showrunner staff row")
                continue
            try:
                if not notify(owner, rows(owner.name, now, shared), send=send_notice):
                    failures.append(f"{owner.name}: the send failed")
            except (Refusal, OSError, ValueError, staff.StaffRefused) as error:
                failures.append(f"{owner.name}: {' '.join(str(error).splitlines())}")
    return failures


def main(arguments: Sequence[str] = ()) -> int:
    """With no arguments, send the new rows. `drop` records a hash dropped on purpose."""
    if not arguments:
        failures = notify_all(pass_record.now_epoch())
        for failure in failures:
            print(f"stranded list not sent for {failure}", file=sys.stderr)
        return 1 if failures else 0
    parser = argparse.ArgumentParser(prog="stranded.py", description="With no command, send the new stranded rows.")
    commands = parser.add_subparsers(dest="command", required=True)
    dropping = commands.add_parser("drop", help="record a hash dropped on purpose; the stranded list skips it")
    _ = dropping.add_argument("--unit", required=True, help="the unit staff name")
    _ = dropping.add_argument("--commit", required=True, help="the commit; it must be in the unit home repository")
    _ = dropping.add_argument("--by", required=True, help="who dropped the hash")
    _ = dropping.add_argument("--why", required=True, help="why the hash is dropped")
    parsed = parser.parse_args(arguments)
    unit, commit, by, why = (cast(str, getattr(parsed, name)) for name in ("unit", "commit", "by", "why"))
    try:
        record = drop(unit, commit, by=by, why=why, now=pass_record.now_epoch())
    except (DropRefused, OSError, staff.StaffRefused) as error:
        print(f"stranded drop refused: {error}", file=sys.stderr)
        return 2
    print(f"dropped {unit} {record['commit'][:10]} by {record['by']}: {record['why']} ({dropped_path()})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Find the state of the work a queued item waits on, from local git refs and unit run records.

The work is a commit, a branch, or `<unit>/<phase>`. The check reads the local `origin/main` and does not fetch.
"""
from __future__ import annotations

import subprocess
import re
from collections.abc import Callable, Collection
from pathlib import Path
from typing import NamedTuple

from scripts.direct import phase_waits, plan_history
from scripts.production import staff


class OnMain(NamedTuple):
    commit: str


class NotOnMain(NamedTuple):
    commit: str


class PhaseOpen(NamedTuple):
    """The phase runs; eta_at is its newest stated ETA as epoch seconds, or None."""

    eta_at: float | None


class PhaseSent(NamedTuple):
    """The phase sent a commit that is not on main, with its recorded unit wait."""

    commit: str
    wait: phase_waits.UnitWait | phase_waits.NoUnitWait = phase_waits.NO_UNIT_WAIT


class PhaseNotStarted(NamedTuple):
    pass


class NotFound(NamedTuple):
    pass


WaitState = OnMain | NotOnMain | PhaseOpen | PhaseSent | PhaseNotStarted | NotFound
RunReader = Callable[[Path], list[dict[str, object]]]


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def on_main(repo: Path, commit: str) -> bool:
    return git(repo, "merge-base", "--is-ancestor", commit, "origin/main").returncode == 0


def carried(repo: Path, commit: str) -> list[str]:
    """The commits a land of this commit takes: the commit and its ancestors that are not on main."""
    result = git(repo, "rev-list", commit, "--not", "origin/main")
    return result.stdout.split() if result.returncode == 0 else []


def commit_of(repo: Path, ref: str) -> str:
    """The commit a hash or branch names: the ref itself, else origin/<ref>; empty when neither names a commit."""
    for name in (ref, f"origin/{ref}"):
        result = git(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if result.returncode == 0:
            return result.stdout.strip()
    return ""


class PhaseMergeSubject(NamedTuple):
    """The unit, phase, and checkpoint hash named by a merge subject."""

    unit: str
    phase: str
    checkpoint_commit: str


class NoPhaseMergeSubject(NamedTuple):
    """The subject does not record a phase merge."""


def merged_phase_subject(subject: str) -> PhaseMergeSubject | NoPhaseMergeSubject:
    """Read the phase and checkpoint only from a merge subject's leading words."""
    match = re.match(r"^Merge (\S+) phase (\S+) \(([0-9a-fA-F]{7,40})\)", subject)
    return PhaseMergeSubject(match[1], match[2], match[3].lower()) if match else NoPhaseMergeSubject()


class PhaseMerge(NamedTuple):
    """The commit, time, and checkpoint hash of a phase's newest merge on main."""

    commit: str
    timestamp_epoch: int
    checkpoint_commit: str


class CommitIdentity(NamedTuple):
    """The author time and subject that remain the same when Git copies a commit."""

    author_epoch: int
    subject: str


def main_commit_identities(repo: Path) -> frozenset[CommitIdentity]:
    """Read the identities of main's non-merge commits in one Git call."""
    result = git(repo, "log", "origin/main", "--no-merges", "--format=%at%x00%s")
    found: set[CommitIdentity] = set()
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            recorded, separator, subject = line.partition("\0")
            if separator and recorded.isdecimal():
                found.add(CommitIdentity(int(recorded), subject))
    return frozenset(found)


def copied_on_main(repo: Path, commit: str, identities: Collection[CommitIdentity]) -> bool:
    """Check whether main contains a non-merge commit with this checkpoint's identity."""
    if not identities:
        return False
    result = git(repo, "show", "-s", "--format=%at%x00%s", commit)
    recorded, separator, subject = result.stdout.rstrip("\n").partition("\0")
    return (result.returncode == 0 and bool(separator) and recorded.isdecimal()
            and CommitIdentity(int(recorded), subject) in identities)


def landed_phases(repo: Path) -> dict[tuple[str, str], PhaseMerge]:
    """Read the newest merge for each unit and phase from main in one Git call."""
    result = git(repo, "log", "origin/main", "--merges", "--format=%H%x00%ct%x00%s")
    found: dict[tuple[str, str], PhaseMerge] = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            commit, separator, rest = line.partition("\0")
            recorded, dated, subject = rest.partition("\0")
            named = merged_phase_subject(subject)
            if separator and dated and recorded.isdecimal() and isinstance(named, PhaseMergeSubject):
                key = (named.unit, named.phase)
                merge = PhaseMerge(commit, int(recorded), named.checkpoint_commit)
                if key not in found or merge.timestamp_epoch > found[key].timestamp_epoch:
                    found[key] = merge
    return found


def landed_phase(repo: Path, unit: str, phase: str) -> str:
    """The land merge on main of this unit phase, or empty."""
    result = git(repo, "log", "origin/main", "--merges", "--fixed-strings", "--format=%H%x00%s",
                 f"--grep=Merge {unit} phase {phase} (")
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            commit, separator, subject = line.partition("\0")
            named = merged_phase_subject(subject)
            if separator and isinstance(named, PhaseMergeSubject) and (named.unit, named.phase) == (unit, phase):
                return commit
    return ""


def phase_state(repo: Path, member: staff.StaffRow, phase: str, read_run: RunReader) -> WaitState:
    events = read_run(plan_history.run_events_path(member.run.path)) if isinstance(member.run, staff.OpenRun) else []
    instance = next((event.get("phase_instance_id") for event in reversed(events)
                     if event.get("event_type") == "phase_started" and event.get("phase_id") == phase), None)
    phase_events = [event for event in events if instance is not None and event.get("phase_instance_id") == instance]
    resumed = False
    for event in reversed(phase_events):
        kind = event.get("event_type")
        if kind == "phase_finished":
            resumed = False
            break
        if kind == "checkpoint" and event.get("checkpoint_commit"):
            break
        if kind in ("pass_started", "activity_started"):
            resumed = True
    if not resumed:
        if landed := landed_phase(repo, member.name, phase):
            return OnMain(landed)
        if instance is None:
            return PhaseNotStarted()
        for event in reversed(phase_events):
            commit = event.get("checkpoint_commit")
            if event.get("event_type") == "checkpoint" and isinstance(commit, str) and commit:
                if on_main(repo, commit):
                    return OnMain(commit)
                wait = next((wait for wait in phase_waits.unit_waits(events) if wait.target == phase), phase_waits.NO_UNIT_WAIT)
                return PhaseSent(commit, wait)
    eta = next((event.get("eta_at") for event in reversed(events)
                if event.get("event_type") == "eta_stated" and event.get("eta_phase_id") == phase), None)
    return PhaseOpen(float(eta) if isinstance(eta, int | float) and not isinstance(eta, bool) else None)


def state(ref: str, *, repo: Path, snapshot: staff.StaffSnapshot, read_run: RunReader) -> WaitState:
    """Find the state of the named work; a `<unit>/<phase>` whose unit is an active unit is a phase."""
    unit, slash, phase = ref.partition("/")
    member = next((row for row in staff.active(snapshot, "unit") if row.name == unit), None) if slash and phase else None
    if member is not None:
        return phase_state(repo, member, phase, read_run)
    commit = commit_of(repo, ref)
    if not commit:
        return NotFound()
    return OnMain(commit) if on_main(repo, commit) else NotOnMain(commit)

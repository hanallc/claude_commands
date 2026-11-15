"""Find the state of the work a queued item waits on, from local git refs and unit run records.

The work is a commit, a branch, or `<unit>/<phase>`. The check reads the local `origin/main` and does not fetch.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from scripts.production import staff


class OnMain(NamedTuple):
    commit: str


class NotOnMain(NamedTuple):
    commit: str


class PhaseOpen(NamedTuple):
    """The phase runs; eta_at is its newest stated ETA as epoch seconds, or None."""

    eta_at: float | None


class PhaseSent(NamedTuple):
    """The phase sent a checkpoint that is not on main."""

    commit: str


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


def commit_of(repo: Path, ref: str) -> str:
    """The commit a hash or branch names: the ref itself, else origin/<ref>; empty when neither names a commit."""
    for name in (ref, f"origin/{ref}"):
        result = git(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}")
        if result.returncode == 0:
            return result.stdout.strip()
    return ""


def landed_phase(repo: Path, unit: str, phase: str) -> str:
    """The land merge on main of this unit phase, or empty."""
    result = git(repo, "log", "origin/main", "--merges", "--fixed-strings", "--format=%H", "-1",
                 f"--grep=Merge {unit} phase {phase} (")
    return result.stdout.strip() if result.returncode == 0 else ""


def phase_state(repo: Path, member: staff.StaffRow, phase: str, read_run: RunReader) -> WaitState:
    if landed := landed_phase(repo, member.name, phase):
        return OnMain(landed)
    if not isinstance(member.run, staff.OpenRun):
        return PhaseNotStarted()
    events = read_run(member.run.path)
    instance = next((event.get("phase_instance_id") for event in reversed(events)
                     if event.get("event_type") == "phase_started" and event.get("phase_id") == phase), None)
    if instance is None:
        return PhaseNotStarted()
    for event in reversed(events):
        commit = event.get("checkpoint_commit")
        if event.get("phase_instance_id") == instance and event.get("event_type") == "checkpoint" and isinstance(commit, str) and commit:
            return OnMain(commit) if on_main(repo, commit) else PhaseSent(commit)
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

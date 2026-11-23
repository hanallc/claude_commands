"""Reads that the units board and the stranded list share: run history, phase checkpoints and merge evidence."""
from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from scripts.direct import plan_history
from scripts.lib import git_files, unit_runs
from scripts.production import land_queue, staff, waits_on

WORK_STARTS = frozenset({"phase_started", "pass_started", "activity_started"})


class UnitRunStart(NamedTuple):
    """The recorded start of a unit run."""

    started: float
    path: Path
    start: dict[str, object]


def unit_run_history(snapshot: staff.StaffSnapshot) -> dict[staff.StaffId, list[UnitRunStart]]:
    """Order each unit's runs with its current staff run first."""
    snapshot = staff.StaffSnapshot(staff.active(snapshot, "unit"))
    runs: dict[staff.StaffId, list[UnitRunStart]] = {}
    for path, started_event in plan_history.run_starts():
        try:
            owner = unit_runs.run_owner(started_event, snapshot)
        except staff.StaffUnreadable:
            continue
        if isinstance(owner, unit_runs.RunOwnerUnrecorded):
            continue
        started = next((float(value) for value in (started_event.get("run_started_at"), started_event.get("timestamp_epoch"))
                        if isinstance(value, int | float) and not isinstance(value, bool)), 0.0)
        runs.setdefault(owner.id, []).append(UnitRunStart(started, path, started_event))
    for owner in snapshot.rows:
        if owner.id in runs:
            current_run = owner.run.path.name if isinstance(owner.run, staff.OpenRun) else ""
            runs[owner.id].sort(key=lambda run: (run.path.stem == current_run, run.started, run.path.name), reverse=True)
    return runs


class CheckpointTimeUnrecorded(NamedTuple):
    """The checkpoint event has no recorded time."""


class SentCommit(NamedTuple):
    """The commit and checkpoint time after the phase's last work start."""

    commit: str
    timestamp_epoch: float | CheckpointTimeUnrecorded = CheckpointTimeUnrecorded()


class SentCommitUnrecorded(NamedTuple):
    """The phase sent work without a recorded commit."""


class NoSentWork(NamedTuple):
    """The phase has no sent work after its last work start."""


PhaseSentWork = SentCommit | SentCommitUnrecorded | NoSentWork


def phase_sent_work(events: list[dict[str, object]]) -> PhaseSentWork:
    """Find the sent commit after the phase's last work start."""
    for event in reversed(events):
        kind = event.get("event_type")
        if kind == "checkpoint":
            commit = event.get("checkpoint_commit")
            at = event.get("timestamp_epoch")
            recorded = float(at) if isinstance(at, int | float) and not isinstance(at, bool) else CheckpointTimeUnrecorded()
            return SentCommit(commit, recorded) if isinstance(commit, str) and commit else SentCommitUnrecorded()
        if kind in WORK_STARTS:
            return NoSentWork()
    return NoSentWork()


def completed_phase_histories(events: Sequence[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    """Read the events of each completed phase's latest instance."""
    instances: dict[str, str] = {}
    phases: dict[str, list[dict[str, object]]] = {}
    completed: set[str] = set()
    for event in events:
        kind = event.get("event_type")
        phase = str(event.get("phase_id") or instances.get(str(event.get("phase_instance_id", "")), ""))
        if kind == "phase_started":
            instances[str(event.get("phase_instance_id", ""))] = phase
            phases[phase] = []
            completed.discard(phase)
        if phase in phases and phase not in completed:
            phases[phase].append(event)
            if kind == "phase_finished" and event.get("status") == "completed":
                completed.add(phase)
    return {phase: history for phase, history in phases.items() if phase in completed}


def completed_unit_phases(events: Sequence[dict[str, object]]) -> dict[str, PhaseSentWork]:
    """Read completed phases and their sent work from their latest instances."""
    return {phase: phase_sent_work(history) for phase, history in completed_phase_histories(events).items()}


class CheckpointRepositoryUnavailable(NamedTuple):
    """The unit has no readable checkout or recorded merge destination."""


class RepositoryEvidence:
    """One repository's merge records and cached checkpoint decisions for this render."""

    def __init__(self, repository: Path) -> None:
        self.repository: Path = repository
        self.land: land_queue.LandView = land_queue.land_view(repository)
        self.phase_merges: dict[tuple[str, str], waits_on.PhaseMerge] = waits_on.landed_phases(repository)
        self.identities: frozenset[waits_on.CommitIdentity] = waits_on.main_commit_identities(repository)
        self.main_states: dict[str, bool] = {}

    def on_main(self, commit: str) -> bool:
        """Share the ancestry and copied-checkpoint decision across all board uses."""
        key = commit.lower()
        if key not in self.main_states:
            self.main_states[key] = (waits_on.on_main(self.repository, commit)
                                     or waits_on.copied_on_main(self.repository, commit, self.identities))
        return self.main_states[key]

    def names_checkpoint(self, unit: str, phase: str, commit: str) -> bool:
        merge = self.phase_merges.get((unit, phase))
        return merge is not None and commit.lower().startswith(merge.checkpoint_commit.lower())


class BoardReads:
    """The unit runs, each run's events and the evidence of each repository, read once for one board render.

    The caller passes the unit run history, so one history read can serve several productions.
    """

    def __init__(self, repository: Path, history: dict[staff.StaffId, list[UnitRunStart]]) -> None:
        self.repository: Path = repository
        self.history: dict[staff.StaffId, list[UnitRunStart]] = history
        self.runs: dict[Path, plan_history.CompleteRunHistory | plan_history.IncompleteRunHistory] = {}
        self.sources: dict[Path, Path] = {}
        self.evidence: dict[Path, RepositoryEvidence] = {}
        try:
            self.sources[git_files.locate(repository).common_dir] = repository
        except (OSError, ValueError, RuntimeError):
            pass

    def run(self, path: Path) -> plan_history.CompleteRunHistory | plan_history.IncompleteRunHistory:
        """The events of one run file; one read for each file."""
        if path not in self.runs:
            self.runs[path] = plan_history.read_run_history(path)
        return self.runs[path]

    def source(self, path: Path) -> Path | CheckpointRepositoryUnavailable:
        """The one checkout that stands for the Git repository of this path."""
        if path == self.repository:
            return self.repository
        try:
            checkout = git_files.locate(path)
        except (OSError, ValueError, RuntimeError):
            return CheckpointRepositoryUnavailable()
        return self.sources.setdefault(checkout.common_dir, checkout.toplevel)

    def at(self, source: Path) -> RepositoryEvidence:
        """The evidence of the repository at this source checkout; one read for each source."""
        if source not in self.evidence:
            self.evidence[source] = RepositoryEvidence(source)
        return self.evidence[source]

"""Give checkpoint lands precedence over claude_commands tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.build_queue import admission_records as records, admission_wire as wire
from scripts.buildlog import repository
from scripts.production import land_queue, land_runs


_REPOSITORY = "https://github.com/natepiano/claude_commands"
TEST_STEPS = frozenset({"merge-gate", "targeted-test", "test", "t", "nextest"})


@dataclass(frozen=True)
class QueuedLandingTests:
    """The land line has work, and no live land has a test reference."""


@dataclass(frozen=True)
class RunningLandingTests:
    """These references belong to live landing processes."""
    references: frozenset[land_runs.LandingTestReference]


@dataclass(frozen=True)
class NoProtectedLandingTests:
    """The land line and live records have no work to protect."""


type LandingTests = QueuedLandingTests | RunningLandingTests | NoProtectedLandingTests


def read() -> LandingTests:
    """Read the line before claims and registrations to cover the handoff."""
    try:
        queued = land_queue.read_items()
    except (OSError, ValueError):
        queued = land_queue.NoLine()
    running = land_runs.running_test_references()
    if running:
        return RunningLandingTests(frozenset(reference for reference in running
            if isinstance(reference, land_runs.LandingTestReference)))
    return QueuedLandingTests() if not isinstance(queued, land_queue.NoLine) and queued else NoProtectedLandingTests()


def waits_for_request(protected: LandingTests,
                      request: wire.AdmitRequest | wire.LaunchRequest | wire.PlacementRequest) -> bool:
    profile, identity = request.profile, request.identity
    if isinstance(protected, NoProtectedLandingTests) or profile.step not in TEST_STEPS:
        return False
    if isinstance(protected, RunningLandingTests) and identity.landing_test in protected.references:
        return False
    source = profile.repository
    if isinstance(source, records.RepositoryIdentity):
        if source.key == _REPOSITORY:
            return True
        if "://" in source.key:
            return False
    folder = profile.worktree
    if isinstance(request, wire.AdmitRequest):
        folder = request.cwd
    elif isinstance(request, wire.PlacementRequest) and isinstance(request.local, wire.LocalAdmissionContext):
        folder = request.local.cwd
    if not Path(folder).is_absolute():
        return False
    common = repository.git_common_dir(folder)
    if common is None:
        return False
    origin = Path(repository.source_git_dir(common)).parent
    return repository.repository_key(str(origin)) == _REPOSITORY

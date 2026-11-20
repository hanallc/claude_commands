"""Give checkpoint lands precedence over claude_commands tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.build_queue import admission_records as records, admission_wire as wire
from scripts.buildlog import repository
from scripts.lib import git_files
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
type TestRequest = wire.AdmitRequest | wire.LaunchRequest | wire.PlacementRequest


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
    if not isinstance(queued, land_queue.NoLine) and queued and land_runs.runner_is_live():
        return QueuedLandingTests()
    return NoProtectedLandingTests()


def _file_revision(path: Path) -> tuple[int, int, int]:
    try:
        status = path.stat()
        return status.st_ino, status.st_mtime_ns, status.st_ctime_ns
    except OSError:
        return 0, 0, 0


class PassProtection:
    """Reuse the landing records while their files have the same revision."""

    def __init__(self) -> None:
        self.revision: tuple[tuple[int, int, int], ...] = ()
        self.protected: LandingTests = NoProtectedLandingTests()

    def read(self) -> LandingTests:
        revision = tuple(_file_revision(path) for path in (land_queue.line_path(), *land_runs.protection_paths()))
        if revision != self.revision:
            self.revision = revision
            self.protected = read()
        return self.protected


def request_folder(request: TestRequest) -> str:
    if isinstance(request, wire.AdmitRequest):
        return request.cwd
    if isinstance(request, wire.PlacementRequest) and isinstance(request.local, wire.LocalAdmissionContext):
        return request.local.cwd
    return request.profile.worktree


def is_test_repository(source: records.RepositoryIdentity | records.UnknownRepository, folder: str) -> bool:
    if isinstance(source, records.RepositoryIdentity) and "://" in source.key:
        return source.key == _REPOSITORY
    if not Path(folder).is_absolute():
        return False
    seen: set[Path] = set()
    while True:
        try:
            checkout = git_files.locate(Path(folder))
            origin = Path(repository.source_git_dir(str(checkout.common_dir)))
            root = origin.parent if origin != checkout.common_dir else checkout.toplevel
            if root in seen:
                return False
            seen.add(root)
            key = repository.repository_key(str(root))
            if key == _REPOSITORY:
                return True
            if not Path(key).is_absolute():
                return False
            folder = key if Path(key).is_dir() else key + ".git"
        except (OSError, ValueError):
            return False

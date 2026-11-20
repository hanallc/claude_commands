"""Types shared by the gate CLI and remote routes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Literal, Protocol

from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob
from scripts.test_times import gate_record


class SuiteMode(Enum):
    """Choose the step and whether the runner selects affected modules."""
    MERGE_GATE = "merge-gate"
    TARGETED_TEST = "targeted-test"


class GateJobKind(Enum):
    TEST_MODULE = auto()
    TYPE_CHECK = auto()
    RUNNER_FAILURE = auto()


@dataclass(frozen=True)
class AllModules:
    """Run every discovered module."""


@dataclass(frozen=True)
class SelectedModules:
    """Run the paths selected on natedev, including a certified empty selection."""
    paths: tuple[str, ...]


@dataclass(frozen=True)
class TreeSelection:
    """Hold selected paths and the Git tree that the selection read."""
    paths: tuple[str, ...]
    tree: str


class ModuleCoverage(Enum):
    COMPLETE = auto()
    SUBSET = auto()


ALL_MODULES = AllModules()


@dataclass(frozen=True)
class GateJobResult:
    suite: str
    module: str
    returncode: int
    seconds: float
    output: str
    killed_for: str = ""
    tripwire_tripped: bool = False
    kind: GateJobKind = GateJobKind.TEST_MODULE
    cpu_seconds: float = 0.0
    times: gate_record.ModuleTimes | gate_record.NoModuleTimes | gate_record.TimingNotApplicable = gate_record.NoModuleTimes("job has no timing data")
    peak_mib: float = 0.0
    restart: Literal["", "hold"] = ""
    memory_stopped: bool = False
    wall_clock: gate_record.WallClockSpan | gate_record.WallClockNotRecorded = field(
        default=gate_record.WallClockNotRecorded("job did not run"), kw_only=True)


@dataclass(frozen=True)
class GateRun:
    jobs: tuple[GateJobResult, ...]
    seconds: float
    peak_memory_mib: float
    tests_reused: bool = False
    # A valid affected selection may have no test modules at all.
    selection_certified: bool = False
    executions: tuple[GateJobResult, ...] = ()
    time_limit_report: str = ""
    coverage: ModuleCoverage = field(default=ModuleCoverage.COMPLETE, kw_only=True)

    @property
    def passed(self) -> bool:
        return ((bool(self.jobs) or self.tests_reused or self.selection_certified)
                and not self.time_limit_report and all(job.returncode == 0 for job in self.jobs))


class CloudGateRoute(Protocol):
    """Run AWS tests and identify the checked-out tree."""
    def route(self, repo: Path, *, mode: SuiteMode, full: bool,
              module_time_limit_s: float, selected: AllModules | TreeSelection = ALL_MODULES,
              gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED) -> GateRun | str: ...
    def working_tree(self, repo: Path) -> tuple[str, set[str]]: ...


@dataclass(frozen=True)
class MacGateCancellation:
    """The descriptor becomes readable when the caller cancels or exits."""
    caller_fd: int


@dataclass(frozen=True)
class MacGateExcludeFileMissing:
    """The checkout has no Mac gate exclude file."""


@dataclass(frozen=True)
class MacGateExclusions:
    """The exclude file lists these test paths."""
    paths: tuple[str, ...]


class MacGateRoute(Protocol):
    """Read the exclude list and run the modules that the Mac queue assigns."""
    def read_modules(self, repo: Path) -> MacGateExclusions | MacGateExcludeFileMissing: ...

    def route(self, repo: Path, *, mode: SuiteMode, full: bool, module_time_limit_s: float,
              selected: TreeSelection, cancellation: MacGateCancellation) -> GateRun | str: ...

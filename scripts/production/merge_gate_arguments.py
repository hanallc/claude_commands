"""Parse the merge gate command before the gate or its wrapper starts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob


# The slowest module took 387 s at load 152 on 2026-10-18.
# The limit is about 4.6 times that measurement.
DEFAULT_MODULE_TIME_LIMIT_S = 1800
LANDING_FIELDS = ("merge_branch", "unit_branch", "checkpoint", "previous_checkpoint", "unit", "phase")


@dataclass
class GateArguments(argparse.Namespace):
    """The command line supplies these gate arguments."""
    repo: str = ""
    merge_branch: str = ""
    unit_branch: str = ""
    checkpoint: str = ""
    previous_checkpoint: str = ""
    change_review: str = ""
    production: str = ""
    unit: str = ""
    phase: str = ""
    body: str = ""
    promote: bool = False
    main_repo: str = ""
    ssh: str = ""
    mac_host: str = ""
    mac_repo: str = ""
    module_time_limit_s: float = DEFAULT_MODULE_TIME_LIMIT_S
    tests_only: bool = False
    promote_only: bool = False
    full: bool = False
    nightly: bool = False
    modules: list[str] = field(default_factory=list)
    gate_job: StepJob | GateJobNotRecorded = GATE_JOB_NOT_RECORDED


def missing_landing_fields(args: GateArguments) -> list[str]:
    return [name.replace("_", "-") for name in LANDING_FIELDS if not getattr(args, name)]


def add_suite_arguments(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument("--repo", "--checkout", default=str(Path.cwd()))
    _ = parser.add_argument("--module-time-limit-s", type=float, default=DEFAULT_MODULE_TIME_LIMIT_S,
                            help="kill a module still running after this many seconds (default: 1800); slowest measured: 387 s at load 152")
    _ = parser.add_argument("--full", action="store_true", help="run all test modules even with a certified base")
    _ = parser.add_argument("--module", action="append", default=[], dest="modules",
                            help="run only this module, named as the gate prints it (suite/module), with no gate record")


def gate_parser(description: str | None = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description, allow_abbrev=False)
    add_suite_arguments(parser)
    for name in LANDING_FIELDS:
        _ = parser.add_argument(f"--{name.replace('_', '-')}", default="")
    _ = parser.add_argument("--body", default="Checkpoint verified by the complete merge gate.")
    _ = parser.add_argument("--change-review", default="", help="the change-review list to add rows to")
    _ = parser.add_argument("--production", default="",
                            help="the production staff name (default: the calling showrunner's)")
    _ = parser.add_argument("--promote", action="store_true")
    _ = parser.add_argument("--main-repo", "--main-checkout", default="~/.claude")
    _ = parser.add_argument("--ssh", default="ssh")
    _ = parser.add_argument("--mac-host", default="mac")
    _ = parser.add_argument("--mac-repo", default="~/.claude")
    _ = parser.add_argument("--tests-only", action="store_true")
    _ = parser.add_argument("--nightly", action="store_true", help="fetch and fully test origin/<merge-branch> in a shared clone")
    _ = parser.add_argument("--promote-only", action="store_true",
                            help="only promote origin/<merge-branch> to main and the Mac (needs --merge-branch)")
    return parser

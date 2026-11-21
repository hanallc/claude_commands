"""Read the display names, prose names and descriptions of build steps."""
from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, cast

from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, GateJobNotRecorded, StepJob as StepJob
from scripts.build_queue.snapshot_codec import RAW_ID, format_queue_name, public_queue_text, select_queue_name
from scripts.buildlog.parse import CARGO_BUILD_VERBS, CI_CARGO_FRONT, cargo_front_call
from scripts.lib import conf
from scripts.lib.proc_text import read_proc_text
from scripts.message.owner_role import OwnerRole


if TYPE_CHECKING:
    from scripts.build_hold.build_hold import ProcessIdentity


class ConfirmedLineage(Protocol):
    @property
    def processes(self) -> frozenset[ProcessIdentity]: ...


class CallerLineageReader(Protocol):
    @property
    def ConfirmedCallerLineage(self) -> type[ConfirmedLineage]: ...

    def read_caller_lineage(self, proc_root: Path, pid: int, start_time: str) -> object: ...


@dataclass(frozen=True)
class StepNames:
    settings: Mapping[str, str]

    def shown(self, step: str) -> str:
        return self.settings.get(f"names.{step}", step)

    def queue_name(self, step: str) -> str:
        """Use the verb or its recognized display form."""
        display = QUEUE_STEP_DISPLAY.get(step, step.replace("-", " "))
        if step == "run":
            return display
        return display if " ".join(self.shown(step).split()) == display else step

    def prose(self, step: str) -> str:
        return self.settings.get(f"prose.{step}", self.shown(step))

    def owner_template(self, step: str, unit: str, role: OwnerRole, *, ci: bool = False) -> str:
        """Read the template for this step and owner role."""
        if ci:
            prefix = "owned.ci-"
        elif role is OwnerRole.SHOWRUNNER:
            prefix = "owned.showrunner-"
        elif role is OwnerRole.UNIT and unit:
            prefix = "owned."
        else:
            return ""
        return self.settings.get(prefix + step, self.settings.get(prefix + "default", ""))

    def for_owner(self, step: str, unit: str, role: OwnerRole, *,
                  ci: bool = False, current_name: str = "", step_name: str = "") -> str:
        """Use the configured owner template when its fields are usable."""
        unit = public_queue_text(unit)
        shown = public_queue_text(step_name or self.shown(step))
        template = self.owner_template(step, unit, role, ci=ci)
        label = format_queue_name(template, unit=unit, step=shown)
        if label:
            return label
        owner = unit if template and role is OwnerRole.UNIT and not ci else ""
        return select_queue_name(current_name, " ".join(part for part in (owner, shown) if part))

    def about_template(self, job: StepJob, unit: str = "") -> tuple[str, str]:
        """Return the description template and the unit it names."""
        owner = job.unit or (unit if unit != "—" else "")
        key = f"about.{job.step}"
        template = self.settings.get(key, "")
        if job.step == "integrate" and job.branch:
            template = self.settings.get(f"{key}-with-branch", template)
        if "{unit}" in template and not owner:
            template = "" if unit == "—" and job.step == "test-all" else self.settings.get(f"{key}-without-unit", "")
        return template, owner

    def about(self, job: StepJob, unit: str = "") -> str:
        """Describe the job when a template applies."""
        template, unit = self.about_template(job, unit)
        return template.format(unit=unit, branch=job.branch, modules=job.modules)

    def changes(self, job: StepJob) -> str:
        """What the job changes: a branch name, or the default text."""
        template = self.settings.get(f"changes.{job.step}", self.settings.get("changes.default", ""))
        branch = job.branch or ("the landing" if job.step == "integrate" else "")
        return template.format(branch=branch)


@dataclass(frozen=True)
class OtherCommand:
    """The command is not a merge gate, a targeted test or verify.sh."""


OTHER_COMMAND = OtherCommand()


@dataclass(frozen=True)
class CopiesRunCommand:
    """The source and command of a copies.py run."""

    source: str
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class CommandUnreadable:
    """The process command is unavailable for this lifetime."""

    error: str


@dataclass(frozen=True)
class VerifyCommand:
    """The arguments that verify.sh reads for this command."""

    arguments: tuple[str, ...]


def verify_command(argv: Sequence[str]) -> VerifyCommand | OtherCommand:
    """Read verify arguments through the same interpreter rules as command_job."""
    program = _program(argv)
    if isinstance(program, OtherCommand) or program[0] != "verify.sh":
        return OTHER_COMMAND
    return VerifyCommand(tuple(program[1]))


def command_program(argv: Sequence[str]) -> str:
    """Name the script after interpreter arguments."""
    program = _program(argv)
    return "" if isinstance(program, OtherCommand) else program[0]



SCRIPT_SUFFIXES = frozenset({".awk", ".bash", ".fish", ".js", ".lua", ".mjs", ".pl", ".ps1", ".py", ".rb",
                             ".sh", ".ts", ".zsh"})
VERIFY_VERBS = frozenset({"check", "test", "lint", "doc", "build", "example", "final", "example-test"})
RECORDED_VERBS = VERIFY_VERBS | CARGO_BUILD_VERBS | {"cargo", "fmt", "sweep", "taplo", "rust-analyzer",
    "targeted-test", "merge-gate", "integrate", "test-all", "test-modules",
    "test-affected", "test-nightly", "fast-track", "toolchain-trial", "promote"}
QUEUE_STEP_DISPLAY = {"run": "cargo run", "test-modules": "test named"}
SHORT_OWNER_STEPS = {"merge-gate": "merge", "targeted-test": "targeted"}
GATE_PROGRAMS = {"merge-gate": "merge_gate.py", "targeted-test": "targeted_test.py"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def queue_identifier(text: str) -> bool:
    """Accept a package or repository name."""
    return bool(IDENTIFIER.fullmatch(text)) and not RAW_ID.search(text)


def is_recorded_step(text: str) -> bool:
    """Accept a build verb with or without a package name."""
    verb, _, package = text.partition(" ")
    return verb in RECORDED_VERBS and (not package or queue_identifier(package))


def script_file(word: str) -> bool:
    """Recognize a script path in a recorded command."""
    return bool(re.fullmatch(r"[\w./~+-]+", word)) and PurePath(word).suffix in SCRIPT_SUFFIXES


def command_script(argv: Sequence[str]) -> str:
    """Return the script file argument, if the command runs one."""
    program = _program(argv, keep_path=True)
    return program[0] if not isinstance(program, OtherCommand) and script_file(program[0]) else ""

def _option(arguments: Sequence[str], name: str) -> str:
    for index, argument in enumerate(arguments):
        if argument == name and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith(name + "="):
            return argument.split("=", 1)[1]
    return ""


def _count(arguments: Sequence[str], name: str) -> int:
    return sum(1 for argument in arguments if argument == name or argument.startswith(name + "="))


# A program that runs the script named after its options.
INTERPRETER = re.compile(r"python[0-9.]*|py|bash|sh")
# Interpreter options that take the next argument, or the rest of their cluster, as their value.
VALUE_OPTIONS = frozenset("XW")
LONG_VALUE_OPTIONS = frozenset({"--check-hash-based-pycs", "--init-file", "--rcfile"})


def _program(argv: Sequence[str], *, keep_path: bool = False) -> tuple[str, Sequence[str]] | OtherCommand:
    """The script a command runs and its arguments, after a python or shell interpreter.

    Code from -c or standard input ("-") runs no named script, so it is OtherCommand.
    """
    index = 0
    while index < len(argv):
        name = PurePath(argv[index]).name
        if not INTERPRETER.fullmatch(name):
            return (argv[index] if keep_path else name), argv[index + 1:]
        index += 1
        while index < len(argv) and argv[index].startswith("-"):
            option = argv[index]
            index += 1
            if option == "-":
                return OTHER_COMMAND
            if option.startswith("--"):
                index += 1 if option in LONG_VALUE_OPTIONS else 0
                continue
            for position, flag in enumerate(option[1:], start=2):
                rest = option[position:]
                if flag == "c":
                    return OTHER_COMMAND
                if flag == "m":
                    module = rest or (argv[index] if index < len(argv) else "")
                    if not module or keep_path:
                        return OTHER_COMMAND
                    return module.rsplit(".", 1)[-1] + ".py", argv[index + (0 if rest else 1):]
                if flag in VALUE_OPTIONS:
                    index += 0 if rest else 1
                    break
    return OTHER_COMMAND


def _verify_step(arguments: Sequence[str]) -> StepJob | OtherCommand:
    """verify.sh takes --session-dir <dir> before its command."""
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        index += 1 if "=" in arguments[index] else 2
    return StepJob(arguments[index]) if index < len(arguments) else OTHER_COMMAND


def copies_run_command(argv: Sequence[str]) -> CopiesRunCommand | OtherCommand:
    """Find the command after the copies.py run options."""
    program = _program(argv)
    if isinstance(program, OtherCommand) or program[0] != "copies.py" or not program[1] or program[1][0] != "run":
        return OTHER_COMMAND
    arguments = program[1]
    if "--" not in arguments:
        return OTHER_COMMAND
    separator = arguments.index("--")
    source = _option(arguments[:separator], "--repo")
    return CopiesRunCommand(source, tuple(arguments[separator + 1:])) if source else OTHER_COMMAND


def build_call_arguments(argv: Sequence[str]) -> Sequence[str]:
    """Read the Cargo or gate command from recorded arguments."""
    program = _program(argv)
    if isinstance(program, OtherCommand):
        return ()
    name, arguments = program
    if name == "queue_place.py" and arguments and arguments[0] == "admit":
        separator = next((index for index, word in enumerate(arguments) if word == "--"), None)
        return build_call_arguments(arguments[separator + 1:]) if separator is not None else ()
    if name == CI_CARGO_FRONT:
        return ("cargo", *cargo_front_call(arguments).arguments)
    if name == "cargo-tile-real":
        return ("cargo", *arguments)
    if name in ("cargo", "cargo-nextest") or name in GATE_PROGRAMS.values():
        return (name, *arguments)
    return ()


def process_arguments(process: Path) -> list[str]:
    """Read the arguments of a process from procfs."""
    return [word for word in read_proc_text(process / "cmdline").split("\0") if word]


def live_process_arguments(process: Path, start_time: str) -> list[str] | CommandUnreadable:
    """Read arguments from the recorded process lifetime."""
    try:
        argv = process_arguments(process)
        fields = read_proc_text(process / "stat").rsplit(") ", 1)[1].split()
        if fields[19] == start_time and fields[0] not in ("Z", "X"):
            return argv
    except (OSError, ValueError, IndexError) as error:
        return CommandUnreadable(str(error))
    return CommandUnreadable("the process lifetime ended")


def gate_job_from_process(step: str, process: Path, *, start_time: str = "") -> StepJob | OtherCommand | CommandUnreadable:
    """Read the gate job from its process or the copies.py caller."""
    try:
        argv = live_process_arguments(process, start_time) if start_time else process_arguments(process)
    except OSError as error:
        return CommandUnreadable(str(error))
    if isinstance(argv, CommandUnreadable):
        return argv
    copied = copies_run_command(argv)
    command = copied.arguments if isinstance(copied, CopiesRunCommand) else argv
    expected = GATE_PROGRAMS.get(step)
    return process_command_job(command, process, start_time=start_time) if (
        expected and command_program(command) == expected) else OTHER_COMMAND


def process_command_job(argv: Sequence[str], process: Path, *, start_time: str = "") -> StepJob | OtherCommand:
    """Name a gate test as integrate when a checked ancestor owns the landing."""
    job = command_job(argv)
    if (not isinstance(job, StepJob) or job.step not in ("test-all", "test-modules")
            or command_program(argv) != GATE_PROGRAMS["merge-gate"]):
        return job

    from scripts.build_hold.build_hold import process_start_time
    from scripts.production.land_runs import NoNatemergeLand, natemerge_land

    if not process.name.isdecimal():
        return job
    pid = int(process.name)
    proc = process.parent
    # Load the lineage reader after the command modules finish importing.
    build_targets = cast(CallerLineageReader, cast(object, import_module("scripts.build_targets.build_targets")))
    lineage = build_targets.read_caller_lineage(proc, pid, start_time or process_start_time(pid, proc))
    if (isinstance(lineage, build_targets.ConfirmedCallerLineage)
            and not isinstance(natemerge_land(proc, lineage.processes), NoNatemergeLand)):
        return StepJob("integrate")
    return job


def current_command_job(argv: Sequence[str]) -> StepJob | OtherCommand:
    """Name the job that this process will start."""
    proc = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
    return process_command_job(argv, proc / str(os.getpid()))


def _test_job(arguments: Sequence[str], *, affected: bool) -> StepJob:
    """A merge gate tests every module; a targeted test tests the affected modules unless --full."""
    modules = _count(arguments, "--module")
    if modules:
        return StepJob("test-modules", modules=modules)
    return StepJob("test-affected" if affected and "--full" not in arguments else "test-all")


def command_job(argv: Sequence[str]) -> StepJob | OtherCommand:
    """Name the job of a merge gate, targeted test or verify.sh command."""
    program = _program(argv)
    if isinstance(program, OtherCommand):
        return program
    name, arguments = program
    if name == "targeted_test.py":
        return _test_job(arguments, affected=True)
    if name == "verify.sh":
        return _verify_step(arguments)
    if name != "merge_gate.py":
        return OTHER_COMMAND
    branch = _option(arguments, "--merge-branch")
    if "--nightly" in arguments:
        return StepJob("test-nightly", branch=branch)
    if "--promote-only" in arguments:
        return StepJob("promote", branch=branch)
    if "--tests-only" in arguments:
        return _test_job(arguments, affected=False)
    unit = _option(arguments, "--unit")
    return StepJob("integrate", unit=unit, branch=branch) if unit and branch else OTHER_COMMAND


def gate_job_for_command(argv: Sequence[str]) -> StepJob | GateJobNotRecorded:
    job = current_command_job(argv)
    return job if isinstance(job, StepJob) else GATE_JOB_NOT_RECORDED


# Show each step as the queue records it, for a caller that reads no configuration.
RECORDED_STEP_NAMES = StepNames(MappingProxyType({}))


def read_step_names() -> StepNames:
    return StepNames(MappingProxyType(conf.read_or_repository("build_steps").values()))

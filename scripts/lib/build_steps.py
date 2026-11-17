"""Read the display names, prose names and descriptions of build steps."""
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath
from types import MappingProxyType

from scripts.lib import conf
from scripts.message.unit_labels import OwnerRole


@dataclass(frozen=True)
class StepNames:
    settings: Mapping[str, str]

    def shown(self, step: str) -> str:
        return self.settings.get(f"names.{step}", step)

    def prose(self, step: str) -> str:
        return self.settings.get(f"prose.{step}", self.shown(step))

    def for_owner(self, step: str, unit: str, role: OwnerRole, *,
                  ci: bool = False, current_name: str = "") -> str:
        """Use the configured owner template when the owner is known."""
        fallback = current_name or self.shown(step)
        if ci:
            prefix = "owned.ci-"
        elif role is OwnerRole.SHOWRUNNER:
            prefix = "owned.showrunner-"
        elif role is OwnerRole.UNIT and unit:
            prefix = "owned."
        else:
            return fallback
        template = self.settings.get(prefix + step, self.settings.get(prefix + "default"))
        return template.format(unit=unit, step=self.shown(step)) if template is not None else fallback


    def about(self, job: "StepJob", unit: str) -> str:
        """The job's description sentence; empty when the configuration has none or it names an unknown unit."""
        template = self.settings.get(f"about.{job.step}", "")
        owner = job.unit or (unit if unit != "—" else "")
        if not template or ("{unit}" in template and not owner):
            return ""
        return template.format(unit=owner, branch=job.branch, modules=job.modules)

    def changes(self, job: "StepJob") -> str:
        """What the job changes: a branch name, or the default text."""
        template = self.settings.get(f"changes.{job.step}", self.settings.get("changes.default", ""))
        return template.format(branch=job.branch)


@dataclass(frozen=True)
class StepJob:
    """The job a command runs, read from its arguments.

    unit is the checkpoint's unit for integrate; the other jobs test the caller's unit.
    """
    step: str
    unit: str = ""
    branch: str = ""
    modules: int = 0


@dataclass(frozen=True)
class OtherCommand:
    """The command is not a merge gate, a targeted test or verify.sh."""


OTHER_COMMAND = OtherCommand()


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


def _program(argv: Sequence[str]) -> tuple[str, Sequence[str]] | OtherCommand:
    """The script a command runs and its arguments, after a python or shell interpreter.

    Code from -c or standard input ("-") runs no named script, so it is OtherCommand.
    """
    index = 0
    while index < len(argv):
        name = PurePath(argv[index]).name
        if not INTERPRETER.fullmatch(name):
            return name, argv[index + 1:]
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
                    if not module:
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
    return StepJob("integrate", unit=_option(arguments, "--unit"), branch=branch)


# Show each step as the queue records it, for a caller that reads no configuration.
RECORDED_STEP_NAMES = StepNames(MappingProxyType({}))


def read_step_names() -> StepNames:
    return StepNames(MappingProxyType(conf.read_or_repository("build_steps").values()))

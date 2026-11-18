"""Read the display names, prose names and descriptions of build steps."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from string import Formatter
from types import MappingProxyType

from scripts.buildlog.parse import CARGO_BUILD_VERBS
from scripts.lib import conf
from scripts.lib.command_wrappers import WRAPPER_ARGUMENTS
from scripts.message.owner_role import OwnerRole


class InvalidNameTemplate(str):
    """A rejected template. Readers of the raw settings can still format the step."""

    def __new__(cls) -> "InvalidNameTemplate":
        return str.__new__(cls, "{step}")


class NameTemplate(str):
    """A display template validated for its named text fields."""

    def __new__(cls, value: str, *fields: str) -> "NameTemplate":
        for _, field, specification, _ in Formatter().parse(value):
            if field is not None and field not in fields:
                raise ValueError("expected a named text field")
            if specification is not None and ("{" in specification or "}" in specification):
                raise ValueError("expected named text fields with fixed format specifications")
        _ = value.format(**dict.fromkeys(fields, ""))
        return str.__new__(cls, value)

    @classmethod
    def read(cls, value: str, *fields: str) -> "NameTemplate | InvalidNameTemplate":
        try:
            return cls(value, *fields)
        except ValueError:
            return InvalidNameTemplate()


class NoOwnerTemplate(Enum):
    """The step has no applicable owner template."""

    FOUND = "no owner template"


@dataclass(frozen=True)
class StepNames:
    settings: Mapping[str, str]

    def __post_init__(self) -> None:
        settings = dict(self.settings)
        for key, value in self.settings.items():
            if key.startswith("owned."):
                settings[key] = NameTemplate.read(value, "unit", "step")
            elif key.startswith("names.") and not shown_name_text(value, NameTextKind.DISPLAY):
                settings[key] = default_queue_name(key.removeprefix("names."))
        object.__setattr__(self, "settings", MappingProxyType(settings))

    def shown(self, step: str) -> str:
        return self.settings.get(f"names.{step}", step)

    def queue_name(self, step: str) -> str:
        """Use the verb or its recognized display form."""
        display = default_queue_name(step)
        if step == "run":
            return display
        return display if " ".join(self.shown(step).split()) == display else step

    def prose(self, step: str) -> str:
        return self.settings.get(f"prose.{step}", self.shown(step))

    def owner_template(self, step: str, unit: str, role: OwnerRole, *,
                       ci: bool = False) -> NameTemplate | InvalidNameTemplate | NoOwnerTemplate:
        """Select the template for the step and owner."""
        if ci:
            prefix = "owned.ci-"
        elif role is OwnerRole.SHOWRUNNER:
            prefix = "owned.showrunner-"
        elif role is OwnerRole.UNIT and unit:
            prefix = "owned."
        else:
            return NoOwnerTemplate.FOUND
        template = self.settings.get(prefix + step, self.settings.get(prefix + "default"))
        return template if isinstance(template, (NameTemplate, InvalidNameTemplate)) else NoOwnerTemplate.FOUND

    def for_owner(self, step: str, unit: str, role: OwnerRole, *,
                  ci: bool = False, current_name: str = "", step_name: str = "") -> str:
        """Use the configured owner template when the owner is known."""
        template = self.owner_template(step, unit, role, ci=ci)
        if isinstance(template, InvalidNameTemplate):
            return self.shown(step)
        if isinstance(template, NameTemplate):
            return template.format(unit=unit, step=step_name or self.shown(step))
        return current_name or self.shown(step)

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
RAW_ID = re.compile(r"(?<![0-9a-f])(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{32})(?![0-9a-f])",
                    re.IGNORECASE)
IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
PROGRAM_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "ash", "csh", "tcsh"})
SHELL_COMMAND_PREFIXES = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{"})
SHELL_SYNTAX = SHELL_COMMAND_PREFIXES | {"fi", "for", "done", "case", "esac", "in", "function", "select",
                                         "time", "}", "[[", "]]", "(", ")"}
SETUP_COMMANDS = frozenset({"cd", "export", "set", "source", ".", "..", "true", ":", "umask"})
QUEUE_WRAPPER_ARGUMENTS = {**WRAPPER_ARGUMENTS, "timeout": {"-k", "--kill-after", "-s", "--signal"}}
NON_WORK_PROGRAMS = SHELL_SYNTAX | SETUP_COMMANDS | frozenset(QUEUE_WRAPPER_ARGUMENTS) | {"direnv"}
UNRECORDED_OWNER_TEXT = frozenset({"", "—", "owner not recorded"})
UNRECORDED_NAMES = UNRECORDED_OWNER_TEXT | {"unknown", "unknown program"}
OTHER_INTERPRETERS = frozenset({"node", "nodejs", "deno", "bun", "ruby", "perl", "php", "lua", "rscript", "osascript"})


class NameTextKind(Enum):
    """Select the rules for one name source."""

    PROGRAM = "program word"
    SCRIPT = "script filename"
    DISPLAY = "display name"


def public_queue_text(text: str) -> str:
    """Return text that can appear in an owner field."""
    text = " ".join(text.split())
    return "" if text in UNRECORDED_OWNER_TEXT or "$" in text or RAW_ID.search(text) else text


def name_interpreter(program: str) -> bool:
    """Check whether a program invokes an interpreter."""
    return program in SHELLS or program in OTHER_INTERPRETERS or bool(INTERPRETER.fullmatch(program))


def forbidden_name_word(word: str) -> bool:
    return word in UNRECORDED_NAMES or word == "env" or name_interpreter(word)


def shown_name_text(text: str, kind: NameTextKind) -> str:
    """Return a valid name for this source, or an empty string."""
    text = " ".join(text.split()) if kind is NameTextKind.DISPLAY else text
    folded = text.casefold()
    if not text.strip() or folded in UNRECORDED_NAMES or "$" in text or RAW_ID.search(text):
        return ""
    if kind is NameTextKind.SCRIPT:
        return "" if forbidden_name_word(folded) or any(ord(char) < 32 or ord(char) == 127 for char in text) else text
    if kind is NameTextKind.PROGRAM:
        return text if (PROGRAM_WORD.fullmatch(text) and not forbidden_name_word(folded)
                        and folded not in NON_WORK_PROGRAMS) else ""
    words = text.split()
    if not all(PROGRAM_WORD.fullmatch(word) for word in words) or forbidden_name_word(words[0].casefold()):
        return ""
    return text


def default_queue_name(step: str) -> str:
    """Return the built-in display name of a build step."""
    return QUEUE_STEP_DISPLAY.get(step, step.replace("-", " "))


def queue_identifier(text: str) -> bool:
    """Accept a package or repository name."""
    return bool(IDENTIFIER.fullmatch(text)) and not RAW_ID.search(text)


def is_recorded_step(text: str) -> bool:
    """Accept a build verb with or without a package name."""
    verb, _, package = text.partition(" ")
    return verb in RECORDED_VERBS and (not package or queue_identifier(package))


class QueueKind(Enum):
    """A fixed name for queue work without a verify or script name."""

    MERGE_GATE = "merge gate"
    CI = "hana CI"
    COMPILER_CACHE = "compiler cache"
    MAC = "Mac builds"
    AWS = "AWS build"
    BUILD_SLOT = "build slot"
    SWAP_GROUP = "Unclassified groups"


class QueueRowName(str):
    """A display name from a queue record."""

    def __new__(cls) -> QueueRowName:
        raise TypeError("Choose a queue name source")

    @classmethod
    def fixed(cls, kind: QueueKind) -> QueueRowName:
        return str.__new__(cls, kind.value)

    @classmethod
    def _from_text(cls, text: str, kind: NameTextKind, *, suffix: str = "") -> QueueRowName | NoNamedStep:
        text = shown_name_text(text, kind)
        return str.__new__(cls, text + suffix) if text else NoNamedStep.FOUND

    @classmethod
    def display_name(cls, text: str) -> QueueRowName | NoNamedStep:
        return cls._from_text(text, NameTextKind.DISPLAY)

    @classmethod
    def ci_label(cls, text: str) -> QueueRowName:
        """Resolve each component of a recorded CI label."""
        label = cls.ci_components(text)
        return cls.fixed(QueueKind.CI) if isinstance(label, NoNamedStep) else label

    @classmethod
    def ci_components(cls, text: str) -> QueueRowName | NoNamedStep:
        """Join valid display components with the CI separator."""
        parts: list[QueueRowName] = []
        for component in text.split("·"):
            if not component.strip():
                continue
            part = cls.display_name(component)
            if isinstance(part, NoNamedStep):
                return part
            parts.append(part)
        return str.__new__(cls, " · ".join(parts)) if parts else NoNamedStep.FOUND

    @classmethod
    def repository(cls, repository: str, folder: str = "") -> QueueRowName | NoNamedStep:
        name = cls._from_text(PurePath(repository).name, NameTextKind.DISPLAY, suffix=" build")
        if isinstance(name, NoNamedStep):
            return cls._from_text(PurePath(folder).name, NameTextKind.DISPLAY, suffix=" build")
        return name

    @classmethod
    def script(cls, path: str) -> QueueRowName | NoNamedStep:
        return cls._from_text(PurePath(path).name, NameTextKind.SCRIPT) if script_file(path) else NoNamedStep.FOUND

    @classmethod
    def recorded_program(cls, program: str) -> QueueRowName | NoNamedStep:
        return cls._from_text(program, NameTextKind.PROGRAM)

    @classmethod
    def recorded_step_text(cls, text: str) -> QueueRowName | NoNamedStep:
        return cls.display_name(text)

    @classmethod
    def step(cls, verb: str, package: str, names: StepNames) -> QueueRowName | NoNamedStep:
        if verb not in RECORDED_VERBS:
            return NoNamedStep.FOUND
        shown = cls.display_name(names.queue_name(verb))
        if isinstance(shown, QueueRowName) and queue_identifier(package):
            return str.__new__(cls, f"{shown} {package}")
        return shown

    def for_owner(self, names: StepNames, step: str, unit: str, role: OwnerRole, *,
                  prefix: str = "") -> FormattedQueueName:
        """Apply the owner template to a recorded build verb."""
        if not is_recorded_step(step):
            return self.with_prefix("")
        verb = step.partition(" ")[0]
        step = names.queue_name(verb)
        unit = shown_name_text(unit, NameTextKind.DISPLAY)
        template = names.owner_template(verb, unit, role)
        if isinstance(template, InvalidNameTemplate):
            for text in (names.shown(verb), default_queue_name(verb)):
                fallback = self.display_name(text)
                if isinstance(fallback, QueueRowName):
                    return fallback.with_prefix("")
        elif isinstance(template, NameTemplate):
            shown = " ".join(template.format(unit=unit, step=step).split())
            owner = "showrunner" if role is OwnerRole.SHOWRUNNER else unit
            for work in (step, SHORT_OWNER_STEPS.get(verb, step)):
                if owner and shown == f"{owner} {work}":
                    title = self.display_name(work)
                    if isinstance(title, QueueRowName):
                        return title.with_prefix(owner)
        return self.with_prefix(prefix)

    def with_prefix(self, prefix: str) -> FormattedQueueName:
        """Keep the prefix and work used to form the heading."""
        prefix = shown_name_text(prefix, NameTextKind.DISPLAY)
        heading = str.__new__(QueueRowName, f"{prefix} {self}") if prefix else self
        return FormattedQueueName(heading, prefix, self)

    def with_mac_status(self, status: str) -> QueueRowName:
        """Add a fixed status to the Mac name."""
        if status not in ("user", "hana-ci", "unplaced"):
            return self
        return str.__new__(QueueRowName, f"{self} · {status}")


@dataclass(frozen=True)
class FormattedQueueName:
    """The heading and its known parts before page serialization."""

    heading: QueueRowName
    prefix: str
    work: QueueRowName

    def with_mac_status(self, status: str) -> FormattedQueueName:
        return FormattedQueueName(self.heading.with_mac_status(status), self.prefix, self.work.with_mac_status(status))


class NoNamedStep(Enum):
    """The caller supplied no step or script name."""

    FOUND = "no named step"


class CINameTemplate(str):
    """A CI label with literal text and runner and job fields."""

    def __new__(cls, value: str) -> CINameTemplate:
        template = NameTemplate(value, "runner", "job")
        for _, _, specification, conversion in Formatter().parse(template):
            if specification or conversion:
                raise ValueError("expected an unformatted CI field")
        rendered = template.format(runner="runner", job="job")
        if isinstance(QueueRowName.ci_components(rendered), NoNamedStep):
            raise ValueError("expected a display name for CI")
        return str.__new__(cls, template)

    def label(self, runner: str, job: str) -> QueueRowName:
        """Resolve the configured label before any caller saves or shows it."""
        components = tuple(shown_name_text(value, NameTextKind.DISPLAY) for value in (runner, job))
        if any(value.strip() and not checked for value, checked in zip((runner, job), components, strict=True)):
            return QueueRowName.fixed(QueueKind.CI)
        return QueueRowName.ci_label(self.format(runner=components[0], job=components[1]))


def script_file(word: str) -> bool:
    """Recognize a script path in a recorded command."""
    return bool(re.fullmatch(r"[\w./~+' -]+", word)) and PurePath(word).suffix in SCRIPT_SUFFIXES


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
RECORDED_STEP_NAMES = StepNames({})


def read_step_names() -> StepNames:
    return StepNames(conf.read_or_repository("build_steps").values())

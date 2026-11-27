"""Read the display names, prose names and descriptions of build steps."""
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import Literal

from scripts.build_queue.admission_jobs import GATE_JOB_NOT_RECORDED, HAND_MADE as HAND_MADE, GateJobNotRecorded, StepJob as StepJob
from scripts.build_queue.snapshot_codec import RAW_ID, public_queue_text
from scripts.buildlog.parse import CARGO_BUILD_VERBS, CI_CARGO_FRONT, cargo_front_call
from scripts.lib import conf
from scripts.lib.proc_text import read_proc_text


@dataclass(frozen=True)
class StepNames:
    settings: Mapping[str, str]

    def shown(self, step: str) -> str:
        return self.settings.get(f"names.{step}", step)

    def queue_name(self, step: str) -> str:
        """Use the configured gate name or the recognized form of another step."""
        if step == "gate":
            return public_queue_text(self.shown(step)) or step
        display = QUEUE_STEP_DISPLAY.get(step, step.replace("-", " "))
        if step == "run":
            return display
        return display if " ".join(self.shown(step).split()) == display else step

    def prose(self, step: str) -> str:
        return self.settings.get(f"prose.{step}", self.shown(step))

    def about(self, job: StepJob) -> str:
        """Describe the job when a template applies."""
        template = self.settings.get(f"about.{job.step}", "")
        return template.format(branch=job.branch, modules=job.modules)

    def changes(self, job: StepJob) -> str:
        """What the job changes: a branch name, or the default text."""
        template = self.settings.get(f"changes.{job.step}", self.settings.get("changes.default", ""))
        return template.format(branch=job.branch)


@dataclass(frozen=True)
class OtherCommand:
    """A command outside the gate and verify.sh routes."""


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
    program = parse_command_program(argv)
    if isinstance(program, OtherCommand) or program[0] != "verify.sh":
        return OTHER_COMMAND
    return VerifyCommand(tuple(program[1]))


def command_program(argv: Sequence[str]) -> str:
    """Name the script after interpreter arguments."""
    program = parse_command_program(argv)
    return "" if isinstance(program, OtherCommand) else program[0]



SCRIPT_SUFFIXES = frozenset({".awk", ".bash", ".fish", ".js", ".lua", ".mjs", ".pl", ".ps1", ".py", ".rb",
                             ".sh", ".ts", ".zsh"})
VERIFY_VERBS = frozenset({"check", "test", "lint", "doc", "build", "example", "final", "example-test"})
RECORDED_VERBS = VERIFY_VERBS | CARGO_BUILD_VERBS | {"cargo", "fmt", "sweep", "taplo", "rust-analyzer",
    "targeted-test", "merge-gate", "gate", "integrate", "test-all", "test-modules",
    "test-affected", "test-nightly", "fast-track", "toolchain-trial", "promote", HAND_MADE.step}
QUEUE_STEP_DISPLAY = {"run": "cargo run", "test-modules": "test named", HAND_MADE.step: "hand made"}
MERGE_GATE_PROGRAMS = ("gate.py", "merge_gate.py")
GATE_PROGRAMS = {
    "gate": MERGE_GATE_PROGRAMS,
    "test-affected": (*MERGE_GATE_PROGRAMS, "targeted_test.py"),
    "merge-gate": MERGE_GATE_PROGRAMS,
    "targeted-test": ("targeted_test.py",),
}
GATE_STEPS = frozenset(GATE_PROGRAMS)
GATE_SCRIPTS = frozenset(script for programs in GATE_PROGRAMS.values() for script in programs)
# The assigned target started no tests. A claim failure can allow the queue to place the gate again.
GATE_PRELAUNCH_REFUSED_STATUS = 79
# The assigned target could have started tests and supplied no verdict.
GATE_RESULT_UNKNOWN_STATUS = 80
GATE_UNSETTLED_LAUNCH_STATUS = GATE_RESULT_UNKNOWN_STATUS
MAC_LINUX_PRELAUNCH_REFUSED_STATUS = GATE_PRELAUNCH_REFUSED_STATUS
CLAIM_FAILURE_SUFFIX = ".claim-failure"
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
    program = parse_command_program(argv, keep_path=True)
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


def gate_natedev_only_flag(arguments: Sequence[str]) -> str | None:
    """Return the gate option that requires natedev."""
    if "--promote-only" in arguments:
        return "--promote-only"
    return None


def gate_target_kind(target: str) -> Literal["natedev", "mac-linux", "aws"] | None:
    """Return the route that can run an assigned gate."""
    if target in ("natedev", "mac-linux", "aws"):
        return target
    return None


# A program that runs the script named after its options.
INTERPRETER = re.compile(r"python[0-9.]*|py|bash|sh")
# Interpreter options that take the next argument, or the rest of their cluster, as their value.
VALUE_OPTIONS = frozenset("XW")
LONG_VALUE_OPTIONS = frozenset({"--check-hash-based-pycs", "--init-file", "--rcfile"})


def parse_command_program(argv: Sequence[str], *, keep_path: bool = False) -> tuple[str, Sequence[str]] | OtherCommand:
    """Return the program name and arguments after Python or shell interpreter options.

    Return OtherCommand for inline code, standard input, or a missing program.
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
    """Read the command that the copies.py process supervises."""
    program = parse_command_program(argv)
    if (isinstance(program, OtherCommand) or program[0] != "copies.py"
            or not program[1] or program[1][0] not in ("run", "gate-step")):
        return OTHER_COMMAND
    arguments = program[1]
    if "--" not in arguments:
        return OTHER_COMMAND
    separator = arguments.index("--")
    source = _option(arguments[:separator], "--repo")
    return CopiesRunCommand(source, tuple(arguments[separator + 1:])) if source else OTHER_COMMAND


def build_call_arguments(argv: Sequence[str]) -> Sequence[str]:
    """Read the Cargo or gate command from recorded arguments."""
    program = parse_command_program(argv)
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
    if name in ("cargo", "cargo-nextest") or name in GATE_SCRIPTS:
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
    """Read the gate job from the gate process or its copies.py supervisor."""
    expected = GATE_PROGRAMS.get(step, ())
    if not expected:
        return OTHER_COMMAND
    try:
        argv = live_process_arguments(process, start_time) if start_time else process_arguments(process)
    except OSError as error:
        return CommandUnreadable(str(error))
    if isinstance(argv, CommandUnreadable):
        return argv
    copied = copies_run_command(argv)
    command = copied.arguments if isinstance(copied, CopiesRunCommand) else argv
    return command_job(command) if command_program(command) in expected else OTHER_COMMAND


def _test_job(arguments: Sequence[str], *, affected: bool) -> StepJob:
    """A gate test runs all modules by default. Targeted tests select affected modules unless --full is set."""
    modules = _count(arguments, "--module")
    if modules:
        return StepJob("test-modules", modules=modules)
    return StepJob("test-affected" if affected and "--full" not in arguments else "test-all")


def command_job(argv: Sequence[str]) -> StepJob | OtherCommand:
    """Name the job of a gate or verify.sh command."""
    program = parse_command_program(argv)
    if isinstance(program, OtherCommand):
        return program
    name, arguments = program
    if name == "targeted_test.py" or (name in MERGE_GATE_PROGRAMS and "--affected" in arguments):
        return _test_job(arguments, affected=True)
    if name == "verify.sh":
        return _verify_step(arguments)
    if name not in MERGE_GATE_PROGRAMS:
        return OTHER_COMMAND
    branch = _option(arguments, "--merge-branch")
    if "--nightly" in arguments:
        return StepJob("test-nightly", branch=branch)
    if "--promote-only" in arguments:
        return StepJob("promote", branch=branch)
    if "--tests-only" in arguments:
        return _test_job(arguments, affected=False)
    return StepJob("integrate", unit=_option(arguments, "--unit"), branch=branch)


def gate_job_for_command(argv: Sequence[str]) -> StepJob | GateJobNotRecorded:
    job = command_job(argv)
    return job if isinstance(job, StepJob) else GATE_JOB_NOT_RECORDED


# Show each step as the queue records it, for a caller that reads no configuration.
RECORDED_STEP_NAMES = StepNames(MappingProxyType({}))


def read_step_names() -> StepNames:
    return StepNames(MappingProxyType(conf.read_or_repository("build_steps").values()))

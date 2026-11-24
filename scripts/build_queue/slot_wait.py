"""Name steps from queued commands and recorded work."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from enum import Enum
from functools import cache, lru_cache
from pathlib import Path
import re
import shlex
import subprocess

from scripts.lib.build_steps import (INTERPRETER, LONG_VALUE_OPTIONS, RECORDED_VERBS,
                                     VERIFY_VERBS, StepJob, StepNames, VerifyCommand, command_job,
                                     command_script, is_recorded_step, queue_identifier, script_file, verify_command)
from scripts.build_queue.admission_jobs import (GATE_JOB_NOT_RECORDED, VERIFY_VERBS as VERIFY_CALL_VERBS,
                                                GateJobNotRecorded, NotVerifyCall, VerifyCall, VerifyStep)
from scripts.build_queue.snapshot_codec import RAW_ID, public_queue_text as public_queue_text, row_text, select_queue_name


SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "ash", "csh", "tcsh"})


class QueueKind(Enum):
    """A fixed name for queue work without a verify or script name."""

    CI = "hana CI"
    COMPILER_CACHE = "compiler cache"
    MAC = "Mac builds"
    AWS = "AWS build"


class QueueRowName(str):
    """A display name from a queue record."""

    def __new__(cls) -> QueueRowName:
        raise TypeError("Choose a queue name source")

    @classmethod
    def fixed(cls, kind: QueueKind) -> QueueRowName:
        return str.__new__(cls, kind.value)

    @classmethod
    def owner_only(cls, *candidates: object) -> QueueRowName:
        return str.__new__(cls, select_queue_name(*candidates))

    @classmethod
    def owned(cls, owner: object, what: object) -> QueueRowName:
        """Name a queue request `<owner>: <what>`."""
        return str.__new__(cls, row_text(owner, what))

    @classmethod
    def repository(cls, repository: str, folder: str = "") -> QueueRowName | NoNamedStep:
        repository = public_queue_text(Path(repository).name) or public_queue_text(Path(folder).name)
        return str.__new__(cls, f"{repository} build") if repository else NoNamedStep.FOUND

    @classmethod
    def script(cls, path: str) -> QueueRowName | NoNamedStep:
        name = public_queue_text(Path(path).name) if script_file(path) else ""
        return str.__new__(cls, name) if name else NoNamedStep.FOUND

    @classmethod
    def recorded_program(cls, program: str) -> QueueRowName | NoNamedStep:
        if (program in ("", ".", "..", "unknown") or Path(program).name != program or RAW_ID.search(program)
                or INTERPRETER.fullmatch(program) or not next(command_programs((program,)), ())):
            return NoNamedStep.FOUND
        return str.__new__(cls, program)

    @classmethod
    def recorded_step_text(cls, text: str) -> QueueRowName | NoNamedStep:
        return str.__new__(cls, text) if text else NoNamedStep.FOUND

    @classmethod
    def step(cls, verb: str, package: str, names: StepNames) -> QueueRowName | NoNamedStep:
        if verb not in RECORDED_VERBS:
            return NoNamedStep.FOUND
        shown = names.queue_name(verb)
        if queue_identifier(package):
            shown = f"{shown} {package}"
        return str.__new__(cls, shown)

    def with_mac_status(self, status: str) -> QueueRowName:
        """Add a fixed status to the Mac name."""
        if status not in ("user", "hana-ci", "unplaced"):
            return self
        return str.__new__(type(self), f"{self} · {status}")


class NoNamedStep(Enum):
    """The caller supplied no step or script name."""

    FOUND = "no named step"


def recorded_step(text: str, names: StepNames, package: str = "") -> QueueRowName | NoNamedStep:
    """Read a stored verb and package, or a script file name."""
    if is_recorded_step(text):
        verb, _, recorded_package = text.partition(" ")
        return QueueRowName.step(verb, recorded_package or package, names)
    return QueueRowName.script(text)


def command_programs(argv: Sequence[str]) -> Iterator[Sequence[str]]:
    """Find programs in wrapper arguments and shell command lists."""
    from scripts.buildlog.parse import command_words
    from scripts.hooks.checkpoint_gate import ASSIGNMENT, shell_commands, shell_text, without_redirections

    if not argv:
        return
    program = Path(argv[0]).name
    if ASSIGNMENT.match(argv[0]):
        yield from command_programs(("env", *argv))
    elif program == "env":
        yield from command_programs(command_words(list(argv)))
    elif program == "direnv":
        if len(argv) >= 3 and argv[1] == "exec":
            yield from command_programs(argv[3:])
    elif program in ("exec", "command"):
        index = 1
        while index < len(argv) and argv[index].startswith("-"):
            option = argv[index]
            index += 1
            if option == "--":
                break
            if program == "command" and option in ("-v", "-V"):
                return
            if program == "exec" and option == "-a":
                index += 1
        yield from command_programs(argv[index:])
    elif program in SHELLS:
        arguments = iter(argv[1:])
        for word in arguments:
            if word in ("-", "--") or not word.startswith("-"):
                if word == "--":
                    word = next(arguments, "")
                if word and word != "-":
                    yield [word, *arguments]
                return
            if word in LONG_VALUE_OPTIONS:
                _ = next(arguments, "")
            elif word == "--command" or re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", word):
                for command in shell_commands(next(arguments, "")):
                    yield from command_programs([shell_text(word) for word in without_redirections(command.words)])
                return
    else:
        yield argv


@cache
def verify_package_parser() -> str:
    """Read the same package parser that verify.sh uses."""
    from scripts.lib.shell_test_support import shell_function

    script = Path(__file__).resolve().parents[1] / "direct/verify.sh"
    return shell_function(script.read_text(), "parse_command_arguments") + """
command_help() { :; }
usage() { :; }
CMD=$1
shift
parse_command_arguments "$@"
printf '%s' "$VERIFY_PACKAGE"
"""


@cache
def verify_value_options() -> frozenset[str]:
    """Read the options that consume two words in the Verify parser."""
    try:
        parser = verify_package_parser()
    except (OSError, AssertionError):
        return frozenset()
    cases = re.finditer(r"^[ \t]+(--[\w-]+(?:\|--[\w-]+)*)\)(.*?);;", parser, re.MULTILINE | re.DOTALL)
    return frozenset(flag for case in cases if re.search(r"\bshift[ \t]+2\b", case.group(2))
                     for flag in case.group(1).split("|"))


def verify_package(step: str, arguments: tuple[str, ...]) -> str:
    """Pass literal arguments to the verify parser without starting a build."""
    try:
        return _parse_verify_package(step, arguments)
    except (OSError, subprocess.SubprocessError, AssertionError):
        return ""


@lru_cache(maxsize=128)
def _parse_verify_package(step: str, arguments: tuple[str, ...]) -> str:
    result = subprocess.run(["bash", "--noprofile", "--norc", "-c", verify_package_parser(),
                             "verify-package", step, *arguments], env={}, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=2, check=False)
    result.check_returncode()
    return result.stdout


def command_slot_name(command: Sequence[str], names: StepNames) -> QueueRowName | NoNamedStep:
    """Name the first build job or script in the command."""
    return command_slot_job(command, names)[0]


def command_slot_job(command: Sequence[str], names: StepNames
                     ) -> tuple[QueueRowName | NoNamedStep, StepJob | GateJobNotRecorded, VerifyStep]:
    """Keep the gate job or Verify call with the step name."""
    script = ""
    try:
        for argv in command_programs(command):
            job = command_job(argv)
            verify = verify_command(argv)
            if isinstance(job, StepJob):
                if not isinstance(verify, VerifyCommand):
                    return QueueRowName.step(job.step, "", names), job, NotVerifyCall()
                if job.step in VERIFY_VERBS:
                    arguments = verify.arguments
                    if arguments[:1] == ("--session-dir",):
                        arguments = arguments[2:]
                    package = verify_package(job.step, arguments[1:])
                    # The slot waits for the whole call.
                    facts = (VerifyCall(job.step, shlex.join(("verify", *arguments)), 1, 1)
                             if job.step in VERIFY_CALL_VERBS else NotVerifyCall())
                    return QueueRowName.step(job.step, package, names), GATE_JOB_NOT_RECORDED, facts
            if not script:
                script = command_script(argv)
    except (ValueError, RecursionError):
        pass
    return (QueueRowName.script(script) if script else NoNamedStep.FOUND), GATE_JOB_NOT_RECORDED, NotVerifyCall()

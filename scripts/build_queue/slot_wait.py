"""Wait for repository slot capacity through the build queue."""
from __future__ import annotations

from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache, cached_property, lru_cache
import os
from pathlib import Path
import re
import subprocess

from scripts.lib import conf
from scripts.lib.build_steps import (LONG_VALUE_OPTIONS, QUEUE_WRAPPER_ARGUMENTS, RECORDED_STEP_NAMES,
                                     SHELLS, SHELL_COMMAND_PREFIXES, VERIFY_VERBS,
                                     NoNamedStep as NoNamedStep, QueueKind as QueueKind, QueueRowName as QueueRowName,
                                     StepJob, StepNames, VerifyCommand, command_job, command_script,
                                     is_recorded_step, verify_command)
from scripts.build_queue import admission_wire as wire


def recorded_step(text: str, names: StepNames, package: str = "") -> QueueRowName | NoNamedStep:
    """Read a stored verb and package, or a script file name."""
    if is_recorded_step(text):
        verb, _, recorded_package = text.partition(" ")
        return QueueRowName.step(verb, recorded_package or package, names)
    return QueueRowName.script(text)


@dataclass(frozen=True)
class SlotCaller:
    """The copy owner and command for a slot wait."""

    staff_id: str
    showrunner: str
    source: Path
    command: tuple[str, ...]
    work: str

    @cached_property
    def step(self) -> QueueRowName | NoNamedStep:
        """Read only the verify step when the slot must wait."""
        try:
            for argv in command_programs(self.command):
                step = command_verify_step(argv, RECORDED_STEP_NAMES)
                if isinstance(step, QueueRowName):
                    return step
        except (ValueError, RecursionError):
            pass
        return supplied_verify_step(self.work, RECORDED_STEP_NAMES)


SLOT_CALLER: ContextVar[SlotCaller | None] = ContextVar("slot_caller", default=None)


@contextmanager
def slot_caller(staff_id: str, showrunner: str, source: Path, command: Sequence[str], work: str) -> Generator[None]:
    """Carry the caller identity until the slot is available."""
    token = SLOT_CALLER.set(SlotCaller(staff_id, showrunner, source, tuple(command), work))
    try:
        yield
    finally:
        SLOT_CALLER.reset(token)


@dataclass(frozen=True)
class ShellCommandText:
    """The command text that a shell receives through its command option."""

    text: str


def shell_payload(arguments: Sequence[str]) -> ShellCommandText | tuple[str, ...]:
    """Read the command text or the script arguments of a shell."""
    words = iter(arguments)
    for word in words:
        if word == "-":
            break
        if word == "--":
            return tuple(words)
        if not word.startswith("-"):
            return (word, *words)
        if word in LONG_VALUE_OPTIONS:
            _ = next(words, "")
        elif word == "--command" or re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", word):
            return ShellCommandText(next(words, ""))
    return ()


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
    elif program in QUEUE_WRAPPER_ARGUMENTS:
        index = 1
        while index < len(argv) and argv[index].startswith("-"):
            option = argv[index]
            index += 1
            if option == "--":
                break
            if program == "command" and option in ("-v", "-V"):
                return
            if option in QUEUE_WRAPPER_ARGUMENTS[program]:
                index += 1
        if program == "timeout":
            index += 1
        yield from command_programs(argv[index:])
    elif program in SHELLS:
        payload = shell_payload(argv[1:])
        if isinstance(payload, ShellCommandText):
            for command in shell_commands(payload.text):
                words = iter(without_redirections(command.words))
                for word in words:
                    if word not in SHELL_COMMAND_PREFIXES:
                        yield from command_programs([shell_text(word), *(shell_text(word) for word in words)])
                        break
        elif payload and payload[0] != "-":
            yield payload
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


@lru_cache(maxsize=128)
def verify_package(step: str, arguments: tuple[str, ...]) -> str:
    """Pass literal arguments to the verify parser without starting a build."""
    try:
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", verify_package_parser(),
                                 "verify-package", step, *arguments], env={}, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired, AssertionError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def supplied_verify_step(text: str, names: StepNames) -> QueueRowName | NoNamedStep:
    """Recognize a verify step supplied without command arguments."""
    if text.partition(" ")[0] in VERIFY_VERBS and is_recorded_step(text):
        return recorded_step(text, names)
    return NoNamedStep.FOUND


def command_verify_step(argv: Sequence[str], names: StepNames) -> QueueRowName | NoNamedStep:
    """Read the verify step and package from one command."""
    verify = verify_command(argv)
    if not isinstance(verify, VerifyCommand):
        return NoNamedStep.FOUND
    job = command_job(argv)
    if not isinstance(job, StepJob) or job.step not in VERIFY_VERBS:
        return NoNamedStep.FOUND
    arguments = verify.arguments[2:] if verify.arguments[:1] == ("--session-dir",) else verify.arguments
    return QueueRowName.step(job.step, verify_package(job.step, arguments[1:]), names)


def recorded_slot_step(text: str, names: StepNames) -> QueueRowName | NoNamedStep:
    """Separate a recorded step from an old script or program fallback."""
    words = text.split()
    if len(words) < 2:
        return supplied_verify_step(text, names)
    if (isinstance(QueueRowName.recorded_program(words[0]), NoNamedStep)
            or isinstance(QueueRowName.script(text), QueueRowName)):
        return NoNamedStep.FOUND
    step = recorded_step(text, names)
    return QueueRowName.recorded_step_text(text) if isinstance(step, NoNamedStep) else step


def command_slot_name(command: Sequence[str], names: StepNames, *, recorded: str = "",
                      repository: str = "", folder: str = "") -> QueueRowName | NoNamedStep:
    """Resolve a slot name in order from its step, repository, and command."""
    step = recorded_slot_step(recorded, names)
    if isinstance(step, QueueRowName):
        return step
    repository_name = QueueRowName.repository(repository, folder)
    script = QueueRowName.script(recorded)
    program: QueueRowName | NoNamedStep = NoNamedStep.FOUND
    try:
        for argv in command_programs(command):
            step = command_verify_step(argv, names)
            if isinstance(step, QueueRowName):
                return step
            if isinstance(repository_name, NoNamedStep):
                if isinstance(script, NoNamedStep):
                    script = QueueRowName.script(command_script(argv))
                if isinstance(program, NoNamedStep):
                    program = QueueRowName.recorded_program(Path(argv[0]).name)
    except (ValueError, RecursionError):
        pass
    for name in (repository_name, script, program):
        if isinstance(name, QueueRowName):
            return name
    return QueueRowName.recorded_program(recorded)


class SlotWaitExpired(RuntimeError):
    """The repository pool has no capacity before the wait ends."""


def read_pool(repository: str, machine: str, root: Path, locks: str) -> wire.RepositorySlotWaiting:
    """Read lock holders without taking a slot lock or changing a slot."""
    settings = conf.read_or_repository("build_folders")
    key = f"slots.{machine}.{repository}.max"
    maximum = settings.whole(key if key in settings.values() else f"slots.{machine}.default_max")
    holders: set[tuple[int, int, int]] = set()
    for line in locks.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[1] != "FLOCK" or fields[3] != "WRITE":
            continue
        major, minor, inode = fields[5].split(":")
        holders.add((int(major, 16), int(minor, 16), int(inode)))
    held: list[int] = []
    for number in range(1, maximum + 1):
        try:
            info = (root / f"{repository}-slot{number}.lock").stat()
        except FileNotFoundError:
            continue
        if (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino) in holders:
            held.append(number)
    return wire.RepositorySlotWaiting(repository, tuple(held), maximum, key)


def wait_for_slot(repository: str, machine: str, root: Path, maximum: int, *, episode: str,
                  since: float, showrunner: str, unit: str, work: str, purpose: str,
                  expires_at: float | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT) -> None:
    """Block on one queue connection; retain episode and since after a lost race."""
    from scripts.build_queue import admission_client as client, admission_records as records
    from scripts.build_queue import admission_protocol as wire

    seconds = conf.read_or_repository("build_folders").number("slots.wait_seconds")
    deadline = since + seconds if isinstance(expires_at, conf.UseConfiguredValue) else expires_at
    start = records.process_start_time(os.getpid())
    if start is None:
        raise RuntimeError("the slot waiter has no process identity")
    caller = SLOT_CALLER.get()
    if caller is None:
        session = os.environ.get("CLAUDE_CODE_SESSION_ID", "") or os.environ.get("CODEX_THREAD_ID", "")
        step = supplied_verify_step(work, RECORDED_STEP_NAMES)
        identity = wire.Identity(session, showrunner, step if isinstance(step, QueueRowName) else "", "")
    else:
        step = caller.step
        identity = wire.Identity(caller.staff_id, caller.showrunner,
                                 step if isinstance(step, QueueRowName) else "", str(caller.source))
    del unit, purpose
    request = wire.RepositorySlotRequest(episode, wire.Owner(os.getpid(), start), since,
        repository, machine, str(root), maximum, deadline, identity)
    reply = client.repository_slot(request)
    if isinstance(reply, wire.RepositorySlotExpired):
        raise SlotWaitExpired(reply.reason)
    if isinstance(reply, wire.Refused):
        raise RuntimeError(reply.reason)

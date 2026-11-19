"""Wait for repository slot capacity through the build queue."""
from __future__ import annotations

from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from functools import cache, cached_property, lru_cache
import os
from pathlib import Path
import re
import subprocess

from scripts.lib import conf
from scripts.lib.build_steps import (INTERPRETER, LONG_VALUE_OPTIONS, RAW_ID, RECORDED_STEP_NAMES, RECORDED_VERBS,
                                     SHORT_OWNER_STEPS, VERIFY_VERBS, StepJob, StepNames, VerifyCommand, command_job,
                                     command_script, is_recorded_step, queue_identifier, script_file, verify_command)
from scripts.build_queue import admission_wire as wire
from scripts.build_queue.snapshot_codec import public_queue_text as public_queue_text, select_queue_name
from scripts.message.owner_role import OwnerRole


SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "ash", "csh", "tcsh"})


class QueueKind(Enum):
    """A fixed name for queue work without a verify or script name."""

    MERGE_GATE = "merge gate"
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

    def for_owner(self, names: StepNames, step: str, unit: str, role: OwnerRole, *,
                  prefix: str = "", ci: bool = False) -> QueueRowName:
        """Apply the owner template to a recorded build verb."""
        if not is_recorded_step(step):
            return self
        verb = step.partition(" ")[0]
        return self.with_prefix(prefix)._show_owner(names, verb, unit, role, ci)

    def _show_owner(self, names: StepNames, verb: str, unit: str, role: OwnerRole, ci: bool) -> QueueRowName:
        step = names.queue_name(verb)
        unit = public_queue_text(unit)
        try:
            shown = public_queue_text(names.for_owner(verb, unit, role, ci=ci, current_name=self, step_name=step))
        except (ValueError, KeyError, AttributeError, IndexError):
            return self
        owner = "CI" if ci else "showrunner" if role is OwnerRole.SHOWRUNNER else unit
        short = SHORT_OWNER_STEPS.get(verb, step)
        if owner and shown in (f"{owner} {step}", f"{owner} {short}"):
            return str.__new__(type(self), shown)
        return self

    def with_prefix(self, prefix: str) -> QueueRowName:
        """Add a repository or a resolved staff name before the step."""
        prefix = public_queue_text(prefix)
        return str.__new__(type(self), f"{prefix} {self}") if prefix else self

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
        """Read the command only when the slot must wait."""
        if self.command:
            return command_slot_name(self.command, RECORDED_STEP_NAMES)
        return recorded_step(self.work, RECORDED_STEP_NAMES)


SLOT_CALLER: ContextVar[SlotCaller | None] = ContextVar("slot_caller", default=None)


@contextmanager
def slot_caller(staff_id: str, showrunner: str, source: Path, command: Sequence[str], work: str) -> Generator[None]:
    """Carry the caller identity until the slot is available."""
    token = SLOT_CALLER.set(SlotCaller(staff_id, showrunner, source, tuple(command), work))
    try:
        yield
    finally:
        SLOT_CALLER.reset(token)


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


def command_slot_name(command: Sequence[str], names: StepNames) -> QueueRowName | NoNamedStep:
    """Name the first build job, or the first script in the command."""
    script = ""
    try:
        for argv in command_programs(command):
            job = command_job(argv)
            verify = verify_command(argv)
            if isinstance(job, StepJob):
                if not isinstance(verify, VerifyCommand):
                    return QueueRowName.step(job.step, "", names)
                if job.step in VERIFY_VERBS:
                    arguments = verify.arguments
                    if arguments[:1] == ("--session-dir",):
                        arguments = arguments[2:]
                    package = verify_package(job.step, arguments[1:])
                    return QueueRowName.step(job.step, package, names)
            if not script:
                script = command_script(argv)
    except (ValueError, RecursionError):
        pass
    return QueueRowName.script(script) if script else NoNamedStep.FOUND


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
        identity = wire.Identity(session, showrunner, work, "")
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

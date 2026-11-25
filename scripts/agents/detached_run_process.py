"""Read process arguments and identify detached run supervisors."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
import subprocess

from scripts.lib import conf


@dataclass(frozen=True)
class ProcessCommandText:
    """The process command from ps has no argument boundaries."""
    text: str


@dataclass(frozen=True)
class ProcessArgumentsUnavailable:
    """The process arguments are unavailable."""


@dataclass(frozen=True)
class ProcessAbsent:
    """The ps process check found no process."""


def process_args(pid: int) -> list[str] | ProcessCommandText | ProcessArgumentsUnavailable | ProcessAbsent:
    """Read the process arguments or report absence or failure."""
    if Path("/proc/self").exists():
        return _proc_args(Path(f"/proc/{pid}"))
    timeout = conf.read("detached_runs").whole("process_check_seconds")
    try:
        shown = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                               capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return ProcessArgumentsUnavailable()
    command = shown.stdout.strip()
    if shown.returncode == 1 and not command and not shown.stderr.strip():
        return ProcessAbsent()
    if shown.returncode or not command or (command.startswith("(") and command.endswith(")")):
        return ProcessArgumentsUnavailable()
    return ProcessCommandText(command)


def _proc_args(process: Path) -> list[str]:
    try:
        return (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return []


def _command_match(pid: int, command: Sequence[str]) -> bool | ProcessArgumentsUnavailable:
    if pid <= 0 or not command:
        return False
    observed = process_args(pid)
    if isinstance(observed, ProcessAbsent):
        return False
    if isinstance(observed, ProcessArgumentsUnavailable):
        return observed
    separator = " " if isinstance(observed, ProcessCommandText) else "\0"
    parts = observed.text.split(" ") if isinstance(observed, ProcessCommandText) else observed

    def matches(index: int, expected: Sequence[str]) -> bool:
        if not expected:
            return not isinstance(observed, ProcessCommandText) or index == len(parts)
        ends = range(index + 1, len(parts) + 1) if isinstance(observed, ProcessCommandText) else (index + 1,)
        for end in ends:
            actual = separator.join(parts[index:end])
            if (actual == expected[0] or Path(actual).resolve() == Path(expected[0]).resolve()) and matches(end, expected[1:]):
                return True
        return False

    return any(Path(part).name == Path(command[0]).name and matches(index + 1, command[1:])
               for index, part in enumerate(parts) if part)


def _possibly_live(match: bool | ProcessArgumentsUnavailable) -> bool:
    return match is not False


def runs_command(pid: int, command: Sequence[str]) -> bool:
    """Keep the command live when its arguments are unavailable."""
    return _possibly_live(_command_match(pid, command))


@dataclass(frozen=True)
class ProcessTableUnavailable:
    """This machine has no /proc process table."""


def supervisor_records() -> dict[Path, frozenset[int]] | ProcessTableUnavailable:
    """Map each record that a live supervisor names to the supervisor process IDs."""
    if not Path("/proc/self").exists():
        return ProcessTableUnavailable()
    found: dict[Path, set[int]] = {}
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        parts = _proc_args(process)
        for index, part in enumerate(parts[:-2]):
            if Path(part).name == "detached_runs.py" and parts[index + 1] == "supervise" and parts[index + 2]:
                found.setdefault(Path(parts[index + 2]).resolve(), set()).add(int(process.name))
                break
    return {path: frozenset(pids) for path, pids in found.items()}


def supervisor_match(pid: int, record_path: Path) -> bool | ProcessArgumentsUnavailable:
    """Match the supervisor and its record when the process arguments are available."""
    return _command_match(pid, ["detached_runs.py", "supervise", str(record_path.resolve())])


def supervises(pid: int, record_path: Path) -> bool:
    """Keep the supervisor live when its arguments are unavailable."""
    return _possibly_live(supervisor_match(pid, record_path))

"""The natedev paths of the read limit of a hand made run.

scripts/build_targets/mac_linux.py holds the rule set of the limit, which the guest uses too: bwrap starts the
program in a new mount namespace that holds only the bound paths. A path that is not bound does not exist for the
program, so no word form can make a tool read it. The program has its own network with only a loopback, so it
reaches no socket or port of another process; cargo runs offline.
Each path comes from the program environment or the tool default; a missing path is not bound.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess

from scripts.build_hold import cargo_folder
from scripts.build_targets.mac_linux import (bindable, home_folder, limit_words, output_folder, read_paths,
                                             written_paths)
from scripts.lib import conf
from scripts.production.hand_made_route import Output, Program, Refused, in_checkout

# The folder of the build-hold cargo wrapper; it runs files from its sibling lib folder.
BUILD_HOLD = "build_hold"
# The repository of the tools that check the run: the repository of this code.
CHECKER = Path(__file__).resolve().parents[2]
TARGET_REFUSAL = "the run builds in {place}; leave out --target-dir, or give that folder"
OUTPUT_REFUSAL = "the run cannot build in {place}, because {reason}; {advice}"
# The advice of OUTPUT_REFUSAL when CARGO_TARGET_DIR names the output folder: the target link is then the output.
NAMED_OUTPUT_ADVICE = "leave out CARGO_TARGET_DIR"
# The advice of OUTPUT_REFUSAL for the checkout target: the folder step of a cargo build makes the target link.
TARGET_LINK_ADVICE = ("a cargo build in a checkout that has a build folder makes the target link to that folder; "
                      + "make such a checkout with worktrees.py make <repo> <unit> <short-name>")
TOOLS_REFUSAL = ("the run writes {bind}, which holds or is in the tools that check the run; use a worktree outside "
                 + "those tools, leave out CARGO_TARGET_DIR, and give SCCACHE_DIR a folder outside those tools")
# The texts of the cargo errors that offline mode causes, from cargo 1.99.1 runs with CARGO_NET_OFFLINE=true:
# a git source, a registry resolve, and a crate download.
OFFLINE_TEXTS = (b"you are in the offline mode (--offline)", b"offline mode (via `--offline`)",
                 b"--offline was specified")
# The bytes at the end of the program output that the offline check reads.
TAIL_BYTES = 64 * 1024
# An ANSI colour or style code, which cargo puts in its output when the terminal takes colour.
ANSI_CODE = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")
NO_NETWORK = ("a hand made run has no network, so cargo cannot fetch a dependency; a normal run of this line "
              + "must fetch the dependency first")
FOLDER_STEP_REFUSAL = "the build-hold cargo folder check refused the run, so the program did not start: {reason}"


def assigned_output(checkout: Path) -> Path | None:
    """The existing build folder that the build folder model links from the checkout target, or None."""
    try:
        output = cargo_folder.assigned_output(checkout)
    except cargo_folder.STEP_ERRORS:
        return None
    if not isinstance(output, cargo_folder.LinkedOutput) or not output.path.is_dir():
        return None
    return output.path


def bound_output(checkout: Path, environment: Mapping[str, str]) -> Path:
    """The folder that the limit binds for the output: the build folder that the checkout target link names
    when it is the build folder of this checkout, else the output folder.

    A link to another build folder is not bound: a build script that the run puts there runs in the next normal
    build of that checkout."""
    place = output_folder(checkout, environment)
    if "CARGO_TARGET_DIR" not in environment and place.is_symlink():
        assigned = assigned_output(checkout)
        if assigned is not None and os.path.realpath(place) == str(assigned) and bindable(assigned, home_folder(
                environment), read=False):
            return assigned
    return Path(os.path.normpath(place))


def held_output(argv: Sequence[str], checkout: Path, environment: Mapping[str, str],
                held: ExitStack, *, note_outside: bool) -> tuple[int, ...]:
    """For a cargo run, the folder steps of the build-hold cargo wrapper, before cargo starts with the program
    environment: make or check the checkout target link to the assigned build folder, hold the build folder lock
    until held closes, and write the use stamp.

    The run does the steps when the wrapper does them (cargo_folder.folder_step_due). The wrapper reads its own
    environment, so CARGO_FOLDER_CHECKED and BUILDLOG_STEP_SCOPE come from the environment of this process:
    the program environment does not pass them. With note_outside False, the steps give no note for a path
    outside the build folders (cargo_folder.checked_step).

    The descriptor of the held lock, which the program also gets, as the wrapper gives it to cargo; empty when
    the run holds no lock. A refusal of the folder check raises Refused.
    """
    if not argv or Path(argv[0]).name != "cargo":
        return ()
    try:
        if not cargo_folder.folder_step_due(argv[1:], os.environ):
            return ()
        output = cargo_folder.checked_step(list(argv[1:]), checkout, held, environment, note_outside=note_outside)
    except cargo_folder.STEP_ERRORS as error:
        raise Refused(FOLDER_STEP_REFUSAL.format(reason=error)) from error
    return (output.descriptor,) if isinstance(output, cargo_folder.CheckedOutputLock) else ()


def python_cache_folder() -> Path:
    """The folder of the python path cache that scripts/lib/py reads and runs, from scripts/lib/py itself."""
    launcher = CHECKER / "scripts" / "lib" / "py"
    result = subprocess.run([str(launcher), "--cache-folder"], stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, check=True)
    return Path(result.stdout.strip())


def tool_folders(environment: Mapping[str, str]) -> list[Path]:
    """The real folders of the tools that check the run: the repository of this code, the build-hold cargo
    wrapper folder and its lib folder, the config folder, and the python path cache folder."""
    return [Path(os.path.realpath(folder)) for folder in (
        CHECKER, *wrapper_folders(environment.get("PATH", "")), conf.directory(), python_cache_folder())]


def tools_refusal(checkout: Path, environment: Mapping[str, str]) -> str:
    """The refusal of a write bind that holds, or is in, a tool folder; empty for none. The run can write each
    write bind, so it could change the check of the next run. The limit binds no path that is not bindable, so
    only a bindable path is checked. The checkout comes first, before the build folder model gives the output
    folder."""
    tools, home = tool_folders(environment), home_folder(environment)
    for bind in write_binds(checkout, environment):
        real = Path(os.path.normpath(bind))
        if bindable(bind, home, read=False) and any(tool.is_relative_to(real) or real.is_relative_to(tool)
                                                    for tool in tools):
            return TOOLS_REFUSAL.format(bind=bind)
    return ""


def start_refusal(argv: Sequence[str], checkout: Path, environment: Mapping[str, str]) -> str:
    """The refusal of a run that the limit cannot take, at the attempt start. As at the run start, the folder steps
    of a cargo run come first, so the limit judges the target link that the steps repair; the lock ends with this
    check. The run start gives the note for a path outside the build folders, so this check gives none."""
    with ExitStack() as held:
        try:
            _ = held_output(argv, checkout, environment, held, note_outside=False)
        except Refused as refusal:
            return str(refusal)
        return limit_refusal(argv, checkout, environment)


def limit_refusal(argv: Sequence[str], checkout: Path, environment: Mapping[str, str]) -> str:
    """The refusal of a run that the limit cannot take, after the folder steps; the tools check comes before the
    build folder model."""
    return tools_refusal(checkout, environment) or output_refusal(argv, checkout, environment)


def output_refusal(argv: Sequence[str], checkout: Path, environment: Mapping[str, str]) -> str:
    """The refusal of an output folder that the limit cannot bind, or of a cargo word that names another output
    folder; empty for none. The words after the first -- are the words of another program."""
    place = output_folder(checkout, environment)
    bound = bound_output(checkout, environment)
    advice = NAMED_OUTPUT_ADVICE if "CARGO_TARGET_DIR" in environment else TARGET_LINK_ADVICE
    if home_folder(environment).is_relative_to(bound):
        return OUTPUT_REFUSAL.format(place=place, reason="it holds the home folder", advice=advice)
    if os.path.realpath(bound) != str(bound):
        reason = ("its link does not name the build folder of this checkout"
                  if "CARGO_TARGET_DIR" not in environment and place.is_symlink()
                  else "a part of its path is a symbolic link")
        return OUTPUT_REFUSAL.format(place=place, reason=reason, advice=advice)
    if not argv or Path(argv[0]).name != "cargo":
        return ""
    words = iter(argv[1:])
    for word in words:
        if word == "--":
            break
        if word == "--target-dir":
            value = next(words, "")
        elif word.startswith("--target-dir="):
            value = word.partition("=")[2]
        elif "target-dir" in word:
            # A --config value such as build.target-dir="x" names a folder that the run does not bind.
            return TARGET_REFUSAL.format(place=place)
        else:
            continue
        if not value or os.path.realpath(checkout / value) != os.path.realpath(place):
            return TARGET_REFUSAL.format(place=place)
    return ""


def wrapper_folders(path: str) -> list[Path]:
    """The build-hold cargo wrapper folder and its lib folder, when the first cargo on PATH is the wrapper."""
    found = shutil.which("cargo", path=path)
    if found is None:
        return []
    folder = Path(os.path.realpath(found)).parent
    return [folder, folder.parent / "lib"] if folder.name == BUILD_HOLD else []


def write_binds(checkout: Path, environment: Mapping[str, str]) -> Iterator[Path]:
    """The paths that the run reads and writes, the checkout first, the output folder last."""
    yield from written_paths(checkout, environment)
    yield bound_output(checkout, environment)


def limit_argv(bwrap: str, program: Program, temp: Path, environment: Mapping[str, str], status_fd: int) -> list[str]:
    """The bwrap words that start the program in its checkout under the read limit (mac_linux.limit_words).

    The program starts from the file that the check found. Each file that the check read, and the start file,
    stays readable at its real path, also outside the checkout. bwrap writes its status lines to status_fd.
    """
    checkout, start = program.repo, program.start
    argv = [str(start), *program.argv[1:]] if start is not None else list(program.argv)
    files = [Path(os.path.realpath(path))
             for path in (*(path for path, _ in program.checked), *([start] if start is not None else []))]
    read = [*read_paths(environment, [temp, checkout]), *(file for file in files if not in_checkout(file, checkout))]
    words = limit_words(bwrap, status_fd, temp, checkout, home_folder(environment), read,
                        write_binds(checkout, environment))
    return [*words, *argv]


@dataclass
class OutputTail:
    """The program output, which passes at once and unchanged; the last TAIL_BYTES bytes stay for the offline
    check."""
    output: Output
    tail: bytes = b""

    def write(self, chunk: bytes) -> None:
        self.output.write(chunk)
        self.tail = (self.tail + chunk)[-TAIL_BYTES:]


def offline_failure(argv: Sequence[str], status: int, tail: bytes) -> bool:
    """cargo failed because offline mode stopped a dependency fetch: the program is cargo, the status is not 0,
    and the end of the output has a cargo offline text."""
    text = ANSI_CODE.sub(b"", tail)
    return (bool(argv) and Path(argv[0]).name == "cargo" and status != 0
            and any(offline in text for offline in OFFLINE_TEXTS))

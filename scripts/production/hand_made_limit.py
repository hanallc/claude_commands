"""The read limit of a hand made run on natedev.

bwrap starts the program in a new mount namespace that holds only the bound paths. A path that is not bound
does not exist for the program, so no word form can make a tool read it. The program has its own network
with only a loopback, so it reaches no socket or port of another process; cargo runs offline.
Each path comes from the program environment or the tool default; a missing path is not bound.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess

from scripts.build_hold import cargo_folder
from scripts.lib import conf
from scripts.production.hand_made_route import Output, Program, in_checkout
from scripts.worktree import worktrees

# The folders of the system that the run reads. /bin and /usr hold the /bin/sh and /usr/bin/env links.
SYSTEM_FOLDERS = tuple(Path(name) for name in ("/nix/store", "/run/current-system", "/etc", "/bin", "/usr"))
# The system folders in which a user can own an entry; the run does not see such an entry.
OWNED_FOLDERS = tuple(Path(name) for name in ("/etc", "/bin", "/usr"))
# No one can change a file here, so a read only bind can have a link into it.
STORE = Path("/nix/store")
# The run sees its attempt temp folder at this path, and TMPDIR names it.
TEMP = Path("/tmp")
# The socket of the private sccache server of the run, in the attempt temp folder.
SCCACHE_SOCKET = TEMP / "sccache.sock"
# The folder of the build-hold cargo wrapper; it runs files from its sibling lib folder.
BUILD_HOLD = "build_hold"
# The repository of the tools that check the run: the repository of this code.
CHECKER = Path(__file__).resolve().parents[2]
# The lock files of the cargo registry and git folders, at the top of CARGO_HOME.
CARGO_LOCKS = (".package-cache", ".package-cache-mutate", ".global-cache")
# The key of the bwrap status line that gives the exit status of the program.
EXIT_STATUS = re.compile(rb'"exit-code"\s*:')
TARGET_REFUSAL = "the run builds in {place}; leave out --target-dir, or give that folder"
OUTPUT_REFUSAL = ("the run cannot build in {place}, because {reason}; make the checkout target link name the build "
                  + "folder of this checkout, or set CARGO_TARGET_DIR to a real folder that does not hold the home folder")
TOOLS_REFUSAL = ("the run writes {bind}, which holds or is in the tools that check the run; use a worktree, and "
                 + "give CARGO_TARGET_DIR, SCCACHE_DIR and CARGO_HOME other folders")
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
BWRAP_MISSING = "bwrap is not on PATH, so the run cannot start under the read limit; the program did not start"
BWRAP_FAILED = ("bwrap did not start the program under the read limit (status {status}); the program did not start, "
                + "and the output above gives the cause")


def output_folder(checkout: Path, environment: Mapping[str, str]) -> Path:
    """The one cargo build output folder of the run: CARGO_TARGET_DIR when it is set, else the checkout target."""
    return checkout / environment.get("CARGO_TARGET_DIR", "target")


def assigned_output(checkout: Path) -> Path | None:
    """The existing build folder that the build folder model links from the checkout target, or None."""
    try:
        output = cargo_folder.assigned_output(checkout)
    except (OSError, RuntimeError, ValueError, KeyError, worktrees.Refused):
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


def home_folder(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("HOME") or Path.home())


def bindable(path: Path, home: Path, *, read: bool) -> bool:
    """The limit can bind the path: it exists, it holds no home folder, and no part of it is a link.

    The program can write a read and write folder, so it can put a link there for the next run. A read only
    bind can have a link into /nix/store, because no one can change that file.
    """
    real = Path(os.path.realpath(path))
    return (os.path.lexists(path) and not home.is_relative_to(path)
            and (real == Path(os.path.normpath(path)) or (read and real.is_relative_to(STORE))))


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


def limit_refusal(argv: Sequence[str], checkout: Path, environment: Mapping[str, str]) -> str:
    """The refusal of a run that the limit cannot take; the tools check comes before the build folder model."""
    return tools_refusal(checkout, environment) or output_refusal(argv, checkout, environment)


def output_refusal(argv: Sequence[str], checkout: Path, environment: Mapping[str, str]) -> str:
    """The refusal of an output folder that the limit cannot bind, or of a cargo word that names another output
    folder; empty for none. The words after the first -- are the words of another program."""
    place = output_folder(checkout, environment)
    bound = bound_output(checkout, environment)
    if home_folder(environment).is_relative_to(bound):
        return OUTPUT_REFUSAL.format(place=place, reason="it holds the home folder")
    if os.path.realpath(bound) != str(bound):
        reason = ("its link does not name the build folder of this checkout"
                  if "CARGO_TARGET_DIR" not in environment and place.is_symlink()
                  else "a part of its path is a symbolic link")
        return OUTPUT_REFUSAL.format(place=place, reason=reason)
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


def shown(folder: Path, kept: Sequence[Path]) -> bool:
    """A PATH folder that the limit adds: absolute, not in a system folder, and it holds no temp folder or
    checkout."""
    return (folder.is_absolute() and not any(folder.is_relative_to(system) for system in SYSTEM_FOLDERS)
            and not any(path.is_relative_to(folder) for path in kept))


def wrapper_folders(path: str) -> list[Path]:
    """The build-hold cargo wrapper folder and its lib folder, when the first cargo on PATH is the wrapper."""
    found = shutil.which("cargo", path=path)
    if found is None:
        return []
    folder = Path(os.path.realpath(found)).parent
    return [folder, folder.parent / "lib"] if folder.name == BUILD_HOLD else []


def unowned(folder: Path) -> Iterator[Path]:
    """Each entry under the folder that root does not own; a link is no entry, because its target is bound or
    hidden on its own. A folder that cannot be read is skipped: the program cannot read it either."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        try:
            owner = entry.stat(follow_symlinks=False).st_uid
        except OSError:
            continue
        if owner != 0:
            yield Path(entry.path)
        elif entry.is_dir(follow_symlinks=False):
            yield from unowned(Path(entry.path))


def hidden_words() -> list[str]:
    """The bwrap words that hide each entry of a system folder that root does not own: an empty read only folder
    over a folder, /dev/null over a file."""
    words: list[str] = []
    for folder in OWNED_FOLDERS:
        for path in unowned(folder):
            words += (["--tmpfs", str(path), "--remount-ro", str(path)] if path.is_dir()
                      else ["--ro-bind", os.devnull, str(path)])
    return words


def write_binds(checkout: Path, environment: Mapping[str, str]) -> Iterator[Path]:
    """The paths that the run reads and writes, the checkout first."""
    home = home_folder(environment)
    cargo_home = Path(environment.get("CARGO_HOME") or home / ".cargo")
    yield checkout
    yield from (cargo_home / "registry", cargo_home / "git", *(cargo_home / name for name in CARGO_LOCKS),
                Path(environment.get("SCCACHE_DIR") or home / ".cache" / "sccache"))
    yield bound_output(checkout, environment)


def limit_argv(bwrap: str, program: Program, temp: Path, environment: Mapping[str, str], status_fd: int) -> list[str]:
    """The bwrap words that start the program in its checkout under the read limit.

    The program starts from the file that the check found. Each file that the check read, and the start file,
    stays readable at its real path, also outside the checkout. The attempt temp folder is /tmp, and its bind
    comes first, so a later bind under /tmp stays visible. A read and write bind comes after each read only
    bind, so a read and write folder in a read only folder stays writable. Only a bindable path is bound.
    bwrap writes its status lines to status_fd.
    """
    checkout, start = program.repo, program.start
    argv = [str(start), *program.argv[1:]] if start is not None else list(program.argv)
    files = [Path(os.path.realpath(path))
             for path in (*(path for path, _ in program.checked), *([start] if start is not None else []))]
    home = home_folder(environment)
    cargo_home = Path(environment.get("CARGO_HOME") or home / ".cargo")
    path = environment.get("PATH", "")
    folders = [Path(entry) for entry in path.split(":") if entry]
    kept = [temp, checkout]
    read = [Path(environment.get("RUSTUP_HOME") or home / ".rustup"), cargo_home / "bin",
            cargo_home / "config.toml", *(folder for folder in folders if shown(folder, kept)),
            *(file for file in files if not in_checkout(file, checkout))]
    write = list(write_binds(checkout, environment))
    words = [bwrap, "--json-status-fd", str(status_fd), "--die-with-parent", "--unshare-pid", "--unshare-net",
             "--dev", "/dev",
             "--proc", "/proc", "--bind", str(temp), str(TEMP)]
    for system in SYSTEM_FOLDERS:
        words += ["--ro-bind", str(system), str(system)]
    words += hidden_words()
    for option, paths in (("--ro-bind", read), ("--bind", write)):
        for item in dict.fromkeys(paths):
            if bindable(item, home, read=option == "--ro-bind"):
                words += [option, str(item), str(item)]
    # The root of the limit is not writable, so a write to a folder that is not bound fails at once.
    return [*words, "--remount-ro", "/", "--setenv", "TMPDIR", str(TEMP), "--setenv", "SCCACHE_SERVER_UDS",
            str(SCCACHE_SOCKET), "--setenv", "CARGO_NET_OFFLINE", "true", "--chdir", str(checkout), "--", *argv]


def setup_failed(status: int, status_fd: int) -> bool:
    """bwrap did not set up the limit or did not start the program.

    bwrap then exits with 1 and writes no exit-code line; it writes that line only when the program ends.
    """
    if status != 1:
        return False
    os.set_blocking(status_fd, False)
    data = b""
    try:
        while chunk := os.read(status_fd, 65536):
            data += chunk
    except BlockingIOError:
        pass
    return EXIT_STATUS.search(data) is None


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

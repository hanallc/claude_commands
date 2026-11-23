"""Check the workspace and output folder before a Cargo step."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from enum import IntEnum
import fcntl
import functools
import os
from pathlib import Path
import re
import stat
import sys

from scripts.lib import build_folder_lock, build_folders, cargo_target_config, git_files
from scripts.lib.atomic_file import write_bytes_atomic
from scripts.worktree import worktrees


# The build-hold cargo wrapper. It holds the cargo word lists, each on one line, and this module reads them there.
WRAPPER = Path(__file__).with_name("cargo")


class OutsideBuildFolders(ValueError):
    """The step would use a path outside the assigned build folder."""


class CleanExit(IntEnum):
    REVIEWED = 0
    USE_CARGO = 3


@dataclass(frozen=True)
class CargoWorkspace:
    cwd: Path
    home: str
    root: Path
    checkout: Path


@dataclass(frozen=True)
class LinkedOutput:
    path: Path


@dataclass(frozen=True)
class LocalOutput:
    path: Path


@dataclass(frozen=True)
class UnassignedWorkspace:
    reason: str


@functools.cache
def wrapper_words(name: str) -> frozenset[str]:
    """The words of the wrapper list on the line name=(word ...)."""
    found = re.search(rf"^{name}=\(([^)]*)\)$", WRAPPER.read_text(), re.MULTILINE)
    if found is None:
        raise ValueError(f"{WRAPPER}: no {name} list")
    return frozenset(found.group(1).split())


def folder_step_due(argv: Sequence[str], environment: Mapping[str, str]) -> bool:
    """The wrapper does its folder steps for the cargo words argv in the environment: a build subcommand other
    than install, no help or version word after it, and no outer step that did the steps."""
    if environment.get("CARGO_FOLDER_CHECKED") == "1" or environment.get("BUILDLOG_STEP_SCOPE"):
        return False
    value_options, skip = wrapper_words("value_options"), False
    for index, word in enumerate(argv):
        if skip:
            skip = False
        elif word.startswith("-"):
            skip = word in value_options
        elif not word.startswith("+"):
            subcommand = word
            rest = argv[index + 1:]
            break
    else:
        return False
    if subcommand == "install" or subcommand not in wrapper_words("build_subcommands"):
        return False
    words = rest[:rest.index("--")] if "--" in rest else rest
    return not any(word in ("-h", "--help", "-V", "--version") or (word == "--build-info" and subcommand == "mend")
                   for word in words)


def _workspace(argv: list[str], cwd: Path, environment: Mapping[str, str]) -> CargoWorkspace:
    directory = cargo_target_config.cargo_path_argument(argv, "-C") or next(
        (arg[2:] for arg in argv if arg.startswith("-C") and len(arg) > 2), "")
    if directory:
        cwd = (cwd / directory).resolve()
    home = cargo_target_config.cargo_home_folder(str(cwd), environment)
    manifest = cargo_target_config.cargo_workspace_manifest(argv, str(cwd), home).resolve()
    checkout = git_files.locate(manifest.parent).toplevel
    return CargoWorkspace(cwd, home, manifest.parent, checkout)


def assigned_output(checkout: Path) -> LinkedOutput | LocalOutput | UnassignedWorkspace:
    """The build output folder that the build folder model gives for the checkout."""
    layout = worktrees.worktree_output(checkout)
    if isinstance(layout, worktrees.RecordedWorktreeOutput):
        return LinkedOutput(worktrees.checked_output(layout))
    form = build_folders.parse_form(checkout)
    if isinstance(form, build_folders.UnverifiedFolderPath):
        raise form.error
    if isinstance(form, build_folders.BuildCopy):
        return LocalOutput(checkout / "target")
    if isinstance(form, build_folders.CiOutput):
        return LocalOutput(checkout)
    try:
        word = build_folders.repo_word(checkout)
        if worktrees.main_checkout_for_word(word) == checkout:
            return LinkedOutput(build_folders.main_output_path(word))
    except ValueError:
        pass
    return UnassignedWorkspace(f"{checkout}: no build folder. Use worktrees.py make <repo> <unit> <short-name>")


def _check_paths(workspace: CargoWorkspace, argv: list[str], output: Path, environment: Mapping[str, str]) -> None:
    cwd = str(workspace.cwd)
    settings = cargo_target_config.cargo_folder_settings(
        argv, cwd, environment, policy=cargo_target_config.CargoConfigPolicy.CURRENT_STRICT)
    destination = settings.get("target-dir")
    selected = workspace.root / "target" if destination is None else destination.base / destination.value
    if selected.resolve() != output.resolve():
        raise OutsideBuildFolders(f"{selected}: Cargo must use {output}")
    intermediate = settings.get("build-dir")
    if intermediate is not None:
        selected = Path(cargo_target_config.cargo_build_folder(intermediate, argv, cwd, workspace.home))
        if selected.resolve() != output.resolve():
            raise OutsideBuildFolders(f"{selected}: Cargo must use {output}")


def _use_linked_output(workspace: CargoWorkspace, output: Path,
                       locks: ExitStack) -> int:
    target = workspace.checkout / "target"
    if not target.is_symlink() and target.exists():
        raise OutsideBuildFolders(f"{target}: expected a link to {output}; retain the existing path")
    root = build_folders.build_root()
    directory = locks.enter_context(build_folder_lock.folder_parent(output, root, create=True))
    current_link = target.is_symlink() and target.readlink() == output and output.is_dir() and not output.is_symlink()
    descriptor = build_folder_lock.open_lock(output, directory, create=True)
    _ = locks.callback(os.close, descriptor)
    mode = fcntl.LOCK_EX if not current_link else fcntl.LOCK_SH
    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
    if not build_folder_lock.lock_current(output, directory, descriptor):
        raise ValueError(f"{output}: the build folder lock changed")
    worktrees.ensure_target_link(workspace.checkout, output, save_existing=False)
    if mode == fcntl.LOCK_EX:
        # The exclusive lock covers only the new folder and its link, so a parallel cargo step in the checkout
        # takes the shared lock. flock does not change a lock in one step: a removal in that gap makes the shared
        # lock fail, or makes one of the checks below fail.
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        if not build_folder_lock.lock_current(output, directory, descriptor) or not output.is_dir():
            raise ValueError(f"{output}: the build folder lock changed")
    return descriptor


def _stamp_assignment(output: Path, checkout: Path) -> None:
    """Store the verified checkout in the existing Cargo use stamp."""
    stamp = output / build_folders.COPY_USE_STAMP
    evidence = os.fsencode(checkout) + b"\n"
    try:
        descriptor = os.open(stamp, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        pass
    else:
        with os.fdopen(descriptor, "rb") as stream:
            inode = os.fstat(stream.fileno())
            if not stat.S_ISREG(inode.st_mode) or inode.st_nlink != 1:
                raise ValueError(f"{stamp}: invalid use stamp")
            if stream.read(len(evidence) + 1) == evidence:
                os.utime(stream.fileno())
                return
    write_bytes_atomic(stamp, evidence, mode=0o600)


@dataclass(frozen=True)
class CheckedOutputLock:
    descriptor: int


@dataclass(frozen=True)
class NoOutputLock:
    """A copy or CI runner owns the output lock."""


def check_step(argv: list[str], cwd: Path, locks: ExitStack, environment: Mapping[str, str], *,
               enforce: bool) -> CheckedOutputLock | NoOutputLock:
    """Check the Cargo paths that the environment gives and hold the assigned output."""
    if "--" in argv:
        argv = argv[:argv.index("--")]
    try:
        workspace = _workspace(argv, cwd, environment)
    except (OSError, ValueError) as error:
        raise OutsideBuildFolders(str(error)) from error
    output = assigned_output(workspace.checkout)
    if isinstance(output, UnassignedWorkspace):
        raise OutsideBuildFolders(output.reason)
    if isinstance(output, LinkedOutput):
        descriptor = _use_linked_output(workspace, output.path, locks)
        try:
            _check_paths(workspace, argv, output.path, environment)
        except OutsideBuildFolders as error:
            if enforce:
                raise
            print(f"cargo: {error}", file=sys.stderr)
        _stamp_assignment(output.path, workspace.checkout)
        return CheckedOutputLock(descriptor)
    path = output.path
    if path != workspace.checkout and (path.is_symlink() or (path.exists() and not path.is_dir())):
        raise OutsideBuildFolders(f"{path}: the copy needs a real target folder at its root")
    _check_paths(workspace, argv, path, environment)
    return NoOutputLock()


# The errors that end a checked step with no Cargo run.
STEP_ERRORS = (OSError, RuntimeError, ValueError, KeyError, worktrees.Refused)


def checked_step(argv: list[str], cwd: Path, locks: ExitStack,
                 environment: Mapping[str, str], *, note_outside: bool) -> CheckedOutputLock | NoOutputLock:
    """The folder steps of the build-hold cargo wrapper before Cargo starts with the environment: make or check
    the checkout target link to the assigned build folder, hold the build folder lock in locks, and write the use
    stamp.

    A path outside the build folders is a note when the build folder model only reports; with note_outside
    False, a later step gives that note. Each other error in STEP_ERRORS ends the step.
    """
    enforce = worktrees.enforce_build_folders()
    try:
        return check_step(argv, cwd, locks, environment, enforce=enforce)
    except OutsideBuildFolders as error:
        if enforce:
            raise
        if note_outside:
            print(f"cargo: {error}", file=sys.stderr)
        return NoOutputLock()


def clean_output(argv: list[str], cwd: Path) -> CleanExit:
    """Report the assigned output for manual review."""
    if argv != ["clean"]:
        return CleanExit.USE_CARGO
    try:
        workspace = _workspace(argv, cwd, os.environ)
    except (OSError, ValueError):
        return CleanExit.USE_CARGO
    output = assigned_output(workspace.checkout)
    if not isinstance(output, LinkedOutput):
        return CleanExit.USE_CARGO
    _check_paths(workspace, argv, output.path, os.environ)
    print(f"kept for a manual removal list: {output.path} (cargo, clean requested)", file=sys.stderr)
    return CleanExit.REVIEWED


def main(argv: list[str]) -> int:
    if argv[:1] == ["--clean-output"]:
        try:
            return clean_output(argv[1:], Path.cwd())
        except STEP_ERRORS as error:
            print(f"cargo: {error}", file=sys.stderr)
            return 1
    execute = argv[:1] == ["--exec-checked"]
    if execute:
        if len(argv) < 4 or argv[1] not in ("direct", "queue"):
            print("cargo: invalid checked execution request", file=sys.stderr)
            return 2
        _, route, executable, *argv = argv
    else:
        route, executable = "", ""
    with ExitStack() as locks:
        try:
            output = checked_step(argv, Path.cwd(), locks, os.environ, note_outside=True)
        except STEP_ERRORS as error:
            print(f"cargo: {error}", file=sys.stderr)
            return 1
        if execute:
            if isinstance(output, CheckedOutputLock):
                os.set_inheritable(output.descriptor, True)
                if route == "queue":
                    os.environ["CARGO_OUTPUT_LOCK_FD"] = str(output.descriptor)
            os.environ["CARGO_FOLDER_CHECKED"] = "1"
            command = [executable, *argv]
            if route == "queue":
                queue = Path(__file__).resolve().parents[1] / "build_queue/queue_place.py"
                command = [sys.executable, str(queue), "--", *command]
            try:
                os.execv(command[0], command)
            except OSError as error:
                print(f"cargo: {error}", file=sys.stderr)
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

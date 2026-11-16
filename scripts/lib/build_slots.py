#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Take reusable repository slots and preserve source before each reset."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from enum import Enum, auto
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from types import MappingProxyType
from typing import BinaryIO, cast
from uuid import uuid4

from scripts.buildlog.store import host_name
from scripts.lib import build_folders, conf, git_files, git_rescue


SLOT_NAME = re.compile(r"(.+)-slot([1-9][0-9]*)")
LEASE_SUFFIX = ".lease.json"


@dataclass(frozen=True)
class Slot:
    path: Path
    cargo_dir: Path
    repository: str
    number: int
    env: Mapping[str, str]


class SlotsFull(RuntimeError):
    """Every slot in the pool has a holder."""

    def __init__(self, repository: str, machine: str, maximum: int) -> None:
        self.repository: str = repository
        self.machine: str = machine
        self.maximum: int = maximum
        super().__init__(f"slots: all {maximum} slots for {repository} on {machine} are held")


class SlotRepositoryMismatch(ValueError):
    """The slot belongs to a different source repository."""

    def __init__(self, path: Path, source: Path) -> None:
        self.path: Path = path
        self.source: Path = source
        super().__init__(f"slots: {path} belongs to a different repository than {source}")


class SlotsNotConfigured(ValueError):
    """The user has not activated repository slots on this machine."""

    def __init__(self, machine: str) -> None:
        super().__init__(f"slots: roots.slots.{machine} is not configured; slots are not active")


class SlotGitFailure(RuntimeError):
    """A slot Git command failed or exceeded its configured deadline."""


@dataclass(frozen=True)
class RepositoryPool:
    source: Path
    repository: str
    machine: str
    root: Path
    cargo_root: Path


def _git(source: Path, *arguments: str, pass_fds: Sequence[int] = (), accepted: tuple[int, ...] = (0,)) -> str:
    timeout = build_folders.repository_git_timeout()
    try:
        result = subprocess.run(["git", "-C", str(source), *arguments],
                                capture_output=True, text=True, pass_fds=pass_fds, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise SlotGitFailure(f"git {' '.join(arguments)} in {source}: timed out after {timeout:g} seconds") from error
    if result.returncode not in accepted:
        raise SlotGitFailure(f"git {' '.join(arguments)} in {source}: {result.stderr.strip()}")
    return result.stdout.strip()



def source_checkout(source: Path) -> Path:
    """Read the main checkout, including repositories with separate Git metadata."""
    # --show-cdup prints a line only inside a work tree: the path from source to its top.
    common, current, bare, *cdup = _git(source, "rev-parse", "--path-format=absolute", "--git-common-dir",
                                       "--git-dir", "--is-bare-repository", "--show-cdup").splitlines()
    if common == current and bare == "false":
        if cdup:
            return (source / cdup[0]).resolve()
        return Path(_git(source, "rev-parse", "--show-toplevel").strip())
    first = _git(source, "worktree", "list", "--porcelain").splitlines()[0]
    checkout = Path(first.removeprefix("worktree "))
    configured = _git(source, "--git-dir", common, "config", "--default", str(checkout),
                      "--get", "core.worktree").strip()
    return (Path(common) / configured).resolve()


def _root(settings: conf.ConfigRead, key: str) -> Path:
    return Path(os.path.expandvars(settings.text(key))).expanduser().resolve()


def slot_root(values: Mapping[str, str], machine: str) -> str:
    """Select the machine's slot root, with the common root as the default."""
    return values.get(f"roots.slots.{machine}", values.get("roots.slots", ""))


def configured_slot_root() -> Path:
    """Read the machine's slot root without creating a folder."""
    settings = conf.read_or_repository("build_folders")
    machine = host_name()
    root = slot_root(settings.values(), machine)
    if not root:
        raise SlotsNotConfigured(machine)
    return Path(os.path.expandvars(root)).expanduser().resolve()


def slot_cargo_root(settings: conf.ConfigRead) -> Path:
    """Read the root for each slot's build output."""
    return _root(settings, "roots.slot_cargo")


def lease_path(path: Path) -> Path:
    return path.with_name(path.name + LEASE_SUFFIX)


def slot_paths(root: Path) -> list[tuple[Path, str, int]]:
    """List each slot's path, repository, and number from its folder or lease record."""
    names = {entry.name.removesuffix(LEASE_SUFFIX) for entry in root.iterdir()}
    return [(root / name, match[1], int(match[2])) for name in sorted(names)
            if (match := SLOT_NAME.fullmatch(name)) is not None]


def unused(last_use: datetime, settings: conf.ConfigRead, now: datetime) -> bool:
    """Check whether the last use exceeds slots.unused_days."""
    return last_use < now - timedelta(days=settings.whole("slots.unused_days"))


def configured_maximum(settings: conf.ConfigRead, machine: str, repository: str) -> int:
    """Read the repository limit or the machine's default limit."""
    key = f"slots.{machine}.{repository}.max"
    return settings.whole(key if key in settings.values() else f"slots.{machine}.default_max")


def _pool(source: Path) -> RepositoryPool:
    settings = conf.read_or_repository("build_folders")
    machine = host_name()
    root = configured_slot_root()
    main = source_checkout(source)
    timeout = settings.number("git_timeout_seconds")
    try:
        origin = subprocess.run(["git", "-C", str(main), "remote", "get-url", "origin"],
                                capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise SlotGitFailure(f"git remote get-url origin in {main}: timed out after {timeout:g} s") from error
    if origin.returncode not in (0, 2):
        origin.check_returncode()
    repository = (origin.stdout.strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
                  if origin.returncode == 0 else main.name)
    return RepositoryPool(main, repository, machine, root, slot_cargo_root(settings))


def _maximum(pool: RepositoryPool, report: Callable[[str], None]) -> int:
    settings = conf.read_or_repository("build_folders")
    key = f"slots.{pool.machine}.{pool.repository}.max"
    maximum = configured_maximum(settings, pool.machine, pool.repository)
    if key in settings.values():
        return maximum
    report(f"slots: {pool.repository} on {pool.machine} has no maximum of its own; it uses {maximum}. "
           + f"Set {key} with /tune.")
    return maximum


def _rescue(path: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"{path.name}-{stamp}"
    try:
        _ = git_rescue.rescue(path, name, generated=[path / "target"])
    except git_rescue.RescueError as error:
        if "status" not in error.command or "index" not in error.stderr:
            raise
        index = git_files.at(path).git_dir / "index"
        with tempfile.NamedTemporaryFile(prefix=".rescue-index-", dir=path, delete=False) as backup:
            saved_index = Path(backup.name)
        _ = index.replace(saved_index)
        try:
            descriptor = build_folders.build_folder_lock_fd(path)
            _ = _git(path, "read-tree", "HEAD", pass_fds=(descriptor,))
            _ = git_rescue.rescue(path, name, generated=[path / "target"])
        except BaseException:
            _ = saved_index.replace(index)
            raise
        saved_index.unlink()


def _exclude_slot_target(source: Path, path: Path, descriptor: int) -> None:
    """Keep build output ignored only in this slot's worktree."""
    checkout = git_files.at(path)
    if _git(source, "config", "--local", "--type=bool", "--get", "extensions.worktreeConfig",
            accepted=(0, 1), pass_fds=(descriptor,)) != "true":
        # Preserve the main checkout's location when Git enables worktree configuration.
        for key in ("core.worktree", "core.bare"):
            value = _git(source, "config", "--local", "--get", key, accepted=(0, 1), pass_fds=(descriptor,))
            if value:
                _ = _git(source, "config", "--file", str(checkout.common_dir / "config.worktree"),
                         key, value, pass_fds=(descriptor,))
                _ = _git(source, "config", "--local", "--unset", key, pass_fds=(descriptor,))
        _ = _git(source, "config", "--local", "extensions.worktreeConfig", "true", pass_fds=(descriptor,))
    exclude = checkout.git_dir / "info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    if "/target/" not in text.splitlines():
        with exclude.open("a") as output:
            _ = output.write(("\n" if text and not text.endswith("\n") else "") + "/target/\n")
    _ = _git(path, "config", "--worktree", "core.excludesFile", str(exclude), pass_fds=(descriptor,))


@contextmanager
def _try_slot(source: Path, commit: str, *, showrunner: str, unit: str, work: str,
              purpose: str, report: Callable[[str], None]) -> Generator[Slot]:
    """Take the lowest free slot and reset its source under the exclusive lock."""
    pool = _pool(source)
    maximum = _maximum(pool, report)
    checkout = build_folders.SlotCheckout(pool.source, _git(pool.source, "rev-parse", f"{commit}^{{commit}}"))
    for number in range(1, maximum + 1):
        spec = build_folders.BuildFolderSpec(kind="slot", parent=pool.root, suffix_length=0,
                                            repository=pool.repository, slot=number, showrunner=showrunner,
                                            unit=unit, work=work, purpose=purpose, slot_checkout=checkout,
                                            machine=pool.machine)
        with ExitStack() as held:
            try:
                path = held.enter_context(build_folders.slot_build_folder(spec))
            except BlockingIOError:
                continue
            if git_files.at(path).common_dir != git_files.at(pool.source).common_dir:
                raise SlotRepositoryMismatch(path, pool.source)
            descriptor = build_folders.build_folder_lock_fd(path)
            (git_files.at(path).git_dir / "index.lock").unlink(missing_ok=True)
            target = path / "target"
            if target.is_symlink():
                target.unlink()
                target.mkdir()
            _exclude_slot_target(pool.source, path, descriptor)
            _rescue(path)
            _ = _git(path, "update-index", "--really-refresh", accepted=(0, 1), pass_fds=(descriptor,))
            _ = _git(path, "checkout", "--force", "--detach", checkout.commit, pass_fds=(descriptor,))
            _ = _git(path, "clean", "-ffdx", "--exclude=/target", pass_fds=(descriptor,))
            cargo = pool.cargo_root / path.name
            settings = conf.read_or_repository("build_folders")
            key = f"slots.{pool.machine}.{pool.repository}.max_bytes"
            if key in settings.values():
                limit = settings.whole(key)
                size = _size(path) + _size(cargo)
                if size > limit:
                    if cargo.exists():
                        shutil.rmtree(cargo)
                    target = path / "target"
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    report(f"slots: {path.name} was {size / 2**30:g} GiB, over its {limit / 2**30:g} GiB limit; "
                           + "its build output is deleted once.")
            cargo.mkdir(parents=True, exist_ok=True)
            yield Slot(path, cargo, pool.repository, number,
                       MappingProxyType({"CARGO_BUILD_BUILD_DIR": str(cargo)}))
        return
    raise SlotsFull(pool.repository, pool.machine, maximum)


class SlotWaitPolicy(Enum):
    """Select queued acquisition or an immediate capacity result."""

    QUEUE = auto()
    REFUSE = auto()


def _wait_mac_slot(root: Path, repository: str, maximum: int, deadline: float) -> None:
    """Wait for any kernel lock, with a finite deadline and no polling."""
    from scripts.build_queue.slot_wait import SlotWaitExpired

    script = ("import fcntl, sys\n"
              "with open(sys.argv[1], 'rb') as lock:\n"
              " fcntl.flock(lock, fcntl.LOCK_SH)\n"
              " sys.stdout.write('free\\n')\n")
    helpers: list[subprocess.Popen[bytes]] = []
    try:
        with selectors.DefaultSelector() as ready:
            for number in range(1, maximum + 1):
                path = root / f"{repository}-slot{number}.lock"
                helper = subprocess.Popen([sys.executable, "-c", script, str(path)], stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE)
                helpers.append(helper)
                assert helper.stdout is not None
                _ = ready.register(helper.stdout, selectors.EVENT_READ)
            events = ready.select(max(0.0, deadline - time.monotonic()))
            if not events:
                raise SlotWaitExpired(f"slots: wait ended for {repository}; all {maximum} slots remain held")
            stream = cast(BinaryIO, events[0][0].fileobj)
            if stream.readline() != b"free\n":
                raise RuntimeError(f"slots: cannot read a free-slot notification for {repository}")
    finally:
        for helper in helpers:
            if helper.poll() is None:
                helper.terminate()
            _ = helper.communicate()


@contextmanager
def take_slot(source: Path, commit: str, *, showrunner: str, unit: str, work: str,
              purpose: str, wait_policy: SlotWaitPolicy = SlotWaitPolicy.QUEUE,
              report: Callable[[str], None] = print) -> Generator[Slot]:
    """Take the lowest free slot and wait through the queue at capacity."""
    from scripts.build_queue import slot_wait

    episode = str(uuid4())
    since = time.time()
    seconds = conf.read_or_repository("build_folders").number("slots.wait_seconds")
    deadline = time.monotonic() + seconds
    while True:
        with ExitStack() as held:
            try:
                slot = held.enter_context(_try_slot(source, commit, showrunner=showrunner, unit=unit,
                                                    work=work, purpose=purpose, report=report))
            except SlotsFull as error:
                if wait_policy is SlotWaitPolicy.REFUSE:
                    raise
                if sys.platform == "darwin":
                    _ = sys.stderr.write(str(error) + "; waiting for a free slot\n")
                    _wait_mac_slot(configured_slot_root(), error.repository, error.maximum, deadline)
                else:
                    slot_wait.wait_for_slot(error.repository, error.machine, configured_slot_root(), error.maximum,
                        episode=episode, since=since, showrunner=showrunner, unit=unit, work=work, purpose=purpose)
                continue
            yield slot
            return


def _size(path: Path) -> int:
    total = 0
    for directory, folders, files in os.walk(path):
        current = Path(directory)
        for name in (*folders, *files):
            total += (current / name).lstat().st_size
    return total


def sweep_slots() -> None:
    """Remove old free slots and delete oversized build output under their locks."""
    machine = host_name()
    settings = conf.read_or_repository("build_folders")
    try:
        root = configured_slot_root()
    except SlotsNotConfigured:
        return
    cargo_root = slot_cargo_root(settings)
    if not root.exists():
        return
    for path, repository, _ in slot_paths(root):
        with build_folders.collector_lease(path, wait=False) as free:
            if not free:
                continue
            settings = conf.read_or_repository("build_folders")
            record_path = lease_path(path)
            cargo = cargo_root / path.name
            if not path.exists():
                if cargo.exists():
                    shutil.rmtree(cargo)
                record_path.unlink()
                continue
            lease = cast(dict[str, object], json.loads(record_path.read_text()))
            last_use = datetime.fromisoformat(cast(str, lease["last_use"]))
            if unused(last_use, settings, datetime.now(timezone.utc)):
                try:
                    _rescue(path)
                except git_rescue.RescueError as error:
                    detail = " ".join(str(error).splitlines())
                    _ = sys.stderr.write(f"slots: keeps {path.name}: {detail}\n")
                    continue
                descriptor = build_folders.build_folder_lock_fd(path)
                _ = _git(source_checkout(path), "worktree", "remove", "--force", str(path), pass_fds=(descriptor,))
                if cargo.exists():
                    shutil.rmtree(cargo)
                record_path.unlink()
                continue
            key = f"slots.{machine}.{repository}.max_bytes"
            if key in settings.values():
                limit = settings.whole(key)
                size = _size(path) + _size(cargo)
                if size > limit and cargo.exists():
                    shutil.rmtree(cargo)
                    _ = sys.stdout.write(
                        f"slots: {path.name} was {size / 2**30:g} GiB, over its {limit / 2**30:g} GiB limit; "
                        + "its build output is deleted.\n")


def main(argv: Sequence[str]) -> int:
    import argparse
    from scripts.build_queue.slot_wait import SlotWaitExpired

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    _ = commands.add_parser("sweep")
    run = commands.add_parser("run")
    _ = run.add_argument("--source", type=Path, required=True)
    _ = run.add_argument("--commit", required=True)
    for name in ("showrunner", "unit", "work", "purpose"):
        _ = run.add_argument(f"--{name}", required=True)
    _ = run.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if cast(str, args.command) == "sweep":
        sweep_slots()
        return 0
    arguments = cast(list[str], args.argv)
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    try:
        with take_slot(cast(Path, args.source), cast(str, args.commit), showrunner=cast(str, args.showrunner),
                       unit=cast(str, args.unit), work=cast(str, args.work), purpose=cast(str, args.purpose)) as slot:
            descriptor = build_folders.build_folder_lock_fd(slot.path)
            return build_folders.run_build_command(arguments, path=slot.path,
                                                  environment={**os.environ, **slot.env, "BUILD_FOLDER": str(slot.path),
                                                               "BUILD_FOLDER_LOCK_FD": str(descriptor)},
                                                  pass_fds=(descriptor,))
    except (SlotsFull, SlotWaitExpired) as error:
        _ = sys.stderr.write(f"{error}\n")
        return 75
    except SlotsNotConfigured as error:
        _ = sys.stderr.write(f"{error}\n")
        return 2
    except (SlotGitFailure, subprocess.SubprocessError) as error:
        _ = sys.stderr.write(f"{error}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

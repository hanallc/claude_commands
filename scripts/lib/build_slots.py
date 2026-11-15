#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Take reusable repository slots and preserve source before each reset."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import MappingProxyType
from typing import cast

from scripts.buildlog.store import host_name
from scripts.copies.copies import main_checkout
from scripts.lib import build_folders, conf, git_files, git_rescue


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


@dataclass(frozen=True)
class RepositoryPool:
    source: Path
    repository: str
    machine: str
    root: Path
    cargo_root: Path


def _git(source: Path, *arguments: str, pass_fds: Sequence[int] = ()) -> str:
    return subprocess.run(["git", "-C", str(source), *arguments], check=True,
                          capture_output=True, text=True, pass_fds=pass_fds).stdout.strip()


def _root(settings: conf.ConfigRead, key: str) -> Path:
    return Path(os.path.expandvars(settings.text(key))).expanduser().resolve()


def slot_root(values: Mapping[str, str], machine: str) -> str:
    """Select the machine's slot root, with the common root as the default."""
    return values.get(f"roots.slots.{machine}", values.get("roots.slots", ""))


def _slot_root(settings: conf.ConfigRead, machine: str) -> Path:
    root = slot_root(settings.values(), machine)
    if not root:
        raise SlotsNotConfigured(machine)
    return Path(os.path.expandvars(root)).expanduser().resolve()


def _pool(source: Path) -> RepositoryPool:
    settings = conf.read_or_repository("build_folders")
    machine = host_name()
    root = _slot_root(settings, machine)
    main = main_checkout(source)
    origin = subprocess.run(["git", "-C", str(main), "remote", "get-url", "origin"],
                            capture_output=True, text=True)
    if origin.returncode not in (0, 2):
        origin.check_returncode()
    repository = (origin.stdout.strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
                  if origin.returncode == 0 else main.name)
    return RepositoryPool(main, repository, machine, root, _root(settings, "roots.slot_cargo"))


def _maximum(pool: RepositoryPool) -> int:
    settings = conf.read_or_repository("build_folders")
    key = f"slots.{pool.machine}.{pool.repository}.max"
    if key in settings.values():
        return settings.whole(key)
    maximum = settings.whole(f"slots.{pool.machine}.default_max")
    print(f"slots: {pool.repository} on {pool.machine} has no maximum of its own; it uses {maximum}. "
          + f"Set {key} with /tune.")
    return maximum


def _rescue(path: Path) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    _ = git_rescue.rescue(path, f"{path.name}-{stamp}", generated=[path / "target"])


@contextmanager
def take_slot(source: Path, commit: str, *, showrunner: str, unit: str, work: str,
              purpose: str) -> Generator[Slot]:
    """Take the lowest free slot and reset its source under the exclusive lock."""
    pool = _pool(source)
    maximum = _maximum(pool)
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
            _rescue(path)
            descriptor = build_folders.build_folder_lock_fd(path)
            _ = _git(path, "checkout", "--force", "--detach", checkout.commit, pass_fds=(descriptor,))
            _ = _git(path, "clean", "-ffdx", "--exclude=/target", pass_fds=(descriptor,))
            cargo = pool.cargo_root / path.name
            cargo.mkdir(parents=True, exist_ok=True)
            yield Slot(path, cargo, pool.repository, number,
                       MappingProxyType({"CARGO_BUILD_BUILD_DIR": str(cargo)}))
        return
    raise SlotsFull(pool.repository, pool.machine, maximum)


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
        root = _slot_root(settings, machine)
    except SlotsNotConfigured:
        return
    cargo_root = _root(settings, "roots.slot_cargo")
    if not root.exists():
        return
    paths = {entry.with_name(entry.name.removesuffix(".lease.json")) for entry in root.iterdir()
             if re.fullmatch(r".+-slot[1-9][0-9]*(?:\.lease\.json)?", entry.name)}
    for path in sorted(paths):
        match = re.fullmatch(r"(.+)-slot([1-9][0-9]*)", path.name)
        if match is None:
            continue
        with build_folders.collector_lease(path, wait=False) as free:
            if not free:
                continue
            settings = conf.read_or_repository("build_folders")
            lease_path = path.with_name(path.name + ".lease.json")
            cargo = cargo_root / path.name
            if not path.exists():
                if cargo.exists():
                    shutil.rmtree(cargo)
                lease_path.unlink()
                continue
            lease = cast(dict[str, object], json.loads(lease_path.read_text()))
            last_use = datetime.fromisoformat(cast(str, lease["last_use"]))
            cutoff = datetime.now(timezone.utc) - timedelta(days=settings.whole("slots.unused_days"))
            if last_use < cutoff:
                try:
                    _rescue(path)
                except git_rescue.RescueError as error:
                    detail = " ".join(str(error).splitlines())
                    print(f"slots: keeps {path.name}: {detail}", file=sys.stderr)
                    continue
                descriptor = build_folders.build_folder_lock_fd(path)
                _ = _git(main_checkout(path), "worktree", "remove", "--force", str(path), pass_fds=(descriptor,))
                if cargo.exists():
                    shutil.rmtree(cargo)
                lease_path.unlink()
                continue
            key = f"slots.{machine}.{match[1]}.max_bytes"
            if key in settings.values():
                limit = settings.whole(key)
                size = _size(path) + _size(cargo)
                if size > limit and cargo.exists():
                    shutil.rmtree(cargo)
                    print(f"slots: {path.name} was {size / 2**30:g} GiB, over its {limit / 2**30:g} GiB limit; "
                          + "its build output is deleted.")


def main(argv: Sequence[str]) -> int:
    import argparse

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
    except SlotsFull as error:
        print(error, file=sys.stderr)
        return 75
    except SlotsNotConfigured as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Take reusable repository slots and preserve source before each reset."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
from enum import Enum, auto
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import MappingProxyType
from typing import cast
from uuid import uuid4

from scripts.buildlog.store import host_name
from scripts.lib import build_folders, conf, copy_record, git_files, git_rescue
from scripts.lib.slot_root import SlotsNotConfigured as SlotsNotConfigured, configured_slot_root as configured_slot_root
from scripts.lib.slot_root import slot_root as slot_root


SLOT_NAME = re.compile(r"(.+)-slot([1-9][0-9]*)")
LEASE_SUFFIX = ".lease.json"


@dataclass(frozen=True)
class Slot:
    path: Path
    cargo_dir: Path
    repository: str
    number: int
    env: Mapping[str, str]


@dataclass(frozen=True)
class ExactSlotRequest:
    """Acquire only the slot number assigned to this command."""

    number: int


@dataclass(frozen=True)
class LowestFreeSlot:
    """Acquire the lowest free number within the configured pool."""


SlotSelection = ExactSlotRequest | LowestFreeSlot
LOWEST_FREE_SLOT = LowestFreeSlot()


@dataclass(frozen=True)
class SlotQueuePosition:
    """Retain the admitted run's place when another run takes the last slot."""

    episode: str
    since: float


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
                                capture_output=True, text=True,
                                pass_fds=tuple(dict.fromkeys((*pass_fds, *build_folders.held_build_folder_descriptors()))),
                                timeout=timeout)
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


def configured_maximum(settings: conf.ConfigRead, machine: str, repository: str) -> int:
    """Read the repository limit or the machine's default limit."""
    key = f"slots.{machine}.{repository}.max"
    return settings.whole(key if key in settings.values() else f"slots.{machine}.default_max")


def _pool(source: Path) -> RepositoryPool:
    settings = conf.read_or_repository("build_folders")
    machine = host_name()
    root = configured_slot_root()
    main = source_checkout(source)
    origin = subprocess.run(["git", "-C", str(main), "remote", "get-url", "origin"],
                            capture_output=True, text=True,
                            pass_fds=build_folders.held_build_folder_descriptors())
    if origin.returncode not in (0, 2):
        origin.check_returncode()
    repository = (origin.stdout.strip().rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")
                  if origin.returncode == 0 else main.name)
    return RepositoryPool(main, repository, machine, root, slot_cargo_root(settings))


def _maximum(pool: RepositoryPool) -> int:
    settings = conf.read_or_repository("build_folders")
    return configured_maximum(settings, pool.machine, pool.repository)


def _slot_repository(pool: RepositoryPool) -> Path:
    """Use the established pool repository before following copy records."""
    if pool.root.is_dir():
        for path, repository, _ in slot_paths(pool.root):
            if repository == pool.repository and path.is_dir():
                try:
                    lease = cast(dict[str, object], json.loads(lease_path(path).read_text()))
                except FileNotFoundError:
                    continue
                if lease.get("ready") is not True:
                    continue
                _ = git_files.at(path)
                return source_checkout(path)
    return _persistent_source(pool.source)


def _persistent_source(source: Path) -> Path:
    seen: set[Path] = set()
    while True:
        record_path = copy_record.record_path(source)
        # Session folders also hold task clones outside the configured scratch root.
        record = copy_record.read_record(record_path if record_path is not None else source / copy_record.RECORD)
        if isinstance(record, copy_record.NotRegistered):
            return source
        if source in seen:
            raise SlotGitFailure(f"slots: copy sources form a cycle at {source}; cannot create a persistent slot")
        seen.add(source)
        original = record.fields.get("source_path") if isinstance(record, copy_record.Registered) else None
        if not isinstance(original, str) or not original or not Path(original).is_dir():
            raise SlotGitFailure(f"slots: {source} has no available persistent source; create its pool from the original repository")
        source = source_checkout(Path(original))


@contextmanager
def _slot_commit(repository: Path, source: Path, commit: str) -> Generator[None]:
    """Retain one imported commit in the owner repository until the take ends."""
    if git_files.at(repository).common_dir == git_files.at(source).common_dir:
        yield
        return
    reference = f"refs/build-slots/take-{uuid4().hex}"
    previous = _git(repository, "rev-parse", "HEAD")
    try:
        _ = _git(repository, "fetch", "--no-tags", "--no-write-fetch-head", "--force", "--",
                 str(source), f"{commit}:{reference}")
        if not _git(repository, "merge-base", reference, previous, accepted=(0, 1)):
            raise SlotRepositoryMismatch(repository, source)
        yield
    finally:
        _ = _git(repository, "update-ref", "-d", reference)


def _rescue(path: Path, base: git_rescue.ReplaceableBase | git_rescue.PreserveBase = git_rescue.PRESERVE_BASE) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"{path.name}-{stamp}"
    try:
        _ = git_rescue.rescue(path, name, generated=[path / "target", path / build_folders.COPY_USE_STAMP],
                             pass_fds=build_folders.held_build_folder_descriptors(), base=base)
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
            _ = git_rescue.rescue(path, name, generated=[path / "target", path / build_folders.COPY_USE_STAMP],
                                 pass_fds=build_folders.held_build_folder_descriptors(), base=base)
        except BaseException:
            _ = saved_index.replace(index)
            raise
        saved_index.unlink()


def _enable_worktree_config(path: Path, common_dir: Path, descriptor: int) -> None:
    read_extension = ("config", "--local", "--type=bool", "--get", "extensions.worktreeConfig")
    if _git(path, *read_extension, accepted=(0, 1), pass_fds=(descriptor,)) == "true":
        return
    with (common_dir / "build-copy-config.lock").open("a") as lock:
        # Git retains the lock if the slot process exits during a write.
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        descriptors = (descriptor, lock.fileno())
        if _git(path, *read_extension, accepted=(0, 1), pass_fds=descriptors) == "true":
            return
        # Preserve the main checkout's location when Git enables worktree configuration.
        for key in ("core.worktree", "core.bare"):
            value = _git(path, "config", "--local", "--get", key, accepted=(0, 1), pass_fds=descriptors)
            if value:
                _ = _git(path, "config", "--file", str(common_dir / "config.worktree"),
                         key, value, pass_fds=descriptors)
                _ = _git(path, "config", "--local", "--unset", key, pass_fds=descriptors)
        _ = _git(path, "config", "--local", "extensions.worktreeConfig", "true", pass_fds=descriptors)


def _exclude_slot_target(path: Path, descriptor: int) -> None:
    """Keep build output ignored only in this slot's worktree."""
    checkout = git_files.at(path)
    _enable_worktree_config(path, checkout.common_dir, descriptor)
    exclude = checkout.git_dir / "info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    missing = [pattern for pattern in ("/target/", f"/{build_folders.COPY_USE_STAMP}")
               if pattern not in text.splitlines()]
    if missing:
        with exclude.open("a") as output:
            _ = output.write(("\n" if text and not text.endswith("\n") else "") + "\n".join(missing) + "\n")
    _ = _git(path, "config", "--worktree", "core.excludesFile", str(exclude), pass_fds=(descriptor,))


@contextmanager
def _try_slot(source: Path, commit: str, *, showrunner: str, unit: str, work: str,
              purpose: str, report: Callable[[str], None],
              selection: SlotSelection = LOWEST_FREE_SLOT) -> Generator[Slot]:
    """Acquire a selected slot and reset its source under the exclusive lock."""
    pool = _pool(source)
    maximum = _maximum(pool)
    if isinstance(selection, ExactSlotRequest):
        if type(selection.number) is not int or not 1 <= selection.number <= maximum:
            raise ValueError('The assigned slot exceeds the configured pool')
        numbers = (selection.number,)
    else:
        numbers = range(1, maximum + 1)
    repository = _slot_repository(pool)
    checkout = build_folders.SlotCheckout(repository, _git(pool.source, "rev-parse", f"{commit}^{{commit}}"))
    source_busy: build_folders.SlotSourceBusy | None = None
    with _slot_commit(repository, pool.source, checkout.commit):
        for number in numbers:
            spec = build_folders.BuildFolderSpec(kind="slot", parent=pool.root, suffix_length=0,
                                                repository=pool.repository, slot=number, showrunner=showrunner,
                                                unit=unit, work=work, purpose=purpose, slot_checkout=checkout,
                                                machine=pool.machine)
            with ExitStack() as held:
                try:
                    path = held.enter_context(build_folders.slot_build_folder(spec))
                except build_folders.SlotSourceBusy as error:
                    source_busy = error
                    continue
                except BlockingIOError:
                    continue
                descriptor = build_folders.build_folder_lock_fd(path)
                (git_files.at(path).git_dir / "index.lock").unlink(missing_ok=True)
                target = path / "target"
                if target.is_symlink():
                    target.unlink()
                    target.mkdir()
                _exclude_slot_target(path, descriptor)
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
        if source_busy is not None:
            raise source_busy
        raise SlotsFull(pool.repository, pool.machine, maximum)


@dataclass(frozen=True)
class LeasedCopy:
    """The source and Cargo paths of a held test copy."""

    path: Path
    repository: str
    number: int
    env: Mapping[str, str]
    pass_fds: tuple[int, ...]

    @property
    def cargo_dir(self) -> Path:
        return self.path / "target"


def _copy_order(parent: Path) -> Generator[int]:
    try:
        numbers = build_folders.copy_numbers(parent)
    except FileNotFoundError:
        numbers = {}
    existing = {number for number, folder in numbers.items() if folder}
    yield from sorted(existing)
    number = 1
    while True:
        if number not in existing:
            yield number
        number += 1


class CopyBasePolicy(Enum):
    """Choose whether to keep the previous source commit during rescue."""

    PRESERVE = auto()
    REPLACE = auto()


@contextmanager
def take_copy(source: Path, commit: str, *, showrunner: str, unit: str, work: str,
              purpose: str, base_policy: CopyBasePolicy = CopyBasePolicy.PRESERVE) -> Generator[LeasedCopy]:
    """Take the first free existing copy or make the first missing number."""
    repository = _persistent_source(source_checkout(source))
    name = build_folders.repo_word(repository)
    revision = _git(source, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}")
    if _git(source, "ls-tree", revision, "--", "target", build_folders.COPY_USE_STAMP):
        raise SlotGitFailure(f"{revision}: the commit tracks a reserved copy path")
    parent = build_folders.unit_folder(name, unit)
    checkout = build_folders.SlotCheckout(repository, revision)
    with _slot_commit(repository, source, revision), ExitStack() as unit_claim:
        _ = unit_claim.enter_context(build_folders.unit_allocation_lock(parent, exclusive=False))
        for number in _copy_order(parent):
            spec = build_folders.BuildFolderSpec(kind="copy", parent=build_folders.build_root(),
                suffix_length=0, repository=name, slot=number, showrunner=showrunner, unit=unit,
                work=work, purpose=purpose, slot_checkout=checkout, machine=host_name())
            with ExitStack() as held:
                try:
                    path = held.enter_context(build_folders.slot_build_folder(spec))
                except build_folders.SlotSourceBusy:
                    raise
                except (BlockingIOError, FileExistsError):
                    continue
                descriptor = build_folders.build_folder_lock_fd(path)
                record = build_folders.numbered_copy_record(path)
                if not isinstance(record, build_folders.NumberedCopySource):
                    continue
                base = (git_rescue.ReplaceableBase(record.commit) if base_policy is CopyBasePolicy.REPLACE
                        else git_rescue.PRESERVE_BASE)
                build_folders.mark_folder_use(path)
                index = record.git_directory / "index"
                index.with_name("index.lock").unlink(missing_ok=True)
                if not os.path.lexists(index):
                    _ = _git(path, "read-tree", "HEAD", pass_fds=(descriptor,))
                scan = ("ls-files", "--others", "--directory", "--no-empty-directory", "-z", "--exclude=target/")
                rescued = False
                try:
                    untracked = _git(path, *scan, pass_fds=(descriptor,))
                except SlotGitFailure as error:
                    if isinstance(error.__cause__, subprocess.TimeoutExpired):
                        raise
                    _rescue(path, base)
                    rescued = True
                    untracked = _git(path, *scan, pass_fds=(descriptor,))
                if any(name.endswith("/") for name in untracked.split("\0")):
                    print(f"kept for a manual removal list: {path} (build_slots, untracked folders)", file=sys.stderr)
                    continue
                unit_claim.close()
                target = path / "target"
                if target.is_symlink():
                    target.unlink()
                _exclude_slot_target(path, descriptor)
                if not rescued:
                    _rescue(path, base)
                _ = _git(path, "update-index", "--really-refresh", accepted=(0, 1))
                _ = _git(path, "checkout", "--force", "--detach", revision)
                _ = _git(path, "clean", "-ffx", "--exclude=/target",
                         f"--exclude=/{build_folders.COPY_USE_STAMP}")
                target.mkdir(exist_ok=True)
                build_folders.record_numbered_copy_source(path, repository, revision)
                yield LeasedCopy(path, name, number, MappingProxyType({
                    "CARGO_TARGET_DIR": str(target), "CARGO_BUILD_BUILD_DIR": str(target),
                    build_folders.NUMBERED_COPY_LOCK_ENV: str(descriptor),
                }), (descriptor,))
                return


class NumberedCopyEnvrc(Enum):
    """The numbered copy requires byte proof for its envrc."""

    OUTSIDE_COPY = auto()
    TRUSTED = auto()
    NEEDS_APPROVAL = auto()


def numbered_copy_envrc(envrc: Path, *, trusted_source: Callable[[Path], bool]) -> NumberedCopyEnvrc:
    """Compare an envrc with the recorded commit in a trusted source checkout."""
    try:
        root = build_folders.build_root()
        candidate = Path(os.path.abspath(envrc))
        physical_root = root.resolve()
        for directory, base in ((candidate.parent, root.absolute()),
                                (candidate.parent.resolve(), physical_root)):
            if not directory.is_relative_to(base):
                continue
            parts = directory.relative_to(base).parts
            if len(parts) >= 3 and parts[1] not in ("main", "ci") and re.fullmatch(r"[0-9]+", parts[2]):
                parent = root.joinpath(*parts[:3])
                break
        else:
            return NumberedCopyEnvrc.OUTSIDE_COPY
        exact = physical_root / parent.relative_to(root)
        if (candidate.name != ".envrc" or candidate.parent.resolve() != exact
                or len(parts) != 3):
            return NumberedCopyEnvrc.NEEDS_APPROVAL
        record = build_folders.numbered_copy_record(parent)
        if not isinstance(record, build_folders.NumberedCopySource) or not trusted_source(record.source):
            return NumberedCopyEnvrc.NEEDS_APPROVAL
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            identity = os.fstat(stream.fileno())
            if not stat.S_ISREG(identity.st_mode):
                return NumberedCopyEnvrc.NEEDS_APPROVAL
            command = ["git", "--git-dir", str(record.common_directory)]
            environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            timeout = build_folders.repository_git_timeout()
            entry = subprocess.run([*command, "ls-tree", record.commit, "--", ".envrc"],
                cwd=record.source, env=environment, capture_output=True, check=True, timeout=timeout)
            if not entry.stdout.startswith((b"100644 blob ", b"100755 blob ")):
                return NumberedCopyEnvrc.NEEDS_APPROVAL
            expected = subprocess.run(
                [*command, "cat-file", "blob", f"{record.commit}:.envrc"],
                cwd=record.source, env=environment, capture_output=True, check=True, timeout=timeout)
            if stream.read(len(expected.stdout) + 1) != expected.stdout:
                return NumberedCopyEnvrc.NEEDS_APPROVAL
            current = candidate.lstat()
            if (not os.path.samestat(identity, current) or identity.st_ctime_ns != current.st_ctime_ns
                    or identity.st_mtime_ns != current.st_mtime_ns
                    or record != build_folders.numbered_copy_record(parent)):
                return NumberedCopyEnvrc.NEEDS_APPROVAL
        return NumberedCopyEnvrc.TRUSTED
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return NumberedCopyEnvrc.NEEDS_APPROVAL


def _check_directory_identity(path: Path, expected: build_folders.CopyDirectoryIdentity) -> bool:
    try:
        identity = build_folders.CopyDirectoryIdentity.from_stat(path.lstat())
    except FileNotFoundError:
        return False
    if identity != expected:
        raise SlotGitFailure(f"{path}: directory identity changed during copy removal")
    return True


@contextmanager
def _numbered_copy_busy_marks(copy: build_folders.NumberedCopySource, *, dry_run: bool) -> Generator[bool]:
    """Hold the old busy marks even when the checkout is partly removed."""
    with ExitStack() as stack:
        try:
            parents = [copy.common_directory, copy.path, copy.path.parent]
            with suppress(FileNotFoundError):
                info = copy.git_directory.lstat()
                if stat.S_ISDIR(info.st_mode) and build_folders.CopyDirectoryIdentity.from_stat(info) == copy.git_identity:
                    parents.insert(1, copy.git_directory)
            for parent in parents:
                mark = parent / build_folders.BUSY
                create = not dry_run and parent in (copy.common_directory, copy.git_directory)
                with suppress(FileNotFoundError):
                    descriptor = build_folders.open_busy_mark(mark, create=create)
                    _ = stack.callback(os.close, descriptor)
                    mode = fcntl.LOCK_SH if parent == copy.common_directory else fcntl.LOCK_EX
                    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                    if not os.path.samestat(os.fstat(descriptor), mark.lstat()):
                        raise BlockingIOError(f"{mark}: the busy mark changed")
        except OSError:
            yield False
        else:
            yield True


def remove_copy_locked(path: Path, *, dry_run: bool = False) -> bool:
    """Check a numbered copy for manual review."""
    from scripts.lib import scratch

    record = build_folders.numbered_copy_record(path)
    if isinstance(record, build_folders.NoNumberedCopySource):
        return False
    if isinstance(record, build_folders.UnreadableNumberedCopySource):
        raise SlotGitFailure(record.reason)
    copy = record.copy if isinstance(record, build_folders.NumberedCopyRemovalPending) else record
    if not build_folders.may_remove_folder_tree(path, copy.git_directory):
        return False
    if not _check_directory_identity(copy.common_directory, copy.common_identity):
        return False
    _ = _check_directory_identity(path, copy.checkout_identity)
    with _numbered_copy_busy_marks(copy, dry_run=True) as idle:
        if not idle or not isinstance(scratch.borrow_lifetime(path, scratch_settings=conf.read("scratch")), scratch.Ended):
            return False
        for borrower in scratch.registered_copy_paths():
            evidence = scratch.task_of(borrower)
            if isinstance(evidence, copy_record.Registered):
                borrowed = evidence.fields.get("borrows_from")
                if isinstance(borrowed, str) and borrowed:
                    store = Path(borrowed).resolve()
                    if store.is_relative_to(copy.git_directory) or store.is_relative_to(path.resolve()):
                        return False
        if not dry_run:
            print(f"kept for a manual removal list: {path} (build_slots, numbered copy)", file=sys.stderr)
        return dry_run


def remove_copy(copy: build_folders.NumberedCopySource | build_folders.NumberedCopyRemovalPending, *,
                dry_run: bool = False) -> bool:
    """Check a free numbered copy for manual review."""
    path = copy.copy.path if isinstance(copy, build_folders.NumberedCopyRemovalPending) else copy.path
    form = build_folders.parse_form(path)
    if isinstance(form, build_folders.UnverifiedFolderPath):
        return False
    if not isinstance(form, build_folders.BuildCopy):
        raise ValueError(f"{path}: expected a numbered copy")
    with build_folders.folder_removal_lease(path, wait=False, create=False,
                                           root=build_folders.build_root()) as lease:
        if not isinstance(lease, build_folders.HeldFolderRemovalLease):
            return False
        if build_folders.numbered_copy_record(path) != copy or not remove_copy_locked(path, dry_run=dry_run):
            return False
        return True


class SlotWaitPolicy(Enum):
    """The AWS caller refuses an occupied assignment."""

    REFUSE = auto()


@contextmanager
def take_slot(source: Path, commit: str, *, showrunner: str, unit: str, work: str,
              purpose: str, wait_policy: SlotWaitPolicy = SlotWaitPolicy.REFUSE,
              report: Callable[[str], None] = print,
              selection: SlotSelection = LOWEST_FREE_SLOT) -> Generator[Slot]:
    """Take a slot for an AWS caller until AWS uses numbered copies."""
    del wait_policy
    with _try_slot(source, commit, showrunner=showrunner, unit=unit, work=work,
                   purpose=purpose, report=report, selection=selection) as slot:
        yield slot


def _size(path: Path) -> int:
    total = 0
    for directory, folders, files in os.walk(path):
        current = Path(directory)
        for name in (*folders, *files):
            total += (current / name).lstat().st_size
    return total


def main(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run a command in a numbered test copy.")
    commands = parser.add_subparsers(required=True)
    run = commands.add_parser("run")
    _ = run.add_argument("--source", type=Path, required=True)
    _ = run.add_argument("--commit", required=True)
    _ = run.add_argument("--showrunner", required=True)
    _ = run.add_argument("--unit", required=True)
    _ = run.add_argument("--work", required=True)
    _ = run.add_argument("--purpose", required=True)
    _ = run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = cast(list[str], args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        with take_copy(cast(Path, args.source), cast(str, args.commit), showrunner=cast(str, args.showrunner),
                       unit=cast(str, args.unit), work=cast(str, args.work), purpose=cast(str, args.purpose)) as copy:
            if not command:
                print(copy.path, flush=True)
                _ = sys.stdin.read(1)
                return 0
            descriptor = build_folders.build_folder_lock_fd(copy.path)
            environment = {**os.environ, **copy.env, "BUILD_FOLDER": str(copy.path),
                           "BUILD_FOLDER_LOCK_FD": str(descriptor)}
            return build_folders.run_build_command(command, path=copy.path, environment=environment,
                                                   pass_fds=build_folders.held_build_folder_descriptors())
    except (OSError, ValueError, SlotGitFailure, subprocess.SubprocessError) as error:
        print(f"copies: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

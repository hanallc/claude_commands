#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Take numbered copies and preserve source before each reset."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import MappingProxyType
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from scripts.buildlog.store import host_name
from scripts.lib import build_folder_lock, build_folders, copy_record, git_files, git_rescue


if TYPE_CHECKING:
    from scripts.lib import scratch
    from scripts.lib.scratch import BorrowerIndex


# Python writes these folders beside each imported module; they hold no work.
_TOOL_OUTPUT_FOLDERS = ("__pycache__",)


@dataclass(frozen=True)
class ExactCopyRequest:
    """Take the assigned copy number."""

    number: int


@dataclass(frozen=True)
class LowestFreeCopy:
    """Acquire the lowest free copy number."""


CopySelection = ExactCopyRequest | LowestFreeCopy
LOWEST_FREE_COPY = LowestFreeCopy()


@dataclass(frozen=True)
class CopyQueuePosition:
    """Keep the admitted run's queue place when another run takes the copy."""

    episode: str
    since: float


class CopyRepositoryMismatch(ValueError):
    """The copy belongs to a different source repository."""

    def __init__(self, path: Path, source: Path) -> None:
        self.path: Path = path
        self.source: Path = source
        super().__init__(f"copies: {path} belongs to a different repository than {source}")


class CopyGitFailure(RuntimeError):
    """A Git command for the copy failed or timed out."""


def _git(source: Path, *arguments: str, pass_fds: Sequence[int] = (), accepted: tuple[int, ...] = (0,)) -> str:
    timeout = build_folders.repository_git_timeout()
    try:
        result = subprocess.run(["git", "-C", str(source), *arguments],
                                capture_output=True, text=True,
                                pass_fds=tuple(dict.fromkeys((*pass_fds, *build_folders.held_build_folder_descriptors()))),
                                timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise CopyGitFailure(f"git {' '.join(arguments)} in {source}: timed out after {timeout:g} seconds") from error
    if result.returncode not in accepted:
        raise CopyGitFailure(f"git {' '.join(arguments)} in {source}: {result.stderr.strip()}")
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


def _persistent_source(source: Path, hold: Callable[[Path], None]) -> Path:
    seen: set[Path] = set()
    while True:
        record_path = copy_record.record_path(source)
        # Session folders also hold task clones outside the configured scratch root.
        record = copy_record.read_record(record_path if record_path is not None else source / copy_record.RECORD)
        if isinstance(record, copy_record.NotRegistered):
            return source
        if source in seen:
            raise CopyGitFailure(f"copies: copy sources form a cycle at {source}; cannot create a numbered copy")
        seen.add(source)
        original = record.fields.get("source_path") if isinstance(record, copy_record.Registered) else None
        if not isinstance(original, str) or not original or not Path(original).is_dir():
            raise CopyGitFailure(f"copies: {source} has no available persistent source; create its pool from the original repository")
        original_path = Path(original)
        hold(original_path)
        source = source_checkout(original_path)


@contextmanager
def _copy_commit(repository: Path, source: Path, commit: str) -> Generator[None]:
    """Retain one imported commit in the owner repository until the take ends."""
    if git_files.at(repository).common_dir == git_files.at(source).common_dir:
        yield
        return
    reference = f"refs/build-copies/take-{uuid4().hex}"
    previous = _git(repository, "rev-parse", "HEAD")
    try:
        _ = _git(repository, "fetch", "--no-tags", "--no-write-fetch-head", "--force", "--",
                 str(source), f"{commit}:{reference}")
        if not _git(repository, "merge-base", reference, previous, accepted=(0, 1)):
            raise CopyRepositoryMismatch(repository, source)
        yield
    finally:
        _ = _git(repository, "update-ref", "-d", reference)


def _rescue(path: Path, output: Sequence[str] = ()) -> None:
    """Save the source work, but not target, the use stamp or the `output` folders."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"{path.name}-{stamp}"
    generated = [path / "target", path / build_folders.COPY_USE_STAMP, *(path / folder for folder in output)]
    try:
        _ = git_rescue.rescue(path, name, generated=generated,
                             pass_fds=build_folders.held_build_folder_descriptors())
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
            _ = git_rescue.rescue(path, name, generated=generated,
                                 pass_fds=build_folders.held_build_folder_descriptors())
        except BaseException:
            _ = saved_index.replace(index)
            raise
        saved_index.unlink()


def _enable_worktree_config(path: Path, common_dir: Path, descriptor: int) -> None:
    read_extension = ("config", "--local", "--type=bool", "--get", "extensions.worktreeConfig")
    if _git(path, *read_extension, accepted=(0, 1), pass_fds=(descriptor,)) == "true":
        return
    lock_path = common_dir / "build-copy-config.lock"
    timeout = build_folders.repository_git_timeout()
    with lock_path.open("a") as lock:
        # Git retains the lock if the copy process exits during a write.
        if not build_folder_lock.take_lock(lock.fileno(), fcntl.LOCK_EX, build_folder_lock.ProbeTiming(timeout, 0.05)):
            raise CopyGitFailure(f"{lock_path}: timed out after {timeout:g} seconds waiting for the repository config lock")
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


def _exclude_copy_target(path: Path, descriptor: int) -> None:
    """Ignore build output in this copy."""
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


@contextmanager
def take_copy(source: Path, commit: str, *, showrunner: str, unit: str, work: str,
              purpose: str, selection: CopySelection = LOWEST_FREE_COPY) -> Generator[LeasedCopy]:
    """Take the assigned number or the first free copy."""
    with ExitStack() as sources, ExitStack() as unit_claim:
        def hold_source(path: Path) -> None:
            for descriptor in sources.enter_context(build_folders.git_source_claims(path)):
                _ = sources.enter_context(build_folders.inherit_build_lock(descriptor))

        try:
            hold_source(source)
            repository = _persistent_source(source_checkout(source), hold_source)
            if repository != source:
                hold_source(repository)
        except build_folders.BuildFolderClaimHeld as error:
            raise build_folders.CopySourceBusy(str(error)) from error
        name = build_folders.repo_word(repository)
        revision = _git(source, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}")
        if _git(source, "ls-tree", revision, "--", "target", build_folders.COPY_USE_STAMP):
            raise CopyGitFailure(f"{revision}: the commit tracks a reserved copy path")
        parent = build_folders.unit_folder(name, unit)
        checkout = build_folders.CopyCheckout(repository, revision)
        numbers = (selection.number,) if isinstance(selection, ExactCopyRequest) else _copy_order(parent)
        _ = sources.enter_context(_copy_commit(repository, source, revision))
        _ = unit_claim.enter_context(build_folders.unit_allocation_lock(parent, exclusive=False))
        for number in numbers:
            spec = build_folders.BuildFolderSpec(kind="copy", parent=build_folders.build_root(),
                suffix_length=0, repository=name, number=number, showrunner=showrunner, unit=unit,
                work=work, purpose=purpose, copy_checkout=checkout, machine=host_name())
            candidate = build_folders.build_folder_path(spec)
            with ExitStack() as held:
                try:
                    claims = held.enter_context(build_folders.build_folder_creation(candidate))
                    for claim in claims:
                        _ = held.enter_context(build_folders.inherit_build_lock(claim))
                    path = held.enter_context(build_folders.copy_build_folder(spec))
                except build_folders.CopySourceBusy:
                    raise
                except build_folders.BuildFolderClaimHeld as error:
                    if error.path != candidate and error.path != candidate.resolve():
                        raise
                    continue
                except (BlockingIOError, FileExistsError):
                    continue
                descriptor = build_folders.build_folder_lock_fd(path)
                record = build_folders.numbered_copy_record(path)
                if not isinstance(record, build_folders.NumberedCopySource):
                    continue
                build_folders.mark_folder_use(path)
                index = record.git_directory / "index"
                index.with_name("index.lock").unlink(missing_ok=True)
                if not os.path.lexists(index):
                    _ = _git(path, "read-tree", "HEAD", pass_fds=(descriptor,))
                scan = ("ls-files", "--others", "--directory", "--no-empty-directory", "-z", "--exclude=/target/",
                        *(f"--exclude={folder}/" for folder in _TOOL_OUTPUT_FOLDERS))
                rescued = False
                try:
                    untracked = _git(path, *scan, "--exclude-standard", pass_fds=(descriptor,))
                except CopyGitFailure as error:
                    if isinstance(error.__cause__, subprocess.TimeoutExpired):
                        raise
                    _rescue(path)
                    rescued = True
                    untracked = _git(path, *scan, "--exclude-standard", pass_fds=(descriptor,))
                if any(name.endswith("/") for name in untracked.split("\0")):
                    print(f"kept for a manual removal list: {path} (build_copies, untracked folders)", file=sys.stderr)
                    continue
                unit_claim.close()
                target = path / "target"
                if target.is_symlink():
                    target.unlink()
                _exclude_copy_target(path, descriptor)
                if not rescued:
                    output = _git(path, *scan, "--ignored", pass_fds=(descriptor,))
                    _rescue(path, [entry for entry in output.split("\0") if entry.endswith("/")])
                _ = _git(path, "update-index", "--really-refresh", accepted=(0, 1))
                _ = _git(path, "checkout", "--force", "--detach", revision)
                _ = _git(path, "clean", "-ffx", "--exclude=/target",
                         f"--exclude=/{build_folders.COPY_USE_STAMP}")
                target.mkdir(exist_ok=True)
                build_folders.record_numbered_copy_source(path, repository, revision)
                yield LeasedCopy(path, name, number, MappingProxyType({
                    "CARGO_TARGET_DIR": str(target), "CARGO_BUILD_BUILD_DIR": str(target),
                    build_folders.NUMBERED_COPY_LOCK_ENV: str(descriptor),
                    build_folders.COPY_KEEP_ENV: str(build_folders.copy_keep_file(path)),
                }), (descriptor, *claims))
                return
        if isinstance(selection, ExactCopyRequest):
            raise BlockingIOError(f"{parent}: assigned copy {selection.number} is unavailable")


def limit_copy_size(copy: LeasedCopy, limit: int) -> None:
    """Discard Cargo output when a held copy exceeds its size limit."""
    size = _size(copy.path)
    if size > limit:
        if not build_folders.reset_numbered_copy_cargo(copy.path):
            raise BlockingIOError(f"{copy.cargo_dir}: the build output or a child lease is held")
        copy.cargo_dir.mkdir()
        print(f"copies: {copy.path} was {size / 2**30:g} GiB, over its {limit / 2**30:g} GiB limit. "
              + "The maker deleted its Cargo output.")


class BuildFolderInUse(RuntimeError):
    """The folder is busy or the claim cannot verify that it is free."""


class BuildFolderReviewOnly(BuildFolderInUse):
    """The inventory can list this folder, but the claim cannot admit it."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: review only: {reason}")
        self.reason: str = reason


@dataclass(frozen=True)
class UnusedBuildFolder:
    path: Path
    bytes: int
    reason: str
    review_only: bool = False


def _borrower_index() -> BorrowerIndex:
    from scripts.lib import scratch

    return scratch.build_folder_borrowers()


def _disk_bytes(path: Path) -> int:
    reading = subprocess.run(["du", "-skx", str(path)], capture_output=True, text=True, check=True,
                             timeout=build_folders.repository_git_timeout())
    return int(reading.stdout.split(maxsplit=1)[0]) * 1024


def _numbered_candidate(path: Path, root: Path) -> bool:
    """Verify the path. Return True for a numbered copy and False for a temporary folder."""
    parts = path.relative_to(root).parts
    if (not parts or build_folders.excluded_build_root_child(parts[0]) or not stat.S_ISDIR(path.lstat().st_mode)
            or path.resolve(strict=True) != root.resolve() / Path(*parts)):
        raise BuildFolderInUse(f"{path}: the folder path is not verified")
    temporary = build_folders.is_temporary_build_folder
    if len(parts) <= 2 and temporary(path) and (len(parts) == 1 or not temporary(path.parent)):
        return False
    if (isinstance(build_folders.parse_resolved_form(path, root), build_folders.BuildCopy)
            and not temporary(path.parent) and not temporary(path.parent.parent)):
        return True
    raise BuildFolderInUse(f"{path}: expected a numbered copy or temporary build folder")


def _copy_source(path: Path) -> build_folders.NumberedCopySource:
    record = build_folders.numbered_copy_record(path)
    if isinstance(record, build_folders.UnreadableNumberedCopySource):
        raise BuildFolderInUse(record.reason)
    if isinstance(record, build_folders.NoNumberedCopySource):
        raise BuildFolderInUse(f"{path}: no numbered copy source record")
    copy = record.copy if isinstance(record, build_folders.NumberedCopyRemovalPending) else record
    for folder, identity in ((copy.path, copy.checkout_identity), (copy.git_directory, copy.git_identity),
                             (copy.common_directory, copy.common_identity)):
        if build_folders.CopyDirectoryIdentity.from_stat(folder.lstat()) != identity:
            raise BuildFolderInUse(f"{folder}: the directory identity changed")
    return copy


def _busy_mark_check(path: Path, copy: build_folders.NumberedCopySource | None, locks: ExitStack
                     ) -> Callable[[Path, set[str], set[str]], None]:
    held: set[Path] = set()

    def hold(folder: Path, *, shared: bool = False) -> None:
        mark = folder / build_folders.BUSY
        if mark in held:
            return
        try:
            descriptor = build_folders.open_busy_mark(mark, create=False)
        except FileNotFoundError:
            return
        _ = locks.callback(os.close, descriptor)
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BuildFolderInUse(f"{mark}: the busy mark is held") from error
        if not os.path.samestat(os.fstat(descriptor), mark.lstat()):
            raise BuildFolderInUse(f"{mark}: the busy mark changed")
        held.add(mark)

    if copy is not None:
        hold(copy.common_directory, shared=True)
        hold(copy.git_directory)
    hold(path.parent)

    def inspect(folder: Path, folders: set[str], files: set[str]) -> None:
        if copy is not None and (".git" in folders or ".git" in files):
            checkout = git_files.at(folder)
            if checkout.common_dir != checkout.git_dir:
                hold(checkout.common_dir, shared=not checkout.common_dir.is_relative_to(path))
            hold(checkout.git_dir)
        elif copy is not None and git_files.is_git_directory(folders, files):
            common = git_files.objects_in_git_directory(folder).parent
            if common != folder:
                hold(common, shared=not common.is_relative_to(path))
            hold(folder)
        if build_folders.BUSY in folders or build_folders.BUSY in files:
            hold(folder)

    return inspect


def _require_ended(state: scratch.Live | scratch.Ended | scratch.Unknown) -> None:
    from scripts.lib import scratch

    if isinstance(state, scratch.Live):
        raise BuildFolderInUse(state.why)
    if isinstance(state, scratch.Unknown):
        raise BuildFolderInUse(state.reason)


def _check_unused_folder(path: Path, root: Path, *, numbered: bool, snapshot: BorrowerIndex | None) -> None:
    if not numbered and path.parent != root and build_folders.is_temporary_build_folder(path.parent):
        raise BuildFolderInUse(f"{path.parent}: the parent has a temporary folder lease")
    with ExitStack() as probes:
        lease = probes.enter_context(build_folders.folder_removal_lease(path, root=root, wait=False))
        if isinstance(lease, build_folders.RemovalLockHeld):
            raise BuildFolderInUse(f"{lease.lock}: a lease lock is active")
        if (not isinstance(lease, build_folders.HeldFolderRemovalLease)
                or build_folders.lease_path(path).name not in lease.sidecar_identities()):
            raise BuildFolderInUse(f"{path}: the lease cannot be verified")
        owner = lease.owner_state(allow_released=numbered)
        if owner is not build_folders.LeaseOwnerState.ENDED:
            raise BuildFolderInUse(f"{path}: the lease owner is {owner.value}")

        copy = _copy_source(path) if numbered else None
        roots = (path, copy.git_directory) if copy is not None else (path,)
        if not build_folders.may_remove_build_path(path) or not build_folders.may_remove_folder_tree(*roots):
            raise BuildFolderInUse(f"{path}: the folder or a mount is protected")
        inspect = _busy_mark_check(path, copy, probes)
        child = build_folders.hold_child_leases(path, probes, wait=False, check_owners=True,
                                               review_temporary=not numbered, inspect=inspect)
        if isinstance(child, build_folders.RemovalLockHeld):
            raise BuildFolderInUse(f"{child.lock}: a lease lock or owner is active")
        if child is not None:
            raise BuildFolderInUse(f"{path}: a child lease cannot be verified")
        borrowers = _borrower_index() if snapshot is None else snapshot
        _require_ended(borrowers.lifetime(path, object_roots=roots))


@contextmanager
def _claim_unused_folder(path: Path, root: Path, *, snapshot: BorrowerIndex | None = None) -> Generator[str]:
    with ExitStack() as claim:
        try:
            numbered = _numbered_candidate(path, root)
            _ = claim.enter_context(build_folders.build_folder_claim_lock(
                path, root, exclusive=True, create=snapshot is None))
            if numbered and os.path.lexists(build_folders.copy_keep_file(path)):
                raise BuildFolderInUse(f"{path}: the numbered copy has a keep file")
            _check_unused_folder(path, root, numbered=numbered, snapshot=snapshot)
        except BuildFolderInUse:
            raise
        except build_folders.TemporaryFolderReview as error:
            raise BuildFolderReviewOnly(path, str(error)) from error
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            raise BuildFolderInUse(f"{path}: {error}") from error
        yield "free numbered copy" if numbered else "ownerless temporary build folder"


@contextmanager
def claim_unused_build_folder(path: Path) -> Generator[None]:
    """Hold the claim while the caller removes a verified unused build folder."""
    with _claim_unused_folder(path, build_folders.build_root()):
        yield


def _inventory_entry(path: Path, root: Path, borrowers: BorrowerIndex) -> UnusedBuildFolder | None:
    try:
        review_only = False
        try:
            with _claim_unused_folder(path, root, snapshot=borrowers) as reason:
                pass
        except BuildFolderReviewOnly as review:
            reason = review.reason
            review_only = True
        return UnusedBuildFolder(path, _disk_bytes(path), reason, review_only=review_only)
    except (BuildFolderInUse, OSError, ValueError, subprocess.SubprocessError):
        return None


def _current_unit_names() -> set[str] | None:
    from scripts.production import staff

    try:
        return {row.name for row in staff.local_snapshot().rows
                if not isinstance(row.worktree, staff.RemovedWorktree)}
    except staff.StaffUnreadable:
        return None


def _unused_unit_entry(unit: Path, root: Path, copies: set[Path]) -> UnusedBuildFolder | None:
    try:
        with build_folders.unit_allocation_lock(unit, root=root, exclusive=True, create=False,
                                                require_existing=not copies):
            if not build_folders.may_remove_build_path(unit) or not build_folders.may_remove_folder_tree(unit):
                return None
            directory = unit.lstat()
            if not stat.S_ISDIR(directory.st_mode) or unit.resolve(strict=True) != root.resolve() / unit.relative_to(root):
                return None
            size = directory.st_blocks * 512
            for entry in unit.iterdir():
                inode = entry.lstat()
                if entry in copies and stat.S_ISDIR(inode.st_mode):
                    continue
                name = build_folders.metadata_folder_name(entry.name)
                if not stat.S_ISREG(inode.st_mode) or name == entry.name or entry.with_name(name) not in copies:
                    return None
                size += inode.st_blocks * 512
            return UnusedBuildFolder(unit, size, "gone unit with free numbered copies")
    except (OSError, ValueError, RuntimeError):
        return None


def _temporary_folders(root: Path) -> Iterator[Path]:
    with suppress(OSError), os.scandir(root) as repositories:
        for repo in repositories:
            if build_folders.excluded_build_root_child(repo.name) or not repo.is_dir(follow_symlinks=False):
                continue
            path = Path(repo.path)
            if build_folders.is_temporary_build_folder(path):
                yield path
                continue
            with suppress(OSError), os.scandir(path) as children:
                for child in children:
                    folder = Path(child.path)
                    if child.is_dir(follow_symlinks=False) and build_folders.is_temporary_build_folder(folder):
                        yield folder


def unused_build_folders(root: Path) -> list[UnusedBuildFolder]:
    """List unused numbered copies, gone units, and ownerless temporary folders."""
    borrowers = _borrower_index()
    current = _current_unit_names()
    found: list[UnusedBuildFolder] = []
    units = sorted(path for path in build_folders.copy_unit_folders(
        root, exclude_root_child=build_folders.excluded_build_root_child) if isinstance(path, Path))
    for unit in units:
        if not isinstance(build_folders.parse_resolved_form(unit / "1", root), build_folders.BuildCopy):
            continue
        try:
            numbers = build_folders.copy_numbers(unit)
        except OSError:
            continue
        free = [entry for number, exists in sorted(numbers.items())
                if exists and (entry := _inventory_entry(unit / str(number), root, borrowers)) is not None]
        found.extend(free)
        if current is not None and unit.name not in current and len(free) == len(numbers):
            entry = _unused_unit_entry(unit, root, {copy.path for copy in free})
            if entry is not None:
                found.append(entry)
    for path in sorted(_temporary_folders(root)):
        entry = _inventory_entry(path, root, borrowers)
        if entry is not None:
            found.append(entry)
    return found


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
    except (OSError, ValueError, CopyGitFailure, subprocess.SubprocessError) as error:
        print(f"copies: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

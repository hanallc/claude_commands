#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Name build folders and preserve the caller's use of temporary folders."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
import json
import os
import errno
import re
import secrets
import shutil
import signal
import stat
import subprocess
from enum import Enum
from pathlib import Path
import sys
import tempfile
from threading import Lock
import time
from typing import TYPE_CHECKING, Literal, cast, final, override

from urllib.parse import urlsplit

from scripts.lib import build_folder_lock, git_files, process_start
from scripts.lib.boot_identity import boot_identity
from scripts.lib.conf import UseConfiguredValue

if TYPE_CHECKING:
    from scripts.lib.conf import ConfigRead

# Python's mkdtemp allocator uses this suffix.
MKDTEMP_SUFFIX_PATTERN = r"[a-z0-9_]{8}"
# Read old shell mktemp folders until 2026-12-03.
LEGACY_MKTEMP_SUFFIX_PATTERN = r"[A-Za-z0-9]{6}"
BUSY = ".claude-busy"
REMOVAL_RECORD_PREFIX = ".scratch-removing-"
REMOVAL_RECORD_SUFFIX = ".json"


def removal_mark(path: Path) -> Path:
    """Locate the deletion intent beside the source."""
    return path.parent / f"{REMOVAL_RECORD_PREFIX}{path.name}{REMOVAL_RECORD_SUFFIX}"


def open_busy_mark(mark: Path, *, create: bool, exclusive: bool = False) -> int:
    """Return a descriptor for a regular busy mark. The caller closes it."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | (os.O_EXCL if exclusive else 0)
    descriptor = os.open(mark, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "busy mark must be a regular file", str(mark))
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def require_no_removal_mark(path: Path) -> None:
    """Refuse to use a source with a deletion intent on it or an ancestor."""
    for folder in (path, *path.parents):
        try:
            _ = removal_mark(folder).lstat()
        except FileNotFoundError:
            continue
        raise BlockingIOError(errno.EWOULDBLOCK, "source removal has already started", str(folder))


@contextmanager
def _linked_source_locks(source: Path) -> Generator[tuple[int, ...]]:
    """Hold the checkout and its shared Git folder while Git creates a link."""
    source = source.expanduser().resolve()
    require_no_removal_mark(source)
    try:
        checkout = git_files.locate(source)
    except git_files.NotACheckout:
        yield ()
        return
    with ExitStack() as stack:
        descriptors: list[int] = []
        for folder in dict.fromkeys((checkout.common_dir, checkout.git_dir)):
            mark = folder / BUSY
            descriptor = open_busy_mark(mark, create=True)
            _ = stack.callback(os.close, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            descriptors.append(descriptor)
            require_no_removal_mark(folder)
            locked, present = os.fstat(descriptor), mark.lstat()
            if (locked.st_dev, locked.st_ino) != (present.st_dev, present.st_ino):
                raise BlockingIOError(errno.EWOULDBLOCK, "source busy mark changed", str(mark))
        require_no_removal_mark(source)
        yield tuple(descriptors)


class FolderPrefixes(Mapping[str, str]):
    """Read legacy caller prefixes from the current configuration."""

    @override
    def __getitem__(self, key: str) -> str:
        return _settings().text(f"prefixes.{key}")

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(key.removeprefix("prefixes.") for key in _settings().values() if key.startswith("prefixes."))

    @override
    def __len__(self) -> int:
        return len([key for key in _settings().values() if key.startswith("prefixes.")])


FOLDER_PREFIXES: Mapping[str, str] = FolderPrefixes()


@dataclass(frozen=True)
class InheritedTemporaryParent:
    """Use the caller's temporary parent."""


@dataclass(frozen=True)
class ExplicitBuildParent:
    """The path specifies the parent for a build folder."""
    path: Path


@dataclass(frozen=True)
class SystemTemporaryParent:
    """Use the short system temporary parent."""


@dataclass(frozen=True)
class UnitRunParent:
    """Use the director's session run root, else the unit root (unit_runs.new_run_root)."""


BuildFolderParent = InheritedTemporaryParent | ExplicitBuildParent | SystemTemporaryParent | UnitRunParent


@dataclass(frozen=True)
class UniqueBuildFolder:
    """The request holds the prefix and parent for a unique build folder."""
    prefix: str
    parent: BuildFolderParent = InheritedTemporaryParent()


@dataclass(frozen=True)
class StableOwnerFolder:
    """The request holds the name, parent, and mode for an owner folder."""
    name: str
    parent: BuildFolderParent
    mode: int = 0o777


@dataclass(frozen=True)
class NewWorktreeBranch:
    """Create the named branch from its start point."""
    name: str
    start_point: str


@dataclass(frozen=True)
class ExistingWorktreeBranch:
    """Use an existing branch without resetting it."""
    name: str


@dataclass(frozen=True)
class LinkedEditableRepository:
    """Create a persistent linked worktree at the exact caller path."""
    path: Path
    source: Path
    branch: NewWorktreeBranch | ExistingWorktreeBranch
    allow_empty: bool = False


@dataclass(frozen=True)
class RemoteEditableRepository:
    """Clone a remote ref at the exact caller path with the requested depth."""
    path: Path
    url: str
    ref: str
    depth: int


EditableRepositoryRequest = LinkedEditableRepository | RemoteEditableRepository
BuildFolderRequest = UniqueBuildFolder | StableOwnerFolder | EditableRepositoryRequest


def inherited_temporary_parent() -> Path:
    """Read the caller's current temporary parent."""
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def tmpfs_temporary_parent() -> Path:
    """Keep nested work under the caller's tmpfs folder."""
    root = Path(_settings().text("roots.gate"))
    inherited = inherited_temporary_parent().resolve()
    return inherited if inherited.is_relative_to(root) else root


def _parent(parent: BuildFolderParent) -> Path:
    if isinstance(parent, InheritedTemporaryParent):
        return inherited_temporary_parent()
    if isinstance(parent, SystemTemporaryParent):
        return Path(_settings().text("roots.system"))
    if isinstance(parent, UnitRunParent):
        from scripts.lib import unit_runs

        return unit_runs.new_run_root()
    return parent.path


def _check_name(name: str) -> None:
    if name in (".", "..") or "/" in name or "\x00" in name:
        raise ValueError("a build folder name must be one path component")


BuildFolderKind = Literal["temporary", "gate", "standing", "editable", "mirror", "target", "shell-cache",
                          "scratch", "legacy-unique", "legacy-stable", "tool", "slot", "copy"]


@dataclass(frozen=True)
class SlotCheckout:
    """The checkout holds the source repository and required commit for a slot."""
    source: Path
    commit: str


class SlotSourceBusy(BlockingIOError):
    """The source lock prevents creation of a slot checkout."""


@dataclass(frozen=True)
class NoSlotCheckout:
    """The folder does not contain a repository slot."""


@dataclass(frozen=True, kw_only=True)
class BuildFolderSpec:
    """The specification holds the folder kind, parent, name parts, and checkout source."""
    kind: BuildFolderKind
    parent: Path
    suffix_length: int
    repository: str = ""
    owner: str = ""
    purpose: str = ""
    slot: int = 0
    suffix: str = ""
    showrunner: str = ""
    unit: str = ""
    work: str = ""
    # The process the lease names; 0 names the allocating process.
    owner_pid: int = 0
    slot_checkout: SlotCheckout | NoSlotCheckout = NoSlotCheckout()
    machine: str = ""


def _settings() -> ConfigRead:
    from scripts.lib import conf

    return conf.read_or_repository("build_folders")


class InvalidFolderForm(ValueError):
    """A value cannot identify a folder in the build layout."""


@final
@dataclass(frozen=True)
class WorktreeOutput:
    """The build output of a named worktree."""

    repo: str
    unit: str
    short_name: str


@final
@dataclass(frozen=True)
class BuildCopy:
    """A numbered test checkout and its build output."""

    repo: str
    unit: str
    number: int


@final
@dataclass(frozen=True)
class MainOutput:
    """The build output of a main checkout."""

    repo: str


@final
@dataclass(frozen=True)
class CiOutput:
    """The folder of a numbered CI runner."""

    repo: str
    number: int


FolderForm = WorktreeOutput | BuildCopy | MainOutput | CiOutput


@final
@dataclass(frozen=True)
class OutsideFolderLayout:
    """The resolved path is outside the build root."""


@final
@dataclass(frozen=True)
class InvalidFolderComponents:
    """The path has no valid form in the build layout."""

    reason: str


@final
@dataclass(frozen=True)
class UnverifiedFolderPath:
    """The operating system could not verify the path."""

    error: OSError


ParsedFolder = FolderForm | OutsideFolderLayout | InvalidFolderComponents | UnverifiedFolderPath
_DECIMAL_NAME = re.compile(r"[0-9]+")
_METADATA = re.compile(r"(.+)\.(?:lock|lease\.json(?:\.[0-9a-f]{16}\.tmp)?)")
_UNIT_LOCKS = ".unit-locks"
_unit_lock_claims: set[tuple[int, Path]] = set()
_unit_lock_claims_guard = Lock()


def metadata_folder_name(name: str) -> str:
    """Return the folder name for a sidecar, or the unchanged name."""
    match = _METADATA.fullmatch(name)
    return match[1] if match else name


def _form_name(name: str) -> str:
    if not name:
        raise InvalidFolderForm("a build folder name must not be empty")
    if metadata_folder_name(name) != name or name == _UNIT_LOCKS:
        raise InvalidFolderForm(f"{name!r} is reserved for build folder metadata")
    try:
        _check_name(name)
    except ValueError as error:
        raise InvalidFolderForm(str(error)) from error
    return name


def _unit_name(unit: str) -> str:
    if unit in ("main", "ci"):
        raise InvalidFolderForm(f"{unit!r} is a reserved unit name")
    return _form_name(unit)


def _short_name(name: str) -> str:
    if _DECIMAL_NAME.fullmatch(name):
        raise InvalidFolderForm("a short name must contain a character other than a digit")
    return _form_name(name)


def _folder_number(number: int) -> str:
    if isinstance(number, bool) or number < 1:
        raise InvalidFolderForm("a folder number must be a positive integer")
    try:
        return f"{number:d}"
    except ValueError as error:
        raise InvalidFolderForm("the folder number is too large") from error


def _parse_number(name: str) -> int:
    if not _DECIMAL_NAME.fullmatch(name) or name.startswith("0"):
        raise InvalidFolderForm("a folder number must use decimal digits without a leading zero")
    return int(name)


def build_root(*, settings: ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Path:
    """Return the configured root for the local build folders."""
    if isinstance(settings, UseConfiguredValue):
        settings = _settings()
    return Path(settings.text("roots.build")).expanduser()


def repo_word(repository: Path) -> str:
    """Read the repository name from origin and apply its configured alias."""
    try:
        origin = subprocess.run(
            ["git", "-C", str(repository), "remote", "get-url", "origin"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
            timeout=repository_git_timeout(),
        ).stdout.rstrip("\n")
        if "://" in origin:
            location = urlsplit(origin).path
        else:
            location = re.sub(r"^(?:[^/:]+@)?(?:\[[^\]]+\]|[^/:]+):", "", origin, count=1)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise InvalidFolderForm(f"{repository}: cannot read the origin URL") from error
    word = _form_name(location.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git"))
    key = f"repo_word.{word}"
    return _form_name(_settings().values(key).get(key, word))


def worktree_path(repo: str, unit: str, short_name: str) -> Path:
    """Return the source path for a named worktree."""
    root = Path(_settings().text("roots.workspace")).expanduser()
    return root / _form_name(repo) / _unit_name(unit) / _short_name(short_name)


def output_path(repo: str, unit: str, short_name: str) -> Path:
    """Return the build output path for a named worktree."""
    return unit_folder(repo, unit) / _short_name(short_name)


def main_output_path(repo: str) -> Path:
    """Return the build output path for the main checkout."""
    return build_root() / _form_name(repo) / "main"


def ci_path(repo: str, number: int) -> Path:
    """Return the path for a numbered CI runner."""
    return build_root() / _form_name(repo) / "ci" / _folder_number(number)


def copy_path(repo: str, unit: str, number: int) -> Path:
    """Return the path for a numbered test copy."""
    return unit_folder(repo, unit) / _folder_number(number)


def unit_folder(repo: str, unit: str, *, root: Path | UseConfiguredValue = UseConfiguredValue.CURRENT) -> Path:
    """Return the parent of the unit's build outputs and copies."""
    base = root if isinstance(root, Path) else build_root()
    return base / _form_name(repo) / _unit_name(unit)


@contextmanager
def unit_allocation_lock(folder: Path, *, exclusive: bool) -> Generator[None]:
    """Reserve the unit name after its output folder moves or is removed."""
    root = build_root()
    lock = root / _UNIT_LOCKS / folder.relative_to(root)
    claim = (os.getpid(), root.resolve() / lock.relative_to(root))
    blocked = f"{folder}: the unit allocation lock is held. Rerun when idle"
    with _unit_lock_claims_guard:
        if claim in _unit_lock_claims:
            raise BlockingIOError(blocked)
        _unit_lock_claims.add(claim)
    try:
        with build_folder_lock.folder_parent(lock, root, create=True) as parent:
            descriptor = build_folder_lock.open_lock(lock, parent, create=True)
            try:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                try:
                    fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise BlockingIOError(blocked) from error
                if not build_folder_lock.lock_current(lock, parent, descriptor):
                    raise ValueError(f"{folder}: the unit lock changed. Rerun when idle")
                yield
            finally:
                os.close(descriptor)
    finally:
        with _unit_lock_claims_guard:
            _unit_lock_claims.remove(claim)


def _resolve_form_path(path: Path) -> Path:
    """Resolve links and permit paths for folders that do not exist yet."""
    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        return path.resolve()


def parse_form(path: Path) -> ParsedFolder:
    """Read the form or identify why the path has no verified form."""
    if metadata_folder_name(path.name) != path.name:
        return InvalidFolderComponents("the path names build folder metadata")
    root = build_root()
    try:
        resolved = _resolve_form_path(path)
        base = _resolve_form_path(root)
    except OSError as error:
        return UnverifiedFolderPath(error)
    except RuntimeError as error:
        return UnverifiedFolderPath(OSError(errno.ELOOP, str(error), str(path)))
    return parse_resolved_form(resolved, base)


def parse_resolved_form(path: Path, build_root: Path) -> ParsedFolder:
    """Read the folder form from a resolved path and build root."""
    if not path.is_relative_to(build_root):
        return OutsideFolderLayout()
    try:
        match path.relative_to(build_root).parts:
            case (repo, "main"):
                return MainOutput(_form_name(repo))
            case (repo, "ci", number):
                return CiOutput(_form_name(repo), _parse_number(number))
            case (repo, unit, name):
                _ = _form_name(repo)
                _ = _unit_name(unit)
                if _DECIMAL_NAME.fullmatch(name):
                    return BuildCopy(repo, unit, _parse_number(name))
                return WorktreeOutput(repo, unit, _short_name(name))
            case _:
                return InvalidFolderComponents("the path has the wrong number of components")
    except ValueError as error:
        return InvalidFolderComponents(str(error))


def repository_git_timeout() -> float:
    """Keep the copies Git deadline when the maker creates a slot."""
    from scripts.lib import conf

    path = os.environ.get("COPIES_CONFIG")
    settings = conf.read(Path(path).expanduser()) if path else conf.read_or_repository("copies")
    return settings.number("git_timeout_seconds")


def repository_key_for(name: str, identity: str) -> str:
    """Append the configured identity hash to a readable repository name."""
    from hashlib import sha256

    return f"{name}-{sha256(os.fsencode(identity)).hexdigest()[:_settings().whole('names.repository_key_length')]}"


def repository_key(repository: Path) -> str:
    """Distinguish repositories with equal basenames using their canonical paths."""
    canonical = repository.resolve()
    return repository_key_for(canonical.name, str(canonical))


def _new_suffix(length: int, settings: ConfigRead) -> str:
    if length <= 0:
        raise ValueError("a unique build folder needs a suffix")
    characters = settings.text("names.suffix_characters")
    if not characters or any(not (char.isascii() and (char.isalnum() or char == "_")) for char in characters):
        raise ValueError("suffix characters must be ASCII letters, digits, or underscores")
    return "".join(secrets.choice(characters) for _ in range(length))


def build_folder_path(spec: BuildFolderSpec) -> Path:
    """Apply the configured name rule without creating a folder."""
    settings = _settings()
    if spec.kind == "copy":
        return copy_path(spec.repository, spec.unit, spec.slot)
    if spec.kind == "slot":
        _check_name(spec.repository)
        return spec.parent / f"{spec.repository}-slot{spec.slot}"
    for component in (spec.repository, spec.owner, spec.purpose, spec.suffix, spec.showrunner, spec.unit, spec.work):
        _check_name(component)
    if spec.suffix_length < 0:
        raise ValueError("a suffix length cannot be negative")
    suffix = spec.suffix
    named = bool(spec.showrunner or spec.unit or spec.work)
    if named and not (spec.showrunner and spec.unit and spec.purpose):
        raise ValueError("a named build folder needs showrunner, unit, and purpose")
    if not suffix and ((named and not spec.work) or
                       (not named and spec.kind in ("temporary", "gate", "scratch", "legacy-unique"))):
        suffix = _new_suffix(spec.suffix_length, settings)
    if named:
        pattern = "{showrunner}-{unit}/{work}-{purpose}"
    elif spec.kind == "editable":
        pattern = settings.text("patterns.editable")
    elif spec.kind == "mirror":
        pattern = "{repository}/{owner}-s{slot}"
    else:
        pattern = settings.text(f"patterns.{spec.kind}")
    for field, value in (("repository", spec.repository), ("owner", spec.owner), ("purpose", spec.purpose)):
        if "{" + field + "}" in pattern and not value and spec.kind != "legacy-unique":
            raise ValueError(f"{spec.kind} needs {field}")
    relative = pattern.format(
        repository=spec.repository, owner=spec.owner, purpose=spec.purpose, suffix=suffix, slot=spec.slot,
        showrunner=spec.showrunner, unit=spec.unit, work=spec.work or suffix)
    if not relative or Path(relative).is_absolute() or any(part in (".", "..") for part in relative.split("/")):
        raise ValueError("a build folder pattern must stay below its parent")
    path = spec.parent / relative
    if not path.is_relative_to(spec.parent) or path == spec.parent:
        raise ValueError("a build folder must stay below its parent")
    return path


_locks: dict[int, Path] = {}

def _probe_timing(*, wait: bool = True, settings: ConfigRead | None = None) -> build_folder_lock.ProbeTiming:
    settings = _settings() if settings is None else settings
    return build_folder_lock.ProbeTiming(settings.number("locks.probe_wait_seconds") if wait else 0.0,
                                        settings.number("locks.probe_retry_seconds"))


def _take_lock(descriptor: int, mode: int, *, wait: bool = True) -> bool:
    return build_folder_lock.take_lock(descriptor, mode, _probe_timing(wait=wait))


def build_folder_state(path: Path) -> Literal["held", "free"]:
    """Check the lock with the host configuration."""
    return build_folder_lock.probe(path, path.parent, _probe_timing)


def build_folder_lock_fd(path: Path) -> int:
    """Return the held descriptor for subprocess pass_fds."""
    return next(descriptor for descriptor, candidate in reversed(_locks.items()) if candidate == path)


def held_build_folder_descriptors() -> tuple[int, ...]:
    """Retain all maker leases in source and command subprocesses."""
    return tuple(_locks)


def _release_build_folder(path: Path, *, descriptor: int | None = None) -> None:
    if descriptor is None:
        try:
            descriptor = build_folder_lock_fd(path)
        except StopIteration:
            return
    if _locks.get(descriptor) != path:
        return
    try:
        _ = _locks.pop(descriptor, None)
    finally:
        # Closing preserves a child's inherited hold on the same lock.
        os.close(descriptor)


COPY_USE_STAMP = ".lint-sweep-used"


def _mac_mount_points() -> tuple[Path, ...]:
    import ctypes

    @final
    class _MacMount(ctypes.Structure):
        """The 64-bit macOS statfs structure."""

        _fields_ = [
            ("block_size", ctypes.c_uint32),
            ("io_size", ctypes.c_int32),
            ("blocks", ctypes.c_uint64),
            ("blocks_free", ctypes.c_uint64),
            ("blocks_available", ctypes.c_uint64),
            ("files", ctypes.c_uint64),
            ("files_free", ctypes.c_uint64),
            ("filesystem_id", ctypes.c_int32 * 2),
            ("owner", ctypes.c_uint32),
            ("filesystem_type", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("subtype", ctypes.c_uint32),
            ("type_name", ctypes.c_char * 16),
            ("mount_point", ctypes.c_char * 1024),
            ("source", ctypes.c_char * 1024),
            ("reserved", ctypes.c_uint32 * 8),
        ]

    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    symbol = "getmntinfo_r_np$INODE64" if os.uname().machine == "x86_64" else "getmntinfo_r_np"
    try:
        read = system[symbol]
    except AttributeError as error:
        raise OSError("the macOS mount table reader is unavailable") from error
    mounts = ctypes.POINTER(_MacMount)()
    read.argtypes = [ctypes.POINTER(type(mounts)), ctypes.c_int]
    read.restype = ctypes.c_int
    count = cast(int, read(ctypes.byref(mounts), 2))  # MNT_NOWAIT
    if count <= 0:
        raise OSError(ctypes.get_errno(), "cannot read the macOS mount table")
    free = system["free"]
    free.argtypes = [ctypes.c_void_p]
    free.restype = None
    try:
        return tuple(Path(os.fsdecode(cast(bytes, mounts[index].mount_point))) for index in range(count))
    finally:
        _ = cast(None, free(mounts))


@dataclass(frozen=True)
class LinuxMount:
    mount_id: int
    device: bytes
    root: Path
    point: Path


def _decode_mount_path(value: bytes) -> Path:
    return Path(os.fsdecode(re.sub(rb"\\([0-7]{3})", lambda match: bytes([int(match[1], 8)]), value)))


def linux_mounts() -> tuple[LinuxMount, ...]:
    mounts: list[LinuxMount] = []
    for line in Path("/proc/self/mountinfo").read_bytes().splitlines():
        fields = line.split()
        if len(fields) < 10 or b"-" not in fields[6:]:
            raise ValueError("invalid Linux mount table entry")
        root, point = map(_decode_mount_path, fields[3:5])
        if not root.is_absolute() or not point.is_absolute():
            raise ValueError("invalid Linux mount path")
        mounts.append(LinuxMount(int(fields[0]), fields[2], root, point))
    return tuple(mounts)


def _linux_mount_points() -> tuple[Path, ...]:
    return tuple(mount.point for mount in linux_mounts())


def may_remove_build_path(path: Path) -> bool:
    """Keep the build root and repository folders owned by another user."""
    physical = path.resolve()
    root = build_root().resolve()
    if (root.is_relative_to(physical) or physical == Path("/build")
            or physical.is_relative_to(root / _UNIT_LOCKS)):
        return False
    if physical.parent in (root, Path("/build")):
        try:
            return physical.stat().st_uid == os.geteuid()
        except FileNotFoundError:
            return False
    return True


def may_remove_folder_tree(*paths: Path) -> bool:
    """Check that the mount table has no mount at or below a removal path."""
    try:
        if sys.platform == "darwin":
            mounts = _mac_mount_points()
        elif sys.platform == "linux":
            mounts = _linux_mount_points()
        else:
            return False
        if Path("/") not in mounts or any(not point.is_absolute() for point in mounts):
            return False
        for path in paths:
            physical = path.resolve()
            if path.is_mount() or any(point.is_relative_to(physical) for point in mounts):
                return False
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def mark_folder_use(path: Path) -> None:
    """Update the use stamp while the caller holds the folder lock."""
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(COPY_USE_STAMP, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"{path}: invalid use stamp")
            os.utime(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


def lease_path(path: Path) -> Path:
    """Return the lease path beside the copy."""
    return path.with_name(path.name + ".lease.json")


def _read_copy_lease(path: Path, directory: int) -> dict[str, object]:
    sidecar = lease_path(path)
    descriptor = os.open(sidecar.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, encoding="utf-8") as record:
        info = os.fstat(record.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise ValueError(f"{sidecar}: invalid lease file")
        fields = cast(object, json.load(record))
    if not isinstance(fields, dict):
        raise ValueError(f"{sidecar}: invalid lease record")
    return cast(dict[str, object], fields)


def folder_lease_fields(path: Path) -> dict[str, object]:
    """Read lease fields for a status display."""
    with build_folder_lock.folder_parent(path, build_root(), create=False) as directory:
        return _read_copy_lease(path, directory)


@dataclass(frozen=True)
class CopyDirectoryIdentity:
    """The device and inode of a copy directory or its Git directory."""

    device: int
    inode: int

    @classmethod
    def from_stat(cls, status: os.stat_result) -> CopyDirectoryIdentity:
        if not stat.S_ISDIR(status.st_mode):
            raise ValueError("expected a directory")
        return cls(status.st_dev, status.st_ino)


@dataclass(frozen=True)
class NumberedCopySource:
    """The source commit and directory identities for one numbered copy."""

    path: Path
    source: Path
    commit: str
    checkout_identity: CopyDirectoryIdentity
    git_directory: Path
    git_identity: CopyDirectoryIdentity
    common_directory: Path
    common_identity: CopyDirectoryIdentity


@dataclass(frozen=True)
class NumberedCopyRemovalPending:
    """The copy owner has started deleting this exact copy."""

    copy: NumberedCopySource


@dataclass(frozen=True)
class NoNumberedCopySource:
    """There is no numbered copy source record at this path."""


@dataclass(frozen=True)
class UnreadableNumberedCopySource:
    """The copy source record cannot establish the checkout's identity."""

    path: Path
    reason: str


NumberedCopyRecord = (NumberedCopySource | NumberedCopyRemovalPending
                      | NoNumberedCopySource | UnreadableNumberedCopySource)


_COMMIT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _record_numbered_copy_path(path: Path) -> BuildCopy:
    form = parse_form(path)
    if isinstance(form, UnverifiedFolderPath):
        raise form.error
    if not isinstance(form, BuildCopy):
        raise ValueError(f"{path}: expected a numbered copy")
    if path.absolute() != copy_path(form.repo, form.unit, form.number).absolute():
        raise ValueError(f"{path}: expected the numbered copy's configured path")
    return form


def _recorded_directory(fields: dict[str, object], key: str) -> CopyDirectoryIdentity:
    device, inode = fields.get(key + "_device"), fields.get(key + "_inode")
    if type(device) is not int or type(inode) is not int:
        raise ValueError(f"missing {key} identity")
    return CopyDirectoryIdentity(device, inode)


def _recorded_path(fields: dict[str, object], key: str) -> Path:
    value = fields.get(key)
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError(f"missing {key} path")
    return Path(value)


def _copy_git_checkout(path: Path, common: Path, common_identity: CopyDirectoryIdentity) -> git_files.Checkout:
    checkout = git_files.at(path)
    if (not stat.S_ISREG((path / ".git").lstat().st_mode)
            or checkout.common_dir != common
            or checkout.git_dir.parent != common / "worktrees"
            or common.is_relative_to(checkout.toplevel)
            or CopyDirectoryIdentity.from_stat(checkout.common_dir_status) != common_identity):
        raise ValueError("copy Git directory identity changed")
    pointer = checkout.git_dir / "gitdir"
    descriptor = os.open(pointer, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("copy Git back-pointer is not a regular file")
        target = stream.read().strip()
    if not target or (checkout.git_dir / target).resolve() != checkout.toplevel / ".git":
        raise ValueError("copy Git back-pointer names a different checkout")
    return checkout


def _copy_identity_fields(fields: dict[str, object], copy: NumberedCopySource) -> None:
    fields.update(copy_path=str(copy.path.absolute()), source_path=str(copy.source),
                  copy_device=copy.checkout_identity.device, copy_inode=copy.checkout_identity.inode,
                  git_directory=str(copy.git_directory), git_device=copy.git_identity.device,
                  git_inode=copy.git_identity.inode, common_directory=str(copy.common_directory),
                  common_device=copy.common_identity.device, common_inode=copy.common_identity.inode)


def _numbered_copy_record(path: Path, form: BuildCopy, directory: int) -> NumberedCopyRecord:
    try:
        fields = _read_copy_lease(path, directory)
    except FileNotFoundError:
        return NoNumberedCopySource()
    return _validate_numbered_copy_record(path, form, directory, fields)


def _validate_numbered_copy_record(path: Path, form: BuildCopy, directory: int,
                                   fields: dict[str, object]) -> NumberedCopyRecord:
    if "source_path" not in fields and "removing" not in fields:
        return NoNumberedCopySource()
    commit = fields.get("commit")
    if (fields.get("unit") != form.unit or fields.get("repository") != form.repo
            or fields.get("slot") != form.number or not isinstance(commit, str) or not _COMMIT_ID.fullmatch(commit)
            or _recorded_path(fields, "copy_path") != path.absolute()):
        raise ValueError("copy source record has the wrong path or commit")
    source = _recorded_path(fields, "source_path")
    identity = _recorded_directory(fields, "copy")
    common = _recorded_path(fields, "common_directory")
    common_identity = _recorded_directory(fields, "common")
    removing = fields.get("removing") is True
    if removing:
        git_directory = _recorded_path(fields, "git_directory")
        if git_directory.parent != common / "worktrees" or common.is_relative_to(path.absolute()):
            raise ValueError("copy Git directory is outside the source repository")
        record = NumberedCopySource(path, source, commit, identity, git_directory,
                                    _recorded_directory(fields, "git"), common, common_identity)
        return NumberedCopyRemovalPending(record)
    if identity != CopyDirectoryIdentity.from_stat(os.stat(path.name, dir_fd=directory, follow_symlinks=False)):
        raise ValueError("copy directory identity changed")
    checkout = _copy_git_checkout(path, common, common_identity)
    if git_files.at(source).common_dir != common:
        raise ValueError("copy source changed")
    git_identity = CopyDirectoryIdentity.from_stat(checkout.git_dir_status)
    if fields.get("ready") is not False or "git_directory" in fields:
        if (checkout.git_dir != _recorded_path(fields, "git_directory")
                or git_identity != _recorded_directory(fields, "git")):
            raise ValueError("copy Git directory identity changed")
    return NumberedCopySource(path, source, commit, identity, checkout.git_dir, git_identity, common, common_identity)


def numbered_copy_record(path: Path) -> NumberedCopyRecord:
    """Read the source or removal record for an exact numbered copy path."""
    try:
        form = _record_numbered_copy_path(path)
        with build_folder_lock.folder_parent(path, build_root(), create=False) as directory:
            return _numbered_copy_record(path, form, directory)
    except FileNotFoundError as error:
        # The lease can remain after an interruption removes the checkout.
        if not os.path.lexists(lease_path(path)):
            return NoNumberedCopySource()
        return UnreadableNumberedCopySource(path, str(error))
    except (OSError, ValueError, RuntimeError) as error:
        return UnreadableNumberedCopySource(path, str(error))


def _copy_creation_fields(path: Path, info: os.stat_result, source: Path, fields: dict[str, object]) -> None:
    repository = git_files.at(source)
    fields.update(copy_path=str(path.absolute()), source_path=str(source),
                  copy_device=info.st_dev, copy_inode=info.st_ino,
                  common_directory=str(repository.common_dir),
                  common_device=repository.common_dir_status.st_dev, common_inode=repository.common_dir_status.st_ino)


def _prepare_copy_reuse(path: Path, directory: int, spec: BuildFolderSpec, fields: dict[str, object]) -> bool:
    form = BuildCopy(spec.repository, spec.unit, spec.slot)
    assert isinstance(spec.slot_checkout, SlotCheckout)
    try:
        record = _numbered_copy_record(path, form, directory)
        if (not isinstance(record, NumberedCopySource) or record.source != spec.slot_checkout.source
                or not may_remove_folder_tree(path, record.git_directory)):
            raise ValueError("copy has no matching source record")
        _copy_identity_fields(fields, record)
        fields["commit"] = record.commit
        return True
    except git_files.NotACheckout as error:
        try:
            lease = _read_copy_lease(path, directory)
            repository = git_files.at(spec.slot_checkout.source)
            if (lease.get("ready") is False and "git_directory" not in lease
                    and _recorded_path(lease, "source_path") == spec.slot_checkout.source
                    and _recorded_path(lease, "common_directory") == repository.common_dir
                    and _recorded_directory(lease, "common") == CopyDirectoryIdentity.from_stat(repository.common_dir_status)
                    and not any(path.iterdir())
                    and may_remove_folder_tree(path)):
                return False
        except (OSError, ValueError, RuntimeError) as unreadable:
            raise FileExistsError(f"{path}: incomplete copy cannot be verified") from unreadable
        raise FileExistsError(f"{path}: incomplete copy retained") from error
    except (OSError, ValueError, RuntimeError) as error:
        raise FileExistsError(f"{path}: copy identity could not be verified") from error


def _current_copy_lock(path: Path, directory: int) -> None:
    try:
        descriptor = build_folder_lock_fd(path)
    except StopIteration:
        raise ValueError(f"{path}: the numbered copy lock is required") from None
    if not build_folder_lock.lock_current(path, directory, descriptor):
        raise ValueError(f"{path}: the numbered copy lock changed")


def record_numbered_copy_source(path: Path, source: Path, commit: str) -> None:
    """Publish the prepared commit while the maker holds the numbered copy lock."""
    form = _record_numbered_copy_path(path)
    with build_folder_lock.folder_parent(path, build_root(), create=False) as directory:
        _current_copy_lock(path, directory)
        record = _numbered_copy_record(path, form, directory)
        if not isinstance(record, NumberedCopySource) or record.source != source.resolve(strict=True):
            raise ValueError(f"{path}: the prepared checkout does not match the lease")
        head = git_files.head(git_files.at(path))
        if not _COMMIT_ID.fullmatch(commit) or not isinstance(head, git_files.DetachedHead) or head.commit != commit:
            raise ValueError(f"{path}: the prepared checkout does not match the requested commit")
        fields = _read_copy_lease(path, directory)
        _copy_identity_fields(fields, record)
        fields.update(commit=commit, ready=True)
        _publish_lease(path, directory, fields)

def mark_numbered_copy_removal(copy: NumberedCopySource) -> None:
    """Record removal intent while the remover holds the numbered copy lock."""
    path = copy.path
    form = _record_numbered_copy_path(path)
    with build_folder_lock.folder_parent(path, build_root(), create=False) as directory:
        _current_copy_lock(path, directory)
        if _numbered_copy_record(path, form, directory) != copy:
            raise ValueError(f"{path}: the numbered copy source changed")
        fields = _read_copy_lease(path, directory)
        _copy_identity_fields(fields, copy)
        fields["removing"] = True
        _publish_lease(path, directory, fields)


NUMBERED_COPY_LOCK_ENV = "NUMBERED_COPY_LOCK_FD"


@dataclass(frozen=True)
class NumberedCopyUseLease:
    """The reader holds the copy and can pass its descriptor to a child."""

    source: NumberedCopySource
    pass_fds: tuple[int, ...]

    @property
    def environment(self) -> Mapping[str, str]:
        return {NUMBERED_COPY_LOCK_ENV: str(self.pass_fds[0])}


@dataclass(frozen=True)
class NumberedCopyUseRefused:
    """The reader cannot hold this numbered copy."""

    reason: str


@contextmanager
def numbered_copy_use_lease(path: Path) -> Generator[NumberedCopyUseLease | NumberedCopyUseRefused]:
    """Hold a free copy for reading or reuse the inherited owner lock."""
    with ExitStack() as stack:
        try:
            form = _record_numbered_copy_path(path)
            directory = stack.enter_context(build_folder_lock.folder_parent(path, build_root(), create=False))
            inherited = next((str(fd) for fd, held in _locks.items() if held == path),
                             os.environ.get(NUMBERED_COPY_LOCK_ENV, ""))
            descriptor = build_folder_lock.open_lock(path, directory, create=False)
            _ = stack.callback(os.close, descriptor)
            if inherited.isdecimal():
                with suppress(OSError, OverflowError):
                    if build_folder_lock.lock_current(path, directory, int(inherited)):
                        descriptor = os.dup(int(inherited))
                        _ = stack.callback(os.close, descriptor)
            if not _take_lock(descriptor, fcntl.LOCK_EX, wait=False) or not build_folder_lock.lock_current(path, directory, descriptor):
                raise BlockingIOError(f"{path}: numbered copy is busy")
            record = _numbered_copy_record(path, form, directory)
            if (not isinstance(record, NumberedCopySource)
                    or _read_copy_lease(path, directory).get("ready") is not True):
                raise ValueError(f"{path}: no available numbered copy source")
            result = NumberedCopyUseLease(record, (descriptor,))
        except (OSError, ValueError, RuntimeError) as error:
            yield NumberedCopyUseRefused(str(error))
        else:
            yield result


@dataclass(frozen=True)
class KnownFolderLease:
    """A held copy has a complete owner record."""

    path: Path
    pid: int
    process_start: str
    start_time: datetime
    unit: str
    repository: str
    commit: str
    last_use: datetime


@dataclass(frozen=True)
class UnknownFolderLease:
    """A copy or its parent has an unknown lock state or owner."""

    path: Path
    reason: str


HeldFolderLease = KnownFolderLease | UnknownFolderLease


def _lease_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing lease time")
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError("lease time has no time zone")
    return date


def _copy_owner_record(path: Path, form: BuildCopy, fields: dict[str, object], *, held: bool) -> KnownFolderLease:
    pid = fields.get("pid")
    identity = fields.get("process_start")
    commit = fields.get("commit")
    if (type(pid) is not int or pid <= 0 or not isinstance(identity, str) or not identity
            or not isinstance(commit, str) or not _COMMIT_ID.fullmatch(commit)
            or fields.get("unit") != form.unit or fields.get("repository") != form.repo
            or type(fields.get("released")) is not bool or (held and fields["released"] is True)):
        raise ValueError("incomplete owner record")
    return KnownFolderLease(path, pid, identity, _lease_time(fields.get("start_time")),
                            form.unit, form.repo, commit, _lease_time(fields.get("last_use")))


BUILD_ROOT_EXCEPTIONS = frozenset({
    _UNIT_LOCKS, "sccache", "session", "lost+found", "old-cargo", "old-paths", "old-slot", "old-claude",
})


def copy_numbers(parent: Path) -> dict[int, bool]:
    """List copy numbers and whether each has a directory."""
    numbers: dict[int, bool] = {}
    with os.scandir(parent) as entries:
        for entry in entries:
            name = metadata_folder_name(entry.name)
            try:
                number = _parse_number(name)
            except ValueError:
                continue
            numbers[number] = numbers.get(number, False) or (name == entry.name and entry.is_dir(follow_symlinks=False))
    return numbers


def _copy_leases_in_unit(unit: Path, root: Path, timing: build_folder_lock.ProbeTiming) -> Iterator[HeldFolderLease]:
    try:
        numbers = copy_numbers(unit)
    except OSError as error:
        yield UnknownFolderLease(unit, str(error))
        return
    for number in sorted(numbers):
        path = unit / str(number)
        try:
            form = BuildCopy(unit.parent.name, unit.name, number)
            with build_folder_lock.folder_parent(path, root, create=False) as directory:
                descriptor = build_folder_lock.open_lock(path, directory, create=False)
                try:
                    free = build_folder_lock.take_lock(descriptor, fcntl.LOCK_EX, timing)
                    if not build_folder_lock.lock_current(path, directory, descriptor):
                        raise ValueError("lock changed during the read")
                    if not stat.S_ISDIR(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
                        raise ValueError("copy is not a directory")
                    fields = _read_copy_lease(path, directory)
                    owner = _copy_owner_record(path, form, fields, held=not free)
                    if free and (fields.get("ready") is not True or not isinstance(
                            _validate_numbered_copy_record(path, form, directory, fields), NumberedCopySource)):
                        raise ValueError("copy has no prepared source record")
                finally:
                    os.close(descriptor)
            if not free:
                yield owner
        except (OSError, ValueError, RuntimeError) as error:
            yield UnknownFolderLease(path, str(error))


def copy_unit_folders(root: Path) -> Iterator[Path | UnknownFolderLease]:
    """List unit directories or report the directory whose state is unreadable."""
    try:
        repositories = list(root.iterdir())
    except FileNotFoundError:
        return
    except OSError as error:
        yield UnknownFolderLease(root, str(error))
        return
    for repo in repositories:
        if repo.name in BUILD_ROOT_EXCEPTIONS:
            continue
        try:
            if not stat.S_ISDIR(repo.lstat().st_mode):
                continue
            units = list(repo.iterdir())
        except OSError as error:
            yield UnknownFolderLease(repo, str(error))
            continue
        for unit in units:
            if unit.name in ("ci", "main"):
                continue
            try:
                mode = unit.lstat().st_mode
            except OSError as error:
                yield UnknownFolderLease(unit, str(error))
                continue
            if stat.S_ISDIR(mode):
                yield unit


def held_folder_leases() -> Iterator[HeldFolderLease]:
    """List held copies and paths whose lease state is unknown."""
    settings = _settings()
    root = Path(settings.text("roots.build")).expanduser()
    timing = _probe_timing(wait=False, settings=settings)
    for unit in copy_unit_folders(root):
        if isinstance(unit, UnknownFolderLease):
            yield unit
        else:
            yield from _copy_leases_in_unit(unit, root, timing)


def copy_folders(root: Path) -> Iterator[tuple[Path, BuildCopy]]:
    """List existing copy directories for the disk reports."""
    for unit in copy_unit_folders(root):
        if isinstance(unit, UnknownFolderLease):
            continue
        try:
            numbers = copy_numbers(unit)
        except OSError:
            continue
        for number, exists in numbers.items():
            if exists:
                yield unit / str(number), BuildCopy(unit.parent.name, unit.name, number)


def numbered_copy_records() -> Iterator[NumberedCopySource | NumberedCopyRemovalPending | UnreadableNumberedCopySource]:
    """List numbered copy records, including interrupted removals and unreadable entries."""
    for unit in copy_unit_folders(build_root()):
        if isinstance(unit, UnknownFolderLease):
            yield UnreadableNumberedCopySource(unit.path, unit.reason)
            continue
        try:
            numbers = copy_numbers(unit)
        except OSError as error:
            yield UnreadableNumberedCopySource(unit, str(error))
            continue
        for number in sorted(numbers):
            path = unit / str(number)
            record = numbered_copy_record(path)
            if not isinstance(record, NoNumberedCopySource):
                yield record
            elif numbers[number]:
                yield UnreadableNumberedCopySource(path, "numbered copy has no source record")


def _write_lease(path: Path, directory: int, spec: BuildFolderSpec, *, released: bool = False,
                 ready: bool = True, copy_fields: Iterable[tuple[str, object]] = ()) -> None:
    now = datetime.now(timezone.utc).isoformat()
    record: dict[str, object] = {"showrunner": spec.showrunner, "unit": spec.unit or spec.owner,
              "work": spec.work, "purpose": spec.purpose, "pid": spec.owner_pid or os.getpid(),
              "start_time": now}
    if spec.kind in ("copy", "slot") and isinstance(spec.slot_checkout, SlotCheckout):
        if released:
            record = _read_copy_lease(path, directory)
        elif spec.kind == "copy":
            started = process_start.process_start(spec.owner_pid or os.getpid())
            if isinstance(started, (process_start.Started, process_start.Zombie)):
                record["process_start"] = started.start
        if spec.kind == "copy":
            record["released"] = released
        record.update(machine=spec.machine, repository=spec.repository, slot=spec.slot,
                      last_use=now)
        if spec.kind != "copy" or not released:
            record.update(commit=spec.slot_checkout.commit, ready=ready)
            record.update(copy_fields)
    _publish_lease(path, directory, record)


def _publish_lease(path: Path, directory: int, record: Mapping[str, object]) -> None:
    name = path.name + ".lease.json"
    try:
        existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(existing.st_mode):
            raise OSError(errno.ELOOP, "a build folder lease cannot be a symbolic link", name)
        # A concurrent os.replace can unlink the lease after this lookup, so no link is not a fault.
        if not stat.S_ISREG(existing.st_mode) or existing.st_nlink > 1:
            raise ValueError("a build folder lease must be a regular file with one link")
    text = json.dumps(record) + "\n"
    temporary = name + "." + secrets.token_hex(8) + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "w") as output:
            _ = output.write(text)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class _TemporaryFolderRequest:
    """The temporary folder request retains its cleanup stack."""
    request: UniqueBuildFolder | BuildFolderSpec
    cleanup: ExitStack


def _request_spec(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest) -> BuildFolderSpec:
    if isinstance(request, _TemporaryFolderRequest):
        return _request_spec(request.request)
    if isinstance(request, BuildFolderSpec):
        return request
    if isinstance(request, (LinkedEditableRepository, RemoteEditableRepository)):
        path = request.path.expanduser().absolute()
        if isinstance(request, RemoteEditableRepository) and request.depth < 1:
            raise ValueError("an editable clone needs a positive depth")
        return BuildFolderSpec(kind="editable", parent=path.parent, purpose=path.name, suffix_length=0)
    else:
        stable = isinstance(request, StableOwnerFolder)
        name = request.name if isinstance(request, StableOwnerFolder) else request.prefix
        _check_name(name)
        if stable and not name:
            raise ValueError("a stable build folder needs an owner name")
        spec = BuildFolderSpec(kind="legacy-stable" if stable else "legacy-unique", parent=_parent(request.parent),
                               purpose=name, suffix_length=_settings().whole("names.suffix_length"))
        return spec


def _stable_folder(spec: BuildFolderSpec) -> bool:
    return spec.kind in ("standing", "editable", "mirror", "target", "shell-cache", "legacy-stable", "slot", "copy")


FolderCreationNotice = Callable[[Path, os.stat_result], None] | Literal["untracked"]


def create_build_folder(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest, *,
                        on_create: FolderCreationNotice = "untracked") -> Path:
    """Allocate a unique folder or retain an owner's folder."""
    original = request.request if isinstance(request, _TemporaryFolderRequest) else request
    spec = _request_spec(original)
    with ExitStack() as source_locks:
        descriptors: tuple[int, ...] = ()
        if isinstance(original, LinkedEditableRepository):
            descriptors = source_locks.enter_context(_linked_source_locks(original.source))
        return _create_build_folder(request, spec, descriptors, on_create)


def _create_build_folder(request: BuildFolderRequest | BuildFolderSpec | _TemporaryFolderRequest,
                         spec: BuildFolderSpec, source_descriptors: tuple[int, ...],
                         on_create: FolderCreationNotice) -> Path:
    original = request.request if isinstance(request, _TemporaryFolderRequest) else request
    if isinstance(original, LinkedEditableRepository) and isinstance(original.branch, ExistingWorktreeBranch):

        branch = subprocess.run(["git", "-C", str(original.source), "show-ref", "--verify", "--quiet",
                                 f"refs/heads/{original.branch.name}"], capture_output=True,
                                 timeout=repository_git_timeout(), pass_fds=source_descriptors)
        if branch.returncode:
            raise ValueError(f"existing worktree branch {original.branch.name!r} does not exist in {original.source}")
    editable_repository = isinstance(original, (LinkedEditableRepository, RemoteEditableRepository))
    stable = _stable_folder(spec)
    # Only a name with a random part gets a new name on a clash; a tool name is fixed per request.
    renamed = not stable and not (spec.suffix or spec.work) and spec.kind != "tool"
    descriptor = -1
    try:
        with ExitStack() as allocation_scope:
            parents = request.cleanup if isinstance(request, _TemporaryFolderRequest) else allocation_scope
            directories: dict[Path, int] = {}
            while True:
                allocation = (replace(spec, work=_new_suffix(spec.suffix_length, _settings()))
                              if spec.kind not in ("copy", "slot") and (spec.showrunner or spec.unit) and not spec.work else spec)
                path = (original.path.expanduser().absolute()
                        if isinstance(original, (LinkedEditableRepository, RemoteEditableRepository))
                        else build_folder_path(allocation))
                directory = directories.get(path.parent)
                if directory is None:
                    directory = parents.enter_context(build_folder_lock.folder_parent(path, spec.parent,
                        create=stable or not isinstance(original, UniqueBuildFolder)))
                    directories[path.parent] = directory
                try:
                    descriptor = build_folder_lock.open_lock(path, directory, create=True)
                except FileNotFoundError:
                    if spec.kind != "copy":
                        raise
                    del directories[path.parent]
                    continue
                made_inode: os.stat_result | None = None
                allocated: os.stat_result | None = None
                new_folder = False
                try:
                    if not _take_lock(descriptor, fcntl.LOCK_SH if stable and spec.kind not in ("copy", "slot") and not editable_repository else fcntl.LOCK_EX,
                                      wait=spec.kind not in ("copy", "slot")):
                        if renamed:
                            continue
                        raise BlockingIOError(errno.EWOULDBLOCK, "build folder is held", str(path))
                    if not build_folder_lock.lock_current(path, directory, descriptor):
                        if spec.kind == "copy":
                            del directories[path.parent]
                        continue
                    try:
                        existing = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        existing = None
                    if existing is not None:
                        if editable_repository:
                            if not (isinstance(original, LinkedEditableRepository) and original.allow_empty
                                    and _empty_editable_destination(path, directory, existing)):
                                raise FileExistsError(errno.EEXIST, "editable repository destination is occupied", str(path))
                        if not stable:
                            if not renamed:
                                raise FileExistsError(errno.EEXIST, "build folder already exists", str(path))
                            continue
                        if not stat.S_ISDIR(existing.st_mode):
                            raise FileExistsError(errno.EEXIST, "build folder is not a directory", str(path))
                    copy_fields: dict[str, object] = {}
                    checkout_ready = False
                    if isinstance(spec.slot_checkout, SlotCheckout):
                        if spec.kind == "copy":
                            if existing is not None:
                                checkout_ready = _prepare_copy_reuse(path, directory, spec, copy_fields)
                            else:
                                try:
                                    lease = _read_copy_lease(path, directory)
                                except FileNotFoundError:
                                    lease = {}
                                except (OSError, ValueError) as error:
                                    raise FileExistsError(f"{path}: copy lease cannot be read") from error
                                if "source_path" in lease or "removing" in lease:
                                    raise FileExistsError(f"{path}: a recorded copy checkout is missing")
                        else:
                            try:
                                lease = _read_copy_lease(path, directory)
                            except (OSError, ValueError):
                                lease = {}
                            checkout_ready = lease.get("ready") is True
                        if existing is None or not checkout_ready:
                            try:
                                source_descriptors = allocation_scope.enter_context(_linked_source_locks(spec.slot_checkout.source))
                            except BlockingIOError as error:
                                raise SlotSourceBusy(errno.EWOULDBLOCK, f"slot source is busy: {error}",
                                                     str(spec.slot_checkout.source)) from error
                        if existing is not None and not checkout_ready and spec.kind != "copy":
                            print(f"kept for a manual removal list: {path} (build_folders, incomplete slot)",
                                  file=sys.stderr)
                            raise FileExistsError(f"{path}: the slot checkout is incomplete")
                    new_folder = existing is None
                    if spec.kind == "slot":
                        _write_lease(path, directory, allocation, ready=not new_folder)
                    elif spec.kind != "copy":
                        _write_lease(path, directory, allocation)
                    if existing is None:
                        mode = request.mode if isinstance(request, StableOwnerFolder) else 0o777 if stable else 0o700
                        try:
                            os.mkdir(path.name, mode=mode, dir_fd=directory)
                            made_inode = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                        except FileExistsError:
                            if not stable or editable_repository or spec.kind == "copy":
                                raise
                            if not stat.S_ISDIR(os.stat(path.name, dir_fd=directory, follow_symlinks=False).st_mode):
                                raise
                    allocated = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                    if isinstance(original, (LinkedEditableRepository, RemoteEditableRepository)):

                        if isinstance(original, LinkedEditableRepository):
                            command = ["git", "-C", str(original.source), "worktree", "add", str(path), original.branch.name]
                        else:
                            command = ["git", "clone", "--depth", str(original.depth), "--branch", original.ref,
                                       "--", original.url, str(path)]
                        try:
                            if isinstance(original, LinkedEditableRepository) and isinstance(original.branch, NewWorktreeBranch):
                                _ = subprocess.run(["git", "-C", str(original.source), "branch", "--",
                                                    original.branch.name, original.branch.start_point], check=True,
                                                    capture_output=True, text=True, timeout=repository_git_timeout(),
                                                    pass_fds=source_descriptors)
                            _ = subprocess.run(command, check=True, capture_output=True, text=True,
                                               timeout=repository_git_timeout(), pass_fds=source_descriptors)
                        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
                            detail: str | bytes = (str(error) if isinstance(error, OSError)
                                                   else cast(str | bytes, error.stderr or str(error)))
                            if isinstance(detail, bytes):
                                detail = detail.decode(errors="replace")
                            raise OSError(errno.EIO, f"git repository creation failed: {detail.strip()}", str(path)) from error
                        if made_inode is not None and callable(on_create):
                            on_create(path, made_inode)
                        return path
                    if spec.kind == "copy" and isinstance(spec.slot_checkout, SlotCheckout):
                        if not checkout_ready:
                            _copy_creation_fields(path, allocated, spec.slot_checkout.source, copy_fields)
                        _write_lease(path, directory, allocation, ready=False, copy_fields=copy_fields.items())
                    if (isinstance(spec.slot_checkout, SlotCheckout)
                            and (new_folder or (spec.kind == "copy" and not checkout_ready))):
                        _ = subprocess.run(["git", "-C", str(spec.slot_checkout.source), "worktree", "add", "--detach",
                                            str(path), spec.slot_checkout.commit], check=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            pass_fds=(descriptor, *held_build_folder_descriptors(), *source_descriptors),
                                           timeout=repository_git_timeout())
                        if spec.kind == "copy":
                            form = BuildCopy(spec.repository, spec.unit, spec.slot)
                            record = _numbered_copy_record(path, form, directory)
                            if not isinstance(record, NumberedCopySource):
                                raise ValueError(f"{path}: copy registration has no source record")
                            fields = _read_copy_lease(path, directory)
                            _copy_identity_fields(fields, record)
                            _publish_lease(path, directory, fields)
                        else:
                            _write_lease(path, directory, allocation)
                    if made_inode is not None and callable(on_create):
                        on_create(path, made_inode)
                    os.set_inheritable(descriptor, True)
                    _locks[descriptor] = path
                    if isinstance(request, _TemporaryFolderRequest):
                        _ = request.cleanup.callback(_finish_temporary, path, directory, allocated,
                                                     descriptor=descriptor, temporary=not stable)
                    return path
                except BaseException:
                    _ = _locks.pop(descriptor, None)
                    if isinstance(original, LinkedEditableRepository) or spec.kind == "copy":
                        if allocated is not None:
                            print(f"kept for a manual removal list: {path} (build_folders, checkout setup failed)",
                                  file=sys.stderr)
                    elif ((made_inode is not None or (new_folder and not stable))
                          and _take_lock(descriptor, fcntl.LOCK_EX)):
                        _remove_failed_allocation(path, directory, made_inode)
                    raise
                finally:
                    if descriptor not in _locks:
                        closing = descriptor
                        descriptor = -1
                        os.close(closing)
    except BaseException:
        if descriptor >= 0:
            _release_build_folder(_locks[descriptor], descriptor=descriptor)
        raise


def _empty_editable_destination(path: Path, parent: int, expected: os.stat_result) -> bool:
    if not stat.S_ISDIR(expected.st_mode):
        return False
    descriptor = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        current = os.fstat(descriptor)
        return (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino) and not os.listdir(descriptor)
    finally:
        os.close(descriptor)


def _remove_failed_allocation(path: Path, directory: int, allocated: os.stat_result | None) -> None:
    if allocated is not None:
        _remove_folder(path, directory, allocated)
    if not _folder_exists(path, directory):
        _unlink_metadata(path, directory)


def _remove_folder(path: Path, directory: int, allocated: os.stat_result) -> None:
    def repair(function: object, name: str, error: BaseException) -> None:
        if not isinstance(error, PermissionError):
            raise error
        entry = Path(name)
        for candidate in (entry.parent, entry):
            if not candidate.is_relative_to(path.name):
                continue
            inode = os.stat(candidate, dir_fd=directory, follow_symlinks=False)
            if inode.st_uid == os.geteuid() and not stat.S_ISLNK(inode.st_mode):
                os.chmod(candidate, 0o700, dir_fd=directory, follow_symlinks=False)
        if function is os.open:
            shutil.rmtree(name, dir_fd=directory, onexc=repair)
        else:
            _ = cast(Callable[..., object], function)(name, dir_fd=directory)

    try:
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if os.path.samestat(current, allocated):
        if not may_remove_build_path(path):
            raise PermissionError(f"{path}: retain the protected build folder")
        shutil.rmtree(path.name, dir_fd=directory, onexc=repair)


def _folder_exists(path: Path, directory: int) -> bool:
    try:
        _ = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _unlink_metadata(path: Path, directory: int) -> None:
    temporary = re.compile(re.escape(path.name) + r"\.lease\.json\.[0-9a-f]{16}\.tmp")
    for name in os.listdir(directory):
        if temporary.fullmatch(name):
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
    for suffix in (".lease.json", ".lock"):
        try:
            os.unlink(path.name + suffix, dir_fd=directory)
        except FileNotFoundError:
            pass


def temporary_allocation_entries(folder: Path) -> tuple[Path, ...]:
    """List the folder and its lease files that are still present."""
    try:
        return tuple(sorted(entry for entry in folder.parent.iterdir() if entry == folder or (
            (metadata := _METADATA.fullmatch(entry.name)) is not None and metadata[1] == folder.name)))
    except FileNotFoundError:
        return ()


def _parent_has_symlink(parent: Path) -> bool:
    absolute = parent.absolute()
    # A link before '..' can lead to the same parent as the absolute path.
    return (Path(os.path.abspath(absolute)) != absolute.resolve()
            or any(component.is_symlink() for component in (absolute, *absolute.parents)))


@dataclass(frozen=True)
class RefusedRemovalLease:
    """A busy or unreadable lock prevents removal."""


@dataclass(frozen=True)
class RemovalLockHeld(RefusedRemovalLease):
    lock: Path


@dataclass(frozen=True)
class UnreservedRemovalFolder:
    """The folder has no lock file to hold."""


SidecarIdentity = tuple[int, int, int]


@dataclass(frozen=True)
class HeldFolderRemovalLease:
    """The descriptor reserves the folder name until lock removal."""

    folder: Path
    directory: int
    descriptor: int

    def _sidecar_identity(self, name: str) -> SidecarIdentity | None:
        try:
            inode = os.stat(name, dir_fd=self.directory, follow_symlinks=False)
        except OSError:
            return None
        if stat.S_ISREG(inode.st_mode) and inode.st_nlink == 1:
            return inode.st_dev, inode.st_ino, inode.st_ctime_ns
        return None

    def sidecar_identities(self) -> dict[str, SidecarIdentity]:
        """Read the identities for this removal while the lock is held."""
        try:
            lock = os.fstat(self.descriptor)
        except OSError:
            return {}
        lock_name = self.folder.name + ".lock"
        lock_identity = (lock.st_dev, lock.st_ino, lock.st_ctime_ns)
        if self._sidecar_identity(lock_name) != lock_identity:
            return {}
        lease_name = self.folder.name + ".lease.json"
        lease_identity = self._sidecar_identity(lease_name)
        identities = {lease_name: lease_identity} if lease_identity is not None else {}
        identities[lock_name] = lock_identity
        return identities

    def _source_removed(self) -> bool:
        try:
            return (os.path.samestat(os.fstat(self.directory), self.folder.parent.stat())
                    and not _folder_exists(self.folder, self.directory))
        except OSError:
            return False

    def unlink_sidecars(self, expected: dict[str, SidecarIdentity]) -> None:
        """Remove the original sidecars after the source and marks are gone."""
        _ = self.unlink_matching_sidecars(expected)

    def unlink_matching_sidecars(self, expected: dict[str, SidecarIdentity], *,
                                 reject_parent_links: bool = False) -> set[str]:
        """Remove sidecars with matching identities and return their names."""
        removed: set[str] = set()
        lock_name = self.folder.name + ".lock"
        lock_identity = expected.get(lock_name)
        if lock_identity is None:
            return removed
        for name in (self.folder.name + ".lease.json", lock_name):
            identity = expected.get(name)
            with suppress(OSError):
                if (identity is not None and self._source_removed()
                        and (not reject_parent_links or not _parent_has_symlink(self.folder.parent))
                        and self._sidecar_identity(name) == identity
                        and (name == lock_name or self._sidecar_identity(lock_name) == lock_identity)):
                    os.unlink(name, dir_fd=self.directory)
                    removed.add(name)
        return removed


FolderRemovalLease = RefusedRemovalLease | UnreservedRemovalFolder | HeldFolderRemovalLease


def _hold_free_lease(path: Path, directory: int, stack: ExitStack, *, wait: bool = True,
                     create: bool = False) -> FolderRemovalLease:
    """Hold the lock if it is free. The stack owns the descriptor."""
    try:
        descriptor = build_folder_lock.open_lock(path, directory, create=create)
    except FileNotFoundError:
        return UnreservedRemovalFolder()
    _ = stack.callback(os.close, descriptor)
    acquired = _take_lock(descriptor, fcntl.LOCK_EX, wait=wait)
    if not build_folder_lock.lock_current(path, directory, descriptor):
        return RefusedRemovalLease()
    if not acquired:
        return RemovalLockHeld(path.with_name(path.name + ".lock"))
    _ = stack.callback(_locks.pop, descriptor, None)
    _locks[descriptor] = path
    return HeldFolderRemovalLease(path, directory, descriptor)


def _hold_child_leases(path: Path, stack: ExitStack, *, wait: bool = True,
                       probe_fds: list[int] | None = None) -> RefusedRemovalLease | None:
    """Hold every lease lock below the path. Return a refusal if a lock prevents removal."""
    def unreadable(error: OSError) -> None:
        if not isinstance(error, FileNotFoundError):
            raise error

    for top, folders, files, directory in os.fwalk(path, follow_symlinks=False, onerror=unreadable):
        for name in files:
            stem = name.removesuffix(".lock")
            if stem != name and (stem in folders or stem + ".lease.json" in files):
                lease = _hold_free_lease(Path(top) / stem, directory, stack, wait=wait)
                if isinstance(lease, RefusedRemovalLease):
                    return lease
                if probe_fds is not None and isinstance(lease, HeldFolderRemovalLease):
                    probe_fds.append(lease.descriptor)
    return None


@contextmanager
def folder_removal_lease(path: Path, *, children: bool = False, wait: bool = True,
                         create: bool = False, root: Path | None = None,
                         probe_fds: list[int] | None = None) -> Generator[FolderRemovalLease]:
    """Hold the collector's locks and return the reservation for a folder."""
    metadata = _METADATA.fullmatch(path.name)
    folder = path.with_name(metadata[1]) if metadata else path
    with ExitStack() as stack:
        try:
            directory = stack.enter_context(build_folder_lock.folder_parent(folder, root or folder.parent, create=False))
            lease = _hold_free_lease(folder, directory, stack, wait=wait, create=create)
            if probe_fds is not None:
                probe_fds.append(directory)
                if isinstance(lease, HeldFolderRemovalLease):
                    probe_fds.append(lease.descriptor)
            if not isinstance(lease, RefusedRemovalLease):
                if metadata is not None:
                    lease = UnreservedRemovalFolder()
                elif children:
                    with suppress(FileNotFoundError):
                        refusal = _hold_child_leases(folder, stack, wait=wait, probe_fds=probe_fds)
                        if refusal is not None:
                            lease = refusal
        except FileNotFoundError:
            lease = UnreservedRemovalFolder()
        except (OSError, ValueError):
            lease = RefusedRemovalLease()
        yield lease


@contextmanager
def collector_lease(path: Path, *, children: bool = False, wait: bool = True) -> Generator[bool]:
    """Hold a managed folder's lease lock while a collector judges and removes it.

    Yield False when an owner holds the lease. A folder without a lock file has no lease.
    Use build_folder_lock_fd while the collector holds the lock.
    A lock file or lease record is judged by its folder's lease. With children, also hold every lease below the folder; a held one, or an unreadable
    folder, is busy.
    """
    with folder_removal_lease(path, children=children, wait=wait) as lease:
        yield not isinstance(lease, RefusedRemovalLease)


@contextmanager
def leased_build_folder(spec: BuildFolderSpec | UniqueBuildFolder, *,
                        on_create: FolderCreationNotice = "untracked") -> Generator[Path]:
    """Hold the maker lease while a persistent folder is in use."""
    path = create_build_folder(spec, on_create=on_create)
    try:
        yield path
    finally:
        _release_build_folder(path)


@contextmanager
def slot_build_folder(spec: BuildFolderSpec) -> Generator[Path]:
    """Keep the checkout locked and record its last use before release."""
    path = create_build_folder(spec)
    try:
        yield path
    finally:
        try:
            if spec.kind == "copy":
                mark_folder_use(path)
            with build_folder_lock.folder_parent(path, spec.parent, create=False) as directory:
                _write_lease(path, directory, spec, released=True)
        finally:
            _release_build_folder(path)


def remove_build_folder(path: Path, cleanup: Callable[[], object], *, temporary: bool = True,
                        dir_fd: int | None = None, wait: bool = True) -> bool:
    """Run cleanup under the free folder's lock, then remove temporary metadata."""
    if not may_remove_build_path(path):
        return False
    with ExitStack() as parents:
        try:
            directory = dir_fd if dir_fd is not None else parents.enter_context(
                build_folder_lock.folder_parent(path, path.parent, create=False))
        except FileNotFoundError:
            # An absent parent has no folder or metadata to lock.
            return cleanup() is not False
        descriptor = build_folder_lock.open_lock(path, directory, create=True)
        try:
            if not _take_lock(descriptor, fcntl.LOCK_EX, wait=wait):
                return False
            if not build_folder_lock.lock_current(path, directory, descriptor):
                return False
            try:
                if cleanup() is False:
                    return False
            except FileNotFoundError:
                if temporary and not _folder_exists(path, directory):
                    _unlink_metadata(path, directory)
                raise
            if _folder_exists(path, directory):
                return False
            if temporary:
                _unlink_metadata(path, directory)
            return True
        finally:
            os.close(descriptor)


def expired_folder(path: Path, *, cutoff: float) -> bool:
    """Check an old use stamp while the folder is free."""
    form = parse_form(path)
    if not isinstance(form, (WorktreeOutput, MainOutput, BuildCopy)):
        return False
    with ExitStack() as stack:
        try:
            directory = stack.enter_context(build_folder_lock.folder_parent(path, build_root(), create=False))
            lease = _hold_free_lease(path, directory, stack, wait=False, create=False)
            return (not isinstance(lease, RefusedRemovalLease)
                    and _folder_has_expired(path, directory, cutoff, form))
        except FileNotFoundError:
            return False


def _folder_has_expired(path: Path, directory: int, cutoff: float, form: FolderForm) -> bool:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    folder = os.open(path.name, flags, dir_fd=directory)
    try:
        info = os.fstat(folder)
        if info.st_dev != os.fstat(directory).st_dev:
            return False
        try:
            stamp = os.stat(COPY_USE_STAMP, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            if not isinstance(form, BuildCopy):
                return False
            try:
                fields = _read_copy_lease(path, directory)
                return (fields.get("ready") is False
                        and _lease_time(fields.get("last_use")).timestamp() <= cutoff
                        and isinstance(_numbered_copy_record(path, form, directory), NumberedCopySource))
            except (OSError, ValueError, RuntimeError):
                return False
        return stat.S_ISREG(stamp.st_mode) and stamp.st_mtime <= cutoff
    finally:
        os.close(folder)


class FolderPairRelease(Enum):
    RELEASED = "released the build folder pair"
    PAIR_ABSENT = "the build folder pair is absent"
    PARTIAL = "only part of the build folder pair was released"
    PAIR_KEPT = "the lease record and lock could not be removed"
    FOLDER_PRESENT = "the folder path still exists"
    LOCK_HELD = "the build folder lock is held or changed"
    PARENT_ABSENT = "the parent folder is absent"
    PARENT_LINK = "the parent path contains a symbolic link"


def release_removed_folder(path: Path) -> FolderPairRelease:
    """Release the lease and lock after another operation removes the folder."""
    path = path.absolute()
    if _parent_has_symlink(path.parent):
        return FolderPairRelease.PARENT_LINK
    with ExitStack() as stack:
        try:
            directory = stack.enter_context(build_folder_lock.folder_parent(path, Path(path.anchor), create=False))
        except FileNotFoundError:
            return FolderPairRelease.PARENT_ABSENT
        if _folder_exists(path, directory):
            return FolderPairRelease.FOLDER_PRESENT
        lock_name = path.name + ".lock"
        names = (path.name + ".lease.json", lock_name)
        present = {name for name in names if _folder_exists(path.with_name(name), directory)}
        if not present:
            return FolderPairRelease.PAIR_ABSENT
        lease = _hold_free_lease(path, directory, stack, wait=False, create=True)
        if not isinstance(lease, HeldFolderRemovalLease):
            return FolderPairRelease.LOCK_HELD
        removed = lease.unlink_matching_sidecars(lease.sidecar_identities(), reject_parent_links=True)
        if present <= removed and lock_name in removed:
            return FolderPairRelease.RELEASED
        if removed:
            return FolderPairRelease.PARTIAL
        if _folder_exists(path, directory):
            return FolderPairRelease.FOLDER_PRESENT
        return FolderPairRelease.PAIR_KEPT


def _finish_temporary(path: Path, directory: int, allocated: os.stat_result, *,
                      descriptor: int, temporary: bool) -> None:
    if _locks.get(descriptor) != path:
        return
    _release_build_folder(path, descriptor=descriptor)
    _ = remove_build_folder(path, lambda: _remove_folder(path, directory, allocated),
                            temporary=temporary, dir_fd=directory)


def _remove_free(path: Path, directory: int, *, wait: bool) -> bool:
    """Remove a folder and its records when no process holds its lock or a lease below it."""
    def cleanup() -> bool:
        try:
            allocated = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return True
        with ExitStack() as children:
            # A held lease below a free folder, or an unreadable folder below it, keeps the folder.
            try:
                if _hold_child_leases(path, children, wait=wait) is not None:
                    return False
            except OSError:
                return False
            _remove_folder(path, directory, allocated)
        return True

    return remove_build_folder(path, cleanup, dir_fd=directory, wait=wait)


def sweep_temporary_build_folders(parent: Path, *, wait: bool = True) -> None:
    """Remove free allocations in a parent reserved for temporary maker folders.
    With wait False, a held folder costs one lock try, not the probe wait."""
    try:
        with build_folder_lock.folder_parent(parent / "entry", parent, create=False) as directory:
            names: set[str] = set()
            for entry in os.listdir(directory):
                match = _METADATA.fullmatch(entry)
                if match is not None:
                    names.add(match[1])
            for name in sorted(names):
                _ = _remove_free(parent / name, directory, wait=wait)
    except FileNotFoundError:
        pass


@contextmanager
def temporary_build_folder(request: UniqueBuildFolder | BuildFolderSpec) -> Generator[Path]:
    """Remove private work after the caller ends, including interrupted callers."""
    with ExitStack() as cleanup:
        yield create_build_folder(_TemporaryFolderRequest(request, cleanup))


def _claim_tool_folder(tool: str, parent: Path | None, allocate: Callable[[BuildFolderSpec], Path], *,
                       owner_pid: int = 0) -> Path:
    """Allocate <parent>/<tool>, or <tool>-2 and up while another run holds a name.

    A folder whose lock is free and whose lease names an ended process is a stopped run's leftover:
    it is removed and its name reused. Any other folder is passed over.
    """
    root = parent if parent is not None else inherited_temporary_parent()
    number = 1
    while True:
        spec = BuildFolderSpec(kind="tool", parent=root, purpose=tool, suffix=f"-{number}" if number > 1 else "",
                               suffix_length=0, owner_pid=owner_pid)
        try:
            return allocate(spec)
        except BlockingIOError:
            number += 1
        except FileExistsError:
            path = build_folder_path(spec)
            with build_folder_lock.folder_parent(path, root, create=False) as directory:
                leftover = _lease_owner_ended(path, directory) and _remove_free(path, directory, wait=False)
            if not leftover:
                number += 1


def _lease_owner_ended(path: Path, directory: int) -> bool:
    """True when the lease names a process that no longer runs. An unreadable lease is not proof."""
    try:
        descriptor = os.open(path.name + ".lease.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except OSError:
        return False
    with os.fdopen(descriptor) as lease:
        try:
            record = cast(object, json.load(lease))
        except ValueError:
            return False
    pid = cast(dict[str, object], record).get("pid") if isinstance(record, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


@contextmanager
def tool_folder(tool: str, parent: Path | None = None) -> Generator[Path]:
    """Give one tool run its own folder in the session folder, and remove it when the run ends.

    The parent is the caller's temporary parent unless the caller names one.
    """
    with ExitStack() as cleanup:
        yield _claim_tool_folder(tool, parent, lambda spec: cleanup.enter_context(temporary_build_folder(spec)))


OWNER_MARKER = ".build-folder-owner.json"


def run_build_command(command: Sequence[str], *, path: Path, environment: Mapping[str, str],
                      pass_fds: Sequence[int]) -> int:
    """Keep the caller until the command and its descendants stop."""
    from scripts.lib.descendants import adopt_descendants
    from scripts.lib.process_group import send_process_group

    if not command:
        raise ValueError("run needs a command after --")
    pid = os.getpid()
    if sys.platform == "darwin":
        child = subprocess.Popen(command, cwd=path, env=environment,
                                 pass_fds=pass_fds, start_new_session=True)
    else:
        _ = adopt_descendants()
        child = subprocess.Popen(command, cwd=path, env=environment,
                                 pass_fds=pass_fds, process_group=os.getpgrp())
    try:
        status = child.wait()
    finally:
        try:
            child.kill()
        except ProcessLookupError:
            pass
        _ = child.wait()
        if sys.platform == "darwin":
            # A descendant outside the group still holds the slot if it inherits the lock descriptor.
            try:
                send_process_group(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            while True:
                try:
                    send_process_group(child.pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
        else:
            while True:
                children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
                for descendant in children:
                    try:
                        os.kill(int(descendant), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    _ = os.waitpid(-1, 0)
                except ChildProcessError:
                    break
    return status if status >= 0 else 128 - status


def run_build(owner: str, name: str, command: Sequence[str], *, showrunner: str = "", unit: str = "",
              work: str = "") -> int:
    """Keep the folder until the command and its descendants stop."""
    import json

    settings = _settings()
    root = Path(os.path.expandvars(settings.text("roots.temporary").replace("${TMPDIR}", str(inherited_temporary_parent())))).expanduser().resolve()
    spec = BuildFolderSpec(kind="temporary", parent=root, repository=repository_key(Path.cwd()),
                           owner=owner, purpose=name, suffix_length=settings.whole("names.suffix_length"),
                           showrunner=showrunner, unit=unit, work=work)

    with temporary_build_folder(spec) as path:
        pid = os.getpid()
        start_time = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        information = path.stat()
        marker = {"owner": owner, "pid": pid, "start_time": start_time, "boot_id": boot_identity(),
                  "copy_path": str(path.resolve()), "copy_device": information.st_dev, "copy_inode": information.st_ino}
        _ = (path / OWNER_MARKER).write_text(json.dumps(marker) + "\n")
        descriptor = build_folder_lock_fd(path)
        return run_build_command(command, path=path,
                                 environment={**os.environ, "BUILD_FOLDER": str(path),
                                              "BUILD_FOLDER_LOCK_FD": str(descriptor)},
                                 pass_fds=(descriptor,))


def main(argv: Sequence[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    unique = commands.add_parser("unique")
    prefix = unique.add_mutually_exclusive_group(required=True)
    _ = prefix.add_argument("--prefix")
    _ = prefix.add_argument("--kind", choices=tuple(FOLDER_PREFIXES))
    _ = unique.add_argument("--owner", default="")
    unique_parent = unique.add_mutually_exclusive_group()
    _ = unique_parent.add_argument("--parent", type=Path)
    _ = unique_parent.add_argument("--system-temp", action="store_true")
    stable = commands.add_parser("stable")
    stable_destination = stable.add_mutually_exclusive_group()
    _ = stable_destination.add_argument("--path", type=Path)
    _ = stable_destination.add_argument("--run-temp", type=Path)
    _ = stable.add_argument("--name", default="")
    stable_parent = stable.add_mutually_exclusive_group()
    _ = stable_parent.add_argument("--parent", type=Path)
    _ = stable_parent.add_argument("--unit-root", action="store_true")
    editable = commands.add_parser("editable-repository")
    _ = editable.add_argument("--path", type=Path, required=True)
    source = editable.add_mutually_exclusive_group(required=True)
    _ = source.add_argument("--source", type=Path)
    _ = source.add_argument("--url")
    branch = editable.add_mutually_exclusive_group()
    _ = branch.add_argument("--new-branch")
    _ = branch.add_argument("--existing-branch")
    _ = editable.add_argument("--start-point")
    _ = editable.add_argument("--ref")
    _ = editable.add_argument("--depth", type=int)
    tool = commands.add_parser("tool")
    _ = tool.add_argument("--tool", required=True)
    _ = tool.add_argument("--parent", type=Path)
    _ = tool.add_argument("--owner-pid", type=int, required=True)
    remove = commands.add_parser("remove")
    _ = remove.add_argument("path", type=Path)
    run_temp = commands.add_parser("run-temp-path")
    _ = run_temp.add_argument("run", type=Path)
    run = commands.add_parser("run")
    _ = run.add_argument("--owner", default="")
    _ = run.add_argument("--showrunner", default="")
    _ = run.add_argument("--unit", default="")
    _ = run.add_argument("--work", default="")
    _ = run.add_argument("--name", required=True)
    _ = run.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    request: BuildFolderRequest
    command = cast(str, args.command)
    try:
        if command == "editable-repository":
            path = cast(Path, args.path)
            source_path = cast(Path | None, args.source)
            new_branch = cast(str | None, args.new_branch)
            existing_branch = cast(str | None, args.existing_branch)
            start = cast(str | None, args.start_point)
            ref = cast(str | None, args.ref)
            depth = cast(int | None, args.depth)
            if source_path is not None:
                if ref is not None or depth is not None:
                    raise ValueError("a linked worktree does not use --ref or --depth")
                selection: NewWorktreeBranch | ExistingWorktreeBranch
                if new_branch is not None and start is not None:
                    selection = NewWorktreeBranch(new_branch, start)
                elif existing_branch is not None and start is None:
                    selection = ExistingWorktreeBranch(existing_branch)
                else:
                    raise ValueError("a linked worktree needs --new-branch and --start-point, or --existing-branch")
                request = LinkedEditableRepository(path, source_path, selection)
            else:
                if new_branch is not None or existing_branch is not None or start is not None:
                    raise ValueError("a remote clone does not use worktree branch flags")
                if ref is None or depth is None:
                    raise ValueError("a remote clone needs --ref and --depth")
                request = RemoteEditableRepository(path, cast(str, args.url), ref, depth)
            print(create_build_folder(request))
            return 0
        if command == "run":
            arguments = cast(list[str], args.argv)
            owner = cast(str, args.owner) or cast(str, args.unit)
            if not owner:
                raise ValueError("run needs --owner or --unit")
            return run_build(owner, cast(str, args.name), arguments[1:] if arguments[:1] == ["--"] else arguments,
                             showrunner=cast(str, args.showrunner), unit=cast(str, args.unit), work=cast(str, args.work))
        if command == "tool":
            print(_claim_tool_folder(cast(str, args.tool), cast(Path | None, args.parent), create_build_folder,
                                     owner_pid=cast(int, args.owner_pid)))
            return 0
        if command == "remove":
            path = cast(Path, args.path)
            if not may_remove_build_path(path):
                raise PermissionError(f"{path}: retain the protected build folder")
            _release_build_folder(path)
            def cleanup() -> None:
                try:
                    shutil.rmtree(path)
                except FileNotFoundError:
                    pass
            return 0 if remove_build_folder(path, cleanup) else 1
        if command == "run-temp-path":
            from scripts.lib import unit_runs

            print(unit_runs.run_temp_path(cast(Path, args.run)))
            return 0
        if command == "stable":
            path = cast(Path | None, args.path)
            run = cast(Path | None, args.run_temp)
            if run is not None:
                from scripts.lib import unit_runs

                path = unit_runs.run_temp_path(run)
            if path is not None:
                request = StableOwnerFolder(path.name, ExplicitBuildParent(path.parent))
            else:
                parent: BuildFolderParent = UnitRunParent() if cast(bool, args.unit_root) else ExplicitBuildParent(cast(Path, args.parent))
                if not cast(bool, args.unit_root) and cast(Path | None, args.parent) is None:
                    raise ValueError("a stable folder needs --path, --run-temp, or a parent and name")
                request = StableOwnerFolder(cast(str, args.name), parent)
        else:
            unique_choice: BuildFolderParent = InheritedTemporaryParent()
            explicit_parent = cast(Path | None, args.parent)
            if cast(bool, args.system_temp):
                unique_choice = SystemTemporaryParent()
            elif explicit_parent is not None:
                unique_choice = ExplicitBuildParent(explicit_parent)
            kind = cast(str | None, args.kind)
            name = FOLDER_PREFIXES[kind].format(owner=cast(str, args.owner)) if kind else cast(str, args.prefix)
            request = UniqueBuildFolder(name, unique_choice)
        print(create_build_folder(request))
    except (OSError, ValueError) as error:
        print(f"build folders: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

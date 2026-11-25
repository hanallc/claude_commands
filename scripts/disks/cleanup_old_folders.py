#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""List old folders outside the build model. Only Nate runs --go by hand."""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root:
        exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import ast
import fnmatch
import hashlib
import hmac
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from itertools import chain
from pathlib import Path
from typing import Literal, NamedTuple, NotRequired, TextIO, TypedDict, cast, override

from scripts.copies import copies
from scripts.lib import build_folder_lock, build_folders, conf, git_files, git_rescue, process_start, removal_guard
from scripts.lib.process_failure import failure_detail
from scripts.lint import tmp_sweep
from scripts.production import staff
from scripts.worktree import worktree_records


class Machine(Enum):
    NATEDEV = "natedev"
    MAC = "mac"


class RemovalAssessment(Enum):
    PENDING = "check for use"


class CleanupPlace(Enum):
    FOLDER = "configured folder"
    LINK = "old worktree link"
    ORPHAN_SIDECARS = "orphan worktree sidecars"
    REVIEW_FOLDER = "old worktree folder for review"


@dataclass(frozen=True)
class Candidate:
    path: Path
    decision: RemovalAssessment | removal_guard.Kept
    place: CleanupPlace = CleanupPlace.FOLDER


@dataclass(frozen=True)
class WouldRemove:
    fingerprint: str


@dataclass(frozen=True)
class Row:
    path: Path
    size: int | None
    outcome: WouldRemove | removal_guard.Kept
    git: str = ""
    sidecars: tuple[str, ...] = ()
    place: CleanupPlace = CleanupPlace.FOLDER


class RowRecord(TypedDict):
    path: str
    bytes: int | None
    keep: str
    fingerprint: str
    git: str
    sidecars: NotRequired[tuple[str, ...]]


class HostRecord(TypedDict):
    machine: str
    rows: list[RowRecord]
    digest: str


class WorkerRowRecord(RowRecord):
    place: str


def row_record(row: Row) -> RowRecord:
    outcome = row.outcome
    record: RowRecord = {
        "path": str(row.path), "bytes": row.size,
        "keep": outcome.reason if isinstance(outcome, removal_guard.Kept) else "",
        "fingerprint": outcome.fingerprint if isinstance(outcome, WouldRemove) else "",
        "git": row.git,
    }
    if row.sidecars:
        record["sidecars"] = row.sidecars
    return record


@dataclass(frozen=True)
class HostList:
    machine: Machine
    rows: tuple[Row, ...]
    unreadable_processes: tuple[tuple[int, str], ...] = ()

    def record(self) -> HostRecord:
        rows = [row_record(row) for row in self.rows]
        return {"machine": self.machine.value, "rows": rows, "digest": digest((self.machine.value, rows))}


@dataclass(frozen=True)
class MacListUnavailable:
    detail: str

    def message(self) -> str:
        return f"{MAC_UNREACHABLE}: {self.detail}" if self.detail else MAC_UNREACHABLE


class LocalListOnly(Enum):
    REQUESTED = "local list only"


class NoUseFound(Enum):
    CLEAR = "no use found"


class Arguments(argparse.Namespace):
    go: str | None = None
    host_list: bool = False
    json: bool = False


SshRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
REMOTE_COMMAND = "~/.claude/scripts/lib/py ~/.claude/scripts/disks/cleanup_old_folders.py --host-list"
MAC_UNREACHABLE = "mac unreachable"
PROC_ROOT = "/proc"
DIGEST = re.compile(r"[0-9a-f]{64}")
CONFIG_SECTION = re.compile(r'\[([a-zA-Z0-9-]+)(?:\.[a-zA-Z0-9.-]+|[ \t]+"(?:[^"\\]|\\.)*")?\]')
CONFIG_INCLUDE_PATH = re.compile(r"path(?=[ \t\r\v\f=;#]|$)", re.IGNORECASE)
GIT_READ_CONFIG = (
    ("core.excludesFile", ""),
    ("core.attributesFile", ""),
    ("core.hooksPath", os.devnull),
    ("core.fsmonitor", "false"),
)
GLOB_CHARACTERS = frozenset("*?[")
BARE_GIT_NAMES = frozenset(("head", "objects", "refs"))
GIT_KEEP_REASON = "holds Git work: the owner saves it, then removes the folder by hand"
UNREGISTERED_GIT = "not a registered linked worktree"
NO_LOCAL_ORIGIN = "no local source repository"
NO_CLEANUP_SETTING = removal_guard.Kept("list only: no setting names it")
LINUX_BUILD_ROOT = Path("/build")
ALTERNATE_SCAN_DEPTH = 7
ALTERNATE_SCAN_SECONDS = 30
SAVED_TARGET_PREFIX = ".saved-target-"
SAVED_TARGET_GLOB = SAVED_TARGET_PREFIX + "*"
SAVED_TARGET_PARENT_DEPTH = 3
MAC_OLD_WORKTREE = "worktrees/claude-prioritize-index-refresh"
CARGO_TARGET_FILES = frozenset((
    removal_guard.CARGO_TAG, ".rustc_info.json", ".rustdoc_fingerprint.json", ".claude-busy", ".lint-sweep-used",
))
CARGO_TARGET_DIRS = frozenset((
    "doc", "package", "tmp", "mend-analyzing", "mend-wrapper", "external-client", "rust-analyzer",
))
CARGO_PROFILE_DIRS = frozenset((".fingerprint", "build", "deps", "examples", "incremental"))
CARGO_PROFILE_FILES = frozenset((".cargo-lock", ".cargo-artifact-lock", ".cargo-build-lock"))
CARGO_INSTALLED_TOOLS = frozenset(("cargo-berth", "cargo-mend", "cargo-style", "cargo-tile"))
CARGO_LIBRARY = re.compile(r"lib[A-Za-z0-9_-]+\.(?:rlib|rmeta|a|so|dylib)")
CARGO_ARTIFACT = re.compile(r"[A-Za-z0-9_-]+(?:\.(?:wasm|exe))?")
CARGO_HASHED_DIR = re.compile(r"[A-Za-z0-9_.-]+-[a-f0-9]{8,}")
CARGO_FINGERPRINT = re.compile(r"(?:dep-)?(?:bin|lib|test|example|bench|build-script-build|run-build-script-build)(?:-[\w-]+)?(?:\.json)?")
RUSTDOC_PAGE = re.compile(r"(?:struct|enum|trait|type|fn|constant|static|macro|derive|attr|union|primitive|keyword)\.[\w-]+\.html")
RUSTDOC_ASSET = re.compile(r"[\w.-]+-[a-fA-F0-9]{8,}\.(?:js|css|svg|woff2|woff|ttf)")
CARGO_TARGET_TRIPLE = re.compile(r"[a-z0-9_]+(?:-[a-z0-9_]+){2,}")
MEND_FINGERPRINT = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?\+(?:nogit|[0-9a-f]+)\+\d+")
MEND_REVISION = re.compile(r"\d+-(?:before|after)-\d+")
MEND_DRIVER_CACHE = re.compile(r"driver-[0-9a-f]+")


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def saved_worktree_pattern(home: Path) -> Path:
    return home / "worktrees" / "**" / SAVED_TARGET_GLOB


def patterns(value: str, home: Path) -> tuple[Path, ...]:
    found: list[Path] = []
    for word in value.split(","):
        word = word.strip()
        path = home / word[2:] if word.startswith("~/") else Path(word)
        if (not path.is_absolute() or path == Path("/") or ".." in path.parts
                or (path != saved_worktree_pattern(home) and any(char in str(path.parent) for char in GLOB_CHARACTERS))
                or any(ord(char) < 32 for char in word)):
            raise ValueError(f"expected an absolute cleanup path or a final-component glob: {word!r}")
        found.append(path)
    return tuple(found)


def protected_build_name(name: str) -> bool:
    return name == "cargo" or name.startswith("old-")


def protected_build_entries(build: Path) -> tuple[Path, ...]:
    try:
        with os.scandir(build) as entries:
            return tuple(build / entry.name for entry in entries if protected_build_name(entry.name))
    except FileNotFoundError:
        return ()


def protected_mounts(home: Path, build: Path) -> tuple[Path, ...]:
    mounts: list[Path] = []
    for path in sorted((home / "rust/hanadocs", *protected_build_entries(build))):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"cannot mask a protected path that is not a real directory: {str(path)!r}")
        mounts.append(path)
    return tuple(mounts)


class ProtectedMaskChanged(Exception):
    """Stop the worker when a protected folder has no verified mask."""


@dataclass(frozen=True)
class ProtectedFolderMasks:
    build: Path
    mounts: frozenset[Path]

    def check(self) -> None:
        try:
            unmasked = sorted(set(protected_build_entries(self.build)) - self.mounts)
        except OSError as error:
            raise ProtectedMaskChanged(f"cannot check protected names: {error}") from error
        if unmasked:
            raise ProtectedMaskChanged(f"protected path has no mask: {str(unmasked[0])!r}")


class MaskCheck(Enum):
    NO_NAMESPACE = "the caller is outside the masked worker"


MASKS_IN_FORCE: ContextVar[ProtectedFolderMasks | MaskCheck] = ContextVar(
    "cleanup_protected_masks", default=MaskCheck.NO_NAMESPACE)


def check_protected_masks() -> None:
    masks = MASKS_IN_FORCE.get()
    if isinstance(masks, ProtectedFolderMasks):
        masks.check()


def check_before_git(event: str, arguments: tuple[object, ...]) -> None:
    if event == "subprocess.Popen":
        executable = arguments[0]
        if isinstance(executable, (str, bytes)) and os.path.basename(os.fsdecode(executable)) == "git":
            check_protected_masks()


def masked_cleanup_command(mounts: Sequence[Path], command: Sequence[str]) -> tuple[str, ...]:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise ValueError("cleanup on natedev requires bwrap")
    options: list[str] = []
    for path in mounts:
        options.extend(("--tmpfs", str(path), "--remount-ro", str(path)))
    return (bwrap, "--unshare-user", "--disable-userns", "--die-with-parent",
            "--bind", "/", "/", "--dev-bind", "/dev", "/dev", *options, "--", *command)


@dataclass(frozen=True)
class CleanupBounds:
    home: Path
    build: Path
    repositories: frozenset[str]

    def forbids_walk(self, path: Path) -> bool:
        if path.is_relative_to(self.home / "rust/hanadocs"):
            return True
        if not path.is_relative_to(LINUX_BUILD_ROOT):
            return False
        parts = path.relative_to(LINUX_BUILD_ROOT).parts
        return bool(parts) and protected_build_name(parts[0])

    def require_walk(self, path: Path) -> None:
        if self.forbids_walk(path):
            raise removal_guard.ChangedEntry(f"protected path: {path}")

    def storage_path(self, path: Path, *, follow_links: bool = True) -> Path:
        """Resolve a Git storage path without entering a protected folder."""
        if not path.is_absolute():
            raise ValueError(f"expected an absolute Git storage path: {path}")
        resolved = Path("/")
        remaining = list(reversed(path.parts[1:]))
        links = 0
        while remaining:
            part = remaining.pop()
            if part == "..":
                resolved = resolved.parent
                continue
            entry = resolved / part
            self.require_walk(entry)
            identity = entry.lstat()
            if stat.S_ISLNK(identity.st_mode):
                if not follow_links:
                    raise GitMetadataRefused(entry, "is a symlink")
                links += 1
                if links > 40:
                    raise ValueError(f"too many links in Git storage path: {path}")
                target = Path(os.readlink(entry))
                if target.is_absolute():
                    resolved = Path("/")
                remaining.extend(reversed(target.parts[1:] if target.is_absolute() else target.parts))
            else:
                resolved = entry
        return resolved

    def protects(self, path: Path) -> bool:
        if any(part.casefold() == ".git" for part in path.parts):
            return True
        if self.forbids_walk(path) or any(root.is_relative_to(path) for root in (
            self.home, self.build, LINUX_BUILD_ROOT, self.home / "rust/hanadocs",
        )):
            return True
        return path.is_relative_to(self.build) and any(self.is_model_folder(part) for part in (path, *path.parents))

    def is_model_folder(self, path: Path) -> bool:
        form = build_folders.parse_resolved_form(path, self.build)
        return isinstance(form, (
            build_folders.BuildCopy, build_folders.MainOutput, build_folders.CiOutput,
            build_folders.WorktreeOutput,
        )) and form.repo in self.repositories


@dataclass(frozen=True)
class CheckedCleanup:
    path: Path
    parent: int
    info: os.stat_result
    parent_identity: removal_guard.Identity
    mount: str
    bounds: CleanupBounds

    @property
    def name(self) -> str:
        return self.path.name

    def recheck(self) -> None:
        if removal_guard.identity(self.path.parent.stat()) != self.parent_identity:
            raise removal_guard.ChangedEntry("the parent folder changed")
        removal_guard.require_identity(self.parent, self.name, removal_guard.identity(self.info))


@dataclass(frozen=True)
class CheckedSidecar(CheckedCleanup):
    lease: build_folders.HeldFolderRemovalLease
    lock_stamp: build_folders.SidecarIdentity

    @override
    def recheck(self) -> None:
        super().recheck()
        require_absent_base(self.lease.folder, self.parent)
        held = os.fstat(self.lease.descriptor)
        if ((*removal_guard.identity(held), held.st_ctime_ns) != self.lock_stamp
                or not build_folder_lock.lock_current(self.lease.folder, self.parent, self.lease.descriptor)):
            raise removal_guard.ChangedEntry("worktree sidecar lock changed")


@dataclass(frozen=True)
class CleanupGuard:
    paths: frozenset[Path]
    bounds: CleanupBounds
    crosses_mount: Callable[[Path, removal_guard.Identity], bool]

    @contextmanager
    def check(self, path: Path, *, link: bool = False) -> Generator[CheckedCleanup]:
        if path not in self.paths or not path.is_absolute() or ".." in path.parts:
            raise removal_guard.Refused(removal_guard.RefusalReason.OUTSIDE_ROOT, str(path))
        if self.bounds.protects(path):
            raise removal_guard.Refused(removal_guard.RefusalReason.PROTECTED_PATH, str(path))
        with self._open_checked(path, link=link) as checked:
            yield checked

    @contextmanager
    def check_sidecar(self, path: Path, lease: build_folders.HeldFolderRemovalLease,
                      lock_stamp: build_folders.SidecarIdentity) -> Generator[CheckedSidecar]:
        base = lease.folder
        if (base not in self.paths or path.parent != base.parent or path.name not in worktree_sidecar_names(base)
                or self.bounds.protects(path)):
            raise removal_guard.Refused(removal_guard.RefusalReason.OUTSIDE_ROOT, str(path))
        with self._open_checked(path, file=True) as checked:
            sidecar = CheckedSidecar(path, checked.parent, checked.info, checked.parent_identity, checked.mount,
                                     checked.bounds, lease, lock_stamp)
            sidecar.recheck()
            yield sidecar

    @contextmanager
    def check_registration(self, checkout: git_files.Checkout) -> Generator[CheckedCleanup]:
        path = checkout.git_dir
        if not is_worktree_registration(checkout) or self.bounds.storage_path(path) != path:
            raise removal_guard.ChangedEntry("cannot verify the Git worktree registration")
        require_unlocked_registration(path)
        with self._open_checked(path) as checked:
            if removal_guard.identity(checked.info) != removal_guard.identity(checkout.git_dir_status):
                raise removal_guard.ChangedEntry("Git worktree registration changed")
            yield checked

    @contextmanager
    def _open_checked(self, path: Path, *, link: bool = False, file: bool = False) -> Generator[CheckedCleanup]:
        with build_folder_lock.folder_parent(path, Path("/"), create=False) as parent:
            anchor = removal_guard.identity(os.fstat(parent))
            mount = removal_guard.mount_id(parent)
            flags = removal_guard.DIRECTORY_FLAGS
            if link:
                flags = os.O_PATH | os.O_NOFOLLOW
            elif file:
                flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
            child = os.open(path.name, flags, dir_fd=parent)
            try:
                info = os.fstat(child)
                if link and not stat.S_ISLNK(info.st_mode):
                    raise removal_guard.ChangedEntry(f"expected a worktree link: {path}")
                if file and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                    raise removal_guard.ChangedEntry(f"expected a regular sidecar: {path}")
                key = removal_guard.identity(info)
                if (key[0] != anchor[0] or removal_guard.mount_id(child) != mount
                        or self.crosses_mount(path, key)):
                    raise removal_guard.Refused(removal_guard.RefusalReason.MOUNT, str(path))
            finally:
                os.close(child)
            checked = CheckedCleanup(path, parent, info, anchor, mount, self.bounds)
            checked.recheck()
            yield checked


class MacMountCheck(Enum):
    PER_FOLDER = "check a folder against the Mac mount table"


def platform_mounts() -> Callable[[], tuple[Path, ...]] | MacMountCheck:
    if sys.platform == "darwin":
        return MacMountCheck.PER_FOLDER
    if sys.platform == "linux":
        return lambda: tuple(mount.point for mount in build_folders.linux_mounts())
    raise removal_guard.Refused(removal_guard.RefusalReason.UNAVAILABLE_MOUNTS, sys.platform)


def read_cleanup_guard(paths: frozenset[Path], bounds: CleanupBounds,
                       *, mount_reader: Callable[[], tuple[Path, ...]] | None = None) -> CleanupGuard:
    if not bounds.build.is_absolute() or ".." in bounds.build.parts or bounds.build == Path("/"):
        raise removal_guard.Refused(removal_guard.RefusalReason.INVALID_ROOT, str(bounds.build))
    reader = mount_reader or platform_mounts()
    if isinstance(reader, MacMountCheck):
        return CleanupGuard(paths, bounds, lambda path, _identity: not build_folders.may_remove_folder_tree(path))
    mounts = removal_guard.mount_snapshot(lambda: tuple(path for path in reader() if not bounds.forbids_walk(path)))
    return CleanupGuard(paths, bounds, lambda _path, identity: identity in mounts.points or identity in mounts.ancestors)


class EntryStamp(NamedTuple):
    device: int
    inode: int
    mode: int
    size: int
    links: int
    uid: int
    gid: int
    modified_ns: int
    changed_ns: int


def entry_stamp(info: os.stat_result) -> EntryStamp:
    return EntryStamp(info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_nlink,
                      info.st_uid, info.st_gid, info.st_mtime_ns, info.st_ctime_ns)


def git_metadata(names: Sequence[str]) -> bool:
    if any(name.casefold() == ".git" for name in names):
        return True
    return BARE_GIT_NAMES.issubset(name.casefold() for name in names)


class LinkNotRead(Enum):
    NO_DESTINATION = "no link destination recorded"


@dataclass(frozen=True)
class CleanupEntry:
    stamp: EntryStamp
    children: tuple[str, ...]
    link: Path | LinkNotRead = LinkNotRead.NO_DESTINATION


class CleanupGit(Enum):
    NONE = "no Git metadata"
    CHECKOUT = "one Git checkout at the listed path"
    PROTECTED = "bare repository or nested Git metadata"


@dataclass(frozen=True)
class CleanupTree:
    fingerprint: str
    size: int
    git: CleanupGit
    lease_owners: tuple[Path, ...]
    source_fingerprint: str


class Snapshot(Enum):
    PREVIEW = "the list keeps fingerprints only"


def inspect_cleanup_tree(checked: CheckedCleanup,
                         entries: dict[Path, CleanupEntry] | Snapshot = Snapshot.PREVIEW) -> CleanupTree:
    """Measure the tree and record each entry without following links."""
    fingerprint = hashlib.sha256()
    source_fingerprint = hashlib.sha256()
    inodes: set[removal_guard.Identity] = set()
    lease_owners: list[Path] = []
    size = 0
    git = CleanupGit.NONE
    dot_git = checked.path / ".git"

    def visit(parent: int, name: str, path: Path) -> None:
        nonlocal size, git
        checked.bounds.require_walk(path)
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        stamp = entry_stamp(info)
        evidence = json.dumps((str(path.relative_to(checked.path)), stamp)).encode()
        fingerprint.update(evidence)
        in_git = path.is_relative_to(dot_git)
        if not in_git:
            source_fingerprint.update(evidence)
        key = removal_guard.identity(info)
        if key not in inodes:
            size += info.st_blocks * 512
            inodes.add(key)
        if path != checked.path and path.name.endswith(".lease.json"):
            owner = path.name.removesuffix(".lease.json")
            if owner not in ("", ".", ".."):
                lease_owners.append(path.with_name(owner))
        names: tuple[str, ...] = ()
        if stat.S_ISDIR(info.st_mode):
            if checked.bounds.is_model_folder(path):
                raise removal_guard.ChangedEntry(f"contains a model folder: {path}")
            descriptor = os.open(name, removal_guard.DIRECTORY_FLAGS, dir_fd=parent)
            try:
                if info.st_dev != checked.info.st_dev or removal_guard.mount_id(descriptor) != checked.mount:
                    raise removal_guard.Refused(removal_guard.RefusalReason.MOUNT, str(path))
                if entry_stamp(os.fstat(descriptor)) != stamp:
                    raise removal_guard.ChangedEntry("folder changed during assessment")
                names = tuple(sorted(os.listdir(descriptor)))
                if git is not CleanupGit.PROTECTED and not in_git and git_metadata(names):
                    bare = BARE_GIT_NAMES.issubset(name.casefold() for name in names)
                    git = (CleanupGit.CHECKOUT if path == checked.path and ".git" in names and not bare
                           else CleanupGit.PROTECTED)
                for child in names:
                    visit(descriptor, child, path / child)
                if entry_stamp(os.fstat(descriptor)) != stamp:
                    raise removal_guard.ChangedEntry("folder changed during assessment")
            finally:
                os.close(descriptor)
        if isinstance(entries, dict):
            link = (Path(os.readlink(name, dir_fd=parent))
                    if stat.S_ISLNK(info.st_mode) and checked.path.name.startswith(SAVED_TARGET_PREFIX)
                    else LinkNotRead.NO_DESTINATION)
            entries[path] = CleanupEntry(stamp, names, link)
        if entry_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) != stamp:
            raise removal_guard.ChangedEntry("entry changed during assessment")

    checked.recheck()
    visit(checked.parent, checked.name, checked.path)
    checked.recheck()
    return CleanupTree(fingerprint.hexdigest(), size, git, tuple(lease_owners), source_fingerprint.hexdigest())


def cargo_artifact(entry: CleanupEntry, path: Path, entries: dict[Path, CleanupEntry]) -> bool:
    name = path.name
    mode = entry.stamp.mode
    if CARGO_LIBRARY.fullmatch(name) or (name in CARGO_INSTALLED_TOOLS and mode & stat.S_IXUSR):
        return True
    artifact = name.removesuffix(".d")
    if not CARGO_ARTIFACT.fullmatch(artifact):
        return False
    paired = ((name.removesuffix(".wasm") + ".d", name + ".d") if artifact == name
              else (artifact, artifact + ".wasm"))
    return any((companion := entries.get(path.with_name(match))) is not None and stat.S_ISREG(companion.stamp.mode)
               for match in paired)


def cargo_generated_file(path: Path, folder: Path) -> bool:
    name = path.name
    if name in CARGO_TARGET_FILES | CARGO_PROFILE_FILES | CARGO_INSTALLED_TOOLS or CARGO_LIBRARY.fullmatch(name):
        return True
    group = folder.name
    if group == "doc":
        directories = path.parent.relative_to(folder).parts
        return (name in ("index.html", "all.html", "settings.html", "help.html", "sidebar-items.js",
                         "crates.js", "search-index.js", "src-files.js")
                or bool(RUSTDOC_PAGE.fullmatch(name))
                or (directories[:1] == ("src",) and name.endswith(".rs.html"))
                or (directories[:1] == ("static.files",) and bool(RUSTDOC_ASSET.fullmatch(name))))
    if group == ".fingerprint":
        return name == "invoked.timestamp" or bool(CARGO_FINGERPRINT.fullmatch(name))
    if group == "build":
        return name in ("output", "root-output", "stderr", "invoked.timestamp")
    if group == "incremental":
        return (name in ("dep-graph.bin", "query-cache.bin", "work-products.bin")
                or bool(re.fullmatch(r"[a-z0-9]+\.o", name)))
    return False


def cargo_generated_directory(path: Path, folder: Path) -> bool:
    parts = path.relative_to(folder).parts
    if path.name.endswith(".d"):
        return False
    if folder.name == "doc":
        return (bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", path.name))
                or (len(parts) == 1 and path.name in ("static.files", "trait.impl", "type.impl", "search-index")))
    if folder.name in (".fingerprint", "build", "incremental"):
        if not CARGO_HASHED_DIR.fullmatch(parts[0]):
            return False
        if len(parts) == 1:
            return True
        return (len(parts) == 2 and (folder.name == "build" and path.name == "out"
                or folder.name == "incremental" and bool(re.fullmatch(r"s-[a-z0-9-]+", path.name))))
    if folder.name == "mend-wrapper" and MEND_FINGERPRINT.fullmatch(parts[0]):
        return (len(parts) == 1 or (parts[1] == "independent-copy-v1"
                and (len(parts) == 2 or len(parts) == 3 and bool(MEND_REVISION.fullmatch(path.name)))))
    return False


def cargo_generated_tree(entries: dict[Path, CleanupEntry], folder: Path, target: Path, home: Path) -> bool:
    """Check each entry in a known Cargo output folder."""
    pending = [folder]
    while pending:
        parent = pending.pop()
        names = entries[parent].children
        for name in names:
            path = parent / name
            entry = entries[path]
            mode = entry.stamp.mode
            if stat.S_ISDIR(mode):
                if cargo_generated_file(path, folder) or not cargo_generated_directory(path, folder):
                    return False
                pending.append(path)
                continue
            if known_mend_alias(path.relative_to(target), entry.link, home):
                continue
            if not stat.S_ISREG(mode):
                return False
            if folder.name != "doc" and cargo_artifact(entry, path, entries):
                continue
            if name not in CARGO_INSTALLED_TOOLS and cargo_generated_file(path, folder):
                continue
            return False
    return True


def cargo_profile(entries: dict[Path, CleanupEntry], folder: Path, target: Path, home: Path) -> bool:
    names = entries[folder].children
    for name in names:
        path = folder / name
        entry = entries[path]
        if name in CARGO_PROFILE_DIRS:
            if not stat.S_ISDIR(entry.stamp.mode) or not cargo_generated_tree(entries, path, target, home):
                return False
            continue
        if stat.S_ISREG(entry.stamp.mode) and (
                name in CARGO_PROFILE_FILES or cargo_artifact(entry, path, entries)):
            continue
        return False
    return True


def cargo_target(entries: dict[Path, CleanupEntry], folder: Path, target: Path, home: Path) -> bool:
    for name in entries[folder].children:
        path = folder / name
        entry = entries[path]
        if stat.S_ISREG(entry.stamp.mode) and (name in CARGO_TARGET_FILES
                or (name == "nextest-hang-wrapper" and entry.stamp.mode & stat.S_IXUSR)):
            continue
        if not stat.S_ISDIR(entry.stamp.mode):
            return False
        if name in CARGO_TARGET_FILES or name == "nextest-hang-wrapper":
            return False
        if name in CARGO_TARGET_DIRS:
            if cargo_generated_tree(entries, path, target, home):
                continue
        elif name in ("debug", "release") or ".cargo-lock" in entry.children:
            if cargo_profile(entries, path, target, home):
                continue
        elif folder == target and CARGO_TARGET_TRIPLE.fullmatch(name) and cargo_target(entries, path, target, home):
            continue
        return False
    return True


def known_mend_alias(relative: Path, destination: Path | LinkNotRead, home: Path) -> bool:
    parts = relative.parts
    if (not isinstance(destination, Path) or len(parts) not in (3, 5)
            or parts[0] != "mend-wrapper" or parts[-1] != "cargo-mend" or not MEND_FINGERPRINT.fullmatch(parts[1])):
        return False
    if len(parts) == 3:
        version = parts[1].split("+", 1)[0]
        install = home / ".local/state/tool-based-ui-hana-migration" / f"mend-{version}"
        return destination == install / "bin/cargo-mend"
    cache = home / ".cache/cargo-mend"
    if (parts[2] != "independent-copy-v1" or not MEND_REVISION.fullmatch(parts[3])
            or not destination.is_relative_to(cache)):
        return False
    driver, *alias = destination.relative_to(cache).parts or ("",)
    return bool(MEND_DRIVER_CACHE.fullmatch(driver)) and tuple(alias) == parts


def require_saved_cargo_output(checked: CheckedCleanup, entries: dict[Path, CleanupEntry]) -> None:
    path = checked.path
    if not path.name.startswith(SAVED_TARGET_PREFIX):
        return
    target = path / "target"
    if (entries[path].children != ("target",) or not stat.S_ISDIR(entries[target].stamp.mode)
            or not cargo_target(entries, target, target, checked.bounds.home)):
        raise removal_guard.ChangedEntry("review only: saved target contents are not verified as Cargo output")


def is_worktree_registration(checkout: git_files.Checkout) -> bool:
    return (checkout.git_dir.parent == checkout.common_dir / "worktrees"
            and not checkout.common_dir.is_relative_to(checkout.toplevel))


def require_unlocked_registration(path: Path) -> None:
    try:
        _ = (path / "locked").lstat()
    except FileNotFoundError:
        return
    raise GitCleanupRefused("git worktree locked")


def registered_worktree(checkout: git_files.Checkout) -> bool:
    if not is_worktree_registration(checkout):
        return False
    require_unlocked_registration(checkout.git_dir)
    from scripts.worktree.worktrees import linked_trees

    try:
        with isolated_git_config():
            return any(tree.path == checkout.toplevel for tree in linked_trees(checkout.common_dir))
    except worktree_records.Refused as error:
        raise removal_guard.ChangedEntry(str(error)) from error


@dataclass(frozen=True)
class LinkedWorktree:
    registration_fingerprint: str


def review_registration(checked: CheckedCleanup, entries: dict[Path, CleanupEntry]) -> LinkedWorktree:
    tree = inspect_cleanup_tree(checked, entries)
    if tree.git is not CleanupGit.NONE:
        raise removal_guard.ChangedEntry("Git worktree registration contains a repository")
    require_unlocked_registration(checked.path)
    # The save creates a temporary directory here. It changes the root's timestamps.
    return LinkedWorktree(digest(tuple(
        (str(path.relative_to(checked.path)), entry.stamp, entry.children)
        for path, entry in entries.items() if path != checked.path
    )))


@dataclass(frozen=True)
class LocalClone:
    origin: Path
    git_dir: Path
    identity: removal_guard.Identity


@dataclass(frozen=True)
class GitCleanupReview:
    checkout: git_files.Checkout
    kind: LinkedWorktree | LocalClone | removal_guard.Kept
    head: str
    status: str
    refs: str
    unpushed: int
    stash: str
    stash_reflog: tuple[str, ...]

    def directory_identities(self) -> tuple[removal_guard.Identity, removal_guard.Identity]:
        return (removal_guard.identity(self.checkout.git_dir_status),
                removal_guard.identity(self.checkout.common_dir_status))

    def fingerprint(self, tree: CleanupTree) -> str:
        checkout = self.checkout
        source = ((str(self.kind.origin), str(self.kind.git_dir), self.kind.identity)
                  if isinstance(self.kind, LocalClone) else ())
        registration = self.kind.registration_fingerprint if isinstance(self.kind, LinkedWorktree) else ""
        return digest((tree.fingerprint, str(checkout.git_dir), str(checkout.common_dir),
                       self.directory_identities(), source, registration,
                       self.head, self.status, self.refs, self.stash, self.stash_reflog))

    def summary(self, *, eligible: bool) -> str:
        dirty = "yes" if any(record and not record.startswith("# ") for record in self.status.split("\0")) else "no"
        stash = "yes" if self.stash else "no"
        state = f"dirty: {dirty}, unpushed: {self.unpushed}, stash: {stash}"
        if isinstance(self.kind, LocalClone):
            origin = str(self.kind.origin)
            state = f"clone of {origin!r}, {state}"
            if eligible:
                state += f"; saved to {origin!r} refs/rescue on --go"
        elif isinstance(self.kind, LinkedWorktree):
            state += f"; registration: {str(self.checkout.git_dir)!r}"
            if eligible:
                state += "; saved to refs/rescue on --go"
        return state


class NoGitReview(Enum):
    ABSENT = "the row has no Git review"


class GitCleanupRefused(removal_guard.ChangedEntry):
    """Keep the exact reason in the list."""


class GitMetadataRefused(GitCleanupRefused):
    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"review only: Git metadata {str(path)!r} {reason}")


WRONG_GIT_WORKTREE = "review only: Git worktree root does not match the row path"


def config_has_include(lines: Iterable[str]) -> bool:
    """Check Git config keys as text. Do not read any include path."""
    section = ""
    continued = quoted = False
    for physical in lines:
        line = physical.rstrip("\r\n")
        if not continued:
            line = line.lstrip(" \t\r\v\f")
            while line.startswith("["):
                match = CONFIG_SECTION.match(line)
                if match is None:
                    raise ValueError("cannot read Git config section")
                section = match[1].lower()
                line = line[match.end():].lstrip(" \t\r\v\f")
            if not line or line.startswith(("#", ";")):
                continue
            if section in ("include", "includeif") and CONFIG_INCLUDE_PATH.match(line):
                return True
            _, equals, line = line.partition("=")
            if not equals:
                continue
        continued = False
        characters = iter(line)
        for char in characters:
            if char == "\\":
                if not next(characters, ""):
                    continued = True
            elif char == '"':
                quoted = not quoted
            elif not quoted and char in "#;":
                break
        if not continued:
            quoted = False
    return False


def require_safe_git_config(common_dir: Path, git_dir: Path, worktree: Path, bounds: CleanupBounds) -> None:
    require_safe_git_tree(bounds.storage_path(common_dir, follow_links=False))
    configs: dict[Path, str] = {}
    for directory in dict.fromkeys((common_dir, git_dir)):
        for name in ("config", "config.worktree"):
            path = directory / name
            try:
                resolved = bounds.storage_path(path, follow_links=False)
            except FileNotFoundError:
                if os.path.lexists(path):
                    raise
                continue
            descriptor = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8-sig", errors="surrogateescape", newline="\n") as config:
                if not stat.S_ISREG(os.fstat(config.fileno()).st_mode):
                    raise ValueError(f"expected a regular Git config: {path}")
                content = config.read()
                if config_has_include(content.splitlines()):
                    raise GitCleanupRefused("git config include")
                configs[path] = content
    for path, content in configs.items():
        with isolated_git_config():
            values = subprocess.run(
                ("git", *git_read_options(), "config", "--file", "-", "--no-includes", "--null", "--list"),
                cwd="/", input=content.encode("utf-8", "surrogateescape"), capture_output=True, check=False)
        if values.returncode:
            raise GitCleanupRefused("review only: cannot read local Git config")
        for item in values.stdout.split(b"\0"):
            key, separator, value = item.partition(b"\n")
            if key.startswith(b"include.") or (key.startswith(b"includeif.") and key.endswith(b".path")):
                raise GitCleanupRefused("git config include")
            if key == b"core.worktree":
                configured = git_dir / os.fsdecode(value)
                if (not value or Path(os.path.normpath(configured)) != worktree
                        or bounds.storage_path(configured) != worktree):
                    raise GitCleanupRefused(WRONG_GIT_WORKTREE)
                continue
            if separator:
                check_git_config_path(os.fsdecode(value), (worktree, path.parent), bounds)


def check_git_config_path(value: str, origins: tuple[Path, Path], bounds: CleanupBounds) -> None:
    if "/" not in value or ("://" in value and not value.startswith("file://")):
        return
    path = Path(value.removeprefix("file://"))
    if value.startswith("~/"):
        path = bounds.home / value[2:]
    for origin in origins:
        candidate = origin / path
        if bounds.forbids_walk(candidate) or bounds.forbids_walk(Path(os.path.normpath(candidate))):
            raise GitCleanupRefused(f"review only: local Git config names a protected path: {value}")
        try:
            _ = bounds.storage_path(candidate)
        except FileNotFoundError:
            continue
        except removal_guard.ChangedEntry as error:
            raise GitCleanupRefused(f"review only: local Git config names a protected path: {value}") from error


def git_read_options() -> tuple[str, ...]:
    return tuple(argument for key, value in GIT_READ_CONFIG for argument in ("-c", f"{key}={value}"))


@contextmanager
def isolated_git_config() -> Generator[None]:
    if "GIT_CONFIG_COUNT" in os.environ or "GIT_CONFIG_PARAMETERS" in os.environ:
        raise GitCleanupRefused("the environment overrides the Git config")
    # The worktree listing and rescue helpers launch their own Git commands.
    settings = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_COUNT": str(len(GIT_READ_CONFIG))}
    for index, (key, value) in enumerate(GIT_READ_CONFIG):
        settings[f"GIT_CONFIG_KEY_{index}"] = key
        settings[f"GIT_CONFIG_VALUE_{index}"] = value
    previous = {name: os.environ[name] for name in settings if name in os.environ}
    os.environ.update(settings)
    try:
        yield
    finally:
        for name in settings:
            if name in previous:
                os.environ[name] = previous[name]
            else:
                del os.environ[name]


def cleanup_git(path: Path, *arguments: str, accepted: tuple[int, ...] = (0,)) -> str:
    with isolated_git_config():
        result = worktree_records.git(path, "--no-optional-locks", *git_read_options(), *arguments, check=False)
    if result.returncode not in accepted:
        raise removal_guard.ChangedEntry(failure_detail(result))
    return result.stdout


def local_clone(checkout: git_files.Checkout, bounds: CleanupBounds) -> LocalClone | removal_guard.Kept:
    path = checkout.toplevel
    raw = cleanup_git(path, "config", "--local", "--no-includes", "--null", "--get-all", "remote.origin.url",
                      accepted=(0, 1))
    url, separator, extra = raw.partition("\0")
    if not separator or extra or not url or ":" in url:
        return removal_guard.Kept(NO_LOCAL_ORIGIN)
    try:
        origin = bounds.storage_path(path / Path(url).expanduser())
        if not origin.is_dir() or origin.is_relative_to(path):
            return removal_guard.Kept(NO_LOCAL_ORIGIN)
        try:
            source = checked_git_files(origin, bounds)
        except git_files.NotACheckout:
            require_safe_git_config(origin, origin, origin, bounds)
            check_object_stores(origin / "objects", bounds)
            actual = cleanup_git(origin, f"--git-dir={origin}", "rev-parse", "--absolute-git-dir").strip()
            if Path(actual) != origin:
                return removal_guard.Kept(NO_LOCAL_ORIGIN)
            git_dir = origin
        else:
            git_dir = source.common_dir
            require_safe_git_config(git_dir, source.git_dir, source.toplevel, bounds)
            check_object_stores(git_dir / "objects", bounds)
        if git_dir.is_relative_to(path):
            return removal_guard.Kept(NO_LOCAL_ORIGIN)
        return LocalClone(origin, git_dir, removal_guard.identity(git_dir.stat()))
    except GitCleanupRefused:
        raise
    except (OSError, RuntimeError, ValueError):
        return removal_guard.Kept(NO_LOCAL_ORIGIN)


def history_not_in_origin(path: Path, head: str, refs: str, clone: LocalClone) -> int:
    # Use real source refs when their objects are available in the clone.
    origin_tips = cleanup_git(clone.git_dir, f"--git-dir={clone.git_dir}", "for-each-ref",
                             "--format=%(objectname)", "refs/heads", "refs/tags", "refs/rescue").splitlines()
    local_tips = {head, *(line.partition(" ")[0] for line in refs.splitlines()
                          if line.partition(" ")[2].startswith("refs/heads/"))}
    revisions = (
        "^" + tip for tip in sorted(set(origin_tips))
        if cleanup_git(path, "cat-file", "-t", tip, accepted=(0, 128)).strip() in ("commit", "tag")
    )
    return int(cleanup_git(path, "rev-list", "--count", *sorted(local_tips), *revisions))


def checkout_for_git_status(checked: CheckedCleanup) -> git_files.Checkout:
    checked.recheck()
    path = checked.path
    checkout = checked_git_files(path, checked.bounds)
    require_safe_git_config(checkout.common_dir, checkout.git_dir, path, checked.bounds)
    overrides = cleanup_git(path, "rev-parse", "--local-env-vars").splitlines()
    if any(name in os.environ for name in overrides):
        raise removal_guard.ChangedEntry("the environment overrides the Git repository or index")
    check_object_stores(checkout.common_dir / "objects", checked.bounds)
    toplevel = cleanup_git(path, "rev-parse", "--show-toplevel").removesuffix("\n")
    if checkout.toplevel != path or Path(toplevel) != path:
        raise GitCleanupRefused(WRONG_GIT_WORKTREE)
    return checkout


def review_cleanup_git(checked: CheckedCleanup, tree: CleanupTree, guard: CleanupGuard) -> GitCleanupReview:
    """Read the Git state of the checkout at the exact path."""
    if tree.git is not CleanupGit.CHECKOUT:
        raise removal_guard.ChangedEntry(GIT_KEEP_REASON)
    checkout = checkout_for_git_status(checked)
    path = checked.path
    marker = (path / ".git").lstat()
    kind: LinkedWorktree | LocalClone | removal_guard.Kept
    if stat.S_ISREG(marker.st_mode) and registered_worktree(checkout):
        with guard.check_registration(checkout) as registration:
            kind = review_registration(registration, {})
    elif stat.S_ISDIR(marker.st_mode) and checkout.git_dir == checkout.common_dir == path / ".git":
        from scripts.worktree.worktrees import linked_trees

        with isolated_git_config():
            has_linked_trees = os.path.lexists(checkout.git_dir / "worktrees") and any(
                worktree.path != path for worktree in linked_trees(checkout.git_dir))
        if has_linked_trees:
            kind = removal_guard.Kept("the clone has registered linked worktrees")
        else:
            kind = local_clone(checkout, checked.bounds)
    else:
        kind = removal_guard.Kept(UNREGISTERED_GIT)
    status = cleanup_git(path, f"--work-tree={path}", "status", "--porcelain=v2", "--branch", "--untracked-files=all", "-z")
    head = cleanup_git(path, "rev-parse", "HEAD").strip()
    refs = cleanup_git(path, "for-each-ref", "--format=%(objectname) %(refname)")
    stash = next((ref.partition(" ")[0] for ref in refs.splitlines() if ref.endswith(" refs/stash")), "")
    stash_reflog = tuple(cleanup_git(path, "reflog", "show", "--format=%H", "refs/stash").splitlines()) if stash else ()
    unpushed = (history_not_in_origin(path, head, refs, kind) if isinstance(kind, LocalClone)
                else int(cleanup_git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")))
    checked.recheck()
    return GitCleanupReview(checkout, kind, head, status, refs, unpushed, stash, stash_reflog)


def borrower_repositories(bounds: CleanupBounds, deadline: float) -> Iterator[Path]:
    """Read checkout and bare repository paths within the three scan roots."""
    roots = ((bounds.home / "rust", 1), (bounds.home / "worktrees", ALTERNATE_SCAN_DEPTH),
             (bounds.build, ALTERNATE_SCAN_DEPTH))
    for root, depth in roots:
        if bounds.forbids_walk(root):
            continue
        try:
            root = bounds.storage_path(root)
        except FileNotFoundError:
            continue
        pending = [(root, depth)]
        while pending:
            folder, remaining = pending.pop()
            if time.monotonic() >= deadline:
                raise ValueError(f"Git object scan timed out at {folder}")
            names: set[str] = set()
            children: set[str] = set()
            with os.scandir(folder) as entries:
                for entry in entries:
                    if bounds.forbids_walk(folder / entry.name):
                        continue
                    names.add(entry.name)
                    if entry.name != ".git" and entry.is_dir(follow_symlinks=False):
                        children.add(entry.name)
            bare = "HEAD" in names and {"refs", "objects"} <= children
            if ".git" in names or bare:
                yield folder
            if remaining:
                if bare:
                    children.difference_update(("objects", "refs", "logs", "worktrees"))
                pending.extend((folder / name, remaining - 1) for name in sorted(children, reverse=True))


def checked_git_files(path: Path, bounds: CleanupBounds) -> git_files.Checkout:
    """Check the row's Git directory and its named registration without following links."""
    marker = path / ".git"
    bounds.require_walk(marker)
    try:
        marker_info = marker.lstat()
    except FileNotFoundError as error:
        raise git_files.NotACheckout(f"{path}: no .git entry") from error
    if stat.S_ISDIR(marker_info.st_mode):
        for name in ("gitdir", "commondir"):
            pointer = marker / name
            if os.path.lexists(pointer):
                check_git_pointer(pointer, marker, bounds)
        return git_files.Checkout(path, marker, marker, marker_info, marker_info)
    pointer = read_git_pointer(marker)
    if not pointer.startswith("gitdir:") or not pointer.removeprefix("gitdir:").strip():
        raise ValueError(f"invalid Git directory pointer: {str(marker)!r}")
    named = path / pointer.removeprefix("gitdir:").strip()
    registration = Path(os.path.normpath(named))
    if registration.parent.name != "worktrees" or registration.is_relative_to(path):
        raise GitMetadataRefused(marker, "points outside the row and its named registration")
    git_dir = bounds.storage_path(named, follow_links=False)
    common = git_dir.parent.parent
    git_info = git_dir.lstat()
    common_info = common.lstat()
    if not stat.S_ISDIR(git_info.st_mode) or not stat.S_ISDIR(common_info.st_mode):
        raise GitMetadataRefused(marker, "does not name a registration directory")
    check_git_pointer(git_dir / "gitdir", marker, bounds)
    check_git_pointer(git_dir / "commondir", common, bounds)
    return git_files.Checkout(path, git_dir, common, git_info, common_info)


def check_git_pointer(pointer: Path, expected: Path, bounds: CleanupBounds) -> None:
    named = pointer.parent / read_git_pointer(pointer)
    if (Path(os.path.normpath(named)) != expected
            or bounds.storage_path(named, follow_links=False) != expected):
        raise GitMetadataRefused(pointer, "points outside the row and its named registration")


def read_git_pointer(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise GitMetadataRefused(path, "is not a regular file")
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding="utf-8") as source:
        if not os.path.samestat(info, os.fstat(source.fileno())):
            raise GitMetadataRefused(path, "changed during the check")
        value = source.read(65536).strip()
        if not value or len(value) >= 65535:
            raise ValueError(f"invalid Git pointer: {path}")
        return value


def require_safe_git_tree(root: Path) -> None:
    """Check Git metadata entries without opening files or following links."""
    def visit(directory: int, path: Path) -> None:
        for name in os.listdir(directory):
            entry = path / name
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, removal_guard.DIRECTORY_FLAGS, dir_fd=directory)
                try:
                    if not os.path.samestat(info, os.fstat(child)):
                        raise GitMetadataRefused(entry, "changed during the check")
                    visit(child, entry)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(info.st_mode):
                raise GitMetadataRefused(entry, "is not a regular file or directory")

    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise GitMetadataRefused(root, "is not a directory")
    directory = os.open(root, removal_guard.DIRECTORY_FLAGS)
    try:
        if not os.path.samestat(info, os.fstat(directory)):
            raise GitMetadataRefused(root, "changed during the check")
        visit(directory, root)
    finally:
        os.close(directory)


def alternate_paths(objects: Path, bounds: CleanupBounds) -> Iterator[Path]:
    """Read the alternate links before any Git command can follow them."""
    entry = objects / "info/alternates"
    try:
        path = bounds.storage_path(entry, follow_links=False)
    except FileNotFoundError:
        if os.path.lexists(entry):
            raise
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError(f"expected a regular alternates file: {path}")
        while line := source.readline(65536):
            if len(line) >= 65536 or b"\0" in line:
                raise ValueError(f"invalid alternate in {path}")
            name = line.removesuffix(b"\n")
            if not name or name.startswith(b"#"):
                continue
            if name.startswith(b'"'):
                try:
                    value = cast(object, ast.literal_eval("b" + name.decode("ascii")))
                except (SyntaxError, ValueError) as error:
                    raise ValueError(f"invalid alternate in {path}") from error
                if not isinstance(value, bytes):
                    raise ValueError(f"invalid alternate in {path}")
                name = value
            yield objects / os.fsdecode(name)


def object_stores(start: Path, bounds: CleanupBounds, deadline: float) -> Iterator[Path]:
    pending = [start]
    visited: set[Path] = set()
    while pending:
        if time.monotonic() >= deadline:
            raise ValueError(f"Git object scan timed out at {start}")
        objects = bounds.storage_path(pending.pop(), follow_links=False)
        if objects in visited:
            continue
        if not objects.is_dir():
            raise ValueError(f"expected a Git object directory: {objects}")
        visited.add(objects)
        yield objects
        pending.extend(alternate_paths(objects, bounds))


def check_object_stores(objects: Path, bounds: CleanupBounds) -> None:
    for store in object_stores(objects, bounds, time.monotonic() + ALTERNATE_SCAN_SECONDS):
        if store != objects:
            require_safe_git_tree(store)


def require_no_borrowers(path: Path, clone: LocalClone, bounds: CleanupBounds,
                         candidates: Iterable[Path]) -> None:
    """Check local Git object stores for alternates that reach this clone."""
    deadline = time.monotonic() + ALTERNATE_SCAN_SECONDS
    checked: set[Path] = set()
    roots = chain((clone.git_dir,), candidates, borrower_repositories(bounds, deadline))
    for repository in roots:
        if repository == path or bounds.forbids_walk(repository):
            continue
        try:
            with build_folder_lock.folder_parent(repository, Path("/"), create=False) as parent:
                info = os.stat(repository.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue
        repository = bounds.storage_path(repository)
        try:
            git_dir = checked_git_files(repository, bounds).common_dir
        except git_files.NotACheckout:
            git_dir = repository
            if not os.path.lexists(git_dir / "objects") or not os.path.lexists(git_dir / "HEAD"):
                continue
        if git_dir in checked:
            continue
        checked.add(git_dir)
        for objects in object_stores(git_dir / "objects", bounds, deadline):
            if objects.is_relative_to(path):
                raise removal_guard.ChangedEntry(f"Git object storage is used by {repository}: {objects}")


def expand_places(pattern: Path, bounds: CleanupBounds) -> Iterator[Path]:
    recursive = pattern == saved_worktree_pattern(bounds.home)
    root = pattern.parent.parent if recursive else pattern.parent
    glob = any(char in pattern.name for char in GLOB_CHARACTERS)

    def search(folder: Path, directory: int, remaining: int) -> Iterator[Path]:
        names = os.listdir(directory) if glob else (pattern.name,)
        for name in names:
            path = folder / name
            if bounds.protects(path):
                continue
            if not glob or fnmatch.fnmatchcase(name, pattern.name):
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not glob or stat.S_ISDIR(info.st_mode):
                    yield path
                continue
            if remaining:
                try:
                    child = os.open(name, removal_guard.DIRECTORY_FLAGS, dir_fd=directory)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                try:
                    yield from search(path, child, remaining - 1)
                finally:
                    os.close(child)

    if bounds.forbids_walk(root):
        return
    try:
        with build_folder_lock.folder_parent(root / pattern.name, Path("/"), create=False) as directory:
            yield from search(root, directory, SAVED_TARGET_PARENT_DEPTH if recursive else 0)
    except FileNotFoundError:
        return


def collect(machine: Machine, settings: conf.ConfigRead, bounds: CleanupBounds) -> tuple[Candidate, ...]:
    """Expand the configured places and skip missing or protected paths."""
    reason = settings.value(f"cleanup.list_only_reason.{machine.value}", str,
                            recover_invalid=False, host_override=conf.HostOverride.IGNORED)
    assignments: dict[Path, Candidate] = {}
    expansions: dict[Path, tuple[Path, ...]] = {}

    def expanded(pattern: Path) -> tuple[Path, ...]:
        if pattern not in expansions:
            expansions[pattern] = tuple(expand_places(pattern, bounds))
        return expansions[pattern]

    for key, decision in (("dead", RemovalAssessment.PENDING),
                          ("list_only", removal_guard.Kept(f"kept: {reason}, list only"))):
        configured = settings.value(f"cleanup.{key}.{machine.value}", lambda value: patterns(value, bounds.home),
                                    recover_invalid=False, host_override=conf.HostOverride.IGNORED)
        for pattern in configured:
            if bounds.protects(pattern):
                continue
            for path in expanded(pattern):
                assignments[path] = Candidate(path, decision)
    if machine is Machine.NATEDEV:
        for pattern in (bounds.build / "p-*", bounds.home / "rust" / SAVED_TARGET_GLOB,
                        saved_worktree_pattern(bounds.home)):
            for path in expanded(pattern):
                if path not in assignments:
                    assignments[path] = Candidate(path, NO_CLEANUP_SETTING)
        for path, place in top_worktrees(bounds):
            configured = assignments.get(path)
            if configured is None:
                assignments[path] = Candidate(path, NO_CLEANUP_SETTING, place)
            elif place is CleanupPlace.LINK:
                assignments[path] = Candidate(path, configured.decision, place)
    return tuple(assignments[path] for path in sorted(assignments))


def top_worktrees(bounds: CleanupBounds) -> Iterator[tuple[Path, CleanupPlace]]:
    root = bounds.home / "worktrees"
    model = bounds.repositories | {"claude", "hana", "cargo-liner"}
    try:
        with build_folder_lock.folder_parent(root / "entry", Path("/"), create=False) as parent:
            names = frozenset(os.listdir(parent))
            orphans: set[str] = set()
            for name in names:
                path = root / name
                if bounds.protects(path):
                    continue
                base = name.removesuffix(".lease.json") if name.endswith(".lease.json") else name.removesuffix(".lock")
                if base != name:
                    if base not in names and base not in model and base not in ("", ".", ".."):
                        orphans.add(base)
                    continue
                try:
                    entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(entry.st_mode):
                    yield path, CleanupPlace.LINK
                elif stat.S_ISDIR(entry.st_mode) and name not in model:
                    yield path, CleanupPlace.REVIEW_FOLDER
            for name in orphans:
                yield root / name, CleanupPlace.ORPHAN_SIDECARS
    except FileNotFoundError:
        return


def process_name(pid: int, *, fallback: str = "<command unavailable>") -> str:
    try:
        return (Path(PROC_ROOT) / str(pid) / "comm").read_text(errors="replace").strip()
    except OSError:
        return fallback


def incomplete_scan_reason(unreadable: Sequence[tuple[int, str]]) -> str:
    names = ", ".join(f"pid {pid} {removal_guard.single_line(name).replace(chr(9), ' ')}"
                      for pid, name in unreadable)
    return f"open file scan is incomplete: {names}" if names else "open file scan is incomplete"


def process_ended(process: Path) -> bool:
    try:
        _ = process.lstat()
    except (FileNotFoundError, ProcessLookupError):
        return True
    except OSError:
        pass
    return False


@dataclass(frozen=True)
class CleanupProcessReview:
    paths: tuple[tuple[str, int, str], ...] = ()
    path_failure: removal_guard.Kept | NoUseFound = NoUseFound.CLEAR
    pwd_users: tuple[tuple[Path, int], ...] = ()
    pwd_failure: removal_guard.Kept | NoUseFound = NoUseFound.CLEAR
    unreadable_processes: tuple[tuple[int, str], ...] = ()

    def all_uses(self) -> Iterator[tuple[str, int, str]]:
        yield from self.paths
        for path, pid in self.pwd_users:
            yield str(path), pid, "uses it in PWD"


class ProcessCheckRequest(TypedDict):
    kind: Literal["process"]
    pid: int
    paths: tuple[str, ...]
    links: bool
    descriptors: tuple[int, ...]


class ProcessCheckResult(TypedDict):
    kind: Literal["process-result"]
    paths: tuple[tuple[str, int, str], ...]
    path_failure: str
    pwd_users: tuple[tuple[str, int], ...]
    pwd_failure: str
    unreadable: tuple[tuple[int, str], ...]


class WorkerReview(TypedDict):
    kind: Literal["review"]
    rows: list[WorkerRowRecord]
    unreadable: tuple[tuple[int, str], ...]


class WorkerRunResult(TypedDict):
    kind: Literal["applied"]
    status: int


class WorkerRemovalResult(TypedDict):
    kind: Literal["removal-result"]
    output: str


def message_object(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("expected a cleanup message object")
    return cast(dict[str, object], raw)


def message_array(raw: object) -> list[object]:
    if not isinstance(raw, list):
        raise ValueError("expected a cleanup message array")
    return cast(list[object], raw)


def message_rows(raw: object, columns: tuple[type[str] | type[int], ...]) -> Iterator[list[object]]:
    for item in message_array(raw):
        fields = message_array(item)
        if len(fields) != len(columns) or any(type(value) is not kind for value, kind in zip(fields, columns)):
            raise ValueError("invalid cleanup message fields")
        yield fields


def unreadable_records(raw: object) -> tuple[tuple[int, str], ...]:
    return tuple((cast(int, pid), cast(str, name)) for pid, name in message_rows(raw, (int, str)))


@dataclass(frozen=True)
class WorkerChannel:
    incoming: TextIO
    outgoing: TextIO

    def send(self, message: object) -> None:
        _ = self.outgoing.write(json.dumps(message) + "\n")
        self.outgoing.flush()

    def receive(self) -> dict[str, object]:
        line = self.incoming.readline()
        if not line:
            raise ValueError("the masked cleanup worker stopped before it sent a result")
        return message_object(cast(object, json.loads(line)))

    def check_process_use(self, paths: Sequence[Path], check_links: bool, fds: frozenset[int]) -> CleanupProcessReview:
        request: ProcessCheckRequest = {
            "kind": "process", "pid": os.getpid(), "paths": tuple(map(str, paths)),
            "links": check_links, "descriptors": tuple(sorted(fds)),
        }
        self.send(request)
        fields = self.receive()
        path_failure = fields.get("path_failure")
        pwd_failure = fields.get("pwd_failure")
        if fields.get("kind") != "process-result" or not isinstance(path_failure, str) or not isinstance(pwd_failure, str):
            raise ValueError("invalid cleanup process result")
        return CleanupProcessReview(
            tuple((cast(str, path), cast(int, pid), cast(str, action))
                  for path, pid, action in message_rows(fields.get("paths"), (str, int, str))),
            removal_guard.Kept(path_failure) if path_failure else NoUseFound.CLEAR,
            tuple((Path(cast(str, path)), cast(int, pid))
                  for path, pid in message_rows(fields.get("pwd_users"), (str, int))),
            removal_guard.Kept(pwd_failure) if pwd_failure else NoUseFound.CLEAR,
            unreadable_records(fields.get("unreadable")),
        )


class LocalProcessCheck(Enum):
    HERE = "read the local process table"


MASKED_WORKER_CHANNEL: ContextVar[WorkerChannel | LocalProcessCheck] = ContextVar(
    "masked_cleanup_worker_channel", default=LocalProcessCheck.HERE)


def report_removal(output: str) -> None:
    channel = MASKED_WORKER_CHANNEL.get()
    if isinstance(channel, WorkerChannel):
        result: WorkerRemovalResult = {"kind": "removal-result", "output": output}
        channel.send(result)
    else:
        print(output, flush=True)


def report_partial_removal(path: Path, error: Exception, deleted: Sequence[Path]) -> None:
    lines = [f"partial removal of {str(path)!r}: {removal_guard.single_line(error)}"]
    lines.extend(f"deleted {str(entry)!r}" for entry in deleted)
    report_removal("\n".join(lines))


def process_uses(paths: Sequence[Path], *, check_links: bool = False,
                 ignored_fds: frozenset[int] = tmp_sweep.NO_IGNORED_FDS) -> CleanupProcessReview:
    channel = MASKED_WORKER_CHANNEL.get()
    if isinstance(channel, WorkerChannel):
        return channel.check_process_use(paths, check_links, ignored_fds)
    return read_process_use(paths, check_links=check_links, ignored_fds=ignored_fds)


def read_process_use(paths: Sequence[Path], *, check_links: bool,
                     ignored_fds: frozenset[int] = tmp_sweep.NO_IGNORED_FDS,
                     worker_fds: tmp_sweep.WorkerRemovalDescriptors | tmp_sweep.NoWorkerDescriptors =
                     tmp_sweep.NoWorkerDescriptors.NONE) -> CleanupProcessReview:
    if not (paths or check_links):
        return CleanupProcessReview()
    prefix = tuple(str(path) for path in paths)
    if isinstance(worker_fds, tmp_sweep.WorkerRemovalDescriptors):
        uses = tmp_sweep.process_paths(PROC_ROOT, prefix, require_readable_user_processes=True, worker_fds=worker_fds,
                                      stop_after_first_use=True)
    else:
        uses = tmp_sweep.process_paths(PROC_ROOT, prefix, require_readable_user_processes=True,
                                      ignored_self_fds=ignored_fds, stop_after_first_use=True)
    use_lsof = (type(uses) is tmp_sweep.ProcessUsesUnavailable
                and isinstance(worker_fds, tmp_sweep.NoWorkerDescriptors))
    pwd_scan = read_worktree_pwd() if not use_lsof or check_links else WorktreePwdScan()
    if use_lsof:
        if not paths:
            uses = tmp_sweep.ProcessUsesKnown([])
        elif ignored_fds:
            uses = tmp_sweep.lsof_paths(prefix, require_complete=True, ignored_self_fds=ignored_fds, same_user_only=True)
        else:
            uses = tmp_sweep.lsof_paths(prefix, require_complete=True, same_user_only=True)
    incomplete = dict(pwd_scan.unreadable)
    ended: set[int] = set()
    path_failure: removal_guard.Kept | NoUseFound = NoUseFound.CLEAR
    if isinstance(uses, tmp_sweep.ProcessUsesKnown):
        hidden = {item.pid: item.comm for item in uses.kernel_hidden}
        for pid in {*uses.unreadable_cwd_pids, *uses.unreadable_process_pids, *hidden} - incomplete.keys():
            process = Path(PROC_ROOT) / str(pid)
            if process_ended(process):
                ended.add(pid)
            else:
                incomplete[pid] = process_name(pid, fallback=hidden.get(pid, "<command unavailable>"))
    unreadable = tuple(sorted(incomplete.items()))
    pwd_failure = pwd_scan.table_failure
    if unreadable or not isinstance(uses, tmp_sweep.ProcessUsesKnown):
        path_failure = removal_guard.Kept(incomplete_scan_reason(unreadable))
        if not use_lsof or unreadable:
            pwd_failure = path_failure
    elif not use_lsof:
        path_failure = pwd_failure
    pwd_users = tuple((path, pid) for path, pid in pwd_scan.users if pid not in ended)
    found = [item for item in uses.paths if item[1] not in ended] if isinstance(uses, tmp_sweep.ProcessUsesKnown) else []
    return CleanupProcessReview(tuple(found), path_failure, pwd_users, pwd_failure, unreadable)


def answer_process_check(channel: WorkerChannel, request: dict[str, object]) -> None:
    pid = request.get("pid")
    links = request.get("links")
    paths = message_array(request.get("paths"))
    fds = message_array(request.get("descriptors"))
    if (type(pid) is not int or pid <= 0 or not isinstance(links, bool)
            or any(not isinstance(path, str) or not path.startswith("/") for path in paths)
            or any(type(fd) is not int or fd <= 2 for fd in fds)):
        raise ValueError("invalid cleanup process request")
    uses = read_process_use(
        tuple(map(Path, cast(list[str], paths))), check_links=links,
        worker_fds=tmp_sweep.WorkerRemovalDescriptors(pid, frozenset(cast(list[int], fds))),
    )
    result: ProcessCheckResult = {
        "kind": "process-result", "paths": uses.paths, "pwd_users": tuple((str(path), pid) for path, pid in uses.pwd_users),
        "path_failure": uses.path_failure.reason if isinstance(uses.path_failure, removal_guard.Kept) else "",
        "pwd_failure": uses.pwd_failure.reason if isinstance(uses.pwd_failure, removal_guard.Kept) else "",
        "unreadable": uses.unreadable_processes,
    }
    channel.send(result)


def keep_for_process(path: Path, uses: CleanupProcessReview, *,
                     sidecars: tuple[Path, ...] = (),
                     other_folders: tuple[Path, ...] = ()) -> removal_guard.Kept | NoUseFound:
    if isinstance(uses.path_failure, removal_guard.Kept):
        return uses.path_failure
    folders = tuple(map(str, (path, *other_folders)))
    sidecar_names = frozenset(map(str, sidecars))
    for name, pid, action in sorted(uses.all_uses()):
        if name in sidecar_names or any(name == folder or name.startswith(folder + "/") for folder in folders):
            return removal_guard.Kept(f"pid {pid} {action}")
    return NoUseFound.CLEAR


@dataclass(frozen=True)
class WorktreePwdScan:
    users: tuple[tuple[Path, int], ...] = ()
    unreadable: tuple[tuple[int, str], ...] = ()
    table_failure: removal_guard.Kept | NoUseFound = NoUseFound.CLEAR


def read_worktree_pwd() -> WorktreePwdScan:
    users: list[tuple[Path, int]] = []
    unreadable: dict[int, str] = {}
    uid = str(os.geteuid())
    try:
        processes = tuple(Path(PROC_ROOT).iterdir())
    except OSError:
        return WorktreePwdScan(table_failure=removal_guard.Kept("cannot read the process PWD list"))
    for process in processes:
        if not process.name.isdecimal():
            continue
        pid = int(process.name)
        try:
            status = (process / "status").read_text(errors="replace").splitlines()
            uids = next(line.split()[1:3] for line in status if line.startswith("Uid:"))
            if len(uids) != 2:
                raise ValueError("cannot read the process user")
            if uid not in uids:
                continue
            state = next(line.split()[1] for line in status if line.startswith("State:"))
            if state in ("Z", "X"):
                continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, StopIteration, IndexError, ValueError):
            if not process_ended(process):
                unreadable[pid] = process_name(pid)
            continue
        try:
            fields = (process / "environ").read_bytes().split(b"\0")
        except OSError:
            if not process_ended(process):
                unreadable[pid] = process_name(pid)
            continue
        for field in fields:
            if field.startswith(b"PWD="):
                path = Path(os.fsdecode(field[4:]))
                if path.is_absolute():
                    users.append((path, pid))
    return WorktreePwdScan(tuple(users), tuple(unreadable.items()))


def worktree_staff() -> staff.StaffSnapshot | removal_guard.Kept:
    try:
        return staff.snapshot()
    except (OSError, staff.StaffRefused) as error:
        return removal_guard.Kept(f"cannot read staff rows: {removal_guard.single_line(error)}")


def check_worktree_link(checked: CheckedCleanup, uses: CleanupProcessReview,
                        membership: staff.StaffSnapshot | removal_guard.Kept
                        ) -> WouldRemove | removal_guard.Kept:
    if isinstance(uses.pwd_failure, removal_guard.Kept):
        return uses.pwd_failure
    try:
        target = Path(os.readlink(checked.name, dir_fd=checked.parent))
        _ = checked.bounds.storage_path(checked.path.parent / target)
    except FileNotFoundError:
        pass
    else:
        pids = [pid for path, pid in uses.pwd_users if path.is_relative_to(checked.path)]
        if pids:
            pid = min(pids)
            return removal_guard.Kept(f"pid {pid} {removal_guard.single_line(process_name(pid))} uses the link in PWD")
        if isinstance(membership, removal_guard.Kept):
            return membership
        for row in membership.all_rows:
            location = row.worktree
            if (isinstance(location, staff.PresentWorktree) and location.machine == Machine.NATEDEV.value
                    and location.path == checked.path):
                return removal_guard.Kept(f"staff row {removal_guard.single_line(row.name)} names the link")
    checked.recheck()
    return WouldRemove(digest((CleanupPlace.LINK.value, checked.parent_identity, entry_stamp(checked.info))))


def list_only(reason: removal_guard.Kept, outcome: WouldRemove | removal_guard.Kept) -> removal_guard.Kept:
    if isinstance(outcome, removal_guard.Kept):
        return removal_guard.Kept(f"{reason.reason}; {outcome.reason}")
    return reason


def review_worktree_folder(checked: CheckedCleanup, uses: CleanupProcessReview,
                           membership: staff.StaffSnapshot | removal_guard.Kept) -> Row:
    path = checked.path
    for name, pid, action in sorted(uses.all_uses(), key=lambda use: (use[2] != tmp_sweep.WORKS_IN, use)):
        if Path(name).is_relative_to(path):
            reason = f"pid {pid} {action}"
            if isinstance(membership, staff.StaffSnapshot):
                row = membership.by_worktree(Machine.NATEDEV.value, Path(name))
                reason += f"; staff row {removal_guard.single_line(row.name)}" if row is not None else "; no staff row found"
            else:
                reason += f"; {membership.reason}"
            return Row(path, None, removal_guard.Kept(reason), place=CleanupPlace.REVIEW_FOLDER)
    reason = "review only"
    if isinstance(uses.path_failure, removal_guard.Kept):
        reason += f"; {uses.path_failure.reason}"
    details = worktree_git_state(checked)
    return Row(path, None, removal_guard.Kept(reason), details, place=CleanupPlace.REVIEW_FOLDER)


def worktree_git_state(checked: CheckedCleanup) -> str:
    path = checked.path
    if not os.path.lexists(path / ".git"):
        return "no Git checkout"
    try:
        _ = checkout_for_git_status(checked)
        status = cleanup_git(path, f"--work-tree={path}", "status", "--porcelain=v2", "--branch", "--no-renames",
                             "--untracked-files=all", "-z")
        items = status.split("\0")
        dirty = sum(bool(item) and not item.startswith("# ") for item in items)
        branch = next(item.removeprefix("# branch.head ") for item in items if item.startswith("# branch.head "))
        unpushed = (0 if "# branch.oid (initial)" in items
                    else int(cleanup_git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")))
        checked.recheck()
        return f"dirty: {dirty}, unpushed: {unpushed}, branch: {branch!r}"
    except (OSError, ValueError, StopIteration, removal_guard.Refused) as error:
        return f"Git state unavailable: {table_text(removal_guard.single_line(error))}"


class LeasePidCheck(Enum):
    MATCH_START = "match the lease PID and start time"
    ANY_LIVE_PID = "keep the place while the lease PID is alive"


def keep_for_lease(path: Path, *, check: LeasePidCheck = LeasePidCheck.MATCH_START) -> removal_guard.Kept | NoUseFound:
    try:
        fields = copies.clone_lease_fields(path)
    except FileNotFoundError:
        return NoUseFound.CLEAR
    except (OSError, ValueError):
        return removal_guard.Kept("cannot read the folder lease")
    pid = fields.get("pid")
    start = fields.get("process_start")
    if type(pid) is not int or pid <= 0:
        return removal_guard.Kept("cannot read the lease PID")
    current = process_start.process_start(pid)
    if isinstance(current, process_start.Unreadable):
        return removal_guard.Kept(f"cannot verify lease PID {pid}")
    if isinstance(current, process_start.Started):
        if check is LeasePidCheck.ANY_LIVE_PID:
            return removal_guard.Kept(f"lease PID {pid} is still running")
        if not isinstance(start, str) or not start:
            return removal_guard.Kept(f"cannot verify the start time for lease PID {pid}")
        if current.start == start:
            return removal_guard.Kept(f"lease PID {pid} is still running")
    return NoUseFound.CLEAR


def keep_for_leases(path: Path, lease_owners: Sequence[Path], check: LeasePidCheck) -> removal_guard.Kept | NoUseFound:
    if isinstance(reason := keep_for_lease(path, check=check), removal_guard.Kept):
        return reason
    for owner in lease_owners:
        if isinstance(reason := keep_for_lease(owner), removal_guard.Kept):
            return reason
    return NoUseFound.CLEAR


def worktree_sidecar_names(path: Path) -> tuple[str, str]:
    return build_folders.lease_path(path).name, path.name + ".lock"


def related_sidecars(path: Path, machine: Machine, bounds: CleanupBounds) -> tuple[Path, ...]:
    if ((machine is Machine.MAC and path == bounds.home / MAC_OLD_WORKTREE)
            or (machine is Machine.NATEDEV and path.parent in (bounds.build, LINUX_BUILD_ROOT)
                and path.name.startswith("p-"))):
        return tuple(path.with_name(name) for name in worktree_sidecar_names(path))
    return ()


@dataclass(frozen=True)
class WorktreeSidecars:
    stamps: dict[str, build_folders.SidecarIdentity]
    size: int

    def bind(self, fingerprint: str) -> str:
        return digest((fingerprint, self.stamps)) if self.stamps else fingerprint


def inspect_sidecars(parent: int, guard: CleanupGuard, related: tuple[Path, ...]) -> WorktreeSidecars:
    stamps: dict[str, build_folders.SidecarIdentity] = {}
    size = 0
    device = os.fstat(parent).st_dev
    for path in related:
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            continue
        key = removal_guard.identity(info)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != device
                or guard.crosses_mount(path, key)):
            raise removal_guard.ChangedEntry(f"cannot verify worktree sidecar: {path}")
        stamps[path.name] = (*key, info.st_ctime_ns)
        size += info.st_blocks * 512
    return WorktreeSidecars(stamps, size)


def confirm_sidecars(checked: CheckedCleanup, guard: CleanupGuard, related: tuple[Path, ...],
                     lease: build_folders.FolderRemovalLease, approved: WorktreeSidecars) -> None:
    if (inspect_sidecars(checked.parent, guard, related) != approved or (approved.stamps and (
            not isinstance(lease, build_folders.HeldFolderRemovalLease)
            or lease.sidecar_identities() != approved.stamps))):
        raise removal_guard.ChangedEntry("worktree sidecars changed or their lock is unavailable")
    if isinstance(reason := keep_for_lease(checked.path, check=LeasePidCheck.ANY_LIVE_PID), removal_guard.Kept):
        raise removal_guard.ChangedEntry(reason.reason)


def finish_sidecars(path: Path, guard: CleanupGuard, lease: build_folders.HeldFolderRemovalLease,
                    approved: WorktreeSidecars) -> None:
    try:
        lock_stamp = approved.stamps.get(worktree_sidecar_names(path)[1])
        if lock_stamp is None or lease.sidecar_identities() != approved.stamps:
            raise removal_guard.ChangedEntry("worktree sidecars changed")
        if isinstance(reason := keep_for_lease(path, check=LeasePidCheck.ANY_LIVE_PID), removal_guard.Kept):
            raise removal_guard.ChangedEntry(reason.reason)
        with ExitStack() as stack:
            targets: list[removal_guard.CleanupTarget] = []
            for name in worktree_sidecar_names(path):
                if name not in approved.stamps:
                    continue
                place = path.with_name(name)
                checked = stack.enter_context(guard.check_sidecar(place, lease, lock_stamp))
                stamp = entry_stamp(checked.info)
                if (stamp.device, stamp.inode, stamp.changed_ns) != approved.stamps[name]:
                    raise removal_guard.ChangedEntry(f"worktree sidecar changed: {place}")
                approval = removal_guard.ApprovedCleanup({place: CleanupEntry(stamp, ())},
                                                         removal_guard.CleanupRemoval.PLAIN)
                targets.append(removal_guard.CleanupTarget(checked, approval))
            first, *other = targets
            descriptors = (lease.descriptor, *(target.checked.parent for target in targets))
            sidecars = tuple(target.checked.path for target in targets)
            remove_reviewed_entries(first.checked, first.approval, *other, before_delete=lambda: require_no_process_use(
                path, descriptors, sidecars=sidecars))
    except ProtectedMaskChanged as error:
        report_partial_removal(path, error, (path,))
        raise
    except (OSError, removal_guard.Refused) as error:
        deleted = error.deleted if isinstance(error, removal_guard.PartialCleanup) else ()
        raise removal_guard.PartialCleanup(error, (path, *deleted)) from error


def require_absent_base(path: Path, parent: int) -> None:
    try:
        _ = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise removal_guard.ChangedEntry("the sidecars have a folder or another entry beside them")


def orphan_sidecars(path: Path, parent: int, guard: CleanupGuard) -> WorktreeSidecars:
    require_absent_base(path, parent)
    related = tuple(path.with_name(name) for name in worktree_sidecar_names(path))
    sidecars = inspect_sidecars(parent, guard, related)
    if not sidecars.stamps:
        raise removal_guard.ChangedEntry("the orphan sidecars are gone")
    return sidecars


def review_orphan(path: Path, guard: CleanupGuard, uses: CleanupProcessReview) -> Row:
    with build_folder_lock.folder_parent(path, Path("/"), create=False) as parent:
        sidecars = orphan_sidecars(path, parent, guard)
        related = tuple(path.with_name(name) for name in sidecars.stamps)
        use = keep_for_process(path, uses, sidecars=related)
        with build_folders.folder_removal_lease(path, wait=False, root=Path("/")) as lease:
            lock = worktree_sidecar_names(path)[1]
            if isinstance(use, removal_guard.Kept):
                outcome = use
            elif isinstance(lease, build_folders.RefusedRemovalLease):
                outcome = removal_guard.Kept("a folder lock is held or unreadable")
            elif lock in sidecars.stamps and (
                    not isinstance(lease, build_folders.HeldFolderRemovalLease)
                    or lease.sidecar_identities() != sidecars.stamps):
                outcome = removal_guard.Kept("worktree sidecars changed")
            elif isinstance(reason := keep_for_lease(path, check=LeasePidCheck.ANY_LIVE_PID), removal_guard.Kept):
                outcome = reason
            elif sidecars != orphan_sidecars(path, parent, guard):
                outcome = removal_guard.Kept("worktree sidecars changed")
            else:
                outcome = NO_CLEANUP_SETTING
    if outcome != NO_CLEANUP_SETTING:
        outcome = list_only(NO_CLEANUP_SETTING, outcome)
    return Row(path, sidecars.size, outcome, sidecars=tuple(sidecars.stamps), place=CleanupPlace.ORPHAN_SIDECARS)


def assess(candidates: Sequence[Candidate], guard: CleanupGuard, machine: Machine) -> HostList:
    """Measure each guarded candidate and check process use and leases."""
    prefixes: list[Path] = []
    for candidate in candidates:
        if candidate.place is CleanupPlace.ORPHAN_SIDECARS:
            prefixes.extend(candidate.path.with_name(name) for name in worktree_sidecar_names(candidate.path))
        elif candidate.decision is RemovalAssessment.PENDING or candidate.place is CleanupPlace.REVIEW_FOLDER:
            prefixes.extend((candidate.path, *related_sidecars(candidate.path, machine, guard.bounds)))
    uses = process_uses(prefixes, check_links=any(candidate.place is CleanupPlace.LINK for candidate in candidates))
    membership = (worktree_staff()
                  if any(candidate.place in (CleanupPlace.LINK, CleanupPlace.REVIEW_FOLDER) for candidate in candidates)
                  else staff.StaffSnapshot(()))
    rows: list[Row] = []
    for candidate in candidates:
        path = candidate.path
        related = related_sidecars(path, machine, guard.bounds)
        pid_check = LeasePidCheck.ANY_LIVE_PID if related else LeasePidCheck.MATCH_START
        size = None
        names: tuple[str, ...] = ()
        git: GitCleanupReview | NoGitReview = NoGitReview.ABSENT
        try:
            if candidate.place is CleanupPlace.ORPHAN_SIDECARS:
                rows.append(review_orphan(path, guard, uses))
                continue
            with guard.check(path, link=candidate.place is CleanupPlace.LINK) as checked:
                if candidate.place is CleanupPlace.LINK:
                    size = checked.info.st_blocks * 512
                    link = check_worktree_link(checked, uses, membership)
                    if isinstance(candidate.decision, removal_guard.Kept):
                        link = list_only(candidate.decision, link)
                    rows.append(Row(path, size, link, place=candidate.place))
                    continue
                if candidate.place is CleanupPlace.REVIEW_FOLDER:
                    row = review_worktree_folder(checked, uses, membership)
                    rows.append(Row(path, row.size, list_only(NO_CLEANUP_SETTING, row.outcome), row.git,
                                    place=candidate.place))
                    continue
                if path.name.startswith(SAVED_TARGET_PREFIX):
                    entries: dict[Path, CleanupEntry] = {}
                    tree = inspect_cleanup_tree(checked, entries)
                    size = tree.size
                    require_saved_cargo_output(checked, entries)
                else:
                    tree = inspect_cleanup_tree(checked)
                size = tree.size
                sidecars = inspect_sidecars(checked.parent, guard, related)
                names = tuple(sidecars.stamps)
                size += sidecars.size
                if candidate.decision is RemovalAssessment.PENDING and tree.git is CleanupGit.CHECKOUT:
                    try:
                        git = review_cleanup_git(checked, tree, guard)
                    except GitCleanupRefused:
                        raise
                    except (OSError, ValueError, worktree_records.Refused) as error:
                        raise removal_guard.ChangedEntry(f"{GIT_KEEP_REASON}. {error}") from error
                outcome: WouldRemove | removal_guard.Kept
                if isinstance(candidate.decision, removal_guard.Kept):
                    outcome = candidate.decision
                elif tree.git is CleanupGit.PROTECTED:
                    outcome = removal_guard.Kept(GIT_KEEP_REASON)
                elif isinstance(git, GitCleanupReview) and isinstance(git.kind, removal_guard.Kept):
                    outcome = git.kind
                elif any(other.path != path and other.path.is_relative_to(path) for other in candidates):
                    outcome = removal_guard.Kept("contains another cleanup candidate")
                elif isinstance(reason := keep_for_process(path, uses, sidecars=related), removal_guard.Kept):
                    outcome = reason
                else:
                    with build_folders.folder_removal_lease(path, children=True, wait=False, root=Path("/")) as lease:
                        if isinstance(lease, build_folders.RefusedRemovalLease):
                            outcome = removal_guard.Kept("a folder lock is held or unreadable")
                        elif isinstance(reason := keep_for_leases(path, tree.lease_owners, pid_check), removal_guard.Kept):
                            outcome = reason
                        elif sidecars.stamps and (not isinstance(lease, build_folders.HeldFolderRemovalLease)
                                                  or lease.sidecar_identities() != sidecars.stamps):
                            outcome = removal_guard.Kept("worktree sidecars changed or their lock is unavailable")
                        else:
                            if isinstance(git, GitCleanupReview) and isinstance(git.kind, LocalClone):
                                require_no_borrowers(path, git.kind, guard.bounds, (other.path for other in candidates))
                            fingerprint = git.fingerprint(tree) if isinstance(git, GitCleanupReview) else tree.fingerprint
                            outcome = WouldRemove(sidecars.bind(fingerprint))
        except (OSError, removal_guard.Refused, RuntimeError, ValueError) as error:
            outcome = removal_guard.Kept(removal_guard.single_line(error))
            if isinstance(candidate.decision, removal_guard.Kept):
                outcome = list_only(candidate.decision, outcome)
        summary = git.summary(eligible=isinstance(outcome, WouldRemove)) if isinstance(git, GitCleanupReview) else ""
        rows.append(Row(path, size, outcome, summary, names, candidate.place))
    return HostList(machine, tuple(rows), uses.unreadable_processes)


def require_no_process_use(path: Path, fds: Sequence[int], *, sidecars: tuple[Path, ...] = (),
                           other_folders: tuple[Path, ...] = (), check_links: bool = False) -> None:
    uses = process_uses((path, *sidecars, *other_folders), check_links=check_links, ignored_fds=frozenset(fds))
    if check_links and isinstance(uses.pwd_failure, removal_guard.Kept):
        raise removal_guard.ChangedEntry(uses.pwd_failure.reason)
    if isinstance(reason := keep_for_process(path, uses, sidecars=sidecars, other_folders=other_folders),
                  removal_guard.Kept):
        raise removal_guard.ChangedEntry(reason.reason)


def require_no_live_leases(path: Path, tree: CleanupTree, check: LeasePidCheck) -> None:
    if isinstance(reason := keep_for_leases(path, tree.lease_owners, check), removal_guard.Kept):
        raise removal_guard.ChangedEntry(reason.reason)


def source_git(clone: LocalClone, *arguments: str, accepted: tuple[int, ...] = (0,)) -> str:
    if removal_guard.identity(clone.git_dir.stat()) != clone.identity:
        raise removal_guard.ChangedEntry("the source repository changed")
    return cleanup_git(clone.git_dir, f"--git-dir={clone.git_dir}", *arguments, accepted=accepted)


def origin_ref(clone: LocalClone, ref: str) -> str:
    refs = source_git(clone, "for-each-ref", "--format=%(objectname) %(refname)", ref)
    return next((line.partition(" ")[0] for line in refs.splitlines() if line.partition(" ")[2] == ref), "")


def source_reaches_commit(clone: LocalClone, commit: str) -> bool:
    kind = source_git(clone, "cat-file", "-t", commit, accepted=(0, 128)).strip()
    return kind == "commit" and bool(source_git(clone, "for-each-ref", "--contains", commit, "--count=1"))


def save_clone(path: Path, clone: LocalClone, refs: str, saved: git_rescue.RescueResult,
               bounds: CleanupBounds) -> dict[str, str]:
    prefix = f"refs/rescue/{path.name}/"
    pending: dict[str, str] = {}
    snapshots: dict[str, str] = {}
    for line in refs.splitlines():
        commit, _, ref = line.partition(" ")
        if ref.startswith("refs/heads/") and not source_reaches_commit(clone, commit):
            pending[prefix + ref.removeprefix("refs/heads/")] = commit
        elif ref.startswith("refs/rescue/") and not source_reaches_commit(clone, commit):
            snapshots[prefix + f"preserved-{commit}"] = commit
    if isinstance(saved, git_rescue.Saved):
        snapshots[prefix + f"snapshot-{saved.commit}"] = saved.commit
    for ref, commit in snapshots.items():
        if ref in pending and pending[ref] != commit:
            raise removal_guard.ChangedEntry(f"the rescue ref conflicts with a local branch: {ref}")
        pending[ref] = commit
    if not pending:
        return {}

    origin = str(clone.origin)
    rewrites = cleanup_git(path, "config", "--null", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$",
                           accepted=(0, 1))
    for rule in rewrites.split("\0"):
        _, separator, prefix_value = rule.partition("\n")
        if separator and origin.startswith(prefix_value):
            raise removal_guard.ChangedEntry("Git rewrites the local source path")
    if local_clone(checked_git_files(path, bounds), bounds) != clone:
        raise removal_guard.ChangedEntry("the source repository changed")
    create: dict[str, str] = {}
    for ref, commit in pending.items():
        previous = origin_ref(clone, ref)
        if previous and previous != commit:
            raise removal_guard.ChangedEntry(f"the source already has a different rescue ref: {ref}")
        if not previous:
            create[ref] = commit
    if create:
        # The empty lease permits creation only. Custom Git refs can otherwise be overwritten.
        _ = cleanup_git(path, "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
                        "-c", "protocol.file.allow=always", "push", "--atomic", "--no-follow-tags",
                        "--recurse-submodules=no", *(f"--force-with-lease={ref}:" for ref in create),
                        "--", origin, *(f"{commit}:{ref}" for ref, commit in create.items()))
    return pending


def verify_clone_save(clone: LocalClone, published: dict[str, str]) -> None:
    for ref, commit in published.items():
        if origin_ref(clone, ref) != commit or source_git(clone, "cat-file", "-t", commit).strip() != "commit":
            raise removal_guard.ChangedEntry(f"cannot verify source rescue ref: {ref}")


def remove_reviewed_entries(checked: removal_guard.CheckedCleanupPath, approval: removal_guard.ApprovedCleanup,
                            *additional: removal_guard.CleanupTarget,
                            before_delete: Callable[[], None] | None = None) -> None:
    check_protected_masks()

    def final_check() -> None:
        if before_delete is not None:
            before_delete()
        check_protected_masks()

    removal_guard.remove_cleanup(checked, approval, *additional, before_delete=final_check)


def remove_git(checked: CheckedCleanup, tree: CleanupTree, reviewed: GitCleanupReview, fds: Sequence[int],
               guard: CleanupGuard, *, sidecars: tuple[Path, ...],
               recheck_leases: Callable[[CheckedCleanup, CleanupGuard, CleanupTree], None]) -> None:
    path = checked.path
    kind = reviewed.kind
    if isinstance(kind, removal_guard.Kept):
        raise removal_guard.ChangedEntry(kind.reason)
    if isinstance(kind, LocalClone):
        require_no_borrowers(path, kind, guard.bounds, guard.paths)
    else:
        require_unlocked_registration(reviewed.checkout.git_dir)

    def recheck_git_config() -> None:
        checked.recheck()
        checkout = checked_git_files(path, guard.bounds)
        if checkout != reviewed.checkout or (
                removal_guard.identity(checkout.git_dir_status), removal_guard.identity(checkout.common_dir_status)
        ) != reviewed.directory_identities():
            raise removal_guard.ChangedEntry("Git metadata directories changed")
        require_safe_git_config(checkout.common_dir, checkout.git_dir, path, guard.bounds)

    recheck_git_config()
    try:
        with isolated_git_config():
            saved = git_rescue.rescue(path, "cleanup", retain_local_refs=True, pass_fds=fds)
    except git_rescue.RescueError as error:
        raise removal_guard.ChangedEntry(f"Git save failed: {error}") from error
    published: dict[str, str] = {}
    if isinstance(kind, LocalClone):
        recheck_git_config()
        require_safe_git_config(kind.git_dir, kind.git_dir, kind.origin, guard.bounds)
        published = save_clone(path, kind, reviewed.refs, saved, guard.bounds)

    fresh = read_cleanup_guard(guard.paths, guard.bounds)
    with fresh.check(path) as current:
        entries: dict[Path, CleanupEntry] = {}
        latest = inspect_cleanup_tree(current, entries)
        recheck_leases(current, fresh, latest)
        now = review_cleanup_git(current, latest, fresh)
        new_ref = f"{saved.commit} {saved.ref}" if isinstance(saved, git_rescue.Saved) else ""
        remaining_refs = "".join(line for line in now.refs.splitlines(keepends=True) if line.rstrip("\n") != new_ref)
        same_files = (latest.fingerprint == tree.fingerprint if isinstance(kind, LinkedWorktree)
                      else latest.source_fingerprint == tree.source_fingerprint)
        if (not same_files or now.kind != kind or now.head != reviewed.head or now.status != reviewed.status
                or remaining_refs != reviewed.refs or now.checkout != reviewed.checkout
                or now.directory_identities() != reviewed.directory_identities()
                or now.stash_reflog != reviewed.stash_reflog):
            raise removal_guard.ChangedEntry("Git state or contents changed during the save")
        checked.recheck()
        if isinstance(kind, LocalClone):
            require_no_borrowers(path, kind, guard.bounds, guard.paths)
            require_safe_git_config(kind.git_dir, kind.git_dir, kind.origin, guard.bounds)
            verify_clone_save(kind, published)
        approval = removal_guard.ApprovedCleanup(entries, removal_guard.CleanupRemoval.SAVED_CHECKOUT)
        if isinstance(kind, LinkedWorktree):
            with fresh.check_registration(now.checkout) as registration:
                registration_entries: dict[Path, CleanupEntry] = {}
                if review_registration(registration, registration_entries) != kind:
                    raise removal_guard.ChangedEntry("Git worktree registration changed")
                metadata_approval = removal_guard.ApprovedCleanup(
                    registration_entries, removal_guard.CleanupRemoval.PLAIN)
                remove_reviewed_entries(
                    current, approval, removal_guard.CleanupTarget(registration, metadata_approval),
                    before_delete=lambda: require_no_process_use(
                        path, (*fds, current.parent, registration.parent), sidecars=sidecars,
                        other_folders=(registration.path,)))
        else:
            remove_reviewed_entries(
                current, approval, before_delete=lambda: require_no_process_use(
                    path, (*fds, current.parent), sidecars=sidecars))
    if isinstance(kind, LocalClone):
        for ref in published:
            report_removal(f"saved {str(path)!r} to {kind.origin} {ref}")
    elif isinstance(saved, git_rescue.Saved):
        report_removal(f"saved {str(path)!r} to {saved.ref}")


def remove_worktree_link(path: Path, guard: CleanupGuard, approval: WouldRemove) -> None:
    with guard.check(path, link=True) as checked:
        outcome = check_worktree_link(checked, process_uses((), check_links=True), worktree_staff())
        if isinstance(outcome, removal_guard.Kept):
            raise removal_guard.ChangedEntry(outcome.reason)
        if outcome != approval:
            raise removal_guard.ChangedEntry("the link changed since the list was checked")
        entries = {path: CleanupEntry(entry_stamp(checked.info), ())}
        remove_reviewed_entries(
            checked, removal_guard.ApprovedCleanup(entries, removal_guard.CleanupRemoval.PLAIN),
            before_delete=lambda: require_no_process_use(path, (checked.parent,), check_links=True))


def remove_configured_folder(path: Path, guard: CleanupGuard, machine: Machine, approval: WouldRemove) -> None:
    related = related_sidecars(path, machine, guard.bounds)
    pid_check = LeasePidCheck.ANY_LIVE_PID if related else LeasePidCheck.MATCH_START
    with guard.check(path) as checked:
        fds = [checked.parent]
        with build_folders.folder_removal_lease(path, children=True, wait=False, probe_fds=fds,
                                                root=Path("/"),
                                                create=not related and os.path.lexists(
                                                    build_folders.lease_path(path))) as lease:
            if isinstance(lease, build_folders.RefusedRemovalLease):
                raise removal_guard.ChangedEntry("a folder lock is held or unreadable")
            sidecars = inspect_sidecars(checked.parent, guard, related)
            require_no_process_use(path, fds, sidecars=related)
            entries: dict[Path, CleanupEntry] = {}
            tree = inspect_cleanup_tree(checked, entries)
            require_saved_cargo_output(checked, entries)
            require_no_live_leases(path, tree, pid_check)

            def recheck_leases(current: CheckedCleanup, fresh: CleanupGuard, latest: CleanupTree) -> None:
                require_no_live_leases(path, latest, pid_check)
                if related:
                    confirm_sidecars(current, fresh, related, lease, sidecars)

            if related:
                confirm_sidecars(checked, guard, related, lease, sidecars)
            if tree.git is CleanupGit.CHECKOUT:
                reviewed = review_cleanup_git(checked, tree, guard)
                if sidecars.bind(reviewed.fingerprint(tree)) != approval.fingerprint:
                    raise removal_guard.ChangedEntry("Git state or contents changed since the list was checked")
                remove_git(checked, tree, reviewed, fds, guard, sidecars=related, recheck_leases=recheck_leases)
            elif tree.git is CleanupGit.PROTECTED:
                raise removal_guard.ChangedEntry(GIT_KEEP_REASON)
            else:
                if sidecars.bind(tree.fingerprint) != approval.fingerprint:
                    raise removal_guard.ChangedEntry("identity or contents changed since the list was checked")
                remove_reviewed_entries(checked, removal_guard.ApprovedCleanup(
                    entries, removal_guard.CleanupRemoval.PLAIN),
                    before_delete=lambda: require_no_process_use(path, fds, sidecars=related))
            if sidecars.stamps and isinstance(lease, build_folders.HeldFolderRemovalLease):
                finish_sidecars(path, guard, lease, sidecars)


def apply(review: HostList, bounds: CleanupBounds) -> int:
    """Recheck each approved row while holding its removal leases."""
    guard = read_cleanup_guard(frozenset(row.path for row in review.rows), bounds)
    status = 0
    for row in review.rows:
        if not isinstance(row.outcome, WouldRemove):
            continue
        path = row.path
        try:
            if row.place is CleanupPlace.LINK:
                remove_worktree_link(path, guard, row.outcome)
            elif row.place is CleanupPlace.FOLDER:
                remove_configured_folder(path, guard, review.machine, row.outcome)
            else:
                raise removal_guard.ChangedEntry("the folder is for review only")
        except removal_guard.PartialCleanup as error:
            report_partial_removal(path, error, error.deleted)
            status = 1
        except (OSError, removal_guard.Refused, RuntimeError, ValueError) as error:
            report_removal(f"kept {str(path)!r}: {removal_guard.single_line(error)}")
            status = 1
        else:
            report_removal(f"removed {str(path)!r}")
    return status


def run_remote(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    timeout = conf.read("disks").whole("cleanup.ssh_seconds")
    return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)


def decode_cleanup_rows(raw: object, machine: Machine) -> tuple[Row, ...]:
    rows: list[Row] = []
    seen: set[Path] = set()
    for item in message_array(raw):
        fields = message_object(item)
        name, size, keep, stamp, git = (fields.get(key) for key in ("path", "bytes", "keep", "fingerprint", "git"))
        if (not isinstance(name, str) or not isinstance(keep, str) or not isinstance(stamp, str)
                or not isinstance(git, str) or any(ord(char) < 32 for char in git)
                or (size is not None and (type(size) is not int or size < 0))
                or (bool(keep) == bool(stamp)) or (stamp and (size is None or not DIGEST.fullmatch(stamp)))):
            raise ValueError("invalid cleanup row")
        path = Path(name)
        if not path.is_absolute() or ".." in path.parts or path in seen:
            raise ValueError("invalid cleanup path")
        seen.add(path)
        listed = message_array(fields.get("sidecars", []))
        sidecars = tuple(name for name in worktree_sidecar_names(path) if name in listed)
        if len(sidecars) != len(listed) or (machine is Machine.MAC and sidecars
                                           and not path.as_posix().endswith("/" + MAC_OLD_WORKTREE)):
            raise ValueError("invalid cleanup sidecars")
        place = CleanupPlace.FOLDER if machine is Machine.MAC else CleanupPlace(fields.get("place"))
        if stamp and place not in (CleanupPlace.FOLDER, CleanupPlace.LINK):
            raise ValueError("invalid cleanup approval")
        rows.append(Row(path, size, removal_guard.Kept(keep) if keep else WouldRemove(stamp), git, sidecars, place))
    return tuple(rows)


def parse_mac_list(output: str) -> HostList:
    record = message_object(cast(object, json.loads(output)))
    if record.get("machine") != Machine.MAC.value:
        raise ValueError("expected Mac cleanup rows")
    result = HostList(Machine.MAC, decode_cleanup_rows(record.get("rows"), Machine.MAC))
    if record.get("digest") != result.record()["digest"]:
        raise ValueError("Mac cleanup digest does not match the rows")
    return result


def remote_list(run_ssh: SshRunner) -> HostList | MacListUnavailable:
    detail = ""
    try:
        answer = run_ssh(("ssh", "-o", "BatchMode=yes", "mac", REMOTE_COMMAND + " --json"))
        detail = answer.stderr
        if answer.returncode == 0:
            return parse_mac_list(answer.stdout)
        detail = detail or f"SSH exited with status {answer.returncode}"
    except subprocess.TimeoutExpired as error:
        stderr = error.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        detail = stderr or f"SSH timed out after {error.timeout:g} seconds"
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        detail = detail or str(error)
    first_line = next(iter(detail.splitlines()), "")
    return MacListUnavailable(first_line.replace("\t", " ").strip())


def remove_remote(review: HostList, run_ssh: SshRunner) -> int:
    if not any(isinstance(row.outcome, WouldRemove) for row in review.rows):
        return 0
    try:
        answer = run_ssh(("ssh", "-o", "BatchMode=yes", "mac", REMOTE_COMMAND + " --go " + review.record()["digest"]))
    except (OSError, subprocess.SubprocessError, ValueError):
        print("mac unreachable", file=sys.stderr)
        return 1
    if answer.stdout:
        print(answer.stdout, end="")
    if answer.stderr:
        print(answer.stderr, end="", file=sys.stderr)
    if answer.returncode == 255:
        print("mac unreachable", file=sys.stderr)
    return answer.returncode


def show(local: HostList, mac: HostList | MacListUnavailable | LocalListOnly, approval: str) -> None:
    uid = os.geteuid()
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    print(f"Use check: processes of {user} only; other users' processes are not checked.")
    if local.unreadable_processes:
        reason = incomplete_scan_reason(local.unreadable_processes)
        print(reason[0].upper() + reason[1:])
    print("Machine\tPlace\tBytes\tResult")
    reviews = (local, mac) if isinstance(mac, HostList) else (local,)
    for review in reviews:
        for row in review.rows:
            reason = row.outcome.reason if isinstance(row.outcome, removal_guard.Kept) else "would remove"
            if row.git:
                reason = f"{reason}, {row.git}"
            if row.sidecars:
                reason = f"{reason}; includes {', '.join(map(repr, row.sidecars))}"
            size = row.size if row.size is not None else "unknown"
            print(f"{review.machine.value}\t{str(row.path)!r}\t{size}\t{table_text(reason)}")
    if isinstance(mac, MacListUnavailable):
        print(f"mac\t-\tunknown\t{table_text(mac.message())}")
    for review in reviews:
        total = sum(row.size or 0 for row in review.rows)
        note = " measured (some sizes unknown)" if any(row.size is None for row in review.rows) else ""
        print(f"{review.machine.value} total: {total} bytes{note}")
    if isinstance(mac, MacListUnavailable):
        print("mac total: unknown")
    print(f"Digest: {approval}")


def table_text(text: str) -> str:
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)


def local_machine() -> Machine:
    if sys.platform == "darwin":
        return Machine.MAC
    if sys.platform == "linux" and socket.gethostname().partition(".")[0] == Machine.NATEDEV.value:
        return Machine.NATEDEV
    raise ValueError("cleanup_old_folders runs only on natedev and the Mac")


def parse_arguments(argv: Sequence[str]) -> Arguments:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--go", metavar="DIGEST", help="apply the review by hand")
    _ = parser.add_argument("--host-list", action="store_true", help="read only this machine")
    _ = parser.add_argument("--json", action="store_true", help="print the local list as JSON")
    args = parser.parse_args(argv, namespace=Arguments())
    if args.json and (not args.host_list or args.go is not None):
        parser.error("--json requires --host-list without --go")
    return args


def review_local(machine: Machine) -> tuple[HostList, CleanupBounds]:
    bounds = CleanupBounds(Path.home(), build_folders.build_root(), removal_guard.known_names().repositories)
    candidates = collect(machine, conf.read("disks"), bounds)
    paths = frozenset(candidate.path for candidate in candidates)
    guard = read_cleanup_guard(paths, bounds)
    return assess(candidates, guard, machine), bounds


def finish_cleanup(args: Arguments, local: HostList, run_ssh: SshRunner, apply_local: Callable[[], int]) -> int:
    local_record = local.record()
    if args.json:
        print(json.dumps(local_record))
        return 0
    mac = (remote_list(run_ssh) if local.machine is Machine.NATEDEV and not args.host_list
           else LocalListOnly.REQUESTED)
    approval = local_record["digest"]
    if isinstance(mac, HostList):
        approval = digest((approval, mac.record()["digest"]))
    elif isinstance(mac, MacListUnavailable):
        approval = digest((approval, MAC_UNREACHABLE))
    if args.go is None:
        show(local, mac, approval)
        return 0
    if isinstance(mac, MacListUnavailable):
        print(mac.message(), file=sys.stderr)
    if not DIGEST.fullmatch(args.go) or not hmac.compare_digest(args.go, approval):
        print("cleanup_old_folders refused: the list digest changed", file=sys.stderr)
        return 1
    if isinstance(mac, HostList) and remove_remote(mac, run_ssh):
        return 1
    return apply_local()


def main(argv: Sequence[str], *, run_ssh: SshRunner = run_remote) -> int:
    args = parse_arguments(argv)
    try:
        local, bounds = review_local(local_machine())
        return finish_cleanup(args, local, run_ssh, lambda: apply(local, bounds))
    except (OSError, ValueError, removal_guard.Refused) as error:
        print(f"cleanup_old_folders refused: {removal_guard.single_line(error)}", file=sys.stderr)
        return 1


def worker_review(local: HostList) -> WorkerReview:
    rows: list[WorkerRowRecord] = [{**row_record(row), "place": row.place.value} for row in local.rows]
    return {"kind": "review", "rows": rows, "unreadable": local.unreadable_processes}


def serve_masked_cleanup(channel: WorkerChannel, build: Path, mounts: frozenset[Path]) -> int:
    token = MASKED_WORKER_CHANNEL.set(channel)
    mask_token = MASKS_IN_FORCE.set(ProtectedFolderMasks(build, mounts))
    try:
        check_protected_masks()
        local, bounds = review_local(Machine.NATEDEV)
        check_protected_masks()
        channel.send(worker_review(local))
        line = channel.incoming.readline()
        check_protected_masks()
        if not line:
            return 0
        if message_object(cast(object, json.loads(line))) != {"kind": "apply"}:
            raise ValueError("invalid cleanup approval message")
        result: WorkerRunResult = {"kind": "applied", "status": apply(local, bounds)}
        channel.send(result)
        return 0
    finally:
        MASKS_IN_FORCE.reset(mask_token)
        MASKED_WORKER_CHANNEL.reset(token)


def masked_child(build: Path, mounts: frozenset[Path]) -> int:
    # The rescue and worktree helpers also start Git commands.
    sys.addaudithook(check_before_git)
    try:
        with os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8") as outgoing:
            _ = os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            return serve_masked_cleanup(WorkerChannel(sys.stdin, outgoing), build, mounts)
    except (OSError, ValueError, removal_guard.Refused, ProtectedMaskChanged) as error:
        print(f"masked cleanup worker failed: {removal_guard.single_line(error)}", file=sys.stderr)
        return 1


def receive_worker_report(channel: WorkerChannel, kind: str) -> dict[str, object]:
    while True:
        message = channel.receive()
        message_kind = message.get("kind")
        if message_kind == kind:
            return message
        if message_kind == "process":
            answer_process_check(channel, message)
        elif kind == "applied" and message_kind == "removal-result" and isinstance(output := message.get("output"), str):
            print(output, flush=True)
        else:
            raise ValueError("unexpected masked cleanup worker message")


def finish_worker(child: subprocess.Popen[str], channel: WorkerChannel) -> None:
    channel.outgoing.close()
    if channel.incoming.read(1) or child.wait():
        raise ValueError("the masked cleanup worker failed")


def run_masked_cleanup(command: Sequence[str], args: Arguments, run_ssh: SshRunner) -> int:
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8") as child:
        assert child.stdout is not None and child.stdin is not None
        channel = WorkerChannel(cast(TextIO, child.stdout), cast(TextIO, child.stdin))
        report = receive_worker_report(channel, "review")
        local = HostList(Machine.NATEDEV, decode_cleanup_rows(report.get("rows"), Machine.NATEDEV),
                         unreadable_records(report.get("unreadable")))
        if args.go is None:
            finish_worker(child, channel)

        def apply_local() -> int:
            channel.send({"kind": "apply"})
            result = receive_worker_report(channel, "applied")
            status = result.get("status")
            if type(status) is not int or status not in (0, 1):
                raise ValueError("invalid cleanup apply result")
            finish_worker(child, channel)
            return status

        return finish_cleanup(args, local, run_ssh, apply_local)


def run_cli(argv: Sequence[str], *, run_ssh: SshRunner = run_remote) -> int:
    if sys.platform != "linux":
        return main(argv, run_ssh=run_ssh)
    args = parse_arguments(argv)
    try:
        _ = local_machine()
        checkout = Path(__file__).absolute().parents[2]
        child = (
            "from pathlib import Path\n"
            + "import sys\n"
            + "sys.path.insert(0, sys.argv[1])\n"
            + "from scripts.disks.cleanup_old_folders import masked_child\n"
            + "raise SystemExit(masked_child(Path(sys.argv[2]), frozenset(map(Path, sys.argv[3:]))))"
        )
        mounts = protected_mounts(Path.home(), LINUX_BUILD_ROOT)
        command = masked_cleanup_command(mounts, (sys.executable, "-B", "-c", child, str(checkout),
                                                  str(LINUX_BUILD_ROOT), *map(str, mounts)))
        return run_masked_cleanup(command, args, run_ssh)
    except (OSError, ValueError) as error:
        print(f"cleanup_old_folders refused: {removal_guard.single_line(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cli(sys.argv[1:]))

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
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import Enum
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, NotRequired, TextIO, TypedDict, cast, final, override
from urllib.parse import unquote, urlsplit

from scripts.copies import copies
from scripts.lib import build_folder_lock, build_folders, conf, git_files, git_rescue, process_start
from scripts.lib import removal_guard
from scripts.lib.process_failure import failure_detail
from scripts.lint import tmp_sweep
from scripts.production import staff
from scripts.worktree import worktree_records

if TYPE_CHECKING:
    from scripts.lib.session_move import TreeEntry


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
    GATE_FOLDER = "gate folder"
    GATE_SIDECARS = "orphan gate sidecars"
    UNREADABLE_GATE_ROOT = "unreadable gate root"


class CleanupMode(Enum):
    STANDARD = "standard"
    MOVE_GATE_GIT = "move-gate-git"


@dataclass(frozen=True)
class Candidate:
    path: Path
    decision: RemovalAssessment | removal_guard.Kept
    place: CleanupPlace = CleanupPlace.FOLDER


@dataclass(frozen=True)
class WouldRemove:
    fingerprint: str


@dataclass(frozen=True)
class WouldMove:
    target: Path
    fingerprint: str


class GateEvidenceUnavailable(Enum):
    UNKNOWN = "unknown"


class LeaseState(Enum):
    ABSENT = "no lease"
    UNREADABLE = "unreadable"
    ALIVE = "alive"
    DEAD = "dead"


@dataclass(frozen=True)
class LeaseStatus:
    state: LeaseState
    owner: str = ""

    def text(self) -> str:
        return f"{self.state.value} ({self.owner})" if self.owner else self.state.value


@dataclass(frozen=True)
class GateDetails:
    kind: str
    newest_ns: int | GateEvidenceUnavailable
    leases: tuple[LeaseStatus, ...]
    use: str


class NoGateDetails(Enum):
    NOT_GATE_ROW = "not a gate row"


class GateRootNotSelected(Enum):
    NOT_SELECTED = "this review has no gate root"


class GateDeviceUnavailable(Enum):
    UNKNOWN = "cannot identify the gate root filesystem"


@dataclass(frozen=True)
class Row:
    path: Path
    size: int | None
    outcome: WouldMove | WouldRemove | removal_guard.Kept
    git: str = ""
    sidecars: tuple[str, ...] = ()
    place: CleanupPlace = CleanupPlace.FOLDER
    gate: GateDetails | NoGateDetails = NoGateDetails.NOT_GATE_ROW


class GateRecord(TypedDict):
    kind: str
    newest_ns: int | None
    lease: tuple[tuple[str, str], ...]
    use: str


class RowRecord(TypedDict):
    path: str
    bytes: int | None
    keep: str
    fingerprint: str
    git: str
    sidecars: NotRequired[tuple[str, ...]]
    gate: NotRequired[GateRecord]
    move_to: NotRequired[str]


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
        "fingerprint": outcome.fingerprint if isinstance(outcome, (WouldMove, WouldRemove)) else "",
        "git": row.git,
    }
    if row.sidecars:
        record["sidecars"] = row.sidecars
    if isinstance(outcome, WouldMove):
        record["move_to"] = str(outcome.target)
    if isinstance(row.gate, GateDetails):
        gate = row.gate
        record["gate"] = {"kind": gate.kind, "newest_ns": gate.newest_ns if isinstance(gate.newest_ns, int) else None,
                          "lease": tuple((lease.state.value, lease.owner) for lease in gate.leases), "use": gate.use}
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


class GatePathUse(Enum):
    CLEAR = ""
    ALIAS = "gate root open through another path; live use not provable"
    UNREADABLE = "cannot check for other paths to the gate root"


class CleanupTaskState(Enum):
    OTHER_USER = "the task belongs to another user"
    ENDED = "the task has ended"
    ALIVE = "a live task to check"


class InheritedCleanupUser(Enum):
    CURRENT = "use the current user"


@dataclass(frozen=True)
class SudoCleanupUser:
    uid: int
    gid: int
    name: str
    home: Path
    setpriv: str

    def command(self, command: Sequence[str]) -> tuple[str, ...]:
        return (self.setpriv, f"--reuid={self.uid}", f"--regid={self.gid}", "--clear-groups", "--no-new-privs",
                "--inh-caps=-all", "--ambient-caps=-all", "--bounding-set=-all", *command)

    def environment(self) -> dict[str, str]:
        return {**os.environ, "HOME": str(self.home), "USER": self.name, "LOGNAME": self.name}


class Arguments(argparse.Namespace):
    go: str | None = None
    host_list: bool = False
    json: bool = False


SshRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
REMOTE_COMMAND = "~/.claude/scripts/lib/py ~/.claude/scripts/disks/cleanup_old_folders.py --host-list"
MAC_UNREACHABLE = "mac unreachable"
PROC_ROOT = "/proc"
DIGEST = re.compile(r"[0-9a-f]{64}")
CONFIG_SECTION = re.compile(r'\[([a-zA-Z0-9-]+)(?:\.([a-zA-Z0-9.-]+)|[ \t]+"((?:[^"\\]|\\.)*)")?\]')
CONFIG_KEY = re.compile(r"[a-zA-Z][a-zA-Z0-9-]*")
CONFIG_ESCAPES = {"n": "\n", "t": "\t", "b": "\b", '"': '"', "\\": "\\"}
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
GATE_IF_NAMED = removal_guard.Kept("would remove if named")
GATE_RECOVERY = "gate recovery record"
GATE_OTHER_OWNER = "other user's files; live use not provable"
GATE_GIT_KEEP_REASON = "holds a Git repository; manual review"
GATE_ALTERNATES = "Git alternates; manual review"
GATE_MOVE_LABEL = "would move to disk"
STANDARD_CLEANUP_ONLY = removal_guard.Kept("use the standard cleanup list to remove this folder")
GATE_GIT_STORAGE = "Git data in the gate root; manual review"
TEST_GATE_KINDS = ("t", "targeted-test")
GATE_PREFIXES = ("merge-gate-recovery", "gate-recovery", "phase-44ca-gate",
                 "merge-gate", "fc", "mg", "g", *TEST_GATE_KINDS)
UNKNOWN_GATE_KIND = "unknown"
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
    return (bwrap, "--unshare-user", "--disable-userns", "--cap-drop", "ALL", "--die-with-parent",
            "--bind", "/", "/", "--dev-bind", "/dev", "/dev", *options, "--", *command)


@dataclass(frozen=True)
class CleanupBounds:
    home: Path
    build: Path
    repositories: frozenset[str]
    gate: Path | GateRootNotSelected = GateRootNotSelected.NOT_SELECTED
    gate_device: int | GateDeviceUnavailable = GateDeviceUnavailable.UNKNOWN

    def is_gate_child(self, path: Path) -> bool:
        return path.parent == self.gate

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

    def require_git_outside_gate(self, path: Path) -> None:
        if isinstance(self.gate, Path) and path.is_relative_to(self.gate):
            raise GateGitStorage()

    def require_git_on_another_device(self, info: os.stat_result) -> None:
        if info.st_dev == self.gate_device:
            raise GateGitStorage()

    def storage_path(self, path: Path, *, follow_links: bool = True, exclude_gate: bool = False) -> Path:
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
            if exclude_gate:
                self.require_git_outside_gate(entry)
            identity = entry.lstat()
            if stat.S_ISLNK(identity.st_mode):
                if not follow_links:
                    if exclude_gate:
                        require_git_pointer_outside_gate(path, self)
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
        if exclude_gate and isinstance(self.gate_device, int):
            self.require_git_on_another_device(resolved.lstat())
        return resolved

    def protects(self, path: Path) -> bool:
        if any(part.casefold() == ".git" for part in path.parts):
            return True
        if self.forbids_walk(path) or any(root.is_relative_to(path) for root in (
            self.home, self.build, LINUX_BUILD_ROOT, self.home / "rust/hanadocs",
        )):
            return True
        if isinstance(self.gate, Path) and (self.gate.is_relative_to(path)
                or (path.is_relative_to(self.gate) and not self.is_gate_child(path))):
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

    @property
    def published_copy_path(self) -> Path:
        return gate_move_target(self.path, self.bounds)

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


class NoSourceFingerprint(Enum):
    GATE_FOLDER = "gate folders do not use Git rescue"


@dataclass(frozen=True)
class CleanupTree:
    fingerprint: str
    size: int
    git: CleanupGit
    lease_owners: tuple[Path, ...]
    source_fingerprint: str | NoSourceFingerprint
    newest_ns: int
    gate_issue: str


class Snapshot(Enum):
    PREVIEW = "the list keeps fingerprints only"


def inspect_cleanup_tree(checked: CheckedCleanup,
                         entries: dict[Path, CleanupEntry] | Snapshot = Snapshot.PREVIEW) -> CleanupTree:
    """Measure the tree and record each entry without following links."""
    fingerprint = hashlib.sha256()
    gate = checked.bounds.is_gate_child(checked.path)
    source_fingerprint = NoSourceFingerprint.GATE_FOLDER if gate else hashlib.sha256()
    inodes: set[removal_guard.Identity] = set()
    lease_owners: list[Path] = []
    size = 0
    newest_ns = checked.info.st_mtime_ns
    gate_issue = ""
    uid = os.geteuid()
    git = CleanupGit.NONE
    dot_git = checked.path / ".git"

    def visit(parent: int, name: str, path: Path) -> None:
        nonlocal size, git, newest_ns, gate_issue
        checked.bounds.require_walk(path)
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        stamp = entry_stamp(info)
        newest_ns = max(newest_ns, stamp.modified_ns)
        if gate:
            if path.name == "run.json":
                gate_issue = GATE_RECOVERY
            elif not gate_issue and stamp.uid != uid:
                gate_issue = GATE_OTHER_OWNER
            elif not gate_issue and not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                gate_issue = f"contains a link or an unknown entry type: {path}"
            elif not gate_issue and stat.S_ISREG(info.st_mode) and path.parts[-3:] == ("objects", "info", "alternates"):
                gate_issue = GATE_ALTERNATES
        evidence = json.dumps((str(path.relative_to(checked.path)), stamp)).encode()
        fingerprint.update(evidence)
        in_git = path.is_relative_to(dot_git)
        if not in_git and not isinstance(source_fingerprint, NoSourceFingerprint):
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
                    git = (CleanupGit.CHECKOUT if not gate and path == checked.path and ".git" in names and not bare
                           else CleanupGit.PROTECTED)
                for child in names:
                    visit(descriptor, child, path / child)
                if entry_stamp(os.fstat(descriptor)) != stamp:
                    raise removal_guard.ChangedEntry("folder changed during assessment")
            finally:
                os.close(descriptor)
        elif gate and isinstance(entries, dict) and sys.platform == "linux":
            flags = os.O_RDONLY | os.O_NONBLOCK if stat.S_ISREG(info.st_mode) else os.O_PATH
            descriptor = os.open(name, flags | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if info.st_dev != checked.info.st_dev or removal_guard.mount_id(descriptor) != checked.mount:
                    raise removal_guard.Refused(removal_guard.RefusalReason.MOUNT, str(path))
                if entry_stamp(os.fstat(descriptor)) != stamp:
                    raise removal_guard.ChangedEntry("entry changed during assessment")
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
    source = (source_fingerprint if isinstance(source_fingerprint, NoSourceFingerprint)
              else source_fingerprint.hexdigest())
    return CleanupTree(fingerprint.hexdigest(), size, git, tuple(lease_owners), source, newest_ns, gate_issue)


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


class GateGitStorage(GitCleanupRefused):
    def __init__(self) -> None:
        super().__init__(GATE_GIT_STORAGE)


class MissingGateGitDirectory(FileNotFoundError):
    """A gate checkout names a Git directory that does not exist."""


WRONG_GIT_WORKTREE = "review only: Git worktree root does not match the row path"


def git_config_value(line: str, lines: Iterator[str]) -> str:
    value: list[str] = []
    end = 0
    quoted = False
    while True:
        characters = iter(line)
        for char in characters:
            if char == '"':
                quoted = not quoted
                continue
            if not quoted and char in "#;":
                return "".join(value[:end])
            if char == "\\":
                escape = next(characters, "")
                if not escape:
                    line = next(lines, "")
                    break
                if escape not in CONFIG_ESCAPES:
                    raise ValueError("cannot read Git config escape")
                char = CONFIG_ESCAPES[escape]
                value.append(char)
                end = len(value)
            elif quoted or char not in " \t\r\v\f":
                value.append(char)
                end = len(value)
            elif value:
                value.append(char)
        else:
            if quoted:
                raise ValueError("cannot read Git config value")
            return "".join(value[:end])


def git_config_origins(content: str) -> Iterator[str]:
    """Read local origin values before Git can open a repository."""
    section = subsection = ""
    lines = iter(content.split("\n"))
    for physical in lines:
        line = physical.lstrip(" \t\r\v\f")
        while line.startswith("["):
            match = CONFIG_SECTION.match(line)
            if match is None:
                raise ValueError("cannot read Git config section")
            section = match[1].lower()
            subsection = re.sub(r"\\(.)", r"\1", match[3]) if match[3] is not None else (match[2] or "").lower()
            line = line[match.end():].lstrip(" \t\r\v\f")
        key = CONFIG_KEY.match(line)
        if key is None:
            continue
        name = key[0].lower()
        if section in ("include", "includeif") and name == "path":
            raise GitCleanupRefused("git config include")
        value = line[key.end():].lstrip(" \t\r\v\f")
        if value.startswith("="):
            parsed = git_config_value(value[1:], lines)
            if section == "remote" and subsection == "origin" and name == "url":
                yield parsed


def check_git_origin_gate(value: str, worktree: Path, bounds: CleanupBounds) -> None:
    if not isinstance(bounds.gate, Path) or not value:
        return
    if value.lower().startswith("file://"):
        url = urlsplit(value)
        if url.netloc not in ("", "localhost"):
            return
        value = unquote(url.path, errors="surrogateescape")
    elif ":" in value.partition("/")[0]:
        return
    try:
        origin = bounds.storage_path(worktree / Path(value).expanduser(), exclude_gate=True)
        try:
            git_dir = checked_git_files(origin, bounds).common_dir
        except git_files.NotACheckout:
            git_dir = origin
        check_object_stores(git_dir / "objects", bounds)
    except GateGitStorage:
        raise
    except (OSError, ValueError):
        # The existing Git review reports other problems with the origin.
        return


def require_safe_git_config(common_dir: Path, git_dir: Path, worktree: Path, bounds: CleanupBounds) -> None:
    check_object_stores(common_dir / "objects", bounds)
    require_safe_git_tree(bounds.storage_path(common_dir, follow_links=False, exclude_gate=True))
    configs: dict[Path, str] = {}
    for directory in dict.fromkeys((common_dir, git_dir)):
        for name in ("config", "config.worktree"):
            path = directory / name
            try:
                resolved = bounds.storage_path(path, follow_links=False, exclude_gate=True)
            except FileNotFoundError:
                if os.path.lexists(path):
                    raise
                continue
            descriptor = os.open(resolved, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8-sig", errors="surrogateescape", newline="\n") as config:
                if not stat.S_ISREG(os.fstat(config.fileno()).st_mode):
                    raise ValueError(f"expected a regular Git config: {path}")
                content = config.read()
                for origin in git_config_origins(content):
                    check_git_origin_gate(origin, worktree, bounds)
                configs[path] = content
    for path, content in configs.items():
        with isolated_git_config():
            values = subprocess.run(
                ("git", "config", "--file", "-", "--no-includes", "--null", "--list"),
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
        origin = bounds.storage_path(path / Path(url).expanduser(), exclude_gate=True)
        if not origin.is_dir() or origin.is_relative_to(path):
            return removal_guard.Kept(NO_LOCAL_ORIGIN)
        try:
            source = checked_git_files(origin, bounds)
        except git_files.NotACheckout:
            require_safe_git_config(origin, origin, origin, bounds)
            actual = cleanup_git(origin, f"--git-dir={origin}", "rev-parse", "--absolute-git-dir").strip()
            if Path(actual) != origin:
                return removal_guard.Kept(NO_LOCAL_ORIGIN)
            git_dir = origin
        else:
            git_dir = source.common_dir
            require_safe_git_config(git_dir, source.git_dir, source.toplevel, bounds)
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


def checked_git_files(path: Path, bounds: CleanupBounds, *, exclude_gate: bool = True) -> git_files.Checkout:
    """Check the row's Git directory and its named registration without following links."""
    marker = path / ".git"
    bounds.require_walk(marker)
    try:
        marker_info = marker.lstat()
    except FileNotFoundError as error:
        raise git_files.NotACheckout(f"{path}: no .git entry") from error
    if stat.S_ISDIR(marker_info.st_mode):
        if exclude_gate:
            bounds.require_git_on_another_device(marker_info)
        for name in ("gitdir", "commondir"):
            pointer = marker / name
            if os.path.lexists(pointer):
                check_git_pointer(pointer, marker, bounds, exclude_gate=exclude_gate)
        return git_files.Checkout(path, marker, marker, marker_info, marker_info)
    if exclude_gate and stat.S_ISLNK(marker_info.st_mode):
        _ = bounds.storage_path(marker, follow_links=False, exclude_gate=True)
    pointer = read_git_pointer(marker)
    if not pointer.startswith("gitdir:") or not pointer.removeprefix("gitdir:").strip():
        raise ValueError(f"invalid Git directory pointer: {str(marker)!r}")
    named = path / pointer.removeprefix("gitdir:").strip()
    registration = Path(os.path.normpath(named))
    named_registration = registration.parent.name == "worktrees" and not registration.is_relative_to(path)
    gate_borrower = not exclude_gate and bounds.is_gate_child(path)
    if not named_registration and exclude_gate:
        require_git_pointer_outside_gate(named, bounds)
    if not named_registration and not gate_borrower:
        raise GitMetadataRefused(marker, "points outside the row and its named registration")
    try:
        git_dir = bounds.storage_path(named, follow_links=False, exclude_gate=exclude_gate)
    except FileNotFoundError as error:
        if gate_borrower:
            raise MissingGateGitDirectory(str(error)) from error
        raise
    if not named_registration:
        raise GitMetadataRefused(marker, "points outside the row and its named registration")
    common = git_dir.parent.parent
    git_info = git_dir.lstat()
    common_info = common.lstat()
    if exclude_gate:
        bounds.require_git_on_another_device(common_info)
    if not stat.S_ISDIR(git_info.st_mode) or not stat.S_ISDIR(common_info.st_mode):
        raise GitMetadataRefused(marker, "does not name a registration directory")
    check_git_pointer(git_dir / "gitdir", marker, bounds, exclude_gate=exclude_gate)
    check_git_pointer(git_dir / "commondir", common, bounds, exclude_gate=exclude_gate)
    return git_files.Checkout(path, git_dir, common, git_info, common_info)


def check_git_pointer(pointer: Path, expected: Path, bounds: CleanupBounds, *, exclude_gate: bool) -> None:
    named = pointer.parent / read_git_pointer(pointer)
    if Path(os.path.normpath(named)) != expected:
        if exclude_gate:
            require_git_pointer_outside_gate(named, bounds)
        raise GitMetadataRefused(pointer, "points outside the row and its named registration")
    if bounds.storage_path(named, follow_links=False, exclude_gate=exclude_gate) != expected:
        raise GitMetadataRefused(pointer, "points outside the row and its named registration")


def require_git_pointer_outside_gate(path: Path, bounds: CleanupBounds) -> None:
    try:
        _ = bounds.storage_path(path, exclude_gate=True)
    except GateGitStorage:
        raise
    except (OSError, ValueError):
        pass


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


def alternate_paths(objects: Path, bounds: CleanupBounds, *, exclude_gate: bool) -> Iterator[Path]:
    """Read the alternate links before any Git command can follow them."""
    entry = objects / "info/alternates"
    try:
        path = bounds.storage_path(entry, follow_links=False, exclude_gate=exclude_gate)
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


def object_stores(start: Path, bounds: CleanupBounds, deadline: float, *, exclude_gate: bool = True) -> Iterator[Path]:
    pending = [start]
    visited: set[Path] = set()
    while pending:
        if time.monotonic() >= deadline:
            raise ValueError(f"Git object scan timed out at {start}")
        objects = bounds.storage_path(pending.pop(), follow_links=False, exclude_gate=exclude_gate)
        if objects in visited:
            continue
        if not objects.is_dir():
            raise ValueError(f"expected a Git object directory: {objects}")
        visited.add(objects)
        yield objects
        pending.extend(alternate_paths(objects, bounds, exclude_gate=exclude_gate))


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
            git_dir = checked_git_files(repository, bounds, exclude_gate=False).common_dir
        except MissingGateGitDirectory:
            continue
        except git_files.NotACheckout:
            git_dir = repository
            if not os.path.lexists(git_dir / "objects") or not os.path.lexists(git_dir / "HEAD"):
                continue
        if git_dir in checked:
            continue
        checked.add(git_dir)
        for objects in object_stores(git_dir / "objects", bounds, deadline, exclude_gate=False):
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


def gate_places(bounds: CleanupBounds) -> dict[Path, CleanupPlace]:
    root = bounds.gate
    if not isinstance(root, Path) or bounds.forbids_walk(root):
        return {}
    try:
        with build_folder_lock.folder_parent(root / "entry", Path("/"), create=False) as directory:
            names = frozenset(os.listdir(directory))
    except FileNotFoundError:
        return {}
    places: dict[Path, CleanupPlace] = {}
    for name in names:
        base = name.removesuffix(".lease.json") if name.endswith(".lease.json") else name.removesuffix(".lock")
        if base in ("", ".", "..") or base.endswith((".lock", ".lease.json")):
            places[root / name] = CleanupPlace.GATE_FOLDER
        elif base == name or base in names:
            places[root / base] = CleanupPlace.GATE_FOLDER
        else:
            places[root / base] = CleanupPlace.GATE_SIDECARS
    return places


def collect(machine: Machine, settings: conf.ConfigRead, bounds: CleanupBounds) -> tuple[Candidate, ...]:
    """Expand the configured places and skip missing or protected paths."""
    reason = settings.value(f"cleanup.list_only_reason.{machine.value}", str,
                            recover_invalid=False, host_override=conf.HostOverride.IGNORED)
    assignments: dict[Path, Candidate] = {}
    expansions: dict[Path, tuple[Path, ...]] = {}
    try:
        gates = gate_places(bounds)
    except OSError as error:
        assert isinstance(bounds.gate, Path)
        gate_error = f"cannot read the gate root: {removal_guard.single_line(error)}"
        assignments[bounds.gate] = Candidate(bounds.gate, removal_guard.Kept(gate_error), CleanupPlace.UNREADABLE_GATE_ROOT)
        gates = {}

    for path, place in gates.items():
        assignments[path] = Candidate(path, NO_CLEANUP_SETTING, place)

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
            if bounds.is_gate_child(pattern):
                for path, place in gates.items():
                    if place is CleanupPlace.GATE_FOLDER and fnmatch.fnmatchcase(path.name, pattern.name):
                        assignments[path] = Candidate(path, decision, place)
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
            if bounds.is_gate_child(path):
                continue
            configured = assignments.get(path)
            if configured is None:
                assignments[path] = Candidate(path, NO_CLEANUP_SETTING, place)
            elif place is CleanupPlace.LINK:
                assignments[path] = Candidate(path, configured.decision, place)
    return tuple(sorted(assignments.values(), key=candidate_order))


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


def process_name(pid: int) -> str:
    try:
        return (Path(PROC_ROOT) / str(pid) / "comm").read_text(errors="replace").strip()
    except OSError:
        return "<command unavailable>"


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
    gate_paths: GatePathUse = GatePathUse.CLEAR

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
    gate_root: str


class ProcessCheckResult(TypedDict):
    kind: Literal["process-result"]
    paths: tuple[tuple[str, int, str], ...]
    path_failure: str
    pwd_users: tuple[tuple[str, int], ...]
    pwd_failure: str
    unreadable: tuple[tuple[int, str], ...]
    gate_paths: str


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

    def check_process_use(self, paths: Sequence[Path], check_links: bool, fds: frozenset[int],
                          gate_root: Path | GateRootNotSelected) -> CleanupProcessReview:
        request: ProcessCheckRequest = {
            "kind": "process", "pid": os.getpid(), "paths": tuple(map(str, paths)),
            "links": check_links, "descriptors": tuple(sorted(fds)),
            "gate_root": str(gate_root) if isinstance(gate_root, Path) else "",
        }
        self.send(request)
        fields = self.receive()
        path_failure = fields.get("path_failure")
        pwd_failure = fields.get("pwd_failure")
        gate_paths = fields.get("gate_paths")
        if (fields.get("kind") != "process-result" or not isinstance(path_failure, str)
                or not isinstance(pwd_failure, str) or not isinstance(gate_paths, str)):
            raise ValueError("invalid cleanup process result")
        return CleanupProcessReview(
            tuple((cast(str, path), cast(int, pid), cast(str, action))
                  for path, pid, action in message_rows(fields.get("paths"), (str, int, str))),
            removal_guard.Kept(path_failure) if path_failure else NoUseFound.CLEAR,
            tuple((Path(cast(str, path)), cast(int, pid))
                  for path, pid in message_rows(fields.get("pwd_users"), (str, int))),
            removal_guard.Kept(pwd_failure) if pwd_failure else NoUseFound.CLEAR,
            unreadable_records(fields.get("unreadable")),
            GatePathUse(gate_paths),
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
                 ignored_fds: frozenset[int] = tmp_sweep.NO_IGNORED_FDS,
                 gate_root: Path | GateRootNotSelected = GateRootNotSelected.NOT_SELECTED) -> CleanupProcessReview:
    channel = MASKED_WORKER_CHANNEL.get()
    if isinstance(channel, WorkerChannel):
        return channel.check_process_use(paths, check_links, ignored_fds, gate_root)
    return read_process_use(paths, check_links=check_links, ignored_fds=ignored_fds, gate_root=gate_root)


@final
@dataclass
class CleanupProcessScan:
    folders: frozenset[bytes]
    descendants: tuple[bytes, ...]
    gate_root: Path | GateRootNotSelected
    gate_device: int | GateDeviceUnavailable
    gate_paths: GatePathUse
    paths: list[tuple[str, int, str]] = field(default_factory=list)
    pwd_users: list[tuple[Path, int]] = field(default_factory=list)

    def record_path(self, target: bytes, pid: int, action: str) -> None:
        candidates = tmp_sweep.proc_path_candidates(target)
        if isinstance(candidates, tmp_sweep.MissingProcessData):
            raise ValueError(candidates.value)
        self.paths.extend((os.fsdecode(path), pid, action) for path in candidates
                          if path in self.folders or path.startswith(self.descendants))

    def check_gate_link(self, link: Path, target: bytes) -> None:
        if (isinstance(self.gate_root, Path) and isinstance(self.gate_device, int)
                and self.gate_paths is not GatePathUse.ALIAS
                and not Path(os.fsdecode(target)).is_relative_to(self.gate_root)):
            try:
                if link.stat().st_dev == self.gate_device:
                    self.gate_paths = GatePathUse.ALIAS
            except (FileNotFoundError, ProcessLookupError):
                if not process_ended(link):
                    self.gate_paths = GatePathUse.UNREADABLE
            except OSError:
                self.gate_paths = GatePathUse.UNREADABLE

    def read_link(self, link: Path, pid: int, action: str, *, required: bool) -> None:
        try:
            target = os.readlink(os.fsencode(link))
        except (FileNotFoundError, ProcessLookupError):
            if required:
                raise
            return
        self.record_path(target, pid, action)
        self.check_gate_link(link, target)

    def check_gate_mapping(self, task: Path, fields: list[bytes]) -> None:
        root, device = self.gate_root, self.gate_device
        if (not isinstance(root, Path) or not isinstance(device, int) or self.gate_paths is GatePathUse.ALIAS
                or len(fields) < 5 or fields[4] == b"0"):
            return
        major, minor = (int(part, 16) for part in fields[3].split(b":"))
        if (major, minor) != (os.major(device), os.minor(device)):
            return
        try:
            target = os.readlink(task / "map_files" / os.fsdecode(fields[0]))
            aliases = not Path(target).is_relative_to(root)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            # The map table includes the device and the path when the link is unavailable.
            if len(fields) < 6:
                raise
            names = tmp_sweep.proc_path_candidates(fields[5])
            if isinstance(names, tmp_sweep.MissingProcessData):
                raise ValueError(names.value) from None
            aliases = any(not Path(os.fsdecode(name)).is_relative_to(root) for name in names)
        if aliases:
            self.gate_paths = GatePathUse.ALIAS

    def read_task(self, task: Path, pid: int, ignored: frozenset[int], *,
                  threaded: bool, read_shared_paths: bool) -> None:
        stop_on_use = len(self.folders) == 1 and isinstance(self.gate_root, GateRootNotSelected)
        first = len(self.paths)
        require_task_links = threaded or isinstance(self.gate_root, Path)
        self.read_link(task / "cwd", pid, tmp_sweep.WORKS_IN, required=require_task_links)
        if stop_on_use and len(self.paths) > first:
            return
        self.read_link(task / "root", pid, tmp_sweep.ROOTS_IN, required=require_task_links)
        if read_shared_paths or isinstance(self.gate_root, Path):
            self.read_link(task / "exe", pid, tmp_sweep.MAPS_FILE, required=False)
        with os.scandir(str(task / "fd")) as descriptors:
            for entry in descriptors:
                if entry.name.isdecimal() and int(entry.name) not in ignored:
                    self.read_link(Path(entry.path), pid, tmp_sweep.HOLDS_FILE, required=False)
                if stop_on_use and len(self.paths) > first:
                    return
        if read_shared_paths:
            try:
                with (task / "maps").open("rb") as mappings:
                    for line in mappings:
                        fields = line.removesuffix(b"\n").split(maxsplit=5)
                        if len(fields) == 6:
                            self.record_path(fields[5], pid, tmp_sweep.MAPS_FILE)
                        self.check_gate_mapping(task, fields)
                        if stop_on_use and len(self.paths) > first:
                            return
            except (FileNotFoundError, ProcessLookupError):
                if isinstance(self.gate_root, Path):
                    raise

    def read_pwd(self, process: Path, pid: int) -> None:
        fields = (process / "environ").read_bytes().split(b"\0")
        for field in fields:
            if field.startswith(b"PWD="):
                path = Path(os.fsdecode(field[4:]))
                if path.is_absolute():
                    self.pwd_users.append((path, pid))


def task_state(status: Sequence[str], uid: int) -> CleanupTaskState:
    users = next(line.split()[1:3] for line in status if line.startswith("Uid:"))
    if len(users) != 2 or not all(user.isdecimal() for user in users):
        raise ValueError("cannot read the process user")
    if uid and str(uid) not in users:
        return CleanupTaskState.OTHER_USER
    state = next(line.split()[1] for line in status if line.startswith("State:"))
    if state in ("Z", "X") or "Kthread:\t1" in status:
        return CleanupTaskState.ENDED
    return CleanupTaskState.ALIVE


def task_read_failed(task: Path, error: Exception) -> bool:
    if not isinstance(error, (FileNotFoundError, ProcessLookupError)):
        return True
    if process_ended(task):
        return False
    try:
        status = (task / "status").read_text(errors="replace").splitlines()
        return task_state(status, 0) is not CleanupTaskState.ENDED
    except (FileNotFoundError, ProcessLookupError):
        return not process_ended(task)
    except (OSError, ValueError, StopIteration, IndexError):
        return True


def scan_cleanup_process(process: Path, scan: CleanupProcessScan, uid: int, ignored: frozenset[int]) -> bool:
    pid = int(process.name)
    status = (process / "status").read_text(errors="replace").splitlines()
    leader_state = task_state(status, uid)
    if leader_state is CleanupTaskState.OTHER_USER:
        return False
    threaded = leader_state is CleanupTaskState.ENDED or not any(
        line.startswith("Threads:") and line.split() == ["Threads:", "1"] for line in status)
    tasks = ((task for task in (process / "task").iterdir()
              if task.name.isdecimal() and task.name != process.name) if threaded else ())
    unreadable = False
    shared_paths_read = False
    for task in chain((process,), tasks):
        try:
            leader = task == process
            state = (leader_state if leader
                     else task_state((task / "status").read_text(errors="replace").splitlines(), uid))
            if state is not CleanupTaskState.ALIVE:
                continue
            if not shared_paths_read:
                scan.read_pwd(task, pid)
            scan.read_task(task, pid, ignored if leader else tmp_sweep.NO_IGNORED_FDS,
                           threaded=threaded, read_shared_paths=not shared_paths_read)
            shared_paths_read = True
        except (OSError, ValueError, StopIteration, IndexError) as error:
            unreadable |= task_read_failed(task, error)
        if len(scan.folders) == 1 and isinstance(scan.gate_root, GateRootNotSelected):
            if scan.paths and scan.paths[-1][1] == pid:
                break
    return unreadable


class ProcessTableAbsent(Enum):
    ABSENT = "no local process table"


def scan_cleanup_processes(paths: Sequence[Path], gate_root: Path | GateRootNotSelected, ignored_fds: frozenset[int],
                           worker_fds: tmp_sweep.WorkerRemovalDescriptors | tmp_sweep.NoWorkerDescriptors
                           ) -> CleanupProcessReview | ProcessTableAbsent:
    try:
        with os.scandir(PROC_ROOT) as entries:
            processes = [Path(entry.path) for entry in entries if entry.name.isdecimal()]
    except FileNotFoundError:
        return ProcessTableAbsent.ABSENT
    except OSError:
        reason = removal_guard.Kept(incomplete_scan_reason(()))
        return CleanupProcessReview(path_failure=reason, pwd_failure=reason)
    gate_device = GateDeviceUnavailable.UNKNOWN
    gate_paths = GatePathUse.CLEAR
    if isinstance(gate_root, Path):
        try:
            gate_device = gate_root.stat().st_dev
        except OSError:
            gate_paths = GatePathUse.UNREADABLE
    folders = frozenset(os.fsencode(path) for path in paths)
    scan = CleanupProcessScan(folders, tuple(path.rstrip(b"/") + b"/" for path in folders),
                              gate_root, gate_device, gate_paths)
    unreadable: dict[int, str] = {}
    uid = os.geteuid()
    for process in processes:
        pid = int(process.name)
        ignored = ignored_fds if pid == os.getpid() else tmp_sweep.NO_IGNORED_FDS
        if isinstance(worker_fds, tmp_sweep.WorkerRemovalDescriptors) and pid == worker_fds.pid:
            ignored |= worker_fds.numbers
        try:
            if scan_cleanup_process(process, scan, uid, ignored):
                unreadable[pid] = process_name(pid)
        except (OSError, ValueError, StopIteration, IndexError) as error:
            if task_read_failed(process, error):
                unreadable[pid] = process_name(pid)
    failures = tuple(sorted(unreadable.items()))
    reason = removal_guard.Kept(incomplete_scan_reason(failures)) if failures else NoUseFound.CLEAR
    return CleanupProcessReview(tuple(scan.paths), reason, tuple(scan.pwd_users), reason, failures, scan.gate_paths)


def read_process_use(paths: Sequence[Path], *, check_links: bool,
                     ignored_fds: frozenset[int] = tmp_sweep.NO_IGNORED_FDS,
                     worker_fds: tmp_sweep.WorkerRemovalDescriptors | tmp_sweep.NoWorkerDescriptors =
                     tmp_sweep.NoWorkerDescriptors.NONE,
                     gate_root: Path | GateRootNotSelected = GateRootNotSelected.NOT_SELECTED) -> CleanupProcessReview:
    if not (paths or check_links):
        return CleanupProcessReview()
    scan = scan_cleanup_processes(paths, gate_root, ignored_fds, worker_fds)
    if isinstance(scan, CleanupProcessReview):
        return scan
    failure = removal_guard.Kept(incomplete_scan_reason(()))
    if isinstance(worker_fds, tmp_sweep.WorkerRemovalDescriptors):
        return CleanupProcessReview(path_failure=failure, pwd_failure=failure)
    pwd_failure = removal_guard.Kept("cannot read the process PWD list") if check_links else NoUseFound.CLEAR
    if not paths:
        return CleanupProcessReview(pwd_failure=pwd_failure)
    prefix = tuple(map(str, paths))
    uses = (tmp_sweep.lsof_paths(prefix, require_complete=True, ignored_self_fds=ignored_fds, same_user_only=True)
            if ignored_fds else tmp_sweep.lsof_paths(prefix, require_complete=True, same_user_only=True))
    if not isinstance(uses, tmp_sweep.ProcessUsesKnown):
        return CleanupProcessReview(path_failure=failure, pwd_failure=pwd_failure)
    return CleanupProcessReview(tuple(uses.paths), pwd_failure=pwd_failure)


def answer_process_check(channel: WorkerChannel, request: dict[str, object]) -> None:
    pid = request.get("pid")
    links = request.get("links")
    paths = message_array(request.get("paths"))
    fds = message_array(request.get("descriptors"))
    gate = request.get("gate_root")
    if (type(pid) is not int or pid <= 0 or not isinstance(links, bool)
            or not isinstance(gate, str) or (gate and (not Path(gate).is_absolute() or ".." in Path(gate).parts))
            or any(not isinstance(path, str) or not path.startswith("/") for path in paths)
            or any(type(fd) is not int or fd <= 2 for fd in fds)):
        raise ValueError("invalid cleanup process request")
    uses = read_process_use(
        tuple(map(Path, cast(list[str], paths))), check_links=links,
        worker_fds=tmp_sweep.WorkerRemovalDescriptors(pid, frozenset(cast(list[int], fds))),
        gate_root=Path(gate) if gate else GateRootNotSelected.NOT_SELECTED,
    )
    result: ProcessCheckResult = {
        "kind": "process-result", "paths": uses.paths, "pwd_users": tuple((str(path), pid) for path, pid in uses.pwd_users),
        "path_failure": uses.path_failure.reason if isinstance(uses.path_failure, removal_guard.Kept) else "",
        "pwd_failure": uses.pwd_failure.reason if isinstance(uses.pwd_failure, removal_guard.Kept) else "",
        "unreadable": uses.unreadable_processes,
        "gate_paths": uses.gate_paths.value,
    }
    channel.send(result)


def keep_for_process(path: Path, uses: CleanupProcessReview, *,
                     sidecars: tuple[Path, ...] = (),
                     other_folders: tuple[Path, ...] = ()) -> removal_guard.Kept | NoUseFound:
    if isinstance(uses.path_failure, removal_guard.Kept):
        return uses.path_failure
    return find_process_use(path, uses, sidecars=sidecars, other_folders=other_folders)


def find_process_use(path: Path, uses: CleanupProcessReview, *, sidecars: tuple[Path, ...],
                     other_folders: tuple[Path, ...] = ()) -> removal_guard.Kept | NoUseFound:
    folders = tuple(map(str, (path, *other_folders)))
    sidecar_names = frozenset(map(str, sidecars))
    for name, pid, action in sorted(uses.all_uses()):
        if name in sidecar_names or any(name == folder or name.startswith(folder + "/") for folder in folders):
            return removal_guard.Kept(f"pid {pid} {action}")
    return NoUseFound.CLEAR


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


def list_only(reason: removal_guard.Kept, outcome: WouldMove | WouldRemove | removal_guard.Kept) -> removal_guard.Kept:
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
    try:
        details = worktree_git_state(checked)
    except GateGitStorage as error:
        return Row(path, None, removal_guard.Kept(str(error)), place=CleanupPlace.REVIEW_FOLDER)
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
    except GateGitStorage:
        raise
    except (OSError, ValueError, StopIteration, removal_guard.Refused) as error:
        return f"Git state unavailable: {table_text(removal_guard.single_line(error))}"


class LeasePidCheck(Enum):
    MATCH_START = "match the lease PID and start time"
    ANY_LIVE_PID = "keep the place while the lease PID is alive"
    MATCH_START_IF_PRESENT = "match the start time when the record has one"


@dataclass(frozen=True)
class FolderLeaseReview:
    status: LeaseStatus
    keep: removal_guard.Kept | NoUseFound = NoUseFound.CLEAR


def review_lease(path: Path, check: LeasePidCheck) -> FolderLeaseReview:
    try:
        fields = copies.clone_lease_fields(path)
    except FileNotFoundError:
        return FolderLeaseReview(LeaseStatus(LeaseState.ABSENT))
    except (OSError, ValueError):
        return FolderLeaseReview(LeaseStatus(LeaseState.UNREADABLE), removal_guard.Kept("cannot read the folder lease"))
    pid = fields.get("pid")
    start = fields.get("process_start")
    if type(pid) is not int or pid <= 0:
        return FolderLeaseReview(LeaseStatus(LeaseState.UNREADABLE), removal_guard.Kept("cannot read the lease PID"))
    owner = f"PID {pid}"
    if isinstance(start, str) and start:
        owner += f" start {start!r}"
    unreadable = LeaseStatus(LeaseState.UNREADABLE, owner)
    invalid_start = removal_guard.Kept(f"cannot verify the start time for lease PID {pid}")
    linux = sys.platform != "darwin"
    start_identity = ""
    if check is LeasePidCheck.MATCH_START_IF_PRESENT and "process_start" in fields:
        try:
            start_identity = copies.clone_process_identity(start if isinstance(start, str) else "", linux=linux)
        except ValueError:
            return FolderLeaseReview(unreadable, invalid_start)
    current = process_start.process_start(pid)
    if isinstance(current, process_start.Unreadable):
        return FolderLeaseReview(unreadable, removal_guard.Kept(f"cannot verify lease PID {pid}"))
    if isinstance(current, process_start.Started):
        if check is LeasePidCheck.MATCH_START and (not isinstance(start, str) or not start):
            return FolderLeaseReview(unreadable, invalid_start)
        try:
            matches = (copies.clone_process_identity(current.start, linux=linux) == start_identity
                       if start_identity else current.start == start)
        except ValueError:
            return FolderLeaseReview(unreadable, invalid_start)
        if check is LeasePidCheck.ANY_LIVE_PID or start is None or matches:
            return FolderLeaseReview(LeaseStatus(LeaseState.ALIVE, owner),
                                     removal_guard.Kept(f"lease PID {pid} is still running"))
    return FolderLeaseReview(LeaseStatus(LeaseState.DEAD, owner))


def keep_for_lease(path: Path, *, check: LeasePidCheck = LeasePidCheck.MATCH_START) -> removal_guard.Kept | NoUseFound:
    return review_lease(path, check).keep


def keep_for_leases(path: Path, lease_owners: Sequence[Path], check: LeasePidCheck) -> removal_guard.Kept | NoUseFound:
    if isinstance(reason := keep_for_lease(path, check=check), removal_guard.Kept):
        return reason
    nested_check = LeasePidCheck.MATCH_START if check is LeasePidCheck.ANY_LIVE_PID else check
    for owner in lease_owners:
        if isinstance(reason := keep_for_lease(owner, check=nested_check), removal_guard.Kept):
            return reason
    return NoUseFound.CLEAR


def worktree_sidecar_names(path: Path) -> tuple[str, str]:
    return build_folders.lease_path(path).name, path.name + ".lock"


def related_sidecars(path: Path, machine: Machine, bounds: CleanupBounds) -> tuple[Path, ...]:
    if (bounds.is_gate_child(path) or (machine is Machine.MAC and path == bounds.home / MAC_OLD_WORKTREE)
            or (machine is Machine.NATEDEV and path.parent in (bounds.build, LINUX_BUILD_ROOT)
                and path.name.startswith("p-"))):
        return tuple(path.with_name(name) for name in worktree_sidecar_names(path))
    return ()


@dataclass(frozen=True)
class WorktreeSidecars:
    stamps: dict[str, build_folders.SidecarIdentity]
    size: int
    newest_ns: int | GateEvidenceUnavailable

    def bind(self, fingerprint: str) -> str:
        return digest((fingerprint, self.stamps)) if self.stamps else fingerprint

    def newest_with(self, modified_ns: int) -> int:
        return max(modified_ns, self.newest_ns) if isinstance(self.newest_ns, int) else modified_ns


def inspect_sidecars(parent: int, guard: CleanupGuard, related: tuple[Path, ...]) -> WorktreeSidecars:
    stamps: dict[str, build_folders.SidecarIdentity] = {}
    size = 0
    modified: list[int] = []
    device = os.fstat(parent).st_dev
    for path in related:
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if path.parent == guard.bounds.gate and info.st_uid != os.geteuid():
            raise removal_guard.ChangedEntry(GATE_OTHER_OWNER)
        key = removal_guard.identity(info)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != device
                or guard.crosses_mount(path, key)):
            raise removal_guard.ChangedEntry(f"cannot verify worktree sidecar: {path}")
        stamps[path.name] = (*key, info.st_ctime_ns)
        size += info.st_blocks * 512
        modified.append(info.st_mtime_ns)
    return WorktreeSidecars(stamps, size, max(modified) if modified else GateEvidenceUnavailable.UNKNOWN)


def confirm_sidecars(checked: CheckedCleanup, guard: CleanupGuard, related: tuple[Path, ...],
                     lease: build_folders.FolderRemovalLease, approved: WorktreeSidecars) -> None:
    if (inspect_sidecars(checked.parent, guard, related) != approved or (approved.stamps and (
            not isinstance(lease, build_folders.HeldFolderRemovalLease)
            or lease.sidecar_identities() != approved.stamps))):
        raise removal_guard.ChangedEntry("worktree sidecars changed or their lock is unavailable")
    check = (LeasePidCheck.MATCH_START_IF_PRESENT if guard.bounds.is_gate_child(checked.path)
             else LeasePidCheck.ANY_LIVE_PID)
    if isinstance(reason := keep_for_lease(checked.path, check=check), removal_guard.Kept):
        raise removal_guard.ChangedEntry(reason.reason)


@contextmanager
def approved_sidecar_targets(path: Path, guard: CleanupGuard, lease: build_folders.HeldFolderRemovalLease,
                             approved: WorktreeSidecars) -> Generator[list[removal_guard.CleanupTarget]]:
    lock_stamp = approved.stamps.get(worktree_sidecar_names(path)[1])
    if lock_stamp is None or lease.sidecar_identities() != approved.stamps:
        raise removal_guard.ChangedEntry("worktree sidecars changed")
    check = (LeasePidCheck.MATCH_START_IF_PRESENT if guard.bounds.is_gate_child(path)
             else LeasePidCheck.ANY_LIVE_PID)
    if isinstance(reason := keep_for_lease(path, check=check), removal_guard.Kept):
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
            approval = removal_guard.ApprovedCleanup({place: CleanupEntry(stamp, ())}, removal_guard.CleanupRemoval.PLAIN)
            targets.append(removal_guard.CleanupTarget(checked, approval))
        yield targets


def finish_sidecars(path: Path, guard: CleanupGuard, lease: build_folders.HeldFolderRemovalLease,
                    approved: WorktreeSidecars, *, ignored_fds: Sequence[int] = ()) -> None:
    try:
        with approved_sidecar_targets(path, guard, lease, approved) as targets:
            first, *other = targets
            descriptors = (*ignored_fds, lease.descriptor, *(target.checked.parent for target in targets))
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


def gate_kind(path: Path) -> str:
    if path.name.endswith((".lock", ".lease.json")):
        return UNKNOWN_GATE_KIND
    return next((prefix for prefix in GATE_PREFIXES if path.name.startswith(prefix + "-")), UNKNOWN_GATE_KIND)


def candidate_order(candidate: Candidate) -> tuple[int, str, Path]:
    if candidate.place in (CleanupPlace.GATE_FOLDER, CleanupPlace.GATE_SIDECARS):
        kind = gate_kind(candidate.path)
        return (0 if kind in TEST_GATE_KINDS else 1), kind, candidate.path
    return 2, "", candidate.path


def gate_tree_refusal(kind: str, tree: CleanupTree) -> str:
    if kind in ("gate-recovery", "merge-gate-recovery") or tree.gate_issue == GATE_RECOVERY:
        return GATE_RECOVERY
    if tree.gate_issue == GATE_ALTERNATES:
        return GATE_ALTERNATES
    if tree.git is not CleanupGit.NONE:
        return GATE_GIT_KEEP_REASON
    if tree.gate_issue:
        return tree.gate_issue
    return "unknown gate entry type" if kind == UNKNOWN_GATE_KIND else ""


def gate_move_target(path: Path, bounds: CleanupBounds) -> Path:
    return build_folders.unit_folder("claude", "resources", root=bounds.build) / "shm-moved" / path.name


@contextmanager
def gate_move_parent(target: Path, bounds: CleanupBounds, *, create: bool) -> Generator[int]:
    from scripts.lib import session_move

    with ExitStack() as stack:
        directory = stack.enter_context(session_move.open_directory(bounds.build))
        mount = session_move.mount_identity(directory)
        for name in target.parent.relative_to(bounds.build).parts:
            directory = stack.enter_context(session_move.child_directory(directory, name, create=create, mount=mount))
        require_gate_move_parent_owner(directory)
        yield directory


def require_gate_move_parent_owner(parent: int) -> None:
    if os.fstat(parent).st_uid != os.geteuid():
        raise removal_guard.ChangedEntry("the move target parent belongs to another user")


def require_vacant_move_target(target: Path, directory: int) -> None:
    for name in (target.name, *worktree_sidecar_names(target)):
        try:
            _ = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise removal_guard.ChangedEntry(f"move target already exists: {target.with_name(name)}")


def require_movable_git(kind: str, tree: CleanupTree, entries: dict[Path, CleanupEntry]) -> None:
    if kind in ("gate-recovery", "merge-gate-recovery"):
        raise removal_guard.ChangedEntry(GATE_RECOVERY)
    if tree.gate_issue or kind == UNKNOWN_GATE_KIND:
        raise removal_guard.ChangedEntry(tree.gate_issue or "unknown gate entry type")
    if any(entry_path.name.casefold() == ".git" and stat.S_ISREG(entry.stamp.mode)
           for entry_path, entry in entries.items()):
        raise removal_guard.ChangedEntry("linked worktree; its registration is elsewhere")
    if tree.git is CleanupGit.NONE:
        raise removal_guard.ChangedEntry("no Git repository to move")


def gate_move_leases(path: Path, tree: CleanupTree) -> tuple[FolderLeaseReview, ...]:
    return tuple(review_lease(owner, LeasePidCheck.MATCH_START_IF_PRESENT)
                 for owner in (path, *tree.lease_owners))


def assess_gate(candidate: Candidate, guard: CleanupGuard, uses: CleanupProcessReview,
                related: tuple[Path, ...], mode: CleanupMode) -> Row:
    path = candidate.path
    kind = gate_kind(path)
    check = LeasePidCheck.MATCH_START_IF_PRESENT
    use = find_process_use(path, uses, sidecars=related)
    use_notes = tuple(reason.reason for reason in (use, uses.path_failure) if isinstance(reason, removal_guard.Kept))
    if uses.gate_paths is GatePathUse.ALIAS:
        use_notes = (uses.gate_paths.value, *use_notes)
    elif uses.gate_paths is GatePathUse.UNREADABLE:
        use_notes += (uses.gate_paths.value,)
    lease_statuses = (LeaseStatus(LeaseState.UNREADABLE),)
    newest: int | GateEvidenceUnavailable = GateEvidenceUnavailable.UNKNOWN
    size: int | None = None
    names: tuple[str, ...] = ()
    outcome: removal_guard.Kept | WouldRemove | WouldMove
    try:
        with build_folder_lock.folder_parent(path, Path("/"), create=False) as parent:
            orphan = candidate.place is CleanupPlace.GATE_SIDECARS
            sidecars = orphan_sidecars(path, parent, guard) if orphan else inspect_sidecars(parent, guard, related)
            names = tuple(sidecars.stamps)
            base_lease = review_lease(path, check)
            lease_statuses = (base_lease.status,)
            if orphan:
                size, newest = sidecars.size, sidecars.newest_ns
                notes = [*use_notes]
                if isinstance(base_lease.keep, removal_guard.Kept):
                    notes.append(base_lease.keep.reason)
                if notes:
                    raise removal_guard.ChangedEntry("orphan gate sidecars. " + ". ".join(notes))
                raise removal_guard.ChangedEntry("orphan gate sidecars")
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            size = info.st_blocks * 512 + sidecars.size
            newest = sidecars.newest_with(info.st_mtime_ns)
            if path.name.casefold() == ".git":
                raise removal_guard.ChangedEntry(GATE_GIT_KEEP_REASON)
            if not stat.S_ISDIR(info.st_mode):
                if info.st_uid != os.geteuid():
                    raise removal_guard.ChangedEntry(GATE_OTHER_OWNER)
                raise removal_guard.ChangedEntry(
                    "gate entry is a link" if stat.S_ISLNK(info.st_mode) else "unknown gate entry type")
            size, newest = None, GateEvidenceUnavailable.UNKNOWN
            with guard.check(path) as checked:
                entries: dict[Path, CleanupEntry] = {}
                tree = inspect_cleanup_tree(checked, entries if mode is CleanupMode.MOVE_GATE_GIT else Snapshot.PREVIEW)
                size = tree.size + sidecars.size
                newest = sidecars.newest_with(tree.newest_ns)
                leases = (base_lease, *(review_lease(owner, check) for owner in tree.lease_owners))
                lease_statuses = tuple(lease.status for lease in leases if lease.status.state is not LeaseState.ABSENT)
                moving = mode is CleanupMode.MOVE_GATE_GIT and tree.git is not CleanupGit.NONE
                if moving:
                    require_movable_git(kind, tree, entries)
                elif reason := gate_tree_refusal(kind, tree):
                    raise removal_guard.ChangedEntry(reason)
                if use_notes:
                    raise removal_guard.ChangedEntry(use_notes[0])
                for owner in leases:
                    if isinstance(owner.keep, removal_guard.Kept):
                        raise removal_guard.ChangedEntry(owner.keep.reason)
                with build_folders.folder_removal_lease(path, children=True, wait=False, root=Path("/")) as lease:
                    if isinstance(lease, build_folders.RefusedRemovalLease):
                        raise removal_guard.ChangedEntry("a folder lock is held or unreadable")
                    if moving and (candidate.decision is RemovalAssessment.PENDING
                                   or candidate.decision == NO_CLEANUP_SETTING):
                        if isinstance(lease, build_folders.HeldFolderRemovalLease):
                            confirm_sidecars(checked, guard, related, lease, sidecars)
                        if inspect_sidecars(checked.parent, guard, related) != sidecars:
                            raise removal_guard.ChangedEntry("worktree sidecars changed")
                        from scripts.lib import session_move

                        target = gate_move_target(path, guard.bounds)
                        with session_move.open_directory(guard.bounds.build):
                            pass
                        try:
                            with gate_move_parent(target, guard.bounds, create=False) as directory:
                                require_vacant_move_target(target, directory)
                        except FileNotFoundError:
                            pass
                        outcome = WouldMove(target, sidecars.bind(tree.fingerprint))
                    else:
                        confirm_sidecars(checked, guard, related, lease, sidecars)
                        if candidate.decision is RemovalAssessment.PENDING:
                            outcome = WouldRemove(sidecars.bind(tree.fingerprint))
                        else:
                            outcome = GATE_IF_NAMED if candidate.decision == NO_CLEANUP_SETTING else candidate.decision
    except (OSError, removal_guard.Refused, RuntimeError, ValueError) as error:
        reason = removal_guard.single_line(error)
        if kind in ("gate-recovery", "merge-gate-recovery") and reason != GATE_RECOVERY:
            reason = f"{GATE_RECOVERY}. {reason}"
        outcome = removal_guard.Kept(reason)
    details = GateDetails(kind, newest, lease_statuses, ", ".join(use_notes) or "none found")
    return Row(path, size, outcome, sidecars=names, place=candidate.place, gate=details)


def assess(candidates: Sequence[Candidate], guard: CleanupGuard, machine: Machine, *,
           mode: CleanupMode = CleanupMode.STANDARD) -> HostList:
    """Measure each guarded candidate and check process use and leases."""
    prefixes: list[Path] = []
    gate_root: Path | GateRootNotSelected = GateRootNotSelected.NOT_SELECTED
    for candidate in candidates:
        if candidate.place is CleanupPlace.ORPHAN_SIDECARS:
            prefixes.extend(candidate.path.with_name(name) for name in worktree_sidecar_names(candidate.path))
        elif (candidate.decision is RemovalAssessment.PENDING
              or candidate.place in (CleanupPlace.REVIEW_FOLDER, CleanupPlace.GATE_FOLDER, CleanupPlace.GATE_SIDECARS)):
            prefixes.extend((candidate.path, *related_sidecars(candidate.path, machine, guard.bounds)))
        if candidate.place in (CleanupPlace.GATE_FOLDER, CleanupPlace.GATE_SIDECARS):
            gate_root = guard.bounds.gate
    uses = process_uses(prefixes, check_links=any(candidate.place is CleanupPlace.LINK for candidate in candidates),
                        gate_root=gate_root)
    membership = (worktree_staff()
                  if any(candidate.place in (CleanupPlace.LINK, CleanupPlace.REVIEW_FOLDER) for candidate in candidates)
                  else staff.StaffSnapshot(()))
    rows: list[Row] = []
    for candidate in candidates:
        path = candidate.path
        if candidate.place is CleanupPlace.UNREADABLE_GATE_ROOT:
            assert isinstance(candidate.decision, removal_guard.Kept)
            rows.append(Row(path, None, candidate.decision, place=candidate.place))
            continue
        related = related_sidecars(path, machine, guard.bounds)
        if candidate.place in (CleanupPlace.GATE_FOLDER, CleanupPlace.GATE_SIDECARS):
            rows.append(assess_gate(candidate, guard, uses, related, mode))
            continue
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
                           other_folders: tuple[Path, ...] = (), check_links: bool = False,
                           gate_root: Path | GateRootNotSelected = GateRootNotSelected.NOT_SELECTED) -> None:
    uses = process_uses((path, *sidecars, *other_folders), check_links=check_links, ignored_fds=frozenset(fds),
                        gate_root=gate_root)
    if uses.gate_paths is not GatePathUse.CLEAR:
        raise removal_guard.ChangedEntry(uses.gate_paths.value)
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
    gate = guard.bounds.is_gate_child(path)
    gate_root = guard.bounds.gate if gate else GateRootNotSelected.NOT_SELECTED
    pid_check = (LeasePidCheck.MATCH_START_IF_PRESENT if gate
                 else LeasePidCheck.ANY_LIVE_PID if related else LeasePidCheck.MATCH_START)
    with guard.check(path) as checked:
        fds = [checked.parent]
        with build_folders.folder_removal_lease(path, children=True, wait=False, probe_fds=fds,
                                                root=Path("/"),
                                                create=not related and os.path.lexists(
                                                    build_folders.lease_path(path))) as lease:
            if isinstance(lease, build_folders.RefusedRemovalLease):
                raise removal_guard.ChangedEntry("a folder lock is held or unreadable")
            sidecars = inspect_sidecars(checked.parent, guard, related)
            require_no_process_use(path, fds, sidecars=related, gate_root=gate_root)
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
            if gate and (reason := gate_tree_refusal(gate_kind(path), tree)):
                raise removal_guard.ChangedEntry(reason)
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

                def before_delete() -> None:
                    require_no_process_use(path, fds, sidecars=related, gate_root=gate_root)
                    if gate:
                        recheck_leases(checked, guard, tree)

                remove_reviewed_entries(checked, removal_guard.ApprovedCleanup(
                    entries, removal_guard.CleanupRemoval.PLAIN), before_delete=before_delete)
            if sidecars.stamps and isinstance(lease, build_folders.HeldFolderRemovalLease):
                finish_sidecars(path, guard, lease, sidecars)


def copy_gate_tree(source: int, destination: int) -> None:
    result = subprocess.run(("cp", "-a", "--reflink=auto", "--one-file-system", "-T", "--",
                             f"/proc/self/fd/{source}/.", f"/proc/self/fd/{destination}/."),
                            pass_fds=(source, destination), capture_output=True, text=True, check=False)
    if result.returncode:
        raise removal_guard.ChangedEntry(f"cannot copy the gate folder: {result.stderr.strip()}")


def sync_gate_copy(destination: int, destination_parent: int) -> None:
    from scripts.lib import session_move

    mount = session_move.mount_identity(destination)
    for path, parent, info, kind in session_move.tree_entries(destination, mount):
        if kind is session_move.HashedEntry.FILE:
            with session_move.regular_file(parent, path.name, info, mount) as entry:
                os.fsync(entry)
        elif kind is session_move.HashedEntry.DIRECTORY:
            with session_move.child_directory(parent, path.name, mount=mount) as entry:
                os.fsync(entry)
    os.fsync(destination)
    os.fsync(destination_parent)


def copy_gate_sidecar(source: int, destination: int, name: str, expected: build_folders.SidecarIdentity) -> None:
    from scripts.lib import session_move

    info = os.stat(name, dir_fd=source, follow_symlinks=False)
    if (*removal_guard.identity(info), info.st_ctime_ns) != expected:
        raise removal_guard.ChangedEntry(f"worktree sidecar changed: {name}")
    with (session_move.regular_file(source, name, info, session_move.mount_identity(source)) as original,
          os.fdopen(original, "rb", closefd=False) as reader):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=destination)
        with os.fdopen(fd, "wb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fchmod(fd, stat.S_IMODE(info.st_mode))
            os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))
            os.fsync(fd)


def move_copy_differs(source: TreeEntry, target: TreeEntry,
                      before: os.stat_result, after: os.stat_result) -> bool:
    return (not source.same_content(target) or before.st_mode != after.st_mode
            or before.st_mtime_ns != after.st_mtime_ns)


@dataclass(frozen=True)
class VerifiedGateCopy:
    source: removal_guard.ProvedCopyCleanup
    sidecars: dict[str, EntryStamp]


def verify_gate_copy(checked: CheckedCleanup, source: int, target: int, target_parent: int,
                     entries: dict[Path, CleanupEntry], sidecars: WorktreeSidecars, *,
                     target_path: Path) -> VerifiedGateCopy:
    from scripts.lib import session_move

    proof = removal_guard.ProvedCopyCleanup(checked, source, removal_guard.CopyDestination(target_path, target), entries)
    copied_sidecars: dict[str, EntryStamp] = {}
    for name in worktree_sidecar_names(checked.path):
        expected = sidecars.stamps.get(name)
        try:
            after = os.stat(name, dir_fd=target_parent, follow_symlinks=False)
        except FileNotFoundError:
            if expected is None:
                continue
            raise
        if expected is None:
            raise removal_guard.ChangedEntry(f"unexpected sidecar at the move target: {name}")
        before = os.stat(name, dir_fd=checked.parent, follow_symlinks=False)
        if (*removal_guard.identity(before), before.st_ctime_ns) != expected or not stat.S_ISREG(after.st_mode):
            raise removal_guard.ChangedEntry(f"copy differs from the checked sidecar: {name}")
        original_file = session_move.read_entry(checked.parent, Path(name), before, session_move.HashedEntry.FILE,
                                                session_move.mount_identity(checked.parent))
        copied_file = session_move.read_entry(target_parent, Path(name), after, session_move.HashedEntry.FILE,
                                              session_move.mount_identity(target_parent))
        if move_copy_differs(original_file, copied_file, before, after):
            raise removal_guard.ChangedEntry(f"copy differs from the checked sidecar: {name}")
        copied_sidecars[name] = entry_stamp(after)
    return VerifiedGateCopy(proof, copied_sidecars)


def require_unchanged_gate_sidecars(target: Path, parent: int, verified: VerifiedGateCopy) -> None:
    sidecars: dict[str, EntryStamp] = {}
    for name in worktree_sidecar_names(target):
        try:
            sidecars[name] = entry_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False))
        except FileNotFoundError:
            continue
    if sidecars != verified.sidecars:
        raise removal_guard.ChangedEntry("the published copy changed during verification")


def publish_gate_copy(target: Path, parent: int, stage_name: str, stage: int, sidecars: WorktreeSidecars) -> None:
    from scripts.lib import session_move

    require_gate_move_parent_owner(parent)
    session_move.require_parent_path(target.parent, parent)
    session_move.require_parent_path(target.parent / stage_name, stage)
    require_vacant_move_target(target, parent)
    attempted: list[tuple[str, removal_guard.Identity]] = []
    try:
        for name in (target.name, *sidecars.stamps):
            identity = removal_guard.identity(os.stat(name, dir_fd=stage, follow_symlinks=False))
            # The rename helper can fail when it syncs a directory after the rename.
            attempted.append((name, identity))
            session_move.rename_exclusive(stage, name, parent, name)
        os.fsync(stage)
        os.fsync(parent)
        session_move.require_parent_path(target.parent / stage_name, stage)
        os.rmdir(stage_name, dir_fd=parent)
    except (OSError, ValueError) as error:
        failures: list[str] = []
        for name, identity in reversed(attempted):
            try:
                staged = session_move.stat_at(stage, name)
                if staged is not None and removal_guard.identity(staged) == identity:
                    continue
                removal_guard.require_identity(parent, name, identity)
                session_move.rename_exclusive(parent, name, stage, name)
            except (OSError, ValueError, removal_guard.Refused) as rollback_error:
                failures.append(f"{name}: {removal_guard.single_line(rollback_error)}")
        if failures:
            detail = ", ".join(failures)
            raise removal_guard.ChangedEntry(
                f"{removal_guard.single_line(error)}. Cannot return the published paths to staging: {detail}") from error
        raise


def move_gate_folder(path: Path, guard: CleanupGuard, approval: WouldMove, details: GateDetails) -> None:
    from scripts.lib import session_move

    target = gate_move_target(path, guard.bounds)
    if not guard.bounds.is_gate_child(path) or target != approval.target:
        raise removal_guard.ChangedEntry("the approved move path changed")
    related = related_sidecars(path, Machine.NATEDEV, guard.bounds)
    with guard.check(path) as checked:
        if checked.info.st_uid != os.geteuid():
            raise removal_guard.ChangedEntry(GATE_OTHER_OWNER)
        sidecars = inspect_sidecars(checked.parent, guard, related)
        fds = [checked.parent]
        with build_folders.folder_removal_lease(path, children=True, wait=False, create=True, transient_lock=True,
                                                probe_fds=fds, root=Path("/")) as lease:
            if not isinstance(lease, build_folders.HeldFolderRemovalLease):
                raise removal_guard.ChangedEntry("a folder lock is held or unreadable")
            locked_sidecars = inspect_sidecars(checked.parent, guard, related)
            lock_name = worktree_sidecar_names(path)[1]
            existing = {name: stamp for name, stamp in locked_sidecars.stamps.items()
                        if not (lease.created and name == lock_name)}
            if existing != sidecars.stamps or (lease.created and lock_name in sidecars.stamps):
                raise removal_guard.ChangedEntry("worktree sidecars changed")
            entries: dict[Path, CleanupEntry] = {}
            tree = inspect_cleanup_tree(checked, entries)
            require_movable_git(details.kind, tree, entries)
            if sidecars.bind(tree.fingerprint) != approval.fingerprint:
                raise removal_guard.ChangedEntry("identity or contents changed since the list was checked")

            def recheck_move_source(descriptors: Sequence[int]) -> None:
                confirm_sidecars(checked, guard, related, lease, locked_sidecars)
                leases = gate_move_leases(path, tree)
                for reviewed in leases:
                    if isinstance(reviewed.keep, removal_guard.Kept):
                        raise removal_guard.ChangedEntry(reviewed.keep.reason)
                states = tuple(reviewed.status for reviewed in leases if reviewed.status.state is not LeaseState.ABSENT)
                if states != details.leases:
                    raise removal_guard.ChangedEntry("lease state changed since the list was checked")
                require_no_process_use(path, descriptors, sidecars=related, gate_root=guard.bounds.gate)

            recheck_move_source(fds)
            with (session_move.child_directory(checked.parent, checked.name, mount=int(checked.mount)) as source,
                  gate_move_parent(target, guard.bounds, create=True) as target_parent):
                if entry_stamp(os.fstat(source)) != entries[path].stamp:
                    raise removal_guard.ChangedEntry("source folder changed before the copy")
                require_vacant_move_target(target, target_parent)
                mount = session_move.mount_identity(target_parent)
                stage_name = ".gate-move-" + secrets.token_hex(16)
                os.mkdir(stage_name, mode=0o700, dir_fd=target_parent)
                with (session_move.child_directory(target_parent, stage_name, mount=mount) as stage,
                      session_move.child_directory(stage, target.name, create=True, mount=mount) as destination):
                    copy_gate_tree(source, destination)
                    for name, expected in sidecars.stamps.items():
                        copy_gate_sidecar(checked.parent, stage, name, expected)
                    sync_gate_copy(destination, stage)
                    _ = verify_gate_copy(checked, source, destination, stage, entries, sidecars,
                                         target_path=target.parent / stage_name / target.name)
                    publish_gate_copy(target, target_parent, stage_name, stage, sidecars)

                    check_protected_masks()
                    verified = verify_gate_copy(checked, source, destination, target_parent, entries, sidecars,
                                                target_path=target)

                    def before_delete() -> None:
                        require_gate_move_parent_owner(target_parent)
                        session_move.require_parent_path(target.parent, target_parent)
                        check_protected_masks()
                        recheck_move_source((*fds, source))

                    removal_guard.remove_proved_copy(
                        checked, verified.source, approval.target, before_delete=before_delete,
                        after_copy_check=lambda: require_unchanged_gate_sidecars(target, target_parent, verified))
            if sidecars.stamps:
                finish_sidecars(path, guard, lease, locked_sidecars, ignored_fds=fds)


def apply(review: HostList, bounds: CleanupBounds, *, mode: CleanupMode = CleanupMode.STANDARD) -> int:
    """Recheck each approved row while holding its removal leases."""
    guard = read_cleanup_guard(frozenset(row.path for row in review.rows), bounds)
    status = 0
    for row in review.rows:
        if isinstance(row.outcome, removal_guard.Kept):
            continue
        if mode is not (CleanupMode.MOVE_GATE_GIT if isinstance(row.outcome, WouldMove) else CleanupMode.STANDARD):
            continue
        path = row.path
        try:
            if isinstance(row.outcome, WouldMove):
                if review.machine is not Machine.NATEDEV or not isinstance(row.gate, GateDetails):
                    raise removal_guard.ChangedEntry("only a natedev gate folder can move")
                move_gate_folder(path, guard, row.outcome, row.gate)
            elif row.place is CleanupPlace.LINK:
                remove_worktree_link(path, guard, row.outcome)
            elif row.place in (CleanupPlace.FOLDER, CleanupPlace.GATE_FOLDER):
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
            if isinstance(row.outcome, WouldMove):
                report_removal(f"moved {str(path)!r} to {str(row.outcome.target)!r}")
            else:
                report_removal(f"removed {str(path)!r}")
    return status


def run_remote(arguments: Sequence[str], *,
               user: SudoCleanupUser | InheritedCleanupUser = InheritedCleanupUser.CURRENT
               ) -> subprocess.CompletedProcess[str]:
    if isinstance(user, SudoCleanupUser):
        checkout = Path(__file__).absolute().parents[2]
        child = (
            "import sys\n"
            + "sys.path.insert(0, sys.argv[1])\n"
            + "from scripts.disks.cleanup_old_folders import run_remote_after_drop\n"
            + "raise SystemExit(run_remote_after_drop(sys.argv[2:]))"
        )
        command = user.command((sys.executable, "-IB", "-c", child, str(checkout), *arguments))
        return subprocess.run(command, capture_output=True, text=True, check=False, env=user.environment())
    return run_ssh_with_config(arguments, "mac_test")


def run_ssh_with_config(arguments: Sequence[str], config: str | Path) -> subprocess.CompletedProcess[str]:
    timeout = conf.read(config).number("floor_sweep_timeout_s")
    return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)


def ssh_timeout_detail(error: subprocess.TimeoutExpired) -> str:
    stderr = error.stderr
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    return stderr or f"SSH timed out after {error.timeout:g} seconds"


def run_remote_after_drop(arguments: Sequence[str]) -> int:
    try:
        require_sudo_caller()
        config = "mac_test" if "CONF_DIR" in os.environ else Path.home() / ".claude/config/mac_test.conf"
        result = run_ssh_with_config(arguments, config)
    except subprocess.TimeoutExpired as error:
        print(ssh_timeout_detail(error), file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def decode_gate_details(raw: object) -> GateDetails | NoGateDetails:
    if raw is None:
        return NoGateDetails.NOT_GATE_ROW
    fields = message_object(raw)
    kind, newest, lease, use = (fields.get(key) for key in ("kind", "newest_ns", "lease", "use"))
    if (not isinstance(kind, str) or kind not in (*GATE_PREFIXES, UNKNOWN_GATE_KIND)
            or (newest is not None and (type(newest) is not int or not -(1 << 64) <= newest < (1 << 64)))
            or not isinstance(use, str)):
        raise ValueError("invalid gate row details")
    leases = tuple(LeaseStatus(LeaseState(state), cast(str, owner)) for state, owner in message_rows(lease, (str, str)))
    return GateDetails(kind, newest if newest is not None else GateEvidenceUnavailable.UNKNOWN, leases, use)


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
        gate = decode_gate_details(fields.get("gate"))
        listed = message_array(fields.get("sidecars", []))
        sidecars = tuple(name for name in worktree_sidecar_names(path) if name in listed)
        if len(sidecars) != len(listed) or (machine is Machine.MAC and sidecars and isinstance(gate, NoGateDetails)
                                           and not path.as_posix().endswith("/" + MAC_OLD_WORKTREE)):
            raise ValueError("invalid cleanup sidecars")
        if machine is Machine.MAC:
            place = CleanupPlace.GATE_FOLDER if isinstance(gate, GateDetails) else CleanupPlace.FOLDER
        else:
            place = CleanupPlace(fields.get("place"))
        if isinstance(gate, GateDetails) != (place in (CleanupPlace.GATE_FOLDER, CleanupPlace.GATE_SIDECARS)):
            raise ValueError("invalid gate row place")
        if stamp and place not in (CleanupPlace.FOLDER, CleanupPlace.LINK, CleanupPlace.GATE_FOLDER):
            raise ValueError("invalid cleanup approval")
        move = fields.get("move_to")
        outcome: WouldMove | WouldRemove | removal_guard.Kept
        if move is not None:
            if (machine is not Machine.NATEDEV or place is not CleanupPlace.GATE_FOLDER or not stamp
                    or not isinstance(move, str) or not Path(move).is_absolute() or ".." in Path(move).parts
                    or Path(move).name != path.name):
                raise ValueError("invalid gate move approval")
            outcome = WouldMove(Path(move), stamp)
        else:
            outcome = removal_guard.Kept(keep) if keep else WouldRemove(stamp)
        rows.append(Row(path, size, outcome, git, sidecars, place, gate))
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
        detail = ssh_timeout_detail(error)
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


def show_gate_totals(review: HostList, mode: CleanupMode) -> None:
    groups: dict[tuple[str, str], list[int | None]] = {}
    kept: dict[str, list[int | None]] = {}
    for row in review.rows:
        if isinstance(row.gate, GateDetails):
            if isinstance(row.outcome, WouldMove):
                verdict = GATE_MOVE_LABEL
            elif isinstance(row.outcome, WouldRemove):
                verdict = "would remove"
            elif row.outcome == GATE_IF_NAMED:
                verdict = GATE_IF_NAMED.reason
            else:
                verdict = "kept"
                kept.setdefault(row.outcome.reason, []).append(row.size)
            groups.setdefault((row.gate.kind, verdict), []).append(row.size)
    show_moves = mode is CleanupMode.MOVE_GATE_GIT and review.machine is Machine.NATEDEV
    if groups or show_moves:
        def total(label: str, sizes: list[int | None]) -> str:
            gib = sum(size or 0 for size in sizes) / (1 << 30)
            unknown = sizes.count(None)
            note = f", {unknown} sizes unknown" if unknown else ""
            return f"{label}: {len(sizes)} ({gib:.3f} GiB{note})"

        test_sizes = [size for kind in TEST_GATE_KINDS for size in groups.get((kind, GATE_IF_NAMED.reason), [])]
        totals = [total(f"t and targeted-test {GATE_IF_NAMED.reason}", test_sizes)]
        moves = [size for (_, verdict), sizes in groups.items() if verdict == GATE_MOVE_LABEL for size in sizes]
        if moves or show_moves:
            totals.append(total(GATE_MOVE_LABEL, moves))
        for kind, verdict in sorted(groups, key=lambda group: (group[0] not in TEST_GATE_KINDS, group)):
            totals.append(total(f"{kind} {verdict}", groups[kind, verdict]))
        totals.extend(total(f"kept ({table_text(reason)})", sizes) for reason, sizes in sorted(kept.items()))
        print(f"Gate rows on {review.machine.value}: " + "; ".join(totals))


def show(local: HostList, mac: HostList | MacListUnavailable | LocalListOnly, approval: str, *,
         mode: CleanupMode = CleanupMode.STANDARD) -> None:
    uid = os.geteuid()
    if uid == 0:
        print("Use check: processes of all users.")
    else:
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
            if isinstance(row.outcome, WouldMove):
                reason = f"{GATE_MOVE_LABEL} at {str(row.outcome.target)!r}"
            else:
                reason = row.outcome.reason if isinstance(row.outcome, removal_guard.Kept) else "would remove"
            if row.git:
                reason = f"{reason}, {row.git}"
            if row.sidecars:
                reason = f"{reason}; includes {', '.join(map(repr, row.sidecars))}"
            if isinstance(row.gate, GateDetails):
                gate = row.gate
                newest = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(gate.newest_ns // 10**9))
                          if isinstance(gate.newest_ns, int) else gate.newest_ns.value)
                leases = ", ".join(lease.text() for lease in gate.leases) or LeaseState.ABSENT.value
                reason += f"; kind={gate.kind}; newest={newest}; lease={leases}; use={gate.use}"
            size = row.size if row.size is not None else "unknown"
            print(f"{review.machine.value}\t{str(row.path)!r}\t{size}\t{table_text(reason)}")
    if isinstance(mac, MacListUnavailable):
        print(f"mac\t-\tunknown\t{table_text(mac.message())}")
    for review in reviews:
        total = sum(row.size or 0 for row in review.rows)
        note = " measured (some sizes unknown)" if any(row.size is None for row in review.rows) else ""
        print(f"{review.machine.value} total: {total} bytes{note}")
        show_gate_totals(review, mode)
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


def read_gate_device(root: Path) -> int | GateDeviceUnavailable:
    try:
        info = root.lstat()
    except OSError:
        return GateDeviceUnavailable.UNKNOWN
    return info.st_dev if stat.S_ISDIR(info.st_mode) else GateDeviceUnavailable.UNKNOWN


def move_only_list(review: HostList) -> HostList:
    rows = tuple(replace(row, outcome=STANDARD_CLEANUP_ONLY) if isinstance(row.outcome, WouldRemove) else row
                 for row in review.rows)
    return replace(review, rows=rows)


def review_local(machine: Machine, *, mode: CleanupMode = CleanupMode.STANDARD) -> tuple[HostList, CleanupBounds]:
    gate = build_folders.configured_gate_root()
    if not gate.is_absolute() or ".." in gate.parts or gate == Path("/"):
        raise ValueError(f"invalid gate root: {gate}")
    bounds = CleanupBounds(Path.home(), build_folders.build_root(), removal_guard.known_names().repositories,
                           gate, read_gate_device(gate))
    candidates = collect(machine, conf.read("disks"), bounds)
    paths = frozenset(candidate.path for candidate in candidates)
    guard = read_cleanup_guard(paths, bounds)
    listed = assess(candidates, guard, machine, mode=mode)
    return (move_only_list(listed) if mode is CleanupMode.MOVE_GATE_GIT else listed), bounds


def show_apply_command(local: HostList, mac: HostList | MacListUnavailable | LocalListOnly, approval: str, *,
                       mode: CleanupMode, host_list: bool) -> None:
    sudo = mode is CleanupMode.MOVE_GATE_GIT
    outcome = WouldMove if sudo else WouldRemove
    rows = chain(local.rows, mac.rows) if not sudo and isinstance(mac, HostList) else local.rows
    applied = [row for row in rows if isinstance(row.outcome, outcome)]
    if applied:
        prefix = "sudo " if sudo else ""
        scope = " --host-list" if host_list else ""
        if sudo:
            gib = sum(row.size or 0 for row in applied) / (1 << 30)
            unknown = sum(row.size is None for row in applied)
            note = f" (size unknown for {unknown})" if unknown else ""
            folders = "folder" if len(applied) == 1 else "folders"
            print(f"{len(applied)} {folders}, {gib:.2f} GiB{note}, would move from memory to disk.")
        print(f"To {'move' if sudo else 'remove'} these folders, run:")
        print(f'{prefix}"$(command -v python3)" -IB "$HOME/.claude/scripts/disks/cleanup_old_folders.py"'
              + f"{scope} --go {approval}")


def finish_cleanup(args: Arguments, local: HostList, run_ssh: SshRunner, apply_local: Callable[[], int], *,
                   mode: CleanupMode = CleanupMode.STANDARD) -> int:
    local_record = local.record()
    if args.json:
        print(json.dumps(local_record))
        return 0
    mac = (remote_list(run_ssh) if local.machine is Machine.NATEDEV and not args.host_list
           else LocalListOnly.REQUESTED)
    approval = local_record["digest"]
    if isinstance(mac, HostList):
        if mode is CleanupMode.MOVE_GATE_GIT:
            mac = move_only_list(mac)
        approval = digest((approval, mac.record()["digest"]))
    elif isinstance(mac, MacListUnavailable):
        approval = digest((approval, MAC_UNREACHABLE))
    if args.go is None:
        show(local, mac, approval, mode=mode)
        show_apply_command(local, mac, approval, mode=mode, host_list=args.host_list)
        return 0
    if isinstance(mac, MacListUnavailable):
        print(mac.message(), file=sys.stderr)
    if not DIGEST.fullmatch(args.go) or not hmac.compare_digest(args.go, approval):
        print("cleanup_old_folders refused: the list digest changed", file=sys.stderr)
        return 1
    if mode is CleanupMode.STANDARD and isinstance(mac, HostList) and remove_remote(mac, run_ssh):
        return 1
    return apply_local()


def main(argv: Sequence[str], *, run_ssh: SshRunner = run_remote) -> int:
    args = parse_arguments(argv)
    try:
        if os.geteuid() == 0:
            _ = sudo_cleanup_user()
            raise ValueError("cleanup through sudo requires the Linux cleanup worker")
        local, bounds = review_local(local_machine())
        return finish_cleanup(args, local, run_ssh, lambda: apply(local, bounds))
    except (OSError, ValueError, removal_guard.Refused) as error:
        print(f"cleanup_old_folders refused: {removal_guard.single_line(error)}", file=sys.stderr)
        return 1


def worker_review(local: HostList) -> WorkerReview:
    rows: list[WorkerRowRecord] = [{**row_record(row), "place": row.place.value} for row in local.rows]
    return {"kind": "review", "rows": rows, "unreadable": local.unreadable_processes}


def serve_masked_cleanup(channel: WorkerChannel, build: Path, mounts: frozenset[Path], *,
                         mode: CleanupMode = CleanupMode.STANDARD) -> int:
    if os.geteuid() == 0:
        raise ValueError("the cleanup worker must run as an unprivileged user")
    if mode is CleanupMode.MOVE_GATE_GIT:
        require_sudo_caller()
    token = MASKED_WORKER_CHANNEL.set(channel)
    mask_token = MASKS_IN_FORCE.set(ProtectedFolderMasks(build, mounts))
    try:
        check_protected_masks()
        local, bounds = review_local(Machine.NATEDEV, mode=mode)
        check_protected_masks()
        channel.send(worker_review(local))
        line = channel.incoming.readline()
        check_protected_masks()
        if not line:
            return 0
        if message_object(cast(object, json.loads(line))) != {"kind": "apply"}:
            raise ValueError("invalid cleanup approval message")
        status = apply(local, bounds, mode=mode) if mode is CleanupMode.MOVE_GATE_GIT else apply(local, bounds)
        result: WorkerRunResult = {"kind": "applied", "status": status}
        channel.send(result)
        return 0
    finally:
        MASKS_IN_FORCE.reset(mask_token)
        MASKED_WORKER_CHANNEL.reset(token)


def masked_child(build: Path, mounts: frozenset[Path], *, mode: CleanupMode = CleanupMode.STANDARD) -> int:
    # The rescue and worktree helpers also start Git commands.
    sys.addaudithook(check_before_git)
    try:
        with os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8") as outgoing:
            _ = os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            return serve_masked_cleanup(WorkerChannel(sys.stdin, outgoing), build, mounts, mode=mode)
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


def run_masked_cleanup(command: Sequence[str], args: Arguments, run_ssh: SshRunner, *,
                       user: SudoCleanupUser | InheritedCleanupUser = InheritedCleanupUser.CURRENT) -> int:
    environment = None
    if isinstance(user, SudoCleanupUser):
        command = user.command(command)
        environment = user.environment()
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8",
                          env=environment) as child:
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

        mode = CleanupMode.MOVE_GATE_GIT if isinstance(user, SudoCleanupUser) else CleanupMode.STANDARD
        return finish_cleanup(args, local, run_ssh, apply_local, mode=mode)


def sudo_cleanup_ids() -> tuple[int, int]:
    uid, gid = (os.environ.get(name, "") for name in ("SUDO_UID", "SUDO_GID"))
    if not all(re.fullmatch(r"[0-9]{1,10}", value) for value in (uid, gid)):
        raise ValueError("root must use sudo with SUDO_UID and SUDO_GID")
    uid_number, gid_number = int(uid), int(gid)
    if not 0 < uid_number < 2**32 - 1 or not 0 <= gid_number < 2**32 - 1:
        raise ValueError("invalid SUDO_UID or SUDO_GID")
    return uid_number, gid_number


def require_sudo_caller() -> None:
    if (os.geteuid(), os.getegid()) != sudo_cleanup_ids():
        raise ValueError("the cleanup worker must run as the sudo caller")


def sudo_cleanup_user() -> SudoCleanupUser:
    uid_number, gid_number = sudo_cleanup_ids()
    try:
        account = pwd.getpwuid(uid_number)
    except KeyError:
        raise ValueError("SUDO_UID has no local account") from None
    setpriv = shutil.which("setpriv")
    if setpriv is None:
        raise ValueError("cleanup through sudo requires setpriv")
    return SudoCleanupUser(uid_number, gid_number, account.pw_name, Path(account.pw_dir), setpriv)


def run_cli(argv: Sequence[str], *, run_ssh: SshRunner = run_remote) -> int:
    if sys.platform != "linux":
        return main(argv, run_ssh=run_ssh)
    args = parse_arguments(argv)
    try:
        user = sudo_cleanup_user() if os.geteuid() == 0 else InheritedCleanupUser.CURRENT
        mode = CleanupMode.MOVE_GATE_GIT if isinstance(user, SudoCleanupUser) else CleanupMode.STANDARD
        _ = local_machine()
        checkout = Path(__file__).absolute().parents[2]
        child = (
            "from pathlib import Path\n"
            + "import sys\n"
            + "sys.path.insert(0, sys.argv[1])\n"
            + "from scripts.disks.cleanup_old_folders import CleanupMode, masked_child\n"
            + "raise SystemExit(masked_child(Path(sys.argv[2]), frozenset(map(Path, sys.argv[4:])), "
            + "mode=CleanupMode(sys.argv[3])))"
        )
        home = user.home if isinstance(user, SudoCleanupUser) else Path.home()
        mounts = protected_mounts(home, LINUX_BUILD_ROOT)
        command = masked_cleanup_command(mounts, (sys.executable, "-B", "-c", child, str(checkout),
                                                  str(LINUX_BUILD_ROOT), mode.value, *map(str, mounts)))
        if isinstance(user, SudoCleanupUser) and run_ssh is run_remote:
            run_ssh = lambda arguments: run_remote(arguments, user=user)
        return run_masked_cleanup(command, args, run_ssh, user=user)
    except (OSError, ValueError) as error:
        print(f"cleanup_old_folders refused: {removal_guard.single_line(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cli(sys.argv[1:]))
